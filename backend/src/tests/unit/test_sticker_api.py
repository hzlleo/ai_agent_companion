"""app/api/stickers 路由测试——上传 / 列表 / 修改 / 删除 / 标注 / 文件（ASGI 零网络）。

测试策略：本地装配 FastAPI app（stickers router + 共享异常处理器）；DB 经
共享会话级 SQLite fixture 外部安装到 engine holder；服务实例经 monkeypatch
替换为注配置 / 注 fake 网关 / tmp 存储的实现。覆盖面：上传 / 内联数据列表 /
单条读取 / 修改 / 删除（单条 + 批量）/ 标注（成功与失败状态）/ 文件字节 /
记录缺失 404 / 人格解析 404 / 功能关闭 409。
"""

from __future__ import annotations

import base64
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

import packages.harness.multimodal.sticker_orm as sticker_orm  # noqa: F401  模型注册副作用导入（共享建表）
import packages.harness.persona.orm as persona_orm  # noqa: F401  模型注册副作用导入（共享建表）
from app.api import stickers as stickers_api
from app.api.stickers import router as stickers_router
from app.middleware.error_handler import register_exception_handlers
from packages.harness.config.model_entry import LlmModelEntry, ModelRole
from packages.harness.config.model_resolver import ModelResolver
from packages.harness.core.errors import BusinessException, ErrorCode
from packages.harness.database.engine import db_engine_holder
from packages.harness.llm.llm_models import ChatMessageModel, ChatResponseModel
from packages.harness.multimodal.sticker_models import StickerSettings
from packages.harness.multimodal.sticker_service import StickerService
from packages.harness.persona.dtos import PersonaCreateDTO
from packages.harness.persona.service import PersonaService

PNG = b"\x89PNG\r\n\x1a\napi-png"


class FakeGateway:
    """测试用视觉标注网关（可注入失败）。"""

    def __init__(self) -> None:
        self.failure: BusinessException | None = None

    async def chat_for_entry(
        self,
        entry: LlmModelEntry,
        messages: Sequence[ChatMessageModel],
        *,
        persona_id: str | None = None,
        purpose: str = "chat",
        request_id: str | None = None,
    ) -> ChatResponseModel:
        """返回固定标注 JSON（失败注入时抛出）。"""
        if self.failure is not None:
            raise self.failure
        return ChatResponseModel(
            content='{"description": "比心", "tags": ["爱心", "比心"]}'
        )


def _resolver() -> ModelResolver:
    """构造多模态直连解析器。"""
    return ModelResolver(
        [
            LlmModelEntry(
                name="vision-api",
                base_url="http://vision.test/v1",
                model="qwen3-vl",
                roles=[ModelRole.LOOK_AT],
                multimodal=True,
            )
        ]
    )


def _item(payload: bytes = PNG, *, source_note: str = "自绘") -> dict[str, Any]:
    """构造上传条目 JSON。"""
    return {
        "image_base64": base64.b64encode(payload).decode("ascii"),
        "content_type": "image/png",
        "source_note": source_note,
        "tags": ["开心"],
        "description": "",
    }


async def _create_persona(db_session: AsyncSession, name: str, *, active: bool = False) -> int:
    """经真实 PersonaService 创建人格（可选启用）。"""
    created = await PersonaService().create(
        db_session,
        PersonaCreateDTO.model_validate(
            {
                "name": name,
                "status": "active" if active else "disabled",
                "card": {"name": "大肥鱼", "first_mes": "你回来啦！"},
            }
        ),
    )
    return created.id


@pytest.fixture
async def sticker_app(
    shared_db_engine: AsyncEngine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> AsyncIterator[FastAPI]:
    """装配最小 FastAPI app（本路由 + 异常处理器 + 注配置服务实例）。

    测试可通过 ``request.param`` 注入 ``StickerSettings`` 覆盖（缺省启用）。
    """
    settings = getattr(request, "param", None) or StickerSettings.model_validate(
        {"enabled": True, "max_upload_bytes": 1024}
    )
    db_engine_holder.async_engine = shared_db_engine
    db_engine_holder.session_factory = async_sessionmaker(
        bind=shared_db_engine, expire_on_commit=False
    )
    db_engine_holder.owner_token = None
    gateway = FakeGateway()
    service = StickerService(
        settings=settings,
        gateway=gateway,
        resolver=_resolver(),
        storage_root=tmp_path,
    )
    monkeypatch.setattr(stickers_api, "sticker_service", service)
    app = FastAPI(docs_url=None, redoc_url=None)
    app.include_router(stickers_router)
    register_exception_handlers(app)
    yield app
    db_engine_holder.reset()


@pytest.fixture
async def client(sticker_app: FastAPI) -> AsyncIterator[AsyncClient]:
    """ASGITransport 客户端（零真实网络）。"""
    transport = ASGITransport(app=sticker_app)
    async with AsyncClient(transport=transport, base_url="http://test") as async_client:
        yield async_client


@pytest.fixture
async def primary_persona_id(
    client: AsyncClient, db_session: AsyncSession
) -> AsyncIterator[int]:
    """铺底一个主人格（返回主键）。"""
    persona_id = await _create_persona(db_session, "sticker-primary", active=True)
    await PersonaService().set_primary(db_session, persona_id)
    yield persona_id


async def _data(response: Any) -> Any:
    """断言成功并返回 data 段。"""
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["success"] is True
    return body["data"]


async def test_sticker_upload_list_update_delete_flow(
    client: AsyncClient, primary_persona_id: int
) -> None:
    """完整链路：批量上传 → 内联列表 → 修改 → 标注 → 文件 → 单条/批量删除。"""
    uploaded = await _data(
        await client.post("/api/v1/stickers", json={"items": [_item(), _item(b"second")]})
    )
    assert len(uploaded["created"]) == 2
    sticker_id = uploaded["created"][0]["id"]
    second_id = uploaded["created"][1]["id"]

    listed = await _data(await client.get("/api/v1/stickers", params={"include_data": True}))
    assert len(listed) == 2
    assert listed[0]["data_base64"] is not None
    assert base64.b64decode(listed[0]["data_base64"]) in {PNG, b"second"}

    patched = await _data(
        await client.patch(
            f"/api/v1/stickers/{sticker_id}",
            json={"tags": ["新标签"], "description": "新描述", "source_note": "朋友授权"},
        )
    )
    assert patched["tags"] == ["新标签"]
    assert patched["source_note"] == "朋友授权"

    annotated = await _data(await client.post(f"/api/v1/stickers/{sticker_id}/annotate"))
    assert annotated["ok"] is True
    assert annotated["record"]["annotation_status"] == "done"
    assert "比心" in annotated["record"]["description"]

    file_response = await client.get(f"/api/v1/stickers/{sticker_id}/file")
    assert file_response.status_code == 200
    assert file_response.headers["content-type"] == "image/png"
    assert file_response.content == PNG

    single = await _data(await client.delete(f"/api/v1/stickers/{sticker_id}"))
    assert single["deleted_ids"] == [sticker_id]
    batch = await _data(
        await client.post("/api/v1/stickers/batch-delete", json={"ids": [second_id, 9999]})
    )
    assert batch["deleted_ids"] == [second_id]
    assert batch["missing_ids"] == [9999]


async def test_sticker_filters_and_validation(
    client: AsyncClient, primary_persona_id: int
) -> None:
    """标签过滤 / 状态过滤 / 非法请求体。"""
    await _data(
        await client.post(
            "/api/v1/stickers",
            json={"items": [_item(), _item(b"second", source_note="朋友授权")]},
        )
    )
    filtered = await _data(await client.get("/api/v1/stickers", params={"tag": "开心"}))
    assert len(filtered) == 2
    pending = await _data(
        await client.get("/api/v1/stickers", params={"annotation_status": "pending"})
    )
    assert len(pending) == 2
    bad = await client.post("/api/v1/stickers", json={"items": []})
    assert bad.status_code in (400, 422)


async def test_sticker_missing_record_404(
    client: AsyncClient, primary_persona_id: int
) -> None:
    """不存在的记录 → 404。"""
    assert (await client.get("/api/v1/stickers/9999")).status_code == 404
    assert (await client.get("/api/v1/stickers/9999/file")).status_code == 404
    assert (await client.patch("/api/v1/stickers/9999", json={"tags": []})).status_code == 404


async def test_sticker_annotation_failure_is_readable(
    client: AsyncClient, primary_persona_id: int
) -> None:
    """标注失败返回 ok=False + failed 状态（HTTP 仍 200）。"""
    uploaded = await _data(await client.post("/api/v1/stickers", json={"items": [_item()]}))
    sticker_id = uploaded["created"][0]["id"]
    gateway = stickers_api.sticker_service._gateway
    assert isinstance(gateway, FakeGateway)
    gateway.failure = BusinessException(code=ErrorCode.LLM_CALL_FAILED, message="视觉端点不可用")
    result = await _data(await client.post(f"/api/v1/stickers/{sticker_id}/annotate"))
    assert result["ok"] is False
    assert result["record"]["annotation_status"] == "failed"
    assert result["record"]["annotation_error"] is not None


async def test_sticker_explicit_persona_missing_404(client: AsyncClient) -> None:
    """显式 persona_id 不存在 → 404。"""
    response = await client.get("/api/v1/stickers", params={"persona_id": 9999})
    assert response.status_code == 404


@pytest.mark.parametrize("sticker_app", [StickerSettings(enabled=False)], indirect=True)
async def test_sticker_disabled_conflict(
    client: AsyncClient, primary_persona_id: int
) -> None:
    """功能关闭 → 409。"""
    response = await client.get("/api/v1/stickers")
    assert response.status_code == 409

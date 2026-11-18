"""app/api/personas 导入路由测试——GET /presets + POST /import（JSON 预设 /
卡片 / multipart 文件）+ POST /import/batch（ASGITransport 零真实网络）。

测试策略：本地装配 FastAPI app（router + 共享异常处理器——与
test_persona_api.py 同模式）；DB 经共享会话级 SQLite fixture 外部安装。
JSON 面与 multipart 面全走真实 HTTP 编码（httpx 原生 multipart——
stdlib email 解析器的边界 / 文件名归一经实证路径）。

覆盖面：预设库读取（11 预设 + 组件注册表）；JSON 导入矩阵（预设生成 /
未知预设 404 / 载荷恰一 422 / 期望名 / 同卡幂等 / 名称冲突三策略）；
multipart 单文件导入（含策略与期望名字段）；批量导入（部分成功 +
策略字段）；错误面（非 multipart 400 / 空体 400 / 非法 JSON 400 /
卡片违约 422）。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import pytest
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.api.personas import import_persona, list_trait_presets
from app.api.personas import router as personas_router
from app.middleware.error_handler import register_exception_handlers
from packages.harness.database.engine import db_engine_holder
from packages.harness.persona.trait_library import (
    load_official_components,
    load_official_presets,
)


@pytest.fixture
async def persona_app(shared_db_engine: AsyncEngine) -> AsyncIterator[FastAPI]:
    """本地装配的最小 FastAPI app（本路由 + 异常处理器；共享引擎外部安装）。

    Args:
        shared_db_engine: 会话级共享 SQLite 引擎（conftest 提供）。

    Yields:
        已注册路由与异常处理器的 :class:`FastAPI` 实例（用后复位 holder）。
    """
    db_engine_holder.async_engine = shared_db_engine
    db_engine_holder.session_factory = async_sessionmaker(
        bind=shared_db_engine, expire_on_commit=False
    )
    db_engine_holder.owner_token = None
    app = FastAPI(docs_url=None, redoc_url=None)
    app.include_router(personas_router)
    register_exception_handlers(app)
    yield app
    db_engine_holder.reset()


@pytest.fixture
async def client(persona_app: FastAPI) -> AsyncIterator[AsyncClient]:
    """ASGITransport 客户端（零真实网络 / 零真实服务器）。

    Args:
        persona_app: 本地装配的 FastAPI app。

    Yields:
        :class:`httpx.AsyncClient`（base_url=http://test）。
    """
    transport = ASGITransport(app=persona_app)
    async with AsyncClient(transport=transport, base_url="http://test") as async_client:
        yield async_client


async def _create_existing(client: AsyncClient, name: str = "feiyu") -> dict[str, Any]:
    """经既有创建端点铺底一个人格（返回 data 段）。"""
    response = await client.post(
        "/api/v1/personas",
        json={"name": name, "card": {"name": "大肥鱼", "first_mes": "你回来啦！"}},
    )
    assert response.status_code == 200
    body: dict[str, Any] = response.json()
    data = body["data"]
    assert isinstance(data, dict)
    return data


def _preset_payload(preset_id: str, **overrides: Any) -> dict[str, Any]:
    """构造 JSON 预设导入请求体。"""
    payload: dict[str, Any] = {"preset_id": preset_id}
    payload.update(overrides)
    return payload


def _card_payload(name: str = "小助手", **overrides: Any) -> dict[str, Any]:
    """构造 JSON 卡片导入请求体。"""
    payload: dict[str, Any] = {
        "card": {"name": name, "personality": "温柔耐心", "first_mes": "你好呀"}
    }
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------------------
# GET /presets —— 官方特质库读取
# ---------------------------------------------------------------------------


async def test_list_trait_presets_http(client: AsyncClient) -> None:
    """预设库读取：11 预设（名单与顺序）+ 组件注册表（≥120）全量下发。"""
    response = await client.get("/api/v1/personas/presets")
    assert response.status_code == 200
    body: dict[str, Any] = response.json()
    assert body["success"] is True
    data = body["data"]
    assert [preset["name_zh"] for preset in data["presets"]] == [
        "大家闺秀", "小家碧玉", "绿茶", "傲娇", "温柔治愈",
        "高冷御姐", "元气少女", "毒舌损友", "同窗旧友", "合租室友", "旧识恋人",
    ]
    assert len(data["components"]) >= 120
    levels = {preset["id"]: preset["intimacy_level"] for preset in data["presets"]}
    assert levels["green-tea"] == 2 and levels["refined-lady"] == 1
    assert levels["reunion-lover"] == 3
    for preset in data["presets"]:
        assert preset["scene_scripts"]["daily"] and preset["scene_scripts"]["flirty"]
    assert {component["id"] for component in data["components"]} == set(
        load_official_components()
    )


async def test_list_trait_presets_direct_call() -> None:
    """处理器直调契约面：ResultDTO 包装（success / code / data 形态）。"""
    result = await list_trait_presets()
    assert result.success is True
    assert result.code == "200"
    assert result.data is not None
    assert len(result.data.presets) == len(load_official_presets())


# ---------------------------------------------------------------------------
# POST /import —— JSON 面矩阵
# ---------------------------------------------------------------------------


async def test_import_preset_generates_and_creates(client: AsyncClient) -> None:
    """预设导入：确定性生成卡片落库（desired_name 缺省 = 预设中文名）。"""
    response = await client.post("/api/v1/personas/import", json=_preset_payload("tsundere"))
    assert response.status_code == 200
    body: dict[str, Any] = response.json()
    assert body["success"] is True
    assert body["message"] == "人格已导入"
    data = body["data"]
    assert data["imported"] is True
    assert data["reason"] is None
    assert data["warnings"] == []
    detail = await client.get(f"/api/v1/personas/{data['persona_id']}")
    card = detail.json()["data"]["card"]
    assert card["name"] == "傲娇"
    assert "口是心非" in card["personality"]
    assert card["system_prompt"] != ""


async def test_import_preset_with_desired_name_and_suffix(client: AsyncClient) -> None:
    """期望名 + 预设后缀：基础名 = 期望名，后缀由预设声明拼接。"""
    response = await client.post(
        "/api/v1/personas/import",
        json=_preset_payload("genki-girl", desired_name="小桃"),
    )
    assert response.status_code == 200
    data = response.json()["data"]
    detail = await client.get(f"/api/v1/personas/{data['persona_id']}")
    card = detail.json()["data"]["card"]
    assert card["name"] == "小桃-酱"


async def test_import_preset_intimacy_override_filters_components(
    client: AsyncClient,
) -> None:
    """覆盖等级：降到 1 时剔除预设内的暧昧组件，卡片不含任何人格级新字段。"""
    default = await client.post(
        "/api/v1/personas/import",
        json=_preset_payload("green-tea", desired_name="绿茶默认"),
    )
    assert default.status_code == 200
    default_card = (
        await client.get(f"/api/v1/personas/{default.json()['data']['persona_id']}")
    ).json()["data"]["card"]
    assert "话从不说满" in default_card["personality"]

    narrowed = await client.post(
        "/api/v1/personas/import",
        json=_preset_payload(
            "green-tea", desired_name="绿茶收敛", intimacy_level_override=1
        ),
    )
    assert narrowed.status_code == 200
    narrowed_card = (
        await client.get(f"/api/v1/personas/{narrowed.json()['data']['persona_id']}")
    ).json()["data"]["card"]
    assert "话从不说满" not in narrowed_card["personality"]
    assert "不经意" not in narrowed_card["personality"]
    assert "说话甜" in narrowed_card["personality"]
    assert "intimacy_level" not in narrowed_card


async def test_import_preset_override_narrows_explicit_pack(client: AsyncClient) -> None:
    """亲密包预设：默认纳入 explicit 组件，覆盖降到 1 时收窄为安全组件。"""
    default = await client.post(
        "/api/v1/personas/import",
        json=_preset_payload("reunion-lover", desired_name="旧识默认"),
    )
    assert default.status_code == 200
    default_card = (
        await client.get(f"/api/v1/personas/{default.json()['data']['persona_id']}")
    ).json()["data"]["card"]
    assert "心动起来藏不住" in default_card["personality"]
    assert "一有机会就要挨着" in default_card["personality"]

    narrowed = await client.post(
        "/api/v1/personas/import",
        json=_preset_payload(
            "reunion-lover", desired_name="旧识收敛", intimacy_level_override=1
        ),
    )
    assert narrowed.status_code == 200
    narrowed_card = (
        await client.get(f"/api/v1/personas/{narrowed.json()['data']['persona_id']}")
    ).json()["data"]["card"]
    assert "心动起来藏不住" not in narrowed_card["personality"]
    assert "关灯之后的谈话" not in narrowed_card["personality"]
    assert "记得各种日子" in narrowed_card["personality"]


async def test_import_preset_intimacy_override_out_of_range_422(
    client: AsyncClient,
) -> None:
    """覆盖等级越界：VALIDATION_ERROR 422（不静默夹取）。"""
    for bad in (-1, 4):
        response = await client.post(
            "/api/v1/personas/import",
            json=_preset_payload("tsundere", intimacy_level_override=bad),
        )
        assert response.status_code == 422
        assert response.json()["code"] == "VALIDATION_ERROR"


async def test_import_card_ignores_intimacy_override(client: AsyncClient) -> None:
    """卡片导入载荷可携带覆盖字段（模型接受），但不影响卡片内容与落库形态。"""
    response = await client.post(
        "/api/v1/personas/import",
        json=_card_payload("小助手", intimacy_level_override=3),
    )
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["imported"] is True
    card = (
        await client.get(f"/api/v1/personas/{data['persona_id']}")
    ).json()["data"]["card"]
    assert card["name"] == "小助手"
    assert "intimacy_level" not in card


async def test_import_preset_identical_content_idempotent(client: AsyncClient) -> None:
    """预设同卡幂等：同名同预设重导 → imported=false + identical_content。"""
    first = await client.post("/api/v1/personas/import", json=_preset_payload("tsundere"))
    assert first.json()["data"]["imported"] is True
    second = await client.post(
        "/api/v1/personas/import",
        json=_preset_payload("tsundere", conflict_strategy="overwrite"),
    )
    assert second.status_code == 200
    data = second.json()["data"]
    assert data["imported"] is False
    assert data["reason"] == "identical_content"
    assert data["persona_id"] == first.json()["data"]["persona_id"]


async def test_import_preset_unknown_404(client: AsyncClient) -> None:
    """未知预设 id：NOT_FOUND 404（ResultDTO error 形态）。"""
    response = await client.post(
        "/api/v1/personas/import", json=_preset_payload("nonexistent-preset")
    )
    assert response.status_code == 404
    body: dict[str, Any] = response.json()
    assert body["success"] is False
    assert body["code"] == "NOT_FOUND"


async def test_import_payload_exclusivity_422(client: AsyncClient) -> None:
    """载荷恰一：preset_id 与 card 同时提供 / 均缺 → VALIDATION_ERROR 422。"""
    both = await client.post(
        "/api/v1/personas/import",
        json={**_preset_payload("tsundere"), "card": {"name": "小助手"}},
    )
    assert both.status_code == 422
    assert both.json()["code"] == "VALIDATION_ERROR"
    neither = await client.post("/api/v1/personas/import", json={})
    assert neither.status_code == 422
    assert neither.json()["code"] == "VALIDATION_ERROR"


async def test_import_card_json_flow(client: AsyncClient) -> None:
    """卡片 JSON 导入：落库 + 期望名覆写注册名（卡片名原样保留）。"""
    response = await client.post(
        "/api/v1/personas/import",
        json=_card_payload("小助手", desired_name="my-helper"),
    )
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["imported"] is True
    detail = await client.get(f"/api/v1/personas/{data['persona_id']}")
    record = detail.json()["data"]
    assert record["name"] == "my-helper"
    assert record["card"]["name"] == "小助手"


async def test_import_name_conflict_matrix_over_json(client: AsyncClient) -> None:
    """名称冲突矩阵（JSON 卡片面）：skip 未导入 / rename 后缀 / overwrite 替换。"""
    await _create_existing(client, name="小助手")
    card = {"name": "小助手", "personality": "全新性格", "first_mes": "你好"}
    skip = await client.post(
        "/api/v1/personas/import", json={"card": card, "conflict_strategy": "skip"}
    )
    assert skip.status_code == 200
    skip_data = skip.json()["data"]
    assert skip_data["imported"] is False
    assert skip_data["reason"] == "name_conflict"
    existing_id = skip_data["persona_id"]
    rename = await client.post(
        "/api/v1/personas/import", json={"card": card, "conflict_strategy": "rename"}
    )
    rename_data = rename.json()["data"]
    assert rename_data["imported"] is True
    detail = await client.get(f"/api/v1/personas/{rename_data['persona_id']}")
    assert detail.json()["data"]["name"] == "小助手-2"
    overwrite = await client.post(
        "/api/v1/personas/import",
        json={"card": {**card, "personality": "覆盖性格"}, "conflict_strategy": "overwrite"},
    )
    overwrite_data = overwrite.json()["data"]
    assert overwrite_data["imported"] is True
    assert overwrite_data["persona_id"] == existing_id
    detail = await client.get(f"/api/v1/personas/{existing_id}")
    assert detail.json()["data"]["card"]["personality"] == "覆盖性格"


# ---------------------------------------------------------------------------
# POST /import —— multipart 面
# ---------------------------------------------------------------------------


async def test_import_multipart_single_file(client: AsyncClient) -> None:
    """multipart 单文件导入：file + conflict_strategy / desired_name 字段。"""
    card_bytes = json.dumps(
        {"name": "文件人格", "personality": "文件性格", "first_mes": "嗨"}
    ).encode("utf-8")
    response = await client.post(
        "/api/v1/personas/import",
        files={"file": ("persona.json", card_bytes, "application/json")},
        data={"desired_name": "file-persona"},
    )
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["imported"] is True
    detail = await client.get(f"/api/v1/personas/{data['persona_id']}")
    assert detail.json()["data"]["name"] == "file-persona"
    assert detail.json()["data"]["card"]["name"] == "文件人格"


async def test_import_multipart_conflict_strategy_rename(client: AsyncClient) -> None:
    """multipart + rename 策略：同名文件自动后缀落库。"""
    await _create_existing(client, name="文件人格")
    card_bytes = json.dumps({"name": "文件人格", "personality": "另一性格"}).encode("utf-8")
    response = await client.post(
        "/api/v1/personas/import",
        files={"file": ("persona.json", card_bytes, "application/json")},
        data={"conflict_strategy": "rename"},
    )
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["imported"] is True
    detail = await client.get(f"/api/v1/personas/{data['persona_id']}")
    assert detail.json()["data"]["name"] == "文件人格-2"


async def test_import_multipart_errors(client: AsyncClient) -> None:
    """multipart 错误面：空体 400 / 文件非法 JSON 422 / 卡片违约 422 /
    缺 file 字段 400。"""
    empty = await client.post(
        "/api/v1/personas/import",
        content=b"",
        headers={"content-type": "multipart/form-data; boundary=x"},
    )
    assert empty.status_code == 400
    bad_json = await client.post(
        "/api/v1/personas/import",
        files={"file": ("broken.json", b"not-json{", "application/json")},
    )
    assert bad_json.status_code == 422
    assert bad_json.json()["code"] == "VALIDATION_ERROR"
    bad_card = await client.post(
        "/api/v1/personas/import",
        files={"file": ("bad.json", json.dumps({"personality": "无名"}).encode(), "application/json")},
    )
    assert bad_card.status_code == 422
    missing_file = await client.post(
        "/api/v1/personas/import",
        files={"notfile": ("x.txt", b"hi", "text/plain")},
    )
    assert missing_file.status_code == 400


async def test_import_bad_strategy_form_value_422(client: AsyncClient) -> None:
    """策略表单值非法（非 skip/rename/overwrite）：VALIDATION_ERROR 422。"""
    card_bytes = json.dumps({"name": "任意", "first_mes": "嗨"}).encode("utf-8")
    response = await client.post(
        "/api/v1/personas/import",
        files={"file": ("persona.json", card_bytes, "application/json")},
        data={"conflict_strategy": "force"},
    )
    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"


async def test_import_json_malformed_400(client: AsyncClient) -> None:
    """JSON 面非法 JSON 体：BAD_REQUEST 400。"""
    response = await client.post(
        "/api/v1/personas/import",
        content=b"not-json{",
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 400
    assert response.json()["code"] == "BAD_REQUEST"


# ---------------------------------------------------------------------------
# POST /import/batch —— 批量（部分成功）
# ---------------------------------------------------------------------------


async def test_import_batch_partial_success(client: AsyncClient) -> None:
    """批量矩阵：新导入 + 同卡幂等 + 名称冲突跳过 + 解析失败——逐文件结果。"""
    existing = await _create_existing(client, name="feiyu")
    existing_card = json.dumps({"name": "大肥鱼", "first_mes": "你回来啦！"}).encode("utf-8")
    response = await client.post(
        "/api/v1/personas/import/batch",
        files=[
            ("files", ("new.json", json.dumps({"name": "批量人格", "first_mes": "嗨"}).encode(), "application/json")),
            ("files", ("dup.json", existing_card, "application/json")),
            ("files", ("conflict.json", json.dumps({"name": "feiyu", "personality": "冲突"}).encode(), "application/json")),
            ("files", ("broken.json", b"not-json{", "application/json")),
        ],
    )
    assert response.status_code == 200
    body: dict[str, Any] = response.json()
    assert "新导入 1" in body["message"]
    data = body["data"]
    assert data["total"] == 4
    assert data["succeeded"] == 3
    assert data["failed"] == 1
    assert data["imported_count"] == 1
    items = data["items"]
    assert [item["filename"] for item in items] == ["new.json", "dup.json", "conflict.json", "broken.json"]
    assert items[0]["imported"] is True
    assert items[1]["reason"] == "identical_content"
    assert items[1]["persona_id"] == existing["id"]
    assert items[2]["reason"] == "name_conflict"
    assert items[3]["success"] is False
    assert items[3]["code"] == "VALIDATION_ERROR"


async def test_import_batch_rename_strategy(client: AsyncClient) -> None:
    """批量 + rename 策略表单字段：同名多文件逐个后缀（-2 / -3）。"""
    response = await client.post(
        "/api/v1/personas/import/batch",
        files=[
            ("files", ("a.json", json.dumps({"name": "同名", "personality": "一"}).encode(), "application/json")),
            ("files", ("b.json", json.dumps({"name": "同名", "personality": "二"}).encode(), "application/json")),
            ("files", ("c.json", json.dumps({"name": "同名", "personality": "三"}).encode(), "application/json")),
        ],
        data={"conflict_strategy": "rename"},
    )
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["succeeded"] == 3
    assert data["imported_count"] == 3
    assert data["failed"] == 0


async def test_import_batch_missing_files_400(client: AsyncClient) -> None:
    """批量缺 files 字段：BAD_REQUEST 400。"""
    response = await client.post(
        "/api/v1/personas/import/batch",
        files={"notfiles": ("x.json", b"{}", "application/json")},
    )
    assert response.status_code == 400
    assert response.json()["code"] == "BAD_REQUEST"


async def test_import_batch_empty_body_400(client: AsyncClient) -> None:
    """批量空请求体：BAD_REQUEST 400。"""
    response = await client.post(
        "/api/v1/personas/import/batch",
        content=b"",
        headers={"content-type": "multipart/form-data; boundary=x"},
    )
    assert response.status_code == 400


# ---------------------------------------------------------------------------
# 处理器直调契约面（JSON 面——Request 构造）
# ---------------------------------------------------------------------------


def _json_request(payload: dict[str, Any]) -> Request:
    """构造携带 JSON 体的 starlette Request（处理器直调面）。"""
    raw = json.dumps(payload).encode("utf-8")
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/import",
        "query_string": b"",
        "headers": [(b"content-type", b"application/json")],
    }

    async def receive() -> dict[str, Any]:
        """一次性下发完整请求体。"""
        return {"type": "http.request", "body": raw, "more_body": False}

    return Request(scope, receive=receive)


async def test_import_handler_direct_call_contract(
    db_session: AsyncSession,
) -> None:
    """导入处理器直调：ResultDTO 包装契约（success / code / data 形态）。"""
    request = _json_request({"preset_id": "gentle-healer"})
    result = await import_persona(request, db_session)
    assert result.success is True
    assert result.code == "200"
    assert result.data is not None
    assert result.data.imported is True
    assert result.data.reason is None

"""表情包服务测试——上传去重 / 校验 / 标签 / 删除 / VLM 标注 / 通道桥。

覆盖面：默认关闭配置；上传（来源备注 / 类型 / 字节 / 标签 / 描述校验、
哈希去重、文件落盘、批量部分成功）；列表（标签 / 状态过滤、数据内联）；
修改（标签 / 描述 / 来源备注与非法值）；删除（单条 / 批量部分成功 / 文件
清理）；VLM 标注（成功写描述标签、失败落状态可重试、解析失败、条目解析
失败、文件缺失）；通道桥（入站收录去重 / 出站取最新）；人格隔离。
"""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

import packages.harness.multimodal.sticker_orm as sticker_orm  # noqa: F401  模型注册副作用导入（共享建表）
from packages.harness.config.model_entry import LlmModelEntry, ModelRole
from packages.harness.config.model_resolver import ModelResolver
from packages.harness.core.errors import BusinessException, ErrorCode
from packages.harness.llm.llm_models import ChatMessageModel, ChatResponseModel
from packages.harness.multimodal.sticker_models import (
    StickerAnnotationStatus,
    StickerBatchDeleteRequest,
    StickerBatchUploadDTO,
    StickerQueryDTO,
    StickerRejectReason,
    StickerSettings,
    StickerUpdateModel,
    StickerUploadItemModel,
)
from packages.harness.multimodal.sticker_orm import StickerRow
from packages.harness.multimodal.sticker_service import StickerService
from packages.harness.persona.service import bind_persona_context

#: 测试人格上下文轴（PersonaMixin 无外键——任意字符串 ID 即隔离轴）。
PERSONA_A = "sticker-alpha"
PERSONA_B = "sticker-beta"
PNG = b"\x89PNG\r\n\x1a\nfake-png"


class FakeGateway:
    """测试用视觉标注网关（返回固定文本 / 可注入异常）。"""

    def __init__(self, content: str = '{"description": "开心地笑", "tags": ["开心", "笑"]}') -> None:
        self.content = content
        self.calls: list[str] = []
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
        """记录条目名并返回配置文本（失败注入时抛出）。"""
        self.calls.append(entry.name)
        if self.failure is not None:
            raise self.failure
        return ChatResponseModel(content=self.content)


class _RacingStickerService(StickerService):
    """首个文件落盘后插入并发胜者行（模拟预检窗口外的唯一键竞态）。"""

    def __init__(self, race_session: AsyncSession, **kwargs: Any) -> None:
        """初始化并保存竞态会话。"""
        super().__init__(**kwargs)
        self._race_session = race_session
        self._raced = False

    async def _write_file(self, digest: str, extension: str, payload: bytes) -> Path:
        """落盘后在首个条目上补一条同哈希竞争行（保存点建立前的 flush 落库）。"""
        path = await super()._write_file(digest, extension, payload)
        if not self._raced:
            self._raced = True
            self._race_session.add(
                StickerRow(
                    sha256=digest,
                    content_type="image/png",
                    storage_path=str(path),
                    size_bytes=len(payload),
                    tags="[]",
                    description="并发胜者",
                    source_note="并发来源",
                    annotation_status=StickerAnnotationStatus.PENDING.value,
                    annotation_error=None,
                    annotation_model_entry=None,
                    annotated_at_ms=None,
                )
            )
        return path


def _settings(**overrides: object) -> StickerSettings:
    """构造已启用的测试配置（上限收窄便于覆盖）。"""
    values: dict[str, object] = {
        "enabled": True,
        "max_upload_bytes": 1024,
        "max_tags": 3,
        "tag_max_length": 6,
        "description_max_length": 20,
        "max_batch_items": 10,
    }
    values.update(overrides)
    return StickerSettings.model_validate(values)


def _resolver() -> ModelResolver:
    """构造一个多模态直连（look_at 角色）的解析器。"""
    return ModelResolver(
        [
            LlmModelEntry(
                name="vision-test",
                base_url="http://vision.test/v1",
                model="qwen3-vl",
                roles=[ModelRole.LOOK_AT],
                multimodal=True,
            )
        ]
    )


def _item(
    payload: bytes = PNG,
    *,
    source_note: str = "自绘素材",
    content_type: str = "image/png",
    tags: list[str] | None = None,
    description: str = "",
) -> StickerUploadItemModel:
    """构造一条上传条目。"""
    return StickerUploadItemModel(
        image_base64=base64.b64encode(payload).decode("ascii"),
        content_type=content_type,
        source_note=source_note,
        tags=tags or [],
        description=description,
    )


async def _upload(
    service: StickerService, session: AsyncSession, payload: bytes = PNG
):
    """铺底上传一条表情。"""
    return await service.upload_batch(
        session, StickerBatchUploadDTO(items=[_item(payload)])
    )


async def test_settings_default_disabled() -> None:
    """缺省配置：功能关闭。"""
    settings = StickerSettings()
    assert settings.enabled is False
    assert settings.storage_dir == "data/stickers"
    assert settings.channel_reply_enabled is True
    assert settings.max_upload_bytes == 5242880
    assert settings.max_tags == 8
    assert settings.max_batch_items == 20


async def test_disabled_feature_conflict(db_session: AsyncSession) -> None:
    """功能关闭 → 列表 / 上传均 409。"""
    service = StickerService(settings=StickerSettings())
    async with bind_persona_context(PERSONA_A):
        with pytest.raises(BusinessException) as list_error:
            await service.list_stickers(db_session, StickerQueryDTO())
        with pytest.raises(BusinessException) as upload_error:
            await service.upload_batch(db_session, StickerBatchUploadDTO(items=[_item()]))
    assert list_error.value.code == ErrorCode.CONFLICT
    assert upload_error.value.code == ErrorCode.CONFLICT


async def test_upload_success_persists_file_and_metadata(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    """上传成功：文件落盘 / 状态 pending / 标签归一 / 来源备注可读。"""
    service = StickerService(
        settings=_settings(), gateway=FakeGateway(), resolver=_resolver(), storage_root=tmp_path
    )
    async with bind_persona_context(PERSONA_A):
        result = await service.upload_batch(
            db_session,
            StickerBatchUploadDTO(
                items=[_item(tags=[" 开心 ", "开心", "笑"], description="  一张笑脸  ")]
            ),
        )
        record = result.created[0]
        fetched = await service.get_sticker(db_session, record.id, include_data=True)
    assert result.rejected == []
    assert record.tags == ["开心", "笑"]
    assert record.description == "一张笑脸"
    assert record.source_note == "自绘素材"
    assert record.annotation_status is StickerAnnotationStatus.PENDING
    assert fetched.data_base64 is not None
    assert base64.b64decode(fetched.data_base64) == PNG
    files = list(tmp_path.rglob("*.png"))
    assert len(files) == 1 and files[0].read_bytes() == PNG


async def test_upload_rejects_invalid_items_partially(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    """类型 / 尺寸 / base64 / 来源备注 / 标签 / 描述非法逐条拒收。"""
    service = StickerService(
        settings=_settings(max_tags=2, tag_max_length=4, description_max_length=5),
        gateway=FakeGateway(),
        resolver=_resolver(),
        storage_root=tmp_path,
    )
    large = b"x" * 1025
    async with bind_persona_context(PERSONA_A):
        result = await service.upload_batch(
            db_session,
            StickerBatchUploadDTO(
                items=[
                    _item(),
                    _item(content_type="image/bmp"),
                    _item(payload=large),
                    StickerUploadItemModel(
                        image_base64="!!bad!!",
                        content_type="image/png",
                        source_note="来源",
                    ),
                    _item(source_note="   "),
                    _item(tags=["超长标签文字"]),
                    _item(tags=["a", "b", "c"]),
                    _item(description="描述超过五个字符"),
                ]
            ),
        )
    assert len(result.created) == 1
    reasons = {item.reason for item in result.rejected}
    assert reasons == {
        StickerRejectReason.UNSUPPORTED_CONTENT_TYPE,
        StickerRejectReason.PAYLOAD_TOO_LARGE,
        StickerRejectReason.PAYLOAD_INVALID,
        StickerRejectReason.SOURCE_NOTE_INVALID,
        StickerRejectReason.TAG_INVALID,
        StickerRejectReason.DESCRIPTION_TOO_LONG,
    }
    rejected_indexes = {item.index for item in result.rejected}
    assert rejected_indexes == set(range(1, 8))


async def test_upload_deduplicates_same_hash(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    """同人格同文件重复上传 → duplicate 拒收。"""
    service = StickerService(
        settings=_settings(), gateway=FakeGateway(), resolver=_resolver(), storage_root=tmp_path
    )
    async with bind_persona_context(PERSONA_A):
        first = await service.upload_batch(
            db_session, StickerBatchUploadDTO(items=[_item(), _item(PNG)])
        )
        second = await service.upload_batch(
            db_session, StickerBatchUploadDTO(items=[_item(PNG), _item(b"another")])
        )
    assert len(first.created) == 1
    assert len(first.rejected) == 1
    assert first.rejected[0].reason is StickerRejectReason.DUPLICATE
    assert len(second.created) == 1
    assert second.rejected[0].reason is StickerRejectReason.DUPLICATE


async def test_upload_concurrent_conflict_isolated_to_duplicate_item(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    """预检后撞唯一键：仅该条目回滚并计 duplicate，同批其余文件照常入库。"""
    service = _RacingStickerService(
        db_session,
        settings=_settings(),
        gateway=FakeGateway(),
        resolver=_resolver(),
        storage_root=tmp_path,
    )
    async with bind_persona_context(PERSONA_A):
        result = await service.upload_batch(
            db_session, StickerBatchUploadDTO(items=[_item(PNG), _item(b"second")])
        )
        listed = await service.list_stickers(db_session, StickerQueryDTO())
    assert [record.description for record in result.created] == [""]
    assert len(result.rejected) == 1
    assert result.rejected[0].index == 0
    assert result.rejected[0].reason is StickerRejectReason.DUPLICATE
    assert {record.description for record in listed} == {"并发胜者", ""}
    assert len(list(tmp_path.rglob("*.png"))) == 2


async def test_list_filters_tags_and_annotation_status(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    """列表支持标签 / 标注状态过滤与条数限制。"""
    service = StickerService(
        settings=_settings(), gateway=FakeGateway(), resolver=_resolver(), storage_root=tmp_path
    )
    async with bind_persona_context(PERSONA_A):
        await service.upload_batch(
            db_session,
            StickerBatchUploadDTO(
                items=[_item(tags=["开心"]), _item(b"second", tags=["难过"])]
            ),
        )
        by_tag = await service.list_stickers(db_session, StickerQueryDTO(tag="开心"))
        pending = await service.list_stickers(
            db_session, StickerQueryDTO(annotation_status=StickerAnnotationStatus.PENDING, limit=1)
        )
        done = await service.list_stickers(
            db_session, StickerQueryDTO(annotation_status=StickerAnnotationStatus.DONE)
        )
    assert len(by_tag) == 1 and by_tag[0].tags == ["开心"]
    assert len(pending) == 1
    assert done == []


async def test_update_sticker_fields_and_validation(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    """修改标签 / 描述 / 来源备注；非法值 422；记录缺失 404。"""
    service = StickerService(
        settings=_settings(), gateway=FakeGateway(), resolver=_resolver(), storage_root=tmp_path
    )
    async with bind_persona_context(PERSONA_A):
        record = (await _upload(service, db_session)).created[0]
        updated = await service.update_sticker(
            db_session,
            record.id,
            StickerUpdateModel(tags=["新标签"], description="新描述", source_note="朋友授权"),
        )
        with pytest.raises(BusinessException) as tag_error:
            await service.update_sticker(
                db_session, record.id, StickerUpdateModel(tags=["这是一个超长标签"])
            )
        with pytest.raises(BusinessException) as note_error:
            await service.update_sticker(
                db_session, record.id, StickerUpdateModel(source_note="   ")
            )
        with pytest.raises(BusinessException) as missing:
            await service.get_sticker(db_session, 9999)
    assert updated.tags == ["新标签"]
    assert updated.description == "新描述"
    assert updated.source_note == "朋友授权"
    assert tag_error.value.code == ErrorCode.VALIDATION_ERROR
    assert note_error.value.code == ErrorCode.VALIDATION_ERROR
    assert missing.value.code == ErrorCode.NOT_FOUND


async def test_delete_batch_partial_and_removes_files(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    """批量删除：存在项删行删文件、缺失项进入 missing；单条删除幂等。"""
    service = StickerService(
        settings=_settings(), gateway=FakeGateway(), resolver=_resolver(), storage_root=tmp_path
    )
    async with bind_persona_context(PERSONA_A):
        created = await service.upload_batch(
            db_session, StickerBatchUploadDTO(items=[_item(), _item(b"second")])
        )
        ids = [record.id for record in created.created]
        result = await service.delete_batch(
            db_session, StickerBatchDeleteRequest(ids=[ids[0], 9999])
        )
        single = await service.delete_sticker(db_session, ids[1])
        again = await service.delete_sticker(db_session, ids[1])
    assert result.deleted_ids == [ids[0]]
    assert result.missing_ids == [9999]
    assert single.deleted_ids == [ids[1]]
    assert again.missing_ids == [ids[1]]
    assert list(tmp_path.rglob("*.png")) == []


async def test_get_binary_missing_file_not_found(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    """记录存在但文件被外部删除 → get_binary 404。"""
    service = StickerService(
        settings=_settings(), gateway=FakeGateway(), resolver=_resolver(), storage_root=tmp_path
    )
    async with bind_persona_context(PERSONA_A):
        record = (await _upload(service, db_session)).created[0]
        for path in tmp_path.rglob("*.png"):
            path.unlink()
        with pytest.raises(BusinessException) as exc_info:
            await service.get_binary(db_session, record.id)
    assert exc_info.value.code == ErrorCode.NOT_FOUND


async def test_annotate_success_writes_description_and_tags(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    """标注成功：描述 / 标签 / 状态 / 模型条目 / 时间写入。"""
    gateway = FakeGateway('```json\n{"description": "笑着挥手", "tags": ["开心", "挥手"]}\n```')
    service = StickerService(
        settings=_settings(), gateway=gateway, resolver=_resolver(), storage_root=tmp_path
    )
    async with bind_persona_context(PERSONA_A):
        record = (await _upload(service, db_session)).created[0]
        result = await service.annotate(db_session, record.id)
    assert result.ok is True
    assert result.record.annotation_status is StickerAnnotationStatus.DONE
    assert result.record.description == "笑着挥手"
    assert set(result.record.tags) == {"开心", "挥手"}
    assert result.record.annotation_model_entry == "vision-test"
    assert result.record.annotated_at_ms is not None
    assert result.record.annotation_error is None
    assert gateway.calls == ["vision-test"]


async def test_annotate_failure_records_status_and_retry_succeeds(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    """标注失败落 failed 状态；重试成功后恢复 done。"""
    gateway = FakeGateway()
    service = StickerService(
        settings=_settings(), gateway=gateway, resolver=_resolver(), storage_root=tmp_path
    )
    async with bind_persona_context(PERSONA_A):
        record = (await _upload(service, db_session)).created[0]
        gateway.failure = BusinessException(code=ErrorCode.LLM_CALL_FAILED, message="端点不可用")
        failed = await service.annotate(db_session, record.id)
        gateway.failure = None
        retried = await service.annotate(db_session, record.id)
    assert failed.ok is False
    assert failed.record.annotation_status is StickerAnnotationStatus.FAILED
    assert failed.record.annotation_error is not None
    assert "端点不可用" in (failed.record.annotation_error or "")
    assert retried.ok is True
    assert retried.record.annotation_status is StickerAnnotationStatus.DONE


async def test_annotate_invalid_json_and_resolver_failure(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    """标注响应非 JSON / 视觉条目未配置 → failed 状态（不抛异常）。"""
    gateway = FakeGateway("这不是 JSON")
    service = StickerService(
        settings=_settings(), gateway=gateway, resolver=_resolver(), storage_root=tmp_path
    )
    async with bind_persona_context(PERSONA_A):
        record = (await _upload(service, db_session)).created[0]
        invalid = await service.annotate(db_session, record.id)
    assert invalid.ok is False
    assert invalid.record.annotation_status is StickerAnnotationStatus.FAILED
    unconfigured = StickerService(
        settings=_settings(), gateway=gateway, resolver=ModelResolver([]), storage_root=tmp_path
    )
    async with bind_persona_context(PERSONA_A):
        missing = await unconfigured.annotate(db_session, record.id)
    assert missing.ok is False
    assert "视觉条目解析失败" in missing.message


async def test_annotate_missing_file_failed(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    """落盘文件缺失时标注写 failed 状态。"""
    service = StickerService(
        settings=_settings(), gateway=FakeGateway(), resolver=_resolver(), storage_root=tmp_path
    )
    async with bind_persona_context(PERSONA_A):
        record = (await _upload(service, db_session)).created[0]
        for path in tmp_path.rglob("*.png"):
            path.unlink()
        result = await service.annotate(db_session, record.id)
    assert result.ok is False
    assert "文件缺失" in result.message


async def test_channel_bridge_ingest_and_pick(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    """通道桥：入站收录去重 / 空类型拒收 / 出站取最新 / 关闭回表情不发。"""
    service = StickerService(
        settings=_settings(), gateway=FakeGateway(), resolver=_resolver(), storage_root=tmp_path
    )
    async with bind_persona_context(PERSONA_A):
        first = await service.ingest_bytes(
            db_session, PNG, "image/png", source_note="通道入站表情"
        )
        duplicate = await service.ingest_bytes(
            db_session, PNG, "image/png", source_note="通道入站表情"
        )
        rejected = await service.ingest_bytes(
            db_session, PNG, "image/bmp", source_note="通道入站表情"
        )
        newer = await service.ingest_bytes(
            db_session, b"newer", "image/gif", source_note="通道入站表情"
        )
        picked = await service.pick_channel_reply(db_session)
    assert first is not None and first.id == duplicate.id
    assert rejected is None
    assert newer is not None
    assert picked is not None and picked.record.id == newer.id
    assert picked.content == b"newer"
    no_reply = StickerService(
        settings=_settings(channel_reply_enabled=False),
        gateway=FakeGateway(),
        resolver=_resolver(),
        storage_root=tmp_path,
    )
    async with bind_persona_context(PERSONA_A):
        assert await no_reply.pick_channel_reply(db_session) is None


async def test_insert_sticker_conflict_resolves_winner_and_keeps_file(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    """并发同哈希收敛：共用插入函数撞唯一键时返回胜者行、不删胜者文件。"""
    service = StickerService(
        settings=_settings(), gateway=FakeGateway(), resolver=_resolver(), storage_root=tmp_path
    )
    async with bind_persona_context(PERSONA_A):
        winner = await service.ingest_bytes(
            db_session, PNG, "image/png", source_note="并发胜者"
        )
        assert winner is not None
        outcome = await service._insert_sticker(
            db_session,
            digest=hashlib.sha256(PNG).hexdigest(),
            content_type="image/png",
            payload=PNG,
            tags=[],
            description="落败写入",
            source_note="并发落败",
        )
        await db_session.rollback()
        rows = list((await db_session.execute(select(StickerRow))).scalars().all())
        binary = await service.get_binary(db_session, winner.id)
    assert outcome.created is False
    assert outcome.row is not None and outcome.row.id == winner.id
    assert outcome.path.exists()
    assert len(rows) == 1
    assert binary.content == PNG


async def test_ingest_same_hash_twice_single_row_and_file(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    """顺序同哈希收录：行唯一、文件在、get_binary 可用、dupe 返回既有记录。"""
    service = StickerService(
        settings=_settings(), gateway=FakeGateway(), resolver=_resolver(), storage_root=tmp_path
    )
    async with bind_persona_context(PERSONA_A):
        first = await service.ingest_bytes(
            db_session, PNG, "image/png", source_note="通道入站表情"
        )
        second = await service.ingest_bytes(
            db_session, PNG, "image/png", source_note="通道入站表情"
        )
        assert first is not None and second is not None
        row_count = (
            await db_session.execute(select(func.count()).select_from(StickerRow))
        ).scalar_one()
        binary = await service.get_binary(db_session, first.id)
    assert second.id == first.id
    assert int(row_count) == 1
    assert binary.content == PNG


async def test_channel_bridge_skips_when_disabled(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    """功能关闭时入站收录返回 None（通道桥零写入）。"""
    service = StickerService(
        settings=StickerSettings(enabled=False), gateway=FakeGateway(), storage_root=tmp_path
    )
    async with bind_persona_context(PERSONA_A):
        assert (
            await service.ingest_bytes(db_session, PNG, "image/png", source_note="x")
            is None
        )


async def test_pick_channel_reply_skips_missing_files(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    """最新素材文件缺失时回溯到更早的可读素材。"""
    service = StickerService(
        settings=_settings(), gateway=FakeGateway(), resolver=_resolver(), storage_root=tmp_path
    )
    async with bind_persona_context(PERSONA_A):
        older = (await _upload(service, db_session)).created[0]
        newer = (await _upload(service, db_session, b"newer")).created[0]
        for path in tmp_path.rglob("*.png"):
            if path.read_bytes() == b"newer":
                path.unlink()
        picked = await service.pick_channel_reply(db_session)
    assert picked is not None
    assert picked.record.id == older.id
    assert newer.id != older.id


async def test_persona_isolation_allows_same_hash(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    """同一文件在两个人格下各自入库（去重键含人格轴）。"""
    service = StickerService(
        settings=_settings(), gateway=FakeGateway(), resolver=_resolver(), storage_root=tmp_path
    )
    async with bind_persona_context(PERSONA_A):
        a = (await _upload(service, db_session)).created[0]
    async with bind_persona_context(PERSONA_B):
        b = (await _upload(service, db_session)).created[0]
        listed_b = await service.list_stickers(db_session, StickerQueryDTO())
    assert a.id != b.id
    assert a.sha256 == b.sha256
    assert [record.id for record in listed_b] == [b.id]


async def test_upload_dimension_guards_batch_and_annotation_payload() -> None:
    """标注 JSON 提取的边界：前后解释文本容忍 / 非对象拒绝。"""
    from packages.harness.multimodal.sticker_service import _extract_annotation_payload

    payload = _extract_annotation_payload('前缀 {"description": "x", "tags": []} 后缀')
    assert payload == {"description": "x", "tags": []}
    with pytest.raises(ValueError):
        _extract_annotation_payload("没有对象")
    with pytest.raises(ValueError):
        _extract_annotation_payload("[1, 2]")
    assert json.loads(json.dumps(payload)) == payload

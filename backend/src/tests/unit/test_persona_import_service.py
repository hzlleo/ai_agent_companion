"""harness/persona import 服务测试——内容哈希幂等 + 名称冲突策略矩阵 +
批量部分成功。

引擎经共享会话级 SQLite fixture（StaticPool 单连接）；Session 由
``db_session`` fixture 注入（服务层事务边界）；引擎 holder 槽位外部
安装（与 test_persona_service.py 同模式）。

覆盖面（锁定语义矩阵）：

    - 判定序：内容同卡幂等（三策略恒跳过）优先于名称冲突；
    - 名称冲突三策略：skip 未导入 / rename 自动后缀（名字-2 起递增）/
      overwrite 卡片替换且保留 M/S 状态 + status + 主人格标记 + 语音；
    - 期望名：提供且空闲直用；空白拒绝；
    - 导入产物恒 disabled + 非主人格；预设建议语音落库（覆盖路径保留
      既有绑定）；
    - 批量：合法跳过是成功项、解析失败是失败项、计数面
      （succeeded / failed / imported_count）与顺序保持。
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from packages.harness.core.errors import BusinessException, ErrorCode
from packages.harness.database.engine import db_engine_holder
from packages.harness.persona.chara_card import CharaCardModel
from packages.harness.persona.dtos import (
    PersonaImportBatchFileDTO,
    PersonaImportRequestDTO,
)
from packages.harness.persona.models import (
    PersonaImportConflictStrategy,
    PersonaImportReason,
    PersonaStateLayer,
    PersonaStatus,
)
from packages.harness.persona.orm import PersonaRow, PersonaStateRow
from packages.harness.persona.service import PersonaService, canonical_card_hash
from packages.harness.voice.voice_models import VoiceProfileModel


@pytest.fixture
async def import_service(shared_db_engine: AsyncEngine) -> AsyncIterator[PersonaService]:
    """人格服务（engine holder 外部安装到共享引擎；用后复位槽位）。

    Args:
        shared_db_engine: 会话级共享 SQLite 引擎（conftest 提供）。

    Yields:
        :class:`PersonaService` 实例。
    """
    db_engine_holder.async_engine = shared_db_engine
    db_engine_holder.session_factory = async_sessionmaker(
        bind=shared_db_engine, expire_on_commit=False
    )
    db_engine_holder.owner_token = None
    yield PersonaService()
    db_engine_holder.reset()


def _card(name: str = "小助手", personality: str = "温柔耐心") -> CharaCardModel:
    """构造导入卡片（最小合法 + 可定制）。"""
    return CharaCardModel(name=name, personality=personality, first_mes="你好呀")


async def _create_existing(
    service: PersonaService, session: AsyncSession, name: str = "feiyu"
) -> int:
    """铺底：经既有创建面落一个人格（返回主键）。"""
    from packages.harness.persona.dtos import PersonaCreateDTO

    record = await service.create(
        session,
        PersonaCreateDTO(
            name=name,
            card=CharaCardModel(name="大肥鱼", personality="温柔爱操心", first_mes="你回来啦"),
        ),
    )
    return record.id


async def _row(session: AsyncSession, persona_id: int) -> PersonaRow:
    """按主键取行（测试断言辅助——存在性由铺底保证）。"""
    row = await session.get(PersonaRow, persona_id)
    assert row is not None
    return row


# ---------------------------------------------------------------------------
# 判定序：内容同卡幂等（与策略无关）
# ---------------------------------------------------------------------------


async def test_import_creates_new_when_free(
    import_service: PersonaService, db_session: AsyncSession
) -> None:
    """空闲名直入：新行 disabled + 非主人格 + 卡片名回落展示名。"""
    result = await import_service.import_persona(db_session, _card("新人格"))
    assert result.imported is True
    assert result.persona_id is not None
    assert result.reason is None
    assert result.warnings == []
    row = await _row(db_session, result.persona_id)
    assert row.name == "新人格"
    assert row.display_name == "新人格"
    assert row.status == PersonaStatus.DISABLED.value
    assert row.is_primary_marker is None


async def test_identical_content_idempotent_regardless_of_strategy(
    import_service: PersonaService, db_session: AsyncSession
) -> None:
    """内容同卡幂等：三策略（skip / rename / overwrite）恒跳过——同卡重导
    不产生第二行、不覆盖既有行。"""
    existing_id = await _create_existing(import_service, db_session)
    existing_row = await _row(db_session, existing_id)
    same_card = CharaCardModel.model_validate(existing_row.chara_card)
    for strategy in PersonaImportConflictStrategy:
        result = await import_service.import_persona(
            db_session, same_card, strategy=strategy
        )
        assert result.imported is False
        assert result.persona_id == existing_id
        assert result.reason == PersonaImportReason.IDENTICAL_CONTENT
    rows = list((await db_session.execute(select(PersonaRow))).scalars().all())
    assert len(rows) == 1


async def test_identical_content_hash_stable_across_field_order(
    import_service: PersonaService, db_session: AsyncSession
) -> None:
    """哈希稳定性：字段序不同的等价 JSON 文档恒同哈希（canonical sort_keys）。"""
    first = _card("同卡", personality="片段一")
    second = CharaCardModel.model_validate(
        {"personality": "片段一", "name": "同卡", "first_mes": "你好呀"}
    )
    assert canonical_card_hash(first) == canonical_card_hash(second)
    await import_service.import_persona(db_session, first)
    result = await import_service.import_persona(db_session, second)
    assert result.imported is False
    assert result.reason == PersonaImportReason.IDENTICAL_CONTENT


# ---------------------------------------------------------------------------
# 名称冲突三策略
# ---------------------------------------------------------------------------


async def test_name_conflict_skip_default(
    import_service: PersonaService, db_session: AsyncSession
) -> None:
    """名称冲突 + skip（缺省）：未导入 + reason=name_conflict + 占用行主键。"""
    existing_id = await _create_existing(import_service, db_session, name="小助手")
    # 卡片名占用「小助手」（与既有注册名同名——内容不同）。
    result = await import_service.import_persona(db_session, _card("小助手", personality="全新性格"))
    assert result.imported is False
    assert result.persona_id == existing_id
    assert result.reason == PersonaImportReason.NAME_CONFLICT
    rows = list((await db_session.execute(select(PersonaRow))).scalars().all())
    assert len(rows) == 1


async def test_name_conflict_rename_auto_suffix(
    import_service: PersonaService, db_session: AsyncSession
) -> None:
    """rename 策略：期望名被占用 → 自动后缀（名字-2 起递增——首个空闲）。"""
    await _create_existing(import_service, db_session, name="小助手")
    first = await import_service.import_persona(
        db_session,
        _card("小助手", personality="性格一"),
        strategy=PersonaImportConflictStrategy.RENAME,
    )
    second = await import_service.import_persona(
        db_session,
        _card("小助手", personality="性格二"),
        strategy=PersonaImportConflictStrategy.RENAME,
    )
    assert first.imported is True
    assert first.persona_id is not None
    assert (await _row(db_session, first.persona_id)).name == "小助手-2"
    assert second.imported is True
    assert second.persona_id is not None
    assert (await _row(db_session, second.persona_id)).name == "小助手-3"
    rows = list((await db_session.execute(select(PersonaRow))).scalars().all())
    assert {row.name for row in rows} == {"小助手", "小助手-2", "小助手-3"}


async def test_name_conflict_rename_with_free_desired_name(
    import_service: PersonaService, db_session: AsyncSession
) -> None:
    """期望名空闲时直用（rename 策略无需触发——判定序 2 自然短路）。"""
    await _create_existing(import_service, db_session, name="小助手")
    result = await import_service.import_persona(
        db_session,
        _card("小助手", personality="全新性格"),
        strategy=PersonaImportConflictStrategy.RENAME,
        desired_name="我的新人格",
    )
    assert result.imported is True
    assert result.persona_id is not None
    row = await _row(db_session, result.persona_id)
    assert row.name == "我的新人格"


async def test_name_conflict_overwrite_replaces_card_keeps_states(
    import_service: PersonaService, db_session: AsyncSession
) -> None:
    """overwrite 策略：卡片整体替换 + M/S 状态 / status / 主人格标记 /
    语音绑定全保留。"""
    from packages.harness.persona.dtos import PersonaStateUpsertDTO, PersonaUpdateDTO

    existing_id = await _create_existing(import_service, db_session, name="小助手")
    await import_service.update(
        db_session,
        existing_id,
        PersonaUpdateDTO(status=PersonaStatus.ACTIVE),
    )
    await import_service.set_primary(db_session, existing_id)
    await import_service.upsert_state(
        db_session,
        existing_id,
        PersonaStateLayer.M,
        PersonaStateUpsertDTO(state={"mood": "calm"}),
    )
    new_card = _card("小助手", personality="覆盖后的全新性格")
    result = await import_service.import_persona(
        db_session, new_card, strategy=PersonaImportConflictStrategy.OVERWRITE
    )
    assert result.imported is True
    assert result.persona_id == existing_id
    row = await _row(db_session, existing_id)
    assert CharaCardModel.model_validate(row.chara_card).personality == "覆盖后的全新性格"
    assert row.status == PersonaStatus.ACTIVE.value
    assert row.is_primary_marker is not None
    states = list(
        (
            await db_session.execute(
                select(PersonaStateRow).where(PersonaStateRow.persona_id == existing_id)
            )
        )
        .scalars()
        .all()
    )
    assert len(states) == 1
    assert states[0].state == {"mood": "calm"}


async def test_import_with_voice_profile_lands_on_create_only(
    import_service: PersonaService, db_session: AsyncSession
) -> None:
    """预设建议语音：创建路径落库；覆盖路径保留既有绑定。"""
    voice = VoiceProfileModel(speaker="xiaoxiao", instruct_text=None, speed=1.0)
    first = await import_service.import_persona(
        db_session, _card("语音人格"), voice_profile=voice
    )
    assert first.persona_id is not None
    row = await _row(db_session, first.persona_id)
    assert row.voice_profile is not None
    overwrite = await import_service.import_persona(
        db_session,
        _card("语音人格", personality="新性格"),
        strategy=PersonaImportConflictStrategy.OVERWRITE,
        voice_profile=VoiceProfileModel(speaker="other", instruct_text=None, speed=1.0),
    )
    assert overwrite.persona_id == first.persona_id
    await db_session.refresh(row)
    assert row.voice_profile == voice.model_dump()


async def test_import_blank_desired_name_rejected(
    import_service: PersonaService, db_session: AsyncSession
) -> None:
    """期望名空白（空白串）：VALIDATION_ERROR 拒绝。"""
    with pytest.raises(BusinessException) as exc_info:
        await import_service.import_persona(db_session, _card("任意"), desired_name="   ")
    assert exc_info.value.code == ErrorCode.VALIDATION_ERROR


async def test_import_warnings_passthrough(
    import_service: PersonaService, db_session: AsyncSession
) -> None:
    """生成软警告透传：并入结果 warnings（预设导入的组件冲突提示等）。"""
    result = await import_service.import_persona(
        db_session, _card("警告人格"), warnings=["组件「甲」与「乙」存在冲突声明"]
    )
    assert result.imported is True
    assert result.warnings == ["组件「甲」与「乙」存在冲突声明"]


# ---------------------------------------------------------------------------
# 批量导入（部分成功形态）
# ---------------------------------------------------------------------------


async def test_batch_import_partial_success(
    import_service: PersonaService, db_session: AsyncSession
) -> None:
    """批量矩阵：新导入 + 同卡幂等 + 名称冲突跳过（合法结果）+ 解析失败
    （失败项）——计数面与顺序保持。"""
    existing_id = await _create_existing(import_service, db_session, name="feiyu")
    existing_row = await _row(db_session, existing_id)
    same_card = CharaCardModel.model_validate(existing_row.chara_card)
    files = [
        PersonaImportBatchFileDTO(filename="new.json", card=_card("批量新人格")),
        PersonaImportBatchFileDTO(filename="dup.json", card=same_card),
        PersonaImportBatchFileDTO(filename="conflict.json", card=_card("feiyu", personality="冲突卡")),
        PersonaImportBatchFileDTO(
            filename="broken.json",
            card=None,
            error_message="卡片文件解析失败（须为 chara card v2 JSON）: invalid",
        ),
    ]
    result = await import_service.import_personas_batch(db_session, files)
    assert result.total == 4
    assert result.succeeded == 3
    assert result.failed == 1
    assert result.imported_count == 1
    assert [item.filename for item in result.items] == [
        "new.json",
        "dup.json",
        "conflict.json",
        "broken.json",
    ]
    assert result.items[0].success is True
    assert result.items[0].imported is True
    assert result.items[0].reason is None
    assert result.items[1].success is True
    assert result.items[1].imported is False
    assert result.items[1].reason == PersonaImportReason.IDENTICAL_CONTENT
    assert result.items[2].success is True
    assert result.items[2].imported is False
    assert result.items[2].reason == PersonaImportReason.NAME_CONFLICT
    assert result.items[3].success is False
    assert result.items[3].imported is False
    assert result.items[3].code == ErrorCode.VALIDATION_ERROR


async def test_batch_import_rename_strategy(
    import_service: PersonaService, db_session: AsyncSession
) -> None:
    """批量 rename 策略：同名文件逐个自动后缀（-2 / -3 顺序递增）。"""
    files = [
        PersonaImportBatchFileDTO(filename="a.json", card=_card("同名", personality="一")),
        PersonaImportBatchFileDTO(filename="b.json", card=_card("同名", personality="二")),
        PersonaImportBatchFileDTO(filename="c.json", card=_card("同名", personality="三")),
    ]
    result = await import_service.import_personas_batch(
        db_session, files, strategy=PersonaImportConflictStrategy.RENAME
    )
    assert result.total == 3
    assert result.succeeded == 3
    assert result.failed == 0
    assert result.imported_count == 3
    rows = list((await db_session.execute(select(PersonaRow))).scalars().all())
    assert {row.name for row in rows} == {"同名", "同名-2", "同名-3"}


async def test_batch_import_empty_files(
    import_service: PersonaService, db_session: AsyncSession
) -> None:
    """空文件列表：零项结果（不失败——空批为合法形态）。"""
    result = await import_service.import_personas_batch(db_session, [])
    assert result.total == 0
    assert result.succeeded == 0
    assert result.failed == 0
    assert result.imported_count == 0
    assert result.items == []


# ---------------------------------------------------------------------------
# 请求 DTO 载荷恰一约束（服务面共用模型）
# ---------------------------------------------------------------------------


async def test_import_request_payload_exclusivity() -> None:
    """载荷恰一：preset_id 与 card 同时提供 / 均未提供 → ValidationError。"""
    with pytest.raises(Exception, match="恰提供一个"):
        PersonaImportRequestDTO(preset_id="tsundere", card=_card("任意"))
    with pytest.raises(Exception, match="空导入"):
        PersonaImportRequestDTO()
    ok_preset = PersonaImportRequestDTO(preset_id="tsundere")
    assert ok_preset.conflict_strategy == PersonaImportConflictStrategy.SKIP
    ok_card = PersonaImportRequestDTO(card=_card("任意"))
    assert ok_card.desired_name is None

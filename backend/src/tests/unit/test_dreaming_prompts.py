"""harness/memory dreaming_prompts Unit 文件测试——byte-lock + 渲染契约。

byte-lock 语义（与 test_chat_prompts.py 同约定）：逐模板锁定 sha256
摘要——prompt 文本变更（有意行为变更）必须同步上调版本常量并更新
此处摘要；意外漂移直接红。版本常量存在性 + 形态一并锁定。
"""

from __future__ import annotations

import hashlib

import pytest

from packages.harness.core.errors import BusinessException
from packages.harness.memory import dreaming_prompts
from packages.harness.memory.dreaming_prompts import render_diary_prompt, render_merge_prompt
from packages.harness.memory.dtos import CuratedRecordDTO, EpisodeRecordDTO
from packages.harness.memory.models import (
    ChatTurnPayloadModel,
    CuratedKind,
    CuratedStatus,
    EpisodeKind,
    MemoryProvenance,
)

#: 逐模板 sha256 摘要锁（UTF-8 字节）——变更 prompt 须同步本表与版本常量。
_BYTE_LOCKS: dict[str, str] = {
    "MERGE_TEMPLATE": "59d92e5ca3caf8b19ec16eb8c0edc7d01f616b797cb454cdb48c7c571789e7ba",
    "DIARY_TEMPLATE": "90015c7f108d91feeda4012bc2c5cbed1a47d8fba7a7a1707fa8f4579e66ea8a",
}

#: 版本常量清单（存在性 + 非空字符串形态）。
_VERSION_CONSTANTS = (
    "DREAMING_PROMPTS_VERSION",
    "MERGE_TEMPLATE_VERSION",
    "DIARY_TEMPLATE_VERSION",
)


def _episode(
    episode_id: int,
    *,
    ts_ms: int = 1_000,
    importance: int = 6,
    provenance: MemoryProvenance = MemoryProvenance.OWNER,
) -> EpisodeRecordDTO:
    """构造最小情景事件记录（prompt 渲染测试铺底）。"""
    return EpisodeRecordDTO(
        id=episode_id,
        persona_id="dream-prompt",
        kind=EpisodeKind.CHAT,
        source="chat",
        content=ChatTurnPayloadModel(
            user_content=f"第{episode_id}条用户消息", assistant_content=f"第{episode_id}条回复"
        ),
        importance=importance,
        provenance=provenance,
        content_hash=f"hash-{episode_id:04d}",
        ts_ms=ts_ms,
        created_at=0,
        updated_at=0,
    )


def _topic(topic: str, *, content: str = "主人喜欢小鱼干") -> CuratedRecordDTO:
    """构造最小 active 策展记录（prompt 渲染测试铺底）。"""
    return CuratedRecordDTO(
        id=1,
        persona_id="dream-prompt",
        kind=CuratedKind.MEMORY,
        topic=topic,
        content=content,
        importance=5,
        provenance=MemoryProvenance.OWNER,
        status=CuratedStatus.ACTIVE,
        superseded_by_id=None,
        version=1,
        created_at=0,
        updated_at=0,
    )


# ---------------------------------------------------------------------------
# byte-lock + 版本常量
# ---------------------------------------------------------------------------


async def test_dream_prompt_templates_byte_lock() -> None:
    """byte-lock：逐模板 sha256 摘要与锁定表一致（漂移即红）。"""
    for name, expected in _BYTE_LOCKS.items():
        value = getattr(dreaming_prompts, name)
        digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
        assert digest == expected, (
            f"prompt 模板 {name} 已变更（sha256 不匹配）。如为有意变更："
            f"上调对应版本常量并同步更新 {__name__} 中的 _BYTE_LOCKS。"
        )


async def test_dream_prompt_version_constants_semver_like() -> None:
    """版本常量存在且为 X.Y.Z 形态（非空 + 三段数字）。"""
    for name in _VERSION_CONSTANTS:
        value = getattr(dreaming_prompts, name)
        assert isinstance(value, str) and value
        parts = value.split(".")
        assert len(parts) == 3 and all(part.isdigit() for part in parts), (
            f"版本常量 {name}={value!r} 应为 X.Y.Z 形态"
        )


# ---------------------------------------------------------------------------
# 合并 prompt 渲染契约
# ---------------------------------------------------------------------------


async def test_render_merge_prompt_numbered_episodes_and_topics() -> None:
    """合并 prompt：事件编号（= 主键）+ kind/source/importance/ts + 既有主题块。"""
    text = render_merge_prompt(
        [_episode(11, ts_ms=1_760_000_000_000, importance=7)],
        [_topic("口味偏好")],
    )
    assert "事件 #11" in text
    assert "kind=chat" in text and "source=chat" in text
    assert "importance=7" in text and "ts=1760000000000" in text
    assert "第11条用户消息" in text
    assert "- 口味偏好（kind=memory importance=5）：主人喜欢小鱼干" in text
    assert "merged_from" in text


async def test_render_merge_prompt_persona_neutral() -> None:
    """合并 prompt 人格中立：模板与渲染产物均不携带任何人格名字段。"""
    text = render_merge_prompt([_episode(1)], [_topic("主题")])
    assert "大肥鱼" not in text
    assert "persona_name" not in dreaming_prompts.MERGE_TEMPLATE
    assert "persona" not in dreaming_prompts.MERGE_TEMPLATE


async def test_render_merge_prompt_empty_blocks_placeholder() -> None:
    """空事件 / 空主题：占位文本（不产出空块）。"""
    text = render_merge_prompt([], [])
    assert "（无）" in text


async def test_render_merge_prompt_template_override_wins() -> None:
    """覆写语义：template_override 整体替换正典模板（自带同名占位符）。"""
    override = "自定义合并模板\n事件块：{episodes_block}\n主题块：{topics_block}"
    text = render_merge_prompt(
        [_episode(21)],
        [_topic("主题")],
        template_override=override,
    )
    assert text.startswith("自定义合并模板")
    assert "事件 #21" in text
    assert "主题" in text
    assert "【记忆整理任务】" not in text


# ---------------------------------------------------------------------------
# 日记 prompt 渲染契约
# ---------------------------------------------------------------------------


async def test_render_diary_prompt_persona_name_substitution() -> None:
    """日记 prompt：人格声线仅经 persona_name 渲染变量进入。"""
    text = render_diary_prompt("大肥鱼", [_episode(31, ts_ms=2_000)])
    assert "你是 大肥鱼" in text
    assert "以 大肥鱼 的第一人称" in text
    assert "第31条用户消息" in text
    assert "kind=chat" in text


async def test_render_diary_prompt_empty_name_rejected() -> None:
    """空人格名拒绝：无声线指向的日记指令属装配错误（VALIDATION_ERROR）。"""
    with pytest.raises(BusinessException) as exc_info:
        render_diary_prompt("  ", [_episode(1)])
    assert "人格名不可为空" in exc_info.value.message


async def test_render_diary_prompt_template_override_wins() -> None:
    """覆写语义：template_override 整体替换正典模板（自带同名占位符）。"""
    override = "给 {persona_name} 的日记\n{episodes_block}"
    text = render_diary_prompt("大肥鱼", [_episode(41)], template_override=override)
    assert text.startswith("给 大肥鱼 的日记")
    assert "第41条用户消息" in text
    assert "【日记写作任务】" not in text

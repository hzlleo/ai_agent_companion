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
from packages.harness.memory.dreaming_models import (
    DREAM_DIARY_MAX_CHARS,
    DREAM_DIARY_MAX_EPISODES,
    DREAM_EVENT_TEXT_MAX_CHARS,
    DREAM_MERGE_TOPIC_LIMIT,
)
from packages.harness.memory.dreaming_prompts import (
    render_diary_prompt,
    render_facts_prompt,
    render_merge_prompt,
    render_skills_prompt,
    select_diary_episodes,
    select_prompt_topics,
)
from packages.harness.memory.dtos import CuratedRecordDTO, EpisodeRecordDTO
from packages.harness.memory.models import (
    ChatTurnPayloadModel,
    CuratedKind,
    CuratedStatus,
    EpisodeKind,
    MemoryProvenance,
)
from packages.harness.persona.skills_models import SkillRecordDTO, SkillSource, SkillStatus

#: 逐模板 sha256 摘要锁（UTF-8 字节）——变更 prompt 须同步本表与版本常量。
_BYTE_LOCKS: dict[str, str] = {
    "MERGE_TEMPLATE": "59d92e5ca3caf8b19ec16eb8c0edc7d01f616b797cb454cdb48c7c571789e7ba",
    "FACTS_TEMPLATE": "0990d05fc02a361863cf19ff8c74de857f6b7a43db9b1d9d2a9fd93116de4b03",
    "SKILLS_TEMPLATE": "23455ccfe2ef38e4b23d317154d7014189c1cc4362281a2d72524eba482447ce",
    "DIARY_TEMPLATE": "90015c7f108d91feeda4012bc2c5cbed1a47d8fba7a7a1707fa8f4579e66ea8a",
}

#: 版本常量清单（存在性 + 非空字符串形态）。
_VERSION_CONSTANTS = (
    "DREAMING_PROMPTS_VERSION",
    "MERGE_TEMPLATE_VERSION",
    "FACTS_TEMPLATE_VERSION",
    "SKILLS_TEMPLATE_VERSION",
    "DIARY_TEMPLATE_VERSION",
)


def _episode(
    episode_id: int,
    *,
    ts_ms: int = 1_000,
    importance: int = 6,
    provenance: MemoryProvenance = MemoryProvenance.OWNER,
    user_content: str | None = None,
    assistant_content: str | None = None,
) -> EpisodeRecordDTO:
    """构造最小情景事件记录（prompt 渲染测试铺底）。"""
    return EpisodeRecordDTO(
        id=episode_id,
        persona_id="dream-prompt",
        kind=EpisodeKind.CHAT,
        source="chat",
        content=ChatTurnPayloadModel(
            user_content=(
                user_content if user_content is not None else f"第{episode_id}条用户消息"
            ),
            assistant_content=(
                assistant_content
                if assistant_content is not None
                else f"第{episode_id}条回复"
            ),
        ),
        importance=importance,
        provenance=provenance,
        content_hash=f"hash-{episode_id:04d}",
        ts_ms=ts_ms,
        created_at=0,
        updated_at=0,
    )


def _skill(
    name: str = "周报流程",
    *,
    slug: str = "周报流程",
    content: str = "1. 先列三件事\n2. 再写进展",
) -> SkillRecordDTO:
    """构造最小启用技能记录（prompt 渲染测试铺底）。"""
    return SkillRecordDTO(
        id=1,
        name=name,
        slug=slug,
        description="主人说「写周报」时使用",
        triggers=["写周报"],
        content=content,
        status=SkillStatus.ACTIVE,
        source=SkillSource.MANUAL,
        confidence=0.8,
        use_count=0,
        last_used_ms=None,
        version=1,
        created_at=0,
        updated_at=0,
    )


def _topic(
    topic: str,
    *,
    content: str = "主人喜欢小鱼干",
    topic_id: int = 1,
    updated_at: int = 0,
) -> CuratedRecordDTO:
    """构造最小 active 策展记录（prompt 渲染测试铺底）。"""
    return CuratedRecordDTO(
        id=topic_id,
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
        updated_at=updated_at,
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


async def test_render_merge_prompt_truncates_long_event_text() -> None:
    """单事件正文超限：按码点截断并附省略标记（不破 UTF-8）。"""
    long_text = "测" * (DREAM_EVENT_TEXT_MAX_CHARS + 200)
    text = render_merge_prompt([_episode(71, user_content=long_text)], [])
    expected = ("用户：" + long_text)[: DREAM_EVENT_TEXT_MAX_CHARS - 1] + "…"
    assert expected in text
    assert long_text[:DREAM_EVENT_TEXT_MAX_CHARS] not in text
    assert "…" in text


async def test_render_merge_prompt_topic_limit_deterministic() -> None:
    """主题上限：按 updated_at / id 降序确定性取样，旧主题不进入 prompt。"""
    topics = [
        _topic(f"主题{index}", topic_id=index, updated_at=1_000 + index)
        for index in range(1, DREAM_MERGE_TOPIC_LIMIT + 11)
    ]
    selected = select_prompt_topics(topics)
    expected_ids = list(range(DREAM_MERGE_TOPIC_LIMIT + 10, 10, -1))
    assert [item.id for item in selected] == expected_ids
    assert [item.id for item in select_prompt_topics(list(reversed(topics)))] == expected_ids
    text = render_merge_prompt([], topics)
    assert f"- 主题{DREAM_MERGE_TOPIC_LIMIT + 10}（" in text
    assert "- 主题10（" not in text


async def test_render_merge_prompt_topic_limit_tie_break_by_id() -> None:
    """同 updated_at 的主题按 id 降序取样（确定性链式排序）。"""
    topics = [
        _topic(f"同刻{index}", topic_id=index, updated_at=5_000)
        for index in range(1, DREAM_MERGE_TOPIC_LIMIT + 6)
    ]
    selected = select_prompt_topics(topics)
    assert [item.id for item in selected] == list(
        range(DREAM_MERGE_TOPIC_LIMIT + 5, 5, -1)
    )


async def test_select_diary_episodes_count_and_char_caps() -> None:
    """日记双帽：条数 200 与正文总字符 60000 分别截止并统计 dropped。"""
    many = [_episode(index) for index in range(1, DREAM_DIARY_MAX_EPISODES + 6)]
    selected, dropped = select_diary_episodes(many)
    assert len(selected) == DREAM_DIARY_MAX_EPISODES
    assert dropped == 5

    long_text = "字" * 1_000
    long_episodes = [
        _episode(index, user_content=long_text, assistant_content="")
        for index in range(1, 131)
    ]
    selected_long, dropped_long = select_diary_episodes(long_episodes)
    expected_count = DREAM_DIARY_MAX_CHARS // DREAM_EVENT_TEXT_MAX_CHARS
    assert len(selected_long) == expected_count
    assert dropped_long == len(long_episodes) - expected_count
    text = render_diary_prompt("大肥鱼", long_episodes)
    assert text.count("- [kind=chat") == len(selected_long)


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


# ---------------------------------------------------------------------------
# 人物事实 prompt 渲染契约
# ---------------------------------------------------------------------------


async def test_render_facts_prompt_numbered_episodes_persona_agnostic() -> None:
    """事实 prompt：编号事件块 + 类别 / 证据字段说明 + 无人格字段。"""
    text = render_facts_prompt([_episode(51, ts_ms=1_760_000_000_000, importance=6)])
    assert text.startswith("【人物事实提取任务】")
    assert "事件 #51" in text
    assert "第51条用户消息" in text
    assert '"category"' in text
    assert '"evidence_episode_ids"' in text
    assert "最多输出 10 条" in text
    assert "dream-prompt" not in text


async def test_render_facts_prompt_template_override_wins() -> None:
    """覆写语义：template_override 整体替换正典模板（自带同名占位符）。"""
    override = "自定义事实模板\n{episodes_block}"
    text = render_facts_prompt([_episode(61)], template_override=override)
    assert text.startswith("自定义事实模板")
    assert "事件 #61" in text
    assert "【人物事实提取任务】" not in text


# ---------------------------------------------------------------------------
# 技能挖矿 prompt 渲染契约
# ---------------------------------------------------------------------------


async def test_render_skills_prompt_numbered_episodes_and_skill_refs() -> None:
    """技能 prompt：编号事件块 + 既有技能名称 / slug 清单 + 人格中立。"""
    text = render_skills_prompt(
        [_episode(71, ts_ms=1_760_000_000_000, importance=6)], [_skill()]
    )
    assert text.startswith("【技能挖矿任务】")
    assert "事件 #71" in text
    assert "第71条用户消息" in text
    assert "- 周报流程（slug: 周报流程）" in text
    assert '"triggers"' in text
    assert '"evidence_episode_ids"' in text
    assert "最多输出 5 条" in text
    assert "dream-prompt" not in text
    assert "1. 先列三件事" not in text  # 既有技能正文不进入 prompt


async def test_render_skills_prompt_empty_skills_placeholder() -> None:
    """无既有技能：清单块为占位文本（不产出空块）。"""
    text = render_skills_prompt([_episode(81)], [])
    assert "事件 #81" in text
    assert "（无）" in text


async def test_render_skills_prompt_template_override_wins() -> None:
    """覆写语义：template_override 整体替换正典模板（自带同名占位符）。"""
    override = "自定义技能模板\n{episodes_block}\n技能：{skills_block}"
    text = render_skills_prompt([_episode(91)], [_skill()], template_override=override)
    assert text.startswith("自定义技能模板")
    assert "事件 #91" in text
    assert "周报流程" in text
    assert "【技能挖矿任务】" not in text

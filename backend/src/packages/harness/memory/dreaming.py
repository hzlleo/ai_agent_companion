"""memory 域梦境整理编排——夜间记忆巩固任务。

夜间梦境（dream）是记忆层的巩固任务：把窗口内的情景事件按规则门
筛选后交 LLM 蒸馏为策展记忆（supersede 语义），再以人格口吻生成
第一人称日记落回情景层——「白天积累、夜里整理」。

调用链（顺序锁定，文档化于 docs/backend/memory.md 夜间梦境一节）::

    DreamService.run_dream(db, gateway=..., persona_name=..., store=?, now_ms=?, cfg=?)
        ↓ ⓪ 孤儿 running 行收敛（stale_running_threshold_ms 阈值内的
          崩溃遗留行批量置 failed——进程崩溃遗留的唯一出口）
        ↓ ① DreamRunRow 落库（status=running——运行日志面，先落库可追溯）
        ↓ ② 取窗口事件 fetch_day_episodes（[now-window, now) 左闭右开；
          超出 max_episodes 按时间序截断并告警）
        ↓ ③ 取回溯窗内既往成功运行的已晋升事件集（promoted_lookback_ms
          回溯窗）+ 既有 active 策展主题
        ↓ ④ 主题相关度命中集（store 可选注入——keyword 检索 top-N，
          失败 / 未注入降级 boost=0）
        ↓ ⑤ 规则门 select_dream_candidates（零模型调用——纯函数）：
          untrusted 硬排除 → 既往已晋升跳过 → 分数门 → 分数降序截断
        ↓ ⑥ LLM 人物事实提取（ModelRole.DREAM，purpose="dream"——人格中立
          prompt）：严格 JSON 数组提案 → 解析（剥代码围栏 + 逐条 Model Class
          校验）→ 证据编号 ⊆ 候选 / 置信度下限 / 单次上限复核 → 归一化
          身份收敛（同身份现行事实只做置信度最大值 + 证据合并的元数据刷新
          ——统计 facts_merged；新身份落 active 行——统计 facts_created；
          丢弃条目录入 facts_dropped）
        ↓ ⑦ LLM 合并（ModelRole.DREAM，purpose="dream"——人格中立 prompt）：
          严格 JSON 数组提案 → 解析（剥代码围栏 + 逐条 Model Class 校验）
        ↓ ⑧ 逐条提案复核（merged_from ⊆ 候选 id——未知引用丢弃计数）→
          upsert_curated（provenance=agent——supersede 语义自动推进链）
        ↓ ⑨ 日记（dream 角色）：当日可信事件 → 第一人称日记 →
          append_episode（kind=dream / source="dream:diary" / provenance=agent）
        ↓ ⑩ 生命周期归档（**纯规则**，零模型调用）：既有 active 策展行按
          memory.lifecycle.should_archive 判定 → archive_curated（执行点
          复核 status=active，快照后已被取代的行跳过；status=archived +
          索引移除）→ stats.archived / decisions(action=archived) 只按
          实际归档集合生成
        ↓ ⑪ DreamRunRow 终态落库（status/error/stats/decisions）→ 返回记录 DTO

冻结 / 置顶语义（生命周期保护面——文档化决策）：

    - **冻结主题不进入合并 prompt 主题列表**（``render_merge_prompt`` 的
      既有主题数据面过滤——模板字节零变化）且**提案目标为冻结主题时
      整条丢弃**（merged_from 逐事件落 skipped 决策，理由含 ``frozen``）；
    - **置顶 / 冻结行不归档**（``should_archive`` 硬门）；置顶行同时免于
      时间衰减（有效强度不衰减）。

fail-closed 语义（锁定决策，逐条）：

    - **untrusted 永不晋升**：规则门在任何打分 / prompt 组装之前硬排除
      （untrusted 既不产生决策、也绝不进入合并 / 日记 prompt）；晋升
      统一经 ``upsert_curated(provenance=agent)``——服务层对 untrusted
      的拒绝构成双保险；
    - **解析失败零晋升**：LLM 输出不可解析为 JSON 数组 → 运行标记
      failed，**零 upsert**（解析先于全部晋升——结构性失败不存在部分
      晋升态）；逐条提案校验失败（字段越界 / merged_from 未知引用）→
      该条丢弃计数（stats.failed），其余提案正常晋升；
    - **事实提取阶段 fail-closed（与合并阶段同轴）**：事实提取的输出
      结构失败（非 JSON / 非数组 / 空输出 / 调用异常）→ 运行标记
      failed，**零合并 / 零日记**（前序阶段已提交的产物保留——事实阶
      段的既有事实写入同样已提交）；逐条事实提案失败（类别越界 / 正文
      越界 / 证据编号越出候选 / 置信度低于下限 / 超单次上限）→ 逐条丢弃
      计数（stats.facts_dropped），其余提案正常落库；
    - **事实提取后置于规则门、前置于合并**（锁定阶段序）：事实与合并
      消费**同一批过门候选**——事实是「对方是谁」的结构化蒸馏、合并是
      「发生了什么」的策展蒸馏，两者互不阻断（事实阶段的丢弃只影响
      事实表，不改变合并提案面）；
    - **日记后置于晋升**（文档化排序决策）：晋升全部提交后才生成日记
      ——日记失败标记运行 failed 但**保留已提交的晋升并在 decisions
      中如实记录**（晋升是本任务的主产物、日记是叙事附产物；倒序会让
      日记失败回滚掉已有效的记忆巩固，代价更高）；
    - **合并中断决策补齐**：事实提取 / 合并 / 晋升阶段异常中断时，过门
      但未决策的候选事件补 skipped 决策（reason 标注中断阶段）——失败
      运行的决策面对全部可信候选同样全覆盖；
    - **孤儿 running 行收敛**：run_dream 入口把本人格
      ``started_ms < now - stale_running_threshold_ms`` 且无终态的
      running 行批量置 failed（error=进程崩溃遗留）——进程崩溃是
      running 行终态缺失的唯一来源，调度 / 手工触发入口共享本收敛；
    - **运行失败不抛异常**：run_dream 返回终态记录 DTO（status=failed +
      error 原因）——手工触发面（admin API）以运行日志为产物，失败是
      业务结果而非请求失败；仅人格上下文缺失 / 运行行落库失败在入口
      fail loud 上抛。

纯函数面（精确数学可测）：:func:`recency_decay` / :func:`dream_score`
/ :func:`select_dream_candidates` / :func:`parse_merge_proposals`——
无 I/O、无模型调用；编排（本模块 DreamService）负责取数与落库。

构造级旋钮：:class:`DreamJobConfig`（窗口 / 半衰期 / 分数门 / 上限 /
相关度加成 / 日记开关 / 孤儿 running 收敛阈值 / 已晋升回溯窗 / 单次
事件上限——构造注入，与 llm 网关 retry 旋钮同模式；Settings env 面
迁移为已文档化的演进方向）。
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any, cast

from loguru import logger
from pydantic import ValidationError
from sqlalchemy import desc, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from packages.harness.config.model_entry import ModelRole
from packages.harness.core import time_utils
from packages.harness.core.context import persona_id_var
from packages.harness.core.dtos import PageResponse, decode_cursor, encode_cursor
from packages.harness.core.errors import BusinessException, ErrorCode
from packages.harness.llm.gateway import LlmGateway
from packages.harness.llm.llm_models import ChatMessageModel, ChatMessageRole
from packages.harness.memory.dreaming_models import (
    DreamDecisionAction,
    DreamDecisionModel,
    DreamFactProposedItemModel,
    DreamJobConfig,
    DreamProposedItemModel,
    DreamRunQueryDTO,
    DreamRunRecordDTO,
    DreamRunStatsModel,
    DreamRunStatus,
)
from packages.harness.memory.dreaming_orm import DreamRunRow
from packages.harness.memory.dreaming_prompts import (
    render_diary_prompt,
    render_facts_prompt,
    render_merge_prompt,
)
from packages.harness.memory.dtos import (
    CuratedRecordDTO,
    CuratedUpsertDTO,
    EpisodeCreateDTO,
    EpisodeRecordDTO,
)
from packages.harness.memory.lifecycle import LifecycleConfig, should_archive
from packages.harness.memory.models import (
    CuratedStatus,
    EpisodeKind,
    EpisodeNotePayloadModel,
    MemoryProvenance,
)
from packages.harness.memory.orm import CuratedMemoryRow
from packages.harness.memory.retrieval_models import (
    MemoryRefType,
    MemorySearchMode,
    MemorySearchQueryDTO,
)
from packages.harness.memory.service import MemoryService
from packages.harness.memory.store import MemoryStore
from packages.harness.persona.facts import FactsService
from packages.harness.persona.facts_models import (
    FACT_MIN_CONFIDENCE,
    FactCreateDTO,
    FactEvidenceRefModel,
    PersonaFactSource,
)

__all__ = [
    "MS_PER_DAY",
    "MS_PER_HOUR",
    "DREAM_PURPOSE",
    "DIARY_SOURCE",
    "FACTS_PROPOSAL_CAP",
    "recency_decay",
    "dream_score",
    "select_dream_candidates",
    "parse_merge_proposals",
    "parse_facts_proposals",
    "select_archive_candidates",
    "DreamService",
    "ARCHIVE_REASON_STRENGTH",
    "SKIP_REASON_FROZEN",
]

#: 一天的毫秒数（epoch 毫秒轴的换算基准）。
MS_PER_DAY: int = 86_400_000
#: 一小时的毫秒数（窗口宽度换算）。
MS_PER_HOUR: int = 3_600_000
#: 梦境链路的用量 purpose（usage 域枚举既有成员——合并 + 日记共用）。
DREAM_PURPOSE: str = "dream"
#: 日记事件的来源标识（情景层 kind=dream 行的 source）。
DIARY_SOURCE: str = "dream:diary"
#: 孤儿 running 行收敛的落库失败原因（run_dream 入口批量置 failed 时写入
#: DreamRunRow.error——进程崩溃遗留的终态补齐）。
_STALE_RUNNING_ERROR: str = "进程崩溃遗留，由调度收敛"
#: 归档决策理由（生命周期阶段的 decisions.reason——强度低于归档阈值）。
ARCHIVE_REASON_STRENGTH: str = "strength_below_threshold"
#: 冻结目标导致的提案丢弃理由（decisions.reason 片段——文档化 deltas）。
SKIP_REASON_FROZEN: str = "目标主题已冻结（frozen），提案丢弃"
#: 人物事实单次提取的条数上限（提案超出部分丢弃并计入 facts_dropped）。
FACTS_PROPOSAL_CAP: int = 10
#: 事实提取阶段中断的决策补齐理由（过门未决策候选的 skipped 理由）。
_FACTS_INTERRUPT_REASON: str = "事实提取中断，本次未完成该事件的决策"
#: 合并阶段中断的决策补齐理由（与事实阶段的措辞区分——可审计）。
_MERGE_INTERRUPT_REASON: str = "合并中断，本次未完成该事件的决策"


class _DreamPhaseError(Exception):
    """梦境阶段失败（fail-closed——携带人读原因，落运行日志 error 列）。

    Attributes:
        reason: 人读失败原因（直接落 DreamRunRow.error）。
    """

    def __init__(self, reason: str) -> None:
        """初始化阶段失败。

        Args:
            reason: 人读失败原因。
        """
        super().__init__(reason)
        self.reason = reason


async def _rollback_quietly(session: AsyncSession) -> None:
    """尽力回滚（异常路径清理会话待处理态——不注入新异常）。

    Args:
        session: 待回滚的异步 Session。
    """
    try:
        await session.rollback()
    except Exception:  # best-effort: 回滚失败由会话关闭兜底
        logger.opt(exception=True).debug("梦境路径会话回滚失败（close 兜底）")


async def _commit(session: AsyncSession) -> None:
    """提交事务（异常回滚后原样上抛——写路径统一出口）。

    Args:
        session: 注入的异步 Session。

    Raises:
        Exception: 提交失败（已回滚，由上层异常处理器接面）。
    """
    try:
        await session.commit()
    except Exception:
        await _rollback_quietly(session)
        raise


# =========================================================================== []
# 纯函数面——精确数学 / 纯规则（无 I/O、无模型调用）
# =========================================================================== []


def recency_decay(ts_ms: int, now_ms: int, half_life_days: float) -> float:
    """时间衰减因子（指数半衰期语义——纯函数）。

    公式：``0.5 ** (age_days / half_life_days)``，其中
    ``age_days = (now_ms - ts_ms) / MS_PER_DAY``。恰过一个半衰期时
    因子**精确为 0.5**（两 个半衰期 0.25，依此类推）；事件时间晚于
    now（未来时间戳）按 age=0 处理（因子 1.0——不奖励未来时间）。

    Args:
        ts_ms: 事件时间（epoch 毫秒）。
        now_ms: 参考当前时间（epoch 毫秒——注入式，测试可控）。
        half_life_days: 半衰期（天；须 > 0）。

    Returns:
        衰减因子（0 < factor <= 1；时间越久单调越小）。

    Raises:
        BusinessException: half_life_days 非正（``VALIDATION_ERROR``——
            装配错误面）。
    """
    if half_life_days <= 0:
        raise BusinessException(
            code=ErrorCode.VALIDATION_ERROR,
            message="半衰期须为正数",
            details={"half_life_days": half_life_days},
        )
    age_days = max(0, now_ms - ts_ms) / MS_PER_DAY
    return float(0.5 ** (age_days / half_life_days))


def dream_score(
    record: EpisodeRecordDTO,
    now_ms: int,
    cfg: DreamJobConfig,
    *,
    topic_hits: frozenset[int] = frozenset(),
) -> float:
    """梦境分（事件筛选的复合权重——纯函数）。

    公式：``importance * recency_decay * (1 + relevance_boost)``，其中
    ``relevance_boost = cfg.boost`` 当事件命中既有主题相关度集合
    （``topic_hits``——编排层经存储 keyword 检索预计算的命中事件 id
    集），否则 0。数学保持纯：存储交互在编排层，本函数只消费预计算
    的命中映射。

    Args:
        record: 情景事件记录。
        now_ms: 参考当前时间（epoch 毫秒）。
        cfg: 梦境任务参数（半衰期 / 加成系数）。
        topic_hits: 主题相关度命中事件 id 集（缺省空——无加成）。

    Returns:
        梦境分（importance 1-10 × 衰减 (0,1] × 加成 (1,1+boost]）。
    """
    decay = recency_decay(record.ts_ms, now_ms, cfg.recency_half_life_days)
    boost = cfg.boost if record.id in topic_hits else 0.0
    return float(record.importance * decay * (1.0 + boost))


def select_dream_candidates(
    episodes: Sequence[EpisodeRecordDTO],
    *,
    now_ms: int,
    cfg: DreamJobConfig,
    already_promoted_ids: frozenset[int] = frozenset(),
    topic_hits: frozenset[int] = frozenset(),
) -> tuple[list[EpisodeRecordDTO], DreamRunStatsModel, list[DreamDecisionModel]]:
    """规则门（零模型调用——纯函数）：窗口事件 → 过门候选 + 统计 + 跳过决策。

    门矩阵（按序应用，文档化于 docs/backend/memory.md）：

        1. **untrusted 硬排除**（先于一切）：provenance=untrusted 的事件
           只计数 ``skipped_untrusted``——不产生决策、永不进入 prompt；
        2. **既往已晋升跳过**：episode_id ∈ ``already_promoted_ids``
           （既往成功运行 promoted/merged 决策的并集）→ skipped 决策；
        3. **分数门**：``dream_score < cfg.min_score`` → skipped 决策
           （理由含分数值）；
        4. **上限截断**：过门事件按分数降序（同分按 id 升序——确定性
           排序）取前 ``cfg.max_candidates``，溢出 → skipped 决策。

    Args:
        episodes: 窗口内事件（时间序——调用方取回口径）。
        now_ms: 参考当前时间（epoch 毫秒）。
        cfg: 梦境任务参数。
        already_promoted_ids: 既往成功运行已晋升事件 id 集。
        topic_hits: 主题相关度命中事件 id 集（空 = 无加成）。

    Returns:
        ``(selected, stats, decisions)`` 三元组——过门候选（分数降序）、
        统计（candidates / skipped_untrusted / gated 已填）、跳过决策
        列表（promoted / superseded 计数由编排终态回填）。
    """
    stats = DreamRunStatsModel()
    decisions: list[DreamDecisionModel] = []
    trusted: list[EpisodeRecordDTO] = []
    for record in episodes:
        if record.provenance == MemoryProvenance.UNTRUSTED:
            stats.skipped_untrusted += 1
            continue
        trusted.append(record)
    stats.candidates = len(trusted)
    pending: list[tuple[float, EpisodeRecordDTO]] = []
    for record in trusted:
        if record.id in already_promoted_ids:
            decisions.append(
                DreamDecisionModel(
                    episode_id=record.id,
                    action=DreamDecisionAction.SKIPPED,
                    reason="既往梦境运行已晋升，跳过重复巩固",
                )
            )
            continue
        score = dream_score(record, now_ms, cfg, topic_hits=topic_hits)
        if score < cfg.min_score:
            decisions.append(
                DreamDecisionModel(
                    episode_id=record.id,
                    action=DreamDecisionAction.SKIPPED,
                    reason=f"梦境分 {score:.3f} 低于阈值 {cfg.min_score:g}",
                )
            )
            continue
        pending.append((score, record))
    pending.sort(key=lambda pair: (-pair[0], pair[1].id))
    selected = [record for _, record in pending[: cfg.max_candidates]]
    for _, record in pending[cfg.max_candidates :]:
        decisions.append(
            DreamDecisionModel(
                episode_id=record.id,
                action=DreamDecisionAction.SKIPPED,
                reason="分数达标但超出单次候选上限，本次未送模型",
            )
        )
    stats.gated = len(selected)
    return selected, stats, decisions


def _strip_code_fences(text: str) -> str:
    """剥除 Markdown 代码围栏（``` / ```json 包裹——模型输出容错）。

    Args:
        text: 模型原始输出文本。

    Returns:
        去围栏后的文本（无围栏时原样 strip 返回）。
    """
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    lines = stripped.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].lstrip().startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines).strip()


def parse_merge_proposals(raw: str) -> tuple[list[DreamProposedItemModel], int]:
    """解析 LLM 合并输出（robust parse——围栏剥离 + 逐条 Model Class 校验）。

    结构性失败（整体 fail-closed——上抛由运行标记 failed，零晋升）：

        - 输出不是合法 JSON（``json.JSONDecodeError``）；
        - 顶层不是 JSON 数组。

    逐条失败（丢弃计数，不整体失败）：数组元素无法通过
    :class:`DreamProposedItemModel` 校验（kind 枚举 / importance 边界 /
    topic 空 / merged_from 空）——返回 dropped 计数。

    Args:
        raw: 模型原始输出文本（可含代码围栏）。

    Returns:
        ``(valid_items, dropped_count)`` 二元组。

    Raises:
        BusinessException: 结构性解析失败（``VALIDATION_ERROR``——运行
            标记 failed 的原因面）。
    """
    text = _strip_code_fences(raw)
    try:
        payload: Any = json.loads(text)
    except json.JSONDecodeError as exc:
        raise BusinessException(
            code=ErrorCode.VALIDATION_ERROR,
            message="梦境合并输出不是合法 JSON",
            details={"reason": str(exc)},
        ) from exc
    if not isinstance(payload, list):
        raise BusinessException(
            code=ErrorCode.VALIDATION_ERROR,
            message="梦境合并输出必须是 JSON 数组",
            details={"got": type(payload).__name__},
        )
    valid: list[DreamProposedItemModel] = []
    dropped = 0
    for element in payload:
        try:
            valid.append(DreamProposedItemModel.model_validate(element))
        except ValidationError:
            dropped += 1
            logger.warning("梦境提案条目校验失败，已丢弃: {}", str(element)[:200])
    return valid, dropped


def parse_facts_proposals(raw: str) -> tuple[list[DreamFactProposedItemModel], int]:
    """解析 LLM 人物事实输出（robust parse——围栏剥离 + 逐条 Model Class 校验）。

    结构性失败（整体 fail-closed——上抛由运行标记 failed，零合并 / 零
    日记）与 :func:`parse_merge_proposals` 同轴：

        - 输出不是合法 JSON（``json.JSONDecodeError``）；
        - 顶层不是 JSON 数组。

    逐条失败（丢弃计数，不整体失败）：元素无法通过
    :class:`DreamFactProposedItemModel` 校验（类别越界 / 正文越界 /
    置信度越界 / 证据编号列表为空）。

    Args:
        raw: 模型原始输出文本（可含代码围栏）。

    Returns:
        ``(valid_items, dropped_count)`` 二元组。

    Raises:
        BusinessException: 结构性解析失败（``VALIDATION_ERROR``——运行
            标记 failed 的原因面）。
    """
    text = _strip_code_fences(raw)
    try:
        payload: Any = json.loads(text)
    except json.JSONDecodeError as exc:
        raise BusinessException(
            code=ErrorCode.VALIDATION_ERROR,
            message="梦境事实提取输出不是合法 JSON",
            details={"reason": str(exc)},
        ) from exc
    if not isinstance(payload, list):
        raise BusinessException(
            code=ErrorCode.VALIDATION_ERROR,
            message="梦境事实提取输出必须是 JSON 数组",
            details={"got": type(payload).__name__},
        )
    valid: list[DreamFactProposedItemModel] = []
    dropped = 0
    for element in payload:
        try:
            valid.append(DreamFactProposedItemModel.model_validate(element))
        except ValidationError:
            dropped += 1
            logger.warning("梦境事实提案条目校验失败，已丢弃: {}", str(element)[:200])
    return valid, dropped


def _backfill_undecided(
    selected: Sequence[EpisodeRecordDTO],
    decided_ids: set[int],
    decisions: list[DreamDecisionModel],
    *,
    reason: str,
) -> None:
    """为过门但未决策的候选补 skipped 决策（失败路径的决策面全覆盖）。

    Args:
        selected: 过门候选（分数降序）。
        decided_ids: 已决策事件 id 集（就地更新——防重复决策）。
        decisions: 决策列表（就地追加）。
        reason: 补齐理由（标注中断阶段——可审计）。
    """
    for candidate in selected:
        if candidate.id in decided_ids:
            continue
        decisions.append(
            DreamDecisionModel(
                episode_id=candidate.id,
                action=DreamDecisionAction.SKIPPED,
                reason=reason,
            )
        )
        decided_ids.add(candidate.id)


def select_archive_candidates(
    topics: Sequence[CuratedRecordDTO],
    *,
    now_ms: int,
    cfg: LifecycleConfig,
) -> list[CuratedRecordDTO]:
    """生命周期归档候选（纯函数——零 I/O、零模型调用）。

    判定委托 :func:`packages.harness.memory.lifecycle.should_archive`
    （非置顶 / 非冻结 / 现行 active / 年龄超阈值 / 强度低于阈值）；
    候选按 id 升序返回（确定性——归档语句与决策列表的稳定排列）。

    Args:
        topics: 既有 active 策展记忆（孤儿 running 收敛后的取回面）。
        now_ms: 参考当前时间（epoch 毫秒——注入式，测试可控）。
        cfg: 生命周期参数（衰减 / 强化 / 归档阈值 / 最小年龄）。

    Returns:
        应归档的策展记忆记录列表（id 升序；空列表 = 本次无归档）。
    """
    candidates = [
        topic
        for topic in topics
        if should_archive(
            importance=topic.importance,
            anchor_ms=topic.created_at,
            access_count=topic.access_count,
            pinned=topic.pinned,
            frozen=topic.frozen,
            status=topic.status,
            now_ms=now_ms,
            config=cfg,
        )
    ]
    return sorted(candidates, key=lambda topic: topic.id)


# =========================================================================== []
# 编排面——DreamService（取数 / LLM 调用 / 落库；无状态，Session 注入）
# =========================================================================== []


class DreamService:
    """梦境整理编排服务（run_dream 一次运行 + 运行日志读写面）。

    人格作用域契约：与 memory 域其余服务一致——**调用方须先经
    ``persona.bind_persona_context`` 绑定人格上下文**（PersonaMixin 自动
    注入 / 自动过滤；缺上下文在运行行落库前即 fail loud）。

    构造注入：``memory_service`` / ``facts_service`` 可替换（默认新实例
    ——无状态共享安全）；网关 / 存储 / 时钟 / 参数逐次调用注入
    （run_dream 关键字参数）。
    """

    def __init__(
        self,
        *,
        memory_service: MemoryService | None = None,
        lifecycle_config: LifecycleConfig | None = None,
        facts_service: FactsService | None = None,
    ) -> None:
        """初始化服务。

        Args:
            memory_service: 记忆域服务（None = 新实例——日记追加 / 策展
                晋升 / 窗口取回 / 归档的复用面；注入索引绑定形态时归档
                同步移除检索索引）。
            lifecycle_config: 生命周期参数（None = LifecycleConfig 默认
                值——归档阶段的强度判定旋钮）。
            facts_service: 人物事实服务（None = 以本服务的记忆服务构造
                默认实例——事实提取阶段的写入面与证据校验面）。
        """
        self._memory = memory_service if memory_service is not None else MemoryService()
        self._lifecycle_config = (
            lifecycle_config if lifecycle_config is not None else LifecycleConfig()
        )
        self._facts = (
            facts_service
            if facts_service is not None
            else FactsService(memory_service=self._memory)
        )

    # ------------------------------------------------------------------
    # 夜间梦境主链路
    # ------------------------------------------------------------------

    async def run_dream(
        self,
        session: AsyncSession,
        *,
        gateway: LlmGateway,
        persona_name: str,
        store: MemoryStore | None = None,
        now_ms: int | None = None,
        cfg: DreamJobConfig | None = None,
    ) -> DreamRunRecordDTO:
        """执行一次夜间梦境整理（fail-closed——返回终态记录，失败不抛）。

        链路与 fail-closed 语义见模块 docstring。入口先收敛孤儿
        running 行（``stale_running_threshold_ms`` 阈值内的崩溃遗留
        行批量置 failed）；关键排序决策：**晋升全部提交后才生成日记**
        ——日记失败标记运行 failed 但保留已提交晋升（decisions 如实
        记录）。

        Args:
            session: 注入的异步 Session（调用方已绑定人格上下文）。
            gateway: LLM 网关（dream 角色路由——合并 + 日记共用）。
            persona_name: 人格展示名（日记 prompt 的声线变量——调用方
                解析目标人格后传入；合并 prompt 人格中立，不消费）。
            store: 记忆检索存储（None = 主题相关度降级 boost=0）。
            now_ms: 注入时钟（epoch 毫秒；None = 当前真实时间——窗口
                与打分的参考轴）。
            cfg: 任务参数（None = DreamJobConfig 全默认）。

        Returns:
            运行记录 DTO（status=success / failed——失败原因在 error）。

        Raises:
            BusinessException: 人格上下文未绑定（``PERSONA_CONTEXT_REQUIRED``
                ——入口装配错误，运行行尚未落库，fail loud）；运行行落库
                失败（透传——无法记录运行）。
        """
        resolved_cfg = cfg if cfg is not None else DreamJobConfig()
        run_now = now_ms if now_ms is not None else time_utils.now_ms()
        persona_scope = persona_id_var.get()
        if persona_scope is None:
            raise BusinessException(
                code=ErrorCode.PERSONA_CONTEXT_REQUIRED,
                message="梦境运行缺少人格上下文",
                details={"fix": "调用方先经 bind_persona_context 绑定人格上下文"},
            )
        await self._converge_stale_running(session, run_now=run_now, cfg=resolved_cfg)
        row = DreamRunRow(
            status=DreamRunStatus.RUNNING.value,
            started_ms=run_now,
            finished_ms=None,
            stats={},
            decisions=[],
            diary_episode_id=None,
            error=None,
        )
        session.add(row)
        await _commit(session)  # running 行先落库——运行日志面可追溯
        logger.info("梦境运行开始: run_id={}, persona={}", row.id, persona_scope)

        stats = DreamRunStatsModel()
        decisions: list[DreamDecisionModel] = []
        error_message: str | None = None
        try:
            await self._run_phases(
                session,
                row=row,
                gateway=gateway,
                persona_scope=persona_scope,
                persona_name=persona_name,
                store=store,
                run_now=run_now,
                cfg=resolved_cfg,
                stats=stats,
                decisions=decisions,
            )
        except _DreamPhaseError as exc:
            error_message = exc.reason
        except BusinessException as exc:
            error_message = f"梦境运行业务异常 {exc.code}: {exc.message}"
        except Exception as exc:  # 兜底：未知异常也落终态（运行日志完整）
            logger.opt(exception=True).error("梦境运行异常: run_id={}", row.id)
            error_message = f"梦境运行异常: {exc}"
        finally:
            if error_message is not None:
                await _rollback_quietly(session)
        # 终态回填：promoted / superseded 从决策动作回算（与决策口径对齐）
        stats.promoted = sum(1 for d in decisions if d.action == DreamDecisionAction.PROMOTED)
        stats.superseded = sum(1 for d in decisions if d.action == DreamDecisionAction.MERGED)
        row.status = (
            DreamRunStatus.FAILED.value
            if error_message is not None
            else DreamRunStatus.SUCCESS.value
        )
        row.finished_ms = time_utils.now_ms()
        row.stats = stats.model_dump()
        row.decisions = [decision.model_dump() for decision in decisions]
        row.error = error_message
        await _commit(session)
        logger.info(
            "梦境运行结束: run_id={}, status={}, candidates={}, promoted={}, superseded={}",
            row.id,
            row.status,
            stats.candidates,
            stats.promoted,
            stats.superseded,
        )
        return DreamRunRecordDTO.model_validate(row)

    async def _converge_stale_running(
        self, session: AsyncSession, *, run_now: int, cfg: DreamJobConfig
    ) -> int:
        """收敛孤儿 running 行（进程崩溃遗留——单条批量 UPDATE 置 failed）。

        判定：``status=running AND finished_ms IS NULL AND started_ms <
        run_now - cfg.stale_running_threshold_ms``（人格作用域经
        PersonaMixin 批量 UPDATE 自动过滤——仅本人格行）。当前运行行
        尚未创建，天然不在收敛面。返回收敛行数（零即无遗留）。

        Args:
            session: 注入的异步 Session（人格作用域批量 UPDATE）。
            run_now: 注入时钟（epoch 毫秒——阈值参考轴）。
            cfg: 任务参数（stale_running_threshold_ms 收敛阈值）。

        Returns:
            本次收敛的行数。

        Raises:
            Exception: 收敛 UPDATE 落库失败（归 run_dream 入口 fail loud
                ——与运行行落库失败同语义）。
        """
        stale_before = run_now - cfg.stale_running_threshold_ms
        # ORM-enabled UPDATE——运行期返回 CursorResult（DML 行计数面）
        result = cast(
            "CursorResult[Any]",
            await session.execute(
                update(DreamRunRow)
                .where(
                    DreamRunRow.status == DreamRunStatus.RUNNING.value,
                    DreamRunRow.finished_ms.is_(None),
                    DreamRunRow.started_ms < stale_before,
                )
                .values(
                    status=DreamRunStatus.FAILED.value,
                    finished_ms=run_now,
                    error=_STALE_RUNNING_ERROR,
                )
            ),
        )
        converged = int(result.rowcount)
        await _commit(session)
        if converged:
            logger.warning(
                "已收敛孤儿梦境运行行（进程崩溃遗留）: count={}, threshold_ms={}",
                converged,
                cfg.stale_running_threshold_ms,
            )
        return converged

    async def _run_phases(
        self,
        session: AsyncSession,
        *,
        row: DreamRunRow,
        gateway: LlmGateway,
        persona_scope: str,
        persona_name: str,
        store: MemoryStore | None,
        run_now: int,
        cfg: DreamJobConfig,
        stats: DreamRunStatsModel,
        decisions: list[DreamDecisionModel],
    ) -> None:
        """执行取数 → 门控 → 事实提取 → 合并 → 晋升 → 日记 → 生命周期归档各阶段。

        阶段顺序锁定（gate → facts → merge → diary → archive → finalize）：
        事实提取消费与合并同一批过门候选、且先于合并（事实是画像素描、
        合并是事件蒸馏——事实层的丢弃不改变合并提案面）；日记之后执行
        归档（归档是**整理产物**，失败归 run_dream 终态记录；归档前序
        阶段异常时本次跳过归档——下个周期重试）。

        Args:
            session: 注入的异步 Session。
            row: 运行日志行（已落库——日记事件 metadata 引用其 id）。
            gateway: LLM 网关。
            persona_scope: 人格作用域 ID（上下文变量值）。
            persona_name: 人格展示名（日记声线变量）。
            store: 检索存储（None = 相关度降级）。
            run_now: 注入时钟（窗口 / 打分参考轴）。
            cfg: 任务参数。
            stats: 运行统计（就地累加）。
            decisions: 决策列表（就地追加）。

        Raises:
            _DreamPhaseError: 阶段失败（fail-closed——见模块 docstring）。
            BusinessException: LLM / 存储 / 落库业务异常（归 run_dream 记录）。
        """
        window_start = run_now - cfg.window_hours * MS_PER_HOUR
        episodes = await self._memory.fetch_day_episodes(
            session, start_ms=window_start, end_ms=run_now
        )
        if len(episodes) > cfg.max_episodes:
            logger.warning(
                "窗口事件数超出单次上限，按时间序截断: count={}, cap={}",
                len(episodes),
                cfg.max_episodes,
            )
            episodes = episodes[: cfg.max_episodes]
        trusted = [record for record in episodes if record.provenance != MemoryProvenance.UNTRUSTED]
        already_promoted = await self._previous_promoted_ids(session, run_now=run_now, cfg=cfg)
        active_topics = await self._active_topics(session)
        prompt_topics = [topic for topic in active_topics if not topic.frozen]
        frozen_keys = frozenset(
            (topic.kind.value, topic.topic) for topic in active_topics if topic.frozen
        )
        topic_hits = (
            await self._collect_topic_hits(store, persona_scope, prompt_topics, cfg)
            if store is not None and trusted and prompt_topics
            else frozenset()
        )
        selected, gate_stats, gate_decisions = select_dream_candidates(
            episodes,
            now_ms=run_now,
            cfg=cfg,
            already_promoted_ids=already_promoted,
            topic_hits=topic_hits,
        )
        stats.candidates = gate_stats.candidates
        stats.skipped_untrusted = gate_stats.skipped_untrusted
        stats.gated = gate_stats.gated
        decisions.extend(gate_decisions)
        decided_ids = {decision.episode_id for decision in decisions}

        if selected:
            await self._extract_facts(
                session,
                gateway=gateway,
                persona_scope=persona_scope,
                selected=selected,
                stats=stats,
                decisions=decisions,
                decided_ids=decided_ids,
            )
            stats.failed += await self._merge_and_promote(
                session,
                gateway=gateway,
                persona_scope=persona_scope,
                selected=selected,
                active_topics=prompt_topics,
                frozen_keys=frozen_keys,
                stats=stats,
                decisions=decisions,
                decided_ids=decided_ids,
            )
        if cfg.diary_enabled and trusted:
            row.diary_episode_id = await self._write_diary(
                session,
                gateway=gateway,
                persona_scope=persona_scope,
                persona_name=persona_name,
                day_episodes=trusted,
                run_now=run_now,
                cfg=cfg,
                row_id=row.id,
            )
        await self._archive_pass(
            session, run_now=run_now, active_topics=active_topics, stats=stats, decisions=decisions
        )

    async def _extract_facts(
        self,
        session: AsyncSession,
        *,
        gateway: LlmGateway,
        persona_scope: str,
        selected: list[EpisodeRecordDTO],
        stats: DreamRunStatsModel,
        decisions: list[DreamDecisionModel],
        decided_ids: set[int],
    ) -> None:
        """LLM 人物事实提取 + 逐条复核 + 落库（异常时补齐决策后上抛）。

        异常中断决策补齐（与合并阶段同一语义）：提取阶段任何异常上抛前，
        过门但未决策的候选补 skipped 决策（reason 标注事实提取中断）——
        失败运行的决策面对全部可信候选同样全覆盖。

        Args:
            session: 注入的异步 Session。
            gateway: LLM 网关（dream 角色）。
            persona_scope: 人格作用域 ID。
            selected: 过门候选（分数降序——与合并阶段同一批）。
            stats: 运行统计（facts_created / facts_merged / facts_dropped
                就地累加）。
            decisions: 决策列表（中断补齐时就地追加）。
            decided_ids: 已决策事件 id 集（就地更新——防重复决策）。

        Raises:
            _DreamPhaseError: 模型返回空内容（fail-closed——零合并 / 零日记）。
            BusinessException: 解析结构失败 / LLM 调用异常 / 落库失败（归
                run_dream 终态记录）。
        """
        try:
            await self._extract_facts_inner(
                session,
                gateway=gateway,
                persona_scope=persona_scope,
                selected=selected,
                stats=stats,
            )
        except Exception:
            _backfill_undecided(selected, decided_ids, decisions, reason=_FACTS_INTERRUPT_REASON)
            raise

    async def _extract_facts_inner(
        self,
        session: AsyncSession,
        *,
        gateway: LlmGateway,
        persona_scope: str,
        selected: list[EpisodeRecordDTO],
        stats: DreamRunStatsModel,
    ) -> None:
        """事实提取主体（异常上抛由 ``_extract_facts`` 补决策）。

        复核矩阵（逐条应用，文档化）：字段校验失败（解析级）→ 计数
        ``facts_dropped``；证据编号越出候选集 → 丢弃计数；置信度低于
        ``FACT_MIN_CONFIDENCE`` → 丢弃计数；超出单次
        ``FACTS_PROPOSAL_CAP`` 条 → 丢弃计数。通过复核的提案经
        ``FactsService.upsert_fact`` 落库（provenance=agent /
        source=dream）：同归一化身份现行事实走元数据刷新（计数
        ``facts_merged``）、新身份落现行行（计数 ``facts_created``）。
        证据摘录由系统从被引用候选事件正文**逐字**截取（≤200 字符）——
        模型不产出摘录文本，引用编号即背书。

        Args:
            session: 注入的异步 Session。
            gateway: LLM 网关（dream 角色）。
            persona_scope: 人格作用域 ID（用量归属）。
            selected: 过门候选（分数降序）。
            stats: 运行统计（三计数就地累加）。

        Raises:
            _DreamPhaseError: 模型返回空内容。
            BusinessException: 解析结构失败 / LLM 调用异常 / 落库失败。
        """
        prompt = render_facts_prompt(selected)
        response = await gateway.chat_by_role(
            ModelRole.DREAM,
            [ChatMessageModel(role=ChatMessageRole.SYSTEM, content=prompt)],
            persona_id=persona_scope,
            purpose=DREAM_PURPOSE,
        )
        raw = response.content.strip()
        if not raw:
            raise _DreamPhaseError("梦境事实提取模型返回空内容")
        valid_items, dropped_count = parse_facts_proposals(raw)
        stats.facts_dropped += dropped_count
        episodes_by_id = {record.id: record for record in selected}
        accepted: list[tuple[DreamFactProposedItemModel, list[FactEvidenceRefModel]]] = []
        for item in valid_items:
            if len(accepted) >= FACTS_PROPOSAL_CAP:
                stats.facts_dropped += 1
                logger.info("人物事实提案超出单次上限，已丢弃: content={}", item.content[:80])
                continue
            unknown_ids = [
                episode_id
                for episode_id in item.evidence_episode_ids
                if episode_id not in episodes_by_id
            ]
            if unknown_ids:
                stats.facts_dropped += 1
                logger.warning(
                    "人物事实提案引用未知事件，已丢弃: content={}, unknown={}",
                    item.content[:80],
                    unknown_ids,
                )
                continue
            if item.confidence < FACT_MIN_CONFIDENCE:
                stats.facts_dropped += 1
                logger.info(
                    "人物事实提案置信度低于下限，已丢弃: content={}, confidence={:.2f}",
                    item.content[:80],
                    item.confidence,
                )
                continue
            references = [
                self._fact_evidence(episodes_by_id[episode_id])
                for episode_id in dict.fromkeys(item.evidence_episode_ids)
            ]
            accepted.append((item, [ref for ref in references if ref is not None]))
        for item, evidence in accepted:
            result = await self._facts.upsert_fact(
                session,
                FactCreateDTO(
                    category=item.category,
                    content=item.content,
                    confidence=item.confidence,
                    evidence=evidence,
                ),
                provenance=MemoryProvenance.AGENT,
                source=PersonaFactSource.DREAM,
            )
            if result.created:
                stats.facts_created += 1
            else:
                stats.facts_merged += 1
        logger.info(
            "梦境人物事实提取完成: created={}, merged={}, dropped={}",
            stats.facts_created,
            stats.facts_merged,
            stats.facts_dropped,
        )

    def _fact_evidence(self, record: EpisodeRecordDTO) -> FactEvidenceRefModel | None:
        """候选事件 → 证据引用（逐字截取——无文本载荷返回 None）。

        Args:
            record: 被引用的候选事件记录。

        Returns:
            证据引用（摘录恒为被引用事件正文的逐字前缀，≤200 字符）；
            事件无文本载荷时返回 None（该证据不落库——事实仍可落库）。
        """
        quote = self._facts.evidence_text(record)
        if not quote:
            return None
        return FactEvidenceRefModel(
            episode_id=record.id,
            quote=quote,
            ts_ms=record.ts_ms,
            provenance=record.provenance,
        )

    async def _merge_and_promote(
        self,
        session: AsyncSession,
        *,
        gateway: LlmGateway,
        persona_scope: str,
        selected: list[EpisodeRecordDTO],
        active_topics: list[CuratedRecordDTO],
        frozen_keys: frozenset[tuple[str, str]],
        stats: DreamRunStatsModel,
        decisions: list[DreamDecisionModel],
        decided_ids: set[int],
    ) -> int:
        """LLM 合并 + 逐条复核 + 晋升（返回提案丢弃计数）。

        异常中断决策补齐：任何阶段异常上抛前，过门但未决策的候选事件
        补 skipped 决策（reason=合并中断）——失败运行的决策面对全部
        可信候选同样全覆盖（与成功路径口径一致）。

        Args:
            session: 注入的异步 Session。
            gateway: LLM 网关（dream 角色）。
            persona_scope: 人格作用域 ID。
            selected: 过门候选（分数降序）。
            active_topics: 可参与合并的既有 active 策展记忆（冻结行已
                排除——prompt 主题列表数据面）。
            frozen_keys: 冻结主题键集（kind 值, topic）——提案目标命中
                即整条丢弃（merged_from 逐事件落 skipped/frozen 决策）。
            stats: 运行统计（failed 就地累加）。
            decisions: 决策列表（晋升 / 并入 / 未收录决策就地追加）。
            decided_ids: 已决策事件 id 集（就地更新——防重复决策）。

        Returns:
            提案丢弃计数（逐条校验失败 + merged_from 未知引用）。

        Raises:
            _DreamPhaseError: LLM 返回空 / 结构性解析失败（零晋升）。
            BusinessException: LLM 调用 / 晋升落库异常（归 run_dream 记录）。
        """
        try:
            return await self._merge_and_promote_inner(
                session,
                gateway=gateway,
                persona_scope=persona_scope,
                selected=selected,
                active_topics=active_topics,
                frozen_keys=frozen_keys,
                stats=stats,
                decisions=decisions,
                decided_ids=decided_ids,
            )
        except Exception:
            # 合并中断：过门但未决策的候选补 skipped 决策（决策面全覆盖）
            _backfill_undecided(selected, decided_ids, decisions, reason=_MERGE_INTERRUPT_REASON)
            raise

    async def _merge_and_promote_inner(
        self,
        session: AsyncSession,
        *,
        gateway: LlmGateway,
        persona_scope: str,
        selected: list[EpisodeRecordDTO],
        active_topics: list[CuratedRecordDTO],
        frozen_keys: frozenset[tuple[str, str]],
        stats: DreamRunStatsModel,
        decisions: list[DreamDecisionModel],
        decided_ids: set[int],
    ) -> int:
        """合并 + 晋升主体（异常上抛由 ``_merge_and_promote`` 补决策）。

        Args:
            session: 注入的异步 Session。
            gateway: LLM 网关（dream 角色）。
            persona_scope: 人格作用域 ID。
            selected: 过门候选（分数降序）。
            active_topics: 可参与合并的既有 active 策展记忆（冻结行已排除）。
            frozen_keys: 冻结主题键集（kind 值, topic）——提案目标命中即
                丢弃（deltas 文档化：merged_from 逐事件落 skipped 决策，
                理由含 frozen；不计入 failed——非校验失败）。
            stats: 运行统计（failed 就地累加）。
            decisions: 决策列表（晋升 / 并入 / 未收录决策就地追加）。
            decided_ids: 已决策事件 id 集（就地更新——防重复决策）。

        Returns:
            提案丢弃计数（逐条校验失败 + merged_from 未知引用）。

        Raises:
            _DreamPhaseError: LLM 返回空 / 结构性解析失败（零晋升）。
            BusinessException: LLM 调用 / 晋升落库异常（归 run_dream 记录）。
        """
        prompt = render_merge_prompt(selected, active_topics)
        response = await gateway.chat_by_role(
            ModelRole.DREAM,
            [ChatMessageModel(role=ChatMessageRole.SYSTEM, content=prompt)],
            persona_id=persona_scope,
            purpose=DREAM_PURPOSE,
        )
        raw = response.content.strip()
        if not raw:
            raise _DreamPhaseError("梦境合并模型返回空内容")
        valid_items, dropped_count = parse_merge_proposals(raw)
        selected_ids = {record.id for record in selected}
        actionable: list[DreamProposedItemModel] = []
        referenced_by_dropped: set[int] = set()
        for item in valid_items:
            unknown_ids = [eid for eid in item.merged_from if eid not in selected_ids]
            if unknown_ids:
                dropped_count += 1
                referenced_by_dropped.update(eid for eid in item.merged_from if eid in selected_ids)
                logger.warning(
                    "梦境提案引用未知事件，已丢弃: topic={}, unknown={}",
                    item.topic,
                    unknown_ids,
                )
                continue
            if (item.kind.value, item.topic) in frozen_keys:
                logger.info(
                    "梦境提案目标主题已冻结，丢弃: topic={}, kind={}",
                    item.topic,
                    item.kind.value,
                )
                for episode_id in item.merged_from:
                    if episode_id in decided_ids:
                        continue
                    decisions.append(
                        DreamDecisionModel(
                            episode_id=episode_id,
                            topic=item.topic,
                            kind=item.kind,
                            action=DreamDecisionAction.SKIPPED,
                            reason=SKIP_REASON_FROZEN,
                        )
                    )
                    decided_ids.add(episode_id)
                continue
            actionable.append(item)
        for item in actionable:
            record = await self._memory.upsert_curated(
                session,
                CuratedUpsertDTO(
                    kind=item.kind,
                    topic=item.topic,
                    content=item.content,
                    importance=item.importance,
                    provenance=MemoryProvenance.AGENT,
                ),
            )
            if record.version == 1:
                action = DreamDecisionAction.PROMOTED
                reason = "晋升为新策展主题（首版）"
            else:
                action = DreamDecisionAction.MERGED
                reason = f"并入既有主题链（新版本 v{record.version}）"
            for episode_id in item.merged_from:
                if episode_id in decided_ids:
                    continue  # 同事件已被其他提案决策——保留首条（防重复计数）
                decisions.append(
                    DreamDecisionModel(
                        episode_id=episode_id,
                        topic=item.topic,
                        kind=item.kind,
                        action=action,
                        reason=reason,
                    )
                )
                decided_ids.add(episode_id)
        # 过门但未参与任何晋升的事件：决策面全覆盖（可信候选每人一条）
        for candidate in selected:
            if candidate.id in decided_ids:
                continue
            reason = (
                "所引用提案因未知事件引用被丢弃"
                if candidate.id in referenced_by_dropped
                else "本次提案未收录该事件"
            )
            decisions.append(
                DreamDecisionModel(
                    episode_id=candidate.id,
                    action=DreamDecisionAction.SKIPPED,
                    reason=reason,
                )
            )
            decided_ids.add(candidate.id)
        return dropped_count

    async def _write_diary(
        self,
        session: AsyncSession,
        *,
        gateway: LlmGateway,
        persona_scope: str,
        persona_name: str,
        day_episodes: list[EpisodeRecordDTO],
        run_now: int,
        cfg: DreamJobConfig,
        row_id: int,
    ) -> int:
        """生成日记并落回情景层（返回日记事件主键）。

        Args:
            session: 注入的异步 Session。
            gateway: LLM 网关（dream 角色）。
            persona_scope: 人格作用域 ID（用量归属）。
            persona_name: 人格展示名（声线变量——经渲染进入 prompt）。
            day_episodes: 当日可信事件（时间升序；untrusted 已硬排除）。
            run_now: 注入时钟（日记事件 ts_ms）。
            cfg: 任务参数（diary_importance）。
            row_id: 运行主键（日记事件 metadata 回链）。

        Returns:
            日记事件主键（append_episode 落库结果）。

        Raises:
            _DreamPhaseError: 日记生成返回空（运行标记 failed——晋升保留）。
            BusinessException: LLM 调用 / 事件落库异常（归 run_dream 记录）。
        """
        prompt = render_diary_prompt(persona_name, day_episodes)
        response = await gateway.chat_by_role(
            ModelRole.DREAM,
            [ChatMessageModel(role=ChatMessageRole.SYSTEM, content=prompt)],
            persona_id=persona_scope,
            purpose=DREAM_PURPOSE,
        )
        diary_text = response.content.strip()
        if not diary_text:
            raise _DreamPhaseError("日记生成返回空内容")
        episode = await self._memory.append_episode(
            session,
            EpisodeCreateDTO(
                kind=EpisodeKind.DREAM,
                source=DIARY_SOURCE,
                content=EpisodeNotePayloadModel(text=diary_text, metadata={"dream_run_id": row_id}),
                importance=cfg.diary_importance,
                provenance=MemoryProvenance.AGENT,
                ts_ms=run_now,
            ),
        )
        return episode.id

    # ------------------------------------------------------------------
    # 生命周期归档阶段（纯规则——零模型调用）
    # ------------------------------------------------------------------

    async def _archive_pass(
        self,
        session: AsyncSession,
        *,
        run_now: int,
        active_topics: list[CuratedRecordDTO],
        stats: DreamRunStatsModel,
        decisions: list[DreamDecisionModel],
    ) -> int:
        """生命周期归档阶段（active 策展行按有效强度批量归档）。

        判定与执行分离：:func:`select_archive_candidates` 纯规则求出候选
        （非置顶 / 非冻结 / 现行 active / 年龄超阈值 / 强度低于阈值），
        再由 :meth:`MemoryService.archive_curated` 批量落库——归档执行点
        会重新校验 ``status='active'``（候选快照与执行之间被 supersede /
        已归档的行被跳过），落库结果为 :class:`ArchiveCuratedResultModel`
        的**实际归档集合**。

        决策面口径：只对**实际归档**的 id 产生 ``action=archived`` 决策
        （``episode_id=0`` / ``curated_id=行主键`` / 理由
        ``strength_below_threshold``）——``stats.archived`` 与 archived
        决策条数恒等（逐行可审计；候选快照中未实际归档的行既不计入统计、
        也不产生决策）。

        Args:
            session: 注入的异步 Session。
            run_now: 注入时钟（epoch 毫秒——强度判定参考轴）。
            active_topics: 既有 active 策展记忆（本轮取回面——候选快照；
                执行点以 ``archive_curated`` 的复核结果为准）。
            stats: 运行统计（archived 按实际归档数就地累加）。
            decisions: 决策列表（归档决策仅对实际归档 id 就地追加）。

        Returns:
            本次实际归档行数（0 = 无候选 / 候选在执行点均已非 active）。
        """
        candidates = select_archive_candidates(
            active_topics, now_ms=run_now, cfg=self._lifecycle_config
        )
        if not candidates:
            return 0
        result = await self._memory.archive_curated(
            session, [candidate.id for candidate in candidates]
        )
        stats.archived += result.count
        candidates_by_id = {candidate.id: candidate for candidate in candidates}
        for archived_id in result.archived_ids:
            candidate = candidates_by_id[archived_id]
            decisions.append(
                DreamDecisionModel(
                    episode_id=0,
                    topic=candidate.topic,
                    kind=candidate.kind,
                    action=DreamDecisionAction.ARCHIVED,
                    reason=ARCHIVE_REASON_STRENGTH,
                    curated_id=candidate.id,
                )
            )
        logger.info(
            "梦境生命周期归档完成: archived={}, candidates={}",
            result.count,
            len(candidates),
        )
        return result.count

    # ------------------------------------------------------------------
    # 运行日志读写面（API 消费）
    # ------------------------------------------------------------------

    async def list_runs(
        self, session: AsyncSession, query: DreamRunQueryDTO
    ) -> PageResponse[DreamRunRecordDTO]:
        """梦境运行游标分页列表（id 降序——新运行在前；人格作用域自动过滤）。

        Args:
            session: 注入的异步 Session。
            query: 查询 DTO（游标 + 页大小）。

        Returns:
            分页响应（``next_cursor`` 非空表示还有更早运行；total 恒 -1）。
        """
        stmt = select(DreamRunRow)
        cursor_payload = decode_cursor(query.cursor)
        cursor_id = cursor_payload.get("id") if cursor_payload is not None else None
        if isinstance(cursor_id, int):
            stmt = stmt.where(DreamRunRow.id < cursor_id)
        stmt = stmt.order_by(desc(DreamRunRow.id)).limit(query.limit + 1)
        rows = list((await session.execute(stmt)).scalars().all())
        has_more = len(rows) > query.limit
        page_rows = rows[: query.limit]
        next_cursor = encode_cursor({"id": page_rows[-1].id}) if has_more and page_rows else None
        return PageResponse.of_cursor(
            items=[DreamRunRecordDTO.model_validate(row) for row in page_rows],
            page_size=query.limit,
            next_cursor=next_cursor,
        )

    async def get_run(self, session: AsyncSession, run_id: int) -> DreamRunRecordDTO:
        """按主键读取运行记录（人格作用域外 / 缺失均 NOT_FOUND）。

        Args:
            session: 注入的异步 Session。
            run_id: 运行主键。

        Returns:
            运行记录 DTO（含全部决策明细）。

        Raises:
            BusinessException: 运行不存在或不在当前人格作用域（``NOT_FOUND``）。
        """
        row = (
            (await session.execute(select(DreamRunRow).where(DreamRunRow.id == run_id)))
            .scalars()
            .first()
        )
        if row is None:
            raise BusinessException(
                code=ErrorCode.NOT_FOUND,
                message="梦境运行记录不存在",
                details={"run_id": run_id},
            )
        return DreamRunRecordDTO.model_validate(row)

    # ------------------------------------------------------------------
    # 内部——既往运行 / 主题取回 / 相关度命中
    # ------------------------------------------------------------------

    async def _previous_promoted_ids(
        self, session: AsyncSession, *, run_now: int, cfg: DreamJobConfig
    ) -> frozenset[int]:
        """既往成功运行的已晋升事件 id 集（回溯窗内 promoted + merged 并集）。

        回溯窗：仅统计 ``finished_ms`` 落在 ``[run_now - cfg.promoted_lookback_ms,
        run_now]`` 内的成功运行（运行中 finished_ms 为 NULL——自然排除；
        窗外运行的晋升不再构成跳过依据，事件可被重新巩固）。

        Args:
            session: 注入的异步 Session（人格作用域自动过滤）。
            run_now: 注入时钟（epoch 毫秒——回溯窗参考轴）。
            cfg: 任务参数（promoted_lookback_ms 回溯窗宽度）。

        Returns:
            已晋升事件 id 集（空 = 窗内无既往晋升 / 首次运行）。
        """
        window_start = run_now - cfg.promoted_lookback_ms
        rows = list(
            (
                await session.execute(
                    select(DreamRunRow).where(
                        DreamRunRow.status == DreamRunStatus.SUCCESS.value,
                        DreamRunRow.finished_ms >= window_start,
                    )
                )
            )
            .scalars()
            .all()
        )
        ids: set[int] = set()
        for run_row in rows:
            for raw_decision in run_row.decisions:
                try:
                    decision = DreamDecisionModel.model_validate(raw_decision)
                except ValidationError:
                    logger.warning("既往运行决策解析失败（跳过该条）: run_id={}", run_row.id)
                    continue
                if decision.action in (DreamDecisionAction.PROMOTED, DreamDecisionAction.MERGED):
                    ids.add(decision.episode_id)
        return frozenset(ids)

    async def _active_topics(self, session: AsyncSession) -> list[CuratedRecordDTO]:
        """取既有 active 策展主题（id 升序——prompt 主题块稳定排列）。

        Args:
            session: 注入的异步 Session（人格作用域自动过滤）。

        Returns:
            active 策展记忆记录列表。
        """
        rows = list(
            (
                await session.execute(
                    select(CuratedMemoryRow)
                    .where(CuratedMemoryRow.status == CuratedStatus.ACTIVE.value)
                    .order_by(CuratedMemoryRow.id.asc())
                )
            )
            .scalars()
            .all()
        )
        return [CuratedRecordDTO.model_validate(row) for row in rows]

    async def _collect_topic_hits(
        self,
        store: MemoryStore,
        persona_scope: str,
        topics: Sequence[CuratedRecordDTO],
        cfg: DreamJobConfig,
    ) -> frozenset[int]:
        """主题相关度命中集（每个 active 主题 keyword 检索 top-N 的并集）。

        降级语义（文档化）：人格 ID 非数值（索引表面为 BigInt——与
        episode_to_index_entry 同规则）或单主题检索失败 → 该来源降级为
        无加成（不 fail 整个运行——相关度是增强轴，不是正确性轴）。

        Args:
            store: 记忆检索存储（keyword 模式——无向量依赖）。
            persona_scope: 人格作用域 ID（数值化进索引隔离谓词）。
            topics: active 策展主题。
            cfg: 任务参数（topic_relevance_limit = top-N）。

        Returns:
            命中事件 id 集（ref_type=episode 的检索结果并集）。
        """
        try:
            numeric_persona = int(persona_scope)
        except ValueError:
            logger.debug("人格 ID 非数值，主题相关度降级为无加成: persona={}", persona_scope)
            return frozenset()
        hits: set[int] = set()
        for topic in topics:
            query = MemorySearchQueryDTO(
                persona_id=numeric_persona,
                query=topic.topic,
                mode=MemorySearchMode.KEYWORD,
                limit=cfg.topic_relevance_limit,
            )
            try:
                results = await store.search(query, None)
            except Exception:  # best-effort: 单主题检索失败降级，不阻断运行
                logger.opt(exception=True).warning(
                    "主题相关度检索失败（该主题降级为无加成）: topic={}", topic.topic
                )
                continue
            hits.update(
                result.ref_id for result in results if result.ref_type == MemoryRefType.EPISODE
            )
        return frozenset(hits)

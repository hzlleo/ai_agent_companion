"""memory 域梦境整理编排——夜间记忆巩固任务（P1 chunk 3 交付）。

夜间梦境（dream）是记忆层的巩固任务：把窗口内的情景事件按规则门
筛选后交 LLM 蒸馏为策展记忆（supersede 语义），再以人格口吻生成
第一人称日记落回情景层——「白天积累、夜里整理」。

调用链（顺序锁定，文档化于 docs/backend/memory.md 夜间梦境一节）::

    DreamService.run_dream(db, gateway=..., persona_name=..., store=?, now_ms=?, cfg=?)
        ↓ ① DreamRunRow 落库（status=running——运行日志面，先落库可追溯）
        ↓ ② 取窗口事件 fetch_day_episodes（[now-window, now) 左闭右开）
        ↓ ③ 取既往成功运行的已晋升事件集 + 既有 active 策展主题
        ↓ ④ 主题相关度命中集（store 可选注入——keyword 检索 top-N，
          失败 / 未注入降级 boost=0）
        ↓ ⑤ 规则门 select_dream_candidates（零模型调用——纯函数）：
          untrusted 硬排除 → 既往已晋升跳过 → 分数门 → 分数降序截断
        ↓ ⑥ LLM 合并（ModelRole.DREAM，purpose="dream"——人格中立 prompt）：
          严格 JSON 数组提案 → 解析（剥代码围栏 + 逐条 Model Class 校验）
        ↓ ⑦ 逐条提案复核（merged_from ⊆ 候选 id——未知引用丢弃计数）→
          upsert_curated（provenance=agent——supersede 语义自动推进链）
        ↓ ⑧ 日记（dream 角色）：当日可信事件 → 第一人称日记 →
          append_episode（kind=dream / source="dream:diary" / provenance=agent）
        ↓ ⑨ DreamRunRow 终态落库（status/error/stats/decisions）→ 返回记录 DTO

fail-closed 语义（锁定决策，逐条）：

    - **untrusted 永不晋升**：规则门在任何打分 / prompt 组装之前硬排除
      （untrusted 既不产生决策、也绝不进入合并 / 日记 prompt）；晋升
      统一经 ``upsert_curated(provenance=agent)``——服务层对 untrusted
      的拒绝构成双保险；
    - **解析失败零晋升**：LLM 输出不可解析为 JSON 数组 → 运行标记
      failed，**零 upsert**（解析先于全部晋升——结构性失败不存在部分
      晋升态）；逐条提案校验失败（字段越界 / merged_from 未知引用）→
      该条丢弃计数（stats.failed），其余提案正常晋升；
    - **日记后置于晋升**（文档化排序决策）：晋升全部提交后才生成日记
      ——日记失败标记运行 failed 但**保留已提交的晋升并在 decisions
      中如实记录**（晋升是本任务的主产物、日记是叙事附产物；倒序会让
      日记失败回滚掉已有效的记忆巩固，代价更高）；
    - **运行失败不抛异常**：run_dream 返回终态记录 DTO（status=failed +
      error 原因）——手工触发面（admin API）以运行日志为产物，失败是
      业务结果而非请求失败；仅人格上下文缺失 / 运行行落库失败在入口
      fail loud 上抛。

纯函数面（精确数学可测）：:func:`recency_decay` / :func:`dream_score`
/ :func:`select_dream_candidates` / :func:`parse_merge_proposals`——
无 I/O、无模型调用；编排（本模块 DreamService）负责取数与落库。

构造级旋钮：:class:`DreamJobConfig`（窗口 / 半衰期 / 分数门 / 上限 /
相关度加成 / 日记开关——构造注入，与 llm 网关 retry 旋钮同模式；
config 域 env 面迁移属后续 chunk）。
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from loguru import logger
from pydantic import ValidationError
from sqlalchemy import desc, select
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
    DreamJobConfig,
    DreamProposedItemModel,
    DreamRunQueryDTO,
    DreamRunRecordDTO,
    DreamRunStatsModel,
    DreamRunStatus,
)
from packages.harness.memory.dreaming_orm import DreamRunRow
from packages.harness.memory.dreaming_prompts import render_diary_prompt, render_merge_prompt
from packages.harness.memory.dtos import (
    CuratedRecordDTO,
    CuratedUpsertDTO,
    EpisodeCreateDTO,
    EpisodeRecordDTO,
)
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

__all__ = [
    "MS_PER_DAY",
    "MS_PER_HOUR",
    "DREAM_PURPOSE",
    "DIARY_SOURCE",
    "recency_decay",
    "dream_score",
    "select_dream_candidates",
    "parse_merge_proposals",
    "DreamService",
]

#: 一天的毫秒数（epoch 毫秒轴的换算基准）。
MS_PER_DAY: int = 86_400_000
#: 一小时的毫秒数（窗口宽度换算）。
MS_PER_HOUR: int = 3_600_000
#: 梦境链路的用量 purpose（usage 域枚举既有成员——合并 + 日记共用）。
DREAM_PURPOSE: str = "dream"
#: 日记事件的来源标识（情景层 kind=dream 行的 source）。
DIARY_SOURCE: str = "dream:diary"


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


# =========================================================================== []
# 编排面——DreamService（取数 / LLM 调用 / 落库；无状态，Session 注入）
# =========================================================================== []


class DreamService:
    """梦境整理编排服务（run_dream 一次运行 + 运行日志读写面）。

    人格作用域契约：与 memory 域其余服务一致——**调用方须先经
    ``persona.bind_persona_context`` 绑定人格上下文**（PersonaMixin 自动
    注入 / 自动过滤；缺上下文在运行行落库前即 fail loud）。

    构造注入：``memory_service`` 可替换（默认新实例——无状态共享安全）；
    网关 / 存储 / 时钟 / 参数逐次调用注入（run_dream 关键字参数）。
    """

    def __init__(self, *, memory_service: MemoryService | None = None) -> None:
        """初始化服务。

        Args:
            memory_service: 记忆域服务（None = 新实例——日记追加 / 策展
                晋升 / 窗口取回的复用面）。
        """
        self._memory = memory_service if memory_service is not None else MemoryService()

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

        链路与 fail-closed 语义见模块 docstring。关键排序决策：**晋升
        全部提交后才生成日记**——日记失败标记运行 failed 但保留已提交
        晋升（decisions 如实记录）。

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
        """执行取数 → 门控 → 合并 → 晋升 → 日记各阶段（异常上抛归 run_dream）。

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
        trusted = [record for record in episodes if record.provenance != MemoryProvenance.UNTRUSTED]
        already_promoted = await self._previous_promoted_ids(session)
        active_topics = await self._active_topics(session)
        topic_hits = (
            await self._collect_topic_hits(store, persona_scope, active_topics, cfg)
            if store is not None and trusted and active_topics
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
            stats.failed += await self._merge_and_promote(
                session,
                gateway=gateway,
                persona_scope=persona_scope,
                selected=selected,
                active_topics=active_topics,
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

    async def _merge_and_promote(
        self,
        session: AsyncSession,
        *,
        gateway: LlmGateway,
        persona_scope: str,
        selected: list[EpisodeRecordDTO],
        active_topics: list[CuratedRecordDTO],
        stats: DreamRunStatsModel,
        decisions: list[DreamDecisionModel],
        decided_ids: set[int],
    ) -> int:
        """LLM 合并 + 逐条复核 + 晋升（返回提案丢弃计数）。

        Args:
            session: 注入的异步 Session。
            gateway: LLM 网关（dream 角色）。
            persona_scope: 人格作用域 ID。
            selected: 过门候选（分数降序）。
            active_topics: 既有 active 策展记忆。
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

    async def _previous_promoted_ids(self, session: AsyncSession) -> frozenset[int]:
        """既往成功运行的已晋升事件 id 集（promoted + merged 决策并集）。

        Args:
            session: 注入的异步 Session（人格作用域自动过滤）。

        Returns:
            已晋升事件 id 集（空 = 首次运行 / 无既往晋升）。
        """
        rows = list(
            (
                await session.execute(
                    select(DreamRunRow).where(DreamRunRow.status == DreamRunStatus.SUCCESS.value)
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

"""harness/duty 域服务——DutyService（章程驱动的值班工具循环 + 报告路由）。

调用链（单一入口 :meth:`DutyService.run_duty_round`，顺序锁定，文档化于
docs/backend/duty.md）::

    run_duty_round(session, cfg=?, now_ms=?)
        ↓ ① 激活人格解析（get_active_persona——无激活 → error 报告返回，
        ↓     不落库不投递，恒不抛）
        ↓ ② bind_persona_context(record.id)（PersonaMixin 作用域 + 工具
        ↓     persona contextvar + 网关用量归属三合一）
        ↓ ③ 消息组装：值班任务书（charter——人格名替换 / 覆写感知）+
        ↓     工具清单帧 → system 首块
        ↓ ④ 工具循环（≤ max_tool_steps 轮）：
        ↓     gateway.chat_by_role(DUTY, messages, tools=registry.list_specs(),
        ↓       purpose="duty")
        ↓       ├─ tool_calls 非空 → assistant 轮回放 + 逐调用
        ↓       │   registry.execute（异常隔离 + 计时）→ tool 轮反馈追加
        ↓       └─ tool_calls 空 → content 即最终报告，循环收束
        ↓ ⑤ 步数上限触发 → 追加收尾指令（system）→ 无工具面最终调用
        ↓ ⑥ 情景层落库：kind=duty / source=duty:report / provenance=agent /
        ↓     载荷 DutyReportPayloadModel（title / summary=最终报告 /
        ↓     details=步骤与终态结构化投影）
        ↓ ⑦ 属主投递（可选）：ChannelRouter 已注册的 Telegram 适配器 →
        ↓     owner_chat_id（失败 best-effort——仅日志，不影响轮次状态）

fail-closed 语义（锁定决策，逐条）：

    - **恒不上抛**：网关 / 工具风暴等任何异常 → 终态 status=error +
      中止文案报告 + error 事件落库（人格已绑定即可落库）；调度器只见
      业务结果，不见异常；
    - **报告先落库后投递**（排序决策）：情景层是报告的第一产物与真源
      （落库失败仅日志——轮次结果仍返回）；属主投递是 best-effort 附产物
      （失败不回滚轮次状态，投递终态经 DutyReportModel.owner_delivery
      观测）；
    - **空最终报告 = failed**：轮次完成但模型未产出可用报告 → 落
      failed 事件（业务失败，非异常）；
    - **无激活人格 = error 且零副作用**：无 PersonaMixin 作用域可落库
      ——返回 error 报告（不落库 / 不投递），日志可观测。

人格声线：任务书模板经人格展示名替换（与 chat 域 prompt 组装同语义——
``{name}`` 替换；覆写经 ``DutyRunConfigModel.charter_text`` 整体替换）。
"""

from __future__ import annotations

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from packages.harness.channels.channel_models import ChannelType, OutboundMessageModel
from packages.harness.channels.channel_service import ChannelAdapterProtocol, ChannelRouter
from packages.harness.config.model_entry import ModelRole
from packages.harness.core import time_utils
from packages.harness.duty.duty_models import (
    DutyOwnerDelivery,
    DutyReportModel,
    DutyRunConfigModel,
    DutyStatus,
    DutyStepModel,
)
from packages.harness.duty.duty_prompts import (
    SECTION_SEPARATOR,
    render_duty_charter,
    render_final_report_instruction,
    render_tool_list,
    render_tool_result,
)
from packages.harness.llm.gateway import LlmGateway
from packages.harness.llm.llm_models import (
    ChatMessageModel,
    ChatMessageRole,
)
from packages.harness.memory.dtos import EpisodeCreateDTO
from packages.harness.memory.models import (
    DutyReportPayloadModel,
    EpisodeKind,
    MemoryProvenance,
)
from packages.harness.memory.service import MemoryService
from packages.harness.persona.service import PersonaService, bind_persona_context
from packages.harness.tools.registry import ToolRegistry
from packages.harness.tools.tool_models import ToolCallRequestModel

__all__ = ["DUTY_PURPOSE", "DUTY_SOURCE", "DutyService"]

#: 值班链路的用量 purpose（usage 域枚举既有成员——工具循环 + 收尾共用）。
DUTY_PURPOSE: str = "duty"

#: 值班报告事件的来源标识（情景层 kind=duty 行的 source）。
DUTY_SOURCE: str = "duty:report"

#: 值班报告事件的默认重要度（1-10 轴——例行报告居中）。
_DUTY_EPISODE_IMPORTANCE: int = 5


def _telegram_send_face(router: ChannelRouter) -> ChannelAdapterProtocol | None:
    """从通道路由器解析 Telegram 适配器发送面。

    经 ``ChannelRouter.get_adapter`` 公共读取面获取（duty 出站与通道
    入站共用同一注册实例；未注册返回 None = 无通道路径）。

    Args:
        router: 通道路由器（app.state.channels_router 装配实例）。

    Returns:
        Telegram 适配器发送面；未注册返回 None。
    """
    return router.get_adapter(ChannelType.TELEGRAM)


class DutyService:
    """值班轮编排服务（章程 + 工具循环 + 报告路由；run_duty_round 恒不抛）。

    构造注入（测试 / 装配友好）：网关 / 工具注册表 / 记忆服务 / 人格
    服务 / 通道路由器与属主会话均可显式注入；``memory_service`` /
    ``persona_service`` 缺省新实例（无状态共享安全）。
    """

    def __init__(
        self,
        *,
        gateway: LlmGateway,
        registry: ToolRegistry,
        memory_service: MemoryService | None = None,
        persona_service: PersonaService | None = None,
        channel_router: ChannelRouter | None = None,
        owner_chat_id: str | None = None,
        report_to_owner: bool = True,
    ) -> None:
        """初始化服务。

        Args:
            gateway: LLM 网关（duty 角色路由——工具循环 + 收尾共用）。
            registry: 工具注册表（list_specs 进请求 / execute 隔离执行）。
            memory_service: 记忆域服务（值班报告事件落库；None = 新实例）。
            persona_service: 人格注册表服务（激活人格解析；None = 新实例）。
            channel_router: 通道路由器（None = 无通道——报告仅落记忆）。
            owner_chat_id: 属主会话 ID（None = 未配置——报告仅落记忆）。
            report_to_owner: 属主投递开关（False = 关闭——报告仅落记忆）。
        """
        self._gateway = gateway
        self._registry = registry
        self._memory = memory_service if memory_service is not None else MemoryService()
        self._persona = persona_service if persona_service is not None else PersonaService()
        self._channel_router = channel_router
        self._owner_chat_id = owner_chat_id
        self._report_to_owner = report_to_owner

    # ------------------------------------------------------------------
    # 值班轮主链路
    # ------------------------------------------------------------------

    async def run_duty_round(
        self,
        session: AsyncSession,
        *,
        now_ms: int | None = None,
        cfg: DutyRunConfigModel | None = None,
    ) -> DutyReportModel:
        """执行一次值班轮（fail-closed——恒不抛异常，返回终态报告）。

        链路与排序决策见模块 docstring；人格上下文在轮内绑定（工具
        persona contextvar / PersonaMixin 落库作用域 / 网关用量归属
        三合一）。

        Args:
            session: 注入的异步 Session（人格解析 + 报告落库共用）。
            now_ms: 注入时钟（epoch 毫秒；None = 当前真实时间）。
            cfg: 运行参数（None = DutyRunConfigModel 全默认——装配面
                经 Settings.duty 映射注入）。

        Returns:
            值班轮报告（终态——success / failed / error；调度器消费面）。
        """
        resolved_cfg = cfg if cfg is not None else DutyRunConfigModel()
        started_ms = now_ms if now_ms is not None else time_utils.now_ms()
        persona_record = await self._persona.get_active_persona(session)
        if persona_record is None:
            logger.warning("值班轮跳过：无激活人格（error 报告返回，零副作用）")
            return DutyReportModel(
                persona="",
                started_ts_ms=started_ms,
                finished_ts_ms=time_utils.now_ms(),
                status=DutyStatus.ERROR,
                error="无激活人格——本轮值班未执行",
                final_report="",
            )
        async with bind_persona_context(persona_record.id):
            return await self._round(
                session,
                persona_id=str(persona_record.id),
                persona_name=persona_record.display_name,
                started_ms=started_ms,
                cfg=resolved_cfg,
            )

    # ------------------------------------------------------------------
    # 内部——轮次主体（人格上下文已绑定）
    # ------------------------------------------------------------------

    async def _round(
        self,
        session: AsyncSession,
        *,
        persona_id: str,
        persona_name: str,
        started_ms: int,
        cfg: DutyRunConfigModel,
    ) -> DutyReportModel:
        """执行轮次主体（工具循环 → 落库 → 投递；异常全部收敛为终态）。

        Args:
            session: 注入的异步 Session（报告落库面）。
            persona_id: 人格作用域 ID（字符串形态——网关用量归属）。
            persona_name: 人格展示名（声线变量 + 报告标题）。
            started_ms: 轮次发起时间（epoch 毫秒）。
            cfg: 运行参数。

        Returns:
            值班轮报告（终态——恒不抛异常）。
        """
        steps: list[DutyStepModel] = []
        final_report = ""
        error_message: str | None = None
        stormed = False
        try:
            final_report = await self._tool_loop(
                persona_id=persona_id,
                persona_name=persona_name,
                steps=steps,
                cfg=cfg,
            )
        except Exception as exc:  # fail-closed：任何风暴收敛为 error 终态
            logger.opt(exception=True).error("值班轮异常中止: persona={}", persona_id)
            stormed = True
            error_message = f"值班轮异常中止: {exc}"
        if error_message is None and not final_report:
            error_message = "最终报告为空"
        if stormed:
            status = DutyStatus.ERROR
        elif error_message is not None:
            status = DutyStatus.FAILED
        else:
            status = DutyStatus.SUCCESS
        finished_ms = time_utils.now_ms()
        report_text = final_report if final_report else (error_message or "")
        report = DutyReportModel(
            persona=persona_id,
            persona_name=persona_name,
            started_ts_ms=started_ms,
            finished_ts_ms=finished_ms,
            steps=steps,
            final_report=report_text,
            status=status,
            error=error_message,
        )
        report.owner_delivery = await self._persist_and_deliver(session, report=report, cfg=cfg)
        return report

    async def _tool_loop(
        self,
        *,
        persona_id: str,
        persona_name: str,
        steps: list[DutyStepModel],
        cfg: DutyRunConfigModel,
    ) -> str:
        """章程 + 工具循环（返回最终报告文本；异常上抛归 _round 收敛）。

        Args:
            persona_id: 人格作用域 ID（网关用量归属）。
            persona_name: 人格展示名（声线变量）。
            steps: 步骤条目列表（就地追加——调用 / 结果对）。
            cfg: 运行参数（步数上限 / 任务书覆写）。

        Returns:
            最终报告文本（空串 = 模型未产出——调用方归 failed）。

        Raises:
            Exception: 网关 / 工具执行链路异常（归 _round fail-closed 收敛；
                registry.execute 自身恒不抛——此处异常来自网关调用）。
        """
        system_prompt = SECTION_SEPARATOR.join(
            [
                render_duty_charter(
                    persona_name, cfg.max_tool_steps, charter_override=cfg.charter_text
                ),
                render_tool_list(self._registry.list_specs()),
            ]
        )
        messages: list[ChatMessageModel] = [
            ChatMessageModel(role=ChatMessageRole.SYSTEM, content=system_prompt)
        ]
        for _ in range(cfg.max_tool_steps):
            response = await self._gateway.chat_by_role(
                ModelRole.DUTY,
                messages,
                tools=self._registry.list_specs(),
                persona_id=persona_id,
                purpose=DUTY_PURPOSE,
            )
            if not response.tool_calls:
                return response.content.strip()
            messages.append(
                ChatMessageModel(
                    role=ChatMessageRole.ASSISTANT,
                    content=response.content,
                    tool_calls=response.tool_calls,
                )
            )
            for call in response.tool_calls:
                result = await self._registry.execute(call.name, call.arguments)
                steps.append(
                    DutyStepModel(
                        call=ToolCallRequestModel(name=call.name, arguments=call.arguments),
                        result=result,
                    )
                )
                messages.append(
                    ChatMessageModel(
                        role=ChatMessageRole.TOOL,
                        content=render_tool_result(result),
                        tool_call_id=call.id,
                    )
                )
        # 步数上限触发：收尾指令 + 无工具面最终调用
        messages.append(
            ChatMessageModel(
                role=ChatMessageRole.SYSTEM,
                content=render_final_report_instruction(persona_name),
            )
        )
        response = await self._gateway.chat_by_role(
            ModelRole.DUTY,
            messages,
            persona_id=persona_id,
            purpose=DUTY_PURPOSE,
        )
        return response.content.strip()

    # ------------------------------------------------------------------
    # 内部——落库与属主投递
    # ------------------------------------------------------------------

    async def _persist_and_deliver(
        self,
        session: AsyncSession,
        *,
        report: DutyReportModel,
        cfg: DutyRunConfigModel,
    ) -> DutyOwnerDelivery:
        """报告落库（第一产物）→ 属主投递（best-effort 附产物）。

        Args:
            session: 注入的异步 Session（人格作用域已绑定）。
            report: 轮次报告（落库后投递；投递终态回填返回）。
            cfg: 运行参数（steps 投影已含在 report 内——cfg 仅日志面）。

        Returns:
            属主投递终态（sent / failed / skipped_* / disabled）。
        """
        title = f"值班日报 · {report.persona_name}" if report.persona_name else "值班日报"
        details = {
            "status": report.status.value,
            "started_ts_ms": report.started_ts_ms,
            "finished_ts_ms": report.finished_ts_ms,
            "tool_step_count": len(report.steps),
            "steps": [step.model_dump() for step in report.steps],
            "error": report.error,
        }
        try:
            await self._memory.append_episode(
                session,
                EpisodeCreateDTO(
                    kind=EpisodeKind.DUTY,
                    source=DUTY_SOURCE,
                    content=DutyReportPayloadModel(
                        title=title,
                        summary=report.final_report,
                        details=details,
                    ),
                    importance=_DUTY_EPISODE_IMPORTANCE,
                    provenance=MemoryProvenance.AGENT,
                    ts_ms=report.finished_ts_ms,
                ),
            )
        except Exception:  # best-effort：落库失败不阻断轮次返回（fail-closed）
            logger.opt(exception=True).error(
                "值班报告事件落库失败（已吞没——轮次结果仍返回）: persona={}", report.persona
            )
        return await self._deliver_owner_report(report.final_report)

    async def _deliver_owner_report(self, text: str) -> DutyOwnerDelivery:
        """属主投递（ChannelRouter Telegram 适配器发送面；best-effort）。

        Args:
            text: 报告文本（最终报告或中止文案）。

        Returns:
            投递终态（观测轴——失败不影响轮次状态）。
        """
        if not self._report_to_owner:
            return DutyOwnerDelivery.DISABLED
        if self._channel_router is None:
            logger.info("值班报告仅落记忆：通道路由器未装配（无通道）")
            return DutyOwnerDelivery.SKIPPED_NO_CHANNEL
        if not self._owner_chat_id:
            logger.info("值班报告仅落记忆：属主会话 ID 未配置")
            return DutyOwnerDelivery.SKIPPED_NO_OWNER
        adapter = _telegram_send_face(self._channel_router)
        if adapter is None:
            logger.info("值班报告仅落记忆：Telegram 适配器未注册（无通道）")
            return DutyOwnerDelivery.SKIPPED_NO_CHANNEL
        try:
            await adapter.send_message(OutboundMessageModel(chat_id=self._owner_chat_id, text=text))
        except Exception:  # best-effort：投递失败仅日志（报告已落记忆）
            logger.opt(exception=True).warning(
                "值班报告属主投递失败（报告已落记忆）: owner_chat_id={}",
                self._owner_chat_id,
            )
            return DutyOwnerDelivery.FAILED
        return DutyOwnerDelivery.SENT

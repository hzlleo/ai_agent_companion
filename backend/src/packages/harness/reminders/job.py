"""reminders 域调度作业——到期扫描 + 属主通道投递（中央调度器间隔作业）。

作业链（``scheduler.register_interval`` 消费的零参协程工厂）::

    build_reminder_scan_job(...) → 作业协程
        ↓ ① 主人格解析（get_primary_persona——无主人格 → 日志跳过，正常返回）
        ↓ ② bind_persona_context(record.id)（扫描在单一人格作用域内进行）
        ↓ ③ scan_due_reminders（到期快照——事务结束即释放行锁）
        ↓ ④ 发送面解析（通道路由器 Telegram 适配器——与 duty 属主投递
        ↓     同一解析面；未装配 / 未注册 → **保持 pending**，下轮重试）
        ↓ ⑤ 逐条投递 ``【提醒】{title}``：
        ↓     ├─ 成功 → 新事务 mark_delivered（delivered_at_ms = 注入时钟）
        ↓     └─ 异常 → 新事务 mark_failed（attempts++；达上限转 failed）

投递语义（锁定决策，文档化于 docs/backend/reminders.md）：

    - **无通道 = 保持 pending**：路由器未装配 / Telegram 适配器未注册 /
      属主会话未配置时不消耗失败次数——通道恢复后下一轮扫描补投；
    - **至少一次**：投递成功与终态回写之间存在进程崩溃窗口——行保持
      pending，下轮重投（重复投递优于漏投，文档化代价）；
    - **失败不计入通道缺失**：仅 ``send_message`` 异常计入 attempts；
    - 作业异常面由调度器 per-job 隔离兜底（人格解析 / 扫描链路残余异常）。

装配开关（app/main.py）：作业注册条件 = Telegram 通道启用且属主会话已
配置——无投递出口时注册一个空转作业没有观测价值；提醒行保持 pending
（管理面可查询 / 取消），通道启用后作业随调度器装配自然生效。
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from contextlib import AbstractAsyncContextManager
from typing import Any

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from packages.harness.channels.channel_models import ChannelType, OutboundMessageModel
from packages.harness.channels.channel_service import ChannelRouter
from packages.harness.core.errors import BusinessException, ErrorCode
from packages.harness.core.time_utils import now_ms as default_now_ms
from packages.harness.persona.service import PersonaService, bind_persona_context
from packages.harness.reminders.models import OutboundSenderFace, ReminderRecordDTO
from packages.harness.reminders.service import ReminderService

__all__ = [
    "REMINDER_SCAN_INTERVAL_SECONDS",
    "REMINDER_MESSAGE_TEMPLATE",
    "build_reminder_scan_job",
]

#: 扫描作业间隔秒（锁定 60——分钟级提醒精度；调度器注册参数，非 env 键）。
REMINDER_SCAN_INTERVAL_SECONDS: float = 60.0

#: 提醒投递文案模板（``{title}`` 为提醒标题——中文「提醒」前缀标识来源）。
REMINDER_MESSAGE_TEMPLATE: str = "【提醒】{title}"


def build_reminder_scan_job(
    *,
    reminder_service: ReminderService,
    persona_service: PersonaService,
    channel_router: ChannelRouter | None,
    owner_chat_id: str | None,
    session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]],
    clock: Callable[[], int] = default_now_ms,
) -> Callable[[], Coroutine[Any, Any, None]]:
    """构建提醒扫描作业工厂（调度器 ``register_interval`` 消费）。

    Args:
        reminder_service: 提醒服务（扫描 / 终态回写）。
        persona_service: 人格注册表服务（主人格解析——扫描作用域）。
        channel_router: 通道路由器（None = 无通道——提醒保持 pending）。
        owner_chat_id: 属主会话 ID（None = 未配置——提醒保持 pending）。
        session_factory: 数据库会话工厂（每轮作业 / 每次回写开新会话）。
        clock: 注入时钟（epoch 毫秒——测试确定性驱动）。

    Returns:
        零参协程工厂（作业体；异常由调度器 per-job 隔离兜底）。
    """

    async def _reminder_scan_job() -> None:
        """单轮扫描作业（无主人格 / 无通道为正常跳过——日志面）。"""
        async with session_factory() as session:
            try:
                persona = await persona_service.get_primary_persona(session)
            except BusinessException as exc:
                if exc.code != ErrorCode.NOT_FOUND:
                    raise
                logger.info("提醒扫描跳过：无主人格")
                return
            persona_id = persona.id
            async with bind_persona_context(persona_id):
                scan = await reminder_service.scan_due_reminders(session, now_ms=clock())
        if scan.scanned == 0:
            return
        adapter = (
            channel_router.get_adapter(ChannelType.TELEGRAM)
            if channel_router is not None
            else None
        )
        if adapter is None or not owner_chat_id:
            logger.info(
                "提醒扫描命中 {} 条但无投递出口（保持 pending，下轮重试）: persona={}",
                scan.scanned,
                persona_id,
            )
            return
        for reminder in scan.due:
            await _deliver(
                reminder,
                adapter=adapter,
                owner_chat_id=owner_chat_id,
                reminder_service=reminder_service,
                session_factory=session_factory,
                persona_id=persona_id,
                delivered_at_ms=clock(),
            )

    async def _deliver(
        reminder: ReminderRecordDTO,
        *,
        adapter: OutboundSenderFace,
        owner_chat_id: str,
        reminder_service: ReminderService,
        session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]],
        persona_id: int,
        delivered_at_ms: int,
    ) -> None:
        """投递单条提醒并回写终态（异常收敛为 mark_failed——不上抛）。

        Args:
            reminder: 到期提醒快照。
            adapter: 通道适配器发送面（Telegram——``send_message`` 契约
                的结构化消费面）。
            owner_chat_id: 属主会话 ID。
            reminder_service: 提醒服务（终态回写）。
            session_factory: 数据库会话工厂（回写短事务）。
            persona_id: 人格作用域 ID（回写事务绑定）。
            delivered_at_ms: 投递成功时刻（epoch 毫秒——注入时钟）。
        """
        try:
            await adapter.send_message(
                OutboundMessageModel(
                    chat_id=owner_chat_id,
                    text=REMINDER_MESSAGE_TEMPLATE.format(title=reminder.title),
                )
            )
        except Exception as exc:
            logger.opt(exception=True).warning("提醒投递失败: id={}", reminder.id)
            async with session_factory() as session, bind_persona_context(persona_id):
                await reminder_service.mark_failed(session, reminder.id, error=str(exc))
            return
        async with session_factory() as session, bind_persona_context(persona_id):
            await reminder_service.mark_delivered(
                session, [reminder.id], delivered_at_ms=delivered_at_ms
            )
        logger.info("提醒已投递: id={}, owner_chat_id={}", reminder.id, owner_chat_id)

    return _reminder_scan_job

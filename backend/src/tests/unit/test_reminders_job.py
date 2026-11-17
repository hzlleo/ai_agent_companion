"""harness/reminders 调度作业测试——到期扫描 + 属主投递 + 终态回写。

测试策略：共享 SQLite fixture 安装到 engine holder（真实 ``session_scope``
事务面——扫描 / 回写走真实会话）；真实 :class:`ChannelRouter` + fake
适配器发送面（零真实网络）；注入时钟（``clock`` 恒返回固定 epoch 毫秒）
与注入会话工厂——全链路确定性驱动。

覆盖面：到期投递成功（文案 + delivered 回写）/ 无适配器保持 pending /
属主会话未配置保持 pending / 发送异常失败计数（达上限转 failed）/
无主人格正常跳过（零副作用）。
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

import packages.harness.persona.orm as persona_orm  # noqa: F401  模型注册副作用导入（共享建表）
import packages.harness.reminders.orm as reminders_orm  # noqa: F401  模型注册副作用导入（共享建表）
from packages.harness.channels.channel_models import (
    ChannelDeliveryResultModel,
    ChannelType,
    OutboundMessageModel,
)
from packages.harness.channels.channel_service import ChannelRouter, ChatServiceFactory
from packages.harness.chat.service import ChatService
from packages.harness.database.engine import db_engine_holder
from packages.harness.database.session import session_scope
from packages.harness.llm.gateway import LlmGateway
from packages.harness.persona.dtos import PersonaCreateDTO
from packages.harness.persona.service import PersonaService, bind_persona_context
from packages.harness.reminders.job import build_reminder_scan_job
from packages.harness.reminders.models import (
    ReminderCreateModel,
    ReminderRecordDTO,
    ReminderStatus,
)
from packages.harness.reminders.service import DELIVERY_FAILURE_LIMIT, ReminderService

#: 注入时钟读数（epoch 毫秒——测试确定性轴）。
NOW_MS = 1_000

#: 属主会话 ID（投递目标）。
OWNER_CHAT_ID = "owner-chat-1"


class FakeTelegramAdapter:
    """fake Telegram 适配器发送面（记录投递文本；可注入发送异常）。"""

    def __init__(self, *, fail: bool = False) -> None:
        """初始化（fail=True 时 ``send_message`` 抛异常）。"""
        self.fail = fail
        self.sent: list[OutboundMessageModel] = []

    async def send_message(self, message: OutboundMessageModel) -> ChannelDeliveryResultModel:
        """记录出站消息（fail 注入时抛异常模拟平台失败）。"""
        if self.fail:
            raise RuntimeError("平台发送失败（测试注入）")
        self.sent.append(message)
        return ChannelDeliveryResultModel(
            channel=ChannelType.TELEGRAM, chat_id=message.chat_id, message_ids=["1"]
        )


def _noop_chat_service_factory() -> ChatService:
    """路由器构造占位工厂（作业路径不触 ChatService——真实类零调用）。"""
    return ChatService(gateway=LlmGateway())


@pytest.fixture(autouse=True)
async def _install_db_holder(shared_db_engine: AsyncEngine) -> AsyncIterator[None]:
    """把共享 SQLite 引擎安装到 engine holder（真实 session_scope 可用）。"""
    db_engine_holder.async_engine = shared_db_engine
    db_engine_holder.session_factory = async_sessionmaker(
        bind=shared_db_engine, expire_on_commit=False
    )
    db_engine_holder.owner_token = None
    yield
    db_engine_holder.reset()


async def _create_primary_persona(db_session: AsyncSession) -> int:
    """创建并设为主人格（扫描作业作用域来源）。"""
    service = PersonaService()
    created = await service.create(
        db_session,
        PersonaCreateDTO.model_validate(
            {
                "name": "rem-job-primary",
                "status": "active",
                "card": {"name": "大肥鱼", "first_mes": "你回来啦！"},
            }
        ),
    )
    await service.set_primary(db_session, created.id)
    return created.id


async def _seed_reminder(db_session: AsyncSession, persona_id: int) -> int:
    """铺底一条到期待投递提醒（返回主键）。"""
    async with bind_persona_context(persona_id):
        record = await ReminderService().create(
            db_session,
            ReminderCreateModel(title="喝水", due_at_ms=NOW_MS - 1),
        )
    return record.id


async def _fetch(
    db_session: AsyncSession, persona_id: int, reminder_id: int
) -> ReminderRecordDTO:
    """按人格作用域读取提醒行终态。"""
    async with bind_persona_context(persona_id):
        return await ReminderService().get(db_session, reminder_id)


def _router(adapter: FakeTelegramAdapter | None) -> ChannelRouter:
    """构造真实路由器（可选注册 fake Telegram 适配器）。"""
    factory: ChatServiceFactory = _noop_chat_service_factory
    router = ChannelRouter(chat_service_factory=factory)
    if adapter is not None:
        router.register_adapter(ChannelType.TELEGRAM, adapter)
    return router


async def test_scan_job_delivers_and_marks_delivered(db_session: AsyncSession) -> None:
    """到期提醒：投递文案【提醒】{title} + delivered 回写（注入时钟）。"""
    persona_id = await _create_primary_persona(db_session)
    reminder_id = await _seed_reminder(db_session, persona_id)
    adapter = FakeTelegramAdapter()
    job = build_reminder_scan_job(
        reminder_service=ReminderService(),
        persona_service=PersonaService(),
        channel_router=_router(adapter),
        owner_chat_id=OWNER_CHAT_ID,
        session_factory=session_scope,
        clock=lambda: NOW_MS,
    )

    await job()

    assert len(adapter.sent) == 1
    assert adapter.sent[0].chat_id == OWNER_CHAT_ID
    assert adapter.sent[0].text == "【提醒】喝水"
    record = await _fetch(db_session, persona_id, reminder_id)
    assert record.status is ReminderStatus.DELIVERED
    assert record.delivered_at_ms == NOW_MS


async def test_scan_job_without_adapter_leaves_pending(db_session: AsyncSession) -> None:
    """无 Telegram 适配器：提醒保持 pending（不消耗失败次数——下轮重试）。"""
    persona_id = await _create_primary_persona(db_session)
    reminder_id = await _seed_reminder(db_session, persona_id)
    job = build_reminder_scan_job(
        reminder_service=ReminderService(),
        persona_service=PersonaService(),
        channel_router=_router(None),
        owner_chat_id=OWNER_CHAT_ID,
        session_factory=session_scope,
        clock=lambda: NOW_MS,
    )

    await job()

    record = await _fetch(db_session, persona_id, reminder_id)
    assert record.status is ReminderStatus.PENDING
    assert record.delivery_attempts == 0


async def test_scan_job_without_router_leaves_pending(db_session: AsyncSession) -> None:
    """路由器未装配（None）：提醒保持 pending（无投递出口语义）。"""
    persona_id = await _create_primary_persona(db_session)
    reminder_id = await _seed_reminder(db_session, persona_id)
    job = build_reminder_scan_job(
        reminder_service=ReminderService(),
        persona_service=PersonaService(),
        channel_router=None,
        owner_chat_id=OWNER_CHAT_ID,
        session_factory=session_scope,
        clock=lambda: NOW_MS,
    )

    await job()

    record = await _fetch(db_session, persona_id, reminder_id)
    assert record.status is ReminderStatus.PENDING


async def test_scan_job_without_owner_chat_leaves_pending(db_session: AsyncSession) -> None:
    """属主会话未配置：提醒保持 pending（无投递目标语义）。"""
    persona_id = await _create_primary_persona(db_session)
    reminder_id = await _seed_reminder(db_session, persona_id)
    adapter = FakeTelegramAdapter()
    job = build_reminder_scan_job(
        reminder_service=ReminderService(),
        persona_service=PersonaService(),
        channel_router=_router(adapter),
        owner_chat_id=None,
        session_factory=session_scope,
        clock=lambda: NOW_MS,
    )

    await job()

    assert adapter.sent == []
    record = await _fetch(db_session, persona_id, reminder_id)
    assert record.status is ReminderStatus.PENDING


async def test_scan_job_send_failure_reaches_failed_state(db_session: AsyncSession) -> None:
    """发送异常：attempts 累计至上限后转 failed（不再重试）。"""
    persona_id = await _create_primary_persona(db_session)
    reminder_id = await _seed_reminder(db_session, persona_id)
    job = build_reminder_scan_job(
        reminder_service=ReminderService(),
        persona_service=PersonaService(),
        channel_router=_router(FakeTelegramAdapter(fail=True)),
        owner_chat_id=OWNER_CHAT_ID,
        session_factory=session_scope,
        clock=lambda: NOW_MS,
    )

    for _ in range(DELIVERY_FAILURE_LIMIT):
        await job()

    record = await _fetch(db_session, persona_id, reminder_id)
    assert record.status is ReminderStatus.FAILED
    assert record.delivery_attempts == DELIVERY_FAILURE_LIMIT
    assert record.last_error is not None
    assert "平台发送失败" in record.last_error


async def test_scan_job_without_primary_persona_is_noop(db_session: AsyncSession) -> None:
    """无主人格：作业正常返回（零异常 / 零副作用）。"""
    job = build_reminder_scan_job(
        reminder_service=ReminderService(),
        persona_service=PersonaService(),
        channel_router=_router(FakeTelegramAdapter()),
        owner_chat_id=OWNER_CHAT_ID,
        session_factory=session_scope,
        clock=lambda: NOW_MS,
    )

    await job()

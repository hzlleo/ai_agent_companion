"""reminders 域 ORM——``ff_reminders``（持久化提醒表）。

设计决策（**人格作用域**）：

    - 表挂 ``PersonaMixin``：提醒是主人与某一人格的约定，作用域与记忆
      表同轴——写路径 ``before_flush`` 自动注入 persona_id，读路径
      ``do_orm_execute`` 自动过滤（调用方须先经 ``bind_persona_context``
      绑定上下文，否则 fail loud）；
    - 扫描作业（调度器）先解析主人格再在人格上下文内扫描——跨人格全表
      扫描有意不做（提醒恒属某人格；无主人格 = 无投递对象，日志跳过）。

列与不变式约定：

    - ``status`` 值域见 ``reminders.models.ReminderStatus``（pending /
      delivered / cancelled / failed）；状态迁移由服务层单向推进
      （pending → delivered / cancelled / failed），无回退路径；
    - ``due_at_ms`` 索引承载扫描到期判定；复合索引
      ``(persona_id, status, due_at_ms)`` 承载扫描主查询形态（当前人格
      的待投递到期行——顺序与 WHERE / ORDER BY 一致）；
    - ``delivery_attempts`` / ``last_error`` 是投递失败观测面：连续失败
      达上限后行转 ``failed``（服务层阈值，见 ``reminders.service``）；
    - ``meta`` 为可空 JSON（来源附加信息——如聊天捕获的原始文本），
      不参与业务判定。

时间列全部 epoch 毫秒（BigInteger）；枚举值以字符串落库（值域由
``reminders.models`` 枚举在 DTO 边界校验）。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import JSON, BigInteger, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from packages.harness.database.base import (
    Base,
    BigIntIdMixin,
    PersonaMixin,
    TimestampMixin,
)

__all__ = ["ReminderRow"]


class ReminderRow(Base, BigIntIdMixin, TimestampMixin, PersonaMixin):
    """持久化提醒行（人格作用域——见模块 docstring 设计决策）。

    Attributes:
        title: 提醒标题（展示 / 投递文案正文——1-200 字符）。
        due_at_ms: 到期时刻（epoch 毫秒；扫描判定轴，索引）。
        status: 状态（pending / delivered / cancelled / failed）。
        delivery_attempts: 投递失败累计次数（达上限后转 failed）。
        last_error: 最近一次投递失败原因（成功 / 未尝试为 NULL）。
        delivered_at_ms: 投递成功时刻（epoch 毫秒；未投递为 NULL）。
        source: 来源（manual = 管理面创建；chat = 对话意图捕获）。
        meta: 来源附加信息 JSON（可空——不参与业务判定）。
    """

    __tablename__ = "ff_reminders"
    __table_args__ = (
        Index("ix_ff_reminders_due_at_ms", "due_at_ms"),
        Index("ix_ff_reminders_persona_status_due", "persona_id", "status", "due_at_ms"),
    )

    title: Mapped[str] = mapped_column(String(200), comment="提醒标题（1-200 字符）")
    due_at_ms: Mapped[int] = mapped_column(
        BigInteger, comment="到期时刻（epoch 毫秒——扫描判定轴）"
    )
    status: Mapped[str] = mapped_column(
        String(16), comment="状态（pending/delivered/cancelled/failed）"
    )
    delivery_attempts: Mapped[int] = mapped_column(
        Integer, default=0, comment="投递失败累计次数（达上限转 failed）"
    )
    last_error: Mapped[str | None] = mapped_column(
        String(512), nullable=True, comment="最近一次投递失败原因（NULL = 无失败）"
    )
    delivered_at_ms: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, comment="投递成功时刻（epoch 毫秒；NULL = 未投递）"
    )
    source: Mapped[str] = mapped_column(
        String(16), comment="来源（manual=管理面；chat=对话捕获）"
    )
    meta: Mapped[dict[str, Any] | None] = mapped_column(
        JSON, nullable=True, comment="来源附加信息 JSON（可空——不参与业务判定）"
    )

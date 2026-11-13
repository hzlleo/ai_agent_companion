"""memory 域梦境 ORM——运行日志表 ff_dream_runs（P1 chunk 3 交付）。

独立文件决策（文档化）：梦境运行日志是 chunk 3 的纯新增面——独立
``dreaming_orm.py`` 保持 chunk 1 交付的 ``orm.py``（四表）零改动，
迁移 / 导入面互不扰动；两文件同处 memory 域包，Base 注册表共享。

表清单：

    - ``ff_dream_runs`` —— 夜间梦境整理运行日志（PersonaMixin 人格
      作用域；每次运行一行，承载状态 / 统计 / 逐事件决策 / 日记链接）。

列约定：时间全部 epoch 毫秒（BigInteger）；枚举值以字符串落库（值域
由 dreaming_models 枚举在 DTO 边界校验）；``stats`` / ``decisions``
为 JSON 列（Model Class 序列化形态——DreamRunStatsModel /
DreamDecisionModel）；``diary_episode_id`` 自引用外键指向情景事件表
（SET NULL——日记事件被管理面删除时运行日志保留，链接置空）。

运行状态语义：行创建即落库 ``status=running``（进程崩溃可能遗留孤儿
running 行——P3 调度器负责收敛，本 chunk 文档化该边界）。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from packages.harness.database.base import (
    Base,
    BigIntIdMixin,
    PersonaMixin,
    TimestampMixin,
)

__all__ = ["DreamRunRow"]


class DreamRunRow(Base, BigIntIdMixin, TimestampMixin, PersonaMixin):
    """梦境整理运行日志行（每次 run_dream 一行——状态 / 统计 / 决策 / 日记链接）。

    Attributes:
        status: 运行状态（running/success/failed）。
        started_ms: 开始时间（epoch 毫秒——注入时钟口径）。
        finished_ms: 结束时间（epoch 毫秒；运行中为 None）。
        stats: 运行统计 JSON（DreamRunStatsModel 序列化形态）。
        decisions: 逐事件决策 JSON 列表（DreamDecisionModel 序列化形态；
            既往成功运行的 promoted/merged 决策构成后续运行的跳过依据）。
        diary_episode_id: 日记事件主键（FK ff_memory_episodes.id，SET
            NULL；未生成 / 失败为 None）。
        error: 失败原因（成功为 None）。
    """

    __tablename__ = "ff_dream_runs"
    __table_args__ = (Index("ix_ff_dream_runs_status", "status"),)

    status: Mapped[str] = mapped_column(String(16), comment="运行状态（running/success/failed）")
    started_ms: Mapped[int] = mapped_column(BigInteger, comment="开始时间（epoch 毫秒）")
    finished_ms: Mapped[int | None] = mapped_column(
        BigInteger, comment="结束时间（epoch 毫秒；运行中为 None）"
    )
    stats: Mapped[dict[str, Any]] = mapped_column(
        JSON, comment="运行统计 JSON（DreamRunStatsModel 形态）"
    )
    decisions: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, comment="逐事件决策 JSON 列表（DreamDecisionModel 形态）"
    )
    diary_episode_id: Mapped[int | None] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        ForeignKey("ff_memory_episodes.id", ondelete="SET NULL"),
        comment="日记事件主键（FK ff_memory_episodes.id；未生成为 None）",
    )
    error: Mapped[str | None] = mapped_column(Text, comment="失败原因（成功为 None）")

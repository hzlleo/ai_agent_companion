"""persona 域人物事实 ORM 行——``ff_persona_facts``（结构化事实 + 证据链）。

设计决策（**PersonaMixin 人格作用域**）：人物事实是「某个人格对主人的
认知」，天然 per-persona 隔离——写路径 ``before_flush`` 自动注入
``persona_id``、读路径 ``do_orm_execute`` 自动过滤（与记忆域同一契约）；
调用方须先经 ``bind_persona_context`` 绑定上下文，否则 fail loud。

supersede / 单 active 不变式（与 ``ff_curated_memory`` 同一已验证模式）：

    - 事实身份 = ``(persona_id, category, normalized_hash)``——正文经
      归一化（NFKC + 空白折叠 + 裁剪）后取 sha256，同文即同身份；
    - 唯一键 ``uq_ff_persona_facts_active`` 含可空守卫列
      ``active_marker``：现行行恒 ``'active'``、superseded / rejected 行
      恒 NULL——MySQL / SQLite 唯一键均忽略 NULL，故同身份至多一条现行行、
      多代历史行共存；更正 / 驳回转换必须清空守卫标记（DB 层兜底）；
    - ``superseded_by_id`` 自引用 FK 指向取代者（旧行 → 新行方向）；
      ``version`` 随更正链递增（首版 1）；
    - 同一身份的重复写入（内容归一化后一致）**不**推进版本——按元数据
      刷新处理（置信度取最大值 + 证据合并），见 ``persona.facts``。

证据链（``evidence`` JSON 列）：``FactEvidenceRefModel`` 数组——情景事件
回链 + 逐字摘录 + 时间 + 来源可信度。梦境提取的事实必须带证据；手工 /
更正事实可无事件背书（``episode_id`` 为 None）。证据是**追加合并**语义
（更正保留原证据），不在更新时整体覆盖。

列约定：时间全部 epoch 毫秒（BigInteger）；枚举值以字符串落库（值域由
``persona.facts_models`` 在 DTO 边界校验）；``confidence`` 为 0-1 浮点。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from packages.harness.database.base import (
    Base,
    BigIntIdMixin,
    PersonaMixin,
    TimestampMixin,
)

__all__ = ["FACT_ACTIVE_MARKER", "PersonaFactRow"]

#: 单 active 守卫标记常量（现行事实行的恒定取值；其余状态 NULL）。
FACT_ACTIVE_MARKER: str = "active"


class PersonaFactRow(Base, BigIntIdMixin, TimestampMixin, PersonaMixin):
    """人物事实行（结构化事实 + 证据链 + 更正链——supersede 语义）。

    单 active 不变式由 ``uq_ff_persona_facts_active`` 唯键在 DB 层强制：
    现行行 ``active_marker='active'``、superseded / rejected 行 NULL
    （两方言唯一键均忽略 NULL——多代历史行共存）。

    Attributes:
        category: 事实类别（preference/biography/relationship/context）。
        content: 事实陈述（≤1000 字符——服务层落库前已归一化裁剪）。
        normalized_hash: 正文归一化指纹（sha256 hex——事实身份轴，索引）。
        confidence: 置信度（0-1）。
        status: 状态（active/superseded/rejected）。
        provenance: 来源可信度（owner/agent——untrusted 服务层拒绝）。
        source: 事实来源（dream/manual/correction）。
        evidence: 证据引用 JSON 数组（FactEvidenceRefModel 序列化形态）。
        superseded_by_id: 取代者行主键（自引用 FK；active 行为 NULL）。
        version: 更正链版本号（首版 1，更正递增）。
        active_marker: 单 active 守卫标记（active 行恒 ``'active'`` / 其余
            状态 NULL——唯一键忽略 NULL 使多代共存且驳回后可重新起链）。
    """

    __tablename__ = "ff_persona_facts"
    __table_args__ = (
        UniqueConstraint(
            "persona_id",
            "category",
            "normalized_hash",
            "active_marker",
            name="uq_ff_persona_facts_active",
        ),
        Index("ix_ff_persona_facts_normalized_hash", "normalized_hash"),
        Index("ix_ff_persona_facts_category", "category"),
        Index("ix_ff_persona_facts_status", "status"),
        Index(
            "ix_ff_persona_facts_persona_status_confidence",
            "persona_id",
            "status",
            "confidence",
        ),
    )

    category: Mapped[str] = mapped_column(
        String(16), comment="事实类别（preference/biography/relationship/context）"
    )
    content: Mapped[str] = mapped_column(String(1000), comment="事实陈述（≤1000 字符）")
    normalized_hash: Mapped[str] = mapped_column(
        String(64), comment="正文归一化指纹（sha256——事实身份轴）"
    )
    confidence: Mapped[float] = mapped_column(Float, comment="置信度（0-1）")
    status: Mapped[str] = mapped_column(String(16), comment="状态（active/superseded/rejected）")
    provenance: Mapped[str] = mapped_column(
        String(16), comment="来源可信度（owner/agent；untrusted 服务层拒绝）"
    )
    source: Mapped[str] = mapped_column(String(16), comment="事实来源（dream/manual/correction）")
    evidence: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, comment="证据引用 JSON 数组（FactEvidenceRefModel 序列化形态）"
    )
    superseded_by_id: Mapped[int | None] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        ForeignKey("ff_persona_facts.id", ondelete="SET NULL"),
        comment="取代者行主键（自引用；active 行为 NULL）",
    )
    version: Mapped[int] = mapped_column(Integer, comment="更正链版本号（首版 1）")
    active_marker: Mapped[str | None] = mapped_column(
        String(16),
        comment=(
            "单 active 守卫标记（active 行 'active' / superseded、rejected 行 NULL"
            "——唯一键忽略 NULL，多代共存）"
        ),
    )

"""persona 域技能 ORM 行——``ff_persona_skills``（程序性技能 / 操作指令）。

设计决策（**PersonaMixin 人格作用域**）：技能是「某个人格对主人习惯的
做事方式记忆」——例如主人偏好的回复结构、固定的写作套路、反复出现的
工作流。它天然 per-persona 隔离：写路径 ``before_flush`` 自动注入
``persona_id``、读路径 ``do_orm_execute`` 自动过滤（与记忆域同一契约）；
调用方须先经 ``bind_persona_context`` 绑定上下文，否则 fail loud。

**原地版本语义（锁定——与人物事实的 supersede 链刻意不同）**：技能是
「操作指令」而不是「信念断言」——某个人格在同一时刻只可能按一套指令
办事；旧版本的技能文本没有被保留回溯的语义价值（主人改的是「以后怎么
做」，不是「曾经怎么想」）。因此：

    - 技能身份 = ``(persona_id, slug)``，唯一键直接落库——同人格下同名
      技能至多一行，编辑走原地更新；
    - ``version`` 随编辑递增（首版 1），仅作「这份指令被改过几次」的
      审计计数，不产生历史行、不链式指向；
    - 需要保留旧版全文时由调用方在编辑前自行导出（管理面不做历史链）。

触发条件（``triggers`` JSON 列）：字符串数组，元素为**主人可能说出的
字面短语**（1-40 字符、1-8 条）——compose 匹配只做字面包含判定，不做
模糊推理；短语越贴近主人口语，命中越可靠。

使用计数（``use_count`` / ``last_used_ms``）：compose 命中后的写读强化面
——``use_count`` 决定同场景下优先注入哪条技能（越多用越靠前）；
``last_used_ms`` 为最近一次命中的 epoch 毫秒。写入是 fire-and-forget
（回复链路不因计数失败而失败），详见 ``persona.skills``。

列约定：时间全部 epoch 毫秒（BigInteger）；枚举值以字符串落库（值域由
``persona.skills_models`` 在 DTO 边界校验）；``confidence`` 为 0-1 浮点；
``content`` 为 markdown 步骤正文（Text 列——超出 varchar 行宽限制）。
"""

from __future__ import annotations

from sqlalchemy import JSON, BigInteger, Float, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from packages.harness.database.base import (
    Base,
    BigIntIdMixin,
    PersonaMixin,
    TimestampMixin,
)

__all__ = ["PersonaSkillRow"]


class PersonaSkillRow(Base, BigIntIdMixin, TimestampMixin, PersonaMixin):
    """技能行（程序性技能 / 触发短语 / markdown 步骤正文）。

    唯一键 ``uq_ff_persona_skills_slug (persona_id, slug)`` 在 DB 层强制
    「同人格同 slug 至多一行」——编辑是原地更新（``version`` 递增），
    不产生历史行（理由见模块文档「原地版本语义」）。

    Attributes:
        name: 技能名（1-120 字符——展示面）。
        slug: 归一化 kebab 标识（由 name 派生；同人格唯一）。
        description: 何时使用的触发描述（1-500 字符——面板展示与
            compose 的次要匹配轴）。
        triggers: 触发短语 JSON 数组（1-8 条、每条 1-40 字符——字面
            包含匹配）。
        content: markdown 步骤正文（1-8000 字符——compose 注入体）。
        status: 状态（active/disabled——disabled 不参与 compose 匹配，
            仍可在面板查看与编辑）。
        source: 来源（manual/mined——mined 为夜间梦境挖矿产物）。
        confidence: 置信度（0-1）。
        use_count: compose 命中次数（写读强化轴——降序注入优先）。
        last_used_ms: 最近一次命中时间（epoch 毫秒；未命中为 NULL）。
        version: 原地编辑版本号（首版 1，编辑递增——无历史链）。
    """

    __tablename__ = "ff_persona_skills"
    __table_args__ = (
        UniqueConstraint("persona_id", "slug", name="uq_ff_persona_skills_slug"),
        Index("ix_ff_persona_skills_status", "status"),
    )

    name: Mapped[str] = mapped_column(String(120), comment="技能名（1-120 字符）")
    slug: Mapped[str] = mapped_column(String(80), comment="归一化 kebab 标识（同人格唯一）")
    description: Mapped[str] = mapped_column(
        String(500), comment="何时使用的触发描述（1-500 字符）"
    )
    triggers: Mapped[list[str]] = mapped_column(
        JSON, comment="触发短语 JSON 数组（1-8 条、每条 1-40 字符——字面包含匹配）"
    )
    content: Mapped[str] = mapped_column(Text, comment="markdown 步骤正文（1-8000 字符）")
    status: Mapped[str] = mapped_column(String(16), comment="状态（active/disabled）")
    source: Mapped[str] = mapped_column(String(16), comment="来源（manual/mined）")
    confidence: Mapped[float] = mapped_column(Float, comment="置信度（0-1）")
    use_count: Mapped[int] = mapped_column(
        Integer, default=0, comment="compose 命中次数（写读强化轴）"
    )
    last_used_ms: Mapped[int | None] = mapped_column(
        BigInteger, comment="最近一次命中时间（epoch 毫秒；未命中为 NULL）"
    )
    version: Mapped[int] = mapped_column(
        Integer, default=1, comment="原地编辑版本号（首版 1，编辑递增——无历史链）"
    )

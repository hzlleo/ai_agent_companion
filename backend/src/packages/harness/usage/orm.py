"""usage 域 ORM——token 用量记录表 ``ff_usage_records``。

设计决策（**persona_id 为普通可空索引列，而非 PersonaMixin**）：

    本表是管理面聚合真源：用量统计天然**跨人格**——admin 总览按
    条目 / 用途 / 时间聚合（全库口径），不按人格切分；PersonaMixin
    的请求上下文自动过滤（无人格上下文即 fail loud）与跨人格聚合
    语义直接冲突。故 persona_id 落为普通可空索引列（系统调用——
    值班巡检 / 梦境整理 / 嵌入回填等无人格上下文），由查询方经
    UsageQueryDTO.persona_id 显式过滤。

列约定：时间全部 epoch 毫秒（BigInteger）；token 计数与毫秒时延
非负（Integer——单次调用量级远小于 2^31）；purpose / status 以
字符串落库（值域由 usage.models 枚举在 DTO 边界校验）。
"""

from __future__ import annotations

from sqlalchemy import BigInteger, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from packages.harness.database.base import (
    Base,
    BigIntIdMixin,
    TimestampMixin,
)

__all__ = ["UsageRecordRow"]


class UsageRecordRow(Base, BigIntIdMixin, TimestampMixin):
    """token 用量记录行（一次 LLM / 辅助端点调用一行）。

    写入方：llm 网关用量发射（成功 / 失败路径均落账）；读取方：管理
    面聚合查询（usage.service）。

    Attributes:
        persona_id: 人格 ID（可空——系统调用无人格；普通索引列，非
            PersonaMixin——跨人格聚合真源，见模块 docstring）。
        ts_ms: 调用发起时间（epoch 毫秒；索引——时间范围查询主轴）。
        model_entry: 模型条目名（注册表条目标识；索引——聚合维度）。
        provider: provider 类型（openai_compatible 等）。
        purpose: 调用用途（chat/duty/dream/look_at/embed/tts/asr/tool；
            索引——聚合维度）。
        input_tokens: 输入 token 数（错误路径 / 端点未回传为 0）。
        output_tokens: 输出 token 数。
        total_tokens: 总 token 数。
        latency_ms: 端到端时延毫秒（含失败耗时）。
        status: 调用结果（success / error）。
        request_id: 请求追踪 ID（可空——无请求上下文的后台调用）。
        error_code: 失败错误码（成功为 NULL）。
    """

    __tablename__ = "ff_usage_records"

    persona_id: Mapped[str | None] = mapped_column(
        String(64),
        index=True,
        comment="人格 ID（可空——系统调用无人格；普通索引列非 PersonaMixin）",
    )
    ts_ms: Mapped[int] = mapped_column(BigInteger, index=True, comment="调用发起时间（epoch 毫秒）")
    model_entry: Mapped[str] = mapped_column(
        String(128), index=True, comment="模型条目名（聚合维度）"
    )
    provider: Mapped[str] = mapped_column(String(64), comment="provider 类型")
    purpose: Mapped[str] = mapped_column(String(16), index=True, comment="调用用途（聚合维度）")
    input_tokens: Mapped[int] = mapped_column(Integer, comment="输入 token 数")
    output_tokens: Mapped[int] = mapped_column(Integer, comment="输出 token 数")
    total_tokens: Mapped[int] = mapped_column(Integer, comment="总 token 数")
    latency_ms: Mapped[int] = mapped_column(Integer, comment="端到端时延（毫秒）")
    status: Mapped[str] = mapped_column(String(16), comment="调用结果（success/error）")
    request_id: Mapped[str | None] = mapped_column(String(64), comment="请求追踪 ID（可空）")
    error_code: Mapped[str | None] = mapped_column(String(64), comment="失败错误码（成功为 NULL）")

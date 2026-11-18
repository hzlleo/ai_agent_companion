"""voice 域声纹 ORM 行——``ff_voiceprint_samples`` / ``ff_voiceprint_consents``。

设计决策（**PersonaMixin 人格作用域 + 发送者维度的样本档案**）：

    - 样本行（``ff_voiceprint_samples``）：每行是一条已提取向量的注册样本，
      归属轴为 ``persona_id``（PersonaMixin 自动注入 / 过滤）× ``sender_id``；
      唯一键 ``(persona_id, sender_id, sample_sha256)`` 让重复上传按幂等
      跳过。样本文件由服务层落盘并在撤回 / 删除 / 过期清理时同步删除——
      DB 行与文件同生命周期，行删除即索引失效；
    - 同意行（``ff_voiceprint_consents``）：授予即插入，撤回只更新
      ``revoked_at_ms``（行本身保留为处理合法性凭证）；同一发送者可多次
      授予 / 撤回，读取面按 ``granted_at_ms`` 倒序取最新一条判断有效性；
    - 枚举值以字符串落库（``pending`` / ``done`` 等由模型层校验）；向量
      以 JSON 文本承载（跨 SQLite / MySQL / SeekDB 零方言差异，向量维度另
      有独立整数列供一致性校验）。

列约定：时间全部 epoch 毫秒（BigInteger）；样本路径为相对 / 绝对路径
字符串（服务层按配置根目录解析）；向量文本量为单条千级浮点，Text 列
足够承载。
"""

from __future__ import annotations

from sqlalchemy import BigInteger, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from packages.harness.database.base import Base, BigIntIdMixin, PersonaMixin, TimestampMixin

__all__ = ["VoiceprintConsentRow", "VoiceprintSampleRow"]


class VoiceprintSampleRow(Base, BigIntIdMixin, TimestampMixin, PersonaMixin):
    """声纹样本行（每人格 × 每发送者一条已向量化样本）。

    Attributes:
        sender_id: 发送者 ID（平台侧用户标识）。
        sample_path: 样本音频落盘路径（撤回 / 删除 / 过期时随行清理）。
        sample_sha256: 样本音频哈希（去重键）。
        duration_ms: 样本时长毫秒。
        sample_rate_hz: 采样率赫兹。
        embedding: 说话人向量（JSON 浮点数组文本形态）。
        embedding_dim: 向量维度（跨样本一致性校验面）。
    """

    __tablename__ = "ff_voiceprint_samples"
    __table_args__ = (
        UniqueConstraint(
            "persona_id",
            "sender_id",
            "sample_sha256",
            name="uq_ff_voiceprint_samples_persona_sender_sha",
        ),
        Index("ix_ff_voiceprint_samples_persona_sender", "persona_id", "sender_id"),
    )

    sender_id: Mapped[str] = mapped_column(
        String(128), comment="发送者 ID（平台侧用户标识；与人格共同构成声纹档案轴）"
    )
    sample_path: Mapped[str] = mapped_column(
        String(512), comment="样本音频落盘路径（撤回 / 删除时随行清理）"
    )
    sample_sha256: Mapped[str] = mapped_column(
        String(64), comment="样本音频 SHA-256（重复上传去重键）"
    )
    duration_ms: Mapped[int] = mapped_column(Integer, comment="样本时长（毫秒）")
    sample_rate_hz: Mapped[int] = mapped_column(Integer, comment="样本采样率（赫兹）")
    embedding: Mapped[str] = mapped_column(
        Text, comment="说话人向量（JSON 浮点数组——跨方言以文本承载）"
    )
    embedding_dim: Mapped[int] = mapped_column(Integer, comment="说话人向量维度")


class VoiceprintConsentRow(Base, BigIntIdMixin, TimestampMixin, PersonaMixin):
    """同意记录行（授予即插入；撤回只更新 ``revoked_at_ms``）。

    Attributes:
        sender_id: 同意主体（发送者 ID）。
        consent_text_version: 同意的告知文本版本。
        granted_at_ms: 授予时间（epoch 毫秒）。
        revoked_at_ms: 撤回时间（epoch 毫秒；NULL = 同意有效）。
    """

    __tablename__ = "ff_voiceprint_consents"
    __table_args__ = (
        Index("ix_ff_voiceprint_consents_persona_sender", "persona_id", "sender_id"),
    )

    sender_id: Mapped[str] = mapped_column(String(128), comment="发送者 ID（同意主体）")
    consent_text_version: Mapped[str] = mapped_column(
        String(64), comment="同意文本版本（与隐私告知版本一致——处理合法性凭证）"
    )
    granted_at_ms: Mapped[int] = mapped_column(
        BigInteger, comment="同意授予时间（epoch 毫秒）"
    )
    revoked_at_ms: Mapped[int | None] = mapped_column(
        BigInteger, comment="撤回时间（epoch 毫秒；NULL = 同意有效）"
    )

"""canvas 域 ORM 行——``ff_canvas_documents``（人格作用域的渲染产物）。

设计决策：

    - 画布产物按人格隔离（``PersonaMixin`` 自动注入 / 过滤）——每个人格
      维护自己的产物集，管理台切换人格即切换画布；
    - 内容以文本列承载（HTML / SVG / Markdown 均为 UTF-8 文本）——不落文件，
      因此没有「行删了文件还在」的清理面；
    - ``revision`` 每次更新递增（导出文件名带修订号——同一画布多次导出不
      互相覆盖）；
    - 标题不设唯一键：同一人格可以有多份同名产物（标题是展示面而非标识），
      主键与修订号才是标识。

列约定：时间全部 epoch 毫秒（BigInteger——与全工程同轴）；类型 / 标题长度由
服务层与模型层校验，DB 列宽为上限兜底。
"""

from __future__ import annotations

from sqlalchemy import Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from packages.harness.database.base import Base, BigIntIdMixin, PersonaMixin, TimestampMixin

__all__ = ["CanvasDocumentRow"]


class CanvasDocumentRow(Base, BigIntIdMixin, TimestampMixin, PersonaMixin):
    """画布产物行（每人格一份产物集）。

    Attributes:
        title: 标题（展示与导出文件名原料）。
        kind: 产物类型（html / svg / markdown）。
        content: 自包含内容（UTF-8 文本）。
        revision: 修订号（创建为 1，每次修改递增）。
    """

    __tablename__ = "ff_canvas_documents"
    __table_args__ = (
        Index("ix_ff_canvas_documents_persona_updated", "persona_id", "updated_at"),
    )

    title: Mapped[str] = mapped_column(String(120), comment="标题")
    kind: Mapped[str] = mapped_column(String(16), comment="产物类型（html/svg/markdown）")
    content: Mapped[str] = mapped_column(Text, comment="自包含内容（HTML / SVG / Markdown）")
    revision: Mapped[int] = mapped_column(Integer, comment="修订号（创建为 1，修改递增）")

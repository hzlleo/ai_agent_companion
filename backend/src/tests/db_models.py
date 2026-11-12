"""数据库测试 ORM 模型（全测试树唯一定义——共享 Base.metadata，防表名冲突）。

独立于 conftest 定义：pytest 将 conftest 按根目录相对名加载，测试文件
再经 ``tests.conftest`` 导入会形成双模块身份（同类双定义 → 表名冲突）；
模型收口在本模块后，conftest 与全部测试统一经 ``tests.db_models`` 导入。
"""

from __future__ import annotations

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from packages.harness.database.base import (
    Base,
    BigIntIdMixin,
    PersonaMixin,
    TimestampMixin,
)

__all__ = ["PersonaNoteRow", "PlainCounterRow"]


class PersonaNoteRow(Base, BigIntIdMixin, TimestampMixin, PersonaMixin):
    """人格作用域测试行（PersonaMixin 自动注入 / 过滤验证载体）。

    Attributes:
        content: 内容列（业务载荷占位）。
    """

    __tablename__ = "test_persona_note"

    content: Mapped[str] = mapped_column(String(200), comment="内容")


class PlainCounterRow(Base, BigIntIdMixin, TimestampMixin):
    """非人格作用域测试行（自动过滤零影响验证载体）。

    Attributes:
        label: 标签列（业务载荷占位）。
    """

    __tablename__ = "test_plain_counter"

    label: Mapped[str] = mapped_column(String(100), comment="标签")

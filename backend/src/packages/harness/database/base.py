"""SQLAlchemy 2.0 声明式基类与可复用 Mixin（含人格作用域自动隔离）。

``Base`` 是全工程 ORM 模型的声明式基类；三个 Mixin 供模型组合使用：

    - ``BigIntIdMixin``  —— BigInteger 自增主键（全工程统一主键形态）
    - ``TimestampMixin`` —— epoch 毫秒 created/updated 时间戳（见
      ``core.time_utils``——跨方言零时区漂移）
    - ``PersonaMixin``   —— ``persona_id`` 列 + 基于请求上下文（contextvars）
      的**自动注入与自动过滤**：多人格数据在同一表内逻辑隔离，业务代码
      无需手写 persona 过滤条件

Mixins 是纯 class（不继承 DeclarativeBase），通过多继承混入模型；
SQLAlchemy 2.0 会将 Mixin 中的 ``Mapped`` + ``mapped_column`` 合并到
子类表定义。

人格隔离机制（event listener，模块加载即挂接）::

    写路径（INSERT）  before_flush   —— 新 PersonaMixin 实例缺 persona_id 时
                                        从 persona_id_var 注入；无上下文 →
                                        BusinessException(PERSONA_CONTEXT_REQUIRED)
    读路径（SELECT）  do_orm_execute —— 语句触及人格作用域实体时自动附加
                                        WHERE persona_id = <当前上下文>；
                                        无上下文 → 同上 fail loud
    绕过方式（管理面/系统级操作）
        session.execute(stmt, execution_options={"skip_persona_filter": True})

时间戳迁移说明：ORM 定义中不设 ``server_default``（SQLite 测试环境不兼容
MySQL 函数），DB 侧默认值由 Alembic 迁移层负责。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import BigInteger, Integer, String, event
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    mapped_column,
    with_loader_criteria,
)

from packages.harness.core.context import persona_id_var
from packages.harness.core.errors import BusinessException, ErrorCode
from packages.harness.core.time_utils import now_ms

__all__ = [
    "Base",
    "BigIntIdMixin",
    "TimestampMixin",
    "PersonaMixin",
    "PERSONA_FILTER_SKIP_OPTION",
]

#: 绕过人格自动过滤的 execution option 键（管理面 / 系统级查询用）。
PERSONA_FILTER_SKIP_OPTION = "skip_persona_filter"


class Base(DeclarativeBase):
    """SQLAlchemy 2.0 声明式基类——所有 ORM 模型继承此类::

    class ChatMessage(Base, PersonaMixin, TimestampMixin):
        __tablename__ = "chat_message"
        ...
    """


class BigIntIdMixin:
    """主键 Mixin——BigInteger 自增主键（全工程统一主键形态）。

    MySQL 8 下 ``BIGINT AUTO_INCREMENT``；SQLite 测试方言下渲染为
    ``INTEGER``（``with_variant``）——SQLite 仅对 ``INTEGER PRIMARY KEY``
    启用 rowid 自增，BIGINT 主键会静默失去自增（NOT NULL 违约）。
    """

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
        comment="主键（BIGINT 自增）",
    )


class TimestampMixin:
    """时间戳 Mixin——epoch 毫秒 created/updated（BigInteger）。

    Python 侧 ``default=now_ms`` / ``onupdate=now_ms``（callable——每次
    取新值）；``updated_at`` 在每次 UPDATE 自动刷新。DB 侧
    ``server_default`` 由 Alembic 迁移设置（ORM 不设，保持 SQLite 测试
    兼容）。
    """

    created_at: Mapped[int] = mapped_column(
        BigInteger, default=now_ms, comment="创建时间（epoch 毫秒）"
    )
    updated_at: Mapped[int] = mapped_column(
        BigInteger,
        default=now_ms,
        onupdate=now_ms,
        comment="更新时间（epoch 毫秒）",
    )


class PersonaMixin:
    """人格作用域 Mixin——``persona_id`` 列 + 上下文驱动的自动隔离。

    继承此 Mixin 的模型表内按 ``persona_id`` 逻辑分区：写路径经
    ``before_flush`` 从 :data:`persona_id_var` 自动注入（业务代码无需
    显式赋值），读路径经 ``do_orm_execute`` 自动附加 ``WHERE persona_id
    = <当前上下文>``（业务代码无需手写过滤）。

    两个守卫均 fail loud（``PERSONA_CONTEXT_REQUIRED``）：触及人格作用域
    实体而无人格上下文是编程错误（入口层必须先绑定 persona 上下文，
    见 ``core.context.bind_context``），静默放行会造成全表可见的数据
    泄漏。绕过通道（管理面 / 跨人格聚合）::

        session.execute(stmt, execution_options={"skip_persona_filter": True})
    """

    persona_id: Mapped[str] = mapped_column(
        String(64),
        index=True,
        nullable=False,
        comment="人格 ID（请求上下文自动注入 + 查询自动过滤）",
    )


# ---------------------------------------------------------------------------
# 人格隔离 event listener（模块加载即挂接；Sync Session 覆盖 AsyncSession）
# ---------------------------------------------------------------------------


def _statement_touches_persona(statement: Any) -> bool:
    """判定 SELECT 语句是否触及人格作用域实体（PersonaMixin 子类）。

    两条检测通道：

        - ``column_descriptions`` 携带 entity（实体 select / 列 select /
          ``func.count(Entity.col)`` 形态）；
        - FROM 子句表名反查 ORM 注册表（``func.count() + select_from(Entity)``
          聚合形态——column_descriptions 不携带 entity，但 FROM 表可映射回
          PersonaMixin 实体）。

    Args:
        statement: SQLAlchemy Select 语句。

    Returns:
        语句触及任一 PersonaMixin 实体返回 True。
    """
    for desc in getattr(statement, "column_descriptions", []):
        entity = desc.get("entity")
        if entity is not None and isinstance(entity, type) and issubclass(entity, PersonaMixin):
            return True

    final_froms = getattr(statement, "get_final_froms", None)
    if final_froms is None:
        return False
    from_table_names = {getattr(table, "name", "") for table in final_froms()}
    for mapped in Base.registry._class_registry.values():
        if (
            isinstance(mapped, type)
            and issubclass(mapped, PersonaMixin)
            and getattr(mapped, "__tablename__", None) in from_table_names
        ):
            return True
    return False


def _require_persona_context(operation: str) -> str:
    """读取当前人格上下文，缺失即 fail loud。

    Args:
        operation: 触发场景描述（写入 details）。

    Returns:
        当前 ``persona_id_var`` 值（非 None）。

    Raises:
        BusinessException: 人格上下文未设置（``PERSONA_CONTEXT_REQUIRED``，
            含可操作指引：经 ``bind_context(persona_id=...)`` 绑定后再执行）。
    """
    persona_id = persona_id_var.get()
    if persona_id is None:
        raise BusinessException(
            code=ErrorCode.PERSONA_CONTEXT_REQUIRED,
            details={
                "operation": operation,
                "fix": "入口层先经 core.context.bind_context(persona_id=...) 绑定人格上下文",
            },
        )
    return persona_id


@event.listens_for(Session, "before_flush")
def _inject_persona_id(session: Session, flush_context: Any, instances: Any) -> None:
    """写路径守卫——新 PersonaMixin 实例缺 ``persona_id`` 时自动注入。

    从 :data:`persona_id_var` 读取当前人格并注入（业务代码无需显式
    赋值）；显式指定的 ``persona_id`` 原样保留（跨人格系统写入的合法
    通道）。缺上下文时 fail loud（见 :func:`_require_persona_context`）。

    Args:
        session: 同步 Session（AsyncSession 内部委派）。
        flush_context: SQLAlchemy flush 上下文（未使用）。
        instances: flush 实例参数（未使用——统一从 session.new 扫描）。
    """
    for obj in session.new:
        if isinstance(obj, PersonaMixin):
            # Mapped[str] 静态类型为非空，但 flush 前未赋值的列属性运行期为
            # None——经 getattr 取值以表达「未显式指定」的 Optional 语义
            current_persona_id: str | None = getattr(obj, "persona_id", None)
            if current_persona_id is None:
                obj.persona_id = _require_persona_context(f"INSERT {type(obj).__name__}")


@event.listens_for(Session, "do_orm_execute")
def _persona_filter(execute_state: Any) -> None:
    """读路径守卫——人格作用域 SELECT 自动附加当前人格过滤。

    语句触及 PersonaMixin 实体时经 ``with_loader_criteria`` 附加
    ``WHERE persona_id = <当前上下文>``（含 alias 场景）；非人格作用域
    语句零影响（criteria 对不相关实体为 no-op）。缺上下文时 fail loud。

    Args:
        execute_state: SQLAlchemy ORMExecuteState（仅 SELECT 处理）。
    """
    if not execute_state.is_select:
        return
    if execute_state.execution_options.get(PERSONA_FILTER_SKIP_OPTION):
        return
    if not _statement_touches_persona(execute_state.statement):
        return

    persona_id = _require_persona_context("SELECT persona-scoped rows")
    execute_state.statement = execute_state.statement.options(
        with_loader_criteria(
            PersonaMixin,
            lambda cls: cls.persona_id == persona_id,
            include_aliases=True,
        )
    )

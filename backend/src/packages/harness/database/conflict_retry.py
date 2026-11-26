"""MySQL 协议族瞬态事务冲突的单次收敛助手（调用方级重试）。

作用面：唯一键并发写入在可重复读旧快照下可能以「事务冲突」而非 1062 的
形态抛出——SeekDB 实测错误 6235（``can't serialize access for this
transaction``），MySQL 协议族死锁实测错误 1213。两类错误都不是业务失败：
回滚后在新事务中重做同一操作，预读会看到已提交的胜者行并收敛为既有记录 /
重复结果；其余驱动错误原样上抛，不做任何吞没。

设计边界（锁定）：

    - **只重试一次**：二次冲突是持续性争用信号，上抛由调用方失败策略处理；
    - **回滚先于重试**：旧事务的快照与失败语句状态必须先清空（SeekDB 旧
      快照不会因锁定读而跨越，实测）；
    - **清理回调可选**：调用方可在回滚后清理本次尝试写入、且无已提交行
      引用的落败文件——清理面归调用方（本助手不感知业务文件语义）；
    - 错误码判定只看驱动原始错误码（``orig.args[0]``），SQLite 不产生这
      两类错误——测试回退路径零影响。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Final, TypeVar, cast

from loguru import logger
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

__all__ = [
    "TRANSACTION_CONFLICT_ERROR_CODES",
    "is_transaction_conflict",
    "run_with_conflict_retry",
]

#: 可单次重试的瞬态事务冲突错误码（SeekDB 6235 / MySQL 协议族死锁 1213）。
TRANSACTION_CONFLICT_ERROR_CODES: Final[frozenset[int]] = frozenset({6235, 1213})

_T = TypeVar("_T")


def is_transaction_conflict(exc: OperationalError) -> bool:
    """判定驱动错误是否为可重试的瞬态事务冲突。

    Args:
        exc: SQLAlchemy 包装的驱动操作错误（``orig`` 为驱动原始异常）。

    Returns:
        ``orig.args[0]`` 为 6235 / 1213 时 True；其余（含 ``orig`` 缺失 /
        首参非整数）False。
    """
    orig = exc.orig
    if orig is None:
        return False
    args = cast("tuple[object, ...]", getattr(orig, "args", ()))
    if not args:
        return False
    code = args[0]
    return isinstance(code, int) and code in TRANSACTION_CONFLICT_ERROR_CODES


async def run_with_conflict_retry(
    session: AsyncSession,
    operation: Callable[[], Awaitable[_T]],
    *,
    cleanup: Callable[[], Awaitable[None]] | None = None,
) -> _T:
    """执行操作；遇瞬态事务冲突时回滚后原样重做一次。

    Args:
        session: 操作使用的异步 Session（冲突时由本助手回滚）。
        operation: 完整操作闭包（含预读 / 写入 / 提交——重试语义要求整段
            重做，使预读看到冲突方已提交的胜者行）。
        cleanup: 回滚后、重试前的可选清理回调（调用方用于清理本次尝试写入
            且无已提交行引用的落败文件；不得吞掉驱动错误）。

    Returns:
        操作在首次成功尝试时的返回值。

    Raises:
        OperationalError: 非瞬态冲突（原样上抛）；或第二次尝试仍为瞬态
            冲突（持续争用——不再重试）。
    """
    try:
        return await operation()
    except OperationalError as exc:
        if not is_transaction_conflict(exc):
            raise
        logger.warning(
            "瞬态事务冲突（驱动错误码 {}），回滚后单次重试",
            cast("tuple[object, ...]", getattr(exc.orig, "args", ()))[0],
        )
    await session.rollback()
    if cleanup is not None:
        await cleanup()
    return await operation()

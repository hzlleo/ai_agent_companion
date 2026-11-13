"""aiomysql 类型化访问面——SeekDB 连接池装配 / 密码解析 / 错误基类（兼容层）。

为什么存在本模块（文档化工程决策）：

    - aiomysql 0.3.2 未随包发布 ``py.typed`` 标记——mypy 严格模式拒绝
      直连 ``import aiomysql``（import-untyped）；本工程约束不允许新增
      依赖（stub 包）也不允许修改 pyproject —— 故经 ``importlib`` 动态
      加载 + Protocol 类型面收口：真实池 / 连接 / 游标对象为鸭子类型，
      本模块声明的 Protocol 即调用方依赖的全部表面；
    - SeekDB（v1.4.0）官方仅文档化 PyMySQL / mysqlclient 驱动，**aiomysql
      无官方背书**（MySQL 线协议标准，大概率可用）——原生 async 符合
      本工程约束（I/O 全 async），选定 aiomysql 为默认驱动并文档化
      风险；真实实例可用性经 ``tests/integration/test_seekdb_smoke.py``
      （env 门控，默认跳过）验证，不兼容时的回退路径为 PyMySQL +
      ``run_blocking_ops`` 下沉（见 docs/backend/memory.md 检索层）。

连接形态：autocommit（单语句自决事务）——索引层的写路径全部为单
语句 upsert / delete / DDL，无跨语句事务需求（文档化决策）。
"""

from __future__ import annotations

import importlib
import os
from collections.abc import Sequence
from contextlib import AbstractAsyncContextManager
from types import ModuleType
from typing import Any, Protocol, cast

from loguru import logger

__all__ = [
    "SeekDbCursorProtocol",
    "SeekDbConnectionProtocol",
    "SeekDbPoolProtocol",
    "create_seekdb_pool",
    "resolve_seekdb_password",
    "seekdb_error_base",
]

#: aiomysql 模块缓存（importlib 动态加载——见模块 docstring；纯 import
#: 零连接副作用，模块加载期安全）。
_AIOMYSQL_MODULE: ModuleType | None = None


class SeekDbCursorProtocol(Protocol):
    """aiomysql 游标的最小依赖面（类型化收口）。"""

    @property
    def description(self) -> Sequence[Sequence[Any]] | None:
        """结果列描述（非 SELECT 语句为 None——列名取自每项首元素）。"""
        ...

    async def execute(self, query: str, args: Sequence[Any] | None = None) -> int:
        """执行单条语句。

        Args:
            query: 语句字符串。
            args: 按序绑定参数序列（None = 无参语句）。

        Returns:
            受影响行数（驱动回传值——DDL / 写语句面）。
        """
        ...

    async def fetchall(self) -> Sequence[Sequence[Any]]:
        """取回全部结果行。

        Returns:
            行值序列列表（与 description 列序一致）。
        """
        ...


class SeekDbConnectionProtocol(Protocol):
    """aiomysql 连接的最小依赖面（游标经 async with 获取 / 归还）。"""

    def cursor(self) -> AbstractAsyncContextManager[SeekDbCursorProtocol]:
        """获取游标。

        Returns:
            游标异步上下文管理器（退出即关闭游标）。
        """
        ...


class SeekDbPoolProtocol(Protocol):
    """aiomysql 连接池的最小依赖面（连接经 async with 获取 / 归还）。"""

    def acquire(self) -> AbstractAsyncContextManager[SeekDbConnectionProtocol]:
        """获取连接。

        Returns:
            连接异步上下文管理器（退出即归还池）。
        """
        ...

    def close(self) -> None:
        """标记关闭（拒绝新连接获取）。"""
        ...

    async def wait_closed(self) -> None:
        """等待在途连接全部归还后完成关闭。"""
        ...


def _load_aiomysql() -> ModuleType:
    """加载并缓存 aiomysql 模块（importlib 动态加载——mypy 兼容面）。

    Returns:
        aiomysql 模块对象（进程内缓存——重复调用零开销）。
    """
    global _AIOMYSQL_MODULE
    if _AIOMYSQL_MODULE is None:
        _AIOMYSQL_MODULE = importlib.import_module("aiomysql")
    return _AIOMYSQL_MODULE


async def create_seekdb_pool(
    *,
    host: str,
    port: int,
    user: str,
    password: str,
    database: str,
    pool_min: int,
    pool_max: int,
) -> SeekDbPoolProtocol:
    """创建 SeekDB aiomysql 连接池（autocommit——单语句自决事务）。

    连接参数全部显式传入（配置映射归调用方——PoolManager 读
    ``Settings.seekdb``，独立场景读自建配置）。

    Args:
        host: SeekDB 服务地址。
        port: MySQL 线协议端口（server 模式 2881）。
        user: 连接用户名。
        password: 连接密码（经 :func:`resolve_seekdb_password` 解析）。
        database: 连接库名。
        pool_min: 常驻连接数（启动期即建立——SeekDB 不可达时 fail loud）。
        pool_max: 连接数上限。

    Returns:
        符合 :class:`SeekDbPoolProtocol` 的连接池（生命周期归调用方——
        ``close`` + ``wait_closed`` 收口）。
    """
    module = _load_aiomysql()
    pool = await module.create_pool(
        minsize=pool_min,
        maxsize=pool_max,
        host=host,
        port=port,
        user=user,
        password=password,
        db=database,
        autocommit=True,
    )
    logger.info(
        "SeekDB aiomysql 连接池已创建: host={}, port={}, db={}, pool=({}-{})",
        host,
        port,
        database,
        pool_min,
        pool_max,
    )
    return cast(SeekDbPoolProtocol, pool)


def resolve_seekdb_password(password_env: str) -> str:
    """按环境变量名读取 SeekDB 密码（env 间接——密码永不落 yaml / 配置树）。

    Args:
        password_env: 密码环境变量名（如 ``SEEKDB_PASSWORD``）。

    Returns:
        密码明文（变量未设置 / 为空时返回空串并告警——本地开发零密码
        形态合法；生产部署应显式设置）。
    """
    password = os.environ.get(password_env, "")
    if not password:
        logger.warning(
            "SeekDB 密码环境变量未设置或为空: env={}（空密码连接——本地开发形态）",
            password_env,
        )
    return password


def seekdb_error_base() -> type[Exception]:
    """取 SeekDB 驱动异常基类（aiomysql re-export 的 ``pymysql.err.Error``）。

    Returns:
        驱动异常基类（存储层 except 子句的捕获面——运行期 TypeError /
        OperationalError / ProgrammingError 等皆为其子类）。
    """
    module = _load_aiomysql()
    return cast("type[Exception]", module.Error)

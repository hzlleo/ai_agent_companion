"""公共池管理器——全工程长生命周期池的统一装配 / 释放（进程级单例）。

管理以下异步资源（P0 决策：NO Redis——本工程无 Redis 池）：

    1. core 共享线程池（``ThreadPoolExecutor``）——同步/CPU 密集操作下沉
       （经 :func:`core.thread_pool.init_shared_thread_pool` 装配，生命周期
       由 core 自持，本管理器只负责启停协调）；
    2. 共享 HTTP 池（``httpx.AsyncClient``）——通用出站 HTTP；
    3. LLM 条目池（:class:`packages.harness.pools.llm_pool.LlmEntryPool`）
       —— per-entry 并发信号量 + 熔断器（P0-4 网关消费）；
    4. SeekDB 检索池（aiomysql——P1 chunk 2 检索层消费；仅
       ``settings.seekdb.enabled`` 时启动，plain pool——无熔断 / 信号量
       复杂度，见 ``pools.seekdb_compat``）。

初始化顺序（app lifespan startup，P0-6 装配）::

    init_pool_manager(settings)  →  pool_manager.start()  →  init_database() → ...

关闭顺序（LIFO，lifespan shutdown）::

    ...  →  close_database()  →  pool_manager.stop()

使用方式::

    from packages.harness.pools.manager import init_pool_manager, pool_manager
    init_pool_manager(settings)      # lifespan 中构造单例
    await pool_manager.start()
    pool_manager.get_http_client()
    pool_manager.get_llm_entry_pool()
    pool_manager.get_seekdb_pool()
    await pool_manager.stop()
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import httpx
from loguru import logger

from packages.harness.config.models import Settings
from packages.harness.core.errors import BusinessException, ErrorCode
from packages.harness.core.thread_pool import (
    get_shared_thread_pool,
    init_shared_thread_pool,
    shutdown_shared_thread_pool,
)
from packages.harness.pools.http_pool import build_shared_http_client
from packages.harness.pools.llm_pool import LlmEntryPool
from packages.harness.pools.seekdb_compat import (
    SeekDbPoolProtocol,
    create_seekdb_pool,
    resolve_seekdb_password,
)

__all__ = [
    "PoolManager",
    "pool_manager",
    "init_pool_manager",
    "get_pool_manager",
]


class PoolManager:
    """统一池管理器——start 后可用，stop 后归 None。

    构造注入（决策）：``__init__`` 接收 :class:`Settings`，保存为
    ``self._settings``——pools 域不持有全局单例引用，测试可注入隔离
    Settings。

    P0 决策：NO Redis——无 Redis 客户端槽位（缓存走 core 进程内 TTL
    实现，见 ``core.cache``）。
    """

    def __init__(self, settings: Settings) -> None:
        """初始化管理器（未 start——四槽位均为空）。

        Args:
            settings: 全局（或隔离）Settings 实例。
        """
        self._settings = settings
        self.http_client: httpx.AsyncClient | None = None
        self.llm_entry_pool: LlmEntryPool | None = None
        self.seekdb_pool: SeekDbPoolProtocol | None = None
        self._started = False

    @property
    def started(self) -> bool:
        """是否已 start（未 start 时 get_* 抛业务异常）。"""
        return self._started

    async def start(self) -> None:
        """装配全部池（从 ``self._settings`` 读取配置）。

        必须在 lifespan startup 中先于数据库 / 业务服务调用——
        ``run_blocking_ops`` 依赖线程池，通用 HTTP 出站依赖共享 client。

        幂等：已 start 时为 no-op（重复 start 直接返回）。

        Raises:
            BusinessException: SeekDB 池创建失败（``enabled=True`` 且
                实例不可达——fail loud，部署顺序契约：SeekDB 先于 app
                就绪；``INTERNAL_ERROR``）。
        """
        if self._started:
            return
        pools = self._settings.pools

        # 1. core 共享线程池——同步库下沉（jwt.decode / zipfile / 同步 SDK）
        init_shared_thread_pool(pools.thread_pool_size)

        # 2. 共享 HTTP 池——通用出站（LLM 专用连接由网关按条目自建）
        self.http_client = build_shared_http_client(pools)

        # 3. LLM 条目池——per-entry 信号量 + 熔断器（网关消费；信号量惰性注册）
        self.llm_entry_pool = LlmEntryPool(pools)

        # 4. SeekDB 检索池——aiomysql plain pool（仅 enabled 时启动）
        seekdb_settings = self._settings.seekdb
        if seekdb_settings.enabled:
            password = resolve_seekdb_password(seekdb_settings.password_env)
            try:
                self.seekdb_pool = await create_seekdb_pool(
                    host=seekdb_settings.host,
                    port=seekdb_settings.port,
                    user=seekdb_settings.user,
                    password=password,
                    database=seekdb_settings.database,
                    pool_min=seekdb_settings.pool_min,
                    pool_max=seekdb_settings.pool_max,
                )
            except Exception as exc:
                raise BusinessException(
                    code=ErrorCode.INTERNAL_ERROR,
                    message="SeekDB 检索池创建失败（fail loud——检查 SEEKDB_* 配置与实例可达性）",
                    details={"reason": f"{type(exc).__name__}: {exc}"},
                ) from exc

        self._started = True
        logger.info(
            "公共池已就绪: thread_pool_size={}, http_max_connections={}, "
            "http_keepalive={}, llm_semaphore_limit={}, cb_fail_max={}, cb_cooldown={}s, "
            "seekdb_enabled={}",
            pools.thread_pool_size,
            pools.http_max_connections,
            pools.http_max_keepalive_connections,
            pools.llm_semaphore_limit,
            pools.cb_fail_max,
            pools.cb_reset_timeout_seconds,
            seekdb_settings.enabled,
        )

    async def stop(self) -> None:
        """关闭全部池（LIFO：SeekDB 池 → HTTP / LLM 条目池 → core 线程池）。

        幂等：未 start 时为 no-op。
        """
        if not self._started:
            return

        # 0. 关闭 SeekDB 检索池（LIFO——最后启动最先关闭）
        if self.seekdb_pool is not None:
            self.seekdb_pool.close()
            await self.seekdb_pool.wait_closed()
            self.seekdb_pool = None
            logger.info("SeekDB 检索池已关闭")

        # 1. 关闭共享 HTTP 池
        if self.http_client is not None:
            await self.http_client.aclose()
            self.http_client = None
            logger.info("共享 HTTP 池已关闭")

        # 2. 关闭 LLM 条目池（信号量无需显式释放；清空注册状态）
        if self.llm_entry_pool is not None:
            self.llm_entry_pool.close()
            self.llm_entry_pool = None
            logger.info("LLM 条目池已关闭")

        # 3. 关闭 core 共享线程池（等待在途阻塞操作自然收尾）
        shutdown_shared_thread_pool(wait=True)
        logger.info("core 共享线程池已关闭")

        self._started = False
        logger.info("公共池已全部关闭")

    def get_thread_pool(self) -> ThreadPoolExecutor:
        """获取 core 共享线程池（代理 core 单例）。

        Returns:
            共享 ``ThreadPoolExecutor`` 实例。

        Raises:
            BusinessException: 未 start（线程池未装配）。
        """
        pool = get_shared_thread_pool()
        if pool is None:
            raise BusinessException(
                code=ErrorCode.INTERNAL_ERROR,
                message="线程池未初始化，请先调用 pool_manager.start()",
            )
        return pool

    def get_http_client(self) -> httpx.AsyncClient:
        """获取共享 HTTP 客户端。

        Returns:
            共享 :class:`httpx.AsyncClient` 实例。

        Raises:
            BusinessException: 未 start。
        """
        if self.http_client is None:
            raise BusinessException(
                code=ErrorCode.INTERNAL_ERROR,
                message="HTTP 池未初始化，请先调用 pool_manager.start()",
            )
        return self.http_client

    def get_llm_entry_pool(self) -> LlmEntryPool:
        """获取 LLM 条目池（per-entry 信号量 + 熔断器）。

        Returns:
            :class:`LlmEntryPool` 实例。

        Raises:
            BusinessException: 未 start。
        """
        if self.llm_entry_pool is None:
            raise BusinessException(
                code=ErrorCode.INTERNAL_ERROR,
                message="LLM 条目池未初始化，请先调用 pool_manager.start()",
            )
        return self.llm_entry_pool

    def get_seekdb_pool(self) -> SeekDbPoolProtocol:
        """获取 SeekDB 检索池（aiomysql plain pool——检索层消费）。

        Returns:
            符合 :class:`SeekDbPoolProtocol` 的连接池实例。

        Raises:
            BusinessException: 未 start 或 ``SEEKDB_ENABLED`` 未开启
                （``INTERNAL_ERROR``——检索存储装配前置检查面）。
        """
        if self.seekdb_pool is None:
            raise BusinessException(
                code=ErrorCode.INTERNAL_ERROR,
                message="SeekDB 检索池未初始化（未 start 或 SEEKDB_ENABLED=false）",
            )
        return self.seekdb_pool


# ---------------------------------------------------------------------------
# 全局单例 + 访问器（延迟初始化——app lifespan 经 init_pool_manager 装配）
# ---------------------------------------------------------------------------

#: pool_manager 单例——None 占位 + ``init_pool_manager`` 延迟构造。
pool_manager: PoolManager | None = None


def init_pool_manager(settings: Settings) -> PoolManager:
    """构造 ``PoolManager`` 单例并赋值到模块级 ``pool_manager``。

    在 app lifespan（P0-6 装配）的 ``pool_manager.start()`` 之前调用。
    重复调用覆盖既有单例（不关闭旧实例——调用方负责先 ``await pm.stop()``）。

    Args:
        settings: 全局（或隔离）Settings 实例。

    Returns:
        新构造的 :class:`PoolManager`（同时赋值到模块级 ``pool_manager``）。
    """
    global pool_manager
    pool_manager = PoolManager(settings)
    return pool_manager


def get_pool_manager() -> PoolManager | None:
    """获取当前 ``pool_manager`` 单例（运行时读取，支持 monkeypatch）。

    Returns:
        :class:`PoolManager` 实例；未调用 ``init_pool_manager`` 时为 ``None``。
    """
    return pool_manager

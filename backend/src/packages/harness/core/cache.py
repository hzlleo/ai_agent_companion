"""统一进程内 TTL 缓存装饰器。

P0 决策：**NO Redis**——缓存走进程内实现（单实例部署；多实例阶段再
评审引入分布式缓存）。提供 ``async_ttl_cache`` 装饰器，为 async 函数
提供 TTL 缓存能力（key_func / prefix / TTL / 降级 / 防穿透语义），
后端存储为进程内 LRU+TTL 表。

设计要点
========

1. **key 生成**：``key_func`` 接收与被装饰函数相同的参数，返回缓存 key。
   未提供 ``key_func`` 时使用 ``prefix + args + kwargs`` 哈希。
2. **TTL**：每个调用可指定独立 TTL（``ttl`` 秒，float），过期读判 miss。
3. **序列化**：进程内直接持有对象引用（无跨进程共享——单实例决策的
   自然推论）；值原样存取。
4. **降级**：默认缓存未初始化时直接调用原函数（不缓存），不影响业务
   逻辑；读写异常按 best-effort 吞掉（debug 日志）。
5. **缓存穿透**：``None`` 结果不缓存（避免缓存穿透）。
6. **容量上限**：``max_entries`` 满 FIFO 淘汰最旧条目 + 惰性清除过期
   条目（epoch-ms 判定，见 ``core.time_utils``）。
7. **批量友好**：``delete(*keys)`` 批量删除、``invalidate_pattern``
   按通配 pattern 批量失效（fnmatch 语义）。

用法
==== ::

    from packages.harness.core.cache import async_ttl_cache

    @async_ttl_cache(prefix="persona_detail", ttl=3600)
    async def get_persona_detail(persona_id: str) -> PersonaDetail:
        ...
"""

from __future__ import annotations

import fnmatch
import functools
import hashlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, ParamSpec, TypeVar

from loguru import logger

from packages.harness.core.time_utils import now_ms

__all__ = [
    "MemoryTTLCache",
    "CacheEntry",
    "async_ttl_cache",
    "init_default_cache",
    "get_default_cache",
    "reset_default_cache",
]

P = ParamSpec("P")
T = TypeVar("T")


@dataclass
class CacheEntry:
    """单条缓存记录（值 + 过期时刻，epoch 毫秒）。

    Attributes:
        value: 缓存值（进程内对象引用，原样存取）。
        expire_at_ms: 过期时刻（epoch 毫秒；由 ``core.time_utils.now_ms``
            生成）。
    """

    value: Any
    expire_at_ms: int

    def is_expired(self, now_epoch_ms: int) -> bool:
        """判断条目是否已过期。

        Args:
            now_epoch_ms: 当前 epoch 毫秒。

        Returns:
            ``expire_at_ms <= now_epoch_ms`` 时已过期（TTL=0 即立即过期）。
        """
        return self.expire_at_ms <= now_epoch_ms


class MemoryTTLCache:
    """进程内 TTL 缓存表（单事件循环内使用，无需加锁）。

    容量语义：``max_entries`` 满时先惰性清除过期条目，仍满则 FIFO 淘汰
    最旧条目（dict 保持插入序，首个 key 即最旧）。
    """

    def __init__(self, max_entries: int = 10_000) -> None:
        """初始化缓存表。

        Args:
            max_entries: 最大条目数（下限 1；满时 FIFO 淘汰）。
        """
        self._entries: dict[str, CacheEntry] = {}
        self._max_entries = max(1, max_entries)

    @property
    def max_entries(self) -> int:
        """容量上限。"""
        return self._max_entries

    def _evict_expired(self, now_epoch_ms: int) -> None:
        """惰性清除全部已过期条目。

        Args:
            now_epoch_ms: 当前 epoch 毫秒。
        """
        expired_keys = [
            key for key, entry in self._entries.items() if entry.is_expired(now_epoch_ms)
        ]
        for key in expired_keys:
            del self._entries[key]

    def _evict_if_full(self) -> None:
        """容量满时的淘汰：先清过期，仍满则 FIFO 弹出最旧条目。"""
        self._evict_expired(now_ms())
        while len(self._entries) >= self._max_entries:
            oldest_key = next(iter(self._entries))
            del self._entries[oldest_key]

    async def get(self, key: str) -> Any | None:
        """读取缓存值（过期判 miss 并惰性清除）。

        Args:
            key: 缓存 key。

        Returns:
            缓存值；miss（不存在 / 已过期）返回 ``None``。
        """
        entry = self._entries.get(key)
        if entry is None:
            return None
        if entry.is_expired(now_ms()):
            del self._entries[key]
            return None
        return entry.value

    async def set(self, key: str, value: Any, ttl_seconds: float = 3600.0) -> None:
        """写入缓存值（容量满时先淘汰）。

        Args:
            key: 缓存 key。
            value: 缓存值（``None`` 由装饰器层拦截不写入——防穿透；
                直接调用本方法时按调用方语义写入）。
            ttl_seconds: TTL 秒数（float；``0`` 即立即过期——测试用）。
        """
        if len(self._entries) >= self._max_entries:
            self._evict_if_full()
        expire_at_ms = now_ms() + int(ttl_seconds * 1000)
        self._entries[key] = CacheEntry(value=value, expire_at_ms=expire_at_ms)

    async def delete(self, *keys: str) -> int:
        """批量删除缓存 key（幂等：不存在的 key 忽略）。

        Args:
            *keys: 待删除的缓存 key 列表（批量友好——一次调用清多键）。

        Returns:
            实际删除的条目数。
        """
        deleted = 0
        for key in keys:
            if self._entries.pop(key, None) is not None:
                deleted += 1
        return deleted

    async def clear(self) -> None:
        """清空全部缓存条目。"""
        self._entries.clear()

    async def size(self) -> int:
        """当前条目数（含尚未惰性清除的过期条目）。

        Returns:
            表内条目数。
        """
        return len(self._entries)

    async def invalidate_pattern(self, pattern: str) -> int:
        """按通配 pattern 批量失效缓存 key（fnmatch 语义）。

        批量友好：一次调用清一族 key（如 ``"persona_detail:*"``）。

        Args:
            pattern: fnmatch 通配 pattern（``*`` / ``?`` / ``[seq]``）。

        Returns:
            实际删除的条目数。
        """
        matched = [key for key in self._entries if fnmatch.fnmatchcase(key, pattern)]
        for key in matched:
            del self._entries[key]
        return len(matched)


# ---------------------------------------------------------------------------
# 默认缓存单例（装饰器消费；app lifespan 经 init_default_cache 装配）
# ---------------------------------------------------------------------------

_default_cache: MemoryTTLCache | None = None


def init_default_cache(max_entries: int = 10_000) -> MemoryTTLCache:
    """初始化默认缓存单例（幂等——已初始化时返回既有实例）。

    Args:
        max_entries: 最大条目数。

    Returns:
        默认 ``MemoryTTLCache`` 实例。
    """
    global _default_cache
    if _default_cache is None:
        _default_cache = MemoryTTLCache(max_entries=max_entries)
    return _default_cache


def get_default_cache() -> MemoryTTLCache | None:
    """返回默认缓存单例（未初始化时为 ``None``——装饰器降级直通）。

    Returns:
        默认 ``MemoryTTLCache``；未初始化返回 ``None``。
    """
    return _default_cache


def reset_default_cache() -> None:
    """重置默认缓存单例（测试隔离用——清空并置 None）。"""
    global _default_cache
    _default_cache = None


def _default_key_func(prefix: str, args: tuple[Any, ...], kwargs: dict[str, Any]) -> str:
    """默认 key 生成：``prefix:sha256(args + kwargs)``。

    Args:
        prefix: key 前缀。
        args: 被装饰函数的位置参数。
        kwargs: 被装饰函数的关键字参数。

    Returns:
        ``{prefix}:{sha256 前 32 位}`` 形式的缓存 key。
    """
    raw = json.dumps(
        {"args": list(args), "kwargs": kwargs},
        default=str,
        sort_keys=True,
        ensure_ascii=False,
    )
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]
    return f"{prefix}:{digest}"


class AsyncTTLCache:
    """进程内 TTL 缓存装饰器实例。

    通过 ``async_ttl_cache(prefix=..., ttl=...)`` 创建，作用于 async 函数。
    """

    def __init__(
        self,
        prefix: str,
        ttl: float = 3600.0,
        key_func: Callable[..., str] | None = None,
    ) -> None:
        """初始化装饰器参数。

        Args:
            prefix: 缓存 key 前缀（如 ``"persona_detail"``）。
            ttl: 缓存 TTL 秒数（默认 3600=1h；下限 0——0 即立即过期）。
            key_func: 自定义 key 生成函数；None 时使用 ``prefix:sha256(args)``。
        """
        self.prefix = prefix
        self.ttl = max(0.0, float(ttl))
        self.key_func = key_func

    def __call__(self, func: Callable[P, Awaitable[T]]) -> Callable[P, Awaitable[T]]:
        """装饰 async 函数，添加进程内 TTL 缓存层。"""

        @functools.wraps(func)
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> Any:
            # 生成 cache key
            if self.key_func is not None:
                cache_key = self.key_func(*args, **kwargs)
            else:
                cache_key = _default_key_func(self.prefix, args=args, kwargs=kwargs)

            cache = get_default_cache()
            if cache is not None:
                try:
                    cached = await cache.get(cache_key)
                except Exception as exc:  # best-effort: 缓存读失败不阻断业务
                    logger.opt(exception=True).debug(
                        "缓存读取失败: key={}, err={}", cache_key, exc
                    )
                    cached = None

                if cached is not None:
                    return cached

            result = await func(*args, **kwargs)

            # 防穿透：None 结果不缓存
            if result is not None and cache is not None:
                try:
                    await cache.set(cache_key, result, ttl_seconds=self.ttl)
                except Exception as exc:  # best-effort: 缓存写失败不阻断业务
                    logger.opt(exception=True).debug(
                        "缓存写入失败: key={}, err={}", cache_key, exc
                    )

            return result

        return wrapper


def async_ttl_cache(
    prefix: str,
    ttl: float = 3600.0,
    key_func: Callable[..., str] | None = None,
) -> AsyncTTLCache:
    """便捷工厂——创建进程内 TTL 缓存装饰器。

    Args:
        prefix: 缓存 key 前缀（如 ``"persona_detail"``）。
        ttl: 缓存 TTL 秒数（默认 3600=1h）。
        key_func: 自定义 key 生成函数；None 时使用 ``prefix:sha256(args)``。

    Returns:
        :class:`AsyncTTLCache` 装饰器实例。
    """
    return AsyncTTLCache(
        prefix=prefix,
        ttl=ttl,
        key_func=key_func,
    )

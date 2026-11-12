"""``core.cache`` 单元测试——进程内 TTL 缓存 + 装饰器（NO Redis，P0 决策）。

覆盖矩阵：

    - ``MemoryTTLCache``：get/set / TTL 过期（含 ttl=0 立即过期）/
      批量 delete / clear / size / invalidate_pattern 通配 / FIFO 淘汰 /
      惰性过期清除；
    - 默认单例：init 幂等 / reset 复位；
    - ``async_ttl_cache`` 装饰器：命中 / miss 回源 / None 不缓存（防穿透）/
      自定义 key_func / 未装配默认缓存时降级直通 / 不同参数不同 key /
      pattern 批量失效。
"""

from __future__ import annotations

import pytest

from packages.harness.core.cache import (
    AsyncTTLCache,
    CacheEntry,
    MemoryTTLCache,
    async_ttl_cache,
    get_default_cache,
    init_default_cache,
    reset_default_cache,
)


class TestCacheEntry:
    async def test_expiry_boundary(self) -> None:
        """过期判定：``expire_at_ms <= now`` 即过期（TTL=0 立即过期）。"""
        entry = CacheEntry(value="v", expire_at_ms=1_000)
        assert entry.is_expired(999) is False
        assert entry.is_expired(1_000) is True
        assert entry.is_expired(1_001) is True


class TestMemoryTTLCache:
    async def test_set_then_get(self, memory_cache: MemoryTTLCache) -> None:
        """写入后读取命中。"""
        await memory_cache.set("k1", {"a": 1})
        assert await memory_cache.get("k1") == {"a": 1}

    async def test_get_missing_returns_none(self, memory_cache: MemoryTTLCache) -> None:
        """miss 返回 None。"""
        assert await memory_cache.get("no-such-key") is None

    async def test_ttl_zero_expires_immediately(self, memory_cache: MemoryTTLCache) -> None:
        """ttl_seconds=0 → 读时立即过期（确定性过期测试——无 sleep）。"""
        await memory_cache.set("k1", "v", ttl_seconds=0)
        assert await memory_cache.get("k1") is None

    async def test_expired_entry_lazily_removed(self, memory_cache: MemoryTTLCache) -> None:
        """过期条目读时惰性清除（size 下降）。"""
        await memory_cache.set("k1", "v", ttl_seconds=0)
        await memory_cache.get("k1")
        assert await memory_cache.size() == 0

    async def test_overwrite_same_key(self, memory_cache: MemoryTTLCache) -> None:
        """同 key 覆盖写（取后值）。"""
        await memory_cache.set("k1", "old")
        await memory_cache.set("k1", "new")
        assert await memory_cache.get("k1") == "new"
        assert await memory_cache.size() == 1

    async def test_delete_batch_counts(self, memory_cache: MemoryTTLCache) -> None:
        """批量删除：返回实际删除数（幂等——不存在的 key 忽略）。"""
        for i in range(4):
            await memory_cache.set(f"k{i}", i)
        deleted = await memory_cache.delete("k0", "k1", "missing")
        assert deleted == 2
        assert await memory_cache.size() == 2

    async def test_clear(self, memory_cache: MemoryTTLCache) -> None:
        """clear 清空全部。"""
        await memory_cache.set("k1", 1)
        await memory_cache.set("k2", 2)
        await memory_cache.clear()
        assert await memory_cache.size() == 0

    async def test_invalidate_pattern(self, memory_cache: MemoryTTLCache) -> None:
        """pattern 批量失效（fnmatch 通配，批量友好）。"""
        await memory_cache.set("persona:1", "a")
        await memory_cache.set("persona:2", "b")
        await memory_cache.set("chat:1", "c")
        deleted = await memory_cache.invalidate_pattern("persona:*")
        assert deleted == 2
        assert await memory_cache.get("persona:1") is None
        assert await memory_cache.get("chat:1") == "c"

    async def test_invalidate_pattern_no_match(self, memory_cache: MemoryTTLCache) -> None:
        """pattern 无匹配返回 0。"""
        await memory_cache.set("k1", "v")
        assert await memory_cache.invalidate_pattern("nope:*") == 0

    async def test_fifo_eviction_when_full(self) -> None:
        """容量满：先清过期，仍满则 FIFO 弹最旧。"""
        cache = MemoryTTLCache(max_entries=2)
        await cache.set("oldest", 1, ttl_seconds=3600)
        await cache.set("middle", 2, ttl_seconds=3600)
        await cache.set("newest", 3, ttl_seconds=3600)
        assert await cache.get("oldest") is None
        assert await cache.get("middle") == 2
        assert await cache.get("newest") == 3

    async def test_expired_evicted_before_fifo(self) -> None:
        """容量满时优先清除过期条目（未过期条目不被误杀）。"""
        cache = MemoryTTLCache(max_entries=2)
        await cache.set("expired", 1, ttl_seconds=0)
        await cache.set("alive-1", 2, ttl_seconds=3600)
        # 触发 _evict_if_full：先清 expired，无需 FIFO 弹出 alive-1
        await cache.set("alive-2", 3, ttl_seconds=3600)
        assert await cache.get("alive-1") == 2
        assert await cache.get("alive-2") == 3

    async def test_max_entries_lower_clamped(self) -> None:
        """max_entries < 1 夹逼为 1。"""
        cache = MemoryTTLCache(max_entries=0)
        assert cache.max_entries == 1


class TestDefaultCacheSingleton:
    async def test_init_returns_singleton(self) -> None:
        """init 幂等——重复调用返回同一实例。"""
        first = init_default_cache(max_entries=10)
        second = init_default_cache(max_entries=99)
        assert first is second
        reset_default_cache()

    async def test_reset_clears_singleton(self) -> None:
        """reset 后单例置 None（get 返回 None → 装饰器降级）。"""
        init_default_cache(max_entries=10)
        reset_default_cache()
        assert get_default_cache() is None


class TestAsyncTTLCacheDecorator:
    async def test_caches_result(self, default_cache: MemoryTTLCache) -> None:
        """命中路径：第二次调用不回源。"""
        calls: list[int] = []

        @async_ttl_cache(prefix="double", ttl=3600)
        async def _double(x: int) -> int:
            calls.append(x)
            return x * 2

        assert await _double(3) == 6
        assert await _double(3) == 6
        assert calls == [3]

    async def test_none_result_not_cached(self, default_cache: MemoryTTLCache) -> None:
        """防穿透：None 结果不缓存（每次都回源）。"""
        calls: list[int] = []

        @async_ttl_cache(prefix="nullable", ttl=3600)
        async def _maybe(x: int) -> int | None:
            calls.append(x)
            return None

        assert await _maybe(1) is None
        assert await _maybe(1) is None
        assert calls == [1, 1]

    async def test_distinct_args_distinct_keys(self, default_cache: MemoryTTLCache) -> None:
        """不同参数 → 不同 key（互不污染）。"""

        @async_ttl_cache(prefix="echo", ttl=3600)
        async def _echo(x: str) -> str:
            return x

        assert await _echo("a") == "a"
        assert await _echo("b") == "b"

    async def test_custom_key_func(self, default_cache: MemoryTTLCache) -> None:
        """自定义 key_func：明文维度进 key（pattern 失效可覆盖）。"""

        def _key(persona_id: str) -> str:
            return f"persona_detail:{persona_id}"

        @async_ttl_cache(prefix="persona_detail", ttl=3600, key_func=_key)
        async def _detail(persona_id: str) -> dict[str, str]:
            return {"persona_id": persona_id}

        assert await _detail("p1") == {"persona_id": "p1"}
        assert await _detail("p2") == {"persona_id": "p2"}

        deleted = await default_cache.invalidate_pattern("persona_detail:*")
        assert deleted == 2
        # 失效后回源重建
        assert await _detail("p1") == {"persona_id": "p1"}

    async def test_degrades_without_default_cache(self) -> None:
        """默认缓存未装配 → 直通原函数（不缓存、不报错）。"""
        reset_default_cache()
        calls: list[int] = []

        @async_ttl_cache(prefix="degraded", ttl=3600)
        async def _add(x: int) -> int:
            calls.append(x)
            return x + 1

        assert await _add(1) == 2
        assert await _add(1) == 2
        assert calls == [1, 1]
        assert get_default_cache() is None

    async def test_ttl_expiry_hits_source(self, default_cache: MemoryTTLCache) -> None:
        """TTL 过期后回源（ttl=0 确定性触发——无 sleep）。"""
        calls: list[int] = []

        @async_ttl_cache(prefix="expired", ttl=0)
        async def _value() -> int:
            calls.append(1)
            return 99

        assert await _value() == 99
        assert await _value() == 99
        assert calls == [1, 1]

    async def test_decorator_instance_type(self) -> None:
        """工厂返回 AsyncTTLCache 实例。"""
        decorator = async_ttl_cache(prefix="x")
        assert isinstance(decorator, AsyncTTLCache)
        assert decorator.ttl == 3600.0

    async def test_negative_ttl_clamped_zero(self) -> None:
        """负 TTL 夹逼 0（立即过期语义）。"""
        decorator = async_ttl_cache(prefix="x", ttl=-5)
        assert decorator.ttl == 0.0

    async def test_degrades_on_cache_read_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """缓存读异常 best-effort 吞掉 → 直通原函数（不阻断业务）。"""
        import packages.harness.core.cache as cache_module

        class _BrokenReadCache(MemoryTTLCache):
            async def get(self, key: str) -> object:
                raise RuntimeError("cache-read-boom")

        monkeypatch.setattr(cache_module, "_default_cache", _BrokenReadCache())
        calls: list[int] = []

        @async_ttl_cache(prefix="broken-read", ttl=3600)
        async def _compute(x: int) -> int:
            calls.append(x)
            return x * 10

        assert await _compute(2) == 20
        assert await _compute(2) == 20
        assert calls == [2, 2]

    async def test_degrades_on_cache_write_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """缓存写异常 best-effort 吞掉 → 结果照常返回（不阻断业务）。"""
        import packages.harness.core.cache as cache_module

        class _BrokenWriteCache(MemoryTTLCache):
            async def set(self, key: str, value: object, ttl_seconds: float = 3600.0) -> None:
                raise RuntimeError("cache-write-boom")

        monkeypatch.setattr(cache_module, "_default_cache", _BrokenWriteCache())

        @async_ttl_cache(prefix="broken-write", ttl=3600)
        async def _compute(x: int) -> int:
            return x + 1

        assert await _compute(41) == 42

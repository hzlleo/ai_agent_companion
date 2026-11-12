"""``core.context`` 单元测试——ContextVars 轴 / 快照 / 作用域绑定 / 日志字段。

覆盖矩阵：

    - 各 ContextVar 默认值；
    - ``current_context`` 快照组合；
    - ``bind_context``：覆盖 / 嵌套合并 / 显式 None 清空 / 退出恢复 /
      并发任务隔离；
    - ``context_log_fields``：占位符形态与实值形态。
"""

from __future__ import annotations

import asyncio

from packages.harness.core.context import (
    RequestContext,
    bind_context,
    context_log_fields,
    current_context,
    locale_var,
    persona_id_var,
    request_id_var,
    session_id_var,
    user_id_var,
)


class TestContextVarDefaults:
    async def test_defaults(self) -> None:
        """缺省：追踪/用户/会话/人格 None，locale zh-CN。"""
        assert request_id_var.get() is None
        assert user_id_var.get() is None
        assert session_id_var.get() is None
        assert persona_id_var.get() is None
        assert locale_var.get() == "zh-CN"

    async def test_current_context_defaults(self) -> None:
        """快照缺省形态（RequestContext 默认值组合）。"""
        assert current_context() == RequestContext()


class TestBindContext:
    async def test_overrides_specified_fields(self) -> None:
        """显式字段覆盖，未指定字段继承外层。"""
        async with bind_context(request_id="req-1", user_id="u-1") as ctx:
            assert ctx.request_id == "req-1"
            assert ctx.user_id == "u-1"
            assert ctx.session_id is None
            assert request_id_var.get() == "req-1"
            assert user_id_var.get() == "u-1"

    async def test_restores_outer_context_on_exit(self) -> None:
        """作用域退出恢复外层值（逐 var 复位）。"""
        token = session_id_var.set("outer-session")
        try:
            async with bind_context(session_id="inner-session"):
                assert session_id_var.get() == "inner-session"
            assert session_id_var.get() == "outer-session"
        finally:
            session_id_var.reset(token)

    async def test_nested_scopes_merge(self) -> None:
        """嵌套作用域合并：内层仅覆盖显式字段，其余继承外层。"""
        async with bind_context(request_id="r1", persona_id="p1"):
            async with bind_context(persona_id="p2") as inner:
                assert inner.request_id == "r1"
                assert inner.persona_id == "p2"
            outer_after = current_context()
            assert outer_after.request_id == "r1"
            assert outer_after.persona_id == "p1"

    async def test_explicit_none_clears_inherited_field(self) -> None:
        """显式传 None 清空继承字段（对齐 usage_scope 合并语义）。"""
        async with bind_context(persona_id="p1"), bind_context(persona_id=None) as inner:
            assert inner.persona_id is None
            assert persona_id_var.get() is None

    async def test_locale_override_and_restore(self) -> None:
        """locale 轴覆盖与恢复。"""
        async with bind_context(locale="en"):
            assert locale_var.get() == "en"
        assert locale_var.get() == "zh-CN"

    async def test_concurrent_task_isolation(self) -> None:
        """并发任务隔离：各任务作用域互不泄漏（ContextVars 复制语义）。"""

        async def _scoped(name: str, gate: asyncio.Event) -> str:
            async with bind_context(session_id=name):
                await gate.wait()
                return current_context().session_id or ""

        gate = asyncio.Event()
        task_a = asyncio.create_task(_scoped("session-a", gate))
        task_b = asyncio.create_task(_scoped("session-b", gate))
        await asyncio.sleep(0)
        gate.set()
        results = await asyncio.gather(task_a, task_b)
        assert results == ["session-a", "session-b"]
        assert session_id_var.get() is None


class TestContextLogFields:
    async def test_placeholder_when_unset(self) -> None:
        """未设置轴输出 ``-`` 占位（字段恒在，聚合系统可解析）。"""
        fields = context_log_fields()
        assert fields == {
            "request_id": "-",
            "session_id": "-",
            "user_id": "-",
            "persona_id": "-",
        }

    async def test_real_values_when_bound(self) -> None:
        """绑定后输出实值。"""
        async with bind_context(
            request_id="req-9", session_id="s-9", user_id="u-9", persona_id="p-9"
        ):
            assert context_log_fields() == {
                "request_id": "req-9",
                "session_id": "s-9",
                "user_id": "u-9",
                "persona_id": "p-9",
            }

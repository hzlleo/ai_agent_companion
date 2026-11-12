"""core 组件协作集成测试——跨模块全链路（无 HTTP 面，P0-2 阶段的真实表面）。

链路形态（组件级集成——app 层未落地前的最大真实表面）：

    logging 装配（fixture 显式构造 LoggingConfig，零真实配置）
      → bind_context 绑定会话/人格上下文
      → logger.contextualize(**context_log_fields()) 写日志（文本 + JSON sink）
      → TaskQueue 提交携带上下文快照的任务（worker 侧 ctx.run 恢复）
      → 任务内经 run_blocking_ops 下沉阻塞操作（共享线程池路径）
      → 任务结果写 async_ttl_cache 缓存
      → 停机收口（TaskQueue.stop drain + 管理器 shutdown）

断言面：JSON 日志行的上下文字段 / 脱敏、任务终态与结果、缓存命中、
上下文隔离复位。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from loguru import logger

from packages.harness.core.background_tasks import BackgroundTaskManager, TaskQueue
from packages.harness.core.cache import init_default_cache, reset_default_cache
from packages.harness.core.context import bind_context, context_log_fields
from packages.harness.core.enums import TaskStatus
from packages.harness.core.logging import setup_logging
from packages.harness.core.logging_models import LoggingConfig
from packages.harness.core.thread_pool import (
    init_shared_thread_pool,
    run_blocking_ops,
    shutdown_shared_thread_pool,
)


def _blocking_upper(text: str) -> str:
    """模拟无法 async 化的阻塞操作（测试辅助——CPU 密集形态）。"""
    return text.upper()


@pytest.fixture
def integration_logging_config(tmp_path: Path) -> LoggingConfig:
    """集成链路日志配置（DEBUG 级——覆盖任务内 debug 日志）。"""
    return LoggingConfig(
        level="DEBUG",
        log_file=str(tmp_path / "logs" / "integration.log"),
        log_json_file=str(tmp_path / "logs" / "integration.json.log"),
    )


class TestCoreWiringIntegration:
    async def test_context_logging_queue_pool_cache_chain(
        self, integration_logging_config: LoggingConfig
    ) -> None:
        """全链路：上下文 → 日志（脱敏 + JSON 字段）→ 队列任务 → 线程池 → 缓存。"""
        setup_logging(integration_logging_config)
        init_shared_thread_pool(max_workers=2)
        cache = init_default_cache(max_entries=100)
        try:
            queue = TaskQueue(workers=2)
            await queue.start()
            mgr = BackgroundTaskManager()

            async with bind_context(
                request_id="req-integration",
                session_id="session-integration",
                user_id="user-42",
                persona_id="persona-fish",
            ):
                with logger.contextualize(**context_log_fields()):
                    logger.info("链路开始：提交缓存预热任务（api_key=sk-integration123456）")

                    async def _warm_cache(key: str, value: str) -> str:
                        upper = await run_blocking_ops(_blocking_upper, value)
                        await cache.set(f"warm:{key}", upper, ttl_seconds=3600)
                        logger.debug("任务内完成缓存预热: key={}", key)
                        return upper

                    task_id = await queue.submit("warm-cache", _warm_cache, "k1", "payload")
                    # 队列停机任务注册进管理器——shutdown 宽限期等待队列排空
                    mgr.register(asyncio.create_task(queue.stop(timeout=5.0)))
                    await mgr.shutdown(timeout=5.0)

            record = queue.get_status(task_id)
            assert record is not None
            assert record.status == TaskStatus.COMPLETED
            assert record.result == "PAYLOAD"
            assert await cache.get("warm:k1") == "PAYLOAD"

            # 上下文作用域已退出——轴复位
            assert context_log_fields()["session_id"] == "-"

            json_file = Path(integration_logging_config.log_json_file)
            lines = [
                ln
                for ln in json_file.read_text(encoding="utf-8").splitlines()
                if ln.startswith("{")
            ]
            payloads = [json.loads(ln) for ln in lines]
            # JSON 行携带四上下文轴实值
            ctx_lines = [p for p in payloads if p.get("request_id") == "req-integration"]
            assert ctx_lines, "JSON sink 应含带 request_id 的日志行"
            assert all(p["session_id"] == "session-integration" for p in ctx_lines)
            assert all(p["persona_id"] == "persona-fish" for p in ctx_lines)
            # 脱敏在 JSON sink 生效
            assert all("sk-integration123456" not in p["message"] for p in payloads)
            assert any("api_key=***" in p["message"] for p in payloads)
        finally:
            reset_default_cache()
            shutdown_shared_thread_pool(wait=True)

    async def test_failed_task_degrades_gracefully(
        self, integration_logging_config: LoggingConfig
    ) -> None:
        """失败链路：任务异常 → failed 终态 + error 落位（链路不崩）。"""
        setup_logging(integration_logging_config)
        queue = TaskQueue(workers=1)
        await queue.start()
        try:

            async def _explode() -> None:
                raise RuntimeError("integration-boom")

            task_id = await queue.submit("explode", _explode)
            await queue.stop(timeout=5.0)

            record = queue.get_status(task_id)
            assert record is not None
            assert record.status == TaskStatus.FAILED
            assert "integration-boom" in (record.error or "")

            json_file = Path(integration_logging_config.log_json_file)
            content = json_file.read_text(encoding="utf-8")
            assert "integration-boom" in content
        finally:
            if queue.started:
                await queue.stop(timeout=5.0)

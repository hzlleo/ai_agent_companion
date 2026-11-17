"""SeekDB 真引擎强制套件根 conftest——显式测试服务目标解析 + scratch 库 fixtures。

套件定位（替代原 ``src/tests/integration/test_seekdb_smoke.py`` 的 env
门控冒烟——skipif 语义废弃，真引擎验证升级为强制面）：

    - **无跳过策略**：``SEEKDB_TEST_URL`` 缺失即 ``pytest.fail``
      （fail-fast 消息携带本地容器启动命令与 CI 说明）——CI 的 backend
      job 经 service container 自动提供，本地须先起容器（命令见
      ``pixi run tests-seekdb`` 任务注释 / docs/test-strategy/README.md）；
    - **显式测试服务配置**（约束 1.6）：本套件只读 runner 注入的
      ``SEEKDB_TEST_*`` env，不读 ``src/conftest.py`` 的 S3 硬覆盖 env
      面（``settings`` / ``SEEKDB_*`` 应用配置）——docker 化测试依赖 +
      显式测试 env 是本工程认可的测试服务模式（同 MySQL service
      container），非「真实项目配置」；
    - **hermetic-per-run**：scratch 库（默认 ``aac_ci_verify``）在每
      测试模块的 fixture setup 期 DROP+CREATE 重建（模块间状态零耦合
      ——收集序无关）；单进程顺序运行，同名库重建无并行冲突。

fixture 架构（pytest-asyncio function 级循环下的宽作用域安全——同步
fixture + ``asyncio.run`` 内联，与 ``tests/conftest.py`` 的
``shared_db_engine`` 同模式；跨测试不持有任何 loop 绑定资源）：

    - ``seekdb_target``（session）——env 解析 + fail-fast 门；
    - ``seekdb_scratch``（module）——scratch 库 DROP+CREATE / teardown
      best-effort DROP；
    - ``migrated_scratch``（module）——scratch + alembic upgrade head
      （并发不变式等模块的公共前置）；
    - ``run_alembic``（module）——异步测试用的 alembic 命令运行器
      （``asyncio.to_thread`` 桥接——env.py 内部 ``asyncio.run`` 不能
      进运行中事件循环）。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterator

import pytest

from tests.integration_seekdb import support
from tests.integration_seekdb.support import SeekDbTestTarget


@pytest.fixture(scope="session")
def seekdb_target() -> SeekDbTestTarget:
    """解析显式测试服务目标（``SEEKDB_TEST_*`` env——缺失即 fail）。

    Returns:
        连接目标（host / port / user / password / database）。

    Raises:
        Failed: ``SEEKDB_TEST_URL`` 缺失或形态非法（pytest.fail——
            消息携带容器启动命令；不静默跳过）。
    """
    try:
        return support.resolve_seekdb_test_target()
    except ValueError as exc:
        pytest.fail(str(exc), pytrace=False)


@pytest.fixture(scope="module")
def seekdb_scratch(seekdb_target: SeekDbTestTarget) -> Iterator[SeekDbTestTarget]:
    """模块级 scratch 库（setup DROP+CREATE——全新空库，teardown 删除）。

    每测试模块独立重建（hermetic-per-run 的模块粒度实现——模块间零
    耦合，收集序无关）。asyncio.run 内联执行（同步 fixture 约定——
    与 tests/conftest.py 的 shared_db_engine 同模式，无 loop 绑定资源
    跨测试持有）。

    Args:
        seekdb_target: 显式测试服务目标。

    Yields:
        目标（scratch 库已就绪——空库，未迁移）。
    """
    asyncio.run(support.recreate_scratch_database(seekdb_target))
    yield seekdb_target
    asyncio.run(support.drop_scratch_database(seekdb_target))


@pytest.fixture(scope="module")
def migrated_scratch(seekdb_scratch: SeekDbTestTarget) -> SeekDbTestTarget:
    """迁移到 head 的 scratch 库（并发不变式模块的公共前置）。

    进程内 alembic command 直调（同步 fixture setup 期无运行中事件
    循环——env.py 内部 asyncio.run 可用；DATABASE_* env + settings
    单例双通道指向 scratch 库，见 ``support.run_alembic_command``）。

    Args:
        seekdb_scratch: scratch 库目标（空库）。

    Returns:
        目标（scratch 库已升级到 head——8 张域表 + alembic_version）。
    """
    support.run_alembic_command(seekdb_scratch, "head")
    return seekdb_scratch


@pytest.fixture(scope="module")
def run_alembic(
    seekdb_scratch: SeekDbTestTarget,
) -> Callable[..., Awaitable[None]]:
    """进程内 alembic 命令运行器（异步面——迁移链测试驱动升降级）。

    异步测试上下文已有运行中事件循环，env.py 的在线迁移在命令内部
    ``asyncio.run``——须经 ``asyncio.to_thread`` 在工作线程执行。

    Args:
        seekdb_scratch: scratch 库目标（命令指向该库）。

    Returns:
        异步运行器 ``async def(revision, *, downgrade=False) -> None``
        （参数语义见 ``support.run_alembic_command``）。
    """
    target = seekdb_scratch

    async def _run(revision: str, *, downgrade: bool = False) -> None:
        await asyncio.to_thread(support.run_alembic_command, target, revision, downgrade=downgrade)

    return _run

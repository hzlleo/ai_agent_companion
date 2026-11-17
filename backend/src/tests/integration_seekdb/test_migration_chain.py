"""迁移链真引擎 round-trip（SeekDB MySQL 方言——0001→0008 全链升 / 降级）。

验证面（SQLite 方言永不暴露、只有真引擎能锁定的迁移序语义）：

    - 升级 head 后 ``SHOW TABLES`` 收敛到 9 表集（``alembic_version`` +
      8 张 ``ff_*`` 域表）且版本戳为 head（0008）；
    - **降级 base**：MySQL 系引擎（含 SeekDB）要求外键列必须保有索引
      ——降级须先删外键约束再删其索引，否则报错误 5320（「索引被外键
      占用」）；SQLite 方言无此约束，该序错误在单测面永不暴露。本
      round-trip 即该 dialect bug 的制度化回归；
    - 降级 base 后域表全删（``alembic_version`` 表保留——live 实测
      形态），版本戳清空；
    - **复升 head**：降级零残留的完整性证明（round-trip 闭环）。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import pytest

from tests.integration_seekdb import support
from tests.integration_seekdb.support import SeekDbTestTarget

#: 套件选择 marker（pyproject markers 注册；显式路径定向运行为主入口）。
pytestmark = [pytest.mark.seekdb]


async def test_migration_chain_round_trip(
    seekdb_scratch: SeekDbTestTarget,
    run_alembic: Callable[..., Awaitable[None]],
) -> None:
    """升级 head（9 表集 + 版本戳）→ 降级 base（5320 方言面）→ 复升 head。

    Args:
        seekdb_scratch: scratch 库目标（空库起步——模块 fixture 已重建）。
        run_alembic: 进程内 alembic 命令运行器（conftest 提供——
            ``asyncio.to_thread`` 桥接的异步 callable）。
    """
    # 1. 升级 head：0001→0008 全链（env.py 异步引擎在线迁移）。
    await run_alembic("head")
    tables_upgraded = await support.fetch_table_names(seekdb_scratch)
    assert tables_upgraded == support.EXPECTED_HEAD_TABLES
    assert await support.fetch_alembic_version(seekdb_scratch) == support.HEAD_REVISION

    # 2. 降级 base：0008→0001 逆向链——错误 5320 的暴露面（FK 列须保有
    #    索引：先删约束再删索引；SQLite 永不复现，真引擎强制验证）。
    await run_alembic("base", downgrade=True)
    tables_downgraded = await support.fetch_table_names(seekdb_scratch)
    assert tables_downgraded == {"alembic_version"}  # 域表全删；版本表保留（live 实测）
    assert await support.fetch_alembic_version(seekdb_scratch) is None

    # 3. 复升 head：降级零残留的完整性证明（round-trip 闭环）。
    await run_alembic("head")
    tables_reupgraded = await support.fetch_table_names(seekdb_scratch)
    assert tables_reupgraded == support.EXPECTED_HEAD_TABLES
    assert await support.fetch_alembic_version(seekdb_scratch) == support.HEAD_REVISION

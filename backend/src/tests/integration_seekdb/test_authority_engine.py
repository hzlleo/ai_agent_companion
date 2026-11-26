"""单栈 SeekDB 权威引擎真引擎验证——init_database + 迁移 head + 人格 round-trip。

生产形态单栈：权威库与检索库同实例、不同库名（见 docs/backend/database.md
「单栈 SeekDB」）。本用例以 ``DatabaseConfig(type=mysql, host=真引擎, port=真
引擎端口, name=专用 scratch 库)`` 走 ``init_database``（引擎 / 会话工厂 / 建表
fallback 全链）与 Alembic 升级到 head，再做一次人格创建 / 读取 / 删除的
round-trip——证明权威存储引擎实际跑在 SeekDB 上，而非仅连接池冒烟。

隔离边界：使用专用 scratch 库（``aac_authority_verify``），不触碰套件库
（``SEEKDB_TEST_DATABASE``）；结束后恢复进程级引擎持有器槽位（不泄漏给其他
用例）。缺 ``SEEKDB_TEST_URL`` 时按套件策略 ``pytest.fail``（不 skip）。
"""

from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest
from pydantic import SecretStr

from packages.harness.config.models import DatabaseConfig
from packages.harness.core.errors import BusinessException, ErrorCode
from packages.harness.database.engine import (
    close_database,
    db_engine_holder,
    init_database,
)
from packages.harness.database.models import load_all_models
from packages.harness.database.session import session_scope
from packages.harness.persona.chara_card import CharaCardModel
from packages.harness.persona.dtos import PersonaCreateDTO
from packages.harness.persona.service import PersonaService
from tests.integration_seekdb import support
from tests.integration_seekdb.support import SeekDbTestTarget

pytestmark = [pytest.mark.seekdb]

#: 本用例专用 scratch 库（与套件库分离——不触碰套件数据面）。
_SCRATCH_DATABASE = "aac_authority_verify"


async def test_authority_engine_runs_on_seekdb(seekdb_target: SeekDbTestTarget) -> None:
    """权威引擎（init_database + 迁移 head）跑在 SeekDB 并完成人格 round-trip。

    Args:
        seekdb_target: 显式测试服务目标（缺 ``SEEKDB_TEST_URL`` 即 fail）。
    """
    target = replace(seekdb_target, database=_SCRATCH_DATABASE)
    await support.recreate_scratch_database(target)
    previous = (
        db_engine_holder.async_engine,
        db_engine_holder.session_factory,
        db_engine_holder.owner_token,
    )
    try:
        await asyncio.to_thread(support.run_alembic_command, target, "head")
        load_all_models()
        config = DatabaseConfig(
            type="mysql",
            host=target.host,
            port=target.port,
            name=target.database,
            user=target.user,
            password=SecretStr(target.password),
        )
        await init_database(config)
        try:
            service = PersonaService()
            async with session_scope() as session:
                record = await service.create(
                    session,
                    PersonaCreateDTO(
                        name="seekdb-authority",
                        card=CharaCardModel(
                            name="单栈验证",
                            personality="温和",
                            first_mes="你好",
                        ),
                    ),
                )
            assert record.id > 0
            async with session_scope() as session:
                loaded = await service.get(session, record.id)
            assert loaded.name == "seekdb-authority"
            async with session_scope() as session:
                await service.delete(session, record.id)
            async with session_scope() as session:
                with pytest.raises(BusinessException) as exc_info:
                    await service.get(session, record.id)
            assert exc_info.value.code == ErrorCode.NOT_FOUND
        finally:
            await close_database()
            (
                db_engine_holder.async_engine,
                db_engine_holder.session_factory,
                db_engine_holder.owner_token,
            ) = previous
    finally:
        await support.drop_scratch_database(target)

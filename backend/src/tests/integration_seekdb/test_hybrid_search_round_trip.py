"""混合检索全链路 round-trip（SeekDBStore 生产路径——真引擎端到端契约）。

自原 ``src/tests/integration/test_seekdb_smoke.py``（env 门控冒烟）整体
迁入——skipif 语义废弃，验证面升级为强制（缺 ``SEEKDB_TEST_URL`` 即
``pytest.fail``，见套件 conftest）。

链路（SeekDBStore 全语义真实验证——单测假池面无法覆盖的端到端契约）：

    ensure_schema（DDL 建表——VECTOR / ik 全文 / hnsw / HEAP）→
    upsert（ON DUPLICATE KEY 批量——运行期语法验证）→ keyword 检索
    回读 → hybrid 检索回读（Form B——DBMS_HYBRID_SEARCH.SEARCH 官方
    融合排序，``_score`` 越大越相关）→ 同键覆盖幂等 → 人格隔离谓词 →
    delete 清理。

aiomysql 驱动选型的真实验证面（官方仅文档化 PyMySQL / mysqlclient——
风险与回退文档化于 docs/backend/memory.md 检索层）。
"""

from __future__ import annotations

import pytest

from packages.harness.memory.retrieval_models import (
    MemoryIndexEntryDTO,
    MemorySearchMode,
    MemorySearchQueryDTO,
    SeekDBStoreConfig,
)
from packages.harness.memory.seekdb_store import SeekDBStore
from tests.integration_seekdb.support import SeekDbTestTarget

#: 套件选择 marker（pyproject markers 注册；显式路径定向运行为主入口）。
pytestmark = [pytest.mark.seekdb]


async def test_hybrid_search_round_trip(seekdb_scratch: SeekDbTestTarget) -> None:
    """全链路：建表 → 批量 upsert → keyword / hybrid 检索 → 覆盖 → 隔离 → 删除。

    Args:
        seekdb_scratch: scratch 库目标（空库起步——ff_memory_index 由
            ensure_schema 建表，不经 alembic 链）。
    """
    # 存储配置自显式测试服务目标构造（密码经 SEEKDB_TEST_PASSWORD env
    # 间接——与生产 password_env 模式同构；vector_dim=4 为测试向量形态）。
    store = SeekDBStore(
        SeekDBStoreConfig(
            host=seekdb_scratch.host,
            port=seekdb_scratch.port,
            user=seekdb_scratch.user,
            password_env="SEEKDB_TEST_PASSWORD",
            database=seekdb_scratch.database,
            vector_dim=4,
        )
    )
    try:
        await store.ensure_schema()

        entries = [
            MemoryIndexEntryDTO.model_validate(
                {
                    "persona_id": 900001,
                    "ref_type": "episode",
                    "ref_id": 1,
                    "kind": "chat",
                    "provenance": "owner",
                    "ts_ms": 1000,
                    "content": "主人今天吃了很多小鱼干",
                }
            ),
            MemoryIndexEntryDTO.model_validate(
                {
                    "persona_id": 900001,
                    "ref_type": "curated",
                    "ref_id": 2,
                    "kind": "memory",
                    "provenance": "agent",
                    "ts_ms": 2000,
                    "content": "主人喜欢小鱼干",
                }
            ),
        ]
        vectors = [[0.1, 0.2, 0.3, 0.4], [0.4, 0.3, 0.2, 0.1]]
        await store.upsert(entries, vectors)

        # keyword 检索回读（MATCH 相关度——「小鱼干」两行命中）。
        keyword = await store.search(
            MemorySearchQueryDTO(persona_id=900001, query="小鱼干", mode=MemorySearchMode.KEYWORD),
            None,
        )
        assert {result.ref_id for result in keyword} == {1, 2}
        assert all(result.score > 0 for result in keyword)

        # hybrid 检索回读（Form B——DBMS_HYBRID_SEARCH.SEARCH 融合排序）。
        hybrid = await store.search(
            MemorySearchQueryDTO(
                persona_id=900001, query="小鱼干", mode=MemorySearchMode.HYBRID, limit=5
            ),
            [0.1, 0.2, 0.3, 0.4],
        )
        assert len(hybrid) >= 1
        assert 1 in {result.ref_id for result in hybrid}
        assert all(result.score > 0 for result in hybrid)  # 融合 _score 越大越相关
        scores = [result.score for result in hybrid]
        assert scores == sorted(scores, reverse=True)  # 引擎融合分降序保持

        # 同键覆盖幂等（ON DUPLICATE KEY UPDATE 运行期验证）。
        updated = entries[0].model_copy(update={"content": "主人今天吃了很多虾干"})
        await store.upsert([updated], [[0.1, 0.2, 0.3, 0.4]])
        only = await store.search(
            MemorySearchQueryDTO(persona_id=900001, query="虾干", mode=MemorySearchMode.KEYWORD),
            None,
        )
        assert [result.ref_id for result in only] == [1]

        # 人格隔离谓词（他人格零命中）。
        isolated = await store.search(
            MemorySearchQueryDTO(persona_id=999999, query="小鱼干", mode=MemorySearchMode.KEYWORD),
            None,
        )
        assert isolated == []

        # delete 清理。
        await store.delete(entries[0].ref_type, entries[0].ref_id, 900001)
        await store.delete(entries[1].ref_type, entries[1].ref_id, 900001)
        final = await store.search(
            MemorySearchQueryDTO(persona_id=900001, query="小鱼干", mode=MemorySearchMode.KEYWORD),
            None,
        )
        assert final == []
    finally:
        await store.close()

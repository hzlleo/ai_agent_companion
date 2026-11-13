"""SeekDB 真实实例冒烟集成测试（默认跳过——env 门控）。

前置：启动本地 SeekDB（server 模式——MySQL 线协议 2881 端口）::

    docker run -d --name seekdb-smoke -p 2881:2881 oceanbase/seekdb:v1.4.0

（镜像与标签以 SeekDB releases 页为准：
https://github.com/oceanbase/seekdb/releases ——v1.4.0 为本工程钉死版本。）

随后导出门控变量并定向运行::

    SEEKDB_SMOKE_URL="127.0.0.1:2881" \\
    SEEKDB_SMOKE_USER=root \\
    SEEKDB_SMOKE_PASSWORD=root \\
    SEEKDB_SMOKE_DATABASE=test \\
    pixi run pytest src/tests/integration/test_seekdb_smoke.py -v --asyncio-mode=auto

链路（SeekDBStore 全语义真实验证——单测假池面无法覆盖的端到端契约）：

    ensure_schema（DDL 建表——VECTOR / jieba 全文 / hnsw / HEAP）→
    upsert（ON DUPLICATE KEY 批量——运行期语法验证：若 SeekDB 拒绝
    ON DUPLICATE KEY，此处即失败，回退路径为 DELETE+INSERT，见
    memory.store.build_upsert_sql 文档）→ keyword 检索回读 →
    hybrid 检索回读（MATCH 预过滤 + APPROXIMATE 向量排序）→
    同键覆盖幂等 → delete 清理。

注：aiomysql 驱动为工程选型（原生 async；SeekDB 官方未文档化——风险
与回退路径文档化于 docs/backend/memory.md 检索层一节）；本冒烟即
    该选型的真实验证面。
"""

from __future__ import annotations

import os

import pytest

from packages.harness.memory.retrieval_models import (
    MemoryIndexEntryDTO,
    MemorySearchMode,
    MemorySearchQueryDTO,
    SeekDBStoreConfig,
)
from packages.harness.memory.seekdb_store import SeekDBStore

#: 冒烟门控：未设置 SEEKDB_SMOKE_URL 时全套跳过（默认 hermetic）。
_SMOKE_URL = os.environ.get("SEEKDB_SMOKE_URL", "")

pytestmark = pytest.mark.skipif(
    not _SMOKE_URL,
    reason="需真实 SeekDB 实例（设置 SEEKDB_SMOKE_URL=host:port 启用——见模块 docstring docker 一行命令）",
)


def _smoke_config() -> SeekDBStoreConfig:
    """从 SEEKDB_SMOKE_* env 门控变量构造存储配置。

    Returns:
        指向真实实例的 :class:`SeekDBStoreConfig`。
    """
    host, _, port_text = _SMOKE_URL.partition(":")
    return SeekDBStoreConfig(
        host=host,
        port=int(port_text) if port_text else 2881,
        user=os.environ.get("SEEKDB_SMOKE_USER", "root"),
        password_env="SEEKDB_SMOKE_PASSWORD",
        database=os.environ.get("SEEKDB_SMOKE_DATABASE", "test"),
        vector_dim=4,
    )


async def test_seekdb_smoke_round_trip() -> None:
    """全链路冒烟：建表 → 批量 upsert → keyword / hybrid 检索 → 覆盖 → 删除。"""
    store = SeekDBStore(_smoke_config())
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

        # keyword 检索回读（MATCH 相关度——「小鱼干」两行命中）
        keyword = await store.search(
            MemorySearchQueryDTO(persona_id=900001, query="小鱼干", mode=MemorySearchMode.KEYWORD),
            None,
        )
        assert {result.ref_id for result in keyword} == {1, 2}
        assert all(result.score > 0 for result in keyword)

        # hybrid 检索回读（MATCH 预过滤 + APPROXIMATE 向量排序）
        hybrid = await store.search(
            MemorySearchQueryDTO(
                persona_id=900001, query="小鱼干", mode=MemorySearchMode.HYBRID, limit=5
            ),
            [0.1, 0.2, 0.3, 0.4],
        )
        assert len(hybrid) >= 1
        assert hybrid[0].ref_id == 1  # 查询向量与首行同向——cosine 距离最小
        assert all(result.score >= 0 for result in hybrid)

        # 同键覆盖幂等（ON DUPLICATE KEY UPDATE 运行期验证）
        updated = entries[0].model_copy(update={"content": "主人今天吃了很多虾干"})
        await store.upsert([updated], [[0.1, 0.2, 0.3, 0.4]])
        only = await store.search(
            MemorySearchQueryDTO(persona_id=900001, query="虾干", mode=MemorySearchMode.KEYWORD),
            None,
        )
        assert [result.ref_id for result in only] == [1]

        # 人格隔离谓词（他人格零命中）
        isolated = await store.search(
            MemorySearchQueryDTO(persona_id=999999, query="小鱼干", mode=MemorySearchMode.KEYWORD),
            None,
        )
        assert isolated == []

        # delete 清理
        await store.delete(entries[0].ref_type, entries[0].ref_id, 900001)
        await store.delete(entries[1].ref_type, entries[1].ref_id, 900001)
        final = await store.search(
            MemorySearchQueryDTO(persona_id=900001, query="小鱼干", mode=MemorySearchMode.KEYWORD),
            None,
        )
        assert final == []
    finally:
        await store.close()

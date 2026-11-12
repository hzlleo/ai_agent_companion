"""数据库引擎持有器 Model Class——引擎/会话工厂槽位的单一真源。

持有器设计要点：

    - **单一真源**：主库引擎与会话工厂收敛为本实例槽位——一切写经
      ``__setattr__`` 可观测（记录 debug 日志），测试 monkeypatch 与
      生产生命周期共用同一写入面；
    - **所有权治理**：``owner_token`` 区分「生产生命周期创建的引擎」与
      「外部安装（测试 bootstrap）的引擎」——生产 init/rebuild 不 dispose
      外部引擎（生命周期归安装方），防止误杀共享测试引擎；
    - **恒 None 哨兵复位**：close 恒写 None（从不回放「关闭前捕获的值」），
      杜绝跨测试 / 跨生命周期的陈旧引擎泄漏。

生命周期逻辑（init / close / rebuild）位于
:mod:`packages.harness.database.engine`；本文件仅承载 Model Class。
"""

from __future__ import annotations

from typing import Any

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

__all__ = ["EngineHolder"]


class EngineHolder:
    """数据库引擎/会话工厂持有器——主库两槽位 + 所有权令牌。

    槽位：

        - ``async_engine``：主库异步引擎（MySQL 权威 / SQLite 测试）；
        - ``session_factory``：主库会话工厂（``async_sessionmaker``）。

    所有权令牌 ``owner_token``：

        - 非 None：当前引擎由生产生命周期（``init_database`` /
          ``rebuild_database_engine``）创建，token 是其处置凭证（每次
          创建/重建刷新）；
        - None：外部安装（测试 bootstrap / 手动注入）——生产路径
          **不得 dispose**（引擎生命周期归安装方）。

    初始化契约：``init_database()`` 调用前两槽位均为 None（懒初始化）。
    """

    __slots__ = (
        "async_engine",
        "session_factory",
        "owner_token",
    )

    def __init__(self) -> None:
        """初始化持有器（两槽位 None + 无所有权）。"""
        self.async_engine: AsyncEngine | None = None
        self.session_factory: async_sessionmaker[AsyncSession] | None = None
        self.owner_token: str | None = None

    def __setattr__(self, name: str, value: Any) -> None:
        """全写可观测钩子——每次槽位写入记 debug 日志。

        ``__slots__`` 保证未知槽位名即刻 ``AttributeError``（拼写错误
        无法静默通过）；值透传 ``object.__setattr__``，不改写。

        Args:
            name: 槽位名。
            value: 写入值。
        """
        logger.debug("EngineHolder 写入: {}={!r:.80}", name, value)
        object.__setattr__(self, name, value)

    def reset(self) -> None:
        """恒 None 哨兵复位（不 dispose——引擎处置由生命周期函数负责）。

        测试 teardown 与外部安装方释放槽位用；写后两槽位归 None、
        所有权清空。
        """
        self.async_engine = None
        self.session_factory = None
        self.owner_token = None

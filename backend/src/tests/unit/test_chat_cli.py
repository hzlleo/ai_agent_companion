"""harness/chat cli 测试——REPL 全流程（注入式 I/O + 假网关，零真实网络）。

测试策略：真实启动序列（隔离 settings + tmp SQLite + 真实 PoolManager /
PersonaService）+ 假网关与序列假输入 / 记录假输出（never real stdin）。
覆盖面：横幅 + 开场白 / 流式回复直写 / /history / /clear / /help /
/quit 与 EOF / 无激活人格指引路径（退出码 1）/ 网关业务异常不崩 REPL /
main 入口（argparse + SystemExit 透传）。
"""

from __future__ import annotations

import sys
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import packages.harness.usage.orm as usage_orm  # noqa: F401  模型注册副作用导入（CLI 建表对齐）
from packages.harness.chat import cli
from packages.harness.config.models import DatabaseConfig
from packages.harness.core.errors import BusinessException, ErrorCode
from packages.harness.database.base import Base
from packages.harness.database.engine import db_engine_holder
from packages.harness.llm.llm_models import ChatResponseModel, ChatStreamChunkModel
from packages.harness.persona.dtos import PersonaCreateDTO
from packages.harness.persona.service import PersonaService
from packages.harness.pools.manager import get_pool_manager


class FakeGateway:
    """假 LLM 网关（CLI 流式面——记录调用；可注入故障）。"""

    def __init__(
        self, *, stream_parts: Sequence[str] = ("早", "安"), error: Exception | None = None
    ) -> None:
        """初始化假网关。

        Args:
            stream_parts: 流式增量块序列。
            error: 故障注入（首个迭代步抛出）。
        """
        self.stream_parts = list(stream_parts)
        self.error = error
        self.calls: list[dict[str, Any]] = []

    async def chat_by_role(
        self, role: Any, messages: Any, params: Any = None, **kwargs: Any
    ) -> ChatResponseModel:
        """非流式假调用（CLI 流式面不触达——补全契约）。"""
        raise AssertionError("CLI 流式面不应触达非流式调用")

    async def stream_by_role(
        self, role: Any, messages: Any, params: Any = None, **kwargs: Any
    ) -> AsyncIterator[ChatStreamChunkModel]:
        """流式假调用（记录参数 + 逐块产出）。"""
        self.calls.append({"role": role, "messages": list(messages), **kwargs})
        if self.error is not None:
            raise self.error
        for part in self.stream_parts:
            yield ChatStreamChunkModel(content=part)


class SequenceInput:
    """序列假输入（逐行返回；耗尽后以 EOFError 表达结束——never real stdin）。"""

    def __init__(self, lines: Sequence[str]) -> None:
        """初始化假输入。

        Args:
            lines: 逐行返回的输入序列。
        """
        self._lines = iter(lines)
        self.prompts: list[str] = []

    def __call__(self, prompt: str) -> str:
        """返回下一行（耗尽即 EOFError）。"""
        self.prompts.append(prompt)
        try:
            return next(self._lines)
        except StopIteration as exc:
            raise EOFError from exc


class RecordingOutput:
    """记录假输出（token 粒度追加——流式直写断言面）。"""

    def __init__(self) -> None:
        """初始化记录器。"""
        self.chunks: list[str] = []
        self.text = ""

    def __call__(self, text: str) -> None:
        """记录输出文本。"""
        self.chunks.append(text)
        self.text += text


async def _seed_active_persona(db_path: Path) -> int:
    """在 tmp SQLite 文件中预置一个激活人格（CLI 启动前铺底）。

    Args:
        db_path: SQLite 库文件路径。

    Returns:
        已激活人格的主键。
    """
    engine: AsyncEngine = create_async_engine(
        f"sqlite+aiosqlite:///{db_path}",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    async with factory() as session:
        service = PersonaService()
        created = await service.create(
            session,
            PersonaCreateDTO.model_validate(
                {
                    "name": "cli-feiyu",
                    "status": "active",
                    "card": {
                        "name": "大肥鱼",
                        "description": "圆滚滚的橘色胖鱼",
                        "personality": "元气",
                        "first_mes": "你回来啦！",
                        "post_history_instructions": "回复保持简短。",
                    },
                }
            ),
        )
        await service.activate(session, created.id)
        persona_id = created.id
    await engine.dispose()
    return persona_id


def _sqlite_config(db_path: Path) -> DatabaseConfig:
    """tmp SQLite 数据库配置（显式注入——隔离真实配置）。"""
    return DatabaseConfig(type="sqlite", name=str(db_path))


async def test_cli_full_chat_flow_with_commands(tmp_path: Path) -> None:
    """全流程：横幅开场 → 流式回复 → /history → /clear → /history → /quit。"""
    db_path = tmp_path / "chat-flow.db"
    persona_id = await _seed_active_persona(db_path)
    gateway = FakeGateway(stream_parts=("早", "安"))
    recorder = RecordingOutput()
    inputs = SequenceInput(["在吗", "/history", "/clear", "/history", "/quit"])

    exit_code = await cli.run_chat_cli(
        database_config=_sqlite_config(db_path),
        gateway=gateway,
        input_fn=inputs,
        output_fn=recorder,
    )

    assert exit_code == 0
    assert "已连接人格：大肥鱼（注册名 cli-feiyu）" in recorder.text
    assert "大肥鱼> 你回来啦！" in recorder.text
    # 流式增量直写（token 粒度）。
    assert "早" in recorder.chunks and "安" in recorder.chunks
    # 网关直通：persona_id / purpose / 组装消息含 system 首块。
    assert len(gateway.calls) == 1
    assert gateway.calls[0]["persona_id"] == str(persona_id)
    assert gateway.calls[0]["purpose"] == "chat"
    assert gateway.calls[0]["messages"][0].role.value == "system"
    # /history（清空前）：开场白 + 用户 + 人格回复。
    assert "用户: 在吗" in recorder.text
    assert "人格: 早安" in recorder.text
    # /clear 后历史为空。
    assert "（会话历史为空）" in recorder.text
    assert "会话历史已清空" in recorder.text
    assert "再见。" in recorder.text
    # 输入提示形态（REPL 提示符）。
    assert inputs.prompts[0] == "你> "
    # 收尾：池已停 + 数据库 holder 已复位。
    manager = get_pool_manager()
    assert manager is None or not manager.started
    assert db_engine_holder.async_engine is None


async def test_cli_help_command_and_eof_exit(tmp_path: Path) -> None:
    """/help 输出命令清单；输入耗尽（EOF）等效 /quit 退出码 0。"""
    db_path = tmp_path / "chat-eof.db"
    await _seed_active_persona(db_path)
    recorder = RecordingOutput()
    inputs = SequenceInput(["/help"])

    exit_code = await cli.run_chat_cli(
        database_config=_sqlite_config(db_path),
        gateway=FakeGateway(),
        input_fn=inputs,
        output_fn=recorder,
    )
    assert exit_code == 0
    assert "/quit" in recorder.text and "/clear" in recorder.text
    assert "/history" in recorder.text and "/help" in recorder.text
    assert "再见。" in recorder.text


async def test_cli_empty_line_skipped(tmp_path: Path) -> None:
    """空行跳过（不触发网关调用），后续输入正常对话。"""
    db_path = tmp_path / "chat-empty.db"
    await _seed_active_persona(db_path)
    gateway = FakeGateway()
    recorder = RecordingOutput()
    inputs = SequenceInput(["", "   ", "/quit"])

    exit_code = await cli.run_chat_cli(
        database_config=_sqlite_config(db_path),
        gateway=gateway,
        input_fn=inputs,
        output_fn=recorder,
    )
    assert exit_code == 0
    assert gateway.calls == []
    assert "已连接人格" in recorder.text


async def test_cli_no_active_persona_hint_path() -> None:
    """无激活人格：中文指引（创建 / 启用 / 激活三步）+ 退出码 1 + LIFO 收尾。"""
    gateway = FakeGateway()
    recorder = RecordingOutput()

    exit_code = await cli.run_chat_cli(
        database_config=DatabaseConfig(type="sqlite", name=":memory:"),
        gateway=gateway,
        input_fn=SequenceInput(["/quit"]),
        output_fn=recorder,
    )

    assert exit_code == 1
    assert "未找到激活人格" in recorder.text
    assert "/api/v1/personas" in recorder.text
    assert "activate" in recorder.text
    assert gateway.calls == []
    manager = get_pool_manager()
    assert manager is None or not manager.started
    assert db_engine_holder.async_engine is None


async def test_cli_business_error_keeps_repl_alive(tmp_path: Path) -> None:
    """网关业务异常：错误提示上屏，REPL 不崩（后续 /quit 正常退出）。"""
    db_path = tmp_path / "chat-error.db"
    await _seed_active_persona(db_path)
    gateway = FakeGateway(
        error=BusinessException(code=ErrorCode.LLM_REQUEST_FAILED, message="端点熔断中，请稍后再试")
    )
    recorder = RecordingOutput()
    inputs = SequenceInput(["在吗", "/quit"])

    exit_code = await cli.run_chat_cli(
        database_config=_sqlite_config(db_path),
        gateway=gateway,
        input_fn=inputs,
        output_fn=recorder,
    )
    assert exit_code == 0
    assert "[对话失败] 端点熔断中，请稍后再试" in recorder.text
    assert "再见。" in recorder.text


async def test_cli_assembles_real_gateway_when_not_injected(tmp_path: Path) -> None:
    """gateway 未注入：现场装配真实网关（usage 默认接线）——/quit 即退出
    （零 LLM 调用，覆盖装配分支）。"""
    db_path = tmp_path / "chat-realgw.db"
    await _seed_active_persona(db_path)
    recorder = RecordingOutput()

    exit_code = await cli.run_chat_cli(
        database_config=_sqlite_config(db_path),
        input_fn=SequenceInput(["/quit"]),
        output_fn=recorder,
    )
    assert exit_code == 0
    assert "已连接人格：大肥鱼" in recorder.text


async def test_cli_seed_persona_rows_visible(tmp_path: Path) -> None:
    """铺底自检：预置人格行真实落库（CLI 读取面的前提）。"""
    db_path = tmp_path / "chat-seed.db"
    persona_id = await _seed_active_persona(db_path)
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    async with engine.connect() as conn:
        count = (await conn.execute(text("SELECT COUNT(*) FROM ff_personas"))).scalar_one()
        active = (
            await conn.execute(
                text("SELECT is_active FROM ff_personas WHERE id = :pid"),
                {"pid": persona_id},
            )
        ).scalar_one()
    await engine.dispose()
    assert count == 1
    assert active == 1


async def test_cli_main_entry_exit_code_passthrough(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """main 入口：argparse 解析 + run_chat_cli 退出码经 SystemExit 透传。"""
    recorded: dict[str, object] = {}

    async def fake_run_chat_cli() -> int:
        """假 REPL（记录调用，返回哨兵退出码）。"""
        recorded["called"] = True
        return 7

    def fake_asyncio_run(coro: Any) -> int:
        """假 asyncio.run（驱动无 await 协程至完成，透传返回值）。"""
        recorded["coro"] = coro
        try:
            coro.send(None)
        except StopIteration as result:
            return int(result.value or 0)
        coro.close()
        return 0

    monkeypatch.setattr(cli, "run_chat_cli", fake_run_chat_cli)
    monkeypatch.setattr(cli.asyncio, "run", fake_asyncio_run)
    monkeypatch.setattr(sys, "argv", ["chat"])
    with pytest.raises(SystemExit) as exc_info:
        cli.main()
    assert exc_info.value.code == 7
    assert recorded["called"] is True


async def test_cli_main_help_exits_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    """main --help：argparse 原生帮助退出 0（不启动任何组件）。"""
    monkeypatch.setattr(sys, "argv", ["chat", "--help"])
    with pytest.raises(SystemExit) as exc_info:
        cli.main()
    assert exc_info.value.code == 0

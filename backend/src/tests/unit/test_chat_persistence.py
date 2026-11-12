"""harness/chat 持久化集成测试——ChatService 会话落库 / 回合落库 / 恢复。

覆盖面：create_session 落库（session_id 携带）/ reply 与 stream_reply
回合落库（user + assistant，seq 递增）/ 网关失败时用户回合保留 /
resume 全链路（持久化 → 恢复 → 续聊）/ 持久化关闭时行为与 P0 一致
（零落库行）/ CLI /new /sessions /resume 命令（真实启动栈 + tmp
SQLite + 假网关）。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

import packages.harness.memory.orm as memory_orm  # noqa: F401  模型注册副作用导入
from packages.harness.chat import cli
from packages.harness.chat.service import ChatService
from packages.harness.config.models import DatabaseConfig
from packages.harness.config.settings import build_isolated_settings
from packages.harness.core.errors import BusinessException, ErrorCode
from packages.harness.database.base import Base
from packages.harness.database.engine import db_engine_holder
from packages.harness.llm.llm_models import (
    ChatMessageRole,
    ChatResponseModel,
    ChatStreamChunkModel,
)
from packages.harness.memory.orm import ChatMessageRow, ChatSessionRow
from packages.harness.memory.service import ChatSessionService
from packages.harness.persona.dtos import PersonaCreateDTO, PersonaRecordDTO
from packages.harness.persona.service import PersonaService, bind_persona_context


class FakeGateway:
    """假 LLM 网关——记录调用参数；可注入非流式回复 / 流式增量 / 首 N 次故障。"""

    def __init__(
        self,
        *,
        response_content: str = "假回复",
        stream_parts: Sequence[str] = ("早", "安"),
        error: Exception | None = None,
        fail_first: int = 0,
    ) -> None:
        """初始化假网关。

        Args:
            response_content: 非流式回复文本。
            stream_parts: 流式增量块序列。
            error: 故障注入（首 ``fail_first`` 次调用抛出；0 = 恒生效）。
            fail_first: 前多少次调用抛 ``error``（0 = 不故障）。
        """
        self.response_content = response_content
        self.stream_parts = list(stream_parts)
        self.error = error
        self.fail_first = fail_first
        self._calls = 0
        self.chat_calls: list[dict[str, Any]] = []

    def _should_fail(self) -> bool:
        """故障判定（首 N 次调用故障；计数自增）。"""
        self._calls += 1
        return self.error is not None and self._calls <= self.fail_first

    async def chat_by_role(
        self, role: Any, messages: Any, params: Any = None, **kwargs: Any
    ) -> ChatResponseModel:
        """非流式假调用（记录参数）。"""
        self.chat_calls.append({"role": role, "messages": list(messages), **kwargs})
        if self._should_fail():
            assert self.error is not None
            raise self.error
        return ChatResponseModel(content=self.response_content)

    async def stream_by_role(
        self, role: Any, messages: Any, params: Any = None, **kwargs: Any
    ) -> AsyncIterator[ChatStreamChunkModel]:
        """流式假调用（逐块产出）。"""
        if self._should_fail():
            assert self.error is not None
            raise self.error
        for part in self.stream_parts:
            yield ChatStreamChunkModel(content=part)


def _persona_payload(name: str = "persist-feiyu") -> dict[str, Any]:
    """构造最小人格创建请求体。"""
    return {
        "name": name,
        "status": "active",
        "card": {
            "name": "大肥鱼",
            "description": "圆滚滚的橘色胖鱼",
            "personality": "元气",
            "first_mes": "你回来啦！",
        },
    }


async def _create_persona(
    db_session: AsyncSession, name: str = "persist-feiyu"
) -> PersonaRecordDTO:
    """经真实 PersonaService 创建并返回人格记录。"""
    service = PersonaService()
    return await service.create(
        db_session, PersonaCreateDTO.model_validate(_persona_payload(name))
    )


async def _count_rows(session: AsyncSession, row_type: type[object]) -> int:
    """统计表行数（跨人格管理面口径——跳过人格自动过滤）。"""
    stmt = select(func.count()).select_from(row_type)
    return int(
        (await session.execute(stmt, execution_options={"skip_persona_filter": True})).scalar_one()
    )


@pytest.fixture
async def persist_env(
    shared_db_engine: AsyncEngine,
) -> AsyncIterator[
    tuple[FakeGateway, ChatService, ChatSessionService, PersonaRecordDTO, AsyncSession]
]:
    """持久化集成环境（共享引擎外部安装到 holder；激活人格铺底）。

    Yields:
        ``(gateway, chat_service, session_service, persona_record, db_session)``
        五元组（holder 用后复位）。
    """
    db_engine_holder.async_engine = shared_db_engine
    db_engine_holder.session_factory = async_sessionmaker(
        bind=shared_db_engine, expire_on_commit=False
    )
    db_engine_holder.owner_token = None
    session_service = ChatSessionService()
    gateway = FakeGateway()
    service = ChatService(gateway=gateway, session_service=session_service)
    async with async_sessionmaker(bind=shared_db_engine, expire_on_commit=False)() as db_session:
        persona = await _create_persona(db_session)
        yield gateway, service, session_service, persona, db_session
    db_engine_holder.reset()


# ---------------------------------------------------------------------------
# 会话落库 / 回合落库
# ---------------------------------------------------------------------------


async def test_create_session_persists_row(persist_env: tuple) -> None:
    """create_session(db_session=...)：ChatSessionRow 落库 + 状态携带 session_id。"""
    _, service, session_service, persona, db_session = persist_env
    state = await service.create_session(
        persona=persona, db_session=db_session, title="持久化测试"
    )
    assert state.session_id is not None
    async with bind_persona_context(persona.id):
        summary = await session_service.get_session(db_session, state.session_id)
    assert summary.title == "持久化测试"
    assert summary.persona_id == str(persona.id)
    assert summary.message_count == 0


async def test_reply_persists_user_and_assistant(persist_env: tuple) -> None:
    """reply：用户 + 助手回合各落一行（seq 1/2），会话计数同步。"""
    _, service, session_service, persona, db_session = persist_env
    state = await service.create_session(
        persona=persona, db_session=db_session, title="回复落库"
    )
    content = await service.reply(state, "今天有点累")
    assert content == "假回复"

    assert await _count_rows(db_session, ChatMessageRow) == 2
    async with bind_persona_context(persona.id):
        detail = await session_service.get_session(db_session, state.session_id or 0)
    assert detail.message_count == 2
    assert [m.seq for m in detail.messages] == [1, 2]
    assert [m.role for m in detail.messages] == [
        ChatMessageRole.USER,
        ChatMessageRole.ASSISTANT,
    ]
    assert [m.content for m in detail.messages] == ["今天有点累", "假回复"]


async def test_stream_reply_persists_turns(persist_env: tuple) -> None:
    """stream_reply：流结束后聚合全文落 assistant 行。"""
    _, service, session_service, persona, db_session = persist_env
    state = await service.create_session(persona=persona, db_session=db_session)
    chunks: list[str] = []
    async for token in service.stream_reply(state, "在吗"):
        chunks.append(token)
    assert "".join(chunks) == "早安"

    async with bind_persona_context(persona.id):
        detail = await session_service.get_session(db_session, state.session_id or 0)
    assert detail.message_count == 2
    assert detail.messages[1].content == "早安"


async def test_reply_gateway_failure_keeps_user_row(persist_env: tuple) -> None:
    """网关失败：用户回合保留在库（无 assistant 行）——重试即重新生成。"""
    gateway = FakeGateway(
        error=BusinessException(code=ErrorCode.LLM_CALL_FAILED), fail_first=1
    )
    service = ChatService(gateway=gateway, session_service=ChatSessionService())
    _, _, _, persona, db_session = persist_env
    state = await service.create_session(persona=persona, db_session=db_session)
    with pytest.raises(BusinessException):
        await service.reply(state, "会失败的一句")
    assert await _count_rows(db_session, ChatMessageRow) == 1

    # 同一服务重试（故障为一次性）：用户回合再次落库（重试语义），assistant 落库
    await service.reply(state, "重试一句")
    assert await _count_rows(db_session, ChatMessageRow) == 3


async def test_persistence_off_behavior_unchanged(persist_env: tuple) -> None:
    """持久化关闭（P0 形态）：session_id 为 None + 零落库行 + 对话照常。"""
    gateway = FakeGateway()
    service = ChatService(gateway=gateway)  # session_service=None
    _, _, _, persona, db_session = persist_env
    state = await service.create_session(persona=persona)
    assert state.session_id is None

    content = await service.reply(state, "纯内存对话")
    assert content == "假回复"
    assert [t.role for t in state.turns] == [
        ChatMessageRole.USER,
        ChatMessageRole.ASSISTANT,
    ]
    assert await _count_rows(db_session, ChatSessionRow) == 0
    assert await _count_rows(db_session, ChatMessageRow) == 0

    chunks = [token async for token in service.stream_reply(state, "流式也照常")]
    assert chunks == ["早", "安"]
    assert await _count_rows(db_session, ChatMessageRow) == 0


async def test_create_session_without_db_session_stays_memory(persist_env: tuple) -> None:
    """绑定持久化但未给 db_session：纯内存态（可选持久化的缺省面）。"""
    _, service, _, persona, db_session = persist_env
    state = await service.create_session(persona=persona)
    assert state.session_id is None
    await service.reply(state, "不落库")
    assert await _count_rows(db_session, ChatMessageRow) == 0


# ---------------------------------------------------------------------------
# resume 全链路
# ---------------------------------------------------------------------------


async def test_resume_roundtrip(persist_env: tuple) -> None:
    """持久化 → 恢复 → 续聊：回合回放进历史 + 新回合续接 seq。"""
    gateway, service, session_service, persona, db_session = persist_env
    state = await service.create_session(persona=persona, db_session=db_session)
    await service.reply(state, "第一句")
    await service.reply(state, "第二句")
    assert len(state.turns) == 4

    async with bind_persona_context(persona.id):
        resumed = await service.resume_session(state.session_id or 0, db_session=db_session)
    assert resumed.session_id == state.session_id
    assert resumed.persona_id == persona.id
    assert resumed.card.name == "大肥鱼"
    assert [(t.role, t.content) for t in resumed.turns] == [
        (ChatMessageRole.USER, "第一句"),
        (ChatMessageRole.ASSISTANT, "假回复"),
        (ChatMessageRole.USER, "第二句"),
        (ChatMessageRole.ASSISTANT, "假回复"),
    ]

    # 恢复后续聊：组装历史包含回放回合 + 新回合续接 seq
    await service.reply(resumed, "恢复后的第三句")
    assert len(resumed.turns) == 6
    sent_messages = gateway.chat_calls[-1]["messages"]
    assert [m.content for m in sent_messages][1:] == [
        "第一句",
        "假回复",
        "第二句",
        "假回复",
        "恢复后的第三句",
    ]
    async with bind_persona_context(persona.id):
        detail = await session_service.get_session(db_session, state.session_id or 0)
    assert detail.message_count == 6


async def test_resume_other_persona_session_not_found(persist_env: tuple) -> None:
    """跨人格恢复：他人格会话经自动过滤不可见（NOT_FOUND）。"""
    _, service, _, persona_a, db_session = persist_env
    persona_b = await _create_persona(db_session, name="persist-other")
    state = await service.create_session(persona=persona_a, db_session=db_session)
    await service.reply(state, "A 的对话")

    async with bind_persona_context(persona_b.id):
        with pytest.raises(BusinessException) as exc_info:
            await service.resume_session(state.session_id or 0, db_session=db_session)
    assert exc_info.value.code == ErrorCode.NOT_FOUND


async def test_resume_without_session_service(persist_env: tuple) -> None:
    """未绑定持久化服务的 resume：INTERNAL_ERROR fail loud。"""
    gateway = FakeGateway()
    service = ChatService(gateway=gateway)
    _, _, _, persona, db_session = persist_env
    async with bind_persona_context(persona.id):
        with pytest.raises(BusinessException) as exc_info:
            await service.resume_session(1, db_session=db_session)
    assert exc_info.value.code == ErrorCode.INTERNAL_ERROR


# ---------------------------------------------------------------------------
# CLI 命令面（/new /sessions /resume——真实启动栈 + tmp SQLite）
# ---------------------------------------------------------------------------


class SequenceInput:
    """序列假输入（逐行返回；耗尽后 EOFError——never real stdin）。"""

    def __init__(self, lines: Sequence[str]) -> None:
        """初始化假输入。

        Args:
            lines: 逐行返回的输入序列。
        """
        self._lines = iter(lines)

    def __call__(self, prompt: str) -> str:
        """返回下一行（耗尽即 EOFError）。"""
        try:
            return next(self._lines)
        except StopIteration as exc:
            raise EOFError from exc


class RecordingOutput:
    """记录假输出（追加式——断言面）。"""

    def __init__(self) -> None:
        """初始化记录器。"""
        self.text = ""

    def __call__(self, text: str) -> None:
        """记录输出文本。"""
        self.text += text


async def _seed_active_persona(db_path: Path) -> int:
    """在 tmp SQLite 中预置激活人格（CLI 启动前铺底）。"""
    engine = create_async_engine(
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
                    "name": "cli-persist",
                    "status": "active",
                    "card": {
                        "name": "大肥鱼",
                        "description": "圆滚滚的橘色胖鱼",
                        "personality": "元气",
                        "first_mes": "你回来啦！",
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


async def test_cli_new_sessions_resume_flow(tmp_path: Path) -> None:
    """CLI 全链：对话落库 → /new 起新会话 → /sessions 列表 → /resume 恢复续聊。"""
    db_path = tmp_path / "persist-cli.db"
    await _seed_active_persona(db_path)

    first_inputs = SequenceInput(["在吗", "/quit"])
    first_output = RecordingOutput()
    exit_code = await cli.run_chat_cli(
        settings=build_isolated_settings(database=_sqlite_config(db_path)),
        database_config=_sqlite_config(db_path),
        config_dir=tmp_path,
        gateway=FakeGateway(stream_parts=("早", "安")),
        input_fn=first_inputs,
        output_fn=first_output,
    )
    assert exit_code == 0
    assert "你回来啦" in first_output.text

    second_inputs = SequenceInput(
        ["/sessions", "/resume 1", "/history", "/quit"]
    )
    second_output = RecordingOutput()
    exit_code = await cli.run_chat_cli(
        settings=build_isolated_settings(database=_sqlite_config(db_path)),
        database_config=_sqlite_config(db_path),
        config_dir=tmp_path,
        gateway=FakeGateway(stream_parts=("继", "续")),
        input_fn=second_inputs,
        output_fn=second_output,
    )
    assert exit_code == 0
    assert "最近的会话" in second_output.text
    assert "#1 [2条]" in second_output.text
    assert "已恢复会话 #1（2条历史回合）" in second_output.text
    # 恢复后的 /history 展示回放回合
    assert "在吗" in second_output.text
    assert "早安" in second_output.text

    third_inputs = SequenceInput(["/new", "/sessions", "/quit"])
    third_output = RecordingOutput()
    exit_code = await cli.run_chat_cli(
        settings=build_isolated_settings(database=_sqlite_config(db_path)),
        database_config=_sqlite_config(db_path),
        config_dir=tmp_path,
        gateway=FakeGateway(),
        input_fn=third_inputs,
        output_fn=third_output,
    )
    assert exit_code == 0
    # 每次启动即开新持久化会话（#3 = 本次启动；/new 再起新会话 #4）
    assert "已新建会话 #4" in third_output.text
    assert "#4 [0条]" in third_output.text
    assert "#1 [2条]" in third_output.text


async def test_cli_resume_missing_session_keeps_repl(tmp_path: Path) -> None:
    """CLI /resume 缺失会话：错误上屏不崩 REPL（退出码 0）。"""
    db_path = tmp_path / "resume-missing.db"
    await _seed_active_persona(db_path)
    inputs = SequenceInput(["/resume 999", "/quit"])
    output = RecordingOutput()
    exit_code = await cli.run_chat_cli(
        settings=build_isolated_settings(database=_sqlite_config(db_path)),
        database_config=_sqlite_config(db_path),
        config_dir=tmp_path,
        gateway=FakeGateway(),
        input_fn=inputs,
        output_fn=output,
    )
    assert exit_code == 0
    assert "恢复失败" in output.text

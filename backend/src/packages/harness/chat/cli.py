"""chat 域控制台 REPL——P0 用户可见交付面（与当前激活人格流式对话）。

启动序列（isolated-safe——全部组件可注入，测试零真实网络 / 零真实配置）::

    settings → setup_logging_from_settings → load_all_config
        → PoolManager start → init_database
        → LlmGateway（usage_recorder=wire_default_usage_recorder()）
        → 经 PersonaService 读取当前激活人格

    - 无激活人格：打印中文指引（如何经 /api/v1/personas 创建并激活），
      LIFO 收尾后以退出码 1 结束；
    - 有激活人格：打印横幅 + 开场白，进入 REPL 循环。

REPL 命令面：``/quit`` 退出（码 0）/ ``/clear`` 清空会话历史 / ``/history``
查看回合 / ``/help`` 帮助；其余输入经 :class:`ChatService` 流式回复
（增量 token 直写 stdout）。回车 / EOF（Ctrl-D）等效 ``/quit``。

停机序列（LIFO——启动逆序）：``close_database`` → ``pool_manager.stop``
（含 core 共享线程池关闭）。

用法（backend/ 目录）::

    pixi run chat          # 进入 REPL
    pixi run chat -- --help
"""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Callable
from pathlib import Path

from loguru import logger

import packages.harness.persona.orm as persona_orm
import packages.harness.usage.orm as usage_orm
from packages.harness.chat.chat_models import ChatSessionStateModel
from packages.harness.chat.service import ChatService
from packages.harness.config.loader import load_all_config, setup_logging_from_settings
from packages.harness.config.models import DatabaseConfig, Settings
from packages.harness.config.models import settings as global_settings
from packages.harness.core.errors import BusinessException
from packages.harness.core.time_utils import from_ms
from packages.harness.database.engine import close_database, init_database
from packages.harness.database.session import session_scope
from packages.harness.llm.gateway import LlmGateway, wire_default_usage_recorder
from packages.harness.llm.llm_models import ChatMessageRole
from packages.harness.persona.dtos import PersonaRecordDTO
from packages.harness.persona.service import PersonaService
from packages.harness.pools.manager import get_pool_manager, init_pool_manager

# ORM 模型注册（导入副作用——init_database 的 create_all 兜底建表依赖
# Base.metadata 注册；usage 域经网关默认用量接线在 init 之后才 import，
# 此处显式注册保证 fresh SQLite 启动即有全部表）。
_ORM_REGISTRY = (persona_orm, usage_orm)

#: 输入契约（prompt 参数 + 返回一行文本；EOF 以 EOFError 表达）。
InputFn = Callable[[str], str]
#: 输出契约（原样写出文本——流式增量即 token 粒度直写）。
OutputFn = Callable[[str], None]

#: REPL 帮助文本。
_HELP_TEXT: str = (
    "可用命令：\n"
    "  /quit    退出会话\n"
    "  /clear   清空会话历史（人格设定保留）\n"
    "  /history 查看会话历史回合\n"
    "  /help    显示本帮助\n"
    "其余输入均作为对话内容发送给当前人格。\n"
)

#: 回合角色 → 展示标签。
_ROLE_LABELS: dict[ChatMessageRole, str] = {
    ChatMessageRole.USER: "用户",
    ChatMessageRole.ASSISTANT: "人格",
    ChatMessageRole.SYSTEM: "系统",
}


def _default_output(text: str) -> None:
    """默认输出（原样写出 + 强制刷新——流式增量即时上屏）。

    Args:
        text: 待写出文本（调用方自带换行语义——流式增量即 token 粒度）。
    """
    print(text, end="", flush=True)


async def _teardown_cli_stack() -> None:
    """LIFO 收尾（启动逆序）：关数据库 → 停公共池（含共享线程池）。

    幂等：数据库 holder 未初始化 / 池未 start 时对应步骤为 no-op。
    """
    await close_database()
    manager = get_pool_manager()
    if manager is not None and manager.started:
        await manager.stop()


def _print_history(service: ChatService, state: ChatSessionStateModel, output_fn: OutputFn) -> None:
    """输出会话历史回合（时间 + 角色标签 + 内容）。

    Args:
        service: 聊天服务（历史读取面）。
        state: 会话状态。
        output_fn: 输出回调。
    """
    turns = service.history(state)
    if not turns:
        output_fn("（会话历史为空）\n")
        return
    for turn in turns:
        moment = from_ms(turn.ts_ms)
        clock = moment.strftime("%H:%M:%S") if moment is not None else "--:--:--"
        output_fn(f"[{clock}] {_ROLE_LABELS[turn.role]}: {turn.content}\n")


async def run_chat_cli(
    *,
    settings: Settings | None = None,
    database_config: DatabaseConfig | None = None,
    config_dir: Path | None = None,
    gateway: LlmGateway | None = None,
    persona_service: PersonaService | None = None,
    input_fn: InputFn = input,
    output_fn: OutputFn = _default_output,
) -> int:
    """运行控制台聊天 REPL（全组件可注入——测试零真实网络）。

    Args:
        settings: Settings（None = 全局单例）。
        database_config: 数据库配置（None = ``settings.database``；测试
            注入 tmp SQLite）。
        config_dir: 结构化配置目录（None = CWD 发现；测试注入 tmp_path）。
        gateway: LLM 网关（None = 现场装配——usage 默认接线）。
        persona_service: 人格服务（None = 现场构造无状态实例）。
        input_fn: 输入回调（默认 ``input``；测试注入序列假输入）。
        output_fn: 输出回调（默认 stdout 原样写出；测试注入记录器）。

    Returns:
        进程退出码（0 = 正常退出；1 = 无激活人格）。
    """
    resolved_settings = settings if settings is not None else global_settings
    resolved_persona_service = persona_service if persona_service is not None else PersonaService()
    setup_logging_from_settings(resolved_settings)
    load_all_config(config_dir=config_dir)
    manager = init_pool_manager(resolved_settings)
    try:
        await manager.start()
        await init_database(
            database_config if database_config is not None else resolved_settings.database
        )
        resolved_gateway = (
            gateway
            if gateway is not None
            else LlmGateway(
                settings=resolved_settings,
                usage_recorder=wire_default_usage_recorder(),
            )
        )
        service = ChatService(gateway=resolved_gateway, persona_service=resolved_persona_service)

        record: PersonaRecordDTO | None
        async with session_scope() as db_session:
            record = await resolved_persona_service.get_active_persona(db_session)
        if record is None:
            output_fn(
                "未找到激活人格：当前没有任何处于激活状态的人格，无法开始对话。\n"
                "请先经人格管理 API（/api/v1/personas）完成三步：\n"
                "  1. POST /api/v1/personas                 创建人格（注册名 + chara card）；\n"
                "  2. PUT  /api/v1/personas/{id}            启用人格（status=active）；\n"
                "  3. POST /api/v1/personas/{id}/activate   激活人格（单激活切换）。\n"
                "完成后重新运行 pixi run chat 即可开始对话。\n"
            )
            return 1

        state = await service.create_session(persona=record)
        persona_label = state.display_name or state.persona_name
        output_fn("=" * 46 + "\n")
        output_fn(f" 已连接人格：{persona_label}（注册名 {state.persona_name}）\n")
        output_fn(" 命令：/help 帮助 | /history 历史 | /clear 清空 | /quit 退出\n")
        output_fn("=" * 46 + "\n")
        greeting = service.opening(state)
        output_fn(f"{persona_label}> {greeting}\n")

        while True:
            try:
                line = input_fn("你> ")
            except EOFError:
                output_fn("再见。\n")
                break
            text = line.strip()
            if not text:
                continue
            if text == "/quit":
                output_fn("再见。\n")
                break
            if text == "/clear":
                service.clear_history(state)
                output_fn("会话历史已清空（人格设定保留）。\n")
                continue
            if text == "/history":
                _print_history(service, state, output_fn)
                continue
            if text == "/help":
                output_fn(_HELP_TEXT)
                continue
            try:
                async for token in service.stream_reply(state, text):
                    output_fn(token)
                output_fn("\n")
            except BusinessException as exc:
                output_fn(f"\n[对话失败] {exc.message}（code={exc.code}）\n")
        return 0
    finally:
        await _teardown_cli_stack()


def main() -> None:
    """CLI 入口（``pixi run chat``；``--help`` 原生退出 0——不启动任何组件）。"""
    parser = argparse.ArgumentParser(
        prog="pixi run chat",
        description="ai_agent_companion 控制台聊天——与当前激活人格流式对话",
        epilog=(
            "会话命令：/quit 退出 | /clear 清空历史 | /history 查看历史 | /help 帮助\n"
            "前置条件：已激活至少一个人格（经 /api/v1/personas 管理面）"
        ),
    )
    parser.parse_args()
    logger.info("控制台聊天 CLI 启动")
    raise SystemExit(asyncio.run(run_chat_cli()))


if __name__ == "__main__":
    main()

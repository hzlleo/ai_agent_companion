"""chat 域控制台 REPL——用户可见对话入口（与当前激活人格流式对话）。

启动序列（isolated-safe——全部组件可注入，测试零真实网络 / 零真实配置）::

    settings → setup_logging_from_settings → load_all_config
        → PoolManager start → init_database
        → LlmGateway（usage_recorder=wire_default_usage_recorder()）
        → 经 PersonaService 读取当前激活人格

    - 无激活人格：打印中文指引（如何经 /api/v1/personas 创建并激活），
      LIFO 收尾后以退出码 1 结束；
    - 有激活人格：打印横幅 + 开场白，进入 REPL 循环。

REPL 命令面：``/quit`` 退出（码 0）/ ``/clear`` 清空会话历史 / ``/history``
查看回合 / ``/new`` 新建持久化会话 / ``/sessions`` 列出最近会话 /
``/resume <id>`` 恢复历史会话 / ``/help`` 帮助；其余输入经
:class:`ChatService` 流式回复（增量 token 直写 stdout）。回车 / EOF
（Ctrl-D）等效 ``/quit``。

语音回复（``--voice`` 可选旗标）：

    - **opt-in**：``--voice`` 传入且 ``settings.voice.tts.enabled=True``
      时，每条人格回复在文本流写完后追加语音合成 + 播放；旗标缺省 =
      零语音对象构造、零合成调用；
    - **人格绑定**：合成经 :class:`~packages.harness.voice.voice_service.
      VoiceService` 以当前激活人格绑定（``persona.voice_profile`` 存在时
      其 speaker / speed 覆盖 settings 缺省）；
    - **best-effort 播放**：本地播放器运行时发现（``ffplay`` →
      ``aplay``），阻塞播放经 ``run_blocking_ops`` 下沉线程池（不占事件
      循环）；**无头机器无播放器 = 告警并跳过**（正常形态——文本回复
      不受影响）；播放失败 / 合成失败均仅提示 / 告警，**恒不中断 REPL**；
    - **TTS 未启用**：``--voice`` 传入但 ``VOICE_TTS_ENABLED`` 未开 →
      启动期一行提示后按纯文本对话（语音是叠加面，不是前置条件）。

会话持久化：CLI 恒绑定 ``ChatSessionService``——会话与
回合自动落库；``/clear`` 仅清内存历史（持久化消息保留——全量记录），
``/new`` 起新会话，``/resume`` 回放历史会话续聊。

停机序列（LIFO——启动逆序）：``close_database`` → ``pool_manager.stop``
（含 core 共享线程池关闭）。

用法（backend/ 目录）::

    pixi run chat                        # 进入 REPL（纯文本）
    pixi run chat -- --voice             # 语音回复形态（需 TTS 服务在线）
    pixi run chat -- --help
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import shutil
import subprocess
import tempfile
from collections.abc import Awaitable, Callable
from pathlib import Path

from loguru import logger

import packages.harness.memory.orm as memory_orm
import packages.harness.persona.orm as persona_orm
import packages.harness.usage.orm as usage_orm
from packages.harness.chat.chat_models import ChatSessionStateModel
from packages.harness.chat.service import ChatService
from packages.harness.config.loader import load_all_config, setup_logging_from_settings
from packages.harness.config.models import DatabaseConfig, Settings
from packages.harness.config.models import settings as global_settings
from packages.harness.core.errors import BusinessException
from packages.harness.core.thread_pool import run_blocking_ops
from packages.harness.core.time_utils import from_ms
from packages.harness.database.engine import close_database, init_database
from packages.harness.database.session import session_scope
from packages.harness.llm.gateway import LlmGateway, wire_default_usage_recorder
from packages.harness.llm.llm_models import ChatMessageRole
from packages.harness.memory.dtos import ChatSessionQueryDTO, ChatSessionSummaryDTO
from packages.harness.memory.service import ChatSessionService
from packages.harness.persona.dtos import PersonaRecordDTO
from packages.harness.persona.service import PersonaService, bind_persona_context
from packages.harness.pools.manager import get_pool_manager, init_pool_manager
from packages.harness.voice.voice_service import (
    VoiceService,
    wire_default_voice_usage_recorder,
)

# ORM 模型注册（导入副作用——init_database 的 create_all 兜底建表依赖
# Base.metadata 注册；usage 域经网关默认用量接线在 init 之后才 import，
# 此处显式注册保证 fresh SQLite 启动即有全部表；memory 域四表为会话
# 持久化的落库面，一并注册）。
_ORM_REGISTRY = (persona_orm, usage_orm, memory_orm)

#: 输入契约（prompt 参数 + 返回一行文本；EOF 以 EOFError 表达）。
InputFn = Callable[[str], str]
#: 输出契约（原样写出文本——流式增量即 token 粒度直写）。
OutputFn = Callable[[str], None]
#: 语音播放契约（音频字节 → 是否播放成功；测试注入记录器 fake）。
PlayAudioFn = Callable[[bytes], Awaitable[bool]]

#: 语音播放单次上限秒数（ffplay -autoexit 正常随音频结束退出；上限仅作
#: 播放器僵死兜底——超时放弃本次播放，不冻结 REPL）。
_PLAYBACK_TIMEOUT_SECONDS: float = 600.0

#: REPL 帮助文本。
_HELP_TEXT: str = (
    "可用命令：\n"
    "  /quit        退出会话\n"
    "  /clear       清空会话历史（人格设定保留）\n"
    "  /history     查看会话历史回合\n"
    "  /new         新建会话（持久化，重新开场）\n"
    "  /sessions    列出最近的持久化会话\n"
    "  /resume <id> 恢复指定会话续聊\n"
    "  /help        显示本帮助\n"
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


def _play_wav_blocking(audio: bytes) -> bool:
    """同步播放 WAV 字节（线程池内执行——经 :func:`run_blocking_ops` 下沉）。

    播放器运行时发现（``ffplay`` 优先——``-autoexit`` 随音频结束退出、
    ``-nodisp`` 无窗口、静音日志；``aplay`` 兜底）；音频落临时文件后经
    播放器子进程播放（bounded 超时兜底）。**无头机器无播放器 = False**
    （正常形态——调用方告警并跳过）。

    Args:
        audio: WAV 音频字节。

    Returns:
        是否播放成功（无播放器 / 非零退出 / 超时 / 执行异常均为 False）。
    """
    player = shutil.which("ffplay") or shutil.which("aplay")
    if player is None:
        logger.warning(
            "未找到可用音频播放器（ffplay / aplay）——跳过语音播放（无头机器正常形态）"
        )
        return False
    tmp_path = ""
    try:
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            tmp.write(audio)
            tmp_path = tmp.name
        command = (
            [player, "-autoexit", "-nodisp", "-loglevel", "quiet", tmp_path]
            if player.endswith("ffplay")
            else [player, tmp_path]
        )
        completed = subprocess.run(
            command, capture_output=True, timeout=_PLAYBACK_TIMEOUT_SECONDS, check=False
        )
        if completed.returncode != 0:
            logger.warning(
                "语音播放器退出非零: player={}, code={}", player, completed.returncode
            )
            return False
        return True
    except subprocess.TimeoutExpired:
        logger.warning("语音播放超时（>{}s）——放弃本次播放", _PLAYBACK_TIMEOUT_SECONDS)
        return False
    except OSError as exc:
        logger.warning("语音播放器执行异常: {}", exc)
        return False
    finally:
        if tmp_path:
            # best-effort 清理：临时文件删除失败无害（系统 tmp 目录回收）
            with contextlib.suppress(OSError):
                os.unlink(tmp_path)


async def _play_audio(audio: bytes) -> bool:
    """播放音频（阻塞播放下沉线程池——不占事件循环）。

    Args:
        audio: WAV 音频字节。

    Returns:
        是否播放成功。
    """
    return await run_blocking_ops(_play_wav_blocking, audio)


async def _speak_reply(
    voice_service: VoiceService,
    persona: PersonaRecordDTO,
    reply_text: str,
    output_fn: OutputFn,
    play_audio: PlayAudioFn,
) -> None:
    """合成并播放人格语音回复（best-effort——任何失败恒不中断 REPL）。

    Args:
        voice_service: 语音服务（人格绑定合成）。
        persona: 当前激活人格（voice_profile 绑定消费面）。
        reply_text: 人格完整回复文本（流式增量聚合）。
        output_fn: 输出回调（合成失败提示上屏）。
        play_audio: 播放回调（默认 ffplay/aplay 发现；测试注入 fake）。
    """
    try:
        result = await voice_service.synthesize(reply_text, persona=persona)
    except BusinessException as exc:
        output_fn(f"[语音合成失败] {exc.message}（code={exc.code}）\n")
        return
    except Exception:
        logger.opt(exception=True).warning("语音合成异常（不中断 REPL）")
        return
    played = await play_audio(result.audio)
    if not played:
        logger.warning("语音播放未成功（无可用播放器或播放失败）——文本回复不受影响")


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


def _format_session_line(summary: ChatSessionSummaryDTO) -> str:
    """会话摘要 → 单行展示文本（/sessions 列表项）。

    Args:
        summary: 会话摘要 DTO。

    Returns:
        ``#id [N条] 标题（最后活跃 HH:MM:SS）`` 形态单行。
    """
    moment = from_ms(summary.last_active_at)
    clock = moment.strftime("%H:%M:%S") if moment is not None else "--:--:--"
    return f"  #{summary.id} [{summary.message_count}条] {summary.title}（最后活跃 {clock}）\n"


async def run_chat_cli(
    *,
    settings: Settings | None = None,
    database_config: DatabaseConfig | None = None,
    config_dir: Path | None = None,
    gateway: LlmGateway | None = None,
    persona_service: PersonaService | None = None,
    session_service: ChatSessionService | None = None,
    input_fn: InputFn = input,
    output_fn: OutputFn = _default_output,
    voice: bool = False,
    voice_service: VoiceService | None = None,
    play_audio_fn: PlayAudioFn | None = None,
) -> int:
    """运行控制台聊天 REPL（全组件可注入——测试零真实网络）。

    Args:
        settings: Settings（None = 全局单例）。
        database_config: 数据库配置（None = ``settings.database``；测试
            注入 tmp SQLite）。
        config_dir: 结构化配置目录（None = CWD 发现；测试注入 tmp_path）。
        gateway: LLM 网关（None = 现场装配——usage 默认接线）。
        persona_service: 人格服务（None = 现场构造无状态实例）。
        session_service: 会话持久化服务（None = 现场构造——CLI 恒持久化；
            测试可注入绑定共享引擎的实例）。
            input_fn: 输入回调（默认 ``input``；测试注入序列假输入——
                同步阻塞读取经 ``run_blocking_ops`` 下沉线程池，不占事件
                循环；注入面不变，同步 callable 照常兼容）。
        output_fn: 输出回调（默认 stdout 原样写出；测试注入记录器）。
        voice: 是否启用语音回复（``--voice`` 旗标；须
            ``settings.voice.tts.enabled=True`` 方生效——未启用时提示后
            按纯文本继续）。
        voice_service: 语音服务（None + voice 生效时 = 现场装配——usage
            默认接线；测试注入 fake 记录合成调用）。
        play_audio_fn: 播放回调（None = ffplay/aplay 运行时发现 + 经
            run_blocking_ops 下沉；测试注入记录器 fake）。

    Returns:
        进程退出码（0 = 正常退出；1 = 无激活人格）。
    """
    resolved_settings = settings if settings is not None else global_settings
    resolved_persona_service = persona_service if persona_service is not None else PersonaService()
    resolved_session_service = (
        session_service if session_service is not None else ChatSessionService()
    )
    resolved_play_audio = play_audio_fn if play_audio_fn is not None else _play_audio
    resolved_voice_service: VoiceService | None = None
    voice_disabled_hint = False
    if voice:
        if resolved_settings.voice.tts.enabled:
            resolved_voice_service = (
                voice_service
                if voice_service is not None
                else VoiceService(usage_recorder=wire_default_voice_usage_recorder())
            )
        else:
            voice_disabled_hint = True
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
        service = ChatService(
            gateway=resolved_gateway,
            persona_service=resolved_persona_service,
            session_service=resolved_session_service,
        )

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

        async with session_scope() as db_session:
            state = await service.create_session(
                persona=record,
                db_session=db_session,
                title=record.display_name or record.name,
            )
        persona_label = state.display_name or state.persona_name
        if voice_disabled_hint:
            output_fn(
                "[语音] TTS 未启用（VOICE_TTS_ENABLED=true 后 --voice 方可生效）"
                "——本次以纯文本对话。\n"
            )
        output_fn("=" * 46 + "\n")
        output_fn(f" 已连接人格：{persona_label}（注册名 {state.persona_name}）\n")
        output_fn(
            " 命令：/help 帮助 | /history 历史 | /clear 清空 | /new 新会话"
            " | /sessions 会话列表 | /resume 恢复 | /quit 退出\n"
        )
        output_fn("=" * 46 + "\n")
        greeting = service.opening(state)
        output_fn(f"{persona_label}> {greeting}\n")

        while True:
            try:
                line = await run_blocking_ops(input_fn, "你> ")
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
                output_fn("会话历史已清空（人格设定保留；持久化消息不受影响）。\n")
                continue
            if text == "/history":
                _print_history(service, state, output_fn)
                continue
            if text == "/new":
                try:
                    async with session_scope() as db_session:
                        state = await service.create_session(
                            persona=record,
                            db_session=db_session,
                            title=record.display_name or record.name,
                        )
                except BusinessException as exc:
                    output_fn(f"[新建失败] {exc.message}（code={exc.code}）\n")
                    continue
                greeting = service.opening(state)
                output_fn(f"已新建会话 #{state.session_id}。\n{persona_label}> {greeting}\n")
                continue
            if text == "/sessions":
                try:
                    async with session_scope() as db_session, bind_persona_context(record.id):
                        page = await resolved_session_service.list_sessions(
                            db_session, ChatSessionQueryDTO(limit=10)
                        )
                except BusinessException as exc:
                    output_fn(f"[查询失败] {exc.message}（code={exc.code}）\n")
                    continue
                if not page.items:
                    output_fn("（暂无历史会话）\n")
                else:
                    output_fn("最近的会话：\n")
                    for summary in page.items:
                        output_fn(_format_session_line(summary))
                continue
            if text.startswith("/resume"):
                parts = text.split()
                if len(parts) != 2 or not parts[1].isdigit():
                    output_fn("用法：/resume <会话ID>（经 /sessions 查看）\n")
                    continue
                resumed: ChatSessionStateModel | None = None
                async with session_scope() as db_session, bind_persona_context(record.id):
                    try:
                        resumed = await service.resume_session(
                            int(parts[1]), db_session=db_session
                        )
                    except BusinessException as exc:
                        output_fn(f"[恢复失败] {exc.message}（code={exc.code}）\n")
                if resumed is not None:
                    state = resumed
                    persona_label = state.display_name or state.persona_name
                    greeting = service.opening(state)
                    output_fn(
                        f"已恢复会话 #{state.session_id}（{len(state.turns)}条历史回合）。\n"
                        f"{persona_label}> {greeting}\n"
                    )
                continue
            if text == "/help":
                output_fn(_HELP_TEXT)
                continue
            try:
                reply_parts: list[str] = []
                async for token in service.stream_reply(state, text):
                    output_fn(token)
                    if resolved_voice_service is not None:
                        reply_parts.append(token)
                output_fn("\n")
                if resolved_voice_service is not None and reply_parts:
                    await _speak_reply(
                        resolved_voice_service,
                        record,
                        "".join(reply_parts),
                        output_fn,
                        resolved_play_audio,
                    )
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
            "会话命令：/quit 退出 | /clear 清空历史 | /history 查看历史 | /new 新会话"
            " | /sessions 会话列表 | /resume 恢复 | /help 帮助\n"
            "前置条件：已激活至少一个人格（经 /api/v1/personas 管理面）"
        ),
    )
    parser.add_argument(
        "--voice",
        action="store_true",
        help=(
            "启用语音回复（人格回复合成语音后本地播放；须 VOICE_TTS_ENABLED=true"
            " 且 TTS 服务在线；无头机器无 ffplay/aplay 时自动跳过播放）"
        ),
    )
    args = parser.parse_args()
    logger.info("控制台聊天 CLI 启动: voice={}", args.voice)
    raise SystemExit(asyncio.run(run_chat_cli(voice=args.voice)))


if __name__ == "__main__":
    main()

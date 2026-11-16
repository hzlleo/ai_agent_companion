"""app 装配——create_app 工厂 + lifespan 生命周期（P0-6 最终装配面）。

装配契约：

    - :func:`create_app` —— FastAPI 工厂（共享异常处理器 + personas /
      memories / usage / models / voice / health 路由 + CORS + lifespan）；
    - :func:`startup_app_state` / :func:`shutdown_app_state` —— **可独立
      驱动的纯异步函数对**（不藏在 lifespan 闭包内——测试无需 ASGI
      lifespan runner 即可直调，本工程不引入 asgi-lifespan 依赖）；
    - ``lifespan`` —— 上述函数对的 ``asynccontextmanager`` 薄封装。

启动序列（LIFO 之源——停机严格逆序）::

    setup_logging_from_settings(settings)     日志先行（后续步骤可观测）
        ↓ load_all_config(config_dir)          config.yaml / 工具清单
        ↓ init_database(settings.database)     引擎 + 会话工厂（槽位）
        ↓ init_pool_manager + start            公共池（线程 / HTTP / LLM 条目）
        ↓ app.state.gateway = LlmGateway(...)  网关上挂（默认用量接线）
        ↓ _startup_channels(app, settings)  通道层（TELEGRAM_ENABLED 时
                                                装配 runner 后台任务；token
                                                缺失 fail fast；QQ_ENABLED
                                                时注册适配器 + 启动校验
                                                warn-and-continue；缺省 no-op）
        ↓ _startup_scheduler(app, settings)    中央调度器（DUTY_ENABLED /
                                                 DREAM_SCHEDULE_ENABLED 任一
                                                 开启时装配；**后于通道**——
                                                 duty 报告出站复用通道路由器；
                                                 缺省 no-op；duty 启用时
                                                 _register_sandbox_tools 在
                                                 内置工具后追加沙盒工具面）

停机序列（启动逆序）::

    _shutdown_scheduler(app)                  调度器优雅停机（先于通道——
                                              duty 出站消费通道路由器）
        ↓ _shutdown_channels(app)             通道 runner 优雅停机（先于池——
                                              runner 消费共享 HTTP client）
        ↓ pool_manager.stop()                  HTTP / LLM 条目池 + 共享线程池
        ↓ close_database()                     引擎 dispose + 槽位复位
        ↓ shutdown_shared_thread_pool()        幂等兜底（pool stop 已关）

``app.state.gateway``：为后续 API 消费方（会话路由 / 通道层）预装配的
chat 角色网关；P0 的对话入口为控制台 CLI（自建等价栈，见
harness.chat.cli）。

CORS（P2 chunk 1）：``ServerConfig.cors_origins`` 驱动——非空时挂
``CORSMiddleware``（``allow_credentials=True`` + 显式 methods / headers；
默认来源为 Vite dev server 两形态），空列表 = 不挂中间件（同源部署
形态，CORS 关闭）。隔离测试经 ``create_app(settings=...)`` 注入自定义
来源。

ORM 注册：persona / usage / memory 三域 ORM 在本模块导入期注册到
``Base.metadata``——保证 ``init_database`` 的 create_all 兜底建表覆盖
全部域表（fresh SQLite 首启即可用；生产 schema 以 Alembic 为准）。
memory 域经 memories 路由导入链注册。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import cast

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from loguru import logger

import packages.harness.persona.orm as persona_orm
import packages.harness.usage.orm as usage_orm
from app.api.channels_qq import router as qq_channels_router
from app.api.health import APP_VERSION
from app.api.health import router as health_router
from app.api.look_at import router as look_at_router
from app.api.memories import router as memories_router
from app.api.models import router as models_router
from app.api.personas import router as personas_router
from app.api.usage import router as usage_router
from app.api.voice import router as voice_router
from app.middleware.error_handler import register_exception_handlers
from packages.harness.channels.channel_models import ChannelType
from packages.harness.channels.channel_service import ChannelRouter
from packages.harness.channels.qq.qq_adapter import NapCatQQAdapter
from packages.harness.channels.qq.qq_client import NapCatOneBotClient
from packages.harness.channels.telegram.tg_client import TelegramClient
from packages.harness.channels.telegram.tg_runner import TelegramChannelRunner
from packages.harness.chat.service import ChatService
from packages.harness.config.loader import load_all_config, setup_logging_from_settings
from packages.harness.config.models import Settings
from packages.harness.config.models import settings as global_settings
from packages.harness.core.background_tasks import background_task_manager
from packages.harness.core.errors import BusinessException, ErrorCode
from packages.harness.core.thread_pool import shutdown_shared_thread_pool
from packages.harness.database.engine import close_database, init_database
from packages.harness.database.session import session_scope
from packages.harness.duty.duty_models import DutyRunConfigModel
from packages.harness.duty.duty_service import DutyService
from packages.harness.llm.gateway import LlmGateway, wire_default_usage_recorder
from packages.harness.memory.dreaming import DreamService
from packages.harness.memory.retrieval import MemorySearchService, build_memory_store
from packages.harness.memory.service import ChatSessionService, MemoryService
from packages.harness.persona.service import PersonaService
from packages.harness.pools.manager import get_pool_manager, init_pool_manager
from packages.harness.scheduler import Scheduler, build_dream_trigger_job
from packages.harness.tools.registry import ToolRegistry, register_builtin_tools
from packages.harness.tools.sandbox import (
    build_sandbox_client,
    register_sandbox_tool,
    register_sandbox_tools_for_bili,
)
from packages.harness.tools.sandbox_models import (
    BILI_TOOL_NAMES,
    HTTP_FETCH_TOOL_NAME,
    build_http_fetch_spec,
)

# ORM 模型注册（导入副作用——见模块 docstring「ORM 注册」一节）。
_ORM_REGISTRY = (persona_orm, usage_orm)

#: CORS 显式放行方法面（管理面全操作集）。
_CORS_ALLOW_METHODS: list[str] = ["GET", "POST", "PUT", "DELETE", "OPTIONS"]
#: CORS 显式放行请求头面（JSON API 最小集 + 鉴权）。
_CORS_ALLOW_HEADERS: list[str] = ["Accept", "Authorization", "Content-Type"]
#: 通道 runner 优雅停机等待秒数（在途长轮询最多一个窗口——超时 cancel 兜底）。
_CHANNELS_STOP_TIMEOUT_SECONDS: float = 5.0
#: 调度器优雅停机等待秒数（tick 收尾 + 在途作业排水——超时 cancel 兜底）。
_SCHEDULER_STOP_TIMEOUT_SECONDS: float = 10.0

__all__ = [
    "APP_VERSION",
    "create_app",
    "lifespan",
    "startup_app_state",
    "shutdown_app_state",
]


async def startup_app_state(
    app: FastAPI, settings: Settings, *, config_dir: Path | None = None
) -> None:
    """启动全部进程级组件并装配 ``app.state``（可独立驱动的纯异步函数）。

    Args:
        app: 待装配的 FastAPI 应用（gateway 上挂到 ``app.state``）。
        settings: Settings（生产传全局单例；测试传隔离实例）。
        config_dir: 结构化配置目录（None = CWD 发现；测试注入 tmp_path）。

    Raises:
        BusinessException: 配置解析失败（``CONFIG_INVALID``——fail loud，
            半生效配置不可审计）；通道启用但 bot token 缺失
            （``CHANNEL_TOKEN_MISSING``——fail loud，装配期拒绝启动）。
    """
    setup_logging_from_settings(settings)
    load_all_config(config_dir=config_dir)
    await init_database(settings.database)
    manager = init_pool_manager(settings)
    await manager.start()
    app.state.gateway = LlmGateway(
        settings=settings,
        usage_recorder=wire_default_usage_recorder(),
    )
    await _startup_channels(app, settings)
    await _startup_scheduler(app, settings)


async def shutdown_app_state(app: FastAPI) -> None:
    """停机全部进程级组件（启动严格逆序；幂等——组件未启动时 no-op）。

    Args:
        app: 待收尾的 FastAPI 应用（``app.state.gateway`` 置 None）。
    """
    await _shutdown_scheduler(app)
    await _shutdown_channels(app)
    manager = get_pool_manager()
    if manager is not None and manager.started:
        await manager.stop()
    await close_database()
    # 共享线程池幂等兜底（pool stop 已关闭；防御独立初始化路径）。
    shutdown_shared_thread_pool(wait=True)
    app.state.gateway = None


async def _startup_channels(app: FastAPI, settings: Settings) -> None:
    """装配通道层（``TELEGRAM_ENABLED`` 长轮询 runner + ``QQ_ENABLED`` 适配器）。

    装配链（单一 ChannelRouter 服务全部通道——会话按 ``channel:chat_id``
    维度天然隔离）：任一通道启用时构造 ChannelRouter（逐消息 ChatService
    工厂闭包 ``app.state.gateway`` + ``session_scope`` 持久化）；telegram
    启用时再装配 TelegramClient + TelegramChannelRunner → create_task +
    ``background_task_manager`` 托管；QQ 启用时装配 NapCatOneBotClient +
    NapCatQQAdapter → ``router.register_adapter`` + 启动校验
    get_login_info。全部关闭时 no-op（零通道对象构造）。

    **QQ 与 telegram 的失败语义差异（锁定决策）**：telegram token 缺失
    fail loud（``CHANNEL_TOKEN_MISSING``——app 拒绝启动）；QQ 启动校验
    失败（NapCat 不可达 / token 无效）**warn-and-continue**——适配器仍
    注册（入站 webhook 照常确认），出站发送失败逐消息经路由器兜底隔离。
    理由：QQ 入站是推送式（NapCat 侧配置上报目标——后端启动时 NapCat
    尚未就绪是常见部署时序），且 QQ 通道无长驻任务可终止；fail fast
    只会因部署顺序杀掉整个 app。

    Args:
        app: 待装配的 FastAPI 应用（router / runner / task 上挂
            ``app.state``——router 供后续 duty 等出站投递面复用 + QQ
            webhook 端点入站消费）。
        settings: 生效 Settings（``settings.channels.telegram`` /
            ``settings.channels.qq`` 分节）。

    Raises:
        BusinessException: telegram token 环境变量缺失 / 为空
            （``CHANNEL_TOKEN_MISSING``——fail loud）；公共池未启动
            （``INTERNAL_ERROR``——调用顺序契约破坏面）。
    """
    app.state.channels_runner = None
    app.state.channels_task = None
    app.state.channels_router = None
    telegram = settings.channels.telegram
    qq = settings.channels.qq
    if not telegram.enabled and not qq.enabled:
        logger.info("通道层未启用（TELEGRAM_ENABLED / QQ_ENABLED 均关闭）——跳过装配")
        return
    manager = get_pool_manager()
    if manager is None or not manager.started:
        raise BusinessException(
            code=ErrorCode.INTERNAL_ERROR,
            message="通道装配前置失败：公共池未启动（lifespan 装配顺序破坏）",
        )
    gateway = app.state.gateway
    router = ChannelRouter(
        chat_service_factory=lambda: ChatService(
            gateway=gateway,
            session_service=ChatSessionService(),
        ),
        session_factory=session_scope,
    )
    if telegram.enabled:
        client = TelegramClient(
            manager.get_http_client(),
            bot_token_env=telegram.bot_token_env,
            api_base=telegram.api_base,
            poll_timeout_seconds=telegram.poll_timeout_seconds,
        )
        runner = TelegramChannelRunner(
            client=client,
            router=router,
            backoff_base_seconds=telegram.backoff_base_seconds,
            backoff_max_seconds=telegram.backoff_max_seconds,
            state_dir=telegram.state_dir,
        )
        router.register_adapter(ChannelType.TELEGRAM, runner)
        task = asyncio.create_task(runner.run(), name="telegram-channel-runner")
        background_task_manager.register(task)
        task.add_done_callback(_log_channel_task_termination)
        app.state.channels_runner = runner
        app.state.channels_task = task
        logger.info(
            "Telegram 通道 runner 已启动（managed background task）: state_dir={}",
            telegram.state_dir,
        )
    if qq.enabled:
        qq_client = NapCatOneBotClient(
            manager.get_http_client(),
            base_url=qq.http_api_base_url,
            access_token_env=qq.access_token_env,
            request_timeout_seconds=qq.request_timeout_seconds,
        )
        adapter = NapCatQQAdapter(client=qq_client)
        router.register_adapter(ChannelType.QQ, adapter)
        await _validate_qq_startup(qq_client)
        logger.info(
            "QQ 通道适配器已注册（webhook 入站 + NapCat HTTP API 出站）: base_url={}",
            qq.http_api_base_url,
        )
    app.state.channels_router = router


async def _validate_qq_startup(client: NapCatOneBotClient) -> None:
    """QQ 启动校验 get_login_info（warn-and-continue——不阻断启动）。

    与 telegram ``_startup_validate`` 的差异（文档化决策）：telegram 是
    长驻轮询任务（token 无效 fail fast 终止任务 + 错误日志）；QQ 无长驻
    任务且入站为推送式——校验失败仅告警，适配器保持注册（出站失败逐
    消息经路由器兜底隔离，不因启动时序杀 app）。

    Args:
        client: NapCat OneBot v11 客户端（共享池 client 承载）。
    """
    try:
        login = await client.get_login_info()
    except BusinessException as exc:
        logger.warning(
            "QQ 通道启动校验失败（warn-and-continue——适配器仍注册，出站失败将逐消息报错）: {}",
            exc.message,
        )
        return
    logger.info(
        "QQ 通道启动校验通过: user_id={}, nickname={}",
        login.user_id,
        login.nickname or "(未提供)",
    )


async def _shutdown_channels(app: FastAPI) -> None:
    """停机通道层（先于公共池——通道组件消费共享 HTTP client；幂等）。

    停机链（telegram runner）：``request_stop``（优雅）→ bounded 等待任务
    收尾 → 超时 ``cancel`` 兜底（在途长轮询被取消——offset 已逐批持久化，
    重启安全）。QQ 通道无长驻任务（推送式入站 + 适配器注册态）——仅随
    ``channels_router`` 槽位复位（共享池 client 生命周期归 pools）。

    Args:
        app: 待收尾的 FastAPI 应用（``channels_router`` / ``channels_runner``
            / ``channels_task`` 槽位复位 None）。
    """
    runner: TelegramChannelRunner | None = getattr(app.state, "channels_runner", None)
    task: asyncio.Task[None] | None = getattr(app.state, "channels_task", None)
    app.state.channels_runner = None
    app.state.channels_task = None
    app.state.channels_router = None
    if task is None or task.done():
        return
    if runner is not None:
        runner.request_stop()
    try:
        await asyncio.wait_for(task, timeout=_CHANNELS_STOP_TIMEOUT_SECONDS)
    except TimeoutError:
        # wait_for 超时路径已 cancel 任务并等待取消收尾——此处仅记录
        logger.warning("Telegram 通道停机等待超时——已强制取消长轮询任务")
    except Exception as exc:
        logger.warning("Telegram 通道任务以异常收尾: {}", exc)
    logger.info("Telegram 通道 runner 已停机")


def _log_channel_task_termination(task: asyncio.Task[None]) -> None:
    """通道后台任务终态日志（异常终止 / 取消均落日志——不留哑任务）。

    Args:
        task: 已完成（或已取消）的后台任务。
    """
    if task.cancelled():
        logger.info("Telegram 通道任务已被取消（停机兜底路径）")
        return
    exc = task.exception()
    if exc is not None:
        logger.error("Telegram 通道任务异常终止: {}", exc)


async def _startup_scheduler(app: FastAPI, settings: Settings) -> None:
    """装配中央调度器（duty / dream 任一启用时注册作业并启动后台任务）。

    装配链（顺序锁定）：工具注册表（内置工具——get_current_time 恒注册；
    search_memory 经记忆检索服务注入，存储与 dream 作业共用同一
    ``build_memory_store`` 产出——SeekDB 形态下不双建池）→ DutyService
    （网关 / 注册表 / 通道路由器 + 属主会话——**通道先行装配**，未启用
    通道时路由器为 None → 报告仅落记忆）→ Scheduler（tick 来自
    Settings.scheduler）→ 注册 duty 每日作业 + dream 每日作业（按开关）
    → ``create_task(scheduler.run())`` + ``background_task_manager`` 托管。
    全部关闭时 no-op（零调度器对象构造）。

    Args:
        app: 待装配的 FastAPI 应用（scheduler / scheduler_task /
            duty_service / duty_registry 上挂 ``app.state``）。
        settings: 生效 Settings（``settings.duty`` / ``settings.dream_schedule``
            / ``settings.scheduler`` 分节）。
    """
    app.state.scheduler = None
    app.state.scheduler_task = None
    app.state.duty_service = None
    app.state.duty_registry = None
    duty_enabled = settings.duty.enabled
    dream_enabled = settings.dream_schedule.enabled
    if not duty_enabled and not dream_enabled:
        logger.info("中央调度器未启用（DUTY_ENABLED / DREAM_SCHEDULE_ENABLED 均关闭）——跳过装配")
        return
    store = build_memory_store(settings)
    scheduler = Scheduler(tick_seconds=settings.scheduler.tick_seconds)
    if duty_enabled:
        registry = ToolRegistry()
        register_builtin_tools(
            registry,
            search_service=MemorySearchService(
                store=store,
                embedder=app.state.gateway.embed_by_role,
            ),
        )
        await _register_sandbox_tools(app, settings, registry)
        duty_service = DutyService(
            gateway=app.state.gateway,
            registry=registry,
            memory_service=MemoryService(),
            persona_service=PersonaService(),
            channel_router=app.state.channels_router,
            owner_chat_id=settings.channels.telegram.owner_chat_id,
            report_to_owner=settings.duty.report_to_owner,
        )
        duty_cfg = DutyRunConfigModel(
            max_tool_steps=settings.duty.max_tool_steps,
            charter_text=settings.duty.charter_text,
        )

        async def _duty_job() -> None:
            """单轮值班作业（每轮新会话——fail-closed 恒不抛）。"""
            async with session_scope() as session:
                report = await duty_service.run_duty_round(session, cfg=duty_cfg)
            logger.info(
                "值班调度轮次收束: persona={}, status={}, steps={}, owner_delivery={}",
                report.persona,
                report.status.value,
                len(report.steps),
                report.owner_delivery.value,
            )

        scheduler.register_daily("duty", _duty_job, daily_at=settings.duty.daily_at)
        app.state.duty_service = duty_service
        app.state.duty_registry = registry
    if dream_enabled:
        dream_job = build_dream_trigger_job(
            dream_service=DreamService(),
            gateway=app.state.gateway,
            persona_service=PersonaService(),
            session_factory=session_scope,
            store=store,
        )
        scheduler.register_daily("dream", dream_job, daily_at=settings.dream_schedule.daily_at)
    task = asyncio.create_task(scheduler.run(), name="central-scheduler")
    background_task_manager.register(task)
    task.add_done_callback(_log_scheduler_task_termination)
    app.state.scheduler = scheduler
    app.state.scheduler_task = task
    logger.info(
        "中央调度器已启动（managed background task）: jobs={}, tick_seconds={}",
        scheduler.job_names,
        settings.scheduler.tick_seconds,
    )


async def _register_sandbox_tools(app: FastAPI, settings: Settings, registry: ToolRegistry) -> None:
    """装配沙盒工具面（enabled + token 就绪 + /health 探测成功时注册 http_fetch + B 站只读工具组）。

    可选组件 warn-and-continue 语义（沙盒是可选运行时——锁定决策）：
    token 未配置 / 探测失败 → 跳过沙盒工具注册并告警，duty 工具面回落
    到内置工具（get_current_time + search_memory——值班轮照常运行）。
    探测经共享 httpx 池（bounded 超时 = ``request_timeout_seconds``）。
    仅在 duty 启用时被调用（沙盒工具的消费面是 duty / persona 工具循环）。

    Args:
        app: 待装配的 FastAPI 应用（本函数不新增 state 槽位——代理执行器
            闭包持有客户端引用）。
        settings: 生效 Settings（``settings.tools_sandbox`` 分节）。
        registry: duty 工具注册表（沙盒工具追加装配目标）。

    Raises:
        BusinessException: 公共池未启动（``INTERNAL_ERROR``——调用顺序
            契约破坏面；与通道装配同前置检查）。
    """
    sandbox = settings.tools_sandbox
    if not sandbox.enabled:
        logger.info("工具沙盒未启用（TOOLS_SANDBOX_ENABLED=false）——跳过装配")
        return
    manager = get_pool_manager()
    if manager is None or not manager.started:
        raise BusinessException(
            code=ErrorCode.INTERNAL_ERROR,
            message="工具沙盒装配前置失败：公共池未启动（lifespan 装配顺序破坏）",
        )
    client = build_sandbox_client(manager.get_http_client(), sandbox)
    if not client.has_token:
        logger.warning(
            "工具沙盒 token 未配置（环境变量 {} 为空）——跳过沙盒工具注册，"
            "duty 工具面仅内置工具",
            sandbox.token_env,
        )
        return
    if not await client.probe_health():
        logger.warning(
            "工具沙盒健康探测失败（{}，超时 {}s 上限）——跳过沙盒工具注册，"
            "duty 工具面仅内置工具",
            sandbox.base_url,
            sandbox.request_timeout_seconds,
        )
        return
    register_sandbox_tool(registry, client, build_http_fetch_spec())
    register_sandbox_tools_for_bili(registry, client)
    logger.info(
        "工具沙盒工具已注册: tools=[{}]",
        ", ".join((HTTP_FETCH_TOOL_NAME, *BILI_TOOL_NAMES)),
    )


async def _shutdown_scheduler(app: FastAPI) -> None:
    """停机中央调度器（先于通道——duty 出站消费通道路由器；幂等）。

    停机链：``request_stop``（优雅——tick 循环 + 在途作业排水）→
    bounded 等待任务收尾 → 超时 ``cancel`` 兜底（排水封顶由调度器
    ``drain_timeout_seconds`` 承担，本层等待覆盖 tick + 排水全程）。

    Args:
        app: 待收尾的 FastAPI 应用（scheduler / scheduler_task /
            duty_service / duty_registry 槽位复位 None）。
    """
    scheduler: Scheduler | None = getattr(app.state, "scheduler", None)
    task: asyncio.Task[None] | None = getattr(app.state, "scheduler_task", None)
    app.state.scheduler = None
    app.state.scheduler_task = None
    app.state.duty_service = None
    app.state.duty_registry = None
    if task is None or task.done():
        if task is not None and not task.cancelled():
            _log_scheduler_task_termination(task)
        return
    if scheduler is not None:
        scheduler.request_stop()
    try:
        await asyncio.wait_for(task, timeout=_SCHEDULER_STOP_TIMEOUT_SECONDS)
    except TimeoutError:
        # wait_for 超时路径已 cancel 任务并等待取消收尾——此处仅记录
        logger.warning("中央调度器停机等待超时——已强制取消调度任务")
    except Exception as exc:
        logger.warning("中央调度器任务以异常收尾: {}", exc)
    logger.info("中央调度器已停机")


def _log_scheduler_task_termination(task: asyncio.Task[None]) -> None:
    """调度器后台任务终态日志（异常终止 / 取消均落日志——不留哑任务）。

    Args:
        task: 已完成（或已取消）的后台任务。
    """
    if task.cancelled():
        logger.info("中央调度器任务已被取消（停机兜底路径）")
        return
    exc = task.exception()
    if exc is not None:
        logger.error("中央调度器任务异常终止: {}", exc)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """应用生命周期（startup / shutdown 函数对的薄封装）。

    Args:
        app: FastAPI 应用实例（``app.state.settings`` 由 :func:`create_app`
            依注入参数上挂——隔离测试经 ``create_app(settings=...)`` 驱动；
            未上挂时回落全局单例）。

    Yields:
        无载荷（服务就绪窗口）。
    """
    settings = cast("Settings | None", getattr(app.state, "settings", None)) or global_settings
    await startup_app_state(app, settings)
    try:
        yield
    finally:
        await shutdown_app_state(app)


def _install_cors(app: FastAPI, settings: Settings) -> None:
    """按 ``ServerConfig.cors_origins`` 挂 CORSMiddleware（空列表 = 关闭）。

    Args:
        app: 待挂中间件的 FastAPI 应用。
        settings: 生效 Settings（origins / credentials / 显式方法与头面）。
    """
    origins = settings.server.cors_origins
    if not origins:
        return
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=True,
        allow_methods=_CORS_ALLOW_METHODS,
        allow_headers=_CORS_ALLOW_HEADERS,
    )


def create_app(settings: Settings | None = None) -> FastAPI:
    """构造 FastAPI 应用（路由 + 异常处理器 + CORS + lifespan 一次性装配）。

    Args:
        settings: 生效 Settings（None = 全局单例 ``global_settings``——
            生产形态；隔离测试注入自定义实例——CORS 来源等 env 面随之
            隔离，并经 ``app.state.settings`` 传递给 lifespan）。

    Returns:
        已完成静态装配的 :class:`FastAPI`（进程级组件在 lifespan 启动
        期装配——见 :func:`startup_app_state`）。
    """
    effective_settings = settings if settings is not None else global_settings
    app = FastAPI(
        title="ai_agent_companion",
        version=APP_VERSION,
        description="自建「大肥鱼」式 AI 陪伴智能体 - 后端服务",
        lifespan=lifespan,
    )
    register_exception_handlers(app)
    _install_cors(app, effective_settings)
    app.include_router(personas_router)
    app.include_router(memories_router)
    app.include_router(usage_router)
    app.include_router(voice_router)
    app.include_router(look_at_router)
    app.include_router(models_router)
    app.include_router(qq_channels_router)
    app.include_router(health_router)
    app.state.settings = effective_settings
    return app

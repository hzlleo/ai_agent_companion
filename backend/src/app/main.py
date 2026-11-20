"""app 装配——create_app 工厂 + lifespan 生命周期（最终装配面）。

装配契约：

    - :func:`create_app` —— FastAPI 工厂（共享异常处理器 + personas /
      memories / usage / voice / voiceprints / look_at / stickers / models /
      search / reminders / directives / facts / skills / core-memory /
      canvas 管理路由 + channels-qq / health 豁免路由 + 实时语音 WS 路由
      + 管理面 owner-token 鉴权（管理路由统一挂接；health / qq webhook /
      realtime WS 豁免——后两者自鉴权，豁免契约见
      :func:`_include_routers`）+ CORS + lifespan）；
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
        ↓ app.state.memory_store = build_memory_store(settings)
                                                 记忆检索存储上挂（检索 /
                                                 梦境 / 调度器共用同一产出）
        ↓ _startup_search(app, settings)       搜索能力（SEARCH_ENABLED 时：
                                                 search 段加载 + 启动校验 +
                                                 SearchService + web_search
                                                 工具面；**先于通道**——通道
                                                 ChatService 工厂消费工具面；
                                                 缺省 no-op）
        ↓ _startup_channels(app, settings)  通道层（TELEGRAM_ENABLED 时
                                                 装配 runner 后台任务；表情包启用时
                                                 接线附件 v2 收发桥；token
                                                 缺失 fail fast；QQ_ENABLED
                                                 时注册适配器 + 启动校验
                                                 warn-and-continue；WECOM_ENABLED
                                                 时装配企微智能机器人长连接
                                                 runner（配置 / 凭据面失败仅
                                                 跳过该通道）；任一通道启用时
                                                 先装配 rhythm / intent
                                                 会话性组件——单一随机源，
                                                 INTENT_ENABLED 关闭时快径
                                                 面 None；缺省 no-op）
        ↓ _startup_scheduler(app, settings)    中央调度器（DUTY_ENABLED /
                                                 DREAM_SCHEDULE_ENABLED 任一
                                                 开启时装配；**后于通道**——
                                                 duty 报告出站复用通道路由器；
                                                 缺省 no-op；duty 启用时工具
                                                 注册表在内置工具后追加沙盒
                                                 工具面、look_at 工具与
                                                 web_search 工具）

停机序列（启动逆序）::

    _shutdown_scheduler(app)                  调度器优雅停机（先于通道——
                                              duty 出站消费通道路由器）
        ↓ _shutdown_channels(app)             通道 runner 优雅停机（telegram /
                                              企微长连接——先于池，runner 消费
                                              共享 HTTP client）
        ↓ background_task_manager.shutdown()  全局后台任务 drain（先于池——
                                              fire-and-forget 索引等在途
                                              任务消费 SeekDB 池 / 共享
                                              client；is_shutting_down 标志
                                              同步置位）
        ↓ pool_manager.stop()                 HTTP / LLM 条目池 + 共享线程池
        ↓ close_database()                    引擎 dispose + 槽位复位
        ↓ shutdown_shared_thread_pool()       幂等兜底（pool stop 已关）

``app.state.gateway``：为后续 API 消费方（会话路由 / 通道层）预装配的
chat 角色网关；当前对话入口为控制台 CLI（自建等价栈，见
harness.chat.cli）。``app.state.memory_store``：记忆检索存储（检索 /
梦境端点与调度器共用——memories 路由经 ``request.app.state`` 读取，
未装配 lifespan 的本地测试面回落按请求设置 once 构建上挂）。

CORS：``ServerConfig.cors_origins`` 驱动——非空时挂
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
import random
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, cast

import httpx
from fastapi import APIRouter, Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from loguru import logger

import packages.harness.canvas.canvas_orm as canvas_orm
import packages.harness.multimodal.sticker_orm as sticker_orm
import packages.harness.persona.orm as persona_orm
import packages.harness.usage.orm as usage_orm
import packages.harness.voice.voiceprint_orm as voiceprint_orm
from app.api.canvas import router as canvas_router
from app.api.channels_qq import router as qq_channels_router
from app.api.core_memory import router as core_memory_router
from app.api.directives import router as directives_router
from app.api.facts import router as facts_router
from app.api.health import APP_VERSION
from app.api.health import router as health_router
from app.api.look_at import router as look_at_router
from app.api.memories import router as memories_router
from app.api.models import router as models_router
from app.api.personas import router as personas_router
from app.api.realtime_voice import router as realtime_voice_router
from app.api.reminders import router as reminders_router
from app.api.search import router as search_router
from app.api.skills import router as skills_router
from app.api.stickers import router as stickers_router
from app.api.usage import router as usage_router
from app.api.voice import router as voice_router
from app.api.voiceprints import router as voiceprints_router
from app.middleware.admin_auth import (
    log_admin_auth_startup_notice,
    require_admin_token,
)
from app.middleware.error_handler import register_exception_handlers
from packages.harness.channels.channel_models import ChannelType
from packages.harness.channels.channel_service import ChannelRouter
from packages.harness.channels.douyin.douyin_client import DouyinWebClient
from packages.harness.channels.douyin.douyin_runner import DouyinChannelRunner
from packages.harness.channels.qq.qq_adapter import NapCatQQAdapter
from packages.harness.channels.qq.qq_client import NapCatOneBotClient
from packages.harness.channels.telegram.tg_client import TelegramClient
from packages.harness.channels.telegram.tg_runner import TelegramChannelRunner
from packages.harness.channels.wecom.wecom_client import WeComLongConnectionClient
from packages.harness.channels.wecom.wecom_runner import WeComChannelRunner
from packages.harness.chat.service import ChatService
from packages.harness.config import search_registry
from packages.harness.config.loader import load_all_config, setup_logging_from_settings
from packages.harness.config.models import Settings
from packages.harness.config.models import settings as global_settings
from packages.harness.config.search_registry import load_search_config
from packages.harness.core.background_tasks import background_task_manager
from packages.harness.core.errors import BusinessException, ErrorCode
from packages.harness.core.thread_pool import shutdown_shared_thread_pool
from packages.harness.database.engine import close_database, init_database
from packages.harness.database.session import session_scope
from packages.harness.duty.duty_models import DutyRunConfigModel
from packages.harness.duty.duty_service import DutyService
from packages.harness.intent.service import IntentShortcutService
from packages.harness.llm.gateway import LlmGateway, wire_default_usage_recorder
from packages.harness.memory.dreaming import DreamService
from packages.harness.memory.episode_sink import ChatTurnEpisodeSink
from packages.harness.memory.retrieval import (
    MemoryIndexer,
    MemorySearchService,
    build_memory_store,
)
from packages.harness.memory.service import (
    ChatSessionService,
    CuratedAccessReinforcer,
    MemoryService,
)
from packages.harness.multimodal.look_at import LookAtService, register_look_at_tool
from packages.harness.multimodal.sticker_bridge import StickerBridge
from packages.harness.multimodal.sticker_service import StickerService
from packages.harness.persona.core_memory import (
    CoreMemoryService,
    core_memory_session_id_var,
    register_core_memory_tools,
)
from packages.harness.persona.directives import DirectivesService
from packages.harness.persona.directives_models import DirectiveCreateDTO
from packages.harness.persona.facts import FactsService
from packages.harness.persona.facts_models import FactQueryDTO, PersonaFactStatus
from packages.harness.persona.relationship import build_relationship_summary
from packages.harness.persona.relationship_models import (
    RELATIONSHIP_EVENT_SCAN_LIMIT,
    RELATIONSHIP_FACT_CATEGORY,
    RelationshipSummaryModel,
)
from packages.harness.persona.service import PersonaService, bind_persona_context
from packages.harness.persona.skills import SkillsService
from packages.harness.pools.manager import get_pool_manager, init_pool_manager
from packages.harness.reminders.job import (
    REMINDER_SCAN_INTERVAL_SECONDS,
    build_reminder_scan_job,
)
from packages.harness.reminders.service import ReminderService
from packages.harness.rhythm.core import resolve_effective
from packages.harness.rhythm.service import RhythmService
from packages.harness.scheduler import Scheduler, build_dream_trigger_job
from packages.harness.search.search_service import SearchService
from packages.harness.search.tool import build_web_search_tool, register_web_search_tool
from packages.harness.tools.browser import (
    build_browser_sidecar_client,
    build_browser_specs,
    register_browser_sidecar_tools,
)
from packages.harness.tools.browser_models import BROWSER_TOOL_NAMES
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
from packages.harness.usage.service import UsageService

# ORM 模型注册（导入副作用——见模块 docstring「ORM 注册」一节）。
_ORM_REGISTRY = (persona_orm, usage_orm, sticker_orm, voiceprint_orm, canvas_orm)

#: CORS 显式放行方法面（管理面全操作集）。
_CORS_ALLOW_METHODS: list[str] = ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]
#: CORS 显式放行请求头面（JSON API 最小集 + 鉴权）。
_CORS_ALLOW_HEADERS: list[str] = ["Accept", "Authorization", "Content-Type"]
#: CORS 暴露给脚本的响应头面（画布导出需要读 Content-Disposition 取文件名）。
_CORS_EXPOSE_HEADERS: list[str] = ["Content-Disposition"]
#: 通道 runner 优雅停机等待秒数（在途长轮询最多一个窗口——超时 cancel 兜底）。
_CHANNELS_STOP_TIMEOUT_SECONDS: float = 5.0
#: 调度器优雅停机等待秒数（tick 收尾 + 在途作业排水——超时 cancel 兜底）。
_SCHEDULER_STOP_TIMEOUT_SECONDS: float = 10.0
#: 全局后台任务 drain 等待秒数（fire-and-forget 索引等在途任务的宽限期
#: ——先于池关闭执行，超时 cancel 兜底）。
_BACKGROUND_DRAIN_TIMEOUT_SECONDS: float = 10.0

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
        app: 待装配的 FastAPI 应用（gateway / memory_store 上挂到
            ``app.state``）。
        settings: Settings（生产传全局单例；测试传隔离实例）。
        config_dir: 结构化配置目录（None = CWD 发现；测试注入 tmp_path）。

    Raises:
        BusinessException: 配置解析失败（``CONFIG_INVALID``——fail loud，
            半生效配置不可审计）；通道启用但 bot token 缺失
            （``CHANNEL_TOKEN_MISSING``——fail loud，装配期拒绝启动）。
    """
    setup_logging_from_settings(settings)
    load_all_config(config_dir=config_dir)
    # 管理面鉴权模式通告（ONE info——日志先行步骤之后、组件装配之前；
    # 鉴权依赖本体在 create_app 静态装配期挂接，此处只落启动日志）。
    log_admin_auth_startup_notice(settings)
    await init_database(settings.database)
    manager = init_pool_manager(settings)
    await manager.start()
    app.state.gateway = LlmGateway(
        settings=settings,
        usage_recorder=wire_default_usage_recorder(),
        top_logprobs=settings.llm.top_logprobs,
        prompt_logprobs=settings.llm.prompt_logprobs,
    )
    app.state.memory_store = build_memory_store(settings)
    _assemble_memory_services(app)
    await _startup_search(app, settings, config_dir=config_dir)
    await _startup_browser_sidecar(app, settings)
    await _startup_channels(app, settings)
    await _startup_scheduler(app, settings)


def _assemble_memory_services(app: FastAPI) -> None:
    """装配记忆管道（索引管道 + 情景 sink + 检索 / 梦境服务——单一长寿实例）。

    装配链（记忆管道闭合——两条既有阶段性边界在此收口）：

        1. ``MemoryIndexer``（embedder = 装配网关的 embed 角色绑定）绑定
           全局后台任务管理器——索引链 fire-and-forget，停机经管理器 drain；
        2. ``MemoryService``（indexer 绑定）→ ``app.state.memory_service``
           ——情景写入提交后触发索引、删除 / 归档触发索引移除；
        3. ``ChatTurnEpisodeSink`` → ``app.state.episode_sink``——通道
           ChatService 工厂消费（对话回合落库即写 kind=chat 情景事件）；
        4. ``MemorySearchService``（写读强化面注入）→
           ``app.state.memory_search_service``——检索命中策展行的访问
           计数经后台管理器异步推进；
        5. ``DreamService``（记忆服务绑定）→ ``app.state.dream_service``
           ——夜间巩固的归档阶段同步移除索引。

    装配前置：``app.state.memory_store`` / ``app.state.gateway`` 已就绪
    （本函数在存储 / 网关装配之后、通道 / 调度器装配之前调用——后者
    消费上述槽位）。

    Args:
        app: 待装配的 FastAPI 应用（服务槽位上挂 ``app.state``）。
    """
    store = app.state.memory_store
    gateway = app.state.gateway
    indexer = MemoryIndexer(
        embedder=gateway.embed_by_role,
        store=store,
        background_manager=background_task_manager,
    )
    memory_service = MemoryService(indexer=indexer)
    app.state.memory_service = memory_service
    app.state.memory_indexer = indexer
    app.state.episode_sink = ChatTurnEpisodeSink(
        session_factory=session_scope,
        memory_service=memory_service,
        background_manager=background_task_manager,
    )
    app.state.memory_search_service = MemorySearchService(
        store=store,
        embedder=gateway.embed_by_role,
        reinforcer=CuratedAccessReinforcer(
            session_factory=session_scope, memory_service=memory_service
        ),
        background_manager=background_task_manager,
    )
    app.state.dream_service = DreamService(memory_service=memory_service)
    logger.info("记忆管道已装配（索引 / 情景 sink / 检索强化 / 梦境服务）")


async def shutdown_app_state(app: FastAPI) -> None:
    """停机全部进程级组件（启动严格逆序；幂等——组件未启动时 no-op）。

    停机链：调度器 → 通道 → 全局后台任务 drain（``background_task_manager.
    shutdown``——fire-and-forget 任务在池关闭前排空，``is_shutting_down``
    标志同步置位）→ 公共池 → 数据库 → 线程池幂等兜底。

    Args:
        app: 待收尾的 FastAPI 应用（``app.state.gateway`` /
            ``app.state.memory_store`` 置 None）。
    """
    await _shutdown_scheduler(app)
    await _shutdown_channels(app)
    # 后台任务 drain 先于池关闭——在途 fire-and-forget 任务（索引链等）
    # 消费 SeekDB 池 / 共享 client，池先关会让其必然失败
    await background_task_manager.shutdown(timeout=_BACKGROUND_DRAIN_TIMEOUT_SECONDS)
    manager = get_pool_manager()
    if manager is not None and manager.started:
        await manager.stop()
    await close_database()
    # 共享线程池幂等兜底（pool stop 已关闭；防御独立初始化路径）。
    shutdown_shared_thread_pool(wait=True)
    app.state.gateway = None
    app.state.memory_store = None
    app.state.memory_service = None
    app.state.memory_indexer = None
    app.state.episode_sink = None
    app.state.memory_search_service = None
    app.state.dream_service = None
    # 搜索槽位复位（服务无自有资源——共享池 client 生命周期归 pools）。
    app.state.search_service = None
    app.state.search_tool_registry = None
    app.state.search_tool_specs = None
    # 浏览器 sidecar 槽位复位（客户端无自有资源——共享池 client 生命周期
    # 归 pools；chat 注册表内的代理执行器随槽位一同丢弃）。
    app.state.browser_sidecar_client = None
    app.state.browser_tool_registry = None
    app.state.browser_tool_specs = None


async def _startup_search(
    app: FastAPI, settings: Settings, *, config_dir: Path | None = None
) -> None:
    """装配联网搜索能力（SEARCH_ENABLED 时：段加载 → 启动校验 → 服务 + 工具面）。

    装配链（顺序锁定，文档化于 docs/backend/search.md）：

        1. ``load_search_config``（config.yaml ``search`` 段——管理面 /
           服务运行时真源；**先于通道 / 调度器**——通道的 ChatService
           工厂与 duty 注册表都消费本步产物）；
        2. ``enabled=false`` → no-op（零搜索对象构造——管理面 CRUD 仍
           可用，操作的是运行时默认态）；
        3. 启动校验：``enabled=true`` 且无 provider 条目 → fail fast
           （清晰中文错误——``CONFIG_INVALID``）；
        4. SearchService 装配（共享 HTTP client + UsageService 上挂
           ``app.state.search_service``）；
        5. ``tool_enabled=true`` 时构建 chat 工具注册表（仅 web_search）
           + 规格列表上挂（``app.state.search_tool_registry`` /
           ``app.state.search_tool_specs``——chat 通道工厂与 duty 注册表
           各自消费）；两级开关：``enabled`` 能力总闸 + ``tool_enabled``
           工具面曝光闸。

    Args:
        app: 待装配的 FastAPI 应用（search 槽位上挂 ``app.state``）。
        settings: 生效 Settings（搜索运营 env 面——``settings.search``
            与 yaml ``web_search`` 子块分层合并，yaml 优先）。
        config_dir: 结构化配置目录（与 ``load_all_config`` 同参——保证
            搜索段与 llm 段读同一份 config.yaml）。

    Raises:
        BusinessException: 能力启用但无 provider 条目（``CONFIG_INVALID``
            ——fail fast，清晰中文错误）；公共池未启动
            （``INTERNAL_ERROR``——装配顺序契约破坏面）。
    """
    app.state.search_service = None
    app.state.search_tool_registry = None
    app.state.search_tool_specs = None
    load_search_config(config_dir=config_dir)
    config = search_registry.runtime_search_config
    if not config.web_search.enabled:
        logger.info("联网搜索未启用（SEARCH_ENABLED / search.web_search.enabled 关闭）——跳过装配")
        return
    if not config.providers:
        raise BusinessException(
            code=ErrorCode.CONFIG_INVALID,
            message=(
                "联网搜索已启用但 config.yaml 的 search 段无 provider 条目——"
                "启用前须至少配置一个搜索 provider（Bocha / Zhipu，见 docs/backend/search.md）"
            ),
        )
    manager = get_pool_manager()
    if manager is None or not manager.started:
        raise BusinessException(
            code=ErrorCode.INTERNAL_ERROR,
            message="搜索装配前置失败：公共池未启动（lifespan 装配顺序破坏）",
        )
    service = SearchService(
        http_client=manager.get_http_client(),
        usage_face=UsageService(),
    )
    app.state.search_service = service
    logger.info(
        "联网搜索服务已装配: providers={}, tool_enabled={}",
        [entry.name for entry in config.providers],
        config.web_search.tool_enabled,
    )
    if not config.web_search.tool_enabled:
        logger.info(
            "web_search 工具面未启用（SEARCH_TOOL_ENABLED / search.web_search.tool_enabled 关闭）"
        )
        return
    chat_registry = ToolRegistry()
    register_web_search_tool(chat_registry, service)
    web_spec, _ = build_web_search_tool(service)
    app.state.search_tool_registry = chat_registry
    app.state.search_tool_specs = [web_spec]
    logger.info(
        "web_search 工具已注册（duty 注册表在调度器装配时追加；chat 工具循环经通道工厂消费）"
    )


async def _startup_browser_sidecar(app: FastAPI, settings: Settings) -> None:
    """装配浏览器 sidecar 工具面（enabled + token 就绪 + /health 探测成功时
    构建客户端 + chat 工具注册）。

    可选组件 warn-and-continue 语义（与工具沙盒同降级语义——锁定决策）：
    token 未配置 / 探测失败 → 跳过浏览器工具注册并告警，工具面回落到
    既有工具集（内置 / 沙盒 / look_at / web_search）。探测经共享 httpx 池
    （bounded 超时 = ``request_timeout_seconds``）。

    chat 注册表接线（镜像 web_search 的双注册形态——文档化决策）：
    搜索装配已建 chat 注册表（``search_tool_registry``）时浏览器八工具
    **并入该注册表**（规格追加到 ``search_tool_specs``——单一 chat 工具
    注册表面）；否则独立建 browser 槽位（``browser_tool_registry`` /
    ``browser_tool_specs``——通道工厂按「搜索槽位优先、浏览器槽位兜底」
    消费）。duty 注册表在调度器装配时经 ``_register_browser_sidecar_tools``
    追加（复用本函数构建的客户端——零二次探测）。

    Args:
        app: 待装配的 FastAPI 应用（client / chat 注册表槽位上挂
            ``app.state``）。
        settings: 生效 Settings（``settings.tools_browser_sidecar`` 分节）。

    Raises:
        BusinessException: 公共池未启动（``INTERNAL_ERROR``——调用顺序
            契约破坏面；与搜索 / 通道装配同前置检查）。
    """
    app.state.browser_sidecar_client = None
    app.state.browser_tool_registry = None
    app.state.browser_tool_specs = None
    sidecar = settings.tools_browser_sidecar
    if not sidecar.enabled:
        logger.info("浏览器 sidecar 未启用（TOOLS_BROWSER_SIDECAR_ENABLED=false）——跳过装配")
        return
    manager = get_pool_manager()
    if manager is None or not manager.started:
        raise BusinessException(
            code=ErrorCode.INTERNAL_ERROR,
            message="浏览器 sidecar 装配前置失败：公共池未启动（lifespan 装配顺序破坏）",
        )
    client = build_browser_sidecar_client(manager.get_http_client(), sidecar)
    if not client.has_token:
        logger.warning(
            "浏览器 sidecar token 未配置（环境变量 {} 为空）——跳过浏览器工具注册，"
            "工具面回落到既有工具集",
            sidecar.token_env,
        )
        return
    if not await client.probe_health():
        logger.warning(
            "浏览器 sidecar 健康探测失败（{}，超时 {}s 上限）——跳过浏览器工具注册，"
            "工具面回落到既有工具集",
            sidecar.base_url,
            sidecar.request_timeout_seconds,
        )
        return
    app.state.browser_sidecar_client = client
    browser_specs = list(build_browser_specs())
    chat_registry = getattr(app.state, "search_tool_registry", None)
    if chat_registry is not None:
        register_browser_sidecar_tools(chat_registry, client)
        search_specs = getattr(app.state, "search_tool_specs", None) or []
        app.state.search_tool_specs = [*search_specs, *browser_specs]
        logger.info(
            "浏览器 sidecar 工具已并入搜索 chat 注册表: tools=[{}]（duty 注册表在调度器装配时追加）",
            ", ".join(BROWSER_TOOL_NAMES),
        )
        return
    browser_registry = ToolRegistry()
    register_browser_sidecar_tools(browser_registry, client)
    app.state.browser_tool_registry = browser_registry
    app.state.browser_tool_specs = browser_specs
    logger.info(
        "浏览器 sidecar 工具已注册（独立 chat 注册表）: tools=[{}]（duty 注册表在调度器装配时追加）",
        ", ".join(BROWSER_TOOL_NAMES),
    )


async def _startup_channels(app: FastAPI, settings: Settings) -> None:
    """装配通道层（Telegram 长轮询 runner / QQ 适配器 / 企微长连接 / 抖音 web runner）。

    装配链（单一 ChannelRouter 服务全部通道——会话按 ``channel:chat_id``
    维度天然隔离）：任一通道启用时先装配 rhythm / intent 会话性组件
    （共享随机源——见下方），再构造 ChannelRouter（逐消息 ChatService
    工厂闭包 ``app.state.gateway`` + ``session_scope`` 持久化 + 核心记忆
    读写面 + 快径面注入）；telegram 启用时再装配 TelegramClient +
    TelegramChannelRunner → create_task + ``background_task_manager``
    托管；QQ 启用时装配 NapCatOneBotClient + NapCatQQAdapter →
    ``router.register_adapter`` + 启动校验 get_login_info；企微启用时装配长连接
    runner；抖音 web 启用时装配轮询 runner（消费浏览器 sidecar 底座——底座
    未就绪即跳过该通道，见 ``_startup_douyin_channel``）。全部关闭时
    no-op（零通道对象构造——rhythm / intent 槽位同样 None：二者的
    R2a 消费面是通道 ChatService 工厂，通道关闭即无消费面）。

    rhythm / intent 装配（R2a-6，通道专属——文档化决策）：

    - 单一随机源：``random.Random()`` 实例由 RhythmService 与
      IntentShortcutService **共享**（节律延迟采样与快径拟真延迟同源
      ——可测性与随机面收敛）；
    - ``app.state.rhythm_service``：门面直通或启用随
      ``RHYTHM_ENABLED``（R2a 回复链路不消费门——R2d 心流 / R2e GUI
      消费面；作息值面恒解析供 intent 问候选带）；
    - ``app.state.intent_shortcut``：``INTENT_ENABLED``（默认 true——
      纯本地零成本）关闭时为 None（工厂零注入，行为与 R2a 前一致）；
      per-persona 节律覆写解析属 R2d（R2a 用全局面）；
    - **duty 不注入快径面**：duty 是无头计划任务（章程驱动的巡检 /
      报告生成），非对话性消费——意图快径是会话性能（「现在几点」
      这类用户高频问法只出现在对话里），duty 工具面 / 章程零变化。

    **QQ 与 telegram 的失败语义差异（锁定决策）**：telegram token 缺失
    fail loud（``CHANNEL_TOKEN_MISSING``——app 拒绝启动）；QQ 启动校验
    失败（NapCat 不可达 / token 无效）**warn-and-continue**——适配器仍
    注册（入站 webhook 照常确认），出站发送失败逐消息经路由器兜底隔离。
    理由：QQ 入站是推送式（NapCat 侧配置上报目标——后端启动时 NapCat
    尚未就绪是常见部署时序），且 QQ 通道无长驻任务可终止；fail fast
    只会因部署顺序杀掉整个 app。

    Args:
        app: 待装配的 FastAPI 应用（router / runner / task / rhythm /
            intent 槽位上挂 ``app.state``——router 供后续 duty 等出站
            投递面复用 + QQ webhook 端点入站消费）。
        settings: 生效 Settings（``settings.channels.telegram`` /
            ``settings.channels.qq`` / ``settings.rhythm`` /
            ``settings.intent`` 分节）。

    Raises:
        BusinessException: telegram token 环境变量缺失 / 为空
            （``CHANNEL_TOKEN_MISSING``——fail loud）；公共池未启动
            （``INTERNAL_ERROR``——调用顺序契约破坏面）。
    """
    app.state.channels_runner = None
    app.state.channels_task = None
    app.state.channels_wecom_runner = None
    app.state.channels_wecom_task = None
    app.state.channels_douyin_runner = None
    app.state.channels_douyin_task = None
    app.state.channels_router = None
    app.state.rhythm_service = None
    app.state.intent_shortcut = None
    app.state.sticker_service = None
    telegram = settings.channels.telegram
    qq = settings.channels.qq
    wecom = settings.channels.wecom
    douyin = settings.channels.douyin
    if not telegram.enabled and not qq.enabled and not wecom.enabled and not douyin.enabled:
        logger.info(
            "通道层未启用（TELEGRAM_ENABLED / QQ_ENABLED / WECOM_ENABLED / "
            "DOUYIN_ENABLED 均关闭）——跳过装配"
        )
        return
    manager = get_pool_manager()
    if manager is None or not manager.started:
        raise BusinessException(
            code=ErrorCode.INTERNAL_ERROR,
            message="通道装配前置失败：公共池未启动（lifespan 装配顺序破坏）",
        )
    gateway = app.state.gateway
    # rhythm / intent 会话性组件（单一随机源共享——见函数 docstring）。
    rhythm_rng = random.Random()
    effective_rhythm = resolve_effective(settings.rhythm, None)
    app.state.rhythm_service = RhythmService(
        effective=effective_rhythm,
        enabled=settings.rhythm.enabled,
        rng=rhythm_rng,
    )
    directives_service = DirectivesService()
    facts_service = FactsService()
    skills_service = SkillsService()

    async def _capture_directive(text: str) -> bool:
        """对话捕获长期指令（DIRECTIVE 快径副作用面——恒不抛异常）。

        人格上下文由通道入站路径绑定（``ChannelRouter.handle_inbound`` 的
        ``bind_persona_context``）——本闭包在同一任务内开短事务落库；
        失败归一 False（快径退化为附和文案，回复链路不受影响）。
        """
        try:
            async with session_scope() as db_session:
                await directives_service.create(db_session, DirectiveCreateDTO(content=text))
        except Exception:  # 副作用失败不阻断回复链路（退化为附和文案）
            logger.opt(exception=True).warning("对话捕获长期指令失败（退化为附和文案）")
            return False
        logger.info("对话捕获长期指令已落库: text={}", text)
        return True

    intent_shortcut: IntentShortcutService | None = None
    if settings.intent.enabled:
        intent_shortcut = IntentShortcutService(
            rng=rhythm_rng, effective=effective_rhythm, directive_sink=_capture_directive
        )
        logger.info("意图快径已装配（通道路径 ChatService 前钩；INTENT_ENABLED=true）")
    else:
        logger.info("意图快径未启用（INTENT_ENABLED=false）——通道回复全量走 LLM")
    app.state.intent_shortcut = intent_shortcut

    episode_sink = getattr(app.state, "episode_sink", None)
    core_memory_service = CoreMemoryService()

    def _core_memory_session_id() -> int | None:
        """当前对话会话主键（工具执行期由 ChatService 绑定——无会话为 None）。"""
        return core_memory_session_id_var.get()

    # 聊天工具注册表（单一注册表面）：搜索槽位优先、浏览器槽位兜底、皆无
    # 则独立建表；核心记忆自编辑工具恒并入——通道 ChatService 逐轮消费，
    # 工具列表在 ChatService 内按发送者来源分级裁剪（peer 回合看不到它）。
    chat_tool_specs = list(getattr(app.state, "search_tool_specs", None) or [])
    chat_tool_registry = getattr(app.state, "search_tool_registry", None)
    if chat_tool_registry is None:
        chat_tool_specs = list(getattr(app.state, "browser_tool_specs", None) or [])
        chat_tool_registry = getattr(app.state, "browser_tool_registry", None)
    if chat_tool_registry is None:
        chat_tool_specs = []
        chat_tool_registry = ToolRegistry()
    chat_tool_specs.append(
        register_core_memory_tools(
            chat_tool_registry, core_memory_service, _core_memory_session_id
        )
    )
    logger.info(
        "通道路径聊天工具面已装配: tools=[{}]（核心记忆自编辑工具仅聊天注册表——duty 不自编辑）",
        ", ".join(spec.name for spec in chat_tool_specs),
    )

    def _chat_service_factory() -> ChatService:
        """逐消息 ChatService 工厂（聊天工具面 + 核心记忆读写面 + 快径面
        ——通道专属消费面；工具列表按发送者来源分级由 ChatService 施加）。"""
        return ChatService(
            gateway=gateway,
            session_service=ChatSessionService(),
            tools=chat_tool_specs,
            tool_executor=chat_tool_registry,
            intent_shortcut=intent_shortcut,
            directive_service=directives_service,
            facts_reader=facts_service,
            core_memory_reader=core_memory_service,
            skills_reader=skills_service,
            episode_sink=episode_sink,
        )

    persona_service = PersonaService()

    async def _relationship_lookup(
        persona_id: int, now_ms: int
    ) -> RelationshipSummaryModel | None:
        """心流门控关系摘要查询（关系类现行事实派生——失败返回 None）。

        门控启用时才被调用：按人格上下文开短事务读取关系类现行事实，
        经 ``build_relationship_summary`` 纯函数聚合。读取失败按「无关系
        数据」处理（门控照常判定，不阻断消息）。
        """
        try:
            async with session_scope() as db_session, bind_persona_context(persona_id):
                page = await facts_service.list_facts(
                    db_session,
                    FactQueryDTO(
                        category=RELATIONSHIP_FACT_CATEGORY,
                        status=PersonaFactStatus.ACTIVE,
                        limit=RELATIONSHIP_EVENT_SCAN_LIMIT,
                    ),
                )
            return build_relationship_summary(page.items, now_ms=now_ms)
        except Exception:  # 门控输入失败不阻断消息——按无关系数据处理
            logger.opt(exception=True).warning(
                "心流门控关系摘要查询失败（按无关系数据处理）: persona_id={}", persona_id
            )
            return None

    sticker_service: StickerService | None = None
    sticker_bridge: StickerBridge | None = None
    if settings.sticker.enabled:
        sticker_service = StickerService(settings=settings.sticker, gateway=gateway)
        sticker_bridge = StickerBridge(
            sticker_service=sticker_service, session_factory=session_scope
        )
        logger.info(
            "表情包库通道桥已装配（入站收录 sticker 附件；回表情={}）",
            settings.sticker.channel_reply_enabled,
        )
    app.state.sticker_service = sticker_service

    router = ChannelRouter(
        chat_service_factory=_chat_service_factory,
        session_factory=session_scope,
        flow_settings=settings.flow,
        group_settings=settings.group_persona,
        rhythm_service=app.state.rhythm_service,
        scheduler_provider=lambda: getattr(app.state, "scheduler", None),
        persona_service=persona_service,
        relationship_lookup=_relationship_lookup,
        sticker_ingest_hook=sticker_bridge.ingest_inbound if sticker_bridge else None,
        sticker_reply_hook=sticker_bridge.pick_reply if sticker_bridge else None,
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
    if wecom.enabled:
        await _startup_wecom_channel(app, settings, router, manager.get_http_client())
    if douyin.enabled:
        await _startup_douyin_channel(app, settings, router)
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


async def _startup_wecom_channel(
    app: FastAPI,
    settings: Settings,
    router: ChannelRouter,
    http_client: httpx.AsyncClient,
) -> None:
    """装配企业微信智能机器人长连接通道（配置 / 凭据面失败即跳过该通道）。

    装配链：`WeComLongConnectionClient`（BotID + Secret 环境变量间接解析；
    构造期缺失即 ``CHANNEL_TOKEN_MISSING``）→ `WeComChannelRunner`（入站队列 +
    msgid 去重 + 回复上下文登记 + 出站文本 / 附件投递）→
    ``router.register_adapter(WECOM, runner)`` → ``create_task(runner.run())`` +
    ``background_task_manager`` 托管。

    **失败语义（与 telegram 的差异，锁定决策）**：telegram token 缺失 fail loud
    （app 拒绝启动）；企微装配期失败（BotID / 密钥缺失等配置面错误）记 ERROR
    并**跳过该通道**——长连接是新增通道，其配置瑕疵不应连带杀掉已在工作的
    telegram / QQ 通道。运行期故障（断开 / 订阅被拒 / 命令失败）由 runner 的
    退避重连与逐消息错误隔离消化，同样不波及其余通道。

    Args:
        app: 待装配的 FastAPI 应用（``channels_wecom_runner`` /
            ``channels_wecom_task`` 槽位上挂）。
        settings: 生效 Settings（读取 ``settings.channels.wecom`` 分节）。
        router: 通道路由器（注册企微适配器发送面）。
        http_client: 共享 HTTP 客户端（出站附件 ``url`` 源下载——不私建连接）。
    """
    wecom = settings.channels.wecom
    try:
        client = WeComLongConnectionClient(
            bot_id=wecom.bot_id,
            secret_env=wecom.secret_env,
            endpoint=wecom.ws_endpoint,
            request_timeout_seconds=wecom.request_timeout_seconds,
            heartbeat_interval_seconds=wecom.heartbeat_interval_seconds,
            heartbeat_timeout_seconds=wecom.heartbeat_timeout_seconds,
            open_timeout_seconds=wecom.open_timeout_seconds,
            backoff_base_seconds=wecom.backoff_base_seconds,
            backoff_max_seconds=wecom.backoff_max_seconds,
            backoff_jitter_ratio=wecom.backoff_jitter_ratio,
        )
    except BusinessException as exc:
        logger.error(
            "企微长连接通道未装配（配置 / 凭据面失败——其余通道继续工作）: {}", exc.message
        )
        return
    runner = WeComChannelRunner(
        client=client,
        router=router,
        reply_window_seconds=wecom.reply_window_seconds,
        http_client=http_client,
    )
    router.register_adapter(ChannelType.WECOM, runner)
    task = asyncio.create_task(runner.run(), name="wecom-channel-runner")
    background_task_manager.register(task)
    task.add_done_callback(_log_channel_task_termination)
    app.state.channels_wecom_runner = runner
    app.state.channels_wecom_task = task
    logger.info(
        "企微长连接通道 runner 已启动（managed background task）: endpoint={}, bot_id={}",
        wecom.ws_endpoint,
        wecom.bot_id,
    )


async def _startup_douyin_channel(
    app: FastAPI,
    settings: Settings,
    router: ChannelRouter,
) -> None:
    """装配抖音 web 通道（复用浏览器 sidecar 底座——底座未就绪即跳过该通道）。

    装配链：``DouyinWebClient``（注入 ``app.state.browser_sidecar_client``——
    共享池 + Bearer + 持久化 profile 的已登录页面）→ ``DouyinChannelRunner``
    （轮询采集 / 去重 / 归一 / 出站投递）→ ``router.register_adapter(DOUYIN,
    runner)`` → ``create_task(runner.run())`` + ``background_task_manager``
    托管。

    **失败语义（与企微同范式）**：底座未就绪（``TOOLS_BROWSER_SIDECAR_ENABLED``
    关闭 / token 缺失 / 健康探测失败）时记 ERROR 并**跳过该通道**——抖音 web
    是依赖浏览器底座的新增通道，其底座瑕疵不应连带杀掉已在工作的其他通道。
    运行期轮询失败由 runner 的指数退避消化。

    Args:
        app: 待装配的 FastAPI 应用（``channels_douyin_runner`` /
            ``channels_douyin_task`` 槽位上挂）。
        settings: 生效 Settings（读取 ``settings.channels.douyin`` 分节）。
        router: 通道路由器（注册抖音适配器发送面）。
    """
    douyin = settings.channels.douyin
    sidecar_client = getattr(app.state, "browser_sidecar_client", None)
    if sidecar_client is None:
        logger.error(
            "抖音 web 通道未装配（浏览器 sidecar 底座未就绪——请检查 "
            "TOOLS_BROWSER_SIDECAR_ENABLED / 令牌 / 健康探测；其余通道继续工作）"
        )
        return
    try:
        client = DouyinWebClient(
            invoker=sidecar_client,
            chat_url=douyin.chat_url,
        )
        runner = DouyinChannelRunner(
            client=client,
            router=router,
            conversation_id=douyin.conversation_id,
            poll_interval_seconds=douyin.poll_interval_seconds,
            backoff_base_seconds=douyin.backoff_base_seconds,
            backoff_max_seconds=douyin.backoff_max_seconds,
            max_messages_per_poll=douyin.max_messages_per_poll,
        )
    except ValueError as exc:
        logger.error(
            "抖音 web 通道未装配（配置不变式失败——其余通道继续工作）: {}", exc
        )
        return
    router.register_adapter(ChannelType.DOUYIN, runner)
    task = asyncio.create_task(runner.run(), name="douyin-channel-runner")
    background_task_manager.register(task)
    task.add_done_callback(_log_channel_task_termination)
    app.state.channels_douyin_runner = runner
    app.state.channels_douyin_task = task
    logger.info(
        "抖音 web 通道 runner 已启动（managed background task）: chat_url={!r}, "
        "conversation_id={!r}, poll_interval_s={}",
        douyin.chat_url,
        douyin.conversation_id,
        douyin.poll_interval_seconds,
    )


async def _shutdown_channels(app: FastAPI) -> None:
    """停机通道层（先于公共池——通道组件消费共享 HTTP client；幂等）。

    停机链（telegram runner）：``request_stop``（优雅）→ bounded 等待任务
    收尾 → 超时 ``cancel`` 兜底（在途长轮询被取消——offset 已逐批持久化，
    重启安全）。QQ 通道无长驻任务（推送式入站 + 适配器注册态）——仅随
    ``channels_router`` 槽位复位（共享池 client 生命周期归 pools）。
    rhythm / intent 会话性组件无自有资源（随机源随实例丢弃）——槽位
    随通道层一同复位。

    Args:
        app: 待收尾的 FastAPI 应用（``channels_router`` / ``channels_runner``
            / ``channels_task`` / ``channels_wecom_runner`` /
            ``channels_wecom_task`` / ``channels_douyin_runner`` /
            ``channels_douyin_task`` / ``rhythm_service`` / ``intent_shortcut``
            槽位复位 None）。
    """
    runner: TelegramChannelRunner | None = getattr(app.state, "channels_runner", None)
    task: asyncio.Task[None] | None = getattr(app.state, "channels_task", None)
    wecom_runner: WeComChannelRunner | None = getattr(
        app.state, "channels_wecom_runner", None
    )
    wecom_task: asyncio.Task[None] | None = getattr(app.state, "channels_wecom_task", None)
    douyin_runner: DouyinChannelRunner | None = getattr(
        app.state, "channels_douyin_runner", None
    )
    douyin_task: asyncio.Task[None] | None = getattr(
        app.state, "channels_douyin_task", None
    )
    app.state.channels_runner = None
    app.state.channels_task = None
    app.state.channels_wecom_runner = None
    app.state.channels_wecom_task = None
    app.state.channels_douyin_runner = None
    app.state.channels_douyin_task = None
    app.state.channels_router = None
    # rhythm / intent 槽位复位（服务无自有资源——随机源随实例丢弃）。
    app.state.rhythm_service = None
    app.state.intent_shortcut = None
    app.state.sticker_service = None
    await _stop_managed_channel_task(runner, task, label="Telegram 通道")
    await _stop_managed_channel_task(wecom_runner, wecom_task, label="企微长连接通道")
    await _stop_managed_channel_task(douyin_runner, douyin_task, label="抖音 web 通道")


async def _stop_managed_channel_task(
    runner: Any, task: asyncio.Task[None] | None, *, label: str
) -> None:
    """停机单个通道的托管后台任务（优雅请求 → bounded 等待 → cancel 兜底）。

    Args:
        runner: 通道 runner（提供 ``request_stop``；None = 无 runner 可请求）。
        task: 该通道的后台任务（None / 已完成 = 无需等待）。
        label: 日志面通道名。
    """
    if task is None or task.done():
        return
    if runner is not None:
        runner.request_stop()
    try:
        await asyncio.wait_for(task, timeout=_CHANNELS_STOP_TIMEOUT_SECONDS)
    except TimeoutError:
        # wait_for 超时路径已 cancel 任务并等待取消收尾——此处仅记录
        logger.warning("{} 停机等待超时——已强制取消后台任务", label)
    except Exception as exc:
        logger.warning("{} 任务以异常收尾: {}", label, exc)
    logger.info("{} runner 已停机", label)


def _log_channel_task_termination(task: asyncio.Task[None]) -> None:
    """通道后台任务终态日志（异常终止 / 取消均落日志——不留哑任务）。

    Args:
        task: 已完成（或已取消）的后台任务（任务名标识通道——``telegram-channel-runner``
            / ``wecom-channel-runner``）。
    """
    if task.cancelled():
        logger.info("{} 已被取消（停机兜底路径）", task.get_name())
        return
    exc = task.exception()
    if exc is not None:
        logger.error("{} 异常终止: {}", task.get_name(), exc)


async def _startup_scheduler(app: FastAPI, settings: Settings) -> None:
    """装配中央调度器（duty / dream / 提醒投递面任一可用时注册作业并启动后台任务）。

    装配链（顺序锁定）：工具注册表（内置工具——get_current_time 恒注册；
    search_memory 经记忆检索服务注入，存储与 dream 作业共用
    ``app.state.memory_store``（startup 装配的同一产出——SeekDB 形态下
    不双建池）；沙盒工具按开关 warn-and-continue 追加；look_at 工具
    经装配网关注册（duty 工具循环的图像理解消费面））→ DutyService
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
    app.state.reminder_service = None
    duty_enabled = settings.duty.enabled
    dream_enabled = settings.dream_schedule.enabled
    telegram = settings.channels.telegram
    reminder_delivery_ready = (
        telegram.enabled
        and telegram.owner_chat_id is not None
        and app.state.channels_router is not None
    )
    flow_delivery_ready = settings.flow.enabled and app.state.channels_router is not None
    if (
        not duty_enabled
        and not dream_enabled
        and not reminder_delivery_ready
        and not flow_delivery_ready
    ):
        logger.info(
            "中央调度器未启用（DUTY_ENABLED / DREAM_SCHEDULE_ENABLED / FLOW_ENABLED 关闭"
            "且提醒投递面不可用）——跳过装配"
        )
        return
    store = app.state.memory_store
    scheduler = Scheduler(tick_seconds=settings.scheduler.tick_seconds)
    if duty_enabled:
        registry = ToolRegistry()
        memory_search_service = getattr(app.state, "memory_search_service", None)
        register_builtin_tools(
            registry,
            search_service=(
                memory_search_service
                if memory_search_service is not None
                else MemorySearchService(
                    store=store,
                    embedder=app.state.gateway.embed_by_role,
                )
            ),
        )
        await _register_sandbox_tools(app, settings, registry)
        # 浏览器 sidecar 工具（JS 渲染回退面——duty 工具循环消费面）：
        # 在内置 + 沙盒工具后注册（复用启动期构建并通过探测的客户端；
        # 未启用 / 探测失败时槽位为 None——零注册，duty 工具面不受影响）。
        await _register_browser_sidecar_tools(app, settings, registry)
        # look_at 工具（图像理解——duty 工具循环消费面）：经装配网关
        # 构建服务（用量随网关发射），在内置 + 沙盒工具后注册。
        register_look_at_tool(registry, LookAtService(gateway=app.state.gateway))
        # web_search 工具（联网搜索——duty 工具循环消费面）：在内置 +
        # 沙盒 + look_at 工具后注册（仅搜索服务装配且工具面启用时；
        # 预算 / failover 在 SearchService 内闭环）。
        search_service = getattr(app.state, "search_service", None)
        if (
            search_service is not None
            and search_registry.runtime_search_config.web_search.tool_enabled
        ):
            register_web_search_tool(registry, search_service)
        duty_service = DutyService(
            gateway=app.state.gateway,
            registry=registry,
            memory_service=getattr(app.state, "memory_service", None) or MemoryService(),
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
            dream_service=getattr(app.state, "dream_service", None) or DreamService(),
            gateway=app.state.gateway,
            persona_service=PersonaService(),
            session_factory=session_scope,
            store=store,
        )
        scheduler.register_daily("dream", dream_job, daily_at=settings.dream_schedule.daily_at)
    if reminder_delivery_ready:
        reminder_service = ReminderService()
        reminder_job = build_reminder_scan_job(
            reminder_service=reminder_service,
            persona_service=PersonaService(),
            channel_router=app.state.channels_router,
            owner_chat_id=telegram.owner_chat_id,
            session_factory=session_scope,
        )
        scheduler.register_interval(
            "reminders", reminder_job, every_seconds=REMINDER_SCAN_INTERVAL_SECONDS
        )
        app.state.reminder_service = reminder_service
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
            "工具沙盒 token 未配置（环境变量 {} 为空）——跳过沙盒工具注册，duty 工具面仅内置工具",
            sandbox.token_env,
        )
        return
    if not await client.probe_health():
        logger.warning(
            "工具沙盒健康探测失败（{}，超时 {}s 上限）——跳过沙盒工具注册，duty 工具面仅内置工具",
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


async def _register_browser_sidecar_tools(
    app: FastAPI, settings: Settings, registry: ToolRegistry
) -> None:
    """装配浏览器 sidecar 工具面到 duty 注册表（复用启动期客户端——镜像
    ``_register_sandbox_tools`` 的 warn-and-continue 语义）。

    客户端由 :func:`_startup_browser_sidecar` 构建并通过探测后上挂
    ``app.state.browser_sidecar_client``；本函数仅在 duty 启用时被调用
    （浏览器工具的 duty 消费面）——槽位为 None（未启用 / token 缺失 /
    探测失败）时零注册（告警已在启动期落日志，此处不重复）。

    Args:
        app: 待装配的 FastAPI 应用（读取 ``browser_sidecar_client`` 槽位）。
        settings: 生效 Settings（``settings.tools_browser_sidecar`` 分节）。
        registry: duty 工具注册表（浏览器八工具追加装配目标）。
    """
    client = getattr(app.state, "browser_sidecar_client", None)
    if client is None:
        if settings.tools_browser_sidecar.enabled:
            logger.info("浏览器 sidecar 客户端未就绪（启动期跳过）——duty 工具面不含浏览器工具")
        return
    register_browser_sidecar_tools(registry, client)


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
    app.state.reminder_service = None
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
        expose_headers=_CORS_EXPOSE_HEADERS,
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
    _include_routers(app)
    app.state.settings = effective_settings
    return app


def _include_routers(app: FastAPI) -> None:
    """注册全部路由（管理面鉴权单点挂接 + 豁免面显式清单）。

    挂载契约（R2 审计放行条件——新端点不得扩张无鉴权面）：

        - **受保护路由集**：personas / memories / usage / voice /
          voiceprints / look_at / stickers / models / search / reminders /
          directives / facts / skills / core-memory / canvas 十五个管理路由统一挂
          ``require_admin_token``
          依赖——经单一 ``APIRouter(dependencies=[...])`` 包装路由**一次
          注册**（依赖定义在 ``app/middleware/admin_auth``；token 经
          ``ServerConfig.admin_token_env`` env 间接，空 = 鉴权关闭）；
        - **豁免路由**（显式清单——每项豁免理由如下，增删须同步
          docs/backend/README.md 认证一节的豁免契约表）：

          1. ``health``——零依赖存活面（health.py docstring 契约：存活
             探针不触 DB / 池 / LLM，同理不得因鉴权配置拒答——liveness
             与鉴权正交）；
          2. ``channels qq webhook``——自鉴权面（NapCat 事件上报经自身
             ``webhook_token_env`` Bearer 校验，见 channels_qq 模块；
             再叠 admin token 会要求 NapCat 侧持有管理凭据——双重鉴权
             破坏上报链路且管理面 token 会暴露给部署机配置）；
          3. ``realtime voice ws``——自鉴权面（浏览器 WebSocket 无法设置
             自定义头，token 经查询参数传递并经 ``verify_admin_token``
             恒时比较；WS 路由无法复用 HTTP 形态的 ``require_admin_token``
             依赖——见 app/api/realtime_voice 模块 docstring）。

    Args:
        app: 待挂路由的 FastAPI 应用。
    """
    admin_api = APIRouter(dependencies=[Depends(require_admin_token)])
    admin_api.include_router(personas_router)
    admin_api.include_router(memories_router)
    admin_api.include_router(usage_router)
    admin_api.include_router(voice_router)
    admin_api.include_router(voiceprints_router)
    admin_api.include_router(look_at_router)
    admin_api.include_router(stickers_router)
    admin_api.include_router(models_router)
    admin_api.include_router(search_router)
    admin_api.include_router(reminders_router)
    admin_api.include_router(directives_router)
    admin_api.include_router(facts_router)
    admin_api.include_router(skills_router)
    admin_api.include_router(core_memory_router)
    admin_api.include_router(canvas_router)
    app.include_router(admin_api)
    # 豁免面（见本函数 docstring 豁免契约——不挂 require_admin_token）。
    app.include_router(qq_channels_router)
    app.include_router(health_router)
    app.include_router(realtime_voice_router)

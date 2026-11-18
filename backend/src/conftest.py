"""Pytest 根 conftest — 环境准备（所有测试目录共享）。

将环境变量设置与 ``sys.path`` 修正放在此根级 conftest，确保
``src/tests/``、``src/tests/integration/`` 以及
``src/packages/harness/**/tests/`` 下的测试在导入任何 ``app`` 或
``packages`` 模块前完成环境准备。

pytest 的 conftest 层级机制：根 conftest 先于子目录 conftest 加载，
因此此文件设置的环境变量在任何业务模块导入时已生效。

关键设计：
    - **测试配置完全独立**：Settings 全量 env 面逐键在此硬覆盖为测试
      专用安全值（硬覆盖而非
      setdefault，开发者 shell 导出的同名变量不得泄入测试），测试不
      依赖 ``.env`` / ``config.yaml`` 文件，可在任意 CWD 下运行。
    - **CWD 隔离（防真实配置泄漏）**：pydantic-settings 按相对路径
      ``.env`` 读取环境文件、config loader 按 CWD 发现 ``config.yaml``。
      本 conftest 在导入任何业务模块之前把 CWD 切换到一次性 scratch
      目录，使配置加载永远找不到真实配置文件（backend/.env、
      backend/config.yaml 均不在视野内）。coverage 数据文件在进程启动
      目录生成，不受 chdir 影响（coverage.py 在构造期固化绝对路径）；
      pytest 缓存按 rootdir 定位，同样不受影响。需要真实文件的测试用
      ``monkeypatch.chdir(tmp_path)`` 自行准备（会话结束时恢复到
      scratch 目录，无害）。
    - ``sys.path`` 确保 ``src/`` 在搜索路径上（与 pyproject.toml 的
      ``pythonpath=["src"]`` 一致，此处为防御性双重保障）；仓库根
      ``scripts/``（配置模板生成器）亦加入——byte-stable 测试按顶层
      模块名导入，与 ``pixi run gen-examples`` 执行口径一致。

用法约束：全部测试 ``async def test_``（asyncio_mode=auto；同步测试
棘轮零容忍，见根 scripts/count_sync_tests.py）。
"""

from __future__ import annotations

import atexit
import os
import shutil
import sys
import tempfile
from pathlib import Path

# ---------------------------------------------------------------------------
# 测试配置独立化：Settings 全量 env 面逐键硬覆盖为测试专用安全值。
# 必须在导入任何业务模块之前完成（pydantic-settings 在模块导入时读取
# 环境变量构造 settings 单例——环境变量优先级高于 .env 文件）。
# 全部使用硬赋值（os.environ[...] = ...）而非 setdefault：
# 开发者 shell 中残留的同名真实配置（如 DATABASE_TYPE=mysql）不得泄漏进测试。
# ---------------------------------------------------------------------------

# --- 服务（SERVER_ 前缀 → ServerConfig） ---
os.environ["SERVER_HOST"] = "127.0.0.1"
os.environ["SERVER_PORT"] = "8000"
os.environ["SERVER_WORKERS"] = "1"
# 管理面 owner token（admin_token_env 默认指向的变量）——硬置空 = 测试
# 默认鉴权关闭（既有路由测试全量不受影响，S3 硬覆盖——开发者 shell
# 导出的真实 ADMIN_AUTH_TOKEN 不得泄入测试；鉴权矩阵测试按需
# monkeypatch 本变量）
os.environ["ADMIN_AUTH_TOKEN"] = ""

# --- 数据库（DATABASE_ 前缀 → DatabaseConfig；MySQL 8 权威 / 测试 sqlite） ---
os.environ["DATABASE_TYPE"] = "sqlite"
os.environ["DATABASE_HOST"] = "localhost"
os.environ["DATABASE_PORT"] = "3306"
os.environ["DATABASE_NAME"] = "test_db"
os.environ["DATABASE_USER"] = "test_user"
os.environ["DATABASE_PASSWORD"] = "test_pass"
os.environ["DATABASE_POOL_SIZE"] = "5"
os.environ["DATABASE_MAX_OVERFLOW"] = "10"
os.environ["DATABASE_POOL_RECYCLE"] = "3600"
os.environ["DATABASE_POOL_PRE_PING"] = "true"

# --- 日志（LOG_ 前缀 → LogConfig；相对路径落在下方 scratch CWD 内，
#     永不触碰真实 logs/） ---
os.environ["LOG_LEVEL"] = "INFO"
os.environ["LOG_FILE"] = "logs/test-app.log"
os.environ["LOG_JSON_FILE"] = "logs/test-app.json.log"
os.environ["LOG_ROTATION"] = "1 day"
os.environ["LOG_RETENTION"] = "30 days"
os.environ["LOG_MODULE_LEVELS"] = ""
os.environ["LOG_COLORIZE_CONSOLE"] = "false"
# 异常 diagnose 开关（LogConfig.diagnose_exceptions env 面）——测试默认
# 关闭（与生产默认一致：traceback 不展示帧局部变量）
os.environ["LOG_DIAGNOSE_EXCEPTIONS"] = "false"

# --- 共享池（POOLS_ 前缀 → PoolsConfig） ---
os.environ["POOLS_THREAD_POOL_SIZE"] = "10"
os.environ["POOLS_HTTP_MAX_CONNECTIONS"] = "100"
os.environ["POOLS_HTTP_MAX_KEEPALIVE_CONNECTIONS"] = "20"
os.environ["POOLS_HTTP_CONNECT_TIMEOUT_SECONDS"] = "5.0"
os.environ["POOLS_HTTP_TIMEOUT_SECONDS"] = "30.0"
os.environ["POOLS_LLM_SEMAPHORE_LIMIT"] = "5"
os.environ["POOLS_LLM_REQUEST_TIMEOUT_SECONDS"] = "120"
os.environ["POOLS_CB_FAIL_MAX"] = "5"
os.environ["POOLS_CB_RESET_TIMEOUT_SECONDS"] = "60"

# --- LLM 默认端点（LLM_ 前缀 → LlmEndpointConfig；测试值不可触达真实端点） ---
os.environ["LLM_BASE_URL"] = "http://127.0.0.1:9/v1"
os.environ["LLM_API_KEY"] = "test-only-llm-api-key"
os.environ["LLM_MODEL"] = "test-model"

# --- SeekDB 检索存储（SEEKDB_ 前缀 → SeekDBSettings；默认关闭 → 测试恒走
#     SQLite 回退存储，零真实连接；S3 硬覆盖模式——开发者 shell 导出的
#     同名真实配置不得泄入测试） ---
os.environ["SEEKDB_ENABLED"] = "false"
os.environ["SEEKDB_HOST"] = "127.0.0.1"
os.environ["SEEKDB_PORT"] = "2881"
os.environ["SEEKDB_USER"] = "test_user"
os.environ["SEEKDB_PASSWORD_ENV"] = "SEEKDB_TEST_PASSWORD"
os.environ["SEEKDB_DATABASE"] = "test_seekdb"
os.environ["SEEKDB_VECTOR_DIM"] = "1024"
os.environ["SEEKDB_POOL_MIN"] = "1"
os.environ["SEEKDB_POOL_MAX"] = "2"
# 密码本值（password_env 指向的变量 + 默认变量名——测试专用值，
# 不可触达真实实例；单测经假池消费，永不真实连接）
os.environ["SEEKDB_PASSWORD"] = "test-only-seekdb-password"
# SEEKDB_TEST_PASSWORD 例外（S4 真引擎套件）：该变量名同时是 SeekDB 真引擎
# 强制套件（src/tests/integration_seekdb/）的显式测试服务 env 面（runner
# 注入：CI service container env / 本地 pixi tests-seekdb 任务 env）。runner
# 已显式提供 SEEKDB_TEST_URL（套件门控变量）时保留注入值（空密码 = 本地
# 容器 root 空密码形态，合法）；未提供时维持下方硬覆盖哨兵值。套件本身
# 只读 SEEKDB_TEST_*（显式配置-only，见其 conftest docstring）——本例外
# 不影响单测：单测恒 SEEKDB_ENABLED=false 零真实连接，且消费该变量的
# 单测均经 monkeypatch 注入自己的值。
if "SEEKDB_TEST_URL" not in os.environ:
    os.environ["SEEKDB_TEST_PASSWORD"] = "test-only-seekdb-password"

# --- Telegram 通道（TELEGRAM_ 前缀 → TelegramSettings；默认关闭 → 测试零
#     真实连接；S3 硬覆盖模式——开发者 shell 导出的同名真实配置不得泄入
#     测试。bot token 经 TELEGRAM_TEST_BOT_TOKEN 间接——测试专用值，
#     不可触达真实 bot；单测经 MockTransport / fake 消费，永不真实连接） ---
os.environ["TELEGRAM_ENABLED"] = "false"
os.environ["TELEGRAM_BOT_TOKEN_ENV"] = "TELEGRAM_TEST_BOT_TOKEN"
os.environ["TELEGRAM_API_BASE"] = "https://api.telegram.org"
os.environ["TELEGRAM_POLL_TIMEOUT_SECONDS"] = "25"
os.environ["TELEGRAM_OWNER_CHAT_ID"] = ""
os.environ["TELEGRAM_BACKOFF_BASE_SECONDS"] = "1.0"
os.environ["TELEGRAM_BACKOFF_MAX_SECONDS"] = "60.0"
os.environ["TELEGRAM_STATE_DIR"] = "data/channels"
# token 本值（bot_token_env 指向的变量 + 默认变量名——测试专用值）
os.environ["TELEGRAM_TEST_BOT_TOKEN"] = "test-only-telegram-bot-token"
os.environ["TELEGRAM_BOT_TOKEN"] = "test-only-telegram-bot-token"

# --- QQ 通道（QQ_ 前缀 → QqChannelSettings；默认关闭 → 测试零真实连接；
#     S3 硬覆盖模式——开发者 shell 导出的同名真实配置不得泄入测试。双
#     token 间接：出站 access token 经 QQ_TEST_ACCESS_TOKEN（NapCat HTTP
#     API 鉴权）；入站 webhook token 经 QQ_TEST_WEBHOOK_TOKEN（上报校验
#     ——默认非空 = 测试默认开启入站鉴权，鉴权矩阵按需 monkeypatch）；
#     单测经 MockTransport / fake 消费，永不真实连接） ---
os.environ["QQ_ENABLED"] = "false"
os.environ["QQ_HTTP_API_BASE_URL"] = "http://127.0.0.1:3000"
os.environ["QQ_ACCESS_TOKEN_ENV"] = "QQ_TEST_ACCESS_TOKEN"
os.environ["QQ_WEBHOOK_TOKEN_ENV"] = "QQ_TEST_WEBHOOK_TOKEN"
os.environ["QQ_REQUEST_TIMEOUT_SECONDS"] = "15"
# 双 token 本值（指向的变量 + 默认变量名——测试专用值，不可触达真实 NapCat）
os.environ["QQ_TEST_ACCESS_TOKEN"] = "test-only-qq-access-token"
os.environ["QQ_ACCESS_TOKEN"] = "test-only-qq-access-token"
os.environ["QQ_TEST_WEBHOOK_TOKEN"] = "test-only-qq-webhook-token"
os.environ["QQ_WEBHOOK_TOKEN"] = "test-only-qq-webhook-token"

# --- 企业微信智能机器人长连接（WECOM_ 前缀 → WecomChannelSettings；默认关闭
#     → 测试零 WebSocket 连接 / 零后台任务；S3 硬覆盖模式——开发者 shell 导出的
#     同名真实配置不得泄入测试。Secret 经 WECOM_TEST_SECRET 间接（长连接专用
#     密钥，与回调 Token 不同源）——测试专用值，不可触达真实机器人；单测经
#     fake 连接消费，永不真实连接） ---
os.environ["WECOM_ENABLED"] = "false"
os.environ["WECOM_BOT_ID"] = ""
os.environ["WECOM_SECRET_ENV"] = "WECOM_TEST_SECRET"
os.environ["WECOM_WS_ENDPOINT"] = "wss://openws.work.weixin.qq.com"
os.environ["WECOM_REQUEST_TIMEOUT_SECONDS"] = "15"
os.environ["WECOM_HEARTBEAT_INTERVAL_SECONDS"] = "30"
os.environ["WECOM_HEARTBEAT_TIMEOUT_SECONDS"] = "10"
os.environ["WECOM_OPEN_TIMEOUT_SECONDS"] = "10"
os.environ["WECOM_BACKOFF_BASE_SECONDS"] = "1.0"
os.environ["WECOM_BACKOFF_MAX_SECONDS"] = "60.0"
os.environ["WECOM_BACKOFF_JITTER_RATIO"] = "0.3"
os.environ["WECOM_REPLY_WINDOW_SECONDS"] = "86400"
# 密钥本值（secret_env 指向的变量 + 默认变量名——测试专用值）
os.environ["WECOM_TEST_SECRET"] = "test-only-wecom-secret"
os.environ["WECOM_SECRET"] = "test-only-wecom-secret"

# --- 值班轮（DUTY_ 前缀 → DutySettings；默认关闭 → 测试零调度副作用；
#     S3 硬覆盖模式——开发者 shell 导出的同名真实配置不得泄入测试） ---
os.environ["DUTY_ENABLED"] = "false"
os.environ["DUTY_DAILY_AT"] = "22:00"
os.environ["DUTY_MAX_TOOL_STEPS"] = "8"
os.environ["DUTY_REPORT_TO_OWNER"] = "true"
os.environ["DUTY_CHARTER_TEXT"] = ""

# --- 梦境调度（DREAM_SCHEDULE_ 前缀 → DreamScheduleSettings；默认关闭
#     → 测试零调度副作用；S3 硬覆盖模式） ---
os.environ["DREAM_SCHEDULE_ENABLED"] = "false"
os.environ["DREAM_SCHEDULE_DAILY_AT"] = "03:00"

# --- 中央调度器（SCHEDULER_ 前缀 → SchedulerSettings；S3 硬覆盖模式） ---
os.environ["SCHEDULER_TICK_SECONDS"] = "1.0"

# --- 工具沙盒（TOOLS_SANDBOX_ 前缀 → ToolsSandboxSettings；默认关闭 →
#     测试零沙盒探测 / 零真实连接；S3 硬覆盖模式——开发者 shell 导出的
#     同名真实配置不得泄入测试。token 经 SANDBOX_TEST_TOKEN 间接——测试
#     专用值，不可触达真实沙盒；单测经 MockTransport / fake 消费，永不
#     真实连接） ---
os.environ["TOOLS_SANDBOX_ENABLED"] = "false"
os.environ["TOOLS_SANDBOX_BASE_URL"] = "http://127.0.0.1:8765"
os.environ["TOOLS_SANDBOX_TOKEN_ENV"] = "SANDBOX_TEST_TOKEN"
os.environ["TOOLS_SANDBOX_REQUEST_TIMEOUT_SECONDS"] = "15"
# token 本值（token_env 指向的变量 + 默认变量名——测试专用值）
os.environ["SANDBOX_TEST_TOKEN"] = "test-only-sandbox-token"
os.environ["SANDBOX_TOKEN"] = "test-only-sandbox-token"

# --- 浏览器 sidecar（TOOLS_BROWSER_SIDECAR_ 前缀 → ToolsBrowserSidecarSettings；
#     默认关闭 → 测试零 sidecar 探测 / 零真实连接；S3 硬覆盖模式——开发者
#     shell 导出的同名真实配置不得泄入测试。token 经
#     BROWSER_SIDECAR_TEST_TOKEN 间接——测试专用值，不可触达真实 sidecar；
#     单测经 MockTransport / fake 消费，永不真实连接） ---
os.environ["TOOLS_BROWSER_SIDECAR_ENABLED"] = "false"
os.environ["TOOLS_BROWSER_SIDECAR_BASE_URL"] = "http://127.0.0.1:8766"
os.environ["TOOLS_BROWSER_SIDECAR_TOKEN_ENV"] = "BROWSER_SIDECAR_TEST_TOKEN"
os.environ["TOOLS_BROWSER_SIDECAR_REQUEST_TIMEOUT_SECONDS"] = "30"
# token 本值（token_env 指向的变量 + 默认变量名——测试专用值）
os.environ["BROWSER_SIDECAR_TEST_TOKEN"] = "test-only-browser-sidecar-token"
os.environ["BROWSER_SIDECAR_TOKEN"] = "test-only-browser-sidecar-token"

# --- 语音域（VOICE_TTS_ / VOICE_ASR_ 前缀 → TtsSettings / AsrSettings；
#     默认关闭 → 测试零真实连接（voice 服务为部署机外部本地进程）；
#     S3 硬覆盖模式——开发者 shell 导出的同名真实配置不得泄入测试；
#     单测经 MockTransport / fake 消费，永不触达真实模型服务） ---
os.environ["VOICE_TTS_ENABLED"] = "false"
os.environ["VOICE_TTS_BASE_URL"] = "http://127.0.0.1:9880"
os.environ["VOICE_TTS_MODEL_ENTRY"] = "cosyvoice3"
os.environ["VOICE_TTS_REQUEST_TIMEOUT_SECONDS"] = "30"
os.environ["VOICE_TTS_DEFAULT_SPEAKER"] = "中文女"
os.environ["VOICE_TTS_DEFAULT_SPEED"] = "1.0"
os.environ["VOICE_ASR_ENABLED"] = "false"
os.environ["VOICE_ASR_BASE_URL"] = "http://127.0.0.1:8082"
os.environ["VOICE_ASR_MODEL_ENTRY"] = "qwen3-asr-0.6b"
os.environ["VOICE_ASR_REQUEST_TIMEOUT_SECONDS"] = "60"
os.environ["VOICE_ASR_LANGUAGE"] = ""
# 上传上限（与生产默认同值——字节精确边界测试经 app.state.settings
# 注入小上限隔离驱动，不依赖本全局值）
os.environ["VOICE_ASR_MAX_UPLOAD_BYTES"] = "26214400"

# --- 联网搜索（SEARCH_ 前缀 → WebSearchSettings；默认关闭 → 测试零真实
#     连接（搜索 provider 为外部 API）；S3 硬覆盖模式——开发者 shell
#     导出的同名真实配置不得泄入测试。api_key_env 间接键（示例条目默认
#     指向的 SEARCH_BOCHA_API_KEY / SEARCH_ZHIPU_API_KEY）同节硬覆盖为
#     测试专用值——单测经 MockTransport 消费，永不触达真实端点；示例
#     模板（.env.example）侧该类含 api_key 字样键按敏感键守卫留空） ---
os.environ["SEARCH_ENABLED"] = "false"
os.environ["SEARCH_TOOL_ENABLED"] = "false"
os.environ["SEARCH_DAILY_BUDGET_PER_PERSONA"] = "50"
os.environ["SEARCH_DAILY_BUDGET_GLOBAL"] = "200"
os.environ["SEARCH_COOLDOWN_SECONDS"] = "60"
# api_key_env 间接键本值（测试专用值，不可触达真实搜索账号）
os.environ["SEARCH_BOCHA_API_KEY"] = "test-only-bocha-api-key"
os.environ["SEARCH_ZHIPU_API_KEY"] = "test-only-zhipu-api-key"

# --- 拟人节律层（RHYTHM_ 前缀 → RhythmSettings；默认关闭 → 测试零节律
#     副作用；S3 硬覆盖模式——开发者 shell 导出的同名真实配置不得泄入
#     测试。naive 本地墙钟约定（scheduler daily_at 同先例）；作息值与
#     生产默认同值——核心函数测试自注时刻，不依赖本全局值） ---
os.environ["RHYTHM_ENABLED"] = "false"
os.environ["RHYTHM_BEDTIME"] = "23:30"
os.environ["RHYTHM_WAKE_TIME"] = "08:00"
os.environ["RHYTHM_TIMEZONE_NOTE"] = "naive-local"
os.environ["RHYTHM_REPLY_DELAY_MIN_MS"] = "1500"
os.environ["RHYTHM_REPLY_DELAY_MAX_MS"] = "45000"
os.environ["RHYTHM_TYPING_SPEED_MS_PER_CHAR"] = "45"
os.environ["RHYTHM_TYPING_BASE_MS"] = "800"
os.environ["RHYTHM_BURST_PROBABILITY"] = "0.12"

# --- 意图快径（INTENT_ 前缀 → IntentSettings；默认开启（纯本地零成本——
#     与 search 等外部成本面开关相反）；S3 硬覆盖模式。快径仅在通道路径
#     注入（CLI / duty 不注入）——既有测试面构造 ChatService 时不带快径
#     面，行为零变化；快径链路测试自注入 fake rng / sleep 面） ---
os.environ["INTENT_ENABLED"] = "true"

# --- 运行环境标识（Settings.env → ENV） ---
os.environ["ENV"] = "test"

# 确保 src/ 在 sys.path 上（与 pyproject.toml 的 pythonpath=["src"] 一致）
_src_path = str(Path(__file__).resolve().parent)
if _src_path not in sys.path:
    sys.path.insert(0, _src_path)

# 仓库根 scripts/（配置模板生成器）亦入搜索路径——byte-stable 测试按顶层
# 模块名导入 generate_examples*，与 pixi gen-examples 执行口径一致。
_scripts_path = Path(_src_path).parent.parent / "scripts"
if _scripts_path.is_dir() and str(_scripts_path) not in sys.path:
    sys.path.insert(0, str(_scripts_path))

# ---------------------------------------------------------------------------
# CWD 隔离：切换到一次性 scratch 目录（在任何业务模块导入之前执行）
# 见模块 docstring「CWD 隔离」一节。进程退出时经 atexit 清理 scratch 目录
# （ignore_errors 幂等安全：多工作者重复清理不冲突），避免 /tmp 下
# ai-companion-test-cwd-* 目录跨多次测试运行累积。
# ---------------------------------------------------------------------------
_TEST_CWD = tempfile.mkdtemp(prefix="ai-companion-test-cwd-")
os.chdir(_TEST_CWD)
atexit.register(shutil.rmtree, _TEST_CWD, ignore_errors=True)

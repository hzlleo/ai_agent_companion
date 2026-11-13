"""Pytest 根 conftest — 环境准备（所有测试目录共享）。

将环境变量设置与 ``sys.path`` 修正放在此根级 conftest，确保
``src/tests/``、``src/tests/integration/`` 以及
``src/packages/harness/**/tests/`` 下的测试在导入任何 ``app`` 或
``packages`` 模块前完成环境准备。

pytest 的 conftest 层级机制：根 conftest 先于子目录 conftest 加载，
因此此文件设置的环境变量在任何业务模块导入时已生效。

关键设计：
    - **测试配置完全独立**：Settings 全量 env 面逐键在此硬覆盖为测试
      专用安全值（P0-3 config chunk 落地后扩充到位——硬覆盖而非
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
os.environ["SEEKDB_TEST_PASSWORD"] = "test-only-seekdb-password"
os.environ["SEEKDB_PASSWORD"] = "test-only-seekdb-password"

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

"""Pytest 根 conftest — 环境准备（所有测试目录共享）。

将环境变量设置与 ``sys.path`` 修正放在此根级 conftest，确保
``src/tests/``、``src/tests/integration/`` 以及
``src/packages/harness/**/tests/`` 下的测试在导入任何 ``app`` 或
``packages`` 模块前完成环境准备。

pytest 的 conftest 层级机制：根 conftest 先于子目录 conftest 加载，
因此此文件设置的环境变量在任何业务模块导入时已生效。

关键设计：
    - **测试配置完全独立**：已知的环境变量在此处强制赋值为测试专用
      安全值（硬覆盖而非 setdefault——开发者 shell 导出的同名变量不得
      泄入测试），测试不依赖 ``.env`` / ``config.yaml`` 文件，可在任意
      CWD 下运行。P0-3 config chunk 落地 Settings 全量 env 面后，须在
      此处同步扩充覆盖清单（本 P0-2 批先覆盖已锁定决策的节段：数据库 /
      日志 / 池 / LLM 端点）。
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
      ``pythonpath=["src"]`` 一致，此处为防御性双重保障）。

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
# 测试配置独立化：已知环境变量强制设置为测试专用安全值
# 必须在导入任何业务模块之前完成（pydantic-settings 在模块导入时读取
# 环境变量构造 settings 单例——环境变量优先级高于 .env 文件）。
# 全部使用硬赋值（os.environ[...] = ...）而非 setdefault：
# 开发者 shell 中残留的同名真实配置（如 DATABASE_TYPE=mysql）不得泄漏进测试。
#
# P0-3 config chunk 落地后扩充：Settings 全量 env 面逐键在此硬覆盖。
# ---------------------------------------------------------------------------

# --- 数据库（MySQL 8 权威存储 / 测试用 sqlite——aiosqlite 驱动） ---
os.environ["DATABASE_TYPE"] = "sqlite"
os.environ["DATABASE_HOST"] = "localhost"
os.environ["DATABASE_PORT"] = "3306"
os.environ["DATABASE_NAME"] = "test_db"
os.environ["DATABASE_USER"] = "test_user"
os.environ["DATABASE_PASSWORD"] = "test_pass"

# --- 日志（相对路径 → 落在下方 scratch CWD 内，永不触碰真实 logs/） ---
os.environ["LOG_LEVEL"] = "INFO"
os.environ["LOG_FILE"] = "logs/test-app.log"
os.environ["LOG_JSON_FILE"] = "logs/test-app.json.log"

# --- 共享池 ---
os.environ["POOLS_THREAD_POOL_SIZE"] = "10"
os.environ["POOLS_HTTP_MAX_CONNECTIONS"] = "100"
os.environ["POOLS_HTTP_MAX_KEEPALIVE_CONNECTIONS"] = "20"

# --- LLM 主端点（远程 OpenAI 兼容端点；测试值不可触达真实端点） ---
os.environ["LLM_BASE_URL"] = "http://127.0.0.1:9/v1"
os.environ["LLM_API_KEY"] = "test-only-llm-api-key"
os.environ["LLM_MODEL"] = "test-model"

# 确保 src/ 在 sys.path 上（与 pyproject.toml 的 pythonpath=["src"] 一致）
_src_path = str(Path(__file__).resolve().parent)
if _src_path not in sys.path:
    sys.path.insert(0, _src_path)

# ---------------------------------------------------------------------------
# CWD 隔离：切换到一次性 scratch 目录（在任何业务模块导入之前执行）
# 见模块 docstring「CWD 隔离」一节。进程退出时经 atexit 清理 scratch 目录
# （ignore_errors 幂等安全：多工作者重复清理不冲突），避免 /tmp 下
# ai-companion-test-cwd-* 目录跨多次测试运行累积。
# ---------------------------------------------------------------------------
_TEST_CWD = tempfile.mkdtemp(prefix="ai-companion-test-cwd-")
os.chdir(_TEST_CWD)
atexit.register(shutil.rmtree, _TEST_CWD, ignore_errors=True)

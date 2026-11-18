"""SeekDB 真引擎强制套件——共享支持件（目标解析 / 连接 / scratch 生命周期 / alembic 进程内运行）。

本套件（``src/tests/integration_seekdb/``）的配置约定（约束 1.6——测试
服务显式配置，文档化于 docs/test-strategy/README.md）：

    - **只读 runner 显式注入的 ``SEEKDB_TEST_*`` env**（CI service
      container env / 本地 ``pixi run tests-seekdb`` 任务 env / 开发者
      shell），**不读** ``src/conftest.py`` 的 S3 硬覆盖 env 面
      （``settings`` / ``SEEKDB_*`` 应用配置）——docker 化测试依赖 +
      显式测试 env 是本工程认可的测试服务模式（同 MySQL service
      container 契约），不属于「真实项目配置」；
    - 缺失 ``SEEKDB_TEST_URL`` → ``pytest.fail`` fail-fast（不静默跳过
      ——SeekDB 真引擎验证为强制面），失败消息携带本地容器启动命令
      （见 :data:`_MISSING_ENV_MESSAGE`）。

关键工程事实（live 实测，v1.4.0 镜像 ``1.4.0-100000172026082615``）：

    - ``innodb_lock_wait_timeout`` 为 **mock 变量**（读回恒 50、
      ``SET SESSION`` 无效——不报错但也不生效）；行锁等待的实际旋钮是
      OceanBase 原生 ``ob_trx_lock_timeout``（微秒），超时抛 MySQL
      错误 1205；
    - 唯一键冲突为 pymysql ``IntegrityError``（错误 1062），消息携带
      约束名（如 ``uq_ff_personas_primary``）——不变式断言按名锁定；
    - ``alembic downgrade base`` 后 ``alembic_version`` 表保留（域表
      全删）——降级态断言按实测形态锁定。
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import aiomysql
from alembic import command
from alembic.config import Config as AlembicConfig
from loguru import logger
from pydantic import SecretStr

from packages.harness.config.models import settings

__all__ = [
    "EXPECTED_HEAD_TABLES",
    "HEAD_REVISION",
    "SeekDbTestTarget",
    "connect_scratch",
    "connect_server",
    "drop_scratch_database",
    "fetch_alembic_version",
    "fetch_column_names",
    "fetch_table_names",
    "recreate_scratch_database",
    "assert_reminder_scan_lock",
    "resolve_seekdb_test_target",
    "run_alembic_command",
]

#: backend 项目根（alembic.ini / alembic/ 的定位锚——CWD 无关；pytest 下
#: CWD 已被根 conftest 切到 scratch 隔离目录，ini 内相对 script_location
#: 失效，须绝对化）。
_BACKEND_ROOT = Path(__file__).resolve().parents[3]

#: 本地容器启动命令（fail-fast 消息与文档共用的事实源——镜像标签钉死
#: v1.4.0 发行镜像，容器默认 root 空密码）。
_DOCKER_RUN_COMMAND = (
    "docker run -d --name seekdb-smoke -p 2881:2881 \\\n"
    "      oceanbase/seekdb:1.4.0-100000172026082615"
)

#: 缺失显式测试 env 的 fail-fast 消息（pytest.fail 载荷——告诉开发者
#: 如何启动容器 + CI 自动提供，绝不静默跳过）。
_MISSING_ENV_MESSAGE = (
    "未设置 SEEKDB_TEST_URL——SeekDB 真引擎强制套件不静默跳过。\n"
    "本地启动容器（root 空密码，一次性命令）：\n"
    f"    {_DOCKER_RUN_COMMAND}\n"
    "随后经 pixi 任务运行（测试 env 已由任务注入）：\n"
    "    cd backend && pixi run tests-seekdb\n"
    "或手动注入 env 定向运行：\n"
    '    SEEKDB_TEST_URL="127.0.0.1:2881" SEEKDB_TEST_USER=root \\\n'
    '    SEEKDB_TEST_PASSWORD="" SEEKDB_TEST_DATABASE="aac_ci_verify" \\\n'
    "    pixi run pytest src/tests/integration_seekdb -v --asyncio-mode=auto\n"
    "CI 已自动提供 SeekDB service container（backend job）——本失败仅出现在"
    "未注入测试服务 env 的本地运行。"
)

#: alembic 迁移链 head 版本戳（0016——声纹 / 同意 / 表情包表迁移）。
HEAD_REVISION = "000000000016"

#: 升级 head 后的全量表集（alembic_version + 16 张 ff_* 域表——0011 新增
#: ff_reminders；0013 新增 ff_persona_facts；0014 新增 ff_persona_skills；
#: 0015 新增 ff_core_memory_blocks / ff_core_memory_revisions；0016 新增
#: ff_voiceprint_samples / ff_voiceprint_consents / ff_stickers）。
EXPECTED_HEAD_TABLES: frozenset[str] = frozenset(
    {
        "alembic_version",
        "ff_chat_messages",
        "ff_chat_sessions",
        "ff_core_memory_blocks",
        "ff_core_memory_revisions",
        "ff_curated_memory",
        "ff_dream_runs",
        "ff_memory_episodes",
        "ff_persona_facts",
        "ff_persona_skills",
        "ff_persona_states",
        "ff_personas",
        "ff_reminders",
        "ff_stickers",
        "ff_usage_records",
        "ff_voiceprint_consents",
        "ff_voiceprint_samples",
    }
)

#: alembic env.py URL 构建入口的 env 覆盖面（DatabaseConfig 的 env_prefix
#: 逐键——见 backend/alembic/env.py）。
_DATABASE_ENV_KEYS = (
    "DATABASE_TYPE",
    "DATABASE_HOST",
    "DATABASE_PORT",
    "DATABASE_NAME",
    "DATABASE_USER",
    "DATABASE_PASSWORD",
)


@dataclass(frozen=True)
class SeekDbTestTarget:
    """真引擎套件的连接目标（显式测试服务配置的解析产物）。

    Attributes:
        host: SeekDB 服务地址（server 模式）。
        port: MySQL 线协议端口。
        user: 连接用户名。
        password: 连接密码（本地容器默认空——root 空密码形态）。
        database: scratch 库名（默认 ``aac_ci_verify``）。
    """

    host: str
    port: int
    user: str
    password: str
    database: str


def resolve_seekdb_test_target() -> SeekDbTestTarget:
    """从显式测试 env（``SEEKDB_TEST_*``）解析连接目标。

    解析面：``SEEKDB_TEST_URL``（``host:port``，缺失即失败——fail-fast
    消息见 :data:`_MISSING_ENV_MESSAGE`）、``SEEKDB_TEST_USER``（默认
    ``root``）、``SEEKDB_TEST_PASSWORD``（默认空）、``SEEKDB_TEST_DATABASE``
    （默认 ``aac_ci_verify``）。

    Returns:
        连接目标。

    Raises:
        ValueError: ``SEEKDB_TEST_URL`` 缺失或形态非法（端口非数字 /
            越界 / 主机为空）——由 conftest 转译为 ``pytest.fail``。
    """
    url = os.environ.get("SEEKDB_TEST_URL", "")
    if not url:
        raise ValueError(_MISSING_ENV_MESSAGE)
    host, _, port_text = url.partition(":")
    if not host:
        raise ValueError(f"SEEKDB_TEST_URL 形态非法（host 为空）: {url!r}")
    if port_text and not port_text.isdigit():
        raise ValueError(f"SEEKDB_TEST_URL 形态非法（port 非数字）: {url!r}")
    port = int(port_text) if port_text else 2881
    if not (1 <= port <= 65535):
        raise ValueError(f"SEEKDB_TEST_URL 端口越界: {url!r}")
    return SeekDbTestTarget(
        host=host,
        port=port,
        user=os.environ.get("SEEKDB_TEST_USER", "root"),
        password=os.environ.get("SEEKDB_TEST_PASSWORD", ""),
        database=os.environ.get("SEEKDB_TEST_DATABASE", "aac_ci_verify"),
    )


async def connect_server(target: SeekDbTestTarget) -> aiomysql.Connection:
    """开服务器级连接（不选库——scratch 库 DROP/CREATE 生命周期用）。

    Args:
        target: 连接目标。

    Returns:
        已建立的连接（关闭归调用方—— ``conn.close()``）。
    """
    return await aiomysql.connect(
        host=target.host,
        port=target.port,
        user=target.user,
        password=target.password,
        autocommit=True,
    )


async def connect_scratch(
    target: SeekDbTestTarget, *, autocommit: bool = True
) -> aiomysql.Connection:
    """开 scratch 库连接（种子 / 断言 / 锁实验用）。

    Args:
        target: 连接目标（库名取 ``target.database``）。
        autocommit: 自动提交开关（种子连接 True——语句即事务落库；锁
            实验连接 False——显式事务持锁）。

    Returns:
        已建立的连接（关闭归调用方）。
    """
    return await aiomysql.connect(
        host=target.host,
        port=target.port,
        user=target.user,
        password=target.password,
        db=target.database,
        autocommit=autocommit,
    )


async def recreate_scratch_database(target: SeekDbTestTarget) -> None:
    """重建 scratch 库（DROP IF EXISTS + CREATE——幂等）。

    套件 hermetic-per-run 的 setup 侧保证：每模块 fixture 先行重建，
    上一轮 / 上一模块的残留表与行全部清除（单进程顺序运行——同名库
    重建无并行冲突）。

    Args:
        target: 连接目标。
    """
    conn = await connect_server(target)
    try:
        async with conn.cursor() as cur:
            await cur.execute(f"DROP DATABASE IF EXISTS {target.database}")
            await cur.execute(f"CREATE DATABASE {target.database}")
    finally:
        conn.close()


async def drop_scratch_database(target: SeekDbTestTarget) -> None:
    """删除 scratch 库（teardown 侧——best-effort，失败仅告警）。

    容器中途消失等 teardown 失败不应翻转已通过的测试信号（失败记
    warning 日志——不静默）；setup 侧 DROP+CREATE 自愈，残留无害。

    Args:
        target: 连接目标。
    """
    try:
        conn = await connect_server(target)
    except Exception as exc:
        logger.warning("SeekDB scratch 库 teardown 连接失败（残留无害）: reason={!r}", exc)
        return
    try:
        async with conn.cursor() as cur:
            await cur.execute(f"DROP DATABASE IF EXISTS {target.database}")
    except Exception as exc:
        logger.warning("SeekDB scratch 库 teardown 删除失败（残留无害）: reason={!r}", exc)
    finally:
        conn.close()


async def fetch_table_names(target: SeekDbTestTarget) -> set[str]:
    """读 scratch 库全量表名（``SHOW TABLES``——迁移链断言面）。

    Args:
        target: 连接目标。

    Returns:
        表名集合。
    """
    conn = await connect_scratch(target)
    try:
        async with conn.cursor() as cur:
            await cur.execute("SHOW TABLES")
            rows = await cur.fetchall()
    finally:
        conn.close()
    return {str(row[0]) for row in rows}


async def fetch_alembic_version(target: SeekDbTestTarget) -> str | None:
    """读 ``alembic_version`` 版本戳（迁移链断言面）。

    Args:
        target: 连接目标。

    Returns:
        当前版本戳（表空 / 表不存在时 None）。
    """
    conn = await connect_scratch(target)
    try:
        async with conn.cursor() as cur:
            await cur.execute("SELECT version_num FROM alembic_version")
            row = await cur.fetchone()
    finally:
        conn.close()
    return str(row[0]) if row else None


async def fetch_column_names(target: SeekDbTestTarget, table_name: str) -> set[str]:
    """读指定表的列名集合（迁移列断言面——information_schema 直查）。

    Args:
        target: 连接目标。
        table_name: 表名。

    Returns:
        列名集合。
    """
    conn = await connect_scratch(target)
    try:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT COLUMN_NAME FROM information_schema.columns "
                "WHERE table_schema = %s AND table_name = %s",
                (target.database, table_name),
            )
            rows = await cur.fetchall()
    finally:
        conn.close()
    return {str(row[0]) for row in rows}


@contextmanager
def _alembic_targeting(target: SeekDbTestTarget) -> Iterator[None]:
    """把进程内 alembic 环境指向 scratch 库（DATABASE_* env + settings 单例）。

    ``backend/alembic/env.py`` 从 ``settings.database.async_sqlalchemy_url``
    构造连接串（pydantic-settings 单例——构造输入是 ``DATABASE_*`` env
    面）。进程内程序化运行 ``alembic.command`` 时单例已在导入期固化
    （本模块顶层导入——值来自根 conftest 的 sqlite 硬覆盖 env），因此
    双通道对齐：

        - env 覆盖（``DATABASE_*`` 逐键硬设——与 alembic CLI 的
          使用口径一致，env.py 未来直读 env 亦正确）；
        - 单例字段补丁（运行期改写已固化的 ``settings.database``——
          ``async_sqlalchemy_url`` 属性实时计算，补丁即刻生效）。

    退出时全量还原（env 逐键还原 + 字段回填——测试间零泄漏）。

    Args:
        target: 连接目标（scratch 库）。

    Yields:
        无载荷（上下文期内 alembic 指向 scratch 库）。
    """
    env_backup = {key: os.environ.get(key) for key in _DATABASE_ENV_KEYS}
    os.environ["DATABASE_TYPE"] = "mysql"
    os.environ["DATABASE_HOST"] = target.host
    os.environ["DATABASE_PORT"] = str(target.port)
    os.environ["DATABASE_NAME"] = target.database
    os.environ["DATABASE_USER"] = target.user
    os.environ["DATABASE_PASSWORD"] = target.password

    database = settings.database
    fields_backup = (
        database.type,
        database.host,
        database.port,
        database.name,
        database.user,
        database.password,
    )
    database.type = "mysql"
    database.host = target.host
    database.port = target.port
    database.name = target.database
    database.user = target.user
    database.password = SecretStr(target.password)
    try:
        yield
    finally:
        (
            database.type,
            database.host,
            database.port,
            database.name,
            database.user,
            database.password,
        ) = fields_backup
        for key, value in env_backup.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def run_alembic_command(
    target: SeekDbTestTarget, revision: str, *, downgrade: bool = False
) -> None:
    """进程内运行 alembic 命令（upgrade / downgrade——指向 scratch 库）。

    同步入口（alembic command API 本为 CLI 同步面）：异步测试上下文
    须经 ``asyncio.to_thread`` 桥接调用（env.py 在线迁移内部
    ``asyncio.run``——运行中事件循环内直接调用即错）；同步 fixture
    setup 期（无运行中循环）可直接调用。

    Args:
        target: 连接目标（scratch 库——命令的数据库指向）。
        revision: 目标版本（``"head"`` / ``"base"`` / 具体版本号）。
        downgrade: True 运行降级（缺省升级）。
    """
    alembic_config = AlembicConfig(str(_BACKEND_ROOT / "alembic.ini"))
    # ini 的 script_location 为相对值（按 CWD 解析）——pytest 下 CWD 是
    # 根 conftest 的 scratch 隔离目录，必须绝对化覆盖。
    alembic_config.set_main_option("script_location", str(_BACKEND_ROOT / "alembic"))
    with _alembic_targeting(target):
        if downgrade:
            command.downgrade(alembic_config, revision)
        else:
            command.upgrade(alembic_config, revision)


#: 提醒扫描语句（与 harness/reminders/service 的 scan_due_reminders 同形——
#: 锁定子句方言面断言真源）。
_REMINDER_SCAN_LOCK_SQL = (
    "SELECT id FROM ff_reminders"
    " WHERE status = 'pending' AND due_at_ms <= %s"
    " ORDER BY id ASC LIMIT 1 FOR UPDATE SKIP LOCKED"
)


async def assert_reminder_scan_lock(target: SeekDbTestTarget) -> None:
    """真引擎断言提醒扫描锁定子句可用（FOR UPDATE SKIP LOCKED）。

    语义（锁定决策）：扫描作业以 ``FOR UPDATE SKIP LOCKED`` 让并发扫描
    实例抢占互不重叠的到期行；MySQL 8 / SeekDB 支持该子句，SQLite 方言
    忽略——因此该能力只能在真引擎锁定。断言形态：插入一条到期行 → 两
    个连接分别执行同形扫描语句（autocommit=False 持锁）——第一个连接
    命中并持锁，第二个连接因 SKIP LOCKED 立即返回空集（不阻塞、不报错）。

    Args:
        target: 连接目标（scratch 库已迁移到 head）。

    Raises:
        aiomysql.MySQLError: 方言不支持 SKIP LOCKED（语句解析失败）——
            测试红（真引擎强制面）。
    """
    seed = await connect_scratch(target)
    try:
        async with seed.cursor() as cursor:
            await cursor.execute(
                "INSERT INTO ff_reminders"
                " (persona_id, title, due_at_ms, status, delivery_attempts, source,"
                " created_at, updated_at)"
                " VALUES ('1', 'scan-lock-probe', 1, 'pending', 0, 'manual', 1, 1)"
            )
    finally:
        seed.close()

    first = await connect_scratch(target, autocommit=False)
    second = await connect_scratch(target, autocommit=False)
    try:
        async with first.cursor() as cursor:
            await cursor.execute(_REMINDER_SCAN_LOCK_SQL, (1000,))
            first_rows = await cursor.fetchall()
        async with second.cursor() as cursor:
            await cursor.execute(_REMINDER_SCAN_LOCK_SQL, (1000,))
            second_rows = await cursor.fetchall()
        assert len(first_rows) == 1, f"首个连接未命中到期行: {first_rows!r}"
        assert len(second_rows) == 0, f"SKIP LOCKED 未生效: {second_rows!r}"
    finally:
        await first.rollback()
        first.close()
        await second.rollback()
        second.close()

#!/usr/bin/env python3
"""ASGI 进程内只读端点冒烟（api smoke）——真实应用栈的延迟基线回归面。

用途：在不启动真实端口、不依赖真实 ``.env`` / ``config.yaml`` / 数据库的
前提下，用 httpx ``ASGITransport`` 直连进程内 FastAPI 应用，对四个只读
管理端点逐端点取样，报告 p50 / p95 毫秒并与基线对照。数据库固定为一次
性临时 SQLite 文件（脚本结束删除），配置由本脚本显式构造——服务器鉴权
令牌取自隔离 Settings 指向的专用变量，开发者环境中的同名配置不参与。

端点面（只读）：

    - ``GET /api/v1/health``（零依赖存活面）；
    - ``GET /api/v1/models/entries``（模型注册表脱敏读取）；
    - ``GET /api/v1/personas``（人格列表）；
    - ``GET /api/v1/canvas?persona_id=<id>``（画布列表；冒烟前落一个人格）。

模式（互斥）：

    - 默认：取样并报告，全部响应须为 200，否则 exit 1；
    - ``--gate``：任一端点 p95 超过基线 p95 × ``max_ratio`` 即红；基线键集
      与端点注册表不一致同样红（防静默漏测）；
    - ``--update``：按本轮取样重建基线（人工评审后提交）；
    - ``--self-check``：只校验端点注册表与基线键集合双向一致，不建库、
      不跑请求（供门禁链快速校验基线完整性）。

基线文件 ``scripts/.api_smoke_baseline.json`` 记录每端点 p50 / p95 上限
毫秒与 ``max_ratio`` 环境容忍倍率。容忍倍率存在的原因：进程内延迟对
CPU 频率、容器配额与同机负载极敏感，硬阈值在共享 CI 上必然抖动；倍数内
只报告漂移，倍数外才判定回归。``--gate`` 建议在部署机或负载可控环境手动
执行，避免与其它构建任务并发导致误报。

用法::

    cd backend && pixi run api-smoke
    cd backend && pixi run api-smoke --self-check
    cd backend && pixi run api-smoke --gate
    cd backend && pixi run api-smoke --update

Exit:
    0 = 报告 / 门禁通过 / 自检通过 / 基线更新成功；
    1 = 响应非 200、门禁超限、自检键集不一致，或无关分节的环境变量取值
        非法（导入期 / 隔离配置构造的 ``ValidationError``——打印单行受控
        错误，不展开 traceback）；
    2 = 参数错误 / 基线缺失或损坏。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import statistics
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: 仓库根（本脚本位于 <repo>/scripts/）。
REPO_ROOT = Path(__file__).resolve().parents[1]

#: 后端源码根（直接执行本脚本时补齐导入路径；pixi 任务已注入 PYTHONPATH）。
BACKEND_SRC = REPO_ROOT / "backend" / "src"

#: 基线文件（随仓库提交）。
BASELINE_PATH = Path(__file__).resolve().parent / ".api_smoke_baseline.json"

#: 基线 schema 版本（口径变化时递增）。
_BASELINE_VERSION = 1

#: 环境容忍倍率：实测 p95 超过基线该倍数才判回归。
MAX_RATIO = 10.0

#: 每端点预热请求数（不计入样本）。
_WARMUP = 3

#: 每端点取样次数。
_SAMPLES = 20

#: 隔离 Settings 使用的专用鉴权环境变量名（脚本内赋值 / 用后恢复）。
_TOKEN_ENV = "API_SMOKE_ADMIN_TOKEN"

#: 脚本内使用的令牌值（只存在于本进程，不落任何配置文件）。
_TOKEN_VALUE = "api-smoke-local-token"


@dataclass(frozen=True)
class Endpoint:
    """只读端点登记。

    Attributes:
        name: 端点标识（基线键 / 报告行标识）。
        path: 请求路径（不含人格参数——需要时由调用方拼接）。
        requires_persona: 是否追加 ``persona_id`` 查询参数。
    """

    name: str
    path: str
    requires_persona: bool = False


@dataclass(frozen=True)
class EndpointMeasurement:
    """单端点取样结果。

    Attributes:
        name: 端点标识。
        p50_ms: 中位延迟毫秒。
        p95_ms: 95 分位延迟毫秒。
        status_code: 最后一次响应状态码（全部取样须为 200）。
    """

    name: str
    p50_ms: float
    p95_ms: float
    status_code: int


#: 端点注册表（name 即基线键；顺序即报告顺序）。
_ENDPOINTS: tuple[Endpoint, ...] = (
    Endpoint(name="GET /api/v1/health", path="/api/v1/health"),
    Endpoint(name="GET /api/v1/models/entries", path="/api/v1/models/entries"),
    Endpoint(name="GET /api/v1/personas", path="/api/v1/personas"),
    Endpoint(
        name="GET /api/v1/canvas?persona_id",
        path="/api/v1/canvas",
        requires_persona=True,
    ),
)


def _endpoint_names() -> list[str]:
    """返回注册表端点名（排序后——键集比较用）。

    Returns:
        端点名列表。
    """
    return sorted(endpoint.name for endpoint in _ENDPOINTS)


def _percentile_ms(samples_ms: list[float], ratio: float) -> float:
    """按最近秩法计算分位延迟。

    Args:
        samples_ms: 单次请求延迟样本（毫秒）。
        ratio: 分位比例（0-1）。

    Returns:
        对应分位延迟毫秒。
    """
    ordered = sorted(samples_ms)
    index = max(0, min(len(ordered) - 1, math.ceil(ratio * len(ordered)) - 1))
    return ordered[index]


def _load_baseline(*, required: bool) -> dict[str, dict[str, float]] | None:
    """读取基线文件并校验 schema。

    Args:
        required: True 时基线缺失 / 损坏直接退出 2；False 时返回 None。

    Returns:
        ``{端点名: {p50_ms, p95_ms}}``；无基线时返回 None。

    Raises:
        SystemExit: 基线缺失 / 损坏 / 版本不符且 ``required``（退出码 2）。
    """
    if not BASELINE_PATH.is_file():
        if required:
            print(
                f"[api_smoke] 基线缺失: {BASELINE_PATH}（先运行 --update）",
                file=sys.stderr,
            )
            raise SystemExit(2)
        return None
    try:
        payload = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"[api_smoke] 基线损坏: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    if payload.get("version") != _BASELINE_VERSION:
        print(
            f"[api_smoke] 基线版本不兼容（{payload.get('version')}）——重新 --update",
            file=sys.stderr,
        )
        raise SystemExit(2)
    raw_endpoints = payload.get("endpoints", {})
    if not isinstance(raw_endpoints, dict):
        print("[api_smoke] 基线 endpoints 字段不是对象", file=sys.stderr)
        raise SystemExit(2)
    endpoints: dict[str, dict[str, float]] = {}
    for name, raw in raw_endpoints.items():
        if not isinstance(raw, dict):
            print(f"[api_smoke] 基线端点 {name} 结构非法", file=sys.stderr)
            raise SystemExit(2)
        endpoints[str(name)] = {
            "p50_ms": float(raw.get("p50_ms", 0.0)),
            "p95_ms": float(raw.get("p95_ms", 0.0)),
        }
    return endpoints


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    """原子写入 JSON 基线（同目录临时文件 + ``os.replace``）。

    先完整写入同目录 ``.tmp`` 再整体替换目标文件——写入中途失败时目标基线
    保持原内容，不会留下半截 JSON；临时文件在 ``finally`` 中清理。

    Args:
        path: 目标 JSON 文件路径。
        payload: 待序列化对象（UTF-8 / 两空格缩进 / 末尾换行）。
    """
    tmp_path = path.with_name(f"{path.name}.tmp")
    try:
        tmp_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(tmp_path, path)
    finally:
        tmp_path.unlink(missing_ok=True)


def _write_baseline(measurements: list[EndpointMeasurement]) -> None:
    """按本轮取样写入基线文件（人工评审后提交）。

    Args:
        measurements: 全部端点取样结果。
    """
    payload = {
        "version": _BASELINE_VERSION,
        "max_ratio": MAX_RATIO,
        "note": (
            "ASGI 进程内只读端点 p50/p95 上限毫秒；门禁只按 p95 × max_ratio "
            "判定；再生成: cd backend && pixi run api-smoke --update"
        ),
        "endpoints": {
            item.name: {"p50_ms": item.p50_ms, "p95_ms": item.p95_ms}
            for item in measurements
        },
    }
    _write_json_atomic(BASELINE_PATH, payload)
    print(
        f"[api_smoke] 基线已写入 {BASELINE_PATH}（端点 {len(measurements)} 项，"
        f"max_ratio={MAX_RATIO:g}）"
    )


def _print_table(
    measurements: list[EndpointMeasurement],
    baseline: dict[str, dict[str, float]] | None,
) -> None:
    """打印取样表（含基线与比值列）。

    Args:
        measurements: 取样结果。
        baseline: 基线（None = 无对照）。
    """
    header = f"{'端点':<40} {'p50 ms':>10} {'p95 ms':>10} {'基线 p95 ms':>14} {'比值':>8}"
    print("[api_smoke] 进程内延迟（ASGI 直连；同机复跑为准，跨机绝对耗时不可比）")
    print(header)
    print("-" * len(header))
    for item in measurements:
        if baseline is None or item.name not in baseline:
            print(
                f"{item.name:<40} {item.p50_ms:>10.3f} {item.p95_ms:>10.3f} "
                f"{'—':>14} {'—':>8}"
            )
            continue
        base_p95 = baseline[item.name]["p95_ms"]
        ratio = item.p95_ms / base_p95 if base_p95 > 0 else float("inf")
        print(
            f"{item.name:<40} {item.p50_ms:>10.3f} {item.p95_ms:>10.3f} "
            f"{base_p95:>14.3f} {ratio:>8.2f}"
        )


def _build_isolated_settings(db_path: Path) -> Any:
    """构造完全显式的隔离 Settings（服务器 / 数据库 / 日志三分节）。

    数据库固定为一次性 SQLite 文件；服务器鉴权变量名固定为脚本专用名，
    令牌值只存在于本进程——开发者 ``ADMIN_AUTH_TOKEN`` 不参与。未显式
    覆盖的分节使用代码默认值（本次冒烟只读端点不消费它们）。

    Args:
        db_path: 一次性 SQLite 文件路径。

    Returns:
        ``Settings`` 实例（调用方按鸭子类型消费，避免模块级重导入）。
    """
    from packages.harness.config.models import (
        DatabaseConfig,
        LogConfig,
        ServerConfig,
        Settings,
    )

    os.environ[_TOKEN_ENV] = _TOKEN_VALUE
    return Settings(
        _env_file=None,
        server=ServerConfig(_env_file=None, admin_token_env=_TOKEN_ENV),
        database=DatabaseConfig(_env_file=None, type="sqlite", name=str(db_path)),
        log=LogConfig(_env_file=None),
    )


async def _seed_persona() -> int:
    """在临时库中落一个人格（画布端点需要目标人格）。

    Returns:
        新建人格主键。
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from packages.harness.database.engine import db_engine_holder
    from packages.harness.persona.chara_card import CharaCardModel
    from packages.harness.persona.dtos import PersonaCreateDTO
    from packages.harness.persona.service import PersonaService

    engine = db_engine_holder.async_engine
    if engine is None:
        raise RuntimeError("数据库引擎未初始化")
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    async with factory() as session:
        record = await PersonaService().create(
            session,
            PersonaCreateDTO(
                name="api-smoke-persona",
                card=CharaCardModel(name="冒烟人格", first_mes="你回来啦！"),
            ),
        )
    return record.id


async def _sample_endpoint(client: Any, endpoint: Endpoint, persona_id: int) -> EndpointMeasurement:
    """对单个端点做预热与取样（全部响应须为 200）。

    Args:
        client: 已构造的 ``httpx.AsyncClient``。
        endpoint: 端点登记。
        persona_id: 画布端点的目标人格主键。

    Returns:
        取样结果。

    Raises:
        RuntimeError: 任一次响应非 200（附带状态码与响应体前缀）。
    """
    url = endpoint.path
    if endpoint.requires_persona:
        url = f"{endpoint.path}?persona_id={persona_id}"
    for _ in range(_WARMUP):
        warmup = await client.get(url)
        if warmup.status_code != 200:
            raise RuntimeError(
                f"{endpoint.name} 预热响应 {warmup.status_code}: {warmup.text[:200]}"
            )
    samples_ms: list[float] = []
    last_status = 0
    for _ in range(_SAMPLES):
        started = time.perf_counter_ns()
        response = await client.get(url)
        samples_ms.append((time.perf_counter_ns() - started) / 1_000_000.0)
        last_status = response.status_code
        if response.status_code != 200:
            raise RuntimeError(
                f"{endpoint.name} 取样响应 {response.status_code}: {response.text[:200]}"
            )
    return EndpointMeasurement(
        name=endpoint.name,
        p50_ms=round(statistics.median(samples_ms), 3),
        p95_ms=round(_percentile_ms(samples_ms, 0.95), 3),
        status_code=last_status,
    )


async def _collect(*, db_dir: Path, token: str) -> list[EndpointMeasurement]:
    """构造进程内应用、落库铺底并完成四端点取样。

    Args:
        db_dir: 一次性临时目录（SQLite 文件落点）。
        token: 隔离 Settings 指向的鉴权令牌（请求头使用）。

    Returns:
        端点取样结果（注册顺序）。
    """
    from httpx import ASGITransport, AsyncClient

    from app.main import create_app
    from packages.harness.database.engine import close_database, init_database

    settings = _build_isolated_settings(db_dir / "api-smoke.db")
    app = create_app(settings)
    await init_database(settings.database)
    try:
        persona_id = await _seed_persona()
        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport,
            base_url="http://api-smoke",
            headers={"Authorization": f"Bearer {token}"},
        ) as client:
            return [
                await _sample_endpoint(client, endpoint, persona_id)
                for endpoint in _ENDPOINTS
            ]
    finally:
        await close_database()


def _print_results(measurements: list[EndpointMeasurement]) -> None:
    """打印逐端点状态行（响应码）。

    Args:
        measurements: 取样结果。
    """
    for item in measurements:
        print(f"[api_smoke]   {item.name} -> {item.status_code}")


def _restore_token_env(previous: str | None) -> None:
    """恢复脚本专用鉴权变量（不污染调用进程环境）。

    Args:
        previous: 脚本启动前的变量值（None = 原本不存在）。
    """
    if previous is None:
        os.environ.pop(_TOKEN_ENV, None)
    else:
        os.environ[_TOKEN_ENV] = previous


def _check_keys(baseline: dict[str, dict[str, float]], *, mode: str) -> bool:
    """校验注册表与基线键集合一致。

    Args:
        baseline: 基线端点表。
        mode: 模式名（输出用）。

    Returns:
        True = 一致；False = 存在缺失或多余键。
    """
    registry = set(_endpoint_names())
    baseline_keys = set(baseline)
    if registry == baseline_keys:
        return True
    print(f"[api_smoke] 基线键集与端点注册表不一致（{mode} 拒绝静默漏测）:", file=sys.stderr)
    for name in sorted(registry - baseline_keys):
        print(f"  - 注册表有、基线缺: {name}", file=sys.stderr)
    for name in sorted(baseline_keys - registry):
        print(f"  - 基线有、注册表缺: {name}", file=sys.stderr)
    return False


def _self_check() -> int:
    """校验端点注册表与基线键集合双向一致（不建库、不跑请求）。

    Returns:
        0 = 一致；1 = 键集不一致；基线问题由 :func:`_load_baseline` 退出 2。
    """
    baseline = _load_baseline(required=True)
    assert baseline is not None
    if not _check_keys(baseline, mode="self-check"):
        return 1
    print(
        f"[api_smoke] 自检通过：端点注册表 {len(_ENDPOINTS)} 项与基线键集一致"
        f"（max_ratio={MAX_RATIO:g}）"
    )
    print(f"[api_smoke] 冒烟完成 端点={len(_ENDPOINTS)} 模式=self-check")
    return 0


def _run_with_temp_db() -> list[EndpointMeasurement]:
    """在一次性临时目录中完成取样（同步包装异步采集）。

    Returns:
        端点取样结果。
    """
    previous = os.environ.get(_TOKEN_ENV)
    with tempfile.TemporaryDirectory(prefix="api-smoke-") as raw_dir:
        try:
            return asyncio.run(_collect(db_dir=Path(raw_dir), token=_TOKEN_VALUE))
        finally:
            _restore_token_env(previous)


def _report() -> int:
    """默认报告模式（全部响应须 200；恒不因延迟判红）。

    Returns:
        0 = 取样成功；1 = 响应非 200。
    """
    baseline = _load_baseline(required=False)
    try:
        measurements = _run_with_temp_db()
    except RuntimeError as exc:
        print(f"[api_smoke] 冒烟失败: {exc}", file=sys.stderr)
        return 1
    _print_results(measurements)
    _print_table(measurements, baseline)
    if baseline is None:
        print("[api_smoke] 未找到基线（仅报告，不判定）——需要门禁时先 --update")
    print(f"[api_smoke] 冒烟完成 端点={len(measurements)} 模式=report 结果=PASS")
    return 0


def _gate() -> int:
    """执行取样并对照基线判定 p95 回归。

    Returns:
        0 = 全部在容忍倍率内；1 = 存在超限 / 非 200 / 键集不一致；基线问题退出 2。
    """
    baseline = _load_baseline(required=True)
    assert baseline is not None
    if not _check_keys(baseline, mode="gate"):
        return 1
    try:
        measurements = _run_with_temp_db()
    except RuntimeError as exc:
        print(f"[api_smoke] 门禁取样失败: {exc}", file=sys.stderr)
        return 1
    _print_results(measurements)
    _print_table(measurements, baseline)
    regressions: list[str] = []
    for item in measurements:
        base_p95 = baseline[item.name]["p95_ms"]
        threshold = base_p95 * MAX_RATIO
        if item.p95_ms > threshold:
            regressions.append(
                f"{item.name}: p95 {item.p95_ms:.3f} ms > 基线 {base_p95:.3f} ms × "
                f"{MAX_RATIO:g}（比值 {item.p95_ms / base_p95:.2f}）"
            )
    if regressions:
        print(f"[api_smoke] 延迟回归（容忍倍率 {MAX_RATIO:g}）:", file=sys.stderr)
        for line in regressions:
            print(f"  - {line}", file=sys.stderr)
        print("[api_smoke] 冒烟完成 模式=gate 结果=FAIL", file=sys.stderr)
        return 1
    print(f"[api_smoke] 门禁通过（{len(measurements)} 端点均在 p95 × {MAX_RATIO:g} 内）")
    print(
        f"[api_smoke] 冒烟完成 端点={len(measurements)} 模式=gate 结果=PASS"
    )
    return 0


def _is_config_validation_error(exc: BaseException) -> bool:
    """判定异常是否为 pydantic 配置校验错误。

    延迟导入 pydantic，保持脚本模块导入期只依赖标准库。

    Args:
        exc: 捕获到的异常。

    Returns:
        True = 该异常是 ``pydantic.ValidationError``。
    """
    from pydantic import ValidationError

    return isinstance(exc, ValidationError)


def _format_config_validation_error(exc: BaseException) -> str:
    """把配置校验错误折叠为单行受控提示（不展开 traceback）。

    Args:
        exc: ``pydantic.ValidationError`` 实例。

    Returns:
        含首个错误字段摘要的单行错误文本（字段路径 + 校验消息 + 错误类型）。
    """
    collector = getattr(exc, "errors", None)
    raw_errors = collector() if callable(collector) else []
    if raw_errors:
        first = raw_errors[0]
        location = ".".join(str(part) for part in first.get("loc", ())) or "<root>"
        detail = (
            f"{location}: {first.get('msg', exc)}"
            f"（type={first.get('type', 'unknown')}）"
        )
    else:
        detail = str(exc)
    return (
        "[api_smoke] 非本次冒烟相关环境变量取值非法"
        f"（导入期或隔离配置构造校验失败）：{detail}"
        "——请清理 / 修正该环境变量后重跑（与真实 app 启动失败同源）"
    )


def main(argv: list[str] | None = None) -> int:
    """CLI 入口（模式互斥）。

    Args:
        argv: 命令行参数（None 取 ``sys.argv[1:]``）。

    Returns:
        进程退出码（见模块文档）。

    Raises:
        SystemExit: argparse 参数错误或基线不可用。
    """
    if str(BACKEND_SRC) not in sys.path:
        sys.path.insert(0, str(BACKEND_SRC))
    parser = argparse.ArgumentParser(description="ASGI 进程内只读端点冒烟")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--gate", action="store_true", help="门禁：p95 超过基线 × max_ratio 即红")
    group.add_argument("--update", action="store_true", help="按本轮取样重建基线")
    group.add_argument("--self-check", action="store_true", help="只校验端点注册表与基线键集一致")
    args = parser.parse_args(argv)

    try:
        if args.self_check:
            return _self_check()
        if args.update:
            try:
                measurements = _run_with_temp_db()
            except RuntimeError as exc:
                print(f"[api_smoke] 取样失败: {exc}", file=sys.stderr)
                return 1
            _print_results(measurements)
            _print_table(measurements, None)
            _write_baseline(measurements)
            print(f"[api_smoke] 冒烟完成 端点={len(measurements)} 模式=update")
            return 0
        if args.gate:
            return _gate()
        return _report()
    except Exception as exc:
        if not _is_config_validation_error(exc):
            raise
        print(_format_config_validation_error(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

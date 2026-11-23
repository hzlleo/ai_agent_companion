"""性能基线脚本守卫——bench_core / api_smoke 的注册表、基线与失败形态。

覆盖面：

    - 注册表与基线键集双向一致（脚本 ``--self-check`` 的生产实现直接复算）；
    - ``MAX_RATIO`` 与基线 ``max_ratio`` 同源（两脚本各自口径）；
    - ``_write_json_atomic`` 成功落盘，以及目标为目录时的失败形态（抛
      ``OSError``、目标原样、不留 ``.tmp``）；
    - 配置校验错误折叠助手：构造真实 pydantic ``ValidationError``，断言输出
      为单行且含字段路径 / 校验消息 / 错误类型（不启动应用）；
    - ``api_smoke`` 端点表为四项，且标识与 ``docs/backend/performance.md``
      的端点表一致（文档与脚本漂移即红）。

脚本以 ``importlib`` 从仓库 ``scripts/`` 直接加载并注册到 ``sys.modules``，
路径由本文件位置推导——不依赖 ``chdir``、不触网、不建库、不等待。
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path
from types import ModuleType

import pytest
from pydantic import BaseModel, Field, ValidationError

#: 仓库根（本文件位于 <repo>/backend/src/tests/unit/）。
REPO_ROOT = Path(__file__).resolve().parents[4]

#: 脚本目录。
SCRIPTS_ROOT = REPO_ROOT / "scripts"

#: 两脚本的基线文件。
BENCH_BASELINE = SCRIPTS_ROOT / ".bench_baseline.json"
SMOKE_BASELINE = SCRIPTS_ROOT / ".api_smoke_baseline.json"

#: 文档中的端点标识形态（反引号包裹的 ``GET /api/v1/...``）。
_DOC_ENDPOINT_PATTERN = re.compile(r"`(GET /api/v1/[^`]+)`")


def _load_script(module_name: str, filename: str) -> ModuleType:
    """从脚本目录加载模块并注册到 ``sys.modules``。

    Args:
        module_name: 注册使用的模块名（与本仓库既有模块不冲突）。
        filename: 脚本文件名。

    Returns:
        已执行完成的模块对象。

    Raises:
        RuntimeError: 文件不存在或无法构造 loader。
    """
    path = SCRIPTS_ROOT / filename
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载脚本模块: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(module_name, None)
        raise
    return module


#: 被测脚本（模块加载一次，导入期只依赖标准库）。
BENCH_CORE = _load_script("perf_scripts_bench_core", "bench_core.py")
API_SMOKE = _load_script("perf_scripts_api_smoke", "api_smoke.py")


def _load_baseline_payload(path: Path) -> dict[str, object]:
    """读取基线 JSON。

    Args:
        path: 基线文件路径。

    Returns:
        反序列化后的顶层对象。
    """
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


class _BurstProbeModel(BaseModel):
    """折叠用例探针模型（越界字段稳定产生 ``less_than_equal`` 错误）。"""

    burst_count: int = Field(ge=1, le=20)


async def test_self_check_key_sets_match_registries() -> None:
    """两脚本的注册表与基线键集双向一致（生产自检实现复算为 0）。"""
    bench_payload = _load_baseline_payload(BENCH_BASELINE)
    smoke_payload = _load_baseline_payload(SMOKE_BASELINE)
    bench_cases = bench_payload["cases"]
    smoke_endpoints = smoke_payload["endpoints"]
    assert isinstance(bench_cases, dict)
    assert isinstance(smoke_endpoints, dict)

    assert BENCH_CORE._case_names() == sorted(bench_cases)
    assert API_SMOKE._endpoint_names() == sorted(smoke_endpoints)
    assert BENCH_CORE._self_check() == 0
    assert API_SMOKE._self_check() == 0


async def test_max_ratio_matches_baseline() -> None:
    """容忍倍率与基线 ``max_ratio`` 同源（不是两份可漂移的常量）。"""
    for module, baseline_path in (
        (BENCH_CORE, BENCH_BASELINE),
        (API_SMOKE, SMOKE_BASELINE),
    ):
        payload = _load_baseline_payload(baseline_path)
        assert module.MAX_RATIO == payload["max_ratio"] == 10.0


async def test_write_json_atomic_is_all_or_nothing(tmp_path: Path) -> None:
    """原子写：成功落盘；目标为目录时抛 ``OSError`` 且不留临时文件。"""
    payload = {"version": 1, "cases": {"probe": {"median_us": 1.0}}}
    for module in (BENCH_CORE, API_SMOKE):
        target = tmp_path / f"{module.__name__}-baseline.json"
        module._write_json_atomic(target, payload)
        assert json.loads(target.read_text(encoding="utf-8")) == payload
        assert not target.with_name(f"{target.name}.tmp").exists()

        blocked = tmp_path / f"{module.__name__}-as-directory"
        blocked.mkdir()
        with pytest.raises(OSError):
            module._write_json_atomic(blocked, payload)
        assert blocked.is_dir()
        assert list(blocked.iterdir()) == []
        assert not blocked.with_name(f"{blocked.name}.tmp").exists()


async def test_validation_error_folding_is_single_line() -> None:
    """折叠助手对真实 ``ValidationError`` 输出单行受控信息（含 loc/msg/type）。"""
    with pytest.raises(ValidationError) as exc_info:
        _BurstProbeModel(burst_count=99)
    error = exc_info.value

    for module, prefix in ((BENCH_CORE, "[bench_core]"), (API_SMOKE, "[api_smoke]")):
        assert module._is_config_validation_error(error) is True
        assert module._is_config_validation_error(ValueError("别的错误")) is False
        message = module._format_config_validation_error(error)
        assert "\n" not in message
        assert message.startswith(prefix)
        assert "burst_count: Input should be less than or equal to 20" in message
        assert "type=less_than_equal" in message


async def test_api_smoke_endpoints_documented() -> None:
    """端点表为四项，且标识与性能基线文档的端点表一致。"""
    endpoints = API_SMOKE._ENDPOINTS
    assert len(endpoints) == 4
    names = set(API_SMOKE._endpoint_names())
    documented = set(
        _DOC_ENDPOINT_PATTERN.findall(
            (REPO_ROOT / "docs" / "backend" / "performance.md").read_text(
                encoding="utf-8"
            )
        )
    )
    assert documented == names
    for endpoint in endpoints:
        assert any(endpoint.path in name for name in documented)

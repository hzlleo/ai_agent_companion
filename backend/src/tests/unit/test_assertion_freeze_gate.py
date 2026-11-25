"""断言棘轮脚本自证——基线外新文件在 gate 报告内被判红，``--update`` 并入。

脚本以 ``importlib`` 从仓库 ``scripts/`` 直接加载，不依赖 ``chdir`` / 子进程；
用例只驱动 ``_report`` / ``_write_baseline`` / ``_load_baseline`` 三个函数，
证明「新增测试文件必须先评审后并入基线」的棘轮语义。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

#: 仓库根（本文件位于 <repo>/backend/src/tests/unit/）。
REPO_ROOT = Path(__file__).resolve().parents[4]

#: 脚本文本路径。
_SCRIPT_PATH = REPO_ROOT / "scripts" / "assertion_freeze.py"


def _load_script() -> ModuleType:
    """加载断言棘轮脚本模块（注册到 ``sys.modules`` 以支持 dataclass 解析）。

    Returns:
        已执行完成的脚本模块。
    """
    spec = importlib.util.spec_from_file_location("assertion_freeze_under_test", _SCRIPT_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载脚本模块: {_SCRIPT_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["assertion_freeze_under_test"] = module
    spec.loader.exec_module(module)
    return module


MODULE = _load_script()


async def test_report_flags_baseline_external_new_file() -> None:
    """基线外新测试文件进入 drops（gate 模式据此 exit 1）。"""
    new_path = "backend/src/tests/unit/test_newly_added.py"
    stats = {
        new_path: MODULE.AssertionStats(asserts=3, raises=1, digest="abc"),
    }
    drops, _digest_changes = MODULE._report(stats, {})
    assert drops == [f"{new_path}: 基线外新测试文件（评审后 --update 并入基线）"]


async def test_update_merges_new_file_into_baseline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--update`` 把基线外新文件并入基线，随后 gate 不再判红。"""
    new_path = "backend/src/tests/unit/test_newly_added.py"
    stats = {
        new_path: MODULE.AssertionStats(asserts=3, raises=1, digest="abc"),
    }
    baseline_path = tmp_path / "baseline.json"
    monkeypatch.setattr(MODULE, "BASELINE_PATH", baseline_path)
    MODULE._write_baseline(stats)
    baseline = MODULE._load_baseline()
    assert baseline[new_path].asserts == 3
    drops, _digest_changes = MODULE._report(stats, baseline)
    assert drops == []

"""GUI 微信通道 wire 契约镜像测试——两树类定义块逐字一致。

漂移门禁（``scripts/check_sandbox_dtos_drift.py``）是权威比对器；本用例在
单测面提供同一判据的快速反馈（块提取口径与门禁一致：类定义行到下一顶层
定义行 / 装饰器，去掉尾部空行）。
"""

from __future__ import annotations

import re
from pathlib import Path

from packages.harness.channels.wechat_gui.wechat_gui_wire_models import (
    HealthResponseModel,
    InvokeRequestModel,
    InvokeResponseModel,
)

_REPO_ROOT = Path(__file__).resolve().parents[4]
_BACKEND = (
    _REPO_ROOT
    / "backend/src/packages/harness/channels/wechat_gui/wechat_gui_wire_models.py"
)
_SANDBOX = _REPO_ROOT / "docker/wechat-gui/src/wechat_gui_poc/wire_models.py"
_TOP_LEVEL_DEF = re.compile(r"^(class\s+\w+|[A-Z][A-Z0-9_]+\s*[:=]|def\s+\w+|@\w)")


def _extract_class_block(text: str, class_name: str) -> str:
    """提取顶层 class 定义块（与漂移门禁同口径）。"""
    lines = text.splitlines()
    pattern = re.compile(rf"^class\s+{re.escape(class_name)}\b")
    start = next((i for i, line in enumerate(lines) if pattern.match(line)), None)
    assert start is not None, f"未找到类定义: {class_name}"
    end = next(
        (i for i in range(start + 1, len(lines)) if _TOP_LEVEL_DEF.match(lines[i])),
        len(lines),
    )
    block = lines[start:end]
    while block and not block[-1].strip():
        block.pop()
    return "\n".join(block)


async def test_wire_models_are_byte_identical_across_trees() -> None:
    """三模型定义块在 backend / 沙盒树逐字一致（任一漂移即红）。"""
    backend_text = _BACKEND.read_text(encoding="utf-8")
    sandbox_text = _SANDBOX.read_text(encoding="utf-8")
    for name in ("InvokeRequestModel", "InvokeResponseModel", "HealthResponseModel"):
        backend_block = _extract_class_block(backend_text, name)
        sandbox_block = _extract_class_block(sandbox_text, name)
        assert backend_block.strip(), name
        assert backend_block == sandbox_block, f"wire 契约漂移: {name}"


async def test_wire_models_serialization_shape() -> None:
    """序列化出口：健康 / 请求 / 成功响应 / 失败响应。"""
    assert HealthResponseModel().to_wire() == {"status": "ok"}
    assert InvokeRequestModel(tool="screenshot").to_wire() == {
        "tool": "screenshot",
        "arguments": {},
    }
    assert InvokeResponseModel(ok=True, result={"a": 1}, latency_ms=2).to_wire() == {
        "ok": True,
        "result": {"a": 1},
        "latency_ms": 2,
    }
    failure = InvokeResponseModel(ok=False, error="失败", latency_ms=3).to_wire()
    assert failure == {"ok": False, "result": {}, "error": "失败", "latency_ms": 3}

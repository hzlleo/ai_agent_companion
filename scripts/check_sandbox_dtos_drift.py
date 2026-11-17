"""沙盒 wire 契约漂移门禁（backend ↔ docker/tool-sandbox-agent）。

两树零代码共享，wire 契约（Model Class 定义块 + 工具名常量行 + 部署
契约常量行）以文本镜像方式耦合——本脚本对两侧定义做文本比对：任一侧
漂移即红（红灯输出首个差异的 unified diff 上下文）。

比对项（8 模型块 + 8 常量行，共 16 项）：

    - ``InvokeRequestModel`` / ``InvokeResponseModel`` /
      ``HealthResponseModel``（backend ``harness/tools/sandbox_models.py``
      ↔ 沙盒树 ``tool_sandbox/wire_models.py``）；
    - ``HttpFetchArgsModel``（backend ``sandbox_models.py`` ↔ 沙盒树
      ``tool_sandbox/http_fetch.py``——参数面双侧校验为有意冗余）；
    - ``BiliSearchVideosArgsModel`` / ``BiliVideoInfoArgsModel`` /
      ``BiliVideoCommentsArgsModel`` / ``BiliSubtitleArgsModel``
      （backend ``sandbox_models.py`` ↔ 沙盒树 ``tool_sandbox/bili_tools.py``）；
    - ``HTTP_FETCH_TOOL_NAME`` 与四个 ``BILI_*_TOOL_NAME`` 常量行（同上文件）；
    - ``SANDBOX_INVOKE_PATH`` / ``SANDBOX_HEALTH_PATH`` /
      ``SANDBOX_TOKEN_ENV_DEFAULT`` 部署契约常量行（backend
      ``harness/tools/sandbox.py`` ↔ 沙盒树 ``server.py``——端点路径与
      token 环境变量缺省名构成两树共享的部署契约面）。

块提取口径：从定义行（``class <名>(`` 顶层起点 / 常量赋值行）到下一个
顶层定义行或文件尾，去除块尾空行——其余文本（含 docstring 与注释）
须 byte 级一致。

用法::

    pixi run sandbox-drift                        # 门禁（漂移 exit 1）
    python scripts/check_sandbox_dtos_drift.py    # 等价直调

Exit:
    0 = 全部一致；1 = 存在漂移（输出差异上下文）；2 = 文件/块缺失。
"""

from __future__ import annotations

import difflib
import re
import sys
from pathlib import Path

#: 仓库根（脚本位于 <root>/scripts/）。
REPO_ROOT: Path = Path(__file__).resolve().parent.parent

#: backend 侧契约文件。
_BACKEND_MODELS: Path = REPO_ROOT / "backend/src/packages/harness/tools/sandbox_models.py"

#: 沙盒树侧 wire 契约文件。
_SANDBOX_WIRE: Path = REPO_ROOT / "docker/tool-sandbox-agent/src/tool_sandbox/wire_models.py"

#: 沙盒树侧 http_fetch 定义文件。
_SANDBOX_FETCH: Path = REPO_ROOT / "docker/tool-sandbox-agent/src/tool_sandbox/http_fetch.py"

#: 沙盒树侧 B站工具定义文件。
_SANDBOX_BILI: Path = REPO_ROOT / "docker/tool-sandbox-agent/src/tool_sandbox/bili_tools.py"

#: backend 侧部署契约常量文件（端点路径 / token 环境变量缺省名）。
_BACKEND_CLIENT: Path = REPO_ROOT / "backend/src/packages/harness/tools/sandbox.py"

#: 沙盒树侧部署契约常量文件（路由定义 / token env 同名镜像）。
_SANDBOX_SERVER: Path = REPO_ROOT / "docker/tool-sandbox-agent/server.py"

#: 顶层定义行模式（class 定义 / 常量赋值 / 函数定义——块边界）。
_TOP_LEVEL_DEF: re.Pattern[str] = re.compile(r"^(class\s+\w+|[A-Z][A-Z0-9_]+\s*[:=]|def\s+\w+)")


def _extract_class_block(text: str, class_name: str) -> str:
    """提取顶层 class 定义块（定义行到下一顶层定义行，去尾空行）。

    Args:
        text: 源文件全文。
        class_name: 类名。

    Returns:
        块文本（含定义行；无尾空行）。

    Raises:
        ValueError: 定义行未找到。
    """
    lines = text.splitlines()
    start = next(
        (i for i, line in enumerate(lines) if line.startswith(f"class {class_name}(")),
        None,
    )
    if start is None:
        raise ValueError(f"未找到类定义: {class_name}")
    end = next(
        (i for i in range(start + 1, len(lines)) if _TOP_LEVEL_DEF.match(lines[i])),
        len(lines),
    )
    block = lines[start:end]
    while block and not block[-1].strip():
        block.pop()
    return "\n".join(block)


def _extract_constant_line(text: str, constant_name: str) -> str:
    """提取顶层常量赋值行（单行契约）。

    Args:
        text: 源文件全文。
        constant_name: 常量名。

    Returns:
        赋值行文本。

    Raises:
        ValueError: 常量行未找到。
    """
    for line in text.splitlines():
        if line.startswith(f"{constant_name}:") or line.startswith(f"{constant_name} ="):
            return line.rstrip()
    raise ValueError(f"未找到常量定义: {constant_name}")


def _report_drift(item: str, backend_text: str, sandbox_text: str) -> None:
    """打印单项漂移的 unified diff 上下文（首 30 行）。"""
    diff = list(
        difflib.unified_diff(
            backend_text.splitlines(),
            sandbox_text.splitlines(),
            fromfile=f"backend:{item}",
            tofile=f"sandbox:{item}",
            lineterm="",
        )
    )[:30]
    print(f"[check_sandbox_dtos_drift] 契约漂移: {item}", file=sys.stderr)
    for line in diff:
        print(f"    {line}", file=sys.stderr)


def main() -> int:
    """门禁入口：逐项文本比对，任一漂移即红。

    Returns:
        0 = 全部一致；1 = 存在漂移；2 = 文件/块缺失。
    """
    paths = [
        _BACKEND_MODELS,
        _SANDBOX_WIRE,
        _SANDBOX_FETCH,
        _SANDBOX_BILI,
        _BACKEND_CLIENT,
        _SANDBOX_SERVER,
    ]
    missing = [str(p.relative_to(REPO_ROOT)) for p in paths if not p.is_file()]
    if missing:
        print(f"[check_sandbox_dtos_drift] 契约文件缺失: {missing}", file=sys.stderr)
        return 2

    backend_text = _BACKEND_MODELS.read_text(encoding="utf-8")
    wire_text = _SANDBOX_WIRE.read_text(encoding="utf-8")
    fetch_text = _SANDBOX_FETCH.read_text(encoding="utf-8")
    bili_text = _SANDBOX_BILI.read_text(encoding="utf-8")
    client_text = _BACKEND_CLIENT.read_text(encoding="utf-8")
    server_text = _SANDBOX_SERVER.read_text(encoding="utf-8")

    items: list[tuple[str, str, str]] = [
        (
            name,
            _extract_class_block(backend_text, name),
            _extract_class_block(wire_text, name),
        )
        for name in ("InvokeRequestModel", "InvokeResponseModel", "HealthResponseModel")
    ]
    items.append(
        (
            "HttpFetchArgsModel",
            _extract_class_block(backend_text, "HttpFetchArgsModel"),
            _extract_class_block(fetch_text, "HttpFetchArgsModel"),
        )
    )
    items.extend(
        (
            name,
            _extract_class_block(backend_text, name),
            _extract_class_block(bili_text, name),
        )
        for name in (
            "BiliSearchVideosArgsModel",
            "BiliVideoInfoArgsModel",
            "BiliVideoCommentsArgsModel",
            "BiliSubtitleArgsModel",
        )
    )
    items.append(
        (
            "HTTP_FETCH_TOOL_NAME",
            _extract_constant_line(backend_text, "HTTP_FETCH_TOOL_NAME"),
            _extract_constant_line(fetch_text, "HTTP_FETCH_TOOL_NAME"),
        )
    )
    items.extend(
        (
            constant,
            _extract_constant_line(backend_text, constant),
            _extract_constant_line(bili_text, constant),
        )
        for constant in (
            "BILI_SEARCH_VIDEOS_TOOL_NAME",
            "BILI_VIDEO_INFO_TOOL_NAME",
            "BILI_VIDEO_COMMENTS_TOOL_NAME",
            "BILI_VIDEO_SUBTITLE_TOOL_NAME",
        )
    )
    items.extend(
        (
            constant,
            _extract_constant_line(client_text, constant),
            _extract_constant_line(server_text, constant),
        )
        for constant in (
            "SANDBOX_INVOKE_PATH",
            "SANDBOX_HEALTH_PATH",
            "SANDBOX_TOKEN_ENV_DEFAULT",
        )
    )

    drifted = False
    try:
        for item, backend_block, sandbox_block in items:
            if backend_block != sandbox_block:
                _report_drift(item, backend_block, sandbox_block)
                drifted = True
    except ValueError as exc:
        print(f"[check_sandbox_dtos_drift] 块提取失败: {exc}", file=sys.stderr)
        return 2

    if drifted:
        print(
            "[check_sandbox_dtos_drift] 存在契约漂移——两树定义块须 byte 级一致，"
            "修改任一侧必须同步另一侧后重跑",
            file=sys.stderr,
        )
        return 1
    print(f"[check_sandbox_dtos_drift] 全部 {len(items)} 项契约一致（backend ↔ 沙盒树）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

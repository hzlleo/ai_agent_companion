#!/usr/bin/env python3
"""导出面完整性门（surface check）——HTTP wire 模型必须登记或豁免留痕。

背景：前端 `src/types/` 是后端 pydantic DTO 的手写镜像，导出登记表
`scripts/export_dto_schemas.py` 是两侧契约漂移门的数据源。新增一个管理
API 的请求 / 响应模型却忘记登记导出，前端契约测试不会红（它只比对已
登记集合），于是新面静默脱离契约保护。

本门禁用 AST 扫描 `backend/src/app/api/`：

    - 路由装饰器的 ``response_model=`` 表达式（含 ``ResultDTO[X]`` 泛型参数）；
    - 路由处理函数的参数注解（请求体模型）；
    - 上述引用解析到 `backend/src/{packages,app}` 内的类定义（harness 侧
      wire 模型即由此纳入）。

每个被引用的模型类名必须满足其一：已登记在导出清单、命中允许清单
`scripts/.surface_allowlist.json`（每项必须写 reason，逐条说明为何不导出
——例如仅通道内部消费、或声纹 DTO 尚未建立前端契约）。未登记的**新增
面**即红；允许清单里已不存在的陈旧条目只报告（清理时删除）。同时用
既有的 `.env.example` 生成器复核 env 键面（不另造轮子）。

用法::

    python scripts/surface_check.py            # 报告
    python scripts/surface_check.py --enforce  # 门禁（未登记新面 exit 1）

Exit:
    0 = 全部登记 / 豁免（报告模式恒 0）；1 = 未登记新面或 env 键面漂移；
    2 = 允许清单损坏。
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path

#: 仓库根（本脚本位于 <repo>/scripts/）。
REPO_ROOT = Path(__file__).resolve().parents[1]

#: 后端源码根。
BACKEND_SRC = REPO_ROOT / "backend" / "src"

#: app 路由目录（扫描根）。
API_ROOT = BACKEND_SRC / "app" / "api"

#: 允许清单路径。
ALLOWLIST_PATH = Path(__file__).resolve().parent / ".surface_allowlist.json"

#: 允许清单 schema 版本。
_ALLOWLIST_VERSION = 1

#: scripts 目录（导入导出器 / 示例生成器用）。
_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))
if str(BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(BACKEND_SRC))

from generate_examples_env import generate_env_example


def _iter_api_files() -> list[Path]:
    """枚举 app/api 下的路由源文件（排序稳定）。"""
    if not API_ROOT.is_dir():
        return []
    return sorted(path for path in API_ROOT.glob("*.py") if path.name != "__init__.py")


def _collect_names(node: ast.AST | None) -> set[str]:
    """收集表达式中的候选类名（Name / Attribute / 容器递归）。

    Args:
        node: 待分析表达式（None 返回空集）。

    Returns:
        候选名字集合。
    """
    if node is None:
        return set()
    names: set[str] = set()
    if isinstance(node, ast.Name):
        names.add(node.id)
    elif isinstance(node, ast.Attribute):
        names.add(node.attr)
        names.update(_collect_names(node.value))
    elif isinstance(node, (ast.Subscript, ast.Tuple, ast.List, ast.Set)):
        names.update(_collect_names(node.value))
        for item in node.slice.elts if isinstance(node.slice, ast.Tuple) else [node.slice]:
            names.update(_collect_names(item))
    elif isinstance(node, ast.BinOp):
        names.update(_collect_names(node.left))
        names.update(_collect_names(node.right))
    return names


def _build_class_index() -> dict[str, set[str]]:
    """建立源码内模块级类名索引（类名 → 定义文件集合）。

    Returns:
        ``{类名: {相对路径}}``。
    """
    index: dict[str, set[str]] = {}
    for root_name in ("packages", "app"):
        root = BACKEND_SRC / root_name
        if not root.is_dir():
            continue
        for path in root.rglob("*.py"):
            parts = path.relative_to(BACKEND_SRC).parts
            if "__pycache__" in parts or "tests" in parts[:-1]:
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            except SyntaxError:
                continue
            rel = path.relative_to(BACKEND_SRC).as_posix()
            for node in tree.body:
                if isinstance(node, ast.ClassDef):
                    index.setdefault(node.name, set()).add(rel)
    return index


def _collect_surface_names() -> dict[str, list[str]]:
    """扫描 app/api，返回被引用的模型类名与出现位置。

    Returns:
        ``{类名: [位置说明]}``。
    """
    index = _build_class_index()
    surfaces: dict[str, list[str]] = {}
    for path in _iter_api_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        rel = path.relative_to(REPO_ROOT).as_posix()
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for decorator in node.decorator_list:
                if not isinstance(decorator, ast.Call):
                    continue
                for keyword in decorator.keywords:
                    if keyword.arg != "response_model":
                        continue
                    for name in _collect_names(keyword.value):
                        if name in index:
                            surfaces.setdefault(name, []).append(
                                f"{rel}:{node.lineno} response_model"
                            )
            for arg in (*node.args.args, *node.args.kwonlyargs):
                if arg.arg in ("self", "cls") or arg.annotation is None:
                    continue
                for name in _collect_names(arg.annotation):
                    if name in index:
                        surfaces.setdefault(name, []).append(
                            f"{rel}:{node.lineno} 参数 {arg.arg}"
                        )
    return dict(sorted(surfaces.items()))


def _load_allowlist() -> dict[str, str]:
    """读取允许清单（surface → reason）。

    Returns:
        ``{面名: reason}``。

    Raises:
        SystemExit: 清单损坏 / 条目缺 reason（退出码 2）。
    """
    if not ALLOWLIST_PATH.is_file():
        print(
            f"[surface_check] 允许清单缺失: {ALLOWLIST_PATH}（新建或先跑报告）",
            file=sys.stderr,
        )
        raise SystemExit(2)
    try:
        payload = json.loads(ALLOWLIST_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"[surface_check] 允许清单损坏: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    if payload.get("version") != _ALLOWLIST_VERSION:
        print(
            f"[surface_check] 允许清单版本不兼容（{payload.get('version')}）",
            file=sys.stderr,
        )
        raise SystemExit(2)
    entries: dict[str, str] = {}
    for raw in payload.get("entries", []):
        surface = str(raw.get("surface", "")).strip()
        reason = str(raw.get("reason", "")).strip()
        if not surface:
            print(f"[surface_check] 允许清单条目缺 surface: {raw}", file=sys.stderr)
            raise SystemExit(2)
        if not reason:
            print(f"[surface_check] 允许清单条目缺 reason: {surface}", file=sys.stderr)
            raise SystemExit(2)
        entries[surface] = reason
    return entries


def _registered_surfaces() -> set[str]:
    """读取导出登记表的 schema 键集合（AST 解析，避免导入 app 触发运行时副作用）。

    Returns:
        已登记模型类名集合。
    """
    path = _SCRIPTS_DIR / "export_dto_schemas.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not (isinstance(node.func, ast.Name) and node.func.id == "SchemaExportEntry"):
            continue
        if not node.args or not isinstance(node.args[0], ast.Constant):
            continue
        if isinstance(node.args[0].value, str):
            names.add(node.args[0].value)
    return names


def _check_env_surface() -> int:
    """用既有生成器复核 `.env.example` 键面。

    Returns:
        0 = 一致；1 = 漂移。
    """
    target = REPO_ROOT / "backend" / ".env.example"
    if not target.is_file():
        print("[surface_check] backend/.env.example 缺失", file=sys.stderr)
        return 1
    expected = generate_env_example()
    actual = target.read_text(encoding="utf-8")
    if expected != actual:
        print(
            "[surface_check] env 键面漂移（.env.example 与配置模型不一致）\n"
            "  修复: cd backend && pixi run gen-examples",
            file=sys.stderr,
        )
        return 1
    print(f"[surface_check] env 键面一致（{len(actual.splitlines())} 行）")
    return 0


def _report(
    surfaces: dict[str, list[str]],
    registered: set[str],
    allowed: dict[str, str],
) -> tuple[list[str], list[str]]:
    """打印面清单对照并返回（未登记明细, 陈旧豁免明细）。"""
    unregistered: list[str] = []
    for name, locations in surfaces.items():
        if name in registered:
            continue
        if name in allowed:
            continue
        unregistered.append(f"{name}（{locations[0]}）")
    stale = sorted(set(allowed) - set(surfaces))
    print(
        f"[surface_check] app/api 引用模型 {len(surfaces)} 个；已登记 "
        f"{len(set(surfaces) & registered)} 个；允许豁免 "
        f"{len(set(surfaces) & set(allowed))} 个"
    )
    if stale:
        print(f"[surface_check] 允许清单陈旧条目 {len(stale)} 个（报告不阻断）:")
        for name in stale:
            print(f"  - {name}: {allowed[name]}")
    if unregistered:
        print("[surface_check] 未登记且未豁免的面:", file=sys.stderr)
        for item in unregistered:
            print(f"  - {item}", file=sys.stderr)
    return unregistered, stale


def main(argv: list[str] | None = None) -> int:
    """CLI 入口。

    Args:
        argv: 命令行参数（None 取 sys.argv[1:]）。

    Returns:
        进程退出码（见模块文档）。
    """
    parser = argparse.ArgumentParser(description="HTTP wire 模型导出面完整性门")
    parser.add_argument("--enforce", action="store_true", help="门禁模式（未登记新面即红）")
    args = parser.parse_args(argv)

    surfaces = _collect_surface_names()
    registered = _registered_surfaces()
    allowed = _load_allowlist()
    unregistered, _ = _report(surfaces, registered, allowed)
    env_status = _check_env_surface()
    if not args.enforce:
        return env_status
    if unregistered:
        print(
            "[surface_check] 结果 FAIL：新增 wire 模型须登记导出（export_dto_schemas.py）"
            "或在允许清单写明 reason",
            file=sys.stderr,
        )
        return 1
    if env_status != 0:
        return env_status
    print("[surface_check] 门禁通过（导出面与 env 键面完整）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

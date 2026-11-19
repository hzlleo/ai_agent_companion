#!/usr/bin/env python3
"""红探针门（red probe）——符号级「去掉实现必须变红」的可执行证据。

用途：证明某一组测试真的在该符号上建立了约束，而不是在断言实现之外
的巧合（字段存在、日志文案、mock 回显）。机制为**源码体替换**：

    1. 把 ``backend/src`` 整树复制到一次性临时目录；
    2. 在副本里用 AST 定位 ``<module>:<attr>`` 的模块级定义，并把它替换
       成抛 ``AssertionError`` 的桩（函数 / 方法替换函数体；类替换全部
       方法体；模块级常量改为惰性抛错的占位赋值）；
    3. 以副本为 rootdir（``pyproject.toml`` 一并复制）只跑 manifest 登记
       的测试 node id，要求至少一条**失败**。

为什么用临时副本做源码替换，而不是 conftest 打补丁：目标模块通常已被
其它模块 ``from x import y`` 提前绑定，运行期改写模块属性只影响之后发起
的 ``x.y`` 查找，改不动已绑定的引用；源码体替换让副本里的模块重新编译
导入，两种引用形态同时失效。临时副本同时保证原树零改动（脚本幂等、可
重跑；中断也不会留下被改坏的源码）。

``--gate`` 模式不执行测试，只校验清单完整性：``backend/src/{packages,app}``
下的非测试源文件必须落在 ``grandfathered``（首版冻结的存量基线）里，或
被至少一个 probe 以符号形式覆盖；未覆盖的新源文件即红。同时校验每个
probe 的符号与测试 node id 真实存在（防登记空探针）。

用法::

    cd backend && pixi run red-probe --symbol packages.harness.chat.flow_gate:evaluate_flow_gate
    cd backend && pixi run red-probe --all
    cd backend && pixi run red-probe --gate

Exit:
    0 = 探针成立 / 门禁通过；1 = 探针未变红或存在未覆盖新文件；
    2 = 清单或符号配置错误。
"""

from __future__ import annotations

import argparse
import ast
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path

#: 仓库根（本脚本位于 <repo>/scripts/）。
REPO_ROOT = Path(__file__).resolve().parents[1]

#: 后端源码根（探针作用域与测试 node id 的解析根）。
BACKEND_SRC = REPO_ROOT / "backend" / "src"

#: 红探针清单（grandfathered 基线 + probe 登记）。
MANIFEST_PATH = REPO_ROOT / "backend" / "red_manifest.toml"

#: 清单 schema 版本（字段口径变化时递增，旧清单拒绝执行）。
_MANIFEST_VERSION = 1

#: 待扫描的生产源根（相对 backend/src）。
_SOURCE_ROOTS = ("packages", "app")

#: 复制副本时跳过的构建产物。
_COPY_IGNORES = ("__pycache__", "*.egg-info", ".pytest_cache", ".coverage*")

#: pytest 摘要计数（-q 输出）。
_FAILED_RE = re.compile(r"(\d+) failed")
_ERROR_RE = re.compile(r"(\d+) (?:errors?|error)")
_PASSED_RE = re.compile(r"(\d+) passed")


@dataclass(frozen=True)
class ProbeSpec:
    """单条红探针登记。

    Attributes:
        name: 探针标识（门禁输出用）。
        module: 目标符号所在模块（``packages.harness...`` 形态）。
        attr: 模块级符号名。
        tests: 期望变红的测试 node id（相对 ``backend/src``）。
    """

    name: str
    module: str
    attr: str
    tests: tuple[str, ...]

    @property
    def symbol(self) -> str:
        """返回 ``module:attr`` 形态的符号标识。"""
        return f"{self.module}:{self.attr}"


@dataclass(frozen=True)
class RedManifest:
    """红探针清单。

    Attributes:
        version: 清单 schema 版本。
        grandfathered: 首版冻结的现存源文件（相对 ``backend/src``）。
        probes: probe 登记表。
    """

    version: int
    grandfathered: tuple[str, ...]
    probes: tuple[ProbeSpec, ...]


@dataclass(frozen=True)
class ProbeOutcome:
    """单次探针执行结果。

    Attributes:
        probe: 探针登记。
        exit_code: pytest 退出码。
        failed: 解析到的失败数。
        errors: 解析到的错误数。
        passed: 解析到的通过数。
    """

    probe: ProbeSpec
    exit_code: int
    failed: int
    errors: int
    passed: int

    @property
    def went_red(self) -> bool:
        """是否至少一条用例失败 / 报错（探针成立判据）。"""
        return self.failed + self.errors >= 1


def _load_manifest(path: Path) -> RedManifest:
    """读取并校验红探针清单。

    Args:
        path: 清单路径。

    Returns:
        解析后的清单对象。

    Raises:
        SystemExit: 文件缺失 / TOML 损坏 / schema 版本不符（退出码 2）。
    """
    if not path.is_file():
        print(f"[red_probe] 清单缺失: {path}", file=sys.stderr)
        raise SystemExit(2)
    try:
        payload = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        print(f"[red_probe] 清单解析失败: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    version = payload.get("version")
    if version != _MANIFEST_VERSION:
        print(
            f"[red_probe] 清单版本不兼容（{version} != {_MANIFEST_VERSION}）",
            file=sys.stderr,
        )
        raise SystemExit(2)
    grandfathered = tuple(str(item) for item in payload.get("grandfathered", []))
    probes: list[ProbeSpec] = []
    for raw in payload.get("probes", []):
        module, _, attr = str(raw.get("symbol", "")).partition(":")
        if not module or not attr:
            print(f"[red_probe] probe 符号格式错误: {raw}", file=sys.stderr)
            raise SystemExit(2)
        probes.append(
            ProbeSpec(
                name=str(raw.get("name", attr)),
                module=module,
                attr=attr,
                tests=tuple(str(item) for item in raw.get("tests", [])),
            )
        )
    return RedManifest(version=version, grandfathered=grandfathered, probes=tuple(probes))


def _module_path(module: str) -> Path:
    """把模块名解析为 ``backend/src`` 下的源文件路径。

    Args:
        module: 点分模块名。

    Returns:
        源文件路径（不保证存在——由调用方校验）。
    """
    return BACKEND_SRC.joinpath(*module.split(".")).with_suffix(".py")


def _relative_source_path(module: str) -> str:
    """返回模块文件相对 ``backend/src`` 的 POSIX 路径。"""
    return _module_path(module).relative_to(BACKEND_SRC).as_posix()


def _parse_module(path: Path) -> ast.Module:
    """解析 Python 源文件为 AST。

    Args:
        path: 源文件路径。

    Returns:
        模块 AST。

    Raises:
        SystemExit: 文件缺失或语法错误（退出码 2）。
    """
    if not path.is_file():
        print(f"[red_probe] 源文件缺失: {path}", file=sys.stderr)
        raise SystemExit(2)
    try:
        return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError as exc:
        print(f"[red_probe] 语法错误: {path}: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc


def _find_top_level(tree: ast.Module, attr: str) -> ast.stmt | None:
    """在模块顶层查找指定名字的定义语句。

    Args:
        tree: 模块 AST。
        attr: 目标符号名。

    Returns:
        命中的定义节点（函数 / 类 / 赋值）；未命中返回 None。
    """
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.name == attr:
                return node
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(isinstance(t, ast.Name) and t.id == attr for t in targets):
                return node
    return None


def _line_indent(lines: list[str], lineno: int) -> str:
    """取某行（1 基）的前导空白。"""
    line = lines[lineno - 1]
    return line[: len(line) - len(line.lstrip())]


def _replace_function_body(
    lines: list[str], node: ast.FunctionDef | ast.AsyncFunctionDef, message: str
) -> None:
    """把函数 / 方法的函数体替换为抛错语句（保留签名与装饰器）。

    Args:
        lines: 源文件行列表（就地修改）。
        node: 函数定义节点。
        message: AssertionError 文案。
    """
    if not node.body:
        return
    indent = _line_indent(lines, node.body[0].lineno)
    lines[node.body[0].lineno - 1 : node.end_lineno] = [
        f"{indent}raise AssertionError({message!r})"
    ]


def _stub_source(text: str, attr: str, message: str) -> str:
    """把模块级符号替换为抛出桩，返回替换后的源码文本。

    Args:
        text: 原始源码。
        attr: 目标符号名（须为模块顶层定义）。
        message: 桩抛出的 AssertionError 文案。

    Returns:
        替换后的源码。

    Raises:
        ValueError: 符号不存在 / 类体无可替换方法且非普通类。
    """
    lines = text.splitlines()
    node = _find_top_level(ast.parse(text), attr)
    if node is None:
        raise ValueError(f"模块顶层未找到符号: {attr}")
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        _replace_function_body(lines, node, message)
        return "\n".join(lines) + "\n"
    if isinstance(node, ast.ClassDef):
        methods = [
            item
            for item in node.body
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]
        if methods:
            # 逆序替换：前面的替换不改变后续节点已记录的行号区间。
            for method in sorted(methods, key=lambda m: m.lineno, reverse=True):
                _replace_function_body(lines, method, message)
            return "\n".join(lines) + "\n"
        is_enum = any(
            (isinstance(base, ast.Name) and "Enum" in base.id)
            or (isinstance(base, ast.Attribute) and "Enum" in base.attr)
            for base in node.bases
        )
        if is_enum:
            raise ValueError(f"枚举类无可替换方法，请改选函数符号: {attr}")
        indent = _line_indent(lines, node.body[0].lineno) if node.body else " " * 4
        insert_at = node.end_lineno
        lines.insert(
            insert_at,
            f"{indent}def __init__(self, *args: object, **kwargs: object) -> None:\n"
            f"{indent}    raise AssertionError({message!r})",
        )
        return "\n".join(lines) + "\n"
    # 模块级赋值：改写成惰性抛错的占位调用（导入期即触发，失败面同 raise）。
    helper = (
        "def _red_probe_boom() -> None:\n"
        f"    raise AssertionError({message!r})\n\n\n"
    )
    value_start = node.lineno - 1
    value_end = node.end_lineno
    indent = _line_indent(lines, node.lineno)
    lines[value_start:value_end] = [f"{indent}{attr} = _red_probe_boom()"]
    return helper + "\n".join(lines) + "\n"


def _check_test_ref(ref: str) -> str | None:
    """校验测试 node id 的文件与用例函数真实存在。

    Args:
        ref: ``tests/unit/test_x.py::test_y`` 形态的 node id。

    Returns:
        错误说明；合法时返回 None。
    """
    rel_path, sep, leaf = ref.partition("::")
    if not sep or not leaf:
        return f"node id 缺少 ::<用例名>: {ref}"
    path = BACKEND_SRC / rel_path
    if not path.is_file():
        return f"测试文件不存在: {rel_path}"
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError as exc:
        return f"测试文件语法错误: {rel_path}: {exc}"
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
    leaf_head = leaf.split("::")[0]
    if leaf_head not in names:
        return f"用例不存在: {ref}"
    return None


def _validate_probe(probe: ProbeSpec) -> list[str]:
    """校验单条 probe 的符号与测试引用。

    Args:
        probe: 探针登记。

    Returns:
        错误说明列表（空 = 合法）。
    """
    problems: list[str] = []
    path = _module_path(probe.module)
    if not path.is_file():
        problems.append(f"{probe.name}: 模块文件不存在 {path}")
        return problems
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError as exc:
        problems.append(f"{probe.name}: 模块语法错误 {exc}")
        return problems
    if _find_top_level(tree, probe.attr) is None:
        problems.append(f"{probe.name}: 模块顶层未找到符号 {probe.attr}")
    if not probe.tests:
        problems.append(f"{probe.name}: 未登记测试 node id")
    for ref in probe.tests:
        problem = _check_test_ref(ref)
        if problem:
            problems.append(f"{probe.name}: {problem}")
    return problems


def _run_probe(probe: ProbeSpec) -> ProbeOutcome:
    """在临时副本中执行单条探针。

    Args:
        probe: 探针登记。

    Returns:
        执行结果（副本内至少一条用例失败即成立）。

    Raises:
        SystemExit: 符号替换失败（退出码 2）。
    """
    target = _module_path(probe.module)
    message = f"红探针触发: {probe.symbol}"
    try:
        stubbed = _stub_source(target.read_text(encoding="utf-8"), probe.attr, message)
    except ValueError as exc:
        print(f"[red_probe] {probe.name}: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc

    with tempfile.TemporaryDirectory(prefix="red-probe-") as raw_tmp:
        tmp_root = Path(raw_tmp)
        shutil.copytree(
            BACKEND_SRC,
            tmp_root / "src",
            ignore=shutil.ignore_patterns(*_COPY_IGNORES),
        )
        shutil.copy2(REPO_ROOT / "backend" / "pyproject.toml", tmp_root / "pyproject.toml")
        relative = target.relative_to(BACKEND_SRC)
        (tmp_root / "src" / relative).write_text(stubbed, encoding="utf-8")

        args = [
            sys.executable,
            "-m",
            "pytest",
            *[
                f"{tmp_root / 'src' / ref.partition('::')[0]}::{ref.partition('::')[2]}"
                for ref in probe.tests
            ],
            "-q",
            "--tb=no",
            "-p",
            "no:cacheprovider",
        ]
        env = dict(os.environ)
        env.pop("PYTEST_ADDOPTS", None)
        env["PYTHONPATH"] = str(tmp_root / "src")
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["COVERAGE_FILE"] = str(tmp_root / ".coverage")
        completed = subprocess.run(
            args,
            cwd=tmp_root,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        output = f"{completed.stdout}\n{completed.stderr}"
        failed = sum(int(match) for match in _FAILED_RE.findall(output))
        errors = sum(int(match) for match in _ERROR_RE.findall(output))
        passed = sum(int(match) for match in _PASSED_RE.findall(output))
        return ProbeOutcome(
            probe=probe,
            exit_code=completed.returncode,
            failed=failed,
            errors=errors,
            passed=passed,
        )


def _probe_covered_files(manifest: RedManifest) -> set[str]:
    """返回被 probe 覆盖的源文件集合（相对 ``backend/src``）。"""
    return {_relative_source_path(probe.module) for probe in manifest.probes}


def _iter_source_files() -> list[str]:
    """枚举生产源文件（相对 ``backend/src``，排序稳定）。"""
    results: set[str] = set()
    for root_name in _SOURCE_ROOTS:
        root = BACKEND_SRC / root_name
        if not root.is_dir():
            continue
        for path in root.rglob("*.py"):
            parts = path.relative_to(BACKEND_SRC).parts
            if "__pycache__" in parts:
                continue
            if any(part == "tests" for part in parts[:-1]):
                continue
            results.add(path.relative_to(BACKEND_SRC).as_posix())
    return sorted(results)


def _run_gate(manifest: RedManifest) -> int:
    """门禁模式：校验清单完整性并报告未覆盖的新源文件。

    Args:
        manifest: 红探针清单。

    Returns:
        进程退出码：0 通过 / 1 红 / 2 配置错误。
    """
    problems: list[str] = []
    for probe in manifest.probes:
        problems.extend(_validate_probe(probe))
    if problems:
        print("[red_probe] 探针登记无效:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 2

    sources = _iter_source_files()
    grandfathered = set(manifest.grandfathered)
    covered = _probe_covered_files(manifest)
    uncovered = [path for path in sources if path not in grandfathered and path not in covered]

    stale = sorted(grandfathered - set(sources) - covered)
    print(
        f"[red_probe] 源文件 {len(sources)} 个；冻结基线 {len(grandfathered)} 个；"
        f"探针 {len(manifest.probes)} 条（覆盖 {len(covered)} 个文件）"
    )
    if stale:
        print(
            f"[red_probe] 基线陈旧条目 {len(stale)} 个（文件已删除或不符——"
            "报告不阻断；可用 --prune 打印精简清单）:"
        )
        for path in stale[:10]:
            print(f"  - {path}")
    if uncovered:
        print(
            "[red_probe] 未覆盖的新源文件（新增实现必须登记红探针，或按评审加入冻结基线）:",
            file=sys.stderr,
        )
        for path in uncovered:
            print(f"  - {path}", file=sys.stderr)
        return 1
    print("[red_probe] 门禁通过（全部源文件在冻结基线或探针覆盖内）")
    return 0


def _run_symbol(manifest: RedManifest, symbol: str) -> int:
    """执行指定符号对应的全部探针。

    Args:
        manifest: 红探针清单。
        symbol: ``module:attr`` 形态的符号。

    Returns:
        进程退出码：0 全部变红 / 1 存在未变红 / 2 符号未登记。
    """
    matched = [probe for probe in manifest.probes if probe.symbol == symbol]
    if not matched:
        print(f"[red_probe] 符号未在清单登记: {symbol}", file=sys.stderr)
        return 2
    failed_any = False
    for probe in matched:
        outcome = _run_probe(probe)
        verdict = "RED" if outcome.went_red else "GREEN（探针失效）"
        print(
            f"[red_probe] {probe.name} [{probe.symbol}] -> {verdict} "
            f"(exit={outcome.exit_code}, failed={outcome.failed}, "
            f"errors={outcome.errors}, passed={outcome.passed})"
        )
        failed_any = failed_any or not outcome.went_red
    return 1 if failed_any else 0


def _run_all(manifest: RedManifest) -> int:
    """执行清单内全部探针（逐条独立副本，互不影响）。

    Args:
        manifest: 红探针清单。

    Returns:
        进程退出码：0 全部变红 / 1 存在未变红。
    """
    broken = False
    for probe in manifest.probes:
        outcome = _run_probe(probe)
        verdict = "RED" if outcome.went_red else "GREEN（探针失效）"
        print(
            f"[red_probe] {probe.name} [{probe.symbol}] -> {verdict} "
            f"(exit={outcome.exit_code}, failed={outcome.failed}, "
            f"errors={outcome.errors}, passed={outcome.passed})"
        )
        broken = broken or not outcome.went_red
    return 1 if broken else 0


def main(argv: list[str] | None = None) -> int:
    """CLI 入口。

    Args:
        argv: 命令行参数（None 取 sys.argv[1:]）。

    Returns:
        进程退出码（见模块文档）。

    Raises:
        SystemExit: argparse 参数错误或清单配置错误。
    """
    parser = argparse.ArgumentParser(description="红探针门（源码体替换 + 测试变红验证）")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--gate", action="store_true", help="门禁：校验源文件覆盖清单")
    mode.add_argument("--symbol", help="执行指定 module:attr 对应的探针")
    mode.add_argument("--all", action="store_true", help="执行清单内全部探针")
    parser.add_argument(
        "--prune",
        action="store_true",
        help="门禁模式附带打印可精简的基线条目清单",
    )
    args = parser.parse_args(argv)

    manifest = _load_manifest(MANIFEST_PATH)
    if args.symbol:
        return _run_symbol(manifest, args.symbol)
    if args.all:
        return _run_all(manifest)
    exit_code = _run_gate(manifest)
    if args.prune:
        sources = set(_iter_source_files())
        covered = _probe_covered_files(manifest)
        keep = sorted(sources | covered)
        print("\n--prune 精简后的 grandfathered（替换清单对应数组）:")
        for path in keep:
            print(f'    "{path}",')
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())

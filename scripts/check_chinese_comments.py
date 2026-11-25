#!/usr/bin/env python3
"""中文注释门禁（Chinese comment gate）——扫描非测试源码的 docstring 合规性。

扫描范围（当前仓库树：backend + 沙盒树 + 根级 scripts/ + 前端源码——新树
落地时在本脚本 ``_TREE_ROOTS`` 登记）：

    - backend/src（排除 src/tests/）
    - docker 沙盒树 src + server.py
    - 仓库根 scripts/（工程级脚本）
    - frontend/src（TypeScript / JavaScript——只检查注释内的编号残留；
      前端没有 Python 式 docstring 契约，因此不套用存在性与中文要求）

测试文件豁免（约定不同）：路径含 ``tests/`` 的文件、``test_*.py`` /
``*_test.py`` / ``conftest.py``。

规则（务实阈值，避免误报风暴）：

    1. **存在性**：每个公开模块 / 公开类 / 公开函数（模块级或类方法，
       名称不以 ``_`` 开头）必须有 docstring。
    2. **中文**：docstring 必须含至少一个 CJK 统一表意文字（U+3400-U+9FFF）。
    3. **Google 风格要素**（AST 启发式，报告但不计违规）：
        - 参数数 > 1（不含 self/cls）的函数 → 建议补 ``Args:`` 段；
        - 返回注解非 ``None`` 的函数 → 建议补 ``Returns:`` 段；
        - 函数体含直接 ``raise`` 语句 → 建议补 ``Raises:`` 段。
       段落缺失只打印 hints（不计入退出码）——历史存量较大，硬规则
       现阶段只锁存在性 + 中文两条，段落要素作为后续增强项逐步补齐；
       启发式不确定时
       （生成器 / 纯 stub / 装饰器剥离了签名）记入 skips。
    4. **无切片 / 拍板编号残留**（硬规则，命中即红）：生产源码的注释与
       docstring 不得把批次编号当语义（如 ``R2a-4`` / ``P6=B`` / ``D7-14``
       / ``chunk 1``）——编号是台账（docs/LEDGER.md）的坐标，源码注释须
       自证书写它讲的事实。检查面 = 注释 token + 各级 docstring；字符串
       字面量不在检查面内（键名 / 错误文案可含编号）。

退出码由规则 1/2/4 的违规驱动：无违规 0；有违规 1（明细 + 计数摘要打印到
stdout）。规则 3 的段落缺口以 hints 形式并列报告，不影响退出码。

用法::

    python scripts/check_chinese_comments.py            # 全树扫描（门禁）
    python scripts/check_chinese_comments.py --tree backend   # 仅 backend
"""

from __future__ import annotations

import argparse
import ast
import io
import re
import sys
import tokenize
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent

# 各树扫描根（相对仓库根）。
_TREE_ROOTS: dict[str, list[str]] = {
    "backend": ["backend/src"],
    "tool-sandbox": [
        "docker/tool-sandbox-agent/src",
        "docker/tool-sandbox-agent/server.py",
    ],
    "browser-sidecar": [
        "docker/browser-sidecar/src",
        "docker/browser-sidecar/server.py",
    ],
    "scripts": ["scripts"],
    "frontend": ["frontend/src"],
}

# 各树扫描的文件后缀；未登记的树按 Python 源码处理。
_TREE_SUFFIXES: dict[str, tuple[str, ...]] = {
    "frontend": (".ts", ".tsx", ".js", ".mjs"),
}

#: 前端源码后缀（无 Python AST——只解析注释面）。
_FRONTEND_SUFFIXES: frozenset[str] = frozenset(_TREE_SUFFIXES["frontend"])

# 模块 docstring 同样要求中文；以下目录/文件模式视为测试，整树豁免。
_TEST_MARKERS = ("tests", "testing")
_TEST_FILE_NAMES = ("conftest.py",)


def _is_test_file(path: Path) -> bool:
    if path.name in _TEST_FILE_NAMES or path.name.startswith("test_") or path.name.endswith("_test.py"):
        return True
    if path.name.endswith((".test.ts", ".test.tsx", ".spec.ts", ".spec.tsx")):
        return True
    return any(part in _TEST_MARKERS or part == "test" for part in path.parts[:-1])


def _has_cjk(text: str) -> bool:
    """docstring 是否含 CJK 统一表意文字（含扩展 A 区）。"""
    return any("\u3400" <= ch <= "\u9fff" for ch in text)


#: 规则 4 的检查面豁免：本文件 docstring 以编号本身作示例，不构成语义残留。
_METADATA_EXEMPT_FILES: frozenset[str] = frozenset({"scripts/check_chinese_comments.py"})

#: 切片 / 拍板编号残留模式（规则 4）：批次码 / 拍板等式 / 引擎档 / 切片号。
#: 前置字符守卫排除「MP3」「UUID4」「chunk_index」这类技术词——只认真正
#: 作为独立记号出现的编号。
_METADATA_CODE_RE = re.compile(
    r"(?<![A-Za-z0-9])R\d[a-z]?(?:-\d+)?(?![A-Za-z0-9])"
    r"|(?<![A-Za-z0-9])G\d+(?![A-Za-z0-9])"
    r"|(?<![A-Za-z0-9])D\d+(?:-\d+)?(?![A-Za-z0-9])"
    r"|(?<![A-Za-z0-9])P\d+(?![A-Za-z0-9])"
    r"|(?<![A-Za-z0-9])chunk \d"
)


def _iter_annotation_texts(path: Path, source: str, tree: ast.Module) -> Iterator[tuple[int, str]]:
    """枚举文件内注释与 docstring 文本（规则 4 检查面）。

    Args:
        path: 源文件路径（注释 token 解析失败时的兜底读取）。
        source: 已读取的源码文本。
        tree: 已解析的 AST（各级 docstring 来源）。

    Yields:
        ``(行号, 文本)`` 二元组；注释按 token 起始行给出，docstring 按
        所属节点起始行给出。
    """
    if (module_doc := ast.get_docstring(tree)) is not None:
        yield 1, module_doc
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            doc = ast.get_docstring(node)
            if doc is not None:
                yield node.lineno, doc
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    except tokenize.TokenError:  # 不完整文件：注释面放弃，docstring 面已覆盖
        return
    for token in tokens:
        if token.type == tokenize.COMMENT:
            yield token.start[0], token.string


def _iter_frontend_comments(source: str) -> Iterator[tuple[int, str]]:
    """枚举 TypeScript / JavaScript 源码内的行注释与块注释文本。

    以最小词法状态机跳过字符串与模板字符串，只产出真正的注释内容——
    字符串字面量（错误文案 / 键名）不在编号残留检查面内。正则字面量按
    普通代码处理，其内容极少包含注释起始符，不影响本规则。

    Args:
        source: 前端源码文本。

    Yields:
        ``(起始行号, 注释文本)`` 二元组。
    """
    index = 0
    line = 1
    state = "code"
    start_line = 0
    buffer: list[str] = []
    length = len(source)
    while index < length:
        char = source[index]
        if state == "code":
            if char == "/" and index + 1 < length and source[index + 1] == "/":
                state = "line"
                start_line = line
                buffer = []
                index += 2
                continue
            if char == "/" and index + 1 < length and source[index + 1] == "*":
                state = "block"
                start_line = line
                buffer = []
                index += 2
                continue
            if char in ("'", '"', "`"):
                state = char
            if char == "\n":
                line += 1
            index += 1
            continue
        if state == "line":
            if char == "\n":
                yield start_line, "".join(buffer)
                state = "code"
                line += 1
                index += 1
                continue
            buffer.append(char)
            index += 1
            continue
        if state == "block":
            if char == "*" and index + 1 < length and source[index + 1] == "/":
                yield start_line, "".join(buffer)
                state = "code"
                index += 2
                continue
            buffer.append(char)
            if char == "\n":
                line += 1
            index += 1
            continue
        # 字符串 / 模板字符串：转义跳过；普通字符串遇换行视为未闭合并复位。
        if char == "\\":
            index += 2
            continue
        if char == state:
            state = "code"
            index += 1
            continue
        if char == "\n":
            line += 1
            if state != "`":
                state = "code"
        index += 1


def _report_metadata_matches(
    path: Path, texts: Iterator[tuple[int, str]], stats: _ScanStats
) -> None:
    """对注释 / docstring 文本应用规则 4 并记录命中。

    Args:
        path: 源文件路径。
        texts: ``(起始行号, 文本)`` 迭代器。
        stats: 扫描计数（命中即记违规）。
    """
    for lineno, text in texts:
        for match in _METADATA_CODE_RE.finditer(text):
            hit_line = lineno + text[: match.start()].count("\n")
            stats.add_violation(
                path,
                hit_line,
                "comment-metadata",
                match.group(0).strip(),
                "残留切片 / 拍板编号（改为自证语义，编号归 docs/LEDGER.md）",
            )


def _check_metadata_codes(
    path: Path, source: str, tree: ast.Module, stats: _ScanStats
) -> None:
    """规则 4：注释 / docstring 内不得残留切片与拍板编号。

    Args:
        path: 源文件路径。
        source: 源码文本。
        tree: 已解析 AST。
        stats: 扫描计数（命中即记违规）。
    """
    if path.relative_to(_REPO_ROOT).as_posix() in _METADATA_EXEMPT_FILES:
        return
    _report_metadata_matches(path, _iter_annotation_texts(path, source, tree), stats)


def _check_frontend_file(path: Path, stats: _ScanStats) -> None:
    """前端源码检查（只扫描注释文本内的编号残留）。

    Args:
        path: 前端源文件路径。
        stats: 扫描计数（命中即记违规）。
    """
    stats.files += 1
    try:
        source = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        stats.add_violation(path, 0, "module", path.name, "源码不是 UTF-8")
        return
    _report_metadata_matches(path, _iter_frontend_comments(source), stats)


def _iter_tree_files(tree: str) -> Iterator[Path]:
    suffixes = _TREE_SUFFIXES.get(tree, (".py",))
    for root in _TREE_ROOTS[tree]:
        p = _REPO_ROOT / root
        if p.is_file():
            if p.suffix in suffixes and not _is_test_file(p):
                yield p
            continue
        for f in sorted(p.rglob("*")):
            if f.is_file() and f.suffix in suffixes and not _is_test_file(f):
                yield f


@dataclass
class _ScanStats:
    """扫描计数（违规 + 段落提示 + 启发式跳过）。"""

    files: int = 0
    modules: int = 0
    classes: int = 0
    functions: int = 0
    violations: list[str] = field(default_factory=list)
    hints: list[str] = field(default_factory=list)
    skips: list[str] = field(default_factory=list)

    def add_violation(self, path: Path, lineno: int, kind: str, name: str, detail: str) -> None:
        rel = path.relative_to(_REPO_ROOT)
        self.violations.append(f"{rel}:{lineno}: {kind} `{name}` {detail}")

    def add_hint(self, path: Path, lineno: int, name: str, detail: str) -> None:
        rel = path.relative_to(_REPO_ROOT)
        self.hints.append(f"{rel}:{lineno}: `{name}` {detail}")

    def add_skip(self, path: Path, name: str, reason: str) -> None:
        rel = path.relative_to(_REPO_ROOT)
        self.skips.append(f"{rel}: `{name}` {reason}")


def _sections(doc: str) -> set[str]:
    """解析 docstring 中的 Google 风格段名（Args/Returns/Raises/Yields）。"""
    found: set[str] = set()
    for line in doc.splitlines():
        stripped = line.strip()
        for section in ("Args", "Returns", "Raises", "Yields"):
            if stripped == f"{section}:":
                found.add(section)
    return found


def _direct_raises(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """函数体（不含嵌套函数/lambda）是否有直接 raise 语句。"""

    class _Visitor(ast.NodeVisitor):
        has_raise = False

        def visit_Raise(self, n: ast.Raise) -> None:  # noqa: N802
            self.has_raise = True

        def visit_FunctionDef(self, n: ast.FunctionDef) -> None:  # noqa: N802
            return  # 嵌套函数不深入

        def visit_AsyncFunctionDef(self, n: ast.AsyncFunctionDef) -> None:  # noqa: N802
            return  # 嵌套函数不深入

        def visit_Lambda(self, n: ast.Lambda) -> None:  # noqa: N802
            return

    v = _Visitor()
    for stmt in node.body:
        v.visit(stmt)
    return v.has_raise


def _is_annotation_none(annotation: ast.expr | None) -> bool:
    if annotation is None:
        return True
    src = ast.unparse(annotation)
    return src == "None"


def _check_function(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    path: Path,
    stats: _ScanStats,
    kind: str,
) -> None:
    name = node.name
    if name.startswith("_"):
        return
    stats.functions += 1

    doc = ast.get_docstring(node)
    if doc is None:
        stats.add_violation(path, node.lineno, kind, name, "缺少 docstring")
    elif not _has_cjk(doc):
        stats.add_violation(path, node.lineno, kind, name, "docstring 不含中文")

    if doc is None:
        return

    # 装饰器剥离签名 / 纯 stub 的启发式跳过
    decorators = {ast.unparse(d).split("(")[0] for d in node.decorator_list}
    if "property" in decorators or decorators & {"overload", "abstractmethod"}:
        if not decorators & {"abstractmethod"}:
            stats.add_skip(path, name, "property/overload 装饰器，段落检查跳过")
        return

    body_stmts = node.body
    if len(body_stmts) == 1 and isinstance(body_stmts[0], ast.Raise):
        stats.add_skip(path, name, "纯 NotImplementedError stub，段落检查跳过")
        return

    has_yield = any(
        isinstance(n, ast.Yield | ast.YieldFrom)
        for n in ast.walk(node)
        if not isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda)
    )
    sections = _sections(doc)
    params = [
        a.arg
        for a in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs)
        if a.arg not in ("self", "cls")
    ]
    if node.args.vararg:
        params.append(f"*{node.args.vararg.arg}")
    if node.args.kwarg:
        params.append(f"**{node.args.kwarg.arg}")

    need_args = len(params) > 1
    if need_args and "Args" not in sections:
        stats.add_hint(path, node.lineno, name, f"参数 {len(params)} 个但缺 Args 段")

    returns_doc_required = not _is_annotation_none(node.returns)
    if returns_doc_required and has_yield:
        stats.add_skip(path, name, "生成器函数，Returns 检查跳过（应记 Yields）")
    elif returns_doc_required and "Returns" not in sections:
        stats.add_hint(path, node.lineno, name, "返回注解非 None 但缺 Returns 段")

    if _direct_raises(node) and "Raises" not in sections:
        stats.add_hint(path, node.lineno, name, "函数体含 raise 但缺 Raises 段")


def _check_class(node: ast.ClassDef, path: Path, stats: _ScanStats) -> None:
    if node.name.startswith("_"):
        return
    stats.classes += 1
    doc = ast.get_docstring(node)
    if doc is None:
        stats.add_violation(path, node.lineno, "class", node.name, "缺少 docstring")
    elif not _has_cjk(doc):
        stats.add_violation(path, node.lineno, "class", node.name, "docstring 不含中文")
    for child in node.body:
        if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
            _check_function(child, path, stats, "method")


def _check_module(path: Path, stats: _ScanStats) -> None:
    if path.suffix in _FRONTEND_SUFFIXES:
        _check_frontend_file(path, stats)
        return
    stats.files += 1
    try:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as exc:
        stats.add_violation(path, exc.lineno or 0, "module", path.name, f"语法解析失败: {exc.msg}")
        return
    stats.modules += 1
    doc = ast.get_docstring(tree)
    if doc is None:
        stats.add_violation(path, 1, "module", path.name, "缺少模块 docstring")
    elif not _has_cjk(doc):
        stats.add_violation(path, 1, "module", path.name, "模块 docstring 不含中文")
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            _check_function(node, path, stats, "function")
        elif isinstance(node, ast.ClassDef):
            _check_class(node, path, stats)
    _check_metadata_codes(path, source, tree, stats)


def main(argv: list[str] | None = None) -> int:
    """门禁入口。

    Args:
        argv: 命令行参数（默认 sys.argv[1:]）。

    Returns:
        0 表示无违规；1 表示存在违规（明细已打印）。

    Raises:
        SystemExit: ``--tree`` 传入未知树名时抛出。
    """
    parser = argparse.ArgumentParser(description="中文注释门禁（docstring 合规性扫描）")
    parser.add_argument(
        "--tree",
        choices=sorted(_TREE_ROOTS),
        action="append",
        help="仅扫描指定树（可重复）；缺省扫描全部树",
    )
    args = parser.parse_args(argv)

    stats = _ScanStats()
    trees = args.tree or sorted(_TREE_ROOTS)
    for tree in trees:
        for path in _iter_tree_files(tree):
            _check_module(path, stats)

    for line in stats.violations:
        print(line)
    print(
        f"\n[check_chinese_comments] files={stats.files} "
        f"modules={stats.modules} classes={stats.classes} functions={stats.functions} "
        f"violations={len(stats.violations)} hints={len(stats.hints)} skips={len(stats.skips)}"
    )
    if stats.skips:
        print("[check_chinese_comments] skips（启发式未强制，不计违规）:")
        for line in stats.skips:
            print(f"  {line}")
    if stats.hints:
        print("[check_chinese_comments] hints（Google 段落缺口，报告不拦截）:")
        for line in stats.hints:
            print(f"  {line}")
    return 1 if stats.violations else 0


if __name__ == "__main__":
    sys.exit(main())

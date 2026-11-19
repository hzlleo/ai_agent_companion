#!/usr/bin/env python3
"""prose-pin 启发式报告——测试断言里的成句文案是否只是实现文本的回声。

prose pin（文案钉死）指测试断言的是实现源码里的某个字符串字面量，而不是
可观察行为：实现改一个字测试就红，实现逻辑烂掉但文案没变测试仍绿。本脚本
做纯启发式盘点（**报告模式，恒退出 0**）：

    - 收集每个测试文件 ``assert`` 子树里的字符串字面量（长度 ≥ 8，过滤纯
      标点）；
    - 若同一字面量出现在 ``packages/harness/**`` 的实现源码中，按形态分桶：
      ``field_or_code``（标识符 / 字典键 / UPPER_SNAKE / 带连字符模型 ID——
      本质是协议字段名，改它等于改契约，不算文案钉死）、``prose``（含空白
      或 CJK 的自然语言；代码 token 的补集与 prose 完全等价，故只有两桶）；
    - **「疑似 prose pin」只统计 prose 桶**，Top-N 也 prose 桶优先——把字段名
      混进总数会让清单虚高、真正的文案断言被淹没；
    - 统计「纯结构断言」占比：一个测试函数内的断言只由 ``status_code``
      属性、``in`` / 比较、``len(...)`` 组成，视为结构断言（这类断言不随
      文案漂移，是 prose pin 的反面）。

只读源码，不改仓库；退出码恒 0，供根门禁链与人工审查参考。

用法::

    python scripts/prose_pin_check.py            # Top-15
    python scripts/prose_pin_check.py --top 30   # 自定义条数

Exit:
    0 = 恒成功（报告模式）；2 = 扫描根缺失。
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

#: 仓库根（本脚本位于 <repo>/scripts/）。
REPO_ROOT = Path(__file__).resolve().parents[1]

#: 测试扫描根。
TESTS_ROOT = REPO_ROOT / "backend" / "src" / "tests"

#: 实现源码扫描根（被测面）。
IMPLEMENTATION_ROOT = REPO_ROOT / "backend" / "src" / "packages" / "harness"

#: 参与比对的最小字面量长度（低于此长度的短串命中噪声过大）。
_MIN_LITERAL_CHARS = 8

#: 代码 / 协议字段形态：不含空白与 CJK 的单个 token（标识符、UPPER_SNAKE、
#: 字典键、带连字符模型 ID、MIME / 路径 / SQL 占位等）。单 token 不承载
#: 自然语言，改它属于改契约而非改文案。
_CODE_TOKEN_RE = re.compile(r"^[^\s\u4e00-\u9fff]+$")

#: 分桶名（报告与 Top-N 排序用）。
_BUCKETS = ("prose", "field_or_code")
_STRUCTURAL_NODES = (
    ast.Assert,
    ast.Compare,
    ast.BoolOp,
    ast.UnaryOp,
    ast.Name,
    ast.Constant,
    ast.Tuple,
    ast.List,
    ast.Set,
    ast.Expr,
)


@dataclass
class ProsePinStats:
    """扫描汇总。

    Attributes:
        literals: 字面量 → 命中测试位置列表。
        tests_with_asserts: 含断言的测试函数数。
        structural_tests: 纯结构断言的测试函数数。
        scanned_files: 扫描的测试文件数。
    """

    literals: dict[str, list[str]] = field(default_factory=dict)
    tests_with_asserts: int = 0
    structural_tests: int = 0
    scanned_files: int = 0


def _iter_test_files() -> list[Path]:
    """枚举测试源文件（test_*.py，跳过缓存目录）。"""
    if not TESTS_ROOT.is_dir():
        return []
    return sorted(
        path
        for path in TESTS_ROOT.rglob("test_*.py")
        if "__pycache__" not in path.parts
    )


def _load_implementation_text() -> str:
    """拼接 harness 实现源码全文（排除测试文件）。"""
    chunks: list[str] = []
    if not IMPLEMENTATION_ROOT.is_dir():
        return ""
    for path in sorted(IMPLEMENTATION_ROOT.rglob("*.py")):
        parts = path.relative_to(IMPLEMENTATION_ROOT).parts
        if "__pycache__" in parts or "tests" in parts[:-1]:
            continue
        if path.name.startswith("test_"):
            continue
        chunks.append(path.read_text(encoding="utf-8", errors="replace"))
    return "\n".join(chunks)


def _is_meaningful_literal(text: str) -> bool:
    """判断字面量是否值得进入比对（长度与内容过滤）。"""
    stripped = text.strip()
    if len(stripped) < _MIN_LITERAL_CHARS:
        return False
    return any(ch.isalnum() or "\u4e00" <= ch <= "\u9fff" for ch in stripped)


def _is_structural_assert(node: ast.Assert) -> bool:
    """判断单个断言是否纯结构（status_code / in / len 面）。"""
    for child in ast.walk(node.test):
        if isinstance(child, ast.Attribute):
            if child.attr != "status_code":
                return False
            continue
        if isinstance(child, ast.Call):
            if not (isinstance(child.func, ast.Name) and child.func.id == "len"):
                return False
            continue
        if isinstance(child, _STRUCTURAL_NODES):
            continue
        if isinstance(child, (ast.Load, ast.And, ast.Or, ast.Not, ast.In, ast.NotIn)):
            continue
        # Compare 的操作符 / 关键字等辅助节点不在 _STRUCTURAL_NODES 内。
        if isinstance(child, (ast.cmpop, ast.operator, ast.unaryop, ast.boolop)):
            continue
        return False
    return True


def _collect_file(
    path: Path, implementation_text: str, stats: ProsePinStats
) -> None:
    """扫描单文件，填充汇总统计（就地修改）。

    Args:
        path: 测试文件。
        implementation_text: harness 实现源码全文。
        stats: 汇总容器。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    rel = path.relative_to(REPO_ROOT).as_posix()
    stats.scanned_files += 1
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not node.name.startswith("test_"):
            continue
        asserts = [child for child in ast.walk(node) if isinstance(child, ast.Assert)]
        if not asserts:
            continue
        stats.tests_with_asserts += 1
        if all(_is_structural_assert(item) for item in asserts):
            stats.structural_tests += 1
        for assert_node in asserts:
            for child in ast.walk(assert_node):
                if not isinstance(child, ast.Constant) or not isinstance(child.value, str):
                    continue
                if not _is_meaningful_literal(child.value):
                    continue
                literal = child.value.strip()
                if literal not in implementation_text:
                    continue
                location = f"{rel}:{child.lineno}"
                stats.literals.setdefault(literal, []).append(location)


def _classify_literal(literal: str) -> str:
    """把命中文案分为 field_or_code / prose 两桶。

    ``_CODE_TOKEN_RE`` 的补集（含空白或 CJK）与 prose 桶完全等价，因此
    不存在第三桶：不相交的判定语句对会留下不可达分支。

    Args:
        literal: 命中实现源码的测试字面量（已 strip）。

    Returns:
        ``field_or_code`` 表示不含空白与 CJK 的单个代码 token（标识符 /
        字典键 / UPPER_SNAKE / 带连字符模型 ID / MIME / SQL 片段）；
        ``prose`` 表示含空白或 CJK 的自然语言文案。
    """
    if _CODE_TOKEN_RE.fullmatch(literal):
        return "field_or_code"
    return "prose"


def _report(stats: ProsePinStats, top: int) -> None:
    """打印分桶计数、prose 桶优先的 Top-N 与结构断言占比。

    Args:
        stats: 扫描汇总。
        top: Top-N 条数。
    """
    ranked = sorted(
        stats.literals.items(), key=lambda item: (-len(item[1]), item[0])
    )
    buckets: dict[str, list[tuple[str, list[str]]]] = {name: [] for name in _BUCKETS}
    for literal, locations in ranked:
        buckets[_classify_literal(literal)].append((literal, locations))
    ratio = (
        stats.structural_tests / stats.tests_with_asserts * 100
        if stats.tests_with_asserts
        else 0.0
    )
    counts = ", ".join(f"{name}={len(buckets[name])}" for name in _BUCKETS)
    print(
        f"[prose_pin_check] 测试文件 {stats.scanned_files} 份；含断言用例 "
        f"{stats.tests_with_asserts} 个；命中实现源字面量 {len(ranked)} 个"
    )
    print(
        f"[prose_pin_check] 疑似 prose pin（prose 桶）{len(buckets['prose'])} 个；"
        f"分桶 {counts}"
    )
    print(
        f"[prose_pin_check] 纯结构断言用例 {stats.structural_tests} 个"
        f"（占比 {ratio:.1f}%——越高越不依赖文案）"
    )
    ordered = [
        (name, literal, locations)
        for name in _BUCKETS
        for literal, locations in buckets[name]
    ]
    for name, literal, locations in ordered[:top]:
        print(f"  - [{name}] x{len(locations)} {literal!r} @ {locations[0]}")


def main(argv: list[str] | None = None) -> int:
    """CLI 入口（报告模式）。

    Args:
        argv: 命令行参数（None 取 sys.argv[1:]）。

    Returns:
        0 = 扫描完成；2 = 扫描根缺失。
    """
    parser = argparse.ArgumentParser(description="prose-pin 启发式报告（不阻断）")
    parser.add_argument("--top", type=int, default=15, help="输出条数（默认 15）")
    args = parser.parse_args(argv)

    if not TESTS_ROOT.is_dir() or not IMPLEMENTATION_ROOT.is_dir():
        print(
            f"[prose_pin_check] 扫描根缺失: {TESTS_ROOT} / {IMPLEMENTATION_ROOT}",
            file=sys.stderr,
        )
        return 2

    implementation_text = _load_implementation_text()
    stats = ProsePinStats()
    for path in _iter_test_files():
        try:
            _collect_file(path, implementation_text, stats)
        except SyntaxError as exc:
            print(f"[prose_pin_check] 语法错误（跳过）: {path}: {exc}", file=sys.stderr)
    _report(stats, max(1, args.top))
    print("[prose_pin_check] 报告完成（不阻断；exit 0）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

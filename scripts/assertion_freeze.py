#!/usr/bin/env python3
"""断言侵蚀棘轮（assertion freeze）——既有单测断言只降不升。

「修测试而不是修实现」最隐蔽的形态是删断言 / 把 ``assert`` 换成 ``print``
或宽松存在性检查。本门禁对 ``backend/src/tests/unit/*.py`` 逐文件记录：

    - ``assert`` 语句数；
    - ``pytest.raises`` 上下文数；
    - 断言 AST 摘要（全部 ``assert`` 测试表达式与 ``pytest.raises`` 调用
      参数的规范化 dump 的 sha256）。

gate 模式下**计数下降即红**（新增用例请在评审后 ``--update`` 抬高基线）；
**基线外新增测试文件同样判红**——新文件必须先经评审，再由 ``--update``
并入基线，避免整文件新增绕过逐文件棘轮；**基线内文件整删同样判红**——
否则整文件删除等于把该文件的全部断言清零却不留计数下降痕迹；合法整删
必须评审后 ``--update`` 重建基线（与计数下降同口径）。AST 摘要变化只报告
（等量重写合法，但需要审查者看见）。

用法::

    python scripts/assertion_freeze.py --enforce   # 门禁（计数下降 / 整删 / 基线外新文件 exit 1）
    python scripts/assertion_freeze.py             # 报告（对照基线但不阻断）
    python scripts/assertion_freeze.py --update    # 重建基线（人工审阅后提交）

Exit:
    0 = 不降 / 报告 / 更新成功；1 = 存在计数下降或基线内文件缺失；
    2 = 基线缺失或损坏。
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import sys
from dataclasses import dataclass
from pathlib import Path

#: 仓库根（本脚本位于 <repo>/scripts/）。
REPO_ROOT = Path(__file__).resolve().parents[1]

#: 扫描目录（单元测试顶层文件）。
UNIT_TESTS_ROOT = REPO_ROOT / "backend" / "src" / "tests" / "unit"

#: 基线文件（与其它棘轮基线同目录）。
BASELINE_PATH = Path(__file__).resolve().parent / ".assertion_baseline.json"

#: 基线 schema 版本（口径变化时递增）。
_BASELINE_VERSION = 1


@dataclass(frozen=True)
class AssertionStats:
    """单文件断言统计。

    Attributes:
        asserts: ``assert`` 语句数。
        raises: ``pytest.raises`` 调用数。
        digest: 断言 AST 摘要（sha256）。
    """

    asserts: int
    raises: int
    digest: str


def _is_raises_call(node: ast.AST) -> bool:
    """判断调用是否 ``pytest.raises(...)`` / ``raises(...)``。"""
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Attribute) and func.attr == "raises":
        return True
    return isinstance(func, ast.Name) and func.id == "raises"


def _canonical_dump(node: ast.AST) -> str:
    """返回节点规范化 dump（排除位置属性，跨重排稳定）。"""
    return ast.dump(node, annotate_fields=True, include_attributes=False)


def analyze_file(path: Path) -> AssertionStats:
    """统计单文件的断言数量与 AST 摘要。

    Args:
        path: 测试文件路径。

    Returns:
        统计结果。

    Raises:
        SyntaxError: 文件语法错误（由调用方统一报告）。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    asserts: list[ast.Assert] = []
    raises_calls: list[ast.Call] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assert):
            asserts.append(node)
        elif _is_raises_call(node):
            raises_calls.append(node)
    parts: list[str] = []
    for node in asserts:
        parts.append("assert:" + _canonical_dump(node.test))
    for node in raises_calls:
        parts.append("raises:" + _canonical_dump(node))
    digest = hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()
    return AssertionStats(asserts=len(asserts), raises=len(raises_calls), digest=digest)


def collect_stats() -> tuple[dict[str, AssertionStats], list[str]]:
    """扫描单元测试目录并汇总统计。

    Returns:
        ``({相对仓库根路径: 统计}, 语法错误列表)``。
    """
    stats: dict[str, AssertionStats] = {}
    errors: list[str] = []
    if not UNIT_TESTS_ROOT.is_dir():
        return stats, errors
    for path in sorted(UNIT_TESTS_ROOT.glob("*.py")):
        rel = path.relative_to(REPO_ROOT).as_posix()
        try:
            stats[rel] = analyze_file(path)
        except SyntaxError as exc:
            errors.append(f"{rel}: {exc}")
    return stats, errors


def _load_baseline() -> dict[str, AssertionStats]:
    """读取基线文件。

    Returns:
        逐文件统计。

    Raises:
        SystemExit: 文件缺失 / JSON 损坏 / 版本不符（退出码 2）。
    """
    if not BASELINE_PATH.is_file():
        print(
            f"[assertion_freeze] 基线缺失: {BASELINE_PATH}（先运行 --update）",
            file=sys.stderr,
        )
        raise SystemExit(2)
    try:
        payload = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"[assertion_freeze] 基线损坏: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    if payload.get("version") != _BASELINE_VERSION:
        print(
            f"[assertion_freeze] 基线版本不兼容（{payload.get('version')}）——重新 --update",
            file=sys.stderr,
        )
        raise SystemExit(2)
    files: dict[str, AssertionStats] = {}
    for path, raw in payload.get("files", {}).items():
        files[str(path)] = AssertionStats(
            asserts=int(raw.get("asserts", 0)),
            raises=int(raw.get("raises", 0)),
            digest=str(raw.get("digest", "")),
        )
    return files


def _write_baseline(stats: dict[str, AssertionStats]) -> None:
    """写入基线文件（人工审阅后提交）。"""
    payload = {
        "version": _BASELINE_VERSION,
        "note": "单元测试断言基线——计数只降不升；再生成: python scripts/assertion_freeze.py --update",
        "totals": {
            "asserts": sum(item.asserts for item in stats.values()),
            "raises": sum(item.raises for item in stats.values()),
            "files": len(stats),
        },
        "files": {
            path: {"asserts": item.asserts, "raises": item.raises, "digest": item.digest}
            for path, item in sorted(stats.items())
        },
    }
    BASELINE_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        f"[assertion_freeze] 基线已写入 {BASELINE_PATH}（asserts={payload['totals']['asserts']}, "
        f"raises={payload['totals']['raises']}, files={payload['totals']['files']}）"
    )


def _report(
    stats: dict[str, AssertionStats], baseline: dict[str, AssertionStats] | None
) -> tuple[list[str], list[str]]:
    """打印逐文件对照并返回（计数下降 / 整删明细, 摘要变化明细）。

    Args:
        stats: 当前扫描统计。
        baseline: 基线统计（None 表示无对照，仅打印总量）。

    Returns:
        ``(drops, digest_changes)``——drops 含计数下降与基线内文件缺失。
    """
    total_asserts = sum(item.asserts for item in stats.values())
    total_raises = sum(item.raises for item in stats.values())
    print(
        f"[assertion_freeze] 文件 {len(stats)} 份；asserts={total_asserts}；"
        f"raises={total_raises}"
    )
    drops: list[str] = []
    digest_changes: list[str] = []
    if baseline is None:
        return drops, digest_changes
    for path, item in stats.items():
        base = baseline.get(path)
        if base is None:
            continue
        if item.asserts < base.asserts:
            drops.append(f"{path}: assert {item.asserts} < 基线 {base.asserts}")
        if item.raises < base.raises:
            drops.append(f"{path}: pytest.raises {item.raises} < 基线 {base.raises}")
        if item.digest != base.digest and item.asserts >= base.asserts:
            digest_changes.append(f"{path}: 断言 AST 摘要变化（计数未降）")
    new_files = sorted(set(stats) - set(baseline))
    removed = sorted(set(baseline) - set(stats))
    if new_files:
        print(f"[assertion_freeze] 基线外新文件 {len(new_files)} 份（评审后 --update 并入）:")
        for path in new_files[:10]:
            print(f"  - {path}")
        for path in new_files:
            drops.append(f"{path}: 基线外新测试文件（评审后 --update 并入基线）")
    if removed:
        print(
            f"[assertion_freeze] 基线内已删除文件 {len(removed)} 份"
            "（整删须评审后 --update 重建基线）:"
        )
        for path in removed[:10]:
            print(f"  - {path}")
        for path in removed:
            drops.append(f"{path}: 基线内测试文件缺失（整删须 --update 重建基线）")
    if digest_changes:
        print(f"[assertion_freeze] AST 摘要变化 {len(digest_changes)} 份（报告不拦截）:")
        for path in digest_changes[:15]:
            print(f"  - {path}")
    return drops, digest_changes


def main(argv: list[str] | None = None) -> int:
    """CLI 入口。

    Args:
        argv: 命令行参数（None 取 sys.argv[1:]）。

    Returns:
        进程退出码（见模块文档）。
    """
    parser = argparse.ArgumentParser(description="单元测试断言侵蚀棘轮")
    parser.add_argument("--enforce", action="store_true", help="棘轮校验（计数下降即红）")
    parser.add_argument("--update", action="store_true", help="重建基线")
    args = parser.parse_args(argv)

    stats, errors = collect_stats()
    if errors:
        print("[assertion_freeze] 语法错误:", file=sys.stderr)
        for item in errors:
            print(f"  - {item}", file=sys.stderr)
        return 2
    if args.update:
        _write_baseline(stats)
        _report(stats, None)
        return 0
    baseline = _load_baseline() if args.enforce or BASELINE_PATH.is_file() else None
    drops, _ = _report(stats, baseline)
    if args.enforce and drops:
        print(
            "[assertion_freeze] 断言侵蚀（计数下降 / 基线内文件缺失 / 基线外新文件）:",
            file=sys.stderr,
        )
        for item in drops:
            print(f"  - {item}", file=sys.stderr)
        print(
            "  修复：补回被删断言 / 恢复被删文件；确属等量重写或整删时评审后 --update 重建基线",
            file=sys.stderr,
        )
        return 1
    if args.enforce:
        print("[assertion_freeze] 棘轮通过（无计数下降、无基线内外文件缺口）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

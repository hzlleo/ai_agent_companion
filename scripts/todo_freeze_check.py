#!/usr/bin/env python3
"""文档遗留标记冻结门（todo freeze）——docs/ 未完成语只降不升。

扫描 ``docs/**`` 全部 Markdown，统计四类未完成标记的出现次数（不是行数
——同一行出现两次算两次）：

    - ``TODO``
    - ``待办`` / ``待定`` / ``暂未``

基线 ``scripts/.todo_baseline.json`` 记录逐文件计数与总数：gate 模式下
总数或任一文件计数**上升**即红（清理后应运行 ``--update`` 下调基线，
使棘轮真正闭合）。报告模式恒退出 0，供人工盘点。

用法::

    python scripts/todo_freeze_check.py --enforce   # 门禁（回升 exit 1）
    python scripts/todo_freeze_check.py             # 报告
    python scripts/todo_freeze_check.py --update    # 重建基线（人工审阅后提交）

Exit:
    0 = 不超基线 / 报告 / 更新成功；1 = 存在回升；2 = 基线缺失或损坏。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

#: 仓库根（本脚本位于 <repo>/scripts/）。
REPO_ROOT = Path(__file__).resolve().parents[1]

#: 扫描根。
DOCS_ROOT = REPO_ROOT / "docs"

#: 基线文件（与其它棘轮基线同目录）。
BASELINE_PATH = Path(__file__).resolve().parent / ".todo_baseline.json"

#: 基线 schema 版本（口径变化时递增）。
_BASELINE_VERSION = 1

#: 未完成标记词表。
_MARKERS: tuple[str, ...] = ("TODO", "待办", "待定", "暂未")


def _iter_docs() -> list[Path]:
    """枚举 docs 下的 Markdown 文件（排序稳定）。"""
    if not DOCS_ROOT.is_dir():
        return []
    return sorted(DOCS_ROOT.rglob("*.md"))


def count_markers() -> dict[str, int]:
    """统计每个文档的未完成标记出现次数（只收录非零文件）。

    Returns:
        ``{相对仓库根路径: 出现次数}``。
    """
    counts: dict[str, int] = {}
    for path in _iter_docs():
        text = path.read_text(encoding="utf-8", errors="replace")
        total = sum(text.count(marker) for marker in _MARKERS)
        if total:
            counts[path.relative_to(REPO_ROOT).as_posix()] = total
    return dict(sorted(counts.items()))


def _load_baseline() -> dict[str, int]:
    """读取基线文件。

    Returns:
        逐文件计数。

    Raises:
        SystemExit: 文件缺失 / JSON 损坏 / 版本不符（退出码 2）。
    """
    if not BASELINE_PATH.is_file():
        print(
            f"[todo_freeze_check] 基线缺失: {BASELINE_PATH}（先运行 --update）",
            file=sys.stderr,
        )
        raise SystemExit(2)
    try:
        payload = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"[todo_freeze_check] 基线损坏: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    if payload.get("version") != _BASELINE_VERSION:
        print(
            f"[todo_freeze_check] 基线版本不兼容（{payload.get('version')}）——重新 --update",
            file=sys.stderr,
        )
        raise SystemExit(2)
    files = payload.get("files", {})
    return {str(key): int(value) for key, value in files.items()}


def _report(counts: dict[str, int], baseline: dict[str, int] | None) -> list[str]:
    """打印逐文件对照并返回超基线明细。"""
    total = sum(counts.values())
    print(f"[todo_freeze_check] 文档 {len(_iter_docs())} 份；命中文件 {len(counts)} 份；总数 {total}")
    regressions: list[str] = []
    for path, count in counts.items():
        allowed = 0 if baseline is None else baseline.get(path, 0)
        if count > allowed:
            regressions.append(f"{path}: {count} > 基线 {allowed}")
    if baseline is not None:
        baseline_total = sum(baseline.values())
        print(f"[todo_freeze_check] 基线总数 {baseline_total}")
        if total > baseline_total:
            regressions.append(f"总数: {total} > 基线 {baseline_total}")
        cleaned = sorted(set(baseline) - set(counts))
        if cleaned:
            print(f"[todo_freeze_check] 已清理文件 {len(cleaned)} 份（可用 --update 下调基线）:")
            for path in cleaned:
                print(f"  - {path}")
    return regressions


def _write_baseline(counts: dict[str, int]) -> None:
    """写入基线文件（人工审阅后提交）。"""
    payload = {
        "version": _BASELINE_VERSION,
        "note": "docs 未完成标记基线——只降不升；再生成: python scripts/todo_freeze_check.py --update",
        "total": sum(counts.values()),
        "files": counts,
    }
    BASELINE_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"[todo_freeze_check] 基线已写入 {BASELINE_PATH}")


def main(argv: list[str] | None = None) -> int:
    """CLI 入口。

    Args:
        argv: 命令行参数（None 取 sys.argv[1:]）。

    Returns:
        进程退出码（见模块文档）。
    """
    parser = argparse.ArgumentParser(description="docs 未完成标记只降不升冻结门")
    parser.add_argument("--enforce", action="store_true", help="棘轮校验（回升即红）")
    parser.add_argument("--update", action="store_true", help="重建基线")
    args = parser.parse_args(argv)

    counts = count_markers()
    if args.update:
        _write_baseline(counts)
        _report(counts, None)
        return 0
    baseline = _load_baseline() if args.enforce else None
    regressions = _report(counts, baseline)
    if args.enforce and regressions:
        print("[todo_freeze_check] 未完成标记回升（只允许减少）:", file=sys.stderr)
        for item in regressions:
            print(f"  - {item}", file=sys.stderr)
        print(
            "  修复：清理标记，或评审后运行 --update 更新基线（严禁掩盖新增待办）",
            file=sys.stderr,
        )
        return 1
    if args.enforce:
        print("[todo_freeze_check] 棘轮通过（无回升）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

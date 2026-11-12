"""同步测试用例计数器 + 零容忍棘轮（sync-test ratchet）基线工具。

统计各代码树中 ``def test_``（同步测试函数）出现次数——**排除**
``async def test_``（原生异步测试）。用途（覆盖率/异步化基建建立、
Phase B 收尾为零容忍）：

    - 四树全量原生异步转换完成后，基线全部归零——棘轮语义
      从"只降不升"升级为**零容忍**：任何树出现任何同步 ``def test_``
      即刻红（新增测试必须 ``async def test_``，pytest asyncio_mode=auto
      下零样板）。

**新增代码树的维护**（树清单为手工维护）：新 Python 树加入后，需在
本文件 ``DEFAULT_TREES`` 元组与 ``scripts/.sync_test_baseline.json``
的 ``trees`` 中各加一行（基线值为 0——新树必须原生异步起步），并在
CI ``ratchet`` 任务覆盖范围内确认其 pixi 清单可被本脚本发现。

三种运行模式：

    - **报告**（默认）——逐树打印同步测试计数表，恒退出 0；
    - ``--write-baseline``——把当前计数写入基线文件（人工审阅后提交）；
    - ``--enforce``——与基线比较，任一树计数 **大于** 基线即退出 1
      （基线为 0 时等价于零容忍；CI / ``pixi run ratchet`` 使用）；
      基线文件缺失时报错并给出再生成指引。

Args（CLI）:
    trees: 参与计数的树目录（相对仓库根或绝对路径）。默认覆盖仓库
        当前唯一 Python 树：backend（docker 沙盒树落地后在此与
        ``DEFAULT_TREES`` / 基线文件中登记，基线值必须为 0 起步）。
    --baseline: 基线 JSON 路径，默认 ``<repo>/scripts/.sync_test_baseline.json``。
    --write-baseline: 覆盖写入当前计数为基线。
    --enforce: 棘轮校验模式。

Raises:
    SystemExit: ``--enforce`` 下计数回升（退出码 1）或基线缺失/不可读
        （退出码 2，提示用 ``--write-baseline`` 再生成）。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

#: 仓库根（本脚本位于 <repo>/scripts/）。
REPO_ROOT = Path(__file__).resolve().parents[1]

#: 默认基线文件路径。
DEFAULT_BASELINE = Path(__file__).resolve().parent / ".sync_test_baseline.json"

#: 未传位置参数时的默认树集合（仓库全部 Python 测试树，键 = 相对仓库根路径）。
DEFAULT_TREES: tuple[str, ...] = (
    "backend",
)

#: 扫描时跳过的目录名（pixi 环境 / 构建产物 / 依赖树 / 隐藏目录在遍历中另行过滤）。
_SKIP_DIRS = frozenset(
    {".pixi", "node_modules", "__pycache__", ".venv", ".git", ".mypy_cache"}
)

#: 同步测试定义行：行首（允许缩进）``def test_*``。
#  ``async def test_`` 行以 ``async`` 开头，天然不匹配 ``^\s*def\s``，无需再排除。
_SYNC_TEST_DEF = re.compile(r"^\s*def\s+test_\w")

#: 基线文件 schema 版本（计数口径变化时递增，旧基线拒绝 enforce）。
_BASELINE_VERSION = 1


def _iter_python_files(tree: Path):
    """递归产出树内全部 Python 源文件。

    Args:
        tree: 树根目录。

    Yields:
        ``.py`` 文件路径（跳过 _SKIP_DIRS、egg-info 与隐藏目录）。
    """
    for path in sorted(tree.rglob("*.py")):
        parts = path.relative_to(tree).parts
        if any(
            part in _SKIP_DIRS or part.startswith(".") or part.endswith(".egg-info")
            for part in parts[:-1]
        ):
            continue
        yield path


def count_sync_tests(tree: Path) -> int:
    """统计单棵树中同步测试函数定义行数。

    Args:
        tree: 树根目录。

    Returns:
        ``def test_``（非 async）出现次数。
    """
    count = 0
    for py_file in _iter_python_files(tree):
        try:
            text = py_file.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        count += sum(1 for line in text.splitlines() if _SYNC_TEST_DEF.match(line))
    return count


def collect_counts(trees: dict[str, Path]) -> dict[str, int]:
    """按树收集同步测试计数。

    Args:
        trees: 树名（相对仓库根的键）→ 树根绝对路径。

    Returns:
        树名 → 同步测试计数。

    Raises:
        SystemExit: 某树根目录不存在（退出码 2）。
    """
    counts: dict[str, int] = {}
    for name, root in trees.items():
        if not root.is_dir():
            print(f"[count_sync_tests] 树根不存在: {name} -> {root}", file=sys.stderr)
            raise SystemExit(2)
        counts[name] = count_sync_tests(root)
    return counts


def print_report(counts: dict[str, int], baseline: dict[str, int] | None) -> None:
    """打印计数表（含与基线的增量对照）。

    Args:
        counts: 当前计数。
        baseline: 基线计数（缺失时仅打印当前值）。
    """
    width = max((len(k) for k in counts), default=7)
    header = f"{'tree'.ljust(width)}  sync-tests  delta-vs-baseline"
    print(header)
    print("-" * len(header))
    for name, count in sorted(counts.items()):
        if baseline is None:
            print(f"{name.ljust(width)}  {count:>10}")
        else:
            delta = count - baseline.get(name, 0)
            print(f"{name.ljust(width)}  {count:>10}  {delta:>+d}")


def main(argv: list[str] | None = None) -> int:
    """CLI 入口。

    Args:
        argv: 命令行参数（None 时取 sys.argv[1:]）。

    Returns:
        进程退出码：报告 0；enforce 通过 0、回升 1、基线缺失 2。

    Raises:
        SystemExit: 参数解析失败（argparse 自身行为）。
    """
    parser = argparse.ArgumentParser(
        description="同步测试计数器 + 只降不升棘轮（sync-test ratchet）"
    )
    parser.add_argument(
        "trees",
        nargs="*",
        default=list(DEFAULT_TREES),
        help="树目录（相对仓库根），默认: " + ", ".join(DEFAULT_TREES),
    )
    parser.add_argument(
        "--baseline",
        type=Path,
        default=DEFAULT_BASELINE,
        help=f"基线 JSON 路径（默认 {DEFAULT_BASELINE}）",
    )
    parser.add_argument(
        "--write-baseline",
        action="store_true",
        help="把当前计数写入基线文件",
    )
    parser.add_argument(
        "--enforce",
        action="store_true",
        help="棘轮校验：任一树计数大于基线即退出 1",
    )
    args = parser.parse_args(argv)

    trees = {
        str(tree): (tree if Path(tree).is_absolute() else REPO_ROOT / tree)
        for tree in args.trees
    }
    counts = collect_counts(trees)

    if args.write_baseline:
        payload = {
            "version": _BASELINE_VERSION,
            "note": "同步测试基线——只降不升；再生成: python scripts/count_sync_tests.py --write-baseline",
            "trees": counts,
        }
        args.baseline.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(f"[count_sync_tests] 基线已写入 {args.baseline}")
        print_report(counts, None)
        return 0

    baseline_counts: dict[str, int] | None = None
    if args.baseline.exists():
        payload = json.loads(args.baseline.read_text(encoding="utf-8"))
        if args.enforce and payload.get("version") != _BASELINE_VERSION:
            print(
                f"[count_sync_tests] 基线版本不兼容（{payload.get('version')}），"
                "请用 --write-baseline 重新生成",
                file=sys.stderr,
            )
            return 2
        baseline_counts = payload.get("trees", {})

    print_report(counts, baseline_counts)

    if args.enforce:
        if baseline_counts is None:
            print(
                f"[count_sync_tests] 基线文件不存在: {args.baseline}\n"
                "首次生成: python scripts/count_sync_tests.py --write-baseline",
                file=sys.stderr,
            )
            return 2
        regressions = {
            name: count
            for name, count in counts.items()
            if count > baseline_counts.get(name, 0)
        }
        if regressions:
            print(
                "[count_sync_tests] 棘轮回升（sync 测试只允许减少，不允许新增）:\n"
                + "\n".join(f"  {k}: {v} > 基线 {baseline_counts.get(k, 0)}"
                             for k, v in regressions.items())
                + "\n修复: 新测试用 async def test_ 编写；确需新增同步测试时，"
                "评审后运行 python scripts/count_sync_tests.py --write-baseline 更新基线",
                file=sys.stderr,
            )
            return 1
        print("[count_sync_tests] 棘轮通过（无回升）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

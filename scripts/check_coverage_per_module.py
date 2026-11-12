"""按模块覆盖率门禁脚本（report / enforce 双模式）。

读取 coverage.py 生成的 ``coverage.json``（``pytest --cov --cov-report=json:...``
或 ``coverage json`` 产物），按**可配置粒度**把文件聚合为模块组，逐组计算
行覆盖率并与阈值比较：

    - **backend 剖面**：``src/packages/harness/<子包>/`` 包级一组 +
      app 层按 ``main.py`` / ``api/`` / ``middleware/`` 分组；
    - 后续 docker 沙盒树落地时再登记对应剖面（当前仓库仅 backend 树）。

聚合口径：组内 ``num_statements`` / ``covered_lines`` **求和**后计算百分比
（不是对单文件百分比求均值——避免零覆盖小文件被高覆盖大文件平均掉）。

跳过规则：任何路径段含 ``tests`` 的文件、``conftest.py``、``__pycache__``
不参与分组（测试基建自身的覆盖不进业务门禁）。

两种模式：

    - ``--report``（默认）——打印逐组覆盖率表，恒退出 0；
    - ``--enforce``——任一低于 ``--threshold``（默认 80）即打印缺口并退出 1。

Args（CLI）:
    coverage_json: coverage.json 路径（相对 CWD 或绝对路径）。
    --profile: ``auto``（按文件路径自动识别）/ ``backend``。
    --threshold: 门禁阈值百分比，默认 80。
    --enforce: 强制模式（缺省为 report 模式）。

Raises:
    SystemExit: ``--enforce`` 下存在低于阈值的模块组（退出码 1）；
        coverage.json 缺失 / 剖面无法识别 / 无可分组文件（退出码 2）。
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from pathlib import Path, PurePosixPath

#: coverage.json 内文件键为 POSIX 风格相对路径；据此归一。
_HARNESS_PREFIX = "src/packages/harness/"
_APP_PREFIX = "src/app/"

#: 不参与分组计数的路径片段。
_SKIP_SEGMENTS = frozenset({"tests", "__pycache__"})


def _is_skippable(rel_path: PurePosixPath) -> bool:
    """判断文件是否不参与业务覆盖分组。

    Args:
        rel_path: 相对 coverage 运行根的 POSIX 风格路径。

    Returns:
        True 表示跳过（测试文件 / conftest / pycache）。
    """
    if rel_path.name == "conftest.py":
        return True
    return any(seg in _SKIP_SEGMENTS for seg in rel_path.parts)


def _classify_backend(rel_path: PurePosixPath) -> str | None:
    """backend 剖面分组规则：harness 子包包级 + app 层按交付层分组。

    Args:
        rel_path: 相对路径。

    Returns:
        组名（如 ``harness/eval`` / ``app/api``）；不属于任何组返回 None。
    """
    parts = rel_path.parts
    posix = rel_path.as_posix()
    if posix.startswith(_HARNESS_PREFIX):
        rest = parts[3:]  # src/packages/harness 之后
        if len(rest) >= 2:
            return f"harness/{rest[0]}"
        return f"harness/{rest[0]}" if rest else None
    if posix.startswith(_APP_PREFIX):
        rest = parts[2:]  # src/app 之后
        if not rest:
            return None
        if rest[0].endswith(".py"):
            return "app/<root>" if rest[0] != "main.py" else "app/main.py"
        return f"app/{rest[0]}"
    return None


#: 剖面 → 分组函数（当前仅 backend；docker 沙盒树落地时再登记）。
_PROFILES: dict[str, Callable[[PurePosixPath], str | None]] = {
    "backend": _classify_backend,
}


def detect_profile(file_keys: list[str]) -> str:
    """按 coverage.json 的文件键自动识别剖面。

    Args:
        file_keys: coverage.json ``files`` 的键列表。

    Returns:
        ``"backend"``（当前唯一剖面）。

    Raises:
        SystemExit: 无法识别（退出码 2）。
    """
    for key in file_keys:
        if key.startswith(_APP_PREFIX) or key.startswith(_HARNESS_PREFIX):
            return "backend"
    print(
        "[check_coverage_per_module] 无法从 coverage.json 识别剖面"
        "（无 backend 路径），请检查 --cov 范围",
        file=sys.stderr,
    )
    raise SystemExit(2)


def group_coverage(files: dict, classify: Callable[[PurePosixPath], str | None]) -> dict[str, dict[str, int]]:
    """把 coverage.json 的 files 段聚合为逐组行覆盖统计。

    Args:
        files: coverage.json 的 ``files`` 字典（路径 → summary/statements）。
        classify: 分组函数。

    Returns:
        组名 → {"statements": 总语句数, "covered": 覆盖行数, "files": 文件数}。
    """
    grouped: dict[str, dict[str, int]] = {}
    for raw_key, payload in files.items():
        rel_path = PurePosixPath(raw_key.replace("\\", "/"))
        if _is_skippable(rel_path):
            continue
        group = classify(rel_path)
        if group is None:
            continue
        summary = payload["summary"]
        bucket = grouped.setdefault(group, {"statements": 0, "covered": 0, "files": 0})
        bucket["statements"] += summary["num_statements"]
        bucket["covered"] += summary["covered_lines"]
        bucket["files"] += 1
    return grouped


def main(argv: list[str] | None = None) -> int:
    """CLI 入口。

    Args:
        argv: 命令行参数（None 时取 sys.argv[1:]）。

    Returns:
        进程退出码：report 恒 0；enforce 全达标 0、有缺口 1；输入错误 2。

    Raises:
        SystemExit: coverage.json 缺失 / 剖面未知 / 无可分组文件（退出码 2）。
    """
    parser = argparse.ArgumentParser(
        description="按模块覆盖率门禁（report / enforce）"
    )
    parser.add_argument("coverage_json", type=Path, help="coverage.json 路径")
    parser.add_argument(
        "--profile",
        choices=("auto", "backend"),
        default="auto",
        help="分组剖面（默认 auto 自动识别）",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=80.0,
        help="门禁阈值百分比（默认 80）",
    )
    parser.add_argument(
        "--report",
        action="store_true",
        default=True,
        help="报告模式：打印逐组覆盖率表并退出 0（默认行为，显式声明便于与 --enforce 对举）",
    )
    parser.add_argument(
        "--enforce",
        action="store_true",
        help="强制模式：低于阈值即退出 1（与 --report 同给时以 --enforce 为准）",
    )
    args = parser.parse_args(argv)

    if not args.coverage_json.exists():
        print(
            f"[check_coverage_per_module] coverage.json 不存在: {args.coverage_json}\n"
            "先生成: pytest <测试路径> --cov=<源码根> --cov-report=json:coverage.json",
            file=sys.stderr,
        )
        return 2

    payload = json.loads(args.coverage_json.read_text(encoding="utf-8"))
    files = payload.get("files", {})
    if not files:
        print("[check_coverage_per_module] coverage.json 无文件数据", file=sys.stderr)
        return 2

    profile = (
        detect_profile(list(files.keys())) if args.profile == "auto" else args.profile
    )
    grouped = group_coverage(files, _PROFILES[profile])
    if not grouped:
        print(
            f"[check_coverage_per_module] 剖面 {profile} 下无文件被分组"
            "（检查 --cov 范围与剖面匹配）",
            file=sys.stderr,
        )
        return 2

    name_width = max(len(g) for g in grouped)
    header = (
        f"{'module'.ljust(name_width)}  files  statements  covered"
        f"  percent  gate(>={args.threshold:g})"
    )
    print(f"[profile: {profile}]")
    print(header)
    print("-" * len(header))

    failures: list[str] = []
    for group in sorted(grouped):
        bucket = grouped[group]
        statements = bucket["statements"]
        percent = (bucket["covered"] / statements * 100.0) if statements else 100.0
        ok = percent >= args.threshold
        marker = "PASS" if ok else "MISS"
        print(
            f"{group.ljust(name_width)}  {bucket['files']:>5}  "
            f"{statements:>10}  {bucket['covered']:>7}  {percent:>6.1f}%  {marker}"
        )
        if not ok:
            failures.append(
                f"  {group}: {percent:.1f}% < {args.threshold:g}%"
                f"（covered {bucket['covered']}/{statements}）"
            )

    if args.enforce and failures:
        print(
            "[check_coverage_per_module] 按模块覆盖率门禁未达标:\n"
            + "\n".join(failures),
            file=sys.stderr,
        )
        return 1
    if args.enforce:
        print(f"[check_coverage_per_module] 全部 {len(grouped)} 组 ≥ {args.threshold:g}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

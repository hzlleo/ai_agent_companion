"""noqa / type:ignore 增量冻结门禁。

冻结语义（本工程硬约束）：存量 noqa / eslint-disable / 存量
type: ignore **只减不增**——计数超基线即红（红灯信息含超限面与
`--update` 提示）；减少为合法（清偿后应同步下调基线）。

口径：

- 生产面计数：排除路径含 ``tests`` 段的文件、排除 docstring 反引号
  提及形态（`` `# noqa: F401` ``——历史陈述文本非真抑制注释）；
- backend = ``backend/src``，计数同时覆盖 ``# noqa`` 与 ``# type: ignore``
  （本工程硬约束禁抑制，基线零起点）；
- frontend = ``frontend/src`` 的 ``eslint-disable``（排 ``*.test.*``
  与 ``__tests__``；frontend 树落地前实测恒 0）。

用法::

    pixi run noqa-freeze                # 门禁（超基线 exit 1）
    python scripts/noqa_freeze_check.py --update   # 打印新基线供下调

Args:
    --update: 只打印各面实测数（供清偿后下调 BASELINES）。

Exit:
    0 = 全部不超基线；1 = 存在超限面（输出明细）。
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# 冻结基线（新工程零起点——只减不增，严禁上调；确需新增抑制须评审后
# 经用户显式豁免并在此留痕）。
BASELINES: dict[str, int] = {
    "backend": 0,
    "frontend-eslint-disable": 0,
}

NOQA_LINE_MARK = "# noqa"
TYPE_IGNORE_MARK = "# type: ignore"


def _iter_py_files(root: Path, extra_files: tuple[Path, ...] = ()) -> list[Path]:
    """收集生产面 Python 文件（排 tests 路径段）。"""
    files: list[Path] = []
    if root.exists():
        for p in root.rglob("*.py"):
            if "tests" in p.relative_to(REPO_ROOT).parts:
                continue
            files.append(p)
    files.extend(f for f in extra_files if f.exists())
    return files


def _count_real_noqa(path: Path) -> int:
    """单文件真实 noqa / type:ignore 行数（排反引号提及形态）。"""
    count = 0
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        for mark in (NOQA_LINE_MARK, TYPE_IGNORE_MARK):
            if mark not in line:
                continue
            idx = line.find(mark)
            if idx > 0 and line[idx - 1] == "`":
                continue  # docstring 反引号提及（历史陈述文本）
            count += 1
            break
    return count


def measure_backend() -> int:
    """backend 生产面 noqa / type:ignore 计数。

    Returns:
        backend 树生产面命中行数。
    """
    return sum(_count_real_noqa(p) for p in _iter_py_files(REPO_ROOT / "backend" / "src"))


def measure_frontend() -> int:
    """frontend 生产面 eslint-disable 计数（排测试文件）。

    Returns:
        frontend 树生产面命中行数（树不存在时为 0）。
    """
    total = 0
    root = REPO_ROOT / "frontend" / "src"
    if not root.exists():
        return 0
    for p in root.rglob("*.ts*"):
        rel = p.relative_to(REPO_ROOT)
        if "tests" in rel.parts or p.name.endswith((".test.ts", ".test.tsx")):
            continue
        if p.suffix not in (".ts", ".tsx"):
            continue
        total += p.read_text(encoding="utf-8", errors="replace").count("eslint-disable")
    return total


def main() -> int:
    """门禁入口：实测对基线，超限即红。

    Returns:
        0 = 全部不超基线；1 = 存在超限面。
    """
    measured = {
        "backend": measure_backend(),
        "frontend-eslint-disable": measure_frontend(),
    }
    print("== noqa / type:ignore 冻结门禁（S8 拍板 A）==")
    for surface, count in measured.items():
        baseline = BASELINES[surface]
        if count > baseline:
            print(
                f"FAIL {surface}: {count} > 基线 {baseline}"
                "（存量只减不增——清偿或新增抑制需评审；下调基线用 --update）"
            )
        else:
            print(f"ok   {surface}: {count} ≤ 基线 {baseline}")
    if "--update" in sys.argv:
        print("\n--update 新基线（清偿后回填 BASELINES，严禁上调）：")
        for surface, count in measured.items():
            print(f'    "{surface}": {count},')
        return 0
    if any(measured[s] > BASELINES[s] for s in measured):
        print("\n结果：FAIL（存在超限面）")
        return 1
    print("\n结果：PASS（全部不超基线）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

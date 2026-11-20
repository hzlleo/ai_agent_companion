#!/usr/bin/env python3
"""conftest env 硬覆盖完整性门——测试配置不得依赖真实工程配置。

背景：``backend/src/conftest.py`` 在 pytest 会话启动时把 Settings 全量 env
面逐键硬覆盖为测试专用值。若某次配置面扩展新增了 ``BaseSettings`` 字段而
conftest 漏补对应键，测试不会红——同名真实配置（开发者 shell 导出或本地
``.env``）会在该键上重新泄入，裸构造 ``XxxSettings()`` 与 app 级 Settings
悄悄取到非测试值。

本门禁以 ``scripts/generate_examples_env.py`` 的 ``_ENV_SECTIONS`` 登记表
（``.env.example`` 生成的同一真源）为全量键集，用 AST 扫描 conftest 的
``os.environ["KEY"] = ...`` 硬赋值，逐键核对：

    - 登记表内每个 env 键必须在 conftest 中被硬赋值，或登记在允许清单
      ``scripts/.conftest_allowlist.json``（每项必须带 ``reason`` 字段，
      可为空串）；
    - 允许清单里已被硬赋值或已不在登记表的条目按陈旧条目报告（不阻断，
      清理时删除）。

用法::

    python scripts/conftest_env_check.py                    # 门禁（缺口 exit 1）
    python scripts/conftest_env_check.py --report           # 只报告（恒 exit 0）
    python scripts/conftest_env_check.py --conftest-path /tmp/conftest.py

Exit:
    0 = 全量键已硬覆盖或在允许清单；1 = 存在缺口；2 = 允许清单损坏。
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path

#: 仓库根（本脚本位于 <repo>/scripts/）。
REPO_ROOT = Path(__file__).resolve().parents[1]

#: 后端源码根（配置类导入路径）。
BACKEND_SRC = REPO_ROOT / "backend" / "src"

#: 默认 conftest 路径。
DEFAULT_CONFTEST = BACKEND_SRC / "conftest.py"

#: 允许清单路径。
ALLOWLIST_PATH = Path(__file__).resolve().parent / ".conftest_allowlist.json"

#: 允许清单 schema 版本。
_ALLOWLIST_VERSION = 1

#: 允许清单固定说明（人工维护）。
_ALLOWLIST_NOTE = (
    "conftest env 硬覆盖豁免清单——每项登记未硬覆盖的 env 键并写明理由"
    "（scripts/conftest_env_check.py 消费）；无豁免时 entries 保持空列表。"
)

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))
if str(BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(BACKEND_SRC))

from generate_examples_env import _ENV_SECTIONS
from generate_examples_helpers import _get_env_var_name, _is_scalar_env_field

from packages.harness.config.models import Settings


def _registry_env_keys() -> dict[str, str]:
    """从生成器登记表枚举全量 env 键。

    Returns:
        ``{env 键: 来源说明}``（来源为 ``类名.字段名``——缺口报告定位用）。
    """
    keys: dict[str, str] = {}
    for _title, classes in _ENV_SECTIONS:
        for cls in classes:
            for field_name, field_info in cls.model_fields.items():
                if not _is_scalar_env_field(field_info):
                    continue
                keys[_get_env_var_name(field_name, cls)] = f"{cls.__name__}.{field_name}"
    # 生成器把 Settings.env 作为独立分节写入（无 env_prefix → ENV），此处
    # 按同一口径并入全量键集。
    env_field = Settings.model_fields["env"]
    if _is_scalar_env_field(env_field):
        keys[_get_env_var_name("env", Settings)] = "Settings.env"
    return keys


def _assigned_keys(tree: ast.AST) -> set[str]:
    """收集 conftest 内的 ``os.environ["KEY"] = ...`` 硬赋值键。

    Args:
        tree: conftest 语法树。

    Returns:
        被硬赋值的 env 键集合（含条件块内的赋值）。
    """
    assigned: set[str] = set()
    for node in ast.walk(tree):
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets = [node.target]
        for target in targets:
            if not isinstance(target, ast.Subscript):
                continue
            if not (
                isinstance(target.value, ast.Attribute)
                and target.value.attr == "environ"
                and isinstance(target.value.value, ast.Name)
                and target.value.value.id == "os"
            ):
                continue
            if isinstance(target.slice, ast.Constant) and isinstance(target.slice.value, str):
                assigned.add(target.slice.value)
    return assigned


def _load_allowlist(path: Path) -> dict[str, str]:
    """读取允许清单（env 键 → reason）。

    Args:
        path: 允许清单路径（不存在按空清单处理）。

    Returns:
        ``{env 键: reason}``（reason 可为空串）。

    Raises:
        SystemExit: 清单版本不兼容 / 条目结构损坏（退出码 2）。
    """
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"[conftest_env_check] 允许清单损坏: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    if payload.get("version") != _ALLOWLIST_VERSION:
        print(
            f"[conftest_env_check] 允许清单版本不兼容（{payload.get('version')}）",
            file=sys.stderr,
        )
        raise SystemExit(2)
    entries: dict[str, str] = {}
    for raw in payload.get("entries", []):
        key = str(raw.get("key", "")).strip()
        if not key:
            print(f"[conftest_env_check] 允许清单条目缺 key: {raw}", file=sys.stderr)
            raise SystemExit(2)
        if "reason" not in raw:
            print(f"[conftest_env_check] 允许清单条目缺 reason 字段: {key}", file=sys.stderr)
            raise SystemExit(2)
        entries[key] = str(raw.get("reason", ""))
    return entries


def main(argv: list[str] | None = None) -> int:
    """CLI 入口。

    Args:
        argv: 命令行参数（None 取 sys.argv[1:]）。

    Returns:
        进程退出码（见模块文档）。
    """
    parser = argparse.ArgumentParser(description="conftest env 硬覆盖完整性门")
    parser.add_argument(
        "--conftest-path",
        type=Path,
        default=DEFAULT_CONFTEST,
        help=f"待核对 conftest（默认 {DEFAULT_CONFTEST}）",
    )
    parser.add_argument(
        "--allowlist-path",
        type=Path,
        default=ALLOWLIST_PATH,
        help=f"允许清单（默认 {ALLOWLIST_PATH}）",
    )
    parser.add_argument(
        "--report",
        action="store_true",
        help="只报告（缺口不阻断，恒 exit 0）",
    )
    args = parser.parse_args(argv)

    conftest_path: Path = args.conftest_path
    if not conftest_path.is_file():
        print(f"[conftest_env_check] conftest 缺失: {conftest_path}", file=sys.stderr)
        return 2
    registry = _registry_env_keys()
    assigned = _assigned_keys(ast.parse(conftest_path.read_text(encoding="utf-8")))
    allowed = _load_allowlist(args.allowlist_path)

    missing = sorted(set(registry) - assigned - set(allowed))
    stale = sorted((set(allowed) & assigned) | (set(allowed) - set(registry)))

    print(
        f"[conftest_env_check] 登记表 env 键 {len(registry)}；conftest 硬覆盖 "
        f"{len(assigned & set(registry))}；允许豁免 {len(set(registry) & set(allowed))}"
        f"（清单 {len(allowed)} 条）"
    )
    if stale:
        print(f"[conftest_env_check] 允许清单陈旧条目 {len(stale)} 个（报告不阻断）:")
        for key in stale:
            print(f"  - {key}: {allowed[key] or '（reason 为空）'}")
    if missing:
        print("[conftest_env_check] conftest 漏硬覆盖的 env 键:", file=sys.stderr)
        for key in missing:
            print(f"  - {key}（{registry[key]}）", file=sys.stderr)
        if args.report:
            print("[conftest_env_check] 报告模式：缺口不阻断", file=sys.stderr)
            return 0
        print(
            "[conftest_env_check] 结果 FAIL：在 backend/src/conftest.py 补硬赋值，"
            "或在 scripts/.conftest_allowlist.json 写明 reason",
            file=sys.stderr,
        )
        return 1
    print("[conftest_env_check] 门禁通过（登记表 env 键均被 conftest 硬覆盖或豁免）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

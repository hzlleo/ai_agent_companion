#!/usr/bin/env python3
"""文档数字真相门——从后端常量派生计数并校验文档内对应数字字面。

只读源码与文档，不改仓库。校验项（任一项解析失败或数字不一致即红）：

    - ``docs/backend/defense.md``：注入规则总数、逐种类规则计数、红队模板
      总数 / 阳性 / 阴性分档；
    - ``docs/backend/chat.md``：红队模板总数；
    - ``docs/backend/config.md``：生成器 ``_ENV_SECTIONS`` 登记表的分节行数；
    - 全仓措辞：每行内同时出现 ``MySQL`` 与「权威」、且不含 ``SeekDB`` 与
      「协议」即红（覆盖「MySQL 8.0.42 权威存储」/ 小写 ``mysql 8 权威`` /
      「MySQL 5.7 权威」/「以 MySQL 8 作为权威库」；``MySQL8.0.42`` /
      ``MySQL8`` 这类紧贴版本号的写法同样算命中，``mysqld`` 等更长标识符
      不算）。裸「InnoDB」泛称同样禁止（唯一白名单是 SeekDB 兼容变量名
      ``innodb_lock_wait_timeout``）。权威存储统一表述为「SeekDB 权威存储
      （MySQL 协议）」/「MySQL 协议 / SeekDB 行锁语义」。
    - ``docs/LEDGER.md``：迁移链 head 必须与 ``backend/alembic/versions``
      目录内最大版本号一致（head 从目录派生，文档只能跟随代码）。

扫描面为仓库根全树（后缀 ``.md`` / ``.py`` / ``.toml`` / ``.ini`` /
``.yml`` / ``.yaml`` / ``.example`` / ``.ts`` / ``.tsx`` / ``.js`` /
``.mjs`` / ``.json`` / ``.cfg`` / ``.sh``，以及无后缀的 ``Dockerfile`` /
``Makefile``）。跳过以下路径，理由逐条列明（跳过清单保持不变）：

    - ``.git``：版本库内部对象（二进制 / 历史文本，不属当前表述）；
    - ``.pixi``：依赖环境（第三方包自带旧口径，不属本仓表述）；
    - ``node_modules``：前端依赖；
    - ``dist``：前端构建产物；
    - ``coverage*``：覆盖率工具产物（``coverage.json`` / ``coverage`` 目录，
      每次运行重写）；
    - ``docs/evidence/logs``：验收日志（当次运行输出快照，含历史措辞）；
    - ``docs/evidence/*.json``：验收证据（冻结的历史源码哈希与步骤输出；
      后缀集本不含 ``.json``，显式列出防未来扩展误扫）；
    - ``__pycache__``：字节码缓存；
    - 本脚本自身：规则定义文本包含被禁字面。

计数与措辞不从文档回读，而是导入规则表、红队模板表与生成器登记表后派生
——文档只能跟随代码变化，不能独立漂移。

用法::

    cd backend && pixi run python ../scripts/check_doc_counts.py

Exit:
    0 = 全部数字一致；1 = 存在不一致；2 = 计数派生失败（导入错误同样非零退出）。
"""

from __future__ import annotations

import re
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from generate_examples_env import _ENV_SECTIONS, render_env_sections_markdown
from packages.harness.chat.injection_models import (
    INJECTION_RULES,
    InjectionKind,
)
from tests.unit.test_prompt_injection_red_team import (
    _NEGATIVE_TEMPLATES,
    _POSITIVE_TEMPLATES,
    RED_TEAM_TEMPLATES,
)

#: 仓库根（本脚本位于 <repo>/scripts/）。
REPO_ROOT = Path(__file__).resolve().parents[1]

#: 文档根（相对仓库根）。
_DOCS_ROOT = REPO_ROOT / "docs"

#: config.md 生成块标记（分节行只在块内计数）。
_CONFIG_MARKER_START = "<!-- generated:env-sections -->"
_CONFIG_MARKER_END = "<!-- /generated:env-sections -->"

#: 措辞扫描根（仓库根全树；跳过清单见模块 docstring）。
_WORDING_ROOT = REPO_ROOT

#: 措辞扫描后缀（文档 / 配置模板 / 工程说明 / Python docstring）。
_WORDING_SUFFIXES: frozenset[str] = frozenset(
    {
        ".md",
        ".py",
        ".toml",
        ".ini",
        ".yml",
        ".yaml",
        ".example",
        ".ts",
        ".tsx",
        ".js",
        ".mjs",
        ".json",
        ".cfg",
        ".sh",
    }
)

#: 措辞扫描的无后缀文件名（构建 / 容器入口）。
_WORDING_EXTENSIONLESS_NAMES: frozenset[str] = frozenset({"Dockerfile", "Makefile"})

#: 措辞扫描跳过目录名（任意层级；理由见模块 docstring）。
_WORDING_SKIP_DIRS: frozenset[str] = frozenset(
    {".git", ".pixi", "node_modules", "dist", "__pycache__"}
)

#: 措辞扫描跳过目录名前缀（工具产物；理由见模块 docstring）。
_WORDING_SKIP_DIR_PREFIXES: tuple[str, ...] = ("coverage",)

#: 措辞扫描跳过路径前缀（相对仓库根；理由见模块 docstring）。
_WORDING_SKIP_PATH_PREFIXES: tuple[str, ...] = ("docs/evidence/logs",)

#: 措辞扫描跳过文件模式（相对仓库根；理由见模块 docstring）。
_WORDING_SKIP_FILE_PATTERNS: tuple[str, ...] = ("docs/evidence/*.json",)

#: 同行判定要素：MySQL 字样与「权威」同现、且该行不含 SeekDB 与「协议」。
#: 词尾守卫放行紧贴版本号的写法（``MySQL8.0.42`` / ``MySQL8``），同时不把
#: 更长的标识符（``mysqld`` 守护进程名）当成引擎泛称。
_MYSQL_WORD = re.compile(r"(?i)\bmysql(?![a-z])")
_SEEKDB_WORD = re.compile(r"(?i)\bseekdb\b")
_AUTHORITY_WORD = "权威"
_PROTOCOL_WORD = "协议"

#: 迁移链 head 的版本目录（文档数字从此派生）。
_ALEMBIC_VERSIONS_ROOT = REPO_ROOT / "backend" / "alembic" / "versions"

#: 唯一允许出现的 InnoDB 字面（SeekDB 兼容占位变量名）。
_INNODB_VARIABLE = re.compile(r"innodb_lock_wait_timeout", re.IGNORECASE)

#: 裸 InnoDB 泛称（白名单剥离后仍出现即红）。
_INNODB_GENERIC = re.compile(r"\bInnoDB\b", re.IGNORECASE)

#: 措辞扫描的自身排除（规则定义文本包含被禁字面本身）。
_WORDING_SELF_EXCLUDED = frozenset({Path(__file__).resolve()})


@dataclass(frozen=True)
class DocCountFact:
    """单条文档数字校验项。

    Attributes:
        doc: 目标文档相对仓库根的路径。
        label: 校验项说明（失败输出用）。
        pattern: 匹配文档数字字面的正则（第 1 组起为整数）。
        expected: 期望整数序列。
    """

    doc: str
    label: str
    pattern: str
    expected: tuple[int, ...]


def _rule_facts() -> list[DocCountFact]:
    """按规则表派生注入规则计数校验项。"""
    counts = Counter(rule.kind for rule in INJECTION_RULES)
    facts = [
        DocCountFact(
            doc="backend/defense.md",
            label="注入规则总数",
            pattern=r"规则表 `INJECTION_RULES`，(\d+) 条",
            expected=(len(INJECTION_RULES),),
        )
    ]
    for kind in (
        InjectionKind.IMPERATIVE,
        InjectionKind.FAKE_SYSTEM,
        InjectionKind.IDENTITY_CLAIM,
        InjectionKind.FORMAT_DEMAND,
        InjectionKind.TAG_MIMICRY,
    ):
        facts.append(
            DocCountFact(
                doc="backend/defense.md",
                label=f"{kind.value} 规则计数",
                pattern=rf"`{kind.value}`（(\d+) 条）",
                expected=(counts[kind],),
            )
        )
    return facts


def _red_team_facts() -> list[DocCountFact]:
    """按红队模板表派生模板总数与阳 / 阴分档校验项。"""
    total = len(RED_TEAM_TEMPLATES)
    positive = len(_POSITIVE_TEMPLATES)
    negative = len(_NEGATIVE_TEMPLATES)
    return [
        DocCountFact(
            doc="backend/defense.md",
            label="红队模板总数（防线一览）",
            pattern=r"（(\d+) 条模板）",
            expected=(total,),
        ),
        DocCountFact(
            doc="backend/defense.md",
            label="红队模板总数（回归集）",
            pattern=r"固化 (\d+) 条模板",
            expected=(total,),
        ),
        DocCountFact(
            doc="backend/defense.md",
            label="红队阳性 / 阴性分档",
            pattern=r"（(\d+) 阳性 /\s*(\d+) 阴性）",
            expected=(positive, negative),
        ),
        DocCountFact(
            doc="backend/defense.md",
            label="红队模板总数（测试与门禁）",
            pattern=r"——(\d+) 条模板检测矩阵",
            expected=(total,),
        ),
        DocCountFact(
            doc="backend/chat.md",
            label="红队模板总数（测试面索引）",
            pattern=r"——(\d+) 条红队模板检测矩阵",
            expected=(total,),
        ),
    ]


def _migration_head() -> int:
    """从 Alembic 版本目录派生当前迁移链 head。

    Returns:
        最大迁移版本号（整型；文档以四位零填充展示）。

    Raises:
        RuntimeError: 版本目录缺失或没有任何迁移文件（门禁无法判定）。
    """
    if not _ALEMBIC_VERSIONS_ROOT.is_dir():
        raise RuntimeError(f"迁移版本目录缺失: {_ALEMBIC_VERSIONS_ROOT}")
    revisions = [
        int(match.group(1))
        for path in _ALEMBIC_VERSIONS_ROOT.glob("*.py")
        if (match := re.match(r"(\d{12})_", path.name)) is not None
    ]
    if not revisions:
        raise RuntimeError("迁移版本目录内没有可识别的版本文件")
    return max(revisions)


def _migration_facts() -> list[DocCountFact]:
    """按迁移目录派生 LEDGER 的迁移链 head 校验项。"""
    return [
        DocCountFact(
            doc="LEDGER.md",
            label="迁移链 head",
            pattern=r"数据库迁移链 0001\.\.(\d{4})",
            expected=(_migration_head(),),
        )
    ]


def _evaluate_fact(fact: DocCountFact) -> list[str]:
    """读取文档并按正则核对单条数字事实。

    Args:
        fact: 数字校验项。

    Returns:
        问题列表（空 = 一致）。
    """
    path = _DOCS_ROOT / fact.doc
    if not path.is_file():
        return [f"{fact.doc} 缺失"]
    text = path.read_text(encoding="utf-8")
    matches = list(re.finditer(fact.pattern, text, re.MULTILINE | re.DOTALL))
    if not matches:
        return [f"{fact.doc} 未匹配到数字字面（{fact.label}: {fact.pattern}）"]
    problems: list[str] = []
    for match in matches:
        actual = tuple(int(group) for group in match.groups())
        if actual != fact.expected:
            problems.append(
                f"{fact.doc} {fact.label} 数字漂移: 文档={actual} 代码={fact.expected}"
            )
    return problems


def _evaluate_config_rows() -> list[str]:
    """统计 config.md 生成块内的分节表数据行数并与登记表比对。

    Returns:
        问题列表（空 = 一致；文档缺失时跳过，标记块缺失即红）。
    """
    path = _DOCS_ROOT / "backend" / "config.md"
    if not path.is_file():
        return []
    text = path.read_text(encoding="utf-8")
    start = text.find(_CONFIG_MARKER_START)
    end = text.find(_CONFIG_MARKER_END)
    if start < 0 or end < 0 or end <= start:
        return ["backend/config.md 缺少 env 分节生成块标记"]
    block = text[start:end]
    rows = _table_data_rows(block)
    expected_rows = _table_data_rows(render_env_sections_markdown())
    if len(rows) != len(expected_rows):
        return [
            "backend/config.md env 分节登记行数数字漂移: "
            f"文档={len(rows)} 代码={len(expected_rows)}"
        ]
    return []


def _table_data_rows(block: str) -> list[str]:
    """提取 Markdown 表格的数据行（排除表头与分隔行）。

    Args:
        block: 含表格的 Markdown 文本。

    Returns:
        数据行列表（保持原序）。
    """
    return [
        line
        for line in block.splitlines()
        if line.startswith("| ")
        and not line.startswith("| 分节")
        and not line.startswith("|---")
    ]


def _wording_files() -> list[Path]:
    """枚举措辞扫描文件（仓库根全树；跳过清单见模块 docstring）。

    Returns:
        待扫描文件路径列表（去重、稳定排序）。
    """
    files: set[Path] = set()
    for directory, dirnames, filenames in _WORDING_ROOT.walk():
        dirnames[:] = sorted(
            name
            for name in dirnames
            if name not in _WORDING_SKIP_DIRS
            and not name.startswith(_WORDING_SKIP_DIR_PREFIXES)
        )
        for name in filenames:
            path = directory / name
            if (
                path.suffix not in _WORDING_SUFFIXES
                and path.name not in _WORDING_EXTENSIONLESS_NAMES
            ):
                continue
            relative = path.relative_to(REPO_ROOT)
            if str(relative).startswith(_WORDING_SKIP_PATH_PREFIXES):
                continue
            if any(relative.match(pattern) for pattern in _WORDING_SKIP_FILE_PATTERNS):
                continue
            if path.resolve() in _WORDING_SELF_EXCLUDED:
                continue
            files.add(path)
    return sorted(files)


def _wording_problems() -> list[str]:
    """扫描全仓禁用措辞（旧权威表述 / 裸行锁引擎泛称）。

    Returns:
        问题列表（``路径:行号:命中规则``；空 = 一致）。
    """
    problems: list[str] = []
    for path in _wording_files():
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError:
            continue
        relative = path.relative_to(REPO_ROOT)
        for number, line in enumerate(lines, start=1):
            if (
                _MYSQL_WORD.search(line)
                and _AUTHORITY_WORD in line
                and not _SEEKDB_WORD.search(line)
                and _PROTOCOL_WORD not in line
            ):
                problems.append(
                    f"{relative}:{number}: 禁用表述「MySQL 权威」"
                    "（同行须带 SeekDB 或「协议」限定）"
                )
            if _INNODB_GENERIC.search(_INNODB_VARIABLE.sub("", line)):
                problems.append(
                    f"{relative}:{number}: 裸「InnoDB」泛称（改「MySQL 协议 / SeekDB 行锁语义」）"
                )
    return problems


def main() -> int:
    """CLI 入口：逐条核对文档数字真相。

    Returns:
        0 = 全部一致；1 = 存在不一致；2 = 计数派生失败。
    """
    try:
        facts = _rule_facts() + _red_team_facts() + _migration_facts()
    except Exception as exc:  # 计数派生失败 = 门禁无法判定，直接红
        print(f"[check_doc_counts] 文档计数派生失败: {exc}", file=sys.stderr)
        return 2

    problems: list[str] = []
    for fact in facts:
        problems.extend(_evaluate_fact(fact))
    problems.extend(_evaluate_config_rows())
    problems.extend(_wording_problems())
    if problems:
        print("[check_doc_counts] 文档数字 / 措辞与代码常量不一致:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    print(
        f"[check_doc_counts] 文档数字与措辞一致：{len(facts) + 1} 项"
        f"（规则 {len(INJECTION_RULES)} 条 / 红队 {len(RED_TEAM_TEMPLATES)} 条"
        f" / 配置分节 {len(_ENV_SECTIONS)} 项 / 措辞扫描 {len(_wording_files())} 文件）"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

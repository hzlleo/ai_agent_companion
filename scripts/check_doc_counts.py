#!/usr/bin/env python3
"""文档数字真相门——从后端常量派生计数并校验文档内对应数字字面。

只读源码与文档，不改仓库。校验项（任一项解析失败或数字不一致即红）：

    - ``docs/backend/defense.md``：注入规则总数、逐种类规则计数、红队模板
      总数 / 阳性 / 阴性分档；
    - ``docs/backend/chat.md``：红队模板总数；
    - ``docs/backend/config.md``：生成器 ``_ENV_SECTIONS`` 登记表的分节行数。

计数不从文档回读，而是导入规则表、红队模板表与生成器登记表后派生——
文档数字只能跟随代码变化，不能独立漂移。

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


def main() -> int:
    """CLI 入口：逐条核对文档数字真相。

    Returns:
        0 = 全部一致；1 = 存在不一致；2 = 计数派生失败。
    """
    try:
        facts = _rule_facts() + _red_team_facts()
    except Exception as exc:  # 计数派生失败 = 门禁无法判定，直接红
        print(f"[check_doc_counts] 文档计数派生失败: {exc}", file=sys.stderr)
        return 2

    problems: list[str] = []
    for fact in facts:
        problems.extend(_evaluate_fact(fact))
    problems.extend(_evaluate_config_rows())
    if problems:
        print("[check_doc_counts] 文档数字与代码常量不一致:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    print(
        f"[check_doc_counts] 文档数字一致：{len(facts) + 1} 项"
        f"（规则 {len(INJECTION_RULES)} 条 / 红队 {len(RED_TEAM_TEMPLATES)} 条"
        f" / 配置分节 {len(_ENV_SECTIONS)} 项）"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

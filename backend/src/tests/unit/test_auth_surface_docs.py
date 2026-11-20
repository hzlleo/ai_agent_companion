"""管理面保护面文档与装配清单一致性测试（README ↔ `_include_routers`）。

解析 ``docs/backend/README.md`` 认证一节的「受保护」行与 ``app/main.py``
``_include_routers`` 中挂到 ``admin_api`` 的路由集逐项比对；任一侧增删
未同步即红（路由清单唯一权威是装配层，文档是镜像）。
"""

from __future__ import annotations

import ast
from pathlib import Path

#: 测试文件到仓库根的层级（backend/src/tests/unit → 仓库根）。
_REPO_ROOT = Path(__file__).resolve().parents[4]

#: 保护面文档（认证一节表格）。
_README_PATH = _REPO_ROOT / "docs" / "backend" / "README.md"

#: 路由装配层（保护面唯一权威）。
_MAIN_PATH = _REPO_ROOT / "backend" / "src" / "app" / "main.py"

#: 装配层当前保护路由集数量（文档行同步的规模校验）。
_EXPECTED_PROTECTED_ROUTER_COUNT = 15


def _normalize_route_group(name: str) -> str:
    """归一化路由集标识（分隔符差异不参与比对——下划线 / 连字符等价）。

    Args:
        name: 路由集标识（文档文本或装配变量名去后缀后的形态）。

    Returns:
        仅保留字母数字的归一键。
    """
    return "".join(character for character in name if character.isalnum()).lower()


def _documented_protected_route_groups() -> set[str]:
    """读取 README 认证表的「受保护」行并拆出路由集标识。

    Returns:
        归一化路由集标识集合（空集 = 文档行缺失，测试应红）。
    """
    for line in _README_PATH.read_text(encoding="utf-8").splitlines():
        if line.startswith("|") and "**受保护**" in line:
            first_cell = line.split("|")[1].strip()
            return {
                _normalize_route_group(piece.strip())
                for piece in first_cell.split("/")
                if piece.strip()
            }
    return set()


def _assembled_protected_route_groups() -> set[str]:
    """解析 `_include_routers` 中挂到 admin_api 的路由变量名。

    Returns:
        归一化路由集标识集合（变量名去 ``_router`` 后缀后归一）。
    """
    tree = ast.parse(_MAIN_PATH.read_text(encoding="utf-8"))
    groups: set[str] = set()
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef) or node.name != "_include_routers":
            continue
        for statement in ast.walk(node):
            if not isinstance(statement, ast.Call):
                continue
            function = statement.func
            if not isinstance(function, ast.Attribute) or function.attr != "include_router":
                continue
            if not isinstance(function.value, ast.Name) or function.value.id != "admin_api":
                continue
            if not statement.args or not isinstance(statement.args[0], ast.Name):
                continue
            router_name = statement.args[0].id
            groups.add(_normalize_route_group(router_name.removesuffix("_router")))
    return groups


async def test_readme_protected_route_groups_match_assembly() -> None:
    """README 保护面行与 `_include_routers` 逐项一致（含 canvas——漂移即红）。"""
    documented = _documented_protected_route_groups()
    assembled = _assembled_protected_route_groups()
    assert len(assembled) == _EXPECTED_PROTECTED_ROUTER_COUNT
    assert documented == assembled
    assert "canvas" in documented

"""公开入站体有界读取的结构守卫——main.py 豁免鉴权 POST 路由必须接入共享读取器。

用例从 ``app/main.py`` 的 ``_include_routers`` 机械枚举**直接挂载的豁免
路由**（不经管理面 ``admin_api`` 包装），解析到实际模块后检查每个
``@router.post`` 处理函数：必须调用 ``app.api.inbound_body`` 的
``read_bounded_body`` / ``read_bounded_json_object``，且不得直接调用
``request.json()`` / ``request.body()`` 绕过上限。未来新增豁免 POST 端点
未接入读取器即红。
"""

from __future__ import annotations

import ast
import importlib
import inspect
from pathlib import Path

import app.main as main_module

_EXEMPT_POST_MODULES: frozenset[str] = frozenset(
    {
        "app.api.channels_qq",
        "app.api.channels_sms_forwarder",
        "app.api.channels_whatsapp",
    }
)

_READER_CALLS: frozenset[str] = frozenset(
    {"read_bounded_body", "read_bounded_json_object"}
)

_BYPASS_CALLS: frozenset[str] = frozenset({"json", "body"})


def _function(tree: ast.Module, name: str) -> ast.FunctionDef:
    """取模块内指定顶层函数（缺失即断言失败）。

    Args:
        tree: 已解析模块 AST。
        name: 顶层函数名。

    Returns:
        对应函数节点。
    """
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"main.py 缺少顶层函数 {name}")


def _direct_exempt_router_names(tree: ast.Module) -> set[str]:
    """枚举 ``_include_routers`` 内直接 ``app.include_router(<name>)`` 的路由名。

    Args:
        tree: ``app/main.py`` 的 AST。

    Returns:
        直接挂载在应用上的路由变量名（不含管理面 ``admin_api`` 包装）。
    """
    names: set[str] = set()
    for node in ast.walk(_function(tree, "_include_routers")):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (
            isinstance(func, ast.Attribute)
            and func.attr == "include_router"
            and isinstance(func.value, ast.Name)
            and func.value.id == "app"
        ):
            continue
        if node.args and isinstance(node.args[0], ast.Name):
            names.add(node.args[0].id)
    return names


def _imported_router_modules(tree: ast.Module) -> dict[str, str]:
    """解析 main.py 中 ``from app.api.<mod> import router as <name>`` 的映射。

    Args:
        tree: ``app/main.py`` 的 AST。

    Returns:
        导出名 → 模块路径（仅 ``app.api`` 前缀）。
    """
    mapping: dict[str, str] = {}
    for node in tree.body:
        if not isinstance(node, ast.ImportFrom) or node.level:
            continue
        if not node.module or not node.module.startswith("app.api."):
            continue
        for alias in node.names:
            if alias.asname is not None:
                mapping[alias.asname] = node.module
    return mapping


def _post_handlers(tree: ast.Module) -> list[ast.AsyncFunctionDef]:
    """取模块内 ``@router.post`` 装饰的异步处理函数。

    Args:
        tree: 目标 API 模块 AST。

    Returns:
        处理函数节点列表。
    """
    handlers: list[ast.AsyncFunctionDef] = []
    for node in tree.body:
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        for decorator in node.decorator_list:
            if not isinstance(decorator, ast.Call):
                continue
            func = decorator.func
            if (
                isinstance(func, ast.Attribute)
                and func.attr == "post"
                and isinstance(func.value, ast.Name)
                and func.value.id == "router"
            ):
                handlers.append(node)
                break
    return handlers


def _call_names(node: ast.AST) -> set[str]:
    """收集函数体内所有调用名（Name.id 或 Attribute.attr）。

    Args:
        node: 函数 / 模块 AST 节点。

    Returns:
        调用名集合。
    """
    names: set[str] = set()
    for child in ast.walk(node):
        if not isinstance(child, ast.Call):
            continue
        func = child.func
        if isinstance(func, ast.Name):
            names.add(func.id)
        elif isinstance(func, ast.Attribute):
            names.add(func.attr)
    return names


async def test_exempt_post_routes_use_shared_bounded_reader() -> None:
    """main.py 豁免鉴权的全部 POST 路由均接入共享有界读取器。"""
    main_tree = ast.parse(Path(main_module.__file__).read_text())
    imported = _imported_router_modules(main_tree)
    direct = _direct_exempt_router_names(main_tree)
    exempt_modules = {imported[name] for name in direct if name in imported}

    modules_with_post: set[str] = set()
    for module_name in sorted(exempt_modules):
        module = importlib.import_module(module_name)
        module_path = inspect.getsourcefile(module)
        assert module_path is not None
        module_tree = ast.parse(Path(module_path).read_text())
        handlers = _post_handlers(module_tree)
        if not handlers:
            continue
        for handler in handlers:
            call_names = _call_names(handler)
            assert call_names & _READER_CALLS, (
                f"{module_name}.{handler.name} 未接入共享有界请求体读取器"
            )
            assert not call_names & _BYPASS_CALLS, (
                f"{module_name}.{handler.name} 直接 request.json()/body() 绕过上限"
            )
        modules_with_post.add(module_name)

    assert modules_with_post == _EXEMPT_POST_MODULES

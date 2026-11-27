"""harness/tools 域——外部 HTTP 扩展工具执行器（管理员声明 → registry 装配）。

职责链（管理员配置文件 → LLM 工具调用）::

    extensions_config.json（管理员本地文件——信任边界）
        ↓ ExtensionTool / HttpToolTargetModel（config 域模型校验）
        ↓ build_extension_tool（参数 schema → 校验 Model Class；
        ↓     {env:} 占位在构造期解析——缺失即装配失败）
        ↓ ToolRegistry.register（与内置工具同一执行入口）
        ↓ duty / chat 工具循环（LLM function calling）
        ↓ 占位替换（URL 路径 / header；URL 值编码、header 值禁换行）
        ↓ 共享 httpx 池发送（超时上限 / 响应字节帽 / 显式禁重定向）
        ↓ 状态码校验 → JSON 解析 → result_path 提取 → 文本结果

设计决策：

    - **参数校验复用 registry 面**：``parameters`` JSON Schema 在构造期
      编译为 pydantic Model Class 并挂到 ``ToolSpecModel.args_model``——
      调用参数经 registry 的既有校验链归一，执行器不重复校验；
    - **密钥只存环境变量名**：header 值以 ``{env:VAR_NAME}`` 占位声明，
      构造期从进程环境解析；缺失变量使该工具装配失败并被装配层跳过
      （warn-and-continue），不阻塞其它工具；
    - **重定向禁用**：请求恒带 ``follow_redirects=False``——端点无法把
      请求带向配置之外的地址（防 SSRF 重定向绕过）；
    - **失败恒不抛到对话层**：运行时失败统一转
      :class:`~packages.harness.core.errors.BusinessException`，经 registry
      归一为 ``ok=False`` + 受控中文错误文本（与内置 / 沙盒工具同语义）；
    - **信任边界**：端点由管理员在本地文件定义，不做私网拦截；本模块只
      保证协议白名单、占位编码、响应帽与重定向禁用。
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Mapping
from typing import Annotated, Any, Literal
from urllib.parse import quote

import httpx
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
    create_model,
)

from packages.harness.config.models import ExtensionTool, HttpToolTargetModel, JsonSchema
from packages.harness.core.errors import BusinessException, ErrorCode
from packages.harness.core.time_utils import now_ms
from packages.harness.llm.llm_models import ToolParametersSchema
from packages.harness.tools.tool_models import ToolExecutor, ToolSpecModel

__all__ = [
    "HttpClientFactory",
    "ToolClock",
    "build_extension_tool",
]

#: 共享 HTTP 客户端工厂（执行期调用——生命周期归 pools.PoolManager / 测试注入）。
HttpClientFactory = Callable[[], httpx.AsyncClient]

#: 时钟注入面（结果时间戳——测试可控）。
ToolClock = Callable[[], int]

#: 参数占位形态（``{参数名}``；``{env:VAR}`` 因含冒号不会被本式命中）。
_ARG_PLACEHOLDER_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
#: 环境变量占位形态（``{env:VAR_NAME}``）。
_ENV_PLACEHOLDER_RE = re.compile(r"\{env:([A-Za-z_][A-Za-z0-9_]*)\}")

#: JSON Schema 基础类型 → Python 注解（动态参数模型编译面）。
#: JSON Schema 语义不接受字符串强转——一律使用 pydantic 严格类型。
_SCHEMA_PYTHON_TYPES: dict[str, Any] = {
    "string": StrictStr,
    "integer": StrictInt,
    "number": StrictFloat | StrictInt,
    "boolean": StrictBool,
    "object": dict[str, Any],
}

#: 错误文本中的响应片段字节 / 字符上限（不含响应头）。
_ERROR_SNIPPET_MAX_BYTES: int = 256
_ERROR_SNIPPET_MAX_CHARS: int = 200


def _extension_error(tool_name: str, message: str) -> BusinessException:
    """构造扩展工具失败异常（受控中文文本——registry 归一 ok=False）。

    Args:
        tool_name: 工具名。
        message: 失败原因（人读文本）。

    Returns:
        携带 ``INTERNAL_ERROR`` 的业务异常。
    """
    return BusinessException(
        code=ErrorCode.INTERNAL_ERROR,
        message=f"扩展工具 {tool_name} {message}",
    )


def _annotation_for(sub_schema: Mapping[str, Any]) -> Any:
    """把 JSON Schema 子模式映射为 Python 类型注解（基础类型面）。

    Args:
        sub_schema: 属性子模式。

    Returns:
        Python 类型（enum → ``Literal``；array → 元素类型列表——``items``
        自身的元素级约束包进 ``Annotated[元素, Field(...)]``；未知 /
        复合模式回落 ``Any``）。
    """
    enum_values = sub_schema.get("enum")
    if isinstance(enum_values, list) and enum_values:
        try:
            return Literal[tuple(enum_values)]
        except TypeError:
            pass
    raw_type = sub_schema.get("type")
    if raw_type == "array":
        items = sub_schema.get("items")
        item_schema: Mapping[str, Any] = items if isinstance(items, Mapping) else {}
        item_annotation = _annotation_for(item_schema)
        item_constraints = _field_constraints(item_schema)
        item_constraints.pop("description", None)
        if item_constraints:
            item_annotation = Annotated[item_annotation, Field(**item_constraints)]
        return list.__class_getitem__(item_annotation)
    if isinstance(raw_type, str):
        mapped = _SCHEMA_PYTHON_TYPES.get(raw_type)
        if mapped is not None:
            return mapped
    return Any


def _field_constraints(sub_schema: Mapping[str, Any]) -> dict[str, Any]:
    """提取属性子模式的校验约束（长度 / 数值范围 / 正则 / 数组长度）。

    Args:
        sub_schema: 属性子模式。

    Returns:
        ``Field`` 关键字参数字典。
    """
    constraints: dict[str, Any] = {}
    description = sub_schema.get("description")
    if isinstance(description, str) and description:
        constraints["description"] = description
    schema_type = sub_schema.get("type")
    if schema_type in ("integer", "number"):
        minimum = sub_schema.get("minimum")
        maximum = sub_schema.get("maximum")
        if isinstance(minimum, (int, float)) and not isinstance(minimum, bool):
            constraints["ge"] = minimum
        if isinstance(maximum, (int, float)) and not isinstance(maximum, bool):
            constraints["le"] = maximum
    elif schema_type == "string":
        pattern = sub_schema.get("pattern")
        if isinstance(pattern, str):
            constraints["pattern"] = pattern
        min_length = sub_schema.get("minLength")
        max_length = sub_schema.get("maxLength")
        if isinstance(min_length, int) and not isinstance(min_length, bool):
            constraints["min_length"] = min_length
        if isinstance(max_length, int) and not isinstance(max_length, bool):
            constraints["max_length"] = max_length
    elif schema_type == "array":
        min_items = sub_schema.get("minItems")
        max_items = sub_schema.get("maxItems")
        if isinstance(min_items, int) and not isinstance(min_items, bool):
            constraints["min_length"] = min_items
        if isinstance(max_items, int) and not isinstance(max_items, bool):
            constraints["max_length"] = max_items
    return constraints


def _build_args_model(schema: JsonSchema) -> type[BaseModel]:
    """把工具 ``parameters`` JSON Schema 编译为参数校验 Model Class。

    Args:
        schema: 工具参数 JSON Schema。

    Returns:
        动态 pydantic Model Class（registry 执行前经 ``model_validate``
        校验并物化默认值）。
    """
    field_definitions: dict[str, Any] = {}
    required = set(schema.required)
    for prop_name, raw_schema in schema.properties.items():
        sub_schema: Mapping[str, Any] = raw_schema if isinstance(raw_schema, Mapping) else {}
        annotation = _annotation_for(sub_schema)
        constraints = _field_constraints(sub_schema)
        if prop_name in required:
            field_definitions[prop_name] = (annotation, Field(..., **constraints))
        else:
            default = sub_schema.get("default")
            field_definitions[prop_name] = (
                annotation | None,
                Field(default=default, **constraints),
            )
    extra: Literal["allow", "forbid"] = (
        "forbid" if schema.additional_properties is False else "allow"
    )
    return create_model(
        "ExtensionToolArgsModel",
        __config__=ConfigDict(extra=extra),
        **field_definitions,
    )


def _declared_placeholders(template: str) -> set[str]:
    """提取模板中的参数占位名集合。

    Args:
        template: URL / header 值模板。

    Returns:
        占位参数名集合。
    """
    return {match.group(1) for match in _ARG_PLACEHOLDER_RE.finditer(template)}


def _ensure_declared(
    template: str, declared: set[str], tool_name: str, label: str
) -> None:
    """校验模板中的参数占位均已声明（装配期 fail loud）。

    Args:
        template: URL / header 值模板。
        declared: 工具参数 schema 已声明的属性名集合。
        tool_name: 工具名（错误文本定位面）。
        label: 模板来源标签（``url`` / ``请求头 X``）。

    Raises:
        BusinessException: 模板引用了未声明的参数占位。
    """
    for placeholder in _declared_placeholders(template):
        if placeholder not in declared:
            raise _extension_error(
                tool_name, f"{label} 引用了未声明的参数占位 {{{placeholder}}}"
            )


def _resolve_env_headers(headers: Mapping[str, str], tool_name: str) -> dict[str, str]:
    """构造期解析 header 值中的 ``{env:VAR}`` 占位。

    Args:
        headers: 配置声明的请求头映射。
        tool_name: 工具名（错误文本定位面）。

    Returns:
        环境变量已解析的请求头映射。

    Raises:
        BusinessException: 环境变量缺失 / 占位语法非法 / 解析值含换行。
    """
    resolved: dict[str, str] = {}
    for header_name, header_value in headers.items():
        resolved[header_name] = _resolve_env_value(header_value, tool_name, header_name)
    return resolved


def _resolve_env_value(header_value: str, tool_name: str, header_name: str) -> str:
    """解析单个 header 值中的环境变量占位。

    Args:
        header_value: header 原始值。
        tool_name: 工具名（错误文本定位面）。
        header_name: header 名称（错误文本定位面）。

    Returns:
        解析后的 header 值。

    Raises:
        BusinessException: 环境变量缺失 / 占位语法非法 / 解析值含换行。
    """

    def _replace(match: re.Match[str]) -> str:
        """替换单个环境变量占位。"""
        variable = match.group(1)
        env_value = os.environ.get(variable, "")
        if not env_value:
            raise _extension_error(
                tool_name,
                f"请求头 {header_name} 引用的环境变量 {variable} 未设置",
            )
        return env_value

    resolved = _ENV_PLACEHOLDER_RE.sub(_replace, header_value)
    if "{env:" in resolved:
        raise _extension_error(
            tool_name, f"请求头 {header_name} 的 {{env:}} 占位语法非法"
        )
    if "\r" in resolved or "\n" in resolved:
        raise _extension_error(tool_name, f"请求头 {header_name} 含换行")
    return resolved


def _render_placeholders(
    template: str,
    args: Mapping[str, Any],
    tool_name: str,
    label: str,
    *,
    url_encode: bool,
) -> str:
    """执行期替换参数占位（URL 值编码 / header 值禁换行）。

    Args:
        template: URL / header 值模板。
        args: 调用参数（registry 校验物化后）。
        tool_name: 工具名（错误文本定位面）。
        label: 模板来源标签。
        url_encode: True = 替换值做 URL 编码（URL 路径面）。

    Returns:
        替换后的字符串。

    Raises:
        BusinessException: 占位参数缺少调用值 / header 替换值含换行。
    """

    def _replace(match: re.Match[str]) -> str:
        """替换单个参数占位。"""
        parameter = match.group(1)
        if parameter not in args:
            raise _extension_error(
                tool_name, f"{label} 占位参数 {{{parameter}}} 缺少调用值"
            )
        raw_value = args[parameter]
        if isinstance(raw_value, str):
            text = raw_value
        elif isinstance(raw_value, (dict, list)):
            text = json.dumps(raw_value, ensure_ascii=False, separators=(",", ":"))
        else:
            text = str(raw_value)
        if url_encode:
            return quote(text, safe="")
        if "\r" in text or "\n" in text:
            raise _extension_error(
                tool_name, f"{label} 占位参数 {{{parameter}}} 含换行"
            )
        return text

    return _ARG_PLACEHOLDER_RE.sub(_replace, template)


def _truncate_snippet(body: bytes) -> str:
    """截断响应体为错误文本片段（不含响应头）。

    Args:
        body: 响应体原始字节。

    Returns:
        去空白并截断后的文本片段。
    """
    text = body[:_ERROR_SNIPPET_MAX_BYTES].decode("utf-8", errors="replace").strip()
    return text[:_ERROR_SNIPPET_MAX_CHARS]


def _extract_result(payload: Any, result_path: str, tool_name: str) -> Any:
    """按点路径从响应 JSON 提取结果。

    Args:
        payload: 已解析的响应 JSON。
        result_path: 点路径（空 = 整个文档）。
        tool_name: 工具名（错误文本定位面）。

    Returns:
        提取到的 JSON 值。

    Raises:
        BusinessException: 路径未命中。
    """
    if not result_path:
        return payload
    current: Any = payload
    for segment in result_path.split("."):
        if not isinstance(current, Mapping) or segment not in current:
            raise _extension_error(tool_name, f"result_path 未命中: {result_path}")
        current = current[segment]
    return current


async def _execute_http_call(
    *,
    tool_name: str,
    target: HttpToolTargetModel,
    resolved_headers: Mapping[str, str],
    url_placeholder_names: set[str],
    args: dict[str, Any],
    http_client_factory: HttpClientFactory,
    clock: ToolClock,
) -> dict[str, Any]:
    """执行一次外部 HTTP 工具调用（失败恒转受控业务异常）。

    Args:
        tool_name: 工具名。
        target: HTTP 目标端点。
        resolved_headers: 环境变量已解析的请求头。
        url_placeholder_names: URL 占位引用的参数名（GET 查询面排除项）。
        args: 调用参数。
        http_client_factory: 共享 HTTP 客户端工厂。
        clock: 时钟注入面（结果时间戳）。

    Returns:
        成功结果 dict（``text`` 为提取后的文本；另含状态码与时间戳）。

    Raises:
        BusinessException: 传输失败 / 超时 / 非 2xx / 响应超限 / 非 JSON /
            result_path 未命中 / 占位值含换行。
    """
    payload = {key: value for key, value in args.items() if value is not None}
    url = _render_placeholders(
        target.url, payload, tool_name, "url", url_encode=True
    )
    headers = {
        header_name: _render_placeholders(
            header_value,
            payload,
            tool_name,
            f"请求头 {header_name}",
            url_encode=False,
        )
        for header_name, header_value in resolved_headers.items()
    }
    timeout = httpx.Timeout(target.timeout_ms / 1000)
    body = bytearray()
    try:
        client = http_client_factory()
        request_kwargs: dict[str, Any] = {
            "headers": headers,
            "timeout": timeout,
            "follow_redirects": False,
        }
        if target.method == "GET":
            query = {
                key: value
                for key, value in payload.items()
                if key not in url_placeholder_names
            }
            request_kwargs["params"] = query or None
        else:
            request_kwargs["json"] = payload
        async with client.stream(target.method, url, **request_kwargs) as response:
            status_code = response.status_code
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                if not 200 <= status_code < 300:
                    if len(body) >= _ERROR_SNIPPET_MAX_BYTES:
                        break
                elif len(body) > target.response_max_bytes:
                    await response.aclose()
                    raise _extension_error(
                        tool_name, f"响应体超过 {target.response_max_bytes} 字节上限"
                    )
    except httpx.TimeoutException as exc:
        raise _extension_error(
            tool_name, f"请求超时（{target.timeout_ms}ms）"
        ) from exc
    except (httpx.HTTPError, httpx.InvalidURL, UnicodeError) as exc:
        raise _extension_error(
            tool_name, f"请求失败: {type(exc).__name__}"
        ) from exc

    body_bytes = bytes(body)
    if not 200 <= status_code < 300:
        snippet = _truncate_snippet(body_bytes)
        suffix = f": {snippet}" if snippet else ""
        raise _extension_error(
            tool_name, f"返回非成功状态 {status_code}{suffix}"
        )
    try:
        decoded = json.loads(body_bytes)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise _extension_error(tool_name, "响应不是合法 JSON") from exc
    extracted = _extract_result(decoded, target.result_path, tool_name)
    if isinstance(extracted, str):
        text = extracted
    else:
        text = json.dumps(extracted, ensure_ascii=False, separators=(",", ":"))
    return {
        "text": text,
        "status_code": status_code,
        "fetched_at_ms": clock(),
    }


def build_extension_tool(
    spec: ExtensionTool,
    target: HttpToolTargetModel,
    *,
    http_client_factory: HttpClientFactory,
    clock: ToolClock | None = None,
) -> tuple[ToolSpecModel, ToolExecutor]:
    """构建外部 HTTP 工具（规格 + 执行器——registry.register 装配面）。

    Args:
        spec: 外部工具声明（name / description / parameters）。
        target: HTTP 目标端点（构造期解析 ``{env:}`` 占位）。
        http_client_factory: 共享 HTTP 客户端工厂（执行期调用）。
        clock: 时钟注入面（None = core.now_ms）。

    Returns:
        ``(spec, executor)`` 二元组（经 registry 注册）。

    Raises:
        BusinessException: 占位未声明 / 环境变量缺失 / header 换行。
    """
    declared = set(spec.parameters.properties)
    _ensure_declared(target.url, declared, spec.name, "url")
    for header_name, header_value in target.headers.items():
        _ensure_declared(header_value, declared, spec.name, f"请求头 {header_name}")
    resolved_headers = _resolve_env_headers(target.headers, spec.name)
    args_model = _build_args_model(spec.parameters)
    tool_spec = ToolSpecModel(
        name=spec.name,
        description=spec.description,
        args_schema=ToolParametersSchema.model_validate(
            spec.parameters.model_dump(exclude_none=True, by_alias=True)
        ),
        args_model=args_model,
    )
    url_placeholder_names = _declared_placeholders(target.url)
    resolved_clock: ToolClock = clock if clock is not None else now_ms

    async def _execute(args: dict[str, Any]) -> dict[str, Any]:
        """执行器闭包（委托 HTTP 调用；失败经 registry 归一）。"""
        return await _execute_http_call(
            tool_name=spec.name,
            target=target,
            resolved_headers=resolved_headers,
            url_placeholder_names=url_placeholder_names,
            args=args,
            http_client_factory=http_client_factory,
            clock=resolved_clock,
        )

    return tool_spec, _execute

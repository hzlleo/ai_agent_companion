#!/usr/bin/env python3
"""``.example`` 配置文件生成器——辅助函数。

提供从 Pydantic ``BaseSettings`` / ``BaseModel`` 字段信息中提取 env-var 名、
描述、默认值字符串的通用辅助函数，供 ``generate_examples_env`` /
``generate_examples_yaml`` / ``generate_examples_json`` 子模块使用。

运行契约：经 ``pixi run gen-examples``（backend pixi 环境）执行；
backend pixi 的 activation 已设置 ``PYTHONPATH=$PIXI_PROJECT_ROOT/src``，
``packages.harness.*`` 可直接导入。
"""

from __future__ import annotations

import inspect
from typing import Any, get_args, get_origin

from pydantic import BaseModel, SecretStr
from pydantic_settings import BaseSettings

#: 敏感字段关键词（小写匹配）——这些字段的默认值在 .env.example 中始终为空
_SENSITIVE_PARTS: tuple[str, ...] = ("password", "secret", "token", "api_key")


def _is_sensitive_key(key: str) -> bool:
    """判断环境变量名是否为敏感字段（密码、密钥等）。

    Args:
        key: 环境变量名。

    Returns:
        命中敏感关键词返回 True。
    """
    normalized = key.lower()
    return any(part in normalized for part in _SENSITIVE_PARTS)


def _stringify_default(value: Any, env_var: str) -> str:
    """将字段默认值转换为 .env 字符串格式。

    - ``SecretStr`` → 始终输出空字符串（不泄露密钥明文）
    - ``bool`` → ``"true"`` / ``"false"``
    - 敏感字段（含 password/secret/token/api_key）→ 空字符串
    - ``None`` → 空字符串
    - 其他 → ``str(value)``

    Args:
        value: 字段默认值。
        env_var: 对应的环境变量名（敏感判定用）。

    Returns:
        .env 兼容的字符串值。
    """
    if isinstance(value, SecretStr):
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return ""
    if _is_sensitive_key(env_var):
        return ""
    return str(value)


def _get_env_var_name(field_name: str, cls: type[BaseSettings]) -> str:
    """根据字段名和类的 env_prefix 推导环境变量名。

    pydantic-settings 的 env_prefix 拼接规则：``env_prefix +
    field_name.upper()``。字段声明了 ``validation_alias``（含
    AliasChoices）时，实际生效的环境变量名来自别名集合——此时优先取
    带 env_prefix 前缀的别名，无前缀别名时取首个别名，确保生成的
    变量名一定是真实可生效的 env 键。

    Args:
        field_name: 字段名。
        cls: BaseSettings 子类。

    Returns:
        该字段对应的环境变量名。
    """
    env_prefix = ""
    model_config = cls.model_config
    if isinstance(model_config, dict):
        env_prefix = model_config.get("env_prefix", "")

    field_info = cls.model_fields.get(field_name)
    validation_alias = getattr(field_info, "validation_alias", None) if field_info else None
    if validation_alias is not None:
        alias_names = [
            alias for alias in _iter_alias_names(validation_alias) if isinstance(alias, str)
        ]
        if alias_names:
            prefixed = [
                alias for alias in alias_names if env_prefix and alias.startswith(env_prefix)
            ]
            return (prefixed or alias_names)[0].upper()

    return f"{env_prefix}{field_name}".upper()


def _iter_alias_names(validation_alias: Any) -> list[str]:
    """展开 ``validation_alias`` 为字符串别名列表（AliasChoices / 单别名兼容）。

    Args:
        validation_alias: ``AliasChoices`` 实例或单个 str 别名。

    Returns:
        别名字符串列表（非字符串成员过滤后为空列表）。
    """
    choices = getattr(validation_alias, "choices", None)
    if choices is None:
        return [validation_alias] if isinstance(validation_alias, str) else []
    return [choice for choice in choices if isinstance(choice, str)]


def _get_field_description(field_info: Any) -> str:
    """从 FieldInfo 提取 description（可能为 None）。

    Args:
        field_info: pydantic FieldInfo。

    Returns:
        描述字符串（缺失为空串）。
    """
    return getattr(field_info, "description", None) or ""


def _is_scalar_env_field(field_info: Any) -> bool:
    """字段是否适用标量 env var 形态。

    嵌套 ``BaseModel`` 结构化配置与标量容器（``list[str]`` 等）经
    config.yaml / extensions_config.json 下发，无安全的
    ``PREFIX_NAME=value`` 标量形态——env 生成应跳过。

    Args:
        field_info: pydantic FieldInfo。

    Returns:
        标量形态返回 ``True``；嵌套模型 / 容器返回 ``False``。
    """
    annotation = field_info.annotation
    if inspect.isclass(annotation) and issubclass(annotation, BaseModel):
        return False
    origin = get_origin(annotation)
    if origin in (list, tuple, set, frozenset, dict):
        return False
    if origin is not None:
        # Optional[scalar] / Literal[...] / Union[scalar, ...] 仍是标量形态；
        # Union 内嵌 BaseModel（嵌套模型可选字段）非标量
        args = get_args(annotation)
        if any(inspect.isclass(a) and issubclass(a, BaseModel) for a in args):
            return False
    return True


__all__ = [
    "_is_scalar_env_field",
    "_SENSITIVE_PARTS",
    "_is_sensitive_key",
    "_stringify_default",
    "_get_env_var_name",
    "_get_field_description",
]

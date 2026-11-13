"""LLM 注册表管理 DTO——/api/v1/models 管理面的传输契约（P2 chunk 1）。

契约分层：

    - :class:`ModelEntryCreateDTO` —— 创建请求（校验面：name 非空唯一 /
      base_url http(s) / model 非空 / api_key_env 可选 / roles 可选 /
      concurrency 可选）；
    - :class:`ModelEntryUpdateDTO` —— 部分更新请求（name 不可变——字段
      不存在，请求体携带即 400；``concurrency`` 显式 null = 重置为全局
      默认，缺席 = 不变）；
    - :class:`ModelEntryMaskedDTO`  —— 条目读取面（脱敏：只携带
      ``api_key_env`` 环境变量名，永不携带密钥值）；
    - :class:`RegistryStateDTO`     —— 注册表状态（每次写操作的返回形态）；
    - :class:`RoleBindingDTO`       —— 角色绑定行（bound / unbound）。

密钥不变式：请求面经 ``extra="forbid"`` 拒绝任何未声明键（含 ``api_key``
密钥字段——写路径不接受密钥明文）；响应面条目只回环境变量名。
"""

from __future__ import annotations

from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator

from packages.harness.config.model_entry import ModelRole

__all__ = [
    "ModelEntryCreateDTO",
    "ModelEntryUpdateDTO",
    "ModelEntryMaskedDTO",
    "RegistryStateDTO",
    "RoleBindingDTO",
]


def _validate_http_base_url(value: str) -> str:
    """校验 base_url 为 http(s) URL（管理面写路径的唯一 scheme 值域）。

    Args:
        value: 原始字段值。

    Returns:
        去除首尾空白后的 URL。

    Raises:
        ValueError: scheme 非 http/https 或缺少 host。
    """
    stripped = value.strip()
    parsed = urlparse(stripped)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError(f"base_url 必须为 http(s) URL（实际: {value!r}）")
    return stripped


def _strip_optional(value: str) -> str:
    """可选字符串字段去空白（空串合法——免鉴权 / 不变语义由字段自持）。

    Args:
        value: 原始字段值。

    Returns:
        去除首尾空白后的值。
    """
    return value.strip()


class ModelEntryCreateDTO(BaseModel):
    """模型条目创建请求（写面）。

    Attributes:
        name: 条目唯一标识（非空；全局唯一校验在服务层——冲突 409）。
        provider: provider 类型（默认 ``openai_compatible``）。
        base_url: API 基础 URL（http(s)；OpenAI 兼容端点根路径含 ``/v1``）。
        api_key_env: API Key 环境变量名（空 = 本地免鉴权端点；**只接受
            变量名，永不接受密钥明文**）。
        model: 模型名（非空；传给 provider 的 model 参数）。
        roles: 条目承接的角色集（默认空——仅供显式角色绑定引用）。
        concurrency: 条目并发上限（None = 全局默认
            ``POOLS_LLM_SEMAPHORE_LIMIT``）。注：本工程注册表无 per-entry
            超时字段——请求超时统一走全局 ``POOLS_LLM_REQUEST_TIMEOUT_SECONDS``。
        multimodal: 是否支持多模态输入（look_at 链路回退判据）。
    """

    model_config = ConfigDict(extra="forbid", protected_namespaces=())

    name: str = Field(min_length=1, max_length=128, description="条目唯一标识")
    provider: str = Field(default="openai_compatible", description="provider 类型")
    base_url: str = Field(min_length=1, description="API 基础 URL（http(s)）")
    api_key_env: str = Field(default="", max_length=128, description="API Key 环境变量名（空=免鉴权）")
    model: str = Field(min_length=1, max_length=256, description="模型名（provider model 参数）")
    roles: list[ModelRole] = Field(default_factory=list, description="条目承接的角色集")
    concurrency: int | None = Field(default=None, ge=1, description="条目并发上限（None=全局默认）")
    multimodal: bool = Field(default=False, description="是否支持多模态输入")

    @field_validator("name", "model")
    @classmethod
    def _strip_required(cls, value: str) -> str:
        """必填字符串去空白 + 拒绝空值。

        Args:
            value: 原始字段值。

        Returns:
            去除空白后的值。

        Raises:
            ValueError: 值为空 / 纯空白。
        """
        stripped = value.strip()
        if not stripped:
            raise ValueError("条目关键字段（name/model）不能为空")
        return stripped

    @field_validator("base_url")
    @classmethod
    def _validate_base_url(cls, value: str) -> str:
        """base_url 去 whitespace + http(s) scheme 校验。

        Args:
            value: 原始字段值。

        Returns:
            校验通过的 URL。

        Raises:
            ValueError: scheme 非法 / 缺少 host / 纯空白。
        """
        return _validate_http_base_url(value)

    @field_validator("api_key_env")
    @classmethod
    def _strip_api_key_env(cls, value: str) -> str:
        """api_key_env 去空白（密钥变量名收口——永不接受密钥明文字段）。

        Args:
            value: 原始字段值。

        Returns:
            去除空白后的变量名。
        """
        return _strip_optional(value)


class ModelEntryUpdateDTO(BaseModel):
    """模型条目部分更新请求（``name`` 不可变——字段不存在，请求体携带
    即经 ``extra="forbid"`` 拒绝 400）。

    字段 null 语义（逐字段文档化）：

        - ``provider`` / ``base_url`` / ``api_key_env`` / ``model`` /
          ``multimodal``：null / 缺席 = 不变（``api_key_env`` 置空传
          ``""``——免鉴权端点显式声明）；
        - ``roles``：null / 缺席 = 不变；清空传 ``[]``；
        - ``concurrency``：**缺席 = 不变；显式 null = 重置为全局默认**
          （经 ``model_fields_set`` 区分——唯一可空重置旋钮）。

    Attributes:
        provider: 新 provider 类型（None = 不变）。
        base_url: 新 API 基础 URL（None = 不变；提供时须 http(s)）。
        api_key_env: 新 API Key 环境变量名（None = 不变；"" = 免鉴权）。
        model: 新模型名（None = 不变）。
        roles: 新角色集（None = 不变；[] = 清空声明）。
        concurrency: 新并发上限（缺席 = 不变；显式 null = 重置全局默认）。
        multimodal: 新多模态标志（None = 不变）。
    """

    model_config = ConfigDict(extra="forbid", protected_namespaces=())

    provider: str | None = Field(default=None, description="新 provider 类型（None=不变）")
    base_url: str | None = Field(default=None, description="新 API 基础 URL（None=不变）")
    api_key_env: str | None = Field(
        default=None, max_length=128, description="新 API Key 环境变量名（None=不变；空串=免鉴权）"
    )
    model: str | None = Field(default=None, min_length=1, max_length=256, description="新模型名（None=不变）")
    roles: list[ModelRole] | None = Field(default=None, description="新角色集（None=不变；空列表=清空）")
    concurrency: int | None = Field(
        default=None, ge=1, description="新并发上限（缺席=不变；显式 null=重置全局默认）"
    )
    multimodal: bool | None = Field(default=None, description="新多模态标志（None=不变）")

    @field_validator("base_url")
    @classmethod
    def _validate_base_url(cls, value: str | None) -> str | None:
        """base_url 提供时校验 http(s)（None 透传——不变语义）。

        Args:
            value: 原始字段值。

        Returns:
            校验通过的 URL 或 None。

        Raises:
            ValueError: 提供了非 http(s) URL。
        """
        if value is None:
            return None
        return _validate_http_base_url(value)

    @field_validator("model")
    @classmethod
    def _strip_model(cls, value: str | None) -> str | None:
        """model 提供时去空白 + 拒绝空值（None 透传）。

        Args:
            value: 原始字段值。

        Returns:
            校验通过的模型名或 None。

        Raises:
            ValueError: 提供了空 / 纯空白值。
        """
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("model 字段提供时不能为空")
        return stripped

    @field_validator("api_key_env")
    @classmethod
    def _strip_api_key_env(cls, value: str | None) -> str | None:
        """api_key_env 提供时去空白（None 透传——不变语义）。

        Args:
            value: 原始字段值。

        Returns:
            去除空白后的变量名或 None。
        """
        if value is None:
            return None
        return _strip_optional(value)


class ModelEntryMaskedDTO(BaseModel):
    """模型条目读取面（脱敏）——只携带 ``api_key_env`` 环境变量名，
    **永不携带密钥值**（密钥仅经 .env / 进程环境变量进入，运行时在
    wire 构建点解析）；不含端点健康状态（管理面不探测端点）。

    Attributes:
        name: 条目唯一标识。
        provider: provider 类型。
        base_url: API 基础 URL。
        api_key_env: API Key 环境变量名（空 = 免鉴权端点）。
        model: 模型名。
        roles: 条目声明的角色集。
        resolved_roles: 解析器当前实际解析到本条目的角色集（显式绑定 +
            声明先注册者胜出的合并结果——解析状态面）。
        concurrency: 条目并发上限（None = 全局默认）。
        multimodal: 是否支持多模态输入。
    """

    model_config = ConfigDict(protected_namespaces=())

    name: str = Field(description="条目唯一标识")
    provider: str = Field(description="provider 类型")
    base_url: str = Field(description="API 基础 URL")
    api_key_env: str = Field(description="API Key 环境变量名（空=免鉴权；永不携带密钥值）")
    model: str = Field(description="模型名")
    roles: list[str] = Field(default_factory=list, description="条目声明的角色集")
    resolved_roles: list[str] = Field(
        default_factory=list, description="解析器当前解析到本条目的角色集"
    )
    concurrency: int | None = Field(default=None, description="条目并发上限（None=全局默认）")
    multimodal: bool = Field(default=False, description="是否支持多模态输入")


class RegistryStateDTO(BaseModel):
    """注册表状态（读取面 + 每次写操作的返回形态）。

    Attributes:
        entries: 当前注册表条目（脱敏，声明顺序）。
        role_bindings: 显式角色绑定（角色 → 条目名；优先于条目声明）。
    """

    entries: list[ModelEntryMaskedDTO] = Field(default_factory=list, description="注册表条目（脱敏）")
    role_bindings: dict[str, str] = Field(
        default_factory=dict, description="显式角色绑定（角色 → 条目名）"
    )


class RoleBindingDTO(BaseModel):
    """角色绑定行（五角色逐一呈现 bound / unbound）。

    Attributes:
        role: 角色名（chat / duty / dream / look_at / embed）。
        bound: 是否已绑定条目（显式绑定或条目声明任一命中）。
        entry_name: 绑定的条目名（未绑定为 None）。
    """

    role: str = Field(description="角色名")
    bound: bool = Field(description="是否已绑定条目")
    entry_name: str | None = Field(default=None, description="绑定的条目名（未绑定为 None）")

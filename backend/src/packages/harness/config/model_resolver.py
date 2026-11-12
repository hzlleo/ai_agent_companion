"""模型解析器——角色 → 模型条目的运行时裁决（热重建安全）。

解析序（单一真相，文档化于 docs/backend/config.md）::

    resolve(role)
        ↓ ① 显式角色绑定（llm.role_bindings[role] → 按条目名查表）
        ↓ ② 条目自身 roles 声明（注册表顺序，首个声明该角色的条目胜出；
    :      同一角色被多个条目重复声明时告警并保持先注册者）
        ↓ 未命中 → BusinessException(MODEL_NOT_FOUND, details 含角色名)

热重建语义：``rebuild`` 整体替换内部索引（条目表 + 角色→条目索引），
已在途的已解析条目引用不受影响（条目为不可变 Pydantic 模型）；调用方
（loader 重载 / 配置推送通道）在任何时刻 rebuild 均安全。单事件循环内
使用，索引替换为原子引用赋值，无需加锁。

全局单例：``get_model_resolver()`` 返回进程级解析器；loader 加载
config.yaml 后经 ``rebuild_model_resolver`` 重建。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from loguru import logger

from packages.harness.config.model_entry import (
    SUPPORTED_MODEL_ROLES,
    LlmModelEntry,
    ModelRole,
)
from packages.harness.core.errors import BusinessException, ErrorCode

__all__ = [
    "ModelResolver",
    "get_model_resolver",
    "rebuild_model_resolver",
]


class ModelResolver:
    """角色 → 模型条目解析器（热重建安全）。

    索引构建规则：

        - 条目按注册表顺序建立 ``name → entry`` 表（重名条目后者覆盖
          前者并告警——配置错误尽早暴露）；
        - ``role_bindings`` 中的显式绑定优先（角色 → 条目名 → 查表；
          绑定指向不存在的条目名 → 告警并视为未绑定，回退条目声明）；
        - 条目 ``roles`` 声明按顺序扫描，某角色已被索引则跳过并告警
          （先注册者胜出，跨调用稳定）。
    """

    def __init__(
        self,
        models: Sequence[LlmModelEntry] = (),
        role_bindings: Mapping[str, str] | None = None,
    ) -> None:
        """初始化解析器并构建索引。

        Args:
            models: 模型条目序列（注册表顺序即声明顺序）。
            role_bindings: 显式角色绑定（角色 → 条目名）。
        """
        self._models: list[LlmModelEntry] = []
        self._by_name: dict[str, LlmModelEntry] = {}
        self._by_role: dict[str, LlmModelEntry] = {}
        self.rebuild(models, role_bindings)

    def rebuild(
        self,
        models: Sequence[LlmModelEntry],
        role_bindings: Mapping[str, str] | None = None,
    ) -> None:
        """整体重建索引（热重建入口——任意时刻调用安全）。

        Args:
            models: 新的模型条目序列。
            role_bindings: 新的显式角色绑定（None = 无显式绑定）。
        """
        by_name: dict[str, LlmModelEntry] = {}
        for entry in models:
            existing = by_name.get(entry.name)
            if existing is not None:
                logger.warning("模型注册表存在重名条目，后者覆盖前者: name={}", entry.name)
            by_name[entry.name] = entry

        by_role: dict[str, LlmModelEntry] = {}
        bindings = role_bindings or {}
        for role, entry_name in bindings.items():
            if role not in SUPPORTED_MODEL_ROLES:
                logger.warning("显式角色绑定含未知角色，忽略: role={}", role)
                continue
            bound = by_name.get(entry_name)
            if bound is None:
                logger.warning(
                    "显式角色绑定指向不存在的条目名，按未绑定处理: role={}, entry={}",
                    role,
                    entry_name,
                )
                continue
            by_role[role] = bound

        for entry in models:
            for role in entry.roles:
                role_value = role.value if isinstance(role, ModelRole) else str(role)
                if role_value in by_role:
                    logger.warning(
                        "角色已被更早的条目/显式绑定承接，保持先注册者: role={}, "
                        "existing={}, skipped={}",
                        role_value,
                        by_role[role_value].name,
                        entry.name,
                    )
                    continue
                by_role[role_value] = entry

        # 原子引用替换——在途 resolve 不受半重建状态影响
        self._models = list(models)
        self._by_name = by_name
        self._by_role = by_role
        logger.info(
            "模型解析器索引已重建: models={}, roles={}",
            len(self._models),
            sorted(self._by_role),
        )

    def resolve(self, role: ModelRole | str) -> LlmModelEntry:
        """按角色解析模型条目（缺失即业务异常）。

        Args:
            role: 模型角色（枚举成员或角色名字符串）。

        Returns:
            该角色绑定的 :class:`LlmModelEntry`。

        Raises:
            BusinessException: 角色未绑定任何条目（``MODEL_NOT_FOUND``，
                details 含角色名与已绑定角色清单）。
        """
        entry = self.resolve_optional(role)
        if entry is None:
            role_value = role.value if isinstance(role, ModelRole) else str(role)
            raise BusinessException(
                code=ErrorCode.MODEL_NOT_FOUND,
                details={
                    "reason": "角色未绑定模型条目",
                    "role": role_value,
                    "bound_roles": sorted(self._by_role),
                    "known_entries": [entry.name for entry in self._models],
                },
            )
        return entry

    def resolve_optional(self, role: ModelRole | str) -> LlmModelEntry | None:
        """按角色解析模型条目（缺失返回 None）。

        Args:
            role: 模型角色。

        Returns:
            绑定的条目；未绑定返回 ``None``。
        """
        role_value = role.value if isinstance(role, ModelRole) else str(role)
        return self._by_role.get(role_value)

    def get(self, name: str) -> LlmModelEntry | None:
        """按条目名查表。

        Args:
            name: 条目唯一标识。

        Returns:
            条目实例；不存在返回 ``None``。
        """
        return self._by_name.get(name)

    @property
    def models(self) -> list[LlmModelEntry]:
        """当前注册表快照（声明顺序）。"""
        return list(self._models)

    @property
    def bound_roles(self) -> list[str]:
        """已绑定角色名快照（字典序）。"""
        return sorted(self._by_role)


# ---------------------------------------------------------------------------
# 全局单例——loader 加载 config.yaml 后重建；空注册表为初始态
# ---------------------------------------------------------------------------

_default_resolver = ModelResolver()


def get_model_resolver() -> ModelResolver:
    """返回进程级模型解析器单例。

    Returns:
        :class:`ModelResolver` 实例（对象身份恒定，``rebuild`` 原地重建索引）。
    """
    return _default_resolver


def rebuild_model_resolver(
    models: Sequence[LlmModelEntry],
    role_bindings: Mapping[str, str] | None = None,
) -> ModelResolver:
    """重建全局解析器索引（loader 重载 / 配置推送通道调用）。

    Args:
        models: 新的模型条目序列。
        role_bindings: 新的显式角色绑定。

    Returns:
        重建后的全局 :class:`ModelResolver`（与单例同一对象）。
    """
    _default_resolver.rebuild(models, role_bindings)
    return _default_resolver

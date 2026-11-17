"""搜索运营配置测试——WebSearchSettings env 面 + SearchConfig 节模型。

覆盖矩阵（镜像 test_voice_settings 范式——monkeypatch env 逐键 +
build_isolated_settings 透传 + 边界值）：SEARCH_ 前缀逐键覆写 / 默认值 /
越界值域拒绝（预算 ≥1）；条目 kind 缺省物化（base_url / api_key_env /
model 三缝 + 显式值保留）；SearchConfig web_search 子块的 yaml 键优先
于 env（分层加载链——yaml 最高层）；Settings 聚合嵌入 + build_
isolated_settings search 参数透传与缺省。
"""

from __future__ import annotations

import pytest

from packages.harness.config.models import (
    SEARCH_PROVIDER_KINDS,
    SearchConfig,
    SearchProviderEntry,
    Settings,
    WebSearchSettings,
    default_search_model_for_kind,
)
from packages.harness.config.settings import build_isolated_settings


class TestWebSearchSettingsEnvFace:
    async def test_defaults(self) -> None:
        """缺省值：两级开关关闭 + 预算 50/200 + 冷却 60s。"""
        settings = WebSearchSettings()
        assert settings.enabled is False
        assert settings.tool_enabled is False
        assert settings.daily_budget_per_persona == 50
        assert settings.daily_budget_global == 200
        assert settings.cooldown_seconds == 60

    async def test_env_prefix_key_overrides(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """SEARCH_ 前缀逐键覆写（env 面生效）。"""
        monkeypatch.setenv("SEARCH_ENABLED", "true")
        monkeypatch.setenv("SEARCH_TOOL_ENABLED", "true")
        monkeypatch.setenv("SEARCH_DAILY_BUDGET_PER_PERSONA", "7")
        monkeypatch.setenv("SEARCH_DAILY_BUDGET_GLOBAL", "9")
        monkeypatch.setenv("SEARCH_COOLDOWN_SECONDS", "11")
        settings = WebSearchSettings()
        assert settings.enabled is True
        assert settings.tool_enabled is True
        assert settings.daily_budget_per_persona == 7
        assert settings.daily_budget_global == 9
        assert settings.cooldown_seconds == 11

    async def test_budget_bounds_rejected(self) -> None:
        """预算越界（<1）：ValidationError（装配错误面）。"""
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            WebSearchSettings(daily_budget_per_persona=0)
        with pytest.raises(ValidationError):
            WebSearchSettings(daily_budget_global=0)

    async def test_build_isolated_settings_passthrough(self) -> None:
        """build_isolated_settings：显式实例原样嵌入（不重读 env）。"""
        search = WebSearchSettings(enabled=True, tool_enabled=True, cooldown_seconds=3)
        isolated = build_isolated_settings(search=search)
        assert isolated.search is search
        default_isolated = build_isolated_settings()
        assert default_isolated.search.enabled is False

    async def test_settings_aggregate_embeds_search(self) -> None:
        """Settings 聚合器嵌入 search 分节（缺省构造读 env 面）。"""
        settings = Settings()
        assert isinstance(settings.search, WebSearchSettings)
        assert settings.search.enabled is False


class TestSearchProviderEntry:
    async def test_kind_defaults_materialized(self) -> None:
        """kind 缺省物化：空三缝按 kind 落官方端点 / 变量名 / 模型码。"""
        bocha = SearchProviderEntry(name="b", kind="bocha")
        assert bocha.base_url == "https://api.bochaai.com"
        assert bocha.api_key_env == "SEARCH_BOCHA_API_KEY"
        assert bocha.model == "web-search"
        zhipu = SearchProviderEntry(name="z", kind="zhipu")
        assert zhipu.base_url == "https://open.bigmodel.cn"
        assert zhipu.api_key_env == "SEARCH_ZHIPU_API_KEY"
        assert zhipu.model == "search_std"

    async def test_explicit_values_preserved(self) -> None:
        """显式值保留（base_url / api_key_env / model / 条数 / 超时）。"""
        entry = SearchProviderEntry(
            name="custom",
            kind="zhipu",
            base_url="http://proxy.test",
            api_key_env="CUSTOM_ZHIPU_KEY",
            model="search_pro",
            max_results=20,
            timeout_seconds=5.0,
        )
        assert entry.base_url == "http://proxy.test"
        assert entry.api_key_env == "CUSTOM_ZHIPU_KEY"
        assert entry.model == "search_pro"
        assert entry.max_results == 20
        assert entry.timeout_seconds == 5.0

    async def test_bounds_rejected(self) -> None:
        """边界：条数 1-20 / 超时 >0 / 未知 kind 拒绝。"""
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            SearchProviderEntry(name="x", kind="bocha", max_results=21)
        with pytest.raises(ValidationError):
            SearchProviderEntry(name="x", kind="bocha", timeout_seconds=0)
        with pytest.raises(ValidationError):
            SearchProviderEntry.model_validate({"name": "x", "kind": "bing"})

    async def test_default_model_for_kind_matrix(self) -> None:
        """default_search_model_for_kind：双 kind 值 + 未知 kind 抛错。"""
        assert default_search_model_for_kind("bocha") == "web-search"
        assert default_search_model_for_kind("zhipu") == "search_std"
        with pytest.raises(ValueError, match="未知搜索"):
            default_search_model_for_kind("bing")
        assert set(SEARCH_PROVIDER_KINDS) == {"bocha", "zhipu"}


class TestSearchConfigSection:
    async def test_yaml_subblock_overrides_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """分层链：yaml 子块键优先于 SEARCH_* env（缺省键回落 env）。"""
        monkeypatch.setenv("SEARCH_ENABLED", "false")
        monkeypatch.setenv("SEARCH_DAILY_BUDGET_GLOBAL", "77")
        config = SearchConfig.model_validate(
            {"web_search": {"enabled": True, "tool_enabled": True}}
        )
        assert config.web_search.enabled is True  # yaml 键覆盖 env
        assert config.web_search.tool_enabled is True
        assert config.web_search.daily_budget_global == 77  # 缺省键回落 env

    async def test_empty_section_is_legal(self) -> None:
        """空段合法：零 provider + env 面运营值（零配置形态）。"""
        config = SearchConfig.model_validate({})
        assert config.providers == []
        assert config.web_search.enabled is False

    async def test_providers_roundtrip(self) -> None:
        """providers 列表回读（dict 输入 → 条目模型——loader 消费形态）。"""
        config = SearchConfig.model_validate(
            {
                "providers": [
                    {"name": "p1", "kind": "bocha"},
                    {"name": "p2", "kind": "zhipu", "model": "search_pro"},
                ]
            }
        )
        assert [entry.name for entry in config.providers] == ["p1", "p2"]
        assert config.providers[1].model == "search_pro"

"""浏览器 sidecar 配置测试——ToolsBrowserSidecarSettings env 面映射 / 默认值 /
聚合。

覆盖矩阵：代码默认值（enabled=False——可选运行时锁定决策；经 delenv
隔离构造）；env 覆写（TOOLS_BROWSER_SIDECAR_ 前缀逐键）；校验（timeout
非正拒绝 / token_env 非空 / base_url 非空）；Settings 聚合嵌入 +
build_isolated_settings 透传（默认关闭 / 注入开启）；conftest 硬覆盖面
（S3 隔离锚点）。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from packages.harness.config.models import Settings, ToolsBrowserSidecarSettings
from packages.harness.config.settings import build_isolated_settings

#: ToolsBrowserSidecarSettings 的全部 env 键（默认值隔离构造用）。
_ALL_ENV_KEYS = (
    "TOOLS_BROWSER_SIDECAR_ENABLED",
    "TOOLS_BROWSER_SIDECAR_BASE_URL",
    "TOOLS_BROWSER_SIDECAR_TOKEN_ENV",
    "TOOLS_BROWSER_SIDECAR_REQUEST_TIMEOUT_SECONDS",
)


def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """删除全部 TOOLS_BROWSER_SIDECAR_ env 键（代码默认值隔离面）。"""
    for key in _ALL_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


class TestDefaults:
    async def test_code_defaults(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """代码默认值：关闭 + 回环端点 + token 间接 + 30s 超时。"""
        _clear_env(monkeypatch)
        settings = ToolsBrowserSidecarSettings()
        assert settings.enabled is False
        assert settings.base_url == "http://127.0.0.1:8766"
        assert settings.token_env == "BROWSER_SIDECAR_TOKEN"
        assert settings.request_timeout_seconds == 30.0

    async def test_aggregated_in_settings(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Settings 聚合：tools_browser_sidecar 分节类型与关闭形态（分节字段级
        默认由 test_code_defaults 的新鲜构造覆盖——聚合器字段缺省实例在模块
        导入期即固化，受 conftest env 影响属预期）。"""
        _clear_env(monkeypatch)
        settings = Settings()
        assert isinstance(settings.tools_browser_sidecar, ToolsBrowserSidecarSettings)
        assert settings.tools_browser_sidecar.enabled is False

    async def test_isolated_settings_default_off(self) -> None:
        """build_isolated_settings：未注入 = 关闭形态。"""
        settings = build_isolated_settings()
        assert settings.tools_browser_sidecar.enabled is False

    async def test_isolated_settings_passthrough(self) -> None:
        """build_isolated_settings：注入开启形态原样嵌入。"""
        settings = build_isolated_settings(
            tools_browser_sidecar=ToolsBrowserSidecarSettings(
                enabled=True,
                base_url="http://127.0.0.1:9999",
                token_env="CUSTOM_BROWSER_SIDECAR_TOKEN",
                request_timeout_seconds=5.0,
            )
        )
        assert settings.tools_browser_sidecar.enabled is True
        assert settings.tools_browser_sidecar.base_url == "http://127.0.0.1:9999"
        assert settings.tools_browser_sidecar.token_env == "CUSTOM_BROWSER_SIDECAR_TOKEN"
        assert settings.tools_browser_sidecar.request_timeout_seconds == 5.0


class TestEnvFace:
    async def test_env_overrides(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """env 覆写：TOOLS_BROWSER_SIDECAR_ 前缀逐键生效。"""
        monkeypatch.setenv("TOOLS_BROWSER_SIDECAR_ENABLED", "true")
        monkeypatch.setenv("TOOLS_BROWSER_SIDECAR_BASE_URL", "http://10.0.0.5:8766")
        monkeypatch.setenv("TOOLS_BROWSER_SIDECAR_TOKEN_ENV", "CUSTOM_BROWSER_SIDECAR_TOKEN")
        monkeypatch.setenv("TOOLS_BROWSER_SIDECAR_REQUEST_TIMEOUT_SECONDS", "8.5")
        settings = ToolsBrowserSidecarSettings()
        assert settings.enabled is True
        assert settings.base_url == "http://10.0.0.5:8766"
        assert settings.token_env == "CUSTOM_BROWSER_SIDECAR_TOKEN"
        assert settings.request_timeout_seconds == 8.5

    async def test_conftest_hard_overrides_active(self) -> None:
        """conftest 硬覆盖：默认关闭 + 测试 token 间接（S3 隔离面）。"""
        settings = ToolsBrowserSidecarSettings()
        assert settings.enabled is False
        assert settings.token_env == "BROWSER_SIDECAR_TEST_TOKEN"


class TestValidation:
    async def test_timeout_must_be_positive(self) -> None:
        """校验：request_timeout_seconds 非正拒绝。"""
        with pytest.raises(ValidationError):
            ToolsBrowserSidecarSettings(request_timeout_seconds=0)
        with pytest.raises(ValidationError):
            ToolsBrowserSidecarSettings(request_timeout_seconds=-1.0)

    async def test_token_env_must_be_non_empty(self) -> None:
        """校验：token_env 空串拒绝（密钥间接面锚点）。"""
        with pytest.raises(ValidationError):
            ToolsBrowserSidecarSettings(token_env="")

    async def test_base_url_must_be_non_empty(self) -> None:
        """校验：base_url 空串拒绝。"""
        with pytest.raises(ValidationError):
            ToolsBrowserSidecarSettings(base_url="")

"""conftest env 硬覆盖回归——裸构造不得受真实工程配置（shell / .env）泄漏。

覆盖两组场景：

    - 会话级硬覆盖生效：不传任何构造参数、不指定 env 文件时，
      ``InjectionGuardSettings()`` / ``ComplianceGuardSettings()`` /
      ``IntimateChannelConfig()`` 取到的是 conftest 写定的测试值；
    - 相反环境值被硬覆盖压回：先用 ``monkeypatch`` 把同一批键设成相反
      值（模拟开发者 shell 导出真实配置），再重放 conftest 的硬覆盖块
      （``runpy`` 执行同一源文件，与 pytest 会话启动同源），裸构造仍取
      测试值——证明调用方无需自设环境即可得到 hermetic 配置。
"""

from __future__ import annotations

import os
import runpy
from pathlib import Path

import pytest

from packages.harness.chat.compliance_models import ComplianceGuardSettings
from packages.harness.chat.injection_models import CounterStyle, InjectionGuardSettings
from packages.harness.llm.intimate_models import IntimateChannelConfig

#: 后端源码根 conftest（会话启动时执行硬覆盖的真实源文件）。
_CONFTEST_PATH = Path(__file__).resolve().parents[2] / "conftest.py"

#: 与 conftest 测试值相反的进程环境值（模拟 shell 泄漏——全部为合法解析值）。
_CONTRARY_ENV: tuple[tuple[str, str], ...] = (
    ("INJECTION_ENABLED", "false"),
    ("INJECTION_COUNTER_STYLE", "tsun"),
    ("INJECTION_THRESHOLD", "9.0"),
    ("INJECTION_ANTI_INJECTION_HINT", "false"),
    ("COMPLIANCE_ENABLED", "false"),
    ("COMPLIANCE_THRESHOLD", "9.0"),
    ("COMPLIANCE_TEMPERATURE_BUMP", "0.9"),
    ("COMPLIANCE_MAX_RETRIES", "0"),
    ("LLM_INTIMATE_ENABLED", "true"),
    ("LLM_INTIMATE_BASE_URL", "http://leaked.test/v1"),
    ("LLM_INTIMATE_API_KEY", "leaked-intimate-key"),
    ("LLM_INTIMATE_MODEL", "leaked-model"),
    ("LLM_INTIMATE_TEMPERATURE", "1.9"),
    ("LLM_INTIMATE_MIN_INTIMACY_LEVEL", "3"),
)


def _assert_test_values() -> None:
    """断言三组裸构造取到 conftest 写定的测试值。"""
    injection = InjectionGuardSettings()
    assert injection.enabled is True
    assert injection.counter_style is CounterStyle.SASSY
    assert injection.threshold == 2.0
    assert injection.anti_injection_hint is True

    compliance = ComplianceGuardSettings()
    assert compliance.enabled is True
    assert compliance.threshold == 2.0
    assert compliance.temperature_bump == 0.2
    assert compliance.max_retries == 1

    intimate = IntimateChannelConfig()
    assert intimate.enabled is False
    assert intimate.base_url == "http://127.0.0.1:9/v1"
    assert intimate.api_key.get_secret_value() == "test-only-llm-intimate-api-key"
    assert intimate.model == "test-intimate-model"
    assert intimate.temperature == 0.8
    assert intimate.min_intimacy_level == 2
    assert intimate.is_configured is True


async def test_bare_settings_take_conftest_test_values() -> None:
    """裸构造取 conftest 测试值（不传参数 / 不读真实 .env）。"""
    _assert_test_values()


async def test_conftest_hard_override_beats_contrary_shell_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """相反环境值先写入进程，再重放硬覆盖块——裸构造仍取测试值。"""
    for key, value in _CONTRARY_ENV:
        monkeypatch.setenv(key, value)
    assert IntimateChannelConfig().model == "leaked-model"

    cwd = Path.cwd()
    try:
        runpy.run_path(str(_CONFTEST_PATH), run_name="conftest_env_replay")
    finally:
        os.chdir(cwd)

    _assert_test_values()

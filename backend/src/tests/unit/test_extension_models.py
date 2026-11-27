"""外部工具扩展模型测试——字段校验矿阵（kind / url / 超时 / 密钥 / 路径）。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from packages.harness.config.models import (
    ExtensionsConfig,
    ExtensionTool,
    HttpToolTargetModel,
)
from packages.harness.tools.tool_models import ToolSpecModel


async def test_kind_http_requires_target() -> None:
    """kind=http 缺少 http 目标：模型校验拒绝（必填面）。"""
    with pytest.raises(ValidationError):
        ExtensionTool(name="t1", description="d")
    valid = ExtensionTool(
        name="t1",
        description="d",
        http=HttpToolTargetModel(url="http://127.0.0.1:8091/x"),
    )
    assert valid.kind == "http"
    assert valid.http is not None
    assert valid.http.method == "POST"
    assert valid.http.timeout_ms == 5000
    assert valid.http.response_max_bytes == 65536


async def test_url_scheme_rejected() -> None:
    """url 仅接受 http/https 且必须含主机名。"""
    for bad in ("ftp://host/x", "file:///tmp/x", "http:///x", "https://"):
        with pytest.raises(ValidationError):
            HttpToolTargetModel(url=bad)
    assert HttpToolTargetModel(url="https://api.example.com/v1").url.startswith("https://")
    assert HttpToolTargetModel(url="http://127.0.0.1:8091/x/{id}").url.endswith("{id}")


async def test_timeout_upper_bound() -> None:
    """timeout_ms 上限 30000（超限拒绝；边界值接受）。"""
    assert HttpToolTargetModel(url="http://h/x", timeout_ms=30000).timeout_ms == 30000
    with pytest.raises(ValidationError):
        HttpToolTargetModel(url="http://h/x", timeout_ms=30001)
    with pytest.raises(ValidationError):
        HttpToolTargetModel(url="http://h/x", timeout_ms=0)


async def test_header_literal_secret_rejected() -> None:
    """header 直接写密钥形态值：拒绝（Bearer / sk- / 超长字面值）。"""
    for bad in (
        "Bearer abc",
        "sk-123456",
        "x" * 33,
    ):
        with pytest.raises(ValidationError):
            HttpToolTargetModel(url="http://h/x", headers={"Authorization": bad})


async def test_header_env_placeholder_accepted() -> None:
    """header 的 {env:VAR} 占位：接受（密钥只存环境变量名）。"""
    target = HttpToolTargetModel(
        url="http://h/x", headers={"Authorization": "{env:CALENDAR_API_TOKEN}"}
    )
    assert target.headers["Authorization"] == "{env:CALENDAR_API_TOKEN}"
    with pytest.raises(ValidationError):
        HttpToolTargetModel(url="http://h/x", headers={"Authorization": "{env:1BAD}"})


async def test_header_secret_rejected_after_env_placeholder_removal() -> None:
    """占位不再豁免剩余字面：前缀 / 超长 / 大小写变体均拒绝。"""
    for bad in (
        "Bearer sk-live-{env:CALENDAR_API_TOKEN}",
        "x" * 62 + "{env:CALENDAR_API_TOKEN}",
        "Basic dXNlcjpwYXNzd29yZA==",
        "a" * 64,
        "bearer abc",
        "BASIC xyz",
        "SK-abc",
    ):
        with pytest.raises(ValidationError):
            HttpToolTargetModel(url="http://h/x", headers={"Authorization": bad})
    accepted = HttpToolTargetModel(
        url="http://h/x", headers={"Authorization": "Bearer {env:CALENDAR_API_TOKEN}"}
    )
    assert accepted.headers["Authorization"] == "Bearer {env:CALENDAR_API_TOKEN}"


async def test_url_userinfo_and_secret_query_rejected() -> None:
    """url 拒绝 userinfo 与查询串中的长字面密钥参数。"""
    for bad in (
        "http://admin:S3cretPassw0rd@10.0.0.1/x",
        "http://10.0.0.1/x?token=abcdefghijklmnop",
        "https://h/x?api_key=abcdefghijklmnop",
        "https://h/x?apikey=abcdefghijklmnop",
        "https://h/x?key=abcdefghijklmnop",
        "https://h/x?secret=abcdefghijklmnop",
    ):
        with pytest.raises(ValidationError):
            HttpToolTargetModel(url=bad)
    assert HttpToolTargetModel(url="https://h/x?token=short").url.endswith("token=short")
    assert HttpToolTargetModel(url="https://h/x?token={token}").url.endswith("token={token}")


async def test_tool_name_pattern_rejected() -> None:
    """ExtensionTool / ToolSpecModel 工具名：空 / CJK / 空格 / 超长拒绝。"""
    for bad in ("", "中文", "has space", "a" * 65, "dot.name"):
        with pytest.raises(ValidationError):
            ExtensionTool(
                name=bad,
                http=HttpToolTargetModel(url="http://127.0.0.1:8091/x"),
            )
    valid = ExtensionTool(
        name="query_local-calendar_1",
        http=HttpToolTargetModel(url="http://127.0.0.1:8091/x"),
    )
    assert valid.name == "query_local-calendar_1"

    for bad_spec in ("", "中文", "has space", "a" * 65):
        with pytest.raises(ValidationError):
            ToolSpecModel(name=bad_spec)
    assert ToolSpecModel(name="get_current_time").name == "get_current_time"


async def test_header_name_rejected() -> None:
    """header 名称非法字符：拒绝。"""
    with pytest.raises(ValidationError):
        HttpToolTargetModel(url="http://h/x", headers={"Bad Header": "ok"})
    with pytest.raises(ValidationError):
        HttpToolTargetModel(url="http://h/x", headers={"Bad:Header": "ok"})


async def test_result_path_shape() -> None:
    """result_path 仅接受空串或点分标识符路径。"""
    assert HttpToolTargetModel(url="http://h/x", result_path="").result_path == ""
    assert HttpToolTargetModel(url="http://h/x", result_path="events").result_path == "events"
    assert (
        HttpToolTargetModel(url="http://h/x", result_path="data.events").result_path
        == "data.events"
    )
    assert HttpToolTargetModel(url="http://h/x", result_path="a-b").result_path == "a-b"
    for bad in (".events", "events.", "a..b"):
        with pytest.raises(ValidationError):
            HttpToolTargetModel(url="http://h/x", result_path=bad)


async def test_method_normalized_and_unknown_rejected() -> None:
    """method 大小写归一；未知方法拒绝。"""
    assert HttpToolTargetModel(url="http://h/x", method="get").method == "GET"
    assert HttpToolTargetModel(url="http://h/x", method="post").method == "POST"
    with pytest.raises(ValidationError):
        HttpToolTargetModel(url="http://h/x", method="PUT")


async def test_extensions_config_defaults_and_example() -> None:
    """扩展配置默认关闭 + 示例工具真实可执行（模型可校验）。"""
    config = ExtensionsConfig()
    assert config.enabled is False
    assert config.allow_override_builtin is False
    assert config.tools == []
    examples = ExtensionsConfig.default_tools()
    assert len(examples) == 1
    parsed = ExtensionsConfig.model_validate({"enabled": True, "tools": examples})
    tool = parsed.tools[0]
    assert tool.name == "query_local_calendar"
    assert tool.http is not None
    assert tool.http.result_path == "events"
    assert tool.http.headers["X-Auth-Token"] == "{env:CALENDAR_API_TOKEN}"
    assert tool.parameters.required == ["date"]

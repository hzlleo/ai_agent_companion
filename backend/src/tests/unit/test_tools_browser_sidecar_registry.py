"""harness/tools 浏览器 sidecar 代理注册测试——fake 客户端回路 + 结果格式化。

覆盖矩阵：八工具注册序；代理回路（ok 透传 / 失败归一 INTERNAL_ERROR）；
本地校验 fail-fast（非法参数零网络往返）；默认值物化经 wire
（read_text max_chars=8000）；read_text 不可信平台内容包裹（标注前缀 +
provenance 字段——记忆域语义镜像）；screenshot 视觉提示（look_at 配对）；
click / fill 原样透传；规格呈现面（名称 / 描述 / schema 形态）。
"""

from __future__ import annotations

from typing import Any

from packages.harness.tools.browser import (
    UNTRUSTED_CONTENT_MARKER,
    build_browser_specs,
    register_browser_sidecar_tools,
)
from packages.harness.tools.browser_models import BROWSER_TOOL_NAMES
from packages.harness.tools.registry import ToolRegistry
from packages.harness.tools.tool_models import ToolResultModel


class FakeBrowserSidecarClient:
    """fake sidecar 客户端（BrowserSidecarClientFace 满足——invoke 可配置）。"""

    def __init__(self) -> None:
        """初始化（成功终态缺省）。"""
        self.invocations: list[tuple[str, dict[str, Any]]] = []
        self.next_result = ToolResultModel(tool="", ok=True, result={})

    @property
    def has_token(self) -> bool:
        """恒持 token（装配面由 app 测试覆盖）。"""
        return True

    async def probe_health(self) -> bool:
        """恒健康（装配面由 app 测试覆盖）。"""
        return True

    async def invoke(self, tool: str, arguments: dict[str, Any]) -> ToolResultModel:
        """fake 执行（记录调用；返回配置终态）。"""
        self.invocations.append((tool, dict(arguments)))
        if not self.next_result.ok:
            return ToolResultModel(
                tool=tool,
                ok=False,
                error=self.next_result.error or "sidecar 工具执行失败",
            )
        return ToolResultModel(
            tool=tool,
            ok=True,
            result=self.next_result.result,
            latency_ms=5,
        )


def _registry_with_fake() -> tuple[ToolRegistry, FakeBrowserSidecarClient]:
    """构造装配好的注册表 + fake 客户端。

    Returns:
        ``(registry, fake)`` 二元组。
    """
    registry = ToolRegistry()
    fake = FakeBrowserSidecarClient()
    register_browser_sidecar_tools(registry, fake)
    return registry, fake


class TestRegistration:
    async def test_all_tools_in_order(self) -> None:
        """注册序：八工具按约定顺序（navigate → evaluate）。"""
        registry, _ = _registry_with_fake()
        assert registry.tool_names == list(BROWSER_TOOL_NAMES)

    async def test_build_browser_specs_matches_registry(self) -> None:
        """规格构建面：build_browser_specs 与注册面同序同集合。"""
        specs = build_browser_specs()
        assert [spec.name for spec in specs] == list(BROWSER_TOOL_NAMES)


class TestProxyLoop:
    async def test_navigate_success_passthrough(self) -> None:
        """navigate 回路：ok=True 结果原样透传（无非工具级格式化）。"""
        registry, fake = _registry_with_fake()
        fake.next_result = ToolResultModel(
            tool="navigate",
            ok=True,
            result={"url": "https://example.com/x", "title": "Example"},
        )
        result = await registry.execute("navigate", {"url": "https://example.com/x"})
        assert result.ok is True
        assert result.result == {"url": "https://example.com/x", "title": "Example"}
        assert fake.invocations == [("navigate", {"url": "https://example.com/x"})]

    async def test_read_text_wrapped_as_untrusted(self) -> None:
        """read_text 回路：原始文本被不可信标注包裹 + provenance 字段。"""
        registry, fake = _registry_with_fake()
        fake.next_result = ToolResultModel(
            tool="read_text",
            ok=True,
            result={
                "url": "https://example.com/page",
                "text": "原始页面正文",
                "truncated": False,
                "char_count": 6,
            },
        )
        result = await registry.execute("read_text", {})
        assert result.ok is True
        wrapped = result.result["text"]
        assert wrapped.startswith(UNTRUSTED_CONTENT_MARKER)
        assert "https://example.com/page" in wrapped
        assert wrapped.endswith("原始页面正文")
        assert "甄别" in wrapped
        assert result.result["provenance"] == "untrusted"
        assert result.result["truncated"] is False

    async def test_screenshot_carries_vision_note(self) -> None:
        """screenshot 回路：结果携带 look_at 配对提示字段。"""
        registry, fake = _registry_with_fake()
        fake.next_result = ToolResultModel(
            tool="screenshot",
            ok=True,
            result={"image_base64": "aW1n", "width": 1280, "height": 720},
        )
        result = await registry.execute("screenshot", {})
        assert result.ok is True
        assert result.result["image_base64"] == "aW1n"
        assert "look_at" in result.result["note"]

    async def test_click_fill_passthrough(self) -> None:
        """click / fill 回路：结果原样透传（零格式化）。"""
        registry, fake = _registry_with_fake()
        fake.next_result = ToolResultModel(
            tool="fill", ok=True, result={"filled": True, "selector": "#input"}
        )
        result = await registry.execute("fill", {"selector": "#input", "value": "v"})
        assert result.ok is True
        assert result.result == {"filled": True, "selector": "#input"}
        # 默认值物化经 wire：read_text 缺省 max_chars=8000
        fake.next_result = ToolResultModel(tool="read_text", ok=True, result={})
        await registry.execute("read_text", {})
        assert fake.invocations[-1] == ("read_text", {"max_chars": 8000})

    async def test_sidecar_failure_normalized(self) -> None:
        """sidecar 失败归一：ok=False → INTERNAL_ERROR 业务异常 → registry ok=False。"""
        registry, fake = _registry_with_fake()
        fake.next_result = ToolResultModel(
            tool="navigate", ok=False, error="目标域名不在允许清单: evil.example.org"
        )
        result = await registry.execute("navigate", {"url": "https://evil.example.org/x"})
        assert result.ok is False
        assert "浏览器工具 navigate 执行失败" in (result.error or "")
        assert "允许清单" in (result.error or "")

    async def test_local_validation_fail_fast(self) -> None:
        """本地校验 fail-fast：非法参数零网络往返（fake 零调用）。"""
        registry, fake = _registry_with_fake()
        result = await registry.execute("navigate", {"url": "javascript:alert(1)"})
        assert result.ok is False
        assert "参数校验失败" in (result.error or "")
        result = await registry.execute("close_tab", {"tab_index": -1})
        assert result.ok is False
        result = await registry.execute("read_text", {"max_chars": 100001})
        assert result.ok is False
        assert fake.invocations == []


class TestSpecPresentation:
    async def test_spec_names_and_descriptions(self) -> None:
        """规格呈现面：八工具名 + 描述含隔离执行说明（LLM 呈现锚点）。"""
        for spec in build_browser_specs():
            assert "经隔离 sidecar 执行" in spec.description or "sidecar" in spec.description
            assert spec.args_model is not None

    async def test_read_text_spec_mentions_untrusted(self) -> None:
        """read_text 规格：描述声明不可信标注（消费面锚点）。"""
        specs = {spec.name: spec for spec in build_browser_specs()}
        assert "不可信" in specs["read_text"].description

    async def test_screenshot_spec_mentions_look_at(self) -> None:
        """screenshot 规格：描述声明 look_at 配对（视觉理解锚点）。"""
        specs = {spec.name: spec for spec in build_browser_specs()}
        assert "look_at" in specs["screenshot"].description

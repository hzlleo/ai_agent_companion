"""harness/multimodal look_at 工具注册测试——registry 集成 / 隔离执行。

覆盖面：注册即出现在 list_specs（duty / persona 工具循环自动获得）；
base64 解码往返（fake 服务）；解码失败 / 参数校验失败 / 服务失败均归一
ok=False 工具错误（恒不上抛——registry 异常隔离契约）。
"""

from __future__ import annotations

import base64
from typing import Any

from packages.harness.core.errors import BusinessException, ErrorCode
from packages.harness.multimodal.look_at import LOOK_AT_TOOL_NAME, register_look_at_tool
from packages.harness.multimodal.mm_models import LookAtResultModel
from packages.harness.tools.registry import ToolRegistry

#: 测试图片字节（内容无关——base64 往返契约面）。
_PNG_BYTES: bytes = b"\x89PNG\r\n\x1a\n fake-image-body"


class FakeLookAtService:
    """fake look_at 服务（describe 调用捕获；可注入故障）。"""

    def __init__(self, *, error: BusinessException | None = None) -> None:
        """初始化。

        Args:
            error: describe 故障注入（None = 正常返回固定结果）。
        """
        self.error = error
        self.calls: list[dict[str, Any]] = []

    async def describe(
        self,
        image: bytes,
        mime_type: str,
        question: str | None = None,
        *,
        persona_id: str | None = None,
        request_id: str | None = None,
    ) -> LookAtResultModel:
        """假理解（记录载荷与参数；返回固定结果 / 注入故障）。"""
        self.calls.append(
            {"image": image, "mime_type": mime_type, "question": question}
        )
        if self.error is not None:
            raise self.error
        return LookAtResultModel(
            description="图中是一只橘猫", model_entry="local-vision", latency_ms=25
        )


def _registry_with(service: FakeLookAtService) -> ToolRegistry:
    """注册了 look_at 工具的注册表（fake 服务注入）。"""
    registry = ToolRegistry()
    register_look_at_tool(registry, service)
    return registry


# ---------------------------------------------------------------------------
# 注册 / 列举面
# ---------------------------------------------------------------------------


async def test_registered_tool_appears_in_list_specs() -> None:
    """注册即列举：tool_names + list_specs 含 look_at（OpenAI 定义形态）。"""
    service = FakeLookAtService()
    registry = _registry_with(service)
    assert LOOK_AT_TOOL_NAME in registry.tool_names
    specs = registry.list_specs()
    look_at_spec = next(spec for spec in specs if spec.name == LOOK_AT_TOOL_NAME)
    assert look_at_spec.description
    assert look_at_spec.parameters.required == ["image_base64"]
    assert "mime_type" in look_at_spec.parameters.properties
    assert "question" in look_at_spec.parameters.properties


async def test_duplicate_registration_overrides() -> None:
    """同名重复注册：后写覆盖前写（装配面语义——与内置工具同约定）。"""
    registry = _registry_with(FakeLookAtService())
    register_look_at_tool(registry, FakeLookAtService())
    assert registry.tool_names.count(LOOK_AT_TOOL_NAME) == 1


# ---------------------------------------------------------------------------
# 执行往返（fake 服务）
# ---------------------------------------------------------------------------


async def test_execute_roundtrip_decodes_base64_and_calls_service() -> None:
    """执行往返：base64 解码 → describe(字节, mime, question) → 结果 dict。"""
    service = FakeLookAtService()
    registry = _registry_with(service)
    result = await registry.execute(
        LOOK_AT_TOOL_NAME,
        {
            "image_base64": base64.b64encode(_PNG_BYTES).decode("ascii"),
            "mime_type": "image/webp",
            "question": "图里有几只猫",
        },
    )
    assert result.ok is True
    assert result.result == {
        "description": "图中是一只橘猫",
        "model_entry": "local-vision",
        "latency_ms": 25,
    }
    assert service.calls == [
        {"image": _PNG_BYTES, "mime_type": "image/webp", "question": "图里有几只猫"}
    ]


async def test_execute_defaults_mime_png_and_question_none() -> None:
    """参数缺省物化：mime png / question None（registry args_model 默认值）。"""
    service = FakeLookAtService()
    registry = _registry_with(service)
    result = await registry.execute(
        LOOK_AT_TOOL_NAME, {"image_base64": base64.b64encode(_PNG_BYTES).decode("ascii")}
    )
    assert result.ok is True
    assert service.calls[0]["mime_type"] == "image/png"
    assert service.calls[0]["question"] is None


# ---------------------------------------------------------------------------
# 失败面（恒 ok=False，不上抛）
# ---------------------------------------------------------------------------


async def test_execute_invalid_base64_is_tool_error() -> None:
    """非法 base64 载荷：ok=False 工具错误（错误文案点名 base64），不上抛。"""
    service = FakeLookAtService()
    registry = _registry_with(service)
    result = await registry.execute(
        LOOK_AT_TOOL_NAME, {"image_base64": "!!!not-base64!!!"}
    )
    assert result.ok is False
    assert result.error is not None
    assert "base64" in result.error
    assert result.result == {}
    assert service.calls == []


async def test_execute_missing_image_arg_is_tool_error() -> None:
    """缺 image_base64 参数：args_model 校验失败 → ok=False。"""
    service = FakeLookAtService()
    registry = _registry_with(service)
    result = await registry.execute(LOOK_AT_TOOL_NAME, {"question": "看什么"})
    assert result.ok is False
    assert service.calls == []


async def test_execute_unknown_mime_arg_is_tool_error() -> None:
    """args 携带未知 MIME：args_model 校验失败 → ok=False（服务不被触达）。"""
    service = FakeLookAtService()
    registry = _registry_with(service)
    result = await registry.execute(
        LOOK_AT_TOOL_NAME,
        {
            "image_base64": base64.b64encode(_PNG_BYTES).decode("ascii"),
            "mime_type": "image/tiff",
        },
    )
    assert result.ok is False
    assert service.calls == []


async def test_execute_service_failure_is_tool_error() -> None:
    """服务侧业务异常（如回退链空）：ok=False + 错误码前缀，不上抛。"""
    service = FakeLookAtService(
        error=BusinessException(
            code=ErrorCode.CONFIG_INVALID, message="look_at 回退链未配置"
        )
    )
    registry = _registry_with(service)
    result = await registry.execute(
        LOOK_AT_TOOL_NAME, {"image_base64": base64.b64encode(_PNG_BYTES).decode("ascii")}
    )
    assert result.ok is False
    assert result.error is not None
    assert "CONFIG_INVALID" in result.error


async def test_execute_unknown_tool_name_is_tool_error() -> None:
    """未注册工具名：ok=False（registry 寻址面——既有契约回归锚点）。"""
    registry = _registry_with(FakeLookAtService())
    result = await registry.execute("look_again", {})
    assert result.ok is False
    assert "未注册工具" in (result.error or "")

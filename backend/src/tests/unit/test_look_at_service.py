"""harness/multimodal look_at 服务测试——回退链裁决 / 消息组装 / 失败面。

测试策略：fake 网关（ScriptedGateway 形态——逐次调用捕获 + 排队响应 /
故障注入）+ 真实 ModelResolver（fake 条目索引）+ 真实
multimodal_fallback_loader 运行时状态（record_multimodal_fallback 注入
——conftest autouse fixture 每测试后复位）。零真实网络 / 零真实配置。
"""

from __future__ import annotations

from typing import Any

import pytest

from packages.harness.config.model_entry import LlmModelEntry
from packages.harness.config.model_resolver import ModelResolver
from packages.harness.config.models import MultimodalFallbackConfig
from packages.harness.config.multimodal_fallback_loader import record_multimodal_fallback
from packages.harness.core.errors import BusinessException, ErrorCode
from packages.harness.llm.llm_models import ChatResponseModel
from packages.harness.multimodal.look_at import LookAtService
from packages.harness.multimodal.mm_models import (
    LOOK_AT_IMAGE_MAX_BYTES,
    build_data_url,
    parse_data_url,
)

#: 测试图片字节（内容无关——契约面是 data URL 透传）。
_PNG_BYTES: bytes = b"\x89PNG\r\n\x1a\n fake-image-body"


def _entry(name: str, *, multimodal: bool = False) -> LlmModelEntry:
    """构造测试模型条目。"""
    return LlmModelEntry(
        name=name,
        base_url="http://127.0.0.1:8001/v1",
        model=f"model-{name}",
        multimodal=multimodal,
    )


def _resolver_with_look_at(entry: LlmModelEntry) -> ModelResolver:
    """构造绑定了 look_at 角色的解析器（显式绑定）。"""
    return ModelResolver([entry], role_bindings={"look_at": entry.name})


class ScriptedGateway:
    """脚本化假网关（逐次 chat_for_entry 捕获 + 排队响应 / 故障注入）。

    Attributes:
        calls: 逐次调用捕获（entry / messages / purpose / persona_id /
            request_id）。
    """

    def __init__(self, responses: list[ChatResponseModel | BusinessException]) -> None:
        """初始化（响应 / 故障按序弹出；队列耗尽即测试装配错误）。"""
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def chat_for_entry(
        self,
        entry: LlmModelEntry,
        messages: Any,
        params: Any = None,
        *,
        persona_id: str | None = None,
        purpose: str = "chat",
        request_id: str | None = None,
        tools: Any = None,
        tool_choice: Any = None,
    ) -> ChatResponseModel:
        """条目直连调用（捕获参数；弹出下一个排队响应 / 故障）。"""
        self.calls.append(
            {
                "entry": entry,
                "messages": list(messages),
                "purpose": purpose,
                "persona_id": persona_id,
                "request_id": request_id,
            }
        )
        if not self._responses:
            raise AssertionError("假网关响应队列耗尽（调用次数超出装配）")
        next_item = self._responses.pop(0)
        if isinstance(next_item, BusinessException):
            raise next_item
        return next_item


def _text_response(content: str) -> ChatResponseModel:
    """纯文本回复响应。"""
    return ChatResponseModel(content=content, model="vision")


# ---------------------------------------------------------------------------
# happy path：多模态角色条目直连
# ---------------------------------------------------------------------------


class TestDescribeDirectMultimodal:
    async def test_multimodal_role_entry_used_directly(self) -> None:
        """角色条目多模态：零回退直连（单条目链）；结果归一。"""
        gateway = ScriptedGateway([_text_response("图中是一只橘猫")])
        role_entry = _entry("local-vision", multimodal=True)
        service = LookAtService(gateway=gateway, resolver=_resolver_with_look_at(role_entry))
        result = await service.describe(_PNG_BYTES, "image/png")
        assert result.description == "图中是一只橘猫"
        assert result.model_entry == "local-vision"
        assert result.latency_ms >= 0
        assert len(gateway.calls) == 1
        assert gateway.calls[0]["entry"] is role_entry

    async def test_messages_shape_system_charter_and_user_parts(self) -> None:
        """消息组装：system 任务书 + user [image(data URL), text] 分块。"""
        gateway = ScriptedGateway([_text_response("描述")])
        service = LookAtService(
            gateway=gateway,
            resolver=_resolver_with_look_at(_entry("v", multimodal=True)),
        )
        await service.describe(_PNG_BYTES, "image/png")
        messages = gateway.calls[0]["messages"]
        assert len(messages) == 2
        system_message, user_message = messages
        assert system_message.role.value == "system"
        assert isinstance(system_message.content, str)
        assert "【视觉理解任务书】" in system_message.content
        assert isinstance(user_message.content, list)
        image_part, text_part = user_message.content
        assert image_part.type == "image_url"
        mime_type, image = parse_data_url(image_part.image_url.url)
        assert mime_type == "image/png"
        assert image == _PNG_BYTES
        assert text_part.type == "text"
        assert text_part.text  # 有缺省指令文本

    async def test_purpose_and_context_passthrough(self) -> None:
        """purpose 恒 look_at；persona_id / request_id 透传网关（用量归属）。"""
        gateway = ScriptedGateway([_text_response("描述")])
        service = LookAtService(
            gateway=gateway,
            resolver=_resolver_with_look_at(_entry("v", multimodal=True)),
        )
        await service.describe(
            _PNG_BYTES,
            "image/png",
            persona_id="42",
            request_id="req-7",
        )
        call = gateway.calls[0]
        assert call["purpose"] == "look_at"
        assert call["persona_id"] == "42"
        assert call["request_id"] == "req-7"

    async def test_question_passthrough_into_text_part_and_charter(self) -> None:
        """聚焦问题：user text 分块即问题原文；system 任务书含聚焦区块。"""
        gateway = ScriptedGateway([_text_response("回答")])
        service = LookAtService(
            gateway=gateway,
            resolver=_resolver_with_look_at(_entry("v", multimodal=True)),
        )
        await service.describe(_PNG_BYTES, "image/png", "图里有几只猫")
        messages = gateway.calls[0]["messages"]
        system_message, user_message = messages
        _, text_part = user_message.content
        assert text_part.text == "图里有几只猫"
        assert "「图里有几只猫」" in system_message.content

    async def test_description_stripped_and_data_url_matches_helper(self) -> None:
        """描述文本去首尾空白；data URL 与构建助手逐字节一致。"""
        gateway = ScriptedGateway([_text_response("  \n描述文本 \n")])
        service = LookAtService(
            gateway=gateway,
            resolver=_resolver_with_look_at(_entry("v", multimodal=True)),
        )
        result = await service.describe(_PNG_BYTES, "image/png")
        assert result.description == "描述文本"
        image_part = gateway.calls[0]["messages"][1].content[0]
        assert image_part.image_url.url == build_data_url(_PNG_BYTES, "image/png")


# ---------------------------------------------------------------------------
# 回退链：非多模态角色条目
# ---------------------------------------------------------------------------


class TestFallbackChain:
    async def test_non_multimodal_role_falls_back_to_chain(self) -> None:
        """非多模态角色条目：回退链 local 命中（远程不出场）。"""
        record_multimodal_fallback(
            MultimodalFallbackConfig(
                local=_entry("local-vision", multimodal=True),
                remote=_entry("remote-vision", multimodal=True),
            )
        )
        gateway = ScriptedGateway([_text_response("本地视觉描述")])
        service = LookAtService(
            gateway=gateway,
            resolver=_resolver_with_look_at(_entry("main-chat", multimodal=False)),
        )
        result = await service.describe(_PNG_BYTES, "image/png")
        assert result.model_entry == "local-vision"
        assert result.description == "本地视觉描述"
        assert len(gateway.calls) == 1

    async def test_chain_local_failure_falls_to_remote(self) -> None:
        """链第一级失败：尝试第二级（远程兜底）；逐条目用量各成一行。"""
        record_multimodal_fallback(
            MultimodalFallbackConfig(
                local=_entry("local-vision", multimodal=True),
                remote=_entry("remote-vision", multimodal=True),
            )
        )
        local_failure = BusinessException(
            code=ErrorCode.LLM_REQUEST_FAILED, message="本地端点熔断"
        )
        gateway = ScriptedGateway([local_failure, _text_response("远程视觉描述")])
        service = LookAtService(
            gateway=gateway,
            resolver=_resolver_with_look_at(_entry("main-chat", multimodal=False)),
        )
        result = await service.describe(_PNG_BYTES, "image/png")
        assert result.model_entry == "remote-vision"
        assert result.description == "远程视觉描述"
        assert [call["entry"].name for call in gateway.calls] == [
            "local-vision",
            "remote-vision",
        ]

    async def test_empty_chain_fails_loud_config_invalid(self) -> None:
        """非多模态 + 回退链未配置：CONFIG_INVALID（config 域 fail loud）。"""
        gateway = ScriptedGateway([])
        service = LookAtService(
            gateway=gateway,
            resolver=_resolver_with_look_at(_entry("main-chat", multimodal=False)),
        )
        with pytest.raises(BusinessException) as exc_info:
            await service.describe(_PNG_BYTES, "image/png")
        assert exc_info.value.code == ErrorCode.CONFIG_INVALID
        assert "multimodal_fallback" in str(exc_info.value.details.get("fix", ""))
        assert gateway.calls == []

    async def test_chain_remote_only_config(self) -> None:
        """仅远程一级的链：非多模态角色直接走远程。"""
        record_multimodal_fallback(
            MultimodalFallbackConfig(remote=_entry("remote-vision", multimodal=True))
        )
        gateway = ScriptedGateway([_text_response("远程描述")])
        service = LookAtService(
            gateway=gateway,
            resolver=_resolver_with_look_at(_entry("main-chat", multimodal=False)),
        )
        result = await service.describe(_PNG_BYTES, "image/png")
        assert result.model_entry == "remote-vision"


# ---------------------------------------------------------------------------
# 失败面
# ---------------------------------------------------------------------------


class TestFailures:
    async def test_role_unbound_model_not_found(self) -> None:
        """look_at 角色未绑定：MODEL_NOT_FOUND（resolver 裁决）。"""
        gateway = ScriptedGateway([])
        service = LookAtService(gateway=gateway, resolver=ModelResolver())
        with pytest.raises(BusinessException) as exc_info:
            await service.describe(_PNG_BYTES, "image/png")
        assert exc_info.value.code == ErrorCode.MODEL_NOT_FOUND
        assert gateway.calls == []

    async def test_single_entry_failure_reraises_original_code(self) -> None:
        """单条目链（多模态直连）失败：透传原始错误码（不被 LLM_CALL_FAILED 包裹）。"""
        original = BusinessException(
            code=ErrorCode.LLM_API_KEY_MISSING, message="鉴权失败"
        )
        gateway = ScriptedGateway([original])
        service = LookAtService(
            gateway=gateway,
            resolver=_resolver_with_look_at(_entry("v", multimodal=True)),
        )
        with pytest.raises(BusinessException) as exc_info:
            await service.describe(_PNG_BYTES, "image/png")
        assert exc_info.value.code == ErrorCode.LLM_API_KEY_MISSING

    async def test_multi_entry_all_failed_summarizes(self) -> None:
        """多级链全部失败：LLM_CALL_FAILED 汇总（details 携带链与末次错误码）。"""
        record_multimodal_fallback(
            MultimodalFallbackConfig(
                local=_entry("local-vision", multimodal=True),
                remote=_entry("remote-vision", multimodal=True),
            )
        )
        failures = [
            BusinessException(code=ErrorCode.LLM_REQUEST_FAILED, message="本地熔断"),
            BusinessException(code=ErrorCode.LLM_CALL_FAILED, message="远程失败"),
        ]
        gateway = ScriptedGateway(failures)
        service = LookAtService(
            gateway=gateway,
            resolver=_resolver_with_look_at(_entry("main-chat", multimodal=False)),
        )
        with pytest.raises(BusinessException) as exc_info:
            await service.describe(_PNG_BYTES, "image/png")
        assert exc_info.value.code == ErrorCode.LLM_CALL_FAILED
        assert exc_info.value.details["chain"] == ["local-vision", "remote-vision"]
        assert exc_info.value.details["last_error_code"] == "LLM_CALL_FAILED"

    async def test_blank_content_treated_as_failure(self) -> None:
        """空 content 响应：视为该条目失败（单条目链 → LLM_CALL_FAILED）。"""
        gateway = ScriptedGateway([_text_response("   ")])
        service = LookAtService(
            gateway=gateway,
            resolver=_resolver_with_look_at(_entry("v", multimodal=True)),
        )
        with pytest.raises(BusinessException) as exc_info:
            await service.describe(_PNG_BYTES, "image/png")
        assert exc_info.value.code == ErrorCode.LLM_CALL_FAILED
        assert "描述文本" in exc_info.value.message

    async def test_image_too_large_validation_error(self) -> None:
        """图像超 10MB：VALIDATION_ERROR（调用方先行压缩）。"""
        gateway = ScriptedGateway([])
        service = LookAtService(
            gateway=gateway,
            resolver=_resolver_with_look_at(_entry("v", multimodal=True)),
        )
        with pytest.raises(BusinessException) as exc_info:
            await service.describe(b"x" * (LOOK_AT_IMAGE_MAX_BYTES + 1), "image/png")
        assert exc_info.value.code == ErrorCode.VALIDATION_ERROR
        assert gateway.calls == []

    async def test_mime_not_in_allowlist_validation_error(self) -> None:
        """MIME 不在允许清单：VALIDATION_ERROR（含合法值提示）。"""
        gateway = ScriptedGateway([])
        service = LookAtService(
            gateway=gateway,
            resolver=_resolver_with_look_at(_entry("v", multimodal=True)),
        )
        with pytest.raises(BusinessException) as exc_info:
            await service.describe(_PNG_BYTES, "image/bmp")
        assert exc_info.value.code == ErrorCode.VALIDATION_ERROR
        assert "image/png" in str(exc_info.value.details)

    async def test_question_over_limit_validation_error(self) -> None:
        """聚焦问题超 200 字符：VALIDATION_ERROR。"""
        gateway = ScriptedGateway([])
        service = LookAtService(
            gateway=gateway,
            resolver=_resolver_with_look_at(_entry("v", multimodal=True)),
        )
        with pytest.raises(BusinessException) as exc_info:
            await service.describe(_PNG_BYTES, "image/png", "问" * 201)
        assert exc_info.value.code == ErrorCode.VALIDATION_ERROR

    async def test_empty_image_validation_error(self) -> None:
        """空图像字节：VALIDATION_ERROR（到达链路解析之前）。"""
        gateway = ScriptedGateway([])
        service = LookAtService(
            gateway=gateway,
            resolver=_resolver_with_look_at(_entry("v", multimodal=True)),
        )
        with pytest.raises(BusinessException) as exc_info:
            await service.describe(b"", "image/png")
        assert exc_info.value.code == ErrorCode.VALIDATION_ERROR
        assert gateway.calls == []

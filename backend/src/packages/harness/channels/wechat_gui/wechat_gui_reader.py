"""GUI 微信截图解析——视觉模型调用与严格 JSON 归一（fail-closed）。

调用链（与 look_at 域共享视觉能力面）::

    runner / adapter（截图 base64）
        ↓ 图像解码 + data URL 构建（不落盘、不落日志）
        ↓ 任务书 / 指令（wechat_gui_prompts Unit 文件）
        ↓ 条目链（resolve_multimodal_chain——look_at 角色；可选 vlm_entry 收敛）
        ↓ gateway.chat_for_entry(purpose="look_at")（用量行由网关发射）
        ↓ 严格 JSON 解析（标题 / 消息 / 置信度）
    VlmReadResultModel

失败语义（锁定，fail-closed）：非 JSON / 缺字段 / 类型不符 / 置信度低于
阈值 / 整条回退链失败——一律受控异常（``LLM_CALL_FAILED``），由 runner 按
退避重试、由适配器拒绝发送（宁可不发，不串会话）。
"""

from __future__ import annotations

import base64
import json
from binascii import Error as Base64Error
from typing import Any

from loguru import logger
from pydantic import ValidationError

from packages.harness.channels.wechat_gui.wechat_gui_models import (
    WECHAT_GUI_MIN_CONFIDENCE,
    VlmReadResultModel,
)
from packages.harness.channels.wechat_gui.wechat_gui_prompts import (
    render_wechat_gui_reader_charter,
    render_wechat_gui_reader_instruction,
)
from packages.harness.config.model_entry import ModelRole
from packages.harness.config.model_resolver import ModelResolver, get_model_resolver
from packages.harness.config.multimodal_fallback_loader import resolve_multimodal_chain
from packages.harness.core.errors import BusinessException, ErrorCode
from packages.harness.llm.gateway import LlmGateway
from packages.harness.llm.llm_models import (
    ChatMessageModel,
    ChatMessageRole,
    ImageContentPart,
    ImageContentUrlModel,
    TextContentPart,
)
from packages.harness.multimodal.mm_models import LookAtGatewayFace, build_data_url

__all__ = [
    "WECHAT_GUI_READER_MAX_MESSAGES",
    "WechatGuiReader",
    "extract_json_object",
]

#: 单次解析消息上限（与 settings.max_messages_per_poll 的缺省同值）。
WECHAT_GUI_READER_MAX_MESSAGES: int = 20

#: 截图 MIME（沙盒 ``import png:-`` 恒输出 PNG）。
_SCREENSHOT_MIME: str = "image/png"


def extract_json_object(content: str) -> dict[str, Any]:
    """从模型响应中提取 JSON 对象（容忍 Markdown 代码围栏与前后说明）。

    Args:
        content: 模型响应文本。

    Returns:
        解析出的 JSON 对象。

    Raises:
        ValueError: 未找到 JSON 对象 / JSON 非法 / 根节点非对象。
    """
    text = content.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("响应中未找到 JSON 对象")
    try:
        payload = json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        raise ValueError(f"响应 JSON 非法：{exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("响应 JSON 根节点必须是对象")
    return payload


class WechatGuiReader:
    """截图 → 视觉解析保（条目链逐级尝试；严格 JSON；低置信度失败）。"""

    def __init__(
        self,
        *,
        gateway: LookAtGatewayFace | None = None,
        resolver: ModelResolver | None = None,
        confidence_threshold: float = WECHAT_GUI_MIN_CONFIDENCE,
        vlm_entry: str = "",
        max_messages: int = WECHAT_GUI_READER_MAX_MESSAGES,
    ) -> None:
        """初始化解析器。

        Args:
            gateway: LLM 网关（None = 裸 :class:`LlmGateway`——调用时读全局
                resolver / pools）。
            resolver: 模型解析器（None = config 全局单例，调用时读取）。
            confidence_threshold: 最低置信度（低于阈值按解析失败处理）。
            vlm_entry: 指定视觉条目名（空串 = 走 look_at 角色回退链）。
            max_messages: 单次解析消息上限（正数；超出按时间序末尾保留）。
        """
        if not 0.0 <= confidence_threshold <= 1.0:
            raise ValueError("confidence_threshold 必须在 0.0-1.0 之间")
        if max_messages < 1:
            raise ValueError("max_messages 必须 >= 1")
        self._gateway: LookAtGatewayFace = gateway if gateway is not None else LlmGateway()
        self._resolver = resolver
        self._confidence_threshold = confidence_threshold
        self._vlm_entry = vlm_entry.strip()
        self._max_messages = max_messages

    async def read(
        self, image_base64: str, *, request_id: str | None = None
    ) -> VlmReadResultModel:
        """解析一张微信聊天窗口截图。

        Args:
            image_base64: PNG base64 载荷。
            request_id: 请求追踪 ID（None = 网关读请求上下文）。

        Returns:
            解析结果（标题 / 消息 / 置信度 / 原始 JSON）。

        Raises:
            BusinessException: 截图载荷非法（``CHANNEL_API_ERROR``）；条目
                链缺失（``CONFIG_INVALID`` / ``MODEL_NOT_FOUND``）；解析
                失败或低置信度（``LLM_CALL_FAILED``——fail-closed）。
        """
        try:
            image = base64.b64decode(image_base64, validate=True)
        except (Base64Error, ValueError) as exc:
            raise BusinessException(
                code=ErrorCode.CHANNEL_API_ERROR,
                message=f"GUI 微信截图不是合法 base64 载荷: {exc}",
            ) from exc
        if not image:
            raise BusinessException(
                code=ErrorCode.CHANNEL_API_ERROR,
                message="GUI 微信截图为空载荷",
            )
        data_url = build_data_url(image, _SCREENSHOT_MIME)
        messages: list[ChatMessageModel] = [
            ChatMessageModel(
                role=ChatMessageRole.SYSTEM,
                content=render_wechat_gui_reader_charter(),
            ),
            ChatMessageModel(
                role=ChatMessageRole.USER,
                content=[
                    ImageContentPart(image_url=ImageContentUrlModel(url=data_url)),
                    TextContentPart(
                        text=render_wechat_gui_reader_instruction(
                            max_messages=self._max_messages
                        )
                    ),
                ],
            ),
        ]
        resolver = self._resolver if self._resolver is not None else get_model_resolver()
        chain = resolve_multimodal_chain(resolver.resolve(ModelRole.LOOK_AT))
        if self._vlm_entry:
            chain = [entry for entry in chain if entry.name == self._vlm_entry]
            if not chain:
                raise BusinessException(
                    code=ErrorCode.CONFIG_INVALID,
                    message=f"WECHAT_GUI_VLM_ENTRY 指定的条目不在视觉回退链中: {self._vlm_entry}",
                )
        logger.info(
            "GUI 微信截图解析开始: size_bytes={}, chain={}",
            len(image),
            [entry.name for entry in chain],
        )
        last_error: BusinessException | None = None
        for entry in chain:
            try:
                response = await self._gateway.chat_for_entry(
                    entry, messages, purpose="look_at", request_id=request_id
                )
            except BusinessException as exc:
                last_error = exc
                logger.warning(
                    "GUI 微信截图解析条目调用失败，尝试回退链下一级: entry={}, code={}",
                    entry.name,
                    exc.code,
                )
                continue
            try:
                payload = extract_json_object(response.content)
                result = self._validate(payload)
            except (ValueError, ValidationError) as exc:
                last_error = BusinessException(
                    code=ErrorCode.LLM_CALL_FAILED,
                    message=f"GUI 微信截图解析响应非法（{entry.name}）: {exc}",
                    details={"model_entry": entry.name},
                )
                logger.warning(
                    "GUI 微信截图解析响应非法: entry={}, error={}", entry.name, exc
                )
                continue
            if result.confidence < self._confidence_threshold:
                last_error = BusinessException(
                    code=ErrorCode.LLM_CALL_FAILED,
                    message=(
                        f"GUI 微信截图解析置信度过低（{result.confidence:.2f} < "
                        f"{self._confidence_threshold:.2f}）"
                    ),
                    details={"model_entry": entry.name},
                )
                logger.warning(
                    "GUI 微信截图解析置信度过低: entry={}, confidence={}",
                    entry.name,
                    result.confidence,
                )
                continue
            return result
        if len(chain) == 1 and last_error is not None:
            raise last_error
        raise BusinessException(
            code=ErrorCode.LLM_CALL_FAILED,
            message="GUI 微信截图解析回退链全部条目失败",
            details={
                "chain": [entry.name for entry in chain],
                "last_error_code": str(last_error.code) if last_error is not None else None,
            },
        )

    def _validate(self, payload: dict[str, Any]) -> VlmReadResultModel:
        """校验模型 JSON 并裁剪消息上限（结构非法抛 ``ValidationError``）。

        Args:
            payload: 已解析的 JSON 对象。

        Returns:
            校验后的解析结果（``raw_json`` 为原始对象）。
        """
        result = VlmReadResultModel.model_validate({**payload, "raw_json": payload})
        bounded = result.messages[-self._max_messages :]
        return result.model_copy(update={"messages": bounded})

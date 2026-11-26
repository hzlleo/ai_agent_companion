"""GUI 微信通道出站适配器——标题校验后输入并发送（fail-closed）。

出站链路::

    OutboundMessageModel（chat_id=wechat_gui:<备注名>）
        ↓ chat_id grammar 解析（备注名）
        ↓ client.open_chat（沙盒搜索并打开会话）
        ↓ reader 对标题区域截图做视觉解析
        ↓ 标题校验：解析标题须与目标备注名一致——不一致即拒绝发送
        ↓ client.type_text（剪贴板粘贴，覆盖中文）+ client.press_key(enter)
        ↓ ChannelDeliveryResultModel（message_ids 恒空——GUI 侧无平台回执 ID）

失败语义：沙盒工具失败 / 视觉解析失败 / 标题不一致均上抛
``CHANNEL_API_ERROR``（可重试面），由路由器逐消息兜底隔离；**标题不一致
绝不发送**——宁可漏发一条，也不把回复投进错误会话。

附件边界（文档化）：GUI 底座当前只支持文本出站；附件逐条标记不支持
（``ok=False``），不阻断文本投递。
"""

from __future__ import annotations

from typing import Final

from loguru import logger

from packages.harness.channels.channel_models import (
    AttachmentDeliveryResult,
    ChannelDeliveryResultModel,
    ChannelType,
    OutboundMessageModel,
)
from packages.harness.channels.wechat_gui.wechat_gui_client import WechatGuiSandboxClient
from packages.harness.channels.wechat_gui.wechat_gui_models import parse_wechat_gui_chat_id
from packages.harness.channels.wechat_gui.wechat_gui_reader import WechatGuiReader
from packages.harness.core.errors import BusinessException, ErrorCode

__all__ = [
    "WECHAT_GUI_ATTACHMENT_UNSUPPORTED_ERROR",
    "WechatGuiChannelAdapter",
]

#: 附件不支持的错误文案（底座只支持文本出站——逐附件结果承载）。
WECHAT_GUI_ATTACHMENT_UNSUPPORTED_ERROR: Final[str] = (
    "GUI 微信底座当前只支持文本出站（附件投递属后续交付面）"
)


class WechatGuiChannelAdapter:
    """GUI 微信出站适配器（实现通道适配器发送面协议）。"""

    def __init__(
        self,
        *,
        client: WechatGuiSandboxClient,
        reader: WechatGuiReader,
    ) -> None:
        """初始化适配器。

        Args:
            client: 沙盒客户端（注入——测试用 fake）。
            reader: 截图解析器（标题校验输入——测试用 fake）。
        """
        self._client = client
        self._reader = reader

    async def send_message(self, message: OutboundMessageModel) -> ChannelDeliveryResultModel:
        """发送出站文本（先打开并校验目标会话，再输入与回车发送）。

        Args:
            message: 通道中立出站消息。

        Returns:
            投递结果（``message_ids`` 恒空；附件逐条标记不支持）。

        Raises:
            BusinessException: chat_id grammar 非法（``VALIDATION_ERROR``）/
                沙盒或视觉失败 / 标题不一致（``CHANNEL_API_ERROR``）——
                路由器兜底隔离。
        """
        remark_name = parse_wechat_gui_chat_id(message.chat_id)
        opened = await self._client.open_chat(remark_name)
        read = await self._reader.read(opened.title_image_base64)
        actual_title = read.title.strip()
        if actual_title != remark_name:
            raise BusinessException(
                code=ErrorCode.CHANNEL_API_ERROR,
                message=(
                    f"GUI 微信出站标题校验失败（拒绝发送）：目标 {remark_name!r}，"
                    f"当前标题 {actual_title!r}"
                ),
            )
        if message.text.strip():
            await self._client.type_text(message.text)
            await self._client.press_key("enter")
        logger.debug(
            "GUI 微信通道出站文本已提交: remark_name={!r}, chars={}",
            remark_name,
            len(message.text),
        )
        attachment_results = [
            AttachmentDeliveryResult(
                index=index,
                ok=False,
                error=WECHAT_GUI_ATTACHMENT_UNSUPPORTED_ERROR,
            )
            for index, _attachment in enumerate(message.attachments)
        ]
        return ChannelDeliveryResultModel(
            channel=ChannelType.WECHAT_GUI,
            chat_id=message.chat_id,
            message_ids=[],
            attachment_results=attachment_results,
        )

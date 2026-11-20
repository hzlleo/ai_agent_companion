"""multimodal 表情通道桥——入站附件解码收录 / 出站素材挑选（装配薄服务）。

与 :class:`~packages.harness.multimodal.sticker_service.StickerService` 的
分工：Service 只消费已解码字节并承担数据库 / 文件 I/O；本桥承担通道附件
模型到服务入参的转换——仅**内联 base64 载荷**可收录（``tg-file://`` /
``url`` 引用需二次下载，本桥跳过并文档化），出站挑选把服务返回的二进制
包装为通道中立 sticker 附件。

装配方（``app/main.py``）只注入 ``StickerService`` 与数据库会话工厂，
路由器 ``sticker_ingest_hook`` / ``sticker_reply_hook`` 直接指向本桥方法
（人格上下文由路由器绑定——桥不重复绑定）。解码失败 / 无素材均为既有
「表情是装饰面」语义：跳过该附件或返回 None，由调用方（路由器）兜底。
"""

from __future__ import annotations

import base64
from collections.abc import Callable, Sequence
from contextlib import AbstractAsyncContextManager
from typing import Protocol

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from packages.harness.channels.channel_models import (
    ChannelAttachmentKind,
    ChannelMessageAttachment,
)
from packages.harness.multimodal.sticker_models import (
    InboundStickerPayloadModel,
    StickerBinaryModel,
    StickerRecordDTO,
)

__all__ = [
    "InboundStickerPayloadModel",
    "StickerBridge",
    "StickerBridgeServiceFace",
    "decode_inbound_stickers",
]

#: 数据库会话工厂契约（app 装配注入 ``session_scope``；测试注入 fake）。
StickerSessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]


class StickerBridgeServiceFace(Protocol):
    """表情服务消费面协议（真实 StickerService / 测试 fake 均满足）。"""

    async def ingest_bytes(
        self,
        session: AsyncSession,
        payload: bytes,
        content_type: str,
        *,
        source_note: str,
    ) -> StickerRecordDTO | None:
        """收录一张表情（哈希去重；非法 / 停用返回 None）。"""
        ...

    async def pick_channel_reply(self, session: AsyncSession) -> StickerBinaryModel | None:
        """取最新一条可读表情用于回发（无素材返回 None）。"""
        ...


def decode_inbound_stickers(
    attachments: Sequence[ChannelMessageAttachment],
) -> list[InboundStickerPayloadModel]:
    """解码入站表情附件（仅内联 base64；引用源与非法载荷跳过）。

    Args:
        attachments: 入站附件列表（调用方已按 sticker 类筛选）。

    Returns:
        解码成功的收录载荷（附件序保持）；base64 缺失 / 不可解码的附件
        不产出条目（告警后跳过——单附件失败不阻断其余附件）。
    """
    payloads: list[InboundStickerPayloadModel] = []
    for attachment in attachments:
        if not attachment.base64:
            continue
        try:
            payload = base64.b64decode("".join(attachment.base64.split()), validate=True)
        except ValueError:
            logger.opt(exception=True).warning("通道入站表情 base64 解码失败（跳过该附件）")
            continue
        source_note = (
            f"通道入站表情：{attachment.caption}"
            if attachment.caption
            else "通道入站表情（平台消息附件）"
        )
        payloads.append(
            InboundStickerPayloadModel(
                payload=payload,
                content_type=attachment.media_type or "image/png",
                source_note=source_note,
            )
        )
    return payloads


class StickerBridge:
    """通道表情桥（入站收录 / 出站挑选——薄服务，无自有状态）。

    Args:
        sticker_service: 表情服务消费面（真实 ``StickerService``）。
        session_factory: 数据库会话工厂（人格上下文由路由器绑定后调用）。
    """

    def __init__(
        self,
        *,
        sticker_service: StickerBridgeServiceFace,
        session_factory: StickerSessionFactory,
    ) -> None:
        """初始化桥（依赖注入——app 装配传真实服务与会话工厂）。"""
        self._sticker_service = sticker_service
        self._session_factory = session_factory

    async def ingest_inbound(self, attachments: Sequence[ChannelMessageAttachment]) -> None:
        """收录入站表情附件（仅已持有字节的 base64 载荷）。

        Args:
            attachments: 入站附件列表（调用方已按 sticker 类筛选）。
        """
        payloads = decode_inbound_stickers(attachments)
        if not payloads:
            return
        async with self._session_factory() as db_session:
            for payload in payloads:
                await self._sticker_service.ingest_bytes(
                    db_session,
                    payload.payload,
                    payload.content_type,
                    source_note=payload.source_note,
                )

    async def pick_reply(self) -> ChannelMessageAttachment | None:
        """取当前人格最新可读表情作为出站附件（无素材返回 None）。

        Returns:
            ``kind=sticker`` 的出站附件（base64 载荷）；库空 / 文件缺失
            时为 None（调用方按「本轮不发表情」处理）。
        """
        async with self._session_factory() as db_session:
            binary = await self._sticker_service.pick_channel_reply(db_session)
        if binary is None:
            return None
        return ChannelMessageAttachment(
            kind=ChannelAttachmentKind.STICKER,
            media_type=binary.record.content_type,
            base64=base64.b64encode(binary.content).decode("ascii"),
        )

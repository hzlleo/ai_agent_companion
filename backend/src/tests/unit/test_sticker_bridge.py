"""表情通道桥测试——附件解码 / 收录 / 出站挑选（fake 服务零数据库）。"""

from __future__ import annotations

import base64
from contextlib import asynccontextmanager
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from packages.harness.channels.channel_models import (
    ChannelAttachmentKind,
    ChannelMessageAttachment,
)
from packages.harness.multimodal.sticker_bridge import (
    StickerBridge,
    decode_inbound_stickers,
)
from packages.harness.multimodal.sticker_models import (
    StickerAnnotationStatus,
    StickerBinaryModel,
    StickerRecordDTO,
)

_STICKER_BYTES = b"sticker-bytes"


class FakeStickerService:
    """fake 表情服务（记录收录入参 / 可编程回发素材）。"""

    def __init__(self, *, binary: StickerBinaryModel | None = None) -> None:
        """初始化（回发素材可编程）。"""
        self.ingested: list[tuple[bytes, str, str]] = []
        self.binary = binary

    async def ingest_bytes(
        self,
        session: AsyncSession,
        payload: bytes,
        content_type: str,
        *,
        source_note: str,
    ) -> StickerRecordDTO | None:
        """记录收录调用。"""
        self.ingested.append((payload, content_type, source_note))
        return None

    async def pick_channel_reply(self, session: AsyncSession) -> StickerBinaryModel | None:
        """返回可编程回发素材。"""
        return self.binary


@asynccontextmanager
async def _session_factory() -> Any:
    """fake 数据库会话工厂。"""
    yield object()


def _attachment(
    *,
    base64_value: str | None = None,
    media_type: str | None = None,
    caption: str | None = None,
) -> ChannelMessageAttachment:
    """构造 sticker 入站附件（base64 源 / 引用源二选一）。"""
    if base64_value is None:
        return ChannelMessageAttachment(
            kind=ChannelAttachmentKind.STICKER,
            media_type=media_type,
            url="https://example.com/s.png",
            caption=caption,
        )
    return ChannelMessageAttachment(
        kind=ChannelAttachmentKind.STICKER,
        media_type=media_type,
        base64=base64_value,
        caption=caption,
    )


def _record() -> StickerRecordDTO:
    """最小表情记录（出站包装面）。"""
    return StickerRecordDTO(
        id=3,
        sha256="a" * 64,
        content_type="image/webp",
        size_bytes=len(_STICKER_BYTES),
        tags=[],
        description="",
        source_note="测试来源",
        annotation_status=StickerAnnotationStatus.PENDING,
        created_at=1,
        updated_at=1,
    )


def _bridge(service: FakeStickerService) -> StickerBridge:
    """装配桥（fake 服务 + fake 会话）。"""
    return StickerBridge(sticker_service=service, session_factory=_session_factory)


async def test_decode_inbound_stickers_keeps_inline_base64_and_caption() -> None:
    """解码成功：空白折叠 / 默认 MIME / caption 进来源备注。"""
    attachment = _attachment(
        base64_value="c3Rp\nY2tl\nci1ieXRlcw==",
        caption="群友发的",
    )
    payloads = decode_inbound_stickers([attachment])
    assert len(payloads) == 1
    assert payloads[0].payload == _STICKER_BYTES
    assert payloads[0].content_type == "image/png"
    assert payloads[0].source_note == "通道入站表情：群友发的"


async def test_decode_inbound_stickers_skips_invalid_and_reference_sources() -> None:
    """不可解码 / 无 base64（url 引用）的附件跳过，不抛。"""
    payloads = decode_inbound_stickers(
        [
            _attachment(base64_value="not base64!!"),
            _attachment(),
        ]
    )
    assert payloads == []


async def test_decode_inbound_stickers_empty_attachments() -> None:
    """空附件列表：空结果（无会话 / 无日志副作用）。"""
    assert decode_inbound_stickers([]) == []


async def test_ingest_inbound_records_only_decodable_stickers() -> None:
    """收录：合法载荷 1 次、非法载荷跳过（单附件失败不阻断其余）。"""
    service = FakeStickerService()
    bridge = _bridge(service)
    await bridge.ingest_inbound(
        [
            _attachment(base64_value="!!!"),
            _attachment(
                base64_value=base64.b64encode(_STICKER_BYTES).decode("ascii"),
                media_type="image/webp",
            ),
        ]
    )
    assert service.ingested == [(_STICKER_BYTES, "image/webp", "通道入站表情（平台消息附件）")]


async def test_ingest_inbound_no_payloads_opens_no_session() -> None:
    """全部为引用源：不打开数据库会话（adapter 空集合语义）。"""
    service = FakeStickerService()
    opened: list[int] = []

    @asynccontextmanager
    async def _tracked_factory() -> Any:
        opened.append(1)
        yield object()

    bridge = StickerBridge(sticker_service=service, session_factory=_tracked_factory)
    await bridge.ingest_inbound([_attachment()])
    assert opened == []
    assert service.ingested == []


async def test_pick_reply_wraps_service_binary_as_sticker_attachment() -> None:
    """出站挑选：服务素材包装为 kind=sticker + base64 出站附件。"""
    service = FakeStickerService(
        binary=StickerBinaryModel(record=_record(), content=_STICKER_BYTES)
    )
    attachment = await _bridge(service).pick_reply()
    assert attachment is not None
    assert attachment.kind is ChannelAttachmentKind.STICKER
    assert attachment.media_type == "image/webp"
    assert attachment.base64 == base64.b64encode(_STICKER_BYTES).decode("ascii")


async def test_pick_reply_returns_none_without_material() -> None:
    """无素材：None（调用方本轮不发表情）。"""
    assert await _bridge(FakeStickerService()).pick_reply() is None


@pytest.mark.parametrize("bad", ["=", "a", "ab"])
async def test_decode_inbound_stickers_rejects_short_invalid_base64(bad: str) -> None:
    """短 / 边界非法 base64：跳过而非抛（多形态容错）。"""
    assert decode_inbound_stickers([_attachment(base64_value=bad)]) == []

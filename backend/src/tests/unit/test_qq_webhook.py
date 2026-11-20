"""qq 通道 webhook 处理器测试——process_webhook_event（fake 路由器零真实网络）。

覆盖矩阵：

    - 私聊 / 群聊消息事件路由形状（chat_id grammar / sender / message_id /
      epoch 秒 ×1000 归一）；
    - 消息展平双形态（段数组 / CQ 码字符串）+ 非文本段占位符表 + 未知段
      兜底 + 多 text 段无缝拼接；
    - message_id 去重（同 ID 重投只路由一次）+ 有界窗口 FIFO 淘汰；
    - notice / request / meta_event 确认并忽略；非 private/group 消息类型
      忽略；
    - 隔离铁律：非法载荷 / 缺关键字段 / 路由器抛错 / 空文本——恒不抛
      （HTTP 层确认语义）；
    - 协议 v2 附件提取：image / record 段 → 附件（url 源 / base64://
      剥前缀 / file:// path 源）；无引用媒体段跳过；CQ 码字符串形态
      维持占位符降级（提取边界）；非媒体段不产附件；
    - 非文本内容归一：无用户文本但含 CQ 码 / 非 text 段（两种 wire 形态
      一致）→ ``text=""`` 投递；混合文本保留占位符；仅空 text 段丢弃。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from packages.harness.channels.channel_models import (
    ChannelAttachmentKind,
    ChannelDeliveryResultModel,
    ChannelType,
    InboundMessageModel,
)
from packages.harness.channels.qq.qq_models import Ob11MessageSegmentModel
from packages.harness.channels.qq.qq_webhook import (
    QqMessageDedupWindow,
    extract_message_attachments,
    flatten_message_text,
    process_webhook_event,
    reset_qq_webhook_state,
)
from packages.harness.core.errors import BusinessException, ErrorCode
from packages.harness.core.time_utils import now_ms


class FakeRouter:
    """fake ChannelRouter——记录入站消息 / 可编程抛错。"""

    def __init__(self, *, handle_error: Exception | None = None) -> None:
        """初始化 fake 路由器。

        Args:
            handle_error: handle_inbound 故障注入（None = 恒成功投递形态）。
        """
        self.handled: list[InboundMessageModel] = []
        self._handle_error = handle_error

    async def handle_inbound(self, message: InboundMessageModel) -> ChannelDeliveryResultModel:
        """fake 路由（记录 + 恒成功投递形态；可编程抛错）。"""
        await asyncio.sleep(0)
        self.handled.append(message)
        if self._handle_error is not None:
            raise self._handle_error
        return ChannelDeliveryResultModel(
            channel=ChannelType.QQ, chat_id=message.chat_id, message_ids=["1"]
        )


def _message_event(
    message_id: int = 1001,
    *,
    message_type: str = "private",
    user_id: int = 10086,
    group_id: int | None = None,
    message: Any = None,
    time: int = 1731000000,
    post_type: str = "message",
) -> dict[str, Any]:
    """构造 message 事件上报 wire dict（测试直供形态）。

    Args:
        message_id: 消息 ID。
        message_type: 消息类型（private / group）。
        user_id: 发送者 QQ 号。
        group_id: 群号（群消息形态）。
        message: 消息内容（段数组 / CQ 码字符串；None = 缺省纯文本段）。
        time: 事件时间（epoch 秒）。
        post_type: 事件大类（覆写面——非 message 大类测试用）。

    Returns:
        事件 wire dict。
    """
    payload: dict[str, Any] = {
        "post_type": post_type,
        "time": time,
        "message_type": message_type,
        "sub_type": "friend" if message_type == "private" else "normal",
        "message_id": message_id,
        "user_id": user_id,
        "message": message if message is not None else [{"type": "text", "data": {"text": "你好"}}],
    }
    if group_id is not None:
        payload["group_id"] = group_id
    return payload


@pytest.fixture(autouse=True)
def _reset_dedup_window() -> None:
    """每测试复位 webhook 处理器进程内去重窗口（隔离铁律）。"""
    reset_qq_webhook_state()


class TestRouting:
    async def test_private_message_routed_with_grammar(self) -> None:
        """私聊消息：chat_id=private:<uid> + sender + message_id + ts 归一。"""
        router = FakeRouter()
        await process_webhook_event(_message_event(message_id=1001, user_id=10086), router)
        assert len(router.handled) == 1
        message = router.handled[0]
        assert message.channel is ChannelType.QQ
        assert message.chat_id == "private:10086"
        assert message.message_id == "1001"
        assert message.sender_id == "10086"
        assert message.text == "你好"
        assert message.ts_ms == 1731000000 * 1000

    async def test_group_message_routed_with_grammar(self) -> None:
        """群聊消息：chat_id=group:<gid>（sender 仍为发言者 QQ 号）。"""
        router = FakeRouter()
        await process_webhook_event(
            _message_event(
                message_id=2002,
                message_type="group",
                user_id=111,
                group_id=67890,
                message=[{"type": "text", "data": {"text": "群里大家好"}}],
            ),
            router,
        )
        message = router.handled[0]
        assert message.chat_id == "group:67890"
        assert message.sender_id == "111"
        assert message.text == "群里大家好"

    async def test_missing_time_falls_back_to_now_ms(self) -> None:
        """事件 time 缺失（0）：ts_ms 回落处理时刻（epoch 毫秒正数形态）。"""
        router = FakeRouter()
        before_ms = now_ms()
        await process_webhook_event(_message_event(time=0), router)
        assert router.handled[0].ts_ms >= before_ms


class TestFlattening:
    async def test_segment_array_text_joined(self) -> None:
        """段数组：多 text 段无缝拼接（还原原文）。"""
        assert (
            flatten_message_text(
                [
                    Ob11MessageSegmentModel(type="text", data={"text": "你"}),
                    Ob11MessageSegmentModel(type="text", data={"text": "好"}),
                ]
            )
            == "你好"
        )

    async def test_segment_array_image_face_placeholders(self) -> None:
        """段数组：image / face 段占位符化（[图片] / [表情]）。"""
        assert (
            flatten_message_text(
                [
                    Ob11MessageSegmentModel(type="text", data={"text": "看这张"}),
                    Ob11MessageSegmentModel(type="image", data={"url": "http://x/1.jpg"}),
                    Ob11MessageSegmentModel(type="face", data={"id": "4"}),
                ]
            )
            == "看这张[图片][表情]"
        )

    async def test_common_placeholder_table(self) -> None:
        """常见非文本段占位符表（record / video / at / reply / share / dice）。"""
        segments = [
            Ob11MessageSegmentModel(type="record", data={}),
            Ob11MessageSegmentModel(type="video", data={}),
            Ob11MessageSegmentModel(type="at", data={"qq": "123"}),
            Ob11MessageSegmentModel(type="reply", data={"id": "9"}),
            Ob11MessageSegmentModel(type="share", data={}),
            Ob11MessageSegmentModel(type="dice", data={}),
        ]
        assert flatten_message_text(segments) == "[语音][视频][提及][引用回复][分享][骰子]"

    async def test_unknown_segment_placeholder(self) -> None:
        """未知段类型：兜底占位符（实现扩展段类型降级形态）。"""
        assert (
            flatten_message_text([Ob11MessageSegmentModel(type="mega_app", data={})])
            == "[其它消息]"
        )

    async def test_cq_string_flattening(self) -> None:
        """CQ 码字符串：码外字面文本保留 + 码占位符化。"""
        assert flatten_message_text("你好[CQ:image,file=x.jpg]世界") == "你好[图片]世界"

    async def test_cq_string_multiple_codes(self) -> None:
        """CQ 码字符串：多码混合（face / record / 未知码）。"""
        assert flatten_message_text("[CQ:face,id=1]哈[CQ:record,file=a.mp3]") == "[表情]哈[语音]"

    async def test_flattened_mixed_message_routed(self) -> None:
        """混合段消息整链路由：展平文本进 InboundMessageModel。"""
        router = FakeRouter()
        await process_webhook_event(
            _message_event(
                message=[
                    {"type": "text", "data": {"text": "看看"}},
                    {"type": "image", "data": {"file": "a.jpg"}},
                ]
            ),
            router,
        )
        assert router.handled[0].text == "看看[图片]"

    async def test_cq_string_event_routed(self) -> None:
        """CQ 码字符串形态事件整链路由（双形态入站兼容）。"""
        router = FakeRouter()
        await process_webhook_event(_message_event(message="在吗[CQ:face,id=2]"), router)
        assert router.handled[0].text == "在吗[表情]"


class TestDedup:
    async def test_same_message_id_routed_once(self) -> None:
        """同 message_id 重投：去重跳过（只路由一次）。"""
        router = FakeRouter()
        event = _message_event(message_id=3003)
        await process_webhook_event(event, router)
        await process_webhook_event(event, router)
        assert len(router.handled) == 1

    async def test_distinct_message_ids_both_routed(self) -> None:
        """不同 message_id：互不影响（正常多消息形态）。"""
        router = FakeRouter()
        await process_webhook_event(_message_event(message_id=1), router)
        await process_webhook_event(_message_event(message_id=2), router)
        assert len(router.handled) == 2

    async def test_window_fifo_eviction_allows_reprocess(self) -> None:
        """有界窗口 FIFO 淘汰：超容后旧 ID 可再次通过（tg_runner 同语义）。"""
        window = QqMessageDedupWindow(cap=2)
        assert window.remember(1) is True
        assert window.remember(2) is True
        assert window.remember(1) is False
        assert window.remember(3) is True  # 淘汰 1（FIFO——窗口 [2, 3]）
        assert window.remember(1) is True  # 1 已出窗——重新首见（淘汰 2 → 窗口 [3, 1]）
        assert window.remember(3) is False  # 3 仍在窗内
        assert window.remember(2) is True  # 2 已被再次淘汰——重新首见

    async def test_negative_message_id_dedup(self) -> None:
        """负值 message_id（NapCat 常见形态）：去重按值承载。"""
        router = FakeRouter()
        event = _message_event(message_id=-400500600)
        await process_webhook_event(event, router)
        await process_webhook_event(event, router)
        assert len(router.handled) == 1
        assert router.handled[0].message_id == "-400500600"

    async def test_cap_below_one_rejected(self) -> None:
        """cap < 1：入口 ValueError（零容量去重静默失效面——拒绝构造）。"""
        for invalid in (0, -1):
            with pytest.raises(ValueError, match="cap 须 ≥ 1"):
                QqMessageDedupWindow(cap=invalid)


class TestIgnoreAndIsolation:
    async def test_notice_event_acknowledged_and_ignored(self) -> None:
        """notice 事件：确认并忽略（不进路由——HTTP 200 防重投语义）。"""
        router = FakeRouter()
        await process_webhook_event(_message_event(post_type="notice", message_id=5005), router)
        assert router.handled == []

    async def test_request_and_meta_event_ignored(self) -> None:
        """request / meta_event 事件：确认并忽略。"""
        router = FakeRouter()
        await process_webhook_event(_message_event(post_type="request", message_id=6006), router)
        await process_webhook_event(_message_event(post_type="meta_event", message_id=7007), router)
        assert router.handled == []

    async def test_unsupported_message_type_ignored(self) -> None:
        """非 private/group 消息类型（如 guild 扩展）：确认并忽略。"""
        router = FakeRouter()
        await process_webhook_event(_message_event(message_type="guild", message_id=8008), router)
        assert router.handled == []

    async def test_empty_payload_object_ignored(self) -> None:
        """空对象载荷（无已知字段全缺省）：post_type 缺失 → 忽略不抛。"""
        router = FakeRouter()
        await process_webhook_event({}, router)
        assert router.handled == []

    async def test_malformed_payload_isolated_ack(self) -> None:
        """字段类型非法载荷：隔离确认（不抛 / 不路由）。"""
        router = FakeRouter()
        await process_webhook_event({"post_type": "message", "message_id": "not-an-int"}, router)
        assert router.handled == []

    async def test_empty_flattened_text_skips_routing(self) -> None:
        """展平后空文本（无段 / 空串）：跳过路由不抛。"""
        router = FakeRouter()
        await process_webhook_event(_message_event(message=[]), router)
        await process_webhook_event(_message_event(message=""), router)
        assert router.handled == []

    async def test_group_message_missing_group_id_isolated(self) -> None:
        """群消息缺 group_id：告警隔离（不路由不抛）。"""
        router = FakeRouter()
        await process_webhook_event(_message_event(message_type="group", group_id=None), router)
        assert router.handled == []

    async def test_private_message_missing_user_id_isolated(self) -> None:
        """私聊消息缺 user_id：告警隔离（不路由不抛）。"""
        router = FakeRouter()
        await process_webhook_event(_message_event(user_id=0), router)
        assert router.handled == []

    async def test_group_message_anonymous_sender_normalized_zero(self) -> None:
        """群消息匿名发言（user_id 缺失）：sender 归一 "0"（tg 同语义）。"""
        router = FakeRouter()
        await process_webhook_event(
            _message_event(message_type="group", user_id=0, group_id=67890), router
        )
        assert router.handled[0].sender_id == "0"
        assert router.handled[0].chat_id == "group:67890"

    async def test_router_exception_isolated(self) -> None:
        """路由器内部异常：处理器兜底捕获（恒不抛——HTTP 确认语义）。"""
        router = FakeRouter(
            handle_error=BusinessException(
                code=ErrorCode.CHANNEL_API_ERROR, message="路由失败（测试注入）"
            )
        )
        await process_webhook_event(_message_event(), router)
        assert len(router.handled) == 1  # 已记录后抛——确认语义不受影响


class TestAttachmentExtractionV2:
    async def test_image_segment_with_url_routed_as_attachment(self) -> None:
        """image 段（data.url）：url 源附件 + 占位符文本并存整链路由。"""
        router = FakeRouter()
        await process_webhook_event(
            _message_event(
                message=[
                    {"type": "text", "data": {"text": "看看"}},
                    {"type": "image", "data": {"url": "https://gchat.qpic.cn/1.jpg"}},
                ]
            ),
            router,
        )
        message = router.handled[0]
        assert message.text == "看看[图片]"
        assert len(message.attachments) == 1
        attachment = message.attachments[0]
        assert attachment.kind is ChannelAttachmentKind.IMAGE
        assert attachment.url == "https://gchat.qpic.cn/1.jpg"

    async def test_record_segment_base64_file_strips_scheme(self) -> None:
        """record 段（data.file=base64://）：audio 类 + base64 源（剥前缀）。

        纯媒体无用户文本：占位符不进入站文本（``text=""``——路由器跳过
        生成，附件照常搭载）。
        """
        router = FakeRouter()
        await process_webhook_event(
            _message_event(
                message=[
                    {"type": "record", "data": {"file": "base64://aGVsbG8="}},
                ]
            ),
            router,
        )
        message = router.handled[0]
        assert message.text == ""
        attachment = message.attachments[0]
        assert attachment.kind is ChannelAttachmentKind.AUDIO
        assert attachment.base64 == "aGVsbG8="

    async def test_pure_image_routed_with_empty_text(self) -> None:
        """纯图片无文本：仍归一入站（text=""）并搭载附件（不再前置丢弃）。"""
        router = FakeRouter()
        await process_webhook_event(
            _message_event(
                message=[
                    {"type": "image", "data": {"url": "https://gchat.qpic.cn/pure.png"}},
                ]
            ),
            router,
        )
        assert len(router.handled) == 1
        message = router.handled[0]
        assert message.text == ""
        assert len(message.attachments) == 1
        assert message.attachments[0].kind is ChannelAttachmentKind.IMAGE

    async def test_pure_sticker_face_with_image_routed_with_empty_text(self) -> None:
        """纯表情（face + 图片段）无文本：归一入站（text=""）供表情桥收录。"""
        router = FakeRouter()
        await process_webhook_event(
            _message_event(
                message=[
                    {"type": "face", "data": {"id": "4"}},
                    {
                        "type": "image",
                        "data": {"file": "base64://c3RpY2tlcg=="},
                    },
                ]
            ),
            router,
        )
        message = router.handled[0]
        assert message.text == ""
        assert len(message.attachments) == 1
        assert message.attachments[0].base64 == "c3RpY2tlcg=="

    async def test_image_segment_file_scheme_maps_path_source(self) -> None:
        """image 段（data.file=file://…，无 url）：path 源原样承载。"""
        attachments = extract_message_attachments(
            [Ob11MessageSegmentModel(type="image", data={"file": "file:///napcat/cache/x.jpg"})]
        )
        assert len(attachments) == 1
        assert attachments[0].kind is ChannelAttachmentKind.IMAGE
        assert attachments[0].path == "file:///napcat/cache/x.jpg"

    async def test_image_segment_base64_payload_strips_prefix(self) -> None:
        """image 段（data.file=base64://…）：base64 源（载荷剥方案前缀）。"""
        attachments = extract_message_attachments(
            [Ob11MessageSegmentModel(type="image", data={"file": "base64://aGVsbG8="})]
        )
        assert len(attachments) == 1
        assert attachments[0].kind is ChannelAttachmentKind.IMAGE
        assert attachments[0].base64 == "aGVsbG8="

    async def test_media_segment_without_reference_skipped(self) -> None:
        """媒体段无可用引用（url / file 皆缺）：跳过不产附件。"""
        attachments = extract_message_attachments([Ob11MessageSegmentModel(type="image", data={})])
        assert attachments == []

    async def test_non_media_segments_produce_no_attachments(self) -> None:
        """非媒体段（face / at / video）：不产附件（占位符降级仅文本面）。"""
        attachments = extract_message_attachments(
            [
                Ob11MessageSegmentModel(type="face", data={"id": "4"}),
                Ob11MessageSegmentModel(type="at", data={"qq": "123"}),
                Ob11MessageSegmentModel(type="video", data={}),
            ]
        )
        assert attachments == []

    async def test_cq_string_form_extracts_no_attachments(self) -> None:
        """CQ 码字符串形态：附件提取不生效（文档化边界——维持占位符降级）。"""
        router = FakeRouter()
        await process_webhook_event(
            _message_event(message="看看[CQ:image,file=x.jpg,url=https://x/1.jpg]"),
            router,
        )
        message = router.handled[0]
        assert message.text == "看看[图片]"
        assert message.attachments == []
        assert extract_message_attachments("看看[CQ:image,file=x.jpg]") == []

    async def test_multiple_media_segments_keep_order(self) -> None:
        """多媒体段：段序保持（image 先 record 后 → 附件序一致）。"""
        attachments = extract_message_attachments(
            [
                Ob11MessageSegmentModel(type="text", data={"text": "两件"}),
                Ob11MessageSegmentModel(type="image", data={"url": "https://x/1.jpg"}),
                Ob11MessageSegmentModel(type="record", data={"url": "https://x/1.silk"}),
            ]
        )
        assert [attachment.kind for attachment in attachments] == [
            ChannelAttachmentKind.IMAGE,
            ChannelAttachmentKind.AUDIO,
        ]


class TestNonTextNormalization:
    async def test_cq_string_pure_image_routed_with_empty_text(self) -> None:
        """CQ 字符串纯图片（无用户文本）：text="" 投递（占位符不冒充用户话语）。"""
        router = FakeRouter()
        await process_webhook_event(_message_event(message="[CQ:image,file=x.jpg]"), router)
        assert len(router.handled) == 1
        assert router.handled[0].text == ""
        assert router.handled[0].attachments == []

    async def test_pure_face_segment_routed_with_empty_text(self) -> None:
        """纯 face 段（无用户文本、无可提取附件）：text="" 投递（no-op 收录）。"""
        router = FakeRouter()
        await process_webhook_event(
            _message_event(message=[{"type": "face", "data": {"id": "4"}}]), router
        )
        assert len(router.handled) == 1
        assert router.handled[0].text == ""
        assert router.handled[0].attachments == []

    async def test_mixed_text_and_face_keeps_placeholder(self) -> None:
        """混合文本 + face：用户文本保留并携带占位符进入生成面。"""
        router = FakeRouter()
        await process_webhook_event(
            _message_event(
                message=[
                    {"type": "text", "data": {"text": "你好"}},
                    {"type": "face", "data": {"id": "4"}},
                ]
            ),
            router,
        )
        assert router.handled[0].text == "你好[表情]"

    async def test_empty_text_segment_without_non_text_skipped(self) -> None:
        """仅空 text 段（无非文本内容）：丢弃（无可投递内容）。"""
        router = FakeRouter()
        await process_webhook_event(
            _message_event(message=[{"type": "text", "data": {"text": ""}}]), router
        )
        assert router.handled == []

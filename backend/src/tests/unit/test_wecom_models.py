"""wecom 长连接 wire 模型测试——入站映射 / 出站命令构造 / 分段矩阵（零网络）。

覆盖矩阵：

    - 会话键 grammar（single / group 解析 + 非法形态拒绝）；
    - 入站映射：文本 / 语音转写 / 图文混排 / 图片 / 文件 / 视频 / 群聊标记 /
      合成来源标记 / 时间注入 / 排重键缺失与未知类型不可路由；
    - 附件提取：url 源、无 url 跳过、视频归 file 类且 MIME 线索；
    - 文本归一：占位符兜底、混排拼接、空内容不可路由；
    - 出站命令构造：订阅 / 心跳 / markdown 回复与推送 / 附件推送 / 素材上传
      三步（含 wire 别名 ``type``）/ 无消息体的素材类型拒绝；
    - markdown 字节上限分段（换行边界优先 / 中文 UTF-8 硬切不破字符）。
"""

from __future__ import annotations

import base64
import json

import pytest

from packages.harness.channels.channel_models import ChannelAttachmentKind, ChannelType
from packages.harness.channels.wecom.wecom_models import (
    WECOM_CHAT_TYPE_GROUP,
    WECOM_CHAT_TYPE_SINGLE,
    WECOM_CMD_PING,
    WECOM_CMD_RESPOND_MSG,
    WECOM_CMD_SEND_MSG,
    WECOM_CMD_UPLOAD_MEDIA_CHUNK,
    WECOM_CMD_UPLOAD_MEDIA_FINISH,
    WECOM_CMD_UPLOAD_MEDIA_INIT,
    WECOM_MARKDOWN_MAX_BYTES,
    WECOM_PLACEHOLDER_FILE,
    WECOM_PLACEHOLDER_IMAGE,
    WECOM_PLACEHOLDER_VIDEO,
    WeComAckModel,
    WeComCallbackFrameModel,
    WeComChatTargetModel,
    WeComMediaKind,
    build_markdown_push_command,
    build_markdown_reply_command,
    build_media_push_command,
    build_ping_command,
    build_subscribe_command,
    build_upload_chunk_command,
    build_upload_finish_command,
    build_upload_init_command,
    extract_wecom_attachments,
    flatten_wecom_callback_text,
    map_wecom_callback_to_inbound,
    parse_wecom_chat_id,
    split_wecom_markdown_text,
    wecom_media_kind_for_attachment,
    wecom_upload_size_limit,
)
from packages.harness.core.errors import BusinessException, ErrorCode


def _frame(body: dict, *, cmd: str = "aibot_msg_callback", req_id: str = "req-cb") -> WeComCallbackFrameModel:
    """构造回调帧模型（测试直供形态）。

    Args:
        body: 回调 body（wire 字段名）。
        cmd: 回调命令。
        req_id: 回调请求标识。

    Returns:
        回调帧模型。
    """
    return WeComCallbackFrameModel.model_validate(
        {"cmd": cmd, "headers": {"req_id": req_id}, "body": body}
    )


class TestChatIdGrammar:
    async def test_single_chat_parses(self) -> None:
        """single:<userid> 解析为单聊目标（chat_type=1）。"""
        target = parse_wecom_chat_id("single:zhangsan")
        assert target.chat_type == WECOM_CHAT_TYPE_SINGLE
        assert target.target_id == "zhangsan"
        assert target.is_group is False

    async def test_group_chat_parses(self) -> None:
        """group:<chatid> 解析为群聊目标（chat_type=2）。"""
        target = parse_wecom_chat_id("group:chat-8899")
        assert target.chat_type == WECOM_CHAT_TYPE_GROUP
        assert target.target_id == "chat-8899"
        assert target.is_group is True

    @pytest.mark.parametrize(
        "chat_id",
        ["zhangsan", "single:", ":user", "private:user", ""],
    )
    async def test_invalid_grammar_rejected(self, chat_id: str) -> None:
        """非法 grammar（无分隔 / 空目标 / 未知前缀）拒绝为 VALIDATION_ERROR。"""
        with pytest.raises(BusinessException) as exc_info:
            parse_wecom_chat_id(chat_id)
        assert exc_info.value.code == ErrorCode.VALIDATION_ERROR


class TestInboundMapping:
    async def test_text_single_chat_maps(self) -> None:
        """单聊文本 → single: 会话键 + 非群聊 + 非合成来源。"""
        inbound = map_wecom_callback_to_inbound(
            _frame(
                {
                    "msgid": "msg-1",
                    "aibotid": "bot-1",
                    "chattype": "single",
                    "from": {"userid": "zhangsan"},
                    "msgtype": "text",
                    "text": {"content": "你好"},
                }
            ),
            ts_ms=1731000000000,
        )
        assert inbound is not None
        assert inbound.channel is ChannelType.WECOM
        assert inbound.chat_id == "single:zhangsan"
        assert inbound.message_id == "msg-1"
        assert inbound.sender_id == "zhangsan"
        assert inbound.text == "你好"
        assert inbound.ts_ms == 1731000000000
        assert inbound.attachments == []
        assert inbound.is_group_chat is False
        assert inbound.synthetic is False
        assert inbound.silent is False

    async def test_text_group_chat_maps(self) -> None:
        """群聊文本 → group: 会话键 + 群聊标记（来源分级依据）。"""
        inbound = map_wecom_callback_to_inbound(
            _frame(
                {
                    "msgid": "msg-2",
                    "chatid": "chat-1",
                    "chattype": "group",
                    "from": {"userid": "lisi"},
                    "msgtype": "text",
                    "text": {"content": "@机器人 在吗"},
                }
            ),
            ts_ms=1,
        )
        assert inbound is not None
        assert inbound.chat_id == "group:chat-1"
        assert inbound.is_group_chat is True
        assert inbound.sender_id == "lisi"

    async def test_voice_maps_transcript(self) -> None:
        """语音走平台转写文本（无附件——音频字节不在回调面）。"""
        inbound = map_wecom_callback_to_inbound(
            _frame(
                {
                    "msgid": "msg-voice",
                    "chattype": "single",
                    "from": {"userid": "u1"},
                    "msgtype": "voice",
                    "voice": {"content": "今天天气怎么样"},
                }
            ),
            ts_ms=1,
        )
        assert inbound is not None
        assert inbound.text == "今天天气怎么样"
        assert inbound.attachments == []

    async def test_mixed_maps_text_and_image_attachment(self) -> None:
        """图文混排：文本项原文 + 图片项占位符与 url 附件。"""
        inbound = map_wecom_callback_to_inbound(
            _frame(
                {
                    "msgid": "msg-mixed",
                    "chatid": "chat-2",
                    "chattype": "group",
                    "from": {"userid": "u2"},
                    "msgtype": "mixed",
                    "mixed": {
                        "msg_item": [
                            {"msgtype": "text", "text": {"content": "看这张"}},
                            {"msgtype": "image", "image": {"url": "https://cdn.example.com/a.jpg"}},
                        ]
                    },
                }
            ),
            ts_ms=1,
        )
        assert inbound is not None
        assert inbound.text == f"看这张{WECOM_PLACEHOLDER_IMAGE}"
        assert len(inbound.attachments) == 1
        assert inbound.attachments[0].kind is ChannelAttachmentKind.IMAGE
        assert inbound.attachments[0].url == "https://cdn.example.com/a.jpg"

    async def test_image_maps_placeholder_and_attachment(self) -> None:
        """纯图片：占位符文本 + image 附件（url 源）。"""
        inbound = map_wecom_callback_to_inbound(
            _frame(
                {
                    "msgid": "msg-image",
                    "chattype": "single",
                    "from": {"userid": "u3"},
                    "msgtype": "image",
                    "image": {"url": "https://cdn.example.com/b.png", "aeskey": "abc"},
                }
            ),
            ts_ms=1,
        )
        assert inbound is not None
        assert inbound.text == WECOM_PLACEHOLDER_IMAGE
        assert inbound.attachments[0].kind is ChannelAttachmentKind.IMAGE
        assert inbound.attachments[0].media_type is None

    async def test_file_and_video_map_as_file_attachments(self) -> None:
        """文件 / 视频均归 file 类附件（视频附加 video/mp4 线索）。"""
        file_frame = _frame(
            {
                "msgid": "msg-file",
                "chattype": "single",
                "from": {"userid": "u4"},
                "msgtype": "file",
                "file": {"url": "https://cdn.example.com/c.pdf"},
            }
        )
        video_frame = _frame(
            {
                "msgid": "msg-video",
                "chattype": "single",
                "from": {"userid": "u4"},
                "msgtype": "video",
                "video": {"url": "https://cdn.example.com/d.mp4"},
            }
        )
        file_inbound = map_wecom_callback_to_inbound(file_frame, ts_ms=1)
        video_inbound = map_wecom_callback_to_inbound(video_frame, ts_ms=1)
        assert file_inbound is not None and video_inbound is not None
        assert file_inbound.text == WECOM_PLACEHOLDER_FILE
        assert file_inbound.attachments[0].kind is ChannelAttachmentKind.FILE
        assert file_inbound.attachments[0].media_type is None
        assert video_inbound.text == WECOM_PLACEHOLDER_VIDEO
        assert video_inbound.attachments[0].kind is ChannelAttachmentKind.FILE
        assert video_inbound.attachments[0].media_type == "video/mp4"

    async def test_compat_info_marks_synthetic(self) -> None:
        """compat_info 在场 = 非普通用户消息 → synthetic=True。"""
        inbound = map_wecom_callback_to_inbound(
            _frame(
                {
                    "msgid": "msg-compat",
                    "chatid": "chat-x",
                    "chattype": "group",
                    "from": {"userid": "u5"},
                    "msgtype": "text",
                    "text": {"content": "助理转述"},
                    "compat_info": {"source": "ai_assistant"},
                }
            ),
            ts_ms=1,
        )
        assert inbound is not None
        assert inbound.synthetic is True

    async def test_event_frame_not_routed(self) -> None:
        """事件回调不产出人类回合（返回 None）。"""
        frame = _frame(
            {
                "msgid": "ev-1",
                "msgtype": "event",
                "event": {"eventtype": "enter_chat"},
            },
            cmd="aibot_event_callback",
        )
        assert map_wecom_callback_to_inbound(frame, ts_ms=1) is None

    @pytest.mark.parametrize(
        "body",
        [
            {"msgid": "", "chattype": "single", "from": {"userid": "u"}, "msgtype": "text", "text": {"content": "hi"}},
            {"msgid": "m1", "chattype": "room", "from": {"userid": "u"}, "msgtype": "text", "text": {"content": "hi"}},
            {"msgid": "m2", "chattype": "group", "from": {"userid": "u"}, "msgtype": "text", "text": {"content": "hi"}},
            {"msgid": "m3", "chattype": "single", "from": {}, "msgtype": "text", "text": {"content": "hi"}},
            {"msgid": "m4", "chattype": "single", "from": {"userid": "u"}, "msgtype": "unknown"},
            {"msgid": "m5", "chattype": "single", "from": {"userid": "u"}, "msgtype": "text", "text": {"content": ""}},
        ],
    )
    async def test_unroutable_shapes_return_none(self, body: dict) -> None:
        """缺排重键 / 未知会话类型 / 群聊缺 chatid / 无发送者 / 未知类型 / 空文本均不可路由。"""
        assert map_wecom_callback_to_inbound(_frame(body), ts_ms=1) is None

    async def test_media_without_url_skipped_but_placeholder_kept(self) -> None:
        """媒体无 url 不产出附件，但占位符文本仍可路由。"""
        frame = _frame(
            {
                "msgid": "m6",
                "chattype": "single",
                "from": {"userid": "u"},
                "msgtype": "image",
                "image": {},
            }
        )
        inbound = map_wecom_callback_to_inbound(frame, ts_ms=1)
        assert inbound is not None
        assert inbound.text == WECOM_PLACEHOLDER_IMAGE
        assert inbound.attachments == []


class TestFlattenAndExtract:
    async def test_flatten_text_and_mixed(self) -> None:
        """文本 / 混排归一（混排逐项拼接且无分隔符）。"""
        text_body = _frame(
            {"msgid": "m", "chattype": "single", "from": {"userid": "u"}, "msgtype": "text", "text": {"content": "abc"}}
        ).body
        mixed_body = _frame(
            {
                "msgid": "m",
                "chattype": "single",
                "from": {"userid": "u"},
                "msgtype": "mixed",
                "mixed": {"msg_item": [{"msgtype": "text", "text": {"content": "a"}}]},
            }
        ).body
        assert flatten_wecom_callback_text(text_body) == "abc"
        assert flatten_wecom_callback_text(mixed_body) == "a"

    async def test_extract_attachments_order_and_skip(self) -> None:
        """附件提取顺序 image → mixed 图片 → file → video，无 url 项跳过。"""
        body = _frame(
            {
                "msgid": "m",
                "chattype": "single",
                "from": {"userid": "u"},
                "msgtype": "mixed",
                "image": {"url": "https://cdn.example.com/1.png"},
                "mixed": {
                    "msg_item": [
                        {"msgtype": "image", "image": {}},
                        {"msgtype": "image", "image": {"url": "https://cdn.example.com/2.png"}},
                    ]
                },
                "file": {"url": "https://cdn.example.com/3.pdf"},
                "video": {"url": "https://cdn.example.com/4.mp4"},
            }
        ).body
        attachments = extract_wecom_attachments(body)
        assert [attachment.url for attachment in attachments] == [
            "https://cdn.example.com/1.png",
            "https://cdn.example.com/2.png",
            "https://cdn.example.com/3.pdf",
            "https://cdn.example.com/4.mp4",
        ]
        assert attachments[-1].media_type == "video/mp4"


class TestCommandBuilding:
    async def test_subscribe_payload_shape(self) -> None:
        """订阅命令 wire 形状（bot_id + secret；无多余字段）。"""
        payload = json.loads(
            build_subscribe_command(req_id="r1", bot_id="bot-1", secret="sec-1").model_dump_json(
                by_alias=True, exclude_none=True
            )
        )
        assert payload == {
            "cmd": "aibot_subscribe",
            "headers": {"req_id": "r1"},
            "body": {"bot_id": "bot-1", "secret": "sec-1"},
        }

    async def test_ping_payload_has_no_body(self) -> None:
        """心跳命令无 body 字段（exclude_none 生效）。"""
        payload = json.loads(
            build_ping_command(req_id="r2").model_dump_json(by_alias=True, exclude_none=True)
        )
        assert payload == {"cmd": WECOM_CMD_PING, "headers": {"req_id": "r2"}}

    async def test_markdown_reply_uses_callback_req_id(self) -> None:
        """回复命令无需请求号，帧头为 callback req_id（平台按该标识归位）。"""
        payload = json.loads(
            build_markdown_reply_command(
                callback_req_id="cb-1", content="好的"
            ).model_dump_json(by_alias=True, exclude_none=True)
        )
        assert payload["cmd"] == WECOM_CMD_RESPOND_MSG
        assert payload["headers"]["req_id"] == "cb-1"
        assert payload["body"] == {"msgtype": "markdown", "markdown": {"content": "好的"}}

    async def test_markdown_push_carries_target(self) -> None:
        """主动推送携带 chatid + chat_type（单聊 = 1 / 群聊 = 2）。"""
        target = WeComChatTargetModel(
            chat_type=WECOM_CHAT_TYPE_GROUP, target_id="chat-1", is_group=True
        )
        payload = json.loads(
            build_markdown_push_command(req_id="r3", target=target, content="广播").model_dump_json(
                by_alias=True, exclude_none=True
            )
        )
        assert payload["cmd"] == WECOM_CMD_SEND_MSG
        assert payload["body"] == {
            "chatid": "chat-1",
            "chat_type": 2,
            "msgtype": "markdown",
            "markdown": {"content": "广播"},
        }

    @pytest.mark.parametrize(
        ("media_kind", "msgtype", "field"),
        [
            (WeComMediaKind.IMAGE, "image", "image"),
            (WeComMediaKind.VOICE, "voice", "voice"),
            (WeComMediaKind.FILE, "file", "file"),
        ],
    )
    async def test_media_push_bodies(self, media_kind: WeComMediaKind, msgtype: str, field: str) -> None:
        """附件推送按素材类型产出对应消息体（media_id 承载）。"""
        target = WeComChatTargetModel(
            chat_type=WECOM_CHAT_TYPE_SINGLE, target_id="u1", is_group=False
        )
        payload = json.loads(
            build_media_push_command(
                req_id="r4", target=target, media_kind=media_kind, media_id="mid-1"
            ).model_dump_json(by_alias=True, exclude_none=True)
        )
        assert payload["body"] == {
            "chatid": "u1",
            "chat_type": 1,
            "msgtype": msgtype,
            field: {"media_id": "mid-1"},
        }

    async def test_media_push_video_rejected(self) -> None:
        """视频素材类型无附件消息体（附件投递不产出视频消息）。"""
        target = WeComChatTargetModel(
            chat_type=WECOM_CHAT_TYPE_SINGLE, target_id="u1", is_group=False
        )
        with pytest.raises(BusinessException) as exc_info:
            build_media_push_command(
                req_id="r5", target=target, media_kind=WeComMediaKind.VIDEO, media_id="mid"
            )
        assert exc_info.value.code == ErrorCode.VALIDATION_ERROR

    async def test_upload_commands_wire_shape(self) -> None:
        """素材上传三步 wire 形状（初始化 type 别名 / 分片 base64 / 完成 upload_id）。"""
        init_payload = json.loads(
            build_upload_init_command(
                req_id="r6",
                media_kind=WeComMediaKind.IMAGE,
                filename="a.png",
                total_size=10,
                total_chunks=1,
                md5="deadbeef",
            ).model_dump_json(by_alias=True, exclude_none=True)
        )
        chunk_payload = json.loads(
            build_upload_chunk_command(
                req_id="r7",
                upload_id="up-1",
                chunk_index=0,
                base64_data=base64.b64encode(b"hello").decode("ascii"),
            ).model_dump_json(by_alias=True, exclude_none=True)
        )
        finish_payload = json.loads(
            build_upload_finish_command(req_id="r8", upload_id="up-1").model_dump_json(
                by_alias=True, exclude_none=True
            )
        )
        assert init_payload["cmd"] == WECOM_CMD_UPLOAD_MEDIA_INIT
        assert init_payload["body"] == {
            "type": "image",
            "filename": "a.png",
            "total_size": 10,
            "total_chunks": 1,
            "md5": "deadbeef",
        }
        assert chunk_payload["cmd"] == WECOM_CMD_UPLOAD_MEDIA_CHUNK
        assert chunk_payload["body"] == {
            "upload_id": "up-1",
            "chunk_index": 0,
            "base64_data": "aGVsbG8=",
        }
        assert finish_payload["cmd"] == WECOM_CMD_UPLOAD_MEDIA_FINISH
        assert finish_payload["body"] == {"upload_id": "up-1"}


class TestAttachmentKindMapping:
    @pytest.mark.parametrize(
        ("kind", "expected"),
        [
            (ChannelAttachmentKind.IMAGE, WeComMediaKind.IMAGE),
            (ChannelAttachmentKind.STICKER, WeComMediaKind.IMAGE),
            (ChannelAttachmentKind.AUDIO, WeComMediaKind.VOICE),
            (ChannelAttachmentKind.FILE, WeComMediaKind.FILE),
        ],
    )
    async def test_attachment_to_upload_kind(
        self, kind: ChannelAttachmentKind, expected: WeComMediaKind
    ) -> None:
        """附件 kind → 上传素材类型（贴纸归图片——企微无贴纸消息类型）。"""
        from packages.harness.channels.channel_models import ChannelMessageAttachment

        attachment = ChannelMessageAttachment(kind=kind, base64="aGk=")
        assert wecom_media_kind_for_attachment(attachment) is expected

    async def test_upload_size_limits(self) -> None:
        """素材类型上限与官方文档一致（图片 10MB / 语音 2MB / 视频 10MB / 文件 20MB）。"""
        assert wecom_upload_size_limit(WeComMediaKind.IMAGE) == 10 * 1024 * 1024
        assert wecom_upload_size_limit(WeComMediaKind.VOICE) == 2 * 1024 * 1024
        assert wecom_upload_size_limit(WeComMediaKind.VIDEO) == 10 * 1024 * 1024
        assert wecom_upload_size_limit(WeComMediaKind.FILE) == 20 * 1024 * 1024


class TestMarkdownSplitting:
    async def test_short_text_single_segment(self) -> None:
        """未超限不切分。"""
        assert split_wecom_markdown_text("你好") == ["你好"]

    async def test_empty_text_returns_empty(self) -> None:
        """空文本返回空列表（调用方拒发）。"""
        assert split_wecom_markdown_text("") == []

    async def test_newline_boundary_preferred(self) -> None:
        """窗口内有换行时按换行切分（段落不跨语义行，切点换行符被消费）。"""
        text = "第一段\n第二段\n第三段"
        segments = split_wecom_markdown_text(text, max_bytes=10)
        assert segments == ["第一段", "第二段", "第三段"]

    async def test_hard_cut_respects_utf8_boundaries(self) -> None:
        """无换行长文本按 UTF-8 字节硬切——不产生半个字符且可无损拼接。"""
        text = "汉" * 20
        segments = split_wecom_markdown_text(text, max_bytes=10)
        assert "".join(segments) == text
        assert all(len(segment.encode("utf-8")) <= 10 for segment in segments)
        # 每段最多 3 个汉字（9 字节），20 字 → 6 段满员 + 1 段余量。
        assert len(segments) == 7

    async def test_default_limit_is_platform_bound(self) -> None:
        """缺省上限为平台 20480 字节。"""
        text = "a" * (WECOM_MARKDOWN_MAX_BYTES + 1)
        segments = split_wecom_markdown_text(text)
        assert len(segments) == 2
        assert len(segments[0]) == WECOM_MARKDOWN_MAX_BYTES


class TestAckModel:
    async def test_ack_success_flag_and_upload_body(self) -> None:
        """应答成功判定 + 上传载荷（type 别名）解析。"""
        ack = WeComAckModel.model_validate(
            {
                "headers": {"req_id": "r1"},
                "errcode": 0,
                "errmsg": "ok",
                "body": {"type": "image", "media_id": "mid-9", "created_at": "1380000000"},
            }
        )
        assert ack.is_success is True
        assert ack.body is not None
        assert ack.body.media_kind == "image"
        assert ack.body.media_id == "mid-9"

    async def test_ack_failure_flag(self) -> None:
        """非零 errcode 判定失败。"""
        ack = WeComAckModel.model_validate({"headers": {"req_id": "r2"}, "errcode": 93000})
        assert ack.is_success is False

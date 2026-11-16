"""qq 通道适配器测试——NapCatQQAdapter（fake 客户端零真实网络）。

覆盖矩阵：

    - chat_id grammar 双形态（private / group → 对应 NapCat 动作 + 目标
      ID 解析 + 投递结果归一）；
    - 投递映射（ok 响应 message_id 归一字符串；wire 未回传 message_id
      → 空 message_ids）；
    - grammar 非法（未知前缀 / 非数字 / 非正整数）→ VALIDATION_ERROR；
    - 客户端失败上抛（适配器协议契约——路由器兜底隔离）。
"""

from __future__ import annotations

from typing import Any

import pytest

from packages.harness.channels.channel_models import OutboundMessageModel
from packages.harness.channels.qq.qq_adapter import NapCatQQAdapter
from packages.harness.channels.qq.qq_models import Ob11ApiResponseModel
from packages.harness.core.errors import BusinessException, ErrorCode


class FakeNapCatClient:
    """fake NapCatOneBotClient——记录调用形状 / 可编程失败。"""

    def __init__(
        self,
        *,
        response: Ob11ApiResponseModel | None = None,
        send_error: Exception | None = None,
    ) -> None:
        """初始化 fake 客户端。

        Args:
            response: 脚本化动作响应（None = 缺省 message_id 形态）。
            send_error: 发送故障注入（None = 恒成功）。
        """
        self.private_calls: list[dict[str, Any]] = []
        self.group_calls: list[dict[str, Any]] = []
        self._response = response or Ob11ApiResponseModel(
            status="ok", retcode=0, data={"message_id": -100200300}
        )
        self._send_error = send_error

    async def send_private_msg(self, user_id: int, text: str) -> Ob11ApiResponseModel:
        """fake 私聊发送（记录调用；可编程抛错）。"""
        self.private_calls.append({"user_id": user_id, "text": text})
        if self._send_error is not None:
            raise self._send_error
        return self._response

    async def send_group_msg(self, group_id: int, text: str) -> Ob11ApiResponseModel:
        """fake 群聊发送（记录调用；可编程抛错）。"""
        self.group_calls.append({"group_id": group_id, "text": text})
        if self._send_error is not None:
            raise self._send_error
        return self._response


def _outbound(chat_id: str, text: str = "你好") -> OutboundMessageModel:
    """构造出站消息（测试直供形态）。

    Args:
        chat_id: 目标会话 ID（grammar 形态）。
        text: 回复文本。

    Returns:
        :class:`OutboundMessageModel` 实例。
    """
    return OutboundMessageModel(chat_id=chat_id, text=text)


class TestChatIdGrammar:
    async def test_private_form_routes_send_private_msg(self) -> None:
        """private grammar：send_private_msg(user_id) + 投递结果归一。"""
        client = FakeNapCatClient()
        adapter = NapCatQQAdapter(client=client)
        result = await adapter.send_message(_outbound("private:10086"))
        assert client.private_calls == [{"user_id": 10086, "text": "你好"}]
        assert client.group_calls == []
        assert result.channel.value == "qq"
        assert result.chat_id == "private:10086"
        assert result.message_ids == ["-100200300"]

    async def test_group_form_routes_send_group_msg(self) -> None:
        """group grammar：send_group_msg(group_id) + 投递结果归一。"""
        client = FakeNapCatClient()
        adapter = NapCatQQAdapter(client=client)
        result = await adapter.send_message(_outbound("group:67890", text="收到"))
        assert client.group_calls == [{"group_id": 67890, "text": "收到"}]
        assert client.private_calls == []
        assert result.channel.value == "qq"
        assert result.chat_id == "group:67890"
        assert result.message_ids == ["-100200300"]

    async def test_client_face_exposed(self) -> None:
        """装配观测面：client 属性透传底层客户端（启动校验消费面）。"""
        client = FakeNapCatClient()
        adapter = NapCatQQAdapter(client=client)
        assert adapter.client is client


class TestDeliveryMapping:
    async def test_missing_message_id_yields_empty_ids(self) -> None:
        """wire 未回传 message_id：送达成功但 message_ids 空（无引用凭据）。"""
        client = FakeNapCatClient(response=Ob11ApiResponseModel(status="ok", retcode=0, data=None))
        adapter = NapCatQQAdapter(client=client)
        result = await adapter.send_message(_outbound("private:10086"))
        assert result.message_ids == []

    async def test_string_message_id_passthrough(self) -> None:
        """wire 回传字符串 message_id：原样承载（实现兼容形态）。"""
        client = FakeNapCatClient(
            response=Ob11ApiResponseModel(status="ok", retcode=0, data={"message_id": "-777"})
        )
        adapter = NapCatQQAdapter(client=client)
        result = await adapter.send_message(_outbound("group:1"))
        assert result.message_ids == ["-777"]


class TestGrammarValidation:
    async def test_unknown_prefix_rejected(self) -> None:
        """未知前缀：VALIDATION_ERROR（grammar 契约面）。"""
        adapter = NapCatQQAdapter(client=FakeNapCatClient())
        with pytest.raises(BusinessException) as exc_info:
            await adapter.send_message(_outbound("channel:10086"))
        assert exc_info.value.code == ErrorCode.VALIDATION_ERROR

    async def test_missing_prefix_rejected(self) -> None:
        """无前缀裸 ID：VALIDATION_ERROR（grammar 契约面）。"""
        adapter = NapCatQQAdapter(client=FakeNapCatClient())
        with pytest.raises(BusinessException) as exc_info:
            await adapter.send_message(_outbound("10086"))
        assert exc_info.value.code == ErrorCode.VALIDATION_ERROR

    async def test_non_numeric_target_rejected(self) -> None:
        """非数字目标 ID：VALIDATION_ERROR（QQ 号 / 群号形态契约）。"""
        adapter = NapCatQQAdapter(client=FakeNapCatClient())
        with pytest.raises(BusinessException) as exc_info:
            await adapter.send_message(_outbound("private:abc"))
        assert exc_info.value.code == ErrorCode.VALIDATION_ERROR

    async def test_non_positive_target_rejected(self) -> None:
        """非正整数目标 ID：VALIDATION_ERROR（QQ 号 / 群号为正整数）。"""
        adapter = NapCatQQAdapter(client=FakeNapCatClient())
        with pytest.raises(BusinessException) as exc_info:
            await adapter.send_message(_outbound("group:0"))
        assert exc_info.value.code == ErrorCode.VALIDATION_ERROR


class TestFailurePropagation:
    async def test_client_failure_propagates_for_router_isolation(self) -> None:
        """客户端失败上抛（适配器协议契约——路由器 handle_inbound 兜底）。"""
        client = FakeNapCatClient(
            send_error=BusinessException(
                code=ErrorCode.CHANNEL_API_ERROR, message="平台发送失败（测试注入）"
            )
        )
        adapter = NapCatQQAdapter(client=client)
        with pytest.raises(BusinessException) as exc_info:
            await adapter.send_message(_outbound("private:10086"))
        assert exc_info.value.code == ErrorCode.CHANNEL_API_ERROR

"""qq 通道适配器——NapCatQQAdapter（OneBot v11 发送面，无长驻 runner）。

入站形态（与 Telegram 的**拉取式**长轮询根本不同）：NapCat 部署侧配置
HTTP POST 事件上报，把 OneBot 事件**推送**到本后端的 webhook 端点
（``app/api/channels_qq``——P4 chunk 4 交付面）；因此 QQ 通道**没有
runner**——零后台任务，仅 webhook 处理器（``qq_webhook``）+ 本发送面
适配器。

chat_id grammar（锁定决策——文档化于 docs/backend/channels.md）：

    ==================  =========================  ==========================
    OutboundMessageModel  NapCat 动作               会话身份键（路由器维度）
    ==================  =========================  ==========================
    ``private:<user_id>``  send_private_msg          ``channel:qq:private:<uid>``
    ``group:<group_id>``   send_group_msg            ``channel:qq:group:<gid>``
    ==================  =========================  ==========================

    路由器按 ``channel:chat_id`` 维度隔离会话——QQ 私聊 / 群聊天然独立于
    Telegram 会话（前缀含通道名 + 目标类型，无跨通道撞键可能）。

引用回复（文档化偏差）：``OutboundMessageModel.reply_to_message_id``
**不透传**——OneBot v11 引用需构造 ``reply`` 消息段（会渲染成引用块），
而通道中立出站为纯文本语义；兜底消息本就不引用（channel_service 锁定），
正常回复的引用增益不抵段组装复杂度（后续通道增强面再评估）。

失败语义：发送失败（``CHANNEL_API_ERROR`` / ``CHANNEL_TOKEN_MISSING``）
按适配器协议**上抛**——路由器 ``handle_inbound`` 统一兜底隔离（单消息
错误不击穿 webhook / 路由链——与 telegram runner 发送面同契约）。
"""

from __future__ import annotations

from packages.harness.channels.channel_models import (
    ChannelDeliveryResultModel,
    OutboundMessageModel,
)
from packages.harness.channels.qq.qq_client import NapCatOneBotClient
from packages.harness.core.errors import BusinessException, ErrorCode

__all__ = ["NapCatQQAdapter", "QQ_PRIVATE_PREFIX", "QQ_GROUP_PREFIX"]

#: chat_id grammar 私聊前缀（``private:<user_id>``）。
QQ_PRIVATE_PREFIX: str = "private"
#: chat_id grammar 群聊前缀（``group:<group_id>``）。
QQ_GROUP_PREFIX: str = "group"


class NapCatQQAdapter:
    """QQ 通道适配器（NapCat OneBot v11 HTTP API 发送面）。

    实现 :class:`packages.harness.channels.channel_service.ChannelAdapterProtocol`
    的 ``send_message`` 面（结构化类型——无需显式继承）；出站经注入的
    :class:`~packages.harness.channels.qq.qq_client.NapCatOneBotClient`
    （共享 httpx client 承载——本类零连接资源、零长驻任务）。
    """

    def __init__(self, *, client: NapCatOneBotClient) -> None:
        """初始化适配器。

        Args:
            client: NapCat OneBot v11 客户端（lifespan 装配——共享池注入）。
        """
        self._client = client

    @property
    def client(self) -> NapCatOneBotClient:
        """底层客户端（装配观测面——启动校验 / 测试注入用）。"""
        return self._client

    async def send_message(self, message: OutboundMessageModel) -> ChannelDeliveryResultModel:
        """发送出站消息（chat_id grammar 解析 → NapCat 动作 → 投递映射）。

        Args:
            message: 出站消息（通道中立形态；``chat_id`` 须为
                ``private:<user_id>`` / ``group:<group_id>`` grammar）。

        Returns:
            投递结果（``data.message_id`` 归一字符串形态；wire 未回传
            message_id 时 ``message_ids`` 为空列表——送达成功但无凭据）。

        Raises:
            BusinessException: chat_id grammar 非法（``VALIDATION_ERROR``
                ——调用方 bug 面）；平台调用失败（``CHANNEL_API_ERROR`` /
                ``CHANNEL_TOKEN_MISSING``——路由器负责兜底隔离）。
        """
        target_type, _, target_id = message.chat_id.partition(":")
        if target_type not in (QQ_PRIVATE_PREFIX, QQ_GROUP_PREFIX):
            raise BusinessException(
                code=ErrorCode.VALIDATION_ERROR,
                message=(
                    "QQ chat_id 须为 private:<user_id> 或 group:<group_id> 形态: "
                    f"{message.chat_id!r}"
                ),
                details={"chat_id": message.chat_id},
            )
        try:
            numeric_id = int(target_id)
        except ValueError as exc:
            raise BusinessException(
                code=ErrorCode.VALIDATION_ERROR,
                message=f"QQ chat_id 目标 ID 须为数字: {target_id!r}",
                details={"chat_id": message.chat_id},
            ) from exc
        if numeric_id < 1:
            raise BusinessException(
                code=ErrorCode.VALIDATION_ERROR,
                message=f"QQ chat_id 目标 ID 须为正整数: {target_id!r}",
                details={"chat_id": message.chat_id},
            )
        if target_type == QQ_PRIVATE_PREFIX:
            response = await self._client.send_private_msg(numeric_id, message.text)
        else:
            response = await self._client.send_group_msg(numeric_id, message.text)
        return response.to_delivery_result(message.chat_id)

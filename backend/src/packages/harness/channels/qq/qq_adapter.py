"""qq 通道适配器——NapCatQQAdapter（OneBot v11 发送面，无长驻 runner）。

入站形态（与 Telegram 的**拉取式**长轮询根本不同）：NapCat 部署侧配置
HTTP POST 事件上报，把 OneBot 事件**推送**到本后端的 webhook 端点
（``app/api/channels_qq``——webhook 交付面）；因此 QQ 通道**没有
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

v2 出站（附件在场时——段数组形态）：文本段先、媒体段后**同消息**投递
（``params.message`` 段数组——OneBot v11 message 字段另一合法形态，见
``qq_client`` 模块 docstring）；整条消息一次动作投递——**段内附件无
独立失败面**（动作失败整条上抛走 v1 语义；逐附件结果按送达构造：
file 类附件映射无 OneBot v11 消息段——``ok=False`` 逐附件结果承载，
文本与其余段照常投递）。``silent`` 优雅忽略（QQ 无通知概念——文档化
no-op）；caption 无段级承载概念——丢弃（段发射文档化于
``qq_models.attachment_to_ob11_segment``）。无附件时 v1 纯文本路径
**调用形态零变更**（纯文本字符串直传）。

失败语义：发送失败（``CHANNEL_API_ERROR`` / ``CHANNEL_TOKEN_MISSING``）
按适配器协议**上抛**——路由器 ``handle_inbound`` 统一兜底隔离（单消息
错误不击穿 webhook / 路由链——与 telegram runner 发送面同契约）。
"""

from __future__ import annotations

from packages.harness.channels.channel_models import (
    AttachmentDeliveryResult,
    ChannelDeliveryResultModel,
    OutboundMessageModel,
)
from packages.harness.channels.qq.qq_client import NapCatOneBotClient
from packages.harness.channels.qq.qq_models import (
    QQ_FILE_ATTACHMENT_UNSUPPORTED_MESSAGE,
    Ob11ApiResponseModel,
    Ob11MessageSegmentModel,
    attachment_to_ob11_segment,
)
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

        v2 分流：无附件时 v1 纯文本路径**调用形态零变更**（纯文本字符串
        直传）；附件在场时段数组形态（文本段先、媒体段后同消息——见
        模块 docstring「v2 出站」）。``silent`` 优雅忽略（QQ 无通知概念
        ——no-op，不映射任何平台参数）。

        Args:
            message: 出站消息（通道中立形态；``chat_id`` 须为
                ``private:<user_id>`` / ``group:<group_id>`` grammar）。

        Returns:
            投递结果（``data.message_id`` 归一字符串形态；wire 未回传
            message_id 时 ``message_ids`` 为空列表——送达成功但无凭据；
            v2 附件在场时 ``attachment_results`` 按附件序承载逐附件
            结局——file 类为 ok=False 文档化失败，其余随整条消息送达）。

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
        if not message.attachments:
            if target_type == QQ_PRIVATE_PREFIX:
                response = await self._client.send_private_msg(numeric_id, message.text)
            else:
                response = await self._client.send_group_msg(numeric_id, message.text)
            return response.to_delivery_result(message.chat_id)
        segments, pending_indexes = self._build_segments(message)
        if target_type == QQ_PRIVATE_PREFIX:
            response = await self._client.send_private_msg_segments(numeric_id, segments)
        else:
            response = await self._client.send_group_msg_segments(numeric_id, segments)
        attachment_results = self._aggregate_attachment_results(response, pending_indexes)
        attachment_results.extend(self._unsupported_attachment_results(message, pending_indexes))
        attachment_results.sort(key=lambda result: result.index)
        return response.to_delivery_result(message.chat_id, attachment_results=attachment_results)

    @staticmethod
    def _build_segments(
        message: OutboundMessageModel,
    ) -> tuple[list[Ob11MessageSegmentModel], list[int]]:
        """组装出站段数组（文本段先 + 媒体段后；不支持的附件记失败序）。

        Args:
            message: 携附件的出站消息。

        Returns:
            ``(segments, pending_indexes)`` 二元组：段列表（文本段恒在
            首位——空文本段不可能，``OutboundMessageModel.text`` 非空）
            与**已映射进段**的附件序列表（送达后按序构造成功结果）。
            未映射附件（file 类）不进段——失败结果由调用方聚合。
        """
        segments = [Ob11MessageSegmentModel(type="text", data={"text": message.text})]
        pending_indexes: list[int] = []
        for index, attachment in enumerate(message.attachments):
            segment = attachment_to_ob11_segment(attachment)
            if segment is None:
                continue
            segments.append(segment)
            pending_indexes.append(index)
        return segments, pending_indexes

    @staticmethod
    def _aggregate_attachment_results(
        response: Ob11ApiResponseModel,
        pending_indexes: list[int],
    ) -> list[AttachmentDeliveryResult]:
        """聚合逐附件投递结果（整条消息一次动作——按段内附件序构造）。

        Args:
            response: 动作响应包裹（成功形态——失败已上抛走 v1 语义）。
            pending_indexes: 已映射进段的附件序列表。

        Returns:
            逐附件结果列表：**已映射附件**全部 ok=True（共享平台消息
            ID——段数组消息无独立失败面；wire 未回传 message_id 时为
            None 凭据）；未映射附件（file 类）由调用方另行追加 ok=False
            条目——本方法只构造送达面。
        """
        raw_id = response.data.get("message_id") if response.data is not None else None
        provider_id: str | None = None
        if isinstance(raw_id, int):
            provider_id = str(raw_id)
        elif isinstance(raw_id, str) and raw_id:
            provider_id = raw_id
        return [
            AttachmentDeliveryResult(index=index, ok=True, provider_message_id=provider_id)
            for index in pending_indexes
        ]

    @staticmethod
    def _unsupported_attachment_results(
        message: OutboundMessageModel,
        delivered_indexes: list[int],
    ) -> list[AttachmentDeliveryResult]:
        """构造未投递附件的失败结果（file 类——无 OneBot v11 消息段）。

        Args:
            message: 携附件的出站消息。
            delivered_indexes: 已投递附件序集合（失败面 = 全集差集）。

        Returns:
            失败结果列表（按附件序；error 承载文档化失败文案——见
            :data:`qq_models.QQ_FILE_ATTACHMENT_UNSUPPORTED_MESSAGE`）。
        """
        delivered = set(delivered_indexes)
        return [
            AttachmentDeliveryResult(
                index=index,
                ok=False,
                error=QQ_FILE_ATTACHMENT_UNSUPPORTED_MESSAGE,
            )
            for index in range(len(message.attachments))
            if index not in delivered
        ]

"""qq 通道 wire Model Classes——OneBot v11（NapCat 实现面）子集（Unit 文件）。

OneBot v11 是 QQ 机器人开放协议（NapCat 为其一个实现——部署机上的独立
服务，暴露 HTTP API + HTTP POST 事件上报）；本工程消费其**严格类型化子集**：
出站动作包裹（``{"action": ..., "params": {...}}``）与入站事件上报载荷。
锁定设计决策：不引入任何 OneBot SDK——手写客户端（见 ``qq_client``），
wire 模型只声明消费到的字段（``extra="ignore"`` 容忍实现加字段）。

wire 归一约定：

    - **消息双形态**：OneBot v11 的 ``message`` 字段既可以是**段数组**
      （``[{"type": "text", "data": {"text": "你好"}}]``）也可以是 **CQ
      码字符串**（``"你好[CQ:image,file=x.jpg]"``）——本模型以联合类型
      承载双形态，展平为纯文本由 ``qq_webhook`` 承担；
    - OneBot ``time`` 为 **epoch 秒**——webhook 处理器映射
      InboundMessageModel 时 ×1000 归一为工程统一的 epoch 毫秒；
    - ``message_id`` 为 int（NapCat 实现下常为负值——合法形态，去重键与
      引用目标均按值承载，通道层归一字符串形态）；
    - ``Ob11ApiResponseModel`` 为动作响应包裹（``status`` / ``retcode`` /
      ``data``）——OneBot v11 语义：``retcode == 0`` 且 ``status`` 为
      ``ok`` / ``async`` 才是成功（``async`` = 已受理异步执行）；
    - 投递映射（``to_delivery_result``）：send_* 动作响应的 ``data``
      携带 ``{"message_id": N}``——归一为通道中立
      :class:`~packages.harness.channels.channel_models.ChannelDeliveryResultModel`
      的 ``message_ids``（wire 未回传 message_id 时为空列表——送达成功
      但无引用凭据）。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from packages.harness.channels.channel_models import (
    ChannelDeliveryResultModel,
    ChannelType,
)

__all__ = [
    "Ob11MessageSegmentModel",
    "Ob11EventModel",
    "Ob11ApiRequestModel",
    "Ob11ApiResponseModel",
    "Ob11LoginInfoModel",
    "OB11_SUCCESS_STATUSES",
]

#: 动作响应的成功 status 值域（``ok`` = 同步完成；``async`` = 已受理
#: 异步执行——两者均视为投递成功）。
OB11_SUCCESS_STATUSES: frozenset[str] = frozenset({"ok", "async"})


class Ob11MessageSegmentModel(BaseModel):
    """OneBot v11 消息段（``{type, data}`` 二段形态）。

    Attributes:
        type: 段类型（``text`` / ``image`` / ``face`` / ``at`` / ``reply``
            等——OneBot v11 标准段类型值域，实现可扩展）。
        data: 段数据（按类型取键——text 段取 ``text``、image 段取
            ``url`` / ``file`` 等；自由形态 dict 承载）。
    """

    model_config = ConfigDict(extra="ignore")

    type: str = Field(default="", description="段类型（text / image / face 等）")
    data: dict[str, Any] = Field(default_factory=dict, description="段数据（按类型取键）")


class Ob11EventModel(BaseModel):
    """OneBot v11 事件对象（HTTP POST 上报载荷——子集 + 容错形态）。

    单一模型承载全部 ``post_type`` 大类（message / notice / request /
    meta_event）：非 message 大类的专有字段经 ``extra="ignore"`` 容忍；
    message 大类外的字段全部给了缺省值（解析恒成功——非法 / 缺字段形态
    由 webhook 处理器按「确认并忽略」语义隔离，不进 HTTP 错误面）。

    Attributes:
        post_type: 事件大类（``message`` / ``notice`` / ``request`` /
            ``meta_event``——仅 message 进路由，其余确认并忽略）。
        time: 事件时间（**epoch 秒**——wire 原生形态；映射入站时 ×1000
            归一为 epoch 毫秒）。
        message_type: 消息类型（message 事件：``private`` / ``group``）。
        sub_type: 子类型（私聊 ``friend`` / 群消息 ``normal`` / ``anonymous``
            等——观测面字段）。
        message_id: 消息 ID（去重键；int 形态，NapCat 实现下常为负值）。
        user_id: 发送者 QQ 号（私聊即对端用户；群消息为发言者）。
        group_id: 群号（仅群消息事件携带；私聊为 None）。
        message: 消息内容（**段数组或 CQ 码字符串双形态**——联合类型承载，
            展平为纯文本由 ``qq_webhook.flatten_message_text`` 承担）。
    """

    model_config = ConfigDict(extra="ignore")

    post_type: str = Field(
        default="", description="事件大类（message / notice / request / meta_event）"
    )
    time: int = Field(default=0, description="事件时间（epoch 秒——wire 原生形态）")
    message_type: str = Field(default="", description="消息类型（private / group）")
    sub_type: str = Field(default="", description="消息子类型（观测面字段）")
    message_id: int = Field(default=0, description="消息 ID（去重键；可为负值）")
    user_id: int = Field(default=0, description="发送者 QQ 号")
    group_id: int | None = Field(default=None, description="群号（仅群消息事件）")
    message: list[Ob11MessageSegmentModel] | str = Field(
        default="", description="消息内容（段数组或 CQ 码字符串双形态）"
    )


class Ob11ApiRequestModel(BaseModel):
    """OneBot v11 HTTP API 动作请求包裹（单一动作对象形态）。

    NapCat HTTP API 的文档化调用形态：``POST {http_api_base_url}`` 请求体
    即本包裹（``{"action": "...", "params": {...}}``——动作名与参数同体；
    与「动作名进 URL 路径」形态二选一，本工程锁定单一对象形态）。

    Attributes:
        action: 动作名（``send_private_msg`` / ``send_group_msg`` /
            ``get_login_info`` 等）。
        params: 动作参数（按动作取键——自由形态 dict 承载）。
    """

    action: str = Field(min_length=1, description="动作名（如 send_private_msg）")
    params: dict[str, Any] = Field(default_factory=dict, description="动作参数")


class Ob11ApiResponseModel(BaseModel):
    """OneBot v11 HTTP API 动作响应包裹。

    Attributes:
        status: 执行状态（``ok`` = 同步完成 / ``async`` = 已受理异步执行 /
            ``failed`` = 失败）。
        retcode: 返回码（0 = 成功——与 status 联合判定，见 ``is_success``）。
        data: 动作载荷（send_* 携带 ``{"message_id": N}``；get_login_info
            携带 ``{"user_id": N, "nickname": "..."}``；可为 None）。
    """

    model_config = ConfigDict(extra="ignore")

    status: str = Field(default="", description="执行状态（ok / async / failed）")
    retcode: int = Field(default=0, description="返回码（0 = 成功）")
    data: dict[str, Any] | None = Field(default=None, description="动作载荷")

    @property
    def is_success(self) -> bool:
        """是否投递成功（``retcode == 0`` 且 ``status`` ∈ {ok, async}）。

        Returns:
            成功判定（``async`` 受理形态亦为 True——出站确认语义）。
        """
        return self.retcode == 0 and self.status in OB11_SUCCESS_STATUSES

    def to_delivery_result(self, chat_id: str) -> ChannelDeliveryResultModel:
        """映射通道中立投递结果（send_* 动作响应 → DeliveryResult）。

        Args:
            chat_id: 目标会话 ID（``private:<user_id>`` / ``group:<group_id>``
                形态——发送面保留原 grammar 串）。

        Returns:
            投递结果（``data.message_id`` 归一字符串形态；wire 未回传
            message_id 时 ``message_ids`` 为空列表——送达成功但无引用凭据）。
        """
        message_ids: list[str] = []
        raw_id = self.data.get("message_id") if self.data is not None else None
        if isinstance(raw_id, int):
            message_ids = [str(raw_id)]
        elif isinstance(raw_id, str) and raw_id:
            message_ids = [raw_id]
        return ChannelDeliveryResultModel(
            channel=ChannelType.QQ,
            chat_id=chat_id,
            message_ids=message_ids,
        )


class Ob11LoginInfoModel(BaseModel):
    """OneBot v11 ``get_login_info`` 动作载荷（启动校验消费面）。

    Attributes:
        user_id: 登录账号 QQ 号（机器人自身账号）。
        nickname: 登录账号昵称（可能缺失——缺省空串）。
    """

    model_config = ConfigDict(extra="ignore")

    user_id: int = Field(default=0, description="登录账号 QQ 号")
    nickname: str = Field(default="", description="登录账号昵称")

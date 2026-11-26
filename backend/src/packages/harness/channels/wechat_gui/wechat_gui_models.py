"""GUI 微信通道 Model Classes——截图解析 / 会话快照 / 联系人 / 会话键 / 设置。

定位：个人微信没有稳定的官方个人号消息 API，本通道以「沙盒内 GUI 微信 +
截图解析 + 键鼠注入」作为实验轨执行面（生产主轨仍是企业微信长连接）。
本模块只承载通道层语义（视觉解析结果 / 会话键 / 去重键 / 归一映射 /
配置面），HTTP 调用在 ``wechat_gui_client``，视觉解析在
``wechat_gui_reader``，轮询与出站在 ``wechat_gui_runner`` /
``wechat_gui_adapter``。

会话键（锁定）：``wechat_gui:<联系人备注名>``——与其余通道的「通道内目标
前缀 + 平台标识」同构；路由器会话身份键因此为
``channel:wechat_gui:<联系人备注名>``。备注名不是稳定主键（可改名 / 重名），
因此入站与出站都以视觉模型对当前聊天窗口标题的校验为落定前提：标题与
目标备注名不一致即拒绝（fail-closed，见 reader / adapter）。

去重键（锁定）：``备注名 + 消息文本 + 时间窗`` 的 sha256——GUI 侧没有平台
消息 ID，同一画面会在每次截图重复出现，去重是正确性硬前提；时间窗把
「同一条消息跨多次轮询」收敛到同一键，同时允许同一文本在窗口外再次出现。
"""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import Any, Final, Self

from pydantic import (
    BaseModel,
    Field,
    PrivateAttr,
    SecretStr,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict

from packages.harness.channels.channel_models import ChannelType, InboundMessageModel
from packages.harness.core.errors import BusinessException, ErrorCode

__all__ = [
    "WECHAT_GUI_CHAT_ID_PREFIX",
    "WECHAT_GUI_DEDUP_WINDOW_MS",
    "WECHAT_GUI_DEDUP_WINDOW_SIZE",
    "WECHAT_GUI_MIN_CONFIDENCE",
    "WechatGuiContactModel",
    "WechatGuiMessageModel",
    "WechatGuiOpenChatResultModel",
    "WechatGuiScreenshotModel",
    "WechatGuiSessionKey",
    "WechatGuiSettings",
    "WechatGuiSnapshotModel",
    "TitleMismatchAction",
    "VlmReadResultModel",
    "build_wechat_gui_chat_id",
    "map_wechat_gui_snapshot_to_inbound",
    "parse_wechat_gui_chat_id",
    "wechat_gui_message_dedup_key",
]

#: 会话键前缀（出站 chat_id 的 grammar 段——与 ``parse_wechat_gui_chat_id`` 对称）。
WECHAT_GUI_CHAT_ID_PREFIX: Final[str] = "wechat_gui:"

#: 消息去重时间窗（毫秒；同一文本 + 同一窗口视为同一条消息）。
WECHAT_GUI_DEDUP_WINDOW_MS: Final[int] = 5 * 60 * 1000

#: 进程内去重窗口容量（有界 FIFO——重启清零）。
WECHAT_GUI_DEDUP_WINDOW_SIZE: Final[int] = 1024

#: 视觉解析最低置信度（低于阈值按解析失败处理——fail-closed）。
WECHAT_GUI_MIN_CONFIDENCE: Final[float] = 0.5


class TitleMismatchAction(StrEnum):
    """标题与目标备注名不一致时的处置方式。"""

    SKIP = "skip"
    FAIL = "fail"


class WechatGuiMessageModel(BaseModel):
    """视觉模型解析出的单条聊天消息。

    Attributes:
        sender: 消息发送者显示名（截图可见文本；缺失为空串）。
        text: 消息正文（纯文本；图片 / 表情 / 语音等非文本消息为占位文本）。
        is_self: 是否本账号发出（True 的消息不进入路由）。
        ts_ms: 消息时间（epoch 毫秒；截图不可读时为 0——归一按采集时刻兜底）。
    """

    model_config = {"extra": "ignore"}

    sender: str = Field(default="", description="发送者显示名")
    text: str = Field(default="", description="消息正文")
    is_self: bool = Field(default=False, description="是否本账号发出")
    ts_ms: int = Field(default=0, ge=0, description="消息时间（epoch 毫秒；0 = 未报告）")


class VlmReadResultModel(BaseModel):
    """视觉模型对一张截图的解析结果（严格 JSON 的校验形态）。

    Attributes:
        title: 当前聊天窗口标题（联系人备注名 / 群名）。
        messages: 解析出的消息（时间序）。
        confidence: 解析置信度（0.0-1.0；低于阈值按失败处理）。
        raw_json: 模型返回的原始 JSON 对象（审计面——已通过结构校验）。
    """

    model_config = {"extra": "ignore"}

    title: str = Field(min_length=1, description="当前聊天窗口标题")
    messages: list[WechatGuiMessageModel] = Field(
        default_factory=list, description="解析出的消息（时间序）"
    )
    confidence: float = Field(ge=0.0, le=1.0, description="解析置信度（0.0-1.0）")
    raw_json: dict[str, Any] = Field(default_factory=dict, description="原始 JSON 对象")

    @field_validator("title")
    @classmethod
    def _strip_title(cls, value: str) -> str:
        """标题去首尾空白并拒绝空白标题。

        Args:
            value: 原始标题。

        Returns:
            去空白后的标题。

        Raises:
            ValueError: 标题为纯空白。
        """
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("标题不得为空白")
        return cleaned


class WechatGuiSnapshotModel(BaseModel):
    """一次采集的会话快照（runner 归一前的中间形态）。

    Attributes:
        title: 当前聊天窗口标题（视觉解析结果）。
        messages: 解析出的消息（时间序）。
        screenshot_sha256: 截图内容 sha256（变化检测与观测面）。
        captured_at_ms: 采集时刻（epoch 毫秒）。
    """

    model_config = {"extra": "ignore"}

    title: str = Field(min_length=1, description="当前聊天窗口标题")
    messages: list[WechatGuiMessageModel] = Field(
        default_factory=list, description="解析出的消息（时间序）"
    )
    screenshot_sha256: str = Field(min_length=1, description="截图 sha256")
    captured_at_ms: int = Field(ge=0, description="采集时刻（epoch 毫秒）")

    @field_validator("title")
    @classmethod
    def _strip_title(cls, value: str) -> str:
        """标题去首尾空白并拒绝空白标题。

        Args:
            value: 原始标题。

        Returns:
            去空白后的标题。

        Raises:
            ValueError: 标题为纯空白。
        """
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("标题不得为空白")
        return cleaned


class WechatGuiContactModel(BaseModel):
    """联系人配置（会话目标 + 人格标注 + 启停）。

    Attributes:
        remark_name: 联系人备注名（会话键原料与搜索输入）。
        persona_id: 人格标识（配置面登记；会话人格仍由会话服务按主持久化
            人格解析，本字段供观测与后续按联系人路由扩展）。
        enabled: 是否轮询该联系人（False = 不打开 / 不采集）。
        is_owner: 是否按主人来源落档（True = ``owner``，False = ``peer``）。
    """

    model_config = {"extra": "forbid"}

    remark_name: str = Field(min_length=1, description="联系人备注名")
    persona_id: str = Field(default="", description="人格标识（配置面登记）")
    enabled: bool = Field(default=True, description="是否轮询该联系人")
    is_owner: bool = Field(default=False, description="是否按主人来源落档")

    @field_validator("remark_name")
    @classmethod
    def _strip_remark_name(cls, value: str) -> str:
        """备注名去首尾空白并拒绝空白值。

        Args:
            value: 原始备注名。

        Returns:
            去空白后的备注名。

        Raises:
            ValueError: 备注名为纯空白。
        """
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("备注名不得为空白")
        return cleaned


class WechatGuiSessionKey(BaseModel):
    """通道会话键（``wechat_gui:<备注名>``——构造 / 解析的模型化入口）。

    Attributes:
        remark_name: 联系人备注名（会话键的可变段）。
    """

    model_config = {"extra": "forbid", "frozen": True}

    remark_name: str = Field(min_length=1, description="联系人备注名")

    @field_validator("remark_name")
    @classmethod
    def _strip_remark_name(cls, value: str) -> str:
        """备注名去首尾空白并拒绝空白值。

        Args:
            value: 原始备注名。

        Returns:
            去空白后的备注名。

        Raises:
            ValueError: 备注名为纯空白。
        """
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("备注名不得为空白")
        return cleaned

    @property
    def chat_id(self) -> str:
        """通道出站 / 入站共用的 chat_id。"""
        return f"{WECHAT_GUI_CHAT_ID_PREFIX}{self.remark_name}"

    @classmethod
    def from_chat_id(cls, chat_id: str) -> WechatGuiSessionKey:
        """从 chat_id 解析会话键（出站 grammar 校验）。

        Args:
            chat_id: 通道中立出站消息的 chat_id。

        Returns:
            会话键模型。

        Raises:
            BusinessException: 前缀缺失 / 后缀为空（``VALIDATION_ERROR``）。
        """
        if not chat_id.startswith(WECHAT_GUI_CHAT_ID_PREFIX):
            raise BusinessException(
                code=ErrorCode.VALIDATION_ERROR,
                message=(
                    f"GUI 微信通道 chat_id 必须以 {WECHAT_GUI_CHAT_ID_PREFIX!r} "
                    f"开头: {chat_id!r}"
                ),
            )
        remark_name = chat_id[len(WECHAT_GUI_CHAT_ID_PREFIX) :].strip()
        if not remark_name:
            raise BusinessException(
                code=ErrorCode.VALIDATION_ERROR,
                message=f"GUI 微信通道 chat_id 缺少备注名: {chat_id!r}",
            )
        return cls(remark_name=remark_name)


class WechatGuiScreenshotModel(BaseModel):
    """沙盒截图结果（``screenshot`` 工具返回）。

    Attributes:
        image_base64: PNG base64 载荷。
        sha256: 截图内容 sha256（变化检测）。
        width: 截图宽度（像素）。
        height: 截图高度（像素）。
    """

    model_config = {"extra": "ignore"}

    image_base64: str = Field(min_length=1, description="PNG base64 载荷")
    sha256: str = Field(min_length=1, description="截图 sha256")
    width: int = Field(ge=1, description="截图宽度（像素）")
    height: int = Field(ge=1, description="截图高度（像素）")


class WechatGuiOpenChatResultModel(BaseModel):
    """``open_chat`` 工具结果（标题区域 + 全屏截图）。

    Attributes:
        remark_name: 搜索并打开的目标备注名。
        title_image_base64: 标题区域 PNG base64（视觉校验输入）。
        title_sha256: 标题区域截图 sha256。
        full_image_base64: 全屏 PNG base64（整窗解析输入）。
        full_sha256: 全屏截图 sha256。
    """

    model_config = {"extra": "ignore"}

    remark_name: str = Field(min_length=1, description="目标备注名")
    title_image_base64: str = Field(min_length=1, description="标题区域 PNG base64")
    title_sha256: str = Field(min_length=1, description="标题区域截图 sha256")
    full_image_base64: str = Field(min_length=1, description="全屏 PNG base64")
    full_sha256: str = Field(min_length=1, description="全屏截图 sha256")


class WechatGuiSettings(BaseSettings):
    """GUI 微信实验轨通道配置（env_prefix=``WECHAT_GUI_``）。

    默认关闭：``enabled=False`` 时 app lifespan 零对象构造、零轮询任务。
    ``enabled=True`` 时需要沙盒 agent 可达且 token 与沙盒侧 token 文件同值；
    缺失任一条件即只跳过本通道（不影响其余通道）。

    Attributes:
        enabled: 是否启用 GUI 微信通道。
        sandbox_url: 沙盒 agent 基础 URL（仅回环部署形态）。
        token: Bearer token（SecretStr；与沙盒 token 文件同值）。
        poll_interval_seconds: 轮询间隔秒数（出站请求量的唯一闸）。
        request_timeout_seconds: 单次 agent 请求超时秒数。
        backoff_base_seconds: 轮询失败退避基数秒。
        backoff_max_seconds: 退避封顶秒数（不得小于基数）。
        max_messages_per_poll: 单次解析消息上限（1-100）。
        contacts_json: 联系人配置 JSON 数组（每项含 remark_name / persona_id /
            enabled / is_owner——env 面标量载体）。
        vlm_entry: 指定视觉模型条目名（空串 = 走 look_at 角色回退链）。
        title_mismatch_action: 标题与目标备注名不一致时的处置方式。
    """

    enabled: bool = Field(default=False, description="是否启用 GUI 微信通道")
    sandbox_url: str = Field(
        default="http://127.0.0.1:8090", description="沙盒 agent 基础 URL（仅回环）"
    )
    token: SecretStr = Field(default=SecretStr(""), description="Bearer token")
    poll_interval_seconds: float = Field(default=5.0, gt=0, description="轮询间隔秒数")
    request_timeout_seconds: float = Field(default=15.0, gt=0, description="agent 请求超时秒数")
    backoff_base_seconds: float = Field(default=2.0, gt=0, description="轮询失败退避基数秒")
    backoff_max_seconds: float = Field(default=60.0, gt=0, description="退避封顶秒数")
    max_messages_per_poll: int = Field(
        default=20, ge=1, le=100, description="单次解析消息上限（1-100）"
    )
    contacts_json: str = Field(
        default="[]",
        description=(
            "联系人配置 JSON 数组（每项含 remark_name / persona_id / enabled / "
            "is_owner）"
        ),
    )
    vlm_entry: str = Field(default="", description="指定视觉模型条目名（空 = 回退链）")
    title_mismatch_action: TitleMismatchAction = Field(
        default=TitleMismatchAction.SKIP, description="标题不一致处置方式"
    )

    model_config = SettingsConfigDict(
        env_prefix="WECHAT_GUI_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @field_validator("sandbox_url")
    @classmethod
    def _validate_sandbox_url(cls, value: str) -> str:
        """校验沙盒 URL（http/https 且含主机名）。

        Args:
            value: 原始配置值。

        Returns:
            去除尾斜杠后的 URL。

        Raises:
            ValueError: 协议非法或主机名缺失。
        """
        cleaned = value.strip().rstrip("/")
        if not cleaned.startswith(("http://", "https://")):
            raise ValueError("WECHAT_GUI_SANDBOX_URL 必须是 http/https URL")
        return cleaned

    @field_validator("vlm_entry")
    @classmethod
    def _strip_vlm_entry(cls, value: str) -> str:
        """视觉条目名去首尾空白（空串 = 走回退链）。

        Args:
            value: 原始配置值。

        Returns:
            去空白后的条目名。
        """
        return value.strip()

    @model_validator(mode="after")
    def _verify_backoff_bounds(self) -> Self:
        """校验退避边界并解析联系人 JSON（配置面 fail fast）。

        Returns:
            校验通过的实例。

        Raises:
            ValueError: ``backoff_max_seconds < backoff_base_seconds``；联系人
                JSON 非法 / 根节点非数组 / 记录重复或字段非法。
        """
        if self.backoff_max_seconds < self.backoff_base_seconds:
            raise ValueError(
                f"backoff_max_seconds({self.backoff_max_seconds}) 不得小于 "
                f"backoff_base_seconds({self.backoff_base_seconds})"
            )
        try:
            raw = json.loads(self.contacts_json or "[]")
        except json.JSONDecodeError as exc:
            raise ValueError(f"WECHAT_GUI_CONTACTS_JSON 不是合法 JSON：{exc}") from exc
        if not isinstance(raw, list):
            raise ValueError("WECHAT_GUI_CONTACTS_JSON 根节点必须是数组")
        mapping: dict[str, WechatGuiContactModel] = {}
        for item in raw:
            contact = WechatGuiContactModel.model_validate(item)
            if contact.remark_name in mapping:
                raise ValueError(f"联系人重复：{contact.remark_name!r}")
            mapping[contact.remark_name] = contact
        self._parsed_contacts = mapping
        return self

    _parsed_contacts: dict[str, WechatGuiContactModel] = PrivateAttr(default_factory=dict)

    @property
    def contacts(self) -> dict[str, WechatGuiContactModel]:
        """解析后的联系人映射（备注名 → 配置；只读副本）。

        Returns:
            联系人映射（构造 / 校验期已解析）。
        """
        return dict(self._parsed_contacts)


def build_wechat_gui_chat_id(remark_name: str) -> str:
    """构造通道会话键（``wechat_gui:<备注名>``）。

    Args:
        remark_name: 联系人备注名（非空白）。

    Returns:
        通道出站 / 入站共用的 chat_id 字符串。

    Raises:
        ValueError: 备注名为纯空白。
    """
    return WechatGuiSessionKey(remark_name=remark_name).chat_id


def parse_wechat_gui_chat_id(chat_id: str) -> str:
    """解析通道会话键为备注名（出站 grammar 校验）。

    Args:
        chat_id: 通道中立出站消息的 chat_id。

    Returns:
        备注名（``wechat_gui:`` 之后的原值）。

    Raises:
        BusinessException: 前缀缺失 / 后缀为空（``VALIDATION_ERROR``——
            调用方 bug 面，不进重试）。
    """
    return WechatGuiSessionKey.from_chat_id(chat_id).remark_name


def wechat_gui_message_dedup_key(
    remark_name: str,
    message: WechatGuiMessageModel,
    *,
    captured_at_ms: int,
    window_ms: int = WECHAT_GUI_DEDUP_WINDOW_MS,
) -> str:
    """计算消息去重键（备注名 + 文本 + 时间窗的 sha256）。

    GUI 侧没有平台消息 ID：同一画面在每次截图都会重复出现，去重键因此
    由「备注名 + 文本 + 时间窗桶」派生。时间取消息自身时间（不可读时用
    采集时刻兜底），桶宽由 ``window_ms`` 决定。

    Args:
        remark_name: 会话备注名。
        message: 解析出的消息。
        captured_at_ms: 采集时刻（消息时间缺失时的兜底）。
        window_ms: 时间窗宽度（毫秒）。

    Returns:
        十六进制 sha256 去重键（同时用作入站 message_id）。
    """
    anchor_ms = message.ts_ms or captured_at_ms
    bucket = anchor_ms // max(1, window_ms)
    raw = f"{remark_name}\x1f{message.text}\x1f{bucket}".encode()
    return hashlib.sha256(raw).hexdigest()


def map_wechat_gui_snapshot_to_inbound(
    snapshot: WechatGuiSnapshotModel,
    *,
    contact: WechatGuiContactModel,
    known_message_ids: frozenset[str],
    max_messages: int,
    now_ms: int,
) -> list[InboundMessageModel]:
    """把会话快照归一为入站消息列表（自身回声 / 空文本 / 去重过滤）。

    过滤顺序：自身回声 → 空文本 → 已处理键 / 批内重复键；随后按时间序取
    末尾 ``max_messages`` 条。发送者来源按联系人配置的 ``is_owner`` 落档
    （私聊默认 peer——第三方联系人消息不由主人发出）。

    Args:
        snapshot: 采集快照（标题已通过目标校验）。
        contact: 联系人配置（会话目标与来源标注）。
        known_message_ids: 已处理去重键集合。
        max_messages: 单次归一时间序末尾上限（1-100）。
        now_ms: 采集时刻（消息时间缺失兜底）。

    Returns:
        通道中立入站消息列表（时间序）。
    """
    chat_id = build_wechat_gui_chat_id(contact.remark_name)
    selected: list[InboundMessageModel] = []
    seen: set[str] = set()
    for message in snapshot.messages:
        text = message.text.strip()
        if message.is_self or not text:
            continue
        key = wechat_gui_message_dedup_key(
            contact.remark_name, message, captured_at_ms=now_ms
        )
        if key in seen or key in known_message_ids:
            continue
        seen.add(key)
        selected.append(
            InboundMessageModel(
                channel=ChannelType.WECHAT_GUI,
                chat_id=chat_id,
                message_id=key,
                sender_id=message.sender.strip() or "unknown",
                text=text,
                ts_ms=message.ts_ms or now_ms,
                is_group_chat=False,
                is_owner=contact.is_owner,
                synthetic=False,
                has_user_text=True,
            )
        )
    bound = max(1, max_messages)
    return selected[-bound:]

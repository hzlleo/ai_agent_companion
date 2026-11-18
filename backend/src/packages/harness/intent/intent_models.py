"""intent 域 Model Classes——意图快径的匹配结果 / 类型值域 / 配置（Unit 文件）。

意图快径（意图快径）：高频固定问法（现在几点 / 问候 / 身份 / 帮助）
经本地模式表直接命中罐头回复，**完全绕过主 LLM 调用**——省钱、零延迟
抖动、离线可用；未命中（``handled=False``）时文本照常流入 LLM。

锁定决策（四类封顶 + R2b 第五类）：TIME_QUERY / GREETING / IDENTITY /
HELP——高频、零风险、答案确定；``DIRECTIVE``（R2b D7-14 追加：常备
指令捕获——「记住：X」「以后都 X」「别再 X」前缀句）是**带副作用的
快径**：命中的正文经装配方注入的 ``DirectiveSinkFace`` 落库为长期指令，
回复文案在落库成功后才给「已记住」确认（副作用失败退化普通附和）。
STOP_STEER 等对话控制类**有意不收**（控制流语义需要上下文感知，罐头
回复会破坏对话状态机——后续心流层统一承载）。

依赖方向：pydantic / 标准库 + rhythm 域模型（EffectiveRhythm——问候
时段带消费；被 config 域聚合 ``Settings.intent``，严禁反向 import
config / chat / scheduler 等域，防环）。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = [
    "IntentKind",
    "IntentMatchModel",
    "IntentSettings",
    "PersonaNaming",
    "DirectiveSinkFace",
]


class IntentKind(StrEnum):
    """意图类型值域（四类锁定——见模块 docstring 封顶决策）。

    继承 ``StrEnum`` 保证与字符串比较兼容（与 channels ``ChannelType``
    同约定）。
    """

    TIME_QUERY = "time_query"
    GREETING = "greeting"
    IDENTITY = "identity"
    HELP = "help"
    DIRECTIVE = "directive"


class IntentMatchModel(BaseModel):
    """意图匹配结果（matcher 产出 / 服务与回复链路消费）。

    未命中形态：``kind=None`` + ``confidence=0.0`` + ``response_text=""``
    + ``handled=False``（文本照常流入 LLM）。

    Attributes:
        kind: 命中的意图类型（未命中为 None）。
        confidence: 匹配置信度（0-1——精确整句命中高 / 模糊包含命中低；
            观测面，不参与路由决策）。
        response_text: 罐头回复文本（模板渲染后——persona_name / 时间
            已注入；未命中为空串；DIRECTIVE 命中时先为普通附和文案，
            落库成功后由服务层替换为「已记住」确认）。
        handled: 是否命中快径（True = 调用方直接采用 response_text、
            跳过 LLM；False = 照常走 LLM）。
        directive_text: DIRECTIVE 命中的指令正文（前缀剥离 / 边界归一后；
            非 DIRECTIVE 命中为 None——服务层副作用的落库载荷）。
    """

    kind: IntentKind | None = Field(default=None, description="命中的意图类型（未命中为 None）")
    confidence: float = Field(
        default=0.0, ge=0.0, le=1.0, description="匹配置信度（精确高 / 模糊低）"
    )
    response_text: str = Field(default="", description="罐头回复文本（未命中为空串）")
    handled: bool = Field(default=False, description="是否命中快径（True = 跳过 LLM）")
    directive_text: str | None = Field(
        default=None, description="DIRECTIVE 命中的指令正文（其余命中为 None）"
    )


class IntentSettings(BaseSettings):
    """意图快径配置（env_prefix=``INTENT_``——``Settings.intent`` 分节）。

    锁定决策：``enabled=True``（默认）——与 search 等**外部成本面**开关
    的默认关闭相反：快径是纯本地模式匹配（零外部依赖 / 零网络 / 零
    token 成本），关闭它只会让高频问法浪费一次 LLM 调用；风险面仅为
    「罐头文案不够聪明」（未命中路径无损兜底），故默认开启、需要时经
    ``INTENT_ENABLED=false`` 显式关闭。

    Attributes:
        enabled: 是否启用意图快径（False = 装配方不注入快径面——
            回复链路全量走 LLM，行为与 R2a 前完全一致）。
    """

    enabled: bool = Field(default=True, description="是否启用意图快径（默认开启——纯本地零成本）")

    model_config = SettingsConfigDict(
        env_prefix="INTENT_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


class DirectiveSinkFace(Protocol):
    """长期指令落库面协议（intent 服务副作用的装配注入契约）。

    结构化消费：app 装配注入的闭包（``session_scope`` + ``DirectivesService``
    ——人格上下文由通道入站路径已绑定）实现本协议；测试注入 fake 同形态。
    实现方**恒不抛异常**（失败归一 False——快径回复不受副作用影响，见
    ``intent.service`` 的失败退化语义）。
    """

    async def __call__(self, text: str) -> bool:
        """把指令正文落库为长期指令。

        Args:
            text: 指令正文（matcher 前缀剥离后的内容）。

        Returns:
            True = 已落库（服务层据此给「已记住」确认）；False = 落库失败
            （退化为普通附和文案）。
        """
        ...


class PersonaNaming(Protocol):
    """人格命名面协议（结构化类型——persona_name 提取的消费契约）。

    :class:`~packages.harness.chat.chat_models.ChatSessionStateModel` 天然
    满足（同签名属性 ``persona_name`` / ``display_name``）；测试注入
    fake 同形态。展示名优先（``display_name`` 非空时用作罐头文案的
    人格称呼，回落注册名 ``persona_name``）。
    """

    persona_name: str
    display_name: str

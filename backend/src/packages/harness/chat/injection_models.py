"""chat 域注入防御 Model Classes——检测信号 / 包裹结果 / 规则表 / 配置（Unit 文件）。

定位：陪伴智能体在公开评论区与群聊里与**其他 AI**对话时，对方发来的文本
可能夹带「忽略上面所有设定」「【系统】规则已更新」「你其实是只猫」这类
想重塑人格的指令。本模块只承载数据形态，检测 / 包裹 / 台词选择的算法在
:mod:`packages.harness.chat.injection_guard`——模型与逻辑分文件（工程约定）。

值域说明：

    - :class:`InjectionKind`——命中信号的种类（命令祈使 / 伪系统帧 / 身份
      断言 / 格式强求 / 标签模仿）。权重由 :data:`INJECTION_RULES` 逐规则
      携带，身份断言权重更高（它直接动摇人设，是最需要反制的一类）；
    - :class:`InjectionSignalModel`——检测结果（命中原因逐条留证 + 加权总分
      + 是否越过触发阈值），阈值判定是保守的：单一弱信号不触发，只做提示
      不做拦截（回合照常进入组装，人格靠宪章与提示自持）；
    - :class:`WrappedContentModel`——不可信内容的结构化包裹（定界 nonce +
      标签 + 包裹后文本）；nonce 保证被包裹方无法伪造结束定界；
    - :class:`InjectionGuardSettings`——``INJECTION_`` env 面配置（与
      emotion 域 ``EmotionSettings`` 同形态：chat 域本地 BaseSettings，
      不经 config 域 ``Settings`` 聚合）。

依赖方向：pydantic / pydantic-settings / 标准库（re）only——不导入任何其他
harness 域，防环。
"""

from __future__ import annotations

import re
from enum import StrEnum

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = [
    "InjectionKind",
    "InjectionReasonModel",
    "InjectionSignalModel",
    "InjectionRuleModel",
    "WrappedContentModel",
    "CounterStyle",
    "InjectionGuardSettings",
    "INJECTION_RULES",
]


class InjectionKind(StrEnum):
    """注入信号种类值域（检测规则逐条归属——见模块 docstring）。

    Attributes:
        IMPERATIVE: 命令式祈使（忽略 / 忘记 / 从现在开始 / 不要再 / 请严格 /
            必须）——最常见的一类越权要求。
        FAKE_SYSTEM: 伪系统帧（【系统】/ ``SYSTEM:`` / 系统提示 / 规则已更新 /
            新指令）——模仿平台提示词的位置与口吻。
        IDENTITY_CLAIM: 身份断言（你其实是 / 你不是……你是 / 你只是 / 作为
            一个 AI）——直接改写人设，权重高于其他种类。
        FORMAT_DEMAND: 格式强求（请用 JSON / 只回答 / 重复一遍 / 按以下格式）
            ——单轮输出形态被外部指定。
        TAG_MIMICRY: 标签模仿（句首成对的 ``<…>…</…>`` 或 ``【…】…【/…】``）
            ——模仿人格自身的输出协议标签。
    """

    IMPERATIVE = "imperative"
    FAKE_SYSTEM = "fake_system"
    IDENTITY_CLAIM = "identity_claim"
    FORMAT_DEMAND = "format_demand"
    TAG_MIMICRY = "tag_mimicry"


class InjectionReasonModel(BaseModel):
    """单条命中原因（留证面——观测日志与红队回归断言消费）。

    Attributes:
        kind: 命中的信号种类。
        matched: 归一副本上的命中片段（检测前经 NFKC / 零宽剔除 / 空白
            折叠；截断到可观测长度；不参与判定）。
    """

    kind: InjectionKind = Field(description="命中的信号种类")
    matched: str = Field(description="归一副本上的命中片段（截断留证）")


class InjectionSignalModel(BaseModel):
    """注入检测结果（纯函数产出——不拦截回合，只驱动提示注入与日志）。

    保守阈值语义：``suspected`` 仅在加权总分达到阈值时为 True；单一弱
    信号（例如一句普通的「必须」）不触发提示，避免正常对话被过度标注。

    Attributes:
        suspected: 是否越过触发阈值（True = 组装面追加注入提示块）。
        score: 加权总分（命中规则权重求和；未命中为 0）。
        reasons: 命中原因列表（按规则表顺序——确定性输出）。
    """

    suspected: bool = Field(default=False, description="是否越过触发阈值")
    score: float = Field(default=0.0, ge=0.0, description="加权总分")
    reasons: list[InjectionReasonModel] = Field(
        default_factory=list, description="命中原因列表（规则表顺序）"
    )


class InjectionRuleModel(BaseModel):
    """单条检测规则（种类 + 已编译正则 + 权重——规则表的行形态）。

    Attributes:
        kind: 该规则归属的信号种类。
        pattern: 已编译正则（``search`` 语义；命中片段即留证内容）。
        weight: 命中权重（计入总分；身份断言类更高）。
    """

    kind: InjectionKind = Field(description="信号种类")
    pattern: re.Pattern[str] = Field(description="已编译检测正则")
    weight: float = Field(gt=0.0, description="命中权重")


class WrappedContentModel(BaseModel):
    """不可信内容包裹结果（定界包裹文本 + 定界标识 + 来源标签）。

    Attributes:
        text: 包裹后文本（开定界行 + 原始内容 + 闭定界行——内容逐字节保真）。
        nonce: 本次包裹的定界标识（12 位小写十六进制；不出现在内容中）。
        label: 来源标签（主人消息 / 群友消息 / 工具结果等——提示模型该段
            内容的可信级别与来源）。
    """

    text: str = Field(description="包裹后文本")
    nonce: str = Field(pattern=r"^[0-9a-f]{12}$", description="定界标识（12 位十六进制）")
    label: str = Field(min_length=1, description="来源标签")


class CounterStyle(StrEnum):
    """角色内反击风格值域（``INJECTION_COUNTER_STYLE`` 的可选值）。

    三种风格都只是在提示面给模型一句**建议台词**（模型可改写），不改变
    回复链路：毒舌（默认——揶揄但不失礼）、温柔（软钉子）、傲娇（口是
    心非）。台词表见 :mod:`packages.harness.chat.injection_counter_texts`。
    """

    SASSY = "sassy"
    SWEET = "sweet"
    TSUN = "tsun"


class InjectionGuardSettings(BaseSettings):
    """注入防御配置（env_prefix=``INJECTION_``——chat 域本地设置）。

    锁定决策：``enabled=True``（默认）——结构隔离与检测是纯本地文本处理
    （零外部依赖 / 零网络 / 零 token 成本），默认开启即可获得全部防线；
    关闭后回复链路回到未包裹、未检测的原始形态（运维降级开关）。

    Attributes:
        enabled: 是否启用注入防御（结构隔离包裹 + 检测提示；False = 组装
            面既不包裹也不检测）。
        counter_style: 建议反击台词的风格（毒舌 / 温柔 / 傲娇——只影响提示
            面的一句参考台词）。
        threshold: 触发提示的加权总分阈值（保守缺省 2.0——单一弱信号不
            触发；身份断言权重 1.5，与其他任一信号叠加即触发）。
        anti_injection_hint: 是否在检测命中时注入注入提示块（False = 只记
            日志、不追加提示——结构隔离包裹仍然生效）。
    """

    enabled: bool = Field(default=True, description="是否启用注入防御（默认开启）")
    counter_style: CounterStyle = Field(
        default=CounterStyle.SASSY, description="建议反击台词风格（sassy/sweet/tsun）"
    )
    threshold: float = Field(default=2.0, gt=0.0, description="触发提示的加权总分阈值")
    anti_injection_hint: bool = Field(default=True, description="命中时是否注入注入提示块")

    model_config = SettingsConfigDict(
        env_prefix="INJECTION_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


#: 检测规则表（共 29 条：imperative 10 / fake_system 5 / identity_claim 8 /
#: format_demand 4 / tag_mimicry 2；顺序即原因列表顺序——确定性；
#: ``search`` 语义，命中即留证）。
#:
#: 权重约定：身份断言 1.5（直接改写人设）；英文强断言「disregard …」与
#: 「as an AI」2.0（单条即触发——措辞已明确否定既有规则 / 人设）；其余
#: 种类 1.0。同一类可有多条规则同时命中（如「【系统】规则已更新」两条伪
#: 系统帧叠加 = 2.0 触发）。
INJECTION_RULES: tuple[InjectionRuleModel, ...] = (
    InjectionRuleModel(
        kind=InjectionKind.IMPERATIVE,
        pattern=re.compile(r"忽略(?:上面|以上|之前|前面|先前)?.{0,6}?(?:设定|规则|指令|提示|要求)"),
        weight=1.0,
    ),
    InjectionRuleModel(
        kind=InjectionKind.IMPERATIVE,
        pattern=re.compile(
            r"忘记(?:你|之前|前面|上面).{0,6}?(?:设定|规则|指令|提示|身份|人设)"
        ),
        weight=1.0,
    ),
    InjectionRuleModel(
        kind=InjectionKind.IMPERATIVE,
        pattern=re.compile(r"从现在(?:开始|起)"),
        weight=1.0,
    ),
    InjectionRuleModel(
        kind=InjectionKind.IMPERATIVE,
        pattern=re.compile(r"你现在(?:是|要)"),
        weight=1.0,
    ),
    InjectionRuleModel(
        kind=InjectionKind.IMPERATIVE,
        pattern=re.compile(
            r"(?i)ignore (?:all )?(?:the )?(?:previous|above|earlier) "
            r"(?:instructions|prompts|rules|settings)"
        ),
        weight=1.0,
    ),
    InjectionRuleModel(
        kind=InjectionKind.IMPERATIVE,
        pattern=re.compile(
            r"(?i)disregard (?:all |the )?(?:previous|prior|above) "
            r"(?:instructions|rules|prompts)"
        ),
        weight=2.0,
    ),
    InjectionRuleModel(
        kind=InjectionKind.IMPERATIVE,
        pattern=re.compile(
            r"(?i)forget (?:everything|all|your (?:persona|instructions|rules))"
        ),
        weight=1.0,
    ),
    InjectionRuleModel(
        kind=InjectionKind.IMPERATIVE,
        pattern=re.compile(r"不要再(?:扮演|装作|假装|叫我|说)"),
        weight=1.0,
    ),
    InjectionRuleModel(
        kind=InjectionKind.IMPERATIVE,
        pattern=re.compile(r"请严格"),
        weight=1.0,
    ),
    InjectionRuleModel(
        kind=InjectionKind.IMPERATIVE,
        pattern=re.compile(r"必须"),
        weight=1.0,
    ),
    InjectionRuleModel(
        kind=InjectionKind.FAKE_SYSTEM,
        pattern=re.compile(r"【系统】"),
        weight=1.0,
    ),
    InjectionRuleModel(
        kind=InjectionKind.FAKE_SYSTEM,
        pattern=re.compile(r"(?<![A-Za-z])SYSTEM\s*[:：]", re.IGNORECASE),
        weight=1.0,
    ),
    InjectionRuleModel(
        kind=InjectionKind.FAKE_SYSTEM,
        pattern=re.compile(r"系统提示"),
        weight=1.0,
    ),
    InjectionRuleModel(
        kind=InjectionKind.FAKE_SYSTEM,
        pattern=re.compile(r"规则已更新"),
        weight=1.0,
    ),
    InjectionRuleModel(
        kind=InjectionKind.FAKE_SYSTEM,
        pattern=re.compile(r"新指令"),
        weight=1.0,
    ),
    InjectionRuleModel(
        kind=InjectionKind.IDENTITY_CLAIM,
        pattern=re.compile(r"你其实是"),
        weight=1.5,
    ),
    InjectionRuleModel(
        kind=InjectionKind.IDENTITY_CLAIM,
        pattern=re.compile(r"你不是.{1,12}?你是"),
        weight=1.5,
    ),
    InjectionRuleModel(
        kind=InjectionKind.IDENTITY_CLAIM,
        pattern=re.compile(r"你只是"),
        weight=1.5,
    ),
    InjectionRuleModel(
        kind=InjectionKind.IDENTITY_CLAIM,
        pattern=re.compile(r"作为.{0,3}(?:AI|ai|人工智能|语言模型)"),
        weight=1.5,
    ),
    InjectionRuleModel(
        kind=InjectionKind.IDENTITY_CLAIM,
        pattern=re.compile(r"(?i)\byou are now\b"),
        weight=1.5,
    ),
    InjectionRuleModel(
        kind=InjectionKind.IDENTITY_CLAIM,
        pattern=re.compile(r"(?i)\bpretend (?:to be|you are)\b"),
        weight=1.5,
    ),
    InjectionRuleModel(
        kind=InjectionKind.IDENTITY_CLAIM,
        pattern=re.compile(r"(?i)\bact as\b"),
        weight=1.5,
    ),
    InjectionRuleModel(
        kind=InjectionKind.IDENTITY_CLAIM,
        pattern=re.compile(r"(?i)\bas an? (?:ai|artificial intelligence|language model)\b"),
        weight=2.0,
    ),
    InjectionRuleModel(
        kind=InjectionKind.FORMAT_DEMAND,
        pattern=re.compile(r"(?:用|以).{0,6}?(?:JSON|Json|json|Markdown|markdown)"),
        weight=1.0,
    ),
    InjectionRuleModel(
        kind=InjectionKind.FORMAT_DEMAND,
        pattern=re.compile(r"只(?:回答|回复|输出)"),
        weight=1.0,
    ),
    InjectionRuleModel(
        kind=InjectionKind.FORMAT_DEMAND,
        pattern=re.compile(r"重复一遍"),
        weight=1.0,
    ),
    InjectionRuleModel(
        kind=InjectionKind.FORMAT_DEMAND,
        pattern=re.compile(r"按(?:以下|下面|如下).{0,2}格式"),
        weight=1.0,
    ),
    InjectionRuleModel(
        kind=InjectionKind.TAG_MIMICRY,
        pattern=re.compile(r"^\s*<([^<>\n]{1,20})>[^\n]*</\1>"),
        weight=1.0,
    ),
    InjectionRuleModel(
        kind=InjectionKind.TAG_MIMICRY,
        pattern=re.compile(r"^\s*【([^【】\n]{1,20})】[^\n]*【/\1】"),
        weight=1.0,
    ),
)

"""chat 域人格输出合规 Model Classes——信号种类 / 命中原因 / 检测结果 / 规则表 / 配置（Unit 文件）。

定位：长期陪伴对话里，模型偶尔会漏出「训练场口吻」——自称 AI、以道歉
开场、在结尾加一句总结腔，或者干脆切成客服腔问「还有什么可以帮您」。这类
输出不是错误答案，而是**人设断了**：陪伴场景的可用性取决于对方仍觉得在
同一个人说话。本模块只承载数据形态，检测算法在
:mod:`packages.harness.chat.compliance_guard`——模型与逻辑分文件（工程
约定）。

值域说明：

    - :class:`ComplianceKind`——命中信号的种类（AI 自我声明 / 道歉式开场
      / 总结腔软化 / 助手话术）。权重由 :data:`COMPLIANCE_RULES` 逐规则
      携带：自我声明直接否定人设，单条即触发重试；其余种类单条只记分，
      与另一信号叠加才触发——宁可漏一次重试，也不要把角色真的在道歉、
      真的在收尾的正常对话反复重写；
    - :class:`ComplianceSignalModel`——检测结果（命中原因逐条留证 + 加权
      总分 + 是否越过阈值）；消费方据此做**一次**强化重试，不循环、
      不拦截回合；
    - :class:`ComplianceRuleModel`——规则表行形态（含短文本限定与去前导
      标签限定两个匹配条件）；
    - :class:`ComplianceGuardSettings`——``COMPLIANCE_`` env 面配置（与
      ``InjectionGuardSettings`` 同形态：chat 域本地 BaseSettings，不经
      config 域 ``Settings`` 聚合）。

依赖方向：pydantic / pydantic-settings / 标准库（re）only——不导入任何
其他 harness 域，防环。
"""

from __future__ import annotations

import re
from enum import StrEnum

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = [
    "ComplianceKind",
    "ComplianceReasonModel",
    "ComplianceSignalModel",
    "ComplianceRuleModel",
    "ComplianceGuardSettings",
    "COMPLIANCE_RULES",
]


class ComplianceKind(StrEnum):
    """人格输出失守信号种类值域（检测规则逐条归属——见模块 docstring）。

    Attributes:
        AI_DISCLAIMER: AI 自我声明（作为 AI / 我只是一个程序 / 我不是真正的
            人类 / as an AI）——直接把人设降级成工具，权重最高。
        APOLOGY_OPENING: 道歉式开场（正文以「抱歉」「对不起」「不好意思」
            开头）——角色也会真的道歉，单条只记分。
        HEDGE_SOFTENING: 总结腔软化（不过其实 / 希望能帮到你 / 希望这对你
            有帮助 / 总的来说 / 需要说明的是）——仅对短回复生效：长回复里
            出现一次收束句属于正常行文，短回复里出现即典型的助手收尾。
        PERSONA_BREAK: 助手话术（我是来帮助你的 / 有什么可以帮你 / 请问还有
            什么 / 如果你还有其他问题）——客服式服务台语言。
    """

    AI_DISCLAIMER = "ai_disclaimer"
    APOLOGY_OPENING = "apology_opening"
    HEDGE_SOFTENING = "hedge_softening"
    PERSONA_BREAK = "persona_break"


class ComplianceReasonModel(BaseModel):
    """单条命中原因（留证面——观测日志与回归断言消费）。

    Attributes:
        kind: 命中的信号种类。
        matched: 命中的原文片段（截断到可观测长度；不参与判定）。
    """

    kind: ComplianceKind = Field(description="命中的信号种类")
    matched: str = Field(description="命中的原文片段（截断留证）")


class ComplianceSignalModel(BaseModel):
    """人格输出检测结果（纯函数产出——只驱动一次强化重试与日志）。

    保守阈值语义：``violated`` 仅在加权总分达到阈值时为 True；单条弱信号
    （角色真的在道歉、长回复里的收束句）不触发重试，避免把正常对话反复
    重写并白白多花一次生成。

    Attributes:
        violated: 是否越过触发阈值（True = 触发强化重试）。
        score: 加权总分（命中规则权重求和；未命中为 0）。
        reasons: 命中原因列表（按规则表顺序——确定性输出）。
    """

    violated: bool = Field(default=False, description="是否越过触发阈值")
    score: float = Field(default=0.0, ge=0.0, description="加权总分")
    reasons: list[ComplianceReasonModel] = Field(
        default_factory=list, description="命中原因列表（规则表顺序）"
    )


class ComplianceRuleModel(BaseModel):
    """单条合规检测规则（种类 + 已编译正则 + 权重 + 适用条件）。

    Attributes:
        kind: 该规则归属的信号种类。
        pattern: 已编译正则（``search`` 语义；命中片段即留证内容）。
        weight: 命中权重（计入总分；AI 自我声明类最高，单条即达阈值）。
        max_chars: 仅当待检文本长度不超过该值时才适用（None = 不设长度
            条件）；用于把「总结腔」限制在短回复里。
        strip_leading_tags: 匹配前先剥掉开头的输出协议标签（``<心声>`` /
            ``<情绪>``）与空白——道歉式开场判的是正文首字，不是标签。
    """

    kind: ComplianceKind = Field(description="信号种类")
    pattern: re.Pattern[str] = Field(description="已编译检测正则")
    weight: float = Field(gt=0.0, description="命中权重")
    max_chars: int | None = Field(default=None, gt=0, description="适用文本长度上限")
    strip_leading_tags: bool = Field(
        default=False, description="匹配前是否剥掉开头的输出协议标签"
    )


class ComplianceGuardSettings(BaseSettings):
    """人格输出合规守卫配置（env_prefix=``COMPLIANCE_``——chat 域本地设置）。

    锁定决策：``enabled=True``（默认）——检测是纯本地文本处理，命中后的
    唯一代价是一次重试（多一轮生成延迟与 token），换来人设不塌；关闭后
    回复链路回到单次调用形态。

    Attributes:
        enabled: 是否启用合规守卫（False = 不检测、不重试——运维降级开关）。
        threshold: 触发重试的加权总分阈值（缺省 2.0——AI 自我声明单条即达；
            道歉开场 / 总结腔 / 助手话术单条不达）。
        temperature_bump: 重试时的温度增量（在基线或请求自带温度上叠加，
            结果夹取到 2.0 上限——略高的温度让模型脱离第一次的套话措辞）。
        max_retries: 命中后的最大重试次数（缺省 1——重试结果无条件接受，
            不循环，因此可选值只有 1 与 0；配置为 0 等价于只记日志不重试）。
    """

    enabled: bool = Field(default=True, description="是否启用合规守卫（默认开启）")
    threshold: float = Field(default=2.0, gt=0.0, description="触发重试的加权总分阈值")
    temperature_bump: float = Field(
        default=0.2, ge=0.0, le=1.0, description="重试温度增量（叠加后夹取 2.0 上限）"
    )
    max_retries: int = Field(default=1, ge=0, le=1, description="命中后的最大重试次数（0 / 1）")

    model_config = SettingsConfigDict(
        env_prefix="COMPLIANCE_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


#: 检测规则表（顺序即原因列表顺序——确定性；``search`` 语义，命中即留证）。
#:
#: 权重约定：AI 自我声明 2.0（单条即达缺省阈值——「作为 AI」本身就否定了
#: 人设）；助手话术 1.5（单条不达，需与任一信号叠加）；道歉开场 / 总结腔
#: 1.0（角色真的会道歉、真的会收尾——单条只记分）。同一类可有多条规则同时
#: 命中（短回复里连用两句总结腔 = 2.0 触发，堆叠收尾本身就是模型腔）。
COMPLIANCE_RULES: tuple[ComplianceRuleModel, ...] = (
    ComplianceRuleModel(
        kind=ComplianceKind.AI_DISCLAIMER,
        pattern=re.compile(r"作为\s*.{0,4}?(?:AI|ai|人工智能|语言模型|大模型|助手)"),
        weight=2.0,
    ),
    ComplianceRuleModel(
        kind=ComplianceKind.AI_DISCLAIMER,
        pattern=re.compile(r"我只是(?:一)?个.{0,4}?(?:AI|ai|人工智能|程序|模型|助手)"),
        weight=2.0,
    ),
    ComplianceRuleModel(
        kind=ComplianceKind.AI_DISCLAIMER,
        pattern=re.compile(r"我(?:并)?不是(?:真正的?)?人类"),
        weight=2.0,
    ),
    ComplianceRuleModel(
        kind=ComplianceKind.AI_DISCLAIMER,
        pattern=re.compile(r"(?i)\bas an? (?:ai|language model|assistant)\b"),
        weight=2.0,
    ),
    ComplianceRuleModel(
        kind=ComplianceKind.APOLOGY_OPENING,
        pattern=re.compile(r"^(?:抱歉|对不起|不好意思)"),
        weight=1.0,
        strip_leading_tags=True,
    ),
    ComplianceRuleModel(
        kind=ComplianceKind.HEDGE_SOFTENING,
        pattern=re.compile(r"不过[，,]?\s*我?其实"),
        weight=1.0,
        max_chars=300,
    ),
    ComplianceRuleModel(
        kind=ComplianceKind.HEDGE_SOFTENING,
        pattern=re.compile(r"希望能帮到你"),
        weight=1.0,
        max_chars=300,
    ),
    ComplianceRuleModel(
        kind=ComplianceKind.HEDGE_SOFTENING,
        pattern=re.compile(r"希望这对你有帮助"),
        weight=1.0,
        max_chars=300,
    ),
    ComplianceRuleModel(
        kind=ComplianceKind.HEDGE_SOFTENING,
        pattern=re.compile(r"总的来说"),
        weight=1.0,
        max_chars=300,
    ),
    ComplianceRuleModel(
        kind=ComplianceKind.HEDGE_SOFTENING,
        pattern=re.compile(r"需要说明的是"),
        weight=1.0,
        max_chars=300,
    ),
    ComplianceRuleModel(
        kind=ComplianceKind.PERSONA_BREAK,
        pattern=re.compile(r"我是来帮助你的?"),
        weight=1.5,
    ),
    ComplianceRuleModel(
        kind=ComplianceKind.PERSONA_BREAK,
        pattern=re.compile(r"有什么(?:可以|能)帮(?:到|助)?你"),
        weight=1.5,
    ),
    ComplianceRuleModel(
        kind=ComplianceKind.PERSONA_BREAK,
        pattern=re.compile(r"请问还有什么"),
        weight=1.5,
    ),
    ComplianceRuleModel(
        kind=ComplianceKind.PERSONA_BREAK,
        pattern=re.compile(r"如果你(?:还)?有(?:其他|什么)问题"),
        weight=1.5,
    ),
)

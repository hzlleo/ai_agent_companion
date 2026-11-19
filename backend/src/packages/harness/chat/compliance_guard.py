"""chat 域人格输出合规守卫——助手腔检测（纯函数 Unit）。

单一职责：:func:`detect_assistant_violation` 按规则表
（:data:`~packages.harness.chat.compliance_models.COMPLIANCE_RULES`）逐条
``search`` 并加权求和，越过阈值即产出命中原因——消费方（``ChatService.reply``）
据此做**一次**强化重试：把同一段对话加上角色提醒块、略提温度再问一遍。
本模块零 I/O / 零网络 / 不依赖会话态，阈值与规则表之外的判定一律不做。

阈值语义（锁定，保守）：缺省 2.0；AI 自我声明权重 2.0——单条即触发（说了
「作为 AI」人设已经断了）；助手话术 1.5、道歉开场 / 总结腔 1.0——单条不
触发，需与另一信号叠加。误伤代价是一轮多余生成，因此宁可漏一次重试，也
不把角色真的在道歉、真的在收尾的正常回复反复重写。

匹配前归一（锁定）：检测在**归一副本**上进行（与注入守卫共用
:func:`~packages.harness.chat.text_normalization.normalize_for_matching`
——NFKC 折叠全角 / 兼容字符、剔除全部格式类不可见字符、空白折叠为单空格）；
命中留证片段也取自该归一副本。原文不参与匹配，因此全角「ＡＩ」与不可见
格式类插入都不会绕过规则表，留证与判定文本同源。

匹配细节（锁定）：每条规则对全文最多计一次权重（重复措辞不堆分）；总结腔
类规则只在短回复（``max_chars`` 上限内，长度按归一副本计）生效；道歉开场
先剥掉开头的输出协议标签再判首字（模型常把 ``<心声>`` 写在最前，正文本体
才是被听的那句）。检测只提示重试、不删改文本：重试结果无条件接受，重试后
仍失守也照常进入解析与落回合——守卫不做二次判定、不循环。

确定性：纯字符串匹配，不消耗随机源；同一文本多次检测结果一致。
"""

from __future__ import annotations

import re

from packages.harness.chat.compliance_models import (
    COMPLIANCE_RULES,
    ComplianceReasonModel,
    ComplianceSignalModel,
)
from packages.harness.chat.text_normalization import normalize_for_matching
from packages.harness.llm.llm_models import ChatRequestParams

__all__ = [
    "DEFAULT_COMPLIANCE_THRESHOLD",
    "COMPLIANCE_RETRY_BASELINE_TEMPERATURE",
    "COMPLIANCE_TEMPERATURE_CEILING",
    "detect_assistant_violation",
    "build_retry_params",
]

#: 检测触发阈值缺省值（保守——见模块 docstring）。
DEFAULT_COMPLIANCE_THRESHOLD: float = 2.0

#: 重试温度的基线值。
#:
#: 重试要施加 ``temperature_bump`` 就必须显式给出温度，而正常回复请求通常
#: 不携带采样参数（交给网关端点默认）。取 0.8 作为陪伴对话的创作基线：保留
#: 口语变化但不至于跑题；缺省增量 0.2 叠加后为 1.0，足以让模型脱离第一次
#: 的套话措辞。请求自带温度时以自带值为基线，不再使用本常量。
COMPLIANCE_RETRY_BASELINE_TEMPERATURE: float = 0.8

#: 重试温度的上限（网关协议允许的最高采样温度——提升到这里即止，绝不
#: 超出 wire 值域）。
COMPLIANCE_TEMPERATURE_CEILING: float = 2.0

#: 开头输出协议标签的剥离模式（``<心声>…</心声>`` / ``<情绪>…</情绪>``，
#: 允许连续出现与标签间空白；仅用于判定正文首字，不改写原文）。
_LEADING_PROTOCOL_TAG_RE: re.Pattern[str] = re.compile(
    r"^\s*(?:<心声>.*?</心声>|<情绪>.*?</情绪>)\s*", re.DOTALL
)

#: 单次剥离的最大轮数（协议标签至多两个，各出现一次）。
_LEADING_TAG_STRIP_LIMIT: int = 2


def _strip_leading_protocol_tags(text: str) -> str:
    """剥掉开头的输出协议标签，返回被听到的正文起点。

    Args:
        text: 归一副本（可能以 ``<心声>`` / ``<情绪>`` 块开头）。

    Returns:
        去掉开头协议标签与紧邻空白后的文本（无标签时原样返回）。
    """
    current = text
    for _ in range(_LEADING_TAG_STRIP_LIMIT):
        stripped = _LEADING_PROTOCOL_TAG_RE.sub("", current, count=1)
        if stripped == current:
            break
        current = stripped
    return current


def detect_assistant_violation(
    text: str, *, threshold: float = DEFAULT_COMPLIANCE_THRESHOLD
) -> ComplianceSignalModel:
    """检测文本里的人格输出失守信号（规则表逐条匹配 + 加权求和，纯函数）。

    Args:
        text: 待检测文本（模型非流式回复原文——含可能的协议标签）。
        threshold: 触发阈值（缺省 :data:`DEFAULT_COMPLIANCE_THRESHOLD`；
            配置面传入 ``COMPLIANCE_THRESHOLD`` 的值）。

    Returns:
        检测结果（是否触发 + 加权总分 + 命中原因列表）；空文本返回未命中
        形态。
    """
    if not text or not text.strip():
        return ComplianceSignalModel()
    normalized = normalize_for_matching(text)
    if not normalized:
        return ComplianceSignalModel()
    reasons: list[ComplianceReasonModel] = []
    score = 0.0
    for rule in COMPLIANCE_RULES:
        if rule.max_chars is not None and len(normalized) > rule.max_chars:
            continue
        target = (
            _strip_leading_protocol_tags(normalized)
            if rule.strip_leading_tags
            else normalized
        )
        match = rule.pattern.search(target)
        if match is None:
            continue
        reasons.append(
            ComplianceReasonModel(kind=rule.kind, matched=match.group(0).strip()[:40])
        )
        score += rule.weight
    return ComplianceSignalModel(
        violated=score >= threshold,
        score=round(score, 4),
        reasons=reasons,
    )


def build_retry_params(
    params: ChatRequestParams | None, *, temperature_bump: float
) -> ChatRequestParams:
    """构建强化重试用的采样参数（温度提升 + 2.0 上限夹取）。

    基线语义：调用方给出参数时以其 ``temperature`` 为基线（None 温度的
    参数按 :data:`COMPLIANCE_RETRY_BASELINE_TEMPERATURE` 取基线），其余
    字段（``max_tokens`` / ``top_p`` / ``extra``）原样保留——重试只改
    措辞倾向，不改输出长度与采样面。未给参数时按基线温度新建一份。

    Args:
        params: 原请求的采样参数（None = 原请求未携带参数）。
        temperature_bump: 温度增量（非负——负增量会把重试变得比首次更
            死板，配置面已限制）。

    Returns:
        重试用采样参数（温度为基线加增量后夹取
        :data:`COMPLIANCE_TEMPERATURE_CEILING` 的结果；其余字段与入参
        一致）。
    """
    base = params if params is not None else ChatRequestParams()
    baseline = (
        base.temperature
        if base.temperature is not None
        else COMPLIANCE_RETRY_BASELINE_TEMPERATURE
    )
    bumped = min(baseline + temperature_bump, COMPLIANCE_TEMPERATURE_CEILING)
    return base.model_copy(update={"temperature": round(bumped, 4)})

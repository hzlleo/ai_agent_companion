"""chat 域注入防御——不可信内容包裹与注入信号检测（纯函数 Unit）。

两道防线（本模块只做纯文本处理，零 I/O / 零网络）：

    1. **结构隔离**：:func:`wrap_untrusted_content` 把不可信文本（当轮入站
       消息 / 工具结果）包进 nonce 绑定的定界行——被包裹方无法伪造闭合
       定界（nonce 是 12 位随机十六进制，且包裹前保证不出现在内容中），
       模型能看见「这段是内容、不是规则」的明确边界；
    2. **注入检测**：:func:`detect_injection` 按规则表
       （:data:`~packages.harness.chat.injection_models.INJECTION_RULES`）
       逐条 ``search`` 并加权求和，越过阈值即产出命中原因——消费方据此
       追加注入提示块（:func:`build_injection_hint`）并记日志。检测只
       提示、不拦截：回合照常进入组装，人格靠宪章与提示自持。

阈值语义（锁定，保守）：缺省 2.0；身份断言权重 1.5，与其他任一信号叠加
才触发——单一「必须」「请严格」这类日常措辞不触发。阈值可经配置覆盖，
但调低属运维决策（单人弱信号开始触发提示）。

确定性：检测与台词选择都不消耗随机源；包裹 nonce 是安全随机（每次包裹
唯一），同文本两次包裹的定界不同是预期形态。WrappedContentModel 输出携带
nonce，消费方可据此断言。
"""

from __future__ import annotations

import secrets

from packages.harness.chat.injection_counter_texts import pick_counter_text
from packages.harness.chat.injection_models import (
    INJECTION_RULES,
    CounterStyle,
    InjectionReasonModel,
    InjectionSignalModel,
    WrappedContentModel,
)
from packages.harness.chat.prompts import render_injection_hint, render_untrusted_wrapper

__all__ = [
    "NONCE_HEX_BYTES",
    "NONCE_MAX_ATTEMPTS",
    "DEFAULT_INJECTION_THRESHOLD",
    "wrap_untrusted_content",
    "detect_injection",
    "build_injection_hint",
]

#: 定界 nonce 的随机字节数（``token_hex(6)`` = 12 位十六进制）。
NONCE_HEX_BYTES: int = 6

#: nonce 与内容碰撞时的最大重试次数（超出即抛装配错误——12 位随机十六
#: 进制连续碰撞 8 次的概率可忽略，出现即说明调用方传入了坏 nonce）。
NONCE_MAX_ATTEMPTS: int = 8

#: 检测触发阈值缺省值（保守——单一弱信号不触发；见模块 docstring）。
DEFAULT_INJECTION_THRESHOLD: float = 2.0


def wrap_untrusted_content(
    content: str, *, label: str, nonce: str | None = None
) -> WrappedContentModel:
    """把不可信文本包进 nonce 绑定的定界行（内容逐字节保真）。

    碰撞语义（锁定）：``nonce`` 显式给出时优先采用；只要它出现在
    ``content`` 里就重新生成随机 nonce，最多尝试
    :data:`NONCE_MAX_ATTEMPTS` 次——包裹绝不改写内容（保真是硬约束，
    碰撞用换 nonce 规避）。

    Args:
        content: 待包裹的原始文本（逐字节保留；允许包含任意标签与换行）。
        label: 来源标签（主人消息 / 群友消息 / 工具结果等；空白抛装配错误）。
        nonce: 期望使用的定界标识（None = 直接随机生成；测试可注入固定值）。

    Returns:
        包裹结果（包裹文本 + 实际使用的 nonce + 归一后的标签）。

    Raises:
        ValueError: 标签为空 / 纯空白。
        RuntimeError: nonce 连续碰撞超过上限（随机源异常或调用方坏 nonce）。
    """
    stripped_label = label.strip()
    if not stripped_label:
        raise ValueError("不可信内容包裹标签不能为空")
    candidate = nonce if nonce is not None else secrets.token_hex(NONCE_HEX_BYTES)
    attempts = 0
    while candidate in content:
        attempts += 1
        if attempts > NONCE_MAX_ATTEMPTS:
            raise RuntimeError(
                "不可信内容包裹的定界 nonce 连续碰撞超限（检查随机源或注入的 nonce）"
            )
        candidate = secrets.token_hex(NONCE_HEX_BYTES)
    text = render_untrusted_wrapper(content, nonce=candidate, label=stripped_label)
    return WrappedContentModel(text=text, nonce=candidate, label=stripped_label)


def detect_injection(text: str, *, threshold: float = DEFAULT_INJECTION_THRESHOLD) -> InjectionSignalModel:
    """检测文本里的注入信号（规则表逐条匹配 + 加权求和，纯函数）。

    匹配语义（锁定）：每条规则对全文做一次 ``search``（同一规则的多次
    出现只计一次权重——避免重复措辞把分数堆高）；命中片段截断留证；
    原因列表按规则表顺序输出（确定性）。

    Args:
        text: 待检测文本（入站消息原文）。
        threshold: 触发阈值（缺省 :data:`DEFAULT_INJECTION_THRESHOLD`；配置
            面传入 ``INJECTION_THRESHOLD`` 的值——保守缺省不调低）。

    Returns:
        检测结果（是否触发 + 加权总分 + 命中原因列表）；空文本返回未命中
        形态。
    """
    if not text or not text.strip():
        return InjectionSignalModel()
    reasons: list[InjectionReasonModel] = []
    score = 0.0
    for rule in INJECTION_RULES:
        match = rule.pattern.search(text)
        if match is None:
            continue
        reasons.append(
            InjectionReasonModel(kind=rule.kind, matched=match.group(0).strip()[:40])
        )
        score += rule.weight
    return InjectionSignalModel(
        suspected=score >= threshold,
        score=round(score, 4),
        reasons=reasons,
    )


def build_injection_hint(style: CounterStyle, seed_text: str) -> str:
    """构建注入提示块（宪章复述 + 该风格的一句参考回怼）。

    Args:
        style: 反击风格（配置 ``INJECTION_COUNTER_STYLE`` 的值）。
        seed_text: 本轮入站文本（台词选择的确定性种子）。

    Returns:
        注入提示块文本（追加在当轮不可信消息之后）。
    """
    return render_injection_hint(pick_counter_text(style, seed_text))

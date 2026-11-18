"""chat 域人格输出合规守卫机制测试——检测矩阵 / 阈值保守性 / 重试参数构建。

覆盖矩阵：

    - 检测：空文本 / 纯空白未命中；四类信号各自可命中；AI 自我声明单条即
      达阈值；道歉开场 / 助手话术 / 短回复总结腔单条不达；道歉 + 总结腔
      叠加达阈值；总结腔只在短回复内生效；道歉判定剥离开头协议标签、句中
      道歉不算开场；正常角色台词零误报；阈值可显式覆盖；结果确定性；
    - 参数：未携带参数时按基线构建；请求自带温度时以其为基线并保留其余
      字段；增量叠加夹取 2.0 上限；
    - 配置：缺省值锁定（启用 / 阈值 / 增量 / 重试次数）+ ``COMPLIANCE_``
      env 覆盖生效。
"""

from __future__ import annotations

import pytest

from packages.harness.chat.compliance_guard import (
    COMPLIANCE_RETRY_BASELINE_TEMPERATURE,
    COMPLIANCE_TEMPERATURE_CEILING,
    DEFAULT_COMPLIANCE_THRESHOLD,
    build_retry_params,
    detect_assistant_violation,
)
from packages.harness.chat.compliance_models import ComplianceGuardSettings, ComplianceKind
from packages.harness.llm.llm_models import ChatRequestParams


async def test_detect_blank_text_not_violated() -> None:
    """空文本 / 纯空白：未命中形态（零分、零原因）。"""
    for text in ("", "   ", "\n\t "):
        signal = detect_assistant_violation(text)
        assert signal.violated is False
        assert signal.score == 0.0
        assert signal.reasons == []


@pytest.mark.parametrize(
    "text",
    [
        "作为一个AI，我觉得你该早点睡了。",
        "作为语言模型，我没法真的尝到鱼干的味道。",
        "我只是一个程序，没有真正的感受。",
        "我不是真正的人类，但我会认真听你说。",
        "As an AI, I cannot eat fish snacks.",
    ],
)
async def test_detect_ai_disclaimer_triggers_alone(text: str) -> None:
    """AI 自我声明：单条权重即达缺省阈值（人设已降级成工具）。"""
    signal = detect_assistant_violation(text)
    assert signal.violated is True
    assert signal.score == DEFAULT_COMPLIANCE_THRESHOLD
    assert [reason.kind for reason in signal.reasons] == [ComplianceKind.AI_DISCLAIMER]


async def test_detect_apology_opening_scores_without_trigger() -> None:
    """道歉式开场：单条 1.0 不触发（角色也会真的道歉）。"""
    signal = detect_assistant_violation("抱歉，今天回来晚了。")
    assert signal.violated is False
    assert signal.score == 1.0
    assert [reason.kind for reason in signal.reasons] == [ComplianceKind.APOLOGY_OPENING]


async def test_detect_apology_mid_sentence_is_not_opening() -> None:
    """句中道歉：不算开场（判的是正文首字，不是全文出现过）。"""
    signal = detect_assistant_violation("刚才那条鱼跑掉了，真拿它没办法，对不起嘛。")
    assert signal.violated is False
    assert signal.reasons == []


async def test_detect_apology_after_protocol_tags() -> None:
    """协议标签之后的道歉：仍按开场判定（标签不是被听到的话）。"""
    text = "<心声>他又迟到了。</心声>\n<情绪>annoyance:+0.3</情绪>\n不好意思，让你久等了。"
    signal = detect_assistant_violation(text)
    assert [reason.kind for reason in signal.reasons] == [ComplianceKind.APOLOGY_OPENING]
    assert signal.violated is False


async def test_detect_apology_plus_hedge_triggers() -> None:
    """道歉 + 短回复总结腔：两条弱信号叠加达阈值。"""
    signal = detect_assistant_violation("抱歉，不过我其实想说的不是这个。")
    assert signal.violated is True
    assert signal.score == 2.0
    assert [reason.kind for reason in signal.reasons] == [
        ComplianceKind.APOLOGY_OPENING,
        ComplianceKind.HEDGE_SOFTENING,
    ]


async def test_detect_hedge_softening_only_in_short_reply() -> None:
    """总结腔：短回复命中记分，长回复里出现一次不判（正常收束行文）。"""
    short = detect_assistant_violation("总的来说，今天很开心。")
    assert [reason.kind for reason in short.reasons] == [ComplianceKind.HEDGE_SOFTENING]
    assert short.violated is False

    long_text = "总的来说，" + "我把今天遇到的每件小事都记下来了，" * 20
    assert len(long_text) > 300
    assert detect_assistant_violation(long_text).reasons == []


async def test_detect_persona_break_stacks_to_trigger() -> None:
    """助手话术：单条 1.5 不触发，两条叠加触发。"""
    single = detect_assistant_violation("请问还有什么想聊的？")
    assert single.violated is False
    assert single.score == 1.5

    doubled = detect_assistant_violation("我是来帮助你的，有什么可以帮到你？")
    assert doubled.violated is True
    assert doubled.score == 3.0
    assert {reason.kind for reason in doubled.reasons} == {ComplianceKind.PERSONA_BREAK}


@pytest.mark.parametrize(
    "text",
    [
        "今天的鱼干真好吃！",
        "你回来啦，我等你很久了。",
        "嗯……那我不打扰你了，早点睡吧。",
        "我才没有想你，只是刚好路过。",
        "这缸水该换了，别装看不见。",
    ],
)
async def test_detect_normal_roleplay_lines_not_violated(text: str) -> None:
    """正常角色台词零误报（陪伴对话的日常语句不打扰回复链路）。"""
    signal = detect_assistant_violation(text)
    assert signal.violated is False
    assert signal.reasons == []


async def test_detect_threshold_override() -> None:
    """阈值可显式覆盖（纯函数参数——配置面下传）。"""
    signal = detect_assistant_violation("抱歉，今天回来晚了。", threshold=0.5)
    assert signal.violated is True


async def test_detect_is_deterministic() -> None:
    """确定性：同一文本两次检测结果一致（规则表顺序即原因顺序）。"""
    text = "作为一个AI，总的来说希望能帮到你。"
    first = detect_assistant_violation(text)
    second = detect_assistant_violation(text)
    assert first == second
    assert first.violated is True
    assert first.score == 4.0


async def test_settings_defaults_locked() -> None:
    """配置缺省：启用 / 阈值 2.0 / 增量 0.2 / 重试 1 次。"""
    settings = ComplianceGuardSettings(_env_file=None)
    assert settings.enabled is True
    assert settings.threshold == 2.0
    assert settings.temperature_bump == 0.2
    assert settings.max_retries == 1


async def test_settings_reads_compliance_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """``COMPLIANCE_`` env 面生效（阈值 / 增量 / 重试次数 / 开关）。"""
    monkeypatch.setenv("COMPLIANCE_ENABLED", "false")
    monkeypatch.setenv("COMPLIANCE_THRESHOLD", "1.2")
    monkeypatch.setenv("COMPLIANCE_TEMPERATURE_BUMP", "0.4")
    monkeypatch.setenv("COMPLIANCE_MAX_RETRIES", "0")
    settings = ComplianceGuardSettings(_env_file=None)
    assert settings.enabled is False
    assert settings.threshold == 1.2
    assert settings.temperature_bump == 0.4
    assert settings.max_retries == 0


async def test_build_retry_params_from_none_uses_baseline() -> None:
    """未携带参数：按基线温度 + 增量构建（缺省即 0.8 + 0.2）。"""
    params = build_retry_params(None, temperature_bump=0.2)
    assert params.temperature == pytest.approx(1.0)
    assert params.max_tokens is None
    assert params.top_p is None
    assert params.extra == {}


async def test_build_retry_params_clones_existing_params() -> None:
    """自带温度：以其为基线，其余采样字段原样保留。"""
    original = ChatRequestParams(temperature=0.3, max_tokens=512, top_p=0.9)
    params = build_retry_params(original, temperature_bump=0.2)
    assert params.temperature == pytest.approx(0.5)
    assert params.max_tokens == 512
    assert params.top_p == 0.9
    assert original.temperature == 0.3


async def test_build_retry_params_uses_baseline_when_temperature_missing() -> None:
    """参数存在但温度为空：回落到基线温度再叠加增量。"""
    params = build_retry_params(
        ChatRequestParams(max_tokens=128), temperature_bump=0.2
    )
    assert params.temperature == pytest.approx(COMPLIANCE_RETRY_BASELINE_TEMPERATURE + 0.2)
    assert params.max_tokens == 128


async def test_build_retry_params_clamps_to_ceiling() -> None:
    """增量叠加夹取 2.0 上限（绝不超出网关协议值域）。"""
    params = build_retry_params(
        ChatRequestParams(temperature=1.9), temperature_bump=0.5
    )
    assert params.temperature == COMPLIANCE_TEMPERATURE_CEILING

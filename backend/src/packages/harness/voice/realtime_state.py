"""realtime voice 域会话状态机——相位迁移表（确定性 / 可测）。

相位（与下行 state 消息同值域）::

    idle → listening → thinking → speaking → listening

迁移规则表（``_RULES``——显式枚举每条合法边，无隐式兜底）覆盖：

    - 会话建立（idle + START → listening）；
    - 语音态内的起说 / 空转（listening + SPEECH_START → listening）；
    - 切段进转写（listening + SEGMENT_READY → thinking）；
    - 首片音频进播放（thinking + AUDIO_PLAYING → speaking）；
    - 轮次收尾（speaking / thinking + TURN_DONE → listening）；
    - 打断（任意非关闭相位 + BARGE_IN → listening——取消语义在会话层）；
    - 错误恢复（ASR / 生成失败 → listening 继续会话；TTS 单句失败保持
      当前相位继续后续句）；
    - 关闭（CLIENT_END / IDLE_TIMEOUT / MAX_DURATION / INTERNAL_ERROR →
      closed；closed 为吸收态）。

非法事件返回 ``allowed=False`` 且相位不变（调用方记 warning 后继续——
协议层重复 / 迟到消息不应打死会话）。状态机自身零 I/O、零时钟依赖：
时间戳由调用方注入，仅写入迁移记录。
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field

from packages.harness.voice.realtime_models import RealtimeSessionPhase

__all__ = [
    "RealtimeStateEvent",
    "RealtimeTransitionModel",
    "RealtimeStateRuleModel",
    "RealtimeVoiceStateMachine",
]


class RealtimeStateEvent(StrEnum):
    """状态机事件值域（会话层内部事件，不直接出现在 wire 上）。"""

    START = "start"
    SPEECH_START = "speech_start"
    SEGMENT_READY = "segment_ready"
    ASR_DONE = "asr_done"
    AUDIO_PLAYING = "audio_playing"
    TURN_DONE = "turn_done"
    BARGE_IN = "barge_in"
    ASR_ERROR = "asr_error"
    GENERATION_ERROR = "generation_error"
    TTS_FAILURE = "tts_failure"
    CLIENT_END = "client_end"
    IDLE_TIMEOUT = "idle_timeout"
    MAX_DURATION = "max_duration"
    INTERNAL_ERROR = "internal_error"


class RealtimeStateRuleModel(BaseModel):
    """单条相位迁移规则。

    Attributes:
        from_phase: 起始相位。
        event: 触发事件。
        to_phase: 目标相位。
        detail: 迁移语义说明（同时作为 state 消息 detail 的来源）。
    """

    from_phase: RealtimeSessionPhase = Field(description="起始相位")
    event: RealtimeStateEvent = Field(description="触发事件")
    to_phase: RealtimeSessionPhase = Field(description="目标相位")
    detail: str = Field(min_length=1, description="迁移语义说明")


class RealtimeTransitionModel(BaseModel):
    """一次迁移裁决结果。

    Attributes:
        event: 触发事件。
        from_phase: 迁移前相位。
        to_phase: 迁移后相位（非法事件与 no-op 均为迁移前相位）。
        allowed: 是否命中规则表。
        changed: 相位是否实际变化。
        detail: 语义说明。
        ts_ms: 裁决时间（调用方注入）。
    """

    event: RealtimeStateEvent = Field(description="触发事件")
    from_phase: RealtimeSessionPhase = Field(description="迁移前相位")
    to_phase: RealtimeSessionPhase = Field(description="迁移后相位")
    allowed: bool = Field(description="是否命中规则")
    changed: bool = Field(description="相位是否变化")
    detail: str = Field(description="语义说明")
    ts_ms: int = Field(ge=0, description="裁决时间（epoch 毫秒）")


#: 迁移规则表（显式枚举；命中即返回，重复规则在模块导入期由测试锁定）。
_RULES: tuple[RealtimeStateRuleModel, ...] = (
    RealtimeStateRuleModel(
        from_phase=RealtimeSessionPhase.IDLE,
        event=RealtimeStateEvent.START,
        to_phase=RealtimeSessionPhase.LISTENING,
        detail="会话建立，进入聆听",
    ),
    RealtimeStateRuleModel(
        from_phase=RealtimeSessionPhase.LISTENING,
        event=RealtimeStateEvent.SPEECH_START,
        to_phase=RealtimeSessionPhase.LISTENING,
        detail="检测到起说（继续聆听并累积语段）",
    ),
    RealtimeStateRuleModel(
        from_phase=RealtimeSessionPhase.LISTENING,
        event=RealtimeStateEvent.SEGMENT_READY,
        to_phase=RealtimeSessionPhase.THINKING,
        detail="语段切分完成，进入转写",
    ),
    RealtimeStateRuleModel(
        from_phase=RealtimeSessionPhase.LISTENING,
        event=RealtimeStateEvent.ASR_DONE,
        to_phase=RealtimeSessionPhase.LISTENING,
        detail="空转写，回到聆听",
    ),
    RealtimeStateRuleModel(
        from_phase=RealtimeSessionPhase.LISTENING,
        event=RealtimeStateEvent.AUDIO_PLAYING,
        to_phase=RealtimeSessionPhase.SPEAKING,
        detail="首片音频下发，进入播报",
    ),
    RealtimeStateRuleModel(
        from_phase=RealtimeSessionPhase.LISTENING,
        event=RealtimeStateEvent.TURN_DONE,
        to_phase=RealtimeSessionPhase.LISTENING,
        detail="轮次结束，继续聆听",
    ),
    RealtimeStateRuleModel(
        from_phase=RealtimeSessionPhase.LISTENING,
        event=RealtimeStateEvent.BARGE_IN,
        to_phase=RealtimeSessionPhase.LISTENING,
        detail="打断（无在途轮次时可忽略）",
    ),
    RealtimeStateRuleModel(
        from_phase=RealtimeSessionPhase.LISTENING,
        event=RealtimeStateEvent.ASR_ERROR,
        to_phase=RealtimeSessionPhase.LISTENING,
        detail="转写失败，继续聆听",
    ),
    RealtimeStateRuleModel(
        from_phase=RealtimeSessionPhase.LISTENING,
        event=RealtimeStateEvent.GENERATION_ERROR,
        to_phase=RealtimeSessionPhase.LISTENING,
        detail="生成失败，继续聆听",
    ),
    RealtimeStateRuleModel(
        from_phase=RealtimeSessionPhase.LISTENING,
        event=RealtimeStateEvent.TTS_FAILURE,
        to_phase=RealtimeSessionPhase.LISTENING,
        detail="合成失败（聆听态无在途音频）",
    ),
    RealtimeStateRuleModel(
        from_phase=RealtimeSessionPhase.THINKING,
        event=RealtimeStateEvent.SPEECH_START,
        to_phase=RealtimeSessionPhase.THINKING,
        detail="生成中检测到新语音（打断由会话层裁决）",
    ),
    RealtimeStateRuleModel(
        from_phase=RealtimeSessionPhase.THINKING,
        event=RealtimeStateEvent.SEGMENT_READY,
        to_phase=RealtimeSessionPhase.THINKING,
        detail="新语段进入转写（串行轮次）",
    ),
    RealtimeStateRuleModel(
        from_phase=RealtimeSessionPhase.THINKING,
        event=RealtimeStateEvent.ASR_DONE,
        to_phase=RealtimeSessionPhase.THINKING,
        detail="转写完成，生成中",
    ),
    RealtimeStateRuleModel(
        from_phase=RealtimeSessionPhase.THINKING,
        event=RealtimeStateEvent.AUDIO_PLAYING,
        to_phase=RealtimeSessionPhase.SPEAKING,
        detail="首片音频下发，进入播报",
    ),
    RealtimeStateRuleModel(
        from_phase=RealtimeSessionPhase.THINKING,
        event=RealtimeStateEvent.TURN_DONE,
        to_phase=RealtimeSessionPhase.LISTENING,
        detail="轮次结束（无音频或文本已收尾），回到聆听",
    ),
    RealtimeStateRuleModel(
        from_phase=RealtimeSessionPhase.THINKING,
        event=RealtimeStateEvent.BARGE_IN,
        to_phase=RealtimeSessionPhase.LISTENING,
        detail="打断在途生成，回到聆听",
    ),
    RealtimeStateRuleModel(
        from_phase=RealtimeSessionPhase.THINKING,
        event=RealtimeStateEvent.ASR_ERROR,
        to_phase=RealtimeSessionPhase.LISTENING,
        detail="转写失败，回到聆听",
    ),
    RealtimeStateRuleModel(
        from_phase=RealtimeSessionPhase.THINKING,
        event=RealtimeStateEvent.GENERATION_ERROR,
        to_phase=RealtimeSessionPhase.LISTENING,
        detail="生成失败，回到聆听",
    ),
    RealtimeStateRuleModel(
        from_phase=RealtimeSessionPhase.THINKING,
        event=RealtimeStateEvent.TTS_FAILURE,
        to_phase=RealtimeSessionPhase.THINKING,
        detail="单句合成失败，继续生成",
    ),
    RealtimeStateRuleModel(
        from_phase=RealtimeSessionPhase.SPEAKING,
        event=RealtimeStateEvent.SPEECH_START,
        to_phase=RealtimeSessionPhase.SPEAKING,
        detail="播放中检测到新语音（打断开关关闭时继续播放）",
    ),
    RealtimeStateRuleModel(
        from_phase=RealtimeSessionPhase.SPEAKING,
        event=RealtimeStateEvent.SEGMENT_READY,
        to_phase=RealtimeSessionPhase.THINKING,
        detail="新语段进入转写（旧轮音频播完后串行生成）",
    ),
    RealtimeStateRuleModel(
        from_phase=RealtimeSessionPhase.SPEAKING,
        event=RealtimeStateEvent.AUDIO_PLAYING,
        to_phase=RealtimeSessionPhase.SPEAKING,
        detail="后续音频分片下发",
    ),
    RealtimeStateRuleModel(
        from_phase=RealtimeSessionPhase.SPEAKING,
        event=RealtimeStateEvent.TURN_DONE,
        to_phase=RealtimeSessionPhase.LISTENING,
        detail="播报结束，回到聆听",
    ),
    RealtimeStateRuleModel(
        from_phase=RealtimeSessionPhase.SPEAKING,
        event=RealtimeStateEvent.BARGE_IN,
        to_phase=RealtimeSessionPhase.LISTENING,
        detail="打断播报，回到聆听",
    ),
    RealtimeStateRuleModel(
        from_phase=RealtimeSessionPhase.SPEAKING,
        event=RealtimeStateEvent.ASR_ERROR,
        to_phase=RealtimeSessionPhase.LISTENING,
        detail="转写失败，停止播报语义面，回到聆听",
    ),
    RealtimeStateRuleModel(
        from_phase=RealtimeSessionPhase.SPEAKING,
        event=RealtimeStateEvent.GENERATION_ERROR,
        to_phase=RealtimeSessionPhase.SPEAKING,
        detail="生成失败但已入队音频照常播完",
    ),
    RealtimeStateRuleModel(
        from_phase=RealtimeSessionPhase.SPEAKING,
        event=RealtimeStateEvent.TTS_FAILURE,
        to_phase=RealtimeSessionPhase.SPEAKING,
        detail="单句合成失败，继续后续句",
    ),
    RealtimeStateRuleModel(
        from_phase=RealtimeSessionPhase.IDLE,
        event=RealtimeStateEvent.CLIENT_END,
        to_phase=RealtimeSessionPhase.CLOSED,
        detail="会话关闭（客户端结束）",
    ),
    RealtimeStateRuleModel(
        from_phase=RealtimeSessionPhase.LISTENING,
        event=RealtimeStateEvent.CLIENT_END,
        to_phase=RealtimeSessionPhase.CLOSED,
        detail="会话关闭（客户端结束）",
    ),
    RealtimeStateRuleModel(
        from_phase=RealtimeSessionPhase.THINKING,
        event=RealtimeStateEvent.CLIENT_END,
        to_phase=RealtimeSessionPhase.CLOSED,
        detail="会话关闭（客户端结束）",
    ),
    RealtimeStateRuleModel(
        from_phase=RealtimeSessionPhase.SPEAKING,
        event=RealtimeStateEvent.CLIENT_END,
        to_phase=RealtimeSessionPhase.CLOSED,
        detail="会话关闭（客户端结束）",
    ),
    RealtimeStateRuleModel(
        from_phase=RealtimeSessionPhase.IDLE,
        event=RealtimeStateEvent.IDLE_TIMEOUT,
        to_phase=RealtimeSessionPhase.CLOSED,
        detail="会话关闭（空闲超时）",
    ),
    RealtimeStateRuleModel(
        from_phase=RealtimeSessionPhase.LISTENING,
        event=RealtimeStateEvent.IDLE_TIMEOUT,
        to_phase=RealtimeSessionPhase.CLOSED,
        detail="会话关闭（空闲超时）",
    ),
    RealtimeStateRuleModel(
        from_phase=RealtimeSessionPhase.THINKING,
        event=RealtimeStateEvent.IDLE_TIMEOUT,
        to_phase=RealtimeSessionPhase.CLOSED,
        detail="会话关闭（空闲超时）",
    ),
    RealtimeStateRuleModel(
        from_phase=RealtimeSessionPhase.SPEAKING,
        event=RealtimeStateEvent.IDLE_TIMEOUT,
        to_phase=RealtimeSessionPhase.CLOSED,
        detail="会话关闭（空闲超时）",
    ),
    RealtimeStateRuleModel(
        from_phase=RealtimeSessionPhase.IDLE,
        event=RealtimeStateEvent.MAX_DURATION,
        to_phase=RealtimeSessionPhase.CLOSED,
        detail="会话关闭（达到最大时长）",
    ),
    RealtimeStateRuleModel(
        from_phase=RealtimeSessionPhase.LISTENING,
        event=RealtimeStateEvent.MAX_DURATION,
        to_phase=RealtimeSessionPhase.CLOSED,
        detail="会话关闭（达到最大时长）",
    ),
    RealtimeStateRuleModel(
        from_phase=RealtimeSessionPhase.THINKING,
        event=RealtimeStateEvent.MAX_DURATION,
        to_phase=RealtimeSessionPhase.CLOSED,
        detail="会话关闭（达到最大时长）",
    ),
    RealtimeStateRuleModel(
        from_phase=RealtimeSessionPhase.SPEAKING,
        event=RealtimeStateEvent.MAX_DURATION,
        to_phase=RealtimeSessionPhase.CLOSED,
        detail="会话关闭（达到最大时长）",
    ),
    RealtimeStateRuleModel(
        from_phase=RealtimeSessionPhase.IDLE,
        event=RealtimeStateEvent.INTERNAL_ERROR,
        to_phase=RealtimeSessionPhase.CLOSED,
        detail="会话关闭（内部错误）",
    ),
    RealtimeStateRuleModel(
        from_phase=RealtimeSessionPhase.LISTENING,
        event=RealtimeStateEvent.INTERNAL_ERROR,
        to_phase=RealtimeSessionPhase.CLOSED,
        detail="会话关闭（内部错误）",
    ),
    RealtimeStateRuleModel(
        from_phase=RealtimeSessionPhase.THINKING,
        event=RealtimeStateEvent.INTERNAL_ERROR,
        to_phase=RealtimeSessionPhase.CLOSED,
        detail="会话关闭（内部错误）",
    ),
    RealtimeStateRuleModel(
        from_phase=RealtimeSessionPhase.SPEAKING,
        event=RealtimeStateEvent.INTERNAL_ERROR,
        to_phase=RealtimeSessionPhase.CLOSED,
        detail="会话关闭（内部错误）",
    ),
    RealtimeStateRuleModel(
        from_phase=RealtimeSessionPhase.CLOSED,
        event=RealtimeStateEvent.CLIENT_END,
        to_phase=RealtimeSessionPhase.CLOSED,
        detail="已关闭（吸收态）",
    ),
    RealtimeStateRuleModel(
        from_phase=RealtimeSessionPhase.CLOSED,
        event=RealtimeStateEvent.IDLE_TIMEOUT,
        to_phase=RealtimeSessionPhase.CLOSED,
        detail="已关闭（吸收态）",
    ),
    RealtimeStateRuleModel(
        from_phase=RealtimeSessionPhase.CLOSED,
        event=RealtimeStateEvent.MAX_DURATION,
        to_phase=RealtimeSessionPhase.CLOSED,
        detail="已关闭（吸收态）",
    ),
    RealtimeStateRuleModel(
        from_phase=RealtimeSessionPhase.CLOSED,
        event=RealtimeStateEvent.INTERNAL_ERROR,
        to_phase=RealtimeSessionPhase.CLOSED,
        detail="已关闭（吸收态）",
    ),
)


class RealtimeVoiceStateMachine:
    """会话相位状态机（规则表驱动；非法事件不改变相位）。"""

    def __init__(self) -> None:
        self._phase = RealtimeSessionPhase.IDLE

    @property
    def phase(self) -> RealtimeSessionPhase:
        """当前相位。"""
        return self._phase

    def transition(
        self, event: RealtimeStateEvent, *, ts_ms: int
    ) -> RealtimeTransitionModel:
        """按规则表裁决一次事件。

        Args:
            event: 触发事件。
            ts_ms: 裁决时间（epoch 毫秒——仅写入迁移记录）。

        Returns:
            迁移裁决（命中规则时 ``allowed=True`` 并按表推进相位；未命中
            时相位不变且 ``allowed=False``）。
        """
        for rule in _RULES:
            if rule.from_phase == self._phase and rule.event == event:
                from_phase = self._phase
                self._phase = rule.to_phase
                return RealtimeTransitionModel(
                    event=event,
                    from_phase=from_phase,
                    to_phase=rule.to_phase,
                    allowed=True,
                    changed=from_phase != rule.to_phase,
                    detail=rule.detail,
                    ts_ms=ts_ms,
                )
        return RealtimeTransitionModel(
            event=event,
            from_phase=self._phase,
            to_phase=self._phase,
            allowed=False,
            changed=False,
            detail=f"相位 {self._phase.value} 不接受事件 {event.value}",
            ts_ms=ts_ms,
        )

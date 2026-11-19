"""realtime voice 域 VAD——能量 / 静音窗口切段（双阈值滞回 + 防抖）。

机制（全部确定性、纯字节输入，不读系统时钟）：

    - 逐帧计算归一化能量（:func:`~packages.harness.voice.realtime_audio.
      compute_rms_normalized`）；
    - **起说**：连续 ``vad_start_consecutive_frames`` 帧达到起说阈值才进入
      语音态（单帧尖峰 / 键盘声不触发）；候选帧全部并入语段缓冲（不丢
      起说前导音）；
    - **断句**：进入语音态后，帧能量持续低于断句阈值满
      ``vad_silence_end_ms`` 即切段；任一帧回到阈值以上则静音计时清零
      （滞回，避免句中换气被切成两段）；
    - **最短语音**：语段有效语音（达到断句阈值的帧时长累计）低于
      ``vad_min_speech_ms`` 直接丢弃，不进 ASR；
    - **单段上限**：语段总时长达到 ``vad_max_segment_ms`` 强制切段（长
      独白不无限阻塞转写）；
    - 计时全部由帧时长累加得出（与接收节奏无关），事件时间戳取调用方
      注入的 ``now_ms``——伪时钟测试可复现同一事件序列。

边界：本模块只做「切段」，不做降噪 / 回声消除 / 说话人分离（部署侧
AEC 与声纹属独立能力面，见 docs/backend/realtime-voice.md 边界一节）。
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field

from packages.harness.voice.realtime_audio import compute_rms_normalized, pcm16_duration_ms
from packages.harness.voice.realtime_models import RealtimeVoiceSettings

__all__ = [
    "VadConfigModel",
    "VadEventKind",
    "VadEventModel",
    "VadFeedResultModel",
    "EnergyVad",
    "build_vad_config",
]


class VadConfigModel(BaseModel):
    """VAD 运行参数（由 Settings 派生——纯数据，与算法分离）。

    Attributes:
        sample_rate_hz: 采样率。
        channels: 声道数。
        speech_rms_threshold: 起说阈值（0-1）。
        silence_rms_threshold: 断句阈值（0-1，滞回下沿）。
        start_consecutive_frames: 起说防抖帧数。
        min_speech_ms: 最短有效语音毫秒。
        silence_end_ms: 静音结束窗口毫秒。
        max_segment_ms: 单段上限毫秒。
    """

    sample_rate_hz: int = Field(gt=0, description="采样率")
    channels: int = Field(gt=0, le=2, description="声道数")
    speech_rms_threshold: float = Field(ge=0.0, le=1.0, description="起说阈值")
    silence_rms_threshold: float = Field(ge=0.0, le=1.0, description="断句阈值")
    start_consecutive_frames: int = Field(ge=1, description="起说防抖帧数")
    min_speech_ms: int = Field(ge=0, description="最短有效语音毫秒")
    silence_end_ms: int = Field(ge=0, description="静音结束窗口毫秒")
    max_segment_ms: int = Field(ge=1, description="单段上限毫秒")


def build_vad_config(settings: RealtimeVoiceSettings) -> VadConfigModel:
    """从实时语音 Settings 派生 VAD 参数（字段一一对应，无隐藏默认）。

    Args:
        settings: 实时语音配置（加载期已完成跨字段不变式校验）。

    Returns:
        VAD 运行参数。
    """
    return VadConfigModel(
        sample_rate_hz=settings.sample_rate_hz,
        channels=settings.channels,
        speech_rms_threshold=settings.vad_speech_rms_threshold,
        silence_rms_threshold=settings.vad_silence_rms_threshold,
        start_consecutive_frames=settings.vad_start_consecutive_frames,
        min_speech_ms=settings.vad_min_speech_ms,
        silence_end_ms=settings.vad_silence_end_ms,
        max_segment_ms=settings.vad_max_segment_ms,
    )


class VadEventKind(StrEnum):
    """VAD 事件类型。"""

    SPEECH_START = "speech_start"
    SEGMENT_READY = "segment_ready"
    SEGMENT_DISCARDED = "segment_discarded"
    SEGMENT_FORCED = "segment_forced"


class VadEventModel(BaseModel):
    """VAD 事件（切段结果载荷）。

    Attributes:
        kind: 事件类型。
        ts_ms: 事件时间（调用方注入的 epoch 毫秒）。
        pcm16: 语段 PCM16 字节（``segment_ready`` / ``segment_forced``
            携带；其余为 None）。
        duration_ms: 语段有效语音时长（毫秒；切段类事件携带）。
        total_ms: 语段总时长（含静音帧，毫秒；切段类事件携带）。
    """

    kind: VadEventKind = Field(description="事件类型")
    ts_ms: int = Field(ge=0, description="事件时间（epoch 毫秒）")
    pcm16: bytes | None = Field(default=None, description="语段 PCM16 字节")
    duration_ms: int | None = Field(default=None, ge=0, description="有效语音时长")
    total_ms: int | None = Field(default=None, ge=0, description="语段总时长")


class VadFeedResultModel(BaseModel):
    """单帧喂入结果（事件批次 + 当前语音态）。

    Attributes:
        events: 本帧触发的事件（时间序，通常为空）。
        speaking: 喂入后是否处于语音态。
        buffered_bytes: 当前语段已缓冲字节数（中途转写判定面）。
        voiced_ms: 当前语段有效语音时长（中途转写判定面）。
    """

    events: list[VadEventModel] = Field(default_factory=list, description="事件批次")
    speaking: bool = Field(description="是否处于语音态")
    buffered_bytes: int = Field(default=0, ge=0, description="语段缓冲字节数")
    voiced_ms: int = Field(default=0, ge=0, description="语段有效语音时长")


class EnergyVad:
    """能量 / 静音窗口 VAD（双阈值滞回 + 防抖 + 段上限）。

    Args:
        config: VAD 参数（见 :class:`VadConfigModel`）。
    """

    def __init__(self, config: VadConfigModel) -> None:
        self._config = config
        self._speaking = False
        self._candidate: list[bytes] = []
        self._candidate_duration_ms = 0
        self._candidate_voiced_ms = 0
        self._segment: list[bytes] = []
        self._segment_elapsed_ms = 0
        self._segment_voiced_ms = 0
        self._silence_ms = 0

    @property
    def speaking(self) -> bool:
        """当前是否处于语音态（起说已确认、尚未切段）。"""
        return self._speaking

    @property
    def voiced_ms(self) -> int:
        """当前语段有效语音累计毫秒（非语音态为 0）。"""
        return self._segment_voiced_ms if self._speaking else 0

    def current_audio(self) -> bytes:
        """当前语段已缓冲的 PCM16 字节（非语音态为空字节）。"""
        return b"".join(self._segment) if self._speaking else b""

    def feed(self, pcm16: bytes, now_ms: int) -> VadFeedResultModel:
        """喂入一帧 PCM16（事件时间戳取 ``now_ms``）。

        Args:
            pcm16: 16 位小端 PCM 字节（空载荷按静音帧处理）。
            now_ms: 当前时间（epoch 毫秒——仅用于事件时间戳）。

        Returns:
            本帧事件批次与语音态快照。
        """
        frame_ms = pcm16_duration_ms(
            pcm16, self._config.sample_rate_hz, self._config.channels
        )
        energy = compute_rms_normalized(pcm16)
        if not self._speaking:
            return self._feed_idle(pcm16, energy, frame_ms, now_ms)
        return self._feed_speaking(pcm16, energy, frame_ms, now_ms)

    def flush(self, now_ms: int) -> VadFeedResultModel:
        """冲刷当前语段（会话结束 / 客户端 end——按切段规则产出事件）。

        Args:
            now_ms: 当前时间（epoch 毫秒）。

        Returns:
            切段事件批次（无语音态时为空）。
        """
        if not self._speaking:
            return VadFeedResultModel(events=[], speaking=False)
        event = self._finish(now_ms, forced=False)
        return VadFeedResultModel(events=[event] if event is not None else [], speaking=False)

    def _feed_idle(
        self, pcm16: bytes, energy: float, frame_ms: int, now_ms: int
    ) -> VadFeedResultModel:
        """非语音态：起说防抖判定。

        Args:
            pcm16: 本帧载荷。
            energy: 本帧归一化能量。
            frame_ms: 本帧时长毫秒。
            now_ms: 当前时间（epoch 毫秒）。

        Returns:
            喂入结果（起说确认时携带 SPEECH_START 事件）。
        """
        if energy < self._config.speech_rms_threshold:
            self._candidate.clear()
            self._candidate_duration_ms = 0
            self._candidate_voiced_ms = 0
            return VadFeedResultModel(events=[], speaking=False)
        self._candidate.append(pcm16)
        self._candidate_duration_ms += frame_ms
        self._candidate_voiced_ms += frame_ms
        if len(self._candidate) < self._config.start_consecutive_frames:
            return VadFeedResultModel(events=[], speaking=False)
        self._speaking = True
        self._segment = list(self._candidate)
        self._segment_elapsed_ms = self._candidate_duration_ms
        self._segment_voiced_ms = self._candidate_voiced_ms
        self._silence_ms = 0
        self._candidate.clear()
        self._candidate_duration_ms = 0
        self._candidate_voiced_ms = 0
        return VadFeedResultModel(
            events=[VadEventModel(kind=VadEventKind.SPEECH_START, ts_ms=now_ms)],
            speaking=True,
            buffered_bytes=sum(len(frame) for frame in self._segment),
            voiced_ms=self._segment_voiced_ms,
        )

    def _feed_speaking(
        self, pcm16: bytes, energy: float, frame_ms: int, now_ms: int
    ) -> VadFeedResultModel:
        """语音态：缓冲累积 + 静音窗 / 段上限判定。

        Args:
            pcm16: 本帧载荷。
            energy: 本帧归一化能量。
            frame_ms: 本帧时长毫秒。
            now_ms: 当前时间（epoch 毫秒）。

        Returns:
            喂入结果（切段时携带段事件）。
        """
        self._segment.append(pcm16)
        self._segment_elapsed_ms += frame_ms
        events: list[VadEventModel] = []
        if energy <= self._config.silence_rms_threshold:
            self._silence_ms += frame_ms
        else:
            self._silence_ms = 0
            self._segment_voiced_ms += frame_ms
        if self._silence_ms >= self._config.silence_end_ms:
            event = self._finish(now_ms, forced=False)
            if event is not None:
                events.append(event)
        elif self._segment_elapsed_ms >= self._config.max_segment_ms:
            event = self._finish(now_ms, forced=True)
            if event is not None:
                events.append(event)
        return VadFeedResultModel(
            events=events,
            speaking=self._speaking,
            buffered_bytes=sum(len(frame) for frame in self._segment)
            if self._speaking
            else 0,
            voiced_ms=self._segment_voiced_ms if self._speaking else 0,
        )

    def _finish(self, now_ms: int, *, forced: bool) -> VadEventModel | None:
        """结束当前语段（按最短语音门槛产出 ready / discarded / forced）。

        Args:
            now_ms: 事件时间（epoch 毫秒）。
            forced: 是否由段上限强制触发。

        Returns:
            切段事件；语段缓冲为空时返回 None（防御性分支——正常流程
            不会出现空语段）。
        """
        pcm = b"".join(self._segment)
        total_ms = self._segment_elapsed_ms
        voiced_ms = self._segment_voiced_ms
        self._reset_segment()
        if not pcm:
            return None
        if not forced and voiced_ms < self._config.min_speech_ms:
            return VadEventModel(
                kind=VadEventKind.SEGMENT_DISCARDED,
                ts_ms=now_ms,
                duration_ms=voiced_ms,
                total_ms=total_ms,
            )
        kind = VadEventKind.SEGMENT_FORCED if forced else VadEventKind.SEGMENT_READY
        return VadEventModel(
            kind=kind,
            ts_ms=now_ms,
            pcm16=pcm,
            duration_ms=voiced_ms,
            total_ms=total_ms,
        )

    def _reset_segment(self) -> None:
        """复位语音态与全部段内计数（下次起说重新走防抖）。"""
        self._speaking = False
        self._segment = []
        self._segment_elapsed_ms = 0
        self._segment_voiced_ms = 0
        self._silence_ms = 0
        self._candidate.clear()
        self._candidate_duration_ms = 0
        self._candidate_voiced_ms = 0

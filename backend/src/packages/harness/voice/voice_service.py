"""voice 域服务——TTS / ASR 编排门面（人格绑定 + 分面预算 + 用量埋点 + 注入式客户端）。

调用链（与 llm 网关同构的编排面）::

    synthesize(text, persona)
        ↓ ① enabled 门控（settings.voice.tts.enabled=False → CONFLICT）
        ↓ ② 发音参数解析：请求参数 > persona.voice_profile > settings 缺省
        ↓ ③ TTS_DAILY_BUDGET_* 分面日预算闸（persona → global；超限拒绝）
        ↓ ④ tts client（注入优先；缺省经公共池共享 httpx client 瞬时构建）
        ↓ ⑤ CosyVoiceTtsClient.synthesize（POST /tts → WAV 字节）
        ↓ ⑥ 成功用量发射（purpose="tts"，token 0/0；失败 / 被拒不落行）
    transcribe(audio, filename, persona)
        ↓ 同构链（enabled 门控 asr 分节；ASR_DAILY_BUDGET_*；purpose="asr"；语言解析：
           请求参数 > settings.voice.asr.language）

设计决策：

    - **voice 不硬依赖 usage 域**：服务持有注入的异步记录器
      (:data:`VoiceUsageRecorder`) 与分面计数面（:data:`VoiceBudgetCounter`）；
      默认接线经 :mod:`packages.harness.voice.voice_budget` 的
      ``wire_default_voice_usage_recorder`` / ``wire_default_voice_usage_counter``
      （app / CLI 装配调用，函数体内惰性 import usage 域——与 llm 网关
      wire_default_usage_recorder 同模式）；
    - **客户端按调用瞬时构建**（注入优先；零连接资源——wire 配置随
      settings 生效，无缓存失效问题）；
    - **人格绑定覆盖**：persona.voice_profile 存在时其 speaker / speed
      覆盖 settings 缺省，请求级显式参数又覆盖人格绑定（优先级：
      请求 > 人格 > settings）；
    - **预算按 purpose 分面**：TTS / ASR 各自独立上限与计数（查询按
      purpose 过滤，LLM / 声纹行不计入）；0 = 不限且零计数查询；软竞态
      与跨 worker 近似口径见 docs/backend/voice.md；
    - **用量发射恒不阻断主调用**：记录器异常仅告警（用量是观测面）；
    - **上下文轴**：persona_id / request_id 显式参数优先，缺省读
      core.context 上下文变量（请求中间件已绑定则自动携带）；
    - **双轨流式通道**：``open_asr_stream`` / ``synthesize_stream`` 在既有
      批量客户端之外提供真流式面（开关默认关闭；客户端由装配层按
      ``VOICE_ASR_STREAMING_ENABLED`` / ``VOICE_TTS_STREAMING_ENABLED``
      构造注入）。计次口径与批量分面同源：ASR 流式连接成功开启计 1 次、
      收尾落 1 行；TTS 每句成功流式合成计 1 次——被预算拒绝时零建连、
      零出站；失败不落行（调用方回退批量主链）。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Awaitable, Callable

import httpx

from packages.harness.config.models import Settings
from packages.harness.config.models import settings as global_settings
from packages.harness.core.context import persona_id_var, request_id_var
from packages.harness.core.errors import BusinessException, ErrorCode
from packages.harness.core.time_utils import now_ms
from packages.harness.persona.dtos import PersonaRecordDTO
from packages.harness.pools.manager import get_pool_manager
from packages.harness.voice.asr_client import Qwen3AsrClient
from packages.harness.voice.asr_stream_client import (
    AsrStreamClientFace,
    AsrStreamHandleFace,
)
from packages.harness.voice.tts_client import CosyVoiceTtsClient
from packages.harness.voice.tts_stream_client import TtsStreamClientFace
from packages.harness.voice.voice_budget import (
    DailyBudgetLimits,
    VoiceBudgetCounter,
    VoiceUsageRecorder,
    emit_voice_usage,
    enforce_daily_budget,
    wire_default_voice_usage_counter,
    wire_default_voice_usage_recorder,
)
from packages.harness.voice.voice_models import (
    AsrClientFace,
    AsrResultModel,
    TtsClientFace,
    TtsResultModel,
)

__all__ = [
    "VoiceService",
    "VoiceBudgetCounter",
    "VoiceUsageRecorder",
    "AsrStreamClientFace",
    "AsrStreamHandleFace",
    "TtsStreamClientFace",
    "wire_default_voice_usage_counter",
    "wire_default_voice_usage_recorder",
]

#: TTS 用量行的 provider 值（CosyVoice3 合成服务）。
_TTS_PROVIDER: str = "cosyvoice"
#: ASR 用量行的 provider 值（Qwen3-ASR 转写服务）。
_ASR_PROVIDER: str = "qwen3-asr"


class VoiceService:
    """语音域服务（门面——编排 enabled 门控 / 人格绑定 / 客户端 / 用量）。

    构造注入（测试 / 装配友好）：tts_client / asr_client / settings /
    usage_recorder 均可显式注入；客户端缺省在调用时经公共池共享
    httpx client 瞬时构建（池未启动 fail loud——与 llm 网关同前置契约）。
    """

    def __init__(
        self,
        *,
        tts_client: TtsClientFace | None = None,
        asr_client: AsrClientFace | None = None,
        asr_stream_client: AsrStreamClientFace | None = None,
        tts_stream_client: TtsStreamClientFace | None = None,
        settings: Settings | None = None,
        usage_recorder: VoiceUsageRecorder | None = None,
        usage_counter: VoiceBudgetCounter | None = None,
    ) -> None:
        """初始化服务。

        Args:
            tts_client: TTS 客户端（None = 调用时经公共池瞬时构建）。
            asr_client: ASR 客户端（None = 调用时经公共池瞬时构建）。
            asr_stream_client: 流式 ASR 客户端（None = 流式通道不可用——
                实时会话回退既有批量转写；装配层经设置开关构造真实客户端）。
            tts_stream_client: 流式 TTS 客户端（None = 流式通道不可用——
                实时会话回退既有整句批量合成）。
            settings: Settings（None = config 全局单例）。
            usage_recorder: 用量记录器（None = 未接线——零记录，装配时经
                wire_default_voice_usage_recorder 绑定）。
            usage_counter: 分面预算计数面（None = 零预算门禁——纯编排 /
                测试形态；装配时经 wire_default_voice_usage_counter 绑定）。
        """
        self._tts_client = tts_client
        self._asr_client = asr_client
        self._asr_stream_client = asr_stream_client
        self._tts_stream_client = tts_stream_client
        self._settings = settings
        self._usage_recorder = usage_recorder
        self._usage_counter = usage_counter

    # ------------------------------------------------------------------
    # 合成面（TTS）
    # ------------------------------------------------------------------

    async def synthesize(
        self,
        text: str,
        *,
        persona: PersonaRecordDTO | None = None,
        speaker: str | None = None,
        speed: float | None = None,
        request_id: str | None = None,
    ) -> TtsResultModel:
        """合成语音（人格绑定优先级：请求参数 > persona.voice_profile >
        settings 缺省；仅成功调用发射 usage 行，失败 / 被拒不落行）。

        Args:
            text: 待合成文本（1-500 字符——客户端边界校验）。
            persona: 人格记录（None = 无人格绑定——settings 缺省参数 +
                用量 persona_id 读请求上下文）。
            speaker: 发音人显式覆盖（None = 走人格绑定 / settings 缺省）。
            speed: 语速显式覆盖（None = 走人格绑定 / settings 缺省）。
            request_id: 请求追踪 ID（None = 读请求上下文）。

        Returns:
            合成结果（audio / content_type / latency_ms）。

        Raises:
            BusinessException: TTS 未启用（``CONFLICT``——加语音是显式
                管理动作）；日预算超限（``RATE_LIMITED``——details 含
                scope / used / limit / purpose）；公共池未启动
                （``INTERNAL_ERROR``）；请求参数越界
                （``VALIDATION_ERROR``）；服务调用失败
                （``LLM_CALL_FAILED``——见 tts_client 映射表）。
        """
        settings = self._settings if self._settings is not None else global_settings
        tts_settings = settings.voice.tts
        if not tts_settings.enabled:
            raise BusinessException(
                code=ErrorCode.CONFLICT,
                message="语音合成未启用（VOICE_TTS_ENABLED=true 时方可调用）",
                details={"reason": "tts disabled", "env_flag": "VOICE_TTS_ENABLED"},
            )
        profile = persona.voice_profile if persona is not None else None
        effective_speaker = speaker if speaker is not None else (
            profile.speaker if profile is not None else tts_settings.default_speaker
        )
        effective_speed = speed if speed is not None else (
            profile.speed if profile is not None else tts_settings.default_speed
        )
        instruct_text = profile.instruct_text if profile is not None else None
        resolved_persona = str(persona.id) if persona is not None else persona_id_var.get()
        resolved_request_id = request_id if request_id is not None else request_id_var.get()
        await enforce_daily_budget(
            purpose="tts",
            label="TTS 语音合成",
            limits=DailyBudgetLimits(
                global_limit=tts_settings.daily_budget_global,
                persona_limit=tts_settings.daily_budget_persona,
            ),
            persona_id=resolved_persona,
            counter=self._usage_counter,
            clock=now_ms,
        )
        client = self._resolve_tts_client()
        started_ms = now_ms()
        result = await client.synthesize(
            text,
            speaker=effective_speaker,
            speed=effective_speed,
            instruct_text=instruct_text,
        )
        await emit_voice_usage(
            self._usage_recorder,
            purpose="tts",
            model_entry=tts_settings.model_entry,
            provider=_TTS_PROVIDER,
            persona_id=resolved_persona,
            request_id=resolved_request_id,
            ts_ms=started_ms,
            latency_ms=now_ms() - started_ms,
        )
        return result

    # ------------------------------------------------------------------
    # 转写面（ASR）
    # ------------------------------------------------------------------

    async def transcribe(
        self,
        audio: bytes,
        filename: str,
        *,
        persona: PersonaRecordDTO | None = None,
        language: str | None = None,
        request_id: str | None = None,
    ) -> AsrResultModel:
        """转写音频（语言解析：请求参数 > settings.voice.asr.language；
        仅成功调用发射 usage 行，失败 / 被拒不落行）。

        Args:
            audio: 音频字节（WAV / MP3 等常见容器）。
            filename: 原始文件名（multipart file 字段携带）。
            persona: 人格记录（仅用于用量绑定——ASR 无发音参数面）。
            language: 语言提示显式覆盖（None = settings 缺省；两者皆空 =
                端点自动检测）。
            request_id: 请求追踪 ID（None = 读请求上下文）。

        Returns:
            转写结果（text / duration_ms / language）。

        Raises:
            BusinessException: ASR 未启用（``CONFLICT``）；日预算超限
                （``RATE_LIMITED``——details 含 scope / used / limit /
                purpose）；公共池未启动（``INTERNAL_ERROR``）；音频 /
                文件名非法（``VALIDATION_ERROR``）；服务调用失败
                （``LLM_CALL_FAILED``——见 asr_client 映射表）。
        """
        settings = self._settings if self._settings is not None else global_settings
        asr_settings = settings.voice.asr
        if not asr_settings.enabled:
            raise BusinessException(
                code=ErrorCode.CONFLICT,
                message="语音识别未启用（VOICE_ASR_ENABLED=true 时方可调用）",
                details={"reason": "asr disabled", "env_flag": "VOICE_ASR_ENABLED"},
            )
        effective_language = language if language is not None else asr_settings.language
        resolved_persona = str(persona.id) if persona is not None else persona_id_var.get()
        resolved_request_id = request_id if request_id is not None else request_id_var.get()
        await enforce_daily_budget(
            purpose="asr",
            label="ASR 语音识别",
            limits=DailyBudgetLimits(
                global_limit=asr_settings.daily_budget_global,
                persona_limit=asr_settings.daily_budget_persona,
            ),
            persona_id=resolved_persona,
            counter=self._usage_counter,
            clock=now_ms,
        )
        client = self._resolve_asr_client()
        started_ms = now_ms()
        result = await client.transcribe(audio, filename, language=effective_language)
        await emit_voice_usage(
            self._usage_recorder,
            purpose="asr",
            model_entry=asr_settings.model_entry,
            provider=_ASR_PROVIDER,
            persona_id=resolved_persona,
            request_id=resolved_request_id,
            ts_ms=started_ms,
            latency_ms=now_ms() - started_ms,
        )
        return result

    # ------------------------------------------------------------------
    # 流式通道（双轨——开关与客户端可用性双门控；失败由调用方回退批量）
    # ------------------------------------------------------------------

    @property
    def streaming_asr_available(self) -> bool:
        """流式识别通道是否可用（开关 + 客户端注入 + 批量 ASR 启用）。

        Returns:
            True 表示实时会话可走真流式转写（partial 为真增量）。
        """
        settings = self._settings if self._settings is not None else global_settings
        asr_settings = settings.voice.asr
        return bool(
            asr_settings.enabled
            and asr_settings.streaming_enabled
            and self._asr_stream_client is not None
        )

    @property
    def streaming_tts_available(self) -> bool:
        """流式合成通道是否可用（开关 + 客户端注入 + 批量 TTS 启用）。

        Returns:
            True 表示实时会话可按句流式合成。
        """
        settings = self._settings if self._settings is not None else global_settings
        tts_settings = settings.voice.tts
        return bool(
            tts_settings.enabled
            and tts_settings.streaming_enabled
            and self._tts_stream_client is not None
        )

    @property
    def streaming_tts_sample_rate_hz(self) -> int:
        """流式合成输出 PCM16 采样率（会话层 WAV 封装依据）。

        Returns:
            配置的采样率（Hz）。
        """
        settings = self._settings if self._settings is not None else global_settings
        return settings.voice.tts.streaming_sample_rate_hz

    async def open_asr_stream(
        self,
        *,
        sample_rate_hz: int,
        on_partial: Callable[[str], Awaitable[None]],
        persona: PersonaRecordDTO | None = None,
        language: str | None = None,
        request_id: str | None = None,
    ) -> AsrStreamHandleFace:
        """建立一条流式识别连接（预算闸先于建连；计量口径见类 docstring）。

        计次口径（锁定，与分面预算语义一致）：连接**成功开启**计 1 次，
        用量行在连接收尾（``finish`` 成功或 ``cancel``）时落账；开启失败
        （握手失败 / 超时）不落行。被预算拒绝时**零建连、零出站**。

        Args:
            sample_rate_hz: 上行 PCM 采样率（Hz——客户端握手参数）。
            on_partial: 进行中转写回调（会话层下发 ``partial_transcript``）。
            persona: 人格记录（用量 persona 绑定）。
            language: 语言提示（None = settings 缺省 / 端点自动检测）。
            request_id: 请求追踪 ID（None = 读请求上下文）。

        Returns:
            已握手的流式识别连接句柄（用量计次包装）。

        Raises:
            BusinessException: ASR 未启用 / 流式通道未接通（``CONFLICT``）；
                日预算超限（``RATE_LIMITED``——零建连）；连接 / 握手失败
                （``LLM_CALL_FAILED``——调用方回退批量）。
        """
        settings = self._settings if self._settings is not None else global_settings
        asr_settings = settings.voice.asr
        if not asr_settings.enabled:
            raise BusinessException(
                code=ErrorCode.CONFLICT,
                message="语音识别未启用（VOICE_ASR_ENABLED=true 时方可调用）",
                details={"reason": "asr disabled", "env_flag": "VOICE_ASR_ENABLED"},
            )
        client = self._asr_stream_client
        if client is None or not asr_settings.streaming_enabled:
            raise BusinessException(
                code=ErrorCode.CONFLICT,
                message="流式语音识别通道未接通（VOICE_ASR_STREAMING_ENABLED / 客户端装配）",
                details={"reason": "asr streaming unavailable", "mode": "streaming"},
            )
        effective_language = language if language is not None else asr_settings.language
        resolved_persona = str(persona.id) if persona is not None else persona_id_var.get()
        resolved_request_id = request_id if request_id is not None else request_id_var.get()
        await enforce_daily_budget(
            purpose="asr",
            label="ASR 语音识别",
            limits=DailyBudgetLimits(
                global_limit=asr_settings.daily_budget_global,
                persona_limit=asr_settings.daily_budget_persona,
            ),
            persona_id=resolved_persona,
            counter=self._usage_counter,
            clock=now_ms,
        )
        started_ms = now_ms()
        handle = await client.open(
            sample_rate_hz=sample_rate_hz,
            language=effective_language,
            on_partial=on_partial,
        )
        return _StreamAsrUsageHandle(
            handle,
            recorder=self._usage_recorder,
            model_entry=asr_settings.model_entry,
            persona_id=resolved_persona,
            request_id=resolved_request_id,
            started_ms=started_ms,
        )

    async def synthesize_stream(
        self,
        text: str,
        *,
        persona: PersonaRecordDTO | None = None,
        speaker: str | None = None,
        speed: float | None = None,
        request_id: str | None = None,
    ) -> AsyncGenerator[bytes, None]:
        """流式合成一句文本（逐分片产出 PCM16；预算闸先于建连）。

        计次口径（锁定）：每句流式合成成功计 1 次（与批量句级等价）——
        仅在分片流正常收尾且产出过至少一个非空分片后落一行 usage（空音频
        回退批量，由批量路径独立记账——一句不得两行）；中途取消
        （barge-in）不落行；被预算拒绝时零建连、零出站。

        Args:
            text: 待合成文本（1-500 字符——客户端边界校验）。
            persona: 人格记录（None = 无人格绑定）。
            speaker: 发音人显式覆盖（None = 人格绑定 / settings 缺省）。
            speed: 语速显式覆盖（None = 人格绑定 / settings 缺省）。
            request_id: 请求追踪 ID（None = 读请求上下文）。

        Yields:
            PCM16 分片（顺序即播放序；采样率见
            :attr:`streaming_tts_sample_rate_hz`）。

        Raises:
            BusinessException: TTS 未启用 / 流式通道未接通（``CONFLICT``）；
                日预算超限（``RATE_LIMITED``——零建连）；参数越界
                （``VALIDATION_ERROR``）；连接 / 分片失败
                （``LLM_CALL_FAILED``——调用方回退批量）。
        """
        settings = self._settings if self._settings is not None else global_settings
        tts_settings = settings.voice.tts
        if not tts_settings.enabled:
            raise BusinessException(
                code=ErrorCode.CONFLICT,
                message="语音合成未启用（VOICE_TTS_ENABLED=true 时方可调用）",
                details={"reason": "tts disabled", "env_flag": "VOICE_TTS_ENABLED"},
            )
        client = self._tts_stream_client
        if client is None or not tts_settings.streaming_enabled:
            raise BusinessException(
                code=ErrorCode.CONFLICT,
                message="流式语音合成通道未接通（VOICE_TTS_STREAMING_ENABLED / 客户端装配）",
                details={"reason": "tts streaming unavailable", "mode": "streaming"},
            )
        profile = persona.voice_profile if persona is not None else None
        effective_speaker = speaker if speaker is not None else (
            profile.speaker if profile is not None else tts_settings.default_speaker
        )
        effective_speed = speed if speed is not None else (
            profile.speed if profile is not None else tts_settings.default_speed
        )
        instruct_text = profile.instruct_text if profile is not None else None
        resolved_persona = str(persona.id) if persona is not None else persona_id_var.get()
        resolved_request_id = request_id if request_id is not None else request_id_var.get()
        await enforce_daily_budget(
            purpose="tts",
            label="TTS 语音合成",
            limits=DailyBudgetLimits(
                global_limit=tts_settings.daily_budget_global,
                persona_limit=tts_settings.daily_budget_persona,
            ),
            persona_id=resolved_persona,
            counter=self._usage_counter,
            clock=now_ms,
        )
        started_ms = now_ms()
        stream = client.synthesize_stream(
            text,
            speaker=effective_speaker,
            speed=effective_speed,
            instruct_text=instruct_text,
        )
        produced = False
        try:
            async for fragment in stream:
                if fragment:
                    produced = True
                    yield fragment
        finally:
            await stream.aclose()
        if not produced:
            return
        await emit_voice_usage(
            self._usage_recorder,
            purpose="tts",
            model_entry=tts_settings.model_entry,
            provider=_TTS_PROVIDER,
            persona_id=resolved_persona,
            request_id=resolved_request_id,
            ts_ms=started_ms,
            latency_ms=now_ms() - started_ms,
        )

    # ------------------------------------------------------------------
    # 内部——客户端解析 / 用量发射
    # ------------------------------------------------------------------

    def _resolve_tts_client(self) -> TtsClientFace:
        """取 TTS 客户端（注入优先；缺省经公共池共享 client 瞬时构建）。

        Raises:
            BusinessException: 公共池未启动（``INTERNAL_ERROR``——调用
                顺序契约破坏面；与 llm 网关同前置检查）。
        """
        if self._tts_client is not None:
            return self._tts_client
        settings = self._settings if self._settings is not None else global_settings
        tts_settings = settings.voice.tts
        return CosyVoiceTtsClient(
            self._require_http_client(),
            base_url=tts_settings.base_url,
            request_timeout_seconds=tts_settings.request_timeout_seconds,
        )

    def _resolve_asr_client(self) -> AsrClientFace:
        """取 ASR 客户端（注入优先；缺省经公共池共享 client 瞬时构建）。

        Raises:
            BusinessException: 公共池未启动（``INTERNAL_ERROR``）。
        """
        if self._asr_client is not None:
            return self._asr_client
        settings = self._settings if self._settings is not None else global_settings
        asr_settings = settings.voice.asr
        return Qwen3AsrClient(
            self._require_http_client(),
            base_url=asr_settings.base_url,
            model_id=asr_settings.model_entry,
            request_timeout_seconds=asr_settings.request_timeout_seconds,
        )

    @staticmethod
    def _require_http_client() -> httpx.AsyncClient:
        """取公共池共享 HTTP client（未启动 fail loud）。

        Returns:
            共享 ``httpx.AsyncClient``（生命周期归 PoolManager）。

        Raises:
            BusinessException: 公共池未初始化 / 未启动（``INTERNAL_ERROR``）。
        """
        manager = get_pool_manager()
        if manager is None or not manager.started:
            raise BusinessException(
                code=ErrorCode.INTERNAL_ERROR,
                message="公共池未初始化，请先在 lifespan startup 中调用 pool_manager.start()",
            )
        return manager.get_http_client()


class _StreamAsrUsageHandle:
    """流式识别连接的用量计次包装（连接收尾时落一行 usage）。

    计次口径（锁定）：连接成功开启计 1 次，收尾（``finish`` 成功或
    ``cancel``）时落一行成功用量；开启失败不经过本包装（不落行）；
    ``cancel`` 幂等——重复调用不重复记账。

    Args:
        handle: 已握手的流式识别连接句柄。
        recorder: 用量记录器（None = 未接线——零记录）。
        model_entry: 用量行模型条目名。
        persona_id: 人格 ID（None = 系统调用）。
        request_id: 请求追踪 ID。
        started_ms: 连接开启时间（epoch 毫秒——时延计量起点）。
    """

    __slots__ = (
        "_handle",
        "_recorder",
        "_model_entry",
        "_persona_id",
        "_request_id",
        "_started_ms",
        "_settled",
        "_finish_unsettled",
    )

    def __init__(
        self,
        handle: AsrStreamHandleFace,
        *,
        recorder: VoiceUsageRecorder | None,
        model_entry: str,
        persona_id: str | None,
        request_id: str | None,
        started_ms: int,
    ) -> None:
        self._handle = handle
        self._recorder = recorder
        self._model_entry = model_entry
        self._persona_id = persona_id
        self._request_id = request_id
        self._started_ms = started_ms
        self._settled = False
        self._finish_unsettled = False

    async def feed(self, pcm16: bytes) -> None:
        """追加一帧 PCM16（委托底层连接）。

        Args:
            pcm16: 16 位小端单声道 PCM 字节。
        """
        await self._handle.feed(pcm16)

    async def finish(self) -> AsrResultModel:
        """提交并等待最终稿（成功后落一行用量）。

        ``finish`` 已发起但未成功落定（失败 / 被外部取消）时标记
        ``_finish_unsettled``：随后会话清理路径的 ``cancel()`` 只关闭
        连接，不再补记一行——与「流式失败回退批量、由批量路径记账」的
        既有计次口径一致。

        Returns:
            最终转写结果。

        Raises:
            BusinessException: 收尾失败（``LLM_CALL_FAILED``——调用方回退批量）。
            asyncio.CancelledError: 收尾等待被外部取消（原样上抛）。
        """
        try:
            result = await self._handle.finish()
        except (Exception, asyncio.CancelledError):
            self._finish_unsettled = True
            raise
        await self._settle()
        return result

    async def cancel(self) -> None:
        """取消并关闭连接（干净取消落一行用量；收尾未落定的清理不补记）。"""
        await self._handle.cancel()
        if not self._finish_unsettled:
            await self._settle()

    @property
    def frames_dropped(self) -> int:
        """发送队列溢出丢弃的帧数（观测面）。"""
        return self._handle.frames_dropped

    async def _settle(self) -> None:
        """落一行成功用量（幂等——首个收尾动作生效）。"""
        if self._settled:
            return
        self._settled = True
        await emit_voice_usage(
            self._recorder,
            purpose="asr",
            model_entry=self._model_entry,
            provider=_ASR_PROVIDER,
            persona_id=self._persona_id,
            request_id=self._request_id,
            ts_ms=self._started_ms,
            latency_ms=now_ms() - self._started_ms,
        )

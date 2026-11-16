"""voice 域服务——TTS / ASR 编排门面（人格绑定 + 用量埋点 + 注入式客户端）。

调用链（与 llm 网关同构的编排面）::

    synthesize(text, persona)
        ↓ ① enabled 门控（settings.voice.tts.enabled=False → CONFLICT）
        ↓ ② 发音参数解析：请求参数 > persona.voice_profile > settings 缺省
        ↓ ③ tts client（注入优先；缺省经公共池共享 httpx client 瞬时构建）
        ↓ ④ CosyVoiceTtsClient.synthesize（POST /tts → WAV 字节）
        ↓ ⑤ 用量发射（成功 / 失败路径均落账——purpose="tts"，token 0/0）
    transcribe(audio, filename, persona)
        ↓ 同构链（enabled 门控 asr 分节；purpose="asr"；语言解析：
           请求参数 > settings.voice.asr.language）

设计决策：

    - **voice 不硬依赖 usage 域**：服务持有注入的异步记录器
      (:data:`VoiceUsageRecorder`)；默认接线经
      :func:`wire_default_voice_usage_recorder`（app / CLI 装配调用，
      函数体内惰性 import usage 域——与 llm 网关 wire_default_usage_recorder
      同模式）；
    - **客户端按调用瞬时构建**（注入优先；零连接资源——wire 配置随
      settings 生效，无缓存失效问题）；
    - **人格绑定覆盖**：persona.voice_profile 存在时其 speaker / speed
      覆盖 settings 缺省，请求级显式参数又覆盖人格绑定（优先级：
      请求 > 人格 > settings）；
    - **用量发射恒不阻断主调用**：记录器异常仅告警（用量是观测面）；
    - **上下文轴**：persona_id / request_id 显式参数优先，缺省读
      core.context 上下文变量（请求中间件已绑定则自动携带）。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import httpx
from loguru import logger

from packages.harness.config.models import Settings
from packages.harness.config.models import settings as global_settings
from packages.harness.core.context import persona_id_var, request_id_var
from packages.harness.core.errors import BusinessException, ErrorCode
from packages.harness.core.time_utils import now_ms
from packages.harness.persona.dtos import PersonaRecordDTO
from packages.harness.pools.manager import get_pool_manager
from packages.harness.voice.asr_client import Qwen3AsrClient
from packages.harness.voice.tts_client import CosyVoiceTtsClient
from packages.harness.voice.voice_models import (
    AsrClientFace,
    AsrResultModel,
    TtsClientFace,
    TtsResultModel,
    VoiceUsageEvent,
)

__all__ = ["VoiceService", "VoiceUsageRecorder", "wire_default_voice_usage_recorder"]

#: 用量记录器契约——voice 服务 → usage 域的注入式异步回调。
VoiceUsageRecorder = Callable[[VoiceUsageEvent], Awaitable[None]]

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
        settings: Settings | None = None,
        usage_recorder: VoiceUsageRecorder | None = None,
    ) -> None:
        """初始化服务。

        Args:
            tts_client: TTS 客户端（None = 调用时经公共池瞬时构建）。
            asr_client: ASR 客户端（None = 调用时经公共池瞬时构建）。
            settings: Settings（None = config 全局单例）。
            usage_recorder: 用量记录器（None = 未接线——零记录，装配时经
                wire_default_voice_usage_recorder 绑定）。
        """
        self._tts_client = tts_client
        self._asr_client = asr_client
        self._settings = settings
        self._usage_recorder = usage_recorder

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
        settings 缺省；成功 / 失败均发射 usage 行）。

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
                管理动作）；公共池未启动（``INTERNAL_ERROR``）；请求参数
                越界（``VALIDATION_ERROR``）；服务调用失败
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
        client = self._resolve_tts_client()
        started_ms = now_ms()
        try:
            result = await client.synthesize(
                text,
                speaker=effective_speaker,
                speed=effective_speed,
                instruct_text=instruct_text,
            )
        except BusinessException as exc:
            await self._emit_usage(
                purpose="tts",
                model_entry=tts_settings.model_entry,
                provider=_TTS_PROVIDER,
                persona_id=resolved_persona,
                request_id=resolved_request_id,
                ts_ms=started_ms,
                latency_ms=now_ms() - started_ms,
                status="error",
                error_code=str(exc.code),
            )
            raise
        await self._emit_usage(
            purpose="tts",
            model_entry=tts_settings.model_entry,
            provider=_TTS_PROVIDER,
            persona_id=resolved_persona,
            request_id=resolved_request_id,
            ts_ms=started_ms,
            latency_ms=now_ms() - started_ms,
            status="success",
            error_code=None,
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
        成功 / 失败均发射 usage 行）。

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
            BusinessException: ASR 未启用（``CONFLICT``）；公共池未启动
                （``INTERNAL_ERROR``）；音频 / 文件名非法
                （``VALIDATION_ERROR``）；服务调用失败
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
        client = self._resolve_asr_client()
        started_ms = now_ms()
        try:
            result = await client.transcribe(audio, filename, language=effective_language)
        except BusinessException as exc:
            await self._emit_usage(
                purpose="asr",
                model_entry=asr_settings.model_entry,
                provider=_ASR_PROVIDER,
                persona_id=resolved_persona,
                request_id=resolved_request_id,
                ts_ms=started_ms,
                latency_ms=now_ms() - started_ms,
                status="error",
                error_code=str(exc.code),
            )
            raise
        await self._emit_usage(
            purpose="asr",
            model_entry=asr_settings.model_entry,
            provider=_ASR_PROVIDER,
            persona_id=resolved_persona,
            request_id=resolved_request_id,
            ts_ms=started_ms,
            latency_ms=now_ms() - started_ms,
            status="success",
            error_code=None,
        )
        return result

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

    async def _emit_usage(
        self,
        *,
        purpose: str,
        model_entry: str,
        provider: str,
        persona_id: str | None,
        request_id: str | None,
        ts_ms: int,
        latency_ms: int,
        status: str,
        error_code: str | None,
    ) -> None:
        """发射用量事件（未接线时 no-op；记录器异常仅告警不阻断）。

        Args:
            purpose: 调用用途（tts / asr）。
            model_entry: 模型条目名（settings.voice.*.model_entry）。
            provider: provider 类型（cosyvoice / qwen3-asr）。
            persona_id: 人格 ID（None = 系统调用）。
            request_id: 请求追踪 ID。
            ts_ms: 调用发起时间（epoch 毫秒）。
            latency_ms: 端到端时延毫秒（真实时延——含失败耗时）。
            status: 结果（success / error）。
            error_code: 失败错误码（成功为 None）。
        """
        recorder = self._usage_recorder
        if recorder is None:
            return
        event = VoiceUsageEvent(
            ts_ms=ts_ms,
            persona_id=persona_id,
            purpose=purpose,
            model_entry=model_entry,
            provider=provider,
            latency_ms=latency_ms,
            status=status,
            request_id=request_id,
            error_code=error_code,
        )
        try:
            await recorder(event)
        except Exception:
            logger.opt(exception=True).warning(
                "voice 用量记录发射失败（不阻断主调用）: purpose={}, model_entry={}, status={}",
                purpose,
                model_entry,
                status,
            )


def wire_default_voice_usage_recorder() -> VoiceUsageRecorder:
    """装配默认 voice 用量记录器（绑定 usage 域服务——app / CLI 装配调用）。

    voice 域不硬依赖 usage 域：本函数是唯一接线点，函数体内惰性 import
    （调用时才解析 usage 模块——voice 模块导入期零耦合；与 llm 网关
    wire_default_usage_recorder 同模式）。装配形态::

        service = VoiceService(usage_recorder=wire_default_voice_usage_recorder())

    Returns:
        将 :class:`VoiceUsageEvent` 映射为 ``UsageCreateDTO``（token 恒
        0——语音调用无 token 语义）落库的异步记录器。
    """
    from packages.harness.usage.dtos import UsageCreateDTO
    from packages.harness.usage.models import UsagePurpose, UsageStatus
    from packages.harness.usage.service import UsageService

    service = UsageService()

    async def _record(event: VoiceUsageEvent) -> None:
        """事件 → usage 域创建 DTO（值域枚举化；token 0/0/0）→ 落库。"""
        await service.create(
            UsageCreateDTO(
                ts_ms=event.ts_ms,
                persona_id=event.persona_id,
                model_entry=event.model_entry,
                provider=event.provider,
                purpose=UsagePurpose(event.purpose),
                input_tokens=0,
                output_tokens=0,
                total_tokens=0,
                latency_ms=event.latency_ms,
                status=UsageStatus(event.status),
                request_id=event.request_id,
                error_code=event.error_code,
            )
        )

    return _record

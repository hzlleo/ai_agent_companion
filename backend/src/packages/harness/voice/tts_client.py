"""voice 域 TTS 客户端——CosyVoice3 合成服务（共享 httpx client 注入）。

**部署契约（本客户端持有文档——部署侧据此包装）**：

    CosyVoice3（ModelScope 模型，``models/`` 子模块承载权重——部署机关注）
    部署为**薄包装 HTTP 服务**，暴露唯一合成端点::

        POST {base_url}/tts
        Content-Type: application/json
        {
            "text": str,           # 待合成文本（必填）
            "speaker": str | null, # 发音人（null = 服务端缺省）
            "speed": float         # 语速 0.5-2.0
            "instruct_text": str   # 可选——instruct2 模式风格指令
        }                          #（仅人格绑定携带时出现；缺省请求体
                                   # 恰为上三键——锁定 wire 形态）

        200 → Content-Type: audio/wav，响应体为 WAV 容器音频字节
        其余状态 / 非 audio/wav 内容类型 → 契约破坏（LLM_CALL_FAILED）

    权重以子模块形式只存在于部署机（后端**只经 HTTP 契约消费，永不读取
    模型文件路径**——与 llm 域「只连端点」同约定）；服务编排（compose
    接线 / 端口分配）由部署侧另行承接，本客户端零部署耦合。

错误码映射表（voice 域——**复用既有码表，core/errors.py 不动**；503
依赖不可用类对应 ``LLM_CALL_FAILED``——语音合成服务与 LLM 端点同属
「远程模型服务依赖」，失败类别一致）：

    ============================  ========================  ==========
    失败类别                       错误码                    可重试
    ============================  ========================  ==========
    文本超长 / 空文本 / 语速越界    VALIDATION_ERROR          否（调用方 bug）
    httpx 传输异常（连接 / 超时）  LLM_CALL_FAILED           由调用方决定
    其余 HTTP 非 200               LLM_CALL_FAILED           由调用方决定
    响应 Content-Type 非 audio/wav LLM_CALL_FAILED           否（契约破坏）
    响应体为空                     LLM_CALL_FAILED           否（契约破坏）
    ============================  ========================  ==========
"""

from __future__ import annotations

from typing import Any, Final

import httpx
from pydantic import ValidationError

from packages.harness.core.errors import BusinessException, ErrorCode
from packages.harness.core.time_utils import now_ms
from packages.harness.voice.voice_models import TtsRequestModel, TtsResultModel

__all__ = ["CosyVoiceTtsClient"]

#: TTS 响应的合法媒体类型（部署契约锁定 WAV 容器）。
_EXPECTED_CONTENT_TYPE: Final[str] = "audio/wav"

#: 错误 details 携带的响应体片段截断长度。
_BODY_SNIPPET_LIMIT: Final[int] = 300


class CosyVoiceTtsClient:
    """CosyVoice3 合成客户端（共享 ``httpx.AsyncClient`` 注入）。

    生命周期归注入方（pools.PoolManager / 测试 MockTransport client）；
    本类零连接资源、零 token（本地服务无鉴权——与沙盒 token 模式不同，
    部署契约不设鉴权头）。请求超时从构造参数读取（settings.voice.tts.
    request_timeout_seconds 经装配层传入）。

    Args（构造期属性文档见 ``__init__`` docstring）:
        无（见 ``__init__``）。
    """

    def __init__(
        self,
        http_client: httpx.AsyncClient,
        *,
        base_url: str = "http://127.0.0.1:9880",
        request_timeout_seconds: float = 30.0,
    ) -> None:
        """初始化客户端。

        Args:
            http_client: 共享 HTTP 客户端（硬规则：不得私建连接）。
            base_url: CosyVoice 包装服务基础 URL（尾斜杠容错）。
            request_timeout_seconds: 单次合成请求超时秒数（音频合成耗时
                显著长于普通 API——缺省 30s，长文本部署侧可调大）。
        """
        self._http = http_client
        self._base_url = base_url.rstrip("/")
        self._timeout = httpx.Timeout(request_timeout_seconds)

    async def synthesize(
        self,
        text: str,
        *,
        speaker: str | None = None,
        speed: float = 1.0,
        instruct_text: str | None = None,
    ) -> TtsResultModel:
        """合成语音（POST /tts → WAV 字节；请求体见模块 docstring 契约）。

        Args:
            text: 待合成文本（1-500 字符——越界 ``VALIDATION_ERROR``）。
            speaker: 发音人（None = 服务端缺省——wire 形态为 JSON null）。
            speed: 语速（0.5-2.0）。
            instruct_text: instruct2 模式风格指令（None = 不携带——缺省
                请求体恰为 ``{text, speaker, speed}`` 三键锁定形态）。

        Returns:
            合成结果（audio = 响应体字节；content_type 恒 ``audio/wav``；
            latency_ms = 客户端侧往返时延）。

        Raises:
            BusinessException: 请求参数越界（``VALIDATION_ERROR``——调用方
                bug 面）；传输异常 / 非 200 / 内容类型不符 / 空响应体
                （``LLM_CALL_FAILED``——映射表见模块 docstring）。
        """
        try:
            request = TtsRequestModel(text=text, speaker=speaker, speed=speed, instruct_text=instruct_text)
        except ValidationError as exc:
            raise BusinessException(
                code=ErrorCode.VALIDATION_ERROR,
                message="语音合成请求参数非法（文本 1-500 字符 / 语速 0.5-2.0）",
                details={"reason": str(exc)[:200]},
            ) from exc
        body: dict[str, Any] = {
            "text": request.text,
            "speaker": request.speaker,
            "speed": request.speed,
        }
        if request.instruct_text is not None:
            body["instruct_text"] = request.instruct_text
        started_ms = now_ms()
        try:
            response = await self._http.post(
                f"{self._base_url}/tts", json=body, timeout=self._timeout
            )
        except httpx.HTTPError as exc:
            raise self._call_error(
                reason="CosyVoice 合成服务传输失败", error=str(exc), latency_ms=now_ms() - started_ms
            ) from exc
        latency_ms = now_ms() - started_ms
        if response.status_code != 200:
            raise self._call_error(
                reason=f"CosyVoice 合成服务返回非成功状态 {response.status_code}",
                status_code=response.status_code,
                body_snippet=response.text[:_BODY_SNIPPET_LIMIT],
                latency_ms=latency_ms,
            )
        content_type = response.headers.get("content-type", "")
        media_type = content_type.split(";", maxsplit=1)[0].strip().lower()
        if media_type != _EXPECTED_CONTENT_TYPE:
            raise self._call_error(
                reason=f"CosyVoice 合成服务响应内容类型不符（期望 audio/wav，实得 {media_type!r}）",
                status_code=response.status_code,
                body_snippet=content_type,
                latency_ms=latency_ms,
            )
        if not response.content:
            raise self._call_error(
                reason="CosyVoice 合成服务返回空响应体",
                status_code=response.status_code,
                body_snippet="",
                latency_ms=latency_ms,
            )
        return TtsResultModel(
            audio=response.content,
            content_type=_EXPECTED_CONTENT_TYPE,
            latency_ms=latency_ms,
        )

    def _call_error(
        self,
        *,
        reason: str,
        latency_ms: int,
        error: str | None = None,
        status_code: int | None = None,
        body_snippet: str | None = None,
    ) -> BusinessException:
        """构造 ``LLM_CALL_FAILED`` 业务异常（details 统一拼装）。

        Args:
            reason: 失败类别描述。
            latency_ms: 失败路径往返时延（观测面携带）。
            error: 传输异常描述（传输失败形态携带）。
            status_code: HTTP 状态码（非 200 / 契约破坏形态携带）。
            body_snippet: 响应体 / 内容类型片段（诊断面）。

        Returns:
            待抛出的业务异常（调用方 ``raise ... from exc`` 保因果链）。
        """
        details: dict[str, Any] = {
            "reason": "语音合成服务调用失败",
            "service": "cosyvoice3",
            "base_url": self._base_url,
            "latency_ms": latency_ms,
        }
        if error is not None:
            details["error"] = error
        if status_code is not None:
            details["status_code"] = status_code
        if body_snippet is not None:
            details["body_snippet"] = body_snippet
        return BusinessException(
            code=ErrorCode.LLM_CALL_FAILED,
            message=f"语音合成失败: {reason}",
            details=details,
        )

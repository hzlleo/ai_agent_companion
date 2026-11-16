"""voice 域 ASR 客户端——Qwen3-ASR 转写服务（OpenAI 兼容 audio 端点）。

**部署契约（本客户端持有文档——部署侧据此起服务）**：

    Qwen3-ASR（ModelScope 模型，``models/`` 子模块承载权重——部署机关注）
    经其 vLLM 工具链服务以 **OpenAI 兼容 audio 端点**形态部署::

        POST {base_url}/v1/audio/transcriptions
        Content-Type: multipart/form-data
            file:     (filename, audio_bytes)   # 音频文件字段（必填）
            model:    str                        # 模型 id（构造期传入）
            language: str                        # 可选——语言提示

        200 → application/json
        {
            "text": str,             # 转写文本（必返）
            "duration_ms": int,      # 可选——音频时长毫秒
            "language": str          # 可选——识别语言
        }
        其余状态 → 端点错误（LLM_CALL_FAILED）

    OpenAI 兼容形态意味着标准 OpenAI SDK 亦可直连该端点；本工程按
    「不引入 SDK」约定经共享 httpx client 手写 multipart（与 llm 域 /
    channels 域同约定）。服务编排（compose 接线 / 端口分配）属后续部署
    chunk，本客户端零部署耦合。

错误码映射表（voice 域——**复用既有码表，core/errors.py 不动**；与
tts_client 同段说明：503 依赖不可用类对应 ``LLM_CALL_FAILED``）：

    ============================  ========================  ==========
    失败类别                       错误码                    可重试
    ============================  ========================  ==========
    音频为空 / 文件名缺失          VALIDATION_ERROR          否（调用方 bug）
    httpx 传输异常（连接 / 超时）  LLM_CALL_FAILED           由调用方决定
    其余 HTTP 非 200               LLM_CALL_FAILED           由调用方决定
    响应 JSON 非法 / text 缺失     LLM_CALL_FAILED           否（契约破坏）
    ============================  ========================  ==========
"""

from __future__ import annotations

from typing import Any, Final

import httpx
from pydantic import ValidationError

from packages.harness.core.errors import BusinessException, ErrorCode
from packages.harness.core.time_utils import now_ms
from packages.harness.voice.voice_models import AsrResultModel

__all__ = ["Qwen3AsrClient"]

#: multipart file 字段的上传内容类型（端点按文件名扩展名嗅探容器格式，
#: 字段内容类型声明为通用八进制流）。
_UPLOAD_CONTENT_TYPE: Final[str] = "application/octet-stream"

#: 错误 details 携带的响应体片段截断长度。
_BODY_SNIPPET_LIMIT: Final[int] = 300


class Qwen3AsrClient:
    """Qwen3-ASR 转写客户端（共享 ``httpx.AsyncClient`` 注入）。

    生命周期归注入方（pools.PoolManager / 测试 MockTransport client）；
    本类零连接资源、零鉴权（本地服务——部署契约不设鉴权头）。multipart
    构建经 httpx 原生 ``files=`` / ``data=`` 参数（零手工拼装）。

    Args（构造期属性文档见 ``__init__`` docstring）:
        无（见 ``__init__``）。
    """

    def __init__(
        self,
        http_client: httpx.AsyncClient,
        *,
        base_url: str = "http://127.0.0.1:8082",
        model_id: str = "qwen3-asr-0.6b",
        request_timeout_seconds: float = 60.0,
    ) -> None:
        """初始化客户端。

        Args:
            http_client: 共享 HTTP 客户端（硬规则：不得私建连接）。
            base_url: Qwen3-ASR 服务基础 URL（尾斜杠容错；OpenAI 兼容
                路径 ``/v1/audio/transcriptions`` 由本类拼接）。
            model_id: 模型 id（multipart ``model`` 字段值——与用量记账的
                model_entry 同名约定，由装配层从 settings 传入）。
            request_timeout_seconds: 单次转写请求超时秒数（音频上传 +
                转写耗时——缺省 60s）。
        """
        self._http = http_client
        self._base_url = base_url.rstrip("/")
        self._model_id = model_id
        self._timeout = httpx.Timeout(request_timeout_seconds)

    async def transcribe(
        self,
        audio: bytes,
        filename: str,
        *,
        language: str | None = None,
    ) -> AsrResultModel:
        """转写音频（POST /v1/audio/transcriptions multipart → JSON）。

        Args:
            audio: 音频字节（WAV / MP3 等常见容器——容器格式由端点按
                文件名扩展名嗅探）。
            filename: 原始文件名（multipart file 字段携带；空 / 缺失
                ``VALIDATION_ERROR``）。
            language: 语言提示（None = 端点自动检测；multipart 可选
                ``language`` 字段）。

        Returns:
            转写结果（text / duration_ms / language——后两者端点未回传
            时为 None）。

        Raises:
            BusinessException: 音频为空 / 文件名缺失（``VALIDATION_ERROR``
                ——调用方 bug 面）；传输异常 / 非 200 / JSON 非法 /
                text 缺失（``LLM_CALL_FAILED``——映射表见模块 docstring）。
        """
        if not audio:
            raise BusinessException(
                code=ErrorCode.VALIDATION_ERROR,
                message="语音识别请求的音频内容不能为空",
                details={"filename": filename, "audio_bytes": 0},
            )
        if not filename.strip():
            raise BusinessException(
                code=ErrorCode.VALIDATION_ERROR,
                message="语音识别请求的文件名不能为空",
            )
        files: dict[str, tuple[str, bytes, str]] = {
            "file": (filename, audio, _UPLOAD_CONTENT_TYPE),
        }
        data: dict[str, str] = {"model": self._model_id}
        if language is not None and language.strip():
            data["language"] = language.strip()
        started_ms = now_ms()
        try:
            response = await self._http.post(
                f"{self._base_url}/v1/audio/transcriptions",
                files=files,
                data=data,
                timeout=self._timeout,
            )
        except httpx.HTTPError as exc:
            raise self._call_error(
                reason="Qwen3-ASR 转写服务传输失败", error=str(exc), latency_ms=now_ms() - started_ms
            ) from exc
        latency_ms = now_ms() - started_ms
        if response.status_code != 200:
            raise self._call_error(
                reason=f"Qwen3-ASR 转写服务返回非成功状态 {response.status_code}",
                status_code=response.status_code,
                body_snippet=response.text[:_BODY_SNIPPET_LIMIT],
                latency_ms=latency_ms,
            )
        try:
            result = AsrResultModel.model_validate_json(response.content)
        except ValidationError as exc:
            raise self._call_error(
                reason="Qwen3-ASR 响应不是合法的转写 JSON（text 字段缺失等）",
                status_code=response.status_code,
                body_snippet=response.text[:_BODY_SNIPPET_LIMIT],
                parse_error=str(exc)[:200],
                latency_ms=latency_ms,
            ) from exc
        return result

    def _call_error(
        self,
        *,
        reason: str,
        latency_ms: int,
        error: str | None = None,
        status_code: int | None = None,
        body_snippet: str | None = None,
        parse_error: str | None = None,
    ) -> BusinessException:
        """构造 ``LLM_CALL_FAILED`` 业务异常（details 统一拼装）。

        Args:
            reason: 失败类别描述。
            latency_ms: 失败路径往返时延（观测面携带）。
            error: 传输异常描述（传输失败形态携带）。
            status_code: HTTP 状态码（非 200 形态携带）。
            body_snippet: 响应体片段（诊断面）。
            parse_error: JSON 解析错误（解析失败形态携带）。

        Returns:
            待抛出的业务异常（调用方 ``raise ... from exc`` 保因果链）。
        """
        details: dict[str, Any] = {
            "reason": "语音识别服务调用失败",
            "service": "qwen3-asr",
            "base_url": self._base_url,
            "latency_ms": latency_ms,
        }
        if error is not None:
            details["error"] = error
        if status_code is not None:
            details["status_code"] = status_code
        if body_snippet is not None:
            details["body_snippet"] = body_snippet
        if parse_error is not None:
            details["parse_error"] = parse_error
        return BusinessException(
            code=ErrorCode.LLM_CALL_FAILED,
            message=f"语音识别失败: {reason}",
            details=details,
        )

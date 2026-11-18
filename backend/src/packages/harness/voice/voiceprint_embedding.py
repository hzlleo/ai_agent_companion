"""voice 域声纹向量客户端——可配置说话人 embedding 服务（HTTP 契约）。

**部署契约（本客户端持有文档——部署侧据此包装）**：说话人向量提取由部署
机上的独立服务承担（不引入新依赖、不读取模型文件路径——与 tts / asr 同
约定）::

    POST {VOICEPRINT_EMBEDDING_BASE_URL}{VOICEPRINT_EMBEDDING_PATH}
    Content-Type: multipart/form-data
        file:  (filename, audio_bytes, content_type)   # 音频文件（必填）
        model: str                                      # 模型名（构造期传入）

    200 → application/json
    {
        "embedding": [float, ...]        # 非空、全为有限浮点
    }
    # 亦接受 OpenAI 兼容形态：{"data": [{"embedding": [float, ...]}]}

    其余状态 → LLM_CALL_FAILED（details 携带状态码与响应体片段）

错误码映射表（与 tts / asr 同段——复用既有码表，``core/errors.py`` 不动）：

    ============================  ========================  ==========
    失败类别                       错误码                    可重试
    ============================  ========================  ==========
    音频为空 / 文件名缺失          VALIDATION_ERROR          否（调用方 bug）
    httpx 传输异常（连接 / 超时）  LLM_CALL_FAILED           由调用方决定
    其余 HTTP 非 200               LLM_CALL_FAILED           由调用方决定
    响应 JSON 非法 / 向量缺失      LLM_CALL_FAILED           否（契约破坏）
    ============================  ========================  ==========
"""

from __future__ import annotations

import math
from typing import Any, Final

import httpx

from packages.harness.core.errors import BusinessException, ErrorCode
from packages.harness.core.time_utils import now_ms

__all__ = ["VoiceprintEmbeddingClient"]

#: 错误 details 携带的响应体片段截断长度。
_BODY_SNIPPET_LIMIT: Final[int] = 300


def _parse_embedding(payload: Any) -> list[float]:
    """从响应 JSON 解析向量（直连形态 / OpenAI 兼容形态）。

    Args:
        payload: 响应 JSON 反序列化结果。

    Returns:
        非空有限浮点向量。

    Raises:
        ValueError: 形态不符 / 向量为空 / 含非数值或非有限值（契约破坏）。
    """
    if not isinstance(payload, dict):
        raise ValueError("响应不是 JSON 对象")
    candidate: Any = payload.get("embedding")
    if candidate is None:
        data = payload.get("data")
        if isinstance(data, list) and data and isinstance(data[0], dict):
            candidate = data[0].get("embedding")
    if not isinstance(candidate, list) or not candidate:
        raise ValueError("响应缺少非空 embedding 数组")
    vector: list[float] = []
    for item in candidate:
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            raise ValueError("embedding 含非数值元素")
        value = float(item)
        if not math.isfinite(value):
            raise ValueError("embedding 含非有限数值")
        vector.append(value)
    return vector


class VoiceprintEmbeddingClient:
    """说话人向量客户端（共享 ``httpx.AsyncClient`` 注入——零私建连接）。

    生命周期归注入方（pools.PoolManager / 测试 MockTransport client）；
    本类零连接资源、零鉴权（本地服务——部署契约不设鉴权头）。
    """

    def __init__(
        self,
        http_client: httpx.AsyncClient,
        *,
        base_url: str,
        endpoint_path: str = "/embedding",
        model_id: str = "voiceprint-embedding",
        request_timeout_seconds: float = 30.0,
    ) -> None:
        """初始化客户端。

        Args:
            http_client: 共享 HTTP 客户端（硬规则：不得私建连接）。
            base_url: 向量服务基础 URL（尾斜杠容错）。
            endpoint_path: 向量端点路径（缺省 ``/embedding``）。
            model_id: 模型名（multipart ``model`` 字段值）。
            request_timeout_seconds: 单次提取请求超时秒数。
        """
        self._http = http_client
        self._base_url = base_url.rstrip("/")
        self._endpoint_path = endpoint_path if endpoint_path.startswith("/") else f"/{endpoint_path}"
        self._model_id = model_id
        self._timeout = httpx.Timeout(request_timeout_seconds)

    async def embed(self, audio: bytes, filename: str, content_type: str) -> list[float]:
        """提取说话人向量（multipart 上传 → 向量数组）。

        Args:
            audio: 音频字节（非空）。
            filename: 原始文件名（multipart file 字段携带）。
            content_type: 音频 MIME 类型（file 字段内容类型）。

        Returns:
            非空浮点向量。

        Raises:
            BusinessException: 音频为空 / 文件名缺失（``VALIDATION_ERROR``）；
                传输异常 / 非 200 / JSON 非法 / 向量缺失
                （``LLM_CALL_FAILED``——映射表见模块 docstring）。
        """
        if not audio:
            raise BusinessException(
                code=ErrorCode.VALIDATION_ERROR,
                message="声纹向量提取的音频内容不能为空",
                details={"filename": filename, "audio_bytes": 0},
            )
        if not filename.strip():
            raise BusinessException(
                code=ErrorCode.VALIDATION_ERROR,
                message="声纹向量提取的文件名不能为空",
            )
        files: dict[str, tuple[str, bytes, str]] = {
            "file": (filename, audio, content_type),
        }
        started_ms = now_ms()
        try:
            response = await self._http.post(
                f"{self._base_url}{self._endpoint_path}",
                files=files,
                data={"model": self._model_id},
                timeout=self._timeout,
            )
        except httpx.HTTPError as exc:
            raise self._call_error(
                reason="说话人向量服务传输失败",
                error=str(exc),
                latency_ms=now_ms() - started_ms,
            ) from exc
        latency_ms = now_ms() - started_ms
        if response.status_code != 200:
            raise self._call_error(
                reason=f"说话人向量服务返回非成功状态 {response.status_code}",
                status_code=response.status_code,
                body_snippet=response.text[:_BODY_SNIPPET_LIMIT],
                latency_ms=latency_ms,
            )
        try:
            payload = response.json()
            return _parse_embedding(payload)
        except ValueError as exc:
            raise self._call_error(
                reason="说话人向量服务响应契约破坏（embedding 数组缺失或非法）",
                status_code=response.status_code,
                body_snippet=response.text[:_BODY_SNIPPET_LIMIT],
                parse_error=str(exc)[:200],
                latency_ms=latency_ms,
            ) from exc

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
            latency_ms: 失败路径往返时延。
            error: 传输异常描述（传输失败形态携带）。
            status_code: HTTP 状态码（非 200 形态携带）。
            body_snippet: 响应体片段（诊断面）。
            parse_error: 解析错误（响应契约破坏形态携带）。

        Returns:
            待抛出的业务异常（调用方 ``raise ... from exc`` 保因果链）。
        """
        details: dict[str, Any] = {
            "reason": "说话人向量服务调用失败",
            "service": "voiceprint-embedding",
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
            message=f"声纹向量提取失败: {reason}",
            details=details,
        )

"""harness/multimodal look_at 服务——图像理解编排门面 + LLM 工具注册面。

调用链（单一真相，文档化于 docs/backend/multimodal.md）::

    describe(image, mime_type, question)
        ↓ ① 请求校验（LookAtRequestModel——MIME 允许清单 / 10MB 上限 /
        ↓     聚焦问题上限；ValidationError → VALIDATION_ERROR）
        ↓ ② data URL 构建（build_data_url——图像字节内联，不落盘不外发）
        ↓ ③ 消息组装（system 视觉理解任务书 + user 内容分块
        ↓     [image, text(question 或缺省指令)]——mm_prompts Unit 文件）
        ↓ ④ 条目链解析（resolver.resolve(look_at) → 角色 条目；
        ↓     resolve_multimodal_chain——多模态直连 / 非多模态回退
        ↓     [local, remote]；链空 → CONFIG_INVALID fail loud）
        ↓ ⑤ 逐条目尝试（gateway.chat_for_entry，purpose="look_at"——
        ↓     用量行由网关自动发射，服务层零双记；失败 / 空响应 →
        ↓     下一级；全部失败 → LLM_CALL_FAILED 汇总）
        ↓ ⑥ 结果归一（description / model_entry / latency_ms）

设计决策：

    - **multimodal 不硬依赖 usage 域**：用量行完全由网关
      ``chat_for_entry(purpose="look_at")`` 自动发射（成功 / 失败路径
      均落账——回退链逐条目尝试各成一行，天然观测重试面）；
    - **网关 / 解析器注入式**（测试 / 装配友好）：gateway 缺省
      :class:`~packages.harness.llm.gateway.LlmGateway`（调用时读全局
      resolver / pools——app lifespan 装配后生效）；resolver 缺省
      config 全局单例（loader 热重建后自动生效）；
    - **图像字节永不落日志**：日志仅携带尺寸 / MIME / 条目名等元数据；
    - **单条目链透传原始错误**（多模态直连或仅一级回退的失败不被
      包裹——熔断 / 鉴权 / 传输错误码原样上抛）；多级链全部失败时
      汇总为 ``LLM_CALL_FAILED``（details 携带链清单与末次错误码）。

工具注册面（:func:`register_look_at_tool`）：LLM 工具循环消费——
图像经 base64 字符串进入参数面（LLM 无法传二进制），执行器解码后
委托本服务；解码失败是工具错误（``ok=False``——registry 异常隔离
契约，恒不上抛）。duty 工具循环经 ``registry.list_specs()``
自动获得该工具（注册即出现——装配面语义；app 装配在
``_startup_scheduler`` duty 分支经装配网关注册）。
"""

from __future__ import annotations

import base64
from typing import Any

from loguru import logger
from pydantic import ValidationError

from packages.harness.config.model_entry import ModelRole
from packages.harness.config.model_resolver import ModelResolver, get_model_resolver
from packages.harness.config.multimodal_fallback_loader import resolve_multimodal_chain
from packages.harness.core.errors import BusinessException, ErrorCode
from packages.harness.core.time_utils import now_ms
from packages.harness.llm.gateway import LlmGateway
from packages.harness.llm.llm_models import (
    ChatMessageModel,
    ChatMessageRole,
    ImageContentPart,
    ImageContentUrlModel,
    TextContentPart,
    ToolParametersSchema,
)
from packages.harness.multimodal.mm_models import (
    LookAtFace,
    LookAtGatewayFace,
    LookAtRequestModel,
    LookAtResultModel,
    LookAtToolArgsModel,
    build_data_url,
)
from packages.harness.multimodal.mm_prompts import (
    render_look_at_charter,
    render_user_instruction,
)
from packages.harness.tools.registry import ToolRegistry
from packages.harness.tools.tool_models import ToolSpecModel

__all__ = [
    "LOOK_AT_TOOL_NAME",
    "LookAtService",
    "register_look_at_tool",
]

#: look_at 工具名（LLM 请求 tools 面标识）。
LOOK_AT_TOOL_NAME: str = "look_at"

#: look_at 工具参数 JSON Schema（与 LookAtToolArgsModel 字段对齐）。
_LOOK_AT_SCHEMA: ToolParametersSchema = ToolParametersSchema.model_validate(
    {
        "type": "object",
        "properties": {
            "image_base64": {
                "type": "string",
                "description": "图片 base64 载荷（PNG / JPEG / WebP / GIF 图片字节）",
            },
            "mime_type": {
                "type": "string",
                "description": "图片 MIME 类型（image/png 默认 / image/jpeg / image/webp / image/gif）",
                "enum": ["image/png", "image/jpeg", "image/webp", "image/gif"],
            },
            "question": {
                "type": "string",
                "description": "聚焦问题（可选，≤200 字符——不填则整体结构化描述）",
            },
        },
        "required": ["image_base64"],
        "additionalProperties": False,
    }
)


class LookAtService:
    """图像理解服务（门面——编排校验 / 消息组装 / 条目链 / 网关调用）。

    构造注入（测试 / 装配友好）：gateway / resolver 均可显式注入；
    缺省网关为裸 :class:`LlmGateway`（用量未接线——装配面经
    ``LlmGateway(usage_recorder=...)`` 显式接线，见 app/api/look_at），
    缺省解析器为 config 全局单例（调用时读取）。
    """

    def __init__(
        self,
        *,
        gateway: LookAtGatewayFace | None = None,
        resolver: ModelResolver | None = None,
    ) -> None:
        """初始化服务。

        Args:
            gateway: LLM 网关（None = 裸 LlmGateway——调用时读全局
                resolver / pools）。
            resolver: 模型解析器（None = config 全局单例，调用时读取）。
        """
        self._gateway: LookAtGatewayFace = gateway if gateway is not None else LlmGateway()
        self._resolver = resolver

    async def describe(
        self,
        image: bytes,
        mime_type: str,
        question: str | None = None,
        *,
        persona_id: str | None = None,
        request_id: str | None = None,
    ) -> LookAtResultModel:
        """理解图像（结构化中文描述 + 可选聚焦回答；回退链逐条目尝试）。

        Args:
            image: 图像字节（非空；上限 10MB——超过由调用方先行压缩）。
            mime_type: 图像 MIME 类型（png / jpeg / webp / gif）。
            question: 聚焦问题（None = 整体结构化描述；≤200 字符）。
            persona_id: 人格 ID（None = 网关读请求上下文）。
            request_id: 请求追踪 ID（None = 网关读请求上下文）。

        Returns:
            look_at 结果（description / model_entry / latency_ms——
            model_entry 为回退链中实际命中的条目）。

        Raises:
            BusinessException: 请求参数非法（``VALIDATION_ERROR``）；
                look_at 角色未绑定（``MODEL_NOT_FOUND``）；角色条目
                非多模态且回退链未配置（``CONFIG_INVALID``——config
                域回退链裁决，fail loud）；链上条目全部失败
                （``LLM_CALL_FAILED``——单条目链透传原始错误码）。
        """
        try:
            request = LookAtRequestModel(image=image, mime_type=mime_type, question=question)
        except ValidationError as exc:
            raise BusinessException(
                code=ErrorCode.VALIDATION_ERROR,
                message="look_at 请求参数非法（图像 / MIME / 聚焦问题）",
                details={"reason": str(exc)[:300]},
            ) from exc
        data_url = build_data_url(request.image, request.mime_type)
        messages: list[ChatMessageModel] = [
            ChatMessageModel(
                role=ChatMessageRole.SYSTEM,
                content=render_look_at_charter(request.question),
            ),
            ChatMessageModel(
                role=ChatMessageRole.USER,
                content=[
                    ImageContentPart(image_url=ImageContentUrlModel(url=data_url)),
                    TextContentPart(text=render_user_instruction(request.question)),
                ],
            ),
        ]
        resolver = self._resolver if self._resolver is not None else get_model_resolver()
        role_entry = resolver.resolve(ModelRole.LOOK_AT)
        chain = resolve_multimodal_chain(role_entry)
        logger.info(
            "look_at 调用开始: size_bytes={}, mime={}, chain={}",
            len(request.image),
            request.mime_type,
            [entry.name for entry in chain],
        )
        started_ms = now_ms()
        last_error: BusinessException | None = None
        for entry in chain:
            try:
                response = await self._gateway.chat_for_entry(
                    entry,
                    messages,
                    purpose="look_at",
                    persona_id=persona_id,
                    request_id=request_id,
                )
            except BusinessException as exc:
                last_error = exc
                logger.warning(
                    "look_at 条目调用失败，尝试回退链下一级: entry={}, code={}",
                    entry.name,
                    exc.code,
                )
                continue
            description = response.content.strip()
            if not description:
                last_error = BusinessException(
                    code=ErrorCode.LLM_CALL_FAILED,
                    message="look_at 响应缺少描述文本（content 为空）",
                    details={"model_entry": entry.name},
                )
                logger.warning("look_at 条目响应 content 为空: entry={}", entry.name)
                continue
            return LookAtResultModel(
                description=description,
                model_entry=entry.name,
                latency_ms=now_ms() - started_ms,
            )
        if len(chain) == 1 and last_error is not None:
            # 单条目链（多模态直连或仅一级回退）透传原始错误——熔断 /
            # 鉴权 / 传输错误码不被包裹，调用方可按码重试。
            raise last_error
        raise BusinessException(
            code=ErrorCode.LLM_CALL_FAILED,
            message="look_at 回退链全部条目失败",
            details={
                "chain": [entry.name for entry in chain],
                "last_error_code": str(last_error.code) if last_error is not None else None,
            },
        )


# =========================================================================== []
# 工具注册面（LLM 工具循环——duty 工具循环经 registry.list_specs() 消费）
# =========================================================================== []


def register_look_at_tool(registry: ToolRegistry, service: LookAtFace) -> None:
    """注册 look_at 工具（图像 base64 参数面 → 解码 → 委托服务）。

    装配语义（与 register_builtin_tools 同约定）：注册即出现在
    ``registry.list_specs()`` ——duty 工具循环自动获得；
    未注册 = 工具面不出现（模型无从调用，循环照常运行）。

    Args:
        registry: 工具注册表（装配目标）。
        service: look_at 服务（结构化协议——真实 :class:`LookAtService`
            / 测试 fake 均可）。
    """

    async def _execute(args: dict[str, Any]) -> dict[str, Any]:
        """执行（参数已经 registry 校验物化——base64 解码后委托服务）。

        Raises:
            BusinessException: base64 载荷非法（``VALIDATION_ERROR``——
                registry 归一 ok=False 工具错误，恒不上抛）；服务侧
                业务异常（链路失败等——同归一 ok=False）。
        """
        try:
            image = base64.b64decode(str(args["image_base64"]), validate=True)
        except ValueError as exc:
            raise BusinessException(
                code=ErrorCode.VALIDATION_ERROR,
                message="look_at 工具的 image_base64 不是合法 base64 载荷",
                details={"reason": str(exc)},
            ) from exc
        result = await service.describe(image, str(args["mime_type"]), args.get("question"))
        return {
            "description": result.description,
            "model_entry": result.model_entry,
            "latency_ms": result.latency_ms,
        }

    spec = ToolSpecModel(
        name=LOOK_AT_TOOL_NAME,
        description=(
            "查看并理解一张图片（PNG / JPEG / WebP / GIF），返回结构化中文描述"
            "（主体 / 场景 / 文字内容 / 图表数据 / 值得注意的细节）；可携带聚焦问题。"
        ),
        args_schema=_LOOK_AT_SCHEMA,
        args_model=LookAtToolArgsModel,
    )
    registry.register(spec, _execute)

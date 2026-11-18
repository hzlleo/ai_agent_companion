"""canvas 域 Model Classes——渲染产物（画布文档）的创建 / 读取 / 导出契约。

能力定位：**渲染 / 导出面**。智能体或操作者在管理台产出一份自包含的
渲染产物（HTML 片段 / SVG 图 / Markdown 文本），按人格作用域持久化，管理台
面板提供预览与单文件导出；导出的字节可直接作为通道 ``file`` 附件投递
（协议 v2 的 ``file`` 类，三通道均支持）。画布内容不经 LLM 渲染服务——
预览在管理台的沙箱 iframe 内完成，导出是原始文本字节。

安全边界（锁定）：预览 iframe 恒不带 ``allow-scripts``——模型生成的 HTML
里的脚本不执行（只保留结构与样式），避免生成内容在管理台上下文里产生副作用；
SVG 同样按文档渲染（内联脚本不执行）。导出文件不受此限制（由打开者决定）。

模型与逻辑分离（约束 5 / 18）：本文件只放 Model Classes 与纯校验，持久化
编排在 ``canvas_service``，ORM 行在 ``canvas_orm``。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final, Self

from pydantic import BaseModel, Field, field_validator, model_validator

__all__ = [
    "CANVAS_MAX_CONTENT_CHARS",
    "CANVAS_MAX_TITLE_CHARS",
    "CanvasKind",
    "CanvasCreateDTO",
    "CanvasUpdateDTO",
    "CanvasQueryDTO",
    "CanvasRecordDTO",
    "CanvasDeleteResultModel",
    "CanvasExportModel",
    "canvas_export_extension",
    "canvas_export_media_type",
]

#: 标题字符上限（管理台卡片标题 + 导出文件名原料）。
CANVAS_MAX_TITLE_CHARS: Final[int] = 120

#: 内容字符上限（自包含渲染产物——256K 字符足以覆盖单页 HTML / SVG）。
CANVAS_MAX_CONTENT_CHARS: Final[int] = 262144

#: 导出 MIME（按类型固定——导出端点 Content-Type 的唯一来源）。
_EXPORT_MEDIA_TYPES: Final[dict[str, str]] = {
    "html": "text/html; charset=utf-8",
    "svg": "image/svg+xml; charset=utf-8",
    "markdown": "text/markdown; charset=utf-8",
}

#: 导出文件扩展名（单文件下载命名）。
_EXPORT_EXTENSIONS: Final[dict[str, str]] = {
    "html": ".html",
    "svg": ".svg",
    "markdown": ".md",
}


class CanvasKind(StrEnum):
    """画布产物类型值域（html / svg / markdown）。

    继承 ``StrEnum`` 保持与字符串比较兼容（与 ``ChannelType`` 同约定）。
    类型决定管理台预览的渲染方式（iframe 文档 / SVG 文档 / 纯文本）与导出
    的 MIME 与扩展名。
    """

    HTML = "html"
    SVG = "svg"
    MARKDOWN = "markdown"


def canvas_export_media_type(kind: CanvasKind) -> str:
    """读取画布类型的导出 MIME。

    Args:
        kind: 画布类型。

    Returns:
        Content-Type 字符串（含 charset——文本类产物统一 UTF-8）。
    """
    return _EXPORT_MEDIA_TYPES[kind.value]


def canvas_export_extension(kind: CanvasKind) -> str:
    """读取画布类型的导出扩展名。

    Args:
        kind: 画布类型。

    Returns:
        文件扩展名（含点号）。
    """
    return _EXPORT_EXTENSIONS[kind.value]


class CanvasCreateDTO(BaseModel):
    """创建画布请求（标题 / 类型 / 自包含内容）。

    Attributes:
        title: 标题（1-120 字符，去首尾空白后非空）。
        kind: 产物类型（html / svg / markdown）。
        content: 自包含内容（1-262144 字符，不得为纯空白——HTML fragment /
            SVG 文档 / Markdown 文本）。
    """

    title: str = Field(max_length=CANVAS_MAX_TITLE_CHARS, description="标题")
    kind: CanvasKind = Field(description="产物类型")
    content: str = Field(
        max_length=CANVAS_MAX_CONTENT_CHARS,
        description="自包含内容（HTML / SVG / Markdown）",
    )

    @field_validator("title")
    @classmethod
    def _normalize_title(cls, value: str) -> str:
        """裁剪标题并拒绝纯空白（空白标题无法在管理台辨识）。

        Args:
            value: 原始标题。

        Returns:
            裁剪后的标题。

        Raises:
            ValueError: 标题为纯空白。
        """
        title = value.strip()
        if not title:
            raise ValueError("画布标题不得为纯空白")
        return title

    @field_validator("content")
    @classmethod
    def _require_content(cls, value: str) -> str:
        """拒绝纯空白内容（空白产物无法预览 / 导出，且几乎总是构造错误）。

        Args:
            value: 原始内容。

        Returns:
            原内容（不裁剪——HTML / SVG 的前导空白是内容的一部分）。

        Raises:
            ValueError: 内容为纯空白。
        """
        if not value.strip():
            raise ValueError("画布内容不得为纯空白")
        return value


class CanvasUpdateDTO(BaseModel):
    """更新画布请求（标题 / 内容按需修改——类型创建后固定）。

    Attributes:
        title: 新标题（None = 不修改）。
        content: 新内容（None = 不修改）。
    """

    title: str | None = Field(
        default=None, max_length=CANVAS_MAX_TITLE_CHARS, description="新标题（None = 不修改）"
    )
    content: str | None = Field(
        default=None,
        max_length=CANVAS_MAX_CONTENT_CHARS,
        description="新内容（None = 不修改）",
    )

    @field_validator("title")
    @classmethod
    def _normalize_title(cls, value: str | None) -> str | None:
        """裁剪标题并拒绝纯空白（None = 不修改）。

        Args:
            value: 原始标题（None = 不修改）。

        Returns:
            裁剪后的标题或 None。

        Raises:
            ValueError: 提供了标题但为纯空白。
        """
        if value is None:
            return None
        title = value.strip()
        if not title:
            raise ValueError("画布标题不得为纯空白")
        return title

    @field_validator("content")
    @classmethod
    def _require_content(cls, value: str | None) -> str | None:
        """拒绝纯空白内容（None = 不修改）。

        Args:
            value: 原始内容（None = 不修改）。

        Returns:
            原内容或 None。

        Raises:
            ValueError: 提供了内容但为纯空白。
        """
        if value is None:
            return None
        if not value.strip():
            raise ValueError("画布内容不得为纯空白")
        return value

    @model_validator(mode="after")
    def _require_some_change(self) -> Self:
        """校验更新面至少提供一项（空对象是无意义请求）。

        Returns:
            校验通过的自身。

        Raises:
            ValueError: 标题与内容两项皆缺。
        """
        if self.title is None and self.content is None:
            raise ValueError("更新画布至少需要提供 title 或 content 之一")
        return self


class CanvasQueryDTO(BaseModel):
    """画布列表查询（类型过滤 + 条数上限）。

    Attributes:
        kind: 产物类型过滤（None = 全部）。
        limit: 返回条数上限（1-200）。
    """

    kind: CanvasKind | None = Field(default=None, description="产物类型过滤（None = 全部）")
    limit: int = Field(default=100, ge=1, le=200, description="返回条数上限（1-200）")


class CanvasRecordDTO(BaseModel):
    """画布读取形态（管理台列表 / 详情 / 导出前置）。

    Attributes:
        id: 主键。
        title: 标题。
        kind: 产物类型。
        content: 自包含内容（列表端点同样携带——产物规模受 256K 上限约束，
            单次列表可承载）。
        revision: 修订号（创建为 1，每次内容 / 标题修改递增）。
        created_at_ms: 创建时间（epoch 毫秒）。
        updated_at_ms: 更新时间（epoch 毫秒）。
    """

    id: int
    title: str
    kind: CanvasKind
    content: str
    revision: int = Field(ge=1, description="修订号（创建为 1，修改递增）")
    created_at_ms: int = Field(ge=0, description="创建时间（epoch 毫秒）")
    updated_at_ms: int = Field(ge=0, description="更新时间（epoch 毫秒）")


class CanvasDeleteResultModel(BaseModel):
    """删除结果（缺失主键幂等报告——与表情包批量删除同语义）。

    Attributes:
        deleted_ids: 实际删除的主键列表（空 = 目标不存在或不属于当前人格）。
    """

    deleted_ids: list[int] = Field(default_factory=list, description="实际删除的主键列表")


class CanvasExportModel(BaseModel):
    """导出产物（文件名 / MIME / 原始内容——路由层直出字节）。

    Attributes:
        filename: 下载文件名（``canvas-<id>-r<revision><扩展名>``）。
        media_type: Content-Type（按类型固定）。
        content: 原始产物文本（UTF-8 编码后即导出字节）。
    """

    filename: str = Field(min_length=1, description="下载文件名")
    media_type: str = Field(min_length=1, description="Content-Type")
    content: str = Field(description="原始产物文本")

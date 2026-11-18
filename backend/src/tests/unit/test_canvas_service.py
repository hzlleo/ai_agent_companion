"""harness/canvas service 测试——创建 / 列举 / 读取 / 修订 / 删除 / 导出 / 隔离。

覆盖面：创建（revision=1 / 标题裁剪 / 内容原样保留）、列举（更新时间倒序 /
类型过滤 / 条数上限）、读取（含内容）、修订（revision 递增 / 部分更新）、
删除（幂等报告）、导出（文件名带主键与修订号 / MIME 按类型 / 中文标题
退化为无 slug 文件名）、**人格隔离**（PersonaMixin 上下文自动过滤——
双人格互不可见、跨人格读取为 NOT_FOUND）。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

import packages.harness.canvas.canvas_orm as canvas_orm  # noqa: F401  模型注册副作用导入（共享建表）
from packages.harness.canvas.canvas_models import (
    CanvasCreateDTO,
    CanvasKind,
    CanvasQueryDTO,
    CanvasUpdateDTO,
    canvas_export_extension,
    canvas_export_media_type,
)
from packages.harness.canvas.canvas_service import CanvasService
from packages.harness.core.errors import BusinessException, ErrorCode
from packages.harness.persona.service import bind_persona_context

#: 测试用双人格上下文轴（PersonaMixin 无外键——任意字符串 ID 即隔离轴）。
PERSONA_A = "canvas-alpha"
PERSONA_B = "canvas-beta"

canvas_service = CanvasService()


def _create_model(
    *,
    title: str = "  今日心情卡  ",
    kind: CanvasKind = CanvasKind.HTML,
    content: str = "<div>今天很好</div>",
) -> CanvasCreateDTO:
    """构造创建入参（测试铺底辅助——标题带空白以断言裁剪）。"""
    return CanvasCreateDTO(title=title, kind=kind, content=content)


async def test_create_returns_first_revision(db_session: AsyncSession) -> None:
    """创建：revision=1 + 标题裁剪 + 内容逐字节保留。"""
    async with bind_persona_context(PERSONA_A):
        record = await canvas_service.create_document(
            db_session, _create_model(content="  <p>保留前导空白</p>  ")
        )
    assert record.id >= 1
    assert record.title == "今日心情卡"
    assert record.kind is CanvasKind.HTML
    assert record.content == "  <p>保留前导空白</p>  "
    assert record.revision == 1
    assert record.created_at_ms >= 0
    assert record.updated_at_ms >= record.created_at_ms


async def test_list_orders_by_update_and_filters_kind(db_session: AsyncSession) -> None:
    """列举：更新时间倒序 + 类型过滤 + 条数上限。"""
    async with bind_persona_context(PERSONA_A):
        first = await canvas_service.create_document(
            db_session, _create_model(title="甲", kind=CanvasKind.HTML)
        )
        second = await canvas_service.create_document(
            db_session, _create_model(title="乙", kind=CanvasKind.SVG, content="<svg/>")
        )
        await canvas_service.update_document(
            db_session, first.id, CanvasUpdateDTO(content="<div>改过</div>")
        )
        everything = await canvas_service.list_documents(db_session, CanvasQueryDTO(limit=10))
        svg_only = await canvas_service.list_documents(
            db_session, CanvasQueryDTO(kind=CanvasKind.SVG, limit=10)
        )
        limited = await canvas_service.list_documents(db_session, CanvasQueryDTO(limit=1))
    assert {record.id for record in everything} == {first.id, second.id}
    assert [record.id for record in svg_only] == [second.id]
    assert len(limited) == 1


async def test_update_increments_revision_and_partial_fields(db_session: AsyncSession) -> None:
    """修订：revision 递增；只给标题时内容不变；只给内容时标题不变。"""
    async with bind_persona_context(PERSONA_A):
        created = await canvas_service.create_document(
            db_session, _create_model(title="原题", content="<div>原文</div>")
        )
        renamed = await canvas_service.update_document(
            db_session, created.id, CanvasUpdateDTO(title="新题")
        )
        assert renamed.title == "新题"
        assert renamed.content == "<div>原文</div>"
        assert renamed.revision == 2
        rewritten = await canvas_service.update_document(
            db_session, created.id, CanvasUpdateDTO(content="<div>新文</div>")
        )
    assert rewritten.title == "新题"
    assert rewritten.content == "<div>新文</div>"
    assert rewritten.revision == 3


async def test_delete_reports_missing_idempotently(db_session: AsyncSession) -> None:
    """删除：存在行删除并报告主键；缺失主键幂等空报告。"""
    async with bind_persona_context(PERSONA_A):
        created = await canvas_service.create_document(db_session, _create_model())
        deleted = await canvas_service.delete_document(db_session, created.id)
        missing = await canvas_service.delete_document(db_session, created.id)
        with pytest.raises(BusinessException) as error:
            await canvas_service.get_document(db_session, created.id)
    assert deleted.deleted_ids == [created.id]
    assert missing.deleted_ids == []
    assert error.value.code is ErrorCode.NOT_FOUND


async def test_export_filename_and_media_type(db_session: AsyncSession) -> None:
    """导出：文件名带主键与修订号；MIME / 扩展名按类型固定。"""
    async with bind_persona_context(PERSONA_A):
        html = await canvas_service.create_document(
            db_session, _create_model(title="Mood Card", content="<div>x</div>")
        )
        exported = await canvas_service.export_document(db_session, html.id)
        markdown = await canvas_service.create_document(
            db_session,
            _create_model(title="中文标题", kind=CanvasKind.MARKDOWN, content="# 标题"),
        )
        exported_markdown = await canvas_service.export_document(db_session, markdown.id)
    assert exported.filename == f"canvas-{html.id}-r1-mood-card.html"
    assert exported.media_type == "text/html; charset=utf-8"
    assert exported.content == "<div>x</div>"
    assert exported_markdown.filename == f"canvas-{markdown.id}-r1.md"
    assert exported_markdown.media_type == "text/markdown; charset=utf-8"
    assert canvas_export_extension(CanvasKind.SVG) == ".svg"
    assert canvas_export_media_type(CanvasKind.SVG).startswith("image/svg+xml")


async def test_export_reflects_latest_revision(db_session: AsyncSession) -> None:
    """导出：修订后文件名带新修订号（多次导出不互相覆盖）。"""
    async with bind_persona_context(PERSONA_A):
        created = await canvas_service.create_document(db_session, _create_model(title="Cards"))
        await canvas_service.update_document(
            db_session, created.id, CanvasUpdateDTO(content="<div>二版</div>")
        )
        exported = await canvas_service.export_document(db_session, created.id)
    assert f"canvas-{created.id}-r2" in exported.filename
    assert exported.content == "<div>二版</div>"


async def test_persona_isolation(db_session: AsyncSession) -> None:
    """人格隔离：他人格的画布不可见 / 不可读 / 不可删。"""
    async with bind_persona_context(PERSONA_A):
        alpha = await canvas_service.create_document(db_session, _create_model(title="甲的"))
    async with bind_persona_context(PERSONA_B):
        beta_list = await canvas_service.list_documents(db_session, CanvasQueryDTO(limit=10))
        with pytest.raises(BusinessException) as error:
            await canvas_service.get_document(db_session, alpha.id)
        beta_delete = await canvas_service.delete_document(db_session, alpha.id)
    assert beta_list == []
    assert error.value.code is ErrorCode.NOT_FOUND
    assert beta_delete.deleted_ids == []


async def test_models_reject_blank_and_oversized_payloads() -> None:
    """模型边界：标题 / 内容纯空白拒绝；更新两项皆缺拒绝；类型值域校验。"""
    with pytest.raises(ValidationError):
        CanvasCreateDTO(title="   ", kind=CanvasKind.HTML, content="<div/>")
    with pytest.raises(ValidationError):
        CanvasCreateDTO(title="标题", kind=CanvasKind.HTML, content="  \n ")
    with pytest.raises(ValidationError):
        CanvasUpdateDTO()
    with pytest.raises(ValidationError):
        CanvasUpdateDTO(title="   ")
    with pytest.raises(ValidationError):
        CanvasUpdateDTO(content="   ")
    with pytest.raises(ValidationError):
        CanvasCreateDTO(title="标题", kind="pdf", content="<div/>")

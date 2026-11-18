"""canvas 域服务——画布产物的创建 / 列举 / 读取 / 修订 / 删除 / 导出。

职责链::

    create_document(session, dto)          新建（revision=1）
    list_documents(session, query)         列举（类型过滤——更新时间倒序）
    get_document(session, canvas_id)       按主键读取（人格作用域外 NOT_FOUND）
    update_document(session, id, dto)      修订（标题 / 内容；revision 递增）
    delete_document(session, id)           删除（缺失主键幂等报告）
    export_document(session, id)           导出（文件名 / MIME / 原始文本）

人格作用域：全部写入靠 ``PersonaMixin`` 从上下文注入 ``persona_id``，读取由
ORM 层自动附加当前人格过滤——因此「他人格的画布」与「不存在」同为
``NOT_FOUND``（与提醒 / 表情包面同隔离语义）。API 层负责先解析目标人格并
绑定上下文。

内容边界：模型层已校验标题与内容（1-262144 字符、非纯空白）；服务层不再
重复裁剪内容（HTML / SVG 的前导空白是内容的一部分），标题裁剪由模型层完成。
"""

from __future__ import annotations

import re
from typing import Final

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.harness.canvas.canvas_models import (
    CanvasCreateDTO,
    CanvasDeleteResultModel,
    CanvasExportModel,
    CanvasKind,
    CanvasQueryDTO,
    CanvasRecordDTO,
    CanvasUpdateDTO,
    canvas_export_extension,
    canvas_export_media_type,
)
from packages.harness.canvas.canvas_orm import CanvasDocumentRow
from packages.harness.core.errors import BusinessException, ErrorCode

__all__ = ["CanvasService"]

#: 导出文件名 slug 的 ASCII 安全字符集（中文标题退化为 ``canvas``）。
_SLUG_PATTERN: Final[re.Pattern[str]] = re.compile(r"[^a-z0-9]+")


def _canvas_record(row: CanvasDocumentRow) -> CanvasRecordDTO:
    """ORM 行 → 记录 DTO（字段直映——类型枚举在 DTO 边界校验）。

    Args:
        row: 画布行。

    Returns:
        画布记录 DTO。
    """
    return CanvasRecordDTO(
        id=row.id,
        title=row.title,
        kind=CanvasKind(row.kind),
        content=row.content,
        revision=row.revision,
        created_at_ms=row.created_at,
        updated_at_ms=row.updated_at,
    )


def _export_filename(row: CanvasDocumentRow) -> str:
    """构造导出文件名（``canvas-<id>-r<revision>[-<slug>]<扩展名>``）。

    Args:
        row: 画布行。

    Returns:
        下载文件名（标题中非 ASCII 字符剔除后为空则省略 slug 段——中文标题
        不会产生乱码文件名）。
    """
    slug = _SLUG_PATTERN.sub("-", row.title.lower()).strip("-")
    suffix = f"-{slug}" if slug else ""
    return f"canvas-{row.id}-r{row.revision}{suffix}{canvas_export_extension(CanvasKind(row.kind))}"


class CanvasService:
    """画布服务（无状态——Session 逐请求注入，服务可安全共享）。"""

    async def create_document(
        self, session: AsyncSession, payload: CanvasCreateDTO
    ) -> CanvasRecordDTO:
        """创建画布产物（revision=1）。

        Args:
            session: 注入的异步 Session（人格上下文已绑定）。
            payload: 创建入参（模型层已校验并裁剪标题）。

        Returns:
            创建后的记录 DTO。
        """
        row = CanvasDocumentRow(
            title=payload.title,
            kind=payload.kind.value,
            content=payload.content,
            revision=1,
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)
        logger.info(
            "画布已创建: id={}, kind={}, revision={}, chars={}",
            row.id,
            row.kind,
            row.revision,
            len(row.content),
        )
        return _canvas_record(row)

    async def list_documents(
        self, session: AsyncSession, query: CanvasQueryDTO
    ) -> list[CanvasRecordDTO]:
        """列举当前人格的画布产物（更新时间倒序；可选类型过滤）。

        Args:
            session: 注入的异步 Session（人格上下文已绑定）。
            query: 查询入参（类型过滤 + 条数上限）。

        Returns:
            画布记录列表（最近修改在前）。
        """
        stmt = select(CanvasDocumentRow).order_by(
            CanvasDocumentRow.updated_at.desc(), CanvasDocumentRow.id.desc()
        )
        if query.kind is not None:
            stmt = stmt.where(CanvasDocumentRow.kind == query.kind.value)
        stmt = stmt.limit(query.limit)
        rows = (await session.execute(stmt)).scalars().all()
        return [_canvas_record(row) for row in rows]

    async def get_document(self, session: AsyncSession, canvas_id: int) -> CanvasRecordDTO:
        """按主键读取画布（人格作用域外 / 缺失均 NOT_FOUND）。

        Args:
            session: 注入的异步 Session（人格上下文已绑定）。
            canvas_id: 画布主键。

        Returns:
            画布记录 DTO。

        Raises:
            BusinessException: 画布不存在（``NOT_FOUND``）。
        """
        return _canvas_record(await self._require(session, canvas_id))

    async def update_document(
        self, session: AsyncSession, canvas_id: int, payload: CanvasUpdateDTO
    ) -> CanvasRecordDTO:
        """修订画布（标题 / 内容按需修改；revision 递增）。

        Args:
            session: 注入的异步 Session（人格上下文已绑定）。
            canvas_id: 画布主键。
            payload: 更新入参（模型层已校验——至少一项且有值）。

        Returns:
            修订后的记录 DTO。

        Raises:
            BusinessException: 画布不存在（``NOT_FOUND``）。
        """
        row = await self._require(session, canvas_id)
        if payload.title is not None:
            row.title = payload.title
        if payload.content is not None:
            row.content = payload.content
        row.revision += 1
        await session.commit()
        await session.refresh(row)
        logger.info(
            "画布已修订: id={}, revision={}, chars={}",
            row.id,
            row.revision,
            len(row.content),
        )
        return _canvas_record(row)

    async def delete_document(
        self, session: AsyncSession, canvas_id: int
    ) -> CanvasDeleteResultModel:
        """删除画布（缺失主键幂等——deleted_ids 为空报告）。

        Args:
            session: 注入的异步 Session（人格上下文已绑定）。
            canvas_id: 画布主键。

        Returns:
            删除结果（实际删除的主键列表）。
        """
        row = (
            await session.execute(select(CanvasDocumentRow).where(CanvasDocumentRow.id == canvas_id))
        ).scalar_one_or_none()
        if row is None:
            logger.info("画布删除目标不存在（幂等报告）: id={}", canvas_id)
            return CanvasDeleteResultModel(deleted_ids=[])
        await session.delete(row)
        await session.commit()
        logger.info("画布已删除: id={}", canvas_id)
        return CanvasDeleteResultModel(deleted_ids=[canvas_id])

    async def export_document(self, session: AsyncSession, canvas_id: int) -> CanvasExportModel:
        """导出画布（文件名 / MIME / 原始文本——路由层直出字节）。

        Args:
            session: 注入的异步 Session（人格上下文已绑定）。
            canvas_id: 画布主键。

        Returns:
            导出产物（文件名带主键与修订号——多次导出不互相覆盖）。

        Raises:
            BusinessException: 画布不存在（``NOT_FOUND``）。
        """
        row = await self._require(session, canvas_id)
        kind = CanvasKind(row.kind)
        return CanvasExportModel(
            filename=_export_filename(row),
            media_type=canvas_export_media_type(kind),
            content=row.content,
        )

    async def _require(self, session: AsyncSession, canvas_id: int) -> CanvasDocumentRow:
        """按主键取行（人格作用域外 / 缺失均 NOT_FOUND——隔离语义）。

        Args:
            session: 注入的异步 Session（人格上下文已绑定）。
            canvas_id: 画布主键。

        Returns:
            画布行。

        Raises:
            BusinessException: 画布不存在（``NOT_FOUND``）。
        """
        row = (
            await session.execute(select(CanvasDocumentRow).where(CanvasDocumentRow.id == canvas_id))
        ).scalar_one_or_none()
        if row is None:
            raise BusinessException(
                code=ErrorCode.NOT_FOUND,
                message="画布不存在（或不属于当前人格）",
                details={"id": canvas_id},
            )
        return row

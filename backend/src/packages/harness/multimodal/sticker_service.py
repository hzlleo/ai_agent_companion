"""multimodal 表情包服务——上传 / 去重 / 标签 / 删除 / VLM 标注 / 通道桥。

职责链（管理 API 与通道表情收发桥共用同一服务）::

    upload_batch(session, dto)             批量上传（类型 / 大小 / 来源备注 /
                                           标签校验 → 哈希去重 → 落盘落库）
    list_stickers / get_sticker            列表（标签 / 标注状态过滤）与单条读取
    update_sticker                         标签 / 描述 / 来源备注修改
    delete_sticker / delete_batch          删除（行 + 文件；缺失主键幂等报告）
    annotate(session, sticker_id)          VLM 标注（视觉 role 回退链 → JSON
                                           解析 → 描述 / 标签写入；失败落
                                           failed 状态且可重试）
    ingest_bytes / pick_channel_reply      通道附件 v2 表情收发桥（入站收录 /
                                           出站取最新素材）

法律边界（锁定设计）：``source_note`` 是所有入库路径的硬前置（管理面上传
由请求模型强制非空；通道自动收录由桥生成「通道来源」备注）。库不做版权
推断，只保证来源备注随记录可读。

标注语义：复用 look_at 的视觉角色条目与多模态回退链（``resolve_multimodal_chain``）+
公共池网关；用例条目全部失败时写 ``annotated_error`` 并保持记录可读（管理面
可重试），不向调用方抛依赖失败（与「标注失败可重试并落错误状态」一致）。
文件 I/O 一律经 ``run_blocking_ops`` 下沉线程池；写入原子换名。
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any, Final

from loguru import logger
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.harness.config.model_entry import ModelRole
from packages.harness.config.model_resolver import ModelResolver, get_model_resolver
from packages.harness.config.models import settings as global_settings
from packages.harness.config.multimodal_fallback_loader import resolve_multimodal_chain
from packages.harness.core import time_utils
from packages.harness.core.context import persona_id_var
from packages.harness.core.errors import BusinessException, ErrorCode
from packages.harness.core.file_utils import unlink_missing_ok, write_bytes_atomic
from packages.harness.core.thread_pool import run_blocking_ops
from packages.harness.llm.gateway import LlmGateway
from packages.harness.llm.llm_models import (
    ChatMessageModel,
    ChatMessageRole,
    ImageContentPart,
    ImageContentUrlModel,
    TextContentPart,
)
from packages.harness.multimodal.mm_models import build_data_url
from packages.harness.multimodal.sticker_models import (
    STICKER_IMAGE_MIME_ALLOWLIST,
    StickerAnnotateGatewayFace,
    StickerAnnotationResultModel,
    StickerAnnotationStatus,
    StickerBatchDeleteRequest,
    StickerBatchDeleteResultModel,
    StickerBatchUploadDTO,
    StickerBatchUploadResultModel,
    StickerBinaryModel,
    StickerQueryDTO,
    StickerRecordDTO,
    StickerRejectedItemModel,
    StickerRejectReason,
    StickerSettings,
    StickerUpdateModel,
    StickerUploadItemModel,
)
from packages.harness.multimodal.sticker_orm import StickerRow
from packages.harness.multimodal.sticker_prompts import (
    render_sticker_annotation_charter,
    render_sticker_annotation_instruction,
)

__all__ = ["STICKER_MIME_EXTENSIONS", "StickerService"]

#: MIME → 文件扩展名（落盘命名与内容类型保持一一对应）。
STICKER_MIME_EXTENSIONS: Final[dict[str, str]] = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
}

#: 标注 JSON 允许出现的代码围栏（模型偶发包裹 ```json）。
_FENCE_PATTERN: Final[re.Pattern[str]] = re.compile(r"^```(?:json)?\s*|\s*```$")


class StickerService:
    """表情包服务（上传 / 读取 / 修改 / 删除 / 标注 / 通道桥）。

    构造注入（测试 / 装配友好）：settings / gateway / resolver / storage_root
    / clock 均可显式覆盖；缺省 settings 读全局单例、网关为裸
    :class:`LlmGateway`、解析器为 config 全局单例。服务自身无状态（Session
    逐次注入），可安全跨请求共享。
    """

    def __init__(
        self,
        *,
        settings: StickerSettings | None = None,
        gateway: StickerAnnotateGatewayFace | None = None,
        resolver: ModelResolver | None = None,
        storage_root: Path | None = None,
        clock: Callable[[], int] = time_utils.now_ms,
    ) -> None:
        """初始化服务。

        Args:
            settings: 表情包配置（None = 全局 settings.sticker）。
            gateway: 视觉标注网关（None = 裸 LlmGateway——调用时读全局
                resolver / pools）。
            resolver: 模型解析器（None = config 全局单例，调用时读取）。
            storage_root: 文件落盘根目录（None = settings.storage_dir）。
            clock: 时钟（epoch 毫秒——标注时间轴）。
        """
        self._settings = settings
        self._gateway: StickerAnnotateGatewayFace = (
            gateway if gateway is not None else LlmGateway()
        )
        self._resolver = resolver
        self._storage_root = storage_root
        self._clock = clock

    # ------------------------------------------------------------------
    # 上传面
    # ------------------------------------------------------------------

    async def upload_batch(
        self, session: AsyncSession, dto: StickerBatchUploadDTO
    ) -> StickerBatchUploadResultModel:
        """批量上传表情包（逐条目部分成功；哈希去重；来源备注强制）。

        Args:
            session: 注入的异步 Session（人格上下文已绑定）。
            dto: 批量上传入参。

        Returns:
            上传结果（成功记录 + 被拒条目）。

        Raises:
            BusinessException: 功能未启用（``CONFLICT``）；批量条数超过
                配置上限（``VALIDATION_ERROR``）。
        """
        settings = self._require_enabled()
        if len(dto.items) > settings.max_batch_items:
            raise BusinessException(
                code=ErrorCode.VALIDATION_ERROR,
                message=f"单次表情包上传最多 {settings.max_batch_items} 条",
                details={
                    "provided": len(dto.items),
                    "max_batch_items": settings.max_batch_items,
                },
            )
        existing_hashes = set(
            (await session.execute(select(StickerRow.sha256))).scalars().all()
        )
        created: list[StickerRecordDTO] = []
        rejected: list[StickerRejectedItemModel] = []
        written_paths: list[Path] = []
        try:
            for index, item in enumerate(dto.items):
                prepared = self._prepare_upload(item, settings)
                if isinstance(prepared, StickerRejectedItemModel):
                    rejected.append(prepared.model_copy(update={"index": index}))
                    continue
                payload, content_type, tags, description, source_note = prepared
                digest = hashlib.sha256(payload).hexdigest()
                if digest in existing_hashes:
                    rejected.append(
                        StickerRejectedItemModel(
                            index=index,
                            reason=StickerRejectReason.DUPLICATE,
                            message="同一人格下已存在相同文件（按哈希去重）",
                        )
                    )
                    continue
                extension = STICKER_MIME_EXTENSIONS[content_type]
                path = await self._write_file(digest, extension, payload)
                written_paths.append(path)
                row = StickerRow(
                    sha256=digest,
                    content_type=content_type,
                    storage_path=str(path),
                    size_bytes=len(payload),
                    tags=json.dumps(tags, ensure_ascii=False),
                    description=description,
                    source_note=source_note,
                    annotation_status=StickerAnnotationStatus.PENDING.value,
                    annotation_error=None,
                    annotation_model_entry=None,
                    annotated_at_ms=None,
                )
                session.add(row)
                await session.flush()
                existing_hashes.add(digest)
                created.append(self._record(row))
            await self._commit(session)
        except Exception:
            await self._rollback_quietly(session)
            await self._remove_files(written_paths)
            raise
        logger.info(
            "表情包批量上传完成: created={}, rejected={}", len(created), len(rejected)
        )
        return StickerBatchUploadResultModel(created=created, rejected=rejected)

    async def ingest_bytes(
        self,
        session: AsyncSession,
        payload: bytes,
        content_type: str,
        *,
        source_note: str,
    ) -> StickerRecordDTO | None:
        """通道入站收录一张表情（哈希去重；重复返回既有记录）。

        Args:
            session: 注入的异步 Session（人格上下文已绑定）。
            payload: 文件字节。
            content_type: 图片 MIME 类型。
            source_note: 来源备注（通道桥生成——过长截断到配置上限）。

        Returns:
            入库或既有的记录；功能关闭 / 类型或大小非法时返回 None（通道
            桥不因单张表情失败而阻断消息回复）。
        """
        settings = self._resolve_settings()
        if not settings.enabled:
            return None
        normalized_type = content_type.strip().lower()
        if normalized_type not in STICKER_IMAGE_MIME_ALLOWLIST or not payload:
            return None
        if len(payload) > settings.max_upload_bytes:
            return None
        digest = hashlib.sha256(payload).hexdigest()
        existing = (
            (
                await session.execute(
                    select(StickerRow).where(StickerRow.sha256 == digest)
                )
            )
            .scalars()
            .first()
        )
        if existing is not None:
            return self._record(existing)
        note = source_note.strip()[: settings.source_note_max_length] or "通道入站表情"
        extension = STICKER_MIME_EXTENSIONS[normalized_type]
        path = await self._write_file(digest, extension, payload)
        row = StickerRow(
            sha256=digest,
            content_type=normalized_type,
            storage_path=str(path),
            size_bytes=len(payload),
            tags=json.dumps([], ensure_ascii=False),
            description="",
            source_note=note,
            annotation_status=StickerAnnotationStatus.PENDING.value,
            annotation_error=None,
            annotation_model_entry=None,
            annotated_at_ms=None,
        )
        session.add(row)
        try:
            await session.flush()
            await self._commit(session)
        except Exception:
            await self._rollback_quietly(session)
            await self._remove_files([path])
            raise
        return self._record(row)

    # ------------------------------------------------------------------
    # 读取 / 修改 / 删除
    # ------------------------------------------------------------------

    async def list_stickers(
        self, session: AsyncSession, query: StickerQueryDTO
    ) -> list[StickerRecordDTO]:
        """读取表情包列表（标签 / 标注状态过滤；可内联文件数据）。

        Args:
            session: 注入的异步 Session（人格上下文已绑定）。
            query: 查询条件。

        Returns:
            记录列表（新入库在前；命中条数按 ``limit`` 截断）。
        """
        self._require_enabled()
        stmt = select(StickerRow).order_by(desc(StickerRow.id))
        if query.annotation_status is not None:
            stmt = stmt.where(
                StickerRow.annotation_status == query.annotation_status.value
            )
        rows = list((await session.execute(stmt)).scalars().all())
        tag = query.tag.strip() if query.tag is not None else None
        matches: list[StickerRow] = []
        for row in rows:
            tags = _load_tags(row.tags)
            if tag is not None and tag not in tags:
                continue
            matches.append(row)
            if len(matches) >= query.limit:
                break
        return [
            await self._record_with_optional_data(row, include_data=query.include_data)
            for row in matches
        ]

    async def get_sticker(
        self, session: AsyncSession, sticker_id: int, *, include_data: bool = False
    ) -> StickerRecordDTO:
        """读取单条表情包（缺失即 ``NOT_FOUND``）。

        Args:
            session: 注入的异步 Session（人格上下文已绑定）。
            sticker_id: 表情包主键。
            include_data: 是否内联文件 base64。

        Returns:
            表情包记录。

        Raises:
            BusinessException: 记录不存在（``NOT_FOUND``）。
        """
        self._require_enabled()
        row = await self._require_row(session, sticker_id)
        return await self._record_with_optional_data(row, include_data=include_data)

    async def get_binary(
        self, session: AsyncSession, sticker_id: int
    ) -> StickerBinaryModel:
        """读取表情包字节（标注 / 通道发送面）。

        Args:
            session: 注入的异步 Session（人格上下文已绑定）。
            sticker_id: 表情包主键。

        Returns:
            记录 + 文件字节。

        Raises:
            BusinessException: 记录或文件不存在（``NOT_FOUND``）。
        """
        self._require_enabled()
        row = await self._require_row(session, sticker_id)
        content = await self._read_file(row.storage_path)
        if content is None:
            raise BusinessException(
                code=ErrorCode.NOT_FOUND,
                message="表情包文件缺失（记录存在但落盘文件已不在）",
                details={"sticker_id": sticker_id, "storage_path": row.storage_path},
            )
        return StickerBinaryModel(record=self._record(row), content=content)

    async def update_sticker(
        self, session: AsyncSession, sticker_id: int, dto: StickerUpdateModel
    ) -> StickerRecordDTO:
        """修改标签 / 描述 / 来源备注。

        Args:
            session: 注入的异步 Session（人格上下文已绑定）。
            sticker_id: 表情包主键。
            dto: 修改入参（至少一项）。

        Returns:
            修改后的记录。

        Raises:
            BusinessException: 记录不存在（``NOT_FOUND``）；标签非法或来源
                备注为空白（``VALIDATION_ERROR``）。
        """
        settings = self._require_enabled()
        row = await self._require_row(session, sticker_id)
        if dto.tags is not None:
            tags, error = _normalize_tags(dto.tags, settings)
            if tags is None:
                raise BusinessException(
                    code=ErrorCode.VALIDATION_ERROR,
                    message=error or "标签非法",
                    details={"sticker_id": sticker_id},
                )
            row.tags = json.dumps(tags, ensure_ascii=False)
        if dto.description is not None:
            description = dto.description.strip()
            if len(description) > settings.description_max_length:
                raise BusinessException(
                    code=ErrorCode.VALIDATION_ERROR,
                    message=f"描述不能超过 {settings.description_max_length} 个字符",
                    details={"length": len(description)},
                )
            row.description = description
        if dto.source_note is not None:
            note = dto.source_note.strip()
            if not note:
                raise BusinessException(
                    code=ErrorCode.VALIDATION_ERROR,
                    message="来源 / 授权备注不能为空",
                    details={"sticker_id": sticker_id},
                )
            row.source_note = note[: settings.source_note_max_length]
        await self._commit(session)
        return self._record(row)

    async def delete_sticker(
        self, session: AsyncSession, sticker_id: int
    ) -> StickerBatchDeleteResultModel:
        """删除单条表情包（行 + 文件；缺失主键幂等报告）。

        Args:
            session: 注入的异步 Session（人格上下文已绑定）。
            sticker_id: 表情包主键。

        Returns:
            删除结果（缺失时 ``missing_ids`` 携带该主键）。
        """
        self._require_enabled()
        return await self.delete_batch(session, StickerBatchDeleteRequest(ids=[sticker_id]))

    async def delete_batch(
        self, session: AsyncSession, dto: StickerBatchDeleteRequest
    ) -> StickerBatchDeleteResultModel:
        """批量删除表情包（行 + 文件；不存在的主键进入 missing 列表）。

        Args:
            session: 注入的异步 Session（人格上下文已绑定）。
            dto: 批量删除入参。

        Returns:
            删除结果（部分成功语义）。
        """
        self._require_enabled()
        rows = list(
            (
                await session.execute(select(StickerRow).where(StickerRow.id.in_(dto.ids)))
            )
            .scalars()
            .all()
        )
        found = {row.id: row for row in rows}
        deleted_ids: list[int] = []
        paths: list[Path] = []
        for sticker_id in dto.ids:
            row = found.get(sticker_id)
            if row is None:
                continue
            paths.append(Path(row.storage_path))
            await session.delete(row)
            deleted_ids.append(sticker_id)
        await self._commit(session)
        await self._remove_files(paths)
        return StickerBatchDeleteResultModel(
            deleted_ids=deleted_ids,
            missing_ids=[sticker_id for sticker_id in dto.ids if sticker_id not in found],
        )

    # ------------------------------------------------------------------
    # VLM 标注面
    # ------------------------------------------------------------------

    async def annotate(
        self, session: AsyncSession, sticker_id: int
    ) -> StickerAnnotationResultModel:
        """VLM 标注（描述 + 标签；失败落 failed 状态且可重试）。

        视觉条目解析与回退链复用 look_at 域裁决；网关 ``purpose="look_at"``
        （同属视觉能力面，用量行与其他视觉调用同口径）。所有条目失败 /
        JSON 解析失败 → 写 ``annotation_status=failed`` + ``annotation_error``
        并返回 ``ok=False``（不向管理面抛依赖异常）。

        Args:
            session: 注入的异步 Session（人格上下文已绑定）。
            sticker_id: 表情包主键。

        Returns:
            标注结果（含标注后的记录）。
        """
        self._require_enabled()
        row = await self._require_row(session, sticker_id)
        content = await self._read_file(row.storage_path)
        if content is None:
            return await self._mark_failed(
                session, row, "表情包文件缺失，无法标注"
            )
        messages: list[ChatMessageModel] = [
            ChatMessageModel(
                role=ChatMessageRole.SYSTEM,
                content=render_sticker_annotation_charter(),
            ),
            ChatMessageModel(
                role=ChatMessageRole.USER,
                content=[
                    ImageContentPart(
                        image_url=ImageContentUrlModel(
                            url=build_data_url(content, row.content_type)
                        )
                    ),
                    TextContentPart(text=render_sticker_annotation_instruction()),
                ],
            ),
        ]
        try:
            resolver = (
                self._resolver if self._resolver is not None else get_model_resolver()
            )
            chain = resolve_multimodal_chain(resolver.resolve(ModelRole.LOOK_AT))
        except BusinessException as exc:
            return await self._mark_failed(session, row, f"视觉条目解析失败：{exc.message}")
        last_error = "视觉回退链未返回可用标注"
        for entry in chain:
            try:
                response = await self._gateway.chat_for_entry(
                    entry, messages, purpose="look_at"
                )
            except BusinessException as exc:
                last_error = f"{entry.name}: {exc.message}"
                logger.warning(
                    "表情包标注条目调用失败，尝试回退链下一级: entry={}, code={}",
                    entry.name,
                    exc.code,
                )
                continue
            try:
                payload = _extract_annotation_payload(response.content)
                description = _normalize_annotation_description(
                    payload, self._resolve_settings()
                )
                tags = _annotation_tags(payload, self._resolve_settings())
            except ValueError as exc:
                last_error = f"{entry.name}: 标注 JSON 非法（{exc}）"
                logger.warning("表情包标注响应解析失败: entry={}, error={}", entry.name, exc)
                continue
            merged_tags = _merge_tags(_load_tags(row.tags), tags, self._resolve_settings())
            row.description = description
            row.tags = json.dumps(merged_tags, ensure_ascii=False)
            row.annotation_status = StickerAnnotationStatus.DONE.value
            row.annotation_error = None
            row.annotation_model_entry = entry.name
            row.annotated_at_ms = self._clock()
            await self._commit(session)
            return StickerAnnotationResultModel(
                ok=True,
                record=self._record(row),
                message=f"标注完成（模型条目 {entry.name}）",
            )
        return await self._mark_failed(session, row, last_error)

    # ------------------------------------------------------------------
    # 通道桥
    # ------------------------------------------------------------------

    async def pick_channel_reply(self, session: AsyncSession) -> StickerBinaryModel | None:
        """取最新一条可读表情用于通道回发（无素材 / 文件缺失返回 None）。

        Args:
            session: 注入的异步 Session（人格上下文已绑定）。

        Returns:
            记录 + 文件字节；库空或文件全缺失时为 None。
        """
        settings = self._resolve_settings()
        if not settings.enabled or not settings.channel_reply_enabled:
            return None
        rows = list(
            (
                await session.execute(select(StickerRow).order_by(desc(StickerRow.id)))
            )
            .scalars()
            .all()
        )
        for row in rows:
            content = await self._read_file(row.storage_path)
            if content is not None:
                return StickerBinaryModel(record=self._record(row), content=content)
        return None

    # ------------------------------------------------------------------
    # 内部——配置 / 行定位 / 上传准备 / 记录 / 文件
    # ------------------------------------------------------------------

    def _resolve_settings(self) -> StickerSettings:
        """取表情包配置（注入优先；None 读全局 settings.sticker）。"""
        return self._settings if self._settings is not None else global_settings.sticker

    def _require_enabled(self) -> StickerSettings:
        """校验功能启用（否则 409——与 voice TTS / ASR 禁用态同形态）。"""
        settings = self._resolve_settings()
        if not settings.enabled:
            raise BusinessException(
                code=ErrorCode.CONFLICT,
                message="表情包库未启用（STICKER_ENABLED=true 时方可调用）",
                details={"reason": "sticker disabled", "env_flag": "STICKER_ENABLED"},
            )
        return settings

    async def _require_row(self, session: AsyncSession, sticker_id: int) -> StickerRow:
        """按主键取表情包行（人格作用域内；缺失即 NOT_FOUND）。"""
        row = (
            (await session.execute(select(StickerRow).where(StickerRow.id == sticker_id)))
            .scalars()
            .first()
        )
        if row is None:
            raise BusinessException(
                code=ErrorCode.NOT_FOUND,
                message="表情包不存在",
                details={"sticker_id": sticker_id},
            )
        return row

    def _prepare_upload(
        self, item: StickerUploadItemModel, settings: StickerSettings
    ) -> tuple[bytes, str, list[str], str, str] | StickerRejectedItemModel:
        """校验并归一单条上传（返回准备物或拒收结局）。"""
        content_type = item.content_type.strip().lower()
        if content_type not in STICKER_IMAGE_MIME_ALLOWLIST:
            return StickerRejectedItemModel(
                index=0,
                reason=StickerRejectReason.UNSUPPORTED_CONTENT_TYPE,
                message=f"不支持的图片类型 {item.content_type}（允许 PNG / JPEG / WebP / GIF）",
            )
        source_note = item.source_note.strip()
        if not source_note:
            return StickerRejectedItemModel(
                index=0,
                reason=StickerRejectReason.SOURCE_NOTE_INVALID,
                message="来源 / 授权备注不能为空（上传必填）",
            )
        try:
            payload = base64.b64decode("".join(item.image_base64.split()), validate=True)
        except (binascii.Error, ValueError) as exc:
            return StickerRejectedItemModel(
                index=0,
                reason=StickerRejectReason.PAYLOAD_INVALID,
                message=f"图片 base64 载荷非法：{exc}",
            )
        if not payload:
            return StickerRejectedItemModel(
                index=0,
                reason=StickerRejectReason.PAYLOAD_INVALID,
                message="图片载荷解码后为空",
            )
        if len(payload) > settings.max_upload_bytes:
            return StickerRejectedItemModel(
                index=0,
                reason=StickerRejectReason.PAYLOAD_TOO_LARGE,
                message=f"图片字节数 {len(payload)} 超过上限 {settings.max_upload_bytes}",
            )
        tags, tag_error = _normalize_tags(item.tags, settings)
        if tags is None:
            return StickerRejectedItemModel(
                index=0,
                reason=StickerRejectReason.TAG_INVALID,
                message=tag_error or "标签非法",
            )
        description = item.description.strip()
        if len(description) > settings.description_max_length:
            return StickerRejectedItemModel(
                index=0,
                reason=StickerRejectReason.DESCRIPTION_TOO_LONG,
                message=f"描述不能超过 {settings.description_max_length} 个字符",
            )
        return payload, content_type, tags, description, source_note

    async def _mark_failed(
        self, session: AsyncSession, row: StickerRow, message: str
    ) -> StickerAnnotationResultModel:
        """把标注失败落为 failed 状态（可重试）并返回可读结果。"""
        row.annotation_status = StickerAnnotationStatus.FAILED.value
        row.annotation_error = message[:500]
        try:
            await self._commit(session)
        except Exception:
            await self._rollback_quietly(session)
            raise
        logger.warning("表情包标注失败（可重试）: sticker_id={}, error={}", row.id, message)
        return StickerAnnotationResultModel(
            ok=False, record=self._record(row), message=message
        )

    def _record(self, row: StickerRow) -> StickerRecordDTO:
        """行 → 读取 DTO（不含文件数据）。"""
        return StickerRecordDTO(
            id=row.id,
            sha256=row.sha256,
            content_type=row.content_type,
            size_bytes=row.size_bytes,
            tags=_load_tags(row.tags),
            description=row.description,
            source_note=row.source_note,
            annotation_status=StickerAnnotationStatus(row.annotation_status),
            annotation_error=row.annotation_error,
            annotation_model_entry=row.annotation_model_entry,
            annotated_at_ms=row.annotated_at_ms,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    async def _record_with_optional_data(
        self, row: StickerRow, *, include_data: bool
    ) -> StickerRecordDTO:
        """行 → 读取 DTO（按需内联文件 base64；文件缺失时数据为 None）。"""
        record = self._record(row)
        if not include_data:
            return record
        content = await self._read_file(row.storage_path)
        if content is None:
            return record
        return record.model_copy(
            update={"data_base64": base64.b64encode(content).decode("ascii")}
        )

    def _storage_dir(self) -> Path:
        """文件落盘根目录（注入优先；缺省 config.storage_dir）。"""
        if self._storage_root is not None:
            return self._storage_root
        return Path(self._resolve_settings().storage_dir)

    async def _write_file(self, digest: str, extension: str, payload: bytes) -> Path:
        """原子写入表情包文件（人格作用域目录——删同人格记录不误删他人文件）。"""
        persona_scope = persona_id_var.get() or "unscoped"
        persona_segment = hashlib.sha256(persona_scope.encode("utf-8")).hexdigest()[:12]
        directory = self._storage_dir() / persona_segment / digest[:2]
        target = directory / f"{digest}{extension}"
        return await run_blocking_ops(write_bytes_atomic, directory, target, payload)

    async def _read_file(self, storage_path: str) -> bytes | None:
        """读取文件（不存在 / 读取失败返回 None——调用方降级处理）。"""
        try:
            return await run_blocking_ops(_read_bytes, Path(storage_path))
        except OSError:
            logger.opt(exception=True).warning("表情包文件读取失败: {}", storage_path)
            return None

    async def _remove_files(self, paths: list[Path]) -> None:
        """尽力删除文件（失败仅告警——DB 行已清理，残留文件不参与查询）。"""
        for path in paths:
            try:
                await run_blocking_ops(unlink_missing_ok, path)
            except OSError:
                logger.opt(exception=True).warning("表情包文件删除失败: {}", path)

    @staticmethod
    async def _commit(session: AsyncSession) -> None:
        """提交事务（失败回滚后原样上抛）。"""
        try:
            await session.commit()
        except Exception:
            await StickerService._rollback_quietly(session)
            raise

    @staticmethod
    async def _rollback_quietly(session: AsyncSession) -> None:
        """尽力回滚（异常路径清理会话待处理态——不注入新异常）。"""
        try:
            await session.rollback()
        except Exception:  # best-effort: 回滚失败由会话关闭兜底
            logger.opt(exception=True).debug("表情包路径会话回滚失败（close 兜底）")


def _read_bytes(path: Path) -> bytes:
    """同步读取文件（供线程池下沉）。"""
    return path.read_bytes()


def _load_tags(raw: str) -> list[str]:
    """解析标签 JSON（损坏时返回空列表——读取面永不因脏数据失败）。"""
    try:
        parsed = json.loads(raw)
    except ValueError:
        return []
    if not isinstance(parsed, list):
        return []
    return [str(item) for item in parsed if isinstance(item, str)]


def _normalize_tags(
    tags: list[str], settings: StickerSettings
) -> tuple[list[str] | None, str | None]:
    """归一去重标签（空串跳过；超长或超量返回错误说明）。

    Args:
        tags: 原始标签列表。
        settings: 配置（长度 / 数量上限）。

    Returns:
        ``(归一标签列表 | None, 错误说明 | None)``；非法时列表为 None。
    """
    normalized: list[str] = []
    seen: set[str] = set()
    for tag in tags:
        value = tag.strip()
        if not value:
            continue
        if len(value) > settings.tag_max_length:
            return (
                None,
                f"标签「{value}」超过 {settings.tag_max_length} 个字符",
            )
        if value in seen:
            continue
        seen.add(value)
        normalized.append(value)
    if len(normalized) > settings.max_tags:
        return None, f"标签数量超过上限 {settings.max_tags} 条"
    return normalized, None


def _annotation_tags(payload: dict[str, Any], settings: StickerSettings) -> list[str]:
    """从模型 JSON 中提取标签（宽容：丢弃非字符串 / 超长项，截断到上限）。"""
    raw = payload.get("tags")
    if not isinstance(raw, list):
        raise ValueError("tags 不是数组")
    tags: list[str] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, str):
            continue
        value = item.strip()[: settings.tag_max_length]
        if not value or value in seen:
            continue
        seen.add(value)
        tags.append(value)
        if len(tags) >= settings.max_tags:
            break
    return tags


def _normalize_annotation_description(
    payload: dict[str, Any], settings: StickerSettings
) -> str:
    """从模型 JSON 中提取并截断描述（空描述视为解析失败）。"""
    raw = payload.get("description")
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("description 缺失或为空")
    return raw.strip()[: settings.description_max_length]


def _merge_tags(existing: list[str], learned: list[str], settings: StickerSettings) -> list[str]:
    """合并人工标签与模型标签（人工在前，去重，截断到上限）。"""
    merged: list[str] = []
    seen: set[str] = set()
    for tag in [*existing, *learned]:
        value = tag.strip()[: settings.tag_max_length]
        if not value or value in seen:
            continue
        seen.add(value)
        merged.append(value)
        if len(merged) >= settings.max_tags:
            break
    return merged


def _extract_annotation_payload(content: str) -> dict[str, Any]:
    """从模型回复中提取 JSON 对象（容忍代码围栏与前后解释文本）。

    Args:
        content: 模型回复文本。

    Returns:
        解析后的 JSON 对象。

    Raises:
        ValueError: 文本中不存在合法 JSON 对象。
    """
    text = _FENCE_PATTERN.sub("", content.strip())
    start = text.find("{")
    if start < 0:
        raise ValueError("回复中不存在 JSON 对象")
    try:
        payload, _ = json.JSONDecoder().raw_decode(text[start:])
    except ValueError as exc:
        raise ValueError(f"JSON 解析失败：{exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("JSON 根节点不是对象")
    return payload


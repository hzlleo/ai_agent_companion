"""persona 域长期指令服务——常备指令 CRUD（复用策展记忆存储，零新表）。

职责链（管理面 API / 面板 / 对话捕获共用同一服务）::

    create(session, dto)            创建 / 同 topic 覆盖（supersede——经
                                    MemoryService.upsert_curated）
    update(session, id, dto)        按既有行 topic 重写（supersede 链 +1）
    list_active(session, limit)     现行指令列表（curated active 收窄投影）
    delete(session, id)             删除（策展行删除——管理面显式动作）
    list_active_texts(session, ...) compose 注入面（现行指令正文列表）

存储决策（锁定，与 directives_models 模块文档一致）：长期指令是
``ff_curated_memory`` 的 ``kind=user_note`` + ``topic='directive:<slug>'``
行——supersede / 历史链 / 唯一 active 不变式全部由既有策展层承担；本服务
**不复制**存储逻辑，只在 ``MemoryService`` 公共面上做命名空间收窄与边界
校验（kind 固定 user_note、topic 前缀强制、provenance 固定 owner、
正文 1-500 字符）。

topic 语义：缺省按正文 sha256 指纹派生（``directive:<12 hex>``）——同文
重复创建收敛在同一链（天然去重）；显式 topic 用于「编辑」（同 topic 新
内容 → 旧行 superseded + 链链接）。

人格作用域：与记忆域同轴（PersonaMixin 自动注入 / 过滤）——调用方须先
经 ``bind_persona_context`` 绑定上下文，否则 fail loud。
"""

from __future__ import annotations

import hashlib

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from packages.harness.core.errors import BusinessException, ErrorCode
from packages.harness.memory.dtos import CuratedQueryDTO, CuratedRecordDTO, CuratedUpsertDTO
from packages.harness.memory.models import (
    CuratedKind,
    CuratedStatus,
    MemoryProvenance,
)
from packages.harness.memory.service import MemoryService
from packages.harness.persona.directives_models import (
    DIRECTIVE_CONTENT_MAX_LENGTH,
    DIRECTIVE_TOPIC_PREFIX,
    DirectiveCreateDTO,
    DirectiveRecordDTO,
    DirectiveUpdateDTO,
)

__all__ = [
    "MAX_ACTIVE_DIRECTIVES",
    "DIRECTIVES_QUERY_LIMIT",
    "DirectivesService",
]

#: 现行指令查询上限（列表 / 注入面拉取上限——compose 另有 20 条渲染上限）。
DIRECTIVES_QUERY_LIMIT: int = 200

#: 面板 / 注入面可用的现行指令上限（超出的最早指令不进入注入块——见
#: chat prompts 的截断说明；列表面仍可见全部）。
MAX_ACTIVE_DIRECTIVES: int = 20

#: topic 指纹长度（sha256 前 12 hex——同文去重键）。
_TOPIC_FINGERPRINT_LENGTH: int = 12


def _record(record: CuratedRecordDTO) -> DirectiveRecordDTO:
    """策展记录 → 指令记录投影（字段收窄——topic / 正文 / 版本 / 时间）。

    Args:
        record: 策展记忆记录。

    Returns:
        指令记录 DTO。
    """
    return DirectiveRecordDTO(
        id=record.id,
        topic=record.topic,
        content=record.content,
        version=record.version,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


def _derive_topic(content: str) -> str:
    """按正文派生 topic（``directive:<sha256 前 12 hex>``——同文确定性）。

    Args:
        content: 指令正文（已裁剪）。

    Returns:
        命名空间内 topic 键。
    """
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()[:_TOPIC_FINGERPRINT_LENGTH]
    return f"{DIRECTIVE_TOPIC_PREFIX}{digest}"


def _validate_content(content: str) -> str:
    """校验并裁剪指令正文（1-500 字符）。

    Args:
        content: 原始正文。

    Returns:
        裁剪后的正文。

    Raises:
        BusinessException: 正文为空 / 超长（``VALIDATION_ERROR``）。
    """
    stripped = content.strip()
    if not stripped or len(stripped) > DIRECTIVE_CONTENT_MAX_LENGTH:
        raise BusinessException(
            code=ErrorCode.VALIDATION_ERROR,
            message="长期指令正文须为 1-500 字符（首尾空白不计）",
            details={"length": len(stripped)},
        )
    return stripped


def _normalize_topic(topic: str) -> str:
    """归一 topic（强制命名空间前缀 + 长度帽）。

    Args:
        topic: 显式 topic（可带 / 不带前缀）。

    Returns:
        归一后的 topic 键。

    Raises:
        BusinessException: 归一后超长（``VALIDATION_ERROR``）。
    """
    stripped = topic.strip()
    normalized = (
        stripped
        if stripped.startswith(DIRECTIVE_TOPIC_PREFIX)
        else f"{DIRECTIVE_TOPIC_PREFIX}{stripped}"
    )
    if len(normalized) > 255:
        raise BusinessException(
            code=ErrorCode.VALIDATION_ERROR,
            message="长期指令主题键须为 1-255 字符（directive: 前缀计入长度）",
            details={"length": len(normalized)},
        )
    return normalized


class DirectivesService:
    """长期指令服务（策展记忆收窄面——CRUD + 注入文本读取）。

    构造注入：``memory_service``（None = 现场构造无状态默认实例）；服务
    自身无状态（Session 逐次注入），可安全跨请求共享。
    """

    def __init__(self, *, memory_service: MemoryService | None = None) -> None:
        """初始化服务。

        Args:
            memory_service: 策展记忆服务（None = 现场构造默认实例——无状态
                共享安全）。
        """
        self._memory = memory_service if memory_service is not None else MemoryService()

    async def create(self, session: AsyncSession, dto: DirectiveCreateDTO) -> DirectiveRecordDTO:
        """创建长期指令（缺省指纹 topic；显式同 topic 覆盖为 supersede）。

        Args:
            session: 注入的异步 Session（人格上下文已绑定）。
            dto: 创建入参。

        Returns:
            新建 / 取代后的现行指令记录。

        Raises:
            BusinessException: 正文 / topic 边界非法（``VALIDATION_ERROR``）；
                并发 upsert 重试后仍冲突（``INTERNAL_ERROR``——策展层透传）。
        """
        content = _validate_content(dto.content)
        topic = _normalize_topic(dto.topic) if dto.topic is not None else _derive_topic(content)
        record = await self._memory.upsert_curated(
            session,
            CuratedUpsertDTO(
                kind=CuratedKind.USER_NOTE,
                topic=topic,
                content=content,
                provenance=MemoryProvenance.OWNER,
            ),
        )
        logger.info("长期指令已写入: id={}, topic={}, version={}", record.id, topic, record.version)
        return _record(record)

    async def update(
        self, session: AsyncSession, directive_id: int, dto: DirectiveUpdateDTO
    ) -> DirectiveRecordDTO:
        """按既有行 topic 重写指令（supersede 链 +1；旧行标记 superseded）。

        Args:
            session: 注入的异步 Session（人格上下文已绑定）。
            directive_id: 既有指令（策展行）主键——取其 topic 定链。
            dto: 更新入参（新正文）。

        Returns:
            取代后的现行指令记录。

        Raises:
            BusinessException: 指令不存在 / 非本域行（``NOT_FOUND``）；
                正文非法（``VALIDATION_ERROR``）。
        """
        existing = await self._require_directive(session, directive_id)
        content = _validate_content(dto.content)
        record = await self._memory.upsert_curated(
            session,
            CuratedUpsertDTO(
                kind=CuratedKind.USER_NOTE,
                topic=existing.topic,
                content=content,
                provenance=MemoryProvenance.OWNER,
            ),
        )
        logger.info(
            "长期指令已更新: old_id={}, new_id={}, topic={}",
            directive_id,
            record.id,
            record.topic,
        )
        return _record(record)

    async def list_active(
        self, session: AsyncSession, *, limit: int = DIRECTIVES_QUERY_LIMIT
    ) -> list[DirectiveRecordDTO]:
        """列出现行长期指令（active 行——id 降序，新写入在前）。

        实现注记：策展查询面无 topic 前缀过滤（仅精确匹配），本服务按
        ``kind=user_note + status=active`` 拉取后在前缀面收窄（长期指令
        量级小——200 行帽，Python 过滤成本可忽略）。

        Args:
            session: 注入的异步 Session（人格上下文已绑定）。
            limit: 返回条数上限（1-200）。

        Returns:
            现行指令记录列表（按 id 降序）。
        """
        page = await self._memory.query_curated(
            session,
            CuratedQueryDTO(
                kind=CuratedKind.USER_NOTE,
                status=CuratedStatus.ACTIVE,
                limit=min(max(limit, 1), DIRECTIVES_QUERY_LIMIT),
            ),
        )
        directives = [record for record in page.items if record.topic.startswith(DIRECTIVE_TOPIC_PREFIX)]
        return [_record(record) for record in directives]

    async def list_active_texts(
        self, session: AsyncSession, *, limit: int = MAX_ACTIVE_DIRECTIVES
    ) -> list[str]:
        """读取现行指令正文（compose 注入面——按写入倒序取前 N 条）。

        Args:
            session: 注入的异步 Session（人格上下文已绑定）。
            limit: 返回条数上限（1-20——compose 渲染帽）。

        Returns:
            指令正文列表（新写入在前；空列表 = 无长期指令）。
        """
        records = await self.list_active(session, limit=min(max(limit, 1), MAX_ACTIVE_DIRECTIVES))
        return [record.content for record in records]

    async def delete(self, session: AsyncSession, directive_id: int) -> None:
        """删除长期指令（策展行删除——管理面显式动作）。

        删除后该 topic 无 active 行，后续同 topic 覆盖重新起链（version
        从 1——策展层既有语义）。

        Args:
            session: 注入的异步 Session（人格上下文已绑定）。
            directive_id: 指令（策展行）主键。

        Raises:
            BusinessException: 指令不存在 / 非本域行（``NOT_FOUND``）。
        """
        await self._require_directive(session, directive_id)
        await self._memory.delete_curated(session, directive_id)
        logger.info("长期指令已删除: id={}", directive_id)

    async def _require_directive(self, session: AsyncSession, directive_id: int) -> CuratedRecordDTO:
        """取指令行并校验命名空间（kind=user_note 且 topic 前缀匹配）。

        Args:
            session: 注入的异步 Session（人格上下文已绑定）。
            directive_id: 指令（策展行）主键。

        Returns:
            既有策展记录。

        Raises:
            BusinessException: 不存在（``NOT_FOUND``）或非长期指令行
                （``NOT_FOUND``——隔离语义：非本域行对本域不可见）。
        """
        record = await self._memory.get_curated(session, directive_id)
        if record.kind is not CuratedKind.USER_NOTE or not record.topic.startswith(
            DIRECTIVE_TOPIC_PREFIX
        ):
            raise BusinessException(
                code=ErrorCode.NOT_FOUND,
                message="长期指令不存在（非本域策展行）",
                details={"id": directive_id, "kind": record.kind.value, "topic": record.topic},
            )
        return record

#!/usr/bin/env python3
"""后端 DTO JSON Schema 导出器——前端契约漂移门禁的数据源。

背景：前端 ``frontend/src/types/`` 是后端 pydantic DTO 的手写镜像
（前后端源码隔离约束），历史无任何漂移检测机制（批次 4 审查 major #2）。
本脚本把两侧共享的 wire 契约（约 45 组 DTO / Model Class）的
``model_json_schema()`` 导出为**确定性 JSON fixture** 提交入库：

    - 前端 ``dtoContract.test.ts`` 读 fixture，用 TypeScript 编译器 API
      提取手写类型的成员名集合，与 schema ``properties`` 键集逐组断言
      相等（拦「前端类型少写 / 多写字段」方向）；
    - 后端 ``test_dto_schema_fixture.py`` 在内存中再生 fixture 并与提交
      态逐字节比对（拦「后端 DTO 改动未重新导出」方向）。

设计原则（与 gen-examples 同模式）：

    - **模型留包内**：pydantic 类是运行时代码，留在 backend 包；导出
      逻辑是构建工具，在仓库根 scripts/；
    - **幂等 byte-stable**：排序键 + 无时间戳 / 无随机性渗入，重复运行
      产出逐字节相同内容；
    - **manifest 即清单**：fixture 头部的 manifest 段固化
      「schema 键 ↔ 前端类型名 ↔ 前端源文件」映射，两侧门禁共用。

用法::

    cd backend && pixi run export-dto-schemas    # 唯一 pixi 任务入口

退出码：0 成功 / 1 失败。
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# sys.path 修复（与 generate_examples.py 同模式）——pixi 运行时 activation
# 已含 PYTHONPATH=backend/src，此修复为直接执行兜底；必须先于
# packages.harness.* / app.* 导入执行。
# ---------------------------------------------------------------------------

_SCRIPTS_DIR: Path = Path(__file__).resolve().parent
_REPO_ROOT: Path = _SCRIPTS_DIR.parent
_BACKEND_SRC: Path = _REPO_ROOT / "backend" / "src"
if str(_BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(_BACKEND_SRC))

# 下方全部 import 必须晚于 sys.path 修复块（与 generate_examples.py 同
# 结构；scripts/ 不在 ruff 检查面，无需 E402 抑制注释——noqa 基线零新增）。
from pydantic import BaseModel

from app.api.usage import UsageSummaryDTO
from packages.harness.config.registry_admin_dtos import (
    ModelEntryCreateDTO,
    ModelEntryUpdateDTO,
    ModelEntryMaskedDTO,
    RegistryStateDTO,
    RoleBindingDTO,
)
from packages.harness.core.dtos import PageResponse, ResultDTO
from packages.harness.memory.dreaming_models import (
    DreamDecisionModel,
    DreamJobConfig,
    DreamRunRecordDTO,
    DreamRunStatsModel,
    DreamTriggerRequestDTO,
)
from packages.harness.memory.dtos import (
    ChatMessageDTO,
    ChatMessageMiniDTO,
    ChatVariantRegenerateRequestDTO,
    ChatVariantSelectRequestDTO,
    ChatSessionDetailDTO,
    ChatSessionSummaryDTO,
    CuratedQueryDTO,
    CuratedRecordDTO,
    CuratedUpsertDTO,
    EpisodeCreateDTO,
    EpisodeQueryDTO,
    EpisodeRecordDTO,
)
from packages.harness.memory.models import (
    ChatTurnPayloadModel,
    DutyReportPayloadModel,
    EpisodeNotePayloadModel,
    PlatformActionPayloadModel,
)
from packages.harness.memory.retrieval_models import (
    MemorySearchRequestDTO,
    MemorySearchResultDTO,
)
from packages.harness.persona.directives_models import (
    DirectiveCreateDTO,
    DirectiveRecordDTO,
    DirectiveUpdateDTO,
)
from packages.harness.persona.chara_card import (
    CharaBookEntryModel,
    CharaBookModel,
    CharaCardModel,
)
from packages.harness.persona.dtos import (
    CharaCardPatchDTO,
    PersonaBatchDeleteItemDTO,
    PersonaBatchDeleteRequest,
    PersonaBatchDeleteResponseDTO,
    PersonaCardDTO,
    PersonaCopyDTO,
    PersonaCreateDTO,
    PersonaImportBatchItemDTO,
    PersonaImportBatchResultDTO,
    PersonaImportRequestDTO,
    PersonaImportResultModel,
    PersonaQueryDTO,
    PersonaRecordDTO,
    PersonaStateDTO,
    PersonaStateUpsertDTO,
    PersonaUpdateDTO,
    TraitLibraryDTO,
)
from packages.harness.persona.trait_library import (
    TraitComponentModel,
    TraitPresetModel,
)
from packages.harness.reminders.models import (
    ReminderBatchCancelItemDTO,
    ReminderBatchCancelRequest,
    ReminderBatchCancelResponseDTO,
    ReminderCreateModel,
    ReminderQueryDTO,
    ReminderRecordDTO,
)
from packages.harness.usage.dtos import (
    UsageAggregateDTO,
    UsageAggregateQueryDTO,
    UsageFilterFields,
    UsageQueryDTO,
    UsageRecordDTO,
)
from packages.harness.voice.voice_models import VoiceProfileModel

__all__ = [
    "SchemaExportEntry",
    "DTO_SCHEMAS_FIXTURE_PATH",
    "build_manifest",
    "build_fixture",
    "serialize_fixture",
    "export_fixture",
    "main",
]

#: 前端契约 fixture 落盘路径（仓库根相对）。
DTO_SCHEMAS_FIXTURE_PATH: Path = (
    _REPO_ROOT / "frontend" / "src" / "test" / "contract" / "dtoSchemas.json"
)

#: 前端类型源文件（仓库根相对；manifest 映射与前端契约测试共用）。
_FRONTEND_COMMON = "frontend/src/types/common.ts"
_FRONTEND_PERSONA = "frontend/src/types/persona.ts"
_FRONTEND_MEMORY = "frontend/src/types/memory.ts"
_FRONTEND_REMINDERS = "frontend/src/types/reminders.ts"
_FRONTEND_USAGE = "frontend/src/types/usage.ts"
_FRONTEND_REGISTRY = "frontend/src/types/model-registry.ts"


@dataclass(frozen=True)
class SchemaExportEntry:
    """manifest 单条：schema 键 ↔ 前端类型 ↔ 前端源文件。

    Attributes:
        schema_key: fixture ``schemas`` 段的键（后端类名）。
        model_cls: 后端 pydantic Model Class（导出 model_json_schema）。
        frontend_type: 前端 ``src/types/`` 中的类型名。
        frontend_file: 前端源文件（仓库根相对路径）。
        note: 备注（同构 / 别名等特殊情况说明）。
    """

    schema_key: str
    model_cls: type[BaseModel]
    frontend_type: str
    frontend_file: str
    note: str = ""


def build_manifest() -> list[SchemaExportEntry]:
    """构建全量导出清单（前后端共享 wire 契约的闭集）。

    Returns:
        manifest 条目列表（顺序即 fixture 中的映射顺序）。
    """
    return [
        # 通用封装。
        SchemaExportEntry("ResultDTO", ResultDTO, "ResultDTO", _FRONTEND_COMMON),
        SchemaExportEntry("PageResponse", PageResponse, "PageResponse", _FRONTEND_COMMON),
        SchemaExportEntry(
            "PersonaBatchDeleteResponseDTO",
            PersonaBatchDeleteResponseDTO,
            "BatchDeleteResult",
            _FRONTEND_COMMON,
            note="人格 / 情景事件批量删除响应同构；memory.ts 的 EpisodeBatchDeleteResponse 为本类型别名",
        ),
        # persona 域。
        SchemaExportEntry("PersonaRecordDTO", PersonaRecordDTO, "PersonaRecord", _FRONTEND_PERSONA),
        SchemaExportEntry("PersonaCardDTO", PersonaCardDTO, "PersonaCardResponse", _FRONTEND_PERSONA),
        SchemaExportEntry("PersonaStateDTO", PersonaStateDTO, "PersonaState", _FRONTEND_PERSONA),
        SchemaExportEntry("PersonaCreateDTO", PersonaCreateDTO, "PersonaCreateRequest", _FRONTEND_PERSONA),
        SchemaExportEntry("PersonaUpdateDTO", PersonaUpdateDTO, "PersonaUpdateRequest", _FRONTEND_PERSONA),
        SchemaExportEntry("PersonaCopyDTO", PersonaCopyDTO, "PersonaCopyRequest", _FRONTEND_PERSONA),
        SchemaExportEntry(
            "PersonaStateUpsertDTO", PersonaStateUpsertDTO, "PersonaStateUpsertRequest", _FRONTEND_PERSONA
        ),
        SchemaExportEntry(
            "PersonaBatchDeleteRequest", PersonaBatchDeleteRequest, "PersonaBatchDeleteRequest", _FRONTEND_PERSONA
        ),
        SchemaExportEntry(
            "PersonaBatchDeleteItemDTO", PersonaBatchDeleteItemDTO, "PersonaBatchDeleteItem", _FRONTEND_PERSONA
        ),
        SchemaExportEntry("PersonaQueryDTO", PersonaQueryDTO, "PersonaListParams", _FRONTEND_PERSONA),
        SchemaExportEntry("CharaCardModel", CharaCardModel, "CharaCard", _FRONTEND_PERSONA),
        SchemaExportEntry("CharaBookModel", CharaBookModel, "CharaBook", _FRONTEND_PERSONA),
        SchemaExportEntry("CharaBookEntryModel", CharaBookEntryModel, "CharaBookEntry", _FRONTEND_PERSONA),
        SchemaExportEntry("CharaCardPatchDTO", CharaCardPatchDTO, "CharaCardPatch", _FRONTEND_PERSONA),
        SchemaExportEntry("VoiceProfileModel", VoiceProfileModel, "VoiceProfile", _FRONTEND_PERSONA),
        # persona 域——特质组件库与导入面（R2b-2）。
        SchemaExportEntry("TraitComponentModel", TraitComponentModel, "TraitComponent", _FRONTEND_PERSONA),
        SchemaExportEntry("TraitPresetModel", TraitPresetModel, "TraitPreset", _FRONTEND_PERSONA),
        SchemaExportEntry("TraitLibraryDTO", TraitLibraryDTO, "TraitLibrary", _FRONTEND_PERSONA),
        SchemaExportEntry("PersonaImportRequestDTO", PersonaImportRequestDTO, "PersonaImportRequest", _FRONTEND_PERSONA),
        SchemaExportEntry("PersonaImportResultModel", PersonaImportResultModel, "PersonaImportResult", _FRONTEND_PERSONA),
        SchemaExportEntry("PersonaImportBatchItemDTO", PersonaImportBatchItemDTO, "PersonaImportBatchItem", _FRONTEND_PERSONA),
        SchemaExportEntry("PersonaImportBatchResultDTO", PersonaImportBatchResultDTO, "PersonaImportBatchResult", _FRONTEND_PERSONA),
        # persona 域——长期指令（R2b D7-14；策展记忆 user_note 收窄面）。
        SchemaExportEntry("DirectiveCreateDTO", DirectiveCreateDTO, "DirectiveCreateRequest", _FRONTEND_PERSONA),
        SchemaExportEntry("DirectiveUpdateDTO", DirectiveUpdateDTO, "DirectiveUpdateRequest", _FRONTEND_PERSONA),
        SchemaExportEntry("DirectiveRecordDTO", DirectiveRecordDTO, "DirectiveRecord", _FRONTEND_PERSONA),
        # memory 域——情景事件。
        SchemaExportEntry("EpisodeCreateDTO", EpisodeCreateDTO, "EpisodeCreateRequest", _FRONTEND_MEMORY),
        SchemaExportEntry("EpisodeRecordDTO", EpisodeRecordDTO, "EpisodeRecord", _FRONTEND_MEMORY),
        SchemaExportEntry("EpisodeQueryDTO", EpisodeQueryDTO, "EpisodeListParams", _FRONTEND_MEMORY),
        # memory 域——载荷（按 kind 判别）。
        SchemaExportEntry("ChatTurnPayloadModel", ChatTurnPayloadModel, "ChatTurnPayload", _FRONTEND_MEMORY),
        SchemaExportEntry("DutyReportPayloadModel", DutyReportPayloadModel, "DutyReportPayload", _FRONTEND_MEMORY),
        SchemaExportEntry(
            "PlatformActionPayloadModel", PlatformActionPayloadModel, "PlatformActionPayload", _FRONTEND_MEMORY
        ),
        SchemaExportEntry("EpisodeNotePayloadModel", EpisodeNotePayloadModel, "EpisodeNotePayload", _FRONTEND_MEMORY),
        # memory 域——策展记忆。
        SchemaExportEntry("CuratedUpsertDTO", CuratedUpsertDTO, "CuratedUpsertRequest", _FRONTEND_MEMORY),
        SchemaExportEntry("CuratedRecordDTO", CuratedRecordDTO, "CuratedRecord", _FRONTEND_MEMORY),
        SchemaExportEntry("CuratedQueryDTO", CuratedQueryDTO, "CuratedListParams", _FRONTEND_MEMORY),
        # memory 域——会话。
        SchemaExportEntry("ChatSessionSummaryDTO", ChatSessionSummaryDTO, "ChatSessionSummary", _FRONTEND_MEMORY),
        SchemaExportEntry("ChatMessageDTO", ChatMessageDTO, "ChatMessage", _FRONTEND_MEMORY),
        SchemaExportEntry("ChatSessionDetailDTO", ChatSessionDetailDTO, "ChatSessionDetail", _FRONTEND_MEMORY),
        SchemaExportEntry("ChatMessageMiniDTO", ChatMessageMiniDTO, "ChatMessageVariant", _FRONTEND_MEMORY),
        SchemaExportEntry(
            "ChatVariantRegenerateRequestDTO",
            ChatVariantRegenerateRequestDTO,
            "ChatVariantRegenerateRequest",
            _FRONTEND_MEMORY,
        ),
        SchemaExportEntry(
            "ChatVariantSelectRequestDTO",
            ChatVariantSelectRequestDTO,
            "ChatVariantSelectRequest",
            _FRONTEND_MEMORY,
        ),
        # memory 域——检索。
        SchemaExportEntry(
            "MemorySearchRequestDTO", MemorySearchRequestDTO, "MemorySearchRequest", _FRONTEND_MEMORY
        ),
        SchemaExportEntry("MemorySearchResultDTO", MemorySearchResultDTO, "MemorySearchResult", _FRONTEND_MEMORY),
        # memory 域——梦境。
        SchemaExportEntry("DreamJobConfig", DreamJobConfig, "DreamJobConfig", _FRONTEND_MEMORY),
        SchemaExportEntry("DreamRunStatsModel", DreamRunStatsModel, "DreamRunStats", _FRONTEND_MEMORY),
        SchemaExportEntry("DreamDecisionModel", DreamDecisionModel, "DreamDecision", _FRONTEND_MEMORY),
        SchemaExportEntry("DreamRunRecordDTO", DreamRunRecordDTO, "DreamRunRecord", _FRONTEND_MEMORY),
        SchemaExportEntry("DreamTriggerRequestDTO", DreamTriggerRequestDTO, "DreamTriggerRequest", _FRONTEND_MEMORY),
        # reminders 域——持久化提醒（P10=B）。
        SchemaExportEntry("ReminderCreateModel", ReminderCreateModel, "ReminderCreateRequest", _FRONTEND_REMINDERS),
        SchemaExportEntry("ReminderRecordDTO", ReminderRecordDTO, "ReminderRecord", _FRONTEND_REMINDERS),
        SchemaExportEntry("ReminderQueryDTO", ReminderQueryDTO, "ReminderListParams", _FRONTEND_REMINDERS),
        SchemaExportEntry(
            "ReminderBatchCancelRequest",
            ReminderBatchCancelRequest,
            "ReminderBatchCancelRequest",
            _FRONTEND_REMINDERS,
        ),
        SchemaExportEntry(
            "ReminderBatchCancelItemDTO",
            ReminderBatchCancelItemDTO,
            "ReminderBatchCancelItem",
            _FRONTEND_REMINDERS,
        ),
        SchemaExportEntry(
            "ReminderBatchCancelResponseDTO",
            ReminderBatchCancelResponseDTO,
            "ReminderBatchCancelResponse",
            _FRONTEND_REMINDERS,
        ),
        # usage 域。
        SchemaExportEntry("UsageRecordDTO", UsageRecordDTO, "UsageRecord", _FRONTEND_USAGE),
        SchemaExportEntry("UsageAggregateDTO", UsageAggregateDTO, "UsageAggregate", _FRONTEND_USAGE),
        SchemaExportEntry("UsageSummaryDTO", UsageSummaryDTO, "UsageSummary", _FRONTEND_USAGE),
        SchemaExportEntry("UsageFilterFields", UsageFilterFields, "UsageFilterParams", _FRONTEND_USAGE),
        SchemaExportEntry("UsageQueryDTO", UsageQueryDTO, "UsageRecordListParams", _FRONTEND_USAGE),
        SchemaExportEntry(
            "UsageAggregateQueryDTO", UsageAggregateQueryDTO, "UsageAggregateParams", _FRONTEND_USAGE
        ),
        # model-registry 域。
        SchemaExportEntry("ModelEntryCreateDTO", ModelEntryCreateDTO, "ModelEntryCreateRequest", _FRONTEND_REGISTRY),
        SchemaExportEntry("ModelEntryUpdateDTO", ModelEntryUpdateDTO, "ModelEntryUpdateRequest", _FRONTEND_REGISTRY),
        SchemaExportEntry("ModelEntryMaskedDTO", ModelEntryMaskedDTO, "ModelEntryMasked", _FRONTEND_REGISTRY),
        SchemaExportEntry("RegistryStateDTO", RegistryStateDTO, "RegistryState", _FRONTEND_REGISTRY),
        SchemaExportEntry("RoleBindingDTO", RoleBindingDTO, "RoleBinding", _FRONTEND_REGISTRY),
    ]


def build_fixture() -> dict[str, Any]:
    """构建 fixture 全量结构（manifest 头 + schemas 段）。

    Returns:
        ``{"manifest": {...}, "schemas": {后端类名: model_json_schema()}}``。

    Raises:
        ValueError: manifest 存在重复 schema_key（清单登记错误——
            fail loud 防止 fixture 键互相覆盖）。
    """
    manifest = build_manifest()
    schemas: dict[str, Any] = {}
    seen_keys: set[str] = set()
    for entry in manifest:
        if entry.schema_key in seen_keys:
            raise ValueError(f"manifest 存在重复 schema_key: {entry.schema_key}")
        seen_keys.add(entry.schema_key)
        schemas[entry.schema_key] = entry.model_cls.model_json_schema()
    mappings = [
        {
            "schema_key": entry.schema_key,
            "frontend_type": entry.frontend_type,
            "frontend_file": entry.frontend_file,
            **({"note": entry.note} if entry.note else {}),
        }
        for entry in manifest
    ]
    return {
        "manifest": {
            "description": (
                "后端 DTO JSON Schema fixture（契约漂移门禁数据源）——由 "
                "scripts/export_dto_schemas.py 导出（cd backend && pixi run "
                "export-dto-schemas 再生成）；前端 dtoContract.test.ts 与后端 "
                "test_dto_schema_fixture.py 双侧比对。严禁手改本文件。"
            ),
            "mappings": mappings,
        },
        "schemas": schemas,
    }


def serialize_fixture(fixture: dict[str, Any]) -> str:
    """确定性序列化（排序键 + 中文原样 + 缩进 2 + 尾换行）。

    Args:
        fixture: build_fixture() 产物。

    Returns:
        byte-stable JSON 文本（同环境重复序列化逐字节一致）。
    """
    return json.dumps(fixture, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


def export_fixture(*, write: bool = True) -> str:
    """导出 fixture（再生成 + 可选落盘）。

    Args:
        write: True 时写入 ``frontend/src/test/contract/dtoSchemas.json``；
            False 仅返回内容（后端 freshness 测试用）。

    Returns:
        序列化后的 fixture 全文。
    """
    content = serialize_fixture(build_fixture())
    if write:
        DTO_SCHEMAS_FIXTURE_PATH.parent.mkdir(parents=True, exist_ok=True)
        DTO_SCHEMAS_FIXTURE_PATH.write_text(content, encoding="utf-8")
    return content


def main() -> None:
    """CLI 入口：导出 fixture 并打印摘要。"""
    print(f"\n{'=' * 60}")
    print("  ai_agent_companion — 后端 DTO JSON Schema 导出器")
    print(f"{'=' * 60}\n")
    try:
        content = export_fixture(write=True)
    except Exception as exc:
        print(f"✗ 导出失败: {exc}", file=sys.stderr)
        import traceback

        traceback.print_exc()
        sys.exit(1)
    manifest = build_manifest()
    print(f"  ✓ {DTO_SCHEMAS_FIXTURE_PATH.relative_to(_REPO_ROOT)}")
    print(f"    清单条目: {len(manifest)} 组（schema 键 ↔ 前端类型）")
    print(f"    文件体积: {len(content.encode('utf-8'))} 字节")
    print("\n  提交 fixture 后，两侧门禁将按此基线拦截漂移。完成。\n")


if __name__ == "__main__":
    main()

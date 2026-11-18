"""核心记忆工具注册测试——edit_core_memory 规格 / 校验 / 执行 / 会话归属。

覆盖面：注册（工具名 / 参数 enum / args_model）/ 参数边界（非法块键由
registry 归一 ok=False）/ 成功执行（会话主键经 provider 落到块行与修订）
/ 锁定块的可读拒绝（registry 层 ok=True、结果载荷 ok=False）/ 人格
上下文缺失（PERSONA_CONTEXT_REQUIRED 归一失败）。
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

import packages.harness.persona.core_memory_orm as core_memory_orm  # noqa: F401  模型注册副作用导入（共享建表）
from packages.harness.database.engine import db_engine_holder
from packages.harness.persona.core_memory import (
    CoreMemoryService,
    register_core_memory_tools,
)
from packages.harness.persona.core_memory_models import (
    CORE_MEMORY_TOOL_NAME,
    AgentEditModel,
    CoreBlockKey,
    OwnerEditModel,
)
from packages.harness.persona.service import bind_persona_context
from packages.harness.tools.registry import ToolRegistry

#: 测试人格上下文轴。
PERSONA = "core-tool"

#: 注入时钟（epoch 毫秒——修订时刻）。
NOW = 1_760_200_000_000


@pytest.fixture
async def installed_engine(shared_db_engine: AsyncEngine) -> AsyncIterator[None]:
    """把共享引擎装进全局持有器（工具执行面经 session_scope 消费）。"""
    db_engine_holder.async_engine = shared_db_engine
    db_engine_holder.session_factory = async_sessionmaker(
        bind=shared_db_engine, expire_on_commit=False
    )
    db_engine_holder.owner_token = None
    try:
        yield
    finally:
        db_engine_holder.reset()


def _registry(session_id: int | None = 11) -> tuple[ToolRegistry, CoreMemoryService]:
    """构造注册表与注时钟服务（会话主键经 provider 固定）。"""
    registry = ToolRegistry()
    service = CoreMemoryService(clock=lambda: NOW)
    register_core_memory_tools(registry, service, lambda: session_id)
    return registry, service


async def test_registers_single_edit_tool_with_fixed_block_enum() -> None:
    """注册面：工具名 / 描述 / 块键 enum / args_model 与契约一致。"""
    registry, _ = _registry()
    assert registry.tool_names == [CORE_MEMORY_TOOL_NAME]
    spec = registry.list_specs()[0]
    assert spec.name == CORE_MEMORY_TOOL_NAME
    assert "稳定" in spec.description
    block_schema = spec.parameters.properties["block"]
    assert block_schema["enum"] == [key.value for key in CoreBlockKey]
    assert spec.parameters.required == ["block", "content"]


async def test_execute_writes_block_with_session_provenance(
    installed_engine: None, db_session: AsyncSession
) -> None:
    """成功执行：registry ok=True + 载荷 ok=True + 块行 / 修订记录会话主键。"""
    registry, service = _registry(session_id=11)
    async with bind_persona_context(PERSONA):
        result = await registry.execute(
            CORE_MEMORY_TOOL_NAME,
            {"block": "self_identity", "content": "我是会写字的鱼", "mode": "replace"},
        )
        blocks = await service.list_blocks(db_session)
        revisions = await service.list_revisions(db_session, CoreBlockKey.SELF_IDENTITY)
    assert result.ok is True
    assert result.result["ok"] is True
    assert result.result["length"] == len("我是会写字的鱼")
    target = next(block for block in blocks if block.block == CoreBlockKey.SELF_IDENTITY)
    assert target.content == "我是会写字的鱼"
    assert target.last_edited_session_id == 11
    assert revisions[0].session_id == 11


async def test_execute_locked_block_returns_readable_result(
    installed_engine: None, db_session: AsyncSession
) -> None:
    """锁定块：registry 层执行成功，载荷 ok=False 且带中文拒绝消息。"""
    registry, service = _registry()
    async with bind_persona_context(PERSONA):
        await service.owner_update(
            db_session, CoreBlockKey.OPEN_NOTES, OwnerEditModel(content="锁住的备忘", locked=True)
        )
        result = await registry.execute(
            CORE_MEMORY_TOOL_NAME, {"block": "open_notes", "content": "新备忘"}
        )
    assert result.ok is True
    assert result.result["ok"] is False
    assert result.result["message"] == "核心记忆块「备忘」已锁定，仅主人可修改"
    assert result.result["length"] == len("锁住的备忘")


async def test_execute_requires_persona_context(installed_engine: None) -> None:
    """无人格上下文：registry 归一 ok=False（PERSONA_CONTEXT_REQUIRED）。"""
    registry, _ = _registry()
    result = await registry.execute(
        CORE_MEMORY_TOOL_NAME, {"block": "open_notes", "content": "备忘"}
    )
    assert result.ok is False
    assert "PERSONA_CONTEXT_REQUIRED" in (result.error or "")


async def test_execute_rejects_unknown_block_key(installed_engine: None) -> None:
    """非法块键：args_model 校验失败 → registry ok=False（不触执行器）。"""
    registry, _ = _registry()
    async with bind_persona_context(PERSONA):
        result = await registry.execute(
            CORE_MEMORY_TOOL_NAME, {"block": "fifth_block", "content": "内容"}
        )
    assert result.ok is False
    assert "参数校验失败" in (result.error or "")


async def test_args_model_is_published_contract() -> None:
    """工具 args_model 与 AgentEditModel 同一契约（字段 / 长度边界）。"""
    edit = AgentEditModel(block="self_identity", content="内容")
    assert edit.mode.value == "replace"
    with pytest.raises(ValueError):
        AgentEditModel(block="self_identity", content="")
    with pytest.raises(ValueError):
        AgentEditModel(block="self_identity", content="x" * 2001)

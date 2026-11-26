"""ORM 模型注册锚——导入全部域 ORM 模块，令 ``Base.metadata`` 完整。

元数据派生操作（人格删除级联 / 结构守卫测试 / Alembic autogenerate）读取
``Base.metadata`` 与 ``Base.registry.mappers``；结果正确性取决于「全部 ORM
模块已被导入」。分散的隐式副作用导入（各服务各自 import 自己域的行类）会
让派生集合随导入顺序静默缩水——本模块是**唯一显式注册入口**：新增 ORM
模块必须加入 :data:`ALL_MODEL_MODULES`，派生面因此始终看到完整表集。

模块导入即完成注册；:func:`load_all_models` 只显式表达调用方意图并返回
注册锚（调用方可用于自检 / 日志）。
"""

from __future__ import annotations

from types import ModuleType
from typing import Final

import packages.harness.canvas.canvas_orm as canvas_orm
import packages.harness.memory.dreaming_orm as dreaming_orm
import packages.harness.memory.orm as memory_orm
import packages.harness.memory.sqlite_store as sqlite_store
import packages.harness.multimodal.sticker_orm as sticker_orm
import packages.harness.persona.core_memory_orm as core_memory_orm
import packages.harness.persona.facts_orm as facts_orm
import packages.harness.persona.orm as persona_orm
import packages.harness.persona.skills_orm as skills_orm
import packages.harness.reminders.orm as reminders_orm
import packages.harness.usage.orm as usage_orm
import packages.harness.voice.voiceprint_orm as voiceprint_orm

__all__ = ["ALL_MODEL_MODULES", "load_all_models"]

#: 全部 ORM 模型模块（导入序即注册序——新增域表须加入本元组）。
ALL_MODEL_MODULES: Final[tuple[ModuleType, ...]] = (
    persona_orm,
    core_memory_orm,
    facts_orm,
    skills_orm,
    memory_orm,
    dreaming_orm,
    sqlite_store,
    sticker_orm,
    voiceprint_orm,
    reminders_orm,
    canvas_orm,
    usage_orm,
)


def load_all_models() -> tuple[ModuleType, ...]:
    """完成全部 ORM 模型注册并返回注册锚模块元组。

    Returns:
        :data:`ALL_MODEL_MODULES`（调用方可将长度 / 模块名用于自检日志）。
    """
    return ALL_MODEL_MODULES

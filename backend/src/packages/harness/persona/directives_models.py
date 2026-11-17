"""persona 域长期指令 Model Classes——常备指令（USER.md 式）的传输契约。

长期指令（D7-14）是主人对人格的**持久行为约定**（如「以后都用简体中文」
「别再熬夜了催我睡觉」）——与一次性记忆的区别是它在每轮对话组装的系统
提示里恒常出现（见 chat compose 链的【长期指令】块）。

存储决策（锁定）：**不新建表**——复用策展记忆域（``ff_curated_memory``）
的 ``kind=user_note`` + ``topic='directive:<slug>'`` 行承载；supersede
语义（同 topic 单 active + 历史链）直接继承，删除即策展行删除。本域只在
其上做**命名空间收窄 + 校验**（topic 前缀 / kind 固定 / provenance 固定
owner），不复制存储实现。

依赖方向：pydantic / 标准库（DTO 层不触 ORM / 不触记忆服务）。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "DIRECTIVE_TOPIC_PREFIX",
    "DIRECTIVE_CONTENT_MAX_LENGTH",
    "DirectiveCreateDTO",
    "DirectiveUpdateDTO",
    "DirectiveRecordDTO",
]

#: 指令 topic 命名空间前缀（策展记忆 user_note 行内收窄——同域其他笔记
#: 不受长期指令面可见性影响）。
DIRECTIVE_TOPIC_PREFIX: str = "directive:"

#: 指令正文长度上限（compose 注入块的单条预算——超长由创建面拒绝）。
DIRECTIVE_CONTENT_MAX_LENGTH: int = 500


class DirectiveCreateDTO(BaseModel):
    """长期指令创建入参（创建 / 覆盖写入面）。

    Attributes:
        content: 指令正文（1-500 字符——首尾空白由服务层裁剪）。
        topic: 主题键（可空——缺省按正文派生 ``directive:<指纹>``；显式
            传入时同 topic 的既有 active 行被 supersede——「编辑」语义
            经此承载）。
    """

    content: str = Field(
        min_length=1, max_length=DIRECTIVE_CONTENT_MAX_LENGTH, description="指令正文（1-500 字符）"
    )
    topic: str | None = Field(
        default=None,
        min_length=1,
        max_length=255,
        description="主题键（可空——缺省按正文派生；显式同 topic 覆盖为 supersede）",
    )


class DirectiveUpdateDTO(BaseModel):
    """长期指令更新入参（同 topic supersede——正文替换）。

    Attributes:
        content: 指令正文（1-500 字符——首尾空白由服务层裁剪）。
    """

    content: str = Field(
        min_length=1, max_length=DIRECTIVE_CONTENT_MAX_LENGTH, description="指令正文（1-500 字符）"
    )


class DirectiveRecordDTO(BaseModel):
    """长期指令记录（策展记忆行的收窄投影——管理面 / 面板读取面）。

    Attributes:
        id: 策展记忆主键（删除 / 更新锚点）。
        topic: 主题键（``directive:`` 前缀——同 topic 覆盖为 supersede）。
        content: 指令正文。
        version: 链内版本号（supersede 递增——面板展示 vN）。
        created_at: 落库时间（epoch 毫秒）。
        updated_at: 行更新时间（epoch 毫秒）。
    """

    model_config = ConfigDict(from_attributes=True)

    id: int = Field(description="策展记忆主键")
    topic: str = Field(description="主题键（directive: 前缀）")
    content: str = Field(description="指令正文")
    version: int = Field(description="链内版本号")
    created_at: int = Field(description="落库时间（epoch 毫秒）")
    updated_at: int = Field(description="行更新时间（epoch 毫秒）")

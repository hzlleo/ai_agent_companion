"""persona 域 DTO——创建 / 更新 / 复制 / 记录 / 卡片 / 状态 / 查询 / 批量
删除的传输契约（Unit 文件）。

契约分层：

    - :class:`PersonaCreateDTO`           —— 创建面（注册名 + 卡片）；
    - :class:`CharaCardPatchDTO`          —— 卡片补丁（部分合并面——仅
      出现的字段覆写既有卡片，其余原样保留）；
    - :class:`PersonaUpdateDTO`           —— 更新面（重命名 / 展示名 /
      状态 / 卡片补丁；**不含 is_active**——单激活位仅经 activate /
      deactivate 事务性操作）；
    - :class:`PersonaCopyDTO`             —— 复制面（新注册名 + 是否深拷贝
      M/S 状态；新人格恒 disabled）；
    - :class:`PersonaRecordDTO`           —— 读取面（ORM 行 → DTO）；
    - :class:`PersonaCardDTO`             —— 卡片端点读取面（GET / PUT
      ``/{id}/card`` 的响应包裹）；
    - :class:`PersonaStateUpsertDTO`      —— 状态写入面（layer 由服务参数
      / 路径参数承载）；
    - :class:`PersonaStateDTO`            —— 状态读取面（ORM 行 → DTO）；
    - :class:`PersonaQueryDTO`            —— 列表查询（status 过滤 + 游标
      分页）；
    - :class:`PersonaBatchDeleteRequest` / :class:`PersonaBatchDeleteItemDTO`
      / :class:`PersonaBatchDeleteResponseDTO` —— 批量删除（部分成功形态）。
"""

from __future__ import annotations

from typing import Any

from pydantic import AliasChoices, BaseModel, ConfigDict, Field

from packages.harness.persona.chara_card import CharaBookModel, CharaCardModel
from packages.harness.persona.models import PersonaStateLayer, PersonaStatus

__all__ = [
    "PersonaCreateDTO",
    "CharaCardPatchDTO",
    "PersonaUpdateDTO",
    "PersonaCopyDTO",
    "PersonaRecordDTO",
    "PersonaCardDTO",
    "PersonaStateUpsertDTO",
    "PersonaStateDTO",
    "PersonaQueryDTO",
    "PersonaBatchDeleteRequest",
    "PersonaBatchDeleteItemDTO",
    "PersonaBatchDeleteResponseDTO",
]


class PersonaCreateDTO(BaseModel):
    """人格创建 DTO（写入面）。

    Attributes:
        name: 注册名（全局唯一；1-128 字符）。
        display_name: 展示名；None 时回落 ``card.name``。
        status: 可用性状态（默认 disabled——启用是显式管理动作）。
        card: 人格卡片（chara card v2 文档；必填——无卡片的人格无意义）。
    """

    name: str = Field(min_length=1, max_length=128, description="注册名（全局唯一）")
    display_name: str | None = Field(
        default=None, min_length=1, max_length=128, description="展示名（缺省回落 card.name）"
    )
    status: PersonaStatus = Field(
        default=PersonaStatus.DISABLED, description="可用性状态（默认 disabled）"
    )
    card: CharaCardModel = Field(description="人格卡片（chara card v2）")


class CharaCardPatchDTO(BaseModel):
    """卡片补丁 DTO（部分合并面——仅出现的字段覆写既有卡片）。

    与 :class:`CharaCardModel` 字段一一对应但全部可选；合并语义在服务层
    实现（``既有卡片 dump ⊕ 补丁 exclude_unset dump`` 后整体重新校验）。
    ``character_book`` 提供时**整体替换**世界书（不做条目级合并）。

    Attributes:
        name: 人格称呼名（提供时须非空——合并校验兜底）。
        description: 人格简介。
        personality: 性格描述。
        scenario: 场景设定。
        first_mes: 开场白。
        mes_example: 对话示例。
        system_prompt: 系统提示覆写。
        post_history_instructions: 历史后指令。
        character_book: 世界书（提供时整体替换；显式 null 清除世界书）。
    """

    model_config = ConfigDict(protected_namespaces=())

    name: str | None = Field(default=None, min_length=1, max_length=128, description="人格称呼名")
    description: str | None = Field(default=None, description="人格简介")
    personality: str | None = Field(default=None, description="性格描述")
    scenario: str | None = Field(default=None, description="场景设定")
    first_mes: str | None = Field(default=None, description="开场白")
    mes_example: str | None = Field(default=None, description="对话示例")
    system_prompt: str | None = Field(default=None, description="系统提示覆写")
    post_history_instructions: str | None = Field(default=None, description="历史后指令")
    character_book: CharaBookModel | None = Field(default=None, description="世界书（整体替换）")


class PersonaUpdateDTO(BaseModel):
    """人格更新 DTO（部分更新面——仅出现的字段生效）。

    **不含 ``is_active``**：单激活位是事务性切换不变式，仅经
    ``activate`` / ``deactivate`` 操作面管理（绕过会破坏「至多一个激活」
    保证）。status 切到 disabled 时若该人格持有激活位，服务层自动清位
    （停用即下线）。

    Attributes:
        name: 新注册名（重命名；全局唯一校验）。
        display_name: 新展示名。
        status: 新可用性状态。
        card: 卡片补丁（仅出现字段覆写；不提供则卡片原样）。
    """

    name: str | None = Field(
        default=None, min_length=1, max_length=128, description="新注册名（重命名）"
    )
    display_name: str | None = Field(
        default=None, min_length=1, max_length=128, description="新展示名"
    )
    status: PersonaStatus | None = Field(default=None, description="新可用性状态")
    card: CharaCardPatchDTO | None = Field(default=None, description="卡片补丁")


class PersonaCopyDTO(BaseModel):
    """人格复制 DTO（写入面）。

    Attributes:
        new_name: 新注册名（全局唯一校验；与源名不同）。
        display_name: 展示名；None 时承接源人格展示名。
        include_states: 是否深拷贝 M/S 状态层（默认 False——新人格从
            空状态起步）。
    """

    new_name: str = Field(min_length=1, max_length=128, description="新注册名（全局唯一）")
    display_name: str | None = Field(
        default=None, min_length=1, max_length=128, description="展示名（缺省承接源人格）"
    )
    include_states: bool = Field(default=False, description="是否深拷贝 M/S 状态层")


class PersonaRecordDTO(BaseModel):
    """人格记录读取 DTO（ORM 行归一）。

    Attributes:
        id: 人格主键（BigInt）。
        name: 注册名（全局唯一）。
        display_name: 展示名。
        status: 可用性状态。
        is_active: 单激活绑定位。
        card: 人格卡片（chara card v2 文档）。
        created_at: 创建时间（epoch 毫秒）。
        updated_at: 更新时间（epoch 毫秒）。
    """

    model_config = ConfigDict(from_attributes=True, protected_namespaces=())

    id: int = Field(description="人格主键")
    name: str = Field(description="注册名（全局唯一）")
    display_name: str = Field(description="展示名")
    status: PersonaStatus = Field(description="可用性状态")
    is_active: bool = Field(description="单激活绑定位")
    card: CharaCardModel = Field(
        validation_alias=AliasChoices("card", "chara_card"),
        description="人格卡片",
    )
    created_at: int = Field(description="创建时间（epoch 毫秒）")
    updated_at: int = Field(description="更新时间（epoch 毫秒）")


class PersonaCardDTO(BaseModel):
    """人格卡片端点读取 DTO（GET / PUT ``/{id}/card`` 响应包裹）。

    Attributes:
        persona_id: 人格主键。
        card: 人格卡片（chara card v2 文档）。
        updated_at: 卡片最近更新时间（epoch 毫秒——即行 updated_at）。
    """

    persona_id: int = Field(description="人格主键")
    card: CharaCardModel = Field(description="人格卡片")
    updated_at: int = Field(description="卡片最近更新时间（epoch 毫秒）")


class PersonaStateUpsertDTO(BaseModel):
    """人格状态写入 DTO（upsert 面——layer 由路径参数 / 服务参数承载）。

    Attributes:
        state: 状态 JSON 文档（整体替换式更新——非字段级合并）。
    """

    state: dict[str, Any] = Field(description="状态 JSON 文档（整体替换）")


class PersonaStateDTO(BaseModel):
    """人格状态读取 DTO（ORM 行归一）。

    Attributes:
        persona_id: 所属人格主键。
        layer: 状态层（M=中期心情/关系；S=短期会话状态）。
        state: 状态 JSON 文档。
        created_at: 创建时间（epoch 毫秒）。
        updated_at: 更新时间（epoch 毫秒）。
    """

    model_config = ConfigDict(from_attributes=True, protected_namespaces=())

    persona_id: int = Field(description="所属人格主键")
    layer: PersonaStateLayer = Field(description="状态层（M/S）")
    state: dict[str, Any] = Field(description="状态 JSON 文档")
    created_at: int = Field(description="创建时间（epoch 毫秒）")
    updated_at: int = Field(description="更新时间（epoch 毫秒）")


class PersonaQueryDTO(BaseModel):
    """人格列表查询 DTO（status 过滤 + 游标分页——id 降序，新人格在前）。

    Attributes:
        status: 可用性状态过滤（None = 全部）。
        cursor: 上一页返回的不透明游标（None = 首页）。
        limit: 页大小（1 - 200，默认 20）。
    """

    status: PersonaStatus | None = Field(default=None, description="可用性状态过滤")
    cursor: str | None = Field(default=None, description="分页游标（首页为 None）")
    limit: int = Field(default=20, ge=1, le=200, description="页大小")


class PersonaBatchDeleteRequest(BaseModel):
    """批量删除人格请求（部分成功形态——逐 id 独立事务）。

    Attributes:
        ids: 待删除人格主键列表（1 - 100 个；含当前激活人格的 id 会导致
            该项失败——激活人格删除守卫见服务层）。
    """

    ids: list[int] = Field(
        min_length=1,
        max_length=100,
        description="待删除人格主键列表（1-100 个）",
    )


class PersonaBatchDeleteItemDTO(BaseModel):
    """批量删除结果单条目。

    Attributes:
        id: 人格主键。
        success: 是否删除成功。
        code: 失败错误码（成功为 None）。
        message: 面向用户的提示信息。
    """

    id: int = Field(description="人格主键")
    success: bool = Field(description="是否删除成功")
    code: str | None = Field(default=None, description="失败错误码（成功为 None）")
    message: str = Field(default="", description="提示信息")


class PersonaBatchDeleteResponseDTO(BaseModel):
    """批量删除响应（部分成功形态）。

    Attributes:
        total: 提交总数。
        succeeded: 成功数量。
        failed: 失败数量。
        items: 逐 id 结果列表（与提交顺序一致）。
    """

    total: int = Field(description="提交总数")
    succeeded: int = Field(description="成功数量")
    failed: int = Field(description="失败数量")
    items: list[PersonaBatchDeleteItemDTO] = Field(default_factory=list, description="逐 id 结果")

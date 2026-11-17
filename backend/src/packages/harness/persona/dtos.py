"""persona 域 DTO——创建 / 更新 / 复制 / 记录 / 卡片 / 状态 / 查询 / 批量
删除 / 导入 / 特质库的传输契约（Unit 文件）。

契约分层：

    - :class:`PersonaCreateDTO`           —— 创建面（注册名 + 卡片）；
    - :class:`CharaCardPatchDTO`          —— 卡片补丁（部分合并面——仅
      出现的字段覆写既有卡片，其余原样保留）；
    - :class:`PersonaUpdateDTO`           —— 更新面（重命名 / 展示名 /
      状态 / 卡片补丁；**不含主人格标记**——主人格仅经 set-primary /
      事务性切换操作）；
    - :class:`PersonaCopyDTO`             —— 复制面（新注册名 + 是否深拷贝
      M/S 状态；新人格恒 disabled + 非主人格）；
    - :class:`PersonaRecordDTO`           —— 读取面（ORM 行 → DTO；
      ``is_primary`` 为主人格归一布尔位）；
    - :class:`PersonaCardDTO`             —— 卡片端点读取面（GET / PUT
      ``/{id}/card`` 的响应包裹）；
    - :class:`PersonaStateUpsertDTO`      —— 状态写入面（layer 由服务参数
      / 路径参数承载）；
    - :class:`PersonaStateDTO`            —— 状态读取面（ORM 行 → DTO）；
    - :class:`PersonaQueryDTO`            —— 列表查询（status 过滤 + 游标
      分页）；
    - :class:`PersonaBatchDeleteRequest` / :class:`PersonaBatchDeleteItemDTO`
      / :class:`PersonaBatchDeleteResponseDTO` —— 批量删除（部分成功形态）；
    - :class:`PersonaImportRequestDTO` / :class:`PersonaImportResultModel`
      —— 导入面（JSON 请求：官方预设 id 或用户卡片文档 + 冲突策略）；
    - :class:`PersonaImportBatchFileDTO`  —— 批量导入的内部装配面
      （multipart 逐文件解析产物——非前端 wire 类型）；
    - :class:`PersonaImportBatchItemDTO` / :class:`PersonaImportBatchResultDTO`
      —— 批量导入结果（部分成功形态）；
    - :class:`TraitLibraryDTO`            —— 特质库读取面（官方预设 +
      组件注册表的画廊载荷）。
"""

from __future__ import annotations

from typing import Any

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, model_validator

from packages.harness.persona.chara_card import CharaBookModel, CharaCardModel
from packages.harness.persona.models import (
    PersonaImportConflictStrategy,
    PersonaImportReason,
    PersonaStateLayer,
    PersonaStatus,
)
from packages.harness.persona.trait_library import (
    TraitComponentModel,
    TraitPresetModel,
)
from packages.harness.voice.voice_models import VoiceProfileModel

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
    "PersonaImportRequestDTO",
    "PersonaImportResultModel",
    "PersonaImportBatchFileDTO",
    "PersonaImportBatchItemDTO",
    "PersonaImportBatchResultDTO",
    "TraitLibraryDTO",
]


class PersonaCreateDTO(BaseModel):
    """人格创建 DTO（写入面）。

    Attributes:
        name: 注册名（全局唯一；1-128 字符）。
        display_name: 展示名；None 时回落 ``card.name``。
        status: 可用性状态（默认 disabled——启用是显式管理动作）。
        card: 人格卡片（chara card v2 文档；必填——无卡片的人格无意义）。
        voice_profile: 人格语音绑定（None = 不绑定——合成回落 settings
            缺省发音人）。
    """

    name: str = Field(min_length=1, max_length=128, description="注册名（全局唯一）")
    display_name: str | None = Field(
        default=None, min_length=1, max_length=128, description="展示名（缺省回落 card.name）"
    )
    status: PersonaStatus = Field(
        default=PersonaStatus.DISABLED, description="可用性状态（默认 disabled）"
    )
    card: CharaCardModel = Field(description="人格卡片（chara card v2）")
    voice_profile: VoiceProfileModel | None = Field(
        default=None, description="人格语音绑定（None = 不绑定）"
    )


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

    **不含主人格标记**：主人格是事务性切换不变式（DB 唯一键强制至多
    一个），仅经 ``set-primary`` 操作面管理（绕过会破坏「至多一个主人格」
    保证）。status 切到 disabled 时若该人格是当前主人格，服务层守卫
    拒绝（先另设主人格再停用——两步显式）。

    Attributes:
        name: 新注册名（重命名；全局唯一校验）。
        display_name: 新展示名。
        status: 新可用性状态（切 disabled 时主人格守卫拒绝）。
        card: 卡片补丁（仅出现字段覆写；不提供则卡片原样）。
        voice_profile: 人格语音绑定（**出现即整体替换**——显式 null 清除
            绑定；不提供则原样保留；与卡片补丁的 exclude_unset 语义不同：
            语音绑定是单对象面，无字段级合并）。
    """

    name: str | None = Field(
        default=None, min_length=1, max_length=128, description="新注册名（重命名）"
    )
    display_name: str | None = Field(
        default=None, min_length=1, max_length=128, description="新展示名"
    )
    status: PersonaStatus | None = Field(default=None, description="新可用性状态")
    card: CharaCardPatchDTO | None = Field(default=None, description="卡片补丁")
    voice_profile: VoiceProfileModel | None = Field(
        default=None, description="语音绑定（出现即替换；显式 null 清除）"
    )


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
        status: 可用性状态（enabled 人格构成可寻址集）。
        is_primary: 是否为主人格（全表至多一个 True——DB 唯一键强制）。
        card: 人格卡片（chara card v2 文档）。
        voice_profile: 人格语音绑定（None = 未绑定）。
        created_at: 创建时间（epoch 毫秒）。
        updated_at: 更新时间（epoch 毫秒）。
    """

    model_config = ConfigDict(from_attributes=True, protected_namespaces=())

    id: int = Field(description="人格主键")
    name: str = Field(description="注册名（全局唯一）")
    display_name: str = Field(description="展示名")
    status: PersonaStatus = Field(description="可用性状态")
    is_primary: bool = Field(description="是否为主人格（至多一个 True）")
    card: CharaCardModel = Field(
        validation_alias=AliasChoices("card", "chara_card"),
        description="人格卡片",
    )
    voice_profile: VoiceProfileModel | None = Field(
        default=None,
        description="人格语音绑定（None = 未绑定）",
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
        ids: 待删除人格主键列表（1 - 100 个；含当前主人格的 id 会导致
            该项失败——主人格删除守卫见服务层）。
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


class PersonaImportRequestDTO(BaseModel):
    """人格导入请求 DTO（JSON 面——multipart 面经同一模型归一）。

    载荷三选一（模型校验强制恰一）：``preset_id``（官方预设导入——
    服务端确定性生成卡片）或 ``card``（用户自有卡片文档导入）；
    冲突策略与期望名对两种载荷通用。multipart 面由 API 层解析后经
    本模型归一（``preset_id`` 恒 None——文件导入即卡片载荷）。

    Attributes:
        preset_id: 官方预设 id（与 ``card`` 恰一提供；提供时服务端按
            预设 + 组件库生成卡片）。
        card: 用户自有 chara card v2 卡片文档（与 ``preset_id`` 恰一
            提供）。
        conflict_strategy: 名称冲突策略（缺省 skip——内容同卡恒幂等，
            策略不适用）。
        desired_name: 期望注册名（None = 卡片名 / 预设名；1-128 字符）。

    Raises:
        ValueError: ``preset_id`` 与 ``card`` 同时提供或均未提供（载荷
            恰一约束）。
    """

    model_config = ConfigDict(protected_namespaces=())

    preset_id: str | None = Field(
        default=None, min_length=2, max_length=64, description="官方预设 id"
    )
    card: CharaCardModel | None = Field(default=None, description="用户自有卡片文档")
    conflict_strategy: PersonaImportConflictStrategy = Field(
        default=PersonaImportConflictStrategy.SKIP, description="名称冲突策略（缺省 skip）"
    )
    desired_name: str | None = Field(
        default=None, min_length=1, max_length=128, description="期望注册名（None = 卡片名）"
    )

    @model_validator(mode="after")
    def _validate_payload_exclusivity(self) -> PersonaImportRequestDTO:
        """载荷恰一约束：preset_id 与 card 须恰提供一个。

        Returns:
            校验通过的实例。

        Raises:
            ValueError: 两者同时提供（载荷歧义）或均未提供（空导入）。
        """
        if self.preset_id is not None and self.card is not None:
            raise ValueError("preset_id 与 card 须恰提供一个（同时提供载荷歧义）")
        if self.preset_id is None and self.card is None:
            raise ValueError("preset_id 与 card 须恰提供一个（空导入无意义）")
        return self


class PersonaImportResultModel(BaseModel):
    """人格导入结果（单发面——含幂等 / 冲突未导入形态）。

    Attributes:
        imported: 是否实际落库（False = 幂等跳过或名称冲突未导入）。
        persona_id: 关联人格主键（导入成功 = 新行 / 覆盖行；幂等 =
            同卡既有人格；名称冲突 skip = 占用名人格；None = 未关联）。
        reason: 未导入原因（None = 已导入；identical_content = 内容
            同卡幂等；name_conflict = 名称冲突被跳过）。
        warnings: 生成警告文案列表（预设导入的组件冲突软警告等；
            空列表 = 无）。
    """

    model_config = ConfigDict(protected_namespaces=())

    imported: bool = Field(description="是否实际落库")
    persona_id: int | None = Field(
        default=None, description="关联人格主键（新行 / 覆盖行 / 既有行）"
    )
    reason: PersonaImportReason | None = Field(
        default=None, description="未导入原因（None = 已导入）"
    )
    warnings: list[str] = Field(default_factory=list, description="生成警告文案列表")


class PersonaImportBatchFileDTO(BaseModel):
    """批量导入单文件装配面（multipart 解析产物——内部形态非 wire）。

    Attributes:
        filename: 文件名（结果呈现与定位用）。
        card: 解析成功的卡片文档（None = 解析失败——见 ``error_message``）。
        error_message: 解析失败原因（成功为 None）。
    """

    model_config = ConfigDict(protected_namespaces=())

    filename: str = Field(description="文件名")
    card: CharaCardModel | None = Field(default=None, description="解析成功的卡片文档")
    error_message: str | None = Field(default=None, description="解析失败原因")


class PersonaImportBatchItemDTO(BaseModel):
    """批量导入结果单条目（部分成功形态——逐文件）。

    Attributes:
        filename: 文件名（与提交顺序一致）。
        success: 请求是否处理成功（解析 / 落库无异常；冲突跳过与幂等
            属合法结果——success 仍为 True）。
        imported: 是否实际导入（False + success=True = 幂等 / 冲突跳过）。
        persona_id: 关联人格主键（None = 未关联）。
        reason: 未导入原因（None = 已导入或处理失败）。
        code: 失败错误码（success=True 时为 None）。
        message: 面向用户的提示信息。
    """

    model_config = ConfigDict(protected_namespaces=())

    filename: str = Field(description="文件名")
    success: bool = Field(description="请求是否处理成功")
    imported: bool = Field(default=False, description="是否实际导入")
    persona_id: int | None = Field(default=None, description="关联人格主键")
    reason: PersonaImportReason | None = Field(default=None, description="未导入原因")
    code: str | None = Field(default=None, description="失败错误码（成功为 None）")
    message: str = Field(default="", description="提示信息")


class PersonaImportBatchResultDTO(BaseModel):
    """批量导入结果（部分成功形态——逐文件独立事务）。

    Attributes:
        total: 提交文件总数。
        succeeded: 处理成功数（含幂等 / 冲突跳过的合法结果）。
        failed: 处理失败数（解析失败 / 内部错误）。
        imported_count: 实际新导入数（succeeded 中 imported=True 的数量）。
        items: 逐文件结果列表（与提交顺序一致）。
    """

    model_config = ConfigDict(protected_namespaces=())

    total: int = Field(description="提交文件总数")
    succeeded: int = Field(description="处理成功数（含合法跳过）")
    failed: int = Field(description="处理失败数")
    imported_count: int = Field(description="实际新导入数")
    items: list[PersonaImportBatchItemDTO] = Field(
        default_factory=list, description="逐文件结果"
    )


class TraitLibraryDTO(BaseModel):
    """特质库读取面（画廊载荷——官方预设 + 组件注册表）。

    Attributes:
        presets: 官方预设列表（数据文件声明序）。
        components: 全量官方组件列表（数据文件声明序；前端按 id 建
            映射解析预设的组件芯片）。
    """

    model_config = ConfigDict(protected_namespaces=())

    presets: list[TraitPresetModel] = Field(description="官方预设列表")
    components: list[TraitComponentModel] = Field(description="全量官方组件列表")

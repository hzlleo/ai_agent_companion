"""chara card v2 规格模型——人格卡片载荷（Unit 文件）。

chara card v2 是人格的**长期身份正典（L 层）**：定义「这个人格是谁」
的全部不可变设定，作为 JSON 文档整体落库在 ``ff_personas.chara_card``
列（由 :class:`PersonaRow` 持有，经 Pydantic Model Classes 在 DTO 边界
校验后序列化）。

字段面（v2 规格）：

    - 基础叙事：``name`` / ``description`` / ``personality`` / ``scenario``；
    - 对话行为：``first_mes``（开场白）/ ``mes_example``（对话示例）；
    - 系统覆写：``system_prompt`` / ``post_history_instructions``；
    - 世界书：``character_book``（检索增强条目集，见
      :class:`CharaBookModel`）。

校验约定（本工程锁定）：

    - ``name`` 非空（人格必须可称呼）；
    - 世界书条目 ``insertion_order`` 界 ``0 <= insertion_order <= 9999``
      （值越大越靠后插入提示词；上界防无意义巨值挤爆 token 预算）；
    - ``secondary_keys`` 仅在 ``selective=True``（选择性触发）时有意义——
      非选择性条目携带非空 ``secondary_keys`` 视为规格错误；
    - ``position`` 取 v2 字符串形态（``before_char`` / ``after_char``）。
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

__all__ = [
    "CharaBookPosition",
    "CharaBookEntryModel",
    "CharaBookModel",
    "CharaCardModel",
]


class CharaBookPosition(StrEnum):
    """世界书条目插入位置值域（v2 字符串形态）。

    - ``before_char``：插在角色定义之前（背景 / 世界观类信息）；
    - ``after_char``：插在角色定义之后（行为补充 / 语料示例类信息）。
    """

    BEFORE_CHAR = "before_char"
    AFTER_CHAR = "after_char"


class CharaBookEntryModel(BaseModel):
    """世界书条目（单条检索增强设定）。

    触发语义：``keys`` 任一命中即插入；``selective=True`` 时进一步要求
    ``secondary_keys`` 任一命中（选择性触发——主键定义话题、副键收敛
    语境）；``constant=True`` 恒插入（免触发，世界观保底条目）。

    Attributes:
        keys: 主触发关键词列表（非空——无触发面的条目应为 constant）。
        secondary_keys: 副触发关键词列表（仅 ``selective=True`` 时有意义）。
        content: 条目正文（插入提示词的设定文本）。
        insertion_order: 插入顺序（0-9999，值越大越靠后）。
        constant: 是否恒插入（默认 False——按关键词触发）。
        case_sensitive: 关键词匹配是否区分大小写（默认 False）。
        selective: 是否选择性触发（默认 False——主键命中即插入）。
        position: 插入位置（默认 ``after_char``——角色定义之后）。
    """

    model_config = ConfigDict(protected_namespaces=())

    keys: list[str] = Field(default_factory=list, description="主触发关键词列表")
    secondary_keys: list[str] = Field(
        default_factory=list, description="副触发关键词列表（selective=True 时参与收敛）"
    )
    content: str = Field(default="", description="条目正文")
    insertion_order: int = Field(
        default=100,
        ge=0,
        le=9999,
        description="插入顺序（0-9999，值越大越靠后插入提示词）",
    )
    constant: bool = Field(default=False, description="是否恒插入（免触发）")
    case_sensitive: bool = Field(default=False, description="关键词匹配是否区分大小写")
    selective: bool = Field(default=False, description="是否选择性触发")
    position: CharaBookPosition = Field(
        default=CharaBookPosition.AFTER_CHAR, description="插入位置"
    )

    @model_validator(mode="after")
    def _validate_selective_keys(self) -> CharaBookEntryModel:
        """规格一致性：非选择性条目不得携带副触发键。

        Returns:
            校验通过的实例。

        Raises:
            ValueError: ``selective=False`` 但 ``secondary_keys`` 非空
                （副键仅在选择触发语义下生效，携带即规格错误）。
        """
        if not self.selective and self.secondary_keys:
            raise ValueError("secondary_keys 仅在 selective=True（选择性触发）时可携带")
        return self


class CharaBookModel(BaseModel):
    """世界书（检索增强条目集）。

    Attributes:
        name: 世界书名称。
        description: 世界书描述。
        scan_depth: 扫描深度（最近 N 条消息内检索关键词；None = 不限）。
        token_budget: 条目 token 预算（超预算时按 insertion_order 截断；
            None = 不限）。
        recursive_scanning: 是否递归扫描（条目 content 中的命中词再触发
            其他条目；默认 False）。
        entries: 条目列表（按 ``insertion_order`` 升序决定插入次序）。
    """

    model_config = ConfigDict(protected_namespaces=())

    name: str = Field(default="", description="世界书名称")
    description: str = Field(default="", description="世界书描述")
    scan_depth: int | None = Field(default=None, ge=1, description="扫描深度（最近 N 条消息）")
    token_budget: int | None = Field(default=None, ge=1, description="条目 token 预算")
    recursive_scanning: bool = Field(default=False, description="是否递归扫描")
    entries: list[CharaBookEntryModel] = Field(default_factory=list, description="条目列表")


class CharaCardModel(BaseModel):
    """chara card v2 人格卡片——长期身份正典（L 层）。

    作为 JSON 文档整体存储在 ``ff_personas.chara_card`` 列；会话运行期
    **不可变**（编辑即管理面操作，经 PUT 端点整体替换或补丁合并）。
    心情 / 关系等可变状态不在此处——见 L/M/S 模型（M/S 层落
    ``ff_persona_states``）。

    Attributes:
        name: 人格称呼名（非空——v2 规格核心字段）。
        description: 人格简介（外观 / 背景 / 身份概述）。
        personality: 性格描述（语气 / 行为倾向 / 价值观）。
        scenario: 场景设定（当前处境与关系背景）。
        first_mes: 开场白（会话首条人格消息）。
        mes_example: 对话示例（few-shot 语态锚定）。
        system_prompt: 系统提示覆写（非空时整体覆写默认人格 system 模板）。
        post_history_instructions: 历史后指令（拼在对话历史之后的强约束）。
        character_book: 世界书（检索增强条目集；None = 无世界书）。
    """

    model_config = ConfigDict(protected_namespaces=())

    name: str = Field(min_length=1, max_length=128, description="人格称呼名（非空）")
    description: str = Field(default="", description="人格简介")
    personality: str = Field(default="", description="性格描述")
    scenario: str = Field(default="", description="场景设定")
    first_mes: str = Field(default="", description="开场白")
    mes_example: str = Field(default="", description="对话示例")
    system_prompt: str = Field(default="", description="系统提示覆写")
    post_history_instructions: str = Field(default="", description="历史后指令")
    character_book: CharaBookModel | None = Field(default=None, description="世界书")

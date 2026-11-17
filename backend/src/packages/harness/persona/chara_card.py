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
    - ``secondary_keys`` 仅在 ``selective=True``（选择性触发）时有意义
      ——非选择性条目携带非空 ``secondary_keys`` 视为规格错误；
      ``selective=True`` 但无任何非空副键同样拒绝（空副键永不触发
      ——选择性条目将永远无法注入，静默失效在构造期 fail loud）；
    - ``position`` 取 v2 字符串形态（``before_char`` / ``after_char``）。

R2b 高级触发扩展（全部加性字段、带缺省——既有卡片零改动保持合法，
缺省值域 = 既有行为；筛选算法链见 chat 域 ``world_book`` 模块）：

    - ``depth``：条目在渲染段内的深度槽位（0-20，缺省 4）；
    - ``recursive``：条目正文参与递归扫描面（须书级
      ``recursive_scanning=True`` 总开关——错配在构造期 fail loud）；
    - ``include_group``：互斥分组名（同组命中条目一次组装只注入优先级
      最高者；``constant`` 条目无视分组恒注入）；
    - ``trigger_probability``：触发概率门（0.0-1.0，缺省 1.0 恒过门）；
    - ``cooldown_turns``：注入后冷却回合数（0-50，缺省 0 不冷却；
      会话作用域追踪——重启即复位，无持久化）。
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
    语境）；``constant=True`` 恒插入（免触发，世界观保底条目——恒插入
    条目无视分组互斥 / 概率门 / 冷却门，直接进入候选集）。

    R2b 高级字段（全部缺省即既有行为）：

        - ``depth`` 声明条目距对话末尾的近因深度（0 = 最贴近末尾），
          在渲染段内落位为深度槽（越小越靠段尾——段尾紧邻对话历史）；
        - ``recursive`` 声明本条目正文可否在递归扫描面再触发其他条目
          （书级 ``recursive_scanning`` 为总开关——本字段为逐条目收窄）；
        - ``include_group`` 声明互斥分组（同组非 constant 命中条目一次
          组装只注入优先级最高者：``insertion_order`` 升序、同值按条目
          索引升序——确定性文档化）；
        - ``trigger_probability`` 声明概率门（仅非 constant 命中条目过
          门；缺省 1.0 = 恒过门不消耗随机数）；
        - ``cooldown_turns`` 声明注入后冷却（仅非 constant 命中条目过
          门；会话作用域内存追踪，重启即复位——无持久化，诚实文档化）。

    Attributes:
        keys: 主触发关键词列表（非空——无触发面的条目应为 constant）。
        secondary_keys: 副触发关键词列表（仅 ``selective=True`` 时有意义）。
        content: 条目正文（插入提示词的设定文本）。
        insertion_order: 插入顺序（0-9999，值越大越靠后）。
        constant: 是否恒插入（默认 False——按关键词触发）。
        case_sensitive: 关键词匹配是否区分大小写（默认 False）。
        selective: 是否选择性触发（默认 False——主键命中即插入）。
        position: 插入位置（默认 ``after_char``——角色定义之后）。
        depth: 深度槽位（0-20，默认 4；段内排序首选键——值越大越靠段
            首、越远离对话末尾，0 为段尾槽；同槽回退 insertion_order）。
        recursive: 条目正文是否参与递归扫描面（默认 False；须书级
            ``recursive_scanning=True``，否则构造期拒绝）。
        include_group: 互斥分组名（默认 None = 不分组；空白串归一为
            None；同组一次组装只注入优先级最高的命中成员）。
        trigger_probability: 触发概率（0.0-1.0，默认 1.0 恒注入）。
        cooldown_turns: 注入后冷却回合数（0-50，默认 0 不冷却；回合
            计数为组装调用次数，会话作用域内存追踪）。
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
    depth: int = Field(
        default=4,
        ge=0,
        le=20,
        description="深度槽位（0-20；段内越大越靠段首，0 为段尾槽——同槽按 insertion_order）",
    )
    recursive: bool = Field(
        default=False,
        description="条目正文参与递归扫描面（须书级 recursive_scanning=True）",
    )
    include_group: str | None = Field(
        default=None,
        max_length=64,
        description="互斥分组名（None = 不分组；同组命中条目一次组装只注入最高优先级者）",
    )
    trigger_probability: float = Field(
        default=1.0,
        ge=0.0,
        le=1.0,
        description="触发概率（非 constant 命中条目的概率门；1.0 恒过门）",
    )
    cooldown_turns: int = Field(
        default=0,
        ge=0,
        le=50,
        description="注入后冷却回合数（回合 = 组装调用；会话作用域内存追踪）",
    )

    @model_validator(mode="after")
    def _validate_selective_keys(self) -> CharaBookEntryModel:
        """规格一致性：副触发键仅在选择性触发语义下且非空时有效。

        兼任 ``include_group`` 归一化：空白分组名归一为 ``None``（前端
        空输入直通本字段时的形态收敛——空串与 None 同义，构造期归一
        而非运行期分支）。

        Returns:
            校验通过的实例。

        Raises:
            ValueError: ``selective=False`` 但 ``secondary_keys`` 非空
                （副键仅在选择触发语义下生效，携带即规格错误）；
                ``selective=True`` 但 ``secondary_keys`` 无任何非空键
                （空副键永不触发——选择性条目将永远无法注入，静默失效
                形态在构造期拒绝）。
        """
        if not self.selective and self.secondary_keys:
            raise ValueError("secondary_keys 仅在 selective=True（选择性触发）时可携带")
        if self.selective and not any(self.secondary_keys):
            raise ValueError(
                "selective=True 条目必须携带非空 secondary_keys（空副键永不触发——条目将永远无法注入）"
            )
        if self.include_group is not None and not self.include_group.strip():
            # 空白分组名归一 None（与「未分组」同义——避免空串成为事实分组键）。
            self.include_group = None
        return self


class CharaBookModel(BaseModel):
    """世界书（检索增强条目集）。

    Attributes:
        name: 世界书名称。
        description: 世界书描述。
        scan_depth: 扫描深度（最近 N 条消息内检索关键词；None = 不限）。
        token_budget: 条目 token 预算（超预算时按渲染序截断；None = 不限）。
        recursive_scanning: 递归扫描总开关（条目 content 中的命中词再触发
            其他条目；逐条目收窄经条目级 ``recursive``——两级同时开启
            才生效；默认 False）。
        entries: 条目列表（按 ``insertion_order`` 升序决定插入次序）。

    Raises:
        ValueError: 条目携带 ``recursive=True`` 但书级
            ``recursive_scanning=False``（逐条目递归须总开关放行——
            静默失效形态在构造期 fail loud）。
    """

    model_config = ConfigDict(protected_namespaces=())

    name: str = Field(default="", description="世界书名称")
    description: str = Field(default="", description="世界书描述")
    scan_depth: int | None = Field(default=None, ge=1, description="扫描深度（最近 N 条消息）")
    token_budget: int | None = Field(default=None, ge=1, description="条目 token 预算")
    recursive_scanning: bool = Field(default=False, description="递归扫描总开关")
    entries: list[CharaBookEntryModel] = Field(default_factory=list, description="条目列表")

    @model_validator(mode="after")
    def _validate_recursive_needs_book_switch(self) -> CharaBookModel:
        """规格一致性：逐条目递归须书级总开关放行。

        Returns:
            校验通过的实例。

        Raises:
            ValueError: 任一条目 ``recursive=True`` 但书级
                ``recursive_scanning=False``（总开关关闭时逐条目递归
                声明将静默失效——构造期拒绝）。
        """
        if not self.recursive_scanning and any(entry.recursive for entry in self.entries):
            raise ValueError(
                "条目 recursive=True 须书级 recursive_scanning=True 总开关放行（否则递归声明静默失效）"
            )
        return self


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

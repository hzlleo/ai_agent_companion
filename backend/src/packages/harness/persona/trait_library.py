"""特质组件库——可组合人格预设的 schema 与确定性卡片生成（Unit 文件）。

本模块是特质组件库的核心：官方预设库以**纯特质组合集**形态发行
（零角色名 / 零背景故事 / 零既有形象引用——版权洁净是设计目标，
用户经文件导入端点私有导入自己的完整卡片）。两层结构：

    - :class:`TraitComponentModel` —— 特质组件（最小可复用人格碎片：
      温性 / 言语风格 / 习惯癖性 / 价值取向 / 社交风格 / 幽默感 /
      言语体系 / 情绪表达 / 主动性 / 亲密风格 / 互动仪礼 / 边界 十二类）；
    - :class:`TraitPresetModel` —— 预设（组件 id 组合集 + 可选卡片覆写
      片段 / 建议语音 / 建议世界书条目 / 四态场景脚本 / 亲密等级上限）。

成熟度分层（``TraitMaturity``）：组件声明 ``safe`` / ``suggestive`` /
``explicit`` 三档，预设声明 0-3 的推荐亲密等级；生成按等级过滤超出
上限的组件（0/1 仅 safe、2 加 suggestive、3 全量），调用方可经
``intimacy_level_override`` 临时改写等级。等级只影响生成期组件选取，
不新增人格列——落在卡片 system 提示文本里。

生成面 :func:`generate_card_from_preset` 是**纯函数**：预设 + 组件注册表
+ 基础名 → chara card v2 卡片 + 软警告列表（组件冲突声明命中时**警告
不抛错**——返回 :class:`GenerationWarningModel` 列表由调用方呈现）。

数据面（仓库数据，非代码）：

    - ``presets/components.yaml`` —— 全量官方组件（120+ 个，十二类目）；
    - ``presets/presets.yaml`` —— 十一个原型预设（八个既有原型 + 同窗
      旧友 / 合租室友两个日常向 SFW 原型 + 作为亲密包的旧识恋人）。

加载面 :func:`load_official_components` / :func:`load_official_presets`
按路径相对资源加载 + 模型校验 + 模块级缓存（模块导入期零副作用——
首次调用即校验，非法数据文件 fail loud；显式传入 ``presets_dir`` 的
调用（测试）绕过缓存）。官方库不变式：预设引用的组件恒存在、官方
预设自身零冲突、每个预设的组件成熟度不越过其亲密等级上限（装载期
校验拒绝违约数据）。
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from enum import StrEnum
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from packages.harness.chat.prompts import (
    PERSONA_DESCRIPTION_SECTION_TEMPLATE,
    PERSONA_NAME_HEADER_TEMPLATE,
    PERSONA_PERSONALITY_SECTION_TEMPLATE,
    WORLD_BOOK_SECTION_SEPARATOR,
)
from packages.harness.persona.chara_card import (
    CharaBookEntryModel,
    CharaBookModel,
    CharaCardModel,
)
from packages.harness.voice.voice_models import VoiceProfileModel

__all__ = [
    "TraitMaturity",
    "TraitComponentCategory",
    "TraitComponentModel",
    "SceneScriptSetModel",
    "PresetCardOverridesModel",
    "TraitPresetModel",
    "GenerationWarningModel",
    "PresetGenerationResultModel",
    "allowed_maturities_for_intimacy_level",
    "render_scene_scripts",
    "generate_card_from_preset",
    "detect_component_conflicts",
    "load_official_components",
    "load_official_presets",
]

#: kebab-case 校验（组件 / 预设 id 的统一形态：小写字母数字 + 连字符）。
_KEBAB_PATTERN = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")

#: 官方预设数据目录（本模块同级的 presets/——路径相对资源加载）。
_PRESETS_DIR = Path(__file__).resolve().parent / "presets"

#: 官方组件缓存（load_official_components 首次调用装配；显式目录绕过）。
_components_cache: dict[str, TraitComponentModel] | None = None

#: 官方预设缓存（load_official_presets 首次调用装配；显式目录绕过）。
_presets_cache: list[TraitPresetModel] | None = None

#: 生成卡片时组件示例台词的最大条数（防止长组件集把 mes_example 淹没）。
_MES_EXAMPLE_MAX_LINES = 24

#: 场景脚本段落地顺序与中文段名（生成时按此序渲染进 system 提示）。
_SCENE_SECTION_LABELS: tuple[tuple[str, str], ...] = (
    ("daily", "日常"),
    ("flirty", "暧昧"),
    ("intimate", "亲密"),
    ("conflict", "冲突"),
)


class TraitMaturity(StrEnum):
    """特质组件成熟度分档（生成期按预设亲密等级过滤的选取依据）。

    - ``safe``：安全（任何亲密等级均可组合——日常相处与人格底色）；
    - ``suggestive``：暧昧（调情、心动、若即若离——亲密等级 2 起可用）；
    - ``explicit``：亲密直白（拥抱、亲吻、同床共枕的直白表达——亲密
      等级 3 才可用；措辞自然克制，不靠露骨词汇制造效果）。
    """

    SAFE = "safe"
    SUGGESTIVE = "suggestive"
    EXPLICIT = "explicit"


class TraitComponentCategory(StrEnum):
    """特质组件分类值域（十二类——覆盖人格表达与亲密互动的完整维度）。

    - ``temperament``：温性（情绪基调与待人温度）；
    - ``speech``：言语风格（说话方式与措辞习惯）；
    - ``quirks``：习惯癖性（标志性小动作与反应模式）；
    - ``values``：价值取向（在意什么 / 如何衡量对错）；
    - ``sociality``：社交风格（与人相处时的姿态）；
    - ``humor``：幽默感（逗趣与自嘲的方式）；
    - ``verbal_system``：言语体系（口癖词汇 / 句长 / 标点与表情符号风格）；
    - ``emotion_expression``：情绪表达（羞怯 / 吃醋 / 委屈如何落成文本）；
    - ``initiative``：主动性（撩拨强度与黏人程度）；
    - ``intimacy_style``：亲密风格（含蓄到直白的光谱位置）；
    - ``interaction_ritual``：互动仪礼（称谓习惯 / 早晚安仪式）；
    - ``boundaries``：边界（雷区与回避话题的处理方式）。
    """

    TEMPERAMENT = "temperament"
    SPEECH = "speech"
    QUIRKS = "quirks"
    VALUES = "values"
    SOCIALITY = "sociality"
    HUMOR = "humor"
    VERBAL_SYSTEM = "verbal_system"
    EMOTION_EXPRESSION = "emotion_expression"
    INITIATIVE = "initiative"
    INTIMACY_STYLE = "intimacy_style"
    INTERACTION_RITUAL = "interaction_ritual"
    BOUNDARIES = "boundaries"


class TraitComponentModel(BaseModel):
    """特质组件（最小可复用人格碎片——预设的组合原子）。

    ``description_zh`` 是**落入卡片的碎片**（组合时拼接进
    ``personality``）；``tone_directives`` 是进入 system 提示的短指令行；
    ``sample_lines`` 是 3-5 条示例台词（组合时各成 ``mes_example`` 的
    ``<START>`` 块）。``sample_speech`` 是单条示例台词的遗留字段：装载
    时若 ``sample_lines`` 为空则折叠为首条示例（两字段并存时以
    ``sample_lines`` 为准，遗留值不再参与生成）。

    Attributes:
        id: 组件 id（kebab-case，库内唯一）。
        name_zh: 组件中文名（画廊芯片展示）。
        description_zh: 特质描述碎片（落入卡片 personality 的短句）。
        category: 分类（十二类值域）。
        maturity: 成熟度分档（默认 safe——需显式声明的才升档）。
        tone_directives: 语气指令行（零或多条——拼入 system 提示碎片）。
        sample_lines: 示例台词（0-8 条；官方库要求 3-5 条，每条 1-120
            字符——拼入 mes_example 的 <START> 块）。
        sample_speech: 遗留单条示例台词（装载时折叠进 sample_lines；
            保留字段以兼容旧数据文件的序列化形态）。
        conflicts: 冲突组件 id 列表（同组组合时软警告——不拒绝）。
    """

    model_config = ConfigDict(protected_namespaces=())

    id: str = Field(min_length=2, max_length=64, description="组件 id（kebab-case）")
    name_zh: str = Field(min_length=1, max_length=64, description="组件中文名")
    description_zh: str = Field(min_length=1, max_length=512, description="特质描述碎片")
    category: TraitComponentCategory = Field(description="分类（十二类值域）")
    maturity: TraitMaturity = Field(
        default=TraitMaturity.SAFE, description="成熟度分档（默认 safe）"
    )
    tone_directives: list[str] = Field(
        default_factory=list, description="语气指令行（拼入 system 提示碎片）"
    )
    sample_lines: list[str] = Field(
        default_factory=list,
        max_length=8,
        description="示例台词（0-8 条；官方库要求 3-5 条，每条 1-120 字符）",
    )
    sample_speech: str | None = Field(
        default=None, max_length=120, description="遗留单条示例台词（装载时折叠进 sample_lines）"
    )
    conflicts: list[str] = Field(
        default_factory=list, description="冲突组件 id 列表（同组组合软警告）"
    )

    @field_validator("id")
    @classmethod
    def _validate_kebab_id(cls, value: str) -> str:
        """组件 id 须为 kebab-case（小写字母数字 + 连字符段）。

        Args:
            value: 待校验的 id 原值。

        Returns:
            校验通过的 id 原值。

        Raises:
            ValueError: id 不符合 kebab-case 形态。
        """
        if not _KEBAB_PATTERN.match(value):
            raise ValueError(f"组件 id 须为 kebab-case（小写字母数字 + 连字符）: {value!r}")
        return value

    @field_validator("conflicts")
    @classmethod
    def _validate_conflicts_kebab(cls, value: list[str]) -> list[str]:
        """冲突引用须同为 kebab-case（引用面形态守卫——存在性由装载期校验）。

        Args:
            value: 冲突组件 id 列表原值。

        Returns:
            校验通过的列表原值。

        Raises:
            ValueError: 任一冲突引用不符合 kebab-case 形态。
        """
        for conflict_id in value:
            if not _KEBAB_PATTERN.match(conflict_id):
                raise ValueError(f"冲突组件 id 须为 kebab-case: {conflict_id!r}")
        return value

    @field_validator("sample_lines")
    @classmethod
    def _validate_sample_lines(cls, value: list[str]) -> list[str]:
        """示例台词逐条须为 1-120 字符（空行 / 超长台词会让示例块失焦）。

        Args:
            value: 示例台词列表原值。

        Returns:
            校验通过的列表原值。

        Raises:
            ValueError: 任一条目为空或超过 120 字符。
        """
        for index, line in enumerate(value):
            if not line.strip():
                raise ValueError(f"示例台词第 {index} 条为空")
            if len(line) > 120:
                raise ValueError(f"示例台词第 {index} 条超过 120 字符: {len(line)}")
        return value

    @model_validator(mode="after")
    def _fold_legacy_sample_speech(self) -> TraitComponentModel:
        """把遗留 ``sample_speech`` 折叠进 ``sample_lines``（装载期归一）。

        ``sample_lines`` 为空时以遗留台词作为唯一示例；两者并存时以
        ``sample_lines`` 为准（遗留字段只为旧数据文件的序列化兼容保留）。

        Returns:
            归一后的实例。
        """
        if self.sample_speech is not None and not self.sample_lines:
            self.sample_lines = [self.sample_speech]
        return self


class SceneScriptSetModel(BaseModel):
    """四态场景脚本（预设级行为指令集——渲染进生成卡片的 system 提示）。

    每个字段是一组短中文行为指令（1-200 字符 / 至多 6 条），描述该
    情境下人格的具体表现方式；缺省或空列表表示该状态不单独成段。

    Attributes:
        daily: 日常相处状态（问候、吃饭、琐事、陪伴）。
        flirty: 暧昧状态（试探、心动、若即若离）。
        intimate: 亲密状态（依偎、亲吻、同处的松弛感）。
        conflict: 冲突状态（争执、冷战、和好）。
    """

    model_config = ConfigDict(protected_namespaces=())

    daily: list[str] = Field(default_factory=list, max_length=6, description="日常状态行为指令")
    flirty: list[str] = Field(default_factory=list, max_length=6, description="暧昧状态行为指令")
    intimate: list[str] = Field(default_factory=list, max_length=6, description="亲密状态行为指令")
    conflict: list[str] = Field(default_factory=list, max_length=6, description="冲突状态行为指令")

    @field_validator("daily", "flirty", "intimate", "conflict")
    @classmethod
    def _validate_script_entries(cls, value: list[str]) -> list[str]:
        """场景指令逐条须为 1-200 字符（空指令与长段落都不适合提示行）。

        Args:
            value: 某一状态的指令列表原值。

        Returns:
            校验通过的列表原值。

        Raises:
            ValueError: 任一条目为空或超过 200 字符。
        """
        for index, entry in enumerate(value):
            if not entry.strip():
                raise ValueError(f"场景指令第 {index} 条为空")
            if len(entry) > 200:
                raise ValueError(f"场景指令第 {index} 条超过 200 字符: {len(entry)}")
        return value


class PresetCardOverridesModel(BaseModel):
    """预设级卡片覆写片段（全部可选——生成时的部分合并面）。

    Attributes:
        name_suffix: 名字后缀建议（拼在基础名之后——如「-酱」；
            None = 不加后缀）。
        personality: 预设级性格补充文本（拼在组件碎片之前）。
        mes_example: 预设级对话示例补充（拼在组件示例台词之前）。
        system_prompt_fragment: 预设级 system 提示补充行（拼在组件
            语气指令之后）。
    """

    model_config = ConfigDict(protected_namespaces=())

    name_suffix: str | None = Field(
        default=None, min_length=1, max_length=32, description="名字后缀建议"
    )
    personality: str | None = Field(
        default=None, max_length=1024, description="预设级性格补充文本"
    )
    mes_example: str | None = Field(
        default=None, max_length=2048, description="预设级对话示例补充"
    )
    system_prompt_fragment: str | None = Field(
        default=None, max_length=1024, description="预设级 system 提示补充行"
    )


class TraitPresetModel(BaseModel):
    """特质预设（组件 id 组合集 + 可选生成覆写——官方库的发行单元）。

    Attributes:
        id: 预设 id（kebab-case，库内唯一）。
        name_zh: 预设中文名（画廊标题；缺省基础名来源）。
        tagline_zh: 一句话标语（画廊副标题）。
        description_zh: 预设描述（组合时作为 personality 的首段碎片）。
        components: 组件 id 组合集（至少一个——空组合的预设无意义）。
        intimacy_level: 推荐亲密等级上限（0-3；生成期按此过滤组件，
            缺省 0 = 仅安全组件）。
        scene_scripts: 四态场景脚本（可选；渲染进 system 提示的命名段）。
        base_card_overrides: 卡片覆写片段（全部可选）。
        suggested_voice: 建议语音绑定（None = 无建议；导入时随人格落库）。
        suggested_book: 建议世界书条目（生成时并入卡片世界书）。
    """

    model_config = ConfigDict(protected_namespaces=())

    id: str = Field(min_length=2, max_length=64, description="预设 id（kebab-case）")
    name_zh: str = Field(min_length=1, max_length=64, description="预设中文名")
    tagline_zh: str = Field(min_length=1, max_length=128, description="一句话标语")
    description_zh: str = Field(min_length=1, max_length=1024, description="预设描述")
    components: list[str] = Field(
        min_length=1, max_length=16, description="组件 id 组合集（1-16 个）"
    )
    intimacy_level: int = Field(
        default=0, ge=0, le=3, description="推荐亲密等级上限（0-3；生成期组件过滤）"
    )
    scene_scripts: SceneScriptSetModel = Field(
        default_factory=SceneScriptSetModel, description="四态场景脚本（可选）"
    )
    base_card_overrides: PresetCardOverridesModel = Field(
        default_factory=PresetCardOverridesModel, description="卡片覆写片段"
    )
    suggested_voice: VoiceProfileModel | None = Field(
        default=None, description="建议语音绑定（None = 无建议）"
    )
    suggested_book: list[CharaBookEntryModel] = Field(
        default_factory=list, description="建议世界书条目"
    )

    @field_validator("id")
    @classmethod
    def _validate_kebab_id(cls, value: str) -> str:
        """预设 id 须为 kebab-case（与组件 id 同形态）。

        Args:
            value: 待校验的 id 原值。

        Returns:
            校验通过的 id 原值。

        Raises:
            ValueError: id 不符合 kebab-case 形态。
        """
        if not _KEBAB_PATTERN.match(value):
            raise ValueError(f"预设 id 须为 kebab-case（小写字母数字 + 连字符）: {value!r}")
        return value


class GenerationWarningModel(BaseModel):
    """生成警告（软性——组合命中冲突声明等值得提示但不拒绝的形态）。

    Attributes:
        code: 警告码（当前唯一值 ``component_conflict``——组件冲突）。
        message: 面向用户的中文提示。
    """

    model_config = ConfigDict(protected_namespaces=())

    code: str = Field(description="警告码（component_conflict 等）")
    message: str = Field(description="面向用户的中文提示")


class PresetGenerationResultModel(BaseModel):
    """预设生成结果（卡片 + 警告并列返回——警告不抛错）。

    Attributes:
        card: 生成的 chara card v2 卡片。
        warnings: 生成警告列表（空 = 无）。
    """

    model_config = ConfigDict(protected_namespaces=())

    card: CharaCardModel = Field(description="生成的卡片")
    warnings: list[GenerationWarningModel] = Field(
        default_factory=list, description="生成警告列表"
    )


def allowed_maturities_for_intimacy_level(level: int) -> frozenset[TraitMaturity]:
    """亲密等级 → 可组合的组件成熟度集合（生成期过滤的唯一映射源）。

    映射（锁定语义）：0 / 1 → 仅 safe；2 → safe + suggestive；
    3 → 全量三档。

    Args:
        level: 亲密等级（0-3）。

    Returns:
        该等级允许组合的成熟度集合。

    Raises:
        ValueError: 等级不是 0-3 的整数。
    """
    if level in (0, 1):
        return frozenset({TraitMaturity.SAFE})
    if level == 2:
        return frozenset({TraitMaturity.SAFE, TraitMaturity.SUGGESTIVE})
    if level == 3:
        return frozenset(TraitMaturity)
    raise ValueError(f"亲密等级须为 0-3 的整数: {level!r}")


def render_scene_scripts(scripts: SceneScriptSetModel) -> str:
    """把四态场景脚本渲染成命名的提示段（日常 / 暧昧 / 亲密 / 冲突）。

    空状态不生成段名；段内指令按声明序逐行加 ``- `` 前缀；段间换行
    分隔。段序固定为日常 → 暧昧 → 亲密 → 冲突（与提示的阅读顺序一致）。

    Args:
        scripts: 四态场景脚本。

    Returns:
        渲染后的提示文本（四态全空时为空串）。
    """
    blocks: list[str] = []
    for field_name, label in _SCENE_SECTION_LABELS:
        entries: list[str] = getattr(scripts, field_name)
        if not entries:
            continue
        blocks.append(f"【{label}】")
        blocks.extend(f"- {entry}" for entry in entries)
    return "\n".join(blocks)


def detect_component_conflicts(
    component_ids: Sequence[str], components_registry: Mapping[str, TraitComponentModel]
) -> list[tuple[str, str]]:
    """检测组合集内的冲突对（声明命中——双向检查、去重、组合序稳定）。

    Args:
        component_ids: 组合集（按预设声明序）。
        components_registry: 组件注册表（id → 组件）。

    Returns:
        冲突对列表 ``(id_a, id_b)``（组合序在前者在先；每对至多一条）。

    Raises:
        ValueError: 组合集引用了注册表中不存在的组件 id（数据一致性
            错误——fail loud，静默跳过会让卡片缺特质）。
    """
    conflicts: list[tuple[str, str]] = []
    seen: set[frozenset[str]] = set()
    for index, comp_id in enumerate(component_ids):
        component = components_registry.get(comp_id)
        if component is None:
            raise ValueError(f"组件注册表缺少组件: {comp_id!r}")
        for other_id in component_ids[index + 1 :]:
            pair = frozenset((comp_id, other_id))
            if pair in seen:
                continue
            other = components_registry.get(other_id)
            if other is None:
                raise ValueError(f"组件注册表缺少组件: {other_id!r}")
            if other_id in component.conflicts or comp_id in other.conflicts:
                seen.add(pair)
                conflicts.append((comp_id, other_id))
    return conflicts


def generate_card_from_preset(
    preset: TraitPresetModel,
    components_registry: Mapping[str, TraitComponentModel],
    *,
    base_name: str,
    intimacy_level_override: int | None = None,
) -> PresetGenerationResultModel:
    """预设 → chara card v2 卡片（**纯函数**——确定性组合）。

    组合规则（锁定语义，逐字段确定）：

        - 生效亲密等级 = ``intimacy_level_override``（提供时）否则
          ``preset.intimacy_level``；成熟度超出该等级的组件在组合前被
          过滤（0/1 仅 safe、2 加 suggestive、3 全量——见
          :func:`allowed_maturities_for_intimacy_level`）；
        - ``name`` = ``base_name`` + ``base_card_overrides.name_suffix``
          （后缀存在时拼接；None = 原名）；
        - ``personality`` = 预设描述 → 预设级补充 → 保留下来的组件描述
          碎片，空段以 ``；`` 拼接（中文分号；该字段继续供 UI / 编辑面
          使用，运行期提示不依赖它）；
        - ``system_prompt`` = **自包含的人格覆写文本**：自称头（名字 +
          角色身份）→【角色设定】（预设描述）→【性格特点】（预设级性格
          补充 + 保留组件的描述碎片）→ 保留组件的语气指令行（声明序）→
          预设级补充行 → 场景脚本命名段（日常 / 暧昧 / 亲密 / 冲突）。
          段落名复用默认人格模板（``PERSONA_*`` 模板），意即
          ``render_persona_system_prompt`` 的覆写路径直接采用本字段时，
          人格主体仍完整进入提示；无任何尾段（指令 / 补充 / 场景）时只
          保留自称头与两个叙事段；
        - ``mes_example`` = 预设级示例 → 保留组件的示例台词（各成
          ``<START>\\n{name}: 台词`` 块，合计至多 24 条台词块——超出
          截断），换行拼接；
        - ``character_book`` = 建议条目非空时建书（名 = ``{预设id}-book``）
          否则 None；
        - 保留组件命中冲突声明 → :class:`GenerationWarningModel` 列表
          （不抛错）。

    Args:
        preset: 特质预设。
        components_registry: 组件注册表（id → 组件）。
        base_name: 基础名（用户期望名 / 预设名——后缀拼接前的原名）。
        intimacy_level_override: 覆盖预设推荐亲密等级（0-3；None = 用
            预设值）。

    Returns:
        生成结果（卡片 + 警告列表）。

    Raises:
        ValueError: 组合集引用了注册表中不存在的组件 id（数据一致性
            错误——fail loud）；``base_name`` 为空；覆盖等级越界。
    """
    if not base_name.strip():
        raise ValueError("base_name 不能为空（人格必须可称呼）")
    effective_level = (
        preset.intimacy_level if intimacy_level_override is None else intimacy_level_override
    )
    allowed_maturities = allowed_maturities_for_intimacy_level(effective_level)
    overrides = preset.base_card_overrides
    declared: list[TraitComponentModel] = []
    for comp_id in preset.components:
        component = components_registry.get(comp_id)
        if component is None:
            raise ValueError(f"组件注册表缺少组件: {comp_id!r}")
        declared.append(component)
    resolved = [
        component for component in declared if component.maturity in allowed_maturities
    ]
    kept_ids = [component.id for component in resolved]
    conflicts = detect_component_conflicts(kept_ids, components_registry)
    warnings = [
        GenerationWarningModel(
            code="component_conflict",
            message=(
                f"组件「{components_registry[a].name_zh}」与"
                f"「{components_registry[b].name_zh}」存在冲突声明"
                f"（{a} ↔ {b}），已同时组合"
            ),
        )
        for a, b in conflicts
    ]
    name = f"{base_name}{overrides.name_suffix}" if overrides.name_suffix else base_name
    personality = "；".join(
        part
        for part in [preset.description_zh, overrides.personality]
        + [component.description_zh for component in resolved]
        if part
    )
    scene_block = render_scene_scripts(preset.scene_scripts)
    personality_body = "；".join(
        part
        for part in [overrides.personality]
        + [component.description_zh for component in resolved]
        if part
    )
    system_sections: list[str] = [
        PERSONA_NAME_HEADER_TEMPLATE.format(name=name),
        PERSONA_DESCRIPTION_SECTION_TEMPLATE.format(description=preset.description_zh),
    ]
    if personality_body:
        system_sections.append(
            PERSONA_PERSONALITY_SECTION_TEMPLATE.format(personality=personality_body)
        )
    tail_lines = [
        line
        for line in [
            directive for component in resolved for directive in component.tone_directives
        ]
        + [overrides.system_prompt_fragment, scene_block]
        if line
    ]
    if tail_lines:
        system_sections.append("\n".join(tail_lines))
    system_prompt = WORLD_BOOK_SECTION_SEPARATOR.join(system_sections)
    example_blocks: list[str] = [overrides.mes_example] if overrides.mes_example else []
    line_count = 0
    for component in resolved:
        for sample in component.sample_lines:
            if line_count >= _MES_EXAMPLE_MAX_LINES:
                break
            example_blocks.append(f"<START>\n{name}: {sample}")
            line_count += 1
    mes_example = "\n".join(example_blocks)
    character_book = (
        CharaBookModel(
            name=f"{preset.id}-book",
            description=f"{preset.name_zh}预设补充设定",
            entries=preset.suggested_book,
        )
        if preset.suggested_book
        else None
    )
    card = CharaCardModel(
        name=name,
        personality=personality,
        system_prompt=system_prompt,
        mes_example=mes_example,
        character_book=character_book,
    )
    return PresetGenerationResultModel(card=card, warnings=warnings)


def _load_yaml_mapping(path: Path, *, expected_key: str) -> dict[str, object]:
    """读取 YAML 数据文件并校验顶层结构（dict + 期望键存在）。

    Args:
        path: 数据文件路径。
        expected_key: 期望的顶层键名（如 ``components`` / ``presets``）。

    Returns:
        顶层映射（已确认期望键存在）。

    Raises:
        ValueError: 文件缺失 / YAML 解析失败 / 顶层非映射 / 期望键缺失。
    """
    if not path.is_file():
        raise ValueError(f"官方预设库数据文件缺失: {path}")
    try:
        raw: object = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ValueError(f"官方预设库数据文件 YAML 解析失败: {path}（{exc}）") from exc
    if not isinstance(raw, dict):
        raise ValueError(f"官方预设库数据文件顶层须为映射: {path}")
    if expected_key not in raw:
        raise ValueError(f"官方预设库数据文件缺少顶层键 {expected_key!r}: {path}")
    return raw


def load_official_components(
    presets_dir: Path | None = None,
) -> dict[str, TraitComponentModel]:
    """装载官方特质组件库（路径相对资源加载 + 校验 + 缓存）。

    Args:
        presets_dir: 数据目录（None = 随包发行的官方目录——命中模块级
            缓存；显式目录（测试）绕过缓存直读）。

    Returns:
        组件注册表（id → 组件；文件声明序）。

    Raises:
        ValueError: 数据文件缺失 / YAML 解析失败 / 组件校验失败 /
            组件 id 重复 / 冲突引用不存在的组件 / 示例台词条数越界。
    """
    global _components_cache
    if presets_dir is None and _components_cache is not None:
        return _components_cache
    directory = presets_dir if presets_dir is not None else _PRESETS_DIR
    raw = _load_yaml_mapping(directory / "components.yaml", expected_key="components")
    entries = raw["components"]
    if not isinstance(entries, list):
        raise ValueError(f"components 须为列表: {directory / 'components.yaml'}")
    registry: dict[str, TraitComponentModel] = {}
    for index, entry in enumerate(entries):
        try:
            component = TraitComponentModel.model_validate(entry)
        except Exception as exc:
            raise ValueError(
                f"官方组件数据非法（第 {index} 项）: {exc}"
            ) from exc
        if component.id in registry:
            raise ValueError(f"官方组件 id 重复: {component.id!r}")
        registry[component.id] = component
    if not registry:
        raise ValueError("官方组件库为空（至少须有一个组件）")
    for component in registry.values():
        unknown = [c for c in component.conflicts if c not in registry]
        if unknown:
            raise ValueError(
                f"组件 {component.id!r} 的冲突声明引用了未知组件: {unknown}"
            )
        if not 3 <= len(component.sample_lines) <= 5:
            raise ValueError(
                f"组件 {component.id!r} 的示例台词须为 3-5 条:"
                f" 当前 {len(component.sample_lines)} 条"
            )
    if presets_dir is None:
        _components_cache = registry
    return registry


def load_official_presets(presets_dir: Path | None = None) -> list[TraitPresetModel]:
    """装载官方特质预设库（路径相对资源加载 + 交叉校验 + 缓存）。

    校验面（官方库不变式——违约数据 fail loud）：

        - 每个预设通过 :class:`TraitPresetModel` 模型校验；
        - 预设 id 唯一；
        - 组合集引用的组件恒存在（组件库先行装载）；
        - 组合集内无重复引用；
        - 官方预设自身零冲突（命中冲突声明 = 发行数据作者错误）；
        - 组合集内每个组件的成熟度不越过预设自身的 ``intimacy_level``
          上限（越界 = 作者声明了生成期会被静默过滤的组件）。

    Args:
        presets_dir: 数据目录（None = 随包发行的官方目录——命中模块级
            缓存；显式目录（测试）绕过缓存直读）。

    Returns:
        预设列表（文件声明序）。

    Raises:
        ValueError: 数据文件缺失 / YAML 解析失败 / 预设校验失败 /
            预设 id 重复 / 引用未知组件 / 组合内重复 / 官方预设命中
            冲突声明 / 组件成熟度越过预设亲密等级上限。
    """
    global _presets_cache
    if presets_dir is None and _presets_cache is not None:
        return _presets_cache
    directory = presets_dir if presets_dir is not None else _PRESETS_DIR
    registry = load_official_components(presets_dir)
    raw = _load_yaml_mapping(directory / "presets.yaml", expected_key="presets")
    entries = raw["presets"]
    if not isinstance(entries, list):
        raise ValueError(f"presets 须为列表: {directory / 'presets.yaml'}")
    presets: list[TraitPresetModel] = []
    seen_ids: set[str] = set()
    for index, entry in enumerate(entries):
        try:
            preset = TraitPresetModel.model_validate(entry)
        except Exception as exc:
            raise ValueError(f"官方预设数据非法（第 {index} 项）: {exc}") from exc
        if preset.id in seen_ids:
            raise ValueError(f"官方预设 id 重复: {preset.id!r}")
        seen_ids.add(preset.id)
        unknown = [c for c in preset.components if c not in registry]
        if unknown:
            raise ValueError(f"预设 {preset.id!r} 引用了未知组件: {unknown}")
        if len(set(preset.components)) != len(preset.components):
            raise ValueError(f"预设 {preset.id!r} 的组合集存在重复组件引用")
        conflicts = detect_component_conflicts(preset.components, registry)
        if conflicts:
            raise ValueError(
                f"官方预设 {preset.id!r} 命中冲突声明（发行数据作者错误）: {conflicts}"
            )
        allowed = allowed_maturities_for_intimacy_level(preset.intimacy_level)
        over_ceiling = [
            component_id
            for component_id in preset.components
            if registry[component_id].maturity not in allowed
        ]
        if over_ceiling:
            raise ValueError(
                f"预设 {preset.id!r} 的组件成熟度越过亲密等级"
                f" {preset.intimacy_level} 上限: {over_ceiling}"
            )
        presets.append(preset)
    if not presets:
        raise ValueError("官方预设库为空（至少须有一个预设）")
    if presets_dir is None:
        _presets_cache = presets
    return presets

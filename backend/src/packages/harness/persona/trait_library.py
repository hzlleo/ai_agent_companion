"""特质组件库——可组合人格预设的 schema 与确定性卡片生成（Unit 文件）。

本模块是 P1 特质组件库的核心：官方预设库以**纯特质组合集**形态发行
（零角色名 / 零背景故事 / 零既有形象引用——版权洁净是设计目标，
用户经文件导入端点私有导入自己的完整卡片）。两层结构：

    - :class:`TraitComponentModel` —— 特质组件（最小可复用人格碎片：
      温性 / 言语风格 / 习惯癖性 / 价值取向 / 社交风格 / 幽默感 六类）；
    - :class:`TraitPresetModel` —— 预设（组件 id 组合集 + 可选卡片覆写
      片段 / 建议语音 / 建议世界书条目）。

生成面 :func:`generate_card_from_preset` 是**纯函数**：预设 + 组件注册表
+ 基础名 → chara card v2 卡片 + 软警告列表（组件冲突声明命中时**警告
不抛错**——返回 :class:`GenerationWarningModel` 列表由调用方呈现）。

数据面（仓库数据，非代码）：

    - ``presets/components.yaml`` —— 全量官方组件（~30 个）；
    - ``presets/presets.yaml`` —— 八个原型预设（大家闺秀 / 小家碧玉 /
      绿茶 / 傲娇 / 温柔治愈 / 高冷御姐 / 元气少女 / 毒舌损友）。

加载面 :func:`load_official_components` / :func:`load_official_presets`
按路径相对资源加载 + 模型校验 + 模块级缓存（模块导入期零副作用——
首次调用即校验，非法数据文件 fail loud；显式传入 ``presets_dir`` 的
调用（测试）绕过缓存）。官方库不变式：预设引用的组件恒存在、官方
预设自身零冲突（装载期校验拒绝违约数据）。
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from enum import StrEnum
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from packages.harness.persona.chara_card import (
    CharaBookEntryModel,
    CharaBookModel,
    CharaCardModel,
)
from packages.harness.voice.voice_models import VoiceProfileModel

__all__ = [
    "TraitComponentCategory",
    "TraitComponentModel",
    "PresetCardOverridesModel",
    "TraitPresetModel",
    "GenerationWarningModel",
    "PresetGenerationResultModel",
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


class TraitComponentCategory(StrEnum):
    """特质组件分类值域（六类——覆盖人格表达的主要维度）。

    - ``temperament``：温性（情绪基调与待人温度）；
    - ``speech``：言语风格（说话方式与措辞习惯）；
    - ``quirks``：习惯癖性（标志性小动作与反应模式）；
    - ``values``：价值取向（在意什么 / 如何衡量对错）；
    - ``sociality``：社交风格（与人相处时的姿态）；
    - ``humor``：幽默感（逗趣与自嘲的方式）。
    """

    TEMPERAMENT = "temperament"
    SPEECH = "speech"
    QUIRKS = "quirks"
    VALUES = "values"
    SOCIALITY = "sociality"
    HUMOR = "humor"


class TraitComponentModel(BaseModel):
    """特质组件（最小可复用人格碎片——预设的组合原子）。

    ``description_zh`` 是**落入卡片的碎片**（组合时拼接进
    ``personality``）；``tone_directives`` 是进入 system 提示的短指令行；
    ``sample_speech`` 是一条示例台词（组合时拼进 ``mes_example``）。

    Attributes:
        id: 组件 id（kebab-case，库内唯一）。
        name_zh: 组件中文名（画廊芯片展示）。
        description_zh: 特质描述碎片（落入卡片 personality 的短句）。
        category: 分类（六类值域）。
        tone_directives: 语气指令行（零或多条——拼入 system 提示碎片）。
        sample_speech: 示例台词（None = 无；一条最能体现该特质的台词）。
        conflicts: 冲突组件 id 列表（同组组合时软警告——不拒绝）。
    """

    model_config = ConfigDict(protected_namespaces=())

    id: str = Field(min_length=2, max_length=64, description="组件 id（kebab-case）")
    name_zh: str = Field(min_length=1, max_length=64, description="组件中文名")
    description_zh: str = Field(min_length=1, max_length=512, description="特质描述碎片")
    category: TraitComponentCategory = Field(description="分类（六类值域）")
    tone_directives: list[str] = Field(
        default_factory=list, description="语气指令行（拼入 system 提示碎片）"
    )
    sample_speech: str | None = Field(
        default=None, max_length=512, description="示例台词（None = 无）"
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
) -> PresetGenerationResultModel:
    """预设 → chara card v2 卡片（**纯函数**——确定性组合）。

    组合规则（锁定语义，逐字段确定）：

        - ``name`` = ``base_name`` + ``base_card_overrides.name_suffix``
          （后缀存在时拼接；None = 原名）；
        - ``personality`` = 预设描述 → 预设级补充 → 组件描述碎片，
          非 ``；`` 空段拼接（中文分号）；
        - ``system_prompt`` = 组件语气指令行（声明序）→ 预设级补充行，
          换行拼接（无任何指令时为空串——回落默认人格模板）；
        - ``mes_example`` = 预设级示例 → 组件示例台词（各成
          ``<START>\\n{name}: 台词`` 块），换行拼接；
        - ``character_book`` = 建议条目非空时建书（名 = ``{预设id}-book``）
          否则 None；
        - 冲突声明命中 → :class:`GenerationWarningModel` 列表（不抛错）。

    Args:
        preset: 特质预设。
        components_registry: 组件注册表（id → 组件）。
        base_name: 基础名（用户期望名 / 预设名——后缀拼接前的原名）。

    Returns:
        生成结果（卡片 + 警告列表）。

    Raises:
        ValueError: 组合集引用了注册表中不存在的组件 id（数据一致性
            错误——fail loud）；``base_name`` 为空。
    """
    if not base_name.strip():
        raise ValueError("base_name 不能为空（人格必须可称呼）")
    overrides = preset.base_card_overrides
    resolved: list[TraitComponentModel] = []
    for comp_id in preset.components:
        component = components_registry.get(comp_id)
        if component is None:
            raise ValueError(f"组件注册表缺少组件: {comp_id!r}")
        resolved.append(component)
    conflicts = detect_component_conflicts(preset.components, components_registry)
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
    system_prompt = "\n".join(
        line
        for line in [
            directive for component in resolved for directive in component.tone_directives
        ]
        + [overrides.system_prompt_fragment]
        if line
    )
    example_blocks = [
        overrides.mes_example,
        *[
            f"<START>\n{name}: {component.sample_speech}"
            for component in resolved
            if component.sample_speech
        ],
    ]
    mes_example = "\n".join(block for block in example_blocks if block)
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
            组件 id 重复 / 冲突引用不存在的组件。
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
        - 官方预设自身零冲突（命中冲突声明 = 发行数据作者错误）。

    Args:
        presets_dir: 数据目录（None = 随包发行的官方目录——命中模块级
            缓存；显式目录（测试）绕过缓存直读）。

    Returns:
        预设列表（文件声明序）。

    Raises:
        ValueError: 数据文件缺失 / YAML 解析失败 / 预设校验失败 /
            预设 id 重复 / 引用未知组件 / 组合内重复 / 官方预设命中
            冲突声明。
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
        presets.append(preset)
    if not presets:
        raise ValueError("官方预设库为空（至少须有一个预设）")
    if presets_dir is None:
        _presets_cache = presets
    return presets

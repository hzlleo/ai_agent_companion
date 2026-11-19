"""harness/persona trait_library 测试——特质组件 / 预设模型规格 + 确定性
卡片生成 + 官方库装载（真实随包 YAML 数据——仓库数据即 hermetic）。

覆盖面：

    - 模型规格：kebab-case id / 十二类分类值域 / 成熟度分档 / 示例台词
      与场景脚本的逐条界校验 / 载荷恰一约束（preset 与 card 互斥——见
      dtos 测试）/ 组件冲突引用形态；
    - 纯函数生成：名字后缀拼接 / personality 中文分号拼接序 /
      system_prompt 语气指令行 + 场景脚本命名段拼接 / mes_example
      <START> 台词块（条数上限）/ 建议世界书并入 / 冲突软警告（双向
      声明 + 去重）/ 未知组件 fail loud / 空基础名拒绝；
    - 成熟度过滤：亲密等级 0-3 × 组件成熟度的完整矩阵 / 预设值缺省 /
      覆盖升级与降级 / 被过滤组件不参与冲突警告；
    - 遗留兼容：单条 sample_speech 装载时折叠为 sample_lines；
    - 官方库装载：120+ 组件 × 十二类目覆盖 + 10 预设 + 组件全引用
      合法 + 官方预设零冲突 + 预设组件不越过自身亲密等级（发行
      不变式）；显式目录（tmp_path 合成数据）的违约形态 fail loud
      （文件缺失 / 结构违约 / id 重复 / 未知引用 / 发行冲突预设 /
      示例台词越界 / 成熟度越过预设上限）；
    - 缓存：缺省目录命中模块级缓存（同实例返回）、显式目录绕过。
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import pytest
from pydantic import ValidationError

from packages.harness.chat.chat_models import ChatSessionStateModel
from packages.harness.chat.service import compose_messages
from packages.harness.persona.chara_card import CharaBookEntryModel
from packages.harness.persona.trait_library import (
    SceneScriptSetModel,
    TraitComponentCategory,
    TraitComponentModel,
    TraitMaturity,
    TraitPresetModel,
    allowed_maturities_for_intimacy_level,
    detect_component_conflicts,
    generate_card_from_preset,
    load_official_components,
    load_official_presets,
    render_scene_scripts,
)


def _component(component_id: str, **overrides: object) -> TraitComponentModel:
    """构造最小合法组件（覆盖式定制字段）。"""
    payload: dict[str, object] = {
        "id": component_id,
        "name_zh": "测试组件",
        "description_zh": "测试特质碎片",
        "category": TraitComponentCategory.TEMPERAMENT,
        "sample_lines": ["测试台词一", "测试台词二", "测试台词三"],
    }
    payload.update(overrides)
    return TraitComponentModel.model_validate(payload)


def _preset(preset_id: str, components: list[str], **overrides: object) -> TraitPresetModel:
    """构造最小合法预设（覆盖式定制字段）。"""
    payload: dict[str, object] = {
        "id": preset_id,
        "name_zh": "测试预设",
        "tagline_zh": "测试标语",
        "description_zh": "测试预设描述",
        "components": components,
    }
    payload.update(overrides)
    return TraitPresetModel.model_validate(payload)


def _registry(*components: TraitComponentModel) -> dict[str, TraitComponentModel]:
    """组件列表 → 注册表。"""
    return {component.id: component for component in components}


# ---------------------------------------------------------------------------
# 模型规格
# ---------------------------------------------------------------------------


async def test_component_rejects_non_kebab_id() -> None:
    """组件 id 非 kebab-case（大写 / 下划线 / 空格 / 前后缀连字符）构造期拒绝。"""
    for bad_id in ("Bad-ID", "bad_id", "bad id", "-bad", "bad-", "bad--id"):
        with pytest.raises(ValidationError):
            _component(bad_id)


async def test_component_rejects_bad_conflict_ref_shape() -> None:
    """冲突引用形态违约（非 kebab-case）构造期拒绝。"""
    with pytest.raises(ValidationError):
        _component("ok-id", conflicts=["Bad_Ref"])


async def test_component_category_enum_domain() -> None:
    """分类值域：十二类合法值全通过、未知值拒绝。"""
    for category in TraitComponentCategory:
        component = _component(f"comp-{category.value.replace('_', '-')}", category=category)
        assert component.category is category
    with pytest.raises(ValidationError):
        _component("comp-unknown", category="nonexistent")


async def test_preset_requires_nonempty_components() -> None:
    """预设组合集至少一个组件（空组合的预设无意义——构造期拒绝）。"""
    with pytest.raises(ValidationError):
        _preset("empty-preset", [])


async def test_preset_rejects_non_kebab_id() -> None:
    """预设 id 非 kebab-case 构造期拒绝。"""
    with pytest.raises(ValidationError):
        _preset("BadPreset", ["warm-hearted"])


# ---------------------------------------------------------------------------
# 确定性生成（纯函数）
# ---------------------------------------------------------------------------


async def test_generate_card_basic_composition() -> None:
    """基础组合：名字原样、personality 分号拼接、指令行拼接、示例块拼接。"""
    registry = _registry(
        _component(
            "warm-core",
            name_zh="温情",
            description_zh="碎片一",
            tone_directives=["指令一", "指令二"],
            sample_lines=["台词一"],
        ),
        _component(
            "calm-core",
            name_zh="沉静",
            description_zh="碎片二",
            tone_directives=["指令三"],
            sample_lines=["台词二"],
        ),
    )
    preset = _preset("demo", ["warm-core", "calm-core"])
    result = generate_card_from_preset(preset, registry, base_name="小测")
    assert result.warnings == []
    assert result.card.name == "小测"
    assert result.card.personality == "测试预设描述；碎片一；碎片二"
    prompt = result.card.system_prompt
    assert prompt.startswith("你现在扮演「小测」")
    assert "【角色设定】\n测试预设描述" in prompt
    assert "【性格特点】\n碎片一；碎片二" in prompt
    assert prompt.index("【角色设定】") < prompt.index("【性格特点】") < prompt.index("指令一")
    assert "指令一\n指令二\n指令三" in prompt
    assert result.card.mes_example == "<START>\n小测: 台词一\n<START>\n小测: 台词二"
    assert result.card.character_book is None


async def test_generate_card_deterministic() -> None:
    """同输入恒同输出（纯函数——重复生成逐字段一致）。"""
    registry = _registry(
        _component("a-core", description_zh="碎片", sample_speech="台词"),
        _component("b-core", description_zh="碎片B"),
    )
    preset = _preset("demo", ["a-core", "b-core"])
    first = generate_card_from_preset(preset, registry, base_name="名字")
    second = generate_card_from_preset(preset, registry, base_name="名字")
    assert first.card.model_dump() == second.card.model_dump()
    assert first.warnings == second.warnings


async def test_generate_card_name_suffix_appended() -> None:
    """名字后缀建议：提供时拼在基础名之后；不提供时原名直传。"""
    registry = _registry(_component("a-core"))
    with_suffix = _preset(
        "demo",
        ["a-core"],
        base_card_overrides={"name_suffix": "-酱"},
    )
    result = generate_card_from_preset(with_suffix, registry, base_name="小桃")
    assert result.card.name == "小桃-酱"
    without_suffix = _preset("demo", ["a-core"])
    assert generate_card_from_preset(without_suffix, registry, base_name="小桃").card.name == "小桃"


async def test_generate_card_overrides_merge() -> None:
    """覆写片段合并：personality 前插 / mes_example 前插 / 提示行尾插。"""
    registry = _registry(
        _component(
            "a-core",
            description_zh="碎片",
            tone_directives=["指令"],
            sample_lines=["台词"],
        )
    )
    preset = _preset(
        "demo",
        ["a-core"],
        base_card_overrides={
            "personality": "预设补充性格",
            "mes_example": "预设示例",
            "system_prompt_fragment": "预设提示行",
        },
    )
    result = generate_card_from_preset(preset, registry, base_name="小测")
    assert result.card.personality == "测试预设描述；预设补充性格；碎片"
    prompt = result.card.system_prompt
    assert "【角色设定】\n测试预设描述" in prompt
    assert "【性格特点】\n预设补充性格；碎片" in prompt
    assert prompt.index("【性格特点】") < prompt.index("指令") < prompt.index("预设提示行")
    assert result.card.mes_example == "预设示例\n<START>\n小测: 台词"


async def test_generate_card_book_entries_merged() -> None:
    """建议世界书：条目非空时建书并入（书名 = 预设id-book）；空则无书。"""
    registry = _registry(_component("a-core"))
    entry = CharaBookEntryModel(keys=["夜宵"], content="她总在深夜煮面。")
    preset = _preset("demo", ["a-core"], suggested_book=[entry])
    result = generate_card_from_preset(preset, registry, base_name="小测")
    assert result.card.character_book is not None
    assert result.card.character_book.name == "demo-book"
    assert result.card.character_book.entries[0].content == "她总在深夜煮面。"
    empty = _preset("demo", ["a-core"])
    assert generate_card_from_preset(empty, registry, base_name="小测").card.character_book is None


async def test_generate_card_conflict_warnings_soft() -> None:
    """冲突软警告：双向声明命中 → 警告不抛错；单向声明同样命中。"""
    registry = _registry(
        _component("sweet", conflicts=["sharp"]),
        _component("sharp"),
        _component("mild"),
    )
    preset = _preset("demo", ["sweet", "sharp", "mild"])
    result = generate_card_from_preset(preset, registry, base_name="小测")
    assert len(result.warnings) == 1
    assert result.warnings[0].code == "component_conflict"
    assert "sweet" in result.warnings[0].message and "sharp" in result.warnings[0].message
    assert result.card.name == "小测"


async def test_detect_conflicts_dedup_and_direction() -> None:
    """冲突检测：反向声明命中、同对去重、无冲突空列表。"""
    registry = _registry(
        _component("comp-a"),
        _component("comp-b", conflicts=["comp-a"]),
        _component("comp-c"),
    )
    assert detect_component_conflicts(["comp-a", "comp-b"], registry) == [("comp-a", "comp-b")]
    assert detect_component_conflicts(["comp-b", "comp-a"], registry) == [("comp-b", "comp-a")]
    assert detect_component_conflicts(["comp-a", "comp-c"], registry) == []
    with pytest.raises(ValueError):
        detect_component_conflicts(["comp-a", "missing"], registry)


async def test_generate_card_unknown_component_fails_loud() -> None:
    """组合集引用注册表缺失组件：ValueError fail loud（数据一致性）。"""
    registry = _registry(_component("a-core"))
    preset = _preset("demo", ["a-core", "missing-core"])
    with pytest.raises(ValueError, match="missing-core"):
        generate_card_from_preset(preset, registry, base_name="小测")


async def test_generate_card_rejects_blank_base_name() -> None:
    """空基础名拒绝（人格必须可称呼）。"""
    registry = _registry(_component("a-core"))
    preset = _preset("demo", ["a-core"])
    with pytest.raises(ValueError, match="base_name"):
        generate_card_from_preset(preset, registry, base_name="   ")


# ---------------------------------------------------------------------------
# 官方库装载（真实随包 YAML——仓库数据即 hermetic）
# ---------------------------------------------------------------------------


async def test_official_library_shape() -> None:
    """官方库形态：11 预设（固定顺序与名单）+ 组件全引用合法 + 零冲突。"""
    presets = load_official_presets()
    registry = load_official_components()
    assert [preset.name_zh for preset in presets] == [
        "大家闺秀",
        "小家碧玉",
        "绿茶",
        "傲娇",
        "温柔治愈",
        "高冷御姐",
        "元气少女",
        "毒舌损友",
        "同窗旧友",
        "合租室友",
        "旧识恋人",
    ]
    assert len(registry) >= 120
    for preset in presets:
        assert preset.id in {
            "refined-lady",
            "petite-sweet",
            "green-tea",
            "tsundere",
            "gentle-healer",
            "cool-queen",
            "genki-girl",
            "sharp-buddy",
            "sunny-classmate",
            "quiet-roommate",
            "reunion-lover",
        }
        assert all(component_id in registry for component_id in preset.components)
        assert detect_component_conflicts(preset.components, registry) == []
    categories = {component.category for component in registry.values()}
    assert categories == set(TraitComponentCategory)


async def test_official_genki_preset_uses_name_suffix() -> None:
    """发行面后缀机制实证：元气少女预设声明「-酱」后缀。"""
    presets = load_official_presets()
    genki = next(preset for preset in presets if preset.id == "genki-girl")
    assert genki.base_card_overrides.name_suffix == "-酱"
    registry = load_official_components()
    result = generate_card_from_preset(genki, registry, base_name="小桃")
    assert result.card.name == "小桃-酱"
    assert result.warnings == []


async def test_official_library_roundtrip_generation() -> None:
    """全部官方预设可确定性生成合法卡片（CharaCardModel 规格全通过）。"""
    presets = load_official_presets()
    registry = load_official_components()
    for preset in presets:
        result = generate_card_from_preset(preset, registry, base_name="基础名")
        assert result.card.name.startswith("基础名")
        assert "；" in result.card.personality
        assert result.warnings == []


async def test_official_cache_reuses_instance() -> None:
    """缺省目录装载命中模块级缓存（同实例返回）。"""
    first = load_official_presets()
    second = load_official_presets()
    assert first is second
    assert load_official_components() is load_official_components()


# ---------------------------------------------------------------------------
# 显式目录装载（tmp_path 合成数据——违约形态 fail loud）
# ---------------------------------------------------------------------------


def _write_library(
    directory: Path,
    components_yaml: str,
    presets_yaml: str,
) -> Path:
    """落盘合成库（components.yaml + presets.yaml）。"""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "components.yaml").write_text(components_yaml, encoding="utf-8")
    (directory / "presets.yaml").write_text(presets_yaml, encoding="utf-8")
    return directory


_MIN_COMPONENTS = """
components:
  - id: comp-a
    name_zh: 组件甲
    description_zh: 碎片甲
    category: temperament
    sample_lines: [示例一, 示例二, 示例三]
  - id: comp-b
    name_zh: 组件乙
    description_zh: 碎片乙
    category: speech
    sample_lines: [示例四, 示例五, 示例六]
"""

_MIN_PRESETS = """
presets:
  - id: preset-a
    name_zh: 预设甲
    tagline_zh: 标语
    description_zh: 描述
    components: [comp-a, comp-b]
"""


async def test_explicit_dir_loads_and_bypasses_cache(tmp_path: Path) -> None:
    """显式目录：合成库装载成功（不触碰模块级缓存）。"""
    directory = _write_library(tmp_path, _MIN_COMPONENTS, _MIN_PRESETS)
    presets = load_official_presets(directory)
    assert [preset.id for preset in presets] == ["preset-a"]
    assert set(load_official_components(directory)) == {"comp-a", "comp-b"}
    # 缓存不受显式装载影响（官方库仍可读）。
    assert load_official_presets() is load_official_presets()


async def test_explicit_dir_missing_files_fail_loud(tmp_path: Path) -> None:
    """数据文件缺失：ValueError fail loud。"""
    with pytest.raises(ValueError, match="缺失"):
        load_official_components(tmp_path)
    _write_library(tmp_path, _MIN_COMPONENTS, "")
    with pytest.raises(ValueError, match="presets"):
        load_official_presets(tmp_path)


async def test_explicit_dir_bad_structure_fail_loud(tmp_path: Path) -> None:
    """顶层结构违约（非映射 / 缺键 / 列表形态错）：ValueError。"""
    directory = tmp_path / "bad-top"
    directory.mkdir(parents=True)
    (directory / "components.yaml").write_text("- a\n- b\n", encoding="utf-8")
    with pytest.raises(ValueError, match="顶层"):
        load_official_components(directory)
    directory2 = tmp_path / "bad-key"
    directory2.mkdir(parents=True)
    (directory2 / "components.yaml").write_text("items: []\n", encoding="utf-8")
    with pytest.raises(ValueError, match="components"):
        load_official_components(directory2)


async def test_explicit_dir_invalid_component_fail_loud(tmp_path: Path) -> None:
    """组件字段违约（缺名 / 非法 id）：装载期 ValueError（含定位信息）。"""
    directory = _write_library(
        tmp_path,
        """
components:
  - id: comp-a
    name_zh: 组件甲
    description_zh: 碎片甲
    category: nonexistent-category
""",
        _MIN_PRESETS,
    )
    with pytest.raises(ValueError, match="第 0 项"):
        load_official_components(directory)


async def test_explicit_dir_duplicate_ids_fail_loud(tmp_path: Path) -> None:
    """组件 / 预设 id 重复：ValueError。"""
    directory = _write_library(
        tmp_path,
        _MIN_COMPONENTS
        + """
  - id: comp-a
    name_zh: 重复
    description_zh: 重复
    category: humor
""",
        _MIN_PRESETS,
    )
    with pytest.raises(ValueError, match="重复"):
        load_official_components(directory)
    directory2 = _write_library(
        tmp_path / "dup-preset",
        _MIN_COMPONENTS,
        _MIN_PRESETS + """
  - id: preset-a
    name_zh: 重复预设
    tagline_zh: 标语
    description_zh: 描述
    components: [comp-a]
""",
    )
    with pytest.raises(ValueError, match="重复"):
        load_official_presets(directory2)


async def test_explicit_dir_unknown_refs_fail_loud(tmp_path: Path) -> None:
    """未知引用：预设引用缺失组件 / 组件冲突声明引用未知组件 → ValueError。"""
    directory = _write_library(
        tmp_path,
        _MIN_COMPONENTS,
        """
presets:
  - id: preset-a
    name_zh: 预设甲
    tagline_zh: 标语
    description_zh: 描述
    components: [comp-a, missing-comp]
""",
    )
    with pytest.raises(ValueError, match="未知组件"):
        load_official_presets(directory)
    directory2 = _write_library(
        tmp_path / "bad-conflict",
        """
components:
  - id: comp-a
    name_zh: 组件甲
    description_zh: 碎片甲
    category: temperament
    conflicts: [ghost-comp]
""",
        _MIN_PRESETS,
    )
    with pytest.raises(ValueError, match="冲突声明"):
        load_official_components(directory2)


async def test_explicit_dir_preset_conflict_fail_loud(tmp_path: Path) -> None:
    """发行冲突预设（组合集命中冲突声明）：装载期 ValueError。"""
    directory = _write_library(
        tmp_path,
        """
components:
  - id: comp-a
    name_zh: 组件甲
    description_zh: 碎片甲
    category: temperament
    conflicts: [comp-b]
    sample_lines: [示例一, 示例二, 示例三]
  - id: comp-b
    name_zh: 组件乙
    description_zh: 碎片乙
    category: speech
    sample_lines: [示例四, 示例五, 示例六]
""",
        _MIN_PRESETS,
    )
    with pytest.raises(ValueError, match="冲突声明"):
        load_official_presets(directory)


async def test_explicit_dir_duplicate_preset_component_ref_fail_loud(tmp_path: Path) -> None:
    """预设组合集内重复引用组件：ValueError。"""
    directory = _write_library(
        tmp_path,
        _MIN_COMPONENTS,
        """
presets:
  - id: preset-a
    name_zh: 预设甲
    tagline_zh: 标语
    description_zh: 描述
    components: [comp-a, comp-a]
""",
    )
    with pytest.raises(ValueError, match="重复组件"):
        load_official_presets(directory)


async def test_explicit_dir_empty_library_fail_loud(tmp_path: Path) -> None:
    """空组件库 / 空预设库：ValueError（至少一项的发行不变式）。"""
    directory = _write_library(tmp_path, "components: []\n", _MIN_PRESETS)
    with pytest.raises(ValueError, match="为空"):
        load_official_components(directory)
    directory2 = _write_library(tmp_path / "empty-presets", _MIN_COMPONENTS, "presets: []\n")
    with pytest.raises(ValueError, match="为空"):
        load_official_presets(directory2)


# ---------------------------------------------------------------------------
# 成熟度分层（组件分档 / 预设上限 / 生成期过滤）
# ---------------------------------------------------------------------------


def _maturity_registry() -> dict[str, TraitComponentModel]:
    """三档成熟度各一组件（生成期过滤矩阵的固定注册表）。"""
    return _registry(
        _component(
            "safe-core",
            name_zh="安全组件",
            description_zh="安全碎片",
            maturity=TraitMaturity.SAFE,
            tone_directives=["安全指令"],
            sample_lines=["安全台词"],
        ),
        _component(
            "sugg-core",
            name_zh="暧昧组件",
            description_zh="暧昧碎片",
            maturity=TraitMaturity.SUGGESTIVE,
            tone_directives=["暧昧指令"],
            sample_lines=["暧昧台词"],
        ),
        _component(
            "expl-core",
            name_zh="直白组件",
            description_zh="直白碎片",
            maturity=TraitMaturity.EXPLICIT,
            tone_directives=["直白指令"],
            sample_lines=["直白台词"],
        ),
    )


async def test_component_maturity_defaults_safe() -> None:
    """组件成熟度缺省 safe（升档须显式声明）。"""
    component = _component("plain-core")
    assert component.maturity is TraitMaturity.SAFE
    with pytest.raises(ValidationError):
        _component("bad-core", maturity="spicy")


async def test_allowed_maturities_mapping() -> None:
    """亲密等级 → 成熟度集合映射（0/1 仅 safe、2 加 suggestive、3 全量）。"""
    assert allowed_maturities_for_intimacy_level(0) == {TraitMaturity.SAFE}
    assert allowed_maturities_for_intimacy_level(1) == {TraitMaturity.SAFE}
    assert allowed_maturities_for_intimacy_level(2) == {
        TraitMaturity.SAFE,
        TraitMaturity.SUGGESTIVE,
    }
    assert allowed_maturities_for_intimacy_level(3) == set(TraitMaturity)
    for bad in (-1, 4, 99):
        with pytest.raises(ValueError, match="0-3"):
            allowed_maturities_for_intimacy_level(bad)


async def test_generate_maturity_filter_matrix() -> None:
    """过滤矩阵：等级 0-3 × 三档成熟度——超限组件不进入任何卡片字段。"""
    registry = _maturity_registry()
    preset = _preset("demo", ["safe-core", "sugg-core", "expl-core"])
    expected = {
        0: {"安全碎片"},
        1: {"安全碎片"},
        2: {"安全碎片", "暧昧碎片"},
        3: {"安全碎片", "暧昧碎片", "直白碎片"},
    }
    for level, fragments in expected.items():
        result = generate_card_from_preset(
            preset, registry, base_name="小测", intimacy_level_override=level
        )
        personality_parts = set(result.card.personality.split("；"))
        assert {part for part in personality_parts if part.endswith("碎片")} == fragments
        for directive in ("安全指令", "暧昧指令", "直白指令"):
            assert (directive in result.card.system_prompt) == (
                directive.removesuffix("指令") + "碎片" in fragments
            )


async def test_generate_uses_preset_level_when_no_override() -> None:
    """未提供覆盖时按预设自身亲密等级过滤（预设值即生成的缺省口径）。"""
    registry = _maturity_registry()
    level_two = _preset(
        "demo-two", ["safe-core", "sugg-core", "expl-core"], intimacy_level=2
    )
    result = generate_card_from_preset(level_two, registry, base_name="小测")
    assert "暧昧碎片" in result.card.personality
    assert "直白碎片" not in result.card.personality
    level_zero = _preset("demo-zero", ["safe-core", "sugg-core"], intimacy_level=0)
    filtered = generate_card_from_preset(level_zero, registry, base_name="小测")
    assert "暧昧碎片" not in filtered.card.personality


async def test_generate_override_can_lower_and_raise() -> None:
    """覆盖可升可降：降级剔除已选组件，升级把更高档组件纳入组合。"""
    registry = _maturity_registry()
    preset = _preset("demo", ["safe-core", "sugg-core", "expl-core"], intimacy_level=2)
    lowered = generate_card_from_preset(
        preset, registry, base_name="小测", intimacy_level_override=0
    )
    assert lowered.card.personality == "测试预设描述；安全碎片"
    raised = generate_card_from_preset(
        preset, registry, base_name="小测", intimacy_level_override=3
    )
    assert raised.card.personality == "测试预设描述；安全碎片；暧昧碎片；直白碎片"


async def test_generate_override_out_of_range_fails_loud() -> None:
    """覆盖等级越界：ValueError（不允许静默夹取）。"""
    registry = _maturity_registry()
    preset = _preset("demo", ["safe-core"])
    for bad in (-1, 4):
        with pytest.raises(ValueError, match="0-3"):
            generate_card_from_preset(
                preset, registry, base_name="小测", intimacy_level_override=bad
            )


async def test_generate_filtered_conflict_pair_does_not_warn() -> None:
    """被过滤掉的冲突组件不参与警告（只对实际参与组合的组件报冲突）。"""
    registry = _registry(
        _component("safe-a", sample_lines=["台词一"]),
        _component("sugg-b", maturity=TraitMaturity.SUGGESTIVE, conflicts=["safe-a"]),
    )
    preset = _preset("demo", ["safe-a", "sugg-b"], intimacy_level=1)
    result = generate_card_from_preset(preset, registry, base_name="小测")
    assert result.warnings == []
    promoted = generate_card_from_preset(
        preset, registry, base_name="小测", intimacy_level_override=2
    )
    assert len(promoted.warnings) == 1


async def test_preset_intimacy_level_bounds() -> None:
    """预设亲密等级值域 0-3（越界构造期拒绝）。"""
    for level in range(4):
        assert _preset("demo", ["a-core"], intimacy_level=level).intimacy_level == level
    for bad in (-1, 4):
        with pytest.raises(ValidationError):
            _preset("demo", ["a-core"], intimacy_level=bad)


async def test_loader_rejects_preset_over_its_ceiling(tmp_path: Path) -> None:
    """发行数据违约：预设组件成熟度越过自身亲密等级上限 → 装载期 ValueError。"""
    directory = _write_library(
        tmp_path,
        """
components:
  - id: comp-a
    name_zh: 组件甲
    description_zh: 碎片甲
    category: temperament
    sample_lines: [示例一, 示例二, 示例三]
  - id: comp-b
    name_zh: 组件乙
    description_zh: 碎片乙
    category: speech
    maturity: suggestive
    sample_lines: [示例四, 示例五, 示例六]
""",
        """
presets:
  - id: preset-a
    name_zh: 预设甲
    tagline_zh: 标语
    description_zh: 描述
    intimacy_level: 1
    components: [comp-a, comp-b]
""",
    )
    with pytest.raises(ValueError, match="亲密等级"):
        load_official_presets(directory)


# ---------------------------------------------------------------------------
# 示例台词与场景脚本（字段界 / 归一 / 渲染）
# ---------------------------------------------------------------------------


async def test_legacy_sample_speech_folds_into_sample_lines() -> None:
    """旧数据兼容：单条 sample_speech 装载时折叠为 sample_lines 首条并参与生成。"""
    component = _component("legacy-core", sample_lines=[], sample_speech="旧台词")
    assert component.sample_lines == ["旧台词"]
    preset = _preset("demo", ["legacy-core"])
    result = generate_card_from_preset(preset, _registry(component), base_name="小测")
    assert "<START>\n小测: 旧台词" in result.card.mes_example


async def test_sample_lines_take_precedence_over_legacy_field() -> None:
    """两字段并存时以 sample_lines 为准（遗留值不重复参与生成）。"""
    component = _component(
        "both-core", sample_lines=["新台词一", "新台词二"], sample_speech="旧台词"
    )
    assert component.sample_lines == ["新台词一", "新台词二"]
    result = generate_card_from_preset(
        _preset("demo", ["both-core"]), _registry(component), base_name="小测"
    )
    assert "旧台词" not in result.card.mes_example
    assert "新台词二" in result.card.mes_example


async def test_sample_lines_length_bounds() -> None:
    """示例台词逐条界：空串与超过 120 字符构造期拒绝。"""
    with pytest.raises(ValidationError, match="为空"):
        _component("blank-line", sample_lines=["ok", "   "])
    with pytest.raises(ValidationError, match="120"):
        _component("long-line", sample_lines=["好" * 121])
    assert _component("max-line", sample_lines=["好" * 120]).sample_lines == ["好" * 120]


async def test_loader_rejects_sample_lines_out_of_range(tmp_path: Path) -> None:
    """官方数据违约：示例台词条数不在 3-5 内 → 装载期 ValueError。"""
    directory = _write_library(
        tmp_path,
        """
components:
  - id: comp-a
    name_zh: 组件甲
    description_zh: 碎片甲
    category: temperament
    sample_lines: [示例一, 示例二]
""",
        _MIN_PRESETS,
    )
    with pytest.raises(ValueError, match="3-5 条"):
        load_official_components(directory)


async def test_scene_script_entry_bounds() -> None:
    """场景脚本界：单状态至多 6 条、单条 1-200 字符、空指令拒绝。"""
    assert SceneScriptSetModel(daily=["日常一"]).daily == ["日常一"]
    with pytest.raises(ValidationError):
        SceneScriptSetModel(daily=[f"指令{index}" for index in range(7)])
    with pytest.raises(ValidationError, match="200"):
        SceneScriptSetModel(daily=["长" * 201])
    with pytest.raises(ValidationError, match="为空"):
        SceneScriptSetModel(daily=["  "])


async def test_scene_scripts_render_named_sections() -> None:
    """场景脚本渲染：命名段按日常 → 暧昧 → 亲密 → 冲突顺序，空段不出现。"""
    scripts = SceneScriptSetModel(
        daily=["日常指令"],
        flirty=["暧昧指令"],
        conflict=["冲突指令"],
    )
    rendered = render_scene_scripts(scripts)
    assert rendered.splitlines() == ["【日常】", "- 日常指令", "【暧昧】", "- 暧昧指令", "【冲突】", "- 冲突指令"]
    assert render_scene_scripts(SceneScriptSetModel()) == ""


async def test_generate_renders_scene_scripts_into_system_prompt() -> None:
    """生成卡片：场景脚本以命名段形式落在 system_prompt 末尾。"""
    registry = _registry(_component("a-core", tone_directives=["基础指令"]))
    preset = _preset(
        "demo",
        ["a-core"],
        scene_scripts={
            "daily": ["一起做早饭"],
            "flirty": ["假装不经意地靠近"],
            "intimate": ["把额头贴过去"],
            "conflict": ["先说事实再讲感受"],
        },
    )
    result = generate_card_from_preset(preset, registry, base_name="小测")
    lines = result.card.system_prompt.splitlines()
    assert lines[0].startswith("你现在扮演「小测」")
    assert "基础指令" in lines
    assert "【日常】" in lines and "【暧昧】" in lines
    assert "【亲密】" in lines and "【冲突】" in lines
    assert lines.index("基础指令") < lines.index("【日常】")
    assert lines.index("【日常】") < lines.index("【暧昧】") < lines.index("【亲密】") < lines.index(
        "【冲突】"
    )


async def test_tsundere_preset_composes_into_system_message_with_persona_body() -> None:
    """集成：傲娇预设生成的覆写卡经 compose 仍携带人格碎片与场景段。"""
    presets = load_official_presets()
    registry = load_official_components()
    tsundere = next(preset for preset in presets if preset.id == "tsundere")
    result = generate_card_from_preset(tsundere, registry, base_name="小傲")
    card = result.card
    assert card.system_prompt.startswith("你现在扮演「小傲」")
    assert "【角色设定】" in card.system_prompt
    assert "【性格特点】" in card.system_prompt

    state = ChatSessionStateModel(persona_id=1, persona_name="tsundere", card=card)
    composed = compose_messages(state)
    system = composed.messages[0].content
    assert card.system_prompt in system
    for fragment in card.personality.split("；"):
        assert fragment in system
    for scene_label in ("【日常】", "【暧昧】", "【亲密】", "【冲突】"):
        assert scene_label in system


async def test_generate_caps_mes_example_lines() -> None:
    """示例台词条数上限：组件示例合计最多 24 条 <START> 块。"""
    components = [
        _component(
            f"cap-{index}",
            description_zh=f"碎片{index}",
            sample_lines=[f"台词{index}-{line}" for line in range(5)],
        )
        for index in range(6)
    ]
    registry = _registry(*components)
    preset = _preset("demo", [component.id for component in components])
    result = generate_card_from_preset(preset, registry, base_name="小测")
    assert result.card.mes_example.count("<START>") == 24


# ---------------------------------------------------------------------------
# 官方库内容不变式（规模 / 类目覆盖 / 成熟度分布 / 预设上限与脚本）
# ---------------------------------------------------------------------------


async def test_official_library_component_scale_and_category_coverage() -> None:
    """官方组件规模与覆盖：120+ 组件、十二类目各至少 8 个。"""
    registry = load_official_components()
    assert len(registry) >= 120
    counts = Counter(component.category for component in registry.values())
    assert set(counts) == set(TraitComponentCategory)
    for category in TraitComponentCategory:
        assert counts[category] >= 8, f"{category} 组件不足: {counts[category]}"


async def test_official_library_sample_lines_complete() -> None:
    """官方组件内容完整：每条 3-5 条示例台词、至少一条语气指令。"""
    for component in load_official_components().values():
        assert 3 <= len(component.sample_lines) <= 5, component.id
        assert component.tone_directives, component.id


async def test_official_library_maturity_distribution_bounds() -> None:
    """官方库成熟度分布：suggestive 25-30%、explicit 10-15%（其余安全）。"""
    components = list(load_official_components().values())
    total = len(components)
    counts = Counter(component.maturity for component in components)
    assert 0.25 <= counts[TraitMaturity.SUGGESTIVE] / total <= 0.30
    assert 0.10 <= counts[TraitMaturity.EXPLICIT] / total <= 0.15
    assert counts[TraitMaturity.SAFE] / total > 0.5


async def test_official_presets_respect_their_own_ceilings() -> None:
    """官方预设不变式：组件成熟度不越过自身上限；explicit 仅存在于亲密包预设。"""
    registry = load_official_components()
    presets = load_official_presets()
    for preset in presets:
        allowed = allowed_maturities_for_intimacy_level(preset.intimacy_level)
        assert all(
            registry[component_id].maturity in allowed
            for component_id in preset.components
        ), preset.id
    explicit_presets = {
        preset.id
        for preset in presets
        if any(
            registry[component_id].maturity is TraitMaturity.EXPLICIT
            for component_id in preset.components
        )
    }
    assert explicit_presets == {"reunion-lover"}
    assert sum(1 for preset in presets if preset.intimacy_level == 3) == 1
    assert all(
        preset.intimacy_level <= 2
        for preset in presets
        if preset.id != "reunion-lover"
    )


async def test_official_presets_carry_scene_scripts() -> None:
    """官方预设场景脚本：日常与暧昧为必备态，四态指令均可用。"""
    for preset in load_official_presets():
        scripts = preset.scene_scripts
        assert len(scripts.daily) >= 1 and len(scripts.flirty) >= 1, preset.id
        for entries in (scripts.daily, scripts.flirty, scripts.intimate, scripts.conflict):
            assert len(entries) <= 6
            assert all(1 <= len(entry) <= 200 for entry in entries)

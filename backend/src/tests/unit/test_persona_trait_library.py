"""harness/persona trait_library 测试——特质组件 / 预设模型规格 + 确定性
卡片生成 + 官方库装载（真实随包 YAML 数据——仓库数据即 hermetic）。

覆盖面：

    - 模型规格：kebab-case id / 分类值域 / 载荷恰一约束（preset 与
      card 互斥——见 dtos 测试）/ 组件冲突引用形态；
    - 纯函数生成：名字后缀拼接 / personality 中文分号拼接序 /
      system_prompt 语气指令行拼接 / mes_example <START> 台词块 /
      建议世界书并入 / 冲突软警告（双向声明 + 去重）/ 未知组件
      fail loud / 空基础名拒绝；
    - 官方库装载：8 预设 + 组件全引用合法 + 官方预设零冲突（发行
      不变式）；显式目录（tmp_path 合成数据）的违约形态 fail loud
      （文件缺失 / 结构违约 / id 重复 / 未知引用 / 发行冲突预设）；
    - 缓存：缺省目录命中模块级缓存（同实例返回）、显式目录绕过。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from packages.harness.persona.chara_card import CharaBookEntryModel
from packages.harness.persona.trait_library import (
    TraitComponentCategory,
    TraitComponentModel,
    TraitPresetModel,
    detect_component_conflicts,
    generate_card_from_preset,
    load_official_components,
    load_official_presets,
)


def _component(component_id: str, **overrides: object) -> TraitComponentModel:
    """构造最小合法组件（覆盖式定制字段）。"""
    payload: dict[str, object] = {
        "id": component_id,
        "name_zh": "测试组件",
        "description_zh": "测试特质碎片",
        "category": TraitComponentCategory.TEMPERAMENT,
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
    """分类值域：六类合法值全通过、未知值拒绝。"""
    for category in TraitComponentCategory:
        component = _component(f"comp-{category.value}", category=category)
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
            sample_speech="台词一",
        ),
        _component(
            "calm-core",
            name_zh="沉静",
            description_zh="碎片二",
            tone_directives=["指令三"],
            sample_speech="台词二",
        ),
    )
    preset = _preset("demo", ["warm-core", "calm-core"])
    result = generate_card_from_preset(preset, registry, base_name="小测")
    assert result.warnings == []
    assert result.card.name == "小测"
    assert result.card.personality == "测试预设描述；碎片一；碎片二"
    assert result.card.system_prompt == "指令一\n指令二\n指令三"
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
            sample_speech="台词",
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
    assert result.card.system_prompt == "指令\n预设提示行"
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
    """官方库形态：8 预设（固定顺序与名单）+ 组件全引用合法 + 零冲突。"""
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
    ]
    assert len(registry) >= 30
    for preset in presets:
        assert preset.id in {"refined-lady", "petite-sweet", "green-tea", "tsundere",
                             "gentle-healer", "cool-queen", "genki-girl", "sharp-buddy"}
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
  - id: comp-b
    name_zh: 组件乙
    description_zh: 碎片乙
    category: speech
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
  - id: comp-b
    name_zh: 组件乙
    description_zh: 碎片乙
    category: speech
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

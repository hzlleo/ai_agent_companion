"""配置模板生成器 byte-stable 测试——gen-examples 产物与提交态零漂移。

生成逻辑在仓库根 scripts/（经根 conftest 加入 sys.path，按顶层模块名
导入——与 ``pixi run gen-examples`` 执行口径一致）；产物为已提交的
backend/ 三件套 .example 文件（绝对路径推导，不受测试 scratch CWD 影响）。
"""

from __future__ import annotations

from pathlib import Path

from generate_examples import generate_all

# backend/ 目录（tests/unit 上溯：unit → tests → src → backend）
_BACKEND_DIR = Path(__file__).resolve().parents[3]


async def test_generated_trio_matches_committed_files() -> None:
    """生成内容与提交的三件套逐字节一致（幂等 byte-stable 契约）。"""
    results = generate_all(write=False)
    expected_paths = {
        "backend/.env.example": _BACKEND_DIR / ".env.example",
        "backend/config.example.yaml": _BACKEND_DIR / "config.example.yaml",
        "backend/extensions_config.example.json": (_BACKEND_DIR / "extensions_config.example.json"),
    }
    assert set(results) == set(expected_paths)
    for rel, path in expected_paths.items():
        assert path.is_file(), f"提交的模板缺失: {rel}"
        committed = path.read_text(encoding="utf-8")
        assert results[rel] == committed, (
            f"生成内容与提交态漂移: {rel}（运行 pixi run gen-examples 再提交）"
        )


async def test_generated_trio_idempotent() -> None:
    """连续两次生成结果一致（无随机性 / 无时间戳渗入）。"""
    first = generate_all(write=False)
    second = generate_all(write=False)
    assert first == second


async def test_env_example_parses_to_fields() -> None:
    """生成的 .env.example 可被向导解析器消费（模板 ↔ 解析闭环）。"""
    from packages.harness.config.wizard_parsers import parse_env_template

    fields = parse_env_template(_BACKEND_DIR / ".env.example")
    keys = {field.key for field in fields}
    # 全量 env 面关键键齐备（Settings 各分节代表键）
    for must_have in (
        "SERVER_HOST",
        "DATABASE_TYPE",
        "DATABASE_PASSWORD",
        "LOG_LEVEL",
        "POOLS_THREAD_POOL_SIZE",
        "LLM_BASE_URL",
        "ENV",
    ):
        assert must_have in keys, f".env.example 缺少 {must_have}"


async def test_config_example_yaml_loads_via_loader(tmp_path: Path) -> None:
    """生成的 config.example.yaml 可被 loader 完整解析（模板 ↔ 加载闭环）。"""
    import shutil

    from packages.harness.config.loader import load_all_config

    shutil.copy(_BACKEND_DIR / "config.example.yaml", tmp_path / "config.yaml")
    shutil.copy(
        _BACKEND_DIR / "extensions_config.example.json", tmp_path / "extensions_config.json"
    )
    llm, extensions = load_all_config(config_dir=tmp_path)
    assert len(llm.models) >= 4
    assert "chat" in llm.models[0].roles or llm.models[0].roles
    assert llm.multimodal_fallback.local is not None
    assert llm.multimodal_fallback.remote is not None
    assert len(extensions.tools) >= 1


async def test_config_example_yaml_disables_night_routing_by_default(
    tmp_path: Path,
) -> None:
    """提交模板经真实 loader：route_policies 为空且 chat_night 未绑定。

    夜间示例（chat-night 条目 / role_bindings / route_policies）在模板中
    全部是注释——新装用户复制模板后不会在夜间被改道到占位端点。
    """
    import shutil

    from packages.harness.config.loader import load_all_config

    shutil.copy(_BACKEND_DIR / "config.example.yaml", tmp_path / "config.yaml")
    shutil.copy(
        _BACKEND_DIR / "extensions_config.example.json", tmp_path / "extensions_config.json"
    )
    llm, _ = load_all_config(config_dir=tmp_path)
    assert llm.route_policies == []
    assert "chat_night" not in llm.role_bindings


async def test_config_example_yaml_carries_search_registry(tmp_path: Path) -> None:
    """config.example.yaml 的 search 段：字段齐全 + 经搜索域加载器闭环。"""
    import shutil

    import yaml

    from packages.harness.config.models import SEARCH_PROVIDER_KINDS, SearchConfig
    from packages.harness.config.search_registry import (
        load_search_config,
        reset_search_runtime_state,
    )

    payload = yaml.safe_load((_BACKEND_DIR / "config.example.yaml").read_text(encoding="utf-8"))
    search = payload["search"]
    providers = search["providers"]
    assert [entry["kind"] for entry in providers] == list(SEARCH_PROVIDER_KINDS)
    assert {entry["api_key_env"] for entry in providers} == {
        "SEARCH_BOCHA_API_KEY",
        "SEARCH_ZHIPU_API_KEY",
    }
    assert search["web_search"]["enabled"] is False
    for entry in providers:
        assert {"name", "kind", "api_key_env", "model", "max_results", "timeout_seconds"} <= set(
            entry
        )
    assert SearchConfig.model_validate(search).providers[0].name == "bocha-main"

    shutil.copy(_BACKEND_DIR / "config.example.yaml", tmp_path / "config.yaml")
    try:
        loaded = load_search_config(config_dir=tmp_path)
    finally:
        reset_search_runtime_state()
    assert [entry.kind for entry in loaded.providers] == list(SEARCH_PROVIDER_KINDS)

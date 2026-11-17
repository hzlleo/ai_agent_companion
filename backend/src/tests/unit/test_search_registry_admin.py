"""搜索注册表管理服务测试——SearchRegistryAdminService（基座 CRUD 消费面）。

测试策略（镜像 test_config_section_admin + test_models_api 双范式）：tmp
config.yaml 种子文件（llm 段 + search 段 + 未知顶层键）经
``load_search_config`` 隔离加载（运行时 + 写路径追踪就绪；复位由
conftest autouse fixture 承担）；故障注入与 LLM 管理面同法（注入仅
首次失败的重建钩子 / monkeypatch os.fsync）。

覆盖面：CRUD round-trip（落盘 + 热重建 + 备份字节一致 + 未知顶层键
与 llm 段保留）/ kind 缺省物化（空 base_url / api_key_env / model 按
kind 落盘显式值）/ 脱敏读取（响应面只回变量名）/ 唯一名 409 / 存在性
404 / name 不可变（请求体携带 name → 400）/ 删除唯一 provider 守卫
（启用 409 + 两步指引；关闭放行）/ 部分更新 merge / 校验零 I/O 副作用 /
原子写失败不留临时文件 / 重建失败内存回滚 / 并发读改写无丢失条目 /
web_search 子块持久化（env 已合并值整体落盘）。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import yaml

from packages.harness.config import search_registry
from packages.harness.config.search_registry import (
    SearchProviderCreateDTO,
    SearchProviderUpdateDTO,
    SearchRegistryAdminService,
    load_search_config,
)
from packages.harness.core.errors import BusinessException, ErrorCode

#: 种子 config.yaml（search 段双 provider + web_search 子块 + llm 段 +
#: 未知顶层键——节外保留断言面）。
_SEED_YAML = """
future_section:
  keep_me: true

llm:
  models: []

search:
  providers:
    - name: bocha-main
      kind: bocha
      base_url: http://bocha.test
      api_key_env: SEARCH_BOCHA_API_KEY
      model: web-search
      max_results: 8
      timeout_seconds: 15
    - name: zhipu-fallback
      kind: zhipu
      base_url: http://zhipu.test
      api_key_env: SEARCH_ZHIPU_API_KEY
      model: search_std
      max_results: 5
      timeout_seconds: 10
  web_search:
    enabled: true
    tool_enabled: true
    daily_budget_per_persona: 30
    daily_budget_global: 100
    cooldown_seconds: 45
"""


def _seed(path: Path, text: str = _SEED_YAML) -> bytes:
    """写入种子 yaml 并返回写前字节（备份一致性断言真源）。"""
    path.write_text(text, encoding="utf-8")
    return path.read_bytes()


def _read_search_section(path: Path) -> dict:
    """读取落盘 yaml 的 search 段。"""
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return data["search"]


class TestCrudRoundTrip:
    async def test_create_persist_rebuild_roundtrip(self, tmp_path: Path) -> None:
        """创建：落盘 + 热重建（运行时从磁盘换血）+ 备份字节一致 + 节外保留。"""
        yaml_path = tmp_path / "config.yaml"
        original_bytes = _seed(yaml_path)
        load_search_config(yaml_path)
        admin = SearchRegistryAdminService()

        state = await admin.create_provider(
            SearchProviderCreateDTO(name="new-provider", kind="bocha"),
            yaml_path=yaml_path,
        )

        names = [entry.name for entry in state.providers]
        assert names == ["bocha-main", "zhipu-fallback", "new-provider"]
        assert state.providers[2].chain_position == 3
        on_disk = _read_search_section(yaml_path)
        assert [entry["name"] for entry in on_disk["providers"]] == names
        # 节外保留：llm 段 + 未知顶层键原样
        data = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
        assert data["future_section"] == {"keep_me": True}
        assert data["llm"] == {"models": []}
        # 备份 = 写前整文件字节；零临时文件残留
        backups = list(tmp_path.glob("config.yaml.bak-*"))
        assert len(backups) == 1
        assert backups[0].read_bytes() == original_bytes
        assert list(tmp_path.glob("config.yaml.tmp-*")) == []
        # 热重建：运行时从磁盘换血
        assert [e.name for e in search_registry.runtime_search_config.providers] == names

    async def test_create_kind_defaults_materialized(self, tmp_path: Path) -> None:
        """kind 缺省物化：空 base_url / api_key_env / model 按官方端点与
        缺省值落盘（显式值语义）。"""
        yaml_path = tmp_path / "config.yaml"
        _seed(yaml_path, "other: 1\n")
        load_search_config(yaml_path)
        admin = SearchRegistryAdminService()

        await admin.create_provider(
            SearchProviderCreateDTO(name="zhipu-2", kind="zhipu"),
            yaml_path=yaml_path,
        )
        entry = _read_search_section(yaml_path)["providers"][-1]
        assert entry["base_url"] == "https://open.bigmodel.cn"
        assert entry["api_key_env"] == "SEARCH_ZHIPU_API_KEY"
        assert entry["model"] == "search_std"
        assert entry["max_results"] == 8
        assert entry["timeout_seconds"] == 15.0

    async def test_update_partial_merge(self, tmp_path: Path) -> None:
        """部分更新：仅出现字段生效；缺席字段保持原值。"""
        yaml_path = tmp_path / "config.yaml"
        _seed(yaml_path)
        load_search_config(yaml_path)
        admin = SearchRegistryAdminService()

        state = await admin.update_provider(
            "bocha-main",
            SearchProviderUpdateDTO(max_results=20, timeout_seconds=30.0),
            yaml_path=yaml_path,
        )
        entry = state.providers[0]
        assert entry.max_results == 20
        assert entry.timeout_seconds == 30.0
        assert entry.kind == "bocha"
        assert entry.base_url == "http://bocha.test"
        on_disk = _read_search_section(yaml_path)["providers"][0]
        assert on_disk["max_results"] == 20
        assert on_disk["base_url"] == "http://bocha.test"

    async def test_update_kind_switch_refills_defaults(self, tmp_path: Path) -> None:
        """kind 切换：按新 kind 物化空值缝（model / api_key_env / base_url）。"""
        yaml_path = tmp_path / "config.yaml"
        _seed(yaml_path)
        load_search_config(yaml_path)
        admin = SearchRegistryAdminService()

        await admin.update_provider(
            "bocha-main",
            SearchProviderUpdateDTO(kind="zhipu", base_url="", api_key_env="", model=""),
            yaml_path=yaml_path,
        )
        entry = _read_search_section(yaml_path)["providers"][0]
        assert entry["kind"] == "zhipu"
        assert entry["base_url"] == "https://open.bigmodel.cn"
        assert entry["api_key_env"] == "SEARCH_ZHIPU_API_KEY"
        assert entry["model"] == "search_std"

    async def test_delete_removes_entry(self, tmp_path: Path) -> None:
        """删除：条目移除 + 剩余链位次重排。"""
        yaml_path = tmp_path / "config.yaml"
        _seed(yaml_path)
        load_search_config(yaml_path)
        admin = SearchRegistryAdminService()

        state = await admin.delete_provider("bocha-main", yaml_path=yaml_path)
        assert [entry.name for entry in state.providers] == ["zhipu-fallback"]
        assert state.providers[0].chain_position == 1
        assert [e.name for e in search_registry.runtime_search_config.providers] == ["zhipu-fallback"]

    async def test_web_search_subblock_persisted_with_effective_values(
        self, tmp_path: Path
    ) -> None:
        """web_search 子块持久化：当前生效值整体落盘（yaml 提供值保持）。"""
        yaml_path = tmp_path / "config.yaml"
        _seed(yaml_path)
        load_search_config(yaml_path)
        admin = SearchRegistryAdminService()

        await admin.create_provider(
            SearchProviderCreateDTO(name="extra", kind="bocha"), yaml_path=yaml_path
        )
        subblock = _read_search_section(yaml_path)["web_search"]
        assert subblock == {
            "enabled": True,
            "tool_enabled": True,
            "daily_budget_per_persona": 30,
            "daily_budget_global": 100,
            "cooldown_seconds": 45,
        }


class TestGuardsAndValidation:
    async def test_create_duplicate_conflict(self, tmp_path: Path) -> None:
        """唯一名：重复创建 → CONFLICT + 零磁盘副作用。"""
        yaml_path = tmp_path / "config.yaml"
        original_bytes = _seed(yaml_path)
        load_search_config(yaml_path)
        admin = SearchRegistryAdminService()

        with pytest.raises(BusinessException) as exc_info:
            await admin.create_provider(
                SearchProviderCreateDTO(name="bocha-main", kind="bocha"),
                yaml_path=yaml_path,
            )
        assert exc_info.value.code == ErrorCode.CONFLICT
        assert yaml_path.read_bytes() == original_bytes
        assert list(tmp_path.glob("config.yaml.bak-*")) == []

    async def test_update_unknown_not_found(self, tmp_path: Path) -> None:
        """存在性：未知条目更新 → NOT_FOUND（details 携带已知清单）。"""
        yaml_path = tmp_path / "config.yaml"
        _seed(yaml_path)
        load_search_config(yaml_path)
        admin = SearchRegistryAdminService()

        with pytest.raises(BusinessException) as exc_info:
            await admin.update_provider(
                "ghost", SearchProviderUpdateDTO(max_results=1), yaml_path=yaml_path
            )
        assert exc_info.value.code == ErrorCode.NOT_FOUND
        assert exc_info.value.details is not None
        assert "bocha-main" in exc_info.value.details["known_providers"]

    async def test_delete_unknown_not_found(self, tmp_path: Path) -> None:
        """存在性：未知条目删除 → NOT_FOUND。"""
        yaml_path = tmp_path / "config.yaml"
        _seed(yaml_path)
        load_search_config(yaml_path)
        admin = SearchRegistryAdminService()

        with pytest.raises(BusinessException) as exc_info:
            await admin.delete_provider("ghost", yaml_path=yaml_path)
        assert exc_info.value.code == ErrorCode.NOT_FOUND

    async def test_delete_only_provider_guard_when_enabled(self, tmp_path: Path) -> None:
        """唯一 provider 守卫：能力启用 + 删最后一条 → CONFLICT + 两步指引。"""
        yaml_path = tmp_path / "config.yaml"
        original_bytes = _seed(
            yaml_path,
            "search:\n"
            "  providers:\n"
            "    - name: solo\n"
            "      kind: bocha\n"
            "      base_url: http://bocha.test\n"
            "  web_search:\n"
            "    enabled: true\n",
        )
        load_search_config(yaml_path)
        admin = SearchRegistryAdminService()

        with pytest.raises(BusinessException) as exc_info:
            await admin.delete_provider("solo", yaml_path=yaml_path)
        assert exc_info.value.code == ErrorCode.CONFLICT
        assert exc_info.value.details is not None
        assert exc_info.value.details["fix"] is not None
        assert yaml_path.read_bytes() == original_bytes

    async def test_delete_only_provider_allowed_when_disabled(self, tmp_path: Path) -> None:
        """守卫解除路径：能力关闭时允许清空注册表（零条目 + 关闭合法）。"""
        yaml_path = tmp_path / "config.yaml"
        _seed(
            yaml_path,
            "search:\n"
            "  providers:\n"
            "    - name: solo\n"
            "      kind: bocha\n"
            "      base_url: http://bocha.test\n"
            "  web_search:\n"
            "    enabled: false\n",
        )
        load_search_config(yaml_path)
        admin = SearchRegistryAdminService()

        state = await admin.delete_provider("solo", yaml_path=yaml_path)
        assert state.providers == []
        assert search_registry.runtime_search_config.providers == []

    async def test_section_validator_rejects_duplicate_before_io(
        self, tmp_path: Path
    ) -> None:
        """节级校验钩子：mutate_section 直调携重复名 → CONFLICT 零 I/O。"""
        from packages.harness.config.models import (
            SearchConfig,
            SearchProviderEntry,
        )

        yaml_path = tmp_path / "config.yaml"
        original_bytes = _seed(yaml_path)
        load_search_config(yaml_path)
        admin = SearchRegistryAdminService()

        invalid = SearchConfig(
            providers=[
                SearchProviderEntry(name="dup", kind="bocha"),
                SearchProviderEntry(name="dup", kind="zhipu"),
            ]
        )
        with pytest.raises(BusinessException) as exc_info:
            await admin.mutate_section(invalid, yaml_path=yaml_path)
        assert exc_info.value.code == ErrorCode.CONFLICT
        assert yaml_path.read_bytes() == original_bytes


class TestFailurePaths:
    async def test_atomic_write_failure_leaves_no_temp_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """原子写失败：fsync 注入失败 → INTERNAL_ERROR + 零残留 + 字节不变。"""
        yaml_path = tmp_path / "config.yaml"
        original_bytes = _seed(yaml_path)
        load_search_config(yaml_path)
        admin = SearchRegistryAdminService()

        def raise_fsync(fd: int) -> None:
            """注入 fsync 失败。"""
            raise OSError("forced fsync failure")

        monkeypatch.setattr("os.fsync", raise_fsync)
        with pytest.raises(BusinessException) as exc_info:
            await admin.create_provider(
                SearchProviderCreateDTO(name="never-landed", kind="bocha"),
                yaml_path=yaml_path,
            )
        assert exc_info.value.code == ErrorCode.INTERNAL_ERROR
        assert list(tmp_path.glob("config.yaml.tmp-*")) == []
        assert yaml_path.read_bytes() == original_bytes

    async def test_rebuild_failure_rolls_back_runtime(self, tmp_path: Path) -> None:
        """热重建失败：运行时回滚写前快照 + 磁盘保留新内容 + CONFIG_INVALID。"""
        yaml_path = tmp_path / "config.yaml"
        _seed(yaml_path)
        load_search_config(yaml_path)
        snapshot_before = search_registry.runtime_search_config.model_copy(deep=True)
        admin = SearchRegistryAdminService()

        calls: list[int] = []
        real_rebuild = admin._rebuild_from_disk

        def flaky_rebuild(path: Path) -> None:
            """仅首次失败的重建钩子。"""
            if not calls:
                calls.append(1)
                raise RuntimeError("forced rebuild failure")
            real_rebuild(path)

        admin._rebuild_from_disk = flaky_rebuild
        with pytest.raises(BusinessException) as exc_info:
            await admin.create_provider(
                SearchProviderCreateDTO(name="rollback-entry", kind="bocha"),
                yaml_path=yaml_path,
            )
        assert exc_info.value.code == ErrorCode.CONFIG_INVALID
        assert "回滚" in exc_info.value.message
        assert search_registry.runtime_search_config == snapshot_before
        # 磁盘语义：保留新内容 + .bak 写前备份
        assert "rollback-entry" in [
            entry["name"] for entry in _read_search_section(yaml_path)["providers"]
        ]
        assert len(list(tmp_path.glob("config.yaml.bak-*"))) >= 1

    async def test_unparseable_existing_rejected(self, tmp_path: Path) -> None:
        """既有 yaml 不可解析：拒绝覆写 + 原字节保留。"""
        yaml_path = tmp_path / "config.yaml"
        admin = SearchRegistryAdminService()
        for garbage in (b"{ invalid flow", b"- a\n- b\n"):
            yaml_path.write_bytes(garbage)
            with pytest.raises(BusinessException) as exc_info:
                await admin.create_provider(
                    SearchProviderCreateDTO(name="clobber", kind="bocha"),
                    yaml_path=yaml_path,
                )
            assert exc_info.value.code == ErrorCode.CONFIG_INVALID
            assert yaml_path.read_bytes() == garbage


class TestConcurrencyAndLoad:
    async def test_write_lock_serializes_read_modify_write(self, tmp_path: Path) -> None:
        """锁内读改写：并发两条 create 全部落盘（无丢失更新）。"""
        yaml_path = tmp_path / "config.yaml"
        _seed(yaml_path, "search:\n  providers: []\n  web_search:\n    enabled: false\n")
        load_search_config(yaml_path)
        admin = SearchRegistryAdminService()

        await asyncio.gather(
            admin.create_provider(
                SearchProviderCreateDTO(name="concurrent-a", kind="bocha"),
                yaml_path=yaml_path,
            ),
            admin.create_provider(
                SearchProviderCreateDTO(name="concurrent-b", kind="zhipu"),
                yaml_path=yaml_path,
            ),
        )
        names = [entry.name for entry in search_registry.runtime_search_config.providers]
        assert sorted(names) == ["concurrent-a", "concurrent-b"]
        on_disk_names = [entry["name"] for entry in _read_search_section(yaml_path)["providers"]]
        assert sorted(on_disk_names) == ["concurrent-a", "concurrent-b"]

    async def test_load_missing_file_keeps_defaults(self, tmp_path: Path) -> None:
        """文件缺失合法态：保持默认运行时 + 写路径仍指向该路径。"""
        missing = tmp_path / "absent.yaml"
        config = load_search_config(missing)
        assert config.providers == []
        assert config.web_search.enabled is False

    async def test_load_invalid_section_fails_loud(self, tmp_path: Path) -> None:
        """search 段结构非法：CONFIG_INVALID fail loud。"""
        yaml_path = tmp_path / "config.yaml"
        yaml_path.write_text("search: [not, a, mapping]\n", encoding="utf-8")
        with pytest.raises(BusinessException) as exc_info:
            load_search_config(yaml_path)
        assert exc_info.value.code == ErrorCode.CONFIG_INVALID

    async def test_masked_state_no_key_values(self, tmp_path: Path) -> None:
        """脱敏读取：state 只回 api_key_env 变量名（密钥值永不出现）。"""
        yaml_path = tmp_path / "config.yaml"
        _seed(yaml_path)
        load_search_config(yaml_path)
        admin = SearchRegistryAdminService()

        state = await admin.state()
        for entry in state.providers:
            assert entry.api_key_env in {"SEARCH_BOCHA_API_KEY", "SEARCH_ZHIPU_API_KEY"}
            assert "test-only" not in entry.model_dump_json()
        assert state.web_search.enabled is True
        assert state.web_search.daily_budget_per_persona == 30

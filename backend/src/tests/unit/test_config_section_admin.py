"""ConfigSectionAdmin 基座单元测试——通用缝契约（玩具配置节替身）。

测试策略：以最小玩具节（``toy`` 段：条目列表 + 绑定映射——与 LLM 注册表
及规划中的搜索 provider 注册表同构的「列表节 + 角色绑定式语义」）子类化
基座，逐缝锁定节无关机械的行为契约：持久化 round-trip（未知顶层键保留 +
备份字节一致）/ 校验钩子零 I/O 副作用 / 原子写失败不留临时文件 / 重建
失败内存回滚（磁盘保留新内容）/ 备份轮换上限 / 写锁读改写串行化（并发
无丢失条目）/ 不可解析既有文件拒绝覆写。故障注入与 LLM 管理面测试
（test_models_api.py）同法：monkeypatch os.fsync / 注入仅首次失败的
重建钩子。

LLM 子类（RegistryAdminService）的端到端行为契约由 test_models_api.py
承担（零改动通过 = 迁移机械不改可观测行为的证明）；本文件只锁基座
自身的缝契约。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import yaml
from pydantic import BaseModel, Field

from packages.harness.config.section_admin import (
    DEFAULT_BACKUP_KEEP,
    ConfigSectionAdmin,
)
from packages.harness.core.errors import BusinessException, ErrorCode

# ---------------------------------------------------------------------------
# 玩具配置节（基座子类——最小缝实现）
# ---------------------------------------------------------------------------


class ToyEntry(BaseModel):
    """玩具节条目（列表节单行——name 唯一标识 + endpoint 载荷）。

    Attributes:
        name: 条目唯一标识（节级校验判据）。
        endpoint: 条目载荷（任意非空字符串）。
    """

    name: str = Field(min_length=1, description="条目唯一标识")
    endpoint: str = Field(min_length=1, description="条目载荷")


class ToySection(BaseModel):
    """玩具配置节（``toy`` 段——条目列表 + 绑定映射）。

    与 LLM 注册表同构的最小列表节形态：``entries`` 列表 + ``bindings``
    显式绑定（绑定式语义的节级校验判据）。

    Attributes:
        entries: 条目列表（声明顺序即落盘顺序）。
        bindings: 显式绑定（键 → 条目名；须指向存在条目）。
    """

    entries: list[ToyEntry] = Field(default_factory=list, description="条目列表")
    bindings: dict[str, str] = Field(default_factory=dict, description="显式绑定")


class ToySectionAdmin(ConfigSectionAdmin[ToySection]):
    """基座测试替身——最小缝实现 + 读改写突变方法（LLM CRUD 同构）。

    运行时树为实例属性 ``runtime``（隔离于 loader 全局——零真实配置
    依赖）；重建钩子镜像 loader 语义：重读磁盘 → 解析 ``toy`` 段 →
    整体换血运行时。
    """

    def __init__(self, default_yaml_path: Path, *, backup_keep: int = DEFAULT_BACKUP_KEEP) -> None:
        """初始化玩具节管理面。

        Args:
            default_yaml_path: 默认持久化目标（``_resolve_yaml_path`` 返回值）。
            backup_keep: 备份保留份数（轮换上限）。
        """
        super().__init__(backup_keep=backup_keep)
        #: 运行时节真源（重建 / 回滚的目标树）。
        self.runtime = ToySection()
        self._default_yaml_path = default_yaml_path

    def _section_key(self) -> str:
        """本管理面持有的顶层键（玩具节 = ``toy``）。"""
        return "toy"

    def _current_section(self) -> ToySection:
        """读取运行时当前节（实例属性真源）。"""
        return self.runtime

    def _apply_section_to_runtime(self, section: ToySection) -> None:
        """把节整体应用到运行时（整体引用替换）。"""
        self.runtime = section

    def _render_section(self, section: ToySection) -> dict[str, object]:
        """节 Model Class → ``toy`` 段 dict（无损回读序列化）。"""
        return {
            "entries": [entry.model_dump() for entry in section.entries],
            "bindings": dict(section.bindings),
        }

    def _validate_section(self, section: ToySection) -> None:
        """节级校验：条目名唯一 + 绑定指向存在条目（绑定式语义闸门）。

        Args:
            section: 待校验的新节。

        Raises:
            BusinessException: 条目重名 / 绑定指向不存在条目
                （``CONFLICT``）。
        """
        names = [entry.name for entry in section.entries]
        duplicates = sorted({name for name in names if names.count(name) > 1})
        if duplicates:
            raise BusinessException(
                code=ErrorCode.CONFLICT,
                message=f"玩具节条目名重复: {duplicates}",
                details={"duplicates": duplicates},
            )
        known = set(names)
        dangling = sorted(key for key, target in section.bindings.items() if target not in known)
        if dangling:
            raise BusinessException(
                code=ErrorCode.CONFLICT,
                message=f"玩具节绑定指向不存在条目: {dangling}",
                details={"dangling_bindings": dangling},
            )

    def _rebuild_from_disk(self, yaml_path: Path) -> None:
        """热重建钩子：重读磁盘 → 解析 ``toy`` 段 → 整体换血运行时。

        Args:
            yaml_path: 刚持久化的 yaml 路径。

        Raises:
            Exception: 落盘内容不可解析（调用方回滚内存后包装
                ``CONFIG_INVALID``）。
        """
        data = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
        section = (data or {}).get("toy") or {}
        self.runtime = ToySection.model_validate(section)

    def _resolve_yaml_path(self) -> Path:
        """默认持久化目标（构造期注入的固定路径）。"""
        return self._default_yaml_path

    async def add_entry(self, name: str, endpoint: str, *, yaml_path: Path | None = None) -> None:
        """读改写突变方法（锁内构造——LLM create_entry 同构范式）。

        Args:
            name: 新条目名。
            endpoint: 新条目载荷。
            yaml_path: 目标 yaml（None = 默认路径）。

        Raises:
            BusinessException: 条目名已存在（``CONFLICT``）。
        """
        async with self._write_lock:
            path = yaml_path if yaml_path is not None else self._resolve_yaml_path()
            current = self._current_section()
            if any(entry.name == name for entry in current.entries):
                raise BusinessException(
                    code=ErrorCode.CONFLICT,
                    message=f"玩具节条目名已存在: {name}",
                    details={"name": name},
                )
            new_section = ToySection(
                entries=[*current.entries, ToyEntry(name=name, endpoint=endpoint)],
                bindings=dict(current.bindings),
            )
            await self._persist_and_rebuild(
                new_section, snapshot=current.model_copy(deep=True), yaml_path=path
            )


# ---------------------------------------------------------------------------
# 铺底辅助
# ---------------------------------------------------------------------------


def _seed_yaml(path: Path, entries: list[dict[str, str]] | None = None) -> bytes:
    """写入种子 yaml（未知顶层键 + toy 节），返回写前字节。

    Args:
        path: 目标 yaml 路径。
        entries: 种子条目（默认两条）。

    Returns:
        写入的完整字节（备份字节一致断言的真源）。
    """
    data = {
        "future_section": {"keep_me": True},
        "toy": {
            "entries": entries
            if entries is not None
            else [
                {"name": "seed-a", "endpoint": "http://127.0.0.1:1"},
                {"name": "seed-b", "endpoint": "http://127.0.0.1:2"},
            ],
            "bindings": {"primary": "seed-a"},
        },
    }
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return path.read_bytes()


def _read_section(path: Path) -> dict:
    """读取落盘 yaml 的 ``toy`` 段。

    Args:
        path: yaml 路径。

    Returns:
        ``toy`` 段 dict。
    """
    return yaml.safe_load(path.read_text(encoding="utf-8"))["toy"]


# ---------------------------------------------------------------------------
# 持久化 round-trip（节替换 + 未知顶层键保留 + 备份字节一致）
# ---------------------------------------------------------------------------


async def test_mutate_section_persist_rebuild_roundtrip(tmp_path: Path) -> None:
    """整体替换突变：落盘节替换 + 未知顶层键保留 + 备份=写前字节 + 运行时换血。"""
    yaml_path = tmp_path / "config.yaml"
    original_bytes = _seed_yaml(yaml_path)
    admin = ToySectionAdmin(yaml_path)
    admin._rebuild_from_disk(yaml_path)  # 种子装载（运行时 = 磁盘初始态）

    new_section = ToySection(
        entries=[ToyEntry(name="replacement", endpoint="http://127.0.0.1:9")],
        bindings={},
    )
    await admin.mutate_section(new_section, yaml_path=yaml_path)

    on_disk = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    assert on_disk["future_section"] == {"keep_me": True}  # 节外顶层键保留
    assert [entry["name"] for entry in on_disk["toy"]["entries"]] == ["replacement"]
    backups = list(tmp_path.glob("config.yaml.bak-*"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == original_bytes  # 备份 = 写前整文件字节
    assert list(tmp_path.glob("config.yaml.tmp-*")) == []  # 零临时文件残留
    # 热重建：运行时从磁盘换血（非入参引用——经重读路径证明可加载）。
    assert admin.runtime == ToySection.model_validate(_read_section(yaml_path))


async def test_mutate_section_fresh_path_creates_without_backup(tmp_path: Path) -> None:
    """首次创建形态：无既有文件 → 直接创建，无写前备份 / 无临时残留。"""
    fresh_dir = tmp_path / "fresh"
    fresh_dir.mkdir()
    fresh_path = fresh_dir / "config.yaml"
    admin = ToySectionAdmin(fresh_path)

    await admin.mutate_section(
        ToySection(entries=[ToyEntry(name="first", endpoint="http://127.0.0.1:1")]),
        yaml_path=fresh_path,
    )

    assert fresh_path.is_file()
    assert list(fresh_dir.glob("config.yaml.bak-*")) == []
    assert list(fresh_dir.glob("config.yaml.tmp-*")) == []
    assert [entry.name for entry in admin.runtime.entries] == ["first"]


async def test_mutate_section_default_path_resolution(tmp_path: Path) -> None:
    """默认目标解析缝：未显式给路径时落在 ``_resolve_yaml_path`` 目标。"""
    default_path = tmp_path / "default.yaml"
    admin = ToySectionAdmin(default_path)

    await admin.mutate_section(
        ToySection(entries=[ToyEntry(name="via-default", endpoint="http://127.0.0.1:1")])
    )

    assert default_path.is_file()
    assert "via-default" in default_path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 校验钩子（节级闸门——零 I/O 副作用）
# ---------------------------------------------------------------------------


async def test_section_validator_rejects_before_any_io(tmp_path: Path) -> None:
    """节级校验失败：CONFLICT 且零磁盘副作用（无备份 / 无临时 / 字节不变）。"""
    yaml_path = tmp_path / "config.yaml"
    original_bytes = _seed_yaml(yaml_path)
    admin = ToySectionAdmin(yaml_path)
    admin._rebuild_from_disk(yaml_path)

    invalid = ToySection(
        entries=[
            ToyEntry(name="dup", endpoint="http://127.0.0.1:1"),
            ToyEntry(name="dup", endpoint="http://127.0.0.1:2"),
        ]
    )
    with pytest.raises(BusinessException) as excinfo:
        await admin.mutate_section(invalid, yaml_path=yaml_path)
    assert excinfo.value.code == ErrorCode.CONFLICT
    assert "重复" in excinfo.value.message

    assert yaml_path.read_bytes() == original_bytes
    assert list(tmp_path.glob("config.yaml.bak-*")) == []
    assert list(tmp_path.glob("config.yaml.tmp-*")) == []
    assert [entry.name for entry in admin.runtime.entries] == ["seed-a", "seed-b"]


async def test_section_validator_rejects_dangling_binding(tmp_path: Path) -> None:
    """绑定式语义闸门：绑定指向不存在条目 → CONFLICT。"""
    admin = ToySectionAdmin(tmp_path / "config.yaml")
    invalid = ToySection(
        entries=[ToyEntry(name="solo", endpoint="http://127.0.0.1:1")],
        bindings={"primary": "ghost-entry"},
    )
    with pytest.raises(BusinessException) as excinfo:
        await admin.mutate_section(invalid)
    assert excinfo.value.code == ErrorCode.CONFLICT


# ---------------------------------------------------------------------------
# 原子写失败（临时文件清理 + 原文件不变 + 内存不变）
# ---------------------------------------------------------------------------


async def test_atomic_write_failure_leaves_no_temp_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """原子写失败：fsync 注入失败 → INTERNAL_ERROR + 零残留 + 字节不变。"""
    yaml_path = tmp_path / "config.yaml"
    original_bytes = _seed_yaml(yaml_path)
    admin = ToySectionAdmin(yaml_path)
    admin._rebuild_from_disk(yaml_path)

    def raise_fsync(fd: int) -> None:
        raise OSError("forced fsync failure")

    monkeypatch.setattr("os.fsync", raise_fsync)
    with pytest.raises(BusinessException) as excinfo:
        await admin.mutate_section(
            ToySection(entries=[ToyEntry(name="never-landed", endpoint="http://127.0.0.1:1")]),
            yaml_path=yaml_path,
        )
    assert excinfo.value.code == ErrorCode.INTERNAL_ERROR

    assert list(tmp_path.glob("config.yaml.tmp-*")) == []
    assert yaml_path.read_bytes() == original_bytes
    assert [entry.name for entry in admin.runtime.entries] == ["seed-a", "seed-b"]


# ---------------------------------------------------------------------------
# 重建失败（内存回滚 + 磁盘保留新内容 + 备份保留）
# ---------------------------------------------------------------------------


async def test_rebuild_failure_rolls_back_runtime(tmp_path: Path) -> None:
    """热重建失败：运行时回滚写前快照 + 磁盘保留新内容与备份 + CONFIG_INVALID。"""
    yaml_path = tmp_path / "config.yaml"
    _seed_yaml(yaml_path)
    admin = ToySectionAdmin(yaml_path)
    admin._rebuild_from_disk(yaml_path)
    snapshot_before = admin.runtime.model_copy(deep=True)

    calls: list[int] = []
    real_rebuild = admin._rebuild_from_disk

    def flaky_rebuild(path: Path) -> None:
        if not calls:
            calls.append(1)
            raise RuntimeError("forced rebuild failure")
        real_rebuild(path)

    admin._rebuild_from_disk = flaky_rebuild  # 注入仅首次失败（实例级——测试替身自有状态）
    new_section = ToySection(
        entries=[ToyEntry(name="rollback-entry", endpoint="http://127.0.0.1:1")]
    )
    with pytest.raises(BusinessException) as excinfo:
        await admin.mutate_section(new_section, yaml_path=yaml_path)
    assert excinfo.value.code == ErrorCode.CONFIG_INVALID
    assert "回滚" in excinfo.value.message
    assert excinfo.value.details["rollback"] == "in-memory"

    assert admin.runtime == snapshot_before  # 内存回滚写前快照
    assert [entry.name for entry in admin.runtime.entries] == ["seed-a", "seed-b"]
    # 磁盘语义（文档化决策）：保留新内容 + .bak 写前备份。
    assert "rollback-entry" in [entry["name"] for entry in _read_section(yaml_path)["entries"]]
    assert len(list(tmp_path.glob("config.yaml.bak-*"))) >= 1


# ---------------------------------------------------------------------------
# 备份轮换（backup_keep 上限）
# ---------------------------------------------------------------------------


async def test_backup_rotation_keeps_configured_keep(tmp_path: Path) -> None:
    """备份轮换：超过 backup_keep 次持久化后仅保留最新 keep 份。"""
    keep = 3
    yaml_path = tmp_path / "config.yaml"
    _seed_yaml(yaml_path, entries=[])
    admin = ToySectionAdmin(yaml_path, backup_keep=keep)

    for index in range(keep + 2):
        await admin.mutate_section(
            ToySection(entries=[ToyEntry(name=f"rot-{index}", endpoint="http://127.0.0.1:1")]),
            yaml_path=yaml_path,
        )
    backups = list(tmp_path.glob("config.yaml.bak-*"))
    assert len(backups) == keep


# ---------------------------------------------------------------------------
# 写锁串行化（读改写无丢失条目）
# ---------------------------------------------------------------------------


async def test_write_lock_serializes_read_modify_write(tmp_path: Path) -> None:
    """锁内读改写：并发两条 add_entry 全部落盘（无丢失更新）。"""
    yaml_path = tmp_path / "config.yaml"
    _seed_yaml(yaml_path, entries=[])
    admin = ToySectionAdmin(yaml_path)

    await asyncio.gather(
        admin.add_entry("concurrent-a", "http://127.0.0.1:1"),
        admin.add_entry("concurrent-b", "http://127.0.0.1:2"),
    )

    names = [entry.name for entry in admin.runtime.entries]
    assert sorted(names) == ["concurrent-a", "concurrent-b"]
    on_disk_names = [entry["name"] for entry in _read_section(yaml_path)["entries"]]
    assert sorted(on_disk_names) == ["concurrent-a", "concurrent-b"]


async def test_add_entry_duplicate_conflict(tmp_path: Path) -> None:
    """读改写突变方法：锁内唯一性校验 → CONFLICT。"""
    yaml_path = tmp_path / "config.yaml"
    _seed_yaml(yaml_path, entries=[{"name": "existing", "endpoint": "http://127.0.0.1:1"}])
    admin = ToySectionAdmin(yaml_path)
    admin._rebuild_from_disk(yaml_path)

    with pytest.raises(BusinessException) as excinfo:
        await admin.add_entry("existing", "http://127.0.0.1:2")
    assert excinfo.value.code == ErrorCode.CONFLICT


# ---------------------------------------------------------------------------
# 不可解析既有文件（拒绝覆写）
# ---------------------------------------------------------------------------


async def test_unparseable_existing_config_rejected(tmp_path: Path) -> None:
    """既有 yaml 不可解析 / 顶层非 mapping：拒绝覆写 + 原字节保留。"""
    yaml_path = tmp_path / "config.yaml"
    admin = ToySectionAdmin(yaml_path)
    for garbage in (b"{ invalid flow", b"- a\n- b\n"):
        yaml_path.write_bytes(garbage)
        with pytest.raises(BusinessException) as excinfo:
            await admin.mutate_section(
                ToySection(entries=[ToyEntry(name="clobber", endpoint="http://127.0.0.1:1")]),
                yaml_path=yaml_path,
            )
        assert excinfo.value.code == ErrorCode.CONFIG_INVALID
        assert "拒绝覆写" in excinfo.value.message
        assert yaml_path.read_bytes() == garbage

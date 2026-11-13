"""app/api/models 路由测试——注册表管理全端点（ASGITransport 零真实网络）。

测试策略：tmp 配置目录（config.yaml 种子文件 + 隔离加载——
``load_all_config(config_dir=...)`` 同步设置运行时与写路径追踪）；本地
装配 FastAPI app（router + 共享异常处理器）；密钥防泄漏断言经
monkeypatch 注入**真实存在的**密钥环境变量值（响应面永不出现该值）。

覆盖面：脱敏读取 + 解析状态 / 创建→落盘→热重建 round-trip / 部分更新
（含 concurrency 显式 null 重置）/ 删除守卫与两步解绑流 / 唯一名 409 /
非法 base_url·未知字段·未知角色 400 / 原子写失败不留临时文件 / 重建
失败内存回滚（注入仅首次失败的解析器）/ 备份生成与轮换 / 处理器直调
契约面。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import yaml
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.api.models import (
    create_entry,
    delete_entry,
    list_entries,
    list_roles,
    registry_admin_service,
    update_entry,
)
from app.api.models import (
    router as models_router,
)
from app.middleware.error_handler import register_exception_handlers
from packages.harness.config import loader as config_loader
from packages.harness.config.loader import get_loaded_config_yaml_path, reset_config_runtime_state
from packages.harness.config.model_resolver import get_model_resolver
from packages.harness.config.registry_admin import REGISTRY_BACKUP_KEEP, RegistryAdminService
from packages.harness.config.registry_admin_dtos import (
    ModelEntryCreateDTO,
    ModelEntryUpdateDTO,
)

#: 密钥环境变量真实值（防泄漏断言真源——响应面出现即失败）。
_SECRET_VALUE = "sk-super-secret-do-not-leak-9f3a"

#: 种子 config.yaml（两条目 + 显式 dream 绑定 + 未知顶层键保留断言面）。
_SEED_YAML = """
future_section:
  keep_me: true

llm:
  models:
    - name: main-chat
      provider: openai_compatible
      base_url: https://api.example.com/v1
      api_key_env: MAIN_CHAT_API_KEY
      model: glm-5.3
      roles: [chat, duty]
      concurrency: 4
    - name: local-embed
      provider: openai_compatible
      base_url: http://127.0.0.1:8002/v1
      api_key_env: ""
      model: bge-m3
      roles: [embed]
  role_bindings:
    dream: main-chat
  multimodal_fallback:
    local: null
    remote: null
"""


@pytest.fixture
async def config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Path]:
    """tmp 配置目录（种子 config.yaml + 隔离加载 + 密钥环境变量注入）。

    Args:
        tmp_path: pytest 一次性目录。
        monkeypatch: 测试内 monkeypatch（环境变量注入）。

    Yields:
        已完成 ``load_all_config`` 的配置目录（运行时 + 写路径追踪就绪；
        复位由 conftest autouse fixture 承担）。
    """
    directory = tmp_path / "registry-config"
    directory.mkdir()
    (directory / "config.yaml").write_text(_SEED_YAML, encoding="utf-8")
    monkeypatch.setenv("MAIN_CHAT_API_KEY", _SECRET_VALUE)
    config_loader.load_all_config(config_dir=directory)
    yield directory


@pytest.fixture
async def models_app(config_dir: Path) -> AsyncIterator[FastAPI]:
    """本地装配的最小 FastAPI app（本路由 + 异常处理器；零 DB 依赖）。

    Args:
        config_dir: 已隔离加载的配置目录（写路径追踪目标）。

    Yields:
        已注册路由与异常处理器的 :class:`FastAPI` 实例。
    """
    app = FastAPI(docs_url=None, redoc_url=None)
    app.include_router(models_router)
    register_exception_handlers(app)
    yield app


@pytest.fixture
async def client(models_app: FastAPI) -> AsyncIterator[AsyncClient]:
    """ASGITransport 客户端（零真实网络 / 零真实服务器）。

    Args:
        models_app: 本地装配的 FastAPI app。

    Yields:
        :class:`httpx.AsyncClient`（base_url=http://test）。
    """
    transport = ASGITransport(app=models_app)
    async with AsyncClient(transport=transport, base_url="http://test") as async_client:
        yield async_client


def _read_yaml(path: Path) -> dict:
    """读取 yaml 文件为 dict（测试铺底断言辅助）。

    Args:
        path: yaml 文件路径。

    Returns:
        解析后的 dict。
    """
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _create_payload(name: str = "vision-helper", **overrides: object) -> dict[str, object]:
    """构造最小创建请求体（name + base_url + model）。"""
    payload: dict[str, object] = {
        "name": name,
        "base_url": "http://127.0.0.1:9000/v1",
        "model": "qwen2.5-vl-7b",
    }
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------------------
# 读取面（脱敏 + 解析状态）
# ---------------------------------------------------------------------------


async def test_list_entries_masked_with_resolution_state(
    client: AsyncClient, config_dir: Path
) -> None:
    """条目脱敏：api_key_env 只回变量名、密钥值永不出现 + 解析状态。"""
    response = await client.get("/api/v1/models/entries")
    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    # 密钥防泄漏：环境变量里真实存在密钥值，响应全文不得出现。
    assert _SECRET_VALUE not in response.text
    assert "api_key" not in response.text.replace("api_key_env", "")
    data = body["data"]
    assert [entry["name"] for entry in data["entries"]] == ["main-chat", "local-embed"]
    main_chat = data["entries"][0]
    assert main_chat["api_key_env"] == "MAIN_CHAT_API_KEY"
    assert main_chat["model"] == "glm-5.3"
    assert main_chat["concurrency"] == 4
    assert main_chat["multimodal"] is False
    assert main_chat["roles"] == ["chat", "duty"]
    # 解析状态：显式 dream 绑定 + 声明 chat/duty 都解析到 main-chat（字典序）。
    assert main_chat["resolved_roles"] == ["chat", "dream", "duty"]
    assert data["role_bindings"] == {"dream": "main-chat"}
    local_embed = data["entries"][1]
    assert local_embed["api_key_env"] == ""
    assert local_embed["resolved_roles"] == ["embed"]


async def test_list_roles_bound_and_unbound(client: AsyncClient, config_dir: Path) -> None:
    """角色面：五角色齐备；look_at 未绑定；dream 显式绑定到 main-chat。"""
    response = await client.get("/api/v1/models/roles")
    assert response.status_code == 200
    bindings = response.json()["data"]
    assert {row["role"] for row in bindings} == {"chat", "duty", "dream", "look_at", "embed"}
    by_role = {row["role"]: row for row in bindings}
    assert by_role["chat"] == {"role": "chat", "bound": True, "entry_name": "main-chat"}
    assert by_role["dream"] == {"role": "dream", "bound": True, "entry_name": "main-chat"}
    assert by_role["embed"] == {"role": "embed", "bound": True, "entry_name": "local-embed"}
    assert by_role["look_at"] == {"role": "look_at", "bound": False, "entry_name": None}


# ---------------------------------------------------------------------------
# 创建（round-trip：内存 → 磁盘 → 热重建）
# ---------------------------------------------------------------------------


async def test_create_entry_persist_reload_roundtrip(
    client: AsyncClient, config_dir: Path
) -> None:
    """创建 → 落盘（含未知顶层键保留 + .bak 备份）→ 热重建（解析器生效）。"""
    yaml_path = config_dir / "config.yaml"
    original_bytes = yaml_path.read_bytes()

    response = await client.post(
        "/api/v1/models/entries",
        json=_create_payload(roles=["look_at"], multimodal=True, concurrency=2),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["message"] == "模型条目已创建"

    # 磁盘 round-trip：新条目落盘 + 未知顶层键保留 + 写前备份生成。
    data_on_disk = _read_yaml(yaml_path)
    assert [m["name"] for m in data_on_disk["llm"]["models"]] == [
        "main-chat",
        "local-embed",
        "vision-helper",
    ]
    assert data_on_disk["future_section"] == {"keep_me": True}
    assert data_on_disk["llm"]["role_bindings"] == {"dream": "main-chat"}
    backups = list(config_dir.glob("config.yaml.bak-*"))
    assert len(backups) == 1
    # 备份 = 写前内容整文件快照（字节一致）。
    assert backups[0].read_bytes() == original_bytes

    # 热重建：运行时 + 解析器已从磁盘换血（look_at → vision-helper）。
    created = next(
        e for e in body["data"]["entries"] if e["name"] == "vision-helper"
    )
    assert created["multimodal"] is True
    assert created["concurrency"] == 2
    assert created["resolved_roles"] == ["look_at"]
    resolver = get_model_resolver()
    assert resolver.resolve_optional("look_at") is not None
    assert resolver.resolve_optional("look_at").name == "vision-helper"
    assert _SECRET_VALUE not in response.text


async def test_create_entry_duplicate_name_409(client: AsyncClient, config_dir: Path) -> None:
    """唯一名冲突：409 + ResultDTO error 形态（code=CONFLICT）。"""
    response = await client.post(
        "/api/v1/models/entries", json=_create_payload(name="main-chat")
    )
    assert response.status_code == 409
    body = response.json()
    assert body["success"] is False
    assert body["code"] == "CONFLICT"
    assert body["data"]["name"] == "main-chat"


async def test_create_entry_invalid_payloads_400(client: AsyncClient, config_dir: Path) -> None:
    """请求校验失败 400：非法 base_url / 空 model / 空白 name / 未知角色 / 密钥字段。"""
    cases: list[dict[str, object]] = [
        _create_payload(base_url="ftp://127.0.0.1:9000/v1"),
        _create_payload(model=""),
        _create_payload(name="   "),
        _create_payload(roles=["codex"]),
        _create_payload(api_key=_SECRET_VALUE),
        _create_payload(concurrency=0),
    ]
    for payload in cases:
        response = await client.post("/api/v1/models/entries", json=payload)
        assert response.status_code == 400, payload
        body = response.json()
        assert body["success"] is False
        assert body["code"] == "VALIDATION_ERROR"


# ---------------------------------------------------------------------------
# 更新（部分语义 + name 不可变）
# ---------------------------------------------------------------------------


async def test_update_entry_partial_semantics(client: AsyncClient, config_dir: Path) -> None:
    """部分更新：提供字段生效 + concurrency 显式 null 重置 + roles 清空。"""
    await client.post(
        "/api/v1/models/entries",
        json=_create_payload(roles=["look_at"], concurrency=2),
    )
    response = await client.put(
        "/api/v1/models/entries/vision-helper",
        json={
            "base_url": "http://127.0.0.1:9100/v1",
            "concurrency": None,
            "roles": [],
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["message"] == "模型条目已更新"
    updated = next(e for e in body["data"]["entries"] if e["name"] == "vision-helper")
    assert updated["base_url"] == "http://127.0.0.1:9100/v1"
    assert updated["concurrency"] is None  # 显式 null → 重置全局默认
    assert updated["roles"] == []
    assert updated["resolved_roles"] == []  # look_at 解除绑定
    # 磁盘同步：concurrency 键省略（全局默认语义）。
    on_disk = _read_yaml(config_dir / "config.yaml")
    entry_section = on_disk["llm"]["models"][2]
    assert "concurrency" not in entry_section
    assert entry_section["roles"] == []
    assert get_model_resolver().resolve_optional("look_at") is None


async def test_update_entry_missing_404_and_name_immutable_400(
    client: AsyncClient, config_dir: Path
) -> None:
    """更新缺失条目 404；请求体携带 name（不可变）→ 400。"""
    missing = await client.put(
        "/api/v1/models/entries/nope", json={"base_url": "http://127.0.0.1:1/v1"}
    )
    assert missing.status_code == 404
    body = missing.json()
    assert body["code"] == "NOT_FOUND"
    assert body["data"]["name"] == "nope"
    assert "main-chat" in body["data"]["known_entries"]

    renamed = await client.put(
        "/api/v1/models/entries/main-chat", json={"name": "renamed"}
    )
    assert renamed.status_code == 400
    assert renamed.json()["code"] == "VALIDATION_ERROR"


async def test_update_entry_invalid_base_url_400(client: AsyncClient, config_dir: Path) -> None:
    """更新面非法 base_url：请求校验失败 400。"""
    response = await client.put(
        "/api/v1/models/entries/main-chat", json={"base_url": "not-a-url"}
    )
    assert response.status_code == 400
    assert response.json()["code"] == "VALIDATION_ERROR"


# ---------------------------------------------------------------------------
# 删除（守卫 + 两步解绑流）
# ---------------------------------------------------------------------------


async def test_delete_guard_and_unbind_then_delete_flow(
    client: AsyncClient, config_dir: Path
) -> None:
    """删除守卫：绑定条目 409（bound_roles + 指引）→ 解绑 → 删除 200。"""
    await client.post(
        "/api/v1/models/entries", json=_create_payload(name="guard-entry", roles=["look_at"])
    )
    guarded = await client.delete("/api/v1/models/entries/guard-entry")
    assert guarded.status_code == 409
    body = guarded.json()
    assert body["code"] == "CONFLICT"
    assert body["data"]["bound_roles"] == ["look_at"]
    assert "roles" in body["data"]["fix"]

    unbind = await client.put("/api/v1/models/entries/guard-entry", json={"roles": []})
    assert unbind.status_code == 200

    deleted = await client.delete("/api/v1/models/entries/guard-entry")
    assert deleted.status_code == 200
    assert deleted.json()["message"] == "模型条目已删除"
    names = [e["name"] for e in deleted.json()["data"]["entries"]]
    assert names == ["main-chat", "local-embed"]
    # 磁盘同步：条目行移除。
    on_disk = _read_yaml(config_dir / "config.yaml")
    assert [m["name"] for m in on_disk["llm"]["models"]] == ["main-chat", "local-embed"]


async def test_delete_explicitly_bound_entry_409(client: AsyncClient, config_dir: Path) -> None:
    """显式 role_bindings 指向的条目：删除同样被守卫拦截（409）。"""
    response = await client.delete("/api/v1/models/entries/main-chat")
    assert response.status_code == 409
    body = response.json()
    # 显式 dream 绑定 + 声明 chat/duty 合并的绑定集（字典序）。
    assert body["data"]["bound_roles"] == ["chat", "dream", "duty"]


async def test_delete_missing_404(client: AsyncClient, config_dir: Path) -> None:
    """删除缺失条目：404 + NOT_FOUND。"""
    response = await client.delete("/api/v1/models/entries/nope")
    assert response.status_code == 404
    assert response.json()["code"] == "NOT_FOUND"


# ---------------------------------------------------------------------------
# 写路径故障面（原子性 / 回滚 / 备份轮换）
# ---------------------------------------------------------------------------


async def test_atomic_write_failure_leaves_no_temp_file(
    client: AsyncClient, config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """原子写失败：临时文件清理 + 原文件字节不变 + 内存注册表不变 + 500。"""
    yaml_path = config_dir / "config.yaml"
    original_bytes = yaml_path.read_bytes()

    def raise_fsync(fd: int) -> None:
        raise OSError("forced fsync failure")

    monkeypatch.setattr("os.fsync", raise_fsync)
    response = await client.post(
        "/api/v1/models/entries", json=_create_payload(name="never-landed")
    )
    assert response.status_code == 500
    body = response.json()
    assert body["success"] is False
    assert body["code"] == "INTERNAL_ERROR"

    assert list(config_dir.glob("config.yaml.tmp-*")) == []
    assert yaml_path.read_bytes() == original_bytes
    names = [entry.name for entry in config_loader.runtime_llm_config.models]
    assert names == ["main-chat", "local-embed"]
    assert get_model_resolver().resolve_optional("look_at") is None


async def test_rebuild_failure_rolls_back_in_memory_registry(
    client: AsyncClient, config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """热重建失败：内存注册表回滚写前快照（磁盘保留新内容 + 备份）+ 422。

    注入仅首次失败的解析器（回滚路径复用同一重建入口，需可成功执行）。
    """
    from collections.abc import Mapping, Sequence

    from packages.harness.config.model_entry import LlmModelEntry

    calls: list[int] = []
    real_rebuild = config_loader.rebuild_model_resolver

    def flaky_rebuild(
        models: Sequence[LlmModelEntry],
        role_bindings: Mapping[str, str] | None = None,
    ) -> object:
        if not calls:
            calls.append(1)
            raise RuntimeError("forced resolver rebuild failure")
        return real_rebuild(models, role_bindings)

    monkeypatch.setattr(config_loader, "rebuild_model_resolver", flaky_rebuild)

    response = await client.post(
        "/api/v1/models/entries", json=_create_payload(name="rollback-entry")
    )
    assert response.status_code == 422
    body = response.json()
    assert body["success"] is False
    assert body["code"] == "CONFIG_INVALID"
    assert body["data"]["rollback"] == "in-memory"
    assert "回滚" in body["message"]

    # 内存回滚：写前快照恢复（新条目不在运行时 / 解析器）。
    names = [entry.name for entry in config_loader.runtime_llm_config.models]
    assert names == ["main-chat", "local-embed"]
    assert get_model_resolver().get("rollback-entry") is None
    # 磁盘语义（文档化决策）：保留新内容 + .bak 写前备份。
    on_disk = _read_yaml(config_dir / "config.yaml")
    assert "rollback-entry" in [m["name"] for m in on_disk["llm"]["models"]]
    assert len(list(config_dir.glob("config.yaml.bak-*"))) >= 1


async def test_backup_rotation_keeps_last_ten(
    client: AsyncClient, config_dir: Path
) -> None:
    """备份轮换：超过 REGISTRY_BACKUP_KEEP 次持久化后仅保留最新 10 份。"""
    for index in range(REGISTRY_BACKUP_KEEP + 2):
        response = await client.post(
            "/api/v1/models/entries", json=_create_payload(name=f"rot-{index}")
        )
        assert response.status_code == 200, index
    backups = list(config_dir.glob("config.yaml.bak-*"))
    assert len(backups) == REGISTRY_BACKUP_KEEP


async def test_unparseable_existing_config_rejected(
    client: AsyncClient, config_dir: Path
) -> None:
    """既有 config.yaml 不可解析 / 顶层非 mapping：拒绝覆写（422）+ 原文件不变。"""
    yaml_path = config_dir / "config.yaml"
    for garbage in (b"{ invalid flow", b"- a\n- b\n"):
        yaml_path.write_bytes(garbage)
        response = await client.post(
            "/api/v1/models/entries", json=_create_payload(name="clobber-attempt")
        )
        assert response.status_code == 422, garbage
        body = response.json()
        assert body["code"] == "CONFIG_INVALID"
        assert "拒绝覆写" in body["message"]
        assert yaml_path.read_bytes() == garbage


async def test_create_on_fresh_path_without_prior_file(tmp_path: Path) -> None:
    """直调服务面：显式 yaml_path 指向不存在文件——首次创建无写前备份。"""
    config_loader.load_all_config(config_dir=tmp_path)  # 运行时隔离装载（无文件）
    fresh_dir = tmp_path / "fresh"
    fresh_dir.mkdir()
    fresh_path = fresh_dir / "config.yaml"

    service = RegistryAdminService()
    state = await service.create_entry(
        ModelEntryCreateDTO(
            name="fresh-entry",
            base_url="http://127.0.0.1:9300/v1",
            model="fresh-model",
        ),
        yaml_path=fresh_path,
    )
    assert [entry.name for entry in state.entries] == ["fresh-entry"]
    assert fresh_path.is_file()
    assert list(fresh_dir.glob("config.yaml.bak-*")) == []
    assert list(fresh_dir.glob("config.yaml.tmp-*")) == []
    # 热重建经显式路径生效（追踪路径切换到 fresh 目录）。
    assert get_loaded_config_yaml_path() == fresh_path
    assert get_model_resolver().resolve_optional("look_at") is None


async def test_loaded_config_yaml_path_fallback_and_reset() -> None:
    """路径追踪访问器：未加载回落 CWD 默认；加载后指向实际文件；复位清空。"""
    reset_config_runtime_state()
    assert get_loaded_config_yaml_path() == Path("config.yaml")
    scratch_dir = Path(__file__).parent / "does-not-exist-config"
    config_loader.load_all_config(config_dir=scratch_dir)
    assert get_loaded_config_yaml_path() == scratch_dir / "config.yaml"
    reset_config_runtime_state()
    assert get_loaded_config_yaml_path() == Path("config.yaml")


# ---------------------------------------------------------------------------
# 处理器直调契约面——handler 函数作为普通 async 可调用直接调用，逐端点
# 锁定 ResultDTO 包装契约（success / code / message / data 形态）。
# ---------------------------------------------------------------------------


async def test_direct_handlers_result_contract(config_dir: Path) -> None:
    """直调面：list / roles / create / update / delete 的 ResultDTO 契约。"""
    listed = await list_entries()
    assert listed.success is True
    assert listed.code == "200"
    assert listed.data is not None
    assert [entry.name for entry in listed.data.entries] == ["main-chat", "local-embed"]
    assert listed.data.role_bindings == {"dream": "main-chat"}

    roles = await list_roles()
    assert roles.success is True
    assert roles.data is not None
    assert len(roles.data) == 5

    created = await create_entry(
        ModelEntryCreateDTO(
            name="direct-entry",
            base_url="http://127.0.0.1:9400/v1",
            model="direct-model",
            roles=["look_at"],
        )
    )
    assert created.success is True
    assert created.message == "模型条目已创建"
    assert any(e.name == "direct-entry" for e in created.data.entries)

    updated = await update_entry(
        "direct-entry",
        ModelEntryUpdateDTO.model_validate({"model": "direct-model-v2"}),
    )
    assert updated.success is True
    assert updated.message == "模型条目已更新"
    target = next(e for e in updated.data.entries if e.name == "direct-entry")
    assert target.model == "direct-model-v2"

    unbound = await update_entry("direct-entry", ModelEntryUpdateDTO.model_validate({"roles": []}))
    assert unbound.success is True

    deleted = await delete_entry("direct-entry")
    assert deleted.success is True
    assert deleted.message == "模型条目已删除"
    assert [entry.name for entry in deleted.data.entries] == ["main-chat", "local-embed"]


async def test_registry_admin_service_write_lock_serializes(config_dir: Path) -> None:
    """服务面复用：模块级服务实例经路由共享（写锁串行化由内部保证）。"""
    assert registry_admin_service is not None
    state = await registry_admin_service.state()
    assert [entry.name for entry in state.entries] == ["main-chat", "local-embed"]

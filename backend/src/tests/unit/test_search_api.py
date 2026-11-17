"""app/api/search 路由测试——provider CRUD 全端点 + usage-summary（ASGITransport）。

测试策略（镜像 test_models_api）：tmp 配置目录（config.yaml 种子 +
``load_search_config`` 隔离加载——写路径追踪就绪）；本地装配 FastAPI
app（router + 共享异常处理器）；密钥防泄漏断言经 monkeypatch 注入真实
存在的密钥环境变量值（响应面永不出现该值）；usage-summary 经真实
UsageService 落库（共享 SQLite fixture）后聚合断言。

覆盖面：脱敏读取 + 链位次 / 创建→落盘→热重建 round-trip / 部分更新 /
删除守卫（启用 409 + 关闭放行）/ 唯一名 409 / 非法 kind·未知字段·非法
base_url 400 / 404 / usage-summary（零记录全零 + 落库聚合 + 预算上限
透出）/ 处理器直调契约面。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import yaml
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from app.api.search import (
    list_providers,
    search_registry_admin_service,
)
from app.api.search import router as search_router
from app.middleware.error_handler import register_exception_handlers
from packages.harness.config.search_registry import load_search_config
from packages.harness.core.dtos import ResultDTO
from packages.harness.database.engine import db_engine_holder
from packages.harness.usage.dtos import UsageCreateDTO
from packages.harness.usage.models import UsagePurpose, UsageStatus
from packages.harness.usage.service import UsageService

#: 密钥环境变量真实值（防泄漏断言真源——响应面出现即失败）。
_SECRET_VALUE = "sk-search-secret-do-not-leak-1a"

#: 种子 config.yaml（单 provider + 关闭形态——删除守卫矩阵双形态驱动）。
_SEED_YAML = """
future_section:
  keep_me: true

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
  web_search:
    enabled: true
    tool_enabled: true
"""


@pytest.fixture
async def config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Path]:
    """tmp 配置目录（种子 config.yaml + 隔离加载 + 密钥环境变量注入）。

    Args:
        tmp_path: pytest 一次性目录。
        monkeypatch: 测试内 monkeypatch（环境变量注入）。

    Yields:
        已完成 ``load_search_config`` 的配置目录（写路径追踪就绪；复位由
        conftest autouse fixture 承担）。
    """
    directory = tmp_path / "search-config"
    directory.mkdir()
    (directory / "config.yaml").write_text(_SEED_YAML, encoding="utf-8")
    monkeypatch.setenv("SEARCH_BOCHA_API_KEY", _SECRET_VALUE)
    load_search_config(directory / "config.yaml")
    yield directory


@pytest.fixture
async def search_app(
    config_dir: Path, shared_db_engine: AsyncEngine
) -> AsyncIterator[FastAPI]:
    """本地装配的最小 FastAPI app（本路由 + 异常处理器；usage-summary
    经 engine holder 挂共享 SQLite——UsageService 自持 session_scope）。

    Args:
        config_dir: 已隔离加载的配置目录。
        shared_db_engine: 共享 SQLite 引擎（usage-summary 落库断言面）。

    Yields:
        已注册路由与异常处理器的 :class:`FastAPI` 实例（holder 用后复位）。
    """
    db_engine_holder.async_engine = shared_db_engine
    db_engine_holder.session_factory = async_sessionmaker(
        bind=shared_db_engine, expire_on_commit=False
    )
    db_engine_holder.owner_token = None
    app = FastAPI(docs_url=None, redoc_url=None)
    app.include_router(search_router)
    register_exception_handlers(app)
    try:
        yield app
    finally:
        db_engine_holder.reset()


@pytest.fixture
async def client(search_app: FastAPI) -> AsyncIterator[AsyncClient]:
    """ASGITransport 客户端（零真实网络 / 零真实服务器）。

    Args:
        search_app: 本地装配的 FastAPI app。

    Yields:
        :class:`httpx.AsyncClient`（base_url=http://test）。
    """
    transport = ASGITransport(app=search_app)
    async with AsyncClient(transport=transport, base_url="http://test") as async_client:
        yield async_client


def _read_search_section(directory: Path) -> dict:
    """读取落盘 yaml 的 search 段。"""
    data = yaml.safe_load((directory / "config.yaml").read_text(encoding="utf-8"))
    return data["search"]


class TestListProviders:
    async def test_masked_state_with_chain_positions(self, client: AsyncClient) -> None:
        """脱敏读取：条目 + 链位次 + 运营子块；密钥值永不出现。"""
        response = await client.get("/api/v1/search/providers")
        assert response.status_code == 200
        payload = response.json()
        assert payload["success"] is True
        state = payload["data"]
        assert [entry["name"] for entry in state["providers"]] == [
            "bocha-main",
            "zhipu-fallback",
        ]
        assert state["providers"][0]["chain_position"] == 1
        assert state["providers"][1]["chain_position"] == 2
        assert state["web_search"]["enabled"] is True
        assert _SECRET_VALUE not in response.text

    async def test_handler_direct_call_contract(self, config_dir: Path) -> None:
        """处理器直调契约面（绕过 HTTP 层——ResultDTO 形态）。"""
        result = await list_providers()
        assert isinstance(result, ResultDTO)
        assert result.success is True
        assert [entry.name for entry in result.data.providers] == [
            "bocha-main",
            "zhipu-fallback",
        ]


class TestCreateProvider:
    async def test_create_roundtrip(self, client: AsyncClient, config_dir: Path) -> None:
        """创建：200 + 写后状态 + 落盘 + 未知顶层键保留。"""
        body = {"name": "zhipu-2", "kind": "zhipu"}
        response = await client.post("/api/v1/search/providers", json=body)
        assert response.status_code == 200
        payload = response.json()
        assert payload["message"] == "搜索 provider 已创建"
        assert [e["name"] for e in payload["data"]["providers"]] == [
            "bocha-main",
            "zhipu-fallback",
            "zhipu-2",
        ]
        on_disk = _read_search_section(config_dir)
        assert on_disk["providers"][-1]["api_key_env"] == "SEARCH_ZHIPU_API_KEY"
        data = yaml.safe_load((config_dir / "config.yaml").read_text(encoding="utf-8"))
        assert data["future_section"] == {"keep_me": True}

    async def test_create_duplicate_conflict(self, client: AsyncClient) -> None:
        """唯一名：409（错误处理器映射 CONFLICT）。"""
        response = await client.post(
            "/api/v1/search/providers", json={"name": "bocha-main", "kind": "bocha"}
        )
        assert response.status_code == 409
        assert response.json()["code"] == "CONFLICT"

    async def test_create_unknown_kind_rejected(self, client: AsyncClient) -> None:
        """未知 kind：422（DTO 值域校验）。"""
        response = await client.post(
            "/api/v1/search/providers", json={"name": "x", "kind": "bing"}
        )
        assert response.status_code == 400

    async def test_create_invalid_base_url_rejected(self, client: AsyncClient) -> None:
        """非法 base_url（非 http(s)）：422。"""
        response = await client.post(
            "/api/v1/search/providers",
            json={"name": "x", "kind": "bocha", "base_url": "ftp://bad.test"},
        )
        assert response.status_code == 400

    async def test_create_unknown_field_rejected(self, client: AsyncClient) -> None:
        """未知字段（含密钥明文字段）：422（extra=forbid——密钥不变式）。"""
        response = await client.post(
            "/api/v1/search/providers",
            json={"name": "x", "kind": "bocha", "api_key": "sk-leak"},
        )
        assert response.status_code == 400


class TestUpdateProvider:
    async def test_update_partial(self, client: AsyncClient, config_dir: Path) -> None:
        """部分更新：仅出现字段生效 + 落盘。"""
        response = await client.put(
            "/api/v1/search/providers/bocha-main",
            json={"max_results": 20},
        )
        assert response.status_code == 200
        assert response.json()["data"]["providers"][0]["max_results"] == 20
        assert _read_search_section(config_dir)["providers"][0]["max_results"] == 20

    async def test_update_name_field_rejected(self, client: AsyncClient) -> None:
        """name 不可变：请求体携带 name → 422（extra=forbid）。"""
        response = await client.put(
            "/api/v1/search/providers/bocha-main",
            json={"name": "renamed", "max_results": 1},
        )
        assert response.status_code == 400

    async def test_update_unknown_not_found(self, client: AsyncClient) -> None:
        """未知条目：404。"""
        response = await client.put(
            "/api/v1/search/providers/ghost", json={"max_results": 1}
        )
        assert response.status_code == 404
        assert response.json()["code"] == "NOT_FOUND"


class TestDeleteProvider:
    async def test_delete_guard_enabled_conflict(self, client: AsyncClient) -> None:
        """唯一 provider 守卫：能力启用 + 删最后一条 → 409 + fix 指引。

        铺底：先把两条目删到只剩一条（能力开启下合法——非最后一条），
        再删最后一条触发守卫。
        """
        response = await client.delete("/api/v1/search/providers/zhipu-fallback")
        assert response.status_code == 200
        response = await client.delete("/api/v1/search/providers/bocha-main")
        assert response.status_code == 409
        payload = response.json()
        assert payload["code"] == "CONFLICT"
        assert "fix" in payload["data"]

    async def test_delete_guard_disabled_allows_empty(
        self, client: AsyncClient, config_dir: Path
    ) -> None:
        """守卫解除路径：能力关闭（yaml 改写 + 重载）后删最后一条放行。"""
        section = _read_search_section(config_dir)
        section["web_search"]["enabled"] = False
        data = yaml.safe_load((config_dir / "config.yaml").read_text(encoding="utf-8"))
        data["search"] = section
        (config_dir / "config.yaml").write_text(
            yaml.safe_dump(data, allow_unicode=True), encoding="utf-8"
        )
        load_search_config(config_dir / "config.yaml")

        response = await client.delete("/api/v1/search/providers/zhipu-fallback")
        assert response.status_code == 200
        response = await client.delete("/api/v1/search/providers/bocha-main")
        assert response.status_code == 200
        assert response.json()["data"]["providers"] == []

    async def test_delete_unknown_not_found(self, client: AsyncClient) -> None:
        """未知条目删除：404。"""
        response = await client.delete("/api/v1/search/providers/ghost")
        assert response.status_code == 404


class TestUsageSummary:
    async def test_empty_summary_zeroes(self, client: AsyncClient) -> None:
        """零记录：全零 + 空明细 + 预算上限透出（运营子块生效值）。"""
        response = await client.get("/api/v1/search/usage-summary")
        assert response.status_code == 200
        summary = response.json()["data"]
        assert summary["global_used"] == 0
        assert summary["global_limit"] == 200
        assert summary["persona_usage"] == []
        assert summary["day_start_ms"] % 86_400_000 == 0

    async def test_summary_aggregates_today_rows(self, client: AsyncClient) -> None:
        """落库聚合：今日 search 行（成功 + 失败 + 双人格）→ 全局计数 +
        per-persona 明细降序。"""
        from packages.harness.core.time_utils import now_ms

        service = UsageService()
        day_start = (now_ms() // 86_400_000) * 86_400_000
        rows = [
            UsageCreateDTO(
                ts_ms=day_start + 1000,
                persona_id="7",
                model_entry="bocha-main",
                provider="bocha",
                purpose=UsagePurpose.SEARCH,
                latency_ms=10,
                status=UsageStatus.SUCCESS,
            ),
            UsageCreateDTO(
                ts_ms=day_start + 2000,
                persona_id="7",
                model_entry="bocha-main",
                provider="bocha",
                purpose=UsagePurpose.SEARCH,
                latency_ms=5,
                status=UsageStatus.ERROR,
                error_code="LLM_CALL_FAILED",
            ),
            UsageCreateDTO(
                ts_ms=day_start + 3000,
                persona_id="9",
                model_entry="zhipu-fallback",
                provider="zhipu",
                purpose=UsagePurpose.SEARCH,
                latency_ms=7,
                status=UsageStatus.SUCCESS,
            ),
            UsageCreateDTO(  # 昨日行——不计入今日口径
                ts_ms=day_start - 1000,
                persona_id="7",
                model_entry="bocha-main",
                provider="bocha",
                purpose=UsagePurpose.SEARCH,
                latency_ms=1,
                status=UsageStatus.SUCCESS,
            ),
            UsageCreateDTO(  # 他用途行——不计入 search 口径
                ts_ms=day_start + 4000,
                persona_id="7",
                model_entry="chat-entry",
                provider="openai_compatible",
                purpose=UsagePurpose.CHAT,
                latency_ms=1,
                status=UsageStatus.SUCCESS,
            ),
        ]
        await service.create_batch(rows)

        response = await client.get("/api/v1/search/usage-summary")
        assert response.status_code == 200
        summary = response.json()["data"]
        assert summary["global_used"] == 3
        persona_rows = summary["persona_usage"]
        assert [(row["persona_id"], row["used"]) for row in persona_rows] == [
            ("7", 2),
            ("9", 1),
        ]
        assert all(row["limit"] == 50 for row in persona_rows)


class TestSharedServiceInstance:
    async def test_module_service_writable_via_state(self, config_dir: Path) -> None:
        """模块级服务实例：处理器直调链路（写路径走同一锁——共享安全）。"""
        from app.api.search import SearchProviderCreateDTO

        state = await search_registry_admin_service.create_provider(
            SearchProviderCreateDTO(name="direct-call", kind="bocha")
        )
        assert "direct-call" in [entry.name for entry in state.providers]
        assert "direct-call" in [
            entry["name"] for entry in _read_search_section(config_dir)["providers"]
        ]

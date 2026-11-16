"""harness/tools B 站只读沙盒工具测试——规格镜像 + 代理注册回路。

覆盖矩阵：四工具规格呈现面（名称 / 描述 / args_model / JSON Schema
形态——enum / pattern / 边界）；参数模型校验矩阵与 JSON Schema 锚点
（**规格镜像的行为锚点**——与沙盒树 test_bili_tools.py 的同组断言对齐，
两树行为等价即镜像面未漂移；文本面看护属根漂移门禁扩展——主 agent 面）；
``register_sandbox_tools_for_bili`` 代理回路（fake 客户端 ok → registry
ok=True；ok=False → INTERNAL_ERROR 归一；本地校验 fail-fast——客户端
零调用；默认值物化经 wire）。
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from packages.harness.tools.registry import ToolRegistry
from packages.harness.tools.sandbox import register_sandbox_tools_for_bili
from packages.harness.tools.sandbox_models import (
    BILI_SEARCH_VIDEOS_TOOL_NAME,
    BILI_TOOL_NAMES,
    BILI_VIDEO_COMMENTS_TOOL_NAME,
    BILI_VIDEO_INFO_TOOL_NAME,
    BILI_VIDEO_SUBTITLE_TOOL_NAME,
    BiliSearchVideosArgsModel,
    BiliSubtitleArgsModel,
    BiliVideoCommentsArgsModel,
    BiliVideoInfoArgsModel,
    build_bili_search_videos_spec,
    build_bili_video_comments_spec,
    build_bili_video_info_spec,
    build_bili_video_subtitle_spec,
)
from packages.harness.tools.tool_models import ToolResultModel


class FakeSandboxClient:
    """fake 沙盒客户端（记录调用；可配置成败终态——SandboxClientFace 满足）。"""

    def __init__(self, *, fail: bool = False, error: str = "Bilibili 风险控制硬冷却中") -> None:
        """初始化（fail=True 时 invoke 返回 ok=False 终态）。"""
        self.fail = fail
        self.error = error
        self.invocations: list[tuple[str, dict[str, Any]]] = []
        self.has_token = True

    async def invoke(self, tool: str, arguments: dict[str, Any]) -> ToolResultModel:
        """记录调用并返回配置的终态。"""
        self.invocations.append((tool, dict(arguments)))
        if self.fail:
            return ToolResultModel(tool=tool, ok=False, error=self.error)
        return ToolResultModel(tool=tool, ok=True, result={"videos": [], "page": 1})


def _registered_registry(client: FakeSandboxClient) -> ToolRegistry:
    """构造已注册全部 B 站沙盒代理工具的注册表。"""
    registry = ToolRegistry()
    register_sandbox_tools_for_bili(registry, client)
    return registry


class TestRegisterSandboxToolsForBili:
    async def test_registers_four_tools_in_order(self) -> None:
        """注册面：四工具齐备（注册序 = BILI_TOOL_NAMES）。"""
        client = FakeSandboxClient()
        registry = _registered_registry(client)
        assert registry.tool_names == list(BILI_TOOL_NAMES)
        assert list(BILI_TOOL_NAMES) == [
            BILI_SEARCH_VIDEOS_TOOL_NAME,
            BILI_VIDEO_INFO_TOOL_NAME,
            BILI_VIDEO_COMMENTS_TOOL_NAME,
            BILI_VIDEO_SUBTITLE_TOOL_NAME,
        ]

    async def test_proxy_round_trip_success(self) -> None:
        """代理回路（成功）：registry.execute → fake invoke → ok=True 透传。"""
        client = FakeSandboxClient()
        registry = _registered_registry(client)
        result = await registry.execute(
            BILI_VIDEO_INFO_TOOL_NAME, {"bvid": "BV1xx411c7mD"}
        )
        assert result.ok is True
        assert result.tool == BILI_VIDEO_INFO_TOOL_NAME
        assert result.result == {"videos": [], "page": 1}
        assert client.invocations == [
            (BILI_VIDEO_INFO_TOOL_NAME, {"bvid": "BV1xx411c7mD"})
        ]

    async def test_defaults_materialized_through_wire(self) -> None:
        """默认值物化：缺省 page / page_size / fetch_subtitle 经本地校验进 wire。"""
        client = FakeSandboxClient()
        registry = _registered_registry(client)
        await registry.execute(
            BILI_SEARCH_VIDEOS_TOOL_NAME, {"keyword": "测试"}
        )
        await registry.execute(
            BILI_VIDEO_COMMENTS_TOOL_NAME, {"bvid": "BV1xx411c7mD"}
        )
        await registry.execute(
            BILI_VIDEO_SUBTITLE_TOOL_NAME, {"bvid": "BV1xx411c7mD"}
        )
        assert client.invocations == [
            (BILI_SEARCH_VIDEOS_TOOL_NAME, {"keyword": "测试", "page": 1, "page_size": 20}),
            (BILI_VIDEO_COMMENTS_TOOL_NAME, {"bvid": "BV1xx411c7mD", "page": 1, "page_size": 20}),
            (BILI_VIDEO_SUBTITLE_TOOL_NAME, {"bvid": "BV1xx411c7mD", "fetch_subtitle": False}),
        ]

    async def test_proxy_failure_maps_to_business_error(self) -> None:
        """代理回路（失败）：fake ok=False → INTERNAL_ERROR 归一（registry 隔离）。"""
        client = FakeSandboxClient(
            fail=True, error="Bilibili 风险控制拦截（HTTP 412）——进入 600 秒硬冷却"
        )
        registry = _registered_registry(client)
        result = await registry.execute(
            BILI_VIDEO_INFO_TOOL_NAME, {"bvid": "BV1xx411c7mD"}
        )
        assert result.ok is False
        assert result.result == {}
        assert "INTERNAL_ERROR" in (result.error or "")
        assert "沙盒工具 bili_video_info 执行失败" in (result.error or "")
        assert "412" in (result.error or "")

    async def test_local_validation_fail_fast(self) -> None:
        """本地校验 fail-fast：非法 BV 号在 registry 层拒绝——客户端零调用。"""
        client = FakeSandboxClient()
        registry = _registered_registry(client)
        result = await registry.execute(
            BILI_VIDEO_INFO_TOOL_NAME, {"bvid": "AV170001"}
        )
        assert result.ok is False
        assert "参数校验失败" in (result.error or "")
        assert client.invocations == []

    async def test_local_bounds_enforced(self) -> None:
        """本地校验：评论 page_size 越界（21）在 registry 层拒绝。"""
        client = FakeSandboxClient()
        registry = _registered_registry(client)
        result = await registry.execute(
            BILI_VIDEO_COMMENTS_TOOL_NAME,
            {"bvid": "BV1xx411c7mD", "page_size": 21},
        )
        assert result.ok is False
        assert client.invocations == []


class TestBiliArgsModels:
    async def test_search_defaults_and_bounds(self) -> None:
        """搜索参数：默认 page=1 / page_size=20；keyword / page / page_size 越界拒绝。"""
        args = BiliSearchVideosArgsModel.model_validate({"keyword": "测试"})
        assert (args.page, args.page_size) == (1, 20)
        with pytest.raises(ValidationError):
            BiliSearchVideosArgsModel.model_validate({"keyword": ""})
        with pytest.raises(ValidationError):
            BiliSearchVideosArgsModel.model_validate({"keyword": "x" * 65})
        with pytest.raises(ValidationError):
            BiliSearchVideosArgsModel.model_validate({"keyword": "x", "page": 6})
        with pytest.raises(ValidationError):
            BiliSearchVideosArgsModel.model_validate({"keyword": "x", "page_size": 15})
        with pytest.raises(ValidationError):
            BiliSearchVideosArgsModel.model_validate({"keyword": "x", "unexpected": 1})

    async def test_bvid_pattern_matrix(self) -> None:
        """BV 号形态：合法 12 位通过；小写前缀 / 长度 / 非法字符 / av 号拒绝。"""
        assert BiliVideoInfoArgsModel.model_validate({"bvid": "BV1xx411c7mD"}).bvid == "BV1xx411c7mD"
        with pytest.raises(ValidationError):
            BiliVideoInfoArgsModel.model_validate({"bvid": "bv1xx411c7mD"})
        with pytest.raises(ValidationError):
            BiliVideoInfoArgsModel.model_validate({"bvid": "BV1xx411c7m"})
        with pytest.raises(ValidationError):
            BiliVideoInfoArgsModel.model_validate({"bvid": "BV1xx411c7m!"})
        with pytest.raises(ValidationError):
            BiliVideoInfoArgsModel.model_validate({"bvid": "AV170001"})

    async def test_comments_bounds(self) -> None:
        """评论参数：1-50 / 10-20 边界（50 / 10 合法；51 / 9 / 21 拒绝）。"""
        assert (
            BiliVideoCommentsArgsModel.model_validate(
                {"bvid": "BV1xx411c7mD", "page": 50, "page_size": 10}
            ).page
            == 50
        )
        with pytest.raises(ValidationError):
            BiliVideoCommentsArgsModel.model_validate({"bvid": "BV1xx411c7mD", "page": 51})
        with pytest.raises(ValidationError):
            BiliVideoCommentsArgsModel.model_validate({"bvid": "BV1xx411c7mD", "page_size": 9})
        with pytest.raises(ValidationError):
            BiliVideoCommentsArgsModel.model_validate({"bvid": "BV1xx411c7mD", "page_size": 21})
        with pytest.raises(ValidationError):
            BiliVideoCommentsArgsModel.model_validate({"bvid": "BV1xx411c7mD", "post": "x"})

    async def test_subtitle_defaults(self) -> None:
        """字幕参数：fetch_subtitle 默认 False；未知参数拒绝。"""
        args = BiliSubtitleArgsModel.model_validate({"bvid": "BV1xx411c7mD"})
        assert args.fetch_subtitle is False
        with pytest.raises(ValidationError):
            BiliSubtitleArgsModel.model_validate({"bvid": "BV1xx411c7mD", "lines": True})

    async def test_json_schema_anchors(self) -> None:
        """JSON Schema 锚点（镜像面行为锚——字段集 + enum + pattern + 边界）。"""
        search = BiliSearchVideosArgsModel.model_json_schema()
        assert set(search["properties"]) == {"keyword", "page", "page_size"}
        assert search["properties"]["page_size"]["enum"] == [10, 20, 30]
        assert search["properties"]["keyword"]["maxLength"] == 64
        assert search["properties"]["page"]["maximum"] == 5
        comments = BiliVideoCommentsArgsModel.model_json_schema()
        assert set(comments["properties"]) == {"bvid", "page", "page_size"}
        assert comments["properties"]["page"]["maximum"] == 50
        assert comments["properties"]["page_size"]["maximum"] == 20
        subtitle = BiliSubtitleArgsModel.model_json_schema()
        assert set(subtitle["properties"]) == {"bvid", "fetch_subtitle"}
        info = BiliVideoInfoArgsModel.model_json_schema()
        assert set(info["properties"]) == {"bvid"}
        for model in (info, comments, subtitle):
            assert model["properties"]["bvid"]["pattern"] == r"^BV[0-9A-Za-z]{10}$"


class TestBiliSpecShapes:
    async def test_spec_names_and_models(self) -> None:
        """规格形态：四工厂名称 + args_model 锚点 + 描述非空。"""
        for spec, name, model in (
            (build_bili_search_videos_spec(), BILI_SEARCH_VIDEOS_TOOL_NAME, BiliSearchVideosArgsModel),
            (build_bili_video_info_spec(), BILI_VIDEO_INFO_TOOL_NAME, BiliVideoInfoArgsModel),
            (build_bili_video_comments_spec(), BILI_VIDEO_COMMENTS_TOOL_NAME, BiliVideoCommentsArgsModel),
            (build_bili_video_subtitle_spec(), BILI_VIDEO_SUBTITLE_TOOL_NAME, BiliSubtitleArgsModel),
        ):
            assert spec.name == name
            assert spec.args_model is model
            assert spec.description

    async def test_specs_listed_for_llm(self) -> None:
        """LLM 呈现面：list_specs 携带四工具定义（required / enum / pattern）。"""
        client = FakeSandboxClient()
        registry = _registered_registry(client)
        definitions = registry.list_specs()
        by_name = {definition.name: definition for definition in definitions}
        search = by_name[BILI_SEARCH_VIDEOS_TOOL_NAME].parameters.model_dump(exclude_none=True)
        assert search["required"] == ["keyword"]
        assert search["properties"]["page_size"]["enum"] == [10, 20, 30]
        assert search["additionalProperties"] is False
        info = by_name[BILI_VIDEO_INFO_TOOL_NAME].parameters.model_dump(exclude_none=True)
        assert info["required"] == ["bvid"]
        assert info["properties"]["bvid"]["pattern"] == r"^BV[0-9A-Za-z]{10}$"
        comments = by_name[BILI_VIDEO_COMMENTS_TOOL_NAME].parameters.model_dump(exclude_none=True)
        assert comments["properties"]["page"]["maximum"] == 50
        subtitle = by_name[BILI_VIDEO_SUBTITLE_TOOL_NAME].parameters.model_dump(exclude_none=True)
        assert subtitle["properties"]["fetch_subtitle"]["type"] == "boolean"

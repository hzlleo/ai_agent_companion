"""Bilibili 只读工具测试——WBI 签名 / 限速 / 硬冷却 / 白名单裁剪（零网络）。

覆盖矩阵：WBI 签名确定性（固定 nav 键 + 参数——期望值经测试内独立
重排算法推导）；nav 键提取与 TTL 缓存；参数模型校验矩阵（四工具）；
允许清单默认 / 显式禁用 / 清单不中；默认抓取（不走系统代理 + cookie
环境间接 + 412 状态保留）；nav 默认实现（412 信号 / 非 200 / 非法
JSON）；最小间隔限速（注入时钟 / sleep）；412 硬冷却递增（600→1200→
1800 封顶）+ 过期恢复 + 成功归零；code -352 不重试（零冷却）；code!=0
映射；白名单裁剪（搜索 / 详情 / 评论 / 字幕）；16KB 结果上限；字幕
空容忍 + CDN 后缀清单；执行器全链（默认值物化 + ok=False 归一）。

hermetic 红线：零真实网络——全部出站经注入 fake（fetch / nav_fetch /
时钟 / sleep）；默认抓取面经 monkeypatch opener / ProxyHandler 捕获。
"""

from __future__ import annotations

import hashlib
import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

import pytest
from pydantic import ValidationError

import tool_sandbox.bili_tools as bili_module
from tool_sandbox.bili_tools import (
    BILI_SEARCH_VIDEOS_TOOL_NAME,
    BILI_VIDEO_COMMENTS_TOOL_NAME,
    BILI_VIDEO_INFO_TOOL_NAME,
    BILI_VIDEO_SUBTITLE_TOOL_NAME,
    BiliCommentModel,
    BiliFetchResponse,
    BiliHTTP412Error,
    BiliSearchVideosArgsModel,
    BiliSubtitleArgsModel,
    BiliVideoCommentsArgsModel,
    BiliVideoInfoArgsModel,
    build_wbi_query,
    default_bili_fetch,
    default_nav_fetch,
    derive_mixin_key,
    extract_wbi_keys,
    register_bili_tools,
    wbi_sign,
)
from tool_sandbox.runtime import SandboxToolError, SandboxToolRuntime
from tool_sandbox.wire_models import InvokeRequestModel

# ---------------------------------------------------------------------------
# 测试基线（固定 nav 键 / 载荷 / 期望推导——独立于被测模块的参考算法）
# ---------------------------------------------------------------------------

#: 固定 nav 键（文档化示例形态——img/sub 各 32 位）。
_FIXED_IMG_KEY = "7cd084941338484aae1ad8427daa9b5a"
_FIXED_SUB_KEY = "4932caff0ff746eab6f01bf08b70ac45"

#: 固定 nav 响应（键经 URL 文件名承载——提取面走 basename 去扩展名）。
_FIXED_NAV_JSON: dict[str, Any] = {
    "code": 0,
    "data": {
        "wbi_img": {
            "img_url": f"https://i0.hdslb.com/bfs/wbi/{_FIXED_IMG_KEY}.png",
            "img_sub_url": f"https://i1.hdslb.com/bfs/wbi/{_FIXED_SUB_KEY}.png",
        }
    },
}

#: 参考打乱表（测试内独立副本——模块表抄写错误即红）。
_REFERENCE_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40,
    61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11,
    36, 20, 34, 44, 52,
]


def _reference_mixin_key(img_key: str, sub_key: str) -> str:
    """参考 mixin key 推导（测试内独立实现——与被测函数对照）。"""
    raw = img_key + sub_key
    return "".join(raw[index] for index in _REFERENCE_ENC_TAB)[:32]


def _reference_wbi_query(params: dict[str, Any], wts: int, mixin_key: str) -> str:
    """参考 WBI 查询串推导（值过滤 + wts + 排序 urlencode + md5——独立实现）。"""
    filtered = {
        str(key): "".join(ch for ch in str(value) if ch not in "!'()*")
        for key, value in params.items()
    }
    filtered["wts"] = str(wts)
    query = urllib.parse.urlencode(dict(sorted(filtered.items())))
    signature = hashlib.md5(f"{query}{mixin_key}".encode()).hexdigest()
    return f"{query}&w_rfd={signature}"


def _json_response(payload: dict[str, Any]) -> BiliFetchResponse:
    """构造 200 + JSON body 的 fake 响应。"""
    return BiliFetchResponse(status_code=200, body=json.dumps(payload).encode("utf-8"))


#: 搜索响应（含高亮标签 / 非白名单字段 / 隐藏播放量变体）。
_SEARCH_PAYLOAD: dict[str, Any] = {
    "code": 0,
    "data": {
        "numResults": 2,
        "result": [
            {
                "bvid": "BV1xx411c7mD",
                "title": "测试<em class=\"keyword\">视频</em>",
                "author": "up主甲",
                "duration": "10:30",
                "play": 12345,
                "pubdate": 1700000000,
                "extra_field": "dropped",
            },
            {
                "bvid": "BV2yy411c7mE",
                "title": "隐藏播放量",
                "author": "up主乙",
                "duration": "1:02:03",
                "play": "—",
                "extra_field": "dropped",
            },
        ],
    },
}

#: 视频详情响应（含大量非白名单字段）。
_VIEW_PAYLOAD: dict[str, Any] = {
    "code": 0,
    "data": {
        "bvid": "BV1xx411c7mD",
        "aid": 170001,
        "title": "视频标题",
        "desc": "视频简介",
        "owner": {"mid": 42, "name": "up主", "face": "https://example.com/face.png"},
        "duration": 754,
        "pubdate": 1700000000,
        "stat": {
            "aid": 170001,
            "view": 100,
            "danmaku": 5,
            "reply": 6,
            "favorite": 7,
            "coin": 8,
            "share": 9,
            "like": 10,
            "his_rank": 0,
        },
        "pages": [{"cid": 111, "page": 1, "part": "P1", "duration": 754}],
        "dynamic": "dropped",
    },
}

#: 评论响应（嵌套 member/content + 非白名单字段 + 超长正文变体）。
_REPLY_PAYLOAD: dict[str, Any] = {
    "code": 0,
    "data": {
        "cursor": {"next": 3, "all_count": 100},
        "replies": [
            {
                "rpid": 111,
                "member": {"uname": "评论者甲", "level": 6},
                "content": {"message": "好视频", "plat": 1},
                "like": 5,
                "ctime": 1700000100,
                "extra": "dropped",
            },
            {
                "rpid": 112,
                "member": {"uname": "评论者乙"},
                "content": {"message": "长" * 1200},
                "like": 0,
                "ctime": 1700000200,
            },
        ],
    },
}

#: 播放器字幕清单响应（协议相对 URL 变体）。
_PLAYER_PAYLOAD: dict[str, Any] = {
    "code": 0,
    "data": {
        "subtitle": {
            "subtitles": [
                {
                    "lan": "ai-zh",
                    "lan_doc": "中文（自动生成）",
                    "subtitle_url": "//aisubtitle.biliapi.net/bfs/subtitle.json?x=1",
                },
                {
                    "lan": "en",
                    "lan_doc": "英文",
                    "subtitle_url": "https://api.bilivideo.com/subtitle.json",
                },
            ]
        }
    },
}

#: 字幕文件 JSON（正文行 + 非白名单字段）。
_SUBTITLE_FILE_JSON: dict[str, Any] = {
    "body": [
        {"from": 0.0, "to": 2.5, "content": "你好", "extra": 1},
        {"from": 2.5, "to": 5.0, "content": "世界"},
    ],
    "extra": "dropped",
}


class _FakeClock:
    """fake 时钟（epoch 秒——手动推进）。"""

    def __init__(self, now: float = 1_000_000.0) -> None:
        """初始化（起始时刻可配置）。"""
        self.now = now

    def __call__(self) -> float:
        """返回当前时刻。"""
        return self.now


class _SleepRecorder:
    """fake sleep（记录全部等待秒数——零真实阻塞）。"""

    def __init__(self) -> None:
        """初始化（空记录）。"""
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        """记录等待调用。"""
        self.calls.append(seconds)


class _ScriptedFetch:
    """fake fetch——按 URL 关键字路由响应；记录全部调用。"""

    def __init__(self) -> None:
        """初始化（空路由）。"""
        self.routes: list[tuple[str, BiliFetchResponse]] = []
        self.calls: list[tuple[str, int]] = []

    def add(self, marker: str, response: BiliFetchResponse) -> None:
        """登记路由（URL 含 marker 即命中——先登先匹配）。"""
        self.routes.append((marker, response))

    def __call__(self, url: str, timeout_seconds: int) -> BiliFetchResponse:
        """记录调用并返回路由命中的响应。"""
        self.calls.append((url, timeout_seconds))
        for marker, response in self.routes:
            if marker in url:
                return response
        raise AssertionError(f"未路由的 fetch URL: {url}")


class _FakeNavFetch:
    """fake nav 键获取（记录 URL；返回固定 nav JSON）。"""

    def __init__(self, nav_json: dict[str, Any]) -> None:
        """初始化（固定 nav JSON）。"""
        self.nav_json = nav_json
        self.calls: list[str] = []

    def __call__(self, url: str) -> dict[str, Any]:
        """记录 URL 并返回固定 nav JSON。"""
        self.calls.append(url)
        return self.nav_json


def _registered_runtime(
    *,
    fetch: _ScriptedFetch,
    clock: _FakeClock,
    sleep: _SleepRecorder,
    nav_fetch: _FakeNavFetch | None = None,
    allowlist: list[str] | None = None,
) -> SandboxToolRuntime:
    """构造已注册全部 B 站工具的运行时（全注入面——零真实网络）。"""
    runtime = SandboxToolRuntime()
    register_bili_tools(
        runtime,
        allowlist_provider=lambda: allowlist if allowlist is not None else ["api.bilibili.com"],
        fetch=fetch,
        clock=clock,
        sleep=sleep,
        nav_fetch=nav_fetch if nav_fetch is not None else _FakeNavFetch(_FIXED_NAV_JSON),
    )
    return runtime


def _default_fetches(fetch: _ScriptedFetch) -> None:
    """登记四工具标准路由（view / search / reply / player / CDN）。"""
    fetch.add("/x/web-interface/view", _json_response(_VIEW_PAYLOAD))
    fetch.add("/x/web-interface/wbi/search/type", _json_response(_SEARCH_PAYLOAD))
    fetch.add("/x/v2/reply/main", _json_response(_REPLY_PAYLOAD))
    fetch.add("/x/player/v2", _json_response(_PLAYER_PAYLOAD))
    fetch.add("aisubtitle.biliapi.net", _json_response(_SUBTITLE_FILE_JSON))


# ---------------------------------------------------------------------------
# WBI 签名（确定性——期望值经测试内独立算法推导）
# ---------------------------------------------------------------------------


class TestWbiSigning:
    async def test_mixin_key_matches_reference(self) -> None:
        """mixin key：模块实现 == 测试内独立打乱表推导（32 位截断）。"""
        expected = _reference_mixin_key(_FIXED_IMG_KEY, _FIXED_SUB_KEY)
        assert derive_mixin_key(_FIXED_IMG_KEY, _FIXED_SUB_KEY) == expected
        assert len(expected) == 32

    async def test_sign_deterministic_and_reference_matched(self) -> None:
        """w_rfd：模块实现 == 参考算法（值过滤 + 排序 + urlencode + md5）。"""
        params: dict[str, Any] = {
            "search_type": "video",
            "keyword": "关键词!'()*",
            "page": 2,
            "pagesize": 30,
        }
        mixin_key = _reference_mixin_key(_FIXED_IMG_KEY, _FIXED_SUB_KEY)
        signature = wbi_sign(params, mixin_key, wts=1_700_000_000)
        expected = _reference_wbi_query(params, 1_700_000_000, mixin_key)
        assert f"w_rfd={signature}" in expected
        assert signature == hashlib.md5(
            (
                urllib.parse.urlencode(
                    dict(
                        sorted(
                            {
                                "search_type": "video",
                                "keyword": "关键词",
                                "page": "2",
                                "pagesize": "30",
                                "wts": "1700000000",
                            }.items()
                        )
                    )
                )
                + mixin_key
            ).encode("utf-8")
        ).hexdigest()

    async def test_build_query_shape(self) -> None:
        """查询串形态：排序参数 + wts + w_rfd 尾接。"""
        params: dict[str, Any] = {"b": 2, "a": 1}
        mixin_key = _reference_mixin_key(_FIXED_IMG_KEY, _FIXED_SUB_KEY)
        query = build_wbi_query(params, mixin_key, wts=1_700_000_000)
        assert query.startswith("a=1&b=2&wts=1700000000")
        assert "&w_rfd=" in query

    async def test_extract_wbi_keys(self) -> None:
        """键提取：URL basename 去扩展名。"""
        assert extract_wbi_keys(_FIXED_NAV_JSON) == (_FIXED_IMG_KEY, _FIXED_SUB_KEY)

    async def test_extract_wbi_keys_error_paths(self) -> None:
        """键提取失败面：data / wbi_img 缺失 + 空键（受控失败）。"""
        with pytest.raises(SandboxToolError, match="data"):
            extract_wbi_keys({"code": 0})
        with pytest.raises(SandboxToolError, match="wbi_img"):
            extract_wbi_keys({"code": 0, "data": {}})
        with pytest.raises(SandboxToolError, match="WBI 键为空"):
            extract_wbi_keys(
                {"code": 0, "data": {"wbi_img": {"img_url": "", "img_sub_url": ""}}}
            )

    async def test_search_url_signed_end_to_end(self) -> None:
        """全链签名：fetch 收到的搜索 URL == 参考算法查询串（nav 一次）。"""
        fetch = _ScriptedFetch()
        _default_fetches(fetch)
        clock = _FakeClock()
        runtime = _registered_runtime(fetch=fetch, clock=clock, sleep=_SleepRecorder())
        response = await runtime.invoke(
            InvokeRequestModel(
                tool=BILI_SEARCH_VIDEOS_TOOL_NAME,
                arguments={"keyword": "测试", "page": 2, "page_size": 30},
            )
        )
        assert response.ok is True
        params: dict[str, Any] = {
            "search_type": "video",
            "keyword": "测试",
            "page": 2,
            "pagesize": 30,
        }
        mixin_key = _reference_mixin_key(_FIXED_IMG_KEY, _FIXED_SUB_KEY)
        expected_query = _reference_wbi_query(params, int(clock.now), mixin_key)
        assert fetch.calls[0][0] == (
            f"https://api.bilibili.com/x/web-interface/wbi/search/type?{expected_query}"
        )

    async def test_nav_keys_cached_within_ttl(self) -> None:
        """nav 键 TTL 缓存：两次搜索 nav 只取一次（时钟未推进）。"""
        fetch = _ScriptedFetch()
        _default_fetches(fetch)
        nav_fetch = _FakeNavFetch(_FIXED_NAV_JSON)
        runtime = _registered_runtime(fetch=fetch, clock=_FakeClock(), sleep=_SleepRecorder(), nav_fetch=nav_fetch)
        for _ in range(2):
            response = await runtime.invoke(
                InvokeRequestModel(
                    tool=BILI_SEARCH_VIDEOS_TOOL_NAME, arguments={"keyword": "测试"}
                )
            )
            assert response.ok is True
        assert nav_fetch.calls == ["https://api.bilibili.com/x/web-interface/nav"]


# ---------------------------------------------------------------------------
# 参数模型校验矩阵
# ---------------------------------------------------------------------------


class TestArgsModels:
    async def test_search_defaults_and_bounds(self) -> None:
        """搜索参数：默认 page=1 / page_size=20；越界拒绝。"""
        args = BiliSearchVideosArgsModel.model_validate({"keyword": "测试"})
        assert (args.page, args.page_size) == (1, 20)
        with pytest.raises(ValidationError):
            BiliSearchVideosArgsModel.model_validate({"keyword": ""})
        with pytest.raises(ValidationError):
            BiliSearchVideosArgsModel.model_validate({"keyword": "x" * 65})
        with pytest.raises(ValidationError):
            BiliSearchVideosArgsModel.model_validate({"keyword": "x", "page": 0})
        with pytest.raises(ValidationError):
            BiliSearchVideosArgsModel.model_validate({"keyword": "x", "page": 6})
        with pytest.raises(ValidationError):
            BiliSearchVideosArgsModel.model_validate({"keyword": "x", "page_size": 15})
        with pytest.raises(ValidationError):
            BiliSearchVideosArgsModel.model_validate({"keyword": "x", "unexpected": 1})

    async def test_bvid_pattern_matrix(self) -> None:
        """BV 号形态：合法 12 位通过；前缀 / 长度 / 非法字符拒绝。"""
        assert BiliVideoInfoArgsModel.model_validate({"bvid": "BV1xx411c7mD"}).bvid == "BV1xx411c7mD"
        with pytest.raises(ValidationError):
            BiliVideoInfoArgsModel.model_validate({"bvid": "bv1xx411c7mD"})
        with pytest.raises(ValidationError):
            BiliVideoInfoArgsModel.model_validate({"bvid": "BV1xx411c7m"})
        with pytest.raises(ValidationError):
            BiliVideoInfoArgsModel.model_validate({"bvid": "BV1xx411c7mDD"})
        with pytest.raises(ValidationError):
            BiliVideoInfoArgsModel.model_validate({"bvid": "BV1xx411c7m!"})
        with pytest.raises(ValidationError):
            BiliVideoInfoArgsModel.model_validate({"bvid": "AV170001"})

    async def test_comments_defaults_and_bounds(self) -> None:
        """评论参数：默认 page=1 / page_size=20；1-50 / 10-20 边界。"""
        args = BiliVideoCommentsArgsModel.model_validate({"bvid": "BV1xx411c7mD"})
        assert (args.page, args.page_size) == (1, 20)
        assert BiliVideoCommentsArgsModel.model_validate(
            {"bvid": "BV1xx411c7mD", "page": 50, "page_size": 10}
        ).page == 50
        with pytest.raises(ValidationError):
            BiliVideoCommentsArgsModel.model_validate({"bvid": "BV1xx411c7mD", "page": 51})
        with pytest.raises(ValidationError):
            BiliVideoCommentsArgsModel.model_validate({"bvid": "BV1xx411c7mD", "page_size": 9})
        with pytest.raises(ValidationError):
            BiliVideoCommentsArgsModel.model_validate({"bvid": "BV1xx411c7mD", "page_size": 21})
        with pytest.raises(ValidationError):
            BiliVideoCommentsArgsModel.model_validate({"bvid": "BV1xx411c7mD", "post": "x"})

    async def test_subtitle_defaults(self) -> None:
        """字幕参数：fetch_subtitle 默认 False；extra 拒绝。"""
        args = BiliSubtitleArgsModel.model_validate({"bvid": "BV1xx411c7mD"})
        assert args.fetch_subtitle is False
        assert (
            BiliSubtitleArgsModel.model_validate(
                {"bvid": "BV1xx411c7mD", "fetch_subtitle": True}
            ).fetch_subtitle
            is True
        )
        with pytest.raises(ValidationError):
            BiliSubtitleArgsModel.model_validate({"bvid": "BV1xx411c7mD", "lines": True})


# ---------------------------------------------------------------------------
# 允许清单（默认非空 / 空串禁用——与 http_fetch 相反语义）
# ---------------------------------------------------------------------------


class TestAllowlist:
    async def test_env_default_when_unset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """未设置 env：默认清单（api.bilibili.com + app.bilibili.com）。"""
        monkeypatch.delenv("BILI_API_ALLOWLIST", raising=False)
        assert bili_module._allowlist_from_env() == ["api.bilibili.com", "app.bilibili.com"]

    async def test_env_empty_string_disables(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """空串：显式禁用（空清单——与 http_fetch 默认空不同，文档化差异）。"""
        monkeypatch.setenv("BILI_API_ALLOWLIST", "")
        assert bili_module._allowlist_from_env() == []

    async def test_env_custom_normalized(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """自定义清单：strip / 小写归一。"""
        monkeypatch.setenv("BILI_API_ALLOWLIST", " API.Bilibili.Com , example.com ")
        assert bili_module._allowlist_from_env() == ["api.bilibili.com", "example.com"]

    async def test_disabled_fails_before_network(self) -> None:
        """空清单：受控失败（未启用文案——fetch 零调用）。"""
        fetch = _ScriptedFetch()
        runtime = _registered_runtime(fetch=fetch, clock=_FakeClock(), sleep=_SleepRecorder(), allowlist=[])
        response = await runtime.invoke(
            InvokeRequestModel(
                tool=BILI_VIDEO_INFO_TOOL_NAME, arguments={"bvid": "BV1xx411c7mD"}
            )
        )
        assert response.ok is False
        assert "未启用" in (response.error or "")
        assert fetch.calls == []

    async def test_allowlist_miss_fails_before_network(self) -> None:
        """清单不含 B 站宿主：受控失败（零网络）。"""
        fetch = _ScriptedFetch()
        runtime = _registered_runtime(
            fetch=fetch, clock=_FakeClock(), sleep=_SleepRecorder(), allowlist=["example.com"]
        )
        response = await runtime.invoke(
            InvokeRequestModel(
                tool=BILI_VIDEO_INFO_TOOL_NAME, arguments={"bvid": "BV1xx411c7mD"}
            )
        )
        assert response.ok is False
        assert "不在允许清单" in (response.error or "")
        assert fetch.calls == []


# ---------------------------------------------------------------------------
# 裁剪纯函数
# ---------------------------------------------------------------------------


class TestTrimHelpers:
    async def test_duration_parsing(self) -> None:
        """时长解析：mm:ss / h:mm:ss / 容错零。"""
        assert bili_module._parse_duration_seconds("10:30") == 630
        assert bili_module._parse_duration_seconds("1:02:03") == 3723
        assert bili_module._parse_duration_seconds("0:00") == 0
        assert bili_module._parse_duration_seconds("") == 0
        assert bili_module._parse_duration_seconds("garbage") == 0

    async def test_em_tag_stripping(self) -> None:
        """标题高亮标签清理。"""
        assert bili_module._strip_em_tags('测试<em class="keyword">视频</em>') == "测试视频"

    async def test_subtitle_url_normalization(self) -> None:
        """字幕 URL 归一：协议相对 → https；绝对原样。"""
        assert (
            bili_module._normalize_subtitle_url("//aisubtitle.biliapi.net/x")
            == "https://aisubtitle.biliapi.net/x"
        )
        assert bili_module._normalize_subtitle_url("https://api.bilivideo.com/x") == "https://api.bilivideo.com/x"


# ---------------------------------------------------------------------------
# 默认抓取面（monkeypatch opener——零网络；代理禁用 + cookie 间接）
# ---------------------------------------------------------------------------


class _FakeUrllibResponse:
    """fake urllib 响应（上下文管理器 + read / status）。"""

    def __init__(self, *, status: int = 200, body: bytes = b"{}") -> None:
        """初始化。"""
        self.status = status
        self._body = body

    def read(self, amount: int = -1) -> bytes:
        """按上限读正文。"""
        if amount < 0:
            return self._body
        return self._body[:amount]

    def __enter__(self) -> _FakeUrllibResponse:
        """进入上下文。"""
        return self

    def __exit__(self, *exc_info: Any) -> bool:
        """退出上下文（不吞异常）。"""
        return False


class _FakeUrllibOpener:
    """fake urllib opener（记录请求 + 可注入异常）。"""

    def __init__(self, *, error: Exception | None = None) -> None:
        """初始化（error 优先于正常响应）。"""
        self._error = error
        self.calls: list[urllib.request.Request] = []

    def open(self, request: urllib.request.Request, timeout: float | None = None) -> _FakeUrllibResponse:
        """记录请求并返回响应 / 抛注入异常。"""
        self.calls.append(request)
        if self._error is not None:
            raise self._error
        return _FakeUrllibResponse(body=json.dumps({"code": 0, "data": {}}).encode())


class _ProxyCapture:
    """fake ProxyHandler（捕获 proxies dict——禁用系统代理断言面）。"""

    captured: list[dict[str, str]] = []

    def __init__(self, proxies: dict[str, str]) -> None:
        """记录 proxies 参数。"""
        _ProxyCapture.captured.append(proxies)


class TestDefaultBiliFetch:
    async def test_no_system_proxy_and_headers(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """代理禁用 + 请求头：ProxyHandler({}) + UA / Referer / Cookie 直发。"""
        opener = _FakeUrllibOpener()
        _ProxyCapture.captured = []
        monkeypatch.setattr(urllib.request, "ProxyHandler", _ProxyCapture)
        monkeypatch.setattr(urllib.request, "build_opener", lambda *args: opener)
        monkeypatch.setenv("BILI_COOKIE", "SESSDATA=abc; buvid3=xyz")
        response = default_bili_fetch("https://api.bilibili.com/x/web-interface/nav", 10)
        assert response.status_code == 200
        # 系统代理禁用：proxies 恒空 dict（不读 env 代理）
        assert _ProxyCapture.captured == [{}]
        request = opener.calls[0]
        assert request.get_header("Cookie") == "SESSDATA=abc; buvid3=xyz"
        assert request.get_header("User-agent") == bili_module._USER_AGENT
        assert request.get_header("Referer") == bili_module._REFERER

    async def test_cookie_absent_when_env_unset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """cookie 环境未设置：请求头不含 Cookie（匿名公共访问）。"""
        opener = _FakeUrllibOpener()
        monkeypatch.setattr(urllib.request, "build_opener", lambda *args: opener)
        monkeypatch.delenv("BILI_COOKIE", raising=False)
        default_bili_fetch("https://api.bilibili.com/x", 10)
        assert "Cookie" not in opener.calls[0].headers

    async def test_http_error_status_preserved(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """HTTP 错误状态：状态码保留（412 / 404 由核心判定语义）。"""
        monkeypatch.setattr(
            urllib.request,
            "build_opener",
            lambda *args: _FakeUrllibOpener(
                error=urllib.error.HTTPError("https://api.bilibili.com/x", 412, "Precondition Failed", None, None)
            ),
        )
        response = default_bili_fetch("https://api.bilibili.com/x", 10)
        assert response.status_code == 412

    async def test_transport_failures_controlled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """传输失败归一：URLError / 超时 / 未预期异常（受控失败面）。"""
        monkeypatch.setattr(
            urllib.request,
            "build_opener",
            lambda *args: _FakeUrllibOpener(error=urllib.error.URLError(OSError("refused"))),
        )
        with pytest.raises(SandboxToolError, match="请求失败"):
            default_bili_fetch("https://api.bilibili.com/x", 10)
        monkeypatch.setattr(
            urllib.request,
            "build_opener",
            lambda *args: _FakeUrllibOpener(error=TimeoutError("timed out")),
        )
        with pytest.raises(SandboxToolError, match="超时"):
            default_bili_fetch("https://api.bilibili.com/x", 10)
        monkeypatch.setattr(
            urllib.request,
            "build_opener",
            lambda *args: _FakeUrllibOpener(error=RuntimeError("boom")),
        )
        with pytest.raises(SandboxToolError, match="RuntimeError"):
            default_bili_fetch("https://api.bilibili.com/x", 10)


class TestDefaultNavFetch:
    async def test_412_raises_internal_signal(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """nav 412：内部信号 BiliHTTP412Error（核心转硬冷却）。"""
        monkeypatch.setattr(
            bili_module,
            "default_bili_fetch",
            lambda url, timeout: BiliFetchResponse(status_code=412, body=b""),
        )
        with pytest.raises(BiliHTTP412Error):
            default_nav_fetch("https://api.bilibili.com/x/web-interface/nav")

    async def test_non_200_and_invalid_json_controlled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """nav 非 200 / 非法 JSON：受控失败。"""
        monkeypatch.setattr(
            bili_module,
            "default_bili_fetch",
            lambda url, timeout: BiliFetchResponse(status_code=404, body=b""),
        )
        with pytest.raises(SandboxToolError, match="404"):
            default_nav_fetch("https://api.bilibili.com/x/web-interface/nav")
        monkeypatch.setattr(
            bili_module,
            "default_bili_fetch",
            lambda url, timeout: BiliFetchResponse(status_code=200, body=b"not json"),
        )
        with pytest.raises(SandboxToolError, match="非法 JSON"):
            default_nav_fetch("https://api.bilibili.com/x/web-interface/nav")

    async def test_success_returns_payload(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """nav 成功：完整 JSON dict 透传（code 判定在核心）。"""
        monkeypatch.setattr(
            bili_module,
            "default_bili_fetch",
            lambda url, timeout: _json_response(_FIXED_NAV_JSON),
        )
        assert default_nav_fetch("https://api.bilibili.com/x/web-interface/nav") == _FIXED_NAV_JSON


# ---------------------------------------------------------------------------
# 限速（最小间隔 1 秒——注入时钟 / sleep）
# ---------------------------------------------------------------------------


class TestIntervalGating:
    async def test_first_request_no_wait(self) -> None:
        """首请求零等待（无上次请求水位）。"""
        fetch = _ScriptedFetch()
        _default_fetches(fetch)
        sleep = _SleepRecorder()
        runtime = _registered_runtime(fetch=fetch, clock=_FakeClock(), sleep=sleep)
        response = await runtime.invoke(
            InvokeRequestModel(
                tool=BILI_VIDEO_INFO_TOOL_NAME, arguments={"bvid": "BV1xx411c7mD"}
            )
        )
        assert response.ok is True
        assert sleep.calls == []

    async def test_second_request_waits_interval(self) -> None:
        """同刻第二请求：等待 1 秒（时钟未推进——确定性断言）。"""
        fetch = _ScriptedFetch()
        _default_fetches(fetch)
        sleep = _SleepRecorder()
        runtime = _registered_runtime(fetch=fetch, clock=_FakeClock(), sleep=sleep)
        for _ in range(2):
            response = await runtime.invoke(
                InvokeRequestModel(
                    tool=BILI_VIDEO_INFO_TOOL_NAME, arguments={"bvid": "BV1xx411c7mD"}
                )
            )
            assert response.ok is True
        assert sleep.calls == [1.0]

    async def test_partial_elapsed_waits_remainder(self) -> None:
        """间隔部分流逝：等待剩余 0.6 秒。"""
        fetch = _ScriptedFetch()
        _default_fetches(fetch)
        clock = _FakeClock()
        sleep = _SleepRecorder()
        runtime = _registered_runtime(fetch=fetch, clock=clock, sleep=sleep)
        response = await runtime.invoke(
            InvokeRequestModel(
                tool=BILI_VIDEO_INFO_TOOL_NAME, arguments={"bvid": "BV1xx411c7mD"}
            )
        )
        assert response.ok is True
        clock.now += 0.4
        response = await runtime.invoke(
            InvokeRequestModel(
                tool=BILI_VIDEO_INFO_TOOL_NAME, arguments={"bvid": "BV1xx411c7mD"}
            )
        )
        assert response.ok is True
        assert sleep.calls == [pytest.approx(0.6)]


# ---------------------------------------------------------------------------
# HTTP 412 硬冷却（600 基线 x2 递增封顶 1800 + 过期恢复 + 成功归零）
# ---------------------------------------------------------------------------


class TestCooldown412:
    async def test_first_412_enters_600s_cooldown(self) -> None:
        """首个 412：600 秒硬冷却（文案可观测）+ 后续请求零网络快速失败。"""
        fetch = _ScriptedFetch()
        fetch.add("/x/web-interface/view", BiliFetchResponse(status_code=412, body=b""))
        clock = _FakeClock()
        runtime = _registered_runtime(fetch=fetch, clock=clock, sleep=_SleepRecorder())
        response = await runtime.invoke(
            InvokeRequestModel(
                tool=BILI_VIDEO_INFO_TOOL_NAME, arguments={"bvid": "BV1xx411c7mD"}
            )
        )
        assert response.ok is False
        assert "412" in (response.error or "")
        assert "600" in (response.error or "")
        # 冷却期内：快速失败（fetch 零新增调用）
        response = await runtime.invoke(
            InvokeRequestModel(
                tool=BILI_VIDEO_INFO_TOOL_NAME, arguments={"bvid": "BV1xx411c7mD"}
            )
        )
        assert response.ok is False
        assert "硬冷却中" in (response.error or "")
        assert len(fetch.calls) == 1

    async def test_escalation_doubles_and_caps(self) -> None:
        """连续 412 递增：600 → 1200 → 1800（封顶）；过期后恢复出站。"""
        fetch = _ScriptedFetch()
        fetch.add("/x/web-interface/view", BiliFetchResponse(status_code=412, body=b""))
        clock = _FakeClock()
        runtime = _registered_runtime(fetch=fetch, clock=clock, sleep=_SleepRecorder())

        async def _invoke() -> Any:
            """单次详情调用（闭包——参数恒定）。"""
            return await runtime.invoke(
                InvokeRequestModel(
                    tool=BILI_VIDEO_INFO_TOOL_NAME, arguments={"bvid": "BV1xx411c7mD"}
                )
            )

        first = await _invoke()
        assert "600" in (first.error or "")
        clock.now += 601
        second = await _invoke()
        assert "1200" in (second.error or "")
        clock.now += 1201
        third = await _invoke()
        assert "1800" in (third.error or "")
        # 三连击后过期恢复：仍可出站（第 4 次 fetch 调用）
        clock.now += 1801
        fourth = await _invoke()
        assert fourth.ok is False
        assert "412" in (fourth.error or "")
        assert len(fetch.calls) == 4

    async def test_success_resets_streak(self) -> None:
        """成功响应归零连击：冷却后再 412 从 600 重新起算。"""
        fetch = _ScriptedFetch()
        view_412 = BiliFetchResponse(status_code=412, body=b"")
        fetch.add("/x/web-interface/view", view_412)
        clock = _FakeClock()
        runtime = _registered_runtime(fetch=fetch, clock=clock, sleep=_SleepRecorder())

        async def _invoke() -> Any:
            """单次详情调用（闭包——参数恒定）。"""
            return await runtime.invoke(
                InvokeRequestModel(
                    tool=BILI_VIDEO_INFO_TOOL_NAME, arguments={"bvid": "BV1xx411c7mD"}
                )
            )

        first = await _invoke()
        assert "600" in (first.error or "")
        # 过期 + 恢复正常响应（路由替换为成功载荷）
        clock.now += 601
        fetch.routes.clear()
        fetch.add("/x/web-interface/view", _json_response(_VIEW_PAYLOAD))
        recovered = await _invoke()
        assert recovered.ok is True
        # 成功归零后再 412：重新 600 起算
        fetch.routes.clear()
        fetch.add("/x/web-interface/view", view_412)
        clock.now += 10
        again = await _invoke()
        assert "600" in (again.error or "")

    async def test_nav_412_enters_cooldown(self) -> None:
        """nav 路径 412：内部信号转硬冷却（搜索失败 + 后续快速失败）。"""

        class _RaisingNav:
            """恒抛 412 信号的 fake nav 获取。"""

            calls: list[str] = []

            def __call__(self, url: str) -> dict[str, Any]:
                """记录 URL 并抛 412 信号。"""
                self.calls.append(url)
                raise BiliHTTP412Error("Bilibili 风险控制（HTTP 412）——nav 键获取被拦截")

        fetch = _ScriptedFetch()
        _default_fetches(fetch)
        raising_nav = _RaisingNav()
        runtime = _registered_runtime(
            fetch=fetch, clock=_FakeClock(), sleep=_SleepRecorder(), nav_fetch=raising_nav
        )
        response = await runtime.invoke(
            InvokeRequestModel(
                tool=BILI_SEARCH_VIDEOS_TOOL_NAME, arguments={"keyword": "测试"}
            )
        )
        assert response.ok is False
        assert "412" in (response.error or "")
        assert "600" in (response.error or "")
        # 冷却中：再试仍快速失败（fetch 零调用——nav 已拦截）
        response = await runtime.invoke(
            InvokeRequestModel(
                tool=BILI_SEARCH_VIDEOS_TOOL_NAME, arguments={"keyword": "测试"}
            )
        )
        assert "硬冷却中" in (response.error or "")
        assert fetch.calls == []


# ---------------------------------------------------------------------------
# JSON code 判定（-352 不重试 / code!=0 映射 / 形态归一）
# ---------------------------------------------------------------------------


class TestPayloadCodes:
    async def test_code_minus_352_no_retry_no_cooldown(self) -> None:
        """code -352：不重试受控失败 + 零冷却（后续请求照常出站）。"""
        fetch = _ScriptedFetch()
        fetch.add(
            "/x/web-interface/view",
            _json_response({"code": -352, "message": "风控拦截"}),
        )
        runtime = _registered_runtime(fetch=fetch, clock=_FakeClock(), sleep=_SleepRecorder())
        response = await runtime.invoke(
            InvokeRequestModel(
                tool=BILI_VIDEO_INFO_TOOL_NAME, arguments={"bvid": "BV1xx411c7mD"}
            )
        )
        assert response.ok is False
        assert "-352" in (response.error or "")
        assert "不重试" in (response.error or "")
        # 无冷却：换正常载荷后照常成功（fetch 第 2 次调用）
        fetch.routes.clear()
        fetch.add("/x/web-interface/view", _json_response(_VIEW_PAYLOAD))
        response = await runtime.invoke(
            InvokeRequestModel(
                tool=BILI_VIDEO_INFO_TOOL_NAME, arguments={"bvid": "BV1xx411c7mD"}
            )
        )
        assert response.ok is True
        assert len(fetch.calls) == 2

    async def test_nonzero_code_mapped_with_code_and_message(self) -> None:
        """code!=0：code + message 入文案。"""
        fetch = _ScriptedFetch()
        fetch.add(
            "/x/web-interface/view",
            _json_response({"code": 40001, "message": "啥都木有"}),
        )
        runtime = _registered_runtime(fetch=fetch, clock=_FakeClock(), sleep=_SleepRecorder())
        response = await runtime.invoke(
            InvokeRequestModel(
                tool=BILI_VIDEO_INFO_TOOL_NAME, arguments={"bvid": "BV1xx411c7mD"}
            )
        )
        assert response.ok is False
        assert "40001" in (response.error or "")
        assert "啥都木有" in (response.error or "")

    async def test_malformed_payloads_controlled(self) -> None:
        """非法载荷：非 JSON / 非 dict / data 段缺失 / 非 200 状态。"""
        fetch = _ScriptedFetch()
        fetch.add("/x/web-interface/view", BiliFetchResponse(status_code=200, body=b"not json"))
        runtime = _registered_runtime(fetch=fetch, clock=_FakeClock(), sleep=_SleepRecorder())
        response = await runtime.invoke(
            InvokeRequestModel(
                tool=BILI_VIDEO_INFO_TOOL_NAME, arguments={"bvid": "BV1xx411c7mD"}
            )
        )
        assert "非法 JSON" in (response.error or "")
        fetch.routes.clear()
        fetch.add("/x/web-interface/view", BiliFetchResponse(status_code=200, body=b"[1,2]"))
        response = await runtime.invoke(
            InvokeRequestModel(
                tool=BILI_VIDEO_INFO_TOOL_NAME, arguments={"bvid": "BV1xx411c7mD"}
            )
        )
        assert "形态非法" in (response.error or "")
        fetch.routes.clear()
        fetch.add("/x/web-interface/view", _json_response({"code": 0}))
        response = await runtime.invoke(
            InvokeRequestModel(
                tool=BILI_VIDEO_INFO_TOOL_NAME, arguments={"bvid": "BV1xx411c7mD"}
            )
        )
        assert "data 段缺失" in (response.error or "")
        fetch.routes.clear()
        fetch.add("/x/web-interface/view", BiliFetchResponse(status_code=404, body=b""))
        response = await runtime.invoke(
            InvokeRequestModel(
                tool=BILI_VIDEO_INFO_TOOL_NAME, arguments={"bvid": "BV1xx411c7mD"}
            )
        )
        assert "HTTP 404" in (response.error or "")


# ---------------------------------------------------------------------------
# 白名单裁剪 + 16KB 结果上限
# ---------------------------------------------------------------------------


class TestSearchTrimming:
    async def test_whitelist_and_duration_parsing(self) -> None:
        """搜索裁剪：白名单字段 + 高亮清理 + 时长解析 + 隐藏播放量 None。"""
        fetch = _ScriptedFetch()
        _default_fetches(fetch)
        runtime = _registered_runtime(fetch=fetch, clock=_FakeClock(), sleep=_SleepRecorder())
        response = await runtime.invoke(
            InvokeRequestModel(
                tool=BILI_SEARCH_VIDEOS_TOOL_NAME,
                arguments={"keyword": "测试", "page": 2, "page_size": 30},
            )
        )
        assert response.ok is True
        result = response.result
        assert result["page"] == 2
        assert result["page_size"] == 30
        assert result["total"] == 2
        assert result["videos"] == [
            {
                "bvid": "BV1xx411c7mD",
                "title": "测试视频",
                "author": "up主甲",
                "duration_seconds": 630,
                "play": 12345,
                "pubdate_ts": 1700000000,
            },
            {
                "bvid": "BV2yy411c7mE",
                "title": "隐藏播放量",
                "author": "up主乙",
                "duration_seconds": 3723,
                "play": None,
                "pubdate_ts": None,
            },
        ]
        assert "truncated" not in result

    async def test_result_capped_at_16kb(self) -> None:
        """16KB 上限：超限自尾部整项裁剪 + truncated 标记。"""
        fat_payload: dict[str, Any] = {
            "code": 0,
            "data": {
                "numResults": 3,
                "result": [
                    {
                        "bvid": f"BV1xx411c7m{i}",
                        "title": "a" * 6000,
                        "author": "up",
                        "duration": "1:00",
                    }
                    for i in range(3)
                ],
            },
        }
        fetch = _ScriptedFetch()
        fetch.add("/x/web-interface/wbi/search/type", _json_response(fat_payload))
        runtime = _registered_runtime(fetch=fetch, clock=_FakeClock(), sleep=_SleepRecorder())
        response = await runtime.invoke(
            InvokeRequestModel(
                tool=BILI_SEARCH_VIDEOS_TOOL_NAME, arguments={"keyword": "测试"}
            )
        )
        assert response.ok is True
        result = response.result
        assert result["truncated"] is True
        assert len(result["videos"]) == 2
        assert result["videos"][0]["bvid"] == "BV1xx411c7m0"
        assert len(json.dumps(result, ensure_ascii=False).encode("utf-8")) <= 16384


class TestVideoInfoTrimming:
    async def test_whitelist_fields_only(self) -> None:
        """详情裁剪：白名单字段精确相等（face / his_rank / dynamic 全裁）。"""
        fetch = _ScriptedFetch()
        _default_fetches(fetch)
        runtime = _registered_runtime(fetch=fetch, clock=_FakeClock(), sleep=_SleepRecorder())
        response = await runtime.invoke(
            InvokeRequestModel(
                tool=BILI_VIDEO_INFO_TOOL_NAME, arguments={"bvid": "BV1xx411c7mD"}
            )
        )
        assert response.ok is True
        assert response.result == {
            "bvid": "BV1xx411c7mD",
            "aid": 170001,
            "title": "视频标题",
            "desc": "视频简介",
            "owner": {"mid": 42, "name": "up主"},
            "duration": 754,
            "pubdate_ts": 1700000000,
            "stat": {
                "view": 100,
                "danmaku": 5,
                "reply": 6,
                "favorite": 7,
                "coin": 8,
                "share": 9,
                "like": 10,
            },
            "cid": 111,
            "pages_total": 1,
        }


class TestComments:
    async def test_cursor_params_and_trimming(self) -> None:
        """评论全链：view→aid 换算 + mode=2 游标参数 + 白名单裁剪。"""
        fetch = _ScriptedFetch()
        _default_fetches(fetch)
        runtime = _registered_runtime(fetch=fetch, clock=_FakeClock(), sleep=_SleepRecorder())
        response = await runtime.invoke(
            InvokeRequestModel(
                tool=BILI_VIDEO_COMMENTS_TOOL_NAME,
                arguments={"bvid": "BV1xx411c7mD", "page": 2, "page_size": 15},
            )
        )
        assert response.ok is True
        # 两次出站：view（换算 aid）→ reply/main（游标分页）
        assert len(fetch.calls) == 2
        reply_url = fetch.calls[1][0]
        assert "/x/v2/reply/main" in reply_url
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(reply_url).query)
        assert query["oid"] == ["170001"]
        assert query["type"] == ["1"]
        assert query["mode"] == ["2"]
        assert query["ps"] == ["15"]
        assert query["next"] == ["2"]
        result = response.result
        assert result["bvid"] == "BV1xx411c7mD"
        assert result["page"] == 2
        assert result["page_size"] == 15
        assert result["next_page"] == 3
        assert result["comments"] == [
            {
                "rpid": 111,
                "uname": "评论者甲",
                "message": "好视频",
                "like": 5,
                "ctime": 1700000100,
            },
            {
                "rpid": 112,
                "uname": "评论者乙",
                "message": "长" * 1000,
                "like": 0,
                "ctime": 1700000200,
            },
        ]

    async def test_replies_null_tolerated(self) -> None:
        """评论空容忍：replies null → 空列表（页尾非失败）。"""
        fetch = _ScriptedFetch()
        fetch.add("/x/web-interface/view", _json_response(_VIEW_PAYLOAD))
        fetch.add("/x/v2/reply/main", _json_response({"code": 0, "data": {"replies": None, "cursor": {}}}))
        runtime = _registered_runtime(fetch=fetch, clock=_FakeClock(), sleep=_SleepRecorder())
        response = await runtime.invoke(
            InvokeRequestModel(
                tool=BILI_VIDEO_COMMENTS_TOOL_NAME, arguments={"bvid": "BV1xx411c7mD"}
            )
        )
        assert response.ok is True
        assert response.result["comments"] == []
        assert response.result["next_page"] is None

    async def test_aid_missing_controlled(self) -> None:
        """aid 换算缺失：受控失败（评论换算失败面）。"""
        fetch = _ScriptedFetch()
        fetch.add(
            "/x/web-interface/view",
            _json_response({"code": 0, "data": {"bvid": "BV1xx411c7mD"}}),
        )
        runtime = _registered_runtime(fetch=fetch, clock=_FakeClock(), sleep=_SleepRecorder())
        response = await runtime.invoke(
            InvokeRequestModel(
                tool=BILI_VIDEO_COMMENTS_TOOL_NAME, arguments={"bvid": "BV1xx411c7mD"}
            )
        )
        assert response.ok is False
        assert "aid" in (response.error or "")

    async def test_comment_model_direct(self) -> None:
        """评论模型直构（白名单字段锚点）。"""
        comment = BiliCommentModel(
            rpid=1, uname="u", message="m", like=2, ctime=3
        )
        assert comment.model_dump() == {
            "rpid": 1,
            "uname": "u",
            "message": "m",
            "like": 2,
            "ctime": 3,
        }


class TestSubtitle:
    async def test_list_only_without_fetch(self) -> None:
        """字幕清单（fetch_subtitle 缺省 False）：URL 归一 + 零 CDN 抓取。"""
        fetch = _ScriptedFetch()
        _default_fetches(fetch)
        runtime = _registered_runtime(fetch=fetch, clock=_FakeClock(), sleep=_SleepRecorder())
        response = await runtime.invoke(
            InvokeRequestModel(
                tool=BILI_VIDEO_SUBTITLE_TOOL_NAME, arguments={"bvid": "BV1xx411c7mD"}
            )
        )
        assert response.ok is True
        # 两次出站：view（换算 cid）→ player；零 CDN 调用
        assert len(fetch.calls) == 2
        player_url = fetch.calls[1][0]
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(player_url).query)
        assert query["bvid"] == ["BV1xx411c7mD"]
        assert query["cid"] == ["111"]
        assert response.result == {
            "bvid": "BV1xx411c7mD",
            "cid": 111,
            "subtitles": [
                {
                    "lan": "ai-zh",
                    "lan_doc": "中文（自动生成）",
                    "subtitle_url": "https://aisubtitle.biliapi.net/bfs/subtitle.json?x=1",
                },
                {
                    "lan": "en",
                    "lan_doc": "英文",
                    "subtitle_url": "https://api.bilivideo.com/subtitle.json",
                },
            ],
        }

    async def test_fetch_first_subtitle_lines(self) -> None:
        """fetch_subtitle=True：CDN 后缀清单内抓取 + 正文行白名单裁剪。"""
        fetch = _ScriptedFetch()
        _default_fetches(fetch)
        runtime = _registered_runtime(fetch=fetch, clock=_FakeClock(), sleep=_SleepRecorder())
        response = await runtime.invoke(
            InvokeRequestModel(
                tool=BILI_VIDEO_SUBTITLE_TOOL_NAME,
                arguments={"bvid": "BV1xx411c7mD", "fetch_subtitle": True},
            )
        )
        assert response.ok is True
        assert len(fetch.calls) == 3
        assert "aisubtitle.biliapi.net" in fetch.calls[2][0]
        assert response.result["lan"] == "ai-zh"
        assert response.result["subtitle_lines"] == [
            {"from_ts": 0.0, "to_ts": 2.5, "content": "你好"},
            {"from_ts": 2.5, "to_ts": 5.0, "content": "世界"},
        ]

    async def test_cdn_suffix_rejected(self) -> None:
        """CDN 后缀不中：受控失败（零 CDN 抓取——清单外域名拒绝）。"""
        hostile_payload: dict[str, Any] = {
            "code": 0,
            "data": {
                "subtitle": {
                    "subtitles": [
                        {"lan": "ai-zh", "lan_doc": "x", "subtitle_url": "//evil.example.com/s.json"}
                    ]
                }
            },
        }
        fetch = _ScriptedFetch()
        fetch.add("/x/web-interface/view", _json_response(_VIEW_PAYLOAD))
        fetch.add("/x/player/v2", _json_response(hostile_payload))
        runtime = _registered_runtime(fetch=fetch, clock=_FakeClock(), sleep=_SleepRecorder())
        response = await runtime.invoke(
            InvokeRequestModel(
                tool=BILI_VIDEO_SUBTITLE_TOOL_NAME,
                arguments={"bvid": "BV1xx411c7mD", "fetch_subtitle": True},
            )
        )
        assert response.ok is False
        assert "字幕 CDN" in (response.error or "")
        assert len(fetch.calls) == 2

    async def test_cdn_invalid_json_controlled(self) -> None:
        """CDN 响应非法 JSON：受控失败（后缀清单内但正文破坏面）。"""
        fetch = _ScriptedFetch()
        fetch.add("/x/web-interface/view", _json_response(_VIEW_PAYLOAD))
        fetch.add("/x/player/v2", _json_response(_PLAYER_PAYLOAD))
        fetch.add(
            "aisubtitle.biliapi.net",
            BiliFetchResponse(status_code=200, body=b"not json"),
        )
        runtime = _registered_runtime(fetch=fetch, clock=_FakeClock(), sleep=_SleepRecorder())
        response = await runtime.invoke(
            InvokeRequestModel(
                tool=BILI_VIDEO_SUBTITLE_TOOL_NAME,
                arguments={"bvid": "BV1xx411c7mD", "fetch_subtitle": True},
            )
        )
        assert response.ok is False
        assert "字幕文件响应非法 JSON" in (response.error or "")

    async def test_empty_subtitles_tolerated(self) -> None:
        """空容忍：无字幕段 / 无分 P → 空清单（登录限定字幕非失败）。"""
        fetch = _ScriptedFetch()
        fetch.add("/x/web-interface/view", _json_response(_VIEW_PAYLOAD))
        fetch.add("/x/player/v2", _json_response({"code": 0, "data": {}}))
        runtime = _registered_runtime(fetch=fetch, clock=_FakeClock(), sleep=_SleepRecorder())
        response = await runtime.invoke(
            InvokeRequestModel(
                tool=BILI_VIDEO_SUBTITLE_TOOL_NAME, arguments={"bvid": "BV1xx411c7mD"}
            )
        )
        assert response.ok is True
        assert response.result["subtitles"] == []
        # 无分 P：零 player 调用（cid 缺失短路）
        fetch.routes.clear()
        no_pages = {**_VIEW_PAYLOAD, "data": {**_VIEW_PAYLOAD["data"], "pages": []}}
        fetch.add("/x/web-interface/view", _json_response(no_pages))
        response = await runtime.invoke(
            InvokeRequestModel(
                tool=BILI_VIDEO_SUBTITLE_TOOL_NAME, arguments={"bvid": "BV1xx411c7mD"}
            )
        )
        assert response.ok is True
        assert response.result["subtitles"] == []
        assert response.result["cid"] is None
        assert len(fetch.calls) == 3


# ---------------------------------------------------------------------------
# 工厂全链（注册面 + 默认值物化 + 参数校验失败归一）
# ---------------------------------------------------------------------------


class TestRegisterBiliTools:
    async def test_registers_four_tools(self) -> None:
        """注册面：四工具齐备（注册序）。"""
        runtime = _registered_runtime(
            fetch=_ScriptedFetch(), clock=_FakeClock(), sleep=_SleepRecorder()
        )
        assert runtime.tool_names == [
            BILI_SEARCH_VIDEOS_TOOL_NAME,
            BILI_VIDEO_INFO_TOOL_NAME,
            BILI_VIDEO_COMMENTS_TOOL_NAME,
            BILI_VIDEO_SUBTITLE_TOOL_NAME,
        ]

    async def test_defaults_materialized_through_wire(self) -> None:
        """默认值物化：缺省 page / page_size 经运行时校验进入 fetch URL。"""
        fetch = _ScriptedFetch()
        _default_fetches(fetch)
        runtime = _registered_runtime(fetch=fetch, clock=_FakeClock(), sleep=_SleepRecorder())
        response = await runtime.invoke(
            InvokeRequestModel(
                tool=BILI_SEARCH_VIDEOS_TOOL_NAME, arguments={"keyword": "测试"}
            )
        )
        assert response.ok is True
        query = urllib.parse.parse_qs(
            urllib.parse.urlsplit(fetch.calls[0][0]).query
        )
        assert query["page"] == ["1"]
        assert query["pagesize"] == ["20"]

    async def test_invalid_args_map_to_ok_false(self) -> None:
        """参数校验失败：运行时归一 ok=False（零 fetch 调用）。"""
        fetch = _ScriptedFetch()
        runtime = _registered_runtime(fetch=fetch, clock=_FakeClock(), sleep=_SleepRecorder())
        response = await runtime.invoke(
            InvokeRequestModel(
                tool=BILI_VIDEO_INFO_TOOL_NAME, arguments={"bvid": "AV170001"}
            )
        )
        assert response.ok is False
        assert "参数校验失败" in (response.error or "")
        assert fetch.calls == []

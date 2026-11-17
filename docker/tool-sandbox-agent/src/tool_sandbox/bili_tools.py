"""Bilibili 只读工具组——公开 JSON 接口的白名单裁剪读取面（平台工具）。

本模块按 ``http_fetch`` 的接入范式实现四个**只读** B 站工具（工厂函数 +
参数 Model Class + :class:`SandboxToolError` 受控失败 + ``asyncio.to_thread``
阻塞下沉），全部命中公开 JSON 接口——**红线：零写入类端点**（无评论 /
弹幕发布、无登录流程、无任何状态变更面）。

端点面（全部 GET、全部公开只读）::

    搜索     /x/web-interface/wbi/search/type   （WBI 签名；search_type=video）
    详情     /x/web-interface/view              （非 WBI；bvid 直查）
    评论     /x/v2/reply                        （pn/ps 页序 + sort=2 时间序；ps<=20）
    字幕     /x/player/v2                       （view→cid 后查字幕清单）
    WBI 键   /x/web-interface/nav               （img/sub 键源——进程内 TTL 缓存）

风险控制语义（逐条锁定——公开风控行为的经验映射）：

    1. **限速**：进程内最小请求间隔 1 秒（含 nav 键获取与字幕 CDN 抓取
       ——全部出站请求统一过闸；时钟 / sleep 可注入——测试确定性面）；
    2. **HTTP 412 硬冷却**：命中即进入硬冷却（基线 600 秒，连续命中
       x2 递增、上限 1800 秒；进程内持久——时钟可注入）；冷却期间全部
       B 站请求快速失败（零网络）；非 412 成功响应将连击计数归零；
    3. **JSON code -352**：风控拦截——按**不重试**受控失败处理（不触发
       冷却——调用方应降频而非重试）；
    4. **JSON code != 0**：受控失败（code + message 入文案）。

WBI 签名（``/x/web-interface/wbi/*`` 端点必需）：nav 键（img_url /
sub_url 文件名去扩展名）经打乱表导出 32 位 mixin key → 参数值过滤
``!'()*`` 字符 + 追加 ``wts``（epoch 秒）→ 键排序 urlencode → 追加
mixin key 后取 md5 即 ``w_rfd``。nav 键每进程缓存（TTL 1800 秒——
键位轮换周期远大于 TTL，保守取半）。

安全面（与 http_fetch 的差异点——逐条红线）：

    1. **域名允许清单默认非空**：env ``BILI_API_ALLOWLIST`` 未设置 =
       默认清单 ``api.bilibili.com,app.bilibili.com``（本工具组存在的
       目的就是访问 B 站——与 http_fetch 的「空 = 默认拒绝」相反）；
       **空串 = 显式禁用**（部署方可一键关停）；匹配语义复用
       http_fetch 的点边界后缀匹配；
    2. **不走系统代理**：出站经 ``ProxyHandler({})`` 显式禁用环境代理
       （系统代理会破坏 B 站风控指纹——锁定决策）；
    3. **cookie 环境变量间接**：env ``BILI_COOKIE``（可选——匿名公共
       访问为缺省形态；设置时作为原生 Cookie 头直发，提升限速余量）；
       **cookie 永不入日志 / 错误面**；
    4. **字幕 CDN 抓取**：仅当 ``fetch_subtitle=true`` 时抓取首个字幕
       JSON——CDN URL 后缀允许清单（``biliapi.net`` / ``bilivideo.com`` /
       ``hdslb.com``——B 站 CDN 实际下发 ``aisubtitle.hdslb.com``）
       + 硬超时 + 体积上限（BILI_API_ALLOWLIST 不覆盖此面——字幕 URL
       由 B 站接口下发，后缀清单即信任面）；
    5. **响应白名单裁剪**：全部工具响应裁剪到白名单字段 + JSON 序列化
       上限约 16KB（超限自列表尾部整项裁剪 + ``truncated`` 标记）。

阻塞 I/O 下沉：stdlib urllib 在线程池执行（``asyncio.to_thread``——
本树零 httpx 依赖，与 http_fetch 同锁定决策）。

线程安全（共享核心状态面）：四工具执行器经 ``asyncio.to_thread`` 下沉
到线程池并发调用——``_BiliClientCore`` 以 ``threading.Lock`` 守护共享
状态迁移：``_gate`` 的限速检查-等待-置位整体持锁（并发下 1 秒最小间隔
不可穿透——「全部出站请求统一过闸」在并发面成立）；``_get_wbi_keys``
的 TTL 判定与键落位持锁（nav 取键在锁外经 ``_nav_gated`` 自行过闸——
罕见并发双取为同值良性竞态，文档化取舍）。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, Field

from tool_sandbox.http_fetch import domain_matches_allowlist, parse_allowlist
from tool_sandbox.runtime import SandboxToolError, SandboxToolRuntime, SandboxToolSpec

__all__ = [
    "BILI_SEARCH_VIDEOS_TOOL_NAME",
    "BILI_VIDEO_INFO_TOOL_NAME",
    "BILI_VIDEO_COMMENTS_TOOL_NAME",
    "BILI_VIDEO_SUBTITLE_TOOL_NAME",
    "BiliSearchVideosArgsModel",
    "BiliVideoInfoArgsModel",
    "BiliVideoCommentsArgsModel",
    "BiliSubtitleArgsModel",
    "BiliVideoSummaryModel",
    "BiliOwnerModel",
    "BiliStatModel",
    "BiliVideoInfoResultModel",
    "BiliCommentModel",
    "BiliSubtitleEntryModel",
    "BiliSubtitleLineModel",
    "BiliFetchResponse",
    "BiliHTTP412Error",
    "FetchFn",
    "NavFetchFn",
    "derive_mixin_key",
    "extract_wbi_keys",
    "wbi_sign",
    "build_wbi_query",
    "default_bili_fetch",
    "default_nav_fetch",
    "register_bili_tools",
]

# ---------------------------------------------------------------------------
# 工具名常量（与后端侧 harness/tools/sandbox_models.py 同名约定——跨树标识一致）
# ---------------------------------------------------------------------------

#: bili_search_videos 工具名（后端侧 LLM 工具面同名注册——跨树约定标识）。
BILI_SEARCH_VIDEOS_TOOL_NAME: str = "bili_search_videos"

#: bili_video_info 工具名（同上）。
BILI_VIDEO_INFO_TOOL_NAME: str = "bili_video_info"

#: bili_video_comments 工具名（同上）。
BILI_VIDEO_COMMENTS_TOOL_NAME: str = "bili_video_comments"

#: bili_video_subtitle 工具名（同上）。
BILI_VIDEO_SUBTITLE_TOOL_NAME: str = "bili_video_subtitle"

# ---------------------------------------------------------------------------
# 常量面（端点 / 限速 / 冷却 / 体积上限）
# ---------------------------------------------------------------------------

#: B 站 API 基础 URL（全部公开只读端点的宿主）。
_API_BASE: str = "https://api.bilibili.com"

#: API 宿主名（允许清单校验面——URL 由常量基座构造，宿主恒定）。
_API_HOST: str = "api.bilibili.com"

#: nav 键端点（WBI 键源——非 WBI 端点本身）。
_NAV_PATH: str = "/x/web-interface/nav"

#: 视频搜索端点（WBI 签名必需；search_type=video 只取视频类目）。
_SEARCH_PATH: str = "/x/web-interface/wbi/search/type"

#: 视频详情端点（非 WBI——bvid 直查公开数据）。
_VIEW_PATH: str = "/x/web-interface/view"

#: 评论主端点（sort=2 时间序 + pn/ps 页序——ps 上限 20）。
_REPLY_PATH: str = "/x/v2/reply"

#: 播放器端点（view→cid 后查字幕清单；部分视频须登录才有字幕——空容忍）。
_PLAYER_PATH: str = "/x/player/v2"

#: nav 键端点完整 URL（键缓存消费）。
_NAV_URL: str = f"{_API_BASE}{_NAV_PATH}"

#: 字幕 CDN 后缀允许清单（字幕 URL 由 B 站接口下发——后缀即信任面）。
_SUBTITLE_CDN_SUFFIXES: tuple[str, ...] = ("biliapi.net", "bilivideo.com", "hdslb.com")

#: 域名允许清单环境变量（未设置 = 默认清单；空串 = 显式禁用——与
#: http_fetch 的 SANDBOX_HTTP_ALLOWLIST「空 = 默认拒绝」语义相反，见模块
#: docstring 安全面第 1 条）。
_ALLOWLIST_ENV: str = "BILI_API_ALLOWLIST"

#: 默认允许清单原始值（未设置 env 时的缺省——本工具组的目的就是访问 B 站）。
_DEFAULT_ALLOWLIST_RAW: str = "api.bilibili.com,app.bilibili.com"

#: cookie 环境变量（可选——原生 Cookie 头值；永不入日志 / 错误面）。
_COOKIE_ENV: str = "BILI_COOKIE"

#: 出站请求 User-Agent（浏览器形态——API 风控对非浏览器 UA 更敏感）。
_USER_AGENT: str = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

#: 出站请求 Referer（B 站 API 风控校验面——搜索端点必需）。
_REFERER: str = "https://www.bilibili.com/"

#: 单次请求硬超时秒数（API 与字幕 CDN 共用）。
_REQUEST_TIMEOUT_SECONDS: int = 10

#: 原始响应体积上限（字节——API JSON / 字幕文件共用）。
_MAX_RESPONSE_BYTES: int = 512 * 1024

#: 结果 JSON 序列化上限（字节——约 16KB；超限自列表尾部整项裁剪）。
_MAX_RESULT_BYTES: int = 16384

#: 进程内最小请求间隔秒数（全部出站请求统一过闸——保守限速）。
_MIN_INTERVAL_SECONDS: float = 1.0

#: 硬冷却基线秒数（HTTP 412 首次命中）。
_COOLDOWN_BASE_SECONDS: float = 600.0

#: 硬冷却上限秒数（连续 412 x2 递增封顶）。
_COOLDOWN_MAX_SECONDS: float = 1800.0

#: WBI nav 键缓存 TTL 秒数（键位轮换周期远大于此——保守取半）。
_NAV_KEY_TTL_SECONDS: float = 1800.0

#: 评论正文单条字符上限（白名单裁剪面——超长评论截断）。
_MAX_COMMENT_MESSAGE_CHARS: int = 1000

#: WBI mixin key 打乱表（公开算法常量——索引重排 img+sub 原始键串）。
_MIXIN_KEY_ENC_TAB: tuple[int, ...] = (
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40,
    61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11,
    36, 20, 34, 44, 52,
)

#: WBI 参数值过滤字符集（公开算法常量——签名前剔除）。
_WBI_FILTER_CHARS: str = "!'()*"

#: 搜索结果标题 HTML 标签清理正则（B 站搜索高亮 ``<em class="keyword">``）。
_EM_TAG_RE: re.Pattern[str] = re.compile(r"<[^>]+>")

#: BV 号形态正则（BV + 10 位字母数字——参数校验面）。
_BVID_PATTERN: str = r"^BV[0-9A-Za-z]{10}$"


# ---------------------------------------------------------------------------
# 参数面 Model Classes（与后端 harness/tools/sandbox_models.py 人为镜像——
# 类定义块 byte 级一致；后端侧用于 registry 本地校验 + 默认值物化）；
# 紧随其后为结果面 Model Classes（白名单裁剪产物——沙盒树自持，不入后端
# 镜像面）。块边界约定（漂移门禁块提取口径）：两类之间**只隔空行**——
# 末位参数模型到下一顶层定义行之间零注释行，保证按「类定义行 → 下一
# 顶层定义行」提取的块在两树 byte 级一致。
# ---------------------------------------------------------------------------


class BiliSearchVideosArgsModel(BaseModel):
    """bili_search_videos 参数面（关键词视频搜索——沙盒侧校验锚点）。

    Attributes:
        keyword: 搜索关键词（1-64 字符）。
        page: 页码（1-5，默认 1）。
        page_size: 每页条数（10/20/30，默认 20）。
    """

    model_config = {"extra": "forbid"}

    keyword: str = Field(min_length=1, max_length=64, description="搜索关键词（1-64 字符）")
    page: int = Field(default=1, ge=1, le=5, description="页码（1-5，默认 1）")
    page_size: Literal[10, 20, 30] = Field(default=20, description="每页条数（10/20/30，默认 20）")


class BiliVideoInfoArgsModel(BaseModel):
    """bili_video_info 参数面（BV 号单视频详情——沙盒侧校验锚点）。

    Attributes:
        bvid: Bilibili 视频 BV 号（``BV`` + 10 位字母数字，共 12 字符）。
    """

    model_config = {"extra": "forbid"}

    bvid: str = Field(
        min_length=12,
        max_length=12,
        pattern=r"^BV[0-9A-Za-z]{10}$",
        description="Bilibili 视频 BV 号（BV + 10 位字母数字）",
    )


class BiliVideoCommentsArgsModel(BaseModel):
    """bili_video_comments 参数面（视频评论读取——沙盒侧校验锚点）。

    Attributes:
        bvid: Bilibili 视频 BV 号（``BV`` + 10 位字母数字，共 12 字符）。
        page: 页码（1-50，默认 1——sort=2 时间序页序）。
        page_size: 每页条数（10-20，默认 20——接口 ps 上限 20）。
    """

    model_config = {"extra": "forbid"}

    bvid: str = Field(
        min_length=12,
        max_length=12,
        pattern=r"^BV[0-9A-Za-z]{10}$",
        description="Bilibili 视频 BV 号（BV + 10 位字母数字）",
    )
    page: int = Field(default=1, ge=1, le=50, description="页码（1-50，默认 1）")
    page_size: int = Field(default=20, ge=10, le=20, description="每页条数（10-20，默认 20）")


class BiliSubtitleArgsModel(BaseModel):
    """bili_video_subtitle 参数面（视频字幕读取——沙盒侧校验锚点）。

    Attributes:
        bvid: Bilibili 视频 BV 号（``BV`` + 10 位字母数字，共 12 字符）。
        fetch_subtitle: 是否抓取首个字幕文件正文（默认 False——仅返回
            字幕清单；True 时经 CDN 后缀允许清单管制抓取）。
    """

    model_config = {"extra": "forbid"}

    bvid: str = Field(
        min_length=12,
        max_length=12,
        pattern=r"^BV[0-9A-Za-z]{10}$",
        description="Bilibili 视频 BV 号（BV + 10 位字母数字）",
    )
    fetch_subtitle: bool = Field(default=False, description="是否抓取首个字幕文件正文（默认否）")


class BiliVideoSummaryModel(BaseModel):
    """搜索结果单条（白名单字段——超出面一律裁剪）。

    Attributes:
        bvid: 视频 BV 号。
        title: 标题（搜索高亮 HTML 标签已清理）。
        author: UP 主名。
        duration_seconds: 时长秒数（``mm:ss`` / ``h:mm:ss`` 解析）。
        play: 播放量（部分结果隐藏时为 None）。
        pubdate_ts: 发布时间（epoch 秒；缺失为 None）。
    """

    bvid: str = Field(description="视频 BV 号")
    title: str = Field(description="标题（高亮标签已清理）")
    author: str = Field(description="UP 主名")
    duration_seconds: int = Field(description="时长秒数")
    play: int | None = Field(default=None, description="播放量（隐藏时 None）")
    pubdate_ts: int | None = Field(default=None, description="发布时间（epoch 秒）")


class BiliOwnerModel(BaseModel):
    """视频 UP 主（白名单字段）。

    Attributes:
        mid: UP 主用户 ID。
        name: UP 主名。
    """

    mid: int = Field(description="UP 主用户 ID")
    name: str = Field(description="UP 主名")


class BiliStatModel(BaseModel):
    """视频 stat 子集（白名单字段）。

    Attributes:
        view: 播放量。
        danmaku: 弹幕数。
        reply: 评论数。
        favorite: 收藏数。
        coin: 投币数。
        share: 分享数。
        like: 点赞数。
    """

    view: int = Field(description="播放量")
    danmaku: int = Field(description="弹幕数")
    reply: int = Field(description="评论数")
    favorite: int = Field(description="收藏数")
    coin: int = Field(description="投币数")
    share: int = Field(description="分享数")
    like: int = Field(description="点赞数")


class BiliVideoInfoResultModel(BaseModel):
    """视频详情结果（白名单字段）。

    Attributes:
        bvid: 视频 BV 号。
        aid: 视频 av 号。
        title: 标题。
        desc: 简介。
        owner: UP 主（mid + name）。
        duration: 时长秒数。
        pubdate_ts: 发布时间（epoch 秒）。
        stat: 互动数据子集。
        cid: 首 P 的 cid（无分 P 信息时 None）。
        pages_total: 分 P 总数。
    """

    bvid: str = Field(description="视频 BV 号")
    aid: int = Field(description="视频 av 号")
    title: str = Field(description="标题")
    desc: str = Field(description="简介")
    owner: BiliOwnerModel = Field(description="UP 主")
    duration: int = Field(description="时长秒数")
    pubdate_ts: int = Field(description="发布时间（epoch 秒）")
    stat: BiliStatModel = Field(description="互动数据子集")
    cid: int | None = Field(default=None, description="首 P cid（缺失 None）")
    pages_total: int = Field(description="分 P 总数")


class BiliCommentModel(BaseModel):
    """评论单条（白名单字段——扁平结构）。

    Attributes:
        rpid: 评论 ID。
        uname: 评论者名。
        message: 评论正文（超长截断至 1000 字符）。
        like: 点赞数。
        ctime: 发表时间（epoch 秒）。
    """

    rpid: int = Field(description="评论 ID")
    uname: str = Field(description="评论者名")
    message: str = Field(description="评论正文")
    like: int = Field(description="点赞数")
    ctime: int = Field(description="发表时间（epoch 秒）")


class BiliSubtitleEntryModel(BaseModel):
    """字幕清单单条（白名单字段）。

    Attributes:
        lan: 字幕语言码（如 ``ai-zh``）。
        lan_doc: 语言人读名（如 ``中文（自动生成）``）。
        subtitle_url: 字幕文件 URL（协议相对形态已归一为 https）。
    """

    lan: str = Field(description="语言码")
    lan_doc: str = Field(description="语言人读名")
    subtitle_url: str = Field(description="字幕文件 URL（已归一 https）")


class BiliSubtitleLineModel(BaseModel):
    """字幕正文单行（白名单字段）。

    Attributes:
        from_ts: 行起始时间（秒）。
        to_ts: 行结束时间（秒）。
        content: 行文本。
    """

    from_ts: float = Field(description="行起始时间（秒）")
    to_ts: float = Field(description="行结束时间（秒）")
    content: str = Field(description="行文本")


# ---------------------------------------------------------------------------
# 注入面契约（fetch / nav_fetch——测试 hermetic 面）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BiliFetchResponse:
    """单次 HTTP GET 结果（注入面契约——状态码 + 原始 body）。

    Attributes:
        status_code: HTTP 状态码（412 由核心转硬冷却；非 200 转受控失败）。
        body: 原始响应字节（体积上限内）。
    """

    status_code: int
    body: bytes


#: 抓取函数契约（url / 超时秒 → 响应；受控失败抛 SandboxToolError——
#: 测试注入面同签名）。
FetchFn = Callable[[str, int], BiliFetchResponse]

#: nav 键获取契约（nav URL → 完整 nav JSON dict——code 判定在核心）。
NavFetchFn = Callable[[str], dict[str, Any]]


class BiliHTTP412Error(SandboxToolError):
    """HTTP 412 风险控制信号（内部信号面——默认 nav 路径上抛，核心转硬冷却）。

    仅在默认 nav_fetch 将 412 状态转译时使用（fetch 路径经
    :class:`BiliFetchResponse.status_code` 判定，不经本类）；核心捕获后
    进入硬冷却并以受控失败上抛——本类**恒不逃逸**到运行时面。
    """


# ---------------------------------------------------------------------------
# WBI 纯函数面（测试直调——签名确定性锚点）
# ---------------------------------------------------------------------------


def derive_mixin_key(img_key: str, sub_key: str) -> str:
    """导出 WBI mixin key（打乱表重排 + 截断 32 位）。

    纯函数（无 I/O）——测试直调面。

    Args:
        img_key: nav ``img_url`` 文件名（去扩展名）。
        sub_key: nav ``sub_url`` 文件名（去扩展名）。

    Returns:
        32 位 mixin key（签名盐）。
    """
    raw = img_key + sub_key
    return "".join(raw[index] for index in _MIXIN_KEY_ENC_TAB)[:32]


def _wbi_filtered_params(params: dict[str, Any], wts: int) -> dict[str, str]:
    """WBI 参数归一（值过滤 ``!'()*`` 字符 + 追加 wts——键排序由调用方）。

    Args:
        params: 原始请求参数。
        wts: 签名时间戳（epoch 秒）。

    Returns:
        归一后的参数（值全为字符串）。
    """
    filtered: dict[str, str] = {
        str(key): "".join(ch for ch in str(value) if ch not in _WBI_FILTER_CHARS)
        for key, value in params.items()
    }
    filtered["wts"] = str(wts)
    return filtered


def wbi_sign(params: dict[str, Any], mixin_key: str, *, wts: int) -> str:
    """计算 WBI 签名（``w_rfd``——排序参数 urlencode 后拼接 mixin key 取 md5）。

    纯函数（无 I/O）——测试直调面。md5 在此为平台签名算法（非安全哈希面）。

    Args:
        params: 请求参数（不含 wts / w_rfd）。
        mixin_key: 32 位签名盐（:func:`derive_mixin_key` 产物）。
        wts: 签名时间戳（epoch 秒）。

    Returns:
        签名十六进制串（``w_rfd`` 值）。
    """
    query = urllib.parse.urlencode(dict(sorted(_wbi_filtered_params(params, wts).items())))
    digest = hashlib.md5(f"{query}{mixin_key}".encode())
    return digest.hexdigest()


def build_wbi_query(params: dict[str, Any], mixin_key: str, *, wts: int) -> str:
    """构造 WBI 签名查询串（排序参数 + wts + ``w_rfd``——完整 query 段）。

    纯函数（无 I/O）——测试直调面。

    Args:
        params: 请求参数（不含 wts / w_rfd）。
        mixin_key: 32 位签名盐。
        wts: 签名时间戳（epoch 秒）。

    Returns:
        完整查询串（直接拼接在端点路径之后）。
    """
    query = urllib.parse.urlencode(dict(sorted(_wbi_filtered_params(params, wts).items())))
    signature = wbi_sign(params, mixin_key, wts=wts)
    return f"{query}&w_rfd={signature}"


def _filename_without_ext(url: str) -> str:
    """提取 URL 路径基名并去扩展名（nav 键提取辅助——纯函数）。

    Args:
        url: 完整 URL（如 ``https://i0.hdslb.com/bfs/wbi/<key>.png``）。

    Returns:
        文件名去扩展名（无路径分隔时原样去扩展名）。
    """
    basename = url.rsplit("/", 1)[-1]
    return basename.split(".", 1)[0]


def extract_wbi_keys(nav_json: dict[str, Any]) -> tuple[str, str]:
    """从 nav 响应提取 WBI 键（img_key / sub_key——纯函数）。

    Args:
        nav_json: nav 端点完整 JSON（``{"code": 0, "data": {"wbi_img": …}}``）。

    Returns:
        ``(img_key, sub_key)`` 二元组。

    Raises:
        SandboxToolError: data / wbi_img 段缺失或键为空（受控失败面）。
    """
    data = nav_json.get("data")
    if not isinstance(data, dict):
        raise SandboxToolError("Bilibili nav 响应 data 段缺失")
    wbi_img = data.get("wbi_img")
    if not isinstance(wbi_img, dict):
        raise SandboxToolError("Bilibili nav 响应 wbi_img 段缺失")
    img_key = _filename_without_ext(str(wbi_img.get("img_url") or ""))
    sub_key = _filename_without_ext(str(wbi_img.get("sub_url") or ""))
    if not img_key or not sub_key:
        raise SandboxToolError("Bilibili nav 响应 WBI 键为空")
    return img_key, sub_key


# ---------------------------------------------------------------------------
# 默认 HTTP 面（stdlib urllib——零 httpx；不走系统代理；cookie 环境间接）
# ---------------------------------------------------------------------------


def _build_headers() -> dict[str, str]:
    """构造出站请求头（UA + Referer + 可选 Cookie——永不入日志）。

    Returns:
        请求头 dict（``BILI_COOKIE`` 设置时含 Cookie 头）。
    """
    headers = {"User-Agent": _USER_AGENT, "Referer": _REFERER}
    cookie = os.environ.get(_COOKIE_ENV, "").strip()
    if cookie:
        headers["Cookie"] = cookie
    return headers


def default_bili_fetch(url: str, timeout_seconds: int) -> BiliFetchResponse:
    """stdlib urllib 同步抓取（线程池下沉由执行器 ``to_thread`` 承担）。

    传输失败归一 :class:`SandboxToolError`；HTTP 状态（含 412）原样返回
    由核心判定（412 → 硬冷却；非 200 → 受控失败）。**不走系统代理**
    （``ProxyHandler({})`` 显式禁用环境代理——锁定决策）。

    Args:
        url: 目标 URL（宿主校验由核心承担——本函数只管传输）。
        timeout_seconds: 请求超时秒数（连接 / 读取共用）。

    Returns:
        抓取结果（状态码 + 体积上限内原始字节）。

    Raises:
        SandboxToolError: 传输失败 / 超时 / 形态非法（受控失败面）。
    """
    request = urllib.request.Request(url, headers=_build_headers(), method="GET")
    no_proxy_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with no_proxy_opener.open(request, timeout=timeout_seconds) as response:
            raw = response.read(_MAX_RESPONSE_BYTES)
            return BiliFetchResponse(status_code=response.status, body=raw)
    except urllib.error.HTTPError as exc:
        # HTTP 错误状态（含 412）保留状态码——412 冷却语义由核心判定。
        return BiliFetchResponse(status_code=exc.code, body=b"")
    except urllib.error.URLError as exc:
        raise SandboxToolError(f"Bilibili 请求失败: {exc.reason}") from exc
    except TimeoutError as exc:
        raise SandboxToolError(f"Bilibili 请求超时（{timeout_seconds}s 上限）") from exc
    except Exception as exc:  # 兜底归一（受控失败——执行器对运行时恒不抛未受控异常）
        raise SandboxToolError(f"Bilibili 请求异常: {type(exc).__name__}") from exc


def default_nav_fetch(url: str) -> dict[str, Any]:
    """nav 键获取默认实现（经 :func:`default_bili_fetch` 传输 + JSON 解析）。

    Args:
        url: nav 端点 URL。

    Returns:
        nav 端点完整 JSON（code 判定在核心——注入面契约一致）。

    Raises:
        BiliHTTP412Error: nav 请求命中 412（核心转硬冷却）。
        SandboxToolError: 非 200 状态 / JSON 解析失败 / 传输失败。
    """
    response = default_bili_fetch(url, _REQUEST_TIMEOUT_SECONDS)
    if response.status_code == 412:
        raise BiliHTTP412Error("Bilibili 风险控制（HTTP 412）——nav 键获取被拦截")
    if response.status_code != 200:
        raise SandboxToolError(f"Bilibili nav 接口非成功状态: HTTP {response.status_code}")
    try:
        payload: Any = json.loads(response.body.decode("utf-8", errors="replace"))
    except ValueError as exc:
        raise SandboxToolError("Bilibili nav 接口响应非法 JSON") from exc
    if not isinstance(payload, dict):
        raise SandboxToolError("Bilibili nav 接口响应形态非法")
    return payload


# ---------------------------------------------------------------------------
# 白名单裁剪纯函数面
# ---------------------------------------------------------------------------


def _strip_em_tags(text: str) -> str:
    """清理搜索标题高亮 HTML 标签（``<em class="keyword">`` 等）。

    Args:
        text: 原始标题（可能含标签）。

    Returns:
        清理后纯文本。
    """
    return _EM_TAG_RE.sub("", text)


def _parse_duration_seconds(raw: str) -> int:
    """解析 B 站时长串（``mm:ss`` / ``h:mm:ss``——纯函数）。

    Args:
        raw: 原始时长串（如 ``"10:30"``）。

    Returns:
        秒数（解析失败返回 0——搜索结果缺失时长面容错）。
    """
    parts = raw.strip().split(":")
    if not all(part.isdigit() for part in parts) or not parts:
        return 0
    seconds = 0
    for part in parts:
        seconds = seconds * 60 + int(part)
    return seconds


def _to_int(value: Any) -> int | None:
    """宽容整数提取（int 直取；其余 None——纯函数）。

    Args:
        value: 原始值（播放量 ``"—"`` 隐藏面等）。

    Returns:
        整数值或 None。
    """
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _trim_search_item(item: dict[str, Any]) -> BiliVideoSummaryModel:
    """裁剪搜索结果单条到白名单字段。

    Args:
        item: 原始结果条目（含大量非白名单字段）。

    Returns:
        白名单裁剪产物。
    """
    return BiliVideoSummaryModel(
        bvid=str(item.get("bvid") or ""),
        title=_strip_em_tags(str(item.get("title") or "")),
        author=str(item.get("author") or ""),
        duration_seconds=_parse_duration_seconds(str(item.get("duration") or "0:0")),
        play=_to_int(item.get("play")),
        pubdate_ts=_to_int(item.get("pubdate")),
    )


def _trim_video_info(data: dict[str, Any]) -> BiliVideoInfoResultModel:
    """裁剪视频详情到白名单字段。

    Args:
        data: view 端点 data 段（含大量非白名单字段）。

    Returns:
        白名单裁剪产物（owner / stat 子集 + 首 P cid + 分 P 总数）。
    """
    owner_raw = data.get("owner")
    owner = owner_raw if isinstance(owner_raw, dict) else {}
    stat_raw = data.get("stat")
    stat = stat_raw if isinstance(stat_raw, dict) else {}
    pages_raw = data.get("pages")
    pages = pages_raw if isinstance(pages_raw, list) else []
    first_page = pages[0] if pages and isinstance(pages[0], dict) else {}
    cid = _to_int(first_page.get("cid"))
    return BiliVideoInfoResultModel(
        bvid=str(data.get("bvid") or ""),
        aid=_to_int(data.get("aid")) or 0,
        title=str(data.get("title") or ""),
        desc=str(data.get("desc") or ""),
        owner=BiliOwnerModel(
            mid=_to_int(owner.get("mid")) or 0,
            name=str(owner.get("name") or ""),
        ),
        duration=_to_int(data.get("duration")) or 0,
        pubdate_ts=_to_int(data.get("pubdate")) or 0,
        stat=BiliStatModel(
            view=_to_int(stat.get("view")) or 0,
            danmaku=_to_int(stat.get("danmaku")) or 0,
            reply=_to_int(stat.get("reply")) or 0,
            favorite=_to_int(stat.get("favorite")) or 0,
            coin=_to_int(stat.get("coin")) or 0,
            share=_to_int(stat.get("share")) or 0,
            like=_to_int(stat.get("like")) or 0,
        ),
        cid=cid,
        pages_total=len(pages),
    )


def _trim_comment(reply: dict[str, Any]) -> BiliCommentModel:
    """裁剪评论单条到白名单字段（扁平结构）。

    Args:
        reply: 原始评论条目（member / content 嵌套 + 大量非白名单字段）。

    Returns:
        白名单裁剪产物（正文超长截断至 1000 字符）。
    """
    member_raw = reply.get("member")
    member = member_raw if isinstance(member_raw, dict) else {}
    content_raw = reply.get("content")
    content = content_raw if isinstance(content_raw, dict) else {}
    message = str(content.get("message") or "")
    return BiliCommentModel(
        rpid=_to_int(reply.get("rpid")) or 0,
        uname=str(member.get("uname") or ""),
        message=message[:_MAX_COMMENT_MESSAGE_CHARS],
        like=_to_int(reply.get("like")) or 0,
        ctime=_to_int(reply.get("ctime")) or 0,
    )


def _normalize_subtitle_url(url: str) -> str:
    """归一字幕 URL（协议相对 ``//…`` → ``https://…``——纯函数）。

    Args:
        url: 接口下发的原始字幕 URL。

    Returns:
        归一后 URL（空串原样返回）。
    """
    if url.startswith("//"):
        return f"https:{url}"
    return url


def _json_bytes(payload: Any) -> int:
    """JSON 序列化字节数（UTF-8——体积上限判定面）。

    Args:
        payload: 待序列化对象。

    Returns:
        序列化字节数。
    """
    return len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))


def _cap_result(result: dict[str, Any], list_field: str) -> dict[str, Any]:
    """结果 JSON 上限约 16KB——超限自列表尾部整项裁剪 + ``truncated`` 标记。

    Args:
        result: 白名单裁剪后的结果 dict。
        list_field: 承裁列表字段名（如 ``videos`` / ``comments``）。

    Returns:
        上限内结果（未超限时原样返回；裁剪时附 ``truncated=True``）。
    """
    items = result.get(list_field)
    if not isinstance(items, list):
        return result
    trimmed = list(items)
    capped = False
    while trimmed and _json_bytes({**result, list_field: trimmed, "truncated": True}) > _MAX_RESULT_BYTES:
        trimmed.pop()
        capped = True
    outcome = {**result, list_field: trimmed}
    if capped:
        outcome["truncated"] = True
    return outcome


# ---------------------------------------------------------------------------
# 共享客户端核心（限速 / 硬冷却 / WBI 键缓存 / JSON 归一）
# ---------------------------------------------------------------------------


class _BiliClientCore:
    """B 站客户端核心（进程内共享——经 :func:`register_bili_tools` 单次构造）。

    状态面全部进程内持久（部署形态 = 单工厂单注册 = 进程生命周期）：

        - 限速水位（上次请求时间——最小间隔 1 秒）；
        - 硬冷却（到期时刻 + 连击计数——412 x2 递增封顶 1800 秒）；
        - WBI 键缓存（img/sub 键 + 取键时刻——TTL 1800 秒）。

    线程安全：执行器经 ``asyncio.to_thread`` 并发进入——共享状态迁移
    （限速水位 / WBI 键缓存）经 ``threading.Lock`` 串行化（见 ``_gate`` /
    ``_get_wbi_keys``；锁不可重入——nav 取键在 WBI 锁段外自行过闸）。

    Args（构造期属性文档见 ``__init__`` docstring）:
        无（见 ``__init__``）。
    """

    def __init__(
        self,
        *,
        fetch: FetchFn,
        clock: Callable[[], float],
        sleep: Callable[[float], None],
        nav_fetch: NavFetchFn,
        allowlist_provider: Callable[[], list[str]],
    ) -> None:
        """初始化核心（全部外部面注入——测试 hermetic 面）。

        Args:
            fetch: HTTP 抓取注入（None 由工厂替换为 :func:`default_bili_fetch`）。
            clock: 时钟注入（epoch 秒——限速 / 冷却 / wts 判定面）。
            sleep: 阻塞等待注入（限速间隔——执行器已下沉线程池）。
            nav_fetch: nav 键获取注入（None 由工厂替换为
                :func:`default_nav_fetch`）。
            allowlist_provider: 允许清单读取注入（每次请求现读）。
        """
        self._fetch = fetch
        self._clock = clock
        self._sleep = sleep
        self._nav_fetch = nav_fetch
        self._allowlist_provider = allowlist_provider
        self._state_lock = threading.Lock()
        self._last_request_at: float | None = None
        self._cooldown_until: float = 0.0
        self._cooldown_streak: int = 0
        self._wbi_keys: tuple[str, str] | None = None
        self._wbi_keys_at: float = 0.0

    # ------------------------------------------------------------------
    # 闸门（冷却 / 限速）与状态迁移
    # ------------------------------------------------------------------

    def _gate(self) -> None:
        """出站前置闸（硬冷却快速失败 + 最小间隔等待——零网络面先行）。

        线程安全：检查-等待-置位整体持 ``_state_lock``——并发调用在
        闸前排队，1 秒最小间隔在并发面不可穿透（等待期持锁为有意
        串行化：后续调用按预约水位依次过闸）。

        Raises:
            SandboxToolError: 硬冷却中（剩余秒数入文案——零网络快速失败）。
        """
        with self._state_lock:
            now = self._clock()
            if now < self._cooldown_until:
                remaining = int(self._cooldown_until - now)
                raise SandboxToolError(f"Bilibili 风险控制硬冷却中（约剩余 {remaining} 秒）——请稍后再试")
            if self._last_request_at is not None:
                wait = self._last_request_at + _MIN_INTERVAL_SECONDS - self._clock()
                if wait > 0:
                    self._sleep(wait)
            self._last_request_at = self._clock()

    def _enter_cooldown(self) -> float:
        """进入硬冷却（连击 x2 递增封顶——成功响应由调用方归零）。

        Returns:
            本轮冷却秒数（600 / 1200 / 1800 封顶）。
        """
        self._cooldown_streak += 1
        duration = min(
            _COOLDOWN_BASE_SECONDS * (2.0 ** (self._cooldown_streak - 1)),
            _COOLDOWN_MAX_SECONDS,
        )
        self._cooldown_until = self._clock() + duration
        return duration

    def _require_api_allowlist(self) -> None:
        """API 宿主允许清单校验（空清单 = 显式禁用——零网络面先行）。

        Raises:
            SandboxToolError: 清单为空（工具禁用）或宿主不在清单。
        """
        allowlist = self._allowlist_provider()
        if not allowlist:
            raise SandboxToolError(
                "bili 工具未启用：BILI_API_ALLOWLIST 显式置空（未设置 = 默认清单；空串 = 禁用）"
            )
        if not domain_matches_allowlist(_API_HOST, allowlist):
            raise SandboxToolError(f"Bilibili 接口域名不在允许清单: {_API_HOST}")

    # ------------------------------------------------------------------
    # WBI 键缓存（进程内 TTL）
    # ------------------------------------------------------------------

    def _get_wbi_keys(self) -> tuple[str, str]:
        """取 WBI 键（TTL 内复用进程缓存；过期经 nav 获取——闸门统一覆盖）。

        线程安全：TTL 判定与键落位各持 ``_state_lock`` 短临界段；nav
        取键在锁段外经 ``_nav_gated`` 自行过闸（锁不可重入——防自锁）。
        罕见并发双取为同值良性竞态（后写覆盖先写，值恒同——文档化取舍）。

        Returns:
            ``(img_key, sub_key)`` 二元组。

        Raises:
            SandboxToolError: nav 获取失败 / 412 冷却 / 响应形态非法。
        """
        with self._state_lock:
            now = self._clock()
            if self._wbi_keys is not None and (now - self._wbi_keys_at) <= _NAV_KEY_TTL_SECONDS:
                return self._wbi_keys
        nav_json = self._nav_gated()
        keys = extract_wbi_keys(nav_json)
        with self._state_lock:
            self._wbi_keys = keys
            self._wbi_keys_at = self._clock()
        return keys

    def _nav_gated(self) -> dict[str, Any]:
        """nav 获取（闸门 + 允许清单 + 412 转冷却 + code 判定）。

        Returns:
            nav **完整** JSON dict（键提取 :func:`extract_wbi_keys` 消费
            完整形态——data / wbi_img 段校验在提取面）。

        Raises:
            SandboxToolError: 禁用 / 清单不中 / 冷却 / 412 / code!=0。
        """
        self._require_api_allowlist()
        self._gate()
        try:
            nav_json = self._nav_fetch(_NAV_URL)
        except BiliHTTP412Error:
            duration = self._enter_cooldown()
            raise SandboxToolError(
                f"Bilibili 风险控制拦截（HTTP 412）——进入 {int(duration)} 秒硬冷却"
            ) from None
        self._check_code(nav_json, "nav ")
        return nav_json

    # ------------------------------------------------------------------
    # 请求归一（状态 / JSON / code 判定）
    # ------------------------------------------------------------------

    def _fetch_checked(self, url: str) -> BiliFetchResponse:
        """闸门 + 抓取 + 状态判定（412 转冷却；非 200 受控失败）。

        Args:
            url: 目标 URL（宿主校验由调用方先行）。

        Returns:
            200 状态的抓取结果。

        Raises:
            SandboxToolError: 冷却中 / 412（转硬冷却）/ 非 200 / 传输失败。
        """
        self._gate()
        response = self._fetch(url, _REQUEST_TIMEOUT_SECONDS)
        if response.status_code == 412:
            duration = self._enter_cooldown()
            raise SandboxToolError(
                f"Bilibili 风险控制拦截（HTTP 412）——进入 {int(duration)} 秒硬冷却"
            )
        if response.status_code != 200:
            raise SandboxToolError(f"Bilibili 接口非成功状态: HTTP {response.status_code}")
        self._cooldown_streak = 0
        return response

    def _check_code(self, payload: dict[str, Any], source: str) -> None:
        """JSON code 判定（code -352 不重试 / code!=0 受控失败）。

        Args:
            payload: 完整 JSON 响应 dict。
            source: 端点名（错误文案面——如 ``"nav "`` / ``"接口"``）。

        Raises:
            SandboxToolError: code -352（风控不重试）/ code!=0（code +
                message 入文案）。
        """
        code = payload.get("code")
        if code == -352:
            raise SandboxToolError("Bilibili 风险控制拦截（code -352）——不重试（请降低调用频率）")
        if code != 0:
            message = str(payload.get("message") or "")
            raise SandboxToolError(f"Bilibili {source}错误: code={code}, message={message}")

    def _request_json(self, path: str, params: dict[str, Any], *, wbi: bool) -> dict[str, Any]:
        """GET JSON API（允许清单 + WBI 签名 + 闸门 + 状态 / code 归一）。

        Args:
            path: 端点路径（如 ``/x/web-interface/view``）。
            params: 请求参数。
            wbi: 是否 WBI 签名（nav 键获取亦过闸——全部出站统一限速）。

        Returns:
            响应 data 段 dict。

        Raises:
            SandboxToolError: 全链受控失败面（禁用 / 清单 / 冷却 / 412 /
                -352 / code!=0 / 非 200 / 传输失败）。
        """
        self._require_api_allowlist()
        if wbi:
            img_key, sub_key = self._get_wbi_keys()
            mixin_key = derive_mixin_key(img_key, sub_key)
            query = build_wbi_query(params, mixin_key, wts=int(self._clock()))
        else:
            query = urllib.parse.urlencode(params)
        response = self._fetch_checked(f"{_API_BASE}{path}?{query}")
        try:
            payload: Any = json.loads(response.body.decode("utf-8", errors="replace"))
        except ValueError as exc:
            raise SandboxToolError("Bilibili 接口响应非法 JSON") from exc
        if not isinstance(payload, dict):
            raise SandboxToolError("Bilibili 接口响应形态非法")
        self._check_code(payload, "接口")
        data = payload.get("data")
        if not isinstance(data, dict):
            raise SandboxToolError("Bilibili 接口响应 data 段缺失")
        return data

    def _fetch_cdn_json(self, url: str) -> dict[str, Any]:
        """字幕 CDN 抓取（后缀允许清单 + 闸门 + 状态判定 + JSON 解析）。

        与 API 允许清单的分工：字幕 URL 由 B 站接口下发（不可预先枚举），
        信任面 = 固定后缀清单（``biliapi.net`` / ``bilivideo.com`` /
        ``hdslb.com``——与 ``_SUBTITLE_CDN_SUFFIXES`` 一致）。

        Args:
            url: 字幕文件 URL（已归一 https 形态）。

        Returns:
            解析后的字幕 JSON dict。

        Raises:
            SandboxToolError: 后缀不中 / 冷却 / 412 / 非 200 / JSON 非法。
        """
        host = (urlparse(url).hostname or "").lower().rstrip(".")
        if not any(host == suffix or host.endswith(f".{suffix}") for suffix in _SUBTITLE_CDN_SUFFIXES):
            raise SandboxToolError(f"字幕 CDN 域名不在允许清单: {host}")
        response = self._fetch_checked(url)
        try:
            payload: Any = json.loads(response.body.decode("utf-8", errors="replace"))
        except ValueError as exc:
            raise SandboxToolError("字幕文件响应非法 JSON") from exc
        if not isinstance(payload, dict):
            raise SandboxToolError("字幕文件响应形态非法")
        return payload

    # ------------------------------------------------------------------
    # 工具面（白名单裁剪 + 16KB 上限——经 to_thread 下沉调用）
    # ------------------------------------------------------------------

    def search_videos(self, *, keyword: str, page: int, page_size: int) -> dict[str, Any]:
        """视频搜索（WBI 签名——search_type=video 类目）。

        Args:
            keyword: 搜索关键词（1-64 字符）。
            page: 页码（1-5）。
            page_size: 每页条数（10/20/30）。

        Returns:
            白名单裁剪结果（videos 列表 + 分页元信息 + total）。

        Raises:
            SandboxToolError: 全链受控失败面（见 :meth:`_request_json`）。
        """
        params: dict[str, Any] = {
            "search_type": "video",
            "keyword": keyword,
            "page": page,
            "pagesize": page_size,
        }
        data = self._request_json(_SEARCH_PATH, params, wbi=True)
        results_raw = data.get("result")
        items = results_raw if isinstance(results_raw, list) else []
        videos = [
            _trim_search_item(item)
            for item in items
            if isinstance(item, dict) and item.get("bvid")
        ]
        result: dict[str, Any] = {
            "videos": [video.model_dump() for video in videos],
            "page": page,
            "page_size": page_size,
            "total": _to_int(data.get("numResults")),
        }
        return _cap_result(result, "videos")

    def video_info(self, *, bvid: str) -> dict[str, Any]:
        """视频详情（view 端点直查——非 WBI）。

        Args:
            bvid: 视频 BV 号。

        Returns:
            白名单裁剪结果（owner / stat 子集 + 首 P cid + 分 P 总数）。

        Raises:
            SandboxToolError: 全链受控失败面（见 :meth:`_request_json`）。
        """
        data = self._request_json(_VIEW_PATH, {"bvid": bvid}, wbi=False)
        return _trim_video_info(data).model_dump()

    def video_comments(self, *, bvid: str, page: int, page_size: int) -> dict[str, Any]:
        """视频评论（view→aid 换算 + reply 端点 pn/ps 页序分页）。

        分页语义：``pn`` 即页序（1 起）、``ps`` 每页条数（参数面 10-20
        收敛）、``sort=2`` 时间序；``next_page`` 由 ``page.count`` 总数
        换算（本页末尾超出总数即 0——无更多页）。

        Args:
            bvid: 视频 BV 号。
            page: 页码（1-50）。
            page_size: 每页条数（10-20）。

        Returns:
            白名单裁剪结果（comments 扁平列表 + 分页元信息 + 下一页码）。

        Raises:
            SandboxToolError: 全链受控失败面 + aid 换算缺失。
        """
        view = self._request_json(_VIEW_PATH, {"bvid": bvid}, wbi=False)
        aid = _to_int(view.get("aid"))
        if aid is None:
            raise SandboxToolError("Bilibili 视频响应 aid 段缺失（评论换算失败）")
        params: dict[str, Any] = {
            "oid": aid,
            "type": 1,
            "sort": 2,
            "pn": page,
            "ps": page_size,
        }
        data = self._request_json(_REPLY_PATH, params, wbi=False)
        replies_raw = data.get("replies")
        replies = replies_raw if isinstance(replies_raw, list) else []
        comments = [
            _trim_comment(reply) for reply in replies if isinstance(reply, dict)
        ]
        page_raw = data.get("page")
        page_info = page_raw if isinstance(page_raw, dict) else {}
        total = _to_int(page_info.get("count")) or 0
        next_page = page + 1 if page * page_size < total else 0
        result: dict[str, Any] = {
            "bvid": bvid,
            "page": page,
            "page_size": page_size,
            "comments": [comment.model_dump() for comment in comments],
            "next_page": next_page,
        }
        return _cap_result(result, "comments")

    def video_subtitle(self, *, bvid: str, fetch_subtitle: bool) -> dict[str, Any]:
        """视频字幕（view→cid 换算 + player v2 字幕清单 + 可选 CDN 抓取）。

        空容忍：部分视频须登录才有字幕（无字幕 / 无分 P → 空清单，非失败）。

        Args:
            bvid: 视频 BV 号。
            fetch_subtitle: 是否抓取首个字幕文件正文（CDN 后缀清单管制）。

        Returns:
            白名单裁剪结果（subtitles 清单 + 可选 subtitle_lines 正文）。

        Raises:
            SandboxToolError: 全链受控失败面 + 字幕 CDN 后缀不中。
        """
        view = self._request_json(_VIEW_PATH, {"bvid": bvid}, wbi=False)
        info = _trim_video_info(view)
        entries: list[BiliSubtitleEntryModel] = []
        if info.cid is not None:
            data = self._request_json(
                _PLAYER_PATH, {"bvid": bvid, "cid": info.cid}, wbi=False
            )
            subtitle_raw = data.get("subtitle")
            subtitle = subtitle_raw if isinstance(subtitle_raw, dict) else {}
            subtitles_raw = subtitle.get("subtitles")
            items = subtitles_raw if isinstance(subtitles_raw, list) else []
            entries = [
                BiliSubtitleEntryModel(
                    lan=str(item.get("lan") or ""),
                    lan_doc=str(item.get("lan_doc") or ""),
                    subtitle_url=_normalize_subtitle_url(str(item.get("subtitle_url") or "")),
                )
                for item in items
                if isinstance(item, dict) and item.get("subtitle_url")
            ]
        result: dict[str, Any] = {
            "bvid": bvid,
            "cid": info.cid,
            "subtitles": [entry.model_dump() for entry in entries],
        }
        if fetch_subtitle and entries:
            subtitle_json = self._fetch_cdn_json(entries[0].subtitle_url)
            body_raw = subtitle_json.get("body")
            lines_raw = body_raw if isinstance(body_raw, list) else []
            lines = [
                BiliSubtitleLineModel(
                    from_ts=float(line.get("from") or 0.0),
                    to_ts=float(line.get("to") or 0.0),
                    content=str(line.get("content") or ""),
                )
                for line in lines_raw
                if isinstance(line, dict)
            ]
            result["lan"] = entries[0].lan
            result["subtitle_lines"] = [line.model_dump() for line in lines]
            return _cap_result(result, "subtitle_lines")
        return _cap_result(result, "subtitles")


# ---------------------------------------------------------------------------
# 允许清单环境面（与 http_fetch 的差异：未设置 = 默认清单；空串 = 禁用）
# ---------------------------------------------------------------------------


def _allowlist_from_env() -> list[str]:
    """从环境变量读取允许清单（每次调用现读——部署语义等价构造期快照）。

    Returns:
        归一化允许清单（``BILI_API_ALLOWLIST`` 未设置 = 默认清单
        ``api.bilibili.com,app.bilibili.com``；空串 / 全空项 = 空列表——
        显式禁用语义）。
    """
    raw = os.environ.get(_ALLOWLIST_ENV)
    if raw is None:
        return parse_allowlist(_DEFAULT_ALLOWLIST_RAW)
    return parse_allowlist(raw)


# ---------------------------------------------------------------------------
# 工厂（依赖构造注入——平台工具接入范式；执行器恒不抛未受控异常）
# ---------------------------------------------------------------------------


def register_bili_tools(
    runtime: SandboxToolRuntime,
    *,
    allowlist_provider: Callable[[], list[str]] | None = None,
    fetch: FetchFn | None = None,
    clock: Callable[[], float] | None = None,
    sleep: Callable[[float], None] | None = None,
    nav_fetch: NavFetchFn | None = None,
) -> None:
    """注册全部 B 站只读工具（依赖构造注入——限速 / 冷却 / WBI 键进程内共享）。

    全部外部面可注入（HTTP 抓取 / 时钟 / sleep / nav 键获取 / 允许清单
    ——测试 hermetic 面）；共享核心经单次构造持有进程内状态（限速水位 /
    硬冷却 / WBI 键缓存）。执行器经 ``asyncio.to_thread`` 下沉阻塞面
    （stdlib urllib + sleep），对运行时**恒不抛未受控异常**。

    Args:
        runtime: 沙盒工具运行时（四工具注册目标——同名后写覆盖前写）。
        allowlist_provider: 允许清单读取注入（None = 环境变量
            ``BILI_API_ALLOWLIST``——未设置默认清单 / 空串禁用）。
        fetch: HTTP 抓取注入（None = :func:`default_bili_fetch`——stdlib
            urllib 不走系统代理）。
        clock: 时钟注入（epoch 秒；None = ``time.time``）。
        sleep: 阻塞等待注入（None = ``time.sleep``——限速间隔面）。
        nav_fetch: nav 键获取注入（None = :func:`default_nav_fetch`——
            WBI 签名键源）。
    """
    core = _BiliClientCore(
        fetch=fetch if fetch is not None else default_bili_fetch,
        clock=clock if clock is not None else time.time,
        sleep=sleep if sleep is not None else time.sleep,
        nav_fetch=nav_fetch if nav_fetch is not None else default_nav_fetch,
        allowlist_provider=allowlist_provider if allowlist_provider is not None else _allowlist_from_env,
    )

    async def _execute_search(args: dict[str, Any]) -> dict[str, Any]:
        """执行搜索（线程池下沉——限速 / 冷却 / WBI 签名均在核心内）。"""
        return await asyncio.to_thread(
            core.search_videos,
            keyword=str(args["keyword"]),
            page=int(args["page"]),
            page_size=int(args["page_size"]),
        )

    async def _execute_info(args: dict[str, Any]) -> dict[str, Any]:
        """执行详情（线程池下沉）。"""
        return await asyncio.to_thread(core.video_info, bvid=str(args["bvid"]))

    async def _execute_comments(args: dict[str, Any]) -> dict[str, Any]:
        """执行评论（线程池下沉——view→aid 换算 + 游标分页在核心内）。"""
        return await asyncio.to_thread(
            core.video_comments,
            bvid=str(args["bvid"]),
            page=int(args["page"]),
            page_size=int(args["page_size"]),
        )

    async def _execute_subtitle(args: dict[str, Any]) -> dict[str, Any]:
        """执行字幕（线程池下沉——CDN 抓取后缀清单管制在核心内）。"""
        return await asyncio.to_thread(
            core.video_subtitle,
            bvid=str(args["bvid"]),
            fetch_subtitle=bool(args["fetch_subtitle"]),
        )

    runtime.register(
        SandboxToolSpec(
            name=BILI_SEARCH_VIDEOS_TOOL_NAME,
            description="搜索 Bilibili 视频（公开只读接口；关键词 + 分页；返回白名单裁剪字段）。",
            args_model=BiliSearchVideosArgsModel,
        ),
        _execute_search,
    )
    runtime.register(
        SandboxToolSpec(
            name=BILI_VIDEO_INFO_TOOL_NAME,
            description="获取 Bilibili 视频详情（公开只读接口；标题 / 简介 / UP 主 / 时长 / stat 子集 / 首 P cid）。",
            args_model=BiliVideoInfoArgsModel,
        ),
        _execute_info,
    )
    runtime.register(
        SandboxToolSpec(
            name=BILI_VIDEO_COMMENTS_TOOL_NAME,
            description="获取 Bilibili 视频评论（公开只读接口；时间序游标分页；返回白名单裁剪扁平列表）。",
            args_model=BiliVideoCommentsArgsModel,
        ),
        _execute_comments,
    )
    runtime.register(
        SandboxToolSpec(
            name=BILI_VIDEO_SUBTITLE_TOOL_NAME,
            description="获取 Bilibili 视频字幕清单（公开只读接口；可选抓取首个字幕正文——CDN 后缀允许清单管制）。",
            args_model=BiliSubtitleArgsModel,
        ),
        _execute_subtitle,
    )

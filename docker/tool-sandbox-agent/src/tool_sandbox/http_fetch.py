"""http_fetch 示例工具——沙盒侧 HTTP 抓取（平台工具的落地范式）。

本模块是「执行器无关工具须隔离运行」的范例实现，平台工具按同一
形态接入（工厂函数 + 参数 Model Class + 受控失败 + 线程池下沉阻塞 I/O）。

安全面（逐条红线——与部署面 compose 加固共同构成沙盒边界）：

    1. **域名允许清单**（env ``SANDBOX_HTTP_ALLOWLIST``，逗号分隔域名后缀）：
       空清单 = 工具禁用（默认拒绝——零配置部署永不产生出站流量）；
       匹配语义为点边界后缀匹配（``api.example.com`` 命中条目
       ``example.com``；``evil-example.com`` **不**命中 ``example.com``）；
    2. **重定向逐跳校验**：30x 跳转的目标主机逐跳过允许清单（自定义
       :class:`urllib.request.HTTPRedirectHandler`——防经重定向跳出清单）；
       终态 URL 复检（belt-and-suspenders）；
    3. **协议面**：仅 http/https；正文按字节截断 8192（8KB），解码
       ``errors="replace"``（字符集取响应头，回退 utf-8——解码恒不抛）；
    4. **不走系统代理**：opener 显式 ``ProxyHandler({})`` 禁用环境代理
       （部署机 ``http_proxy`` 等残留不得改道出站——与 bili_tools 同
       锁定决策：允许清单校验的是目标主机，出站路径须点对点直连）；
    5. **受控失败**：全部失败路径归一 :class:`SandboxToolError`（受控
       中文文案——不携带 token / 内部路径；执行器对运行时**恒不抛
       未受控异常**）。

阻塞 I/O 下沉：stdlib urllib 在线程池执行（``asyncio.to_thread``——
本树零 httpx 依赖，锁定决策）。
"""

from __future__ import annotations

import asyncio
import os
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any
from urllib.parse import urlparse

from pydantic import BaseModel, Field, field_validator

from tool_sandbox.runtime import SandboxToolError, SandboxToolExecutor, SandboxToolSpec

__all__ = [
    "HTTP_FETCH_TOOL_NAME",
    "HttpFetchArgsModel",
    "FetchFn",
    "build_http_fetch_tool",
    "default_fetch",
    "domain_matches_allowlist",
    "parse_allowlist",
    "validate_fetch_url",
]

#: http_fetch 工具名（后端侧 LLM 工具面同名注册——跨树约定标识）。
HTTP_FETCH_TOOL_NAME: str = "http_fetch"

#: 域名允许清单环境变量（容器内注入；空 = 工具禁用）。
_ALLOWLIST_ENV: str = "SANDBOX_HTTP_ALLOWLIST"

#: 响应正文截断上限（字节）。
_MAX_BODY_BYTES: int = 8192

#: 允许的 URL 协议面。
_ALLOWED_SCHEMES: tuple[str, ...] = ("http", "https")

#: 出站请求 User-Agent（标识来源——最小信息面）。
_USER_AGENT: str = "tool-sandbox-agent/0.1.0"

#: 抓取函数契约（url / 超时秒 / 允许清单 → 结果 dict；受控失败抛
#: SandboxToolError——测试注入面同签名）。
FetchFn = Callable[[str, int, list[str]], dict[str, Any]]


class HttpFetchArgsModel(BaseModel):
    """http_fetch 参数面（url + 超时秒——沙盒侧校验锚点）。

    Attributes:
        url: 目标 URL（http/https 且含主机名——协议与主机在字段校验
            层拒绝；域名允许清单在执行层校验）。
        timeout_seconds: 请求超时秒数（1-30，默认 10）。
    """

    model_config = {"extra": "forbid"}

    url: str = Field(min_length=1, max_length=2048, description="目标 URL（http/https）")
    timeout_seconds: int = Field(
        default=10, ge=1, le=30, description="请求超时秒数（1-30，默认 10）"
    )

    @field_validator("url")
    @classmethod
    def _validate_url_scheme(cls, value: str) -> str:
        """校验 URL 协议与主机名（http/https + 主机非空）。

        Args:
            value: 原始 URL 字符串。

        Returns:
            校验通过的原值。

        Raises:
            ValueError: 协议非法或主机名缺失（装配 / 调用参数面）。
        """
        parsed = urlparse(value)
        if parsed.scheme not in _ALLOWED_SCHEMES or not parsed.hostname:
            raise ValueError("url 必须是 http/https 协议且包含主机名")
        return value


def parse_allowlist(raw: str) -> list[str]:
    """解析逗号分隔的域名后缀清单（strip + 小写 + 去首尾点 + 去空项）。

    纯函数（无 I/O）——测试直调面。空串 / 纯分隔符输入返回空列表
    （空清单 = 工具禁用语义）；``.ORG`` 形态的领先点条目归一为
    ``org``（域名后缀语义——用户「*.org」意图）。

    Args:
        raw: 环境变量原始值（如 ``"example.com, api.github.com"``）。

    Returns:
        归一化域名后缀列表（保持输入顺序；全空输入为空列表）。
    """
    entries: list[str] = []
    for piece in raw.split(","):
        entry = piece.strip().lower().strip(".")
        if entry:
            entries.append(entry)
    return entries


def domain_matches_allowlist(host: str, allowlist: list[str]) -> bool:
    """域名后缀匹配（点边界——精确命中或子域命中）。

    纯函数（无 I/O）——测试直调面。匹配规则：

        - ``host == entry``（精确命中）；
        - ``host.endswith("." + entry)``（子域命中——点边界保证
          ``evil-example.com`` 不命中条目 ``example.com``）；
        - 大小写不敏感 + 尾点归一（DNS 语义）。

    Args:
        host: 待匹配主机名。
        allowlist: 允许清单（:func:`parse_allowlist` 归一化产物）。

    Returns:
        是否命中任一条目（空清单恒 False——默认拒绝）。
    """
    normalized = host.lower().rstrip(".")
    return any(normalized == entry or normalized.endswith("." + entry) for entry in allowlist)


def validate_fetch_url(url: str, allowlist: list[str]) -> str:
    """校验目标 URL（协议 / 主机名 / 域名允许清单）。

    Args:
        url: 目标 URL。
        allowlist: 允许清单（空清单由调用方先行禁用检查）。

    Returns:
        归一化主机名（小写、去尾点）。

    Raises:
        SandboxToolError: 协议非法 / 主机名缺失 / 域名不在允许清单
            （受控失败——人读中文文案）。
    """
    parsed = urlparse(url)
    if parsed.scheme not in _ALLOWED_SCHEMES:
        raise SandboxToolError("URL 协议必须是 http/https")
    host = (parsed.hostname or "").lower().rstrip(".")
    if not host:
        raise SandboxToolError("URL 缺少主机名")
    if not domain_matches_allowlist(host, allowlist):
        raise SandboxToolError(f"目标域名不在允许清单: {host}")
    return host


class _AllowlistRedirectHandler(urllib.request.HTTPRedirectHandler):
    """重定向逐跳校验（30x 目标主机过允许清单——防经重定向跳出清单）。

    Attributes:
        _allowlist: 域名允许清单（归一化产物）。
    """

    def __init__(self, allowlist: list[str]) -> None:
        """初始化（携带允许清单快照）。

        Args:
            allowlist: 域名允许清单。
        """
        super().__init__()
        self._allowlist = allowlist

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> urllib.request.Request | None:
        """重定向前校验目标 URL（违规即抛——不发起跳转请求）。

        Args:
            req: 原始请求。
            fp: 上游响应文件对象（透传基类）。
            code: 重定向状态码。
            msg: 状态消息。
            headers: 响应头（透传基类）。
            newurl: 重定向目标 URL（校验面）。

        Returns:
            校验通过后基类构造的新请求（透传 urllib 语义）。

        Raises:
            SandboxToolError: 重定向目标域名不在允许清单。
        """
        validate_fetch_url(newurl, self._allowlist)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def default_fetch(url: str, timeout_seconds: int, allowlist: list[str]) -> dict[str, Any]:
    """stdlib urllib 同步抓取（线程池下沉由执行器 ``to_thread`` 承担）。

    全部失败路径归一 :class:`SandboxToolError`（受控文案）；成功返回
    结果 dict（正文按字节截断 8KB + 字符集解码 ``errors="replace"``）。
    出站不走系统代理：opener 携带 ``ProxyHandler({})`` 显式禁用环境
    代理（``build_opener`` 缺省会拾取 ``http_proxy`` 等 env——与
    bili_tools 同锁定决策，允许清单的信任模型按点对点直连解释）。

    Args:
        url: 目标 URL（首跳校验 + 重定向逐跳校验 + 终态复检）。
        timeout_seconds: 请求超时秒数（连接 / 读取共用）。
        allowlist: 域名允许清单（非空——禁用检查由执行器承担）。

    Returns:
        抓取结果（``url`` 终态 / ``status_code`` / ``content_type`` /
        ``body`` 截断文本 / ``truncated`` 是否截断）。

    Raises:
        SandboxToolError: 首跳或重定向目标被拒 / 非成功 HTTP 状态 /
            传输失败 / 超时 / URL 形态非法（受控失败面）。
    """
    try:
        validate_fetch_url(url, allowlist)
        request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT}, method="GET")
        opener = urllib.request.build_opener(
            _AllowlistRedirectHandler(allowlist), urllib.request.ProxyHandler({})
        )
        with opener.open(request, timeout=timeout_seconds) as response:
            final_url = response.geturl()
            validate_fetch_url(final_url, allowlist)
            raw = response.read(_MAX_BODY_BYTES + 1)
            truncated = len(raw) > _MAX_BODY_BYTES
            if truncated:
                raw = raw[:_MAX_BODY_BYTES]
            charset = response.headers.get_content_charset() or "utf-8"
            try:
                body = raw.decode(charset, errors="replace")
            except LookupError:
                # 未知字符集（响应头声明了不存在的编码）——回退 utf-8 容错解码。
                body = raw.decode("utf-8", errors="replace")
            return {
                "url": final_url,
                "status_code": response.status,
                "content_type": response.headers.get("Content-Type"),
                "body": body,
                "truncated": truncated,
            }
    except SandboxToolError:
        # 受控失败原样上抛（已归一文案）。
        raise
    except urllib.error.HTTPError as exc:
        raise SandboxToolError(f"目标返回非成功状态: HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise SandboxToolError(f"请求失败: {exc.reason}") from exc
    except TimeoutError as exc:
        raise SandboxToolError(f"请求超时（{timeout_seconds}s 上限）") from exc
    except ValueError as exc:
        raise SandboxToolError("URL 形态非法（urllib 拒绝）") from exc
    except Exception as exc:  # 兜底归一（受控失败——执行器对运行时恒不抛未受控异常）
        raise SandboxToolError(f"抓取失败: {type(exc).__name__}") from exc


def _allowlist_from_env() -> list[str]:
    """从环境变量读取允许清单（每次调用现读——部署语义等价构造期快照）。

    Returns:
        归一化允许清单（变量缺失 / 空 = 空列表——工具禁用语义）。
    """
    return parse_allowlist(os.environ.get(_ALLOWLIST_ENV, ""))


def build_http_fetch_tool(
    *,
    allowlist_provider: Callable[[], list[str]] | None = None,
    fetch_fn: FetchFn | None = None,
) -> tuple[SandboxToolSpec, SandboxToolExecutor]:
    """构建 http_fetch 工具（依赖构造注入——平台工具的接入范式）。

    Args:
        allowlist_provider: 允许清单读取注入（None = 环境变量
            ``SANDBOX_HTTP_ALLOWLIST``——测试可控）。
        fetch_fn: 抓取函数注入（None = :func:`default_fetch`——stdlib
            urllib；测试注入 fake 函数实现 hermetic）。

    Returns:
        ``(spec, executor)`` 二元组（经 ``SandboxToolRuntime.register`` 装配）。
    """
    provider = allowlist_provider if allowlist_provider is not None else _allowlist_from_env
    fetch = fetch_fn if fetch_fn is not None else default_fetch

    async def _execute(args: dict[str, Any]) -> dict[str, Any]:
        """执行（读允许清单 → 禁用检查 → 线程池下沉抓取）。"""
        allowlist = provider()
        if not allowlist:
            raise SandboxToolError("http_fetch 未启用：SANDBOX_HTTP_ALLOWLIST 未配置（空 = 默认拒绝）")
        return await asyncio.to_thread(
            fetch, str(args["url"]), int(args["timeout_seconds"]), allowlist
        )

    spec = SandboxToolSpec(
        name=HTTP_FETCH_TOOL_NAME,
        description="抓取 HTTP/HTTPS 页面文本（域名允许清单管制；正文按字节截断 8KB）。",
        args_model=HttpFetchArgsModel,
    )
    return spec, _execute

"""浏览器 sidecar 导航 / 子资源策略——纯函数面（零 I/O，测试直调）。

策略链（navigate 工具的锁定顺序——浏览器层 :func:`browser_sidecar.browser`
按本顺序组装；本模块承担其中全部纯判定步）::

    ① 协议面    :func:`url_scheme_is_allowed`（仅 http/https——``about:`` /
                 ``javascript:`` / ``file:`` 等一律拒绝）
    ② 主机提取  :func:`extract_url_host`（小写 + 去尾点归一）
    ③ 私网拒绝  IP 字面量：:func:`ip_literal_host_is_private`（loopback /
                 RFC1918 / link-local 等——**无论是否在允许清单**均拒绝）；
                 域名：浏览器层先 DNS 解析（I/O——注入面），解析产物逐 IP 过
                 :func:`ip_text_is_private`（任一私网即拒绝——DNS rebinding
                 对策：解析先行、按主机名导航）
    ④ 允许清单  :func:`require_host_allowed`（env ``BROWSER_SIDECAR_ALLOWLIST``
                 逗号分隔域名后缀；**空清单 = navigate 拒绝一切目标**——与
                 tool-sandbox http_fetch 同默认拒绝哲学：零配置部署永不产生
                 出站流量）
    ⑤ 路由拦截  :func:`request_host_allowed`（context 创建期 ``page.route``
                 逐请求判定——子资源与重定向逐跳的强制面；子资源被拒即
                 静默 abort，页面仍渲染允许清单内来源的内容）

子资源面权衡（文档化决策——compose 注释同源）：路由拦截按**请求宿主**
判定，跨域 CDN（页面引的第三方脚本 / 字体 / 图片）默认全部阻断——
需要跨域资源的站点由运维向允许清单追加后缀。文档化取舍：默认面
宁可渲染残缺，不放行未知宿主。

失败面：全部纯判定步拒绝时抛 :class:`PolicyViolationError`（``ValueError``
子类——受控中文文案）；浏览器层捕获后归一为工具受控失败
（:class:`browser_sidecar.runtime.BrowserSidecarError`），对运行时恒不抛
未受控异常。
"""

from __future__ import annotations

import ipaddress
from urllib.parse import urlparse

__all__ = [
    "PolicyViolationError",
    "parse_allowlist",
    "domain_matches_allowlist",
    "url_scheme_is_allowed",
    "extract_url_host",
    "parse_ip_literal",
    "ip_literal_host_is_private",
    "ip_text_is_private",
    "require_host_allowed",
    "request_host_allowed",
]

#: 允许的 URL 协议面（导航与子资源共用——``about:`` / ``javascript:`` /
#: ``file:`` / ``data:`` 等一律拒绝）。
_ALLOWED_SCHEMES: tuple[str, ...] = ("http", "https")


class PolicyViolationError(ValueError):
    """策略违规（纯策略层受控拒绝——中文文案；浏览器层转译为工具受控失败）。

    Attributes:
        message: 人读拒绝原因（受控文案——不携带 token / 内部路径）。
    """

    def __init__(self, message: str) -> None:
        """初始化策略违规。

        Args:
            message: 人读拒绝原因（受控文案）。
        """
        super().__init__(message)
        self.message = message


def parse_allowlist(raw: str) -> list[str]:
    """解析逗号分隔的域名后缀清单（strip + 小写 + 去首尾点 + 去空项）。

    纯函数（无 I/O）——测试直调面。空串 / 纯分隔符输入返回空列表
    （空清单 = navigate 拒绝一切语义）；``.ORG`` 形态的领先点条目归一为
    ``org``（域名后缀语义——用户「*.org」意图）。

    Args:
        raw: 环境变量原始值（如 ``"example.com, cdn.example.org"``）。

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
        host: 待匹配主机名（已归一或原始均可——内部归一）。
        allowlist: 允许清单（:func:`parse_allowlist` 归一化产物）。

    Returns:
        是否命中任一条目（空清单恒 False——默认拒绝）。
    """
    normalized = host.lower().rstrip(".")
    return any(normalized == entry or normalized.endswith("." + entry) for entry in allowlist)


def url_scheme_is_allowed(url: str) -> bool:
    """校验 URL 协议面（仅 http/https——纯函数）。

    Args:
        url: 目标 URL。

    Returns:
        协议是否在允许面（无协议 / ``about:`` / ``javascript:`` / ``file:`` /
        ``data:`` 等均 False）。
    """
    return urlparse(url).scheme in _ALLOWED_SCHEMES


def extract_url_host(url: str) -> str:
    """提取并归一 URL 主机名（小写 + 去尾点——纯函数）。

    Args:
        url: 目标 URL。

    Returns:
        归一主机名（如 ``Example.COM.`` → ``example.com``）。

    Raises:
        PolicyViolationError: 主机名缺失（URL 形态非法面）。
    """
    host = (urlparse(url).hostname or "").lower().rstrip(".")
    if not host:
        raise PolicyViolationError("URL 缺少主机名")
    return host


def parse_ip_literal(
    host: str,
) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """解析主机名的 IP 字面量形态（纯函数）。

    Args:
        host: 归一主机名（域名或 IP 字面量——IPv4 点分 / IPv6 冒分及
            ``[::1]`` 括号形态）。

    Returns:
        解析出的 IP 地址对象；非字面量（域名）/ 非法形态为 None。
    """
    candidate = host.strip("[]")
    try:
        return ipaddress.ip_address(candidate)
    except ValueError:
        return None


def ip_literal_host_is_private(host: str) -> bool:
    """判定主机名是否为**私网 IP 字面量**（纯函数）。

    私网语义（fail-closed 合集）：loopback（127.0.0.0/8 与 ::1）、
    RFC1918（10/8、172.16/12、192.168/16）、link-local（169.254/16 与
    fe80::/10）、唯一本地（fc00::/7）、保留 / 未指定 / 组播段——
    即 stdlib ``ipaddress`` 的私网属性并集。**无论是否在允许清单**，
    私网字面量一律拒绝（部署面 SSRF 红线）。

    Args:
        host: 归一主机名。

    Returns:
        是否为私网 IP 字面量（域名 / 公网字面量为 False）。
    """
    literal = parse_ip_literal(host)
    if literal is None:
        return False
    return _address_is_private(literal)


def ip_text_is_private(ip_text: str) -> bool:
    """判定 DNS 解析产物 IP 文本是否私网（纯函数——fail-closed）。

    Args:
        ip_text: 解析产物 IP 文本（如 ``"10.0.0.5"``）。

    Returns:
        是否私网（不可解析的非法文本**按私网处理**——fail-closed：解析器
        产出的垃圾值不得穿透私网拒绝面）。
    """
    try:
        address = ipaddress.ip_address(ip_text.strip("[]"))
    except ValueError:
        return True
    return _address_is_private(address)


def require_host_allowed(host: str, allowlist: list[str]) -> None:
    """允许清单强制（空清单 = 拒绝一切——纯函数）。

    Args:
        host: 归一主机名（导航目标或子资源宿主）。
        allowlist: 允许清单（:func:`parse_allowlist` 归一化产物）。

    Raises:
        PolicyViolationError: 清单为空（默认拒绝）或主机不命中任何条目。
    """
    if not allowlist:
        raise PolicyViolationError(
            "浏览器导航未启用：BROWSER_SIDECAR_ALLOWLIST 未配置（空 = 默认拒绝）"
        )
    if not domain_matches_allowlist(host, allowlist):
        raise PolicyViolationError(f"目标域名不在允许清单: {host}")


def request_host_allowed(url: str, allowlist: list[str]) -> bool:
    """路由拦截判定（子资源 / 重定向逐跳——纯函数）。

    判定面：协议 http/https + 宿主命中允许清单 + 宿主非私网 IP 字面量。
    空清单恒 False（拦截面与 navigate 的默认拒绝语义一致——belt and
    suspenders：navigate 已在空清单下先行拒绝，拦截面独立复检）。

    Args:
        url: 请求 URL（子资源 / 重定向跳转目标 / 主文档）。
        allowlist: 允许清单（归一化产物）。

    Returns:
        是否放行（False = 路由层 abort——子资源静默死亡）。
    """
    if not url_scheme_is_allowed(url):
        return False
    try:
        host = extract_url_host(url)
    except PolicyViolationError:
        return False
    if ip_literal_host_is_private(host):
        return False
    return domain_matches_allowlist(host, allowlist)


def _address_is_private(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """IP 地址私网属性并集判定（内部纯函数）。

    Args:
        address: 解析出的 IP 地址（v4 / v6）。

    Returns:
        是否落入任一私网 / 保留语义段（``is_private`` 已涵盖 loopback /
        link-local / RFC1918 / ULA；``is_reserved`` / ``is_multicast`` /
        ``is_unspecified`` 并集收紧——fail-closed）。
    """
    return bool(
        address.is_private
        or address.is_reserved
        or address.is_multicast
        or address.is_unspecified
    )

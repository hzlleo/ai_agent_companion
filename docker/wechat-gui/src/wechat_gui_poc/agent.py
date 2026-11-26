"""GUI 微信 agent——仅回环的 HTTP 工具面（后端通道 ↔ 沙盒执行面）。

职责：把后端通道需要的五个 UI 原语暴露为 ``POST /invoke`` 工具调用，并在
本机回环上提供 ``GET /health`` 存活探测。工具面：

    ==============  ==============================================
    工具            行为
    ==============  ==============================================
    screenshot      抓根窗口 PNG，返回 base64 与 sha256
    click           绝对坐标移动 + 左键点击（经 uinputd 控制面）
    type_text       文本经剪贴板写入后 Ctrl+V 粘贴（覆盖非 ASCII）
    press_key       按键 / 组合键（Enter / Esc / ctrl+f 等）
    open_chat       搜索并打开联系人会话，返回标题区域与全屏截图
    ==============  ==============================================

传输与鉴权：服务只监听回环地址；``POST /invoke`` 要求 Bearer token（与
``WECHAT_GUI_AGENT_TOKEN`` / token 文件内容一致，恒时比较）。工具级失败
（未知工具 / 参数非法 / 执行失败）恒以 HTTP 200 携带 ``ok=false`` 终态；
401（鉴权失败）与 422（请求形态非法）属传输层失败。凭据经文件挂载，
镜像内不含明文。

注入归属：所有键鼠事件经 uinputd 的 Unix socket 控制面（JSON 行协议）转发
——设备生命周期由守护进程持有，本进程不直接打开事件节点。截图与剪贴板
走 ImageMagick / xclip 子进程（镜像已含），不引入第三方 Python 依赖。

本模块只用标准库；后端侧的 wire 契约镜像见
``channels/wechat_gui/wechat_gui_wire_models.py``。
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import logging
import os
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Protocol

from .uinput_devices import (
    BTN_LEFT,
    KEY_BACKSPACE,
    KEY_DELETE,
    KEY_DOWN,
    KEY_ENTER,
    KEY_ESC,
    KEY_LEFT,
    KEY_LEFTALT,
    KEY_LEFTCTRL,
    KEY_LEFTSHIFT,
    KEY_RIGHT,
    KEY_SPACE,
    KEY_TAB,
    KEY_UP,
)
from .uinputd import SyncUinputControlClient
from .wire_models import (
    CLICK_TOOL,
    DEFAULT_AGENT_HOST,
    DEFAULT_AGENT_PORT,
    DEFAULT_HEIGHT,
    DEFAULT_SOCKET_PATH,
    DEFAULT_WIDTH,
    MAX_AGENT_REQUEST_BYTES,
    OPEN_CHAT_TOOL,
    PRESS_KEY_TOOL,
    SCREENSHOT_TOOL,
    TITLE_REGION_HEIGHT,
    TYPE_TEXT_TOOL,
    WECHAT_GUI_HEALTH_PATH,
    WECHAT_GUI_INVOKE_PATH,
    ClickRequest,
    ClickToolArgs,
    CommandOutcome,
    ControlRequest,
    HealthResponseModel,
    KeyRequest,
    MoveRequest,
    OpenChatToolArgs,
    PressKeyToolArgs,
    ScreenshotToolArgs,
    TypeTextToolArgs,
)

#: token 缺省文件路径（compose secret 挂载点）。
DEFAULT_AGENT_TOKEN_FILE = Path("/run/secrets/wechat_gui_agent_token")
#: token 文件路径环境变量名。
AGENT_TOKEN_FILE_ENV = "WECHAT_GUI_AGENT_TOKEN_FILE"
#: token 明文环境变量名（优先级高于文件——开发期覆盖形态）。
AGENT_TOKEN_ENV = "WECHAT_GUI_AGENT_TOKEN"

#: 模块级 logger（stdlib logging——本树最小依赖约定）。
logger = logging.getLogger(__name__)


class AgentToolError(RuntimeError):
    """工具执行失败（归一为 ``ok=false`` 载荷终态）。"""


class ControlPlane(Protocol):
    """键鼠注入面协议（生产实现为同步 uinputd 客户端；测试注入 fake）。"""

    def send(self, request: ControlRequest) -> CommandOutcome:
        """发送一条控制面请求。

        Args:
            request: 控制面请求模型。

        Returns:
            控制面响应。
        """
        ...


class ScreenshotCapturer(Protocol):
    """截图面协议（生产实现调用 ImageMagick；测试注入 fake）。"""

    def capture(self, *, crop: tuple[int, int] | None = None) -> bytes:
        """抓取根窗口 PNG。

        Args:
            crop: 可选的标题区域尺寸（宽, 高）；None 表示整屏。

        Returns:
            PNG 字节。

        Raises:
            AgentToolError: 抓图失败或输出为空。
        """
        ...


class ClipboardWriter(Protocol):
    """剪贴板写入面协议（生产实现调用 xclip；测试注入 fake）。"""

    def write(self, text: str) -> None:
        """把文本写入 X 剪贴板。

        Args:
            text: 待写入文本。

        Raises:
            AgentToolError: 写入失败。
        """
        ...


@dataclass(frozen=True)
class ImagemagickScreenshot:
    """ImageMagick 截图实现（``import -window root png:-``）。

    Attributes:
        display: X 显示号。
        timeout: 单次子进程超时秒数。
    """

    display: str
    timeout: float = 10.0

    def capture(self, *, crop: tuple[int, int] | None = None) -> bytes:
        """抓取根窗口 PNG（可选按左上角区域裁剪）。

        Args:
            crop: 标题区域尺寸（宽, 高）；None 表示整屏。

        Returns:
            PNG 字节。

        Raises:
            AgentToolError: 子进程非零退出 / 超时 / 输出为空。
        """
        argv = ["import", "-display", self.display, "-window", "root"]
        if crop is not None:
            argv.extend(["-crop", f"{crop[0]}x{crop[1]}+0+0"])
        argv.append("png:-")
        env = {**os.environ, "DISPLAY": self.display}
        try:
            result = subprocess.run(
                argv,
                capture_output=True,
                timeout=self.timeout,
                env=env,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise AgentToolError(f"截图命令失败：{exc}") from exc
        if result.returncode != 0:
            detail = result.stderr.decode("utf-8", errors="replace").strip()[-300:]
            raise AgentToolError(f"截图命令非零退出（{result.returncode}）：{detail}")
        if not result.stdout:
            raise AgentToolError("截图命令输出为空")
        return result.stdout


@dataclass(frozen=True)
class XclipClipboard:
    """xclip 剪贴板实现（写入后由 xclip 常驻持有 selection）。

    Attributes:
        display: X 显示号。
        timeout: 单次子进程超时秒数。
    """

    display: str
    timeout: float = 10.0

    def write(self, text: str) -> None:
        """把文本写入 X 剪贴板。

        标准输出 / 错误接 ``/dev/null``：xclip 会 fork 常驻的 selection
        owner，继承管道会让父进程等待其退出。

        Args:
            text: 待写入文本。

        Raises:
            AgentToolError: 子进程启动失败 / 超时 / 非零退出。
        """
        env = {**os.environ, "DISPLAY": self.display}
        try:
            result = subprocess.run(
                ["xclip", "-selection", "clipboard", "-in"],
                input=text.encode("utf-8"),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=self.timeout,
                env=env,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise AgentToolError(f"剪贴板写入失败：{exc}") from exc
        if result.returncode != 0:
            raise AgentToolError(f"剪贴板写入非零退出（{result.returncode}）")


#: 具名按键码（press_key 的键名面——大小写不敏感）。
_KEY_NAME_CODES: dict[str, int] = {
    "enter": KEY_ENTER,
    "return": KEY_ENTER,
    "esc": KEY_ESC,
    "escape": KEY_ESC,
    "tab": KEY_TAB,
    "space": KEY_SPACE,
    "backspace": KEY_BACKSPACE,
    "delete": KEY_DELETE,
    "up": KEY_UP,
    "down": KEY_DOWN,
    "left": KEY_LEFT,
    "right": KEY_RIGHT,
}

#: 修饰键名到按键码的映射（组合键语法 ``ctrl+f``）。
_MODIFIER_CODES: dict[str, int] = {
    "ctrl": KEY_LEFTCTRL,
    "control": KEY_LEFTCTRL,
    "shift": KEY_LEFTSHIFT,
    "alt": KEY_LEFTALT,
}

#: 字母键码（Linux input-event-codes 按物理键位编号，字母非字母序连续）。
_LETTER_CODES: dict[str, int] = {
    "q": 16,
    "w": 17,
    "e": 18,
    "r": 19,
    "t": 20,
    "y": 21,
    "u": 22,
    "i": 23,
    "o": 24,
    "p": 25,
    "a": 30,
    "s": 31,
    "d": 32,
    "f": 33,
    "g": 34,
    "h": 35,
    "j": 36,
    "k": 37,
    "l": 38,
    "z": 44,
    "x": 45,
    "c": 46,
    "v": 47,
    "b": 48,
    "n": 49,
    "m": 50,
}
#: 数字键码（1-9 连续，0 在 11）。
_DIGIT_CODES: dict[str, int] = {
    "1": 2,
    "2": 3,
    "3": 4,
    "4": 5,
    "5": 6,
    "6": 7,
    "7": 8,
    "8": 9,
    "9": 10,
    "0": 11,
}


def _key_code(token: str) -> int:
    """把单个键名解析为 Linux 按键码。

    Args:
        token: 键名（具名键 / 单个字母 / 单个数字）。

    Returns:
        按键码。

    Raises:
        ValueError: 不支持的键名。
    """
    lowered = token.strip().lower()
    if lowered in _KEY_NAME_CODES:
        return _KEY_NAME_CODES[lowered]
    if lowered in _LETTER_CODES:
        return _LETTER_CODES[lowered]
    if lowered in _DIGIT_CODES:
        return _DIGIT_CODES[lowered]
    raise ValueError(f"不支持的按键：{token!r}")


def parse_key_expression(expression: str) -> tuple[list[int], int]:
    """解析按键 / 组合键表达式。

    Args:
        expression: 形如 ``enter`` / ``ctrl+f`` 的表达式（``+`` 分隔）。

    Returns:
        ``(修饰键码列表, 主键码)`` 二元组（修饰键按书写顺序按下）。

    Raises:
        ValueError: 空表达式 / 修饰键重复 / 主键本身是修饰键 / 键名未知。
    """
    tokens = [token.strip().lower() for token in expression.split("+") if token.strip()]
    if not tokens:
        raise ValueError("按键表达式不得为空")
    modifiers: list[int] = []
    for token in tokens[:-1]:
        if token not in _MODIFIER_CODES:
            raise ValueError(f"组合键前缀必须是修饰键：{token!r}")
        code = _MODIFIER_CODES[token]
        if code in modifiers:
            raise ValueError(f"修饰键重复：{token!r}")
        modifiers.append(code)
    if tokens[-1] in _MODIFIER_CODES:
        raise ValueError(f"组合键缺少主键：{expression!r}")
    return modifiers, _key_code(tokens[-1])


@dataclass
class AgentRuntime:
    """五工具执行面（注入控制面 / 截图 / 剪贴板——测试可全量替身）。

    Attributes:
        control: 键鼠注入面（生产 = 同步 uinputd 客户端）。
        screenshots: 截图面（生产 = ImageMagick 子进程）。
        clipboard: 剪贴板写入面（生产 = xclip 子进程）。
        width: 屏幕宽度（像素——结果元数据）。
        height: 屏幕高度（像素）。
        title_height: 标题区域截图高度（像素）。
        open_chat_timeout_seconds: 打开会话后的标题变化等待上限（秒）。
        settle_seconds: 粘贴 / 按键后的稳定等待（秒）。
        sleep: 休眠注入（测试用记录式 fake——零真实等待）。
        monotonic: 单调时钟注入（测试控制有界等待）。
        now_ms: 当前 epoch 毫秒注入（时延统计）。
    """

    control: ControlPlane
    screenshots: ScreenshotCapturer
    clipboard: ClipboardWriter
    width: int = DEFAULT_WIDTH
    height: int = DEFAULT_HEIGHT
    title_height: int = TITLE_REGION_HEIGHT
    open_chat_timeout_seconds: float = 5.0
    settle_seconds: float = 0.05
    sleep: Callable[[float], None] = time.sleep
    monotonic: Callable[[], float] = time.monotonic
    now_ms: Callable[[], int] = field(
        default=lambda: time.time_ns() // 1_000_000
    )

    def execute(self, tool: str, arguments: Mapping[str, object]) -> dict[str, object]:
        """执行一次工具调用（失败抛 :class:`AgentToolError`）。

        Args:
            tool: 工具名。
            arguments: 调用参数对象。

        Returns:
            结果 JSON 对象。

        Raises:
            AgentToolError: 未知工具 / 参数非法 / 执行失败。
        """
        if tool == SCREENSHOT_TOOL:
            return self._screenshot(ScreenshotToolArgs.from_arguments(arguments))
        if tool == CLICK_TOOL:
            return self._click(ClickToolArgs.from_arguments(arguments))
        if tool == TYPE_TEXT_TOOL:
            return self._type_text(TypeTextToolArgs.from_arguments(arguments))
        if tool == PRESS_KEY_TOOL:
            return self._press_key(PressKeyToolArgs.from_arguments(arguments))
        if tool == OPEN_CHAT_TOOL:
            return self._open_chat(OpenChatToolArgs.from_arguments(arguments))
        raise AgentToolError(f"未知工具：{tool}")

    # ------------------------------------------------------------------
    # 工具实现
    # ------------------------------------------------------------------

    def _screenshot(self, args: ScreenshotToolArgs) -> dict[str, object]:
        """抓取整屏并编码。"""
        png = self.screenshots.capture()
        return {
            "image_base64": base64.b64encode(png).decode("ascii"),
            "sha256": hashlib.sha256(png).hexdigest(),
            "width": self.width,
            "height": self.height,
        }

    def _click(self, args: ClickToolArgs) -> dict[str, object]:
        """移动到绝对坐标并左键点击。"""
        self._require_ok(self.control.send(MoveRequest(x=args.x, y=args.y)), "指针移动")
        self._require_ok(self.control.send(ClickRequest(button=BTN_LEFT)), "鼠标点击")
        return {"x": args.x, "y": args.y}

    def _type_text(self, args: TypeTextToolArgs) -> dict[str, object]:
        """把文本写入剪贴板后 Ctrl+V 粘贴（覆盖中文等非 ASCII 输入）。"""
        self.clipboard.write(args.text)
        self._press_combination(["ctrl", "v"])
        self.sleep(self.settle_seconds)
        return {"chars": len(args.text)}

    def _press_key(self, args: PressKeyToolArgs) -> dict[str, object]:
        """解析并注入按键 / 组合键。"""
        modifiers, code = parse_key_expression(args.key)
        self._emit_sequence(modifiers, code)
        self.sleep(self.settle_seconds)
        return {"key": args.key}

    def _open_chat(self, args: OpenChatToolArgs) -> dict[str, object]:
        """Ctrl+F 搜索备注名并打开会话，返回标题与全屏截图。"""
        baseline = self.screenshots.capture(crop=(self.width, self.title_height))
        baseline_sha = hashlib.sha256(baseline).hexdigest()
        self._press_combination(["ctrl", "f"])
        self.clipboard.write(args.remark_name)
        self._press_combination(["ctrl", "v"])
        self.sleep(self.settle_seconds)
        self._press_combination(["enter"])
        title_png = self._await_title_change(baseline_sha)
        full_png = self.screenshots.capture()
        return {
            "remark_name": args.remark_name,
            "title_image_base64": base64.b64encode(title_png).decode("ascii"),
            "title_sha256": hashlib.sha256(title_png).hexdigest(),
            "full_image_base64": base64.b64encode(full_png).decode("ascii"),
            "full_sha256": hashlib.sha256(full_png).hexdigest(),
        }

    # ------------------------------------------------------------------
    # 注入助手
    # ------------------------------------------------------------------

    def _await_title_change(self, baseline_sha: str) -> bytes:
        """有界等待标题区域画面变化并返回最新标题截图。

        Args:
            baseline_sha: 搜索前的标题区域 sha256。

        Returns:
            最新标题区域 PNG（截止仍未变化时返回最后一次抓取结果）。
        """
        deadline = self.monotonic() + max(self.open_chat_timeout_seconds, 0.0)
        title_png = self.screenshots.capture(crop=(self.width, self.title_height))
        while hashlib.sha256(title_png).hexdigest() == baseline_sha:
            if self.monotonic() >= deadline:
                break
            self.sleep(self.settle_seconds)
            title_png = self.screenshots.capture(crop=(self.width, self.title_height))
        return title_png

    def _press_combination(self, tokens: list[str]) -> None:
        """按 ``press_key`` 语法注入一组按键（修饰键 + 主键）。

        Args:
            tokens: 表达式分段（如 ``["ctrl", "f"]``）。

        Raises:
            AgentToolError: 表达式非法或注入失败。
        """
        expression = "+".join(tokens)
        try:
            modifiers, code = parse_key_expression(expression)
        except ValueError as exc:
            raise AgentToolError(str(exc)) from exc
        self._emit_sequence(modifiers, code)

    def _emit_sequence(self, modifiers: list[int], code: int) -> None:
        """按序注入修饰键按下 → 主键按下 / 抬起 → 修饰键抬起。

        Args:
            modifiers: 修饰键码列表（书写顺序）。
            code: 主键码。

        Raises:
            AgentToolError: 任一按键注入失败。
        """
        for modifier in modifiers:
            self._require_ok(
                self.control.send(KeyRequest(code=modifier, pressed=True)), "修饰键按下"
            )
        try:
            self._require_ok(self.control.send(KeyRequest(code=code, pressed=True)), "按键按下")
            self._require_ok(self.control.send(KeyRequest(code=code, pressed=False)), "按键抬起")
        finally:
            for modifier in reversed(modifiers):
                self._require_ok(
                    self.control.send(KeyRequest(code=modifier, pressed=False)), "修饰键抬起"
                )

    @staticmethod
    def _require_ok(outcome: CommandOutcome, action: str) -> None:
        """断言控制面响应成功。

        Args:
            outcome: 控制面响应。
            action: 动作名（失败文案面）。

        Raises:
            AgentToolError: ``ok=False``。
        """
        if not outcome.ok:
            raise AgentToolError(f"{action}失败：{outcome.error or '控制面未报告原因'}")


# ---------------------------------------------------------------------------
# token 解析与 HTTP 服务
# ---------------------------------------------------------------------------


def resolve_agent_token(
    *,
    token_file: Path = DEFAULT_AGENT_TOKEN_FILE,
    environ: Mapping[str, str] | None = None,
) -> str:
    """解析 Bearer token（明文环境变量优先，其次 token 文件）。

    Args:
        token_file: token 文件路径（compose secret 挂载点）。
        environ: 环境变量映射（None = 进程环境）。

    Returns:
        token 本值（去首尾空白）；两处皆缺失时为空串（服务拒绝启动）。
    """
    env = os.environ if environ is None else environ
    plaintext = env.get(AGENT_TOKEN_ENV, "").strip()
    if plaintext:
        return plaintext
    path = Path(env.get(AGENT_TOKEN_FILE_ENV, str(token_file)) or str(token_file))
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _bearer_token(headers: Any) -> str:
    """从 Authorization 头提取 Bearer token。

    Args:
        headers: 请求头映射。

    Returns:
        token 本值；缺失或形态不符时为空串。
    """
    raw = str(headers.get("Authorization", "")).strip()
    scheme, _, value = raw.partition(" ")
    if scheme.lower() != "bearer":
        return ""
    return value.strip()


def _decode_invoke_request(payload: object) -> tuple[str, dict[str, object]]:
    """校验并解码 invoke 请求载荷。

    Args:
        payload: 已解析的 JSON 载荷。

    Returns:
        ``(tool, arguments)`` 二元组。

    Raises:
        ValueError: 根节点非对象 / tool 缺失或非法 / arguments 非对象 /
            携带未声明的顶层字段。
    """
    if not isinstance(payload, dict):
        raise ValueError("请求根节点必须是对象")
    extra = set(payload) - {"tool", "arguments"}
    if extra:
        raise ValueError(f"请求携带未声明的字段：{sorted(extra)}")
    tool = payload.get("tool")
    if not isinstance(tool, str) or not tool.strip():
        raise ValueError("字段 tool 必须是非空字符串")
    arguments = payload.get("arguments", {})
    if not isinstance(arguments, dict):
        raise ValueError("字段 arguments 必须是对象")
    return tool.strip(), dict(arguments)


def build_handler(runtime: AgentRuntime, token: str) -> type[BaseHTTPRequestHandler]:
    """构造绑定运行时与 token 的请求处理器类。

    Args:
        runtime: 五工具执行面。
        token: Bearer token（恒时比较）。

    Returns:
        BaseHTTPRequestHandler 子类。
    """

    class AgentRequestHandler(BaseHTTPRequestHandler):
        """agent HTTP 请求处理器（仅 /health 与 /invoke）。"""

        def do_GET(self) -> None:
            """处理存活探测。"""
            if self.path != WECHAT_GUI_HEALTH_PATH:
                self._send_json(404, {"detail": "not found"})
                return
            self._send_json(200, HealthResponseModel().to_wire())

        def do_POST(self) -> None:
            """处理工具调用（鉴权 → 体校验 → 执行 → 恒 200 终态）。"""
            if self.path != WECHAT_GUI_INVOKE_PATH:
                self._send_json(404, {"detail": "not found"})
                return
            if not self._authorized():
                self._send_json(401, {"detail": "unauthorized"})
                return
            body = self._read_body()
            if body is None:
                return
            try:
                payload = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._send_json(422, {"detail": "请求体不是合法 JSON"})
                return
            try:
                tool, arguments = _decode_invoke_request(payload)
            except ValueError as exc:
                self._send_json(422, {"detail": str(exc)})
                return
            started_ms = runtime.now_ms()
            try:
                result = runtime.execute(tool, arguments)
            except Exception as exc:  # 工具级失败恒归一为 200 + ok=false
                logger.warning("agent 工具执行失败: tool=%s, error=%s", tool, exc)
                self._send_json(
                    200,
                    {
                        "ok": False,
                        "result": {},
                        "error": str(exc) or type(exc).__name__,
                        "latency_ms": max(0, runtime.now_ms() - started_ms),
                    },
                )
                return
            self._send_json(
                200,
                {
                    "ok": True,
                    "result": result,
                    "latency_ms": max(0, runtime.now_ms() - started_ms),
                },
            )

        def log_message(self, log_format: str, *args: object) -> None:
            """把访问日志降级到 debug（避免每请求 stderr 噪声）。"""
            logger.debug("%s - %s", self.address_string(), log_format % args)

        def _authorized(self) -> bool:
            """恒时比较 Bearer token。"""
            provided = _bearer_token(self.headers)
            return bool(provided) and hmac.compare_digest(provided, token)

        def _read_body(self) -> bytes | None:
            """读取有界请求体（失败时已回写状态码并返回 None）。"""
            raw_length = self.headers.get("Content-Length", "").strip()
            if not raw_length.isdigit():
                self._send_json(422, {"detail": "缺少合法的 Content-Length"})
                return None
            length = int(raw_length)
            if length > MAX_AGENT_REQUEST_BYTES:
                self._send_json(413, {"detail": "请求体超过大小上限"})
                return None
            return self.rfile.read(length)

        def _send_json(self, status: int, payload: Mapping[str, object]) -> None:
            """回写 JSON 响应（固定 Content-Type / Content-Length）。"""
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return AgentRequestHandler


def build_server(
    runtime: AgentRuntime,
    token: str,
    *,
    host: str = DEFAULT_AGENT_HOST,
    port: int = DEFAULT_AGENT_PORT,
) -> ThreadingHTTPServer:
    """构造 HTTP 服务（未启动——调用方决定 serve_forever / 线程）。

    Args:
        runtime: 五工具执行面。
        token: Bearer token。
        host: 监听地址（缺省仅回环）。
        port: 监听端口（0 = 由内核分配——测试形态）。

    Returns:
        ThreadingHTTPServer 实例。
    """
    return ThreadingHTTPServer((host, port), build_handler(runtime, token))


def build_parser() -> argparse.ArgumentParser:
    """构造命令行解析器。

    Returns:
        已配置的解析器。
    """
    parser = argparse.ArgumentParser(description="GUI 微信 agent（仅回环 HTTP 工具面）")
    parser.add_argument("--host", default=DEFAULT_AGENT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_AGENT_PORT)
    parser.add_argument("--token-file", type=Path, default=DEFAULT_AGENT_TOKEN_FILE)
    parser.add_argument("--control-socket", type=Path, default=DEFAULT_SOCKET_PATH)
    parser.add_argument("--display", default=":99")
    parser.add_argument("--width", type=int, default=DEFAULT_WIDTH)
    parser.add_argument("--height", type=int, default=DEFAULT_HEIGHT)
    parser.add_argument("--title-height", type=int, default=TITLE_REGION_HEIGHT)
    parser.add_argument("--operation-timeout", type=float, default=10.0)
    parser.add_argument("--open-chat-timeout", type=float, default=5.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    """命令行入口（token 缺失即拒绝启动）。

    Args:
        argv: 参数列表（None 时为进程参数）。

    Returns:
        进程退出码（0 正常收口；1 缺 token / 端口占用等启动失败）。
    """
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    token = resolve_agent_token(token_file=args.token_file)
    if not token:
        logger.error("agent Bearer token 缺失（检查 %s 或 %s）", args.token_file, AGENT_TOKEN_ENV)
        return 1
    runtime = AgentRuntime(
        control=SyncUinputControlClient(args.control_socket, timeout=args.operation_timeout),
        screenshots=ImagemagickScreenshot(args.display, timeout=args.operation_timeout),
        clipboard=XclipClipboard(args.display, timeout=args.operation_timeout),
        width=args.width,
        height=args.height,
        title_height=args.title_height,
        open_chat_timeout_seconds=args.open_chat_timeout,
    )
    server = build_server(runtime, token, host=args.host, port=args.port)
    logger.info("agent 就绪：http://%s:%d（仅回环）", args.host, server.server_address[1])
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("agent 收到中断，收口")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

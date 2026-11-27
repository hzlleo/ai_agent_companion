"""GUI 微信 agent——仅回环的异步 HTTP 工具面（后端通道 ↔ 沙盒执行面）。

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
``WECHAT_GUI_AGENT_TOKEN`` / token 文件内容一致，恒时比较 UTF-8 字节）。
工具级失败（未知工具 / 参数非法 / 执行失败 / 执行异常）恒以 HTTP 200 携带
``ok=false`` 终态；401（鉴权失败）与 422（请求形态非法）属传输层失败。
凭据经文件挂载，镜像内不含明文。

注入归属：所有键鼠事件经 uinputd 的 Unix socket 控制面（JSON 行协议）转发
——设备生命周期由守护进程持有，本进程不直接打开事件节点。截图与剪贴板
走 ImageMagick / xclip 异步子进程（镜像已含），不引入第三方 Python 依赖。

并发：HTTP 面为 asyncio 单事件循环；每个连接只处理一个请求（响应后关闭，
``Connection: close``）。``/invoke`` 的工具执行由一把进程内锁串行化，
保证单窗口键鼠序列不交错；``/health`` 不占该锁。HTTP 解析为纯函数
（bytes → 请求模型），便于单测矩阵覆盖。

本模块只用标准库；后端侧的 wire 契约镜像见
``channels/wechat_gui/wechat_gui_wire_models.py``。
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import hashlib
import hmac
import json
import logging
import os
import signal
import sys
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from http import HTTPStatus
from pathlib import Path
from typing import Final, Protocol

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
from .uinputd import UinputControlClient
from .wire_models import (
    DEFAULT_AGENT_HOST,
    DEFAULT_AGENT_PORT,
    DEFAULT_HEIGHT,
    DEFAULT_SOCKET_PATH,
    DEFAULT_WIDTH,
    MAX_AGENT_REQUEST_BYTES,
    TITLE_REGION_HEIGHT,
    WECHAT_GUI_HEALTH_PATH,
    WECHAT_GUI_INVOKE_PATH,
    WECHAT_GUI_TOOL_CLICK,
    WECHAT_GUI_TOOL_OPEN_CHAT,
    WECHAT_GUI_TOOL_PRESS_KEY,
    WECHAT_GUI_TOOL_SCREENSHOT,
    WECHAT_GUI_TOOL_TYPE_TEXT,
    ClickRequest,
    ClickResultModel,
    ClickToolArgs,
    CommandOutcome,
    ControlRequest,
    HealthResponseModel,
    InvokeRequestModel,
    InvokeResponseModel,
    KeyRequest,
    MoveRequest,
    OpenChatResultModel,
    OpenChatToolArgs,
    PressKeyResultModel,
    PressKeyToolArgs,
    ScreenshotResultModel,
    ScreenshotToolArgs,
    TypeTextResultModel,
    TypeTextToolArgs,
)

#: token 缺省文件路径（compose secret 挂载点）。
DEFAULT_AGENT_TOKEN_FILE = Path("/run/secrets/wechat_gui_agent_token")
#: token 文件路径环境变量名。
AGENT_TOKEN_FILE_ENV = "WECHAT_GUI_AGENT_TOKEN_FILE"
#: token 明文环境变量名（优先级高于文件——开发期覆盖形态）。
AGENT_TOKEN_ENV = "WECHAT_GUI_AGENT_TOKEN"

#: HTTP 请求头读取上限（字节；超限直接 413，不进入请求体读取）。
_MAX_HTTP_HEADER_BYTES: Final[int] = 65_536
#: 单次连接读请求的截止秒数（避免慢客户端占住事件循环）。
_HTTP_READ_TIMEOUT_SECONDS: Final[float] = 10.0
#: 请求头分隔符。
_HEADER_SEPARATOR: Final[bytes] = b"\r\n\r\n"

#: 模块级 logger（stdlib logging——本树最小依赖约定）。
logger = logging.getLogger(__name__)


class AgentToolError(RuntimeError):
    """工具执行失败（归一为 ``ok=false`` 载荷终态）。"""


class ControlPlane(Protocol):
    """键鼠注入面协议（生产实现为异步 uinputd 客户端；测试注入 fake）。"""

    async def send(self, request: ControlRequest) -> CommandOutcome:
        """发送一条控制面请求。

        Args:
            request: 控制面请求模型。

        Returns:
            控制面响应。
        """
        ...


class ScreenshotCapturer(Protocol):
    """截图面协议（生产实现调用 ImageMagick；测试注入 fake）。"""

    async def capture(self, *, crop: tuple[int, int] | None = None) -> bytes:
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

    async def write(self, text: str) -> None:
        """把文本写入 X 剪贴板。

        Args:
            text: 待写入文本。

        Raises:
            AgentToolError: 写入失败。
        """
        ...


@dataclass(frozen=True)
class ProcessOutput:
    """外部命令执行结果。

    Attributes:
        returncode: 退出码。
        stdout: 标准输出字节。
        stderr: 标准错误字节。
    """

    returncode: int
    stdout: bytes
    stderr: bytes


class ProcessRunner(Protocol):
    """异步子进程执行面协议（生产实现 :func:`run_process`；测试注入 fake）。"""

    async def __call__(
        self,
        argv: Sequence[str],
        *,
        timeout: float,
        env: Mapping[str, str] | None = None,
        input_bytes: bytes | None = None,
        capture_output: bool = True,
    ) -> ProcessOutput:
        """执行外部命令。

        Args:
            argv: 命令与参数列表。
            timeout: 超时秒数。
            env: 子进程环境；None 表示继承。
            input_bytes: 标准输入字节；None 表示关闭标准输入。
            capture_output: 是否采集标准输出 / 错误。

        Returns:
            命令执行结果。
        """
        ...


async def run_process(
    argv: Sequence[str],
    *,
    timeout: float,
    env: Mapping[str, str] | None = None,
    input_bytes: bytes | None = None,
    capture_output: bool = True,
) -> ProcessOutput:
    """执行外部命令（argv 列表、无 shell、有界超时）。

    Args:
        argv: 命令与参数列表。
        timeout: 超时秒数。
        env: 子进程环境；None 表示继承。
        input_bytes: 标准输入字节；None 表示关闭标准输入。
        capture_output: 是否采集标准输出 / 错误。

    Returns:
        命令执行结果。

    Raises:
        AgentToolError: 启动失败或执行超时。
    """
    stdout_target = (
        asyncio.subprocess.PIPE if capture_output else asyncio.subprocess.DEVNULL
    )
    stderr_target = (
        asyncio.subprocess.PIPE if capture_output else asyncio.subprocess.DEVNULL
    )
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE
            if input_bytes is not None
            else asyncio.subprocess.DEVNULL,
            stdout=stdout_target,
            stderr=stderr_target,
            env=dict(env) if env is not None else None,
        )
    except OSError as exc:
        raise AgentToolError(f"无法启动命令 {argv[0]}：{exc}") from exc
    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(input_bytes), timeout=timeout
        )
    except TimeoutError as exc:
        process.kill()
        await process.wait()
        raise AgentToolError(f"命令超时（{timeout:.1f}s）：{argv[0]}") from exc
    return ProcessOutput(
        returncode=process.returncode if process.returncode is not None else -1,
        stdout=stdout if stdout is not None else b"",
        stderr=stderr if stderr is not None else b"",
    )


@dataclass(frozen=True)
class ImagemagickScreenshot:
    """ImageMagick 截图实现（``import -window root png:-``）。

    Attributes:
        display: X 显示号。
        timeout: 单次子进程超时秒数。
        runner: 子进程执行面（测试注入 fake）。
    """

    display: str
    timeout: float = 10.0
    runner: ProcessRunner = run_process

    async def capture(self, *, crop: tuple[int, int] | None = None) -> bytes:
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
        result = await self.runner(
            argv,
            timeout=self.timeout,
            env={**os.environ, "DISPLAY": self.display},
        )
        if result.returncode != 0:
            detail = result.stderr.decode("utf-8", errors="replace").strip()[-300:]
            raise AgentToolError(f"截图命令非零退出（{result.returncode}）：{detail}")
        if not result.stdout:
            raise AgentToolError("截图命令输出为空")
        return result.stdout


@dataclass(frozen=True)
class XclipClipboard:
    """xclip 剪贴板实现（写入后由 xclip 常驻持有 selection）。

    标准输出 / 错误接 ``/dev/null``：xclip 会 fork 常驻的 selection owner，
    继承管道会让父进程等待其退出。粘贴后不清空剪贴板——后续按键可能继续
    复用该内容，清空会与 X selection 的异步所有权竞争。

    Attributes:
        display: X 显示号。
        timeout: 单次子进程超时秒数。
        runner: 子进程执行面（测试注入 fake）。
    """

    display: str
    timeout: float = 10.0
    runner: ProcessRunner = run_process

    async def write(self, text: str) -> None:
        """把文本写入 X 剪贴板。

        Args:
            text: 待写入文本。

        Raises:
            AgentToolError: 子进程启动失败 / 超时 / 非零退出。
        """
        result = await self.runner(
            ["xclip", "-selection", "clipboard", "-in"],
            timeout=self.timeout,
            env={**os.environ, "DISPLAY": self.display},
            input_bytes=text.encode("utf-8"),
            capture_output=False,
        )
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


#: 五工具结果模型的封闭联合。
ToolResultModel = (
    ScreenshotResultModel
    | ClickResultModel
    | TypeTextResultModel
    | PressKeyResultModel
    | OpenChatResultModel
)


@dataclass
class AgentRuntime:
    """五工具执行面（注入控制面 / 截图 / 剪贴板——测试可全量替身）。

    工具执行经 :attr:`lock` 串行化；健康探测不占用该锁。

    Attributes:
        control: 键鼠注入面（生产 = 异步 uinputd 客户端）。
        screenshots: 截图面（生产 = ImageMagick 子进程）。
        clipboard: 剪贴板写入面（生产 = xclip 子进程）。
        width: 屏幕宽度（像素——结果元数据）。
        height: 屏幕高度（像素）。
        title_height: 标题区域截图高度（像素）。
        open_chat_timeout_seconds: 打开会话后的标题变化等待上限（秒）。
        settle_seconds: 粘贴 / 按键后的稳定等待（秒）。
        sleep: 异步休眠注入（测试用记录式 fake——零真实等待）。
        monotonic: 单调时钟注入（测试控制有界等待）。
        now_ms: 当前 epoch 毫秒注入（时延统计）。
        lock: 工具执行串行化锁（每个 /invoke 持锁执行）。
    """

    control: ControlPlane
    screenshots: ScreenshotCapturer
    clipboard: ClipboardWriter
    width: int = DEFAULT_WIDTH
    height: int = DEFAULT_HEIGHT
    title_height: int = TITLE_REGION_HEIGHT
    open_chat_timeout_seconds: float = 5.0
    settle_seconds: float = 0.05
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    monotonic: Callable[[], float] = time.monotonic
    now_ms: Callable[[], int] = field(default=lambda: time.time_ns() // 1_000_000)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def execute(self, tool: str, arguments: Mapping[str, object]) -> ToolResultModel:
        """执行一次工具调用（失败抛 :class:`AgentToolError`）。

        Args:
            tool: 工具名。
            arguments: 调用参数对象。

        Returns:
            工具结果模型。

        Raises:
            AgentToolError: 未知工具 / 参数非法 / 执行失败。
        """
        if tool == WECHAT_GUI_TOOL_SCREENSHOT:
            return await self._screenshot(ScreenshotToolArgs.from_arguments(arguments))
        if tool == WECHAT_GUI_TOOL_CLICK:
            return await self._click(ClickToolArgs.from_arguments(arguments))
        if tool == WECHAT_GUI_TOOL_TYPE_TEXT:
            return await self._type_text(TypeTextToolArgs.from_arguments(arguments))
        if tool == WECHAT_GUI_TOOL_PRESS_KEY:
            return await self._press_key(PressKeyToolArgs.from_arguments(arguments))
        if tool == WECHAT_GUI_TOOL_OPEN_CHAT:
            return await self._open_chat(OpenChatToolArgs.from_arguments(arguments))
        raise AgentToolError(f"未知工具：{tool}")

    async def _screenshot(self, args: ScreenshotToolArgs) -> ScreenshotResultModel:
        """抓取整屏并编码。"""
        png = await self.screenshots.capture()
        return ScreenshotResultModel(
            image_base64=base64.b64encode(png).decode("ascii"),
            sha256=hashlib.sha256(png).hexdigest(),
            width=self.width,
            height=self.height,
        )

    async def _click(self, args: ClickToolArgs) -> ClickResultModel:
        """移动到绝对坐标并左键点击。"""
        self._require_ok(
            await self.control.send(MoveRequest(x=args.x, y=args.y)), "指针移动"
        )
        self._require_ok(await self.control.send(ClickRequest(button=BTN_LEFT)), "鼠标点击")
        return ClickResultModel(x=args.x, y=args.y)

    async def _type_text(self, args: TypeTextToolArgs) -> TypeTextResultModel:
        """把文本写入剪贴板后 Ctrl+V 粘贴（覆盖中文等非 ASCII 输入）。"""
        await self.clipboard.write(args.text)
        await self._press_combination(["ctrl", "v"])
        await self.sleep(self.settle_seconds)
        return TypeTextResultModel(chars=len(args.text))

    async def _press_key(self, args: PressKeyToolArgs) -> PressKeyResultModel:
        """解析并注入按键 / 组合键。"""
        modifiers, code = parse_key_expression(args.key)
        await self._emit_sequence(modifiers, code)
        await self.sleep(self.settle_seconds)
        return PressKeyResultModel(key=args.key)

    async def _open_chat(self, args: OpenChatToolArgs) -> OpenChatResultModel:
        """Ctrl+F 搜索备注名并打开会话，返回标题与全屏截图。"""
        baseline = await self.screenshots.capture(crop=(self.width, self.title_height))
        baseline_sha = hashlib.sha256(baseline).hexdigest()
        await self._press_combination(["ctrl", "f"])
        await self.clipboard.write(args.remark_name)
        await self._press_combination(["ctrl", "v"])
        await self.sleep(self.settle_seconds)
        await self._press_combination(["enter"])
        title_png, title_changed = await self._await_title_change(baseline_sha)
        full_png = await self.screenshots.capture()
        return OpenChatResultModel(
            remark_name=args.remark_name,
            title_image_base64=base64.b64encode(title_png).decode("ascii"),
            full_image_base64=base64.b64encode(full_png).decode("ascii"),
            title_sha256=hashlib.sha256(title_png).hexdigest(),
            full_sha256=hashlib.sha256(full_png).hexdigest(),
            title_changed=title_changed,
        )

    # ------------------------------------------------------------------
    # 注入助手
    # ------------------------------------------------------------------

    async def _await_title_change(self, baseline_sha: str) -> tuple[bytes, bool]:
        """有界等待标题区域画面变化并返回最新标题截图。

        Args:
            baseline_sha: 搜索前的标题区域 sha256。

        Returns:
            ``(最新标题区域 PNG, 是否在截止内发生变化)`` 二元组；截止仍未
            变化时返回最后一次抓取结果与 False。
        """
        deadline = self.monotonic() + max(self.open_chat_timeout_seconds, 0.0)
        title_png = await self.screenshots.capture(crop=(self.width, self.title_height))
        while hashlib.sha256(title_png).hexdigest() == baseline_sha:
            if self.monotonic() >= deadline:
                return title_png, False
            await self.sleep(self.settle_seconds)
            title_png = await self.screenshots.capture(
                crop=(self.width, self.title_height)
            )
        return title_png, True

    async def _press_combination(self, tokens: list[str]) -> None:
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
        await self._emit_sequence(modifiers, code)

    async def _emit_sequence(self, modifiers: list[int], code: int) -> None:
        """按序注入修饰键按下 → 主键按下 / 抬起 → 修饰键抬起。

        Args:
            modifiers: 修饰键码列表（书写顺序）。
            code: 主键码。

        Raises:
            AgentToolError: 任一按键注入失败。
        """
        for modifier in modifiers:
            self._require_ok(
                await self.control.send(KeyRequest(code=modifier, pressed=True)),
                "修饰键按下",
            )
        try:
            self._require_ok(
                await self.control.send(KeyRequest(code=code, pressed=True)), "按键按下"
            )
            self._require_ok(
                await self.control.send(KeyRequest(code=code, pressed=False)), "按键抬起"
            )
        finally:
            for modifier in reversed(modifiers):
                self._require_ok(
                    await self.control.send(KeyRequest(code=modifier, pressed=False)),
                    "修饰键抬起",
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
# token 解析
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


# ---------------------------------------------------------------------------
# HTTP/1.1 解析（纯函数）与响应编码
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ParsedHttpRequest:
    """已解析的单请求 HTTP/1.1 报文。

    Attributes:
        method: 请求方法（原样大写形态）。
        target: 请求目标（路径；不含查询串处理）。
        headers: 请求头映射（名称小写）。
        body: 请求体字节。
    """

    method: str
    target: str
    headers: dict[str, str]
    body: bytes


@dataclass(frozen=True)
class HttpResponse:
    """待写回的 JSON 响应。

    Attributes:
        status: HTTP 状态码。
        payload: JSON 载荷对象。
    """

    status: int
    payload: Mapping[str, object]


class HttpRequestError(Exception):
    """HTTP 请求报文非法（携带应回写的状态码）。

    Attributes:
        status: 应回写的 HTTP 状态码。
        detail: 人读失败原因。
    """

    def __init__(self, status: int, detail: str) -> None:
        """初始化请求错误。

        Args:
            status: 应回写的 HTTP 状态码。
            detail: 人读失败原因。
        """
        super().__init__(detail)
        self.status = status
        self.detail = detail


def _parse_head(head_text: str) -> tuple[str, str, dict[str, str], int]:
    """解析请求行与请求头并返回声明的请求体长度。

    Args:
        head_text: 请求头文本（不含空行分隔符）。

    Returns:
        ``(method, target, headers, content_length)`` 四元组。

    Raises:
        HttpRequestError: 请求行 / 请求头非法，或 Content-Length 越界。
    """
    lines = head_text.split("\r\n")
    if not lines or not lines[0].strip():
        raise HttpRequestError(422, "请求行不得为空")
    parts = lines[0].split(" ")
    if len(parts) != 3 or not all(parts):
        raise HttpRequestError(422, "请求行格式非法")
    method, target, version = parts
    if not version.startswith("HTTP/1."):
        raise HttpRequestError(422, "仅支持 HTTP/1.x")
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if not line.strip():
            continue
        name, separator, value = line.partition(":")
        key = name.strip().lower()
        if not separator or not key:
            raise HttpRequestError(422, "请求头行格式非法")
        cleaned = value.strip()
        if key == "content-length" and key in headers and headers[key] != cleaned:
            raise HttpRequestError(422, "Content-Length 重复且不一致")
        headers[key] = cleaned
    transfer_encoding = headers.get("transfer-encoding", "").strip().lower()
    if transfer_encoding and transfer_encoding != "identity":
        raise HttpRequestError(422, "不支持 Transfer-Encoding")
    raw_length = headers.get("content-length")
    if raw_length is None:
        if method == "POST":
            raise HttpRequestError(422, "缺少合法的 Content-Length")
        return method, target, headers, 0
    if not raw_length.isascii() or not raw_length.isdigit():
        # ``str.isdigit`` 对非 ASCII 数字字符（如上标 ``²``）同样为真，但
        # ``int()`` 只接受 ASCII 数字——不拦会落 ValueError → 受控 500。
        raise HttpRequestError(422, "缺少合法的 Content-Length")
    if len(raw_length) > 10:
        # 10 位十进制已远超 1 MiB 上限；先判长度同时规避 int() 的 4300 位
        # 位数上限（超限同样抛 ValueError）。
        raise HttpRequestError(413, "请求体超过大小上限")
    length = int(raw_length)
    if length > MAX_AGENT_REQUEST_BYTES:
        raise HttpRequestError(413, "请求体超过大小上限")
    return method, target, headers, length


def parse_http_request(raw: bytes) -> ParsedHttpRequest:
    """把完整请求报文解析为请求模型（纯函数）。

    Args:
        raw: 原始请求字节（含请求行 / 请求头 / 空行 / 请求体）。

    Returns:
        已解析的请求模型。

    Raises:
        HttpRequestError: 请求头缺失或超限 / 请求行或请求头非法 /
            Content-Length 非法或与请求体长度不符。
    """
    head_bytes, separator, body = raw.partition(_HEADER_SEPARATOR)
    if not separator:
        raise HttpRequestError(422, "请求头不完整")
    if len(head_bytes) > _MAX_HTTP_HEADER_BYTES:
        raise HttpRequestError(413, "请求头超过大小上限")
    method, target, headers, length = _parse_head(
        head_bytes.decode("utf-8", errors="replace")
    )
    if len(body) != length:
        raise HttpRequestError(422, "请求体长度与 Content-Length 不符")
    return ParsedHttpRequest(
        method=method, target=target, headers=headers, body=body
    )


def _bearer_token(headers: Mapping[str, str]) -> str:
    """从 Authorization 头提取 Bearer token。

    Args:
        headers: 请求头映射（名称小写）。

    Returns:
        token 本值；缺失或形态不符时为空串。
    """
    raw = headers.get("authorization", "").strip()
    scheme, _, value = raw.partition(" ")
    if scheme.lower() != "bearer":
        return ""
    return value.strip()


def _authorized(headers: Mapping[str, str], token: str) -> bool:
    """恒时比较 Bearer token（UTF-8 字节比较，非 ASCII 不抛异常）。

    Args:
        headers: 请求头映射。
        token: 期望 token。

    Returns:
        True 表示鉴权通过。
    """
    provided = _bearer_token(headers)
    return bool(provided) and hmac.compare_digest(
        provided.encode("utf-8"), token.encode("utf-8")
    )


def _encode_response(response: HttpResponse) -> bytes:
    """把响应模型编码为 HTTP/1.1 报文（恒关闭连接）。

    Args:
        response: 响应模型。

    Returns:
        完整响应字节。
    """
    body = json.dumps(dict(response.payload), ensure_ascii=False).encode("utf-8")
    try:
        reason = HTTPStatus(response.status).phrase
    except ValueError:
        reason = "Unknown"
    head = (
        f"HTTP/1.1 {response.status} {reason}\r\n"
        "Content-Type: application/json; charset=utf-8\r\n"
        f"Content-Length: {len(body)}\r\n"
        "Connection: close\r\n"
        "\r\n"
    ).encode("ascii")
    return head + body


async def _read_request(reader: asyncio.StreamReader) -> ParsedHttpRequest:
    """从流中读取一个有界请求（请求头 + Content-Length 声明的请求体）。

    Args:
        reader: 连接读取流。

    Returns:
        已解析的请求模型。

    Raises:
        HttpRequestError: 请求头超限 / 读取超时 / 请求体不完整。
    """
    try:
        head = await asyncio.wait_for(
            reader.readuntil(_HEADER_SEPARATOR), timeout=_HTTP_READ_TIMEOUT_SECONDS
        )
    except asyncio.LimitOverrunError as exc:
        raise HttpRequestError(413, "请求头超过大小上限") from exc
    except asyncio.IncompleteReadError as exc:
        raise HttpRequestError(422, "请求头不完整") from exc
    except TimeoutError as exc:
        raise HttpRequestError(408, "读取请求超时") from exc
    head_bytes = head[: -len(_HEADER_SEPARATOR)]
    if len(head_bytes) > _MAX_HTTP_HEADER_BYTES:
        raise HttpRequestError(413, "请求头超过大小上限")
    method, target, headers, length = _parse_head(
        head_bytes.decode("utf-8", errors="replace")
    )
    if length == 0:
        return ParsedHttpRequest(
            method=method, target=target, headers=headers, body=b""
        )
    try:
        body = await asyncio.wait_for(
            reader.readexactly(length), timeout=_HTTP_READ_TIMEOUT_SECONDS
        )
    except asyncio.IncompleteReadError as exc:
        raise HttpRequestError(422, "请求体不完整") from exc
    except TimeoutError as exc:
        raise HttpRequestError(408, "读取请求体超时") from exc
    return ParsedHttpRequest(method=method, target=target, headers=headers, body=body)


async def dispatch_request(
    request: ParsedHttpRequest, runtime: AgentRuntime, token: str
) -> HttpResponse:
    """处理一个已解析请求（健康面 / 鉴权 / 载荷校验 / 工具执行）。

    Args:
        request: 已解析请求。
        runtime: 五工具执行面。
        token: 期望 Bearer token。

    Returns:
        待写回的响应（工具失败恒 200 + ok=false）。
    """
    if request.method == "GET":
        if request.target == WECHAT_GUI_HEALTH_PATH:
            return HttpResponse(200, HealthResponseModel().to_wire())
        return HttpResponse(404, {"detail": "not found"})
    if request.method != "POST":
        return HttpResponse(405, {"detail": "method not allowed"})
    if request.target != WECHAT_GUI_INVOKE_PATH:
        return HttpResponse(404, {"detail": "not found"})
    if not _authorized(request.headers, token):
        return HttpResponse(401, {"detail": "unauthorized"})
    try:
        payload = json.loads(request.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        return HttpResponse(422, {"detail": "请求体不是合法 JSON"})
    try:
        invoke = InvokeRequestModel.from_wire(payload)
    except ValueError as exc:
        return HttpResponse(422, {"detail": str(exc)})
    started_ms = runtime.now_ms()
    try:
        async with runtime.lock:
            result = await runtime.execute(invoke.tool, invoke.arguments)
    except Exception as exc:  # 工具级失败恒归一为 200 + ok=false
        logger.warning("agent 工具执行失败: tool=%s, error=%s", invoke.tool, exc)
        return HttpResponse(
            200,
            InvokeResponseModel(
                ok=False,
                error=str(exc) or type(exc).__name__,
                latency_ms=max(0, runtime.now_ms() - started_ms),
            ).to_wire(),
        )
    return HttpResponse(
        200,
        InvokeResponseModel(
            ok=True,
            result=result.to_wire(),
            latency_ms=max(0, runtime.now_ms() - started_ms),
        ).to_wire(),
    )


async def _handle_connection(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    runtime: AgentRuntime,
    token: str,
) -> None:
    """处理单连接（一请求一响应，响应后关闭——含顶层 500 兜底）。

    Args:
        reader: 连接读取流。
        writer: 连接写入流。
        runtime: 五工具执行面。
        token: 期望 Bearer token。
    """
    response = HttpResponse(500, {"detail": "agent 内部错误"})
    try:
        try:
            request = await _read_request(reader)
        except HttpRequestError as exc:
            response = HttpResponse(exc.status, {"detail": exc.detail})
        else:
            response = await dispatch_request(request, runtime, token)
    except Exception as exc:  # 顶层兜底：受控 500，不断连
        logger.warning("agent 请求处理未预期异常: %s", exc)
        response = HttpResponse(500, {"detail": "agent 内部错误"})
    try:
        writer.write(_encode_response(response))
        await writer.drain()
    except (ConnectionError, OSError) as exc:
        logger.debug("agent 响应写回失败: %s", exc)
    finally:
        writer.close()
        with contextlib.suppress(OSError):
            await writer.wait_closed()


async def start_server(
    runtime: AgentRuntime,
    token: str,
    *,
    host: str = DEFAULT_AGENT_HOST,
    port: int = DEFAULT_AGENT_PORT,
) -> asyncio.Server:
    """启动异步 HTTP 服务（仅回环；每请求一连接）。

    Args:
        runtime: 五工具执行面。
        token: Bearer token。
        host: 监听地址（缺省仅回环）。
        port: 监听端口（0 = 由内核分配——测试形态）。

    Returns:
        已启动的 asyncio Server。
    """

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await _handle_connection(reader, writer, runtime, token)

    return await asyncio.start_server(
        handle,
        host,
        port,
        limit=_MAX_HTTP_HEADER_BYTES + len(_HEADER_SEPARATOR),
    )


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


async def _run(args: argparse.Namespace, *, stop_event: asyncio.Event | None = None) -> int:
    """在事件循环内启动 agent 直至停止事件置位。

    Args:
        args: 命令行参数。
        stop_event: 停止事件（None = 新建并绑定 SIGTERM / SIGINT）。

    Returns:
        进程退出码（0 正常收口；1 缺 token）。
    """
    token = resolve_agent_token(token_file=args.token_file)
    if not token:
        logger.error("agent Bearer token 缺失（检查 %s 或 %s）", args.token_file, AGENT_TOKEN_ENV)
        return 1
    runtime = AgentRuntime(
        control=UinputControlClient(args.control_socket, timeout=args.operation_timeout),
        screenshots=ImagemagickScreenshot(args.display, timeout=args.operation_timeout),
        clipboard=XclipClipboard(args.display, timeout=args.operation_timeout),
        width=args.width,
        height=args.height,
        title_height=args.title_height,
        open_chat_timeout_seconds=args.open_chat_timeout,
    )
    event = stop_event if stop_event is not None else asyncio.Event()
    if stop_event is None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            # 非主线程的事件循环无法注册信号处理器（测试注入场景）；
            # 生产入口在主线程运行，不触发该抑制。
            with contextlib.suppress(RuntimeError):
                loop.add_signal_handler(sig, event.set)
    server = await start_server(runtime, token, host=args.host, port=args.port)
    sockets = server.sockets or ()
    bound_port = sockets[0].getsockname()[1] if sockets else args.port
    logger.info("agent 就绪：http://%s:%d（仅回环）", args.host, bound_port)
    async with server:
        await event.wait()
    return 0


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
    try:
        return asyncio.run(_run(args))
    except KeyboardInterrupt:
        logger.info("agent 收到中断，收口")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())

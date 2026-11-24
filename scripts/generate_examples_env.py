#!/usr/bin/env python3
"""``.example`` 配置文件生成器——``backend/.env.example`` 与文档分节表生成。

从 :class:`BaseSettings` 子类的 ``model_fields`` 内省字段信息，按分节
生成 ``.env.example`` 内容。示例永不与模型漂移：字段增删 / 默认值 /
描述变更后重新运行 ``pixi run gen-examples`` 即同步。

同一份 ``_ENV_SECTIONS`` 登记表还驱动 ``docs/backend/config.md`` 的 env
分节表：``--sync-docs`` 把生成表写入
``<!-- generated:env-sections -->`` 标记块，``--check-docs`` 校验文档与
模型是否漂移（漂移即红）。文档标记块是唯一允许手改的位置以外区域
保持原文——生成器只替换标记块内文本。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from generate_examples_helpers import (
    _get_env_var_name,
    _get_field_description,
    _is_scalar_env_field,
    _stringify_default,
)
from pydantic_settings import BaseSettings

from packages.harness.chat.compliance_models import ComplianceGuardSettings
from packages.harness.chat.flow_models import FlowGateSettings
from packages.harness.chat.injection_models import InjectionGuardSettings
from packages.harness.chat.persona_rotation_models import GroupPersonaSettings
from packages.harness.config.models import (
    AsrSettings,
    DatabaseConfig,
    DouyinChannelSettings,
    DreamScheduleSettings,
    DutySettings,
    LlmEndpointConfig,
    LogConfig,
    PoolsConfig,
    QqChannelSettings,
    SchedulerSettings,
    SeekDBSettings,
    ServerConfig,
    Settings,
    SmsForwarderChannelSettings,
    TelegramSettings,
    ToolsBrowserSidecarSettings,
    ToolsSandboxSettings,
    TtsSettings,
    WebSearchSettings,
    WecomChannelSettings,
    WhatsAppChannelSettings,
)
from packages.harness.emotion.emotion_models import EmotionSettings
from packages.harness.intent.intent_models import IntentSettings
from packages.harness.llm.intimate_models import IntimateChannelConfig
from packages.harness.multimodal.sticker_models import StickerSettings
from packages.harness.rhythm.rhythm_models import RhythmSettings
from packages.harness.voice.realtime_models import RealtimeVoiceSettings
from packages.harness.voice.voiceprint_models import VoiceprintSettings

# .env.example 分节标题: (section_title, config_classes)
_ENV_SECTIONS: list[tuple[str, list[type[BaseSettings]]]] = [
    ("SERVER — Web 服务配置", [ServerConfig]),
    ("DATABASE — 数据库连接配置（MySQL 8 权威 / SQLite 测试）", [DatabaseConfig]),
    ("LOG — 日志配置（三 sink：console + 文本 + JSON）", [LogConfig]),
    ("POOLS — 共享池配置（线程池 / HTTP 池 / LLM 信号量 / 熔断）", [PoolsConfig]),
    ("LLM — 默认端点 env 面（模型注册表经 config.yaml 配置）", [LlmEndpointConfig]),
    ("LLM.INTIMATE — 亲密回复独立通道（默认关闭；base_url / api_key / model 三项齐全且开关开启才生效）", [IntimateChannelConfig]),
    ("SEEKDB — 检索存储配置（enabled=False 走 SQLite 回退；密码经 SEEKDB_PASSWORD 间接）", [SeekDBSettings]),
    ("CHANNELS.TELEGRAM — Telegram 通道配置（机器人令牌经 TELEGRAM_BOT_TOKEN 间接）", [TelegramSettings]),
    ("CHANNELS.QQ — QQ 通道配置（NapCat OneBot v11；令牌经 QQ_ACCESS_TOKEN / QQ_WEBHOOK_TOKEN 间接）", [QqChannelSettings]),
    ("CHANNELS.WECOM — 企业微信智能机器人长连接配置（默认关闭；订阅凭据 = BotID + Secret，密钥经 WECOM_SECRET 间接）", [WecomChannelSettings]),
    ("CHANNELS.DOUYIN — 抖音 web 通道配置（默认关闭；复用浏览器 sidecar 的已登录页面，登录态是 profile cookie，无独立密钥键）", [DouyinChannelSettings]),
    ("CHANNELS.SMS_FORWARDER — SmsForwarder 短信 / 通知转发通道配置（默认关闭；仅入站；令牌经 SMS_FORWARDER_TOKEN 直接配置）", [SmsForwarderChannelSettings]),
    ("CHANNELS.WHATSAPP — WhatsApp Cloud API 通道配置（默认关闭；webhook 入站 + 文本出站；hub token / 应用密钥 / 访问令牌经 WHATSAPP_* 直接配置）", [WhatsAppChannelSettings]),
    ("DUTY — 值班轮配置（charter 工具循环 + 报告属主投递）", [DutySettings]),
    ("DREAM_SCHEDULE — 夜间梦境定时触发配置", [DreamScheduleSettings]),
    ("SCHEDULER — 中央调度器配置（自研异步循环）", [SchedulerSettings]),
    ("TOOLS_SANDBOX — 工具沙盒配置（可选运行时；令牌经 SANDBOX_TOKEN 间接）", [ToolsSandboxSettings]),
    ("TOOLS_BROWSER_SIDECAR — 浏览器 sidecar 配置（可选运行时；令牌经 BROWSER_SIDECAR_TOKEN 间接；容器侧 BROWSER_SIDECAR_* 键见 compose）", [ToolsBrowserSidecarSettings]),
    ("VOICE.TTS — 语音合成配置（CosyVoice3 服务端点）", [TtsSettings]),
    ("VOICE.ASR — 语音识别配置（Qwen3-ASR 服务端点）", [AsrSettings]),
    ("REALTIME_VOICE — 实时语音全双工会话配置（默认关闭；VAD 双阈值 / 静音窗 / 句切上限 / 音频队列与时长上限 / 打断开关）", [RealtimeVoiceSettings]),
    ("VOICEPRINT — 声纹档案配置（默认关闭；说话人向量服务端点 + 阈值 / 样本门槛 / 保存期）", [VoiceprintSettings]),
    ("STICKER — 表情包库配置（默认关闭；哈希去重 / VLM 标注 / 通道表情收发）", [StickerSettings]),
    ("SEARCH.WEB — 联网搜索运营配置（默认关闭；密钥经 SEARCH_BOCHA_API_KEY / SEARCH_ZHIPU_API_KEY 间接——条目表在 config.yaml search 节管理）", [WebSearchSettings]),
    ("RHYTHM — 拟人节律层配置（默认关闭；HH:MM 为 naive 本地时间——调度器同口径）", [RhythmSettings]),
    ("FLOW — 心流门控配置（默认关闭；延迟 / 合并批次经中央调度器一次性作业排程）", [FlowGateSettings]),
    ("GROUP_PERSONA — 群聊多人格应答配置（默认关闭；单人格应答保持既有语义）", [GroupPersonaSettings]),
    ("INTENT — 意图快径配置（默认开启——纯本地零成本罐头意图，绕过主 LLM）", [IntentSettings]),
    ("EMOTION — 轻情绪引擎配置（默认开启——协议解析宽容降级；驱动 M 层情绪状态）", [EmotionSettings]),
    ("INJECTION — 提示词注入防御配置（默认开启——保守阈值；反击风格可选）", [InjectionGuardSettings]),
    ("COMPLIANCE — 人格输出合规守卫配置（默认开启——一次强化重试）", [ComplianceGuardSettings]),
]


def generate_env_section(
    title: str,
    classes: list[type[BaseSettings]],
    *,
    field_filter: str | None = None,
) -> list[str]:
    """生成 .env.example 的单个分节。

    输出格式::

        # =========================================================================== []
        # <title>
        # =========================================================================== []
        # <field description>
        <ENV_VAR>=<default>
        ...

    Args:
        title: 分节标题。
        classes: BaseSettings 子类列表。
        field_filter: 若提供，仅生成字段名与此值匹配的字段（用于 Settings.env）。

    Returns:
        分节文本行列表。
    """
    separator = "# " + "=" * 75
    lines: list[str] = [separator, f"# {title}", separator]

    for cls in classes:
        for field_name, field_info in cls.model_fields.items():
            if field_filter is not None and field_name != field_filter:
                continue
            if not _is_scalar_env_field(field_info):
                continue
            env_var = _get_env_var_name(field_name, cls)
            description = _get_field_description(field_info)
            default = _stringify_default(field_info.default, env_var)

            if description:
                lines.append(f"# {description}")
            lines.append(f"{env_var}={default}")
            lines.append("")  # 字段间空行

    return lines


def generate_env_example() -> str:
    """从 BaseSettings 子类内省生成 .env.example 内容。

    Returns:
        完整的 ``.env.example`` 文件内容字符串。
    """
    lines: list[str] = [
        "# ai_agent_companion backend 环境变量模板",
        "#",
        "# 用法：复制为 .env 并填写真实值；或运行 `pixi run setup` 交互式生成。",
        "# 分层加载链（优先级从低到高）：代码默认值 < .env < 进程环境变量；",
        "# config.yaml / extensions_config.json 承载结构化配置（llm 模型注册表 /",
        "# 角色绑定 / 多模态回退链 / 工具清单），与 env 面不重叠。",
        "#",
        "# 所有变量名必须 UPPERCASE_WITH_UNDERSCORES。",
        "#",
        "# 本文件由 `pixi run gen-examples` 从 packages/harness/config 的 Pydantic 配置类自动生成。",
        "# 不要手动编辑——请修改配置类的 Field(description=...) 后重新生成。",
        "",
    ]

    for title, classes in _ENV_SECTIONS:
        lines.extend(generate_env_section(title, classes))

    # 末尾 ENV 分节（Settings 类的 env 字段，无 env_prefix → ENV）
    # Settings 类包含嵌套子配置字段（server / database / ...），仅保留 env 字段
    lines.extend(generate_env_section("ENV — 运行环境标识", [Settings], field_filter="env"))

    # 去除尾部多余空行
    while len(lines) > 1 and lines[-1] == "":
        lines.pop()

    return "\n".join(lines) + "\n"


#: 文档分节表标记（起止行必须原样出现在 docs/backend/config.md）。
ENV_DOC_MARKER_START = "<!-- generated:env-sections -->"
ENV_DOC_MARKER_END = "<!-- /generated:env-sections -->"

#: 文档分节表目标文件（仓库根相对定位，支持 --docs-path 覆盖）。
DOCS_CONFIG_PATH = Path(__file__).resolve().parents[1] / "docs" / "backend" / "config.md"


def _env_prefix(cls: type[BaseSettings]) -> str:
    """读取配置类的 env 前缀。

    Args:
        cls: BaseSettings 子类。

    Returns:
        前缀字符串（无前缀返回空串）。
    """
    raw = cls.model_config.get("env_prefix", "")
    return str(raw) if raw is not None else ""


def _render_doc_fields(classes: list[type[BaseSettings]]) -> str:
    """把配置类的标量 env 字段渲染为单元格文本。

    Args:
        classes: 共享同一分节的配置类列表。

    Returns:
        `` `KEY`(默认值) · ... `` 形态的单元格（无字段时为占位文本）。
    """
    items: list[str] = []
    for cls in classes:
        for field_name, field_info in cls.model_fields.items():
            if not _is_scalar_env_field(field_info):
                continue
            env_var = _get_env_var_name(field_name, cls)
            default = _stringify_default(field_info.default, env_var)
            shown = default if default != "" else "空"
            items.append(f"`{env_var}`({shown})")
    return " · ".join(items) if items else "（无标量字段）"


def _escape_cell(text: str) -> str:
    """转义 Markdown 表格单元格中的竖线。"""
    return text.replace("|", "\\|")


def render_env_sections_markdown() -> str:
    """渲染 docs/backend/config.md 的 env 分节表标记块。

    Returns:
        含起止标记的完整 Markdown 文本（末尾无多余空行）。
    """
    lines: list[str] = [
        ENV_DOC_MARKER_START,
        "<!-- 本块由 `cd backend && pixi run docs-truth` 从 `_ENV_SECTIONS` 生成；"
        "改动配置类后重新同步，勿手改本块。 -->",
        "",
        "| 分节 | env 前缀 | 字段（默认值） |",
        "|---|---|---|",
    ]
    for title, classes in _ENV_SECTIONS:
        prefix = _env_prefix(classes[0]) if classes else ""
        lines.append(
            f"| {_escape_cell(title)} | `{prefix or '（无前缀）'}` | "
            f"{_render_doc_fields(classes)} |"
        )
    # Settings 的 ENV 字段无前缀，作为独立行保留与 .env.example 的同构关系。
    lines.append(
        f"| Settings（环境标识） | （无前缀） | {_render_doc_fields([Settings])} |"
    )
    lines.append(ENV_DOC_MARKER_END)
    return "\n".join(lines)


def _replace_doc_block(text: str, block: str, path: Path) -> str:
    """用生成块替换文档标记块内容。

    Args:
        text: 文档全文。
        block: 生成块文本。
        path: 文档路径（错误信息用）。

    Returns:
        替换后的文档全文。

    Raises:
        SystemExit: 标记缺失（退出码 2，提示应插入的位置）。
    """
    start = text.find(ENV_DOC_MARKER_START)
    end = text.find(ENV_DOC_MARKER_END)
    if start < 0 or end < 0 or end < start:
        print(
            f"[generate_examples_env] 文档标记缺失: {path}\n"
            f"  请在 env 分节表位置插入 {ENV_DOC_MARKER_START} / {ENV_DOC_MARKER_END}",
            file=sys.stderr,
        )
        raise SystemExit(2)
    end += len(ENV_DOC_MARKER_END)
    return f"{text[:start]}{block}{text[end:]}"


def sync_docs(path: Path | None = None) -> Path:
    """把生成表写入文档标记块。

    Args:
        path: 目标文档路径（None 取 DOCS_CONFIG_PATH）。

    Returns:
        实际写入的文档路径。

    Raises:
        SystemExit: 目标缺失或标记缺失。
    """
    target = path or DOCS_CONFIG_PATH
    if not target.is_file():
        print(f"[generate_examples_env] 文档不存在: {target}", file=sys.stderr)
        raise SystemExit(2)
    text = target.read_text(encoding="utf-8")
    updated = _replace_doc_block(text, render_env_sections_markdown(), target)
    target.write_text(updated, encoding="utf-8")
    print(f"[generate_examples_env] 已同步 env 分节表: {target}")
    return target


def check_docs(path: Path | None = None) -> int:
    """校验文档分节表与配置模型一致。

    Args:
        path: 目标文档路径（None 取 DOCS_CONFIG_PATH）。

    Returns:
        0 = 一致；1 = 漂移或标记缺失。
    """
    target = path or DOCS_CONFIG_PATH
    if not target.is_file():
        print(f"[generate_examples_env] 文档不存在: {target}", file=sys.stderr)
        return 1
    text = target.read_text(encoding="utf-8")
    expected = render_env_sections_markdown()
    start = text.find(ENV_DOC_MARKER_START)
    end = text.find(ENV_DOC_MARKER_END)
    if start < 0 or end < 0 or end < start:
        print(
            f"[generate_examples_env] 文档标记缺失: {target}（运行 --sync-docs 初始化）",
            file=sys.stderr,
        )
        return 1
    actual = text[start : end + len(ENV_DOC_MARKER_END)]
    if actual != expected:
        print(
            f"[generate_examples_env] env 分节表漂移: {target}\n"
            "  修复: cd backend && pixi run python ../scripts/generate_examples_env.py --sync-docs",
            file=sys.stderr,
        )
        return 1
    print("[generate_examples_env] env 分节表与配置模型一致")
    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI 入口（文档分节表同步 / 校验）。

    Args:
        argv: 命令行参数（None 取 sys.argv[1:]）。

    Returns:
        进程退出码：0 成功；1 漂移；2 配置错误。
    """
    parser = argparse.ArgumentParser(description="env 分节表文档同步 / 漂移校验")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--sync-docs", action="store_true", help="把生成表写入文档标记块")
    mode.add_argument("--check-docs", action="store_true", help="校验文档与模型是否漂移")
    parser.add_argument(
        "--docs-path",
        type=Path,
        default=DOCS_CONFIG_PATH,
        help=f"目标文档（默认 {DOCS_CONFIG_PATH}）",
    )
    args = parser.parse_args(argv)
    if args.sync_docs:
        sync_docs(args.docs_path)
        return 0
    return check_docs(args.docs_path)


__all__ = [
    "_ENV_SECTIONS",
    "check_docs",
    "generate_env_example",
    "generate_env_section",
    "render_env_sections_markdown",
    "sync_docs",
]

if __name__ == "__main__":
    raise SystemExit(main())

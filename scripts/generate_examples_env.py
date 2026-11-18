#!/usr/bin/env python3
"""``.example`` 配置文件生成器——``backend/.env.example`` 生成。

从 :class:`BaseSettings` 子类的 ``model_fields`` 内省字段信息，按分节
生成 ``.env.example`` 内容。示例永不与模型漂移：字段增删 / 默认值 /
描述变更后重新运行 ``pixi run gen-examples`` 即同步。
"""

from __future__ import annotations

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
    TelegramSettings,
    ToolsBrowserSidecarSettings,
    ToolsSandboxSettings,
    TtsSettings,
    WebSearchSettings,
    WecomChannelSettings,
)
from packages.harness.emotion.emotion_models import EmotionSettings
from packages.harness.intent.intent_models import IntentSettings
from packages.harness.llm.intimate_models import IntimateChannelConfig
from packages.harness.multimodal.sticker_models import StickerSettings
from packages.harness.rhythm.rhythm_models import RhythmSettings
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
    ("DUTY — 值班轮配置（charter 工具循环 + 报告属主投递）", [DutySettings]),
    ("DREAM_SCHEDULE — 夜间梦境定时触发配置", [DreamScheduleSettings]),
    ("SCHEDULER — 中央调度器配置（自研异步循环）", [SchedulerSettings]),
    ("TOOLS_SANDBOX — 工具沙盒配置（可选运行时；令牌经 SANDBOX_TOKEN 间接）", [ToolsSandboxSettings]),
    ("TOOLS_BROWSER_SIDECAR — 浏览器 sidecar 配置（可选运行时；令牌经 BROWSER_SIDECAR_TOKEN 间接；容器侧 BROWSER_SIDECAR_* 键见 compose）", [ToolsBrowserSidecarSettings]),
    ("VOICE.TTS — 语音合成配置（CosyVoice3 服务端点）", [TtsSettings]),
    ("VOICE.ASR — 语音识别配置（Qwen3-ASR 服务端点）", [AsrSettings]),
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


__all__ = [
    "_ENV_SECTIONS",
    "generate_env_section",
    "generate_env_example",
]

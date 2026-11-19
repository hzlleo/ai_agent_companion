"""Setup Wizard — 配置 schema 构建与文件生成。

提供:

    - ``build_setup_schema``: 基于 .env.example 构建表单 schema
    - ``generate_env_content``: 基于模板和用户值生成 .env 文件内容
    - ``mask_sensitive``: 敏感值打码（用于摘要展示）
    - ``write_config_files``: 写入 .env / config.yaml / extensions_config.json
      （后两者覆写既有相异文件前需经确认通道放行）

路径常量（``ENV_EXAMPLE`` / ``ENV_FILE`` 等）定义在
:mod:`packages.harness.config.wizard_paths`（两模块共享的叶子模块，
无循环依赖）；本模块经模块对象限定访问（函数体内解析
``wizard_paths.ENV_EXAMPLE`` 等属性），因此测试
``monkeypatch.setattr(wizard_paths, "ENV_FILE", ...)`` 一次替换即对
全部消费方生效。

步骤定义（``_STEP_DEFINITIONS``）覆盖 .env.example 的全部分节前缀
（SERVER / DATABASE / LOG / POOLS / LLM_INTIMATE / LLM / SEEKDB /
TELEGRAM / QQ / WECOM / DOUYIN / DUTY / DREAM_SCHEDULE / SCHEDULER / TOOLS_SANDBOX /
VOICE_TTS / VOICE_ASR / REALTIME_VOICE / VOICEPRINT / STICKER / ENV），与模板分节顺序一致。前缀存在包含关系时
（``LLM_INTIMATE_`` ⊂ ``LLM_``）按定义顺序首次命中归属，模板键不会在
两个步骤里重复出现；``FLOW_`` / ``GROUP_PERSONA_`` 为独立前缀，各自
对应心流门控与群聊多人格配置步骤。
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from packages.harness.config import wizard_paths
from packages.harness.config.wizard_dtos import (
    SetupConfigFileEntry,
    SetupField,
    SetupSchema,
    SetupStep,
)
from packages.harness.config.wizard_fields import _is_sensitive_key
from packages.harness.config.wizard_parsers import _ENV_KEY_PATTERN, parse_env_template
from packages.harness.config.wizard_ui import _print_error, _print_info

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

# 步骤定义: (步骤名, 匹配前缀列表)
# 按配置域分组，与 .env.example 分节顺序一致（gen-examples 产物的
# 分节顺序 = scripts/generate_examples_env.py 的 _ENV_SECTIONS 顺序）
_STEP_DEFINITIONS: list[tuple[str, list[str]]] = [
    ("服务配置", ["SERVER_"]),
    ("数据库配置", ["DATABASE_"]),
    ("日志配置", ["LOG_"]),
    ("连接池配置", ["POOLS_"]),
    # 亲密通道步骤必须先于「LLM 端点配置」：LLM_INTIMATE_* 同时命中
    # LLM_ 前缀，先归入更具体的步骤（build_setup_schema 按首次命中归属）。
    ("亲密回复通道配置", ["LLM_INTIMATE_"]),
    ("LLM 端点配置", ["LLM_"]),
    ("SeekDB 检索配置", ["SEEKDB_"]),
    ("Telegram 通道配置", ["TELEGRAM_"]),
    ("QQ 通道配置", ["QQ_"]),
    ("企业微信智能机器人通道配置", ["WECOM_"]),
    ("抖音 web 通道配置", ["DOUYIN_"]),
    ("值班轮配置", ["DUTY_"]),
    ("梦境调度配置", ["DREAM_SCHEDULE_"]),
    ("中央调度器配置", ["SCHEDULER_"]),
    ("工具沙盒配置", ["TOOLS_SANDBOX_"]),
    ("浏览器 sidecar 配置", ["TOOLS_BROWSER_SIDECAR_"]),
    ("语音合成配置", ["VOICE_TTS_"]),
    ("语音识别配置", ["VOICE_ASR_"]),
    ("实时语音会话配置", ["REALTIME_VOICE_"]),
    ("声纹档案配置", ["VOICEPRINT_"]),
    ("表情包库配置", ["STICKER_"]),
    ("联网搜索配置", ["SEARCH_"]),
    ("拟人节律配置", ["RHYTHM_"]),
    ("心流门控配置", ["FLOW_"]),
    ("群聊多人格配置", ["GROUP_PERSONA_"]),
    ("意图快径配置", ["INTENT_"]),
    ("轻情绪引擎配置", ["EMOTION_"]),
    ("提示词注入防御配置", ["INJECTION_"]),
    ("人格输出合规守卫配置", ["COMPLIANCE_"]),
    ("其他配置", ["ENV"]),
]


# ---------------------------------------------------------------------------
# Schema 构建
# ---------------------------------------------------------------------------


def build_setup_schema() -> SetupSchema:
    """基于 .example 文件动态构建配置表单 schema。

    读取 .env.example 模板，解析出所有配置字段，按步骤分组返回。

    Returns:
        :class:`SetupSchema`，包含 ``steps`` 列表和 ``config_files`` 信息。
        每个 step 包含 ``name`` 和 ``fields`` 列表，每个 field 包含
        ``key`` / ``label`` / ``type`` / ``default``。
    """
    fields = parse_env_template(wizard_paths.ENV_EXAMPLE)

    steps: list[SetupStep] = []
    claimed: set[str] = set()
    for step_name, prefixes in _STEP_DEFINITIONS:
        step_fields: list[SetupField] = []
        for f in fields:
            if f.key in claimed:
                continue
            if any(f.key.startswith(prefix) for prefix in prefixes):
                claimed.add(f.key)
                step_fields.append(
                    SetupField(
                        key=f.key,
                        label=f.label,
                        type=f.field_type,
                        default=f.default,
                    )
                )
        if step_fields:
            steps.append(SetupStep(name=step_name, fields=step_fields))

    return SetupSchema(
        steps=steps,
        config_files={
            "env": SetupConfigFileEntry(template=".env.example", output=".env"),
            "config": SetupConfigFileEntry(template="config.example.yaml", output="config.yaml"),
            "extensions": SetupConfigFileEntry(
                template="extensions_config.example.json",
                output="extensions_config.json",
            ),
        },
    )


# ---------------------------------------------------------------------------
# 生成与写入
# ---------------------------------------------------------------------------


def generate_env_content(template_path: Path, values: dict[str, str]) -> str:
    """基于模板和用户值生成 .env 文件内容。

    保留模板的原始结构（注释、分隔线、空行、格式），仅替换存在于
    ``values`` 中的 key 的值。

    Args:
        template_path: .env.example 模板路径。
        values: 用户输入的配置值字典。

    Returns:
        生成的 .env 文件内容字符串。
    """
    lines = template_path.read_text(encoding="utf-8").splitlines(keepends=True)
    result: list[str] = []

    for line in lines:
        stripped = line.strip()
        match = _ENV_KEY_PATTERN.match(stripped)
        if match:
            key = match.group(1)
            if key in values:
                newline = "\n" if line.endswith("\n") else ""
                result.append(f"{key}={values[key]}{newline}")
                continue
        result.append(line)

    output = "".join(result)
    if not output.endswith("\n"):
        output += "\n"
    return output


def mask_sensitive(values: dict[str, str]) -> dict[str, str]:
    """返回敏感值打码后的副本（用于摘要展示）。

    Args:
        values: 原始配置值字典。

    Returns:
        敏感字段值替换为 ``***`` 的副本，非敏感字段保持原值。
    """
    result: dict[str, str] = {}
    for key, val in values.items():
        if _is_sensitive_key(key) and val:
            result[key] = "***"
        else:
            result[key] = val
    return result


def _write_managed_file(
    output: Path,
    content: str,
    *,
    confirm_overwrite: Callable[[Path], bool] | None,
) -> None:
    """写入 registry_admin 管理的结构化配置文件（覆写需确认）。

    目标文件（config.yaml / extensions_config.json）不存在或与待写
    内容一致时直接写入；已存在且内容相异时打印中文警告（文件位置 /
    registry_admin 管理面语义 / ``.bak-<epoch-ms>`` 备份恢复路径），
    经 ``confirm_overwrite`` 确认通道放行后才覆写——通道未提供或
    拒绝时跳过写入并提示（fail-safe：不静默丢弃既有管理内容）。

    Args:
        output: 目标输出路径。
        content: 待写入的模板渲染内容。
        confirm_overwrite: 覆写确认通道（收到目标路径，返回是否放行
            覆写）；``None`` 时按拒绝处理。
    """
    if not output.exists() or output.read_text(encoding="utf-8") == content:
        output.write_text(content, encoding="utf-8")
        return

    _print_error(f"检测到已存在的管理面配置文件，内容与模板不同: {output}")
    _print_error(
        "  该文件由 registry_admin 管理面维护（LLM 模型注册表 / 角色绑定 / 多模态回退链）。"
    )
    _print_error("  覆写将丢弃现有内容；历史版本可从同目录 .bak-<epoch-ms> 备份文件恢复。")
    approved = confirm_overwrite(output) if confirm_overwrite is not None else False
    if not approved:
        _print_info(f"已跳过覆写（未确认）: {output}")
        return
    output.write_text(content, encoding="utf-8")


def write_config_files(
    values: dict[str, str],
    *,
    dry_run: bool = False,
    env_template: Path | None = None,
    config_template: Path | None = None,
    extensions_template: Path | None = None,
    env_output: Path | None = None,
    config_output: Path | None = None,
    extensions_output: Path | None = None,
    confirm_overwrite: Callable[[Path], bool] | None = None,
) -> dict[str, str]:
    """写入三个配置文件。

    - .env: 基于 .env.example 模板，用 ``values`` 替换对应字段（保留
      既有合并语义——既有值作为默认值回流，无条件写入）
    - config.yaml: 从 config.example.yaml 复制（已存在且内容相异时
      需经 ``confirm_overwrite`` 确认，见 :func:`_write_managed_file`）
    - extensions_config.json: 从 extensions_config.example.json 复制
      （覆写确认语义同 config.yaml）

    Args:
        values: 用户输入的 .env 配置值。
        dry_run: 为 True 时仅返回内容预览，不实际写入文件。
        env_template: 自定义 .env 模板路径（默认 ENV_EXAMPLE）。
        config_template: 自定义 config.yaml 模板路径（默认 CONFIG_EXAMPLE）。
        extensions_template: 自定义 extensions 模板路径（默认 EXTENSIONS_EXAMPLE）。
        env_output: 自定义 .env 输出路径（默认 ENV_FILE）。
        config_output: 自定义 config.yaml 输出路径（默认 CONFIG_FILE）。
        extensions_output: 自定义 extensions 输出路径（默认 EXTENSIONS_FILE）。
        confirm_overwrite: 覆写确认通道（config.yaml / extensions_config.json
            已存在且内容相异时调用；``None`` = 拒绝覆写）。

    Returns:
        ``{filename: content}`` 字典，包含三个文件的内容（用于预览/dry-run）。
    """
    # 路径常量经 wizard_paths 模块对象限定访问，测试 monkeypatch
    # wizard_paths.ENV_FILE 等一次即对全部消费方生效
    env_tmpl = env_template or wizard_paths.ENV_EXAMPLE
    config_tmpl = config_template or wizard_paths.CONFIG_EXAMPLE
    ext_tmpl = extensions_template or wizard_paths.EXTENSIONS_EXAMPLE
    env_out = env_output or wizard_paths.ENV_FILE
    config_out = config_output or wizard_paths.CONFIG_FILE
    ext_out = extensions_output or wizard_paths.EXTENSIONS_FILE

    # 生成 .env 内容（模板替换）
    env_content = generate_env_content(env_tmpl, values)

    # config.yaml 和 extensions_config.json 从模板复制
    # （用户可配置的基础设施参数均在 .env 中，这两个文件为结构化配置）
    config_content = config_tmpl.read_text(encoding="utf-8")
    ext_content = ext_tmpl.read_text(encoding="utf-8")

    result: dict[str, str] = {
        ".env": env_content,
        "config.yaml": config_content,
        "extensions_config.json": ext_content,
    }

    if dry_run:
        return result

    # .env 保留既有合并语义（无条件写入——既有值已作为默认值并入 values）
    env_out.write_text(env_content, encoding="utf-8")
    _write_managed_file(config_out, config_content, confirm_overwrite=confirm_overwrite)
    _write_managed_file(ext_out, ext_content, confirm_overwrite=confirm_overwrite)

    return result

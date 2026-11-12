"""Setup Wizard Schema（Pydantic v2）——配置向导的结构化表单契约。

- ``SetupField``           —— 单个 .env 配置字段（表单渲染单元）
- ``SetupStep``            —— 配置向导步骤（一组逻辑相关的字段）
- ``SetupConfigFileEntry`` —— 配置文件模板/输出路径映射
- ``SetupSchema``          —— 向导全量表单 schema（steps + config_files）
"""

from __future__ import annotations

from pydantic import BaseModel, Field

__all__ = [
    "SetupField",
    "SetupStep",
    "SetupConfigFileEntry",
    "SetupSchema",
]


class SetupField(BaseModel):
    """单个 .env 配置字段（表单渲染单元）。

    Attributes:
        key: 环境变量名（大写下划线，如 ``DATABASE_HOST``）。
        label: 字段中文标签（UI 展示用，缺失时回退到 key）。
        type: 字段类型——``password`` / ``boolean`` / ``number`` / ``text``。
        default: 模板默认值（始终为字符串，可能为空字符串）。
    """

    key: str
    label: str
    type: str
    default: str = ""


class SetupStep(BaseModel):
    """配置向导步骤（一组逻辑相关的 .env 字段）。

    Attributes:
        name: 步骤名称（如 ``"数据库配置"``）。
        fields: 该步骤下的字段列表。
    """

    name: str
    fields: list[SetupField] = Field(default_factory=list)


class SetupConfigFileEntry(BaseModel):
    """单个配置文件的模板/输出路径映射。

    Attributes:
        template: 模板文件名（如 ``.env.example``）。
        output: 输出文件名（如 ``.env``）。
    """

    template: str
    output: str


class SetupSchema(BaseModel):
    """向导全量表单 schema——基于 .example 文件动态生成的配置表单结构。

    Attributes:
        steps: 配置向导步骤列表（按展示顺序）。
        config_files: 三类配置文件的模板/输出路径映射
            （``env`` / ``config`` / ``extensions``）。
    """

    steps: list[SetupStep] = Field(default_factory=list)
    config_files: dict[str, SetupConfigFileEntry] = Field(default_factory=dict)

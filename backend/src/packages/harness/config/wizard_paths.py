"""Setup Wizard — 路径常量（模板与输出文件的唯一真相源）。

wizard facade（:mod:`packages.harness.config.wizard`）与生成器
（:mod:`packages.harness.config.wizard_generators`）共享本模块定义的
路径常量，两模块之间不再互相引用（历史上 generators 为取路径常量
反向 import wizard 形成循环依赖，经本模块消解）。

消费契约：两个消费模块均以模块对象限定访问（函数体内解析
``wizard_paths.ENV_FILE`` 等属性），因此测试
``monkeypatch.setattr(wizard_paths, "ENV_FILE", ...)`` 一次替换即对
全部消费方生效。

路径基于 ``__file__`` 推导（本文件位于
backend/src/packages/harness/config/，上溯四级即 backend/），不依赖
进程 CWD；运行契约为 ``pixi run setup``（backend pixi 环境的
activation 已设置 PYTHONPATH，packages.harness.* 可直接导入）。
"""

from __future__ import annotations

from pathlib import Path

__all__ = [
    "BACKEND_DIR",
    "CONFIG_EXAMPLE",
    "CONFIG_FILE",
    "ENV_EXAMPLE",
    "ENV_FILE",
    "EXTENSIONS_EXAMPLE",
    "EXTENSIONS_FILE",
]

#: backend/ 目录（wizard 三件套模板与输出文件的根）。
BACKEND_DIR: Path = Path(__file__).resolve().parents[4]

#: .env 模板（gen-examples 产物——字段面单一真相源）。
ENV_EXAMPLE: Path = BACKEND_DIR / ".env.example"

#: config.yaml 模板（LLM 模型注册表 / 角色绑定 / 多模态回退链）。
CONFIG_EXAMPLE: Path = BACKEND_DIR / "config.example.yaml"

#: extensions_config.json 模板（LLM 工具 function calling schema）。
EXTENSIONS_EXAMPLE: Path = BACKEND_DIR / "extensions_config.example.json"

#: .env 输出（向导生成——保留既有值的合并语义）。
ENV_FILE: Path = BACKEND_DIR / ".env"

#: config.yaml 输出（registry_admin 管理面持久化目标——覆写需确认）。
CONFIG_FILE: Path = BACKEND_DIR / "config.yaml"

#: extensions_config.json 输出（工具清单——覆写需确认）。
EXTENSIONS_FILE: Path = BACKEND_DIR / "extensions_config.json"

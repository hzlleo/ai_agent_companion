#!/usr/bin/env python3
"""ai_agent_companion — 统一配置模板生成器（scripts/ 唯一入口）。

生成全部 3 份 ``.example`` 模板文件：

    1. ``backend/.env.example``                    ← ServerConfig 等 env 注入类
    2. ``backend/config.example.yaml``             ← LlmModelEntry / LlmConfig /
                                                       MultimodalFallbackConfig
    3. ``backend/extensions_config.example.json``  ← ExtensionsConfig

设计原则：
    - **单一入口**：``pixi run gen-examples`` 统一生成。
    - **模型留包内**：Pydantic 配置类是运行时代码，留在 backend 包；
      生成逻辑是构建工具，全部在本 scripts/ 目录。
    - **幂等**：重复运行产出相同内容（byte-stable；测试与 CI 校验模板
      是否漂移）。
    - **从 Field description 生成**：示例永不与模型契约漂移——字段增删 /
      默认值 / 描述变更后重新生成即同步。

用法::

    cd backend && pixi run gen-examples    # 推荐（唯一 pixi 任务）

退出码：0 成功 / 1 失败。
"""

from __future__ import annotations

import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# sys.path 修复 — 允许直接执行 python scripts/generate_examples.py
# （经 pixi 运行时 activation 已含 PYTHONPATH=backend/src，此修复为
# 直接执行兜底；必须先于 generate_examples_* 子模块导入执行）
# ---------------------------------------------------------------------------

_PROJECT_ROOT: Path = Path(__file__).resolve().parent
_SCRIPTS_DIR: Path = _PROJECT_ROOT
_BACKEND_SRC: Path = _PROJECT_ROOT.parent / "backend" / "src"
for _p in (_BACKEND_SRC, str(_SCRIPTS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, str(_p))

# ---------------------------------------------------------------------------
# 统一入口
# ---------------------------------------------------------------------------

_BACKEND_DIR: Path = _PROJECT_ROOT.parent / "backend"


def generate_all(*, write: bool = True) -> dict[str, str]:
    """生成全部 3 份模板文件。

    Args:
        write: 为 True 时写入磁盘；False 仅返回内容（dry-run / 漂移校验）。

    Returns:
        ``{相对路径: 文件内容}`` 字典。
    """
    # 延迟导入（scripts/ 内模块，依赖 sys.path 已修复）
    from generate_examples_env import generate_env_example
    from generate_examples_json import generate_extensions_example_json
    from generate_examples_yaml import generate_config_example_yaml

    generators: dict[str, tuple[Path, str]] = {
        "backend/.env.example": (
            _BACKEND_DIR / ".env.example",
            generate_env_example(),
        ),
        "backend/config.example.yaml": (
            _BACKEND_DIR / "config.example.yaml",
            generate_config_example_yaml(),
        ),
        "backend/extensions_config.example.json": (
            _BACKEND_DIR / "extensions_config.example.json",
            generate_extensions_example_json(),
        ),
    }

    results: dict[str, str] = {}
    for filename, (path, content) in generators.items():
        results[filename] = content
        if write:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")

    return results


def main() -> None:
    """CLI 入口：生成全部模板并打印摘要。"""
    print(f"\n{'=' * 60}")
    print("  ai_agent_companion — 统一配置模板生成器（3 份）")
    print(f"{'=' * 60}\n")

    try:
        results = generate_all(write=True)
    except Exception as exc:
        print(f"✗ 生成失败: {exc}", file=sys.stderr)
        import traceback

        traceback.print_exc()
        sys.exit(1)

    for filename, content in results.items():
        line_count = content.count("\n")
        print(f"  ✓ {filename:45s}  ({line_count} 行)")

    print(f"\n  输出根目录: {_PROJECT_ROOT.parent}")
    print("  完成。请用 git diff 检查变更。\n")


if __name__ == "__main__":
    main()

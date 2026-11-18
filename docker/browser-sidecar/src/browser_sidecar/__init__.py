"""browser_sidecar —— 浏览器自动化 sidecar 包（独立运行时，与后端零代码共享）。

包内容（按文件粒度拆分——与本树 pyproject 布局约定一致）：

    - ``wire_models`` —— wire 契约 Model Classes（人为镜像后端，漂移门禁看护）；
    - ``runtime``     —— sidecar 侧工具运行时（注册 / 隔离执行 / 计时）；
    - ``policy``      —— 导航 / 子资源策略纯函数面（允许清单 + 私网拒绝 + 协议面）；
    - ``browser``     —— playwright 运行时（context-per-profile + 路由拦截 +
      八工具执行器 + 资源护栏）。

本包为常规包（含 ``__init__.py``）：独立分发单元，无多项目命名空间
拼接需求（与后端 PEP 420 命名空间布局的差异是文档化决策——见
本目录 README.md「目录布局」一节）。
"""

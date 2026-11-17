"""harness/search 域——联网搜索能力（provider 客户端 / 编排服务 / LLM 工具）。

R2a-4 落地（owner 决策 D5=A + P6=B）：Bocha 主 / Zhipu search_std 兜底
两 provider，经 config.yaml ``search`` 段注册表管理（管理面见
:mod:`packages.harness.config.search_registry`）；``web_search`` 工具
同时供 duty 工具循环与 chat 工具循环消费；per-day / per-persona 预算
门禁经 usage 域 purpose=``search`` 行计数；详细口径见
docs/backend/search.md。
"""

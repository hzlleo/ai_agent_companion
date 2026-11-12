# 后端模块文档索引

按 harness 领域包逐模块建档；每份文档必须包含：完整 JSON 示例、字段说明表、链路顺序。

| 模块 | 文档 | 状态 |
|---|---|---|
| core（errors/dtos/time_utils/logging/thread_pool/background_tasks/context/i18n/cache/enums） | [core.md](core.md) | 已交付（P0-2） |
| config（loader/models/settings/wizard/三件套生成） | [config.md](config.md) | 已交付（P0-3） |
| database（engine/base Mixins/session/alembic） | [database.md](database.md) | 已交付（P0-3） |
| pools（manager/http/llm 共享池 + 熔断） | [pools.md](pools.md) | 已交付（P0-3） |
| llm（模型网关：多 provider / roles 路由 / 用量埋点） | [llm.md](llm.md) | 已交付（P0-4） |
| usage（token 用量记录与多维聚合） | [usage.md](usage.md) | 已交付（P0-4） |
| persona（chara card / 注册表 / 切换复制 / L-M-S 状态） | [persona.md](persona.md) | 已交付（P0-5，含首个 app 层：personas 管理 API + 共享异常处理器） |
| chat（会话循环 / prompt 组装 / 世界书 / 控制台 CLI） | [chat.md](chat.md) | 已交付（P0-6） |
| app 装配（create_app + lifespan + health 存活面） | [chat.md](chat.md#app-装配appmainpy--appapihealthpy) | 已交付（P0-6，随 chat 域文档建档） |
| memory（三层记忆 / 梦境 / SeekDB 检索） | memory.md | P1 交付 |
| duty / scheduler（值班轮与中央调度） | duty.md | P3 交付 |
| tools / multimodal / voice（工具层与辅助模型客户端） | tools.md | P3-P4 交付 |
| channels / safety（平台通道 / 审批与红线） | channels.md | P3 交付 |

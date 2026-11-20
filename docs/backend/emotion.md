# emotion — 轻情绪引擎

`backend/src/packages/harness/emotion/` 承载轻量情绪状态引擎：模型经输出协议
`<情绪>维度:+增量</情绪>` 自述情绪变化 → 引擎把增量推进到人格 M 层状态文档
（`ff_persona_states.state['emotion']`）→ compose 侧以心情状态块回注摘要行。
同轮产出（`reply` 路径）把心声 / 增量 / 推进后快照写入会话消息 `meta`
（9 号迁移），会话详情 API 读侧暴露。

配套协议解析与可见正文剥离见 [chat.md](chat.md#输出协议心声--情绪)；
流式路径不跑完整协议（同文「stream_reply 边界」）。

## 模块一览

| 模块 | 职责 | 关键导出 |
|---|---|---|
| `emotion_models.py` | 值域与配置 Unit：8 维情绪枚举 + 中文展示名 / 状态 / 增量模型 / 引擎参数 / `EMOTION_` 环境开关 | `EmotionDimension` / `DIMENSION_LABELS` / `EmotionStateModel` / `EmotionDeltaModel` / `EmotionConfig` / `EmotionSettings` / `EMOTION_DOCUMENT_KEY` |
| `core.py` | 纯函数算法（时钟注入 / 无 I/O）：增量叠加 + 夹取 / 指数半衰期衰减 / 摘要行 / 中性判定 | `apply_deltas` / `decay_toward_baseline` / `summarize_emotion` / `is_neutral` |
| `service.py` | 服务面：M 层文档读取 / 增量推进持久化 / 摘要行产出；存储访问面与时钟可注入 | `EmotionService` / `EmotionStoreFace` / `PersonaStateEmotionStore` |

依赖方向：emotion → persona（M 层状态表读写）+ core（time_utils）+ database
（session_scope）。emotion 不依赖 chat（chat 单向消费 emotion——
`chat/output_protocol` 引用维度枚举做协议值域校验）。

## 情绪维度（协议值域 = M 态字段真源）

| 值 | 展示名 | 语义锚（陪伴场景） |
|---|---|---|
| `joy` | 愉悦 | 开心 / 被逗乐 / 期待好事 |
| `affection` | 亲昵 | 亲近 / 依赖 / 想黏人 |
| `shyness` | 羞怯 | 被夸害羞 / 别扭 / 不好意思 |
| `excitement` | 兴奋 | 高唤醒正性情绪 / 跃跃欲试 |
| `calm` | 平静 | 安稳 / 松弛 / 低唤醒正性 |
| `sadness` | 低落 | 失落 / 想念 / 情绪下沉 |
| `anxiety` | 不安 | 担心 / 紧张 / 怕打扰 |
| `annoyance` | 微恼 | 小情绪 / 撒娇式不满（非真怒） |

维度值域固定为 8 维：模型输出未知维度一律丢弃（debug 记录），状态文档
边界二次过滤（陈旧数据防御）。前端展示名与后端 `DIMENSION_LABELS` 同源
一致（`frontend/src/pages/memories/SessionDetailDialog.tsx`）。

## M 态文档形态（`ff_persona_states.state`，layer=`M`）

```json
{
  "emotion": {
    "values": {
      "joy": 0.9,
      "affection": 0.7,
      "shyness": 0.5,
      "excitement": 0.5,
      "calm": 0.5,
      "sadness": 0.5,
      "anxiety": 0.5,
      "annoyance": 0.5
    },
    "updated_at_ms": 1760000000000
  }
}
```

- 键名常量 `EMOTION_DOCUMENT_KEY = "emotion"`；写入为**合并写**——M 文档内
  其他键（心情备注 / 关系评价等他人格状态消费者字段）原样保留，只替换
  `emotion` 子文档；
- 首轮推进（无既有状态）先初始化 8 维全中性基线（0.5）再叠加增量；
- 状态行 upsert 走 `PersonaService.upsert_state`（每 (persona_id, layer) 一行、
  state 整体替换语义——见 [persona.md](persona.md)）；
- 值域 0..1（引擎夹取）；`updated_at_ms` 是衰减时间锚。

## 衰减数学

`decay_toward_baseline(state, now_ms, config)`：

```
elapsed = max(0, now_ms - state.updated_at_ms)      # 时钟回拨按 0 耗时
value' = baseline + (value - baseline) * 0.5 ** (elapsed / half_life_ms)
```

| 参数 | 默认 | 说明 |
|---|---|---|
| `decay_half_life_ms` | 6 小时（21 600 000） | 偏离量每半衰期减半；长期回落到基线 |
| `neutral_baseline` | 0.5 | 中性参考值（8 维无偏向） |
| `summary_threshold` | 0.15 | 摘要 / 中性判定阈值（偏离绝对值） |

示例：`joy=0.9` 经一个半衰期为 0.7（偏离 0.4）、两个半衰期为 0.6（偏离
0.2）、约 6 个半衰期后偏离 < 0.01。时间锚随每次推进刷新（无交互期间状态
自然回落到基线）。

## 摘要与 compose 注入

`summarize_emotion(state, config)` 产出中文摘要行：

```
当前情绪：愉悦↑、羞怯↑、低落↓
```

- 仅收录 `|value - baseline| >= summary_threshold` 的维度，按偏离绝对值降序
  （并列按维度枚举序）；
- 方向标记：高于基线 `↑`、低于基线 `↓`；
- 全中性返回 `None`——`compose_messages` 不注入心情块（空块跳过）。

注入位置（compose 链）：`人格正典 → 心情状态块 → 输出格式协议 → 世界书
after_char`。心情块文本模板为 `EMOTION_STATE_TEMPLATE`（`【心情状态】\n
{summary}`）。读取 / 衰减在 `ChatService` 侧完成（`compose_messages` 保持
纯函数）。

## 服务与装配

```python
EmotionService(
    settings=EmotionSettings(),   # None = 读 EMOTION_ env
    config=EmotionConfig(),       # None = 由 settings 派生
    store=PersonaStateEmotionStore(),
    clock=now_ms,                 # 注入式时钟（测试确定性面）
)
```

| 方法 | 行为 |
|---|---|
| `current_state(persona_id)` | 读 M 文档 → 衰减到当前时刻（只读、不回写）；无状态 / 非法文档返回 None |
| `apply_deltas(persona_id, deltas)` | 读 → 衰减 → 叠加 → 合并写回；引擎停用 / 空增量 → None（零读写） |
| `summary(persona_id)` | 读 → 衰减 → 摘要行；停用 / 无状态 / 全中性 → None |

`ChatService(emotion_service=...)` 接受注入；缺省构造默认实例（读
`EMOTION_` env）。**运行判定**：仅当会话持久化服务已绑定且会话已落库时
情绪读写才激活（M 态存储面依赖数据库——纯内存形态零附加 I/O）。

## 环境开关（`EMOTION_` 前缀）

| env 键 | 默认 | 说明 |
|---|---|---|
| `EMOTION_ENABLED` | `true` | 引擎总开关（**默认开启**——纯本地零成本，同 intent 快径决策：协议块只发格式指令，不合规模型优雅降级；关闭后不读不写 M 态、不注入心情块，meta 仍留档模型自述的心声 / 增量） |
| `EMOTION_DECAY_HALF_LIFE_MS` | `21600000` | 衰减半衰期（毫秒） |
| `EMOTION_NEUTRAL_BASELINE` | `0.5` | 中性基线（0..1） |
| `EMOTION_SUMMARY_THRESHOLD` | `0.15` | 摘要阈值（0..1，>0） |

装配说明：`EmotionSettings` 物理定义在 `emotion_models.py`（pydantic-settings
直读 env）。当前 `config/models.py` 的 `Settings` 聚合器**尚未**纳入
`emotion` 分节（本 chunk 允许清单外）——env 键已可直接生效，聚合器接入与
示例生成器同步列为待办（主 agent 跟进项）。

## 持久化与 API

- 迁移 `000000000009`：`ff_chat_messages` 增 `meta` 可空 JSON 列（升 / 降
  两方言可逆；真引擎 round-trip 见 SeekDB 强制套件）；
- 回合 meta 文档：`{"thought": str|null, "emotion_deltas": {维度: -1..1},
  "emotion_snapshot": {维度: 0..1}|null}`——无元数据回合为 NULL；
- `ChatSessionService.append_message(..., meta=None)` 为可选追加参数（既有
  调用方零变更）；`GET /api/v1/memories/sessions/{id}` 消息 DTO 的 `meta`
  字段读侧暴露（Model Class 校验：`ChatMessageMetaModel`）；
- 前端会话详情渲染心声块（灰色斜体、`心声:` 前缀）与情绪快照小标签；
  无 meta 回合零附加渲染。

## 未来消费者（语音风格 / 前端表情）

- TTS 风格：`current_state` 的维度值可直接映射语速 / 音高 / 情绪
  风格参数；`emotion_snapshot` 为逐轮时序真源；
- 前端表情：同一快照驱动立绘 / 表情切换（维度 → 表情资源映射层
  在前端，后端只保证维度值与协议稳定）。

两消费面均**只读**本域公开模型与方法（`EmotionDimension` /
`EmotionStateModel` / `EmotionService.current_state`），不直接触表。

## 测试与门禁

- `tests/unit/test_emotion.py`——值域模型（未知维度丢弃 / 夹取）/ 引擎参数 /
  env 开关 / 增量叠加 / 半衰期数学 / 时钟回拨 / 摘要排序与方向 / 中性判定 /
  服务持久化合并写 / 停用零读写 / 非法文档宽容；
- `tests/unit/test_output_protocol.py` + `tests/unit/test_chat_emotion.py`——
  协议解析与 chat 集成（真实 SQLite）；
- `tests/unit/test_migration_chat_message_meta.py`——SQLite 升降级 round-trip；
- `tests/integration_seekdb/test_migration_chain.py`——真引擎 0001→0009 升 /
  降 / 复升 + `meta` 列存在性断言；
- 覆盖率门禁：`harness/emotion` ≥80%（per-module 剖面）。

# intent — 意图快径（canned intents）

`backend/src/packages/harness/intent/`（纯函数匹配器 + 薄服务）+ `Settings.intent` 分节（INTENT_ env 面）+ `ChatService.reply` 前钩，构成「意图快径」：**高频固定问法 → 本地模式表直接命中罐头回复，完全绕过主 LLM 调用**——省钱、零延迟抖动、离线可用；未命中时文本照常流入 LLM（无损兜底）。

## 模块一览

| 模块 | 职责 | 关键导出 |
|---|---|---|
| `harness/intent/intent_models.py` | Unit 文件：类型值域 / 匹配结果 / env 配置（INTENT_）/ 人格命名面协议 | `IntentKind` / `IntentMatchModel` / `IntentSettings` / `PersonaNaming` |
| `harness/intent/intent_texts.py` | 罐头文案 Unit 文件（文案与逻辑分离——约束 19 邻接；**非 LLM prompt**，确定性模板） | `TIME_QUERY_TEXT` / `GREETING_*_TEXT` / `IDENTITY_TEXT` / `HELP_TEXT` |
| `harness/intent/matcher.py` | 纯函数匹配器（NFKC 归一 + 两层模式表 + 模板渲染；零 I/O 零异常） | `match_intent` / `CONFIDENCE_EXACT` / `CONFIDENCE_FUZZY` |
| `harness/intent/service.py` | 薄服务（匹配委托 + 称呼提取 + 拟真延迟面；恒不抛异常） | `IntentShortcutService` / `INTENT_SHORTCUT_DELAY_MIN_MS` / `INTENT_SHORTCUT_DELAY_MAX_MS` |
| `chat/service.py`（前钩扩展） | `IntentShortcutFace` 协议 + `reply` 前端快径分派 + `_reply_via_shortcut` 落回合 | `IntentShortcutFace` |
| `app/main.py`（装配） | `_startup_channels` 内装配（通道专属注入——见「装配」一节） | `app.state.intent_shortcut` |

依赖方向：intent_models / intent_texts → pydantic only；matcher → rhythm 纯函数（`is_sleeping`——问候深夜带消费，单向）；service → matcher + rhythm 模型；chat → intent_models（协议结构化消费，**不** import intent/service——`IntentShortcutFace` 结构化类型解耦）。

## 类型值域（五类封顶——锁定决策）

| 类型 | 触发问法（例） | 罐头回复形态 |
|---|---|---|
| `TIME_QUERY` | 现在几点 / 几点了 / what time | `现在是{time}呀～{persona_name}刚瞄了一眼时间`（time = 本地 HH:MM，注入时钟） |
| `GREETING` | 你好 / 早 / 晚安 / hi / 在吗 | 按时段带：`早上好呀～{persona_name}在这里` / 下午 / 晚上 / `这么晚还没睡呀？{persona_name}陪着你呢…` |
| `IDENTITY` | 你是谁 / 你叫什么 / who are you | `我是{persona_name}呀，你的专属小伙伴…` |
| `HELP` | 帮助 / 你能干什么 / help | 静态能力清单（陪伴对话 / 记忆 / 时间问答 + 能力解锁提示） |
| `DIRECTIVE` | 记住… / 记得… / 以后都… / 以后… / 别再… / 不要… | 常备指令捕获（`directive_text` 剥前缀后经注入的 `DirectiveSinkFace` 落库；成功确认「好，我记住啦：{正文}」，sink 缺失或失败降级为普通纪律 ack——恒不阻断回复） |

`STOP_STEER` 等**对话控制类有意不收**（控制流语义需要上下文感知，罐头回复会破坏对话状态机——后续心流层统一承载）。

## 匹配链（`match_intent`——顺序锁定）

```
原文
    ↓ 归一：NFKC（全角→半角）+ 去全部空白 + casefold
    ↓ ① 精确层：整句等值（五类按 DIRECTIVE → TIME_QUERY → GREETING → IDENTITY → HELP 优先序；DIRECTIVE 为前缀捕获类优先判读）→ confidence 0.95
    ↓ ② 模糊层：子串包含（同优先序）→ confidence 0.65
    ↓ 未命中 → IntentMatchModel(kind=None, confidence=0.0, response_text="", handled=False)
    ↓ 命中 → 模板渲染（{persona_name} 称呼 / {time} 本地 HH:MM 注入）
```

- **归一空间一致性**：模式表构造期统一归一（字面量手误防线——模式与文本恒同空间）；
- **短模式安全**：「早」/「hi」仅入精确层（「早饭」/「this」误命中防线）；时间模糊层锚定问法上下文（「现在几点」/「几点了」——「几点睡觉合适」歧义句照常走 LLM）；
- **优先序裁定**：跨类模糊竞争时 TIME_QUERY 最高（「你好，现在几点了」答时间优于答问候）；置信度仅观测面不参与路由。

问候时段带（naive 本地小时）：早上 0-11 / 下午 12-17 / 晚上 18-22；深夜带 = 固定 23-05（无节律上下文）或**人格睡眠窗口内**（提供 `effective` 时——`is_sleeping` 判定，人格视角的「这么晚」）。

匹配结果 JSON 示例::

    {
        "kind": "time_query",
        "confidence": 0.95,
        "response_text": "现在是09:05呀～大肥鱼刚瞄了一眼时间",
        "handled": true
    }

## 回复链路（ChatService.reply 前钩——通道专属）

```
ChannelRouter.handle_inbound（既有链路不变）
    ↓ ChatService.reply(state, text)
    ↓ ① 前钩：intent_shortcut 面（未注入 → 直接跳到 ④——CLI / duty 形态）
    ↓     try_shortcut(text, state)（恒不抛；内部异常归一未命中）
    ↓     ├─ handled=True：
    ↓     │     debug 日志一行 → 用户回合落库 → realism_delay()（800-1500ms 随机）
    ↓     │     → 罐头文本追加 assistant 回合（落库）→ 返回（**网关零调用**）
    ↓     └─ handled=False → 落到 ④
    ↓ ④ 既有链路：组装 → （工具循环）→ 网关 → assistant 回合落库
```

- **持久化不变式**：快径回合照常落 `state.turns` + `ff_chat_messages`（用户 + 罐头助手各一行）——记忆 / 梦境消费面可见（快径对话是真实对话史的一部分）；
- **流式分叉**：`stream_reply` 不走快径（CLI 消费面零变化——通道回复全量走 `reply`）。

## 拟真延迟（决策文档化）

快径回复用**短固定区间 800-1500ms** 随机延迟（`INTENT_SHORTCUT_DELAY_MIN_MS` / `MAX_MS` 服务常量），**不走**节律门完整延迟（上限 45s——罐头回复是「顺手秒回」语义，长延迟反而失真）；采样经与 rhythm 域共享的随机源（单一随机源决策）；睡眠门控不适用于快径（R2d 心流层统一承载回复调度）。测试经注入随机源 + 假休眠面断言区间（零真实等待）。

## 装配（app `_startup_channels`——通道专属，duty 零变化）

| 形态 | 装配结果 |
|---|---|
| 任一通道启用 + `INTENT_ENABLED=true`（默认） | `IntentShortcutService(rng=共享随机源, effective=全局面节律)` 上挂 `app.state.intent_shortcut`；通道 ChatService 工厂三分支（搜索工具 / 浏览器工具 / 零工具）均注入快径面 |
| `INTENT_ENABLED=false` | 槽位 None——工厂零注入，通道回复全量走 LLM（R2a 前行为） |
| 通道全关 | 槽位 None（快径的唯一消费面是通道 ChatService 工厂） |

**duty 不注入快径面**（文档化决策）：duty 是无头计划任务（章程驱动巡检 / 报告生成），非对话性消费——「现在几点」这类用户高频问法只出现在对话里；duty 工具面 / 章程零变化。`effective` 值面恒注入（作息描述与 `RHYTHM_ENABLED` 门控正交——问候选带恒可用节律值）。

## 配置（INTENT_ env 面）

| 字段 | env 键 | 默认 | 说明 |
|---|---|---|---|
| `enabled` | `INTENT_ENABLED` | `true` | 快径开关。**默认开启**（与 search 等外部成本面开关相反）：纯本地模式匹配——零外部依赖 / 零网络 / 零 token 成本，关闭只会让高频问法浪费一次 LLM 调用；风险面仅为罐头文案不够聪明（未命中路径无损兜底） |

## 测试

`tests/unit/test_intent_matcher.py`（五类命中 / 非命中 / 全角 / 大小写 / 多语句 / 优先序 / 时段带表驱动）+ `test_intent_service.py`（委托 / 称呼提取 / 恒不抛 / 延迟区间）+ `test_intent_shortcut_wiring.py`（chat 前钩 + 持久化不变式 + router 级全链路：入站命中 → 罐头投递 + 网关零调用 + 落库；未命中 → 网关调用）+ `test_app_channels_lifespan.py::TestRhythmIntentSlots`（装配槽位 + 单一随机源 + 关闭形态）。

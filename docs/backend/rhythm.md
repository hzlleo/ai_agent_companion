# rhythm — 拟人节律层（persona rhythm）

`backend/src/packages/harness/rhythm/`（纯函数核心 + 薄服务门面）+ `Settings.rhythm` 分节（RHYTHM_ env 面），构成「拟人节律层」：**作息窗口判定 + 拟人回复延迟采样 + 打字模拟**，为 R2d 心流层（回复调度）与 R2e GUI / 浏览器通道（状态展示）提供决策原料；R2a 交付核心数学 + 门面契约，回复链路暂不消费（行为零变化）。

## 模块一览

| 模块 | 职责 | 关键导出 |
|---|---|---|
| `harness/rhythm/rhythm_models.py` | Unit 文件：env 配置（RHYTHM_）/ 人格覆写 / 生效值 / 打字模拟 / 回复门 Model Classes | `RhythmSettings` / `PersonaRhythmOverride` / `EffectiveRhythm` / `TypingSimulation` / `RhythmGate` |
| `harness/rhythm/core.py` | 纯函数核心（睡眠窗口 / 延迟采样 / 打字模拟 / 醒来推进 / 生效值解析——时钟与随机源全注入） | `is_sleeping` / `reply_delay_ms` / `typing_duration_ms` / `next_active_window_start` / `resolve_effective` / `TYPING_DURATION_CAP_MS` |
| `harness/rhythm/service.py` | 薄服务门面（唯一有状态件——生效节律 + 注入时钟 / 随机源；门判定） | `RhythmService` / `Clock` |
| `config/models.py`（分节） | `Settings.rhythm` 聚合（config → rhythm 单向——物理定义在 rhythm_models） | `settings.rhythm` |

依赖方向：pydantic / 标准库 only（`rhythm_models` 被 config 域聚合——**严禁**反向 import config / scheduler / llm；`core` 的「下一个出现」推进计算镜像 scheduler 同语义但本地自持，防 `config → rhythm → scheduler → llm → config` 回环）。

## 配置（RHYTHM_ env 面）

| 字段 | env 键 | 默认 | 说明 |
|---|---|---|---|
| `enabled` | `RHYTHM_ENABLED` | `false` | 节律门控开关（False = RhythmService 门直通——零行为变化；纯拟人增强面，缺省关闭） |
| `bedtime` | `RHYTHM_BEDTIME` | `23:30` | 就寝时刻（HH:MM 24 小时制，**含端点**；naive 本地墙钟） |
| `wake_time` | `RHYTHM_WAKE_TIME` | `08:00` | 醒来时刻（HH:MM，**不含端点**；窗口可跨午夜） |
| `timezone_note` | `RHYTHM_TIMEZONE_NOTE` | `naive-local` | 时区约定说明（文档锚点字段——不参与计算；与 scheduler `daily_at` 同 naive 本地约定） |
| `reply_delay_min_ms` | `RHYTHM_REPLY_DELAY_MIN_MS` | `1500` | 回复延迟下限毫秒（log-uniform 采样域下界；≥1——对数域要求） |
| `reply_delay_max_ms` | `RHYTHM_REPLY_DELAY_MAX_MS` | `45000` | 回复延迟上限毫秒（不得小于下限） |
| `typing_speed_ms_per_char` | `RHYTHM_TYPING_SPEED_MS_PER_CHAR` | `45` | 打字速度（毫秒/字符） |
| `typing_base_ms` | `RHYTHM_TYPING_BASE_MS` | `800` | 打字基础耗时毫秒（起手耗时） |
| `burst_probability` | `RHYTHM_BURST_PROBABILITY` | `0.12` | 快答概率（0-1——快尾 `[min, min*2)` 采样机会） |

生效值 JSON 示例（`resolve_effective(RhythmSettings(), PersonaRhythmOverride(delay_scale=1.5))`）::

    {
        "bedtime": "23:30",
        "wake_time": "08:00",
        "bedtime_minute": 1410,
        "wake_minute": 480,
        "reply_delay_min_ms": 2250,
        "reply_delay_max_ms": 67500,
        "typing_speed_ms_per_char": 45,
        "typing_base_ms": 800,
        "burst_probability": 0.12,
        "delay_scale": 1.5
    }

人格覆写（`PersonaRhythmOverride`，全可选——per-persona 解析属 R2d 心流层）：`bedtime` / `wake_time`（HH:MM 校验）+ `delay_scale`（0.5-2.0 温度缩放——延迟 min/max 同乘；打字参数与快答概率为全局面不参与覆写）。

## 纯函数语义（core）

### 睡眠窗口（`is_sleeping`——边界锁定）

```
bedtime == wake        → 空窗口，恒醒（零长度窗口无意义）
bedtime <  wake（同日）  → [bedtime, wake)           如 13:00-15:00 午休
bedtime >  wake（跨午夜）→ [bedtime, 24:00) ∪ [00:00, wake)   如 23:30-08:00
端点：bedtime 含（23:30 即入睡）/ wake 不含（08:00 即清醒）
```

### 延迟采样（`reply_delay_ms`——bounded log-uniform + 快答尾）

```
rng.random() < burst_probability
    ├─ 是 → log-uniform [min, min(min*2, max))     快答尾（偶尔秒回）
    └─ 否 → log-uniform [min, max]                 常规（短密长疏）
夹逼：结果恒 int 且 ∈ [min, max]；min == max 恒返回该值
now_ms 参数：当前采样不消费（时钟轴一致性——将来按时段调制保留统一注入面）
```

### 打字模拟（`typing_duration_ms`）

`duration_ms = min(base + chars × speed, 8000)`（封顶 8s——超长文本不无限累计）；负长度按 0 夹逼。

### 下一个醒来时刻（`next_active_window_start`）

「下一个出现」语义（镜像 `scheduler.next_daily_occurrence_ms`）：今日 `wake_time` 严格晚于参考时刻则今日、否则明日。入睡中调用方据此调度苏醒；清醒调用方得到明日 wake（下一活跃窗起点）。

## 服务门面（RhythmService）

### 门判定链（`gate_reply`——顺序锁定）

```
gate_reply(now_ms=None → 注入时钟)
    ↓ ① enabled=False → RhythmGate(allowed=True, suggested_delay_ms=0, reason="节律层未启用…")   直通
    ↓ ② is_sleeping(now) → RhythmGate(allowed=False,
    ↓        suggested_delay_ms=next_active_window_start(now) - now,      距醒来毫秒（调度参考）
    ↓        reason="睡眠窗口内（23:30-08:00）——建议静默至醒来")
    ↓ ③ 清醒 → RhythmGate(allowed=True, suggested_delay_ms=reply_delay_ms(采样), reason="清醒时段…")
```

`should_reply_now` = 门的 `allowed` 布尔投影（心流层轻量消费面；R2d 将演化为带主动节拍的完整决策面）。

门结果 JSON 示例::

    {
        "allowed": false,
        "suggested_delay_ms": 27000000,
        "reason": "睡眠窗口内（23:30-08:00）——建议静默至醒来"
    }

## 装配与消费面（app lifespan）

`_startup_channels`（任一通道启用时）：`random.Random()` 单一随机源 → `resolve_effective(settings.rhythm, None)`（全局面——per-persona 覆写解析属 R2d）→ `RhythmService` 上挂 `app.state.rhythm_service`（enabled 随 `RHYTHM_ENABLED`）；随机源**同时**注入 `IntentShortcutService`（快径拟真延迟与节律延迟采样同源——见 intent.md 装配一节）。停机随通道层槽位复位。

消费面前向兼容（文档化）：

| 消费方 | 版本 | 形态 |
|---|---|---|
| R2d heartflow | 后继 | `gate_reply` / `should_reply_now` 回复调度决策（睡眠静默 + 延迟节拍）；per-persona `PersonaRhythmOverride` 解析 |
| R2e GUI / 浏览器通道 | 后继 | 门结果（allowed + reason）人设状态展示（在线 / 休息中） |
| R2a 通道路径 | 本版 | **不消费门**——既有回复行为零变化（意图快径的短延迟属意图面，见 intent.md） |

## 测试

`tests/unit/test_rhythm_settings.py`（env 面 / 校验 / 解析合并）+ `test_rhythm_core.py`（窗口边界表 / 采样区间与确定性 / 打字封顶 / 醒来推进——时区无关构造：测试时刻经 naive `datetime(...).timestamp()` 构建）+ `test_rhythm_service.py`（门三分支 / 投影 / 注入时钟）。

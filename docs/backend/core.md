# core — harness 基础设施包（P0-2 交付）

`backend/src/packages/harness/core/` 是全后端的共享基座：业务异常契约、统一响应封装、epoch 毫秒时间、三 sink 日志、共享线程池、后台任务调度、请求上下文、i18n 消息、进程内 TTL 缓存与跨域枚举。后续全部域包（config / database / pools / llm / persona / chat / usage …）依赖 core，core 不依赖它们（依赖方向单向：`域包 → core`）。

## 模块一览

| 模块 | 职责 | 关键导出 |
|---|---|---|
| `errors.py` | 业务异常基类 + 错误码枚举 + HTTP 状态映射 | `BusinessException` / `ErrorCode` / `get_http_status_for_code` |
| `dtos.py` | 统一响应封装 + 分页模型 + 游标编解码 | `ResultDTO` / `PageResponse` / `encode_cursor` / `decode_cursor` |
| `time_utils.py` | epoch 毫秒时间工具 + 时长格式化 | `now_ms` / `to_ms` / `from_ms` / `format_duration_ms` |
| `logging_models.py` | 日志配置 Model Class（core 的配置契约） | `LoggingConfig` |
| `logging.py` | loguru 三 sink + 轮转 + 脱敏 + 按模块级别 | `setup_logging` / `mask_sensitive` / `parse_module_log_levels` |
| `thread_pool.py` | core 自持共享线程池 + 阻塞操作下沉入口 | `run_blocking_ops` / `init_shared_thread_pool` |
| `background_tasks.py` | 后台任务管理器 + 内存任务队列 | `background_task_manager` / `TaskQueue` / `task_queue` |
| `context.py` | 请求级 ContextVars + 快照绑定 | `bind_context` / `current_context` / `context_log_fields` |
| `i18n_messages.py` | 错误消息表（与 ErrorCode 1:1） | `MESSAGES` |
| `i18n.py` | 消息查找 + locale 解析 + resolver 注册 | `get_message` / `resolve_locale` |
| `cache.py` | 进程内 TTL 缓存 + 装饰器（P0 决策：NO Redis） | `async_ttl_cache` / `MemoryTTLCache` |
| `enums.py` | 跨域通用枚举 | `Locale` / `TaskStatus` / `SUPPORTED_LOCALES` |

## errors.py — 业务异常与错误码

### 异常处理链路（顺序）

```
业务代码 raise BusinessException(code, details)
    ↓
app 层全局异常处理器（后续 chunk 落地）捕获
    ↓
get_http_status_for_code(code) → 语义化 HTTP 状态码
    ↓
ResultDTO.error(message=exc.message, code=exc.code) → JSON 响应
```

### BusinessException 字段

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `code` | `str` | `"INTERNAL_ERROR"` | 错误码；可传 `ErrorCode` 成员或字符串字面量（StrEnum 兼容 `==` 比较） |
| `details` | `dict[str, Any] \| None` | `None` | 附加详情（如 `{"persona_id": "p1"}`），透传到响应体 |
| `message` | `str \| None` | `None` | 面向用户的描述；`None` 时经 `_message_resolver` 回调派生，回调未注册回退 `str(code)` |
| `headers` | `dict[str, str] \| None` | `None` | 随响应透传的额外 HTTP 头（如 `Retry-After`） |

`http_status` 属性按 code 自动查询（枚举成员直取 / 字符串按值查表 / 未知码默认 400）。

### message 派生优先级（链序）

```
显式传入 message
    ↓ 未传
_message_resolver(code, details)   ← app 层经 register_message_resolver 注册
    ↓ 未注册（测试 / harness 独立运行）
str(code)
```

依赖反转：core 不能反向 import app 层 i18n——`core.i18n` 模块加载时把 `get_message` 注册为回调（`i18n.py` 末行）。

### ErrorCode 值域（P0-2 交付面）

| 分组 | 成员 → HTTP 状态码 |
|---|---|
| 通用 | `INTERNAL_ERROR`→500 · `BAD_REQUEST`→400 · `VALIDATION_ERROR`→422 · `NOT_FOUND`→404 · `METHOD_NOT_ALLOWED`→405 · `CONFLICT`→409 · `UNSUPPORTED_MEDIA_TYPE`→415 · `RATE_LIMITED`→429 |
| 鉴权 | `UNAUTHORIZED`→401 · `FORBIDDEN`→403 · `TOKEN_EXPIRED`→401 · `TOKEN_INVALID`→401 |
| 配置 | `CONFIG_INVALID`→422 |
| LLM 调用 | `LLM_CALL_FAILED`→503 · `LLM_PROVIDER_NOT_SUPPORTED`→422 · `LLM_API_KEY_MISSING`→500 · `LLM_REQUEST_FAILED`→503 · `MODEL_NOT_FOUND`→404 |

后续域 chunk（persona / chat / usage …）按各自域段追加成员，并**同步** `i18n_messages.py`（1:1 对齐由 `test_core_i18n.py` 双向锁强制——新增错误码未注册消息即红）。

### 失败响应 JSON 示例

```json
{
    "success": false,
    "code": "NOT_FOUND",
    "message": "资源不存在",
    "data": null
}
```

## dtos.py — 统一响应封装

### ResultDTO 字段

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `success` | `bool` | `True` | 业务成败 |
| `code` | `str` | `"200"` | 成功恒 "200"；失败见 ErrorCode |
| `message` | `str` | `"成功"` | 面向用户的提示 |
| `data` | `T \| None` | `None` | 业务载荷（泛型） |

### 成功响应 JSON 示例（cursor 分页载荷）

```json
{
    "success": true,
    "code": "200",
    "message": "成功",
    "data": {
        "items": [{"persona_id": "p1", "name": "小鱼"}],
        "total": -1,
        "page": 1,
        "page_size": 20,
        "next_cursor": "eyJpZCI6InAxIiwidHMiOjE2OTAwMDAwMDAwMDB9",
        "has_more": true
    }
}
```

### PageResponse 字段

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `items` | `list[T]` | `[]` | 当前页数据 |
| `total` | `int (ge=-1)` | `0` | offset 模式为完整计数；cursor 模式无精确值时 -1 |
| `page` | `int (ge=1)` | `1` | offset 页码；cursor 模式恒 1 |
| `page_size` | `int (ge=1)` | `20` | 每页条数 |
| `next_cursor` | `str \| None` | `None` | 下一页游标（仅 cursor 模式） |
| `has_more` | `bool` | `False` | 由 `next_cursor` 非空推导 |

构造入口：`PageResponse.of()`（offset 模式）/ `PageResponse.of_cursor()`（cursor 模式）。

### 游标编解码链（顺序）

```
服务层取页末 (id, created_at) → encode_cursor({"id": ..., "ts": epoch_ms})
    → base64url(JSON) 无 padding，对外不透明
客户端原样回传 → decode_cursor(cursor)
    → dict（malformed / 非 dict / 空 → None，调用方按"无游标"处理，不抛异常）
```

## time_utils.py — epoch 毫秒时间

全工程 DB 时间戳列统一 epoch 毫秒整数（BigInteger），避免 `DateTime` 跨方言（MySQL vs SQLite）差异与时区问题。

| 函数 | 语义 |
|---|---|
| `now_ms()` | 当前 epoch 毫秒（ORM `default` / `onupdate` 用 callable） |
| `to_ms(dt)` | `datetime` → epoch 毫秒；`None` 安全返回 0 |
| `from_ms(ms)` | epoch 毫秒 → UTC `datetime`（仅展示/日志，不写库）；`None` → `None` |
| `format_duration_ms(ms)` | 人类可读时长（RATIFIED 三档规格） |

三档规格：`< 1000` → `850毫秒`；`>= 1000` → `3.5秒`（1 位小数）；`>= 60000` → `2分30秒`（秒段 0 省略：`120000 → 2分`）。单位文案可参数化（locale 透传）；负数夹逼 0；`None` 原样返回。

## context.py — 请求级上下文

| ContextVar | 默认值 | 写入方 |
|---|---|---|
| `request_id_var` | `None` | 入口层（UUID4，即 trace_id） |
| `user_id_var` | `None` | 入口层（智能体属主） |
| `session_id_var` | `None` | 会话入口（chat session） |
| `persona_id_var` | `None` | 人格切换面 |
| `locale_var` | `"zh-CN"` | 中间件（Accept-Language 解析） |

`RequestContext`（不可变 dataclass）是五轴快照；`bind_context(**fields)` 提供合并语义的作用域绑定：

```
base = current_context()
    ↓ replace(base, **fields)   ← 仅覆盖显式传入字段；None 同样生效（显式清空）
_apply_context(merged) → 业务代码运行
    ↓ finally
_apply_context(base)   ← 退出恢复（嵌套 / 并发任务天然隔离，ContextVars 复制语义）
```

`context_log_fields()` 输出 `{"request_id", "session_id", "user_id", "persona_id"}`（未设置轴为 `"-"` 占位），供 `logger.contextualize(**...)` 注入——与 logging 的默认 extra 键对齐。

## logging.py + logging_models.py — 三 sink 日志

core 不 import config 域（chunk 边界）：`setup_logging(config: LoggingConfig)` 以 Model Class 为入参契约，app 层 lifespan 由 Settings 映射构造传入（依赖方向 config → core）；测试经 fixture 显式构造（tmp_path 落盘，零真实配置）。

### LoggingConfig 字段

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `level` | `str` | `"INFO"` | 全局级别（校验 loguru 值域，统一大写） |
| `log_file` | `str` | `"logs/app.log"` | 文本日志路径（父目录自动创建） |
| `log_json_file` | `str` | `"logs/app.json.log"` | JSON 日志路径 |
| `rotation` | `str` | `"1 day"` | 轮转策略（每日轮转） |
| `retention` | `str` | `"30 days"` | 保留时长（30 天） |
| `module_levels` | `str` | `""` | 按模块覆盖（`"pkg.mod=DEBUG,app=WARNING"`，最长前缀优先） |
| `text_format` | `str \| None` | `None` | 自定义文本模板（console + 文本文件共用；未含 `{exception}` 自动追加） |
| `colorize_console` | `bool` | `True` | console 彩色输出 |

### 初始化链（顺序）

```
setup_logging(config)
    1. register_secret_masker()          ← patcher 注册（幂等）
    2. parse_module_log_levels(config.module_levels)
    3. logger.remove()                    ← 清默认 sink
    4. add(console sink)                  ← stderr，text_format，colorize
    5. add(文本文件 sink)                 ← rotation/retention，统一文本格式
    6. add(JSON 文件 sink)                ← 同一轮转口径，单行 JSON
    7. logger.configure(extra={"-"...})   ← 上下文轴默认占位
    8. logger.info("日志系统已初始化", ...)
```

### 文本格式（默认模板）

```
2026-11-12 10:30:45 - packages.harness.core.cache - INFO - get:42 - req=req-1 session=s-1 user=u-1 persona=p-1 - 消息文本
```

### JSON 行示例

```json
{
    "timestamp": "2026-11-12T10:30:45.123456+00:00",
    "level": "INFO",
    "logger": "packages.harness.core.cache",
    "message": "缓存命中: key=persona_detail:p1",
    "request_id": "req-1",
    "session_id": "s-1",
    "user_id": "u-1",
    "persona_id": "p-1",
    "module": "packages.harness.core.cache",
    "function": "get",
    "line": 42
}
```

### 脱敏规则（写入前执行，全部 sink 生效）

| 形态 | 示例 → 脱敏后 |
|---|---|
| `key=value` / `key: value`（含引号包裹） | `api_key=sk-1234567890abcdef` → `api_key=***` |
| Bearer 头 | `Authorization: Bearer abcdef123456` → `Authorization: Bearer ***` |
| `sk-` / `sk-ant-` 长前缀 | `sk-proj-abc123...` → `***` |

敏感 key 词表：`password` / `secret` / `token` / `api_key` / `apikey` / `authorization` / `access_token` / `refresh_token`（大小写不敏感）。**长度阈值 8**：短于 8 的值不脱敏（避免误伤 `id=1` / `port=8080`）。

## thread_pool.py — 共享线程池

pools 域是 P0-3 交付面，core 不 import pools/config——共享 `ThreadPoolExecutor` 由 core 自持：

| API | 语义 |
|---|---|
| `init_shared_thread_pool(max_workers=8)` | lifespan 启动期装配（幂等；max_workers 下限 1） |
| `shutdown_shared_thread_pool(wait=True)` | 停机期关闭（幂等；wait=给在途阻塞操作自然收尾窗口） |
| `get_shared_thread_pool()` | 读单例（未初始化 → None → 装饰路径回退） |
| `run_blocking_ops(func, *args, **kwargs)` | 全工程统一的同步/CPU 密集下沉入口 |

### run_blocking_ops 执行链（顺序）

```
pool = 共享单例（None → 事件循环默认执行器，同为线程池）
ctx = contextvars.copy_context()      ← 请求级上下文快照
loop.run_in_executor(pool, lambda: ctx.run(func, *args, **kwargs))
    ← kwargs 透传 + contextvars 传播 + 取消时线程仍执行完（abandon 语义）
```

## background_tasks.py — 后台任务

### BackgroundTaskManager（进程级单例 `background_task_manager`）

注册（`set` 去重 + `add_done_callback` 自动移除）→ 停机（`shutdown(timeout)`）：标记 `is_shutting_down` → `wait_for(gather(*tasks, return_exceptions=True), timeout)` 宽限 drain → 超时对未退任务强制 `cancel()` 并二次 gather 收集。`mark_shutting_down()` 供信号 handler 第一时间置位（不 drain）。

### TaskQueue（内存任务队列，全局单例 `task_queue`）

任务状态机（`core.enums.TaskStatus`）：

```
pending ──worker 拾取──> running ──成功──> completed（result 落位）
   │                        ├──异常──> failed（error 落位，worker 存活继续消费）
   │                        └──取消──> cancelled（停机超时强杀路径）
```

关键语义：

- **上下文快照**：`submit` 时 `contextvars.copy_context()`，worker 侧 `ctx.run(asyncio.ensure_future, coro)` 恢复——任务帧在提交方上下文执行；
- **确定性收口**：`stop(timeout)` 先 `queue.join()` 等待排空（测试经 stop 收口，零轮询）；
- **记录淘汰**：`max_results` FIFO（默认 10000）；`list_tasks(limit)` 倒序最近在前。

TaskRecord 字段：`task_id` / `status` / `result` / `error` / `created_at` / `started_at` / `completed_at` / `name`（单调时钟计时）。

## cache.py — 进程内 TTL 缓存（P0 决策：NO Redis）

P0 无 Redis（锁定决策）——缓存走进程内实现（单实例部署的自然推论；多实例阶段再评审分布式缓存）。API 形态：key_func / prefix / TTL / 降级 / 防穿透，后端存储为进程内表。

### MemoryTTLCache API

| 方法 | 语义 |
|---|---|
| `get(key)` | 读值；过期判 miss 并惰性清除 |
| `set(key, value, ttl_seconds=3600.0)` | 写入；容量满先清过期再 FIFO 弹最旧（epoch-ms 过期判定） |
| `delete(*keys)` | **批量**删除，返回实际删除数（幂等） |
| `invalidate_pattern(pattern)` | **批量**通配失效（fnmatch：`persona_detail:*`） |
| `clear()` / `size()` | 清空 / 计数 |

默认单例：`init_default_cache()` 装配（lifespan）→ 装饰器消费；`get_default_cache()` 未装配返回 None → **降级直通**。

### async_ttl_cache 装饰器链（顺序）

```
key = key_func(*args) 或 prefix:sha256(args+kwargs)
    ↓ 默认缓存已装配？
cache.get(key) 命中 → 直接返回
    ↓ miss（或读异常 best-effort 吞掉）
result = await func(*args, **kwargs)
    ↓ result 非 None 且写不抛
cache.set(key, result, ttl)   ← None 不缓存（防穿透）
```

## enums.py — 跨域通用枚举

- `Locale`：`zh-CN`（默认回退）/ `en`；`SUPPORTED_LOCALES` 为 i18n 回退判定真源；
- `TaskStatus`：`pending` / `running` / `completed` / `failed` / `cancelled`；`ACTIVE_TASK_STATUSES`（pending+running）为停机 drain 打捞面真源。

域专属枚举（persona 状态机 / chat 会话 / usage 维度）由各自域包定义，core 不前置实现。

## i18n — 消息查找

查找链（顺序）：`get_message(key, locale=None, **kwargs)` → `resolve_locale`（显式传入 → `locale_var` contextvar → 默认 `zh-CN`；未知值回退默认）→ `MESSAGES[key][locale]` → 未命中 locale 回退 `zh-CN` 条目 → 仍缺回退 key 本身 → kwargs 插值（失败静默）。

`MESSAGES` 与 `ErrorCode` 1:1 双向对齐（`test_core_i18n.py` 回归锁）；每条目覆盖 `zh-CN` + `en`。

## 测试与门禁

- 测试位置：`src/tests/unit/test_core_*.py`（单元 10 份）+ `src/tests/integration/test_core_wiring.py`（组件协作全链路）；
- 全部 `async def test_`（asyncio_mode=auto；同步测试棘轮零容忍）；零真实配置（logging 经 fixture 构造 `LoggingConfig` + tmp_path 落盘）；TTL 过期经 `ttl=0` 确定性触发（零 sleep）；
- 覆盖率（`pixi run tests-unit` 实测）：harness/core 组 **99.0%**（逐模块：cache/context/dtos/enums/errors/i18n_messages/logging/logging_models/thread_pool/time_utils 100%，logging 链 99%，background_tasks 99%，i18n 86%——剩余为防御分支：ContextVar 兜底 except 与 py3.12 gather-取消语义下的强杀安全网）；
- 运行：`cd backend && pixi run tests-unit`（单元）/ `pixi run tests-all`（全量）/ `pixi run coverage-gates`（逐模块 ≥80% enforce）。

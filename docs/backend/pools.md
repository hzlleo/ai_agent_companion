# pools — 共享池域（P0-3 交付；P1-2 增 SeekDB 检索池）

`backend/src/packages/harness/pools/` 承载全工程长生命周期池的统一装配 / 释放：core 共享线程池启停协调、共享 HTTP 池、LLM 条目池（per-entry 并发信号量 + 熔断状态机——P0-4 LLM 网关消费）、SeekDB 检索池（aiomysql plain pool——P1-2 检索层消费，仅 `SEEKDB_ENABLED=true` 时启动）。P0 决策：**NO Redis**——无 Redis 池（缓存走 core 进程内 TTL 实现）。

## 模块一览

| 模块 | 职责 | 关键导出 |
|---|---|---|
| `manager.py` | PoolManager 生命周期（start / stop，LIFO）+ 全局单例 | `PoolManager` / `init_pool_manager` / `pool_manager` / `get_seekdb_pool` |
| `http_pool.py` | 共享 `httpx.AsyncClient` 构建器（limits / timeouts） | `build_shared_http_client` |
| `llm_pool.py` | 熔断状态机 + per-entry 信号量注册表 + 组合门 | `CircuitBreaker` / `LlmEntryPool` / `entry_gate` |
| `seekdb_compat.py` | aiomysql 类型化访问面（importlib 动态加载 + Protocol 收口 + 密码 env 解析 + 错误基类） | `create_seekdb_pool` / `resolve_seekdb_password` |

依赖方向：pools → config（Settings / PoolsConfig / SeekDBSettings / LlmModelEntry）+ core（thread_pool / errors / time_utils），合法。

## 装配 / 释放链（顺序）

```
lifespan startup（P0-6 装配）:
    init_pool_manager(settings)     # 构造单例（构造注入——不持全局单例引用）
    await pool_manager.start()
        1. core.init_shared_thread_pool(POOLS_THREAD_POOL_SIZE)   # 线程池下沉
        2. build_shared_http_client(POOLS_*)                       # 通用出站 HTTP
        3. LlmEntryPool(POOLS_*)                                    # 信号量/熔断（惰性注册）
        4. create_seekdb_pool(SEEKDB_*)                             # 检索池（仅 ENABLED 时）

lifespan shutdown（LIFO）:
    await pool_manager.stop()
        0. seekdb_pool.close() + wait_closed()   # 最后启动最先关闭
        1. HTTP client aclose()
        2. LlmEntryPool.close()（信号量无需显式释放，清注册状态）
        3. shutdown_shared_thread_pool(wait=True)（在途阻塞操作自然收尾）
```

幂等语义：重复 start 为 no-op（既有池不重建）；未 start / 重复 stop 为 no-op；get_* 未 start 时抛 `INTERNAL_ERROR`（快速暴露编程错误）；`get_seekdb_pool` 在 `SEEKDB_ENABLED=false` 时同样 `INTERNAL_ERROR`。

P0-6 app lifespan 的装配顺序契约：`init_pool_manager → start → load_all_config → init_database → …`（本域只提供 start/stop API，装配是 P0-6 职责）。

## PoolsConfig env 面

| env 键 | 默认值 | 消费方 |
|---|---|---|
| `POOLS_THREAD_POOL_SIZE` | 8 | core 共享线程池（`run_blocking_ops` 下沉） |
| `POOLS_HTTP_MAX_CONNECTIONS` | 100 | 共享 HTTP 池总连接上限 |
| `POOLS_HTTP_MAX_KEEPALIVE_CONNECTIONS` | 20 | 共享 HTTP 池 keepalive 上限 |
| `POOLS_HTTP_CONNECT_TIMEOUT_SECONDS` | 5.0 | HTTP 连接建立超时 |
| `POOLS_HTTP_TIMEOUT_SECONDS` | 30.0 | HTTP 单请求总超时 |
| `POOLS_LLM_SEMAPHORE_LIMIT` | 5 | LLM 全局并发回落值（条目未声明 concurrency 时） |
| `POOLS_LLM_REQUEST_TIMEOUT_SECONDS` | 120 | LLM 单请求超时（P0-4 网关） |
| `POOLS_CB_FAIL_MAX` | 5 | 熔断失败阈值 |
| `POOLS_CB_RESET_TIMEOUT_SECONDS` | 60 | 熔断冷却秒数 |
| `SEEKDB_ENABLED` | false | SeekDB 检索池启动开关（false = 不启动，检索走 SQLite 回退） |
| `SEEKDB_HOST` / `SEEKDB_PORT` | 127.0.0.1 / 2881 | SeekDB 实例地址（MySQL 线协议端口） |
| `SEEKDB_USER` / `SEEKDB_DATABASE` | root / test | 连接用户名 / 库名 |
| `SEEKDB_PASSWORD_ENV` | SEEKDB_PASSWORD | 密码环境变量名（密码本值从该变量读取——env 间接） |
| `SEEKDB_POOL_MIN` / `SEEKDB_POOL_MAX` | 1 / 4 | 检索池常驻 / 上限连接数 |

## 共享 HTTP 池（http_pool.py）

`build_shared_http_client(pools)` → `httpx.AsyncClient(limits=..., timeout=...)`（连接惰性建立；生命周期归 PoolManager——stop 期 aclose）。LLM 端点专用连接隔离由 P0-4 网关按模型条目自建 client 承担（带 per-entry 超时）；本池只管「通用出站」。

## 熔断状态机（llm_pool.CircuitBreaker）

```
closed ──连续失败达 cb_fail_max──> open（冷却计时开始，拒绝一切请求）
   ↑ 成功（计数清零）                 │ 冷却期满（epoch-ms 比较，查询时惰性翻转）
   │                                 ↓
   └────探测成功──── half_open <──冷却期满
                      │ 单次探测放行
                      └─探测失败─> open（冷却重新计时）
```

- 三态：`closed`（正常放行，计数连续失败）/ `open`（冷却期内拒绝）/ `half_open`（冷却期满，放行单次探测）；
- 计时全 epoch-ms（`core.now_ms`；时钟可注入——测试零 sleep 确定性驱动）；
- `allow_request()` / `record_success()` / `record_failure()` / `is_open` / `cooldown_remaining_ms`（退避提示）/ `reset()`（热重建通道）；
- 单事件循环内使用（状态变更无 await 点，无需加锁）。

## per-entry 并发信号量（LlmEntryPool）

- 条目键 = `(provider, base_url 去尾斜杠, model)` 三元组——同端点不同 model 的条目互不阻塞；同端点同 model 复用同一信号量；
- 限值优先级：条目 `concurrency` → `POOLS_LLM_SEMAPHORE_LIMIT` 回落；
- 冲突规则（keep-max）：同键同限值幂等返回；限值上调立即替换信号量（新等待者用新值）；下调保留较大值并告警——下调唯一生效路径是热重建 `reconfigure`（清空注册表后按新配置重建）；
- 熔断器按条目键隔离（坏条目熔断不牵连同端点好模型）；阈值来自 `POOLS_CB_*`；
- `reconfigure(pools)` 热重建：信号量清空 + 熔断记忆保留（避免热重建瞬间旧故障记忆清零导致故障端点被立即打满）；
- `entry_gate(entry)` 组合门（P0-4 网关调用点入口）：熔断预检（open → `LLM_REQUEST_FAILED`，details 含条目键与冷却剩余毫秒）→ 信号量获取 → 释放。

```
async with llm_entry_pool.entry_gate(entry):
    await client.chat(...)
    # 成功 → breaker.record_success()；失败 → breaker.record_failure()
    # （结果上报由 P0-4 网关调用点承担——本域只提供池管道，无 HTTP 调用）
```

## SeekDB 检索池（seekdb_compat.py——P1-2）

`create_seekdb_pool(...)` 创建 aiomysql 连接池（autocommit——单语句自决事务；检索层写路径全部单语句）。**plain pool**：无熔断 / 信号量（检索为可降级路径——存储失败经 SeekDBStore 映射为业务异常，不影响主链路）；启动期即建立 `POOL_MIN` 常驻连接——SeekDB 不可达时 `pool_manager.start()` fail loud（部署顺序契约：SeekDB 先于 app 就绪）。

为什么有 compat 层：aiomysql 0.3.2 无 `py.typed`（mypy 严格模式拒绝直连 import）且不允许新增依赖——经 importlib 动态加载 + Protocol 类型面收口；错误基类（`pymysql.err.Error` re-export）供存储层错误映射。aiomysql 驱动选型风险与回退路径见 memory.md「检索层」。

## 全局单例（manager.py）

```
pool_manager: PoolManager | None    # None 占位 + init_pool_manager 延迟构造
init_pool_manager(settings)         # lifespan 装配（重复调用覆盖；调用方先 stop 旧实例）
get_pool_manager()                  # 运行时读取（支持 monkeypatch）
```

## 测试与门禁

- 测试位置：`src/tests/unit/test_pools_manager.py` / `test_pools_llm_pool.py` / `test_pools_seekdb.py`（3 份）；
- 全部 `async def test_`；Settings 经 `build_isolated_settings` 显式值构造；HTTP 消费经 `httpx.MockTransport`（零真实出站）；熔断状态机经注入固定时钟确定性驱动（零 sleep）；信号量经真实 `asyncio.Semaphore` 并发语义验证（gather 峰值 ≤ 限值）；SeekDB 池经假池工厂 monkeypatch（零真实连接）；
- 覆盖率（`pixi run tests-unit` 实测）：harness/pools 组 **100%**；
- 运行：`cd backend && pixi run tests-unit`。

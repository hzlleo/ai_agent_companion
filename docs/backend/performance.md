# performance — 性能基线（纯本地微基准 + ASGI 进程内冒烟）

本页记录两条**无新依赖、可回归、失败可解释**的性能基线面：

- `scripts/bench_core.py`：核心纯函数的本地微基准（聊天门控 / 关系摘要 /
  语音句切 / VAD / 关系块渲染）；
- `scripts/api_smoke.py`：FastAPI 应用栈的进程内只读端点冒烟（health /
  模型注册表 / 人格列表 / 画布列表）。

两条基线都只用于**同机复跑对照**：绝对耗时随 CPU 型号、频率、容器配额、
同机负载漂移，跨机比较没有意义。基线文件随仓库提交，但任何结论都必须以
同一台机器上重新测量的结果为准。

本页另载**前端生产构建体积基线**与体积门禁（见文末「前端构建产物体积基线
（R4-1）」）：前者是运行期延迟，后者是静态产物字节，两者同为可复跑回归面。

## 测量边界

| 面 | 覆盖 | 明确不覆盖 |
|---|---|---|
| `bench_core.py` | 纯函数 CPU 路径（无 I/O / 无等待 / 无网络 / 无数据库） | 真实 LLM 调用、磁盘、网络、进程间调度 |
| `api_smoke.py` | FastAPI 路由 → 服务层 → 临时 SQLite 的进程内调用链（httpx `ASGITransport`） | 真实端口监听、真实 MySQL/SeekDB、TLS、反向代理、真实 `.env` |

`api_smoke.py` 显式构造隔离 Settings（`_env_file=None`；数据库固定为一次性
临时 SQLite，鉴权令牌取自脚本专用变量），应用实例不依赖开发者 `.env` /
`config.yaml` 的内容；脚本结束删除临时目录并关闭引擎。

`_env_file=None` 只屏蔽 `.env` 文件来源，进程环境变量仍参与解析，因此**无关
分节**（例如本次冒烟根本不消费的流程门控）的非法环境值会让脚本显式拒跑：
导入期 Settings 校验失败时打印单行受控错误（含首个错误字段摘要）并 exit 1，
不展开 traceback。这与真实 app 启动失败同源，属设计内的受控失败——清理该
环境变量即可复跑，不代表端点本身有问题。

`bench_core.py` 的用例同样逐字段显式构造配置模型（初始化参数优先级高于进程
环境）：VAD 用例把 `RealtimeVoiceSettings` 的全部字段写死，开发者 shell 或
`.env` 中的同名变量既不改变被测输入，也不会让基准拒跑。两脚本在导入期或构造
用例模型时遇到 `ValidationError` 都折叠为单行受控错误并 exit 1——对
`bench_core` 而言该路径只在被导入模块自行构造配置时触发。

## 基线表（本次建库实测，同一台机器）

### 纯函数微基准（`bench_core.py`，单位微秒）

门禁判定：median > 基线 median × `max_ratio`（10.0）即红。p95 仅报告，
用于观察抖动。

| 用例 | 基线 median | 基线 p95 | 本次实测 median / p95 |
|---|---:|---:|---:|
| `flow_gate.evaluate_flow_gate` | 1.659 | 2.163 | 1.659 / 2.163 |
| `persona.build_relationship_summary.200_facts` | 356.228 | 383.772 | 356.228 / 383.772 |
| `persona.build_relationship_summary.empty_facts` | 2.943 | 3.360 | 2.943 / 3.360 |
| `voice.split_assistant_sentences.long_text` | 221.407 | 263.124 | 221.407 / 263.124 |
| `voice.energy_vad.feed_60_frames` | 720.510 | 751.046 | 720.510 / 751.046 |
| `chat.render_relationship_block.synthetic` | 3.482 | 3.942 | 3.482 / 3.942 |

用例输入规模：关系摘要 200 条合成关系事实、固定 `now_ms`；句切 60 句中文
长文本、单句上限 80 字符；VAD 为起说 + 语音 + 静音切段的 60 帧合成 PCM
序列整体；关系块为含 5 条事件与 2 条边界约定的合成摘要。

### ASGI 进程内冒烟（`api_smoke.py`，单位毫秒）

门禁判定：p95 > 基线 p95 × `max_ratio`（10.0）即红。

| 端点 | 基线 p50 | 基线 p95 | 本次实测 p50 / p95 |
|---|---:|---:|---:|
| `GET /api/v1/health` | 0.534 | 0.651 | 0.534 / 0.651 |
| `GET /api/v1/models/entries` | 0.578 | 0.648 | 0.578 / 0.648 |
| `GET /api/v1/personas` | 3.098 | 3.587 | 3.098 / 3.587 |
| `GET /api/v1/canvas?persona_id` | 3.596 | 5.184 | 3.596 / 5.184 |

每端点预热 3 次后取样 20 次；全部响应必须为 200，否则报告与门禁都直接
失败（冒烟首先验证可用性，其次才是延迟）。

canvas 端点 p95（5.184 ms）约为 health（0.651 ms）的 8 倍，这一差异有明确
结构原因，不是未解释的抖动：canvas 端点是按 persona 过滤的产物列表查询——
每次请求先解析 persona 作用域，再在临时 SQLite 上对该人格行集做索引范围
扫描并逐行物化 DTO，首样还叠一次冷缓存页面读取（预热只覆盖连接与路由，
不覆盖该查询的首轮页面缓存）。对照 health 的零依赖存活面，这是「有数据库
查询」与「无数据库查询」的结构差异。10× 容忍覆盖此差异；**不以该端点做
绝对性能承诺**，它只用于发现数量级回归。

## 运行与更新

```bash
cd backend && pixi run bench-core                  # 报告（恒 exit 0）
cd backend && pixi run bench-core --self-check     # 注册表 / 基线键集双向一致（不跑计时）
cd backend && pixi run bench-core --gate           # 门禁（median 超基线 × 10 即红）
cd backend && pixi run bench-core --update         # 重建基线（评审后提交）

cd backend && pixi run api-smoke                   # 报告（响应非 200 即 exit 1）
cd backend && pixi run api-smoke --self-check      # 端点 / 基线键集双向一致（不建库）
cd backend && pixi run api-smoke --gate            # 门禁（p95 超基线 × 10 即红）
cd backend && pixi run api-smoke --update          # 重建基线（评审后提交）
```

根门禁链 `pixi run gates` 已包含 `bench-core --self-check`、
`bench-core --gate` 与 `api-smoke --self-check`，位于 prose-pin 之前
（prose-pin 保持链尾）。`api-smoke --gate` 需要建临时库并真实取样，对
同机负载敏感，**不入链**；在部署机或负载可控环境手动执行。

## 阈值策略与 10× 环境容忍

`max_ratio = 10.0` 是环境容忍倍率，不是性能目标。选择理由：

1. 绝对耗时对硬件与调度极敏感，同一份源码在共享 CI、容器 CPU 限流、
   省电模式下的中位数可以相差数倍；
2. 基线的作用是发现**数量级回归**（例如误引入 O(n²) 聚合、循环内 I/O、
   重复 Pydantic 校验），而不是卡住几微秒的抖动；
3. 倍数内只报告比值，倍数外才判红——门禁失败可以直接解释为「实测相对
   基线超过 10 倍」，不会因一次 GC 或邻居任务误报。

p95 在 `bench_core` 中只报告不判定；`api_smoke` 用 p95 判定（请求延迟的
尾部比中位数更能反映路由 / 序列化层的退化），但同样套用 10 倍容忍。

## 何时上调或下调基线

- **上调基线**：确认是环境变化（换机器、容器配额调整、同机长期负载）
  而非代码退化；先在同机复跑至少两次，确认新中位数稳定，再评审后
  `--update` 并提交基线文件。
- **下调基线**：只在确认性能优化真实生效、且同机复跑稳定后执行；下调
  会收紧门禁，必须与优化同批评审。
- **禁止**为了让门禁变绿而静默上调；基线与允许清单一样，只允许在评审后
  调整，不允许绕过。
- 新增用例：先在 `bench_core.py` / `api_smoke.py` 注册表登记，再
  `--update`；`--self-check` 会双向校验键集，遗漏任一侧都会红。

## 与 pools.md 的关系

`docs/backend/pools.md` 记录的是**长生命周期资源池**（线程池 / 共享 HTTP /
LLM 信号量与熔断 / SeekDB 连接池）的装配与限值；本页记录的是这些池**之外**
的纯函数 CPU 路径与进程内路由延迟。两者互补：

- 池配置变化（限值、超时、多 worker 放大）不会改变 `bench_core` 的纯函数
  测量，但会反映在 `api_smoke` 的端到端进程内延迟与真实部署延迟上；
- 池相关的真实网络 / 数据库耗时不在本页范围内（`api_smoke` 用临时 SQLite
  替代真实存储，`bench_core` 完全不触 I/O）；
- 语音实时链路的 VAD / 句切与共享线程池、LLM 并发共享同一进程 CPU，池
  限值调整后建议同时复跑 `bench_core --gate` 与 `api-smoke --gate`。

## 分面预算查询成本（R4-2）

ASR / TTS / voiceprint 的日预算闸在每次出站前执行一次 usage 查询
（`purpose` + 人格 + UTC 日桶；单页 200 行，超出才翻页，页帽 50——有效
上限 10,000 行）。正常量级下为单条索引查询，成本与当日该分面调用数弱相关；
两档均 0 时零查询。实时语音逐句 TTS / 逐段 ASR 会按调用次数触发查询——
预算开启后实时链路多一次 DB 往返，关闭（0）时与既有形态完全一致。该成本
不进入 `bench_core`（纯函数面），需在部署负载下观测时看 `api_smoke` 或
服务端慢日志。

## 与其它门禁的边界

- `bench_core --self-check` / `api-smoke --self-check` 只校验注册表与基线
  键集合，不跑计时、不建库，适合每次门禁链执行；
- `--gate` 只判定数量级回归，不替代 `tests-all` / `coverage-gates` 的正确性
  门禁；
- 基线文件是纯数据（JSON），不引入新依赖、新 env 键或数据库迁移。

## 前端构建产物体积基线（R4-1）

管理台前端（`frontend/`）的生产构建体积是独立回归面：`pixi run bundle-check`
解析 `dist/.vite/manifest.json`，把入口静态闭包（初始加载 JS）与路由级动态
chunk 分开计量。构建产物由 `cd frontend && pixi run build` 生成
（`vite.config.ts` 开启 `build.manifest`）；门禁不重复构建。

### 阈值与触发条件

- 初始加载 JS 总量上限 **1,000,000 字节**（1 MB）：超过即必须继续拆分；
- 动态 chunk 下限 **3** 个：少于 3 个说明路由级拆分未生效；
- dist 缺失 / manifest 缺失或不可解析 / manifest 引用的产物文件缺失均判失败；
- 阈值常量的唯一来源是 `frontend/scripts/check_bundle_budget.mjs`
  （`INITIAL_JS_BUDGET_BYTES` / `MIN_DYNAMIC_CHUNKS`），文档不复制数值口径之外
  的判定逻辑。

门禁输出末行为固定标记
`[bundle-budget] PASS total=<初始 JS 字节> lazy=<动态 chunk 数>`；失败输出
`[bundle-budget] FAIL reason=<原因码>` 并以非 0 退出。

### 当前构建聚合基线（同机实测）

R4-1 将全部路由页改为 `React.lazy` 独立分包，共享壳（`AppShell` /
`ToastProvider` / 令牌门）保持 eager。下列基线为**聚合口径**：初始加载 JS
（`index.html` 入口静态闭包）与动态 chunk 总量分开计量，gzip 用 Node 内置
`zlib.gzipSync`（默认参数）计算，与 `frontend/scripts/check_bundle_budget.mjs`
的 `measure()` 同工具同参数。逐 chunk 的文件名与字节会随内容哈希变化，
不在文档固化——**明细以 `pixi run bundle-check` 输出为准**。

| 指标 | R4-1 前（单 chunk） | 当前（路由级拆分） |
|---|---:|---:|
| 初始加载 JS | 717,443 B（gzip 213,912 B） | 389,799 B（gzip 123,627 B） |
| 动态 chunk | 0 | 25 个 / 340,756 B（gzip 110,888 B） |
| JS 产物合计 | 717,443 B | 730,555 B（gzip 234,515 B） |
| 构建耗时（Vite 自报 `✓ built in`，暖缓存复跑） | 5.96s | 3.08s |

构建耗时口径统一为 Vite 自报 `✓ built in` 的暖缓存复跑（同一棵树上第二次
`pixi run build`），随机器负载与缓存状态波动，只做同机观察、不作门禁；首跑
冷启动数字与热跑不并列（本机冷缓存首跑约 14.8s，仅供对照）。根门禁判定仍
以 `bundle-check` 的初始 JS 上限与动态 chunk 下限为准；样式产物仍为单一文件，
路由拆分未产生独立样式 chunk。

### 运行

```bash
cd frontend && pixi run build           # 生成 dist + manifest
cd frontend && pixi run bundle-check    # 体积门禁（须先 build）
```

CI 的 `frontend` job 在 `Build` 步骤之后执行 `pixi run bundle-check`；本地全链
为 `pixi run build && pixi run bundle-check`。

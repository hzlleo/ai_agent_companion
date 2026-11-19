# 验收 SOP（防偷懒 / 防违规复核）

> 适用：任何切片在合并或交付前的独立复核。门禁命令与工具索引见
> [工具与门禁](README.md#工具与门禁)；工程状态与既有验证数值见
> [LEDGER §5](LEDGER.md#5-工程实现事实恢复续跑所需)。

## 1. 三步固定动作（顺序不可省）

### 步骤一：无证据包不审

1. 打开 `docs/evidence/<slice>-<sha8>.json`；缺失即停止复核，退回执行者。
2. 复算源码树标识：`cd backend && pixi run verify-slice -- --hash-only`，
   与证据包 `sha8` / `source_tree.aggregate_sha256` 对照——不一致说明证据
   对应的是另一份源码。
3. 检查 `result=pass`、`failed_steps=[]`，且每个步骤 `summary_matched=true`
   （退出码 0 但摘要缺失在 `verify_slice` 里已判失败；手工复核同样口径）。
4. 抽查 `docs/evidence/logs/` 中 tests-all / gates 两份日志的尾部摘要与
   JSON 记录一致（防手写 JSON）。

### 步骤二：随机 2 个新符号红探针

1. 从 `backend/red_manifest.toml` 的 `probes` 与本次新增实现中各随机挑 2 个
   符号（不得只挑登记过的示例探针）。
2. 执行 `cd backend && pixi run red-probe --symbol <module:attr>`，必须至少
   一条用例失败并打印 `RED`；出现 `GREEN（探针失效）` 即退回——测试没有
   真正约束该符号。
3. 新增实现若不在冻结基线且无 probe 覆盖，`pixi run red-probe --gate` 必须
   已经红过；复核者确认执行者补的是真 probe（符号与 node id 都存在）。

### 步骤三：diff 越权与既有测试改动审计

1. 按任务书文件白名单逐文件核对 diff；白名单外改动一律要求回退或补授权。
2. 核对 `scripts/.assertion_baseline.json` 与 `pixi run assertion_freeze`
   输出：既有测试的 `assert` / `pytest.raises` 计数不得下降；AST 摘要变化
   逐条对照任务书第 5 节申报。
3. 检查既有测试改动是否只是「等量重写」：断言对象必须仍是被测行为，不得
   把强断言降级为存在性 / 状态码断言。
4. 检查 gates 之外的手工命令：所有验证必须经 pixi；输出被截断（`head` /
   前 N 行）判定成功视为无效。

## 2. 证伪型审查者提示词（复制给独立审查者）

```text
你不是来确认「测试通过」的，而是来寻找能推翻该结论的证据。

输入：
- 任务书（文件白名单 / 机器化 DoD / 六路接线矩阵 / 既有测试改动申报）
- docs/evidence/<slice>-<sha8>.json 与 docs/evidence/logs/
- 本次 diff（由主 Agent 提供；你自己不跑 git）

必须完成的检查：
1. 证据自洽：sha8 与当前源码树复算一致；每步 summary_matched=true；
   日志尾部摘要与 JSON 计数一致。任一处对不上就判 FAIL。
2. 删实现会红：对至少 2 个本次新增/修改的符号执行
   `pixi run red-probe --symbol <module:attr>`，必须见 RED。
3. 跨模块语义断裂：按六路接线矩阵逐路核对（reply / stream_reply /
   工具循环 / 合规重试 / swipe / duty）；「不适用」必须给文件:行号与
   语义理由；同一服务方法复用不算逐路证据。
4. 越权与弱化：白名单外改动、既有测试断言计数下降、prose pin（断言
   实现文案）、字段存在断言、截断尾部判定成功——逐项列出证据。
5. 文档真值：docs-truth 与 surface-check 的结论是否覆盖本次新增 env 键 /
   wire 模型；新增面若走允许清单，reason 是否成立。

输出格式：每项给 PASS / FAIL + 证据（文件:行号 / 命令输出行）。
存在任一 FAIL 时，结论只能是「退回」，不得给「有条件通过」。
```

## 3. 复审模板

### 3.1 删实现会红（红探针证据）

| 符号 | 命令 | 结果（RED/GREEN） | 失败用例数 | 结论 |
|---|---|---|---|---|
| `<module:attr>` | `pixi run red-probe --symbol <module:attr>` | `<RED/GREEN>` | `<n>` | `<PASS/FAIL>` |
| `<module:attr>` | 同上 | | | |

判定：GREEN 即测试未约束该符号；manifest 覆盖门（`--gate`）红过但未补
真 probe 同判 FAIL。

### 3.2 跨模块语义断裂（六路逐路）

| 路径 | 接入点复核 | test id / 不适用行号 | 结论 |
|---|---|---|---|
| `reply` | `<文件:符号>` | `<test id 或 文件:行号 + 理由>` | `<PASS/FAIL>` |
| `stream_reply` | | | |
| 工具循环 | | | |
| 合规重试 | | | |
| `swipe` | | | |
| `duty` | | | |

判定：任一路既无 test id 又无「不适用 + 行号理由」→ FAIL；语义分叉（例如
返回形态、重试策略、持久化边界）必须逐路说明差异是否被测试锁定。

### 3.3 越权与弱化

| 检查项 | 证据 | 结论 |
|---|---|---|
| 文件白名单 | diff 文件清单 vs 任务书第 2 节 | `<PASS/FAIL>` |
| 既有测试改动 | 任务书第 5 节 + `assertion_freeze` 输出 | |
| 断言计数 | 基线 vs 当前（只降不升） | |
| prose pin | `pixi run prose-pin` Top-N 与本次新增断言对照 | |
| 截断 / 空跑 | `verify_slice` JSON 的 `summary_matched` 全 true | |
| 文档真值 | `docs-truth` / `surface-check` 结果 | |

判定：任何一格 FAIL 即退回；「顺手修复」的无关改动即使正确也按越权记录，
由主 Agent 决定是否扩权。

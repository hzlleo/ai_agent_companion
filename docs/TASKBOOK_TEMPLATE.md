# 任务书模板（TASKBOOK）

> 用法：把本文件复制为本次切片的执行任务书，替换全部 `<>` 占位符。任务书是
> 子代理的唯一输入，也是验收时的对照物；口径与门禁命令以
> [LEDGER §5](LEDGER.md#5-工程实现事实恢复续跑所需) 与
> [工具与门禁](README.md#工具与门禁) 为准。执行者禁任何 `git` 命令（主 Agent
> 独占版本控制）。

## 1. 元信息

| 项 | 值 |
|---|---|
| 切片名 | `<slice>` |
| 分支 / 基线 | `<branch>` / `<commit>` |
| 目标 | `<一句话：可观察的完成态>` |
| 非目标（明确不做） | `<越权边界：相邻但不在本切片的能力>` |
| 验收人 | `<主 Agent / 审查者>` |

## 2. 文件白名单

只允许触碰下表路径；目录须写明递归范围。白名单外的任何改动都算越权，
必须回退或在任务书中先行扩权。

| 路径 | 新增 / 修改 | 职责 | 备注 |
|---|---|---|---|
| `<backend/src/packages/harness/<域>/<file>.py>` | 修改 | `<该文件承担的改动>` | `<不可动的相邻面>` |
| `<backend/src/tests/unit/test_<域>.py>` | 新增 | `<测试面>` | 全部 `async def test_` |
| `<docs/backend/<域>.md>` | 修改 | `<文档真相化>` | 只补本切片语义 |

禁改：`<列出必须保持原样的文件，例如 prompts byte-lock / DTO fixture / 既有测试>`。

## 3. 机器化 DoD（逐条命令 + 期望，缺一不可）

「退出码 0」与「输出尾部摘要匹配」必须同时满足；只有退出码 0 而摘要缺失
视为空跑失败（判定实现在 `scripts/verify_slice.py`）。

| 命令 | 期望 | 证据落点 |
|---|---|---|
| `cd backend && pixi run verify-slice -- --slice <slice>` | exit 0；`result=pass`；每步 `summary_matched=true` | `docs/evidence/<slice>-<sha8>.json` + `docs/evidence/logs/` |
| `cd backend && pixi run red-probe --symbol <module:attr>` | exit 0 且输出 `RED`（至少 1 条用例失败；清单外新符号追加 `--tests <nodeid>` 走一次性探针，不写清单） | 终端输出抄入任务书第 7 节 |
| `pixi run gates` | exit 0；尾部 `[prose_pin_check] 报告完成` | `verify-slice` 的 `gates` 步骤日志 |
| `cd backend && pixi run tests-all` | exit 0；`== N passed` | 同上 |
| `cd backend && pixi run coverage-gates` | exit 0；24 组 ≥80% | 同上 |
| `cd backend && pixi run lint` / `pixi run typecheck` | exit 0；`All checks passed` / `Success: no issues found` | 同上 |
| `cd backend && pixi run docs-truth` | exit 0（env 分节表一致 + TODO 类标记不回升） | 同上 |
| `cd backend && pixi run surface-check` | exit 0（新增 wire 模型已登记或豁免留 reason） | 同上 |

迁移 / 并发 / 唯一键 / 混合检索切片额外加：`cd backend && pixi run tests-seekdb`
（活 SeekDB 容器，缺环境即 fail 不 skip）。

## 4. 六路接线矩阵（强制逐路，禁空白）

产品语义变更必须逐路核对下列六条消费路径。每格二选一：给可执行的 test id，
或给「不适用 + 文件:行号 + 语义理由」。不得以「同一服务方法复用」代替逐路
证据（分叉语义恰好在接线层发生）。

| 路径 | 接入点（文件:符号） | test id | 不适用理由（含文件:行号） |
|---|---|---|---|
| `reply` | `<chat/service.py::ChatService.reply>` | `<tests/unit/test_...py::test_...>` | |
| `stream_reply` | `<chat/service.py::ChatService.stream_reply>` | | |
| 工具循环 | `<chat/service.py::ChatService._tool_loop>` | | |
| 合规重试 | `<chat/service.py 合规分支>` | | |
| `swipe` 变体 | `<chat/swipe_service 或 app/api/memories.py>` | | |
| `duty` | `<duty/duty_service.py::_tool_loop>` | | |

## 5. 既有测试改动申报

既有测试原则上不改；确需改动时逐条申报，验收人核对 `assertion_freeze`
（`asserts` / `pytest.raises` 计数只降不升，AST 摘要变化会被报告）。

| 文件 | 用例 | 改动类型（等量重写 / 新增断言 / 删除用例） | 理由 | 断言计数（前 → 后） |
|---|---|---|---|---|
| `<path>` | `<test_name>` | `<类型>` | `<契约变更依据>` | `<n> → <m>` |

删除用例或断言计数下降时，`scripts/assertion_freeze.py --update` 必须与
测试改动同批评审，不得静默下调基线。**整文件删除同样判红**（基线内路径
消失即计入下降，消息为「基线内测试文件缺失」），合法整删必须同批
`--update` 重建基线。

## 6. 反模式黑名单

| 反模式 | 典型形态 | 正确做法 |
|---|---|---|
| 症状式修正（非根修） | 只改文档数字、加特判分支、仅补测试或调松断言让红灯变绿，产生缺陷的机制仍可再次触发 | 修改根因所在的机制/不变量/数据流；客观边界下只能缓解时，显式标注「缓解（非根修）」与根修排期 |
| prose pin | 断言实现源码里的成句文案（`assert reply == "……"`） | 断言可观察语义：协议标签剥离、结构字段、错误码、副作用；文案只做包含性/端到端抽样 |
| 字段存在断言 | `assert "x" in payload` 代替值语义 | 断言具体值 / 类型 / 数量 / 状态迁移 |
| 截断尾部 | 只看输出的前 N 行或 `head` 就判成功 | 读完整输出；以退出码 + 尾部摘要判定（见 `verify_slice.py`） |
| 范围外改动 | 顺手重构 / 全局格式化 / 修无关 typo | 白名单内最小 diff；无关问题记录后在评审说明，不夹带 |
| 批量注释改写 | 正则 / tokenize 批量替换注释措辞 | 逐处读齐上下文（docstring + 描述的实现 + 引用符号）再写；脚本只用于定位（LEDGER 15.2–15.4） |
| 空跑验收 | 命令 exit 0 但没有真实执行摘要 | 摘要缺失即失败；用 `verify-slice` 的证据 JSON 复核 |
| 假探针 | manifest 登记但不实际跑 / 符号不存在的 probe | `red-probe --gate` 校验符号与 node id 真实存在；`--symbol` 必须见 `RED`；`errors>0 且 failed==0` 属导入期假红，须补直接断言该符号的用例 |

## 7. 交付与复核

- 证据包：`docs/evidence/<slice>-<sha8>.json`（sha8 由源码树哈希计算，不用 git；
  哈希剔除 `.egg-info` / 缓存 / 依赖目录，干净副本应复算出同一标识）；
- 证据入库：只有 canonical 切片证据入库；`--only` 调试或手工探针产生的
  临时证据不必提交；
- 红探针输出：`pixi run red-probe --all` 的逐条 `RED` 行；
- 既有测试改动清单：第 5 节表格 + `assertion_freeze` 报告；
- 复核流程：按 [ACCEPTANCE_SOP.md](ACCEPTANCE_SOP.md) 三步固定动作执行。

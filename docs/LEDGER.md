# 工程台账（LEDGER）

> 本文件是**独立可读的工程状态与决策总账**：计划、全部拍板、完成状态、方案取舍、硬约束、遗留事项与恢复指引。
> 任何新会话（或上下文压缩后的续跑）**只读本文件即可无损恢复工程状态**。
> 维护规则：每个切片完成后更新对应状态段；台账与代码事实冲突时以代码为准并立即修台账。

---

## 1. 工程概况

- 工程：`ai_agent_companion`（自建「大肥鱼」式 AI 陪伴智能体——模型无关 / 编排自持 / 多人格）
- 本地路径：`/home/hzlleo/Documents/WorkRelated/Projects/rsi_report/ai_agent_companion`
- 远端：`https://github.com/hzlleo/ai_agent_companion`（主分支 `main`）
- 部署目标机：Clevo DTR（i9-9900K + 64G RAM + RTX 3080-Laptop 16GB + Kali Linux）；开发机（本机）为 i9-12900H 笔记本（无 NVIDIA dGPU——GPU 部署项归属 Clevo）
- 技术栈：Python 3.12 / FastAPI / SQLAlchemy async / MySQL 8（aiomysql）生产 + SQLite 测试 / SeekDB 1.4.0（真引擎，MySQL 协议 2881）/ React 19 + Vite 管理台 / docker 隔离树（tool-sandbox-agent、browser-sidecar）/ pixi 管理环境 / 模型子模组（ModelScope 源：Qwen3-VL-4B-GGUF、Qwen3-Embedding-0.6B-GGUF、Qwen3-ASR-0.6B、Fun-CosyVoice3-0.5B-2512）
- 主 LLM：远端 vLLM 0.32+（OpenAI 兼容端点，工程内仅消费不集成）

## 2. 硬约束（用户全文，必须逐条遵守）

```
1.1 全场景全链路逻辑正常/正确，没有断裂或 BUG。
1.2 开发/修改/测试时使用 pixi，测试必须通过 pixi run。
1.3 工程中不得使用 pixi 管理应由 pyproject.toml 管理的依赖包。
1.4 tests 必须是全量原生异步，禁止为了过测试去改实现；除非从测试反推出的理论实现更符合当前设计。
1.5 没有 lint / mypy / pytest 问题（另外不要盲目对 LLM 提示词做长度合规换行，容易破坏提示词效果）。
1.6 所有测试不得依赖真实工程配置，也不得使用默认配置。
1.7 多测试依赖资源必须共享。
1.8 前后端测试覆盖率必须 80%+，按模块算。
2.1 如需我拍板，必须至少给 3 个方案及优缺点。
2.2 如发现死代码，必须告知并给 3 个方案及优缺点。
2.3 如发现其他性能或代码问题，必须问我是否一起放入 Plan。
3.1 查阅类工作优先给子 Agent 并行处理；完成后及时拉取。
3.2 子 Agent 不超过 5 个。
3.3 子 Agent 严禁再开子代理。
3.4 实现类子 Agent 严禁动范围外文件、严禁任何 git 命令、严禁 stash/pop；它们只能跑自己改动涉及的测试，其他测试只能由主 Agent 来跑。
3.5 没有完成通知前严禁拉取子 Agent 结果。
3.6 不得并行任何实现类子 Agent。
4.1 全工程原生 async；CPU/IO/无法安全 async 化时才用 run_blocking_ops（包括 docker/ 文件夹下的沙盒镜像源码）。
4.2 线程池 / DB 池 / LLM 池 / HTTP 池必须复用公共池（包括 docker/ 文件夹下的沙盒镜像源码）。
4.3 原生 async 优先，实在不能才下沉（包括 docker/ 文件夹下的沙盒镜像源码）。
5. 后端除特殊情况禁止裸 dict，优先 Model Class，且与逻辑分开单独放在 Unit 文件中（包括 docker/ 文件夹下的沙盒镜像源码）。
6. 所有配置项都必须能通过 .env.example / config.example.yaml / extensions_config.example.json / Setup Wizard 管理。
7. docs/ 文档必须贴合代码，且包含完整 JSON 示例、字段说明、链路顺序。
8. 代码查阅必须深度探索，不能流于表面；Agent 输出若截断必须完整读取。
9. 时间统一使用 Linux Epoch 毫秒。
10. 实现严禁偷懒。
11. 所有实现和设计必须覆盖所有使用场景、用户场景、关联场景和类似产品常见场景。
12. 所有实现必须有完整错误处理机制。
13. 需支持批量处理。
14. 日志系统必须完整：Console + 文件、每日轮换、保留 30 天、格式与等级均可配置。
15.1 注释必须 Google Style 中文化；代码洁净；禁止 stub/shim/re-export/lazy import（包括 docker/ 文件夹下的沙盒镜像源码）。
15.2 注释严禁元数据：日期 / 拍板编号 / P-编号 / 批次号 / 会话出处一律不入代码注释——出处与决策史归 git 提交信息与计划档案；注释只保留 WHY / 规则 / 机制 / 约束 / 数值等实质信息。
15.3 修改注释严禁机械式替换：脚本 / 正则 / tokenize 批量改写不得作为产出手段（定位盘点可辅助）；每处修改前必须读齐上下文——整个 docstring / 连续注释段 + 其描述的代码实况 + 段内被引用的符号。
15.4 注释产出必须是完整的、真正语义通顺的、包含完全真实信息的、给人看的正确版注释：完整句段不留残句；不留悬空代指（「该批 / 上述拍板 / 这个问题 / 此前」等须写明具体所指或重组句式消除）；每处写完自读——不了解历史的工程师能否无歧义理解，不能就重写。同规则适用于文档批（docs 改动同 15.2-15.4 口径）。
16. app/ 只做 API 层，其余逻辑全部放 harness/（包括 docker/ 文件夹下的沙盒镜像源码）。
17. backend 和所有沙盒以及每个沙盒之间源码必须是完整隔离的。严禁任何交叉引用或代码共用。
18. 除特殊情况禁止裸 dict（包括已有 Model Class 内的成员），必须优先 Model Class 且与代码逻辑分开单独放在 Unit 文件中。
19. 所有 LLM 提示词必须和 Model Class 及与代码逻辑分开单独放在 Unit 文件中（按模块分）。
```

补充常设纪律（用户明确指示）：
- 子 Agent 并发模型：**Oracle / Deep 型 = 严格串行（任意时刻至多一个）**；**Explore / Librarian 等 Flash 型可并行**（总数 ≤5）。
- 实现类子 Agent 在跑期间，主 Agent 不启动任何新工作流（仅等待）。
- PR 描述一律走 `--body-file` 文件（防 zsh 反引号命令替换）。
- 方言敏感面（迁移 DDL / 唯一键语义 / with_for_update 行锁 / JSON 列）必须**真引擎验证**（SeekDB 容器），不得仅以 SQLite 冒烟代替——此前曾因 SQLite 假绿掩盖 MySQL 错误 5320。
- SeekDB 关联测试已强制真引擎（`src/tests/integration_seekdb`，无环境即 FAIL 不许 skip；CI 起 SeekDB 服务容器随 PR 强制跑）。

## 3. 完成状态总览

### R1（P0–P4）——已完成并合并（main）

| 阶段 | 内容 | 状态 |
|---|---|---|
| P0 骨架 | core/config/database/pools/llm/usage/persona/chat/app/CLI + 585→1319 测试 | ✅ merged（PR #1） |
| P1 记忆 | 三层记忆/梦境/检索（SeekDB Form B 融合）/memories API | ✅ merged（PR #2） |
| P2 管理台 | admin API（usage/models）+ React 管理台（人格/记忆/用量/模型） | ✅ merged（PR #3） |
| P3 运行面 | Telegram/QQ 通道、值班轮、调度器、tool-sandbox docker 树、compose、CI 三 job | ✅ merged（PR #4） |
| P4 语音视觉 | CosyVoice/Qwen3-ASR 语音域、look_at 多模态、B站沙盒工具、QQ 适配、模型子模组、compose 辅助服务 | ✅ merged（PR #5） |
| 审计整改 | 六批 Oracle 深度审计→ALL CLEAN（blocker 5 / major ~35 / minor ~54 / 注释存量 ~240 处）：loguru 掩码链、并发唯一键（active_marker 模式）、per-chat 锁、46 项漂移门、契约门禁、look_at 接线、docs 真相化 | ✅ merged（PR #9） |
| 真网修复 | B站工具真网验证修复 3 bug（WBI 子键名/评论端点/字幕后缀）+ SeekDB 实测对齐（ik 分词/Form B 混合检索精确排序/`1.4.0-100000172026082615` 镜像/root 空密码） | ✅ merged（PR #6/#8） |

### R2（能力扩展）——进行中

| 切片 | 内容 | 状态 |
|---|---|---|
| R2a 地基与感官 | ①admin Bearer 鉴权（豁免 health/QQ webhook）②人格多激活重构（is_primary_marker 唯一键/迁移0008/真引擎重验）③ConfigSectionAdmin 通用基座 ④搜索双 provider（博查+智谱）+chat 工具循环+预算闸+usage search ⑤browser-sidecar 独立树（七工具/SSRF 链/漂移门36项）⑥rhythm 节律层+意图快径 ⑦通道协议 v2（attachments/silent/synthetic） | ✅ merged（PR #10，`fef2b0e`） |
| R2b 人格表达 | ①世界书高级（depth/recursive/groups/probability/cooldown）②性格组件库（33组件×8原型预设+导入API+冲突UI，DTO 68组）③内心独白协议+轻情绪引擎（8维/M层）④swipes 变体（迁移0010）⑤提醒（ff_reminders 迁移0011+扫描作业）+常备指令 | ✅ merged（PR #11 → main `6b54b2a`） |
| R2c 记忆关系 | c1 管线闭环+生命周期（迁移0012/索引真接线/归档/审计二审整改）；c2 人物画像事实链（迁移0013/facts 域/梦境提取阶段/纠错闭环）；c2b provenance 分级紧急修（PEER 枚举/群聊 peer 落档/梦境双排除/skipped_peer 统计/证据面纵深防御）；c3 SKILL层+历史挖矿（迁移 0014 `ff_persona_skills` / `SkillsService` / 梦境 skills 提取阶段 / compose 相关技能块 / API+面板+文档）；c4 自编辑核心记忆+溯源闸（迁移 0015 `ff_core_memory_blocks` / `ff_core_memory_revisions` / `CoreMemoryService` / `edit_core_memory` 工具 + 发送者来源分级结构性工具闸 / compose【自我认知】块 / API+面板+文档，含活 SeekDB 0015 round-trip）；c5 终验 PR | ✅ merged（PR #12 → main `3872504`） |
| R2-defense | 丙+丁：D1 结构隔离 + 宪章五条 + nonce 定界、D2 注入检测 + 角色内反击（毒舌 / 温柔 / 傲娇）、D3 peer 记忆免疫与来源标注、D4 38 条红队回归集 | ✅ merged（PR #13 → main `7fe68a0`；`INJECTION_*` 与 compliance 示例模板生成已并入主线） |
| R2b+ 亲密补强 | 甲套件（角色契约/禁止自我审查/示例台词 few-shot/compliance_guard 1次重试）+乙分轨（roles:intimate 可开端点）+组件库A（120+组件×12类目×3-5台词）+成熟度分层（safe/suggestive/explicit+intimacy_level 0-3+四态场景脚本）+关系连续性A（关系进展摘要注入） | ✅ 代码完成、PR #15 OPEN（`b5846c0`）；甲套件 core + 组件库A/成熟度分层 + 关系连续性A + 乙分轨已全门禁绿；**剩余仅「示例台词 few-shot」** |
| R2d 心流社交 | R2d-1 心流门控（`FLOW_` env 面——判定矩阵 / 连发合并 / 延迟批次经调度器 / 关系与节律输入）+ 群聊多人格（`GROUP_PERSONA_` env 面——轮转 / 冷却 / 重复抑制 / 人格会话归属）；R2d-2 声纹（`VOICEPRINT_` env 面——PIPL 告知 / 同意记录 / 注册 / 校验 / 撤回删除 / 保存期）+ 表情包库与 VLM 标注（`STICKER_` env 面——哈希去重 / 来源授权备注 / 标注状态机 / 通道附件 v2 收发桥 / 前端面板），迁移 0016 | ✅ R2d-1 + R2d-2 代码完成（本分支；全门禁见第 5 节） |
| R2e 通道扩展 | R2e-1 企微智能机器人长连接（WebSocket 订阅 / 心跳 / 退避重连 / 去重 / 回复上下文 / 文本分段 / 素材分片上传 / 配置面 / 装配 / 文档）；R2e-2 抖音 web 通道（浏览器 sidecar 底座：页面采集 / 发送脚本、去重、synthetic、图片附件、指数退避、配置面、装配、文档）+ 画布产物面（迁移 0017 / 服务 / API / 沙箱预览面板 / 导出）；余项：GUI 微信容器化（PoC 门——部署机未跑） | 🔄 R2e-1 ✅ 代码完成、全门禁绿；R2e-2 ✅ 代码完成、全门禁绿（本分支，验证数值见第 5 节）；GUI 微信容器化 ⏳（部署机 PoC 门） |
| R2f 实时语音 | 实时语音全双工会话：WS 协议 + 相位状态机 + 能量/静音 VAD 切段 + 切段转写（节流中途字幕）+ 人格回复句切与逐句 TTS 分片下行 + barge-in（取消在途生成/合成 + 清空队列）+ 会话资源上限（背压/空闲/最大时长/优雅关闭）+ `REALTIME_VOICE_` env 面 + 管理台实时字幕面板 | ✅ 代码完成、全门禁绿（本分支；溢出项与验证数值见第 5 节） |
| R3 | 安全/防穿透/性能深度审计版（独立版本）：admin 鉴权已前置完成，余项：提示注入纵深/SSRF/SeekDB DSL 注入/密钥治理/限流背压/性能基线 | ⏳ |

**当前分支状态**：
- main：R2b（PR #11 → `6b54b2a`）、R2c（PR #12 → `3872504`）、R2-defense（PR #13 → `7fe68a0`）、logprobs 通道（PR #14 → `1305420`）已合并。
- `feat/r2b-plus-intimacy`：PR #15 OPEN（`b5846c0`），承载 R2b+ 甲/乙/组件库/关系连续性；剩余仅示例台词 few-shot。
- `feat/r2f-realtime-voice`：从合并后 main `56cf148` 新建，承载 R2f 实时语音全双工会话（本台账第 5 节「R2f 验证记录」）。
- 本机工作区在 `feat/r2b-plus-intimacy` 分支上堆叠 R2d-1 + R2d-2 改动（未提交/未推送，以本台账第 5 节为准）。

## 4. 全部拍板记录（原文语义）

### R2 设计审计（Oracle，2026-11）P1–P10
- **P1**：仙剑等预设→**性格组件库**（纯 trait 组合、零名字/经历版权）；用户可私载完整卡。
- **P2**：导入冲突=**B+C+全套 UI**（skip 默认+content-hash 幂等+rename/overwrite 显式三选）。
- **P3**：通道协议 v2=**B**（一次扩展：attachments/synthetic/silent；流式降级聚合单发）。
- **P4**：GUI 微信会话键=**B**（联系人备注名+VLM 校验）。
- **P5**：搜索配置面=**B** 且要求抽出共享抽象基建（→ConfigSectionAdmin 基座）。
- **P6**：web_search 消费面=**B**（duty+chat 工具循环+预算闸）。
- **P7**：浏览器 wire=**A**（独立树+REST 同构）。
- **P8**：群聊前置=**A**（R2a 全局多激活重构，一次做掉）。
- **P9**：语音打断=**B**（真全双工+**独立新会话**实现；允许溢出 R3）。
- **P10**：提醒持久化=**B**（ff_reminders+扫描作业）。

### 亲密与丰富度补强
- 道德层吞没=**丙**（甲提示词对抗+后处理/乙模型分轨可开/渐进组合）；组件库=**A**（120+×12类目×3-5示例台词）；18+=**A+C**（成熟度标签+intimacy_level+四态场景脚本）；关系连续性=**A**（关系进展摘要注入）；compliance_guard=1 次强化重试（推荐默认）。

### 提示词攻击防御
- 方案=**丙+丁**（结构隔离宪章/检测+毒舌反击/peer 记忆免疫/30 条红队回归）；E2 毒舌回怼（推荐）；E3 保守阈值（推荐）；E4 首批 30 条（推荐）。

### 其他重要决策（历史）
- 模型无关：主 LLM 远端 vLLM/任意 OpenAI 兼容；辅助模型本地独立服务（D3-B 全原生进程）。
- 记忆存储=SeekDB 统一检索（Q3 方案 A）+MySQL 权威库；知识图谱后置（D2 未来 FalkorDB/Graphiti 可选）。
- 向量库/ES 弃用（SeekDB 覆盖）；Form B 为官方混合检索（DBMS_HYBRID_SEARCH）；零命中=SQL NULL 单元格。
- 微信=企微智能机器人主轨+GUI 微信实验轨（D2-A）；抖音=GUI/浏览器底座（D4）；短信=SmsForwarder 记账推迟（D3）。
- TTS=CosyVoice3-0.5B、ASR=Qwen3-ASR-0.6B、Embedding=Qwen3-Embedding-0.6B 主+bge-m3 备、look_at=Qwen3-VL（GGUF）。
- 管理台鉴权=owner Bearer（ADMIN_AUTH_TOKEN env；空=禁用+启动提示）。
- 数据库迁移链 0001..0017；0012=curated lifecycle；0013=persona facts；0014=persona skills；0015=core memory（blocks + append-only revisions）；0016=voiceprint samples/consents + sticker library；0017=canvas documents。

## 5. 工程实现事实（恢复续跑所需）

### 门禁与验证命令（全部经 pixi）
```
cd backend && pixi run tests-all            # 全量后端测试（当前 2788 passed）
cd backend && pixi run coverage-gates       # 24 组 ≥80% enforce
cd backend && pixi run lint / typecheck / gates（注释门/棘轮/noqa冻结/sandbox-drift；当前 mypy 190 文件零问题）
cd backend && SEEKDB_TEST_URL=127.0.0.1:2881 pixi run pytest src/tests/integration_seekdb -q --asyncio-mode=auto   # 强制真引擎 6 用例（迁移链/并发唯一键/行锁/混合检索）
cd frontend && pixi run test / coverage / lint / build   # 666 passed（高负载机器跑 coverage 可加 -- --maxWorkers=4 稳定）
cd docker/tool-sandbox-agent && pixi run tests-unit；cd docker/browser-sidecar && pixi run tests-unit
根级：pixi run gen-examples（三件套再生成）、pixi run gates
```
- SeekDB 本地容器：`docker run -d --name seekdb-smoke -p 2881:2881 oceanbase/seekdb:1.4.0-100000172026082615`（root 空密码；就绪约 5-60s；网络中断后需重启）。
- DTO 契约门：`scripts/export_dto_schemas.py` + `frontend/src/test/contract/dtoSchemas.json`（当前 94 组）；改后端 wire DTO 必须 `pixi run export-dto-schemas` 再提交，改前端 types 契约测试即红。
- 漂移门：`scripts/check_sandbox_dtos_drift.py`（38 项，三树镜像契约 byte 级）。
- 沙盒树：`docker/tool-sandbox-agent`、`docker/browser-sidecar`（独立 pixi；CI 有对应 job；browser 镜像 ~2GB 不在 CI 构建）。
- 模型子模组：`models/`（ModelScope；权重 lfs 延迟拉取；ModelScope UA 墙：`git config http.https://www.modelscope.cn/.userAgent "git/2.47.0"`）。
- 测试环境：全异步 asyncio_mode=auto；SQLite 共享 fixture；不依赖真实配置；conftest 硬覆盖 env。
- 关键模块坐标：harness/{core,config,database,pools,llm,usage,persona,chat,memory,channels（含 douyin）,canvas,tools,duty,scheduler,voice（含 voiceprint_*）,multimodal（含 sticker_*）,search,emotion,intent,rhythm,reminders}、app/api/{personas,memories,usage,models,voice,voiceprints,look_at,stickers,facts,reminders,directives,skills,core_memory,canvas,health,channels_qq}、前台 pages/{personas,memories,usage,models,stickers,canvas}。

**LLM 网关强制 logprobs 字段**：新版 vLLM 仅在请求显式携带 `logprobs/top_logprobs/prompt_logprobs` 时回传 logprob 数据（置信度计算输入面）——网关非流式请求（含工具）固定携带（缺省 20/20，构造旋钮 None 可省略），流式请求省略 `prompt_logprobs`（端点约束见下条）；响应侧解析为 `content_logprobs/prompt_logprobs` 并派生 `mean_token_logprob/confidence`（exp 均值，观测信号）。已实现于 `feat/llm-confidence-logprobs`（`08dd81e`）；请求构造测试 7 例（含流式 / 工具分支 / 旋钮略省）全异步。

**logprobs 批次修复（P0/P1/P2 闭环）**：流式请求强制省略 `prompt_logprobs`——vLLM 协议校验器对 `stream=true` 且 `prompt_logprobs>0` 直接 400，流式响应也不回传该字段；网关构造新增 `top_logprobs` / `prompt_logprobs` 并接通 `LLM_TOP_LOGPROBS` / `LLM_PROMPT_LOGPROBS` env（int ≥1；空 = 省略）；置信度派生对正均值短路 1.0、过滤 NaN 并保留 -inf，全 NaN 时派生字段为 None；wire `logprob` 改可空并以 `None` 为真缺失判据（空 token / 0.0 合法值保留）；prompt 空 dict / 无整数键元素占位 None 保持位次；畸形条目跳过记 debug 计数（不打印原文）。测试与文档同步（`test_llm_logprobs.py` 全异步）。

### 已完成切片：R2c 记忆能力（核心记忆 + 关系进展 + 前端面板）——已合并（PR#12，`3872504`）
**已完成**：
1. 迁移 0015：`ff_core_memory_blocks`（四块唯一键 + persona 索引）与 `ff_core_memory_revisions`（block_id 级联外键 + block_id / ts_ms 索引；append-only）；降级先删修订表，block_id 索引随表清理（5320 方言面已真引擎验证）。
2. `harness/persona/core_memory.py` / `core_memory_models.py` / `core_memory_orm.py`：四块懒建种子 / 主人编辑（版本 + 修订 + 锁定开关）/ 人格自编辑（锁定与目标全文超限可读拒绝、append 换行追加）/ 修订倒序读取 / `register_core_memory_tools`（`edit_core_memory` + `core_memory_session_id_var`）。
3. `chat/service.py`：compose 链⑦【自我认知】块（事实之后、技能之前）；工具列表按 `sender_provenance` 结构化裁剪（peer 回合剔除自编辑工具）；工具循环会话主键绑定与复位。`chat/prompts.py`：`CORE_MEMORY_BLOCK_TEMPLATE` + `render_core_memory_block`（2400 帽）+ PROMPTS_VERSION 1.5.0 + byte-lock。
4. `app/api/core_memory.py`（GET blocks / PUT blocks/{key} / GET revisions）+ `app/main.py` 受保护路由与通道聊天注册表装配（duty 不注册）。
5. 前端 `CoreMemoryPanel`（四块编辑卡 + 锁定 + 修订抽屉）+ types/api/hooks/msw/测试；DTO fixture 78 组双侧重生成。
6. docs：persona.md 核心记忆一节 / chat.md 链路与工具闸 / README 行 / 本台账。
**验证**：后端 2106 passed / 23 组覆盖率 / mypy 148 文件零问题 / ruff / 门禁集（36 项漂移 + noqa 冻结 + 注释门 + 棘轮）/ 活 SeekDB 6 用例；前端 555 passed / coverage / lint / build。
**剩余**：无。合并后堆叠分支已按 `rebase --onto origin/main <base>` 收敛：R2-defense 并入 main `7fe68a0`、logprobs 并入 main `1305420`、R2b+ 为 PR #15（`b5846c0`，OPEN）。

### 已完成切片：R2b+ 关系连续性 A（关系进展摘要注入）
**已完成**：
1. `harness/persona/relationship_models.py` / `relationship.py`：关系进展 Model Classes（阶段 / 事件 / 摘要 / 锁定阈值表）与确定性纯逻辑——证据唯一来源为事实层 `category=relationship` 且 `status=active` 的现行记录（读 `facts_models` 现成类别 / 置信度 / `updated_at`，不新增存储、不迁移、不改 DTO）；`importance = confidence × 10`、`score = Σ importance × 衰减(年龄分档：≤7d 1.0 / ≤30d 0.75 / ≤90d 0.5 / ≤365d 0.25 / 更早 0.1)`、阶段阶梯 stranger<2.0≤familiar<10.0≤close<24.0≤intimate、`intimacy_level` 夹取 0-3；时间一律由调用方注入 `now_ms`（模块内不读系统时钟）；人格 / 预设 `intimacy_level` 仅存在于生成期，运行期只输出事实派生值（构建函数保留 `intimacy_ceiling` 形参，决策与边界写进模块文档）。
2. `chat/prompts.py`：`RELATIONSHIP_BLOCK_TEMPLATE` + 事件标题 / 事件行 / 边界行模板 + `render_relationship_block`（中文阶段称谓、最近 5 条事件、边界行、1200 字符硬帽与确定性截断）；`PROMPTS_VERSION` 1.7.0 → 1.8.0，既有模板字节零变更（byte-lock 仅增补四条新模板）。
3. `chat/service.py`：compose 链在【自我认知】之后、【相关技能】之前恒注入【关系进展】块（后续块序号顺移，链路十六段）；`reply` / `stream_reply` / 工具循环 / 合规重试分支全部一致携带；关系事实经既有 `FactsService.list_facts` 读取（公共池 + 既有异步面），派生失败记 warning 并降级为空摘要（初识）、不阻断回复；`ChatService` 可注入 `RelationshipThresholdsModel`（缺省 = 锁定表），无新 env 键。
4. 测试：`test_relationship_summary.py`（派生矩阵 31 例（23 个用例含参数化）：空事实 / 混类与历史过滤 / 阶梯上下沿 / 衰减档边界与未来时间戳 / 夹取与阶段回落 / 事件裁剪顺序 / 边界上限 / 确定性 / 时间轴拒绝 / 阈值表不变式）+ `test_chat_relationship.py`（渲染 7 例 / 链序 2 例 / 服务集成 8 例：正常注入 / 阈值注入 / 读失败降级 / 未接线 / 纯内存零 I/O / 流式 / 工具循环 / 合规重试）+ prompts byte-lock 增补。
5. docs：persona.md 关系进展摘要一节（来源 / 公式与阈值表 / 衰减档 / 夹取语义 / 注入位置 / 降级行为 / JSON 示例）、chat.md 组装链十六段完整重写与关系进展块示例、backend README 行、本台账状态行。
**验证**（主 Agent 终验，全部经 pixi）：后端 2573 passed（新增 116 条企微用例）/ 23 组覆盖率门禁（harness/channels 97.0%、app/main 89.8%）/ mypy 175 文件零问题 / ruff 零问题 / 门禁链全绿（注释门 violations=0、同步测试棘轮 +0、noqa 冻结 0、沙盒漂移 36 项一致）/ `gen-examples` 再生成 byte-stable（仅 CHANNELS.WECOM 分节 39 行）。示意命令：`cd backend && pixi run tests-all`、`pixi run coverage-gates`、`pixi run lint`、`pixi run typecheck`、根 `pixi run gates`、根 `pixi run gen-examples`。
**Oracle 深审修复批次（R2b+，P1/P2 全闭环）**：预设生成的 `system_prompt` 改为自包含覆写形态（名字头 /【角色设定】/【性格特点】/ 语气指令 / 四态场景脚本；`personality` 字段仍保留供 UI / 编辑面——覆写路径已修为含人格主体）；工具循环的合规重试基底保留 assistant 工具调用轮与已包裹 tool 轮（不携带工具面、不重跑工具）；swipe 变体重生成派生关系摘要并传 `intimacy_level`（与 reply 同语义）；config.md 补 INJECTION / COMPLIANCE / LLM.INTIMATE 分节；`LLM_INTIMATE_API_KEY` 装载期 strip（仅空白 = 未配置）；合规检测与注入守卫共用归一副本（全角 / 零宽变形不绕过）。**`mes_example` few-shot 注入仍为台账遗留**（本批未改）。

### 已完成切片：R2b+ 组件库 A + 成熟度分层（亲密补强）
**已完成**：
1. `harness/persona/trait_library.py`：`TraitMaturity`（safe/suggestive/explicit）+ 分类值域扩至十二类；组件新增 `maturity` / `sample_lines`（3-5 条 × 1-120 字符）与 `sample_speech` 装载期折叠归一；`SceneScriptSetModel`（日常/暧昧/亲密/冲突四态 × ≤6 条 × 1-200 字符）+ 预设 `intimacy_level` 0-3；`allowed_maturities_for_intimacy_level` 过滤映射 + `render_scene_scripts` 命名段渲染；生成面过滤超限组件、mes_example 24 条上限、场景脚本并入 system 提示；装载期新增示例台词条数与预设成熟度上限校验。
2. `presets/components.yaml`：126 组件 × 十二类目（每类 ≥10）× 3 条示例台词；分布 safe 78 / suggestive 33 / explicit 15。`presets/presets.yaml`：11 预设（8 既有 + 同窗旧友 / 合租室友两个 SFW 原型 + 旧识恋人 explicit 亲密包）× 四态场景脚本 × intimacy_level（10 个 ≤2，亲密包为 3）。
3. `dtos.py` / `app/api/personas.py`：导入请求新增 `intimacy_level_override`（0-3，越界 422）并接入生成期覆盖；不新增人格字段。
4. 前端：类型 / msw / 画廊组件成熟度徽章 + 预设亲密等级上限 + 生成对话框亲密等级选择器（默认预设值、中文提示、过滤预览、提交携带 override）+ 测试。
5. docs：persona.md 组件 schema（含 `sample_speech` 迁移说明）/ 成熟度映射表 / 场景脚本语义 / 计数；README / 本台账；DTO fixture 79 组双侧重生成。
**验证**：后端 2258 passed / 23 组覆盖率（harness/persona 94.9%）/ mypy 153 文件零问题 / ruff / 门禁链全绿（注释门 + noqa 冻结 + 沙盒漂移 36 + sync 棘轮 0）/ DTO fixture 79 组；前端 559 passed / coverage（96.23% statements）/ lint / build。
**剩余**：示例台词 few-shot 注入（chat 提示面）/ 乙分轨 roles:intimate 端点 / 关系连续性 A。

### 已完成切片：R2b+ 乙分轨（亲密回复独立模型通道 roles:intimate）
**已完成**：
1. `harness/llm/intimate_models.py`：`IntimateChannelConfig`（`LLM_INTIMATE_` env 面：enabled / base_url / api_key / model / temperature / min_intimacy_level；默认关闭、必填项判定与缺项清单）+ `IntimateRouteReason` / `IntimateRouteDecisionModel`（裁决契约，模型与逻辑分文件、禁裸 dict）。
2. `harness/llm/intimate_router.py`：确定性纯函数——`evaluate_intimate_route`（开关 → 配置齐全 → 摘要可用 → 阈值，先命中者结论）/ `build_intimate_entry`（条目键 `llm-intimate-channel`）/ `build_intimate_request_params`（通道温度只补调用方未设置的字段）/ `log_incomplete_intimate_config`（半配置单次告警文案）。
3. `harness/llm/gateway.py`：`chat_by_role` / `stream_by_role` 新增 `intimacy_level` 输入并按裁决切换端点；命中路径复用公共 HTTP 池 + 条目池（专属信号量 / 熔断器 / 用量条目名），wire 凭据直接取通道配置不回落全局密钥；半配置 warning 单次闩；调用失败按既有 `LLM_*` 映射上抛，不静默换道。
4. `harness/chat/service.py`：`reply` / `stream_reply` / 工具循环（含收尾调用）/ 合规强化重试一致传递本轮关系摘要等级；摘要未接线传 None、读取失败降级空摘要传 0——两者均回退默认通道。
5. 配置面：`scripts/generate_examples_env.py` 新增 LLM.INTIMATE 分节并再生成 `backend/.env.example`；`wizard_generators.py` 新增「亲密回复通道配置」步骤（前缀重合按定义顺序首次命中归属，模板键不在两步重复）。
6. 测试：`test_llm_intimate_channel.py`（配置面 / 九格路由矩阵 / 阈值覆盖 / 越界拒绝 / 未启用请求字节一致 / 双端点命中与消息工具面不变 / 温度优先级 / 流式命中 / 半配置单次 warning / 失败不换道）+ `test_chat_intimate_channel.py`（reply / stream / 工具循环 / 合规重试一致性、读取失败降级、未接线与纯内存 None）。
7. docs：llm.md「亲密通道」整节（键表 / 启用与半配置策略 / 路由矩阵 / 链路 / 请求示例 / 失败语义 / 默认关闭）、chat.md 路由输入说明、backend README 行、本台账。
**验证**：后端 2339 passed / 23 组覆盖率（harness/llm 98.5%）/ mypy 157 文件零问题 / ruff / 门禁链全绿（注释门 + noqa 冻结 + 沙盒漂移 36 + sync 棘轮 0）；示例模板再生成 byte-stable（改动仅 LLM.INTIMATE 分节）；既有测试仅两处假网关补 `**kwargs`（红队 / chat_service 的调用面契约同步）。

### 已完成切片：R2b+ 甲套件 core（人格输出契约 + 合规守卫）
**已完成**：
1. `chat/prompts.py`：`PERSONA_CONTRACT_TEMPLATE`（角色契约五条）+ `COMPLIANCE_NUDGE_TEMPLATE`（重试提醒）+ 渲染函数与独立版本常量；PROMPTS_VERSION 1.7.0（1.6.0 → 1.7.0）；既有模板字节零变更。
2. `chat/compliance_models.py` / `compliance_guard.py`：助手腔四类规则表（AI 自我声明 2.0 / 客服话术 1.5 / 道歉开场 1.0 / 短回复总结腔 1.0；阈值 2.0）+ 加权检测（确定性、命中片段截断留证）+ 重试采样参数构建（0.8 基线、2.0 夹取、克隆保留其余字段）+ `COMPLIANCE_` env 面（enabled / threshold / temperature_bump / max_retries）。
3. `chat/service.py`：compose 链宪章之后、心情块之前恒注入角色契约（后续块序号顺移一位）；`reply` 在网关响应后、输出协议解析前检测助手腔并做一次强化重试（末尾角色提醒 system 块 + 提升温度、结果无条件采用、不循环；工具循环分支重试不带工具面与 tool 轮；`max_retries=0` 只记日志）；`stream_reply` 不重试（流式边界文档化）。
4. 测试：`test_compliance_guard.py`（25 用例：四类信号矩阵 / 保守阈值 / 正常台词零误报 / 协议标签之后的开场判定 / 重试参数 / env 面）+ `test_chat_compliance.py`（11 用例：契约链位置 / 一次重试携带提醒与提升温度 / 两次违规只重试一次 / 停用与零重试 / 重试文本走协议解析 / 工具循环分支 / 流式不重试）+ prompts byte-lock 增补两条。
5. docs：chat.md「人格输出契约与合规守卫」整节（契约五条 / 规则矩阵 / 重试语义 / 请求 JSON / 流式边界 / `COMPLIANCE_` env 表）+ backend README 行 + 本台账。
**验证**（主 Agent 终验）：后端 2232 passed / 23 组覆盖率（harness/chat 95.9%）/ mypy 153 文件零问题 / ruff / 门禁链全绿（注释门 + noqa 冻结 + 沙盒漂移 36 + sync 棘轮 0）/ 活 SeekDB 6 用例 / DTO fixture 零变更（78 组）；既有测试零改动（prompts 锁仅增补）。
**办结**：`COMPLIANCE_*` 四键已由主 Agent 纳入 `.env.example` / Setup Wizard 生成器（+15 行，闭包测试 51 绿）；`test_llm_logprobs.py` 4 条同步用例已在 logprobs 分支修正（等量重写 + 原文语义不动）。已提交并推送 `feat/r2b-plus-intimacy`（`60eee37`，堆叠于 `08dd81e` → `82cb052`）。

### 已完成切片：R2-defense（丙+丁——提示注入防线）
**已完成**：
1. `chat/injection_models.py` / `injection_guard.py` / `injection_counter_texts.py`：不可信内容 nonce 定界包裹（12 位十六进制，碰撞重生成上限 8 次，内容逐字节保真）+ 规则表加权检测（5 种类 / 24 条规则 / 身份断言 1.5 / 保守阈值 2.0）+ 三风格台词确定性选句。
2. `chat/prompts.py`：`CONSTITUTION_TEMPLATE`（五条）、`UNTRUSTED_WRAP_*_TEMPLATE`、`INJECTION_HINT_TEMPLATE`、来源标签常量；PROMPTS_VERSION 1.6.0（1.5.0 → 1.6.0）；既有模板字节零变更。
3. `chat/service.py`：compose 链人格正典之后恒注入宪章；`compose_messages` 新增 `wrap_current_message` / `current_message_label` / `injection_hint`（包裹当轮入站与最近 10 条历史 user 回合；assistant 与更早回合原文）；`ChatService` 装配 `InjectionGuardSettings`（`INJECTION_` env），reply / stream_reply 检测 + 提示 + 来源标签，工具循环 tool 轮正文定界包裹。
4. `duty/duty_prompts.py`：值班工具反馈恒加「工具结果」定界包裹（与 chat 同语义；不跟随 chat 开关，文档化）。
5. 测试：`test_injection_guard.py`（机制）+ `test_prompt_injection_red_team.py`（38 条模板 / 20 阳性 / 18 阴性 + 服务层开关与流式）+ prompts byte-lock 增补；既有测试仅按新契约更新 3 处断言（tool 反馈包裹正文、resume 续聊当轮包裹）。
6. docs：`backend/defense.md`（攻击面 × 防线 × 缺口矩阵 / 包裹格式 / 检测矩阵 / JSON 示例 / env 表）、chat.md / channels.md / backend README。
**验证**：后端 2189 passed / 23 组覆盖率（harness/chat 95.5%）/ mypy 151 文件 / ruff / 注释门 + 棘轮 + noqa 冻结 + 漂移 36 / 活 SeekDB 6 用例；前端未改（DTO fixture 零变更）。
**办结**：`INJECTION_*` 四键已由主 Agent 纳入 `.env.example` / Setup Wizard 生成器（已验证两处各 4 键）；R2b+ 的 compliance_guard 联动已落地（见上节）。
**Oracle 深审修复批次（P1/P2 全闭环）**：swipe 重生成末位 user 回合复用 compose 包裹开关 + 检测提示（缺省「消息」标签 / 默认台词风格）；检测前归一副本（NFKC + 零宽剔除 + 空白折叠，留证取自副本）并新增 5 条英文规则（规则表 24 → 29 条）；compose 对最近 10 条历史 user 回合定界包裹（标签「历史消息」，assistant / 更早回合原文）；红队集 38 → 47 条（含混淆形与防误报阴性）、prose pin 改结构断言、补工具循环×注入共存与合规重试不变量用例；defense.md 失效部署待办删除并同步矩阵 / 边界，chat.md 同步 compose 链与 swipe 说明。

**Oracle 复审修复批次（第二轮 P1/P2 闭环）**：复审未通过项逐条闭环——(1) 检测前归一由固定零宽字符表改为剔除全部 Unicode `Cf` 类别 + 变体选择符 U+FE00–FE0F（`Mn` 组合附加符保留），红队补软连字符 / word joiner 阳性与「归一后与无混淆基线同分」防回归断言；(2) `tag_mimicry` 的 `<…>` 规则锚点由句首放宽到 8 字符内短前缀（闭合回引 / 权重不变），补短前缀阳性与长前缀阴性，defense.md 边界表述同步；(3) `as an AI` 权重 2.0 → 1.5（与中文身份断言同权），补 4 条良性英文阴性（`as an AI` / `you are now` / `act as` / `pretend to be`），规则表总数不变（29 条）；(4) `SwipeService` 构造注入 `InjectionGuardSettings`（`app/api/memories.py` 装配点显式注入），变体路径的包裹 / 检测 / 提示 / 阈值 / 风格随 `INJECTION_` 配置生效，补停用 / 提示关闭 / 阈值抬高 3 例，chat.md / defense.md kill-switch 表述改为 reply/stream 与 swipe 变体均按配置生效；(5) 红队测试注释订正（assistant 历史原文 / user 历史包裹）。红队 47 → 55 条（29 阳性 / 26 阴性）。**验证**：`pixi run tests-all` → 0（2808 passed）；`pixi run coverage-gates` → 0（24 组全 ≥80，harness/chat 96.3%）；`pixi run lint` → 0；`pixi run typecheck` → 0（191 文件）；根 `pixi run gates` → 0（注释门 / 棘轮 +0 / noqa 冻结 / 漂移 38 项一致）；`backend pixi run gen-examples` → 0（三件套再生成无 diff，幂等）。

### 已完成切片：R2d-1 心流门控 + 群聊多人格（未提交工作区）
**已完成**：
1. `chat/flow_models.py` / `chat/flow_gate.py`：门控 Model Classes（`FLOW_` env 面：连发窗口 / 合并静默与上限 / 回复最小间隔 / 积压阶梯与封顶 / 排程丢失宽限）与全纯函数判定——裁决 `reply | defer | merge | skip`、理由枚举、`now_ms` 由调用方注入；判定矩阵锁定（机器事件不回复、睡眠窗口延迟、连发合并、密度节流、积压延迟、亲近 / 亲密关系优先、节律拟人延迟），连发合并保留最近条目并报告丢弃条数。
2. `chat/persona_rotation_models.py` / `chat/persona_rotation.py`：群聊多人格 Model Classes（`GROUP_PERSONA_` env 面：开关 / 应答人数上限 / 冷却）与确定性选择纯函数——冷却排除、上一轮未发言者优先（轮转）、重复兜底、全冷却选最早应答者；单聊与功能关闭返回空选择。
3. `channels/channel_service.py`：门控接入入站回复管线（判定 → 即时回复 / 调度器一次性作业排程 / 不回复）；延迟批次在同一 per-chat 锁内执行，静默窗口未满或睡眠窗口未结束按剩余时长重排，排程失败与调度器缺失降级立即回复；群聊多人格选择接线（`list_addressable` → 逐人格独立会话 `channel:<ch>:<chat>:persona:<id>` → 逐条投递，逐人格失败隔离）；单聊与非启用形态保持既有单人格即时回复语义。
4. `scheduler.py`：新增一次性延迟作业面 `schedule_once(name, factory, delay_ms)`（到期先移除再执行——不重复触发、同名可复用；负延迟 / 在途重名 fail loud）。
5. app 装配：`Settings.flow` / `Settings.group_persona` 分节；`_startup_channels` 注入节律门面 / 群聊配置 / 调度器提供者 / 关系摘要查询（关系类现行事实派生）；`_startup_scheduler` 在仅 `FLOW_ENABLED` 需要延迟面时也启动调度器。
6. 配置面：`scripts/generate_examples_env.py` 新增 FLOW / GROUP_PERSONA 分节并再生成 `backend/.env.example`；`wizard_generators.py` 新增「心流门控配置」「群聊多人格配置」步骤。
7. 前端：会话页对群聊多人格人格会话显示「群聊多人格 · 人格#N」徽标（会话标题解析 helper + msw/测试）；类型面零 wire 变更（未新增 DTO）。
8. 测试：`test_flow_gate.py`（判定矩阵 / 合并裁剪 / 状态迁移）、`test_persona_rotation.py`（轮转 / 冷却 / 兜底 / 上限）、`test_scheduler_once.py`（一次性作业）、`test_channel_router_flow.py`（延迟 / 合并 / 降级 / 关系优先 / 失败隔离）、`test_channel_router_group.py`（轮转 / 多应答 / 冷却 / 失败隔离 / 回退）。
9. docs：chat.md 心流门控与群聊多人格两节（矩阵 / 配置 / JSON 示例 / 链路）、channels.md 接线段落与归属语义、rhythm.md 消费面、persona.md 选址说明、config.md 分节、backend README、本台账。

**验证**：后端 2385 passed / 23 组覆盖率（harness/channels 96.5%、harness/chat 96.4%）/ mypy 161 文件零问题 / ruff / 门禁链全绿（注释门 + noqa 冻结 + 沙盒漂移 36 + sync 棘轮 0）；前端 564 passed / coverage 96.24% statements / lint / build；示例模板再生成 byte-stable。

### 已完成切片：R2d-2 声纹（PIPL 合规）+ 表情包库与 VLM 标注（本分支未提交工作区）
**已完成**：
1. 迁移 0016：`ff_voiceprint_samples`（人格 × 发送者样本档案 / 哈希去重 / 向量 JSON / 时长采样率）、`ff_voiceprint_consents`（同意文本版本 / 授予时间 / 撤回时间）、`ff_stickers`（人格作用域哈希去重 / MIME / 路径 / 标签 / 描述 / 来源授权备注 / VLM 标注状态与错误）；唯一键与索引按人格作用域建立；SQLite round-trip + SeekDB 迁移链断言同步。
2. `harness/voice/voiceprint_models.py` / `voiceprint_orm.py` / `voiceprint_embedding.py` / `voiceprint_service.py`：PIPL 告知与同意闸（版本 / 授予 / 撤回清样本留凭证）、批量注册（最小样本数 / 时长 / 采样率 / 类型 / 字节 / 哈希去重 / 向量维度）、余弦校验与短音频拒识、档案删除、保存期清理、人格 × 发送者隔离；说话人向量经可配置 HTTP 端点（共享池，无新依赖；未配置即整功能关闭）。
3. `harness/multimodal/sticker_models.py` / `sticker_orm.py` / `sticker_prompts.py` / `sticker_service.py`：批量上传（来源授权备注必填 / 类型 / 字节 / 标签 / 描述 / 哈希去重）、列表与标签状态过滤、修改、单条 / 批量删除、VLM 标注（look_at 角色回退链 + 公共池网关 + 严格 JSON 解析；失败落 failed 可重试）、通道桥 ingest/pick；提示词按约束 19 独立 Unit 并 byte-lock。
4. API：`app/api/voiceprints.py`（notice / consent / withdraw / enroll / verify / profile / delete）与 `app/api/stickers.py`（list / upload / update / delete / batch-delete / annotate / file）；主装配挂 `require_admin_token` 并注册 ORM；CORS 方法面补 PATCH。
5. 通道：`ChannelRouter` 可选 `sticker_ingest_hook` / `sticker_reply_hook`（缺省 None 零影响）；`_startup_channels` 在 `STICKER_ENABLED` 时接线（入站 base64 表情按应答人格收录；本轮带 sticker 且 `STICKER_CHANNEL_REPLY_ENABLED` 时随文本附发最新素材；失败仅告警）。
6. 配置面：`Settings.voiceprint` / `Settings.sticker` 分节 + `.env.example` 再生成（VOICEPRINT_* / STICKER_*）+ Setup Wizard 两个新步骤。
7. 前端：`/stickers` 管理面板（人格 / 状态过滤、多文件上传 + 来源授权备注必填、缩略图、标签编辑、VLM 标注与失败重试、删除确认）+ types / API / hooks / msw / 测试；路由与导航接入。
8. DTO：表情包 10 组 wire 契约导出（fixture 89 组）并双侧重生成。
9. 测试：声纹（服务 / 向量客户端 / API）、表情包（服务 / API / 提示词 / 通道桥 / 页面 / API / hooks / 文件助手）、迁移 0016 SQLite round-trip；SeekDB 迁移链断言扩到 0016。

**验证**（主 Agent 终验，全部经 pixi）：后端 2457 passed / 23 组覆盖率（harness/voice 95.8%、harness/multimodal 93.5%、app/api 82.6%）/ mypy 172 文件零问题 / ruff / 门禁链全绿（注释门 + noqa 冻结 + 沙盒漂移 36 + sync 棘轮 0）/ 活 SeekDB 6 用例（迁移链 0016 升 / 降 base / 复升）；DTO fixture 89 组双侧重生成；前端 596 passed / coverage perFile（96.04% statements / 92.68% branches）/ lint / build；gen-examples 再生成 byte-stable。示意命令：`cd backend && pixi run tests-all`、`pixi run coverage-gates`、`pixi run lint`、`pixi run typecheck`、根 `pixi run gates`、`SEEKDB_TEST_URL=127.0.0.1:2881 pixi run pytest src/tests/integration_seekdb -q --asyncio-mode=auto`、`cd frontend && pixi run test / coverage / lint / build`。

### 已完成切片：R2e-1 企业微信智能机器人长连接通道（本分支未提交工作区）
**已完成**：
1. `harness/channels/wecom/wecom_models.py`：长连接 wire Model Classes（Unit 文件）——订阅 / 心跳 / 消息与事件回调 / 应答 / 回复（markdown）/ 主动推送（markdown、image、voice、file）/ 素材分片上传（init / chunk / finish）命令；入站映射（`single:<userid>` / `group:<chatid>` 会话键、文本 / 语音转写 / 图文混排 / 图片 / 文件 / 视频归一与占位符、媒体 url 附件、`compat_info` → `synthetic`）；chat_id grammar 解析；markdown 20480 字节分段（换行边界优先、UTF-8 硬切不破字符）。
2. `harness/channels/wecom/wecom_client.py`：长连接客户端——BotID + Secret（环境变量间接，缺失即 `CHANNEL_TOKEN_MISSING`）订阅；req_id 应答关联（超时 / 平台拒绝 / 断连均拒绝在途请求）；应用层心跳（间隔 30s 缺省、应答超时判失效）；指数退避重连 + 比例抖动（可被停机即时打断）；单连接不变式（重复 run 幂等）；`disconnected_event` 被踢下线即停止重连；优雅停机（关闭连接 + STOPPED）；连接状态观测日志；素材分片上传（512KB/片、100 片上限、类型体积上限）。连接传输面经 `connector` 注入（生产 websockets，测试进程内 fake）——`websockets` 由声明的 `uvicorn[standard]` 提供，未新增依赖。
3. `harness/channels/wecom/wecom_runner.py`：runner——回调去重（msgid，有界 FIFO 1024）、回复上下文登记与时效判定、有界入站队列 + 固定 worker（解耦收帧循环与 LLM 耗时）、出站文本分段（首段回复 / 续段推送）、附件三源（base64 / url 经共享池 / path 本地）上传与推送、逐附件失败承载、停机收尾。
4. `channel_models.py`：`ChannelType.WECOM`；`config/models.py`：`WecomChannelSettings`（`WECOM_` env 面 12 字段 + 不变式：启用必填 BotID / 退避边界 / 心跳预算 / 抖动值域）并聚合进 `ChannelsSettings`；协议面不引入 `corp_id` / `agent_id` / access_token（长连接只认 BotID + Secret，文档化理由）。
5. `app/main.py`：`_startup_wecom_channel` 装配（客户端 → runner → 适配器注册 → 托管后台任务 → 槽位）；装配期凭据失败记 ERROR 并跳过该通道（不波及其余通道，与 telegram fail-fast 的差异锁定）；`_shutdown_channels` 复用 `_stop_managed_channel_task` 对 telegram / 企微逐一优雅停机（bounded 5s + cancel 兜底）。
6. 配置面：`scripts/generate_examples_env.py` 新增 `CHANNELS.WECOM` 分节并再生成 `backend/.env.example`（+39 行，其余分节 byte-stable）；`wizard_generators.py` 新增「企业微信智能机器人通道配置」步骤；`src/conftest.py` 补 `WECOM_*` / 密钥硬覆盖块。
7. 测试：`tests/wecom_fakes.py`（共享 fake 基建——wire 级连接替身、连接工厂、逐次放行伪休眠、fake 客户端 / 路由器）+ `test_wecom_models.py` / `test_wecom_client.py` / `test_wecom_runner.py` / `test_wecom_settings.py` / `test_app_wecom_channels_lifespan.py`（伪时钟与 fake 连接，零真实网络、零真实等待）；既有 `test_channels_models.py` 枚举值域断言按新成员更新（唯一既有测试改动）。
8. docs：channels.md 企微长连接整节（协议依据 / 模块表 / 入站字段表与 JSON / 归一表 / 出站命令 JSON / 附件映射与平台限制 / 生命周期状态机与语义表 / 错误码表 / 配置表与 .env 示例 / 部署五步 / 测试面）、channels.md 概览与模块表与协议 v2 映射表补列、config.md 分节行、backend README channels 行、根 README 核心特性行、本台账。

**验证**（主 Agent 终验，全部经 pixi）：后端 2573 passed（新增 116 条企微用例）/ 23 组覆盖率门禁（harness/channels 97.0%、app/main 89.8%）/ mypy 175 文件零问题 / ruff 零问题 / 门禁链全绿（注释门 violations=0、同步测试棘轮 +0、noqa 冻结 0、沙盒漂移 36 项一致）/ `gen-examples` 再生成 byte-stable（仅 CHANNELS.WECOM 分节 39 行）。示意命令：`cd backend && pixi run tests-all`、`pixi run coverage-gates`、`pixi run lint`、`pixi run typecheck`、根 `pixi run gates`、根 `pixi run gen-examples`。

### 已完成切片：R2e-2 抖音 web 通道（浏览器底座）+ 画布产物面（本分支未提交工作区）

**拍板覆盖度与取舍（拍板未覆盖部分在此定性，不改写拍板原文）**：

- 抖音：拍板为「GUI/浏览器底座」与「抖音 web（同底座）」。落地为**复用既有浏览器 sidecar**（playwright 持久化 profile + 允许清单 + 路由拦截），并在该底座上补一个通用 `evaluate` 工具（页面上下文 JS 求值，结果规模封顶）——抖音 web 的采集 / 发送是本通道自己的页面脚本，sidecar 不承载任何抖音专有语义。不另起浏览器进程、不新增依赖（playwright / websockets 均在既有树内）。
- Canvas：拍板只给了名称（通道协议 v2 的「贴纸 / Canvas 媒体」语境）。本切片按**渲染 / 导出面**落地（自包含 HTML / SVG / Markdown 产物的持久化 + 沙箱预览 + 单文件导出）——理由是协议 v2 把 Canvas 与「媒体」并列，产物是可投递的媒体面；「会话画布可视化」读法未采用（会在会话 API 之上另建展示层，且无存储需求）。取舍已写入 docs/backend/canvas.md「能力边界」。

**已完成**：
1. `docker/browser-sidecar`：新增第八工具 `evaluate`（`EvaluateArgsModel` script / max_chars / timeout_ms；页面上下文执行；结果 JSON 序列化规模超 `max_chars` 受控失败不截断；超时受控失败）+ `PageFace.evaluate` / `PlaywrightPage.evaluate` + 运行时注册；sidecar 单元测试补 5 例（成功 / 超限 / 脚本异常 / 超时 / 参数边界）。
2. 后端 `harness/tools/browser_models.py` / `browser.py`：镜像 `EvaluateArgsModel` 与 `EVALUATE_TOOL_NAME`（byte 级，漂移门 +2 项 = 38 项）、`build_evaluate_spec` + 注册（duty / chat 双面，八工具）；`scripts/check_sandbox_dtos_drift.py` 与 tools-browser.md 同步。
3. `harness/channels/douyin/`：`douyin_models`（会话键 `im:<conversation_id>` grammar / wire 快照 / 归一：自身回声剔除、系统消息 synthetic、群聊标记、图片 URL 附件、缺失时间兜底、批内重复与已去重过滤、上限截断）、`douyin_scripts`（采集 / 发送 JS 表达式构造——参数 JSON 嵌入、候选选择器与文本哈希兜底、受控 input 事件与发送按钮 / 回车回落）、`douyin_client`（注入 `DouyinBrowserInvoker`——生产为 sidecar 客户端；采集 / 发送 / 打开会话页；失败归一 `CHANNEL_API_ERROR`）、`douyin_runner`（轮询 / 去重窗口 1024 / 逐消息路由隔离 / 指数退避可被停机打断 / 单实例不变式 / 出站文本 + 附件逐条不支持承载）。
4. 配置与装配：`ChannelType.DOUYIN`；`DouyinChannelSettings`（`DOUYIN_` env 面 7 键：enabled / chat_url / conversation_id / poll_interval / max_messages_per_poll / backoff_base / backoff_max；chat_url 协议校验与退避不变式）并聚合进 `ChannelsSettings`；`app/main.py` `_startup_douyin_channel`（底座未就绪只跳过该通道）+ 停机槽位与传导；`.env.example` CHANNELS.DOUYIN 分节再生成；Setup Wizard「抖音 web 通道配置」步骤；conftest 硬覆盖。
5. 画布：迁移 0017 `ff_canvas_documents`（人格作用域 / 标题 / 类型 / 内容 / 修订号 / 时间戳 + 人格 × 更新时间索引）；`harness/canvas/{canvas_models,canvas_orm,canvas_service}`（类型值域 / 上限 / 创建 / 列举 / 读取 / 修订递增 / 幂等删除 / 导出命名与 MIME）；`app/api/canvas.py`（列表 / 创建 / 读取 / 修订 / 删除 / 导出）挂管理面鉴权；CORS 暴露 `Content-Disposition`（前端读导出文件名）。
6. 前端：`/canvas` 面板（人格与类型过滤、沙箱 iframe 预览（无 allow-scripts）、创建、内联修订、导出下载、删除确认、错误面）+ types / api / hooks / msw / 路由 / 导航 / 测试；DTO 契约 5 组导出（fixture 94 组）双侧重生成。
7. 测试：后端 douyin（模型 12 例 / 客户端 7 例 / runner 13 例 / 设置 6 例 / 装配 3 例，fake 基建 `tests/douyin_fakes.py`）+ canvas（服务 8 例 / API 9 例 / 迁移 1 例）+ 浏览器 evaluate（sidecar 5 例 + registry / lifespan 既有断言同步）；SeekDB 迁移链断言扩到 0017。前端 canvas（api 9 例 / hooks 5 例 / 页面 12 例）。
8. docs：channels.md「抖音 web 通道」整节（底座分工 / 装配 / 入站链路与字段表 / 归一 JSON / 出站脚本与边界 / 去重退避 / 错误码 / Settings / 部署五步 / 测试面）、canvas.md 整节（决策 / 模型 / API JSON / 校验 / 安全边界 / 面板 / 测试 / 扩展路径）、config.md 行、backend README 行、根 README 行、LEDGER。

**依赖边界（诚实标注，未验证项）**：

- **GUI 微信容器化 PoC 门与本切片无耦合**：抖音依赖的是浏览器 sidecar（在 CI 与本地均有 hermetic 测试），不是 GUI 微信的 Xorg + uinput 栈。因此本切片不阻塞在该门上；GUI 微信轨道仍待部署机 PoC。
- **真实抖音页面的选择器命中率未在本机验证**（本机测试全部经 fake 调用面）：部署侧需按 channels.md「部署要点 4」执行一次性真网验证（先采集非空、再发送 `{"ok": true}`）。这是本切片唯一未能在开发机闭环的面，已在文档中标注。

**验证**（主 Agent 终验，全部经 pixi）：后端 2630 passed / 24 组覆盖率门禁（harness/canvas 98.7%、harness/channels 97.3%、app/api 81.7%、app/main.py 89.9%）/ mypy 183 文件零问题 / ruff 零问题 / 门禁链全绿（注释门 violations=0、同步测试棘轮 +0、noqa 冻结 0、沙盒漂移 38 项一致）/ 活 SeekDB 6 用例（迁移链 0017 升 / 降 base / 复升）；DTO fixture 94 组双侧重生成；前端 623 passed / coverage perFile（96.08% statements / 92.60% branches）/ lint / build。示意命令：`cd backend && pixi run tests-all`、`pixi run coverage-gates`、`pixi run lint`、`pixi run typecheck`、根 `pixi run gates`、`SEEKDB_TEST_URL=127.0.0.1:2881 pixi run pytest src/tests/integration_seekdb -q --asyncio-mode=auto`、`cd frontend && pixi run test / coverage / lint / build`。

### 已完成切片：R2f 实时语音全双工会话（本分支未提交工作区）

**拍板覆盖度与取舍（拍板未覆盖部分在此定性，不改写拍板原文）**：P9=B「真全双工 + 独立新会话」；本切片按独立 WS 端点 + 独立会话引擎落地（不复用通道 runner）。「流式 ASR / 流式 TTS」在既有服务端点上的实际取材：ASR 为 Qwen3-ASR **批量** audio 端点（无流式通道），中途字幕由**节流的中途批量转写**产生；TTS 为 CosyVoice **整句** WAV（无流式音频通道），逐句合成后按固定字节分片下行、客户端整句聚合解码播放（半句音频永不下发）。两端点的真实连通性留部署侧一次性验证（开发机测试全经 fake，零真实网络）。

**已完成**：
1. `harness/voice/realtime_models.py`：上下行判别联合（start / audio_chunk / end / barge_in / barge_in_ack / ping；ready / partial_transcript / final_transcript / assistant_text / audio_chunk / state / interrupted / error / closed）、VAD 公开参数与会话上限、`RealtimeVoiceSettings`（19 键 + 跨字段不变式：双阈值滞回 / 批量≤队列 / 空闲<最大时长 / 单段>最短语音 / 中途起点≤单段上限）、指标快照、传输与回复生成面协议。
2. `harness/voice/realtime_vad.py`：能量/静音窗口 VAD（起说防抖帧 / 双阈值滞回 / 最短语音丢弃 / 单段上限强制切段 / 冲刷），帧时长累加计时（不读系统时钟，伪时钟可复现同一事件序列）。
3. `harness/voice/realtime_state.py`：相位迁移规则表（idle→listening→thinking→speaking→listening；打断/超时/错误/关闭；closed 吸收态；非法事件不改变相位）。
4. `harness/voice/realtime_sentence.py` + `realtime_audio.py`：句切（标点/收尾符号/长度上限/停顿优先/流式累积）与 PCM16 能量、WAV 封装、字节分片。
5. `harness/voice/realtime_session.py`：会话引擎——有界音频队列（批量出队 / 溢出丢最旧并计数）、VAD 事件处理、切段转写与节流中途转写、`ChatService.stream_reply` 生成、句切与逐句 TTS 分片下行、barge-in（取消生成/合成任务 + 清空句队列 + 下行 interrupted/state + 指标日志）、看门狗（空闲/最大时长，注入时钟与休眠面）、end 尾段冲刷与等待播完后 graceful close、统一任务登记与取消（关闭后任务集合为空）、关闭指标日志。`ChatSessionStreamAdapter` 委托真实 ChatService。
6. `app/api/realtime_voice.py` + `app/main.py`：WS 端点（`GET /api/v1/realtime/voice/ws`）——查询参数 token（浏览器 WS 无法设置自定义头）/ Authorization 头鉴权（复用 `verify_admin_token` 恒时比较）、总闸与 ASR 门控、人格解析、连接级持久化聊天会话（与文字通道同表落库）、Starlette 传输适配；`admin_auth` 抽出 `verify_admin_token` 供 HTTP/WS 共用。豁免面清单同步 README 认证一节。
7. 配置面：`Settings.realtime_voice` 聚合、`build_isolated_settings` 参数、`scripts/generate_examples_env.py` 的 `REALTIME_VOICE` 分节、Setup Wizard「实时语音会话配置」步骤、`src/conftest.py` 19 键硬覆盖。
8. 测试：`tests/realtime_fakes.py`（内存双工 / 脚本语音与聊天面 / 伪时钟 / 即时与挂起休眠面）+ 六个后端测试文件（协议 / VAD / 状态机 / 句切 / 音频 / 会话端到端 27 例 / WS 端点 9 例）——全部异步、零真实网络、零固定等待。
9. 前端：`/realtime` 面板（开始/停止、麦克风 16kHz PCM 分块上行、句级音频聚合与串行播放、实时字幕、打断按钮、状态与错误提示、会话参数展示）+ `types/realtimeVoice` / `api/realtimeVoice`（ws 地址构造）/ `lib/realtimeAudio`（降采样/编码/播放队列）/ `lib/realtimeVoiceClient`（WS 客户端 + 采集）/ 路由与侧栏 + 四个测试文件。
10. 文档：`docs/backend/realtime-voice.md`（协议全示例与字段表 / 状态迁移表 / VAD 矩阵 / 打断时序 / 背压与上限 / 错误码 / 配置表与 .env 片段 / 前端面板 / 部署真机验证步骤 / 测试面 / 边界）、README 索引与认证豁免表、config.md 分节行、voice.md 交叉引用、根 README 特性行、本台账。

**验证记录**（主 Agent 终验，全部经 pixi；命令与退出码）：

- `cd backend && pixi run coverage-gates`（含全量测试）→ 0：**2759 passed**；24 组覆盖率全部 ≥80（harness/voice 95.1%、app/api 83.2%、app/main.py 90.1%）；
- `cd backend && pixi run lint` → 0（`All checks passed!`）；`pixi run typecheck` → 0（190 源文件零问题）；
- 根 `pixi run gates` → 0：注释门 violations=0（hints=30 为既有报告面）、同步测试棘轮 +0、noqa/type:ignore 全基线通过、沙盒漂移 38 项一致；
- `cd backend && pixi run tests-seekdb`（活 SeekDB 1.4.0 容器）→ 0：6 passed（迁移链 0001..0017 / 并发唯一键 / 行锁 / 混合检索——本切片无新增迁移，回归确认既有链不受影响）；
- `cd backend && pixi run gen-examples` → 幂等：仅 `.env.example` 新增 `REALTIME_VOICE` 分节 60 行，`config.example.yaml` / `extensions_config.example.json` 零变化（套件内 byte-stable 测试同步通过）；
- `cd frontend && pixi run test` → 0：**666 passed**；`pixi run coverage` → 0（perFile ≥80 envelope 全过；总 statements 96.21%，realtimeAudio.ts 99.13% lines / 87.5% branches，pages/realtime 95.61% statements）；`pixi run lint` → 0；`pixi run build` → 0（仅既有 chunk>500kB 提示）；
- 说明：本机在终验窗口负载可达 48/20 核，前端 coverage 全量并发跑时既有页面用例偶发 `findBy` 默认 1s 超时（canvas / memories / personas / stickers / reminders hooks，均与本切片无关）；以 `pixi run coverage -- --maxWorkers=4` 复跑全绿（85 文件 / 666 用例）。

**溢出项与未做项**（诚实标注）：真实 ASR / TTS 供应商连通性与首字/首音延迟留部署侧一次性验证（步骤见 docs/backend/realtime-voice.md）；AEC / 回声消除未做；移动端采集与后台挂起优化未做；既有 ASR/TTS 端点无真流式通道（partial 为节流中途批量转写、句级音频整句解码后播放）；实时语音轮次不注入工具面；流式路径沿用合规守卫「不重试」边界。

### 已完成切片：快模型偷懒 / 违规加固（Tier-1 五项 + Tier-2 三项）

**已完成**：

1. 红探针门 `scripts/red_probe.py` + `backend/red_manifest.toml`：临时副本整树复制 + AST 源码体替换（函数体换抛错桩，覆盖 `from x import y` 已绑定引用；conftest 补丁对已绑定名无效），只跑登记的 node id，要求 ≥1 红；`--gate` 要求 `backend/src/{packages,app}` 新源文件被 probe 覆盖或在 grandfathered 基线。首版基线 190 文件 + 3 条真实 probe（flow_gate / persona_rotation / compliance_guard）。
2. `scripts/verify_slice.py` + backend `verify-slice` 任务：按序执行 tests-all / coverage-gates / lint / typecheck / gates，每步校验退出码与输出尾部摘要（摘要缺失即失败）；全量日志与 `docs/evidence/<slice>-<sha8>.json`（文件级 sha256 + 聚合哈希，不依赖 git）；`--with-seekdb` / `--with-frontend` 可选。
3. 文档真相：`generate_examples_env.py` 增加 `--sync-docs` / `--check-docs`（`docs/backend/config.md` 的 `generated:env-sections` 标记块由 `_ENV_SECTIONS` 生成）；新增 `scripts/todo_freeze_check.py`（docs 未完成标记只降不升）；backend `docs-truth` 串两者。
4. `scripts/assertion_freeze.py` + `.assertion_baseline.json`：unit 测试逐文件 assert / pytest.raises 计数与断言 AST 摘要；计数下降即红，摘要变化只报告。
5. `scripts/surface_check.py` + `.surface_allowlist.json`：AST 扫 app/api 路由 response_model 与处理函数注解，解析到 harness 侧 wire 模型后对照导出登记表；未登记且无 reason 的新面即红；同时用既有生成器复核 `.env.example` 键面。存量 40 个未导出面（声纹 / 搜索 / 语音 / 健康 / 查询参数枚举等）如实登记 reason，枚举值域单独注明导出器不覆盖。
6. `scripts/prose_pin_check.py`：断言字符串字面量命中 harness 实现源的疑似 prose pin Top-N + 纯结构断言占比；报告模式恒 exit 0，作为 gates 链尾步骤（其完成标记是 `verify_slice` 的 gates 尾摘要）。
7. `docs/TASKBOOK_TEMPLATE.md`（文件白名单 / 机器化 DoD / 六路接线矩阵 / 既有测试改动申报 / 反模式黑名单）与 `docs/ACCEPTANCE_SOP.md`（三步固定动作 / 证伪型审查者提示词 / 复审模板三节）。
8. `chat/service.py` 两处工具循环 docstring 措辞订正：模块文档与 `_tool_loop` 方法文档改为「骨架与 duty 同源」并写明返回形态分叉（本域 `ToolLoopResultModel` 含尾部轮次；duty 仅返回报告文本），不再宣称机械镜像。

**验证**（主 Agent 终验，全部经 pixi；命令与退出码）：

- `cd backend && pixi run verify-slice -- --slice r2-hardening` → 0：tests-all **2808 passed**；coverage-gates **24 组 ≥80%**；lint `All checks passed!`；typecheck `Success: no issues found in 191 source files`；gates 尾标记命中。证据包 `docs/evidence/r2-hardening-ab346493.json`（sha8=`ab346493`）。
- `cd backend && pixi run red-probe --all` → 0：3/3 `RED`（flow_gate / persona_rotation / compliance_guard 删实现后对应用例失败）。
- `cd backend && pixi run red-probe-gate` → 0：源文件 190 / 冻结基线 190 / probe 3。
- 根 `pixi run gates` → 0：注释门 violations=0；同步测试棘轮 +0；noqa 冻结 0 / 0 / 0 / 7 / 0（scripts 存量 7 未新增）；沙盒漂移 38 项一致；红探针门；文档真相；断言棘轮 8012 asserts / 593 raises / 190 文件；导出面（102 引用 / 62 已登记 / 40 豁免）与 env 键面（686 行）；prose-pin 命中实现源字面量 483 个，分桶 field_or_code=424 / prose=59 / other=0——「疑似 prose pin」按 prose 桶计 **59** 个（CJK 文案归入 prose 桶后修订）（原「483 疑似」是未分桶的命中总数），纯结构断言 7.7%。
- 可选步骤验证：`cd backend && pixi run verify-slice -- --with-seekdb --only tests-seekdb` → 0（活 SeekDB 6 passed）；`--with-frontend --only frontend-test,frontend-coverage,frontend-lint,frontend-build` → 0（666 passed / statements 95.98% / lint / build），两条可选链路均已按摘要校验跑通。
- 基线文件：`backend/red_manifest.toml` 190；`scripts/.assertion_baseline.json` 190 文件 / 8012 / 593；`scripts/.todo_baseline.json` 总数 7；`scripts/.surface_allowlist.json` 40 条。全部只降不升，更新入口分别为 `--prune` / `--update` / `--update`。
- 存量问题处理：40 个未导出面（22 个 DTO / 结果模型 + 18 个 StrEnum 值域）按类别登记允许清单并写明 reason（声纹 PIPL 面、搜索管理面、语音 / 健康 / QQ webhook 等前端未建契约；`EpisodeBatchDelete*` 已由前端别名消费；枚举值域不进 BaseModel schema 导出器），未扩散修改前端契约；prose-pin 报告保留协议字段名类命中（启发式口径透明，不阻断）。

### 已完成切片：加固门禁复审闭环（P1×2 + P2×5）

**已完成**（逐条对应 Oracle 复审意见）：

1. 断言棘轮整删漏检（P1）：`scripts/assertion_freeze.py` 在 `--enforce` 下把「基线内路径但当前扫描缺失」计入 drops 并 exit 1（明细「基线内测试文件缺失（整删须 --update 重建基线）」），与计数下降同口径；报告模式仍只报告；合法整删必须 `--update` 重建基线。
2. 证据 sha 含 egg-info（P1）：`scripts/verify_slice.py` 的 `_iter_hash_files` 跳过任一路径段以 `.egg-info` 结尾的条目（与 `red_probe.py` 复制副本的 `*.egg-info` ignore 口径一致），sha8 不再随 editable 安装漂移；删除孤儿证据 `docs/evidence/r2-hardening-main-ab346493.json` 与其 5 份日志，仅保留 canonical 证据与重跑产物。
3. ad-hoc 探针（P2）：`scripts/red_probe.py --symbol <module:attr> --tests <nodeid[,...]>` 构造一次性探针（不读不写 manifest，先校验符号与 node id 存在）；省略 `--tests` 时保持清单查找语义。
4. prose-pin 分桶（P2）：`scripts/prose_pin_check.py` 把命中文案分三桶——`field_or_code`（无空白无 CJK 的单 token：标识符 / 字典键 / UPPER_SNAKE / 带连字符模型 ID / MIME / 路径）、`prose`（含空白多词或句读标点的自然语言）、`other`；「疑似 prose pin」只统计 prose 桶，Top-N prose 桶优先并逐条标注桶名。
5. 孤儿证据与入库口径（P2）：新增 `docs/evidence/README.md`，`.gitignore` 证据段加注——仅 canonical slice 证据入库，`--only` 调试 / 手工探针证据不必提交。
6. 允许清单规模棘轮（P2）：`scripts/.surface_allowlist.json` 增加 `max_entries`（当前 40）；`surface_check.py --enforce` 在条目数 > 上限时 exit 1，`--update` 重建清单并写 `max_entries=当前条目数`（对现有 40 条 byte-identical，重跑无 diff），陈旧条目报告逻辑保留。
7. 常量探针假红（P2）：模块级常量改惰性占位（导入期不抛；属性访问 / 调用 / 迭代 / 比较 / 运算时抛，`repr` 不抛以免破坏 pytest 内省）；探针输出 `errors>0 且 failed==0` 时打印「疑似导入期假红（collection error 主导）」警示，仍计红。
8. 文档同步：`docs/ACCEPTANCE_SOP.md`（证据复算口径 / ad-hoc 探针用法 / 导入期假红判读 / prose 分桶）、`docs/TASKBOOK_TEMPLATE.md`（DoD、整删申报、证据入库、假探针行）、`docs/README.md`、`backend/pixi.toml` 任务说明。

**验证**（主 Agent 实跑，命令与退出码）：

- 删文件实证（/tmp 副本，含 190 份测试与同一基线）：`python scripts/assertion_freeze.py --enforce` → 0；`rm backend/src/tests/unit/test_chat_prompts.py` 后同一命令 → **1**（明细「test_chat_prompts.py: 基线内测试文件缺失」）；`--report` → 0（仅报告）；`--update` 后 `--enforce` → 0。
- ad-hoc 探针：`cd backend && pixi run red-probe --symbol packages.harness.chat.flow_gate:evaluate_flow_gate --tests tests/unit/test_flow_gate.py::test_disabled_gate_always_replies` → 0，`RED (exit=1, failed=1, errors=0)`；省略 `--tests` 走清单 → 0，`RED (exit=1, failed=2)`。
- 常量惰性桩：`pixi run red-probe --symbol packages.harness.core.enums:SUPPORTED_LOCALES --tests tests/unit/test_core_enums.py::TestLocale::test_supported_locales_frozenset` → 0，`RED (failed=1, errors=0)`（断言级红，非导入崩）。
- 导入期假红标注（/tmp 最小副本：常量在导入期被 `LIMIT + 1` 消费）→ 0，`RED (exit=4, failed=0, errors=1)` 且输出「警示 …: 疑似导入期假红（collection error 主导）」。
- 允许清单棘轮：临时加第 41 条（`max_entries` 仍 40）后 `pixi run surface-check` → **1**（「允许清单条目 41 > max_entries 40」）；恢复后 → 0；`--update` 重跑 `diff` 无差异（byte-identical）。
- 根 `pixi run gates` → 0：注释门 violations=0（hints=30）；同步测试棘轮通过；noqa 冻结 0 / 0 / 0 / 7 / 0；沙盒漂移 38 项一致；红探针门（190 / 190 / 3）；文档真相通过；断言棘轮 8012 asserts / 593 raises / 190 文件（无缺失）；导出面 102 引用 / 62 已登记 / 40 豁免（清单 40 条 / max_entries=40）与 env 键面 686 行；prose-pin 命中 483，分桶 prose=59 / other=0 / field_or_code=424（CJK 文案归入 prose 桶后修订）。
- `cd backend && pixi run verify-slice -- --slice r2-hardening` → 0（tests-all **2808 passed** / coverage-gates **24 组 ≥80%** / lint / typecheck 191 源文件 / gates 全步摘要命中），证据包 `docs/evidence/r2-hardening-55a44a52.json`（sha8=`55a44a52`，640 文件，`result=pass`、`failed_steps=[]`、全部 `summary_matched=true`）。
- 复算：`cd backend && pixi run verify-slice -- --hash-only` → `sha8=55a44a52 files=640`；把源码拷到不含 `*.egg-info` 的干净副本（`rsync` 排除 `.pixi` / `.git` / `node_modules` / `__pycache__` / `*.egg-info` / 缓存与覆盖产物）后在该副本 `cd backend && pixi run verify-slice -- --hash-only` → 同为 `sha8=55a44a52 files=640`（pixi install 在副本里重新生成了 `.egg-info`，标识仍不变，证明剔除口径生效）。

### 已完成切片：加固门禁复审残留闭环（Oracle 复审第 3 轮 P2×2）

- prose 分桶：含 CJK 文案统一归入 prose 桶，全角括号 `【】（）` 补入句读标点；重跑 `pixi run prose-pin` → 命中 483（分桶 prose=59 / other=0 / field_or_code=424），纯结构断言 193 例 / 7.7%。
- 证据目录说明：`docs/evidence/README.md` 增补「`ab346493` 及更早 sha8 由旧哈希口径（含 `.egg-info`）计算，不能用当前代码复算，仅作历史留档」。
- `cd backend && pixi run verify-slice -- --slice r2-hardening` → 0（tests-all 2808 passed / coverage 24 组 / lint / typecheck 191 源文件 / gates 全步摘要命中），证据包 `docs/evidence/r2-hardening-afaaf9a9.json`（sha8=`afaaf9a9`）；`55a44a52` 作为上一提交的历史证据保留，按 README 口径只认与当前 `--hash-only` 一致的一份。

## 6. 遗留与部署台账（非阻塞）

- Clevo 部署窗口：模型 lfs 拉取（~8-10GB）、compose 起 llama-server×2/CosyVoice/Qwen3-ASR 包装面、TTS/ASR 服务端点配置、GUI 微信 PoC 门（Xorg+uinput 栈验证；不通过则整轨推迟）、抖音 web 真网一次性验证（sidecar profile 登录抖音 + allowlist 加 douyin.com + 采集/发送脚本命中确认——单元测试覆盖选择器之外的全部语义）、B站真网复测（hdslb 字幕后缀已修；评论端点已切 /x/v2/reply）、SeekDB 生产实例、QQ NapCat 部署与令牌、ADMIN_AUTH_TOKEN 必设。
- SmsForwarder 短信通道（D3 记账推迟）；WhatsApp/其他平台未列入 R2。
- 声纹（PIPL 合规）与表情包素材法律边界提示义务已随 R2d-2 交付（默认关闭，部署侧按需配置 `VOICEPRINT_*` / `STICKER_*` 并起说话人向量服务）。
- 实时语音（R2f）遗留：真实 ASR / TTS 连通性与首字/首音延迟需部署机一次性验证（步骤见 docs/backend/realtime-voice.md「部署侧一次性真机验证」）；AEC / 回声消除未做（外放可能自触发打断，建议耳机或浏览器平台 AEC）；移动端采集（ScriptProcessor）与后台挂起优化未做；ASR/TTS 服务端点无真流式通道（partial 为节流中途批量转写、句级音频整句解码后播放）；实时语音轮次不注入工具面（需要搜索/沙盒走文字通道）。
- 已知部署机注意：开发机无 NVIDIA GPU（Iris Xe）；GPU/模型项一律在 Clevo 机执行。

## 7. 恢复指引（新会话首读）

1. 读本台账第 3/4/5 节确认分支与拍板；
2. `cd` 工程 → `git status`/`git log --oneline -5` 核对与台账一致；
3. 若上下文中的「当前进行中」段存在未提交工作 → 先按第 5 节剩余步骤收口再开新切片；
4. 任何切片开工前：确认无重型子 Agent 在跑（串行纪律）；任务书必须包含硬约束全文、信息卫生要求（禁止影射其他工程/仓库作为约定来源）、15.2-15.4 注释口径、真引擎验证要求（若涉迁移/并发/唯一键）、范围清单（禁 git、禁范围外文件、禁子代理）。
5. 完成后更新本台账对应状态段并提交。

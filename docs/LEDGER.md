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
| R2b 人格表达 | ①世界书高级（depth/recursive/groups/probability/cooldown）②性格组件库（33组件×8原型预设+导入API+冲突UI，DTO 68组）③内心独白协议+轻情绪引擎（8维/M层）④swipes 变体（迁移0010）⑤提醒（ff_reminders 迁移0011+扫描作业）+常备指令 | ✅ 代码完成，**PR #11 OPEN（未合并）**，分支 `feat/r2b-persona-expression` 最新 `c65b930` |
| R2c 记忆关系 | c1 管线闭环+生命周期（迁移0012/索引真接线/归档/审计二审整改）✅`99317c6`；c2 人物画像事实链（迁移0013/facts 域/梦境提取阶段/纠错闭环）✅`0880cc1`；c2b provenance 分级紧急修（PEER 枚举/群聊 peer 落档/梦境双排除/skipped_peer 统计/证据面纵深防御）✅ 已实现并全量验收；c3 SKILL层+历史挖矿（迁移 0014 `ff_persona_skills` / `SkillsService` / 梦境 skills 提取阶段 / compose 相关技能块 / API+面板+文档）✅ 代码完成、全门禁绿；c4 自编辑核心记忆+溯源闸（迁移 0015 `ff_core_memory_blocks` / `ff_core_memory_revisions` / `CoreMemoryService` / `edit_core_memory` 工具 + 发送者来源分级结构性工具闸 / compose【自我认知】块 / API+面板+文档）✅ 代码完成、全门禁绿（含活 SeekDB 0015 round-trip）；c5 终验PR —— **待做** | 🔄 进行中 |
| R2-defense | 丙+丁：D1 结构隔离 + 宪章五条 + nonce 定界、D2 注入检测 + 角色内反击（毒舌 / 温柔 / 傲娇）、D3 peer 记忆免疫与来源标注、D4 38 条红队回归集 | ✅ 代码完成、全门禁绿（2026-11；`INJECTION_*` 示例模板生成待主 Agent 补齐） |
| R2b+ 亲密补强 | 甲套件（角色契约/禁止自我审查/示例台词 few-shot/compliance_guard 1次重试）+乙分轨（roles:intimate 可开端点）+组件库A（120+组件×12类目×3-5台词）+成熟度分层（safe/suggestive/explicit+intimacy_level 0-3+四态场景脚本）+关系连续性A（关系进展摘要注入） | ⏳ 排 R2-defense 后 |
| R2d 心流社交 | 心流门控/群聊多人格/声纹/表情包库+VLM标注 | ⏳ |
| R2e 通道扩展 | 企微智能机器人（长连接）/GUI 微信容器化（PoC 门）/抖音 web（同底座）/表情包收发/Canvas | ⏳ |
| R2f 实时语音 | 独立全双工会话（VAD+流式ASR+句级TTS+打断），允许溢出 R3 | ⏳ |
| R3 | 安全/防穿透/性能深度审计版（独立版本）：admin 鉴权已前置完成，余项：提示注入纵深/SSRF/SeekDB DSL 注入/密钥治理/限流背压/性能基线 | ⏳ |

**当前分支状态**：
- `feat/r2b-persona-expression`（`c65b930`，PR #11 OPEN）
- `feat/r2c-memory-relationships`（基于 `c65b930` 的堆叠分支；`99317c6` c1、`0880cc1` c2 已推送；c2b / c3 / c4 在工作区未提交）—— PR #11 合并后需 `git rebase main` 收敛再开 R2c PR。

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
- 数据库迁移链 0001..0015；0012=curated lifecycle；0013=persona facts；0014=persona skills；0015=core memory（blocks + append-only revisions）。

## 5. 工程实现事实（恢复续跑所需）

### 门禁与验证命令（全部经 pixi）
```
cd backend && pixi run tests-all            # 全量后端测试（当前 2106 passed）
cd backend && pixi run coverage-gates       # 23 组 ≥80% enforce
cd backend && pixi run lint / typecheck / gates（注释门/棘轮/noqa冻结/sandbox-drift）
cd backend && SEEKDB_TEST_URL=127.0.0.1:2881 pixi run pytest src/tests/integration_seekdb -q --asyncio-mode=auto   # 强制真引擎 6 用例（迁移链/并发唯一键/行锁/混合检索）
cd frontend && pixi run test / coverage / lint / build   # 555 passed
cd docker/tool-sandbox-agent && pixi run tests-unit；cd docker/browser-sidecar && pixi run tests-unit
根级：pixi run gen-examples（三件套再生成）、pixi run gates
```
- SeekDB 本地容器：`docker run -d --name seekdb-smoke -p 2881:2881 oceanbase/seekdb:1.4.0-100000172026082615`（root 空密码；就绪约 5-60s；网络中断后需重启）。
- DTO 契约门：`scripts/export_dto_schemas.py` + `frontend/src/test/contract/dtoSchemas.json`（当前 78 组）；改后端 wire DTO 必须 `pixi run export-dto-schemas` 再提交，改前端 types 契约测试即红。
- 漂移门：`scripts/check_sandbox_dtos_drift.py`（36 项，三树镜像契约 byte 级）。
- 沙盒树：`docker/tool-sandbox-agent`、`docker/browser-sidecar`（独立 pixi；CI 有对应 job；browser 镜像 ~2GB 不在 CI 构建）。
- 模型子模组：`models/`（ModelScope；权重 lfs 延迟拉取；ModelScope UA 墙：`git config http.https://www.modelscope.cn/.userAgent "git/2.47.0"`）。
- 测试环境：全异步 asyncio_mode=auto；SQLite 共享 fixture；不依赖真实配置；conftest 硬覆盖 env。
- 关键模块坐标：harness/{core,config,database,pools,llm,usage,persona,chat,memory,channels,tools,duty,scheduler,voice,multimodal,search,emotion,intent,rhythm,reminders}、app/api/{personas,memories,usage,models,voice,look_at,facts,reminders,directives,skills,core_memory,health,channels_qq}、前台 pages/{personas,memories,usage,models}。

### 当前切片：R2c-4（自编辑核心记忆 + 溯源闸）
**已完成**：
1. 迁移 0015：`ff_core_memory_blocks`（四块唯一键 + persona 索引）与 `ff_core_memory_revisions`（block_id 级联外键 + block_id / ts_ms 索引；append-only）；降级先删修订表，block_id 索引随表清理（5320 方言面已真引擎验证）。
2. `harness/persona/core_memory.py` / `core_memory_models.py` / `core_memory_orm.py`：四块懒建种子 / 主人编辑（版本 + 修订 + 锁定开关）/ 人格自编辑（锁定与目标全文超限可读拒绝、append 换行追加）/ 修订倒序读取 / `register_core_memory_tools`（`edit_core_memory` + `core_memory_session_id_var`）。
3. `chat/service.py`：compose 链⑦【自我认知】块（事实之后、技能之前）；工具列表按 `sender_provenance` 结构化裁剪（peer 回合剔除自编辑工具）；工具循环会话主键绑定与复位。`chat/prompts.py`：`CORE_MEMORY_BLOCK_TEMPLATE` + `render_core_memory_block`（2400 帽）+ PROMPTS_VERSION 1.5.0 + byte-lock。
4. `app/api/core_memory.py`（GET blocks / PUT blocks/{key} / GET revisions）+ `app/main.py` 受保护路由与通道聊天注册表装配（duty 不注册）。
5. 前端 `CoreMemoryPanel`（四块编辑卡 + 锁定 + 修订抽屉）+ types/api/hooks/msw/测试；DTO fixture 78 组双侧重生成。
6. docs：persona.md 核心记忆一节 / chat.md 链路与工具闸 / README 行 / 本台账。
**验证**：后端 2106 passed / 23 组覆盖率 / mypy 148 文件零问题 / ruff / 门禁集（36 项漂移 + noqa 冻结 + 注释门 + 棘轮）/ 活 SeekDB 6 用例；前端 555 passed / coverage / lint / build。
**剩余**：c5 终验 PR（分支收敛 + PR 描述）。

### 已完成切片：R2-defense（丙+丁——提示注入防线）
**已完成**：
1. `chat/injection_models.py` / `injection_guard.py` / `injection_counter_texts.py`：不可信内容 nonce 定界包裹（12 位十六进制，碰撞重生成上限 8 次，内容逐字节保真）+ 规则表加权检测（5 种类 / 24 条规则 / 身份断言 1.5 / 保守阈值 2.0）+ 三风格台词确定性选句。
2. `chat/prompts.py`：`CONSTITUTION_TEMPLATE`（五条）、`UNTRUSTED_WRAP_*_TEMPLATE`、`INJECTION_HINT_TEMPLATE`、来源标签常量；PROMPTS_VERSION 1.6.0（1.5.0 → 1.6.0）；既有模板字节零变更。
3. `chat/service.py`：compose 链人格正典之后恒注入宪章；`compose_messages` 新增 `wrap_current_message` / `current_message_label` / `injection_hint`（只包裹当轮入站，历史原文）；`ChatService` 装配 `InjectionGuardSettings`（`INJECTION_` env），reply / stream_reply 检测 + 提示 + 来源标签，工具循环 tool 轮正文定界包裹。
4. `duty/duty_prompts.py`：值班工具反馈恒加「工具结果」定界包裹（与 chat 同语义；不跟随 chat 开关，文档化）。
5. 测试：`test_injection_guard.py`（机制）+ `test_prompt_injection_red_team.py`（38 条模板 / 20 阳性 / 18 阴性 + 服务层开关与流式）+ prompts byte-lock 增补；既有测试仅按新契约更新 3 处断言（tool 反馈包裹正文、resume 续聊当轮包裹）。
6. docs：`backend/defense.md`（攻击面 × 防线 × 缺口矩阵 / 包裹格式 / 检测矩阵 / JSON 示例 / env 表）、chat.md / channels.md / backend README。
**验证**：后端 2189 passed / 23 组覆盖率（harness/chat 95.5%）/ mypy 151 文件 / ruff / 注释门 + 棘轮 + noqa 冻结 + 漂移 36 / 活 SeekDB 6 用例；前端未改（DTO fixture 零变更）。
**剩余**：`INJECTION_*` 四键纳入 `.env.example` / Setup Wizard 生成器（脚本不在本切片允许清单）；R2b+ 的 compliance_guard 联动按排期衔接。

## 6. 遗留与部署台账（非阻塞）

- Clevo 部署窗口：模型 lfs 拉取（~8-10GB）、compose 起 llama-server×2/CosyVoice/Qwen3-ASR 包装面、TTS/ASR 服务端点配置、GUI 微信 PoC 门（Xorg+uinput 栈验证；不通过则整轨推迟）、B站真网复测（hdslb 字幕后缀已修；评论端点已切 /x/v2/reply）、SeekDB 生产实例、QQ NapCat 部署与令牌、ADMIN_AUTH_TOKEN 必设。
- SmsForwarder 短信通道（D3 记账推迟）；WhatsApp/其他平台未列入 R2。
- 声纹（PIPL 合规）在 R2d；表情包素材法律边界提示义务在 R2d 设计时处理。
- 已知部署机注意：开发机无 NVIDIA GPU（Iris Xe）；GPU/模型项一律在 Clevo 机执行。

## 7. 恢复指引（新会话首读）

1. 读本台账第 3/4/5 节确认分支与拍板；
2. `cd` 工程 → `git status`/`git log --oneline -5` 核对与台账一致；
3. 若上下文中的「当前进行中」段存在未提交工作 → 先按第 5 节剩余步骤收口再开新切片；
4. 任何切片开工前：确认无重型子 Agent 在跑（串行纪律）；任务书必须包含硬约束全文、信息卫生要求（禁「参考工程」类字样）、15.2-15.4 注释口径、真引擎验证要求（若涉迁移/并发/唯一键）、范围清单（禁 git、禁范围外文件、禁子代理）。
5. 完成后更新本台账对应状态段并提交。

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
- 技术栈：Python 3.12 / FastAPI / SQLAlchemy async / SeekDB 1.4.0（权威 + 检索单栈，MySQL 协议 2881，aiomysql 驱动）+ SQLite 测试/ React 19 + Vite 管理台 / docker 隔离树（tool-sandbox-agent、browser-sidecar）/ pixi 管理环境 / 模型子模组（ModelScope 源：Qwen3-VL-4B-GGUF、Qwen3-Embedding-0.6B-GGUF、Qwen3-ASR-0.6B、Fun-CosyVoice3-0.5B-2512）
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
20. 修正必须根修（root fix）：任何缺陷修正必须消除根因（被破坏的不变量、机制或数据流），禁止以症状打补丁、文档豁免、特例分支或仅补测试掩盖；确因客观边界只能临时缓解时，提交与台账必须显式标注「缓解（非根修）」并给出根修排期；复审发现非根修即视为未修复。
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

### R2（能力扩展）——已交付（归档于 `e95f5fe`）

| 切片 | 内容 | 状态 |
|---|---|---|
| R2a 地基与感官 | ①admin Bearer 鉴权（豁免 health/QQ webhook）②人格多激活重构（is_primary_marker 唯一键/迁移0008/真引擎重验）③ConfigSectionAdmin 通用基座 ④搜索双 provider（博查+智谱）+chat 工具循环+预算闸+usage search ⑤browser-sidecar 独立树（七工具/SSRF 链/漂移门36项）⑥rhythm 节律层+意图快径 ⑦通道协议 v2（attachments/silent/synthetic） | ✅ merged（PR #10，`fef2b0e`） |
| R2b 人格表达 | ①世界书高级（depth/recursive/groups/probability/cooldown）②性格组件库（33组件×8原型预设+导入API+冲突UI，DTO 68组）③内心独白协议+轻情绪引擎（8维/M层）④swipes 变体（迁移0010）⑤提醒（ff_reminders 迁移0011+扫描作业）+常备指令 | ✅ merged（PR #11 → main `6b54b2a`） |
| R2c 记忆关系 | c1 管线闭环+生命周期（迁移0012/索引真接线/归档/审计二审整改）；c2 人物画像事实链（迁移0013/facts 域/梦境提取阶段/纠错闭环）；c2b provenance 分级紧急修（PEER 枚举/群聊 peer 落档/梦境双排除/skipped_peer 统计/证据面纵深防御）；c3 SKILL层+历史挖矿（迁移 0014 `ff_persona_skills` / `SkillsService` / 梦境 skills 提取阶段 / compose 相关技能块 / API+面板+文档）；c4 自编辑核心记忆+溯源闸（迁移 0015 `ff_core_memory_blocks` / `ff_core_memory_revisions` / `CoreMemoryService` / `edit_core_memory` 工具 + 发送者来源分级结构性工具闸 / compose【自我认知】块 / API+面板+文档，含活 SeekDB 0015 round-trip）；c5 终验 PR | ✅ merged（PR #12 → main `3872504`） |
| R2-defense | 丙+丁：D1 结构隔离 + 宪章五条 + nonce 定界、D2 注入检测 + 角色内反击（毒舌 / 温柔 / 傲娇）、D3 peer 记忆免疫与来源标注、D4 38 条红队回归集 | ✅ merged（PR #13 → main `7fe68a0`；`INJECTION_*` 与 compliance 示例模板生成已并入主线） |
| R2b+ 亲密补强 | 甲套件（角色契约/禁止自我审查/示例台词 few-shot/compliance_guard 1次重试）+乙分轨（roles:intimate 可开端点）+组件库A（120+组件×12类目×3-5台词）+成熟度分层（safe/suggestive/explicit+intimacy_level 0-3+四态场景脚本）+关系连续性A（关系进展摘要注入） | ✅ merged（PR #15）；四切片 + 逐批评审修复全部 PASS；示例台词已由组件库生成期折叠 mes_example 落地（证据见第 5 节） |
| R2d 心流社交 | R2d-1 心流门控（`FLOW_` env 面——判定矩阵 / 连发合并 / 延迟批次经调度器 / 关系与节律输入）+ 群聊多人格（`GROUP_PERSONA_` env 面——轮转 / 冷却 / 重复抑制 / 人格会话归属）；R2d-2 声纹（`VOICEPRINT_` env 面——PIPL 告知 / 同意记录 / 注册 / 校验 / 撤回删除 / 保存期）+ 表情包库与 VLM 标注（`STICKER_` env 面——哈希去重 / 来源授权备注 / 标注状态机 / 通道附件 v2 收发桥 / 前端面板），迁移 0016 | ✅ merged（PR #16；复审修复 PR #29）；全门禁与证据见第 5 节 |
| R2e 通道扩展 | R2e-1 企微智能机器人长连接（WebSocket 订阅 / 心跳 / 退避重连 / 去重 / 回复上下文 / 文本分段 / 素材分片上传 / 配置面 / 装配 / 文档）；R2e-2 抖音 web 通道（浏览器 sidecar 底座：页面采集 / 发送脚本、去重、synthetic、图片附件、指数退避、配置面、装配、文档）+ 画布产物面（迁移 0017 / 服务 / API / 沙箱预览面板 / 导出）；余项：GUI 微信容器化（PoC 门制品与本机栈实测已交付；通道本体与 Live 待后续阶段） | 🔄 R2e-1 ✅ merged（PR #16 的 WeCom 提交）；R2e-2 ✅ merged（PR #17；复审修复 PR #30）；GUI 微信容器化 ✅ 本机栈 PoC PASS（证据 `docs/evidence/poc/wechat-gui-stack-LeoWorkLaptop-20261126T025721Z.json`；部署机复跑 + 微信客户端 Live 随统一阶段，见第 6 节） |
| R2f 实时语音 | 实时语音全双工会话：WS 协议 + 相位状态机 + 能量/静音 VAD 切段 + 切段转写（节流中途字幕）+ 人格回复句切与逐句 TTS 分片下行 + barge-in（取消在途生成/合成 + 清空队列）+ 会话资源上限（背压/空闲/最大时长/优雅关闭）+ `REALTIME_VOICE_` env 面 + 管理台实时字幕面板 | ✅ merged（PR #18；复审修复 PR #31）；溢出项与验证数值见第 5 节 |
| R3-1 安全纵深 | 提示注入纵深 / SSRF 对称 / 密钥与鉴权治理 / base64 载荷上限 / 日志掩码（P1×2 + P2×5） | ✅ merged（PR #35；证据 `e8232b42`） |
| R3-2 限流与背压 | QQ 入站有界 / 实时语音 TTS 队列与会话帽 / LLM 日预算闸 / 多 worker 语义 / 最小观测面 / 前端 Node 护栏与构建体积基线（P1×1 + P2×7；复审 6×P2 闭环） | ✅ merged（PR #36；证据 `fe261c9a`；PR body 引用的 `0276c1a5` 为首提交树证据，终态以 `fe261c9a` 为准） |
| R3-3 性能基线 | `bench_core.py` 6 项纯函数微基准 + `api_smoke.py` 4 端点进程内冒烟 + 门禁链落位（复审 4×P2 闭环；终局复核 7×P2 闭环） | ✅ merged（PR #37 → main `3f313a6`；评审 head `66b14b3`，终态含 `ecb7714`/`ee9381a`；canonical 证据 `16b07c7c`，历史 `bc71f261`） |

| R4 体验+平台+规模 | R4-1 前端体积治理（路由级 lazy split + 体积门）；R4-2 按 purpose 预算分面（ASR/TTS/voiceprint）；R4-3 实时语音增强（真流式 ASR/TTS + AEC + 移动端采集）；R4-4 SmsForwarder 通道；R4-5 WhatsApp Cloud API 通道；R4-6 GUI 微信容器化（R4-6a PoC 门制品 + 本机栈实测，已合并 PR #45 → `b065bf4`；R4-6b 通道本体本分支实现态；Live 推迟）；R4-7 平台化扩展（多角色模型路由 + 外部工具扩展注册）；R4-8 规模与压测（长会话/万级记忆/容量报告） | 🔄 R4-4 已合并（PR #43 → main `f8eb398`）；R4-5 已合并（PR #44 → main `215317c`）；R4-6a 已合并（PR #45 → main `b065bf4`）；R4-6b 本分支（`feat/r4-6b-wechat-gui-channel`）工作区（沙盒 agent + 后端通道实现态；Live 待统一阶段）；R4-1 / R4-2 / R4-3 已合并；R4-7 / R4-8 待开工 |

**当前分支状态**：
- main：`b065bf4`（PR#34–#45 已并入）；R2 归档点：`e95f5fe`（R2 全部批次）/ `6b54b2a`（R2b）/ `3872504`（R2c）/ `7fe68a0`（R2-defense）/ `1305420`（logprobs）。
- `feat/r3-perf`：R3-3 性能基线，已合并（PR #37），可删除。
- R4：R4-4 已合并（PR #43 → `f8eb398`）；R4-5 已合并（PR #44 → `215317c`）；R4-6a 已合并（PR #45 → `b065bf4`）；R4-6b（通道本体）在当前分支 `feat/r4-6b-wechat-gui-channel` 工作区；R4-1 / R4-2 / R4-3 已合并；R4-7 / R4-8 待排期。
- 历史堆叠分支（`feat/r2b-plus-intimacy` / `feat/r2f-realtime-voice` / `feat/llm-confidence-logprobs` 等）均已合入 main，可删除；工作区 clean。

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
- 记忆存储=单栈 SeekDB（Q3 方案 A：权威库与检索库同实例、不同库名；行锁语义沿用 MySQL 协议历史术语）；知识图谱后置（D2 未来 FalkorDB/Graphiti 可选）。
- 向量库/ES 弃用（SeekDB 覆盖）；Form B 为官方混合检索（DBMS_HYBRID_SEARCH）；零命中=SQL NULL 单元格。
- 微信=企微智能机器人主轨+GUI 微信实验轨（D2-A）；抖音=GUI/浏览器底座（D4）；短信=SmsForwarder 记账推迟（D3）。
- TTS=CosyVoice3-0.5B、ASR=Qwen3-ASR-0.6B、Embedding=Qwen3-Embedding-0.6B 主+bge-m3 备、look_at=Qwen3-VL（GGUF）。
- 管理台鉴权=owner Bearer（ADMIN_AUTH_TOKEN env；空=禁用+启动提示）。
- 数据库迁移链 0001..0019；0012=curated lifecycle；0013=persona facts；0014=persona skills；0015=core memory（blocks + append-only revisions）；0016=voiceprint samples/consents + sticker library；0017=canvas documents；0018=persona facts 复合索引（persona_id/status/confidence）；0019=dream_runs 单飞标记（running_marker 唯一键）。
- **记忆域保留期 = append-only 无限期保留**：不设 TTL 自动清理；删除权由单条管理面删除 + 人格级联删除覆盖。理由：审计与梦境回溯（运行决策 / 证据链 / 修订链需全量可复算）。

### 4.1 拍板三案对照（抽样归档）

> 抽样自 Oracle 审计 B 的三条拍板。口径：**选定项与其括号内定义是 §4 原始
> 记录**；非选定项的名称与优缺点文本据该条决策要点重建（原始会话只保留选定
> 项），不写入记录之外的细节。

**抽样 1：P4 GUI 微信会话键（原始记录：B）**

| 方案 | 优点 | 缺点 | 结论 |
|---|---|---|---|
| A 稳定平台标识（wxid / 会话 id） | 天然唯一，重命名不受影响；无需视觉识别 | GUI 微信不直接暴露稳定标识，需另建读取面；跨设备/换号时易漂移 | 非选定（重建） |
| B 联系人备注名 + VLM 校验 | 备注名是 GUI 内可直接读取的稳定语义面；VLM 校验可抵消截图误读 | 依赖视觉链路；备注名被修改时需重建映射 | **选定（原始记录：B=联系人备注名+VLM 校验）** |
| C 固定会话窗口索引 / 人工绑定 | 实现最简单，零识别成本 | 窗口顺序或数量变化即失效；人工绑定不可规模化 | 非选定（重建） |

**抽样 2：亲密补强「道德层吞没」（原始记录：丙）**

| 方案 | 优点 | 缺点 | 结论 |
|---|---|---|---|
| 甲 提示词对抗 + 后处理 | 不新增端点，全模型一致；角色契约 / 合规重试可复用 | 依赖模型服从性，长对话仍可能被保守策略吞没 | 非选定（重建） |
| 乙 独立模型分轨（可开端点） | 端点与采样独立调优，不受主模型保守策略牵制 | 多一份端点、成本与运维；全部回合切轨时影响普通对话 | 非选定（重建） |
| 丙 渐进组合（甲 + 乙可开 + 成熟度分层） | 默认零新增依赖，按关系阶段与成熟度逐级放开 | 配置面、测试面与文档面最大 | **选定（原始记录：丙=甲提示词对抗+后处理 / 乙模型分轨可开 / 渐进组合）** |

**抽样 3：记忆存储 Q3（原始记录：A）**

| 方案 | 优点 | 缺点 | 结论 |
|---|---|---|---|
| A SeekDB 统一检索 + SeekDB 权威库（单栈同实例） | 单一检索栈 + 单一引擎运维；官方混合检索能力可直接用；知识图谱后置不阻塞 | 权威库与检索库同实例共用容量 / 故障域（拆分实例需另立决策，启动期不变式 fail loud） | **选定（原始记录：Q3 方案 A；知识图谱后置，D2 未来 FalkorDB/Graphiti 可选）** |
| B 独立向量库（Milvus / Qdrant）+ 关系库 | 向量生态成熟，容量与索引选项多 | 多一套引擎、同步链路与备份面；与权威库的一致性成本更高 | 非选定（重建） |
| C 纯关系库全文检索（FULLTEXT / SQL LIKE） | 零新增引擎，部署最轻 | 语义召回弱；规模上升后相关性与延迟不足 | 非选定（重建） |

### R4 方向拍板（用户）
- 主题=体验多模态 + 平台化扩展 + 规模性能（B+C+D）；生产化与统一 Live 测试（A）推迟到全部开发完成后统一执行。
- GUI 微信：企微智能机器人长连接为生产主轨，GUI 微信为实验轨（D2-A）；会话键=B（联系人备注名 + VLM 校验，P4）；PoC 门=Xorg+uinput 栈验证，通过才实现通道本体，不通过整轨推迟；Live（客户端安装 / 登录 / 真消息）随统一 Live 阶段。
- 部署真网验证：先做最低三项（企微 WSS 握手 / 抖音选择器 / ASR-TTS 连通），完整上线就绪包归统一 Live 阶段。
- 实时语音：全量增强（真流式 ASR/TTS 通道 + AEC + 移动端采集优化）。
- 预算与限额：按 purpose 分面（ASR/TTS/voiceprint 独立预算）。
- 多实例：单实例定案，限制文档化。
- 通道扩展：SmsForwarder + WhatsApp Cloud API。
- 前端/Canvas：路由级 lazy split，超过 1MB 触发拆分与体积门。
- 平台化（R4-7）具体面：多角色模型路由与外部工具扩展注册，为本轮拟稿；如与实际预期不符可再拍板。

## 5. 工程实现事实（恢复续跑所需）

### 门禁与验证命令（全部经 pixi）
```
cd backend && pixi run tests-all            # 全量后端测试（数量以 canonical 证据 JSON 为准）
cd backend && pixi run coverage-gates       # 24 组 ≥80% enforce
cd backend && pixi run lint / typecheck / gates（注释门/棘轮/noqa冻结/conftest env 硬覆盖/sandbox-drift；源文件数等明细以 canonical 证据 JSON 为准）
cd backend && SEEKDB_TEST_URL=127.0.0.1:2881 pixi run pytest src/tests/integration_seekdb -q --asyncio-mode=auto   # 强制真引擎（迁移链 head 以 `backend/alembic/versions` 为准，由 doc-counts 机械校验；覆盖并发唯一键/行锁/混合检索/梦境单飞）
cd frontend && pixi run test / coverage / lint / build   # 用例数与构建耗时以 canonical 证据 JSON 为准（高负载机器跑 coverage 可加 -- --maxWorkers=4 稳定）
cd frontend && pixi run build && pixi run bundle-check     # 体积门禁（需先 build）；聚合数字以 canonical 证据 JSON / bundle-check 输出为准
cd docker/tool-sandbox-agent && pixi run tests-unit；cd docker/browser-sidecar && pixi run tests-unit
根级：pixi run gen-examples（三件套再生成）、pixi run gates
```
- SeekDB 本地容器：`docker run -d --name seekdb-smoke -p 2881:2881 oceanbase/seekdb:1.4.0-100000172026082615`（root 空密码；就绪约 5-60s；网络中断后需重启）。
- DTO 契约门：`scripts/export_dto_schemas.py` + `frontend/src/test/contract/dtoSchemas.json`（当前 94 组）；改后端 wire DTO 必须 `pixi run export-dto-schemas` 再提交，改前端 types 契约测试即红。
- 漂移门：`scripts/check_sandbox_dtos_drift.py`（53 项，三树镜像契约 byte 级；含 GUI 微信五工具结果模型与工具名 / 路径常量行）。
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
3. `chat/service.py`：compose 链人格正典之后恒注入宪章；`compose_messages` 新增 `wrap_current_message` / `current_message_label` / `injection_hint`（包裹当轮入站与最近 10 条历史 user 回合；assistant 与更早回合原文）；`ChatService` 装配 `InjectionGuardSettings`（`INJECTION_` env），reply / stream_reply 检测 + 提示 + 来源标签，工具循环 tool 轮正文定界包裹。**已根修（G1）**：包裹 / 来源标签 / 检测提示裁决收敛为 `compose_for_generation` 单一入口（`compose_messages.wrap_current_message` 必填），结构测试枚举直连调用点——见「G1 审计非根修项根修」节。
4. `duty/duty_prompts.py`：值班工具反馈恒加「工具结果」定界包裹（与 chat 同语义；不跟随 chat 开关，文档化）。
5. 测试：`test_injection_guard.py`（机制）+ `test_prompt_injection_red_team.py`（38 条模板 / 20 阳性 / 18 阴性 + 服务层开关与流式）+ prompts byte-lock 增补；既有测试仅按新契约更新 3 处断言（tool 反馈包裹正文、resume 续聊当轮包裹）。
6. docs：`backend/defense.md`（攻击面 × 防线 × 缺口矩阵 / 包裹格式 / 检测矩阵 / JSON 示例 / env 表）、chat.md / channels.md / backend README。
**验证**：后端 2189 passed / 23 组覆盖率（harness/chat 95.5%）/ mypy 151 文件 / ruff / 注释门 + 棘轮 + noqa 冻结 + 漂移 36 / 活 SeekDB 6 用例；前端未改（DTO fixture 零变更）。
**办结**：`INJECTION_*` 四键已由主 Agent 纳入 `.env.example` / Setup Wizard 生成器（已验证两处各 4 键）；R2b+ 的 compliance_guard 联动已落地（见上节）。
**Oracle 深审修复批次（P1/P2 全闭环）**：swipe 重生成末位 user 回合复用 compose 包裹开关 + 检测提示（缺省「消息」标签 / 默认台词风格）；检测前归一副本（NFKC + 零宽剔除 + 空白折叠，留证取自副本）并新增 5 条英文规则（规则表 24 → 29 条）；compose 对最近 10 条历史 user 回合定界包裹（标签「历史消息」，assistant / 更早回合原文）；红队集 38 → 47 条（含混淆形与防误报阴性）、prose pin 改结构断言、补工具循环×注入共存与合规重试不变量用例；defense.md 失效部署待办删除并同步矩阵 / 边界，chat.md 同步 compose 链与 swipe 说明。

**Oracle 复审修复批次（第二轮 P1/P2 闭环）**：复审未通过项逐条闭环——(1) 检测前归一由固定零宽字符表改为剔除全部 Unicode `Cf` 类别 + 变体选择符 U+FE00–FE0F（`Mn` 组合附加符保留），红队补软连字符 / word joiner 阳性与「归一后与无混淆基线同分」防回归断言（**已根修（G1）**：剔除面扩为 Unicode 默认可忽略码点集 + Hangul 填充符，保留可见组合附加符，见「G1 审计非根修项根修」节）；(2) `tag_mimicry` 的 `<…>` 规则锚点由句首放宽到 8 字符内短前缀（闭合回引 / 权重不变），补短前缀阳性与长前缀阴性，defense.md 边界表述同步；(3) `as an AI` 权重 2.0 → 1.5（与中文身份断言同权），补 4 条良性英文阴性（`as an AI` / `you are now` / `act as` / `pretend to be`），规则表总数不变（29 条）；(4) `SwipeService` 构造注入 `InjectionGuardSettings`（`app/api/memories.py` 装配点显式注入），变体路径的包裹 / 检测 / 提示 / 阈值 / 风格随 `INJECTION_` 配置生效，补停用 / 提示关闭 / 阈值抬高 3 例，chat.md / defense.md kill-switch 表述改为 reply/stream 与 swipe 变体均按配置生效；(5) 红队测试注释订正（assistant 历史原文 / user 历史包裹）。红队 47 → 55 条（29 阳性 / 26 阴性）。**验证**：`pixi run tests-all` → 0（2808 passed）；`pixi run coverage-gates` → 0（24 组全 ≥80，harness/chat 96.3%）；`pixi run lint` → 0；`pixi run typecheck` → 0（191 文件）；根 `pixi run gates` → 0（注释门 / 棘轮 +0 / noqa 冻结 / 漂移 38 项一致）；`backend pixi run gen-examples` → 0（三件套再生成无 diff，幂等）。

### 已完成切片：R2d-1 心流门控 + 群聊多人格（未提交工作区）
**已完成**：
1. `chat/flow_models.py` / `chat/flow_gate.py`：门控 Model Classes（`FLOW_` env 面：连发窗口 / 合并静默与上限 / 回复最小间隔 / 积压阶梯与封顶 / 排程丢失宽限）与全纯函数判定——裁决 `reply | defer | merge | skip`、理由枚举、`now_ms` 由调用方注入；判定矩阵锁定（机器事件不回复、睡眠窗口延迟、连发合并、密度节流、积压延迟、亲近 / 亲密关系优先、节律拟人延迟），连发合并保留最近条目并报告丢弃条数。**已根修（G1）**：判定输入改为显式 `prior_arrivals_ms`（路由器先取历史 → 门控 → 再记录当前），删除 `recent[-1]==now_ms` 分支推断——见「G1 审计非根修项根修」节。
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
1. `harness/channels/wecom/wecom_models.py`：长连接 wire Model Classes（Unit 文件）——订阅 / 心跳 / 消息与事件回调 / 应答 / 回复（markdown）/ 主动推送（markdown、image、voice、file）/ 素材分片上传（init / chunk / finish）命令；入站映射（`single:<userid>` / `group:<chatid>` 会话键、文本 / 语音转写 / 图文混排 / 图片 / 文件 / 视频归一与占位符、媒体 url 附件、`compat_info` → `synthetic`）；chat_id grammar 解析；markdown 20480 字节分段（换行边界优先、UTF-8 硬切不破字符）。**已根修（G1）**：纯媒体占位文本统一声明 `has_user_text=False`（路由器跳过生成）——见「G1 审计非根修项根修」节。
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
2. 后端 `harness/tools/browser_models.py` / `browser.py`：镜像 `EvaluateArgsModel` 与 `EVALUATE_TOOL_NAME`（byte 级，漂移门 +2 项 = 38 项）、`build_evaluate_spec` + 注册（duty / chat 双面，八工具）；`scripts/check_sandbox_dtos_drift.py` 与 tools-browser.md 同步。**已根修（G1）**：`evaluate` 退出 chat / duty LLM 工具注册表（LLM 面七工具；抖音内部直调保留 + 显式 opt-in）——见「G1 审计非根修项根修」节。
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

### R2f 复审 P2×4 闭环（音频块上限 / 转写任务化 / 关闭 wire 顺序 / end 排空抢占）

**已完成**（逐条对应 Oracle 复审意见）：

1. **音频块上限**：`realtime_models.py` 新增单一来源常量 `REALTIME_AUDIO_CHUNK_MAX_BYTES`（64 KiB）、`REALTIME_AUDIO_CHUNK_MAX_BASE64_CHARS`（编码长度上限）与 `AUDIO_CHUNK_TOO_LARGE_ERROR_TYPE`；`audio_chunk.data` 增加 `max_length` 且字段校验器断言解码后字节数 ≤ 64 KiB（自定义错误类型可被上行分派识别）；`realtime_session.py` 在上行校验失败分支区分「超限」——下行可恢复 `error(VALIDATION_ERROR)`、计入 `audio_chunks_rejected`、关闭指标日志新增 `rejected=`，超限块不入队且会话继续。保留校验器 + 入队解码的二次解码：把解码结果缓存在模型内会引入 wire 不可见状态且需新访问面，而单块 ≤64 KiB 的一次额外解码代价可忽略。文档同步单块上限 / 超限拒绝计数 / 错误码行 / 测试面。
2. **转写任务化**：`_handle_segment` 只推进相位与指标并 `_spawn(_transcribe_segment(...))`（同时登记进统一 `_tasks` 与域内 `_transcribe_tasks`），任务内完成 transcribe → final_transcript → ASR_DONE → `_request_generation`；音频工人移除内联 await，转写窗口内 VAD / 起说 / 打断判定持续工作。多段转写可并行，生成请求按**转写完成序**进入既有待生成通道（仍单轮次串行；不按 segment 序号重排——重排需为 final 下行与错误映射引入跨任务缓冲状态，完成序可由 `segment_id` 对齐，故取完成序并在代码与文档写明）。并发转写可能在播报中完成，状态机补 `speaking + asr_done → speaking`（转写完成，等待当前轮播完后生成），避免落入非法迁移告警。文档链路图 / 并发表 / 状态表 / 边界节同步。既有 `test_audio_backpressure_drops_oldest_chunks` 的造压手段随本变更同步（原靠 ASR 门闩阻塞音频工人；现用下行发送门闩把工人停在切段状态迁移；断言不变）。
3. **关闭 wire 顺序**：`_request_close` 改为「`_close_reason` 先行占位（幂等守卫 + 首个原因生效）→ `_advance(...)` 使 state(closed) 真实下行 → `_closing` 置位 → close_event」；异常路径改经 `_request_close(INTERNAL_ERROR)`，internal_error 路径同样下发 state(closed)。五条关闭路径（client_end / idle_timeout / max_duration / internal_error / session_rejected）wire 序均为（可选 error）→ state(closed) → closed；transport_closed 对端不可达，仅尽力下行 closed（文档已写明该例外）。
4. **end 排空抢占**：`_handle_end` 拆出 `_drain_before_close`（在途转写等待与轮次等待均与 `_close_event` 竞争，FIRST_COMPLETED）+ `_wait_drain_progress`；关闭事件先到 → 取消在途生成 / TTS（提取 `_cancel_inflight_turn` 复用打断的取消面，关闭路径同时清空待生成文本）→ 退出排空 → 主循环收尾；正常路径保持「冲刷 → 排空 → client_end」。文档并发表「优雅关闭」行同步。

**反向实证**（`git show HEAD:backend/src/packages/harness/voice/{realtime_models,realtime_session}.py` 临时回退后运行新增用例，随后恢复）：
- 旧模型对 65537 字节块直接接受且无上限常量：反向探针输出 `OLD accepts 65537 bytes: 65537` / `OLD has ceiling attr: False`；
- 旧 session 运行 6 条新增 session 用例：`5 failed, 1 passed`——超限计数断言 `0 == 1` 红、转写挂起时 VAD 打断 `TimeoutError` 红、client_end / idle_timeout 的末条 state 为 `listening`（非 `closed`）红、end 排空抢占 `TimeoutError` 红；「关闭取消挂起转写」通过（旧实现转写内联在音频任务内亦被收尾取消，属保留型守卫）。

**验证**（主 Agent 终验，命令与退出码）：

- `cd backend && pixi run tests-all` → 0：**2845 passed**（新增 7 条用例：模型上限边界 1 / 超限后会话继续 1 / 转写挂起时 VAD 打断 1 / 关闭取消挂起转写 1 / 关闭顺序 2 / end 排空抢占 1；另状态机迁移矩阵新增 `speaking + asr_done` 合法边 1 条）；
- `cd backend && pixi run coverage-gates` → 0：**2845 passed**；24 组 ≥80（harness/voice 95.2%）；
- `cd backend && pixi run lint` → 0（`All checks passed!`）；`pixi run typecheck` → 0（`Success: no issues found in 191 source files`）；
- 根 `pixi run gates` → 0：注释门 violations=0（hints=30 为既有报告面）、同步测试棘轮 +0、noqa/type:ignore 冻结全基线、沙盒漂移 38 项一致、红探针 190/190、docs-truth 一致、断言棘轮 asserts=8173 / raises=602 无下降、surface-check 102/62/40 通过、prose-pin 报告；
- `cd backend && pixi run gen-examples` → 0，`git status` 无示例模板变化（幂等）；
- 本批未改前端，前端门禁未重跑（前端 666 基线不受影响）。


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
- 根 `pixi run gates` → 0：注释门 violations=0；同步测试棘轮 +0；noqa 冻结 0 / 0 / 0 / 7 / 0（scripts 存量 7 未新增）；沙盒漂移 38 项一致；红探针门；文档真相；断言棘轮 8012 asserts / 593 raises / 190 文件；导出面（102 引用 / 62 已登记 / 40 豁免）与 env 键面（686 行）；prose-pin 命中实现源字面量 483 个，分桶 field_or_code=424 / prose=59——「疑似 prose pin」按 prose 桶计 **59** 个（CJK 文案归入 prose 桶后修订；不可达的 other 桶已删除；原「483 疑似」是未分桶的命中总数），纯结构断言 7.7%。
- 可选步骤验证：`cd backend && pixi run verify-slice -- --with-seekdb --only tests-seekdb` → 0（活 SeekDB 6 passed）；`--with-frontend --only frontend-test,frontend-coverage,frontend-lint,frontend-build` → 0（666 passed / statements 95.98% / lint / build），两条可选链路均已按摘要校验跑通。
- 基线文件：`backend/red_manifest.toml` 190；`scripts/.assertion_baseline.json` 190 文件 / 8012 / 593；`scripts/.todo_baseline.json` 总数 7；`scripts/.surface_allowlist.json` 40 条。全部只降不升，更新入口分别为 `--prune` / `--update` / `--update`。
- 存量问题处理：40 个未导出面（22 个 DTO / 结果模型 + 18 个 StrEnum 值域）按类别登记允许清单并写明 reason（声纹 PIPL 面、搜索管理面、语音 / 健康 / QQ webhook 等前端未建契约；`EpisodeBatchDelete*` 已由前端别名消费；枚举值域不进 BaseModel schema 导出器），未扩散修改前端契约；prose-pin 报告保留协议字段名类命中（启发式口径透明，不阻断）。

### 已完成切片：加固门禁复审闭环（P1×2 + P2×5）

**已完成**（逐条对应 Oracle 复审意见）：

1. 断言棘轮整删漏检（P1）：`scripts/assertion_freeze.py` 在 `--enforce` 下把「基线内路径但当前扫描缺失」计入 drops 并 exit 1（明细「基线内测试文件缺失（整删须 --update 重建基线）」），与计数下降同口径；报告模式仍只报告；合法整删必须 `--update` 重建基线。
2. 证据 sha 含 egg-info（P1）：`scripts/verify_slice.py` 的 `_iter_hash_files` 跳过任一路径段以 `.egg-info` 结尾的条目（与 `red_probe.py` 复制副本的 `*.egg-info` ignore 口径一致），sha8 不再随 editable 安装漂移；删除孤儿证据 `docs/evidence/r2-hardening-main-ab346493.json` 与其 5 份日志，仅保留 canonical 证据与重跑产物。
3. ad-hoc 探针（P2）：`scripts/red_probe.py --symbol <module:attr> --tests <nodeid[,...]>` 构造一次性探针（不读不写 manifest，先校验符号与 node id 存在）；省略 `--tests` 时保持清单查找语义。
4. prose-pin 分桶（P2）：`scripts/prose_pin_check.py` 把命中文案分三桶——`field_or_code`（无空白无 CJK 的单 token：标识符 / 字典键 / UPPER_SNAKE / 带连字符模型 ID / MIME / 路径）、`prose`（含空白或 CJK 的自然语言）；「疑似 prose pin」只统计 prose 桶，Top-N prose 桶优先并逐条标注桶名；原第三桶 `other` 因与 prose 桶判定域完全重合、分支不可达而在复审后删除。
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
- 根 `pixi run gates` → 0：注释门 violations=0（hints=30）；同步测试棘轮通过；noqa 冻结 0 / 0 / 0 / 7 / 0；沙盒漂移 38 项一致；红探针门（190 / 190 / 3）；文档真相通过；断言棘轮 8012 asserts / 593 raises / 190 文件（无缺失）；导出面 102 引用 / 62 已登记 / 40 豁免（清单 40 条 / max_entries=40）与 env 键面 686 行；prose-pin 命中 483，分桶 prose=59 / field_or_code=424（CJK 文案归入 prose 桶后修订；不可达的 other 桶已删除）。
- `cd backend && pixi run verify-slice -- --slice r2-hardening` → 0（tests-all **2808 passed** / coverage-gates **24 组 ≥80%** / lint / typecheck 191 源文件 / gates 全步摘要命中），证据包 `docs/evidence/r2-hardening-55a44a52.json`（sha8=`55a44a52`，640 文件，`result=pass`、`failed_steps=[]`、全部 `summary_matched=true`）。
- 复算：`cd backend && pixi run verify-slice -- --hash-only` → `sha8=55a44a52 files=640`；把源码拷到不含 `*.egg-info` 的干净副本（`rsync` 排除 `.pixi` / `.git` / `node_modules` / `__pycache__` / `*.egg-info` / 缓存与覆盖产物）后在该副本 `cd backend && pixi run verify-slice -- --hash-only` → 同为 `sha8=55a44a52 files=640`（pixi install 在副本里重新生成了 `.egg-info`，标识仍不变，证明剔除口径生效）。

### 已完成切片：加固门禁复审残留闭环（Oracle 复审第 3 轮 P2×2）

- prose 分桶：含 CJK 文案统一归入 prose 桶，全角括号 `【】（）` 补入句读标点；重跑 `pixi run prose-pin` → 命中 483（分桶 prose=59 / field_or_code=424），纯结构断言 193 例 / 7.7%；不可达的 `other` 桶已从实现、报告与三处文档（模块 docstring / docs/README / 验收 SOP）删除。
- 证据目录说明：`docs/evidence/README.md` 增补「`ab346493` 及更早 sha8 由旧哈希口径（含 `.egg-info`）计算，不能用当前代码复算，仅作历史留档」。
- `cd backend && pixi run verify-slice -- --slice r2-hardening` → 0（tests-all 2808 passed / coverage 24 组 / lint / typecheck 191 源文件 / gates 全步摘要命中），证据包 `docs/evidence/r2-hardening-d193c13e.json`（sha8=`d193c13e`，本提交为当前 canonical）；`c524502a` / `afaaf9a9` / `55a44a52` / `ab346493` 作为前序提交的历史证据保留，按 README 口径只认与当前 `--hash-only` 一致的一份。

### 已完成切片：R2c 记忆批次 Oracle 深审修复（P1×1 + P2×7，本分支工作区）

**已完成**（逐条闭环）：

1. **P1 人格删除级联**：`PersonaService.delete` 在同一事务内按 `persona_id` 批删 `ff_persona_states` / `ff_memory_episodes` / `ff_curated_memory` / `ff_dream_runs` / `ff_persona_facts` / `ff_persona_skills` / `ff_core_memory_blocks` / `ff_core_memory_revisions` / `ff_chat_sessions` / `ff_chat_messages`；事实与策展自引用 `superseded_by_id` 先置 NULL；`ff_memory_index_lite` 按 episode / curated ref 集合同事务清理，SeekDB `ff_memory_index` 经 `app.state.memory_indexer` 提交后 best-effort 分发删除（`app/api/personas.py` 的 delete / batch-delete 解析请求级 indexer）。测试 `test_delete_cascades_entire_memory_domain` 覆盖双人格七表 + 索引行（A 全空 / B 原样 / 二次删除 NOT_FOUND）；docs persona.md / database.md 写明级联范围与「无跨域 FK，由服务层批删」。**已根修（G1）**：级联范围改为 `Base.metadata` 派生（含磁盘文件清理 + 保留表白名单），结构守卫测试断言派生集合 == 元数据 persona_id 表全集——见「G1 审计非根修项根修」节。
2. **P2 梦境输入上限**：`dreaming_models` 新增 `DREAM_EVENT_TEXT_MAX_CHARS=500` / `DREAM_DIARY_MAX_EPISODES=200` / `DREAM_DIARY_MAX_CHARS=60000` / `DREAM_MERGE_TOPIC_LIMIT=50`；`dreaming_prompts` 统一截断单事件正文（省略标记、按码点不破 UTF-8）、日记双帽取样、合并主题按 `updated_at`/`id` 降序取样；`_run_phases` 接线并记 debug 计数（不打印原文）。测试覆盖长事件截断 / 日记条数与总字符双帽 / 主题上限与同刻确定性；docs memory.md 增上限表。
3. **P2 梦境重入闸**：`DREAM_REENTRY_WINDOW_MS=3600000`；`run_dream` 在孤儿收敛后查询本人格窗内 running 行，命中直接返回该记录（不新建行、不跑阶段、零模型调用）。测试覆盖连续两次触发返回同一 run id 且网关零调用；既有 `test_run_dream_converges_stale_running_rows` 的期望按新单飞契约调整（新鲜 running 行改为返回而非新建运行——任务要求的行为变更）。docs memory.md 记录。
4. **P2 活引擎并发覆盖**：`test_concurrency_invariants.py` 新增 facts 更正腾位写序（NULL 标记插入 → 旧行 superseded → 取标记；两连接重叠事务，后提交者 1062 `uq_ff_persona_facts_active`，终态仅一条 active）、skills slug（`uq_ff_persona_skills_slug`）、core memory key（`uq_ff_core_memory_blocks_key`）三例；均断言 1062 + 约束名，实跑活 SeekDB 通过。
5. **P2 复合索引**：新迁移 `000000000018_persona_facts_confidence_index.py`（down_revision=000000000017）为 `ff_persona_facts` 增 `(persona_id, status, confidence)` 索引，ORM `__table_args__` 同步；migration_chain 测试升级/降级/复升覆盖并断言索引存在（`support.HEAD_REVISION=000000000018`）；database.md / persona.md 补链与索引说明。
6. **P2 时钟一致**：`run_dream` 终态 `finished_ms` 改用注入 `run_now`（与 `started_ms` 同轴）；测试固定注入时钟断言 `finished_ms == NOW`。
7. **P2 PII 保留期决策**：不改代码；memory.md 增「保留期决策（append-only 无限期）」、LEDGER §4 增同条拍板（删除权由单条删除 + 人格级联覆盖；理由：审计与梦境回溯）。
8. **P2 database.md**：补齐 `0017_canvas_documents` 与 `0018_persona_facts_confidence_index` 两节（字段/索引/迁移链）及人格级联删除服务层说明。

**验证**（主 Agent 实跑，命令与退出码）：

- `cd backend && pixi run tests-all` → **0**（**2816 passed**）。
- `cd backend && pixi run coverage-gates` → **0**（24 组 ≥80；harness/memory 94.5% / harness/persona 95.2%）。
- `cd backend && pixi run lint` → **0**（ruff 全通过）；`pixi run typecheck` → **0**（mypy 191 文件零问题）。
- 根 `pixi run gates` → **0**（注释门 violations=0 hints=30；同步测试棘轮通过；noqa 冻结 0/0/0/7；沙盒漂移 38 项一致；红探针 190/190/3；文档真相通过；断言棘轮 8041 asserts / 594 raises；导出面 102 引用 / 62 登记 / 40 豁免；prose-pin 484 命中，prose=59）。
- `cd backend && pixi run gen-examples` → **0** 连续两次；三件套 `.env.example` / `config.example.yaml` / `extensions_config.example.json` sha256 两次一致（无 diff，幂等）。
- `cd backend && SEEKDB_TEST_URL=127.0.0.1:2881 pixi run pytest src/tests/integration_seekdb -q --asyncio-mode=auto` → **0**（**9 passed**：0018 迁移链升/降/复升、facts/skills/core memory 三条新 1062、既有主键/策展/变体/行锁/混合检索）。

**剩余**：无。

### 已完成切片：R2a 通道底座 Oracle 补审修复（P2×4，本分支工作区）

**已完成**（逐条闭环）：

1. **合成事件恒不回**：`ChannelRouter.handle_inbound` 在适配器寻址之后、per-chat 锁与心流门控之前对 `message.synthetic is True` 恒跳过——返回空投递结果，不建会话、不写心流节拍状态、不调用门控判定；跳过计数 `synthetic_skip_count` 累加并记 debug 日志（含累计条数）。语义与 `FLOW_ENABLED` 无关（门控关闭的部署同样不回复）；判定层 `SKIP / SYNTHETIC_EVENT` 保留为纯函数契约与纵深防御。既有断言同步（行为契约变更）：`test_channel_router.py` 的 v2 附件透传用例改为 `synthetic=False`（附件透传意图保持），`test_channel_router_flow.py` 的合成事件用例改为断言「不建会话 / 不回复 / 不排程 / 节拍状态为零」；两文件各补一条「门控关闭时 synthetic 仍不回复」用例。docs/backend/channels.md 合成语义、心流门控链路、消费者边界与测试面表同步为「路由器恒跳过，与 FLOW_ENABLED 无关」。
2. **TG 出站 429 retry_after**：`TelegramClient` 构造期新增注入 `sleep`（缺省 `asyncio.sleep`）；`_post_send_method` 遇 HTTP 429 解析响应 JSON `parameters.retry_after`（数字秒）→ 夹取到 1..30 秒 → 经注入休眠面延迟后**单次**重试同一请求；重试仍非 200 才抛既有 `CHANNEL_API_ERROR`（不发第三次）；429 缺该参数 / 非数字 / 体非 JSON 不重试，直接按既有映射上抛。请求执行拆为 `_post_once`（传输异常映射共用），重试只覆盖出站 POST（`getUpdates` / `getMe` 不参与）。测试 `test_tg_client.py` 新增六例（伪休眠断言 7s 延迟与两次请求 / 上界夹取 30s / 下界夹取 1s / 重试仍 429 只重试一次 / 缺参数不重试 / 媒体出站共用链路）。channels.md Telegram 节新增「出站 429 限流（`retry_after` 单次重试）」规则表与错误码表行。
3. **群聊表情收录边界文档化**：channels.md 表情包桥节改写为「应答侧各人格用各自库；入站收录只进主人格（第一位候选）素材库」（多人格下表情归属歧义 → 首位候选为确定性选择）；`channel_service.py` 的 `gate_persona_id = group_pool[0].id` 处补同口径中文注释。代码语义未改。
4. **企微踢线用例去 flake**：`tests/wecom_fakes.py` 新增 `FakeWeComConnector.connected_event` / `FakeWeComConnection.subscribed_event` / `FakeWeComConnection.closed_event`（连接产出 / 订阅应答入队 / 连接关闭三处显式事件）；`test_kicked_event_stops_reconnect` 改为事件门控（等连接产出 → 等订阅完成 → push 踢线事件 → 等连接关闭 → await run 收尾），删除真实 2s `wait_for` 与 `_wait_until` 轮询。重复实证：该用例单独循环 30 次，0 失败。

**验证**（主 Agent 实跑，命令与退出码）：

- `cd backend && pixi run tests-all` → **0**（**2824 passed**；新增 8 例）。
- `cd backend && pixi run coverage-gates` → **0**（24 组 ≥80；harness/channels 97.1%）。
- `cd backend && pixi run lint` → **0**（`All checks passed!`）；`pixi run typecheck` → **0**（191 源文件零问题）。
- 根 `pixi run gates` → **0**（注释门 violations=0；同步测试棘轮通过；noqa 冻结通过；沙盒漂移 38 项一致；红探针门通过；文档真相通过；断言棘轮 8074 asserts / 596 raises（无下降）；导出面 102 引用 / 62 登记 / 40 豁免 + env 键面 686 行；prose-pin 484 命中，prose=59）。
- `cd backend && pixi run gen-examples` → **0** 连续两次；三件套 `.env.example` / `config.example.yaml` / `extensions_config.example.json` sha256 前后一致（与改动前亦一致——幂等、无 env 键新增）。
- 踢线用例去 flake 实证：`for i in $(seq 1 30); do pixi run pytest "src/tests/unit/test_wecom_client.py::TestSubscribeAndLifecycle::test_kicked_event_stops_reconnect" -q --asyncio-mode=auto; done` → **0**（30 次运行，failures=0）。

**剩余**：无。

### 已完成切片：R2c 记忆批次 Oracle 补审修复（P2×2，本分支工作区）

**范围澄清**：记忆域（facts / skills / core memory / dreaming）的代码载体为 PR#12（本台账 R2c 行）；PR#11（R2b 人格表达，`6b54b2a`）仅轻触 memory 三文件与迁移 `0010` / `0011`（swipes 变体 + 提醒），不构成文档漂移来源。

**已完成**（逐条闭环）：

1. **P2 facts 更正并发**：`FactsService.correct_fact` 对 `_upsert_once` 套上与 `upsert_fact` 同款的 `IntegrityError` 处理——捕获后回滚、重读目标行（回滚使行对象过期）并**单次**重试；重试仍撞 `uq_ff_persona_facts_active` 按既有 `INTERNAL_ERROR` 语义上抛（`details={id, category}`）；docstring 的并发面段落与 Raises 同步该语义。测试 `test_correct_converges_after_concurrent_active_conflict`：提交点钩子先回滚本次半成品、提交同身份竞争活动行，再用一条同身份待插入行触发**真实** SQLite 唯一键 `IntegrityError`（断言 `orig` 含 `UNIQUE constraint failed`），复现「旧行降级后、新行置标记前」被他写者抢先的时序；断言终态仅一条 active、旧行 superseded 且 `superseded_by_id` 指向竞争现行行、更正说明已并入该行证据、无异常上抛、重试恰一次。`test_correct_retry_conflict_raises_internal_error`：两次提交均撞键 → `INTERNAL_ERROR`，回滚后原行仍 active / version=1（无半成品行）。反向实证：去掉重试编排后首条用例单跑 1 failed，复原后通过。
2. **P2 skills 扫描确定性**：`match_for_compose` 候选查询在 `.limit(SKILL_MATCH_SCAN_LIMIT)` 前加 `ORDER BY use_count DESC, id ASC`——扫描帽只裁剪低强化候选，高强化技能不再被插入顺序挤出命中面；模块 docstring 与 persona.md 技能节改写为「按强化度（`use_count` 降序、`id` 升序）取前 500 后评估」。测试 `test_match_scan_limit_keeps_highest_strength_candidates`：ORM 批量插入 500 条填充启用技能 + 1 条 `use_count=999` 的命中技能（共 501 条启用行），断言最高强化技能必在命中面且两次调用结果一致。反向实证：临时去掉 ORDER BY 后该用例单跑 1 failed，复原后通过。

**验证**（主 Agent 实跑，命令与退出码）：

- `cd backend && pixi run tests-all` → **0**（**2827 passed**；新增 3 例）。
- `cd backend && pixi run coverage-gates` → **0**（24 组 ≥80%；harness/persona 95.7%）。
- `cd backend && pixi run lint` → **0**（`All checks passed!`）；`pixi run typecheck` → **0**（191 源文件零问题）。
- 根 `pixi run gates` → **0**（注释门 violations=0 hints=30；同步测试棘轮通过；noqa 冻结 0/0/0/7；沙盒漂移 38 项一致；红探针 190/190/3；文档真相门 env 分节表一致 + 未完成标记 7=基线；导出面 102 引用 / 62 登记 / 40 豁免 + env 键面 686 行；断言棘轮 asserts=8090 raises=597 无下降；prose-pin 484 命中，prose=59）。
- `cd backend && pixi run gen-examples` → **0** 连续两次；三件套 `.env.example` / `config.example.yaml` / `extensions_config.example.json` sha256 两次完全一致（幂等，无 env 键新增）。

**剩余**：无。

### 已完成切片：记忆批次复审残留闭环（成功路径单次提交）

- `correct_fact` 成功路径改为 `else` 分支记录日志并直接返回，第二个 `_upsert_once` 仅作冲突后的重试路径；此前 try 体为赋值导致每次成功更正都重复执行一次写入（终态依赖合并分支的偶然幂等）。
- 用例 `test_correct_happy_path_commits_once` 以计数 `_commit` 钉死「无冲突时恰好一次提交」（反向：去掉 `else` 短路即红）。
- 验证：`cd backend && pixi run verify-slice -- --slice memory-review`（见当前 canonical 证据文件）；`test_facts_service.py` + `test_skills_service.py` 47 passed。

### 已完成切片：R2d 声纹 / 表情 / 门控复审闭环（P1×1 + P2×5）

**已完成**（逐条闭环）：

1. **P1 密度节流语义**：`evaluate_flow_gate` 的密度锚点改为「当前消息之前最近一条入站」——判定历史含当前条目时取 `recent[-2]`，否则取 `recent[-1]`；仅 `elapsed < min_reply_gap_ms` 时才置 `density_wait`，达到或超过即落入后续规则（连发合并仍按含当前消息的窗口计数）。单测改为生产形态（`[now-300, now]` → 900ms），保留未含当前条目的历史形态用例并注明其与生产形态等价，新增「间隔 3500ms ≥ gap → `reply/normal` 不延迟」用例；路由器级 `test_density_gap_reached_replies_without_scheduling` 断言追发 3500ms 后立即回复且 `scheduler.jobs == []`。chat.md ⑤ 行同步为「以上一条入站为锚，间隔 < 阈值才延迟并补齐剩余毫秒；间隔 ≥ 阈值立即回复」。
2. **P2 声纹校验时长带**：`_validate_sample` 增加必填 `min_duration_ms` 参数；注册传 `min_sample_duration_ms`，校验传 `min_verify_duration_ms`——校验不再套注册门槛，`[min_verify, min_sample)` 区间正常进入相似度比对而非 422；低于校验门槛仍返回 `matched=false` / `audio_too_short`。新增该区间用例（900ms 落在 800-1000ms 带内 → `matched=true`，不抛异常）。voice.md 链路节写明两条阈值的独立关系与裁决形态。
3. **P2 并发唯一键收敛**：表情包批量上传逐文件经 `begin_nested()` 保存点隔离，撞 `uq_ff_stickers_persona_sha` 时只回滚该条目、计入 duplicate 拒收并清理无引用文件，同批其余条目照常入库；声纹注册撞 `uq_ff_voiceprint_samples_persona_sender_sha` 时整体回滚、清理本批孤儿文件并按 duplicate 语义抛 `CONFLICT`（details.reason=duplicate）。新增两个确定性竞态单测（预检窗口外注入同哈希行）分别验证保存点隔离与异常收敛。真引擎套件补 `ff_stickers` 同人格同哈希、`ff_voiceprint_samples` 同人格同发送者同样本哈希两条 1062 + 约束点名用例，并保留跨人格 / 跨发送者正控。**已根修（G1）**：保存点 + 胜者行/文件判定抽为 `sticker_service` 共用插入函数，`ingest_bytes` 复用（并发冲突 + 顺序同哈希双路用例）——见「G1 审计非根修项根修」节。
4. **P2 conftest 硬覆盖**：`backend/src/conftest.py` 按生成器清单硬赋值 `FLOW_*`（10）/ `GROUP_PERSONA_*`（3）/ `VOICEPRINT_*`（15）/ `STICKER_*`（9）共 37 键（S3 硬覆盖口径）；裸构造 `FlowGateSettings()` / `GroupPersonaSettings()` / `VoiceprintSettings()` / `StickerSettings()` 与 app 级 Settings 在开发者本地 `.env` 存在同名键时仍取测试值。默认值用例同步断言四组关键值作为 hermetic 守卫。
5. **P2 实时语音边界声明**：chat.md 心流门控节与 realtime-voice.md 边界节均明示「心流门控仅作用于通道入站；实时语音直连 `ChatService.stream_reply`，不经门控裁决」。
6. **P2 排序 docstring**：`persona_rotation.py` 规则④补「主人格（`is_primary`）优先于主键次序」，与排序键 `(上一轮未发言, is_primary, id)` 一致；channels.md 选人流程与规则说明同步。

**验证**（命令与退出码）：

- `cd backend && pixi run tests-all` → **0**（**2835 passed**；新增 7 例）。
- `cd backend && pixi run coverage-gates` → **0**（24 组 ≥80%；harness/chat 96.3% / harness/voice 95.3% / harness/multimodal 93.4%）。
- `cd backend && pixi run lint` → **0**（`All checks passed!`）；`pixi run typecheck` → **0**（191 源文件零问题）。
- 根 `pixi run gates` → **0**（注释门 / 同步测试棘轮 / noqa 冻结 / 沙盒漂移 / 红探针 / 文档真相 / 断言棘轮 / 导出面 / prose-pin 全过）。
- `cd backend && pixi run gen-examples` → **0**；三件套 `.env.example` / `config.example.yaml` / `extensions_config.example.json` sha256 前后一致（幂等）。
- `cd backend && pixi run tests-seekdb` → **0**（**11 passed**：9 条并发不变式 + 混合检索 + 迁移链；含本批两条 1062 用例）。
- **P1 路由器级反向实证**：临时把密度计算还原为旧式 `min_reply_gap_ms - (now - recent[-1])`，`test_density_gap_reached_replies_without_scheduling` 单跑 **1 failed**（裁决 `defer` / `density_pacing` / 1200ms、`message_ids=[]`、产生 1 个排程作业）；复原后该用例通过。

**剩余**：无。

### 已完成切片：R2e 复审 P2×3 闭环（Canvas 预览子资源 / 企微回复请求号 / 过载丢弃语义）

**已完成**（逐条闭环）：

1. **P2 Canvas 预览外部子资源**：`CanvasPage` 预览 iframe 在 `sandbox=""` 之外增加 `csp="default-src 'none'; style-src 'unsafe-inline'; img-src data:"`——沙箱无脚本之上再阻断外部子资源（仅放行内联样式与 `data:` 图）；不支持该属性的浏览器按规范忽略并退回纯沙箱行为。canvas.md 锁定决策行与「安全边界」同步写明「沙箱无脚本 + CSP 阻断外部子资源（样式/内联数据图除外），跟踪信标不再外发」。前端既有预览用例把 `csp` 与 `sandbox` 一并断言为机器可消费契约（`getAttribute` 精确值）。反向实证：临时去掉 `csp` 属性，该用例 **1 failed**（`expected null to be 'default-src ...'`）；复原后 12 passed。
2. **P2 企微回复死参 / 键覆盖**：`build_markdown_reply_command` 删除未使用的 `req_id` 形参（docstring 改为「响应帧头携带 callback req_id 供平台归位，命令自身不再生成请求号」），客户端调用同步；`_execute` 在登记 pending 前检查同键在途，命中即抛 `CHANNEL_API_ERROR`（message=`请求号在途重复，拒绝覆盖在途应答`、`details={"req_id": ...}`）且不覆盖原 future；客户端模块 docstring 改为「订阅/心跳/推送等主动命令自生成 req_id；aibot_respond_msg 的帧头 req_id 必须回抄 callback req_id，在途表亦以该键登记」。用例：`test_markdown_reply_uses_callback_req_id` 断言构造不再需要请求号且帧头为 callback req_id；`test_duplicate_pending_req_id_rejected` 在首帧在途后同键再发送 → 受控抛错、出站帧仍为 1、原 future 未被替换且平台应答到达后正常归位（成功完成）。
3. **P2 过载丢弃语义**：`WeComChannelRunner._handle_callback` 的去重登记移到 `put_nowait` 成功之后——`WeComMessageDedupWindow.contains` 只读判重，入队成功后 `remember` 登记；队列满丢弃不留 msgid，平台重投可在队列排空后真正恢复；不可路由回调不登记。constants 注释与 channels.md ② / ⑤ 改为「过载丢弃为终态策略：不入队即不登记去重，平台重投可在队列恢复后重新消费；已入队消息的重投由 msgid 去重保证不二次消费」。用例 `test_queue_overflow_does_not_register_dropped_msgid`：单 worker 被闸门占住 + 容量 1 构造确定性丢弃，断言丢弃计数 1、ERROR 日志含「未登记去重 / msgid=q3 / dropped=1」，队列排空后同 msgid 重投成功投递且终序 `[q1, q2, q3]`、丢弃计数不再增长。反向实证：把登记还原到入队前，该用例 **1 failed**（受帧日志「企微回调重复投递（msgid 去重跳过）: q3」）；复原后 32 passed。

**验证**（主 Agent 实跑，命令与退出码）：

- `cd backend && pixi run tests-all` → **0**（**2837 passed**；新增 2 例 + 1 例签名同步）。
- `cd backend && pixi run coverage-gates` → **0**（24 组 ≥80%）。
- `cd backend && pixi run lint` → **0**（`All checks passed!`）；`pixi run typecheck` → **0**（191 源文件零问题）。
- 根 `pixi run gates` → **0**（注释门 / 同步测试棘轮 / noqa 冻结 / 沙盒漂移 / 红探针 / 文档真相 / 断言棘轮 / 导出面 / prose-pin 全过）。
- `cd backend && pixi run gen-examples` → **0** 连续两次；三件套 sha256 两次完全一致（幂等，无 env 键新增）。
- `cd frontend && pixi run test` → **0**（**666 passed** / 85 文件）；`pixi run lint` → **0**；`pixi run build` → **0**（仅既有 chunk>500kB 提示）。
- sidecar 树未改动，按任务书略过。

**剩余**：无。

### 里程碑：R2 全部批次交付并逐批通过 Oracle 深审

- 批次清单与结论：注入防御、logprobs 置信度、亲密补强（R2b+ 四切片）、R2c 记忆与关系（含修复）、R2d 心流/群聊/声纹/表情包（含修复）、R2e 企微/抖音/Canvas（含修复）、R2f 实时语音（含修复）、加固门禁（Tier-1+Tier-2，含复审残留）、补审 R2a 通道底座与记忆域（含残留）——全部经 Oracle 明确判定无问题（PASS），修复循环见各自小节的命令与证据。
- 合并状态：PR#18 至 PR#33 已全部合并入 main（`e95f5fe`），工作区 clean；每批验收证据在 `docs/evidence/`（按 sha8 对应）。
- 未决事项：无。审计 A（实现/架构，2×P1+3×P2）、审计 B（测试/纪律，1×P1+4×P2）、审计 C（文档/注释/提示词/配置，4×P1+3×P2）均已执行、修复并经 Oracle 复审通过（记录见 §5 各小节）；终局交叉复核提出的 1 条台账过期句已在本次修正，复审在该修正提交上执行。R3 启动条件 = 终局交叉复核 PASS（本段即该状态）。
- 恢复指引：R2 归档于 main `e95f5fe`；新工作从该点起分支，修复分支命名沿用 `feat/<范围>-review-fixes`；审计提示词口径见本会话记录（覆盖 19 条硬约束）。

### 已完成切片：全约束审计 A 修复（2×P1 + 3×P2，本分支工作区）

**已完成**（逐条闭环，分支 `feat/audit-a-fixes`，基线 main `f43d705`）：

1. **P1 QQ 纯附件消息被前置丢弃**：`qq_webhook` 先提取附件再判文本——仅有附件（纯图片 / 纯表情）而无用户字面文本时归一为 `text=""` 入站并投递（`_has_user_text` 剥离占位符与 CQ 码字面判定）；`InboundMessageModel.text` 允许空串（说明同步：仅附件入站语义）；`ChannelRouter.handle_inbound` 在表情收录与门控游标之后对空文本短路——不调用生成、不发兜底、不写空回合，只累加 `empty_text_skip_count`。同语义同步到 Telegram（`map_tg_message_to_inbound` 纯媒体无 caption → `text=""` 入站，sticker 附件可进表情桥）。既无文本又无附件仍丢弃。**已根修（G1）**：占位语义统一为 `has_user_text` 字段（四通道适配器按 wire 语义声明 + 路由器按字段跳过生成），跨通道用例锁定——见「G1 审计非根修项根修」节。
2. **P1 实时语音任务异常静默**：`_spawn` 的 done 回调消费并记录非取消异常（`logger.opt(exception=...)` + 任务名，`task_failure_count` 观测面）；`_audio_worker` 主体加防御——未预期异常记日志、下行可恢复 `INTERNAL_ERROR` error、`_request_close(INTERNAL_ERROR)`（不再永久失聪至最大时长）；`_transcribe_segment` 的 `pcm16_to_wav` 移入异常处理范围（空 / 不足一帧 PCM 不抛）。
3. **P2 写侧裸 dict 绕过 Model**：`_emotion_round_meta` 构造既有 `ChatMessageMetaModel`（thought / emotion_deltas / emotion_snapshot / variant_index 对齐），`_persist_turn` 形参改该 Model，落库前 `model_dump(exclude={"variant_index", "selected_variant_id"})` 保持旧三键 JSON 字节结构；读侧 DTO 兼容不变。
4. **P2 app/ 表情桥业务逻辑**：解码 / 筛选 / 出站包装下沉 `harness/multimodal/sticker_bridge.py`（`StickerBridge` + `decode_inbound_stickers` + `InboundStickerPayloadModel` + 服务面 Protocol）；`app/main.py` 删除两个闭包，仅注入 `StickerService` / `session_scope` 并接线钩子；「只收录内联 base64」与失败隔离语义不变。
5. **P2 沙盒公共池口径**：沙盒树源码隔离（不 import backend 公共池），`asyncio.to_thread` 使用进程级默认执行器、树内禁止自建 `ThreadPoolExecutor`——口径写入 `http_fetch.py` 模块注释与 `docs/backend/tools-sandbox.md`「阻塞 I/O 与线程池口径」小节；全树 `grep -rn "ThreadPoolExecutor(" docker/tool-sandbox-agent/src` 零命中。
6. **同步面**：`docs/backend/channels.md`（空文本入站 / QQ 仅附件 / 表情桥）、`docs/backend/multimodal.md`（桥模块与下沉边界）、`docs/backend/chat.md`（meta Model 写侧口径）；`red_manifest.toml` 登记 `sticker-bridge-decode-removal` 红探针（新源文件门禁要求）。新增 17 例测试（QQ 仅附件 2、Telegram 纯媒体路由 1、路由器空文本 1、实时语音 2、meta Model 1、表情桥 10）。

**验证**（主 Agent 实跑，命令与退出码）：

- `cd backend && pixi run tests-all` → **0**（**2862 passed**；基线 2845 + 新增 17）。
- `cd backend && pixi run coverage-gates` → **0**（24 组 ≥80%；harness/channels 97.0% / harness/chat 96.3% / harness/multimodal 93.9% / harness/voice 95.6% / app/main.py 93.1%）。
- `cd backend && pixi run lint` → **0**（`All checks passed!`）；`pixi run typecheck` → **0**（192 源文件零问题）。
- 根 `pixi run gates` → **0**（注释门 / 同步测试棘轮 / noqa 冻结 / 沙盒漂移 38 项 / 红探针门 / 文档真相 / 断言棘轮 / 导出面 / prose-pin 全过）。
- `cd backend && pixi run gen-examples` → **0** 连续两次；三件套 sha256 两次完全一致（幂等，无 env 键新增）。
- `cd docker/tool-sandbox-agent && pixi run tests-unit` → **0**（**118 passed**；`http_fetch.py` 覆盖率 100%）；同树 `ThreadPoolExecutor(` 源文件零命中。
- 前端未改动，按任务书略过；canonical 证据包 `docs/evidence/audit-a-fixes-da5b1bd3.json`（`verify-slice -- --slice audit-a-fixes` → **0**，5 步全 PASS：tests-all=2862 / coverage-gates=24 / typecheck=192）。
- **反向实证**（逐条临时还原实现 → 目标用例变红 → 复原）：
  - 还原 QQ 前置丢弃条件 → `test_qq_webhook.py` **3 failed**（纯图 / 纯表情 / record 文本空）；
  - 还原 Telegram 前置丢弃条件 → `test_pure_media_without_caption_routed_with_empty_text` **1 failed**；
  - `pcm16_to_wav` 移回 try 外 → `test_transcribe_segment_tolerates_empty_and_short_pcm` **1 failed**；
  - meta 落库改 `model_dump()` 全字段 → `test_reply_meta_is_model_validated_and_byte_compatible` **1 failed**（键面多出变体字段）；
  - 表情桥 base64 去掉空白折叠 → `test_decode_inbound_stickers_keeps_inline_base64_and_caption` **1 failed**；
  - 红探针：`pixi run red-probe -- --symbol packages.harness.multimodal.sticker_bridge:decode_inbound_stickers` → **RED（failed=2）**。

**剩余**：无（本切片范围）。

### 已完成切片：审计 A 复审残留闭环（P2×2，本分支工作区）

**已完成**（逐条闭环，分支 `feat/audit-a-fixes`）：

1. **P2 心流合并空文本 → 兜底文案**：`record_inbound` 新增仅关键字形参 `append_text`——仅附件入站（`text=""`）以 False 记录，到达时刻 / 积压照常递增、空串不进入 `buffered_texts`；`_apply_flow_gate` 按入站 `text.strip()` 是否为空传入该开关；`_run_flow_batch` 的跳过守卫改为 `if not merged.text.strip():`（清除 pending 后直接返回，防御任何空白合并）。新增只读观测面 `ChannelRouter.flow_state_for`（返回节拍状态深拷贝，供断言缓冲 / 积压 / 到达面）。用例：`test_empty_text_media_burst_keeps_buffer_clean_and_merges_real_text`（门控启用 + 伪时钟连发 3 条纯媒体：缓冲零空条目、到达与积压均为 3、批次到期零生成零兜底、pending 清除；随后真实文本合并为 `真实文本`，无前导换行）、`test_single_empty_text_media_skips_generation_without_fallback`（单条立即路径仍跳过生成且不排程 / 不兜底）、`test_record_inbound_without_text_append_keeps_arrival_and_backlog`（纯函数面）。`docs/backend/channels.md` 心流节与空文本语义同步（纯媒体批次无净文本直接结束；占位文本不算用户文本）。
2. **P2 QQ 占位符形态残留**：新增 `_has_non_text_content`（CQ 码字符串形态 `_CQ_CODE_RE.search`；段数组形态存在 `type != "text"` 的段）；入站归一条件改为「无用户字面文本且消息含非文本内容 → `text=""`」，丢弃条件改为「既无用户文本也无任何非文本内容」。CQ 字符串纯图片与纯 face 表情均以空文本投递（无可提取附件时路由器按 no-op 收录），混合文本 + 图片 / face 保留占位符进入生成面。用例 4 例：CQ 字符串纯图片 → `text=""`、纯 face 段 → `text=""`、混合文本 + face → `你好[表情]`、仅空 text 段 → 丢弃。既有 CQ / face 相关断言逐个核对后无需改动：`test_segment_array_image_face_placeholders` / `test_cq_string_flattening` / `test_cq_string_multiple_codes` 是展平纯函数断言（展平逻辑未变）；`test_cq_string_event_routed`（`在吗[CQ:face,id=2]`）带用户字面文本，仍为 `在吗[表情]`；`test_pure_sticker_face_with_image_routed_with_empty_text` 本就断言 `text=""`。qq_webhook 模块 docstring 与 `docs/backend/channels.md`（空文本语义 / QQ 仅附件 / 消息展平）同步为「占位文本不算用户文本，所有 wire 形态一致」。

**验证**（主 Agent 实跑，命令与退出码）：

- `cd backend && pixi run tests-all` → **0**（**2869 passed**；基线 2862 + 新增 7：心流 3、QQ 归一 4）。
- `cd backend && pixi run coverage-gates` → **0**（24 组 ≥80%；harness/channels 97.2% / harness/chat 96.3%）。
- `cd backend && pixi run lint` → **0**（`All checks passed!`）；`pixi run typecheck` → **0**（192 源文件零问题）。
- 根 `pixi run gates` → **0**（注释门 / 同步测试棘轮 / noqa 冻结 / 沙盒漂移 / 红探针门 / 文档真相 / 断言棘轮 / 导出面 / prose-pin 全过）。
- `cd backend && pixi run gen-examples` → **0** 连续两次；三件套 sha256 两次完全一致（幂等，无 env 键新增）。
- 前端未改动，按任务书略过。
- **反向实证**（逐条临时还原实现 → 目标用例变红 → 复原）：
  - 还原心流调用（`record_inbound(state, message.text, now)`）与批次守卫（`if not merged.text:`）→ `test_empty_text_media_burst_keeps_buffer_clean_and_merges_real_text` **1 failed**（`assert ['', '', ''] == []`——空条目进入缓冲）；复原后两文件 33 passed。
  - 还原 QQ 归一 / 丢弃条件（`attachments and not _has_user_text` / `not text.strip() and not attachments`）→ `test_qq_webhook.py` **2 failed**（CQ 字符串纯图片、纯 face 段未以空文本投递）；复原后三文件 73 passed。

**剩余**：无（本切片范围）；该批复审已执行并随 PR #33 合入 main。

### 已完成切片：R3-1 安全修复（P1×2 + P2×5，已合并 PR #35）

**已完成**（逐条闭环，分支 `feat/r3-security` 已并入 main）：

1. **P1 群聊 peer 写入长期指令污染受信层**：`IntentShortcutService.try_shortcut` 新增仅关键字 `sender_provenance`（默认 `owner`，既有直调不变），`ChatService.reply` 透传真实来源；DIRECTIVE 捕获副作用改为仅 `owner` 执行，群聊统一按 `peer` 处理（群内主人发言同样不写指令，只回附和文案）。`chat/service.py` 的 `IntentShortcutFace` 协议与 docstring 同步。docs：`persona.md` 长期指令来源收口、`defense.md` 受信层表与攻击面矩阵、`intent.md` DIRECTIVE 行、`chat.md` 意图快径节。测试：`test_intent_directive.py` 新增 peer 不建指令仍回复 / owner 经真实 `DirectivesService` 建指令两例；`test_intent_shortcut_wiring.py` 新增 peer 来源透传一例。
2. **P1 生产 fail-closed 鉴权**：新增 `RuntimeEnv` 枚举（`local`/`dev`/`test`/`prod`）与独立纯函数 `packages/harness/config/auth_policy.py::ensure_auth_policy`（`ENV=prod` 时空管理面 token 拒绝启动；QQ 通道启用时空入站 token 拒绝启动）；`app/middleware/admin_auth` 的 `resolve_admin_token` 迁入该模块并保持导出面；`startup_app_state` 在组件装配前调用。docs：`README.md` 认证节改写为「R3 起生产强制」、`config.md` ENV 枚举说明、`channels.md` QQ token 行与部署要点、`.env.example` 经生成器同步（无新增 env 键）。测试：`test_auth_policy.py` prod+空拒绝 / prod+非空通过 / dev+空通过 / prod+QQ 启用+空 webhook 拒绝 / startup 接线五例。
3. **P2 企微出站附件守卫**：新增 `packages/harness/channels/outbound_guard.py`（`validate_http_url`：协议白名单 + 私网 IP 字面量拒绝；`is_private_ip_literal`；`validate_attachment_size`：8MB 帽）；`wecom_runner._fetch_url` 改为流式累积并在超帽时受控失败，`_read_local_file` 以 `read(帽+1)` 限定并注明 path 源仅内部生产者。测试：`test_outbound_guard.py` 协议 / 私网矩阵 / 大小边界；`test_wecom_runner.py` 新增 url 超帽 / 本地超帽 / 私网字面量三例（既有 32 例全绿）。
4. **P2 http_fetch 私网对称**：沙盒树 `tool_sandbox/http_fetch.py` 移植私网判定（`ip_literal_host_is_private` / `ip_text_is_private`），允许清单命中后仍拒绝私网字面量；首跳 / 重定向 / 终态均经 `socket.getaddrinfo` 解析复检（全部地址须公网，解析失败 fail-closed；解析面可注入）。`tools-sandbox.md` 边界表与测试矩阵同步。测试：`test_http_fetch.py` 新增私网字面量矩阵、解析到私网 / 混合解析 fail-closed / 公网通过；为保 hermetic，既有 opener 测试辅助与重定向透传用例注入固定公网解析面。
5. **P2 base64 DTO 请求体上限**：声纹 / 表情包模型新增模块常量 `VOICEPRINT_MAX_UPLOAD_BYTES` / `VOICEPRINT_AUDIO_BASE64_MAX_LENGTH` 与 `STICKER_MAX_UPLOAD_BYTES` / `STICKER_IMAGE_BASE64_MAX_LENGTH`（按字节帽 4/3 精确推导，设置默认与 DTO 上限同源）；`audio_base64` / `image_base64` 增加 `max_length`，服务层既有精确字节校验未动。测试：`test_base64_payload_bounds.py` 上限通过 / 超一字符拒绝 / 编码增量拒绝 / 常量同源五例；`test_voiceprint_api.py` / `test_sticker_api.py` 各新增超帽请求体被共享校验处理器拒绝一例。DTO schema fixture 已重生成。
6. **P2 鉴权契约文档漂移**：`docs/backend/README.md` 保护面表补 canvas 行；`admin_auth.py` docstring 改为保护面清单以 `app/main.py` `_include_routers` 为唯一权威（当前 15 个路由集）。测试：`test_auth_surface_docs.py` 解析 README 保护行与装配层 AST 逐项比对（归一化分隔符），数量 15 且含 canvas。
7. **P2 日志掩码连字符形态**：`core/logging.py` 的敏感 key 模式将关键词内 `_` 展开为 `[-_]`（覆盖 `X-Api-Key` / `access-token`），值长度阈值与 Bearer / sk- 规则不变。测试：`test_core_logging.py` 新增连字符形态矩阵（含 Bearer / sk- 不回归与普通键不动）。

**验证**（命令与退出码）：

- `cd backend && pixi run tests-all` → **0**（**2927 passed**；基线 2875 + 52 例）。
- `cd backend && pixi run coverage-gates` → **0**（2927 passed；24 组 ≥80%，harness/intent 100.0% / harness/config 96.5% / harness/channels 97.3%）。
- `cd backend && pixi run lint` → **0**（`All checks passed!`）；`pixi run typecheck` → **0**（`Success: no issues found in 194 source files`）。
- 根 `pixi run gates` → **0**（注释门 / 同步测试棘轮 / noqa 冻结 / conftest env 196/196、豁免 0 / 沙盒漂移 38 项 / 红探针门 193 源文件·6 探针 / 文档真相 / 断言棘轮 / 导出面 102 引用·62 登记·40 豁免 + env 键面 689 行 / prose-pin）。
- `cd backend && pixi run conftest-env` → **0**（登记表 196 / 硬覆盖 196 / 豁免 0）。
- `cd backend && pixi run red-probe -- --all` → **0**（6 条探针全部 RED：含新增 auth-policy-removal failed=2、outbound-guard-removal failed=1）。
- `cd backend && pixi run export-dto-schemas` → **0**（94 组；base64 长度帽进入 schema）。
- `cd backend && pixi run gen-examples` 连跑两次 → 两次均 **0**；三件套 sha256 运行前 / 第一次 / 第二次完全一致（幂等，无 env 键新增）。
- `cd docker/tool-sandbox-agent && pixi run tests-unit` → **0**（128 passed；总覆盖率 96.71%，http_fetch 90%）；`pixi run lint` / `pixi run typecheck` → **0**（6 源文件零问题）。

**既有测试同步（列明）**：`test_intent_shortcut_wiring.py::FakeIntentFace.try_shortcut` 增加 `sender_provenance` 形参并记录（协议签名同步，断言不变）；沙盒 `test_http_fetch.py` 的 `_patch_opener` 与重定向透传用例注入固定公网解析面（DNS 复检引入后的 hermetic 同步，断言不变）；`test_voiceprint_api.py` / `test_sticker_api.py` / `test_core_logging.py` 仅新增用例。无断言弱化、无既有用例删除。

**偏离与决策**：共享异常处理器把 pydantic `RequestValidationError` 归一为 HTTP **400**（既有契约），新增 base64 超帽 API 用例按 400 断言而非 FastAPI 默认 422；DTO 长度帽按代码默认字节帽精确推导（未加 padding），运行期把 `max_upload_bytes` 调到默认以上不会扩展 wire 上限（docstring 已注明），服务层精确字节校验保持；`ENV` 收紧为 `RuntimeEnv` 枚举后，`local`/`dev`/`test`/`prod` 之外的值在配置解析期被拒绝。

**剩余**：无（本切片范围）。

**终态**：已合并 PR #35，验收证据包 `docs/evidence/r3-security-e8232b42.json`（`verify-slice -- --slice r3-security` → **0**）。深审复审对本切片 8 项逐条核验，结论为 **8 项逐一核验 PASS**——该切片走「逐条核验」口径，不产生 MATCH / 偏离 / VIOLATION 三分类计数，故不计入 §5.4 符合性统计的 MATCH 列。

### 已完成切片：R3-2 限流/背压修复（P1×1 + P2×7，已合并 PR #36）

**已完成**（逐条闭环，分支 `feat/r3-security` 已并入 main）：

1. **P1 QQ 入站有界化**：新增 `channels/qq/qq_runner.py`（`QqWebhookRunner`——容量 64 / worker 2 / 去重窗 1024 均为代码常量非 env，与企微 runner 同风格）；`qq_webhook.py` 拆出纯解析面 `parse_webhook_event`（不可路由事件返回 None、不登记去重），`process_webhook_event` 保留单事件直调语义（去重 + 路由，恒不抛）。端点改为 `runner.submit(payload)` 后**立即 200**（runner 未装配的嵌入形态回落直调）；队列满丢弃并计 `dropped_inbound`（ERROR 含 message_id / 队列深度 / 累计丢弃，不登记去重）；lifespan 装配 runner + 托管任务，停机 `request_stop` → cancel worker → 清空队列（槽位复位）。测试：`test_qq_runner.py` 10 例（容量满丢弃/计数/日志、丢弃不登记去重与重投恢复、重复吸收、不可路由不登记、worker 消费、停机零残留、周期观测三态）+ `test_channels_qq_api.py` 慢路由下立即确认 + `test_app_qq_channels_lifespan.py` 装配/停机槽位。
2. **P2 实时语音 TTS 队列有界**：`REALTIME_VOICE_TTS_QUEUE_MAX`（默认 32，≥1）→ 队列 maxsize；满时丢最旧句并计 `tts_chunks_dropped`（与音频队列同策略），关闭指标日志新增 `tts_dropped=`。测试：`test_realtime_session.py` 满/丢最旧顺序/计数/关闭清零与 `tts_queue_max=0` 拒绝。
3. **P2 WS 会话帽**：`REALTIME_VOICE_MAX_SESSIONS`（默认 2，≥1）→ 进程内并发计数；超限下行 `error(RATE_LIMITED)` 并以 **1013** 拒绝；名额在 `finally` 释放（含人格解析 / 装配失败的提前返回路径）。测试：两连通过、第三连 1013、结束释放后新连可用（API 层注入人格/聊天装配，避免共享库连接争用）。
4. **P2 LLM 日预算闸**：`LLM_DAILY_BUDGET_GLOBAL` / `LLM_DAILY_BUDGET_PERSONA`（默认 0 = 不限，`ge=0`）落在 `LlmEndpointConfig`；网关在条目门控 / 传输前按 persona→global 判定，超限抛既有 `RATE_LIMITED`（details 含 scope/used/limit[/persona_id]），**不落用量行**；计数经 `wire_default_usage_counter()`（usage DB 口径，跨 worker 近似一致——软竞态写入 llm.md）；两档均 0 时零计数查询。慢调用 WARN：阈值常量 `SLOW_CALL_WARN_MS=10s`，success 路径输出 entry/model/purpose/latency（不打印内容）；网关新增注入式 `clock` 支撑判定。测试：persona/global 超限、limit-1 放行与 limit 拒绝、0 不限零查询、无人格上下文走 global、流式首迭代前拒绝、慢调用触发/不触发（伪时钟）。
5. **P2 workers>1 语义**：`docs/backend/config.md` 新增「多 worker 语义」节（进程内限值 ×N、去重窗 / 心流 / per-chat 锁 / 实时语音计数不跨进程共享、预算 DB 计数近似全局）；`docs/backend/pools.md` 同步一句；代码零变更。
6. **P2 最小观测面**：企微 / QQ runner 增加周期 INFO（`WECOM_STATS_INTERVAL_MS` / `QQ_STATS_INTERVAL_MS` 均为 60s 代码常量，注入时钟节流；仅 pending>0 或 dropped 有增量时输出深度/增量）；LLM 慢调用 WARN 同第 4 条。测试：两 runner 各自「到达间隔+有积压输出」「空队列静默」（QQ 另测未到间隔静默）+ LLM 阈值两组。
7. **P2 前端 Node 护栏**：`frontend/package.json` 增 `engines.node=">=22 <23"`，`frontend/.npmrc` 增 `engine-strict=true`；`frontend/README.md` 的 Pixi 命令节与根 README 快速开始注明必须经 pixi（Node 22），勿直接 npm。
8. **P2 构建体积基线**：`frontend/README.md` 新增「构建体积基线（决策）」——单 chunk 717.44 KB / gzip 213.91 KB、CSS 23.26 KB / gzip 5.28 KB；审计实测构建 6.16s、本机复跑 3.61s（负载相关）。决策维持单 chunk，触发条件 >1 MB 或部署机 LCP 明显退化再拆路由级 lazy（先 realtime / memories）。§5.2 已同步性能行。
9. **env / 模板 / 向导 / conftest 同步**：4 个新键经 `_ENV_SECTIONS` 生成器进入 `.env.example` 与 `config.md` 分节表（`--sync-docs`）；向导按既有前缀自动落入「LLM 端点配置」/「实时语音会话配置」步骤；`conftest.py` 硬覆盖 4 键（门禁 200/200）。
10. **文档**：`channels.md`（QQ 链路 / runner 行 / 有界队列与确认语义 / 去重边界）、`realtime-voice.md`（TTS 队列 / 并发帽 / 错误码 / 配置行 / 测试面）、`llm.md`（链路 ⓪ 预算闸、构造注入、日预算与慢调用节、测试位置）、`config.md`、`pools.md`、`frontend/README.md`、根 README、本台账。

**验证**（命令与退出码）：

- `cd backend && pixi run tests-all` → **0**（**2950 passed**；基线 2927 + 23 例）。
- `cd backend && pixi run coverage-gates` → **0**（2950 passed；24 组 ≥80%，harness/channels 97.3% / harness/llm 98.0% / harness/voice 95.4% / app/api 83.5% / app/main.py 94.3%）。
- `cd backend && pixi run lint` → **0**（`All checks passed!`）；`pixi run typecheck` → **0**（`Success: no issues found in 195 source files`）。
- 根 `pixi run gates` → **0**（注释门 violations=0；同步测试棘轮通过；noqa/type:ignore 基线 0；conftest env 200/200、豁免 0；沙盒漂移 38 项一致；红探针门 194 源文件 / 探针 7 条；文档真相；断言棘轮；导出面 102 引用·62 登记·40 豁免 + env 键面 701 行；prose-pin 报告）。
- `cd backend && pixi run red-probe -- --symbol packages.harness.channels.qq.qq_runner:QqWebhookRunner` → **1**（期望 RED：failed=1——探针「去掉实现必变红」的正向证据）。
- `cd backend && pixi run gen-examples` 连跑两次 → 两次均 **0**；三件套 sha256 运行前 / 第一次 / 第二次完全一致（幂等）。
- `cd frontend && pixi run test -- --maxWorkers=4` → **0**（**666 passed** / 85 文件）；`pixi run lint` → **0**；`pixi run build` → **0**（单 chunk 717.44 KB / gzip 213.91 KB，构建 3.61s）。
- 前端未改业务代码（仅 package.json engines / .npmrc / README）；默认并发跑 `pixi run test` 出现既有负载敏感用例 `useStickersApi.test.ts` 的 `waitFor` 1s 超时（LEDGER 既载已知项），按既有做法以 `--maxWorkers=4` 复跑全绿。

**偏离与决策**：

- **预算计数口径**：单次 usage 查询（不按 purpose 过滤）以保持每调用 O(1) 查询；因此 search / tts / asr / voiceprint 的 usage 行也占用 LLM 日预算（模型调用总量闸语义），分面独立预算列入边界（见 §6）。
- **「恰好等于上限」语义**：沿用 search 预算的 `used >= limit` 拒绝；测试同时锁定 `used = limit-1` 放行与 `used = limit` 拒绝（「调用后恰好达到上限」放行）。
- **QQ 端点保留直调回落**：`app.state.channels_qq_runner` 未装配时仍走 `process_webhook_event`（嵌入 / 最小 app 形态与既有 API 测试兼容）；生产 QQ 启用形态恒装配 runner。
- **WS 并发帽为进程内计数**：多 worker 部署每进程独立（config.md 已写明）；强全局上限需跨进程存储，本工程 NO Redis 决策下不引入。
- **周期观测只在有事件入站时检查节流点**：空闲期无 ticker 任务，无积压可报；有积压时下一条入站即触发（与「仅当 pending>0 或 dropped 有增量」一致）。

**剩余**：无（本切片范围）。

**复审修复（Oracle 深审本切片后 6×P2 逐条闭环）**：

1. **P2 预算 >10,000 静默失效**：`LlmEndpointConfig.daily_budget_global` / `daily_budget_persona` 增 `le=10_000`（计数页帽 50×200 为有效上限，超出则预算永远无法触发）；字段说明与 `docs/backend/llm.md`、`docs/backend/config.md` 注明「有效上限 10,000（计数页帽 50×200）」，`.env.example` 经生成器重出。测试：`test_config_settings.py` 新增上限校验（10,000 通过 / 10,001 双档拒绝）。
2. **P2 计数器分页零测试**：`test_llm_gateway.py` 新增 `wire_default_usage_counter` 分页用例 4 例（共享 SQLite + ORM 批量灌行）：201 行跨页累计 = 201（200+1 且尾页终止）、恰好 200 行 = 200、空库 = 0、10,200 行按页帽截断 = 10,000。
3. **P2 embed 预算零测试**：`test_llm_embeddings.py` 新增 embed 预算耗尽用例——`RATE_LIMITED`、details 形状与 persona 档一致（scope/used/limit/persona_id）、零出站、零用量事件。
4. **P2 qq_webhook docstring 虚构共享窗口**：模块与 `process_webhook_event` docstring 改为「共享解析面与去重窗口类型（各自实例：直调路径用模块级窗口，runner 持独立窗口）」；`docs/backend/channels.md` 无同型失真句，未改。
5. **P2 llm.md 流程 ⓪/① 顺序失真**：调用链图按实码重排（chat/stream 先角色解析后预算闸），并补图注「chat/stream：角色解析后、条目门控/传输前；embed：解析前」；日预算节同步位置描述与有效上限。
6. **P2 qq runner worker 异常隔离零覆盖**：`test_qq_runner.py` 新增用例——首条抛 `RuntimeError` 后 worker 记录 ERROR 并继续消费第二条；断言两条均进入 router、第二条正常处理、runner 未停机（事件门等待，无固定 sleep）。

**验证**（命令与退出码）：

- `cd backend && pixi run tests-all` → **0**（**2957 passed**；基线 2950 + 7 例）。
- `cd backend && pixi run coverage-gates` → **0**（2957 passed；24 组 ≥80%，harness/llm 99.0% / harness/channels 97.3% / harness/config 96.5% / app/api 83.5% / app/main.py 94.1%）。
- `cd backend && pixi run lint` → **0**（`All checks passed!`）；`pixi run typecheck` → **0**（`Success: no issues found in 195 source files`）。
- 根 `pixi run gates` → **0**（注释门 violations=0；同步测试棘轮通过；noqa/type:ignore 基线 0；conftest env 200/200、豁免 0；沙盒漂移 38 项一致；红探针门 194 源文件 / 探针 7 条；文档真相；断言棘轮；导出面 102 引用·62 登记·40 豁免 + env 键面 701 行；prose-pin 报告）。
- `cd backend && pixi run conftest-env` → **0**（登记表 200 / 硬覆盖 200 / 豁免 0）。
- `cd backend && pixi run gen-examples` 连跑两次 → 两次均 **0**；三件套 sha256 运行前 / 第一次 / 第二次完全一致（幂等，无 env 键新增）。

**终态**：已合并 PR #36，验收证据包 `docs/evidence/r3-backpressure-fe261c9a.json`（`verify-slice -- --slice r3-backpressure` → **0**）。深审复审对本切片 21 个计划点的三分类结果为 **MATCH 16 / 已记录偏离 5 / VIOLATION 0**，偏离项均为上列「偏离与决策」与复审修复中已记录的口径取舍；明细并入 §5.4 符合性统计，无未闭环项。

### 5.1 死代码处置（审计 B 抽样：5 个零引用公共符号）

复核口径：对五个符号逐一执行 `grep -rIn` 覆盖仓库全树（backend / frontend /
scripts / docs / docker / models；排 `__pycache__` / `.pixi` / `node_modules` /
`.git` / `logs` / `coverage.json` / `dtoSchemas.json`）→ **零命中**（仅定义处与
`__all__` 自身）。无任何非测试消费面，全部采用方案 A（删除并同步 `__all__`）。

| 符号 | 位置 | 方案 | 优点 | 缺点 | 结论 |
|---|---|---|---|---|---|
| `render_config_yaml_text` | `config/registry_admin.py` | 删除并同步 `__all__` | 导出面与实际消费一致，消除误导性公共 API | 未来若需完整 yaml 文本需重写（一次性转发，成本极低） | **选定**：基座 `ConfigSectionAdmin` 已统一经 `_dump_config_yaml_text` 渲染，此函数只做 llm 段转发 |
| 同上 | 同上 | 接线消费 | 公共面成为唯一渲染路径 | 与基座渲染职责重叠，llm 段出现第二条序列化路径 | 非选定（重建） |
| 同上 | 同上 | 保留 | 零改动零风险 | 零引用符号持续扩大导出面，读者误判存在外部消费 | 非选定（重建） |
| `current_settings_database` | `database/engine.py` | 删除并收窄 import | 去掉无意义间接层，`Settings` 导入随之消除 | 若未来需要统一读取面需重加 | **选定**：`init_database` / `rebuild_database_engine` 已直接读 `settings.database` |
| 同上 | 同上 | 接线消费 | 读取路径统一 | 对单字段读取多一层函数调用，仍无第二个消费者 | 非选定（重建） |
| 同上 | 同上 | 保留 | 零改动 | 测试/可观测性辅助实际无人使用，属于名义 API | 非选定（重建） |
| `payload_for_kind` | `memory/models.py` | 删除并同步 `__all__` | 判别表真源只保留一个消费点（`parse_episode_payload`），减少误导 | 需要单取载荷类时需重加一行转发 | **选定**：`_PAYLOAD_BY_KIND` 判别表由解析函数直接消费，公共转发无调用方 |
| 同上 | 同上 | 接线消费（解析函数改走它） | 公共面成为唯一查询路径 | 解析函数多一次转发，无行为收益 | 非选定（重建） |
| 同上 | 同上 | 保留 | 零改动 | 与解析函数的职责重复，读者无法判断应使用哪个入口 | 非选定（重建） |
| `RealtimeInboundType` / `RealtimeOutboundType` | `voice/realtime_models.py` | 删除并同步 `__all__` | 判别联合的 wire 真源保持为 `Literal`，避免两套类型真源 | 未来若需枚举化判别需重加并改联合定义 | **选定**：上下行消息全部用 `Literal` 判别，两个枚举从未参与解析、校验或导出 |
| 同上 | 同上 | 接线消费（改为枚举判别） | 类型面统一为枚举 | 改变 pydantic 判别联合的 wire 校验形态，收益低于改动风险 | 非选定（重建） |
| 同上 | 同上 | 保留 | 零改动 | 读者误以为枚举是协议真源，实际解析不经过它们 | 非选定（重建） |

**prose-pin `other` 桶（历史处置补记）**：该桶已在加固门禁复审中删除（实现与
报告均不再输出 `other`；分桶仅 `field_or_code` 与 `prose`）。按同格式补记当时
的三案对照：

| 方案 | 优点 | 缺点 | 结论 |
|---|---|---|---|
| 删除不可达分支并同步三处文档 | 分桶输出与实现一致，无维护噪声 | 历史报告口径需注明变更 | **选定（历史处置：已删除）** |
| 保留空桶 | 零改动 | 判定域与 `prose` 完全重合、分支不可达，读者误以为存在第三类 | 非选定（重建） |
| 合并进 `prose` 并保留报告字段 | 报告字段兼容旧读者 | 字段恒为空，同样需要文档解释 | 非选定（重建；最终效果与删除一致） |

### 5.2 性能变更决策记录（复审必修项）

| 变更 | 来源 | 是否入 R3 Plan | 影响面与后续观察点 |
|---|---|---|---|
| `skills.match_for_compose` 在 500 行扫描帽前排序（`use_count DESC, id ASC`） | 复审必修（R2c 补审：扫描确定性） | **否——随复审即时落地** | 影响：扫描帽只裁剪低强化候选，高强化技能不再被插入顺序挤出命中面；观察：命中分布、`EXPLAIN` 计划稳定性、扫描上限是否需要随技能库规模调整 |
| `ff_persona_facts` 复合索引 0018（`persona_id, status, confidence`） | 复审必修（R2c 深审：事实查询性能） | **否——随复审即时落地** | 影响：按人格 / 状态 / 置信度的现行事实查询；观察：迁移链升 / 降 / 复升、慢查询日志、写入放大（插入与更正路径的索引维护成本） |
| 前端维持单 chunk（minified 717.44 KB / gzip 213.91 KB） | R3-2 构建体积基线实测 | **否——维持现状** | 影响：管理台首屏加载单文件；观察：单 chunk >1 MB 或部署机 LCP 明显退化时再引入路由级 `lazy()`（先拆 realtime / memories，保留共享 vendor chunk）。基线见 frontend/README.md「构建体积基线」 |
| 核心纯函数 + ASGI 进程内只读端点的性能基线（`bench_core.py` / `api_smoke.py`，10× 环境容忍） | R3-3 性能基线交付 | **否——本切片建档** | 影响：门控 / 关系摘要 / 句切 / VAD / 关系块渲染与 health / models / personas / canvas 只读面有可复跑基线；观察：同机 median / p95 漂移、门禁误报率、后续热点函数是否入表。基线见 [backend/performance.md](backend/performance.md) |

### 5.3 性能基线回归面（本切片交付）

新增两条无新依赖、无新 env 键、无迁移的性能回归面（完整说明与阈值策略见
[backend/performance.md](backend/performance.md)）：

- `scripts/bench_core.py`：6 项纯本地微基准（`flow_gate.evaluate_flow_gate` /
  `build_relationship_summary` 200 事实与空事实 / 长文本句切 / VAD 60 帧喂入 /
  关系块渲染），`perf_counter_ns` 预热 + 15 轮取样，报告 median / p95 微秒；
  基线 `scripts/.bench_baseline.json`（median / p95 上限 + `max_ratio=10.0`）；
- `scripts/api_smoke.py`：ASGI 进程内 4 个只读端点（health / models entries /
  personas / canvas）各预热 3 次、取样 20 次，报告 p50 / p95 毫秒；隔离
  Settings + 一次性临时 SQLite，不读开发者 `.env`；基线
  `scripts/.api_smoke_baseline.json`（p50 / p95 上限 + `max_ratio=10.0`）。

**本机建库实测（基线值；同机复跑为准）**：

| 用例 / 端点 | 基线 median / p95 | 单位 |
|---|---|---|
| `flow_gate.evaluate_flow_gate` | 1.659 / 2.163 | µs |
| `persona.build_relationship_summary.200_facts` | 356.228 / 383.772 | µs |
| `persona.build_relationship_summary.empty_facts` | 2.943 / 3.360 | µs |
| `voice.split_assistant_sentences.long_text` | 221.407 / 263.124 | µs |
| `voice.energy_vad.feed_60_frames` | 720.510 / 751.046 | µs |
| `chat.render_relationship_block.synthetic` | 3.482 / 3.942 | µs |
| `GET /api/v1/health` | 0.534 / 0.651 | ms |
| `GET /api/v1/models/entries` | 0.578 / 0.648 | ms |
| `GET /api/v1/personas` | 3.098 / 3.587 | ms |
| `GET /api/v1/canvas?persona_id` | 3.596 / 5.184 | ms |

canvas 端点基线 p95（5.184 ms）约为 health（0.651 ms）的 8 倍：该端点是按
persona 过滤的产物列表查询，临时 SQLite 上存在行集扫描与逐行 DTO 物化，
首样还叠冷缓存页面读取（health 为零依赖存活面，差异属「有数据库查询」与
「无数据库查询」的结构区别）；10× 容忍覆盖此差异，不以该端点做绝对性能
承诺（原因详见 [backend/performance.md](backend/performance.md) 基线表下
说明）。

**验证**（本机实跑，命令与退出码）：

- `cd backend && pixi run bench-core --gate` → **0**，连续 2 次 PASS（6 项
  median 均在基线 ×10 内）；
- `cd backend && pixi run api-smoke --gate` → **0**，连续 2 次 PASS
  （4 端点均 200；p95 均在基线 ×10 内）；
- 人为从基线删除一条用例键后 `bench-core --self-check` → **1**（打印
  「注册表有、基线缺: flow_gate.evaluate_flow_gate」）；恢复后 → **0**；
  端点基线同理（删 `GET /api/v1/health` → **1**，恢复 → **0**）；
- 两脚本各执行 `--update` 后 `--gate` → **0**（更新后的基线仍可过门禁）。

门禁链落位：根 `pixi run gates` 在 prose-pin 之前加入
`bench-core --self-check`、`bench-core --gate`、`api-smoke --self-check`；
`api-smoke --gate` 需建临时库真实取样，对同机负载敏感，**不入链**，在
部署机 / 负载可控环境手动执行。**基线随硬件漂移，以同机复跑为准；跨机
绝对耗时不可比。**

**复审修复（深审报告 4 条 P2 逐条闭环）**：

1. `scripts/api_smoke.py`：`main()` 外层捕获导入期 / 隔离构造的 pydantic
   `ValidationError`，折叠为单行受控错误（含首个错误字段摘要）并 exit 1——
   无关分节的非法环境值不再抛裸 traceback；正常路径与 0 / 2 退出码语义不变。
2. `scripts/bench_core.py` / `scripts/api_smoke.py`：`_write_baseline` 改为
   先在目标同目录完整写 `.tmp`、再 `os.replace` 原子替换，失败在 `finally`
   清理临时文件；JSON 结构与键序（顶层 / 条目）与更新前逐项一致。
3. `docs/backend/performance.md`：隔离节补「无关分节非法环境值显式拒跑
   （单行错误；与真实 app 启动失败同源，属设计内受控失败）」；基线表下补
   canvas p95 约 8 倍于 health 的结构原因（persona 过滤列表查询的行集扫描与
   冷缓存首样；10× 容忍覆盖该差异，不作绝对性能承诺）。
4. 根 `README.md` 快速开始区补性能基线指向行（`docs/backend/performance.md`
   与 `bench-core` / `api-smoke` 入口）。

**复审修复验证**（本机实跑，命令与退出码）：

- `FLOW_BURST_COUNT=99 pixi run api-smoke --gate` → **1**；脚本输出恰 1 行
  「非本次冒烟相关环境变量取值非法……burst_count: Input should be less than
  or equal to 20（type=less_than_equal）」，`Traceback` 出现 0 次；同命令在
  干净环境 → **0**（4 端点 200，p95 均在基线 ×10 内）。
- `pixi run bench-core --update` 连跑 2 次 → **0 / 0**，随后 `--gate` → **0**；
  `pixi run api-smoke --update` 连跑 2 次 → **0 / 0**，随后 `--gate` → **0**；
  两基线 JSON 顶层键序与条目键序与更新前一致、脚本目录无 `.tmp` 残留（数值
  复跑后恢复交付前字节，本切片不含性能口径变化）。原子写故障探针（目标为
  目录 → `os.replace` 失败）对两脚本同款函数各跑一次：原目标内容不变、
  无 `.tmp` 残留。
- `pixi run bench-core --self-check` / `api-smoke --self-check` → **0 / 0**；
  `pixi run bench-core --gate` / `api-smoke --gate` → **0 / 0**。
- 全量门禁：`pixi run tests-all` → **0**（2957 passed）；
  `pixi run coverage-gates` → **0**（24 组 ≥80%）；`pixi run lint` → **0**；
  `pixi run typecheck` → **0**（195 files）；`pixi run conftest-env` → **0**
  （登记表 200 / 硬覆盖 200 / 豁免 0）；根 `pixi run gates` → **0**；
  `pixi run gen-examples` → **0** 且三件套 sha256 前后一致（幂等）。

**符合性合计**：R3 三切片合计 338 个计划点 = MATCH 310 / 已记录偏离 28 /
VIOLATION 0（逐项明细见 §5.4；R3-1 的复审为 8 项逐一核验 PASS，走核验口径，
不计入 MATCH 列）。

**canonical 证据**：`docs/evidence/r3-perf-16b07c7c.json`
（`verify-slice -- --slice r3-perf` → **0**，5 步 PASS：tests-all=2962 /
coverage-gates=24 / typecheck=195）；`758a3ee1` / `bc71f261` 为前序源码树的
历史证据，只在复核历史时引用。

### 5.4 R3 符合性统计

R3 三切片的计划点与三分类核验结果。计划点 = 审计 / 复审报告列出的条目数；
MATCH = 代码与报告要求一致；已记录偏离 = 实现或口径与报告字面不同、但已在
台账记明理由并经复审接受；VIOLATION = 未被接受的不一致。

| 切片 / 审计面 | 计划点 | MATCH | 已记录偏离 | VIOLATION |
|---|---:|---:|---:|---:|
| R3 安全审计 C1（提示注入纵深） | 108 | 96 | 12 | 0 |
| R3 安全审计 C2（SSRF / 出站守卫） | 81 | 79 | 2 | 0 |
| R3 安全审计 C3（SeekDB DSL 注入） | 50 | 43 | 7 | 0 |
| R3 安全审计 C4（密钥与鉴权治理） | 62 | 60 | 2 | 0 |
| R3-2 复审计划点 | 21 | 16 | 5 | 0 |
| R3-3 复审计划点 | 16 | 16 | 0 | 0 |
| **合计** | **338** | **310** | **28** | **0** |

口径说明：

- C1–C4 是 R3 安全审计面（提示注入 / SSRF / SeekDB DSL / 密钥与鉴权）的计划
  点三分类结果；R3-1 切片另有「8 项逐一核验 PASS」的复审结论，那是切片复审
  口径、不产生 MATCH 计数，因此不重复计入上表。
- R3-2 的 5 个已记录偏离指预算计数口径、QQ 直调回落、WS 进程内计数、周期
  观测触发点与「恰好等于上限」语义，均在 §5 对应「偏离与决策」段记明。
- 本表是 R3 符合性口径的唯一来源；此前任何与本表不符的写法（例如把计划点
  写成 301、或把 MATCH / 偏离写成别的合计）一律以本表为准。

### 已完成切片：R3 终局复核修复（P2×7 逐条闭环，本分支工作区）

**已完成**（逐条闭环，分支 `feat/r3-perf`；定位来自 R3 终局交叉复核，
该复核结论为无 P0/P1、7×P2）：

1. **P2 台账 §3 过期**：R3 行拆为三切片状态（R3-1 ✅ PR #35 / 证据
   `e8232b42`；R3-2 ✅ PR #36 / 证据 `fe261c9a`，PR body 引用的 `0276c1a5`
   标为首提交树证据；R3-3 PR #37 open / canonical 证据 `16b07c7c`）；「当前
   分支状态」改为 main `df2ca2d`（PR#34–#36 已并入）、补 `feat/r3-perf`
   （PR #37 open）、删除 `docs/final-cross-review-ledger` 待合并句。
2. **P2 台账 §5 收口**：R3-1 / R3-2 两段各补「终态」行（合并 PR 编号、证据
   包路径、复审结论）；段内「本分支工作区」「复审由主 Agent 后续执行」一律
   改为已完成事实。R3-1 终态口径为「8 项逐一核验 PASS」，R3-2 为
   MATCH 16 / 已记录偏离 5 / VIOLATION 0。
3. **P2 符合性合计口径**：新增 §5.4「R3 符合性统计」表（C1 108=96/12/0、
   C2 81=79/2/0、C3 50=43/7/0、C4 62=60/2/0、R3-2 21=16/5/0、R3-3 16=16/0/0、
   合计 338=310/28/0），并声明本表为唯一口径来源；§5.3 补「符合性合计」行，
   注明 R3-1 的 8 项核验 PASS 不并入 MATCH 计数。
4. **P2 bench-core 不 hermetic**：`_make_vad_feed_case` 把
   `RealtimeVoiceSettings` 全部 21 个字段显式传参（补齐 12 个），用例输入
   不再受进程环境 / `.env` 影响；`main()` 增加与 `api_smoke` 同款
   `ValidationError` 折叠（延迟导入判定、单行 loc/msg/type 摘要、exit 1，
   其余异常原样上抛）；模块 docstring 的 Exit 契约同步。`scripts/bench_core.py`
   的 `_print_table` 标题行顺带去掉无效 f-string 前缀（ruff F541）。
5. **P2 断言棘轮基线**：`--update` 重建 `scripts/.assertion_baseline.json`，
   并入 7 份既有基线外测试文件与本切片新增的 1 份——基线 190→198 文件、
   asserts 8012→8451、raises 593→622。
6. **P2 R3-3 自动化守卫**：新增 `backend/src/tests/unit/test_perf_scripts.py`
   （5 例全异步）——importlib 从 `<repo>/scripts` 加载两脚本并注册
   `sys.modules`，覆盖注册表/基线键集双向一致、`MAX_RATIO` 与基线 `max_ratio`
   同源、`_write_json_atomic` 成功与目标为目录的失败形态（`OSError` / 目标
   原样 / 无 `.tmp`）、`ValidationError` 折叠输出单行且含 loc/msg/type、
   `api_smoke` 端点表为四项且与 `docs/backend/performance.md` 端点表一致；
   不触网、不建库、不等待、不依赖 chdir。
7. **P2 门禁链文案**：根 `pixi.toml` 的 gates 用法头与
   `scripts/verify_slice.py` 的 gates 步骤 description 补「性能基线（bench
   self-check/gate + api-smoke self-check）」，链序与任务定义不变。

**验证**（主 Agent 实跑，命令与退出码）：

- `cd backend && pixi run tests-all` → **0**（**2962 passed**；基线 2957 +
  新增 5 例）。
- `cd backend && pixi run coverage-gates` → **0**（2962 passed；24 组 ≥80%）。
- `cd backend && pixi run lint` → **0**（`All checks passed!`）；`pixi run
  typecheck` → **0**（`Success: no issues found in 195 source files`）。
- `cd backend && pixi run conftest-env` → **0**（登记表 200 / 硬覆盖 200 /
  豁免 0）。
- 根 `pixi run gates` → **0**（注释门 files=226 / violations=0 / hints=33；
  同步测试棘轮 0 回升；noqa 冻结 scripts 7≤基线 7；沙盒漂移 38 项；红探针门
  194 源文件 / 7 探针；文档真相；断言棘轮 198 文件·8451 asserts·622 raises；
  导出面 102 引用·62 登记·40 豁免 + env 键面 701 行；bench-core
  --self-check/--gate PASS；api-smoke --self-check；prose-pin 报告完成）。
- `cd backend && pixi run gen-examples` 连跑两次 → **0 / 0**；三件套 sha256
  运行前 / 第一次 / 第二次完全一致（幂等，无 env 键新增）。
- `cd backend && pixi run bench-core --self-check` → **0**（6 项与基线键集
  一致）；`pixi run bench-core --gate` → **0**（6 项 median 均在基线 ×10 内）；
  `pixi run api-smoke --self-check` → **0**（4 端点与基线键集一致）；
  `pixi run api-smoke --gate` → **0**（4 端点均 200，p95 均在基线 ×10 内）。
- **hermetic 实证**：`REALTIME_VOICE_TTS_QUEUE_MAX=0 pixi run bench-core
  --gate` → **0**（全字段显式传参压过环境非法值）；`FLOW_BURST_COUNT=99`
  与 `REALTIME_VOICE_SAMPLE_RATE_HZ=1` 同命令 → **0 / 0**，输出中
  `Traceback` 计数 0。`FLOW_BURST_COUNT=99 pixi run api-smoke --gate` → **1**
  （单行「非本次冒烟相关环境变量取值非法……burst_count: Input should be less
  than or equal to 20（type=less_than_equal）」，无 traceback——既有折叠行为
  不变）。
- **断言棘轮反证实证**：从 `test_outbound_guard.py` 临时删除一条
  `assert "http/https" in exc_info.value.message` 后
  `pixi run python ../scripts/assertion_freeze.py --enforce` → **1**
  （`test_outbound_guard.py: assert 9 < 基线 10`）；还原后 → **0**
  （198 文件 / 8451 asserts / 622 raises）。
- `cd backend && pixi run pytest src/tests/unit/test_perf_scripts.py -q
  --asyncio-mode=auto` → **0**（**5 passed**）。
- `cd backend && pixi run verify-slice -- --slice r3-perf` → **0**；canonical
  证据包 `docs/evidence/r3-perf-16b07c7c.json`（5 步全 PASS：tests-all=2962 /
  coverage-gates=24 / typecheck=195）。

**剩余**：无（本切片范围）。

### 已完成切片：全约束审计 B 修复（P1×1 + P2×4，本分支工作区）

**已完成**（逐条闭环，分支 `feat/audit-a-fixes`）：

1. **P1 conftest 硬覆盖缺口（1.6）**：以 `scripts/generate_examples_env.py` 的 `_ENV_SECTIONS` 登记表为真源逐字段枚举——`LLM_INTIMATE_*` 6 键 / `COMPLIANCE_*` 4 键 / `INJECTION_*` 4 键全部在 `backend/src/conftest.py` 按既有分节风格硬赋值（敏感键 `LLM_INTIMATE_API_KEY` 为测试专用值，温度给数值型测试值 0.8——空串对可空 float 解析非法）。同一门禁按全量登记表核对，因此同批补齐同表缺口的 `EMOTION_*` 4 键与 `SERVER_ADMIN_TOKEN_ENV`（间接变量名，必须与 `ADMIN_AUTH_TOKEN` 一同钉住）；数值与生产默认同值的分节在注释中写明理由（检测矩阵 / 守卫用例显式注入自己的配置）。新增 `scripts/conftest_env_check.py`（AST 扫描 `os.environ[...] = ...`；缺口打印键名与来源字段并 exit 1；允许清单 `scripts/.conftest_allowlist.json` 每项须带 `reason`，当前空）、backend `conftest-env` 任务与根 `gates` 链（`prose-pin` 保持链尾）。测试 `test_conftest_env_hardening.py` 2 例：裸构造取 conftest 测试值；先用 `monkeypatch` 写入相反环境值、再重放 conftest 硬覆盖块（`runpy` 执行同一源文件），裸构造仍取测试值。既有 `test_llm_intimate_channel.py` 4 处构造改为显式空值 / `temperature=None`——env 硬覆盖后原先依赖「环境为空」的隐式半配置不再成立，断言与用例集合零变化。
2. **P2 死代码（2.2）**：全仓复核零引用后删除 5 个公共符号并同步 `__all__`（`render_config_yaml_text` / `current_settings_database` / `payload_for_kind` / `RealtimeInboundType` / `RealtimeOutboundType`），`docs/backend/config.md` 的模块导出列同步为仍存在的符号；三方案对照见 §5.1。删除后 lint / mypy / 覆盖率门禁全绿。
3. **P2 拍板三案可复核（2.1）**：新增 §4.1，为 P4 GUI 微信会话键（B）、亲密补强「道德层吞没」（丙）、记忆存储 Q3（A）各列三案与优缺点；非选定项文本明确标注「据决策要点重建」。
4. **P2 性能入 Plan 留痕（2.3）**：§5.2 记录 skills 扫描排序与 facts 复合索引 0018 的来源、是否入 R3 Plan（均为「否——随复审即时落地」）、影响面与观察点。
5. **P2 测试资源重复（1.7）**：新增 `backend/src/tests/router_fakes.py` 共享 fake（会话 / 回复记录、`create_error` / `reply_error` / 人格级失败、回显形态、回复门控、适配器失败注入与出站 ID 工厂、假时钟、空会话工厂）；`test_channel_router{,_flow,_group}.py` 改为引用共享模块，用例数 20 / 14 / 9 保持不变。唯一断言形态改写是门控用例的 `create_calls` 计数（共享 fake 的记录面统一为列表，`== 1` 等价改为 `len(...) == 1`），无断言弱化。
6. **同步面**：`docs/README.md` 门禁表、根 `README.md` 目录行、根 `pixi.toml` gates 注释与 `backend/pixi.toml` 任务说明同步新增门禁。

**验证**（主 Agent 实跑，命令与退出码）：

- `cd backend && pixi run tests-all` → **0**（**2871 passed**；基线 2869 + 新增 2 条硬覆盖用例；既有用例仅 `test_llm_intimate_channel.py` 4 处构造显式化）。
- `cd backend && pixi run coverage-gates` → **0**（2871 passed；24 组 ≥80%：harness/config 96.4% / harness/llm 99.0% / harness/voice 95.5% / harness/chat 96.3% / harness/channels 97.2% / harness/memory 94.5% / harness/persona 95.6% / harness/emotion 94.9% / harness/database 93.4% / app/api 83.3% / app/main.py 93.1%）。
- `cd backend && pixi run lint` → **0**（`All checks passed!`）；`pixi run typecheck` → **0**（`Success: no issues found in 192 source files`）。
- 根 `pixi run gates` → **0**（注释门 / 同步测试棘轮 / noqa 冻结 / conftest env 硬覆盖 / 沙盒漂移 38 项 / 红探针门 / 文档真相 / 断言棘轮无下降 / 导出面 102 引用·62 登记·40 豁免 + env 键面 686 行 / prose-pin 报告完成）。
- **conftest 门禁红 / 绿实证**：`cd backend && pixi run conftest-env` → **0**（登记表 env 键 195；conftest 硬覆盖 195；允许豁免 0）；把 conftest 复制到 `/tmp/conftest_red.py` 并删除 `os.environ["INJECTION_THRESHOLD"]` 一行后 `pixi run python ../scripts/conftest_env_check.py --conftest-path /tmp/conftest_red.py` → **1**（输出 `INJECTION_THRESHOLD（InjectionGuardSettings.threshold）`，硬覆盖 194 / 195）。
- **死代码复核**：`grep -rIn "<五符号>"`（全仓，排缓存与生成物）→ 无输出（exit 1）；`pixi run ruff check` 四文件 → `All checks passed!`；`pixi run mypy` 四文件 → `Success: no issues found in 4 source files`；相关测试 153 passed。
- `cd backend && pixi run gen-examples` → **0**；三件套 `.env.example` / `config.example.yaml` / `extensions_config.example.json` 运行前后 sha256 完全一致（幂等，无 env 键新增）。
- 前端未改动，按任务书略过（666 passed 基线不受影响）。

**剩余**：无（本切片范围）。

### 已完成切片：全约束审计 C 修复（P1×4 + P2×3，本分支工作区）

**已完成**（逐条闭环，分支 `feat/audit-a-fixes`）：

1. **P1 loader 误报 search 段（`config/loader.py`）**：未知顶层键判定改为模块常量白名单
   `_CONFIG_YAML_SECTIONS = {"llm", "search"}`（`search` 段由 `search_registry.load_search_config`
   独立加载——同文件双段共存，不属于未知键），告警文案改为「config.yaml 含未识别的顶层键
   （已忽略，仅解析 llm / search 段）」。测试：`test_config_loader.py` 新增「含合法 search 段 →
   加载 llm 且无该告警」（loguru handler 捕获 WARNING），既有第三键用例补文案断言；search 段
   本身仍由 `load_search_config` 正常消费（示例模板闭环用例覆盖）。
2. **P1 config.example.yaml 缺 search 节（`scripts/generate_examples_yaml.py`）**：新增
   `_search_section_lines()`——provider 示例经 `SearchProviderEntry(name, kind).model_dump()`
   物化（bocha 主 / zhipu 兜底；`api_key_env` 指向 `SEARCH_BOCHA_API_KEY` /
   `SEARCH_ZHIPU_API_KEY`；`base_url` 按「空=按 kind 缺省」省略以免与缺省表重复），
   `web_search` 子块取 `WebSearchSettings.model_fields` 的代码默认（`enabled=false`），字段注释
   由 `generate_yaml_field_comments` 从 `SearchConfig` / `SearchProviderEntry` /
   `WebSearchSettings` 内省生成；文件头补「search 搜索 provider 注册表」与生成源类清单。
   `backend/config.example.yaml` 重生成（88 → 132 行）。测试
   `test_config_examples_generation.py` 新增：search 段 kind 序 / 密钥变量名 / `enabled=false` /
   条目字段齐备，且可经 `load_search_config` 完整加载（模板 ↔ 搜索域加载器闭环）。
3. **P1 工具循环提示词内联（`chat/service.py` → `chat/prompts.py`）**：
   `TOOL_LOOP_FINAL_INSTRUCTION_TEMPLATE` 迁入 prompt Unit 文件（无占位符），新增
   `TOOL_LOOP_FINAL_INSTRUCTION_TEMPLATE_VERSION=1.0.0`，`PROMPTS_VERSION` 1.8.0 → 1.9.0；
   service 改为 import，删除「chat/prompts.py 不在本变更允许清单内」的悬空理由注释。文本
   sha256 `16076ed1…cbe94f`（与迁移前内联串字节一致）已入 `test_chat_prompts.py` 的
   `_BYTE_LOCKS` 与版本常量清单；`test_chat_tool_loop.py` 步数上限用例改为与模板整串相等
   断言（发送文本字节锁定，非子串）。
4. **P1 拍板 / 切片编号元数据清洗（生产源码 + docs）**：逐处读上下文改写，去掉 `R2*` /
   `D5=A` / `P6=B` / `P10=B` / `P1` / `chunk N` 编号而保留语义事实——`config/models.py`、
   `config/search_registry.py`、`search/*`、`usage/models.py`、`chat/{service,prompts,world_book}.py`、
   `persona/{chara_card,trait_library,directives_models}.py`、`reminders/models.py`、
   `app/api/{reminders,directives}.py`、`app/main.py`、`app/middleware/admin_auth.py`、
   `rhythm/*`、`intent/*`、`emotion/*`、`channels/*`。docs 侧 10 份（`backend/README`、`chat`、
   `persona`、`search`、`config`、`usage`、`memory`、`emotion`、`reminders`、`channels` 及
   `docs/README.md` 门禁行）同步；标题改写后锚点引用一并修正（`#长期指令usermd-式常备指令` /
   `#消息变体swipes` / `#输出协议心声--情绪`）。编号保留面：`docs/LEDGER.md`（决策档案本体）与
   `docs/README.md` 路线图 P0-P4（表格自身行标识，非跨文档引用坐标）。
5. **P2 wizard docstring（`config/wizard_generators.py`）**：步骤前缀枚举改为「覆盖
   `_ENV_SECTIONS` 全部前缀（当前 30 个）」，与既有 `_STEP_DEFINITIONS` × `.env.example` 键面
   闭环测试同轴，消除枚举漂移面。
6. **P2 日志格式可配（`config/models.py` LogConfig）**：新增 `text_format: str = ""`（空串 =
   core 默认统一模板），`to_core_config` 映射空串 → `None`；重生成 `.env.example`
   （`LOG_TEXT_FORMAT=` 出现，689 行）与 `docs/backend/config.md` env 分节表（`--sync-docs`）；
   `backend/src/conftest.py` 同步硬覆盖（conftest-env 门禁 196/196）。测试：空串 → None、
   非空透传两例；既有日志接线用例零变更全绿（默认行为零变化）。
7. **P2 测试 docstring 批次元数据**：16 个测试文件（含 `src/tests/conftest.py`、
   `src/tests/integration/test_core_wiring.py`）去掉 `P#` / `R2*` / `chunk N`，保留覆盖语义；
   门禁选择「仅生产源码检查」，测试清理一次性完成。
8. **新增门禁（`scripts/check_chinese_comments.py` 规则 4）**：注释 token + 各级 docstring 扫描
   `D\d+\s*[=＝]|P\d+\b|R2[a-z]?-\d+|chunk \d`（前置字符守卫排除 `MP3` / `UUID4` /
   `chunk_index` 类技术词；字符串字面量不在检查面），命中即违规 exit 1；本脚本 docstring 以编号
   作示例，登记为唯一检查面豁免。`docs/README.md` 门禁表与根 `scripts` 的形态说明同步。

**验证**（主 Agent 实跑，命令与退出码）：

- `cd backend && pixi run tests-all` → **0**（**2875 passed**；基线 2871 + 新增 4 例：loader 无告警
  1 / 示例 search 闭环 1 / text_format 2）。
- `cd backend && pixi run coverage-gates` → **0**（2875 passed；24 组 ≥ 80%）。
- `cd backend && pixi run lint` → **0**（`All checks passed!`）；`pixi run typecheck` → **0**
  （`Success: no issues found in 192 source files`）。
- 根 `pixi run gates` → **0**（注释门 221 文件 / 0 违规 / 33 hints / 92 skips；同步测试棘轮；
  noqa 冻结；conftest env 196/196 硬覆盖、豁免 0；沙盒漂移 38 项；红探针 191 源文件；文档真相；
  断言棘轮 8283 asserts / 602 raises 无下降；导出面 102 引用·62 登记·40 豁免 + env 键面 689 行；
  prose-pin 报告完成）。
- `cd backend && pixi run gen-examples` → **0**，三件套 sha256 运行前后完全一致（幂等）。
- `cd backend && pixi run export-dto-schemas` → **0**（仅 DTO description 文本随源码 docstring 清洗
  更新，结构零变化；`test_dto_schema_fixture.py` 2 例绿）。
- **门禁红 / 绿实证**：向 `search/__init__.py` 追加 `# R2a-9 临时探针（D5=A）chunk 2 / MP3 不算`
  → `cd backend && pixi run comment-gates` → **1**（输出命中 `R2a-9` / `D5=` / `chunk 2` 三条，
  `MP3` 不命中）；还原文件后 → **0**（violations=0）。

**剩余**：无（本切片范围）。

### 已完成切片：终局交叉复核（跨审计无回归）

- 复核方式：单只读会话在同一棵树（`89d5312`，后合入 `e95f5fe`）上核验审计 A/B/C 全部闭合项同时成立、无交互回归，并复跑全链。
- 实测：`tests-all` 2875 passed / `coverage-gates` 24 组 ≥80% / `lint` / `typecheck` 192 文件 / `conftest-env` 196/196（豁免 0）/ 根 gates 全链 exit 0（断言棘轮 8283 asserts·602 raises 无下降、红探针门、导出面、文档真相、prose-pin 尾标记齐）/ `gen-examples` 连跑两次工作树零差异 / `verify-slice --hash-only` = `c1fddaf8`（646 文件）与 canonical 证据一致。
- 交叉点核验：conftest `LOG_TEXT_FORMAT=""` 经空串→None 不改变默认日志行为；注释元数据门只扫 docstring 与 COMMENT token（字符串字面量不在面内，prompts 模板与示例注释零误伤，豁免仅检查器自身）；loader 白名单与示例 search 节闭环互洽；`router_fakes` 重构后三套用例 20/14/9 不变。
- R2 不变量抽查 8/8 在树：synthetic 恒跳过、密度锚点、64KiB 块上限与池 LIFO 关闭序、关系摘要注入、亲密通道路由、梦境上限与重入窗、0018 复合索引、voiceprint 同意闸。
- 结论：唯一 finding 为本文件上方「审计 C 尚未执行」过期句（P2，已按本切片修正并提交复审）；除此之外复核会话判定全部通过。

### 已完成切片：R4-1 前端体积治理（本分支工作区）

**已完成**：
1. 路由级 lazy split：`frontend/src/App.tsx` 全部路由页改 `React.lazy` 独立分包，
   共享壳（AppShell / ToastProvider / 令牌门）保持 eager；新增
   `frontend/src/components/PageBoundary.tsx`（Suspense 加载态 + chunk 失败态错误
   边界，侧栏导航在失败面保持可用）。
2. 体积门禁：`frontend/scripts/check_bundle_budget.mjs`（纯 Node 内置，零新依赖）
   解析 `dist/.vite/manifest.json`；失败条件为初始加载 JS >1,000,000 字节、动态
   chunk <3、dist/manifest 缺失或解析失败。阈值常量单源在本脚本，文档见
   [backend/performance.md](backend/performance.md)「前端构建产物体积基线（R4-1）」。
3. 任务与 CI：`frontend/pixi.toml` 增 `bundle-check`（读取现有 dist，不重复构建）；
   `.github/workflows/ci.yml` 的 frontend job 在 Build 之后执行 `pixi run bundle-check`。
4. 测试：新增 PageBoundary 3 例与 App 路由懒加载 6 例；App.test 既有同步断言
   均位于同一懒加载页已 `await` 的 `findBy*` 之后（同页后续查询依赖已加载页），
   无需改写；MSW 与页面级测试未改。
5. 文档：frontend/README 命令表与构建基线节、performance.md 新基线表、根 README
   性能基线行同步。

**实测（本机，命令 + 退出码）**：
- `cd frontend && pixi run test` → 0：86 files / 675 passed（基线 666 + 新增 9）。
- `cd frontend && pixi run coverage` → 0：perFile ≥80% 全绿（statements 96.02% /
  branches 92.22% / functions 95.58% / lines 96.23%）。
- `cd frontend && pixi run lint` → 0（tsc --noEmit + eslint）。
- `cd frontend && pixi run build` → 0：Vite 自报 `✓ built in` 2.41s（暖缓存复跑；
  同口径拆分前基线 5.96s），产物见下。
- `cd frontend && pixi run bundle-check` → 0：`[bundle-budget] PASS total=389799 lazy=25`。

**拆分前后（本机同轮实测）**：单 chunk 717,443 B（gzip 213,912 B）→ 初始加载
389,799 B（gzip 123,614 B）+ 25 个动态 chunk 337,058 B（gzip 109,646 B）；JS 产物
合计 726,857 B（共享模块拆分开销 +9,414 B）。gzip 均为 Node `zlib.gzipSync`
（默认参数）对产物文件的计算结果（与 `check_bundle_budget.mjs` 的 `measure()`
同工具），「前」列由 `git archive 01ac640 frontend` 临时树构建后同函数复算。
完整 chunk 表见
[backend/performance.md](backend/performance.md)「前端构建产物体积基线（R4-1）」。

**剩余 / 复审**：后端未改动（未复跑后端链）；chunk 明细与构建耗时为同机快照，
复审归主 Agent。

**复审修复（Oracle 深审 `e1e119b`：符合性 12/12 MATCH、0 violation，4×P2 全为
文档/注释口径；本条只记录闭环事项，复审结论归主 Agent）**：

1. **gzip 口径统一**：`docs/backend/performance.md` 拆分前后表、本节拆分前后段与
   `frontend/README.md` 决策历史统一为 Node `zlib.gzipSync`（默认参数）口径（与
   `check_bundle_budget.mjs` 的 `measure()` 同工具）。「R4-1 前」不再引用旧数字，
   由 `git archive 01ac640 frontend | tar -x -C /tmp/r4-before`、软链本仓
   `frontend/node_modules` 与 `.pixi`、在临时树 `pixi run build` 后同函数复算：
   单 chunk JS 717,443 B → gzip 213,912 B，CSS 23,257 B → gzip 5,276 B（原
   214,006 B 与 README 213.91 KB 的互斥数字消除；213,912 ÷1000 四舍五入两位＝
   213.91 KB）。CSS 前后真实差值写明为原始 +56 B、gzip +19 B（5,276 → 5,295）。
   构建耗时改单一口径：Vite 自报 `✓ built in` 暖缓存复跑，前 5.96s / 后 2.41s；
   冷启动与热跑不再并列。
2. **同步断言理由纠正**：App.test 既有同步断言的实际条件是位于同一懒加载页已
   `await` 的 `findBy*` 之后（同页后续查询依赖已加载页），非「既有断言均为
   `findBy*`/`waitFor`」；675（基线 666 + 新增 9）与新 9 例事实不变。
3. **台账数字同步**：§5 门禁命令块前端行改 `675 passed` 并补 `bundle-check` 行；
   §3 R4 行状态改「🔄 R4-1 本分支工作区（其余切片待开工）」，「当前分支状态」改
   「R4：已开工（R4-1 本分支），后续切片待排期」。
4. **脚本头单源口径**：`frontend/scripts/check_bundle_budget.mjs` 头注释改为
   「阈值常量唯一来源；文档可引用数值，不复写判定逻辑」，与 performance.md 阈值
   节措辞一致，删除「不得在别处复写数值」的绝对句。

修复后复跑（本机，命令 + 退出码）：

```
cd frontend && pixi run build && pixi run bundle-check   # 0；[bundle-budget] PASS total=389799 lazy=25
cd frontend && pixi run test                             # 0；86 files / 675 passed
根 pixi run gates                                        # 0；全链 exit 0（含文档真相/prose-pin 尾标记）
```

gzip 复算命令（对任一产物目录）：`node -e 'const {gzipSync}=require("node:zlib"),{readFileSync}=require("node:fs");console.log(gzipSync(readFileSync("<file>")).length)'`。

### 已完成切片：R4-2 预算分面（ASR / TTS / voiceprint 独立日预算，本分支工作区）

**已完成**：
1. **计量与值域**：`UsagePurpose` 增 `voiceprint`（`asr` / `tts` 既有）；
   三域均按**调用次数**计量（每次真实出站调用成功后 1 行，token 恒 0）：
   TTS 每句 / 每次合成、ASR 每次转写（含实时语音切段与中途转写）、声纹
   每样本向量提取 / 每次校验。persona 上下文存在时带 `persona_id`；**失败
   与被预算拒绝的调用不落行**（不消耗预算、不自我强化）。既有 LLM 记账
   语义不变（成功 / 失败均落账）。
2. **配置面**：`TTS_DAILY_BUDGET_GLOBAL/PERSONA`（`TtsSettings`，构造
   alias + `populate_by_name`）、`ASR_DAILY_BUDGET_GLOBAL/PERSONA`
   （`AsrSettings`）、`VOICEPRINT_DAILY_BUDGET_GLOBAL/PERSONA`
   （`VoiceprintSettings`）；均 0..10,000（计数页帽 50×200），默认 0 = 不限。
   `.env.example` / config.md env 分节表由生成器重出；Wizard 步骤前缀补
   `TTS_` / `ASR_`；conftest 六键硬覆盖。
3. **闸门与错误**：新增 `packages/harness/voice/voice_budget.py`
   （persona→global 判定、0/0 零查询、purpose 强制过滤、10,000 页帽、
   `RATE_LIMITED` details 含 scope/used/limit/purpose[/persona_id]）；TTS /
   ASR 在 `VoiceService` 出站客户端调用前接入；声纹在 `VoiceprintService`
   每次 `embed` 前接入（注册批量中途超限沿用既有回滚：本批文件 + 样本行
   清理，已成功调用各留一行）。实时语音会话中途超限：ASR 段 / TTS 句按
   既有可恢复 error 面收尾（`code=RATE_LIMITED`），会话继续。
4. **预算计数隔离**：`wire_default_voice_usage_counter()` 按 purpose 查询；
   `wire_default_usage_counter()`（LLM）改用 `LLM_BUDGET_PURPOSES` 收窄到
   chat/duty/dream/look_at/embed/tool——LLM 与语音 / 声纹行双向不占用
   （`UsageQueryDTO.purposes` 集合过滤轴；`purpose` 单轴行为不变）。
5. **接线**：voice API / realtime WS / voiceprints API / chat CLI 全部装配
   默认计数 + 成功记录器；无新增依赖、无迁移（`purpose` 列 VARCHAR(16)
   容得下 `voiceprint`，枚举为 DTO 值域校验，无 DDL）。
6. **文档**：usage.md 新增「分面日预算」节（单位 / 口径 / 表结构引用 /
   软竞态 / 错误示例 / 配置键表）；voice.md（链路 / 设置 / 声纹链路与错误
   码）、realtime-voice.md（中途超限可恢复语义与错误行）、config.md（键表
   / 多 worker 计数边界）、performance.md（预算查询成本一句）、llm.md
   （LLM 计数收窄与分面边界）；根 README 未涉及后端预算，未改。
7. **测试**：`test_voice_budget.py`（16 例：配置 env / 双档超限 / limit-1
   放行 / 0 不限零查询 / 失败不记 / 活 SQLite purpose 隔离 / 成功恰一行且
   被拒不新增 / 声纹注册与校验超限零出站 / 批量中途回滚 / 每次 embed 恰
   一行）；`test_realtime_session.py` 增 2 例（ASR / TTS 中途 `RATE_LIMITED`
   可恢复、会话继续）；`test_voice_service.py` 失败行断言改为「不落行」。
   活 SQLite 全异步、伪时钟 / 脚本计数，无真实等待。

**计量口径（每 purpose 一行说明）**：`tts` = 每次 TTS 合成调用成功；`asr`
= 每次 ASR 转写调用成功；`voiceprint` = 每次说话人向量提取调用成功。三域
均不记录失败 / 被拒调用；LLM 用途维持成功 / 失败双路径记账。

**实测（本机，命令 + 退出码）**：
- `cd backend && pixi run tests-all` → 0：2980 passed（基线 2962 + 新增 18）。
- `cd backend && pixi run coverage-gates` → 0：24 组全部 ≥80%（voice 95.7% /
  usage 99.5% / llm 99.0%）。
- `cd backend && pixi run lint` → 0；`pixi run typecheck` → 0（Success: no
  issues found）。
- `cd backend && pixi run gen-examples` → 0（三件套重出，幂等复跑无差异）。
- 根 `pixi run gates` → 0（注释门 / 同步测试棘轮 / noqa 冻结 / conftest-env
  206/206 / 沙盒漂移 / 红探针 8 条 / 文档真相 / 断言棘轮 / surface-check /
  bench 自检与门禁 / prose-pin）。
- `cd backend && pixi run export-dto-schemas` → 0（前端 contract fixture 同步
  `UsageFilterFields.purposes` 字段）；`test_dto_schema_fixture.py` → 0。
- 活 SeekDB：本切片无迁移，未跑（不涉）。

**偏离 / 记录**：语音 / 声纹记账由「成功 + 失败双行」改为「仅成功行」（按
R4-2 计量口径；既有 `test_voice_service` 一条用例同步断言）；用量记录器 /
计数接线从 `voice_service.py` 抽到 `voice_budget.py`（voice / voiceprint
共用，`voice_service` 保留同名导出兼容 app / CLI 导入）；LLM 预算计数从
全 purpose 收窄为 LLM 用途集合（分面隔离要求，见 llm.md）；`UsageQueryDTO`
增集合过滤字段（前端 types 同步 `purposes` 与 `voiceprint` 枚举/标签，contract fixture 已重出）。复审归主 Agent。

### 已完成切片：R4-3 实时语音增强（双轨流式 ASR/TTS + AEC + 移动端采集，本分支工作区）

**已完成**：
1. **流式 ASR 客户端**（`harness/voice/asr_stream_client.py`）：Qwen3-ASR 实时
   事件族协议（session.update / append / commit / finish / cancel；partial+final
   事件映射），传输注入（`stream_models.py` 帧模型 / 协议 / 关闭码语义），
   握手、收尾、`finish` 超时、`cancel`、心跳保活、喂帧有界队列（溢出丢最旧
   并计数）、错误统一映射 `LLM_CALL_FAILED`（details 含端点 / 模型 / 关闭码）。
2. **流式 TTS 客户端**（`tts_stream_client.py`）：CosyVoice 流式 PCM16 分片
   （tts.start / delta / completed / cancel），消费侧 `aclose` 即发 cancel 并
   关闭连接（barge-in 安全边界）；参数校验复用批量请求模型；输出分片用既有
   `pcm16_to_wav` 封装（wire 契约不变）。
3. **真实 WebSocket 传输**（`stream_transport.py`）：websockets 运行时依赖
   （uvicorn[standard] 既有，未新增工程依赖），打开超时 / 帧透传 / 对端关闭码
   观测；仅装配层与探测脚本消费（harness 客户端保持注入面）。
4. **会话双轨接线**（`realtime_session.py`）：开关开且客户端可用 → 语段起点
   后台握手（握手窗口音频有界缓冲回放）、partial 为真增量、切段收尾取最终稿
   进入生成；TTS 逐句流式合成后按既有 `audio_chunk` WAV 契约分片下行。握手
   失败 / 中途断流 / 收尾失败 / 空音频 → 本会话降级批量（一次告警 + 指标
   `stream_asr_fallbacks` / `stream_tts_fallbacks`），会话不中断。barge-in 同
   时取消流式 TTS 连接（生成器 aclose）与生成、取消流式 ASR 句柄；关闭路径
   双轨连接零残留（任务集合为空）。ready 增 `aec` / `streaming_asr` /
   `streaming_tts` 能力字段与指标计数。
5. **预算与计次口径（R4-2 语义不变）**：`VoiceService.open_asr_stream` /
   `synthesize_stream` 先过分面预算闸（被拒零建连零出站零落行）；ASR 流式连接
   成功开启计 1 次、收尾（finish / cancel）落 1 行；TTS 每句成功流式合成落 1
   行，中途取消不落行；开启失败不落行（回退批量后按批量口径独立记账）。
6. **配置面**：`VOICE_ASR_STREAMING_ENABLED/BASE_URL/MODEL/TIMEOUT_SECONDS`、
   `VOICE_TTS_STREAMING_ENABLED/BASE_URL/MODEL/TIMEOUT_SECONDS/SAMPLE_RATE_HZ`
   （全默认关闭；凭据沿用语部分节不重复）、`REALTIME_VOICE_AEC_ENABLED`（ready
   公告）；`.env.example` / config.md 分节表由生成器重出，conftest 十键硬覆盖；
   Wizard 经 `VOICE_ASR_` / `VOICE_TTS_` / `REALTIME_VOICE_` 既有前缀自动归组。
7. **部署探测脚本**（`scripts/probe_voice_streaming.py`）：按生效配置对 ASR /
   TTS 流式端点做最小握手与一帧往返，输出 PASS / SKIP（未启用 / 无凭据端点）/
   FAIL（含端点与原因）；不入门禁链，失败则该子项降级、不影响批量主链。
8. **前端 AEC 与移动端采集**：`getUserMedia` 固定全开 `echoCancellation` /
   `noiseSuppression` / `autoGainControl` 并回读实际生效值（未生效提示，不阻断）；
   采集优先 `AudioWorklet`（新增 `realtimeCaptureWorklet.ts` + Vite `?worker&url`
   模块加载路径，块切分核心 `takeFixedBlocks` 可单测），加载 / 建节点失败回退
   `ScriptProcessor`（16k PCM16 100ms 帧语义与心跳不变）；后台切换暂停采集、
   前台 / 点击恢复（WS 会话不重连）；面板展示能力字段与降级态。
9. **文档**：voice.md（双轨矩阵 / 协议面 / 探测脚本 / 降级与计次口径）、
   realtime-voice.md（ready 能力字段 / partial 语义 / barge-in 时序 / 失败回退 /
   AEC 与移动端边界）、config.md（键表说明）、frontend README（采集增强）、
   LEDGER 本节。
10. **测试**：`test_voice_stream_clients.py`（23 例：握手 / partial+final /
    断流 / 超时 / 取消 / 队列溢出 / 心跳 / 非法载荷 / 连接失败 / 收尾帧序 /
    升级被拒映射）、`test_voice_stream_transport.py`（7 例：回环真实 WS 帧透传 /
    关闭码 / 连接拒绝 / 关闭握手有界）、`test_voice_service_streaming.py`
    （12 例：双轨门控 / 预算拒绝零建连 / 计次口径 / 取消不落行 / 空音频不落行 /
    人格绑定）、`test_realtime_session_streaming.py`（16 例：ready 能力字段 /
    流式 partial→final→生成 / 三种失败回退 / 预算拒绝零降级 / 开关关 /
    流式 TTS 成功与回退 / barge-in 取消双轨 / 关闭零残留 / 握手缓冲上限与回放）、
    `test_probe_voice_streaming.py`（5 例：PASS / SKIP / FAIL / 超时受控）；
    前端 `lib/realtimeCapture.test.ts`（20 例）、
    `pages/realtime/RealtimeVoicePage.test.tsx`（14 例）、
    `pages/realtime/RealtimeVoicePage.capabilities.test.tsx`（6 例）。全异步、
    零真网（流式全 fake 传输；传输层用本机回环）、零真实音频设备。

**计量口径（流式连接计次——锁定）**：ASR 流式连接开启成功计 1 次，收尾（finish
成功 / cancel 干净关闭）落 1 行 `purpose="asr"`；开启失败不落行。TTS 每句流式
合成成功收尾落 1 行 `purpose="tts"`（与批量句级等价），中途取消不落行。被预算
拒绝时不建连、不出站、不落行；回退批量后按批量路径独立判定与记账（不重复计数）。

**实测（本机，命令 + 退出码；复审修复后终态）**：
- `cd backend && pixi run tests-all` → 0：3043 passed（首轮 3034 + 复审新增 9）。
- `cd backend && pixi run coverage-gates` → 0：24 组全部 ≥80%（voice 93.2%）。
- `cd backend && pixi run lint` → 0；`pixi run typecheck` → 0（Success: no issues
  found in 200 source files）。
- `cd backend && pixi run gen-examples` → 0（三件套重出，幂等复跑无差异）。
- 根 `pixi run gates` → 0（注释门 / 同步测试棘轮 / noqa 冻结 / conftest-env
  216/216 / 沙盒漂移 / 红探针 12 条覆盖 12 文件 / 文档真相 / 断言棘轮 /
  surface-check / bench 自检与门禁 / api-smoke 自检 / prose-pin）。
- `cd frontend && pixi run test` → 0：702 passed（首轮 699 + 复审新增 3）；
  `pixi run coverage` → 0（perFile ≥80%；Statements 96.08%）；`pixi run lint` → 0；
  `pixi run build` → 0（initial 389.80 KB，未触 1 MB 门）；
  `pixi run bundle-check` → 0（PASS total=389799 lazy=25）。
- `cd backend && pixi run python ../scripts/probe_voice_streaming.py` → 0
  （本机默认关闭：ASR SKIP / TTS SKIP）。
- `cd backend && pixi run verify-slice -- --with-frontend --slice
  r4-3-voice-streaming` → 0（canonical 证据包
  `docs/evidence/r4-3-voice-streaming-ce692b47.json`；历史 `b83d6556`（复审修复首轮树）、`e7a8cc1a`（复审前树）与 `942b5832`（复审中间树）均为 PASS 并留档；失败的中间证据 `cafb22d7`（frontend-test）、`740facd4`（frontend-coverage）与 `47b09cd0`（frontend-lint）已删除；本切片无迁移，未跑 SeekDB 真引擎
  套件）。

**偏离 / 记录**：
- 流式 TTS 分片在**服务端整句缓冲后**按既有 `audio_chunk` WAV 契约下行（wire /
  前端解码路径零变更，半句音频永不下发）；句内边收边播需改 wire 契约，留待后续。
- 流式 ASR 语段起点握手在后台任务执行（音频工人不阻塞）；握手窗口音频进 50 帧
  有界缓冲回放，超限丢最旧（前导音可能丢失，最终稿仍以完整落定音频为准）。
- 新增 4 个 harness 源文件已登记红探针（`stream_models` / `stream_transport` /
  `asr_stream_client` / `tts_stream_client`）。
- 前端测试基建 `src/test/setup.ts` 保持 Testing Library 默认等待上限（不再全局
  放宽）；仅 `App.test.tsx` 懒加载路由用例局部放宽用例超时到 5s（覆盖率插桩 +
  并行 worker 下首次 chunk 求值；断言强度不变）。
- `websockets` 未新加入 `pyproject.toml`（随 `uvicorn[standard]` 既有进入运行时，
  `pixi.lock` 已锁，用户拍板「用既有 websockets 依赖」）；`stream_transport.py`
  直接导入（缺包即 fail loud）。
- 未触碰 R4-2 预算语义（仅新增流式接线调用同一闸 / 同一记录器）；无迁移。
- 复审归主 Agent。

**复审修复（Oracle R4-3 P1×2 + P2×6 逐条闭环；canonical 证据 `ce692b47`）**：
1. **P1-1 ASR 收尾帧序**：`finish()` 改向发送队列投哨兵，发送工人按队列序先发
   `input_audio_buffer.commit` 再发 `session.finish`（不再与在途 append 竞争）；
   `_wait_finish_sent` 保证收尾事件出站后才等待最终稿。证据：
   `test_asr_finish_keeps_queued_appends_before_commit_and_finish`（fake send 强制
   让出；断言 append 下标 < commit < finish 且最终稿非空）；probe 场景（feed 后
   立即 finish）同路径不再空缓冲。
2. **P1-2 前端恢复/并发**：`MicrophoneCapture.start/resume` 共享在途 promise 守卫，
   `resume` 不再提前清暂停（仅 `start` 成功路径清）；恢复失败保持
   `isPaused()===true`，页面恢复提示与点击入口照常可见可点。证据：
   `realtimeCapture.test.ts` 两例（失败仍 paused；在途自动恢复 + 点击只 1 次
   `getUserMedia`，总链路不翻倍）。`src/test/setup.ts` 回默认等待上限，懒加载
   路由用例局部 `{ timeout: 5000 }`。
3. **P2-3 关闭有界**：`connect(close_timeout=min(open_timeout, 1.0))`（测试可注入
   更小值）。证据：`test_close_is_bounded_when_peer_ignores_close_handshake` /
   `test_close_timeout_is_injectable_for_tests`（原始 TCP 对端不回关闭帧，close
   在注入/推导预算内返回）。
4. **P2-4 升级被拒映射**：连接 / 发送路径把 `WebSocketException`（含
   `InvalidStatus` / `InvalidURI` / `ConnectionClosed`）统一归一 `OSError`，客户端
   既有「`OSError` → `LLM_CALL_FAILED`」映射生效。证据：
   `test_asr_upgrade_rejected_maps_to_llm_call_failed`（回环 HTTP 404 responder；
   details 含 endpoint 与连接失败语义）。
5. **P2-5 预算拒绝语义**：ASR `RATE_LIMITED` 不 `_degrade_stream_asr`、不计数、
   不告警（清握手缓冲后该语段走批量并由批量闸拒绝收尾）；TTS 流式仅当产出过
   非空分片才落用量行（空音频回退批量不一句两行）。证据：
   `test_stream_asr_budget_rejection_does_not_degrade`（两语段均重新尝试、fallbacks=0）、
   `test_tts_stream_empty_audio_records_no_usage_row`。ASR cancel 干净关闭落 1 行
   保持不变（与 voice.md 计次表一致）。
6. **P2-6 会话声明测试**：握手窗口 >50 帧只回放最后 50 帧且每帧恰一次；缓冲回放
   不重复喂 VAD / 流；在途流式 ASR 句柄与流式 TTS 连接同时存在时 barge-in 句柄
   `cancelled` 且 TTS 连接取消。证据：`test_realtime_session_streaming.py` 三例
   （全异步、事件门控）。
7. **P2-7 过期句**：LEDGER §6 / §3、`realtime_session.py` / `realtime_models.py`
   注释改为「批量为主、流式双轨可开（默认关闭）」，删「无流式通道」旧表述。
8. **P2-8 关闭码文档**：`voice.md` / `stream_models.py` 改写为「关闭码仅入 details；
   收尾事件（final/completed）前任何对端关闭按失败处理并回退；1008/1011 属服务端
   语义，客户端不主动发送」。
9. **验证**：`verify-slice -- --with-frontend --slice r4-3-voice-streaming` → 0，
   sha8=`ce692b47`（9 步全 PASS：tests-all=3043 / coverage-gates=24 /
   typecheck=200 / frontend-test=702 / frontend-coverage=96.08；
   命令与日志见 `docs/evidence/`）；根 `gates` / 前端 `bundle-check` /
   `gen-examples` 幂等均 0。

### 已完成切片：R4-4 SmsForwarder 短信 / 通知转发通道（仅入站，已合并 PR #43 → main `f8eb398`）

**已完成**：
1. **协议与安全**（`app/api/channels_sms_forwarder.py`）：公开路由
   `POST /api/v1/channels/sms-forwarder`，加入 `main.py` 豁免清单与
   README 保护面；鉴权接受 `Authorization: Bearer` /
   `X-SmsForwarder-Token` / `X-Sms-Forwarder-Token` / `?token=` 四形态，
   与 `SMS_FORWARDER_TOKEN`（SecretStr）恒时比较——缺失 401、值不符 403，
   且鉴权先于体解析 / 去重登记（被拒不消耗去重）。仅接受
   `application/json`；64 KiB 体上限；非 JSON / 非对象 / 深嵌套 / 空体均
   受控 400；字段级非法按隔离确认（200 不进 4xx）。
2. **归一与语义**（`channels/sms_forwarder/sms_models.py`）：kind 归一
   （短信默认接收；notification 由 `SMS_FORWARDER_NOTIFICATIONS_ENABLED`
   决定）；发送者按手机号 / 联系名归一（`+86` / `0086` / `86` 中国码前缀
   归一、分隔符剥离），会话键 `sms:<sender>`，发送者不作人格 ID；
   `synthetic=False`、`is_group_chat=False`；默认 `peer` 落档，仅
   `SMS_FORWARDER_OWNER_NUMBERS` 命中时 `is_owner=True`；空正文丢弃；
   时间戳秒级 ×1000 / 毫秒原样 / 非法或缺失回落服务端时钟。
3. **有界与幂等**（`channels/sms_forwarder/sms_runner.py`）：仿 QQ runner
   的 webhook 校验 + 去重登记 + 有界队列（容量 64 / worker 2，代码常量
   非 env）后立即 200；队列满丢弃不登记去重（手机端重投可恢复），ERROR
   含去重键 / chat_id / message_id / 深度 / 累计；不可路由事件不登记；
   去重按 msgid 常驻 FIFO（1024）或「发送者 + 正文」内容哈希 + 120s 时间
   窗；停机 `request_stop` → 取消 worker → 清空队列（零残留）；未装配
   runner 的嵌入形态保留 `process_sms_forwarder_payload` 直调回落。
4. **渠道配置与四表面**：`SmsForwarderChannelSettings`
   （`SMS_FORWARDER_ENABLED` 默认 false / `SMS_FORWARDER_TOKEN` /
   `SMS_FORWARDER_NOTIFICATIONS_ENABLED` 默认 false /
   `SMS_FORWARDER_OWNER_NUMBERS`）；`.env.example` 与 config.md 分节表由
   生成器重出；Wizard 新增 `SMS_FORWARDER_` 前缀步骤；conftest 四键硬覆盖
   （conftest-env 门 220/220）；`ChannelType` 增 `SMS_FORWARDER` 成员；
   `InboundMessageModel` additive 增 `is_owner`（既有私聊默认 True 保持零
   变化，群聊仍 peer）；`health` 与默认关闭形态零影响。
5. **出站边界**：锁定仅入站——`SmsForwarderAdapter.send_message` 恒空
   投递结果、不发起平台调用；人格回复仍生成并落会话 / 记忆，手机端由用户
   自行阅读或回复。`config.yaml` 不承载通道配置（env 面专属）。
6. **测试**（82 例，全异步、伪时钟、零真网）：`test_sms_forwarder_models.py`
   （载荷变体 / 别名 / kind 开关 / 空正文 / 时间戳矩阵 / 发送者与属主 /
   msgid 与内容去重窗 / 直调路径去重与异常隔离）、
   `test_sms_forwarder_runner.py`（过载丢弃与重投恢复 / 去重 / 通知门 /
   属主落档 / 停机零残留 / worker 隔离 / 周期观测）、
   `test_channels_sms_forwarder_api.py`（鉴权矩阵 / 四 token 形态 / 被拒
   不消耗去重 / Content-Type 与体校验 / 立即 ack / 未装配确认丢弃）、
   `test_sms_forwarder_settings.py`（默认 / env / SecretStr / 生产
   fail-closed）、`test_sms_forwarder_adapter.py`（仅入站空投递）、
   `test_app_sms_forwarder_lifespan.py`（装配 / 停机复位 / 出站丢弃）、
   `test_channel_router_owner_flag.py`（is_owner=False 私聊按 peer）。
7. **文档与门禁产物**：channels.md 新增 SmsForwarder 整节（部署 / token /
   完整 JSON 示例 / 字段表 / 链路 / 去重队列丢弃 / 仅入站 / 群聊不适用）；
   config.md 键表由生成器同步；docs/backend/README 保护面与 channels 模块
   状态；root README 特性与快速开始一句；红探针 4 条（3 个 harness 文件 +
   1 个 API）；surface 豁免清单新增 `SmsForwarderAckDTO`（41/41）。
8. **证据纪律**：最终树跑 `cd backend && pixi run verify-slice -- --slice
   r4-4-sms-forwarder` → 0，canonical 证据包
   `docs/evidence/r4-4-sms-forwarder-9010c7b8.json`（sha8=`9010c7b8`；
   `60a4ced6` 为复审前实现证据、`e994abcc` 为更早中间树——均按历史留档）。

**设计取舍 / 偏离**：
- 仓库没有可复用的通用有界 runner 基类（QQ / 企微各自持有同范式实现，
  队列元素与解析面不同）；本切片沿用同一范式实现，未抽象共享基类——
  避免为一个消费者改公共面。
- 未引入 `SMS_FORWARDER_PATH`：路由静态注册，路径配置会引入动态挂载复杂度，
  当前无实际需要（避免死配置）。
- token 按任务指定以 `SMS_FORWARDER_TOKEN` 直接配置字段承载（SecretStr），
  并扩展 `auth_policy.ensure_auth_policy`：`ENV=prod` 且通道启用时空 token
  拒绝启动；非 prod 保持「空 = 鉴权关闭」，与既有通道一致。
- 内容去重窗口 120s 的已知边界：无 msgid 时同发送者 + 同正文在窗口内会被
  视为重投；需要区分真实重复短信时，手机端模板应携带 `msgid`。
- 既有 `test_channels_models.py` 的 `ChannelType` 值域断言同步追加
  `sms_forwarder`（枚举扩展的必要同步，语义未变）。
- 无迁移、无新依赖；前端未动（未跑前端链）。
- 复审归主 Agent。

**实测（本机，命令 + 退出码；最终树）**：
- `cd backend && pixi run tests-all` → 0：3125 passed。
- `cd backend && pixi run coverage-gates` → 0：24 组 ≥80%
  （harness/channels 97.5%（2682/2751）、app/api 84.2%；同树复跑覆盖行数
  有 2 行浮动——2680/2751 = 97.4%，差异落在 `wecom/wecom_client.py:534-535`
  的运行顺序敏感路径，与本次改动无关）。
- `cd backend && pixi run lint` → 0；`pixi run typecheck` → 0
  （Success: no issues found in 204 source files）。
- `cd backend && pixi run conftest-env` → 0（220/220）。
- `cd backend && pixi run gen-examples` → 0；复跑三件套 sha256 不变（幂等）。
- 根 `pixi run gates` → 0（comment-gates / sync-test-ratchet / noqa-freeze /
  conftest-env 220/220 / sandbox-drift 38 项 / red-probe 203 源文件 16 探针 /
  docs-truth / assertion-freeze / surface-check 41/41 / bench 自检与门禁 /
  api-smoke 自检 / prose-pin）。
- `cd backend && pixi run verify-slice -- --slice r4-4-sms-forwarder` → 0
  （5 步全 PASS：tests-all=3125 / coverage-gates=24 / typecheck=204；
   canonical `9010c7b8`；`60a4ced6` 为复审前历史证据、`e994abcc` 为更早
   中间树）。
- 前端未动；无迁移，未跑 SeekDB 真引擎套件。

**复审修复（4 条 P2；未改产品语义，仅新增测试与文档）**：
1. 心流延迟批次 `is_owner` 用例：`test_channel_router_owner_flag.py` 增
   `test_delayed_batch_keeps_peer_provenance_for_non_owner`（FLOW 开启 +
   睡眠窗口排程，`is_owner=False` / `is_group_chat=False` 入站到期执行
   `scheduler.jobs[-1][1]()` 后仍按 `peer` 落档）；自证：临时把
   `channel_service.py` 批次 `is_owner` 硬置 `True` → 该例红
   （1 failed / 2 passed），复原后绿（3 passed）。
2. token 恒时比较用例：`test_channels_sms_forwarder_api.py` 增
   `test_token_comparison_uses_constant_time_primitive`（monkeypatch 录制
   `secrets.compare_digest` 实参并委托真实实现；正确 Bearer token 请求后
   断言恰一次 `(token, token)` 比较）；自证：临时把实现改成
   `presented != expected` → 该例红（1 failed / 22 passed），复原后绿
   （23 passed）。
3. 覆盖率数字按证据日志更正：`harness/channels 97.5%`（2682/2751；原
   `97.4%` 为誊写误差；同树复跑 2680/2751 的 2 行浮动见上方实测注记）、
   `app/api 84.2%`。
4. 归一规则文档：channels.md 发送者归一补「点」（实现剥离
   `[\s\-().]`）并注明通知发送者包名会话键形态（`com.example.app` →
   `sms:comexampleapp`）；LEDGER §6 记「仅点位置不同的包名共用会话」碰撞面
   遗留（可选收紧归一，需评审）；归一实现未改。
- 修复后全链：`cd backend && pixi run tests-all` → 0（3125 passed）；
  `pixi run coverage-gates` → 0（24 组 ≥80%）；`pixi run lint` → 0；
  `pixi run typecheck` → 0（204 源文件）；`pixi run conftest-env` → 0；
  `pixi run gen-examples` → 0（三件套 sha256 复跑不变）；根 `pixi run gates`
  → 0；`cd backend && pixi run verify-slice -- --slice r4-4-sms-forwarder`
  → 0（5 步全 PASS：tests-all=3125 / coverage-gates=24 / typecheck=204；
  canonical `9010c7b8`）。

### 已完成切片：R4-5 WhatsApp Cloud API 通道（webhook 入站 + 文本出站 + 24h 服务窗，本分支工作区）

**已完成**：
1. **Webhook 安全**（`app/api/channels_whatsapp.py`）：公开 GET
   `/api/v1/channels/whatsapp` 做 Meta hub 校验（`hub.mode=subscribe` +
   `hub.verify_token` 与 `WHATSAPP_VERIFY_TOKEN` 恒时比较 + challenge 非空
   → 200 纯文本回显；不匹配 403）；POST 同路径先校验
   `X-Hub-Signature-256 = sha256=<HMAC-SHA256(raw body, WHATSAPP_APP_SECRET)>`
   （缺失 / 不符 403，**先于** JSON 解析与去重登记），再校验
   `application/json` 与 64 KiB 上限（Content-Length 先检；无
   Content-Length 的 chunked 请求按 64 KiB 逐块累计中止——超限即停，
   不整包读入；反代建议再设 client_max_body_size）。
   空 verify token / app secret = 对应校验关闭（开发 / 测试形态）；
   `ENV=prod` + 通道启用时四项凭据任一为空拒绝启动
   （`auth_policy.ensure_auth_policy` 扩展）。
2. **协议归一**（`channels/whatsapp/whatsapp_models.py`）：模型化
   `entry[].changes[].value`（metadata / contacts / messages / statuses）；
   `text.body`；image / audio / document / sticker / video 取 caption 或
   中文占位标签；location / button / interactive 取文本回退；statuses[]
   只累计 `status_events_ignored`；会话键 `wa:<wa_id>`、sender=wa_id、
   `synthetic=False`、`is_group_chat=False`（群聊不适用）；默认 peer，仅
   `WHATSAPP_OWNER_NUMBERS` 命中时 `is_owner=True`；空正文 / 缺 wamid /
   缺发送者丢弃；时间戳秒级 ×1000、非法 / 缺失回落服务端时钟。
   号码归一抽取到 `channels/phone_numbers.py`：WhatsApp 使用
   `normalize_whatsapp_wa_id` / `parse_whatsapp_owner_numbers`（剥分隔符与
   前导 `+`、保留国家码，会话键 / 属主 / 出站 `to` 同源）；SmsForwarder
   继续使用中国码前缀归一的旧函数（行为不变）。
3. **有界幂等**（`channels/whatsapp/whatsapp_runner.py`）：校验 → wamid
   去重（进程内 FIFO 1024）→ 入有界队列（容量 64 / worker 2，代码常量）
   → 立即 200；满队丢弃不登记去重（端点恒 200，丢弃即最终丢失；不登记
   去重仅吸收 Meta 偶发重复推送），ERROR 含 wamid /
   chat_id / 深度 / 累计；statuses / 不可路由不登记；worker 异常隔离；
   停机 cancel + 清队零残留；未装配 runner 的嵌入形态保留
   `process_whatsapp_webhook_payload` 直调回落。
4. **文本出站与 24h 窗**（`channels/whatsapp/whatsapp_client.py` /
   `whatsapp_adapter.py`）：共享 httpx 注入（测试 MockTransport）调用
   `POST {API_BASE}/{API_VERSION}/{phone_number_id}/messages`（Bearer /
   type=text / to=wa_id），解析返回 message_id；401/403 →
   `CHANNEL_TOKEN_MISSING`，其余非 2xx / 超时 / 响应非法 →
   `CHANNEL_API_ERROR`；runner 登记每会话最近入站毫秒（与适配器共享同一
   `WhatsAppServiceWindow`），超 24h / 从未入站 → 受控 `CHANNEL_API_ERROR`
   且不发起 HTTP；模板消息未实现（文档写明）；附件忽略、引用回复不透传；
   适配器失败不反噬入站。
5. **配置四表面**：`WhatsAppChannelSettings`（`WHATSAPP_ENABLED` 默认
   false / `VERIFY_TOKEN` / `APP_SECRET` / `ACCESS_TOKEN` 三份 SecretStr /
   `PHONE_NUMBER_ID` / `API_VERSION` 默认 v21.0 / `API_BASE` 默认
   `https://graph.facebook.com` / `OWNER_NUMBERS`）；`.env.example` 与
   config.md 分节表由生成器重出；Wizard 新增 `WHATSAPP_` 前缀步骤；
   conftest 八键硬覆盖（conftest-env 228/228）；`ChannelType.WHATSAPP`
   与值域断言同步；默认关闭零影响。
6. **测试**（新增 143 例——含复审修复 20 例（第 1 轮 18 + 第 2 轮 2）+
   枚举值域断言同步 1 处，全异步、
   伪时钟、零真网）：
   `test_whatsapp_models.py`（载荷变体 / 媒体与结构化回退 / 状态计数 /
   缺字段 / 时间戳含非有限值 / 含国家码会话键与属主 / 多 entry / 服务窗 /
   去重 / 直调隔离）、
   `test_whatsapp_runner.py`（过载丢弃（不登记去重）/ 重复推送吸收 /
   状态计数 / 服务窗登记 / 属主 / 停机零残留 / worker 隔离 / 观测）、
   `test_channels_whatsapp_api.py`（GET 握手矩阵 + 恒时比较；签名
   合法/篡改/缺失/错密钥/空密钥；非 JSON/非法/非对象/超限（声明 /
   chunked 逐块，声明超限不读流）/Content-Length 缺失；被拒不消耗去重；
   立即 ack / 未装配确认丢弃）、
   `test_whatsapp_adapter.py`（MockTransport 成功 message_id / 出站 to 保留
   国家码 / 401 / 5xx / 超时 / 非法 JSON / 窗口内外与边界 / runner 入站更新 /
   grammar / 附件 / 缺令牌与号码 ID）、`test_whatsapp_settings.py`（默认 /
   env / SecretStr / 生产 fail-closed）、`test_phone_numbers.py`（SMS 与
   WhatsApp 两侧归一矩阵）、
   `test_app_whatsapp_lifespan.py`（装配 / 共享服务窗 / 停机复位 / 窗口外
   零请求）、`test_channels_models.py`（枚举值域同步）。
7. **文档与门禁产物**：channels.md 新增 WhatsApp 整节（Meta 配置步骤 /
   握手与签名 / 完整 JSON 示例 / 字段表 / 链路 / 去重队列丢弃 / 24h 窗
   与模板边界 / 群聊不适用 / 出站错误码 / Settings）；README /
   docs/backend/README / defense.md / config.md 同步；红探针 7 条
   （phone_numbers 两侧归一 / WhatsApp 四源文件 / API 路由）逐条实测 RED；
   surface 豁免新增 `WhatsAppAckDTO`（42/42）。

**设计取舍 / 偏离**：
- 24h 服务窗状态为进程内状态（单实例限制文档写明）；多 worker / 多实例
  部署下窗口判定可能偏严（未见过入站的实例拒绝出站）。
- 模板消息未实现：24h 窗外自由格式出站以受控业务错误拒绝；duty 等主动
  报文本通道投递需属主在窗口内先发消息。
- 出站仅文本：附件优雅忽略、超长文本不分段（平台 4096 上限侧拒绝）、
  引用回复未映射 `context` 字段。
- `WHATSAPP_API_TIMEOUT_SECONDS` 为客户端代码常量（任务配置最小集不含
  超时键）。
- 号码归一按通道分口径：WhatsApp `wa_id` 保留国家码（只剥分隔符与前导
  `+`，不剥 `86` / `0086`）——剥掉会让 `+86 138 0013 8000` 与 Cloud API
  的 `8613800138000` 互不相认并把出站投到错误号码；SmsForwarder 继续中国码
  前缀归一（行为不变），两侧不共享归一函数。
- 无迁移、无新依赖；前端未动。

**实测（本机，命令 + 退出码；第 1 轮复审后树）**：
- `cd backend && pixi run tests-all` → 0：3266 passed。
- `cd backend && pixi run coverage-gates` → 0：24 组 ≥80%
  （harness/channels 97.3%（3182/3270）、app/api 85.2%（1257/1476））。
- `cd backend && pixi run lint` → 0；`pixi run typecheck` → 0
  （Success: no issues found in 210 source files）。
- `cd backend && pixi run conftest-env` → 0（228/228）。
- `cd backend && pixi run gen-examples` → 0；连跑两次三件套 sha256 一致
  （`.env.example` `01b87d59…` / `config.example.yaml` `19bba95d…` /
  `extensions_config.example.json` `1a17d6e6…`，幂等）；
  `generate_examples_env.py --check-docs` → 0（gates 内 docs-truth）。
- 根 `pixi run gates` → 0（comment-gates / sync-test-ratchet / noqa-freeze /
  conftest-env 228/228 / sandbox-drift 38 项 / red-probe 209 源文件 23 探针
  覆盖 22 文件 / docs-truth / assertion-freeze asserts=9193 raises=671 /
  surface-check 42/42 / bench 自检与门禁 / api-smoke 自检 / prose-pin）。
- 红探针 `pixi run red-probe --all` → 0：23 条全部 RED（errors=0）——
  `phone-number-normalize-removal` failed=8、新增
  `whatsapp-wa-id-normalize-removal` failed=1、其余 21 条各 ≥1（`flow-gate-removal`、`sticker-bridge-decode-removal`、`auth-policy-removal` 为 failed=2）。
- `cd backend && pixi run verify-slice -- --slice r4-5-whatsapp` → 0
  （5 步全 PASS：tests-all=3266 / coverage-gates=24 / lint / typecheck=210 /
  gates；证据 `docs/evidence/r4-5-whatsapp-1a50b8fc.json`，
  sha8=`1a50b8fc`（现为历史）；`5547e269` 为更早历史证据）。
- 前端未动；无迁移，未跑 SeekDB 真引擎套件。

**复审修复（Oracle R4-5 深审 1×P1 + 5×P2 逐条闭环；历史证据
`1a50b8fc`）**：
1. **P1 出站 to 丢 +86 国家码**：`channels/phone_numbers.py` 新增
   `normalize_whatsapp_wa_id` / `parse_whatsapp_owner_numbers`（只剥分隔符
   与前导 `+`，保留全部数字——不剥 `86` / `0086`；SMS 旧归一函数与行为
   不变）；`whatsapp_models` 的发送者键 / 会话键 / 联系名映射、
   `whatsapp_adapter` 的 `to`、`main.py` 与 webhook 直调路径的属主解析
   全部改用新口径；channels.md 的 `to` / 会话键 / 归一说明同步为
   `wa:8613800138000`。证据：`test_whatsapp_wa_id_preserves_country_code_for_chat_key_and_owner`
   （入站 `8613800138000` → `wa:8613800138000`、属主清单
   `+86 138 0013 8000` 命中、非中国号码 `15551234567` 不变）、
   `test_send_preserves_country_code_in_wire_to_field`（MockTransport 断言
   `to=="8613800138000"`）、`test_normalize_whatsapp_wa_id_preserves_country_code`、
   `test_parse_whatsapp_owner_numbers_preserves_country_code`；红探针
   `whatsapp-wa-id-normalize-removal` → RED（failed=1, errors=0）。
2. **P2 恒 200 与重投语义**：统一为「端点恒 200（Meta 仅对非 2xx 重投），
   因此队列满丢弃即最终丢失；不登记去重仅为吸收 Meta 偶发重复推送」——
   改写 channels.md 丢弃 / 恒 200 两节、本段队列句、runner 模块 docstring /
   队列容量常量注释 / 队列满 ERROR 文案；用例更名
   `test_dropped_message_leaves_dedup_unregistered`（保留「不登记」断言，
   不再暗示恢复），相关 docstring 的「重投」改「重复推送 / 再次到达」。
3. **P2 chunked 体无界**：`channels_whatsapp.py` 新增
   `_read_request_body_bounded`——`request.stream()` 逐块累计，累计超限
   立即抛受控 400（不再整包 `request.body()` 读入），保留 Content-Length
   预检；channels.md / 本段写明「无 Content-Length 的 chunked 请求按
   64 KiB 逐块上限中止；反代建议再设 client_max_body_size」。证据：
   `test_bounded_body_reader_stops_reading_after_limit`（第二块触发超限后
   第三次 receive 未被调用）、
   `test_declared_oversize_rejected_without_reading_stream`（手工
   `starlette.requests.Request` 直接调用端点：受控 `BAD_REQUEST`，receive
   从未被调用）。
4. **P2 NaN / Infinity 时间戳 500**：`resolve_whatsapp_timestamp_ms` 先做
   `math.isfinite` 判定并捕获 `OverflowError` / `ValueError`，非有限值
   回落服务端时钟；时间戳矩阵补 `math.nan` / `math.inf` / `"inf"` 三参数。
   SMS 同病不在本批，§6 记一行遗留。
5. **P2 豁免接线实证**：`test_admin_auth.py::TestAssembledAppMatrix` 新增
   两条基于真实 `create_app()` 的用例——admin token 已配置且无 admin 头时
   WhatsApp GET 握手 200 回显 challenge；POST 无签名 403（而非 401，证明
   未被 admin 依赖掩蔽）。
6. **P2 lifespan 用例实证 + 链路顺序**：lifespan 用例经
   `WhatsAppCloudClient.http_client` 的 httpx request 事件钩子挂记录器，
   窗口外拒绝后断言 `requests == []`；channels.md 链路图把服务窗登记移到
   入队之后（与 `whatsapp_runner.py` 的 put_nowait → dedup 登记 → 服务窗
   记录顺序一致），runner 模块 docstring 同步。
7. **附（台账 / 探针 / 最终树复跑）**：§3 R4-4 改「已合并（PR #43 → main
   `f8eb398`）」、R4-5 标当前分支；red_manifest 增
   `normalize_whatsapp_wa_id` 探针（映射 +86 用例）。当轮树复跑：
   `pixi run tests-all` → 0（3266 passed）；`pixi run coverage-gates` → 0
   （24 组 ≥80%）；`pixi run lint` → 0；`pixi run typecheck` → 0（210 源
   文件）；`pixi run conftest-env` → 0（228/228）；`pixi run gen-examples`
   连跑两次 → 0（幂等）；根 `pixi run gates` → 0；`pixi run red-probe --all`
   → 0（23 条全 RED）；`pixi run verify-slice -- --slice r4-5-whatsapp`
   → 0（canonical `1a50b8fc`，现为历史；`5547e269` 更早）。

**复审修复（Oracle R4-5 第 2 轮复审 4×P2 + 台账项逐条闭环；canonical 证据
`d90786ec`）**：
1. **P2-1 属主解析回归钉**：`test_app_whatsapp_lifespan.py` 属主清单改
   `+86 138 0013 8000,妈妈`，断言 `_owner_numbers == {"8613800138000",
   "妈妈"}`；`test_channels_whatsapp_api.py` 直调路径补国家码属主用例
   （`from=8613800138000` → `wa:8613800138000` 且 `is_owner=True`，属主清单
   同上）。变异自证：`main.py` 与 `channels_whatsapp.py` 分别临时换回
   `parse_phone_owner_numbers` → 前者红于 owner 集合断言、后者红于
   `is_owner` 断言；复原后相关三文件 46 例全绿。
2. **P2-2 归一口径文档残留 + 国家码告知**：`config/models.py` 的 WhatsApp
   设置 docstring / Attributes / Field description 改为「独立归一、保留国家
   码，清单须含国家码（如 `+86 138 0013 8000`）」；`sms_models.py` 两处委托
   说明改为「短信口径（中国码前缀归一）；WhatsApp 使用保留国家码的独立
   归一」；channels.md 设置表 `owner_numbers` 行同步国家码示例；
   `pixi run gen-examples` 重出 `.env.example`（`WHATSAPP_OWNER_NUMBERS`
   注释同步）。全仓 grep 无「WhatsApp 与短信共享归一 / 逐字节一致」类现行句。
3. **P2-3 零请求钩子**：lifespan 用例的 httpx request 钩子改 `async def`
   （httpx 以 await 调用）；出站 `api_base` 指向 `http://127.0.0.1:9`
   （本机不可路由——判定被绕过也不打真网）。变异自证：临时跳过适配器
   24h 服务窗判定 → 用例红于 `recorder.requests == []`（钩子确有记录，传输
   失败仍映射 `CHANNEL_API_ERROR`）；复原后绿。
4. **P2-4 批内满队少计**：`whatsapp_runner.py` 满队分支改为对当前及批内
   剩余每条消息分别 `_dropped_inbound += 1` + ERROR（wamid / 深度 / 累计）
   后 `continue`，不再首条满队即返回；批内任一条丢弃时 `submit` 返回 False。
   新增用例：容量 1、单推送 3 条 → 首条入队、`dropped_inbound==2`、两条
   ERROR（`dropped=1` / `dropped=2`）；既有容量 2 / 单条满队断言语义未变、
   保持通过。
5. **备注项**：§3 main 指针改 `f8eb398`（PR#34–#43 已并入）；
   `whatsapp_models.py` 服务窗 docstring「乱序重投」改「乱序到达」；§6 增
   SmsForwarder runner 满队分支同形遗留一行（单条载荷下无少计，扩展批载荷
   前需同法逐条计数）。
6. **最终树复跑（命令 + 退出码）**：`cd backend && pixi run tests-all` → 0
   （3268 passed）；`pixi run coverage-gates` → 0（24 组 ≥80%；
   harness/channels 97.4%（3185/3271）、app/api 85.2%（1257/1476））；
   `pixi run lint` → 0；`pixi run typecheck` → 0（210 源文件）；`pixi run
   conftest-env` → 0（228/228）；`pixi run gen-examples` 连跑两次 → 0
   （三件套 sha256 一致，幂等；`.env.example` `02872633…`、
   `config.example.yaml` `19bba95d…`、`extensions_config.example.json`
   `1a17d6e6…`）；根 `pixi run gates` → 0（assertion-freeze asserts=9202
   raises=671 / red-probe 209 源文件 23 探针 / surface-check 42/42）；
   `pixi run verify-slice -- --slice r4-5-whatsapp` → 0（5 步全 PASS：
   tests-all=3268 / coverage-gates=24 / lint / typecheck=210 / gates；
   canonical 证据 `docs/evidence/r4-5-whatsapp-d90786ec.json`，
   sha8=`d90786ec`（G1 根修前 canonical，现为历史）；`1c33f1fe`（第 3 轮备注收尾前树）与 `1a50b8fc` 为历史）。

**复审备注收尾（第 3 轮复审 4 条备注 + 企微派发用例根修；canonical 证据
`d90786ec`——G1 根修前，现为历史，最新见下节 `bd3bdfd3`）**：
1. **企微派发用例根修**：`test_wecom_client.py` 的「回调无处理器丢弃」分支由
   单次 `asyncio.sleep(0)` 赌派发改为 loguru 事件门控（命中「企微回调无处理器」
   即放行，1s 超时即红）——分支执行从抖动变为确定；连跑 5 轮稳定，`harness/
   channels` 覆盖率稳定 97.4%（3185/3271）。原先 3183/3185 的覆盖率抖动即该
   竞态的产物，非 R4-5 代码问题。
2. **台账门禁头**：3268 passed / conftest 228/228 / mypy 210 / 前端 702 passed
   （build 2.52s、bundle 389799·25）。
3. **红探针计数表述**：其余 21 条各 ≥1，其中 `flow-gate-removal`、`sticker-
   bridge-decode-removal`、`auth-policy-removal` 为 failed=2。
4. **channels.md 测试面表**：补 runner「批内满队逐条计数与告警」与 API「直调
   路径国家码属主命中」两条。
5. 复跑：`pixi run tests-all` → 0（3268）；`coverage-gates` → 0（24 组）；
   `lint` / `typecheck`（210）/ `conftest-env`（228/228）/ 根 `gates` → 0；
   `verify-slice -- --slice r4-5-whatsapp` → 0（5 步；canonical
   `docs/evidence/r4-5-whatsapp-d90786ec.json`，sha8=`d90786ec`，700 文件；
   G1 根修后由 `bd3bdfd3` 取代，本行保留为历史）。

### 已完成切片：R4-6a GUI 微信容器化 PoC 门制品 + 本机栈实测（本分支工作区）

**已完成**：
1. **独立沙盒树 `docker/wechat-gui/`**（与后端及其余沙盒零代码共享）：
   独立 `pixi.toml`（Python 3.12 + 系统工具；任务名契约
   `tests-unit` / `lint` / `typecheck` / `coverage-gates`，另加
   `poc-stack`）、`pyproject.toml`（运行时仅 Pillow，dev extras 含
   pytest/ruff/mypy）、`Dockerfile`（debian:bookworm-slim tag+digest
   固定；清华 apt 镜像；Xorg dummy/evdev、x11vnc、novnc/websockify、
   imagemagick/xclip、python3-minimal/python3-pil、tini；不装微信客户端）、
   `entrypoint.sh`（root 建运行时目录 → uinputd → 降权 Xorg :99 →
   x11vnc 5900 → websockify/noVNC 6080 → `serve`/`poc` 两模式；
   HEALTHCHECK 探 X 与 noVNC）、`poc/run_stack_poc.sh`（防御式宿主运行器：
   build 网络重试 ≤3、trap 清理、特权仅 PoC、证据落
   `docs/evidence/poc/wechat-gui-stack-<host>-<UTC>.json`）。
2. **uinput 注入器** `uinput_devices.py`（纯 stdlib）：`UI_SET_EVBIT` /
   `UI_SET_KEYBIT` / `UI_SET_ABSBIT` / `UI_SET_PROPBIT` / `UI_DEV_CREATE` /
   `UI_DEV_DESTROY` ioctl 编码，`uinput_user_dev` 结构体（1116 字节）
   打包，`input_event`（24 字节）写入；虚拟键盘 + 绝对指针；sysfs 名称
   发现 + 容器内按 `dev` 号 `mknod`（无 udev）；`create()` /
   `emit_key()` / `emit_move()` / `emit_click()` / `destroy()`。
3. **控制面守护** `uinputd.py`：设备生命周期持有、Xorg 静态配置生成、
   设备信息落盘、事件节点 chown 给 wechat 用户、Unix socket JSON 行协议
   （ping/key/move/click/shutdown；非法载荷归一 `ok=false`）、客户端
   封装、SIGTERM/SIGINT 收口。
4. **PoC 门** `poc_gate.py`：七步（x_up / uinput_create / xinput_visible /
   event_roundtrip / screenshot / clipboard / vnc_up），每步
   `perf_counter` 毫秒耗时与结果；首失败即 FAIL 并跳过后续；单行 JSON
   报告（verdict/steps/hostname/kernel/docker/时间）到 stdout 与 `--out`；
   退出码 0/1/2；事件回环用 `xev` 流式读取 + 带截止注入（无固定长睡眠）；
   截图 Pillow 校验 PNG/尺寸/非全黑/非全同色。
5. **单测**（全异步、hermetic）：`test_uinput_devices.py`（ioctl 编号 /
   结构体布局 / 能力注册 / sysfs 发现 / mknod / 事件字节 / 销毁）、
   `test_uinputd.py`（协议矩阵 / 落盘 / chown / Unix socket 服务与客户端 /
   非法响应 / CLI）、`test_poc_gate.py`（解析器 / 截图校验 / 命令超时 /
   步骤编排与跳过 / 报告与退出码 / 七步经临时 PATH 假命令）、
   `test_xorg.py`（静态 evdev 段 / dummy 屏幕）。
6. **接线**：`scripts/check_coverage_per_module.py` 增 `wechat-gui` 剖面；
   `scripts/check_chinese_comments.py` / `noqa_freeze_check.py` /
   `count_sync_tests.py`（基线 0）/ `verify_slice.py` 的 `_HASH_ROOTS`
   （`docker/wechat-gui/src`）纳入本树；`docker/docker-compose.yml` 增
   `wechat-gui` 服务（profile 默认不启动；仅回环端口；`/dev/uinput` 设备；
   `cap_drop ALL` + MKNOD/CHOWN/SETUID/SETGID/KILL + `device_cgroup_rules`
   `c 13:* rwm`（uinput 事件节点主设备号 13；仅映射 /dev/uinput 会被
   device cgroup EPERM 拒绝）；`no-new-privileges`；VNC 密码文件 secret；
   **不 privileged**）；`.github/workflows/ci.yml` 增
   `wechat-gui (tests + lint + typecheck)` job（不构建镜像）。
7. **文档**：本树 README（职责 / 架构 / PoC / 生产非特权 / noVNC 登录与
   微信客户端安装 / 会话键 B 预告 / 安全边界 / 排查）；channels.md 增
   「GUI 微信实验轨（PoC 门阶段——尚未接后端）」整节。

**本机实测（命令 + 退出码）**：
- `cd docker/wechat-gui && pixi install` → 0。
- `pixi run tests-unit` → 0：93 passed；总覆盖率 93.31%。
- `pixi run coverage-gates` → 0：6 组 ≥80%（`__init__.py` 100% /
  `poc_gate.py` 88.9% / `uinput_devices.py` 96.1% / `uinputd.py` 94.9% /
  `wire_models.py` 100% / `xorg.py` 100%）。
- `pixi run lint` → 0（All checks passed）；`pixi run typecheck` → 0
  （Success: no issues found in 6 source files）。
- `WECHAT_GUI_POC_ALLOW_HOST_SESSION=1 pixi run poc-stack` → 0（特权形态
  PASS 7/7，221 ms）与
  `WECHAT_GUI_POC_ALLOW_HOST_SESSION=1 WECHAT_GUI_POC_RUN_MODE=unprivileged
  pixi run poc-stack` → 0（非特权 PASS 7/7，240 ms）：单轮注入（五类事件
  各 1、54 ms）；证据见本节末「复审修复」条（`docker` 字段为 null——门
  脚本在容器内，镜像不含 docker CLI）。
- `cd docker && docker compose -f docker-compose.wechat-gui.yml config` → 0
  （独立项目 `ai_agent_companion-wechat-gui` + 独立网络 `wechat-gui`、
  ports host_ip=127.0.0.1、`/dev/uinput` 设备映射、`c 13:* rwm` 规则、
  secret 文件、无 privileged；不需要 `SANDBOX_TOKEN` /
  `BROWSER_SIDECAR_TOKEN`）；默认栈
  `SANDBOX_TOKEN=x BROWSER_SIDECAR_TOKEN=x docker compose -f
  docker-compose.yml config` → 0（项目名 `ai_agent_companion`）。
- **生产形态非特权**：`WECHAT_GUI_POC_RUN_MODE=unprivileged` 即生产同款
  能力面（`--device /dev/uinput` + `c 13:* rwm` + `cap_drop ALL` +
  MKNOD/CHOWN/SETUID/SETGID/KILL + no-new-privileges，不 privileged），
  门 PASS 7/7（证据
  `docs/evidence/poc/wechat-gui-stack-LeoWorkLaptop-unprivileged-20261126T034711Z.json`）。

**实测中定位并根修的真实缺陷**（前两轮 FAIL，不伪装通过）：
1. **xinput 设备名不匹配**：Xorg 的 InputDevice `Identifier` 才是
   `xinput list` 显示名；首轮门失败后把配置 Identifier 对齐内核设备名
   （`wechat-gui virtual keyboard` / `pointer`），`xinput_visible` 才通过。
2. **xclip 写路径挂起**：`xclip -in` fork 出常驻 selection owner 并继承
   stdout/stderr 管道，`communicate` 等待到 30s 超时；写路径改为
   stdout/stderr 接 `/dev/null`（不采集输出），读回路径保持采集。
3. **设备节点缺失**：容器无 udev，uinput 设备只现于 sysfs；补
   `_ensure_event_node` 按 sysfs `dev`（major:minor）`mknod`。
4. **非 root Xorg 本地套接字目录**：`/tmp/.X11-unix` 由入口 root 预建
   （`chmod 1777`），消除 `_XSERVTransmkdir` 告警。

**设计取舍 / 偏离**：
- **设备创建归属**：uinput 设备由 `uinputd` 在 Xorg 之前创建并长期持有
  （Xorg 静态 InputDevice 在 PreInit 打开节点，晚创建不会被绑定）；门脚本
  `uinput_create` 采用「校验看守进程创建的设备 + 节点可打开 + 控制面
  ping」，而非在门内再建一对（再建会得到 4 个设备，与「两虚拟设备」判据
  冲突，且门内 fd 生命周期无法覆盖 Xorg 启动）。注入仍经同一控制面完成。
- **绝对指针**：evdev 将仅 ABS 的设备归类为 touchscreen 类型 slave
  pointer；坐标点击 / 拖拽语义等价，`xinput` 中非漂浮。需要鼠标相对
  语义时可补 REL 轴，门断言不变。
- **特权只在 PoC**：宿主运行器用 `--privileged` 仅为同时打通 uinput 与
  事件节点命名空间；生产 compose 用 `--device /dev/uinput` +
  `device_cgroup_rules: c 13:* rwm` + MKNOD/CHOWN/SETUID/SETGID/KILL +
  no-new-privileges，已写进 compose 与 README 双重约束。非特权实测中
  仅映射 /dev/uinput 会因 device cgroup 对 13:* 的默认拒绝出现 EPERM，
  该规则是必需项（已在 compose / README 固化）。
- **VNC 密码缺省文件**：compose 允许 `WECHAT_GUI_VNC_PASSWORD_FILE`
  指向真实密码文件；未设置时用仓库内 `vnc.pass.default`（`changeme`）
  保证既有 compose 命令不因新增 secret 报错，生产必须覆盖。
- **散列根**：`_HASH_ROOTS` 只纳 `docker/wechat-gui/src`（与
  tool-sandbox / browser-sidecar 的「src + server.py」口径一致；本树无
  server.py）；Dockerfile / pixi 等部署面与其余沙盒树一样不入哈希。
- **无 wire 契约**：本阶段不接后端，故不纳入 sandbox-drift 门；过门后
  的通道实现再评估契约镜像。
- 无后端 / 前端 / 迁移改动；未跑 SeekDB 真引擎套件（不涉数据库）。

**最终树复跑（命令 + 退出码）**：`cd backend && pixi run verify-slice -- --with-seekdb --with-frontend --slice r4-6a` → 0（10 步全 PASS：tests-all=3472 / coverage-gates=24 / lint / typecheck=216 / gates / tests-seekdb=15 / frontend-test=716 / frontend-coverage=96.15 / frontend-lint / frontend-build=3.04）；canonical 证据 `docs/evidence/r4-6a-5446cb25.json`（sha8=`5446cb25`，756 文件级证据；`--hash-only` 复算一致；`874e54e1` 为首轮树，`5446cb25` 为二轮复审修复前的树，二者现均为历史；二轮复审后的 canonical `9971f110` 亦由 `98cfdadd` 取代，见「三轮复审修复」小节）。后端零逻辑改动；SeekDB 真引擎套件经本机 seekdb-smoke 容器通过；新树的注释门 / noqa 冻结 / 同步测试棘轮经根 gates 链通过（无 wire 契约，sandbox-drift 不涉）。

**复审修复（Oracle R4-6a 深审 3×P1 + 6×P2 逐条根修；canonical 证据
`docs/evidence/r4-6a-5446cb25.json`）**：

1. **P1-1 隔离性声明不实 + 宿主注入面**：(a) 沙盒树 README「安全边界」与
   channels.md「边界与风险」改为事实陈述——uinput 设备是内核全局对象，宿主
   图形会话（或同机其它 X 会话）会接入 `wechat-gui virtual*` 并接收注入；
   复跑前须在宿主 float；生产形态必须配部署机宿主 udev 规则（对
   `wechat-gui virtual*` 置 `LIBINPUT_IGNORE_DEVICE="1"` 或清空
   `ID_INPUT*`）并实测确认；该缓解已在开发机验证（见下一小节「二轮复审
   修复」），部署机待复验；无法满足时按 VM 隔离评估。(b) `poc/run_stack_poc.sh` 增宿主活动图形会话
   探测（`DISPLAY` 可连 / `/tmp/.X11-unix/X*` 且 `xdpyinfo` 可连）：默认
   拒绝运行并打印说明，仅 `WECHAT_GUI_POC_ALLOW_HOST_SESSION=1` 放行；放行
   后在设备出现时用镜像内 `xinput` 于宿主逐一 `float`（只处理确实出现的
   设备），证据 JSON 增 `host_session_guard`
   （`host_session_active` / `host_display` / `allow_host_session` /
   `floated_devices` / `residual_devices`）。(c) `poc_gate._collect_xev` 改
   单轮注入：`stdbuf -oL xev -root` + 客户端就绪探测（`/proc/<pid>/fd`
   出现 X socket），一次性注入 a 键按下/抬起 + 移动 + 左键按下/抬起，按
   截止轮询解析；门断言面扩为 KeyPress / KeyRelease / ButtonPress /
   ButtonRelease / Motion 各 ≥1。实测：宿主 `:0`（KDE）两次运行都在设备
   出现后浮动两台、`residual_devices=[]`、无残留容器；未设置放行变量时
   `bash poc/run_stack_poc.sh` → 1（拒绝运行）。
2. **P1-2 生产形态密码注入不可用**：(a) entrypoint 去 `readonly PASSFILE`；
   可读性按运行用户判定（`setpriv --reuid/--regid --keep-groups test -r`，
   消除 root 的 `test -r` 恒真口径），root 可读而 `wechat` 不可读时
   `install -o wechat -g wechat -m 0400` 到 `/run/wechat-gui/vnc.pass`，复制
   失败即整体失败；(b) 运行器临时密码文件改 0644（目录 0700），`openssl`
   列为必需命令（缺失即失败、删除 `RANDOM` 弱回退），证据改「容器内路径
   写出 + `docker cp` 取回」（不再把仓库目录 bind mount 进容器：非特权形态
   `cap_drop ALL` 时容器内 root 无 DAC_OVERRIDE，绑定挂载写仓库目录报
   `PermissionError`；取回文件天然属调用者，无需事后 chown）；(c)
   README / compose 写明密码文件须对运行用户可读（`wechat:wechat 0400`，
   或 root 持有 0400 由入口复制）；`WECHAT_GUI_VNC_PASSWORD` 回退路径修复
   （原 `readonly` 变量在 `set -u` 下二次赋值即退出，回退分支不可达）并补
   `test_entrypoint_password.py`（运行用户可读直接使用 / 明文回退 0400 /
   明文优先 / 两者不可读即失败）与 README 冒烟命令。
3. **P1-3 硬约束 9**：门报告 ISO 时间戳改 `started_at_ms`（Unix Epoch
   毫秒整数），删除 `started_at_utc`；单测、README 字段表与入库证据同步。
4. **P2-1 硬约束 5/18**：新增 `src/wechat_gui_poc/wire_models.py`（数据模型与
   wire 契约 Unit 文件）；控制面请求改五个具体模型（`PingRequest` /
   `KeyRequest` / `MoveRequest` / `ClickRequest` / `ShutdownRequest`）、
   `CommandOutcome.details` 改 `PingDetails` 模型、`dispatch_command` 按模型
   分发；仅 `to_wire()` 保留 dict。
5. **P2-2 可证伪性**：新增用例钉住「剪贴板回读不一致 → FAIL」「Motion /
   ButtonRelease 缺失 → FAIL」「控制面注入失败 → FAIL」「noVNC 非 200 →
   FAIL」「`stdbuf -oL` 调用」；变异矩阵见下。
6. **P2-3**：`collect_environment` 的 `subprocess.run` 改同文件
   `run_command`（async）；`uinputd` 的 `print` 改 logging（模块 logger +
   `main` 的 basicConfig）；`InputEmitter.emit_key/emit_move` 补 Args/Raises。
7. **P2-4 死代码**：删 `UI_SET_RELBIT` / `KEY_1` / `KEY_Q`；`DeviceKind` 与
   `VirtualDeviceInfo.kind` 删除（无判定消费面，保留只会与内核名重复）；
   `XevCounts.button_release` 接入断言（不再只解析不使用）。
8. **P2-5**：镜像构建实际尝试次数写入证据 `build_attempts`；uid=1000 隐藏
   依赖消除（临时密码文件 0644 + 入口按运行用户判定 + 证据经 `docker cp`
   取回）。
9. **P2-6 硬约束 7**：README 增控制面 JSON 行示例（ping/key/move/click/
   shutdown）、`uinput-devices.json` 字段表与门报告字段表（含宿主运行器
   追加字段）；channels.md 同步协议示例与字段指引。

**命令 + 退出码（最终树）**：
- `cd docker/wechat-gui && pixi run tests-unit` → 0（93 passed；总覆盖率
  93.31%）；`pixi run coverage-gates` → 0（6 组 ≥80：`__init__` 100 /
  `poc_gate` 88.9 / `uinput_devices` 96.1 / `uinputd` 94.9 /
  `wire_models` 100 / `xorg` 100）；`pixi run lint` → 0；
  `pixi run typecheck` → 0（6 源文件）。
- `WECHAT_GUI_POC_ALLOW_HOST_SESSION=1 pixi run poc-stack` → 0（privileged
  PASS 7/7；221 ms；证据
  `docs/evidence/poc/wechat-gui-stack-LeoWorkLaptop-privileged-20261126T034706Z.json`，
  `build_attempts=1`、`container_exit_code=0`、
  `host_session_guard.floated_devices` 两台、`residual_devices=[]`）。
- `WECHAT_GUI_POC_ALLOW_HOST_SESSION=1 WECHAT_GUI_POC_RUN_MODE=unprivileged
  pixi run poc-stack` → 0（PASS 7/7；240 ms；证据
  `docs/evidence/poc/wechat-gui-stack-LeoWorkLaptop-unprivileged-20261126T034711Z.json`）。
  各步 perf_counter ms：x_up 3 / uinput_create 83 / xinput_visible 3 /
  event_roundtrip 54（单轮注入，五类事件各 1）/ screenshot 77 /
  clipboard 8 / vnc_up 8。
- `bash docker/wechat-gui/poc/run_stack_poc.sh`（不设放行变量）→ 1（宿主
  `:0` 被探测到，拒绝运行并打印注入面说明）。
- 旧证据 `wechat-gui-stack-LeoWorkLaptop-20261126T025721Z.json`（ISO 时间戳 /
  20 轮注入口径）已由上述两份取代并删除；运行后宿主 `/sys/class/input`
  无 `wechat-gui virtual*`、`docker ps -a` 无 `wechat-gui-poc` 容器。
- `cd backend && pixi run verify-slice -- --with-seekdb --with-frontend
  --slice r4-6a` → 0（10 步全 PASS：tests-all=3472 / coverage-gates=24 /
  lint / typecheck=216 / gates / tests-seekdb=15 / frontend-test=716 /
  frontend-coverage=96.15 / frontend-lint / frontend-build=3.04）；canonical
  证据 `docs/evidence/r4-6a-5446cb25.json`（sha8=`5446cb25`，756 文件；
  `--hash-only` 复算一致）。该步 gates 日志
  `docs/evidence/logs/r4-6a-5446cb25-gates.log`：comment-gates files=386
  violations=0 hints=23 / sync-test-ratchet 无回升 / noqa-freeze 基线 0 /
  sandbox-drift / red-probe-gate / docs-truth / doc-counts /
  assertion-freeze asserts=9570 raises=708 / surface-check / bench-core PASS /
  api-smoke PASS / prose-pin 75≤75。

**变异矩阵（逐条变异 → 对应用例转红 → 复原全绿；M1 即上一轮存活的
剪贴板分支）**：

| # | 变异 | 预期红用例 | 实测 |
|---|---|---|---|
| M1 | `step_clipboard` 回读比较改恒真 | `test_step_clipboard_mismatch_fails` | FAILED（1 failed） |
| M2 | `_roundtrip_complete` 去 `motion >= 1` | `test_step_event_roundtrip_rejects_incomplete_events[Motion=0]` | FAILED |
| M3 | 去 `button_release >= 1` | `...[ButtonRelease=0]` | FAILED |
| M4 | 注入失败分支短路（`if False`） | `test_step_event_roundtrip_injection_failure` | FAILED |
| M5 | `step_vnc_up` 去 200 判定 | `test_step_vnc_up_rejects_non_200` | FAILED |
| M6 | `entrypoint.run_user_can_read` 恒真（root 口径） | `test_resolve_fails_when_nobody_can_read` | FAILED |
| M7 | 去 `stdbuf -oL` | `test_step_event_roundtrip_uses_line_buffered_single_round` | FAILED |

**缓解登记（udev 规则）**：仓库参考件
`docker/wechat-gui/udev/90-wechat-gui-ignore.rules` 已在本机验证——X11 +
Xorg libinput 宿主上 `event24` / `event25` 均 `LIBINPUT_IGNORE_DEVICE=1`、
无 `ID_INPUT*`、宿主 `xinput` 无该设备；部署机待复验。Wayland 会话下
`xinput float` 不可行，只能依赖该规则或 VM 隔离。PoC 期间以「有宿主图形
会话即默认拒绝 + 显式放行 + 设备安全态同步 + 证据留痕 + 运行后残留校验」
兜底；二轮复审已把 float 从「事后尽力」改为容器启动前同步门，详见下一小节。

### 已完成切片：R4-6a 二轮复审修复 + compose 隔离 + udev 入库（本分支工作区）

**背景**：Oracle 对 R4-6a 的复审不通过（2×P1 + 4×P2），另有用户要求
（compose 项目隔离、udev 参考件入库）。逐条根修，不写「复审通过」——
结论由命令与证据支撑。

1. **P1-2 密码路径在生产能力集下失败**（`entrypoint.sh`）：
   `resolve_vnc_passfile` 的明文路径改为 `rm -f` → 写入 → `chmod 0400`
   → `chown`；复制路径改为 `rm -f` → `install -m 0400` → `chown`。
   根因是 chown 之后无 `CAP_FOWNER` 的 root 不能再 chmod。生产同款能力集
   （`--cap-drop ALL` + MKNOD/CHOWN/SETUID/SETGID/KILL +
   `no-new-privileges`）实测三条路径均 exit 0：
   `copy=wechat:wechat 400`、`env=wechat:wechat 400`、同容器再次解析
   `rerun=wechat:wechat 400`。可读性探针抽成可注入函数参数
   （`probe_readable_as_user` / `run_user_can_read ... <probe>`），新增
   `test_resolve_probe_distinguishes_run_user_from_caller`：调用方可读而
   「运行用户不可读」时走复制；把判定退化成 `[[ -r ]]` 该用例转红。
   README 冒烟命令加生产 flag，能力清单与 compose 五项对齐。
2. **P1-1 float 不是注入前置条件**（`poc/run_stack_poc.sh` +
   `entrypoint.sh`）：运行器以 `WECHAT_GUI_HOST_FLOAT_WAIT=1` 启动容器，
   入口在启动 Xorg 前有界等待 `/run/wechat-gui/host-float.done`（超时
   exit 1）。运行器判定两台设备「安全态」：`xinput` 可见的必须浮动成功，
   sysfs 存在而 `xinput` 不可见的视为被宿主 udev 规则忽略（连续两轮确认）；
   安全后 `docker exec` 落 done，截止不满足则 `docker rm -f` + exit 1。
   ALLOW 分支在启动容器前先以镜像内 `xinput list` 验证可连宿主
   （`Authorization required` 即拒绝）。`residual_devices` 非空现在 exit 1。
   证据 `host_session_guard` 增 `float_wait` / `float_sync_ms` /
   `ignored_devices`。实测：特权与生产同款非特权均为 PASS 7/7（193 /
   174 ms），`float_wait=true`、`float_sync_ms` 1198 / 1180 ms、
   `ignored_devices` 两台（宿主已装 udev 规则，`xinput` 不可见）、
   `residual_devices=[]`；udev 规则安装前的历史证据
   `...-privileged-20261126T034706Z.json` / `...-unprivileged-20261126T034711Z.json`
   仍记录真实 `float` 两台。失败路径：`XAUTHORITY=` 复跑 → 退出码 1
   （启动容器前拒绝，无 `wechat-gui-poc-*` 残留、无注入）；浮动不满足的
   移除路径由 `test_float_until_settled_fails_when_neither_safe_nor_absent`
   守卫。
3. **守卫 fail-open**（`run_stack_poc.sh`）：`detect_host_session` 改为
   任何 `X11_SOCKET_DIR/X*` 或 `WAYLAND_SOCKET_DIR/wayland-*` 存在即活动
   （不再要求 `xdpyinfo` 可连）；Wayland 会话直接拒绝并说明需 udev 或 VM
   隔离。socket 根可经 `WECHAT_GUI_POC_X11_SOCKET_DIR` /
   `WECHAT_GUI_POC_WAYLAND_SOCKET_DIR` 参数化；新增
   `test_run_stack_poc.py`（临时 socket 根）三态自证。
4. **生产形态重启不可用**（`entrypoint.sh`）：`chown -R` 前删除运行用户
   写出的 `xorg.log` / `x11vnc.log` / `novnc.log`，并在启动 Xorg 前删除
   `/tmp/.X<n>-lock` 与残留套接字。实测同一容器连续 `docker restart` 两次
   均约 9s 内 healthy。
5. **P2-4 死代码**：删 `uinputd._require_bool` 与
   `uinput_devices._USER_DEV_SIZE`（仅定义无消费）。
6. **P2-2 守卫缺口**：`poc_gate._xev_connected` 的 proc 根参数化并新增
   「有 socket fd / 仅普通 fd / 进程已退出」三态用例；`uinputd` 新增
   `pressed=False` 释放用例（硬编码 `pressed=True` 即红）；第 1 项的可读
   探针用例。
7. **P2-6**：`channels.md` 的 `uinput-devices.json` 与门报告字段改为字段表
   （并链接 README 字段表）。
8. **用户要求 A — compose 隔离**：默认 `docker/docker-compose.yml` 顶层
   `name: ai_agent_companion`；wechat-gui 服务 / secret 整体迁出到
   `docker/docker-compose.wechat-gui.yml`（`name:
   ai_agent_companion-wechat-gui`、独立网络 `wechat-gui`、仍仅回环端口、
   能力集逐项保留、不加入 `tool-sandbox`、不需要砂盒 token）。旧 Exited
   容器 `wechat-gui`（compose 项目 `docker`）已 `docker rm -f` 并记录。
   实测：新文件 `config` → 0；`up -d` 起容器（项目名 / 网络隔离）、约 6s
   healthy、宿主 `xinput` 无该设备、`down` → 0 且容器 / 网络删除、无残留
   设备；默认栈 `config`（内联占位 token）→ 0。README 增迁移说明。
9. **用户要求 B — udev 参考件入库**：新增
   `docker/wechat-gui/udev/90-wechat-gui-ignore.rules`（与宿主已验证文件
   逐字一致，`diff` 无差异）；README 增部署三步（复制 → `udevadm reload`
   → 首次启动前就位）与验证三步。本机实证：serve 运行期间
   `event24` / `event25` 的 `udevadm info` 均 `LIBINPUT_IGNORE_DEVICE=1`、
   无 `ID_INPUT*`，宿主 `xinput` 无输出。`.dockerignore` 增 `udev`
   （宿主侧制品，保持镜像精简）。`_HASH_ROOTS` 取舍：按其余 `docker/`
   条目「运行时源码 + 运行时入口（server.py）」口径，本轮补入
   `docker/wechat-gui/entrypoint.sh`（生产入口，P1/P4 修复所在）；compose
   文件与 `udev/` 属部署编排 / 宿主契约，与 Dockerfile 同类不入哈希；
   设备名契约本身在已入哈希的 `uinput_devices.py` 中。
10. **台账**：本小节与 §6 同步：udev 缓解从「未验证」改为「本机已验证
    （2026-11-26，X11 + Xorg libinput；event24/event25 均
    `LIBINPUT_IGNORE_DEVICE=1`、无 `ID_INPUT*`、宿主 xinput 无输出）；
    部署机待复验；Wayland 会话 float 不可行」。

**命令 + 退出码（本轮最终树）**：
- `cd docker/wechat-gui && pixi run tests-unit` → 0（102 passed；总覆盖率
  93.80%）；`pixi run coverage-gates` → 0（6 组 ≥80：`poc_gate` 89.2 /
  `uinput_devices` 96.1 / `uinputd` 96.4 / 其余 100）；`pixi run lint` → 0；
  `pixi run typecheck` → 0（6 源文件）。
- `WECHAT_GUI_POC_ALLOW_HOST_SESSION=1 bash
  docker/wechat-gui/poc/run_stack_poc.sh` → 0（privileged PASS 7/7，193 ms；
  证据 `docs/evidence/poc/wechat-gui-stack-LeoWorkLaptop-privileged-20261126T045413Z.json`）。
- `WECHAT_GUI_POC_RUN_MODE=unprivileged WECHAT_GUI_POC_ALLOW_HOST_SESSION=1
  bash docker/wechat-gui/poc/run_stack_poc.sh` → 0（PASS 7/7，174 ms；
  证据 `...-unprivileged-20261126T045418Z.json`）。
- `XAUTHORITY= WECHAT_GUI_POC_ALLOW_HOST_SESSION=1 bash
  docker/wechat-gui/poc/run_stack_poc.sh` → 1（启动容器前拒绝，无残留）。
- 生产同款能力集密码冒烟（copy / env / rerun）→ 0，
  `wechat:wechat 400` ×3。
- `docker compose -f docker/docker-compose.wechat-gui.yml config` → 0；
  `up -d` → 0（healthy，6s；`restart` ×2 各约 9s healthy）；`down` → 0；
  默认栈 `SANDBOX_TOKEN=x BROWSER_SIDECAR_TOKEN=x docker compose -f
  docker/docker-compose.yml config` → 0。

**变异自证（新增守卫，变异后转红，复原全绿）**：

| # | 变异 | 预期红用例 | 实测 |
|---|---|---|---|
| N1 | 源文件可读判定改 `[[ -r ]]` | `test_resolve_probe_distinguishes_run_user_from_caller` | FAILED |
| N2 | `_xev_connected` 直接 `return True` | `test_xev_connected_requires_socket_fd` | FAILED |
| N3 | `dispatch_command` 键事件硬编码 `pressed=True` | `test_dispatch_key_release_forwarded` | FAILED |
| N4 | `detect_host_session` 直接 `return 1` | `test_run_stack_poc.py` 三态 | FAILED |

**最终树复跑（命令 + 退出码）**：`cd backend && pixi run verify-slice --
--with-seekdb --with-frontend --slice r4-6a` → 0（10 步全 PASS：
tests-all=3472 / coverage-gates=24 / lint / typecheck=216 / gates /
tests-seekdb=15 / frontend-test=716 / frontend-coverage=96.15 /
frontend-lint / frontend-build=2.07）；canonical 证据
`docs/evidence/r4-6a-9971f110.json`（sha8=`9971f110`；`5446cb25` 标历史，
`874e54e1` 更早为历史；**三轮复审修复后由 `98cfdadd` 取代，本行与证据为
历史**）。后端零逻辑改动；SeekDB 真引擎套件经本机
seekdb-smoke 容器通过；根 gates 链（注释 / 同步测试棘轮 / noqa 冻结 /
沙盒漂移 / 红探针 / 文档真相 / 文档数字 / 断言棘轮 / 导出面 / bench /
api-smoke / prose-pin）全绿。

**偏离与未验证**：
- 宿主已装 udev 规则，本轮 PoC 的真实 `float` 不可达（设备在 `xinput`
  不可见）；运行器按「udev 已忽略」安全态通过，真实 float 路径以历史证据
  （03:47，规则安装前）与 hermetic 用例覆盖。
- 本机无 Wayland 会话，Wayland 拒绝逻辑只经临时 socket 根单测，未在真实
  Wayland 桌面复跑。
- 部署机 udev 复验与微信客户端 Live 仍随统一阶段。

### 已完成切片：R4-6a 三轮复审修复（4×P2 + 3 P3 逐条根修——本分支工作区）

**背景**：Oracle 对 `5a79144` 的三轮复审无 P0/P1，给 4×P2 + 3 条 P3。
本批逐条根修并留可红守卫；旧 canonical `9971f110` 自本条起为历史。
不写「复审通过」——结论由命令 / 退出码 / 变异支撑。

1. **F1 SAFE 按名字「任一行 floating」即放行**（`poc/run_stack_poc.sh`）：
   a) 新增 `reject_residual_host_devices`，起容器前 `host_devices_present`
   为真即 exit 1，删除「先浮动残留」分支；
   b) `host_float_probe_script` 改为先取 `xinput list --name-only`，要求
   同名**全部行**均含 `floating`；SAFE 与 ABSENT 同口径连续两轮
   （`safe_streak` / `absent_streak`），float 成功后强制重新列举复核，
   复核不通过即 RETRY，删除「float 返回 0 即结算」路径；
   c) 用例：`test_float_probe_requires_all_rows_floating`（残留 floating +
   新设备 attached → 不 SAFE、rc=1、不 touch）、
   `..._accepts_all_rows_floating_two_rounds`（同名两行均 floating 两轮 →
   SAFE）、`test_reject_residual_devices_exits_before_create`。
2. **F2 「已忽略」为负证据推断**（同文件）：
   a) 列举失败不再记 ABSENT：`listing="$(xinput list --name-only)"` 退出码
   非零即 exit 3；用例 `test_float_probe_listing_failure_is_not_absence`
   （首列举成功、后续 `list` 失败 → rc=3、无 ABSENT、ignored=0）；
   b) 新增 `host_device_ignored_evidence`：按 name→eventN 取宿主节点，
   `udevadm info -q property` 须含 `LIBINPUT_IGNORE_DEVICE=1` 且无
   `^ID_INPUT` 行；无 `udevadm` 返回 2 回落启发式并在证据
   `ignored_basis` 记 `heuristic`（正证据记 `udev_positive`），两轮口径
   保留。用例：`test_udev_positive_evidence_marks_ignored` /
   `..._rejects_residual_id_input` /
   `test_float_until_settled_falls_back_without_udevadm`。首轮复跑暴露真实
   回归：宿主 `/dev/input/eventN` 是 `root:input 0660`、运行用户不在
   `input` 组，原先 `[[ -r ]]` 前置把有效 udev 证据判失败（rc=1，安全态
   未达成）；改为只依据 `udevadm` 退出码，测试节点置 `chmod 000` 留守卫。
   c) `detect_host_session` 收集全部 X* 会话（`HOST_DISPLAYS`），探测 /
   浮动逐会话执行；Wayland 先取 `XDG_RUNTIME_DIR`，缺省扫描
   `/run/user/*/wayland-*`。用例：`test_detect_enumerates_every_x11_socket`
   / `..._falls_back_to_run_user_scan` / `..._probes_every_display`。
3. **F3 两条子性质无守卫**：新增
   `test_float_until_settled_fails_with_partial_safe_and_retry`（SAFE
   keyboard + 恒 RETRY pointer → rc=1、floated=0、ignored=0）与
   `test_float_until_settled_requires_two_absent_rounds`（首轮 ABSENT、次轮
   起 RETRY → rc=1；计数器用文件）。变异：ABSENT `-ge 2`→`-ge 1` 仅红
   后者；结算计数 `-eq 数量`→`-ge 1` 仅红前者（实测各 1 failed /
   17 passed）。
4. **F4 README udev 验证第三步**：镜像内 xinput 命令补
   `-v "${XAUTHORITY}":/xauth:ro -e XAUTHORITY=/xauth`，并注明连接报错
   不等于已验证；本机实测无参数 rc=1、补参数 rc=0。
5. **P3**：README 安全边界能力清单补 SETUID/SETGID/KILL；测试表补 3 条
   float 用例及多会话 / udev 正证据 / 回落；`.dockerignore` 改
   `**/__pycache__` / `**/*.egg-info`，重建镜像内
   `find /app -name '*.pyc' -o -name '*.egg-info' -o -name '__pycache__'`
   无输出。

**命令 + 退出码（最终树）**：
- `cd docker/wechat-gui && pixi run tests-unit` → 0（114 passed；总覆盖率
  93.80%）；`pixi run coverage-gates` → 0（6 组 ≥80：poc_gate 89.2 /
  uinput_devices 96.1 / uinputd 96.4 / 其余 100）；`pixi run lint` → 0；
  `pixi run typecheck` → 0（6 源文件）。
- `WECHAT_GUI_POC_ALLOW_HOST_SESSION=1 bash
  docker/wechat-gui/poc/run_stack_poc.sh` → 0（privileged PASS 7/7；
  `ignored_devices` 两台、`ignored_basis=udev_positive`、
  `floated_devices=[]`、`residual_devices=[]`、`host_displays=[":0"]`、
  `float_sync_ms=1321`；证据
  `docs/evidence/poc/wechat-gui-stack-LeoWorkLaptop-privileged-20261126T054924Z.json`）。
- `WECHAT_GUI_POC_RUN_MODE=unprivileged WECHAT_GUI_POC_ALLOW_HOST_SESSION=1
  bash docker/wechat-gui/poc/run_stack_poc.sh` → 0（PASS 7/7；同
  `ignored_basis` / 空 floated / 空 residual、`float_sync_ms=1316`；证据
  `...-unprivileged-20261126T054931Z.json`）。
- 首轮（`[[ -r ]]` 回归未修时）同命令 → 1：安全态未达成，容器移除、未
  启动 Xorg、无注入、无证据文件；修复后重跑 PASS。
- `cd backend && VITEST_MAX_WORKERS=6 pixi run verify-slice --
  --with-seekdb --with-frontend --slice r4-6a` → 0（10 步全 PASS：
  tests-all=3472 / coverage-gates=24 / lint / typecheck=216 / gates /
  tests-seekdb=15 / frontend-test=716 / frontend-coverage=96.15 /
  frontend-lint / frontend-build=2.81；canonical
  `docs/evidence/r4-6a-98cfdadd.json`，sha8=`98cfdadd`，758 文件级证据；
  `7c673863` 为补 Pyright `returncode` 断言前的中间树，与 `9971f110`
  及更早证据同为历史）。首次未限 worker 的同一命令 → 1：
  frontend-test 5 条加载态等待超时（当时负载 54），单跑 3 条关注用例与
  全量 716 均 PASS；`VITEST_MAX_WORKERS=6` 复跑全链 PASS。

**偏离与未验证**：
- 前端 5 条加载态超时是宿主机负载下的 Testing Library 等待抖动（改动面不涉
  前端）；最终取证用 `VITEST_MAX_WORKERS=6` 限流，未改测试代码 / 断言。
- 多 X 会话遍历只经临时 socket 根单测（本机仅 `:0`）；真实多会话桌面未复跑。
- Wayland `/run/user/*` 回落只经临时扫描根单测；本机无 Wayland 会话。
- 宿主已装 udev 规则，本轮 PoC `floated_devices=[]`（设备在 xinput 不可见），
  真实 float 路径仍以历史证据（规则安装前）与 hermetic 用例覆盖。
- 部署机 udev 复验与微信客户端 Live 仍随统一阶段。

### 已完成切片：G1 审计非根修项根修（§20 修正必须根修——本分支工作区）

**背景**：G1 审计判定 7 项此前的修复止于症状面（补白名单 / 补测试 / 补分支推断 / 补文档），未消除再发机制。按新硬约束 §20 逐条根修——范围由元数据派生、裁决收敛为单一入口、状态显式化、语义字段化、判据由默认可忽略码点集定义、并发路径共用同一插入函数。

逐条（原修复位置 → 根修位置 → 结构性守卫测试）：

1. **P1-1 人格删除级联**：原修复（`R2c 记忆批次 Oracle 深审` P1）在 `PersonaService.delete` 内手写七表白名单，新增表（表情 / 声纹 / 提醒 / 画布）静默漏删。根修：`packages/harness/persona/service.py` 的 `_persona_cascade_models()` 从 `Base.metadata` 派生全部含 `persona_id` 列的表（含未挂 PersonaMixin 的整数列依赖表；比较值按列类型派生），新增 ORM 注册锚 `packages/harness/database/models.py`（唯一显式注册入口，防导入序静默缩水）；无 persona_id 的 `ff_chat_messages` / `ff_core_memory_revisions` 按归属轴清理；`ff_usage_records` 明确保留（`PERSONA_DELETE_RETAINED_TABLES`）；删除前收集表情 `storage_path` / 声纹 `sample_path`，提交后 best-effort 删文件（失败仅告警）。守卫：`test_persona_cascade_derives_every_persona_id_table`（派生集合 == 元数据全集，未覆盖即红）+ `test_delete_clears_every_persona_scoped_table_and_files`（13 张 persona_id 表 + 会话消息 / 修订 / 索引造行 + 双人格 + 文件：A 全清 / B 全留 / 文件不存在 / 二次删 NOT_FOUND）。
2. **P1-2 evaluate 退出 LLM 工具面**：原修复（`R2e-2 抖音 web 通道` item 2）把 `evaluate` 注册进 duty / chat 双面（八工具），页面上下文任意 JS 的副作用暴露给 LLM。根修：`tools/browser_models.py` 拆 `BROWSER_LLM_TOOL_NAMES`（七工具）与协议能力面 `BROWSER_TOOL_NAMES`（八工具）；`build_browser_specs` / `register_browser_sidecar_tools` 默认不含 evaluate（`include_evaluate=True` 显式 opt-in，默认关闭，无新增 env 键）；`app/main.py` 两处注册与日志改用七工具名。抖音通道内部 `client.invoke(EVALUATE_TOOL_NAME)` 不受影响。守卫：`test_all_tools_in_order`（evaluate 不在注册表）+ `test_evaluate_opt_in_extends_llm_surface` + `test_evaluate_still_callable_through_client_for_internal_use` + lifespan 用例断言 chat / duty 注册表均不含 evaluate。
3. **P2-8 注入防御内聚**：原修复（`R2-defense` item 3）在 `ChatService._injection_compose_inputs` 与 `SwipeService` 各自复制包裹 / 标签 / 提示裁决。根修：`chat/service.py` 新增 `compose_for_generation(state, *, injection, intent, provenance, ...)` 单一入口（`GenerationIntent` = reply/stream/swipe；会话回合缺来源分级 fail loud；swipe 默认标签）统一裁决；`compose_messages.wrap_current_message` 改为**必填关键字**；`ChatService.reply` / `stream_reply` / `SwipeService.ride` 全部改经该入口，`_injection_compose_inputs` 删除。守卫：`test_only_unified_entry_calls_compose_messages_in_production`（AST 枚举生产直连点，仅统一入口可直连）+ 统一入口语义三例（标签按分级 / 停用不包裹 / swipe 默认标签）；既有红队与 swipe 用例零行为回归（新增必填参数在测试直调点显式传入）。
4. **P2-10 心流门控隐式契约**：原修复（`R2d-1` item 1 及后续密度锚点补丁）用 `recent[-1] == now_ms` 推断「当前消息是否已入历史」，同毫秒历史产生歧义。根修：`FlowGateInputModel` 删除 `state` 输入，改为显式 `prior_arrivals_ms`（不含当前）+ `backlog_count`；`evaluate_flow_gate` 用「窗口内历史 + 当前」计连发、历史末条作密度锚，删除分支推断；`ChannelRouter._apply_flow_gate` 改为先取历史 → 门控 → 再 `record_inbound`（仅附件按 `has_user_text=False` 不入缓冲）。守卫：重写 `test_flow_gate.py` 的连发 / 密度 / 同毫秒历史 / 空历史用例（旧「两种形态等价」用例删除）。
5. **P2-3 占位文本语义统一**：原修复（`审计 A` P1 + `R2d 复审` P2）由路由器嗅探 `text.strip()` / 各适配器自行清空文本，WeCom / 抖音的 `[图片]` 占位仍被当作可回复文本。根修：`InboundMessageModel.has_user_text`（空文本模型校验强制 False），QQ / Telegram / WeCom / 抖音 / WhatsApp 适配器按各自 wire 语义声明；路由器改按该字段跳过生成（附件收录 / 游标推进不变），观测计数更名 `no_user_text_skip_count`。守卫：四通道纯媒体「不生成 / 不兜底」跨通道用例 + 混合文本仍生成用例 + 各适配器映射断言（wecom / douyin / qq / tg / whatsapp）。
6. **P2-2 归一口径**：原修复（`R2-defense` 复审 item 1）只剔除 `Cf` + U+FE00–FE0F，U+034F / U+180B–180D / U+E0100–E01EF / Hangul 填充符插入仍改变得分。根修：`chat/text_normalization.py` 改为 Unicode 默认可忽略码点可见性等价集（`Cf` 全类 + 不可见 `Mn` 族 + Hangul 填充符；可见组合附加符保留），只作用于匹配副本、原文不动。守卫：`test_chat_text_normalization.py`（12 个不可见码点插入后检测得分与基线一致 + 可见重音阴性集 + NFKC 折叠保持）。
7. **P2-1 表情并发根修**：原修复（`R2d 声纹/表情复审` item 3）把保存点 + 胜者判定只写在 `upload_batch`，`ingest_bytes` 无保存点，竞态落败方整事务回滚并可能删除胜者文件。根修：`multimodal/sticker_service.py` 抽出共用 `_insert_sticker`（落盘 → 保存点插入 → 撞唯一键解析胜者行，仅当胜者路径 ≠ 本路径才删落败文件），`upload_batch` 与 `ingest_bytes` 共用。守卫：`test_insert_sticker_conflict_resolves_winner_and_keeps_file`（冲突分支：行唯一 / 胜者文件在 / get_binary 可用）+ `test_ingest_same_hash_twice_single_row_and_file`（顺序同哈希：单行 / 文件在 / 返回既有记录）。

**文档同步**：persona.md / database.md（级联由元数据派生 + 文件清理 + 保留表）、tools-browser.md（LLM 七工具 / evaluate 内部直调 + opt-in）、channels.md（has_user_text 全 wire 形态 / 心流先历史后记录 / 抖音 evaluate 内部面）、chat.md（统一入口 / 必填参数 / 先历史门控矩阵）、defense.md（缺口矩阵 evaluate 行 + swipe 统一入口 + 默认可忽略归一）、wecom_models / douyin_models docstring（占位不算用户文本）。

**实测（命令 + 退出码；最终树）**：
- `cd backend && pixi run verify-slice -- --slice r4-5-whatsapp` → 0（5 步全 PASS：tests-all=3297 / coverage-gates=24 组 ≥80% / lint / typecheck=211 源文件 / gates；证据 `docs/evidence/r4-5-whatsapp-bd3bdfd3.json`，canonical sha8=`bd3bdfd3`（702 文件级证据；`--hash-only` 复算一致）；`fbc7dc56`（G1 中间树）、`d90786ec`（G1 根修前）与更早证据均为历史，按 README 口径只认与当前 `--hash-only` 一致的一份）。
- `cd backend && pixi run tests-all` → 0（3297 passed）；`pixi run coverage-gates` → 0（24 组 ≥80%：harness/persona 95.6%（1774/1855）、harness/channels 97.3%（3210/3299）、harness/chat 96.3%（1551/1611）、harness/multimodal 94.0%（669/712）、harness/database 94.0%（189/201）、app/main.py 94.1%（556/591）、app/api 85.2%（1257/1476））；`pixi run lint` → 0；`pixi run typecheck` → 0（211 源文件）；`pixi run conftest-env` → 0（228/228）；`pixi run gen-examples` 连跑两次 → 0（三件套 sha256 一致：`.env.example` `02872633…` / `config.example.yaml` `19bba95d…` / `extensions_config.example.json` `1a17d6e6…`）；根 `pixi run gates` → 0（comment-gates violations=0 / sync-test-ratchet +0 / noqa-freeze 0 / conftest-env 228/228 / sandbox-drift 38 项 / red-probe-gate 24 条覆盖 23 文件 / docs-truth / assertion-freeze asserts=9265 raises=673 / surface-check 42/42 + env 791 行 / bench-core PASS / api-smoke PASS / prose-pin 报告）。
- `cd backend && pixi run red-probe --all` → 0：24 条全部 RED（errors=0，含新增 `orm-registry-anchor-removal` → failed=1）。
- 无迁移、无新依赖；前端未动。

### 已完成切片：G1 根修 B1（§20 剩余非根修项——本分支工作区）

**背景**：G1 审计另列 5 项非根修修复（时钟单一读数、窗宽单飞、声明时长、墙钟轮询、首调用同步读盘）与 1 项注释段落缺口。本批按 §20 逐条消除再发机制，并补齐对应结构性守卫。

逐条（原修复位置 → 根修位置 → 守卫测试）：

1. **梦境终态时钟**：原实现 `run_dream(now_ms=…)` 用单一读数同时写 `started_ms` / `finished_ms`（运行耗时恒 0，终态时间不是结束时刻）。根修：入口改为可调用 `clock_ms`（`packages/harness/memory/dreaming.py`），开始与结束各读取一次，`finished_ms` 取第二次读数；调度器透传可调用时钟，API 缺省真实时钟。守卫：`test_run_dream_finished_ms_uses_injected_clock`（可推进伪时钟：两次读数、`finished_ms > started_ms` 且等于第二读数）、调度器透传用例。
2. **梦境单飞**：原实现以 `DREAM_REENTRY_WINDOW_MS` 窗内查询为唯一防线——窗外的并发触发会双插 running 行。根修：迁移 `0019_dream_runs_single_flight` 给 `ff_dream_runs` 增可空 `running_marker` + 唯一键 `uq_ff_dream_runs_persona_running (persona_id, running_marker)`（运行中 `'1'`，终态 / 失败 / 崩溃收敛清 NULL）；`run_dream` 以插入带标记行为权威单飞动作，唯一键冲突回滚后读回既有运行并原样返回；窗内复用保留为辅助快路径，二者关系写入 memory.md。守卫：`test_run_dream_unique_marker_conflict_returns_existing`（窗外在途行撞键复用）、`test_run_dream_clears_marker_and_allows_rerun`、`test_run_dream_failed_terminal_clears_marker`、`test_migration_dream_single_flight.py`（SQLite 升 / 降 / 复升 + 唯一键行为）、SeekDB `test_dream_run_single_flight_marker_unique`（1062 约束点名 + 清标记后可再插）、迁移链 0019 升级 / 降级 / 复升断言。
3. **声纹实测时长**：原实现信任上传方 `duration_ms` / `sample_rate_hz` 声明。根修：新增 `packages/harness/voice/voiceprint_audio.py` + `VoiceprintAudioProbeModel`——WAV 从 RIFF/fmt/data 头实测采样率 / 声道 / 时长，裸 PCM 按声明采样率与字节长度换算，不可解析容器回落声明；注册与校验的时长 / 采样率门槛及落库元数据一律用实测值，非法 / 截断头按 `payload_invalid` 拒收。守卫：`test_voiceprint_audio.py`（不同时长 / 采样率 / 双声道 / 裸 PCM / 非法与截断头 / 容器回落）、`test_voiceprint_service.py` 的声明撒谎（短音频声明长时长、低采样率声明高采样率）注册与校验双路径拒收；既有用例已改为真实 WAV 载荷（列明于 voice.md 测试面）。
4. **测试去墙钟**：原实现 10 处 `_wait_until` 与多处以 2-5s 墙钟 deadline + 固定睡眠轮询，慢机抖动会翻面。根修：新增 `tests/async_wait.py`（`wait_until` 10,000 次零延时让出封顶、`drain_loop` 固定次数让出），10 处本地 helper 与 duty / channels / reminders 的轮询循环统一接入；负向断言改 `drain_loop`。保留的真实时间面逐条注明：企微心跳超时改 `closed_event` 事件门（`wait_for` 为真实超时语义）、`DelayedCrashRunner` 0.05s 模拟停机窗口内在途、后台任务 30s 阻塞哨兵、`tg_runner` 线程池往返 0.01s、`realtime_fakes` 3600s 常驻哨兵、`realtime_fakes.py:628` 的 `wait_until` 2s 兜底预算、`test_app_realtime_voice.py:98` 的 `FakeWebSocket.wait_for` 5s deadline。守卫：`test_async_wait.py`（注入记录器断言只请求零延时让出 + 失败消息携带预算与描述 + `drain_loop` 零延时）。
5. **特质库预加载**：原实现首个管理面请求在事件循环内同步读 YAML。根修：`trait_library` 新增 `load_official_presets_async` / `load_official_components_async` / `preload_official_trait_library`（读盘 + 解析经 `run_blocking_ops`），`app/main.py` 启动期预加载，`app/api/personas.py` 两处读取改异步入口（未预加载的懒路径同样下沉线程池）。守卫：`test_preload_warms_official_cache`（预加载后缓存命中且异步读取返回同实例）、`test_lazy_official_loader_uses_blocking_sink`（注入记录器断言懒路径经 `run_blocking_ops`）、`test_app_main.py` 启动后缓存已热；persona.md 同步。
6. **注释段落**：`sticker_bridge.py` / `sticker_models.py` / `sticker_service.py` / `realtime_models.py` / `realtime_session.py` / `realtime_vad.py` 的 8 条 hints 逐处读上下文后补 Google 段落（Args / Returns / Raises 按实际签名）。注释门复跑：violations=0、hints=23（剩余均为本批范围外存量：browser-sidecar 17 条 + 其 server 1 条、tool-sandbox server 1 条、chat cli 2 条、duty 1 条、douyin runner 1 条——均为各自域后续注释批次，非本批文件）。

**实测（命令 + 退出码；最终树）**：
- `cd backend && pixi run verify-slice -- --slice r4-5-whatsapp` → 0（5 步全 PASS：tests-all=3316 / coverage-gates=24 组 / lint / typecheck=212 源文件 / gates；证据 `docs/evidence/r4-5-whatsapp-fc26e182.json`，sha8=`fc26e182`（707 文件级证据；`--hash-only` 复算一致）；B2 后由 `a09677e7` 取代，本行保留为历史；`bd3bdfd3`（G1 根修前）与更早证据同样为历史，按 README 口径只认与当前 `--hash-only` 一致的一份）。
- `cd backend && pixi run tests-all` → 0（3316 passed）；`pixi run coverage-gates` → 0（24 组 ≥80%：harness/voice 93.1%（2628/2822）、harness/memory 94.8%（1902/2007）、harness/persona 95.7%（1783/1864）、harness/channels 97.3%（3211/3299）、app/main.py 93.9%（558/594）、app/api 85.2%（1257/1476））；`pixi run lint` → 0；`pixi run typecheck` → 0（212 源文件）；`pixi run conftest-env` → 0（228/228，豁免 0）；`pixi run gen-examples` 连跑两次 → 0（三件套 sha256 与连跑前一致：`.env.example` `02872633…` / `config.example.yaml` `19bba95d…` / `extensions_config.example.json` `1a17d6e6…`）；根 `pixi run gates` → 0（comment-gates violations=0 hints=23 / sync-test-ratchet +0 / noqa-freeze 0 / conftest-env 228/228 / sandbox-drift 38 项 / red-probe-gate 25 条覆盖 24 文件 / docs-truth / assertion-freeze asserts=9316 raises=679 / surface-check 42/42 + env 791 行 / bench-core PASS / api-smoke PASS / prose-pin 报告）。
- `cd backend && SEEKDB_TEST_URL=127.0.0.1:2881 pixi run pytest src/tests/integration_seekdb -q --asyncio-mode=auto` → 0（12 passed：迁移链 0019 真引擎升 / 降 / 复升、梦境单飞 1062、既有并发唯一键 / 行锁 / 混合检索）。
- `cd frontend && pixi run npx vitest run src/test/contract/dtoContract.test.ts` → 0（2 passed；前端源码未改动，契约 fixture 与后端导出面一致）。
- 无新增依赖 / env 键；迁移号 `0019`。

### 已完成切片：G1 根修 B2（§20 剩余非根修项——本分支工作区）

**背景**：G1 审计剩余 4 项非根修（群聊表情固定收录 `pool[0]`、历史 user 回合包裹条数上限、`tag_mimicry` 短前缀锚点、配置 / 计数口径多处硬编码）与 1 项缓解台账缺失。本批按 §20 逐条消除再发机制，并新增文档数字单源门与两条数量棘轮。

逐条（原修复位置 → 根修位置 → 守卫）：

1. **P2-4 群聊表情收录归属**：原实现群聊入站表情固定写入第一位候选（`pool[0]`）素材库，与本轮实际应答集合脱节，非应答人格与多人格应答者的库长期偏斜。根修：`packages/harness/channels/channel_service.py` 抽出 `_select_group_responders`（纯读取选择结论）与 `_group_responder_persona_ids`，表情收录按**本轮全部应答人格**逐人绑定上下文调用收录桥，逐人格独立哈希去重与失败隔离；单聊仍只收录主人格，应答侧「各人格用各自库」不变。守卫：`test_group_sticker_ingested_into_every_responder_library`（两位应答者各一份、第三人不在应答集不受影响）、`test_group_sticker_same_hash_deduped_independently_per_library`（同哈希跨人格互不吞并、各库去重）、`test_group_sticker_ingest_failure_isolated_from_reply`（单人格收录失败不阻断其余人格与回复）。
2. **P2-8 历史 user 回合全包裹**：原实现 `HISTORY_WRAP_MAX_TURNS=10` 只包裹最近 10 条历史 user 回合，更早回合裸文本进入请求面，边界信号随会话长度失效。根修：删除该常量与切片，`compose_messages` 对**全部历史 user 回合**逐条独立 nonce 定界包裹（assistant 回合保持原文）；包裹总开销与 user 回合数线性（每条两行定界 + 12 位 nonce），不改变 `state.turns` 原文与落库内容。守卫：`test_compose_wraps_all_history_user_turns_with_independent_nonce`（11 条历史 user 全部包裹、逐条开闭 nonce 独立且不重复、assistant 原文、原文 / 落库不变）。
3. **TAG 前缀任意位置**：原实现 `tag_mimicry` 的 `<…>…</…>` 规则锚定句首且仅允许 ≤8 字符前缀，长前缀中段标签不命中。根修：规则改为行内任意位置匹配同名开闭标签对（内容段 ≤120 字符、闭合回引同名），权重保持 1.0（单项不达阈值，防误报）；`【…】…【/…】` 句首规则不变。守卫：`test_tag_mimicry_matches_pair_anywhere_in_line`（超 8 字符深前缀命中且单项不触发）、`test_tag_mimicry_leaves_non_pairs_unmatched`（无闭合对 / 异名对 / 普通尖括号比较不误报）；原 55 条红队矩阵阳性集零回归。
4. **P2-11 口径单源与机械守卫**：
   a. `config/loader.py` 顶层段白名单改为公开常量 `KNOWN_TOP_LEVEL_SECTIONS`（并由 `LLM_SECTION_KEY` / `SEARCH_SECTION_KEY` 组成），未知键告警文案由常量生成；`config/search_registry.py` 的读取与 `_section_key` 引用同一常量，不再三处硬编码。
   b. `config/wizard_generators.py` docstring 删除手写「当前 31 个」，改为「覆盖 `_ENV_SECTIONS` 全量登记前缀」。
   c. 计数改为精确断言：`test_injection_rule_table_has_exact_kind_counts`（29 条 + 逐种类 10/5/8/4/2）与 `test_red_team_set_has_exact_template_counts`（55 总 / 29 阳 / 26 阴）。新增 `scripts/check_doc_counts.py`：从 `INJECTION_RULES`、红队模板表与生成器 `_ENV_SECTIONS` 派生计数，校验 `docs/backend/{defense.md,chat.md,config.md}` 内数字字面（解析失败 / 不一致即红）；backend `doc-counts` 任务接入根 gates 的 docs-truth 之后。
   d. `scripts/prose_pin_check.py` 增加 `--gate` / `--baseline` / `--update`：当前 prose 计数（基线 `scripts/.prose_pin_baseline.json` = 75）不得高于基线，report 模式保留；根 gates 的 prose-pin 改为 `--gate`。
   e. `scripts/red_probe.py --gate` 增加冻结数量棘轮：`backend/red_manifest.toml` 的 `[metadata].grandfathered_baseline = 190`，当前 `grandfathered` 条目数 190 不得高于基线（只冻结不收敛同样红），`--prune` 精简后同步下调。
5. **缓解台账落档**：§6 新增「缓解台账（非根修，已排期）」表，逐项记录仍属缓解的 5 项（流式合规不重试 / 实时语音多段完成序 / TG 429 夹取单次重试 / 注释门测试面豁免 / 梦境 `stats`/`decisions` JSON 动态键），含现状、暂不根修理由与根修方案 / 目标批次；历史包裹与 TAG 前缀两条本批已根修，不再列入缓解表。

**文档同步**：channels.md（表情收录归属改为本轮全部应答人格 + 逐人格去重 / 失败隔离）、defense.md（全部历史 user 包裹与线性开销、tag_mimicry 任意位置与残余跨行边界、红队 55 条分档与精确计数说明）、chat.md（compose 链 / 注入面 / 测试面索引去上限化 + 线性常数开销）、docs/README.md（doc-counts / prose 棘轮 / 红探针冻结数量棘轮）。

**实测（命令 + 退出码；最终树）**：
- `cd backend && pixi run verify-slice -- --slice r4-5-whatsapp` → 0（5 步全 PASS：tests-all=3322 / coverage-gates=24 组 / lint / typecheck=212 源文件 / gates；证据 `docs/evidence/r4-5-whatsapp-a09677e7.json`，canonical sha8=`a09677e7`（709 文件级证据；`--hash-only` 复算 `sha8=a09677e7 files=709` 一致；G1 复审修复后由 `a9b45231` 取代，本行保留为历史）；`d41a6aa2` / `5a5446ad`（B2 中间树）、`fc26e182`（B1）与更早证据为历史，按 README 口径只认与当前 `--hash-only` 一致的一份）。
- `cd backend && pixi run tests-all` → 0（3322 passed）；`pixi run coverage-gates` → 0（24 组 ≥80%：harness/channels 97.3%（3227/3315）、harness/chat 96.3%（1550/1610）、harness/config 96.6%（1554/1609）、app/main.py 93.9%（558/594）、app/api 85.2%（1257/1476））；`pixi run lint` → 0；`pixi run typecheck` → 0（212 源文件）；`pixi run conftest-env` → 0（228/228，豁免 0）。
- `cd backend && pixi run gen-examples` 连跑两次 → 0（三件套 sha256 一致：`.env.example` `02872633…` / `config.example.yaml` `19bba95d…` / `extensions_config.example.json` `1a17d6e6…`）。
- 根 `pixi run gates` → 0（comment-gates violations=0 hints=23 / sync-test-ratchet +0 / noqa-freeze scripts 7≤7 其余 0 / conftest-env 228/228 / sandbox-drift 38 项 / red-probe-gate 25 条覆盖 24 文件 + grandfathered 棘轮 190≤190 / docs-truth / doc-counts 12 项（规则 29 / 红队 55 / 配置分节 31）/ assertion-freeze asserts=9333 raises=679 / surface-check 42/42 + env 791 行 / bench-core PASS / api-smoke PASS / prose-pin 棘轮 75≤75）。
- 无新增依赖 / env 键 / 迁移；前端未动（`--with-frontend` 未跑，源码零改动）。

### 已完成切片：G1 复审修复（8×P2 + 4 条备注逐条根修——本分支工作区）

**背景**：G1 根修复审给出有条件通过项——8 条 P2 与 4 条备注仍属症状面（快照读假设、可忽略集缺口、声明值回落台账缺失、适配器映射守卫不完整、纯媒体推高未读积压、段键硬编码、去重自证与双时钟、AST 守卫可绕过、收集失败语义不符）。本批按 §20 逐条消除再发机制并留守卫；旧 canonical `a09677e7` 自本批起为历史。

逐条（原修复位置 → 根修位置 → 结构性守卫）：

1. **P2-1 快照读（MySQL 协议 / SeekDB 行锁语义）**：冲突后胜者解析原为普通 ``SELECT``（RR 下可能落入事务早先读视图而看不到对方已提交行）。根修：`multimodal/sticker_service.py` 抽出 `_winner_select`，胜者查询带 ``with_for_update()`` 行锁（当前读，规避快照读（行锁语义）落入事务早先读视图）。守卫：`test_winner_select_uses_locking_read`（现已更名为 `test_winner_select_uses_shared_locking_read`；MySQL 方言编译断言锁定子句——快照读回归即红）+ SeekDB `test_sticker_conflict_across_connections_uses_current_read`（现已更名为 `test_sticker_conflict_stale_snapshot_retries_to_winner`；显式 RR 引擎、B 先建读视图、A 提交后同哈希落败：单行、胜者文件存在、败者文件已清、胜者主键一致）；同连接冲突分支既有用例保留。
2. **P2-2 可忽略集补全**：原剔除面为 `Cf` + 少量不可见 `Mn` + Hangul 填充符，漏 Khmer 固有元音 / 蒙古文 FVS4 / 未赋值可忽略段。根修：`chat/text_normalization.py` 改为 Unicode `Default_Ignorable_Code_Point` 属性全族区间表（含 U+17B4–17B5、U+180B–180F、U+FFF0–FFF8、U+E0000–E0FFF 等；保留 `Cf` 超集面）。守卫：`test_chat_text_normalization.py` 新增 9 个码点参数 + 句首 / 「忽略」中间 / 句尾逐位置同分（检测得分与归一副本一致）+ 可见组合符 / 常规 Hangul 字母与音节阴性集；defense.md 写明「默认可忽略属性全族」。
3. **P2-9 不可解析容器回落**：LEDGER §6 缓解台账新增「声纹不可解析容器（MP3/OGG/WebM/MP4）回落声明值」行（现状 / 原因 / 根修方案：容器头探测或供应商元数据 / 目标 R5-2）；voice.md 校验链与数据模型表改为「WAV / PCM 以实测为准；不可解析容器回落声明并见缓解台账」。
4. **P2-3 WhatsApp 映射守卫**：`test_whatsapp_models.py` 回退矩阵新增 `has_user_text` 期望列（文本 / caption True；媒体纯占位 False）并断言映射结果——与 QQ / Telegram / WeCom / 抖音四适配器的 `has_user_text` 断言一致（五适配器映射面完整）。
5. **P2-3 纯媒体积压**：`chat/flow_gate.py` 的 `record_inbound` 参数改为语义字段 `has_user_text`——仅用户文本入站递增未读积压并进缓冲，纯媒体只推进到达历史（`channels/channel_service.py` 显式传入）。守卫：`test_flow_gate.py` 纯媒体零积压 / 文本正常计数；`test_channel_router_flow.py` 连发纯媒体积压为 0、6 图后文本立即应答（不触发积压延迟 8s）、合并批次缓冲仍无空条目。
6. **P2-11 段键硬编码**：`config/registry_admin.py` 的 `_section_key` 改返回 `config_loader.LLM_SECTION_KEY`（已导入同一 loader，无循环导入）。
7. **P2-4 去重自证 + 双时钟**：`test_channel_router_sticker.py` 的同哈希群聊用例改为记录逐次收录调用（不再用自去重 set 掩盖调用次数），断言各库按哈希独立去重且重复到达逐次进入对应库；`channels/channel_service.py` 的 `handle_inbound` 每入站只读一次时钟，`_apply_flow_gate` / `_reply_via_group` 显式接收 `now_ms`（收录选择与应答选择同源；成功应答时间戳用同一次读数）。守卫：`test_group_sticker_cooling_boundary_single_clock_read_keeps_sets_aligned`（逐次推进时钟 + 冷却边界：时钟读数恰 1 次、收录集 == 应答集；变异回二次读数即红）。
8. **P2-8 AST 守卫绕过**：`test_chat_service.py` 抽出 `_compose_message_call_sites`——覆盖 `ast.Name`、`ast.Attribute.attr`、`ImportFrom` 别名，并扫描模块层调用；生产守卫改为消费该 matcher。守卫：别名导入调用与 `service.compose_messages(...)` / 模块层直调两条反例证明可命中；生产面仍断言仅 `compose_for_generation` 可直连。
9. **备注项**：`persona/service.py` 的表情 / 声纹路径收集各自 try/except 告警（与「收集失败不阻断」docstring 一致；用例 `test_delete_survives_file_path_collection_failure` 证明行照删、声纹文件仍清、表情残留无害）；§6 实时语音多段完成序目标批次改 `R5-1`；B1 真实等待清单补 `realtime_fakes.py:628`（2s 兜底预算）与 `test_app_realtime_voice.py:98`（5s deadline）；`BROWSER_TOOL_NAMES` 注为协议能力面单一真源（无运行时消费方，由文档与 `test_protocol_surface_is_llm_surface_plus_evaluate` 结构测试对照，不再是无说明死常量）。
10. **文档同步**：defense.md（DI 全族）/ channels.md（纯媒体不计未读 + 单次时钟）/ chat.md（积压只计用户文本）/ voice.md / tools-browser.md；§6 缓解台账与目标批次同步。

**实测（命令 + 退出码；最终树）**：
- `cd backend && pixi run verify-slice -- --slice r4-5-whatsapp` → 0（5 步全 PASS：tests-all=3352 / coverage-gates=24 组 / lint / typecheck=212 源文件 / gates；证据 `docs/evidence/r4-5-whatsapp-a9b45231.json`，canonical sha8=`a9b45231`（709 文件级证据；**本批后由 `ddc4fb27` 取代，本行与证据为历史**；旧 `a09677e7`、`fc26e182`、`bd3bdfd3` 与更早证据均为历史）。
- `cd backend && pixi run tests-all` → 0（3352 passed）；`pixi run coverage-gates` → 0（24 组 ≥80%：harness/channels 97.3%（3225/3316）、harness/chat 96.3%（1547/1607）、harness/multimodal 94.0%（671/714）、harness/persona 95.6%（1790/1872）、harness/config 96.6%（1554/1609）、harness/voice 93.1%（2628/2822）、app/main.py 93.9%（558/594）、app/api 85.2%（1257/1476））。
- `cd backend && pixi run lint` → 0；`pixi run typecheck` → 0（Success: no issues found in 212 source files）；`pixi run conftest-env` → 0（228/228，豁免 0）。
- `cd backend && pixi run gen-examples` 连跑两次 → 0（三件套 sha256 一致：`.env.example` `02872633…` / `config.example.yaml` `19bba95d…` / `extensions_config.example.json` `1a17d6e6…`）。
- 根 `pixi run gates` → 0（comment-gates violations=0 hints=23 / sync-test-ratchet 无回升 / noqa-freeze 基线 0 / conftest-env 228/228 / sandbox-drift 38 项 / red-probe-gate 211 源文件 25 探针覆盖 24 文件 + grandfathered 190≤190 / docs-truth / doc-counts 12 项 / assertion-freeze asserts=9369 raises=679 / surface-check 42/42 + env 791 行 / bench-core PASS / api-smoke PASS / prose-pin 75≤75）。
- `cd backend && pixi run red-probe --all` → 0：25 条全部 RED（errors=0）。
- `cd backend && SEEKDB_TEST_URL=127.0.0.1:2881 pixi run pytest src/tests/integration_seekdb -q --asyncio-mode=auto` → 0（13 passed：含新增双连接同哈希 RR 收敛用例；活容器 `seekdb-smoke`）。
- 无迁移 / 无新增依赖 / 无新增 env 键；前端未动。


### 已完成切片：G1 三审 3×P2 根修 + 单栈 SeekDB 拍板落地（本分支工作区）

**背景**：G1 三审给出 3 条 P2（锁读形态 / SeekDB 用例前提 / 6235 与 1213 收敛仅缓解）与 4 条备注；用户同时拍板「方案 B：单栈 SeekDB + 清理行锁引擎误导表述 / 路径 / 命名」。本批按 §20 逐条根修并新增机械守卫。

**A 部分（G1 三审 3×P2 + 备注）**

1. **A1 锁读形态**：`multimodal/sticker_service.py` 的 `_winner_select` 由 `with_for_update()` 改为 `with_for_update(read=True)`（方言编译 `LOCK IN SHARE MODE`）——不请求排他锁，消除「唯一键检查已持共享锁 → 胜者查询升级排他锁」的 S→X 升级死锁。守卫：`test_winner_select_uses_shared_locking_read`（方言编译断言共享锁子句；撤锁 / 回退 FOR UPDATE 即红）。
2. **A2 SeekDB 用例前提**：`test_concurrency_invariants.py` 的跨连接用例 B 首条语句改为 `ff_stickers` 表快照读（与生产 `upload_batch` / `ingest_bytes` 预读同形），断言旧读视图普通 SELECT 不可见胜者、SeekDB 落败插入实测抛 6235、调用方回滚单次重试后收敛为既有胜者（单行 / 胜者文件保留 / 落败 webp 清理 / 零异常）；另立无旧快照用例证明 1062 分支的共享锁定读解析胜者。删除「`SELECT 1` 建立读视图」错误前提（`SELECT 1` 不触表、不建快照）。变异撤锁子句 → 首用例红（实测）。
3. **A3 6235 / 1213 收敛（根修）**：新增 `packages/harness/database/conflict_retry.py`——`is_transaction_conflict` 只按 `orig.args[0] in {6235, 1213}` 判定；`run_with_conflict_retry` 在**调用方**捕获后 `rollback` → 可选胜者保护文件清理 → 整段操作单次重试（预读命中已提交胜者 → 既有行 / 重复拒收；二次冲突原样上抛；非冲突驱动错误不重试）。`upload_batch` / `ingest_bytes` 接入（单条上传无独立路径），落盘尝试日志 + `_discard_loser_attempts`（只清理无已提交行引用、且胜者路径不同的落败文件——胜者文件恒保留）。守卫：一次性 6235 → 上传 dupe / 收录既有记录（零异常）；连续两次 → 上抛且只重试一次；1213 同收敛；1042 等非冲突错误一次即上抛；红探针 `transaction-conflict-retry-removal`（去实现 → 2 用例 RED）。
4. **A4 备注**：`persona_rotation_models.py` 冷却定义改为「自该轮入站时刻起」（与单次时钟读数一致）；`channels.md` 占位语义区分「纯媒体占位不算用户文本；位置 / 按钮 / 交互回执属用户动作按含内容处理」；`voiceprint_audio.py` 首行改为「WAV / 裸 PCM 以实测为准；不可解析容器回落声明值（见缓解台账）」；既有冷却用例已断言逐人格集合相等 + 时钟读数恰 1 次，无需加强。

**引擎矩阵实测（真实 `StickerService`；临时 `mysql:8.0.42` 容器 `--rm --tmpfs` 用完移除）**：
- MySQL 协议族 / 旧快照：普通 SELECT 不可见（0）；`LOCK IN SHARE MODE` 与 `FOR UPDATE` 均为当前读（可见 1）；落败插入 1062 → 共享锁定读收敛（created=False + 胜者主键，胜者文件保留、败者清理）。
- SeekDB / 旧快照：普通 SELECT 不可见（0）；锁定读**不跨越旧快照**（共享 / 排他均 0，实测）；落败插入 `OperationalError 6235` → 调用方重试收敛（既有 id / 单行 / 落败 webp 清理 / 零异常）。
- SeekDB / 无旧快照：落败插入 1062 → 共享锁定读可见胜者（1）并收敛。
- S→X 升级死锁：MySQL 协议族两事务各持共享锁后同时 `FOR UPDATE` → 1213；改 `LOCK IN SHARE MODE` → 无错误。SeekDB 实测共享锁与共享锁互斥（第二把共享读 1205），故其并发收敛由 6235 重试承载而非锁定读跨快照——文档与用例按实测锁定该边界。

**B 部分（单栈 SeekDB 方案 B）**

1. **B1 默认单栈**：`DatabaseConfig` 默认 `127.0.0.1:2881 / root / 空密码 / ai_agent_companion`（SeekDB 实例）；`SeekDBSettings` 仅保留检索池字段（enabled / database / vector_dim / pool_min / pool_max），host / port / user / password 改为可选覆盖（None = 继承权威库，password 留空继承 `DATABASE_PASSWORD`），检索库名默认 `ai_agent_companion_search`（同实例、不同库名）。新增 `pools/seekdb_compat.resolve_seekdb_config`：显式 host / port 与权威库不一致 → `BusinessException(CONFIG_INVALID)`「单栈 SeekDB：权威库与检索池必须同实例；拆分需另立决策」（dev 同口径）；`PoolManager.start` 与 `build_memory_store` 均经该唯一解析入口。
2. **B2 去误导命名**：`backend/src` / `docs` / `scripts` / `pixi` 说明的旧口径「MySQL 8」权威存储统一改为「SeekDB 权威存储（MySQL 协议，aiomysql 驱动）」；裸行锁引擎泛称改「MySQL 协议 / SeekDB 行锁语义」；`innodb_lock_wait_timeout` 保留处注明「SeekDB 兼容占位变量，生效旋钮 `ob_trx_lock_timeout`」；driver 字符串 `mysql+aiomysql` 不变（协议层）。
3. **B3 单栈证据**：`integration_seekdb/test_authority_engine.py`——专用 scratch 库 `aac_authority_verify` 经 Alembic 升级 head 后 `init_database(DatabaseConfig(type=mysql, host/port=真引擎))`，完成人格创建 / 读取 / 删除 round-trip；不触碰套件库，结束恢复引擎持有器槽位；缺 `SEEKDB_TEST_URL` 按套件策略 FAIL 不 skip。
4. **B4 配置与文档**：`.env.example` 重新生成（DATABASE_PORT 2881、SEEKDB_* 留空继承说明）；config.md（单栈不变式段）/ database.md（单栈段 + 方言表）/ README（关系库选型）/ pools.md（检索池配置与解析入口）/ wizard 分节文案同步；LEDGER §4 Q3 改为「单栈 SeekDB（权威库与检索库同实例、不同库名；行锁语义沿用 MySQL 协议历史术语）」。
5. **B5 守卫**：单栈不变式单测（host / port 不一致 → `CONFIG_INVALID`；留空继承 / 显式一致通过）；`scripts/check_doc_counts.py` 新增全仓措辞机械检查（禁把 MySQL 8 表述为权威存储的旧口径（须带 SeekDB 或 MySQL 协议限定）+ 裸行锁引擎泛称，唯一白名单 `innodb_lock_wait_timeout`；扫描 505 文件），随 `doc-counts` 接入根 gates。

**文档同步**：multimodal.md（并发冲突语义：1062 共享锁定读 / 6235+1213 回滚单次重试 / 胜者文件保护）、database.md（单栈）、config.md（单栈配置）、pools.md、channels.md（占位语义区分）、voiceprint_audio 首行、persona_rotation_models 冷却口径、test-strategy/README（四模块 + 双形态收敛 + 权威引擎用例）、LEDGER §4/§6。

**实测（命令 + 退出码；最终树）**：
- `cd backend && pixi run verify-slice -- --slice r4-5-whatsapp` → 0（5 步全 PASS：tests-all=3359 / coverage-gates=24 组 / lint / typecheck=213 源文件 / gates；证据 `docs/evidence/r4-5-whatsapp-5f3a5e42.json`，canonical sha8=`5f3a5e42`（711 文件级证据；**G1 四审修复后由 `ee6fa370` 取代，本行与证据为历史**；`18ba54f1` / `ddc4fb27`（本批中间树）与 `a9b45231` 及更早证据为历史）。
- `cd backend && pixi run tests-all` → 0（3359 passed）；`pixi run coverage-gates` → 0（24 组 ≥80%：harness/database 93.5%（215/230）、harness/multimodal 94.0%（694/738）、harness/pools 100.0%（271/271）、harness/channels 97.3%（3226/3316）、harness/chat 96.3%（1547/1607）、harness/config 96.6%（1554/1609）、harness/memory 94.8%（1904/2009）、harness/persona 95.6%（1790/1872）、harness/voice 93.1%（2628/2822）、app/main.py 93.9%（558/594）、app/api 85.2%（1257/1476））。
- `cd backend && pixi run lint` → 0；`pixi run typecheck` → 0（Success: no issues found in 213 source files）；`pixi run conftest-env` → 0（228/228，豁免 0）。
- `cd backend && pixi run gen-examples` 连跑两次 → 0（三件套 sha256 一致：`.env.example` `1df4d48c…` / `config.example.yaml` `19bba95d…` / `extensions_config.example.json` `1a17d6e6…`——`.env.example` 因配置面变更更新且复跑 byte-stable）。
- 根 `pixi run gates` → 0（comment-gates files=246 violations=0 hints=23 / sync-test-ratchet 无回升 / noqa-freeze 基线 0 / conftest-env 228/228 / sandbox-drift 38 项 / red-probe-gate 212 源文件 26 探针覆盖 25 文件 + grandfathered 190≤190 / docs-truth / doc-counts 12 项（规则 29 / 红队 55 / 配置分节 31 / 措辞扫描 505 文件）/ assertion-freeze asserts=9399 raises=684 / surface-check 42/42 + env 791 行 / bench-core PASS / api-smoke PASS / prose-pin 75≤75）。
- `cd backend && pixi run red-probe --all` → 0（26 条全部 RED，errors=0）。
- `cd backend && pixi run tests-seekdb` → 0（15 passed：迁移链 / 并发不变式（含双形态表情收敛）/ 混合检索往返 / 权威引擎单栈 round-trip；活容器 `seekdb-smoke`）。
- 无迁移 / 无新增依赖 / 无新增 env 键；前端未动（`--with-frontend` 未跑，源码零改动）。

### 已完成切片：G1 四审 4×P2 修复（本分支工作区）

**背景**：G1 四审给出 4 条 P2——同扩展名胜者文件在终态失败时被误删、重试前失败留下孤儿文件、措辞门禁未覆盖仓库根三处残留、复审段历史断言被后续批次改写。本批逐条根修并留守卫；旧 canonical `5f3a5e42` 自本批起为历史，中间树 `06d6b7a5`（补 `dist` 跳过前的验收树）亦为历史。

逐条（原缺陷位置 → 根修位置 → 结构性守卫）：

1. **F1 同扩展名胜者文件误删**：`multimodal/sticker_service.py` 的 `_insert_sticker` 在 1062 分支保留胜者路径文件后，后续终态提交失败曾按日志清理该路径，删掉胜者仍引用的字节。根修：终态失败清理由 F2 的 `_remove_unreferenced_files` 按已提交行裁决（同路径已有胜者行即保留）；`_insert_sticker` 的 1062 分支不再需要把同路径尝试移出 `written`，该对象身份分支在 F2 落地后冗余并已删除。守卫：`test_same_path_winner_file_survives_terminal_commit_failure`（同人格同哈希同扩展名胜者行已提交 → 批内 1062 → 终局提交抛非冲突 2013 → 胜者行与其文件均在——经 F2 的已提交行裁决通过）；`test_different_extension_winner_still_removes_loser_file`（异扩展名胜者：落败文件仍按路径判定清理）。
2. **F2 重试前失败孤儿文件**：`_discard_loser_attempts` 清理并清空 `written` 后，重试若在预读阶段即抛非冲突错误，前一次保留的落盘文件无人跟踪而残留。根修：该方法返回「文件保留、本次行未提交」的尝试集合；`_insert_sticker` 重写同一路径即从该集合移除（重写者接管清理责任）；`upload_batch` / `ingest_bytes` 的终态失败路径改走 `_remove_unreferenced_files`——按同一人格哈希面查询已提交行，仅删除路径无任何已提交行引用的文件（查询失败保留文件，不掩盖原异常）。守卫：`test_retry_pre_read_failure_removes_orphan_file`（6235 清理保留 X → 重试预读 2013 → X 清除、无行残留）、`test_retry_pre_read_failure_keeps_winner_referenced_file`（同一路径已有已提交胜者行 → X 不删）。
3. **F3 措辞门禁全仓化**：`scripts/check_doc_counts.py` 的扫描根由限定清单改为仓库根全树（后缀 `.md/.py/.toml/.ini/.yml/.yaml/.example`），跳过 `.git` / `.pixi` / `node_modules` / `dist` / `coverage*` / `docs/evidence/logs` / `docs/evidence/*.json` / `__pycache__` / 脚本自身（跳过清单与理由写入模块 docstring）；禁用正则放宽为 `MySQL\s*(8(\.\d+(\.x)?)?)?\s*[（(]?\s*权威`（「MySQL 协议」不误伤）。同时修 3 处残留：`backend/alembic.ini:4`、`database/units/engine_holder.py:33`、`database/engine.py:56` 统一改为「SeekDB 权威存储（MySQL 协议）/ SQLite 测试」。变异实证：把旧句写进 `backend/alembic/env.py`（旧门禁扫不到）→ 新门禁 exit 1；`alembic.ini` 回旧句 → exit 1；写入「MySQL 协议权威」→ exit 0。
4. **F4 台账历史段还原**：`docs/LEDGER.md` 的 G1 复审段恢复当时事实——`_winner_select` 当时为 `with_for_update()` 行锁（当前读，规避快照读（行锁语义）落入事务早先读视图），不改写为后续批次的「共享锁定读」；两个守卫用例名加注现名（`test_winner_select_uses_shared_locking_read` / `test_sticker_conflict_stale_snapshot_retries_to_winner`）；同段其余历史断言逐条核对未被本批改写。
5. **备注项**：multimodal.md 并发语义补「SeekDB 共享锁互斥 → 1205 不在重试集合（仅 6235 / 1213）；当前两入口均先预读故不可达；未来新增不预读调用方需补 1205 处理」；database.md / config.md 单栈段写明 `DATABASE_TYPE=sqlite` 时不变式仍按 host / port 字面比较（dev 同口径，无方言豁免；仅 `seekdb.enabled=true` 时校验）。

**实测（命令 + 退出码；最终树）**：
- `cd backend && pixi run verify-slice -- --slice r4-5-whatsapp` → 0（5 步全 PASS：tests-all=3363 / coverage-gates=24 组 / lint / typecheck=213 源文件 / gates；证据 `docs/evidence/r4-5-whatsapp-ee6fa370.json`，canonical sha8=`ee6fa370`（711 文件级证据；`--hash-only` 复算 `sha8=ee6fa370 files=711` 一致）；`06d6b7a5`（本批中间树）与 `5f3a5e42` 及更早证据为历史）。
- `cd backend && pixi run tests-all` → 0（3363 passed）。
- `cd backend && pixi run coverage-gates` → 0（24 组 ≥80%：harness/multimodal 95.3%（733/769）、harness/database 93.5%（215/230）、harness/channels 97.3%（3226/3316）、harness/chat 96.3%（1547/1607）、harness/config 96.6%（1554/1609）、harness/persona 95.6%（1790/1872）、harness/voice 93.1%（2628/2822）、app/main.py 93.9%（558/594）、app/api 85.2%（1257/1476））。
- `cd backend && pixi run lint` → 0；`pixi run typecheck` → 0（213 源文件）；`pixi run conftest-env` → 0（228/228，豁免 0）。
- `cd backend && pixi run gen-examples` 连跑两次 → 0（三件套 sha256 一致：`.env.example` `1df4d48c…` / `config.example.yaml` `19bba95d…` / `extensions_config.example.json` `1a17d6e6…`）。
- 根 `pixi run gates` → 0（comment-gates files=246 violations=0 hints=23 / sync-test-ratchet 无回升 / noqa-freeze 基线 0 / conftest-env 228/228 / sandbox-drift 38 项 / red-probe-gate 212 源文件 26 探针覆盖 25 文件 + grandfathered 190≤190 / docs-truth / doc-counts 12 项（规则 29 / 红队 55 / 配置分节 31 / 措辞扫描 575 文件）/ assertion-freeze asserts=9420 raises=687 / surface-check 42/42 + env 791 行 / bench-core PASS / api-smoke PASS / prose-pin 75≤75）。
- `cd backend && pixi run tests-seekdb` → 0（15 passed：迁移链 / 并发不变式（含双形态表情收敛）/ 混合检索往返 / 权威引擎单栈 round-trip；活容器 `seekdb-smoke`）。
- F3 变异实证（退出码）：`backend/alembic/env.py` 写旧句 → 1（命中 `env.py:116`）；`backend/alembic.ini` 回旧句 → 1（命中 `alembic.ini:4`）；写「MySQL 协议权威」→ 0（不误伤）。
- 无迁移 / 无新增依赖 / 无新增 env 键；前端未动（`--with-frontend` 未跑，源码零改动）。

### 已完成切片：G2 全局审计 5×P2 运行时正确性根修（本分支工作区）

**背景**：G2 全局审计给出 5 条运行时正确性 P2（N1 公开入站体无界、N2 出站私网守卫可被非规范形态 / 自动重定向绕过、N3 满队丢弃文案与端点恒 200 实现矛盾、N4 时间戳解析双实现且 SMS 非有限值可能抛错、N9 流式收尾句柄被外部取消时泄漏）。本批逐条根修并留守卫；旧 canonical `ee6fa370`（G1 四审）自本批起为历史。

逐条（原缺陷位置 → 根修位置 → 结构性守卫）：

1. **N1 公开入站体有界（三端点统一）**：QQ / SmsForwarder 端点此前分别整包 `request.json()` / `request.body()`（chunked 无 Content-Length 时无上限），WhatsApp 仅有局部实现。根修：新增 `backend/src/app/api/inbound_body.py` 单源模块——`read_bounded_body` 以 `request.stream()` 逐块累计（累计超限瞬间抛受控 400，异常传播终止迭代、后续块不再索取），声明 Content-Length 预检保留；`parse_json_object` / `read_bounded_json_object` 统一空体 / 非法 JSON / 非对象错误面；per-channel 上限常量单源（QQ 8 MiB——容纳 OneBot `base64://` 媒体段，按出站附件 8 MiB 帽与表情单文件 5 MiB 的 base64 膨胀留余量；SmsForwarder / WhatsApp 64 KiB）。三端点全部接入。守卫：三端点 chunked 超限伪 stream 提前中止（`test_channels_{qq,sms_forwarder,whatsapp}_api.py`）、声明超限零读取、QQ 8 MiB 内 base64 通过 / 超 8 MiB 拒；`test_inbound_body_structure.py` AST 枚举 `main.py` 豁免鉴权 POST 路由，断言每个处理函数调用共享读取器且不直接 `request.json()/body()`（新增豁免 POST 未接即红）。
2. **N2 出站目标守卫（防 SSRF 绕过）**：`outbound_guard` 此前仅拒绝可被 `ipaddress` 直接解析的私网字面量，且 `wecom_runner._fetch_url` 依赖 httpx 自动重定向（首跳校验后可直接跳到私网）。根修：`validate_http_url` 改为「主机归一（去空白 / 尾点 / 方括号 / 大小写）→ 可注入 resolver（默认 `asyncio` 事件循环 `socket.getaddrinfo`）→ 全部地址判公网（loopback / link-local / private / reserved / multicast / unspecified / IPv6 / v4-mapped）」，解析失败 / 空结果 fail-closed；非规范 IPv4 形态（`127.1` / `2130706433` / `0x7f.1` / `0177.0.0.1` / `0`）一律按解析结果判定。`wecom_runner._fetch_url` 改手动重定向循环（≤3 跳、每跳重新校验目标、3xx 无 `Location` 受控失败），客户端自动跟随关闭；流式限读与 8 MiB 帽保留。守卫：`test_outbound_guard.py` 七类非规范 / 特殊形态全部拒绝、多地址任一私网拒绝、公网通过、resolver 抛错 / 空结果 / 非法地址 fail-closed、主机归一矩阵；`test_wecom_runner.py` 重定向链逐跳校验、重定向到私网、无 Location、超跳数、resolver 失败；既有企微 URL 用例同步列明并注入 fake resolver（`test_url_attachment_downloaded_via_shared_client` / `test_url_http_error_reported_per_attachment` / `test_url_transport_error_reported_per_attachment` / `test_url_response_over_size_cap_fails_per_attachment`）。
3. **N3 丢弃语义文案单源**：QQ / SMS / 企微 runner 的模块 docstring、队列常量注释、满队 ERROR 文案与 `channels.md` 多处旧口径「平台 / 手机端重投可重新消费」与端点恒 200（仅非 200 重试）矛盾。根修：统一为真源口径「端点恒 200（上报方仅非 200 重试）→ 满队丢弃即最终丢失；不登记去重仅吸收偶发重复」；企微长连接按「回调即时确认（平台仅对失败回调重试）」同义收口。用例更名 `test_dropped_message_not_dedup_registered_so_repeat_can_be_consumed`（QQ / SMS），并各补「满队丢弃后端点仍 200」端点用例（`TestQueueFullAck::test_queue_full_drop_still_returns_ok`）。
4. **N4 时间戳解析单源**：SMS 与 WhatsApp 各持一份解析实现，SMS 缺 `math.isfinite` / `OverflowError` 护栏（非有限值可能抛错）。根修：新增 `backend/src/packages/harness/channels/inbound_timestamp.py` 共享实现（入口标识 `channel_label` 仅用于诊断），非有限值（`NaN` / `Infinity` / `"nan"` / `"inf"` / `"Infinity"` / `"1e999"`）与溢出 / 非法 / 缺失 / 非正统一回落服务端 Epoch 毫秒；SMS / WhatsApp 的 `resolve_*_timestamp_ms` 仅保留自有名字与 docstring 并委托共享实现；删除 SMS 旧实现与 §6 对应遗留行。守卫：`test_channel_timestamp.py` 两通道同一参数化矩阵（秒 / 毫秒 / 非有限 / 非法字符串 / 缺失 / 布尔 / 非正）与逐值一致性。
5. **N9 流式收尾句柄收口**：`realtime_session._finish_stream_segment` 对 `CancelledError` 仅原样上抛，挂起中的 `finish()` 被外部取消时连接句柄无人收口；`asr_stream_client.finish()` 取消路径同样未关闭传输；用量包装在收尾未落定时会被清理 cancel 补记。根修：`_finish_stream_segment` 捕获取消后以独立任务 + `asyncio.shield` 驱动 `handle.cancel()` 至完成（幂等、屏蔽二次取消）再上抛；`asr_stream_client.finish()` 取消路径经同样的 shield 收口（传输关闭 + 派生任务回收）；`_StreamAsrUsageHandle` 标记「finish 已发起但未落定」，随后的清理 `cancel()` 只关闭连接、不补记 usage 行（与 finish 失败回退批量口径一致）；会话关闭 / 对端断开路径经 `_finalize` 的 stale 任务取消链同步收口。守卫：`test_realtime_session_streaming.py` 外部取消 / 传输断开落在 finish 挂起时句柄已取消、任务集合清零、无 final；`test_voice_stream_clients.py` finish 取消后 cancel 事件出站、传输关闭、`_tasks` 清零、cancel 幂等；`test_voice_service_streaming.py` 取消后清理不留 usage 行；`voice.md` 计次表补「finish 被外部取消 → 0 行」行。

**实测（命令 + 退出码；最终树）**：
- `cd backend && pixi run verify-slice -- --slice r4-5-whatsapp` → 0（5 步全 PASS：tests-all=3442 / coverage-gates=24 组 / lint / typecheck=215 源文件 / gates；证据 `docs/evidence/r4-5-whatsapp-ac48f039.json`，sha8=`ac48f039`（716 文件级证据；**G2 审计修复 B2 后由 `eeed61b8` 取代，本行为历史**）；`6fd7efc8`（本批最终文案收口前树）、`ee6fa370`（G1 四审）与更早证据为历史）。
- `cd backend && pixi run tests-all` → 0（3442 passed；G1 四审基线 3363，本批 +79）。
- `cd backend && pixi run coverage-gates` → 0（24 组 ≥80%：app/api 85.3%（1249/1465）、harness/channels 97.5%（3277/3362）、harness/voice 93.0%（2657/2857）、harness/database 93.5%（215/230）、harness/multimodal 95.3%（733/769）、harness/persona 95.6%（1790/1872）、app/main.py 93.9%（558/594））。
- `cd backend && pixi run lint` → 0；`pixi run typecheck` → 0（215 源文件）；`pixi run conftest-env` → 0（228/228，豁免 0）。
- `cd backend && pixi run gen-examples` 连跑两次 → 0（三件套 sha256 一致：`.env.example` `1df4d48c…` / `config.example.yaml` `19bba95d…` / `extensions_config.example.json` `1a17d6e6…`）。
- 根 `pixi run gates` → 0（comment-gates files=248 violations=0 hints=23 / sync-test-ratchet +0 / noqa-freeze 0 / conftest-env 228/228 / sandbox-drift 38 项 / red-probe-gate 源文件 214、冻结基线 190、探针 28 条覆盖 27 文件 / docs-truth / doc-counts 12 项（规则 29 / 红队 55 / 配置分节 31 / 措辞扫描 580 文件）/ assertion-freeze files=224 asserts=9506 raises=700 / surface-check app/api 104→62 已登记 + 42 豁免、env 791 行 / bench-core PASS 6 项 / api-smoke PASS 4 端点 / prose-pin 75≤75）。
- 红探针 adhoc：`app.api.inbound_body:read_bounded_body` → RED（failed=1）；`packages.harness.channels.inbound_timestamp:resolve_epoch_ms_timestamp` → RED（failed=42）。
- `cd backend && pixi run tests-seekdb` → 0（15 passed；活容器 `seekdb-smoke`）。
- 无迁移 / 无新增依赖 / 无新增 env 键；前端未动（`--with-frontend` 未跑，源码零改动）。

### 已完成切片：G2 审计修复 B2（N5–N8 / N10–N12 + 缓解表逐条根修——本分支工作区）

**背景**：G2 审计剩余 N5–N8 / N10–N12 与缓解台账缺项。本批逐条消除再发机制：预算上限与用途分区单源、注释门编号正则与前端扫描、措辞门同行判定、F1 与 F2 机制归并、断言棘轮新文件收紧、台账迁移 head 机械校验、前端构建聚合基线与两条结构测试；旧 canonical `ac48f039` 自本批起为历史。

逐条（根修位置 → 守卫 / 变异证据）：

1. **N5 预算上限单源**：新增 `packages/harness/usage/budget_limits.py`——`BUDGET_COUNT_PAGE_LIMIT=200` / `BUDGET_COUNT_PAGE_CAP=50` / `DAILY_BUDGET_MAX=乘积` 与运行时容量访问器；LLM 网关与 voice 预算计数器引用同一页常量，`config/models.py` 六处与 `voiceprint_models.py` 两处 `le` 全部改引用 `DAILY_BUDGET_MAX`。用途分区单源改为 `usage/models.py` 的 `BudgetPurposeGroup` + `BUDGET_PURPOSE_GROUPS`（llm / voice / search / exempt）。守卫：`test_daily_budget_limit_is_single_source_across_config_and_counters`（逐字段 `le == DAILY_BUDGET_MAX`、两计数器页常量同源、容量访问器一致）、`test_budget_purpose_partition_covers_usage_enum_without_overlap`（分区并集 == `set(UsagePurpose)` 且互不重叠）；红探针 `daily-budget-limit-single-source-removal`（`budget_count_capacity` 桩 → failed=1）。变异：任一 `le` 改为 `DAILY_BUDGET_MAX + 1` → 单源测试 1 failed。
2. **N6 注释门泛化**：`check_chinese_comments.py` 的编号正则改为独立记号 `R\d[a-z]?(-\d+)?` / `G\d+` / `D\d+(-\d+)?` / `P\d+`（词边界排除 `R2D2` / `MP3` / `UUID4` / `chunk_index`）；扫描根补 `frontend/src`，以最小词法状态机只解析 TS/JS 注释（字符串与模板字符串不入检查面）；清理 `realtime_session.py` 3 处、`voice_service.py` 1 处、`export_dto_schemas.py` 4 处、前端三文件 4 处编号残留（逐处按上下文改写为自证语义）。变异：生产注释写回 `R4` → exit 1；写 `R2D2` → exit 0。门禁：files=356 violations=0 hints=23。
3. **N7 措辞门可绕过**：`check_doc_counts.py` 改为同行规则——`(?i)\bmysql\b` 与「权威」同现且该行不含 `SeekDB` 与「协议」即红；后缀补 `.ts/.tsx/.js/.mjs/.json/.cfg/.sh` 与无后缀 `Dockerfile/Makefile`；跳过清单保持不变并写入模块 docstring。变异矩阵 8 形态（4 正 4 反）：`MySQL 8.0.42 权威存储` / `小写 mysql 8 权威` / `MySQL 5.7 权威` / `以 MySQL 8 作为权威库` → 4 行红；`SeekDB 权威存储（MySQL 协议）` / `MySQL 协议 / SeekDB 行锁语义` / `SeekDB … 底层为 MySQL` / 含「协议」限定 → 不误伤。新暴露残留（`.env.example`、`config/models.py` 的 `mysql（生产权威）` 描述、LEDGER 自身旧口径）已同步改写/重生成。门禁：13 项一致、措辞扫描 817 文件。
4. **N8 F1 / F2 机制归并**：`multimodal/sticker_service.py` 删除 1062 分支内「按对象身份把同路径尝试移出 `written`」的冗余分支——终态失败清理由 F2 的 `_remove_unreferenced_files` 按已提交行裁决（胜者路径恒保留）。`test_same_path_winner_file_survives_terminal_commit_failure` 经 F2 仍绿；变异：把 `_remove_unreferenced_files` 改为无条件删除 → 该用例 1 failed。直接状态断言未新增：删行后 `written` 合法保留同路径尝试，原「不含该尝试」断言不再成立，改由终态已提交行裁决覆盖。
5. **N10 断言棘轮收紧**：`assertion_freeze.py` 的 gate 语义改为「基线外新测试文件即红（提示评审后 `--update`）」；`--update` 将当前 27 份基线外新文件并入（基线 225 文件 / asserts=9535 / raises=702）。脚本自证：临时新增 `test_tmp_freeze_probe.py` → `--enforce` exit 1（明细「基线外新测试文件」）；`--update` 后 exit 0；删除该文件 → exit 1（基线内文件缺失）；再 `--update` → exit 0。新增 `test_assertion_freeze_gate.py` 两用例（`_report` 判红 + `_write_baseline` 并入）。
6. **N11 台账与性能文档**：LEDGER 迁移链改 `0001..0019` 并补 0019 说明；§5 门禁命令的易变数字（后端用例数 / mypy 文件数 / 前端用例与构建耗时 / bundle 数字）改为「以 canonical 证据 JSON 为准」，仅保留可机械核对的迁移 head——`check_doc_counts.py` 从 `backend/alembic/versions` 派生 head 并校验（变异 0019→0018 → exit 1）。`docs/backend/performance.md` 构建基线改为聚合口径（初始 JS / lazy chunk 数与总量 / 构建耗时，删除内容哈希文件名，注明明细以 `pixi run bundle-check` 输出为准）；本机实测：初始 389,799 B（gzip 123,627 B）、lazy 25 个 / 340,756 B（gzip 110,888 B）、暖缓存构建 2.78s。`check_bundle_budget.mjs` 的 summary 行补 `lazy_gzip` / `lazy_chunks` 聚合字段。
7. **N12 结构测试**：`config/auth_policy.py` 新增 `WEBHOOK_PATH_AUTH_CHECKS`（path 前缀 → 检查函数）单源表，`ensure_auth_policy` 逐项派发；`test_auth_policy.py` 机械枚举 `main.py` 豁免鉴权 POST 路由并断言每条路径命中映射，且生产策略逐项执行全部检查（变异：删除 whatsapp 映射项 → 1 failed）。`test_pools_seekdb.py` 新增单栈 SeekDB 参数化矩阵 18 形态（enabled 9：继承 / 显式一致 / 用户与密码变量覆盖 / host / port / 双不一致 fail loud；disabled 9：不变式不校验、显式覆盖原样解析、PoolManager 零 SeekDB 装配）（变异：不变式 `or` → `and` → 4 failed）。
8. **缓解表补齐**：§6 新增 SmsForwarder 发送者归一碰撞面（R5-3，需产品决定点号语义）与流式 ASR 起始边界（R5-1，VAD 候选帧纳入流式起点）两行（现状 / 原因 / 根修方案 / 目标批次）；N3 满队短计说明与 N4 时间戳旧行已清；N9 finish 句柄已根修，无残留行。

**实测（命令 + 退出码；最终树）**：
- `cd backend && pixi run verify-slice -- --slice r4-5-whatsapp --with-seekdb --with-frontend` → 0（10 步全 PASS：tests-all=3466 / coverage-gates=24 组 / lint / typecheck=216 源文件 / gates / tests-seekdb=15 / frontend-test=702 / frontend-coverage=96.08 / frontend-lint / frontend-build=3.08s；证据 `docs/evidence/r4-5-whatsapp-eeed61b8.json`，canonical sha8=`eeed61b8`（718 文件级证据；`ac48f039`（G2 运行时修复）与更早证据均为历史）。
- `cd frontend && pixi run bundle-check` → 0（PASS total=389799 lazy=25；summary initial_gzip=123627 lazy_bytes=340756 lazy_gzip=110888 lazy_chunks=25）。
- `cd backend && pixi run gen-examples` 连跑两次 → 0（三件套 sha256 一致：`.env.example` `e756beba…` / `config.example.yaml` `19bba95d…` / `extensions_config.example.json` `1a17d6e6…`）。
- 根 `pixi run gates` → 0（comment-gates files=356 violations=0 / red-probe-gate 29 探针覆盖 28 文件 / doc-counts 13 项 / assertion-freeze asserts=9535 raises=702 / 其余门禁同链）。
- 变异证据：N5 `le + 1` → 单源测试 failed；N6 注释写回 `R4` → exit 1、`R2D2` → exit 0；N7 8 形态 4 红 4 绿；N8 无条件删除孤儿清理 → F1 用例 failed；N10 临时新文件 → exit 1 → `--update` → exit 0；N11 迁移 head 0018 → exit 1；N12 删除 whatsapp 映射 → failed、单栈不变式 `or`→`and` → 4 failed。
- 无新增依赖 / env 键 / 迁移；前端源码改动仅清理注释，后端与外键面未变。

### 已完成切片：G2 终审修复（N9 / N7 / N6 / N3 / N11 逐条收口——本分支工作区）

**背景**：G2 终审给出 5 条 P2（N9 finish 尾段收口仍可被取消打断、N7 措辞门把 `mysqld` 误当 MySQL 字样、N6 注释门未覆盖 `backend/alembic` 与 `frontend/scripts`、N3 企微用例 docstring 沿用旧「重投可重新消费」口径、N11 台账仍以证据 JSON 指代迁移链 head）。本批逐条根修并留守卫 / 变异；旧 canonical `eeed61b8`（G2 审计修复 B2）自本批起为历史。

逐条（根修位置 → 守卫 / 变异证据）：

1. **N9 finish 终段取消安全**：`asr_stream_client.finish()` 成功尾段的 `_shutdown` 与 `_wait_settled` 超时收尾改经 `_finalize_cancellation_safe`——收尾（关闭传输 + 回收派生任务 + 置终态）作为独立任务挂起，以 `asyncio.shield` 循环等待至完成，重复取消只记录不打断，收尾完成后原取消信号优先上抛（收尾异常不掩盖）。`cancel()` 与既有取消路径语义（cancel 事件 + 正常关闭 + 幂等）不变。守卫：`test_voice_stream_clients.py` 新增「传输 close 被门闩挂起时取消 finish → 关闭完成 / `_tasks` 清零 / 零新增挂起任务 / 二次 cancel 不重复关闭」与「关闭在途重复取消仍完整收口」；`test_voice_service_streaming.py` 新增真客户端同场景「零 usage 行」。变异：尾段改回直接 `await self._shutdown(...)` → 上述 3 用例红（3 failed / 37 passed）。
2. **N7 措辞门词边界**：`check_doc_counts.py` 的 MySQL 判定改为 `(?i)\bmysql(?![a-z])`——`MySQL8.0.42` / `MySQL8` 这类紧贴版本号的写法仍算命中（缺 SeekDB / 协议限定即红），`mysqld` 守护进程名不再误伤；同行判定与裸行锁引擎泛称判定不变。变异矩阵 10 形态（5 红 5 绿）：原 4 条阳性 + `MySQL8.0.42 权威存储`（缺 SeekDB / 协议限定）→ 红；`mysqld 权威守护` + 原 4 条阴性（带 SeekDB 或「协议」限定）→ 绿。
3. **N6 注释门扫描根补齐**：`check_chinese_comments.py` 的 `_TREE_ROOTS` 增 `backend/alembic`（Python 面同 backend/src 规则）与 `frontend/scripts`（TS/JS 注释词法面），模块 docstring 同步扫描范围。新暴露 8 行编号残留逐处按上下文语义化改写：`alembic/env.py`「迁移链起点无域表」、`000000000001_initial.py`「版本链锚点，无域表」、`009`「回合观测元数据」、`010`「swipes 候选重生成」、`011`「定时提醒落库」、`012`「置顶 / 冻结 / 检索强化的持久面」、`013`「事实 / 证据链 / 更正循环」、`check_bundle_budget.mjs` 去掉批次前缀。变异：`backend/alembic/env.py` 与 `frontend/scripts/check_bundle_budget.mjs` 注释各写 `R4` → exit 1（逐文件命中）；改写 `R2D2` → exit 0。门禁：files=377 violations=0 hints=23。
4. **N3 企微用例口径**：`test_wecom_runner.py::test_queue_overflow_does_not_register_dropped_msgid` 的 docstring 改为「丢弃不登记去重：同 msgid 再次到达仍可消费（仅吸收平台偶发重复）」，与 `wecom_runner` 模块 docstring「满队丢弃即最终丢失；不登记去重仅吸收平台偶发重复」及 `channels.md` 同源。
5. **N11 迁移 head 指针**：§5 门禁命令的迁移链说明改为「迁移链 head 以 `backend/alembic/versions` 为准，由 doc-counts 机械校验」；`check_doc_counts.py` 的 `_migration_facts`（从版本目录派生 head 并校验 LEDGER 字面）仍在 gates 链内。

**实测（命令 + 退出码；最终树）**：
- `cd backend && pixi run verify-slice -- --with-seekdb --with-frontend --slice r4-5-whatsapp` → 0（10 步全 PASS：tests-all=3469 / coverage-gates=24 组 ≥80% / lint / typecheck=216 源文件 / gates / tests-seekdb=15 / frontend-test=702 / frontend-coverage=96.08 / frontend-lint / frontend-build=2.66s；证据 `docs/evidence/r4-5-whatsapp-819c3f22.json`，canonical sha8=`819c3f22`（718 文件级证据；`--hash-only` 复算一致；**G2 五审修复后由 `87fc56e8`、其后由 `5eb1d5ea` 取代，本行为历史**）；`eeed61b8`（G2 审计修复 B2）与更早证据均为历史）。
- 变异证据：N9 尾段回退直接 await → 新 3 用例红（3 failed / 37 passed）；N7 10 形态 5 红 5 绿；N6 两扫描根写 `R4` → exit 1、写 `R2D2` → exit 0。
- 根 `pixi run gates` → 0（comment-gates files=377 violations=0 / red-probe-gate 29 探针覆盖 28 文件 / doc-counts 13 项 / assertion-freeze asserts=9549 raises=705 / 其余门禁同链）。
- `cd backend && pixi run tests-seekdb` → 0（15 passed；活容器 `seekdb-smoke`）。
- 无迁移 / 无新增依赖 / 无新增 env 键；前端仅 `scripts/check_bundle_budget.mjs` 注释一行。

### 已完成切片：G2 五审修复（N9 三条取消安全守卫 + 证据 / 注释门覆盖边角——本分支工作区）

**背景**：G2 五审唯一 P2——`asr_stream_client.finish()` 的四处 `_finalize_cancellation_safe` 接入中，此前只有成功尾段有「关闭在途取消仍收口」的红守卫；收尾超时、业务失败、外部取消三条出口的守卫缺失，回退为直接 `await` 不会变红。本批补三条门闩用例，并收口两个覆盖边角（证据哈希根覆盖注释门已纳入的树、注释门前端配置面）；旧 canonical `819c3f22`（G2 终审修复）自本批起为历史。

逐条（守卫 → 断言面 / 变异证据）：

1. **收尾超时出口守卫**：`test_asr_settle_timeout_close_cancel_still_finalizes`——final 事件永不到达触发 settle 超时，`close_gate` 把 `_shutdown(STREAM_CLOSE_GOING_AWAY, "settle_timeout")` 挂在传输关闭在途；此时取消 `finish` → 断言关闭码 `(1001, "settle_timeout")`、`close_calls == 1`、`handle._tasks` 为空、`_closed` 为真，取消信号上抛。
2. **业务失败出口守卫**：`test_asr_business_failure_cancel_still_finalizes`——收尾事件出站后注入对端 1011 断流触发 `BusinessException` 出口，`cancel()` 收口挂在关闭在途；关闭在途重复取消两次 → 断言 `session.cancel` 出站、关闭码 `(1000, "client_cancel")`、连接只关一次、`handle._tasks` 为空，取消信号上抛（不暴露业务失败异常）。
3. **外部取消出口守卫**：`test_asr_cancel_exit_second_cancel_still_finalizes`——外部取消进入 `CancelledError` 出口的 `cancel()` 收口并挂在关闭在途；期间二次取消 → 断言连接只关一次、`_tasks` 为空、无新增挂起任务、取消信号上抛，收口完成后幂等 `cancel()` 不重复关闭。
4. **变异矩阵（四接入点；逐处临时回退为直接 `await`，复原后 29 passed）**：业务失败出口 `_finalize_cancellation_safe(self.cancel())` → `await self.cancel()` → 仅用例 2 红（1 failed / 28 passed）；外部取消出口同改 → 仅用例 3 红（1 failed / 28 passed）；收尾超时出口 `_finalize_cancellation_safe(self._shutdown(STREAM_CLOSE_GOING_AWAY, "settle_timeout", ...))` → 直接 `await` → 仅用例 1 红（1 failed / 28 passed）；成功尾段 `_finalize_cancellation_safe(self._shutdown(STREAM_CLOSE_NORMAL, "session_finished", ...))` → 直接 `await` → 既有两条成功尾段守卫同时红（2 failed / 27 passed）。
5. **证据哈希根覆盖边角**：`verify_slice.py` 的 `_HASH_ROOTS` 增 `backend/alembic` 与 `frontend/scripts`（与注释门扫描根对齐——证据哈希须覆盖被门禁约束的树）。新增根枚举 22 个源文件（21 个 Alembic 环境 / 迁移文件 + 1 个前端预算脚本），`__pycache__` 按既有跳过口径剔除，无缓存 / 生成物。canonical 由 718 文件增至 740 文件级证据。
6. **注释门前端配置面**：`check_chinese_comments.py` 的 `_TREE_ROOTS["frontend"]` 增 `frontend/eslint.config.js` / `frontend/vite.config.ts` / `frontend/vitest.config.ts`（TS/JS 注释词法面），模块 docstring 同步。前端树 files=111 violations=0；全链 comment-gates files=380 violations=0 hints=23（原 377——新增三文件均无编号残留，无需清理暴露项）。
7. **断言基线**：三条守卫新增断言后运行 `pixi run python ../scripts/assertion_freeze.py --update`（exit 0），基线 asserts 9549→9570（+21）、raises 705→708（+3）、文件 225 不变；`--enforce` 复跑 exit 0。

**实测（命令 + 退出码；最终树）**：
- `cd backend && pixi run pytest src/tests/unit/test_voice_stream_clients.py -q --asyncio-mode=auto` → 0（29 passed——既有 26 + 新增 3）。
- `cd backend && pixi run verify-slice -- --with-seekdb --with-frontend --slice r4-5-whatsapp` → 0（10 步全 PASS：tests-all=3472 / coverage-gates=24 组 ≥80% / lint / typecheck=216 源文件 / gates / tests-seekdb=15 / frontend-test=702 / frontend-coverage=96.08 / frontend-lint / frontend-build=3.36s；证据 `docs/evidence/r4-5-whatsapp-5eb1d5ea.json`，canonical sha8=`5eb1d5ea`（743 文件级证据；`--hash-only` 复算一致；G2 六审边角并入后由本行取代 `87fc56e8`，后者为历史）；`819c3f22` 与更早证据均为历史）。
- `cd backend && pixi run coverage-gates` → 0（24 组 ≥80%：harness/voice 93.0%（2667/2868）、harness/channels 97.4%（3276/3362）、app/api 85.3%（1249/1465）、app/main.py 93.9%（558/594）等全组 PASS）。
- `cd backend && pixi run lint` → 0；`pixi run typecheck` → 0（216 源文件）；`pixi run conftest-env` → 0（228/228，豁免 0）。
- `cd backend && pixi run gen-examples` 连跑两次 → 0（三件套 sha256 一致：`.env.example` `e756beba…` / `config.example.yaml` `19bba95d…` / `extensions_config.example.json` `1a17d6e6…`）。
- 根 `pixi run gates` → 0（comment-gates files=380 violations=0 hints=23 / sync-test-ratchet 无回升 / noqa-freeze 基线 0 / conftest-env 228/228 / sandbox-drift 38 项 / red-probe-gate 源文件 215、冻结基线 190、探针 29 条覆盖 28 文件 / docs-truth / doc-counts 13 项（规则 29 / 红队 55 / 配置分节 31 / 措辞扫描 817 文件）/ assertion-freeze asserts=9570 raises=708 / surface-check 62 已登记 + 42 豁免、env 791 行 / bench-core PASS 6 项 / api-smoke PASS 4 端点 / prose-pin 75≤75）。
- `cd backend && pixi run tests-seekdb` → 0（15 passed；活容器 `seekdb-smoke`）。
- 变异矩阵见逐条第 4 项；复原后源文件与 HEAD 零差异（`_finalize_cancellation_safe` 四处接入原样）。
- 无迁移 / 无新增依赖 / 无新增 env 键；前端仅新增三处配置扫描根（源码零改动）。

**G2 六审边角并入**：`scripts/verify_slice.py` 的 `_HASH_ROOTS` 增补
`frontend/eslint.config.js` / `frontend/vite.config.ts` / `frontend/vitest.config.ts`
（注释门扫描根与证据哈希根对齐；文件数 740→743，canonical 更新为 `5eb1d5ea`）。

### 已完成切片：前端实时语音 act 警告根修 + 采集生命周期守卫（本分支工作区）

**背景**：8 条 React `act(...)` 警告来自 `RealtimeVoicePage` 的 `ready` 分支——fire-and-forget 续体在 `await capture.start()` 后于 act 外回写会话状态，测试却以同步 `act(() => client.emit(ready))` 立即断言。组件面另有一处真实缺陷：`MicrophoneCapture.stop()` 在 `getUserMedia` 未决时为空操作，迟到的媒体流随后照建且无人停轨（麦克风常开 / 隐私风险）；停止后立即重开时旧会话的迟到结果还可能覆盖新会话能力与暂停态。本批逐条根修；旧 canonical `5eb1d5ea` 自本批起为历史。

逐条（根修位置 → 守卫 / 变异证据）：

1. **测试根修（8 处）**：`RealtimeVoicePage.capabilities.test.tsx`（2 处）与 `RealtimeVoicePage.test.tsx`（6 处）中「同步 emit ready 后立即断言」的块改为 `await act(async () => { client.emit(...); await Promise.resolve() })`；内层 `await` 满足 `require-await`，断言保持原样（未改为 `findBy` / `waitFor`）。`src/pages/realtime` 复跑：21 passed（7+14）、输出中 `not wrapped in act` 计数 0。
2. **能力回读补强**：新增「浏览器未生效 AEC 时摘要展示降级（服务端公告为开）」用例——fake 采集注入 `echoCancellation:false`，断言摘要行 `回声消除 降级` 与 AEC 提示条。把该用例的 act 临时改回同步后，断言立即失败（`Unable to find ... /回声消除 降级/`）并同时触发 2 条 act 告警，证明该语义锁定的是异步回读链路而非默认值兜底。
3. **页面会话身份守卫**：`ready` 异步续体在 `await capture.start()` 后与每次回写前检查 `captureRef.current === capture`，不等则直接返回；`resumeCapture` 的 `await capture.resume()` 后同样校验。停止 / 重开的旧会话结果不再覆盖新状态（原「可恢复错误」用例的迟到回写也被该守卫消除——这正是修复前门禁实测需要同时回退该守卫才能复现 8 例的原因）。
4. **采集世代 / pending-stop**：`MicrophoneCapture` 增加世代计数与 `pendingStop` 标记；`stop()` / `pause()` 自增世代，`startInternal` 在 `getUserMedia`、`AudioContext.resume`、worklet 加载（含节点创建）后的每个 `await` 检查世代，过期时停轨、断开节点、关闭上下文并返回；启动资源改为局部束、成功后才提交实例字段，迟到启动无法覆盖新会话。`stop()` 在启动在途时会等待其迟到清理完成（`finally` 复位 `pendingStop`），重复 stop 幂等；新会话在 stop 等待期间启动时不会误清其资源。
5. **两例生命周期测试**：`realtimeVoiceClient.test.ts` 新增「`getUserMedia` 未决时 stop → 迟到媒体流被停轨、不建上下文、无残留」与「stop 后立即重开 → 旧 start 迟到结果不覆盖新会话能力 / 暂停态（旧轨停止、新上下文保留、暂停态不被改写）」；以手动 deferred Promise 驱动，无真实等待。
6. **act 门禁**：`src/test/setup.ts` 包装 `console.error`，仅匹配 `not wrapped in act(...)` 文案并计数，其余日志原样透传；`beforeEach` 经 `context.onTestFinished` 在用例结束（宏任务排空 + afterAll 兜底；onTestFinished 晚于 `afterEach`（含 RTL cleanup））按条数与用例名抛错。

**实测（命令 + 退出码；最终树）**：
- 修复前门禁复现（临时回退 8 处 async act 与页面身份守卫）：`cd frontend && pixi run npx vitest run src/pages/realtime --reporter=default` → 8 failed / 12 passed；失败用例与修复前采集到的 8 条 act 告警逐条对应（capabilities 2 + RealtimeVoicePage 6）。
- `cd frontend && pixi run npx vitest run src/pages/realtime --reporter=default` → 0（21 passed（7+14）；`not wrapped in act` 计数 0、门禁告警 0）。
- `cd frontend && pixi run test` → 0（705 passed；相对修复前 702 增加 3 个用例——2 个采集生命周期 + 1 个能力回读；全量 act 门禁 0 告警）。
- `cd frontend && pixi run coverage` → 0（statements 95.86% / branches 91.87% / functions 95.75% / lines 96.05%，perFile ≥80% 门禁通过）；`pixi run lint` → 0；`pixi run build` → 0（3.31s，初始 chunk 389.80 kB / gzip 123.62 kB）。
- `cd backend && pixi run verify-slice -- --with-seekdb --with-frontend --slice r4-5-whatsapp` → 0（10 步全 PASS：tests-all=3472 / coverage-gates=24 组 ≥80% / lint / typecheck=216 源文件 / gates / tests-seekdb=15 / frontend-test=705 / frontend-coverage=95.86 / frontend-lint / frontend-build=3.28s；证据 `docs/evidence/r4-5-whatsapp-f8403536.json`，canonical sha8=`f8403536`（743 文件级证据）；`5eb1d5ea` 与更早证据均为历史）。
- 无后端改动 / 无新增依赖 / 无新增 env 键 / 无迁移；前端改动文件：`src/pages/realtime/RealtimeVoicePage.tsx`、`src/pages/realtime/RealtimeVoicePage.capabilities.test.tsx`、`src/pages/realtime/RealtimeVoicePage.test.tsx`、`src/lib/realtimeVoiceClient.ts`、`src/lib/realtimeVoiceClient.test.ts`、`src/test/setup.ts`。

### 已完成切片：前端实时语音承载性守卫 + act 门禁宏任务归属 + 清理异常收口（本分支工作区）

**背景**：复审（`bbf0592`）给出 2 P1 / 4 P2：采集层三处承载性检查（worklet 窗口 stop、getUserMedia 窗口 pause、stop 等待在途清理）无红守卫；页面两处会话身份守卫无红守卫；启动失败时清理异常会掩盖 `onError`；`pendingStop` 为死字段且建节点后同步复核不可达；act 门禁仅排空微任务，迟到宏任务告警与文件末条 / afterAll 残留会漏记。本批逐条根修并留守卫 / 变异；旧 canonical `f8403536` 自本批起为历史。

逐条（根修位置 → 守卫 / 变异证据）：

1. **采集承载性守卫（P1-1）**：`MicrophoneCapture` 的 `stop` / `pause` 世代与在途等待语义由三条 deferred 门闩用例锁定（无真实等待）：
   - 「stop 落在 worklet addModule 窗口」——addModule 挂起时 stop、放行后断言停轨 1 / 关上下文 1 / `getCapabilities()===null` / 不建 worklet 节点；删除 `startInternal` 中 worklet 后的过期检查 → 1 failed / 16 skipped。
   - 「pause 落在 getUserMedia 窗口」——媒体流挂起时 pause、放行后断言 `isPaused()===true` / 停轨 1 / 未建上下文；删除 `pause()` 的世代自增 → 1 failed / 16 skipped。
   - 「stop 等待在途启动的迟到清理」——addModule 放行后 context.close 挂起，断言 stop 在 close 放行前未 settle；删除 `stop()` 的 `await inflight.task` → 1 failed / 16 skipped。
2. **页面会话身份守卫（P1-2）**：`RealtimeVoicePage.capabilities.test.tsx` 的 FakeCapture 支持挂起 start / resume，并在 `stop()` 清空 capabilities（与真实采集一致）。两条用例覆盖「停止→重开→旧 ready 的 start 迟到」与「停止→重开→旧 resume 迟到」：旧会话回读不得覆盖新会话能力 / 暂停提示。删除 ready / AEC 公告 / resume 三处身份守卫 → 2 failed / 7 skipped；复原后 9 passed（`src/pages/realtime` 现 23 条 = 9 + 14）。
3. **清理异常不掩盖启动失败（P2-1）**：`startInternal` 的业务失败出口与三处过期清理统一改为 `await this.releaseResources(...).catch(() => undefined)` 后再 `onError`（过期路径同样吞清理异常，避免二次释放）。新增「启动失败 + context.close reject」用例：`start()` resolve、`onError` 一次、关上下文 1 次、停轨 1 次；回退该 `.catch` → 1 failed / 16 skipped。
4. **死字段与不可达复核（P2-2）**：删除 `pendingStop` 字段及全部读写（`isStale` 仅保留世代比较），删除 `createWorkletNode` 后的同步复核（建节点为同步调用，世代不可能在该区间变化）。`stop()` / `pause()` 的世代自增是过期判定的唯一来源，删后语义不变；全量 713 用例保持绿。
5. **act 门禁宏任务归属（P2-3）**：门禁逻辑抽到 `src/test/actWarningGate.ts`；`onTestFinished` 先排空一个宏任务（真实计时 `setTimeout(0)`；`vi.isFakeTimers()` 为真时 `vi.advanceTimersByTime(0)`），再按条数 / 用例名判错并清空归属；`afterAll` 经 `finishRun` 对尝试收尾后的残留按无归属判错，覆盖一个宏任务窗口内与文件收尾 hooks 内的告警；迟到 ≥2 个宏任务的合成告警可能漏记 / 归属到下一条用例（React act 告警在 setState 时同步发出，本套件内够不到）。新增两条门禁单测（收尾抛错并清空、afterAll 残留判错）。
   - 临时探针实测：迟到宏任务告警在有排空时归属到该用例（1 failed / 1 passed）；删除排空后 2 passed（漏记）。afterAll 告警 → `残留告警 1 条（无归属...）` 文件级失败；retry 首轮告警后次轮干净 → 1 passed（首轮告警不记到次轮）。
6. **说明与台账（P2-4）**：`setup.ts` 注释更正为「onTestFinished 在 afterEach（含 RTL cleanup）之后执行，见 vitest 4 生命周期」；上文两处 `src/pages/realtime` 实数按实测改为 21（7+14）。

**实测（命令 + 退出码；最终树）**：
- `cd frontend && pixi run npx vitest run` → 0（713 passed；新增 8 条：采集承载性 3 + 清理 1 + 页面身份 2 + 门禁 2；全量 act 门禁 0 告警）。
- `cd frontend && pixi run npx vitest run --coverage` → 0（statements 95.98% / branches 92.12% / functions 95.5% / lines 96.18%，perFile ≥80% 通过）。
- `cd frontend && pixi run lint` → 0；`pixi run build` → 0（3.24s，初始 chunk 389.80 kB / gzip 123.62 kB）。
- `cd backend && pixi run verify-slice -- --with-seekdb --with-frontend --slice r4-5-whatsapp` → 0（10 步全 PASS：tests-all=3472 / coverage-gates=24 组 ≥80% / lint / typecheck=216 源文件 / gates / tests-seekdb=15 / frontend-test=713 / frontend-coverage=95.98 / frontend-lint / frontend-build=2.94s；证据 `docs/evidence/r4-5-whatsapp-5eb8fd3b.json`，canonical sha8=`5eb8fd3b`（745 文件级证据；`--hash-only` 复算一致）；`f8403536` 与更早证据均为历史；本批 `5eb8fd3b` 自「收尾复审修复」起亦为历史，见下条）。
- 根 `pixi run gates` → 0（comment-gates files=380 violations=0 hints=23 / red-probe 29 探针覆盖 28 文件 / assertion-freeze asserts=9570 raises=708 / prose-pin 75≤75 / 其余门禁同链）。
- 变异清单见逐条第 1-3、5 项；复原后 `git status` 仅本批预期改动。
- 无后端改动 / 无新增依赖 / 无新增 env 键 / 无迁移；前端改动文件：`src/lib/realtimeVoiceClient.ts`、`src/lib/realtimeVoiceClient.test.ts`、`src/pages/realtime/RealtimeVoicePage.capabilities.test.tsx`、`src/test/setup.ts`、`src/test/actWarningGate.ts`、`src/test/actWarningGate.test.ts`、`docs/LEDGER.md`。

### 已完成切片：收尾复审修复（P2-1..P2-4，本分支工作区）

**背景**：对 `8ae5206` 的收尾复审给出 4×P2（无 P0/P1）：三处过期清理 `.catch` 无红守卫；act 门禁「抛出并清空」未被断言；台账 `stop()` 世代自增漏写 `pause()`；门禁覆盖面措辞过宽。本批逐条闭环，旧 canonical `5eb8fd3b` 自本条起为历史。

逐条（根修 / 守卫位置 → 变异实证）：

1. **三处过期清理 `.catch` 红守卫（P2-1）**：`startInternal` 三条过期清理（`realtimeVoiceClient.ts:456/471/490`）各配一条无真实等待的 deferred 门闩用例：
   - worklet addModule 窗口 + `context.close()` reject：`start()` / `stop()` 均 resolve、`onError` 0、停轨 1、close 1、不建节点；删 `:490` 的 `.catch` → 1 failed / 19 passed（首红断言 `stopped()` 由 1→2，`test.ts:602`；`closeCalls` 同为 2，断言在 `:603` 未执行到）。
   - resume 窗口 + `context.close()` reject：同上断言（停轨 1 / close 1 / 不提交能力 / 无暂停误置）；删 `:471` 的 `.catch` → 1 failed / 19 passed（二次释放）。
   - getUserMedia 窗口 + 过期清理停轨抛出：`start()` / `stop()` 均 resolve、停轨调用 1、`onError` 0、不建上下文；删 `:456` 的 `.catch` → 1 failed / 19 passed（停轨由 1→2）。
2. **门禁「抛出并清空」断言（P2-2）**：`actWarningGate.test.ts` 单测 1 在 `finishAttempt` 抛出后追加 `finishRun()` 不抛出（证明 `warnings` 已清空）；删除 `reportOrClear` 的 `this.warnings = []` → 1 failed / 1 passed（残留告警 1 条）。
3. **台账措辞（P2-3）**：`stop()` 的世代自增表述补为 `stop()` / `pause()`。
4. **门禁覆盖面措辞收窄（P2-4）**：`actWarningGate.ts` 头注与台账同步改为「覆盖一个宏任务窗口内与文件收尾 hooks 内的告警；更迟的告警不保证归属」，并注明「迟到 ≥2 个宏任务的合成告警可能漏记 / 归属到下一条用例（React act 告警在 setState 时同步发出，本套件内够不到）」。

**实测（命令 + 退出码；最终树）**：
- 定向：`cd frontend && pixi run npx vitest run src/lib/realtimeVoiceClient.test.ts src/test/actWarningGate.test.ts` → 0（22 passed）。
- 变异：删 `realtimeVoiceClient.ts` 的 `:456` / `:471` / `:490` 任一 `.catch` → 各 1 failed / 19 passed（恢复后 20 passed）；删 `actWarningGate.ts` 的 `this.warnings = []` → 1 failed / 1 passed（恢复后 2 passed）。
- `cd frontend && pixi run npx vitest run` → 0（716 passed；相对 713 增加 3 条过期清理变体；全量 act 门禁 0 告警）。
- `cd frontend && pixi run npx vitest run --coverage` → 0（statements 96.15% / branches 92.18% / functions 95.76% / lines 96.35%，perFile ≥80% 通过）。
- `cd frontend && pixi run lint` → 0；`pixi run build` → 0（3.83s，初始 chunk 389.80 kB / gzip 123.62 kB）。
- `cd backend && pixi run verify-slice -- --with-seekdb --with-frontend --slice r4-5-whatsapp` → 0（10 步全 PASS：tests-all=3472 / coverage-gates=24 / typecheck=216 / tests-seekdb=15 / frontend-test=716 / frontend-coverage=96.15 / frontend-lint / frontend-build=3.08s；证据 `docs/evidence/r4-5-whatsapp-01f95d4e.json`，canonical sha8=`01f95d4e`（745 文件级证据）；`5eb8fd3b` 与更早证据均为历史）。
- 无后端改动 / 无新增依赖 / 无新增 env 键 / 无迁移；前端改动文件：`src/lib/realtimeVoiceClient.test.ts`、`src/test/actWarningGate.ts`、`src/test/actWarningGate.test.ts`、`docs/LEDGER.md`、`docs/evidence/r4-5-whatsapp-01f95d4e.json`、`docs/evidence/logs/r4-5-whatsapp-01f95d4e-*.log`（10 份）。

### 已完成切片：R4-6a 四轮复审修复（1×P2 + 4×P3 逐条根修——本分支工作区）

**背景**：Oracle 对 `2d53687` 的四轮复审无 P0/P1，给 1×P2（F2 负路径三条
变异存活）+ 4×P3。本批逐条根修并补可红守卫；旧 canonical `98cfdadd` 自本条
起为历史。不写「复审通过」——结论由命令 / 退出码 / 变异支撑。

1. **P2-1 F2 负路径无守卫**（`docker/wechat-gui/poc/run_stack_poc.sh` +
   `src/tests/unit/test_run_stack_poc.py`）：
   a) udevadm 属性查询非零退出：`_ignored_evidence_rc` 扩 `udev_exit` 参数，
   新增用例 `test_udev_property_query_failure_is_not_positive_evidence`
   （假 udevadm exit 1 → 断言 `rc=1`）；
   b) 证据不足（`host_device_ignored_evidence` 返回 1）配 ABSENT 打印器：新增
   `test_float_until_settled_rejects_insufficient_udev_evidence`（断言
   `rc=1 floated=0 ignored=0 basis=`）；
   c) 首个 `--name-only` 列举失败：新增
   `test_float_probe_first_listing_failure_is_not_absence`
   （`_write_fake_xinput(name_only=None, list_lines=[...])`，断言探针 `rc=3`、
   `stdout==""`）。
2. **P3-1 部分残留不拒绝**（`run_stack_poc.sh:145-160`）：
   `reject_residual_host_devices` 改为逐 `DEVICE_NAMES` 调
   `host_device_present`，任一存在即 exit 1（原 `host_devices_present` 要求两台
   都在，单台残留会放行）。守卫
   `test_reject_residual_devices_exits_before_create` 改用
   `_make_sysfs(tmp_path, [KEYBOARD])` 走真实函数（原先整体打桩，ALL / ANY
   差异不可见）。
3. **P3-2 SAFE 两轮无守卫**（`run_stack_poc.sh:325`）：新增
   `test_float_until_settled_requires_two_safe_rounds`（首轮 SAFE、次轮起
   RETRY → `rc=1 floated=0 ignored=0 basis=`；计数器用文件，零真实等待）。
4. **P3-3 正证据只查首个同名节点**（`run_stack_poc.sh:196`）：新增
   `test_udev_evidence_checks_every_same_name_node`
   （`_make_sysfs(tmp_path, [KEYBOARD, KEYBOARD])`，第二个节点带 `ID_INPUT`
   → 断言 `rc=1`）。
5. **P3-4 文档漂移**：README 门报告字段补 `host_displays` / `ignored_basis`
   （与 `write_run_metadata` 的 10 字段一致）；Wayland 口径改为「先
   `XDG_RUNTIME_DIR`，再扫 `/run/user/*/wayland-*`（非 root 调用者看不到其他
   用户的 0700 目录）」；放行前置改为「逐会话连接」；忽略判定改为正证据表述
   （`udevadm info` 的 `LIBINPUT_IGNORE_DEVICE=1` 且无 `ID_INPUT*`，无 udevadm
   才回落启发式）；`docs/backend/channels.md` 字段表同步。

**变异矩阵（副本备份注入后恢复，`cmp` 字节一致；每条只红对应用例）**：

| # | 变异 | 结果 |
|---|---|---|
| M1 | `run_stack_poc.sh:189` `\|\| return 1`→`\|\| return 0` | 1 failed / 22 passed；仅 `test_udev_property_query_failure_is_not_positive_evidence` 红 |
| M2 | `:336` case `0)`→`0\|1)` | 1 failed / 22 passed；仅 `test_float_until_settled_rejects_insufficient_udev_evidence` 红 |
| M3 | `:211` 删除 `\|\| exit 3` | 1 failed / 22 passed；仅 `test_float_probe_first_listing_failure_is_not_absence` 红 |
| M4 | 残留判定改回 `host_devices_present`（ALL） | 1 failed / 22 passed；仅 `test_reject_residual_devices_exits_before_create` 红 |
| M5 | `:325` SAFE `-ge 2`→`-ge 1` | 1 failed / 22 passed；仅 `test_float_until_settled_requires_two_safe_rounds` 红 |
| M6 | `:196` 加 `\| head -n 1` | 1 failed / 22 passed；仅 `test_udev_evidence_checks_every_same_name_node` 红 |
| M7 | `:326` ABSENT `-ge 2`→`-ge 1`（既有两轮守卫复验） | 1 failed / 22 passed；仅 `test_float_until_settled_requires_two_absent_rounds` 红 |

每条恢复后 23 passed（本文件全量）；全树 119 passed。

**命令 + 退出码（最终树）**：
- `cd docker/wechat-gui && pixi run tests-unit` → 0（119 passed；总覆盖率
  93.80%）；`pixi run coverage-gates` → 0（6 组 ≥80：poc_gate 89.2 /
  uinput_devices 96.1 / uinputd 96.4 / 其余 100）；`pixi run lint` → 0；
  `pixi run typecheck` → 0（6 源文件）。
- `WECHAT_GUI_POC_ALLOW_HOST_SESSION=1 bash docker/wechat-gui/poc/run_stack_poc.sh`
  → 0（privileged PASS 7/7；`ignored_devices` 两台、
  `ignored_basis=udev_positive`、`floated_devices=[]`、
  `residual_devices=[]`、`host_displays=[":0"]`、`float_sync_ms=1342`；证据
  `docs/evidence/poc/wechat-gui-stack-LeoWorkLaptop-privileged-20261126T070135Z.json`）。
- `WECHAT_GUI_POC_RUN_MODE=unprivileged WECHAT_GUI_POC_ALLOW_HOST_SESSION=1
  bash docker/wechat-gui/poc/run_stack_poc.sh` → 0（PASS 7/7；同
  `ignored_basis` / 空 floated / 空 residual、`float_sync_ms=1313`；证据
  `...-unprivileged-20261126T070141Z.json`）。
- `cd backend && VITEST_MAX_WORKERS=6 pixi run verify-slice --
  --with-seekdb --with-frontend --slice r4-6a` → 0（10 步全 PASS：
  tests-all=3472 / coverage-gates=24 / lint / typecheck=216 / gates /
  tests-seekdb=15 / frontend-test=716 / frontend-coverage=96.15 /
  frontend-lint / frontend-build=2.55；canonical
  `docs/evidence/r4-6a-6488ca04.json`，sha8=`6488ca04`，758 文件级证据；
  `--hash-only` 复算 `sha8=6488ca04 files=758` 一致；`98cfdadd` 及更早证据
  为历史）。

**偏离与未验证**：
- README「本轮证据」在 PoC 复跑后指向本次 `070135Z` / `070141Z`；任务给定的
  `054924Z` / `054931Z` 作为「三轮最终树」一行保留在表内——复跑生成新时间戳，
  若只写旧戳会再次漂移。
- 多 X 会话遍历 / Wayland `/run/user/*` 回落仍只经临时 socket 根单测（本机仅
  `:0`、无 Wayland 会话）；真实多会话桌面未复跑。
- 宿主已装 udev 规则，本轮两次 PoC 均 `floated_devices=[]`
  （`ignored_basis=udev_positive`），真实 float 路径以历史证据（规则安装前）
  与 hermetic 用例覆盖。
- 部署机 udev 复验与微信客户端 Live 仍随统一阶段。

### 已完成切片：R4-6b GUI 微信实验轨通道（沙盒 agent + 后端通道，本分支工作区）

**背景**：R4-6a 的 Xorg + uinput PoC 门已通过并合并（PR #45 → `b065bf4`）。
本片按「生产主轨仍为企微；GUI 微信为实验轨、默认关闭、Live 推迟」落地：
沙盒树新增仅回环 HTTP agent（Bearer 鉴权 + 五工具），后端新增
`channels/wechat_gui/` 通道（wire 镜像 / 设置 / 提示词 Unit / 客户端 /
视觉解析 / 轮询 runner / 出站适配器）并接入 `ChannelType` / 配置四表面 /
lifespan 装配。

**改动面（文件）**：
- 沙盒树：`docker/wechat-gui/src/wechat_gui_poc/wire_models.py`（新增 agent
  HTTP 契约 + 五工具参数模型，三 wire 模型与后端文本镜像）、`agent.py`
  （stdlib `ThreadingHTTPServer`；修复 B 已改 asyncio；`/health` 无鉴权；
  `/invoke` Bearer 恒时比较；五工具；open_chat 有界等待；恒 200 `ok=false`
  归一）、`uinputd.py`
  （同步控制面客户端 + 共用响应解码）、`entrypoint.sh`（Xorg 就绪后以运行
  用户启动 agent、token 文件解析）、`Dockerfile`（HEALTHCHECK 改查
  `/health`）、`docker/docker-compose.wechat-gui.yml`（8090 回环映射 +
  agent token secret）、`agent.token.default`、README、单测
  `test_agent.py` / `test_entrypoint_password.py` 增补。
- 后端：`channels/wechat_gui/{wechat_gui_wire_models,wechat_gui_models,
  wechat_gui_prompts,wechat_gui_client,wechat_gui_reader,wechat_gui_runner,
  wechat_gui_adapter}.py`；`channel_models.py`（`ChannelType.WECHAT_GUI`）；
  `config/models.py`（`WechatGuiSettings` 聚合）；`app/main.py`（lifespan
  装配 / 停机 / 默认关闭零对象）；`scripts/generate_examples_env.py` /
  `config/wizard_generators.py` / `src/conftest.py` / `.env.example` /
  `docs/backend/config.md`；`scripts/check_sandbox_dtos_drift.py`（纳入
  wechat-gui 镜像对，块起点支持无括号 dataclass）；`backend/red_manifest.toml`
  （7 条新探针）；单测 `test_wechat_gui_*` / `test_app_wechat_gui_channels_lifespan`
  / `tests/wechat_gui_fakes.py` / `test_channels_models.py` 值域同步。
- 文档：`docs/backend/channels.md`「GUI 微信实验轨」整节改实现态；
  `docker/wechat-gui/README.md` agent 面 / 安全边界 / 测试面同步。

**五工具 → uinputd / 子进程映射**：`screenshot` = `import -window root
png:-`（base64 + sha256）；`click{x,y}` = `move` + `click`（左键）经
uinputd 控制面；`type_text{text}` = `xclip -selection clipboard -in` +
`ctrl+v` 序列（覆盖中文）；`press_key{key}` = 键名 / 组合键解析后的
`key` 序列（按下 → 抬起逆序）；`open_chat{remark_name}` = `ctrl+f` → 粘贴
备注名 → `enter` → 有界等待标题区域变化 → 标题区域 + 全屏截图。

**标题校验与 fail-closed 用例**：入站解析标题与 open_chat 目标不一致默认
`skip` 并计数（`title_mismatch_action=fail` 时触发退避）；出站 open_chat
后先视觉校验标题，不一致拒绝发送；解析非 JSON / 缺字段 / 置信度低于阈值
一律 `LLM_CALL_FAILED`；客户端把恒 200 `ok=false` / 401 / 超时 / wire 破坏
归一 `CHANNEL_API_ERROR`；token 缺失时装配跳过且 invoke 短路失败。

**命令 + 退出码（`128821d` 树）**：
- `cd docker/wechat-gui && pixi run tests-unit` → 0（141 passed；总覆盖率
  94.08%；agent 95%、wire_models 100%）；`pixi run lint` → 0；
  `pixi run typecheck` → 0（7 源文件）。
- `cd backend && pixi run pytest src/tests/unit -k "wechat_gui or channel_type"
  --asyncio-mode=auto` → 0（58 passed）。
- `cd backend && pixi run mypy src/packages/harness/channels/wechat_gui
  src/app/main.py src/packages/harness/config/models.py src/tests/wechat_gui_fakes.py`
  → 0（10 源文件）。
- `cd backend && pixi run python ../scripts/check_sandbox_dtos_drift.py` → 0
  （全部 53 项契约一致，含 wechat-gui 三 wire 模型 + 五结果模型 + 七常量行）。
- `cd backend && pixi run red-probe --all` → 0（36 条探针全部 RED，含新增
  7 条）、`pixi run red-probe --gate` → 0（190 <= 基线 190）。
- `cd backend && pixi run docs-truth` → 0；`check_doc_counts.py` → 0（13 项）；
  `conftest_env_check.py` → 0（登记 239 键 / 硬覆盖 239）；
  `check_chinese_comments.py --tree backend` → 0（violations=0）、
  `--tree wechat-gui` → 0（violations=0）。
- 棘轮面：`assertion-freeze --update` → 0（基线 233 文件 / asserts=9763 /
  raises=744；新增测试文件并入基线）后 `assertion-freeze --enforce` → 0；
  `surface-check` → 0（env 键面 827 行）；`sync-test-ratchet` → 0（+0）；
  `noqa-freeze` → 0；`prose-pin` → 0（prose 75 <= 基线 75）。

**偏离与未验证**：
- **Live 未验证**：镜像不装微信客户端——真实截图解析准确率、`open_chat`
  命中率、中文输入法 / 字体 / 窗口管理器行为均待统一 Live 阶段；本片只测
  到假数据层（fake 沙盒客户端 / fake 视觉网关 / 本机回环 MockTransport）。
- 部署机宿主 udev 规则仍待复验（开发机已验；Wayland 会话依赖规则或 VM
  隔离）。
- `verify-slice --with-seekdb --with-frontend --slice r4-6b` → PASS（tests-all
  3529 / coverage 24 组 / mypy 223 / SeekDB 15 / 前端 716 + 96.15；证据
  `r4-6b-a6ca265f`）；提交 `128821d`。
- 群聊按单会话处理（`is_group_chat=False`）；出站只支持文本；GUI 侧无平台
  消息回执 ID（`message_ids` 恒空）。

**Oracle 深审结论（提交 `128821d`；不通过——无 P0，P1×5 + P2×4；仅记录未修）**
回归面全绿（tests-all 3529 / coverage 24 组 / mypy 223 / SeekDB 15 / 前端 716+96.15；
沙盒 141 单测；漂移门 41 项一致；红探针 36 条全 RED；三条指定变异均成立）。
源码隔离与 P4 比对（strip 后全等、空标题拒绝、skip/fail 强校验）成立。

P1（入站/出站主链可复现逻辑缺陷）：
1. `wechat_gui_runner.py:315-317`：截图哈希在 `reader.read` 成功**之前**登记，视觉解析瞬时失败后下一轮命中「截图未变化」直接返回——同一画面永久不再解析（复现：首轮 `LLM_CALL_FAILED` 后 `reader.calls=1 / inbound=[]`）。修复：sha 登记移到解析成功之后 + 补「首轮失败次轮成功」用例。
2. `wechat_gui_runner.py:305-341`：`_current_target` 只赋值不复位；标题不一致后单联系人配置下轮询永久空转（复现：`open_calls=['小林']` 后无解析）。触发源：noVNC 人工操作、向非轮询联系人出站、容器重启。修复：skip/fail 两分支均置 `_current_target=None`。
3. `wechat_gui_adapter.py:82-95` × `wechat_gui_runner.py:305-317`：单窗口无互斥，出站「校验→type_text→enter」与 runner 的 `open_chat`/`screenshot` 之间可被轮询切走会话——校验通过后文本与回车落进新会话并返回成功（复现：发给小林的消息 `sent_in=['阿青']`）；agent 侧 `agent.py:738` 每连接一线程同样无串行化。修复：装配处建一把 `asyncio.Lock` 注入 runner（仅 open_chat/截图/解析/校验段持锁，`handle_inbound` 在锁外）与 adapter（全程持锁）。
4. `wechat_gui_models.py:95-96,499-500` + `wechat_gui_prompts.py:44-59`：去重键依赖 VLM 输出的 epoch 毫秒，但提示词不给当前时间参照（微信界面仅 `HH:MM`）——`ts_ms=0` 回落采集分桶时跨 5 分钟边界旧消息再次入站，幻觉值漂移时每次画面变化重复（复现均得 `['你好','你好']`）；首帧把全部可见消息当新消息投递；`is_self` 缺省 False、`messages` 缺省空与文档「缺字段一律 LLM_CALL_FAILED」不符。**需拍板**：甲 会话水位重叠比对（首帧只建基线，改动中等）/ 乙 注入当前时间 + 校验 ts 合理性（改动小，读不出时照旧）/ 丙 仅首帧基线（最小，跨边界重复仍在）。无论选哪个：`is_self`/`text`/`sender`/`ts_ms`/`messages` 改必填。
5. 硬约束 4.1/4.3 未记录偏差：`agent.py` 用 `ThreadingHTTPServer` + 阻塞 `subprocess.run` + 新增同步控制面客户端，而同树 `uinputd.py`/`poc_gate.py` 已是 asyncio；台账/README 只陈述事实未写为何不能 async。**需拍板**：甲 改 `asyncio.start_server` + 既有 `UinputControlClient` + `create_subprocess_exec`（符合约束，删重复客户端，约 300 行重写）/ 乙 保留同步、台账「偏离」段写明理由并经用户确认 / 丙 仅 HTTP 面换 asyncio、工具执行仍下沉线程。

P2：
6. `agent.py:694,653-657`：`hmac.compare_digest` 直接比较 str（sidecar 比较 UTF-8 bytes），Bearer 含非 ASCII 抛 `TypeError`、深层嵌套 JSON 抛 `RecursionError`，均未捕获、连接无响应断连（前者无需正确 token 即可触发）。修复：比较前 `.encode("utf-8")`；except 增补 `RecursionError`；补两条用例。
7. `wire_models.py:834-885` × `agent.py:668-685`：沙盒 `InvokeRequestModel`/`InvokeResponseModel` 生产零引用，handler 手拼裸 dict 响应——漂移门看护的不是实际 wire 出口；工具名/路径常量在后端 `wechat_gui_client.py:50-65` 另有一份字符串镜像、五工具结果字段只在两树逻辑代码里，均未入门禁（约束 5/18）。修复：handler 改用 `InvokeResponseModel(...).to_wire()`；五工具结果补 Model Class；工具名与路径常量行入漂移门。
8. 死代码（约束 2.2/15.1）：`uinputd.py:650-683` `SyncUinputControlClient.emit_key/emit_move/emit_click`（零引用、覆盖 0）、`wire_models.py:632` `WECHAT_GUI_AGENT_TOOLS`、`wechat_gui_runner.py:395` `WECHAT_GUI_CHANNEL`、`wechat_gui_client.py:189` `probe_health` 与 `:307` `click`（仅测试用）、`wechat_gui_runner.py:57` 把导入常量列入 `__all__`（re-export）。
9. 文档与代码不符（约束 7/15.4）：`docker/wechat-gui/README.md:386-394` 仍写「门阶段不产生 wire 契约、不接后端」且承诺的群聊区分/重名消歧/键迁移未实现；`README.md:262-268` compose 生产启动未导出 `WECHAT_GUI_AGENT_TOKEN_FILE_SECRET`（照此操作 agent 用仓库公开占位 token），全文未说明 `WECHAT_GUI_AGENT_TOKEN` 明文 env 优先于文件；`channels.md:108-109,113` 缺省 no-op 与停机链漏 GUI 微信、`:1287`「缺字段」过誉、`:1344` 联系人 JSON 无示例无字段表、`:1255` 语义不通；`LEDGER.md:102` main 仍写 `215317c`、`:193` 漂移门写 38 项（实为 41）、`:2578` 沙盒覆盖 93.96%（实测 94.08%）、`:2605-2606` 称全量门禁「由主 Agent 执行」但同提交已含 `r4-6b-a6ca265f` 证据且未记提交号。

Live 前风险（Live 阶段验证）：`open_chat` 在 Ctrl+F 后立即粘贴再回车无稳定等待（焦点未切到搜索框时备注名会被当作消息发进当前会话，后端标题校验拦不住）；标题区域取顶部 140px 首次像素变化而搜索框回显的正是备注名（VLM 可能把搜索框文字读成标题）；`is_self` 全靠 VLM 判定，误判即自回声。

流程观察：任务书 A1–D17 原文未落仓库，Oracle 无法逐字出具覆盖表——后续任务书要点应写入台账或 `docs/plans/`。
修复顺序：P1 1/2（各一处改动 + 一条用例）→ P1 3 共享锁 → P1 4 待拍板后改（提示词变更需上调 byte-lock 版本）→ P1 5 待拍板 → P2 6/7/8 一起收 → P2 9 文档对齐 + 台账补 verify-slice 记录与提交号 → 重跑 `verify-slice` 出新证据后再推送。复现用例与已验证补丁副本在 `/tmp/opencode/audit/patched_copy/`（仓库外）。

**修复 A（Oracle 深审 P1-1 / P1-2 / P1-3 / P1-4 与 P2-8 / P2-9 根修；只记事实，不复审结论）**

- 入站截图：`_last_screenshot_sha` 仅在视觉解析成功且标题校验通过后登记；
  瞬时 `LLM_CALL_FAILED` 不登记，同画面下一轮重新解析。标题不一致（skip /
  fail 两分支）复位共享窗口目标并丢弃未接受画面的 sha，下一轮重新
  `open_chat`。
- 单窗口互斥：装配处建一把 `asyncio.Lock` 与一份 `WechatGuiWindowState`，
  同实例注入 runner 与出站适配器；runner 仅页面读写段持锁，
  `handle_inbound` 在锁外；适配器「校验 → type_text → enter」全程持锁。
- 去重改为**会话水位**（新 Unit `wechat_gui_watermark.py`）：尾部重叠最大 k
  + 末次出现定位；首帧 / 目标首次出现 / 无匹配 / 超出保留长度只建基线；
  基线长度 = `WECHAT_GUI_MAX_MESSAGES_PER_POLL`；水位在投递尝试之后落基线
  （失败不回退）。删除时间桶常量、`DedupWindow` 与旧去重键用例；
  `VlmReadResultModel.messages` 与每条消息字段改必填。
- 提示词：时间字段改为「尽力读取界面相对时间，读不出填 0」，`ts_ms` 仍为
  整数；`WECHAT_GUI_PROMPTS_VERSION` / 任务书版本 1.0.0 → 1.1.0，byte-lock
  摘要同步更新（仅任务书模板变更）。
- 死代码 / 接线：删 `WECHAT_GUI_CHANNEL`、`__all__` re-export、客户端
  `click` 方法与 `WECHAT_GUI_TOOL_CLICK`；`probe_health` 接入装配期
  `/health` 探测（不可达记录并跳过，不抛出）。
- 命令 + 退出码：`cd backend && pixi run pytest
  src/tests/unit/test_wechat_gui_*.py
  src/tests/unit/test_app_wechat_gui_channels_lifespan.py --asyncio-mode=auto`
  → 0（76 passed）；`pixi run mypy src/packages/harness/channels/wechat_gui/
  src/app/main.py` → 0（9 source files）；`pixi run ruff check`（同面 + 测试
  基建）→ 0。
- 变异矩阵（逐条守卫实测红）：sha 登记移回解析前 → 2 红；标题不一致不复位
  目标 → 2 红；runner 不共享锁 → 2 红；适配器不共享锁 → 2 红；水位「末次
  出现」改「首次出现」→ 1 红；移除超出保留长度重置 → 1 红；移除装配期
  `/health` 探测跳过 → 1 红。

**修复 B（Oracle 深审 P1-5 / P2-6 / P2-7 / P2-8 / P2-9 的沙盒与跨树部分根修；只记事实，不复审结论）**

- P1-5（方案甲，全 asyncio）：`docker/wechat-gui/src/wechat_gui_poc/agent.py`
  整体改写——`asyncio.start_server` + 手写最小 HTTP/1.1 解析（请求行 /
  请求头 / 有界 `Content-Length` / 每连接一请求、`Connection: close`）；
  解析器拆为纯函数 `parse_http_request(bytes) → ParsedHttpRequest`；
  工具执行复用既有异步 `UinputControlClient`，截图 / 剪贴板改
  `asyncio.create_subprocess_exec`（argv、无 shell、有界超时），
  `open_chat` 有界等待改 `asyncio.sleep`；`/invoke` 工具执行由一把
  `asyncio.Lock` 串行化（`/health` 不占锁）；CLI 改 `asyncio.run`。
  `uinputd.py` 删除 `SyncUinputControlClient` 及其 `emit_*`。
- P2-6：Bearer 比较前转 UTF-8 字节（非 ASCII 不再 `TypeError`）；`json.loads`
  的 except 增补 `RecursionError`（深层嵌套 → 422）；handler 顶层兜底任何
  未预期异常 → 受控 500 JSON（不再无响应断连）。守卫：非 ASCII Bearer →
  401；深层嵌套 → 422；工具内未预期异常 → 恒 200 `ok=false`；handler
  顶层异常 → 500。
- P2-7：沙盒 `wire_models.py` 新增
  `ScreenshotResultModel` / `ClickResultModel` / `TypeTextResultModel` /
  `PressKeyResultModel` / `OpenChatResultModel`（`OpenChatResultModel` 含
  `title_changed`：截止内未变化为 false，后端仍以视觉标题校验 fail-closed
  落定）与 `WECHAT_GUI_HEALTH_PATH` / `WECHAT_GUI_INVOKE_PATH` /
  `WECHAT_GUI_TOOL_*` 模块级 `Final` 常量；handler 改用
  `InvokeRequestModel.from_wire` 解码与 `InvokeResponseModel(...).to_wire()`
  序列化。后端 `wechat_gui_wire_models.py` 逐字镜像五结果模型与七常量行；
  `wechat_gui_client.py` 删除本地字符串镜像、改引用镜像常量并用镜像
  Result Model 解码 `result`（字段缺失 / 类型错 → `CHANNEL_API_ERROR`）。
  漂移门 `scripts/check_sandbox_dtos_drift.py` 纳入结果模型块与常量行，
  实跑 53 项一致（台账 `:193` 已同步）。
- P2-8：删除 `wire_models.py` 的 `WECHAT_GUI_AGENT_TOOLS`（五工具名常量
  已替代其作用）；随 P1-5 删除同步控制面客户端。
- P2-9：`docker/wechat-gui/README.md` 会话键节改实现态（指向
  `docs/backend/channels.md`，如实写群聊单会话 / 群聊个人区分 / 重名消歧 /
  备注变更键迁移未实现）；compose 生产启动补
  `WECHAT_GUI_AGENT_TOKEN_FILE_SECRET` 导出步骤与「明文 env 优先于文件」
  说明；agent 节补 async 架构、401/404/413/422/500、`title_changed` 与超时
  语义、剪贴板粘贴后不清空与 x11vnc clipboard 同步面；删同步客户端描述；
  测试面表同步。`docs/backend/channels.md` 沙盒相关行（agent 面、结果字段、
  常量来源、测试面）同步。
- 命令 + 退出码：`cd docker/wechat-gui && pixi run tests-unit` → 0（150
  passed；总覆盖率 92.72%；agent 93%、wire_models 92%）；`pixi run lint`
  → 0；`pixi run typecheck` → 0（7 源文件）。`cd backend && pixi run pytest
  src/tests/unit/test_wechat_gui_wire_models.py
  src/tests/unit/test_wechat_gui_client.py
  src/tests/unit/test_wechat_gui_adapter.py --asyncio-mode=auto` → 0（22
  passed）；`pixi run python ../scripts/check_sandbox_dtos_drift.py` → 0
  （全部 53 项契约一致）；改动的后端 wire / client / 两测试文件
  `pixi run mypy` → 0、`pixi run ruff check` → 0。
- 变异矩阵（逐条守卫实测红）：移除 `/invoke` 锁 → 串行化用例 1 红；Bearer
  改回 str 比较 → 非 ASCII 用例 1 红；移除 `RecursionError` → 深层嵌套
  用例 1 红（500）；移除 handler 顶层兜底 → 受控 500 用例 1 红（断连）；
  移除 413 上限 → 解析矩阵 1 红；沙盒 `ScreenshotResultModel.width` 改
  `float` → 漂移门 exit 1；后端 `title_changed` 缺省化 → 客户端用例 1 红；
  恢复 `WECHAT_GUI_AGENT_TOOLS` → 死代码用例 1 红。
- 未动修复 A 的后端文件：`wechat_gui_runner.py` / `wechat_gui_adapter.py` /
  `wechat_gui_models.py` / `wechat_gui_prompts.py` / `wechat_gui_reader.py` /
  `wechat_gui_watermark.py` / `app/main.py`。

**最终树验证（`36e3449d` 树）**

- 命令 + 退出码：`cd backend && VITEST_MAX_WORKERS=6 pixi run verify-slice
  -- --with-seekdb --with-frontend --slice r4-6b-fix` → 0，10/10 PASS：
  tests-all=3551 / coverage-gates=24 组 / lint / typecheck=224 / gates /
  tests-seekdb=15 / frontend-test=716 / frontend-coverage=96.15 /
  frontend-lint / frontend-build；证据
  `docs/evidence/r4-6b-fix-36e3449d.json`（sha8=`36e3449d`，779 文件）。
- 沙盒 150 passed / 92.72%；漂移门 53 项一致；红探针 36→37（新增
  `wechat-gui-watermark-removal`）；断言基线 233/9763/744 → 235/9832/754
  （并入 `test_wechat_gui_watermark.py` 与 `test_wechat_gui_window_mutex.py`）。


**修复 C 最终树验证（`162f863c` 树，提交 `e1db542`）**：`cd backend && VITEST_MAX_WORKERS=6 pixi run verify-slice -- --with-seekdb --with-frontend --slice r4-6b-fix` → 0，10/10 PASS：tests-all=3555 / coverage-gates=24 组 / lint / typecheck=224 / gates / tests-seekdb=15 / frontend-test=716 / frontend-coverage=96.15 / frontend-lint / frontend-build；证据 `docs/evidence/r4-6b-fix-162f863c.json`（sha8=`162f863c`，779 文件）；沙盒 151 passed / 92.74%；同步测试棘轮通过；断言基线未变（新增断言不拦截）。上一行「最终树验证」记录的是修复 C 之前的 `36e3449d` 树，现为历史。

## 6. 遗留与部署台账（非阻塞）

### 缓解台账（非根修，已排期）

| 项 | 现状 | 为何暂不根修 | 根修方案 + 目标批次 |
|---|---|---|---|
| 流式路径合规守卫不重试 | `stream_reply` 已逐块交付消费方，助手腔检测命中时不重试（只覆盖非流式 `reply`） | 流式输出无法回撤已发送块；重生成会造成可见重复 | 有界首段缓冲（首句成稿前完成检测）或在收尾时追加一次角色提醒纠偏；目标 R5-1 |
| 实时语音多段转写完成序 | 多段转写可并行，最终稿按**转写完成序**进入待生成通道；客户端靠 `segment_id` 对齐 | 会话通道按到达序串行消费，暂无跨段排序状态；改动触及实时轮次状态机 | 携带段序并在生成入口做有界重排（超时即按完成序放行）；目标 R5-1 |
| 声纹不可解析容器回落声明值 | MP3 / OGG / WebM / MP4 等容器在零依赖探测面（`voiceprint_audio.py`）不解析，时长 / 采样率沿用上传方声明值（`source=declared`）；WAV / 裸 PCM 已实测 | 当前无容器解析依赖；引入探测库或元数据读取会扩大依赖面，且语音样本主形态为 WAV / PCM | 容器头探测（MP3 frame header / OGG page / ISO-BMFF box）或供应商元数据字段；目标 R5-2 |
| TG 429 `retry_after` 夹取 1..30s 单次重试 | 出站 429 按 `parameters.retry_after` 夹取到 1..30s 后单次重试，其余错误不重试 | 单次重试是成本 / 时延的可预测边界；持续限流需上层退避与再入队 | 可取消的预算内退避 + 失败消息再入队（不阻塞 send 调用）；目标 R5-1 |
| 注释门对测试面豁免 | `check_chinese_comments.py` 整树豁免 `tests/` 与 `test_*.py` | 测试 docstring 以用例名 / 行为说明为主，强制 Google 段落会显著增噪 | 对测试 helper 采用放宽策略（仅要求中文说明，不强制 Args/Returns）；目标 R5-1 |
| SmsForwarder 通知发送者归一碰撞面 | 仅点位置不同的包名共用会话（`com.example.app` / `com.exampleapp` → 同一 `sms:` 会话键） | 归一规则未区分包名点号语义；收紧需产品决定点号是否参与会话身份 | 点分段归一或保留原始包名作为会话键的一部分；目标 R5-3（需产品决定点语义） |
| 流式 ASR 起始边界 | 流式通道从 SPEECH_START 帧起喂，VAD 防抖候选帧（默认 3 帧）仅入批量缓冲，流式最终稿可能缺句首约 200ms；`test_realtime_session_streaming.py` 的 `frames[1:]` 已固化该行为 | 候选帧在起说确认前不进流式缓冲，避免误触发占用连接；改起点需重排候选帧与握手时序 | 把 VAD 候选帧纳入流式起点（起说确认后回放候选帧）并更新固化用例；目标 R5-1 |
| 梦境 `stats` / `decisions` JSON 动态键 | `ff_dream_runs.stats` / `decisions` 为 JSON 列，写入 `DreamRunStatsModel` / `DreamDecisionModel` 的 dump 形态，列类型仍是 `dict` / `list` | 无当前缺陷；转 Pydantic 列需 TypeDecorator + 迁移，成本高于本批收益 | **已记录例外**（不排期）；随 memory 域 schema 批次转模型时一并迁移 |

- Clevo 部署窗口：模型 lfs 拉取（~8-10GB）、compose 起 llama-server×2/CosyVoice/Qwen3-ASR 包装面、TTS/ASR 服务端点配置、GUI 微信 PoC 门：本机栈 PoC PASS（证据 `docs/evidence/poc/wechat-gui-stack-LeoWorkLaptop-unprivileged-20261126T070141Z.json`，7/7 步、204ms；特权形态同目录 `...-privileged-20261126T070135Z.json`）；宿主 udev 规则（仓库参考件 `docker/wechat-gui/udev/90-wechat-gui-ignore.rules`）已在本机验证——X11 + Xorg libinput，event24/event25 均 `LIBINPUT_IGNORE_DEVICE=1`、无 `ID_INPUT*`、宿主 xinput 无输出；**部署机仍须复验**；Wayland 会话 float 不可行，只能依赖该规则或 VM 隔离；部署机复跑 + 微信客户端 Live 随统一阶段（Xorg+uinput 栈验证已过）、抖音 web 真网一次性验证（sidecar profile 登录抖音 + allowlist 加 douyin.com + 采集/发送脚本命中确认——单元测试覆盖选择器之外的全部语义）、B站真网复测（hdslb 字幕后缀已修；评论端点已切 /x/v2/reply）、SeekDB 生产实例、QQ NapCat 部署与令牌、ADMIN_AUTH_TOKEN 必设。
- SmsForwarder 短信 / 通知转发通道已交付（默认关闭、仅入站；部署侧配置 `SMS_FORWARDER_*` 并在手机端配置 Webhook 与 token）；WhatsApp Cloud API 通道已交付（`WHATSAPP_ENABLED` 默认关闭；Meta 应用侧配置 webhook / 订阅 messages + 四项凭据；出站仅自由格式文本并受 24h 客户服务窗约束——窗外主动消息需模板发送（未实现）或属主先发消息；窗口状态与 wamid 去重均为进程内单实例语义）。
- 声纹（PIPL 合规）与表情包素材法律边界提示义务已随 R2d-2 交付（默认关闭，部署侧按需配置 `VOICEPRINT_*` / `STICKER_*` 并起说话人向量服务）。
- 预算边界（R3-2 → R4-2 已分面）：LLM 日预算只计 LLM 用途行；ASR / TTS / voiceprint 各自独立预算（按 purpose 计数，见 [usage.md](backend/usage.md#分面日预算asr--tts--voiceprint)）。
- 实时语音（R2f）遗留：AEC/移动端已由 R4-3 前置（浏览器 AEC/Worklet），残余：真实设备验证、真流式供应商连通验证（双轨默认关闭；步骤见 docs/backend/realtime-voice.md「部署侧一次性真机验证」）；实时语音轮次不注入工具面（需要搜索/沙盒走文字通道）。
- 已知部署机注意：开发机无 NVIDIA GPU（Iris Xe）；GPU/模型项一律在 Clevo 机执行。

## 7. 恢复指引（新会话首读）

1. 读本台账第 3/4/5 节确认分支与拍板；
2. `cd` 工程 → `git status`/`git log --oneline -5` 核对与台账一致；
3. 若上下文中的「当前进行中」段存在未提交工作 → 先按第 5 节剩余步骤收口再开新切片；
4. 任何切片开工前：确认无重型子 Agent 在跑（串行纪律）；任务书必须包含硬约束全文、信息卫生要求（禁止影射其他工程/仓库作为约定来源）、15.2-15.4 注释口径、真引擎验证要求（若涉迁移/并发/唯一键）、范围清单（禁 git、禁范围外文件、禁子代理）。
5. 完成后更新本台账对应状态段并提交。

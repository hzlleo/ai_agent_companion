# AI 陪伴伙伴 · 管理前端

人格驱动 AI 陪伴伙伴（ai_agent_companion）的管理台前端：独立 Pixi 项目
（Node.js 22 + npm），技术栈为 Vite 6 + React 19 + TypeScript（strict）+
Tailwind CSS 4 + React Query 5 + React Router 7。

当前模块：

- **人格管理（personas）**——对接后端 `/api/v1/personas` 全部 13 个端点（游标
  分页列表 / 创建 / 读取 / 部分更新 / 删除 / 复制 / 激活 / 停用 / 卡片读取 /
  卡片整体替换 / 状态层读取 / 状态层 upsert / 批量删除）；
- **记忆管理（memories）**——对接 `/api/v1/memories` 全部 16 个端点，页签式
  操作面：情景（事件日志 + 手工注入 + 批量删除部分成功）/ 策展（supersede
  语义 upsert + 历史链）/ 会话（详情 + 删除）/ 检索（dense / keyword / hybrid
  三模式，回退存储拒绝向量模式时中文指引）/ 梦境（同步触发 + 运行日志 +
  逐事件决策）；人格作用域经共享 PersonaPicker 显式指定；
- **用量观测（usage）**——对接 `/api/v1/usage` 3 个端点：总览瓦片（调用 /
  token / 均时延 + per-purpose 明细）、多维聚合（模型 / 用途 / 时间桶 +
  minute|hour|day 桶宽）、调用记录游标分页（多轴过滤）；跨人格观测口径，
  仅表格 + 瓦片（无图表库）；
- **模型注册表（models）**——对接 `/api/v1/models` 5 个端点：条目表（脱敏：
  只见 api_key_env 环境变量名）+ 角色绑定汇总 + 创建 / 编辑（roles 清空解绑）/
  删除（绑定守卫 409 → 两步指引）。

## 认证（管理面 owner-token）

后端可启用 owner-token 鉴权（R2 起——运维在部署侧设置 `ADMIN_AUTH_TOKEN`
环境变量，非空 = 启用；变量名可经 `SERVER_ADMIN_TOKEN_ENV` 调整，完整契约
见 `docs/backend/README.md` 认证一节）。前端形态：

- **token 携带**：`src/api/auth.ts` 存取 localStorage（键 `aac_admin_token`
  ——本值只存浏览器本地，不入 URL / 日志）；`src/api/client.ts` 请求拦截器
  逐请求以 `Authorization: Bearer <token>` 头携带（无 token 不带头——后端
  鉴权关闭形态照常工作）；
- **401 令牌门（事件驱动）**：服务端是否要求 token 无法在挂载期得知
  （`/api/v1/health` 是零依赖存活面，不携带鉴权状态）——首个请求收到
  HTTP 401 时 client 广播鉴权需求事件，`App.tsx` 据此以
  `AdminTokenScreen`（中文令牌输入页）替换整个应用壳；
- **保存即整页重载（锁定决策）**：门内「保存并进入」写入 localStorage 后
  `window.location.reload()`——token 经请求拦截器逐请求读取，重载是最小
  可靠路径（免手工重放在途查询 / 刷新 React Query 缓存），门自身无路由态；
- **注销**：侧栏底部「注销」清除 localStorage token 并经同一事件面回到
  令牌门（与 401 门共用承接路径）。

测试口径：msw 默认 handlers 保持无鉴权形态（不校验 Authorization 头）；
鉴权相关用例（401 → 门、token 头携带、保存 / 注销）在各自测试内经
`server.use` 覆写与 localStorage 直写驱动。

## 目录结构

```
frontend/
├── pixi.toml              # Pixi 任务入口（Node 22 环境 + 全部质量命令）
├── package.json           # npm 依赖与 scripts
├── vite.config.ts         # Vite（@ → src 别名；5173 端口）
├── vitest.config.ts       # Vitest（jsdom + 覆盖率 perFile ≥80% 门禁）
├── scripts/               # 构建体积门禁（check_bundle_budget.mjs，纯 Node 内置）
├── eslint.config.js       # ESLint 9 flat（strict-type-checked 全 error）
├── tsconfig.json          # TypeScript strict（noUncheckedIndexedAccess 等）
└── src/
    ├── api/               # 类型化 API 客户端
    │   ├── client.ts      #   axios 实例 + ResultDTO 解包 + ApiError 归一 + owner-token Bearer 携带 / 401 广播
    │   ├── auth.ts        #   owner token 存取（localStorage）+ 鉴权需求事件（401 → 令牌门）
    │   ├── personas.ts    #   personas 13 端点 typed 封装
    │   ├── memories.ts    #   memories 16 端点 typed 封装
    │   ├── usage.ts       #   usage 3 端点 typed 封装
    │   ├── models.ts      #   models 5 端点 typed 封装
    │   └── pagination.ts  #   游标分页助手（next_cursor / 页摊平）
    ├── types/             # DTO 类型 Unit 文件（结构化数据禁止裸字面量）
    │   ├── common.ts      #   ResultDTO<T> / PageResponse<T> / 批量删除通用形态
    │   ├── persona.ts     #   persona 域全部 wire 契约
    │   ├── memory.ts      #   memory 域全部 wire 契约
    │   ├── usage.ts       #   usage 域全部 wire 契约
    │   └── model-registry.ts # model-registry 域全部 wire 契约
    ├── components/
    │   ├── AppShell.tsx   # 侧边栏导航壳（人格/记忆/用量/模型）
    │   ├── PageBoundary.tsx # 路由懒加载边界（加载态 + 失败态）
    │   ├── ConfirmDialog.tsx
    │   ├── PersonaPicker.tsx       # 目标人格选择器（admin 路由显式 persona_id）
    │   ├── BatchDeleteResultDialog.tsx # 批量删除部分成功结果（共享）
    │   └── ui/            # 基础组件库（Button/Input/Dialog/Toast/Table…）
    ├── hooks/
    │   ├── usePersonasApi.ts  # React Query hooks（列表 + 13 端点 mutations）
    │   ├── useMemoriesApi.ts  # memories 16 端点 hooks（检索/触发为 mutation）
    │   ├── useUsageApi.ts     # usage 总览/聚合/记录 hooks
    │   └── useModelsApi.ts    # models 注册表状态/绑定 + CRUD hooks
    ├── pages/
    │   ├── personas/             # 人格模块（列表/表单/世界书/复制/批量/状态层）
    │   ├── memories/             # 记忆模块（五页签：情景/策展/会话/检索/梦境）
    │   ├── usage/                # 用量模块（总览瓦片 + 聚合 + 记录表）
    │   └── models/               # 模型注册表模块（条目 + 角色绑定 + 409 守卫流）
    ├── lib/               # cn / 时间格式化 / 实时语音（WS 客户端、采集 worklet、播放队列）
    ├── test/              # msw 服务器（四域默认 handlers）+ 共享渲染工具 + 契约漂移门禁（contract/）
    ├── App.tsx            # 路由 + ToastProvider
    └── main.tsx           # 入口（React Query Provider）
```

## Pixi 命令

全部质量命令必须经 pixi 执行（`pixi install` 提供 Node.js 22；`package.json` 的
`engines` 与 `.npmrc` 的 `engine-strict=true` 拒绝其它 Node 大版本——直接 `npm`
在非 22 的环境下会失败）：

| 命令 | 说明 |
| --- | --- |
| `pixi install` | 安装 Node.js 22 环境（Pixi 侧） |
| `pixi run install` | `npm install`（依赖 + lockfile） |
| `pixi run dev` | 启动 Vite 开发服务（端口 5173） |
| `pixi run build` | 生产构建（`tsc && vite build`） |
| `pixi run bundle-check` | 构建体积门禁（需先 `pixi run build`；初始 JS ≤1 MB 且 ≥3 动态 chunk） |
| `pixi run test` | 运行测试（`vitest run`，单次执行） |
| `pixi run coverage` | 测试 + 覆盖率（src/ 全模块 **perFile ≥80%** 门禁） |
| `pixi run lint` | `tsc --noEmit` + `eslint src/`（strict-type-checked） |
| `pixi run typecheck` | 仅 `tsc --noEmit` |
| `pixi run preview` | 本地预览生产包 |

## 实时语音采集与播放（R4-3 增强）

`src/pages/realtime/` + `src/lib/realtimeVoiceClient.ts` / `realtimeAudio.ts` /
`realtimeCaptureWorklet.ts` 承载全双工语音面板：

- **采集约束（锁定）**：`getUserMedia` 固定请求 `echoCancellation` /
  `noiseSuppression` / `autoGainControl` 全开；启动后经轨道 `getSettings()`
  回读实际生效值（`MicrophoneCapture.getCapabilities()`），未生效时面板提示
  佩戴耳机 / 识别率下降（不阻断会话）。
- **采集路径**：优先 `AudioWorklet`（`realtimeCaptureWorklet.ts` 经 Vite
  `?worker&url` 模块化加载，定长块回传；块切分核心 `takeFixedBlocks` 在
  `realtimeAudio.ts` 内可单测）；模块加载 / 建节点失败回退 `ScriptProcessor`。
  两条路径输出同为 16kHz PCM16 100ms 帧（既有心跳与后端协议不变）。
- **移动端边界**：页面后台（`visibilitychange` hidden）暂停采集（断节点 /
  停轨 / 关上下文，WS 会话不重连）；回到前台自动恢复，浏览器要求手势时由
  页面点击触发恢复。
- **能力公告**：`ready` 的 `aec` / `streaming_asr` / `streaming_tts` 字段
  展示在会话参数行（流式 / 回声消除态）；两条流式通道由后端开关控制，前端
  无需配置。
- **工作线程覆盖率**：`realtimeCaptureWorklet.ts` 在音频渲染线程运行、jsdom
  不执行，按 `vitest.config.ts` 注释排除（其核心逻辑由 `takeFixedBlocks`
  单测覆盖）。

## 构建体积基线与门禁（R4-1）

生产构建已按路由拆分：初始加载 JS `dist/assets/index-*.js` **389.80 KB**（gzip
**123.61 KB**），25 个动态 chunk 合计 337.06 KB（gzip 109.65 KB），CSS 23.31 KB
（gzip 5.30 KB）。完整 chunk 明细、构建耗时与测量口径见
[docs/backend/performance.md「前端构建产物体积基线（R4-1）」](../docs/backend/performance.md#前端构建产物体积基线r4-1)。

本节的 KB 为十进制换算（字节 ÷ 1000，四舍五入保留两位），与
performance.md 的字节值一一对应、可复算；gzip 口径同为 Node `zlib.gzipSync`
（默认参数），与 `scripts/check_bundle_budget.mjs` 的 `measure()` 同工具。

**门禁**：`cd frontend && pixi run build && pixi run bundle-check`——初始加载 JS
超过 **1 MB** 或动态 chunk 少于 **3** 个即失败；阈值常量只在
`scripts/check_bundle_budget.mjs`。**触发条件**：初始加载 JS 超过 1 MB 必须继续
拆分；新增路由页经 `React.lazy` 独立分包（共享壳 `AppShell` / `ToastProvider` /
令牌门保持 eager），不得为了拆而拆。

决策历史：R3 曾维持单 chunk（717.44 KB / gzip 213.91 KB＝213,912 B）；R4-1 依实测
改为路由级 `lazy()` + 体积门禁，初始加载 JS 降至 389.80 KB。

## 开发工作流（与后端联调）

1. 启动后端（默认 `http://127.0.0.1:8000`，见 backend 模块说明）；
2. `cd frontend && pixi install && pixi run install`（首次）；
3. `pixi run dev` → 浏览器打开 `http://localhost:5173`。

后端 CORS 已放行 Vite 开发来源（5173），前端直连后端、无开发代理。
后端地址经环境变量覆盖（复制 `.env.example` 为 `.env` 后按需修改）：

```
VITE_API_BASE_URL=http://127.0.0.1:8000
```

所有 API 响应为 `ResultDTO` 封装（`{success, code, message, data}`），
列表端点为游标分页（`PageResponse.items + next_cursor`）——前端在
`src/api/client.ts` 统一解包，失败统一抛 `ApiError`（中文 message 直接
toast 呈现，含 400 校验 / 404 / 409 冲突等后端语义）。

## 测试约定

- 全部 API 请求经 **msw**（Mock Service Worker，Node 拦截器形态）mock，
  测试永不依赖真实后端 / 真实网络（未 mock 请求直接报错）；
- 页面测试走 `renderWithProviders`（React Query + memory router +
  Toast），覆盖：表单校验、游标分页 load-more、批量删除部分成功、
  激活守卫 409 等错误路径的用户可见呈现；
- 覆盖率门禁：`pixi run coverage` 对 `src/**` 逐文件 ≥80%
  （lines / branches / functions / statements）。

## 契约漂移门禁（types 镜像 ↔ 后端 DTO）

`src/types/` 是后端 pydantic DTO 的手写镜像（前后端源码隔离），漂移
由**双侧门禁**拦截（数据源为提交态 fixture
`src/test/contract/dtoSchemas.json`，49 组共享 wire 契约）：

- **改后端 DTO** → 先 `cd backend && pixi run export-dto-schemas`
  重新导出 fixture 并随改动一并提交——否则后端
  `test_dto_schema_fixture.py`（内存再生 ↔ 提交态逐字节比对）即红；
- **改前端 types**（增删字段）→ 前端契约测试
  `src/test/contract/dtoContract.test.ts` 即拦（TypeScript 编译器 API
  提取手写类型成员名，与 fixture 中 schema `properties` 键集逐组断言
  相等，中文 diff 报缺失 / 多余字段）；
- fixture 由脚本确定性生成（排序键 + byte-stable），**严禁手改**；
  manifest 段固化「schema 键 ↔ 前端类型 ↔ 源文件」映射。

## 代码规范（本工程约定）

- TypeScript strict，无 `any` / `@ts-ignore` / `eslint-disable`；
  ESLint 为 typescript-eslint strict-type-checked 全 error 基线；
- 结构化数据一律走 `src/types/` 的接口形态（禁止裸对象字面量承载
  DTO 语义）；表单经 typed form model 收敛为 wire 契约后提交；
- 导出模块 / 工具带简明中文 JSDoc；UI 文案全部中文。

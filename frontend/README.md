# AI 陪伴伙伴 · 管理前端

人格驱动 AI 陪伴伙伴（ai_agent_companion）的管理台前端：独立 Pixi 项目
（Node.js 22 + npm），技术栈为 Vite 6 + React 19 + TypeScript（strict）+
Tailwind CSS 4 + React Query 5 + React Router 7。

当前模块：**人格管理（personas）**——对接后端 `/api/v1/personas` 全部
13 个端点（游标分页列表 / 创建 / 读取 / 部分更新 / 删除 / 复制 / 激活 /
停用 / 卡片读取 / 卡片整体替换 / 状态层读取 / 状态层 upsert / 批量删除）。
记忆 / 用量 / 模型为「建设中」占位页，后续模块落地后替换。

## 目录结构

```
frontend/
├── pixi.toml              # Pixi 任务入口（Node 22 环境 + 全部质量命令）
├── package.json           # npm 依赖与 scripts
├── vite.config.ts         # Vite（@ → src 别名；5173 端口）
├── vitest.config.ts       # Vitest（jsdom + 覆盖率 perFile ≥80% 门禁）
├── eslint.config.js       # ESLint 9 flat（strict-type-checked 全 error）
├── tsconfig.json          # TypeScript strict（noUncheckedIndexedAccess 等）
└── src/
    ├── api/               # 类型化 API 客户端
    │   ├── client.ts      #   axios 实例 + ResultDTO 解包 + ApiError 归一
    │   ├── personas.ts    #   personas 13 端点 typed 封装
    │   └── pagination.ts  #   游标分页助手（next_cursor / 页摊平）
    ├── types/             # DTO 类型 Unit 文件（结构化数据禁止裸字面量）
    │   ├── common.ts      #   ResultDTO<T> / PageResponse<T>
    │   └── persona.ts     #   persona 域全部 wire 契约
    ├── components/
    │   ├── AppShell.tsx   # 侧边栏导航壳（人格/记忆/用量/模型）
    │   ├── ConfirmDialog.tsx
    │   └── ui/            # 基础组件库（Button/Input/Dialog/Toast/Table…）
    ├── hooks/
    │   └── usePersonasApi.ts  # React Query hooks（列表 + 13 端点 mutations）
    ├── pages/
    │   ├── PlaceholderPage.tsx   # 「建设中」占位页
    │   └── personas/
    │       ├── PersonaListPage.tsx        # 列表（游标分页/过滤/激活/复制/删除/批量）
    │       ├── PersonaFormPage.tsx        # 创建/编辑（卡片 + 世界书 + JSON 预览）
    │       ├── personaFormModel.ts        # typed form model（校验 + wire 转换）
    │       ├── CharacterBookEditor.tsx    # 世界书条目编辑器
    │       ├── CopyPersonaDialog.tsx      # 复制对话框（include_states）
    │       ├── BatchDeleteResultDialog.tsx# 批量删除部分成功结果
    │       ├── PersonaStatesPanel.tsx     # M/S 状态层查看 + JSON 写入
    │       └── PersonaCardJsonPreview.tsx # 卡片 JSON 实时预览
    ├── lib/               # cn / 时间格式化
    ├── test/              # msw 服务器 + 共享渲染工具（测试永不触达真实后端）
    ├── App.tsx            # 路由 + ToastProvider
    └── main.tsx           # 入口（React Query Provider）
```

## Pixi 命令

全部质量命令经 pixi 执行（保证 Node 版本一致）：

| 命令 | 说明 |
| --- | --- |
| `pixi install` | 安装 Node.js 22 环境（Pixi 侧） |
| `pixi run install` | `npm install`（依赖 + lockfile） |
| `pixi run dev` | 启动 Vite 开发服务（端口 5173） |
| `pixi run build` | 生产构建（`tsc && vite build`） |
| `pixi run test` | 运行测试（`vitest run`，单次执行） |
| `pixi run coverage` | 测试 + 覆盖率（src/ 全模块 **perFile ≥80%** 门禁） |
| `pixi run lint` | `tsc --noEmit` + `eslint src/`（strict-type-checked） |
| `pixi run typecheck` | 仅 `tsc --noEmit` |
| `pixi run preview` | 本地预览生产包 |

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

## 代码规范（本工程约定）

- TypeScript strict，无 `any` / `@ts-ignore` / `eslint-disable`；
  ESLint 为 typescript-eslint strict-type-checked 全 error 基线；
- 结构化数据一律走 `src/types/` 的接口形态（禁止裸对象字面量承载
  DTO 语义）；表单经 typed form model 收敛为 wire 契约后提交；
- 导出模块 / 工具带简明中文 JSDoc；UI 文案全部中文。

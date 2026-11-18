import { useEffect, useState } from 'react'
import { Navigate, Outlet, RouterProvider, createBrowserRouter, type RouteObject } from 'react-router-dom'
import { onAuthRequired } from '@/api/auth'
import { AdminTokenScreen } from '@/components/AdminTokenScreen'
import { AppShell } from '@/components/AppShell'
import { ToastProvider } from '@/components/ui/Toast'
import PersonaListPage from '@/pages/personas/PersonaListPage'
import PersonaFormPage from '@/pages/personas/PersonaFormPage'
import PersonaPresetGallery from '@/pages/personas/PersonaPresetGallery'
import MemoriesPage from '@/pages/memories/MemoriesPage'
import UsagePage from '@/pages/usage/UsagePage'
import ModelsPage from '@/pages/models/ModelsPage'
import StickersPage from '@/pages/stickers/StickersPage'

/**
 * 应用路由（本工程约定）：
 * - / → /personas（人格管理模块）；
 * - /personas/presets 官方特质预设画廊（生成导入）；
 * - /memories 记忆管理（五页签：情景 / 策展 / 会话 / 检索 / 梦境）；
 * - /usage 用量观测（总览 / 聚合 / 记录）；
 * - /models 模型注册表（条目 / 角色绑定）；
 * - /stickers 表情包库（上传 / 标签 / 删除 / VLM 标注）；
 * - 全部页面挂 AppShell（侧边栏导航）之下，ToastProvider 包裹根。
 */
const routes: RouteObject[] = [
  {
    element: (
      <ToastProvider>
        <Outlet />
      </ToastProvider>
    ),
    children: [
      {
        element: <AppShell />,
        children: [
          { index: true, element: <Navigate to="/personas" replace /> },
          { path: 'personas', element: <PersonaListPage /> },
          { path: 'personas/presets', element: <PersonaPresetGallery /> },
          { path: 'personas/new', element: <PersonaFormPage mode="create" /> },
          { path: 'personas/:personaId/edit', element: <PersonaFormPage mode="edit" /> },
          { path: 'memories', element: <MemoriesPage /> },
          { path: 'usage', element: <UsagePage /> },
          { path: 'models', element: <ModelsPage /> },
          { path: 'stickers', element: <StickersPage /> },
          { path: '*', element: <Navigate to="/personas" replace /> },
        ],
      },
    ],
  },
]

/**
 * 应用根：惰性创建一次 browser router（生产单页根 = 单例）。
 *
 * 管理面鉴权门（事件驱动）：服务端是否要求 owner token 无法在挂载期
 * 得知（health 零依赖契约不携带鉴权状态）——首个 401 由 api client
 * 广播鉴权需求事件，本根据此以令牌门（AdminTokenScreen）替换应用壳；
 * 侧栏「注销」清 token 后复用同一事件面回到令牌门。
 */
function App() {
  const [router] = useState(() => createBrowserRouter(routes))
  const [authRequired, setAuthRequired] = useState(false)
  useEffect(() => onAuthRequired(() => setAuthRequired(true)), [])
  if (authRequired) {
    return <AdminTokenScreen />
  }
  return <RouterProvider router={router} />
}

export default App

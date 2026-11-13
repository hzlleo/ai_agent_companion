import { useState } from 'react'
import { Navigate, Outlet, RouterProvider, createBrowserRouter, type RouteObject } from 'react-router-dom'
import { AppShell } from '@/components/AppShell'
import { ToastProvider } from '@/components/ui/Toast'
import PersonaListPage from '@/pages/personas/PersonaListPage'
import PersonaFormPage from '@/pages/personas/PersonaFormPage'
import MemoriesPage from '@/pages/memories/MemoriesPage'
import UsagePage from '@/pages/usage/UsagePage'
import ModelsPage from '@/pages/models/ModelsPage'

/**
 * 应用路由（本工程约定）：
 * - / → /personas（人格管理模块）；
 * - /memories 记忆管理（五页签：情景 / 策展 / 会话 / 检索 / 梦境）；
 * - /usage 用量观测（总览 / 聚合 / 记录）；
 * - /models 模型注册表（条目 / 角色绑定）；
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
          { path: 'personas/new', element: <PersonaFormPage mode="create" /> },
          { path: 'personas/:personaId/edit', element: <PersonaFormPage mode="edit" /> },
          { path: 'memories', element: <MemoriesPage /> },
          { path: 'usage', element: <UsagePage /> },
          { path: 'models', element: <ModelsPage /> },
          { path: '*', element: <Navigate to="/personas" replace /> },
        ],
      },
    ],
  },
]

/** 应用根：惰性创建一次 browser router（生产单页根 = 单例）。 */
function App() {
  const [router] = useState(() => createBrowserRouter(routes))
  return <RouterProvider router={router} />
}

export default App

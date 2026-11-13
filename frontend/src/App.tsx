import { useState } from 'react'
import { Navigate, Outlet, RouterProvider, createBrowserRouter, type RouteObject } from 'react-router-dom'
import { AppShell } from '@/components/AppShell'
import { ToastProvider } from '@/components/ui/Toast'
import PersonaListPage from '@/pages/personas/PersonaListPage'
import PersonaFormPage from '@/pages/personas/PersonaFormPage'
import { PlaceholderPage } from '@/pages/PlaceholderPage'

/**
 * 应用路由（本工程约定）：
 * - / → /personas（人格是当前唯一落地的管理模块）；
 * - 记忆 / 用量 / 模型为「建设中」占位路由，模块落地后替换；
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
          { path: 'memories', element: <PlaceholderPage moduleName="记忆" /> },
          { path: 'usage', element: <PlaceholderPage moduleName="用量" /> },
          { path: 'models', element: <PlaceholderPage moduleName="模型" /> },
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

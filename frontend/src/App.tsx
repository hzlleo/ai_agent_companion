import { lazy, useEffect, useState, type ReactElement } from 'react'
import { Navigate, Outlet, RouterProvider, createBrowserRouter, type RouteObject } from 'react-router-dom'
import { onAuthRequired } from '@/api/auth'
import { AdminTokenScreen } from '@/components/AdminTokenScreen'
import { AppShell } from '@/components/AppShell'
import { PageBoundary } from '@/components/PageBoundary'
import { ToastProvider } from '@/components/ui/Toast'

/** 路由页按模块独立分包：首屏只含共享壳与登录门，页面 chunk 按访问加载。 */
const PersonaListPage = lazy(() => import('@/pages/personas/PersonaListPage'))
const PersonaFormPage = lazy(() => import('@/pages/personas/PersonaFormPage'))
const PersonaPresetGallery = lazy(() => import('@/pages/personas/PersonaPresetGallery'))
const MemoriesPage = lazy(() => import('@/pages/memories/MemoriesPage'))
const UsagePage = lazy(() => import('@/pages/usage/UsagePage'))
const ModelsPage = lazy(() => import('@/pages/models/ModelsPage'))
const StickersPage = lazy(() => import('@/pages/stickers/StickersPage'))
const CanvasPage = lazy(() => import('@/pages/canvas/CanvasPage'))
const RealtimeVoicePage = lazy(() => import('@/pages/realtime/RealtimeVoicePage'))

/** 路由页元素：内容区加载态 / 失败态由 PageBoundary 统一承接；key 取路由路径，切换路由时重置失败态。 */
function page(key: string, element: ReactElement) {
  return (
    <PageBoundary key={key}>
      {element}
    </PageBoundary>
  )
}

/**
 * 应用路由（本工程约定）：
 * - / → /personas（人格管理模块）；
 * - /personas/presets 官方特质预设画廊（生成导入）；
 * - /memories 记忆管理（五页签：情景 / 策展 / 会话 / 检索 / 梦境）；
 * - /usage 用量观测（总览 / 聚合 / 记录）；
 * - /models 模型注册表（条目 / 角色绑定）；
 * - /stickers 表情包库（上传 / 标签 / 删除 / VLM 标注）；
 * - /canvas 画布（渲染产物创建 / 预览 / 修订 / 导出）；
 * - /realtime 实时语音（全双工会话：麦克风上行 / 字幕 / 播报 / 打断）；
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
          { path: 'personas', element: page('personas', <PersonaListPage />) },
          { path: 'personas/presets', element: page('personas/presets', <PersonaPresetGallery />) },
          { path: 'personas/new', element: page('personas/new', <PersonaFormPage mode="create" />) },
          { path: 'personas/:personaId/edit', element: page('personas/:personaId/edit', <PersonaFormPage mode="edit" />) },
          { path: 'memories', element: page('memories', <MemoriesPage />) },
          { path: 'usage', element: page('usage', <UsagePage />) },
          { path: 'models', element: page('models', <ModelsPage />) },
          { path: 'stickers', element: page('stickers', <StickersPage />) },
          { path: 'canvas', element: page('canvas', <CanvasPage />) },
          { path: 'realtime', element: page('realtime', <RealtimeVoicePage />) },
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

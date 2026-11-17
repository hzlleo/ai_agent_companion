import { NavLink, Outlet } from 'react-router-dom'
import { Bot, Brain, Gauge, LogOut, Sparkles } from 'lucide-react'
import { clearAdminToken, notifyAuthRequired } from '@/api/auth'
import { cn } from '@/lib/utils'

/** 侧边导航项。 */
interface NavItem {
  /** 路由路径。 */
  to: string
  /** 导航文案（中文）。 */
  label: string
  /** 图标。 */
  icon: typeof Bot
}

const NAV_ITEMS: NavItem[] = [
  { to: '/personas', label: '人格', icon: Bot },
  { to: '/memories', label: '记忆', icon: Brain },
  { to: '/usage', label: '用量', icon: Gauge },
  { to: '/models', label: '模型', icon: Sparkles },
]

/**
 * 管理台应用壳：左侧导航 + 主内容区。
 *
 * 导航面（本工程约定）：人格 / 记忆 / 用量 / 模型四个管理模块入口，
 * 各自挂真实路由页面（模块落地情况见 App.tsx 路由表）；侧栏底部
 * 「注销」清除本地 owner token 并回到令牌门（经鉴权需求事件——与
 * 401 门同一承接面，见 AdminTokenScreen）。
 */
export function AppShell() {
  const handleLogout = () => {
    clearAdminToken()
    notifyAuthRequired()
  }

  return (
    <div className="flex min-h-screen">
      <aside className="flex w-56 shrink-0 flex-col border-r border-[var(--color-border)] bg-[var(--color-sidebar)]">
        <div className="flex h-16 items-center gap-2 border-b border-[var(--color-border)] px-5">
          <Bot className="h-6 w-6 text-[var(--color-primary)]" aria-hidden="true" />
          <span className="text-base font-semibold text-[var(--color-sidebar-text)]">
            AI 陪伴伙伴 · 管理台
          </span>
        </div>
        <nav aria-label="主导航" className="flex flex-1 flex-col gap-1 p-3">
          {NAV_ITEMS.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              className={({ isActive }) =>
                cn(
                  'flex items-center gap-3 rounded-md px-3 py-2 text-sm font-medium transition-colors',
                  isActive
                    ? 'bg-[var(--color-sidebar-active)] text-[var(--color-primary-hover)]'
                    : 'text-[var(--color-sidebar-text)] hover:bg-[var(--color-sidebar-hover)]',
                )
              }
            >
              <item.icon className="h-4 w-4" aria-hidden="true" />
              {item.label}
            </NavLink>
          ))}
        </nav>
        <div className="border-t border-[var(--color-border)] p-3">
          <button
            type="button"
            onClick={handleLogout}
            className="flex w-full items-center gap-3 rounded-md px-3 py-2 text-sm font-medium text-[var(--color-sidebar-text)] transition-colors hover:bg-[var(--color-sidebar-hover)]"
          >
            <LogOut className="h-4 w-4" aria-hidden="true" />
            注销
          </button>
        </div>
      </aside>
      <main className="min-w-0 flex-1 bg-[var(--color-surface)]">
        <Outlet />
      </main>
    </div>
  )
}

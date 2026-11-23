import { Component, Suspense, type ErrorInfo, type ReactNode } from 'react'
import { Alert, AlertDescription } from '@/components/ui/Alert'
import { Spinner } from '@/components/ui/Spinner'

/** 路由页加载占位：懒加载 chunk 拉取期间的应用内容区加载态。 */
export function PageLoading() {
  return (
    <div className="flex min-h-[50vh] items-center justify-center gap-3 text-sm text-[var(--color-text-secondary)]">
      <Spinner size="sm" />
      <span>页面加载中…</span>
    </div>
  )
}

interface PageBoundaryProps {
  /** 路由页元素（通常为 React.lazy 包装的页面组件）。 */
  children: ReactNode
}

interface PageBoundaryState {
  /** 页面模块渲染/拉取是否已失败。 */
  failed: boolean
}

/**
 * 路由页边界：懒加载 chunk 的 Suspense 加载态 + 失败态错误边界。
 *
 * 边界只承接应用内容区（AppShell 侧栏与导航保持可用）；chunk 拉取失败
 * （例如发版后旧入口请求已删除的哈希资源）时给出刷新指引——整页重载即可
 * 取得新的资源映射，不做自动重试以免掩盖持续失败。
 */
export class PageBoundary extends Component<PageBoundaryProps, PageBoundaryState> {
  override state: PageBoundaryState = { failed: false }

  static getDerivedStateFromError(): PageBoundaryState {
    return { failed: true }
  }

  override componentDidCatch(error: Error, errorInfo: ErrorInfo): void {
    console.error('页面模块加载失败', error, errorInfo.componentStack)
  }

  override render(): ReactNode {
    if (this.state.failed) {
      return (
        <div className="p-6">
          <Alert variant="destructive">
            <AlertDescription>页面加载失败，请刷新页面重试。</AlertDescription>
          </Alert>
        </div>
      )
    }
    return <Suspense fallback={<PageLoading />}>{this.props.children}</Suspense>
  }
}

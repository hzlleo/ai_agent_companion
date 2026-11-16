import { Construction } from 'lucide-react'
import { EmptyState } from '@/components/ui/EmptyState'

export interface PlaceholderPageProps {
  /** 模块名（如「记忆」）。 */
  moduleName: string
}

/**
 * 占位页面：尚未落地的模块统一渲染「建设中」。
 *
 * 本工程约定：记忆/用量/模型等后续模块在路由注册期先挂本页，
 * 模块落地后替换为真实页面组件。
 */
export function PlaceholderPage({ moduleName }: PlaceholderPageProps) {
  return (
    <div className="p-8">
      <h1 className="text-2xl font-semibold text-[var(--color-text)]">{moduleName}</h1>
      <EmptyState
        icon={<Construction className="w-6 h-6 text-[var(--color-text-muted)]" />}
        title="建设中"
        description={`「${moduleName}」模块正在建设中，敬请期待。`}
      />
    </div>
  )
}

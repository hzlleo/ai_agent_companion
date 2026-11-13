import { type ReactNode } from 'react'
import { Inbox } from 'lucide-react'
import { cn } from '@/lib/utils'

export interface EmptyStateProps {
  icon?: ReactNode
  title: string
  description?: string
  action?: ReactNode
  className?: string
}

/** 空态占位（图标 + 标题 + 描述 + 可选操作）。 */
export function EmptyState({
  icon,
  title,
  description,
  action,
  className,
}: EmptyStateProps) {
  return (
    <div className={cn('flex flex-col items-center justify-center py-16', className)}>
      <div className="inline-flex items-center justify-center w-12 h-12 rounded-full bg-[var(--color-surface-secondary)] mb-3">
        {icon ?? <Inbox className="w-6 h-6 text-[var(--color-text-muted)]" />}
      </div>
      <p className="text-[var(--color-text-secondary)] font-medium">{title}</p>
      {description && <p className="text-[var(--color-text-muted)] text-sm mt-1">{description}</p>}
      {action && <div className="mt-4">{action}</div>}
    </div>
  )
}

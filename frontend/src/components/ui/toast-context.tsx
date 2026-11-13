import { createContext, type ReactNode } from 'react'
import { AlertCircle, AlertTriangle, CheckCircle, Info } from 'lucide-react'

/** Toast 变体。 */
export type ToastVariant = 'success' | 'error' | 'warning' | 'info'

/** 单条 Toast。 */
export interface ToastItem {
  id: number
  message: string
  variant: ToastVariant
}

/** Toast 上下文契约。 */
export interface ToastContextValue {
  success: (message: string) => void
  error: (message: string) => void
  warning: (message: string) => void
  info: (message: string) => void
}

export const ToastContext = createContext<ToastContextValue | null>(null)

/** 变体 → 图标 + 边框色（Toast 渲染消费）。 */
export const toastVariantConfig: Record<ToastVariant, { icon: ReactNode; className: string }> = {
  success: {
    icon: <CheckCircle className="w-5 h-5 text-[var(--color-success)]" />,
    className: 'border-[var(--color-success)]',
  },
  error: {
    icon: <AlertCircle className="w-5 h-5 text-[var(--color-destructive)]" />,
    className: 'border-[var(--color-destructive)]',
  },
  warning: {
    icon: <AlertTriangle className="w-5 h-5 text-[var(--color-warning)]" />,
    className: 'border-[var(--color-warning)]',
  },
  info: {
    icon: <Info className="w-5 h-5 text-[var(--color-info)]" />,
    className: 'border-[var(--color-info)]',
  },
}

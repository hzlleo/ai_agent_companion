import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react'
import { X } from 'lucide-react'
import { cn } from '@/lib/utils'
import {
  ToastContext,
  toastVariantConfig,
  type ToastContextValue,
  type ToastItem,
  type ToastVariant,
} from './toast-context'

let toastIdCounter = 0

/**
 * Toast 提供方：右下角浮层通知（success/error/warning/info）。
 *
 * 自动消失计时器统一登记，unmount 时清理——否则计时器在测试环境
 * 拆除后仍触发状态更新（jsdom window 已释放 → unhandled error）。
 */
export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<ToastItem[]>([])
  const autoDismissTimersRef = useRef<Set<ReturnType<typeof setTimeout>>>(new Set())

  useEffect(() => {
    const timers = autoDismissTimersRef.current
    return () => {
      for (const timer of timers) {
        clearTimeout(timer)
      }
      timers.clear()
    }
  }, [])

  const removeToast = useCallback((id: number) => {
    setToasts((prev) => prev.filter((t) => t.id !== id))
  }, [])

  const addToast = useCallback(
    (message: string, variant: ToastVariant) => {
      const id = ++toastIdCounter
      setToasts((prev) => [...prev, { id, message, variant }])
      const timer = setTimeout(() => {
        autoDismissTimersRef.current.delete(timer)
        removeToast(id)
      }, 4000)
      autoDismissTimersRef.current.add(timer)
    },
    [removeToast],
  )

  const value: ToastContextValue = {
    success: (msg) => {
      addToast(msg, 'success')
    },
    error: (msg) => {
      addToast(msg, 'error')
    },
    warning: (msg) => {
      addToast(msg, 'warning')
    },
    info: (msg) => {
      addToast(msg, 'info')
    },
  }

  return (
    <ToastContext.Provider value={value}>
      {children}
      <div className="fixed bottom-4 right-4 z-[100] flex flex-col gap-2">
        {toasts.map((toast) => {
          const config = toastVariantConfig[toast.variant]
          return (
            <div
              key={toast.id}
              role="status"
              className={cn(
                'flex items-center gap-2 px-4 py-3 rounded-lg border bg-[var(--color-surface)] shadow-lg min-w-[300px]',
                config.className,
              )}
            >
              {config.icon}
              <span className="text-sm text-[var(--color-text)] flex-1">{toast.message}</span>
              <button
                onClick={() => {
                  removeToast(toast.id)
                }}
                aria-label="关闭提示"
                className="text-[var(--color-text-muted)] hover:text-[var(--color-text)]"
              >
                <X className="w-4 h-4" />
              </button>
            </div>
          )
        })}
      </div>
    </ToastContext.Provider>
  )
}

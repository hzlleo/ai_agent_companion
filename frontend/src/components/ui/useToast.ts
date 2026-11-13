import { useContext } from 'react'
import { ToastContext, type ToastContextValue } from './toast-context'

/** Toast 消费 hook：必须在 ToastProvider 内使用。 */
export function useToast(): ToastContextValue {
  const ctx = useContext(ToastContext)

  if (!ctx) {
    throw new Error('useToast must be used within ToastProvider')
  }

  return ctx
}

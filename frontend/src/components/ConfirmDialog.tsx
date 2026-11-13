import type { ReactNode } from 'react'
import { AlertTriangle } from 'lucide-react'
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/Dialog'
import { Button } from '@/components/ui/Button'

export interface ConfirmDialogProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  /** 对话框标题。 */
  title: string
  /** 确认说明（支持 ReactNode，可携带对象名等上下文）。 */
  description: ReactNode
  /** 确认按钮文案（默认「确认」）。 */
  confirmText?: string
  /** 取消按钮文案（默认「取消」）。 */
  cancelText?: string
  /** 危险操作（确认按钮红色）。 */
  destructive?: boolean
  /** 确认中（按钮 loading + 禁用）。 */
  loading?: boolean
  /** 确认回调（返回 Promise 时按钮进入 loading；关闭交由调用方控制）。 */
  onConfirm: () => void | Promise<void>
}

/**
 * 通用确认对话框：危险/不可逆操作前的二次确认。
 *
 * 确认执行期间禁用取消与关闭（防重复提交）；onConfirm 的异常由
 * 调用方自行处理（toast），本组件不吞错。
 */
export function ConfirmDialog({
  open,
  onOpenChange,
  title,
  description,
  confirmText = '确认',
  cancelText = '取消',
  destructive = false,
  loading = false,
  onConfirm,
}: ConfirmDialogProps) {
  return (
    <Dialog open={open} onOpenChange={loading ? () => undefined : onOpenChange}>
      <DialogHeader>
        <DialogTitle>{title}</DialogTitle>
        <DialogClose
          onClose={() => {
            if (!loading) onOpenChange(false)
          }}
        />
      </DialogHeader>
      <DialogContent>
        <div className="flex items-start gap-3">
          <AlertTriangle
            className={
              destructive
                ? 'mt-0.5 h-5 w-5 shrink-0 text-[var(--color-destructive)]'
                : 'mt-0.5 h-5 w-5 shrink-0 text-[var(--color-warning)]'
            }
            aria-hidden="true"
          />
          <div className="text-sm text-[var(--color-text-secondary)]">{description}</div>
        </div>
      </DialogContent>
      <DialogFooter>
        <Button variant="outline" onClick={() => onOpenChange(false)} disabled={loading}>
          {cancelText}
        </Button>
        <Button
          variant={destructive ? 'destructive' : 'default'}
          loading={loading}
          onClick={() => {
            void onConfirm()
          }}
        >
          {confirmText}
        </Button>
      </DialogFooter>
    </Dialog>
  )
}

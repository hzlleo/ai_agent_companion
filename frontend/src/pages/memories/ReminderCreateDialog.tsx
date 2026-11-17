import { useEffect, useState } from 'react'
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/Dialog'
import { Button } from '@/components/ui/Button'
import { Input } from '@/components/ui/Input'
import { useToast } from '@/components/ui/useToast'
import { toApiError } from '@/api/client'
import { apiErrorFieldSuffix } from '@/api/errorDetails'
import { useCreateReminder } from '@/hooks/useRemindersApi'
import { datetimeLocalToEpochMs } from '@/lib/datetime'

export interface ReminderCreateDialogProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  /** 目标人格主键（undefined = 当前激活人格）。 */
  personaId: number | undefined
}

/**
 * 提醒创建对话框（标题 + 到期本地时间）。
 *
 * 到期时间经 ``datetime-local`` 输入（本地墙钟）→ epoch 毫秒（与后端
 * 扫描判定同轴）；允许过去时刻（创建即到期的补投语义——下一轮扫描
 * 立即投递）。标题 1-200 字符（前端预校验，后端边界复核）。
 */
export function ReminderCreateDialog({ open, onOpenChange, personaId }: ReminderCreateDialogProps) {
  const toast = useToast()
  const createMutation = useCreateReminder()
  const [title, setTitle] = useState('')
  const [dueText, setDueText] = useState('')
  const [errors, setErrors] = useState<Record<string, string>>({})

  useEffect(() => {
    if (open) {
      setTitle('')
      setDueText('')
      setErrors({})
    }
  }, [open])

  const handleConfirm = (): void => {
    const nextErrors: Record<string, string> = {}
    const trimmed = title.trim()
    if (trimmed.length < 1 || trimmed.length > 200) {
      nextErrors['title'] = '提醒标题必填，且长度为 1-200 个字符'
    }
    const dueAtMs = datetimeLocalToEpochMs(dueText)
    if (dueAtMs === null) {
      nextErrors['due'] = '请选择到期时间'
    }
    setErrors(nextErrors)
    if (Object.keys(nextErrors).length > 0 || dueAtMs === null) {
      toast.error('表单校验未通过，请检查标红字段')
      return
    }
    createMutation.mutate(
      { personaId, payload: { title: trimmed, due_at_ms: dueAtMs, source: 'manual' } },
      {
        onSuccess: (record) => {
          toast.success(`提醒已创建（#${record.id} 待投递）`)
          onOpenChange(false)
        },
        onError: (error) => {
          const apiError = toApiError(error)
          toast.error(`${apiError.message}${apiErrorFieldSuffix(apiError)}`)
        },
      },
    )
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogHeader>
        <DialogTitle>新建提醒</DialogTitle>
        <DialogClose onClose={() => onOpenChange(false)} />
      </DialogHeader>
      <DialogContent>
        <div className="space-y-4">
          <div>
            <label
              htmlFor="reminder-title"
              className="mb-1 block text-sm font-medium text-[var(--color-text-secondary)]"
            >
              提醒标题
            </label>
            <Input
              id="reminder-title"
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              placeholder="到点要提醒你的事（1-200 字符）"
              aria-invalid={errors['title'] !== undefined}
            />
            {errors['title'] !== undefined && (
              <p className="mt-1 text-sm text-[var(--color-destructive)]">{errors['title']}</p>
            )}
          </div>
          <div>
            <label
              htmlFor="reminder-due"
              className="mb-1 block text-sm font-medium text-[var(--color-text-secondary)]"
            >
              到期时间
            </label>
            <Input
              id="reminder-due"
              type="datetime-local"
              value={dueText}
              onChange={(e) => setDueText(e.target.value)}
              aria-invalid={errors['due'] !== undefined}
            />
            {errors['due'] !== undefined && (
              <p className="mt-1 text-sm text-[var(--color-destructive)]">{errors['due']}</p>
            )}
            <p className="mt-1 text-xs text-[var(--color-text-muted)]">
              到期后由后端扫描作业经属主通道投递；通道未接通时提醒保持待投递状态。
            </p>
          </div>
        </div>
      </DialogContent>
      <DialogFooter>
        <Button variant="outline" onClick={() => onOpenChange(false)}>
          取消
        </Button>
        <Button loading={createMutation.isPending} onClick={handleConfirm}>
          创建
        </Button>
      </DialogFooter>
    </Dialog>
  )
}

import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/Dialog'
import { Button } from '@/components/ui/Button'
import { Spinner } from '@/components/ui/Spinner'
import { toApiError } from '@/api/client'
import { useDreamRunDetail } from '@/hooks/useMemoriesApi'
import { DreamRunReport } from './DreamRunReport'

export interface DreamRunDetailDialogProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  /** 目标人格主键（undefined = 当前激活人格）。 */
  personaId: number | undefined
  /** 运行主键（0 = 不拉取）。 */
  runId: number
}

/** 梦境运行详情对话框：详情端点读取面 + 共享运行报告展示。 */
export function DreamRunDetailDialog({
  open,
  onOpenChange,
  personaId,
  runId,
}: DreamRunDetailDialogProps) {
  const detailQuery = useDreamRunDetail(personaId, open ? runId : 0)
  const noTarget = runId <= 0

  return (
    <Dialog open={open} onOpenChange={onOpenChange} size="xl">
      <DialogHeader>
        <DialogTitle>梦境运行详情</DialogTitle>
        <DialogClose onClose={() => onOpenChange(false)} />
      </DialogHeader>
      <DialogContent>
        {noTarget ? (
          <p className="py-4 text-sm text-[var(--color-text-muted)]">未选择运行。</p>
        ) : detailQuery.isPending ? (
          <div className="flex items-center justify-center py-12">
            <Spinner size="lg" />
          </div>
        ) : detailQuery.isError ? (
          <p className="py-4 text-sm text-[var(--color-destructive)]" role="alert">
            运行详情加载失败：{toApiError(detailQuery.error).message}
          </p>
        ) : (
          <DreamRunReport run={detailQuery.data} personaId={personaId} />
        )}
      </DialogContent>
      <DialogFooter>
        <Button onClick={() => onOpenChange(false)}>知道了</Button>
      </DialogFooter>
    </Dialog>
  )
}

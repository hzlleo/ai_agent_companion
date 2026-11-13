import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/Dialog'
import { Button } from '@/components/ui/Button'
import { Badge } from '@/components/ui/Badge'
import { Spinner } from '@/components/ui/Spinner'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/Table'
import { toApiError } from '@/api/client'
import { formatEpochMs } from '@/lib/datetime'
import { useCuratedHistory } from '@/hooks/useMemoriesApi'
import {
  CURATED_KIND_LABELS,
  CURATED_STATUS_BADGE_VARIANTS,
  CURATED_STATUS_LABELS,
} from './memoryDisplay'

export interface CuratedHistoryDialogProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  /** 目标人格主键（undefined = 当前激活人格）。 */
  personaId: number | undefined
  /** 历史链锚点（链内任一记忆主键；0 / null = 不拉取）。 */
  anchorMemoryId: number
}

/**
 * 策展记忆历史链对话框：supersede 链全量回溯（版本升序——现行版本
 * 恒在末行）。
 */
export function CuratedHistoryDialog({
  open,
  onOpenChange,
  personaId,
  anchorMemoryId,
}: CuratedHistoryDialogProps) {
  const historyQuery = useCuratedHistory(personaId, open ? anchorMemoryId : 0)
  const noTarget = anchorMemoryId <= 0

  return (
    <Dialog open={open} onOpenChange={onOpenChange} size="lg">
      <DialogHeader>
        <DialogTitle>主题历史链（supersede 链）</DialogTitle>
        <DialogClose onClose={() => onOpenChange(false)} />
      </DialogHeader>
      <DialogContent>
        {noTarget ? (
          <p className="py-4 text-sm text-[var(--color-text-muted)]">未选择记忆。</p>
        ) : historyQuery.isPending ? (
          <div className="flex items-center justify-center py-12">
            <Spinner size="lg" />
          </div>
        ) : historyQuery.isError ? (
          <p className="py-4 text-sm text-[var(--color-destructive)]" role="alert">
            历史链加载失败：{toApiError(historyQuery.error).message}
          </p>
        ) : historyQuery.data.length === 0 ? (
          <p className="py-4 text-sm text-[var(--color-text-muted)]">该主题暂无历史版本。</p>
        ) : (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead className="w-16">ID</TableHead>
                <TableHead className="w-20">类型</TableHead>
                <TableHead className="w-20">版本</TableHead>
                <TableHead className="w-24">状态</TableHead>
                <TableHead>正文</TableHead>
                <TableHead className="w-44">更新时间</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {historyQuery.data.map((record) => (
                <TableRow key={record.id} data-testid={`curated-history-row-${record.id}`}>
                  <TableCell>{record.id}</TableCell>
                  <TableCell>{CURATED_KIND_LABELS[record.kind]}</TableCell>
                  <TableCell>v{record.version}</TableCell>
                  <TableCell>
                    <Badge variant={CURATED_STATUS_BADGE_VARIANTS[record.status]}>
                      {CURATED_STATUS_LABELS[record.status]}
                    </Badge>
                  </TableCell>
                  <TableCell className="text-[var(--color-text-secondary)]">
                    {record.content}
                  </TableCell>
                  <TableCell className="text-[var(--color-text-secondary)]">
                    {formatEpochMs(record.updated_at)}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      </DialogContent>
      <DialogFooter>
        <Button onClick={() => onOpenChange(false)}>知道了</Button>
      </DialogFooter>
    </Dialog>
  )
}

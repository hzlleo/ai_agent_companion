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
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/Table'
import type { PersonaImportBatchItem, PersonaImportBatchResult } from '@/types/persona'

export interface PersonaImportBatchResultDialogProps {
  /** 对话框开闭（调用方条件挂载——结果即开态）。 */
  open: boolean
  /** 关闭请求（Radix 关闭面恒传 false——统一按关闭处理）。 */
  onOpenChange: (open: boolean) => void
  /** 批量导入结果（部分成功形态——条件挂载保证非空）。 */
  result: PersonaImportBatchResult
}

/** 单条目 → 结果徽章形态。 */
function itemBadge(item: PersonaImportBatchItem): { label: string; variant: 'success' | 'secondary' | 'destructive' } {
  if (!item.success) {
    return { label: '失败', variant: 'destructive' }
  }
  return item.imported
    ? { label: '已导入', variant: 'success' }
    : { label: '已跳过', variant: 'secondary' }
}

/**
 * 批量导入结果对话框：总数 + 成功/失败/新导入汇总 + 逐文件结果表。
 *
 * 后端契约为部分成功形态（逐文件独立事务）——解析失败记失败项；
 * 同卡幂等与名称冲突（skip 策略）是合法结果（已跳过），不阻塞其余
 * 文件。与人格 / 情景事件的批量删除结果对话框同模式。
 */
export function PersonaImportBatchResultDialog({
  open,
  onOpenChange,
  result,
}: PersonaImportBatchResultDialogProps) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange} size="lg">
      <DialogHeader>
        <DialogTitle>批量导入结果</DialogTitle>
        <DialogClose onClose={() => onOpenChange(false)} />
      </DialogHeader>
      <DialogContent>
        <div className="space-y-4">
          <div className="flex flex-wrap items-center gap-4 text-sm">
            <span>
              提交总数：<span className="font-semibold">{result.total}</span>
            </span>
            <span>
              处理成功：
              <span className="font-semibold text-[var(--color-success)]">{result.succeeded}</span>
            </span>
            <span>
              失败：
              <span className="font-semibold text-[var(--color-destructive)]">{result.failed}</span>
            </span>
            <span>
              新导入：
              <span className="font-semibold text-[var(--color-primary)]">{result.imported_count}</span>
            </span>
          </div>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>文件名</TableHead>
                <TableHead className="w-24">结果</TableHead>
                <TableHead>说明</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {result.items.map((item) => {
                const badge = itemBadge(item)
                return (
                  <TableRow key={item.filename}>
                    <TableCell className="font-medium">{item.filename}</TableCell>
                    <TableCell>
                      <Badge variant={badge.variant}>{badge.label}</Badge>
                    </TableCell>
                    <TableCell>
                      <span>
                        {item.message}
                        {item.code !== null && (
                          <span className="ml-1 text-[var(--color-text-muted)]">（{item.code}）</span>
                        )}
                      </span>
                    </TableCell>
                  </TableRow>
                )
              })}
            </TableBody>
          </Table>
        </div>
      </DialogContent>
      <DialogFooter>
        <Button onClick={() => onOpenChange(false)}>知道了</Button>
      </DialogFooter>
    </Dialog>
  )
}

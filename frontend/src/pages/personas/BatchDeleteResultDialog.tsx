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
import type { PersonaBatchDeleteResponse } from '@/types/persona'

export interface BatchDeleteResultDialogProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  /** 批量删除结果（部分成功形态）。 */
  result: PersonaBatchDeleteResponse | null
}

/**
 * 批量删除结果对话框：总数 + 成功/失败汇总 + 逐 id 结果表。
 *
 * 后端契约为部分成功形态（逐 id 独立事务）——失败项（如激活人格
 * 409 / 缺失 404）逐条呈现，不阻塞其余项。
 */
export function BatchDeleteResultDialog({ open, onOpenChange, result }: BatchDeleteResultDialogProps) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange} size="lg">
      <DialogHeader>
        <DialogTitle>批量删除结果</DialogTitle>
        <DialogClose onClose={() => onOpenChange(false)} />
      </DialogHeader>
      <DialogContent>
        {result !== null && (
          <div className="space-y-4">
            <div className="flex items-center gap-4 text-sm">
              <span>
                提交总数：<span className="font-semibold">{result.total}</span>
              </span>
              <span>
                成功：
                <span className="font-semibold text-[var(--color-success)]">{result.succeeded}</span>
              </span>
              <span>
                失败：
                <span className="font-semibold text-[var(--color-destructive)]">{result.failed}</span>
              </span>
            </div>
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead className="w-24">ID</TableHead>
                  <TableHead className="w-24">结果</TableHead>
                  <TableHead>说明</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {result.items.map((item) => (
                  <TableRow key={item.id}>
                    <TableCell>{item.id}</TableCell>
                    <TableCell>
                      {item.success ? (
                        <Badge variant="success">成功</Badge>
                      ) : (
                        <Badge variant="destructive">失败</Badge>
                      )}
                    </TableCell>
                    <TableCell>
                      {item.success ? (
                        <span className="text-[var(--color-text-muted)]">已删除</span>
                      ) : (
                        <span>
                          {item.message}
                          {item.code !== null && (
                            <span className="ml-1 text-[var(--color-text-muted)]">（{item.code}）</span>
                          )}
                        </span>
                      )}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>
        )}
      </DialogContent>
      <DialogFooter>
        <Button onClick={() => onOpenChange(false)}>知道了</Button>
      </DialogFooter>
    </Dialog>
  )
}

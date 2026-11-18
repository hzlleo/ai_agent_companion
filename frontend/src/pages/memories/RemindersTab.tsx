import { useMemo, useState } from 'react'
import { Plus } from 'lucide-react'
import { Button } from '@/components/ui/Button'
import { Badge } from '@/components/ui/Badge'
import { Select } from '@/components/ui/Select'
import { Spinner } from '@/components/ui/Spinner'
import { EmptyState } from '@/components/ui/EmptyState'
import { Alert, AlertDescription } from '@/components/ui/Alert'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/Table'
import { ConfirmDialog } from '@/components/ConfirmDialog'
import { BatchDeleteResultDialog } from '@/components/BatchDeleteResultDialog'
import { useToast } from '@/components/ui/useToast'
import { flattenCursorPages, isAllSelected } from '@/api/pagination'
import { toApiError } from '@/api/client'
import {
  useBatchCancelReminders,
  useCancelReminder,
  useRemindersList,
} from '@/hooks/useRemindersApi'
import { formatEpochMs } from '@/lib/datetime'
import type { ReminderBatchCancelResponse, ReminderRecord, ReminderStatus } from '@/types/reminders'
import { ReminderCreateDialog } from './ReminderCreateDialog'

export interface RemindersTabProps {
  /** 目标人格主键（undefined = 当前激活人格）。 */
  personaId: number | undefined
}

/** 状态过滤态（空串 = 全部）。 */
type ReminderStatusFilter = '' | ReminderStatus

/** 状态展示文案。 */
const STATUS_LABELS: Record<ReminderStatus, string> = {
  pending: '待投递',
  delivered: '已投递',
  cancelled: '已取消',
  failed: '投递失败',
}

/** 状态徽标变体（视觉分层——失败为破坏色）。 */
const STATUS_BADGE_VARIANTS: Record<
  ReminderStatus,
  'default' | 'secondary' | 'destructive'
> = {
  pending: 'default',
  delivered: 'secondary',
  cancelled: 'secondary',
  failed: 'destructive',
}

/** 确认动作形态（单条取消 / 批量取消）。 */
type ReminderConfirmAction =
  | { kind: 'cancel'; reminder: ReminderRecord }
  | { kind: 'batch-cancel'; ids: number[] }
  | null

/**
 * 提醒页签：游标分页表（状态过滤）+ 创建对话框 + 单条取消（确认）+
 * 批量取消（部分成功结果对话框）。
 *
 * 投递语义（页面提示）：到期提醒由后端扫描作业经属主通道投递——本页
 * 只读写库；无通道时提醒保持「待投递」（不消耗失败次数）。
 */
export function RemindersTab({ personaId }: RemindersTabProps) {
  const toast = useToast()
  const [statusFilter, setStatusFilter] = useState<ReminderStatusFilter>('')
  const [selectedIds, setSelectedIds] = useState<Set<number>>(new Set())
  const [confirmAction, setConfirmAction] = useState<ReminderConfirmAction>(null)
  const [createOpen, setCreateOpen] = useState(false)
  const [batchResult, setBatchResult] = useState<ReminderBatchCancelResponse | null>(null)
  const [batchResultOpen, setBatchResultOpen] = useState(false)

  const listQuery = useRemindersList({
    personaId,
    status: statusFilter === '' ? undefined : statusFilter,
    limit: 20,
  })
  const cancelMutation = useCancelReminder()
  const batchCancelMutation = useBatchCancelReminders()

  const reminders = useMemo(
    () => flattenCursorPages(listQuery.data?.pages ?? []),
    [listQuery.data],
  )
  const nonPendingIds = useMemo(
    () => new Set(reminders.filter((row) => row.status !== 'pending').map((row) => row.id)),
    [reminders],
  )
  const loadedIds = useMemo(() => reminders.map((reminder) => reminder.id), [reminders])

  const toggleSelected = (id: number, checked: boolean): void => {
    setSelectedIds((prev) => {
      const next = new Set(prev)
      if (checked) {
        next.add(id)
      } else {
        next.delete(id)
      }
      return next
    })
  }

  const toggleAll = (checked: boolean): void => {
    setSelectedIds(checked ? new Set(loadedIds) : new Set())
  }

  const handleConfirm = (): void => {
    if (confirmAction === null) {
      return
    }
    if (confirmAction.kind === 'cancel') {
      cancelMutation.mutate(
        { personaId, reminderId: confirmAction.reminder.id },
        {
          onSuccess: () => {
            toast.success(`提醒 #${confirmAction.reminder.id} 已取消`)
            setConfirmAction(null)
          },
          onError: (error) => {
            toast.error(toApiError(error).message)
            setConfirmAction(null)
          },
        },
      )
      return
    }
    batchCancelMutation.mutate(
      { personaId, ids: confirmAction.ids },
      {
        onSuccess: (result) => {
          setBatchResult(result)
          setBatchResultOpen(true)
          setSelectedIds(new Set())
          setConfirmAction(null)
          toast.success(`批量取消完成（成功 ${result.succeeded} / 失败 ${result.failed}）`)
        },
        onError: (error) => {
          toast.error(toApiError(error).message)
          setConfirmAction(null)
        },
      },
    )
  }

  return (
    <div>
      <div className="mb-4 flex flex-wrap items-center gap-3">
        <label
          htmlFor="reminder-status-filter"
          className="text-sm text-[var(--color-text-secondary)]"
        >
          状态
        </label>
        <Select
          id="reminder-status-filter"
          className="w-32"
          value={statusFilter}
          onChange={(e) => {
            setStatusFilter(e.target.value as ReminderStatusFilter)
            setSelectedIds(new Set())
          }}
        >
          <option value="">全部</option>
          {Object.entries(STATUS_LABELS).map(([value, label]) => (
            <option key={value} value={value}>
              {label}
            </option>
          ))}
        </Select>
        <div className="ml-auto flex items-center gap-2">
          <Button
            size="sm"
            variant="destructive"
            disabled={selectedIds.size === 0}
            onClick={() => setConfirmAction({ kind: 'batch-cancel', ids: [...selectedIds] })}
          >
            批量取消（已选 {selectedIds.size}）
          </Button>
          <Button size="sm" onClick={() => setCreateOpen(true)}>
            <Plus className="mr-1 h-4 w-4" aria-hidden="true" />
            新建提醒
          </Button>
        </div>
      </div>

      {listQuery.isPending ? (
        <div className="flex items-center justify-center py-16">
          <Spinner size="lg" />
        </div>
      ) : listQuery.isError ? (
        <Alert variant="destructive">
          <AlertDescription>
            <p>提醒加载失败：{toApiError(listQuery.error).message}</p>
            <Button
              variant="outline"
              size="sm"
              className="mt-2"
              onClick={() => {
                void listQuery.refetch()
              }}
            >
              重试
            </Button>
          </AlertDescription>
        </Alert>
      ) : reminders.length === 0 ? (
        <EmptyState
          title="暂无提醒"
          description="当前过滤条件下没有提醒；可新建提醒或调整过滤条件。"
        />
      ) : (
        <div className="rounded-xl border border-[var(--color-border)]">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead className="w-12">
                  <input
                    type="checkbox"
                    aria-label="全选提醒"
                    checked={isAllSelected(loadedIds, selectedIds)}
                    onChange={(e) => toggleAll(e.target.checked)}
                  />
                </TableHead>
                <TableHead className="w-16">ID</TableHead>
                <TableHead>标题</TableHead>
                <TableHead className="w-44">到期时间</TableHead>
                <TableHead className="w-24">状态</TableHead>
                <TableHead className="w-20">尝试次数</TableHead>
                <TableHead className="w-24">操作</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {reminders.map((reminder) => (
                <TableRow key={reminder.id} data-testid={`reminder-row-${reminder.id}`}>
                  <TableCell>
                    <input
                      type="checkbox"
                      aria-label={`选择提醒 ${reminder.id}`}
                      checked={selectedIds.has(reminder.id)}
                      onChange={(e) => toggleSelected(reminder.id, e.target.checked)}
                    />
                  </TableCell>
                  <TableCell>{reminder.id}</TableCell>
                  <TableCell className="font-medium">{reminder.title}</TableCell>
                  <TableCell>{formatEpochMs(reminder.due_at_ms)}</TableCell>
                  <TableCell>
                    <Badge variant={STATUS_BADGE_VARIANTS[reminder.status]}>
                      {STATUS_LABELS[reminder.status]}
                    </Badge>
                  </TableCell>
                  <TableCell>{reminder.delivery_attempts}</TableCell>
                  <TableCell>
                    <Button
                      variant="ghost"
                      size="sm"
                      disabled={nonPendingIds.has(reminder.id)}
                      onClick={() => setConfirmAction({ kind: 'cancel', reminder })}
                    >
                      取消
                    </Button>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
          <div className="flex items-center justify-center border-t border-[var(--color-border)] p-4">
            {listQuery.hasNextPage ? (
              <Button
                variant="outline"
                loading={listQuery.isFetchingNextPage}
                onClick={() => {
                  void listQuery.fetchNextPage()
                }}
              >
                加载更多
              </Button>
            ) : (
              <span className="text-sm text-[var(--color-text-muted)]">已加载全部</span>
            )}
          </div>
        </div>
      )}

      {confirmAction !== null && (
        <ConfirmDialog
          open
          onOpenChange={(open) => {
            if (!open) setConfirmAction(null)
          }}
          title={confirmAction.kind === 'cancel' ? '取消提醒' : '批量取消提醒'}
          description={
            confirmAction.kind === 'cancel' ? (
              <span>
                确认取消提醒 #{confirmAction.reminder.id}（「{confirmAction.reminder.title}」）？
                取消后不再投递，且不可恢复。
              </span>
            ) : (
              <span>
                确认取消选中的 {confirmAction.ids.length} 条提醒？逐项独立执行：
                已投递 / 已取消 / 缺失的项将记为失败，不阻塞其余项。
              </span>
            )
          }
          confirmText={confirmAction.kind === 'cancel' ? '取消提醒' : '批量取消'}
          destructive
          loading={cancelMutation.isPending || batchCancelMutation.isPending}
          onConfirm={handleConfirm}
        />
      )}

      <ReminderCreateDialog open={createOpen} onOpenChange={setCreateOpen} personaId={personaId} />

      <BatchDeleteResultDialog
        open={batchResultOpen}
        onOpenChange={setBatchResultOpen}
        result={batchResult}
      />
    </div>
  )
}

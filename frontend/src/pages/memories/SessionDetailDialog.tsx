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
import { toApiError } from '@/api/client'
import { formatEpochMs } from '@/lib/datetime'
import { useSessionDetail } from '@/hooks/useMemoriesApi'
import type { ChatMessageRole } from '@/types/memory'

export interface SessionDetailDialogProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  /** 目标人格主键（undefined = 当前激活人格）。 */
  personaId: number | undefined
  /** 会话主键（0 = 不拉取）。 */
  sessionId: number
}

/** 消息角色 → 中文标签 + 徽章变体。 */
const ROLE_LABELS: Record<ChatMessageRole, { label: string; variant: 'info' | 'success' | 'secondary' }> = {
  user: { label: '用户', variant: 'info' },
  assistant: { label: '人格', variant: 'success' },
  system: { label: '系统', variant: 'secondary' },
}

/**
 * 会话详情对话框：摘要头 + 全量消息列表（seq 升序——时间序回放形态）。
 */
export function SessionDetailDialog({
  open,
  onOpenChange,
  personaId,
  sessionId,
}: SessionDetailDialogProps) {
  const detailQuery = useSessionDetail(personaId, open ? sessionId : 0)
  const noTarget = sessionId <= 0

  return (
    <Dialog open={open} onOpenChange={onOpenChange} size="lg">
      <DialogHeader>
        <DialogTitle>会话详情</DialogTitle>
        <DialogClose onClose={() => onOpenChange(false)} />
      </DialogHeader>
      <DialogContent>
        {noTarget ? (
          <p className="py-4 text-sm text-[var(--color-text-muted)]">未选择会话。</p>
        ) : detailQuery.isPending ? (
          <div className="flex items-center justify-center py-12">
            <Spinner size="lg" />
          </div>
        ) : detailQuery.isError ? (
          <p className="py-4 text-sm text-[var(--color-destructive)]" role="alert">
            会话详情加载失败：{toApiError(detailQuery.error).message}
          </p>
        ) : (
          <div className="space-y-4">
            <div className="flex flex-wrap items-center gap-3 text-sm text-[var(--color-text-secondary)]">
              <span className="font-medium text-[var(--color-text)]">{detailQuery.data.title}</span>
              <Badge variant="secondary">会话 #{detailQuery.data.id}</Badge>
              <Badge variant="secondary">{detailQuery.data.message_count} 条消息</Badge>
              <span>创建于 {formatEpochMs(detailQuery.data.created_at)}</span>
            </div>
            <ol className="space-y-2">
              {detailQuery.data.messages.map((message) => {
                const role = ROLE_LABELS[message.role]
                return (
                  <li
                    key={message.id}
                    data-testid={`session-message-${message.seq}`}
                    className="rounded-md border border-[var(--color-border)] p-3"
                  >
                    <div className="mb-1 flex items-center gap-2">
                      <Badge variant={role.variant}>{role.label}</Badge>
                      <span className="text-xs text-[var(--color-text-muted)]">
                        #{message.seq} · {formatEpochMs(message.ts_ms)}
                      </span>
                    </div>
                    <p className="whitespace-pre-wrap text-sm text-[var(--color-text)]">
                      {message.content}
                    </p>
                  </li>
                )
              })}
            </ol>
          </div>
        )}
      </DialogContent>
      <DialogFooter>
        <Button onClick={() => onOpenChange(false)}>知道了</Button>
      </DialogFooter>
    </Dialog>
  )
}

import { useMemo, useState } from 'react'
import { Button } from '@/components/ui/Button'
import { Badge } from '@/components/ui/Badge'
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
import { useToast } from '@/components/ui/useToast'
import { flattenCursorPages } from '@/api/pagination'
import { toApiError } from '@/api/client'
import { formatEpochMs } from '@/lib/datetime'
import { useDeleteSession, useSessionsList } from '@/hooks/useMemoriesApi'
import type { ChatSessionSummary } from '@/types/memory'
import { SessionDetailDialog } from './SessionDetailDialog'

export interface SessionsTabProps {
  /** 目标人格主键（undefined = 当前激活人格）。 */
  personaId: number | undefined
}

/**
 * 会话页签：游标分页表 + 详情对话框（消息列表 seq 升序）+ 删除
 * （确认——消息同事务清理）。
 */
export function SessionsTab({ personaId }: SessionsTabProps) {
  const toast = useToast()
  const [deleteTarget, setDeleteTarget] = useState<ChatSessionSummary | null>(null)
  const [detailSessionId, setDetailSessionId] = useState<number | null>(null)

  const listQuery = useSessionsList(personaId, 20)
  const deleteMutation = useDeleteSession()

  const sessions = useMemo(
    () => flattenCursorPages(listQuery.data?.pages ?? []),
    [listQuery.data],
  )

  return (
    <div>
      {listQuery.isPending ? (
        <div className="flex items-center justify-center py-16">
          <Spinner size="lg" />
        </div>
      ) : listQuery.isError ? (
        <Alert variant="destructive">
          <AlertDescription>
            <p>会话列表加载失败：{toApiError(listQuery.error).message}</p>
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
      ) : sessions.length === 0 ? (
        <EmptyState
          title="暂无会话"
          description="该人格作用域下还没有持久化的对话会话。"
        />
      ) : (
        <div className="rounded-xl border border-[var(--color-border)]">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead className="w-16">ID</TableHead>
                <TableHead>标题</TableHead>
                <TableHead className="w-24">消息数</TableHead>
                <TableHead className="w-44">创建时间</TableHead>
                <TableHead className="w-44">最后活跃</TableHead>
                <TableHead className="w-32">操作</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {sessions.map((session) => (
                <TableRow key={session.id} data-testid={`session-row-${session.id}`}>
                  <TableCell>{session.id}</TableCell>
                  <TableCell className="font-medium">{session.title}</TableCell>
                  <TableCell>
                    <Badge variant="secondary">{session.message_count} 条</Badge>
                  </TableCell>
                  <TableCell className="text-[var(--color-text-secondary)]">
                    {formatEpochMs(session.created_at)}
                  </TableCell>
                  <TableCell className="text-[var(--color-text-secondary)]">
                    {formatEpochMs(session.last_active_at)}
                  </TableCell>
                  <TableCell>
                    <div className="flex flex-wrap items-center gap-1">
                      <Button
                        variant="outline"
                        size="sm"
                        onClick={() => setDetailSessionId(session.id)}
                      >
                        详情
                      </Button>
                      <Button
                        variant="ghost"
                        size="sm"
                        className="text-[var(--color-destructive)] hover:bg-[var(--color-destructive-light)]"
                        onClick={() => setDeleteTarget(session)}
                      >
                        删除
                      </Button>
                    </div>
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

      {deleteTarget !== null && (
        <ConfirmDialog
          open
          onOpenChange={(open) => {
            if (!open) setDeleteTarget(null)
          }}
          title="删除会话"
          description={
            <span>
              确认删除会话 #{deleteTarget.id}「{deleteTarget.title}」？该会话的全部消息
              （{deleteTarget.message_count} 条）将同事务清理，操作不可恢复。
            </span>
          }
          confirmText="删除"
          destructive
          loading={deleteMutation.isPending}
          onConfirm={() => {
            deleteMutation.mutate(
              { personaId, sessionId: deleteTarget.id },
              {
                onSuccess: () => {
                  toast.success(`会话 #${deleteTarget.id} 已删除（消息同事务清理）`)
                  setDeleteTarget(null)
                },
                onError: (error) => {
                  toast.error(toApiError(error).message)
                  setDeleteTarget(null)
                },
              },
            )
          }}
        />
      )}

      <SessionDetailDialog
        open={detailSessionId !== null}
        onOpenChange={(open) => {
          if (!open) setDetailSessionId(null)
        }}
        personaId={personaId}
        sessionId={detailSessionId ?? 0}
      />
    </div>
  )
}

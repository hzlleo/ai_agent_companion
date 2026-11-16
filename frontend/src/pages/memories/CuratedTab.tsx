import { useMemo, useState } from 'react'
import { Plus } from 'lucide-react'
import { Button } from '@/components/ui/Button'
import { Badge } from '@/components/ui/Badge'
import { Input } from '@/components/ui/Input'
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
import { useToast } from '@/components/ui/useToast'
import { flattenCursorPages } from '@/api/pagination'
import { toApiError } from '@/api/client'
import { useCuratedList, useDeleteCurated } from '@/hooks/useMemoriesApi'
import type { CuratedKind, CuratedRecord, CuratedStatus } from '@/types/memory'
import {
  CURATED_KIND_LABELS,
  CURATED_STATUS_BADGE_VARIANTS,
  CURATED_STATUS_LABELS,
  PROVENANCE_BADGE_VARIANTS,
  PROVENANCE_LABELS,
  truncateText,
} from './memoryDisplay'
import { CuratedUpsertDialog } from './CuratedUpsertDialog'
import { CuratedHistoryDialog } from './CuratedHistoryDialog'

export interface CuratedTabProps {
  /** 目标人格主键（undefined = 当前激活人格）。 */
  personaId: number | undefined
}

/** 类型过滤态（空串 = 全部）。 */
type CuratedKindFilter = '' | CuratedKind

/** 状态过滤态（空串 = 全部）。 */
type CuratedStatusFilter = '' | CuratedStatus

/**
 * 策展记忆页签：游标分页表（kind / topic / status 过滤）+ upsert
 * 对话框（supersede 语义）+ 历史链对话框 + 删除（确认）。
 */
export function CuratedTab({ personaId }: CuratedTabProps) {
  const toast = useToast()
  const [kindFilter, setKindFilter] = useState<CuratedKindFilter>('')
  const [statusFilter, setStatusFilter] = useState<CuratedStatusFilter>('')
  const [topicFilter, setTopicFilter] = useState('')
  const [deleteTarget, setDeleteTarget] = useState<CuratedRecord | null>(null)
  const [upsertOpen, setUpsertOpen] = useState(false)
  const [upsertInitial, setUpsertInitial] = useState<CuratedRecord | null>(null)
  const [historyAnchorId, setHistoryAnchorId] = useState<number | null>(null)

  const listQuery = useCuratedList({
    personaId,
    kind: kindFilter === '' ? undefined : kindFilter,
    topic: topicFilter.trim() === '' ? undefined : topicFilter.trim(),
    status: statusFilter === '' ? undefined : statusFilter,
    limit: 20,
  })
  const deleteMutation = useDeleteCurated()

  const records = useMemo(
    () => flattenCursorPages(listQuery.data?.pages ?? []),
    [listQuery.data],
  )

  return (
    <div>
      <div className="mb-4 flex flex-wrap items-center gap-3">
        <label htmlFor="curated-kind-filter" className="text-sm text-[var(--color-text-secondary)]">
          类型
        </label>
        <Select
          id="curated-kind-filter"
          className="w-32"
          value={kindFilter}
          onChange={(e) => setKindFilter(e.target.value as CuratedKindFilter)}
        >
          <option value="">全部</option>
          {Object.entries(CURATED_KIND_LABELS).map(([value, label]) => (
            <option key={value} value={value}>
              {label}
            </option>
          ))}
        </Select>
        <label
          htmlFor="curated-status-filter"
          className="text-sm text-[var(--color-text-secondary)]"
        >
          状态
        </label>
        <Select
          id="curated-status-filter"
          className="w-32"
          value={statusFilter}
          onChange={(e) => setStatusFilter(e.target.value as CuratedStatusFilter)}
        >
          <option value="">全部</option>
          {Object.entries(CURATED_STATUS_LABELS).map(([value, label]) => (
            <option key={value} value={value}>
              {label}
            </option>
          ))}
        </Select>
        <label htmlFor="curated-topic-filter" className="text-sm text-[var(--color-text-secondary)]">
          主题键
        </label>
        <Input
          id="curated-topic-filter"
          className="w-48"
          value={topicFilter}
          onChange={(e) => setTopicFilter(e.target.value)}
          placeholder="精确匹配（留空 = 全部）"
        />
        <div className="ml-auto">
          <Button
            size="sm"
            onClick={() => {
              setUpsertInitial(null)
              setUpsertOpen(true)
            }}
          >
            <Plus className="mr-1 h-4 w-4" aria-hidden="true" />
            新建策展记忆
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
            <p>策展记忆加载失败：{toApiError(listQuery.error).message}</p>
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
      ) : records.length === 0 ? (
        <EmptyState
          title="暂无策展记忆"
          description="当前过滤条件下没有策展记忆；可新建或调整过滤条件。"
        />
      ) : (
        <div className="rounded-xl border border-[var(--color-border)]">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead className="w-16">ID</TableHead>
                <TableHead className="w-24">类型</TableHead>
                <TableHead className="w-36">主题键</TableHead>
                <TableHead>正文</TableHead>
                <TableHead className="w-16">重要度</TableHead>
                <TableHead className="w-20">可信度</TableHead>
                <TableHead className="w-20">状态</TableHead>
                <TableHead className="w-16">版本</TableHead>
                <TableHead className="w-28">操作</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {records.map((record) => (
                <TableRow key={record.id} data-testid={`curated-row-${record.id}`}>
                  <TableCell>{record.id}</TableCell>
                  <TableCell>
                    <Badge variant="secondary">{CURATED_KIND_LABELS[record.kind]}</Badge>
                  </TableCell>
                  <TableCell className="font-medium">{record.topic}</TableCell>
                  <TableCell className="text-[var(--color-text-secondary)]">
                    {truncateText(record.content)}
                  </TableCell>
                  <TableCell>{record.importance}</TableCell>
                  <TableCell>
                    <Badge variant={PROVENANCE_BADGE_VARIANTS[record.provenance]}>
                      {PROVENANCE_LABELS[record.provenance]}
                    </Badge>
                  </TableCell>
                  <TableCell>
                    <Badge variant={CURATED_STATUS_BADGE_VARIANTS[record.status]}>
                      {CURATED_STATUS_LABELS[record.status]}
                    </Badge>
                  </TableCell>
                  <TableCell>v{record.version}</TableCell>
                  <TableCell>
                    <div className="flex flex-wrap items-center gap-1">
                      <Button
                        variant="outline"
                        size="sm"
                        onClick={() => {
                          setUpsertInitial(record)
                          setUpsertOpen(true)
                        }}
                      >
                        编辑
                      </Button>
                      <Button
                        variant="ghost"
                        size="sm"
                        onClick={() => setHistoryAnchorId(record.id)}
                      >
                        历史
                      </Button>
                      <Button
                        variant="ghost"
                        size="sm"
                        className="text-[var(--color-destructive)] hover:bg-[var(--color-destructive-light)]"
                        onClick={() => setDeleteTarget(record)}
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
          title="删除策展记忆"
          description={
            <span>
              确认删除策展记忆 #{deleteTarget.id}（主题「{deleteTarget.topic}」v
              {deleteTarget.version}）？该操作不可恢复；历史链中其余版本不受影响。
            </span>
          }
          confirmText="删除"
          destructive
          loading={deleteMutation.isPending}
          onConfirm={() => {
            deleteMutation.mutate(
              { personaId, memoryId: deleteTarget.id },
              {
                onSuccess: () => {
                  toast.success(`策展记忆 #${deleteTarget.id} 已删除`)
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

      <CuratedUpsertDialog
        open={upsertOpen}
        onOpenChange={setUpsertOpen}
        personaId={personaId}
        initial={upsertInitial}
      />

      <CuratedHistoryDialog
        open={historyAnchorId !== null}
        onOpenChange={(open) => {
          if (!open) setHistoryAnchorId(null)
        }}
        personaId={personaId}
        anchorMemoryId={historyAnchorId ?? 0}
      />
    </div>
  )
}

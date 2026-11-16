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
import { BatchDeleteResultDialog } from '@/components/BatchDeleteResultDialog'
import { useToast } from '@/components/ui/useToast'
import { flattenCursorPages, isAllSelected } from '@/api/pagination'
import { toApiError } from '@/api/client'
import { datetimeLocalToEpochMs, formatEpochMs } from '@/lib/datetime'
import { useBatchDeleteEpisodes, useDeleteEpisode, useEpisodesList } from '@/hooks/useMemoriesApi'
import type {
  EpisodeBatchDeleteResponse,
  EpisodeKind,
  EpisodeRecord,
  MemoryProvenance,
} from '@/types/memory'
import {
  EPISODE_KIND_LABELS,
  PROVENANCE_BADGE_VARIANTS,
  PROVENANCE_LABELS,
  episodePayloadSummary,
} from './memoryDisplay'
import { EpisodeInjectDialog } from './EpisodeInjectDialog'

export interface EpisodesTabProps {
  /** 目标人格主键（undefined = 当前激活人格）。 */
  personaId: number | undefined
}

/** 类型过滤态（空串 = 全部）。 */
type KindFilter = '' | EpisodeKind

/** 可信度过滤态（空串 = 全部）。 */
type ProvenanceFilter = '' | MemoryProvenance

/** 待确认操作（单条删除 / 批量删除）。 */
type EpisodeConfirmAction =
  | { kind: 'delete'; episode: EpisodeRecord }
  | { kind: 'batch-delete'; ids: number[] }
  | null

/**
 * 情景事件页签：游标分页表（kind / provenance / 时间范围过滤）+
 * 手工注入对话框 + 单条删除（确认）+ 批量删除（部分成功结果对话框）。
 */
export function EpisodesTab({ personaId }: EpisodesTabProps) {
  const toast = useToast()
  const [kindFilter, setKindFilter] = useState<KindFilter>('')
  const [provenanceFilter, setProvenanceFilter] = useState<ProvenanceFilter>('')
  const [startText, setStartText] = useState('')
  const [endText, setEndText] = useState('')
  const [selectedIds, setSelectedIds] = useState<Set<number>>(new Set())
  const [confirmAction, setConfirmAction] = useState<EpisodeConfirmAction>(null)
  const [injectOpen, setInjectOpen] = useState(false)
  const [batchResult, setBatchResult] = useState<EpisodeBatchDeleteResponse | null>(null)
  const [batchResultOpen, setBatchResultOpen] = useState(false)

  const listQuery = useEpisodesList({
    personaId,
    kind: kindFilter === '' ? undefined : kindFilter,
    provenance: provenanceFilter === '' ? undefined : provenanceFilter,
    startMs: datetimeLocalToEpochMs(startText) ?? undefined,
    endMs: datetimeLocalToEpochMs(endText) ?? undefined,
    limit: 20,
  })
  const deleteMutation = useDeleteEpisode()
  const batchDeleteMutation = useBatchDeleteEpisodes()

  const episodes = useMemo(
    () => flattenCursorPages(listQuery.data?.pages ?? []),
    [listQuery.data],
  )
  const loadedIds = useMemo(() => episodes.map((episode) => episode.id), [episodes])

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

  const handleConfirm = (): void => {
    if (confirmAction === null) {
      return
    }
    if (confirmAction.kind === 'delete') {
      deleteMutation.mutate(
        { personaId, episodeId: confirmAction.episode.id },
        {
          onSuccess: () => {
            toast.success(`情景事件 #${confirmAction.episode.id} 已删除`)
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
    batchDeleteMutation.mutate(
      { personaId, ids: confirmAction.ids },
      {
        onSuccess: (result) => {
          setConfirmAction(null)
          setSelectedIds(new Set())
          if (result.failed > 0) {
            setBatchResult(result)
            setBatchResultOpen(true)
            toast.warning(`批量删除完成（成功 ${result.succeeded} / 失败 ${result.failed}）`)
          } else {
            toast.success(`批量删除完成（成功 ${result.succeeded} / 失败 0）`)
          }
        },
        onError: (error) => {
          toast.error(toApiError(error).message)
          setConfirmAction(null)
        },
      },
    )
  }

  const confirmLoading =
    (confirmAction?.kind === 'delete' && deleteMutation.isPending) ||
    (confirmAction?.kind === 'batch-delete' && batchDeleteMutation.isPending)

  return (
    <div>
      <div className="mb-4 flex flex-wrap items-center gap-3">
        <label htmlFor="episode-kind-filter" className="text-sm text-[var(--color-text-secondary)]">
          类型
        </label>
        <Select
          id="episode-kind-filter"
          className="w-36"
          value={kindFilter}
          onChange={(e) => {
            setKindFilter(e.target.value as KindFilter)
            setSelectedIds(new Set())
          }}
        >
          <option value="">全部</option>
          {Object.entries(EPISODE_KIND_LABELS).map(([value, label]) => (
            <option key={value} value={value}>
              {label}
            </option>
          ))}
        </Select>
        <label
          htmlFor="episode-provenance-filter"
          className="text-sm text-[var(--color-text-secondary)]"
        >
          可信度
        </label>
        <Select
          id="episode-provenance-filter"
          className="w-32"
          value={provenanceFilter}
          onChange={(e) => {
            setProvenanceFilter(e.target.value as ProvenanceFilter)
            setSelectedIds(new Set())
          }}
        >
          <option value="">全部</option>
          {Object.entries(PROVENANCE_LABELS).map(([value, label]) => (
            <option key={value} value={value}>
              {label}
            </option>
          ))}
        </Select>
        <label htmlFor="episode-start-filter" className="text-sm text-[var(--color-text-secondary)]">
          时间范围
        </label>
        <Input
          id="episode-start-filter"
          type="datetime-local"
          className="w-52"
          value={startText}
          onChange={(e) => {
            setStartText(e.target.value)
            setSelectedIds(new Set())
          }}
        />
        <span className="text-sm text-[var(--color-text-muted)]">至</span>
        <Input
          id="episode-end-filter"
          type="datetime-local"
          className="w-52"
          value={endText}
          onChange={(e) => {
            setEndText(e.target.value)
            setSelectedIds(new Set())
          }}
        />
        <div className="ml-auto">
          <Button
            size="sm"
            variant="destructive"
            disabled={selectedIds.size === 0}
            onClick={() => setConfirmAction({ kind: 'batch-delete', ids: [...selectedIds] })}
          >
            批量删除（已选 {selectedIds.size}）
          </Button>
        </div>
        <Button size="sm" onClick={() => setInjectOpen(true)}>
          <Plus className="mr-1 h-4 w-4" aria-hidden="true" />
          手工注入
        </Button>
      </div>

      {listQuery.isPending ? (
        <div className="flex items-center justify-center py-16">
          <Spinner size="lg" />
        </div>
      ) : listQuery.isError ? (
        <Alert variant="destructive">
          <AlertDescription>
            <p>情景事件加载失败：{toApiError(listQuery.error).message}</p>
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
      ) : episodes.length === 0 ? (
        <EmptyState
          title="暂无情景事件"
          description="当前过滤条件下没有事件；可通过「手工注入」添加，或调整过滤条件。"
        />
      ) : (
        <div className="rounded-xl border border-[var(--color-border)]">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead className="w-10">
                  <input
                    type="checkbox"
                    aria-label="全选当前已加载事件"
                    checked={isAllSelected(loadedIds, selectedIds)}
                    onChange={(e) =>
                      setSelectedIds(e.target.checked ? new Set(loadedIds) : new Set())
                    }
                  />
                </TableHead>
                <TableHead className="w-16">ID</TableHead>
                <TableHead className="w-24">类型</TableHead>
                <TableHead className="w-32">来源</TableHead>
                <TableHead>载荷摘要</TableHead>
                <TableHead className="w-16">重要度</TableHead>
                <TableHead className="w-20">可信度</TableHead>
                <TableHead className="w-44">事件时间</TableHead>
                <TableHead className="w-20">操作</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {episodes.map((episode) => (
                <TableRow key={episode.id} data-testid={`episode-row-${episode.id}`}>
                  <TableCell>
                    <input
                      type="checkbox"
                      aria-label={`选择事件 ${episode.id}`}
                      checked={selectedIds.has(episode.id)}
                      onChange={(e) => toggleSelected(episode.id, e.target.checked)}
                    />
                  </TableCell>
                  <TableCell>{episode.id}</TableCell>
                  <TableCell>
                    <Badge variant="secondary">{EPISODE_KIND_LABELS[episode.kind]}</Badge>
                  </TableCell>
                  <TableCell className="font-medium">{episode.source}</TableCell>
                  <TableCell className="text-[var(--color-text-secondary)]">
                    {episodePayloadSummary(episode.kind, episode.content)}
                  </TableCell>
                  <TableCell>{episode.importance}</TableCell>
                  <TableCell>
                    <Badge variant={PROVENANCE_BADGE_VARIANTS[episode.provenance]}>
                      {PROVENANCE_LABELS[episode.provenance]}
                    </Badge>
                  </TableCell>
                  <TableCell className="text-[var(--color-text-secondary)]">
                    {formatEpochMs(episode.ts_ms)}
                  </TableCell>
                  <TableCell>
                    <Button
                      variant="ghost"
                      size="sm"
                      className="text-[var(--color-destructive)] hover:bg-[var(--color-destructive-light)]"
                      onClick={() => setConfirmAction({ kind: 'delete', episode })}
                    >
                      删除
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
          title={confirmAction.kind === 'delete' ? '删除情景事件' : '批量删除情景事件'}
          description={
            confirmAction.kind === 'delete' ? (
              <span>
                确认删除情景事件 #{confirmAction.episode.id}（{EPISODE_KIND_LABELS[confirmAction.episode.kind]}）？
                情景层为 append-only 日志，删除是管理面显式动作且不可恢复。
              </span>
            ) : (
              <span>
                确认删除选中的 {confirmAction.ids.length} 条情景事件？逐项独立执行：
                不在当前人格作用域或缺失的项将记为失败，不阻塞其余项。
              </span>
            )
          }
          confirmText={confirmAction.kind === 'delete' ? '删除' : '批量删除'}
          destructive
          loading={confirmLoading}
          onConfirm={handleConfirm}
        />
      )}

      <EpisodeInjectDialog
        open={injectOpen}
        onOpenChange={setInjectOpen}
        personaId={personaId}
      />

      <BatchDeleteResultDialog
        open={batchResultOpen}
        onOpenChange={setBatchResultOpen}
        result={batchResult}
      />
    </div>
  )
}

import { useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
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
import { useToast } from '@/components/ui/useToast'
import { flattenCursorPages, isAllSelected } from '@/api/pagination'
import { toApiError } from '@/api/client'
import {
  useActivatePersona,
  useBatchDeletePersonas,
  useDeactivatePersona,
  useDeletePersona,
  usePersonasList,
} from '@/hooks/usePersonasApi'
import { formatEpochMs } from '@/lib/datetime'
import type {
  PersonaBatchDeleteResponse,
  PersonaRecord,
  PersonaStatus,
} from '@/types/persona'
import { CopyPersonaDialog } from './CopyPersonaDialog'
import { BatchDeleteResultDialog } from '@/components/BatchDeleteResultDialog'

/** 状态过滤态（空串 = 全部）。 */
type StatusFilter = '' | PersonaStatus

/** 待确认操作（激活 / 停用 / 删除 / 批量删除）。 */
type ConfirmAction =
  | { kind: 'activate'; persona: PersonaRecord }
  | { kind: 'deactivate'; persona: PersonaRecord }
  | { kind: 'delete'; persona: PersonaRecord }
  | { kind: 'batch-delete'; ids: number[] }
  | null

/** 状态 → 徽章形态。 */
function statusBadge(status: PersonaStatus): { label: string; variant: 'success' | 'secondary' } {
  return status === 'active'
    ? { label: '启用', variant: 'success' }
    : { label: '停用', variant: 'secondary' }
}

/**
 * 人格管理列表页。
 *
 * 数据面：游标分页（load more）+ status 过滤；操作面：激活/停用
 * （确认后事务性切换）、复制（对话框，可选深拷贝状态）、删除
 * （激活守卫 409 经 toast 呈现后端提示）、批量删除（部分成功结果
 * 对话框逐项呈现）。
 */
export default function PersonaListPage() {
  const toast = useToast()
  const [statusFilter, setStatusFilter] = useState<StatusFilter>('')
  const [selectedIds, setSelectedIds] = useState<Set<number>>(new Set())
  const [confirmAction, setConfirmAction] = useState<ConfirmAction>(null)
  const [copyTarget, setCopyTarget] = useState<PersonaRecord | null>(null)
  const [copyOpen, setCopyOpen] = useState(false)
  const [batchResult, setBatchResult] = useState<PersonaBatchDeleteResponse | null>(null)
  const [batchResultOpen, setBatchResultOpen] = useState(false)

  const listQuery = usePersonasList({
    status: statusFilter === '' ? undefined : statusFilter,
    limit: 20,
  })
  const activateMutation = useActivatePersona()
  const deactivateMutation = useDeactivatePersona()
  const deleteMutation = useDeletePersona()
  const batchDeleteMutation = useBatchDeletePersonas()

  const personas = useMemo(
    () => flattenCursorPages(listQuery.data?.pages ?? []),
    [listQuery.data],
  )
  const loadedIds = useMemo(() => personas.map((persona) => persona.id), [personas])

  /** 单行勾选切换。 */
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

  /** 全选/全不选（作用于当前已加载行）。 */
  const toggleSelectAll = (checked: boolean): void => {
    setSelectedIds(checked ? new Set(loadedIds) : new Set())
  }

  /** 确认操作分发（各操作经 msw 端点落库，错误 toast 后端 message）。 */
  const handleConfirm = (): void => {
    if (confirmAction === null) {
      return
    }
    switch (confirmAction.kind) {
      case 'activate': {
        activateMutation.mutate(confirmAction.persona.id, {
          onSuccess: () => {
            toast.success(`人格「${confirmAction.persona.name}」已激活（单激活切换）`)
            setConfirmAction(null)
          },
          onError: (error) => {
            toast.error(toApiError(error).message)
            setConfirmAction(null)
          },
        })
        break
      }
      case 'deactivate': {
        deactivateMutation.mutate(confirmAction.persona.id, {
          onSuccess: () => {
            toast.success(`人格「${confirmAction.persona.name}」激活位已停用`)
            setConfirmAction(null)
          },
          onError: (error) => {
            toast.error(toApiError(error).message)
            setConfirmAction(null)
          },
        })
        break
      }
      case 'delete': {
        deleteMutation.mutate(confirmAction.persona.id, {
          onSuccess: () => {
            toast.success(`人格「${confirmAction.persona.name}」已删除`)
            setConfirmAction(null)
          },
          onError: (error) => {
            // 激活守卫（409）等后端语义直接透出 message。
            toast.error(toApiError(error).message)
            setConfirmAction(null)
          },
        })
        break
      }
      case 'batch-delete': {
        batchDeleteMutation.mutate(confirmAction.ids, {
          onSuccess: (result) => {
            setConfirmAction(null)
            setSelectedIds(new Set())
            if (result.failed > 0) {
              // 部分成功：结果对话框逐项呈现。
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
        })
        break
      }
    }
  }

  /** 确认对话框 loading 态（按操作种类取对应 mutation）。 */
  const confirmLoading =
    (confirmAction?.kind === 'activate' && activateMutation.isPending) ||
    (confirmAction?.kind === 'deactivate' && deactivateMutation.isPending) ||
    (confirmAction?.kind === 'delete' && deleteMutation.isPending) ||
    (confirmAction?.kind === 'batch-delete' && batchDeleteMutation.isPending)

  /** 确认对话框文案组装。 */
  const confirmDialogProps = (() => {
    if (confirmAction === null) {
      return null
    }
    switch (confirmAction.kind) {
      case 'activate':
        return {
          title: '激活人格',
          description: (
            <span>
              确认激活人格「{confirmAction.persona.name}」？激活为事务性单激活切换——原激活人格（如有）将被自动停用。
            </span>
          ),
          confirmText: '激活',
          destructive: false,
        }
      case 'deactivate':
        return {
          title: '停用激活',
          description: (
            <span>
              确认停用人格「{confirmAction.persona.name}」的激活位？停用后系统允许零激活状态。
            </span>
          ),
          confirmText: '停用激活',
          destructive: false,
        }
      case 'delete':
        return {
          title: '删除人格',
          description: (
            <span>
              确认删除人格「{confirmAction.persona.name}」？该操作不可恢复；当前激活人格将被后端拒绝删除（需先停用）。
            </span>
          ),
          confirmText: '删除',
          destructive: true,
        }
      case 'batch-delete':
        return {
          title: '批量删除人格',
          description: (
            <span>
              确认删除选中的 {confirmAction.ids.length} 个人格？逐项独立执行：激活人格与缺失项将记为失败，不阻塞其余项。
            </span>
          ),
          confirmText: '批量删除',
          destructive: true,
        }
    }
  })()

  return (
    <div className="p-8">
      <div className="mb-6 flex items-center justify-between">
        <h1 className="text-2xl font-semibold text-[var(--color-text)]">人格管理</h1>
        <Link
          to="/personas/new"
          className="inline-flex h-10 items-center gap-1 rounded-md bg-[var(--color-primary)] px-4 text-sm font-medium text-white transition-colors hover:bg-[var(--color-primary-hover)]"
        >
          <Plus className="h-4 w-4" aria-hidden="true" />
          新建人格
        </Link>
      </div>

      {/* 工具栏：状态过滤 + 批量删除 */}
      <div className="mb-4 flex items-center gap-3">
        <label htmlFor="status-filter" className="text-sm text-[var(--color-text-secondary)]">
          状态
        </label>
        <Select
          id="status-filter"
          className="w-40"
          value={statusFilter}
          onChange={(e) => {
            setStatusFilter(e.target.value === 'active' || e.target.value === 'disabled' ? e.target.value : '')
            setSelectedIds(new Set())
          }}
        >
          <option value="">全部</option>
          <option value="active">启用</option>
          <option value="disabled">停用</option>
        </Select>
        <Button
          variant="destructive"
          size="sm"
          disabled={selectedIds.size === 0}
          onClick={() => setConfirmAction({ kind: 'batch-delete', ids: [...selectedIds] })}
        >
          批量删除（已选 {selectedIds.size}）
        </Button>
      </div>

      {/* 列表主体 */}
      {listQuery.isPending ? (
        <div className="flex items-center justify-center py-16">
          <Spinner size="lg" />
        </div>
      ) : listQuery.isError ? (
        <Alert variant="destructive">
          <AlertDescription>
            <p>人格列表加载失败：{toApiError(listQuery.error).message}</p>
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
      ) : personas.length === 0 ? (
        <EmptyState
          title="暂无人格"
          description="点击右上角「新建人格」创建第一个人格。"
        />
      ) : (
        <div className="rounded-xl border border-[var(--color-border)]">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead className="w-10">
                  <input
                    type="checkbox"
                    aria-label="全选当前已加载人格"
                    checked={isAllSelected(loadedIds, selectedIds)}
                    onChange={(e) => toggleSelectAll(e.target.checked)}
                  />
                </TableHead>
                <TableHead className="w-16">ID</TableHead>
                <TableHead>注册名</TableHead>
                <TableHead>展示名</TableHead>
                <TableHead>卡片称呼</TableHead>
                <TableHead className="w-20">状态</TableHead>
                <TableHead className="w-24">激活位</TableHead>
                <TableHead className="w-44">更新时间</TableHead>
                <TableHead className="w-72">操作</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {personas.map((persona) => {
                const status = statusBadge(persona.status)
                return (
                  <TableRow key={persona.id} data-testid={`persona-row-${persona.id}`}>
                    <TableCell>
                      <input
                        type="checkbox"
                        aria-label={`选择人格 ${persona.name}`}
                        checked={selectedIds.has(persona.id)}
                        onChange={(e) => toggleSelected(persona.id, e.target.checked)}
                      />
                    </TableCell>
                    <TableCell>{persona.id}</TableCell>
                    <TableCell className="font-medium">{persona.name}</TableCell>
                    <TableCell>{persona.display_name}</TableCell>
                    <TableCell>{persona.card.name}</TableCell>
                    <TableCell>
                      <Badge variant={status.variant}>{status.label}</Badge>
                    </TableCell>
                    <TableCell>
                      {persona.is_active ? (
                        <Badge variant="info">当前激活</Badge>
                      ) : (
                        <span className="text-[var(--color-text-muted)]">—</span>
                      )}
                    </TableCell>
                    <TableCell className="text-[var(--color-text-secondary)]">
                      {formatEpochMs(persona.updated_at)}
                    </TableCell>
                    <TableCell>
                      <div className="flex flex-wrap items-center gap-1">
                        <Link
                          to={`/personas/${persona.id}/edit`}
                          className="rounded px-2 py-1 text-sm text-[var(--color-primary)] hover:bg-[var(--color-primary-soft)]"
                        >
                          编辑
                        </Link>
                        <Button
                          variant="outline"
                          size="sm"
                          disabled={persona.status !== 'active' || persona.is_active}
                          title={
                            persona.status !== 'active'
                              ? '停用状态的人格不可激活，请先编辑状态为启用'
                              : persona.is_active
                                ? '已是当前激活人格'
                                : undefined
                          }
                          onClick={() => setConfirmAction({ kind: 'activate', persona })}
                        >
                          激活
                        </Button>
                        <Button
                          variant="outline"
                          size="sm"
                          disabled={!persona.is_active}
                          onClick={() => setConfirmAction({ kind: 'deactivate', persona })}
                        >
                          停用
                        </Button>
                        <Button
                          variant="outline"
                          size="sm"
                          onClick={() => {
                            setCopyTarget(persona)
                            setCopyOpen(true)
                          }}
                        >
                          复制
                        </Button>
                        <Button
                          variant="ghost"
                          size="sm"
                          className="text-[var(--color-destructive)] hover:bg-[var(--color-destructive-light)]"
                          onClick={() => setConfirmAction({ kind: 'delete', persona })}
                        >
                          删除
                        </Button>
                      </div>
                    </TableCell>
                  </TableRow>
                )
              })}
            </TableBody>
          </Table>
          {/* 游标分页 load more */}
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

      {/* 确认对话框 */}
      {confirmDialogProps !== null && confirmAction !== null && (
        <ConfirmDialog
          open
          onOpenChange={(open) => {
            if (!open) setConfirmAction(null)
          }}
          title={confirmDialogProps.title}
          description={confirmDialogProps.description}
          confirmText={confirmDialogProps.confirmText}
          destructive={confirmDialogProps.destructive}
          loading={confirmLoading}
          onConfirm={handleConfirm}
        />
      )}

      {/* 复制对话框 */}
      <CopyPersonaDialog open={copyOpen} onOpenChange={setCopyOpen} persona={copyTarget} />

      {/* 批量删除结果对话框 */}
      <BatchDeleteResultDialog
        open={batchResultOpen}
        onOpenChange={setBatchResultOpen}
        result={batchResult}
      />
    </div>
  )
}

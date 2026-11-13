import { useState } from 'react'
import { Plus } from 'lucide-react'
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
import { ApiError, toApiError } from '@/api/client'
import {
  useDeleteModelEntry,
  useRegistryState,
  useRoleBindings,
} from '@/hooks/useModelsApi'
import type { ModelEntryMasked } from '@/types/model-registry'
import { ROLE_LABELS } from './modelsFormModel'
import { ModelEntryDialog } from './ModelEntryDialog'

/** 删除守卫提示态（409 后呈现——携带绑定角色与两步指引）。 */
interface DeleteGuardState {
  entry: ModelEntryMasked
  boundRoles: string[]
}

/** 从 409 details 提取 bound_roles（非数组形态回落空）。 */
function extractBoundRoles(error: ApiError): string[] {
  if (typeof error.details !== 'object' || error.details === null) {
    return []
  }
  const boundRoles = (error.details as Record<string, unknown>)['bound_roles']
  return Array.isArray(boundRoles) ? boundRoles.filter((role): role is string => typeof role === 'string') : []
}

/** 角色键 → 中文标签查找面（string 索引——未知键经 noUncheckedIndexedAccess 判空回落）。 */
const ROLE_LABEL_LOOKUP: Record<string, string> = { ...ROLE_LABELS }

/** 角色（字符串值）→ 中文标签（未知值回落原文）。 */
function roleLabel(role: string): string {
  return ROLE_LABEL_LOOKUP[role] ?? role
}

/**
 * 模型注册表管理页：条目表（脱敏读取面）+ 角色绑定汇总行 + 创建 /
 * 编辑对话框 + 删除（绑定守卫 409 → 中文提示 + 两步指引 + 解绑入口）。
 *
 * 密钥不变式：条目只展示 api_key_env 环境变量名（密钥值永不出现）。
 */
export default function ModelsPage() {
  const toast = useToast()
  const stateQuery = useRegistryState()
  const rolesQuery = useRoleBindings()
  const deleteMutation = useDeleteModelEntry()

  const [deleteTarget, setDeleteTarget] = useState<ModelEntryMasked | null>(null)
  const [deleteGuard, setDeleteGuard] = useState<DeleteGuardState | null>(null)
  const [dialogOpen, setDialogOpen] = useState(false)
  const [dialogMode, setDialogMode] = useState<'create' | 'edit'>('create')
  const [dialogEntry, setDialogEntry] = useState<ModelEntryMasked | null>(null)

  const entries = stateQuery.data?.entries ?? []

  const handleDelete = (): void => {
    if (deleteTarget === null) {
      return
    }
    deleteMutation.mutate(deleteTarget.name, {
      onSuccess: () => {
        toast.success(`模型条目「${deleteTarget.name}」已删除`)
        setDeleteTarget(null)
      },
      onError: (error) => {
        const apiError = toApiError(error)
        toast.error(apiError.message)
        setDeleteTarget(null)
        if (apiError.status === 409) {
          // 绑定守卫：呈现 bound_roles + 两步指引，并提供解绑入口。
          setDeleteGuard({ entry: deleteTarget, boundRoles: extractBoundRoles(apiError) })
        }
      },
    })
  }

  return (
    <div className="p-8">
      <div className="mb-6 flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold text-[var(--color-text)]">模型注册表</h1>
          <p className="mt-1 text-sm text-[var(--color-text-secondary)]">
            LLM 注册表是进程级配置真源（全局管理面，无人格参数）；写路径为原子持久化 + 热重建。
          </p>
        </div>
        <Button
          onClick={() => {
            setDialogMode('create')
            setDialogEntry(null)
            setDialogOpen(true)
          }}
        >
          <Plus className="mr-1 h-4 w-4" aria-hidden="true" />
          新建条目
        </Button>
      </div>

      {/* 角色绑定汇总行（五角色 bound / unbound） */}
      <div className="mb-6 rounded-xl border border-[var(--color-border)] p-4">
        <h2 className="mb-2 text-sm font-medium text-[var(--color-text)]">角色绑定</h2>
        {rolesQuery.isPending ? (
          <div className="flex items-center gap-2 text-sm text-[var(--color-text-secondary)]">
            <Spinner size="sm" />
            正在加载角色绑定…
          </div>
        ) : rolesQuery.isError ? (
          <p className="text-sm text-[var(--color-destructive)]" role="alert">
            角色绑定加载失败：{toApiError(rolesQuery.error).message}
          </p>
        ) : (
          <div className="flex flex-wrap items-center gap-2">
            {rolesQuery.data.map((binding) => (
              <span
                key={binding.role}
                data-testid={`role-binding-${binding.role}`}
                className="inline-flex items-center gap-1.5 rounded-full border border-[var(--color-border)] px-3 py-1 text-xs"
              >
                <span className="font-medium text-[var(--color-text)]">
                  {roleLabel(binding.role)}
                </span>
                {binding.bound ? (
                  <Badge variant="success">{binding.entry_name}</Badge>
                ) : (
                  <Badge variant="secondary">未绑定</Badge>
                )}
              </span>
            ))}
            <span className="text-xs text-[var(--color-text-muted)]">
              未绑定角色调用链路将以 MODEL_NOT_FOUND fail loud。
            </span>
          </div>
        )}
      </div>

      {/* 条目表 */}
      {stateQuery.isPending ? (
        <div className="flex items-center justify-center py-16">
          <Spinner size="lg" />
        </div>
      ) : stateQuery.isError ? (
        <Alert variant="destructive">
          <AlertDescription>
            <p>注册表状态加载失败：{toApiError(stateQuery.error).message}</p>
            <Button
              variant="outline"
              size="sm"
              className="mt-2"
              onClick={() => {
                void stateQuery.refetch()
              }}
            >
              重试
            </Button>
          </AlertDescription>
        </Alert>
      ) : entries.length === 0 ? (
        <EmptyState
          title="暂无模型条目"
          description="注册表为空；点击右上角「新建条目」创建第一个模型条目。"
        />
      ) : (
        <div className="rounded-xl border border-[var(--color-border)]">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>条目名</TableHead>
                <TableHead className="w-36">Provider</TableHead>
                <TableHead>基础 URL</TableHead>
                <TableHead className="w-36">模型名</TableHead>
                <TableHead className="w-40">API Key 环境变量</TableHead>
                <TableHead className="w-24">并发上限</TableHead>
                <TableHead className="w-20">多模态</TableHead>
                <TableHead className="w-40">声明角色</TableHead>
                <TableHead className="w-40">解析角色</TableHead>
                <TableHead className="w-28">操作</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {entries.map((entry) => (
                <TableRow key={entry.name} data-testid={`model-entry-row-${entry.name}`}>
                  <TableCell className="font-medium">{entry.name}</TableCell>
                  <TableCell>{entry.provider}</TableCell>
                  <TableCell className="text-[var(--color-text-secondary)]">
                    {entry.base_url}
                  </TableCell>
                  <TableCell>{entry.model}</TableCell>
                  <TableCell>
                    {entry.api_key_env === '' ? (
                      <span className="text-[var(--color-text-muted)]">（免鉴权）</span>
                    ) : (
                      entry.api_key_env
                    )}
                  </TableCell>
                  <TableCell>
                    {entry.concurrency === null ? (
                      <span className="text-[var(--color-text-muted)]">全局默认</span>
                    ) : (
                      entry.concurrency
                    )}
                  </TableCell>
                  <TableCell>
                    {entry.multimodal ? (
                      <Badge variant="info">支持</Badge>
                    ) : (
                      <span className="text-[var(--color-text-muted)]">—</span>
                    )}
                  </TableCell>
                  <TableCell>
                    {entry.roles.length === 0 ? (
                      <span className="text-[var(--color-text-muted)]">—</span>
                    ) : (
                      <div className="flex flex-wrap gap-1">
                        {entry.roles.map((role) => (
                          <Badge key={role} variant="secondary">
                            {roleLabel(role)}
                          </Badge>
                        ))}
                      </div>
                    )}
                  </TableCell>
                  <TableCell>
                    {entry.resolved_roles.length === 0 ? (
                      <span className="text-[var(--color-text-muted)]">—</span>
                    ) : (
                      <div className="flex flex-wrap gap-1">
                        {entry.resolved_roles.map((role) => (
                          <Badge key={role} variant="default">
                            {roleLabel(role)}
                          </Badge>
                        ))}
                      </div>
                    )}
                  </TableCell>
                  <TableCell>
                    <div className="flex flex-wrap items-center gap-1">
                      <Button
                        variant="outline"
                        size="sm"
                        onClick={() => {
                          setDialogMode('edit')
                          setDialogEntry(entry)
                          setDialogOpen(true)
                        }}
                      >
                        编辑
                      </Button>
                      <Button
                        variant="ghost"
                        size="sm"
                        className="text-[var(--color-destructive)] hover:bg-[var(--color-destructive-light)]"
                        onClick={() => setDeleteTarget(entry)}
                      >
                        删除
                      </Button>
                    </div>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
      )}

      {/* 删除守卫提示（409）：绑定角色 + 两步指引 + 解绑入口 */}
      {deleteGuard !== null && (
        <div
          role="alert"
          data-testid="model-delete-guard"
          className="mt-4 rounded-lg border border-[var(--color-destructive)] bg-[var(--color-destructive-light)] px-4 py-3 text-sm text-[var(--color-destructive)]"
        >
          <p className="font-medium">
            条目「{deleteGuard.entry.name}」仍绑定角色
            {deleteGuard.boundRoles.length > 0
              ? `：${deleteGuard.boundRoles.map(roleLabel).join('、')}`
              : '（显式 role_bindings 指向）'}
            ，禁止删除。
          </p>
          <p className="mt-1">
            两步操作：先在编辑对话框把「声明角色」清空（roles=[]）保存，再回来删除；
            显式 role_bindings 指向的条目需先在 config.yaml 移除该绑定。
          </p>
          <div className="mt-2 flex gap-2">
            <Button
              variant="outline"
              size="sm"
              onClick={() => {
                setDialogMode('edit')
                setDialogEntry(deleteGuard.entry)
                setDialogOpen(true)
                setDeleteGuard(null)
              }}
            >
              去编辑（解绑）
            </Button>
            <Button variant="ghost" size="sm" onClick={() => setDeleteGuard(null)}>
              知道了
            </Button>
          </div>
        </div>
      )}

      {deleteTarget !== null && (
        <ConfirmDialog
          open
          onOpenChange={(open) => {
            if (!open) setDeleteTarget(null)
          }}
          title="删除模型条目"
          description={
            <span>
              确认删除模型条目「{deleteTarget.name}」？仍承接角色的条目将被后端拒绝（409）——
              需先解绑再删除；该操作经原子持久化 + 热重建生效。
            </span>
          }
          confirmText="删除"
          destructive
          loading={deleteMutation.isPending}
          onConfirm={handleDelete}
        />
      )}

      <ModelEntryDialog
        open={dialogOpen}
        onOpenChange={(open) => {
          setDialogOpen(open)
          if (!open) setDialogEntry(null)
        }}
        mode={dialogMode}
        entry={dialogEntry}
      />
    </div>
  )
}

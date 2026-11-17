import { useEffect, useState } from 'react'
import { Plus } from 'lucide-react'
import { Button } from '@/components/ui/Button'
import { Badge } from '@/components/ui/Badge'
import { Textarea } from '@/components/ui/Textarea'
import { Spinner } from '@/components/ui/Spinner'
import { EmptyState } from '@/components/ui/EmptyState'
import { Alert, AlertDescription } from '@/components/ui/Alert'
import { ConfirmDialog } from '@/components/ConfirmDialog'
import { useToast } from '@/components/ui/useToast'
import { toApiError } from '@/api/client'
import {
  useCreateDirective,
  useDeleteDirective,
  useDirectivesList,
  useUpdateDirective,
} from '@/hooks/useDirectivesApi'
import type { DirectiveRecord } from '@/types/persona'

export interface DirectivesPanelProps {
  /** 目标人格主键（面板在人格编辑作用域内——必填）。 */
  personaId: number
}

/** 指令正文长度上限（与后端契约对齐）。 */
const DIRECTIVE_CONTENT_MAX_LENGTH = 500

/**
 * 常备指令面板（R2b D7-14——USER.md 式持久指令的编辑面）。
 *
 * 语义（文档化）：现行指令在每轮对话组装的系统提示里恒常注入
 * （【长期指令】块——20 条 / 2000 字符帽）；编辑保存经同 topic
 * supersede（旧版标记已取代并进入历史链），删除是不可恢复的管理面
 * 动作。对话里说「记住：X」也会落成同一域的指令（通道捕获面）。
 */
export function DirectivesPanel({ personaId }: DirectivesPanelProps) {
  const toast = useToast()
  const listQuery = useDirectivesList(personaId)
  const createMutation = useCreateDirective()
  const updateMutation = useUpdateDirective()
  const deleteMutation = useDeleteDirective()

  const [draft, setDraft] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [editing, setEditing] = useState<DirectiveRecord | null>(null)
  const [deleteTarget, setDeleteTarget] = useState<DirectiveRecord | null>(null)

  useEffect(() => {
    if (editing !== null) {
      setDraft(editing.content)
      setError(null)
    }
  }, [editing])

  const resetForm = (): void => {
    setEditing(null)
    setDraft('')
    setError(null)
  }

  const handleSave = (): void => {
    const content = draft.trim()
    if (content.length < 1 || content.length > DIRECTIVE_CONTENT_MAX_LENGTH) {
      setError(`长期指令正文必填，且长度为 1-${DIRECTIVE_CONTENT_MAX_LENGTH} 个字符`)
      toast.error('长期指令校验未通过')
      return
    }
    setError(null)
    const onSuccess = (record: DirectiveRecord): void => {
      toast.success(
        editing === null
          ? `长期指令已保存（v${record.version}）`
          : `长期指令已更新（v${record.version}）`,
      )
      resetForm()
    }
    const onError = (cause: unknown): void => {
      toast.error(toApiError(cause).message)
    }
    if (editing === null) {
      createMutation.mutate({ personaId, payload: { content } }, { onSuccess, onError })
      return
    }
    updateMutation.mutate(
      { personaId, directiveId: editing.id, payload: { content } },
      { onSuccess, onError },
    )
  }

  const busy = createMutation.isPending || updateMutation.isPending

  return (
    <div className="space-y-4">
      <div className="rounded-md border border-[var(--color-info)] bg-[var(--color-info-light)] px-3 py-2 text-xs text-[var(--color-info)]">
        常备指令会在每轮对话的系统提示里恒常生效（最多 20 条 / 2000 字符）；编辑保存后旧版本进入历史链。
        对话里说「记住：……」也会自动落成常备指令。
      </div>

      <div className="space-y-2">
        <Textarea
          id="directive-draft"
          rows={3}
          value={draft}
          maxLength={DIRECTIVE_CONTENT_MAX_LENGTH}
          onChange={(e) => setDraft(e.target.value)}
          placeholder="例如：以后都用简体中文；别再熬夜了，催我早点睡"
          aria-label="长期指令正文"
          aria-invalid={error !== null}
        />
        {error !== null && (
          <p className="text-xs text-[var(--color-destructive)]" role="alert">
            {error}
          </p>
        )}
        <div className="flex items-center gap-2">
          <Button size="sm" loading={busy} onClick={handleSave}>
            {editing === null ? (
              <>
                <Plus className="mr-1 h-4 w-4" aria-hidden="true" />
                新建常备指令
              </>
            ) : (
              '保存修改'
            )}
          </Button>
          {editing !== null && (
            <Button size="sm" variant="outline" onClick={resetForm}>
              取消编辑
            </Button>
          )}
        </div>
      </div>

      {listQuery.isPending ? (
        <div className="flex items-center justify-center py-8">
          <Spinner />
        </div>
      ) : listQuery.isError ? (
        <Alert variant="destructive">
          <AlertDescription>
            <p>常备指令加载失败：{toApiError(listQuery.error).message}</p>
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
      ) : listQuery.data.length === 0 ? (
        <EmptyState title="暂无常备指令" description="写一条长期生效的约定，让人格每轮都记得。" />
      ) : (
        <ul className="divide-y divide-[var(--color-border)] rounded-xl border border-[var(--color-border)]">
          {listQuery.data.map((directive) => (
            <li
              key={directive.id}
              className="flex items-start gap-3 p-3"
              data-testid={`directive-row-${directive.id}`}
            >
              <div className="min-w-0 flex-1">
                <p className="break-words text-sm text-[var(--color-text)]">{directive.content}</p>
                <p className="mt-1 text-xs text-[var(--color-text-muted)]">
                  {directive.topic} · v{directive.version}
                </p>
              </div>
              <Badge variant="secondary">生效中</Badge>
              <div className="flex items-center gap-1">
                <Button variant="outline" size="sm" onClick={() => setEditing(directive)}>
                  编辑
                </Button>
                <Button
                  variant="ghost"
                  size="sm"
                  className="text-[var(--color-destructive)] hover:bg-[var(--color-destructive-light)]"
                  onClick={() => setDeleteTarget(directive)}
                >
                  删除
                </Button>
              </div>
            </li>
          ))}
        </ul>
      )}

      {deleteTarget !== null && (
        <ConfirmDialog
          open
          onOpenChange={(open) => {
            if (!open) setDeleteTarget(null)
          }}
          title="删除常备指令"
          description={
            <span>
              确认删除常备指令「{deleteTarget.content}」？删除后不再注入后续对话，且不可恢复。
            </span>
          }
          confirmText="删除"
          destructive
          loading={deleteMutation.isPending}
          onConfirm={() => {
            deleteMutation.mutate(
              { personaId, directiveId: deleteTarget.id },
              {
                onSuccess: () => {
                  toast.success('常备指令已删除')
                  setDeleteTarget(null)
                  if (editing?.id === deleteTarget.id) {
                    resetForm()
                  }
                },
                onError: (cause) => {
                  toast.error(toApiError(cause).message)
                  setDeleteTarget(null)
                },
              },
            )
          }}
        />
      )}
    </div>
  )
}

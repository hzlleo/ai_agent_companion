import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { Plus } from 'lucide-react'
import { Button } from '@/components/ui/Button'
import { Badge } from '@/components/ui/Badge'
import { Input } from '@/components/ui/Input'
import { Select } from '@/components/ui/Select'
import { Textarea } from '@/components/ui/Textarea'
import { Spinner } from '@/components/ui/Spinner'
import { EmptyState } from '@/components/ui/EmptyState'
import { Alert, AlertDescription } from '@/components/ui/Alert'
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/Dialog'
import { ConfirmDialog } from '@/components/ConfirmDialog'
import { useToast } from '@/components/ui/useToast'
import { toApiError } from '@/api/client'
import {
  useCorrectFact,
  useCreateFact,
  useDeleteFact,
  useFactsList,
  useRejectFact,
} from '@/hooks/useFactsApi'
import type { FactRecord, PersonaFactCategory } from '@/types/persona'

export interface FactsPanelProps {
  /** 目标人格主键（面板在人格编辑作用域内——必填）。 */
  personaId: number
}

/** 类别展示标签（与后端 FACT_CATEGORY_LABELS 同口径）。 */
const CATEGORY_LABELS: Record<PersonaFactCategory, string> = {
  preference: '偏好',
  biography: '背景',
  relationship: '关系',
  context: '近况',
}

/** 类别展示顺序（面板分组序——固定，不随数据漂移）。 */
const CATEGORY_ORDER: PersonaFactCategory[] = [
  'preference',
  'biography',
  'relationship',
  'context',
]

/** 来源展示标签（梦境提取 / 手工录入 / 主人更正）。 */
const SOURCE_LABELS: Record<FactRecord['source'], string> = {
  dream: '梦境提取',
  manual: '手工录入',
  correction: '主人更正',
}

/** 事实正文长度上限（与后端契约对齐）。 */
const FACT_CONTENT_MAX_LENGTH = 1000

/** 事实录入 / 更正对话框形态（create = 新建；edit = 更正）。 */
type FactDialogMode = 'create' | 'edit'

/**
 * 人物事实面板（「了解 TA」——结构化事实 / 证据链 / 更正循环的编辑面）。
 *
 * 语义（文档化）：现行高置信事实在每轮对话组装的系统提示里以【关于对方】
 * 块注入（12 条 / 1500 字符帽）；梦境提取会自动落库并合并同身份事实；
 * 更正走 supersede（旧版本进入历史链并保留证据，更正说明追加为证据）；
 * 驳回后该事实不再注入（同内容可重新提取起链）。
 */
export function FactsPanel({ personaId }: FactsPanelProps) {
  const toast = useToast()
  const listQuery = useFactsList(personaId)
  const createMutation = useCreateFact()
  const correctMutation = useCorrectFact()
  const rejectMutation = useRejectFact()
  const deleteMutation = useDeleteFact()

  const [dialogMode, setDialogMode] = useState<FactDialogMode | null>(null)
  const [editing, setEditing] = useState<FactRecord | null>(null)
  const [category, setCategory] = useState<PersonaFactCategory>('preference')
  const [content, setContent] = useState('')
  const [confidenceText, setConfidenceText] = useState('0.8')
  const [error, setError] = useState<string | null>(null)
  const [deleteTarget, setDeleteTarget] = useState<FactRecord | null>(null)

  useEffect(() => {
    if (editing !== null) {
      setCategory(editing.category)
      setContent(editing.content)
      setConfidenceText(String(editing.confidence))
      setError(null)
    }
  }, [editing])

  const openCreate = (): void => {
    setEditing(null)
    setCategory('preference')
    setContent('')
    setConfidenceText('0.8')
    setError(null)
    setDialogMode('create')
  }

  const openCorrect = (record: FactRecord): void => {
    setEditing(record)
    setDialogMode('edit')
  }

  const closeDialog = (): void => {
    setDialogMode(null)
    setEditing(null)
  }

  const handleSave = (): void => {
    const trimmed = content.trim()
    if (trimmed.length < 1 || trimmed.length > FACT_CONTENT_MAX_LENGTH) {
      setError(`事实正文必填，且长度为 1-${FACT_CONTENT_MAX_LENGTH} 个字符`)
      toast.error('人物事实校验未通过')
      return
    }
    const confidence = Number(confidenceText)
    if (!Number.isFinite(confidence) || confidence < 0 || confidence > 1) {
      setError('置信度须为 0-1 之间的小数')
      toast.error('人物事实校验未通过')
      return
    }
    setError(null)
    const onError = (cause: unknown): void => {
      toast.error(toApiError(cause).message)
    }
    if (dialogMode === 'edit' && editing !== null) {
      correctMutation.mutate(
        { personaId, factId: editing.id, payload: { content: trimmed, confidence } },
        {
          onSuccess: (record) => {
            toast.success(`人物事实已更正（v${record.version}）`)
            closeDialog()
          },
          onError,
        },
      )
      return
    }
    createMutation.mutate(
      { personaId, payload: { category, content: trimmed, confidence } },
      {
        onSuccess: (record) => {
          toast.success(`人物事实已保存（v${record.version}）`)
          closeDialog()
        },
        onError,
      },
    )
  }

  const handleReject = (record: FactRecord): void => {
    rejectMutation.mutate(
      { personaId, factId: record.id },
      {
        onSuccess: () => {
          toast.success('人物事实已驳回')
        },
        onError: (cause) => {
          toast.error(toApiError(cause).message)
        },
      },
    )
  }

  const busy = createMutation.isPending || correctMutation.isPending

  const facts = listQuery.data?.items ?? []
  const grouped = CATEGORY_ORDER.map((key) => ({
    key,
    label: CATEGORY_LABELS[key],
    items: facts.filter((fact) => fact.category === key),
  })).filter((group) => group.items.length > 0)

  return (
    <div className="space-y-4">
      <div className="rounded-md border border-[var(--color-info)] bg-[var(--color-info-light)] px-3 py-2 text-xs text-[var(--color-info)]">
        人物事实是对 TA 的结构化认知（偏好 / 背景 / 关系 / 近况），高置信事实会在每轮对话里以【关于对方】块注入；
        夜间梦境会自动提取并合并同身份事实，更正会保留历史与证据。
      </div>

      <div className="flex items-center justify-between">
        <p className="text-xs text-[var(--color-text-muted)]">
          共 {facts.length} 条现行事实（按置信度降序）
        </p>
        <Button size="sm" onClick={openCreate}>
          <Plus className="mr-1 h-4 w-4" aria-hidden="true" />
          新建事实
        </Button>
      </div>

      {listQuery.isPending ? (
        <div className="flex items-center justify-center py-8">
          <Spinner />
        </div>
      ) : listQuery.isError ? (
        <Alert variant="destructive">
          <AlertDescription>
            <p>人物事实加载失败：{toApiError(listQuery.error).message}</p>
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
      ) : grouped.length === 0 ? (
        <EmptyState
          title="还不了解 TA"
          description="手动记下一条关于 TA 的事实，或者等夜间梦境自动整理。"
        />
      ) : (
        <div className="space-y-4">
          {grouped.map((group) => (
            <section key={group.key} aria-label={`${group.label}类事实`}>
              <h4 className="mb-2 text-sm font-medium text-[var(--color-text)]">
                {group.label}
              </h4>
              <ul className="divide-y divide-[var(--color-border)] rounded-xl border border-[var(--color-border)]">
                {group.items.map((fact) => (
                  <li key={fact.id} className="p-3" data-testid={`fact-row-${fact.id}`}>
                    <div className="flex items-start gap-3">
                      <div className="min-w-0 flex-1">
                        <p className="break-words text-sm text-[var(--color-text)]">
                          {fact.content}
                        </p>
                        <div className="mt-1 flex flex-wrap items-center gap-1 text-xs text-[var(--color-text-muted)]">
                          <Badge variant="secondary">
                            置信度 {Math.round(fact.confidence * 100)}%
                          </Badge>
                          <Badge variant="info">{SOURCE_LABELS[fact.source]}</Badge>
                          <span>v{fact.version}</span>
                        </div>
                      </div>
                      <div className="flex items-center gap-1">
                        <Button variant="outline" size="sm" onClick={() => openCorrect(fact)}>
                          更正
                        </Button>
                        <Button
                          variant="ghost"
                          size="sm"
                          className="text-[var(--color-destructive)] hover:bg-[var(--color-destructive-light)]"
                          onClick={() => handleReject(fact)}
                        >
                          驳回
                        </Button>
                        <Button variant="ghost" size="sm" onClick={() => setDeleteTarget(fact)}>
                          删除
                        </Button>
                      </div>
                    </div>
                    {fact.evidence.length > 0 && (
                      <details className="mt-2" data-testid={`fact-evidence-${fact.id}`}>
                        <summary className="cursor-pointer text-xs text-[var(--color-primary-hover)]">
                          证据（{fact.evidence.length}）
                        </summary>
                        <ul className="mt-2 space-y-1">
                          {fact.evidence.map((ref, index) => (
                            <li
                              key={`${fact.id}-${index}`}
                              className="rounded-md bg-[var(--color-surface-secondary)] p-2 text-xs text-[var(--color-text-muted)]"
                            >
                              <blockquote className="break-words">「{ref.quote}」</blockquote>
                              <p className="mt-1">
                                {ref.episode_id === null ? (
                                  <span>主人直接告知</span>
                                ) : (
                                  <Link to="/memories" className="underline">
                                    情景 #{ref.episode_id}
                                  </Link>
                                )}
                                <span> · {new Date(ref.ts_ms).toLocaleString()}</span>
                              </p>
                            </li>
                          ))}
                        </ul>
                      </details>
                    )}
                  </li>
                ))}
              </ul>
            </section>
          ))}
        </div>
      )}

      <Dialog
        open={dialogMode !== null}
        onOpenChange={(open) => {
          if (!open) closeDialog()
        }}
      >
        <DialogHeader>
          <DialogTitle>{dialogMode === 'edit' ? '更正人物事实' : '新建人物事实'}</DialogTitle>
        </DialogHeader>
        <DialogContent className="space-y-3">
          <div className="space-y-1">
            <label htmlFor="fact-category" className="text-xs text-[var(--color-text-muted)]">
              类别
            </label>
            <Select
              id="fact-category"
              value={category}
              disabled={dialogMode === 'edit'}
              onChange={(e) => setCategory(e.target.value as PersonaFactCategory)}
            >
              {CATEGORY_ORDER.map((key) => (
                <option key={key} value={key}>
                  {CATEGORY_LABELS[key]}
                </option>
              ))}
            </Select>
          </div>
          <div className="space-y-1">
            <label htmlFor="fact-content" className="text-xs text-[var(--color-text-muted)]">
              事实正文
            </label>
            <Textarea
              id="fact-content"
              rows={3}
              value={content}
              maxLength={FACT_CONTENT_MAX_LENGTH}
              onChange={(e) => setContent(e.target.value)}
              placeholder="例如：TA 喜欢无糖乌龙茶；TA 在杭州工作"
              aria-invalid={error !== null}
            />
          </div>
          <div className="space-y-1">
            <label htmlFor="fact-confidence" className="text-xs text-[var(--color-text-muted)]">
              置信度（0-1）
            </label>
            <Input
              id="fact-confidence"
              type="number"
              min={0}
              max={1}
              step={0.05}
              value={confidenceText}
              onChange={(e) => setConfidenceText(e.target.value)}
            />
          </div>
          {error !== null && (
            <p className="text-xs text-[var(--color-destructive)]" role="alert">
              {error}
            </p>
          )}
        </DialogContent>
        <DialogFooter>
          <Button variant="outline" onClick={closeDialog}>
            取消
          </Button>
          <Button loading={busy} onClick={handleSave}>
            {dialogMode === 'edit' ? '保存更正' : '保存事实'}
          </Button>
        </DialogFooter>
      </Dialog>

      {deleteTarget !== null && (
        <ConfirmDialog
          open
          onOpenChange={(open) => {
            if (!open) setDeleteTarget(null)
          }}
          title="删除人物事实"
          description={
            <span>
              确认删除事实「{deleteTarget.content}」？证据链会一并消失，且不可恢复。
            </span>
          }
          confirmText="删除"
          destructive
          loading={deleteMutation.isPending}
          onConfirm={() => {
            deleteMutation.mutate(
              { personaId, factId: deleteTarget.id },
              {
                onSuccess: () => {
                  toast.success('人物事实已删除')
                  setDeleteTarget(null)
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

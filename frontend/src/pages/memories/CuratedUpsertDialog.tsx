import { useEffect, useState } from 'react'
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/Dialog'
import { Button } from '@/components/ui/Button'
import { Input } from '@/components/ui/Input'
import { Textarea } from '@/components/ui/Textarea'
import { Select } from '@/components/ui/Select'
import { useToast } from '@/components/ui/useToast'
import { toApiError } from '@/api/client'
import { useUpsertCurated } from '@/hooks/useMemoriesApi'
import type { CuratedKind, CuratedRecord, CuratedUpsertRequest, MemoryProvenance } from '@/types/memory'
import { CURATED_KIND_LABELS } from './memoryDisplay'

export interface CuratedUpsertDialogProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  /** 目标人格主键（undefined = 当前激活人格）。 */
  personaId: number | undefined
  /** 编辑锚点（null = 新建；提供时预填并沿同主题 upsert）。 */
  initial: CuratedRecord | null
}

/** 策展 upsert 表单字段态。 */
interface CuratedFormState {
  kind: CuratedKind
  topic: string
  content: string
  importance: string
  provenance: MemoryProvenance
}

/** 记录 → 表单态（编辑锚点预填）。 */
function recordToForm(record: CuratedRecord | null): CuratedFormState {
  if (record === null) {
    return { kind: 'memory', topic: '', content: '', importance: '5', provenance: 'owner' }
  }
  return {
    kind: record.kind,
    topic: record.topic,
    content: record.content,
    importance: String(record.importance),
    provenance: record.provenance,
  }
}

/**
 * 策展记忆 upsert 对话框（supersede 语义提示）。
 *
 * 同（人格, 类型, 主题键）至多一条现行版本——保存同主题新内容将取代
 * 旧版（旧版标记已取代并进入历史链）。untrusted 来源不提供（后端
 * 强制拒绝——平台内容永不入策展层）。
 */
export function CuratedUpsertDialog({ open, onOpenChange, personaId, initial }: CuratedUpsertDialogProps) {
  const toast = useToast()
  const upsertMutation = useUpsertCurated()
  const [form, setForm] = useState<CuratedFormState>(recordToForm(null))
  const [errors, setErrors] = useState<Record<string, string>>({})

  useEffect(() => {
    if (open) {
      setForm(recordToForm(initial))
      setErrors({})
    }
  }, [open, initial])

  const handleConfirm = (): void => {
    const nextErrors: Record<string, string> = {}
    const topic = form.topic.trim()
    if (topic.length < 1 || topic.length > 255) {
      nextErrors['topic'] = '主题键必填，且长度为 1-255 个字符'
    }
    if (form.content.trim() === '') {
      nextErrors['content'] = '记忆正文必填'
    }
    const importance = Number(form.importance)
    if (!Number.isInteger(importance) || importance < 1 || importance > 10) {
      nextErrors['importance'] = '重要度须为 1-10 的整数'
    }
    setErrors(nextErrors)
    if (Object.keys(nextErrors).length > 0) {
      toast.error('表单校验未通过，请检查标红字段')
      return
    }
    const payload: CuratedUpsertRequest = {
      kind: form.kind,
      topic,
      content: form.content,
      importance,
      provenance: form.provenance,
    }
    upsertMutation.mutate(
      { personaId, payload },
      {
        onSuccess: (record) => {
          toast.success(
            `策展记忆已保存（主题「${record.topic}」现行版本 v${record.version}）`,
          )
          onOpenChange(false)
        },
        onError: (error) => {
          toast.error(toApiError(error).message)
        },
      },
    )
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange} size="lg">
      <DialogHeader>
        <DialogTitle>{initial === null ? '新建策展记忆' : `保存策展记忆（主题「${initial.topic}」）`}</DialogTitle>
        <DialogClose onClose={() => onOpenChange(false)} />
      </DialogHeader>
      <DialogContent>
        <div className="space-y-4">
          <div className="rounded-md border border-[var(--color-info)] bg-[var(--color-info-light)] px-3 py-2 text-xs text-[var(--color-info)]">
            supersede 语义：同（人格 · 类型 · 主题键）至多一条现行版本——保存同主题新内容将取代旧版，
            旧版标记「已取代」并保留在历史链中可回溯。
          </div>
          <div className="grid grid-cols-2 gap-4">
            <div className="space-y-1.5">
              <label htmlFor="curated-kind" className="text-sm font-medium">
                策展类型
              </label>
              <Select
                id="curated-kind"
                value={form.kind}
                onChange={(e) => setForm((prev) => ({ ...prev, kind: e.target.value as CuratedKind }))}
              >
                {Object.entries(CURATED_KIND_LABELS).map(([value, label]) => (
                  <option key={value} value={value}>
                    {label}
                  </option>
                ))}
              </Select>
            </div>
            <div className="space-y-1.5">
              <label htmlFor="curated-topic" className="text-sm font-medium">
                主题键
              </label>
              <Input
                id="curated-topic"
                value={form.topic}
                onChange={(e) => setForm((prev) => ({ ...prev, topic: e.target.value }))}
                placeholder="同主题键 upsert 将取代旧版"
                aria-invalid={errors['topic'] !== undefined}
              />
              {errors['topic'] !== undefined && (
                <p className="text-xs text-[var(--color-destructive)]" role="alert">
                  {errors['topic']}
                </p>
              )}
            </div>
            <div className="space-y-1.5">
              <label htmlFor="curated-importance" className="text-sm font-medium">
                重要度（1-10）
              </label>
              <Input
                id="curated-importance"
                type="number"
                min={1}
                max={10}
                value={form.importance}
                onChange={(e) => setForm((prev) => ({ ...prev, importance: e.target.value }))}
                aria-invalid={errors['importance'] !== undefined}
              />
              {errors['importance'] !== undefined && (
                <p className="text-xs text-[var(--color-destructive)]" role="alert">
                  {errors['importance']}
                </p>
              )}
            </div>
            <div className="space-y-1.5">
              <label htmlFor="curated-provenance" className="text-sm font-medium">
                来源可信度
              </label>
              <Select
                id="curated-provenance"
                value={form.provenance}
                onChange={(e) =>
                  setForm((prev) => ({ ...prev, provenance: e.target.value as MemoryProvenance }))
                }
              >
                <option value="owner">主人</option>
                <option value="agent">智能体</option>
              </Select>
            </div>
          </div>
          <div className="space-y-1.5">
            <label htmlFor="curated-content" className="text-sm font-medium">
              记忆正文
            </label>
            <Textarea
              id="curated-content"
              rows={5}
              value={form.content}
              onChange={(e) => setForm((prev) => ({ ...prev, content: e.target.value }))}
              placeholder="蒸馏后的结构化记忆正文（必填）"
              aria-invalid={errors['content'] !== undefined}
            />
            {errors['content'] !== undefined && (
              <p className="text-xs text-[var(--color-destructive)]" role="alert">
                {errors['content']}
              </p>
            )}
          </div>
          <p className="text-xs text-[var(--color-text-muted)]">
            平台内容（untrusted 来源）永不入策展层——来源可信度仅提供主人 / 智能体两档。
          </p>
        </div>
      </DialogContent>
      <DialogFooter>
        <Button variant="outline" onClick={() => onOpenChange(false)} disabled={upsertMutation.isPending}>
          取消
        </Button>
        <Button onClick={handleConfirm} loading={upsertMutation.isPending}>
          保存
        </Button>
      </DialogFooter>
    </Dialog>
  )
}

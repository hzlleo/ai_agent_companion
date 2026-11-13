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
import { useCreateEpisode } from '@/hooks/useMemoriesApi'
import type { EpisodeKind, MemoryProvenance } from '@/types/memory'
import {
  createEmptyEpisodeForm,
  formToEpisodeRequest,
  payloadTemplateForKind,
  validateEpisodeForm,
  type EpisodeFormErrors,
  type EpisodeFormState,
} from './episodeFormModel'
import { EPISODE_KIND_LABELS, PROVENANCE_LABELS } from './memoryDisplay'

export interface EpisodeInjectDialogProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  /** 目标人格主键（undefined = 当前激活人格）。 */
  personaId: number | undefined
}

/** 载荷字段提示（按 kind——与后端载荷 Model Class 契约对齐）。 */
const PAYLOAD_HINTS: Record<EpisodeKind, string> = {
  chat: 'chat 载荷：user_content / assistant_content（字符串）+ session_id（数值或 null）',
  duty: 'duty 载荷：title / summary（字符串）+ details（自由 JSON 对象）',
  platform: 'platform 载荷：action / summary（字符串）+ metadata（自由 JSON 对象）',
  dream: 'dream 载荷：text（字符串）+ metadata（自由 JSON 对象）',
  system: 'system 载荷：text（字符串）+ metadata（自由 JSON 对象）',
}

/** 字段错误取值（undefined 安全）。 */
function fieldError(errors: EpisodeFormErrors, key: string): string | null {
  return errors[key] ?? null
}

/**
 * 情景事件手工注入对话框：kind / 来源 / 重要度 / 可信度 + JSON 载荷
 * 编辑器（按 kind 判别校验——类型不符前置拦截，后端校验兜底）。
 */
export function EpisodeInjectDialog({ open, onOpenChange, personaId }: EpisodeInjectDialogProps) {
  const toast = useToast()
  const createMutation = useCreateEpisode()
  const [form, setForm] = useState<EpisodeFormState>(createEmptyEpisodeForm)
  const [errors, setErrors] = useState<EpisodeFormErrors>({})

  useEffect(() => {
    if (open) {
      setForm(createEmptyEpisodeForm())
      setErrors({})
    }
  }, [open])

  const handleKindChange = (kind: EpisodeKind): void => {
    setForm((prev) => ({ ...prev, kind, payloadText: payloadTemplateForKind(kind) }))
  }

  const handleConfirm = (): void => {
    const validation = validateEpisodeForm(form)
    setErrors(validation)
    if (Object.keys(validation).length > 0) {
      toast.error('表单校验未通过，请检查标红字段')
      return
    }
    const payload = formToEpisodeRequest(form)
    if (payload === null) {
      return
    }
    createMutation.mutate(
      { personaId, payload },
      {
        onSuccess: (episode) => {
          toast.success(`情景事件 #${episode.id} 已记录（${EPISODE_KIND_LABELS[episode.kind]}）`)
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
        <DialogTitle>手工注入情景事件</DialogTitle>
        <DialogClose onClose={() => onOpenChange(false)} />
      </DialogHeader>
      <DialogContent>
        <div className="space-y-4">
          <div className="grid grid-cols-2 gap-4">
            <div className="space-y-1.5">
              <label htmlFor="episode-kind" className="text-sm font-medium">
                事件类型
              </label>
              <Select
                id="episode-kind"
                value={form.kind}
                onChange={(e) => handleKindChange(e.target.value as EpisodeKind)}
              >
                {Object.entries(EPISODE_KIND_LABELS).map(([value, label]) => (
                  <option key={value} value={value}>
                    {label}
                  </option>
                ))}
              </Select>
            </div>
            <div className="space-y-1.5">
              <label htmlFor="episode-source" className="text-sm font-medium">
                事件来源
              </label>
              <Input
                id="episode-source"
                value={form.source}
                onChange={(e) => setForm((prev) => ({ ...prev, source: e.target.value }))}
                placeholder="来源标识（必填，1-128 字符）"
                aria-invalid={fieldError(errors, 'source') !== null}
              />
              {fieldError(errors, 'source') !== null && (
                <p className="text-xs text-[var(--color-destructive)]" role="alert">
                  {fieldError(errors, 'source')}
                </p>
              )}
            </div>
            <div className="space-y-1.5">
              <label htmlFor="episode-importance" className="text-sm font-medium">
                重要度（1-10）
              </label>
              <Input
                id="episode-importance"
                type="number"
                min={1}
                max={10}
                value={form.importance}
                onChange={(e) => setForm((prev) => ({ ...prev, importance: e.target.value }))}
                aria-invalid={fieldError(errors, 'importance') !== null}
              />
              {fieldError(errors, 'importance') !== null && (
                <p className="text-xs text-[var(--color-destructive)]" role="alert">
                  {fieldError(errors, 'importance')}
                </p>
              )}
            </div>
            <div className="space-y-1.5">
              <label htmlFor="episode-provenance" className="text-sm font-medium">
                来源可信度
              </label>
              <Select
                id="episode-provenance"
                value={form.provenance}
                onChange={(e) =>
                  setForm((prev) => ({ ...prev, provenance: e.target.value as MemoryProvenance }))
                }
              >
                {Object.entries(PROVENANCE_LABELS).map(([value, label]) => (
                  <option key={value} value={value}>
                    {label}
                  </option>
                ))}
              </Select>
            </div>
          </div>
          <div className="space-y-1.5">
            <label htmlFor="episode-payload" className="text-sm font-medium">
              载荷 JSON（按事件类型判别校验）
            </label>
            <p className="text-xs text-[var(--color-text-muted)]">{PAYLOAD_HINTS[form.kind]}</p>
            <Textarea
              id="episode-payload"
              rows={8}
              className="font-mono text-xs"
              value={form.payloadText}
              onChange={(e) => setForm((prev) => ({ ...prev, payloadText: e.target.value }))}
              aria-invalid={fieldError(errors, 'payloadText') !== null}
            />
            {fieldError(errors, 'payloadText') !== null && (
              <p className="text-xs text-[var(--color-destructive)]" role="alert">
                {fieldError(errors, 'payloadText')}
              </p>
            )}
          </div>
          <p className="text-xs text-[var(--color-text-muted)]">
            时间戳缺省为落库当前时间；同一（类型 + 来源 + 载荷）将命中内容指纹去重。
          </p>
        </div>
      </DialogContent>
      <DialogFooter>
        <Button variant="outline" onClick={() => onOpenChange(false)} disabled={createMutation.isPending}>
          取消
        </Button>
        <Button onClick={handleConfirm} loading={createMutation.isPending}>
          注入
        </Button>
      </DialogFooter>
    </Dialog>
  )
}

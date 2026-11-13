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
import { Checkbox } from '@/components/ui/Checkbox'
import { useToast } from '@/components/ui/useToast'
import { useCopyPersona } from '@/hooks/usePersonasApi'
import { toApiError } from '@/api/client'
import type { PersonaRecord } from '@/types/persona'

export interface CopyPersonaDialogProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  /** 源人格（展示名承接提示用）。 */
  persona: PersonaRecord | null
}

/**
 * 复制人格对话框：新注册名 + 展示名（可空承接源）+ 是否深拷贝 M/S 状态。
 *
 * 409（新注册名被占用）等错误经 toast 呈现后端 message。
 */
export function CopyPersonaDialog({ open, onOpenChange, persona }: CopyPersonaDialogProps) {
  const toast = useToast()
  const copyMutation = useCopyPersona()
  const [newName, setNewName] = useState('')
  const [displayName, setDisplayName] = useState('')
  const [includeStates, setIncludeStates] = useState(false)
  const [nameError, setNameError] = useState<string | null>(null)

  // 打开时重置字段（默认名 = 源名-copy）。
  useEffect(() => {
    if (open && persona !== null) {
      setNewName(`${persona.name}-copy`)
      setDisplayName('')
      setIncludeStates(false)
      setNameError(null)
    }
  }, [open, persona])

  if (persona === null) {
    return null
  }

  const handleConfirm = (): void => {
    const trimmed = newName.trim()
    if (trimmed.length < 1 || trimmed.length > 128) {
      setNameError('新注册名必填，且长度为 1-128 个字符')
      return
    }
    setNameError(null)
    copyMutation.mutate(
      {
        id: persona.id,
        payload: {
          new_name: trimmed,
          display_name: displayName.trim() === '' ? null : displayName.trim(),
          include_states: includeStates,
        },
      },
      {
        onSuccess: () => {
          toast.success(`人格已复制为「${trimmed}」`)
          onOpenChange(false)
        },
        onError: (error) => {
          toast.error(toApiError(error).message)
        },
      },
    )
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogHeader>
        <DialogTitle>复制人格</DialogTitle>
        <DialogClose onClose={() => onOpenChange(false)} />
      </DialogHeader>
      <DialogContent>
        <div className="space-y-4">
          <div className="text-sm text-[var(--color-text-secondary)]">
            源人格：<span className="font-medium text-[var(--color-text)]">{persona.name}</span>
          </div>
          <div className="space-y-1.5">
            <label htmlFor="copy-new-name" className="text-sm font-medium">
              新注册名
            </label>
            <Input
              id="copy-new-name"
              value={newName}
              onChange={(e) => setNewName(e.target.value)}
              placeholder="全局唯一，1-128 个字符"
              aria-invalid={nameError !== null}
            />
            {nameError !== null && (
              <p className="text-xs text-[var(--color-destructive)]" role="alert">
                {nameError}
              </p>
            )}
          </div>
          <div className="space-y-1.5">
            <label htmlFor="copy-display-name" className="text-sm font-medium">
              展示名（可空，空则承接源人格）
            </label>
            <Input
              id="copy-display-name"
              value={displayName}
              onChange={(e) => setDisplayName(e.target.value)}
              placeholder="留空承接源人格展示名"
            />
          </div>
          <Checkbox
            checked={includeStates}
            onChange={(e) => setIncludeStates(e.target.checked)}
            label="深拷贝 M/S 状态层（中期心情关系 + 短期会话状态）"
          />
        </div>
      </DialogContent>
      <DialogFooter>
        <Button variant="outline" onClick={() => onOpenChange(false)} disabled={copyMutation.isPending}>
          取消
        </Button>
        <Button onClick={handleConfirm} loading={copyMutation.isPending}>
          复制
        </Button>
      </DialogFooter>
    </Dialog>
  )
}

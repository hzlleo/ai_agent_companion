import { useState } from 'react'
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

export interface PersonaImportConflictDialogProps {
  /** 对话框开闭（调用方条件挂载——关闭即卸载，输入态自然复位）。 */
  open: boolean
  /** 关闭请求（Radix 关闭面恒传 false——统一按关闭处理）。 */
  onOpenChange: (open: boolean) => void
  /** 冲突的目标注册名（展示用）。 */
  targetName: string
  /** 重试进行中（改名 / 覆盖共用 mutation 的 pending 态）。 */
  loading: boolean
  /** 改名重试（携带新期望名）。 */
  onRename: (newName: string) => void
  /** 覆盖重试（整体替换既有人格卡片，保留状态）。 */
  onOverwrite: () => void
  /** 跳过（保留现状——调用方负责 toast）。 */
  onSkip: () => void
}

/**
 * 导入名称冲突三选一对话框（预设生成 / 文件导入共用）。
 *
 * 后端 name_conflict 响应后的策略重问面：跳过（保留现状）/ 改名（输入
 * 新期望名后以 rename 策略重试）/ 覆盖（destructive 确认后以 overwrite
 * 策略重试——整体替换既有人格卡片，保留 M/S 状态）。内容同卡（幂等）
 * 不进入本对话框——由调用方直接 toast。调用方条件挂载（关闭即卸载）
 * ——输入态经卸载自然复位，无需 open 联动重置 effect。
 */
export function PersonaImportConflictDialog({
  open,
  onOpenChange,
  targetName,
  loading,
  onRename,
  onOverwrite,
  onSkip,
}: PersonaImportConflictDialogProps) {
  const [newName, setNewName] = useState('')
  const [nameError, setNameError] = useState<string | null>(null)

  const handleRename = (): void => {
    const trimmed = newName.trim()
    if (trimmed.length < 1 || trimmed.length > 128) {
      setNameError('新名字必填，且长度为 1-128 个字符')
      return
    }
    setNameError(null)
    onRename(trimmed)
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogHeader>
        <DialogTitle>名称冲突</DialogTitle>
        <DialogClose
          onClose={() => {
            if (!loading) onSkip()
          }}
        />
      </DialogHeader>
      <DialogContent>
        <div className="space-y-4">
          <p className="text-sm text-[var(--color-text-secondary)]">
            注册名「{targetName}」已被占用且内容不同，可选择以下处理方式：
          </p>
          <div className="space-y-1.5">
            <label htmlFor="import-rename-name" className="text-sm font-medium">
              改名导入
            </label>
            <div className="flex items-center gap-2">
              <Input
                id="import-rename-name"
                value={newName}
                onChange={(e) => setNewName(e.target.value)}
                placeholder="输入新的注册名"
                aria-invalid={nameError !== null}
                disabled={loading}
              />
              <Button onClick={handleRename} loading={loading}>
                以新名导入
              </Button>
            </div>
            {nameError !== null && (
              <p className="text-xs text-[var(--color-destructive)]" role="alert">
                {nameError}
              </p>
            )}
          </div>
          <div className="flex items-center justify-between gap-2 border-t border-[var(--color-border)] pt-4">
            <span className="text-sm text-[var(--color-text-secondary)]">
              覆盖会整体替换既有人格的卡片（M/S 状态保留）。
            </span>
            <Button variant="destructive" onClick={onOverwrite} loading={loading}>
              覆盖既有人格
            </Button>
          </div>
        </div>
      </DialogContent>
      <DialogFooter>
        <Button variant="outline" onClick={onSkip} disabled={loading}>
          跳过（保留现状）
        </Button>
      </DialogFooter>
    </Dialog>
  )
}

import { useEffect, useState } from 'react'
import { Plus } from 'lucide-react'
import { Button } from '@/components/ui/Button'
import { Badge } from '@/components/ui/Badge'
import { Input } from '@/components/ui/Input'
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
  useCreateSkill,
  useDeleteSkill,
  useDisableSkill,
  useEnableSkill,
  useSkillsList,
  useUpdateSkill,
} from '@/hooks/useSkillsApi'
import type { SkillRecord } from '@/types/persona'

export interface SkillsPanelProps {
  /** 目标人格主键（面板在人格编辑作用域内——必填）。 */
  personaId: number
}

/** 来源展示标签（手工录入 / 梦境挖矿）。 */
const SOURCE_LABELS: Record<SkillRecord['source'], string> = {
  manual: '手工录入',
  mined: '梦境挖矿',
}

/** 名称 / 描述 / 正文长度上限（与后端契约对齐）。 */
const SKILL_NAME_MAX_LENGTH = 120
const SKILL_DESCRIPTION_MAX_LENGTH = 500
const SKILL_CONTENT_MAX_LENGTH = 8000

/** 触发短语的条数与单条长度上限（与后端契约对齐）。 */
const SKILL_TRIGGERS_MAX = 8
const SKILL_TRIGGER_MAX_LENGTH = 40

/** 技能编辑对话框形态（create = 新建；edit = 原地编辑）。 */
type SkillDialogMode = 'create' | 'edit'

/** 把触发短语输入解析为列表（换行 / 英文逗号 / 中文逗号分隔）。 */
function parseTriggers(text: string): string[] {
  return text
    .split(/[\n,，]/)
    .map((item) => item.trim())
    .filter((item) => item.length > 0)
}

/**
 * 技能面板（「做事方式」——程序性技能的编辑面）。
 *
 * 语义（文档化）：启用的技能在每轮对话组装时按**当前用户输入**与触发短语
 * 做字面匹配，命中后以【相关技能】块注入系统提示（最多两条、总字符 2000
 * 帽）；编辑是原地更新（版本递增，不保留历史）；停用后不再参与匹配但数据
 * 保留；梦境会从历史事件挖矿生成技能（与手工行同等可编辑 / 停用 / 删除）。
 */
export function SkillsPanel({ personaId }: SkillsPanelProps) {
  const toast = useToast()
  const listQuery = useSkillsList(personaId)
  const createMutation = useCreateSkill()
  const updateMutation = useUpdateSkill()
  const enableMutation = useEnableSkill()
  const disableMutation = useDisableSkill()
  const deleteMutation = useDeleteSkill()

  const [dialogMode, setDialogMode] = useState<SkillDialogMode | null>(null)
  const [editing, setEditing] = useState<SkillRecord | null>(null)
  const [name, setName] = useState('')
  const [description, setDescription] = useState('')
  const [triggersText, setTriggersText] = useState('')
  const [content, setContent] = useState('')
  const [confidenceText, setConfidenceText] = useState('0.6')
  const [error, setError] = useState<string | null>(null)
  const [deleteTarget, setDeleteTarget] = useState<SkillRecord | null>(null)

  useEffect(() => {
    if (editing !== null) {
      setName(editing.name)
      setDescription(editing.description)
      setTriggersText(editing.triggers.join('\n'))
      setContent(editing.content)
      setConfidenceText(String(editing.confidence))
      setError(null)
    }
  }, [editing])

  const openCreate = (): void => {
    setEditing(null)
    setName('')
    setDescription('')
    setTriggersText('')
    setContent('')
    setConfidenceText('0.6')
    setError(null)
    setDialogMode('create')
  }

  const openEdit = (record: SkillRecord): void => {
    setEditing(record)
    setDialogMode('edit')
  }

  const closeDialog = (): void => {
    setDialogMode(null)
    setEditing(null)
  }

  const handleSave = (): void => {
    const trimmedName = name.trim()
    if (trimmedName.length < 1 || trimmedName.length > SKILL_NAME_MAX_LENGTH) {
      setError(`技能名称必填，且长度为 1-${SKILL_NAME_MAX_LENGTH} 个字符`)
      toast.error('技能校验未通过')
      return
    }
    const trimmedDescription = description.trim()
    if (
      trimmedDescription.length < 1 ||
      trimmedDescription.length > SKILL_DESCRIPTION_MAX_LENGTH
    ) {
      setError(`触发描述必填，且长度为 1-${SKILL_DESCRIPTION_MAX_LENGTH} 个字符`)
      toast.error('技能校验未通过')
      return
    }
    const triggers = parseTriggers(triggersText)
    if (triggers.length < 1 || triggers.length > SKILL_TRIGGERS_MAX) {
      setError(`触发短语须为 1-${SKILL_TRIGGERS_MAX} 条（每行一条）`)
      toast.error('技能校验未通过')
      return
    }
    if (triggers.some((trigger) => trigger.length > SKILL_TRIGGER_MAX_LENGTH)) {
      setError(`单条触发短语不能超过 ${SKILL_TRIGGER_MAX_LENGTH} 个字符`)
      toast.error('技能校验未通过')
      return
    }
    const trimmedContent = content.trim()
    if (trimmedContent.length < 1 || trimmedContent.length > SKILL_CONTENT_MAX_LENGTH) {
      setError(`步骤正文必填，且长度为 1-${SKILL_CONTENT_MAX_LENGTH} 个字符`)
      toast.error('技能校验未通过')
      return
    }
    const confidence = Number(confidenceText)
    if (!Number.isFinite(confidence) || confidence < 0 || confidence > 1) {
      setError('置信度须为 0-1 之间的小数')
      toast.error('技能校验未通过')
      return
    }
    setError(null)
    const onError = (cause: unknown): void => {
      toast.error(toApiError(cause).message)
    }
    if (dialogMode === 'edit' && editing !== null) {
      updateMutation.mutate(
        {
          personaId,
          skillId: editing.id,
          payload: {
            name: trimmedName,
            description: trimmedDescription,
            triggers,
            content: trimmedContent,
            confidence,
          },
        },
        {
          onSuccess: (record) => {
            toast.success(`技能已保存（v${record.version}）`)
            closeDialog()
          },
          onError,
        },
      )
      return
    }
    createMutation.mutate(
      {
        personaId,
        payload: {
          name: trimmedName,
          description: trimmedDescription,
          triggers,
          content: trimmedContent,
          confidence,
        },
      },
      {
        onSuccess: (record) => {
          toast.success(`技能已创建（${record.name}）`)
          closeDialog()
        },
        onError,
      },
    )
  }

  const handleToggle = (record: SkillRecord): void => {
    const mutation = record.status === 'active' ? disableMutation : enableMutation
    mutation.mutate(
      { personaId, skillId: record.id },
      {
        onSuccess: (updated) => {
          toast.success(updated.status === 'active' ? '技能已启用' : '技能已停用')
        },
        onError: (cause) => {
          toast.error(toApiError(cause).message)
        },
      },
    )
  }

  const busy = createMutation.isPending || updateMutation.isPending
  const skills = listQuery.data?.items ?? []

  return (
    <div className="space-y-4">
      <div className="rounded-md border border-[var(--color-info)] bg-[var(--color-info-light)] px-3 py-2 text-xs text-[var(--color-info)]">
        技能是「以后怎么做」的程序性指令：启用的技能会在你说到触发短语时以【相关技能】块注入对话；
        编辑是原地更新（版本递增），停用后不再参与匹配；夜间梦境会从历史事件里挖矿生成技能。
      </div>

      <div className="flex items-center justify-between">
        <p className="text-xs text-[var(--color-text-muted)]">
          共 {skills.length} 条技能（按使用次数降序）
        </p>
        <Button size="sm" onClick={openCreate}>
          <Plus className="mr-1 h-4 w-4" aria-hidden="true" />
          新建技能
        </Button>
      </div>

      {listQuery.isPending ? (
        <div className="flex items-center justify-center py-8">
          <Spinner />
        </div>
      ) : listQuery.isError ? (
        <Alert variant="destructive">
          <AlertDescription>
            <p>技能加载失败：{toApiError(listQuery.error).message}</p>
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
      ) : skills.length === 0 ? (
        <EmptyState
          title="还没有技能"
          description="手动写一条「以后怎么做」的步骤，或者等夜间梦境从历史里挖矿生成。"
        />
      ) : (
        <ul className="divide-y divide-[var(--color-border)] rounded-xl border border-[var(--color-border)]">
          {skills.map((skill) => (
            <li key={skill.id} className="p-3" data-testid={`skill-row-${skill.id}`}>
              <div className="flex items-start gap-3">
                <div className="min-w-0 flex-1">
                  <p className="break-words text-sm font-medium text-[var(--color-text)]">
                    {skill.name}
                  </p>
                  <p className="mt-1 break-words text-xs text-[var(--color-text-secondary)]">
                    {skill.description}
                  </p>
                  <div className="mt-2 flex flex-wrap items-center gap-1">
                    {skill.triggers.map((trigger) => (
                      <Badge key={`${skill.id}-${trigger}`} variant="secondary">
                        {trigger}
                      </Badge>
                    ))}
                  </div>
                  <div className="mt-2 flex flex-wrap items-center gap-2 text-xs text-[var(--color-text-muted)]">
                    <Badge variant={skill.status === 'active' ? 'success' : 'secondary'}>
                      {skill.status === 'active' ? '已启用' : '已停用'}
                    </Badge>
                    <Badge variant="info">{SOURCE_LABELS[skill.source]}</Badge>
                    <span>置信度 {Math.round(skill.confidence * 100)}%</span>
                    <span>命中 {skill.use_count} 次</span>
                    <span>v{skill.version}</span>
                  </div>
                </div>
                <div className="flex items-center gap-1">
                  <Button variant="outline" size="sm" onClick={() => openEdit(skill)}>
                    编辑
                  </Button>
                  <Button variant="ghost" size="sm" onClick={() => handleToggle(skill)}>
                    {skill.status === 'active' ? '停用' : '启用'}
                  </Button>
                  <Button
                    variant="ghost"
                    size="sm"
                    className="text-[var(--color-destructive)] hover:bg-[var(--color-destructive-light)]"
                    onClick={() => setDeleteTarget(skill)}
                  >
                    删除
                  </Button>
                </div>
              </div>
            </li>
          ))}
        </ul>
      )}

      <Dialog
        open={dialogMode !== null}
        onOpenChange={(open) => {
          if (!open) closeDialog()
        }}
      >
        <DialogHeader>
          <DialogTitle>{dialogMode === 'edit' ? '编辑技能' : '新建技能'}</DialogTitle>
        </DialogHeader>
        <DialogContent className="space-y-3">
          <div className="space-y-1">
            <label htmlFor="skill-name" className="text-xs text-[var(--color-text-muted)]">
              技能名称
            </label>
            <Input
              id="skill-name"
              value={name}
              maxLength={SKILL_NAME_MAX_LENGTH}
              onChange={(e) => setName(e.target.value)}
              placeholder="例如：周报流程"
              aria-invalid={error !== null}
            />
          </div>
          <div className="space-y-1">
            <label htmlFor="skill-description" className="text-xs text-[var(--color-text-muted)]">
              触发描述
            </label>
            <Textarea
              id="skill-description"
              rows={2}
              value={description}
              maxLength={SKILL_DESCRIPTION_MAX_LENGTH}
              onChange={(e) => setDescription(e.target.value)}
              placeholder="例如：主人说「写周报」时使用：先列三件事再写进展"
            />
          </div>
          <div className="space-y-1">
            <label htmlFor="skill-triggers" className="text-xs text-[var(--color-text-muted)]">
              触发短语（每行一条）
            </label>
            <Textarea
              id="skill-triggers"
              rows={3}
              value={triggersText}
              onChange={(e) => setTriggersText(e.target.value)}
              placeholder={'例如：\n写周报\n本周总结'}
            />
          </div>
          <div className="space-y-1">
            <label htmlFor="skill-content" className="text-xs text-[var(--color-text-muted)]">
              步骤正文（markdown）
            </label>
            <Textarea
              id="skill-content"
              rows={6}
              value={content}
              maxLength={SKILL_CONTENT_MAX_LENGTH}
              onChange={(e) => setContent(e.target.value)}
              placeholder={'1. 先列三件事\n2. 再写进展\n3. 最后列出待办'}
            />
          </div>
          <div className="space-y-1">
            <label htmlFor="skill-confidence" className="text-xs text-[var(--color-text-muted)]">
              置信度（0-1）
            </label>
            <Input
              id="skill-confidence"
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
            {dialogMode === 'edit' ? '保存修改' : '保存技能'}
          </Button>
        </DialogFooter>
      </Dialog>

      {deleteTarget !== null && (
        <ConfirmDialog
          open
          onOpenChange={(open) => {
            if (!open) setDeleteTarget(null)
          }}
          title="删除技能"
          description={
            <span>确认删除技能「{deleteTarget.name}」？删除后不可恢复。</span>
          }
          confirmText="删除"
          destructive
          loading={deleteMutation.isPending}
          onConfirm={() => {
            deleteMutation.mutate(
              { personaId, skillId: deleteTarget.id },
              {
                onSuccess: () => {
                  toast.success('技能已删除')
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

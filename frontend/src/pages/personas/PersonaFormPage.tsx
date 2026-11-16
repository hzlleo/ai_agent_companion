import { useEffect, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { ArrowLeft } from 'lucide-react'
import { Button } from '@/components/ui/Button'
import { Input } from '@/components/ui/Input'
import { Textarea } from '@/components/ui/Textarea'
import { Select } from '@/components/ui/Select'
import { Checkbox } from '@/components/ui/Checkbox'
import { Spinner } from '@/components/ui/Spinner'
import { Alert, AlertDescription } from '@/components/ui/Alert'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card'
import { useToast } from '@/components/ui/useToast'
import { toApiError } from '@/api/client'
import {
  useCreatePersona,
  usePersonaDetail,
  useReplacePersonaCard,
  useUpdatePersona,
} from '@/hooks/usePersonasApi'
import {
  cardFormToWire,
  createEmptyBook,
  createEmptyForm,
  formToBaseUpdateRequest,
  formToCreateRequest,
  recordToFormState,
  validatePersonaForm,
  type CharaCardFormState,
  type PersonaFormErrors,
  type PersonaFormState,
} from './personaFormModel'
import { CharacterBookEditor } from './CharacterBookEditor'
import { PersonaCardJsonPreview } from './PersonaCardJsonPreview'
import { PersonaStatesPanel } from './PersonaStatesPanel'

export interface PersonaFormPageProps {
  /** 表单模式：create = 新建；edit = 编辑既有人格。 */
  mode: 'create' | 'edit'
}

/** 卡片叙事文本字段（单行）。 */
const CARD_TEXT_FIELDS: { key: keyof Pick<CharaCardFormState, 'name' | 'description' | 'personality' | 'scenario'>; label: string; placeholder: string }[] = [
  { key: 'name', label: '称呼名', placeholder: '人格称呼名（必填，1-128 字符）' },
  { key: 'description', label: '简介', placeholder: '外观 / 背景 / 身份概述' },
  { key: 'personality', label: '性格', placeholder: '语气 / 行为倾向 / 价值观' },
  { key: 'scenario', label: '场景设定', placeholder: '当前处境与关系背景' },
]

/** 卡片长文本字段（多行 textarea）。 */
const CARD_TEXTAREA_FIELDS: { key: keyof Pick<CharaCardFormState, 'first_mes' | 'mes_example' | 'system_prompt' | 'post_history_instructions'>; label: string; placeholder: string; rows: number }[] = [
  { key: 'first_mes', label: '开场白', placeholder: '会话首条人格消息', rows: 3 },
  { key: 'mes_example', label: '对话示例', placeholder: 'few-shot 语态锚定（<START> 分隔多轮示例）', rows: 5 },
  { key: 'system_prompt', label: '系统提示覆写', placeholder: '非空时整体覆写默认人格 system 模板', rows: 4 },
  { key: 'post_history_instructions', label: '历史后指令', placeholder: '拼在对话历史之后的强约束', rows: 4 },
]

/** 字段错误取值（undefined 安全）。 */
function fieldError(errors: PersonaFormErrors, key: string): string | null {
  return errors[key] ?? null
}

/**
 * 人格创建 / 编辑表单页。
 *
 * - 基础字段：注册名 / 展示名 / 可用性状态；
 * - 卡片字段：chara card v2 全字段（叙事 + 长文本 + 世界书条目编辑器）
 *   + 实时 JSON 预览；
 * - 编辑模式分两次落库：基础字段经 PUT /personas/{id}，卡片经
 *   PUT /personas/{id}/card（整体替换语义）；
 * - 状态层（M/S）面板仅编辑模式呈现；
 * - 校验与后端契约对齐（长度 / 数值界 / 规格一致性），错误逐字段
 *   内联呈现。
 */
export default function PersonaFormPage({ mode }: PersonaFormPageProps) {
  const toast = useToast()
  const navigate = useNavigate()
  const { personaId } = useParams<{ personaId: string }>()
  const parsedId = Number(personaId)

  const [form, setForm] = useState<PersonaFormState>(createEmptyForm)
  const [errors, setErrors] = useState<PersonaFormErrors>({})
  const [submitted, setSubmitted] = useState(false)

  const isEditMode = mode === 'edit'
  const detailQuery = usePersonaDetail(isEditMode && Number.isInteger(parsedId) ? parsedId : 0)
  const createMutation = useCreatePersona()
  const updateMutation = useUpdatePersona()
  const replaceCardMutation = useReplacePersonaCard()

  // 编辑模式：记录落地为表单态（仅首次成功加载）。
  useEffect(() => {
    if (detailQuery.data !== undefined) {
      setForm(recordToFormState(detailQuery.data))
    }
  }, [detailQuery.data])

  /** 基础字段更新。 */
  const updateField = (patch: Partial<Pick<PersonaFormState, 'name' | 'display_name' | 'status'>>): void => {
    setForm((prev) => ({ ...prev, ...patch }))
  }

  /** 卡片字段更新。 */
  const updateCard = (patch: Partial<CharaCardFormState>): void => {
    setForm((prev) => ({ ...prev, card: { ...prev.card, ...patch } }))
  }

  /** 创建提交。 */
  const handleCreate = (): void => {
    const validation = validatePersonaForm(form)
    setErrors(validation)
    setSubmitted(true)
    if (Object.keys(validation).length > 0) {
      toast.error('表单校验未通过，请检查标红字段')
      return
    }
    createMutation.mutate(formToCreateRequest(form), {
      onSuccess: (record) => {
        toast.success(`人格「${record.name}」已创建（默认停用，可激活）`)
        void navigate('/personas')
      },
      onError: (error) => {
        toast.error(toApiError(error).message)
      },
    })
  }

  /** 编辑提交：基础字段 PUT /{id} + 卡片 PUT /{id}/card（整体替换）。 */
  const handleUpdate = (): void => {
    const validation = validatePersonaForm(form)
    setErrors(validation)
    setSubmitted(true)
    if (Object.keys(validation).length > 0) {
      toast.error('表单校验未通过，请检查标红字段')
      return
    }
    if (!Number.isInteger(parsedId)) {
      return
    }
    updateMutation.mutate(
      { id: parsedId, payload: formToBaseUpdateRequest(form) },
      {
        onSuccess: () => {
          replaceCardMutation.mutate(
            { id: parsedId, card: cardFormToWire(form.card) },
            {
              onSuccess: () => {
                toast.success('人格已更新（基础字段 + 卡片）')
                void navigate('/personas')
              },
              onError: (error) => {
                toast.error(`卡片替换失败：${toApiError(error).message}`)
              },
            },
          )
        },
        onError: (error) => {
          toast.error(toApiError(error).message)
        },
      },
    )
  }

  const submitPending = createMutation.isPending || updateMutation.isPending || replaceCardMutation.isPending

  // 编辑模式加载态。
  if (isEditMode) {
    if (detailQuery.isPending) {
      return (
        <div className="flex items-center justify-center py-16">
          <Spinner size="lg" />
        </div>
      )
    }
    if (detailQuery.isError) {
      return (
        <div className="p-8">
          <Alert variant="destructive">
            <AlertDescription>
              <p>人格加载失败：{toApiError(detailQuery.error).message}</p>
              <Link to="/personas" className="mt-2 inline-block text-sm underline">
                返回人格列表
              </Link>
            </AlertDescription>
          </Alert>
        </div>
      )
    }
  }

  const shouldShowError = (key: string): boolean => submitted && fieldError(errors, key) !== null

  return (
    <div className="mx-auto max-w-4xl p-8">
      <div className="mb-6 flex items-center gap-3">
        <Link
          to="/personas"
          className="inline-flex h-9 w-9 items-center justify-center rounded-md border border-[var(--color-border)] text-[var(--color-text-secondary)] hover:bg-[var(--color-surface-hover)]"
          aria-label="返回人格列表"
        >
          <ArrowLeft className="h-4 w-4" aria-hidden="true" />
        </Link>
        <h1 className="text-2xl font-semibold text-[var(--color-text)]">
          {isEditMode ? '编辑人格' : '新建人格'}
        </h1>
      </div>

      <div className="space-y-6">
        {/* 基础字段 */}
        <Card>
          <CardHeader>
            <CardTitle>基础信息</CardTitle>
          </CardHeader>
          <CardContent className="space-y-4">
            <div className="grid grid-cols-2 gap-4">
              <div className="space-y-1.5">
                <label htmlFor="persona-name" className="text-sm font-medium">
                  注册名
                </label>
                <Input
                  id="persona-name"
                  value={form.name}
                  onChange={(e) => updateField({ name: e.target.value })}
                  placeholder="全局唯一，1-128 字符"
                  aria-invalid={shouldShowError('name')}
                />
                {shouldShowError('name') && (
                  <p className="text-xs text-[var(--color-destructive)]" role="alert">
                    {fieldError(errors, 'name')}
                  </p>
                )}
              </div>
              <div className="space-y-1.5">
                <label htmlFor="persona-display-name" className="text-sm font-medium">
                  展示名（可空，回落卡片称呼名）
                </label>
                <Input
                  id="persona-display-name"
                  value={form.display_name}
                  onChange={(e) => updateField({ display_name: e.target.value })}
                  placeholder="留空时使用卡片称呼名"
                  aria-invalid={shouldShowError('display_name')}
                />
                {shouldShowError('display_name') && (
                  <p className="text-xs text-[var(--color-destructive)]" role="alert">
                    {fieldError(errors, 'display_name')}
                  </p>
                )}
              </div>
            </div>
            <div className="w-56 space-y-1.5">
              <label htmlFor="persona-status" className="text-sm font-medium">
                可用性状态
              </label>
              <Select
                id="persona-status"
                value={form.status}
                onChange={(e) =>
                  updateField({ status: e.target.value === 'active' ? 'active' : 'disabled' })
                }
              >
                <option value="disabled">停用（默认）</option>
                <option value="active">启用</option>
              </Select>
              <p className="text-xs text-[var(--color-text-muted)]">
                启用是激活的前置条件；激活位经列表页「激活」操作切换。
              </p>
            </div>
          </CardContent>
        </Card>

        {/* 卡片字段 */}
        <Card>
          <CardHeader>
            <CardTitle>人格卡片（chara card v2）</CardTitle>
          </CardHeader>
          <CardContent className="space-y-4">
            {CARD_TEXT_FIELDS.map((field) => (
              <div key={field.key} className="space-y-1.5">
                <label htmlFor={`card-${field.key}`} className="text-sm font-medium">
                  {field.label}
                </label>
                <Input
                  id={`card-${field.key}`}
                  value={form.card[field.key]}
                  onChange={(e) => updateCard({ [field.key]: e.target.value })}
                  placeholder={field.placeholder}
                  aria-invalid={field.key === 'name' && shouldShowError('card.name')}
                />
                {field.key === 'name' && shouldShowError('card.name') && (
                  <p className="text-xs text-[var(--color-destructive)]" role="alert">
                    {fieldError(errors, 'card.name')}
                  </p>
                )}
              </div>
            ))}
            {CARD_TEXTAREA_FIELDS.map((field) => (
              <div key={field.key} className="space-y-1.5">
                <label htmlFor={`card-${field.key}`} className="text-sm font-medium">
                  {field.label}
                </label>
                <Textarea
                  id={`card-${field.key}`}
                  rows={field.rows}
                  value={form.card[field.key]}
                  onChange={(e) => updateCard({ [field.key]: e.target.value })}
                  placeholder={field.placeholder}
                />
              </div>
            ))}

            {/* 世界书开关 + 编辑器 */}
            <div className="space-y-3 border-t border-[var(--color-border)] pt-4">
              <Checkbox
                checked={form.card.character_book !== null}
                onChange={(e) =>
                  updateCard({ character_book: e.target.checked ? createEmptyBook() : null })
                }
                label="启用世界书（character_book，检索增强条目集）"
              />
              {form.card.character_book !== null && (
                <CharacterBookEditor
                  book={form.card.character_book}
                  onChange={(book) => updateCard({ character_book: book })}
                  errors={errors}
                />
              )}
            </div>

            {/* JSON 预览 */}
            <div className="border-t border-[var(--color-border)] pt-4">
              <PersonaCardJsonPreview card={form.card} />
            </div>
          </CardContent>
        </Card>

        {/* 状态层（仅编辑模式） */}
        {isEditMode && Number.isInteger(parsedId) && (
          <Card>
            <CardHeader>
              <CardTitle>状态层（M / S）</CardTitle>
            </CardHeader>
            <CardContent>
              <PersonaStatesPanel personaId={parsedId} />
            </CardContent>
          </Card>
        )}

        {/* 提交区 */}
        <div className="flex justify-end gap-2">
          <Link
            to="/personas"
            className="inline-flex h-[38px] items-center rounded-md border border-[var(--color-border)] px-4 text-sm font-medium hover:bg-[var(--color-surface-hover)]"
          >
            取消
          </Link>
          <Button
            size="form"
            loading={submitPending}
            onClick={() => {
              if (isEditMode) {
                handleUpdate()
              } else {
                handleCreate()
              }
            }}
          >
            {isEditMode ? '保存修改' : '创建人格'}
          </Button>
        </div>
      </div>
    </div>
  )
}

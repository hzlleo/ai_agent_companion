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
import { toApiError } from '@/api/client'
import { useCreateModelEntry, useUpdateModelEntry } from '@/hooks/useModelsApi'
import type { ModelEntryMasked, ModelRole } from '@/types/model-registry'
import {
  MODEL_ROLES,
  ROLE_LABELS,
  createEmptyEntryForm,
  entryToFormState,
  formToCreateRequest,
  formToUpdateRequest,
  validateEntryForm,
  type ModelEntryFormErrors,
  type ModelEntryFormState,
} from './modelsFormModel'

export interface ModelEntryDialogProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  /** 表单模式：create = 新建；edit = 编辑既有条目。 */
  mode: 'create' | 'edit'
  /** 编辑锚点（edit 模式必传；create 模式忽略）。 */
  entry: ModelEntryMasked | null
}

/** 字段错误取值（undefined 安全）。 */
function fieldError(errors: ModelEntryFormErrors, key: string): string | null {
  return errors[key] ?? null
}

/**
 * 模型条目创建 / 编辑对话框：typed 表单（name 编辑期只读 / base_url
 * http(s) 校验 / 五角色勾选 / concurrency 三态——留空=不变或全局默认、
 * 勾选重置=显式 null）。密钥只接受环境变量名（api_key_env）。
 */
export function ModelEntryDialog({ open, onOpenChange, mode, entry }: ModelEntryDialogProps) {
  const toast = useToast()
  const createMutation = useCreateModelEntry()
  const updateMutation = useUpdateModelEntry()
  const [form, setForm] = useState<ModelEntryFormState>(createEmptyEntryForm)
  const [errors, setErrors] = useState<ModelEntryFormErrors>({})

  useEffect(() => {
    if (open) {
      setForm(entry !== null ? entryToFormState(entry) : createEmptyEntryForm())
      setErrors({})
    }
  }, [open, entry])

  const toggleRole = (role: ModelRole, checked: boolean): void => {
    setForm((prev) => ({
      ...prev,
      roles: checked ? [...prev.roles, role] : prev.roles.filter((item) => item !== role),
    }))
  }

  const handleConfirm = (): void => {
    const validation = validateEntryForm(form, mode)
    setErrors(validation)
    if (Object.keys(validation).length > 0) {
      toast.error('表单校验未通过，请检查标红字段')
      return
    }
    if (mode === 'create') {
      createMutation.mutate(formToCreateRequest(form), {
        onSuccess: (state) => {
          toast.success(`模型条目「${form.name.trim()}」已创建（注册表现存 ${state.entries.length} 条）`)
          onOpenChange(false)
        },
        onError: (error) => {
          toast.error(toApiError(error).message)
        },
      })
      return
    }
    updateMutation.mutate(
      { name: form.name, payload: formToUpdateRequest(form) },
      {
        onSuccess: () => {
          toast.success(`模型条目「${form.name}」已更新`)
          onOpenChange(false)
        },
        onError: (error) => {
          toast.error(toApiError(error).message)
        },
      },
    )
  }

  const pending = createMutation.isPending || updateMutation.isPending

  return (
    <Dialog open={open} onOpenChange={onOpenChange} size="lg">
      <DialogHeader>
        <DialogTitle>{mode === 'create' ? '新建模型条目' : `编辑模型条目（${form.name}）`}</DialogTitle>
        <DialogClose onClose={() => onOpenChange(false)} />
      </DialogHeader>
      <DialogContent>
        <div className="space-y-4">
          <div className="grid grid-cols-2 gap-4">
            <div className="space-y-1.5">
              <label htmlFor="entry-name" className="text-sm font-medium">
                条目名（唯一标识）
              </label>
              <Input
                id="entry-name"
                value={form.name}
                onChange={(e) => setForm((prev) => ({ ...prev, name: e.target.value }))}
                placeholder="全局唯一，1-128 字符"
                disabled={mode === 'edit'}
                aria-invalid={fieldError(errors, 'name') !== null}
              />
              {mode === 'edit' && (
                <p className="text-xs text-[var(--color-text-muted)]">条目名不可变（路径标识）。</p>
              )}
              {fieldError(errors, 'name') !== null && (
                <p className="text-xs text-[var(--color-destructive)]" role="alert">
                  {fieldError(errors, 'name')}
                </p>
              )}
            </div>
            <div className="space-y-1.5">
              <label htmlFor="entry-provider" className="text-sm font-medium">
                provider 类型
              </label>
              <Input
                id="entry-provider"
                value={form.provider}
                onChange={(e) => setForm((prev) => ({ ...prev, provider: e.target.value }))}
                placeholder="默认 openai_compatible"
                aria-invalid={fieldError(errors, 'provider') !== null}
              />
              {fieldError(errors, 'provider') !== null && (
                <p className="text-xs text-[var(--color-destructive)]" role="alert">
                  {fieldError(errors, 'provider')}
                </p>
              )}
            </div>
            <div className="space-y-1.5">
              <label htmlFor="entry-base-url" className="text-sm font-medium">
                API 基础 URL
              </label>
              <Input
                id="entry-base-url"
                value={form.base_url}
                onChange={(e) => setForm((prev) => ({ ...prev, base_url: e.target.value }))}
                placeholder="http(s) URL，OpenAI 兼容端点含 /v1"
                aria-invalid={fieldError(errors, 'base_url') !== null}
              />
              {fieldError(errors, 'base_url') !== null && (
                <p className="text-xs text-[var(--color-destructive)]" role="alert">
                  {fieldError(errors, 'base_url')}
                </p>
              )}
            </div>
            <div className="space-y-1.5">
              <label htmlFor="entry-model" className="text-sm font-medium">
                模型名
              </label>
              <Input
                id="entry-model"
                value={form.model}
                onChange={(e) => setForm((prev) => ({ ...prev, model: e.target.value }))}
                placeholder="传给 provider 的 model 参数"
                aria-invalid={fieldError(errors, 'model') !== null}
              />
              {fieldError(errors, 'model') !== null && (
                <p className="text-xs text-[var(--color-destructive)]" role="alert">
                  {fieldError(errors, 'model')}
                </p>
              )}
            </div>
            <div className="space-y-1.5">
              <label htmlFor="entry-api-key-env" className="text-sm font-medium">
                API Key 环境变量名
              </label>
              <Input
                id="entry-api-key-env"
                value={form.api_key_env}
                onChange={(e) => setForm((prev) => ({ ...prev, api_key_env: e.target.value }))}
                placeholder="留空 = 本地免鉴权端点（只接受变量名，非密钥）"
                aria-invalid={fieldError(errors, 'api_key_env') !== null}
              />
              {fieldError(errors, 'api_key_env') !== null && (
                <p className="text-xs text-[var(--color-destructive)]" role="alert">
                  {fieldError(errors, 'api_key_env')}
                </p>
              )}
            </div>
            <div className="space-y-1.5">
              <label htmlFor="entry-concurrency" className="text-sm font-medium">
                并发上限
              </label>
              <Input
                id="entry-concurrency"
                type="number"
                min={1}
                value={form.concurrency}
                onChange={(e) => setForm((prev) => ({ ...prev, concurrency: e.target.value }))}
                placeholder={mode === 'create' ? '留空 = 全局默认' : '留空 = 保持不变'}
                disabled={form.resetConcurrency}
                aria-invalid={fieldError(errors, 'concurrency') !== null}
              />
              {mode === 'edit' && (
                <Checkbox
                  checked={form.resetConcurrency}
                  onChange={(e) => setForm((prev) => ({ ...prev, resetConcurrency: e.target.checked }))}
                  label="重置为全局默认"
                />
              )}
              {fieldError(errors, 'concurrency') !== null && (
                <p className="text-xs text-[var(--color-destructive)]" role="alert">
                  {fieldError(errors, 'concurrency')}
                </p>
              )}
            </div>
          </div>
          <fieldset className="space-y-2">
            <legend className="text-sm font-medium">声明角色集（解析优先级：显式绑定 &gt; 声明）</legend>
            <div className="flex flex-wrap gap-2">
              {MODEL_ROLES.map((role) => (
                <Checkbox
                  key={role}
                  checked={form.roles.includes(role)}
                  onChange={(e) => toggleRole(role, e.target.checked)}
                  label={ROLE_LABELS[role]}
                />
              ))}
            </div>
          </fieldset>
          <Checkbox
            checked={form.multimodal}
            onChange={(e) => setForm((prev) => ({ ...prev, multimodal: e.target.checked }))}
            label="支持多模态输入（look_at 链路回退判据）"
          />
          <p className="text-xs text-[var(--color-text-muted)]">
            密钥不变式：表单只接受环境变量名——密钥值经 .env / 进程环境变量进入，管理面永不接收明文。
          </p>
        </div>
      </DialogContent>
      <DialogFooter>
        <Button variant="outline" onClick={() => onOpenChange(false)} disabled={pending}>
          取消
        </Button>
        <Button onClick={handleConfirm} loading={pending}>
          {mode === 'create' ? '创建' : '保存'}
        </Button>
      </DialogFooter>
    </Dialog>
  )
}

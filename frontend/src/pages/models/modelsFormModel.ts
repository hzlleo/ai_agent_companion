/**
 * 模型条目表单模型（typed form model）：字段态 / 校验 / 请求体转换。
 *
 * 本工程约定：
 * - 创建与编辑共用一套字段态（编辑期 name 只读——后端不可变标识）；
 * - 校验与后端 DTO 契约对齐（name 1-128 / base_url http(s) / model
 *   1-256 / api_key_env ≤128 / concurrency ≥1 整数或留空），前端先拦
 *   一道，后端 400/409 形态仍可能到达（服务端校验兜底）；
 * - concurrency 的三态：留空 = 创建时全局默认 / 编辑时不变；
 *   「重置全局默认」勾选（仅编辑）= 显式 null。
 */

import type {
  ModelEntryCreateRequest,
  ModelEntryMasked,
  ModelEntryUpdateRequest,
  ModelRole,
} from '@/types/model-registry'

/** 五角色全集（表单勾选面）。 */
export const MODEL_ROLES: ModelRole[] = ['chat', 'duty', 'dream', 'look_at', 'embed']

/** 角色 → 中文标签。 */
export const ROLE_LABELS: Record<ModelRole, string> = {
  chat: '主对话',
  duty: '值班任务',
  dream: '梦境生成',
  look_at: '多模态查看',
  embed: '向量化',
}

/** 模型条目表单字段态。 */
export interface ModelEntryFormState {
  /** 条目唯一标识（编辑期只读）。 */
  name: string
  /** provider 类型。 */
  provider: string
  /** API 基础 URL（http(s)）。 */
  base_url: string
  /** API Key 环境变量名（空 = 免鉴权端点）。 */
  api_key_env: string
  /** 模型名。 */
  model: string
  /** 声明角色集（勾选面）。 */
  roles: ModelRole[]
  /** 并发上限（字符串态；空 = 创建全局默认 / 编辑不变）。 */
  concurrency: string
  /** 编辑期显式重置并发为全局默认（请求体 concurrency=null）。 */
  resetConcurrency: boolean
  /** 是否支持多模态输入。 */
  multimodal: boolean
}

/** 字段错误集（字段名 → 中文错误文案）。 */
export type ModelEntryFormErrors = Record<string, string>

/** 新建空表单。 */
export function createEmptyEntryForm(): ModelEntryFormState {
  return {
    name: '',
    provider: 'openai_compatible',
    base_url: '',
    api_key_env: '',
    model: '',
    roles: [],
    concurrency: '',
    resetConcurrency: false,
    multimodal: false,
  }
}

/** 条目读取面 → 表单态（编辑锚点预填）。 */
export function entryToFormState(entry: ModelEntryMasked): ModelEntryFormState {
  return {
    name: entry.name,
    provider: entry.provider,
    base_url: entry.base_url,
    api_key_env: entry.api_key_env,
    model: entry.model,
    roles: entry.roles.filter((role): role is ModelRole =>
      (MODEL_ROLES as string[]).includes(role),
    ),
    concurrency: entry.concurrency === null ? '' : String(entry.concurrency),
    resetConcurrency: false,
    multimodal: entry.multimodal,
  }
}

/** base_url 校验（http(s) scheme + 非空 host——与后端 _validate_http_base_url 对齐）。 */
export function isValidHttpBaseUrl(value: string): boolean {
  try {
    const parsed = new URL(value.trim())
    return (parsed.protocol === 'http:' || parsed.protocol === 'https:') && parsed.host !== ''
  } catch {
    return false
  }
}

/**
 * 表单整体校验：
 * - name（仅创建必校验）：1-128 字符；
 * - provider：必填；
 * - base_url：必填且为 http(s) URL；
 * - model：1-256 字符；
 * - api_key_env：≤128 字符；
 * - concurrency：留空合法；填了须为 ≥1 整数。
 *
 * @returns 错误集（空对象 = 校验通过）。
 */
export function validateEntryForm(form: ModelEntryFormState, mode: 'create' | 'edit'): ModelEntryFormErrors {
  const errors: ModelEntryFormErrors = {}
  if (mode === 'create') {
    const name = form.name.trim()
    if (name.length < 1 || name.length > 128) {
      errors['name'] = '条目名必填，且长度为 1-128 个字符'
    }
  }
  if (form.provider.trim() === '') {
    errors['provider'] = 'provider 类型必填'
  }
  if (!isValidHttpBaseUrl(form.base_url)) {
    errors['base_url'] = 'API 基础 URL 必须为 http(s) URL（如 http://127.0.0.1:8001/v1）'
  }
  const model = form.model.trim()
  if (model.length < 1 || model.length > 256) {
    errors['model'] = '模型名必填，且长度为 1-256 个字符'
  }
  if (form.api_key_env.length > 128) {
    errors['api_key_env'] = 'API Key 环境变量名长度不得超过 128 个字符'
  }
  if (!form.resetConcurrency && form.concurrency.trim() !== '') {
    const parsed = Number(form.concurrency)
    if (!Number.isInteger(parsed) || parsed < 1) {
      errors['concurrency'] = '并发上限须为不小于 1 的整数（留空 = 全局默认/不变）'
    }
  }
  return errors
}

/** 并发上限收敛（表单字符串 → 请求体数值语义）。 */
function concurrencyToRequestValue(form: ModelEntryFormState): number | null | undefined {
  if (form.resetConcurrency) {
    return null
  }
  const trimmed = form.concurrency.trim()
  if (trimmed === '') {
    return undefined
  }
  const parsed = Number(trimmed)
  return Number.isInteger(parsed) && parsed >= 1 ? parsed : undefined
}

/** 表单态 → 创建请求体。 */
export function formToCreateRequest(form: ModelEntryFormState): ModelEntryCreateRequest {
  return {
    name: form.name.trim(),
    provider: form.provider.trim(),
    base_url: form.base_url.trim(),
    api_key_env: form.api_key_env.trim(),
    model: form.model.trim(),
    roles: form.roles,
    concurrency: concurrencyToRequestValue(form) ?? null,
    multimodal: form.multimodal,
  }
}

/** 表单态 → 部分更新请求体（name 不携带——不可变；concurrency 三态见模块 docstring）。 */
export function formToUpdateRequest(form: ModelEntryFormState): ModelEntryUpdateRequest {
  const concurrency = concurrencyToRequestValue(form)
  return {
    provider: form.provider.trim(),
    base_url: form.base_url.trim(),
    api_key_env: form.api_key_env.trim(),
    model: form.model.trim(),
    roles: form.roles,
    ...(concurrency === undefined ? {} : { concurrency }),
    multimodal: form.multimodal,
  }
}

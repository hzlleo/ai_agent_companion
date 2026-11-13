/**
 * 情景事件注入表单模型（typed form model）：字段态 / 校验 / 请求体转换。
 *
 * 本工程约定：
 * - 表单态用字符串承载数值（输入未定形），提交期一次性收敛为 wire
 *   形态（EpisodeCreateRequest）；
 * - JSON 载荷编辑器按 kind 判别校验（与后端 _DTO_PAYLOAD_BY_KIND 契约
 *   对齐——未知键忽略、字段类型不符前置拦截），前端先拦一道，后端
 *   400/422 形态仍可能到达（服务端校验兜底）。
 */

import type {
  EpisodeCreateRequest,
  EpisodeKind,
  EpisodePayload,
  MemoryProvenance,
} from '@/types/memory'
import { EPISODE_PAYLOAD_TEMPLATES } from './memoryDisplay'

/** 情景注入表单字段态。 */
export interface EpisodeFormState {
  /** 事件类型。 */
  kind: EpisodeKind
  /** 事件来源标识（1-128 字符）。 */
  source: string
  /** 重要度（字符串态，收敛期转 1-10 整数）。 */
  importance: string
  /** 来源可信度。 */
  provenance: MemoryProvenance
  /** 载荷 JSON 文本（按 kind 判别校验）。 */
  payloadText: string
}

/** 字段错误集（字段名 → 中文错误文案）。 */
export type EpisodeFormErrors = Record<string, string>

/** 新建空表单（载荷模板随 kind 切换由对话框维护）。 */
export function createEmptyEpisodeForm(): EpisodeFormState {
  return {
    kind: 'chat',
    source: '',
    importance: '5',
    provenance: 'owner',
    payloadText: EPISODE_PAYLOAD_TEMPLATES.chat,
  }
}

/** kind 切换后的载荷模板（编辑中载荷整体替换为对应骨架）。 */
export function payloadTemplateForKind(kind: EpisodeKind): string {
  return EPISODE_PAYLOAD_TEMPLATES[kind]
}

/** 可选字符串字段读取（缺席 / 类型不符 → null）。 */
function optionalString(record: Record<string, unknown>, key: string): string | null {
  const value = record[key]
  return typeof value === 'string' ? value : null
}

/** 可选自由 JSON 字段读取（缺席 / 非对象 → 空对象）。 */
function optionalRecord(record: Record<string, unknown>, key: string): Record<string, unknown> {
  const value = record[key]
  if (typeof value === 'object' && value !== null && !Array.isArray(value)) {
    return value as Record<string, unknown>
  }
  return {}
}

/** 可选会话主键读取（缺席 / 非数值 → null）。 */
function optionalSessionId(record: Record<string, unknown>): number | null {
  const value = record['session_id']
  return typeof value === 'number' && Number.isInteger(value) ? value : null
}

/**
 * 载荷 JSON 文本 → 按 kind 判别的类型化载荷。
 *
 * 校验面（与后端载荷 Model Class 对齐）：必须为 JSON 对象；已知字段
 * 类型不符即报错；未知键忽略（后端 pydantic 同语义丢弃）。
 */
export function parseEpisodePayload(
  kind: EpisodeKind,
  text: string,
): { payload: EpisodePayload } | { error: string } {
  let parsed: unknown
  try {
    parsed = JSON.parse(text)
  } catch {
    return { error: 'JSON 解析失败，请检查格式' }
  }
  if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) {
    return { error: '事件载荷必须为 JSON 对象' }
  }
  const record = parsed as Record<string, unknown>

  if (kind === 'chat') {
    for (const key of ['user_content', 'assistant_content']) {
      if (key in record && typeof record[key] !== 'string') {
        return { error: `chat 载荷的 ${key} 须为字符串` }
      }
    }
    if ('session_id' in record && record['session_id'] !== null && typeof record['session_id'] !== 'number') {
      return { error: 'chat 载荷的 session_id 须为数值或 null' }
    }
    return {
      payload: {
        user_content: optionalString(record, 'user_content') ?? '',
        assistant_content: optionalString(record, 'assistant_content') ?? '',
        session_id: optionalSessionId(record),
      },
    }
  }
  if (kind === 'duty') {
    for (const key of ['title', 'summary']) {
      if (key in record && typeof record[key] !== 'string') {
        return { error: `duty 载荷的 ${key} 须为字符串` }
      }
    }
    if ('details' in record && (typeof record['details'] !== 'object' || record['details'] === null || Array.isArray(record['details']))) {
      return { error: 'duty 载荷的 details 须为 JSON 对象' }
    }
    return {
      payload: {
        title: optionalString(record, 'title') ?? '',
        summary: optionalString(record, 'summary') ?? '',
        details: optionalRecord(record, 'details'),
      },
    }
  }
  if (kind === 'platform') {
    for (const key of ['action', 'summary']) {
      if (key in record && typeof record[key] !== 'string') {
        return { error: `platform 载荷的 ${key} 须为字符串` }
      }
    }
    return {
      payload: {
        action: optionalString(record, 'action') ?? '',
        summary: optionalString(record, 'summary') ?? '',
        metadata: optionalRecord(record, 'metadata'),
      },
    }
  }
  // dream / system：通用注记载荷。
  if ('text' in record && typeof record['text'] !== 'string') {
    return { error: '注记载荷的 text 须为字符串' }
  }
  return {
    payload: {
      text: optionalString(record, 'text') ?? '',
      metadata: optionalRecord(record, 'metadata'),
    },
  }
}

/**
 * 表单整体校验（与后端契约对齐）：
 * - source 必填，1-128 字符；
 * - importance 须为 1-10 整数；
 * - 载荷 JSON 按 kind 判别校验。
 *
 * @returns 错误集（空对象 = 校验通过）。
 */
export function validateEpisodeForm(form: EpisodeFormState): EpisodeFormErrors {
  const errors: EpisodeFormErrors = {}
  const source = form.source.trim()
  if (source.length < 1 || source.length > 128) {
    errors['source'] = '事件来源必填，且长度为 1-128 个字符'
  }
  const importance = Number(form.importance)
  if (!Number.isInteger(importance) || importance < 1 || importance > 10) {
    errors['importance'] = '重要度须为 1-10 的整数'
  }
  const parsed = parseEpisodePayload(form.kind, form.payloadText)
  if ('error' in parsed) {
    errors['payloadText'] = parsed.error
  }
  return errors
}

/** 表单态 → 创建请求体（ts_ms 恒 null——注入即落库当前时间）。 */
export function formToEpisodeRequest(form: EpisodeFormState): EpisodeCreateRequest | null {
  const parsed = parseEpisodePayload(form.kind, form.payloadText)
  if ('error' in parsed) {
    return null
  }
  return {
    kind: form.kind,
    source: form.source.trim(),
    content: parsed.payload,
    importance: Number(form.importance),
    provenance: form.provenance,
    ts_ms: null,
  }
}

/**
 * personas 端点层：后端 17 个路由的 typed 封装（多激活可寻址模型）。
 *
 * 路由面（全部经 ResultDTO 解包，失败抛 ApiError）：
 *   - GET    /personas                        游标分页列表（status 过滤）
 *   - POST   /personas                        创建
 *   - POST   /personas/batch-delete           批量删除（部分成功形态）
 *   - GET    /personas/primary                读取当前主人格（无主人格 404）
 *   - GET    /personas/presets                官方特质预设库（画廊载荷）
 *   - POST   /personas/import                 导入（JSON 预设/卡片 或 multipart 文件）
 *   - POST   /personas/import/batch           批量导入（multipart 多文件，部分成功）
 *   - GET    /personas/{id}                   读取
 *   - PUT    /personas/{id}                   部分更新
 *   - DELETE /personas/{id}                   删除（主人格守卫）
 *   - POST   /personas/{id}/copy              复制（可选深拷贝 M/S 状态）
 *   - POST   /personas/{id}/set-primary       设为主人格（事务性切换）
 *   - POST   /personas/{id}/disable           停用（生命周期；主人格守卫拒绝）
 *   - GET    /personas/{id}/card              读取卡片
 *   - PUT    /personas/{id}/card              整体替换卡片
 *   - GET    /personas/{id}/states            读取已写状态层
 *   - PUT    /personas/{id}/states/{layer}    状态层 upsert（M/S）
 *
 * multipart 面（导入端点）：FormData 经 axios 原生序列化（浏览器侧由
 * 实现自动携带 boundary——axios 对 FormData 请求移除实例级 JSON 头）。
 */

import { request } from './client'
import type { PageResponse } from '@/types/common'
import type {
  CharaCard,
  PersonaBatchDeleteResponse,
  PersonaCardResponse,
  PersonaCopyRequest,
  PersonaCreateRequest,
  PersonaImportBatchResult,
  PersonaImportConflictStrategy,
  PersonaImportRequest,
  PersonaImportResult,
  PersonaListParams,
  PersonaRecord,
  PersonaState,
  PersonaStateLayer,
  PersonaStateUpsertRequest,
  PersonaUpdateRequest,
  TraitLibrary,
} from '@/types/persona'

/** GET /personas —— 游标分页列表（status 过滤；id 降序）。 */
export async function listPersonas(params: PersonaListParams): Promise<PageResponse<PersonaRecord>> {
  return request<PageResponse<PersonaRecord>>({
    method: 'GET',
    url: '/personas',
    params,
  })
}

/** POST /personas —— 创建人格（注册名唯一；status 缺省 disabled）。 */
export async function createPersona(payload: PersonaCreateRequest): Promise<PersonaRecord> {
  return request<PersonaRecord>({
    method: 'POST',
    url: '/personas',
    data: payload,
  })
}

/** POST /personas/batch-delete —— 批量删除（逐 id 独立事务，部分成功）。 */
export async function batchDeletePersonas(ids: number[]): Promise<PersonaBatchDeleteResponse> {
  return request<PersonaBatchDeleteResponse>({
    method: 'POST',
    url: '/personas/batch-delete',
    data: { ids },
  })
}

/** GET /personas/{id} —— 按主键读取人格。 */
export async function getPersona(personaId: number): Promise<PersonaRecord> {
  return request<PersonaRecord>({
    method: 'GET',
    url: `/personas/${personaId}`,
  })
}

/** PUT /personas/{id} —— 部分更新人格（仅出现字段生效）。 */
export async function updatePersona(
  personaId: number,
  payload: PersonaUpdateRequest,
): Promise<PersonaRecord> {
  return request<PersonaRecord>({
    method: 'PUT',
    url: `/personas/${personaId}`,
    data: payload,
  })
}

/** DELETE /personas/{id} —— 删除人格（当前主人格 409 拒绝）。 */
export async function deletePersona(personaId: number): Promise<void> {
  await request<null>({
    method: 'DELETE',
    url: `/personas/${personaId}`,
  })
}

/** POST /personas/{id}/copy —— 复制人格（新注册名；产物恒 disabled）。 */
export async function copyPersona(
  personaId: number,
  payload: PersonaCopyRequest,
): Promise<PersonaRecord> {
  return request<PersonaRecord>({
    method: 'POST',
    url: `/personas/${personaId}/copy`,
    data: payload,
  })
}

/** GET /personas/primary —— 读取当前主人格（无主人格 404 fail loud）。 */
export async function getPrimaryPersona(): Promise<PersonaRecord> {
  return request<PersonaRecord>({
    method: 'GET',
    url: '/personas/primary',
  })
}

/** POST /personas/{id}/set-primary —— 设为主人格（事务性切换）。 */
export async function setPrimaryPersona(personaId: number): Promise<PersonaRecord> {
  return request<PersonaRecord>({
    method: 'POST',
    url: `/personas/${personaId}/set-primary`,
  })
}

/** POST /personas/{id}/disable —— 停用人格（主人格 409 拒绝）。 */
export async function disablePersona(personaId: number): Promise<PersonaRecord> {
  return request<PersonaRecord>({
    method: 'POST',
    url: `/personas/${personaId}/disable`,
  })
}

/** GET /personas/{id}/card —— 读取人格卡片。 */
export async function getPersonaCard(personaId: number): Promise<PersonaCardResponse> {
  return request<PersonaCardResponse>({
    method: 'GET',
    url: `/personas/${personaId}/card`,
  })
}

/** PUT /personas/{id}/card —— 整体替换人格卡片（PUT 语义，非补丁）。 */
export async function replacePersonaCard(
  personaId: number,
  card: CharaCard,
): Promise<PersonaCardResponse> {
  return request<PersonaCardResponse>({
    method: 'PUT',
    url: `/personas/${personaId}/card`,
    data: card,
  })
}

/** GET /personas/{id}/states —— 读取全部已写状态层（M/S）。 */
export async function getPersonaStates(personaId: number): Promise<PersonaState[]> {
  return request<PersonaState[]>({
    method: 'GET',
    url: `/personas/${personaId}/states`,
  })
}

/** PUT /personas/{id}/states/{layer} —— 状态层 upsert（state 整体替换）。 */
export async function upsertPersonaState(
  personaId: number,
  layer: PersonaStateLayer,
  payload: PersonaStateUpsertRequest,
): Promise<PersonaState> {
  return request<PersonaState>({
    method: 'PUT',
    url: `/personas/${personaId}/states/${layer}`,
    data: payload,
  })
}

/** GET /personas/presets —— 官方特质预设库（预设 + 组件注册表画廊载荷）。 */
export async function getTraitLibrary(): Promise<TraitLibrary> {
  return request<TraitLibrary>({
    method: 'GET',
    url: '/personas/presets',
  })
}

/** POST /personas/import —— 导入（JSON 面：官方预设或用户卡片文档）。 */
export async function importPersona(payload: PersonaImportRequest): Promise<PersonaImportResult> {
  return request<PersonaImportResult>({
    method: 'POST',
    url: '/personas/import',
    data: payload,
  })
}

/** multipart 单文件导入的可选控制面。 */
export interface ImportFileOptions {
  /** 名称冲突策略（默认 skip——内容同卡恒幂等，策略不适用）。 */
  conflictStrategy?: PersonaImportConflictStrategy
  /** 期望注册名（缺省 = 卡片名）。 */
  desiredName?: string
}

/** multipart 部件（文本载荷——卡片 JSON 文档即 UTF-8 文本）。 */
interface MultipartPart {
  /** 字段名（file / files / conflict_strategy / desired_name）。 */
  field: string
  /** 文件名（文件部件必带；文本部件缺省）。 */
  filename?: string
  /** 部件正文（UTF-8 文本）。 */
  content: string
}

/** 逐请求随机边界（RFC 2046——边界串不出现在部件内容即合法）。 */
function multipartBoundary(): string {
  return `----aioc-frontend-${Math.random().toString(36).slice(2)}-${Date.now().toString(36)}`
}

/**
 * 构造 multipart/form-data 请求体（确定性手工序列化——与后端 voice.asr
 * 的 stdlib email 解析器互为对端的同哲学）。
 *
 * 不走 FormData 平台序列化的原因（实证）：axios 在 JSON 实例头下会把
 * FormData 改写为 JSON；且拦截器链（msw / undici Request）对跨实现
 * File 对象不保留文件名（filename 退化为 blob）。卡片文档恒为 UTF-8
 * 文本——手工 RFC 2046 序列化在浏览器与测试环境产出逐字节确定的
 * wire 形态（文件名 / 字段序 / 边界全可控）。
 */
function buildMultipartBody(parts: MultipartPart[]): { body: string; contentType: string } {
  const boundary = multipartBoundary()
  const segments = parts.map((part) => {
    const disposition =
      part.filename !== undefined
        ? `form-data; name="${part.field}"; filename="${part.filename.replace(/"/g, '\\"')}"`
        : `form-data; name="${part.field}"`
    const headers =
      part.filename !== undefined
        ? ['Content-Disposition: ' + disposition, 'Content-Type: application/json']
        : ['Content-Disposition: ' + disposition]
    return [`--${boundary}`, ...headers, '', part.content].join('\r\n')
  })
  const body = `${segments.join('\r\n')}\r\n--${boundary}--`
  return { body, contentType: `multipart/form-data; boundary=${boundary}` }
}

/** POST /personas/import —— 导入（multipart 面：卡片 JSON 文件上传）。 */
export async function importPersonaFile(
  file: File,
  options: ImportFileOptions = {},
): Promise<PersonaImportResult> {
  const parts: MultipartPart[] = [
    { field: 'file', filename: file.name, content: await file.text() },
  ]
  if (options.conflictStrategy !== undefined) {
    parts.push({ field: 'conflict_strategy', content: options.conflictStrategy })
  }
  if (options.desiredName !== undefined) {
    parts.push({ field: 'desired_name', content: options.desiredName })
  }
  const { body, contentType } = buildMultipartBody(parts)
  return request<PersonaImportResult>({
    method: 'POST',
    url: '/personas/import',
    data: body,
    headers: { 'Content-Type': contentType },
  })
}

/** POST /personas/import/batch —— 批量导入（multipart 多文件，部分成功形态）。 */
export async function importPersonasBatch(
  files: File[],
  conflictStrategy: PersonaImportConflictStrategy = 'skip',
): Promise<PersonaImportBatchResult> {
  const parts: MultipartPart[] = []
  for (const file of files) {
    parts.push({ field: 'files', filename: file.name, content: await file.text() })
  }
  parts.push({ field: 'conflict_strategy', content: conflictStrategy })
  const { body, contentType } = buildMultipartBody(parts)
  return request<PersonaImportBatchResult>({
    method: 'POST',
    url: '/personas/import/batch',
    data: body,
    headers: { 'Content-Type': contentType },
  })
}

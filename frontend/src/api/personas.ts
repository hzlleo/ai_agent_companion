/**
 * personas 端点层：后端 13 个路由的typed 封装。
 *
 * 路由面（全部经 ResultDTO 解包，失败抛 ApiError）：
 *   - GET    /personas                        游标分页列表（status 过滤）
 *   - POST   /personas                        创建
 *   - POST   /personas/batch-delete           批量删除（部分成功形态）
 *   - GET    /personas/{id}                   读取
 *   - PUT    /personas/{id}                   部分更新
 *   - DELETE /personas/{id}                   删除（激活守卫）
 *   - POST   /personas/{id}/copy              复制（可选深拷贝 M/S 状态）
 *   - POST   /personas/{id}/activate          激活（事务性单激活切换）
 *   - POST   /personas/{id}/deactivate        停用激活
 *   - GET    /personas/{id}/card              读取卡片
 *   - PUT    /personas/{id}/card              整体替换卡片
 *   - GET    /personas/{id}/states            读取已写状态层
 *   - PUT    /personas/{id}/states/{layer}    状态层 upsert（M/S）
 */

import { request } from './client'
import type { PageResponse } from '@/types/common'
import type {
  CharaCard,
  PersonaBatchDeleteResponse,
  PersonaCardResponse,
  PersonaCopyRequest,
  PersonaCreateRequest,
  PersonaListParams,
  PersonaRecord,
  PersonaState,
  PersonaStateLayer,
  PersonaStateUpsertRequest,
  PersonaUpdateRequest,
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

/** DELETE /personas/{id} —— 删除人格（当前激活人格 409 拒绝）。 */
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

/** POST /personas/{id}/activate —— 激活人格（事务性单激活切换）。 */
export async function activatePersona(personaId: number): Promise<PersonaRecord> {
  return request<PersonaRecord>({
    method: 'POST',
    url: `/personas/${personaId}/activate`,
  })
}

/** POST /personas/{id}/deactivate —— 停用激活（允许零激活）。 */
export async function deactivatePersona(personaId: number): Promise<PersonaRecord> {
  return request<PersonaRecord>({
    method: 'POST',
    url: `/personas/${personaId}/deactivate`,
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

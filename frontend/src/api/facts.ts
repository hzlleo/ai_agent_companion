/**
 * facts 端点层：人物事实（结构化事实 / 证据链 / 更正循环）的 typed 封装。
 *
 * 路由面（全部经 ResultDTO 解包，失败抛 ApiError）：
 *   - GET    /facts                    事实列表（类别 / 状态过滤 + 游标）
 *   - POST   /facts                    手工录入事实（owner 来源）
 *   - PUT    /facts/{id}               主人更正（supersede 链 +1）
 *   - POST   /facts/{id}/reject        驳回（active → rejected，幂等）
 *   - DELETE /facts/{id}               硬删除（管理面显式动作）
 *
 * 人格作用域（本工程约定）：persona_id 为显式查询参数（undefined = 当前
 * 激活人格，由后端解析）；他人格事实不可见 / 不可更正（404 隔离语义）。
 */

import { request } from './client'
import type { FactCorrectRequest, FactCreateRequest, FactRecord } from '@/types/persona'
import type { PageResponse } from '@/types/common'

/** facts 列表查询参数（category / status 过滤 + 游标分页）。 */
export interface FactListParams {
  /** 类别过滤（undefined = 全部类别）。 */
  category?: FactRecord['category']
  /** 状态过滤（undefined = 后端缺省 active）。 */
  status?: FactRecord['status']
  /** 分页游标（undefined = 首页）。 */
  cursor?: string
  /** 页大小（1-200）。 */
  limit?: number
}

/** persona_id 查询参数形态（undefined 时不出现在查询串）。 */
function personaScope(personaId: number | undefined): { persona_id: number } | undefined {
  return personaId === undefined ? undefined : { persona_id: personaId }
}

/** GET /facts —— 人物事实列表（置信度降序 + 游标分页）。 */
export async function listFacts(
  personaId: number | undefined,
  params: FactListParams = {},
): Promise<PageResponse<FactRecord>> {
  return request<PageResponse<FactRecord>>({
    method: 'GET',
    url: '/facts',
    params: { ...personaScope(personaId), ...params },
  })
}

/** POST /facts —— 手工录入人物事实（owner 来源）。 */
export async function createFact(
  personaId: number | undefined,
  payload: FactCreateRequest,
): Promise<FactRecord> {
  return request<FactRecord>({
    method: 'POST',
    url: '/facts',
    params: personaScope(personaId),
    data: payload,
  })
}

/** PUT /facts/{id} —— 主人更正（supersede 链 +1）。 */
export async function correctFact(
  personaId: number | undefined,
  factId: number,
  payload: FactCorrectRequest,
): Promise<FactRecord> {
  return request<FactRecord>({
    method: 'PUT',
    url: `/facts/${factId}`,
    params: personaScope(personaId),
    data: payload,
  })
}

/** POST /facts/{id}/reject —— 驳回事实（幂等）。 */
export async function rejectFact(
  personaId: number | undefined,
  factId: number,
): Promise<FactRecord> {
  return request<FactRecord>({
    method: 'POST',
    url: `/facts/${factId}/reject`,
    params: personaScope(personaId),
  })
}

/** DELETE /facts/{id} —— 硬删除事实。 */
export async function deleteFact(
  personaId: number | undefined,
  factId: number,
): Promise<void> {
  await request<null>({
    method: 'DELETE',
    url: `/facts/${factId}`,
    params: personaScope(personaId),
  })
}

/**
 * skills 端点层：程序性技能（触发短语 / 原地版本 / 启用状态）的 typed 封装。
 *
 * 路由面（全部经 ResultDTO 解包，失败抛 ApiError）：
 *   - GET    /skills                     技能列表（状态过滤 + 游标）
 *   - POST   /skills                     手工创建（slug 由后端派生；同名 409）
 *   - PUT    /skills/{id}                原地编辑（版本递增，无历史链）
 *   - POST   /skills/{id}/enable         启用（参与 compose 匹配）
 *   - POST   /skills/{id}/disable        停用（不参与匹配）
 *   - DELETE /skills/{id}                硬删除（管理面显式动作）
 *
 * 人格作用域（本工程约定）：persona_id 为显式查询参数（undefined = 当前
 * 激活人格，由后端解析）；他人格技能不可见 / 不可编辑（404 隔离语义）。
 */

import { request } from './client'
import type { PageResponse } from '@/types/common'
import type {
  SkillCreateRequest,
  SkillRecord,
  SkillUpdateRequest,
} from '@/types/persona'

/** skills 列表查询参数（状态过滤 + 游标分页）。 */
export interface SkillListParams {
  /** 状态过滤（undefined = 全部状态）。 */
  status?: SkillRecord['status']
  /** 分页游标（undefined = 首页）。 */
  cursor?: string
  /** 页大小（1-200）。 */
  limit?: number
}

/** persona_id 查询参数形态（undefined 时不出现在查询串）。 */
function personaScope(personaId: number | undefined): { persona_id: number } | undefined {
  return personaId === undefined ? undefined : { persona_id: personaId }
}

/** GET /skills —— 技能列表（使用次数降序 + 游标分页）。 */
export async function listSkills(
  personaId: number | undefined,
  params: SkillListParams = {},
): Promise<PageResponse<SkillRecord>> {
  return request<PageResponse<SkillRecord>>({
    method: 'GET',
    url: '/skills',
    params: { ...personaScope(personaId), ...params },
  })
}

/** POST /skills —— 手工创建技能（slug 由后端按名称派生）。 */
export async function createSkill(
  personaId: number | undefined,
  payload: SkillCreateRequest,
): Promise<SkillRecord> {
  return request<SkillRecord>({
    method: 'POST',
    url: '/skills',
    params: personaScope(personaId),
    data: payload,
  })
}

/** PUT /skills/{id} —— 原地编辑（版本递增）。 */
export async function updateSkill(
  personaId: number | undefined,
  skillId: number,
  payload: SkillUpdateRequest,
): Promise<SkillRecord> {
  return request<SkillRecord>({
    method: 'PUT',
    url: `/skills/${skillId}`,
    params: personaScope(personaId),
    data: payload,
  })
}

/** POST /skills/{id}/enable —— 启用技能（幂等）。 */
export async function enableSkill(
  personaId: number | undefined,
  skillId: number,
): Promise<SkillRecord> {
  return request<SkillRecord>({
    method: 'POST',
    url: `/skills/${skillId}/enable`,
    params: personaScope(personaId),
  })
}

/** POST /skills/{id}/disable —— 停用技能（幂等）。 */
export async function disableSkill(
  personaId: number | undefined,
  skillId: number,
): Promise<SkillRecord> {
  return request<SkillRecord>({
    method: 'POST',
    url: `/skills/${skillId}/disable`,
    params: personaScope(personaId),
  })
}

/** DELETE /skills/{id} —— 硬删除技能。 */
export async function deleteSkill(
  personaId: number | undefined,
  skillId: number,
): Promise<void> {
  await request<null>({
    method: 'DELETE',
    url: `/skills/${skillId}`,
    params: personaScope(personaId),
  })
}

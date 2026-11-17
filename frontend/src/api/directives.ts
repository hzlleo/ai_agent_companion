/**
 * directives 端点层：长期指令（USER.md 式常备指令）的 typed 封装。
 *
 * 路由面（全部经 ResultDTO 解包，失败抛 ApiError）：
 *   - GET    /directives                 现行指令列表（active——新写入在前）
 *   - POST   /directives                 创建（同 topic 覆盖为 supersede）
 *   - PUT    /directives/{id}            按既有 topic 重写（supersede 链 +1）
 *   - DELETE /directives/{id}            删除（管理面显式动作）
 *
 * 人格作用域（本工程约定）：persona_id 为显式查询参数（undefined = 当前
 * 激活人格，由后端解析）；他人格指令不可见 / 不可删（404 隔离语义）。
 */

import { request } from './client'
import type {
  DirectiveCreateRequest,
  DirectiveRecord,
  DirectiveUpdateRequest,
} from '@/types/persona'

/** persona_id 查询参数形态（undefined 时不出现在查询串）。 */
function personaScope(personaId: number | undefined): { persona_id: number } | undefined {
  return personaId === undefined ? undefined : { persona_id: personaId }
}

/** GET /directives —— 现行长期指令列表（id 降序）。 */
export async function listDirectives(personaId: number | undefined): Promise<DirectiveRecord[]> {
  return request<DirectiveRecord[]>({
    method: 'GET',
    url: '/directives',
    params: personaScope(personaId),
  })
}

/** POST /directives —— 创建长期指令（同 topic 覆盖为 supersede）。 */
export async function createDirective(
  personaId: number | undefined,
  payload: DirectiveCreateRequest,
): Promise<DirectiveRecord> {
  return request<DirectiveRecord>({
    method: 'POST',
    url: '/directives',
    params: personaScope(personaId),
    data: payload,
  })
}

/** PUT /directives/{id} —— 按既有 topic 重写（supersede 链 +1）。 */
export async function updateDirective(
  personaId: number | undefined,
  directiveId: number,
  payload: DirectiveUpdateRequest,
): Promise<DirectiveRecord> {
  return request<DirectiveRecord>({
    method: 'PUT',
    url: `/directives/${directiveId}`,
    params: personaScope(personaId),
    data: payload,
  })
}

/** DELETE /directives/{id} —— 删除长期指令（策展行删除）。 */
export async function deleteDirective(
  personaId: number | undefined,
  directiveId: number,
): Promise<void> {
  await request<null>({
    method: 'DELETE',
    url: `/directives/${directiveId}`,
    params: personaScope(personaId),
  })
}

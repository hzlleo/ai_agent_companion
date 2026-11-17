/**
 * reminders 端点层：持久化提醒的 typed 封装。
 *
 * 路由面（全部经 ResultDTO 解包，失败抛 ApiError）：
 *   - POST /reminders                    创建（到期时刻允许过去）
 *   - GET  /reminders                    游标分页（status 过滤）
 *   - GET  /reminders/{id}               读取
 *   - POST /reminders/{id}/cancel        取消（仅 pending）
 *   - POST /reminders/batch-cancel       批量取消（部分成功形态）
 *
 * 人格作用域（本工程约定）：admin 路由无人格上下文语义——persona_id
 * 为显式查询参数（undefined = 当前激活人格，由后端解析）。投递由后端
 * 调度器扫描作业执行（无通道时保持 pending——见 docs/backend/reminders.md）。
 */

import { request } from './client'
import type { PageResponse } from '@/types/common'
import type {
  ReminderBatchCancelResponse,
  ReminderCreateRequest,
  ReminderListParams,
  ReminderRecord,
} from '@/types/reminders'

/** persona_id 查询参数形态（undefined 时不出现在查询串）。 */
function personaScope(personaId: number | undefined): { persona_id: number } | undefined {
  return personaId === undefined ? undefined : { persona_id: personaId }
}

/** POST /reminders —— 创建提醒（pending；到期允许过去——创建即到期）。 */
export async function createReminder(
  personaId: number | undefined,
  payload: ReminderCreateRequest,
): Promise<ReminderRecord> {
  return request<ReminderRecord>({
    method: 'POST',
    url: '/reminders',
    params: personaScope(personaId),
    data: payload,
  })
}

/** GET /reminders —— 提醒游标分页（status 过滤；id 降序）。 */
export async function listReminders(
  personaId: number | undefined,
  params: ReminderListParams,
): Promise<PageResponse<ReminderRecord>> {
  return request<PageResponse<ReminderRecord>>({
    method: 'GET',
    url: '/reminders',
    params: { ...personaScope(personaId), ...params },
  })
}

/** GET /reminders/{id} —— 按主键读取（他人格 / 缺失均 404）。 */
export async function getReminder(
  personaId: number | undefined,
  reminderId: number,
): Promise<ReminderRecord> {
  return request<ReminderRecord>({
    method: 'GET',
    url: `/reminders/${reminderId}`,
    params: personaScope(personaId),
  })
}

/** POST /reminders/{id}/cancel —— 取消提醒（仅 pending）。 */
export async function cancelReminder(
  personaId: number | undefined,
  reminderId: number,
): Promise<ReminderRecord> {
  return request<ReminderRecord>({
    method: 'POST',
    url: `/reminders/${reminderId}/cancel`,
    params: personaScope(personaId),
  })
}

/** POST /reminders/batch-cancel —— 批量取消（逐 id 独立判定，部分成功）。 */
export async function batchCancelReminders(
  personaId: number | undefined,
  ids: number[],
): Promise<ReminderBatchCancelResponse> {
  return request<ReminderBatchCancelResponse>({
    method: 'POST',
    url: '/reminders/batch-cancel',
    params: personaScope(personaId),
    data: { ids },
  })
}

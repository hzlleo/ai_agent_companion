/**
 * reminders React Query hooks：5 个端点的数据面封装。
 *
 * 约定（本工程）：
 * - 列表经 useInfiniteQuery 消费游标分页（pageParam = next_cursor）；
 * - 创建 / 取消 / 批量取消为显式动作——经 useMutation；
 * - 全部写操作成功后失效 reminders 缓存族；
 * - mutation 的 onSuccess/onError 语义（toast 文案）归页面层，本层
 *   只保证缓存一致。
 */

import { useInfiniteQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import {
  batchCancelReminders,
  cancelReminder,
  createReminder,
  listReminders,
} from '@/api/reminders'
import { getCursorPageParam } from '@/api/pagination'
import type {
  ReminderBatchCancelResponse,
  ReminderCreateRequest,
  ReminderStatus,
} from '@/types/reminders'

/** reminders 缓存族前缀（全部失效入口）。 */
export const REMINDERS_QUERY_KEY = ['reminders'] as const

/** 失效 reminders 全族（列表 + 逐 id 面）。 */
function useInvalidateReminders() {
  const queryClient = useQueryClient()
  return () => {
    void queryClient.invalidateQueries({ queryKey: REMINDERS_QUERY_KEY })
  }
}

/** 提醒列表查询参数（页面消费形态）。 */
export interface UseRemindersListParams {
  personaId?: number
  status?: ReminderStatus
  limit?: number
}

/** 提醒游标分页列表（status 过滤 + load more）。 */
export function useRemindersList(params: UseRemindersListParams) {
  return useInfiniteQuery({
    queryKey: [...REMINDERS_QUERY_KEY, 'list', params],
    queryFn: ({ pageParam }) =>
      listReminders(params.personaId, {
        status: params.status,
        cursor: pageParam,
        limit: params.limit,
      }),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: getCursorPageParam,
  })
}

/** 创建提醒（成功即失效列表）。 */
export function useCreateReminder() {
  const invalidate = useInvalidateReminders()
  return useMutation({
    mutationFn: ({
      personaId,
      payload,
    }: {
      personaId: number | undefined
      payload: ReminderCreateRequest
    }) => createReminder(personaId, payload),
    onSuccess: invalidate,
  })
}

/** 取消单条提醒（仅 pending——非 pending 由后端 422 拒绝）。 */
export function useCancelReminder() {
  const invalidate = useInvalidateReminders()
  return useMutation({
    mutationFn: ({
      personaId,
      reminderId,
    }: {
      personaId: number | undefined
      reminderId: number
    }) => cancelReminder(personaId, reminderId),
    onSuccess: invalidate,
  })
}

/** 批量取消提醒（部分成功形态——逐 id 结果由响应携带）。 */
export function useBatchCancelReminders() {
  const invalidate = useInvalidateReminders()
  return useMutation<
    ReminderBatchCancelResponse,
    Error,
    { personaId: number | undefined; ids: number[] }
  >({
    mutationFn: ({ personaId, ids }) => batchCancelReminders(personaId, ids),
    onSuccess: invalidate,
  })
}

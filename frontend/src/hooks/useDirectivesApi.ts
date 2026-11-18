/**
 * directives React Query hooks：长期指令（USER.md 式常备指令）数据面。
 *
 * 约定（本工程）：
 * - 列表为现行指令全量（后端已收窄 active + id 降序——无游标分页）；
 * - 创建 / 更新（supersede）/ 删除为显式动作——经 useMutation；
 * - 全部写操作成功后失效 directives 缓存族；
 * - 归属人格由调用方显式传入（面板在人格编辑页——personaId 必填）。
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  createDirective,
  deleteDirective,
  listDirectives,
  updateDirective,
} from '@/api/directives'
import type {
  DirectiveCreateRequest,
  DirectiveUpdateRequest,
} from '@/types/persona'

/** directives 缓存族前缀（全部失效入口）。 */
export const DIRECTIVES_QUERY_KEY = ['directives'] as const

/** 失效 directives 全族。 */
function useInvalidateDirectives() {
  const queryClient = useQueryClient()
  return () => {
    void queryClient.invalidateQueries({ queryKey: DIRECTIVES_QUERY_KEY })
  }
}

/** 现行长期指令列表（personaId 必填——面板在人格作用域内）。 */
export function useDirectivesList(personaId: number) {
  return useQuery({
    queryKey: [...DIRECTIVES_QUERY_KEY, personaId],
    queryFn: () => listDirectives(personaId),
    enabled: personaId > 0,
  })
}

/** 创建长期指令（同 topic 覆盖为 supersede）。 */
export function useCreateDirective() {
  const invalidate = useInvalidateDirectives()
  return useMutation({
    mutationFn: ({
      personaId,
      payload,
    }: {
      personaId: number
      payload: DirectiveCreateRequest
    }) => createDirective(personaId, payload),
    onSuccess: invalidate,
  })
}

/** 按既有 topic 重写长期指令（supersede 链 +1）。 */
export function useUpdateDirective() {
  const invalidate = useInvalidateDirectives()
  return useMutation({
    mutationFn: ({
      personaId,
      directiveId,
      payload,
    }: {
      personaId: number
      directiveId: number
      payload: DirectiveUpdateRequest
    }) => updateDirective(personaId, directiveId, payload),
    onSuccess: invalidate,
  })
}

/** 删除长期指令（策展行删除——管理面显式动作）。 */
export function useDeleteDirective() {
  const invalidate = useInvalidateDirectives()
  return useMutation({
    mutationFn: ({
      personaId,
      directiveId,
    }: {
      personaId: number
      directiveId: number
    }) => deleteDirective(personaId, directiveId),
    onSuccess: invalidate,
  })
}

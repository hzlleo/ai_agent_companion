/**
 * facts React Query hooks：人物事实（结构化事实 / 证据链 / 更正循环）数据面。
 *
 * 约定（本工程）：
 * - 列表为现行事实（后端缺省 active——面板只看现行；终态过滤显式传参）；
 * - 创建 / 更正（supersede）/ 驳回 / 删除为显式动作——经 useMutation；
 * - 全部写操作成功后失效 facts 缓存族；
 * - 归属人格由调用方显式传入（面板在人格编辑页——personaId 必填）。
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  correctFact,
  createFact,
  deleteFact,
  listFacts,
  rejectFact,
} from '@/api/facts'
import type { FactCorrectRequest, FactCreateRequest } from '@/types/persona'

/** facts 缓存族前缀（全部失效入口）。 */
export const FACTS_QUERY_KEY = ['facts'] as const

/** 面板列表页大小（事实量级小——单页拉全现行集）。 */
export const FACTS_PANEL_LIMIT = 200

/** 失效 facts 全族。 */
function useInvalidateFacts() {
  const queryClient = useQueryClient()
  return () => {
    void queryClient.invalidateQueries({ queryKey: FACTS_QUERY_KEY })
  }
}

/** 现行人物事实列表（personaId 必填——面板在人格作用域内）。 */
export function useFactsList(personaId: number) {
  return useQuery({
    queryKey: [...FACTS_QUERY_KEY, personaId],
    queryFn: () => listFacts(personaId, { limit: FACTS_PANEL_LIMIT }),
    enabled: personaId > 0,
  })
}

/** 手工录入人物事实。 */
export function useCreateFact() {
  const invalidate = useInvalidateFacts()
  return useMutation({
    mutationFn: ({ personaId, payload }: { personaId: number; payload: FactCreateRequest }) =>
      createFact(personaId, payload),
    onSuccess: invalidate,
  })
}

/** 主人更正事实（supersede 链 +1）。 */
export function useCorrectFact() {
  const invalidate = useInvalidateFacts()
  return useMutation({
    mutationFn: ({
      personaId,
      factId,
      payload,
    }: {
      personaId: number
      factId: number
      payload: FactCorrectRequest
    }) => correctFact(personaId, factId, payload),
    onSuccess: invalidate,
  })
}

/** 驳回事实（幂等）。 */
export function useRejectFact() {
  const invalidate = useInvalidateFacts()
  return useMutation({
    mutationFn: ({ personaId, factId }: { personaId: number; factId: number }) =>
      rejectFact(personaId, factId),
    onSuccess: invalidate,
  })
}

/** 硬删除事实。 */
export function useDeleteFact() {
  const invalidate = useInvalidateFacts()
  return useMutation({
    mutationFn: ({ personaId, factId }: { personaId: number; factId: number }) =>
      deleteFact(personaId, factId),
    onSuccess: invalidate,
  })
}

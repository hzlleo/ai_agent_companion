/**
 * core-memory React Query hooks：自编辑核心记忆（四块 / 修订历史）数据面。
 *
 * 约定（本工程）：
 * - 四块为固定集合（后端懒建），面板单次拉全量；
 * - 主人编辑为 useMutation——成功后失效四块与修订两个缓存族；
 * - 修订历史按块懒加载（抽屉打开时才启用查询）；
 * - 归属人格由调用方显式传入（面板在人格编辑页——personaId 必填）。
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  listCoreMemoryBlocks,
  listCoreMemoryRevisions,
  updateCoreMemoryBlock,
} from '@/api/coreMemory'
import type { CoreBlockKey, CoreMemoryOwnerUpdateRequest } from '@/types/persona'

/** core-memory 缓存族前缀（全部失效入口）。 */
export const CORE_MEMORY_QUERY_KEY = ['core-memory'] as const

/** 修订抽屉的缺省拉取条数（与后端 API 缺省一致）。 */
export const CORE_MEMORY_REVISIONS_LIMIT = 20

/** 失效 core-memory 全族（四块 + 全部修订）。 */
function useInvalidateCoreMemory() {
  const queryClient = useQueryClient()
  return () => {
    void queryClient.invalidateQueries({ queryKey: CORE_MEMORY_QUERY_KEY })
  }
}

/** 四块读取（personaId 必填——面板在人格作用域内）。 */
export function useCoreMemoryBlocks(personaId: number) {
  return useQuery({
    queryKey: [...CORE_MEMORY_QUERY_KEY, personaId, 'blocks'],
    queryFn: () => listCoreMemoryBlocks(personaId),
    enabled: personaId > 0,
  })
}

/** 主人编辑块（可清空 / 可锁定；成功后失效缓存族）。 */
export function useUpdateCoreMemoryBlock() {
  const invalidate = useInvalidateCoreMemory()
  return useMutation({
    mutationFn: ({
      personaId,
      block,
      payload,
    }: {
      personaId: number
      block: CoreBlockKey
      payload: CoreMemoryOwnerUpdateRequest
    }) => updateCoreMemoryBlock(personaId, block, payload),
    onSuccess: invalidate,
  })
}

/** 修订历史（enabled 关时零请求——抽屉关闭不拉历史）。 */
export function useCoreMemoryRevisions(
  personaId: number,
  block: CoreBlockKey | null,
  enabled: boolean,
) {
  return useQuery({
    queryKey: [...CORE_MEMORY_QUERY_KEY, personaId, 'revisions', block],
    queryFn: () =>
      listCoreMemoryRevisions(personaId, block as CoreBlockKey, CORE_MEMORY_REVISIONS_LIMIT),
    enabled: enabled && block !== null && personaId > 0,
  })
}

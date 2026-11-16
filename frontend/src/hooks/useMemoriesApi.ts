/**
 * memories React Query hooks：16 个端点的数据面封装。
 *
 * 约定（本工程）：
 * - 列表经 useInfiniteQuery 消费游标分页（pageParam = next_cursor）；
 * - 检索 / 梦境触发为显式提交动作——经 useMutation（每次提交重发）；
 * - 全部写操作成功后失效 memories 缓存族（列表 + 详情 + 历史链）；
 * - mutation 的 onSuccess/onError 语义（toast 文案）归页面层，本层
 *   只保证缓存一致。
 */

import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  batchDeleteEpisodes,
  createEpisode,
  deleteCurated,
  deleteEpisode,
  deleteSession,
  getCuratedHistory,
  getDreamRun,
  getEpisode,
  getSession,
  listCurated,
  listDreamRuns,
  listEpisodes,
  listSessions,
  searchMemories,
  triggerDream,
  upsertCurated,
} from '@/api/memories'
import { getCursorPageParam } from '@/api/pagination'
import type {
  CuratedKind,
  CuratedRecord,
  CuratedStatus,
  CuratedUpsertRequest,
  DreamTriggerRequest,
  EpisodeBatchDeleteResponse,
  EpisodeCreateRequest,
  EpisodeKind,
  MemoryProvenance,
  MemorySearchRequest,
  MemorySearchResult,
} from '@/types/memory'

/** memories 缓存族前缀（全部失效入口）。 */
export const MEMORIES_QUERY_KEY = ['memories'] as const

/** 失效 memories 全族（列表 + 逐 id 面 + 历史链）。 */
function useInvalidateMemories() {
  const queryClient = useQueryClient()
  return () => {
    void queryClient.invalidateQueries({ queryKey: MEMORIES_QUERY_KEY })
  }
}

/** 目标人格解析（picker 值 → persona_id 参数；'' = 当前激活人格）。 */
export function personaIdFromPicker(value: string): number | undefined {
  const parsed = Number(value)
  return value === '' || !Number.isInteger(parsed) ? undefined : parsed
}

/** 情景事件列表查询参数（页面消费形态）。 */
export interface UseEpisodesListParams {
  personaId?: number
  kind?: EpisodeKind
  provenance?: MemoryProvenance
  startMs?: number
  endMs?: number
  limit?: number
}

/** 情景事件游标分页列表（kind / provenance / 时间范围过滤 + load more）。 */
export function useEpisodesList(params: UseEpisodesListParams) {
  return useInfiniteQuery({
    queryKey: [...MEMORIES_QUERY_KEY, 'episodes', params],
    queryFn: ({ pageParam }) =>
      listEpisodes(params.personaId, {
        kind: params.kind,
        provenance: params.provenance,
        start_ms: params.startMs,
        end_ms: params.endMs,
        cursor: pageParam,
        limit: params.limit,
      }),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: getCursorPageParam,
  })
}

/** 单条情景事件（梦境日记回源等详情面）。 */
export function useEpisodeDetail(personaId: number | undefined, episodeId: number) {
  return useQuery({
    queryKey: [...MEMORIES_QUERY_KEY, 'episode', personaId, episodeId],
    queryFn: () => getEpisode(personaId, episodeId),
    enabled: episodeId > 0,
  })
}

/** 手工注入情景事件。 */
export function useCreateEpisode() {
  const invalidate = useInvalidateMemories()
  return useMutation({
    mutationFn: ({ personaId, payload }: { personaId: number | undefined; payload: EpisodeCreateRequest }) =>
      createEpisode(personaId, payload),
    onSuccess: invalidate,
  })
}

/** 删除单条情景事件（404 等错误由页面 toast 呈现）。 */
export function useDeleteEpisode() {
  const invalidate = useInvalidateMemories()
  return useMutation({
    mutationFn: ({ personaId, episodeId }: { personaId: number | undefined; episodeId: number }) =>
      deleteEpisode(personaId, episodeId),
    onSuccess: invalidate,
  })
}

/** 批量删除情景事件（部分成功形态——逐 id 结果由页面呈现）。 */
export function useBatchDeleteEpisodes() {
  const invalidate = useInvalidateMemories()
  return useMutation({
    mutationFn: ({
      personaId,
      ids,
    }: {
      personaId: number | undefined
      ids: number[]
    }): Promise<EpisodeBatchDeleteResponse> => batchDeleteEpisodes(personaId, ids),
    onSuccess: invalidate,
  })
}

/** 策展记忆列表查询参数（页面消费形态）。 */
export interface UseCuratedListParams {
  personaId?: number
  kind?: CuratedKind
  topic?: string
  status?: CuratedStatus
  limit?: number
}

/** 策展记忆游标分页列表（kind / topic / status 过滤 + load more）。 */
export function useCuratedList(params: UseCuratedListParams) {
  return useInfiniteQuery({
    queryKey: [...MEMORIES_QUERY_KEY, 'curated', params],
    queryFn: ({ pageParam }) =>
      listCurated(params.personaId, {
        kind: params.kind,
        topic: params.topic,
        status: params.status,
        cursor: pageParam,
        limit: params.limit,
      }),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: getCursorPageParam,
  })
}

/** 策展记忆 upsert（supersede 语义——新 active 行由响应返回）。 */
export function useUpsertCurated() {
  const invalidate = useInvalidateMemories()
  return useMutation({
    mutationFn: ({
      personaId,
      payload,
    }: {
      personaId: number | undefined
      payload: CuratedUpsertRequest
    }): Promise<CuratedRecord> => upsertCurated(personaId, payload),
    onSuccess: invalidate,
  })
}

/** 策展记忆历史链（supersede 链全量——版本升序；锚点 id 驱动）。 */
export function useCuratedHistory(personaId: number | undefined, memoryId: number) {
  return useQuery({
    queryKey: [...MEMORIES_QUERY_KEY, 'curated-history', personaId, memoryId],
    queryFn: () => getCuratedHistory(personaId, memoryId),
    enabled: memoryId > 0,
  })
}

/** 删除单条策展记忆。 */
export function useDeleteCurated() {
  const invalidate = useInvalidateMemories()
  return useMutation({
    mutationFn: ({ personaId, memoryId }: { personaId: number | undefined; memoryId: number }) =>
      deleteCurated(personaId, memoryId),
    onSuccess: invalidate,
  })
}

/** 会话游标分页列表（id 降序 + load more）。 */
export function useSessionsList(personaId: number | undefined, limit?: number) {
  return useInfiniteQuery({
    queryKey: [...MEMORIES_QUERY_KEY, 'sessions', personaId, limit],
    queryFn: ({ pageParam }) => listSessions(personaId, { cursor: pageParam, limit }),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: getCursorPageParam,
  })
}

/** 会话详情（摘要 + 全量消息 seq 升序）。 */
export function useSessionDetail(personaId: number | undefined, sessionId: number) {
  return useQuery({
    queryKey: [...MEMORIES_QUERY_KEY, 'session', personaId, sessionId],
    queryFn: () => getSession(personaId, sessionId),
    enabled: sessionId > 0,
  })
}

/** 删除会话（消息同事务清理）。 */
export function useDeleteSession() {
  const invalidate = useInvalidateMemories()
  return useMutation({
    mutationFn: ({ personaId, sessionId }: { personaId: number | undefined; sessionId: number }) =>
      deleteSession(personaId, sessionId),
    onSuccess: invalidate,
  })
}

/** 记忆检索（显式提交——结果列表经 mutate 返回值呈现）。 */
export function useSearchMemories() {
  return useMutation({
    mutationFn: (payload: MemorySearchRequest): Promise<MemorySearchResult[]> =>
      searchMemories(payload),
  })
}

/** 手工触发梦境整理（同步运行——响应即运行记录；failed 为业务结果非请求错误）。 */
export function useTriggerDream() {
  const invalidate = useInvalidateMemories()
  return useMutation({
    mutationFn: (payload: DreamTriggerRequest) => triggerDream(payload),
    onSuccess: invalidate,
  })
}

/** 梦境运行日志游标分页（id 降序 + load more）。 */
export function useDreamRunsList(personaId: number | undefined, limit?: number) {
  return useInfiniteQuery({
    queryKey: [...MEMORIES_QUERY_KEY, 'dream-runs', personaId, limit],
    queryFn: ({ pageParam }) => listDreamRuns(personaId, { cursor: pageParam, limit }),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: getCursorPageParam,
  })
}

/** 梦境运行详情（含逐事件决策明细——对话框打开时拉取）。 */
export function useDreamRunDetail(personaId: number | undefined, runId: number) {
  return useQuery({
    queryKey: [...MEMORIES_QUERY_KEY, 'dream-run', personaId, runId],
    queryFn: () => getDreamRun(personaId, runId),
    enabled: runId > 0,
  })
}

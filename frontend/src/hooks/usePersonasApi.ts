/**
 * personas React Query hooks：17 个端点的数据面封装（多激活可寻址模型）。
 *
 * 约定（本工程）：
 * - 列表经 useInfiniteQuery 消费游标分页（pageParam = next_cursor）；
 * - 全部写操作成功后失效 personas 缓存族（列表 + 详情/卡片/状态）；
 * - mutation 的 onSuccess/onError 语义（toast 文案）归页面层，本层
 *   只保证缓存一致；
 * - 导入面的幂等 / 冲突未导入结果是**成功响应**（HTTP 200）——失效
 *   列表缓存同样成立（覆盖导入与自动改名都会改列表）。
 */

import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  batchDeletePersonas,
  copyPersona,
  createPersona,
  deletePersona,
  disablePersona,
  getPersona,
  getPersonaCard,
  getPersonaStates,
  getPrimaryPersona,
  getTraitLibrary,
  importPersona,
  importPersonaFile,
  importPersonasBatch,
  listPersonas,
  replacePersonaCard,
  setPrimaryPersona,
  updatePersona,
  upsertPersonaState,
} from '@/api/personas'
import { getCursorPageParam } from '@/api/pagination'
import type { ImportFileOptions } from '@/api/personas'
import type {
  CharaCard,
  PersonaBatchDeleteResponse,
  PersonaCopyRequest,
  PersonaCreateRequest,
  PersonaImportConflictStrategy,
  PersonaImportRequest,
  PersonaState,
  PersonaStateLayer,
  PersonaStateUpsertRequest,
  PersonaStatus,
  PersonaUpdateRequest,
} from '@/types/persona'

/** personas 缓存族前缀（全部失效入口）。 */
export const PERSONAS_QUERY_KEY = ['personas'] as const

/** 失效 personas 全族（列表 + 逐 id 面）。 */
function useInvalidatePersonas() {
  const queryClient = useQueryClient()
  return () => {
    void queryClient.invalidateQueries({ queryKey: PERSONAS_QUERY_KEY })
  }
}

/** 列表查询参数（页面消费形态）。 */
export interface UsePersonasListParams {
  status?: PersonaStatus
  limit?: number
}

/** 人格游标分页列表（status 过滤 + load more）。 */
export function usePersonasList(params: UsePersonasListParams) {
  return useInfiniteQuery({
    queryKey: [...PERSONAS_QUERY_KEY, 'list', params],
    queryFn: ({ pageParam }) =>
      listPersonas({ status: params.status, cursor: pageParam, limit: params.limit }),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: getCursorPageParam,
  })
}

/** 单个人格详情（含卡片）。 */
export function usePersonaDetail(personaId: number) {
  return useQuery({
    queryKey: [...PERSONAS_QUERY_KEY, 'detail', personaId],
    queryFn: () => getPersona(personaId),
    enabled: personaId > 0,
  })
}

/** 创建人格。 */
export function useCreatePersona() {
  const invalidate = useInvalidatePersonas()
  return useMutation({
    mutationFn: (payload: PersonaCreateRequest) => createPersona(payload),
    onSuccess: invalidate,
  })
}

/** 部分更新人格（基础字段 / 卡片补丁）。 */
export function useUpdatePersona() {
  const invalidate = useInvalidatePersonas()
  return useMutation({
    mutationFn: ({ id, payload }: { id: number; payload: PersonaUpdateRequest }) =>
      updatePersona(id, payload),
    onSuccess: invalidate,
  })
}

/** 删除人格（主人格守卫 409 由页面 toast 呈现）。 */
export function useDeletePersona() {
  const invalidate = useInvalidatePersonas()
  return useMutation({
    mutationFn: (id: number) => deletePersona(id),
    onSuccess: invalidate,
  })
}

/** 复制人格（新注册名 + 可选深拷贝 M/S 状态）。 */
export function useCopyPersona() {
  const invalidate = useInvalidatePersonas()
  return useMutation({
    mutationFn: ({ id, payload }: { id: number; payload: PersonaCopyRequest }) =>
      copyPersona(id, payload),
    onSuccess: invalidate,
  })
}

/** 设为主人格（事务性切换——设 B 为主必然解除 A）。 */
export function useSetPrimaryPersona() {
  const invalidate = useInvalidatePersonas()
  return useMutation({
    mutationFn: (id: number) => setPrimaryPersona(id),
    onSuccess: invalidate,
  })
}

/** 停用人格（生命周期操作——主人格 409 由页面 toast 呈现）。 */
export function useDisablePersona() {
  const invalidate = useInvalidatePersonas()
  return useMutation({
    mutationFn: (id: number) => disablePersona(id),
    onSuccess: invalidate,
  })
}

/** 当前主人格（无主人格 404——消费面按需捕获）。 */
export function usePrimaryPersona() {
  return useQuery({
    queryKey: [...PERSONAS_QUERY_KEY, 'primary'],
    queryFn: () => getPrimaryPersona(),
  })
}

/** 读取人格卡片（卡片端点读取面）。 */
export function usePersonaCard(personaId: number) {
  return useQuery({
    queryKey: [...PERSONAS_QUERY_KEY, 'card', personaId],
    queryFn: () => getPersonaCard(personaId),
    enabled: personaId > 0,
  })
}

/** 整体替换人格卡片。 */
export function useReplacePersonaCard() {
  const invalidate = useInvalidatePersonas()
  return useMutation({
    mutationFn: ({ id, card }: { id: number; card: CharaCard }) => replacePersonaCard(id, card),
    onSuccess: invalidate,
  })
}

/** 读取人格全部已写状态层（M/S）。 */
export function usePersonaStates(personaId: number) {
  return useQuery({
    queryKey: [...PERSONAS_QUERY_KEY, 'states', personaId],
    queryFn: () => getPersonaStates(personaId),
    enabled: personaId > 0,
  })
}

/** 状态层 upsert（M/S；state 整体替换）。 */
export function useUpsertPersonaState() {
  const invalidate = useInvalidatePersonas()
  return useMutation({
    mutationFn: ({
      id,
      layer,
      payload,
    }: {
      id: number
      layer: PersonaStateLayer
      payload: PersonaStateUpsertRequest
    }) => upsertPersonaState(id, layer, payload),
    onSuccess: invalidate,
  })
}

/** 批量删除（部分成功形态——逐 id 结果由页面呈现）。 */
export function useBatchDeletePersonas() {
  const invalidate = useInvalidatePersonas()
  return useMutation({
    mutationFn: (ids: number[]): Promise<PersonaBatchDeleteResponse> => batchDeletePersonas(ids),
    onSuccess: invalidate,
  })
}

/** 官方特质预设库（画廊载荷——预设 + 组件注册表）。 */
export function useTraitLibrary() {
  return useQuery({
    queryKey: [...PERSONAS_QUERY_KEY, 'trait-library'],
    queryFn: () => getTraitLibrary(),
  })
}

/** 单发导入（JSON 面：官方预设生成或用户卡片文档——幂等/冲突结果由页面呈现）。 */
export function useImportPersona() {
  const invalidate = useInvalidatePersonas()
  return useMutation({
    mutationFn: (payload: PersonaImportRequest) => importPersona(payload),
    onSuccess: invalidate,
  })
}

/** 单发文件导入（multipart 面：卡片 JSON 文件上传——冲突重试由页面携带策略）。 */
export function useImportPersonaFile() {
  const invalidate = useInvalidatePersonas()
  return useMutation({
    mutationFn: ({
      file,
      options,
    }: {
      file: File
      options?: ImportFileOptions
    }) => importPersonaFile(file, options),
    onSuccess: invalidate,
  })
}

/** 批量导入（multipart 多文件，部分成功形态——逐文件结果由页面呈现）。 */
export function useImportPersonasBatch() {
  const invalidate = useInvalidatePersonas()
  return useMutation({
    mutationFn: ({
      files,
      strategy,
    }: {
      files: File[]
      strategy: PersonaImportConflictStrategy
    }) => importPersonasBatch(files, strategy),
    onSuccess: invalidate,
  })
}

/** 状态层读取结果的便捷映射（M/S → 已写状态）。 */
export function findStateByLayer(states: PersonaState[] | undefined, layer: PersonaStateLayer): PersonaState | undefined {
  return states?.find((state) => state.layer === layer)
}

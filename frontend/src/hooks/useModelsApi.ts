/**
 * models React Query hooks：5 个端点的数据面封装。
 *
 * 约定（本工程）：
 * - 注册表状态 / 角色绑定为只读查询（全局管理面，无人格参数）；
 * - 全部写操作成功后失效 models 缓存族（状态 + 角色绑定）；
 * - 409（重名 / 仍绑定角色）等错误面由页面 toast / 提示条呈现。
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  createModelEntry,
  deleteModelEntry,
  getRegistryState,
  listRoleBindings,
  updateModelEntry,
} from '@/api/models'
import type {
  ModelEntryCreateRequest,
  ModelEntryUpdateRequest,
  RegistryState,
} from '@/types/model-registry'

/** models 缓存族前缀（全部失效入口）。 */
export const MODELS_QUERY_KEY = ['models'] as const

/** 失效 models 全族（注册表状态 + 角色绑定）。 */
function useInvalidateModels() {
  const queryClient = useQueryClient()
  return () => {
    void queryClient.invalidateQueries({ queryKey: MODELS_QUERY_KEY })
  }
}

/** 注册表状态（条目脱敏 + 解析状态 + 显式绑定）。 */
export function useRegistryState() {
  return useQuery({
    queryKey: [...MODELS_QUERY_KEY, 'state'],
    queryFn: () => getRegistryState(),
  })
}

/** 五角色绑定面（bound / unbound 逐一呈现）。 */
export function useRoleBindings() {
  return useQuery({
    queryKey: [...MODELS_QUERY_KEY, 'roles'],
    queryFn: () => listRoleBindings(),
  })
}

/** 创建模型条目（重名 409；写后返回注册表状态）。 */
export function useCreateModelEntry() {
  const invalidate = useInvalidateModels()
  return useMutation({
    mutationFn: (payload: ModelEntryCreateRequest): Promise<RegistryState> =>
      createModelEntry(payload),
    onSuccess: invalidate,
  })
}

/** 部分更新模型条目（name 不可变；仅出现字段生效）。 */
export function useUpdateModelEntry() {
  const invalidate = useInvalidateModels()
  return useMutation({
    mutationFn: ({ name, payload }: { name: string; payload: ModelEntryUpdateRequest }) =>
      updateModelEntry(name, payload),
    onSuccess: invalidate,
  })
}

/** 删除模型条目（角色绑定守卫 409——先解绑再删除）。 */
export function useDeleteModelEntry() {
  const invalidate = useInvalidateModels()
  return useMutation({
    mutationFn: (name: string): Promise<RegistryState> => deleteModelEntry(name),
    onSuccess: invalidate,
  })
}

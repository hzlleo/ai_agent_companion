/**
 * skills React Query hooks：程序性技能（触发短语 / 启用状态）数据面。
 *
 * 约定（本工程）：
 * - 列表为全部状态（后端缺省不做状态过滤；面板可显式传 status 收窄）；
 * - 创建 / 编辑（原地版本递增）/ 启用 / 停用 / 删除为显式动作——经
 *   useMutation；
 * - 全部写操作成功后失效 skills 缓存族；
 * - 归属人格由调用方显式传入（面板在人格编辑页——personaId 必填）。
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  createSkill,
  deleteSkill,
  disableSkill,
  enableSkill,
  listSkills,
  updateSkill,
} from '@/api/skills'
import type { SkillCreateRequest, SkillUpdateRequest } from '@/types/persona'

/** skills 缓存族前缀（全部失效入口）。 */
export const SKILLS_QUERY_KEY = ['skills'] as const

/** 面板列表页大小（技能量级小——单页拉全量）。 */
export const SKILLS_PANEL_LIMIT = 200

/** 失效 skills 全族。 */
function useInvalidateSkills() {
  const queryClient = useQueryClient()
  return () => {
    void queryClient.invalidateQueries({ queryKey: SKILLS_QUERY_KEY })
  }
}

/** 技能列表（personaId 必填——面板在人格作用域内）。 */
export function useSkillsList(personaId: number) {
  return useQuery({
    queryKey: [...SKILLS_QUERY_KEY, personaId],
    queryFn: () => listSkills(personaId, { limit: SKILLS_PANEL_LIMIT }),
    enabled: personaId > 0,
  })
}

/** 手工创建技能（同名 409）。 */
export function useCreateSkill() {
  const invalidate = useInvalidateSkills()
  return useMutation({
    mutationFn: ({ personaId, payload }: { personaId: number; payload: SkillCreateRequest }) =>
      createSkill(personaId, payload),
    onSuccess: invalidate,
  })
}

/** 原地编辑技能（版本递增）。 */
export function useUpdateSkill() {
  const invalidate = useInvalidateSkills()
  return useMutation({
    mutationFn: ({
      personaId,
      skillId,
      payload,
    }: {
      personaId: number
      skillId: number
      payload: SkillUpdateRequest
    }) => updateSkill(personaId, skillId, payload),
    onSuccess: invalidate,
  })
}

/** 启用技能（幂等）。 */
export function useEnableSkill() {
  const invalidate = useInvalidateSkills()
  return useMutation({
    mutationFn: ({ personaId, skillId }: { personaId: number; skillId: number }) =>
      enableSkill(personaId, skillId),
    onSuccess: invalidate,
  })
}

/** 停用技能（幂等）。 */
export function useDisableSkill() {
  const invalidate = useInvalidateSkills()
  return useMutation({
    mutationFn: ({ personaId, skillId }: { personaId: number; skillId: number }) =>
      disableSkill(personaId, skillId),
    onSuccess: invalidate,
  })
}

/** 硬删除技能。 */
export function useDeleteSkill() {
  const invalidate = useInvalidateSkills()
  return useMutation({
    mutationFn: ({ personaId, skillId }: { personaId: number; skillId: number }) =>
      deleteSkill(personaId, skillId),
    onSuccess: invalidate,
  })
}

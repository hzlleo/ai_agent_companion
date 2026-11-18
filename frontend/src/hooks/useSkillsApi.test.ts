import { describe, expect, it } from 'vitest'
import { act, renderHook, waitFor } from '@testing-library/react'
import {
  SKILLS_QUERY_KEY,
  useCreateSkill,
  useDeleteSkill,
  useDisableSkill,
  useEnableSkill,
  useSkillsList,
  useUpdateSkill,
} from '@/hooks/useSkillsApi'
import { createQueryClientWrapperWithSpy, renderHookWithQueryClient } from '@/test/utils'

describe('useSkillsList', () => {
  it('personaId 生效时加载技能列表', async () => {
    const { result } = renderHookWithQueryClient(() => useSkillsList(1))
    await waitFor(() => expect(result.current.isSuccess).toBe(true))
    expect(result.current.data?.items.map((row) => row.name)).toEqual(['周报流程', '日报流程'])
    expect(result.current.data?.items.map((row) => row.status)).toEqual(['active', 'disabled'])
  })

  it('personaId=0 时不发起请求（enabled 关）', () => {
    const { result } = renderHookWithQueryClient(() => useSkillsList(0))
    expect(result.current.fetchStatus).toBe('idle')
    expect(result.current.data).toBeUndefined()
  })
})

describe('skills mutations', () => {
  it('useCreateSkill：成功后失效 skills 缓存族', async () => {
    const { wrapper, invalidateSpy } = createQueryClientWrapperWithSpy()
    const { result } = renderHook(() => useCreateSkill(), { wrapper })
    act(() => {
      result.current.mutate({
        personaId: 1,
        payload: {
          name: '日报流程',
          description: '主人说「写日报」时使用',
          triggers: ['写日报'],
          content: '1. 写今日完成',
        },
      })
    })
    await waitFor(() => expect(result.current.isSuccess).toBe(true))
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: SKILLS_QUERY_KEY })
    expect(result.current.data?.source).toBe('manual')
  })

  it('useUpdateSkill：原地编辑成功（version 递增）', async () => {
    const { wrapper } = createQueryClientWrapperWithSpy()
    const { result } = renderHook(() => useUpdateSkill(), { wrapper })
    act(() => {
      result.current.mutate({ personaId: 1, skillId: 1, payload: { content: '1. 新的步骤' } })
    })
    await waitFor(() => expect(result.current.isSuccess).toBe(true))
    expect(result.current.data?.version).toBe(2)
  })

  it('useEnableSkill / useDisableSkill：状态切换成功', async () => {
    const enableHook = renderHook(() => useEnableSkill(), {
      wrapper: createQueryClientWrapperWithSpy().wrapper,
    })
    act(() => {
      enableHook.result.current.mutate({ personaId: 1, skillId: 1 })
    })
    await waitFor(() => expect(enableHook.result.current.isSuccess).toBe(true))
    expect(enableHook.result.current.data?.status).toBe('active')

    const disableHook = renderHook(() => useDisableSkill(), {
      wrapper: createQueryClientWrapperWithSpy().wrapper,
    })
    act(() => {
      disableHook.result.current.mutate({ personaId: 1, skillId: 1 })
    })
    await waitFor(() => expect(disableHook.result.current.isSuccess).toBe(true))
    expect(disableHook.result.current.data?.status).toBe('disabled')
  })

  it('useDeleteSkill：删除成功并失效缓存族', async () => {
    const { wrapper, invalidateSpy } = createQueryClientWrapperWithSpy()
    const { result } = renderHook(() => useDeleteSkill(), { wrapper })
    act(() => {
      result.current.mutate({ personaId: 1, skillId: 1 })
    })
    await waitFor(() => expect(result.current.isSuccess).toBe(true))
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: SKILLS_QUERY_KEY })
  })
})

import { describe, expect, it } from 'vitest'
import { act, renderHook, waitFor } from '@testing-library/react'
import {
  FACTS_QUERY_KEY,
  useCorrectFact,
  useCreateFact,
  useDeleteFact,
  useFactsList,
  useRejectFact,
} from '@/hooks/useFactsApi'
import { createQueryClientWrapperWithSpy, renderHookWithQueryClient } from '@/test/utils'

describe('useFactsList', () => {
  it('personaId 生效时加载现行事实列表', async () => {
    const { result } = renderHookWithQueryClient(() => useFactsList(1))
    await waitFor(() => expect(result.current.isSuccess).toBe(true))
    expect(result.current.data?.items.map((row) => row.content)).toEqual([
      '主人喜欢无糖乌龙茶',
      '主人在杭州工作',
    ])
  })

  it('personaId=0 时不发起请求（enabled 关）', () => {
    const { result } = renderHookWithQueryClient(() => useFactsList(0))
    expect(result.current.fetchStatus).toBe('idle')
    expect(result.current.data).toBeUndefined()
  })
})

describe('facts mutations', () => {
  it('useCreateFact：成功后失效 facts 缓存族', async () => {
    const { wrapper, invalidateSpy } = createQueryClientWrapperWithSpy()
    const { result } = renderHook(() => useCreateFact(), { wrapper })
    act(() => {
      result.current.mutate({
        personaId: 1,
        payload: { category: 'preference', content: '主人喜欢无糖乌龙茶' },
      })
    })
    await waitFor(() => expect(result.current.isSuccess).toBe(true))
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: FACTS_QUERY_KEY })
  })

  it('useCorrectFact：supersede 成功（version 递增）', async () => {
    const { wrapper } = createQueryClientWrapperWithSpy()
    const { result } = renderHook(() => useCorrectFact(), { wrapper })
    act(() => {
      result.current.mutate({
        personaId: 1,
        factId: 1,
        payload: { content: '主人其实更喜欢茶' },
      })
    })
    await waitFor(() => expect(result.current.isSuccess).toBe(true))
    expect(result.current.data?.version).toBe(2)
  })

  it('useRejectFact：404 → onError 可达', async () => {
    const { wrapper } = createQueryClientWrapperWithSpy()
    const { result } = renderHook(() => useRejectFact(), { wrapper })
    act(() => {
      result.current.mutate({ personaId: 1, factId: 404 })
    })
    await waitFor(() => expect(result.current.isError).toBe(true))
  })

  it('useDeleteFact：404 → onError 可达', async () => {
    const { wrapper } = createQueryClientWrapperWithSpy()
    const { result } = renderHook(() => useDeleteFact(), { wrapper })
    act(() => {
      result.current.mutate({ personaId: 1, factId: 404 })
    })
    await waitFor(() => expect(result.current.isError).toBe(true))
  })
})

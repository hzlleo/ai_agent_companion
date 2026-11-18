import { describe, expect, it } from 'vitest'
import { act, renderHook, waitFor } from '@testing-library/react'
import {
  DIRECTIVES_QUERY_KEY,
  useCreateDirective,
  useDeleteDirective,
  useDirectivesList,
  useUpdateDirective,
} from '@/hooks/useDirectivesApi'
import { createQueryClientWrapperWithSpy, renderHookWithQueryClient } from '@/test/utils'

describe('useDirectivesList', () => {
  it('personaId 生效时加载现行指令列表', async () => {
    const { result } = renderHookWithQueryClient(() => useDirectivesList(1))
    await waitFor(() => expect(result.current.isSuccess).toBe(true))
    expect(result.current.data?.map((row) => row.content)).toEqual([
      '不要超过十二点睡',
      '以后都用简体中文',
    ])
  })

  it('personaId=0 时不发起请求（enabled 关）', () => {
    const { result } = renderHookWithQueryClient(() => useDirectivesList(0))
    expect(result.current.fetchStatus).toBe('idle')
    expect(result.current.data).toBeUndefined()
  })
})

describe('directives mutations', () => {
  it('useCreateDirective：成功后失效 directives 缓存族', async () => {
    const { wrapper, invalidateSpy } = createQueryClientWrapperWithSpy()
    const { result } = renderHook(() => useCreateDirective(), { wrapper })
    act(() => {
      result.current.mutate({ personaId: 1, payload: { content: '以后都用简体中文' } })
    })
    await waitFor(() => expect(result.current.isSuccess).toBe(true))
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: DIRECTIVES_QUERY_KEY })
  })

  it('useUpdateDirective：supersede 成功（version 递增）', async () => {
    const { wrapper } = createQueryClientWrapperWithSpy()
    const { result } = renderHook(() => useUpdateDirective(), { wrapper })
    act(() => {
      result.current.mutate({
        personaId: 1,
        directiveId: 1,
        payload: { content: '不要超过十二点睡' },
      })
    })
    await waitFor(() => expect(result.current.isSuccess).toBe(true))
    expect(result.current.data?.version).toBe(2)
  })

  it('useDeleteDirective：404 → onError 可达', async () => {
    const { wrapper } = createQueryClientWrapperWithSpy()
    const { result } = renderHook(() => useDeleteDirective(), { wrapper })
    act(() => {
      result.current.mutate({ personaId: 1, directiveId: 404 })
    })
    await waitFor(() => expect(result.current.isError).toBe(true))
  })
})

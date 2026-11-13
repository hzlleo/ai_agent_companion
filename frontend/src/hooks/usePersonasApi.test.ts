import { describe, expect, it } from 'vitest'
import { http, HttpResponse } from 'msw'
import { act, renderHook, waitFor } from '@testing-library/react'
import {
  findStateByLayer,
  useActivatePersona,
  useBatchDeletePersonas,
  useCreatePersona,
  useDeletePersona,
  usePersonasList,
  useUpsertPersonaState,
} from '@/hooks/usePersonasApi'
import { BACKEND_ORIGIN, errorResult, mockCard, mockPersona, okResult, server } from '@/test/server'
import { createQueryClientWrapperWithSpy, renderHookWithQueryClient } from '@/test/utils'

describe('usePersonasList', () => {
  it('首页加载 + fetchNextPage 追加（游标透传）', async () => {
    let secondPageCursor: string | null = null
    server.use(
      http.get(`${BACKEND_ORIGIN}/personas`, ({ request }) => {
        const url = new URL(request.url)
        const cursor = url.searchParams.get('cursor')
        if (cursor === null) {
          return HttpResponse.json(
            okResult({ items: [mockPersona({ id: 1 })], total: -1, page: 1, page_size: 20, next_cursor: 'c2', has_more: true }),
          )
        }
        secondPageCursor = cursor
        return HttpResponse.json(
          okResult({ items: [mockPersona({ id: 2 })], total: -1, page: 1, page_size: 20, next_cursor: null, has_more: false }),
        )
      }),
    )
    const { result } = renderHookWithQueryClient(() => usePersonasList({}))
    await waitFor(() => expect(result.current.isSuccess).toBe(true))
    expect(result.current.data?.pages).toHaveLength(1)
    expect(result.current.hasNextPage).toBe(true)

    await act(async () => {
      await result.current.fetchNextPage()
    })
    await waitFor(() => expect(result.current.data?.pages).toHaveLength(2))
    expect(secondPageCursor).toBe('c2')
    expect(result.current.data?.pages[1]?.items[0]?.id).toBe(2)
    expect(result.current.hasNextPage).toBe(false)
  })

  it('status 过滤进入查询参数', async () => {
    let capturedStatus: string | null = null
    server.use(
      http.get(`${BACKEND_ORIGIN}/personas`, ({ request }) => {
        capturedStatus = new URL(request.url).searchParams.get('status')
        return HttpResponse.json(
          okResult({ items: [], total: -1, page: 1, page_size: 20, next_cursor: null, has_more: false }),
        )
      }),
    )
    const { result } = renderHookWithQueryClient(() => usePersonasList({ status: 'disabled' }))
    await waitFor(() => expect(result.current.isSuccess).toBe(true))
    expect(capturedStatus).toBe('disabled')
  })
})

describe('personas mutations', () => {
  it('useCreatePersona：成功后失效 personas 缓存族', async () => {
    const { wrapper, invalidateSpy } = createQueryClientWrapperWithSpy()
    const { result } = renderHook(() => useCreatePersona(), { wrapper })
    act(() => {
      result.current.mutate({ name: 'new-one', display_name: null, status: 'disabled', card: { ...mockCard } })
      })
    await waitFor(() => expect(result.current.isSuccess).toBe(true))
    expect(result.current.data?.name).toBe('new-one')
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ['personas'] })
  })

  it('useActivatePersona：409 错误可达 onError', async () => {
    server.use(
      http.post(`${BACKEND_ORIGIN}/personas/:personaId/activate`, () =>
        HttpResponse.json(errorResult('停用状态的人格不可激活', 'CONFLICT'), { status: 409 }),
      ),
    )
    const { result } = renderHookWithQueryClient(() => useActivatePersona())
    act(() => {
      result.current.mutate(2)
      })
    await waitFor(() => expect(result.current.isError).toBe(true))
  })

  it('useDeletePersona：成功后失效缓存', async () => {
    const { wrapper, invalidateSpy } = createQueryClientWrapperWithSpy()
    const { result } = renderHook(() => useDeletePersona(), { wrapper })
    act(() => {
      result.current.mutate(7)
      })
    await waitFor(() => expect(result.current.isSuccess).toBe(true))
    expect(invalidateSpy).toHaveBeenCalled()
  })

  it('useBatchDeletePersonas：部分成功形态返回', async () => {
    const { result } = renderHookWithQueryClient(() => useBatchDeletePersonas())
    act(() => {
      result.current.mutate([1, 2])
      })
    await waitFor(() => expect(result.current.isSuccess).toBe(true))
    expect(result.current.data?.succeeded).toBe(1)
    expect(result.current.data?.failed).toBe(1)
  })

  it('useUpsertPersonaState：layer 路径 + 成功返回', async () => {
    const { result } = renderHookWithQueryClient(() => useUpsertPersonaState())
    act(() => {
      result.current.mutate({ id: 8, layer: 'S', payload: { state: { topic: 'work' } } })
      })
    await waitFor(() => expect(result.current.isSuccess).toBe(true))
    expect(result.current.data?.layer).toBe('S')
    expect(result.current.data?.state).toEqual({ topic: 'work' })
  })
})

describe('findStateByLayer', () => {
  it('按层取已写状态', () => {
    const states = [
      { persona_id: 1, layer: 'M' as const, state: { mood: 'calm' }, created_at: 1, updated_at: 1 },
      { persona_id: 1, layer: 'S' as const, state: { topic: 'x' }, created_at: 1, updated_at: 1 },
    ]
    expect(findStateByLayer(states, 'S')?.state).toEqual({ topic: 'x' })
    expect(findStateByLayer(states, 'M')?.state).toEqual({ mood: 'calm' })
  })

  it('未写层 → undefined', () => {
    expect(findStateByLayer(undefined, 'M')).toBeUndefined()
    expect(findStateByLayer([], 'S')).toBeUndefined()
  })
})

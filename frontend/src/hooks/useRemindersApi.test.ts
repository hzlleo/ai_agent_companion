import { describe, expect, it } from 'vitest'
import { http, HttpResponse } from 'msw'
import { act, renderHook, waitFor } from '@testing-library/react'
import {
  REMINDERS_QUERY_KEY,
  useBatchCancelReminders,
  useCancelReminder,
  useCreateReminder,
  useRemindersList,
} from '@/hooks/useRemindersApi'
import { BACKEND_ORIGIN, okResult, server } from '@/test/server'
import { createQueryClientWrapperWithSpy, renderHookWithQueryClient } from '@/test/utils'

const EMPTY_PAGE = { items: [], total: -1, page: 1, page_size: 20, next_cursor: null, has_more: false }

describe('useRemindersList', () => {
  it('游标透传 + fetchNextPage 追加', async () => {
    let secondPageCursor: string | null = null
    server.use(
      http.get(`${BACKEND_ORIGIN}/reminders`, ({ request }) => {
        const cursor = new URL(request.url).searchParams.get('cursor')
        if (cursor === null) {
          return HttpResponse.json(
            okResult({ ...EMPTY_PAGE, items: [{ id: 1 }], next_cursor: 'r2', has_more: true }),
          )
        }
        secondPageCursor = cursor
        return HttpResponse.json(okResult({ ...EMPTY_PAGE, items: [{ id: 2 }] }))
      }),
    )
    const { result } = renderHookWithQueryClient(() => useRemindersList({ personaId: 1 }))
    await waitFor(() => expect(result.current.isSuccess).toBe(true))
    expect(result.current.hasNextPage).toBe(true)
    await act(async () => {
      await result.current.fetchNextPage()
    })
    await waitFor(() => expect(result.current.data?.pages).toHaveLength(2))
    expect(secondPageCursor).toBe('r2')
  })

  it('status / persona_id 进入查询参数', async () => {
    const holder: { url: URL | null } = { url: null }
    server.use(
      http.get(`${BACKEND_ORIGIN}/reminders`, ({ request }) => {
        holder.url = new URL(request.url)
        return HttpResponse.json(okResult(EMPTY_PAGE))
      }),
    )
    const { result } = renderHookWithQueryClient(() =>
      useRemindersList({ personaId: 7, status: 'pending' }),
    )
    await waitFor(() => expect(result.current.isSuccess).toBe(true))
    expect(holder.url?.searchParams.get('persona_id')).toBe('7')
    expect(holder.url?.searchParams.get('status')).toBe('pending')
  })
})

describe('reminders mutations', () => {
  it('useCreateReminder：成功后失效 reminders 缓存族', async () => {
    const { wrapper, invalidateSpy } = createQueryClientWrapperWithSpy()
    const { result } = renderHook(() => useCreateReminder(), { wrapper })
    act(() => {
      result.current.mutate({
        personaId: 1,
        payload: { title: '喝水', due_at_ms: 1_700_000_000_000 },
      })
    })
    await waitFor(() => expect(result.current.isSuccess).toBe(true))
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: REMINDERS_QUERY_KEY })
  })

  it('useCancelReminder：状态守卫 422 → onError 可达', async () => {
    const { wrapper } = createQueryClientWrapperWithSpy()
    const { result } = renderHook(() => useCancelReminder(), { wrapper })
    act(() => {
      result.current.mutate({ personaId: 1, reminderId: 2 })
    })
    await waitFor(() => expect(result.current.isError).toBe(true))
  })

  it('useBatchCancelReminders：部分成功响应透传 + 失效缓存族', async () => {
    const { wrapper, invalidateSpy } = createQueryClientWrapperWithSpy()
    const { result } = renderHook(() => useBatchCancelReminders(), { wrapper })
    act(() => {
      result.current.mutate({ personaId: 1, ids: [3, 1] })
    })
    await waitFor(() => expect(result.current.isSuccess).toBe(true))
    expect(result.current.data?.failed).toBe(1)
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: REMINDERS_QUERY_KEY })
  })
})

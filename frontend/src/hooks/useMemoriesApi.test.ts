import { describe, expect, it } from 'vitest'
import { http, HttpResponse } from 'msw'
import { act, renderHook, waitFor } from '@testing-library/react'
import {
  MEMORIES_QUERY_KEY,
  personaIdFromPicker,
  useBatchDeleteEpisodes,
  useCreateEpisode,
  useCuratedHistory,
  useDeleteSession,
  useEpisodesList,
  useSearchMemories,
  useTriggerDream,
  useUpsertCurated,
} from '@/hooks/useMemoriesApi'
import { BACKEND_ORIGIN, errorResult, mockDreamRun, okResult, server } from '@/test/server'
import { createQueryClientWrapperWithSpy, renderHookWithQueryClient } from '@/test/utils'

const EMPTY_PAGE = { items: [], total: -1, page: 1, page_size: 20, next_cursor: null, has_more: false }

describe('personaIdFromPicker', () => {
  it('空串 → undefined；数字串 → number；非法 → undefined', () => {
    expect(personaIdFromPicker('')).toBeUndefined()
    expect(personaIdFromPicker('3')).toBe(3)
    expect(personaIdFromPicker('abc')).toBeUndefined()
  })
})

describe('useEpisodesList', () => {
  it('游标透传 + fetchNextPage 追加', async () => {
    let secondPageCursor: string | null = null
    server.use(
      http.get(`${BACKEND_ORIGIN}/memories/episodes`, ({ request }) => {
        const cursor = new URL(request.url).searchParams.get('cursor')
        if (cursor === null) {
          return HttpResponse.json(
            okResult({ ...EMPTY_PAGE, items: [{ id: 1 }], next_cursor: 'e2', has_more: true }),
          )
        }
        secondPageCursor = cursor
        return HttpResponse.json(okResult({ ...EMPTY_PAGE, items: [{ id: 2 }] }))
      }),
    )
    const { result } = renderHookWithQueryClient(() => useEpisodesList({ personaId: 1 }))
    await waitFor(() => expect(result.current.isSuccess).toBe(true))
    expect(result.current.hasNextPage).toBe(true)
    await act(async () => {
      await result.current.fetchNextPage()
    })
    await waitFor(() => expect(result.current.data?.pages).toHaveLength(2))
    expect(secondPageCursor).toBe('e2')
  })

  it('kind / provenance / 时间范围进入查询参数', async () => {
    const { url } = captureEpisodesUrl()
    const { result } = renderHookWithQueryClient(() =>
      useEpisodesList({ kind: 'chat', provenance: 'owner', startMs: 1, endMs: 2 }),
    )
    await waitFor(() => expect(result.current.isSuccess).toBe(true))
    expect(url()?.searchParams.get('kind')).toBe('chat')
    expect(url()?.searchParams.get('provenance')).toBe('owner')
    expect(url()?.searchParams.get('start_ms')).toBe('1')
    expect(url()?.searchParams.get('end_ms')).toBe('2')
  })
})

/** 注册 episodes URL 捕获 handler（闭包持有——规避直读窄化）。 */
function captureEpisodesUrl(): { url: () => URL | null } {
  let slot: URL | null = null
  server.use(
    http.get(`${BACKEND_ORIGIN}/memories/episodes`, ({ request }) => {
      slot = new URL(request.url)
      return HttpResponse.json(okResult(EMPTY_PAGE))
    }),
  )
  return { url: () => slot }
}

describe('memories mutations', () => {
  it('useCreateEpisode：成功后失效 memories 缓存族', async () => {
    const { wrapper, invalidateSpy } = createQueryClientWrapperWithSpy()
    const { result } = renderHook(() => useCreateEpisode(), { wrapper })
    act(() => {
      result.current.mutate({
        personaId: 1,
        payload: {
          kind: 'system',
          source: 'admin',
          content: { text: '注记', metadata: {} },
          importance: 5,
          provenance: 'owner',
          ts_ms: null,
        },
      })
    })
    await waitFor(() => expect(result.current.isSuccess).toBe(true))
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: MEMORIES_QUERY_KEY })
  })

  it('useUpsertCurated：untrusted 422 → onError 可达', async () => {
    server.use(
      http.post(`${BACKEND_ORIGIN}/memories/curated/upsert`, () =>
        HttpResponse.json(
          errorResult('策展层不接受 untrusted 来源（平台内容永不入策展层）', 'VALIDATION_ERROR'),
          { status: 422 },
        ),
      ),
    )
    const { result } = renderHookWithQueryClient(() => useUpsertCurated())
    act(() => {
      result.current.mutate({
        personaId: undefined,
        payload: { kind: 'memory', topic: 't', content: 'c', importance: 5, provenance: 'untrusted' },
      })
    })
    await waitFor(() => expect(result.current.isError).toBe(true))
  })

  it('useCuratedHistory：memoryId>0 时拉取（版本升序链）', async () => {
    const { result } = renderHookWithQueryClient(() => useCuratedHistory(1, 2))
    await waitFor(() => expect(result.current.isSuccess).toBe(true))
    expect(result.current.data).toHaveLength(2)
    expect(result.current.data?.[0]?.id).toBe(2)
    expect(result.current.data?.[1]?.status).toBe('active')
  })

  it('useBatchDeleteEpisodes：部分成功形态返回 + 缓存失效', async () => {
    const { wrapper, invalidateSpy } = createQueryClientWrapperWithSpy()
    const { result } = renderHook(() => useBatchDeleteEpisodes(), { wrapper })
    act(() => {
      result.current.mutate({ personaId: 1, ids: [1, 2] })
    })
    await waitFor(() => expect(result.current.isSuccess).toBe(true))
    expect(result.current.data?.succeeded).toBe(1)
    expect(result.current.data?.failed).toBe(1)
    expect(invalidateSpy).toHaveBeenCalled()
  })

  it('useDeleteSession：路径 + persona_id 透传', async () => {
    const { result } = renderHookWithQueryClient(() => useDeleteSession())
    act(() => {
      result.current.mutate({ personaId: 2, sessionId: 3 })
    })
    await waitFor(() => expect(result.current.isSuccess).toBe(true))
  })

  it('useSearchMemories：keyword 成功返回结果列表', async () => {
    const { result } = renderHookWithQueryClient(() => useSearchMemories())
    act(() => {
      result.current.mutate({ persona_id: 1, query: '汤面', mode: 'keyword', limit: 10 })
    })
    await waitFor(() => expect(result.current.isSuccess).toBe(true))
    expect(result.current.data).toHaveLength(2)
  })

  it('useTriggerDream：同步运行记录返回（failed 为业务结果非错误）', async () => {
    server.use(
      http.post(`${BACKEND_ORIGIN}/memories/dream/trigger`, () =>
        HttpResponse.json(
          okResult(
            mockDreamRun({ id: 5, status: 'failed', error: 'LLM 输出不可解析' }),
            '梦境整理失败：LLM 输出不可解析',
          ),
        ),
      ),
    )
    const { result } = renderHookWithQueryClient(() => useTriggerDream())
    act(() => {
      result.current.mutate({ persona_id: 1, cfg: { window_hours: 12 } })
    })
    await waitFor(() => expect(result.current.isSuccess).toBe(true))
    expect(result.current.data?.status).toBe('failed')
  })
})

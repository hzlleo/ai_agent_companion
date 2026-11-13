import { describe, expect, it } from 'vitest'
import { http, HttpResponse } from 'msw'
import { act, renderHook, waitFor } from '@testing-library/react'
import {
  USAGE_QUERY_KEY,
  useUsageAggregates,
  useUsageRecordsList,
  useUsageSummary,
} from '@/hooks/useUsageApi'
import { BACKEND_ORIGIN, errorResult, mockUsageSummary, okResult, server } from '@/test/server'
import { createQueryClientWrapperWithSpy, renderHookWithQueryClient } from '@/test/utils'

const EMPTY_PAGE = { items: [], total: -1, page: 1, page_size: 20, next_cursor: null, has_more: false }

/** 注册 URL 捕获 handler（闭包持有——规避直读窄化）。 */
function captureUrl(
  pattern: string,
  respond: () => ReturnType<typeof HttpResponse.json>,
): { url: () => URL | null } {
  let slot: URL | null = null
  server.use(
    http.get(pattern, ({ request }) => {
      slot = new URL(request.url)
      return respond()
    }),
  )
  return { url: () => slot }
}

describe('useUsageSummary', () => {
  it('过滤面进入查询串 + 总览解包', async () => {
    const { url } = captureUrl(`${BACKEND_ORIGIN}/usage/summary`, () =>
      HttpResponse.json(okResult(mockUsageSummary())),
    )
    const { result } = renderHookWithQueryClient(() =>
      useUsageSummary({ persona_id: '2', purpose: 'chat' }),
    )
    await waitFor(() => expect(result.current.isSuccess).toBe(true))
    expect(result.current.data?.call_count).toBe(3)
    expect(url()?.searchParams.get('persona_id')).toBe('2')
    expect(url()?.searchParams.get('purpose')).toBe('chat')
  })
})

describe('useUsageAggregates', () => {
  it('group_by 查询 + 行解包', async () => {
    const { url } = captureUrl(`${BACKEND_ORIGIN}/usage/aggregates`, () =>
      HttpResponse.json(
        okResult([
          {
            group: 'main',
            bucket_ms: null,
            input_tokens: 1,
            output_tokens: 1,
            total_tokens: 2,
            call_count: 1,
            avg_latency_ms: 5,
            error_count: 0,
          },
        ]),
      ),
    )
    const { result } = renderHookWithQueryClient(() =>
      useUsageAggregates({ group_by: 'model_entry' }),
    )
    await waitFor(() => expect(result.current.isSuccess).toBe(true))
    expect(result.current.data?.[0]?.group).toBe('main')
    expect(url()?.searchParams.get('group_by')).toBe('model_entry')
  })

  it('缺桶宽 422 → isError 可达', async () => {
    server.use(
      http.get(`${BACKEND_ORIGIN}/usage/aggregates`, () =>
        HttpResponse.json(
          errorResult('group_by=time_bucket 时必须声明 time_bucket 桶宽', 'VALIDATION_ERROR'),
          { status: 422 },
        ),
      ),
    )
    const { result } = renderHookWithQueryClient(() =>
      useUsageAggregates({ group_by: 'time_bucket' }),
    )
    await waitFor(() => expect(result.current.isError).toBe(true))
  })
})

describe('useUsageRecordsList', () => {
  it('游标透传 + fetchNextPage 追加 + 缓存族前缀', async () => {
    let secondPageCursor: string | null = null
    server.use(
      http.get(`${BACKEND_ORIGIN}/usage/records`, ({ request }) => {
        const cursor = new URL(request.url).searchParams.get('cursor')
        if (cursor === null) {
          return HttpResponse.json(
            okResult({ ...EMPTY_PAGE, items: [{ id: 1 }], next_cursor: 'u2', has_more: true }),
          )
        }
        secondPageCursor = cursor
        return HttpResponse.json(okResult({ ...EMPTY_PAGE, items: [{ id: 2 }] }))
      }),
    )
    const { wrapper } = createQueryClientWrapperWithSpy()
    const { result } = renderHook(() => useUsageRecordsList({ purpose: 'chat' }), { wrapper })
    await waitFor(() => expect(result.current.isSuccess).toBe(true))
    expect(result.current.hasNextPage).toBe(true)
    expect(USAGE_QUERY_KEY).toEqual(['usage'])
    await act(async () => {
      await result.current.fetchNextPage()
    })
    await waitFor(() => expect(result.current.data?.pages).toHaveLength(2), { timeout: 3000 })
    expect(secondPageCursor).toBe('u2')
  })
})

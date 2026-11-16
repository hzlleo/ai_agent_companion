import { describe, expect, it } from 'vitest'
import { http, HttpResponse } from 'msw'
import { aggregateUsage, listUsageRecords, usageSummary } from '@/api/usage'
import { ApiError } from '@/api/client'
import { BACKEND_ORIGIN, errorResult, okResult, server } from '@/test/server'

/** 捕获到的请求 URL。 */
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

const EMPTY_PAGE = { items: [], total: -1, page: 1, page_size: 20, next_cursor: null, has_more: false }

describe('usage 端点', () => {
  it('listUsageRecords：全过滤轴进查询串；缺省不发送', async () => {
    const { url } = captureUrl(`${BACKEND_ORIGIN}/usage/records`, () =>
      HttpResponse.json(okResult(EMPTY_PAGE)),
    )
    await listUsageRecords({
      persona_id: '3',
      model_entry: 'main',
      purpose: 'chat',
      status: 'error',
      start_ms: 100,
      end_ms: 200,
      cursor: 'c1',
      limit: 50,
    })
    const first = url()
    if (first === null) throw new Error('请求未被捕获')
    expect(first.searchParams.get('persona_id')).toBe('3')
    expect(first.searchParams.get('model_entry')).toBe('main')
    expect(first.searchParams.get('purpose')).toBe('chat')
    expect(first.searchParams.get('status')).toBe('error')
    expect(first.searchParams.get('start_ms')).toBe('100')
    expect(first.searchParams.get('end_ms')).toBe('200')
    expect(first.searchParams.get('cursor')).toBe('c1')
    expect(first.searchParams.get('limit')).toBe('50')

    await listUsageRecords({})
    const second = url()
    if (second === null) throw new Error('请求未被捕获')
    expect(second.searchParams.get('persona_id')).toBeNull()
    expect(second.searchParams.get('purpose')).toBeNull()
  })

  it('aggregateUsage：group_by / time_bucket 查询串 + 行解包', async () => {
    const { url } = captureUrl(`${BACKEND_ORIGIN}/usage/aggregates`, () =>
      HttpResponse.json(
        okResult([
          {
            group: 'main',
            bucket_ms: null,
            input_tokens: 100,
            output_tokens: 50,
            total_tokens: 150,
            call_count: 2,
            avg_latency_ms: 800,
            error_count: 0,
          },
        ]),
      ),
    )
    const rows = await aggregateUsage({ group_by: 'model_entry', purpose: 'chat' })
    expect(rows).toHaveLength(1)
    expect(rows[0]?.group).toBe('main')
    const capturedUrl = url()
    if (capturedUrl === null) throw new Error('请求未被捕获')
    expect(capturedUrl.searchParams.get('group_by')).toBe('model_entry')
    expect(capturedUrl.searchParams.get('purpose')).toBe('chat')
    expect(capturedUrl.searchParams.get('time_bucket')).toBeNull()
  })

  it('aggregateUsage：time_bucket 维度携带桶宽', async () => {
    const { url } = captureUrl(`${BACKEND_ORIGIN}/usage/aggregates`, () =>
      HttpResponse.json(okResult([])),
    )
    await aggregateUsage({ group_by: 'time_bucket', time_bucket: 'hour' })
    const capturedUrl = url()
    if (capturedUrl === null) throw new Error('请求未被捕获')
    expect(capturedUrl.searchParams.get('group_by')).toBe('time_bucket')
    expect(capturedUrl.searchParams.get('time_bucket')).toBe('hour')
  })

  it('aggregateUsage：缺桶宽 → 422 ApiError', async () => {
    server.use(
      http.get(`${BACKEND_ORIGIN}/usage/aggregates`, () =>
        HttpResponse.json(
          errorResult('group_by=time_bucket 时必须声明 time_bucket 桶宽', 'VALIDATION_ERROR'),
          { status: 422 },
        ),
      ),
    )
    const error = await aggregateUsage({ group_by: 'time_bucket' }).catch((e: unknown) => e)
    expect(error).toBeInstanceOf(ApiError)
    const apiError = error as ApiError
    expect(apiError.message).toBe('group_by=time_bucket 时必须声明 time_bucket 桶宽')
    expect(apiError.status).toBe(422)
  })

  it('usageSummary：总览解包（purposes 明细随行）', async () => {
    const { url } = captureUrl(`${BACKEND_ORIGIN}/usage/summary`, () =>
      HttpResponse.json(
        okResult({
          call_count: 3,
          input_tokens: 100,
          output_tokens: 50,
          total_tokens: 150,
          avg_latency_ms: 800,
          error_count: 1,
          purposes: [{ group: 'chat', bucket_ms: null, input_tokens: 100, output_tokens: 50, total_tokens: 150, call_count: 3, avg_latency_ms: 800, error_count: 1 }],
        }),
      ),
    )
    const summary = await usageSummary({ persona_id: '1' })
    expect(summary.call_count).toBe(3)
    expect(summary.purposes[0]?.group).toBe('chat')
    const capturedUrl = url()
    if (capturedUrl === null) throw new Error('请求未被捕获')
    expect(capturedUrl.searchParams.get('persona_id')).toBe('1')
  })
})

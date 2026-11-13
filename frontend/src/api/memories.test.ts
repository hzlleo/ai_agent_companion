import { describe, expect, it } from 'vitest'
import { http, HttpResponse } from 'msw'
import {
  batchDeleteEpisodes,
  createEpisode,
  deleteCurated,
  deleteEpisode,
  deleteSession,
  getCuratedHistory,
  getDreamRun,
  getEpisode,
  getSession,
  listCurated,
  listDreamRuns,
  listEpisodes,
  listSessions,
  searchMemories,
  triggerDream,
  upsertCurated,
} from '@/api/memories'
import { ApiError } from '@/api/client'
import { BACKEND_ORIGIN, errorResult, mockDreamRun, mockEpisode, okResult, server } from '@/test/server'

/** 捕获到的请求（method / url / body）。 */
interface CapturedRequest {
  method: string
  url: URL
  body: unknown
}

/** 注册捕获 handler 并返回捕获槽。 */
function capture(
  pattern: string,
  method: 'get' | 'post' | 'put' | 'delete',
  respond: () => ReturnType<typeof HttpResponse.json>,
): { captured: () => CapturedRequest | null } {
  let slot: CapturedRequest | null = null
  server.use(
    http[method](pattern, async ({ request }) => {
      const body =
        request.method === 'GET' || request.method === 'DELETE'
          ? null
          : await request.json().catch(() => null)
      slot = { method: request.method, url: new URL(request.url), body }
      return respond()
    }),
  )
  return { captured: () => slot }
}

/** 断言 rejects 为 ApiError 且 message 匹配。 */
async function expectApiError(promise: Promise<unknown>, message: string, code: string): Promise<ApiError> {
  const error = await promise.catch((e: unknown) => e)
  expect(error).toBeInstanceOf(ApiError)
  const apiError = error as ApiError
  expect(apiError.message).toBe(message)
  expect(apiError.code).toBe(code)
  return apiError
}

const EMPTY_PAGE = { items: [], total: -1, page: 1, page_size: 20, next_cursor: null, has_more: false }

describe('memories 端点（人格作用域查询参数）', () => {
  it('listEpisodes：persona_id + 过滤参数进查询串；undefined 时不发送', async () => {
    const { captured } = capture(`${BACKEND_ORIGIN}/memories/episodes`, 'get', () =>
      HttpResponse.json(okResult(EMPTY_PAGE)),
    )
    await listEpisodes(3, { kind: 'chat', provenance: 'owner', start_ms: 100, end_ms: 200, cursor: 'c', limit: 50 })
    const first = captured()
    if (first === null) throw new Error('请求未被捕获')
    expect(first.url.searchParams.get('persona_id')).toBe('3')
    expect(first.url.searchParams.get('kind')).toBe('chat')
    expect(first.url.searchParams.get('provenance')).toBe('owner')
    expect(first.url.searchParams.get('start_ms')).toBe('100')
    expect(first.url.searchParams.get('end_ms')).toBe('200')
    expect(first.url.searchParams.get('cursor')).toBe('c')
    expect(first.url.searchParams.get('limit')).toBe('50')

    await listEpisodes(undefined, {})
    const second = captured()
    if (second === null) throw new Error('请求未被捕获')
    expect(second.url.searchParams.get('persona_id')).toBeNull()
  })

  it('createEpisode：persona_id 查询串 + 载荷请求体', async () => {
    const { captured } = capture(`${BACKEND_ORIGIN}/memories/episodes`, 'post', () =>
      HttpResponse.json(okResult(mockEpisode({ id: 9 }), '情景事件已记录')),
    )
    const record = await createEpisode(5, {
      kind: 'system',
      source: 'admin',
      content: { text: '注记', metadata: {} },
      importance: 5,
      provenance: 'owner',
      ts_ms: null,
    })
    expect(record.id).toBe(9)
    const capturedRequest = captured()
    if (capturedRequest === null) throw new Error('请求未被捕获')
    expect(capturedRequest.url.searchParams.get('persona_id')).toBe('5')
    expect(capturedRequest.body).toEqual({
      kind: 'system',
      source: 'admin',
      content: { text: '注记', metadata: {} },
      importance: 5,
      provenance: 'owner',
      ts_ms: null,
    })
  })

  it('batchDeleteEpisodes：ids 请求体 + 部分成功形态解包', async () => {
    const { captured } = capture(`${BACKEND_ORIGIN}/memories/episodes/batch-delete`, 'post', () =>
      HttpResponse.json(
        okResult({
          total: 2,
          succeeded: 1,
          failed: 1,
          items: [
            { id: 1, success: false, code: 'NOT_FOUND', message: '事件不存在' },
            { id: 2, success: true, code: null, message: '已删除' },
          ],
        }),
      ),
    )
    const result = await batchDeleteEpisodes(undefined, [1, 2])
    expect(result.succeeded).toBe(1)
    expect(captured()?.body).toEqual({ ids: [1, 2] })
  })

  it('getEpisode / deleteEpisode：路径参数 + persona_id', async () => {
    const getCapture = capture(`${BACKEND_ORIGIN}/memories/episodes/:id`, 'get', () =>
      HttpResponse.json(okResult(mockEpisode({ id: 7 }))),
    )
    const episode = await getEpisode(2, 7)
    expect(episode.id).toBe(7)
    expect(getCapture.captured()?.url.pathname).toBe('/api/v1/memories/episodes/7')

    const deleteCapture = capture(`${BACKEND_ORIGIN}/memories/episodes/:id`, 'delete', () =>
      HttpResponse.json(okResult(null, '情景事件已删除')),
    )
    await deleteEpisode(2, 7)
    expect(deleteCapture.captured()?.url.searchParams.get('persona_id')).toBe('2')
  })

  it('listCurated：kind / topic / status 过滤进查询串', async () => {
    const { captured } = capture(`${BACKEND_ORIGIN}/memories/curated`, 'get', () =>
      HttpResponse.json(okResult(EMPTY_PAGE)),
    )
    await listCurated(1, { kind: 'memory', topic: 'food', status: 'active', limit: 10 })
    const capturedRequest = captured()
    if (capturedRequest === null) throw new Error('请求未被捕获')
    expect(capturedRequest.url.searchParams.get('kind')).toBe('memory')
    expect(capturedRequest.url.searchParams.get('topic')).toBe('food')
    expect(capturedRequest.url.searchParams.get('status')).toBe('active')
    expect(capturedRequest.url.searchParams.get('limit')).toBe('10')
  })

  it('upsertCurated：untrusted → 422 ApiError（message + code）', async () => {
    const { captured } = capture(`${BACKEND_ORIGIN}/memories/curated/upsert`, 'post', () =>
      HttpResponse.json(
        errorResult('策展层不接受 untrusted 来源（平台内容永不入策展层）', 'VALIDATION_ERROR'),
        { status: 422 },
      ),
    )
    const error = await expectApiError(
      upsertCurated(undefined, {
        kind: 'memory',
        topic: 't',
        content: 'c',
        importance: 5,
        provenance: 'untrusted',
      }),
      '策展层不接受 untrusted 来源（平台内容永不入策展层）',
      'VALIDATION_ERROR',
    )
    expect(error.status).toBe(422)
    expect(captured()?.body).toMatchObject({ provenance: 'untrusted' })
  })

  it('getCuratedHistory / deleteCurated：路径 + 解包', async () => {
    const historyCapture = capture(`${BACKEND_ORIGIN}/memories/curated/:id/history`, 'get', () =>
      HttpResponse.json(okResult([])),
    )
    const history = await getCuratedHistory(1, 3)
    expect(history).toEqual([])
    expect(historyCapture.captured()?.url.pathname).toBe('/api/v1/memories/curated/3/history')

    const deleteCapture = capture(`${BACKEND_ORIGIN}/memories/curated/:id`, 'delete', () =>
      HttpResponse.json(okResult(null, '策展记忆已删除')),
    )
    await deleteCurated(1, 3)
    expect(deleteCapture.captured()?.url.pathname).toBe('/api/v1/memories/curated/3')
  })

  it('listSessions / getSession / deleteSession', async () => {
    const listCapture = capture(`${BACKEND_ORIGIN}/memories/sessions`, 'get', () =>
      HttpResponse.json(okResult(EMPTY_PAGE)),
    )
    await listSessions(4, { cursor: 's2', limit: 10 })
    const listRequest = listCapture.captured()
    if (listRequest === null) throw new Error('请求未被捕获')
    expect(listRequest.url.searchParams.get('persona_id')).toBe('4')
    expect(listRequest.url.searchParams.get('cursor')).toBe('s2')

    const detailCapture = capture(`${BACKEND_ORIGIN}/memories/sessions/:id`, 'get', () =>
      HttpResponse.json(
        okResult({
          id: 6,
          persona_id: '4',
          title: 't',
          message_count: 0,
          created_at: 1,
          last_active_at: 1,
          messages: [],
        }),
      ),
    )
    const detail = await getSession(4, 6)
    expect(detail.id).toBe(6)
    expect(detailCapture.captured()?.url.pathname).toBe('/api/v1/memories/sessions/6')

    const deleteCapture = capture(`${BACKEND_ORIGIN}/memories/sessions/:id`, 'delete', () =>
      HttpResponse.json(okResult(null, '会话已删除')),
    )
    await deleteSession(undefined, 6)
    expect(deleteCapture.captured()?.url.pathname).toBe('/api/v1/memories/sessions/6')
  })

  it('searchMemories：persona_id 在请求体（非查询串）', async () => {
    const { captured } = capture(`${BACKEND_ORIGIN}/memories/search`, 'post', () =>
      HttpResponse.json(okResult([])),
    )
    await searchMemories({
      persona_id: 2,
      query: '热汤面',
      mode: 'keyword',
      limit: 10,
    })
    const capturedRequest = captured()
    if (capturedRequest === null) throw new Error('请求未被捕获')
    expect(capturedRequest.body).toMatchObject({ persona_id: 2, query: '热汤面', mode: 'keyword' })
    expect(capturedRequest.url.searchParams.get('persona_id')).toBeNull()
  })

  it('searchMemories：回退存储拒绝向量模式 → 422 + details 透传', async () => {
    const { captured } = capture(`${BACKEND_ORIGIN}/memories/search`, 'post', () =>
      HttpResponse.json(
        errorResult('回退存储不支持向量检索（仅 keyword 模式可用）', 'VALIDATION_ERROR', {
          mode: 'dense',
          fix: '启用 SeekDB（SEEKDB_ENABLED=true）或改用 mode=keyword',
        }),
        { status: 422 },
      ),
    )
    const error = await expectApiError(
      searchMemories({ query: '热汤面', mode: 'dense', limit: 10 }),
      '回退存储不支持向量检索（仅 keyword 模式可用）',
      'VALIDATION_ERROR',
    )
    expect(error.status).toBe(422)
    expect(error.details).toEqual({
      mode: 'dense',
      fix: '启用 SeekDB（SEEKDB_ENABLED=true）或改用 mode=keyword',
    })
    expect(captured()?.body).toMatchObject({ mode: 'dense' })
  })

  it('triggerDream：cfg 部分覆写请求体 + 运行记录解包', async () => {
    const { captured } = capture(`${BACKEND_ORIGIN}/memories/dream/trigger`, 'post', () =>
      HttpResponse.json(
        okResult(
          {
            id: 9,
            persona_id: '1',
            status: 'failed',
            started_ms: 1,
            finished_ms: 2,
            stats: { candidates: 0, gated: 0, promoted: 0, superseded: 0, skipped_untrusted: 0, failed: 0 },
            decisions: [],
            diary_episode_id: null,
            error: 'LLM 输出不可解析',
            created_at: 1,
            updated_at: 2,
          },
          '梦境整理失败：LLM 输出不可解析',
        ),
      ),
    )
    const run = await triggerDream({ persona_id: 1, cfg: { window_hours: 48, diary_enabled: false } })
    expect(run.status).toBe('failed')
    expect(run.error).toBe('LLM 输出不可解析')
    expect(captured()?.body).toEqual({
      persona_id: 1,
      cfg: { window_hours: 48, diary_enabled: false },
    })
  })

  it('listDreamRuns / getDreamRun：路径与查询串', async () => {
    const listCapture = capture(`${BACKEND_ORIGIN}/memories/dream/runs`, 'get', () =>
      HttpResponse.json(okResult(EMPTY_PAGE)),
    )
    await listDreamRuns(8, { limit: 5 })
    const listRequest = listCapture.captured()
    if (listRequest === null) throw new Error('请求未被捕获')
    expect(listRequest.url.searchParams.get('persona_id')).toBe('8')
    expect(listRequest.url.searchParams.get('limit')).toBe('5')

    const detailCapture = capture(`${BACKEND_ORIGIN}/memories/dream/runs/:id`, 'get', () =>
      HttpResponse.json(okResult(mockDreamRun({ id: 3 }))),
    )
    await getDreamRun(8, 3)
    expect(detailCapture.captured()?.url.pathname).toBe('/api/v1/memories/dream/runs/3')
  })

  it('404 错误形态：message + code + status 透传', async () => {
    server.use(
      http.get(`${BACKEND_ORIGIN}/memories/episodes/:id`, () =>
        HttpResponse.json(errorResult('事件不存在或不在此人格作用域', 'NOT_FOUND'), { status: 404 }),
      ),
    )
    const error = await expectApiError(
      getEpisode(undefined, 404),
      '事件不存在或不在此人格作用域',
      'NOT_FOUND',
    )
    expect(error.status).toBe(404)
  })
})

import { describe, expect, it } from 'vitest'
import { http, HttpResponse } from 'msw'
import { ApiError } from '@/api/client'
import {
  createCanvas,
  deleteCanvas,
  downloadCanvas,
  listCanvas,
  updateCanvas,
} from '@/api/canvas'
import { BACKEND_ORIGIN, errorResult, mockCanvas, okResult, server } from '@/test/server'
import { CANVAS_DEFAULT_QUERY } from '@/hooks/useCanvasApi'
import type { CanvasCreateRequest } from '@/types/canvas'

/** 捕获到的请求（method / url / body）。 */
interface CapturedRequest {
  method: string
  url: URL
  body: unknown
}

/** 注册捕获 handler 并返回捕获槽。 */
function capture(
  pattern: string,
  method: 'get' | 'post' | 'patch' | 'delete',
  respond: () => Response,
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

const createPayload: CanvasCreateRequest = {
  title: '今日心情卡',
  kind: 'html',
  content: '<div>今天很好</div>',
}

describe('canvas 端点', () => {
  it('listCanvas：persona_id / 类型过滤 / 条数上限进查询串', async () => {
    const { captured } = capture(`${BACKEND_ORIGIN}/canvas`, 'get', () =>
      HttpResponse.json(okResult([mockCanvas()])),
    )
    const records = await listCanvas(3, { ...CANVAS_DEFAULT_QUERY, kind: 'svg' })
    expect(records[0]?.title).toBe('今日心情卡')
    const request = captured()
    expect(request?.url.searchParams.get('persona_id')).toBe('3')
    expect(request?.url.searchParams.get('kind')).toBe('svg')
    expect(request?.url.searchParams.get('limit')).toBe('100')
  })

  it('listCanvas：缺省人格与非类型过滤不出现在查询串', async () => {
    const { captured } = capture(`${BACKEND_ORIGIN}/canvas`, 'get', () =>
      HttpResponse.json(okResult([])),
    )
    await listCanvas(undefined, CANVAS_DEFAULT_QUERY)
    const request = captured()
    expect(request?.url.searchParams.get('persona_id')).toBeNull()
    expect(request?.url.searchParams.get('kind')).toBeNull()
  })

  it('createCanvas：请求体与 persona 作用域', async () => {
    const { captured } = capture(`${BACKEND_ORIGIN}/canvas`, 'post', () =>
      HttpResponse.json(okResult(mockCanvas())),
    )
    const record = await createCanvas(2, createPayload)
    expect(record.revision).toBe(1)
    const request = captured()
    expect(request?.url.searchParams.get('persona_id')).toBe('2')
    expect(request?.body).toEqual(createPayload)
  })

  it('updateCanvas：路径主键 + 修订载荷；失败抛 ApiError', async () => {
    const { captured } = capture(`${BACKEND_ORIGIN}/canvas/:canvasId`, 'patch', () =>
      HttpResponse.json(okResult(mockCanvas({ revision: 2 }))),
    )
    const updated = await updateCanvas(undefined, 7, { title: '新题', content: null })
    expect(updated.revision).toBe(2)
    const request = captured()
    expect(request?.url.pathname.endsWith('/canvas/7')).toBe(true)
    expect(request?.body).toEqual({ title: '新题', content: null })

    server.use(
      http.patch(`${BACKEND_ORIGIN}/canvas/:canvasId`, () =>
        HttpResponse.json(errorResult('画布不存在', 'NOT_FOUND'), { status: 404 }),
      ),
    )
    await expect(
      updateCanvas(undefined, 99, { title: '改', content: null }),
    ).rejects.toBeInstanceOf(ApiError)
  })

  it('deleteCanvas：幂等结果透传', async () => {
    server.use(
      http.delete(`${BACKEND_ORIGIN}/canvas/:canvasId`, () =>
        HttpResponse.json(okResult({ deleted_ids: [] })),
      ),
    )
    const result = await deleteCanvas(undefined, 5)
    expect(result.deleted_ids).toEqual([])
  })

  it('downloadCanvas：blob 与响应头文件名；头缺失时按类型回退', async () => {
    const exported = await downloadCanvas(undefined, { id: 4, kind: 'html' })
    expect(exported.filename).toBe('canvas-4-r1.html')
    expect(await exported.blob.text()).toBe('<div>今天很好</div>')

    server.use(
      http.get(`${BACKEND_ORIGIN}/canvas/:canvasId/export`, () => {
        return new HttpResponse('<svg/>', { headers: { 'Content-Type': 'image/svg+xml' } })
      }),
    )
    const fallback = await downloadCanvas(undefined, { id: 9, kind: 'svg' })
    expect(fallback.filename).toBe('canvas-9.svg')
  })

  it('downloadCanvas：Markdown 回退扩展名 + 头存在但无 filename 段', async () => {
    server.use(
      http.get(`${BACKEND_ORIGIN}/canvas/:canvasId/export`, () => {
        return new HttpResponse('# 标题', {
          headers: {
            'Content-Type': 'text/markdown; charset=utf-8',
            'Content-Disposition': 'attachment',
          },
        })
      }),
    )
    const exported = await downloadCanvas(undefined, { id: 11, kind: 'markdown' })
    expect(exported.filename).toBe('canvas-11.md')
    expect(await exported.blob.text()).toBe('# 标题')
  })

  it('downloadCanvas：响应缺 Content-Type 时按八位字节流兜底', async () => {
    server.use(
      http.get(`${BACKEND_ORIGIN}/canvas/:canvasId/export`, () => {
        return new HttpResponse(new Uint8Array([104, 105]), {
          headers: { 'Content-Disposition': 'attachment; filename="canvas-12-r1.html"' },
        })
      }),
    )
    const exported = await downloadCanvas(undefined, { id: 12, kind: 'html' })
    expect(exported.blob.type).toBe('application/octet-stream')
    expect(await exported.blob.text()).toBe('hi')
  })

  it('downloadCanvas：服务端失败归一为 ApiError', async () => {
    server.use(
      http.get(`${BACKEND_ORIGIN}/canvas/:canvasId/export`, () =>
        HttpResponse.json(errorResult('画布不存在', 'NOT_FOUND'), { status: 404 }),
      ),
    )
    await expect(
      downloadCanvas(undefined, { id: 404, kind: 'markdown' }),
    ).rejects.toBeInstanceOf(ApiError)
  })
})

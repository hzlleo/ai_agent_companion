import { describe, expect, it } from 'vitest'
import { http, HttpResponse } from 'msw'
import {
  annotateSticker,
  deleteSticker,
  listStickers,
  updateSticker,
  uploadStickers,
} from '@/api/stickers'
import { ApiError } from '@/api/client'
import {
  BACKEND_ORIGIN,
  errorResult,
  mockSticker,
  okResult,
  server,
} from '@/test/server'
import { STICKERS_DEFAULT_QUERY } from '@/hooks/useStickersApi'
import type { StickerBatchUploadRequest } from '@/types/stickers'

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
  respond: () => ReturnType<typeof HttpResponse.json>,
): { captured: () => CapturedRequest | null } {
  let slot: CapturedRequest | null = null
  server.use(
    http[method](pattern, async ({ request }) => {
      const body = request.method === 'GET' || request.method === 'DELETE'
        ? null
        : await request.json().catch(() => null)
      slot = { method: request.method, url: new URL(request.url), body }
      return respond()
    }),
  )
  return { captured: () => slot }
}

const uploadPayload: StickerBatchUploadRequest = {
  items: [
    {
      image_base64: 'aGVsbG8=',
      content_type: 'image/png',
      source_note: '自绘',
      tags: ['开心'],
      description: '',
    },
  ],
}

describe('stickers 端点', () => {
  it('listStickers：persona_id / 过滤项 / 内联开关进查询串', async () => {
    const { captured } = capture(`${BACKEND_ORIGIN}/stickers`, 'get', () =>
      HttpResponse.json(okResult([mockSticker()])),
    )
    const records = await listStickers(4, {
      ...STICKERS_DEFAULT_QUERY,
      tag: '开心',
      annotation_status: 'failed',
    })
    const url = captured()?.url
    expect(url?.searchParams.get('persona_id')).toBe('4')
    expect(url?.searchParams.get('tag')).toBe('开心')
    expect(url?.searchParams.get('annotation_status')).toBe('failed')
    expect(url?.searchParams.get('limit')).toBe('100')
    expect(url?.searchParams.get('include_data')).toBe('true')
    expect(records[0]?.id).toBe(1)
  })

  it('listStickers：null 过滤项与 undefined 人格不出现', async () => {
    const { captured } = capture(`${BACKEND_ORIGIN}/stickers`, 'get', () =>
      HttpResponse.json(okResult([])),
    )
    await listStickers(undefined, STICKERS_DEFAULT_QUERY)
    const url = captured()?.url
    expect(url?.searchParams.get('persona_id')).toBeNull()
    expect(url?.searchParams.get('tag')).toBeNull()
    expect(url?.searchParams.get('annotation_status')).toBeNull()
  })

  it('uploadStickers：POST 批量载荷 + persona_id', async () => {
    const { captured } = capture(`${BACKEND_ORIGIN}/stickers`, 'post', () =>
      HttpResponse.json(okResult({ created: [mockSticker()], rejected: [] })),
    )
    const result = await uploadStickers(4, uploadPayload)
    expect(captured()?.method).toBe('POST')
    expect(captured()?.url.searchParams.get('persona_id')).toBe('4')
    expect(captured()?.body).toEqual(uploadPayload)
    expect(result.created).toHaveLength(1)
  })

  it('updateSticker：PATCH 路径 + 修改载荷', async () => {
    const { captured } = capture(`${BACKEND_ORIGIN}/stickers/:id`, 'patch', () =>
      HttpResponse.json(okResult(mockSticker({ tags: ['新'] }))),
    )
    const record = await updateSticker(4, 9, {
      tags: ['新'],
      description: null,
      source_note: null,
    })
    expect(captured()?.method).toBe('PATCH')
    expect(captured()?.url.pathname.endsWith('/stickers/9')).toBe(true)
    expect(captured()?.body).toEqual({ tags: ['新'], description: null, source_note: null })
    expect(record.tags).toEqual(['新'])
  })

  it('deleteSticker：DELETE 路径 + persona_id', async () => {
    const { captured } = capture(`${BACKEND_ORIGIN}/stickers/:id`, 'delete', () =>
      HttpResponse.json(okResult({ deleted_ids: [9], missing_ids: [] })),
    )
    const result = await deleteSticker(undefined, 9)
    expect(captured()?.method).toBe('DELETE')
    expect(captured()?.url.pathname.endsWith('/stickers/9')).toBe(true)
    expect(result.deleted_ids).toEqual([9])
  })

  it('annotateSticker：POST 标注端点返回可读结果', async () => {
    const { captured } = capture(`${BACKEND_ORIGIN}/stickers/:id/annotate`, 'post', () =>
      HttpResponse.json(
        okResult({
          ok: false,
          record: mockSticker({ annotation_status: 'failed', annotation_error: '端点不可用' }),
          message: '端点不可用',
        }),
      ),
    )
    const result = await annotateSticker(4, 9)
    expect(captured()?.url.pathname.endsWith('/stickers/9/annotate')).toBe(true)
    expect(result.ok).toBe(false)
    expect(result.record.annotation_status).toBe('failed')
  })

  it('失败面：错误 ResultDTO 转 ApiError', async () => {
    server.use(
      http.get(`${BACKEND_ORIGIN}/stickers`, () =>
        HttpResponse.json(errorResult('表情包库未启用', 'CONFLICT'), { status: 409 }),
      ),
    )
    await expect(listStickers(undefined, STICKERS_DEFAULT_QUERY)).rejects.toBeInstanceOf(ApiError)
  })
})

import { describe, expect, it } from 'vitest'
import { http, HttpResponse } from 'msw'
import {
  getTraitLibrary,
  importPersona,
  importPersonaFile,
  importPersonasBatch,
} from '@/api/personas'
import { ApiError } from '@/api/client'
import {
  BACKEND_ORIGIN,
  errorResult,
  isFileEntry,
  mockTraitLibrary,
  okResult,
  server,
} from '@/test/server'

/** 断言 rejects 为 ApiError 且 message 匹配。 */
async function expectApiError(promise: Promise<unknown>, message: string, code: string): Promise<void> {
  const error = await promise.catch((e: unknown) => e)
  expect(error).toBeInstanceOf(ApiError)
  const apiError = error as ApiError
  expect(apiError.message).toBe(message)
  expect(apiError.code).toBe(code)
}

describe('personas 导入 / 预设库端点', () => {
  it('getTraitLibrary：GET /personas/presets 解包画廊载荷', async () => {
    const library = await getTraitLibrary()
    expect(library.presets.map((preset) => preset.id)).toEqual(['tsundere', 'genki-girl'])
    expect(library.components.length).toBeGreaterThanOrEqual(5)
    expect(library.components[0]).toMatchObject({
      id: 'warm-hearted',
      name_zh: '温情底色',
      category: 'temperament',
    })
  })

  it('importPersona：JSON 面 POST /personas/import（预设 + 期望名 + 策略）', async () => {
    const holder: { url?: string; body?: Record<string, unknown> } = {}
    server.use(
      http.post(`${BACKEND_ORIGIN}/personas/import`, async ({ request }) => {
        holder.url = new URL(request.url).pathname
        holder.body = (await request.json()) as Record<string, unknown>
        return HttpResponse.json(
          okResult({ imported: true, persona_id: 11, reason: null, warnings: [] }, '人格已导入'),
        )
      }),
    )
    const result = await importPersona({
      preset_id: 'tsundere',
      card: null,
      conflict_strategy: 'rename',
      desired_name: '小桃',
    })
    expect(result.imported).toBe(true)
    expect(result.persona_id).toBe(11)
    expect(holder.url).toBe('/api/v1/personas/import')
    expect(holder.body).toEqual({
      preset_id: 'tsundere',
      card: null,
      conflict_strategy: 'rename',
      desired_name: '小桃',
    })
  })

  it('importPersonaFile：multipart 面（file + 策略 / 期望名字段）', async () => {
    const holder: {
      fileName?: string
      fileText?: string
      strategy?: string | null
      desired?: string | null
    } = {}
    server.use(
      http.post(`${BACKEND_ORIGIN}/personas/import`, async ({ request }) => {
        const form = await request.formData()
        const file = form.get('file')
        if (isFileEntry(file)) {
          holder.fileName = file.name
          holder.fileText = await file.text()
        }
        const strategy = form.get('conflict_strategy')
        holder.strategy = typeof strategy === 'string' ? strategy : null
        const desired = form.get('desired_name')
        holder.desired = typeof desired === 'string' ? desired : null
        return HttpResponse.json(
          okResult({ imported: true, persona_id: 12, reason: null, warnings: [] }, '人格已导入'),
        )
      }),
    )
    const file = new File([JSON.stringify({ name: '文件人格', first_mes: '嗨' })], 'card.json', {
      type: 'application/json',
    })
    const result = await importPersonaFile(file, { conflictStrategy: 'overwrite', desiredName: 'file-persona' })
    expect(result.imported).toBe(true)
    expect(holder.fileName).toBe('card.json')
    expect(JSON.parse(holder.fileText ?? '{}')).toMatchObject({ name: '文件人格' })
    expect(holder.strategy).toBe('overwrite')
    expect(holder.desired).toBe('file-persona')
  })

  it('importPersonasBatch：multipart 多文件（files 重复字段 + 策略字段）', async () => {
    const holder: { names?: string[]; strategy?: string | null } = {}
    server.use(
      http.post(`${BACKEND_ORIGIN}/personas/import/batch`, async ({ request }) => {
        const form = await request.formData()
        holder.names = form
          .getAll('files')
          .filter(isFileEntry)
          .map((file) => file.name)
        const strategy = form.get('conflict_strategy')
        holder.strategy = typeof strategy === 'string' ? strategy : null
        return HttpResponse.json(
          okResult(
            {
              total: 2,
              succeeded: 2,
              failed: 0,
              imported_count: 2,
              items: [],
            },
            '批量导入完成（成功 2 / 失败 0 / 新导入 2）',
          ),
        )
      }),
    )
    const files = [
      new File(['{"name":"甲"}'], 'a.json', { type: 'application/json' }),
      new File(['{"name":"乙"}'], 'b.json', { type: 'application/json' }),
    ]
    const result = await importPersonasBatch(files, 'skip')
    expect(result.total).toBe(2)
    expect(holder.names).toEqual(['a.json', 'b.json'])
    expect(holder.strategy).toBe('skip')
  })

  it('importPersona：未知预设 404 → ApiError（NOT_FOUND）', async () => {
    server.use(
      http.post(`${BACKEND_ORIGIN}/personas/import`, () =>
        HttpResponse.json(errorResult('官方预设不存在', 'NOT_FOUND'), { status: 404 }),
      ),
    )
    await expectApiError(
      importPersona({ preset_id: 'ghost', card: null, conflict_strategy: 'skip', desired_name: null }),
      '官方预设不存在',
      'NOT_FOUND',
    )
  })

  it('importPersonaFile：非法 JSON 卡片 422 → ApiError（VALIDATION_ERROR）', async () => {
    server.use(
      http.post(`${BACKEND_ORIGIN}/personas/import`, () =>
        HttpResponse.json(
          errorResult('导入卡片文件解析失败（须为 chara card v2 JSON 文档）', 'VALIDATION_ERROR'),
          { status: 422 },
        ),
      ),
    )
    const broken = new File(['not-json{'], 'broken.json', { type: 'application/json' })
    await expectApiError(
      importPersonaFile(broken),
      '导入卡片文件解析失败（须为 chara card v2 JSON 文档）',
      'VALIDATION_ERROR',
    )
  })

  it('getTraitLibrary：预设库装载失败 500 → ApiError', async () => {
    server.use(
      http.get(`${BACKEND_ORIGIN}/personas/presets`, () =>
        HttpResponse.json(
          errorResult('官方预设库数据非法（装载失败）', 'INTERNAL_ERROR'),
          { status: 500 },
        ),
      ),
    )
    await expectApiError(getTraitLibrary(), '官方预设库数据非法（装载失败）', 'INTERNAL_ERROR')
  })

  it('mockTraitLibrary：默认 handler 覆盖画廊成功形态', async () => {
    server.use(
      http.get(`${BACKEND_ORIGIN}/personas/presets`, () =>
        HttpResponse.json(okResult(mockTraitLibrary())),
      ),
    )
    const library = await getTraitLibrary()
    const genki = library.presets.find((preset) => preset.id === 'genki-girl')
    expect(genki?.base_card_overrides.name_suffix).toBe('-酱')
    expect(genki?.components).toEqual(['energetic-sunny', 'pun-enthusiast'])
  })
})

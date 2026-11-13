/**
 * 共享 msw 服务器：setupServer 单例 + personas 域默认 handlers。
 *
 * 约定（本工程）：
 * - 默认 handlers 覆盖 personas 全部路由的成功形态（真实 wire 契约），
 *   各测试经 ``server.use(http...)`` 覆写错误路径（400/404/409 等）；
 * - onUnhandledRequest: 'error'（setup.ts 注册）——任何未 mock 的请求
 *   直接失败，测试永不依赖真实后端。
 */

import { setupServer } from 'msw/node'
import { http, HttpResponse } from 'msw'
import { API_BASE_URL, API_PREFIX } from '@/api/client'
import type { ResultDTO } from '@/types/common'
import type {
  PersonaRecord,
  PersonaState,
} from '@/types/persona'

/** 后端 URL 前缀（handlers 统一经此拼接，与 api client 同源）。 */
export const BACKEND_ORIGIN = `${API_BASE_URL}${API_PREFIX}`

/** 构造成功 ResultDTO。 */
export function okResult<T>(data: T, message = '成功'): ResultDTO<T> {
  return { success: true, code: '200', message, data }
}

/** 构造失败 ResultDTO（HTTP 状态由 handler 指定）。 */
export function errorResult(message: string, code: string, details: unknown = null): ResultDTO<unknown> {
  return { success: false, code, message, data: details }
}

/** 测试用人格卡片（wire 形态）。 */
export const mockCard = {
  name: '小助手',
  description: '一个温柔的陪伴型人格',
  personality: '温柔、耐心、共情',
  scenario: '深夜陪伴对话',
  first_mes: '你好呀，今天过得怎么样？',
  mes_example: '<START>\nuser: 我好累\nassistant: 辛苦了，想聊聊吗？',
  system_prompt: '',
  post_history_instructions: '',
  character_book: null,
}

/** 测试用人格记录。 */
export function mockPersona(overrides: Partial<PersonaRecord> = {}): PersonaRecord {
  return {
    id: 1,
    name: 'companion-1',
    display_name: '小助手',
    status: 'active',
    is_active: true,
    card: { ...mockCard },
    created_at: 1731000000000,
    updated_at: 1731000000000,
    ...overrides,
  }
}

/** 测试用状态层。 */
export function mockState(overrides: Partial<PersonaState> = {}): PersonaState {
  return {
    persona_id: 1,
    layer: 'M',
    state: { mood: 'calm', affinity: 0.6 },
    created_at: 1731000000000,
    updated_at: 1731000000000,
    ...overrides,
  }
}

/** personas 域默认 handlers（成功形态）。 */
export const personaHandlers = [
  // 游标分页列表：首页 2 条 + next_cursor；带 cursor 时返回末页。
  http.get(`${BACKEND_ORIGIN}/personas`, ({ request }) => {
    const url = new URL(request.url)
    const cursor = url.searchParams.get('cursor')
    const status = url.searchParams.get('status')
    const all = [
      mockPersona({ id: 2, name: 'companion-2', is_active: false }),
      mockPersona({ id: 1, name: 'companion-1', is_active: true }),
      mockPersona({ id: 3, name: 'old-friend', status: 'disabled', is_active: false }),
    ]
    const filtered = status === null ? all : all.filter((p) => p.status === status)
    if (cursor === null) {
      return HttpResponse.json(
        okResult({
          items: filtered.slice(0, 2),
          total: -1,
          page: 1,
          page_size: 20,
          next_cursor: 'cursor-page-2',
          has_more: true,
        }),
      )
    }
    return HttpResponse.json(
      okResult({
        items: [filtered[2] ?? mockPersona({ id: 3, name: 'old-friend', status: 'disabled', is_active: false })],
        total: -1,
        page: 1,
        page_size: 20,
        next_cursor: null,
        has_more: false,
      }),
    )
  }),
  // 创建。
  http.post(`${BACKEND_ORIGIN}/personas`, async ({ request }) => {
    const body = (await request.json()) as { name: string }
    return HttpResponse.json(okResult(mockPersona({ id: 9, name: body.name }), '人格已创建'))
  }),
  // 批量删除（部分成功形态：id=1 激活守卫失败）。
  http.post(`${BACKEND_ORIGIN}/personas/batch-delete`, async ({ request }) => {
    const body = (await request.json()) as { ids: number[] }
    const items = body.ids.map((id) =>
      id === 1
        ? { id, success: false, code: 'CONFLICT', message: '当前激活人格禁止删除，请先停用' }
        : { id, success: true, code: null, message: '已删除' },
    )
    const succeeded = items.filter((item) => item.success).length
    return HttpResponse.json(
      okResult(
        {
          total: body.ids.length,
          succeeded,
          failed: body.ids.length - succeeded,
          items,
        },
        `批量删除完成（成功 ${succeeded} / 失败 ${body.ids.length - succeeded}）`,
      ),
    )
  }),
  // 读取。
  http.get(`${BACKEND_ORIGIN}/personas/:personaId`, ({ params }) => {
    const id = Number(params.personaId)
    if (id === 404) {
      return HttpResponse.json(errorResult('人格不存在', 'NOT_FOUND'), { status: 404 })
    }
    return HttpResponse.json(okResult(mockPersona({ id })))
  }),
  // 部分更新。
  http.put(`${BACKEND_ORIGIN}/personas/:personaId`, async ({ request }) => {
    const body = (await request.json()) as { name?: string }
    return HttpResponse.json(okResult(mockPersona({ name: body.name ?? 'companion-1' }), '人格已更新'))
  }),
  // 删除：id=1 为激活人格 → 409 激活守卫。
  http.delete(`${BACKEND_ORIGIN}/personas/:personaId`, ({ params }) => {
    const id = Number(params.personaId)
    if (id === 1) {
      return HttpResponse.json(errorResult('当前激活人格禁止删除，请先停用', 'CONFLICT'), {
        status: 409,
      })
    }
    if (id === 404) {
      return HttpResponse.json(errorResult('人格不存在', 'NOT_FOUND'), { status: 404 })
    }
    return HttpResponse.json(okResult(null, '人格已删除'))
  }),
  // 复制。
  http.post(`${BACKEND_ORIGIN}/personas/:personaId/copy`, async ({ request }) => {
    const body = (await request.json()) as { new_name: string }
    if (body.new_name === 'companion-1') {
      return HttpResponse.json(errorResult('注册名已被占用', 'CONFLICT'), { status: 409 })
    }
    return HttpResponse.json(
      okResult(mockPersona({ id: 10, name: body.new_name, is_active: false, status: 'disabled' }), '人格已复制'),
    )
  }),
  // 激活：id=3（old-friend，停用状态）→ 409。
  http.post(`${BACKEND_ORIGIN}/personas/:personaId/activate`, ({ params }) => {
    const id = Number(params.personaId)
    if (id === 3) {
      return HttpResponse.json(errorResult('停用状态的人格不可激活', 'CONFLICT'), { status: 409 })
    }
    return HttpResponse.json(okResult(mockPersona({ id, is_active: true }), '人格已激活（单激活切换）'))
  }),
  // 停用激活。
  http.post(`${BACKEND_ORIGIN}/personas/:personaId/deactivate`, ({ params }) => {
    const id = Number(params.personaId)
    return HttpResponse.json(okResult(mockPersona({ id, is_active: false }), '人格激活位已停用'))
  }),
  // 卡片读取。
  http.get(`${BACKEND_ORIGIN}/personas/:personaId/card`, ({ params }) => {
    const id = Number(params.personaId)
    return HttpResponse.json(okResult({ persona_id: id, card: { ...mockCard }, updated_at: 1731000000000 }))
  }),
  // 卡片整体替换。
  http.put(`${BACKEND_ORIGIN}/personas/:personaId/card`, async ({ request }) => {
    const body = (await request.json()) as { name: string }
    const id = Number(new URL(request.url).pathname.split('/').pop())
    return HttpResponse.json(
      okResult({ persona_id: id, card: { ...mockCard, name: body.name }, updated_at: 1731000000001 }, '人格卡片已替换'),
    )
  }),
  // 状态层读取（M 已写 / S 未写）。
  http.get(`${BACKEND_ORIGIN}/personas/:personaId/states`, ({ params }) => {
    const id = Number(params.personaId)
    return HttpResponse.json(okResult([mockState({ persona_id: id, layer: 'M' })]))
  }),
  // 状态层 upsert。
  http.put(`${BACKEND_ORIGIN}/personas/:personaId/states/:layer`, async ({ request }) => {
    const body = (await request.json()) as { state: Record<string, unknown> }
    const url = new URL(request.url)
    const id = Number(url.pathname.split('/')[4])
    const layer = url.pathname.split('/').pop()
    return HttpResponse.json(
      okResult(
        mockState({ persona_id: id, layer: layer === 'S' ? 'S' : 'M', state: body.state }),
        '人格状态已更新',
      ),
    )
  }),
]

/** 共享 msw 服务器（setup.ts 管理生命周期）。 */
export const server = setupServer(...personaHandlers)

import { describe, expect, it } from 'vitest'
import { http, HttpResponse } from 'msw'
import {
  createSkill,
  deleteSkill,
  disableSkill,
  enableSkill,
  listSkills,
  updateSkill,
} from '@/api/skills'
import { ApiError } from '@/api/client'
import { BACKEND_ORIGIN, mockSkill, okResult, server } from '@/test/server'

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

describe('skills 端点（人格作用域查询参数）', () => {
  it('listSkills：persona_id / 状态 / 游标进查询串；undefined 时不发送', async () => {
    const { captured } = capture(`${BACKEND_ORIGIN}/skills`, 'get', () =>
      HttpResponse.json(
        okResult({
          items: [mockSkill()],
          total: -1,
          page: 1,
          page_size: 20,
          next_cursor: null,
          has_more: false,
        }),
      ),
    )
    const page = await listSkills(4, { status: 'active', cursor: 'abc', limit: 5 })
    expect(captured()?.url.searchParams.get('persona_id')).toBe('4')
    expect(captured()?.url.searchParams.get('status')).toBe('active')
    expect(captured()?.url.searchParams.get('cursor')).toBe('abc')
    expect(captured()?.url.searchParams.get('limit')).toBe('5')
    expect(page.items[0]?.status).toBe('active')

    await listSkills(undefined)
    expect(captured()?.url.searchParams.has('persona_id')).toBe(false)
    expect(captured()?.url.searchParams.has('status')).toBe(false)
  })

  it('createSkill：POST 体透传（name / description / triggers / content / confidence）', async () => {
    const { captured } = capture(`${BACKEND_ORIGIN}/skills`, 'post', () =>
      HttpResponse.json(okResult(mockSkill({ id: 9 }), '技能已创建')),
    )
    await createSkill(2, {
      name: '日报流程',
      description: '主人说「写日报」时使用',
      triggers: ['写日报'],
      content: '1. 写今日完成',
      confidence: 0.7,
    })
    expect(captured()?.url.searchParams.get('persona_id')).toBe('2')
    expect(captured()?.body).toEqual({
      name: '日报流程',
      description: '主人说「写日报」时使用',
      triggers: ['写日报'],
      content: '1. 写今日完成',
      confidence: 0.7,
    })
  })

  it('updateSkill：PUT 编辑路径 + 版本递增 + 404 → ApiError', async () => {
    const { captured } = capture(`${BACKEND_ORIGIN}/skills/:skillId`, 'put', () =>
      HttpResponse.json(okResult(mockSkill({ id: 1, version: 2 }), '技能已保存')),
    )
    const updated = await updateSkill(3, 1, { content: '1. 新的步骤' })
    expect(captured()?.url.pathname.endsWith('/skills/1')).toBe(true)
    expect(captured()?.url.searchParams.get('persona_id')).toBe('3')
    expect(captured()?.body).toEqual({ content: '1. 新的步骤' })
    expect(updated.version).toBe(2)

    server.use(
      http.put(`${BACKEND_ORIGIN}/skills/:skillId`, () =>
        HttpResponse.json(
          { success: false, code: 'NOT_FOUND', message: '技能不存在', data: null },
          { status: 404 },
        ),
      ),
    )
    await expect(updateSkill(3, 404, { content: '不存在' })).rejects.toBeInstanceOf(ApiError)
  })

  it('enableSkill / disableSkill：状态切换路径', async () => {
    const enableCapture = capture(`${BACKEND_ORIGIN}/skills/:skillId/enable`, 'post', () =>
      HttpResponse.json(okResult(mockSkill({ id: 1, status: 'active' }), '技能已启用')),
    )
    const enabled = await enableSkill(2, 1)
    expect(enableCapture.captured()?.url.pathname.endsWith('/skills/1/enable')).toBe(true)
    expect(enabled.status).toBe('active')

    const disableCapture = capture(`${BACKEND_ORIGIN}/skills/:skillId/disable`, 'post', () =>
      HttpResponse.json(okResult(mockSkill({ id: 1, status: 'disabled' }), '技能已停用')),
    )
    const disabled = await disableSkill(2, 1)
    expect(disableCapture.captured()?.url.searchParams.get('persona_id')).toBe('2')
    expect(disabled.status).toBe('disabled')
  })

  it('deleteSkill：DELETE 路径 + 404 抛 ApiError', async () => {
    const { captured } = capture(`${BACKEND_ORIGIN}/skills/:skillId`, 'delete', () =>
      HttpResponse.json(okResult(null, '技能已删除')),
    )
    await deleteSkill(2, 1)
    expect(captured()?.method).toBe('DELETE')
    expect(captured()?.url.pathname.endsWith('/skills/1')).toBe(true)

    server.use(
      http.delete(`${BACKEND_ORIGIN}/skills/:skillId`, () =>
        HttpResponse.json(
          { success: false, code: 'NOT_FOUND', message: '技能不存在', data: null },
          { status: 404 },
        ),
      ),
    )
    await expect(deleteSkill(2, 404)).rejects.toBeInstanceOf(ApiError)
  })
})

import { describe, expect, it } from 'vitest'
import { getPersona, listPersonas } from '@/api/personas'

/**
 * msw 拦截烟囱测试：验证 axios 请求被 msw 拦截并按 ResultDTO 契约
 * 解包（测试环境永不触达真实后端）。
 */
describe('msw 拦截烟囱', () => {
  it('listPersonas 经 msw 返回首页数据', async () => {
    const page = await listPersonas({})
    expect(page.items).toHaveLength(2)
    expect(page.next_cursor).toBe('cursor-page-2')
    expect(page.has_more).toBe(true)
  })

  it('getPersona 经 msw 返回记录', async () => {
    const record = await getPersona(1)
    expect(record.id).toBe(1)
    expect(record.card.name).toBe('小助手')
  })
})

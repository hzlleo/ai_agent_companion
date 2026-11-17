import { describe, expect, it, beforeEach } from 'vitest'
import {
  clearAdminToken,
  getAdminToken,
  notifyAuthRequired,
  onAuthRequired,
  setAdminToken,
} from '@/api/auth'

/** token 持久化键（与模块内常量同值——wire 契约锚）。 */
const STORAGE_KEY = 'aac_admin_token'

describe('admin token 存取', () => {
  beforeEach(() => {
    localStorage.clear()
  })

  it('未保存 → 空串（无 token 语义）', () => {
    expect(getAdminToken()).toBe('')
  })

  it('保存 → 读取（trim 归一——前后空白不进入持久值）', () => {
    setAdminToken('  secret-token  ')
    expect(localStorage.getItem(STORAGE_KEY)).toBe('secret-token')
    expect(getAdminToken()).toBe('secret-token')
  })

  it('清除 → 空串 + localStorage 键移除', () => {
    setAdminToken('secret-token')
    clearAdminToken()
    expect(localStorage.getItem(STORAGE_KEY)).toBeNull()
    expect(getAdminToken()).toBe('')
  })
})

describe('鉴权需求事件', () => {
  it('订阅后广播触发；退订后不再触发', () => {
    const calls: string[] = []
    const off = onAuthRequired(() => calls.push('a'))
    notifyAuthRequired()
    expect(calls).toEqual(['a'])
    off()
    notifyAuthRequired()
    expect(calls).toEqual(['a'])
  })

  it('多监听器各自触发；单个退订互不影响', () => {
    const calls: string[] = []
    const offA = onAuthRequired(() => calls.push('a'))
    const offB = onAuthRequired(() => calls.push('b'))
    offA()
    notifyAuthRequired()
    expect(calls).toEqual(['b'])
    offB()
    notifyAuthRequired()
    expect(calls).toEqual(['b'])
  })
})

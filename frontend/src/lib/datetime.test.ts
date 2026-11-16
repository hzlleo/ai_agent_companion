import { describe, expect, it } from 'vitest'
import { datetimeLocalToEpochMs, epochMsToDatetimeLocal, formatEpochMs } from '@/lib/datetime'

describe('formatEpochMs', () => {
  it('epoch 毫秒 → zh-CN 日期时间串', () => {
    const text = formatEpochMs(new Date(2026, 0, 2, 3, 4, 5).getTime())
    expect(text).toMatch(/2026/)
    expect(text).toMatch(/01\/02/)
    expect(text).toMatch(/03:04:05/)
  })

  it('非法数值 → 占位符', () => {
    expect(formatEpochMs(Number.NaN)).toBe('-')
  })
})

describe('datetimeLocalToEpochMs', () => {
  it('datetime-local 值 → epoch 毫秒（本地时区解析）', () => {
    const expected = new Date(2026, 10, 13, 9, 30).getTime()
    expect(datetimeLocalToEpochMs('2026-11-13T09:30')).toBe(expected)
  })

  it('空串 / 非法值 → null', () => {
    expect(datetimeLocalToEpochMs('')).toBeNull()
    expect(datetimeLocalToEpochMs('  ')).toBeNull()
    expect(datetimeLocalToEpochMs('not-a-date')).toBeNull()
  })
})

describe('epochMsToDatetimeLocal', () => {
  it('epoch 毫秒 → datetime-local 输入值（与解析互逆）', () => {
    const ms = new Date(2026, 10, 13, 9, 30).getTime()
    expect(epochMsToDatetimeLocal(ms)).toBe('2026-11-13T09:30')
    expect(datetimeLocalToEpochMs(epochMsToDatetimeLocal(ms))).toBe(ms)
  })

  it('非法数值 → 空串', () => {
    expect(epochMsToDatetimeLocal(Number.NaN)).toBe('')
  })
})

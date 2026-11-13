import { describe, expect, it } from 'vitest'
import { formatEpochMs } from '@/lib/datetime'

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

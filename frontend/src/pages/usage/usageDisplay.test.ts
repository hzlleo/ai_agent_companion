import { describe, expect, it } from 'vitest'
import {
  GROUP_BY_LABELS,
  PURPOSE_LABELS,
  STATUS_LABELS,
  TIME_BUCKET_LABELS,
  aggregateGroupDisplay,
  formatTokenCount,
  purposeLabel,
} from './usageDisplay'

describe('usageDisplay', () => {
  it('值域标签覆盖', () => {
    expect(PURPOSE_LABELS['look_at']).toBe('多模态查看')
    expect(STATUS_LABELS['error']).toBe('失败')
    expect(GROUP_BY_LABELS['time_bucket']).toBe('按时间桶')
    expect(TIME_BUCKET_LABELS['minute']).toBe('分钟')
  })

  it('purposeLabel：已知翻译 / 未知回落原文', () => {
    expect(purposeLabel('chat')).toBe('主对话')
    expect(purposeLabel('unknown-purpose')).toBe('unknown-purpose')
  })

  it('aggregateGroupDisplay：time_bucket 行格式化桶起点；其余原样', () => {
    expect(aggregateGroupDisplay('1731000000000', 1731000000000)).toMatch(/20/)
    expect(aggregateGroupDisplay('main', null)).toBe('main')
  })

  it('formatTokenCount：zh-CN 千分位', () => {
    expect(formatTokenCount(1234567)).toBe('1,234,567')
    expect(formatTokenCount(0)).toBe('0')
  })
})

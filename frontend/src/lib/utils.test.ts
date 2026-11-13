import { describe, expect, it } from 'vitest'
import { cn } from '@/lib/utils'

describe('cn', () => {
  it('条件类名拼接', () => {
    function build(flag: boolean): string {
      return cn('a', flag && 'b', !flag && 'c')
    }
    expect(build(true)).toBe('a b')
  })

  it('冲突类名去重（后者胜）', () => {
    expect(cn('px-2', 'px-4')).toBe('px-4')
  })
})

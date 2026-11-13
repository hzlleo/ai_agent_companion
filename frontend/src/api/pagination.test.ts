import { describe, expect, it } from 'vitest'
import { flattenCursorPages, getCursorPageParam, isAllSelected } from '@/api/pagination'
import type { PageResponse } from '@/types/common'
import type { PersonaRecord } from '@/types/persona'

function page(items: number[], nextCursor: string | null): PageResponse<PersonaRecord> {
  return {
    items: items.map((id) => ({ id }) as PersonaRecord),
    total: -1,
    page: 1,
    page_size: 20,
    next_cursor: nextCursor,
    has_more: nextCursor !== null,
  }
}

describe('游标分页助手', () => {
  it('getCursorPageParam：next_cursor 非空 → 原样返回', () => {
    expect(getCursorPageParam(page([1], 'cursor-2'))).toBe('cursor-2')
  })

  it('getCursorPageParam：next_cursor 为 null → undefined（终止翻页）', () => {
    expect(getCursorPageParam(page([1], null))).toBeUndefined()
  })

  it('flattenCursorPages：多页摊平保序', () => {
    const flattened = flattenCursorPages([page([1, 2], 'c2'), page([3], null)])
    expect(flattened.map((p) => p.id)).toEqual([1, 2, 3])
  })

  it('flattenCursorPages：空页列表 → 空数组', () => {
    expect(flattenCursorPages([])).toEqual([])
  })

  it('isAllSelected：已加载非空且全选中 → true', () => {
    expect(isAllSelected([1, 2, 3], new Set([1, 2, 3]))).toBe(true)
  })

  it('isAllSelected：部分选中 → false', () => {
    expect(isAllSelected([1, 2, 3], new Set([1]))).toBe(false)
  })

  it('isAllSelected：已加载为空 → false（无可全选对象）', () => {
    expect(isAllSelected([], new Set([1]))).toBe(false)
  })
})

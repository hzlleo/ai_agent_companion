/**
 * 游标分页助手：next_cursor 传递 / 页摊平 / has_more 判定。
 *
 * 供 useInfiniteQuery 消费（queryFn 直连列表端点；getNextPageParam 经
 * ``getCursorPageParam`` 取上一页返回的 ``next_cursor``），以及列表
 * 渲染前的多页摊平（``flattenCursorPages``）。
 */

import type { PageResponse } from '@/types/common'

/** useInfiniteQuery 的 pageParam 形态（首页 undefined）。 */
export type CursorPageParam = string | undefined

/** 下一页参数：next_cursor 非空才有下一页（null → undefined 终止）。 */
export function getCursorPageParam<T>(lastPage: PageResponse<T>): CursorPageParam {
  return lastPage.next_cursor ?? undefined
}

/** 多页摊平：把 useInfiniteQuery 的 pages 拼为单一列表。 */
export function flattenCursorPages<T>(pages: PageResponse<T>[]): T[] {
  return pages.flatMap((page) => page.items)
}

/** 当前选中集是否覆盖了全部已加载行（批量选择态判定）。 */
export function isAllSelected(loadedIds: number[], selectedIds: Set<number>): boolean {
  return loadedIds.length > 0 && loadedIds.every((id) => selectedIds.has(id))
}

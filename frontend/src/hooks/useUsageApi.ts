/**
 * usage React Query hooks：3 个端点的数据面封装。
 *
 * 约定（本工程）：
 * - 记录列表经 useInfiniteQuery 消费游标分页（pageParam = next_cursor）；
 * - 总览 / 聚合为只读查询（过滤面变化即自动重取）；
 * - 跨人格观测：persona_id 为普通字符串过滤（undefined = 全人格）。
 */

import { useInfiniteQuery, useQuery } from '@tanstack/react-query'
import { aggregateUsage, listUsageRecords, usageSummary } from '@/api/usage'
import { getCursorPageParam } from '@/api/pagination'
import type { UsageAggregateParams, UsageFilterParams, UsageRecordListParams } from '@/types/usage'

/** usage 缓存族前缀。 */
export const USAGE_QUERY_KEY = ['usage'] as const

/** 用量记录游标分页列表（多轴过滤 + load more）。 */
export function useUsageRecordsList(params: UsageRecordListParams) {
  return useInfiniteQuery({
    queryKey: [...USAGE_QUERY_KEY, 'records', params],
    queryFn: ({ pageParam }) => listUsageRecords({ ...params, cursor: pageParam }),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: getCursorPageParam,
  })
}

/** 用量多维聚合（group_by 必填；time_bucket 维度携带桶宽）。 */
export function useUsageAggregates(params: UsageAggregateParams) {
  return useQuery({
    queryKey: [...USAGE_QUERY_KEY, 'aggregates', params],
    queryFn: () => aggregateUsage(params),
  })
}

/** 用量总览（总量 + 均时延 + per-purpose 明细）。 */
export function useUsageSummary(params: UsageFilterParams) {
  return useQuery({
    queryKey: [...USAGE_QUERY_KEY, 'summary', params],
    queryFn: () => usageSummary(params),
  })
}

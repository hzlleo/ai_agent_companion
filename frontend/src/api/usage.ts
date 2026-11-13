/**
 * usage 端点层：后端 3 个路由的 typed 封装。
 *
 * 路由面（全部经 ResultDTO 解包，失败抛 ApiError）：
 *   - GET /usage/records     用量记录游标分页（多轴过滤）
 *   - GET /usage/aggregates  多维聚合（model_entry / purpose / time_bucket）
 *   - GET /usage/summary     总览（总量 + 均时延 + per-purpose 明细）
 *
 * 跨人格观测语义（本工程约定）：用量端点无人格上下文要求——
 * persona_id 为普通字符串过滤参数（undefined = 全人格）。
 */

import { request } from './client'
import type { PageResponse } from '@/types/common'
import type {
  UsageAggregate,
  UsageAggregateParams,
  UsageFilterParams,
  UsageRecord,
  UsageRecordListParams,
  UsageSummary,
} from '@/types/usage'

/** GET /usage/records —— 用量记录游标分页（id 降序；过滤面全可选）。 */
export async function listUsageRecords(
  params: UsageRecordListParams,
): Promise<PageResponse<UsageRecord>> {
  return request<PageResponse<UsageRecord>>({
    method: 'GET',
    url: '/usage/records',
    params,
  })
}

/** GET /usage/aggregates —— 多维聚合（分组行按总 token 降序；time_bucket 维度须声明桶宽）。 */
export async function aggregateUsage(params: UsageAggregateParams): Promise<UsageAggregate[]> {
  return request<UsageAggregate[]>({
    method: 'GET',
    url: '/usage/aggregates',
    params,
  })
}

/** GET /usage/summary —— 用量总览（经 group_by=purpose 聚合派生）。 */
export async function usageSummary(params: UsageFilterParams): Promise<UsageSummary> {
  return request<UsageSummary>({
    method: 'GET',
    url: '/usage/summary',
    params,
  })
}

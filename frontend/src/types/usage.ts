/**
 * usage 域类型契约（与后端 harness.usage 契约对齐的 wire 形态）。
 *
 * 覆盖面：用量记录（游标分页 + 多轴过滤）/ 多维聚合（model_entry /
 * purpose / time_bucket）/ 总览（总量 + 均时延 + per-purpose 明细）。
 * 时间语义：全部 epoch 毫秒；范围过滤左闭右开（start_ms <= ts_ms <
 * end_ms——时间桶对齐无重叠）。本文件只承载类型（Unit 文件）。
 */

// ---------------------------------------------------------------------------
// 值域
// ---------------------------------------------------------------------------

/** 调用用途值域（五大模型角色 + 辅助能力用途）。 */
export type UsagePurpose =
  | 'chat'
  | 'duty'
  | 'dream'
  | 'look_at'
  | 'embed'
  | 'tts'
  | 'asr'
  | 'tool'

/** 调用结果值域。 */
export type UsageStatus = 'success' | 'error'

/** 聚合分组维度值域。 */
export type UsageGroupBy = 'model_entry' | 'purpose' | 'time_bucket'

/** 时间桶宽值域（group_by=time_bucket 时必填）。 */
export type UsageTimeBucket = 'minute' | 'hour' | 'day'

// ---------------------------------------------------------------------------
// 记录 / 聚合 / 总览
// ---------------------------------------------------------------------------

/** 用量记录（ORM 行归一）。 */
export interface UsageRecord {
  /** 记录主键。 */
  id: number
  /** 人格 ID（null = 系统调用——跨人格观测口径）。 */
  persona_id: string | null
  /** 调用发起时间（epoch 毫秒）。 */
  ts_ms: number
  /** 模型条目名。 */
  model_entry: string
  /** provider 类型。 */
  provider: string
  /** 调用用途。 */
  purpose: UsagePurpose
  /** 输入 token 数。 */
  input_tokens: number
  /** 输出 token 数。 */
  output_tokens: number
  /** 总 token 数。 */
  total_tokens: number
  /** 端到端时延（毫秒）。 */
  latency_ms: number
  /** 调用结果。 */
  status: UsageStatus
  /** 请求追踪 ID（可空）。 */
  request_id: string | null
  /** 失败错误码（成功为 null）。 */
  error_code: string | null
  /** 记录落库时间（epoch 毫秒）。 */
  created_at: number
}

/** 用量聚合结果（单分组行——按总 token 降序返回）。 */
export interface UsageAggregate {
  /** 分组键（条目名 / 用途值 / 桶起点毫秒字符串形态）。 */
  group: string
  /** 时间桶起点（epoch 毫秒；仅 time_bucket 维度非 null）。 */
  bucket_ms: number | null
  /** 输入 token 求和。 */
  input_tokens: number
  /** 输出 token 求和。 */
  output_tokens: number
  /** 总 token 求和。 */
  total_tokens: number
  /** 调用次数。 */
  call_count: number
  /** 平均时延（毫秒，四舍五入取整）。 */
  avg_latency_ms: number
  /** 失败次数（status=error 计数）。 */
  error_count: number
}

/** 用量总览（单 payload：总量 + 均时延 + per-purpose 明细）。 */
export interface UsageSummary {
  /** 调用总次数。 */
  call_count: number
  /** 输入 token 总量。 */
  input_tokens: number
  /** 输出 token 总量。 */
  output_tokens: number
  /** 总 token 总量。 */
  total_tokens: number
  /** 整体平均时延（毫秒，按调用次数加权）。 */
  avg_latency_ms: number
  /** 失败总次数（status=error）。 */
  error_count: number
  /** per-purpose 聚合明细（总量降序）。 */
  purposes: UsageAggregate[]
}

// ---------------------------------------------------------------------------
// 查询参数
// ---------------------------------------------------------------------------

/** 用量公共过滤面（记录 / 聚合 / 总览共用；全可选）。 */
export interface UsageFilterParams {
  /** 人格 ID 过滤（undefined = 全人格——管理面口径）。 */
  persona_id?: string
  /** 模型条目名过滤（undefined = 全条目）。 */
  model_entry?: string
  /** 调用用途过滤（undefined = 全用途）。 */
  purpose?: UsagePurpose
  /** 调用结果过滤（undefined = 全状态）。 */
  status?: UsageStatus
  /** 范围起点（epoch 毫秒，含）。 */
  start_ms?: number
  /** 范围终点（epoch 毫秒，不含）。 */
  end_ms?: number
}

/** 用量记录列表查询参数（过滤面 + 游标分页）。 */
export interface UsageRecordListParams extends UsageFilterParams {
  /** 分页游标（首页 undefined）。 */
  cursor?: string
  /** 页大小（1-200，默认 20）。 */
  limit?: number
}

/** 用量聚合查询参数（分组维度 + 过滤面）。 */
export interface UsageAggregateParams extends UsageFilterParams {
  /** 分组维度（必填）。 */
  group_by: UsageGroupBy
  /** 时间桶宽（group_by=time_bucket 时必填；其余维度忽略）。 */
  time_bucket?: UsageTimeBucket
}

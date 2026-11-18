/**
 * reminders 域类型契约（与后端 harness.reminders 契约对齐的 wire 形态）。
 *
 * 覆盖面：持久化提醒（ff_reminders——人格作用域 / 到期扫描投递 /
 * 状态单向推进）/ 创建 / 查询参数 / 批量取消（部分成功形态）。本文件
 * 只承载类型（Unit 文件）——结构化数据一律走这里的接口形态，禁止裸
 * 对象字面量承载结构化语义。
 */

/** 提醒状态（ff_reminders.status——单向推进：pending → 其余终态）。 */
export type ReminderStatus = 'pending' | 'delivered' | 'cancelled' | 'failed'

/** 提醒来源（ff_reminders.source——观测面）。 */
export type ReminderSource = 'manual' | 'chat'

/** 提醒记录（ORM 行归一——读取 / 投递观测面）。 */
export interface ReminderRecord {
  /** 提醒主键。 */
  id: number
  /** 所属人格 ID。 */
  persona_id: string
  /** 提醒标题（投递文案正文）。 */
  title: string
  /** 到期时刻（epoch 毫秒）。 */
  due_at_ms: number
  /** 状态。 */
  status: ReminderStatus
  /** 投递失败累计次数。 */
  delivery_attempts: number
  /** 最近一次投递失败原因（null = 无失败）。 */
  last_error: string | null
  /** 投递成功时刻（epoch 毫秒；null = 未投递）。 */
  delivered_at_ms: number | null
  /** 来源。 */
  source: ReminderSource
  /** 来源附加信息（可空 JSON）。 */
  meta: Record<string, unknown> | null
  /** 落库时间（epoch 毫秒）。 */
  created_at: number
  /** 行更新时间（epoch 毫秒）。 */
  updated_at: number
}

/** 提醒创建请求（到期时刻允许过去——创建即到期的补投语义）。 */
export interface ReminderCreateRequest {
  /** 提醒标题（1-200 字符）。 */
  title: string
  /** 到期时刻（epoch 毫秒）。 */
  due_at_ms: number
  /** 来源（缺省 manual——后端缺省值）。 */
  source?: ReminderSource
  /** 来源附加信息（可空）。 */
  meta?: Record<string, unknown> | null
}

/** 提醒列表查询参数（status 过滤 + 游标分页）。 */
export interface ReminderListParams {
  /** 状态过滤（undefined = 全部）。 */
  status?: ReminderStatus
  /** 分页游标（首页 undefined）。 */
  cursor?: string
  /** 页大小（1-200，默认 20）。 */
  limit?: number
}

/** 批量取消请求（部分成功形态——逐 id 独立判定）。 */
export interface ReminderBatchCancelRequest {
  /** 待取消提醒主键列表（1-100 个且不得重复）。 */
  ids: number[]
}

/** 批量取消结果单条目。 */
export interface ReminderBatchCancelItem {
  /** 提醒主键。 */
  id: number
  /** 是否取消成功。 */
  success: boolean
  /** 失败错误码（成功为 null）。 */
  code: string | null
  /** 提示信息。 */
  message: string
}

/** 批量取消响应（部分成功形态——逐 id 结果与提交顺序一致）。 */
export interface ReminderBatchCancelResponse {
  /** 提交总数。 */
  total: number
  /** 成功数量。 */
  succeeded: number
  /** 失败数量。 */
  failed: number
  /** 逐 id 结果列表。 */
  items: ReminderBatchCancelItem[]
}

/**
 * 通用响应契约（与后端 harness.core.dtos 对齐的 wire 形态）。
 *
 * 全部 API 响应为 ``ResultDTO`` 封装；列表端点经 ``PageResponse``
 * 游标分页（``next_cursor`` 非空表示还有下一页）。本文件只承载
 * 类型（Unit 文件）——结构化数据一律走这里的接口形态，禁止裸
 * 对象字面量承载结构化语义。
 */

/** 统一响应封装（wire 形态）。 */
export interface ResultDTO<T> {
  /** 是否成功（true=业务成功）。 */
  success: boolean
  /** 状态码字符串（成功恒 "200"；失败为后端错误码）。 */
  code: string
  /** 面向用户的提示信息（失败时为错误描述）。 */
  message: string
  /** 业务数据载荷。 */
  data: T | null
}

/** 游标分页响应（包裹在 ResultDTO.data 中）。 */
export interface PageResponse<T> {
  /** 当前页数据列表。 */
  items: T[]
  /** 总记录数（cursor 模式无精确值时为 -1）。 */
  total: number
  /** 当前页码（cursor 模式恒为 1）。 */
  page: number
  /** 每页条数。 */
  page_size: number
  /** 下一页游标（null = 已到末尾）。 */
  next_cursor: string | null
  /** 是否还有下一页。 */
  has_more: boolean
}

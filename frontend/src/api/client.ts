/**
 * API 客户端底座：axios 实例 + ResultDTO 解包 + 错误归一。
 *
 * 本工程约定：
 * - 基础地址取环境变量 ``VITE_API_BASE_URL``（缺省
 *   ``http://127.0.0.1:8000``），实际请求打向 ``${base}/api/v1``；
 * - 全部响应为 ``ResultDTO`` 封装——成功时解包 ``data`` 返回，失败
 *   （HTTP 4xx/5xx 或 ``success=false``）统一抛 ``ApiError``（携带
 *   后端错误码与面向用户的中文提示），调用方直接 toast 即可。
 */

import { AxiosError, create, type AxiosRequestConfig } from 'axios'
import type { ResultDTO } from '@/types/common'

/** 后端 API 基础地址（不含 /api/v1 前缀）。 */
export const API_BASE_URL: string = import.meta.env.VITE_API_BASE_URL || 'http://127.0.0.1:8000'

/** API 前缀（后端路由统一挂载点）。 */
export const API_PREFIX = '/api/v1'

/** axios 实例：统一 baseURL + 超时 + JSON 头。 */
export const api = create({
  baseURL: `${API_BASE_URL}${API_PREFIX}`,
  timeout: 15_000,
  headers: {
    'Content-Type': 'application/json',
  },
})

/** ApiError 构造选项。 */
export interface ApiErrorOptions {
  /** 后端错误码（缺省 UNKNOWN）。 */
  code?: string
  /** HTTP 状态码（网络错误时缺省）。 */
  status?: number
  /** 结构化上下文（如 400 校验错误的逐字段列表）。 */
  details?: unknown
}

/** 统一 API 错误：message 恒为面向用户的中文提示。 */
export class ApiError extends Error {
  /** 后端错误码。 */
  readonly code: string
  /** HTTP 状态码。 */
  readonly status?: number
  /** 结构化上下文。 */
  readonly details?: unknown

  constructor(message: string, options: ApiErrorOptions = {}) {
    super(message)
    this.name = 'ApiError'
    this.code = options.code ?? 'UNKNOWN'
    this.status = options.status
    this.details = options.details
  }
}

/** 按 HTTP 状态码给出兜底中文提示（无后端 message 时使用）。 */
function defaultMessageForStatus(status: number | undefined): string {
  switch (status) {
    case 400:
      return '请求参数错误'
    case 401:
      return '未授权的访问'
    case 403:
      return '没有执行该操作的权限'
    case 404:
      return '请求的资源不存在'
    case 409:
      return '操作与当前状态冲突'
    case 422:
      return '请求体校验失败'
    case 429:
      return '请求过于频繁，请稍后重试'
    case 500:
      return '服务内部错误'
    case 503:
      return '服务暂不可用'
    default:
      return '请求失败，请稍后重试'
  }
}

/**
 * 从任意抛出物解析出 ``ApiError``。
 *
 * 解析优先级：已是 ApiError → 原样；axios 错误 → 取响应体
 * ``ResultDTO.message/code``（无响应体时按网络错误/状态码兜底）；
 * 其余 → 通用未知错误。
 */
export function toApiError(error: unknown): ApiError {
  if (error instanceof ApiError) {
    return error
  }
  if (error instanceof AxiosError) {
    const status = error.response?.status
    if (error.code === 'ERR_NETWORK') {
      return new ApiError('网络错误，无法连接服务', { code: 'NETWORK_ERROR', status })
    }
    if (error.code === 'ECONNABORTED') {
      return new ApiError('请求超时，请稍后重试', { code: 'TIMEOUT', status })
    }
    const payload = error.response?.data as ResultDTO<unknown> | undefined
    const message = payload?.message || defaultMessageForStatus(status)
    return new ApiError(message, {
      code: payload?.code ?? String(status ?? 'HTTP_ERROR'),
      status,
      details: payload?.data,
    })
  }
  return new ApiError('未知错误', { code: 'UNKNOWN' })
}

/**
 * 统一请求入口：发送请求 → 校验 ResultDTO → 解包 data。
 *
 * @param config axios 请求配置（url 相对 ``/api/v1``）。
 * @returns 解包后的业务数据（T）。
 * @throws ApiError 失败形态（HTTP 错误 / success=false）。
 */
export async function request<T>(config: AxiosRequestConfig): Promise<T> {
  try {
    const response = await api.request<ResultDTO<T>>(config)
    const payload = response.data
    if (!payload.success) {
      throw new ApiError(payload.message || '请求失败', {
        code: payload.code,
        status: response.status,
        details: payload.data,
      })
    }
    return payload.data as T
  } catch (error) {
    throw toApiError(error)
  }
}

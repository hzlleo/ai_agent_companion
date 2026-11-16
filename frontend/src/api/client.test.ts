import { describe, expect, it } from 'vitest'
import { AxiosError } from 'axios'
import { http, HttpResponse } from 'msw'
import {
  API_BASE_URL,
  ApiError,
  api,
  request,
  toApiError,
} from '@/api/client'
import { BACKEND_ORIGIN, errorResult, okResult, server } from '@/test/server'
import type { ResultDTO } from '@/types/common'

/** 测试专用端点（避免与 personas 默认 handlers 冲突）。 */
const TEST_URL = `${BACKEND_ORIGIN}/__test`

describe('api client 基础配置', () => {
  it('默认基础地址与 /api/v1 前缀', () => {
    expect(API_BASE_URL).toBe('http://127.0.0.1:8000')
    expect(api.defaults.baseURL).toBe(`${API_BASE_URL}/api/v1`)
  })
})

describe('request ResultDTO 解包', () => {
  it('成功响应解包 data', async () => {
    server.use(
      http.get(TEST_URL, () =>
        HttpResponse.json(okResult({ value: 42 }, '成功')),
      ),
    )
    const data = await request<{ value: number }>({ method: 'GET', url: '/__test' })
    expect(data).toEqual({ value: 42 })
  })

  it('HTTP 200 但 success=false → 抛 ApiError（message/code 透传）', async () => {
    server.use(
      http.get(TEST_URL, () =>
        HttpResponse.json(errorResult('业务失败提示', 'BUSINESS_ERROR'), { status: 200 }),
      ),
    )
    const error = await request<unknown>({ method: 'GET', url: '/__test' }).catch((e: unknown) => e)
    expect(error).toBeInstanceOf(ApiError)
    const apiError = error as ApiError
    expect(apiError.message).toBe('业务失败提示')
    expect(apiError.code).toBe('BUSINESS_ERROR')
  })

  it('HTTP 400 校验错误 → message 透传 + details 携带逐字段列表', async () => {
    const validationDetails = { errors: [{ loc: ['body', 'name'], msg: '字段必填', type: 'missing' }] }
    server.use(
      http.post(TEST_URL, () =>
        HttpResponse.json(errorResult('请求参数校验失败', 'VALIDATION_ERROR', validationDetails), {
          status: 400,
        }),
      ),
    )
    const error = await request<unknown>({ method: 'POST', url: '/__test' }).catch((e: unknown) => e)
    const apiError = error as ApiError
    expect(apiError).toBeInstanceOf(ApiError)
    expect(apiError.status).toBe(400)
    expect(apiError.message).toBe('请求参数校验失败')
    expect(apiError.details).toEqual(validationDetails)
  })

  it('HTTP 404 → 后端 message 呈现', async () => {
    server.use(
      http.get(TEST_URL, () => HttpResponse.json(errorResult('人格不存在', 'NOT_FOUND'), { status: 404 })),
    )
    const error = await request<unknown>({ method: 'GET', url: '/__test' }).catch((e: unknown) => e)
    const apiError = error as ApiError
    expect(apiError.message).toBe('人格不存在')
    expect(apiError.code).toBe('NOT_FOUND')
    expect(apiError.status).toBe(404)
  })

  it('网络错误（无响应）→ 网络错误提示', async () => {
    server.use(
      http.get(TEST_URL, () => HttpResponse.error()),
    )
    const error = await request<unknown>({ method: 'GET', url: '/__test' }).catch((e: unknown) => e)
    const apiError = error as ApiError
    expect(apiError).toBeInstanceOf(ApiError)
    expect(apiError.message).toBe('网络错误，无法连接服务')
    expect(apiError.code).toBe('NETWORK_ERROR')
  })

  it('HTTP 500 无 message 字段 → 状态码兜底文案', async () => {
    server.use(
      http.get(TEST_URL, () =>
        HttpResponse.json({ success: false, code: '', message: '', data: null } satisfies ResultDTO<null>, {
          status: 500,
        }),
      ),
    )
    const error = await request<unknown>({ method: 'GET', url: '/__test' }).catch((e: unknown) => e)
    const apiError = error as ApiError
    expect(apiError.message).toBe('服务内部错误')
  })
})

describe('toApiError 归一', () => {
  it('ApiError 原样透传', () => {
    const original = new ApiError('已有错误', { code: 'X' })
    expect(toApiError(original)).toBe(original)
  })

  it('非 axios 非 ApiError → 未知错误', () => {
    const apiError = toApiError(new Error('随便什么'))
    expect(apiError.message).toBe('未知错误')
    expect(apiError.code).toBe('UNKNOWN')
  })

  it('超时中断（ECONNABORTED）→ 超时提示', () => {
    const axiosError = new AxiosError('timeout of 15000ms exceeded', 'ECONNABORTED')
    const apiError = toApiError(axiosError)
    expect(apiError.message).toBe('请求超时，请稍后重试')
    expect(apiError.code).toBe('TIMEOUT')
  })

  it('axios 错误携带 ResultDTO 响应体 → 提取 code/message/details', () => {
    const axiosError = new AxiosError('conflict', 'ERR_BAD_REQUEST')
    axiosError.response = {
      status: 409,
      data: errorResult('注册名已被占用', 'CONFLICT', { persona_id: 3 }),
      headers: {},
      config: {} as never,
      statusText: '',
    }
    const apiError = toApiError(axiosError)
    expect(apiError.message).toBe('注册名已被占用')
    expect(apiError.code).toBe('CONFLICT')
    expect(apiError.status).toBe(409)
    expect(apiError.details).toEqual({ persona_id: 3 })
  })

  it('axios 错误无响应 → 状态码兜底文案', () => {
    const axiosError = new AxiosError('bad response', 'ERR_BAD_RESPONSE')
    axiosError.response = {
      status: 503,
      data: undefined,
      headers: {},
      config: {} as never,
      statusText: '',
    }
    const apiError = toApiError(axiosError)
    expect(apiError.message).toBe('服务暂不可用')
  })

  it.each([
    [401, '未授权的访问'],
    [403, '没有执行该操作的权限'],
    [422, '请求体校验失败'],
    [429, '请求过于频繁，请稍后重试'],
    [418, '请求失败，请稍后重试'],
  ])('状态码 %d → 兜底文案「%s」', (status, expected) => {
    const axiosError = new AxiosError('fail', 'ERR_BAD_REQUEST')
    axiosError.response = {
      status,
      data: undefined,
      headers: {},
      config: {} as never,
      statusText: '',
    }
    expect(toApiError(axiosError).message).toBe(expected)
  })
})

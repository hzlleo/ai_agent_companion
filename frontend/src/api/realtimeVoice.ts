/**
 * 实时语音 WebSocket 地址构造。
 *
 * 浏览器 WebSocket 无法设置自定义请求头，owner token 与人格绑定一律经
 * 查询参数传递（后端 `app/api/realtime_voice` 同口径消费）：
 *   - `token`：已保存的管理面 token（无 token 时不带该参数）；
 *   - `persona_id`：可选人格绑定（缺省由后端解析当前激活人格）。
 *
 * 基础地址由 `VITE_API_BASE_URL`（与 REST 客户端同源）推导：http→ws、
 * https→wss；路径固定 `/api/v1/realtime/voice/ws`。
 */

import { getAdminToken } from '@/api/auth'
import { API_BASE_URL, API_PREFIX } from './client'

/** 实时语音 WS 路径（与后端路由同源常量）。 */
export const REALTIME_VOICE_WS_PATH = `${API_PREFIX}/realtime/voice/ws`

/**
 * 把 HTTP 基础地址转为 WS 基础地址。
 *
 * @param httpBase 后端 HTTP 基础地址（http / https）。
 * @returns 对应协议（ws / wss）的地址；未知协议按 ws 处理。
 */
export function toWebSocketOrigin(httpBase: string): string {
  const trimmed = httpBase.replace(/\/+$/, '')
  if (trimmed.startsWith('https://')) {
    return `wss://${trimmed.slice('https://'.length)}`
  }
  if (trimmed.startsWith('http://')) {
    return `ws://${trimmed.slice('http://'.length)}`
  }
  if (trimmed.startsWith('wss://') || trimmed.startsWith('ws://')) {
    return trimmed
  }
  return `ws://${trimmed}`
}

/**
 * 构造实时语音 WS 地址（含鉴权与人格查询参数）。
 *
 * @param personaId 可选人格主键（undefined / null 时不带该参数）。
 * @param options 覆盖项（测试注入 token 读取面 / 基础地址）。
 * @returns 完整 WS URL。
 */
export function buildRealtimeVoiceUrl(
  personaId?: number | null,
  options: { baseUrl?: string; token?: string | null } = {},
): string {
  const base = toWebSocketOrigin(options.baseUrl ?? API_BASE_URL)
  const url = new URL(`${base}${REALTIME_VOICE_WS_PATH}`)
  const token = options.token === undefined ? getAdminToken() : options.token
  if (token) {
    url.searchParams.set('token', token)
  }
  if (personaId !== undefined && personaId !== null) {
    url.searchParams.set('persona_id', String(personaId))
  }
  return url.toString()
}

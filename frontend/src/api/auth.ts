/**
 * 管理面 owner-token 存取 + 鉴权事件（401 → 令牌门）。
 *
 * 本工程约定：
 * - token 本值只存浏览器 localStorage（键 `aac_admin_token`），永不入
 *   URL / 日志 / 状态树；后端经 `Authorization: Bearer <token>` 校验
 *   （见 docs/backend/README.md 认证一节）；
 * - 鉴权需求事件为极简 pub/sub：api client 在任意请求收到 HTTP 401 时
 *   经 `notifyAuthRequired` 广播，App 壳订阅后挂载令牌门
 *   （AdminTokenScreen）；注销（清 token + 广播）复用同一事件面。
 */

/** token 持久化键（localStorage）。 */
const ADMIN_TOKEN_STORAGE_KEY = 'aac_admin_token'

/** 鉴权需求监听器（无参回调——事件不携带任何上下文）。 */
type AuthRequiredListener = () => void

/** 活跃监听器集合（模块级单例——App 壳外无其他订阅面）。 */
const listeners = new Set<AuthRequiredListener>()

/** 读取已保存的 owner token（未保存 / 空值归一空串 = 无 token）。 */
export function getAdminToken(): string {
  return localStorage.getItem(ADMIN_TOKEN_STORAGE_KEY)?.trim() ?? ''
}

/** 保存 owner token（trim 后落 localStorage；空串等价未保存）。 */
export function setAdminToken(token: string): void {
  localStorage.setItem(ADMIN_TOKEN_STORAGE_KEY, token.trim())
}

/** 清除 owner token（注销入口）。 */
export function clearAdminToken(): void {
  localStorage.removeItem(ADMIN_TOKEN_STORAGE_KEY)
}

/**
 * 订阅鉴权需求事件（401 / 注销时广播）。
 *
 * @param listener 无参回调（App 声明挂载令牌门）。
 * @returns 退订函数（组件卸载时调用——防泄漏）。
 */
export function onAuthRequired(listener: AuthRequiredListener): () => void {
  listeners.add(listener)
  return () => {
    listeners.delete(listener)
  }
}

/** 广播鉴权需求事件（api client 401 拦截 / 注销入口消费）。 */
export function notifyAuthRequired(): void {
  for (const listener of listeners) {
    listener()
  }
}

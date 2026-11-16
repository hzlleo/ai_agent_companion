/**
 * 时间格式化工具：后端时间字段统一为 epoch 毫秒。
 */

/** epoch 毫秒 → 本地化日期时间串（yyyy/MM/dd HH:mm:ss）。 */
export function formatEpochMs(ms: number): string {
  if (!Number.isFinite(ms)) {
    return '-'
  }
  return new Intl.DateTimeFormat('zh-CN', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  }).format(new Date(ms))
}

/** datetime-local 输入值（yyyy-MM-ddTHH:mm）→ epoch 毫秒（非法 / 空串 → null）。 */
export function datetimeLocalToEpochMs(value: string): number | null {
  if (value.trim() === '') {
    return null
  }
  const parsed = new Date(value).getTime()
  return Number.isNaN(parsed) ? null : parsed
}

/** epoch 毫秒 → datetime-local 输入值（yyyy-MM-ddTHH:mm；非法 → 空串）。 */
export function epochMsToDatetimeLocal(ms: number): string {
  if (!Number.isFinite(ms)) {
    return ''
  }
  const date = new Date(ms)
  const pad = (n: number): string => String(n).padStart(2, '0')
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`
}

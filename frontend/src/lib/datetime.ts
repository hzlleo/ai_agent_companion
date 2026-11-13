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

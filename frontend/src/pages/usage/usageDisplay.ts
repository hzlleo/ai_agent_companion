/**
 * 用量模块展示助手：用途 / 状态 / 聚合维度 / 桶宽中文标签 + 聚合行
 * 分组键展示。纯函数集（无 React 依赖）。
 */

import type {
  UsageGroupBy,
  UsagePurpose,
  UsageStatus,
  UsageTimeBucket,
} from '@/types/usage'
import { formatEpochMs } from '@/lib/datetime'

/** 调用用途 → 中文标签（过滤下拉 option 面——值域内键）。 */
export const PURPOSE_LABELS: Record<UsagePurpose, string> = {
  chat: '主对话',
  duty: '值班任务',
  dream: '梦境生成',
  look_at: '多模态查看',
  embed: '向量化',
  tts: '语音合成',
  asr: '语音识别',
  tool: '工具调用',
}

/** 用途键 → 中文标签查找面（string 索引——未知键经 noUncheckedIndexedAccess 判空回落）。 */
const PURPOSE_LABEL_LOOKUP: Record<string, string> = { ...PURPOSE_LABELS }

/** 调用结果 → 中文标签。 */
export const STATUS_LABELS: Record<UsageStatus, string> = {
  success: '成功',
  error: '失败',
}

/** 聚合维度 → 中文标签。 */
export const GROUP_BY_LABELS: Record<UsageGroupBy, string> = {
  model_entry: '按模型条目',
  purpose: '按用途',
  time_bucket: '按时间桶',
}

/** 时间桶宽 → 中文标签。 */
export const TIME_BUCKET_LABELS: Record<UsageTimeBucket, string> = {
  minute: '分钟',
  hour: '小时',
  day: '天',
}

/** 用途键 → 中文标签（未知值回落原文——跨值域防御展示）。 */
export function purposeLabel(group: string): string {
  return PURPOSE_LABEL_LOOKUP[group] ?? group
}

/** 聚合行分组键展示（time_bucket 行格式化桶起点；其余维度按需翻译）。 */
export function aggregateGroupDisplay(group: string, bucketMs: number | null): string {
  if (bucketMs !== null) {
    return formatEpochMs(bucketMs)
  }
  return group
}

/** token 计数展示（zh-CN 千分位）。 */
export function formatTokenCount(count: number): string {
  return count.toLocaleString('zh-CN')
}

/**
 * 记忆模块展示助手：值域中文标签 + 情景载荷摘要摊平 + 检索分数方向说明。
 *
 * 纯函数集（无 React 依赖）——供记忆页各子组件共用；标签表与后端
 * 值域一一对应，未知值回落原文（防御展示面，不改变契约）。
 */

import type {
  ChatTurnPayload,
  CuratedKind,
  CuratedStatus,
  DreamDecisionAction,
  DreamRunStatus,
  DutyReportPayload,
  EpisodeKind,
  EpisodeNotePayload,
  EpisodePayload,
  MemoryProvenance,
  MemorySearchMode,
  PlatformActionPayload,
} from '@/types/memory'
import type { BadgeProps } from '@/components/ui/Badge'

/** 情景事件类型 → 中文标签。 */
export const EPISODE_KIND_LABELS: Record<EpisodeKind, string> = {
  chat: '对话回合',
  duty: '值班任务',
  dream: '梦境产物',
  platform: '平台动作',
  system: '系统注记',
}

/** 来源可信度 → 中文标签。 */
export const PROVENANCE_LABELS: Record<MemoryProvenance, string> = {
  owner: '主人',
  agent: '智能体',
  untrusted: '不可信',
}

/** 来源可信度 → 徽章变体。 */
export const PROVENANCE_BADGE_VARIANTS: Record<MemoryProvenance, BadgeProps['variant']> = {
  owner: 'success',
  agent: 'info',
  untrusted: 'destructive',
}

/** 策展类型 → 中文标签。 */
export const CURATED_KIND_LABELS: Record<CuratedKind, string> = {
  memory: '蒸馏记忆',
  user_note: '主人备忘',
  relationship: '关系状态',
}

/** 策展状态 → 中文标签。 */
export const CURATED_STATUS_LABELS: Record<CuratedStatus, string> = {
  active: '现行',
  superseded: '已取代',
}

/** 策展状态 → 徽章变体。 */
export const CURATED_STATUS_BADGE_VARIANTS: Record<CuratedStatus, BadgeProps['variant']> = {
  active: 'success',
  superseded: 'secondary',
}

/** 梦境运行状态 → 中文标签。 */
export const DREAM_STATUS_LABELS: Record<DreamRunStatus, string> = {
  running: '运行中',
  success: '成功',
  failed: '失败',
}

/** 梦境运行状态 → 徽章变体。 */
export const DREAM_STATUS_BADGE_VARIANTS: Record<DreamRunStatus, BadgeProps['variant']> = {
  running: 'info',
  success: 'success',
  failed: 'destructive',
}

/** 梦境决策动作 → 中文标签。 */
export const DREAM_ACTION_LABELS: Record<DreamDecisionAction, string> = {
  promoted: '晋升',
  skipped: '跳过',
  merged: '并入',
}

/** 梦境决策动作 → 徽章变体。 */
export const DREAM_ACTION_BADGE_VARIANTS: Record<DreamDecisionAction, BadgeProps['variant']> = {
  promoted: 'success',
  skipped: 'secondary',
  merged: 'info',
}

/** 检索模式 → 中文标签。 */
export const SEARCH_MODE_LABELS: Record<MemorySearchMode, string> = {
  dense: '稠密向量',
  keyword: '全文关键词',
  hybrid: '混合检索',
}

/** 检索模式 → 分数方向说明（dense/hybrid 为 cosine 距离，keyword 为相关度）。 */
export const SEARCH_SCORE_DIRECTION_HINTS: Record<MemorySearchMode, string> = {
  dense: '分数为 cosine 距离（越小越相关）',
  keyword: '分数为全文相关度（越大越相关）',
  hybrid: '分数为 cosine 距离（越小越相关）',
}

/** 长文本截断（展示面单行摘要用）。 */
export function truncateText(text: string, maxLength = 48): string {
  return text.length > maxLength ? `${text.slice(0, maxLength)}…` : text
}

function isChatTurnPayload(content: EpisodePayload): content is ChatTurnPayload {
  return 'user_content' in content
}

function isDutyReportPayload(content: EpisodePayload): content is DutyReportPayload {
  return 'title' in content
}

function isPlatformActionPayload(content: EpisodePayload): content is PlatformActionPayload {
  return 'action' in content
}

function isEpisodeNotePayload(content: EpisodePayload): content is EpisodeNotePayload {
  return 'text' in content
}

/**
 * 情景载荷摘要（按 kind 摊平为单行中文摘要）。
 *
 * kind 与载荷形态不符（异常 wire）时回落 JSON 串——展示面防御，
 * 不抛错。
 */
export function episodePayloadSummary(kind: EpisodeKind, content: EpisodePayload): string {
  if (kind === 'chat' && isChatTurnPayload(content)) {
    const user = content.user_content === '' ? '（无用户侧文本）' : content.user_content
    const assistant = content.assistant_content === '' ? '（无回复）' : content.assistant_content
    return truncateText(`用户：${user} / 人格：${assistant}`)
  }
  if (kind === 'duty' && isDutyReportPayload(content)) {
    const title = content.title === '' ? '（无标题）' : content.title
    const summary = content.summary === '' ? '（无摘要）' : content.summary
    return truncateText(`${title} — ${summary}`)
  }
  if (kind === 'platform' && isPlatformActionPayload(content)) {
    const action = content.action === '' ? '（无动作标识）' : content.action
    return truncateText(`${action}：${content.summary}`)
  }
  if ((kind === 'dream' || kind === 'system') && isEpisodeNotePayload(content)) {
    return truncateText(content.text === '' ? '（无正文）' : content.text)
  }
  return truncateText(JSON.stringify(content))
}

/** 情景载荷的 JSON 预填文本（注入对话框按 kind 提供示例骨架）。 */
export const EPISODE_PAYLOAD_TEMPLATES: Record<EpisodeKind, string> = {
  chat: JSON.stringify({ user_content: '', assistant_content: '', session_id: null }, null, 2),
  duty: JSON.stringify({ title: '', summary: '', details: {} }, null, 2),
  dream: JSON.stringify({ text: '', metadata: {} }, null, 2),
  platform: JSON.stringify({ action: '', summary: '', metadata: {} }, null, 2),
  system: JSON.stringify({ text: '', metadata: {} }, null, 2),
}

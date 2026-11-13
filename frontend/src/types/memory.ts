/**
 * memory 域类型契约（与后端 harness.memory 契约对齐的 wire 形态）。
 *
 * 覆盖面：情景事件（episodes——载荷按 kind 判别）/ 策展记忆（curated
 * ——supersede 语义）/ 会话（sessions——chat 持久化面）/ 记忆检索
 * （search——dense / keyword / hybrid 三模式）/ 夜间梦境（dream——
 * 同步触发 + 运行日志）。本文件只承载类型（Unit 文件）——结构化数据
 * 一律走这里的接口形态，禁止裸对象字面量承载结构化语义。
 */

import type { BatchDeleteResult } from './common'

// ---------------------------------------------------------------------------
// 值域
// ---------------------------------------------------------------------------

/** 情景事件类型（ff_memory_episodes.kind）。 */
export type EpisodeKind = 'chat' | 'duty' | 'dream' | 'platform' | 'system'

/** 记忆来源可信度（全记忆层统一 taxonomy）。 */
export type MemoryProvenance = 'owner' | 'agent' | 'untrusted'

/** 策展记忆类型（ff_curated_memory.kind）。 */
export type CuratedKind = 'memory' | 'user_note' | 'relationship'

/** 策展记忆状态（同 (persona, kind, topic) 至多一条 active）。 */
export type CuratedStatus = 'active' | 'superseded'

/** 会话消息角色。 */
export type ChatMessageRole = 'user' | 'assistant' | 'system'

/** 记忆检索模式（score 方向随模式——见 MemorySearchResult）。 */
export type MemorySearchMode = 'dense' | 'keyword' | 'hybrid'

/** 检索索引引用类型（回源定位）。 */
export type MemoryRefType = 'episode' | 'curated'

/** 梦境运行状态（failed 为业务结果——响应恒 200）。 */
export type DreamRunStatus = 'running' | 'success' | 'failed'

/** 梦境逐事件决策动作。 */
export type DreamDecisionAction = 'promoted' | 'skipped' | 'merged'

// ---------------------------------------------------------------------------
// 情景事件载荷（content JSON——按 kind 判别）
// ---------------------------------------------------------------------------

/** chat 回合载荷（kind=chat）。 */
export interface ChatTurnPayload {
  /** 用户侧文本（单向回合可为空串）。 */
  user_content: string
  /** 人格回复文本。 */
  assistant_content: string
  /** 关联会话主键（无关联为 null）。 */
  session_id: number | null
}

/** duty 报告载荷（kind=duty）。 */
export interface DutyReportPayload {
  /** 报告标题。 */
  title: string
  /** 报告摘要。 */
  summary: string
  /** 结构化明细（自由 JSON）。 */
  details: Record<string, unknown>
}

/** platform 动作载荷（kind=platform）。 */
export interface PlatformActionPayload {
  /** 动作标识。 */
  action: string
  /** 动作摘要。 */
  summary: string
  /** 平台侧附加信息（自由 JSON）。 */
  metadata: Record<string, unknown>
}

/** 通用注记载荷（kind=dream / system）。 */
export interface EpisodeNotePayload {
  /** 注记正文。 */
  text: string
  /** 附加信息（自由 JSON）。 */
  metadata: Record<string, unknown>
}

/** 情景事件载荷联合（content 字段的类型面——按 kind 判别解析）。 */
export type EpisodePayload =
  | ChatTurnPayload
  | DutyReportPayload
  | PlatformActionPayload
  | EpisodeNotePayload

// ---------------------------------------------------------------------------
// 情景事件（episodic——append-only 事件日志）
// ---------------------------------------------------------------------------

/** 情景事件记录（ORM 行归一）。 */
export interface EpisodeRecord {
  /** 事件主键。 */
  id: number
  /** 所属人格 ID。 */
  persona_id: string
  /** 事件类型。 */
  kind: EpisodeKind
  /** 事件来源标识。 */
  source: string
  /** 事件载荷（按 kind 判别）。 */
  content: EpisodePayload
  /** 重要度（1-10）。 */
  importance: number
  /** 来源可信度。 */
  provenance: MemoryProvenance
  /** 内容指纹（去重键，16 hex）。 */
  content_hash: string
  /** 事件发生时间（epoch 毫秒）。 */
  ts_ms: number
  /** 落库时间（epoch 毫秒）。 */
  created_at: number
  /** 行更新时间（epoch 毫秒；append-only 下与创建同值）。 */
  updated_at: number
}

/** 情景事件创建请求（管理面手工注入；载荷按 kind 判别校验）。 */
export interface EpisodeCreateRequest {
  /** 事件类型。 */
  kind: EpisodeKind
  /** 事件来源标识（1-128 字符）。 */
  source: string
  /** 事件载荷（按 kind 判别）。 */
  content: EpisodePayload
  /** 重要度（1-10，默认 5）。 */
  importance: number
  /** 来源可信度（管理面默认 owner）。 */
  provenance: MemoryProvenance
  /** 事件发生时间（epoch 毫秒；null = 落库时取当前时间）。 */
  ts_ms: number | null
}

/** 情景事件列表查询参数（kind / provenance / 时间范围过滤 + 游标分页）。 */
export interface EpisodeListParams {
  /** 事件类型过滤（undefined = 全部）。 */
  kind?: EpisodeKind
  /** 来源可信度过滤（undefined = 全部）。 */
  provenance?: MemoryProvenance
  /** 时间范围起点（epoch 毫秒，含）。 */
  start_ms?: number
  /** 时间范围终点（epoch 毫秒，不含）。 */
  end_ms?: number
  /** 分页游标（首页 undefined）。 */
  cursor?: string
  /** 页大小（1-200，默认 20）。 */
  limit?: number
}

/** 情景事件批量删除响应（部分成功形态——与通用批量删除契约同构）。 */
export type EpisodeBatchDeleteResponse = BatchDeleteResult

// ---------------------------------------------------------------------------
// 策展记忆（curated——supersede 语义）
// ---------------------------------------------------------------------------

/** 策展记忆记录（ORM 行归一）。 */
export interface CuratedRecord {
  /** 记忆主键。 */
  id: number
  /** 所属人格 ID。 */
  persona_id: string
  /** 策展类型。 */
  kind: CuratedKind
  /** 主题键（同 (persona, kind, topic) 单 active）。 */
  topic: string
  /** 记忆正文。 */
  content: string
  /** 重要度（1-10）。 */
  importance: number
  /** 来源可信度（owner/agent——untrusted 服务层拒绝）。 */
  provenance: MemoryProvenance
  /** 状态（active/superseded）。 */
  status: CuratedStatus
  /** 取代者行主键（active 行为 null）。 */
  superseded_by_id: number | null
  /** 链内版本号（首版 1）。 */
  version: number
  /** 落库时间（epoch 毫秒）。 */
  created_at: number
  /** 行更新时间（epoch 毫秒）。 */
  updated_at: number
}

/** 策展记忆 upsert 请求（supersede 语义——同 (persona, kind, topic) 单 active）。 */
export interface CuratedUpsertRequest {
  /** 策展类型。 */
  kind: CuratedKind
  /** 主题键（1-255 字符）。 */
  topic: string
  /** 记忆正文（非空）。 */
  content: string
  /** 重要度（1-10，默认 5）。 */
  importance: number
  /** 来源可信度（默认 owner；untrusted 服务层拒绝）。 */
  provenance: MemoryProvenance
}

/** 策展记忆列表查询参数（kind / topic / status 过滤 + 游标分页）。 */
export interface CuratedListParams {
  /** 策展类型过滤（undefined = 全部）。 */
  kind?: CuratedKind
  /** 主题键精确过滤（undefined = 全部）。 */
  topic?: string
  /** 状态过滤（undefined = 全部；active = 现行版本面）。 */
  status?: CuratedStatus
  /** 分页游标（首页 undefined）。 */
  cursor?: string
  /** 页大小（1-200，默认 20）。 */
  limit?: number
}

// ---------------------------------------------------------------------------
// 会话（working——chat 会话持久化面）
// ---------------------------------------------------------------------------

/** 会话摘要（列表面）。 */
export interface ChatSessionSummary {
  /** 会话主键。 */
  id: number
  /** 所属人格 ID。 */
  persona_id: string
  /** 会话标题。 */
  title: string
  /** 消息计数。 */
  message_count: number
  /** 创建时间（epoch 毫秒）。 */
  created_at: number
  /** 最后活跃时间（epoch 毫秒）。 */
  last_active_at: number
}

/** 会话消息（按 session 归属，seq 升序）。 */
export interface ChatMessage {
  /** 消息主键。 */
  id: number
  /** 所属会话主键。 */
  session_id: number
  /** 会话内序号（从 1 递增）。 */
  seq: number
  /** 消息角色。 */
  role: ChatMessageRole
  /** 消息文本。 */
  content: string
  /** 消息时间（epoch 毫秒）。 */
  ts_ms: number
}

/** 会话详情（摘要 + 全量消息 seq 升序）。 */
export interface ChatSessionDetail {
  /** 会话主键。 */
  id: number
  /** 所属人格 ID。 */
  persona_id: string
  /** 会话标题。 */
  title: string
  /** 消息计数。 */
  message_count: number
  /** 创建时间（epoch 毫秒）。 */
  created_at: number
  /** 最后活跃时间（epoch 毫秒）。 */
  last_active_at: number
  /** 消息列表（seq 升序——时间序回放形态）。 */
  messages: ChatMessage[]
}

// ---------------------------------------------------------------------------
// 记忆检索（retrieval——dense / keyword / hybrid）
// ---------------------------------------------------------------------------

/** 记忆检索请求体（POST /memories/search；persona_id 缺省 = 当前激活人格）。 */
export interface MemorySearchRequest {
  /** 目标人格主键（undefined = 当前激活人格；无激活则 404）。 */
  persona_id?: number
  /** 检索文本（1-512 字符）。 */
  query: string
  /** 检索模式（默认 dense）。 */
  mode: MemorySearchMode
  /** 记忆类型过滤（undefined = 全部；最多 10 个）。 */
  kinds?: string[]
  /** 来源可信度过滤（undefined = 全部）。 */
  provenance?: MemoryProvenance
  /** 时间范围起点（epoch 毫秒，含；检索层闭区间语义）。 */
  start_ms?: number
  /** 时间范围终点（epoch 毫秒，含）。 */
  end_ms?: number
  /** 返回条数上限（1-50，默认 10）。 */
  limit: number
}

/** 记忆检索结果单条命中。 */
export interface MemorySearchResult {
  /** 引用类型（episode / curated——回源定位）。 */
  ref_type: MemoryRefType
  /** 引用行主键（回源定位）。 */
  ref_id: number
  /** 记忆类型值（episode / curated 两层词表）。 */
  kind: string
  /** 来源可信度。 */
  provenance: MemoryProvenance
  /** 事件时间（epoch 毫秒）。 */
  ts_ms: number
  /** 索引正文（命中片段展示面）。 */
  content: string
  /** 相关度分（dense/hybrid = cosine 距离越小越相关；keyword = 相关度越大越相关）。 */
  score: number
}

// ---------------------------------------------------------------------------
// 夜间梦境（dreaming——同步触发 + 运行日志）
// ---------------------------------------------------------------------------

/** 梦境任务构造级参数（DreamJobConfig 全量形态）。 */
export interface DreamJobConfig {
  /** 候选窗口宽度（小时；窗口 [now - window_hours, now) 左闭右开）。 */
  window_hours: number
  /** 时间衰减半衰期（天——指数衰减语义）。 */
  recency_half_life_days: number
  /** 梦境分晋级阈值（score >= min_score 才入 LLM 候选）。 */
  min_score: number
  /** 单次运行候选事件上限（按分数降序截断）。 */
  max_candidates: number
  /** 主题相关度判定的检索 top-N。 */
  topic_relevance_limit: number
  /** 主题相关度加成系数（命中事件得分乘 1 + boost）。 */
  boost: number
  /** 日记生成开关（关闭则跳过日记阶段）。 */
  diary_enabled: boolean
  /** 日记事件重要度（kind=dream 落库）。 */
  diary_importance: number
}

/** 梦境运行统计（计数口径与决策动作对齐）。 */
export interface DreamRunStats {
  /** 可信候选事件数（untrusted 在打分前硬排除）。 */
  candidates: number
  /** 过门事件数（分数达标 + 未被既往晋升排除 + 上限截断后）。 */
  gated: number
  /** 晋升事件数（新起链主题）。 */
  promoted: number
  /** 并入事件数（推进既有 active 主题链）。 */
  superseded: number
  /** untrusted 硬排除计数。 */
  skipped_untrusted: number
  /** LLM 提案丢弃计数（逐条丢弃，不整体失败）。 */
  failed: number
}

/** 梦境逐事件决策记录。 */
export interface DreamDecision {
  /** 情景事件主键（决策主体）。 */
  episode_id: number
  /** 关联策展主题键（纯跳过决策为空串）。 */
  topic: string
  /** 关联策展类型（纯跳过决策为 null）。 */
  kind: CuratedKind | null
  /** 决策动作（promoted / skipped / merged）。 */
  action: DreamDecisionAction
  /** 人读决策理由。 */
  reason: string
}

/** 梦境运行记录（触发响应与运行日志共用读取面）。 */
export interface DreamRunRecord {
  /** 运行主键。 */
  id: number
  /** 所属人格 ID。 */
  persona_id: string
  /** 运行状态（running / success / failed）。 */
  status: DreamRunStatus
  /** 开始时间（epoch 毫秒）。 */
  started_ms: number
  /** 结束时间（epoch 毫秒；运行中为 null）。 */
  finished_ms: number | null
  /** 运行统计。 */
  stats: DreamRunStats
  /** 逐事件决策列表。 */
  decisions: DreamDecision[]
  /** 日记事件主键（未生成 / 失败为 null）。 */
  diary_episode_id: number | null
  /** 失败原因（成功为 null）。 */
  error: string | null
  /** 落库时间（epoch 毫秒）。 */
  created_at: number
  /** 行更新时间（epoch 毫秒）。 */
  updated_at: number
}

/** 梦境手工触发请求体（cfg 为嵌套部分覆写——仅出现字段生效）。 */
export interface DreamTriggerRequest {
  /** 目标人格主键（undefined = 当前激活人格）。 */
  persona_id?: number
  /** 任务参数覆写（undefined = 全默认）。 */
  cfg?: Partial<DreamJobConfig>
}

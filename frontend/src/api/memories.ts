/**
 * memories 端点层：后端 16 个路由的 typed 封装。
 *
 * 路由面（全部经 ResultDTO 解包，失败抛 ApiError）：
 *   - GET    /memories/episodes                   游标分页列表（kind/provenance/时间范围过滤）
 *   - POST   /memories/episodes                   手工注入（provenance 缺省 owner）
 *   - POST   /memories/episodes/batch-delete      批量删除（部分成功形态）
 *   - GET    /memories/episodes/{id}              读取
 *   - DELETE /memories/episodes/{id}              删除（append-only 的管理面出口）
 *   - GET    /memories/curated                    游标分页列表（kind/topic/status 过滤）
 *   - POST   /memories/curated/upsert             upsert（supersede 语义）
 *   - GET    /memories/curated/{id}/history       历史链读取（版本升序）
 *   - DELETE /memories/curated/{id}               删除
 *   - GET    /memories/sessions                   会话游标分页列表
 *   - GET    /memories/sessions/{id}              会话详情（含全量消息）
 *   - DELETE /memories/sessions/{id}              删除（消息同事务清理）
 *   - POST   /memories/search                     记忆检索（dense/keyword/hybrid）
 *   - POST   /memories/dream/trigger              手工触发梦境整理（同步运行）
 *   - GET    /memories/dream/runs                 运行日志游标分页
 *   - GET    /memories/dream/runs/{id}            运行详情（含逐事件决策）
 *
 * 人格作用域（本工程约定）：admin 路由无人格上下文语义——persona_id
 * 为显式查询参数（undefined = 当前激活人格，由后端解析）。
 */

import { request } from './client'
import type { PageResponse } from '@/types/common'
import type {
  ChatSessionDetail,
  ChatSessionSummary,
  CuratedListParams,
  CuratedRecord,
  CuratedUpsertRequest,
  DreamRunRecord,
  DreamTriggerRequest,
  EpisodeBatchDeleteResponse,
  EpisodeCreateRequest,
  EpisodeListParams,
  EpisodeRecord,
  MemorySearchRequest,
  MemorySearchResult,
} from '@/types/memory'

/** persona_id 查询参数形态（undefined 时不出现在查询串）。 */
function personaScope(personaId: number | undefined): { persona_id: number } | undefined {
  return personaId === undefined ? undefined : { persona_id: personaId }
}

/** GET /memories/episodes —— 情景事件游标分页（kind/provenance/时间范围过滤）。 */
export async function listEpisodes(
  personaId: number | undefined,
  params: EpisodeListParams,
): Promise<PageResponse<EpisodeRecord>> {
  return request<PageResponse<EpisodeRecord>>({
    method: 'GET',
    url: '/memories/episodes',
    params: { ...personaScope(personaId), ...params },
  })
}

/** POST /memories/episodes —— 手工注入情景事件（载荷按 kind 判别校验）。 */
export async function createEpisode(
  personaId: number | undefined,
  payload: EpisodeCreateRequest,
): Promise<EpisodeRecord> {
  return request<EpisodeRecord>({
    method: 'POST',
    url: '/memories/episodes',
    params: personaScope(personaId),
    data: payload,
  })
}

/** POST /memories/episodes/batch-delete —— 批量删除（逐 id 独立事务，部分成功）。 */
export async function batchDeleteEpisodes(
  personaId: number | undefined,
  ids: number[],
): Promise<EpisodeBatchDeleteResponse> {
  return request<EpisodeBatchDeleteResponse>({
    method: 'POST',
    url: '/memories/episodes/batch-delete',
    params: personaScope(personaId),
    data: { ids },
  })
}

/** GET /memories/episodes/{id} —— 按主键读取（人格作用域外 / 缺失均 404）。 */
export async function getEpisode(
  personaId: number | undefined,
  episodeId: number,
): Promise<EpisodeRecord> {
  return request<EpisodeRecord>({
    method: 'GET',
    url: `/memories/episodes/${episodeId}`,
    params: personaScope(personaId),
  })
}

/** DELETE /memories/episodes/{id} —— 删除单条（append-only 的管理面显式出口）。 */
export async function deleteEpisode(
  personaId: number | undefined,
  episodeId: number,
): Promise<void> {
  await request<null>({
    method: 'DELETE',
    url: `/memories/episodes/${episodeId}`,
    params: personaScope(personaId),
  })
}

/** GET /memories/curated —— 策展记忆游标分页（kind/topic/status 过滤）。 */
export async function listCurated(
  personaId: number | undefined,
  params: CuratedListParams,
): Promise<PageResponse<CuratedRecord>> {
  return request<PageResponse<CuratedRecord>>({
    method: 'GET',
    url: '/memories/curated',
    params: { ...personaScope(personaId), ...params },
  })
}

/** POST /memories/curated/upsert —— upsert（supersede 语义；untrusted 422 拒绝）。 */
export async function upsertCurated(
  personaId: number | undefined,
  payload: CuratedUpsertRequest,
): Promise<CuratedRecord> {
  return request<CuratedRecord>({
    method: 'POST',
    url: '/memories/curated/upsert',
    params: personaScope(personaId),
    data: payload,
  })
}

/** GET /memories/curated/{id}/history —— 历史链读取（supersede 链全量，版本升序）。 */
export async function getCuratedHistory(
  personaId: number | undefined,
  memoryId: number,
): Promise<CuratedRecord[]> {
  return request<CuratedRecord[]>({
    method: 'GET',
    url: `/memories/curated/${memoryId}/history`,
    params: personaScope(personaId),
  })
}

/** DELETE /memories/curated/{id} —— 删除单条策展记忆。 */
export async function deleteCurated(
  personaId: number | undefined,
  memoryId: number,
): Promise<void> {
  await request<null>({
    method: 'DELETE',
    url: `/memories/curated/${memoryId}`,
    params: personaScope(personaId),
  })
}

/** GET /memories/sessions —— 会话游标分页（id 降序，新会话在前）。 */
export async function listSessions(
  personaId: number | undefined,
  params: { cursor?: string; limit?: number },
): Promise<PageResponse<ChatSessionSummary>> {
  return request<PageResponse<ChatSessionSummary>>({
    method: 'GET',
    url: '/memories/sessions',
    params: { ...personaScope(personaId), ...params },
  })
}

/** GET /memories/sessions/{id} —— 会话详情（摘要 + 全量消息 seq 升序）。 */
export async function getSession(
  personaId: number | undefined,
  sessionId: number,
): Promise<ChatSessionDetail> {
  return request<ChatSessionDetail>({
    method: 'GET',
    url: `/memories/sessions/${sessionId}`,
    params: personaScope(personaId),
  })
}

/** DELETE /memories/sessions/{id} —— 删除会话（消息同事务清理）。 */
export async function deleteSession(
  personaId: number | undefined,
  sessionId: number,
): Promise<void> {
  await request<null>({
    method: 'DELETE',
    url: `/memories/sessions/${sessionId}`,
    params: personaScope(personaId),
  })
}

/** POST /memories/search —— 记忆检索（dense/keyword/hybrid；回退存储拒绝向量模式 422）。 */
export async function searchMemories(payload: MemorySearchRequest): Promise<MemorySearchResult[]> {
  return request<MemorySearchResult[]>({
    method: 'POST',
    url: '/memories/search',
    data: payload,
  })
}

/** POST /memories/dream/trigger —— 手工触发梦境整理（同步运行——响应即运行记录）。 */
export async function triggerDream(payload: DreamTriggerRequest): Promise<DreamRunRecord> {
  return request<DreamRunRecord>({
    method: 'POST',
    url: '/memories/dream/trigger',
    data: payload,
  })
}

/** GET /memories/dream/runs —— 梦境运行日志游标分页（id 降序）。 */
export async function listDreamRuns(
  personaId: number | undefined,
  params: { cursor?: string; limit?: number },
): Promise<PageResponse<DreamRunRecord>> {
  return request<PageResponse<DreamRunRecord>>({
    method: 'GET',
    url: '/memories/dream/runs',
    params: { ...personaScope(personaId), ...params },
  })
}

/** GET /memories/dream/runs/{id} —— 运行详情（含逐事件决策明细）。 */
export async function getDreamRun(
  personaId: number | undefined,
  runId: number,
): Promise<DreamRunRecord> {
  return request<DreamRunRecord>({
    method: 'GET',
    url: `/memories/dream/runs/${runId}`,
    params: personaScope(personaId),
  })
}

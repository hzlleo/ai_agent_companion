/**
 * 共享 msw 服务器：setupServer 单例 + personas 域默认 handlers。
 *
 * 约定（本工程）：
 * - 默认 handlers 覆盖 personas 全部路由的成功形态（真实 wire 契约），
 *   各测试经 ``server.use(http...)`` 覆写错误路径（400/404/409 等）；
 * - onUnhandledRequest: 'error'（setup.ts 注册）——任何未 mock 的请求
 *   直接失败，测试永不依赖真实后端。
 */

import { setupServer } from 'msw/node'
import { http, HttpResponse } from 'msw'
import { API_BASE_URL, API_PREFIX } from '@/api/client'
import type { ResultDTO } from '@/types/common'
import type {
  PersonaRecord,
  PersonaState,
} from '@/types/persona'
import type {
  ChatSessionDetail,
  ChatSessionSummary,
  CuratedRecord,
  DreamRunRecord,
  EpisodeRecord,
  MemorySearchResult,
} from '@/types/memory'
import type { UsageAggregate, UsageRecord, UsageSummary } from '@/types/usage'
import type { ModelEntryMasked, RegistryState, RoleBinding } from '@/types/model-registry'

/** 后端 URL 前缀（handlers 统一经此拼接，与 api client 同源）。 */
export const BACKEND_ORIGIN = `${API_BASE_URL}${API_PREFIX}`

/** 构造成功 ResultDTO。 */
export function okResult<T>(data: T, message = '成功'): ResultDTO<T> {
  return { success: true, code: '200', message, data }
}

/** 构造失败 ResultDTO（HTTP 状态由 handler 指定）。 */
export function errorResult(message: string, code: string, details: unknown = null): ResultDTO<unknown> {
  return { success: false, code, message, data: details }
}

/** 测试用人格卡片（wire 形态）。 */
export const mockCard = {
  name: '小助手',
  description: '一个温柔的陪伴型人格',
  personality: '温柔、耐心、共情',
  scenario: '深夜陪伴对话',
  first_mes: '你好呀，今天过得怎么样？',
  mes_example: '<START>\nuser: 我好累\nassistant: 辛苦了，想聊聊吗？',
  system_prompt: '',
  post_history_instructions: '',
  character_book: null,
}

/** 测试用人格记录。 */
export function mockPersona(overrides: Partial<PersonaRecord> = {}): PersonaRecord {
  return {
    id: 1,
    name: 'companion-1',
    display_name: '小助手',
    status: 'active',
    is_active: true,
    card: { ...mockCard },
    created_at: 1731000000000,
    updated_at: 1731000000000,
    ...overrides,
  }
}

/** 测试用状态层。 */
export function mockState(overrides: Partial<PersonaState> = {}): PersonaState {
  return {
    persona_id: 1,
    layer: 'M',
    state: { mood: 'calm', affinity: 0.6 },
    created_at: 1731000000000,
    updated_at: 1731000000000,
    ...overrides,
  }
}

/** personas 域默认 handlers（成功形态）。 */
export const personaHandlers = [  // 游标分页列表：首页 2 条 + next_cursor；带 cursor 时返回末页。
  http.get(`${BACKEND_ORIGIN}/personas`, ({ request }) => {
    const url = new URL(request.url)
    const cursor = url.searchParams.get('cursor')
    const status = url.searchParams.get('status')
    const all = [
      mockPersona({ id: 2, name: 'companion-2', is_active: false }),
      mockPersona({ id: 1, name: 'companion-1', is_active: true }),
      mockPersona({ id: 3, name: 'old-friend', status: 'disabled', is_active: false }),
    ]
    const filtered = status === null ? all : all.filter((p) => p.status === status)
    if (cursor === null) {
      return HttpResponse.json(
        okResult({
          items: filtered.slice(0, 2),
          total: -1,
          page: 1,
          page_size: 20,
          next_cursor: 'cursor-page-2',
          has_more: true,
        }),
      )
    }
    return HttpResponse.json(
      okResult({
        items: [filtered[2] ?? mockPersona({ id: 3, name: 'old-friend', status: 'disabled', is_active: false })],
        total: -1,
        page: 1,
        page_size: 20,
        next_cursor: null,
        has_more: false,
      }),
    )
  }),
  // 创建。
  http.post(`${BACKEND_ORIGIN}/personas`, async ({ request }) => {
    const body = (await request.json()) as { name: string }
    return HttpResponse.json(okResult(mockPersona({ id: 9, name: body.name }), '人格已创建'))
  }),
  // 批量删除（部分成功形态：id=1 激活守卫失败）。
  http.post(`${BACKEND_ORIGIN}/personas/batch-delete`, async ({ request }) => {
    const body = (await request.json()) as { ids: number[] }
    const items = body.ids.map((id) =>
      id === 1
        ? { id, success: false, code: 'CONFLICT', message: '当前激活人格禁止删除，请先停用' }
        : { id, success: true, code: null, message: '已删除' },
    )
    const succeeded = items.filter((item) => item.success).length
    return HttpResponse.json(
      okResult(
        {
          total: body.ids.length,
          succeeded,
          failed: body.ids.length - succeeded,
          items,
        },
        `批量删除完成（成功 ${succeeded} / 失败 ${body.ids.length - succeeded}）`,
      ),
    )
  }),
  // 读取。
  http.get(`${BACKEND_ORIGIN}/personas/:personaId`, ({ params }) => {
    const id = Number(params.personaId)
    if (id === 404) {
      return HttpResponse.json(errorResult('人格不存在', 'NOT_FOUND'), { status: 404 })
    }
    return HttpResponse.json(okResult(mockPersona({ id })))
  }),
  // 部分更新。
  http.put(`${BACKEND_ORIGIN}/personas/:personaId`, async ({ request }) => {
    const body = (await request.json()) as { name?: string }
    return HttpResponse.json(okResult(mockPersona({ name: body.name ?? 'companion-1' }), '人格已更新'))
  }),
  // 删除：id=1 为激活人格 → 409 激活守卫。
  http.delete(`${BACKEND_ORIGIN}/personas/:personaId`, ({ params }) => {
    const id = Number(params.personaId)
    if (id === 1) {
      return HttpResponse.json(errorResult('当前激活人格禁止删除，请先停用', 'CONFLICT'), {
        status: 409,
      })
    }
    if (id === 404) {
      return HttpResponse.json(errorResult('人格不存在', 'NOT_FOUND'), { status: 404 })
    }
    return HttpResponse.json(okResult(null, '人格已删除'))
  }),
  // 复制。
  http.post(`${BACKEND_ORIGIN}/personas/:personaId/copy`, async ({ request }) => {
    const body = (await request.json()) as { new_name: string }
    if (body.new_name === 'companion-1') {
      return HttpResponse.json(errorResult('注册名已被占用', 'CONFLICT'), { status: 409 })
    }
    return HttpResponse.json(
      okResult(mockPersona({ id: 10, name: body.new_name, is_active: false, status: 'disabled' }), '人格已复制'),
    )
  }),
  // 激活：id=3（old-friend，停用状态）→ 409。
  http.post(`${BACKEND_ORIGIN}/personas/:personaId/activate`, ({ params }) => {
    const id = Number(params.personaId)
    if (id === 3) {
      return HttpResponse.json(errorResult('停用状态的人格不可激活', 'CONFLICT'), { status: 409 })
    }
    return HttpResponse.json(okResult(mockPersona({ id, is_active: true }), '人格已激活（单激活切换）'))
  }),
  // 停用激活。
  http.post(`${BACKEND_ORIGIN}/personas/:personaId/deactivate`, ({ params }) => {
    const id = Number(params.personaId)
    return HttpResponse.json(okResult(mockPersona({ id, is_active: false }), '人格激活位已停用'))
  }),
  // 卡片读取。
  http.get(`${BACKEND_ORIGIN}/personas/:personaId/card`, ({ params }) => {
    const id = Number(params.personaId)
    return HttpResponse.json(okResult({ persona_id: id, card: { ...mockCard }, updated_at: 1731000000000 }))
  }),
  // 卡片整体替换。
  http.put(`${BACKEND_ORIGIN}/personas/:personaId/card`, async ({ request }) => {
    const body = (await request.json()) as { name: string }
    const id = Number(new URL(request.url).pathname.split('/').pop())
    return HttpResponse.json(
      okResult({ persona_id: id, card: { ...mockCard, name: body.name }, updated_at: 1731000000001 }, '人格卡片已替换'),
    )
  }),
  // 状态层读取（M 已写 / S 未写）。
  http.get(`${BACKEND_ORIGIN}/personas/:personaId/states`, ({ params }) => {
    const id = Number(params.personaId)
    return HttpResponse.json(okResult([mockState({ persona_id: id, layer: 'M' })]))
  }),
  // 状态层 upsert。
  http.put(`${BACKEND_ORIGIN}/personas/:personaId/states/:layer`, async ({ request }) => {
    const body = (await request.json()) as { state: Record<string, unknown> }
    const url = new URL(request.url)
    const id = Number(url.pathname.split('/')[4])
    const layer = url.pathname.split('/').pop()
    return HttpResponse.json(
      okResult(
        mockState({ persona_id: id, layer: layer === 'S' ? 'S' : 'M', state: body.state }),
        '人格状态已更新',
      ),
    )
  }),
]

/** 测试用情景事件（wire 形态；kind=chat 回合载荷）。 */
export function mockEpisode(overrides: Partial<EpisodeRecord> = {}): EpisodeRecord {
  return {
    id: 1,
    persona_id: '1',
    kind: 'chat',
    source: 'chat-runtime',
    content: { user_content: '今天好累', assistant_content: '辛苦了，想聊聊吗？', session_id: null },
    importance: 5,
    provenance: 'owner',
    content_hash: 'a1b2c3d4e5f60718',
    ts_ms: 1731000000000,
    created_at: 1731000000000,
    updated_at: 1731000000000,
    ...overrides,
  }
}

/** 测试用策展记忆（wire 形态）。 */
export function mockCurated(overrides: Partial<CuratedRecord> = {}): CuratedRecord {
  return {
    id: 1,
    persona_id: '1',
    kind: 'memory',
    topic: 'favorite-food',
    content: '主人最喜欢深夜吃一碗热汤面。',
    importance: 6,
    provenance: 'owner',
    status: 'active',
    superseded_by_id: null,
    version: 2,
    created_at: 1731000000000,
    updated_at: 1731000000000,
    ...overrides,
  }
}

/** 测试用会话摘要（wire 形态）。 */
export function mockSessionSummary(overrides: Partial<ChatSessionSummary> = {}): ChatSessionSummary {
  return {
    id: 1,
    persona_id: '1',
    title: '深夜陪伴对话',
    message_count: 3,
    created_at: 1731000000000,
    last_active_at: 1731000001000,
    ...overrides,
  }
}

/** 测试用会话详情（wire 形态；3 条消息 seq 升序）。 */
export function mockSessionDetail(
  overrides: Partial<ChatSessionDetail> = {},
): ChatSessionDetail {
  const sessionId = overrides.id ?? 1
  return {
    id: sessionId,
    persona_id: '1',
    title: '深夜陪伴对话',
    message_count: 3,
    created_at: 1731000000000,
    last_active_at: 1731000001000,
    messages: [
      { id: 1, session_id: sessionId, seq: 1, role: 'user', content: '今天好累', ts_ms: 1731000000000 },
      { id: 2, session_id: sessionId, seq: 2, role: 'assistant', content: '辛苦了，想聊聊吗？', ts_ms: 1731000000500 },
      { id: 3, session_id: sessionId, seq: 3, role: 'user', content: '就是工作上的事', ts_ms: 1731000001000 },
    ],
    ...overrides,
  }
}

/** 测试用检索结果（keyword 相关度形态）。 */
export function mockSearchResult(overrides: Partial<MemorySearchResult> = {}): MemorySearchResult {
  return {
    ref_type: 'episode',
    ref_id: 1,
    kind: 'chat',
    provenance: 'owner',
    ts_ms: 1731000000000,
    content: '主人提到深夜想吃热汤面。',
    score: 2,
    ...overrides,
  }
}

/** 测试用梦境运行记录（wire 形态；success 终态）。 */
export function mockDreamRun(overrides: Partial<DreamRunRecord> = {}): DreamRunRecord {
  return {
    id: 1,
    persona_id: '1',
    status: 'success',
    started_ms: 1731000000000,
    finished_ms: 1731000009000,
    stats: { candidates: 3, gated: 2, promoted: 1, superseded: 1, skipped_untrusted: 1, failed: 0 },
    decisions: [
      { episode_id: 1, topic: 'favorite-food', kind: 'memory', action: 'promoted', reason: '分数达标，晋升为新主题' },
      { episode_id: 2, topic: 'sleep-pattern', kind: 'relationship', action: 'merged', reason: '并入既有 active 主题' },
      { episode_id: 3, topic: '', kind: null, action: 'skipped', reason: '低于分数线' },
    ],
    diary_episode_id: 12,
    error: null,
    created_at: 1731000000000,
    updated_at: 1731000009000,
    ...overrides,
  }
}

/** 测试用量记录（wire 形态）。 */
export function mockUsageRecord(overrides: Partial<UsageRecord> = {}): UsageRecord {
  return {
    id: 1,
    persona_id: '1',
    ts_ms: 1731000000000,
    model_entry: 'main',
    provider: 'openai_compatible',
    purpose: 'chat',
    input_tokens: 1200,
    output_tokens: 300,
    total_tokens: 1500,
    latency_ms: 850,
    status: 'success',
    request_id: 'req-001',
    error_code: null,
    created_at: 1731000000000,
    ...overrides,
  }
}

/** 测试用量聚合行（wire 形态）。 */
export function mockUsageAggregate(overrides: Partial<UsageAggregate> = {}): UsageAggregate {
  return {
    group: 'chat',
    bucket_ms: null,
    input_tokens: 1200,
    output_tokens: 300,
    total_tokens: 1500,
    call_count: 2,
    avg_latency_ms: 850,
    error_count: 0,
    ...overrides,
  }
}

/** 测试用量总览（wire 形态）。 */
export function mockUsageSummary(overrides: Partial<UsageSummary> = {}): UsageSummary {
  return {
    call_count: 3,
    input_tokens: 1700,
    output_tokens: 500,
    total_tokens: 2200,
    avg_latency_ms: 867,
    error_count: 1,
    purposes: [
      mockUsageAggregate({ group: 'chat' }),
      mockUsageAggregate({
        group: 'embed',
        input_tokens: 500,
        output_tokens: 200,
        total_tokens: 700,
        call_count: 1,
        avg_latency_ms: 900,
        error_count: 1,
      }),
    ],
    ...overrides,
  }
}

/** 测试用模型条目（脱敏 wire 形态）。 */
export function mockModelEntry(overrides: Partial<ModelEntryMasked> = {}): ModelEntryMasked {
  return {
    name: 'main',
    provider: 'openai_compatible',
    base_url: 'http://127.0.0.1:8001/v1',
    api_key_env: 'MAIN_API_KEY',
    model: 'companion-main',
    roles: ['chat'],
    resolved_roles: ['chat'],
    concurrency: null,
    multimodal: false,
    ...overrides,
  }
}

/** 测试用注册表状态（wire 形态）。 */
export function mockRegistryState(
  entries: ModelEntryMasked[] = [
    mockModelEntry(),
    mockModelEntry({
      name: 'embed-model',
      base_url: 'http://127.0.0.1:8002/v1',
      api_key_env: '',
      model: 'text-embedding',
      roles: ['embed'],
      resolved_roles: ['embed'],
      multimodal: false,
    }),
  ],
  roleBindings: Record<string, string> = {},
): RegistryState {
  return { entries, role_bindings: roleBindings }
}

/** 测试用角色绑定面（wire 形态；chat/embed 绑定、duty/dream/look_at 未绑定）。 */
export function mockRoleBindings(): RoleBinding[] {
  return [
    { role: 'chat', bound: true, entry_name: 'main' },
    { role: 'dream', bound: false, entry_name: null },
    { role: 'duty', bound: false, entry_name: null },
    { role: 'embed', bound: true, entry_name: 'embed-model' },
    { role: 'look_at', bound: false, entry_name: null },
  ]
}

/** memories 域默认 handlers（成功形态 + 回退存储向量模式拒绝）。 */
export const memoryHandlers = [
  // 情景事件列表：3 行（kind / provenance / 时间范围过滤 + 游标两页）。
  http.get(`${BACKEND_ORIGIN}/memories/episodes`, ({ request }) => {
    const url = new URL(request.url)
    const cursor = url.searchParams.get('cursor')
    const kind = url.searchParams.get('kind')
    const provenance = url.searchParams.get('provenance')
    const startMs = url.searchParams.get('start_ms')
    const endMs = url.searchParams.get('end_ms')
    const all = [
      mockEpisode({
        id: 3,
        kind: 'duty',
        source: 'duty-runner',
        content: { title: '巡检报告', summary: '一切正常', details: {} },
        ts_ms: 1731000200000,
      }),
      mockEpisode({ id: 2, provenance: 'agent', ts_ms: 1731000100000 }),
      mockEpisode({ id: 1, ts_ms: 1731000000000 }),
    ]
    const filtered = all.filter((row) => {
      if (kind !== null && row.kind !== kind) return false
      if (provenance !== null && row.provenance !== provenance) return false
      if (startMs !== null && row.ts_ms < Number(startMs)) return false
      if (endMs !== null && row.ts_ms >= Number(endMs)) return false
      return true
    })
    if (cursor === null) {
      return HttpResponse.json(
        okResult({
          items: filtered.slice(0, 2),
          total: -1,
          page: 1,
          page_size: 20,
          next_cursor: 'episodes-page-2',
          has_more: true,
        }),
      )
    }
    return HttpResponse.json(
      okResult({
        items: filtered.slice(2),
        total: -1,
        page: 1,
        page_size: 20,
        next_cursor: null,
        has_more: false,
      }),
    )
  }),
  // 手工注入：回显记录。
  http.post(`${BACKEND_ORIGIN}/memories/episodes`, async ({ request }) => {
    const body = (await request.json()) as { kind: string }
    return HttpResponse.json(
      okResult(mockEpisode({ id: 9, kind: body.kind as EpisodeRecord['kind'] }), '情景事件已记录'),
    )
  }),
  // 批量删除（部分成功形态：id=1 记 NOT_FOUND 失败——人格作用域外语义）。
  http.post(`${BACKEND_ORIGIN}/memories/episodes/batch-delete`, async ({ request }) => {
    const body = (await request.json()) as { ids: number[] }
    const items = body.ids.map((id) =>
      id === 1
        ? { id, success: false, code: 'NOT_FOUND', message: '事件不存在或不在此人格作用域' }
        : { id, success: true, code: null, message: '已删除' },
    )
    const succeeded = items.filter((item) => item.success).length
    return HttpResponse.json(
      okResult(
        { total: body.ids.length, succeeded, failed: body.ids.length - succeeded, items },
        `批量删除完成（成功 ${succeeded} / 失败 ${body.ids.length - succeeded}）`,
      ),
    )
  }),
  // 读取 / 删除（404 语义）。
  http.get(`${BACKEND_ORIGIN}/memories/episodes/:episodeId`, ({ params }) => {
    const id = Number(params.episodeId)
    if (id === 404) {
      return HttpResponse.json(errorResult('事件不存在或不在此人格作用域', 'NOT_FOUND'), { status: 404 })
    }
    return HttpResponse.json(okResult(mockEpisode({ id })))
  }),
  http.delete(`${BACKEND_ORIGIN}/memories/episodes/:episodeId`, ({ params }) => {
    const id = Number(params.episodeId)
    if (id === 404) {
      return HttpResponse.json(errorResult('事件不存在或不在此人格作用域', 'NOT_FOUND'), { status: 404 })
    }
    return HttpResponse.json(okResult(null, '情景事件已删除'))
  }),
  // 策展记忆列表：3 行（superseded 链 + 游标两页）。
  http.get(`${BACKEND_ORIGIN}/memories/curated`, ({ request }) => {
    const url = new URL(request.url)
    const cursor = url.searchParams.get('cursor')
    const kind = url.searchParams.get('kind')
    const topic = url.searchParams.get('topic')
    const status = url.searchParams.get('status')
    const all = [
      mockCurated({ id: 3, topic: 'sleep-pattern', kind: 'relationship', status: 'active', version: 1 }),
      mockCurated({ id: 2, topic: 'sleep-pattern', kind: 'relationship', status: 'superseded', superseded_by_id: 3, version: 1 }),
      mockCurated({ id: 1 }),
    ]
    const filtered = all.filter((row) => {
      if (kind !== null && row.kind !== kind) return false
      if (topic !== null && row.topic !== topic) return false
      if (status !== null && row.status !== status) return false
      return true
    })
    if (cursor === null) {
      return HttpResponse.json(
        okResult({
          items: filtered.slice(0, 2),
          total: -1,
          page: 1,
          page_size: 20,
          next_cursor: 'curated-page-2',
          has_more: true,
        }),
      )
    }
    return HttpResponse.json(
      okResult({
        items: filtered.slice(2),
        total: -1,
        page: 1,
        page_size: 20,
        next_cursor: null,
        has_more: false,
      }),
    )
  }),
  // upsert：untrusted 422 拒绝（平台内容永不入策展层）。
  http.post(`${BACKEND_ORIGIN}/memories/curated/upsert`, async ({ request }) => {
    const body = (await request.json()) as { provenance: string }
    if (body.provenance === 'untrusted') {
      return HttpResponse.json(
        errorResult('策展层不接受 untrusted 来源（平台内容永不入策展层）', 'VALIDATION_ERROR', {
          provenance: 'untrusted',
        }),
        { status: 422 },
      )
    }
    return HttpResponse.json(okResult(mockCurated({ id: 9, provenance: body.provenance as CuratedRecord['provenance'] }), '策展记忆已保存'))
  }),
  // 历史链（版本升序 supersede 链）。
  http.get(`${BACKEND_ORIGIN}/memories/curated/:memoryId/history`, ({ params }) => {
    const id = Number(params.memoryId)
    if (id === 404) {
      return HttpResponse.json(errorResult('记忆不存在或不在此人格作用域', 'NOT_FOUND'), { status: 404 })
    }
    return HttpResponse.json(
      okResult([
        mockCurated({ id: 2, topic: 'sleep-pattern', kind: 'relationship', status: 'superseded', superseded_by_id: 3, version: 1, content: '旧版本：主人最近睡得不太好。' }),
        mockCurated({ id: 3, topic: 'sleep-pattern', kind: 'relationship', status: 'active', version: 2 }),
      ]),
    )
  }),
  http.delete(`${BACKEND_ORIGIN}/memories/curated/:memoryId`, ({ params }) => {
    const id = Number(params.memoryId)
    if (id === 404) {
      return HttpResponse.json(errorResult('记忆不存在或不在此人格作用域', 'NOT_FOUND'), { status: 404 })
    }
    return HttpResponse.json(okResult(null, '策展记忆已删除'))
  }),
  // 会话列表 + 详情 + 删除。
  http.get(`${BACKEND_ORIGIN}/memories/sessions`, ({ request }) => {
    const cursor = new URL(request.url).searchParams.get('cursor')
    const all = [
      mockSessionSummary({ id: 2, title: '周末闲聊', message_count: 5 }),
      mockSessionSummary({ id: 1 }),
    ]
    if (cursor === null) {
      return HttpResponse.json(
        okResult({
          items: all.slice(0, 1),
          total: -1,
          page: 1,
          page_size: 20,
          next_cursor: 'sessions-page-2',
          has_more: true,
        }),
      )
    }
    return HttpResponse.json(
      okResult({
        items: all.slice(1),
        total: -1,
        page: 1,
        page_size: 20,
        next_cursor: null,
        has_more: false,
      }),
    )
  }),
  http.get(`${BACKEND_ORIGIN}/memories/sessions/:sessionId`, ({ params }) => {
    const id = Number(params.sessionId)
    if (id === 404) {
      return HttpResponse.json(errorResult('会话不存在或不在此人格作用域', 'NOT_FOUND'), { status: 404 })
    }
    return HttpResponse.json(okResult(mockSessionDetail({ id })))
  }),
  http.delete(`${BACKEND_ORIGIN}/memories/sessions/:sessionId`, ({ params }) => {
    const id = Number(params.sessionId)
    if (id === 404) {
      return HttpResponse.json(errorResult('会话不存在或不在此人格作用域', 'NOT_FOUND'), { status: 404 })
    }
    return HttpResponse.json(okResult(null, '会话已删除'))
  }),
  // 检索：回退存储拒绝 dense/hybrid（422 VALIDATION_ERROR——fix 指引随 details）。
  http.post(`${BACKEND_ORIGIN}/memories/search`, async ({ request }) => {
    const body = (await request.json()) as { mode: string }
    if (body.mode !== 'keyword') {
      return HttpResponse.json(
        errorResult('回退存储不支持向量检索（仅 keyword 模式可用）', 'VALIDATION_ERROR', {
          mode: body.mode,
          fix: '启用 SeekDB（SEEKDB_ENABLED=true）或改用 mode=keyword',
        }),
        { status: 422 },
      )
    }
    return HttpResponse.json(
      okResult([
        mockSearchResult({ score: 3 }),
        mockSearchResult({ ref_id: 2, content: '主人喜欢周末睡懒觉。', score: 1 }),
      ]),
    )
  }),
  // 梦境触发（同步运行——响应即运行记录）。
  http.post(`${BACKEND_ORIGIN}/memories/dream/trigger`, () => {
    return HttpResponse.json(
      okResult(
        mockDreamRun({ id: 9 }),
        '梦境整理完成（候选 3 / 晋升 1 / 并入 1）',
      ),
    )
  }),
  // 运行日志列表 + 详情。
  http.get(`${BACKEND_ORIGIN}/memories/dream/runs`, ({ request }) => {
    const cursor = new URL(request.url).searchParams.get('cursor')
    const all = [
      mockDreamRun({ id: 2, started_ms: 1731000100000 }),
      mockDreamRun({ id: 1 }),
    ]
    if (cursor === null) {
      return HttpResponse.json(
        okResult({
          items: all.slice(0, 1),
          total: -1,
          page: 1,
          page_size: 20,
          next_cursor: 'dream-runs-page-2',
          has_more: true,
        }),
      )
    }
    return HttpResponse.json(
      okResult({
        items: all.slice(1),
        total: -1,
        page: 1,
        page_size: 20,
        next_cursor: null,
        has_more: false,
      }),
    )
  }),
  http.get(`${BACKEND_ORIGIN}/memories/dream/runs/:runId`, ({ params }) => {
    const id = Number(params.runId)
    if (id === 404) {
      return HttpResponse.json(errorResult('运行不存在或不在此人格作用域', 'NOT_FOUND'), { status: 404 })
    }
    return HttpResponse.json(okResult(mockDreamRun({ id })))
  }),
]

/** usage 域默认 handlers（成功形态 + time_bucket 缺桶宽 422）。 */
export const usageHandlers = [
  http.get(`${BACKEND_ORIGIN}/usage/records`, ({ request }) => {
    const url = new URL(request.url)
    const cursor = url.searchParams.get('cursor')
    const personaId = url.searchParams.get('persona_id')
    const modelEntry = url.searchParams.get('model_entry')
    const purpose = url.searchParams.get('purpose')
    const status = url.searchParams.get('status')
    const all = [
      mockUsageRecord({ id: 2, ts_ms: 1731000100000, purpose: 'embed', model_entry: 'embed-model', status: 'error', error_code: 'MODEL_NOT_FOUND', persona_id: null }),
      mockUsageRecord({ id: 1 }),
    ]
    const filtered = all.filter((row) => {
      if (personaId !== null && row.persona_id !== personaId) return false
      if (modelEntry !== null && row.model_entry !== modelEntry) return false
      if (purpose !== null && row.purpose !== purpose) return false
      if (status !== null && row.status !== status) return false
      return true
    })
    if (cursor === null) {
      return HttpResponse.json(
        okResult({
          items: filtered.slice(0, 1),
          total: -1,
          page: 1,
          page_size: 20,
          next_cursor: 'usage-records-page-2',
          has_more: true,
        }),
      )
    }
    return HttpResponse.json(
      okResult({
        items: filtered.slice(1),
        total: -1,
        page: 1,
        page_size: 20,
        next_cursor: null,
        has_more: false,
      }),
    )
  }),
  http.get(`${BACKEND_ORIGIN}/usage/aggregates`, ({ request }) => {
    const url = new URL(request.url)
    const groupBy = url.searchParams.get('group_by')
    const timeBucket = url.searchParams.get('time_bucket')
    if (groupBy === 'time_bucket' && timeBucket === null) {
      return HttpResponse.json(
        errorResult('group_by=time_bucket 时必须声明 time_bucket 桶宽', 'VALIDATION_ERROR'),
        { status: 422 },
      )
    }
    if (groupBy === 'model_entry') {
      return HttpResponse.json(
        okResult([
          mockUsageAggregate({ group: 'main', total_tokens: 1500 }),
          mockUsageAggregate({ group: 'embed-model', total_tokens: 700 }),
        ]),
      )
    }
    if (groupBy === 'time_bucket') {
      return HttpResponse.json(
        okResult([
          mockUsageAggregate({ group: '1731000000000', bucket_ms: 1731000000000, total_tokens: 1500 }),
          mockUsageAggregate({ group: '1731003600000', bucket_ms: 1731003600000, total_tokens: 700 }),
        ]),
      )
    }
    return HttpResponse.json(okResult([mockUsageAggregate({ group: 'chat' }), mockUsageAggregate({ group: 'embed' })]))
  }),
  http.get(`${BACKEND_ORIGIN}/usage/summary`, () => {
    return HttpResponse.json(okResult(mockUsageSummary()))
  }),
]

/** models 域默认 handlers（成功形态 + 409 / 404 / 400 错误面）。 */
export const modelHandlers = [
  http.get(`${BACKEND_ORIGIN}/models/entries`, () => {
    return HttpResponse.json(okResult(mockRegistryState()))
  }),
  http.get(`${BACKEND_ORIGIN}/models/roles`, () => {
    return HttpResponse.json(okResult(mockRoleBindings()))
  }),
  // 创建：重名 main → 409。
  http.post(`${BACKEND_ORIGIN}/models/entries`, async ({ request }) => {
    const body = (await request.json()) as {
      name: string
      provider: string
      base_url: string
      api_key_env: string
      model: string
      roles: string[]
      concurrency: number | null
      multimodal: boolean
    }
    if (body.name === 'main') {
      return HttpResponse.json(errorResult('模型条目名已存在: main', 'CONFLICT', { name: 'main' }), {
        status: 409,
      })
    }
    return HttpResponse.json(
      okResult(
        mockRegistryState([
          mockModelEntry(),
          mockModelEntry({ name: 'embed-model', roles: ['embed'], resolved_roles: ['embed'] }),
          mockModelEntry({
            name: body.name,
            provider: body.provider,
            base_url: body.base_url,
            api_key_env: body.api_key_env,
            model: body.model,
            roles: body.roles,
            resolved_roles: [],
            concurrency: body.concurrency,
            multimodal: body.multimodal,
          }),
        ]),
        '模型条目已创建',
      ),
    )
  }),
  // 部分更新：请求体携带 name（extra=forbid）→ 400；未知条目 → 404。
  http.put(`${BACKEND_ORIGIN}/models/entries/:name`, async ({ request, params }) => {
    const name = String(params.name)
    const body = (await request.json()) as Record<string, unknown>
    if ('name' in body) {
      return HttpResponse.json(
        errorResult('请求参数错误', 'VALIDATION_ERROR', {
          errors: [{ loc: ['body', 'name'], msg: 'Extra inputs are not permitted', type: 'extra_forbidden' }],
        }),
        { status: 400 },
      )
    }
    if (name !== 'main' && name !== 'embed-model') {
      return HttpResponse.json(errorResult(`模型条目不存在: ${name}`, 'NOT_FOUND', { name }), {
        status: 404,
      })
    }
    return HttpResponse.json(okResult(mockRegistryState(), '模型条目已更新'))
  }),
  // 删除：main 仍绑定角色 → 409（bound_roles + 两步指引）；embed-model 可删。
  http.delete(`${BACKEND_ORIGIN}/models/entries/:name`, ({ params }) => {
    const name = String(params.name)
    if (name === 'main') {
      return HttpResponse.json(
        errorResult('模型条目仍绑定角色，禁止删除（先解绑再删除）: main', 'CONFLICT', {
          name: 'main',
          bound_roles: ['chat'],
          fix: '两步操作：PUT /api/v1/models/entries/{name} 将 roles 置空（条目声明角色）；显式 role_bindings 指向的条目需先在 config.yaml 移除该绑定',
        }),
        { status: 409 },
      )
    }
    if (name !== 'embed-model') {
      return HttpResponse.json(errorResult(`模型条目不存在: ${name}`, 'NOT_FOUND', { name }), {
        status: 404,
      })
    }
    return HttpResponse.json(okResult(mockRegistryState([mockModelEntry()]), '模型条目已删除'))
  }),
]

/** 共享 msw 服务器（setup.ts 管理生命周期）。 */
export const server = setupServer(...personaHandlers, ...memoryHandlers, ...usageHandlers, ...modelHandlers)

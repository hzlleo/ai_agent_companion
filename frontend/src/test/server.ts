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
  CoreMemoryBlock,
  CoreMemoryRevision,
  DirectiveRecord,
  FactRecord,
  PersonaRecord,
  PersonaState,
  SkillRecord,
  TraitComponent,
  TraitLibrary,
  TraitPreset,
} from '@/types/persona'
import type {
  ChatMessage,
  ChatMessageVariant,
  ChatSessionDetail,
  ChatSessionSummary,
  CuratedRecord,
  DreamRunRecord,
  EpisodeRecord,
  MemorySearchResult,
} from '@/types/memory'
import type { UsageAggregate, UsageRecord, UsageSummary } from '@/types/usage'
import type { ModelEntryMasked, RegistryState, RoleBinding } from '@/types/model-registry'
import type { ReminderRecord } from '@/types/reminders'
import type {
  StickerAnnotationResult,
  StickerBatchDeleteResult,
  StickerBatchUploadResult,
  StickerRecord,
} from '@/types/stickers'

/** 后端 URL 前缀（handlers 统一经此拼接，与 api client 同源）。 */
export const BACKEND_ORIGIN = `${API_BASE_URL}${API_PREFIX}`

/** msw formData 部件 → 文件项判定（duck-typing——msw 重建的 File 与测试域全局 File 非同构造器，instanceof 不可靠）。 */
export function isFileEntry(entry: FormDataEntryValue | null): entry is File {
  return typeof entry === 'object' && entry !== null && 'name' in entry && 'text' in entry
}

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
    is_primary: true,
    card: { ...mockCard },
    voice_profile: null,
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

/** 测试用特质组件（wire 形态）。 */
export function mockTraitComponent(overrides: Partial<TraitComponent> = {}): TraitComponent {
  return {
    id: 'warm-hearted',
    name_zh: '温情底色',
    description_zh: '待人有一种天然的暖意，会默默惦记对方的冷暖',
    category: 'temperament',
    maturity: 'safe',
    tone_directives: ['语气里带着藏不住的关切'],
    sample_lines: ['今天降温了，出门记得把外套穿上。', '早饭吃了吗？', '早点休息。'],
    sample_speech: null,
    conflicts: [],
    ...overrides,
  }
}

/** 测试用特质预设（wire 形态；傲娇组合）。 */
export function mockTraitPreset(overrides: Partial<TraitPreset> = {}): TraitPreset {
  return {
    id: 'tsundere',
    name_zh: '傲娇',
    tagline_zh: '嘴上全是拒绝，行动全是真心',
    description_zh: '口是心非的别扭鬼，好话反着说、关心藏着给',
    components: ['duplicitous-mouth', 'soft-heart-hard-mouth'],
    intimacy_level: 1,
    scene_scripts: {
      daily: ['关心一律反着说，行动上不落下'],
      flirty: ['被撩到先嘴硬，否认到一半自己笑场'],
      intimate: ['亲近前先别扭一句，得到回应就靠过去'],
      conflict: ['吵架时句句带刺，刺完偷偷看你脸色'],
    },
    base_card_overrides: {
      name_suffix: null,
      personality: null,
      mes_example: null,
      system_prompt_fragment: null,
    },
    suggested_voice: null,
    suggested_book: [],
    ...overrides,
  }
}

/** 测试用特质库（画廊载荷：2 预设 + 组件注册表）。 */
export function mockTraitLibrary(): TraitLibrary {
  return {
    presets: [
      mockTraitPreset(),
      mockTraitPreset({
        id: 'reunion-lover',
        name_zh: '旧识恋人',
        tagline_zh: '隔了几年，还是先叫出你的名字',
        description_zh: '兜兜转转又走回彼此身边的旧恋人',
        components: ['love-drunk', 'anniversary-memory'],
        intimacy_level: 3,
        scene_scripts: {
          daily: ['用旧日的默契相处，很多话不必说完'],
          flirty: ['用旧称呼试探，谁先提起从前谁先输'],
          intimate: ['想念直接说出口，不拿习惯当借口'],
          conflict: ['旧账只翻一次，说完就收'],
        },
      }),
      mockTraitPreset({
        id: 'genki-girl',
        name_zh: '元气少女',
        tagline_zh: '人形小太阳，电量永远 100%',
        description_zh: '元气满格的活泼少女，一句话能把阴天聊出太阳',
        components: ['energetic-sunny', 'pun-enthusiast'],
        intimacy_level: 2,
        scene_scripts: {
          daily: ['情绪外放，用感叹把气氛拉起来'],
          flirty: ['用打趣起哄试探，自己先笑到不行'],
          intimate: ['亲近时热烈直接，被抱住就安静下来'],
          conflict: ['生气来得快去得也快，凑过去用玩笑破冰'],
        },
        base_card_overrides: {
          name_suffix: '-酱',
          personality: null,
          mes_example: null,
          system_prompt_fragment: null,
        },
      }),
    ],
    components: [
      mockTraitComponent(),
      mockTraitComponent({
        id: 'duplicitous-mouth',
        name_zh: '口是心非',
        description_zh: '想要偏说不要，喜欢偏说讨厌',
        category: 'speech',
        maturity: 'safe',
        tone_directives: ['想法与说出口的话刻意相反'],
        sample_lines: ['谁想和你多待一会儿啊，我只是刚好顺路。'],
        sample_speech: null,
      }),
      mockTraitComponent({
        id: 'soft-heart-hard-mouth',
        name_zh: '嘴硬心软',
        description_zh: '嘴上不饶人，落在行动里却诚实得很',
        category: 'temperament',
        maturity: 'safe',
        tone_directives: [],
        sample_lines: ['才不是为了你，只是刚好多做了一份。'],
        sample_speech: null,
        conflicts: [],
      }),
      mockTraitComponent({
        id: 'energetic-sunny',
        name_zh: '元气满满',
        description_zh: '情绪高昂有干劲，是身边的晴天',
        category: 'temperament',
        maturity: 'suggestive',
        tone_directives: ['语气轻快上扬'],
        sample_lines: ['走走走！出去兜一圈！'],
        sample_speech: null,
        conflicts: [],
      }),
      mockTraitComponent({
        id: 'love-drunk',
        name_zh: '情难自禁',
        description_zh: '心动起来藏不住，目光会黏在人身上',
        category: 'temperament',
        maturity: 'explicit',
        tone_directives: ['情绪上来时坦白得近乎笨拙'],
        sample_lines: ['别看我，再看下去我就要说些收不回去的话了。'],
        sample_speech: null,
        conflicts: [],
      }),
      mockTraitComponent({
        id: 'anniversary-memory',
        name_zh: '纪念日仪式',
        description_zh: '记得各种日子，会有一句「今天是什么日子」',
        category: 'interaction_ritual',
        maturity: 'safe',
        tone_directives: ['重要日期以一句提醒与一件小事落地'],
        sample_lines: ['今天几号？想一想。'],
        sample_speech: null,
        conflicts: [],
      }),
      mockTraitComponent({
        id: 'pun-enthusiast',
        name_zh: '谐音梗爱好者',
        description_zh: '谐音梗十级学者，逮着机会就要抖一个',
        category: 'humor',
        maturity: 'safe',
        tone_directives: [],
        sample_lines: ['我知道一个店特别蒜了。'],
        sample_speech: null,
        conflicts: [],
      }),
    ],
  }
}

/** personas 域默认 handlers（成功形态）。 */
export const personaHandlers = [  // 游标分页列表：首页 2 条 + next_cursor；带 cursor 时返回末页。
  http.get(`${BACKEND_ORIGIN}/personas`, ({ request }) => {
    const url = new URL(request.url)
    const cursor = url.searchParams.get('cursor')
    const status = url.searchParams.get('status')
    const all = [
      mockPersona({ id: 2, name: 'companion-2', is_primary: false }),
      mockPersona({ id: 1, name: 'companion-1', is_primary: true }),
      mockPersona({ id: 3, name: 'old-friend', status: 'disabled', is_primary: false }),
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
        items: [filtered[2] ?? mockPersona({ id: 3, name: 'old-friend', status: 'disabled', is_primary: false })],
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
        ? { id, success: false, code: 'CONFLICT', message: '不能删除当前主人格，请先将另一个人格设为主人格（set-primary）再删除' }
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
  // 当前主人格读取（id=1 在位）。
  http.get(`${BACKEND_ORIGIN}/personas/primary`, () =>
    HttpResponse.json(okResult(mockPersona({ id: 1, is_primary: true }))),
  ),
  // 官方特质预设库（画廊载荷）。
  http.get(`${BACKEND_ORIGIN}/personas/presets`, () =>
    HttpResponse.json(okResult(mockTraitLibrary())),
  ),
  // 单发导入：JSON 面（官方预设生成 / 用户卡片）——同名「傲娇」与
  // desired_name=companion-1 触发冲突形态，其余成功导入。
  http.post(`${BACKEND_ORIGIN}/personas/import`, async ({ request }) => {
    const contentType = request.headers.get('content-type') ?? ''
    let presetId: string | null = null
    let cardName = ''
    let desiredName: string | null = null
    let strategy = 'skip'
    if (contentType.includes('multipart/form-data')) {
      const form = await request.formData()
      presetId = null
      const file = form.get('file')
      if (isFileEntry(file)) {
        try {
          cardName = (JSON.parse(await file.text()) as { name?: string }).name ?? ''
        } catch {
          return HttpResponse.json(
            errorResult('导入卡片文件解析失败（须为 chara card v2 JSON 文档）', 'VALIDATION_ERROR'),
            { status: 422 },
          )
        }
      }
      const strategyField = form.get('conflict_strategy')
      if (typeof strategyField === 'string' && strategyField !== '') {
        strategy = strategyField
      }
      const desiredField = form.get('desired_name')
      if (typeof desiredField === 'string' && desiredField !== '') {
        desiredName = desiredField
      }
    } else {
      const body = (await request.json()) as {
        preset_id?: string | null
        card?: { name?: string } | null
        conflict_strategy?: string
        desired_name?: string | null
      }
      presetId = body.preset_id ?? null
      cardName = body.card?.name ?? ''
      desiredName = body.desired_name ?? null
      strategy = body.conflict_strategy ?? 'skip'
    }
    if (presetId !== null && presetId !== 'tsundere' && presetId !== 'genki-girl') {
      return HttpResponse.json(errorResult('官方预设不存在', 'NOT_FOUND'), { status: 404 })
    }
    const targetName = desiredName ?? (presetId !== null ? '傲娇' : cardName)
    if (strategy === 'skip' && targetName === 'companion-1') {
      return HttpResponse.json(
        okResult(
          {
            imported: false,
            persona_id: 1,
            reason: 'name_conflict',
            warnings: [],
          },
          '注册名已存在，按跳过策略未导入',
        ),
      )
    }
    if (strategy === 'rename' && targetName === 'companion-1') {
      return HttpResponse.json(
        okResult(
          { imported: true, persona_id: 11, reason: null, warnings: [] },
          '人格已导入',
        ),
      )
    }
    return HttpResponse.json(
      okResult(
        { imported: true, persona_id: presetId !== null ? 11 : 12, reason: null, warnings: [] },
        '人格已导入',
      ),
    )
  }),
  // 批量导入（multipart 多文件，部分成功形态：非法 JSON 失败 + companion-1 冲突跳过）。
  http.post(`${BACKEND_ORIGIN}/personas/import/batch`, async ({ request }) => {
    const form = await request.formData()
    const files = form.getAll('files').filter(isFileEntry)
    const results = await Promise.all(
      files.map(async (file) => {
        try {
          const cardName = (JSON.parse(await file.text()) as { name?: string }).name ?? ''
          if (cardName === 'companion-1') {
            return {
              filename: file.name,
              success: true,
              imported: false,
              persona_id: 1,
              reason: 'name_conflict',
              code: null,
              message: '注册名已存在，按跳过策略未导入',
            }
          }
          return {
            filename: file.name,
            success: true,
            imported: true,
            persona_id: 20,
            reason: null,
            code: null,
            message: '已导入',
          }
        } catch {
          return {
            filename: file.name,
            success: false,
            imported: false,
            persona_id: null,
            reason: null,
            code: 'VALIDATION_ERROR',
            message: '卡片文件解析失败（须为 chara card v2 JSON）: invalid',
          }
        }
      }),
    )
    const succeeded = results.filter((item) => item.success).length
    const importedCount = results.filter((item) => item.imported).length
    return HttpResponse.json(
      okResult(
        {
          total: results.length,
          succeeded,
          failed: results.length - succeeded,
          imported_count: importedCount,
          items: results,
        },
        `批量导入完成（成功 ${succeeded} / 失败 ${results.length - succeeded} / 新导入 ${importedCount}）`,
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
  // 删除：id=1 为当前主人格 → 409 主人格守卫。
  http.delete(`${BACKEND_ORIGIN}/personas/:personaId`, ({ params }) => {
    const id = Number(params.personaId)
    if (id === 1) {
      return HttpResponse.json(
        errorResult('不能删除当前主人格，请先将另一个人格设为主人格（set-primary）再删除', 'CONFLICT'),
        { status: 409 },
      )
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
      okResult(mockPersona({ id: 10, name: body.new_name, is_primary: false, status: 'disabled' }), '人格已复制'),
    )
  }),
  // 设为主人格：id=3（old-friend，停用状态）→ 409。
  http.post(`${BACKEND_ORIGIN}/personas/:personaId/set-primary`, ({ params }) => {
    const id = Number(params.personaId)
    if (id === 3) {
      return HttpResponse.json(errorResult('停用状态的人格不可设为主人格，请先启用（status=active）', 'CONFLICT'), { status: 409 })
    }
    return HttpResponse.json(okResult(mockPersona({ id, is_primary: true }), '人格已设为主人格（事务性切换）'))
  }),
  // 停用：id=1（当前主人格）→ 409 主人格守卫。
  http.post(`${BACKEND_ORIGIN}/personas/:personaId/disable`, ({ params }) => {
    const id = Number(params.personaId)
    if (id === 1) {
      return HttpResponse.json(
        errorResult('不能停用当前主人格，请先将另一个人格设为主人格（set-primary）再停用', 'CONFLICT'),
        { status: 409 },
      )
    }
    return HttpResponse.json(okResult(mockPersona({ id, is_primary: false, status: 'disabled' }), '人格已停用'))
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
    pinned: false,
    frozen: false,
    access_count: 0,
    last_accessed_ms: null,
    effective_strength: 5.2,
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

/** 测试用消息变体（wire 形态；父回合候选回复）。 */
export function mockVariant(overrides: Partial<ChatMessageVariant> = {}): ChatMessageVariant {
  return {
    id: 21,
    content: '变体回复一',
    ts_ms: 1731000000600,
    meta: {
      thought: '换个更温柔的说法。',
      emotion_deltas: { affection: 0.3 },
      emotion_snapshot: null,
      variant_index: 1,
    },
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
      { id: 1, session_id: sessionId, seq: 1, role: 'user', content: '今天好累', ts_ms: 1731000000000, variants: [] },
      {
        id: 2,
        session_id: sessionId,
        seq: 2,
        role: 'assistant',
        content: '辛苦了，想聊聊吗？',
        ts_ms: 1731000000500,
        meta: {
          thought: '他听起来真的很累，先别急着给建议。',
          emotion_deltas: { affection: 0.2 },
          emotion_snapshot: { affection: 0.7, shyness: 0.55 },
        },
        variants: [],
      },
      { id: 3, session_id: sessionId, seq: 3, role: 'user', content: '就是工作上的事', ts_ms: 1731000001000, variants: [] },
    ],
    ...overrides,
  }
}

/** 会话详情读侧 handler 用的 assistant 回合（含两条候选变体）。 */
function mockAssistantMessageWithVariants(sessionId: number): ChatMessage {
  const detail = mockSessionDetail({ id: sessionId })
  const assistant = detail.messages.find((message) => message.role === 'assistant')
  if (assistant === undefined) {
    throw new Error('mockSessionDetail 缺少 assistant 回合（测试数据损坏）')
  }
  return {
    ...assistant,
    variants: [
      mockVariant(),
      mockVariant({
        id: 22,
        content: '变体回复二',
        meta: {
          thought: '换一种更温柔的说法。',
          emotion_deltas: { affection: 0.3 },
          emotion_snapshot: null,
          variant_index: 2,
        },
      }),
    ],
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
    stats: {
      candidates: 3,
      gated: 2,
      promoted: 1,
      superseded: 1,
      skipped_untrusted: 1,
      skipped_peer: 0,
      failed: 0,
      archived: 0,
      facts_created: 1,
      facts_merged: 1,
      facts_dropped: 0,
      skills_created: 1,
      skills_merged: 1,
      skills_dropped: 0,
    },
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
    const includeArchived = url.searchParams.get('include_archived') === 'true'
    const all = [
      mockCurated({ id: 3, topic: 'sleep-pattern', kind: 'relationship', status: 'active', version: 1 }),
      mockCurated({ id: 2, topic: 'sleep-pattern', kind: 'relationship', status: 'superseded', superseded_by_id: 3, version: 1 }),
      mockCurated({ id: 1 }),
    ]
    const filtered = all.filter((row) => {
      if (kind !== null && row.kind !== kind) return false
      if (topic !== null && row.topic !== topic) return false
      if (status !== null) return row.status === status
      if (!includeArchived) return row.status !== 'archived'
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
  // 生命周期端点：置顶 / 冻结态切换（幂等——返回目标态）
  http.post(`${BACKEND_ORIGIN}/memories/curated/:memoryId/:action`, ({ params }) => {
    const id = Number(params.memoryId)
    const action = String(params.action)
    if (id === 404) {
      return HttpResponse.json(errorResult('记忆不存在或不在此人格作用域', 'NOT_FOUND'), { status: 404 })
    }
    if (action === 'pin') return HttpResponse.json(okResult(mockCurated({ id, pinned: true }), '策展记忆已置顶'))
    if (action === 'unpin') return HttpResponse.json(okResult(mockCurated({ id, pinned: false }), '策展记忆已取消置顶'))
    if (action === 'freeze') return HttpResponse.json(okResult(mockCurated({ id, frozen: true }), '策展记忆已冻结'))
    if (action === 'unfreeze') return HttpResponse.json(okResult(mockCurated({ id, frozen: false }), '策展记忆已解冻'))
    return HttpResponse.json(okResult(mockCurated({ id })))
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
  http.get(`${BACKEND_ORIGIN}/memories/sessions/:sessionId`, ({ request, params }) => {
    const id = Number(params.sessionId)
    if (id === 404) {
      return HttpResponse.json(errorResult('会话不存在或不在此人格作用域', 'NOT_FOUND'), { status: 404 })
    }
    const includeVariants =
      new URL(request.url).searchParams.get('include_variants') === 'true'
    const detail = mockSessionDetail({ id })
    if (includeVariants) {
      detail.messages = detail.messages.map((message) =>
        message.role === 'assistant' ? mockAssistantMessageWithVariants(id) : message,
      )
    }
    return HttpResponse.json(okResult(detail))
  }),
  // 变体重生成：按 count 返回候选（id 100 起）。
  http.post(
    `${BACKEND_ORIGIN}/memories/sessions/:sessionId/messages/:messageId/variants`,
    async ({ request }) => {
      const body = (await request.json()) as { count: number }
      const variants = Array.from({ length: body.count }, (_, index) =>
        mockVariant({ id: 100 + index, content: `新候选${index + 1}` }),
      )
      return HttpResponse.json(okResult(variants, `已生成 ${variants.length} 条变体`))
    },
  ),
  // 变体选定：回显父回合（内容替换为选中候选）。
  http.post(
    `${BACKEND_ORIGIN}/memories/sessions/:sessionId/messages/:messageId/select-variant`,
    async ({ request, params }) => {
      const body = (await request.json()) as { variant_id: number }
      const sessionId = Number(params.sessionId)
      const assistant = mockAssistantMessageWithVariants(sessionId)
      const variant =
        [mockVariant(), mockVariant({ id: 22, content: '变体回复二' }), mockVariant({ id: 100, content: '新候选1' })]
          .find((item) => item.id === body.variant_id) ?? mockVariant()
      return HttpResponse.json(
        okResult(
          {
            ...assistant,
            content: variant.content,
            meta: { ...variant.meta, selected_variant_id: variant.id },
            variants: assistant.variants,
          },
          '已选中该变体',
        ),
      )
    },
  ),
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

// ---------------------------------------------------------------------------
// reminders 域（持久化提醒——P10=B）
// ---------------------------------------------------------------------------

/** 测试用提醒记录（wire 形态）。 */
export function mockReminder(overrides: Partial<ReminderRecord> = {}): ReminderRecord {
  return {
    id: 1,
    persona_id: '1',
    title: '起来喝水',
    due_at_ms: 1731000600000,
    status: 'pending',
    delivery_attempts: 0,
    last_error: null,
    delivered_at_ms: null,
    source: 'manual',
    meta: null,
    created_at: 1731000000000,
    updated_at: 1731000000000,
    ...overrides,
  }
}

/** reminders 域默认 handlers（成功形态 + 状态守卫 / 部分成功面）。 */
export const reminderHandlers = [
  // 列表：3 行（status 过滤 + 游标两页）。
  http.get(`${BACKEND_ORIGIN}/reminders`, ({ request }) => {
    const url = new URL(request.url)
    const cursor = url.searchParams.get('cursor')
    const status = url.searchParams.get('status')
    const all = [
      mockReminder({ id: 3, title: '交水电费', due_at_ms: 1731001200000 }),
      mockReminder({
        id: 2,
        title: '早点睡',
        due_at_ms: 1731000900000,
        status: 'delivered',
        delivered_at_ms: 1731000900000,
      }),
      mockReminder({ id: 1, title: '已取消的提醒', status: 'cancelled' }),
    ]
    const filtered = all.filter((row) => status === null || row.status === status)
    if (cursor === null) {
      return HttpResponse.json(
        okResult({
          items: filtered.slice(0, 2),
          total: -1,
          page: 1,
          page_size: 20,
          next_cursor: 'reminders-page-2',
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
  // 创建：回显 pending 记录。
  http.post(`${BACKEND_ORIGIN}/reminders`, async ({ request }) => {
    const body = (await request.json()) as { title: string; due_at_ms: number }
    return HttpResponse.json(
      okResult(
        mockReminder({ id: 9, title: body.title, due_at_ms: body.due_at_ms }),
        '提醒已创建',
      ),
    )
  }),
  // 批量取消（部分成功形态：id=1 记 NOT_FOUND 失败——人格作用域外语义）。
  http.post(`${BACKEND_ORIGIN}/reminders/batch-cancel`, async ({ request }) => {
    const body = (await request.json()) as { ids: number[] }
    const items = body.ids.map((id) =>
      id === 1
        ? { id, success: false, code: 'NOT_FOUND', message: '提醒不存在（或不属于当前人格）' }
        : { id, success: true, code: null, message: '已取消' },
    )
    const succeeded = items.filter((item) => item.success).length
    return HttpResponse.json(
      okResult(
        { total: items.length, succeeded, failed: items.length - succeeded, items },
        '批量取消已完成',
      ),
    )
  }),
  // 读取：id=404 → 404。
  http.get(`${BACKEND_ORIGIN}/reminders/:reminderId`, ({ params }) => {
    const id = Number(params.reminderId)
    if (id === 404) {
      return HttpResponse.json(errorResult('提醒不存在（或不属于当前人格）', 'NOT_FOUND'), {
        status: 404,
      })
    }
    return HttpResponse.json(okResult(mockReminder({ id })))
  }),
  // 取消：id=2（已投递）→ 422 状态守卫；其余 → cancelled。
  http.post(`${BACKEND_ORIGIN}/reminders/:reminderId/cancel`, ({ params }) => {
    const id = Number(params.reminderId)
    if (id === 2) {
      return HttpResponse.json(
        errorResult('仅待投递提醒可以取消', 'VALIDATION_ERROR', { id, status: 'delivered' }),
        { status: 422 },
      )
    }
    if (id === 404) {
      return HttpResponse.json(errorResult('提醒不存在（或不属于当前人格）', 'NOT_FOUND'), {
        status: 404,
      })
    }
    return HttpResponse.json(okResult(mockReminder({ id, status: 'cancelled' }), '提醒已取消'))
  }),
]

// ---------------------------------------------------------------------------
// directives 域（长期指令——USER.md 式常备指令）
// ---------------------------------------------------------------------------

/** 测试用长期指令记录（wire 形态）。 */
export function mockDirective(overrides: Partial<DirectiveRecord> = {}): DirectiveRecord {
  return {
    id: 1,
    topic: 'directive:7f1a2b3c4d5e',
    content: '以后都用简体中文',
    version: 1,
    created_at: 1731000000000,
    updated_at: 1731000000000,
    ...overrides,
  }
}

/** directives 域默认 handlers（成功形态 + 404 面）。 */
export const directiveHandlers = [
  // 列表：两条现行指令（新写入在前）。
  http.get(`${BACKEND_ORIGIN}/directives`, () => {
    return HttpResponse.json(
      okResult([
        mockDirective({ id: 2, topic: 'directive:aa11bb22cc33', content: '不要超过十二点睡' }),
        mockDirective(),
      ]),
    )
  }),
  // 创建：回显新记录。
  http.post(`${BACKEND_ORIGIN}/directives`, async ({ request }) => {
    const body = (await request.json()) as { content: string; topic?: string }
    return HttpResponse.json(
      okResult(
        mockDirective({
          id: 9,
          content: body.content,
          topic: body.topic ?? 'directive:9f8e7d6c5b4a',
        }),
        '长期指令已保存',
      ),
    )
  }),
  // 更新：id=404 → 404；其余 → version+1。
  http.put(`${BACKEND_ORIGIN}/directives/:directiveId`, async ({ request, params }) => {
    const id = Number(params.directiveId)
    if (id === 404) {
      return HttpResponse.json(errorResult('长期指令不存在（非本域策展行）', 'NOT_FOUND'), {
        status: 404,
      })
    }
    const body = (await request.json()) as { content: string }
    return HttpResponse.json(
      okResult(mockDirective({ id: id + 100, content: body.content, version: 2 }), '长期指令已更新'),
    )
  }),
  // 删除：id=404 → 404；其余成功。
  http.delete(`${BACKEND_ORIGIN}/directives/:directiveId`, ({ params }) => {
    const id = Number(params.directiveId)
    if (id === 404) {
      return HttpResponse.json(errorResult('长期指令不存在（非本域策展行）', 'NOT_FOUND'), {
        status: 404,
      })
    }
    return HttpResponse.json(okResult(null, '长期指令已删除'))
  }),
]

// ---------------------------------------------------------------------------
// facts 域（人物事实——结构化事实 / 证据链 / 更正循环）
// ---------------------------------------------------------------------------

/** 测试用人物事实记录（wire 形态——含证据链）。 */
export function mockFact(overrides: Partial<FactRecord> = {}): FactRecord {
  return {
    id: 1,
    category: 'preference',
    content: '主人喜欢无糖乌龙茶',
    confidence: 0.9,
    status: 'active',
    provenance: 'agent',
    source: 'dream',
    evidence: [
      {
        episode_id: 12,
        quote: '用户：我喜欢喝无糖乌龙茶',
        ts_ms: 1731000000000,
        provenance: 'owner',
      },
    ],
    superseded_by_id: null,
    version: 1,
    created_at: 1731000000000,
    updated_at: 1731000000000,
    ...overrides,
  }
}

/** facts 域默认 handlers（成功形态 + 404 面）。 */
export const factHandlers = [
  // 列表：分页包裹的两条现行事实（不同类别）。
  http.get(`${BACKEND_ORIGIN}/facts`, () => {
    return HttpResponse.json(
      okResult({
        items: [
          mockFact(),
          mockFact({
            id: 2,
            category: 'biography',
            content: '主人在杭州工作',
            confidence: 0.7,
            source: 'manual',
            provenance: 'owner',
            evidence: [],
          }),
        ],
        total: -1,
        page: 1,
        page_size: 200,
        next_cursor: null,
        has_more: false,
      }),
    )
  }),
  // 创建：回显新记录（owner 手工来源）。
  http.post(`${BACKEND_ORIGIN}/facts`, async ({ request }) => {
    const body = (await request.json()) as {
      category: FactRecord['category']
      content: string
      confidence?: number
    }
    return HttpResponse.json(
      okResult(
        mockFact({
          id: 9,
          category: body.category,
          content: body.content,
          confidence: body.confidence ?? 0.8,
          source: 'manual',
          provenance: 'owner',
          evidence: [],
        }),
        '人物事实已保存',
      ),
    )
  }),
  // 更正：id=404 → 404；其余 → version+1 + correction 来源。
  http.put(`${BACKEND_ORIGIN}/facts/:factId`, async ({ request, params }) => {
    const id = Number(params.factId)
    if (id === 404) {
      return HttpResponse.json(errorResult('人物事实不存在', 'NOT_FOUND'), { status: 404 })
    }
    const body = (await request.json()) as { content: string; confidence?: number }
    return HttpResponse.json(
      okResult(
        mockFact({
          id: id + 100,
          content: body.content,
          confidence: body.confidence ?? 0.9,
          source: 'correction',
          version: 2,
        }),
        '人物事实已更正',
      ),
    )
  }),
  // 驳回：id=404 → 404；其余 → rejected。
  http.post(`${BACKEND_ORIGIN}/facts/:factId/reject`, ({ params }) => {
    const id = Number(params.factId)
    if (id === 404) {
      return HttpResponse.json(errorResult('人物事实不存在', 'NOT_FOUND'), { status: 404 })
    }
    return HttpResponse.json(okResult(mockFact({ id, status: 'rejected' }), '人物事实已驳回'))
  }),
  // 删除：id=404 → 404；其余成功。
  http.delete(`${BACKEND_ORIGIN}/facts/:factId`, ({ params }) => {
    const id = Number(params.factId)
    if (id === 404) {
      return HttpResponse.json(errorResult('人物事实不存在', 'NOT_FOUND'), { status: 404 })
    }
    return HttpResponse.json(okResult(null, '人物事实已删除'))
  }),
]

// ---------------------------------------------------------------------------
// skills 域（程序性技能——触发短语 / 原地版本 / 启用状态）
// ---------------------------------------------------------------------------

/** 测试用技能记录（wire 形态——启用 + 梦境挖矿来源）。 */
export function mockSkill(overrides: Partial<SkillRecord> = {}): SkillRecord {
  return {
    id: 1,
    name: '周报流程',
    slug: '周报流程',
    description: '主人说「写周报」时使用：先列三件事再写进展',
    triggers: ['写周报', '本周总结'],
    content: '1. 先列三件事\n2. 再写进展',
    status: 'active',
    source: 'mined',
    confidence: 0.8,
    use_count: 3,
    last_used_ms: 1731000000000,
    version: 1,
    created_at: 1731000000000,
    updated_at: 1731000000000,
    ...overrides,
  }
}

/** skills 域默认 handlers（成功形态 + 404 面）。 */
export const skillHandlers = [
  // 列表：一条启用（mined）+ 一条停用（manual）。
  http.get(`${BACKEND_ORIGIN}/skills`, () => {
    return HttpResponse.json(
      okResult({
        items: [
          mockSkill(),
          mockSkill({
            id: 2,
            name: '日报流程',
            slug: '日报流程',
            description: '主人说「写日报」时使用',
            triggers: ['写日报'],
            content: '1. 写今日完成\n2. 写明日计划',
            status: 'disabled',
            source: 'manual',
            confidence: 0.6,
            use_count: 0,
            last_used_ms: null,
          }),
        ],
        total: -1,
        page: 1,
        page_size: 200,
        next_cursor: null,
        has_more: false,
      }),
    )
  }),
  // 创建：回显新记录（manual 来源）。
  http.post(`${BACKEND_ORIGIN}/skills`, async ({ request }) => {
    const body = (await request.json()) as {
      name: string
      description: string
      triggers: string[]
      content: string
      confidence?: number
    }
    return HttpResponse.json(
      okResult(
        mockSkill({
          id: 9,
          name: body.name,
          slug: body.name.toLowerCase().replaceAll(' ', '-'),
          description: body.description,
          triggers: body.triggers,
          content: body.content,
          confidence: body.confidence ?? 0.6,
          source: 'manual',
          use_count: 0,
          last_used_ms: null,
        }),
        '技能已创建',
      ),
    )
  }),
  // 编辑：id=404 → 404；其余 → version+1。
  http.put(`${BACKEND_ORIGIN}/skills/:skillId`, async ({ request, params }) => {
    const id = Number(params.skillId)
    if (id === 404) {
      return HttpResponse.json(errorResult('技能不存在', 'NOT_FOUND'), { status: 404 })
    }
    const body = (await request.json()) as Record<string, unknown>
    return HttpResponse.json(
      okResult(
        mockSkill({
          id,
          ...(typeof body.name === 'string' ? { name: body.name } : {}),
          ...(typeof body.description === 'string' ? { description: body.description } : {}),
          ...(Array.isArray(body.triggers) ? { triggers: body.triggers as string[] } : {}),
          ...(typeof body.content === 'string' ? { content: body.content } : {}),
          ...(typeof body.confidence === 'number' ? { confidence: body.confidence } : {}),
          version: 2,
        }),
        '技能已保存',
      ),
    )
  }),
  // 启用：id=404 → 404；其余 → active。
  http.post(`${BACKEND_ORIGIN}/skills/:skillId/enable`, ({ params }) => {
    const id = Number(params.skillId)
    if (id === 404) {
      return HttpResponse.json(errorResult('技能不存在', 'NOT_FOUND'), { status: 404 })
    }
    return HttpResponse.json(okResult(mockSkill({ id, status: 'active' }), '技能已启用'))
  }),
  // 停用：id=404 → 404；其余 → disabled。
  http.post(`${BACKEND_ORIGIN}/skills/:skillId/disable`, ({ params }) => {
    const id = Number(params.skillId)
    if (id === 404) {
      return HttpResponse.json(errorResult('技能不存在', 'NOT_FOUND'), { status: 404 })
    }
    return HttpResponse.json(okResult(mockSkill({ id, status: 'disabled' }), '技能已停用'))
  }),
  // 删除：id=404 → 404；其余成功。
  http.delete(`${BACKEND_ORIGIN}/skills/:skillId`, ({ params }) => {
    const id = Number(params.skillId)
    if (id === 404) {
      return HttpResponse.json(errorResult('技能不存在', 'NOT_FOUND'), { status: 404 })
    }
    return HttpResponse.json(okResult(null, '技能已删除'))
  }),
]

// ---------------------------------------------------------------------------
// core-memory 域（自编辑核心记忆——四块自我模型 / 锁定 / 修订历史）
// ---------------------------------------------------------------------------

/** 测试用核心记忆块记录（wire 形态——默认「自我认知」块）。 */
export function mockCoreMemoryBlock(overrides: Partial<CoreMemoryBlock> = {}): CoreMemoryBlock {
  return {
    id: 1,
    block: 'self_identity',
    content: '我是一条会写字的鱼',
    locked: false,
    version: 2,
    updated_by: 'agent',
    last_edited_session_id: 5,
    created_at: 1731000000000,
    updated_at: 1731000000000,
    ...overrides,
  }
}

/** 测试用核心记忆修订记录（wire 形态）。 */
export function mockCoreMemoryRevision(
  overrides: Partial<CoreMemoryRevision> = {},
): CoreMemoryRevision {
  return {
    id: 11,
    block: 'self_identity',
    version: 3,
    content: '我更正了自我认知',
    provenance: 'owner',
    source: 'admin',
    session_id: null,
    ts_ms: 1731000000000,
    created_at: 1731000000000,
    ...overrides,
  }
}

/** 四块默认夹具（固定块序——与后端懒建顺序一致）。 */
function defaultCoreMemoryBlocks(): CoreMemoryBlock[] {
  return [
    mockCoreMemoryBlock(),
    mockCoreMemoryBlock({
      id: 2,
      block: 'owner_profile',
      content: '主人喜欢无糖乌龙茶',
      locked: true,
      version: 3,
      updated_by: 'owner',
      last_edited_session_id: null,
    }),
    mockCoreMemoryBlock({
      id: 3,
      block: 'relationship_story',
      content: '我们一起看过海。',
      version: 2,
      updated_by: 'owner',
      last_edited_session_id: null,
    }),
    mockCoreMemoryBlock({
      id: 4,
      block: 'open_notes',
      content: '',
      version: 1,
      updated_by: 'owner',
      last_edited_session_id: null,
    }),
  ]
}

/** core-memory 域默认 handlers（成功形态 + 非法块键 400 面）。 */
export const coreMemoryHandlers = [
  http.get(`${BACKEND_ORIGIN}/core-memory/blocks`, () => {
    return HttpResponse.json(okResult(defaultCoreMemoryBlocks()))
  }),
  http.put(`${BACKEND_ORIGIN}/core-memory/blocks/:block`, async ({ request, params }) => {
    const block = String(params.block)
    if (!['self_identity', 'owner_profile', 'relationship_story', 'open_notes'].includes(block)) {
      return HttpResponse.json(errorResult('非法核心记忆块键', 'VALIDATION_ERROR'), { status: 400 })
    }
    const body = (await request.json()) as { content: string; locked?: boolean }
    return HttpResponse.json(
      okResult(
        mockCoreMemoryBlock({
          block: block as CoreMemoryBlock['block'],
          content: body.content,
          locked: body.locked ?? false,
          version: 4,
          updated_by: 'owner',
          last_edited_session_id: null,
        }),
        '核心记忆已保存',
      ),
    )
  }),
  http.get(`${BACKEND_ORIGIN}/core-memory/blocks/:block/revisions`, ({ params }) => {
    const block = String(params.block)
    return HttpResponse.json(
      okResult([
        mockCoreMemoryRevision({ id: 12, block: block as CoreMemoryBlock['block'] }),
        mockCoreMemoryRevision({
          id: 11,
          block: block as CoreMemoryBlock['block'],
          version: 2,
          content: '我是一条会写字的鱼',
          provenance: 'agent',
          source: 'chat',
          session_id: 5,
        }),
      ]),
    )
  }),
]

/** 测试用表情包记录（wire 形态）。 */
export function mockSticker(overrides: Partial<StickerRecord> = {}): StickerRecord {
  return {
    id: 1,
    sha256: 'a'.repeat(64),
    content_type: 'image/png',
    size_bytes: 12,
    tags: ['开心'],
    description: '笑着挥手',
    source_note: '自绘素材',
    annotation_status: 'pending',
    annotation_error: null,
    annotation_model_entry: null,
    annotated_at_ms: null,
    created_at: 1731000000000,
    updated_at: 1731000000000,
    data_base64: 'aGVsbG8=',
    ...overrides,
  }
}

/** sticker 域默认 handlers（成功形态；状态机与错误面由测试覆写）。 */
export const stickerHandlers = [
  http.get(`${BACKEND_ORIGIN}/stickers`, () => {
    return HttpResponse.json(okResult([mockSticker()]))
  }),
  http.post(`${BACKEND_ORIGIN}/stickers`, async ({ request }) => {
    const body = (await request.json()) as {
      items: Array<{ image_base64: string; content_type: string; source_note: string; tags: string[]; description: string }>
    }
    const result: StickerBatchUploadResult = {
      created: body.items.map((item, index) =>
        mockSticker({
          id: index + 1,
          sha256: String(index + 1).repeat(64),
          content_type: item.content_type,
          source_note: item.source_note,
          tags: item.tags,
          description: item.description,
          data_base64: item.image_base64,
        }),
      ),
      rejected: [],
    }
    return HttpResponse.json(okResult(result, '表情包上传完成'))
  }),
  http.patch(`${BACKEND_ORIGIN}/stickers/:stickerId`, async ({ request, params }) => {
    const body = (await request.json()) as {
      tags: string[] | null
      description: string | null
      source_note: string | null
    }
    return HttpResponse.json(
      okResult(
        mockSticker({
          id: Number(params.stickerId),
          tags: body.tags ?? ['开心'],
          description: body.description ?? '笑着挥手',
          source_note: body.source_note ?? '自绘素材',
        }),
        '表情包已更新',
      ),
    )
  }),
  http.delete(`${BACKEND_ORIGIN}/stickers/:stickerId`, ({ params }) => {
    const result: StickerBatchDeleteResult = {
      deleted_ids: [Number(params.stickerId)],
      missing_ids: [],
    }
    return HttpResponse.json(okResult(result, '表情包已删除'))
  }),
  http.post(`${BACKEND_ORIGIN}/stickers/:stickerId/annotate`, ({ params }) => {
    const result: StickerAnnotationResult = {
      ok: true,
      record: mockSticker({
        id: Number(params.stickerId),
        annotation_status: 'done',
        annotation_model_entry: 'local-vision',
        annotated_at_ms: 1731000001000,
        description: '笑着挥手',
        tags: ['开心', '挥手'],
      }),
      message: '标注完成',
    }
    return HttpResponse.json(okResult(result, result.message))
  }),
]

/** 共享 msw 服务器（setup.ts 管理生命周期）。 */
export const server = setupServer(
  ...personaHandlers,
  ...memoryHandlers,
  ...usageHandlers,
  ...modelHandlers,
  ...reminderHandlers,
  ...directiveHandlers,
  ...factHandlers,
  ...skillHandlers,
  ...coreMemoryHandlers,
  ...stickerHandlers,
)

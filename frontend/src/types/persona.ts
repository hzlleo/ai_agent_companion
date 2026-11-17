/**
 * persona 域类型契约（与后端 harness.persona 契约对齐的 wire 形态）。
 *
 * 覆盖面：人格记录 / chara card v2 卡片（含世界书条目）/ 状态层（M/S）/
 * 批量删除部分成功形态 / 各写入端点请求体。本文件只承载类型
 * （Unit 文件）——结构化数据一律走这里的接口形态。
 */

// ---------------------------------------------------------------------------
// 值域
// ---------------------------------------------------------------------------

/** 人格可用性状态（enabled 人格构成可寻址集；与主人格标记正交）。 */
export type PersonaStatus = 'active' | 'disabled'

/** 状态层（M=中期心情/关系；S=短期会话状态）。 */
export type PersonaStateLayer = 'M' | 'S'

/** 世界书条目插入位置（v2 字符串形态）。 */
export type CharaBookPosition = 'before_char' | 'after_char'

// ---------------------------------------------------------------------------
// 语音绑定（voice_profile——人格级 TTS 配置覆盖）
// ---------------------------------------------------------------------------

/**
 * 人格语音绑定（镜像后端 VoiceProfileModel）。
 *
 * 绑定语义：人格绑定存在时，其 speaker / speed 覆盖 settings 缺省；
 * 请求级显式参数又覆盖人格绑定（请求 > 人格绑定 > settings 缺省）。
 */
export interface VoiceProfile {
  /** 发音人（CosyVoice speaker id——预置名或部署侧克隆音色名；1-128 字符）。 */
  speaker: string
  /** instruct2 模式风格指令（null = 普通合成；提供时透传 CosyVoice instruct2 模式）。 */
  instruct_text: string | null
  /** 语速（0.5-2.0，默认 1.0——1.0 为自然语速）。 */
  speed: number
}

// ---------------------------------------------------------------------------
// chara card v2 卡片
// ---------------------------------------------------------------------------

/** 世界书条目（单条检索增强设定）。 */
export interface CharaBookEntry {
  /** 主触发关键词列表（任一命中即插入；无触发面的条目应为 constant）。 */
  keys: string[]
  /** 副触发关键词列表（仅 selective=true 时有意义）。 */
  secondary_keys: string[]
  /** 条目正文。 */
  content: string
  /** 插入顺序（0-9999，值越大越靠后插入提示词）。 */
  insertion_order: number
  /** 是否恒插入（免触发）。 */
  constant: boolean
  /** 关键词匹配是否区分大小写。 */
  case_sensitive: boolean
  /** 是否选择性触发（true 时 secondary_keys 参与收敛）。 */
  selective: boolean
  /** 插入位置。 */
  position: CharaBookPosition
  /** 深度槽位（0-20，默认 4；段内值越大越靠段首，0 为段尾槽——最贴近对话末尾；同槽按 insertion_order）。 */
  depth: number
  /** 条目正文是否参与递归扫描面（默认 false；须书级 recursive_scanning=true）。 */
  recursive: boolean
  /** 互斥分组名（null = 不分组；同组命中条目一次组装只注入最高优先级者）。 */
  include_group: string | null
  /** 触发概率（0.0-1.0，默认 1.0 恒注入；非 constant 条目的概率门）。 */
  trigger_probability: number
  /** 注入后冷却回合数（0-50，默认 0 不冷却；会话作用域——重启即复位）。 */
  cooldown_turns: number
}

/** 世界书（检索增强条目集）。 */
export interface CharaBook {
  /** 世界书名称。 */
  name: string
  /** 世界书描述。 */
  description: string
  /** 扫描深度（最近 N 条消息内检索；null = 不限）。 */
  scan_depth: number | null
  /** 条目 token 预算（null = 不限）。 */
  token_budget: number | null
  /** 是否递归扫描。 */
  recursive_scanning: boolean
  /** 条目列表。 */
  entries: CharaBookEntry[]
}

/** chara card v2 人格卡片——长期身份正典。 */
export interface CharaCard {
  /** 人格称呼名（非空，1-128）。 */
  name: string
  /** 人格简介。 */
  description: string
  /** 性格描述。 */
  personality: string
  /** 场景设定。 */
  scenario: string
  /** 开场白。 */
  first_mes: string
  /** 对话示例。 */
  mes_example: string
  /** 系统提示覆写。 */
  system_prompt: string
  /** 历史后指令。 */
  post_history_instructions: string
  /** 世界书（null = 无）。 */
  character_book: CharaBook | null
}

// ---------------------------------------------------------------------------
// 读取面
// ---------------------------------------------------------------------------

/** 人格记录（ORM 行归一；card 随行携带）。 */
export interface PersonaRecord {
  /** 人格主键。 */
  id: number
  /** 注册名（全局唯一）。 */
  name: string
  /** 展示名。 */
  display_name: string
  /** 可用性状态（enabled 人格构成可寻址集）。 */
  status: PersonaStatus
  /** 是否为主人格（全表至多一个 true——DB 唯一键强制）。 */
  is_primary: boolean
  /** 人格卡片。 */
  card: CharaCard
  /** 语音绑定（null = 未绑定——合成回落 settings 缺省发音人）。 */
  voice_profile: VoiceProfile | null
  /** 创建时间（epoch 毫秒）。 */
  created_at: number
  /** 更新时间（epoch 毫秒）。 */
  updated_at: number
}

/** 卡片端点读取面（GET / PUT /personas/{id}/card 响应包裹）。 */
export interface PersonaCardResponse {
  /** 人格主键。 */
  persona_id: number
  /** 人格卡片。 */
  card: CharaCard
  /** 卡片最近更新时间（epoch 毫秒）。 */
  updated_at: number
}

/** 状态层读取面。 */
export interface PersonaState {
  /** 所属人格主键。 */
  persona_id: number
  /** 状态层（M/S）。 */
  layer: PersonaStateLayer
  /** 状态 JSON 文档。 */
  state: Record<string, unknown>
  /** 创建时间（epoch 毫秒）。 */
  created_at: number
  /** 更新时间（epoch 毫秒）。 */
  updated_at: number
}

// ---------------------------------------------------------------------------
// 写入面（请求体）
// ---------------------------------------------------------------------------

/** 创建人格请求。 */
export interface PersonaCreateRequest {
  /** 注册名（全局唯一，1-128）。 */
  name: string
  /** 展示名（null 时回落 card.name）。 */
  display_name: string | null
  /** 可用性状态（默认 disabled）。 */
  status: PersonaStatus
  /** 人格卡片。 */
  card: CharaCard
  /** 语音绑定（null = 不绑定——合成回落 settings 缺省发音人）。 */
  voice_profile: VoiceProfile | null
}

/** 卡片补丁（部分合并面——仅出现字段覆写；character_book 整体替换）。 */
export interface CharaCardPatch {
  /** 人格称呼名。 */
  name?: string
  /** 人格简介。 */
  description?: string
  /** 性格描述。 */
  personality?: string
  /** 场景设定。 */
  scenario?: string
  /** 开场白。 */
  first_mes?: string
  /** 对话示例。 */
  mes_example?: string
  /** 系统提示覆写。 */
  system_prompt?: string
  /** 历史后指令。 */
  post_history_instructions?: string
  /** 世界书（提供时整体替换）。 */
  character_book?: CharaBook | null
}

/** 部分更新人格请求（不含主人格标记——主人格经 set-primary 操作面）。 */
export interface PersonaUpdateRequest {
  /** 新注册名（重命名）。 */
  name?: string
  /** 新展示名。 */
  display_name?: string | null
  /** 新可用性状态。 */
  status?: PersonaStatus
  /** 卡片补丁。 */
  card?: CharaCardPatch
  /** 语音绑定（出现即整体替换——显式 null 清除绑定；缺席则原样保留）。 */
  voice_profile?: VoiceProfile | null
}

/** 复制人格请求。 */
export interface PersonaCopyRequest {
  /** 新注册名（全局唯一）。 */
  new_name: string
  /** 展示名（null 时承接源人格）。 */
  display_name: string | null
  /** 是否深拷贝 M/S 状态层。 */
  include_states: boolean
}

/** 状态层写入请求（state 整体替换）。 */
export interface PersonaStateUpsertRequest {
  /** 状态 JSON 文档。 */
  state: Record<string, unknown>
}

/** 批量删除请求（1-100 个 id）。 */
export interface PersonaBatchDeleteRequest {
  /** 待删除人格主键列表。 */
  ids: number[]
}

/** 批量删除结果单条目（部分成功形态）。 */
export interface PersonaBatchDeleteItem {
  /** 人格主键。 */
  id: number
  /** 是否删除成功。 */
  success: boolean
  /** 失败错误码（成功为 null）。 */
  code: string | null
  /** 提示信息。 */
  message: string
}

/** 批量删除响应（部分成功形态）。 */
export interface PersonaBatchDeleteResponse {
  /** 提交总数。 */
  total: number
  /** 成功数量。 */
  succeeded: number
  /** 失败数量。 */
  failed: number
  /** 逐 id 结果（与提交顺序一致）。 */
  items: PersonaBatchDeleteItem[]
}

/** 列表查询参数（status 过滤 + 游标分页）。 */
export interface PersonaListParams {
  /** 可用性状态过滤（undefined = 全部）。 */
  status?: PersonaStatus
  /** 分页游标（首页 undefined）。 */
  cursor?: string
  /** 页大小（1-200，默认 20）。 */
  limit?: number
}

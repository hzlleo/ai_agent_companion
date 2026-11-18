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

// ---------------------------------------------------------------------------
// 特质组件库（R2b-2——官方预设画廊 / 生成面）
// ---------------------------------------------------------------------------

/** 特质组件分类（六类：温性 / 言语风格 / 习惯癖性 / 价值取向 / 社交风格 / 幽默感）。 */
export type TraitComponentCategory =
  | 'temperament'
  | 'speech'
  | 'quirks'
  | 'values'
  | 'sociality'
  | 'humor'

/** 特质组件（最小可复用人格碎片——预设的组合原子）。 */
export interface TraitComponent {
  /** 组件 id（kebab-case，库内唯一）。 */
  id: string
  /** 组件中文名（画廊芯片展示）。 */
  name_zh: string
  /** 特质描述碎片（落入卡片 personality 的短句）。 */
  description_zh: string
  /** 分类（六类值域）。 */
  category: TraitComponentCategory
  /** 语气指令行（拼入 system 提示碎片）。 */
  tone_directives: string[]
  /** 示例台词（null = 无）。 */
  sample_speech: string | null
  /** 冲突组件 id 列表（同组组合软警告——不拒绝）。 */
  conflicts: string[]
}

/** 预设级卡片覆写片段（全部可选——生成时的部分合并面）。 */
export interface TraitPresetCardOverrides {
  /** 名字后缀建议（拼在基础名之后；null = 不加）。 */
  name_suffix: string | null
  /** 预设级性格补充文本。 */
  personality: string | null
  /** 预设级对话示例补充。 */
  mes_example: string | null
  /** 预设级 system 提示补充行。 */
  system_prompt_fragment: string | null
}

/** 特质预设（组件 id 组合集 + 可选生成覆写——官方库的发行单元）。 */
export interface TraitPreset {
  /** 预设 id（kebab-case，库内唯一）。 */
  id: string
  /** 预设中文名（画廊标题；缺省基础名来源）。 */
  name_zh: string
  /** 一句话标语（画廊副标题）。 */
  tagline_zh: string
  /** 预设描述（生成卡片 personality 的首段碎片）。 */
  description_zh: string
  /** 组件 id 组合集（1-16 个——声明序即组合序）。 */
  components: string[]
  /** 卡片覆写片段（全部可选）。 */
  base_card_overrides: TraitPresetCardOverrides
  /** 建议语音绑定（null = 无建议；导入时随人格落库）。 */
  suggested_voice: VoiceProfile | null
  /** 建议世界书条目（生成时并入卡片世界书）。 */
  suggested_book: CharaBookEntry[]
}

/** 特质库读取面（画廊载荷——预设 + 组件注册表，前端按 id 映射解析芯片）。 */
export interface TraitLibrary {
  /** 官方预设列表（数据文件声明序）。 */
  presets: TraitPreset[]
  /** 全量官方组件列表。 */
  components: TraitComponent[]
}

// ---------------------------------------------------------------------------
// 导入面（R2b-2——内容哈希幂等 + 名称冲突策略）
// ---------------------------------------------------------------------------

/** 导入冲突策略（仅名称冲突时生效——内容同卡恒幂等）。 */
export type PersonaImportConflictStrategy = 'skip' | 'rename' | 'overwrite'

/** 导入未落库原因（imported=false 时的机器可读原因）。 */
export type PersonaImportReason = 'identical_content' | 'name_conflict'

/** 导入请求（JSON 面：官方预设 id 或用户卡片文档 + 冲突策略）。 */
export interface PersonaImportRequest {
  /** 官方预设 id（与 card 恰一提供）。 */
  preset_id: string | null
  /** 用户自有卡片文档（与 preset_id 恰一提供）。 */
  card: CharaCard | null
  /** 名称冲突策略（默认 skip）。 */
  conflict_strategy: PersonaImportConflictStrategy
  /** 期望注册名（null = 卡片名 / 预设名；1-128 字符）。 */
  desired_name: string | null
}

/** 导入结果（单发面——含幂等 / 冲突未导入形态）。 */
export interface PersonaImportResult {
  /** 是否实际落库（false = 幂等跳过或名称冲突未导入）。 */
  imported: boolean
  /** 关联人格主键（新行 / 覆盖行 / 既有行；null = 未关联）。 */
  persona_id: number | null
  /** 未导入原因（null = 已导入）。 */
  reason: PersonaImportReason | null
  /** 生成警告文案列表（预设导入的组件冲突软警告等）。 */
  warnings: string[]
}

/** 批量导入结果单条目（部分成功形态——逐文件）。 */
export interface PersonaImportBatchItem {
  /** 文件名（与提交顺序一致）。 */
  filename: string
  /** 请求是否处理成功（冲突跳过与幂等属合法结果——仍为 true）。 */
  success: boolean
  /** 是否实际导入。 */
  imported: boolean
  /** 关联人格主键（null = 未关联）。 */
  persona_id: number | null
  /** 未导入原因（null = 已导入或处理失败）。 */
  reason: PersonaImportReason | null
  /** 失败错误码（成功为 null）。 */
  code: string | null
  /** 提示信息。 */
  message: string
}

/** 批量导入结果（部分成功形态——逐文件独立事务）。 */
export interface PersonaImportBatchResult {
  /** 提交文件总数。 */
  total: number
  /** 处理成功数（含幂等 / 冲突跳过的合法结果）。 */
  succeeded: number
  /** 处理失败数（解析失败 / 内部错误）。 */
  failed: number
  /** 实际新导入数。 */
  imported_count: number
  /** 逐文件结果（与提交顺序一致）。 */
  items: PersonaImportBatchItem[]
}

// ---------------------------------------------------------------------------
// 长期指令（directives——USER.md 式常备指令；策展记忆 user_note 收窄面）
// ---------------------------------------------------------------------------

/** 长期指令记录（策展记忆行收窄投影——管理面 / 面板读取面）。 */
export interface DirectiveRecord {
  /** 策展记忆主键（删除 / 更新锚点）。 */
  id: number
  /** 主题键（directive: 前缀——同 topic 覆盖为 supersede）。 */
  topic: string
  /** 指令正文。 */
  content: string
  /** 链内版本号（supersede 递增——面板展示 vN）。 */
  version: number
  /** 落库时间（epoch 毫秒）。 */
  created_at: number
  /** 行更新时间（epoch 毫秒）。 */
  updated_at: number
}

/** 长期指令创建请求（同 topic 覆盖为 supersede——「编辑」语义）。 */
export interface DirectiveCreateRequest {
  /** 指令正文（1-500 字符）。 */
  content: string
  /** 主题键（undefined = 按正文派生 directive:<指纹>；显式同 topic 覆盖）。 */
  topic?: string
}

/** 长期指令更新请求（按既有 topic 重写——supersede 链 +1）。 */
export interface DirectiveUpdateRequest {
  /** 指令正文（1-500 字符）。 */
  content: string
}

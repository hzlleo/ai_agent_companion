/**
 * 人格表单模型（typed form model）：人格基础字段 + chara card 卡片
 * 编辑的字段态 / 校验 / 请求体转换。
 *
 * 本工程约定：
 * - 表单态用字符串承载数值与关键词（输入未定形），提交期一次性
 *   收敛为 wire 形态（PersonaCreateRequest / CharaCard）；
 * - 关键词列表（keys / secondary_keys）在表单中以逗号分隔字符串
 *   编辑，收敛期经 ``parseKeys`` 解析；
 * - 校验规则与后端 Pydantic 契约对齐（长度 / 数值界 / 规格一致性），
 *   前端先拦一道，后端 400 形态仍可能到达（服务端校验兜底）。
 */

import type {
  CharaBook,
  CharaBookEntry,
  CharaCard,
  PersonaCreateRequest,
  PersonaRecord,
  PersonaStatus,
  PersonaUpdateRequest,
} from '@/types/persona'

// ---------------------------------------------------------------------------
// 表单态类型
// ---------------------------------------------------------------------------

/** 世界书条目字段态。 */
export interface BookEntryFormState {
  /** 主触发关键词（逗号分隔编辑）。 */
  keys: string
  /** 副触发关键词（逗号分隔编辑；仅 selective 时有意义）。 */
  secondary_keys: string
  /** 条目正文。 */
  content: string
  /** 插入顺序（字符串态，收敛期转数字）。 */
  insertion_order: string
  /** 是否恒插入。 */
  constant: boolean
  /** 关键词是否区分大小写。 */
  case_sensitive: boolean
  /** 是否选择性触发。 */
  selective: boolean
  /** 插入位置。 */
  position: 'before_char' | 'after_char'
}

/** 世界书字段态。 */
export interface CharacterBookFormState {
  /** 世界书名称。 */
  name: string
  /** 世界书描述。 */
  description: string
  /** 扫描深度（字符串态；空 = 不限）。 */
  scan_depth: string
  /** token 预算（字符串态；空 = 不限）。 */
  token_budget: string
  /** 是否递归扫描。 */
  recursive_scanning: boolean
  /** 条目列表。 */
  entries: BookEntryFormState[]
}

/** 卡片字段态。 */
export interface CharaCardFormState {
  /** 人格称呼名。 */
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
  /** 世界书（null = 未启用）。 */
  character_book: CharacterBookFormState | null
}

/** 人格表单完整字段态（基础 + 卡片）。 */
export interface PersonaFormState {
  /** 注册名。 */
  name: string
  /** 展示名（空 = 不设置，回落卡片称呼名）。 */
  display_name: string
  /** 可用性状态。 */
  status: PersonaStatus
  /** 卡片字段态。 */
  card: CharaCardFormState
}

/** 字段错误集（字段路径 → 中文错误文案）。 */
export type PersonaFormErrors = Record<string, string>

// ---------------------------------------------------------------------------
// 工厂（空态）
// ---------------------------------------------------------------------------

/** 新建一条空世界书条目（默认值与后端字段缺省一致）。 */
export function createEmptyEntry(): BookEntryFormState {
  return {
    keys: '',
    secondary_keys: '',
    content: '',
    insertion_order: '100',
    constant: false,
    case_sensitive: false,
    selective: false,
    position: 'after_char',
  }
}

/** 新建空世界书。 */
export function createEmptyBook(): CharacterBookFormState {
  return {
    name: '',
    description: '',
    scan_depth: '',
    token_budget: '',
    recursive_scanning: false,
    entries: [createEmptyEntry()],
  }
}

/** 新建空卡片。 */
export function createEmptyCard(): CharaCardFormState {
  return {
    name: '',
    description: '',
    personality: '',
    scenario: '',
    first_mes: '',
    mes_example: '',
    system_prompt: '',
    post_history_instructions: '',
    character_book: null,
  }
}

/** 新建空人格表单。 */
export function createEmptyForm(): PersonaFormState {
  return {
    name: '',
    display_name: '',
    status: 'disabled',
    card: createEmptyCard(),
  }
}

// ---------------------------------------------------------------------------
// 关键词解析 / 回显
// ---------------------------------------------------------------------------

/** 逗号/中文逗号/换行分隔的关键词串 → 去空去重后的数组。 */
export function parseKeys(input: string): string[] {
  return input
    .split(/[,，\n]/)
    .map((token) => token.trim())
    .filter((token) => token.length > 0)
    .filter((token, index, all) => all.indexOf(token) === index)
}

/** 关键词数组 → 逗号分隔回显串。 */
export function stringifyKeys(keys: string[]): string {
  return keys.join(', ')
}

// ---------------------------------------------------------------------------
// wire → 表单态
// ---------------------------------------------------------------------------

/** 人格记录 → 表单态（编辑页初始化）。 */
export function recordToFormState(record: PersonaRecord): PersonaFormState {
  return {
    name: record.name,
    display_name: record.display_name,
    status: record.status,
    card: cardToFormState(record.card),
  }
}

/** wire 卡片 → 表单态。 */
export function cardToFormState(card: CharaCard): CharaCardFormState {
  return {
    name: card.name,
    description: card.description,
    personality: card.personality,
    scenario: card.scenario,
    first_mes: card.first_mes,
    mes_example: card.mes_example,
    system_prompt: card.system_prompt,
    post_history_instructions: card.post_history_instructions,
    character_book: card.character_book === null ? null : bookToFormState(card.character_book),
  }
}

/** wire 世界书 → 表单态。 */
export function bookToFormState(book: CharaBook): CharacterBookFormState {
  return {
    name: book.name,
    description: book.description,
    scan_depth: book.scan_depth === null ? '' : String(book.scan_depth),
    token_budget: book.token_budget === null ? '' : String(book.token_budget),
    recursive_scanning: book.recursive_scanning,
    entries: book.entries.map((entry) => ({
      keys: stringifyKeys(entry.keys),
      secondary_keys: stringifyKeys(entry.secondary_keys),
      content: entry.content,
      insertion_order: String(entry.insertion_order),
      constant: entry.constant,
      case_sensitive: entry.case_sensitive,
      selective: entry.selective,
      position: entry.position,
    })),
  }
}

// ---------------------------------------------------------------------------
// 校验
// ---------------------------------------------------------------------------

/** 校验可选正整数字段（空 = null 合法）；返回错误文案或 null。 */
function validateOptionalPositiveInt(value: string, fieldLabel: string): string | null {
  if (value.trim() === '') {
    return null
  }
  const parsed = Number(value)
  if (!Number.isInteger(parsed) || parsed < 1) {
    return `${fieldLabel}须为不小于 1 的整数`
  }
  return null
}

/** 校验 insertion_order（0-9999 整数）；返回错误文案或 null。 */
function validateInsertionOrder(value: string): string | null {
  const parsed = Number(value)
  if (value.trim() === '' || !Number.isInteger(parsed)) {
    return '插入顺序须为整数'
  }
  if (parsed < 0 || parsed > 9999) {
    return '插入顺序须在 0-9999 之间'
  }
  return null
}

/**
 * 表单整体校验（与后端契约对齐）：
 * - 注册名 / 卡片称呼名：必填，1-128 字符；
 * - 展示名：可空；填了则 1-128；
 * - 世界书 scan_depth / token_budget：可空（= 不限），填了须 ≥1 整数；
 * - 条目 insertion_order：0-9999 整数；
 * - 条目 selective=false 时不得携带副触发键（后端规格一致性校验）。
 *
 * @returns 错误集（空对象 = 校验通过）。
 */
export function validatePersonaForm(form: PersonaFormState): PersonaFormErrors {
  const errors: PersonaFormErrors = {}

  if (form.name.trim().length < 1 || form.name.trim().length > 128) {
    errors['name'] = '注册名必填，且长度为 1-128 个字符'
  }
  if (form.display_name.trim() !== '' && form.display_name.trim().length > 128) {
    errors['display_name'] = '展示名长度不得超过 128 个字符'
  }
  if (form.card.name.trim().length < 1 || form.card.name.trim().length > 128) {
    errors['card.name'] = '卡片称呼名必填，且长度为 1-128 个字符'
  }

  const book = form.card.character_book
  if (book !== null) {
    const scanDepthError = validateOptionalPositiveInt(book.scan_depth, '扫描深度')
    if (scanDepthError !== null) {
      errors['card.character_book.scan_depth'] = scanDepthError
    }
    const tokenBudgetError = validateOptionalPositiveInt(book.token_budget, 'token 预算')
    if (tokenBudgetError !== null) {
      errors['card.character_book.token_budget'] = tokenBudgetError
    }
    book.entries.forEach((entry, index) => {
      const prefix = `card.character_book.entries.${index}`
      const orderError = validateInsertionOrder(entry.insertion_order)
      if (orderError !== null) {
        errors[`${prefix}.insertion_order`] = orderError
      }
      if (!entry.selective && parseKeys(entry.secondary_keys).length > 0) {
        errors[`${prefix}.secondary_keys`] = '副触发键仅在开启「选择性触发」时可填写'
      }
    })
  }

  return errors
}

// ---------------------------------------------------------------------------
// 表单态 → wire
// ---------------------------------------------------------------------------

/** 表单态条目 → wire 条目。 */
function entryFormToWire(entry: BookEntryFormState): CharaBookEntry {
  return {
    keys: parseKeys(entry.keys),
    secondary_keys: parseKeys(entry.secondary_keys),
    content: entry.content,
    insertion_order: Number(entry.insertion_order),
    constant: entry.constant,
    case_sensitive: entry.case_sensitive,
    selective: entry.selective,
    position: entry.position,
  }
}

/** 可选正整数字符串 → number | null（空串/非法 → null）。 */
function optionalPositiveInt(value: string): number | null {
  const parsed = Number(value)
  if (value.trim() !== '' && Number.isInteger(parsed) && parsed >= 1) {
    return parsed
  }
  return null
}

/** 表单态卡片 → wire 卡片（JSON 预览与 PUT /card 共用）。 */
export function cardFormToWire(card: CharaCardFormState): CharaCard {
  return {
    name: card.name.trim(),
    description: card.description,
    personality: card.personality,
    scenario: card.scenario,
    first_mes: card.first_mes,
    mes_example: card.mes_example,
    system_prompt: card.system_prompt,
    post_history_instructions: card.post_history_instructions,
    character_book:
      card.character_book === null
        ? null
        : {
            name: card.character_book.name,
            description: card.character_book.description,
            scan_depth: optionalPositiveInt(card.character_book.scan_depth),
            token_budget: optionalPositiveInt(card.character_book.token_budget),
            recursive_scanning: card.character_book.recursive_scanning,
            entries: card.character_book.entries.map(entryFormToWire),
          },
  }
}

/** 表单态 → 创建请求体。 */
export function formToCreateRequest(form: PersonaFormState): PersonaCreateRequest {
  return {
    name: form.name.trim(),
    display_name: form.display_name.trim() === '' ? null : form.display_name.trim(),
    status: form.status,
    card: cardFormToWire(form.card),
  }
}

/** 表单态 → 基础字段更新请求体（卡片经 PUT /card 单独整体替换）。 */
export function formToBaseUpdateRequest(form: PersonaFormState): PersonaUpdateRequest {
  return {
    name: form.name.trim(),
    display_name: form.display_name.trim() === '' ? null : form.display_name.trim(),
    status: form.status,
  }
}

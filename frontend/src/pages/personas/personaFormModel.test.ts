import { describe, expect, it } from 'vitest'
import {
  cardFormToWire,
  cardToFormState,
  createEmptyBook,
  createEmptyCard,
  createEmptyEntry,
  createEmptyForm,
  formToBaseUpdateRequest,
  formToCreateRequest,
  parseKeys,
  recordToFormState,
  stringifyKeys,
  validatePersonaForm,
} from './personaFormModel'
import type { PersonaRecord } from '@/types/persona'

/** 测试用记录（wire 形态）。 */
function sampleRecord(): PersonaRecord {
  return {
    id: 1,
    name: 'companion-1',
    display_name: '小助手',
    status: 'active',
    is_active: true,
    card: {
      name: '小助手',
      description: '描述',
      personality: '温柔',
      scenario: '深夜',
      first_mes: '你好',
      mes_example: '<START>',
      system_prompt: 'sys',
      post_history_instructions: 'post',
      character_book: {
        name: '书',
        description: 'desc',
        scan_depth: 4,
        token_budget: 800,
        recursive_scanning: true,
        entries: [
          {
            keys: ['问候', '打招呼'],
            secondary_keys: [],
            content: '正文',
            insertion_order: 100,
            constant: false,
            case_sensitive: true,
            selective: false,
            position: 'after_char',
          },
        ],
      },
    },
    created_at: 1,
    updated_at: 2,
  }
}

describe('parseKeys / stringifyKeys', () => {
  it('逗号 / 中文逗号 / 换行分隔解析 + 去空去重', () => {
    expect(parseKeys('问候, 打招呼\n你好，问候')).toEqual(['问候', '打招呼', '你好'])
  })

  it('空串 → 空数组', () => {
    expect(parseKeys('  ')).toEqual([])
  })

  it('stringifyKeys 逗号拼接', () => {
    expect(stringifyKeys(['a', 'b'])).toBe('a, b')
  })
})

describe('wire → 表单态', () => {
  it('recordToFormState 全字段落地', () => {
    const form = recordToFormState(sampleRecord())
    expect(form.name).toBe('companion-1')
    expect(form.display_name).toBe('小助手')
    expect(form.status).toBe('active')
    expect(form.card.character_book?.scan_depth).toBe('4')
    expect(form.card.character_book?.entries[0]?.keys).toBe('问候, 打招呼')
  })

  it('无世界书 → character_book null', () => {
    const record = sampleRecord()
    record.card.character_book = null
    const form = recordToFormState(record)
    expect(form.card.character_book).toBeNull()
  })

  it('scan_depth null → 空串', () => {
    const card = cardToFormState({ ...sampleRecord().card, character_book: { name: '', description: '', scan_depth: null, token_budget: null, recursive_scanning: false, entries: [] } })
    expect(card.character_book?.scan_depth).toBe('')
    expect(card.character_book?.token_budget).toBe('')
  })
})

describe('validatePersonaForm', () => {
  it('空表单：注册名 + 卡片称呼名报错', () => {
    const errors = validatePersonaForm(createEmptyForm())
    expect(errors['name']).toBeDefined()
    expect(errors['card.name']).toBeDefined()
  })

  it('合法表单通过', () => {
    const form = createEmptyForm()
    form.name = 'companion-x'
    form.card.name = '小夜'
    const errors = validatePersonaForm(form)
    expect(Object.keys(errors)).toHaveLength(0)
  })

  it('展示名超长报错', () => {
    const form = createEmptyForm()
    form.name = 'x'
    form.card.name = 'y'
    form.display_name = 'a'.repeat(129)
    expect(validatePersonaForm(form)['display_name']).toBeDefined()
  })

  it('scan_depth / token_budget 非法（非正整数）报错', () => {
    const form = createEmptyForm()
    form.name = 'x'
    form.card.name = 'y'
    form.card.character_book = createEmptyBook()
    form.card.character_book.scan_depth = '0'
    form.card.character_book.token_budget = 'abc'
    const errors = validatePersonaForm(form)
    expect(errors['card.character_book.scan_depth']).toBeDefined()
    expect(errors['card.character_book.token_budget']).toBeDefined()
  })

  it('insertion_order 界外（<0 / >9999 / 非整数）报错', () => {
    const form = createEmptyForm()
    form.name = 'x'
    form.card.name = 'y'
    form.card.character_book = createEmptyBook()
    form.card.character_book.entries[0]!.insertion_order = '-1'
    expect(validatePersonaForm(form)['card.character_book.entries.0.insertion_order']).toBeDefined()
    form.card.character_book.entries[0]!.insertion_order = '10000'
    expect(validatePersonaForm(form)['card.character_book.entries.0.insertion_order']).toBeDefined()
    form.card.character_book.entries[0]!.insertion_order = '1.5'
    expect(validatePersonaForm(form)['card.character_book.entries.0.insertion_order']).toBeDefined()
  })

  it('selective=false 携带副触发键 → 规格一致性报错', () => {
    const form = createEmptyForm()
    form.name = 'x'
    form.card.name = 'y'
    form.card.character_book = createEmptyBook()
    form.card.character_book.entries[0]!.secondary_keys = '夜谈, 深夜'
    const errors = validatePersonaForm(form)
    expect(errors['card.character_book.entries.0.secondary_keys']).toBeDefined()
  })

  it('selective=true 携带副触发键合法', () => {
    const form = createEmptyForm()
    form.name = 'x'
    form.card.name = 'y'
    form.card.character_book = createEmptyBook()
    form.card.character_book.entries[0]!.selective = true
    form.card.character_book.entries[0]!.secondary_keys = '夜谈'
    const errors = validatePersonaForm(form)
    expect(errors['card.character_book.entries.0.secondary_keys']).toBeUndefined()
  })
})

describe('表单态 → wire', () => {
  it('cardFormToWire：关键词解析 + 可选数值收敛', () => {
    const card = createEmptyCard()
    card.name = '小夜'
    card.character_book = createEmptyBook()
    const entry = card.character_book.entries[0]!
    entry.keys = '问候，打招呼'
    entry.selective = true
    entry.secondary_keys = '夜谈'
    entry.insertion_order = '42'
    card.character_book.scan_depth = '5'
    card.character_book.token_budget = ''
    const wire = cardFormToWire(card)
    expect(wire.name).toBe('小夜')
    expect(wire.character_book?.scan_depth).toBe(5)
    expect(wire.character_book?.token_budget).toBeNull()
    expect(wire.character_book?.entries[0]).toEqual({
      keys: ['问候', '打招呼'],
      secondary_keys: ['夜谈'],
      content: '',
      insertion_order: 42,
      constant: false,
      case_sensitive: false,
      selective: true,
      position: 'after_char',
    })
  })

  it('cardFormToWire：无世界书 → null', () => {
    const wire = cardFormToWire(createEmptyCard())
    expect(wire.character_book).toBeNull()
  })

  it('formToCreateRequest：展示名空白归一 null + trim', () => {
    const form = createEmptyForm()
    form.name = '  companion-x  '
    form.display_name = '   '
    form.card.name = '小夜'
    const payload = formToCreateRequest(form)
    expect(payload.name).toBe('companion-x')
    expect(payload.display_name).toBeNull()
    expect(payload.status).toBe('disabled')
  })

  it('formToBaseUpdateRequest：基础三字段', () => {
    const form = createEmptyForm()
    form.name = 'renamed'
    form.display_name = '新展示名'
    form.status = 'active'
    form.card.name = '小夜'
    expect(formToBaseUpdateRequest(form)).toEqual({
      name: 'renamed',
      display_name: '新展示名',
      status: 'active',
    })
  })
})

describe('工厂', () => {
  it('createEmptyEntry 默认值与后端字段缺省一致', () => {
    expect(createEmptyEntry()).toEqual({
      keys: '',
      secondary_keys: '',
      content: '',
      insertion_order: '100',
      constant: false,
      case_sensitive: false,
      selective: false,
      position: 'after_char',
    })
  })

  it('createEmptyBook 自带一条空条目', () => {
    expect(createEmptyBook().entries).toHaveLength(1)
  })
})

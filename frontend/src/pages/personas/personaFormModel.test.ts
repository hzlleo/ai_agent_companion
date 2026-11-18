import { describe, expect, it } from 'vitest'
import {
  cardFormToWire,
  cardToFormState,
  createEmptyBook,
  createEmptyCard,
  createEmptyEntry,
  createEmptyForm,
  createEmptyVoiceForm,
  formToBaseUpdateRequest,
  formToCreateRequest,
  parseKeys,
  recordToFormState,
  stringifyKeys,
  validatePersonaForm,
  voiceFormToWire,
} from './personaFormModel'
import type { PersonaRecord } from '@/types/persona'

/** 测试用记录（wire 形态）。 */
function sampleRecord(): PersonaRecord {
  return {
    id: 1,
    name: 'companion-1',
    display_name: '小助手',
    status: 'active',
    is_primary: true,
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
            depth: 4,
            recursive: false,
            include_group: null,
            trigger_probability: 1.0,
            cooldown_turns: 0,
          },
        ],
      },
    },
    voice_profile: null,
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
    expect(form.voice).toEqual(createEmptyVoiceForm())
  })

  it('recordToFormState：语音绑定落地表单态', () => {
    const record = sampleRecord()
    record.voice_profile = { speaker: '温柔女声', instruct_text: '语气轻柔', speed: 1.2 }
    const form = recordToFormState(record)
    expect(form.voice).toEqual({ speaker: '温柔女声', instruct_text: '语气轻柔', speed: '1.2' })
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

  it('selective=true 且副触发键全空 → 对称分支报错（镜像后端空副键拒绝）', () => {
    const form = createEmptyForm()
    form.name = 'x'
    form.card.name = 'y'
    form.card.character_book = createEmptyBook()
    form.card.character_book.entries[0]!.selective = true
    form.card.character_book.entries[0]!.secondary_keys = ''
    const errors = validatePersonaForm(form)
    expect(errors['card.character_book.entries.0.secondary_keys']).toBe(
      '开启选择性触发的条目必须填写至少一个副触发关键词',
    )
  })

  it('selective=true 且副触发键仅空白 → 同样报错（parseKeys 去空）', () => {
    const form = createEmptyForm()
    form.name = 'x'
    form.card.name = 'y'
    form.card.character_book = createEmptyBook()
    form.card.character_book.entries[0]!.selective = true
    form.card.character_book.entries[0]!.secondary_keys = '  ,  '
    expect(validatePersonaForm(form)['card.character_book.entries.0.secondary_keys']).toBeDefined()
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

  it('R2b：depth 界外（<0 / >20 / 非整数）报错', () => {
    const form = createEmptyForm()
    form.name = 'x'
    form.card.name = 'y'
    form.card.character_book = createEmptyBook()
    form.card.character_book.entries[0]!.depth = '-1'
    expect(validatePersonaForm(form)['card.character_book.entries.0.depth']).toBeDefined()
    form.card.character_book.entries[0]!.depth = '21'
    expect(validatePersonaForm(form)['card.character_book.entries.0.depth']).toBeDefined()
    form.card.character_book.entries[0]!.depth = '1.5'
    expect(validatePersonaForm(form)['card.character_book.entries.0.depth']).toBeDefined()
    form.card.character_book.entries[0]!.depth = '0'
    expect(validatePersonaForm(form)['card.character_book.entries.0.depth']).toBeUndefined()
  })

  it('R2b：cooldown_turns 界外（<0 / >50 / 非整数）报错', () => {
    const form = createEmptyForm()
    form.name = 'x'
    form.card.name = 'y'
    form.card.character_book = createEmptyBook()
    form.card.character_book.entries[0]!.cooldown_turns = '-1'
    expect(validatePersonaForm(form)['card.character_book.entries.0.cooldown_turns']).toBeDefined()
    form.card.character_book.entries[0]!.cooldown_turns = '51'
    expect(validatePersonaForm(form)['card.character_book.entries.0.cooldown_turns']).toBeDefined()
    form.card.character_book.entries[0]!.cooldown_turns = '2.5'
    expect(validatePersonaForm(form)['card.character_book.entries.0.cooldown_turns']).toBeDefined()
    form.card.character_book.entries[0]!.cooldown_turns = '50'
    expect(validatePersonaForm(form)['card.character_book.entries.0.cooldown_turns']).toBeUndefined()
  })

  it('R2b：trigger_probability 界外 / 非数值报错；0 / 0.5 / 1 合法', () => {
    const form = createEmptyForm()
    form.name = 'x'
    form.card.name = 'y'
    form.card.character_book = createEmptyBook()
    form.card.character_book.entries[0]!.trigger_probability = '-0.1'
    expect(validatePersonaForm(form)['card.character_book.entries.0.trigger_probability']).toBeDefined()
    form.card.character_book.entries[0]!.trigger_probability = '1.1'
    expect(validatePersonaForm(form)['card.character_book.entries.0.trigger_probability']).toBeDefined()
    form.card.character_book.entries[0]!.trigger_probability = 'abc'
    expect(validatePersonaForm(form)['card.character_book.entries.0.trigger_probability']).toBeDefined()
    for (const valid of ['0', '0.5', '1']) {
      form.card.character_book.entries[0]!.trigger_probability = valid
      expect(validatePersonaForm(form)['card.character_book.entries.0.trigger_probability']).toBeUndefined()
    }
  })

  it('R2b：include_group 超长报错；空 = 不分组合法', () => {
    const form = createEmptyForm()
    form.name = 'x'
    form.card.name = 'y'
    form.card.character_book = createEmptyBook()
    form.card.character_book.entries[0]!.include_group = '分'.repeat(65)
    expect(validatePersonaForm(form)['card.character_book.entries.0.include_group']).toBeDefined()
    form.card.character_book.entries[0]!.include_group = '问候变体'
    expect(validatePersonaForm(form)['card.character_book.entries.0.include_group']).toBeUndefined()
  })

  it('R2b：recursive 未开书级总开关报错；开启后合法（镜像后端规格校验）', () => {
    const form = createEmptyForm()
    form.name = 'x'
    form.card.name = 'y'
    form.card.character_book = createEmptyBook()
    form.card.character_book.recursive_scanning = false
    form.card.character_book.entries[0]!.recursive = true
    expect(validatePersonaForm(form)['card.character_book.entries.0.recursive']).toBeDefined()
    form.card.character_book.recursive_scanning = true
    expect(validatePersonaForm(form)['card.character_book.entries.0.recursive']).toBeUndefined()
  })

  it('语音绑定：仅填指令 / 语速而缺发音人 → 报错', () => {
    const form = createEmptyForm()
    form.name = 'x'
    form.card.name = 'y'
    form.voice.instruct_text = '语气轻柔'
    form.voice.speed = '1.2'
    const errors = validatePersonaForm(form)
    expect(errors['voice.speaker']).toBe('填写发音人后才会绑定语音（指令 / 语速不单独生效）')
  })

  it('语音绑定：语速界外（<0.5 / >2.0 / 非数值）→ 报错', () => {
    const form = createEmptyForm()
    form.name = 'x'
    form.card.name = 'y'
    form.voice.speaker = '温柔女声'
    form.voice.speed = '0.4'
    expect(validatePersonaForm(form)['voice.speed']).toBe('语速须在 0.5-2.0 之间')
    form.voice.speed = '2.1'
    expect(validatePersonaForm(form)['voice.speed']).toBe('语速须在 0.5-2.0 之间')
    form.voice.speed = 'abc'
    expect(validatePersonaForm(form)['voice.speed']).toBe('语速须在 0.5-2.0 之间')
  })

  it('语音绑定：风格指令超 2000 字符 → 报错；恰 2000 通过', () => {
    const form = createEmptyForm()
    form.name = 'x'
    form.card.name = 'y'
    form.voice.speaker = '温柔女声'
    form.voice.instruct_text = '柔'.repeat(2001)
    expect(validatePersonaForm(form)['voice.instruct_text']).toBe('风格指令长度不得超过 2000 个字符')
    form.voice.instruct_text = '柔'.repeat(2000)
    expect(validatePersonaForm(form)['voice.instruct_text']).toBeUndefined()
  })

  it('语音绑定：发音人 + 合法语速通过', () => {
    const form = createEmptyForm()
    form.name = 'x'
    form.card.name = 'y'
    form.voice.speaker = '温柔女声'
    form.voice.speed = '1.2'
    const errors = validatePersonaForm(form)
    expect(errors['voice.speaker']).toBeUndefined()
    expect(errors['voice.speed']).toBeUndefined()
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
      depth: 4,
      recursive: false,
      include_group: null,
      trigger_probability: 1,
      cooldown_turns: 0,
    })
  })

  it('cardFormToWire：R2b 高级字段收敛（trim 分组 / 数值收敛）', () => {
    const card = createEmptyCard()
    card.name = '小夜'
    card.character_book = createEmptyBook()
    card.character_book.recursive_scanning = true
    const entry = card.character_book.entries[0]!
    entry.depth = '2'
    entry.recursive = true
    entry.include_group = '  问候变体  '
    entry.trigger_probability = '0.5'
    entry.cooldown_turns = '3'
    const wire = cardFormToWire(card)
    expect(wire.character_book?.entries[0]).toMatchObject({
      depth: 2,
      recursive: true,
      include_group: '问候变体',
      trigger_probability: 0.5,
      cooldown_turns: 3,
    })
  })

  it('cardFormToWire：R2b 非法输入兜底缺省（校验先行，此处兜底形态）', () => {
    const card = createEmptyCard()
    card.name = '小夜'
    card.character_book = createEmptyBook()
    const entry = card.character_book.entries[0]!
    entry.depth = 'abc'
    entry.trigger_probability = ''
    entry.cooldown_turns = 'xyz'
    const wire = cardFormToWire(card)
    expect(wire.character_book?.entries[0]).toMatchObject({
      depth: 4,
      trigger_probability: 1.0,
      cooldown_turns: 0,
    })
  })

  it('recordToFormState：R2b 高级 wire 字段落地表单态', () => {
    const record = sampleRecord()
    const book = record.card.character_book!
    book.entries[0] = {
      ...book.entries[0]!,
      depth: 9,
      recursive: true,
      include_group: '问候变体',
      trigger_probability: 0.3,
      cooldown_turns: 7,
    }
    const form = recordToFormState(record)
    const entry = form.card.character_book?.entries[0]
    expect(entry).toBeDefined()
    expect(entry?.depth).toBe('9')
    expect(entry?.recursive).toBe(true)
    expect(entry?.include_group).toBe('问候变体')
    expect(entry?.trigger_probability).toBe('0.3')
    expect(entry?.cooldown_turns).toBe('7')
  })

  it('cardFormToWire：无世界书 → null', () => {
    const wire = cardFormToWire(createEmptyCard())
    expect(wire.character_book).toBeNull()
  })

  it('formToCreateRequest：展示名空白归一 null + trim + 语音绑定收敛', () => {
    const form = createEmptyForm()
    form.name = '  companion-x  '
    form.display_name = '   '
    form.card.name = '小夜'
    form.voice.speaker = '  温柔女声  '
    form.voice.instruct_text = '  语气轻柔  '
    form.voice.speed = '1.2'
    const payload = formToCreateRequest(form)
    expect(payload.name).toBe('companion-x')
    expect(payload.display_name).toBeNull()
    expect(payload.status).toBe('disabled')
    expect(payload.voice_profile).toEqual({
      speaker: '温柔女声',
      instruct_text: '语气轻柔',
      speed: 1.2,
    })
  })

  it('formToCreateRequest：语音全空 → voice_profile null（不绑定）', () => {
    const form = createEmptyForm()
    form.name = 'x'
    form.card.name = 'y'
    const payload = formToCreateRequest(form)
    expect(payload.voice_profile).toBeNull()
  })

  it('voiceFormToWire：speed 空 / 非法 → 兜底默认 1.0；指令空 → null', () => {
    expect(voiceFormToWire(createEmptyVoiceForm())).toBeNull()
    expect(
      voiceFormToWire({ speaker: '温柔女声', instruct_text: '', speed: '' }),
    ).toEqual({ speaker: '温柔女声', instruct_text: null, speed: 1.0 })
    expect(
      voiceFormToWire({ speaker: '温柔女声', instruct_text: '', speed: 'abc' }),
    ).toEqual({ speaker: '温柔女声', instruct_text: null, speed: 1.0 })
  })

  it('formToBaseUpdateRequest：基础字段 + 语音绑定', () => {
    const form = createEmptyForm()
    form.name = 'renamed'
    form.display_name = '新展示名'
    form.status = 'active'
    form.card.name = '小夜'
    form.voice.speaker = '温柔女声'
    expect(formToBaseUpdateRequest(form)).toEqual({
      name: 'renamed',
      display_name: '新展示名',
      status: 'active',
      voice_profile: { speaker: '温柔女声', instruct_text: null, speed: 1.0 },
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
      depth: '4',
      recursive: false,
      include_group: '',
      trigger_probability: '1',
      cooldown_turns: '0',
    })
  })

  it('createEmptyBook 自带一条空条目', () => {
    expect(createEmptyBook().entries).toHaveLength(1)
  })
})

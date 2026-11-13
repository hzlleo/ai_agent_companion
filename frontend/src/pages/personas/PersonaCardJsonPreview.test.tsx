import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import { PersonaCardJsonPreview } from './PersonaCardJsonPreview'
import { createEmptyCard } from './personaFormModel'

describe('PersonaCardJsonPreview', () => {
  it('空卡片预览：world book 为 null + 字段空串', () => {
    render(<PersonaCardJsonPreview card={createEmptyCard()} />)
    const text = screen.getByTestId('card-json-preview').textContent
    const parsed = JSON.parse(text) as { name: string; character_book: string | null }
    expect(parsed.name).toBe('')
    expect(parsed.character_book).toBeNull()
  })

  it('随表单态更新：称呼名与世界书条目实时反映', () => {
    const card = createEmptyCard()
    card.name = '小夜'
    card.character_book = {
      name: '夜谈',
      description: '',
      scan_depth: '5',
      token_budget: '',
      recursive_scanning: false,
      entries: [
        {
          keys: '问候, 打招呼',
          secondary_keys: '',
          content: '正文',
          insertion_order: '42',
          constant: true,
          case_sensitive: false,
          selective: false,
          position: 'before_char',
        },
      ],
    }
    render(<PersonaCardJsonPreview card={card} />)
    const parsed = JSON.parse(screen.getByTestId('card-json-preview').textContent) as {
      name: string
      character_book: { scan_depth: number | null; token_budget: number | null; entries: Array<{ keys: string[]; secondary_keys: string[]; content: string; insertion_order: number; constant: boolean; case_sensitive: boolean; selective: boolean; position: string }> } | null
    }
    expect(parsed.name).toBe('小夜')
    expect(parsed.character_book?.scan_depth).toBe(5)
    expect(parsed.character_book?.token_budget).toBeNull()
    expect(parsed.character_book?.entries[0]).toEqual({
      keys: ['问候', '打招呼'],
      secondary_keys: [],
      content: '正文',
      insertion_order: 42,
      constant: true,
      case_sensitive: false,
      selective: false,
      position: 'before_char',
    })
  })
})

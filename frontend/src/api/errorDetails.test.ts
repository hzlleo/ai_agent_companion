import { describe, expect, it } from 'vitest'
import { ApiError } from './client'
import { apiErrorFieldSuffix, formatFieldLoc } from './errorDetails'

describe('formatFieldLoc', () => {
  it('剥离 body 前缀 + 点号拼接', () => {
    expect(formatFieldLoc(['body', 'card', 'character_book', 'entries', 2, 'secondary_keys'])).toBe(
      'card.character_book.entries[2].secondary_keys',
    )
  })

  it('query / path 前缀同样剥离；无前缀原样拼接', () => {
    expect(formatFieldLoc(['query', 'persona_id'])).toBe('persona_id')
    expect(formatFieldLoc(['path', 'personaId', 'card'])).toBe('personaId.card')
    expect(formatFieldLoc(['display_name'])).toBe('display_name')
  })

  it('连续下标渲染为多重下标', () => {
    expect(formatFieldLoc(['body', 'rows', 0, 'cells', 3])).toBe('rows[0].cells[3]')
  })
})

describe('apiErrorFieldSuffix', () => {
  it('details.errors 首条 → （路径: 信息）附注', () => {
    const error = new ApiError('请求校验失败', {
      code: 'VALIDATION_ERROR',
      status: 400,
      details: {
        errors: [
          {
            loc: ['body', 'card', 'character_book', 'entries', 2, 'secondary_keys'],
            msg: 'Value error, selective=True 条目必须携带非空 secondary_keys',
            type: 'value_error',
          },
          { loc: ['body', 'name'], msg: 'second error', type: 'value_error' },
        ],
      },
    })
    expect(apiErrorFieldSuffix(error)).toBe(
      '（card.character_book.entries[2].secondary_keys: Value error, selective=True 条目必须携带非空 secondary_keys）',
    )
  })

  it('details 无 errors（null / 非对象 / 空数组 / 形态不符）→ 空串附注', () => {
    expect(apiErrorFieldSuffix(new ApiError('冲突', { details: null }))).toBe('')
    expect(apiErrorFieldSuffix(new ApiError('冲突', { details: 'plain text' }))).toBe('')
    expect(apiErrorFieldSuffix(new ApiError('冲突', { details: { errors: [] } }))).toBe('')
    expect(apiErrorFieldSuffix(new ApiError('冲突', { details: { errors: 'not-array' } }))).toBe('')
    expect(
      apiErrorFieldSuffix(new ApiError('冲突', { details: { errors: [{ loc: 'name', msg: 1 }] } })),
    ).toBe('')
    expect(apiErrorFieldSuffix(new ApiError('冲突', { details: { errors: [{ loc: [], msg: 'x' }] } }))).toBe(
      '',
    )
  })

  it('仅 body 单段 loc（剥离后为空）→ 空串附注', () => {
    expect(
      apiErrorFieldSuffix(
        new ApiError('请求校验失败', {
          details: { errors: [{ loc: ['body'], msg: '整体校验失败' }] },
        }),
      ),
    ).toBe('')
  })
})

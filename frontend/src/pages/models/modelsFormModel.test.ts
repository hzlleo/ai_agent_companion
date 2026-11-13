import { describe, expect, it } from 'vitest'
import {
  createEmptyEntryForm,
  entryToFormState,
  formToCreateRequest,
  formToUpdateRequest,
  isValidHttpBaseUrl,
  validateEntryForm,
} from './modelsFormModel'
import { mockModelEntry } from '@/test/server'

describe('isValidHttpBaseUrl', () => {
  it('http(s) + host 合法；其余非法', () => {
    expect(isValidHttpBaseUrl('http://127.0.0.1:8001/v1')).toBe(true)
    expect(isValidHttpBaseUrl('https://api.example.com/v1')).toBe(true)
    expect(isValidHttpBaseUrl('ftp://example.com')).toBe(false)
    expect(isValidHttpBaseUrl('not-a-url')).toBe(false)
    expect(isValidHttpBaseUrl('http://')).toBe(false)
    expect(isValidHttpBaseUrl('')).toBe(false)
  })
})

describe('validateEntryForm', () => {
  it('合法创建表单通过', () => {
    const form = {
      ...createEmptyEntryForm(),
      name: 'main',
      base_url: 'http://127.0.0.1:8001/v1',
      model: 'companion-main',
    }
    expect(Object.keys(validateEntryForm(form, 'create'))).toHaveLength(0)
  })

  it('缺 name（创建）/ 非法 base_url / 空 model → 逐字段错误', () => {
    const form = createEmptyEntryForm()
    const errors = validateEntryForm(form, 'create')
    expect(errors['name']).toContain('必填')
    expect(errors['base_url']).toContain('http(s)')
    expect(errors['model']).toContain('必填')
  })

  it('编辑模式不校验 name（不可变）', () => {
    const form = { ...createEmptyEntryForm(), base_url: 'http://x/v1', model: 'm' }
    const errors = validateEntryForm(form, 'edit')
    expect(errors['name']).toBeUndefined()
  })

  it('api_key_env 超长 / concurrency 非法 → 错误', () => {
    const form = {
      ...createEmptyEntryForm(),
      name: 'n',
      base_url: 'http://x/v1',
      model: 'm',
      api_key_env: 'K'.repeat(129),
      concurrency: '0',
    }
    const errors = validateEntryForm(form, 'create')
    expect(errors['api_key_env']).toContain('128')
    expect(errors['concurrency']).toContain('不小于 1')
  })

  it('重置勾选时跳过 concurrency 数值校验', () => {
    const form = {
      ...createEmptyEntryForm(),
      name: 'n',
      base_url: 'http://x/v1',
      model: 'm',
      concurrency: 'abc',
      resetConcurrency: true,
    }
    expect(validateEntryForm(form, 'edit')['concurrency']).toBeUndefined()
  })
})

describe('entryToFormState', () => {
  it('读取面 → 表单态（roles 过滤未知值；concurrency null → 空串）', () => {
    const entry = mockModelEntry({
      roles: ['chat', 'ghost-role'],
      concurrency: 4,
    })
    const form = entryToFormState(entry)
    expect(form.name).toBe('main')
    expect(form.roles).toEqual(['chat'])
    expect(form.concurrency).toBe('4')
    expect(form.resetConcurrency).toBe(false)

    const nullConcurrency = entryToFormState(mockModelEntry({ concurrency: null }))
    expect(nullConcurrency.concurrency).toBe('')
  })
})

describe('formToCreateRequest / formToUpdateRequest', () => {
  it('创建：concurrency 留空 → null（全局默认）', () => {
    const form = { ...createEmptyEntryForm(), name: 'n', base_url: 'http://x/v1', model: 'm' }
    const request = formToCreateRequest(form)
    expect(request.concurrency).toBeNull()
    expect(request.roles).toEqual([])
  })

  it('创建：concurrency 填写 → 数值', () => {
    const form = {
      ...createEmptyEntryForm(),
      name: 'n',
      base_url: 'http://x/v1',
      model: 'm',
      concurrency: '8',
    }
    expect(formToCreateRequest(form).concurrency).toBe(8)
  })

  it('更新：concurrency 三态（留空=缺席 / 填写=数值 / 重置=null）且不携带 name', () => {
    const base = { ...createEmptyEntryForm(), name: 'main', base_url: 'http://x/v1', model: 'm' }
    expect(formToUpdateRequest(base)).not.toHaveProperty('concurrency')
    expect(formToUpdateRequest({ ...base, concurrency: '8' }).concurrency).toBe(8)
    expect(formToUpdateRequest({ ...base, resetConcurrency: true }).concurrency).toBeNull()
    expect(formToUpdateRequest(base)).not.toHaveProperty('name')
  })
})

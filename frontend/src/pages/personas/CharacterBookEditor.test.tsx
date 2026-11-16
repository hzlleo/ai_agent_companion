import { describe, expect, it, vi } from 'vitest'
import userEvent from '@testing-library/user-event'
import { render, screen } from '@testing-library/react'
import { useState } from 'react'
import { CharacterBookEditor } from './CharacterBookEditor'
import { createEmptyBook, createEmptyEntry, type CharacterBookFormState } from './personaFormModel'

/**
 * 有状态测试壳：受控编辑器必须由父级回灌 state——React 会在每次
 * onChange 后把受控输入复位回 value prop（mock onChange 不回灌时
 * 逐键输入会被逐键清空），故用真实 useState 壳驱动。
 */
function StatefulEditor({
  initial,
  onChange,
  errors = {},
}: {
  initial: CharacterBookFormState
  onChange?: (book: CharacterBookFormState) => void
  errors?: Record<string, string>
}) {
  const [book, setBook] = useState(initial)
  return (
    <CharacterBookEditor
      book={book}
      errors={errors}
      onChange={(next) => {
        setBook(next)
        onChange?.(next)
      }}
    />
  )
}

describe('CharacterBookEditor', () => {
  it('书级元信息编辑（名称 / 描述 / 扫描深度 / token 预算 / 递归扫描）', async () => {
    const user = userEvent.setup()
    render(<StatefulEditor initial={createEmptyBook()} />)
    await user.type(screen.getByLabelText('世界书名称'), '夜谈设定')
    await user.type(screen.getByLabelText('世界书描述'), '夜间陪伴词库')
    await user.type(screen.getByLabelText(/扫描深度/), '5')
    await user.type(screen.getByLabelText(/token 预算/), '800')
    await user.click(screen.getByRole('checkbox', { name: /递归扫描/ }))
    expect(screen.getByLabelText('世界书名称')).toHaveValue('夜谈设定')
    expect(screen.getByLabelText('世界书描述')).toHaveValue('夜间陪伴词库')
    expect(screen.getByLabelText(/扫描深度/)).toHaveValue('5')
    expect(screen.getByLabelText(/token 预算/)).toHaveValue('800')
    expect(screen.getByRole('checkbox', { name: /递归扫描/ })).toBeChecked()
  })

  it('添加条目（一条 → 两条）', async () => {
    const user = userEvent.setup()
    const onChange = vi.fn()
    render(<StatefulEditor initial={createEmptyBook()} onChange={onChange} />)
    await user.click(screen.getByRole('button', { name: /添加条目/ }))
    expect(onChange).toHaveBeenLastCalledWith(
      expect.objectContaining({ entries: [expect.anything(), expect.anything()] }),
    )
    expect(screen.getByText('条目列表（2）')).toBeInTheDocument()
  })

  it('删除条目（两条 → 一条）', async () => {
    const user = userEvent.setup()
    render(
      <StatefulEditor
        initial={{ ...createEmptyBook(), entries: [createEmptyEntry(), createEmptyEntry()] }}
      />,
    )
    expect(screen.getByText('条目列表（2）')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: '删除条目 2' }))
    expect(screen.getByText('条目列表（1）')).toBeInTheDocument()
  })

  it('仅一条时删除按钮禁用', () => {
    render(<StatefulEditor initial={createEmptyBook()} />)
    expect(screen.getByRole('button', { name: '删除条目 1' })).toBeDisabled()
  })

  it('selective 未开启时副触发键禁用；开启后解除并随关闭清空', async () => {
    const user = userEvent.setup()
    render(<StatefulEditor initial={createEmptyBook()} />)
    expect(screen.getByLabelText(/副触发关键词/)).toBeDisabled()

    await user.click(screen.getByRole('checkbox', { name: '选择性触发' }))
    expect(screen.getByLabelText(/副触发关键词/)).toBeEnabled()
    await user.type(screen.getByLabelText(/副触发关键词/), '夜谈')
    expect(screen.getByLabelText(/副触发关键词/)).toHaveValue('夜谈')

    await user.click(screen.getByRole('checkbox', { name: '选择性触发' }))
    expect(screen.getByLabelText(/副触发关键词/)).toBeDisabled()
    expect(screen.getByLabelText(/副触发关键词/)).toHaveValue('')
  })

  it('校验错误内联呈现（扫描深度 / token 预算 / 插入顺序 / 副键规则）', () => {
    render(
      <StatefulEditor
        initial={createEmptyBook()}
        errors={{
          'card.character_book.scan_depth': '扫描深度须为不小于 1 的整数',
          'card.character_book.token_budget': 'token 预算须为不小于 1 的整数',
          'card.character_book.entries.0.insertion_order': '插入顺序须在 0-9999 之间',
          'card.character_book.entries.0.secondary_keys': '副触发键仅在开启「选择性触发」时可填写',
        }}
      />,
    )
    expect(screen.getByText('扫描深度须为不小于 1 的整数')).toBeInTheDocument()
    expect(screen.getByText('token 预算须为不小于 1 的整数')).toBeInTheDocument()
    expect(screen.getByText('插入顺序须在 0-9999 之间')).toBeInTheDocument()
    expect(screen.getByText('副触发键仅在开启「选择性触发」时可填写')).toBeInTheDocument()
  })

  it('多条目时编辑第二条不影响第一条', async () => {
    const user = userEvent.setup()
    render(
      <StatefulEditor
        initial={{ ...createEmptyBook(), entries: [createEmptyEntry(), createEmptyEntry()] }}
      />,
    )
    await user.type(screen.getAllByLabelText(/主触发关键词/)[1]!, '晚安')
    expect((screen.getAllByLabelText(/主触发关键词/)[0] as HTMLInputElement).value).toBe('')
    expect((screen.getAllByLabelText(/主触发关键词/)[1] as HTMLInputElement).value).toBe('晚安')
  })

  it('条目字段编辑：keys 文本 + 插入顺序 + 插入位置 + 恒插入/区分大小写', async () => {
    const user = userEvent.setup()
    render(<StatefulEditor initial={createEmptyBook()} />)
    await user.type(screen.getByLabelText(/主触发关键词/), '问候，晚安')
    await user.clear(screen.getByLabelText(/插入顺序/))
    await user.type(screen.getByLabelText(/插入顺序/), '42')
    await user.selectOptions(screen.getByLabelText(/插入位置/), 'before_char')
    await user.click(screen.getByRole('checkbox', { name: '恒插入' }))
    await user.click(screen.getByRole('checkbox', { name: '区分大小写' }))
    expect(screen.getByLabelText(/主触发关键词/)).toHaveValue('问候，晚安')
    expect(screen.getByLabelText(/插入顺序/)).toHaveValue('42')
    expect(screen.getByLabelText(/插入位置/)).toHaveValue('before_char')
    expect(screen.getByRole('checkbox', { name: '恒插入' })).toBeChecked()
    expect(screen.getByRole('checkbox', { name: '区分大小写' })).toBeChecked()
    await user.selectOptions(screen.getByLabelText(/插入位置/), 'after_char')
    expect(screen.getByLabelText(/插入位置/)).toHaveValue('after_char')
  })
})

import { describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import { Input } from './Input'
import { Textarea } from './Textarea'
import { Select } from './Select'

describe('Input', () => {
  it('受控值 + onChange', () => {
    const onChange = vi.fn()
    render(<Input value="abc" onChange={onChange} aria-label="名称" />)
    const input = screen.getByLabelText('名称')
    expect(input).toHaveValue('abc')
    fireEvent.change(input, { target: { value: 'abcd' } })
    expect(onChange).toHaveBeenCalledTimes(1)
  })

  it('placeholder 呈现', () => {
    render(<Input value="" onChange={() => undefined} placeholder="请输入" />)
    expect(screen.getByPlaceholderText('请输入')).toBeInTheDocument()
  })
})

describe('Textarea', () => {
  it('受控值 + rows 属性', () => {
    const onChange = vi.fn()
    render(<Textarea value="内容" onChange={onChange} rows={5} aria-label="正文" />)
    const textarea = screen.getByLabelText('正文')
    expect(textarea).toHaveValue('内容')
    expect(textarea).toHaveAttribute('rows', '5')
    fireEvent.change(textarea, { target: { value: '新内容' } })
    expect(onChange).toHaveBeenCalledTimes(1)
  })
})

describe('Select', () => {
  it('选项渲染 + 选中值', () => {
    render(
      <Select value="b" onChange={() => undefined} aria-label="状态">
        <option value="a">启用</option>
        <option value="b">停用</option>
      </Select>,
    )
    expect(screen.getByLabelText('状态')).toHaveValue('b')
    expect(screen.getByRole('option', { name: '启用' })).toBeInTheDocument()
  })
})

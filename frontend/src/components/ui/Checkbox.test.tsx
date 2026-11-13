import { describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { Checkbox } from './Checkbox'

describe('Checkbox', () => {
  it('label 文本渲染 + 显式关联', () => {
    render(<Checkbox checked={false} onChange={() => undefined} label="深拷贝状态层" />)
    const checkbox = screen.getByLabelText('深拷贝状态层')
    expect(checkbox).not.toBeChecked()
  })

  it('checked 受控态呈现', () => {
    render(<Checkbox checked onChange={() => undefined} label="恒插入" />)
    expect(screen.getByLabelText('恒插入')).toBeChecked()
  })

  it('disabled：不可交互', () => {
    const onChange = vi.fn()
    render(<Checkbox checked={false} disabled onChange={onChange} label="副触发键" />)
    const checkbox = screen.getByLabelText<HTMLInputElement>('副触发键')
    expect(checkbox).toBeDisabled()
  })
})

import { describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import { Button } from './Button'

describe('Button', () => {
  it('渲染文本并响应点击', () => {
    const onClick = vi.fn()
    render(<Button onClick={onClick}>保存</Button>)
    fireEvent.click(screen.getByRole('button', { name: '保存' }))
    expect(onClick).toHaveBeenCalledTimes(1)
  })

  it('loading：禁用 + aria-busy', () => {
    const onClick = vi.fn()
    render(
      <Button loading onClick={onClick}>
        提交
      </Button>,
    )
    const button = screen.getByRole('button', { name: /提交/ })
    expect(button).toBeDisabled()
    expect(button).toHaveAttribute('aria-busy', 'true')
    fireEvent.click(button)
    expect(onClick).not.toHaveBeenCalled()
  })

  it('disabled：不响应点击', () => {
    const onClick = vi.fn()
    render(
      <Button disabled onClick={onClick}>
        禁用
      </Button>,
    )
    fireEvent.click(screen.getByRole('button', { name: '禁用' }))
    expect(onClick).not.toHaveBeenCalled()
  })

  it('destructive 变体类', () => {
    render(<Button variant="destructive">删除</Button>)
    expect(screen.getByRole('button', { name: '删除' }).className).toContain(
      'bg-[var(--color-destructive)]',
    )
  })
})


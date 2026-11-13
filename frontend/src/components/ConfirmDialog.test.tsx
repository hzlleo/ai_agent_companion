import { describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import { ConfirmDialog } from './ConfirmDialog'

describe('ConfirmDialog', () => {
  it('确认按钮触发 onConfirm 并显示文案', () => {
    const onConfirm = vi.fn()
    render(
      <ConfirmDialog
        open
        onOpenChange={() => undefined}
        title="删除人格"
        description="确认删除 companion-1？"
        confirmText="删除"
        onConfirm={onConfirm}
      />,
    )
    fireEvent.click(screen.getByRole('button', { name: '删除' }))
    expect(onConfirm).toHaveBeenCalledTimes(1)
    expect(screen.getByText('删除人格')).toBeInTheDocument()
    expect(screen.getByText('确认删除 companion-1？')).toBeInTheDocument()
  })

  it('取消按钮触发 onOpenChange(false)', () => {
    const onOpenChange = vi.fn()
    render(
      <ConfirmDialog
        open
        onOpenChange={onOpenChange}
        title="标题"
        description="说明"
        onConfirm={() => undefined}
      />,
    )
    fireEvent.click(screen.getByRole('button', { name: '取消' }))
    expect(onOpenChange).toHaveBeenCalledWith(false)
  })

  it('loading：确认与取消均禁用', () => {
    const onConfirm = vi.fn()
    render(
      <ConfirmDialog
        open
        onOpenChange={() => undefined}
        title="标题"
        description="说明"
        loading
        onConfirm={onConfirm}
      />,
    )
    expect(screen.getByRole('button', { name: '确认' })).toBeDisabled()
    expect(screen.getByRole('button', { name: '取消' })).toBeDisabled()
  })

  it('dangerous 变体：确认按钮为 destructive 样式', () => {
    render(
      <ConfirmDialog
        open
        onOpenChange={() => undefined}
        title="批量删除"
        description="说明"
        destructive
        confirmText="批量删除"
        onConfirm={() => undefined}
      />,
    )
    expect(screen.getByRole('button', { name: '批量删除' }).className).toContain(
      'bg-[var(--color-destructive)]',
    )
  })

  it('右上角关闭按钮触发 onOpenChange(false)；loading 时不触发', () => {
    const onOpenChange = vi.fn()
    const { rerender } = render(
      <ConfirmDialog
        open
        onOpenChange={onOpenChange}
        title="标题"
        description="说明"
        onConfirm={() => undefined}
      />,
    )
    fireEvent.click(screen.getByRole('button', { name: '关闭对话框' }))
    expect(onOpenChange).toHaveBeenCalledWith(false)

    rerender(
      <ConfirmDialog
        open
        onOpenChange={onOpenChange}
        title="标题"
        description="说明"
        loading
        onConfirm={() => undefined}
      />,
    )
    onOpenChange.mockClear()
    fireEvent.click(screen.getByRole('button', { name: '关闭对话框' }))
    expect(onOpenChange).not.toHaveBeenCalled()
  })
})

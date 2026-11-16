import { describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from './Dialog'
import { Button } from './Button'

describe('Dialog', () => {
  it('open 时渲染标题与内容', () => {
    render(
      <Dialog open onOpenChange={() => undefined}>
        <DialogHeader>
          <DialogTitle>删除人格</DialogTitle>
          <DialogClose onClose={() => undefined} />
        </DialogHeader>
        <DialogContent>确认删除？该操作不可恢复。</DialogContent>
        <DialogFooter>
          <Button>确认</Button>
        </DialogFooter>
      </Dialog>,
    )
    expect(screen.getByRole('dialog')).toBeInTheDocument()
    expect(screen.getByText('删除人格')).toBeInTheDocument()
    expect(screen.getByText('确认删除？该操作不可恢复。')).toBeInTheDocument()
  })

  it('open=false 时不渲染', () => {
    render(
      <Dialog open={false} onOpenChange={() => undefined}>
        <DialogHeader>
          <DialogTitle>标题</DialogTitle>
        </DialogHeader>
        <DialogContent>内容</DialogContent>
      </Dialog>,
    )
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  })

  it('关闭按钮触发回调', () => {
    const onClose = vi.fn()
    render(
      <Dialog open onOpenChange={() => undefined}>
        <DialogHeader>
          <DialogTitle>标题</DialogTitle>
          <DialogClose onClose={onClose} />
        </DialogHeader>
        <DialogContent>内容</DialogContent>
      </Dialog>,
    )
    fireEvent.click(screen.getByRole('button', { name: '关闭对话框' }))
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('标题向面板注册 aria-labelledby', () => {
    render(
      <Dialog open onOpenChange={() => undefined}>
        <DialogHeader>
          <DialogTitle>注册检查</DialogTitle>
        </DialogHeader>
        <DialogContent>内容</DialogContent>
      </Dialog>,
    )
    const dialog = screen.getByRole('dialog')
    const titleId = screen.getByText('注册检查').id
    expect(titleId).not.toBe('')
    expect(dialog).toHaveAttribute('aria-labelledby', titleId)
  })

  it('DialogTitle 脱离 Dialog 独立渲染（无桥接上下文）', () => {
    render(<DialogTitle>独立标题</DialogTitle>)
    expect(screen.getByText('独立标题')).toBeInTheDocument()
  })

  it('标题切换时旧 id 注销、新 id 接管 aria-labelledby', () => {
    const { rerender } = render(
      <Dialog open onOpenChange={() => undefined}>
        <DialogHeader>
          <DialogTitle>标题甲</DialogTitle>
        </DialogHeader>
        <DialogContent>内容</DialogContent>
      </Dialog>,
    )
    const firstTitleId = screen.getByText('标题甲').id
    expect(screen.getByRole('dialog')).toHaveAttribute('aria-labelledby', firstTitleId)

    // 头部换为不同结构（fragment → div）：标题组件真实重挂载，新 id 注册
    // 先于旧 id 注销——旧 id 的 stale 注销分支被命中。
    rerender(
      <Dialog open onOpenChange={() => undefined}>
        <div>
          <DialogTitle>标题乙</DialogTitle>
        </div>
        <DialogContent>内容</DialogContent>
      </Dialog>,
    )
    const secondTitleId = screen.getByText('标题乙').id
    expect(secondTitleId).not.toBe(firstTitleId)
    expect(screen.getByRole('dialog')).toHaveAttribute('aria-labelledby', secondTitleId)
  })
})

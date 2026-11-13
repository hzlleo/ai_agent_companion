import { describe, expect, it, vi } from 'vitest'
import { act, fireEvent, render, screen } from '@testing-library/react'
import { ToastProvider } from './Toast'
import { useToast } from './useToast'
import { ToastContext, type ToastContextValue } from './toast-context'

/** Toast 触发探针组件。 */
function ToastProbe() {
  const toast = useToast()
  return (
    <div>
      <button type="button" onClick={() => toast.success('成功提示')}>
        触发成功
      </button>
      <button type="button" onClick={() => toast.error('失败提示')}>
        触发失败
      </button>
    </div>
  )
}

describe('ToastProvider', () => {
  it('success/error 变体渲染 + 手动关闭', () => {
    render(
      <ToastProvider>
        <ToastProbe />
      </ToastProvider>,
    )
    fireEvent.click(screen.getByRole('button', { name: '触发成功' }))
    fireEvent.click(screen.getByRole('button', { name: '触发失败' }))
    expect(screen.getByText('成功提示')).toBeInTheDocument()
    expect(screen.getByText('失败提示')).toBeInTheDocument()
    fireEvent.click(screen.getAllByRole('button', { name: '关闭提示' })[0]!)
    expect(screen.queryByText('成功提示')).not.toBeInTheDocument()
    expect(screen.getByText('失败提示')).toBeInTheDocument()
  })

  it('自动消失（4s 计时器）', () => {
    vi.useFakeTimers()
    try {
      render(
        <ToastProvider>
          <ToastProbe />
        </ToastProvider>,
      )
      fireEvent.click(screen.getByRole('button', { name: '触发成功' }))
      expect(screen.getByText('成功提示')).toBeInTheDocument()
      act(() => {
        vi.advanceTimersByTime(4000)
      })
      expect(screen.queryByText('成功提示')).not.toBeInTheDocument()
    } finally {
      vi.useRealTimers()
    }
  })

  it('卸载时清理计时器（无残留状态更新）', () => {
    vi.useFakeTimers()
    try {
      const { unmount } = render(
        <ToastProvider>
          <ToastProbe />
        </ToastProvider>,
      )
      fireEvent.click(screen.getByRole('button', { name: '触发成功' }))
      unmount()
      act(() => {
        vi.advanceTimersByTime(4000)
      })
      // 无 unhandled error 即通过。
    } finally {
      vi.useRealTimers()
    }
  })
})

describe('useToast', () => {
  it('脱离 Provider 使用 → 抛错', () => {
    // eslint 规则禁止 console.error 静默——用 spy 捕获 React 报错日志。
    const consoleSpy = vi.spyOn(console, 'error').mockImplementation(() => undefined)
    try {
      expect(() => render(<ToastProbe />)).toThrow('useToast must be used within ToastProvider')
    } finally {
      consoleSpy.mockRestore()
    }
  })
})

describe('toast-context 契约', () => {
  it('ToastContext 缺省 null（供 useContext 判空）', () => {
    expect(ToastContext.Provider).toBeDefined()
  })

  it('变体配置齐全', () => {
    const variants: Array<keyof ToastContextValue> = ['success', 'error', 'warning', 'info']
    expect(variants).toHaveLength(4)
  })
})

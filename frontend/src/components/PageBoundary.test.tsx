import { describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import type { ReactNode } from 'react'
import { PageBoundary } from './PageBoundary'

/** 永不 settle 的 thenable：Suspense 以此为挂起信号（unknown 转换绕过 only-throw-error）。 */
const pendingForever: Promise<void> = new Promise<void>(() => undefined)

/** 挂起组件：用于观察 Suspense 加载态。 */
function NeverResolve(): ReactNode {
  throw pendingForever as unknown
}

/** 渲染即抛错的组件：用于观察错误边界失败态。 */
function AlwaysThrow(): ReactNode {
  throw new Error('chunk 加载失败')
}

describe('PageBoundary', () => {
  it('chunk 拉取期间呈现加载态', () => {
    render(
      <PageBoundary>
        <NeverResolve />
      </PageBoundary>,
    )
    expect(screen.getByText('页面加载中…')).toBeInTheDocument()
    expect(screen.getByRole('status', { name: '加载中' })).toBeInTheDocument()
  })

  it('模块就绪后渲染页面内容', () => {
    render(
      <PageBoundary>
        <div>页面内容</div>
      </PageBoundary>,
    )
    expect(screen.getByText('页面内容')).toBeInTheDocument()
    expect(screen.queryByText('页面加载中…')).not.toBeInTheDocument()
  })

  it('渲染失败时呈现刷新指引而非白屏', () => {
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => undefined)
    render(
      <PageBoundary>
        <AlwaysThrow />
      </PageBoundary>,
    )
    expect(screen.getByRole('alert')).toHaveTextContent('页面加载失败，请刷新页面重试。')
    expect(consoleError).toHaveBeenCalled()
    consoleError.mockRestore()
  })
})

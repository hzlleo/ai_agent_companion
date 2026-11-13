import { describe, expect, it } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import App from './App'

/** 渲染完整应用（browser router + QueryClient + Toast，RTL 自动清理）。 */
function renderApp(): void {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  })
  render(
    <QueryClientProvider client={queryClient}>
      <App />
    </QueryClientProvider>,
  )
}

describe('App', () => {
  it('根路由重定向 /personas：壳导航 + 列表渲染', async () => {
    window.history.replaceState({}, '', '/')
    renderApp()
    expect(await screen.findByText('AI 陪伴伙伴 · 管理台')).toBeInTheDocument()
    expect(await screen.findByTestId('persona-row-1')).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: '人格管理' })).toBeInTheDocument()
  })

  it('记忆路由渲染真实页面（人格选择 + 五页签）', async () => {
    window.history.replaceState({}, '', '/memories')
    renderApp()
    expect(await screen.findByRole('heading', { name: '记忆管理' })).toBeInTheDocument()
    const tablist = screen.getByRole('tablist', { name: '记忆子模块' })
    expect(tablist).toBeInTheDocument()
    expect(screen.getByRole('tab', { name: '情景' })).toBeInTheDocument()
    expect(screen.getByRole('tab', { name: '梦境' })).toBeInTheDocument()
  })

  it('用量与模型路由渲染真实页面', async () => {
    window.history.replaceState({}, '', '/usage')
    renderApp()
    expect(await screen.findByRole('heading', { name: '用量观测' })).toBeInTheDocument()
    window.history.replaceState({}, '', '/models')
    renderApp()
    expect(await screen.findByRole('heading', { name: '模型注册表' })).toBeInTheDocument()
  })

  it('未知路由兜底回 /personas', async () => {
    window.history.replaceState({}, '', '/not-exist')
    renderApp()
    await waitFor(() => expect(window.location.pathname).toBe('/personas'))
  })
})

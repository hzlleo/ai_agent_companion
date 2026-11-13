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

  it('占位路由渲染「建设中」', async () => {
    window.history.replaceState({}, '', '/memories')
    renderApp()
    expect(await screen.findByText('建设中')).toBeInTheDocument()
    expect(screen.getByText('「记忆」模块正在建设中，敬请期待。')).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: '记忆' })).toBeInTheDocument()
  })

  it('未知路由兜底回 /personas', async () => {
    window.history.replaceState({}, '', '/not-exist')
    renderApp()
    await waitFor(() => expect(window.location.pathname).toBe('/personas'))
  })
})

import { describe, expect, it, beforeEach } from 'vitest'
import { configure, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { http, HttpResponse } from 'msw'
import App from './App'
import { BACKEND_ORIGIN, errorResult, server } from '@/test/server'

// 懒加载路由 chunk 在并行 worker / 覆盖率插桩下首求值可能超过默认 1s；
// 仅本文件统一放宽查询等待，断言内容不变。
configure({ asyncUtilTimeout: 5000 })

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
  beforeEach(() => {
    localStorage.clear()
  })

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
    expect(await screen.findByRole('heading', { name: '用量观测' }, { timeout: 5000 })).toBeInTheDocument()
    window.history.replaceState({}, '', '/models')
    renderApp()
    expect(await screen.findByRole('heading', { name: '模型注册表' }, { timeout: 5000 })).toBeInTheDocument()
  })

  it.each([
    ['/personas/presets', 'heading', '预设画廊'],
    ['/personas/new', 'heading', '新建人格'],
    ['/personas/1/edit', 'heading', '编辑人格'],
    ['/stickers', 'heading', '表情包库'],
    ['/canvas', 'heading', '画布'],
    ['/realtime', 'text', '实时语音会话'],
  ] as const)(
    '路由 %s 经懒加载渲染页面',
    { timeout: 5000 },
    async (route, kind, label) => {
      window.history.replaceState({}, '', route)
      renderApp()
      const rendered =
        kind === 'heading'
          ? await screen.findByRole('heading', { name: label })
          : await screen.findByText(label)
      expect(rendered).toBeInTheDocument()
    },
  )

  it('未知路由兜底回 /personas', async () => {
    window.history.replaceState({}, '', '/not-exist')
    renderApp()
    await waitFor(() => expect(window.location.pathname).toBe('/personas'))
  })
})

describe('App 管理面鉴权门', () => {
  beforeEach(() => {
    localStorage.clear()
  })

  it('首个 401 → 令牌门替换应用壳（事件驱动挂载）', async () => {
    window.history.replaceState({}, '', '/')
    server.use(
      http.get(`${BACKEND_ORIGIN}/personas`, () =>
        HttpResponse.json(
          errorResult('管理面鉴权失败：Authorization Bearer token 缺失或不匹配', 'UNAUTHORIZED'),
          { status: 401 },
        ),
      ),
    )
    renderApp()
    expect(await screen.findByText('管理访问令牌')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '保存并进入' })).toBeInTheDocument()
    expect(screen.queryByText('AI 陪伴伙伴 · 管理台')).not.toBeInTheDocument()
  })

  it('非 401 错误照常呈现页面错误面（不误挂令牌门）', async () => {
    window.history.replaceState({}, '', '/')
    server.use(
      http.get(`${BACKEND_ORIGIN}/personas`, () =>
        HttpResponse.json(errorResult('服务暂不可用', 'SERVICE_UNAVAILABLE'), { status: 503 }),
      ),
    )
    renderApp()
    expect(await screen.findByText('AI 陪伴伙伴 · 管理台')).toBeInTheDocument()
    await waitFor(() =>
      expect(screen.queryByText('管理访问令牌')).not.toBeInTheDocument(),
    )
  })

  it('侧栏「注销」：清除本地 token + 回到令牌门', async () => {
    localStorage.setItem('aac_admin_token', 'stale-token')
    window.history.replaceState({}, '', '/')
    renderApp()
    expect(await screen.findByText('AI 陪伴伙伴 · 管理台')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '注销' }))
    expect(await screen.findByText('管理访问令牌')).toBeInTheDocument()
    expect(localStorage.getItem('aac_admin_token')).toBeNull()
  })
})

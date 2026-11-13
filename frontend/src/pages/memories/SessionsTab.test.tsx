import { describe, expect, it } from 'vitest'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { screen, waitFor } from '@testing-library/react'
import { SessionsTab } from './SessionsTab'
import { BACKEND_ORIGIN, errorResult, mockSessionSummary, okResult, server } from '@/test/server'
import { renderWithProviders } from '@/test/utils'

const EMPTY_PAGE = { items: [], total: -1, page: 1, page_size: 20, next_cursor: null, has_more: false }

describe('SessionsTab', () => {
  it('首页加载渲染行 + load more 终止', async () => {
    const user = userEvent.setup()
    renderWithProviders(<SessionsTab personaId={undefined} />)
    expect(await screen.findByTestId('session-row-2')).toBeInTheDocument()
    expect(screen.getByText('周末闲聊')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: '加载更多' }))
    await waitFor(() => expect(screen.getByTestId('session-row-1')).toBeInTheDocument())
    expect(await screen.findByText('已加载全部')).toBeInTheDocument()
  })

  it('空列表 → 空态；失败 → 错误呈现', async () => {
    server.use(
      http.get(`${BACKEND_ORIGIN}/memories/sessions`, () =>
        HttpResponse.json(okResult(EMPTY_PAGE)),
      ),
    )
    renderWithProviders(<SessionsTab personaId={undefined} />)
    expect(await screen.findByText('暂无会话')).toBeInTheDocument()

    server.use(
      http.get(`${BACKEND_ORIGIN}/memories/sessions`, () =>
        HttpResponse.json(errorResult('服务暂不可用', 'UNAVAILABLE'), { status: 503 }),
      ),
    )
    renderWithProviders(<SessionsTab personaId={undefined} />)
    expect(await screen.findByText(/会话列表加载失败：服务暂不可用/)).toBeInTheDocument()
  })

  it('详情：对话框呈现消息列表（seq 升序 + 角色徽章）', async () => {
    const user = userEvent.setup()
    renderWithProviders(<SessionsTab personaId={undefined} />)
    await screen.findByTestId('session-row-2')
    await user.click(withinRow(screen.getByTestId('session-row-2'), '详情'))
    expect(await screen.findByText('会话详情')).toBeInTheDocument()
    expect(await screen.findByTestId('session-message-1')).toBeInTheDocument()
    expect(screen.getByTestId('session-message-3')).toBeInTheDocument()
    expect(screen.getByText('今天好累')).toBeInTheDocument()
    expect(screen.getAllByText('用户').length).toBeGreaterThan(0)
    expect(screen.getAllByText('人格').length).toBeGreaterThan(0)
  })

  it('删除：确认 → 成功 toast', async () => {
    const user = userEvent.setup()
    renderWithProviders(<SessionsTab personaId={undefined} />)
    await screen.findByTestId('session-row-2')
    await user.click(withinRow(screen.getByTestId('session-row-2'), '删除'))
    expect(screen.getByText('删除会话')).toBeInTheDocument()
    expect(screen.getByText(/同事务清理/)).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: '删除' }))
    expect(await screen.findByText(/会话 #2 已删除/)).toBeInTheDocument()
  })

  it('删除：取消确认不触发请求；404 → toast 错误', async () => {
    const user = userEvent.setup()
    renderWithProviders(<SessionsTab personaId={undefined} />)
    await screen.findByTestId('session-row-2')
    await user.click(withinRow(screen.getByTestId('session-row-2'), '删除'))
    await user.click(screen.getByRole('button', { name: '取消' }))
    expect(screen.queryByText('删除会话')).not.toBeInTheDocument()

    server.use(
      http.delete(`${BACKEND_ORIGIN}/memories/sessions/:id`, () =>
        HttpResponse.json(errorResult('会话不存在或不在此人格作用域', 'NOT_FOUND'), { status: 404 }),
      ),
    )
    await user.click(withinRow(screen.getByTestId('session-row-2'), '删除'))
    await user.click(screen.getByRole('button', { name: '删除' }))
    expect(await screen.findByText('会话不存在或不在此人格作用域')).toBeInTheDocument()
  })

  it('详情对话框经「知道了」关闭；错误态重试按钮可达', async () => {
    const user = userEvent.setup()
    renderWithProviders(<SessionsTab personaId={undefined} />)
    await screen.findByTestId('session-row-2')
    await user.click(withinRow(screen.getByTestId('session-row-2'), '详情'))
    expect(await screen.findByTestId('session-message-1')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: '知道了' }))
    await waitFor(() => expect(screen.queryByTestId('session-message-1')).not.toBeInTheDocument())

    server.use(
      http.get(`${BACKEND_ORIGIN}/memories/sessions`, () =>
        HttpResponse.json(errorResult('服务暂不可用', 'UNAVAILABLE'), { status: 503 }),
      ),
    )
    renderWithProviders(<SessionsTab personaId={undefined} />)
    expect(await screen.findByText(/会话列表加载失败：服务暂不可用/)).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: '重试' }))
    await waitFor(() => expect(screen.getByText(/会话列表加载失败/)).toBeInTheDocument())
  })

  it('mockSessionSummary 工厂默认形态（类型面完整性锚点）', () => {
    const summary = mockSessionSummary()
    expect(summary.message_count).toBe(3)
    expect(summary.title).toBe('深夜陪伴对话')
  })
})

/** 在行内按按钮名查找操作按钮。 */
function withinRow(row: HTMLElement, name: string): HTMLElement {
  const button = Array.from(row.querySelectorAll('button')).find((el) => el.textContent === name)
  if (button === undefined) {
    throw new Error(`行内未找到按钮：${name}`)
  }
  return button
}

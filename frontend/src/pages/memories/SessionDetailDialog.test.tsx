import { describe, expect, it } from 'vitest'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { screen } from '@testing-library/react'
import { SessionDetailDialog } from './SessionDetailDialog'
import { BACKEND_ORIGIN, errorResult, mockSessionDetail, okResult, server } from '@/test/server'
import { renderWithProviders } from '@/test/utils'

describe('SessionDetailDialog', () => {
  it('消息列表渲染（角色徽章 + 时间 + 正文）', async () => {
    renderWithProviders(
      <SessionDetailDialog open onOpenChange={() => undefined} personaId={1} sessionId={1} />,
    )
    expect(await screen.findByTestId('session-message-1')).toBeInTheDocument()
    expect(screen.getByTestId('session-message-2')).toBeInTheDocument()
    expect(screen.getByText('就是工作上的事')).toBeInTheDocument()
    expect(screen.getByText('深夜陪伴对话')).toBeInTheDocument()
    expect(screen.getByText('3 条消息')).toBeInTheDocument()
  })

  it('心声块渲染（元数据存在——心声前缀 + 情绪快照小标签）', async () => {
    renderWithProviders(
      <SessionDetailDialog open onOpenChange={() => undefined} personaId={1} sessionId={1} />,
    )
    expect(await screen.findByTestId('session-thought-2')).toHaveTextContent(
      '心声:他听起来真的很累，先别急着给建议。',
    )
    const emotions = screen.getByTestId('session-emotion-2')
    expect(emotions).toHaveTextContent('亲昵 0.70')
    expect(emotions).toHaveTextContent('羞怯 0.55')
  })

  it('无元数据消息不渲染心声块（元数据缺失 → 零附加渲染）', async () => {
    const base = mockSessionDetail({ id: 1 })
    server.use(
      http.get(`${BACKEND_ORIGIN}/memories/sessions/:sessionId`, () =>
        HttpResponse.json(
          okResult(
            mockSessionDetail({
              id: 1,
              messages: base.messages.map(({ meta: _meta, ...rest }) => rest),
            }),
          ),
        ),
      ),
    )
    renderWithProviders(
      <SessionDetailDialog open onOpenChange={() => undefined} personaId={1} sessionId={1} />,
    )
    expect(await screen.findByTestId('session-message-2')).toBeInTheDocument()
    expect(screen.queryByTestId('session-thought-2')).toBeNull()
    expect(screen.queryByTestId('session-emotion-2')).toBeNull()
  })

  it('404 → 中文错误呈现', async () => {
    server.use(
      http.get(`${BACKEND_ORIGIN}/memories/sessions/:id`, () =>
        HttpResponse.json(errorResult('会话不存在或不在此人格作用域', 'NOT_FOUND'), { status: 404 }),
      ),
    )
    renderWithProviders(
      <SessionDetailDialog open onOpenChange={() => undefined} personaId={1} sessionId={404} />,
    )
    expect(await screen.findByText(/会话详情加载失败：会话不存在或不在此人格作用域/)).toBeInTheDocument()
  })

  it('未选择会话（sessionId=0）→ 提示文案', async () => {
    renderWithProviders(
      <SessionDetailDialog open onOpenChange={() => undefined} personaId={1} sessionId={0} />,
    )
    expect(await screen.findByText('未选择会话。')).toBeInTheDocument()
  })

  it('关闭路径：知道了与右上角关闭均触发 onOpenChange(false)', async () => {
    const user = userEvent.setup()
    let closed = false
    renderWithProviders(
      <SessionDetailDialog
        open
        onOpenChange={(open) => {
          closed = !open
        }}
        personaId={1}
        sessionId={1}
      />,
    )
    await screen.findByTestId('session-message-1')
    await user.click(screen.getByRole('button', { name: '知道了' }))
    expect(closed).toBe(true)
    await user.click(screen.getByRole('button', { name: '关闭对话框' }))
    expect(closed).toBe(true)
  })
})

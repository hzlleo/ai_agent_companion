import { describe, expect, it } from 'vitest'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { screen } from '@testing-library/react'
import { SessionDetailDialog } from './SessionDetailDialog'
import { BACKEND_ORIGIN, errorResult, server } from '@/test/server'
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

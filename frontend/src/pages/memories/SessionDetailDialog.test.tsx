import { describe, expect, it } from 'vitest'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { screen, waitFor } from '@testing-library/react'
import { SessionDetailDialog } from './SessionDetailDialog'
import { BACKEND_ORIGIN, errorResult, mockSessionDetail, mockVariant, okResult, server } from '@/test/server'
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

describe('SessionDetailDialog 消息变体（swipes）', () => {
  it('仅 assistant 回合渲染重生成按钮（用户回合无动作面）', async () => {
    renderWithProviders(
      <SessionDetailDialog open onOpenChange={() => undefined} personaId={1} sessionId={1} />,
    )
    await screen.findByTestId('session-message-2')
    expect(screen.getAllByRole('button', { name: '重生成变体' })).toHaveLength(1)
  })

  it('重生成 → 候选轮播（计数 / 前后导航 / 心声）', async () => {
    const user = userEvent.setup()
    renderWithProviders(
      <SessionDetailDialog open onOpenChange={() => undefined} personaId={1} sessionId={1} />,
    )
    await screen.findByTestId('session-message-2')
    await user.click(screen.getByRole('button', { name: '重生成变体' }))

    const carousel = await screen.findByTestId('session-variants-2')
    expect(carousel).toHaveTextContent('1/2')
    expect(screen.getByTestId('session-variant-content-2')).toHaveTextContent('变体回复一')
    expect(carousel).toHaveTextContent('心声:换个更温柔的说法。')

    await user.click(screen.getByRole('button', { name: '下一条变体' }))
    expect(screen.getByTestId('session-variant-counter-2')).toHaveTextContent('2/2')
    expect(screen.getByTestId('session-variant-content-2')).toHaveTextContent('变体回复二')

    await user.click(screen.getByRole('button', { name: '上一条变体' }))
    expect(screen.getByTestId('session-variant-counter-2')).toHaveTextContent('1/2')
  })

  it('选中此条 → 正典回合正文替换（详情重拉生效）', async () => {
    const user = userEvent.setup()
    const withVariants = (source: ReturnType<typeof mockSessionDetail>) => ({
      ...source,
      messages: source.messages.map((message) =>
        message.id === 2
          ? {
              ...message,
              variants: [
                mockVariant(),
                mockVariant({ id: 22, content: '变体回复二' }),
              ],
            }
          : message,
      ),
    })
    let detail = withVariants(mockSessionDetail({ id: 1 }))
    server.use(
      http.get(`${BACKEND_ORIGIN}/memories/sessions/:sessionId`, () => {
        return HttpResponse.json(okResult(detail))
      }),
      http.post(
        `${BACKEND_ORIGIN}/memories/sessions/:sessionId/messages/:messageId/select-variant`,
        async ({ request }) => {
          const body = (await request.json()) as { variant_id: number }
          if (body.variant_id !== 22) {
            throw new Error('选中变体 id 未透传')
          }
          detail = {
            ...detail,
            messages: detail.messages.map((message) =>
              message.id === 2
                ? {
                    ...message,
                    content: '变体回复二',
                    meta: {
                      thought: '换一种说法更贴心。',
                      emotion_deltas: { affection: 0.4 },
                      emotion_snapshot: { affection: 0.8 },
                      selected_variant_id: 22,
                    },
                  }
                : message,
            ),
          }
          return HttpResponse.json(okResult(detail.messages[1], '已选中该变体'))
        },
      ),
    )
    renderWithProviders(
      <SessionDetailDialog open onOpenChange={() => undefined} personaId={1} sessionId={1} />,
    )
    await screen.findByTestId('session-message-2')
    await user.click(screen.getByRole('button', { name: '重生成变体' }))
    await screen.findByTestId('session-variants-2')
    await user.click(screen.getByRole('button', { name: '下一条变体' }))
    await user.click(screen.getByRole('button', { name: '选中此条' }))

    await waitFor(() => {
      expect(screen.getByTestId('session-message-2')).toHaveTextContent('变体回复二')
    })
    expect(screen.getByTestId('session-thought-2')).toHaveTextContent('心声:换一种说法更贴心。')
  })

  it('重生成失败 → 中文错误提示（按钮恢复可用）', async () => {
    const user = userEvent.setup()
    server.use(
      http.post(
        `${BACKEND_ORIGIN}/memories/sessions/:sessionId/messages/:messageId/variants`,
        () => HttpResponse.json(errorResult('模型返回空变体回复', 'LLM_CALL_FAILED'), { status: 500 }),
      ),
    )
    renderWithProviders(
      <SessionDetailDialog open onOpenChange={() => undefined} personaId={1} sessionId={1} />,
    )
    await screen.findByTestId('session-message-2')
    await user.click(screen.getByRole('button', { name: '重生成变体' }))
    expect(await screen.findByText('变体生成失败：模型返回空变体回复')).toBeInTheDocument()
  })
})

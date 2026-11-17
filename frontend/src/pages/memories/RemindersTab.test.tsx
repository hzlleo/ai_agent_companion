import { describe, expect, it } from 'vitest'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { screen, waitFor, within } from '@testing-library/react'
import { RemindersTab } from './RemindersTab'
import { BACKEND_ORIGIN, errorResult, mockReminder, okResult, server } from '@/test/server'
import { renderWithProviders } from '@/test/utils'

const EMPTY_PAGE = { items: [], total: -1, page: 1, page_size: 20, next_cursor: null, has_more: false }


describe('RemindersTab', () => {
  it('首页加载渲染行（状态徽章 / 尝试次数）', async () => {
    renderWithProviders(<RemindersTab personaId={undefined} />)
    expect(await screen.findByTestId('reminder-row-3')).toBeInTheDocument()
    expect(screen.getByText('交水电费')).toBeInTheDocument()
    expect(screen.getAllByText('待投递').length).toBeGreaterThan(0)
    expect(screen.getAllByText('已投递').length).toBeGreaterThan(0)
  })

  it('状态过滤进入查询串', async () => {
    const user = userEvent.setup()
    const holder: { url: URL | null } = { url: null }
    server.use(
      http.get(`${BACKEND_ORIGIN}/reminders`, ({ request }) => {
        holder.url = new URL(request.url)
        return HttpResponse.json(okResult({ ...EMPTY_PAGE, items: [mockReminder({ id: 3 })] }))
      }),
    )
    renderWithProviders(<RemindersTab personaId={2} />)
    await screen.findByTestId('reminder-row-3')
    await user.selectOptions(screen.getByLabelText('状态'), 'pending')
    await waitFor(() => expect(holder.url?.searchParams.get('status')).toBe('pending'))
    expect(holder.url?.searchParams.get('persona_id')).toBe('2')
  })

  it('创建提醒：校验失败提示 + 成功后关闭对话框', async () => {
    const user = userEvent.setup()
    renderWithProviders(<RemindersTab personaId={undefined} />)
    await screen.findByTestId('reminder-row-3')
    await user.click(screen.getByRole('button', { name: '新建提醒' }))
    expect(screen.getAllByText('新建提醒').length).toBeGreaterThan(1)

    await user.click(screen.getByRole('button', { name: '创建' }))
    expect(await screen.findByText(/提醒标题必填/)).toBeInTheDocument()

    await user.type(screen.getByLabelText('提醒标题'), '喝水')
    await user.type(screen.getByLabelText('到期时间'), '2030-01-01T09:00')
    await user.click(screen.getByRole('button', { name: '创建' }))
    expect(await screen.findByText(/提醒已创建（#9 待投递）/)).toBeInTheDocument()
    await waitFor(() => expect(screen.getAllByText('新建提醒')).toHaveLength(1))
  })

  it('取消提醒：已投递行禁用；待投递行确认后成功', async () => {
    const user = userEvent.setup()
    renderWithProviders(<RemindersTab personaId={undefined} />)
    const deliveredRow = await screen.findByTestId('reminder-row-2')
    const cancelButton = Array.from(deliveredRow.querySelectorAll('button')).find(
      (candidate) => candidate.textContent === '取消',
    )
    expect(cancelButton).toBeDefined()
    expect(cancelButton?.disabled).toBe(true)

    await user.click(within(screen.getByTestId('reminder-row-3')).getByRole('button', { name: '取消' }))
    expect(screen.getByRole('button', { name: '取消提醒' })).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: '取消提醒' }))
    expect(await screen.findByText(/提醒 #3 已取消/)).toBeInTheDocument()
  })

  it('批量取消：部分成功结果对话框呈现逐 id 结果', async () => {
    const user = userEvent.setup()
    server.use(
      http.post(`${BACKEND_ORIGIN}/reminders/batch-cancel`, async ({ request }) => {
        const body = (await request.json()) as { ids: number[] }
        const items = body.ids.map((id, index) =>
          index === 0
            ? { id, success: true, code: null, message: '已取消' }
            : { id, success: false, code: 'NOT_FOUND', message: '提醒不存在' },
        )
        const succeeded = items.filter((item) => item.success).length
        return HttpResponse.json(
          okResult({
            total: items.length,
            succeeded,
            failed: items.length - succeeded,
            items,
          }),
        )
      }),
    )
    renderWithProviders(<RemindersTab personaId={undefined} />)
    await screen.findByTestId('reminder-row-3')
    const selectAll = screen.getByRole('checkbox', { name: '全选提醒' })
    await user.click(selectAll)
    await user.click(screen.getByRole('button', { name: /批量取消（已选/ }))
    expect(screen.getByText('批量取消提醒')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: '批量取消' }))
    expect(await screen.findByText('批量删除结果')).toBeInTheDocument()
    expect(screen.getByText(/批量取消完成（成功 1 \/ 失败 1）/)).toBeInTheDocument()
    expect(screen.getByText(/NOT_FOUND/)).toBeInTheDocument()
  })

  it('空列表 → 空态；加载失败 → 错误呈现', async () => {
    server.use(
      http.get(`${BACKEND_ORIGIN}/reminders`, () => HttpResponse.json(okResult(EMPTY_PAGE))),
    )
    renderWithProviders(<RemindersTab personaId={undefined} />)
    expect(await screen.findByText('暂无提醒')).toBeInTheDocument()

    server.use(
      http.get(`${BACKEND_ORIGIN}/reminders`, () =>
        HttpResponse.json(errorResult('当前没有主人格', 'NOT_FOUND'), { status: 404 }),
      ),
    )
    renderWithProviders(<RemindersTab personaId={undefined} />)
    expect(await screen.findByText(/提醒加载失败：当前没有主人格/)).toBeInTheDocument()
  })
})

describe('RemindersTab（补充路径）', () => {
  it('加载更多：追加末页；全选 / 取消全选切换选中集', async () => {
    const user = userEvent.setup()
    renderWithProviders(<RemindersTab personaId={undefined} />)
    await screen.findByTestId('reminder-row-3')
    await user.click(screen.getByRole('checkbox', { name: '全选提醒' }))
    expect(screen.getByRole('button', { name: /批量取消（已选 2）/ })).toBeInTheDocument()
    await user.click(screen.getByRole('checkbox', { name: '全选提醒' }))
    expect(screen.getByRole('button', { name: /批量取消（已选 0）/ })).toBeInTheDocument()
    await user.click(screen.getByRole('checkbox', { name: '选择提醒 3' }))
    await user.click(screen.getByRole('checkbox', { name: '选择提醒 3' }))
    await user.click(screen.getByRole('checkbox', { name: '选择提醒 3' }))
    await user.click(screen.getByRole('button', { name: '加载更多' }))
    expect(await screen.findByTestId('reminder-row-1')).toBeInTheDocument()
    expect(await screen.findByText('已加载全部')).toBeInTheDocument()
  })

  it('取消失败（状态守卫 422）→ 错误 toast', async () => {
    const user = userEvent.setup()
    server.use(
      http.post(`${BACKEND_ORIGIN}/reminders/:reminderId/cancel`, () =>
        HttpResponse.json(errorResult('仅待投递提醒可以取消', 'VALIDATION_ERROR'), { status: 422 }),
      ),
    )
    renderWithProviders(<RemindersTab personaId={undefined} />)
    await screen.findByTestId('reminder-row-3')
    await user.click(within(screen.getByTestId('reminder-row-3')).getByRole('button', { name: '取消' }))
    await user.click(screen.getByRole('button', { name: '取消提醒' }))
    expect(await screen.findByText(/仅待投递提醒可以取消/)).toBeInTheDocument()
  })

  it('批量取消失败 → 错误 toast（结果对话框不开启）', async () => {
    const user = userEvent.setup()
    server.use(
      http.post(`${BACKEND_ORIGIN}/reminders/batch-cancel`, () =>
        HttpResponse.json(errorResult('内部错误', 'INTERNAL_ERROR'), { status: 500 }),
      ),
    )
    renderWithProviders(<RemindersTab personaId={undefined} />)
    await screen.findByTestId('reminder-row-3')
    await user.click(screen.getByRole('checkbox', { name: '选择提醒 3' }))
    await user.click(screen.getByRole('button', { name: /批量取消（已选/ }))
    await user.click(screen.getByRole('button', { name: '批量取消' }))
    expect(await screen.findByText('内部错误')).toBeInTheDocument()
    expect(screen.queryByText('批量删除结果')).not.toBeInTheDocument()
  })

  it('加载失败 → 重试按钮可重新请求', async () => {
    const user = userEvent.setup()
    let calls = 0
    server.use(
      http.get(`${BACKEND_ORIGIN}/reminders`, () => {
        calls += 1
        if (calls === 1) {
          return HttpResponse.json(errorResult('当前没有主人格', 'NOT_FOUND'), { status: 404 })
        }
        return HttpResponse.json(okResult({ ...EMPTY_PAGE, items: [mockReminder({ id: 3 })] }))
      }),
    )
    renderWithProviders(<RemindersTab personaId={undefined} />)
    expect(await screen.findByText(/提醒加载失败：当前没有主人格/)).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: '重试' }))
    expect(await screen.findByTestId('reminder-row-3')).toBeInTheDocument()
  })

  it('创建失败（服务端拒绝）→ 错误 toast；取消按钮关闭对话框', async () => {
    const user = userEvent.setup()
    server.use(
      http.post(`${BACKEND_ORIGIN}/reminders`, () =>
        HttpResponse.json(errorResult('内部错误', 'INTERNAL_ERROR'), { status: 500 }),
      ),
    )
    renderWithProviders(<RemindersTab personaId={undefined} />)
    await screen.findByTestId('reminder-row-3')
    await user.click(screen.getByRole('button', { name: '新建提醒' }))
    await user.type(screen.getByLabelText('提醒标题'), '喝水')
    await user.type(screen.getByLabelText('到期时间'), '2030-01-01T09:00')
    await user.click(screen.getByRole('button', { name: '创建' }))
    expect(await screen.findByText(/内部错误/)).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: '取消' }))
    await waitFor(() => expect(screen.getAllByText('新建提醒')).toHaveLength(1))
  })
})

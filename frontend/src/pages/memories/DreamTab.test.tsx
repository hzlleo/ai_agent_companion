import { describe, expect, it } from 'vitest'
import userEvent from '@testing-library/user-event'
import { fireEvent, screen, waitFor } from '@testing-library/react'
import { http, HttpResponse } from 'msw'
import { DreamTab } from './DreamTab'
import { BACKEND_ORIGIN, errorResult, mockDreamRun, okResult, server } from '@/test/server'
import { renderWithProviders } from '@/test/utils'

describe('DreamTab', () => {
  it('运行日志列表 + load more 终止', async () => {
    const user = userEvent.setup()
    renderWithProviders(<DreamTab personaId={undefined} />)
    expect(await screen.findByTestId('dream-run-row-2')).toBeInTheDocument()
    expect(screen.getByText(/候选 3 \/ 晋升 1 \/ 并入 1/)).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: '加载更多' }))
    await waitFor(() => expect(screen.getByTestId('dream-run-row-1')).toBeInTheDocument())
    expect(await screen.findByText('已加载全部')).toBeInTheDocument()
  })

  it('触发成功：同步结果面板 + 成功 toast', async () => {
    const user = userEvent.setup()
    renderWithProviders(<DreamTab personaId={undefined} />)
    await screen.findByTestId('dream-run-row-2')
    await user.click(screen.getByRole('button', { name: /触发梦境整理/ }))
    expect(await screen.findByText(/梦境整理完成（候选 3 \/ 晋升 1 \/ 并入 1）/)).toBeInTheDocument()
    expect(await screen.findByTestId('dream-run-report-9')).toBeInTheDocument()
    expect(screen.getByText(/本次触发结果/)).toBeInTheDocument()
  })

  it('触发失败（业务结果）：warning toast + 失败原因面板', async () => {
    const user = userEvent.setup()
    server.use(
      http.post(`${BACKEND_ORIGIN}/memories/dream/trigger`, () =>
        HttpResponse.json(
          okResult(
            mockDreamRun({ id: 11, status: 'failed', error: 'LLM 输出不可解析', diary_episode_id: null }),
            '梦境整理失败：LLM 输出不可解析',
          ),
        ),
      ),
    )
    renderWithProviders(<DreamTab personaId={undefined} />)
    await screen.findByTestId('dream-run-row-2')
    await user.click(screen.getByRole('button', { name: /触发梦境整理/ }))
    expect(await screen.findByText(/梦境整理失败：LLM 输出不可解析/)).toBeInTheDocument()
    expect(await screen.findByText(/失败原因：LLM 输出不可解析/)).toBeInTheDocument()
    expect(screen.getByText(/本次运行未生成日记事件/)).toBeInTheDocument()
  })

  it('cfg 覆写校验 + 请求体（仅出现字段生效）', async () => {
    const user = userEvent.setup()
    let capturedBody: Record<string, unknown> | null = null
    server.use(
      http.post(`${BACKEND_ORIGIN}/memories/dream/trigger`, async ({ request }) => {
        capturedBody = (await request.json()) as Record<string, unknown>
        return HttpResponse.json(okResult(mockDreamRun({ id: 9 })))
      }),
    )
    renderWithProviders(<DreamTab personaId={3} />)
    await screen.findByTestId('dream-run-row-2')
    await user.clear(screen.getByLabelText('候选窗口（小时）'))
    await user.type(screen.getByLabelText('候选窗口（小时）'), '999')
    await user.click(screen.getByRole('button', { name: /触发梦境整理/ }))
    expect(await screen.findByText('候选窗口宽度须为 1-720（小时）的整数')).toBeInTheDocument()

    await user.clear(screen.getByLabelText('候选窗口（小时）'))
    fireEvent.change(screen.getByLabelText('晋级阈值'), { target: { value: '-1' } })
    await user.click(screen.getByRole('button', { name: /触发梦境整理/ }))
    expect(await screen.findByText('晋级阈值须为不小于 0 的数值')).toBeInTheDocument()

    fireEvent.change(screen.getByLabelText('晋级阈值'), { target: { value: '2.5' } })
    await user.clear(screen.getByLabelText('候选上限'))
    await user.type(screen.getByLabelText('候选上限'), '0')
    await user.click(screen.getByRole('button', { name: /触发梦境整理/ }))
    expect(await screen.findByText('候选事件上限须为 1-500 的整数')).toBeInTheDocument()

    await user.clear(screen.getByLabelText('候选上限'))
    await user.click(screen.getByRole('checkbox', { name: '生成梦境日记' }))
    await user.click(screen.getByRole('button', { name: /触发梦境整理/ }))
    await waitFor(() => expect(capturedBody).not.toBeNull())
    expect(capturedBody).toEqual({
      persona_id: 3,
      cfg: { min_score: 2.5, diary_enabled: false },
    })
  })

  it('触发请求失败（404 无激活人格）→ toast 错误', async () => {
    const user = userEvent.setup()
    server.use(
      http.post(`${BACKEND_ORIGIN}/memories/dream/trigger`, () =>
        HttpResponse.json(
          errorResult('当前没有激活人格，请先创建并激活一个人格再操作记忆面', 'NOT_FOUND'),
          { status: 404 },
        ),
      ),
    )
    renderWithProviders(<DreamTab personaId={undefined} />)
    await screen.findByTestId('dream-run-row-2')
    await user.click(screen.getByRole('button', { name: /触发梦境整理/ }))
    expect(await screen.findByText('当前没有激活人格，请先创建并激活一个人格再操作记忆面')).toBeInTheDocument()
  })

  it('运行日志错误态重试可达；详情对话框经「知道了」关闭', async () => {
    const user = userEvent.setup()
    server.use(
      http.get(`${BACKEND_ORIGIN}/memories/dream/runs`, () =>
        HttpResponse.json(errorResult('服务暂不可用', 'UNAVAILABLE'), { status: 503 }),
      ),
    )
    renderWithProviders(<DreamTab personaId={undefined} />)
    expect(await screen.findByText(/运行日志加载失败：服务暂不可用/)).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: '重试' }))
    await waitFor(() => expect(screen.getByText(/运行日志加载失败/)).toBeInTheDocument())

    server.use(
      http.get(`${BACKEND_ORIGIN}/memories/dream/runs`, () =>
        HttpResponse.json(
          okResult({
            items: [mockDreamRun({ id: 2 })],
            total: -1,
            page: 1,
            page_size: 20,
            next_cursor: null,
            has_more: false,
          }),
        ),
      ),
    )
    renderWithProviders(<DreamTab personaId={undefined} />)
    await screen.findByTestId('dream-run-row-2')
    await user.click(withinRow(screen.getByTestId('dream-run-row-2'), '详情'))
    expect(await screen.findByText('梦境运行详情')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: '知道了' }))
    await waitFor(() => expect(screen.queryByText('梦境运行详情')).not.toBeInTheDocument())
  })

  it('运行详情对话框打开（详情端点拉取）', async () => {
    const user = userEvent.setup()
    renderWithProviders(<DreamTab personaId={undefined} />)
    await screen.findByTestId('dream-run-row-2')
    await user.click(withinRow(screen.getByTestId('dream-run-row-2'), '详情'))
    expect(await screen.findByText('梦境运行详情')).toBeInTheDocument()
    expect(await screen.findByTestId('dream-decision-1')).toBeInTheDocument()
  })

  it('空日志 → 空态；失败 → 错误呈现', async () => {
    server.use(
      http.get(`${BACKEND_ORIGIN}/memories/dream/runs`, () =>
        HttpResponse.json(
          okResult({ items: [], total: -1, page: 1, page_size: 20, next_cursor: null, has_more: false }),
        ),
      ),
    )
    renderWithProviders(<DreamTab personaId={undefined} />)
    expect(await screen.findByText('暂无运行记录')).toBeInTheDocument()

    server.use(
      http.get(`${BACKEND_ORIGIN}/memories/dream/runs`, () =>
        HttpResponse.json(errorResult('服务暂不可用', 'UNAVAILABLE'), { status: 503 }),
      ),
    )
    renderWithProviders(<DreamTab personaId={undefined} />)
    expect(await screen.findByText(/运行日志加载失败：服务暂不可用/)).toBeInTheDocument()
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

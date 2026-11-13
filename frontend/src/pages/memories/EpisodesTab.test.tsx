import { describe, expect, it } from 'vitest'
import userEvent from '@testing-library/user-event'
import { fireEvent, screen, waitFor } from '@testing-library/react'
import { http, HttpResponse } from 'msw'
import { EpisodesTab } from './EpisodesTab'
import { BACKEND_ORIGIN, errorResult, mockEpisode, okResult, server } from '@/test/server'
import { renderWithProviders } from '@/test/utils'

const EMPTY_PAGE = { items: [], total: -1, page: 1, page_size: 20, next_cursor: null, has_more: false }

describe('EpisodesTab', () => {
  it('首页加载渲染行（载荷摘要摊平 + 类型 / 可信度徽章）', async () => {
    renderWithProviders(<EpisodesTab personaId={undefined} />)
    expect(await screen.findByTestId('episode-row-3')).toBeInTheDocument()
    expect(screen.getByTestId('episode-row-2')).toBeInTheDocument()
    // 「值班任务」同时命中过滤下拉 option 与类型徽章——按多命中断言。
    expect(screen.getAllByText('值班任务').length).toBeGreaterThan(1)
    expect(screen.getByText('巡检报告 — 一切正常')).toBeInTheDocument()
    // 首页仅一行 chat（另一行在第二页）。
    expect(screen.getAllByText('用户：今天好累 / 人格：辛苦了，想聊聊吗？').length).toBe(1)
    expect(screen.getAllByText('主人').length).toBeGreaterThan(0)
    // 「智能体」同时命中过滤下拉 option 与可信度徽章——按多命中断言。
    expect(screen.getAllByText('智能体').length).toBeGreaterThan(1)
    expect(screen.getByRole('button', { name: '加载更多' })).toBeInTheDocument()
  })

  it('加载更多：追加末页并终止翻页', async () => {
    const user = userEvent.setup()
    renderWithProviders(<EpisodesTab personaId={undefined} />)
    await screen.findByTestId('episode-row-3')
    await user.click(screen.getByRole('button', { name: '加载更多' }))
    await waitFor(() => expect(screen.getByTestId('episode-row-1')).toBeInTheDocument())
    expect(await screen.findByText('已加载全部')).toBeInTheDocument()
  })

  it('kind 过滤：仅 duty 行呈现（查询串携带 kind）', async () => {
    const user = userEvent.setup()
    let capturedKind: string | null = null
    server.use(
      http.get(`${BACKEND_ORIGIN}/memories/episodes`, ({ request }) => {
        capturedKind = new URL(request.url).searchParams.get('kind')
        return HttpResponse.json(
          okResult({ ...EMPTY_PAGE, items: [mockEpisode({ id: 3, kind: 'duty' })] }),
        )
      }),
    )
    renderWithProviders(<EpisodesTab personaId={undefined} />)
    await screen.findByTestId('episode-row-3')
    await user.selectOptions(screen.getByLabelText('类型'), 'duty')
    await waitFor(() => expect(capturedKind).toBe('duty'))
  })

  it('provenance + 时间范围过滤进入查询串', async () => {
    const user = userEvent.setup()
    let capturedUrl: URL | null = null
    server.use(
      http.get(`${BACKEND_ORIGIN}/memories/episodes`, ({ request }) => {
        capturedUrl = new URL(request.url)
        return HttpResponse.json(
          okResult({ ...EMPTY_PAGE, items: [mockEpisode({ id: 3 })] }),
        )
      }),
    )
    renderWithProviders(<EpisodesTab personaId={undefined} />)
    await screen.findByTestId('episode-row-3')
    await user.selectOptions(screen.getByLabelText('可信度'), 'owner')
    await waitFor(() => expect(capturedUrl?.searchParams.get('provenance')).toBe('owner'))
    fireEvent.change(screen.getByLabelText('时间范围'), { target: { value: '2026-01-01T00:00' } })
    await waitFor(() => expect(capturedUrl?.searchParams.get('start_ms') !== null).toBe(true))
  })

  it('空列表 → 空态', async () => {
    server.use(
      http.get(`${BACKEND_ORIGIN}/memories/episodes`, () =>
        HttpResponse.json(okResult(EMPTY_PAGE)),
      ),
    )
    renderWithProviders(<EpisodesTab personaId={undefined} />)
    expect(await screen.findByText('暂无情景事件')).toBeInTheDocument()
  })

  it('加载失败 → 错误呈现 + 重试恢复', async () => {
    const user = userEvent.setup()
    server.use(
      http.get(`${BACKEND_ORIGIN}/memories/episodes`, () =>
        HttpResponse.json(errorResult('服务暂不可用', 'UNAVAILABLE'), { status: 503 }),
      ),
    )
    renderWithProviders(<EpisodesTab personaId={undefined} />)
    expect(await screen.findByText(/情景事件加载失败：服务暂不可用/)).toBeInTheDocument()
    server.use(
      http.get(`${BACKEND_ORIGIN}/memories/episodes`, () =>
        HttpResponse.json(okResult({ ...EMPTY_PAGE, items: [mockEpisode({ id: 1 })] })),
      ),
    )
    await user.click(screen.getByRole('button', { name: '重试' }))
    await waitFor(() => expect(screen.getByTestId('episode-row-1')).toBeInTheDocument())
  })

  it('单条删除：确认 → 成功 toast', async () => {
    const user = userEvent.setup()
    renderWithProviders(<EpisodesTab personaId={undefined} />)
    await screen.findByTestId('episode-row-2')
    await user.click(withinRow(screen.getByTestId('episode-row-2'), '删除'))
    expect(screen.getByText('删除情景事件')).toBeInTheDocument()
    expect(screen.getByText(/append-only 日志/)).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: '删除' }))
    expect(await screen.findByText(/情景事件 #2 已删除/)).toBeInTheDocument()
  })

  it('单条删除 404 → toast 错误', async () => {
    const user = userEvent.setup()
    server.use(
      http.delete(`${BACKEND_ORIGIN}/memories/episodes/:id`, () =>
        HttpResponse.json(errorResult('事件不存在或不在此人格作用域', 'NOT_FOUND'), { status: 404 }),
      ),
    )
    renderWithProviders(<EpisodesTab personaId={undefined} />)
    await screen.findByTestId('episode-row-2')
    await user.click(withinRow(screen.getByTestId('episode-row-2'), '删除'))
    await user.click(screen.getByRole('button', { name: '删除' }))
    expect(await screen.findByText('事件不存在或不在此人格作用域')).toBeInTheDocument()
  })

  it('批量删除：部分成功结果对话框逐项呈现', async () => {
    const user = userEvent.setup()
    renderWithProviders(<EpisodesTab personaId={undefined} />)
    await screen.findByTestId('episode-row-3')
    // 加载第二页取事件 1（批量删除 handler 对 id=1 记 NOT_FOUND 失败）。
    await user.click(screen.getByRole('button', { name: '加载更多' }))
    await screen.findByTestId('episode-row-1')
    await user.click(screen.getByLabelText('选择事件 3'))
    await user.click(screen.getByLabelText('选择事件 1'))
    await user.click(screen.getByRole('button', { name: /批量删除（已选 2）/ }))
    expect(screen.getByText('批量删除情景事件')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: '批量删除' }))
    expect(await screen.findByText('批量删除结果')).toBeInTheDocument()
    expect(screen.getByText('事件不存在或不在此人格作用域')).toBeInTheDocument()
    expect(screen.getByText(/成功 1 \/ 失败 1/)).toBeInTheDocument()
  })

  it('全选开关作用于已加载行', async () => {
    const user = userEvent.setup()
    renderWithProviders(<EpisodesTab personaId={undefined} />)
    await screen.findByTestId('episode-row-3')
    await user.click(screen.getByLabelText('全选当前已加载事件'))
    expect(screen.getByRole('button', { name: /批量删除（已选 2）/ })).toBeInTheDocument()
    await user.click(screen.getByLabelText('全选当前已加载事件'))
    expect(screen.getByRole('button', { name: /批量删除（已选 0）/ })).toBeDisabled()
  })

  it('打开手工注入对话框', async () => {
    const user = userEvent.setup()
    renderWithProviders(<EpisodesTab personaId={undefined} />)
    await screen.findByTestId('episode-row-3')
    await user.click(screen.getByRole('button', { name: /手工注入/ }))
    expect(await screen.findByRole('heading', { name: '手工注入情景事件' })).toBeInTheDocument()
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

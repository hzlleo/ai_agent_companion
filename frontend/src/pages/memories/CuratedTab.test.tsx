import { describe, expect, it } from 'vitest'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { screen, waitFor } from '@testing-library/react'
import { CuratedTab } from './CuratedTab'
import { BACKEND_ORIGIN, errorResult, mockCurated, okResult, server } from '@/test/server'
import { renderWithProviders } from '@/test/utils'

const EMPTY_PAGE = { items: [], total: -1, page: 1, page_size: 20, next_cursor: null, has_more: false }

describe('CuratedTab', () => {
  it('首页加载渲染行（状态徽章 + 版本链）', async () => {
    renderWithProviders(<CuratedTab personaId={undefined} />)
    expect(await screen.findByTestId('curated-row-3')).toBeInTheDocument()
    expect(screen.getByTestId('curated-row-2')).toBeInTheDocument()
    expect(screen.getAllByText('现行').length).toBeGreaterThan(0)
    expect(screen.getAllByText('已取代').length).toBeGreaterThan(0)
    // 同主题两行（active + superseded）→ 主题键多命中。
    expect(screen.getAllByText('sleep-pattern').length).toBe(2)
    // 首页两行均为 relationship（memory 行在第二页）——类型徽章 + 下拉 option 多命中。
    expect(screen.getAllByText('关系状态').length).toBeGreaterThan(2)
  })

  it('加载更多：追加末页并终止翻页', async () => {
    const user = userEvent.setup()
    renderWithProviders(<CuratedTab personaId={undefined} />)
    await screen.findByTestId('curated-row-3')
    await user.click(screen.getByRole('button', { name: '加载更多' }))
    await waitFor(() => expect(screen.getByTestId('curated-row-1')).toBeInTheDocument())
    expect(await screen.findByText('已加载全部')).toBeInTheDocument()
  })

  it('kind / status / topic 过滤进入查询串', async () => {
    const user = userEvent.setup()
    let capturedUrl: URL | null = null
    server.use(
      http.get(`${BACKEND_ORIGIN}/memories/curated`, ({ request }) => {
        capturedUrl = new URL(request.url)
        return HttpResponse.json(
          okResult({ ...EMPTY_PAGE, items: [mockCurated({ id: 3 })] }),
        )
      }),
    )
    renderWithProviders(<CuratedTab personaId={undefined} />)
    await screen.findByTestId('curated-row-3')
    await user.selectOptions(screen.getByLabelText('类型'), 'memory')
    await user.selectOptions(screen.getByLabelText('状态'), 'active')
    await user.type(screen.getByLabelText('主题键'), 'food')
    await waitFor(() => expect(capturedUrl?.searchParams.get('kind')).toBe('memory'))
    await waitFor(() => expect(capturedUrl?.searchParams.get('status')).toBe('active'))
    await waitFor(() => expect(capturedUrl?.searchParams.get('topic')).toBe('food'))
  })

  it('空列表 → 空态；加载失败 → 错误呈现', async () => {
    server.use(
      http.get(`${BACKEND_ORIGIN}/memories/curated`, () =>
        HttpResponse.json(okResult(EMPTY_PAGE)),
      ),
    )
    renderWithProviders(<CuratedTab personaId={undefined} />)
    expect(await screen.findByText('暂无策展记忆')).toBeInTheDocument()

    server.use(
      http.get(`${BACKEND_ORIGIN}/memories/curated`, () =>
        HttpResponse.json(errorResult('当前没有激活人格', 'NOT_FOUND'), { status: 404 }),
      ),
    )
    renderWithProviders(<CuratedTab personaId={undefined} />)
    expect(await screen.findByText(/策展记忆加载失败：当前没有激活人格/)).toBeInTheDocument()
  })

  it('删除：确认 → 成功 toast；404 → toast 错误', async () => {
    const user = userEvent.setup()
    renderWithProviders(<CuratedTab personaId={undefined} />)
    await screen.findByTestId('curated-row-3')
    await user.click(withinRow(screen.getByTestId('curated-row-3'), '删除'))
    expect(screen.getByText('删除策展记忆')).toBeInTheDocument()
    expect(screen.getByText(/历史链中其余版本不受影响/)).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: '删除' }))
    expect(await screen.findByText(/策展记忆 #3 已删除/)).toBeInTheDocument()

    server.use(
      http.delete(`${BACKEND_ORIGIN}/memories/curated/:id`, () =>
        HttpResponse.json(errorResult('记忆不存在或不在此人格作用域', 'NOT_FOUND'), { status: 404 }),
      ),
    )
    await user.click(withinRow(screen.getByTestId('curated-row-2'), '删除'))
    await user.click(screen.getByRole('button', { name: '删除' }))
    expect(await screen.findByText('记忆不存在或不在此人格作用域')).toBeInTheDocument()
  })

  it('编辑（预填锚点）与新建均打开 upsert 对话框', async () => {
    const user = userEvent.setup()
    renderWithProviders(<CuratedTab personaId={undefined} />)
    await screen.findByTestId('curated-row-3')
    await user.click(withinRow(screen.getByTestId('curated-row-3'), '编辑'))
    expect(await screen.findByRole('heading', { name: /保存策展记忆（主题「sleep-pattern」）/ })).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: '取消' }))
    await user.click(screen.getByRole('button', { name: /新建策展记忆/ }))
    expect(await screen.findByRole('heading', { name: '新建策展记忆' })).toBeInTheDocument()
  })

  it('历史按钮打开历史链对话框', async () => {
    const user = userEvent.setup()
    renderWithProviders(<CuratedTab personaId={undefined} />)
    await screen.findByTestId('curated-row-3')
    await user.click(withinRow(screen.getByTestId('curated-row-3'), '历史'))
    expect(await screen.findByText('主题历史链（supersede 链）')).toBeInTheDocument()
    expect(await screen.findByTestId('curated-history-row-2')).toBeInTheDocument()
  })

  it('mockCurated 工厂默认形态（类型面完整性锚点）', () => {
    const record = mockCurated()
    expect(record.superseded_by_id).toBeNull()
    expect(record.version).toBe(2)
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

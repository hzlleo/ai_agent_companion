import { describe, expect, it } from 'vitest'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { screen, waitFor } from '@testing-library/react'
import PersonaListPage from './PersonaListPage'
import {
  BACKEND_ORIGIN,
  errorResult,
  mockPersona,
  okResult,
  server,
} from '@/test/server'
import { renderWithProviders } from '@/test/utils'

describe('PersonaListPage', () => {
  it('首页加载渲染行（游标分页首页 2 条）', async () => {
    renderWithProviders(<PersonaListPage />, { route: '/personas' })
    expect(await screen.findByTestId('persona-row-2')).toBeInTheDocument()
    expect(screen.getByTestId('persona-row-1')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '加载更多' })).toBeInTheDocument()
    expect(screen.getByText('companion-2')).toBeInTheDocument()
    expect(screen.getByText('当前激活')).toBeInTheDocument()
    // 「启用」同时命中过滤下拉 option 与状态徽章——按多命中断言。
    expect(screen.getAllByText('启用').length).toBeGreaterThan(1)
  })

  it('加载更多：追加末页并终止翻页', async () => {
    const user = userEvent.setup()
    renderWithProviders(<PersonaListPage />, { route: '/personas' })
    await screen.findByTestId('persona-row-2')
    await user.click(screen.getByRole('button', { name: '加载更多' }))
    await waitFor(() => expect(screen.getByTestId('persona-row-3')).toBeInTheDocument())
    expect(await screen.findByText('已加载全部')).toBeInTheDocument()
  })

  it('status 过滤：仅停用（disabled）行呈现', async () => {
    const user = userEvent.setup()
    renderWithProviders(<PersonaListPage />, { route: '/personas' })
    await screen.findByTestId('persona-row-2')
    await user.selectOptions(screen.getByLabelText('状态'), 'disabled')
    await waitFor(() => expect(screen.getByTestId('persona-row-3')).toBeInTheDocument())
    expect(screen.queryByTestId('persona-row-1')).not.toBeInTheDocument()
  })

  it('空列表 → 空态', async () => {
    server.use(
      http.get(`${BACKEND_ORIGIN}/personas`, () =>
        HttpResponse.json(
          okResult({ items: [], total: -1, page: 1, page_size: 20, next_cursor: null, has_more: false }),
        ),
      ),
    )
    renderWithProviders(<PersonaListPage />, { route: '/personas' })
    expect(await screen.findByText('暂无人格')).toBeInTheDocument()
  })

  it('加载失败 → 错误呈现 + 重试恢复', async () => {
    const user = userEvent.setup()
    server.use(
      http.get(`${BACKEND_ORIGIN}/personas`, () =>
        HttpResponse.json(errorResult('服务暂不可用', 'UNAVAILABLE'), { status: 503 }),
      ),
    )
    renderWithProviders(<PersonaListPage />, { route: '/personas' })
    expect(await screen.findByText(/人格列表加载失败：服务暂不可用/)).toBeInTheDocument()
    // 重试 → 默认 handlers（成功形态）恢复可见。
    server.use(
      http.get(`${BACKEND_ORIGIN}/personas`, () =>
        HttpResponse.json(
          okResult({
            items: [mockPersona({ id: 1 })],
            total: -1,
            page: 1,
            page_size: 20,
            next_cursor: null,
            has_more: false,
          }),
        ),
      ),
    )
    await user.click(screen.getByRole('button', { name: '重试' }))
    await waitFor(() => expect(screen.getByTestId('persona-row-1')).toBeInTheDocument())
  })

  it('激活：确认对话框 → 成功 toast', async () => {
    const user = userEvent.setup()
    renderWithProviders(<PersonaListPage />, { route: '/personas' })
    await screen.findByTestId('persona-row-2')
    // companion-2（active 状态、未激活位）可激活。
    await user.click(withinRow(screen.getByTestId('persona-row-2'), '激活'))
    expect(screen.getByText('激活人格')).toBeInTheDocument()
    expect(screen.getByText(/事务性单激活切换/)).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: '激活' }))
    expect(await screen.findByText(/已激活（单激活切换）/)).toBeInTheDocument()
  })

  it('激活失败（停用状态 409）→ toast 错误', async () => {
    const user = userEvent.setup()
    server.use(
      http.post(`${BACKEND_ORIGIN}/personas/:personaId/activate`, () =>
        HttpResponse.json(errorResult('停用状态的人格不可激活', 'CONFLICT'), { status: 409 }),
      ),
    )
    renderWithProviders(<PersonaListPage />, { route: '/personas' })
    await screen.findByTestId('persona-row-2')
    await user.click(withinRow(screen.getByTestId('persona-row-2'), '激活'))
    await user.click(screen.getByRole('button', { name: '激活' }))
    expect(await screen.findByText('停用状态的人格不可激活')).toBeInTheDocument()
  })

  it('停用状态行：激活按钮禁用', async () => {
    const user = userEvent.setup()
    renderWithProviders(<PersonaListPage />, { route: '/personas' })
    await screen.findByTestId('persona-row-2')
    await user.click(screen.getByRole('button', { name: '加载更多' }))
    const disabledRow = await waitFor(() => screen.getByTestId('persona-row-3'))
    expect(withinRow(disabledRow, '激活')).toBeDisabled()
    expect(withinRow(disabledRow, '激活')).toHaveAttribute(
      'title',
      '停用状态的人格不可激活，请先编辑状态为启用',
    )
  })

  it('停用激活：确认 → 成功 toast', async () => {
    const user = userEvent.setup()
    renderWithProviders(<PersonaListPage />, { route: '/personas' })
    await screen.findByTestId('persona-row-1')
    await user.click(withinRow(screen.getByTestId('persona-row-1'), '停用'))
    expect(screen.getByRole('heading', { name: '停用激活' })).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: '停用激活' }))
    expect(await screen.findByText(/激活位已停用/)).toBeInTheDocument()
  })

  it('删除：激活守卫 409 → toast 呈现后端提示', async () => {
    const user = userEvent.setup()
    renderWithProviders(<PersonaListPage />, { route: '/personas' })
    await screen.findByTestId('persona-row-1')
    await user.click(withinRow(screen.getByTestId('persona-row-1'), '删除'))
    expect(screen.getByText(/该操作不可恢复/)).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: '删除' }))
    expect(await screen.findByText('当前激活人格禁止删除，请先停用')).toBeInTheDocument()
  })

  it('删除：非激活人格成功 → toast', async () => {
    const user = userEvent.setup()
    renderWithProviders(<PersonaListPage />, { route: '/personas' })
    await screen.findByTestId('persona-row-2')
    await user.click(withinRow(screen.getByTestId('persona-row-2'), '删除'))
    await user.click(screen.getByRole('button', { name: '删除' }))
    expect(await screen.findByText(/已删除/)).toBeInTheDocument()
  })

  it('复制：对话框打开并成功复制', async () => {
    const user = userEvent.setup()
    renderWithProviders(<PersonaListPage />, { route: '/personas' })
    await screen.findByTestId('persona-row-2')
    await user.click(withinRow(screen.getByTestId('persona-row-2'), '复制'))
    expect(await screen.findByText('复制人格')).toBeInTheDocument()
    expect(screen.getByLabelText('新注册名')).toHaveValue('companion-2-copy')
    await user.click(screen.getByRole('button', { name: '复制' }))
    expect(await screen.findByText(/人格已复制为/)).toBeInTheDocument()
  })

  it('批量选择 + 批量删除：部分成功结果对话框逐项呈现', async () => {
    const user = userEvent.setup()
    renderWithProviders(<PersonaListPage />, { route: '/personas' })
    await screen.findByTestId('persona-row-1')
    screen.getByTestId('persona-row-2')

    // 选中 id=1（激活守卫失败）与 id=2（成功）。
    await user.click(screen.getByLabelText('选择人格 companion-1'))
    await user.click(screen.getByLabelText('选择人格 companion-2'))
    expect(screen.getByRole('button', { name: /批量删除（已选 2）/ })).toBeEnabled()

    await user.click(screen.getByRole('button', { name: /批量删除（已选 2）/ }))
    expect(screen.getByText('批量删除人格')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: '批量删除' }))

    // 部分成功 → 结果对话框。
    expect(await screen.findByText('批量删除结果')).toBeInTheDocument()
    expect(screen.getByText('当前激活人格禁止删除，请先停用')).toBeInTheDocument()
    expect(screen.getByText('（CONFLICT）')).toBeInTheDocument()
    expect(screen.getByText(/成功 1 \/ 失败 1/)).toBeInTheDocument()
  })

  it('全选开关作用于已加载行', async () => {
    const user = userEvent.setup()
    renderWithProviders(<PersonaListPage />, { route: '/personas' })
    await screen.findByTestId('persona-row-2')
    await user.click(screen.getByLabelText('全选当前已加载人格'))
    expect(screen.getByRole('button', { name: /批量删除（已选 2）/ })).toBeInTheDocument()
    await user.click(screen.getByLabelText('全选当前已加载人格'))
    expect(screen.getByRole('button', { name: /批量删除（已选 0）/ })).toBeDisabled()
  })

  it('未选择时批量删除禁用', async () => {
    renderWithProviders(<PersonaListPage />, { route: '/personas' })
    await screen.findByTestId('persona-row-2')
    expect(screen.getByRole('button', { name: /批量删除/ })).toBeDisabled()
  })

  it('取消确认对话框不触发请求', async () => {
    const user = userEvent.setup()
    renderWithProviders(<PersonaListPage />, { route: '/personas' })
    await screen.findByTestId('persona-row-1')
    await user.click(withinRow(screen.getByTestId('persona-row-1'), '删除'))
    await user.click(screen.getByRole('button', { name: '取消' }))
    expect(screen.queryByText('删除人格')).not.toBeInTheDocument()
  })
})

/** 在行内按按钮名查找操作按钮。 */
function withinRow(row: HTMLElement, name: string): HTMLElement {
  const button = Array.from(row.querySelectorAll('button')).find(
    (el) => el.textContent === name,
  )
  if (button === undefined) {
    throw new Error(`行内未找到按钮：${name}`)
  }
  return button
}


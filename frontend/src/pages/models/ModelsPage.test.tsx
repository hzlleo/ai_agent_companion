import { describe, expect, it } from 'vitest'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { screen, waitFor } from '@testing-library/react'
import ModelsPage from './ModelsPage'
import { BACKEND_ORIGIN, errorResult, mockModelEntry, mockRegistryState, okResult, server } from '@/test/server'
import { renderWithProviders } from '@/test/utils'

describe('ModelsPage', () => {
  it('条目表渲染（脱敏字段 + 解析角色徽章 + 免鉴权 / 全局默认提示）', async () => {
    renderWithProviders(<ModelsPage />, { route: '/models' })
    expect(await screen.findByTestId('model-entry-row-main')).toBeInTheDocument()
    expect(screen.getByTestId('model-entry-row-embed-model')).toBeInTheDocument()
    expect(screen.getByText('MAIN_API_KEY')).toBeInTheDocument()
    expect(screen.getByText('（免鉴权）')).toBeInTheDocument()
    // 两个条目并发上限均为 null（全局默认）——按多命中断言。
    expect(screen.getAllByText('全局默认').length).toBe(2)
    expect(screen.getAllByText('主对话').length).toBeGreaterThan(0)
    expect(screen.getByText('text-embedding')).toBeInTheDocument()
  })

  it('角色绑定汇总行（bound / unbound + fail loud 提示）', async () => {
    renderWithProviders(<ModelsPage />, { route: '/models' })
    expect(await screen.findByTestId('role-binding-chat')).toBeInTheDocument()
    expect(screen.getByTestId('role-binding-embed')).toHaveTextContent('embed-model')
    expect(screen.getByTestId('role-binding-duty')).toHaveTextContent('未绑定')
    expect(screen.getByText(/MODEL_NOT_FOUND fail loud/)).toBeInTheDocument()
  })

  it('空注册表 → 空态；加载失败 → 错误 + 重试', async () => {
    const user = userEvent.setup()
    server.use(
      http.get(`${BACKEND_ORIGIN}/models/entries`, () => HttpResponse.json(okResult(mockRegistryState([])))),
    )
    renderWithProviders(<ModelsPage />, { route: '/models' })
    expect(await screen.findByText('暂无模型条目')).toBeInTheDocument()

    server.use(
      http.get(`${BACKEND_ORIGIN}/models/entries`, () =>
        HttpResponse.json(errorResult('服务暂不可用', 'UNAVAILABLE'), { status: 503 }),
      ),
    )
    renderWithProviders(<ModelsPage />, { route: '/models' })
    expect(await screen.findByText(/注册表状态加载失败：服务暂不可用/)).toBeInTheDocument()
    server.use(
      http.get(`${BACKEND_ORIGIN}/models/entries`, () =>
        HttpResponse.json(okResult(mockRegistryState())),
      ),
    )
    await user.click(screen.getByRole('button', { name: '重试' }))
    await waitFor(() => expect(screen.getByTestId('model-entry-row-main')).toBeInTheDocument())
  })

  it('删除守卫（409）：绑定角色提示 + 两步指引 + 「去编辑（解绑）」打开编辑对话框', async () => {
    const user = userEvent.setup()
    renderWithProviders(<ModelsPage />, { route: '/models' })
    await screen.findByTestId('model-entry-row-main')

    await user.click(withinRow(screen.getByTestId('model-entry-row-main'), '删除'))
    expect(screen.getByText('删除模型条目')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: '删除' }))

    const guard = await screen.findByTestId('model-delete-guard')
    expect(guard).toHaveTextContent('仍绑定角色：主对话')
    expect(guard).toHaveTextContent('两步操作')
    expect(await screen.findByText('模型条目仍绑定角色，禁止删除（先解绑再删除）: main')).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: '去编辑（解绑）' }))
    expect(await screen.findByRole('heading', { name: /编辑模型条目（main）/ })).toBeInTheDocument()
  })

  it('删除守卫（409 无 details / bound_roles 非数组）→ 回落文案 + 「知道了」关闭守卫', async () => {
    const user = userEvent.setup()
    server.use(
      http.delete(`${BACKEND_ORIGIN}/models/entries/:name`, () =>
        HttpResponse.json(errorResult('模型条目仍绑定角色，禁止删除（先解绑再删除）: embed-model', 'CONFLICT'), {
          status: 409,
        }),
      ),
    )
    renderWithProviders(<ModelsPage />, { route: '/models' })
    await screen.findByTestId('model-entry-row-embed-model')
    await user.click(withinRow(screen.getByTestId('model-entry-row-embed-model'), '删除'))
    await user.click(screen.getByRole('button', { name: '删除' }))
    const guard = await screen.findByTestId('model-delete-guard')
    expect(guard).toHaveTextContent('（显式 role_bindings 指向）')
    await user.click(screen.getByRole('button', { name: '知道了' }))
    expect(screen.queryByTestId('model-delete-guard')).not.toBeInTheDocument()

    server.use(
      http.delete(`${BACKEND_ORIGIN}/models/entries/:name`, () =>
        HttpResponse.json(
          errorResult('模型条目仍绑定角色，禁止删除（先解绑再删除）: embed-model', 'CONFLICT', {
            name: 'embed-model',
            bound_roles: 'not-an-array',
          }),
          { status: 409 },
        ),
      ),
    )
    await user.click(withinRow(screen.getByTestId('model-entry-row-embed-model'), '删除'))
    await user.click(screen.getByRole('button', { name: '删除' }))
    const fallbackGuard = await screen.findByTestId('model-delete-guard')
    expect(fallbackGuard).toHaveTextContent('（显式 role_bindings 指向）')
  })

  it('删除确认取消不触发请求；未知角色值回落原文', async () => {
    const user = userEvent.setup()
    server.use(
      http.get(`${BACKEND_ORIGIN}/models/entries`, () =>
        HttpResponse.json(
          okResult(
            mockRegistryState([
              mockModelEntry({ name: 'odd', roles: ['chat', 'ghost-role'], resolved_roles: ['ghost-role'] }),
            ]),
          ),
        ),
      ),
    )
    renderWithProviders(<ModelsPage />, { route: '/models' })
    await screen.findByTestId('model-entry-row-odd')
    expect(screen.getAllByText('ghost-role').length).toBeGreaterThan(0)

    await user.click(withinRow(screen.getByTestId('model-entry-row-odd'), '删除'))
    await user.click(screen.getByRole('button', { name: '取消' }))
    expect(screen.queryByText('删除模型条目')).not.toBeInTheDocument()
  })

  it('空角色 / 数值并发 / 多模态条目的展示分支', async () => {
    server.use(
      http.get(`${BACKEND_ORIGIN}/models/entries`, () =>
        HttpResponse.json(
          okResult(
            mockRegistryState([
              mockModelEntry({
                name: 'bare',
                roles: [],
                resolved_roles: [],
                concurrency: 5,
                multimodal: true,
              }),
            ]),
          ),
        ),
      ),
    )
    renderWithProviders(<ModelsPage />, { route: '/models' })
    const row = await screen.findByTestId('model-entry-row-bare')
    expect(row).toHaveTextContent('5')
    expect(screen.getByText('支持')).toBeInTheDocument()
    // 声明角色与解析角色两列的空态占位（各一）。
    expect(screen.getAllByText('—').length).toBeGreaterThanOrEqual(2)
  })

  it('解绑后删除成功（embed-model 两步流末端）', async () => {
    const user = userEvent.setup()
    renderWithProviders(<ModelsPage />, { route: '/models' })
    await screen.findByTestId('model-entry-row-embed-model')
    await user.click(withinRow(screen.getByTestId('model-entry-row-embed-model'), '删除'))
    await user.click(screen.getByRole('button', { name: '删除' }))
    expect(await screen.findByText(/模型条目「embed-model」已删除/)).toBeInTheDocument()
  })

  it('编辑入口打开预填对话框；新建入口打开空表单', async () => {
    const user = userEvent.setup()
    renderWithProviders(<ModelsPage />, { route: '/models' })
    await screen.findByTestId('model-entry-row-main')
    await user.click(withinRow(screen.getByTestId('model-entry-row-main'), '编辑'))
    expect(await screen.findByRole('heading', { name: /编辑模型条目（main）/ })).toBeInTheDocument()
    expect(screen.getByLabelText('模型名')).toHaveValue('companion-main')

    await user.click(screen.getByRole('button', { name: '取消' }))
    await user.click(screen.getByRole('button', { name: /新建条目/ }))
    expect(await screen.findByRole('heading', { name: '新建模型条目' })).toBeInTheDocument()
  })

  it('mockModelEntry 工厂默认形态（类型面完整性锚点）', () => {
    const entry = mockModelEntry()
    expect(entry.roles).toEqual(['chat'])
    expect(entry.resolved_roles).toEqual(['chat'])
    expect(entry.multimodal).toBe(false)
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

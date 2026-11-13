import { describe, expect, it } from 'vitest'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { screen } from '@testing-library/react'
import { ModelEntryDialog } from './ModelEntryDialog'
import { BACKEND_ORIGIN, errorResult, mockModelEntry, okResult, server } from '@/test/server'
import { renderWithProviders } from '@/test/utils'

describe('ModelEntryDialog（创建）', () => {
  it('校验失败：name / base_url / model 内联错误 + toast', async () => {
    const user = userEvent.setup()
    renderWithProviders(
      <ModelEntryDialog open onOpenChange={() => undefined} mode="create" entry={null} />,
    )
    await screen.findByRole('heading', { name: '新建模型条目' })
    await user.click(screen.getByRole('button', { name: '创建' }))
    expect(await screen.findByText('条目名必填，且长度为 1-128 个字符')).toBeInTheDocument()
    expect(screen.getByText(/必须为 http\(s\) URL/)).toBeInTheDocument()
    expect(screen.getByText('模型名必填，且长度为 1-256 个字符')).toBeInTheDocument()
    expect(await screen.findByText('表单校验未通过，请检查标红字段')).toBeInTheDocument()
  })

  it('合法创建 → 成功 toast + 关闭', async () => {
    const user = userEvent.setup()
    renderWithProviders(
      <ModelEntryDialog open onOpenChange={() => undefined} mode="create" entry={null} />,
    )
    await screen.findByRole('heading', { name: '新建模型条目' })
    await user.type(screen.getByLabelText(/条目名（唯一标识）/), 'aux')
    await user.type(screen.getByLabelText('API 基础 URL'), 'http://127.0.0.1:9100/v1')
    await user.type(screen.getByLabelText('模型名'), 'aux-model')
    await user.click(screen.getByRole('checkbox', { name: '值班任务' }))
    await user.click(screen.getByRole('button', { name: '创建' }))
    expect(await screen.findByText(/模型条目「aux」已创建/)).toBeInTheDocument()
  })

  it('创建重名（409）→ toast 呈现后端 message', async () => {
    const user = userEvent.setup()
    server.use(
      http.post(`${BACKEND_ORIGIN}/models/entries`, () =>
        HttpResponse.json(errorResult('模型条目名已存在: main', 'CONFLICT'), { status: 409 }),
      ),
    )
    renderWithProviders(
      <ModelEntryDialog open onOpenChange={() => undefined} mode="create" entry={null} />,
    )
    await screen.findByRole('heading', { name: '新建模型条目' })
    await user.type(screen.getByLabelText(/条目名（唯一标识）/), 'main')
    await user.type(screen.getByLabelText('API 基础 URL'), 'http://127.0.0.1:9100/v1')
    await user.type(screen.getByLabelText('模型名'), 'm')
    await user.click(screen.getByRole('button', { name: '创建' }))
    expect(await screen.findByText('模型条目名已存在: main')).toBeInTheDocument()
  })

  it('全字段交互：provider / api_key_env / 并发 / 多模态 + 取消关闭', async () => {
    const user = userEvent.setup()
    let closed = false
    renderWithProviders(
      <ModelEntryDialog
        open
        onOpenChange={(open) => {
          closed = !open
        }}
        mode="create"
        entry={null}
      />,
    )
    await screen.findByRole('heading', { name: '新建模型条目' })
    await user.clear(screen.getByLabelText('provider 类型'))
    await user.type(screen.getByLabelText('provider 类型'), 'openai_compatible')
    await user.type(screen.getByLabelText('API Key 环境变量名'), 'AUX_API_KEY')
    await user.type(screen.getByLabelText('并发上限'), '8')
    await user.click(screen.getByRole('checkbox', { name: '支持多模态输入（look_at 链路回退判据）' }))
    await user.click(screen.getByRole('button', { name: '取消' }))
    expect(closed).toBe(true)
  })
})

describe('ModelEntryDialog（编辑）', () => {
  it('锚点预填 + name 只读 + 重置勾选禁用输入', async () => {
    const user = userEvent.setup()
    renderWithProviders(
      <ModelEntryDialog
        open
        onOpenChange={() => undefined}
        mode="edit"
        entry={mockModelEntry({ concurrency: 4 })}
      />,
    )
    await screen.findByRole('heading', { name: /编辑模型条目（main）/ })
    expect(screen.getByLabelText(/条目名（唯一标识）/)).toBeDisabled()
    expect(screen.getByLabelText(/条目名（唯一标识）/)).toHaveValue('main')
    expect(screen.getByLabelText('API 基础 URL')).toHaveValue('http://127.0.0.1:8001/v1')
    expect(screen.getByLabelText(/并发上限/)).toHaveValue(4)
    expect(screen.getByText('条目名不可变（路径标识）。')).toBeInTheDocument()

    await user.click(screen.getByRole('checkbox', { name: '重置为全局默认' }))
    expect(screen.getByLabelText(/并发上限/)).toBeDisabled()
  })

  it('编辑保存：roles=[] 清空声明（解绑语义）→ 成功 toast', async () => {
    const user = userEvent.setup()
    let capturedBody: Record<string, unknown> | null = null
    server.use(
      http.put(`${BACKEND_ORIGIN}/models/entries/:name`, async ({ request }) => {
        capturedBody = (await request.json()) as Record<string, unknown>
        return HttpResponse.json(okResult(null, '模型条目已更新'))
      }),
    )
    renderWithProviders(
      <ModelEntryDialog
        open
        onOpenChange={() => undefined}
        mode="edit"
        entry={mockModelEntry({ roles: ['chat'] })}
      />,
    )
    await screen.findByRole('heading', { name: /编辑模型条目（main）/ })
    await user.click(screen.getByRole('checkbox', { name: '主对话' }))
    await user.click(screen.getByRole('button', { name: '保存' }))
    expect(await screen.findByText(/模型条目「main」已更新/)).toBeInTheDocument()
    expect(capturedBody).toMatchObject({ roles: [] })
  })
})

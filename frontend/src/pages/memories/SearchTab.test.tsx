import { describe, expect, it } from 'vitest'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { screen, waitFor } from '@testing-library/react'
import { SearchTab } from './SearchTab'
import { BACKEND_ORIGIN, okResult, server } from '@/test/server'
import { renderWithProviders } from '@/test/utils'

describe('SearchTab', () => {
  it('keyword 检索：结果表 + 分数方向提示 + 命中 toast', async () => {
    const user = userEvent.setup()
    renderWithProviders(<SearchTab personaId={1} />)
    await screen.findByLabelText('检索文本')
    await user.type(screen.getByLabelText('检索文本'), '热汤面')
    await user.click(screen.getByRole('button', { name: /检索/ }))
    expect(await screen.findByText(/检索完成（命中 2 条）/)).toBeInTheDocument()
    expect(screen.getByText(/分数为全文相关度（越大越相关）/)).toBeInTheDocument()
    expect(screen.getByText('主人提到深夜想吃热汤面。')).toBeInTheDocument()
    expect(screen.getAllByText('情景').length).toBeGreaterThan(0)
  })

  it('类型勾选 + 模式进入请求体', async () => {
    const user = userEvent.setup()
    let capturedBody: Record<string, unknown> | null = null
    server.use(
      http.post(`${BACKEND_ORIGIN}/memories/search`, async ({ request }) => {
        capturedBody = (await request.json()) as Record<string, unknown>
        return HttpResponse.json(okResult([]))
      }),
    )
    renderWithProviders(<SearchTab personaId={2} />)
    await screen.findByLabelText('检索文本')
    await user.type(screen.getByLabelText('检索文本'), '汤面')
    await user.click(screen.getByRole('checkbox', { name: '对话回合' }))
    await user.click(screen.getByRole('checkbox', { name: '蒸馏记忆' }))
    await user.click(screen.getByRole('button', { name: /检索/ }))
    await waitFor(() => expect(capturedBody).not.toBeNull())
    expect(capturedBody).toMatchObject({
      persona_id: 2,
      query: '汤面',
      mode: 'keyword',
      kinds: ['chat', 'memory'],
    })
  })

  it('回退存储拒绝 dense（422）→ 中文警示 + 指引文案', async () => {
    const user = userEvent.setup()
    renderWithProviders(<SearchTab personaId={1} />)
    await screen.findByLabelText('检索文本')
    await user.type(screen.getByLabelText('检索文本'), '热汤面')
    await user.selectOptions(screen.getByLabelText('模式'), 'dense')
    await user.click(screen.getByRole('button', { name: /检索/ }))
    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('检索失败：回退存储不支持向量检索（仅 keyword 模式可用）')
    expect(alert).toHaveTextContent('启用 SeekDB（SEEKDB_ENABLED=true）或改用 mode=keyword')
    expect(alert).toHaveTextContent('当前回退存储仅支持关键词模式')
  })

  it('校验：空文本 / limit 越界 → toast 拦截（不发请求）', async () => {
    const user = userEvent.setup()
    renderWithProviders(<SearchTab personaId={1} />)
    await screen.findByLabelText('检索文本')
    await user.click(screen.getByRole('button', { name: /检索/ }))
    expect(await screen.findByText('检索文本必填')).toBeInTheDocument()

    await user.type(screen.getByLabelText('检索文本'), '汤面')
    await user.clear(screen.getByLabelText(/条数上限（1-50）/))
    await user.type(screen.getByLabelText(/条数上限（1-50）/), '99')
    await user.click(screen.getByRole('button', { name: /检索/ }))
    expect(await screen.findByText('返回条数上限须为 1-50 的整数')).toBeInTheDocument()
  })

  it('空结果 → 空态', async () => {
    const user = userEvent.setup()
    server.use(
      http.post(`${BACKEND_ORIGIN}/memories/search`, () => HttpResponse.json(okResult([]))),
    )
    renderWithProviders(<SearchTab personaId={1} />)
    await screen.findByLabelText('检索文本')
    await user.type(screen.getByLabelText('检索文本'), '不存在的内容')
    await user.click(screen.getByRole('button', { name: /检索/ }))
    expect(await screen.findByText('未命中任何记忆')).toBeInTheDocument()
  })
})

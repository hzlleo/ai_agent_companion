import { describe, expect, it } from 'vitest'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { screen, waitFor } from '@testing-library/react'
import { FactsPanel } from './FactsPanel'
import { BACKEND_ORIGIN, errorResult, mockFact, okResult, server } from '@/test/server'
import { renderWithProviders } from '@/test/utils'

describe('FactsPanel', () => {
  it('加载现行事实：类别分组 + 置信度徽章 + 来源标签', async () => {
    renderWithProviders(<FactsPanel personaId={1} />)
    expect(await screen.findByTestId('fact-row-1')).toBeInTheDocument()
    expect(screen.getByText('偏好')).toBeInTheDocument()
    expect(screen.getByText('背景')).toBeInTheDocument()
    expect(screen.getByText('主人喜欢无糖乌龙茶')).toBeInTheDocument()
    expect(screen.getByText('置信度 90%')).toBeInTheDocument()
    expect(screen.getByText('梦境提取')).toBeInTheDocument()
  })

  it('证据展开器：逐字摘录 + 情景回链', async () => {
    renderWithProviders(<FactsPanel personaId={1} />)
    const evidence = await screen.findByTestId('fact-evidence-1')
    expect(evidence).toHaveTextContent('「用户：我喜欢喝无糖乌龙茶」')
    expect(screen.getByRole('link', { name: '情景 #12' })).toHaveAttribute('href', '/memories')
  })

  it('新建事实：正文必填校验 + 成功后关闭并 toast', async () => {
    const user = userEvent.setup()
    let sentCategory: string | null = null
    server.use(
      http.post(`${BACKEND_ORIGIN}/facts`, async ({ request }) => {
        const body = (await request.json()) as { category: string; content: string }
        sentCategory = body.category
        return HttpResponse.json(
          okResult(
            mockFact({
              id: 9,
              category: body.category as 'preference',
              content: body.content,
              source: 'manual',
              provenance: 'owner',
              evidence: [],
            }),
          ),
        )
      }),
    )
    renderWithProviders(<FactsPanel personaId={1} />)
    await screen.findByTestId('fact-row-1')

    await user.click(screen.getByRole('button', { name: /新建事实/ }))
    await user.click(screen.getByRole('button', { name: '保存事实' }))
    expect(await screen.findByText(/事实正文必填/)).toBeInTheDocument()

    await user.type(screen.getByLabelText('事实正文'), '主人喜欢下雨天')
    await user.click(screen.getByRole('button', { name: '保存事实' }))
    expect(await screen.findByText(/人物事实已保存/)).toBeInTheDocument()
    expect(sentCategory).toBe('preference')
    await waitFor(() => expect(screen.queryByLabelText('事实正文')).not.toBeInTheDocument())
  })

  it('置信度越界：前端预校验拦截', async () => {
    const user = userEvent.setup()
    renderWithProviders(<FactsPanel personaId={1} />)
    await screen.findByTestId('fact-row-1')
    await user.click(screen.getByRole('button', { name: /新建事实/ }))
    await user.type(screen.getByLabelText('事实正文'), '主人喜欢下雨天')
    await user.clear(screen.getByLabelText('置信度（0-1）'))
    await user.type(screen.getByLabelText('置信度（0-1）'), '2')
    await user.click(screen.getByRole('button', { name: '保存事实' }))
    expect(await screen.findByText(/置信度须为 0-1/)).toBeInTheDocument()
  })

  it('更正：预填正文 → 保存走 supersede 路径', async () => {
    const user = userEvent.setup()
    let correctedContent: string | null = null
    server.use(
      http.put(`${BACKEND_ORIGIN}/facts/:factId`, async ({ request }) => {
        const body = (await request.json()) as { content: string }
        correctedContent = body.content
        return HttpResponse.json(
          okResult(mockFact({ id: 101, content: body.content, source: 'correction', version: 2 })),
        )
      }),
    )
    renderWithProviders(<FactsPanel personaId={1} />)
    const row = await screen.findByTestId('fact-row-1')
    await user.click(
      Array.from(row.querySelectorAll('button')).find(
        (candidate) => candidate.textContent === '更正',
      ) as HTMLButtonElement,
    )
    expect(screen.getByLabelText('事实正文')).toHaveValue('主人喜欢无糖乌龙茶')
    expect(screen.getByLabelText('类别')).toBeDisabled()
    await user.clear(screen.getByLabelText('事实正文'))
    await user.type(screen.getByLabelText('事实正文'), '主人其实更喜欢茶')
    await user.click(screen.getByRole('button', { name: '保存更正' }))
    expect(await screen.findByText(/人物事实已更正（v2）/)).toBeInTheDocument()
    expect(correctedContent).toBe('主人其实更喜欢茶')
  })

  it('驳回：直接生效 toast；删除走确认框', async () => {
    const user = userEvent.setup()
    renderWithProviders(<FactsPanel personaId={1} />)
    const row = await screen.findByTestId('fact-row-1')
    await user.click(
      Array.from(row.querySelectorAll('button')).find(
        (candidate) => candidate.textContent === '驳回',
      ) as HTMLButtonElement,
    )
    expect(await screen.findByText('人物事实已驳回')).toBeInTheDocument()

    const secondRow = await screen.findByTestId('fact-row-2')
    await user.click(
      Array.from(secondRow.querySelectorAll('button')).find(
        (candidate) => candidate.textContent === '删除',
      ) as HTMLButtonElement,
    )
    expect(screen.getByText('删除人物事实')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: '删除' }))
    expect(await screen.findByText('人物事实已删除')).toBeInTheDocument()
  })

  it('加载失败：错误提示 + 重试恢复', async () => {
    let failures = 1
    server.use(
      http.get(`${BACKEND_ORIGIN}/facts`, () => {
        if (failures > 0) {
          failures -= 1
          return HttpResponse.json(errorResult('人物事实不存在', 'NOT_FOUND'), { status: 404 })
        }
        return HttpResponse.json(
          okResult({
            items: [mockFact()],
            total: -1,
            page: 1,
            page_size: 200,
            next_cursor: null,
            has_more: false,
          }),
        )
      }),
    )
    renderWithProviders(<FactsPanel personaId={1} />)
    expect(await screen.findByText(/人物事实加载失败/)).toBeInTheDocument()
    await userEvent.setup().click(screen.getByRole('button', { name: '重试' }))
    expect(await screen.findByTestId('fact-row-1')).toBeInTheDocument()
  })

  it('对话框取消 / 类别切换 / 删除确认取消均零写入', async () => {
    const user = userEvent.setup()
    renderWithProviders(<FactsPanel personaId={1} />)
    await screen.findByTestId('fact-row-1')

    await user.click(screen.getByRole('button', { name: /新建事实/ }))
    await user.selectOptions(screen.getByLabelText('类别'), 'context')
    await user.click(screen.getByRole('button', { name: '取消' }))
    await waitFor(() => expect(screen.queryByLabelText('事实正文')).not.toBeInTheDocument())

    const row = screen.getByTestId('fact-row-1')
    await user.click(
      Array.from(row.querySelectorAll('button')).find(
        (candidate) => candidate.textContent === '删除',
      ) as HTMLButtonElement,
    )
    expect(screen.getByText('删除人物事实')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: '取消' }))
    await waitFor(() => expect(screen.queryByText('删除人物事实')).not.toBeInTheDocument())
    expect(screen.getByTestId('fact-row-1')).toBeInTheDocument()
  })

  it('写操作失败：更正 / 驳回 / 删除各自报错且不破坏列表', async () => {
    const user = userEvent.setup()
    server.use(
      http.put(`${BACKEND_ORIGIN}/facts/:factId`, () =>
        HttpResponse.json(errorResult('更正失败：事实不存在', 'NOT_FOUND'), { status: 404 }),
      ),
      http.post(`${BACKEND_ORIGIN}/facts/:factId/reject`, () =>
        HttpResponse.json(errorResult('驳回失败：事实不存在', 'NOT_FOUND'), { status: 404 }),
      ),
      http.delete(`${BACKEND_ORIGIN}/facts/:factId`, () =>
        HttpResponse.json(errorResult('删除失败：事实不存在', 'NOT_FOUND'), { status: 404 }),
      ),
    )
    renderWithProviders(<FactsPanel personaId={1} />)
    const row = await screen.findByTestId('fact-row-1')

    await user.click(
      Array.from(row.querySelectorAll('button')).find(
        (candidate) => candidate.textContent === '更正',
      ) as HTMLButtonElement,
    )
    await user.click(screen.getByRole('button', { name: '保存更正' }))
    expect(await screen.findByText(/更正失败/)).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: '取消' }))

    await user.click(
      Array.from(screen.getByTestId('fact-row-1').querySelectorAll('button')).find(
        (candidate) => candidate.textContent === '驳回',
      ) as HTMLButtonElement,
    )
    expect(await screen.findByText(/驳回失败/)).toBeInTheDocument()

    await user.click(
      Array.from(screen.getByTestId('fact-row-1').querySelectorAll('button')).find(
        (candidate) => candidate.textContent === '删除',
      ) as HTMLButtonElement,
    )
    await user.click(screen.getByRole('button', { name: '删除' }))
    expect(await screen.findByText(/删除失败/)).toBeInTheDocument()
  })

  it('空列表：空态提示', async () => {
    server.use(
      http.get(`${BACKEND_ORIGIN}/facts`, () =>
        HttpResponse.json(
          okResult({
            items: [],
            total: -1,
            page: 1,
            page_size: 200,
            next_cursor: null,
            has_more: false,
          }),
        ),
      ),
    )
    renderWithProviders(<FactsPanel personaId={1} />)
    expect(await screen.findByText('还不了解 TA')).toBeInTheDocument()
  })
})

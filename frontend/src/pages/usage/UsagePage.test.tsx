import { describe, expect, it } from 'vitest'
import userEvent from '@testing-library/user-event'
import { fireEvent, screen, waitFor } from '@testing-library/react'
import { http, HttpResponse } from 'msw'
import UsagePage from './UsagePage'
import { BACKEND_ORIGIN, errorResult, mockPersona, mockUsageRecord, mockUsageSummary, okResult, server } from '@/test/server'
import { renderWithProviders } from '@/test/utils'

const EMPTY_PAGE = { items: [], total: -1, page: 1, page_size: 20, next_cursor: null, has_more: false }
const EMPTY_SUMMARY = {
  call_count: 0,
  input_tokens: 0,
  output_tokens: 0,
  total_tokens: 0,
  avg_latency_ms: 0,
  error_count: 0,
  purposes: [],
}

describe('UsagePage', () => {
  it('总览瓦片 + per-purpose 明细渲染', async () => {
    renderWithProviders(<UsagePage />, { route: '/usage' })
    expect(await screen.findByTestId('usage-summary-call_count')).toHaveTextContent('3次')
    expect(screen.getByTestId('usage-summary-total_tokens')).toHaveTextContent('2,200')
    expect(screen.getByTestId('usage-summary-avg_latency_ms')).toHaveTextContent('867ms')
    expect(screen.getByTestId('usage-summary-error_count')).toHaveTextContent('1次')
    // 用途标签同时命中过滤下拉 option 与明细行——按多命中断言。
    expect(screen.getAllByText('主对话').length).toBeGreaterThan(1)
    expect(screen.getAllByText('向量化').length).toBeGreaterThan(1)
  })

  it('聚合维度切换：purpose → model_entry → time_bucket（桶宽选择器联动）', async () => {
    const user = userEvent.setup()
    renderWithProviders(<UsagePage />, { route: '/usage' })
    await screen.findByTestId('usage-summary-call_count')

    await user.selectOptions(screen.getByLabelText('分组维度'), 'model_entry')
    // 聚合行到位（条目 main / embed-model）；embed-model 另见于记录行——多命中。
    await waitFor(() => expect(screen.getAllByText('main')).toHaveLength(1))
    expect(screen.getAllByText('embed-model').length).toBeGreaterThanOrEqual(2)

    await user.selectOptions(screen.getByLabelText('分组维度'), 'time_bucket')
    expect(await screen.findByLabelText('桶宽')).toBeInTheDocument()
  })

  it('time_bucket 桶宽切换刷新聚合', async () => {
    const user = userEvent.setup()
    let capturedBucket: string | null = null
    server.use(
      http.get(`${BACKEND_ORIGIN}/usage/aggregates`, ({ request }) => {
        capturedBucket = new URL(request.url).searchParams.get('time_bucket')
        return HttpResponse.json(
          okResult([
            {
              group: '1731000000000',
              bucket_ms: 1731000000000,
              input_tokens: 1,
              output_tokens: 1,
              total_tokens: 2,
              call_count: 1,
              avg_latency_ms: 5,
              error_count: 0,
            },
          ]),
        )
      }),
    )
    renderWithProviders(<UsagePage />, { route: '/usage' })
    await screen.findByTestId('usage-summary-call_count')
    await user.selectOptions(screen.getByLabelText('分组维度'), 'time_bucket')
    await user.selectOptions(screen.getByLabelText('桶宽'), 'day')
    await waitFor(() => expect(capturedBucket).toBe('day'))
    expect(await screen.findByText('桶起点')).toBeInTheDocument()
  })

  it('记录表渲染 + load more 终止', async () => {
    const user = userEvent.setup()
    // 人格选择器压平为单页——避免其「加载更多」与记录表翻页按钮撞名。
    server.use(
      http.get(`${BACKEND_ORIGIN}/personas`, () =>
        HttpResponse.json(
          okResult({
            items: [mockPersona()],
            total: -1,
            page: 1,
            page_size: 100,
            next_cursor: null,
            has_more: false,
          }),
        ),
      ),
    )
    renderWithProviders(<UsagePage />, { route: '/usage' })
    expect(await screen.findByTestId('usage-record-row-2')).toBeInTheDocument()
    // 条目名同时命中角色绑定汇总 / 聚合行 / 记录行——按多命中断言。
    expect(screen.getAllByText('embed-model').length).toBeGreaterThan(0)
    expect(screen.getByText('MODEL_NOT_FOUND')).toBeInTheDocument()
    expect(screen.getByText('系统')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: '加载更多' }))
    await waitFor(() => expect(screen.getByTestId('usage-record-row-1')).toBeInTheDocument())
    expect(await screen.findByText('已加载全部')).toBeInTheDocument()
  })

  it('过滤面进入查询串（人格 / 条目 / 用途 / 状态 / 时间范围）', async () => {
    const user = userEvent.setup()
    let capturedUrl: URL | null = null
    server.use(
      http.get(`${BACKEND_ORIGIN}/usage/records`, ({ request }) => {
        capturedUrl = new URL(request.url)
        return HttpResponse.json(okResult(EMPTY_PAGE))
      }),
    )
    renderWithProviders(<UsagePage />, { route: '/usage' })
    await screen.findByTestId('usage-summary-call_count')
    await user.selectOptions(screen.getByLabelText('人格'), '1')
    await user.type(screen.getByLabelText('模型条目'), 'main')
    await user.selectOptions(screen.getByLabelText('用途'), 'chat')
    await user.selectOptions(screen.getByLabelText('结果'), 'error')
    fireEvent.change(screen.getByLabelText('时间范围'), { target: { value: '2026-01-01T00:00' } })
    await waitFor(() => expect(capturedUrl?.searchParams.get('persona_id')).toBe('1'))
    await waitFor(() => expect(capturedUrl?.searchParams.get('model_entry')).toBe('main'))
    await waitFor(() => expect(capturedUrl?.searchParams.get('purpose')).toBe('chat'))
    await waitFor(() => expect(capturedUrl?.searchParams.get('status')).toBe('error'))
    await waitFor(() => expect(capturedUrl?.searchParams.get('start_ms') !== null).toBe(true))
  })

  it('空数据 → 空态；总览失败 → 错误 + 重试恢复', async () => {
    const user = userEvent.setup()
    server.use(
      http.get(`${BACKEND_ORIGIN}/usage/summary`, () => HttpResponse.json(okResult(EMPTY_SUMMARY))),
      http.get(`${BACKEND_ORIGIN}/usage/aggregates`, () => HttpResponse.json(okResult([]))),
      http.get(`${BACKEND_ORIGIN}/usage/records`, () => HttpResponse.json(okResult(EMPTY_PAGE))),
    )
    const emptyRender = renderWithProviders(<UsagePage />, { route: '/usage' })
    expect(await screen.findByText('暂无聚合数据')).toBeInTheDocument()
    expect(screen.getByText('暂无调用记录')).toBeInTheDocument()
    // 明细空态提示同时命中总览明细与聚合空态——按多命中断言。
    expect(screen.getAllByText('当前过滤范围内没有用量数据。').length).toBeGreaterThan(1)
    emptyRender.unmount()

    server.use(
      http.get(`${BACKEND_ORIGIN}/usage/summary`, () =>
        HttpResponse.json(errorResult('服务暂不可用', 'UNAVAILABLE'), { status: 503 }),
      ),
    )
    renderWithProviders(<UsagePage />, { route: '/usage' })
    expect(await screen.findByText(/用量总览加载失败：服务暂不可用/)).toBeInTheDocument()
    server.use(
      http.get(`${BACKEND_ORIGIN}/usage/summary`, () =>
        HttpResponse.json(okResult(mockUsageSummary())),
      ),
    )
    await user.click(screen.getByRole('button', { name: '重试' }))
    await waitFor(() => expect(screen.getByTestId('usage-summary-call_count')).toBeInTheDocument())
  })

  it('mockUsageRecord 工厂默认形态（类型面完整性锚点）', () => {
    const record = mockUsageRecord()
    expect(record.persona_id).toBe('1')
    expect(record.request_id).toBe('req-001')
  })
})

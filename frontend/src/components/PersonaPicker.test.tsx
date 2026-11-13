import { describe, expect, it } from 'vitest'
import { userEvent } from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { screen, waitFor, within } from '@testing-library/react'
import { PersonaPicker } from './PersonaPicker'
import { BACKEND_ORIGIN, errorResult, okResult, server } from '@/test/server'
import { renderWithProviders } from '@/test/utils'

function pageOfPersonas() {
  return {
    items: [],
    total: -1,
    page: 1,
    page_size: 100,
    next_cursor: null,
    has_more: false,
  }
}

describe('PersonaPicker', () => {
  it('渲染默认项 + 候选人格（展示名 + 注册名 + 激活提示）', async () => {
    renderWithProviders(<PersonaPicker id="picker" value="" onChange={() => undefined} />)
    const select = await screen.findByLabelText('人格')
    await waitFor(() =>
      expect(within(select).getByRole('option', { name: /companion-1/ })).toBeInTheDocument(),
    )
    expect(within(select).getByRole('option', { name: '当前激活人格（缺省）' })).toBeInTheDocument()
    expect(
      within(select).getByRole('option', { name: /companion-1.*当前激活/ }),
    ).toBeInTheDocument()
  })

  it('选择人格 → onChange 收到 id 字符串', async () => {
    const user = userEvent.setup()
    let captured = ''
    renderWithProviders(
      <PersonaPicker id="picker" value="" onChange={(value) => (captured = value)} />,
    )
    const select = await screen.findByLabelText('人格')
    await waitFor(() =>
      expect(within(select).getByRole('option', { name: /companion-1/ })).toBeInTheDocument(),
    )
    await user.selectOptions(select, '1')
    expect(captured).toBe('1')
  })

  it('加载失败 → 选择器可用（缺省项保留）', async () => {
    server.use(
      http.get(`${BACKEND_ORIGIN}/personas`, () =>
        HttpResponse.json(errorResult('服务暂不可用', 'UNAVAILABLE'), { status: 503 }),
      ),
    )
    renderWithProviders(<PersonaPicker id="picker" value="" onChange={() => undefined} />)
    const select = await screen.findByLabelText('人格')
    await waitFor(() => expect(select).toBeEnabled())
    expect(within(select).getByRole('option', { name: '当前激活人格（缺省）' })).toBeInTheDocument()
  })

  it('超过一页时提供「加载更多」并追加候选', async () => {
    const user = userEvent.setup()
    server.use(
      http.get(`${BACKEND_ORIGIN}/personas`, ({ request }) => {
        const cursor = new URL(request.url).searchParams.get('cursor')
        if (cursor === null) {
          return HttpResponse.json(
            okResult({ ...pageOfPersonas(), items: [], next_cursor: 'p2', has_more: true }),
          )
        }
        return HttpResponse.json(
          okResult({
            ...pageOfPersonas(),
            items: [
              {
                id: 7,
                name: 'extra-persona',
                display_name: '追加人格',
                status: 'disabled',
                is_active: false,
                card: {
                  name: '追加',
                  description: '',
                  personality: '',
                  scenario: '',
                  first_mes: '',
                  mes_example: '',
                  system_prompt: '',
                  post_history_instructions: '',
                  character_book: null,
                },
                created_at: 1,
                updated_at: 1,
              },
            ],
            next_cursor: null,
            has_more: false,
          }),
        )
      }),
    )
    renderWithProviders(<PersonaPicker id="picker" value="" onChange={() => undefined} />)
    await screen.findByRole('button', { name: '加载更多' })
    await user.click(screen.getByRole('button', { name: '加载更多' }))
    const select = screen.getByLabelText('人格')
    await waitFor(() =>
      expect(within(select).getByRole('option', { name: /extra-persona/ })).toBeInTheDocument(),
    )
  })
})

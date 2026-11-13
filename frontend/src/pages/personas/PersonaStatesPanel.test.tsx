import { describe, expect, it } from 'vitest'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { fireEvent, screen, waitFor } from '@testing-library/react'
import { PersonaStatesPanel } from './PersonaStatesPanel'
import { BACKEND_ORIGIN, errorResult, mockState, okResult, server } from '@/test/server'
import { renderWithProviders } from '@/test/utils'

describe('PersonaStatesPanel', () => {
  it('加载已写层：M 已写入（JSON 回显）、S 未写入', async () => {
    renderWithProviders(<PersonaStatesPanel personaId={1} />)
    await waitFor(() =>
      expect(screen.getByLabelText(/M 层/)).toHaveValue(
        JSON.stringify({ mood: 'calm', affinity: 0.6 }, null, 2),
      ),
    )
    expect(screen.getByText('已写入')).toBeInTheDocument()
    expect(screen.getByText('未写入')).toBeInTheDocument()
    expect(screen.getByLabelText(/S 层/)).toHaveValue('{}')
  })

  it('加载失败 → 错误提示', async () => {
    server.use(
      http.get(`${BACKEND_ORIGIN}/personas/:personaId/states`, () =>
        HttpResponse.json(errorResult('人格不存在', 'NOT_FOUND'), { status: 404 }),
      ),
    )
    renderWithProviders(<PersonaStatesPanel personaId={404} />)
    expect(await screen.findByText(/状态层加载失败：人格不存在/)).toBeInTheDocument()
  })

  it('保存 M 层：合法 JSON → PUT 携带 state；成功后回显已保存形态', async () => {
    const user = userEvent.setup()
    let capturedBody: { state: Record<string, unknown> } | null = null
    server.use(
      http.put(`${BACKEND_ORIGIN}/personas/:personaId/states/:layer`, async ({ request }) => {
        capturedBody = (await request.json()) as { state: Record<string, unknown> }
        return HttpResponse.json(
          okResult(mockState({ layer: 'M', state: { mood: 'happy' } }), '人格状态已更新'),
        )
      }),
    )
    renderWithProviders(<PersonaStatesPanel personaId={1} />)
    await waitFor(() => expect(mEditor().value).toContain('calm'))
    fireEvent.change(mEditor(), { target: { value: '{"mood": "happy"}' } })
    await user.click(screen.getByRole('button', { name: '保存 M 层' }))
    await waitFor(() => expect(capturedBody).toEqual({ state: { mood: 'happy' } }))
    expect(await screen.findByText('M 层状态已更新')).toBeInTheDocument()
    await waitFor(() =>
      expect(mEditor().value).toBe(JSON.stringify({ mood: 'happy' }, null, 2)),
    )
  })

  it('非法 JSON → 前置拦截提示（不发请求）', async () => {
    const user = userEvent.setup()
    server.use(
      http.put(`${BACKEND_ORIGIN}/personas/:personaId/states/:layer`, () => {
        throw new Error('不应发出请求')
      }),
    )
    renderWithProviders(<PersonaStatesPanel personaId={1} />)
    await waitFor(() => expect(screen.getByLabelText(/S 层/)).toHaveValue('{}'))
    fireEvent.change(screen.getByLabelText(/S 层/), { target: { value: '{invalid json' } })
    await user.click(screen.getByRole('button', { name: '保存 S 层' }))
    expect(await screen.findByText('JSON 解析失败，请检查格式')).toBeInTheDocument()
  })

  it('非对象 JSON（数组）→ 前置拦截提示', async () => {
    const user = userEvent.setup()
    renderWithProviders(<PersonaStatesPanel personaId={1} />)
    await waitFor(() => expect(screen.getByLabelText(/S 层/)).toHaveValue('{}'))
    fireEvent.change(screen.getByLabelText(/S 层/), { target: { value: '[1, 2]' } })
    await user.click(screen.getByRole('button', { name: '保存 S 层' }))
    expect(await screen.findByText('状态必须为 JSON 对象（如 {"mood": "calm"}）')).toBeInTheDocument()
  })

  it('保存失败（404）→ toast 错误', async () => {
    const user = userEvent.setup()
    server.use(
      http.put(`${BACKEND_ORIGIN}/personas/:personaId/states/:layer`, () =>
        HttpResponse.json(errorResult('人格不存在', 'NOT_FOUND'), { status: 404 }),
      ),
    )
    renderWithProviders(<PersonaStatesPanel personaId={1} />)
    await waitFor(() => expect(mEditor().value).toContain('calm'))
    fireEvent.change(mEditor(), { target: { value: '{"mood": "x"}' } })
    await user.click(screen.getByRole('button', { name: '保存 M 层' }))
    expect(await screen.findByText('人格不存在')).toBeInTheDocument()
  })
})

function mEditor(): HTMLTextAreaElement {
  return screen.getByLabelText<HTMLTextAreaElement>(/M 层/)
}

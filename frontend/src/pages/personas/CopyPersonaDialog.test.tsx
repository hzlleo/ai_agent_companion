import { describe, expect, it, vi } from 'vitest'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { fireEvent, screen, waitFor } from '@testing-library/react'
import { CopyPersonaDialog } from './CopyPersonaDialog'
import { BACKEND_ORIGIN, errorResult, mockPersona, okResult, server } from '@/test/server'
import { renderWithProviders } from '@/test/utils'

const source = mockPersona({ id: 3, name: 'companion-3', is_active: false })

describe('CopyPersonaDialog', () => {
  it('打开时预填默认新注册名（源名-copy）', () => {
    renderWithProviders(
      <CopyPersonaDialog open onOpenChange={() => undefined} persona={source} />,
    )
    expect(screen.getByLabelText('新注册名')).toHaveValue('companion-3-copy')
    expect(screen.getByLabelText(/深拷贝 M\/S 状态层/)).not.toBeChecked()
    expect(screen.getByText('源人格：')).toBeInTheDocument()
    expect(screen.getByText('companion-3')).toBeInTheDocument()
  })

  it('校验：新注册名清空后提示必填', async () => {
    const user = userEvent.setup()
    renderWithProviders(
      <CopyPersonaDialog open onOpenChange={() => undefined} persona={source} />,
    )
    await user.clear(screen.getByLabelText('新注册名'))
    fireEvent.click(screen.getByRole('button', { name: '复制' }))
    expect(await screen.findByText('新注册名必填，且长度为 1-128 个字符')).toBeInTheDocument()
  })

  it('提交：携带 new_name/display_name/include_states；成功后关闭', async () => {
    const user = userEvent.setup()
    const onOpenChange = vi.fn()
    let capturedBody: Record<string, unknown> | null = null
    server.use(
      http.post(`${BACKEND_ORIGIN}/personas/:personaId/copy`, async ({ request, params }) => {
        capturedBody = (await request.json()) as Record<string, unknown>
        expect(params.personaId).toBe('3')
        return HttpResponse.json(okResult(mockPersona({ id: 10, name: 'companion-3-v2' })))
      }),
    )
    renderWithProviders(
      <CopyPersonaDialog open onOpenChange={onOpenChange} persona={source} />,
    )
    await user.clear(screen.getByLabelText('新注册名'))
    await user.type(screen.getByLabelText('新注册名'), 'companion-3-v2')
    await user.type(screen.getByLabelText(/展示名/), '复制人格')
    await user.click(screen.getByLabelText(/深拷贝 M\/S 状态层/))
    await user.click(screen.getByRole('button', { name: '复制' }))
    await waitFor(() => expect(onOpenChange).toHaveBeenCalledWith(false))
    expect(capturedBody).toEqual({
      new_name: 'companion-3-v2',
      display_name: '复制人格',
      include_states: true,
    })
    expect(await screen.findByText(/人格已复制为/)).toBeInTheDocument()
  })

  it('409 注册名占用 → toast 错误呈现后端提示', async () => {
    const user = userEvent.setup()
    server.use(
      http.post(`${BACKEND_ORIGIN}/personas/:personaId/copy`, () =>
        HttpResponse.json(errorResult('注册名已被占用', 'CONFLICT'), { status: 409 }),
      ),
    )
    renderWithProviders(
      <CopyPersonaDialog open onOpenChange={() => undefined} persona={source} />,
    )
    await user.click(screen.getByRole('button', { name: '复制' }))
    expect(await screen.findByText('注册名已被占用')).toBeInTheDocument()
  })
})

import { describe, expect, it } from 'vitest'
import userEvent from '@testing-library/user-event'
import { fireEvent, screen } from '@testing-library/react'
import { http, HttpResponse } from 'msw'
import { EpisodeInjectDialog } from './EpisodeInjectDialog'
import { BACKEND_ORIGIN, errorResult, server } from '@/test/server'
import { renderWithProviders } from '@/test/utils'

describe('EpisodeInjectDialog', () => {
  it('默认表单：kind=chat + 载荷模板；kind 切换换骨架', async () => {
    const user = userEvent.setup()
    renderWithProviders(
      <EpisodeInjectDialog open onOpenChange={() => undefined} personaId={1} />,
    )
    const payload = await screen.findByLabelText<HTMLTextAreaElement>(/载荷 JSON/)
    expect(payload.value).toContain('user_content')
    await user.selectOptions(screen.getByLabelText('事件类型'), 'duty')
    expect(payload.value).toContain('title')
    expect(screen.getByText(/duty 载荷：title \/ summary/)).toBeInTheDocument()
    await user.selectOptions(screen.getByLabelText('事件类型'), 'dream')
    expect(payload.value).toContain('text')
  })

  it('校验失败：source 空 / importance 越界 / JSON 非法 → 内联错误 + toast', async () => {
    const user = userEvent.setup()
    renderWithProviders(
      <EpisodeInjectDialog open onOpenChange={() => undefined} personaId={1} />,
    )
    await screen.findByLabelText(/载荷 JSON/)
    await user.clear(screen.getByLabelText('事件来源'))
    await user.clear(screen.getByLabelText(/重要度（1-10）/))
    await user.type(screen.getByLabelText(/重要度（1-10）/), '99')
    await user.click(screen.getByRole('button', { name: '注入' }))
    expect(await screen.findByText('事件来源必填，且长度为 1-128 个字符')).toBeInTheDocument()
    expect(screen.getByText('重要度须为 1-10 的整数')).toBeInTheDocument()
    expect(await screen.findByText('表单校验未通过，请检查标红字段')).toBeInTheDocument()

    fireEvent.change(screen.getByLabelText(/载荷 JSON/), { target: { value: '{bad json' } })
    await user.click(screen.getByRole('button', { name: '注入' }))
    expect(await screen.findByText('JSON 解析失败，请检查格式')).toBeInTheDocument()
  })

  it('载荷类型不符（duty details 给数组）→ 中文错误', async () => {
    const user = userEvent.setup()
    renderWithProviders(
      <EpisodeInjectDialog open onOpenChange={() => undefined} personaId={1} />,
    )
    await screen.findByLabelText(/载荷 JSON/)
    await user.selectOptions(screen.getByLabelText('事件类型'), 'duty')
    fireEvent.change(screen.getByLabelText(/载荷 JSON/), {
      target: { value: '{"details": [1]}' },
    })
    await user.click(screen.getByRole('button', { name: '注入' }))
    expect(await screen.findByText('duty 载荷的 details 须为 JSON 对象')).toBeInTheDocument()
  })

  it('合法提交 → 成功 toast + 关闭', async () => {
    const user = userEvent.setup()
    renderWithProviders(
      <EpisodeInjectDialog open onOpenChange={() => undefined} personaId={1} />,
    )
    await screen.findByLabelText(/载荷 JSON/)
    await user.type(screen.getByLabelText('事件来源'), 'admin-console')
    await user.click(screen.getByRole('button', { name: '注入' }))
    expect(await screen.findByText(/情景事件 #9 已记录/)).toBeInTheDocument()
  })

  it('后端 400（载荷与 kind 不匹配）→ toast 呈现后端 message', async () => {
    const user = userEvent.setup()
    server.use(
      http.post(`${BACKEND_ORIGIN}/memories/episodes`, () =>
        HttpResponse.json(
          errorResult('请求参数错误', 'VALIDATION_ERROR', {
            errors: [{ loc: ['body', 'content'], msg: 'Input should be a valid dictionary', type: 'model_type' }],
          }),
          { status: 400 },
        ),
      ),
    )
    renderWithProviders(
      <EpisodeInjectDialog open onOpenChange={() => undefined} personaId={1} />,
    )
    await screen.findByLabelText(/载荷 JSON/)
    await user.type(screen.getByLabelText('事件来源'), 'admin-console')
    await user.click(screen.getByRole('button', { name: '注入' }))
    expect(await screen.findByText('请求参数错误')).toBeInTheDocument()
  })
})

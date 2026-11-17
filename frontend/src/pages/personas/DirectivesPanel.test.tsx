import { describe, expect, it } from 'vitest'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { screen, waitFor } from '@testing-library/react'
import { DirectivesPanel } from './DirectivesPanel'
import { BACKEND_ORIGIN, errorResult, mockDirective, okResult, server } from '@/test/server'
import { renderWithProviders } from '@/test/utils'

describe('DirectivesPanel', () => {
  it('加载现行指令列表（正文 + 版本 + 生效徽章）', async () => {
    renderWithProviders(<DirectivesPanel personaId={1} />)
    expect(await screen.findByTestId('directive-row-2')).toBeInTheDocument()
    expect(screen.getByText('不要超过十二点睡')).toBeInTheDocument()
    expect(screen.getByText('以后都用简体中文')).toBeInTheDocument()
    expect(screen.getAllByText('生效中')).toHaveLength(2)
  })

  it('创建：正文必填校验 + 成功后清空草稿', async () => {
    const user = userEvent.setup()
    renderWithProviders(<DirectivesPanel personaId={1} />)
    await screen.findByTestId('directive-row-2')

    await user.click(screen.getByRole('button', { name: /新建常备指令/ }))
    expect(await screen.findByText(/长期指令正文必填/)).toBeInTheDocument()

    await user.type(screen.getByLabelText('长期指令正文'), '以后都用简体中文')
    await user.click(screen.getByRole('button', { name: /新建常备指令/ }))
    expect(await screen.findByText(/长期指令已保存（v1）/)).toBeInTheDocument()
    await waitFor(() => expect(screen.getByLabelText('长期指令正文')).toHaveValue(''))
  })

  it('编辑：预填正文 → 保存走 supersede 更新路径', async () => {
    const user = userEvent.setup()
    let updatedContent: string | null = null
    server.use(
      http.put(`${BACKEND_ORIGIN}/directives/:directiveId`, async ({ request, params }) => {
        const body = (await request.json()) as { content: string }
        updatedContent = body.content
        return HttpResponse.json(
          okResult(mockDirective({ id: Number(params.directiveId) + 100, content: body.content, version: 2 })),
        )
      }),
    )
    renderWithProviders(<DirectivesPanel personaId={1} />)
    const row = await screen.findByTestId('directive-row-2')
    await user.click(
      Array.from(row.querySelectorAll('button')).find(
        (candidate) => candidate.textContent === '编辑',
      ) as HTMLButtonElement,
    )
    expect(screen.getByLabelText('长期指令正文')).toHaveValue('不要超过十二点睡')
    await user.clear(screen.getByLabelText('长期指令正文'))
    await user.type(screen.getByLabelText('长期指令正文'), '不要超过十一点睡')
    await user.click(screen.getByRole('button', { name: '保存修改' }))
    expect(await screen.findByText(/长期指令已更新（v2）/)).toBeInTheDocument()
    expect(updatedContent).toBe('不要超过十一点睡')
  })

  it('取消编辑：草稿清空', async () => {
    const user = userEvent.setup()
    renderWithProviders(<DirectivesPanel personaId={1} />)
    const row = await screen.findByTestId('directive-row-2')
    await user.click(
      Array.from(row.querySelectorAll('button')).find(
        (candidate) => candidate.textContent === '编辑',
      ) as HTMLButtonElement,
    )
    await user.click(screen.getByRole('button', { name: '取消编辑' }))
    expect(screen.getByLabelText('长期指令正文')).toHaveValue('')
    expect(screen.getByRole('button', { name: /新建常备指令/ })).toBeInTheDocument()
  })

  it('删除：确认后成功 toast；404 → 错误 toast', async () => {
    const user = userEvent.setup()
    renderWithProviders(<DirectivesPanel personaId={1} />)
    const row = await screen.findByTestId('directive-row-2')
    await user.click(
      Array.from(row.querySelectorAll('button')).find(
        (candidate) => candidate.textContent === '删除',
      ) as HTMLButtonElement,
    )
    expect(screen.getByText('删除常备指令')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: '删除' }))
    expect(await screen.findByText('常备指令已删除')).toBeInTheDocument()

    server.use(
      http.delete(`${BACKEND_ORIGIN}/directives/:directiveId`, () =>
        HttpResponse.json(errorResult('长期指令不存在（非本域策展行）', 'NOT_FOUND'), {
          status: 404,
        }),
      ),
    )
    const rowAgain = await screen.findByTestId('directive-row-2')
    await user.click(
      Array.from(rowAgain.querySelectorAll('button')).find(
        (candidate) => candidate.textContent === '删除',
      ) as HTMLButtonElement,
    )
    await user.click(screen.getByRole('button', { name: '删除' }))
    expect(await screen.findByText(/长期指令不存在/)).toBeInTheDocument()
  })

  it('空列表 → 空态；加载失败 → 错误呈现', async () => {
    server.use(http.get(`${BACKEND_ORIGIN}/directives`, () => HttpResponse.json(okResult([]))))
    renderWithProviders(<DirectivesPanel personaId={1} />)
    expect(await screen.findByText('暂无常备指令')).toBeInTheDocument()

    server.use(
      http.get(`${BACKEND_ORIGIN}/directives`, () =>
        HttpResponse.json(errorResult('人格不存在', 'NOT_FOUND'), { status: 404 }),
      ),
    )
    renderWithProviders(<DirectivesPanel personaId={1} />)
    expect(await screen.findByText(/常备指令加载失败：人格不存在/)).toBeInTheDocument()
  })

  it('personaId=0（新建模式）：不发起请求', () => {
    renderWithProviders(<DirectivesPanel personaId={0} />)
    expect(screen.queryByTestId('directive-row-1')).not.toBeInTheDocument()
  })
})

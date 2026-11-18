import { describe, expect, it } from 'vitest'
import { fireEvent, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { CoreMemoryPanel } from './CoreMemoryPanel'
import {
  BACKEND_ORIGIN,
  errorResult,
  mockCoreMemoryBlock,
  mockCoreMemoryRevision,
  okResult,
  server,
} from '@/test/server'
import { renderWithProviders } from '@/test/utils'

describe('CoreMemoryPanel', () => {
  it('四块卡片：固定块序 / 锁定徽章 / 内容回填', async () => {
    renderWithProviders(<CoreMemoryPanel personaId={1} />)
    expect(await screen.findByTestId('core-memory-card-self_identity')).toBeInTheDocument()
    expect(screen.getByTestId('core-memory-card-owner_profile')).toBeInTheDocument()
    expect(screen.getByTestId('core-memory-card-relationship_story')).toBeInTheDocument()
    expect(screen.getByTestId('core-memory-card-open_notes')).toBeInTheDocument()
    expect(screen.getByText('我们的故事')).toBeInTheDocument()
    expect(screen.getByLabelText('自我认知正文')).toHaveValue('我是一条会写字的鱼')
    expect(screen.getByLabelText('关于对方正文')).toHaveValue('主人喜欢无糖乌龙茶')
    expect(screen.getByLabelText('锁定（关于对方）')).toBeChecked()
    expect(screen.getByLabelText('锁定（自我认知）')).not.toBeChecked()
  })

  it('保存：正文与锁定开关原样提交 + 成功 toast', async () => {
    const user = userEvent.setup()
    let body: { content?: string; locked?: boolean } | null = null
    server.use(
      http.put(`${BACKEND_ORIGIN}/core-memory/blocks/:block`, async ({ request }) => {
        body = (await request.json()) as { content?: string; locked?: boolean }
        return HttpResponse.json(
          okResult(
            mockCoreMemoryBlock({ content: body.content ?? '', version: 4, updated_by: 'owner' }),
            '核心记忆已保存',
          ),
        )
      }),
    )
    renderWithProviders(<CoreMemoryPanel personaId={1} />)
    await screen.findByTestId('core-memory-card-self_identity')
    const textarea = screen.getByLabelText('自我认知正文')
    await user.clear(textarea)
    await user.type(textarea, '我是会写字也会改记忆的鱼')
    await user.click(screen.getByLabelText('保存自我认知'))

    expect(await screen.findByText(/「自我认知」已保存（v4）/)).toBeInTheDocument()
    expect(body).toEqual({ content: '我是会写字也会改记忆的鱼', locked: false })
  })

  it('解锁：锁定块保存时提交 locked=false', async () => {
    const user = userEvent.setup()
    let body: { content?: string; locked?: boolean } | null = null
    server.use(
      http.put(`${BACKEND_ORIGIN}/core-memory/blocks/:block`, async ({ request }) => {
        body = (await request.json()) as { content?: string; locked?: boolean }
        return HttpResponse.json(
          okResult(
            mockCoreMemoryBlock({
              block: 'owner_profile',
              content: body.content ?? '',
              locked: body.locked ?? false,
              version: 4,
              updated_by: 'owner',
            }),
          ),
        )
      }),
    )
    renderWithProviders(<CoreMemoryPanel personaId={1} />)
    await screen.findByTestId('core-memory-card-owner_profile')
    await user.click(screen.getByLabelText('锁定（关于对方）'))
    await user.click(screen.getByLabelText('保存关于对方'))
    await waitFor(() => expect(body).toEqual({ content: '主人喜欢无糖乌龙茶', locked: false }))
  })

  it('超长预校验：超过 2000 字符时提示且不发请求', async () => {
    let requested = false
    server.use(
      http.put(`${BACKEND_ORIGIN}/core-memory/blocks/:block`, () => {
        requested = true
        return HttpResponse.json(okResult(mockCoreMemoryBlock()))
      }),
    )
    renderWithProviders(<CoreMemoryPanel personaId={1} />)
    const textarea = await screen.findByLabelText('自我认知正文')
    fireEvent.change(textarea, { target: { value: 'x'.repeat(2001) } })
    fireEvent.click(screen.getByLabelText('保存自我认知'))
    expect(
      await screen.findByText(/核心记忆正文不能超过 2000 个字符/),
    ).toBeInTheDocument()
    expect(requested).toBe(false)
  })

  it('修订历史抽屉：最新在前 + 写入者 / 来源标签', async () => {
    const user = userEvent.setup()
    renderWithProviders(<CoreMemoryPanel personaId={1} />)
    await screen.findByTestId('core-memory-card-self_identity')
    await user.click(screen.getByLabelText('修订历史自我认知'))

    const newest = await screen.findByTestId('core-memory-revision-12')
    expect(newest).toHaveTextContent('v3 · 主人 · 管理面')
    expect(newest).toHaveTextContent('我更正了自我认知')
    const older = screen.getByTestId('core-memory-revision-11')
    expect(older).toHaveTextContent('v2 · 人格自编辑 · 对话内')
    expect(older).toHaveTextContent('我是一条会写字的鱼')
  })

  it('保存失败：后端错误消息进 toast', async () => {
    const user = userEvent.setup()
    server.use(
      http.put(`${BACKEND_ORIGIN}/core-memory/blocks/:block`, () =>
        HttpResponse.json(errorResult('核心记忆块不存在', 'NOT_FOUND'), { status: 404 }),
      ),
    )
    renderWithProviders(<CoreMemoryPanel personaId={1} />)
    await screen.findByTestId('core-memory-card-self_identity')
    await user.click(screen.getByLabelText('保存自我认知'))
    expect(await screen.findByText(/核心记忆块不存在/)).toBeInTheDocument()
  })

  it('加载失败：错误提示 + 重试成功后渲染四块', async () => {
    const user = userEvent.setup()
    server.use(
      http.get(`${BACKEND_ORIGIN}/core-memory/blocks`, () =>
        HttpResponse.json(errorResult('核心记忆加载失败（测试）', 'INTERNAL_ERROR'), { status: 500 }),
      ),
    )
    renderWithProviders(<CoreMemoryPanel personaId={1} />)
    expect(await screen.findByText(/核心记忆加载失败：核心记忆加载失败（测试）/)).toBeInTheDocument()

    server.use(
      http.get(`${BACKEND_ORIGIN}/core-memory/blocks`, () =>
        HttpResponse.json(okResult([mockCoreMemoryBlock()])),
      ),
    )
    await user.click(screen.getByRole('button', { name: '重试' }))
    expect(await screen.findByTestId('core-memory-card-self_identity')).toBeInTheDocument()
  })

  it('修订为空：抽屉显示空态说明', async () => {
    const user = userEvent.setup()
    server.use(
      http.get(`${BACKEND_ORIGIN}/core-memory/blocks/:block/revisions`, () =>
        HttpResponse.json(okResult([])),
      ),
    )
    renderWithProviders(<CoreMemoryPanel personaId={1} />)
    await screen.findByTestId('core-memory-card-self_identity')
    await user.click(screen.getByLabelText('修订历史自我认知'))
    expect(await screen.findByText('该块还没有内容修订。')).toBeInTheDocument()
  })

  it('修订加载失败 + 已清空正文：错误提示与占位文案', async () => {
    const user = userEvent.setup()
    server.use(
      http.get(`${BACKEND_ORIGIN}/core-memory/blocks/:block/revisions`, () =>
        HttpResponse.json(errorResult('修订读取失败（测试）', 'INTERNAL_ERROR'), { status: 500 }),
      ),
    )
    renderWithProviders(<CoreMemoryPanel personaId={1} />)
    await screen.findByTestId('core-memory-card-self_identity')
    await user.click(screen.getByLabelText('修订历史自我认知'))
    expect(await screen.findByText(/修订历史加载失败：修订读取失败（测试）/)).toBeInTheDocument()

    server.use(
      http.get(`${BACKEND_ORIGIN}/core-memory/blocks/:block/revisions`, () =>
        HttpResponse.json(
          okResult([mockCoreMemoryRevision({ id: 21, content: '', version: 4 })]),
        ),
      ),
    )
    await user.keyboard('{Escape}')
    await user.click(screen.getByLabelText('修订历史关于对方'))
    expect(await screen.findByTestId('core-memory-revision-21')).toHaveTextContent('（已清空）')
  })

  it('关闭抽屉：按钮与 Escape 均收起', async () => {
    const user = userEvent.setup()
    renderWithProviders(<CoreMemoryPanel personaId={1} />)
    await screen.findByTestId('core-memory-card-self_identity')
    await user.click(screen.getByLabelText('修订历史自我认知'))
    await screen.findByTestId('core-memory-revision-12')
    await user.keyboard('{Escape}')
    await waitFor(() =>
      expect(screen.queryByTestId('core-memory-revision-12')).not.toBeInTheDocument(),
    )

    await user.click(screen.getByLabelText('修订历史自我认知'))
    await screen.findByTestId('core-memory-revision-12')
    await user.click(screen.getByRole('button', { name: '关闭' }))
    await waitFor(() =>
      expect(screen.queryByTestId('core-memory-revision-12')).not.toBeInTheDocument(),
    )
  })
})

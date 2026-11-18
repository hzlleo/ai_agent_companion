import { beforeEach, describe, expect, it, vi } from 'vitest'
import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import CanvasPage from './CanvasPage'
import { BACKEND_ORIGIN, errorResult, mockCanvas, okResult, server } from '@/test/server'
import { renderWithProviders } from '@/test/utils'
import type { CanvasCreateRequest } from '@/types/canvas'

/** 导出下载的浏览器面替身（jsdom 不实现对象 URL 与真实导航）。 */
const createObjectURL = vi.fn(() => 'blob:canvas-export')
const revokeObjectURL = vi.fn()
const anchorClick = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})

describe('CanvasPage', () => {
  beforeEach(() => {
    createObjectURL.mockClear()
    revokeObjectURL.mockClear()
    anchorClick.mockClear()
    Object.assign(URL, { createObjectURL, revokeObjectURL })
  })

  it('列表：HTML 预览沙箱 / 类型徽章 / 修订号 / 更新时间', async () => {
    renderWithProviders(<CanvasPage />)
    const preview = await screen.findByTitle('画布预览 1')
    expect(preview.tagName).toBe('IFRAME')
    expect(preview.getAttribute('sandbox')).toBe('')
    expect(preview.getAttribute('srcdoc')).toBe('<div>今天很好</div>')
    expect(screen.getByText('HTML 卡片')).toBeInTheDocument()
    expect(screen.getByText(/修订 1/)).toBeInTheDocument()
  })

  it('列表：Markdown 产物按纯文本展示；类型过滤改变查询', async () => {
    const user = userEvent.setup()
    let lastKind: string | null = null
    server.use(
      http.get(`${BACKEND_ORIGIN}/canvas`, ({ request }) => {
        lastKind = new URL(request.url).searchParams.get('kind')
        return HttpResponse.json(
          okResult([
            mockCanvas({
              id: 2,
              title: '笔记',
              kind: 'markdown',
              content: '# 标题',
            }),
          ]),
        )
      }),
    )
    renderWithProviders(<CanvasPage />)
    expect(await screen.findByText('# 标题')).toBeInTheDocument()
    expect(screen.queryByTitle('画布预览 2')).not.toBeInTheDocument()
    await user.selectOptions(screen.getByLabelText('类型'), 'markdown')
    await waitFor(() => expect(lastKind).toBe('markdown'))
  })

  it('空态与加载失败提示', async () => {
    server.use(
      http.get(`${BACKEND_ORIGIN}/canvas`, () => HttpResponse.json(okResult([]))),
    )
    const { unmount } = renderWithProviders(<CanvasPage />)
    expect(await screen.findByText('还没有画布')).toBeInTheDocument()
    unmount()

    server.use(
      http.get(`${BACKEND_ORIGIN}/canvas`, () =>
        HttpResponse.json(errorResult('画布服务不可用', 'INTERNAL_ERROR'), { status: 500 }),
      ),
    )
    renderWithProviders(<CanvasPage />)
    expect(await screen.findByText(/画布加载失败/)).toBeInTheDocument()
  })

  it('创建校验：标题与内容为空均提示且不发请求', async () => {
    const user = userEvent.setup()
    let posts = 0
    server.use(
      http.post(`${BACKEND_ORIGIN}/canvas`, () => {
        posts += 1
        return HttpResponse.json(okResult(mockCanvas()))
      }),
    )
    renderWithProviders(<CanvasPage />)
    await screen.findByText(/修订 1/)
    await user.click(screen.getByRole('button', { name: '创建画布' }))
    expect(await screen.findByText('请填写画布标题')).toBeInTheDocument()
    await user.type(screen.getByLabelText('画布标题'), '新卡片')
    await user.click(screen.getByRole('button', { name: '创建画布' }))
    expect(await screen.findByText('请填写画布内容')).toBeInTheDocument()
    expect(posts).toBe(0)
  })

  it('创建成功：提交载荷正确并清空输入', async () => {
    const user = userEvent.setup()
    let payload: CanvasCreateRequest | null = null
    server.use(
      http.post(`${BACKEND_ORIGIN}/canvas`, async ({ request }) => {
        payload = (await request.json()) as CanvasCreateRequest
        return HttpResponse.json(okResult(mockCanvas({ title: payload.title })))
      }),
    )
    renderWithProviders(<CanvasPage />)
    await screen.findByText(/修订 1/)
    await user.type(screen.getByLabelText('画布标题'), '新卡片')
    await user.type(screen.getByLabelText('画布内容'), '<div>新内容</div>')
    await user.click(screen.getByRole('button', { name: '创建画布' }))
    expect(await screen.findByText(/画布已创建/)).toBeInTheDocument()
    expect(payload).toEqual({
      title: '新卡片',
      kind: 'html',
      content: '<div>新内容</div>',
    })
    expect(screen.getByLabelText('画布标题')).toHaveValue('')
  })

  it('创建失败：错误提示来自后端 message', async () => {
    const user = userEvent.setup()
    server.use(
      http.post(`${BACKEND_ORIGIN}/canvas`, () =>
        HttpResponse.json(errorResult('画布标题不得为纯空白', 'VALIDATION_ERROR'), {
          status: 400,
        }),
      ),
    )
    renderWithProviders(<CanvasPage />)
    await screen.findByText(/修订 1/)
    await user.type(screen.getByLabelText('画布标题'), '卡片')
    await user.type(screen.getByLabelText('画布内容'), '<div/>')
    await user.click(screen.getByRole('button', { name: '创建画布' }))
    expect(await screen.findByText('画布标题不得为纯空白')).toBeInTheDocument()
  })

  it('修订：保存后提示新修订号；取消不提交', async () => {
    const user = userEvent.setup()
    renderWithProviders(<CanvasPage />)
    await screen.findByText(/修订 1/)
    await user.click(screen.getByRole('button', { name: '编辑画布 1' }))
    const contentBox = screen.getByLabelText('画布 1 内容')
    await user.clear(contentBox)
    await user.type(contentBox, '<div>二版</div>')
    await user.click(screen.getByRole('button', { name: '保存' }))
    expect(await screen.findByText(/画布已保存（修订 2）/)).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: '编辑画布 1' }))
    await user.click(screen.getByRole('button', { name: '取消' }))
    expect(screen.getByTitle('画布预览 1')).toBeInTheDocument()
  })

  it('修订失败：提示错误并保持编辑态', async () => {
    const user = userEvent.setup()
    server.use(
      http.patch(`${BACKEND_ORIGIN}/canvas/:canvasId`, () =>
        HttpResponse.json(errorResult('画布不存在', 'NOT_FOUND'), { status: 404 }),
      ),
    )
    renderWithProviders(<CanvasPage />)
    await screen.findByText(/修订 1/)
    await user.click(screen.getByRole('button', { name: '编辑画布 1' }))
    await user.click(screen.getByRole('button', { name: '保存' }))
    expect(await screen.findByText('画布不存在')).toBeInTheDocument()
    expect(screen.getByLabelText('画布 1 内容')).toBeInTheDocument()
  })

  it('导出：对象 URL 下载 + 服务端文件名 + 释放', async () => {
    const user = userEvent.setup()
    renderWithProviders(<CanvasPage />)
    await screen.findByText(/修订 1/)
    await user.click(screen.getByRole('button', { name: '导出' }))
    expect(await screen.findByText('已导出 canvas-1-r1.html')).toBeInTheDocument()
    expect(createObjectURL).toHaveBeenCalledTimes(1)
    expect(anchorClick).toHaveBeenCalledTimes(1)
    expect(revokeObjectURL).toHaveBeenCalledWith('blob:canvas-export')
  })

  it('导出失败：提示错误且不触发下载', async () => {
    const user = userEvent.setup()
    server.use(
      http.get(`${BACKEND_ORIGIN}/canvas/:canvasId/export`, () =>
        HttpResponse.json(errorResult('导出目标不存在', 'NOT_FOUND'), { status: 404 }),
      ),
    )
    renderWithProviders(<CanvasPage />)
    await screen.findByText(/修订 1/)
    await user.click(screen.getByRole('button', { name: '导出' }))
    expect(await screen.findByText('请求的资源不存在')).toBeInTheDocument()
    expect(anchorClick).not.toHaveBeenCalled()
  })

  it('删除：确认后提示成功；缺失报告提示已不存在', async () => {
    const user = userEvent.setup()
    renderWithProviders(<CanvasPage />)
    await screen.findByText(/修订 1/)
    await user.click(screen.getByRole('button', { name: '删除画布 1' }))
    await user.click(screen.getByRole('button', { name: '删除' }))
    expect(await screen.findByText(/已删除画布「今日心情卡」/)).toBeInTheDocument()

    server.use(
      http.delete(`${BACKEND_ORIGIN}/canvas/:canvasId`, () =>
        HttpResponse.json(okResult({ deleted_ids: [] })),
      ),
    )
    await user.click(screen.getByRole('button', { name: '删除画布 1' }))
    await user.click(screen.getByRole('button', { name: '删除' }))
    expect(await screen.findByText('画布 #1 已不存在')).toBeInTheDocument()
  })

  it('删除失败：提示后端 message', async () => {
    const user = userEvent.setup()
    server.use(
      http.delete(`${BACKEND_ORIGIN}/canvas/:canvasId`, () =>
        HttpResponse.json(errorResult('画布服务不可用', 'INTERNAL_ERROR'), { status: 500 }),
      ),
    )
    renderWithProviders(<CanvasPage />)
    await screen.findByText(/修订 1/)
    await user.click(screen.getByRole('button', { name: '删除画布 1' }))
    await user.click(screen.getByRole('button', { name: '删除' }))
    expect(await screen.findByText('画布服务不可用')).toBeInTheDocument()
  })
})

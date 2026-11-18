import { describe, expect, it } from 'vitest'
import { fireEvent, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import StickersPage from './StickersPage'
import {
  BACKEND_ORIGIN,
  errorResult,
  mockSticker,
  okResult,
  server,
} from '@/test/server'
import { renderWithProviders } from '@/test/utils'
import type { StickerBatchUploadRequest } from '@/types/stickers'

/** 上传用 PNG 文件（内容任意——读取助手只做 base64）。 */
function pngFile(name = 'cat.png'): File {
  return new File([new Uint8Array([137, 80, 78, 71])], name, { type: 'image/png' })
}

describe('StickersPage', () => {
  it('列表：缩略图 / 来源备注 / 标注状态展示', async () => {
    renderWithProviders(<StickersPage />)
    expect(await screen.findByText('来源与授权：自绘素材')).toBeInTheDocument()
    expect(screen.getAllByText('待标注').length).toBeGreaterThan(1)
    expect(screen.getByAltText('笑着挥手')).toBeInTheDocument()
    expect(screen.queryByText('标注模型：local-vision')).not.toBeInTheDocument()
  })

  it('空态与加载失败提示', async () => {
    server.use(
      http.get(`${BACKEND_ORIGIN}/stickers`, () => HttpResponse.json(okResult([]))),
    )
    const { unmount } = renderWithProviders(<StickersPage />)
    expect(await screen.findByText('还没有表情包')).toBeInTheDocument()
    unmount()

    server.use(
      http.get(`${BACKEND_ORIGIN}/stickers`, () =>
        HttpResponse.json(errorResult('表情包库未启用', 'CONFLICT'), { status: 409 }),
      ),
    )
    renderWithProviders(<StickersPage />)
    expect(await screen.findByText(/表情包加载失败/)).toBeInTheDocument()
  })

  it('上传校验：未选文件 / 未填来源备注均提示且不发请求', async () => {
    const user = userEvent.setup()
    let uploads = 0
    server.use(
      http.post(`${BACKEND_ORIGIN}/stickers`, () => {
        uploads += 1
        return HttpResponse.json(okResult({ created: [], rejected: [] }))
      }),
    )
    renderWithProviders(<StickersPage />)
    await screen.findByText('来源与授权：自绘素材')
    await user.click(screen.getByRole('button', { name: '上传到表情包库' }))
    expect(await screen.findByText('请先选择要上传的图片')).toBeInTheDocument()

    await user.upload(screen.getByLabelText('选择表情包图片'), pngFile())
    expect(await screen.findByText('已选择 1 个文件')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: '上传到表情包库' }))
    expect(
      await screen.findByText('请填写素材来源与授权备注（法律边界提示义务）'),
    ).toBeInTheDocument()
    expect(uploads).toBe(0)
  })

  it('上传成功：提交来源备注 / 标签 / base64 并清空选择', async () => {
    const user = userEvent.setup()
    const captured: { body: StickerBatchUploadRequest | null } = { body: null }
    server.use(
      http.post(`${BACKEND_ORIGIN}/stickers`, async ({ request }) => {
        captured.body = (await request.json()) as StickerBatchUploadRequest
        return HttpResponse.json(
          okResult({ created: [mockSticker()], rejected: [] }, '表情包上传完成'),
        )
      }),
    )
    renderWithProviders(<StickersPage />)
    await screen.findByText('来源与授权：自绘素材')
    await user.upload(screen.getByLabelText('选择表情包图片'), pngFile())
    await user.type(screen.getByLabelText('来源与授权备注'), '朋友授权使用')
    await user.type(screen.getByLabelText('初始标签'), '开心, 猫猫')
    await user.click(screen.getByRole('button', { name: '上传到表情包库' }))

    expect(await screen.findByText('已上传 1 张表情包')).toBeInTheDocument()
    expect(captured.body?.items[0]?.source_note).toBe('朋友授权使用')
    expect(captured.body?.items[0]?.tags).toEqual(['开心', '猫猫'])
    expect(captured.body?.items[0]?.content_type).toBe('image/png')
    expect(captured.body?.items[0]?.image_base64).toBeTruthy()
    await waitFor(() => expect(screen.queryByText('已选择 1 个文件')).not.toBeInTheDocument())
  })

  it('上传拒收 / 不支持类型 / 读取失败提示', async () => {
    const user = userEvent.setup()
    server.use(
      http.post(`${BACKEND_ORIGIN}/stickers`, () =>
        HttpResponse.json(
          okResult({
            created: [],
            rejected: [{ index: 0, reason: 'duplicate', message: '重复' }],
          }),
        ),
      ),
    )
    const { unmount } = renderWithProviders(<StickersPage />)
    await screen.findByText('来源与授权：自绘素材')
    await user.type(screen.getByLabelText('来源与授权备注'), '自绘')
    fireEvent.change(screen.getByLabelText('选择表情包图片'), {
      target: { files: [new File(['x'], 'a.bmp', { type: 'image/bmp' })] },
    })
    await user.click(screen.getByRole('button', { name: '上传到表情包库' }))
    expect(await screen.findByText('不支持的图片类型：a.bmp')).toBeInTheDocument()

    fireEvent.change(screen.getByLabelText('选择表情包图片'), {
      target: { files: [pngFile('x.png')] },
    })
    await user.click(screen.getByRole('button', { name: '上传到表情包库' }))
    expect(await screen.findByText('没有新表情入库（跳过 1 条：重复或校验失败）')).toBeInTheDocument()
    unmount()
  })

  it('标签保存：提交新标签并提示', async () => {
    const user = userEvent.setup()
    let body: unknown = null
    server.use(
      http.patch(`${BACKEND_ORIGIN}/stickers/:id`, async ({ request }) => {
        body = await request.json()
        return HttpResponse.json(okResult(mockSticker({ tags: ['新标签'] })))
      }),
    )
    renderWithProviders(<StickersPage />)
    const input = await screen.findByLabelText('表情包 1 标签')
    await user.clear(input)
    await user.type(input, '新标签, 二次')
    await user.click(screen.getAllByRole('button', { name: '保存' })[0] as HTMLElement)
    expect(await screen.findByText(/标签已保存/)).toBeInTheDocument()
    expect(body).toEqual({ tags: ['新标签', '二次'], description: null, source_note: null })
  })

  it('标注失败可读提示与成功提示', async () => {
    const user = userEvent.setup()
    server.use(
      http.post(`${BACKEND_ORIGIN}/stickers/:id/annotate`, () =>
        HttpResponse.json(
          okResult({
            ok: false,
            record: mockSticker({ annotation_status: 'failed', annotation_error: '端点不可用' }),
            message: '端点不可用',
          }),
        ),
      ),
    )
    const { unmount } = renderWithProviders(<StickersPage />)
    await screen.findByText('来源与授权：自绘素材')
    await user.click(screen.getByRole('button', { name: '自动标注' }))
    expect(await screen.findByText('标注失败：端点不可用')).toBeInTheDocument()
    unmount()
    server.resetHandlers()

    renderWithProviders(<StickersPage />)
    await screen.findByText('来源与授权：自绘素材')
    await user.click(screen.getByRole('button', { name: '自动标注' }))
    expect(await screen.findByText('标注完成')).toBeInTheDocument()
  })

  it('删除：确认后提交并提示；已不存在提示', async () => {
    const user = userEvent.setup()
    server.use(
      http.delete(`${BACKEND_ORIGIN}/stickers/:id`, ({ params }) =>
        HttpResponse.json(
          okResult({ deleted_ids: [Number(params.id)], missing_ids: [] }),
        ),
      ),
    )
    renderWithProviders(<StickersPage />)
    await screen.findByText('来源与授权：自绘素材')
    await user.click(screen.getByLabelText('删除表情包 1'))
    await user.click(screen.getByRole('button', { name: '删除' }))
    expect(await screen.findByText('已删除表情包 #1')).toBeInTheDocument()

    server.use(
      http.delete(`${BACKEND_ORIGIN}/stickers/:id`, () =>
        HttpResponse.json(okResult({ deleted_ids: [], missing_ids: [1] })),
      ),
    )
    await user.click(screen.getByLabelText('删除表情包 1'))
    await user.click(screen.getByRole('button', { name: '删除' }))
    expect(await screen.findByText('表情包 #1 已不存在')).toBeInTheDocument()
  })

  it('标注状态过滤进查询串', async () => {
    const user = userEvent.setup()
    let status: string | null = null
    server.use(
      http.get(`${BACKEND_ORIGIN}/stickers`, ({ request }) => {
        status = new URL(request.url).searchParams.get('annotation_status')
        return HttpResponse.json(okResult([mockSticker()]))
      }),
    )
    renderWithProviders(<StickersPage />)
    await screen.findByText('来源与授权：自绘素材')
    await user.selectOptions(screen.getByLabelText('标注状态'), 'failed')
    await waitFor(() => expect(status).toBe('failed'))
  })

  it('标注错误与模型条目在卡片上可见', async () => {
    server.use(
      http.get(`${BACKEND_ORIGIN}/stickers`, () =>
        HttpResponse.json(
          okResult([
            mockSticker({
              annotation_status: 'failed',
              annotation_error: '端点不可用',
              annotation_model_entry: 'local-vision',
            }),
          ]),
        ),
      ),
    )
    renderWithProviders(<StickersPage />)
    expect(await screen.findByText('标注错误：端点不可用')).toBeInTheDocument()
    expect(screen.getByText('标注模型：local-vision')).toBeInTheDocument()
    expect(screen.getAllByText('标注失败').length).toBeGreaterThan(1)
    expect(screen.getByRole('button', { name: '重试标注' })).toBeInTheDocument()
  })

  it('无预览数据占位', async () => {
    server.use(
      http.get(`${BACKEND_ORIGIN}/stickers`, () =>
        HttpResponse.json(okResult([mockSticker({ data_base64: null, description: '' })])),
      ),
    )
    renderWithProviders(<StickersPage />)
    await screen.findByText('表情包 #1')
    expect(screen.getByText('无预览数据')).toBeInTheDocument()
    expect(screen.getByText('（尚未标注描述）')).toBeInTheDocument()
  })
})

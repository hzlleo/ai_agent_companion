import { describe, expect, it } from 'vitest'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { screen } from '@testing-library/react'
import { CuratedHistoryDialog } from './CuratedHistoryDialog'
import { BACKEND_ORIGIN, errorResult, okResult, server } from '@/test/server'
import { renderWithProviders } from '@/test/utils'

describe('CuratedHistoryDialog', () => {
  it('版本升序历史链渲染（现行版本末行）', async () => {
    renderWithProviders(
      <CuratedHistoryDialog open onOpenChange={() => undefined} personaId={1} anchorMemoryId={3} />,
    )
    expect(await screen.findByTestId('curated-history-row-2')).toBeInTheDocument()
    expect(screen.getByTestId('curated-history-row-3')).toBeInTheDocument()
    expect(screen.getByText('旧版本：主人最近睡得不太好。')).toBeInTheDocument()
    expect(screen.getAllByText('现行').length).toBeGreaterThan(0)
    expect(screen.getAllByText('已取代').length).toBeGreaterThan(0)
  })

  it('空链 → 提示文案', async () => {
    server.use(
      http.get(`${BACKEND_ORIGIN}/memories/curated/:id/history`, () =>
        HttpResponse.json(okResult([])),
      ),
    )
    renderWithProviders(
      <CuratedHistoryDialog open onOpenChange={() => undefined} personaId={1} anchorMemoryId={3} />,
    )
    expect(await screen.findByText('该主题暂无历史版本。')).toBeInTheDocument()
  })

  it('404 → 中文错误呈现', async () => {
    server.use(
      http.get(`${BACKEND_ORIGIN}/memories/curated/:id/history`, () =>
        HttpResponse.json(errorResult('记忆不存在或不在此人格作用域', 'NOT_FOUND'), { status: 404 }),
      ),
    )
    renderWithProviders(
      <CuratedHistoryDialog open onOpenChange={() => undefined} personaId={1} anchorMemoryId={404} />,
    )
    expect(
      await screen.findByText(/历史链加载失败：记忆不存在或不在此人格作用域/),
    ).toBeInTheDocument()
  })

  it('关闭路径：知道了与右上角关闭均触发 onOpenChange(false)', async () => {
    const user = userEvent.setup()
    let closed = false
    renderWithProviders(
      <CuratedHistoryDialog
        open
        onOpenChange={(open) => {
          closed = !open
        }}
        personaId={1}
        anchorMemoryId={3}
      />,
    )
    await screen.findByTestId('curated-history-row-2')
    await user.click(screen.getByRole('button', { name: '知道了' }))
    expect(closed).toBe(true)
    await user.click(screen.getByRole('button', { name: '关闭对话框' }))
    expect(closed).toBe(true)
  })

  it('未选择锚点（anchorMemoryId=0）→ 提示文案', async () => {
    renderWithProviders(
      <CuratedHistoryDialog open onOpenChange={() => undefined} personaId={1} anchorMemoryId={0} />,
    )
    expect(await screen.findByText('未选择记忆。')).toBeInTheDocument()
  })
})

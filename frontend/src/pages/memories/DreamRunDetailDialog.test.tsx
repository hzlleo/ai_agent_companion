import { describe, expect, it } from 'vitest'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { screen } from '@testing-library/react'
import { DreamRunDetailDialog } from './DreamRunDetailDialog'
import { BACKEND_ORIGIN, errorResult, mockDreamRun, okResult, server } from '@/test/server'
import { renderWithProviders } from '@/test/utils'

describe('DreamRunDetailDialog', () => {
  it('详情拉取：运行报告完整呈现（统计 + 决策徽章 + 日记回源）', async () => {
    server.use(
      http.get(`${BACKEND_ORIGIN}/memories/episodes/:id`, () =>
        HttpResponse.json(
          okResult({
            id: 12,
            persona_id: '1',
            kind: 'dream',
            source: 'dream-diary',
            content: { text: '主人提到深夜想吃热汤面。', metadata: {} },
            importance: 5,
            provenance: 'owner',
            content_hash: 'deadbeefdeadbeef',
            ts_ms: 1731000000000,
            created_at: 1731000000000,
            updated_at: 1731000000000,
          }),
        ),
      ),
    )
    renderWithProviders(
      <DreamRunDetailDialog open onOpenChange={() => undefined} personaId={1} runId={1} />,
    )
    expect(await screen.findByTestId('dream-run-report-1')).toBeInTheDocument()
    expect(screen.getByTestId('dream-decision-1')).toBeInTheDocument()
    expect(screen.getByTestId('dream-decision-3')).toBeInTheDocument()
    expect(screen.getAllByText('晋升').length).toBeGreaterThan(0)
    expect(screen.getAllByText('并入').length).toBeGreaterThan(0)
    expect(screen.getAllByText('跳过').length).toBeGreaterThan(0)
    expect(await screen.findByText(/日记事件 #12/)).toBeInTheDocument()
    expect(await screen.findByText(/主人提到深夜想吃热汤面。/)).toBeInTheDocument()
  })

  it('404 → 中文错误呈现', async () => {
    server.use(
      http.get(`${BACKEND_ORIGIN}/memories/dream/runs/:id`, () =>
        HttpResponse.json(errorResult('运行不存在或不在此人格作用域', 'NOT_FOUND'), { status: 404 }),
      ),
    )
    renderWithProviders(
      <DreamRunDetailDialog open onOpenChange={() => undefined} personaId={1} runId={404} />,
    )
    expect(await screen.findByText(/运行详情加载失败：运行不存在或不在此人格作用域/)).toBeInTheDocument()
  })

  it('未选择运行（runId=0）→ 提示文案；关闭路径（知道了 + 右上角）', async () => {
    const user = userEvent.setup()
    let closed = false
    const noTargetRender = renderWithProviders(
      <DreamRunDetailDialog
        open
        onOpenChange={(open) => {
          closed = !open
        }}
        personaId={1}
        runId={0}
      />,
    )
    expect(await screen.findByText('未选择运行。')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: '知道了' }))
    expect(closed).toBe(true)
    noTargetRender.unmount()

    renderWithProviders(
      <DreamRunDetailDialog open onOpenChange={() => undefined} personaId={1} runId={1} />,
    )
    await screen.findByTestId('dream-run-report-1')
    await user.click(screen.getByRole('button', { name: '关闭对话框' }))
    expect(closed).toBe(true)
  })

  it('mockDreamRun 工厂默认形态（类型面完整性锚点）', () => {
    const run = mockDreamRun()
    expect(run.stats.candidates).toBe(3)
    expect(run.decisions).toHaveLength(3)
    expect(run.diary_episode_id).toBe(12)
  })
})

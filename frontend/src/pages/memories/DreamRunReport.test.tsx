import { describe, expect, it } from 'vitest'
import { http, HttpResponse } from 'msw'
import { screen } from '@testing-library/react'
import { DreamRunReport } from './DreamRunReport'
import { BACKEND_ORIGIN, mockDreamRun, okResult, server } from '@/test/server'
import { renderWithProviders } from '@/test/utils'

describe('DreamRunReport', () => {
  it('统计瓦片 + 决策表（动作徽章 / 主题 / 理由）+ 状态徽章', async () => {
    renderWithProviders(<DreamRunReport run={mockDreamRun({ id: 7 })} personaId={1} />)
    expect(await screen.findByTestId('dream-run-report-7')).toBeInTheDocument()
    expect(screen.getByText('成功')).toBeInTheDocument()
    expect(screen.getByText('可信候选')).toBeInTheDocument()
    expect(screen.getByText('untrusted 硬排除')).toBeInTheDocument()
    expect(screen.getByText('分数达标，晋升为新主题')).toBeInTheDocument()
    expect(screen.getByText('favorite-food')).toBeInTheDocument()
    expect(screen.getByText('低于分数线')).toBeInTheDocument()
    expect(screen.getAllByText('—').length).toBeGreaterThan(0)
  })

  it('无决策 → 提示文案；无日记 → 提示文案', async () => {
    renderWithProviders(
      <DreamRunReport
        run={mockDreamRun({ decisions: [], diary_episode_id: null })}
        personaId={1}
      />,
    )
    expect(await screen.findByText('本次运行没有候选事件决策。')).toBeInTheDocument()
    expect(screen.getByText(/本次运行未生成日记事件/)).toBeInTheDocument()
  })

  it('failed 终态：失败原因警示条', async () => {
    renderWithProviders(
      <DreamRunReport
        run={mockDreamRun({ status: 'failed', error: '日记生成失败' })}
        personaId={1}
      />,
    )
    expect(await screen.findByText(/失败原因：日记生成失败/)).toBeInTheDocument()
    expect(screen.getByText('失败')).toBeInTheDocument()
  })

  it('日记回源失败 → 降级提示（不阻塞报告）', async () => {
    server.use(
      http.get(`${BACKEND_ORIGIN}/memories/episodes/:id`, () =>
        HttpResponse.json(okResult(null, '事件不存在'), { status: 404 }),
      ),
    )
    renderWithProviders(<DreamRunReport run={mockDreamRun()} personaId={1} />)
    expect(await screen.findByText(/日记事件 #12 读取失败/)).toBeInTheDocument()
  })
})

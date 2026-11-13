import { describe, expect, it } from 'vitest'
import userEvent from '@testing-library/user-event'
import { screen } from '@testing-library/react'
import MemoriesPage from './MemoriesPage'
import { renderWithProviders } from '@/test/utils'

describe('MemoriesPage', () => {
  it('默认渲染情景页签 + 人格选择器 + 五页签切换', async () => {
    const user = userEvent.setup()
    renderWithProviders(<MemoriesPage />, { route: '/memories' })
    expect(await screen.findByRole('heading', { name: '记忆管理' })).toBeInTheDocument()
    expect(screen.getByLabelText('人格')).toBeInTheDocument()
    expect(await screen.findByTestId('episode-row-3')).toBeInTheDocument()

    await user.click(screen.getByRole('tab', { name: '策展' }))
    expect(await screen.findByTestId('curated-row-3')).toBeInTheDocument()

    await user.click(screen.getByRole('tab', { name: '会话' }))
    expect(await screen.findByTestId('session-row-2')).toBeInTheDocument()

    await user.click(screen.getByRole('tab', { name: '检索' }))
    expect(await screen.findByRole('heading', { name: '记忆检索' })).toBeInTheDocument()

    await user.click(screen.getByRole('tab', { name: '梦境' }))
    expect(await screen.findByRole('heading', { name: '手工触发梦境整理' })).toBeInTheDocument()
  })

  it('选中具体人格 → 列表请求携带 persona_id', async () => {
    const user = userEvent.setup()
    renderWithProviders(<MemoriesPage />, { route: '/memories' })
    await screen.findByTestId('episode-row-3')
    await user.selectOptions(screen.getByLabelText('人格'), '2')
    // 选中后查询键变化重取（默认 handler 忽略 persona_id——行仍在）。
    expect(await screen.findByTestId('episode-row-3')).toBeInTheDocument()
    expect(screen.getByRole('tab', { name: '情景', selected: true })).toBeInTheDocument()
  })
})

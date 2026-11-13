import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { AppShell } from './AppShell'

describe('AppShell', () => {
  it('渲染四个导航入口与产品标题', () => {
    render(
      <MemoryRouter initialEntries={['/personas']}>
        <AppShell />
      </MemoryRouter>,
    )
    expect(screen.getByText('AI 陪伴伙伴 · 管理台')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: '人格' })).toHaveAttribute('href', '/personas')
    expect(screen.getByRole('link', { name: '记忆' })).toHaveAttribute('href', '/memories')
    expect(screen.getByRole('link', { name: '用量' })).toHaveAttribute('href', '/usage')
    expect(screen.getByRole('link', { name: '模型' })).toHaveAttribute('href', '/models')
    expect(screen.getByRole('navigation', { name: '主导航' })).toBeInTheDocument()
  })

  it('当前路由对应导航项高亮', () => {
    render(
      <MemoryRouter initialEntries={['/personas']}>
        <AppShell />
      </MemoryRouter>,
    )
    const activeLink = screen.getByRole('link', { name: '人格' })
    expect(activeLink.className).toContain('bg-[var(--color-sidebar-active)]')
    const inactiveLink = screen.getByRole('link', { name: '记忆' })
    expect(inactiveLink.className).not.toContain('bg-[var(--color-sidebar-active)]')
  })
})

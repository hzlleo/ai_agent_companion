import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import { PlaceholderPage } from './PlaceholderPage'

describe('PlaceholderPage', () => {
  it('渲染「建设中」与模块名', () => {
    render(<PlaceholderPage moduleName="记忆" />)
    expect(screen.getByText('记忆')).toBeInTheDocument()
    expect(screen.getByText('建设中')).toBeInTheDocument()
    expect(screen.getByText('「记忆」模块正在建设中，敬请期待。')).toBeInTheDocument()
  })
})

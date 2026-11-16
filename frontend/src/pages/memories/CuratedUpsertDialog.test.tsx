import { describe, expect, it } from 'vitest'
import userEvent from '@testing-library/user-event'
import { screen } from '@testing-library/react'
import { CuratedUpsertDialog } from './CuratedUpsertDialog'
import { mockCurated } from '@/test/server'
import { renderWithProviders } from '@/test/utils'

describe('CuratedUpsertDialog', () => {
  it('新建：supersede 语义提示呈现；校验错误内联', async () => {
    const user = userEvent.setup()
    renderWithProviders(
      <CuratedUpsertDialog open onOpenChange={() => undefined} personaId={1} initial={null} />,
    )
    expect(await screen.findByRole('heading', { name: '新建策展记忆' })).toBeInTheDocument()
    expect(screen.getByText(/supersede 语义/)).toBeInTheDocument()
    expect(screen.getByText(/平台内容（untrusted 来源）永不入策展层/)).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: '保存' }))
    expect(await screen.findByText('主题键必填，且长度为 1-255 个字符')).toBeInTheDocument()
    expect(screen.getByText('记忆正文必填')).toBeInTheDocument()
    expect(await screen.findByText('表单校验未通过，请检查标红字段')).toBeInTheDocument()
  })

  it('重要度越界 → 内联错误', async () => {
    const user = userEvent.setup()
    renderWithProviders(
      <CuratedUpsertDialog open onOpenChange={() => undefined} personaId={1} initial={null} />,
    )
    await screen.findByRole('heading', { name: '新建策展记忆' })
    await user.clear(screen.getByLabelText(/重要度（1-10）/))
    await user.type(screen.getByLabelText(/重要度（1-10）/), '20')
    await user.click(screen.getByRole('button', { name: '保存' }))
    expect(await screen.findByText('重要度须为 1-10 的整数')).toBeInTheDocument()
  })

  it('编辑锚点预填（同主题 upsert）；成功 toast 携带新版本', async () => {
    const user = userEvent.setup()
    const anchor = mockCurated({ id: 3, topic: 'sleep-pattern', version: 2 })
    renderWithProviders(
      <CuratedUpsertDialog open onOpenChange={() => undefined} personaId={1} initial={anchor} />,
    )
    expect(await screen.findByLabelText('主题键')).toHaveValue('sleep-pattern')
    expect(screen.getByLabelText('策展类型')).toHaveValue('memory')
    expect(screen.getByLabelText(/记忆正文/)).toHaveValue(anchor.content)
    await user.click(screen.getByRole('button', { name: '保存' }))
    expect(await screen.findByText(/现行版本 v2/)).toBeInTheDocument()
  })

  it('untrusted 来源不在可信度选项中', async () => {
    renderWithProviders(
      <CuratedUpsertDialog open onOpenChange={() => undefined} personaId={1} initial={null} />,
    )
    await screen.findByRole('heading', { name: '新建策展记忆' })
    const provenanceSelect = screen.getByLabelText('来源可信度')
    const options = Array.from(provenanceSelect.querySelectorAll('option'))
    expect(options.map((option) => option.value)).toEqual(['owner', 'agent'])
  })

  it('全字段交互：类型 / 主题 / 正文 / 重要度 / 可信度 + 取消关闭', async () => {
    const user = userEvent.setup()
    let closed = false
    renderWithProviders(
      <CuratedUpsertDialog
        open
        onOpenChange={(open) => {
          closed = !open
        }}
        personaId={1}
        initial={null}
      />,
    )
    await screen.findByRole('heading', { name: '新建策展记忆' })
    await user.selectOptions(screen.getByLabelText('策展类型'), 'user_note')
    await user.type(screen.getByLabelText('主题键'), 'favorite-food')
    await user.type(screen.getByLabelText(/记忆正文/), '主人喜欢周末睡懒觉。')
    await user.clear(screen.getByLabelText(/重要度（1-10）/))
    await user.type(screen.getByLabelText(/重要度（1-10）/), '8')
    await user.selectOptions(screen.getByLabelText('来源可信度'), 'agent')
    await user.click(screen.getByRole('button', { name: '取消' }))
    expect(closed).toBe(true)
  })
})

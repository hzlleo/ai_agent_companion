import { describe, expect, it } from 'vitest'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { screen, waitFor } from '@testing-library/react'
import { SkillsPanel } from './SkillsPanel'
import { BACKEND_ORIGIN, errorResult, mockSkill, okResult, server } from '@/test/server'
import { renderWithProviders } from '@/test/utils'

describe('SkillsPanel', () => {
  it('加载技能：名称 / 描述 / 触发短语 / 状态 / 来源 / 命中次数', async () => {
    renderWithProviders(<SkillsPanel personaId={1} />)
    const row = await screen.findByTestId('skill-row-1')
    expect(row).toHaveTextContent('周报流程')
    expect(row).toHaveTextContent('主人说「写周报」时使用')
    expect(row).toHaveTextContent('写周报')
    expect(row).toHaveTextContent('本周总结')
    expect(row).toHaveTextContent('已启用')
    expect(row).toHaveTextContent('梦境挖矿')
    expect(row).toHaveTextContent('命中 3 次')
    expect(await screen.findByTestId('skill-row-2')).toHaveTextContent('已停用')
  })

  it('新建技能：必填校验 + 触发短语逐行解析 + 成功后关闭并 toast', async () => {
    const user = userEvent.setup()
    let sentTriggers: string[] | null = null
    server.use(
      http.post(`${BACKEND_ORIGIN}/skills`, async ({ request }) => {
        const body = (await request.json()) as {
          name: string
          description: string
          triggers: string[]
          content: string
        }
        sentTriggers = body.triggers
        return HttpResponse.json(
          okResult(
            mockSkill({
              id: 9,
              name: body.name,
              description: body.description,
              triggers: body.triggers,
              content: body.content,
              source: 'manual',
              use_count: 0,
              last_used_ms: null,
            }),
          ),
        )
      }),
    )
    renderWithProviders(<SkillsPanel personaId={1} />)
    await screen.findByTestId('skill-row-1')

    await user.click(screen.getByRole('button', { name: /新建技能/ }))
    await user.click(screen.getByRole('button', { name: '保存技能' }))
    expect(await screen.findByText(/技能名称必填/)).toBeInTheDocument()

    await user.type(screen.getByLabelText('技能名称'), '日报流程')
    await user.type(screen.getByLabelText('触发描述'), '主人说「写日报」时使用')
    await user.type(screen.getByLabelText('触发短语（每行一条）'), '写日报{enter}今日总结')
    await user.type(screen.getByLabelText('步骤正文（markdown）'), '1. 写今日完成')
    await user.click(screen.getByRole('button', { name: '保存技能' }))
    expect(await screen.findByText(/技能已创建/)).toBeInTheDocument()
    expect(sentTriggers).toEqual(['写日报', '今日总结'])
    await waitFor(() => expect(screen.queryByLabelText('技能名称')).not.toBeInTheDocument())
  })

  it('同名冲突：409 消息透传到 toast', async () => {
    const user = userEvent.setup()
    server.use(
      http.post(`${BACKEND_ORIGIN}/skills`, () =>
        HttpResponse.json(
          errorResult('同人格下已存在同名技能（slug 冲突），请改名或直接编辑该技能', 'CONFLICT'),
          { status: 409 },
        ),
      ),
    )
    renderWithProviders(<SkillsPanel personaId={1} />)
    await screen.findByTestId('skill-row-1')
    await user.click(screen.getByRole('button', { name: /新建技能/ }))
    await user.type(screen.getByLabelText('技能名称'), '周报流程')
    await user.type(screen.getByLabelText('触发描述'), '描述')
    await user.type(screen.getByLabelText('触发短语（每行一条）'), '写周报')
    await user.type(screen.getByLabelText('步骤正文（markdown）'), '1. 步骤')
    await user.click(screen.getByRole('button', { name: '保存技能' }))
    expect(await screen.findByText(/slug 冲突/)).toBeInTheDocument()
  })

  it('编辑：预填字段 → 保存走原地更新路径（版本提示）', async () => {
    const user = userEvent.setup()
    let updatedContent: string | null = null
    server.use(
      http.put(`${BACKEND_ORIGIN}/skills/:skillId`, async ({ request, params }) => {
        const body = (await request.json()) as { content: string }
        updatedContent = body.content
        return HttpResponse.json(
          okResult(mockSkill({ id: Number(params.skillId), content: body.content, version: 2 })),
        )
      }),
    )
    renderWithProviders(<SkillsPanel personaId={1} />)
    const row = await screen.findByTestId('skill-row-1')
    await user.click(
      Array.from(row.querySelectorAll('button')).find(
        (candidate) => candidate.textContent === '编辑',
      ) as HTMLButtonElement,
    )
    expect(screen.getByLabelText('技能名称')).toHaveValue('周报流程')
    expect(screen.getByLabelText('触发短语（每行一条）')).toHaveValue('写周报\n本周总结')
    await user.clear(screen.getByLabelText('步骤正文（markdown）'))
    await user.type(screen.getByLabelText('步骤正文（markdown）'), '1. 新的步骤')
    await user.click(screen.getByRole('button', { name: '保存修改' }))
    expect(await screen.findByText(/技能已保存（v2）/)).toBeInTheDocument()
    expect(updatedContent).toBe('1. 新的步骤')
  })

  it('加载失败：错误提示 + 重试重新拉取', async () => {
    const user = userEvent.setup()
    server.use(
      http.get(`${BACKEND_ORIGIN}/skills`, () =>
        HttpResponse.json(errorResult('后端暂不可用', 'INTERNAL_ERROR'), { status: 500 }),
      ),
    )
    renderWithProviders(<SkillsPanel personaId={1} />)
    expect(await screen.findByText(/技能加载失败/)).toBeInTheDocument()
    server.use(
      http.get(`${BACKEND_ORIGIN}/skills`, () =>
        HttpResponse.json(
          okResult({
            items: [mockSkill()],
            total: -1,
            page: 1,
            page_size: 200,
            next_cursor: null,
            has_more: false,
          }),
        ),
      ),
    )
    await user.click(screen.getByRole('button', { name: '重试' }))
    expect(await screen.findByTestId('skill-row-1')).toBeInTheDocument()
  })

  it('表单校验：描述 / 触发短语条数与长度 / 正文 / 置信度逐项拦截', async () => {
    const user = userEvent.setup()
    renderWithProviders(<SkillsPanel personaId={1} />)
    await screen.findByTestId('skill-row-1')
    await user.click(screen.getByRole('button', { name: /新建技能/ }))

    await user.type(screen.getByLabelText('技能名称'), '校验技能')
    await user.click(screen.getByRole('button', { name: '保存技能' }))
    expect(await screen.findByText(/触发描述必填/)).toBeInTheDocument()

    await user.type(screen.getByLabelText('触发描述'), '描述')
    await user.click(screen.getByRole('button', { name: '保存技能' }))
    expect(await screen.findByText(/触发短语须为 1-8 条/)).toBeInTheDocument()

    const triggers = screen.getByLabelText('触发短语（每行一条）')
    await user.type(triggers, Array.from({ length: 9 }, (_, index) => `触发${index}`).join('\n'))
    await user.click(screen.getByRole('button', { name: '保存技能' }))
    expect(await screen.findByText(/触发短语须为 1-8 条/)).toBeInTheDocument()

    await user.clear(triggers)
    await user.type(triggers, '长'.repeat(41))
    await user.click(screen.getByRole('button', { name: '保存技能' }))
    expect(await screen.findByText(/单条触发短语不能超过 40/)).toBeInTheDocument()

    await user.clear(triggers)
    await user.type(triggers, '写周报')
    await user.click(screen.getByRole('button', { name: '保存技能' }))
    expect(await screen.findByText(/步骤正文必填/)).toBeInTheDocument()

    await user.type(screen.getByLabelText('步骤正文（markdown）'), '1. 步骤')
    await user.clear(screen.getByLabelText('置信度（0-1）'))
    await user.type(screen.getByLabelText('置信度（0-1）'), '2')
    await user.click(screen.getByRole('button', { name: '保存技能' }))
    expect(await screen.findByText(/置信度须为 0-1/)).toBeInTheDocument()
  })

  it('启用：停用行点击启用后 toast', async () => {
    const user = userEvent.setup()
    renderWithProviders(<SkillsPanel personaId={1} />)
    const disabledRow = await screen.findByTestId('skill-row-2')
    await user.click(
      Array.from(disabledRow.querySelectorAll('button')).find(
        (candidate) => candidate.textContent === '启用',
      ) as HTMLButtonElement,
    )
    expect(await screen.findByText('技能已启用')).toBeInTheDocument()
  })

  it('操作失败：停用错误与删除错误透传 toast', async () => {
    const user = userEvent.setup()
    server.use(
      http.post(`${BACKEND_ORIGIN}/skills/:skillId/disable`, () =>
        HttpResponse.json(errorResult('技能不存在', 'NOT_FOUND'), { status: 404 }),
      ),
      http.delete(`${BACKEND_ORIGIN}/skills/:skillId`, () =>
        HttpResponse.json(errorResult('技能不存在', 'NOT_FOUND'), { status: 404 }),
      ),
    )
    renderWithProviders(<SkillsPanel personaId={1} />)
    const row = await screen.findByTestId('skill-row-1')
    await user.click(
      Array.from(row.querySelectorAll('button')).find(
        (candidate) => candidate.textContent === '停用',
      ) as HTMLButtonElement,
    )
    expect(await screen.findByText('技能不存在')).toBeInTheDocument()

    const secondRow = await screen.findByTestId('skill-row-2')
    await user.click(
      Array.from(secondRow.querySelectorAll('button')).find(
        (candidate) => candidate.textContent === '删除',
      ) as HTMLButtonElement,
    )
    const confirmButtons = screen.getAllByRole('button', { name: '删除' })
    await user.click(confirmButtons[confirmButtons.length - 1] as HTMLButtonElement)
    expect((await screen.findAllByText('技能不存在')).length).toBeGreaterThan(0)
  })

  it('停用：直接生效 toast；删除走确认框', async () => {
    const user = userEvent.setup()
    renderWithProviders(<SkillsPanel personaId={1} />)
    const row = await screen.findByTestId('skill-row-1')
    await user.click(
      Array.from(row.querySelectorAll('button')).find(
        (candidate) => candidate.textContent === '停用',
      ) as HTMLButtonElement,
    )
    expect(await screen.findByText('技能已停用')).toBeInTheDocument()

    const secondRow = await screen.findByTestId('skill-row-2')
    await user.click(
      Array.from(secondRow.querySelectorAll('button')).find(
        (candidate) => candidate.textContent === '删除',
      ) as HTMLButtonElement,
    )
    expect(screen.getByText('删除技能')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: '删除' }))
    expect(await screen.findByText('技能已删除')).toBeInTheDocument()
  })
})

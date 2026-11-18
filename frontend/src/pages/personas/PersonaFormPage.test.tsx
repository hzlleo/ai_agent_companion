import { describe, expect, it } from 'vitest'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { screen, waitFor } from '@testing-library/react'
import PersonaFormPage from './PersonaFormPage'
import {
  BACKEND_ORIGIN,
  errorResult,
  mockCard,
  mockPersona,
  okResult,
  server,
} from '@/test/server'
import { renderWithProviders, NAVIGATED_AWAY_TESTID } from '@/test/utils'

describe('PersonaFormPage — 创建模式', () => {
  it('空表单提交：校验错误内联呈现（不发请求）', async () => {
    const user = userEvent.setup()
    server.use(
      http.post(`${BACKEND_ORIGIN}/personas`, () => {
        throw new Error('不应发出请求')
      }),
    )
    renderWithProviders(<PersonaFormPage mode="create" />, { route: '/personas/new' })
    await user.click(screen.getByRole('button', { name: '创建人格' }))
    expect(await screen.findByText('注册名必填，且长度为 1-128 个字符')).toBeInTheDocument()
    expect(screen.getByText('卡片称呼名必填，且长度为 1-128 个字符')).toBeInTheDocument()
    expect(await screen.findByText('表单校验未通过，请检查标红字段')).toBeInTheDocument()
  })

  it('填写后提交：POST 携带完整请求体（含世界书条目收敛）', async () => {
    const user = userEvent.setup()
    let capturedBody: Record<string, unknown> | null = null
    server.use(
      http.post(`${BACKEND_ORIGIN}/personas`, async ({ request }) => {
        capturedBody = (await request.json()) as Record<string, unknown>
        return HttpResponse.json(okResult(mockPersona({ id: 9, name: 'night-1' }), '人格已创建'))
      }),
    )
    renderWithProviders(<PersonaFormPage mode="create" />, { route: '/personas/new' })

    await user.type(screen.getByLabelText(/注册名/), 'night-1')
    await user.type(screen.getByLabelText(/展示名/), '小夜')
    await user.selectOptions(screen.getByLabelText(/可用性状态/), 'active')
    await user.type(screen.getByLabelText('称呼名'), '小夜')
    await user.type(screen.getByLabelText('简介'), '温柔的深夜陪伴')
    await user.type(screen.getByLabelText('对话示例'), '<START>\nuser: 我好累')
    await user.type(screen.getByLabelText('系统提示覆写'), '你是温柔的朋友')

    await user.click(screen.getByRole('checkbox', { name: '启用世界书（character_book，检索增强条目集）' }))
    await user.type(screen.getByLabelText('世界书名称'), '夜谈词库')
    await user.type(screen.getByLabelText(/扫描深度/), '5')
    await user.type(screen.getByLabelText(/主触发关键词/), '问候，晚安')
    await user.click(screen.getByRole('checkbox', { name: '选择性触发' }))
    await user.type(screen.getByLabelText(/副触发关键词/), '夜谈')
    await user.type(screen.getByLabelText('条目正文'), '问候时先关心对方状态')
    await user.clear(screen.getByLabelText(/插入顺序/))
    await user.type(screen.getByLabelText(/插入顺序/), '42')

    await user.click(screen.getByRole('button', { name: '创建人格' }))

    await waitFor(() => expect(capturedBody).not.toBeNull())
    expect(capturedBody).toEqual({
      name: 'night-1',
      display_name: '小夜',
      status: 'active',
      card: {
        name: '小夜',
        description: '温柔的深夜陪伴',
        personality: '',
        scenario: '',
        first_mes: '',
        mes_example: '<START>\nuser: 我好累',
        system_prompt: '你是温柔的朋友',
        post_history_instructions: '',
        character_book: {
          name: '夜谈词库',
          description: '',
          scan_depth: 5,
          token_budget: null,
          recursive_scanning: false,
          entries: [
            {
              keys: ['问候', '晚安'],
              secondary_keys: ['夜谈'],
              content: '问候时先关心对方状态',
              insertion_order: 42,
              constant: false,
              case_sensitive: false,
              selective: true,
              position: 'after_char',
              depth: 4,
              recursive: false,
              include_group: null,
              trigger_probability: 1,
              cooldown_turns: 0,
            },
          ],
        },
      },
      voice_profile: null,
    })
  })

  it('创建成功 → toast + 返回列表', async () => {
    const user = userEvent.setup()
    server.use(
      http.post(`${BACKEND_ORIGIN}/personas`, () =>
        HttpResponse.json(okResult(mockPersona({ id: 9, name: 'night-1' }), '人格已创建')),
      ),
    )
    renderWithProviders(<PersonaFormPage mode="create" />, { route: '/personas/new' })
    await user.type(screen.getByLabelText(/注册名/), 'night-1')
    await user.type(screen.getByLabelText('称呼名'), '小夜')
    await user.click(screen.getByRole('button', { name: '创建人格' }))
    expect(await screen.findByText(/已创建/)).toBeInTheDocument()
    expect(await screen.findByTestId(NAVIGATED_AWAY_TESTID)).toBeInTheDocument()
  })

  it('创建 409（注册名占用）→ toast 呈现后端提示', async () => {
    const user = userEvent.setup()
    server.use(
      http.post(`${BACKEND_ORIGIN}/personas`, () =>
        HttpResponse.json(errorResult('注册名已被占用', 'CONFLICT'), { status: 409 }),
      ),
    )
    renderWithProviders(<PersonaFormPage mode="create" />, { route: '/personas/new' })
    await user.type(screen.getByLabelText(/注册名/), 'dup')
    await user.type(screen.getByLabelText('称呼名'), '小夜')
    await user.click(screen.getByRole('button', { name: '创建人格' }))
    expect(await screen.findByText('注册名已被占用')).toBeInTheDocument()
  })

  it('创建 400 校验错误 → toast 附注首条字段路径（details.errors 透出）', async () => {
    const user = userEvent.setup()
    server.use(
      http.post(`${BACKEND_ORIGIN}/personas`, () =>
        HttpResponse.json(
          errorResult('请求校验失败', 'VALIDATION_ERROR', {
            errors: [
              {
                loc: ['body', 'card', 'character_book', 'entries', 0, 'secondary_keys'],
                msg: 'Value error, selective=True 条目必须携带非空 secondary_keys',
                type: 'value_error',
              },
            ],
          }),
          { status: 400 },
        ),
      ),
    )
    renderWithProviders(<PersonaFormPage mode="create" />, { route: '/personas/new' })
    await user.type(screen.getByLabelText(/注册名/), 'night-1')
    await user.type(screen.getByLabelText('称呼名'), '小夜')
    await user.click(screen.getByRole('button', { name: '创建人格' }))
    expect(
      await screen.findByText(
        '请求校验失败（card.character_book.entries[0].secondary_keys: Value error, selective=True 条目必须携带非空 secondary_keys）',
      ),
    ).toBeInTheDocument()
  })

  it('JSON 预览实时反映表单态', async () => {
    const user = userEvent.setup()
    renderWithProviders(<PersonaFormPage mode="create" />, { route: '/personas/new' })
    await user.type(screen.getByLabelText('称呼名'), '小夜')
    const preview = JSON.parse(screen.getByTestId('card-json-preview').textContent) as {
      name: string
      character_book: string | null
    }
    expect(preview.name).toBe('小夜')
    expect(preview.character_book).toBeNull()
  })
})

describe('PersonaFormPage — 编辑模式', () => {
  it('加载记录回填表单 + 状态层面板', async () => {
    renderWithProviders(<PersonaFormPage mode="edit" />, { route: '/personas/1/edit', path: '/personas/:personaId/edit' })
    await waitFor(() =>
      expect(screen.getByLabelText(/注册名/)).toHaveValue('companion-1'),
    )
    expect(screen.getByLabelText(/展示名/)).toHaveValue('小助手')
    expect(screen.getByLabelText('称呼名')).toHaveValue('小助手')
    await waitFor(() =>
      expect(screen.getByText('M 层（中期：心情 / 关系评价）')).toBeInTheDocument(),
    )
    expect(screen.getByText('S 层（短期：会话内即时状态）')).toBeInTheDocument()
  })

  it('加载失败（404）→ 错误呈现 + 返回链接', async () => {
    server.use(
      http.get(`${BACKEND_ORIGIN}/personas/:personaId`, () =>
        HttpResponse.json(errorResult('人格不存在', 'NOT_FOUND'), { status: 404 }),
      ),
    )
    renderWithProviders(<PersonaFormPage mode="edit" />, { route: '/personas/404/edit', path: '/personas/:personaId/edit' })
    expect(await screen.findByText(/人格加载失败：人格不存在/)).toBeInTheDocument()
    expect(screen.getByText('返回人格列表')).toBeInTheDocument()
  })

  it('保存：基础字段 PUT /{id} + 卡片 PUT /{id}/card 两次落库', async () => {
    const user = userEvent.setup()
    const putBodies: Array<{ url: string; body: Record<string, unknown> }> = []
    server.use(
      http.put(`${BACKEND_ORIGIN}/personas/:personaId`, async ({ request }) => {
        putBodies.push({
          url: new URL(request.url).pathname,
          body: (await request.json()) as Record<string, unknown>,
        })
        return HttpResponse.json(okResult(mockPersona(), '人格已更新'))
      }),
      http.put(`${BACKEND_ORIGIN}/personas/:personaId/card`, async ({ request }) => {
        putBodies.push({
          url: new URL(request.url).pathname,
          body: (await request.json()) as Record<string, unknown>,
        })
        return HttpResponse.json(
          okResult({ persona_id: 1, card: { ...mockCard }, updated_at: 1 }, '人格卡片已替换'),
        )
      }),
    )
    renderWithProviders(<PersonaFormPage mode="edit" />, { route: '/personas/1/edit', path: '/personas/:personaId/edit' })
    await waitFor(() => expect(screen.getByLabelText(/注册名/)).toHaveValue('companion-1'))

    await user.clear(screen.getByLabelText(/注册名/))
    await user.type(screen.getByLabelText(/注册名/), 'companion-1-renamed')
    await user.clear(screen.getByLabelText('称呼名'))
    await user.type(screen.getByLabelText('称呼名'), '新称呼')
    await user.click(screen.getByRole('button', { name: '保存修改' }))

    await waitFor(() => expect(putBodies).toHaveLength(2))
    expect(putBodies[0]?.url).toBe('/api/v1/personas/1')
    expect(putBodies[0]?.body).toEqual({
      name: 'companion-1-renamed',
      display_name: '小助手',
      status: 'active',
      voice_profile: null,
    })
    expect(putBodies[1]?.url).toBe('/api/v1/personas/1/card')
    expect((putBodies[1]?.body as { name: string }).name).toBe('新称呼')
    expect(await screen.findByText('人格已更新（基础字段 + 卡片）')).toBeInTheDocument()
    expect(await screen.findByTestId(NAVIGATED_AWAY_TESTID)).toBeInTheDocument()
  })

  it('基础字段更新失败 → toast 且不触发卡片替换', async () => {
    const user = userEvent.setup()
    let cardPutCalled = false
    server.use(
      http.put(`${BACKEND_ORIGIN}/personas/:personaId`, () =>
        HttpResponse.json(errorResult('注册名已被占用', 'CONFLICT'), { status: 409 }),
      ),
      http.put(`${BACKEND_ORIGIN}/personas/:personaId/card`, () => {
        cardPutCalled = true
        return HttpResponse.json(okResult({ persona_id: 1, card: { ...mockCard }, updated_at: 1 }))
      }),
    )
    renderWithProviders(<PersonaFormPage mode="edit" />, { route: '/personas/1/edit', path: '/personas/:personaId/edit' })
    await waitFor(() => expect(screen.getByLabelText(/注册名/)).toHaveValue('companion-1'))
    await user.click(screen.getByRole('button', { name: '保存修改' }))
    expect(await screen.findByText('注册名已被占用')).toBeInTheDocument()
    expect(cardPutCalled).toBe(false)
  })
})

describe('PersonaFormPage — 语音绑定', () => {
  it('填写发音人 + 指令 + 语速 → 创建请求携带 voice_profile', async () => {
    const user = userEvent.setup()
    let capturedBody: Record<string, unknown> | null = null
    server.use(
      http.post(`${BACKEND_ORIGIN}/personas`, async ({ request }) => {
        capturedBody = (await request.json()) as Record<string, unknown>
        return HttpResponse.json(okResult(mockPersona({ id: 9 }), '人格已创建'))
      }),
    )
    renderWithProviders(<PersonaFormPage mode="create" />, { route: '/personas/new' })

    await user.click(screen.getByText('语音绑定（可选——TTS 发音人 / 风格指令 / 语速）'))
    await user.type(screen.getByLabelText('发音人（speaker）'), '温柔女声')
    await user.type(screen.getByLabelText('语速（0.5-2.0）'), '1.2')
    await user.type(screen.getByLabelText('instruct2 风格指令（可空）'), '语气轻柔，深夜向')
    await user.type(screen.getByLabelText(/注册名/), 'night-voice')
    await user.type(screen.getByLabelText('称呼名'), '小夜')
    await user.click(screen.getByRole('button', { name: '创建人格' }))

    await waitFor(() => expect(capturedBody).not.toBeNull())
    // msw 闭包内赋值不参与 CFA 收窄——显式还原声明类型后取字段。
    const body = capturedBody as Record<string, unknown> | null
    expect(body?.voice_profile).toEqual({
      speaker: '温柔女声',
      instruct_text: '语气轻柔，深夜向',
      speed: 1.2,
    })
  })

  it('语速界外 → 内联报错 + 面板自动展开（不发请求）', async () => {
    const user = userEvent.setup()
    server.use(
      http.post(`${BACKEND_ORIGIN}/personas`, () => {
        throw new Error('不应发出请求')
      }),
    )
    renderWithProviders(<PersonaFormPage mode="create" />, { route: '/personas/new' })
    await user.type(screen.getByLabelText(/注册名/), 'night-voice')
    await user.type(screen.getByLabelText('称呼名'), '小夜')

    // 面板初始折叠：直接提交一个仅语速非法的表单需要先展开填值。
    await user.click(screen.getByText('语音绑定（可选——TTS 发音人 / 风格指令 / 语速）'))
    await user.type(screen.getByLabelText('发音人（speaker）'), '温柔女声')
    await user.type(screen.getByLabelText('语速（0.5-2.0）'), '3')
    await user.click(screen.getByRole('button', { name: '创建人格' }))
    expect(await screen.findByText('语速须在 0.5-2.0 之间')).toBeInTheDocument()
    expect(screen.getByText('表单校验未通过，请检查标红字段')).toBeInTheDocument()
  })

  it('仅填语速而缺发音人 → 报错并自动展开面板', async () => {
    const user = userEvent.setup()
    renderWithProviders(<PersonaFormPage mode="create" />, { route: '/personas/new' })
    await user.type(screen.getByLabelText(/注册名/), 'night-voice')
    await user.type(screen.getByLabelText('称呼名'), '小夜')

    // 语速输入位于折叠面板内——jsdom 下 details 内容仍在 DOM，可直接填值。
    await user.type(screen.getByLabelText('语速（0.5-2.0）'), '1.2')
    await user.click(screen.getByRole('button', { name: '创建人格' }))
    expect(await screen.findByText('填写发音人后才会绑定语音（指令 / 语速不单独生效）')).toBeInTheDocument()
  })

  it('编辑模式：既有绑定回填 + 自动展开；清除发音人 → PUT 发送 voice_profile null', async () => {
    const user = userEvent.setup()
    server.use(
      http.get(`${BACKEND_ORIGIN}/personas/:personaId`, () =>
        HttpResponse.json(
          okResult(
            mockPersona({
              voice_profile: { speaker: '温柔女声', instruct_text: '语气轻柔', speed: 1.2 },
            }),
          ),
        ),
      ),
    )
    const putBodies: Array<Record<string, unknown>> = []
    server.use(
      http.put(`${BACKEND_ORIGIN}/personas/:personaId`, async ({ request }) => {
        putBodies.push((await request.json()) as Record<string, unknown>)
        return HttpResponse.json(okResult(mockPersona(), '人格已更新'))
      }),
      http.put(`${BACKEND_ORIGIN}/personas/:personaId/card`, () =>
        HttpResponse.json(okResult({ persona_id: 1, card: { ...mockCard }, updated_at: 1 })),
      ),
    )
    renderWithProviders(<PersonaFormPage mode="edit" />, { route: '/personas/1/edit', path: '/personas/:personaId/edit' })
    await waitFor(() => expect(screen.getByLabelText(/注册名/)).toHaveValue('companion-1'))

    // 既有绑定 → 面板自动展开 + 字段回填。
    expect(screen.getByLabelText('发音人（speaker）')).toHaveValue('温柔女声')
    // type=number 输入的 value 断言经 RTL 归一为数值形态。
    expect(screen.getByLabelText('语速（0.5-2.0）')).toHaveValue(1.2)
    expect(screen.getByLabelText('instruct2 风格指令（可空）')).toHaveValue('语气轻柔')

    // 清空全部语音字段 = 解绑（保存时整体替换为 null）。
    await user.clear(screen.getByLabelText('发音人（speaker）'))
    await user.clear(screen.getByLabelText('语速（0.5-2.0）'))
    await user.clear(screen.getByLabelText('instruct2 风格指令（可空）'))
    await user.click(screen.getByRole('button', { name: '保存修改' }))
    await waitFor(() => expect(putBodies).toHaveLength(1))
    expect(putBodies[0]?.voice_profile).toBeNull()
  })
})

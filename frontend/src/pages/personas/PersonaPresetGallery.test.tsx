import { describe, expect, it } from 'vitest'
import userEvent from '@testing-library/user-event'
import { fireEvent, screen, within } from '@testing-library/react'
import { http, HttpResponse } from 'msw'
import PersonaPresetGallery from './PersonaPresetGallery'
import {
  BACKEND_ORIGIN,
  errorResult,
  mockTraitLibrary,
  okResult,
  server,
} from '@/test/server'
import { NAVIGATED_AWAY_TESTID, renderWithProviders } from '@/test/utils'
import type { TraitPreset } from '@/types/persona'

/** 卡片内查询辅助（多卡同文案隔离）。 */
function inCard(cardId: string) {
  return within(screen.getByTestId(`preset-card-${cardId}`))
}

describe('PersonaPresetGallery', () => {
  it('渲染预设网格：名称 / 标语 / 描述 / 组件芯片（两预设 + 后缀提示）', async () => {
    renderWithProviders(<PersonaPresetGallery />, {
      route: '/personas/presets',
      path: '/personas/presets',
    })
    expect(await screen.findByTestId('preset-card-tsundere')).toBeInTheDocument()
    expect(screen.getByTestId('preset-card-genki-girl')).toBeInTheDocument()
    expect(screen.getAllByText('傲娇').length).toBeGreaterThan(0)
    expect(screen.getByText('嘴上全是拒绝，行动全是真心')).toBeInTheDocument()
    expect(screen.getByTestId('preset-chip-tsundere-duplicitous-mouth')).toHaveTextContent('口是心非')
    expect(screen.getByTestId('preset-chip-tsundere-soft-heart-hard-mouth')).toHaveTextContent('嘴硬心软')
    expect(screen.getByTestId('preset-chip-genki-girl-energetic-sunny')).toHaveTextContent('元气满满')
    // genki 预设声明了名字后缀 → 生成对话框提示（打开后可见）。
    const user = userEvent.setup()
    await user.click(inCard('genki-girl').getByRole('button', { name: '生成' }))
    expect(await screen.findByRole('heading', { name: '生成人格' })).toBeInTheDocument()
    expect(screen.getByText(/名字后缀「-酱」/)).toBeInTheDocument()
    expect(screen.getByLabelText('期望名')).toHaveValue('元气少女')
  })

  it('生成流：期望名输入（预填预设名）→ 提交导入 → 成功 toast + 返回列表', async () => {
    const user = userEvent.setup()
    renderWithProviders(<PersonaPresetGallery />, {
      route: '/personas/presets',
      path: '/personas/presets',
    })
    await screen.findByTestId('preset-card-tsundere')
    await user.click(inCard('tsundere').getByRole('button', { name: '生成' }))
    const nameInput = await screen.findByLabelText('期望名')
    expect(nameInput).toHaveValue('傲娇')
    await user.clear(nameInput)
    await user.type(nameInput, '小桃')
    await user.click(screen.getByRole('button', { name: '生成' }))
    expect(await screen.findByText(/已按预设「傲娇」生成人格/)).toBeInTheDocument()
    expect(await screen.findByTestId(NAVIGATED_AWAY_TESTID)).toBeInTheDocument()
  })

  it('生成对话框取消 / 右上角关闭 → 返回画廊（不发请求）', async () => {
    const user = userEvent.setup()
    renderWithProviders(<PersonaPresetGallery />, {
      route: '/personas/presets',
      path: '/personas/presets',
    })
    await screen.findByTestId('preset-card-tsundere')
    // 取消按钮路径。
    await user.click(inCard('tsundere').getByRole('button', { name: '生成' }))
    expect(await screen.findByRole('heading', { name: '生成人格' })).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: '取消' }))
    expect(screen.queryByRole('heading', { name: '生成人格' })).not.toBeInTheDocument()
    // 右上角 X 关闭路径。
    await user.click(inCard('tsundere').getByRole('button', { name: '生成' }))
    expect(await screen.findByRole('heading', { name: '生成人格' })).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: '关闭对话框' }))
    expect(screen.queryByRole('heading', { name: '生成人格' })).not.toBeInTheDocument()
    expect(screen.getByTestId('preset-card-tsundere')).toBeInTheDocument()
  })

  it('名称冲突三选一：skip 分支 → 关闭重问 + 已跳过 toast', async () => {
    const user = userEvent.setup()
    renderWithProviders(<PersonaPresetGallery />, {
      route: '/personas/presets',
      path: '/personas/presets',
    })
    await screen.findByTestId('preset-card-tsundere')
    await user.click(inCard('tsundere').getByRole('button', { name: '生成' }))
    const nameInput = await screen.findByLabelText('期望名')
    await user.clear(nameInput)
    await user.type(nameInput, 'companion-1')
    await user.click(screen.getByRole('button', { name: '生成' }))
    // 默认 handler：desired_name=companion-1 + skip → name_conflict。
    expect(await screen.findByRole('heading', { name: '名称冲突' })).toBeInTheDocument()
    expect(screen.getByText(/「companion-1」已被占用/)).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: '跳过（保留现状）' }))
    expect(await screen.findByText('已跳过（保留现状）')).toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: '名称冲突' })).not.toBeInTheDocument()
  })

  it('名称冲突三选一：改名分支 → 新期望名以 rename 策略重试成功', async () => {
    const user = userEvent.setup()
    const holder: { retryBody?: Record<string, unknown> } = {}
    server.use(
      http.post(`${BACKEND_ORIGIN}/personas/import`, async ({ request }) => {
        const body = (await request.json()) as Record<string, unknown>
        if (body.conflict_strategy === 'rename') {
          holder.retryBody = body
        }
        if (body.conflict_strategy === 'skip' && body.desired_name === 'companion-1') {
          return HttpResponse.json(
            okResult(
              { imported: false, persona_id: 1, reason: 'name_conflict', warnings: [] },
              '注册名已存在，按跳过策略未导入',
            ),
          )
        }
        return HttpResponse.json(
          okResult({ imported: true, persona_id: 11, reason: null, warnings: [] }, '人格已导入'),
        )
      }),
    )
    renderWithProviders(<PersonaPresetGallery />, {
      route: '/personas/presets',
      path: '/personas/presets',
    })
    await screen.findByTestId('preset-card-tsundere')
    await user.click(inCard('tsundere').getByRole('button', { name: '生成' }))
    const nameInput = await screen.findByLabelText('期望名')
    await user.clear(nameInput)
    await user.type(nameInput, 'companion-1')
    await user.click(screen.getByRole('button', { name: '生成' }))
    await screen.findByRole('heading', { name: '名称冲突' })
    const renameInput = screen.getByLabelText('改名导入')
    await user.type(renameInput, '傲娇二号')
    await user.click(screen.getByRole('button', { name: '以新名导入' }))
    expect(await screen.findByText(/已按预设「傲娇」生成人格/)).toBeInTheDocument()
    expect(holder.retryBody).toEqual({
      preset_id: 'tsundere',
      card: null,
      conflict_strategy: 'rename',
      desired_name: '傲娇二号',
    })
  })

  it('名称冲突三选一：覆盖分支 → overwrite 策略重试成功', async () => {
    const user = userEvent.setup()
    const holder: { overwriteBody?: Record<string, unknown> } = {}
    server.use(
      http.post(`${BACKEND_ORIGIN}/personas/import`, async ({ request }) => {
        const body = (await request.json()) as Record<string, unknown>
        if (body.conflict_strategy === 'overwrite') {
          holder.overwriteBody = body
          return HttpResponse.json(
            okResult({ imported: true, persona_id: 1, reason: null, warnings: [] }, '人格已导入'),
          )
        }
        return HttpResponse.json(
          okResult(
            { imported: false, persona_id: 1, reason: 'name_conflict', warnings: [] },
            '注册名已存在，按跳过策略未导入',
          ),
        )
      }),
    )
    renderWithProviders(<PersonaPresetGallery />, {
      route: '/personas/presets',
      path: '/personas/presets',
    })
    await screen.findByTestId('preset-card-tsundere')
    await user.click(inCard('tsundere').getByRole('button', { name: '生成' }))
    const nameInput = await screen.findByLabelText('期望名')
    await user.clear(nameInput)
    await user.type(nameInput, 'companion-1')
    await user.click(screen.getByRole('button', { name: '生成' }))
    await screen.findByRole('heading', { name: '名称冲突' })
    await user.click(screen.getByRole('button', { name: '覆盖既有人格' }))
    expect(await screen.findByText(/已按预设「傲娇」生成人格/)).toBeInTheDocument()
    expect(holder.overwriteBody).toMatchObject({
      preset_id: 'tsundere',
      conflict_strategy: 'overwrite',
      desired_name: 'companion-1',
    })
  })

  it('改名输入为空时校验报错（不发起请求）', async () => {
    const user = userEvent.setup()
    renderWithProviders(<PersonaPresetGallery />, {
      route: '/personas/presets',
      path: '/personas/presets',
    })
    await screen.findByTestId('preset-card-tsundere')
    await user.click(inCard('tsundere').getByRole('button', { name: '生成' }))
    const nameInput = await screen.findByLabelText('期望名')
    await user.clear(nameInput)
    await user.type(nameInput, 'companion-1')
    await user.click(screen.getByRole('button', { name: '生成' }))
    await screen.findByRole('heading', { name: '名称冲突' })
    const renameInput = screen.getByLabelText('改名导入')
    await user.click(screen.getByRole('button', { name: '以新名导入' }))
    expect(await screen.findByText('新名字必填，且长度为 1-128 个字符')).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: '名称冲突' })).toBeInTheDocument()
    expect(renameInput).toBeInTheDocument()
  })

  it('同卡幂等：identical_content → info toast（不进入三选一）', async () => {
    const user = userEvent.setup()
    server.use(
      http.post(`${BACKEND_ORIGIN}/personas/import`, () =>
        HttpResponse.json(
          okResult(
            { imported: false, persona_id: 1, reason: 'identical_content', warnings: [] },
            '检测到同内容人格已存在（幂等跳过）',
          ),
        ),
      ),
    )
    renderWithProviders(<PersonaPresetGallery />, {
      route: '/personas/presets',
      path: '/personas/presets',
    })
    await screen.findByTestId('preset-card-tsundere')
    await user.click(inCard('tsundere').getByRole('button', { name: '生成' }))
    await user.click(await screen.findByRole('button', { name: '生成' }))
    expect(await screen.findByText('检测到同卡已存在（内容相同，幂等跳过）')).toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: '名称冲突' })).not.toBeInTheDocument()
  })

  it('导入失败（未知预设 404）→ 错误 toast 呈现后端 message', async () => {
    const user = userEvent.setup()
    server.use(
      http.post(`${BACKEND_ORIGIN}/personas/import`, () =>
        HttpResponse.json(errorResult('官方预设不存在', 'NOT_FOUND'), { status: 404 }),
      ),
    )
    renderWithProviders(<PersonaPresetGallery />, {
      route: '/personas/presets',
      path: '/personas/presets',
    })
    await screen.findByTestId('preset-card-tsundere')
    await user.click(inCard('tsundere').getByRole('button', { name: '生成' }))
    await user.click(await screen.findByRole('button', { name: '生成' }))
    expect(await screen.findByText('官方预设不存在')).toBeInTheDocument()
  })

  it('预设库加载失败 → 错误呈现 + 重试恢复', async () => {
    server.use(
      http.get(`${BACKEND_ORIGIN}/personas/presets`, () =>
        HttpResponse.json(errorResult('官方预设库数据非法（装载失败）', 'INTERNAL_ERROR'), {
          status: 500,
        }),
      ),
    )
    renderWithProviders(<PersonaPresetGallery />, {
      route: '/personas/presets',
      path: '/personas/presets',
    })
    expect(await screen.findByText(/预设库加载失败：官方预设库数据非法/)).toBeInTheDocument()
    server.use(
      http.get(`${BACKEND_ORIGIN}/personas/presets`, () =>
        HttpResponse.json(okResult(mockTraitLibrary())),
      ),
    )
    fireEvent.click(screen.getByRole('button', { name: '重试' }))
    expect(await screen.findByTestId('preset-card-tsundere')).toBeInTheDocument()
  })

  it('空预设库 → 空态呈现', async () => {
    server.use(
      http.get(`${BACKEND_ORIGIN}/personas/presets`, () =>
        HttpResponse.json(okResult({ presets: [], components: [] })),
      ),
    )
    renderWithProviders(<PersonaPresetGallery />, {
      route: '/personas/presets',
      path: '/personas/presets',
    })
    expect(await screen.findByText('暂无官方预设')).toBeInTheDocument()
  })

  it('未知组件引用的芯片不渲染（注册表缺位容错）', async () => {
    const library = mockTraitLibrary()
    const base = library.presets[0]
    if (base === undefined) {
      throw new Error('mock 特质库缺预设')
    }
    const preset: TraitPreset = {
      ...base,
      components: ['duplicitous-mouth', 'ghost-component'],
    }
    server.use(
      http.get(`${BACKEND_ORIGIN}/personas/presets`, () =>
        HttpResponse.json(okResult({ ...library, presets: [preset] })),
      ),
    )
    renderWithProviders(<PersonaPresetGallery />, {
      route: '/personas/presets',
      path: '/personas/presets',
    })
    expect(await screen.findByTestId('preset-chip-tsundere-duplicitous-mouth')).toBeInTheDocument()
    expect(
      screen.queryByTestId('preset-chip-tsundere-ghost-component'),
    ).not.toBeInTheDocument()
  })
})

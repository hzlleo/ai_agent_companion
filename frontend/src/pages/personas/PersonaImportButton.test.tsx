import { describe, expect, it } from 'vitest'
import userEvent from '@testing-library/user-event'
import { fireEvent, screen } from '@testing-library/react'
import { http, HttpResponse } from 'msw'
import { PersonaImportButton } from './PersonaImportButton'
import { BACKEND_ORIGIN, errorResult, isFileEntry, okResult, server } from '@/test/server'
import { renderWithProviders } from '@/test/utils'
import type { PersonaImportResult } from '@/types/persona'

/** 构造卡片 JSON 文件。 */
function cardFile(name: string, filename = 'card.json', content?: object): File {
  const payload = content ?? { name, personality: '温柔', first_mes: '嗨' }
  return new File([JSON.stringify(payload)], filename, { type: 'application/json' })
}

/** 触发隐藏 file input 的 change（jsdom 无真实文件对话框）。 */
function uploadFiles(files: File[]): void {
  const input = screen.getByLabelText('人格卡片 JSON 文件上传')
  fireEvent.change(input, { target: { files } })
}

/** 幂等 / 冲突 / 成功三种单发结果。 */
function singleResult(
  imported: boolean,
  reason: 'identical_content' | 'name_conflict' | null,
): PersonaImportResult {
  return { imported, persona_id: imported ? 12 : 1, reason, warnings: [] }
}

describe('PersonaImportButton', () => {
  it('上传入口按钮点击（隐藏 input 无真实对话框——覆盖点击路径）', async () => {
    const user = userEvent.setup()
    renderWithProviders(<PersonaImportButton />)
    await user.click(screen.getByRole('button', { name: '导入人格' }))
    expect(screen.getByLabelText('人格卡片 JSON 文件上传')).toBeInTheDocument()
  })

  it('空选择（无文件）→ 静默早退（不发请求不弹窗）', async () => {
    renderWithProviders(<PersonaImportButton />)
    fireEvent.change(screen.getByLabelText('人格卡片 JSON 文件上传'), { target: {} })
    // 无请求发出（msw 未捕获也无 toast / 对话框）。
    expect(await screen.findByRole('button', { name: '导入人格' })).toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: '名称冲突' })).not.toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: '批量导入结果' })).not.toBeInTheDocument()
  })

  it('单文件导入成功 → toast 文件名', async () => {
    renderWithProviders(<PersonaImportButton />)
    uploadFiles([cardFile('新人格', 'new-persona.json')])
    expect(await screen.findByText(/人格「new-persona.json」已导入/)).toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: '名称冲突' })).not.toBeInTheDocument()
  })

  it('单文件同卡幂等 → info toast（不进入三选一）', async () => {
    server.use(
      http.post(`${BACKEND_ORIGIN}/personas/import`, () =>
        HttpResponse.json(
          okResult(singleResult(false, 'identical_content'), '检测到同内容人格已存在（幂等跳过）'),
        ),
      ),
    )
    renderWithProviders(<PersonaImportButton />)
    uploadFiles([cardFile('小助手', 'same.json')])
    expect(await screen.findByText('检测到同卡已存在（内容相同，幂等跳过）')).toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: '名称冲突' })).not.toBeInTheDocument()
  })

  it('单文件名称冲突 → 三选一：改名分支（rename + 新期望名重试成功）', async () => {
    const user = userEvent.setup()
    const retry: { strategy: string | null; desired: string | null; filename: string | null } = {
      strategy: null,
      desired: null,
      filename: null,
    }
    server.use(
      http.post(`${BACKEND_ORIGIN}/personas/import`, async ({ request }) => {
        const form = await request.formData()
        const file = form.get('file')
        const strategy = form.get('conflict_strategy')
        const desired = form.get('desired_name')
        const strategyText = typeof strategy === 'string' ? strategy : null
        const desiredText = typeof desired === 'string' ? desired : null
        if (strategyText === 'rename') {
          retry.strategy = strategyText
          retry.desired = desiredText
          retry.filename = isFileEntry(file) ? file.name : null
          return HttpResponse.json(
            okResult(singleResult(true, null), '人格已导入'),
          )
        }
        return HttpResponse.json(
          okResult(singleResult(false, 'name_conflict'), '注册名已存在，按跳过策略未导入'),
        )
      }),
    )
    renderWithProviders(<PersonaImportButton />)
    uploadFiles([cardFile('companion-1', 'conflict.json')])
    expect(await screen.findByRole('heading', { name: '名称冲突' })).toBeInTheDocument()
    expect(screen.getByText(/「conflict」已被占用/)).toBeInTheDocument()
    await user.type(screen.getByLabelText('改名导入'), '新注册名')
    await user.click(screen.getByRole('button', { name: '以新名导入' }))
    expect(await screen.findByText(/人格「conflict.json」已导入/)).toBeInTheDocument()
    expect(retry).toEqual({ strategy: 'rename', desired: '新注册名', filename: 'conflict.json' })
  })

  it('单文件名称冲突 → 三选一：覆盖分支（overwrite 重试成功）', async () => {
    const user = userEvent.setup()
    let overwriteSeen = false
    server.use(
      http.post(`${BACKEND_ORIGIN}/personas/import`, async ({ request }) => {
        const form = await request.formData()
        const strategy = form.get('conflict_strategy')
        if (strategy === 'overwrite') {
          overwriteSeen = true
          return HttpResponse.json(okResult(singleResult(true, null), '人格已导入'))
        }
        return HttpResponse.json(
          okResult(singleResult(false, 'name_conflict'), '注册名已存在，按跳过策略未导入'),
        )
      }),
    )
    renderWithProviders(<PersonaImportButton />)
    uploadFiles([cardFile('companion-1', 'conflict.json')])
    await screen.findByRole('heading', { name: '名称冲突' })
    await user.click(screen.getByRole('button', { name: '覆盖既有人格' }))
    expect(await screen.findByText(/人格「conflict.json」已导入/)).toBeInTheDocument()
    expect(overwriteSeen).toBe(true)
  })

  it('单文件名称冲突 → 三选一：跳过分支（toast + 对话框关闭）', async () => {
    const user = userEvent.setup()
    server.use(
      http.post(`${BACKEND_ORIGIN}/personas/import`, () =>
        HttpResponse.json(
          okResult(singleResult(false, 'name_conflict'), '注册名已存在，按跳过策略未导入'),
        ),
      ),
    )
    renderWithProviders(<PersonaImportButton />)
    uploadFiles([cardFile('companion-1', 'conflict.json')])
    await screen.findByRole('heading', { name: '名称冲突' })
    await user.click(screen.getByRole('button', { name: '跳过（保留现状）' }))
    expect(await screen.findByText('已跳过（保留现状）')).toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: '名称冲突' })).not.toBeInTheDocument()
  })

  it('冲突对话框右上角关闭（X）→ 同跳过语义（toast + 关闭）', async () => {
    const user = userEvent.setup()
    server.use(
      http.post(`${BACKEND_ORIGIN}/personas/import`, () =>
        HttpResponse.json(
          okResult(singleResult(false, 'name_conflict'), '注册名已存在，按跳过策略未导入'),
        ),
      ),
    )
    renderWithProviders(<PersonaImportButton />)
    uploadFiles([cardFile('companion-1', 'conflict.json')])
    await screen.findByRole('heading', { name: '名称冲突' })
    await user.click(screen.getByRole('button', { name: '关闭对话框' }))
    expect(await screen.findByText('已跳过（保留现状）')).toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: '名称冲突' })).not.toBeInTheDocument()
  })

  it('改名输入为空 → 行内校验报错（不发起请求）', async () => {
    const user = userEvent.setup()
    server.use(
      http.post(`${BACKEND_ORIGIN}/personas/import`, () =>
        HttpResponse.json(
          okResult(singleResult(false, 'name_conflict'), '注册名已存在，按跳过策略未导入'),
        ),
      ),
    )
    renderWithProviders(<PersonaImportButton />)
    uploadFiles([cardFile('companion-1', 'conflict.json')])
    await screen.findByRole('heading', { name: '名称冲突' })
    await user.click(screen.getByRole('button', { name: '以新名导入' }))
    expect(await screen.findByText('新名字必填，且长度为 1-128 个字符')).toBeInTheDocument()
  })

  it('多文件批量：部分成功结果对话框（逐文件 + 计数 + 失败项错误码）', async () => {
    const user = userEvent.setup()
    server.use(
      http.post(`${BACKEND_ORIGIN}/personas/import/batch`, async ({ request }) => {
        const form = await request.formData()
        const files = form.getAll('files').filter(isFileEntry)
        const items = await Promise.all(
          files.map(async (file) => {
            try {
              const cardName = (JSON.parse(await file.text()) as { name?: string }).name ?? ''
              if (cardName === 'companion-1') {
                return {
                  filename: file.name,
                  success: true,
                  imported: false,
                  persona_id: 1,
                  reason: 'name_conflict' as const,
                  code: null,
                  message: '注册名已存在，按跳过策略未导入',
                }
              }
              return {
                filename: file.name,
                success: true,
                imported: true,
                persona_id: 20,
                reason: null,
                code: null,
                message: '已导入',
              }
            } catch {
              return {
                filename: file.name,
                success: false,
                imported: false,
                persona_id: null,
                reason: null,
                code: 'VALIDATION_ERROR',
                message: '卡片文件解析失败（须为 chara card v2 JSON）: invalid',
              }
            }
          }),
        )
        const succeeded = items.filter((item) => item.success).length
        const importedCount = items.filter((item) => item.imported).length
        return HttpResponse.json(
          okResult(
            {
              total: items.length,
              succeeded,
              failed: items.length - succeeded,
              imported_count: importedCount,
              items,
            },
            `批量导入完成（成功 ${succeeded} / 失败 ${items.length - succeeded} / 新导入 ${importedCount}）`,
          ),
        )
      }),
    )
    renderWithProviders(<PersonaImportButton />)
    uploadFiles([
      cardFile('新人格', 'a.json'),
      cardFile('companion-1', 'b.json'),
      cardFile('解析失败占位', 'c.json', {}),
      new File(['not-json{'], 'broken.json', { type: 'application/json' }),
    ])
    expect(await screen.findByRole('heading', { name: '批量导入结果' })).toBeInTheDocument()
    expect(screen.getByText(/提交总数：/)).toBeInTheDocument()
    expect(screen.getByText(/新导入：/)).toBeInTheDocument()
    expect(screen.getByText('a.json')).toBeInTheDocument()
    expect(screen.getByText('b.json')).toBeInTheDocument()
    expect(screen.getByText('broken.json')).toBeInTheDocument()
    expect(screen.getAllByText('已导入').length).toBeGreaterThan(0)
    expect(screen.getAllByText('已跳过').length).toBeGreaterThan(0)
    expect(screen.getAllByText('失败').length).toBeGreaterThan(0)
    expect(screen.getByText(/卡片文件解析失败/)).toBeInTheDocument()
    expect(screen.getByText(/VALIDATION_ERROR/)).toBeInTheDocument()
    expect(await screen.findByText(/批量导入完成（成功 3 \/ 失败 1 \/ 新导入 2）/)).toBeInTheDocument()
    // 关闭按钮（知道了 + 右上角 X）逐个消费后对话框消失。
    await user.click(screen.getByRole('button', { name: '知道了' }))
    expect(screen.queryByRole('heading', { name: '批量导入结果' })).not.toBeInTheDocument()
  })

  it('单文件导入请求失败 → 错误 toast 呈现后端 message', async () => {
    server.use(
      http.post(`${BACKEND_ORIGIN}/personas/import`, () =>
        HttpResponse.json(
          errorResult('导入卡片文件解析失败（须为 chara card v2 JSON 文档）', 'VALIDATION_ERROR'),
          { status: 422 },
        ),
      ),
    )
    renderWithProviders(<PersonaImportButton />)
    uploadFiles([new File(['not-json{'], 'broken.json', { type: 'application/json' })])
    expect(
      await screen.findByText('导入卡片文件解析失败（须为 chara card v2 JSON 文档）'),
    ).toBeInTheDocument()
  })

  it('批量导入请求失败 → 错误 toast 呈现后端 message', async () => {
    server.use(
      http.post(`${BACKEND_ORIGIN}/personas/import/batch`, () =>
        HttpResponse.json(
          errorResult('批量导入上传缺少 files 字段（卡片 JSON 文件，可多个）', 'BAD_REQUEST'),
          { status: 400 },
        ),
      ),
    )
    renderWithProviders(<PersonaImportButton />)
    uploadFiles([cardFile('甲', 'a.json'), cardFile('乙', 'b.json')])
    expect(
      await screen.findByText('批量导入上传缺少 files 字段（卡片 JSON 文件，可多个）'),
    ).toBeInTheDocument()
  })
})

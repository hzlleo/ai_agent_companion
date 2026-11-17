import { beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { AdminTokenScreen } from '@/components/AdminTokenScreen'

/** token 持久化键（与 auth 模块 wire 契约一致）。 */
const STORAGE_KEY = 'aac_admin_token'

/**
 * 替换 window.location 为仅带 reload 桩的最小对象（jsdom 的 reload
 * 不可 spyOn——non-configurable；本组件只消费 reload，最小替换最可靠）。
 */
function stubLocationReload(): ReturnType<typeof vi.fn> {
  const reload = vi.fn()
  Object.defineProperty(window, 'location', {
    configurable: true,
    value: { reload },
  })
  return reload
}

describe('AdminTokenScreen 令牌门', () => {
  beforeEach(() => {
    localStorage.clear()
  })

  it('空 / 纯空白输入：提交按钮禁用（不落库不重载）', async () => {
    const reloadSpy = stubLocationReload()
    const user = userEvent.setup()
    render(<AdminTokenScreen />)
    await user.type(screen.getByLabelText('owner token'), '   ')
    const submit = screen.getByRole('button', { name: '保存并进入' })
    expect(submit).toBeDisabled()
    expect(localStorage.getItem(STORAGE_KEY)).toBeNull()
    expect(reloadSpy).not.toHaveBeenCalled()
  })

  it('空值 submit 事件直发：守卫早退（不落库不重载——禁用按钮之外的防线）', () => {
    const reloadSpy = stubLocationReload()
    render(<AdminTokenScreen />)
    fireEvent.submit(screen.getByRole('form', { name: '管理访问令牌' }))
    expect(localStorage.getItem(STORAGE_KEY)).toBeNull()
    expect(reloadSpy).not.toHaveBeenCalled()
  })

  it('保存：token trim 后落 localStorage + 整页 reload（锁定决策：拦截器逐请求读取，重载即生效）', async () => {
    const reloadSpy = stubLocationReload()
    const user = userEvent.setup()
    render(<AdminTokenScreen />)
    await user.type(screen.getByLabelText('owner token'), '  secret-token  ')
    await user.click(screen.getByRole('button', { name: '保存并进入' }))
    expect(localStorage.getItem(STORAGE_KEY)).toBe('secret-token')
    expect(reloadSpy).toHaveBeenCalledTimes(1)
  })

  it('呈现中文说明（运维 key 指引 + 输入框 + 保存按钮）', () => {
    render(<AdminTokenScreen />)
    expect(screen.getByText('管理访问令牌')).toBeInTheDocument()
    expect(screen.getByText(/ADMIN_AUTH_TOKEN/)).toBeInTheDocument()
    expect(screen.getByPlaceholderText('粘贴 owner token')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '保存并进入' })).toBeInTheDocument()
  })
})

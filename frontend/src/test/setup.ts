import '@testing-library/jest-dom/vitest'
import { afterEach, afterAll, beforeAll, beforeEach, vi } from 'vitest'
import { cleanup } from '@testing-library/react'
import { server } from './server'
import { ActWarningGate } from './actWarningGate'

/** React 未包裹 act 的固定告警文案。 */
const ACT_WARNING_PATTERN = /not wrapped in act\(\.\.\.\)/

/** 包装前保存的原始输出通道（非 act 日志原样透传）。 */
const originalConsoleError = console.error.bind(console)

/** act 告警门禁（归属 / 排空 / 兜底语义见 ActWarningGate）。 */
const actGate = new ActWarningGate()

/** 排空一个宏任务：act 告警常在微任务之后的下一个宏任务落盘。 */
async function drainMacrotask(): Promise<void> {
  if (vi.isFakeTimers()) {
    vi.advanceTimersByTime(0)
    await Promise.resolve()
    return
  }
  await new Promise<void>((resolve) => {
    setTimeout(resolve, 0)
  })
}

// act 门禁：console.error 中的 act 告警在用例结束时转为失败（onTestFinished
// 在 afterEach（含 RTL cleanup）之后执行，见 vitest 4 生命周期；先排空宏任务
// 再判定，afterAll 兜底无归属残留）。
beforeAll(() => {
  console.error = (...args: unknown[]) => {
    const text = args.map((arg) => (typeof arg === 'string' ? arg : String(arg))).join(' ')
    if (ACT_WARNING_PATTERN.test(text)) {
      actGate.record(text)
      return
    }
    originalConsoleError(...args)
  }
  server.listen({ onUnhandledRequest: 'error' })
})

beforeEach((context) => {
  actGate.beginAttempt()
  const testName = context.task.name
  context.onTestFinished(async () => {
    await drainMacrotask()
    actGate.finishAttempt(testName)
  })
})

afterEach(() => {
  cleanup()
  // 每个用例后清除 runtime handler 覆盖（server.use 的临时 mock）。
  server.resetHandlers()
})

afterAll(() => {
  console.error = originalConsoleError
  server.close()
  actGate.finishRun()
})

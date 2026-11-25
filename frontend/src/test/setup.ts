import '@testing-library/jest-dom/vitest'
import { afterEach, afterAll, beforeAll, beforeEach } from 'vitest'
import { cleanup } from '@testing-library/react'
import { server } from './server'

/** React 未包裹 act 的固定告警文案。 */
const ACT_WARNING_PATTERN = /not wrapped in act\(\.\.\.\)/

/** 包装前保存的原始输出通道（非 act 日志原样透传）。 */
const originalConsoleError = console.error.bind(console)

/** 当前用例捕获的 act 告警样本。 */
let actWarnings: string[] = []

// act 门禁：console.error 中的 act 告警在用例结束时转为失败（onTestFinished
// 早于 afterEach cleanup，避免逆序跳过 RTL 卸载造成连锁失败）。
beforeAll(() => {
  console.error = (...args: unknown[]) => {
    const text = args.map((arg) => (typeof arg === 'string' ? arg : String(arg))).join(' ')
    if (ACT_WARNING_PATTERN.test(text)) {
      actWarnings.push(text.split('\n')[0] ?? text)
      return
    }
    originalConsoleError(...args)
  }
  server.listen({ onUnhandledRequest: 'error' })
})

beforeEach((context) => {
  actWarnings = []
  const testName = context.task.name
  context.onTestFinished(async () => {
    await Promise.resolve()
    if (actWarnings.length === 0) {
      return
    }
    throw new Error(
      `React act(...) 告警 ${actWarnings.length} 条（用例：${testName}）：${actWarnings.join(' ｜ ')}`,
    )
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
})

import '@testing-library/jest-dom/vitest'
import { afterEach, afterAll, beforeAll } from 'vitest'
import { cleanup } from '@testing-library/react'
import { server } from './server'

// msw 服务器生命周期：全部测试文件共享同一 setupServer 实例，
// 请求拦截在 beforeAll 启用、afterAll 还原（onUnhandledRequest 报错，
// 保证测试永不触达真实网络）。
beforeAll(() => {
  server.listen({ onUnhandledRequest: 'error' })
})

afterEach(() => {
  cleanup()
  // 每个用例后清除 runtime handler 覆盖（server.use 的临时 mock）。
  server.resetHandlers()
})

afterAll(() => {
  server.close()
})

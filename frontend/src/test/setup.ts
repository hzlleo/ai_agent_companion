import '@testing-library/jest-dom/vitest'
import { afterEach, afterAll, beforeAll } from 'vitest'
import { cleanup, configure } from '@testing-library/react'
import { server } from './server'

// 异步查询默认等待 1s 对覆盖率插桩下的懒加载路由过紧（并行 worker + v8
// 插桩使首次 chunk 求值显著变慢，曾出现「页面还在加载态」的假失败）。
// 这里只放宽等待上限，不放宽任何断言（查询仍须真实命中）。
configure({ asyncUtilTimeout: 5000 })

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

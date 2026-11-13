/**
 * 共享测试工具：QueryClient 工厂 + 页面级渲染快捷方式。
 *
 * 约定（本工程）：所有工厂每次调用创建全新 QueryClient（查询关闭
 * 重试、变更不重试），避免用例间缓存串扰；页面渲染统一经
 * ``renderWithProviders``（QueryClient + MemoryRouter + ToastProvider）。
 */

import { createElement, type ReactElement, type ReactNode } from 'react'
import { render, renderHook, type RenderResult } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { RouterProvider, createMemoryRouter } from 'react-router-dom'
import { vi } from 'vitest'
import { ToastProvider } from '@/components/ui/Toast'

/** 页面 / 组件测试用 QueryClient：查询与变更均不重试。 */
export function createTestQueryClient(options: { gcTime?: number } = {}): QueryClient {
  const defaultOptions =
    options.gcTime === undefined
      ? { queries: { retry: false }, mutations: { retry: false } }
      : { queries: { retry: false, gcTime: options.gcTime }, mutations: { retry: false } }
  return new QueryClient({ defaultOptions })
}

/** renderHook 用 wrapper（仅 QueryClientProvider）。 */
export function createQueryClientWrapper(client: QueryClient = createTestQueryClient()) {
  return function Wrapper({ children }: { children: ReactNode }) {
    return createElement(QueryClientProvider, { client }, children)
  }
}

/** QueryClient wrapper + invalidateQueries spy（断言缓存失效）。 */
export function createQueryClientWrapperWithSpy() {
  const queryClient = createTestQueryClient()
  const invalidateSpy = vi.spyOn(queryClient, 'invalidateQueries')
  return { wrapper: createQueryClientWrapper(queryClient), invalidateSpy }
}

export interface RenderWithProvidersOptions {
  /** MemoryRouter initialEntries[0]，默认 '/'。 */
  route?: string
  /** 路由匹配模式（useParams 消费路径参数时必传，如 '/personas/:personaId/edit'）。 */
  path?: string
}

/**
 * 页面级渲染快捷方式：QueryClientProvider > ToastProvider > memory
 * data router（ui 作为路由 element 挂载——useParams 经 path 模式解析
 * 路径参数）。层级与生产一致（Toast 在页面可消费范围内）。
 */
export function renderWithProviders(
  ui: ReactElement,
  options: RenderWithProvidersOptions = {},
): RenderResult {
  const { route = '/', path = '*' } = options
  const queryClient = createTestQueryClient()
  const router = createMemoryRouter([{ path, element: ui }], { initialEntries: [route] })
  return render(
    createElement(
      QueryClientProvider,
      { client: queryClient },
      createElement(ToastProvider, null, createElement(RouterProvider, { router })),
    ),
  )
}

/** renderHook + wrapper 的组合快捷方式。 */
export function renderHookWithQueryClient<TResult>(hook: () => TResult) {
  return renderHook(hook, { wrapper: createQueryClientWrapper() })
}

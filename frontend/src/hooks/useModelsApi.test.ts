import { describe, expect, it } from 'vitest'
import { http, HttpResponse } from 'msw'
import { act, renderHook, waitFor } from '@testing-library/react'
import {
  MODELS_QUERY_KEY,
  useCreateModelEntry,
  useDeleteModelEntry,
  useRegistryState,
  useRoleBindings,
  useUpdateModelEntry,
} from '@/hooks/useModelsApi'
import { BACKEND_ORIGIN, errorResult, mockRegistryState, mockRoleBindings, okResult, server } from '@/test/server'
import { createQueryClientWrapperWithSpy, renderHookWithQueryClient } from '@/test/utils'

describe('useRegistryState / useRoleBindings', () => {
  it('注册表状态解包（脱敏条目）', async () => {
    const { result } = renderHookWithQueryClient(() => useRegistryState())
    await waitFor(() => expect(result.current.isSuccess).toBe(true))
    expect(result.current.data?.entries).toHaveLength(2)
    expect(result.current.data?.entries[0]?.name).toBe('main')
  })

  it('角色绑定面解包（bound / unbound）', async () => {
    const { result } = renderHookWithQueryClient(() => useRoleBindings())
    await waitFor(() => expect(result.current.isSuccess).toBe(true))
    expect(result.current.data?.find((binding) => binding.role === 'embed')?.bound).toBe(true)
    expect(result.current.data?.find((binding) => binding.role === 'duty')?.bound).toBe(false)
  })
})

describe('models mutations', () => {
  it('useCreateModelEntry：成功后失效 models 缓存族', async () => {
    const { wrapper, invalidateSpy } = createQueryClientWrapperWithSpy()
    const { result } = renderHook(() => useCreateModelEntry(), { wrapper })
    act(() => {
      result.current.mutate({
        name: 'aux',
        provider: 'openai_compatible',
        base_url: 'http://127.0.0.1:9100/v1',
        api_key_env: '',
        model: 'm',
        roles: [],
        concurrency: null,
        multimodal: false,
      })
    })
    await waitFor(() => expect(result.current.isSuccess).toBe(true))
    expect(result.current.data?.entries.map((entry) => entry.name)).toContain('aux')
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: MODELS_QUERY_KEY })
  })

  it('useUpdateModelEntry：name 路径 + 请求体（roles=[] 清空）', async () => {
    let capturedBody: unknown = null
    let capturedPath = ''
    server.use(
      http.put(`${BACKEND_ORIGIN}/models/entries/:name`, async ({ request, params }) => {
        capturedBody = await request.json()
        capturedPath = String(params.name)
        return HttpResponse.json(okResult(mockRegistryState(), '模型条目已更新'))
      }),
    )
    const { result } = renderHookWithQueryClient(() => useUpdateModelEntry())
    act(() => {
      result.current.mutate({ name: 'main', payload: { roles: [] } })
    })
    await waitFor(() => expect(result.current.isSuccess).toBe(true))
    expect(capturedPath).toBe('main')
    expect(capturedBody).toEqual({ roles: [] })
  })

  it('useDeleteModelEntry：绑定守卫 409 → onError 可达', async () => {
    server.use(
      http.delete(`${BACKEND_ORIGIN}/models/entries/:name`, () =>
        HttpResponse.json(
          errorResult('模型条目仍绑定角色，禁止删除（先解绑再删除）: main', 'CONFLICT', {
            name: 'main',
            bound_roles: ['chat'],
          }),
          { status: 409 },
        ),
      ),
    )
    const { result } = renderHookWithQueryClient(() => useDeleteModelEntry())
    act(() => {
      result.current.mutate('main')
    })
    await waitFor(() => expect(result.current.isError).toBe(true))
  })

  it('useDeleteModelEntry：成功返回写后状态', async () => {
    server.use(
      http.delete(`${BACKEND_ORIGIN}/models/entries/:name`, () =>
        HttpResponse.json(okResult(mockRegistryState(), '模型条目已删除')),
      ),
    )
    const { result } = renderHookWithQueryClient(() => useDeleteModelEntry())
    act(() => {
      result.current.mutate('embed-model')
    })
    await waitFor(() => expect(result.current.isSuccess).toBe(true))
    expect(mockRoleBindings).toBeDefined()
  })
})

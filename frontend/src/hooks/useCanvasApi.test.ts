import { describe, expect, it } from 'vitest'
import { act, renderHook, waitFor } from '@testing-library/react'
import { http, HttpResponse } from 'msw'
import {
  CANVAS_DEFAULT_QUERY,
  CANVAS_QUERY_KEY,
  useCanvasDocuments,
  useCreateCanvas,
  useDeleteCanvas,
  useExportCanvas,
  useUpdateCanvas,
} from '@/hooks/useCanvasApi'
import { BACKEND_ORIGIN, errorResult, server } from '@/test/server'
import {
  createQueryClientWrapperWithSpy,
  renderHookWithQueryClient,
} from '@/test/utils'

describe('useCanvasDocuments', () => {
  it('加载列表（按人格与查询条件构造缓存键）', async () => {
    const { result } = renderHookWithQueryClient(() =>
      useCanvasDocuments(1, CANVAS_DEFAULT_QUERY),
    )
    await waitFor(() => expect(result.current.isSuccess).toBe(true))
    expect(result.current.data?.[0]?.title).toBe('今日心情卡')
  })

  it('加载失败暴露错误（列表端点错误面）', async () => {
    server.use(
      http.get(`${BACKEND_ORIGIN}/canvas`, () =>
        HttpResponse.json(errorResult('画布不存在', 'NOT_FOUND'), { status: 404 }),
      ),
    )
    const { result } = renderHookWithQueryClient(() =>
      useCanvasDocuments(undefined, CANVAS_DEFAULT_QUERY),
    )
    await waitFor(() => expect(result.current.isError).toBe(true))
  })
})

describe('canvas mutations', () => {
  it('useCreateCanvas：成功后失效画布缓存族', async () => {
    const { wrapper, invalidateSpy } = createQueryClientWrapperWithSpy()
    const { result } = renderHook(() => useCreateCanvas(1), { wrapper })
    act(() => {
      result.current.mutate({ title: '新卡', kind: 'html', content: '<div/>' })
    })
    await waitFor(() => expect(result.current.isSuccess).toBe(true))
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: CANVAS_QUERY_KEY })
    expect(result.current.data?.title).toBe('新卡')
  })

  it('useUpdateCanvas：成功后失效缓存族并回传修订号', async () => {
    const { wrapper, invalidateSpy } = createQueryClientWrapperWithSpy()
    const { result } = renderHook(() => useUpdateCanvas(undefined), { wrapper })
    act(() => {
      result.current.mutate({ canvasId: 3, payload: { content: '<div>二版</div>', title: null } })
    })
    await waitFor(() => expect(result.current.isSuccess).toBe(true))
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: CANVAS_QUERY_KEY })
    expect(result.current.data?.revision).toBe(2)
  })

  it('useDeleteCanvas：成功后失效缓存族并回传删除结果', async () => {
    const { wrapper, invalidateSpy } = createQueryClientWrapperWithSpy()
    const { result } = renderHook(() => useDeleteCanvas(undefined), { wrapper })
    act(() => {
      result.current.mutate(6)
    })
    await waitFor(() => expect(result.current.isSuccess).toBe(true))
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: CANVAS_QUERY_KEY })
    expect(result.current.data?.deleted_ids).toEqual([6])
  })

  it('useExportCanvas：命令式导出不写缓存', async () => {
    const { wrapper } = createQueryClientWrapperWithSpy()
    const { result } = renderHook(() => useExportCanvas(undefined), { wrapper })
    act(() => {
      result.current.mutate({ id: 2, kind: 'html' })
    })
    await waitFor(() => expect(result.current.isSuccess).toBe(true))
    expect(result.current.data?.filename).toBe('canvas-2-r1.html')
    expect(result.current.data?.blob).toBeInstanceOf(Blob)
  })
})

import { describe, expect, it } from 'vitest'
import { act, renderHook, waitFor } from '@testing-library/react'
import {
  STICKERS_DEFAULT_QUERY,
  STICKERS_QUERY_KEY,
  useAnnotateSticker,
  useDeleteSticker,
  useStickers,
  useUpdateSticker,
  useUploadStickers,
} from '@/hooks/useStickersApi'
import {
  createQueryClientWrapperWithSpy,
  renderHookWithQueryClient,
} from '@/test/utils'

describe('useStickers', () => {
  it('加载列表（按人格与查询条件构造缓存键）', async () => {
    const { result } = renderHookWithQueryClient(() =>
      useStickers(1, STICKERS_DEFAULT_QUERY),
    )
    await waitFor(() => expect(result.current.isSuccess).toBe(true))
    expect(result.current.data?.[0]?.source_note).toBe('自绘素材')
  })
})

describe('sticker mutations', () => {
  it('useUploadStickers：成功后失效表情包缓存族', async () => {
    const { wrapper, invalidateSpy } = createQueryClientWrapperWithSpy()
    const { result } = renderHook(() => useUploadStickers(1), { wrapper })
    act(() => {
      result.current.mutate({
        items: [
          {
            image_base64: 'aGVsbG8=',
            content_type: 'image/png',
            source_note: '自绘',
            tags: [],
            description: '',
          },
        ],
      })
    })
    await waitFor(() => expect(result.current.isSuccess).toBe(true))
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: STICKERS_QUERY_KEY })
    expect(result.current.data?.created).toHaveLength(1)
  })

  it('useUpdateSticker：成功后失效缓存族并回传记录', async () => {
    const { wrapper, invalidateSpy } = createQueryClientWrapperWithSpy()
    const { result } = renderHook(() => useUpdateSticker(undefined), { wrapper })
    act(() => {
      result.current.mutate({
        stickerId: 2,
        payload: { tags: ['新标签'], description: null, source_note: null },
      })
    })
    await waitFor(() => expect(result.current.isSuccess).toBe(true))
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: STICKERS_QUERY_KEY })
    expect(result.current.data?.tags).toEqual(['新标签'])
  })

  it('useDeleteSticker：成功后失效缓存族', async () => {
    const { wrapper, invalidateSpy } = createQueryClientWrapperWithSpy()
    const { result } = renderHook(() => useDeleteSticker(1), { wrapper })
    act(() => {
      result.current.mutate(3)
    })
    await waitFor(() => expect(result.current.isSuccess).toBe(true))
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: STICKERS_QUERY_KEY })
    expect(result.current.data?.deleted_ids).toEqual([3])
  })

  it('useAnnotateSticker：成功后失效缓存族并回传标注结果', async () => {
    const { wrapper, invalidateSpy } = createQueryClientWrapperWithSpy()
    const { result } = renderHook(() => useAnnotateSticker(1), { wrapper })
    act(() => {
      result.current.mutate(4)
    })
    await waitFor(() => expect(result.current.isSuccess).toBe(true))
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: STICKERS_QUERY_KEY })
    expect(result.current.data?.record.annotation_status).toBe('done')
  })
})

import { describe, expect, it } from 'vitest'
import { act, renderHook, waitFor } from '@testing-library/react'
import {
  CORE_MEMORY_QUERY_KEY,
  useCoreMemoryBlocks,
  useCoreMemoryRevisions,
  useUpdateCoreMemoryBlock,
} from '@/hooks/useCoreMemoryApi'
import { createQueryClientWrapperWithSpy, renderHookWithQueryClient } from '@/test/utils'

describe('useCoreMemoryBlocks', () => {
  it('personaId 生效时加载四块（固定块序）', async () => {
    const { result } = renderHookWithQueryClient(() => useCoreMemoryBlocks(1))
    await waitFor(() => expect(result.current.isSuccess).toBe(true))
    expect(result.current.data?.map((block) => block.block)).toEqual([
      'self_identity',
      'owner_profile',
      'relationship_story',
      'open_notes',
    ])
    expect(result.current.data?.[1]?.locked).toBe(true)
  })

  it('personaId=0 时不发起请求（enabled 关）', () => {
    const { result } = renderHookWithQueryClient(() => useCoreMemoryBlocks(0))
    expect(result.current.fetchStatus).toBe('idle')
    expect(result.current.data).toBeUndefined()
  })
})

describe('core-memory mutations', () => {
  it('useUpdateCoreMemoryBlock：成功后失效 core-memory 缓存族', async () => {
    const { wrapper, invalidateSpy } = createQueryClientWrapperWithSpy()
    const { result } = renderHook(() => useUpdateCoreMemoryBlock(), { wrapper })
    act(() => {
      result.current.mutate({
        personaId: 1,
        block: 'open_notes',
        payload: { content: '新的备忘', locked: false },
      })
    })
    await waitFor(() => expect(result.current.isSuccess).toBe(true))
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: CORE_MEMORY_QUERY_KEY })
    expect(result.current.data?.content).toBe('新的备忘')
    expect(result.current.data?.updated_by).toBe('owner')
  })
})

describe('useCoreMemoryRevisions', () => {
  it('enabled 关时零请求；开启后按最新在前加载修订', async () => {
    const { result } = renderHookWithQueryClient(() =>
      useCoreMemoryRevisions(1, 'self_identity', false),
    )
    expect(result.current.fetchStatus).toBe('idle')

    const enabled = renderHookWithQueryClient(() =>
      useCoreMemoryRevisions(1, 'self_identity', true),
    )
    await waitFor(() => expect(enabled.result.current.isSuccess).toBe(true))
    expect(enabled.result.current.data?.map((revision) => revision.version)).toEqual([3, 2])
  })
})

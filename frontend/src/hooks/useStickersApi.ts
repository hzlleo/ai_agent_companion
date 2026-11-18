/**
 * 表情包库 React Query hooks：列表 / 上传 / 修改 / 删除 / 标注的数据面。
 *
 * 约定（本工程）：
 * - 列表按「人格作用域 + 查询条件」缓存，上传 / 修改 / 删除 / 标注成功后
 *   失效表情包全族（状态机与标签变化需要重取）；
 * - 列表请求内联 base64（面板缩略图直接使用 data URL，不额外拉文件端点）；
 * - 归属人格由调用方显式传入（undefined = 后端解析当前激活人格）。
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  annotateSticker,
  deleteSticker,
  listStickers,
  updateSticker,
  uploadStickers,
} from '@/api/stickers'
import type {
  StickerAnnotationResult,
  StickerBatchDeleteResult,
  StickerBatchUploadRequest,
  StickerBatchUploadResult,
  StickerListParams,
  StickerRecord,
  StickerUpdateRequest,
} from '@/types/stickers'

/** 表情包缓存族前缀（全部失效入口）。 */
export const STICKERS_QUERY_KEY = ['stickers'] as const

/** 面板缺省查询（最新 100 条 / 内联缩略图）。 */
export const STICKERS_DEFAULT_QUERY: StickerListParams = {
  tag: null,
  annotation_status: null,
  limit: 100,
  include_data: true,
}

/** 失效表情包全族。 */
function useInvalidateStickers() {
  const queryClient = useQueryClient()
  return () => {
    void queryClient.invalidateQueries({ queryKey: STICKERS_QUERY_KEY })
  }
}

/** 表情包列表（按人格与查询条件缓存）。 */
export function useStickers(personaId: number | undefined, params: StickerListParams) {
  return useQuery({
    queryKey: [...STICKERS_QUERY_KEY, personaId ?? 'active', params],
    queryFn: () => listStickers(personaId, params),
  })
}

/** 批量上传（成功后失效列表）。 */
export function useUploadStickers(personaId: number | undefined) {
  const invalidate = useInvalidateStickers()
  return useMutation<StickerBatchUploadResult, Error, StickerBatchUploadRequest>({
    mutationFn: (payload) => uploadStickers(personaId, payload),
    onSuccess: invalidate,
  })
}

/** 修改标签 / 描述 / 来源备注（成功后失效列表）。 */
export function useUpdateSticker(personaId: number | undefined) {
  const invalidate = useInvalidateStickers()
  return useMutation<
    StickerRecord,
    Error,
    { stickerId: number; payload: StickerUpdateRequest }
  >({
    mutationFn: ({ stickerId, payload }) => updateSticker(personaId, stickerId, payload),
    onSuccess: invalidate,
  })
}

/** 删除单条表情包（成功后失效列表）。 */
export function useDeleteSticker(personaId: number | undefined) {
  const invalidate = useInvalidateStickers()
  return useMutation<StickerBatchDeleteResult, Error, number>({
    mutationFn: (stickerId) => deleteSticker(personaId, stickerId),
    onSuccess: invalidate,
  })
}

/** VLM 标注（失败也是成功响应——返回 ok=false 的可读结果）。 */
export function useAnnotateSticker(personaId: number | undefined) {
  const invalidate = useInvalidateStickers()
  return useMutation<StickerAnnotationResult, Error, number>({
    mutationFn: (stickerId) => annotateSticker(personaId, stickerId),
    onSuccess: invalidate,
  })
}

/**
 * 画布 React Query hooks：列表 / 创建 / 修订 / 删除 / 导出的数据面。
 *
 * 约定（本工程）：
 * - 列表按「人格作用域 + 查询条件」缓存，创建 / 修订 / 删除成功后失效画布
 *   全族（列表与详情需要重取）；
 * - 归属人格由调用方显式传入（undefined = 后端解析当前激活人格）；
 * - 导出是命令式操作（不写入缓存）——页面点导出时直接调用 mutation。
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { createCanvas, deleteCanvas, downloadCanvas, listCanvas, updateCanvas } from '@/api/canvas'
import type {
  CanvasCreateRequest,
  CanvasDeleteResult,
  CanvasExportFile,
  CanvasListParams,
  CanvasRecord,
  CanvasUpdateRequest,
} from '@/types/canvas'

/** 画布缓存族前缀（全部失效入口）。 */
export const CANVAS_QUERY_KEY = ['canvas'] as const

/** 面板缺省查询（最近 100 条 / 全类型）。 */
export const CANVAS_DEFAULT_QUERY: CanvasListParams = {
  kind: null,
  limit: 100,
}

/** 失效画布全族。 */
function useInvalidateCanvas() {
  const queryClient = useQueryClient()
  return () => {
    void queryClient.invalidateQueries({ queryKey: CANVAS_QUERY_KEY })
  }
}

/** 画布列表（按人格与查询条件缓存）。 */
export function useCanvasDocuments(personaId: number | undefined, params: CanvasListParams) {
  return useQuery({
    queryKey: [...CANVAS_QUERY_KEY, personaId ?? 'active', params],
    queryFn: () => listCanvas(personaId, params),
  })
}

/** 创建画布（成功后失效列表）。 */
export function useCreateCanvas(personaId: number | undefined) {
  const invalidate = useInvalidateCanvas()
  return useMutation<CanvasRecord, Error, CanvasCreateRequest>({
    mutationFn: (payload) => createCanvas(personaId, payload),
    onSuccess: invalidate,
  })
}

/** 修订画布（成功后失效列表）。 */
export function useUpdateCanvas(personaId: number | undefined) {
  const invalidate = useInvalidateCanvas()
  return useMutation<
    CanvasRecord,
    Error,
    { canvasId: number; payload: CanvasUpdateRequest }
  >({
    mutationFn: ({ canvasId, payload }) => updateCanvas(personaId, canvasId, payload),
    onSuccess: invalidate,
  })
}

/** 删除画布（成功后失效列表）。 */
export function useDeleteCanvas(personaId: number | undefined) {
  const invalidate = useInvalidateCanvas()
  return useMutation<CanvasDeleteResult, Error, number>({
    mutationFn: (canvasId) => deleteCanvas(personaId, canvasId),
    onSuccess: invalidate,
  })
}

/** 导出画布（命令式下载载荷——不写入查询缓存）。 */
export function useExportCanvas(personaId: number | undefined) {
  return useMutation<CanvasExportFile, Error, Pick<CanvasRecord, 'id' | 'kind'>>({
    mutationFn: (record) => downloadCanvas(personaId, record),
  })
}

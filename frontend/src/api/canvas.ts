/**
 * 画布管理端点层：列表 / 创建 / 修订 / 删除 / 导出的 typed 封装。
 *
 * 路由面（JSON 端点经 ResultDTO 解包，失败抛 ApiError）：
 *   - GET    /canvas                  列表（类型过滤）
 *   - POST   /canvas                  创建（标题 / 类型 / 自包含内容）
 *   - PATCH  /canvas/{id}             修订（revision 递增）
 *   - DELETE /canvas/{id}             删除（缺失主键幂等报告）
 *   - GET    /canvas/{id}/export      单文件导出（二进制响应）
 *
 * 导出端点的响应不是 JSON（原始产物字节 + Content-Disposition），因此不经
 * `request` 解包：字节直取后组装 Blob，文件名读响应头（后端 CORS 已显式暴露
 * Content-Disposition；读取失败时按主键回退默认文件名）。二进制模式下失败
 * 响应体不是 ResultDTO（无法读出后端 message），错误按 HTTP 状态兜底文案
 * 归一为 ApiError。
 */

import { api, request, toApiError } from './client'
import type {
  CanvasCreateRequest,
  CanvasDeleteResult,
  CanvasExportFile,
  CanvasListParams,
  CanvasRecord,
  CanvasUpdateRequest,
} from '@/types/canvas'

/** persona_id 查询参数形态（undefined 时不出现在查询串）。 */
function personaScope(personaId: number | undefined): { persona_id: number } | undefined {
  return personaId === undefined ? undefined : { persona_id: personaId }
}

/** GET /canvas —— 画布列表（最近修改在前）。 */
export async function listCanvas(
  personaId: number | undefined,
  params: CanvasListParams,
): Promise<CanvasRecord[]> {
  return request<CanvasRecord[]>({
    method: 'GET',
    url: '/canvas',
    params: {
      ...personaScope(personaId),
      kind: params.kind ?? undefined,
      limit: params.limit,
    },
  })
}

/** POST /canvas —— 创建画布（revision=1）。 */
export async function createCanvas(
  personaId: number | undefined,
  payload: CanvasCreateRequest,
): Promise<CanvasRecord> {
  return request<CanvasRecord>({
    method: 'POST',
    url: '/canvas',
    params: personaScope(personaId),
    data: payload,
  })
}

/** PATCH /canvas/{id} —— 修订画布（revision 递增）。 */
export async function updateCanvas(
  personaId: number | undefined,
  canvasId: number,
  payload: CanvasUpdateRequest,
): Promise<CanvasRecord> {
  return request<CanvasRecord>({
    method: 'PATCH',
    url: `/canvas/${canvasId}`,
    params: personaScope(personaId),
    data: payload,
  })
}

/** DELETE /canvas/{id} —— 删除画布（缺失主键幂等报告）。 */
export async function deleteCanvas(
  personaId: number | undefined,
  canvasId: number,
): Promise<CanvasDeleteResult> {
  return request<CanvasDeleteResult>({
    method: 'DELETE',
    url: `/canvas/${canvasId}`,
    params: personaScope(personaId),
  })
}

/** 文件名兜底（响应头缺失时按主键与扩展名推断）。 */
function fallbackFilename(canvasId: number, kind: string): string {
  const extension = kind === 'svg' ? 'svg' : kind === 'markdown' ? 'md' : 'html'
  return `canvas-${canvasId}.${extension}`
}

/** 从 Content-Disposition 头解析文件名（失败返回 null）。 */
function filenameFromDisposition(header: string | undefined): string | null {
  if (!header) {
    return null
  }
  const match = /filename="([^"]+)"/.exec(header)
  return match ? (match[1] ?? null) : null
}

/** GET /canvas/{id}/export —— 导出画布（二进制 + 文件名）。 */
export async function downloadCanvas(
  personaId: number | undefined,
  record: Pick<CanvasRecord, 'id' | 'kind'>,
): Promise<CanvasExportFile> {
  try {
    // arraybuffer 承载（而非 responseType=blob）：下载载荷在这里组装为
    // Blob，媒体类型的来源是响应头而不是浏览器推断。
    const response = await api.get<ArrayBuffer>(`/canvas/${record.id}/export`, {
      params: personaScope(personaId),
      responseType: 'arraybuffer',
    })
    const dispositionHeader = response.headers['content-disposition'] as string | undefined
    const filename =
      filenameFromDisposition(dispositionHeader) ??
      fallbackFilename(record.id, record.kind)
    const mediaType =
      (response.headers['content-type'] as string | undefined) ?? 'application/octet-stream'
    return { blob: new Blob([response.data], { type: mediaType }), filename }
  } catch (error) {
    throw toApiError(error)
  }
}

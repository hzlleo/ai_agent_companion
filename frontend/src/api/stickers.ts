/**
 * 表情包库端点层：上传 / 列表 / 修改 / 删除 / VLM 标注的 typed 封装。
 *
 * 路由面（全部经 ResultDTO 解包，失败抛 ApiError）：
 *   - GET    /stickers                 列表（标签 / 标注状态过滤，可内联数据）
 *   - POST   /stickers                 批量上传（来源备注必填）
 *   - PATCH  /stickers/{id}            标签 / 描述 / 来源备注修改
 *   - DELETE /stickers/{id}            单条删除
 *   - POST   /stickers/{id}/annotate   VLM 标注（失败落状态可重试）
 *
 * 人格作用域（本工程约定）：persona_id 为显式查询参数（undefined = 当前
 * 激活人格，由后端解析）；他人格表情包不可见（404 隔离语义）。
 */

import { request } from './client'
import type {
  StickerAnnotationResult,
  StickerBatchDeleteResult,
  StickerBatchUploadRequest,
  StickerBatchUploadResult,
  StickerListParams,
  StickerRecord,
  StickerUpdateRequest,
} from '@/types/stickers'

/** persona_id 查询参数形态（undefined 时不出现在查询串）。 */
function personaScope(personaId: number | undefined): { persona_id: number } | undefined {
  return personaId === undefined ? undefined : { persona_id: personaId }
}

/** 列表查询串（null 过滤项不出现；include_data 直传布尔）。 */
function listParams(
  personaId: number | undefined,
  params: StickerListParams,
): Record<string, string | number | boolean | undefined> {
  return {
    ...personaScope(personaId),
    tag: params.tag ?? undefined,
    annotation_status: params.annotation_status ?? undefined,
    limit: params.limit,
    include_data: params.include_data,
  }
}

/** GET /stickers —— 表情包列表（新入库在前）。 */
export async function listStickers(
  personaId: number | undefined,
  params: StickerListParams,
): Promise<StickerRecord[]> {
  return request<StickerRecord[]>({
    method: 'GET',
    url: '/stickers',
    params: listParams(personaId, params),
  })
}

/** POST /stickers —— 批量上传（来源备注必填；重复文件按哈希跳过）。 */
export async function uploadStickers(
  personaId: number | undefined,
  payload: StickerBatchUploadRequest,
): Promise<StickerBatchUploadResult> {
  return request<StickerBatchUploadResult>({
    method: 'POST',
    url: '/stickers',
    params: personaScope(personaId),
    data: payload,
  })
}

/** PATCH /stickers/{id} —— 修改标签 / 描述 / 来源备注。 */
export async function updateSticker(
  personaId: number | undefined,
  stickerId: number,
  payload: StickerUpdateRequest,
): Promise<StickerRecord> {
  return request<StickerRecord>({
    method: 'PATCH',
    url: `/stickers/${stickerId}`,
    params: personaScope(personaId),
    data: payload,
  })
}

/** DELETE /stickers/{id} —— 单条删除（缺失主键幂等报告）。 */
export async function deleteSticker(
  personaId: number | undefined,
  stickerId: number,
): Promise<StickerBatchDeleteResult> {
  return request<StickerBatchDeleteResult>({
    method: 'DELETE',
    url: `/stickers/${stickerId}`,
    params: personaScope(personaId),
  })
}

/** POST /stickers/{id}/annotate —— VLM 标注（失败返回 ok=false 的可读结果）。 */
export async function annotateSticker(
  personaId: number | undefined,
  stickerId: number,
): Promise<StickerAnnotationResult> {
  return request<StickerAnnotationResult>({
    method: 'POST',
    url: `/stickers/${stickerId}/annotate`,
    params: personaScope(personaId),
  })
}

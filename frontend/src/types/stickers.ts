/**
 * 表情包库 wire 类型（后端 multimodal/sticker_models 的手写镜像）。
 *
 * 契约约束（与后端 JSON Schema 逐字段对齐）：字段名保持 snake_case；新增 /
 * 删除字段须同步后端 DTO 并重跑 `pixi run export-dto-schemas` 更新契约
 * fixture（dtoContract.test.ts 双侧比对）。
 */

/** VLM 标注状态值域。 */
export type StickerAnnotationStatus = 'pending' | 'done' | 'failed'

/** 上传拒收原因值域。 */
export type StickerRejectReason =
  | 'duplicate'
  | 'payload_invalid'
  | 'payload_too_large'
  | 'unsupported_content_type'
  | 'source_note_invalid'
  | 'tag_invalid'
  | 'description_too_long'

/** 单条表情包上传请求（图片字节 base64 + 来源备注必填）。 */
export interface StickerUploadItem {
  image_base64: string
  content_type: string
  source_note: string
  tags: string[]
  description: string
}

/** 批量上传请求。 */
export interface StickerBatchUploadRequest {
  items: StickerUploadItem[]
}

/** 表情包修改请求（至少一项非 null）。 */
export interface StickerUpdateRequest {
  tags: string[] | null
  description: string | null
  source_note: string | null
}

/** 表情包列表查询参数。 */
export interface StickerListParams {
  tag: string | null
  annotation_status: StickerAnnotationStatus | null
  limit: number
  include_data: boolean
}

/** 批量删除请求。 */
export interface StickerBatchDeleteRequest {
  ids: number[]
}

/** 表情包记录（含标注状态与来源备注）。 */
export interface StickerRecord {
  id: number
  sha256: string
  content_type: string
  size_bytes: number
  tags: string[]
  description: string
  source_note: string
  annotation_status: StickerAnnotationStatus
  annotation_error: string | null
  annotation_model_entry: string | null
  annotated_at_ms: number | null
  created_at: number
  updated_at: number
  data_base64: string | null
}

/** 批量上传中被拒收的条目。 */
export interface StickerRejectedItem {
  index: number
  reason: StickerRejectReason
  message: string
}

/** 批量上传结果（部分成功语义）。 */
export interface StickerBatchUploadResult {
  created: StickerRecord[]
  rejected: StickerRejectedItem[]
}

/** 批量删除结果（部分成功语义）。 */
export interface StickerBatchDeleteResult {
  deleted_ids: number[]
  missing_ids: number[]
}

/** VLM 标注结果（失败也是可读结构）。 */
export interface StickerAnnotationResult {
  ok: boolean
  record: StickerRecord
  message: string
}

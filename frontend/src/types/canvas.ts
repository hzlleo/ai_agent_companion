/**
 * 画布产物 wire 类型（后端 canvas/canvas_models 的手写镜像）。
 *
 * 契约约束（与后端 JSON Schema 逐字段对齐）：字段名保持 snake_case；新增 /
 * 删除字段须同步后端 DTO 并重跑 `pixi run export-dto-schemas` 更新契约
 * fixture（dtoContract.test.ts 双侧比对）。
 */

/** 画布产物类型值域（决定管理台预览方式与导出 MIME / 扩展名）。 */
export type CanvasKind = 'html' | 'svg' | 'markdown'

/** 创建画布请求（标题 / 类型 / 自包含内容）。 */
export interface CanvasCreateRequest {
  title: string
  kind: CanvasKind
  content: string
}

/** 更新画布请求（至少一项非 null；类型创建后固定）。 */
export interface CanvasUpdateRequest {
  title: string | null
  content: string | null
}

/** 画布列表查询参数（类型过滤 + 条数上限）。 */
export interface CanvasListParams {
  kind: CanvasKind | null
  limit: number
}

/** 画布记录（列表 / 详情 / 导出前置共用形态）。 */
export interface CanvasRecord {
  id: number
  title: string
  kind: CanvasKind
  content: string
  revision: number
  created_at_ms: number
  updated_at_ms: number
}

/** 删除结果（缺失主键幂等报告）。 */
export interface CanvasDeleteResult {
  deleted_ids: number[]
}

/** 导出下载载荷（二进制 + 服务端文件名）。 */
export interface CanvasExportFile {
  blob: Blob
  filename: string
}

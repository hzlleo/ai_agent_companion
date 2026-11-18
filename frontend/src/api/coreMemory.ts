/**
 * core-memory 端点层：自编辑核心记忆（四块自我模型 / 修订历史）的 typed 封装。
 *
 * 路由面（全部经 ResultDTO 解包，失败抛 ApiError）：
 *   - GET /core-memory/blocks                          四块读取（首次读取懒建空行）
 *   - PUT /core-memory/blocks/{block}                  主人编辑（可清空 / 可锁定）
 *   - GET /core-memory/blocks/{block}/revisions        修订历史（最新在前）
 *
 * 人格作用域（本工程约定）：persona_id 为显式查询参数（undefined = 当前
 * 激活人格，由后端解析）；他人格的块不可见（404 隔离语义）。
 */

import { request } from './client'
import type {
  CoreBlockKey,
  CoreMemoryBlock,
  CoreMemoryOwnerUpdateRequest,
  CoreMemoryRevision,
} from '@/types/persona'

/** persona_id 查询参数形态（undefined 时不出现在查询串）。 */
function personaScope(personaId: number | undefined): { persona_id: number } | undefined {
  return personaId === undefined ? undefined : { persona_id: personaId }
}

/** GET /core-memory/blocks —— 四块读取（固定块序）。 */
export async function listCoreMemoryBlocks(
  personaId: number | undefined,
): Promise<CoreMemoryBlock[]> {
  return request<CoreMemoryBlock[]>({
    method: 'GET',
    url: '/core-memory/blocks',
    params: personaScope(personaId),
  })
}

/** PUT /core-memory/blocks/{block} —— 主人编辑（可清空 / 可锁定）。 */
export async function updateCoreMemoryBlock(
  personaId: number | undefined,
  block: CoreBlockKey,
  payload: CoreMemoryOwnerUpdateRequest,
): Promise<CoreMemoryBlock> {
  return request<CoreMemoryBlock>({
    method: 'PUT',
    url: `/core-memory/blocks/${block}`,
    params: personaScope(personaId),
    data: payload,
  })
}

/** GET /core-memory/blocks/{block}/revisions —— 修订历史（最新在前）。 */
export async function listCoreMemoryRevisions(
  personaId: number | undefined,
  block: CoreBlockKey,
  limit = 20,
): Promise<CoreMemoryRevision[]> {
  return request<CoreMemoryRevision[]>({
    method: 'GET',
    url: `/core-memory/blocks/${block}/revisions`,
    params: { ...personaScope(personaId), limit },
  })
}

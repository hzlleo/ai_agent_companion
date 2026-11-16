/**
 * models 端点层：后端 5 个路由的 typed 封装。
 *
 * 路由面（全部经 ResultDTO 解包，失败抛 ApiError）：
 *   - GET    /models/entries          注册表状态（条目脱敏 + 解析状态 + 显式绑定）
 *   - GET    /models/roles            五角色绑定面（bound / unbound）
 *   - POST   /models/entries          创建条目（原子持久化 + 热重建；重名 409）
 *   - PUT    /models/entries/{name}   部分更新（name 不可变——请求体携带即 400）
 *   - DELETE /models/entries/{name}   删除（角色绑定守卫：仍绑定角色 409）
 *
 * 密钥不变式（本工程约定）：请求面 extra=forbid（未声明键即 400）；
 * 响应面条目只回 api_key_env 环境变量名。
 */

import { request } from './client'
import type {
  ModelEntryCreateRequest,
  ModelEntryUpdateRequest,
  RegistryState,
  RoleBinding,
} from '@/types/model-registry'

/** GET /models/entries —— 读取当前注册表状态（脱敏；每次写操作的返回形态）。 */
export async function getRegistryState(): Promise<RegistryState> {
  return request<RegistryState>({
    method: 'GET',
    url: '/models/entries',
  })
}

/** GET /models/roles —— 读取五角色绑定面（角色名字典序；未绑定角色调用链路 fail loud）。 */
export async function listRoleBindings(): Promise<RoleBinding[]> {
  return request<RoleBinding[]>({
    method: 'GET',
    url: '/models/roles',
  })
}

/** POST /models/entries —— 创建条目（name 全局唯一；base_url 须 http(s)）。 */
export async function createModelEntry(payload: ModelEntryCreateRequest): Promise<RegistryState> {
  return request<RegistryState>({
    method: 'POST',
    url: '/models/entries',
    data: payload,
  })
}

/** PUT /models/entries/{name} —— 部分更新（仅出现字段生效；concurrency 显式 null = 重置全局默认）。 */
export async function updateModelEntry(
  name: string,
  payload: ModelEntryUpdateRequest,
): Promise<RegistryState> {
  return request<RegistryState>({
    method: 'PUT',
    url: `/models/entries/${name}`,
    data: payload,
  })
}

/** DELETE /models/entries/{name} —— 删除（仍绑定角色 409——先解绑再删除）。 */
export async function deleteModelEntry(name: string): Promise<RegistryState> {
  return request<RegistryState>({
    method: 'DELETE',
    url: `/models/entries/${name}`,
  })
}

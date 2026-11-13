/**
 * model-registry 域类型契约（与后端 harness.config.registry_admin 契约
 * 对齐的 wire 形态）。
 *
 * 覆盖面：注册表状态（条目脱敏 + 显式绑定）/ 角色绑定行 / 条目创建 /
 * 部分更新请求。密钥不变式：读取面只携带 api_key_env 环境变量名（密钥
 * 值永不出现）；写入面 extra=forbid（携带未声明键——含 api_key 密钥
 * 字段——即 400）。本文件只承载类型（Unit 文件）。
 */

// ---------------------------------------------------------------------------
// 值域
// ---------------------------------------------------------------------------

/** 模型角色值域（五大角色——与后端 config.ModelRole 一一对应）。 */
export type ModelRole = 'chat' | 'duty' | 'dream' | 'look_at' | 'embed'

// ---------------------------------------------------------------------------
// 读取面
// ---------------------------------------------------------------------------

/** 模型条目（脱敏读取面——永不携带密钥值）。 */
export interface ModelEntryMasked {
  /** 条目唯一标识（不可变）。 */
  name: string
  /** provider 类型。 */
  provider: string
  /** API 基础 URL（http(s)）。 */
  base_url: string
  /** API Key 环境变量名（空 = 本地免鉴权端点）。 */
  api_key_env: string
  /** 模型名（传给 provider 的 model 参数）。 */
  model: string
  /** 条目声明的角色集（角色名字符串数组）。 */
  roles: string[]
  /** 解析器当前实际解析到本条目的角色集（显式绑定 + 声明合并裁决）。 */
  resolved_roles: string[]
  /** 条目并发上限（null = 全局默认）。 */
  concurrency: number | null
  /** 是否支持多模态输入。 */
  multimodal: boolean
}

/** 注册表状态（读取面 + 每次写操作的返回形态）。 */
export interface RegistryState {
  /** 当前注册表条目（脱敏，声明顺序）。 */
  entries: ModelEntryMasked[]
  /** 显式角色绑定（角色 → 条目名；优先于条目声明）。 */
  role_bindings: Record<string, string>
}

/** 角色绑定行（五角色逐一呈现 bound / unbound）。 */
export interface RoleBinding {
  /** 角色名。 */
  role: string
  /** 是否已绑定条目（显式绑定或条目声明任一命中）。 */
  bound: boolean
  /** 绑定的条目名（未绑定为 null）。 */
  entry_name: string | null
}

// ---------------------------------------------------------------------------
// 写入面（请求体——extra=forbid）
// ---------------------------------------------------------------------------

/** 模型条目创建请求（name 全局唯一；base_url 须 http(s)）。 */
export interface ModelEntryCreateRequest {
  /** 条目唯一标识（非空，1-128 字符）。 */
  name: string
  /** provider 类型（默认 openai_compatible）。 */
  provider: string
  /** API 基础 URL（http(s)；OpenAI 兼容端点根路径含 /v1）。 */
  base_url: string
  /** API Key 环境变量名（空 = 本地免鉴权端点；只接受变量名）。 */
  api_key_env: string
  /** 模型名（非空，1-256 字符）。 */
  model: string
  /** 条目承接的角色集（默认空）。 */
  roles: ModelRole[]
  /** 条目并发上限（null = 全局默认）。 */
  concurrency: number | null
  /** 是否支持多模态输入。 */
  multimodal: boolean
}

/**
 * 模型条目部分更新请求（name 不可变——请求体携带即 400）。
 *
 * 逐字段 null 语义：provider / base_url / api_key_env / model /
 * multimodal 缺席 = 不变；roles 缺席 = 不变、[] = 清空声明；
 * concurrency 缺席 = 不变、显式 null = 重置为全局默认。
 */
export interface ModelEntryUpdateRequest {
  /** 新 provider 类型（缺席 = 不变）。 */
  provider?: string
  /** 新 API 基础 URL（缺席 = 不变；提供时须 http(s)）。 */
  base_url?: string
  /** 新 API Key 环境变量名（缺席 = 不变；空串 = 免鉴权）。 */
  api_key_env?: string
  /** 新模型名（缺席 = 不变）。 */
  model?: string
  /** 新角色集（缺席 = 不变；空列表 = 清空声明）。 */
  roles?: ModelRole[]
  /** 新并发上限（缺席 = 不变；显式 null = 重置全局默认）。 */
  concurrency?: number | null
  /** 新多模态标志（缺席 = 不变）。 */
  multimodal?: boolean
}

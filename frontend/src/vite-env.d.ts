/// <reference types="vite/client" />

/** 环境变量契约（本工程约定）：仅 VITE_API_BASE_URL 一个可选项。 */
interface ImportMetaEnv {
  /** 后端 API 基础地址（缺省 http://127.0.0.1:8000）。 */
  readonly VITE_API_BASE_URL?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}

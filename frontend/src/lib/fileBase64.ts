/**
 * 浏览器文件 → base64 载荷助手（表情包上传面）。
 *
 * 上传接口使用 JSON + base64（与后端 look_at / 声纹面同约定）；本模块把
 * ``File`` 读成 data URL 并剥离前缀，另提供容错的内容类型推断（浏览器
 * 未提供 MIME 时按扩展名映射）。读取失败抛错由调用方 toast。
 */

/** 允许的表情包 MIME 白名单（与后端 STICKER_IMAGE_MIME_ALLOWLIST 一致）。 */
export const STICKER_MIME_ALLOWLIST: readonly string[] = [
  'image/png',
  'image/jpeg',
  'image/webp',
  'image/gif',
]

/** 扩展名 → MIME 兜底映射（浏览器 File.type 为空时使用）。 */
const EXTENSION_MIME: Record<string, string> = {
  png: 'image/png',
  jpg: 'image/jpeg',
  jpeg: 'image/jpeg',
  webp: 'image/webp',
  gif: 'image/gif',
}

/**
 * 读取文件为纯 base64（不含 data URL 前缀）。
 *
 * @param file 浏览器文件对象。
 * @returns base64 载荷（无换行）。
 * @throws Error 读取失败（FileReader 错误）。
 */
export function readFileAsBase64(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = () => {
      const result = typeof reader.result === 'string' ? reader.result : ''
      const comma = result.indexOf(',')
      resolve(comma >= 0 ? result.slice(comma + 1) : result)
    }
    reader.onerror = () => {
      reject(reader.error ?? new Error('读取文件失败'))
    }
    reader.readAsDataURL(file)
  })
}

/**
 * 推断文件的内容类型（白名单内直用；否则按扩展名兜底）。
 *
 * @param file 浏览器文件对象。
 * @returns MIME 类型；无法识别时返回空串（调用方按不支持处理）。
 */
export function resolveStickerContentType(file: File): string {
  const declared = file.type.trim().toLowerCase()
  if (STICKER_MIME_ALLOWLIST.includes(declared)) {
    return declared
  }
  const extension = file.name.includes('.')
    ? file.name.slice(file.name.lastIndexOf('.') + 1).toLowerCase()
    : ''
  return EXTENSION_MIME[extension] ?? ''
}

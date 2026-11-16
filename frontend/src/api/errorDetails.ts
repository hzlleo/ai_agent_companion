/**
 * ApiError details 首条字段定位提取——表单提交失败 toast 附注用。
 *
 * 后端 400 校验错误（RequestValidationError）的 details 形态为
 * ``{errors: [{loc, msg, type}, ...]}``（逐字段定位）；通用 message
 * 无字段信息，用户无法定位出错条目。本助手提取首条错误的字段路径
 * （loc 以 ``.`` 拼接、数字段渲染为 ``[n]`` 下标、剥离 ``body`` /
 * ``query`` / ``path`` 前缀）拼为 toast 附注形态。
 */

import type { ApiError } from './client'

/** 后端校验错误 details.errors 单条形态（loc 可能混合字符串与下标）。 */
interface FieldErrorItem {
  loc: (string | number)[]
  msg: string
}

/**
 * 判别值是否为合法的 ``errors`` 数组（逐条 ``{loc, msg}`` 形态）。
 *
 * @param values 待判别值（ApiError.details 的 errors 字段——unknown）。
 * @returns 全部条目合法时 true（谓词收窄为 FieldErrorItem[]）。
 */
function isFieldErrorItemArray(values: unknown): values is FieldErrorItem[] {
  if (!Array.isArray(values)) {
    return false
  }
  return values.every((item) => {
    if (typeof item !== 'object' || item === null) {
      return false
    }
    const { loc, msg } = item as { loc?: unknown; msg?: unknown }
    if (!Array.isArray(loc) || typeof msg !== 'string') {
      return false
    }
    return loc.every((part) => typeof part === 'string' || typeof part === 'number')
  })
}

/**
 * 判别并提取 details 中首个 ``errors`` 数组条目。
 *
 * @param details ApiError.details（unknown——形态不保证）。
 * @returns 首条 ``{loc, msg}``；非预期形态返回 null。
 */
function firstFieldError(details: unknown): FieldErrorItem | null {
  if (typeof details !== 'object' || details === null) {
    return null
  }
  const errors = (details as { errors?: unknown }).errors
  if (!isFieldErrorItemArray(errors) || errors.length === 0) {
    return null
  }
  return errors[0] ?? null
}

/**
 * 字段路径拼接（loc → 可读路径）。
 *
 * 剥离开头的 ``body`` / ``query`` / ``path`` 段；数字段渲染为前一字段的
 * 下标（``entries[2]``），其余以 ``.`` 相接。
 *
 * @param loc 后端 loc 数组（字符串 / 下标混合）。
 * @returns 形如 ``card.character_book.entries[2].secondary_keys`` 的路径。
 */
export function formatFieldLoc(loc: (string | number)[]): string {
  const parts = loc[0] === 'body' || loc[0] === 'query' || loc[0] === 'path' ? loc.slice(1) : loc
  let result = ''
  for (const part of parts) {
    if (typeof part === 'number') {
      result += `[${part}]`
    } else {
      result += result === '' ? part : `.${part}`
    }
  }
  return result
}

/**
 * toast 附注后缀：首条校验错误的 ``（字段路径: 错误信息）``。
 *
 * @param error 表单提交失败抛出的 ApiError。
 * @returns 形如 ``（card.character_book.entries[2].secondary_keys: selective=True 条目必须…）``
 *     的附注；details 无结构化 errors 时返回空串（无附注）。
 */
export function apiErrorFieldSuffix(error: ApiError): string {
  const first = firstFieldError(error.details)
  if (first === null) {
    return ''
  }
  const path = formatFieldLoc(first.loc)
  if (path === '') {
    return ''
  }
  return `（${path}: ${first.msg}）`
}

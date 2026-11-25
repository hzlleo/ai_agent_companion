// 前端产物体积门禁：解码 Vite 构建 manifest，区分初始加载 JS 与路由级
// 动态 chunk，按固定阈值判定。纯 Node 内置模块，零第三方依赖。
//
// 阈值（唯一常量来源；文档可引用数值，不复写判定逻辑）：
//   INITIAL_JS_BUDGET_BYTES = 1_000_000  —— 初始加载 JS 总量上限（超过即必须拆分）
//   MIN_DYNAMIC_CHUNKS      = 3          —— 至少 3 个动态 chunk（证明路由级拆分生效）
//
// 前置：dist 由 `pixi run build` 生成（vite.config.ts 开启 build.manifest，
// manifest 位于 dist/.vite/manifest.json）。任一步失败均以非 0 退出，成功输出
// 固定的末行标记 `[bundle-budget] PASS total=<bytes> lazy=<count>`。

import { existsSync, readFileSync, statSync } from 'node:fs'
import { join, resolve } from 'node:path'
import { gzipSync } from 'node:zlib'

const INITIAL_JS_BUDGET_BYTES = 1_000_000
const MIN_DYNAMIC_CHUNKS = 3

const DIST_DIR = resolve(process.cwd(), 'dist')
const MANIFEST_PATH = join(DIST_DIR, '.vite', 'manifest.json')

/**
 * 以受控单行消息失败退出（不展开 traceback）。
 * @param {string} reason 机器可读的失败原因码。
 * @param {string} [detail] 便于定位的补充字段。
 */
function fail(reason, detail) {
  const suffix = detail === undefined ? '' : ` ${detail}`
  process.stderr.write(`[bundle-budget] FAIL reason=${reason}${suffix}\n`)
  process.exit(1)
}

/**
 * 读取 dist 内产物的字节数与 gzip 字节数。
 * @param {string} file manifest 中的相对路径（相对 dist）。
 * @returns {{bytes: number, gzip: number}} 体积统计。
 */
function measure(file) {
  const absolute = join(DIST_DIR, file)
  if (!existsSync(absolute)) {
    fail('chunk_missing', `file=${file}`)
  }
  const raw = readFileSync(absolute)
  return { bytes: raw.length, gzip: gzipSync(raw).length }
}

if (!existsSync(DIST_DIR)) {
  fail('dist_missing', `path=${DIST_DIR}`)
}
if (!existsSync(MANIFEST_PATH)) {
  fail('manifest_missing', `path=${MANIFEST_PATH} hint=run-build-first`)
}

let manifest
try {
  manifest = JSON.parse(readFileSync(MANIFEST_PATH, 'utf8'))
} catch (error) {
  fail('manifest_parse_error', `message=${error instanceof Error ? error.message : String(error)}`)
}

if (manifest === null || typeof manifest !== 'object' || Array.isArray(manifest)) {
  fail('manifest_invalid', 'expected=object')
}
const records = Object.entries(manifest)
if (records.length === 0) {
  fail('manifest_empty', 'expected=non-empty')
}

const byKey = new Map(records)
const initialFiles = new Set()
const queue = []

for (const [, item] of records) {
  if (item !== null && typeof item === 'object' && item.isEntry === true) {
    if (typeof item.file === 'string' && item.file.endsWith('.js')) {
      initialFiles.add(item.file)
      queue.push(item)
    }
  }
}
if (initialFiles.size === 0) {
  fail('entry_missing', 'expected=manifest.isEntry')
}

// 初始闭包 = 入口 + 其静态 import 链（动态 import 不计入首屏）。
while (queue.length > 0) {
  const item = queue.shift()
  const imports = Array.isArray(item.imports) ? item.imports : []
  for (const importedKey of imports) {
    const dependency = byKey.get(importedKey)
    if (dependency === undefined || typeof dependency.file !== 'string') {
      continue
    }
    const file = dependency.file
    if (!file.endsWith('.js') || initialFiles.has(file)) {
      continue
    }
    initialFiles.add(file)
    queue.push(dependency)
  }
}

const allJsFiles = new Set()
for (const [, item] of records) {
  if (item !== null && typeof item === 'object' && typeof item.file === 'string' && item.file.endsWith('.js')) {
    allJsFiles.add(item.file)
  }
}
const lazyFiles = [...allJsFiles].filter((file) => !initialFiles.has(file)).sort()

const initialRows = [...initialFiles].sort().map((file) => ({ file, ...measure(file) }))
const lazyRows = lazyFiles.map((file) => ({ file, ...measure(file) }))
const initialTotal = initialRows.reduce((sum, row) => sum + row.bytes, 0)
const initialGzip = initialRows.reduce((sum, row) => sum + row.gzip, 0)
const lazyTotal = lazyRows.reduce((sum, row) => sum + row.bytes, 0)
const lazyGzip = lazyRows.reduce((sum, row) => sum + row.gzip, 0)

for (const row of initialRows) {
  process.stdout.write(`[bundle-budget] initial ${row.file} bytes=${row.bytes} gzip=${row.gzip}\n`)
}
for (const row of lazyRows) {
  process.stdout.write(`[bundle-budget] lazy ${row.file} bytes=${row.bytes} gzip=${row.gzip}\n`)
}
process.stdout.write(
  `[bundle-budget] summary initial_bytes=${initialTotal} initial_gzip=${initialGzip} lazy_bytes=${lazyTotal} lazy_gzip=${lazyGzip} lazy_chunks=${lazyRows.length}\n`,
)

if (initialTotal > INITIAL_JS_BUDGET_BYTES) {
  fail('initial_budget_exceeded', `total=${initialTotal} budget=${INITIAL_JS_BUDGET_BYTES}`)
}
if (lazyRows.length < MIN_DYNAMIC_CHUNKS) {
  fail('dynamic_chunks_below_min', `lazy=${lazyRows.length} min=${MIN_DYNAMIC_CHUNKS}`)
}

process.stdout.write(`[bundle-budget] PASS total=${initialTotal} lazy=${lazyRows.length}\n`)

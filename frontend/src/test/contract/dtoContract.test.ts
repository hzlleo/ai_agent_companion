/**
 * 前后端契约漂移门禁（前端侧）：手写 types 成员名 ↔ 后端 DTO JSON Schema
 * properties 键集逐组相等断言。
 *
 * 数据源为提交态 fixture ``dtoSchemas.json``（由仓库根
 * ``scripts/export_dto_schemas.py`` 经 ``cd backend && pixi run
 * export-dto-schemas`` 导出——幂等 byte-stable）。fixture 的 manifest
 * 段固化「schema 键 ↔ 前端类型名 ↔ 前端源文件」映射；本测试用
 * TypeScript 编译器 API 解析前端源文件，提取各类型的成员名集合
 * （含同文件 extends 继承面）与 schema properties 键集比对——拦
 * 「前端类型少写 / 多写字段」方向；「后端 DTO 改动未重新导出」方向
 * 由后端 test_dto_schema_fixture.py 把关。测试只读提交文件，零网络。
 */

import ts from 'typescript'
import { describe, expect, it } from 'vitest'

/** fixture manifest 单条映射形态。 */
interface SchemaMapping {
  schema_key: string
  frontend_type: string
  frontend_file: string
  note?: string
}

/** fixture 根形态（schemas 值只取 properties 键，其余不关心）。 */
interface DtoSchemasFixture {
  manifest: { mappings: SchemaMapping[] }
  schemas: Record<string, { properties?: Record<string, unknown> }>
}

/** 前端类型声明提取结果：自身成员名 + 同文件 extends 基名。 */
interface TypeDecl {
  members: Set<string>
  bases: string[]
}

/**
 * 前端类型源文件（提交态原文经 Vite ``?raw`` 管道内联——零 node:fs、
 * 零网络）。键为相对本文件的 glob 路径（``../../types/*.ts``）。
 */
const TYPE_SOURCES: Record<string, string> = import.meta.glob<string>('../../types/*.ts', {
  query: '?raw',
  import: 'default',
  eager: true,
})

/** fixture 原文（同目录提交态 JSON）。 */
const FIXTURE_SOURCES: Record<string, string> = import.meta.glob<string>('./dtoSchemas.json', {
  query: '?raw',
  import: 'default',
  eager: true,
})

/** manifest 的仓库根相对路径 → 本测试文件的 glob 键（frontend/src/ 前缀折叠）。 */
function globKeyFor(frontendFile: string): string {
  return `../../${frontendFile.replace('frontend/src/', '')}`
}

/**
 * 解析一个前端类型源文件，产出「类型名 → 声明」映射。
 *
 * 支持导出 interface 与 ``type X = { ... }`` 字面量两种形态；extends
 * 基名仅记录（继承面由 resolveTypeMembers 按同文件闭包展开）。
 */
function parseTypeDeclarations(source: string): Map<string, TypeDecl> {
  const sourceFile = ts.createSourceFile(
    'types.ts',
    source,
    ts.ScriptTarget.ES2022,
    /* setParentNodes */ true,
  )
  const declarations = new Map<string, TypeDecl>()
  for (const statement of sourceFile.statements) {
    let name: string | undefined
    let members: readonly ts.TypeElement[] | undefined
    let bases: string[] = []
    if (ts.isInterfaceDeclaration(statement)) {
      name = statement.name.text
      members = statement.members
      bases = statement.heritageClauses
        ?.filter((clause) => clause.token === ts.SyntaxKind.ExtendsKeyword)
        .flatMap((clause) => clause.types.map((type) => type.expression.getText()))
        ?? []
    } else if (ts.isTypeAliasDeclaration(statement) && ts.isTypeLiteralNode(statement.type)) {
      name = statement.name.text
      members = statement.type.members
    }
    if (name === undefined || members === undefined) {
      continue
    }
    const memberNames = new Set<string>()
    for (const member of members) {
      if (ts.isPropertySignature(member)) {
        memberNames.add(member.name.getText())
      }
    }
    declarations.set(name, { members: memberNames, bases })
  }
  return declarations
}

/** 文件级声明缓存（每源文件只解析一次）。 */
const fileDeclarations = new Map<string, Map<string, TypeDecl>>()

/** 取文件声明映射（带缓存；文件缺失即失败——门禁数据完整性）。 */
function declarationsFor(frontendFile: string): Map<string, TypeDecl> {
  const cached = fileDeclarations.get(frontendFile)
  if (cached !== undefined) {
    return cached
  }
  const source = TYPE_SOURCES[globKeyFor(frontendFile)]
  if (source === undefined) {
    throw new Error(`前端源文件未入 glob 收集面: ${frontendFile}`)
  }
  const parsed = parseTypeDeclarations(source)
  fileDeclarations.set(frontendFile, parsed)
  return parsed
}

/**
 * 展开类型的完整成员名集合（自身 + 同文件 extends 继承面）。
 *
 * @throws 跨文件继承（本门禁闭集内不存在）——显式失败而非静默漏检。
 */
function resolveTypeMembers(frontendFile: string, typeName: string): Set<string> {
  const declarations = declarationsFor(frontendFile)
  const decl = declarations.get(typeName)
  if (decl === undefined) {
    throw new Error(`前端源文件 ${frontendFile} 未找到类型 ${typeName}（manifest 映射失效）`)
  }
  const resolved = new Set(decl.members)
  for (const base of decl.bases) {
    const baseDecl = declarations.get(base)
    if (baseDecl === undefined) {
      throw new Error(
        `${frontendFile}: ${typeName} 的 extends 基类 ${base} 不在本文件（跨文件继承需先扩门禁解析器）`,
      )
    }
    for (const inherited of resolveTypeMembers(frontendFile, base)) {
      resolved.add(inherited)
    }
  }
  return resolved
}

/** fixture（提交态——门禁基线；与本测试文件同目录）。 */
const fixtureRaw: string | undefined = FIXTURE_SOURCES['./dtoSchemas.json']
if (fixtureRaw === undefined) {
  throw new Error('提交态 fixture 缺失: src/test/contract/dtoSchemas.json')
}
const fixture: DtoSchemasFixture = JSON.parse(fixtureRaw) as DtoSchemasFixture

/** manifest 映射（schema 键 ↔ 前端类型 ↔ 前端源文件）。 */
const mappings: SchemaMapping[] = fixture.manifest.mappings

describe('DTO 契约漂移门禁（types 镜像 ↔ 后端 schema）', () => {
  it('fixture 自洽：映射与 schemas 键集一一对应（无孤儿 / 无缺位）', () => {
    const mappingKeys = new Set(mappings.map((mapping) => mapping.schema_key))
    const schemaKeys = new Set(Object.keys(fixture.schemas))
    expect(mappingKeys.size).toBe(mappings.length)
    expect([...mappingKeys].sort()).toEqual([...schemaKeys].sort())
  })

  it('逐组断言：前端类型成员名集合 === schema properties 键集', () => {
    const drifts: string[] = []
    for (const mapping of mappings) {
      const schema = fixture.schemas[mapping.schema_key]
      expect(schema, `fixture 缺 schema: ${mapping.schema_key}`).toBeDefined()
      const schemaProps = new Set(Object.keys(schema?.properties ?? {}))
      const tsMembers = resolveTypeMembers(mapping.frontend_file, mapping.frontend_type)
      const missing = [...schemaProps].filter((key) => !tsMembers.has(key)).sort()
      const extra = [...tsMembers].filter((key) => !schemaProps.has(key)).sort()
      if (missing.length > 0 || extra.length > 0) {
        drifts.push(
          `${mapping.frontend_file} 的 ${mapping.frontend_type} ↔ ${mapping.schema_key}：` +
            `前端缺失字段 [${missing.join(', ')}]；前端多余字段 [${extra.join(', ')}]`,
        )
      }
    }
    expect(
      drifts,
      `契约漂移（后端改 DTO → cd backend && pixi run export-dto-schemas 重导出；` +
        `前端改 types → 按后端契约补齐成员）：\n${drifts.join('\n')}`,
    ).toEqual([])
  })
})

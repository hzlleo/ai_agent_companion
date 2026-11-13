/**
 * 人格选择器：记忆 / 用量 / 梦境等管理路由的目标人格过滤面。
 *
 * 本工程约定：admin 路由无人格上下文语义——persona_id 是显式参数；
 * 空值（''）= 当前激活人格（后端解析），选择具体人格即显式过滤。
 * 候选面复用 personas 列表端点（全部状态；游标分页，超过一页时
 * 提供「加载更多」入口）。
 */

import { useMemo } from 'react'
import { usePersonasList } from '@/hooks/usePersonasApi'
import { flattenCursorPages } from '@/api/pagination'
import { Button } from '@/components/ui/Button'
import { Select } from '@/components/ui/Select'
import { Spinner } from '@/components/ui/Spinner'
import type { PersonaRecord } from '@/types/persona'

export interface PersonaPickerProps {
  /** 选择器 id（页面 label 关联用）。 */
  id: string
  /** 当前值（'' = 当前激活人格；否则为 persona id 字符串）。 */
  value: string
  /** 值变更回调（透传字符串形态——调用方按需转 number / 透传）。 */
  onChange: (value: string) => void
  /** 标签文案（默认「人格」）。 */
  label?: string
}

/** 候选行 → option 文案（展示名 + 注册名 + 状态提示）。 */
function optionLabel(persona: PersonaRecord): string {
  const suffix = persona.is_active ? ' · 当前激活' : persona.status === 'disabled' ? ' · 停用' : ''
  return `${persona.display_name}（${persona.name}）${suffix}`
}

export function PersonaPicker({ id, value, onChange, label = '人格' }: PersonaPickerProps) {
  const listQuery = usePersonasList({ limit: 100 })
  const personas = useMemo(
    () => flattenCursorPages(listQuery.data?.pages ?? []),
    [listQuery.data],
  )

  return (
    <div className="flex items-center gap-2">
      <label htmlFor={id} className="shrink-0 text-sm text-[var(--color-text-secondary)]">
        {label}
      </label>
      <div className="flex items-center gap-2">
        <Select
          id={id}
          className="w-64"
          value={value}
          onChange={(e) => onChange(e.target.value)}
          disabled={listQuery.isPending}
        >
          <option value="">当前激活人格（缺省）</option>
          {listQuery.isPending && <option value="loading">加载中…</option>}
          {personas.map((persona) => (
            <option key={persona.id} value={String(persona.id)}>
              {optionLabel(persona)}
            </option>
          ))}
        </Select>
        {listQuery.isPending && <Spinner size="sm" />}
        {listQuery.hasNextPage && (
          <Button
            variant="ghost"
            size="sm"
            loading={listQuery.isFetchingNextPage}
            onClick={() => {
              void listQuery.fetchNextPage()
            }}
          >
            加载更多
          </Button>
        )}
      </div>
    </div>
  )
}

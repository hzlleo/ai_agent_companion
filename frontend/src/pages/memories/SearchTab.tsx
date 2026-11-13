import { useState } from 'react'
import { Search } from 'lucide-react'
import { Button } from '@/components/ui/Button'
import { Badge } from '@/components/ui/Badge'
import { Input } from '@/components/ui/Input'
import { Select } from '@/components/ui/Select'
import { Checkbox } from '@/components/ui/Checkbox'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card'
import { EmptyState } from '@/components/ui/EmptyState'
import { Spinner } from '@/components/ui/Spinner'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/Table'
import { useToast } from '@/components/ui/useToast'
import { ApiError, toApiError } from '@/api/client'
import { datetimeLocalToEpochMs, formatEpochMs } from '@/lib/datetime'
import { useSearchMemories } from '@/hooks/useMemoriesApi'
import type {
  MemoryProvenance,
  MemorySearchMode,
  MemorySearchResult,
} from '@/types/memory'
import {
  EPISODE_KIND_LABELS,
  PROVENANCE_BADGE_VARIANTS,
  PROVENANCE_LABELS,
  SEARCH_MODE_LABELS,
  SEARCH_SCORE_DIRECTION_HINTS,
  truncateText,
} from './memoryDisplay'

export interface SearchTabProps {
  /** 目标人格主键（undefined = 当前激活人格）。 */
  personaId: number | undefined
}

/** 检索类型候选（情景层 + 策展层两层词表合并）。 */
const SEARCH_KIND_OPTIONS: { value: string; label: string }[] = [
  ...Object.entries(EPISODE_KIND_LABELS).map(([value, label]) => ({ value, label })),
  ...(
    [
      ['memory', '蒸馏记忆'],
      ['user_note', '主人备忘'],
      ['relationship', '关系状态'],
    ] as const
  ).map(([value, label]) => ({ value, label })),
]

/** 可信度过滤态（空串 = 全部）。 */
type ProvenanceFilter = '' | MemoryProvenance

/** 回退存储指引文案（dense/hybrid 被 422 拒绝时的静态补充说明）。 */
const FALLBACK_STORE_GUIDANCE =
  '向量检索（稠密 / 混合）需要 SeekDB 混合检索存储；当前回退存储仅支持关键词模式——可改用「全文关键词」模式重试。'

/** 从 ApiError.details 提取后端 fix 指引（非字符串形态时回落 null）。 */
function extractFixGuidance(error: ApiError): string | null {
  if (typeof error.details !== 'object' || error.details === null) {
    return null
  }
  const fix = (error.details as Record<string, unknown>)['fix']
  return typeof fix === 'string' ? fix : null
}

/**
 * 记忆检索页签：检索表单（query / 模式 / 类型 / 可信度 / 时间范围 /
 * limit）+ 结果表（score 方向随模式）。回退存储对 dense / hybrid 的
 * 422 拒绝以中文警示条呈现（后端 message + fix 指引 + 静态补充说明）。
 */
export function SearchTab({ personaId }: SearchTabProps) {
  const toast = useToast()
  const searchMutation = useSearchMemories()
  const [queryText, setQueryText] = useState('')
  const [mode, setMode] = useState<MemorySearchMode>('keyword')
  const [kinds, setKinds] = useState<Set<string>>(new Set())
  const [provenanceFilter, setProvenanceFilter] = useState<ProvenanceFilter>('')
  const [startText, setStartText] = useState('')
  const [endText, setEndText] = useState('')
  const [limitText, setLimitText] = useState('10')

  const toggleKind = (value: string, checked: boolean): void => {
    setKinds((prev) => {
      const next = new Set(prev)
      if (checked) {
        next.add(value)
      } else {
        next.delete(value)
      }
      return next
    })
  }

  const handleSearch = (): void => {
    const query = queryText.trim()
    if (query === '') {
      toast.error('检索文本必填')
      return
    }
    const limit = Number(limitText)
    if (!Number.isInteger(limit) || limit < 1 || limit > 50) {
      toast.error('返回条数上限须为 1-50 的整数')
      return
    }
    searchMutation.mutate(
      {
        persona_id: personaId,
        query,
        mode,
        kinds: kinds.size === 0 ? undefined : [...kinds],
        provenance: provenanceFilter === '' ? undefined : provenanceFilter,
        start_ms: datetimeLocalToEpochMs(startText) ?? undefined,
        end_ms: datetimeLocalToEpochMs(endText) ?? undefined,
        limit,
      },
      {
        onSuccess: (results) => {
          toast.success(`检索完成（命中 ${results.length} 条）`)
        },
        onError: (error) => {
          toast.error(toApiError(error).message)
        },
      },
    )
  }

  const results: MemorySearchResult[] = searchMutation.data ?? []
  const searchError = searchMutation.isError ? toApiError(searchMutation.error) : null
  const fixGuidance = searchError !== null ? extractFixGuidance(searchError) : null

  return (
    <div className="space-y-6">
      <Card>
        <CardHeader>
          <CardTitle>记忆检索</CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="flex flex-wrap items-end gap-3">
            <div className="min-w-64 flex-1 space-y-1.5">
              <label htmlFor="search-query" className="text-sm font-medium">
                检索文本
              </label>
              <Input
                id="search-query"
                value={queryText}
                onChange={(e) => setQueryText(e.target.value)}
                placeholder="关键词或语义查询文本（必填，1-512 字符）"
              />
            </div>
            <div className="space-y-1.5">
              <label htmlFor="search-mode" className="text-sm font-medium">
                模式
              </label>
              <Select
                id="search-mode"
                className="w-36"
                value={mode}
                onChange={(e) => setMode(e.target.value as MemorySearchMode)}
              >
                {Object.entries(SEARCH_MODE_LABELS).map(([value, label]) => (
                  <option key={value} value={value}>
                    {label}
                  </option>
                ))}
              </Select>
            </div>
            <div className="space-y-1.5">
              <label htmlFor="search-limit" className="text-sm font-medium">
                条数上限（1-50）
              </label>
              <Input
                id="search-limit"
                type="number"
                min={1}
                max={50}
                className="w-28"
                value={limitText}
                onChange={(e) => setLimitText(e.target.value)}
              />
            </div>
            <div className="space-y-1.5">
              <label htmlFor="search-provenance" className="text-sm font-medium">
                可信度
              </label>
              <Select
                id="search-provenance"
                className="w-32"
                value={provenanceFilter}
                onChange={(e) => setProvenanceFilter(e.target.value as ProvenanceFilter)}
              >
                <option value="">全部</option>
                {Object.entries(PROVENANCE_LABELS).map(([value, label]) => (
                  <option key={value} value={value}>
                    {label}
                  </option>
                ))}
              </Select>
            </div>
            <div className="space-y-1.5">
              <label htmlFor="search-start" className="text-sm font-medium">
                时间范围（闭区间）
              </label>
              <div className="flex items-center gap-2">
                <Input
                  id="search-start"
                  type="datetime-local"
                  className="w-48"
                  value={startText}
                  onChange={(e) => setStartText(e.target.value)}
                />
                <span className="text-sm text-[var(--color-text-muted)]">至</span>
                <Input
                  id="search-end"
                  type="datetime-local"
                  className="w-48"
                  value={endText}
                  onChange={(e) => setEndText(e.target.value)}
                />
              </div>
            </div>
            <Button onClick={handleSearch} loading={searchMutation.isPending}>
              <Search className="mr-1 h-4 w-4" aria-hidden="true" />
              检索
            </Button>
          </div>
          <fieldset className="space-y-2">
            <legend className="text-sm font-medium">类型过滤（不选 = 全部）</legend>
            <div className="flex flex-wrap gap-2">
              {SEARCH_KIND_OPTIONS.map((option) => (
                <Checkbox
                  key={option.value}
                  checked={kinds.has(option.value)}
                  onChange={(e) => toggleKind(option.value, e.target.checked)}
                  label={option.label}
                />
              ))}
            </div>
          </fieldset>
        </CardContent>
      </Card>

      {searchError !== null && (
        <div
          role="alert"
          className="rounded-lg border border-[var(--color-destructive)] bg-[var(--color-destructive-light)] px-4 py-3 text-sm text-[var(--color-destructive)]"
        >
          <p className="font-medium">检索失败：{searchError.message}</p>
          {fixGuidance !== null && <p className="mt-1">{fixGuidance}</p>}
          <p className="mt-1">{FALLBACK_STORE_GUIDANCE}</p>
        </div>
      )}

      {searchMutation.isPending ? (
        <div className="flex items-center justify-center py-16">
          <Spinner size="lg" />
        </div>
      ) : searchMutation.isSuccess ? (
        results.length === 0 ? (
          <EmptyState title="未命中任何记忆" description="可调整检索文本、模式或过滤条件后重试。" />
        ) : (
          <div className="rounded-xl border border-[var(--color-border)]">
            <div className="border-b border-[var(--color-border)] px-4 py-2 text-xs text-[var(--color-text-muted)]">
              命中 {results.length} 条 · {SEARCH_SCORE_DIRECTION_HINTS[mode]}
            </div>
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead className="w-20">引用</TableHead>
                  <TableHead className="w-20">ID</TableHead>
                  <TableHead className="w-24">类型</TableHead>
                  <TableHead className="w-20">可信度</TableHead>
                  <TableHead className="w-44">时间</TableHead>
                  <TableHead>命中正文</TableHead>
                  <TableHead className="w-24">分数</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {results.map((result, index) => (
                  <TableRow key={`${result.ref_type}-${result.ref_id}-${index}`}>
                    <TableCell>
                      <Badge variant={result.ref_type === 'episode' ? 'secondary' : 'default'}>
                        {result.ref_type === 'episode' ? '情景' : '策展'}
                      </Badge>
                    </TableCell>
                    <TableCell>{result.ref_id}</TableCell>
                    <TableCell>{result.kind}</TableCell>
                    <TableCell>
                      <Badge variant={PROVENANCE_BADGE_VARIANTS[result.provenance]}>
                        {PROVENANCE_LABELS[result.provenance]}
                      </Badge>
                    </TableCell>
                    <TableCell className="text-[var(--color-text-secondary)]">
                      {formatEpochMs(result.ts_ms)}
                    </TableCell>
                    <TableCell className="text-[var(--color-text-secondary)]">
                      {truncateText(result.content)}
                    </TableCell>
                    <TableCell className="font-mono">{result.score}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>
        )
      ) : null}
    </div>
  )
}

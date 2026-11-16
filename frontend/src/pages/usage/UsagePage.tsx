import { useMemo, useState } from 'react'
import { Button } from '@/components/ui/Button'
import { Badge } from '@/components/ui/Badge'
import { Input } from '@/components/ui/Input'
import { Select } from '@/components/ui/Select'
import { Spinner } from '@/components/ui/Spinner'
import { EmptyState } from '@/components/ui/EmptyState'
import { Alert, AlertDescription } from '@/components/ui/Alert'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/Table'
import { PersonaPicker } from '@/components/PersonaPicker'
import { flattenCursorPages } from '@/api/pagination'
import { toApiError } from '@/api/client'
import { datetimeLocalToEpochMs, formatEpochMs } from '@/lib/datetime'
import {
  useUsageAggregates,
  useUsageRecordsList,
  useUsageSummary,
} from '@/hooks/useUsageApi'
import type {
  UsageGroupBy,
  UsagePurpose,
  UsageStatus,
  UsageTimeBucket,
} from '@/types/usage'
import {
  GROUP_BY_LABELS,
  PURPOSE_LABELS,
  STATUS_LABELS,
  TIME_BUCKET_LABELS,
  aggregateGroupDisplay,
  formatTokenCount,
  purposeLabel,
} from './usageDisplay'

/** 用途过滤态（空串 = 全部）。 */
type PurposeFilter = '' | UsagePurpose

/** 结果过滤态（空串 = 全部）。 */
type StatusFilter = '' | UsageStatus

/** 总览指标瓦片定义。 */
const SUMMARY_TILES: { key: 'call_count' | 'total_tokens' | 'avg_latency_ms' | 'error_count'; label: string; unit: string }[] = [
  { key: 'call_count', label: '调用次数', unit: '次' },
  { key: 'total_tokens', label: '总 Tokens', unit: '' },
  { key: 'avg_latency_ms', label: '平均时延', unit: 'ms' },
  { key: 'error_count', label: '失败次数', unit: '次' },
]

/**
 * 用量观测页：总览瓦片（调用 / token / 均时延 + per-purpose 明细）+
 * 多维聚合（模型 / 用途 / 时间桶 + minute|hour|day 桶宽）+ 记录表
 * （游标分页 + 多轴过滤）。跨人格观测：人格过滤为显式参数（缺省全人格）。
 *
 * 本工程约定：本页仅表格 + 瓦片（不引入图表库）。
 */
export default function UsagePage() {
  const [personaValue, setPersonaValue] = useState('')
  const [modelEntryFilter, setModelEntryFilter] = useState('')
  const [purposeFilter, setPurposeFilter] = useState<PurposeFilter>('')
  const [statusFilter, setStatusFilter] = useState<StatusFilter>('')
  const [startText, setStartText] = useState('')
  const [endText, setEndText] = useState('')
  const [groupBy, setGroupBy] = useState<UsageGroupBy>('purpose')
  const [timeBucket, setTimeBucket] = useState<UsageTimeBucket>('hour')

  const filterParams = useMemo(
    () => ({
      persona_id: personaValue === '' ? undefined : personaValue,
      model_entry: modelEntryFilter.trim() === '' ? undefined : modelEntryFilter.trim(),
      purpose: purposeFilter === '' ? undefined : purposeFilter,
      status: statusFilter === '' ? undefined : statusFilter,
      start_ms: datetimeLocalToEpochMs(startText) ?? undefined,
      end_ms: datetimeLocalToEpochMs(endText) ?? undefined,
    }),
    [personaValue, modelEntryFilter, purposeFilter, statusFilter, startText, endText],
  )

  const summaryQuery = useUsageSummary(filterParams)
  const aggregatesQuery = useUsageAggregates({
    ...filterParams,
    group_by: groupBy,
    time_bucket: groupBy === 'time_bucket' ? timeBucket : undefined,
  })
  const recordsQuery = useUsageRecordsList({ ...filterParams, limit: 20 })

  const records = useMemo(
    () => flattenCursorPages(recordsQuery.data?.pages ?? []),
    [recordsQuery.data],
  )

  return (
    <div className="p-8">
      <div className="mb-6">
        <h1 className="text-2xl font-semibold text-[var(--color-text)]">用量观测</h1>
        <p className="mt-1 text-sm text-[var(--color-text-secondary)]">
          跨人格观测口径：全部端点无人格上下文要求，人格过滤为显式参数（缺省 = 全人格）。
        </p>
      </div>

      {/* 过滤面（总览 / 聚合 / 记录共用） */}
      <div className="mb-6 flex flex-wrap items-center gap-3 rounded-xl border border-[var(--color-border)] p-4">
        <PersonaPicker id="usage-persona" value={personaValue} onChange={setPersonaValue} />
        <label htmlFor="usage-model-filter" className="text-sm text-[var(--color-text-secondary)]">
          模型条目
        </label>
        <Input
          id="usage-model-filter"
          className="w-40"
          value={modelEntryFilter}
          onChange={(e) => setModelEntryFilter(e.target.value)}
          placeholder="条目名（留空 = 全部）"
        />
        <label htmlFor="usage-purpose-filter" className="text-sm text-[var(--color-text-secondary)]">
          用途
        </label>
        <Select
          id="usage-purpose-filter"
          className="w-32"
          value={purposeFilter}
          onChange={(e) => setPurposeFilter(e.target.value as PurposeFilter)}
        >
          <option value="">全部</option>
          {Object.entries(PURPOSE_LABELS).map(([value, label]) => (
            <option key={value} value={value}>
              {label}
            </option>
          ))}
        </Select>
        <label htmlFor="usage-status-filter" className="text-sm text-[var(--color-text-secondary)]">
          结果
        </label>
        <Select
          id="usage-status-filter"
          className="w-28"
          value={statusFilter}
          onChange={(e) => setStatusFilter(e.target.value as StatusFilter)}
        >
          <option value="">全部</option>
          {Object.entries(STATUS_LABELS).map(([value, label]) => (
            <option key={value} value={value}>
              {label}
            </option>
          ))}
        </Select>
        <label htmlFor="usage-start" className="text-sm text-[var(--color-text-secondary)]">
          时间范围
        </label>
        <Input
          id="usage-start"
          type="datetime-local"
          className="w-52"
          value={startText}
          onChange={(e) => setStartText(e.target.value)}
        />
        <span className="text-sm text-[var(--color-text-muted)]">至</span>
        <Input
          id="usage-end"
          type="datetime-local"
          className="w-52"
          value={endText}
          onChange={(e) => setEndText(e.target.value)}
        />
      </div>

      {/* 总览瓦片 + per-purpose 明细 */}
      <Card className="mb-6">
        <CardHeader>
          <CardTitle>总览</CardTitle>
        </CardHeader>
        <CardContent>
          {summaryQuery.isPending ? (
            <div className="flex items-center justify-center py-10">
              <Spinner size="lg" />
            </div>
          ) : summaryQuery.isError ? (
            <Alert variant="destructive">
              <AlertDescription>
                <p>用量总览加载失败：{toApiError(summaryQuery.error).message}</p>
                <Button
                  variant="outline"
                  size="sm"
                  className="mt-2"
                  onClick={() => {
                    void summaryQuery.refetch()
                  }}
                >
                  重试
                </Button>
              </AlertDescription>
            </Alert>
          ) : (
            <div className="space-y-4">
              <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
                {SUMMARY_TILES.map((tile) => (
                  <div
                    key={tile.key}
                    data-testid={`usage-summary-${tile.key}`}
                    className="rounded-lg border border-[var(--color-border)] px-4 py-3"
                  >
                    <div className="text-2xl font-semibold text-[var(--color-text)]">
                      {formatTokenCount(summaryQuery.data[tile.key])}
                      {tile.unit}
                    </div>
                    <div className="text-xs text-[var(--color-text-muted)]">{tile.label}</div>
                  </div>
                ))}
              </div>
              <div>
                <h4 className="mb-2 text-sm font-medium">按用途明细（总 token 降序）</h4>
                {summaryQuery.data.purposes.length === 0 ? (
                  <p className="text-sm text-[var(--color-text-muted)]">当前过滤范围内没有用量数据。</p>
                ) : (
                  <Table>
                    <TableHeader>
                      <TableRow>
                        <TableHead>用途</TableHead>
                        <TableHead className="w-24">调用次数</TableHead>
                        <TableHead className="w-28">输入 Tokens</TableHead>
                        <TableHead className="w-28">输出 Tokens</TableHead>
                        <TableHead className="w-28">总 Tokens</TableHead>
                        <TableHead className="w-28">平均时延</TableHead>
                        <TableHead className="w-24">失败次数</TableHead>
                      </TableRow>
                    </TableHeader>
                    <TableBody>
                      {summaryQuery.data.purposes.map((row) => (
                        <TableRow key={row.group}>
                          <TableCell className="font-medium">{purposeLabel(row.group)}</TableCell>
                          <TableCell>{formatTokenCount(row.call_count)}</TableCell>
                          <TableCell>{formatTokenCount(row.input_tokens)}</TableCell>
                          <TableCell>{formatTokenCount(row.output_tokens)}</TableCell>
                          <TableCell>{formatTokenCount(row.total_tokens)}</TableCell>
                          <TableCell>{row.avg_latency_ms} ms</TableCell>
                          <TableCell>
                            {row.error_count > 0 ? (
                              <Badge variant="destructive">{row.error_count}</Badge>
                            ) : (
                              row.error_count
                            )}
                          </TableCell>
                        </TableRow>
                      ))}
                    </TableBody>
                  </Table>
                )}
              </div>
            </div>
          )}
        </CardContent>
      </Card>

      {/* 多维聚合 */}
      <Card className="mb-6">
        <CardHeader>
          <CardTitle>多维聚合</CardTitle>
        </CardHeader>
        <CardContent>
          <div className="mb-4 flex flex-wrap items-center gap-3">
            <label htmlFor="usage-group-by" className="text-sm text-[var(--color-text-secondary)]">
              分组维度
            </label>
            <Select
              id="usage-group-by"
              className="w-36"
              value={groupBy}
              onChange={(e) => setGroupBy(e.target.value as UsageGroupBy)}
            >
              {Object.entries(GROUP_BY_LABELS).map(([value, label]) => (
                <option key={value} value={value}>
                  {label}
                </option>
              ))}
            </Select>
            {groupBy === 'time_bucket' && (
              <>
                <label htmlFor="usage-time-bucket" className="text-sm text-[var(--color-text-secondary)]">
                  桶宽
                </label>
                <Select
                  id="usage-time-bucket"
                  className="w-28"
                  value={timeBucket}
                  onChange={(e) => setTimeBucket(e.target.value as UsageTimeBucket)}
                >
                  {Object.entries(TIME_BUCKET_LABELS).map(([value, label]) => (
                    <option key={value} value={value}>
                      {label}
                    </option>
                  ))}
                </Select>
              </>
            )}
          </div>
          {aggregatesQuery.isPending ? (
            <div className="flex items-center justify-center py-10">
              <Spinner size="lg" />
            </div>
          ) : aggregatesQuery.isError ? (
            <Alert variant="destructive">
              <AlertDescription>
                <p>聚合加载失败：{toApiError(aggregatesQuery.error).message}</p>
              </AlertDescription>
            </Alert>
          ) : aggregatesQuery.data.length === 0 ? (
            <EmptyState title="暂无聚合数据" description="当前过滤范围内没有用量数据。" />
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>{groupBy === 'time_bucket' ? '桶起点' : '分组'}</TableHead>
                  <TableHead className="w-24">调用次数</TableHead>
                  <TableHead className="w-28">输入 Tokens</TableHead>
                  <TableHead className="w-28">输出 Tokens</TableHead>
                  <TableHead className="w-28">总 Tokens</TableHead>
                  <TableHead className="w-28">平均时延</TableHead>
                  <TableHead className="w-24">失败次数</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {aggregatesQuery.data.map((row) => (
                  <TableRow key={row.group}>
                    <TableCell className="font-medium">
                      {aggregateGroupDisplay(
                        groupBy === 'purpose' ? purposeLabel(row.group) : row.group,
                        row.bucket_ms,
                      )}
                    </TableCell>
                    <TableCell>{formatTokenCount(row.call_count)}</TableCell>
                    <TableCell>{formatTokenCount(row.input_tokens)}</TableCell>
                    <TableCell>{formatTokenCount(row.output_tokens)}</TableCell>
                    <TableCell>{formatTokenCount(row.total_tokens)}</TableCell>
                    <TableCell>{row.avg_latency_ms} ms</TableCell>
                    <TableCell>{row.error_count}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>

      {/* 记录表 */}
      <div>
        <h3 className="mb-3 text-base font-semibold text-[var(--color-text)]">调用记录</h3>
        {recordsQuery.isPending ? (
          <div className="flex items-center justify-center py-16">
            <Spinner size="lg" />
          </div>
        ) : recordsQuery.isError ? (
          <Alert variant="destructive">
            <AlertDescription>
              <p>记录列表加载失败：{toApiError(recordsQuery.error).message}</p>
              <Button
                variant="outline"
                size="sm"
                className="mt-2"
                onClick={() => {
                  void recordsQuery.refetch()
                }}
              >
                重试
              </Button>
            </AlertDescription>
          </Alert>
        ) : records.length === 0 ? (
          <EmptyState title="暂无调用记录" description="当前过滤范围内没有用量记录。" />
        ) : (
          <div className="rounded-xl border border-[var(--color-border)]">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead className="w-16">ID</TableHead>
                  <TableHead className="w-44">时间</TableHead>
                  <TableHead className="w-20">人格</TableHead>
                  <TableHead className="w-32">模型条目</TableHead>
                  <TableHead className="w-24">用途</TableHead>
                  <TableHead className="w-28">总 Tokens</TableHead>
                  <TableHead className="w-24">时延</TableHead>
                  <TableHead className="w-20">结果</TableHead>
                  <TableHead className="w-36">错误码</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {records.map((record) => (
                  <TableRow key={record.id} data-testid={`usage-record-row-${record.id}`}>
                    <TableCell>{record.id}</TableCell>
                    <TableCell className="text-[var(--color-text-secondary)]">
                      {formatEpochMs(record.ts_ms)}
                    </TableCell>
                    <TableCell>{record.persona_id ?? '系统'}</TableCell>
                    <TableCell className="font-medium">{record.model_entry}</TableCell>
                    <TableCell>{purposeLabel(record.purpose)}</TableCell>
                    <TableCell>{formatTokenCount(record.total_tokens)}</TableCell>
                    <TableCell>{record.latency_ms} ms</TableCell>
                    <TableCell>
                      {record.status === 'success' ? (
                        <Badge variant="success">成功</Badge>
                      ) : (
                        <Badge variant="destructive">失败</Badge>
                      )}
                    </TableCell>
                    <TableCell className="text-[var(--color-text-muted)]">
                      {record.error_code ?? '—'}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
            <div className="flex items-center justify-center border-t border-[var(--color-border)] p-4">
              {recordsQuery.hasNextPage ? (
                <Button
                  variant="outline"
                  loading={recordsQuery.isFetchingNextPage}
                  onClick={() => {
                    void recordsQuery.fetchNextPage()
                  }}
                >
                  加载更多
                </Button>
              ) : (
                <span className="text-sm text-[var(--color-text-muted)]">已加载全部</span>
              )}
            </div>
          </div>
        )}
      </div>
    </div>
  )
}

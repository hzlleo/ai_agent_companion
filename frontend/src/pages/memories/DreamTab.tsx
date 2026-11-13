import { useMemo, useState } from 'react'
import { MoonStar } from 'lucide-react'
import { Button } from '@/components/ui/Button'
import { Badge } from '@/components/ui/Badge'
import { Input } from '@/components/ui/Input'
import { Checkbox } from '@/components/ui/Checkbox'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card'
import { Spinner } from '@/components/ui/Spinner'
import { EmptyState } from '@/components/ui/EmptyState'
import { Alert, AlertDescription } from '@/components/ui/Alert'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/Table'
import { useToast } from '@/components/ui/useToast'
import { flattenCursorPages } from '@/api/pagination'
import { toApiError } from '@/api/client'
import { formatEpochMs } from '@/lib/datetime'
import { useDreamRunsList, useTriggerDream } from '@/hooks/useMemoriesApi'
import type { DreamJobConfig, DreamRunRecord } from '@/types/memory'
import { DREAM_STATUS_BADGE_VARIANTS, DREAM_STATUS_LABELS } from './memoryDisplay'
import { DreamRunReport } from './DreamRunReport'
import { DreamRunDetailDialog } from './DreamRunDetailDialog'

export interface DreamTabProps {
  /** 目标人格主键（undefined = 当前激活人格）。 */
  personaId: number | undefined
}

/** cfg 覆写表单态（字符串承载——空 = 不覆写，走 DreamJobConfig 默认）。 */
interface DreamConfigFormState {
  window_hours: string
  min_score: string
  max_candidates: string
  diary_enabled: boolean
}

const EMPTY_CONFIG_FORM: DreamConfigFormState = {
  window_hours: '',
  min_score: '',
  max_candidates: '',
  diary_enabled: true,
}

/**
 * cfg 覆写表单 → 请求体 cfg 片段（仅出现字段生效——与后端嵌套部分
 * 覆写语义对齐；全空 + 日记开关默认时返回 undefined = 全默认）。
 */
function formToConfigOverrides(
  form: DreamConfigFormState,
): Partial<DreamJobConfig> | undefined {
  const overrides: Partial<DreamJobConfig> = {}
  if (form.window_hours.trim() !== '') {
    overrides.window_hours = Number(form.window_hours)
  }
  if (form.min_score.trim() !== '') {
    overrides.min_score = Number(form.min_score)
  }
  if (form.max_candidates.trim() !== '') {
    overrides.max_candidates = Number(form.max_candidates)
  }
  overrides.diary_enabled = form.diary_enabled
  return overrides
}

/** cfg 覆写校验（与 DreamJobConfig 值域对齐）；返回错误文案或 null。 */
function validateConfigForm(form: DreamConfigFormState): string | null {
  if (form.window_hours.trim() !== '') {
    const parsed = Number(form.window_hours)
    if (!Number.isInteger(parsed) || parsed < 1 || parsed > 24 * 30) {
      return '候选窗口宽度须为 1-720（小时）的整数'
    }
  }
  if (form.min_score.trim() !== '') {
    const parsed = Number(form.min_score)
    if (!Number.isFinite(parsed) || parsed < 0) {
      return '晋级阈值须为不小于 0 的数值'
    }
  }
  if (form.max_candidates.trim() !== '') {
    const parsed = Number(form.max_candidates)
    if (!Number.isInteger(parsed) || parsed < 1 || parsed > 500) {
      return '候选事件上限须为 1-500 的整数'
    }
  }
  return null
}

/**
 * 梦境页签：手工触发面板（同步运行——loading 后呈现运行记录）+
 * 运行日志游标分页 + 运行详情对话框。
 *
 * 运行失败是业务结果而非请求失败（响应恒 200）：failed 终态经
 * warning toast + 报告内失败原因呈现。
 */
export function DreamTab({ personaId }: DreamTabProps) {
  const toast = useToast()
  const [configForm, setConfigForm] = useState<DreamConfigFormState>(EMPTY_CONFIG_FORM)
  const [lastRun, setLastRun] = useState<DreamRunRecord | null>(null)
  const [detailRunId, setDetailRunId] = useState<number | null>(null)

  const runsQuery = useDreamRunsList(personaId, 20)
  const triggerMutation = useTriggerDream()

  const runs = useMemo(() => flattenCursorPages(runsQuery.data?.pages ?? []), [runsQuery.data])

  const handleTrigger = (): void => {
    const configError = validateConfigForm(configForm)
    if (configError !== null) {
      toast.error(configError)
      return
    }
    triggerMutation.mutate(
      {
        persona_id: personaId,
        cfg: formToConfigOverrides(configForm),
      },
      {
        onSuccess: (run) => {
          setLastRun(run)
          if (run.status === 'failed') {
            toast.warning(`梦境整理失败：${run.error ?? '未知原因'}`)
          } else {
            toast.success(
              `梦境整理完成（候选 ${run.stats.candidates} / 晋升 ${run.stats.promoted} / 并入 ${run.stats.superseded}）`,
            )
          }
        },
        onError: (error) => {
          toast.error(toApiError(error).message)
        },
      },
    )
  }

  return (
    <div className="space-y-6">
      <Card>
        <CardHeader>
          <CardTitle>手工触发梦境整理</CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          <p className="text-sm text-[var(--color-text-secondary)]">
            同步运行（admin 触发语义）：触发后整条梦境链（打分 → LLM 合并 → 日记）内联执行，
            完成即返回运行记录。夜间自动调度属后续交付。
          </p>
          <div className="flex flex-wrap items-end gap-3">
            <div className="space-y-1.5">
              <label htmlFor="dream-window-hours" className="text-sm font-medium">
                候选窗口（小时）
              </label>
              <Input
                id="dream-window-hours"
                type="number"
                min={1}
                max={720}
                className="w-32"
                value={configForm.window_hours}
                onChange={(e) =>
                  setConfigForm((prev) => ({ ...prev, window_hours: e.target.value }))
                }
                placeholder="默认 24"
              />
            </div>
            <div className="space-y-1.5">
              <label htmlFor="dream-min-score" className="text-sm font-medium">
                晋级阈值
              </label>
              <Input
                id="dream-min-score"
                type="number"
                min={0}
                step="0.1"
                className="w-28"
                value={configForm.min_score}
                onChange={(e) => setConfigForm((prev) => ({ ...prev, min_score: e.target.value }))}
                placeholder="默认 1.0"
              />
            </div>
            <div className="space-y-1.5">
              <label htmlFor="dream-max-candidates" className="text-sm font-medium">
                候选上限
              </label>
              <Input
                id="dream-max-candidates"
                type="number"
                min={1}
                max={500}
                className="w-28"
                value={configForm.max_candidates}
                onChange={(e) =>
                  setConfigForm((prev) => ({ ...prev, max_candidates: e.target.value }))
                }
                placeholder="默认 50"
              />
            </div>
            <Checkbox
              checked={configForm.diary_enabled}
              onChange={(e) =>
                setConfigForm((prev) => ({ ...prev, diary_enabled: e.target.checked }))
              }
              label="生成梦境日记"
            />
            <Button onClick={handleTrigger} loading={triggerMutation.isPending}>
              <MoonStar className="mr-1 h-4 w-4" aria-hidden="true" />
              触发梦境整理
            </Button>
          </div>
          <p className="text-xs text-[var(--color-text-muted)]">
            覆写参数仅出现字段生效（其余走任务默认值）；候选窗口左闭右开
            [now − 窗口, now)。
          </p>
        </CardContent>
      </Card>

      {triggerMutation.isPending && (
        <div className="flex items-center justify-center gap-2 py-8 text-sm text-[var(--color-text-secondary)]">
          <Spinner size="md" />
          梦境整理同步运行中（合并 + 日记的内联 LLM 调用可能需要片刻）…
        </div>
      )}

      {lastRun !== null && !triggerMutation.isPending && (
        <Card>
          <CardHeader>
            <CardTitle>本次触发结果</CardTitle>
          </CardHeader>
          <CardContent>
            <DreamRunReport run={lastRun} personaId={personaId} />
          </CardContent>
        </Card>
      )}

      <div>
        <h3 className="mb-3 text-base font-semibold text-[var(--color-text)]">运行日志</h3>
        {runsQuery.isPending ? (
          <div className="flex items-center justify-center py-16">
            <Spinner size="lg" />
          </div>
        ) : runsQuery.isError ? (
          <Alert variant="destructive">
            <AlertDescription>
              <p>运行日志加载失败：{toApiError(runsQuery.error).message}</p>
              <Button
                variant="outline"
                size="sm"
                className="mt-2"
                onClick={() => {
                  void runsQuery.refetch()
                }}
              >
                重试
              </Button>
            </AlertDescription>
          </Alert>
        ) : runs.length === 0 ? (
          <EmptyState
            title="暂无运行记录"
            description="还没有触发过梦境整理；可在上方面板手工触发一次。"
          />
        ) : (
          <div className="rounded-xl border border-[var(--color-border)]">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead className="w-16">ID</TableHead>
                  <TableHead className="w-20">状态</TableHead>
                  <TableHead className="w-44">开始时间</TableHead>
                  <TableHead className="w-44">结束时间</TableHead>
                  <TableHead>统计摘要</TableHead>
                  <TableHead className="w-20">操作</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {runs.map((run) => (
                  <TableRow key={run.id} data-testid={`dream-run-row-${run.id}`}>
                    <TableCell>{run.id}</TableCell>
                    <TableCell>
                      <Badge variant={DREAM_STATUS_BADGE_VARIANTS[run.status]}>
                        {DREAM_STATUS_LABELS[run.status]}
                      </Badge>
                    </TableCell>
                    <TableCell className="text-[var(--color-text-secondary)]">
                      {formatEpochMs(run.started_ms)}
                    </TableCell>
                    <TableCell className="text-[var(--color-text-secondary)]">
                      {run.finished_ms === null ? '—' : formatEpochMs(run.finished_ms)}
                    </TableCell>
                    <TableCell className="text-[var(--color-text-secondary)]">
                      候选 {run.stats.candidates} / 晋升 {run.stats.promoted} / 并入{' '}
                      {run.stats.superseded}
                    </TableCell>
                    <TableCell>
                      <Button
                        variant="outline"
                        size="sm"
                        onClick={() => setDetailRunId(run.id)}
                      >
                        详情
                      </Button>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
            <div className="flex items-center justify-center border-t border-[var(--color-border)] p-4">
              {runsQuery.hasNextPage ? (
                <Button
                  variant="outline"
                  loading={runsQuery.isFetchingNextPage}
                  onClick={() => {
                    void runsQuery.fetchNextPage()
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

      <DreamRunDetailDialog
        open={detailRunId !== null}
        onOpenChange={(open) => {
          if (!open) setDetailRunId(null)
        }}
        personaId={personaId}
        runId={detailRunId ?? 0}
      />
    </div>
  )
}

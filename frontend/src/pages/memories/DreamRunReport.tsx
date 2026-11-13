import { Badge } from '@/components/ui/Badge'
import { Spinner } from '@/components/ui/Spinner'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/Table'
import { toApiError } from '@/api/client'
import { formatEpochMs } from '@/lib/datetime'
import { useEpisodeDetail } from '@/hooks/useMemoriesApi'
import type { DreamRunRecord } from '@/types/memory'
import {
  DREAM_STATUS_BADGE_VARIANTS,
  DREAM_STATUS_LABELS,
  DREAM_ACTION_BADGE_VARIANTS,
  DREAM_ACTION_LABELS,
  CURATED_KIND_LABELS,
  episodePayloadSummary,
} from './memoryDisplay'

export interface DreamRunReportProps {
  /** 运行记录（触发响应或详情端点读取面）。 */
  run: DreamRunRecord
  /** 目标人格主键（日记事件回源用）。 */
  personaId: number | undefined
}

/** 统计项定义（顺序即展示顺序）。 */
const STATS_ITEMS: { key: keyof DreamRunRecord['stats']; label: string }[] = [
  { key: 'candidates', label: '可信候选' },
  { key: 'gated', label: '过门' },
  { key: 'promoted', label: '晋升（新起链）' },
  { key: 'superseded', label: '并入（推进既有链）' },
  { key: 'skipped_untrusted', label: 'untrusted 硬排除' },
  { key: 'failed', label: '提案丢弃' },
]

/** 日记事件面板：diary_episode_id 回源情景层（kind=dream 注记）。 */
function DiaryEpisodeNote({ personaId, episodeId }: { personaId: number | undefined; episodeId: number }) {
  const episodeQuery = useEpisodeDetail(personaId, episodeId)
  if (episodeQuery.isPending) {
    return (
      <div className="flex items-center gap-2 text-sm text-[var(--color-text-secondary)]">
        <Spinner size="sm" />
        正在加载日记事件…
      </div>
    )
  }
  if (episodeQuery.isError) {
    return (
      <p className="text-sm text-[var(--color-text-muted)]">
        日记事件 #{episodeId} 读取失败：{toApiError(episodeQuery.error).message}
      </p>
    )
  }
  const episode = episodeQuery.data
  return (
    <div className="rounded-md border border-[var(--color-border)] bg-[var(--color-surface-secondary)] p-3">
      <div className="mb-1 flex items-center gap-2 text-xs text-[var(--color-text-muted)]">
        <span>日记事件 #{episode.id}</span>
        <Badge variant="secondary">梦境产物</Badge>
        <span>{formatEpochMs(episode.ts_ms)}</span>
      </div>
      <p className="whitespace-pre-wrap text-sm text-[var(--color-text)]">
        {episodePayloadSummary(episode.kind, episode.content)}
      </p>
    </div>
  )
}

/**
 * 梦境运行报告（共享展示面）：状态 + 统计 + 逐事件决策表 + 日记事件
 * 回源。触发结果面板与运行详情对话框共用。
 */
export function DreamRunReport({ run, personaId }: DreamRunReportProps) {
  return (
    <div className="space-y-4" data-testid={`dream-run-report-${run.id}`}>
      <div className="flex flex-wrap items-center gap-3 text-sm text-[var(--color-text-secondary)]">
        <Badge variant={DREAM_STATUS_BADGE_VARIANTS[run.status]}>
          {DREAM_STATUS_LABELS[run.status]}
        </Badge>
        <span>运行 #{run.id}</span>
        <span>开始 {formatEpochMs(run.started_ms)}</span>
        <span>
          结束 {run.finished_ms === null ? '—' : formatEpochMs(run.finished_ms)}
        </span>
      </div>

      {run.status === 'failed' && run.error !== null && (
        <div
          role="alert"
          className="rounded-lg border border-[var(--color-destructive)] bg-[var(--color-destructive-light)] px-4 py-3 text-sm text-[var(--color-destructive)]"
        >
          失败原因：{run.error}
        </div>
      )}

      <div className="grid grid-cols-3 gap-2 lg:grid-cols-6">
        {STATS_ITEMS.map((item) => (
          <div
            key={item.key}
            className="rounded-md border border-[var(--color-border)] px-3 py-2 text-center"
          >
            <div className="text-lg font-semibold text-[var(--color-text)]">
              {run.stats[item.key]}
            </div>
            <div className="text-xs text-[var(--color-text-muted)]">{item.label}</div>
          </div>
        ))}
      </div>

      <div>
        <h4 className="mb-2 text-sm font-medium">逐事件决策</h4>
        {run.decisions.length === 0 ? (
          <p className="text-sm text-[var(--color-text-muted)]">本次运行没有候选事件决策。</p>
        ) : (
          <div className="rounded-xl border border-[var(--color-border)]">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead className="w-20">事件</TableHead>
                  <TableHead className="w-24">策展类型</TableHead>
                  <TableHead className="w-36">主题键</TableHead>
                  <TableHead className="w-20">决策</TableHead>
                  <TableHead>理由</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {run.decisions.map((decision) => (
                  <TableRow
                    key={decision.episode_id}
                    data-testid={`dream-decision-${decision.episode_id}`}
                  >
                    <TableCell>#{decision.episode_id}</TableCell>
                    <TableCell>
                      {decision.kind === null ? (
                        <span className="text-[var(--color-text-muted)]">—</span>
                      ) : (
                        CURATED_KIND_LABELS[decision.kind]
                      )}
                    </TableCell>
                    <TableCell>
                      {decision.topic === '' ? (
                        <span className="text-[var(--color-text-muted)]">—</span>
                      ) : (
                        decision.topic
                      )}
                    </TableCell>
                    <TableCell>
                      <Badge variant={DREAM_ACTION_BADGE_VARIANTS[decision.action]}>
                        {DREAM_ACTION_LABELS[decision.action]}
                      </Badge>
                    </TableCell>
                    <TableCell className="text-[var(--color-text-secondary)]">
                      {decision.reason}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>
        )}
      </div>

      <div>
        <h4 className="mb-2 text-sm font-medium">梦境日记</h4>
        {run.diary_episode_id === null ? (
          <p className="text-sm text-[var(--color-text-muted)]">
            本次运行未生成日记事件（开关关闭或生成失败）。
          </p>
        ) : (
          <DiaryEpisodeNote personaId={personaId} episodeId={run.diary_episode_id} />
        )}
      </div>
    </div>
  )
}

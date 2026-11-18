import { useState } from 'react'
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/Dialog'
import { Button } from '@/components/ui/Button'
import { Badge } from '@/components/ui/Badge'
import { Spinner } from '@/components/ui/Spinner'
import { toApiError } from '@/api/client'
import { formatEpochMs } from '@/lib/datetime'
import {
  useRegenerateVariants,
  useSelectVariant,
  useSessionDetail,
} from '@/hooks/useMemoriesApi'
import type { ChatMessage, ChatMessageRole } from '@/types/memory'

export interface SessionDetailDialogProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  /** 目标人格主键（undefined = 当前激活人格）。 */
  personaId: number | undefined
  /** 会话主键（0 = 不拉取）。 */
  sessionId: number
}

/** 情绪维度 → 中文展示名（与后端 emotion 域枚举一致）。 */
const EMOTION_LABELS: Record<string, string> = {
  joy: '愉悦',
  affection: '亲昵',
  shyness: '羞怯',
  excitement: '兴奋',
  calm: '平静',
  sadness: '低落',
  anxiety: '不安',
  annoyance: '微恼',
}

/** 消息角色 → 中文标签 + 徽章变体。 */
const ROLE_LABELS: Record<ChatMessageRole, { label: string; variant: 'info' | 'success' | 'secondary' }> = {
  user: { label: '用户', variant: 'info' },
  assistant: { label: '人格', variant: 'success' },
  system: { label: '系统', variant: 'secondary' },
}

/** 变体轮播态（消息主键 + 当前候选下标）。 */
interface VariantCarouselState {
  messageId: number
  index: number
}

/** 单条消息的变体轮播（候选内容 + 心声 + 导航 / 选中动作）。 */
function VariantCarousel({
  message,
  index,
  onIndexChange,
  onSelect,
  selecting,
}: {
  message: ChatMessage
  index: number
  onIndexChange: (index: number) => void
  onSelect: (variantId: number) => void
  selecting: boolean
}) {
  const variants = message.variants
  const variant = variants[index]
  if (variant === undefined) {
    return null
  }
  return (
    <div
      data-testid={`session-variants-${message.seq}`}
      className="mt-2 rounded-md border border-dashed border-[var(--color-border)] bg-[var(--color-surface-hover)] p-2"
    >
      <div className="flex flex-wrap items-center gap-2 text-xs text-[var(--color-text-muted)]">
        <Badge variant="info">候选</Badge>
        <span data-testid={`session-variant-counter-${message.seq}`}>
          {index + 1}/{variants.length}
        </span>
        <Button
          type="button"
          variant="ghost"
          size="sm"
          aria-label="上一条变体"
          disabled={index <= 0}
          onClick={() => onIndexChange(index - 1)}
        >
          ←
        </Button>
        <Button
          type="button"
          variant="ghost"
          size="sm"
          aria-label="下一条变体"
          disabled={index >= variants.length - 1}
          onClick={() => onIndexChange(index + 1)}
        >
          →
        </Button>
        <Button
          type="button"
          variant="outline"
          size="sm"
          loading={selecting}
          onClick={() => onSelect(variant.id)}
        >
          选中此条
        </Button>
      </div>
      <p
        data-testid={`session-variant-content-${message.seq}`}
        className="mt-2 whitespace-pre-wrap text-sm text-[var(--color-text)]"
      >
        {variant.content}
      </p>
      {variant.meta?.thought ? (
        <p className="mt-1 whitespace-pre-wrap text-xs italic text-[var(--color-text-muted)]">
          心声:{variant.meta.thought}
        </p>
      ) : null}
    </div>
  )
}

/**
 * 会话详情对话框：摘要头 + 正典消息列表（seq 升序）+ assistant 回合的
 * 变体重生成 / 轮播 / 选定（swipes 操作面）。
 */
export function SessionDetailDialog({
  open,
  onOpenChange,
  personaId,
  sessionId,
}: SessionDetailDialogProps) {
  const detailQuery = useSessionDetail(personaId, open ? sessionId : 0, true)
  const regenerateMutation = useRegenerateVariants()
  const selectMutation = useSelectVariant()
  const [regeneratingId, setRegeneratingId] = useState<number | null>(null)
  const [carousel, setCarousel] = useState<VariantCarouselState | null>(null)
  const noTarget = sessionId <= 0

  const regenerate = (messageId: number) => {
    setRegeneratingId(messageId)
    regenerateMutation.mutate(
      { personaId, sessionId, messageId, payload: { count: 4 } },
      {
        onSuccess: () => {
          setRegeneratingId(null)
          setCarousel({ messageId, index: 0 })
        },
        onError: () => setRegeneratingId(null),
      },
    )
  }

  const select = (messageId: number, variantId: number) => {
    selectMutation.mutate({ personaId, sessionId, messageId, payload: { variant_id: variantId } })
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange} size="lg">
      <DialogHeader>
        <DialogTitle>会话详情</DialogTitle>
        <DialogClose onClose={() => onOpenChange(false)} />
      </DialogHeader>
      <DialogContent>
        {noTarget ? (
          <p className="py-4 text-sm text-[var(--color-text-muted)]">未选择会话。</p>
        ) : detailQuery.isPending ? (
          <div className="flex items-center justify-center py-12">
            <Spinner size="lg" />
          </div>
        ) : detailQuery.isError ? (
          <p className="py-4 text-sm text-[var(--color-destructive)]" role="alert">
            会话详情加载失败：{toApiError(detailQuery.error).message}
          </p>
        ) : (
          <div className="space-y-4">
            <div className="flex flex-wrap items-center gap-3 text-sm text-[var(--color-text-secondary)]">
              <span className="font-medium text-[var(--color-text)]">{detailQuery.data.title}</span>
              <Badge variant="secondary">会话 #{detailQuery.data.id}</Badge>
              <Badge variant="secondary">{detailQuery.data.message_count} 条消息</Badge>
              <span>创建于 {formatEpochMs(detailQuery.data.created_at)}</span>
            </div>
            {regenerateMutation.isError ? (
              <p className="text-sm text-[var(--color-destructive)]" role="alert">
                变体生成失败：{toApiError(regenerateMutation.error).message}
              </p>
            ) : null}
            {selectMutation.isError ? (
              <p className="text-sm text-[var(--color-destructive)]" role="alert">
                选定失败：{toApiError(selectMutation.error).message}
              </p>
            ) : null}
            <ol className="space-y-2">
              {detailQuery.data.messages.map((message) => {
                const role = ROLE_LABELS[message.role]
                const isCarouselOpen = carousel?.messageId === message.id
                return (
                  <li
                    key={message.id}
                    data-testid={`session-message-${message.seq}`}
                    className="rounded-md border border-[var(--color-border)] p-3"
                  >
                    <div className="mb-1 flex items-center gap-2">
                      <Badge variant={role.variant}>{role.label}</Badge>
                      <span className="text-xs text-[var(--color-text-muted)]">
                        #{message.seq} · {formatEpochMs(message.ts_ms)}
                      </span>
                      {message.role === 'assistant' ? (
                        <Button
                          type="button"
                          variant="ghost"
                          size="sm"
                          loading={regeneratingId === message.id}
                          onClick={() => regenerate(message.id)}
                        >
                          重生成变体
                        </Button>
                      ) : null}
                    </div>
                    <p className="whitespace-pre-wrap text-sm text-[var(--color-text)]">
                      {message.content}
                    </p>
                    {message.meta?.thought ? (
                      <p
                        data-testid={`session-thought-${message.seq}`}
                        className="mt-2 whitespace-pre-wrap text-xs italic text-[var(--color-text-muted)]"
                      >
                        心声:{message.meta.thought}
                      </p>
                    ) : null}
                    {message.meta?.emotion_snapshot &&
                    Object.keys(message.meta.emotion_snapshot).length > 0 ? (
                      <div
                        data-testid={`session-emotion-${message.seq}`}
                        className="mt-1 flex flex-wrap gap-1"
                      >
                        {Object.entries(message.meta.emotion_snapshot).map(([dimension, value]) => (
                          <Badge key={dimension} variant="secondary">
                            {EMOTION_LABELS[dimension] ?? dimension} {value.toFixed(2)}
                          </Badge>
                        ))}
                      </div>
                    ) : null}
                    {isCarouselOpen ? (
                      <VariantCarousel
                        message={message}
                        index={Math.min(carousel.index, Math.max(message.variants.length - 1, 0))}
                        onIndexChange={(index) =>
                          setCarousel({ messageId: message.id, index })
                        }
                        onSelect={(variantId) => select(message.id, variantId)}
                        selecting={selectMutation.isPending}
                      />
                    ) : null}
                  </li>
                )
              })}
            </ol>
          </div>
        )}
      </DialogContent>
      <DialogFooter>
        <Button onClick={() => onOpenChange(false)}>知道了</Button>
      </DialogFooter>
    </Dialog>
  )
}

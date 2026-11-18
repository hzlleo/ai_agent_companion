import { useEffect, useState } from 'react'
import { Badge } from '@/components/ui/Badge'
import { Button } from '@/components/ui/Button'
import { Checkbox } from '@/components/ui/Checkbox'
import { Spinner } from '@/components/ui/Spinner'
import { Textarea } from '@/components/ui/Textarea'
import { Alert, AlertDescription } from '@/components/ui/Alert'
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/Dialog'
import { useToast } from '@/components/ui/useToast'
import { toApiError } from '@/api/client'
import {
  useCoreMemoryBlocks,
  useCoreMemoryRevisions,
  useUpdateCoreMemoryBlock,
} from '@/hooks/useCoreMemoryApi'
import type {
  CoreBlockKey,
  CoreMemoryActor,
  CoreMemoryBlock,
  CoreMemorySource,
} from '@/types/persona'

export interface CoreMemoryPanelProps {
  /** 目标人格主键（面板在人格编辑作用域内——必填）。 */
  personaId: number
}

/** 块键 → 中文名称（与后端块表标签一致）。 */
const BLOCK_LABELS: Record<CoreBlockKey, string> = {
  self_identity: '自我认知',
  owner_profile: '关于对方',
  relationship_story: '我们的故事',
  open_notes: '备忘',
}

/** 固定渲染序（与后端块表顺序一致）。 */
const BLOCK_ORDER: CoreBlockKey[] = [
  'self_identity',
  'owner_profile',
  'relationship_story',
  'open_notes',
]

/** 正文长度上限（与后端契约对齐）。 */
const CORE_MEMORY_CONTENT_MAX_LENGTH = 2000

/** 写入者展示标签。 */
const ACTOR_LABELS: Record<CoreMemoryActor, string> = {
  owner: '主人',
  agent: '人格自编辑',
}

/** 修订来源展示标签。 */
const SOURCE_LABELS: Record<CoreMemorySource, string> = {
  admin: '管理面',
  chat: '对话内',
}

interface CoreMemoryCardProps {
  personaId: number
  block: CoreMemoryBlock
  onOpenRevisions: (block: CoreBlockKey) => void
}

/**
 * 单块编辑卡（正文 + 锁定开关 + 保存 + 修订入口）。
 *
 * 本地草稿以服务端版本为同步轴：块版本 / 正文 / 锁定态变化（保存后
 * 失效重取、他人格切换）时重置草稿；后台同版本重取不会覆盖正在输入的
 * 文本。
 */
function CoreMemoryCard({ personaId, block, onOpenRevisions }: CoreMemoryCardProps) {
  const toast = useToast()
  const updateMutation = useUpdateCoreMemoryBlock()
  const [content, setContent] = useState(block.content)
  const [locked, setLocked] = useState(block.locked)
  const label = BLOCK_LABELS[block.block]

  useEffect(() => {
    setContent(block.content)
    setLocked(block.locked)
  }, [block.content, block.locked, block.version])

  const handleSave = (): void => {
    if (content.length > CORE_MEMORY_CONTENT_MAX_LENGTH) {
      toast.error(`核心记忆正文不能超过 ${CORE_MEMORY_CONTENT_MAX_LENGTH} 个字符`)
      return
    }
    updateMutation.mutate(
      { personaId, block: block.block, payload: { content, locked } },
      {
        onSuccess: (updated) => {
          toast.success(`「${label}」已保存（v${updated.version}）`)
        },
        onError: (cause) => {
          toast.error(toApiError(cause).message)
        },
      },
    )
  }

  return (
    <div
      className="space-y-3 rounded-lg border border-[var(--color-border)] p-3"
      data-testid={`core-memory-card-${block.block}`}
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="text-sm font-medium text-[var(--color-text)]">{label}</p>
        <div className="flex items-center gap-2 text-xs text-[var(--color-text-muted)]">
          <Badge variant={locked ? 'success' : 'secondary'}>
            {locked ? '已锁定' : '可自编辑'}
          </Badge>
          <span>v{block.version}</span>
          <span>{ACTOR_LABELS[block.updated_by]}</span>
        </div>
      </div>
      <Textarea
        aria-label={`${label}正文`}
        rows={4}
        value={content}
        maxLength={CORE_MEMORY_CONTENT_MAX_LENGTH}
        onChange={(e) => setContent(e.target.value)}
        placeholder="写下一段稳定的自我叙述（空块不会进入对话注入）"
      />
      <div className="flex flex-wrap items-center justify-between gap-2">
        <Checkbox
          label={`锁定（${label}）`}
          checked={locked}
          onChange={(e) => setLocked(e.target.checked)}
        />
        <div className="flex items-center gap-1">
          <Button
            variant="ghost"
            size="sm"
            aria-label={`修订历史${label}`}
            onClick={() => onOpenRevisions(block.block)}
          >
            修订历史
          </Button>
          <Button
            size="sm"
            loading={updateMutation.isPending}
            aria-label={`保存${label}`}
            onClick={handleSave}
          >
            保存
          </Button>
        </div>
      </div>
    </div>
  )
}

/**
 * 自我认知面板（「自我认知」——四块自编辑核心记忆 + 修订历史抽屉）。
 *
 * 语义（文档化）：核心记忆是人格可按对话自编辑的长期自我模型，固定四块；
 * 锁定后人格的对话工具不再能改写该块，只能由主人在这里编辑或解锁；每次
 * 内容变更都会留下一条 append-only 修订（谁在什么场景改的、改成了什么），
 * 面板抽屉按最新在前展示。群聊中非主人的发言不会触发自编辑（后端工具面
 * 按发送者来源分级裁剪）。
 */
export function CoreMemoryPanel({ personaId }: CoreMemoryPanelProps) {
  const listQuery = useCoreMemoryBlocks(personaId)
  const [revisionBlock, setRevisionBlock] = useState<CoreBlockKey | null>(null)
  const revisionsQuery = useCoreMemoryRevisions(
    personaId,
    revisionBlock,
    revisionBlock !== null,
  )
  const blocks = listQuery.data ?? []
  const orderedBlocks = BLOCK_ORDER.map((key) => blocks.find((block) => block.block === key)).filter(
    (block): block is CoreMemoryBlock => block !== undefined,
  )
  const revisionLabel = revisionBlock === null ? '' : BLOCK_LABELS[revisionBlock]

  return (
    <div className="space-y-4">
      <div className="rounded-md border border-[var(--color-info)] bg-[var(--color-info-light)] px-3 py-2 text-xs text-[var(--color-info)]">
        核心记忆是 TA 可以自己维护的长期自我认知：四个块会以【自我认知】注入对话；锁定后
        TA 不能再改写该块，只有你能编辑或解锁；每次内容变更都会留下一条修订记录。
      </div>

      {listQuery.isPending ? (
        <div className="flex items-center justify-center py-8">
          <Spinner />
        </div>
      ) : listQuery.isError ? (
        <Alert variant="destructive">
          <AlertDescription>
            <p>核心记忆加载失败：{toApiError(listQuery.error).message}</p>
            <Button
              variant="outline"
              size="sm"
              className="mt-2"
              onClick={() => {
                void listQuery.refetch()
              }}
            >
              重试
            </Button>
          </AlertDescription>
        </Alert>
      ) : (
        <div className="grid gap-3 lg:grid-cols-2">
          {orderedBlocks.map((block) => (
            <CoreMemoryCard
              key={block.block}
              personaId={personaId}
              block={block}
              onOpenRevisions={setRevisionBlock}
            />
          ))}
        </div>
      )}

      <Dialog
        open={revisionBlock !== null}
        onOpenChange={(open) => {
          if (!open) setRevisionBlock(null)
        }}
        size="lg"
      >
        <DialogHeader>
          <DialogTitle>修订历史 · {revisionLabel}</DialogTitle>
        </DialogHeader>
        <DialogContent className="max-h-[60vh] space-y-2 overflow-y-auto">
          {revisionsQuery.isPending ? (
            <div className="flex items-center justify-center py-6">
              <Spinner />
            </div>
          ) : revisionsQuery.isError ? (
            <Alert variant="destructive">
              <AlertDescription>
                <p>修订历史加载失败：{toApiError(revisionsQuery.error).message}</p>
              </AlertDescription>
            </Alert>
          ) : revisionsQuery.data.length === 0 ? (
            <p className="py-6 text-center text-xs text-[var(--color-text-muted)]">
              该块还没有内容修订。
            </p>
          ) : (
            revisionsQuery.data.map((revision) => (
              <div
                key={revision.id}
                className="space-y-1 rounded-md border border-[var(--color-border)] p-2"
                data-testid={`core-memory-revision-${revision.id}`}
              >
                <p className="text-xs text-[var(--color-text-muted)]">
                  v{revision.version} · {ACTOR_LABELS[revision.provenance]} ·{' '}
                  {SOURCE_LABELS[revision.source]}
                </p>
                <p className="whitespace-pre-wrap break-words text-sm text-[var(--color-text)]">
                  {revision.content || '（已清空）'}
                </p>
              </div>
            ))
          )}
        </DialogContent>
        <DialogFooter>
          <Button variant="outline" onClick={() => setRevisionBlock(null)}>
            关闭
          </Button>
        </DialogFooter>
      </Dialog>
    </div>
  )
}

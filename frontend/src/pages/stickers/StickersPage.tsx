/**
 * 表情包库管理面板：上传 / 来源备注 / 标签 / VLM 标注 / 删除。
 *
 * 法律边界（与后端契约同口径）：上传必须填写「来源与授权备注」——面板在
 * 上传区与每张卡片上显著展示该字段，让操作者对素材授权状态一目了然。
 *
 * 标注语义：VLM 标注是可选增强（描述 + 标签）；失败会落在记录状态并可在
 * 卡片上重试。面板列表内联 base64 缩略图（一次请求完成预览，不再逐图拉
 * 文件端点）。
 */

import { useEffect, useMemo, useRef, useState, type ChangeEvent } from 'react'
import { ImagePlus, RefreshCw, Trash2 } from 'lucide-react'
import { PersonaPicker } from '@/components/PersonaPicker'
import { ConfirmDialog } from '@/components/ConfirmDialog'
import { Badge } from '@/components/ui/Badge'
import { Button } from '@/components/ui/Button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card'
import { EmptyState } from '@/components/ui/EmptyState'
import { Input } from '@/components/ui/Input'
import { Select } from '@/components/ui/Select'
import { Spinner } from '@/components/ui/Spinner'
import { useToast } from '@/components/ui/useToast'
import { toApiError } from '@/api/client'
import {
  STICKERS_DEFAULT_QUERY,
  useAnnotateSticker,
  useDeleteSticker,
  useStickers,
  useUpdateSticker,
  useUploadStickers,
} from '@/hooks/useStickersApi'
import { readFileAsBase64, resolveStickerContentType } from '@/lib/fileBase64'
import { parseTagsInput } from '@/lib/stickerTags'
import type {
  StickerAnnotationStatus,
  StickerBatchUploadRequest,
  StickerRecord,
} from '@/types/stickers'

/** 标注状态展示标签（与后端状态机同口径）。 */
const STATUS_LABELS: Record<StickerAnnotationStatus, string> = {
  pending: '待标注',
  done: '已标注',
  failed: '标注失败',
}

/** 标注状态徽章语义色。 */
const STATUS_VARIANTS: Record<StickerAnnotationStatus, 'secondary' | 'success' | 'destructive'> = {
  pending: 'secondary',
  done: 'success',
  failed: 'destructive',
}

/** 记录标签 → 编辑框文本。 */
function tagsToInput(tags: string[]): string {
  return tags.join('、')
}

interface StickerCardProps {
  personaId: number | undefined
  record: StickerRecord
  onDelete: (record: StickerRecord) => void
}

/**
 * 单条表情包卡片：缩略图 / 来源备注 / 标签编辑 / 标注 / 删除。
 *
 * 标签草稿以服务端值为同步轴：记录标签变化（保存成功 / 重新标注）时重置
 * 草稿，但不覆盖正在输入的文本（同一来源值重复渲染不重置）。
 */
function StickerCard({ personaId, record, onDelete }: StickerCardProps) {
  const toast = useToast()
  const [tagDraft, setTagDraft] = useState(tagsToInput(record.tags))
  const updateMutation = useUpdateSticker(personaId)
  const annotateMutation = useAnnotateSticker(personaId)
  const serverTags = tagsToInput(record.tags)

  useEffect(() => {
    setTagDraft(serverTags)
  }, [serverTags])

  const previewSrc = record.data_base64
    ? `data:${record.content_type};base64,${record.data_base64}`
    : null

  const saveTags = async () => {
    try {
      const updated = await updateMutation.mutateAsync({
        stickerId: record.id,
        payload: { tags: parseTagsInput(tagDraft), description: null, source_note: null },
      })
      toast.success(`标签已保存（${updated.tags.length} 个）`)
    } catch (error) {
      toast.error(toApiError(error).message)
    }
  }

  const annotate = async () => {
    try {
      const result = await annotateMutation.mutateAsync(record.id)
      if (result.ok) {
        toast.success(result.message)
      } else {
        toast.error(`标注失败：${result.message}`)
      }
    } catch (error) {
      toast.error(toApiError(error).message)
    }
  }

  return (
    <Card className="flex flex-col">
      <CardHeader className="flex-row items-center justify-between gap-2">
        <CardTitle className="text-sm">表情包 #{record.id}</CardTitle>
        <Badge variant={STATUS_VARIANTS[record.annotation_status]}>
          {STATUS_LABELS[record.annotation_status]}
        </Badge>
      </CardHeader>
      <CardContent className="flex flex-1 flex-col gap-3 text-sm">
        <div className="flex h-36 items-center justify-center rounded-md bg-[var(--color-surface-secondary)] p-2">
          {previewSrc ? (
            <img
              src={previewSrc}
              alt={record.description || '表情包预览'}
              className="max-h-32 max-w-full object-contain"
            />
          ) : (
            <span className="text-xs text-[var(--color-text-muted)]">无预览数据</span>
          )}
        </div>
        <p className="text-[var(--color-text-secondary)]">
          {record.description || '（尚未标注描述）'}
        </p>
        <p className="text-xs text-[var(--color-text-muted)]">
          来源与授权：{record.source_note}
        </p>
        {record.annotation_error ? (
          <p className="text-xs text-[var(--color-destructive)]">
            标注错误：{record.annotation_error}
          </p>
        ) : null}
        {record.annotation_model_entry ? (
          <p className="text-xs text-[var(--color-text-muted)]">
            标注模型：{record.annotation_model_entry}
          </p>
        ) : null}
        <div className="flex items-end gap-2">
          <label
            htmlFor={`sticker-tags-${record.id}`}
            className="flex-1 text-xs text-[var(--color-text-secondary)]"
          >
            标签（逗号分隔）
            <Input
              id={`sticker-tags-${record.id}`}
              aria-label={`表情包 ${record.id} 标签`}
              value={tagDraft}
              onChange={(event) => setTagDraft(event.target.value)}
              className="mt-1"
            />
          </label>
          <Button
            size="sm"
            variant="outline"
            loading={updateMutation.isPending}
            onClick={() => {
              void saveTags()
            }}
          >
            保存
          </Button>
        </div>
        <div className="flex items-center justify-between gap-2">
          <Button
            size="sm"
            variant="outline"
            loading={annotateMutation.isPending}
            onClick={() => {
              void annotate()
            }}
          >
            <RefreshCw className="mr-1 h-4 w-4" aria-hidden="true" />
            {record.annotation_status === 'failed' ? '重试标注' : '自动标注'}
          </Button>
          <Button
            size="sm"
            variant="ghost"
            aria-label={`删除表情包 ${record.id}`}
            onClick={() => onDelete(record)}
          >
            <Trash2 className="h-4 w-4" aria-hidden="true" />
          </Button>
        </div>
      </CardContent>
    </Card>
  )
}

export default function StickersPage() {
  const toast = useToast()
  const [personaValue, setPersonaValue] = useState('')
  const personaId = personaValue ? Number(personaValue) : undefined
  const fileInputRef = useRef<HTMLInputElement>(null)
  const [files, setFiles] = useState<File[]>([])
  const [sourceNote, setSourceNote] = useState('')
  const [tagsInput, setTagsInput] = useState('')
  const [statusFilter, setStatusFilter] = useState<'' | StickerAnnotationStatus>('')
  const [pendingDelete, setPendingDelete] = useState<StickerRecord | null>(null)

  const query = useMemo(
    () => ({
      ...STICKERS_DEFAULT_QUERY,
      annotation_status: statusFilter || null,
    }),
    [statusFilter],
  )
  const listQuery = useStickers(personaId, query)
  const uploadMutation = useUploadStickers(personaId)
  const deleteMutation = useDeleteSticker(personaId)

  const handleFiles = (event: ChangeEvent<HTMLInputElement>) => {
    setFiles(Array.from(event.target.files ?? []))
  }

  const submitUpload = async () => {
    if (files.length === 0) {
      toast.error('请先选择要上传的图片')
      return
    }
    if (!sourceNote.trim()) {
      toast.error('请填写素材来源与授权备注（法律边界提示义务）')
      return
    }
    const items: StickerBatchUploadRequest['items'] = []
    for (const file of files) {
      const contentType = resolveStickerContentType(file)
      if (!contentType) {
        toast.error(`不支持的图片类型：${file.name}`)
        return
      }
      try {
        items.push({
          image_base64: await readFileAsBase64(file),
          content_type: contentType,
          source_note: sourceNote.trim(),
          tags: parseTagsInput(tagsInput),
          description: '',
        })
      } catch (error) {
        toast.error(`读取文件失败：${file.name}（${toApiError(error).message}）`)
        return
      }
    }
    try {
      const result = await uploadMutation.mutateAsync({ items })
      const skipped = result.rejected.length
      if (result.created.length === 0) {
        toast.error(`没有新表情入库（跳过 ${skipped} 条：重复或校验失败）`)
      } else if (skipped > 0) {
        toast.success(`已上传 ${result.created.length} 张，跳过 ${skipped} 张`)
      } else {
        toast.success(`已上传 ${result.created.length} 张表情包`)
      }
      setFiles([])
      setTagsInput('')
      if (fileInputRef.current) {
        fileInputRef.current.value = ''
      }
    } catch (error) {
      toast.error(toApiError(error).message)
    }
  }

  const confirmDelete = async () => {
    if (pendingDelete === null) {
      return
    }
    try {
      const result = await deleteMutation.mutateAsync(pendingDelete.id)
      if (result.deleted_ids.length > 0) {
        toast.success(`已删除表情包 #${pendingDelete.id}`)
      } else {
        toast.error(`表情包 #${pendingDelete.id} 已不存在`)
      }
    } catch (error) {
      toast.error(toApiError(error).message)
    } finally {
      setPendingDelete(null)
    }
  }

  const records = listQuery.data ?? []

  return (
    <div className="space-y-4 p-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold text-[var(--color-text)]">表情包库</h1>
          <p className="mt-1 text-sm text-[var(--color-text-secondary)]">
            上传素材必须填写来源与授权备注；VLM 标注会补充描述与标签，失败可在卡片上重试。
          </p>
        </div>
        <div className="flex items-center gap-3">
          <PersonaPicker id="stickers-persona" value={personaValue} onChange={setPersonaValue} />
          <label className="text-sm text-[var(--color-text-secondary)]" htmlFor="sticker-status">
            标注状态
          </label>
          <Select
            id="sticker-status"
            className="w-32"
            value={statusFilter}
            onChange={(event) => setStatusFilter(event.target.value as '' | StickerAnnotationStatus)}
          >
            <option value="">全部</option>
            <option value="pending">待标注</option>
            <option value="done">已标注</option>
            <option value="failed">标注失败</option>
          </Select>
        </div>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>上传表情包</CardTitle>
        </CardHeader>
        <CardContent className="grid gap-3 md:grid-cols-3">
          <label className="text-sm text-[var(--color-text-secondary)]">
            图片文件（可多选）
            <input
              ref={fileInputRef}
              type="file"
              multiple
              accept="image/png,image/jpeg,image/webp,image/gif"
              aria-label="选择表情包图片"
              onChange={handleFiles}
              className="mt-1 block w-full text-sm"
            />
            {files.length > 0 ? (
              <span className="mt-1 block text-xs text-[var(--color-text-muted)]">
                已选择 {files.length} 个文件
              </span>
            ) : null}
          </label>
          <label
            htmlFor="sticker-source-note"
            className="text-sm text-[var(--color-text-secondary)]"
          >
            来源与授权备注（必填）
            <Input
              id="sticker-source-note"
              aria-label="来源与授权备注"
              value={sourceNote}
              onChange={(event) => setSourceNote(event.target.value)}
              placeholder="如：自绘 / 朋友授权使用 / 公开表情站 CC0"
              className="mt-1"
            />
          </label>
          <label
            htmlFor="sticker-initial-tags"
            className="text-sm text-[var(--color-text-secondary)]"
          >
            初始标签（逗号分隔，可选）
            <Input
              id="sticker-initial-tags"
              aria-label="初始标签"
              value={tagsInput}
              onChange={(event) => setTagsInput(event.target.value)}
              placeholder="开心, 猫猫"
              className="mt-1"
            />
          </label>
          <div className="md:col-span-3">
            <Button
              loading={uploadMutation.isPending}
              onClick={() => {
                void submitUpload()
              }}
            >
              <ImagePlus className="mr-1 h-4 w-4" aria-hidden="true" />
              上传到表情包库
            </Button>
          </div>
        </CardContent>
      </Card>

      {listQuery.isPending ? (
        <div className="flex items-center justify-center py-12">
          <Spinner />
        </div>
      ) : listQuery.isError ? (
        <div className="rounded-md border border-[var(--color-destructive)] bg-[var(--color-destructive-light)] px-4 py-3 text-sm text-[var(--color-destructive)]">
          表情包加载失败：{toApiError(listQuery.error).message}
        </div>
      ) : records.length === 0 ? (
        <EmptyState
          title="还没有表情包"
          description="上传第一张图片后，可以让 VLM 自动补全描述与标签。"
        />
      ) : (
        <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
          {records.map((record) => (
            <StickerCard
              key={record.id}
              personaId={personaId}
              record={record}
              onDelete={setPendingDelete}
            />
          ))}
        </div>
      )}

      <ConfirmDialog
        open={pendingDelete !== null}
        onOpenChange={(open) => {
          if (!open) {
            setPendingDelete(null)
          }
        }}
        title="删除表情包"
        description={
          pendingDelete
            ? `确认删除表情包 #${pendingDelete.id}（${pendingDelete.source_note}）？磁盘文件会一并清除。`
            : ''
        }
        confirmText="删除"
        destructive
        loading={deleteMutation.isPending}
        onConfirm={confirmDelete}
      />
    </div>
  )
}

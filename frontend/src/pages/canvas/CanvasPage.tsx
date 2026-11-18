/**
 * 画布产物管理面板：创建 / 预览 / 修订 / 导出 / 删除。
 *
 * 安全边界（与后端同口径）：预览在**不带 allow-scripts** 的沙箱 iframe 内
 * 渲染——模型生成的 HTML / SVG 里的脚本不执行，只保留结构与样式；Markdown
 * 按纯文本展示（面板不引入 Markdown 渲染依赖）。导出是原始产物字节——下载后
 * 由打开者决定如何执行。
 *
 * 产物规模上限（标题 120 字符 / 内容 262144 字符）由后端模型层强制；面板在
 * 输入框上同步提示，避免提交后才收到 400。
 */

import { useEffect, useMemo, useState } from 'react'
import { Download, Pencil, Plus, Trash2 } from 'lucide-react'
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
  CANVAS_DEFAULT_QUERY,
  useCanvasDocuments,
  useCreateCanvas,
  useDeleteCanvas,
  useExportCanvas,
  useUpdateCanvas,
} from '@/hooks/useCanvasApi'
import type { CanvasKind, CanvasRecord } from '@/types/canvas'

/** 产物类型展示标签（与后端值域同口径）。 */
const KIND_LABELS: Record<CanvasKind, string> = {
  html: 'HTML 卡片',
  svg: 'SVG 图形',
  markdown: 'Markdown 文稿',
}

/** 内容字符上限（与后端模型层一致——输入框同步提示）。 */
const CONTENT_MAX_CHARS = 262144

/** 把导出载荷交给浏览器下载（对象 URL 用后即释放）。 */
function saveBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = filename
  document.body.appendChild(anchor)
  anchor.click()
  anchor.remove()
  URL.revokeObjectURL(url)
}

/** 预览节点（沙箱 iframe / 纯文本——见模块 docstring 安全边界）。 */
function CanvasPreview({ record }: { record: CanvasRecord }) {
  if (record.kind === 'markdown') {
    return (
      <pre className="max-h-48 overflow-auto whitespace-pre-wrap break-words rounded-md bg-[var(--color-surface-secondary)] p-2 text-xs">
        {record.content}
      </pre>
    )
  }
  return (
    <iframe
      title={`画布预览 ${record.id}`}
      sandbox=""
      srcDoc={record.content}
      className="h-48 w-full rounded-md border border-[var(--color-border)] bg-white"
    />
  )
}

interface CanvasCardProps {
  personaId: number | undefined
  record: CanvasRecord
  onDelete: (record: CanvasRecord) => void
}

/** 单条画布卡片：预览 / 修订编辑 / 导出 / 删除。 */
function CanvasCard({ personaId, record, onDelete }: CanvasCardProps) {
  const toast = useToast()
  const [editing, setEditing] = useState(false)
  const [titleDraft, setTitleDraft] = useState(record.title)
  const [contentDraft, setContentDraft] = useState(record.content)
  const updateMutation = useUpdateCanvas(personaId)
  const exportMutation = useExportCanvas(personaId)

  useEffect(() => {
    if (!editing) {
      setTitleDraft(record.title)
      setContentDraft(record.content)
    }
  }, [editing, record.title, record.content])

  const save = async () => {
    try {
      const updated = await updateMutation.mutateAsync({
        canvasId: record.id,
        payload: {
          title: titleDraft.trim() === record.title ? null : titleDraft,
          content: contentDraft === record.content ? null : contentDraft,
        },
      })
      toast.success(`画布已保存（修订 ${updated.revision}）`)
      setEditing(false)
    } catch (error) {
      toast.error(toApiError(error).message)
    }
  }

  const exportFile = async () => {
    try {
      const file = await exportMutation.mutateAsync({ id: record.id, kind: record.kind })
      saveBlob(file.blob, file.filename)
      toast.success(`已导出 ${file.filename}`)
    } catch (error) {
      toast.error(toApiError(error).message)
    }
  }

  return (
    <Card className="flex flex-col">
      <CardHeader className="flex-row items-center justify-between gap-2">
        <CardTitle className="text-sm">
          {record.title}
          <span className="ml-2 text-xs text-[var(--color-text-muted)]">
            修订 {record.revision}
          </span>
        </CardTitle>
        <Badge variant="secondary">{KIND_LABELS[record.kind]}</Badge>
      </CardHeader>
      <CardContent className="flex flex-1 flex-col gap-3 text-sm">
        {editing ? (
          <div className="space-y-2">
            <label
              htmlFor={`canvas-title-${record.id}`}
              className="block text-xs text-[var(--color-text-secondary)]"
            >
              标题
              <Input
                id={`canvas-title-${record.id}`}
                aria-label={`画布 ${record.id} 标题`}
                value={titleDraft}
                onChange={(event) => setTitleDraft(event.target.value)}
                className="mt-1"
              />
            </label>
            <label
              htmlFor={`canvas-content-${record.id}`}
              className="block text-xs text-[var(--color-text-secondary)]"
            >
              内容
              <textarea
                id={`canvas-content-${record.id}`}
                aria-label={`画布 ${record.id} 内容`}
                value={contentDraft}
                onChange={(event) => setContentDraft(event.target.value)}
                className="mt-1 h-40 w-full rounded-md border border-[var(--color-border)] bg-[var(--color-surface)] p-2 font-mono text-xs"
              />
            </label>
            <div className="flex items-center gap-2">
              <Button
                size="sm"
                loading={updateMutation.isPending}
                onClick={() => {
                  void save()
                }}
              >
                保存
              </Button>
              <Button
                size="sm"
                variant="ghost"
                onClick={() => {
                  setEditing(false)
                }}
              >
                取消
              </Button>
            </div>
          </div>
        ) : (
          <CanvasPreview record={record} />
        )}
        <p className="text-xs text-[var(--color-text-muted)]">
          更新于 {new Date(record.updated_at_ms).toLocaleString()}
        </p>
        <div className="flex items-center justify-between gap-2">
          <div className="flex items-center gap-2">
            <Button
              size="sm"
              variant="outline"
              loading={exportMutation.isPending}
              onClick={() => {
                void exportFile()
              }}
            >
              <Download className="mr-1 h-4 w-4" aria-hidden="true" />
              导出
            </Button>
            <Button
              size="sm"
              variant="ghost"
              aria-label={`编辑画布 ${record.id}`}
              onClick={() => {
                setEditing(true)
              }}
            >
              <Pencil className="h-4 w-4" aria-hidden="true" />
            </Button>
          </div>
          <Button
            size="sm"
            variant="ghost"
            aria-label={`删除画布 ${record.id}`}
            onClick={() => onDelete(record)}
          >
            <Trash2 className="h-4 w-4" aria-hidden="true" />
          </Button>
        </div>
      </CardContent>
    </Card>
  )
}

export default function CanvasPage() {
  const toast = useToast()
  const [personaValue, setPersonaValue] = useState('')
  const personaId = personaValue ? Number(personaValue) : undefined
  const [kindFilter, setKindFilter] = useState<'' | CanvasKind>('')
  const [title, setTitle] = useState('')
  const [kind, setKind] = useState<CanvasKind>('html')
  const [content, setContent] = useState('')
  const [pendingDelete, setPendingDelete] = useState<CanvasRecord | null>(null)

  const query = useMemo(
    () => ({ ...CANVAS_DEFAULT_QUERY, kind: kindFilter || null }),
    [kindFilter],
  )
  const listQuery = useCanvasDocuments(personaId, query)
  const createMutation = useCreateCanvas(personaId)
  const deleteMutation = useDeleteCanvas(personaId)

  const submitCreate = async () => {
    if (!title.trim()) {
      toast.error('请填写画布标题')
      return
    }
    if (!content.trim()) {
      toast.error('请填写画布内容')
      return
    }
    try {
      const created = await createMutation.mutateAsync({ title, kind, content })
      toast.success(`画布已创建（修订 ${created.revision}）`)
      setTitle('')
      setContent('')
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
        toast.success(`已删除画布「${pendingDelete.title}」`)
      } else {
        toast.error(`画布 #${pendingDelete.id} 已不存在`)
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
          <h1 className="text-2xl font-semibold text-[var(--color-text)]">画布</h1>
          <p className="mt-1 text-sm text-[var(--color-text-secondary)]">
            渲染产物按人格隔离；预览在无脚本沙箱内渲染，导出为原始文件（HTML / SVG / Markdown）。
          </p>
        </div>
        <div className="flex items-center gap-3">
          <PersonaPicker id="canvas-persona" value={personaValue} onChange={setPersonaValue} />
          <label className="text-sm text-[var(--color-text-secondary)]" htmlFor="canvas-kind-filter">
            类型
          </label>
          <Select
            id="canvas-kind-filter"
            className="w-32"
            value={kindFilter}
            onChange={(event) => setKindFilter(event.target.value as '' | CanvasKind)}
          >
            <option value="">全部</option>
            <option value="html">HTML</option>
            <option value="svg">SVG</option>
            <option value="markdown">Markdown</option>
          </Select>
        </div>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>新建画布</CardTitle>
        </CardHeader>
        <CardContent className="grid gap-3 md:grid-cols-3">
          <label
            htmlFor="canvas-title"
            className="text-sm text-[var(--color-text-secondary)]"
          >
            标题（≤120 字符）
            <Input
              id="canvas-title"
              aria-label="画布标题"
              value={title}
              maxLength={120}
              onChange={(event) => setTitle(event.target.value)}
              className="mt-1"
            />
          </label>
          <label htmlFor="canvas-kind" className="text-sm text-[var(--color-text-secondary)]">
            产物类型
            <Select
              id="canvas-kind"
              className="mt-1"
              value={kind}
              onChange={(event) => setKind(event.target.value as CanvasKind)}
            >
              <option value="html">HTML</option>
              <option value="svg">SVG</option>
              <option value="markdown">Markdown</option>
            </Select>
          </label>
          <div className="flex items-end">
            <Button
              loading={createMutation.isPending}
              onClick={() => {
                void submitCreate()
              }}
            >
              <Plus className="mr-1 h-4 w-4" aria-hidden="true" />
              创建画布
            </Button>
          </div>
          <label
            htmlFor="canvas-content"
            className="md:col-span-3 text-sm text-[var(--color-text-secondary)]"
          >
            内容（HTML fragment / SVG 文档 / Markdown 文本；≤262144 字符）
            <textarea
              id="canvas-content"
              aria-label="画布内容"
              value={content}
              maxLength={CONTENT_MAX_CHARS}
              onChange={(event) => setContent(event.target.value)}
              placeholder={'<div style="padding:24px">今天很好</div>'}
              className="mt-1 h-40 w-full rounded-md border border-[var(--color-border)] bg-[var(--color-surface)] p-2 font-mono text-xs"
            />
          </label>
        </CardContent>
      </Card>

      {listQuery.isPending ? (
        <div className="flex items-center justify-center py-12">
          <Spinner />
        </div>
      ) : listQuery.isError ? (
        <div className="rounded-md border border-[var(--color-destructive)] bg-[var(--color-destructive-light)] px-4 py-3 text-sm text-[var(--color-destructive)]">
          画布加载失败：{toApiError(listQuery.error).message}
        </div>
      ) : records.length === 0 ? (
        <EmptyState
          title="还没有画布"
          description="创建第一份渲染产物后，可在此预览、修订与导出。"
        />
      ) : (
        <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
          {records.map((record) => (
            <CanvasCard
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
        title="删除画布"
        description={
          pendingDelete ? `确认删除画布「${pendingDelete.title}」？该操作不可撤销。` : ''
        }
        confirmText="删除"
        destructive
        loading={deleteMutation.isPending}
        onConfirm={confirmDelete}
      />
    </div>
  )
}

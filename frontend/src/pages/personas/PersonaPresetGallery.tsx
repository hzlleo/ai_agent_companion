import { useMemo, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { ArrowLeft, Sparkles } from 'lucide-react'
import { Button } from '@/components/ui/Button'
import { Badge } from '@/components/ui/Badge'
import { Spinner } from '@/components/ui/Spinner'
import { EmptyState } from '@/components/ui/EmptyState'
import { Alert, AlertDescription } from '@/components/ui/Alert'
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
} from '@/components/ui/Card'
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/Dialog'
import { Input } from '@/components/ui/Input'
import { useToast } from '@/components/ui/useToast'
import { toApiError } from '@/api/client'
import { useImportPersona, useTraitLibrary } from '@/hooks/usePersonasApi'
import type { TraitComponent, TraitComponentCategory, TraitPreset } from '@/types/persona'
import { PersonaImportConflictDialog } from './PersonaImportConflictDialog'

/** 组件分类 → 中文标签（画廊芯片 title 提示用）。 */
const CATEGORY_LABELS: Record<TraitComponentCategory, string> = {
  temperament: '温性',
  speech: '言语风格',
  quirks: '习惯癖性',
  values: '价值取向',
  sociality: '社交风格',
  humor: '幽默感',
}

/** 待生成的预设（生成对话框目标）。 */
type GenerateTarget = TraitPreset | null

/** 名称冲突重问目标（预设 + 原期望名——策略重试上下文）。 */
interface ConflictTarget {
  preset: TraitPreset
  desiredName: string
}

export interface GeneratePresetDialogProps {
  /** 对话框开闭（调用方条件挂载——关闭即卸载，输入态自然复位）。 */
  open: boolean
  /** 关闭请求（Radix 关闭面恒传 false——统一按关闭处理）。 */
  onOpenChange: (open: boolean) => void
  /** 生成目标预设（条件挂载保证非空）。 */
  preset: TraitPreset
  /** 提交（期望名——缺省策略 skip）。 */
  onSubmit: (desiredName: string) => void
  loading: boolean
}

/** 预设生成对话框：期望名输入（预填预设中文名）→ 提交导入。 */
function GeneratePresetDialog({
  open,
  onOpenChange,
  preset,
  onSubmit,
  loading,
}: GeneratePresetDialogProps) {
  const [name, setName] = useState(preset.name_zh)
  const [nameError, setNameError] = useState<string | null>(null)

  const handleConfirm = (): void => {
    const trimmed = name.trim()
    if (trimmed.length < 1 || trimmed.length > 128) {
      setNameError('期望名必填，且长度为 1-128 个字符')
      return
    }
    setNameError(null)
    onSubmit(trimmed)
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogHeader>
        <DialogTitle>生成人格</DialogTitle>
        <DialogClose onClose={() => onOpenChange(false)} />
      </DialogHeader>
      <DialogContent>
        <div className="space-y-4">
          <p className="text-sm text-[var(--color-text-secondary)]">
            将按预设「{preset.name_zh}」组合 {preset.components.length} 个特质组件，
            确定性生成 chara card v2 卡片并导入人格库（产物为停用状态，启用为显式后续动作）。
          </p>
          <div className="space-y-1.5">
            <label htmlFor="generate-desired-name" className="text-sm font-medium">
              期望名
            </label>
            <Input
              id="generate-desired-name"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="全局唯一，1-128 个字符"
              aria-invalid={nameError !== null}
              disabled={loading}
            />
            {preset.base_card_overrides.name_suffix !== null && (
              <p className="text-xs text-[var(--color-text-muted)]">
                该预设声明了名字后缀「{preset.base_card_overrides.name_suffix}」，生成时会拼接在期望名之后。
              </p>
            )}
            {nameError !== null && (
              <p className="text-xs text-[var(--color-destructive)]" role="alert">
                {nameError}
              </p>
            )}
          </div>
        </div>
      </DialogContent>
      <DialogFooter>
        <Button variant="outline" onClick={() => onOpenChange(false)} disabled={loading}>
          取消
        </Button>
        <Button onClick={handleConfirm} loading={loading}>
          生成
        </Button>
      </DialogFooter>
    </Dialog>
  )
}

/**
 * 官方特质预设画廊：预设卡片网格（名称 / 标语 / 描述 / 组件芯片）+
 * 生成流（期望名输入 → 导入；名称冲突三选一重问；同卡幂等 toast）。
 *
 * 数据面：GET /personas/presets（预设 + 组件注册表——前端按 id 映射
 * 解析芯片）；导入经 POST /personas/import {preset_id, desired_name}。
 */
export default function PersonaPresetGallery() {
  const toast = useToast()
  const navigate = useNavigate()
  const libraryQuery = useTraitLibrary()
  const importMutation = useImportPersona()
  const [generateTarget, setGenerateTarget] = useState<GenerateTarget>(null)
  const [conflictTarget, setConflictTarget] = useState<ConflictTarget | null>(null)

  const componentMap = useMemo(() => {
    const map = new Map<string, TraitComponent>()
    for (const component of libraryQuery.data?.components ?? []) {
      map.set(component.id, component)
    }
    return map
  }, [libraryQuery.data])

  /** 提交导入（冲突重试时携带策略）。 */
  const runImport = (
    preset: TraitPreset,
    desiredName: string,
    strategy: 'skip' | 'rename' | 'overwrite' = 'skip'
  ): void => {
    importMutation.mutate(
      {
        preset_id: preset.id,
        card: null,
        conflict_strategy: strategy,
        desired_name: desiredName,
      },
      {
        onSuccess: (result) => {
          setGenerateTarget(null)
          setConflictTarget(null)
          if (result.imported) {
            toast.success(`已按预设「${preset.name_zh}」生成人格，请在列表中查看`)
            void navigate('/personas')
          } else if (result.reason === 'identical_content') {
            toast.info('检测到同卡已存在（内容相同，幂等跳过）')
          } else {
            // 名称冲突 → 三选一重问（跳过 / 改名 / 覆盖）。
            setConflictTarget({ preset, desiredName })
          }
        },
        onError: (error) => {
          setGenerateTarget(null)
          toast.error(toApiError(error).message)
        },
      },
    )
  }

  const presets = libraryQuery.data?.presets ?? []

  return (
    <div className="p-8">
      <div className="mb-6 flex items-center justify-between">
        <div className="flex items-center gap-3">
          <Link
            to="/personas"
            className="inline-flex h-10 items-center gap-1 rounded-md px-3 text-sm text-[var(--color-text-secondary)] transition-colors hover:bg-[var(--color-surface-hover)]"
          >
            <ArrowLeft className="h-4 w-4" aria-hidden="true" />
            返回列表
          </Link>
          <h1 className="text-2xl font-semibold text-[var(--color-text)]">预设画廊</h1>
        </div>
        <Link
          to="/personas/new"
          className="inline-flex h-10 items-center gap-1 rounded-md bg-[var(--color-primary)] px-4 text-sm font-medium text-white transition-colors hover:bg-[var(--color-primary-hover)]"
        >
          <Sparkles className="h-4 w-4" aria-hidden="true" />
          从空白创建
        </Link>
      </div>
      <p className="mb-6 max-w-3xl text-sm text-[var(--color-text-secondary)]">
        官方预设库以纯特质组合发行（无既有角色形象引用）；点击「生成」按期望名
        确定性组合成 chara card v2 卡片并导入人格库。完整角色卡请经列表页的
        「导入人格」上传 JSON 文件。
      </p>

      {libraryQuery.isPending ? (
        <div className="flex items-center justify-center py-16">
          <Spinner size="lg" />
        </div>
      ) : libraryQuery.isError ? (
        <Alert variant="destructive">
          <AlertDescription>
            <p>预设库加载失败：{toApiError(libraryQuery.error).message}</p>
            <Button
              variant="outline"
              size="sm"
              className="mt-2"
              onClick={() => {
                void libraryQuery.refetch()
              }}
            >
              重试
            </Button>
          </AlertDescription>
        </Alert>
      ) : presets.length === 0 ? (
        <EmptyState title="暂无官方预设" description="官方预设库为空（数据文件未随包发行）。" />
      ) : (
        <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-3">
          {presets.map((preset) => (
            <Card key={preset.id} data-testid={`preset-card-${preset.id}`}>
              <CardHeader>
                <CardTitle>{preset.name_zh}</CardTitle>
                <p className="text-sm text-[var(--color-text-secondary)]">{preset.tagline_zh}</p>
              </CardHeader>
              <CardContent className="space-y-4">
                <p className="text-sm text-[var(--color-text-secondary)]">{preset.description_zh}</p>
                <div className="flex flex-wrap gap-1.5" aria-label="特质组件">
                  {preset.components.map((componentId) => {
                    const component = componentMap.get(componentId)
                    if (component === undefined) {
                      return null
                    }
                    return (
                      <Badge
                        key={componentId}
                        variant="secondary"
                        title={`${CATEGORY_LABELS[component.category]}：${component.description_zh}`}
                        data-testid={`preset-chip-${preset.id}-${componentId}`}
                      >
                        {component.name_zh}
                      </Badge>
                    )
                  })}
                </div>
                <Button
                  className="w-full"
                  onClick={() => setGenerateTarget(preset)}
                  loading={
                    importMutation.isPending &&
                    generateTarget !== null &&
                    generateTarget.id === preset.id
                  }
                >
                  生成
                </Button>
              </CardContent>
            </Card>
          ))}
        </div>
      )}

      {generateTarget !== null && (
        <GeneratePresetDialog
          open
          onOpenChange={() => setGenerateTarget(null)}
          preset={generateTarget}
          onSubmit={(desiredName) => {
            runImport(generateTarget, desiredName)
          }}
          loading={importMutation.isPending}
        />
      )}

      {conflictTarget !== null && (
        <PersonaImportConflictDialog
          open
          onOpenChange={() => setConflictTarget(null)}
          targetName={conflictTarget.desiredName}
          loading={importMutation.isPending}
          onRename={(newName) => {
            runImport(conflictTarget.preset, newName, 'rename')
          }}
          onOverwrite={() => {
            runImport(conflictTarget.preset, conflictTarget.desiredName, 'overwrite')
          }}
          onSkip={() => {
            setConflictTarget(null)
            toast.info('已跳过（保留现状）')
          }}
        />
      )}
    </div>
  )
}

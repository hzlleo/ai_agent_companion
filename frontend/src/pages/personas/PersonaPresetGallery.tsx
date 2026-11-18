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
import { Select } from '@/components/ui/Select'
import { useToast } from '@/components/ui/useToast'
import { toApiError } from '@/api/client'
import { useImportPersona, useTraitLibrary } from '@/hooks/usePersonasApi'
import type {
  TraitComponent,
  TraitComponentCategory,
  TraitMaturity,
  TraitPreset,
} from '@/types/persona'
import { PersonaImportConflictDialog } from './PersonaImportConflictDialog'

/** 组件分类 → 中文标签（画廊芯片 title 提示用）。 */
const CATEGORY_LABELS: Record<TraitComponentCategory, string> = {
  temperament: '温性',
  speech: '言语风格',
  quirks: '习惯癖性',
  values: '价值取向',
  sociality: '社交风格',
  humor: '幽默感',
  verbal_system: '言语体系',
  emotion_expression: '情绪表达',
  initiative: '主动性',
  intimacy_style: '亲密风格',
  interaction_ritual: '互动仪礼',
  boundaries: '边界',
}

/** 成熟度 → 徽章文案与配色（安全 / 暧昧 / 亲密直白）。 */
const MATURITY_META: Record<
  TraitMaturity,
  { label: string; variant: 'success' | 'warning' | 'destructive' }
> = {
  safe: { label: '安全', variant: 'success' },
  suggestive: { label: '暧昧', variant: 'warning' },
  explicit: { label: '直白', variant: 'destructive' },
}

/** 亲密等级 → 允许参与组合的组件成熟度（与后端过滤映射一致）。 */
const ALLOWED_MATURITY: Record<number, TraitMaturity[]> = {
  0: ['safe'],
  1: ['safe'],
  2: ['safe', 'suggestive'],
  3: ['safe', 'suggestive', 'explicit'],
}

/** 亲密等级 → 面向用户的中文说明（生成对话框选项提示）。 */
const INTIMACY_LEVEL_HINTS: Record<number, string> = {
  0: '仅安全特质：日常陪伴为主，不含任何暧昧桥段。',
  1: '仅安全特质：温和靠近与克制表达，适合第一次见面。',
  2: '安全 + 暧昧：允许调情、心动与若即若离的描写。',
  3: '全部特质：包含拥抱、亲吻等亲密直白表达，仍保持自然克制。',
}

/** 生成对话框的亲密等级可选值（0-3）。 */
const INTIMACY_LEVELS = [0, 1, 2, 3] as const

/** 待生成的预设与所选亲密等级（生成对话框目标）。 */
interface GenerateTarget {
  preset: TraitPreset
  intimacyLevel: number
}

/** 名称冲突重问目标（预设 + 原期望名 + 亲密等级——策略重试上下文）。 */
interface ConflictTarget {
  preset: TraitPreset
  desiredName: string
  intimacyLevel: number | null
}

export interface GeneratePresetDialogProps {
  /** 对话框开闭（调用方条件挂载——关闭即卸载，输入态自然复位）。 */
  open: boolean
  /** 关闭请求（Radix 关闭面恒传 false——统一按关闭处理）。 */
  onOpenChange: (open: boolean) => void
  /** 生成目标预设（条件挂载保证非空）。 */
  preset: TraitPreset
  /** 组件注册表（按 id 解析成熟度，用于预览等级过滤后的组件数）。 */
  components: Map<string, TraitComponent>
  /** 提交（期望名 + 亲密等级——缺省策略 skip）。 */
  onSubmit: (desiredName: string, intimacyLevel: number) => void
  loading: boolean
}

/** 预设生成对话框：期望名输入（预填预设中文名）→ 提交导入。 */
function GeneratePresetDialog({
  open,
  onOpenChange,
  preset,
  components: componentMap,
  onSubmit,
  loading,
}: GeneratePresetDialogProps) {
  const [name, setName] = useState(preset.name_zh)
  const [nameError, setNameError] = useState<string | null>(null)
  const [intimacyLevel, setIntimacyLevel] = useState<number>(preset.intimacy_level)

  const handleConfirm = (): void => {
    const trimmed = name.trim()
    if (trimmed.length < 1 || trimmed.length > 128) {
      setNameError('期望名必填，且长度为 1-128 个字符')
      return
    }
    setNameError(null)
    onSubmit(trimmed, intimacyLevel)
  }

  const allowed = ALLOWED_MATURITY[intimacyLevel] ?? ['safe']
  const keptCount = preset.components.filter((componentId) => {
    const component = componentMap.get(componentId)
    return component !== undefined && allowed.includes(component.maturity)
  }).length
  const filteredCount = preset.components.length - keptCount

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
          <div className="space-y-1.5">
            <label htmlFor="generate-intimacy-level" className="text-sm font-medium">
              亲密等级
            </label>
            <Select
              id="generate-intimacy-level"
              value={String(intimacyLevel)}
              onChange={(e) => setIntimacyLevel(Number(e.target.value))}
              disabled={loading}
            >
              {INTIMACY_LEVELS.map((level) => (
                <option key={level} value={String(level)}>
                  {level} · {level === preset.intimacy_level ? '预设推荐' : '自定义'}
                </option>
              ))}
            </Select>
            <p
              className="text-xs text-[var(--color-text-muted)]"
              data-testid="generate-intimacy-hint"
            >
              {INTIMACY_LEVEL_HINTS[intimacyLevel]}
            </p>
            <p className="text-xs text-[var(--color-text-muted)]" data-testid="generate-intimacy-preview">
              本次将组合 {keptCount} / {preset.components.length} 个组件
              {filteredCount > 0
                ? `，${filteredCount} 个超出该等级上限的组件会被过滤`
                : '，无组件被过滤'}
              。
            </p>
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
 * 解析芯片与成熟度徽章）；导入经 POST /personas/import
 * {preset_id, desired_name, intimacy_level_override}。
 */
export default function PersonaPresetGallery() {
  const toast = useToast()
  const navigate = useNavigate()
  const libraryQuery = useTraitLibrary()
  const importMutation = useImportPersona()
  const [generateTarget, setGenerateTarget] = useState<GenerateTarget | null>(null)
  const [conflictTarget, setConflictTarget] = useState<ConflictTarget | null>(null)

  const componentMap = useMemo(() => {
    const map = new Map<string, TraitComponent>()
    for (const component of libraryQuery.data?.components ?? []) {
      map.set(component.id, component)
    }
    return map
  }, [libraryQuery.data])

  /** 提交导入（冲突重试时携带策略；亲密等级仅生成期生效）。 */
  const runImport = (
    preset: TraitPreset,
    desiredName: string,
    strategy: 'skip' | 'rename' | 'overwrite' = 'skip',
    intimacyLevel: number | null = null
  ): void => {
    importMutation.mutate(
      {
        preset_id: preset.id,
        card: null,
        conflict_strategy: strategy,
        desired_name: desiredName,
        intimacy_level_override: intimacyLevel,
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
            setConflictTarget({ preset, desiredName, intimacyLevel })
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
                <Badge
                  variant="info"
                  title={`预设推荐亲密等级上限 ${preset.intimacy_level}；生成时可覆盖`}
                  data-testid={`preset-intimacy-${preset.id}`}
                >
                  亲密等级上限 {preset.intimacy_level}
                </Badge>
              </CardHeader>
              <CardContent className="space-y-4">
                <p className="text-sm text-[var(--color-text-secondary)]">{preset.description_zh}</p>
                <div className="flex flex-wrap gap-1.5" aria-label="特质组件">
                  {preset.components.map((componentId) => {
                    const component = componentMap.get(componentId)
                    if (component === undefined) {
                      return null
                    }
                    const maturity = MATURITY_META[component.maturity]
                    return (
                      <Badge
                        key={componentId}
                        variant="secondary"
                        title={`${CATEGORY_LABELS[component.category]}（${maturity.label}）：${component.description_zh}`}
                        data-testid={`preset-chip-${preset.id}-${componentId}`}
                      >
                        {component.name_zh}
                        <Badge
                          variant={maturity.variant}
                          className="ml-1.5 px-1.5 py-0 text-[10px]"
                          data-testid={`preset-chip-${preset.id}-${componentId}-maturity`}
                        >
                          {maturity.label}
                        </Badge>
                      </Badge>
                    )
                  })}
                </div>
                <Button
                  className="w-full"
                  onClick={() => setGenerateTarget({ preset, intimacyLevel: preset.intimacy_level })}
                  loading={
                    importMutation.isPending &&
                    generateTarget !== null &&
                    generateTarget.preset.id === preset.id
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
          preset={generateTarget.preset}
          components={componentMap}
          onSubmit={(desiredName, intimacyLevel) => {
            runImport(generateTarget.preset, desiredName, 'skip', intimacyLevel)
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
            runImport(conflictTarget.preset, newName, 'rename', conflictTarget.intimacyLevel)
          }}
          onOverwrite={() => {
            runImport(
              conflictTarget.preset,
              conflictTarget.desiredName,
              'overwrite',
              conflictTarget.intimacyLevel
            )
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

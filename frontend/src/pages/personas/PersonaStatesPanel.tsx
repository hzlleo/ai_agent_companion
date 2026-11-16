import { useEffect, useState } from 'react'
import { Button } from '@/components/ui/Button'
import { Textarea } from '@/components/ui/Textarea'
import { Badge } from '@/components/ui/Badge'
import { Spinner } from '@/components/ui/Spinner'
import { useToast } from '@/components/ui/useToast'
import {
  findStateByLayer,
  usePersonaStates,
  useUpsertPersonaState,
} from '@/hooks/usePersonasApi'
import { toApiError } from '@/api/client'
import type { PersonaStateLayer } from '@/types/persona'

export interface PersonaStatesPanelProps {
  /** 人格主键。 */
  personaId: number
}

/** 状态层本地编辑态。 */
interface LayerEditorState {
  /** JSON 文本（编辑中）。 */
  text: string
  /** 是否已写入过（后端存在该层）。 */
  existed: boolean
}

/** 层标签。 */
const LAYER_LABELS: Record<PersonaStateLayer, string> = {
  M: 'M 层（中期：心情 / 关系评价）',
  S: 'S 层（短期：会话内即时状态）',
}

/**
 * 人格状态层面板：M/S 两层的查看 + JSON 整体替换写入。
 *
 * - 读取 GET /personas/{id}/states（仅已写层返回，未写层空编辑器起步）；
 * - 写入 PUT /personas/{id}/states/{layer}（state 整体替换语义）；
 * - JSON 必须为对象（非数组/标量），解析失败前置拦截。
 */
export function PersonaStatesPanel({ personaId }: PersonaStatesPanelProps) {
  const toast = useToast()
  const statesQuery = usePersonaStates(personaId)
  const upsertMutation = useUpsertPersonaState()
  const [editors, setEditors] = useState<Record<PersonaStateLayer, LayerEditorState>>({
    M: { text: '', existed: false },
    S: { text: '', existed: false },
  })

  // 读取结果落地为编辑态（仅首次加载；后续写成功经返回值刷新）。
  useEffect(() => {
    const states = statesQuery.data
    if (states === undefined) {
      return
    }
    setEditors({
      M: {
        text: JSON.stringify(findStateByLayer(states, 'M')?.state ?? {}, null, 2),
        existed: findStateByLayer(states, 'M') !== undefined,
      },
      S: {
        text: JSON.stringify(findStateByLayer(states, 'S')?.state ?? {}, null, 2),
        existed: findStateByLayer(states, 'S') !== undefined,
      },
    })
  }, [statesQuery.data])

  /** 解析 JSON 文本为状态对象；非法时返回错误文案。 */
  const parseStateJson = (text: string): { state: Record<string, unknown> } | { error: string } => {
    try {
      const parsed: unknown = JSON.parse(text)
      if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) {
        return { error: '状态必须为 JSON 对象（如 {"mood": "calm"}）' }
      }
      return { state: parsed as Record<string, unknown> }
    } catch {
      return { error: 'JSON 解析失败，请检查格式' }
    }
  }

  const handleSave = (layer: PersonaStateLayer): void => {
    const editor = editors[layer]
    const parsed = parseStateJson(editor.text)
    if ('error' in parsed) {
      toast.error(parsed.error)
      return
    }
    upsertMutation.mutate(
      { id: personaId, layer, payload: { state: parsed.state } },
      {
        onSuccess: (saved) => {
          toast.success(`${layer} 层状态已更新`)
          setEditors((prev) => ({
            ...prev,
            [layer]: { text: JSON.stringify(saved.state, null, 2), existed: true },
          }))
        },
        onError: (error) => {
          toast.error(toApiError(error).message)
        },
      },
    )
  }

  if (statesQuery.isPending) {
    return (
      <div className="flex items-center gap-2 py-4 text-sm text-[var(--color-text-secondary)]">
        <Spinner size="sm" />
        正在加载状态层…
      </div>
    )
  }

  if (statesQuery.isError) {
    return (
      <p className="py-4 text-sm text-[var(--color-destructive)]" role="alert">
        状态层加载失败：{toApiError(statesQuery.error).message}
      </p>
    )
  }

  return (
    <div className="space-y-4">
      {(['M', 'S'] as const).map((layer) => {
        const editor = editors[layer]
        return (
          <div key={layer} className="space-y-1.5">
            <div className="flex items-center gap-2">
              <label htmlFor={`state-layer-${layer}`} className="text-sm font-medium">
                {LAYER_LABELS[layer]}
              </label>
              {editor.existed ? (
                <Badge variant="info">已写入</Badge>
              ) : (
                <Badge variant="secondary">未写入</Badge>
              )}
            </div>
            <Textarea
              id={`state-layer-${layer}`}
              rows={5}
              className="font-mono text-xs"
              value={editor.text}
              onChange={(e) =>
                setEditors((prev) => ({ ...prev, [layer]: { ...prev[layer], text: e.target.value } }))
              }
              placeholder='状态 JSON 文档，如 {"mood": "calm", "affinity": 0.6}'
            />
            <div className="flex justify-end">
              <Button
                size="sm"
                variant="outline"
                loading={upsertMutation.isPending}
                onClick={() => handleSave(layer)}
              >
                保存 {layer} 层
              </Button>
            </div>
          </div>
        )
      })}
    </div>
  )
}

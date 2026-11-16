import { useMemo } from 'react'
import { cardFormToWire, type CharaCardFormState } from './personaFormModel'

export interface PersonaCardJsonPreviewProps {
  /** 卡片字段态（实时转换为 wire 形态后预览）。 */
  card: CharaCardFormState
}

/**
 * 卡片 JSON 预览：表单态实时收敛为 wire 卡片并以格式化 JSON 呈现。
 *
 * 预览与提交共用同一转换（cardFormToWire），所见即所发。
 */
export function PersonaCardJsonPreview({ card }: PersonaCardJsonPreviewProps) {
  const jsonText = useMemo(() => JSON.stringify(cardFormToWire(card), null, 2), [card])
  return (
    <div className="space-y-1.5">
      <span className="text-sm font-medium">卡片 JSON 预览</span>
      <pre
        data-testid="card-json-preview"
        className="max-h-96 overflow-auto rounded-lg border border-[var(--color-border)] bg-[var(--color-surface-secondary)] p-4 font-mono text-xs leading-relaxed text-[var(--color-text)]"
      >
        {jsonText}
      </pre>
    </div>
  )
}

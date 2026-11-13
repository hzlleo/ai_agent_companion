import { forwardRef, useId, type InputHTMLAttributes, type ReactNode } from 'react'
import { cn } from '@/lib/utils'

/**
 * 受控复选框：原生 input + label 显式关联（htmlFor/id），保证无障碍语义。
 * label 支持 ReactNode，点击文本即可切换。
 */
export interface CheckboxProps extends Omit<InputHTMLAttributes<HTMLInputElement>, 'type'> {
  /** 复选框文本（支持任意 ReactNode）。 */
  label: ReactNode
}

export const Checkbox = forwardRef<HTMLInputElement, CheckboxProps>(
  ({ className, label, id: idProp, disabled, ...props }, ref) => {
    // 未显式传入 id 时自动生成，保证 label/htmlFor 关联唯一。
    const autoId = useId()
    const inputId = idProp ?? autoId

    return (
      <label
        htmlFor={inputId}
        className={cn(
          'flex w-fit cursor-pointer select-none items-center gap-2 rounded px-2 py-1 transition-colors hover:bg-[var(--color-surface-hover)]',
          disabled && 'cursor-not-allowed opacity-50 hover:bg-transparent',
        )}
      >
        <input
          id={inputId}
          type="checkbox"
          ref={ref}
          disabled={disabled}
          className={cn(
            'h-4 w-4 shrink-0 cursor-pointer accent-[var(--color-primary)]',
            'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-primary)] focus-visible:ring-offset-2',
            'disabled:cursor-not-allowed',
            className,
          )}
          {...props}
        />
        <span className="text-sm text-[var(--color-text)]">{label}</span>
      </label>
    )
  },
)
Checkbox.displayName = 'Checkbox'

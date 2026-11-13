import { forwardRef, type TextareaHTMLAttributes } from 'react'
import { cn } from '@/lib/utils'

export type TextareaProps = TextareaHTMLAttributes<HTMLTextAreaElement>

/** 多行文本域（默认不可拉伸，行高经 rows 控制）。 */
export const Textarea = forwardRef<HTMLTextAreaElement, TextareaProps>(
  ({ className, ...props }, ref) => {
    return (
      <textarea
        ref={ref}
        className={cn(
          'flex w-full rounded-md border border-[var(--color-border)] bg-[var(--color-surface)] px-3 py-2 text-sm',
          'placeholder:text-[var(--color-text-muted)]',
          'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-primary)] focus-visible:ring-offset-2',
          'disabled:cursor-not-allowed disabled:opacity-50 resize-none',
          className,
        )}
        {...props}
      />
    )
  },
)

Textarea.displayName = 'Textarea'

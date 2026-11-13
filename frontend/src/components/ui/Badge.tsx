import { cva, type VariantProps } from 'class-variance-authority'
import { type HTMLAttributes } from 'react'
import { cn } from '@/lib/utils'

/**
 * 徽章：浅色底 + 语义色文字（不用实心大色块）。
 * default=品牌、success/warning/destructive=功能状态、secondary/info=中性。
 */
const badgeVariants = cva(
  'inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-semibold transition-colors',
  {
    variants: {
      variant: {
        default: 'bg-[var(--color-primary-soft)] text-[var(--color-primary-hover)]',
        success: 'bg-[var(--color-success-light)] text-[var(--color-success)]',
        warning: 'bg-[var(--color-warning-light)] text-[var(--color-warning-text)]',
        destructive: 'bg-[var(--color-destructive-light)] text-[var(--color-destructive)]',
        secondary: 'bg-[var(--color-surface-secondary)] text-[var(--color-text-secondary)]',
        info: 'bg-[var(--color-info-light)] text-[var(--color-info)]',
      },
    },
    defaultVariants: {
      variant: 'default',
    },
  },
)

export interface BadgeProps
  extends HTMLAttributes<HTMLDivElement>,
    VariantProps<typeof badgeVariants> {}

/** 状态徽章（div 形态，非交互）。 */
export function Badge({ className, variant, ...props }: BadgeProps) {
  return <div className={cn(badgeVariants({ variant }), className)} {...props} />
}

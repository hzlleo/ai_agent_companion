import { cva, type VariantProps } from 'class-variance-authority'
import { type HTMLAttributes } from 'react'
import { cn } from '@/lib/utils'

const alertVariants = cva(
  'relative w-full rounded-lg border px-4 py-3 text-sm',
  {
    variants: {
      variant: {
        default: 'border-[var(--color-border)] bg-[var(--color-surface)] text-[var(--color-text)]',
        destructive:
          'border-[var(--color-destructive)] bg-[var(--color-destructive-light)] text-[var(--color-destructive)]',
        info: 'border-[var(--color-info)] bg-[var(--color-info-light)] text-[var(--color-info)]',
      },
    },
    defaultVariants: {
      variant: 'default',
    },
  },
)

export interface AlertProps
  extends HTMLAttributes<HTMLDivElement>,
    VariantProps<typeof alertVariants> {}

/** 内联提示条（非浮层）。 */
export function Alert({ className, variant, ...props }: AlertProps) {
  return <div role="alert" className={cn(alertVariants({ variant }), className)} {...props} />
}

/** 提示条内容区。 */
export function AlertDescription({ className, ...props }: HTMLAttributes<HTMLDivElement>) {
  return <div className={cn('text-sm [&_p]:leading-relaxed', className)} {...props} />
}

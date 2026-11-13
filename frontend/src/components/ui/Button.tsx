import { cva, type VariantProps } from 'class-variance-authority'
import { forwardRef, type ButtonHTMLAttributes } from 'react'
import { Loader2 } from 'lucide-react'
import { cn } from '@/lib/utils'

const buttonVariants = cva(
  'inline-flex items-center justify-center rounded-md font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-offset-2 disabled:opacity-50 disabled:pointer-events-none',
  {
    variants: {
      variant: {
        default: 'bg-[var(--color-primary)] text-white hover:bg-[var(--color-primary-hover)]',
        destructive:
          'bg-[var(--color-destructive)] text-white hover:bg-[var(--color-destructive-hover)]',
        outline:
          'border border-[var(--color-border)] bg-transparent hover:bg-[var(--color-surface-hover)]',
        ghost: 'hover:bg-[var(--color-surface-hover)]',
      },
      size: {
        sm: 'h-8 px-3 text-sm',
        form: 'h-[38px] px-4 text-sm',
        md: 'h-10 px-4',
        lg: 'h-12 px-6 text-lg',
      },
    },
    defaultVariants: {
      variant: 'default',
      size: 'md',
    },
  },
)

export interface ButtonProps
  extends ButtonHTMLAttributes<HTMLButtonElement>,
    VariantProps<typeof buttonVariants> {
  /** 加载中：在 children 前渲染旋转图标并强制禁用按钮。 */
  loading?: boolean
  loadingSpinnerSize?: string
}

/** 通用按钮：cva 变体（default/destructive/outline/ghost）+ loading 态。 */
export const Button = forwardRef<HTMLButtonElement, ButtonProps>(
  (
    {
      className,
      variant,
      size,
      loading = false,
      loadingSpinnerSize = 'h-4 w-4',
      disabled,
      children,
      ...props
    },
    ref,
  ) => {
    return (
      <button
        className={cn(buttonVariants({ variant, size, className }))}
        ref={ref}
        {...props}
        disabled={loading || disabled}
        aria-busy={loading || undefined}
      >
        {loading ? (
          <Loader2
            className={cn(children != null && children !== false && 'mr-2', 'animate-spin', loadingSpinnerSize)}
          />
        ) : null}
        {children}
      </button>
    )
  },
)

Button.displayName = 'Button'

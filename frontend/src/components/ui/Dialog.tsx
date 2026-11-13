import {
  type HTMLAttributes,
  type ReactNode,
  createContext,
  useContext,
  useEffect,
  useId,
  useMemo,
  useState,
} from 'react'
import * as DialogPrimitive from '@radix-ui/react-dialog'
import { X } from 'lucide-react'
import { cn } from '@/lib/utils'
import { Button } from './Button'

export interface DialogProps extends HTMLAttributes<HTMLDivElement> {
  open: boolean
  onOpenChange: (open: boolean) => void
  children: ReactNode
  /** 面板宽度档位；默认 md。 */
  size?: 'sm' | 'md' | 'lg' | 'xl'
}

// size → 面板 max-w-* 档位映射
const SIZE_WIDTH: Record<NonNullable<DialogProps['size']>, string> = {
  sm: 'max-w-sm',
  md: 'max-w-lg',
  lg: 'max-w-2xl',
  xl: 'max-w-4xl',
}

// DialogTitle → 面板的 id 桥接：Radix Title 无法脱离 Root 单独渲染，
// 故自建上下文把标题 id 注册给面板的 aria-labelledby。
interface TitleBridge {
  register: (id: string) => void
  unregister: (id: string) => void
}
const TitleIdContext = createContext<TitleBridge | null>(null)

/** 模态对话框（Radix Dialog 封装）：受控 open + 面板尺寸档位。 */
export function Dialog({ open, onOpenChange, children, className, size = 'md', ...props }: DialogProps) {
  const [titleId, setTitleId] = useState<string | undefined>(undefined)
  const bridge = useMemo<TitleBridge>(
    () => ({
      register: (id: string) => {
        setTitleId(id)
      },
      // 仅在仍是当前注册者时清除，避免误清后续标题。
      unregister: (id: string) => {
        setTitleId((cur) => (cur === id ? undefined : cur))
      },
    }),
    [],
  )

  // 滚动锁契约：打开时锁 body 滚动，关闭恢复。
  useEffect(() => {
    if (!open) return
    document.body.style.overflow = 'hidden'
    return () => {
      document.body.style.overflow = 'unset'
    }
  }, [open])

  return (
    <DialogPrimitive.Root open={open} onOpenChange={onOpenChange}>
      <DialogPrimitive.Portal>
        {/* 遮罩：点击关闭（可访问名「关闭对话框」）。 */}
        <DialogPrimitive.Overlay
          role="button"
          tabIndex={-1}
          aria-label="关闭对话框"
          className="fixed inset-0 z-50 bg-black/50"
        />
        <DialogPrimitive.Content
          aria-modal="true"
          aria-labelledby={titleId}
          // 显式声明无 Description，抑制 Radix 缺描述告警。
          aria-describedby={undefined}
          className={cn(
            'fixed left-1/2 top-1/2 z-50 w-full -translate-x-1/2 -translate-y-1/2 rounded-lg border border-[var(--color-border)] bg-[var(--color-surface)] p-6 shadow-lg',
            SIZE_WIDTH[size],
            className,
          )}
          {...props}
        >
          <TitleIdContext.Provider value={bridge}>{children}</TitleIdContext.Provider>
        </DialogPrimitive.Content>
      </DialogPrimitive.Portal>
    </DialogPrimitive.Root>
  )
}

/** 对话框头（标题区）。 */
export function DialogHeader({ className, ...props }: HTMLAttributes<HTMLDivElement>) {
  return (
    <div
      className={cn('flex flex-col space-y-1.5 text-left', className)}
      {...props}
    />
  )
}

/** 对话框标题（自动向面板注册 aria-labelledby）。 */
export function DialogTitle({ className, children, ...props }: HTMLAttributes<HTMLHeadingElement>) {
  const bridge = useContext(TitleIdContext)
  const id = useId()

  useEffect(() => {
    if (!bridge) return
    bridge.register(id)
    return () => {
      bridge.unregister(id)
    }
  }, [bridge, id])

  return (
    <h2
      id={id}
      className={cn('text-lg font-semibold leading-none tracking-tight', className)}
      {...props}
    >
      {children}
    </h2>
  )
}

/** 对话框内容区（统一限高 + 滚动）。 */
export function DialogContent({ className, ...props }: HTMLAttributes<HTMLDivElement>) {
  return (
    <div
      className={cn('mt-4 max-h-[calc(100vh-10rem)] overflow-y-auto', className)}
      {...props}
    />
  )
}

/** 对话框底部操作区。 */
export function DialogFooter({ className, ...props }: HTMLAttributes<HTMLDivElement>) {
  return (
    <div className={cn('mt-6 flex flex-row justify-end space-x-2', className)} {...props} />
  )
}

/** 右上角关闭按钮（脱离 Radix Close 语义，调用方自行控制开关）。 */
export function DialogClose({ onClose }: { onClose: () => void }) {
  return (
    <Button
      variant="ghost"
      size="sm"
      className="absolute right-4 top-4"
      onClick={onClose}
      aria-label="关闭对话框"
    >
      <X className="h-4 w-4" />
    </Button>
  )
}

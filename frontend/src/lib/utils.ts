import { clsx, type ClassValue } from 'clsx'
import { twMerge } from 'tailwind-merge'

/** Tailwind 类名合并工具：条件拼接 + 冲突去重。 */
export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs))
}

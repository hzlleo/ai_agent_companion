import { describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import { BatchDeleteResultDialog } from './BatchDeleteResultDialog'
import type { BatchDeleteResult } from '@/types/common'

/** 部分成功结果样例（1 激活守卫失败 + 1 成功 + 1 缺失失败）。 */
const partialResult: BatchDeleteResult = {
  total: 3,
  succeeded: 1,
  failed: 2,
  items: [
    { id: 1, success: false, code: 'CONFLICT', message: '当前激活人格禁止删除，请先停用' },
    { id: 2, success: true, code: null, message: '已删除' },
    { id: 3, success: false, code: 'NOT_FOUND', message: '人格不存在' },
  ],
}

describe('BatchDeleteResultDialog', () => {
  it('渲染汇总（总数/成功/失败）', () => {
    render(
      <BatchDeleteResultDialog open onOpenChange={() => undefined} result={partialResult} />,
    )
    expect(screen.getByText('批量删除结果')).toBeInTheDocument()
    expect(screen.getByText('提交总数：')).toBeInTheDocument()
    expect(screen.getByText('成功：')).toBeInTheDocument()
    expect(screen.getByText('失败：')).toBeInTheDocument()
    // 汇总数字与表体 ID 列并存——数字断言经父级文案锚定。
    const summary = screen.getByText(/提交总数：/)
    expect(summary.textContent).toContain('3')
  })

  it('逐项呈现：失败原因 + 错误码 / 成功已删除', () => {
    render(
      <BatchDeleteResultDialog open onOpenChange={() => undefined} result={partialResult} />,
    )
    expect(screen.getByText('当前激活人格禁止删除，请先停用')).toBeInTheDocument()
    expect(screen.getByText('（CONFLICT）')).toBeInTheDocument()
    expect(screen.getByText('人格不存在')).toBeInTheDocument()
    expect(screen.getByText('（NOT_FOUND）')).toBeInTheDocument()
    expect(screen.getAllByText('已删除').length).toBeGreaterThanOrEqual(1)
    expect(screen.getAllByText('失败').length).toBe(2)
  })

  it('result=null 时不渲染明细', () => {
    render(<BatchDeleteResultDialog open onOpenChange={() => undefined} result={null} />)
    expect(screen.getByRole('dialog')).toBeInTheDocument()
    expect(screen.queryByText('提交总数：')).not.toBeInTheDocument()
  })

  it('关闭路径：知道了与右上角关闭均触发 onOpenChange(false)', () => {
    const onOpenChange = vi.fn()
    render(
      <BatchDeleteResultDialog open onOpenChange={onOpenChange} result={partialResult} />,
    )
    fireEvent.click(screen.getByRole('button', { name: '知道了' }))
    expect(onOpenChange).toHaveBeenCalledWith(false)
    fireEvent.click(screen.getByRole('button', { name: '关闭对话框' }))
    expect(onOpenChange).toHaveBeenCalledTimes(2)
  })
})

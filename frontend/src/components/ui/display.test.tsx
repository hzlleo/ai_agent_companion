import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import { Badge } from './Badge'
import { Card, CardContent, CardHeader, CardTitle } from './Card'
import { Alert, AlertDescription } from './Alert'
import { Spinner } from './Spinner'
import { EmptyState } from './EmptyState'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from './Table'

describe('Badge', () => {
  it('success 变体渲染文本', () => {
    render(<Badge variant="success">启用</Badge>)
    const badge = screen.getByText('启用')
    expect(badge.className).toContain('rounded-full')
  })
})

describe('Card', () => {
  it('标题与内容渲染', () => {
    render(
      <Card>
        <CardHeader>
          <CardTitle>基础信息</CardTitle>
        </CardHeader>
        <CardContent>内容区</CardContent>
      </Card>,
    )
    expect(screen.getByText('基础信息')).toBeInTheDocument()
    expect(screen.getByText('内容区')).toBeInTheDocument()
  })
})

describe('Alert', () => {
  it('destructive 变体 + role=alert', () => {
    render(
      <Alert variant="destructive">
        <AlertDescription>加载失败</AlertDescription>
      </Alert>,
    )
    expect(screen.getByRole('alert')).toHaveTextContent('加载失败')
  })
})

describe('Spinner', () => {
  it('渲染加载态可访问名', () => {
    render(<Spinner />)
    expect(screen.getByRole('status')).toHaveAttribute('aria-label', '加载中')
  })
})

describe('EmptyState', () => {
  it('标题 + 描述 + 操作', () => {
    render(
      <EmptyState
        title="暂无人格"
        description="点击新建创建第一个"
        action={<button type="button">新建</button>}
      />,
    )
    expect(screen.getByText('暂无人格')).toBeInTheDocument()
    expect(screen.getByText('点击新建创建第一个')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '新建' })).toBeInTheDocument()
  })
})

describe('Table', () => {
  it('表头与单元格渲染', () => {
    render(
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>ID</TableHead>
            <TableHead>注册名</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          <TableRow>
            <TableCell>1</TableCell>
            <TableCell>companion-1</TableCell>
          </TableRow>
        </TableBody>
      </Table>,
    )
    expect(screen.getByRole('columnheader', { name: 'ID' })).toBeInTheDocument()
    expect(screen.getByRole('cell', { name: 'companion-1' })).toBeInTheDocument()
  })
})

import { useState, type SyntheticEvent } from 'react'
import { setAdminToken } from '@/api/auth'
import { Button } from '@/components/ui/Button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card'
import { Input } from '@/components/ui/Input'

/**
 * 管理面令牌门：任意请求收到 401（或侧栏「注销」）后承接的 owner token
 * 输入屏（替代整个应用壳挂载）。
 *
 * 保存后 ``window.location.reload()`` 整页重载（锁定决策）：token 经
 * api client 请求拦截器逐请求读取 localStorage，重载是最小可靠路径——
 * 免去手工重放在途查询 / 逐组件刷新 React Query 缓存；门自身无路由态，
 * 无深链需要保留。
 */
export function AdminTokenScreen() {
  const [token, setToken] = useState('')

  const handleSubmit = (event: SyntheticEvent<HTMLFormElement>) => {
    event.preventDefault()
    const trimmed = token.trim()
    if (!trimmed) {
      return
    }
    setAdminToken(trimmed)
    window.location.reload()
  }

  return (
    <div className="flex min-h-screen items-center justify-center bg-[var(--color-surface)] p-4">
      <Card className="w-full max-w-sm">
        <form onSubmit={handleSubmit} aria-label="管理访问令牌">
          <CardHeader>
            <CardTitle>管理访问令牌</CardTitle>
            <p className="text-sm text-[var(--color-text-muted)]">
              本管理台已启用访问鉴权。请输入服务端 ADMIN_AUTH_TOKEN 环境变量
              配置的 owner token（由运维设置），保存后自动进入。
            </p>
          </CardHeader>
          <CardContent className="space-y-4">
            <Input
              type="password"
              value={token}
              onChange={(event) => setToken(event.target.value)}
              placeholder="粘贴 owner token"
              aria-label="owner token"
            />
            <Button type="submit" disabled={!token.trim()} className="w-full">
              保存并进入
            </Button>
          </CardContent>
        </form>
      </Card>
    </div>
  )
}

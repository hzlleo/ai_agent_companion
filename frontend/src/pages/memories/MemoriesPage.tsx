import { useState } from 'react'
import { PersonaPicker } from '@/components/PersonaPicker'
import { personaIdFromPicker } from '@/hooks/useMemoriesApi'
import { cn } from '@/lib/utils'
import { EpisodesTab } from './EpisodesTab'
import { CuratedTab } from './CuratedTab'
import { SessionsTab } from './SessionsTab'
import { SearchTab } from './SearchTab'
import { DreamTab } from './DreamTab'

/** 记忆模块子页签（本工程约定：页签态经本地 state 承载，不入 URL）。 */
type MemoryTab = 'episodes' | 'curated' | 'sessions' | 'search' | 'dream'

/** 页签定义（顺序即展示顺序）。 */
const TABS: { key: MemoryTab; label: string }[] = [
  { key: 'episodes', label: '情景' },
  { key: 'curated', label: '策展' },
  { key: 'sessions', label: '会话' },
  { key: 'search', label: '检索' },
  { key: 'dream', label: '梦境' },
]

/**
 * 记忆管理页：三层记忆 + 检索 + 梦境的操作面。
 *
 * 结构决策（文档化）：五个子视图共享同一目标人格作用域（PersonaPicker
 * 顶层一次选择），故采用**页签（本地 state）而非子路由**——与人格
 * 模块「单页 + 对话框」的粒度一致，避免每页签重复解析人格参数；
 * 深链需求出现时再迁移为子路由（路由壳已预留 /memories 单路由）。
 */
export default function MemoriesPage() {
  const [personaValue, setPersonaValue] = useState('')
  const [activeTab, setActiveTab] = useState<MemoryTab>('episodes')
  const personaId = personaIdFromPicker(personaValue)

  return (
    <div className="p-8">
      <div className="mb-6 flex items-center justify-between">
        <h1 className="text-2xl font-semibold text-[var(--color-text)]">记忆管理</h1>
        <PersonaPicker id="memories-persona" value={personaValue} onChange={setPersonaValue} />
      </div>

      <div
        role="tablist"
        aria-label="记忆子模块"
        className="mb-6 flex items-center gap-1 border-b border-[var(--color-border)]"
      >
        {TABS.map((tab) => (
          <button
            key={tab.key}
            role="tab"
            type="button"
            aria-selected={activeTab === tab.key}
            onClick={() => setActiveTab(tab.key)}
            className={cn(
              '-mb-px rounded-t-md border border-b-0 px-4 py-2 text-sm font-medium transition-colors',
              activeTab === tab.key
                ? 'border-[var(--color-border)] bg-[var(--color-surface)] text-[var(--color-primary-hover)]'
                : 'border-transparent text-[var(--color-text-secondary)] hover:bg-[var(--color-surface-hover)]',
            )}
          >
            {tab.label}
          </button>
        ))}
      </div>

      {activeTab === 'episodes' && <EpisodesTab personaId={personaId} />}
      {activeTab === 'curated' && <CuratedTab personaId={personaId} />}
      {activeTab === 'sessions' && <SessionsTab personaId={personaId} />}
      {activeTab === 'search' && <SearchTab personaId={personaId} />}
      {activeTab === 'dream' && <DreamTab personaId={personaId} />}
    </div>
  )
}

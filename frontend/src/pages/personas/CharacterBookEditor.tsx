import { Plus, Trash2 } from 'lucide-react'
import { Button } from '@/components/ui/Button'
import { Input } from '@/components/ui/Input'
import { Textarea } from '@/components/ui/Textarea'
import { Select } from '@/components/ui/Select'
import { Checkbox } from '@/components/ui/Checkbox'
import {
  createEmptyEntry,
  type BookEntryFormState,
  type CharacterBookFormState,
} from './personaFormModel'

export interface CharacterBookEditorProps {
  /** 世界书字段态（null = 未启用，由父级开关控制）。 */
  book: CharacterBookFormState
  /** 字段变更回调（整体替换式）。 */
  onChange: (book: CharacterBookFormState) => void
  /** 校验错误集（personaFormModel 产出，按字段路径取用）。 */
  errors: Record<string, string>
}

/**
 * 世界书条目编辑器：书级元信息 + 条目行编辑。
 *
 * 条目字段面（与后端 chara card v2 契约一致）：keys / secondary_keys /
 * content / insertion_order / constant / case_sensitive / selective /
 * position + 高级触发五字段（depth 深度槽 / recursive 正文递归 /
 * include_group 互斥分组 / trigger_probability 概率门 / cooldown_turns
 * 冷却回合）。keywords 以逗号分隔编辑；selective 未开启时副触发键
 * 置灰（规格一致性：非选择性条目不得携带副键）；开启时副触发键
 * 必填（空副键永不触发——提交期校验，输入不阻断）。
 */
export function CharacterBookEditor({ book, onChange, errors }: CharacterBookEditorProps) {
  const updateEntry = (index: number, patch: Partial<BookEntryFormState>): void => {
    const entries = book.entries.map((entry, i) => (i === index ? { ...entry, ...patch } : entry))
    onChange({ ...book, entries })
  }

  const addEntry = (): void => {
    onChange({ ...book, entries: [...book.entries, createEmptyEntry()] })
  }

  const removeEntry = (index: number): void => {
    onChange({ ...book, entries: book.entries.filter((_, i) => i !== index) })
  }

  return (
    <div className="space-y-4">
      {/* 书级元信息 */}
      <div className="grid grid-cols-2 gap-3">
        <div className="space-y-1.5">
          <label htmlFor="book-name" className="text-sm font-medium">
            世界书名称
          </label>
          <Input
            id="book-name"
            value={book.name}
            onChange={(e) => onChange({ ...book, name: e.target.value })}
            placeholder="世界书名称"
          />
        </div>
        <div className="space-y-1.5">
          <label htmlFor="book-description" className="text-sm font-medium">
            世界书描述
          </label>
          <Input
            id="book-description"
            value={book.description}
            onChange={(e) => onChange({ ...book, description: e.target.value })}
            placeholder="世界书描述"
          />
        </div>
        <div className="space-y-1.5">
          <label htmlFor="book-scan-depth" className="text-sm font-medium">
            扫描深度（空 = 不限）
          </label>
          <Input
            id="book-scan-depth"
            value={book.scan_depth}
            onChange={(e) => onChange({ ...book, scan_depth: e.target.value })}
            placeholder="最近 N 条消息内检索"
            inputMode="numeric"
          />
          {errors['card.character_book.scan_depth'] !== undefined && (
            <p className="text-xs text-[var(--color-destructive)]" role="alert">
              {errors['card.character_book.scan_depth']}
            </p>
          )}
        </div>
        <div className="space-y-1.5">
          <label htmlFor="book-token-budget" className="text-sm font-medium">
            token 预算（空 = 不限）
          </label>
          <Input
            id="book-token-budget"
            value={book.token_budget}
            onChange={(e) => onChange({ ...book, token_budget: e.target.value })}
            placeholder="条目 token 预算"
            inputMode="numeric"
          />
          {errors['card.character_book.token_budget'] !== undefined && (
            <p className="text-xs text-[var(--color-destructive)]" role="alert">
              {errors['card.character_book.token_budget']}
            </p>
          )}
        </div>
      </div>
      <Checkbox
        checked={book.recursive_scanning}
        onChange={(e) => onChange({ ...book, recursive_scanning: e.target.checked })}
        label="递归扫描（条目正文中的命中词可再触发其他条目）"
      />

      {/* 条目列表 */}
      <div className="space-y-3">
        <div className="flex items-center justify-between">
          <span className="text-sm font-medium">条目列表（{book.entries.length}）</span>
          <Button type="button" variant="outline" size="sm" onClick={addEntry}>
            <Plus className="mr-1 h-4 w-4" aria-hidden="true" />
            添加条目
          </Button>
        </div>
        {book.entries.map((entry, index) => {
          const prefix = `card.character_book.entries.${index}`
          return (
            <fieldset
              key={index}
              className="space-y-3 rounded-lg border border-[var(--color-border)] p-4"
            >
              <legend className="px-1 text-xs font-medium text-[var(--color-text-secondary)]">
                条目 {index + 1}
              </legend>
              <div className="grid grid-cols-2 gap-3">
                <div className="space-y-1.5">
                  <label htmlFor={`entry-keys-${index}`} className="text-sm font-medium">
                    主触发关键词（逗号分隔）
                  </label>
                  <Input
                    id={`entry-keys-${index}`}
                    value={entry.keys}
                    onChange={(e) => updateEntry(index, { keys: e.target.value })}
                    placeholder="如：问候, 打招呼"
                  />
                </div>
                <div className="space-y-1.5">
                  <label htmlFor={`entry-secondary-keys-${index}`} className="text-sm font-medium">
                    {/* selective 开启时副键为必填（提交期校验——此处仅视觉提示）。 */}
                    {entry.selective
                      ? '副触发关键词（必填，逗号分隔）'
                      : '副触发关键词（需开启选择性触发）'}
                  </label>
                  <Input
                    id={`entry-secondary-keys-${index}`}
                    value={entry.secondary_keys}
                    onChange={(e) => updateEntry(index, { secondary_keys: e.target.value })}
                    placeholder="仅选择性触发时参与收敛"
                    disabled={!entry.selective}
                  />
                  {errors[`${prefix}.secondary_keys`] !== undefined && (
                    <p className="text-xs text-[var(--color-destructive)]" role="alert">
                      {errors[`${prefix}.secondary_keys`]}
                    </p>
                  )}
                </div>
              </div>
              <div className="space-y-1.5">
                <label htmlFor={`entry-content-${index}`} className="text-sm font-medium">
                  条目正文
                </label>
                <Textarea
                  id={`entry-content-${index}`}
                  rows={3}
                  value={entry.content}
                  onChange={(e) => updateEntry(index, { content: e.target.value })}
                  placeholder="插入提示词的设定文本"
                />
              </div>
              <div className="grid grid-cols-2 gap-3">
                <div className="space-y-1.5">
                  <label htmlFor={`entry-order-${index}`} className="text-sm font-medium">
                    插入顺序（0-9999）
                  </label>
                  <Input
                    id={`entry-order-${index}`}
                    value={entry.insertion_order}
                    onChange={(e) => updateEntry(index, { insertion_order: e.target.value })}
                    inputMode="numeric"
                  />
                  {errors[`${prefix}.insertion_order`] !== undefined && (
                    <p className="text-xs text-[var(--color-destructive)]" role="alert">
                      {errors[`${prefix}.insertion_order`]}
                    </p>
                  )}
                </div>
                <div className="space-y-1.5">
                  <label htmlFor={`entry-position-${index}`} className="text-sm font-medium">
                    插入位置
                  </label>
                  <Select
                    id={`entry-position-${index}`}
                    value={entry.position}
                    onChange={(e) =>
                      updateEntry(index, {
                        position: e.target.value === 'before_char' ? 'before_char' : 'after_char',
                      })
                    }
                  >
                    <option value="before_char">角色定义之前（before_char）</option>
                    <option value="after_char">角色定义之后（after_char）</option>
                  </Select>
                </div>
              </div>
              <div className="grid grid-cols-2 gap-3">
                <div className="space-y-1.5">
                  <label htmlFor={`entry-depth-${index}`} className="text-sm font-medium">
                    插入深度（0-20）
                  </label>
                  <Input
                    id={`entry-depth-${index}`}
                    value={entry.depth}
                    onChange={(e) => updateEntry(index, { depth: e.target.value })}
                    placeholder="默认 4"
                    inputMode="numeric"
                  />
                  <p className="text-xs text-[var(--color-text-secondary)]">
                    段内深度槽：0 = 最贴近对话末尾（段尾槽），值越大越靠段首；同深度按插入顺序
                  </p>
                  {errors[`${prefix}.depth`] !== undefined && (
                    <p className="text-xs text-[var(--color-destructive)]" role="alert">
                      {errors[`${prefix}.depth`]}
                    </p>
                  )}
                </div>
                <div className="space-y-1.5">
                  <label htmlFor={`entry-include-group-${index}`} className="text-sm font-medium">
                    互斥分组（可选）
                  </label>
                  <Input
                    id={`entry-include-group-${index}`}
                    value={entry.include_group}
                    onChange={(e) => updateEntry(index, { include_group: e.target.value })}
                    placeholder="如：问候变体"
                  />
                  <p className="text-xs text-[var(--color-text-secondary)]">
                    同分组命中的条目一次回复只注入优先级最高的一条
                  </p>
                  {errors[`${prefix}.include_group`] !== undefined && (
                    <p className="text-xs text-[var(--color-destructive)]" role="alert">
                      {errors[`${prefix}.include_group`]}
                    </p>
                  )}
                </div>
              </div>
              <div className="grid grid-cols-2 gap-3">
                <div className="space-y-1.5">
                  <label htmlFor={`entry-probability-${index}`} className="text-sm font-medium">
                    触发概率（0-1）
                  </label>
                  <Input
                    id={`entry-probability-${index}`}
                    value={entry.trigger_probability}
                    onChange={(e) => updateEntry(index, { trigger_probability: e.target.value })}
                    placeholder="默认 1（恒注入）"
                    inputMode="decimal"
                  />
                  <p className="text-xs text-[var(--color-text-secondary)]">
                    命中后按概率注入；1 = 恒注入，0 = 永不注入
                  </p>
                  {errors[`${prefix}.trigger_probability`] !== undefined && (
                    <p className="text-xs text-[var(--color-destructive)]" role="alert">
                      {errors[`${prefix}.trigger_probability`]}
                    </p>
                  )}
                </div>
                <div className="space-y-1.5">
                  <label htmlFor={`entry-cooldown-${index}`} className="text-sm font-medium">
                    冷却回合数（0-50）
                  </label>
                  <Input
                    id={`entry-cooldown-${index}`}
                    value={entry.cooldown_turns}
                    onChange={(e) => updateEntry(index, { cooldown_turns: e.target.value })}
                    placeholder="默认 0（不冷却）"
                    inputMode="numeric"
                  />
                  <p className="text-xs text-[var(--color-text-secondary)]">
                    注入后 N 个回复内不再注入；会话内生效，重启即复位
                  </p>
                  {errors[`${prefix}.cooldown_turns`] !== undefined && (
                    <p className="text-xs text-[var(--color-destructive)]" role="alert">
                      {errors[`${prefix}.cooldown_turns`]}
                    </p>
                  )}
                </div>
              </div>
              <div className="flex flex-wrap items-center justify-between gap-2">
                <div className="flex flex-wrap gap-1">
                  <Checkbox
                    checked={entry.constant}
                    onChange={(e) => updateEntry(index, { constant: e.target.checked })}
                    label="恒插入"
                  />
                  <Checkbox
                    checked={entry.case_sensitive}
                    onChange={(e) => updateEntry(index, { case_sensitive: e.target.checked })}
                    label="区分大小写"
                  />
                  <Checkbox
                    checked={entry.selective}
                    onChange={(e) => {
                      const selective = e.target.checked
                      updateEntry(index, {
                        selective,
                        // 关闭选择性触发时清空副键（规格一致性）。
                        secondary_keys: selective ? entry.secondary_keys : '',
                      })
                    }}
                    label="选择性触发"
                  />
                  <Checkbox
                    checked={entry.recursive}
                    onChange={(e) => updateEntry(index, { recursive: e.target.checked })}
                    label="正文递归触发"
                  />
                </div>
                {errors[`${prefix}.recursive`] !== undefined && (
                  <p className="text-xs text-[var(--color-destructive)]" role="alert">
                    {errors[`${prefix}.recursive`]}
                  </p>
                )}
                <Button
                  type="button"
                  variant="ghost"
                  size="sm"
                  onClick={() => removeEntry(index)}
                  aria-label={`删除条目 ${index + 1}`}
                  disabled={book.entries.length <= 1}
                >
                  <Trash2 className="h-4 w-4 text-[var(--color-destructive)]" aria-hidden="true" />
                  删除
                </Button>
              </div>
            </fieldset>
          )
        })}
      </div>
    </div>
  )
}

import { describe, expect, it } from 'vitest'
import {
  CURATED_KIND_LABELS,
  DREAM_ACTION_LABELS,
  EPISODE_PAYLOAD_TEMPLATES,
  PROVENANCE_LABELS,
  SEARCH_SCORE_DIRECTION_HINTS,
  episodePayloadSummary,
  truncateText,
} from './memoryDisplay'

describe('episodePayloadSummary', () => {
  it('chat 回合 → 用户/人格双栏摘要', () => {
    const summary = episodePayloadSummary('chat', {
      user_content: '今天好累',
      assistant_content: '辛苦了',
      session_id: null,
    })
    expect(summary).toBe('用户：今天好累 / 人格：辛苦了')
  })

  it('chat 空文本 → 占位提示', () => {
    const summary = episodePayloadSummary('chat', {
      user_content: '',
      assistant_content: '',
      session_id: 3,
    })
    expect(summary).toBe('用户：（无用户侧文本） / 人格：（无回复）')
  })

  it('duty 报告 → 标题 — 摘要', () => {
    const summary = episodePayloadSummary('duty', {
      title: '巡检报告',
      summary: '一切正常',
      details: {},
    })
    expect(summary).toBe('巡检报告 — 一切正常')
  })

  it('platform 动作 → 动作：摘要', () => {
    const summary = episodePayloadSummary('platform', {
      action: 'message.received',
      summary: '收到主人消息',
      metadata: {},
    })
    expect(summary).toBe('message.received：收到主人消息')
  })

  it('dream / system 注记 → 正文', () => {
    expect(episodePayloadSummary('dream', { text: '梦境叙事正文', metadata: {} })).toBe(
      '梦境叙事正文',
    )
    expect(episodePayloadSummary('system', { text: '', metadata: {} })).toBe('（无正文）')
  })

  it('超长正文 → 截断加省略号', () => {
    const summary = episodePayloadSummary('dream', {
      text: 'a'.repeat(60),
      metadata: {},
    })
    expect(summary).toBe(`${'a'.repeat(48)}…`)
    expect(summary.length).toBe(49)
  })

  it('kind 与载荷形态不符（异常 wire）→ JSON 串回落', () => {
    const summary = episodePayloadSummary('chat', { text: '注记形态', metadata: {} })
    expect(summary).toBe('{"text":"注记形态","metadata":{}}')
  })
})

describe('truncateText', () => {
  it('短文本原样返回；长文本截断', () => {
    expect(truncateText('短文本')).toBe('短文本')
    expect(truncateText('x'.repeat(100), 10)).toBe('xxxxxxxxxx…')
  })
})

describe('标签表与提示文案', () => {
  it('值域标签全覆盖', () => {
    expect(PROVENANCE_LABELS['owner']).toBe('主人')
    expect(CURATED_KIND_LABELS['user_note']).toBe('主人备忘')
    expect(DREAM_ACTION_LABELS['merged']).toBe('并入')
    expect(SEARCH_SCORE_DIRECTION_HINTS['keyword']).toContain('越大越相关')
    expect(SEARCH_SCORE_DIRECTION_HINTS['dense']).toContain('越小越相关')
  })

  it('载荷模板可解析为 JSON 对象且键与 kind 契约对齐', () => {
    for (const [kind, template] of Object.entries(EPISODE_PAYLOAD_TEMPLATES)) {
      const parsed: unknown = JSON.parse(template)
      expect(typeof parsed).toBe('object')
      if (kind === 'chat') {
        expect(parsed).toHaveProperty('user_content')
      }
      if (kind === 'duty') {
        expect(parsed).toHaveProperty('title')
      }
      if (kind === 'platform') {
        expect(parsed).toHaveProperty('action')
      }
      if (kind === 'dream' || kind === 'system') {
        expect(parsed).toHaveProperty('text')
      }
    }
  })
})

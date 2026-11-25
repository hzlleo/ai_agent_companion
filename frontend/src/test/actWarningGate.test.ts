import { describe, expect, it } from 'vitest'
import { ActWarningGate } from './actWarningGate'

describe('ActWarningGate', () => {
  it('尝试收尾：有告警抛出并清空，干净次轮不继承首轮告警', () => {
    const gate = new ActWarningGate()
    gate.beginAttempt()
    gate.record('Warning: An update to Probe was not wrapped in act(...).')
    expect(() => gate.finishAttempt('用例 A')).toThrow(
      /React act\(\.\.\.\) 告警 1 条（用例：用例 A）/,
    )
    gate.beginAttempt()
    expect(() => gate.finishAttempt('用例 A')).not.toThrow()
  })

  it('整轮兜底：尝试收尾后到达的残留告警按无归属判错（文件末条不漏）', () => {
    const gate = new ActWarningGate()
    gate.beginAttempt()
    gate.finishAttempt('末条用例')
    gate.record('late warning: not wrapped in act(...).')
    expect(() => gate.finishRun()).toThrow(
      /React act\(\.\.\.\) 残留告警 1 条（无归属：最后用例收尾 \/ afterAll）/,
    )
  })
})

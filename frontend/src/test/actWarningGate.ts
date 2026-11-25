/**
 * React act 告警门禁的可测逻辑（生命周期接入见 setup.ts）。
 *
 * 归属：`beginAttempt` 之后记录的告警归当前尝试；`finishAttempt` 在
 * `onTestFinished`（晚于 afterEach cleanup）排空宏任务后判定并清空，retry
 * 首轮告警不会带入干净次轮。`finishRun` 为 afterAll 兜底：尝试收尾后到达
 * 的残留（文件末条 / afterAll 期间）按无归属判错，不漏告警。
 */
export class ActWarningGate {
  private warnings: string[] = []

  /** 尝试开始：重置归属（retry 每轮独立）。 */
  beginAttempt(): void {
    this.warnings = []
  }

  /** 记录一条 act 告警（仅首行，避免堆栈噪声）。 */
  record(text: string): void {
    this.warnings.push(text.split('\n')[0] ?? text)
  }

  /** 尝试收尾：有告警则抛出并清空（次轮从干净归属开始）。 */
  finishAttempt(testName: string): void {
    this.reportOrClear(
      (samples) =>
        `React act(...) 告警 ${samples.length} 条（用例：${testName}）：${samples.join(' ｜ ')}`,
    )
  }

  /** 整轮收尾：尝试收尾后到达的残留按无归属判错。 */
  finishRun(): void {
    this.reportOrClear(
      (samples) =>
        `React act(...) 残留告警 ${samples.length} 条（无归属：最后用例收尾 / afterAll）：${samples.join(' ｜ ')}`,
    )
  }

  private reportOrClear(buildMessage: (samples: string[]) => string): void {
    if (this.warnings.length === 0) {
      return
    }
    const samples = this.warnings
    this.warnings = []
    throw new Error(buildMessage(samples))
  }
}

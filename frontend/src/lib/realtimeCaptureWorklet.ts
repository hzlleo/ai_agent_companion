/**
 * 实时语音采集 AudioWorklet 处理器（在音频渲染线程运行）。
 *
 * 职责（单一）：把输入采样按定长块（`CAPTURE_WORKLET_BLOCK_SAMPLES`）经
 * `port.postMessage` 回传主线程；降采样 / PCM16 成帧 / 上行全部留在主线程
 * （与既有 ScriptProcessor 路径同一套 `PcmFrameChunker` 语义）。
 *
 * 该文件由 `realtimeVoiceClient` 经 Vite `?worker&url` 模块化加载
 * （`audioWorklet.addModule`）；加载或建节点失败时主线程回退 ScriptProcessor。
 * 音频线程不在 jsdom 内运行，故由 vitest 覆盖率排除（逻辑核心
 * `takeFixedBlocks` 在 `realtimeAudio.ts` 内被单测覆盖）。
 */

import { CAPTURE_WORKLET_BLOCK_SAMPLES, CAPTURE_WORKLET_NAME, takeFixedBlocks } from './realtimeAudio'

declare abstract class AudioWorkletProcessor {
  readonly port: MessagePort
}
declare function registerProcessor(
  name: string,
  processorCtor: new () => AudioWorkletProcessor,
): void

/** 块累积采集处理器（满块即回传，余量留待下次）。 */
class RealtimeCaptureProcessor extends AudioWorkletProcessor {
  private rest: Float32Array = new Float32Array(0)

  /**
   * 音频渲染回调：累积并回传满块。
   *
   * @param inputs 输入通道采样（单声道第 0 通道）。
   * @returns 恒 true（处理器持续运行）。
   */
  process(inputs: Float32Array[][]): boolean {
    const channel = inputs[0]?.[0]
    if (channel !== undefined && channel.length > 0) {
      const { blocks, rest } = takeFixedBlocks(
        this.rest,
        channel,
        CAPTURE_WORKLET_BLOCK_SAMPLES,
      )
      this.rest = rest
      for (const block of blocks) {
        this.port.postMessage(block, [block.buffer])
      }
    }
    return true
  }
}

registerProcessor(CAPTURE_WORKLET_NAME, RealtimeCaptureProcessor)

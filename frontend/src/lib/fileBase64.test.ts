import { describe, expect, it } from 'vitest'
import {
  readFileAsBase64,
  resolveStickerContentType,
  STICKER_MIME_ALLOWLIST,
} from '@/lib/fileBase64'

describe('readFileAsBase64', () => {
  it('读取文件为纯 base64（不含 data URL 前缀）', async () => {
    const file = new File([new Uint8Array([1, 2, 3, 4])], 'cat.png', { type: 'image/png' })
    const encoded = await readFileAsBase64(file)
    expect(encoded).toBe(btoa(String.fromCharCode(1, 2, 3, 4)))
    expect(encoded.startsWith('data:')).toBe(false)
  })

  it('非字符串结果 / 无逗号结果按纯文本返回（防御分支）', async () => {
    const file = new File(['x'], 'x.png', { type: 'image/png' })
    const original = globalThis.FileReader
    class NullResultReader {
      result: string | null = null
      error: Error | null = null
      onload: (() => void) | null = null
      onerror: (() => void) | null = null
      readAsDataURL(): void {
        this.onload?.()
      }
    }
    class PlainResultReader {
      result = 'abc'
      error: Error | null = null
      onload: (() => void) | null = null
      onerror: (() => void) | null = null
      readAsDataURL(): void {
        this.onload?.()
      }
    }
    try {
      globalThis.FileReader = NullResultReader as unknown as typeof FileReader
      await expect(readFileAsBase64(file)).resolves.toBe('')
      globalThis.FileReader = PlainResultReader as unknown as typeof FileReader
      await expect(readFileAsBase64(file)).resolves.toBe('abc')
    } finally {
      globalThis.FileReader = original
    }
  })

  it('读取失败时 reject（FileReader 错误路径）', async () => {
    const file = new File(['x'], 'x.png', { type: 'image/png' })
    const original = globalThis.FileReader
    class FailingReader {
      result: string | null = null
      error: Error | null = new Error('读取失败')
      onload: (() => void) | null = null
      onerror: (() => void) | null = null
      readAsDataURL(): void {
        this.onerror?.()
      }
    }
    // 仅替换该用例的 FileReader（jsdom 运行期桩）。
    globalThis.FileReader = FailingReader as unknown as typeof FileReader
    try {
      await expect(readFileAsBase64(file)).rejects.toThrow('读取失败')
    } finally {
      globalThis.FileReader = original
    }
    class EmptyErrorReader {
      result: string | null = null
      error: Error | null = null
      onload: (() => void) | null = null
      onerror: (() => void) | null = null
      readAsDataURL(): void {
        this.onerror?.()
      }
    }
    globalThis.FileReader = EmptyErrorReader as unknown as typeof FileReader
    try {
      await expect(readFileAsBase64(file)).rejects.toThrow('读取文件失败')
    } finally {
      globalThis.FileReader = original
    }
  })
})

describe('resolveStickerContentType', () => {
  it('白名单 MIME 直用', () => {
    expect(resolveStickerContentType(new File(['x'], 'a.gif', { type: 'image/gif' }))).toBe(
      'image/gif',
    )
  })

  it('无 MIME 时按扩展名兜底（jpg / jpeg / webp / png）', () => {
    expect(resolveStickerContentType(new File(['x'], 'a.jpg'))).toBe('image/jpeg')
    expect(resolveStickerContentType(new File(['x'], 'a.JPEG'))).toBe('image/jpeg')
    expect(resolveStickerContentType(new File(['x'], 'a.webp'))).toBe('image/webp')
    expect(resolveStickerContentType(new File(['x'], 'a.png'))).toBe('image/png')
  })

  it('未知类型返回空串（调用方按不支持处理）', () => {
    expect(resolveStickerContentType(new File(['x'], 'a.bmp', { type: 'image/bmp' }))).toBe('')
    expect(resolveStickerContentType(new File(['x'], 'noext'))).toBe('')
  })

  it('白名单常量与后端同集', () => {
    expect([...STICKER_MIME_ALLOWLIST].sort()).toEqual([
      'image/gif',
      'image/jpeg',
      'image/png',
      'image/webp',
    ])
  })
})

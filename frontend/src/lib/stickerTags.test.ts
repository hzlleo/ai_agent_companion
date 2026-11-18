import { describe, expect, it } from 'vitest'
import { parseTagsInput } from '@/lib/stickerTags'

describe('parseTagsInput', () => {
  it('按逗号 / 中文逗号 / 空白切分并去重（保序）', () => {
    expect(parseTagsInput('开心, 猫猫，开心  挥手')).toEqual(['开心', '猫猫', '挥手'])
  })

  it('空白输入返回空数组', () => {
    expect(parseTagsInput('   ')).toEqual([])
    expect(parseTagsInput('')).toEqual([])
  })
})

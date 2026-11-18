/**
 * 表情包标签输入解析（管理面板 ↔ 后端标签数组的边界转换）。
 *
 * 面板以逗号 / 空白分隔的自由文本承载标签；本模块做切分、去空与去重，
 * 后端仍会按配置上限再次校验（非法标签以逐条拒收结局返回）。
 */

/**
 * 解析标签输入文本。
 *
 * @param value 用户输入（逗号 / 中文逗号 / 空白分隔）。
 * @returns 去空去重后的标签数组（保序）。
 */
export function parseTagsInput(value: string): string[] {
  const parts = value.split(/[,，\s]+/)
  const tags: string[] = []
  for (const part of parts) {
    const tag = part.trim()
    if (tag && !tags.includes(tag)) {
      tags.push(tag)
    }
  }
  return tags
}

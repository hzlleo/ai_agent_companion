/** 群聊多人格会话标题解析（管理台展示面）。 */
export interface GroupSessionLabel {
  /** 是否为群聊多人格人格会话（标题带 `:persona:<id>` 后缀）。 */
  isGroupLane: boolean
  /** 应答人格主键（解析失败为 null）。 */
  personaId: number | null
}

const GROUP_LANE_MARKER = ':persona:'

/**
 * 解析会话标题中的应答人格归属。
 *
 * 群聊多人格模式下每个应答人格拥有自己的持久化会话，标题形如
 * `channel:telegram:<chat_id>:persona:<persona_id>`；会话行的人格主键
 * 即该会话消息的应答归属（replied_by）。单人格会话标题不含该后缀。
 *
 * @param title 会话标题（ff_chat_sessions.title）。
 * @returns 群聊多人格标记与应答人格主键。
 */
export function parseGroupSessionTitle(title: string): GroupSessionLabel {
  const index = title.lastIndexOf(GROUP_LANE_MARKER)
  if (index < 0 || !title.startsWith('channel:')) {
    return { isGroupLane: false, personaId: null }
  }
  const suffix = title.slice(index + GROUP_LANE_MARKER.length)
  if (!/^\d+$/.test(suffix)) {
    return { isGroupLane: false, personaId: null }
  }
  return { isGroupLane: true, personaId: Number(suffix) }
}

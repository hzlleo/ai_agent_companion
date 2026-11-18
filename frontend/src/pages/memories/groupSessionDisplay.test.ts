import { parseGroupSessionTitle } from './groupSessionDisplay'

describe('parseGroupSessionTitle', () => {
  it('识别群聊多人格人格会话并取回人格主键', () => {
    expect(parseGroupSessionTitle('channel:telegram:10086:persona:7')).toEqual({
      isGroupLane: true,
      personaId: 7,
    })
  })

  it('普通单人格会话标题不标记群聊多人格', () => {
    expect(parseGroupSessionTitle('channel:telegram:10086')).toEqual({
      isGroupLane: false,
      personaId: null,
    })
  })

  it('非通道路径标题即使带后缀也不误判', () => {
    expect(parseGroupSessionTitle('manual:persona:3')).toEqual({
      isGroupLane: false,
      personaId: null,
    })
  })

  it('后缀非数字时不误判', () => {
    expect(parseGroupSessionTitle('channel:qq:group:1:persona:abc')).toEqual({
      isGroupLane: false,
      personaId: null,
    })
  })
})

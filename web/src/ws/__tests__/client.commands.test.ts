import { describe, it, expect, vi } from 'vitest'
import { SessionSocket } from '@/ws/client'

describe('SessionSocket commands', () => {
  it('emits commands and commandResult to listeners', () => {
    const s = new SessionSocket('/ws')
    const onCommands = vi.fn()
    const onResult = vi.fn()
    s.on('commands', onCommands)
    s.on('commandResult', onResult)
    // drive the private inbound handler
    ;(s as unknown as { _handleInbound: (m: unknown) => void })._handleInbound(
      { type: 'commands', sessionId: 's1', commands: [{ name: 'help', description: '', argumentHint: null, kind: 'client' }] })
    ;(s as unknown as { _handleInbound: (m: unknown) => void })._handleInbound(
      { type: 'commandResult', sessionId: 's1', ok: true, name: 'model', render: 'notice', data: { text: 'Model → sonnet' } })
    expect(onCommands).toHaveBeenCalledOnce()
    expect(onResult).toHaveBeenCalledWith(expect.objectContaining({ render: 'notice' }))
  })
})

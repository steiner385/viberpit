import { describe, it, expect, vi } from 'vitest'
import { SessionSocket } from './client'

describe('SessionSocket — two instances', () => {
  it('track independent active session ids and do not share attach frames', () => {
    const sends: string[][] = [[], []]
    const make = (i: number) => {
      const s = new SessionSocket() as unknown as {
        _send: (frame: unknown) => void
        attach: (id: string) => void
        activeSessionId: string | null
      }
      vi.spyOn(s as never, '_send' as never).mockImplementation(((f: unknown) => {
        sends[i].push((f as { sessionId: string }).sessionId)
      }) as never)
      return s
    }
    const a = make(0)
    const b = make(1)
    a.attach('route-1')
    b.attach('assistant-1')
    expect(a.activeSessionId).toBe('route-1')
    expect(b.activeSessionId).toBe('assistant-1')
    expect(sends[0]).toEqual(['route-1'])
    expect(sends[1]).toEqual(['assistant-1'])
  })
})

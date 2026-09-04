// @vitest-environment node
// web/src/ws/client.freshness.test.ts
import { describe, it, expect } from 'vitest'
import { QueryClient } from '@tanstack/react-query'
import { SessionSocket } from './client'
import { QK } from '@/api/hooks'
import type { Session } from '@/api/types'

function seed(qc: QueryClient, s: Partial<Session>) {
  qc.setQueryData<Session[]>(QK.sessions, [{ id: 's1', status: 'idle', updatedAt: '2020-01-01T00:00:00+00:00', accountName: 'a', ...s } as Session])
}
function makeSocket() {
  const qc = new QueryClient()
  const s = new SessionSocket('/ws') as unknown as { setQueryClient: (q: QueryClient) => void; _handleInbound: (m: unknown) => void }
  s.setQueryClient(qc)
  return { s, qc }
}
const row = (qc: QueryClient) => (qc.getQueryData<Session[]>(QK.sessions) ?? [])[0]

describe('SessionSocket freshness patches', () => {
  it('status patch writes the server updatedAt (not the client clock)', () => {
    const { s, qc } = makeSocket(); seed(qc, {})
    s._handleInbound({ type: 'status', sessionId: 's1', status: 'done', updatedAt: '2021-06-01T00:00:00+00:00' })
    expect(row(qc).status).toBe('done')
    expect(row(qc).updatedAt).toBe('2021-06-01T00:00:00+00:00')
  })

  it('status patch without updatedAt leaves the cached value untouched', () => {
    const { s, qc } = makeSocket(); seed(qc, { updatedAt: '2020-05-05T00:00:00+00:00' })
    s._handleInbound({ type: 'status', sessionId: 's1', status: 'in_progress' })
    expect(row(qc).status).toBe('in_progress')
    expect(row(qc).updatedAt).toBe('2020-05-05T00:00:00+00:00')
  })

  it('rotation patch does not change updatedAt', () => {
    const { s, qc } = makeSocket(); seed(qc, { updatedAt: '2020-05-05T00:00:00+00:00' })
    s._handleInbound({ type: 'rotated', sessionId: 's1', fromAccount: 'a', toAccount: 'b' })
    expect(row(qc).accountName).toBe('b')
    expect(row(qc).updatedAt).toBe('2020-05-05T00:00:00+00:00')
  })
})

import { describe, it, expect } from 'vitest'
import { QueryClient } from '@tanstack/react-query'
import { SessionSocket } from './client'
import { QK } from '@/api/hooks'
import type { Session } from '@/api/types'

const T1 = '2021-06-01T12:00:00+00:00'
const T2 = '2021-06-01T12:00:05+00:00'
function seed(qc: QueryClient, over: Partial<Session>) {
  qc.setQueryData<Session[]>(QK.sessions, [{ id: 's1', status: 'idle', accountName: 'a', ...over } as Session])
}
function makeSocket() {
  const qc = new QueryClient()
  const s = new SessionSocket('/ws') as unknown as { setQueryClient: (q: QueryClient) => void; _handleInbound: (m: unknown) => void }
  s.setQueryClient(qc)
  return { s, qc }
}
const row = (qc: QueryClient) => (qc.getQueryData<Session[]>(QK.sessions) ?? [])[0]

describe('SessionSocket rev-guarded status patch', () => {
  it('applies a status whose revAt is newer', () => {
    const { s, qc } = makeSocket(); seed(qc, { revAt: T1 })
    s._handleInbound({ type: 'status', sessionId: 's1', status: 'done', revAt: T2 })
    expect(row(qc).status).toBe('done')
    expect(row(qc).revAt).toBe(T2)
  })
  it('ignores a status whose revAt is strictly older (stale/duplicate frame)', () => {
    const { s, qc } = makeSocket(); seed(qc, { status: 'done', revAt: T2 })
    s._handleInbound({ type: 'status', sessionId: 's1', status: 'in_progress', revAt: T1 })
    expect(row(qc).status).toBe('done')   // unchanged — older frame rejected
    expect(row(qc).revAt).toBe(T2)
  })
})

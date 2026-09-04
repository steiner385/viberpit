import { describe, it, expect, beforeEach } from 'vitest'
import { addPendingSession, removePendingSession, getPendingSessions, __resetPending } from './pendingSessions'

describe('pendingSessions store', () => {
  beforeEach(() => { __resetPending() })

  it('adds a placeholder with a temp id and pending flag', () => {
    const id = addPendingSession({ name: 'New', accountName: '', status: 'in_progress', createdAt: '2026-06-23T00:00:00Z', prs: [] })
    expect(id).toMatch(/^pending-/)
    const list = getPendingSessions()
    expect(list).toHaveLength(1)
    expect(list[0]).toMatchObject({ id, name: 'New', pending: true })
  })

  it('removes by id', () => {
    const id = addPendingSession({ name: 'A', accountName: '', status: 'in_progress', createdAt: 'x', prs: [] })
    removePendingSession(id)
    expect(getPendingSessions()).toHaveLength(0)
  })

  it('keeps a stable snapshot reference between changes', () => {
    addPendingSession({ name: 'A', accountName: '', status: 'in_progress', createdAt: 'x', prs: [] })
    expect(getPendingSessions()).toBe(getPendingSessions())
  })
})

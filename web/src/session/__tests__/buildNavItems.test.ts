// @vitest-environment node
// web/src/session/__tests__/buildNavItems.test.ts
import { describe, it, expect } from 'vitest'
import { buildNavItems, type SessionGroup } from '../ordering'
import type { Session } from '@/api/types'
import { mkSession } from '@/test/factories'

const mk = (id: string): Session =>
  mkSession(id, {
    accountName: 'a',
    createdAt: '2026-06-01T00:00:00Z',
    updatedAt: '2026-06-01T00:00:00Z',
  })
const grp = (key: string, label: string | null, sessions: Session[]): SessionGroup =>
  ({ key, label, count: sessions.length, sessions })

describe('buildNavItems', () => {
  it('ungrouped (label null) yields only session items — parity with sessions list', () => {
    const groups = [grp('all', null, [mk('a'), mk('b')])]
    const items = buildNavItems(groups, new Set())
    expect(items).toEqual([
      { kind: 'session', session: groups[0].sessions[0] },
      { kind: 'session', session: groups[0].sessions[1] },
    ])
  })

  it('labelled group emits a header followed by its sessions', () => {
    const groups = [grp('busy', 'Working', [mk('a')])]
    const items = buildNavItems(groups, new Set())
    expect(items[0]).toEqual({ kind: 'header', key: 'busy', label: 'Working', count: 1, collapsed: false })
    expect(items[1]).toEqual({ kind: 'session', session: groups[0].sessions[0] })
  })

  it('collapsed group emits its header (collapsed:true) but none of its sessions', () => {
    const groups = [grp('busy', 'Working', [mk('a'), mk('b')]), grp('idle', 'Idle', [mk('c')])]
    const items = buildNavItems(groups, new Set(['busy']))
    expect(items).toEqual([
      { kind: 'header', key: 'busy', label: 'Working', count: 2, collapsed: true },
      { kind: 'header', key: 'idle', label: 'Idle', count: 1, collapsed: false },
      { kind: 'session', session: groups[1].sessions[0] },
    ])
  })
})

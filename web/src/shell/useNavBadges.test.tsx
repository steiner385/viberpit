import { describe, it, expect, vi, beforeEach } from 'vitest'
import { renderHook } from '@testing-library/react'
import type { Account, ArchivedSession, PullRequestInfo, Session } from '@/api/types'
import type { Automation } from '@/api/automations'

const useSessions = vi.fn()
const useArchivedSessions = vi.fn()
const useAllPrs = vi.fn()
const useAutomations = vi.fn()
const useAccounts = vi.fn()

vi.mock('@/api/hooks', () => ({
  useSessions: () => useSessions(),
  useArchivedSessions: () => useArchivedSessions(),
  useAllPrs: () => useAllPrs(),
  useAutomations: () => useAutomations(),
  useAccounts: () => useAccounts(),
}))

import { useNavBadges } from './useNavBadges'
import { NAV_DEFS, type NavBadgeKey } from './navDefs'

// Typed factories with defaults — real values, not casts. Each fills every
// required field of the real type so a fixture drifting from the real shape
// (a bad status string, a field that belongs to a different type) is a type
// error here, not a silent lie hidden behind `as unknown as T`.

function account(over: Partial<Account> = {}): Account {
  return {
    name: 'acct', status: 'ok', usage: null,
    liveSessions: { active: 0, idle: 0 }, rank: null, rankReason: null,
    ...over,
  }
}

function session(over: Partial<Session> = {}): Session {
  return {
    id: 'a', name: 'a', accountName: 'acct', engine: 'claude_code',
    engineSessionId: null, status: 'idle',
    createdAt: '2024-01-01T00:00:00Z', prs: [],
    ...over,
  }
}

function archivedSession(over: Partial<ArchivedSession> = {}): ArchivedSession {
  return {
    id: 'x', name: 'x', account: 'acct', cwd: '/tmp/x', repo: null,
    engine: 'claude_code', engineSessionId: null, permissionMode: 'default',
    createdAt: '2024-01-01T00:00:00Z', archivedAt: '2024-01-02T00:00:00Z',
    ...over,
  }
}

function automation(over: Partial<Automation> = {}): Automation {
  return {
    id: 'a', name: 'a', description: '', enabled: true, mode: null, modes: [],
    status: { text: 'idle', level: 'idle', counts: null },
    configSchema: [], config: {}, lastActiveAt: null, activeSessions: 0,
    ...over,
  }
}

describe('useNavBadges', () => {
  beforeEach(() => {
    useSessions.mockReturnValue({ data: [] })
    useArchivedSessions.mockReturnValue({ data: [] })
    useAllPrs.mockReturnValue({ data: [] })
    useAutomations.mockReturnValue({ data: [] })
    useAccounts.mockReturnValue({ data: [] })
  })

  it('every key it returns is a declared NavBadgeKey', () => {
    const { result } = renderHook(() => useNavBadges())
    // Set<string>, not Set<NavBadgeKey>: Object.keys() only ever gives back
    // string, and widening the set (rather than narrowing the key with a
    // cast) is what lets .has() take that string with no assertion at all.
    const badgeKeys = new Set<string>(
      NAV_DEFS.map((d) => d.badge).filter((b): b is NavBadgeKey => b !== undefined),
    )
    for (const key of Object.keys(result.current)) {
      expect(badgeKeys.has(key)).toBe(true)
    }
  })

  it('a zero count comes back as undefined so no badge renders', () => {
    const { result } = renderHook(() => useNavBadges())
    expect(result.current.sessions).toBeUndefined()
    expect(result.current.pipeline).toBeUndefined()
    expect(result.current.automations).toBeUndefined()
    expect(result.current.archived).toBeUndefined()
  })

  it('settings is boolean-or-undefined, never a number', () => {
    useAccounts.mockReturnValue({ data: [account({ status: 'needs-reauth' })] })
    const { result } = renderHook(() => useNavBadges())
    expect(result.current.settings).toBe(true)
    expect(typeof result.current.settings).toBe('boolean')

    useAccounts.mockReturnValue({ data: [] })
    const { result: result2 } = renderHook(() => useNavBadges())
    expect(result2.current.settings).toBeUndefined()
  })

  // Migrated from `DashboardTabs.test.tsx`'s three needs-reauth cases (Task 18),
  // which asserted the amber dot on the Settings TAB. The tab is gone; the rule
  // that decides the dot is here, and `needs-relogin` — a status the case above
  // never exercises — is the half that would silently stop badging.
  it.each([
    ['needs-reauth' as const, true],
    ['needs-relogin' as const, true],
    ['ok' as const, undefined],
  ])('an account with status %s gives settings = %s', (status, expected) => {
    useAccounts.mockReturnValue({ data: [account({ status })] })
    const { result } = renderHook(() => useNavBadges())
    expect(result.current.settings).toBe(expected)
  })

  it('DEC-4: sessions counts every session in the section, including queued ones', () => {
    // DesktopShell used to count orderedSessions.length (queued excluded) — that
    // was an artifact of it never rendering PendingApprovalSection. The decision
    // is sessions.length: a section badge counts what the section contains, and
    // queued/pending-approval rows render inside the Sessions section.
    useSessions.mockReturnValue({
      data: [
        session({ id: 'a', name: 'a', status: 'idle' }),
        session({ id: 'b', name: 'b', status: 'queued' }),
      ],
    })
    const { result } = renderHook(() => useNavBadges())
    expect(result.current.sessions).toBe(2)
  })

  it('pipeline counts in-flight PRs; archived and automations count their own data', () => {
    const prs: PullRequestInfo[] = [
      { number: 1, url: 'https://example/pr/1', state: 'open', checks: 'pending', title: 't', deployed: false },
    ]
    useAllPrs.mockReturnValue({ data: prs })
    useArchivedSessions.mockReturnValue({ data: [archivedSession({ id: 'x' })] })
    useAutomations.mockReturnValue({ data: [automation({ activeSessions: 3 })] })
    const { result } = renderHook(() => useNavBadges())
    expect(result.current.pipeline).toBe(1)
    expect(result.current.archived).toBe(1)
    expect(result.current.automations).toBe(3)
  })
})

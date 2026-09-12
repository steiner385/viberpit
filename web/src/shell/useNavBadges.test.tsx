import { describe, it, expect, vi, beforeEach } from 'vitest'
import { renderHook } from '@testing-library/react'
import type { Account, PullRequestInfo, Session } from '@/api/types'
import type { Automation } from '@/api/automations'
import type { TriageProposal } from '@/api/triage'

const useSessions = vi.fn()
const useAllPrs = vi.fn()
const useAutomations = vi.fn()
const useAccounts = vi.fn()
const useTriage = vi.fn()

const useDeployState = vi.fn()

vi.mock('@/api/hooks', () => ({
  useSessions: () => useSessions(),
  useAllPrs: () => useAllPrs(),
  useAutomations: () => useAutomations(),
  useAccounts: () => useAccounts(),
  useTriage: () => useTriage(),
}))

// Mocked at the DERIVED state, not at the two queries under it: the dot is a
// function of `hasFailure`, and stubbing raw `pending`/`deploy` payloads here
// would make this file a second place that knows how a failure is derived.
vi.mock('@/lib/deployState', () => ({ useDeployState: () => useDeployState() }))

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

function automation(over: Partial<Automation> = {}): Automation {
  return {
    id: 'a', name: 'a', description: '', enabled: true, mode: null, modes: [],
    status: { text: 'idle', level: 'idle', counts: null },
    configSchema: [], config: {}, lastActiveAt: null, activeSessions: 0,
    ...over,
  }
}

function proposal(over: Partial<TriageProposal> = {}): TriageProposal {
  return {
    id: 'p1', workItemId: 'w1', projectSlug: 'kindash', repo: 'steiner385/kindash',
    number: 1, kind: 'issue', title: 't', url: null,
    proposedFeatureId: null, proposedFeatureSlug: null, noFitSuggestion: null,
    reach: null, impact: null, effort: null, confidence: null,
    rationale: null, confidenceLevel: null, status: 'pending', createdAt: null,
    classifiedBy: null, resolvedAt: null,
    ...over,
  }
}

describe('useNavBadges', () => {
  beforeEach(() => {
    useSessions.mockReturnValue({ data: [] })
    useAllPrs.mockReturnValue({ data: [] })
    useAutomations.mockReturnValue({ data: [] })
    useAccounts.mockReturnValue({ data: [] })
    useTriage.mockReturnValue({ data: [] })
    useDeployState.mockReturnValue({ hasFailure: false, count: 0 })
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
    expect(result.current.delivery).toBeUndefined()
    expect(result.current.roadmap).toBeUndefined()
  })

  // Finish-line row 3: the Triage section is gone and its queue is the Inbox
  // scope of Roadmap, so the pending count badges Roadmap. The count is the
  // whole app-wide queue (`useTriage()` with no project scope) — the same key
  // the Inbox column reads when it is not narrowed, so nav and scope agree.
  it('roadmap counts pending proposals, and an empty queue shows no badge', () => {
    useTriage.mockReturnValue({ data: [proposal({ id: 'a' }), proposal({ id: 'b' })] })
    const { result } = renderHook(() => useNavBadges())
    expect(result.current.roadmap).toBe(2)

    useTriage.mockReturnValue({ data: [] })
    const { result: empty } = renderHook(() => useNavBadges())
    expect(empty.current.roadmap).toBeUndefined()
  })

  // The list `useTriage` returns is WIDENED: it also carries the triage loop's
  // fresh auto-accepts for their undo window, so the inbox can render the
  // "auto-accepted · undo" chip. A badge that counted those says "6 need you"
  // about 5 — and the sixth is already decided.
  it('roadmap badge excludes a fresh auto-accept in its undo window', () => {
    useTriage.mockReturnValue({
      data: [
        ...['a', 'b', 'c', 'd', 'e'].map((id) => proposal({ id })),
        proposal({ id: 'auto', status: 'accepted', classifiedBy: 'auto' }),
      ],
    })
    const { result } = renderHook(() => useNavBadges())
    expect(result.current.roadmap).toBe(5)
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

  // The deploy banner is hidden on phone (`useAppBanners`, `hideOn: ['phone']`),
  // so a failed deploy would otherwise be completely silent there — Settings ›
  // Deploy holds the detail, but nothing would point at it. This dot is that
  // pointer.
  it('settings lights up when a deploy failed, with no account needing reauth', () => {
    useDeployState.mockReturnValue({ hasFailure: true, count: 0 })
    const { result } = renderHook(() => useNavBadges())
    expect(result.current.settings).toBe(true)
  })

  // Pending commits are routine — master advances all day. Only a FAILURE earns
  // the dot, or Settings would wear one permanently and stop meaning anything.
  it('settings stays clear for commits merely pending deploy', () => {
    useDeployState.mockReturnValue({ hasFailure: false, count: 7 })
    const { result } = renderHook(() => useNavBadges())
    expect(result.current.settings).toBeUndefined()
  })

  it('settings stays boolean when both a reauth and a deploy failure are live', () => {
    useAccounts.mockReturnValue({ data: [account({ status: 'needs-reauth' })] })
    useDeployState.mockReturnValue({ hasFailure: true, count: 3 })
    const { result } = renderHook(() => useNavBadges())
    expect(result.current.settings).toBe(true)
    expect(typeof result.current.settings).toBe('boolean')
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

  it('leaves quiet maintenance sessions out of the count, but counts one that needs you', () => {
    // The badge is the eye-drawing surface; Background exists so these rows do
    // not draw the eye. A maintenance session that lifts into Active is counted.
    useSessions.mockReturnValue({
      data: [
        session({ id: 'a', name: 'a', status: 'idle' }),
        session({ id: 'm1', name: 'pm map', status: 'in_progress', origin: 'pm-onboard', workClass: 'maintenance' }),
        session({ id: 'm2', name: 'pr-doctor fix', status: 'waiting_input', origin: 'pr-doctor', workClass: 'maintenance' }),
      ],
    })
    const { result } = renderHook(() => useNavBadges())
    expect(result.current.sessions).toBe(2)
  })

  it('pipeline counts in-flight PRs; delivery counts live automation sessions', () => {
    const prs: PullRequestInfo[] = [
      { number: 1, url: 'https://example/pr/1', state: 'open', checks: 'pending', title: 't', deployed: false },
    ]
    useAllPrs.mockReturnValue({ data: prs })
    useAutomations.mockReturnValue({ data: [automation({ activeSessions: 3 })] })
    const { result } = renderHook(() => useNavBadges())
    expect(result.current.pipeline).toBe(1)
    expect(result.current.delivery).toBe(3)
  })
})

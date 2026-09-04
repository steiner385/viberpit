/**
 * Shared typed fixtures. Refs #952.
 *
 * ONE definition per domain shape, so a new required field on `Session`
 * (`prs` was the last one) is added here once instead of in the dozen-odd
 * hand-rolled copies that used to live beside each suite. Before this file
 * there were eight separate `session()` / `mk()` factories with different
 * defaults — the parallel-implementation shape this codebase treats as its
 * most expensive bug (constitution VI).
 *
 * Rules for anything added here:
 *  - real values, never `as unknown as T` — a fixture that drifts from the
 *    real type must be a type error, which is the whole point;
 *  - defaults are the MINIMUM that satisfies the type plus whatever the
 *    majority of call sites want. Anything a suite cares about it passes in
 *    `over`, so the default can never silently drive an assertion.
 *
 * Note on `updatedAt`: deliberately absent by default. It is optional on
 * `Session` and several ordering/liveness paths branch on its presence, so a
 * default here would change what those suites exercise. Suites that want a
 * session with recorded activity pass it explicitly.
 */
import type { AppSettings } from '@/api/hooks'
import type { Account, ArchivedSession, PullRequestInfo, Session } from '@/api/types'

/** A `Session` with every required field filled in. */
export function session(over: Partial<Session> = {}): Session {
  return {
    id: 'sess-1',
    name: 'First',
    accountName: 'alice',
    status: 'idle',
    createdAt: '2026-06-19T10:00:00Z',
    prs: [],
    ...over,
  }
}

/**
 * `session()` for the common list-fixture shape where the id doubles as the
 * name (`mk('a')`, `mk('b')` …) — the form seven of the eight old factories took.
 */
export function mkSession(id: string, over: Partial<Session> = {}): Session {
  return session({ id, name: id, ...over })
}

/** An `Account` with every required field filled in. */
export function account(over: Partial<Account> = {}): Account {
  return {
    name: 'alice',
    status: 'ok',
    usage: { session: { pct: 10, resetsAt: null }, weekly: { pct: 20, resetsAt: null } },
    liveSessions: { active: 0, idle: 0 },
    rank: 1,
    rankReason: null,
    ...over,
  }
}

/** An `ArchivedSession` with every required field filled in. */
export function archivedSession(over: Partial<ArchivedSession> = {}): ArchivedSession {
  return {
    id: 'sess-1',
    name: 'First',
    account: 'alice',
    cwd: '/tmp',
    repo: null,
    claudeSessionId: null,
    permissionMode: 'interactive',
    createdAt: '2026-06-19T10:00:00Z',
    archivedAt: '2026-06-19T11:00:00Z',
    ...over,
  }
}

/** A `PullRequestInfo` with every required field filled in. */
export function pullRequest(over: Partial<PullRequestInfo> = {}): PullRequestInfo {
  return {
    number: 1,
    url: 'https://github.com/owner/repo/pull/1',
    state: 'open',
    checks: 'pending',
    title: 'A pull request',
    deployed: false,
    ...over,
  }
}

/**
 * `AppSettings` at the server's own defaults.
 *
 * Every suite that mocks `useSettings` used to stub three of the ten fields and
 * cast the literal at `AppSettings` — so a component reading `autoArchiveEnabled`
 * or `maxActiveSessions` saw `undefined` in the test and a real value in the
 * browser. The values below match the `?? default` each reader already applies
 * (GeneralSection.tsx), so routing a stub through this changes nothing it
 * rendered — it only stops the fixture lying about the payload.
 */
export function appSettings(over: Partial<AppSettings> = {}): AppSettings {
  return {
    defaultPermissionMode: 'interactive',
    thinkingCollapsed: false,
    repoRoot: null,
    defaultToolDetail: 'auto',
    maxArchivedSessions: null,
    archiveHome: null,
    maxActiveSessions: null,
    autoArchiveEnabled: true,
    modelApprovalAction: null,
    modelApprovalDenylist: null,
    ...over,
  }
}

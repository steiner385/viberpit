import { readFileSync } from 'node:fs'
import path from 'node:path'
import { describe, it, expect } from 'vitest'

/**
 * Regression guard for #43 — single source of truth across the mobile and desktop
 * rendering paths (constitution Principle VI: no parallel implementations).
 *
 * PR #41 (master-detail desktop layout) intentionally kept the desktop components
 * as standalone mirrors of logic that still lived inline in the mobile components.
 * That tech debt was paid down incrementally:
 *   - accounts/archived panels  → AccountsManager / ArchivedManager     (#222)
 *   - session runtime + body    → useSessionRuntime / SessionBody        (570a4e6)
 * so both breakpoints now render the SAME extracted units.
 *
 * These are structural (source-text) assertions rather than behavioral tests: a
 * render test proves the shared unit renders, but it cannot prove that a SECOND
 * inline copy wasn't re-introduced elsewhere. The whole point of the issue is the
 * absence of a duplicate, so we assert on the source directly — mirroring the
 * existing viewportMeta.test.ts guard. If someone re-inlines the extracted logic
 * into the mobile component (the exact shape of the original tech debt), the
 * negative assertions below fail loudly.
 *
 * Vitest runs with cwd at the web/ package root.
 */
function read(rel: string): string {
  return readFileSync(path.resolve(process.cwd(), rel), 'utf-8')
}

const dashboard = read('src/components/Dashboard.tsx')
const desktopShell = read('src/components/DesktopShell.tsx')
const sessionView = read('src/components/session/SessionView.tsx')

describe('#43 accounts/archived panels — one implementation, both breakpoints', () => {
  it('mobile Dashboard renders the shared AccountsManager + ArchivedManager', () => {
    expect(dashboard).toMatch(/<AccountsManager\b/)
    expect(dashboard).toMatch(/<ArchivedManager\b/)
  })

  it('desktop DesktopShell renders the same shared AccountsManager + ArchivedManager', () => {
    expect(desktopShell).toMatch(/<AccountsManager\b/)
    expect(desktopShell).toMatch(/<ArchivedManager\b/)
  })

  it('Dashboard does NOT re-inline the extracted account/archived internals', () => {
    // AccountRow / AccountsStrip / DeleteAccountDialog live only in AccountsManager;
    // the archived list lives only in ArchivedManager. Re-declaring any of them in
    // the mobile component is the tech debt #43 removes.
    expect(dashboard).not.toMatch(/\bfunction\s+(AccountRow|AccountsStrip|DeleteAccountDialog|ArchivedList)\b/)
  })
})

describe('#43 session runtime/body — one implementation, both breakpoints', () => {
  it('mobile SessionView consumes the shared useSessionRuntime + SessionBody', () => {
    expect(sessionView).toMatch(/useSessionRuntime\(/)
    expect(sessionView).toMatch(/<SessionBody\b/)
  })

  it('desktop DesktopShell renders SessionBody off the shared runtime provider/context', () => {
    expect(desktopShell).toMatch(/<SessionBody\b/)
    expect(desktopShell).toMatch(/SessionRuntimeProvider/)
    expect(desktopShell).toMatch(/useSessionRuntimeContext\(/)
  })

  it('SessionView does NOT re-inline the transcript reducer / socket runtime', () => {
    // The live runtime (socket wiring + transcript reducer + derived state) belongs
    // to useSessionRuntime alone. SessionView owns only full-screen chrome, so it
    // must not touch the reducer directly.
    expect(sessionView).not.toMatch(/transcriptReducer/)
    expect(sessionView).not.toMatch(/@\/session\/reducer/)
    expect(sessionView).not.toMatch(/\buseReducer\b/)
  })
})

import { readdirSync, readFileSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { describe, it, expect } from 'vitest'

/**
 * Regression guard for #43 — single source of truth for the accounts/archived
 * panels and the session runtime (constitution Principle VI: no parallel
 * implementations).
 *
 * ## What changed, and why the file survives (R12 / DEC-12)
 *
 * It was written when there were TWO shells: PR #41 gave the desktop layout
 * standalone mirrors of logic that still lived inline in the mobile components,
 * and the debt was paid down by extracting shared units
 * (`AccountsManager`/`ArchivedManager` #222, `useSessionRuntime`/`SessionBody`
 * 570a4e6). Every assertion was then "and BOTH shells use the shared one".
 *
 * Task 17 deleted both shells. The naive reading is that the guard has nothing
 * left to check — but the property it protects is not "the two shells agree", it
 * is "there is exactly one implementation, and no host re-inlines it". That
 * property survives a single shell perfectly well, and the risk it guards is if
 * anything HIGHER now: `AppShell` plus seven region files is a lot of new places
 * for a second copy to appear. So the greps are retargeted at the shell and its
 * regions, and the "both breakpoints" halves collapse into one assertion each,
 * because there is one breakpoint-independent path.
 *
 * These stay structural (source-text) assertions rather than render tests, for
 * the original reason: a render test proves the shared unit renders, but it
 * cannot prove that a SECOND inline copy was not introduced somewhere else. The
 * subject is the absence of a duplicate.
 *
 * Paths resolve from THIS FILE, not from `process.cwd()`. The original read
 * `cwd()` and happened to work because `npm --prefix web test` runs with the
 * package root as cwd — but `vitest --root web` from the repo root does not, and
 * a guard that silently cannot find its subject is worse than no guard.
 */
const WEB_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../..')

function read(rel: string): string {
  return readFileSync(path.resolve(WEB_ROOT, rel), 'utf-8')
}

const SHELL_DIR = 'src/shell'
const REGIONS_DIR = 'src/shell/regions'

/**
 * Every shell/region source file, discovered rather than listed.
 *
 * A hand-written list is how this guard would quietly stop covering the region
 * added next week — the exact failure mode the file exists to prevent, one level
 * up. Tests are excluded: a test may legitimately name a component it stubs.
 */
function shellSources(): { rel: string; text: string }[] {
  const out: { rel: string; text: string }[] = []
  for (const dir of [SHELL_DIR, REGIONS_DIR]) {
    for (const name of readdirSync(path.resolve(WEB_ROOT, dir))) {
      if (!/\.tsx?$/.test(name) || /\.test\.tsx?$/.test(name)) continue
      out.push({ rel: `${dir}/${name}`, text: read(`${dir}/${name}`) })
    }
  }
  return out
}

const routes = read('src/shell/routes.tsx')
const appShell = read('src/shell/AppShell.tsx')
const mainPaneSession = read('src/features/sessions/MainPaneSession.tsx')
const settingsView = read('src/components/settings/SettingsView.tsx')

describe('#43 accounts/archived panels — one implementation', () => {
  it('the route table is the only mount point for SettingsView and ArchivedManager', () => {
    // Both were mounted by each shell before; both are pages now, and the table
    // is the one place a page is named.
    expect(routes).toMatch(/<SettingsView\b/)
    expect(routes).toMatch(/<ArchivedManager\b/)
    for (const { rel, text } of shellSources()) {
      if (rel === 'src/shell/routes.tsx') continue
      expect(text, `${rel} mounts SettingsView`).not.toMatch(/<SettingsView\b/)
      expect(text, `${rel} mounts ArchivedManager`).not.toMatch(/<ArchivedManager\b/)
    }
  })

  it('SettingsView is the single mount point for the shared AccountsManager', () => {
    expect(settingsView).toMatch(/<AccountsManager\b/)
    for (const { rel, text } of shellSources()) {
      expect(text, `${rel} mounts AccountsManager`).not.toMatch(/<AccountsManager\b/)
    }
    expect(routes).not.toMatch(/<AccountsManager\b/)
  })

  it('no shell or region re-inlines the extracted account/archived internals', () => {
    // AccountRow / AccountsStrip / DeleteAccountDialog live only in
    // AccountsManager; the archived list lives only in ArchivedManager.
    // Re-declaring any of them in a host is the tech debt #43 removed.
    const inlined = /\bfunction\s+(AccountRow|AccountsStrip|DeleteAccountDialog|ArchivedList)\b/
    for (const { rel, text } of shellSources()) {
      expect(text, `${rel} re-inlines an extracted panel internal`).not.toMatch(inlined)
    }
    expect(appShell).not.toMatch(inlined)
  })
})

describe('#43 session runtime/body — one implementation', () => {
  it('MainPaneSession consumes the shared runtime context + SessionBody', () => {
    // The pane READS the runtime (audit F2.3): `PageHost` owns the single
    // provider, so the transcript and the dock's composer are one instance.
    expect(mainPaneSession).toMatch(/<SessionBody\b/)
    expect(mainPaneSession).toMatch(/useSessionRuntimeContext\(/)
  })

  it('PageHost is the only place the session runtime provider is mounted', () => {
    const mounts = shellSources().filter(({ text }) => /<SessionRuntimeProvider\b/.test(text))
    expect(mounts.map((m) => m.rel)).toEqual(['src/shell/regions/PageHost.tsx'])
  })

  it('MainPaneSession does NOT re-inline the transcript reducer / socket runtime', () => {
    // The live runtime (socket wiring + transcript reducer + derived state)
    // belongs to `useSessionRuntime` alone. The pane owns only chrome.
    expect(mainPaneSession).not.toMatch(/transcriptReducer/)
    expect(mainPaneSession).not.toMatch(/@\/session\/reducer/)
    expect(mainPaneSession).not.toMatch(/\buseReducer\b/)
    expect(mainPaneSession).not.toMatch(/useSessionRuntime\(/)
  })
})

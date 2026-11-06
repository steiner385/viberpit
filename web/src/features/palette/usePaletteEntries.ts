import { useMemo } from 'react'
import { useArchivedSessions, useGithubRepos, usePresets, type GithubRepo } from '@/api/hooks'
import type { ArchivedSession, SessionPreset } from '@/api/types'
import { formatRepoLabel } from '@/lib/repoLabel'
import { TIER_LABEL } from '@/session/statusTier'
import { triageTier } from '@/session/triageTier'
import { SETTINGS_GROUPS, SETTINGS_META } from '@/features/settings/settingsMeta'
import { NAV_BY_ID, NAV_DEFS, settingsSectionPath, type NavId } from '@/shell/navDefs'
import { SHORTCUTS, displayKeys, shortcutAllowed } from '@/shell/shortcuts'
import { prepareSearch, searchPrepared, type Facets, type Prepared, type Searchable } from './paletteSearch'
import type { PaletteHost } from './paletteHost'

/** Section order is the spec's (D-10 §Palette): it is also the render order. */
export const PALETTE_SECTIONS = [
  { id: 'sessions', label: 'Sessions' },
  { id: 'actions', label: 'Actions' },
  { id: 'navigate', label: 'Navigate' },
  { id: 'settings', label: 'Settings' },
  { id: 'repos', label: 'Repos' },
  { id: 'branches', label: 'Branches' },
] as const

export type PaletteSectionId = (typeof PALETTE_SECTIONS)[number]['id']

interface PaletteEntryBase extends Searchable {
  /** Unique across the palette — the option's DOM id derives from it. */
  key: string
  section: PaletteSectionId
  detail?: string
  /** The row's own `displayKeys()` — never spelled here. */
  chord?: string
  /** Epoch ms, sessions only: the empty query's "recent" order. */
  recency?: number
}

/**
 * Selecting an entry either RUNS it (the palette closes first) or DRILLS: the
 * palette's own query becomes `drill` — a `repo:` / `branch:` facet term — and
 * the palette stays open. A drill is how a repo or branch the session list
 * cannot filter to exactly is shown: session entries carry their exact repo
 * and branch as `facets`, so the Sessions section then lists exactly the
 * sessions on it, background and archived included.
 */
export type PaletteEntry = PaletteEntryBase & (
  | { run: () => void; drill?: undefined }
  | { drill: string; run?: undefined }
)

/** Sessions shown for an empty query, before Actions. */
export const RECENT_SESSIONS = 8
/** Rows rendered per section for a query — the rest are a keystroke away. */
export const MAX_PER_SECTION = 30

export interface PaletteData {
  archived: readonly ArchivedSession[]
  repos: readonly GithubRepo[]
  /** Saved presets, one "New session from preset" action each (D-14). */
  presets?: readonly SessionPreset[]
}

const time = (iso: string | null | undefined): number => {
  const t = iso ? Date.parse(iso) : NaN
  return Number.isNaN(t) ? 0 : t
}

/** A session's exact repo / branch, for `repo:` / `branch:` drill-downs;
 *  absent values are omitted so the facet term fails rather than matching "". */
function facetsOf(repo: string | null | undefined, branch: string | null | undefined): Facets {
  const out: Facets = {}
  const r = repo?.trim()
  const b = branch?.trim()
  if (r) out.repo = r
  if (b) out.branch = b
  return out
}

const joinDetail = (parts: (string | null | undefined)[]): string =>
  parts.filter((p): p is string => !!p).join(' · ')

/**
 * Every palette entry, from the registries and hooks that already exist — no
 * index of its own. Pure, so the kiosk and "same handler" rules are testable
 * without a shell.
 */
export function buildPaletteEntries(host: PaletteHost, data: PaletteData): PaletteEntry[] {
  const c = host.context
  // Kiosk shows a section's entries only where kiosk may go: the same
  // `showInKiosk` claim `KioskPageGuard` enforces on the route.
  const reachable = (id: NavId): boolean => c.interactive || NAV_BY_ID[id].showInKiosk
  const out: PaletteEntry[] = []

  if (reachable('sessions')) {
    for (const { session: s, scope } of host.live) {
      const repo = s.repo ? formatRepoLabel(s.repo).text : null
      const status = TIER_LABEL[triageTier(s)]
      out.push({
        key: `session:${s.id}`, section: 'sessions', label: s.name,
        detail: joinDetail([repo, s.branch, status]),
        keywords: [repo, s.branch, status].filter((k): k is string => !!k),
        facets: facetsOf(s.repo, s.branch),
        recency: time(s.updatedAt ?? s.createdAt),
        run: () => host.openSession(s.id, scope),
      })
    }
    for (const a of data.archived) {
      const repo = a.repo ? formatRepoLabel(a.repo).text : null
      out.push({
        key: `archived:${a.id}`, section: 'sessions', label: a.name,
        detail: joinDetail([repo, 'Archived']),
        keywords: [repo, 'Archived'].filter((k): k is string => !!k),
        facets: facetsOf(a.repo, null),
        recency: time(a.archivedAt),
        run: () => host.openArchived(a.id),
      })
    }
  }

  if (c.interactive) {
    out.push({ key: 'action:new-session', section: 'actions', label: 'New session', run: host.newSession })
    // No chord: every digit family is taken (D-14 spike §4); the palette is the
    // keyboard path to a preset.
    for (const p of data.presets ?? []) {
      out.push({
        key: `action:preset:${p.id}`, section: 'actions', label: `New session from preset: ${p.name}`,
        keywords: ['preset', p.name], run: () => host.newSessionFromPreset(p.id),
      })
    }
  }
  // Rows sharing one handler (← / Esc / Ctrl+Z all detach) are ONE action:
  // the first row in registry order names it.
  const seen = new Set<() => void>()
  for (const s of SHORTCUTS) {
    if (s.group !== 'app' && s.group !== 'in-session') continue
    const run = host.actions[s.id]
    if (!run || seen.has(run) || !shortcutAllowed(s, c)) continue
    seen.add(run)
    out.push({ key: `action:${s.id}`, section: 'actions', label: s.description, chord: displayKeys(s), run })
  }

  for (const d of NAV_DEFS) {
    if (!reachable(d.id)) continue
    out.push({ key: `nav:${d.id}`, section: 'navigate', label: d.label, run: () => host.navigate(d.path) })
  }

  if (reachable('settings')) {
    const groupLabel = new Map(SETTINGS_GROUPS.map((g) => [g.id, g.label]))
    for (const m of SETTINGS_META) {
      const group = groupLabel.get(m.group)
      out.push({
        key: `settings:${m.id}`, section: 'settings', label: m.label, detail: joinDetail(['Settings', group]),
        keywords: [...(m.keywords ?? []), ...(group ? [group] : [])],
        run: () => host.navigate(settingsSectionPath(m.id)),
      })
    }
  }

  if (reachable('sessions')) {
    // The list's Project filter only offers repos with an Active-scope session;
    // any other repo (archived-only, background-only, idle) drills instead of
    // routing to a list that cannot show it.
    for (const r of data.repos) {
      const key = `repo:${r.nameWithOwner}`
      out.push(host.canFilterRepo(r.nameWithOwner)
        ? { key, section: 'repos', label: r.nameWithOwner, run: () => host.filterRepo(r.nameWithOwner) }
        : { key, section: 'repos', label: r.nameWithOwner, drill: `repo:${r.nameWithOwner}` })
    }
    // Archived rows carry no branch, so the live sessions are the whole domain.
    // No list filter narrows to a branch, so a branch always drills. Each branch a
    // session has on a repo also offers a new session there (D-19): the dispatch bar
    // opens with that repo and branch chosen — nothing is created until it is sent.
    const branches = new Map<string, Set<string>>()
    for (const { session } of host.live) {
      const b = session.branch?.trim()
      if (!b) continue
      const repos = branches.get(b) ?? new Set<string>()
      const r = session.repo?.trim()
      if (r) repos.add(r)
      branches.set(b, repos)
    }
    for (const [b, repos] of branches) {
      out.push({ key: `branch:${b}`, section: 'branches', label: b, drill: `branch:${b}` })
      if (!c.interactive) continue
      for (const r of repos) {
        out.push({
          key: `branch-new:${r}@${b}`, section: 'branches', label: `New session on ${r}@${b}`,
          keywords: ['new session', r, b], run: () => host.startOnBranch(r, b),
        })
      }
    }
  }
  return out
}

export interface PaletteSection {
  id: PaletteSectionId
  label: string
  entries: PaletteEntry[]
}

export type PreparedPalette = Record<PaletteSectionId, Prepared<PaletteEntry>[]>

export function preparePalette(entries: readonly PaletteEntry[]): PreparedPalette {
  const by = {} as Record<PaletteSectionId, PaletteEntry[]>
  for (const s of PALETTE_SECTIONS) by[s.id] = []
  for (const e of entries) by[e.section].push(e)
  const out = {} as PreparedPalette
  for (const s of PALETTE_SECTIONS) out[s.id] = prepareSearch(by[s.id])
  return out
}

/**
 * What the palette lists for `query`: each section's matches, best first, in
 * section order. An empty query is the most recent sessions, then Actions.
 */
export function paletteSections(prepared: PreparedPalette, query: string): PaletteSection[] {
  const pick = (id: PaletteSectionId, entries: PaletteEntry[]): PaletteSection[] => {
    const label = PALETTE_SECTIONS.find((s) => s.id === id)?.label ?? id
    return entries.length > 0 ? [{ id, label, entries }] : []
  }
  if (query.trim() === '') {
    const recent = prepared.sessions
      .map((p) => p.item)
      .sort((a, b) => (b.recency ?? 0) - (a.recency ?? 0))
      .slice(0, RECENT_SESSIONS)
    return [...pick('sessions', recent), ...pick('actions', prepared.actions.map((p) => p.item))]
  }
  return PALETTE_SECTIONS.flatMap((s) =>
    pick(s.id, searchPrepared(prepared[s.id], query).slice(0, MAX_PER_SECTION)))
}

/**
 * The palette's entries, prepared for search. The archived list, the GitHub
 * repos and the presets are fetched HERE, inside the lazy chunk, so nothing loads them until
 * the palette first opens — and the shell's many `@/api/hooks` test mocks never
 * meet these hooks.
 */
export function usePaletteEntries(host: PaletteHost): PreparedPalette {
  const { data: archived = [] } = useArchivedSessions()
  const { data: repos = [] } = useGithubRepos()
  const { data: presets = [] } = usePresets()
  return useMemo(
    () => preparePalette(buildPaletteEntries(host, { archived, repos, presets })),
    [host, archived, repos, presets],
  )
}

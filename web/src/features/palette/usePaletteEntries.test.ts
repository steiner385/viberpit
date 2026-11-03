import { describe, expect, it, vi } from 'vitest'
import type { ArchivedSession, Session } from '@/api/types'
import type { GithubRepo } from '@/api/hooks'
import type { ShortcutContext } from '@/shell/shortcuts'
import { SETTINGS_META } from '@/features/settings/settingsMeta'
import { NAV_DEFS } from '@/shell/navDefs'
import type { PaletteHost } from './paletteHost'
import {
  RECENT_SESSIONS, buildPaletteEntries, paletteSections, preparePalette, type PaletteEntry,
} from './usePaletteEntries'

const ctx = (over: Partial<ShortcutContext> = {}): ShortcutContext => ({
  overlayOpen: false, hasOpenAction: false, hasDeniablePermission: false, sessionOpen: false,
  sessionBusy: false, inSessions: true, interactive: true, active: null, yieldsToTextEntry: false,
  transcriptOpen: false, ...over,
})

const live = (id: string, over: Partial<Session> = {}): Session => ({
  id, name: `Live ${id}`, accountName: 'a', engine: 'claude_code', engineSessionId: null,
  status: 'idle', createdAt: '2026-11-01T10:00:00Z', prs: [], ...over,
})

const archived = (id: string, over: Partial<ArchivedSession> = {}): ArchivedSession => ({
  id, name: `Old ${id}`, account: 'a', cwd: '/x', repo: null, engine: 'claude_code',
  engineSessionId: null, permissionMode: 'default', createdAt: '2026-10-01T10:00:00Z',
  archivedAt: '2026-10-02T10:00:00Z', ...over,
})

const repo = (nameWithOwner: string): GithubRepo => ({
  nameWithOwner, name: nameWithOwner.split('/')[1], owner: nameWithOwner.split('/')[0], isPrivate: false,
  url: '', defaultBranch: 'main', pushedAt: null, cloned: true,
})

function host(over: Partial<PaletteHost> = {}): PaletteHost {
  return {
    context: ctx(), actions: {}, live: [],
    openSession: vi.fn(), openArchived: vi.fn(), newSession: vi.fn(), navigate: vi.fn(),
    canFilterRepo: () => false, filterRepo: vi.fn(), close: vi.fn(),
    ...over,
  }
}

const NO_DATA = { archived: [], repos: [] }
const inSection = (entries: PaletteEntry[], section: PaletteEntry['section']) =>
  entries.filter((e) => e.section === section)

describe('buildPaletteEntries', () => {
  it('lists live sessions, then archived ones, each opened through its own list path', () => {
    const h = host({ live: [{ session: live('a'), scope: 'live' }, { session: live('b'), scope: 'background' }] })
    const sessions = inSection(buildPaletteEntries(h, { archived: [archived('z')], repos: [] }), 'sessions')
    expect(sessions.map((e) => e.label)).toEqual(['Live a', 'Live b', 'Old z'])
    sessions[1].run!()
    expect(h.openSession).toHaveBeenCalledWith('b', 'background')
    sessions[2].run!()
    expect(h.openArchived).toHaveBeenCalledWith('z')
    expect(sessions[2].detail).toMatch(/Archived/)
  })

  it('a session row shows and searches its repo, branch and status', () => {
    const h = host({ live: [{ session: live('a', { repo: 'o/viberpit', branch: 'feat/x' }), scope: 'live' }] })
    const [e] = inSection(buildPaletteEntries(h, NO_DATA), 'sessions')
    expect(e.detail).toContain('o/viberpit')
    expect(e.detail).toContain('feat/x')
    expect(e.keywords).toEqual(expect.arrayContaining(['o/viberpit', 'feat/x']))
  })

  it('an action runs the SAME function as its shortcut, shows its row’s chord, and obeys the row’s `when`', () => {
    const help = vi.fn()
    const detach = vi.fn()
    const stop = vi.fn()
    const actions = {
      'app.help': help, 'insession.detach': detach, 'insession.detachEsc': detach,
      'insession.forceDetach': detach, 'insession.stop': stop,
    }
    const idle = inSection(buildPaletteEntries(host({ actions, context: ctx({ sessionOpen: true }) }), NO_DATA), 'actions')
    const helpEntry = idle.find((e) => e.key === 'action:app.help')!
    expect(helpEntry.run).toBe(help)
    expect(helpEntry.chord).toMatch(/^(Ctrl\+\/|⌘\/)$/)
    // Three detach rows share one handler: one action.
    expect(idle.filter((e) => e.run === detach)).toHaveLength(1)
    // `insession.stop` needs a running turn.
    expect(idle.some((e) => e.run === stop)).toBe(false)
    const busy = inSection(buildPaletteEntries(host({ actions, context: ctx({ sessionOpen: true, sessionBusy: true }) }), NO_DATA), 'actions')
    expect(busy.some((e) => e.run === stop)).toBe(true)
    // No session open: no detach at all.
    expect(inSection(buildPaletteEntries(host({ actions }), NO_DATA), 'actions').some((e) => e.run === detach)).toBe(false)
  })

  it('offers only rows with a handler (never the palette itself), plus New session', () => {
    const h = host({ actions: { 'app.help': vi.fn() } })
    const keys = inSection(buildPaletteEntries(h, NO_DATA), 'actions').map((e) => e.key)
    expect(keys).toEqual(['action:new-session', 'action:app.help'])
  })

  it('navigate and settings come from their registries', () => {
    const h = host()
    const entries = buildPaletteEntries(h, NO_DATA)
    expect(inSection(entries, 'navigate').map((e) => e.label)).toEqual(NAV_DEFS.map((d) => d.label))
    const settings = inSection(entries, 'settings')
    expect(settings.map((e) => e.label)).toEqual(SETTINGS_META.map((m) => m.label))
    settings.find((e) => e.label === 'Hooks')!.run!()
    expect(h.navigate).toHaveBeenCalledWith('/settings/hooks')
  })

  it('lists "Search this transcript" (⌘F) only while a session\'s transcript is open', () => {
    const actions = { 'transcript.search': vi.fn() }
    const label = (c: ShortcutContext) =>
      inSection(buildPaletteEntries(host({ context: c, actions }), NO_DATA), 'actions').map((e) => e.label)
    expect(label(ctx())).not.toContain('Search this conversation')
    expect(label(ctx({ sessionOpen: true, transcriptOpen: false }))).not.toContain('Search this conversation')
    const open = inSection(
      buildPaletteEntries(host({ context: ctx({ sessionOpen: true, transcriptOpen: true }), actions }), NO_DATA), 'actions')
    const entry = open.find((e) => e.label === 'Search this conversation')!
    expect(entry).toBeDefined()
    entry.run!()
    expect(actions['transcript.search']).toHaveBeenCalled()
  })

  it('kiosk hides every section kiosk cannot reach, and every action whose `when` fails', () => {
    const h = host({
      context: ctx({ interactive: false }),
      actions: { 'app.help': vi.fn(), 'transcript.promptPrev': vi.fn() },
      live: [{ session: live('a', { branch: 'b1' }), scope: 'live' }],
    })
    const entries = buildPaletteEntries(h, { archived: [archived('z')], repos: [repo('o/r')] })
    const kioskNav = NAV_DEFS.filter((d) => d.showInKiosk).map((d) => d.label)
    expect(inSection(entries, 'navigate').map((e) => e.label)).toEqual(kioskNav)
    for (const s of ['sessions', 'settings', 'repos', 'branches'] as const) {
      expect(inSection(entries, s), s).toEqual([])
    }
    // app.help needs `interactive`; no session is open for the prompt jump; no New session.
    expect(inSection(entries, 'actions')).toEqual([])
  })

  it('a repo the list’s Project filter offers applies it; any other repo, and every branch, drills in the palette', () => {
    const h = host({
      canFilterRepo: (r) => r === 'o/one',
      live: [
        { session: live('a', { branch: 'feat/x' }), scope: 'live' },
        { session: live('b', { branch: 'feat/x' }), scope: 'live' },
        { session: live('c', { branch: 'main' }), scope: 'background' },
        { session: live('d'), scope: 'live' },
      ],
    })
    const entries = buildPaletteEntries(h, { archived: [], repos: [repo('o/one'), repo('o/two')] })
    const [one, two] = inSection(entries, 'repos')
    expect(one.drill).toBeUndefined()
    one.run!()
    expect(h.filterRepo).toHaveBeenCalledWith('o/one')
    expect(two.drill).toBe('repo:o/two')
    const branches = inSection(entries, 'branches')
    expect(branches.map((e) => e.label)).toEqual(['feat/x', 'main'])
    expect(branches.map((e) => e.drill)).toEqual(['branch:feat/x', 'branch:main'])
    expect(h.filterRepo).toHaveBeenCalledTimes(1)
  })

  it('a drill-down’s Sessions are EXACTLY the sessions on that repo or branch — background and archived included', () => {
    const h = host({
      live: [
        { session: live('fg', { name: 'Maintain the docs', repo: 'o/app', branch: 'feat/x' }), scope: 'live' },
        { session: live('bg', { repo: 'o/app-two', branch: 'main' }), scope: 'background' },
        { session: live('m', { repo: 'o/app', branch: 'main' }), scope: 'live' },
      ],
    })
    const entries = buildPaletteEntries(h, { archived: [archived('z', { repo: 'o/app' })], repos: [repo('o/app')] })
    expect(inSection(entries, 'sessions').map((e) => e.facets)).toEqual([
      { repo: 'o/app', branch: 'feat/x' }, { repo: 'o/app-two', branch: 'main' }, { repo: 'o/app', branch: 'main' },
      { repo: 'o/app' },
    ])
    const prepared = preparePalette(entries)
    const drill = (q: string) => paletteSections(prepared, q).map((s) => [s.id, s.entries.map((e) => e.label)])
    // Only Sessions survive a drill: nothing else carries the facet.
    expect(drill('branch:main')).toEqual([['sessions', ['Live bg', 'Live m']]])
    expect(drill('repo:o/app')).toEqual([['sessions', ['Maintain the docs', 'Live m', 'Old z']]])
    expect(drill('repo:o/app-two')).toEqual([['sessions', ['Live bg']]])
  })
})

describe('paletteSections', () => {
  it('an empty query shows the 8 most recent sessions (live and archived), then Actions — nothing else', () => {
    const lives = Array.from({ length: 6 }, (_, i) =>
      ({ session: live(`l${i}`, { updatedAt: `2026-11-0${i + 1}T10:00:00Z` }), scope: 'live' as const }))
    const olds = Array.from({ length: 6 }, (_, i) => archived(`z${i}`, { archivedAt: `2026-10-1${i}T10:00:00Z` }))
    const h = host({ live: lives, actions: { 'app.help': vi.fn() } })
    const sections = paletteSections(preparePalette(buildPaletteEntries(h, { archived: olds, repos: [repo('o/r')] })), '')
    expect(sections.map((s) => s.id)).toEqual(['sessions', 'actions'])
    expect(sections[0].entries).toHaveLength(RECENT_SESSIONS)
    expect(sections[0].entries.map((e) => e.label)).toEqual([
      'Live l5', 'Live l4', 'Live l3', 'Live l2', 'Live l1', 'Live l0', 'Old z5', 'Old z4',
    ])
  })

  it('a query searches every section, in section order', () => {
    const h = host({ live: [{ session: live('a', { name: 'Hook the deploy' }), scope: 'live' }] })
    const sections = paletteSections(preparePalette(buildPaletteEntries(h, NO_DATA)), 'hook')
    expect(sections.map((s) => s.id)).toEqual(['sessions', 'settings'])
    // Prefix (Hooks) before a keyword substring (Channels: "webhook").
    expect(sections[1].entries.map((e) => e.label)).toEqual(['Hooks', 'Channels'])
  })
})

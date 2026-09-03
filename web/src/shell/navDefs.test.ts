import { describe, it, expect } from 'vitest'
import { NAV_DEFS, NAV_BY_ID, navDefForPath } from './navDefs'

describe('navDefs', () => {
  it('preserves the legacy tab order and labels', () => {
    expect(NAV_DEFS.map((d) => [d.id, d.label])).toEqual([
      ['sessions', 'Sessions'],
      ['pipeline', 'Pipeline'],
      ['automations', 'Automations'],
      ['initiatives', 'Initiatives'],
      ['projects', 'Products'],
      ['archived', 'Archived'],
      ['insights', 'Insights'],
      ['settings', 'Settings'],
    ])
  })

  it('has unique ids and unique paths', () => {
    expect(new Set(NAV_DEFS.map((d) => d.id)).size).toBe(NAV_DEFS.length)
    expect(new Set(NAV_DEFS.map((d) => d.path)).size).toBe(NAV_DEFS.length)
  })

  it('gives every entry an absolute path and an icon', () => {
    for (const d of NAV_DEFS) {
      expect(d.path.startsWith('/'), `${d.id} path`).toBe(true)
      expect(typeof d.icon, `${d.id} icon`).toBe('object')
    }
  })

  // `help` is a required field, so the compiler already refuses a section with
  // none; this catches the way round the type cannot — an empty or placeholder
  // string added to silence it. `HelpSheet` renders these verbatim.
  it('gives every entry real help text', () => {
    for (const d of NAV_DEFS) {
      expect(d.help.trim().length, `${d.id} help`).toBeGreaterThan(20)
    }
  })

  it('only Sessions owns a list column', () => {
    expect(NAV_DEFS.filter((d) => d.hasList).map((d) => d.id)).toEqual(['sessions'])
  })

  /**
   * R9 successor for 'excludes Settings from kiosk and includes every other
   * section', which asserted the inverse rule — everything is kiosk-safe unless
   * excluded. That rule is what put `/sessions/:id` and `/archived` on an
   * unattended wall display, where Archive (`closeSession.mutate`), Unarchive
   * (`unarchiveSession.mutate`) and a pending permission card are all one tap
   * from a passer-by. `showInKiosk` now means "verified read-only" (the field's
   * docstring carries the criterion and each row its evidence), so the set is
   * pinned by EQUALITY rather than by exclusion: widening it has to edit this
   * line, which is the point at which the evidence gets asked for.
   */
  it('marks exactly the verified read-only sections kiosk-safe', () => {
    const inKiosk = NAV_DEFS.filter((d) => d.showInKiosk).map((d) => d.id)
    expect(inKiosk).toEqual(['pipeline', 'insights'])
  })

  it('NAV_BY_ID indexes every entry', () => {
    for (const d of NAV_DEFS) expect(NAV_BY_ID[d.id]).toBe(d)
    expect(Object.keys(NAV_BY_ID).length).toBe(NAV_DEFS.length)
  })

  // Guards the badge wiring in Task 6: every badge key a def declares must be a
  // key useNavBadges actually produces. Kept here (not in useNavBadges.test) so
  // the failure names the offending nav entry.
  it('declares only known badge keys', () => {
    const known = new Set(['sessions', 'pipeline', 'automations', 'archived', 'settings'])
    for (const d of NAV_DEFS) {
      if (d.badge !== undefined) expect(known.has(d.badge), `${d.id} badge=${d.badge}`).toBe(true)
    }
  })
})

describe('navDefForPath', () => {
  it('matches exact and nested paths', () => {
    expect(navDefForPath('/sessions')?.id).toBe('sessions')
    expect(navDefForPath('/sessions/abc123')?.id).toBe('sessions')
    expect(navDefForPath('/sessions/abc123/agents/x')?.id).toBe('sessions')
    expect(navDefForPath('/settings/general')?.id).toBe('settings')
  })

  it('does not match a path that merely shares a prefix string', () => {
    expect(navDefForPath('/sessionsfoo')).toBeNull()
  })

  it('returns null for an unknown path', () => {
    expect(navDefForPath('/nope')).toBeNull()
  })
})

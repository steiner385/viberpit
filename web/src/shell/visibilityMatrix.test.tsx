// The region-visibility matrix — umbrella §4.2's table, used as the fixture.
//
// Every one of the 28 mode × region cells appears in EXPECTED below, not just
// the interesting ones. A matrix test that covers only the conditional cells is
// how the unconditional ones break: `app.main` disappearing from kiosk, or
// `app.overlays` from phone, would be caught by nothing. The coverage assertion
// at the bottom iterates `LAYOUT_MODES` × `REGION_IDS` — both derived from the
// source, so adding a mode or a region reddens this file instead of silently
// leaving a new column unasserted.
//
// This file tests the RULES. `AppShell.test.tsx` tests that the rendered shell
// obeys them; neither substitutes for the other.
import { describe, it, expect } from 'vitest'
import { LAYOUT_MODES, type LayoutMode } from './useViewport'
import { APP_REGION_PREFIX } from '@/test/regions'
import { REGION_IDS, REGION_VISIBILITY, type RegionContext, type RegionId } from './AppShell'

/**
 * The context each cell is evaluated against unless the row overrides it: the
 * Sessions section, no item open, a drawer open. That is deliberately the
 * MOST-VISIBLE context, so the 28 base rows below read exactly like the spec's
 * table — a `1` in the table is `true` here with no override needed.
 */
const BASE: Omit<RegionContext, 'mode'> = {
  hasList: true,
  itemOpen: false,
  inSessions: true,
  drawer: 'help',
}

type Cell = [LayoutMode, RegionId, Partial<RegionContext>, boolean]

/** Umbrella §4.2, verbatim: 1 = rendered, 0 = not, D = danger-tone only.
 *
 * | Region       | phone            | tablet           | desktop          | kiosk |
 * |--------------|------------------|------------------|------------------|-------|
 * | app.banners  | 1                | 1                | 1                | D     |
 * | app.rail     | 1 (NavBar+TopBar)| 1 (NavRail)      | 1 (NavRail)      | 0     |
 * | app.list     | 1 when no item   | 1 when no item   | 1                | 0     |
 * | app.main     | 1                | 1                | 1                | 1     |
 * | app.dock     | 1 in Sessions    | 1 in Sessions    | 1 in Sessions    | 0     |
 * | app.drawer   | 1 when ?drawer=  | 1 when ?drawer=  | 1 when ?drawer=  | 0     |
 * | app.overlays | 1                | 1                | 1                | 1     |
 */
const EXPECTED: Cell[] = [
  // ── app.banners: present in every mode. Kiosk's "D" is a FILTER on the
  //    stack's contents (Task 19), not an absent region — pinned below.
  ['phone', 'app.banners', {}, true],
  ['tablet', 'app.banners', {}, true],
  ['desktop', 'app.banners', {}, true],
  ['kiosk', 'app.banners', {}, true],

  // ── app.rail
  ['phone', 'app.rail', {}, true],
  ['tablet', 'app.rail', {}, true],
  ['desktop', 'app.rail', {}, true],
  ['kiosk', 'app.rail', {}, false],

  // ── app.list
  ['phone', 'app.list', {}, true],
  ['tablet', 'app.list', {}, true],
  ['desktop', 'app.list', {}, true],
  ['kiosk', 'app.list', {}, false],

  // ── app.main: the one region nothing can remove.
  ['phone', 'app.main', {}, true],
  ['tablet', 'app.main', {}, true],
  ['desktop', 'app.main', {}, true],
  ['kiosk', 'app.main', {}, true],

  // ── app.dock
  ['phone', 'app.dock', {}, true],
  ['tablet', 'app.dock', {}, true],
  ['desktop', 'app.dock', {}, true],
  ['kiosk', 'app.dock', {}, false],

  // ── app.drawer
  ['phone', 'app.drawer', {}, true],
  ['tablet', 'app.drawer', {}, true],
  ['desktop', 'app.drawer', {}, true],
  ['kiosk', 'app.drawer', {}, false],

  // ── app.overlays: kiosk keeps the region and REFUSES dialogs inside it
  //    (Task 19, in ui/Dialog) — a hidden region would take the toast/portal
  //    root with it.
  ['phone', 'app.overlays', {}, true],
  ['tablet', 'app.overlays', {}, true],
  ['desktop', 'app.overlays', {}, true],
  ['kiosk', 'app.overlays', {}, true],

  // ── the three conditional cells, per mode ─────────────────────────────────

  // app.list — a section with no list column never renders one …
  ['phone', 'app.list', { hasList: false }, false],
  ['tablet', 'app.list', { hasList: false }, false],
  ['desktop', 'app.list', { hasList: false }, false],
  // … and an open item takes the single band on phone/tablet, but NOT on
  // desktop, which is master-detail.
  ['phone', 'app.list', { itemOpen: true }, false],
  ['tablet', 'app.list', { itemOpen: true }, false],
  ['desktop', 'app.list', { itemOpen: true }, true],
  ['desktop', 'app.list', { hasList: false, itemOpen: true }, false],
  ['kiosk', 'app.list', { itemOpen: true }, false],

  // app.dock — Sessions only (DEC-10).
  ['phone', 'app.dock', { inSessions: false }, false],
  ['tablet', 'app.dock', { inSessions: false }, false],
  ['desktop', 'app.dock', { inSessions: false }, false],
  ['kiosk', 'app.dock', { inSessions: false }, false],

  // app.drawer — only with a recognised `?drawer=`. `useDrawerName()` already
  // maps an unknown value to null, so null is the whole of "no drawer".
  ['phone', 'app.drawer', { drawer: null }, false],
  ['tablet', 'app.drawer', { drawer: null }, false],
  ['desktop', 'app.drawer', { drawer: null }, false],
  ['kiosk', 'app.drawer', { drawer: 'assistant' }, false],

  // The unconditional regions stay unconditional under every override.
  ['kiosk', 'app.main', { hasList: false, inSessions: false, drawer: null }, true],
  ['kiosk', 'app.banners', { hasList: false, inSessions: false, drawer: null }, true],
  ['kiosk', 'app.overlays', { hasList: false, inSessions: false, drawer: null }, true],
]

describe('REGION_VISIBILITY', () => {
  it.each(EXPECTED)('%s / %s %o → %s', (mode, id, overrides, expected) => {
    const ctx: RegionContext = { ...BASE, mode, ...overrides }
    expect(REGION_VISIBILITY[id](ctx)).toBe(expected)
  })

  it('covers every mode × region cell', () => {
    const covered = new Set(EXPECTED.map(([mode, id]) => `${mode}|${id}`))
    const missing: string[] = []
    for (const mode of LAYOUT_MODES) {
      for (const id of REGION_IDS) {
        if (!covered.has(`${mode}|${id}`)) missing.push(`${mode}|${id}`)
      }
    }
    expect(missing, `uncovered cells:\n${missing.join('\n')}`).toEqual([])
    expect(LAYOUT_MODES.length * REGION_IDS.length).toBe(28)
  })

  // DEC-1. `AppShell.test.tsx` and `kiosk.integration.test.tsx` compare the
  // document's regions against this matrix scoped to the `app.` prefix, because
  // pages own their own namespaces (`session.header`, Task 10) and the matrix
  // cannot answer for a region the shell neither places nor hides. That scoping
  // is only sound while every region the matrix DOES answer for is inside the
  // prefix — a shell region renamed to `chrome.dock` would silently drop out of
  // both comparisons instead of failing. This is the assertion that stops that.
  it('REGION_IDS is app.* only — the namespace the shape assertions scope to', () => {
    const outside = REGION_IDS.filter((id) => !id.startsWith(APP_REGION_PREFIX))
    expect(outside, `shell regions outside "${APP_REGION_PREFIX}": ${outside.join(', ')}`).toEqual([])
  })
})

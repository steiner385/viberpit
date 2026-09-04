// "Which shell regions are on screen?" — in one place.
//
// `AppShell.test.tsx` owned this as a local function and `kiosk.integration.test.tsx`
// needed the identical thing. The two copies would have been byte-identical on
// the day they were written and would have drifted the first time one of them
// learned about a new portal target: the reason the whole-DOCUMENT query below
// exists is that `app.overlays` is stamped on the portal root and `app.drawer`'s
// overlay panel portals into it, both OUTSIDE the render container — a second
// copy that scoped itself to `container` would silently report those regions
// missing and pass a kiosk "the drawer is hidden" assertion for the wrong reason.

/**
 * Every `data-region` node in the DOCUMENT, counted. Document-wide, not
 * container-scoped — see the note above.
 */
export function regionCounts(): Map<string, number> {
  const counts = new Map<string, number>()
  for (const el of document.querySelectorAll('[data-region]')) {
    const id = el.getAttribute('data-region')!
    counts.set(id, (counts.get(id) ?? 0) + 1)
  }
  return counts
}

/**
 * The prefix the SHELL's own regions carry (DEC-1).
 *
 * `app.*` regions are placed by `AppShell` and answered for by
 * `REGION_VISIBILITY` — the shell decides whether each one is on screen, so a
 * set-equality assertion over them is a real contract. `session.*` (and any
 * later page namespace) is PAGE-owned: `SessionHeader` carries
 * `data-region="session.header"` whenever the session page renders, and the
 * shell neither places nor hides it. Comparing the shell's matrix against a
 * document that also contains page regions would make the matrix answer for
 * regions it does not control, so the shape assertions scope to this prefix.
 *
 * `visibilityMatrix.test.tsx` pins the other half: every id in `REGION_IDS`
 * starts with this prefix. Without that, scoping here could quietly stop
 * covering a shell region that had been renamed out of the namespace.
 */
export const APP_REGION_PREFIX = 'app.'

/** `regionCounts()`, restricted to the shell's own regions. */
export function appRegionCounts(): Map<string, number> {
  const counts = new Map<string, number>()
  for (const [id, n] of regionCounts()) {
    if (id.startsWith(APP_REGION_PREFIX)) counts.set(id, n)
  }
  return counts
}

/** The SHELL region ids on screen, sorted — what the shape assertions compare. */
export function presentAppRegions(): string[] {
  return [...appRegionCounts().keys()].sort()
}

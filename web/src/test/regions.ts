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

/** The region ids on screen, sorted — the shape assertions compare against. */
export function presentRegions(): string[] {
  return [...regionCounts().keys()].sort()
}

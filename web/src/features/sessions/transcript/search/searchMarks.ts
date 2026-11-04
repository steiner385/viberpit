/** The search marks' look and hook (D-11, lazy) — shared by both renderers. */

/** Every match: a translucent warning tone, so it shows on every prose surface — the
 *  canvas, the accent-washed user bubble, the info-washed automated prompt and the
 *  warning-washed insight callout alike — in both themes. */
export const MARK_CLASS = 'rounded-sm bg-warning/30 text-ink'
/** The current hit: the solid strong tone with its on-strong ink (AA), so it stands
 *  out among the other marks. */
export const MARK_CURRENT_CLASS = 'rounded-sm bg-warning-strong text-on-strong'
/** Set on the current hit's `<mark>`, for the jump (centring a tall turn on it), tests
 *  and e2e to find it. Spelled out again in `TranscriptView`'s eager selector. */
export const MARK_CURRENT_ATTR = 'data-search-current'

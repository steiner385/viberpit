# Mobile session-card declutter — design

**Date:** 2026-06-24
**Component:** `web/src/components/SessionCard.tsx` (+ its test file)
**Status:** approved (brainstorming) — pending spec review

## Problem

On a phone, every session card renders all three info rows plus a five-icon action
cluster, none of which is hidden by viewport. The card layout has no Tailwind
breakpoints by design (it lives in a drag-resizable desktop sidebar, so it relies on
flex truncation to reflow at any *container* width). The side effect: on a ~360px
phone the card crams title + account chip + repo + status pill + Goal badge + PR badge
+ deploy/rocket count + usage `%` bar + "active Xm ago" + up to five 44px action icons
into one card. It reads as cluttered.

## Goal

On mobile, show only the at-a-glance essentials; keep the full card on desktop.
Nothing is deleted — demoted info remains reachable by tapping into the session
detail view.

### Keep on mobile (viewport < 1024px)
- Session title
- Account chip
- Status pill
- A single **resume** action (the `Play` button)

### Desktop-only (viewport ≥ 1024px)
- Goal badge
- PR badge + deploy/rocket count (`<PrList>`)
- The entire telemetry row (`card-telemetry`): usage `%` bar + "active Xm ago"
- The suggestions chevron, rename, archive, and delete action buttons

## Approach — CSS-only via Tailwind `lg:` (min-width 1024px)

Demoted elements get `hidden lg:<display>` so they are absent on phones and reappear
on desktop. No JavaScript, no `useMediaQuery`, no prop threading, no extra re-renders.

**Why `lg:` is correct here despite the "no breakpoints" rule:** the original rule
avoids breakpoints because the desktop sidebar is *resizable* — a breakpoint can't
tell whether the sidebar container is 200px or 800px. But `lg:` keys off **viewport**
width, and `App.tsx:91` already splits the desktop master-detail shell from the mobile
routed views at exactly `min-width: 1024px`. So `lg:` is a 1:1 proxy for "am I in the
desktop shell?" The desktop sidebar, however narrow it is dragged, always lives inside
a ≥1024px viewport — so per-element flex truncation continues to handle sidebar
resizing on desktop, untouched. Mobile vs desktop divergence is the *only* thing `lg:`
adds.

Rejected alternatives:
- **`useMediaQuery` prop threaded from the shell** — more plumbing and re-renders for
  no benefit over a pure-CSS class.
- **Container queries** — we specifically want viewport ("phone or not"), not the
  card's container width.

## Concrete changes (`SessionCard.tsx`)

1. **Row 2 (`card-context`):** repo label + `StatusPill` unchanged. Wrap the `Goal`
   badge and `<PrList>` so each renders only at `lg:` (e.g. `hidden lg:inline-flex`
   on the Goal `<span>`; wrap `<PrList>` in a `hidden lg:inline-flex` span). The
   right-hand chip group then shows just the status pill on mobile.
2. **Row 3 (`card-telemetry`):** keep the existing render condition; add
   `hidden lg:flex` to the row wrapper so it is gone on mobile, restored on desktop.
3. **Action cluster:** resume `Play` button stays inline at all widths. Wrap the
   suggestions chevron block, rename, archive, and delete buttons so they are
   `hidden lg:flex` / `lg:inline-flex`. Desktop's existing hover-to-reveal behavior
   (`[@media(hover:hover)]:opacity-0` …) is unchanged.

Keyboard shortcuts in `handleKeyDown` (rename/archive/delete via list-focus mode) are
**not** gated — they are desktop-keyboard affordances and harmless on mobile where no
hardware keyboard fires them.

## Stranding check (verify during implementation)

Hiding rename/archive/delete from the mobile list means those actions must be reachable
elsewhere on mobile. **Verify the session detail/`SessionView` route exposes rename,
archive, and delete on mobile.** If any is missing there, surface it before merging —
do not silently strand a destructive action. (Archive/delete are also available from
the Archived tab for already-archived sessions, but that does not cover archiving a
*live* session from mobile.)

## Testing

The card already has jsdom tests asserting className contracts. Add assertions that
lock the mobile/desktop contract:
- Demoted elements (`card-telemetry`, the Goal badge, the `PrList` wrapper, and the
  rename/archive/delete buttons) carry `hidden` + an `lg:` class.
- Essentials (title, account chip, `StatusPill`, resume button) carry **no** `hidden`
  class.

Run the full web gate after: `vitest run`, `tsc -b --force` (the real type gate —
`tsc --noEmit` checks nothing here), and `eslint` with `--max-warnings 0`.

## Out of scope

- No change to desktop layout or desktop hover behavior.
- No new design tokens, no overflow/kebab menu, no JS viewport hook.
- No change to which elements render based on session state (busy, suggestions count,
  etc.) — only the viewport gating is added.

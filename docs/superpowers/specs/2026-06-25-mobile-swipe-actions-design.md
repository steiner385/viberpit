# Mobile swipe actions for session cards — Design

**Date:** 2026-06-25
**Status:** Approved (brainstorming), pending implementation plan
**Component:** `web/src/components/SessionCard.tsx` + new `useSwipeAction` hook

## Problem

On touch/mobile widths (`< lg`), a `SessionCard` shows only the Resume (Sparkles)
action inline. Rename/Archive/Delete are gated behind `hidden lg:flex` (the
`card-secondary-actions` group), so mobile users have **no card-level way to archive
or delete a session** — they must open the session detail view first.

The underlying actions already exist as card props:
- `onClose(id)` — archive (stops the driver, moves to the Archived tab; reversible).
- `onDelete(id)` — permanent delete; the parent (`Dashboard`) routes this through the
  existing `PermanentDeleteDialog` confirmation.

This feature adds a **new touch gesture surface** over those existing actions. No
backend, mutation, or API work.

## Goals

- Swipe a session card left or right on mobile to reach Delete / Archive.
- Reuse the existing archive (`onClose`) and delete (`onDelete` → `PermanentDeleteDialog`)
  flows unchanged.
- Keep desktop (`>= lg`) cards byte-for-byte unchanged.
- Keep the gesture math isolated and unit-testable.

## Non-goals (YAGNI)

- No swipe on the Archived tab cards (active list only).
- No swipe on desktop / hover-capable pointers.
- No new undo/restore path — Delete keeps its confirm dialog rather than an undo toast.
- No new dependency (`framer-motion` / `react-swipeable` / `use-gesture`).

## Decisions (from brainstorming)

| Decision | Choice |
|----------|--------|
| Interaction model | **Hybrid**: a short swipe past a *reveal* threshold opens a button to tap; a long full-swipe past a *commit* threshold fires the action directly. |
| Direction mapping | **Left swipe → Delete** (red panel on the right edge). **Right swipe → Archive** (panel on the left edge). Matches iOS Mail (destructive on left-swipe). |
| Delete safety | Full-swipe **and** button-tap both route through the existing `PermanentDeleteDialog`. Swipe is just a faster path to the same confirm step. |
| Archive safety | Commits directly (reversible — lands in Archived tab). |
| Scope | Active session list, **mobile only** (`< lg`). |

## Architecture

**Gesture mechanism:** hand-rolled Pointer Events (no library). Rationale: no swipe
lib is installed, the constitution favors minimal vetted deps (VII), a single component
doesn't justify a dependency, and Pointer Events give one code path for touch plus full
control over axis-locking. (Rejected alternatives: CSS scroll-snap — can't express
"full-swipe commits"; a library — overkill for one card.)

**`useSwipeAction` hook** (new, in `web/src/lib/` or `web/src/components/session/`).
Owns all gesture state + math. Session-agnostic.

- Input: `{ revealPx, commitFraction, onCommitLeft, onCommitRight, enabled }`.
- Output: `{ dx, openSide, handlers }` where `handlers` are the
  `onPointerDown/Move/Up/Cancel` to spread onto the foreground element, `dx` is the
  current horizontal offset, and `openSide` is `'left' | 'right' | null` (the snapped-open
  state).
- Knows nothing about sessions — `SessionCard` passes session-bound callbacks.

**`SessionCard` changes (mobile branch only, gated on `!isWide`):**
- Wrap the existing card content (the inner flex row) as a *translatable foreground*
  with `transform: translateX(dx)`.
- Behind it, two absolutely-positioned, full-height action panels:
  - Right edge: red **Delete** panel (`Trash2`), revealed by swiping left.
  - Left edge: **Archive** panel (`Archive`), revealed by swiping right.
- The revealed panel contains a real `<button>` (committing the action on tap) so the
  reveal step is operable without a full swipe.
- `useMediaQuery('(min-width: 1024px)')` (the app's existing `lg` definition, already
  used in `App.tsx`) gates whether the swipe layer mounts at all.

## Gesture mechanics

1. `pointerdown`: record start x/y. Do not engage yet.
2. First `pointermove`: if `|dx| > |dy| + bias` → **lock horizontal**, `setPointerCapture`,
   begin translating. Otherwise yield to vertical list scroll and never engage this gesture.
   `touch-action: pan-y` on the foreground lets the browser own vertical scroll until lock.
3. While locked, `translateX` follows the finger (optionally rubber-banded past the panel
   width).
4. `pointerup`:
   - past **commit threshold** (~45% of card width) → fire the side's action directly
     (Archive → `onClose`; Delete → `onDelete` → dialog), snap closed.
   - past **reveal threshold** (~64px) but under commit → snap *open* to the panel width;
     the revealed button is now tappable. Tapping it commits; tapping elsewhere or swiping
     back snaps closed.
   - under reveal → snap closed.
5. A horizontal-locked gesture **suppresses the card's `onClick` open**, so a swipe never
   also opens the session.
6. `prefers-reduced-motion`: skip the transition; snap instantly.

Thresholds (`revealPx`, `commitFraction`) are hook inputs so tests can pin them and they
stay tunable.

## Accessibility

Swipe is additive and touch-only — not the sole path to any action. The revealed panels
are real `<button>`s with the same `aria-label`s as the desktop buttons
(`Archive session <name>`, `Delete session <name>`), so screen-reader and keyboard users
keep existing affordances. List-mode keyboard shortcuts (`matchListShortcut`: archive,
permanent-delete) are unchanged.

## Testing (TDD)

**`useSwipeAction` pure-logic tests:**
- Axis lock: vertical-dominant first move never engages; horizontal-dominant locks.
- Reveal vs commit thresholds: release distances map to closed / open / committed.
- Direction → callback: left past commit calls `onCommitLeft`; right calls `onCommitRight`.
- Snap-back below reveal.

**`SessionCard` jsdom tests:**
- Full left-swipe pointer sequence → `onDelete` fires.
- Full right-swipe → `onClose` fires.
- Short swipe reveals the panel; tapping the revealed button fires the action.
- A vertical drag does NOT engage and does NOT open the session.
- At `lg` (wide), the swipe layer is not rendered and desktop buttons are unchanged.

## Files touched

- `web/src/lib/useSwipeAction.ts` (or `web/src/components/session/useSwipeAction.ts`) — new hook.
- `web/src/components/SessionCard.tsx` — mobile swipe layer + foreground wrapper.
- `web/src/lib/__tests__/useSwipeAction.test.ts` — new hook tests.
- `web/src/components/__tests__/SessionCardSwipe.test.tsx` — new card-level tests.
- No backend changes.

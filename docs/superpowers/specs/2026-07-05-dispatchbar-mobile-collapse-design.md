# DispatchBar mobile selector collapse — design

**Date:** 2026-07-05
**Status:** approved (user request: "collapse the three drop-downs to a single line on mobile, or drop model/account into a 'more' menu")

## Problem

On mobile the DispatchBar renders Repository, Account, and Model as three stacked
full-width labelled selects (`grid grid-cols-1 sm:grid-cols-3`). That block eats
~180px of vertical space above the prompt textarea, pushing the session list off
screen. Account is almost always "Auto-select" and Model "Default"; Repository is
the only per-dispatch pick.

## Design

Hybrid of the user's two suggestions — a single line *and* a more-menu:

- **Mobile (`< sm`)**: one row — Repository select (flex-1) + a 44px "More
  options" toggle button (`⋯`). Account + Model render below only while
  expanded. Repository's visible label is hidden on mobile (`hidden sm:block`);
  the select keeps its `aria-label`.
- **Indicator**: when collapsed and account ≠ `''` (Auto-select) or model ≠
  `'default'`, the toggle shows a small dot so a hidden non-default choice is
  never invisible at dispatch time.
- **Desktop (`sm+`)**: unchanged 3-across grid with labels; the toggle is
  `sm:hidden`.

## Implementation notes

- Visibility is CSS-only (`hidden sm:flex` on the Account/Model wrappers,
  flipped to `flex` by a `showMore` state). All three selects stay mounted, so
  existing jsdom tests that query them by role/label are unaffected; real
  layout behavior is Playwright territory per project convention.
- A11y: button gets `aria-expanded` + `aria-controls="dispatch-bar-account
  dispatch-bar-model"`; dot carries sr-only text.
- No new state escapes the component; dispatch payload semantics unchanged.

## Testing

- Toggle button toggles `aria-expanded` (and the wrappers' class state).
- Dot appears only when collapsed with a non-default account or model.
- Existing DispatchBar suites (dispatch payload, images, voice) pass untouched.

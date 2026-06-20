# Accessibility (WCAG 2.1 AA)

## Scope and conformance target

**Management surfaces** (Dashboard, session list, account strip, SessionActions overflow menu,
EmptyState, AllLimitedBanner, UsageBadge/StatusPill) target **WCAG 2.1 Level AA** in full.

**Terminal surface** (xterm.js PTY view) is **partial conformance** — see the documented exception
below. All other surfaces are fully AA-conformant.

---

## What was audited

Automated audit via **axe-core** (`jest-axe` v10, Vitest/jsdom) covering:

| Component | States tested |
|-----------|--------------|
| `AllLimitedBanner` | with reset time, with null reset |
| `EmptyState` | default |
| `SessionList` | empty, loading skeleton, 2-session list |
| `UsageBadge` | low (20%), high (95%), no label |
| `StatusPill` | all 5 account statuses, all 4 session statuses |

Tests live in `web/src/components/__tests__/a11y.test.tsx` and run as part of `npm run test`.

---

## Violations found and fixed

### `aria-label` on `<div>` with no valid role (axe rule: `aria-prohibited-attr`)

**Component:** `SessionList` loading skeleton container.

**Before:**
```tsx
<div className="space-y-2" aria-label="Loading sessions" aria-busy="true">
```

**After (fixed):** Added `role="status"` so `aria-label` is valid and the element is announced
by screen readers as a live region when content changes.

```tsx
<div className="space-y-2" role="status" aria-label="Loading sessions" aria-busy="true">
```

**Impact:** Screen readers (NVDA, VoiceOver) would previously ignore or mis-announce the loading
state. With `role="status"` the container is a live region and `aria-label` is permitted.

---

## Design commitments (management surfaces)

- **Color is never the sole means of conveying status.** `StatusPill` always pairs an icon with
  a text label (`OK`, `Limited`, `Needs login`, etc.); `UsageBadge` includes a numeric percentage.
- **Touch targets ≥ 44 × 44 px** (`min-h-[44px]`) on all interactive elements.
- **Focus management:** dialogs trap focus; closing a dialog returns focus to the trigger
  element (verified in `SessionList.test.tsx` and `SessionActions.test.tsx`).
- **Keyboard navigation:** all interactive elements reachable by Tab; overflow menus are
  navigable with arrow keys and closed with Escape.
- **ARIA roles and labels:** dialogs use `role="dialog"` + `aria-modal="true"` +
  `aria-labelledby`; menus use `role="menu"` + `role="menuitem"`; lists use `<ul>` + `<li>`.

---

## Terminal surface — documented partial conformance

The xterm.js terminal (PTY live stream) **cannot achieve full WCAG 2.1 AA conformance** due to
the nature of a raw interactive terminal. Applicable exceptions under WCAG Conformance Claim:

- **1.1.1 Non-text content** — the PTY byte stream has no text alternative; applying one would
  destroy the live-editing experience (WCAG "essential" exception applies).
- **2.2.2 Pause, Stop, Hide** — live terminal output cannot be paused without breaking
  interactivity (WCAG "essential live process" exception). A future enhancement may add an
  output-pause toggle.

**Mitigations in place:**
- An `aria-live="polite"` recent-output region exposes the last N lines of output to screen
  readers (xterm.js accessibility addon).
- A "Back to sessions" keyboard-accessible link is always visible, returning focus to the
  management surface.
- The helper-key bar provides touch-accessible Ctrl-C / Escape equivalents.

This partial conformance is documented per WCAG 2.1 Conformance Claim guidance
(see research note R6 in `specs/001-ccswitch-web/research.md`).

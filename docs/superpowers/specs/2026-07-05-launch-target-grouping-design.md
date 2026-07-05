# Launch-target grouping in the New Session dialog

**Date:** 2026-07-05
**Status:** Approved — ready for planning
**Area:** `web/src/components/Dashboard.tsx` (frontend only)

## Problem

The New Session dialog already has Account and Model dropdowns, fully wired to the
backend (account → `CLAUDE_CONFIG_DIR`, model → `driver.set_model()`), but users can't
find them. The form is a tall vertical stack:

```
Name → Filter repos → Repository → Branch → Account → Tool approvals → Model → Initial prompt
```

Account sits just below Branch, but **Model is buried below "Tool approvals"**, nowhere
near the repo chooser. Nothing groups these "what am I launching" controls together, and
on a `max-w-md` mobile-first modal the Model dropdown is often below the fold. A user who
starts sessions regularly did not know the dropdowns existed.

This is a **discoverability + grouping** change. No new functionality — the dropdowns,
state, submit payload, and API already exist and stay unchanged.

## Goal

Cluster the launch-configuration controls into one visually distinct group directly under
the repo chooser, so Account and Model are obvious and above the fold: "drop-downs right
next to the repo chooser."

## Design

Frontend-only edit to `NewSessionDialog` in `web/src/components/Dashboard.tsx`. Pure JSX
reordering plus wrapper elements — no state, handler, hook, or API changes.

### New form order

```
Name
┌─ Launch target ─────────────────────────────┐   ← subtle bordered group + heading
│ Filter repos                                 │
│ Repository                                    │
│ Branch                                        │
│ [ Account ▾ ]        [ Model ▾ ]              │   ← side-by-side, moved up
└──────────────────────────────────────────────┘
Tool approvals
Initial prompt
```

### Changes

1. **Move** the Model `<div>` block (currently ~lines 309-328) up to sit immediately after
   the Account block. Delete it from its old position (no duplicate — single source).
2. **Wrap** Account + Model in a responsive 2-column container:
   `grid grid-cols-1 sm:grid-cols-2 gap-3`. Side-by-side on desktop (≥640px), auto-stacking
   to full-width on narrow phones so neither dropdown is cramped.
3. **Group box:** wrap Filter repos + Repository + Branch + the Account/Model grid in a
   `<fieldset>` with a subtle border (matching existing `border-gray-200 dark:border-gray-600`
   tokens, rounded) and a `<legend>` reading **"Launch target"**. Use a real
   `fieldset`/`legend` for semantics/accessibility, styled to match the dialog. Reuse the
   existing `inputClass` for the selects — no new design tokens (single source of truth).

### Explicitly unchanged

- `handleSubmit` payload (`name`, `account`, `initialPrompt`, `repo`, `branch`,
  `permissionMode`, `model`) — identical.
- All `useState` hooks, `useModels`/`useGithubRepos`/`useGithubBranches` usage, and the
  "default" → omit-model rule.
- Backend, API contract, orchestrator — untouched.
- The focus-trap `querySelectorAll` already targets `button, input, select, textarea` and
  keeps working regardless of DOM order/nesting; `fieldset` does not break it.

## Testing

- `web/` → `vitest run`, `tsc --noEmit`, `eslint`.
- Existing `Dashboard` test mocks stay valid (no new hooks introduced).
- Add/adjust an assertion that Account and Model render inside the "Launch target" group,
  and that Model now appears before "Tool approvals" in DOM order.
- Manual check at mobile (<640px) and desktop widths: dropdowns stack vs. sit side-by-side;
  group border/legend renders in light and dark mode.

## Risks

Minimal — cosmetic reordering. Main watch-items: (a) `fieldset`/`legend` default browser
styling can fight Tailwind — reset with utility classes; (b) keep the `gap-3` grid from
overflowing the `max-w-md` modal at the narrow end (the `grid-cols-1` fallback covers this).

## Out of scope

- Mid-session model/account switching (separate feature, already designed
  2026-06-22).
- Changing the account auto-select or model-default semantics.

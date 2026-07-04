# Plotroom-style progress bars for CI check panels

**Date:** 2026-07-04
**Status:** Approved design, ready for implementation plan
**Scope:** `web/` frontend only. No backend or API-type changes.

## Problem

The CI drill-down bars (`CheckRowList`) and the StageTrack CI band **self-normalize per
row**: `scale = elapsedS + highS`, computed independently for each bar
(`web/src/lib/etaBand.ts`). Consequences:

- Every *completed* job renders as a **100%-full bar** regardless of how long it took. A 26s
  job and a 3m job look identical.
- Bar **length carries no information** — color is the only signal. You cannot eyeball the
  critical path.

The reference implementation ("plotroom",
`/mnt/ssk-ssd/tony/GitHub/pr-dashboard/frontend/src/CheckGantt.tsx`) instead uses **one shared
time scale per panel**, turning the panel into a Gantt/waterfall where bar length encodes
duration. ccswitch's own backend already treats plotroom as its source-of-truth check shape
(`server/github/parity/normalize.py`).

## Goal

Adopt the plotroom shared-scale Gantt method for:

1. The CI check panels (`CheckRowList`, both `head` and `merge` groups).
2. The StageTrack CI-node band (visual consistency).

Full-fidelity visual replication of plotroom, **except** the sub-states that require backend
fields ccswitch does not have (see Non-goals).

## Data mapping (no backend change)

ccswitch `CheckRow` stores **remaining** estimates; plotroom uses **total expected**. Derive
totals on the client:

| plotroom `CheckView` | ccswitch `CheckRow` source |
|----------------------|----------------------------|
| `elapsedSeconds`     | `duration_seconds` (live-adjusted via `liveElapsedS` while running) |
| `expectedLowSeconds` | `duration_seconds + rem_low_seconds` |
| `expectedSeconds`    | `duration_seconds + rem_nominal_seconds` |
| `expectedHighSeconds`| `duration_seconds + rem_high_seconds` |
| `status`             | `status` (`QUEUED` / `IN_PROGRESS` / `COMPLETED`) |
| `conclusion`         | `conclusion` (lowercased: `success` / `failure` / `pending` / `skipped` / …) |

Fields are nullable; when remaining estimates are absent, band/tick are omitted (bar still
renders fill + label).

## Design

### 1. Shared-scale math — extend `web/src/lib/etaBand.ts` (do not fork)

- `expectedTotals(row)` → `{ expLow, expNominal, expHigh }` from the mapping above (null-safe).
- `ganttScale(rows, updatedAt, now)` → `max` over all rows of `(liveElapsed, expHigh)`, floored
  at 60s (plotroom's `ganttScale`). Computed **once per panel**.
- `ganttSegments({ elapsed, expLow, expNominal, expHigh, scale })` →
  `{ fillPct, bandLeftPct, bandWidthPct, tickPct }`, every value divided by the **shared**
  `scale` (not the per-row scale). Returns `fillPct` only when band inputs are absent.

Keep the existing `bandSegments` / `liveElapsedS` / `isOverrun` exports; `ganttSegments` is the
shared-scale sibling. Migrate call sites, then remove `bandSegments` if it has no remaining
consumers (no half-dead code — CLAUDE.md).

### 2. New shared primitive — `web/src/components/GanttBar.tsx`

The three-span bar markup is currently **duplicated inline** in `CheckRowList` and
`StageTrack`. Extract one component:

- Props: `{ fillPct, bandLeftPct?, bandWidthPct?, tickPct?, colorClass, textureClass?, ariaNow }`.
- Renders track + band (optional) + fill + tick (optional), `role="progressbar"`.
- Both `CheckRowList` and `StageTrack` render through it. Delete the inline copies.

### 3. State → color + texture — extend `web/src/lib/checkState.ts`

- Extend `CheckVisualState` with `'skipped'` and `'queued'`.
- Keep `checkStateColor` returning the existing `bg-*` tokens; add `skipped`/`queued` → muted
  gray.
- Add `checkStateTexture(state)` returning CVD-safe overlays (Tailwind arbitrary
  `repeating-linear-gradient`):
  - `overdue` → 90° white stripes over amber.
  - `failed` → −45° white stripes over red.
  - others → none.
- `queued` renders as the fixed **15% stub** (fill width forced to 15%, per plotroom).

State resolution (in `CheckRowList`, extending current `visualState`):
`overdue` (live) > `failed` (`conclusion==='failure'`) > `done` (`conclusion==='success'`) >
`skipped` (`conclusion==='skipped'`) > `active` (`status IN_PROGRESS`/`pending`) >
`queued` (`status QUEUED` / no elapsed).

### 4. Labels — `CheckRowList`, matching plotroom `timeText`

- `done` → `"<elapsed> ✓"`
- `running` → `"<elapsed> / ~<expected>"`
- `overdue` → `"<elapsed> ⚠ overdue"`
- `failed` → `"<elapsed> ✗"`
- `skipped` → `"–"`
- `queued` → `"—"`

`formatEta` already covers durations. Scale is computed once and passed to each row (rows do
not each recompute it).

### 5. StageTrack band

Single bar → its own scale is the natural scale (no sibling panel). Route it through
`<GanttBar>` and pick up the overdue-stripe texture so it matches the panel. Preserve the
`data-testid="ci-band"` used by existing tests.

## Non-goals (explicit omissions)

plotroom has `runner-wait` / `runner-wait-amber` diagonal patterns, flake annotations
(`✗ · ↻ re-run in progress`), and "blocked on X" queued text. ccswitch's `CheckRow` carries
**no** fields for wait-kind, flake, or blocker. These are **out of scope** and tracked as a
follow-up issue (would require backend `CheckRow` additions). Bars degrade gracefully to plain
`queued` / `failed`.

## Testing (TDD)

- `etaBand.test.ts`: `expectedTotals` (null-safety), `ganttScale` (a 26s + 3m panel → 26s bar
  ≈ 0.14× the 3m bar), `ganttSegments` (shared denominator, band/tick positions).
- `checkState.test.ts`: state resolution incl. `skipped`/`queued`; `checkStateTexture` classes.
- `GanttBar` render test: band/fill/tick geometry, `role="progressbar"`.
- `CheckRowList` render test: queued = 15% stub; two-job shared-scale proportionality; each
  label format.
- StageTrack: existing `ci-band` test stays green.

Gates: `vitest run`, `tsc --noEmit`, `eslint` (run in `web/`).

## Files

- `web/src/lib/etaBand.ts` (extend)
- `web/src/lib/checkState.ts` (extend)
- `web/src/components/GanttBar.tsx` (new)
- `web/src/components/CheckRowList.tsx` (rewrite bar + labels)
- `web/src/components/StageTrack.tsx` (route through GanttBar)
- Tests alongside each.

# M-Track-8: Live-ticking progress bars — Design

**Date:** 2026-06-30
**Status:** Approved (design); pending user review → implementation plan
**Program:** Visual Pipeline Track — see
`docs/superpowers/specs/2026-06-27-visual-pipeline-track-overview-design.md`. Makes the M5/M6 bars
(drill-down rows + M3/M6 CI-node connector band) advance second-by-second between polls, like Plotroom.

---

## 1. Goal

Today a running check's bar is a **snapshot**: `CheckRow.duration_seconds` (elapsed) and the band are
computed server-side at poll time and only change when the backend re-polls GitHub (~75s) and the
frontend refetches (`useAllPrs` every 10s). So the bars never appear to *move*. Make running bars
advance smoothly every second between polls — both the **per-check drill-down rows** and the
**collapsed track's CI-node connector band** (user decision: both).

## 2. Key mechanism (pure frontend — no backend/wire change)

`CheckRow.duration_seconds` is the elapsed **at poll time**, and react-query exposes `dataUpdatedAt`
(when the browser received that `/api/prs` response). So the live elapsed is simply:

```
displayElapsed = duration_seconds + max(0, (nowMs − dataUpdatedAt) / 1000)
```

A 1-second `now` ticker re-renders the bars; `bandSegments` is recomputed from `displayElapsed`. When the
next poll lands, `dataUpdatedAt` jumps, the delta resets to ~0, and the bar snaps to fresh server truth —
no drift accumulation. **No backend, no new wire field, no `started_at` needed.**

## 3. Decisions (locked during brainstorming)

1. **Scope:** both the drill-down rows (`CheckRowList`) AND the collapsed CI-node connector band
   (`StageTrack`) tick.
2. **Tick rate:** 1 second.
3. **Pause on hidden tab:** the ticker stops while `document.hidden` (visibilitychange) and resumes when
   visible — self-corrects on the next poll regardless.
4. **Overrun → pin full + overdue tint:** when `displayElapsed` passes the upper (p90) estimate, the
   solid clamps to 100% (already handled by `bandSegments`) and the bar switches to the amber `overdue`
   color + ⚠.

## 4. Architecture

### 4.1 `useNow` hook — `web/src/hooks/useNow.ts`
```
useNow(intervalMs = 1000): number   // returns nowMs, advanced on an interval
```
- `Date.now()` is read **inside an effect / interval callback**, never during render (the
  react-hooks/purity rule the codebase already follows in `CondensingProgress`/`WorkingIndicator`).
- The interval is created in a `useEffect`; a `visibilitychange` listener **clears the interval when
  `document.hidden` and restarts it (with an immediate `setNow(Date.now())`) when visible**. Cleanup
  removes both the interval and the listener.

### 4.2 Pure tick helpers — `web/src/lib/etaBand.ts` (alongside `bandSegments`)
```
liveElapsedS(snapshotS: number, updatedAtMs: number, nowMs: number): number
   = snapshotS + Math.max(0, (nowMs - updatedAtMs) / 1000)
isOverrun(liveDeltaS: number, remHighS: number | null): boolean
   = remHighS != null && liveDeltaS >= remHighS
```
(`liveDeltaS = max(0, (nowMs - updatedAtMs)/1000)`. Overrun = the elapsed since poll has consumed the
entire p90 remaining.) Pure → unit-testable without React.

### 4.3 `useAllPrs` exposes `dataUpdatedAt`
`web/src/api/hooks.ts`: `useAllPrs` already returns the react-query result; `PipelineView` reads
`const { data: prs = [], dataUpdatedAt } = useAllPrs()` and threads `updatedAt={dataUpdatedAt}` to
`PipelineBoard` → both `StageTrack` and `CheckRowList`.

### 4.4 `CheckRowList` — per-row tick
- New prop `updatedAt: number`. Call `useNow()` once.
- For a **running** row (`!done && duration_seconds != null`):
  `displayElapsed = liveElapsedS(duration_seconds, updatedAt, now)`; pass `displayElapsed` as
  `bandSegments`' `elapsedS`; show `formatEta(displayElapsed)` in the right label (the counter ticks).
  `overrun = isOverrun(liveDeltaS, rem_high_seconds)` → `liveState = overrun ? 'overdue' : visualState(c)`;
  the bar/label use `liveState`.
- **done/failed/non-running** rows are unchanged (no tick; final duration).

### 4.5 `StageTrack` — connector-band tick
- New props `updatedAt: number` (already has `etaSeconds/etaLow/etaHigh/etaPercent`). Call `useNow()`.
- When the band renders (CI node active + range present): advance the pseudo-elapsed —
  `tickedPseudoElapsed = pseudoElapsedFromPercent(etaSeconds ?? 0, etaPercent) + liveDeltaS`, fed to
  `bandSegments` as `elapsedS` (low/nominal/high from `etaLow/etaSeconds/etaHigh`). On overrun
  (`isOverrun(liveDeltaS, etaHighSeconds)`) the band's fill switches from `bg-accent` to the amber
  overdue color.
- No range / not active → today's flat connector, unchanged.

## 5. Data flow

```
/api/prs (snapshot duration_seconds, rem_*) + react-query dataUpdatedAt
  → PipelineView threads updatedAt
     ├─ CheckRowList: useNow → liveElapsedS(duration, updatedAt, now) → bandSegments + label + liveState
     └─ StageTrack:  useNow → pseudoElapsed + liveDeltaS → bandSegments (connector band)
  → next poll resets dataUpdatedAt → snap to truth → resume ticking
```

## 6. Error handling & edge cases

- **Clock skew:** `displayElapsed` adds *browser* wall-clock since the *browser's* receive time
  (`dataUpdatedAt`); the only error is network/processing latency (sub-second) — negligible for smooth
  motion, and corrected each poll. No server-clock dependency.
- **`nowMs < updatedAt`** (stale render before first tick) → `max(0, …)` floors the delta at 0.
- **Hidden tab:** interval cleared; on return, an immediate `setNow` jumps to current time so the bar
  catches up in one step (then the next poll corrects).
- **Done mid-tick:** the next poll flips the row to `done` (final duration) → `!done` gate stops ticking.
- **Missing range** (no `rem_*`): `bandSegments` returns null → fall back to the existing plain bar
  (which, for a running no-range row, is the fixed 40% — unchanged; ticking needs the range to advance).
- **`bandSegments` clamps** solid/band/tick to [0,1], so an overrun elapsed can't draw past the track.

## 7. Testing (TDD)

**Frontend (vitest)**
- `liveElapsedS`/`isOverrun`: non-running passthrough (delta 0 when now==updatedAt); advancing delta;
  `nowMs<updatedAt` floors to 0; overrun threshold at `liveDelta >= remHigh`.
- `useNow`: advances with fake timers; clears interval on `document.hidden` and resumes on visible
  (mock `document.hidden` + dispatch `visibilitychange`); cleanup removes interval + listener.
- `CheckRowList`: a running row with `updatedAt` in the past + a fake `now` renders a **larger** solid
  width than the snapshot-only render (it advanced); past upper → `data-state="overdue"`; a done row is
  unaffected by `now`.
- `StageTrack`: CI-active + range + advanced `now` → connector band `solid` width grows vs snapshot;
  no-range → flat connector unchanged.
- Full `vitest run` + `tsc -b --force` + `eslint --max-warnings 0` (watch the react-hooks/purity rule —
  no `Date.now()` in render).

## 8. Files touched

**New**
- `web/src/hooks/useNow.ts` (+ test).

**Modified**
- `web/src/lib/etaBand.ts` — add `liveElapsedS` + `isOverrun` (+ tests).
- `web/src/api/hooks.ts` — `useAllPrs` consumers read `dataUpdatedAt` (no signature change; it's already
  on the returned query object).
- `web/src/components/PipelineView.tsx` — thread `updatedAt` to board → StageTrack + CheckRowList.
- `web/src/components/CheckRowList.tsx` — per-row live tick.
- `web/src/components/StageTrack.tsx` — connector-band live tick.
- Tests for `CheckRowList`, `StageTrack`.

## 9. Non-goals / scope guard

- **No backend or wire change** — ticks off the existing snapshot + `dataUpdatedAt`.
- **Not the GitHub-App-credentials work** — that (higher rate limits via an App installation token) is a
  separate backend milestone; the hot poll path already uses GraphQL. Tracked separately.
- No change to poll cadences (the smooth motion comes from the client tick, not faster polling).

## 10. Risks

- **react-hooks/purity** (`Date.now()` during render) — mitigated by capturing time only in the effect/
  interval (the established `CondensingProgress` pattern); the eslint gate enforces it.
- **Re-render cost** — a 1s `useNow` re-renders the pipeline board's running rows; bounded (few open PRs,
  few running checks) and paused on hidden tabs. If it ever bites, scope the ticker to the expanded
  drill-down + the active CI node only.
- **Tick vs snapshot disagreement** at the poll boundary — accepted: the bar snaps to server truth each
  poll; the 1s drift between is sub-perceptible.

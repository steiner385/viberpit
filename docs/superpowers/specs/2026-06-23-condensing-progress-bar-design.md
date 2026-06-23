# In-Session Condensing Progress Bar — Design

**Date:** 2026-06-23
**Status:** Approved (brainstorming)
**Builds on:** the merged context-condensing indicator (PR #201,
`docs/superpowers/specs/2026-06-23-condensing-indicator-design.md`).

## Problem

The condensing indicator (PR #201) surfaces compaction in the session header (a small
"Condensing context…" StatusBar spinner + an escalating `ctx %`). But the **prominent**
in-conversation indicator below the last turn — `SessionBody.tsx:186`, rendered whenever
`statusTier === 'busy'` — was never given the condensing label, so during compaction it still shows
the whimsical "working" word (e.g. "Percolating…"). Users want a clearer in-session signal, ideally a
Claude-Code-style progress bar with a sense of "how long is left".

## The honest constraint (carried from PR #201's investigation)

Verified against `claude-agent-sdk` 0.2.105: compaction emits **no progress signal and no ETA** — it
is a single opaque blocking model call exposing only a start (`PreCompact` hook) and an end
(`compact_boundary`). A *true* progress bar is therefore impossible. What IS possible and honest: a
determinate bar **animated against an expected duration**, where the expected duration is **learned
from measured past compactions**. The bar can only reach 100% when the real `compact_boundary`
arrives; before that it is capped/asymptotic so it never claims false precision.

## Goal & non-goals

**Goal:** in the prominent in-session indicator, show "Condensing context…" with a determinate
progress bar + readout, animated against a **globally learned** expected compaction duration that
calibrates over time.

**Non-goals:** a bar driven by real-time compaction progress (no such signal); per-account or
per-context-size calibration (decided: a single global estimate); changing the StatusBar text or the
`ctx %` readout (both stay as shipped).

## Architecture

### Backend

**Measure duration (driver).** `server/sessions/driver.py`:
- On `_on_pre_compact` (start), stamp `self._condense_started_at = self._clock()`.
- On the real boundary (`_drain` sees `ev.Condensing(active=False)`), compute
  `duration_ms = (self._clock() - self._condense_started_at) * 1000` when a start stamp exists,
  clamp to `[3000, 60000]`, and fold it into the persisted estimate via the stats module. Clear the
  start stamp. Record **only** on a real boundary — the turn-end backstop path records nothing.

**Persist a global EWMA (reuse `AppSettings` singleton).** `server/db/models.py` — add two
**nullable** columns to `AppSettings` (nullable so `init_db` backfills them; no migration):
- `compaction_ewma_ms: Mapped[float | None]` (default `None`)
- `compaction_samples: Mapped[int | None]` (default `None`)

New module `server/sessions/compaction_stats.py` (pure-ish; DB I/O isolated):
- `EWMA_ALPHA = 0.3`, `DEFAULT_ESTIMATE_MS = 12000`, `MIN_MS = 3000`, `MAX_MS = 60000`.
- `fold(prev_ewma: float | None, sample_ms: float) -> float` — pure: clamp `sample_ms` to
  `[MIN_MS, MAX_MS]`; if `prev_ewma is None` return the clamped sample (seed); else
  `0.3*sample + 0.7*prev_ewma`.
- `async record_duration(session_maker, sample_ms: float) -> None` — load the singleton
  `AppSettings`, `compaction_ewma_ms = fold(...)`, `compaction_samples = (samples or 0) + 1`, commit.
- `async current_estimate(session_maker) -> float` — return `compaction_ewma_ms` or
  `DEFAULT_ESTIMATE_MS` when `None`.

The driver reaches persistence the same way it already reaches the DB for status/session-id
callbacks (injected callable), to keep `driver.py` testable without a live DB. Concretely: the
orchestrator injects two async callbacks at driver construction — `on_record_condense_ms(ms)` and
`get_condense_estimate() -> float` — wired to `compaction_stats`. (Mirror the existing
`on_status` / `on_session_id` injection pattern.) Tests pass fakes.

**Deliver the estimate (wire event).** `server/sessions/events.py` — give the existing internal
`Condensing` dataclass a wire form and an `expected_ms` field:
```python
@dataclass
class Condensing:
    active: bool
    trigger: str | None = None
    pre_tokens: int | None = None
    expected_ms: float | None = None
    def to_wire(self):
        return {"type": "condensing", "active": self.active, "expectedMs": self.expected_ms}
```
Driver flow:
- On start (`_on_pre_compact`): `est = await get_condense_estimate()`; set `_condensing=True`; emit
  status (unchanged) AND push `ev.Condensing(active=True, expected_ms=est)` onto `_out` (forward to
  UI — no longer `continue`-dropped on the start path).
- On end (`_drain` sees the translator's `ev.Condensing(active=False)`): set `_condensing=False`,
  emit status, record the duration, and forward `ev.Condensing(active=False)` to `_out` so the bar
  can clear. (The translator-produced end event has `expected_ms=None`, which is fine — the client
  only reads `expectedMs` on the active=true event.)

### Frontend

**Reducer + types.** `web/src/session/events.ts` + `reducer.ts`: add the `condensing` event
(`{type:'condensing'; active:boolean; expectedMs?:number|null}`) to the union/known-types, and a
reducer field `condense: { active: boolean; expectedMs: number } | null` (default `null`). On
`active:true` store `{active:true, expectedMs: expectedMs ?? 12000}`; on `active:false` set `null`.

**`CondensingProgress` component.** New `web/src/components/session/CondensingProgress.tsx`:
- Props: `expectedMs: number`.
- Captures mount time in an effect (impure `Date.now()` out of render, mirroring `WorkingIndicator`),
  ticks ~10/s. `frac = elapsed / expectedMs`; while `frac < 1`, displayed `pct = min(95, frac*95)`
  (linear, reaching 95% exactly at the expected finish); once `frac >= 1`, asymptote:
  `pct = 95 + 4*(1 - 1/(1 + overrunSeconds))` (approaches but never reaches 99). Never reaches 100 —
  the component unmounts when condensing ends.
- Renders: "Condensing context…", a thin determinate bar (`role="progressbar"` with
  `aria-valuemin/max/now`, `aria-valuetext="Condensing context…"`), and a small elapsed readout
  reusing `formatElapsed`. `prefers-reduced-motion`: no eased animation — show a static/stepwise bar
  and the elapsed text only (reuse the existing `prefersReducedMotion` helper).

**SessionBody swap.** `web/src/components/session/SessionBody.tsx` (~line 185): when
`rt.state.status.state === 'condensing'`, render `<CondensingProgress expectedMs={rt.state.condense?.expectedMs ?? 12000} />`
instead of `<WorkingIndicator …>`; otherwise the existing busy-tier `WorkingIndicator` as today. The
StatusBar (`StatusBar.tsx`) is unchanged — it keeps its compact "Condensing context…" text.

## Data flow

```
PreCompact hook ─▶ driver: started_at=now; est=get_condense_estimate()
                         └▶ status=condensing  +  Condensing{active:true, expectedMs:est} ─▶ WS ─▶ reducer.condense ─▶ CondensingProgress bar
compact_boundary ─▶ translator Condensing{active:false} ─▶ driver: duration=now-started_at;
                         record_duration(duration)  +  status leaves condensing  +  Condensing{active:false} ─▶ WS ─▶ reducer.condense=null ─▶ bar unmounts
next compaction ─▶ get_condense_estimate() now reflects the updated EWMA
```

## Error handling / edge cases

- **Cold DB / no samples:** `current_estimate` returns `DEFAULT_ESTIMATE_MS` (12s); reducer also
  defaults `expectedMs` to 12000 if the event omits it.
- **Outliers:** every sample is clamped to `[3s, 60s]` before folding, so a stalled compaction can't
  blow up the average.
- **Missing boundary** (crash / turn-end backstop clears `_condensing` without a real boundary): no
  duration recorded (no sample), and the status leaving `condensing` clears `condense` → bar
  unmounts. The estimate is never poisoned by a non-completion.
- **Overrun:** the bar asymptotes below 100% and only the real end clears it — no false "done".
- **Persistence failure:** `record_duration` is best-effort (wrapped like other driver callbacks); a
  failure must never break the turn loop. `get_condense_estimate` failure falls back to the default.
- **No stuck state:** `condense` is derived purely from the wire events; on reconnect/replay with no
  live compaction there is no `active:true` event, so `condense` stays `null`. (Consistent with the
  transient-status persistence rule from PR #201.)

## Testing

**Backend (pytest):**
- `compaction_stats.fold`: seed (None→clamped sample), EWMA math, clamp at both bounds, default.
- `record_duration` / `current_estimate`: round-trip against a real `AppSettings` row; cold returns
  default; samples increment.
- driver: `_on_pre_compact` stamps start and emits `Condensing(active=True, expected_ms=<from
  injected getter>)`; a `Condensing(active=False)` records the elapsed (using injected `_clock`) via
  the injected recorder and forwards the event; a clear via the turn-end backstop (no boundary)
  records nothing.
- events: `Condensing.to_wire()` shape (`type:'condensing'`, `active`, `expectedMs`).

**Frontend (vitest):**
- reducer: `condensing` active stores `expectedMs` (and the 12000 default when omitted); `active:false`
  clears to `null`.
- `CondensingProgress`: under fake timers the bar advances and stays `< 100%` before the end;
  asymptotes on overrun; exposes `role="progressbar"`; reduced-motion path renders static bar +
  elapsed, no eased frames.
- `SessionBody`: status `condensing` renders `CondensingProgress` (not the whimsical
  `WorkingIndicator`); a normal busy status still renders `WorkingIndicator`.

## Out of scope (possible follow-ups)

- Per-account or context-size-bucketed calibration (decided global for v1).
- Surfacing the learned estimate anywhere else (e.g., a tooltip "avg ~Ns").
- A mini bar in the StatusBar (kept text-only).

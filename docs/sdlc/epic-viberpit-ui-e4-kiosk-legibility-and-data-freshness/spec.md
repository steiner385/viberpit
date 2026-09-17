# Spec — ViberPit UI E4: Kiosk legibility and data freshness

| | |
|---|---|
| **Initiative** | `epic-viberpit-ui-e4-kiosk-legibility-and-data-freshness` |
| **Stage** | Design (propose-only; this artifact merges at a human gate) |
| **Epic** | [steiner385/viberpit#1397](https://github.com/steiner385/viberpit/issues/1397) — part of review initiative [#1393](https://github.com/steiner385/viberpit/issues/1393) |
| **Children** | G09 [#1407](https://github.com/steiner385/viberpit/issues/1407) · G19 [#1417](https://github.com/steiner385/viberpit/issues/1417) |
| **Sibling epics (same initiative)** | E2 accessibility [#1395](https://github.com/steiner385/viberpit/issues/1395), spec merged (#1579) · E3 kiosk privilege [#1396](https://github.com/steiner385/viberpit/issues/1396), spec merged (#1580) |
| **Specced against** | `3ba8665c` (master, 2026-09-17) |
| **Audit reviewed** | `07c02a99` (2026-09-07) |

---

## 0. Provenance note (read this before the acceptance criteria)

This initiative's `origin` is `epic-adoption`, not the SDLC Intake stage, so **no
`docs/sdlc/<slug>/intent.md` exists**, and this Design session did not author one — requirement 5
(upstream artifacts are read-only) forbids fabricating an Intake artifact to fill the gap. The
Design dispatch for this session inlined a placeholder pointing at that nonexistent file. This is
the same process gap E2's spec recorded as PC-5 and E3's recorded independently; it is flagged again
below as PC-1 for the harness owner, not re-litigated here.

The authoritative upstream for this spec is epic #1397 and its two children, read from GitHub.

**The audit's scope references are stale and were re-verified against current master
(`3ba8665c`), not trusted at face value.** Both children pin their scope to commit `07c02a99`
(2026-09-07); master has moved through the page-migration work since (the pipeline UI split into a
thin route shell plus a `features/pipeline` implementation). Of the five file paths the two children
cite:

| Cited path | Status on master @ `3ba8665c` |
| --- | --- |
| `web/src/pipeline/PipelineTab.tsx` (G09, G19) | **gone** — split into `web/src/features/pipeline/PipelinePage.tsx` (page shell, health band, stale banner) and `web/src/pipeline/PipelineView.tsx` (board) |
| `web/src/pipeline/plotroom.css` (G09) | **moved** to `web/src/features/pipeline/charts/plotroom.css` |
| `web/src/shell/AppShell.tsx` (G09) | present — now carries a documented `KIOSK_SCALE = '1.25'` mechanism (see § 1) that did not exist in this form at audit time |
| `web/src/pipeline/useDashboard.ts` (G19) | present, current implementation verified against directly (see § 1) |

Consequently this spec inherits the audit's **criteria** (legibility at distance, honest staleness
signaling) but not its **counts or code excerpts**. The "9–14px" figure and the "starts only after
an SSE frame" description are treated as historical observations that this session independently
re-confirmed against the live tree (evidence in § 1), not as citations trusted unread.

---

## 1. Problem

Both children concern the one kiosk-safe route, `/pipeline` (`web/src/features/pipeline/PipelinePage.tsx`,
gated `showInKiosk` in `web/src/shell/navDefs.ts` — the only section kiosk mode may render, per
`KioskPageGuard` in `AppShell.tsx`). They are independent defects, not two views of one bug:

### G09 — chart text does not participate in the kiosk type scale

`AppShell.tsx` already implements a real, working kiosk magnification mechanism: mounting in kiosk
mode sets `--scale: 1.25` on `<html>` (`KIOSK_SCALE`), and `web/src/index.css` defines every
`--fs-*` step (and the `text-*` Tailwind utilities that resolve to them) as `calc(<rem> * var(--scale))`.
Ordinary app chrome — headers, lists, badges — enlarges coherently under this mechanism, by design
(the code comment calls this "the whole type scale," and warns explicitly against a second,
parallel kiosk-size rule).

**Verified against `3ba8665c`:** `web/src/features/pipeline/charts/plotroom.css` — the stylesheet
for the four pipeline chart components (`MetroTrack`, `QueueTrain`, `Waterfall`, `CheckGantt`) — sets
`font-size` in literal pixels throughout: `9px` (node labels, ETA text), `10px` (section dividers,
gantt inline legend), `11px` (node status circles, gantt rows, queue cards), `12px` (checks list).
None of these read `var(--fs-*)` or `var(--scale)`. `--scale: 1.25` therefore enlarges the health
band, tabs, and legend around the chart, but the chart's own labels — which is most of the
information density on the page — stay pinned at their unscaled size. A 4K kiosk viewport does not
make this content wall-readable, exactly as G09 states; this session's own reading of the CSS is the
evidence, not a repeated claim from the audit.

There is a second, independent gap: `web/src/shell/useViewport.tsx` already exposes a `portrait`
boolean (`useMediaQuery('(orientation: portrait)')`), consumed elsewhere in the app (e.g. Settings'
tablet-portrait drilldown). Nothing in `web/src/features/pipeline/` or `web/src/pipeline/` reads it
— `git grep -n "portrait" -- web/src/features/pipeline web/src/pipeline` returns no matches. The
board's current layout is an unconditional horizontal arrangement (metro track, horizontally
scrolling queue cards). G09 asks for composition in both orientations; today there is none.

### G19 — the staleness signal can never arm if no SSE message ever arrives

`web/src/pipeline/useDashboard.ts` (unchanged path, still present) implements one 90-second watchdog
(`STALE_AFTER_MS`) that flips `stale` to `true` if no fresh frame lands in time. **Verified against
`3ba8665c`:** `armStale()` — the only place that starts or resets that timer — is called from exactly
one place, `onFrame`, which is wired only to the `EventSource`'s `onmessage` handler. The initial
`/snapshot` fetch (the "fast first paint" path) sets `state` directly and never calls `armStale()`.
So: warm snapshot succeeds, and the SSE stream is refused, stays silently open with no messages, or
is malformed → `stale` stays at its initial `false` **forever**, because the timer that would flip it
was never started. This is a stronger and more precisely-located claim than the audit's "starts only
after an SSE frame" — it is not merely delayed, it is un-armed.

A second signal exists but is disconnected from the UI: `es.onerror` sets `connected = false`, and
the existing test suite (`web/src/pipeline/__tests__/useDashboard.test.tsx`) asserts this. But
`connected` is not consumed anywhere — `web/src/features/pipeline/PipelinePage.tsx` destructures
`{ state, stale, notifySupported, notifyEnabled, toggleNotify }` and never reads `connected`. A
dropped or refused connection produces **no visible change** distinct from a healthy one, as long as
`stale` also never fires (which, per above, it may not).

A dedicated indicator already exists and is the right building block: `PipelinePage.tsx` renders a
`<Banner tone="warning" role="status" title="Pipeline feed stalled — showing the last frame.">` when
`stale` is true, and keeps `PipelineView` rendered underneath it (last-good content is retained, as
G19 requires). The defect is that this banner's trigger can be permanently un-armed, and that there
is no equivalent for "not merely stale, actually disconnected."

**A third, related finding this session surfaced independently (not in either issue's text):** the
wire contract already anticipates server-vouched freshness. `DashboardState` (`web/src/pipeline/types.ts`)
carries `generatedAt: string; staleSince: string | null` at the top level, populated on *every*
emission (snapshot and each SSE frame) by `server/pipeline/dashboard_state.py`. `generatedAt` is a
real, freshly-computed timestamp each call. `staleSince`, however, is a dead field: the function's
only `return` statement hardcodes `"staleSince": None` (`dashboard_state.py:350`), and nothing in
`server/pipeline/` ever sets it otherwise. The client-side watchdog is therefore reinventing
staleness detection from scratch, timer-based and message-triggered, alongside a same-purpose field
in the payload that the server never populates and the client never reads. This is flagged as OQ-1 —
it changes the shape of the correct fix, not just its bug count.

---

## 2. Functional behavior

### FB-1 — Chart type scale participates in `--scale` (G09)

`plotroom.css`'s font-size (and any other viewing-distance-relevant dimension: node/circle size,
bar height, spacing) rules are re-expressed through the existing `--fs-*` custom properties (or
`text-*` Tailwind utilities that resolve to them) instead of literal pixels, so `MetroTrack`,
`QueueTrain`, `Waterfall`, and `CheckGantt` enlarge under kiosk's `--scale: 1.25` the same way the
rest of the app already does. This is a **routing** fix, not a new mechanism — no second
scale/multiplier is introduced (constitution VI; the code comment in `AppShell.tsx` already warns
against exactly this).

Where an existing `--fs-*` step does not land at a size that reads at kiosk viewing distance, a
distance-appropriate floor is established using the audit's own proposed starting point — 18px
secondary / 24px primary at a 1080p CSS viewport — **as a starting point to validate physically, not
a final target** (same caveat E2's spec applied to its own audit numbers). The exact floor is an
open question (OQ-2) for Plan/Build to close with physical validation at the four viewports in AC-1.

### FB-2 — Portrait and landscape composition (G09)

The Pipeline board composes usably in both kiosk orientations — 1920×1080 / 3840×2160 (landscape)
and 1080×1920 / 2160×3840 (portrait) — reusing the existing `useViewport().portrait` primitive
rather than introducing a second orientation signal. A kiosk has no pointer and no scrollbar
affordance a viewer can act on, so a portrait layout that degrades to "the same horizontal-scrolling
board, now narrower" does not satisfy this — some vertical composition (stacked lanes, reflowed
cards, or an equivalent) is required. The exact reflow strategy is a design decision left to Plan
(OQ-4); this spec fixes the *requirement* (both orientations legible, no pointer-dependent overflow
in kiosk), not the layout.

### FB-3 — Five-second glanceability reuses the existing rollup (G09)

`HealthHeader` already computes and renders the "all systems green" / "N lanes need attention"
rollup that both the Pipeline overview and the Delivery spine share (`buildLaneHealth`, `rollup` in
`web/src/pipeline/spine/laneStatus.ts`). G09's acceptance test ("identify system health within five
seconds") is validated against this existing surface at kiosk scale and distance — this spec does
not add a second glance indicator alongside it.

### FB-4 — The freshness watchdog arms from every frame path, not only SSE messages (G19)

The mechanism that decides `stale` observes **every** path that can deliver fresh data — the initial
`/snapshot` fetch and each SSE `message` — not just the SSE path alone. Concretely: whatever currently
calls `armStale()` only from `onFrame` is extended (or replaced) so that a successful snapshot paint
also starts the freshness clock, closing the exact gap in § 1 (snapshot succeeds, SSE never delivers
a message, `stale` never fires). `STALE_AFTER_MS` (already `90_000`) remains the one threshold
constant — this must not gain a second, snapshot-specific timeout.

This requirement is intentionally implementation-neutral about *how* freshness is tracked (client
timer restarted on every frame vs. deriving `stale` declaratively from `generatedAt` vs. `now()`,
optionally backed by a server-populated `staleSince`) — see OQ-1. Whichever approach Plan selects, it
must satisfy: a frame arriving from either path resets the "last known fresh" point, and staleness is
correctly detected when *neither* path has delivered a fresh frame within the window.

### FB-5 — Connection state is surfaced (G19)

`connected` (already computed by `useDashboard`, already tested at the hook level, currently
unconsumed) is threaded into the Pipeline page so a refused or dropped `EventSource` is visibly
distinguishable from "connected but the feed has gone quiet." This reuses the existing `Banner`
pattern already used for `stale` — no new indicator component — but a disconnected state must not
be visually identical to a merely-stale one; a viewer walking up to a wall display needs to be able
to tell "the browser lost its connection" from "the server hasn't produced a new frame in a while."
The exact visual treatment (a second banner tone, combined copy, etc.) is left to Plan (OQ-3).

### FB-6 — Reconnect and resume correctly clear both signals (G19)

Native `EventSource` reconnection, and a backgrounded-then-resumed kiosk tab, both correctly clear
`stale` and the disconnected state on the next good frame. The existing test "staleness resets when a
new frame arrives" is the pattern to extend — it must be joined by equivalent coverage for the
snapshot-only-then-reconnect path (currently untested; see § 1) and for the connected-state
transition.

### FB-7 — Staleness is shown with an age, not just a boolean (G19)

While `stale` (or disconnected), the board continues to show the last-good content — already true,
`PipelineView` renders underneath the banner unconditionally — and the banner additionally surfaces
how old that content is, derived from the already-present `state.generatedAt`, so the signal reads as
"last updated N minutes ago" rather than only "something may be wrong." No new timestamp source is
introduced; `generatedAt` is populated on every emission today (§ 1).

---

## 3. Acceptance criteria

**AC-1 — Chart legibility (G09 / FB-1, FB-3)**
1. `grep` over `web/src/features/pipeline/charts/plotroom.css` finds no literal pixel `font-size` (or
   equivalent legibility-relevant literal dimension) that bypasses `--fs-*`/`--scale`; a regression
   test (rendering a chart component at `--scale: 1` and `--scale: 1.25` and asserting computed
   font sizes differ proportionally) exists and passes.
2. At each of 1920×1080, 1080×1920, 3840×2160, and 2160×3840 in kiosk mode (`?kiosk=1`), a recorded
   human verification confirms overall system health (the `HealthHeader` rollup) is identifiable
   within five seconds at the intended physical viewing distance for that resolution class.
3. The distance floor adopted (starting from the audit's 18px/24px-at-1080p proposal, OQ-2) is
   documented in the PR and validated against at least one of the four viewports physically, not
   only computed.

**AC-2 — Orientation (G09 / FB-2)**
4. An automated test renders `PipelinePage` (or the board) at a portrait kiosk viewport and asserts
   the layout does not depend on pointer-only horizontal scroll to reach board content that a
   landscape kiosk viewport shows without scrolling.
5. `web/src/features/pipeline/` and `web/src/pipeline/` import `useViewport().portrait` for this
   purpose; `grep` confirms no second orientation-detection mechanism (a raw `matchMedia` call, a
   new hook) was added alongside it.

**AC-3 — Freshness watchdog (G19 / FB-4, FB-6, FB-7)**
6. A new test: `/snapshot` resolves successfully, zero SSE `message` frames ever arrive (stream
   silent or refused post-open), `STALE_AFTER_MS` elapses → `stale` becomes `true`. This is the
   regression test proving the § 1 gap is closed (today this scenario leaves `stale` permanently
   `false` — verified in this session, not asserted from memory).
7. The existing "staleness resets when a new frame arrives" test continues to pass, extended to cover
   a frame arriving via `/snapshot`-then-reconnect, not only via a second SSE message.
8. `grep` confirms exactly one staleness-threshold constant (`STALE_AFTER_MS`) is defined and used;
   no second, snapshot-specific timeout was introduced to close (6).
9. The banner (or its replacement) shown while `stale` displays an age derived from
   `state.generatedAt`, asserted in a component test.

**AC-4 — Connection state (G19 / FB-5)**
10. A new test: the `EventSource` fires `onerror` before any message has ever arrived → the rendered
    Pipeline page shows a state that is distinguishable (by an accessible-name or `role="status"`
    text assertion, not only a CSS class) from the `stale`-but-connected state.
11. `connected` is consumed by at least one component under `web/src/features/pipeline/` or
    `web/src/pipeline/`; `grep` for `useDashboard()` call sites confirms `connected` is destructured
    where the hook is used for rendering (it is not, today — see § 1).
12. Reconnection (a fresh `onopen`/`onmessage` after a prior `onerror`) clears the disconnected
    indicator, asserted in a test.

**AC-5 — Repository gates**
13. `ruff check src/ server/`, `mypy server/ --ignore-missing-imports`, `pytest tests/`,
    `pytest server/tests/` (the four-step `lint-and-test` gate) all pass — required only if the
    chosen fix for OQ-1 touches `server/pipeline/dashboard_state.py`; otherwise this criterion is
    satisfied vacuously by no server change.
14. In `web/`: `npm run typecheck`, `npm run lint` (`--max-warnings 0`), the **full** `npm run test`
    (not a changed-files subset — `web/src/features/pipeline` and `web/src/pipeline` are consumed
    across mocked test files elsewhere in the suite), `npm run build` all pass.
15. No replaced path survives the change: if a CSS rule, helper, or constant is superseded, the old
    one is deleted in the same PR (constitution VI; no half-dead code).
16. Sibling occurrences of each fixed pattern are audited, not just the two cited files — e.g., any
    other kiosk-visible surface with literal-px chart-adjacent CSS, and any other consumer of
    `useDashboard`-shaped freshness state — and the audit sweep is recorded in the PR body.

---

## 4. Data and API surface

**No new HTTP endpoint, database model, column, migration, or session-engine contract is required
by the minimum fix.** The two existing endpoints behind `ApiBaseProvider base="/api/pipeline"` —
`GET /api/pipeline/snapshot` and the SSE stream `GET /api/pipeline/events` — are unchanged in shape
under the baseline (client-only) resolution of OQ-1.

| Surface | Kind | Nature of change |
| --- | --- | --- |
| `web/src/features/pipeline/charts/plotroom.css` | Stylesheet | Literal px `font-size`/dimension rules replaced with `--fs-*`/`--scale`-derived values. No new CSS custom property is added — this routes through the existing type scale. |
| `web/src/shell/useViewport.tsx` | Hook (existing) | No API change; `portrait` (already exported) gains a first consumer in Pipeline. |
| `web/src/pipeline/useDashboard.ts` | Hook | `DashboardHook`'s existing `stale`/`connected` fields change **when** they flip, not their type. Internal watchdog-arming logic changes per FB-4. |
| `web/src/features/pipeline/PipelinePage.tsx` | Component | Consumes `connected` (new); the `stale` banner gains an age string. No new prop surface on child components is anticipated. |
| `web/src/pipeline/types.ts` — `DashboardState.staleSince` | Wire field (existing, currently dead) | **Decision-dependent (OQ-1).** If Plan selects the server-vouched approach, `server/pipeline/dashboard_state.py` starts computing a real value instead of the hardcoded `None`, and the client trusts it instead of (or in addition to) its own timer. If Plan keeps the client-only fix, this field is untouched and its dead status is called out for a follow-up issue (constitution VI) rather than silently left as unexplained dead code. |
| New test fixtures/mocks | Test-only | The G19 regression tests (AC-3.6, AC-4.10) need a fake `EventSource`/fetch harness matching the existing pattern in `web/src/pipeline/__tests__/useDashboard.test.tsx` — reuse that fixture, do not add a second fake-SSE implementation. |

**No product-code change is anticipated in `src/ccswitch/`.** If the OQ-1 resolution requires a
`server/pipeline/dashboard_state.py` change, note the shared-venv rule: `server/*` resolves from the
checkout under test via pytest's rootdir (unaffected by the deploy-worktree `.pth` issue that only
affects `src/ccswitch/`), so no `PYTHONPATH` workaround is needed there.

---

## 5. Non-goals

- **Not E3's kiosk-privilege findings.** E3's spec (merged, #1580) already documents that the
  kiosk-safe Pipeline page renders a live mutating "Fix" button (`PrRow.tsx`) and opens the
  full-privilege session WebSocket even in kiosk mode (`AppShell.tsx` → `useAppBanners.tsx` →
  `useInputHold.ts` → `getSessionSocket()`). This epic does not touch authorization, privilege
  scoping, or which controls render in kiosk — only how legibly existing kiosk content renders and
  how honestly its freshness is signaled. A future PR implementing this spec must not casually widen
  or narrow what E3 has already scoped as a privilege boundary.
- **Not E2's contrast/touch-target accessibility criteria.** G09's "legible at viewing distance" is a
  size/scale axis, distinct from E2's WCAG 1.4.3 contrast and 2.5.8 touch-target work. A change here
  must not *regress* E2's contrast criteria (e.g., enlarging text must not change composited color),
  but does not need to independently re-satisfy E2's full conformance matrix.
- **Not a visual redesign** of the Pipeline board beyond what legibility and orientation composition
  require. Where a fix has layout consequences, the minimal one wins.
- **Not committing to the server-vouched `staleSince` architecture.** § 1's third finding is real, but
  whether to close it now (bigger, cleaner) or defer it (smaller, client-only) is OQ-1, left to Plan.
- **Not the other UI-audit findings.** Only G09 and G19 (this epic's children) are in scope; other
  children of #1393 (including E2's and E3's) are out of scope here.
- **Not backfilling the 2026-09-07 audit's specific numbers as targets** — per § 0, they are
  historical observations this session independently re-verified, not acceptance thresholds.
- **Not a constitution amendment.** No governance change is requested by this spec.

---

## 6. Policy concerns — flagged for resolution at the human gate

> Per the Design-stage requirement, called out for a human decision before Build; none should be
> resolved by an implementing agent.

**PC-1 — Process: this initiative has no `intent.md`, and the Design prompt assumed one.**
Same gap E2 (PC-5) and E3 recorded independently for their own epic-adopted initiatives: the
synthesizer's Design prompt inlines `docs/sdlc/<slug>/intent.md` from `origin/master`
unconditionally; for `origin: epic-adoption` that file never exists. This session recovered by
reading epic #1397 and its two children directly. *No decision needed for this epic specifically* —
recorded a third time so the harness-level fix (skip the inline, or substitute the epic body, when
`origin == "epic-adoption"`) gets weight proportional to how often it recurs. Filed as ViberPit-
harness feedback, not repository work.

**Security:** no new authentication, authorization, network, credential, or data-egress surface is
introduced. The freshness-age string (FB-7) and the connection-state indicator (FB-5) surface only
information already computed and already reachable by anyone viewing the kiosk screen (which is
already unauthenticated-by-physical-presence, per E3's existing framing of the kiosk boundary) — no
*new* data reaches the kiosk display, only a clearer signal about the freshness of data it already
shows. No security concern identified beyond what E3 already owns.

**ToS:** no third-party service, model provider, or vendor surface is involved. No ToS concern
identified.

**Data handling:** no PII or account/session-identifying content is introduced by either fix. If
Build produces before/after evidence (screenshots at the four kiosk viewports for AC-1.2), that
evidence must show only synthetic/fixture pipeline data, matching E2's PC-2 sanitization constraint
— this spec adopts that same constraint by reference rather than restating it.

---

## 7. Open questions for the gate

- **OQ-1 — Does the G19 fix populate the server's dead `staleSince` field, or stay client-only?**
  § 1 documents a real architectural finding: the wire contract already has a slot for
  server-vouched freshness (`DashboardState.staleSince`), populated nowhere. The *minimum* fix for
  FB-4 is purely client-side (arm the existing watchdog from the snapshot path too). The *more
  correct* fix, more aligned with constitution VI's single-source-of-truth principle, has the server
  compute real staleness once and the client trust it, retiring the client-side timer-guessing
  entirely. The latter is a larger surface (`server/pipeline/dashboard_state.py`, its tests, and the
  wire contract's actual semantics) for a freshness signal that, per FB-4's phrasing, can also be
  satisfied without it. Recommendation: default to the client-only fix for this epic (smaller, ships
  faster, directly closes G19's stated defect) and file the server-side `staleSince` cleanup as a
  separate follow-up issue rather than silently leaving a newly-noticed dead field unexplained — but
  Plan should confirm this trade-off explicitly rather than inherit it by default.
- **OQ-2 — Exact legibility floor for G09.** The audit's 18px secondary / 24px primary (at a 1080p
  CSS viewport) is a *proposed* starting point, not verified physically by anyone in this session's
  evidence chain. Plan/Build should confirm (or revise) it against the four viewports in AC-1.2
  before treating it as final.
- **OQ-3 — Visual treatment for "disconnected" vs. "stale."** FB-5 requires the two be
  distinguishable; it does not specify how (a second `Banner` tone, combined copy with different
  icons, etc.). A design decision, not an engineering one — recommend resolving alongside OQ-4 so
  both land as one small design pass rather than two.
- **OQ-4 — Portrait reflow strategy for the Pipeline board.** FB-2 requires the board not depend on
  pointer-only horizontal scroll in portrait kiosk orientations but does not prescribe the resulting
  layout (stacked lanes, paginated cards, a condensed view). Needs a design decision before Build.
- **OQ-5 — Sequencing against E3.** E3's spec is merged but its own Build work (if any is in flight)
  touches `AppShell.tsx` and the Pipeline page's kiosk gating. Before Build starts here, check for
  open PRs touching `web/src/features/pipeline/`, `web/src/pipeline/`, `web/src/shell/AppShell.tsx`,
  and `web/src/shell/kiosk.ts` (per the repository's "before implementing, check whether the same
  work is already in flight" norm) — none were open at spec time (`3ba8665c`), but epics in the same
  initiative land close together in practice (E2 and E3's specs merged one day apart).

---

## 8. Constitution check

| Principle | How this spec complies |
| --- | --- |
| I — no forked binaries, gate experimental surfaces | No agent binary touched. No experimental vendor surface. |
| II — account isolation, one credential authority | No account, credential, or session-isolation surface touched. |
| III — verify against the real binary/API | Central to this spec: § 0 and § 1 re-derive every claim from the live tree at `3ba8665c` (file existence, actual CSS values, actual watchdog wiring, actual dead-field status) rather than trusting the audit's 2026-09-07 excerpts. |
| IV — security-first, exposure-conditional auth | No new exposure; PC-Security confirms no data newly reaches the kiosk surface. |
| V — TDD + quality gates | AC-1.1, AC-3.6–9, and AC-4.10–12 are each a named regression test closing a specifically-identified gap; AC-5 requires the full gate. |
| VI — single source of truth / no dead code | FB-1/AC-1.1 and FB-2/AC-2.5 forbid a second scale mechanism or orientation detector; FB-4/AC-3.8 forbids a second staleness threshold; OQ-1 explicitly surfaces (rather than silently ignores) the dead `staleSince` field; AC-5.15–16 require deletion of replaced paths and a sibling-occurrence audit. |
| VII — modern vetted deps | No new dependency is anticipated by either fix. |

---

*This is a propose-only Design artifact. It specifies; it does not implement, amend governance, or
authorize execution.*

---
initiative: epic-viberpit-ui-e4-kiosk-legibility-and-data-freshness
kind: plan
epic: steiner385/viberpit#1397
upstream: [docs/sdlc/epic-viberpit-ui-e4-kiosk-legibility-and-data-freshness/spec.md]
verified_against: 05a5fd9e
---

# Plan — ViberPit UI E4: Kiosk legibility and data freshness

## 0. Headline: most of the spec's defects are already fixed on master

The spec was written against `3ba8665c`. This plan re-verified every claim against current
`origin/master` (`05a5fd9e`) and found that two landed fixes have since closed the spec's two
central defects. **Build must not re-implement them.**

| Spec claim | State on `05a5fd9e` | Evidence |
| --- | --- | --- |
| G09 / FB-1: `plotroom.css` uses literal px `font-size` | **Fixed** by #2420 (`fix(web): route pipeline chart text through kiosk type scale…`, #1407) | Every `font-size` in `charts/plotroom.css` is `var(--fs-base)` or `var(--fs-xl)`; `plotroom.test.ts` §"#1407" already guards it (literal ban, `--fs-base` floor, `--fs-xl` reserved for two glyphs, `index.css` defines each step as `calc(<rem> * var(--scale))`). |
| G09 / FB-2: nothing reads `portrait` | **Fixed** by #2420 | `PipelinePage.tsx` passes `useViewport().portrait` to `PipelineView`, which adds `.pipeline-portrait`; CSS wraps `.queue-train`; `PipelinePage.test.tsx` has portrait/landscape cases; `plotroom.test.ts` asserts no second orientation media query. |
| G19 / FB-4: `armStale()` only runs from `onFrame`, so `stale` can never arm | **Fixed** by #2021 | `useDashboard.ts` calls `armStale()` at effect mount, before the snapshot fetch. A snapshot-succeeds / SSE-silent run trips `stale` at `STALE_AFTER_MS`. Test `staleness: a feed that never delivers a frame still trips stale from mount` covers a silent feed. |
| FB-5: `connected` is not consumed | **Partly fixed** | `PipelinePage.tsx` reads `connected`, but only to choose the banner when there is no `state` yet (`stale && !state && !connected` → danger). Once a frame has been painted, a dropped `EventSource` (`connected=false`) shows nothing until 90 s elapse, then the *same* "stalled" warning as a connected-but-quiet feed. **This is the real remaining gap.** |
| FB-7: banner shows age | **Not done** | Banner copy is static. `state.generatedAt` is already shown in the page header (`PipelinePage.test.tsx` "the header says when the frame was computed"), but not in the banner. |
| FB-6: reconnect clears both signals | **Untested** for the disconnected→reconnected transition with a prior frame. |

Therefore the remaining work is small and the plan is deliberately scoped to it. The residual
items are the **freshness/connection banner (FB-5, FB-7, FB-6)**, **physical legibility
validation (AC-1.2/1.3, OQ-2)**, **AC mapping gaps in tests**, and the **OQ-1 follow-up issue**.

## 1. Decisions on the spec's open questions

- **OQ-1 — client-only.** Confirmed: keep the fix client-side. `DashboardState.staleSince` stays
  untouched (server hardcodes `None` at `server/pipeline/dashboard_state.py`; nothing reads it).
  File **one follow-up issue** ("retire or populate the dead `DashboardState.staleSince` wire
  field"; Principle VI) and link it in the PR body. No server change, so AC-5.13 is vacuous.
- **OQ-2 — floor.** #2420 chose `--fs-base` (20px at `--scale: 1.25`) and `--fs-xl` (25px),
  above the audit's 18/24 starting point. Keep. Physical confirmation is the open item (Step 4).
- **OQ-3 — disconnected vs stale.** Two banners, distinct tone **and** distinct accessible text,
  both reusing `Banner` (no new component):
  - connected, no frame in window → existing `warning` "Pipeline feed stalled — …"
  - not connected → `danger` "Pipeline feed disconnected — reconnecting…"
  Both append age (below). Disconnected is shown **immediately on `onerror`** once a frame has
  been seen (no 90 s wait); before the first frame we keep the current `stale` gating so a
  cold-start blip does not flash a danger banner over the loading skeleton.
- **OQ-4 — portrait reflow.** Already decided and shipped (#2420: queue train wraps under
  `.pipeline-portrait`). Remaining question is whether the *metro track* and *Gantt* also need
  portrait treatment; Step 4's physical/screenshot pass decides, and any CSS change lands in that
  step guarded by a `plotroom.test.ts` assertion.
- **OQ-5 — sequencing.** At plan time (`05a5fd9e`) no open PR touched `features/pipeline`,
  `web/src/pipeline`, `AppShell.tsx` or `shell/kiosk.ts` (checked via `gh pr list`). Build re-runs
  the in-flight GraphQL check before coding; E3 Build work may land close by.

## 2. Files

| File | Change |
| --- | --- |
| `web/src/features/pipeline/PipelinePage.tsx` | Replace the two banner conditions with one derived `feedBanner` (disconnected / stalled / none) built from `connected`, `stale`, `state`; append an age string from `state.generatedAt`. Reuse `Banner`. |
| `web/src/pipeline/format.ts` | Only if `formatSince(iso, now)` cannot already render "N minutes ago" for the banner; otherwise **reuse it unchanged** (no second age formatter — Principle VI). |
| `web/src/pipeline/useDashboard.ts` | Expected: **no change**. Only touched if Step 2 shows `connected` must be reset/latched differently (e.g. a `hasConnectedOnce` signal); if so keep `STALE_AFTER_MS` as the single threshold constant. |
| `web/src/features/pipeline/PipelinePage.test.tsx` | New banner/age/transition tests (§4). Reuse its existing `FakeEventSource` harness; add no second fake SSE. |
| `web/src/pipeline/__tests__/useDashboard.test.tsx` | Add snapshot-then-silent-stream and error→reconnect-with-prior-frame cases, reusing its fixture. |
| `web/src/features/pipeline/charts/plotroom.css` + `plotroom.test.ts` | Only if Step 4's viewport pass finds clipped or overflowing labels at `--scale: 1.25` (e.g. `.node` fixed `width: 84px`, `.node .c` 32px circle holding a `--fs-xl` glyph). Fix by scaling the dimension through the existing `--scale`, never a second multiplier. |
| `docs/sdlc/.../verification.md` (Build-stage artifact, not this PR) | Record of AC-1.2/1.3 human verification with fixture-only screenshots. |
| GitHub issue (not a file) | Follow-up for dead `staleSince` (OQ-1). |

No server, DB, migration, `src/viberpit/`, or dependency change.

## 3. Order

1. **In-flight check + baseline** — GraphQL open-PR scan for the touched paths; run the existing
   `useDashboard`, `PipelinePage`, `plotroom` suites to confirm green on master.
2. **Red tests first** (§4 tests T1–T6). Confirm each fails for the intended reason against
   unmodified code (T1–T3 and T5 should fail; T4 and T6 characterize existing behaviour and pass —
   keep them as regression pins).
3. **Banner implementation** in `PipelinePage.tsx` until T1–T5 are green. Hook change only if
   forced (see Files).
4. **Physical/viewport validation (AC-1.2, AC-1.3, OQ-2, OQ-4)** — render `/pipeline?kiosk=1`
   against synthetic fixture data at 1920×1080, 1080×1920, 3840×2160, 2160×3840 using the
   headless-Playwright harness (probe `scrollWidth`/bounding rects, not eyeballing). Record the
   five-second HealthHeader-rollup check at one viewport on a real display. Any CSS fix goes in
   with a `plotroom.test.ts` assertion (red first).
5. **Sibling audit (AC-5.16)** — grep for (a) literal-px `font-size` in other kiosk-visible
   surfaces (`HealthHeader`, `StatusStrip`, `LegendPanel`, `PrRow`, `spine/`, `index.css` kiosk
   rules); (b) other `EventSource`/watchdog consumers with a boolean-only staleness signal
   (`useDashboard` is currently used by `PipelinePage`, `StatusStrip`'s host chain; check
   `useRepoCycle`, `useFocusedPipeline`). Record the sweep in the PR body. Fix only what is
   kiosk-visible and in-scope; file issues for the rest.
6. **Follow-up issue** for `staleSince`; link from the PR.
7. **Gates** — in `web/`: `npm run typecheck`, `npm run lint`, **full** `npm run test`, `npm run
   build`; plus `e2e` grep (`web/e2e`) for the changed banner strings and run the Pipeline spec
   (e2e is not a required check, so a red spec would merge silently). Server gates are vacuous.
8. PR via the completion workflow (branch off `origin/master`, non-draft, auto-merge for the
   Build PR).

## 4. Tests (mapped to spec ACs)

| ID | Test | Proves |
| --- | --- | --- |
| T1 | `PipelinePage`: frame delivered, then `onerror` → a `role="status"` banner with accessible text matching /disconnected/ appears **without advancing 90 s**. | AC-4.10, AC-4.11, FB-5 |
| T2 | `PipelinePage`: frame delivered, no error, 90 s elapse → the *stalled* banner shows and its text does **not** match /disconnected/ (the two states are distinguishable by text, not CSS class). | AC-4.10, FB-5 |
| T3 | `PipelinePage`: after T1/T2, a fresh `onopen`+frame removes the banner. | AC-4.12, FB-6 |
| T4 | `PipelinePage`: error before any frame keeps today's behaviour (skeleton, danger banner only after the stale window). Pins the existing test at `PipelinePage.test.tsx` "a feed that only errors…". | AC-4.10 (cold-start path) |
| T5 | `PipelinePage`: with fake timers and a `generatedAt` N minutes old, the banner text contains the age ("… 5 minutes ago"), via the existing `formatSince`. | AC-3.9, FB-7 |
| T6 | `useDashboard`: `/snapshot` resolves, zero SSE messages, advance `STALE_AFTER_MS` → `stale === true` (regression pin for the fixed gap; the existing mount-arming test covers the no-snapshot case only). | AC-3.6 |
| T7 | `useDashboard`: frame → `onerror` → `onmessage` clears `connected=false` and `stale`; extends the existing "staleness resets" test to the snapshot-then-reconnect path. | AC-3.7, AC-4.12, FB-6 |
| T8 | Existing `plotroom.test.ts` §"#1407" literal-px / `--fs-base` floor / `--scale` calc tests — **keep, run, do not duplicate**. Add one rendered-size test (chart at `--scale: 1` vs `1.25`, computed `font-size` ratio 1.25) only if jsdom can resolve the `calc`; otherwise the static CSS assertions stand and the ratio is checked in the Step 4 browser pass. | AC-1.1 |
| T9 | Existing `PipelinePage` portrait/landscape `.pipeline-portrait` tests + `plotroom.test.ts` no-second-media-query test — keep; add a portrait no-horizontal-overflow assertion from the Step 4 browser probe if reproducible in Playwright. | AC-2.4, AC-2.5 |
| T10 | Grep checks in the PR body: one `STALE_AFTER_MS`; one orientation signal; `connected` destructured in a rendering component. | AC-3.8, AC-2.5, AC-4.11 |
| Manual | Step 4 recorded verification at four viewports + one physical display; floor documented. | AC-1.2, AC-1.3 |
| Gates | Step 7. | AC-5.13 (vacuous), AC-5.14, AC-5.15, AC-5.16 |

## 5. Risks

| Risk | Mitigation |
| --- | --- |
| **Build re-implements already-merged fixes** (G09 CSS routing, mount arming), producing a duplicate scale mechanism or a second timeout. | §0 table is the scope fence; T6/T8/T9 pin existing behaviour; T10 greps enforce single constant and single orientation signal. |
| `onerror` fires transiently during native `EventSource` auto-reconnect, so a danger banner flickers on every brief blip. | Gate the danger banner on a short debounce (e.g. render only if still disconnected after the browser's first retry window), decided and tested in Step 3; keep it inside `PipelinePage`, not a new timeout constant in the hook. If a constant is unavoidable, name it next to `STALE_AFTER_MS` and justify it in the PR. |
| Age string re-renders: a static "5 minutes ago" goes stale itself on a wall display. | Derive age from a ticking `now` already available to the page (the header's "frame computed" display shows how this is handled); reuse it rather than adding a new interval. Verify with fake timers (T5). |
| Banner height shifts the kiosk board layout. | Banner already renders in the same slot (`shrink-0`); Step 4 viewport pass checks no overflow with the banner shown at all four sizes. |
| `PipelinePage` is mocked across ~47 web test files (`@/api/hooks`), so a change can break unrelated suites. | Banner change adds no new hook; run the **full** `npm run test` (Step 7), not changed-files. |
| Fixed-px dimensions (`.node` 84px, `.node .c` 32px) clip text enlarged by `--scale`. | Step 4 probes rects at `--scale: 1.25`; any fix scales the dimension via `--scale` with a CSS-text test, no new mechanism. |
| Physical legibility cannot be proven by automation; the audit's numbers are an unvalidated starting point. | AC-1.2/1.3 are explicitly human-verified and recorded; evidence uses synthetic fixture data only (E2 PC-2). |
| Dead `staleSince` field is left unexplained. | Follow-up issue filed and linked (Step 6); this PR does not touch server or wire type. |
| E3 Build work lands concurrently on `AppShell.tsx` / kiosk gating. | In-flight check at Step 1 and again before PR; this plan does not touch `AppShell.tsx`, `kiosk.ts`, or the privilege boundary. |
| Spec/code drift recurs: the spec's premise was stale within a month. | Step 1 re-verification is mandatory; the PR body states which spec claims were already satisfied, so the roadmap does not show a phantom gap. |

# Plotroom Pipeline Replica in ccswitch — Design

**Date:** 2026-07-12
**Status:** Approved (design); spec pending user review
**Decision:** Approach C — source-port plotroom's pipeline frontend into ccswitch + backend gap-fill

## 1. Goal & principles

The ccswitch Pipeline tab becomes an **exact replica of plotroom's pipeline experience** —
UI, behaviors, and configurability, chrome included — with ccswitch's session extras
(session label, Fix button) blended in.

Reference implementation: plotroom `origin/main` (commit `5166b52`), checkout at the
clone `/mnt/ssk-ssd/tony/GitHub/.ccswitch/clones/steiner385/plotroom` (temporary read
worktree `/tmp/plotroom-main`). Frontend sources under `frontend/src`.

Principles:

- **Exact by transcription, not imitation.** Port plotroom's actual sources — JSX
  structure, CSS, ordering logic, copy strings — rather than re-implementing them in
  ccswitch idioms. Hand-translation is how the current inferior copy came to be.
- **Pin with plotroom's own tests.** Port plotroom's frontend test suite; extend the
  existing parity harness (`server/github/parity/`) so data semantics stay oracle-checked.
- **No half-dead code** (constitution VI): the old pipeline components are deleted in
  the same change that replaces them.
- **Single source of truth for config**: plotroom's knobs land in ccswitch's existing
  settings store — no parallel `config.json`.

## 2. Frontend port

Port from plotroom `frontend/src` into `web/src/pipeline/`, verbatim-as-possible:

| Ported unit | Plotroom source | Role |
|---|---|---|
| `PipelineView` + `ordering.ts` + `queueFront.ts` | `sections/pipeline/` | Repo sections, attentionSort, cohort collapse, next-to-merge, deploy backlog/chain lines |
| `PrRow` | `PrRow.tsx` | Row header (num/title/⚙ CI change/ETA chip), sub-line, expand panel (cost, workflow impact, Waterfall, check panels), ready+auto-merge action |
| `MetroTrack` | `MetroTrack.tsx` | 3-node (simple) / 5-node (deploy) stage track with glyphs, progress segments, per-node tooltips |
| `QueueTrain` (+ OpsStrip) | `QueueTrain.tsx` | Horizontal merge-train cars: building / waiting / unmergeable / queue-blocked; click-to-scroll to PR |
| `CheckGantt` | `CheckGantt.tsx` | Per-check Gantt bars, p10–p90 band, p50 tick, runner-wait stripes, flake/regression/spot-reclaim annotations |
| `Waterfall` | `Waterfall.tsx` | Lead-time waterfall (5 segments, CVD-safe patterns, honest gaps → null) |
| `StatusStrip` (pipeline tiles) | `StatusStrip.tsx` | 5 bucket tiles (running/queued/deploy/failed/idle), failed ▲/▼ delta vs 5-min baseline, kiosk read-only mode, `bucketPr`/`isFailedPr`/`isActivePr`/`TILE_DEFINITIONS` |
| `LegendPanel` | `LegendPanel.tsx` | Visual vocabulary modal (sourced from the same definitions as the components — no drift) |
| `HealthHeader` + lane derivations | `HealthHeader.tsx`, lane health utils | Rollup pill + 7 lane chips routing to richest detail |
| `PipelineSwitcher` + `useFocusedPipeline` | `shell/PipelineSwitcher.tsx` | Focused repo: localStorage + `?pipeline=` URL param, keyboard-operable |
| `useDashboard` | `useDashboard.ts` | SSE consumption, 90 s staleness, notification frames, bell persistence |
| `format` / `motion` utils, `types.ts` | `format.ts`, `motion.ts`, `types.ts` | ETA formatting (`~Xs/~Xm/~Xh`), reduced-motion scroll, the full plotroom state vocabulary (`DashboardState`, `PrView`, `StageResult`, `CheckView`, `RepoQueueView`, `MergeEtaSimulation`, `PrTimeline`, …) |
| `styles.css` (scoped) | `styles.css` | The pipeline visual language |

**Styling:** the ported CSS is scoped under a single root wrapper class (the same
mechanism plotroom's embed uses with `.prdash-root`) so the CSS island cannot leak into
the Tailwind app and vice versa. No Tailwind translation of plotroom styles.

**Deletions (same change as replacement):** old `PipelineView.tsx`, `StageTiles.tsx`,
`StageTrack.tsx`, `MergeTrainWidget.tsx`, `CheckDrillDown.tsx`, `CheckRowList.tsx`,
`GanttBar.tsx`, `lib/checkState.ts`, `session/stageBucket.ts`, and their tests.

**Kept (shared consumers outside the pipeline):** `/api/prs` + `useAllPrs`
(Dashboard.tsx:559, DesktopShell.tsx:340), `PrPill` (PrList.tsx on session cards).

**Mobile:** the tab panel keeps a `flex-1 min-h-0 overflow-y-auto` scroll wrapper
(known dashboard-tab gotcha); QueueTrain scrolls horizontally as in plotroom.

## 3. Data transport & backend state

- **New SSE endpoint** `GET /api/pipeline/events` (FastAPI `StreamingResponse`),
  pushing a full plotroom-shaped `DashboardState` frame on every poll cycle, plus named
  `notification` events for the bell. The ported `useDashboard` consumes it unmodified
  (EventSource auto-reconnect; `stale` after 90 s without a frame).
- **New assembler** `server/pipeline/dashboard_state.py` builds `DashboardState`:
  - `PrView` from existing `stage.py` / `eta.py` (`StageResult`: stage, substate,
    percent, etaSeconds, etaRangeSeconds, overdue) and checks detail → `CheckView`
    (flake/regression/runner-wait fields exist in the insights machinery).
  - `RepoQueueView` from `merge_train.py` (groups/waiting/unmergeable/queueBlocked +
    health, depth, trainsPerHour, batchSuccessRatePct, ejects24h).
  - `generatedAt` / `staleSince` from the poller cycle.
- **Mutations:** `POST /api/pr/ready-merge` (`{repo, number}` →
  `{markedReady?, cleanReadyToMerge?, alreadyArmed?, autoMergeArmed?}`) implemented
  against GitHub (GraphQL preferred for reads; REST/`gh` for mutations).
- **Endpoint lifecycle:** `/api/pipeline/repos` and `/api/pipeline/trains` are absorbed
  into the SSE state and removed once no consumer references them; `/api/prs` stays
  (shared consumers).

## 4. Gap subsystems (new backend capabilities)

Each is a Python port of plotroom's server logic, parity-checked against a live
plotroom oracle via new harness phases:

1. **mergeEtaSim** — queue simulation producing `{p50Secs, p90Secs, trainsAhead,
   assumesEjects}` per waiting entry; drives the `~p50 / ~p90 p90` ETA chips and
   queue-train car tooltips.
2. **PrTimeline** — capture `createdAt → firstGreenAt → enqueuedAt → mergedAt →
   qaLiveAt → prodLiveAt` in the pipeline history DB; feeds the Waterfall. Honest gaps:
   segments missing an endpoint render nothing.
3. **Deploy environments** — per-repo config `{name, healthUrl, auto, shaKey}`; health
   polling → `envs[{name, liveSha, reachable}]`, deploy backlog (`awaitingQa`,
   `awaitingProd`), deploy chain (`inFlight`, `supersededCount`). Enables the 5-node
   deploy MetroTrack and the backlog/chain paragraphs.
4. **Workflow-impact** — `touchesWorkflows` flag (PR touches `.github/workflows/**`) +
   graph-diff summary (added/removed jobs, timeout changes) for the "⚙ CI change" badge
   and expanded impact card.
5. **CI cost** — per-run `costMinutes` / `costDollars` (+ partial flag) from job
   durations × runner rates; `POST /api/cost/actuals` for calibration.
6. **Notifications engine** — plotroom's 9 event types (ci-failed, group-failed,
   queue-blocked, ready, overdue, prod-live, queue-stalled, duration-regression,
   runner-starvation) + digest; emitted as SSE `notification` frames (browser bell) and
   optional webhook. A minimal emitter (ci-failed, ready, overdue) ships earlier with
   the bell (phase 2); this completes the set.

## 5. Chrome & configurability

- **HealthHeader** above the pipeline content: rollup pill ("All systems green" / "N
  lanes need attention") + 7 lane chips (PR CI, Merge Queue, Main, Deploy, Scheduled,
  Failures/Flake, Cost) with glyphs and one-line summaries. Plotroom routes some chips
  to its Diagnose section, which is out of scope here: ccswitch maps each chip to its
  richest existing surface (Pipeline filtered for PR CI / Merge Queue / Deploy /
  Failures, Insights for Cost and Scheduled/Main until richer targets exist).
- **LegendPanel** modal from the `?` button — definitions sourced from the same
  constants as the rendering components.
- **Notification bell** — Web Notifications on SSE `notification` frames; permission
  gate; localStorage persistence; dedupe tags `{repo}#{pr}|{type}` / `digest`.
- **Kiosk mode** — `?kiosk=true&cycle=N` scoped to the Pipeline tab: auto-cycles the
  focused repo (paused when tab hidden), read-only tiles (divs, not buttons), no row
  expansion.
- **PipelineSwitcher** — focused repo sticky via localStorage + `?pipeline=` deep link;
  URL wins over localStorage; adopts `repos[0]` when repos stream in late.
- **Persistence** — repo-collapse set in localStorage.
- **Config** — plotroom's knobs live in ccswitch's existing settings store (no parallel
  file): global + per-repo `batchSize`; `deploy.environments`; `intervals`
  (hot/sweep/deploy cadences); `notifications` (master switch, per-event toggles,
  webhook, digest); per-repo `workflowPath`, `rollupJobId`. Exposed via safe-subset
  GET/PUT config API with hot-apply, edited through a ported settings-panel section.

## 6. ccswitch extras, blended

- PrRow header carries the **session label** (SquareTerminal icon + name) linking to the
  owning session, and the amber **Fix** button under the existing predicates
  (`pr.session && checks === 'failing' && state actionable`).
- Interaction model follows plotroom: row click expands, title links to GitHub; the
  session label is the way into the session. Nested interactive elements stop
  propagation (and stay out of the row's button role — nested-interactive a11y gotcha).

## 7. Testing & parity

- **Frontend:** port plotroom's tests — PipelineView (sections, collapse, cohort,
  merges-next, deploy lines), StatusStrip (bucketing, delta, kiosk, disabled tiles),
  perf/memoization, PipelineSwitcher (stickiness, URL, keyboard), ordering/queueFront
  units — plus new tests for the blended session extras. jsdom offset stub applies to
  any virtualized content.
- **Backend:** unit tests per new module (dashboard_state, merge sim, timeline capture,
  deploy poller, workflow impact, cost, notifications).
- **Parity harness:** extend `server/github/parity/` with phases beyond stage/ETA —
  queue view, merge sim, timeline — diffing ccswitch SSE state against plotroom
  `/api/state` on shared PRs. Population differences stay informational; frozen-rollup
  rule (drop `checks` on terminal PRs) continues to apply.
- **CI:** `lint-and-test` (ruff + mypy + pytest) and `frontend` (vitest, tsc, eslint)
  green on every PR.

## 8. Delivery phases

Each phase lands as one or more green PRs with auto-merge armed:

1. **Core port** — SSE endpoint + `DashboardState` from existing data; frontend core
   (StatusStrip, PipelineView + ordering/cohort, PrRow, MetroTrack, QueueTrain,
   CheckGantt); ready+auto-merge action; session extras; old components deleted.
2. **Chrome** — HealthHeader, LegendPanel, PipelineSwitcher, kiosk, collapse
   persistence; notification bell UI backed by a minimal emitter covering events
   derivable from existing data (ci-failed, ready, overdue).
3. **Data gap-fill** — mergeEtaSim, PrTimeline + Waterfall, deploy environments +
   chain, workflow-impact.
4. **Config & cost** — settings surface + hot-apply, CI cost, notifications engine +
   digest.

## 9. Error handling & empty states (plotroom behaviors, replicated)

- No state yet → "Loading pipeline…" (`role="status"`); stale (no SSE frame in 90 s) →
  stale indicator; SSE drop → EventSource reconnects, `connected=false` meanwhile.
- Repo with no visible PRs → "no active PRs" (+ hidden count when a filter is active);
  no queue → QueueTrain omitted; no deploy data → backlog/chain lines omitted; missing
  timeline endpoints → Waterfall omits segments (never fabricates).
- Empty-bucket tiles disabled, except an active filter tile stays clickable so a stuck
  filter can always be cleared.

## 10. Out of scope

- Plotroom's non-pipeline sections (Model & Edit / Designer's ProtectionMap, Build,
  Tune, Optimize, Insights beyond the lane chip routing targets).
- Publishing/consuming plotroom as an npm dependency (approach A/B — rejected in favor
  of source port).
- Changes to plotroom itself.

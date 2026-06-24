# Combined App — Architecture

**Date:** 2026-06-23
**Status:** Draft for review
**Source:** Software Architect persona analysis, reconciled with the CI/CD and founder personas.
**Reads with:** `2026-06-23-combined-app-requirements.md` (NFRs live there), `2026-06-23-combined-app-roadmap.md`.

> **Sequencing note:** this describes the *destination* architecture (full Level-3 merge). Per the requirements doc §0, the recommended delivery order (Option A) reaches it incrementally with automations shipped first on thin integration. The module layout below is the end state; early milestones populate only the parts they need.

## 1. Runtime shape

One **FastAPI daemon** (owns pollers, engine, history DB, sessions) + one **edge process** (stateless, serves browser HTTP/WS/SSE) + one **React SPA**. Plotroom's Node engine is re-expressed as a Python subpackage hanging off the `server/github/` data foundation that Phase 0 establishes. The two React frontends unify (both are React 19 + Vite + TS + Tailwind; Plotroom already publishes a mountable `<PrDashboard/>` from `plotroom/embed`).

## 2. Backend module layout (proposed end state)

```
server/
  github/
    pr_status.py          # EXISTS — pure GraphQL build/parse; Phase 0 adds CheckDetail, head_sha, checks_detail
    pr_poller.py          # EXISTS — PrStatusPoller; THE single GitHub poller (absorbed, never duplicated)
    parity/               # Phase 0 — dev oracle harness: coverage / normalize / harness / __main__
  pipeline/               # ← PORT of Plotroom server/pipeline-model/ (the stage machine; pure functions)
    parse_jobs.py  parse_triggers.py  expand_matrix.py  gating.py
    narrow_events.py  derive_static.py  stage_machine.py  types.py
  estimator/              # ← PORT of Plotroom server/estimator/ (~22 files; mostly pure)
    progress.py           #   conditional-remaining estimator (crown-jewel heuristic)
    waits.py              #   runner pickup-wait learning
    regression.py  starvation.py  queue_health.py  deploy_status.py
    critical_path.py  calibrate.py  flake_summary.py  required_prefixes.py  ...
  ancestry.py             # ← PORT ancestry.ts — deploy ancestry over GitHub compare API (api mode only)
  pipeline_history.py     # ← PORT history.ts — the ~17-table learned-history store (thin sqlite3 repo)
  notifier.py             # ← PORT notifier.ts — notification taxonomy + debounce
  digest.py               # (DROP per requirements; listed for provenance only)
  pipeline_state.py       # snapshot builder feeding /api/pipeline/state + live channel; frame-dedup
  automations/
    triage_lens.py        #   ranked attention queue (session status ⨝ pipeline state)
    work_dispatcher.py    #   idle/done session → next-task proposal
    follow_through.py     #   pipeline event → owning session by live branch
  sessions/
    orchestrator.py       # EXISTS — owns PrStatusPoller wiring (~line 168); gains engine handle
    remote.py             # EXISTS — daemon↔edge RPC; PullRequestInfo crosses it; new types must too
  api/
    sessions.py           # EXISTS — list_sessions attaches session["pr"]
    pipeline.py           # ← NEW router: GET /api/pipeline/state, /metrics, live events
```

**Why this maps cleanly:** Plotroom's `pipeline-model` is a *pure-functional* pipeline (parse → expand → gate → narrow → derive) and the estimator files are mostly pure (history rows in → ETA out). I/O is concentrated in `pipeline_history.py` (reads) and the poller (writes). Keeping that purity boundary intact is what makes the parity harness a clean oracle and the heuristics unit-testable without a DB or network.

## 3. Data flow

```
GitHub GraphQL ──► PrStatusPoller (one loop; sweep+hot intervals) ──► raw PR + per-check rollup
                                  │ writes raw facts                         │
                                  ▼                                          ▼
                       pipeline_history (~17 tables) ◄──reads── pipeline/ stage machine (pure)
                                  ▲ writes durations/waits/gaps              │ stage + parked substate
                                  └────────────────── estimator/ (pure) ◄────┘
                                                          │ ETAs, overdue, queue/starvation
                                                          ▼
                                              pipeline_state (frame-dedup snapshot)
                                       GET /api/pipeline/state │ live events ──► Pipeline view
                                                          ▼
                                              automations/ (Triage · Dispatch · Follow-through)
                                                          │ JOIN on worktrees.live_branch == PR headRefName
                                                          ▼
                                              orchestrator (status, chips, propose→approve) ──► push/phone
```

**The branch→session join is the spine and the highest-risk seam.** `orchestrator._pr_pairs()` currently reads the *stored* `r.branch`; it MUST read `worktrees.live_branch` live from HEAD (PR #221 / `pr-join-branch-must-be-live` memory) — ccswitch's completion workflow moves worktrees onto fresh `feat/*` branches, so the stored branch goes stale and every automation silently mis-routes. All three automations inherit this fix.

## 4. Data stores (ADR-1: separate engine DB — recommended)

| | Engine | Schema | Writer |
|---|---|---|---|
| ccswitch | SQLAlchemy + SQLite (WAL), Alembic | `sessions`, `app_sessions`, `audit_events`, `attachments`, `app_settings` | dev `create_all`+`init_db` backfill (no ALTER); prod Alembic |
| Plotroom | better-sqlite3 (single-writer) | ~17 time-series tables in `history.db` (`check_durations`, `queue_waits`, `runner_waits`, `eta_accuracy`, `group_runs`, `observed_pools`, `cost_actuals`, …) | own `CREATE TABLE IF NOT EXISTS` + hand-rolled ALTERs |

**Recommendation:** keep the engine's learned history in a **separate `pipeline_history.db`**, not folded into the main DB. The append-heavy NOT-NULL time-series schema fights ccswitch's nullable-only / no-ALTER dev rule; isolating it keeps the two migration disciplines from colliding. Only the daemon writes it; the edge reads via RPC. The test shim that redirects in-memory `:memory:` → a durable temp file (PR #208) must be applied to this second DB too.

**History continuity is a hard requirement** (not nice-to-have): a one-time, schema-faithful `history.db` → `pipeline_history.db` table-by-table copy at estimator-cutover, so ETAs are accurate from day one instead of cold-starting for ~20 runs/check. `cost_actuals`/`observed_pools` only migrate if their consumers are ported (cost explorer is dropped per requirements).

## 5. Key interfaces (concrete shapes)

```python
# Stage machine (pipeline/types.py) — pure, no I/O
@dataclass(frozen=True)
class StageSnapshot:
    repo: str; number: int; head_sha: str
    stage: str          # 'ci' | 'merge-queue' | 'qa-deploy' | 'awaiting-prod' | 'shipped'
    parked: str | None  # 'draft' | 'conflicting' | 'ci-failed' | 'queue-group-failed' | None
    checks: tuple[CheckDetail, ...]      # reuse the Phase-0 CheckDetail (single source)

# Estimator (estimator/progress.py)
@dataclass(frozen=True)
class StageEta:
    stage: str
    eta_secs: float | None   # conditional-remaining; None until ≥5 samples
    overdue: bool
    accuracy_label: str      # "typically ±2m (n=14)"

# History read interface — estimator depends on THIS, not on sqlite
class PipelineHistory(Protocol):
    def durations(self, repo, check, event) -> list[float]: ...
    def runner_waits(self, repo, check, event) -> list[float]: ...
    def eta_accuracy(self, repo, stage) -> list[tuple[float, float]]: ...
    def record_duration(self, ...) -> None: ...   # poller-side write

# Snapshot served to UI/live channel — MUST match the shape parity/normalize.py encodes
def build_state() -> dict          # { repos: [{ nameWithOwner, prs: [PrSnapshot...] }] }

# Automation join — the load-bearing primitive
def session_for_branch(repo: str, live_branch: str) -> SessionRecord | None
def pipeline_for_session(s: SessionRecord) -> StageSnapshot | None

# Daemon↔edge RPC (sessions/remote.py) — each NEW type needs to_dict/from_dict + round-trip test
RemoteOrchestrator.pr_for(repo, branch) -> PullRequestInfo | None      # EXISTS
RemoteOrchestrator.pipeline_for(repo, branch) -> StageSnapshot | None  # NEW
RemoteOrchestrator.pipeline_state() -> dict                            # NEW
```

**Contract anchor:** `server/github/parity/normalize.py` is the machine-readable spec of the `/api/state` JSON shape. `pipeline_state.build_state()` must emit exactly what `normalize.py` consumes — that is how the harness stays a valid oracle through every phase.

## 6. Strangler mechanics — cutover seams

The seam is the parity harness: live Plotroom `/api/state` = oracle, the Python engine = candidate, `PHASE_FIELDS[n]` = the contract widened one phase at a time. The harness fails loudly if a phase claims a field it doesn't produce.

| Phase | Field set widened | Old runs alongside? | Retired in the same PR |
|---|---|---|---|
| 0 | basic PR facts | Plotroom as oracle only | — (no behavior change) |
| 1 | + `stage`, `eta_secs`, `overdue` | yes (oracle) | Python engine becomes source of stage/ETA; `history.db` migrated in |
| 2 | + ancestry, live stream, notifications | yes | Plotroom notifier/SSE decommissioned |
| 3 | frontend | Plotroom UI standalone | retire Plotroom's standalone UI |
| 4 | — | — | decommission Plotroom service entirely; `normalize.py`/live-harness die |
| 5–7 | automations (net-new) | n/a | — |

**No half-dead code (Constitution VI):** each phase ships a working unified app with one capability cut over and its Plotroom counterpart deleted. Reconcile Plotroom's `required-prefixes` with ccswitch's existing prefix concept into ONE Python impl — do not land a third parallel copy.

## 7. Concurrency / runtime model (ADR-3/4)

- **One poller**, an asyncio task owned by `orchestrator`. The pipeline engine runs **synchronously inside the poller's sweep callback** (pure functions over just-fetched data + history reads) — no second polling loop (ADR-3). Plotroom's `sweepMs/hotMs/deployMs` tiers map onto the existing poller's interval + a hot mode.
- **Live updates:** prefer reusing ccswitch's existing WebSocket channel; if SSE frame-dedup proves necessary, the **edge subscribes to the daemon event bus over RPC and re-emits** (ADR-4) — keeps the edge the sole browser-facing process.
- **Single-writer SQLite** end to end: only the daemon writes; edge reads via RPC. Keep history reads synchronous (sqlite3) inside the poller task; never put a blocking sqlite handle in an async edge handler (the edge never touches the file).

## 8. Risk register (port-focused)

| # | Risk | L | I | Mitigation |
|---|---|---|---|---|
| R1 | Behavioral drift porting tuned heuristics (conditional-remaining estimator, pickup-wait learning, phase-aware needs narrowing) | H | H | Parity harness as a hard gate per field; port file-by-file; never loosen `PHASE_FIELDS` |
| R2 | Stale-branch join — `_pr_pairs` reads stored `r.branch`; all 3 automations mis-route | H | H | Route through `worktrees.live_branch` (PR #221); test a worktree moved to a fresh PR branch still joins |
| R3 | History cold-start — garbage ETAs ~20 runs/check if `history.db` not migrated | H | M | Schema-faithful copy at estimator cutover; verify row counts + ETA backfill |
| R4 | RPC serialization of new types fails to round-trip across daemon/edge | M | H | `to_dict`/`from_dict` + remote-mode round-trip test on every new RPC type |
| R5 | Two-runtime divergence while both live | M | M | Strict strangler phasing; delete each Plotroom path in its cutover PR; harness compares continuously |
| R6 | Parallel implementations (required-prefixes / status palette / poller) | M | H | Grep-first (Constitution VI); reconcile into one impl; map onto existing `statusTier` |
| R7 | Sync sqlite call sneaks into an async edge handler | M | M | Engine + history I/O only in the daemon poller task; lint for sqlite handles in `api/` |
| R8 | `/api/state` shape drift breaks the oracle silently | M | M | `normalize.py` = single shape encoder + golden-fixture contract test; pin a recorded blob |
| R9 | NOT-NULL engine columns collide with no-ALTER nullable-only rule | M | L | Separate `pipeline_history.db` with its own bootstrap; main-DB additions nullable |
| R10 | Frontend dual-render drift (Pipeline view in mobile but not desktop) | M | L | Single-source the view (accounts/archived dual-impl traps) |
| R11 | In-task engine stalls the poller sweep | L | M | Keep estimator pure & bounded; no per-PR network calls; offload only if profiling demands |
| R12 | File-only credential config becomes web-writable on the port | L | H | Port the file-only rejection set verbatim |

## 9. Architecture decisions (ADRs)

- **ADR-1 — Engine history store:** separate `pipeline_history.db` (vs fold into main SQLite). **Recommend separate** — schema discipline mismatch; no cross-DB joins needed (the branch join is in-memory).
- **ADR-2 — History layer:** thin `sqlite3` repository porting `history.ts` near-verbatim (vs full SQLAlchemy ORM). **Recommend thin repo** behind the `PipelineHistory` Protocol — low-drift faithful port, readable next to Plotroom's SQL, no ORM overhead on hot reads.
- **ADR-3 — Engine placement:** inline in the existing poller sweep (vs separate task/subprocess). **Recommend inline** — pure functions over already-fetched data; a second loop re-introduces the two-poller anti-pattern. Revisit only if profiling shows stalls (R11).
- **ADR-4 — Live transport:** reuse ccswitch's WebSocket channel; if SSE needed, edge subscribes to the daemon bus and re-emits (vs daemon serving SSE directly). **Recommend reuse-first.**
- **ADR-5 — Notifications:** keep Plotroom's tuned event taxonomy + debounce, deliver via ccswitch push/phone + session-card chip (vs porting the full browser-bell/host-command sink stack). **Recommend taxonomy yes, parallel sinks no** (matches the chosen surfaces; rejects "fold into Plotroom UI").
- **ADR-6 — Config model:** a `pipeline.*` namespace inside ccswitch config preserving Plotroom's file-only/PUT-writable split (vs one fully unified config). **Recommend the namespace** — preserves the security boundary with minimal reshaping.
- **ADR-7 — GitHub App mode / webhooks:** **drop for v1** (requirements §1); ccswitch already authenticates per-account via the `gh` keyring — unifying with Plotroom's multi-installation token map is a real, deferred decision, not free.

**No material contradiction with the committed overview design.** The one deliberate, well-justified deviation (carried from the Phase-0 plan) is declining the `PrSnapshot`/`PrSnapshotPoller` rename: `PullRequestInfo` is RPC-serialized across `remote.py` + 6 test files, so the rename ripples for zero behavior gain. One poller, extended in place.

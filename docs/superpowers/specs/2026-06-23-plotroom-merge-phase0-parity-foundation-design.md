# Plotroom Merge — Phase 0: Parity Harness + Data Foundation

> **⚠️ Parity model superseded (2026-06-24), as built in PR #258.** A live run revealed
> this spec's assumptions about Plotroom's `/api/state` were wrong: it exposes **no
> branch/`head_sha`/PR `state`** (only a pipeline `stage`), and it tracks *all* PRs while
> ccswitch's per-session poller doesn't. So the as-built harness is **number-keyed** (not
> branch-keyed), **intersection-based** (`ok` = no field mismatches on shared PRs;
> oracle-only/candidate-only are informational, not failures), and Phase-0 coverage is
> `{number,url,checks}`. Finding + evidence: `docs/superpowers/evidence/2026-06-24-parity-phase0-finding.json`
> and `…-parity-merged-stage-OK.json`. The data-foundation half (extending `PullRequestInfo`)
> shipped as specced. Sequencing is value-first — see the roadmap.

**Date:** 2026-06-23
**Status:** Approved (design)
**Program:** See `2026-06-23-ccswitch-plotroom-merge-overview-design.md` for the full
roadmap and the Level-3 (true merge) decision this phase opens.

## Purpose of this phase

De-risk the entire Plotroom→Python port **before** any tuned heuristic is touched. Phase 0
delivers two things and **no user-visible behavior change**:

1. A **data foundation** in ccswitch's Python backend: a PR/CI snapshot model and a single
   GitHub poller that *absorbs and replaces* today's `server/github/pr_poller.py`
   (`PrStatusPoller`), structured so later phases can hang the stage machine and ETA
   engine off it.
2. A **parity harness**: a developer tool that runs the Python snapshot against the same
   repos as a live Plotroom instance and **diffs the two**, so every later phase has a
   pass/fail oracle for "does the Python output match Plotroom?"

Phase 0 explicitly does **not** port the stage machine or the estimator (Phase 1). It
reproduces only the *basic* PR/CI facts ccswitch already computes today — so parity is
achievable now and the harness itself is proven against a known-equal baseline.

## Why a parity harness first (the core bet)

Plotroom's value is its *tuned* output — stages, ETAs, queue/deploy signals. A blind
re-implementation in Python would drift silently. The constitution's Principle III
("verify against the real binary/API, not assumptions") says: don't trust the port,
*measure* it. Plotroom already exposes a canonical, queryable oracle — `GET /api/state`
(plus `GET /api/metrics`) — so the harness is feasible without instrumenting Plotroom's
internals.

The bet: **if Phase 0 can demonstrate byte-for-byte (modulo documented tolerances) parity
on the basic fields, then every later phase reduces to "port file X, watch the harness
go green."** The harness is the safety rail that makes a strangler port of tuned logic
tractable.

## Current state (verified)

- **ccswitch** — `server/github/pr_status.py` (pure: `build_pr_query`,
  `parse_pr_response`, `PullRequestInfo`) + `server/github/pr_poller.py`
  (`PrStatusPoller`, mirrors `server/usage/poller.py`; runs `gh` via
  `gitrepos._run_gh`; in-memory cache keyed `(repo, branch)`). `Session.branch` carries
  the worktree branch; `list_sessions` attaches `session["pr"]`.
- **Plotroom** — Node/Express. Canonical snapshot at `GET /api/state`; aggregates at
  `GET /api/metrics`; SSE at `GET /api/events`. Learned history in `history.db`
  (`check_durations`, `queue_waits`, `runner_waits`, `eta_accuracy`, `group_runs`,
  `state_samples`, `observed_pools`, …). Stage machine in `server/pipeline-model/`,
  ETA engine in `server/estimator/`.

## Design

### Part A — Data foundation (Python)

Introduce a snapshot layer that the current pill reads from and later phases extend. Keep
it additive and field-compatible with today's `PullRequestInfo` so the dashboard is
unchanged in this phase.

- **`server/github/pr_snapshot.py`** — a richer, forward-looking dataclass
  `PrSnapshot` that is a *superset* of the existing `PullRequestInfo` (`number`, `url`,
  `state`, `checks`, `title`) plus fields later phases will populate and that the harness
  compares: `head_sha`, `is_draft`, `merged`, `updated_at`, and a `checks_detail` list of
  per-check `{name, status, conclusion, started_at, completed_at}`. In Phase 0 only the
  fields ccswitch can already source from GraphQL are filled; the rest are `None`/empty
  and are excluded from parity comparison via the field-coverage map (below).
- **Absorb `PrStatusPoller`.** Rename/relocate the poller into the snapshot module as
  `PrSnapshotPoller`, preserving its `UsagePoller`-shaped contract (interval, jitter,
  backoff, injectable `query_fn`, `cached(repo, branch)`). Extend `build_pr_query` /
  `parse_pr_response` to also request `headRefOid` and the head commit's
  `statusCheckRollup.contexts` (per-check detail) so `checks_detail` is populated.
  **Delete the old `pr_poller.py` in this same PR** (single source of truth — no
  half-dead duplicate); update the orchestrator wiring and `list_sessions` to the new
  name. The serialized `pr` payload keeps its existing shape (the new fields are additive
  and optional), so `PrPill` and all web tests are untouched.
- **No new frontend hook** — the `pr` key continues to ride the existing session-list
  payload (avoids the `dashboard-hooks-partial-mocks` fragility trap).
- **New column policy:** any new persisted column is **nullable** (dev SQLite `init_db`
  backfills; no ALTER) per the repo's migration rule. Phase 0 adds no required columns.

### Part B — Parity harness (developer tool, not a server feature)

A standalone, opt-in tool — never wired into the request path or the default app lifecycle.

- **`server/github/parity/harness.py`** — given a Plotroom base URL
  (default `http://127.0.0.1:4400`) and a repo list, it:
  1. Fetches Plotroom's `GET /api/state` (the **oracle**) and normalizes it to a
     comparable shape via an adapter.
  2. Computes ccswitch's **candidate** snapshot for the same `(repo, branch/PR)` set
     using `PrSnapshotPoller` synchronously (one sweep).
  3. Diffs candidate vs oracle **only over the fields in the phase's coverage map**, and
     emits a structured report: matched / mismatched / oracle-only / candidate-only,
     keyed by `(repo, prNumber, field)`.
- **`server/github/parity/coverage.py`** — `PHASE_FIELDS: dict[int, set[str]]`, the
  explicit list of fields each phase claims parity on. **Phase 0 covers exactly:**
  `number, url, state, is_draft, merged, head_sha, checks (rollup),
  checks_detail[].{name, conclusion}`. Stage, ETA, queue, deploy, and notification fields
  are **declared not-yet-covered** and skipped — comparing them now would be noise. Each
  later phase widens this map; the harness fails loudly if a phase claims a field it
  doesn't actually produce.
- **`server/github/parity/normalize.py`** — the Plotroom-`/api/state`→comparable adapter.
  This is the one place that encodes Plotroom's JSON shape, isolated so a Plotroom schema
  change touches a single file. Documented field-by-field tolerances live here:
  - **Timestamps:** compared at second granularity (both sides ISO-8601 UTC).
  - **"Most relevant PR" selection:** ccswitch's existing open/draft > merged >
    most-recent-closed rule must match Plotroom's; if Plotroom surfaces multiple PRs per
    branch, the harness compares the same selection and flags a selection mismatch
    distinctly from a field mismatch.
  - **Float fields (ETAs):** not in Phase 0 coverage; when later phases add them,
    tolerance is `±max(2s, 5%)` (documented per field), never exact equality.
- **CLI entry:** `python -m server.github.parity --plotroom-url <url> --repos a/b,c/d
  [--json report.json] [--phase 0]`. Exit non-zero if any covered field mismatches, so it
  can gate a phase in CI later. Human-readable table by default; `--json` for machine use.

### What "parity passing" means for Phase 0

For every repo with open PRs visible to both tools, every **Phase-0-covered** field
agrees (within documented tolerance) for the selected PR per branch, with zero
candidate-only or oracle-only PRs in the covered set. That baseline proves the harness
itself is correct (the two tools already compute these basics the same way) — which is the
whole point of running it on covered-basics first.

## Components summary

| File | Role |
|---|---|
| `server/github/pr_snapshot.py` | `PrSnapshot` dataclass + `PrSnapshotPoller` (absorbs `PrStatusPoller`) |
| `server/github/pr_status.py` | extended query/parse (per-check detail, head OID); kept pure |
| `server/github/parity/harness.py` | diff candidate vs Plotroom `/api/state` oracle |
| `server/github/parity/coverage.py` | `PHASE_FIELDS` — per-phase parity field map |
| `server/github/parity/normalize.py` | Plotroom `/api/state` adapter + documented tolerances |
| `server/github/parity/__main__.py` | CLI entry |
| *(deleted)* `server/github/pr_poller.py` | replaced by `pr_snapshot.py` in the same PR |

## Error handling

- **Poller** keeps the existing contract: `gh` absent / unauth / 5xx / rate-limited → log,
  exponential backoff, leave cache untouched; PR data stays decorative and never raises
  into a dashboard request.
- **Harness** is a dev tool: a Plotroom fetch failure (service down, non-200) is reported
  as a harness error, not a parity mismatch (don't conflate "Plotroom unreachable" with
  "outputs differ"). `--json` still writes a report with the error recorded.
- Branch with no PR / detached worktree (null branch) → no snapshot → excluded from
  comparison (matches today's "no pill" behavior).

## Testing (TDD)

**Server (pytest, `server/tests/`):**
- `pr_status` extended parse: per-check `contexts` → `checks_detail`; `headRefOid` →
  `head_sha`; existing rollup→`checks` mapping and "most relevant PR" rule unchanged.
- `PrSnapshotPoller`: caching, interval sweep with injected `query_fn`, backoff on
  failure (mirrors the existing `test_poller_backoff`/`test_poller_cached`); the old
  poller's tests migrate to the new name and the old module is gone.
- `list_sessions` still attaches a `pr` of the existing shape (regression guard) and the
  new fields are present-but-optional.
- **Harness with a fake oracle:** feed `harness` a canned Plotroom-`/api/state` JSON and a
  canned candidate; assert the diff classifies matched / mismatched / oracle-only /
  candidate-only correctly, respects `PHASE_FIELDS[0]` (ignores stage/ETA fields), and
  applies timestamp/selection tolerances.
- **Coverage guard:** a test asserts the harness refuses to report parity on a field not
  in the current phase's `PHASE_FIELDS` (prevents a future phase from silently claiming
  uncovered parity).
- `normalize`: golden-file test mapping a recorded real `/api/state` blob → comparable
  shape (recorded once from the live instance, checked in as a fixture).

No web test changes — the `pr` payload shape and `PrPill` are unchanged this phase.

## Out of scope (deferred to later phases)

- Porting the stage machine (`pipeline-model/`) and the estimator (`estimator/`) — Phase 1.
- Deploy ancestry, SSE, notifications, digest — Phase 2.
- Any UI for the pipeline, and retiring Plotroom's frontend — Phase 3.
- Migrating Plotroom's `history.db` learned history into ccswitch — Phase 1 (the engine
  that consumes it lands there); Phase 0 reads only live GitHub state, no history.
- Wiring the harness into CI as a required gate — done when a phase first needs it as a
  gate; Phase 0 ships it as a local CLI.

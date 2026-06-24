# M5 Slice 1c — ETA engine (parity-gated), and its hard prerequisite

> **Status: BLOCKED on a live ETA parity surface.** The estimator is the crown-jewel tuned logic and the worst thing to port without the parity gate. Do NOT start 1c.2 until the gate below passes.

## The blocker (why 1c is on hold)
The parity harness can only verify ETAs against a PR that currently has one. As of 2026-06-24 the oracle (live Plotroom watching `steiner385/cc-account-switcher` at :4400) shows **0 `ci`-stage PRs and 0 PRs with a non-null `stage.etaSeconds`** — because ETAs only render on a PR mid-CI with ≥5 historical samples, and there's no such PR right now. With nothing to diff, a ported estimator can't be parity-proven; unit tests alone won't catch subtle divergence from Plotroom's tuned output.

### Unblock (operational — the user's call; not code I'll do unprompted)
One of:
- **Wait for organic CI** — when one of your PRs is running CI, a `ci`-stage PR with a non-null ETA appears in the oracle.
- **Point the live Plotroom at a busier repo** — edit the running instance's `config.json` `owners`/repo set (instance runs from `/mnt/ssk-ssd/tony/GitHub/pr-dashboard`, data at `data/`) to watch a repo with active CI + accumulated history, then restart it. This is an operational change to a running service — confirm before doing it.

**Gate to start 1c.2:** `curl -s http://127.0.0.1:4400/api/state | python3 -c "import json,sys;print([p['number'] for r in json.load(sys.stdin)['repos'] for p in r['prs'] if (p.get('stage') or {}).get('etaSeconds') is not None])"` prints a non-empty list.

## Confirmed prerequisites (ready)
- **Migration source FOUND:** `/mnt/ssk-ssd/tony/GitHub/pr-dashboard/data/history.db` (40 MB; `check_durations`=51156, `runner_waits`=53459, `eta_accuracy`=3305, all 17 tables). Read-only-open it via `file:…?mode=ro` (it's WAL, live-written — never write to it).
- **Float-tolerance** for ETA parity is already specced in `normalize.py` (`±max(2s, 5%)`, never bare `==`).

## Sub-slices (each parity-gated once the surface exists)
- **1c.1 — history migration.** Schema-faithful copy of `check_durations`, `queue_waits`, `runner_waits`, `eta_accuracy`, `group_runs`, `observed_pools` from the source DB into a ccswitch engine store (`pipeline_history.db`, ADR-1: separate from the main SQLite). Verify row counts match the source. *(Independently verifiable now, but low value until 1c.2 consumes it — do it as the first step of the 1c build, not standalone.)*
- **1c.2 — `estimator/progress.ts`** (113 lines) → pure `progress(history_rows, elapsed, now) -> (percent, eta_secs, overdue)`. Unit-test the bimodal cold/warm re-anchoring, overdue-when-elapsed-exceeds-all-samples, and <5-samples→no-point-ETA cases. Then widen `PHASE_FIELDS[1]` with `eta_secs`/`overdue` (float tolerance) and confirm live-parity green on the ci-stage PR.
- **1c.3 — `estimator/waits.ts`** (143 lines) → pure runner pickup-wait (`wait = startedAt − max(needs' completedAt)`, per-`(repo,check,event)` median); feeds 1c.2's still-queued estimate.

## Done-so-far context (M5, all in PR #258)
Phase 0 (data foundation + parity harness + working gate) · slice 1a open-stage (live-parity green) · 1b queue (unit-tested, parity-deferred — no merge queue here) · merged slice (live-parity **229/229, 0 mismatches**, full population). The stage machine is parity-complete for every stage this repo exercises. Queue (1b), deploy (1d), and now ETA (1c) all await a richer oracle to be parity-verified — the recurring constraint is that this single repo's live state is a thin parity surface.

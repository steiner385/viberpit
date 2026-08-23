# Fleet Recovery — Design

**Date:** 2026-08-22
**Status:** Approved (design); implementation plan pending
**Scope:** dobby (edge/master) + rocky (session runner)
**Related:** #734 (edge wedges on a slow Postgres consumer), #736 (disabled account ranked #1)

## Problem

An end-to-end audit of the live fleet found the app unusable and unable to recover on its own:

- **No session can be opened.** Every `/api/sessions/{id}/transcript` returns 503 after 30s.
- **The session queue is deadlocked.** 14 sessions queued, oldest 16h, zero running.
- **rocky's event loop is ~100% saturated**, so every RPC is slow and ~10% time out outright.
- **The edge wedges** until `systemctl restart ccswitch.service`. This happened twice on 2026-08-22
  in two *different* connection pools (#734 was Postgres; the audit found the same in httpx).

All six root causes below are confirmed by measurement, not inference. Two prior fixes in this area
patched symptoms rather than causes (see the `2026-07-02` comment at `server/ipc/client.py:133`),
so this design targets causes and sweeps each pattern for siblings.

## Root causes

### RC1 — Blocking SQLite on the event loop starves rocky

`server/pipeline/history.py:123` opens a **synchronous stdlib** connection:

```python
self._conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
```

`recent_durations` queries it directly from the event loop, once per check per PR per repo, driven
by the `pr_population` background loop. A `py-spy` dump of the live daemon caught the loop inside
`server/pipeline/history.py` in **8 of 8 samples over 24 seconds**:

```
_loop (server/github/pr_population.py:624)
  refresh_once (server/github/pr_population.py:573)
    _enrich_with_eta (server/github/pr_population.py:406)
      compute_pr_eta (server/pipeline/eta.py:23)
        compute_progress (server/pipeline/estimator.py:46)
          recent_durations (server/pipeline/history.py:131)   ← blocking sqlite3
```

While this runs, nothing else on the daemon is scheduled. This is upstream of most observed
symptoms: RPC timeouts, the flapping events WebSocket, and sockets accumulating in `CLOSE-WAIT`
on the edge.

### RC2 — Transcript replay globs 1,696 directories over a network mount

`server/sessions/replay.py:259`:

```python
metas = sorted(projects_root.glob(f"*/{sid}/subagents/*.meta.json"))
```

`projects_root` lives on an **sshfs mount from dobby**, traversed across a DERP-relayed link at
~54ms RTT. The leading `*/` forces a descent into every project directory.

| measurement | value |
|---|---|
| project dirs under `projects/` | **1,696** (identical for all 8 accounts) |
| `subagents/` dirs that actually exist | **0**, across every account |
| cost of a single cached top-level listing | 0.017 s |
| observed `transcript_window` latency | **hangs; >35 s, 5/5 attempts** |

The traversal performs ~1,696 network round trips to return an **empty list, every time**. Reading
the transcript file itself is fast (780 KB read + JSON parse in 0.77 s on rocky) — the cost is
directory traversal, not I/O volume.

### RC3 — Finished sessions hold capacity slots forever

The drain loop breaks on its first iteration (`server/sessions/orchestrator.py:915`):

```python
if self._active_session_count() >= await self._max_active_sessions():
    break
```

`NON_ACTIVE_SESSION_STATUSES` is `{ended, archived, queued}`, so `idle`, `complete` and `done`
count as active. Seven such sessions occupy a cap of seven; none is running.

Releasing a slot requires auto-archive, which has **two independent blockers**:

1. **`manual-unarchive` is a permanent opt-out.** `orchestrator.py:1428` skips auto-archive whenever
   `auto_archive_opt_out_reason` is set. **6 of the 7** cap-holding sessions carry
   `manual-unarchive`. These would never be archived even by a perfectly healthy autopilot —
   unarchiving N sessions permanently removes N capacity slots.
2. **Autopilot fails every cycle.** `autopilot.decide` has failed continuously for hours
   (12+ consecutive observed), returning `escalate` instead of acting. It logs a
   self-contradictory message — `Claude Code returned an error result: success` — which
   `server/sessions/helper_call.py:139` documents as the SDK's shape for an API error whose real
   status must be read off the collected messages. Account headroom is a likely contributor
   (`tony_kindash.com` at 100% weekly, `tony_steinonline.net` at 91%).

The failure is silent: nothing in the UI explains why a queue of 14 is not draining.

### RC4 — Three datetime conventions share one naive column

`sessions.created_at` / `updated_at` are `timestamp WITHOUT time zone`, written by three different
conventions in the session paths:

| convention | meaning | sites |
|---|---|---|
| `datetime.utcnow()` | naive **UTC** | 14 |
| `datetime.now(timezone.utc)` | tz-aware UTC | 9 |
| `datetime.now()` | naive **local** | 2 |

Both hosts' clocks agree to within one second, so this is not skew. Mixing naive-UTC and
naive-local writes puts rows up to **4 hours in the future** relative to Postgres `now()` — four
sessions currently show negative age. Every staleness threshold, sweep interval, ordering and
"last active" display computed from these columns is wrong.

This is the repo's own `eliminating-parallel-implementations` anti-pattern: there is no single
source of truth for "now".

### RC5 — Connection pools have no health or bound, so one slow consumer wedges the edge

Two pools, one defect class, both hit on 2026-08-22:

- **httpx → rocky.** `server/ipc/client.py:97` constructs `AsyncClient(..., timeout=30.0)` with no
  `httpx.Limits` and a single scalar timeout. When RC1 stalls rocky, connections die and the master
  accumulates dead sockets: **17 observed in `CLOSE-WAIT`**. Routes needing an RPC then blocked for
  the full 30s and 503'd, while no-RPC routes stayed instant. A *fresh* connection to the same
  daemon returned 200 in 0.5 s. Only a restart cleared it (17 → 0).
- **Postgres.** #734: an unbounded 12 GB `ci_test_result` query held a pool slot for 1,986s; the
  edge stayed wedged with zero active queries and `/healthz` — which gates deploys — hung with it.

The shared failure: *no timeout, no bound, no health check, no self-recovery*.

### RC6 — The runner runs the whole orchestrator, not just sessions

`orchestrator.start()` spawns **17 background loops**, all executing on rocky:

| Belongs on a session runner | Does not |
|---|---|
| stall-watchdog, stuck-session-sweep, titling, goal-inference, rotation, token-refresh | pr-population, pr-poller, pr-branch-monitor, cicd-doctor, archive-audit, backlog-runner, autopilot, deploy-watcher, worktree-janitor, archive-limit-sweep |

The master/runner split relocated the entire orchestrator, not just session ownership. The two
worst offenders are in the right-hand column: `pr-population` (RC1) and `cicd-doctor` (#734's 12 GB
query). This is the structural answer to #734's question — *why does rocky query the CI tables at
all?* It should not.

### Amplifier — the fleet link is relayed, not direct

dobby and rocky are **0.55 ms apart on 192.168.243.0/24**, but Tailscale never hole-punched: both
report `direct connection not established` and all traffic detours via DERP (~54 ms). Contributing
conditions: no UPnP/PMP/PCP port mapping, and dobby holding **two NICs on one subnet** (`.183` and
`.204`) with the portmap monitor logging the gateway/self IP flapping between them.

This is a **multiplier, not a cause** — 1,696 round trips and a blocked event loop are bugs at
0.5 ms too — but it converts each into an outage. Note the LAN is not a drop-in substitute: the
daemon binds only to the tailscale interface and its cert carries only
`DNS:rocky.tail0dd570.ts.net` and `IP:100.69.70.109`.

## Decisions

**D1 — The runner owns sessions only.** Target state: the session daemon owns PTYs and session
lifecycle; the ten non-session loops move to dobby. This is the documented master/runner model, it
answers #734 directly, and it removes rocky's need to reach Postgres CI tables, `gh`, and dobby's
filesystem. Reached in two phases — non-blocking first (P1), relocation second (P5) — because the
migration is large and the app is unusable now.

**D2 (AMENDED 2026-08-22) — Bound the transcript glob; do NOT narrow it to one project directory.**

*Original decision, now withdrawn:* narrow the glob to `main.parent`, collapsing ~1,696 round trips
to 1, on the grounds that zero `subagents/` directories exist in any account today.

*Why it was wrong:* `_subagent_events`' own docstring states that the main transcript and the
`subagents/` directory **can live under different encoded-cwd directories** (main checkout vs.
worktree), which is precisely why it globs across all of them and dedupes by `toolUseId`. ccswitch
is worktree-heavy — one live session's transcript sits under
`-home-tony-GitHub--ccswitch-worktrees-ccswitch-521e7ca0` — so the cross-cwd case is real for this
project. Narrowing would pass today only because the feature is unexercised (zero `subagents/`
dirs), then silently drop sub-agent rendering the first time a sub-agent runs in a worktree. The
original spec flagged this decision as needing sanity-checking by someone who knew the layout; the
code is that authority and it refutes the decision.

*Amended decision:* keep cross-cwd lookup, remove the unbounded cost.
1. **Fast path** — probe `main.parent / sid / "subagents"` directly (one filesystem op). This is the
   overwhelmingly common case: the sub-agent ran in the same cwd as its parent.
2. **Bounded cross-cwd scan** — only when the fast path misses. Iterate `os.scandir(projects_root)`
   under a hard wall-clock budget instead of materialising `sorted(glob(...))` over every project.
3. **Never hang** — on budget exhaustion, return what was found, log once, and mark the result
   partial. Sub-agent reconstruction is an enhancement to the Task card; opening a session is not
   optional. Degrading it beats a 30s 503.
4. **Memoize** the resolved directories per `(projects_root, sid)` so re-opening a session does not
   re-pay the scan.

The standing rule survives unchanged: a transcript read must never perform unbounded traversal, and
must degrade on timeout rather than hang. Note this bounds the symptom; the architectural fix is
P5 (session data should not be read across an sshfs mount from the runner at all).

**D3 — Fix causes, then sweep for siblings.** RC1, RC4 and RC5 are each a *pattern* (sync I/O on the
loop; parallel "now" implementations; unguarded pools) with more than one instance already
confirmed. Each work item ends with an audit for siblings, per `capturing-fix-patterns`.

## Plan of record

Sequenced by blast radius, then structural. Each phase makes the next measurable.

### P0 · Diagnostic recovery *(no code)*

Capture why each cap-holding session went stale — **done**, and it produced RC3 and RC4.

**The cap release is deferred until after P1 and P2.** Releasing it now would start 7 concurrent
sessions on a saturated event loop, against accounts at 91–100% weekly quota, with no way to
observe them because sessions cannot be opened. Manual archive is throwaway recovery; the reaper in
P4 is the fix.

### P1 · Stop the event-loop starvation *(highest leverage)*

Move `pipeline/history.py` off the event loop. Sweep for other sync-I/O-on-loop call sites rather
than patching one. Add a **loop-lag watchdog** — today nothing reports that the daemon's loop is
fully saturated, which is why this went unnoticed.

### P2 · Make sessions openable

Apply D2. Add a bounded timeout so a slow mount degrades to "no subagent data" instead of a 30s
503. Regression-test the **traversal cost**, not just the return value.

### P3 · Edge resilience *(audit finding + #734, one defect class)*

Explicit `httpx.Limits` and separate connect/read/pool timeouts on `DaemonClient`. A
`statement_timeout` on DB connections. `/healthz` on a path that cannot be starved by the shared
pool. Then sweep for other unguarded pools.

### P4 · Queue liveness

Finished sessions must stop holding capacity slots. Resolve the `manual-unarchive` permanent
opt-out (RC3.1) — an opt-out should not be a capacity leak. Add the orphan reconciler
(one `queued` row exists with no `session_queue` entry: `9cacc747`, stranded since 01:03; it also
surfaces as a duplicate queue position in the UI). Establish a single source of truth for "now"
(RC4) and migrate the columns. **Make failure loud**: a queue that cannot drain must say why in the
UI, and N consecutive autopilot failures must alert rather than `log.warning`.

### P5 · Structural — relocate non-session work to dobby

Move the ten loops per D1.

### P6 · Transport and hygiene

Direct tailnet path (dual-NIC, port mapping). Fleet host stamping (`host` is null on every session
despite #721). `gh` credentials on rocky (archive-audit's GraphQL calls all fail). #736.

## Testing strategy

- **Cost, not just correctness.** RC2 shipped because a test asserting the *result* passes while
  performing 1,696 network round trips. P1 and P2 need assertions on event-loop lag and filesystem
  operation counts.
- **Fault injection.** P3 needs a test where the daemon is deliberately slow/dead and the edge must
  degrade rather than wedge — the condition both 2026-08-22 incidents shared.
- **Postgres suite.** RC4's column migration touches shared tables; the Postgres suite is skipped
  unless `CCSWITCH_TEST_PG_URL` is set, so it must be run explicitly or it is green locally and red
  in CI.
- **Live verification.** The audit's Playwright sweep is the acceptance check: open a session, see a
  terminal, watch the queue drain.

## Explicitly out of scope

- Fixing the DERP relay first. It is real and it amplifies everything, but fixing transport first
  would mask RC1 and RC2 rather than resolve them.
- Retention/rollup for the 15M-row `ci_test_result` table. Tracked in #734; P5 removes rocky's
  dependency on it, which is this design's concern.
- Account quota management. RC3.2 is aggravated by exhausted accounts, but quota policy is a
  separate product decision.

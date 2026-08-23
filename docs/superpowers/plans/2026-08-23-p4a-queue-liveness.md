# P4a — Queue Liveness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the session queue drain again, and make it say why when it cannot.

**Architecture:** The capacity gate counts *finished* sessions as active, so seven finished-but-unarchived sessions occupy a cap of seven and 15 queued jobs never start. Freeing a slot requires auto-archive, which six of the seven are permanently exempt from. We fix the root: finished work stops consuming runtime capacity, which defuses the opt-out leak without overriding the user's intent. Then we reconcile a stranded queue row, and replace silent failure with a visible reason.

**Tech Stack:** Python 3.12, SQLAlchemy, FastAPI, pytest / pytest-asyncio.

**Spec:** `docs/superpowers/specs/2026-08-22-fleet-recovery-design.md` — root cause **RC3**.

## Global Constraints

- Tests: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 pytest <paths>` from the repo root.
- **Run everything in the foreground with an explicit `timeout N`.** Do not background any command.
  Three implementers in the previous plans stalled by backgrounding a test run and waiting for a
  notification that never reaches a subagent.
- **Do not run the full `server/tests/` suite** (~12 min); the controller runs it. Run the scoped
  sets each task names.
- This plan touches only `server/*`, so the `src/ccswitch` editable-install caveat in `CLAUDE.md`
  does not apply.
- No new model, so no Alembic revision and no Postgres-suite requirement. If a task turns out to
  need a column, stop and report — that changes the plan.
- Every commit message ends with `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`.

## Measured starting state (2026-08-23, live fleet)

| | |
|---|---|
| `maxActiveSessions` | 7 |
| cap-occupying (non-queued) sessions | **7** — 3 `idle`, 3 `complete`, 1 `done` |
| of those, permanently exempt from auto-archive | **6** (`auto_archive_opt_out_reason='manual-unarchive'`) |
| queued, unable to start | **15** |
| sessions actually mid-turn (`/info` activeSessions) | **0** |
| autopilot | still failing every ~9 min post-deploy |

## The two-counter problem this plan resolves

There are two counters answering "how many sessions are active?", with **opposite polarity**, and
they disagree:

```python
# orchestrator.py:4655 — allow-list. Feeds /info activeSessions. Reports 0.
_ACTIVE_STATUSES = frozenset({"in_progress", "working", "waiting_input", "condensing"})
def active_session_count(self): return sum(1 for r in ... if r.status in _ACTIVE_STATUSES)

# orchestrator.py:870 — deny-list. Gates the queue. Reports 7.
NON_ACTIVE_SESSION_STATUSES = {"ended", "archived", "queued"}
def _active_session_count(self): return sum(1 for r in ... if r.status not in NON_ACTIVE_SESSION_STATUSES)
```

`server/db/models.py:36-38` claims *"EVERY cap/slot count derives from this set; no inline status
math elsewhere"* — a single-source-of-truth claim that `_ACTIVE_STATUSES` already contradicts. This
is the repo's own `eliminating-parallel-implementations` anti-pattern, and the capacity gate is
using the looser of the two definitions.

---

### Task 1: Verify whether finished sessions hold a live driver

**This task is a spike. It writes no production code.** Task 2's correctness depends on its answer,
so it runs first and its finding is recorded.

The premise of Task 2 is that a `complete`/`done` session has finished its work and should not
consume runtime capacity. If those sessions still hold a live driver (a running `claude` process),
then freeing their capacity slot would *oversubscribe the host* — more live processes than the cap
intends — and the correct fix would be driver teardown instead.

**Files:**
- Create: `docs/superpowers/spikes/2026-08-23-finished-session-drivers.md`
- Modify: none

**Interfaces:**
- Consumes: nothing.
- Produces: a recorded finding — for each of `complete`, `done`, `idle`: does a session in that
  status retain an entry in `orchestrator._drivers`, and does a `claude` process survive? Task 2
  reads this before changing the capacity set.

- [ ] **Step 1: Read how drivers are torn down**

Read `server/sessions/orchestrator.py` around lines 788, 1237, and 3934 (`self._drivers.pop(...)`
sites and where `status = "done"` is set). Determine from the code whether reaching `complete` or
`done` pops the driver.

- [ ] **Step 2: Ask the live daemon**

The daemon exposes RPCs over mTLS. Correlate its live drivers against session status:

```bash
C="--cert /etc/ccswitch/tls/client.pem --key /etc/ccswitch/tls/client.key --cacert /etc/ccswitch/tls/ca.pem"
curl -s --max-time 20 $C https://rocky.tail0dd570.ts.net:9443/info
curl -s --max-time 30 $C -X POST -H 'content-type: application/json' -d '{}' \
  https://rocky.tail0dd570.ts.net:9443/rpc/live_session_counts
```

`live_session_counts` is at `orchestrator.py:4820`. Compare its output against the status counts
from the API (`/api/sessions`). If it reports zero live sessions while 7 sessions occupy the cap,
that is direct evidence the cap is counting records with no runtime cost.

- [ ] **Step 3: Check for surviving processes on the host**

```bash
ssh tony@rocky 'pgrep -af "claude" | grep -v grep | head -20'
```

Be careful reading this: many paths contain the string `claude` (e.g. `/home/tony/.claude/...`), so
match on the actual session-launch command shape, not the bare substring. Report how many are
genuine session processes versus incidental matches.

- [ ] **Step 4: Write the finding**

Create `docs/superpowers/spikes/2026-08-23-finished-session-drivers.md` stating, for each of
`complete` / `done` / `idle`: driver retained yes/no, process alive yes/no, and the evidence.

End with an explicit recommendation:
- **If finished sessions hold no driver** → Task 2 proceeds as written (exclude them from capacity).
- **If they do hold a driver** → **stop and report to the controller.** Task 2's premise is wrong
  and the plan needs a teardown task instead. Do not improvise a different fix.

- [ ] **Step 5: Commit**

```bash
git add docs/superpowers/spikes/2026-08-23-finished-session-drivers.md
git commit -m "docs: spike — do finished sessions still hold a live driver?

Task 2 frees the capacity slot held by complete/done sessions. That is only
safe if they hold no live driver; otherwise it oversubscribes the host and
the right fix is teardown. Records the evidence either way.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 2: Release the driver when a session completes

Task 1's spike (`docs/superpowers/spikes/2026-08-23-finished-session-drivers.md`, commit `d67f9797`)
proved the premise of the original plan wrong: `complete`/`done`/`idle` sessions **never** have their
driver popped. Only `close_session`, `archive_session`, and daemon `stop()` do. So a finished session
genuinely holds a live `claude` process, and the capacity gate counting it is *correct*.

The real defect is that **auto-archive is the only path that releases those resources**, and it has
two independent failure modes (autopilot broken; six sessions permanently opted out). This task
removes that dependency: completion itself releases the process.

Nothing is archived. The session, its DB row and its transcript all stay. The user's
`manual-unarchive` choice keeps its exact meaning.

**Files:**
- Modify: `server/sessions/orchestrator.py` — the completion transition (see Step 1)
- Test: `server/tests/test_completion_releases_driver.py` (create)

**Interfaces:**
- Consumes: Task 1's finding.
- Produces: reaching `complete` or `done` pops the session's driver and closes it. Task 3 depends on
  this: only once the process is released is exempting those statuses from capacity correct.

- [ ] **Step 1: Find the completion transition and the teardown idiom**

Read `server/sessions/orchestrator.py` around **line 1343** (`completed_turn = status == "done"`) and
**1372/1388/1448** (`status == "complete"` handling) to locate where a session settles into
`complete`/`done`. Then read the teardown idiom at **lines 783-791** (`close_session`/`stop()`):

```python
driver = self._drivers.pop(sid, None)
if driver is not None:
    with contextlib.suppress(Exception):
        await driver.aclose()
```

Reuse that idiom — do not invent a second teardown path. If a helper already wraps it, call the
helper.

- [ ] **Step 2: Confirm reopening still works**

Read `orchestrator.py:~4890` (the lazy re-attach). It already handles "no live driver": it builds a
fresh driver resuming from `rec.claude_session_id` and recreates a missing cwd. **Confirm by reading
that this path is reachable for a `complete`/`done` session** — note it explicitly refuses only
`queued` sessions.

If it turns out `complete`/`done` are *also* refused there, stop and report: releasing the driver
would make those sessions unopenable, which is worse than the bug we are fixing.

- [ ] **Step 3: Write the failing test**

Create `server/tests/test_completion_releases_driver.py`. Cover:

1. a session transitioning to `complete` has its driver popped and `aclose()` awaited;
2. same for `done`;
3. a session going `idle` **keeps** its driver — idle is alive and awaiting input;
4. teardown failure does not break the transition (the `contextlib.suppress` behaviour) — a driver
   whose `aclose()` raises must still be removed from `_drivers`.

Build the orchestrator as `SessionOrchestrator(sdk_factory=mock.MagicMock())` and inject a fake
driver exposing an awaitable `aclose()`; that is the pattern the existing orchestrator tests use.
Read `server/tests/test_orchestrator_drain.py` for the closest existing example.

- [ ] **Step 4: Run test to verify it fails**

Run: `timeout 300 env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 .venv/bin/python -m pytest server/tests/test_completion_releases_driver.py -v`

Expected: FAIL — the driver is still in `_drivers` after the transition.

- [ ] **Step 5: Implement**

Pop and close the driver on the `complete`/`done` transition, using the Step 1 idiom. Keep it
**deterministic** — no model call, no dependency on autopilot. That is the point: capacity must be
freed by completion itself so a broken autopilot can never deadlock the queue again.

Do not touch the `idle` path.

- [ ] **Step 6: Run test to verify it passes, and check the neighbours**

```bash
timeout 600 env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 .venv/bin/python -m pytest \
  server/tests/test_completion_releases_driver.py \
  server/tests/test_orchestrator_drain.py \
  server/tests/test_autopilot_opt_out.py \
  server/tests/test_adopt_session.py -v 2>&1 | tail -25
```

- [ ] **Step 7: Commit**

```bash
git add server/sessions/orchestrator.py server/tests/test_completion_releases_driver.py
git commit -m "fix(sessions): release the driver when a session completes

A finished session kept its claude process alive until something archived it,
and auto-archive was the ONLY path that freed capacity — with two independent
failure modes (autopilot failing every ~9 min; six sessions permanently opted
out via manual-unarchive). The cap filled and nothing could reclaim it.

Completion now releases the process itself: deterministic, no model call, no
autopilot dependency. The session, its row and its transcript all stay, and
reopening takes the existing lazy re-attach path that resumes from
claude_session_id. Nothing is archived behind the user's back.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 3: Stop finished sessions from consuming capacity

**Do not start until Task 2 has landed.** Task 1's spike proved finished sessions DO hold a live
driver, so this exemption is only safe once Task 2 tears that driver down. Doing it first would
oversubscribe the host.

This is the change that unblocks the queue. It also defuses the `manual-unarchive` leak without
overriding user intent: the opt-out keeps meaning "do not auto-archive me", and stops meaning
"remove a capacity slot from the fleet forever", because capacity no longer depends on archiving.

**Files:**
- Modify: `server/db/models.py:34-39` (the status-set definitions and their comment)
- Test: `server/tests/test_queue_capacity.py` (create)

**Interfaces:**
- Consumes: Task 2 — drivers are released on completion, which is what makes this exemption safe.
- Produces: a new frozenset `CAPACITY_EXEMPT_STATUSES` exported from `server/db/models.py`,
  used by `orchestrator._active_session_count()`. `NON_ACTIVE_SESSION_STATUSES` keeps its current
  meaning and members for every other consumer — do not repurpose it.

- [ ] **Step 1: Write the failing test**

Create `server/tests/test_queue_capacity.py`:

```python
"""Finished sessions must not consume runtime capacity (RC3).

Seven finished-but-unarchived sessions occupied a cap of seven on the live
fleet while zero sessions were mid-turn, so 15 queued jobs could never start.
Six of the seven were permanently exempt from auto-archive, so the deadlock
could not self-heal.
"""
from server.db.models import (
    CAPACITY_EXEMPT_STATUSES,
    INACTIVE_SESSION_STATUSES,
    NON_ACTIVE_SESSION_STATUSES,
)


def test_finished_statuses_do_not_consume_capacity():
    # Work is over: the completion marker was emitted. No runtime slot.
    assert "complete" in CAPACITY_EXEMPT_STATUSES
    assert "done" in CAPACITY_EXEMPT_STATUSES


def test_queued_and_terminal_statuses_still_exempt():
    # Everything that was already exempt stays exempt.
    for s in NON_ACTIVE_SESSION_STATUSES:
        assert s in CAPACITY_EXEMPT_STATUSES, f"{s} lost its exemption"


def test_idle_still_consumes_capacity():
    """An idle session is alive and awaiting input — it holds a real slot."""
    assert "idle" not in CAPACITY_EXEMPT_STATUSES


def test_non_active_set_is_unchanged_for_other_consumers():
    """NON_ACTIVE_SESSION_STATUSES has other callers; do not repurpose it."""
    assert NON_ACTIVE_SESSION_STATUSES == INACTIVE_SESSION_STATUSES | {"queued"}
```

Then add the scenario test that proves the deadlock is gone. Read
`server/tests/test_session_queue_model.py` first and reuse its fixtures; if it has no orchestrator
fixture, build records the way `_active_session_count` reads them (`self._records`, each with a
`.status`) rather than standing up a full orchestrator.

```python
def test_seven_finished_sessions_do_not_block_a_queue(monkeypatch):
    """The live deadlock, reproduced: 3 idle + 3 complete + 1 done vs a cap of 7."""
    from server.sessions.orchestrator import SessionOrchestrator
    from unittest import mock

    orch = SessionOrchestrator(sdk_factory=mock.MagicMock())
    orch._records = {
        f"s{i}": mock.Mock(status=st)
        for i, st in enumerate(["idle", "idle", "idle", "complete", "complete", "complete", "done"])
    }
    # Before the fix this was 7 and the drain loop broke immediately.
    assert orch._active_session_count() == 3
```

- [ ] **Step 2: Run test to verify it fails**

Run: `timeout 300 env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 .venv/bin/python -m pytest server/tests/test_queue_capacity.py -v`

Expected: FAIL — `ImportError: cannot import name 'CAPACITY_EXEMPT_STATUSES'`.

- [ ] **Step 3: Write minimal implementation**

In `server/db/models.py`, beside the existing sets:

```python
# Statuses that consume no RUNTIME capacity slot. Distinct from
# NON_ACTIVE_SESSION_STATUSES, which answers "is this session reviewable/inactive"
# and has other consumers.
#
# `complete` and `done` are here because the work is finished — the completion
# marker was emitted and no turn is running. Counting them against the cap made
# finished work starve queued work: on 2026-08-23 seven finished sessions held a
# cap of seven while ZERO sessions were mid-turn, and 15 queued jobs could never
# start. Six of the seven were permanently exempt from auto-archive
# (`manual-unarchive`), so the deadlock could not self-heal — an archive opt-out
# had silently become a permanent capacity leak.
#
# `idle` is deliberately NOT here: an idle session is alive and awaiting input.
CAPACITY_EXEMPT_STATUSES: frozenset[str] = NON_ACTIVE_SESSION_STATUSES | {"complete", "done"}
```

Update the comment on `NON_ACTIVE_SESSION_STATUSES` — its current claim that *"EVERY cap/slot count
derives from this set"* becomes false. Say instead that capacity derives from
`CAPACITY_EXEMPT_STATUSES` and that this set answers the reviewable/inactive question.

Then in `server/sessions/orchestrator.py`, change `_active_session_count` (line 870) to use the new
set, and import it alongside the existing model imports at line 44:

```python
    def _active_session_count(self) -> int:
        """Sessions consuming a runtime capacity slot (see CAPACITY_EXEMPT_STATUSES)."""
        return sum(1 for r in self._records.values()
                   if r.status not in CAPACITY_EXEMPT_STATUSES)
```

Change nothing else. In particular, leave `active_session_count()` (line 4655, the `_ACTIVE_STATUSES`
allow-list used by `/info` and the deploy drain) alone — unifying the two counters is a larger
change with its own consumers, and this task is about unblocking the queue.

- [ ] **Step 4: Run test to verify it passes**

Run: `timeout 300 env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 .venv/bin/python -m pytest server/tests/test_queue_capacity.py -v`

Expected: PASS.

- [ ] **Step 5: Verify no consumer of the old behaviour regressed**

```bash
timeout 600 env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 .venv/bin/python -m pytest \
  server/tests/test_session_queue_model.py \
  server/tests/test_orchestrator_drain.py \
  server/tests/test_schema_reconcile_coverage.py \
  server/tests/test_deploy_drain.py -v 2>&1 | tail -25
```

If a test file in that list does not exist, find the real ones with
`grep -rl "NON_ACTIVE_SESSION_STATUSES\|_active_session_count\|max_active" server/tests/` and run
those instead — name what you ran in your report.

- [ ] **Step 6: Commit**

```bash
git add server/db/models.py server/sessions/orchestrator.py server/tests/test_queue_capacity.py
git commit -m "fix(queue): finished sessions no longer consume a capacity slot

The cap counted complete/done sessions as active, so seven finished sessions
occupied a cap of seven while ZERO were mid-turn, and 15 queued jobs could
never start. Freeing a slot required auto-archive, and six of the seven were
permanently exempt from it (manual-unarchive) — so an archive opt-out had
silently become a permanent capacity leak the queue could not recover from.

Capacity now derives from CAPACITY_EXEMPT_STATUSES. The opt-out keeps its
intended meaning (do not auto-archive me) and stops meaning (remove a slot
from the fleet forever). idle is deliberately still counted: it is alive and
awaiting input.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 4: Reconcile queued rows that are not in the queue

One session sits at `status='queued'` with **no matching `session_queue` row**, so the drain loop
can never pick it up and nothing cleans it up. It has been stranded since 01:03 on 2026-08-22 and it
also surfaces in the UI as a duplicate queue position — two cards both numbered 1.

**Files:**
- Modify: `server/sessions/orchestrator.py` — the rehydrate path (the existing `queue-drain-on-rehydrate` spawn
  is at `orchestrator.py:3958`; reconcile immediately before it)
- Test: `server/tests/test_queue_orphan_reconcile.py` (create)

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `async def _reconcile_orphan_queued(self) -> int` on the orchestrator, returning the
  number of rows reconciled. Called from rehydrate before the drain.

- [ ] **Step 1: Write the failing test**

Read `server/sessions/session_queue.py` first for the real enqueue/list API — do not guess function
names. Then create `server/tests/test_queue_orphan_reconcile.py` covering:

1. a session row with `status='queued'` and **no** `session_queue` entry is re-enqueued (or marked
   `ended` — see Step 3's decision) and no longer stranded;
2. a session row with `status='queued'` **and** a matching queue entry is left untouched;
3. reconcile is idempotent — running it twice does not double-enqueue.

Build the rows with the same DB fixtures `server/tests/test_session_queue_model.py` uses.

- [ ] **Step 2: Run test to verify it fails**

Run: `timeout 300 env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 .venv/bin/python -m pytest server/tests/test_queue_orphan_reconcile.py -v`

Expected: FAIL — `AttributeError: 'SessionOrchestrator' object has no attribute '_reconcile_orphan_queued'`.

- [ ] **Step 3: Write minimal implementation**

**Decide re-enqueue vs. mark-ended, and state the reason in the docstring.** Re-enqueueing preserves
the user's intent (they asked for this session) but risks resurrecting a row whose params are stale
or gone; marking `ended` is safe and matches how `_maybe_start_queued` already handles a start
failure (`orchestrator.py:920` explains that reasoning — read it). Prefer re-enqueue **only if**
the row still carries everything `_enqueue_session` needs; otherwise mark `ended` so the row stops
being invisible garbage. Whichever you choose, log one line per reconciled row.

Call it from rehydrate immediately before the existing `queue-drain-on-rehydrate` spawn, so a
reconciled row is eligible in the same pass.

- [ ] **Step 4: Run test to verify it passes**

Run: `timeout 300 env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 .venv/bin/python -m pytest server/tests/test_queue_orphan_reconcile.py -v`

Expected: PASS, all three cases.

- [ ] **Step 5: Run the queue suites**

```bash
timeout 600 env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 .venv/bin/python -m pytest \
  server/tests/test_queue_orphan_reconcile.py server/tests/test_queue_capacity.py \
  server/tests/test_session_queue_model.py -v 2>&1 | tail -20
```

- [ ] **Step 6: Commit**

```bash
git add server/sessions/orchestrator.py server/tests/test_queue_orphan_reconcile.py
git commit -m "fix(queue): reconcile queued rows with no session_queue entry

One session has sat at status='queued' with no queue row since 2026-08-22
01:03. The drain loop can never pick it up and nothing reaps it, so it is
invisible garbage that also renders as a duplicate queue position in the UI.
Reconcile on rehydrate, before the existing drain.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 5: Make a stuck queue say why

The deadlock was silent. The UI showed 15 queued sessions and no explanation; it took a database
query and a source read to find the cause. Autopilot has been failing every ~9 minutes for over a
day and nothing surfaced that either.

**Files:**
- Modify: `server/api/sessions.py` — the queue/session-list response
- Modify: `server/automations/autopilot_decider.py:110` — consecutive-failure counting
- Test: `server/tests/test_queue_stall_reason.py` (create)

**Interfaces:**
- Consumes: `CAPACITY_EXEMPT_STATUSES` from Task 2.
- Produces: the sessions API exposes a queue-stall reason when the queue is non-empty and nothing
  can start. Shape is yours to choose, but it must be **additive** — no existing field changes type
  or disappears, or the frontend breaks.

- [ ] **Step 1: Write the failing test**

Create `server/tests/test_queue_stall_reason.py` asserting:

1. queue non-empty **and** capacity full → the response carries a reason naming the cause (capacity)
   and the numbers (occupying vs cap);
2. queue non-empty and capacity available → **no** stall reason (nothing is wrong);
3. empty queue → no stall reason;
4. the addition is backward-compatible: every field the endpoint returned before is still present
   with the same type.

Read the endpoint first and match its existing response conventions.

- [ ] **Step 2: Run test to verify it fails**

Run: `timeout 300 env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 .venv/bin/python -m pytest server/tests/test_queue_stall_reason.py -v`

Expected: FAIL — the field does not exist.

- [ ] **Step 3: Implement the stall reason**

Compute it from the same source of truth Task 2 established — do not re-derive status math inline
(that is the anti-pattern this plan exists to remove). Keep it cheap: this is on the session-list
path, which is already the slowest route in the app.

- [ ] **Step 4: Implement autopilot failure counting**

In `server/automations/autopilot_decider.py`, the failure at line 110 currently does
`log.warning(...)` and returns `escalate`. Track consecutive failures and escalate the **log level**
past a threshold (start at 5 — roughly 45 minutes at the observed ~9-minute cadence), so a
persistent outage is visibly different from a transient blip. Reset the counter on any success.

Do **not** add a new alerting transport, and do not go looking for a Python helper — I checked,
there is none. `CCSWITCH_ALERT_WEBHOOK` is set in the deployed environment but its only consumer in
the entire repo is a **shell script**, `scripts/ccswitch-health-check.sh:54`, which posts to it out
of band. The Python app has no alerting path at all.

So: emit a distinct high-severity log line and stop there. Wiring Python to that webhook is a
separate decision about where alerting belongs, not something to bolt on inside this task.

- [ ] **Step 5: Run test to verify it passes, plus the autopilot suites**

```bash
timeout 600 env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 .venv/bin/python -m pytest \
  server/tests/test_queue_stall_reason.py server/tests/test_autopilot_decider.py -v 2>&1 | tail -20
```

If `test_autopilot_decider.py` does not exist, find the autopilot tests with
`ls server/tests/ | grep -i autopilot` and run those; name what you ran.

- [ ] **Step 6: Commit**

```bash
git add server/api/sessions.py server/automations/autopilot_decider.py server/tests/test_queue_stall_reason.py
git commit -m "feat(queue): surface why a queue is not draining, and flag persistent autopilot failure

The 2026-08-23 deadlock was silent: 15 queued sessions, no explanation in the
UI, and autopilot failing every ~9 minutes for over a day with nothing louder
than a log.warning. A queue that cannot drain should say so.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

## Verification against the live fleet

- [ ] Deploy and confirm the daemon starts cleanly.
- [ ] **The queue drains.** Baseline to beat: 7 cap-occupying / cap 7 / 15 queued / 0 mid-turn.
  ```bash
  curl -s --max-time 40 http://127.0.0.1:8787/api/sessions | python3 -c "
  import json,sys; from collections import Counter
  d=json.load(sys.stdin); c=Counter(s['status'] for s in d)
  print(c); print('cap-occupying:', sum(v for k,v in c.items() if k not in ('queued','complete','done','ended','archived')))"
  ```
  Expected: cap-occupying drops to 4 — the 3 `idle` sessions plus the 1 `done` one (see the
  amendment below; `done` still counts) — and queued sessions begin starting. Drop `'done'` from
  the exclusion tuple in the snippet above when re-running it.
- [ ] Confirm the orphan is gone:
  ```sql
  select count(*) from sessions s where s.status='queued'
    and s.id not in (select session_id from session_queue);
  ```
  Expected: 0.
- [ ] **Watch account quota.** `tony_kindash.com` was at 100% weekly and `tony_steinonline.net` at
  91%. Draining 15 queued jobs at once against exhausted accounts may fail them immediately. If the
  queue starts jobs that fail on quota, that is a real finding — report it rather than treating the
  drain as successful.

**Note:** P4a does not fix autopilot's underlying failure (RC3.2) — it only makes the failure
visible. The `Claude Code returned an error result: success` root cause is still unproven, with
account quota exhaustion the leading hypothesis. That remains its own investigation.


---

## Amendment (2026-08-23, final review) — `done` was the wrong signal

The plan above treats `complete` and `done` as one "finished work" state. They are not.
`derive_status` (`server/sessions/status.py`) returns `done` for **any** ended turn
(`turns_completed >= 1` with no turn active) — i.e. after every ordinary prompt in every session.
`_persist_status` says so itself: `completed_turn = status == "done"`.

As shipped in the plan, that meant the `claude` process was released at the end of **every turn in
every session**, and a live between-turns session was exempt from the capacity cap. What actually
ships:

- The driver release in `_persist_status` fires on `complete` only.
- `CAPACITY_EXEMPT_STATUSES` = `INACTIVE_SESSION_STATUSES | {"queued", "complete"}` — `done`
  counts against the cap, like `idle`. `NON_ACTIVE_SESSION_STATUSES` had no production consumer
  and was deleted; the exempt set is derived directly.
- The live-fleet arithmetic becomes 7 → **4** cap-occupying (3 `idle` + 1 `done`) against a cap of
  7, which still unblocks the queue.
- Invariant added: a session that acquires a live driver must not stay capacity-exempt, so
  `ensure_driver` transitions a resumed `complete` session back to `done`.
- The completion nudge in `_auto_archive_completed` now calls `ensure_driver` before `send_prompt`
  (the release had already popped the driver, so the nudge was silently discarded), and the WS
  bridge clears its `live_tasks` entry when a forwarding task ends so the next prompt on an open
  socket resumes instead of vanishing.

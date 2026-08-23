# P1 — Stop the Event-Loop Starvation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop rocky's session daemon from blocking its asyncio event loop on synchronous SQLite reads, so every other task on the daemon can be scheduled again.

**Architecture:** `HistoryStore` is a synchronous SQLite reader called directly from the event loop by the `pr_population` background loop, once per check per PR per repo. We make the store safe to use from a worker thread (connection + lock), then move the whole ETA/check-group enrichment into a single `asyncio.to_thread` hop. Finally we add a loop-lag watchdog so this class of regression is observable instead of silent.

**Tech Stack:** Python 3.12, asyncio, stdlib `sqlite3`, `threading.Lock`, pytest / pytest-asyncio.

**Spec:** `docs/superpowers/specs/2026-08-22-fleet-recovery-design.md` (root cause RC1)

## Global Constraints

- Tests: `pytest server/tests/` from the repo root. This plan touches only `server/*`, so the
  `src/ccswitch` editable-install caveat in `CLAUDE.md` does not apply.
- Run the suite with `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 pytest server/tests/`
  — the live shell env sets a real allowlist that makes API-auth tests 401.
- No schema changes in this plan, so no Alembic revision and no Postgres-suite requirement.
- `HistoryStore` opens the DB **read-only** (`mode=ro`). Keep it read-only.
- Do not change any public method signature on `HistoryStore` — 11 methods and ~15 existing tests
  depend on the current surface.
- Every commit message ends with `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`.

---

### Task 1: Make `HistoryStore` safe to use from a worker thread

`sqlite3.connect()` defaults to `check_same_thread=True`, so a connection created on the event-loop
thread raises `ProgrammingError` the moment a worker thread touches it. This is the exact reason
`pr_population.py:390` says the work "run[s] on the event-loop thread (store is not thread-safe)".
Removing that constraint is the prerequisite for Task 2.

**Files:**
- Modify: `server/pipeline/history.py:121-124` (the `__init__`) and every method that touches `self._conn`
- Test: `server/tests/test_pipeline_history_threadsafe.py` (create)

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `HistoryStore` becomes callable from any thread. Public method signatures are unchanged:
  `recent_durations(repo: str, name: str, event: str) -> list[float]`,
  `expected(...) -> Expected | None`, `samples(...) -> list[float]`,
  `expected_set(repo, event, now, window_days=14) -> list[str]`,
  `slowest_checks(...)`, `flakiest_checks(...)`, `duration_regressions(repo) -> list[CheckRegressionStat]`,
  `runner_wait_health(...)`, `expected_runner_wait(repo, name, event) -> float | None`,
  `ci_graph(repo) -> dict | None`. Task 2 relies on all of these being thread-safe.

- [ ] **Step 1: Write the failing test**

Create `server/tests/test_pipeline_history_threadsafe.py`:

```python
"""HistoryStore must be usable from a worker thread.

pr_population moves ETA enrichment onto a thread (see P1 Task 2); a store that
raises ProgrammingError off the main thread would make that impossible.
"""
import sqlite3
from concurrent.futures import ThreadPoolExecutor

from server.pipeline.history import HistoryStore


def _store(tmp_path) -> HistoryStore:
    p = tmp_path / "ph.db"
    c = sqlite3.connect(str(p))
    c.executescript(
        """
        CREATE TABLE check_durations (repo TEXT, check_name TEXT, event TEXT,
          duration_secs REAL, completed_at TEXT, conclusion TEXT, head_sha TEXT,
          run_attempt INTEGER, started_at TEXT, run_number INTEGER);
        CREATE TABLE runner_waits (repo TEXT, check_name TEXT, event TEXT,
          wait_secs REAL, started_at TEXT, pool TEXT);
        """
    )
    for i, dur in enumerate([100, 110, 120, 130, 140, 150]):
        c.execute(
            "INSERT INTO check_durations VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("o/n", "ci", "pull_request", dur, f"2026-06-2{i}T00:00:00Z",
             "SUCCESS", "sha", 1, f"2026-06-2{i}T00:00:00Z", i),
        )
    c.commit()
    c.close()
    return HistoryStore(str(p))


def test_recent_durations_works_from_a_worker_thread(tmp_path):
    store = _store(tmp_path)
    expected = store.recent_durations("o/n", "ci", "pull_request")
    assert expected, "fixture should produce at least one sample"

    with ThreadPoolExecutor(max_workers=1) as ex:
        got = ex.submit(store.recent_durations, "o/n", "ci", "pull_request").result()

    assert got == expected


def test_concurrent_readers_do_not_interleave(tmp_path):
    """Two threads hammering the same connection must not raise or return garbage."""
    store = _store(tmp_path)
    expected = store.recent_durations("o/n", "ci", "pull_request")

    with ThreadPoolExecutor(max_workers=4) as ex:
        futures = [
            ex.submit(store.recent_durations, "o/n", "ci", "pull_request")
            for _ in range(40)
        ]
        results = [f.result() for f in futures]

    assert all(r == expected for r in results)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 pytest server/tests/test_pipeline_history_threadsafe.py -v`

Expected: FAIL. `test_recent_durations_works_from_a_worker_thread` raises
`sqlite3.ProgrammingError: SQLite objects created in a thread can only be used in that same thread`.

- [ ] **Step 3: Write minimal implementation**

In `server/pipeline/history.py`, add `import threading` beside the existing `import sqlite3`, then
replace the constructor (currently lines 121-124):

```python
class HistoryStore:
    def __init__(self, db_path: str) -> None:
        # check_same_thread=False + an explicit lock: the store is read from a
        # worker thread by pr_population's ETA enrichment so the event loop is
        # not blocked (RC1). sqlite3 connections are not safe for *concurrent*
        # use, so every query serialises on _lock.
        self._conn = sqlite3.connect(
            f"file:{db_path}?mode=ro", uri=True, check_same_thread=False
        )
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
```

Then guard every `self._conn.execute(...)` call. There are 11 public methods; each one wraps its
query. `recent_durations` becomes:

```python
    def recent_durations(self, repo: str, name: str, event: str) -> list[float]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT duration_secs, completed_at FROM check_durations "
                "WHERE repo=? AND check_name=? AND event=? AND conclusion='SUCCESS' "
                "ORDER BY completed_at DESC LIMIT 20",
                (repo, name, event),
            ).fetchall()
        if not rows:
            return []
        cutoff = _parse_iso_ms(rows[0]["completed_at"]) - DURATION_FRESH_DAYS * 86400_000
        fresh = [r for r in rows if _parse_iso_ms(r["completed_at"]) >= cutoff]
        chosen = fresh if len(fresh) >= DURATION_FRESH_MIN else rows[:DURATION_FALLBACK_LIMIT]
        return [float(r["duration_secs"]) for r in chosen]
```

Apply the same `with self._lock:` wrapper around the `self._conn.execute(...).fetchall()` (or
`.fetchone()`) call in each of: `expected_set`, `slowest_checks`, `flakiest_checks`,
`duration_regressions`, `runner_wait_health`, `expected_runner_wait`, `ci_graph`. Hold the lock only
for the query and row fetch — never around the pure-Python math that follows, or the lock becomes a
second bottleneck. `expected` and `samples` need no change: they delegate to `recent_durations`,
which is already guarded, and re-locking would deadlock a non-reentrant `Lock`.

- [ ] **Step 4: Run test to verify it passes**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 pytest server/tests/test_pipeline_history_threadsafe.py -v`

Expected: PASS, both tests.

- [ ] **Step 5: Verify no existing history test regressed**

Run:
```bash
env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 pytest \
  server/tests/test_pipeline_history_read.py \
  server/tests/test_pipeline_history_migrate.py \
  server/tests/test_history_check_insights.py \
  server/tests/test_history_runner_waits.py \
  server/tests/test_pipeline_eta.py \
  server/tests/test_pipeline_estimator.py -v
```

Expected: PASS. If any test deadlocks (hangs rather than fails), a method acquired `_lock` twice —
find the nested acquisition and remove the inner one.

- [ ] **Step 6: Commit**

```bash
git add server/pipeline/history.py server/tests/test_pipeline_history_threadsafe.py
git commit -m "fix(pipeline): make HistoryStore usable from a worker thread

sqlite3 defaults to check_same_thread=True, so the store could only be read
from the event-loop thread — which is why pr_population computes ETAs inline
and starves the loop (RC1). Open with check_same_thread=False and serialise
every query on a lock so the enrichment can move to a thread.

Refs #734

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 2: Move ETA enrichment off the event loop

With the store thread-safe, the per-PR enrichment loop moves to a worker thread in one hop. This is
the change that actually fixes RC1: a `py-spy` dump caught the event loop inside
`server/pipeline/history.py` in 8 of 8 samples over 24 seconds.

**Files:**
- Modify: `server/github/pr_population.py:321-...` (`_enrich_with_eta`), including the now-false
  comment at line 390
- Test: `server/tests/test_population_eta_offloads_loop.py` (create)

**Interfaces:**
- Consumes: thread-safe `HistoryStore` from Task 1.
- Produces: `_enrich_with_eta` keeps its existing `async def` signature and return type
  (`dict[str, tuple[PullRequestInfo, ...]]`) — callers are unchanged. Only where the work runs changes.

- [ ] **Step 1: Write the failing test**

This asserts *cost*, not just correctness — per the spec, a test that only checks the return value
is how RC1 shipped. A ticker coroutine runs concurrently and must keep ticking while the blocking
half runs.

**Read `server/tests/test_population_eta_wired.py` first.** `_enrich_with_eta(self, prs_by_repo)`
takes only `prs_by_repo`; `checks_by_pr` and `merge_by_pr` are derived inside it from
`await self._fetch_eta(open_keys)`. Reuse that file's existing poller-construction and `_fetch_eta`
stubbing helpers rather than inventing new ones — import them if they are importable.

Create `server/tests/test_population_eta_offloads_loop.py`:

```python
"""ETA enrichment must not block the event loop (RC1).

The blocking half lives in the module-level _enrich_sync. We replace it with one
that blocks for a known duration: run inline it freezes every other task; run via
asyncio.to_thread the loop keeps ticking. Asserting scheduling latency — not the
return value — is the point, because a result-only test is what let RC1 ship.
"""
import asyncio
import time

import pytest

from server.github import pr_population


async def _ticker(stop: asyncio.Event, ticks: list[float]) -> None:
    """Records a timestamp every 10ms until told to stop."""
    while not stop.is_set():
        ticks.append(time.monotonic())
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_enrichment_keeps_the_event_loop_responsive(monkeypatch):
    BLOCK_S = 0.4

    def _blocking_enrich(*args, **kwargs):
        time.sleep(BLOCK_S)          # stands in for many blocking sqlite3 reads
        return {}

    monkeypatch.setattr(pr_population, "_enrich_sync", _blocking_enrich)

    # Build the poller and stub the network fetch. Mirror the construction and
    # _fetch_eta stub used by test_population_eta_wired.py.
    poller = _make_poller_with_stubbed_fetch(monkeypatch)
    prs_by_repo = _fake_prs_by_repo(repo="o/n", count=3)

    stop = asyncio.Event()
    ticks: list[float] = []
    ticker = asyncio.create_task(_ticker(stop, ticks))

    started = time.monotonic()
    await poller._enrich_with_eta(prs_by_repo)
    elapsed = time.monotonic() - started

    stop.set()
    await ticker

    assert elapsed >= BLOCK_S, "the stub must actually block, else the test proves nothing"
    # Offloaded: ~elapsed/0.01 ticks. Run inline: the ticker is frozen, ~1 tick.
    assert len(ticks) > (elapsed / 0.02), (
        f"event loop was starved: only {len(ticks)} ticks in {elapsed:.2f}s"
    )
```

Write `_make_poller_with_stubbed_fetch` and `_fake_prs_by_repo` in the same file, modelled on
`test_population_eta_wired.py`. `_fake_prs_by_repo` must produce objects with the attributes the
enrichment path reads: `pr.state` (one of `"open"`/`"draft"`) and `pr.number`. The `_fetch_eta` stub
must return whatever shape `parse_eta_checks_response` accepts — copy it from the wired test rather
than deriving it.

- [ ] **Step 2: Run test to verify it fails**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 pytest server/tests/test_population_eta_offloads_loop.py -v`

Expected: FAIL with `event loop was starved: only 1 ticks in N.NNs` (or similar low count).

- [ ] **Step 3: Write minimal implementation**

In `server/github/pr_population.py`, extract the synchronous body of the enrichment loop into a
module-level helper, then await it via `asyncio.to_thread`. Replace the comment at line 390 —
it is now false.

```python
def _enrich_sync(prs_by_repo, checks_by_pr, merge_by_pr, store, compute_pr_eta,
                 now, now_iso, eta_cache) -> dict:
    """The blocking half of ETA enrichment: SQLite reads + pure compute.

    Runs on a worker thread (RC1) — it must not touch the event loop, awaitables,
    or any asyncio primitive.
    """
    result: dict = {}
    for repo, prs in prs_by_repo.items():
        enriched = []
        for pr in prs:
            # ... existing per-PR body, verbatim, with self._eta_cache -> eta_cache ...
            enriched.append(pr)
        result[repo] = tuple(enriched)
    return result
```

Then in `_enrich_with_eta`, replace the inline loop with:

```python
        # SQLite reads + compute are blocking; run them on a worker thread so the
        # daemon's event loop keeps scheduling (RC1). HistoryStore is thread-safe
        # as of P1 Task 1.
        result = await asyncio.to_thread(
            _enrich_sync, prs_by_repo, checks_by_pr, merge_by_pr, store,
            compute_pr_eta, now, now_iso, self._eta_cache,
        )
```

Move the per-PR body verbatim — do not rewrite its logic. `self._eta_cache` is passed in and mutated
in place, so cache behaviour is unchanged. `asyncio` is already imported (line 10).

Two stale comments now describe the opposite of what the code does. Both must be corrected in this
commit, or the next reader will re-introduce RC1:

1. The inline comment at line ~390:
   `# SQLite reads + compute run on the event-loop thread (store is not thread-safe).`
2. The `_enrich_with_eta` docstring, which claims:
   `The SQLite HistoryStore is used on the event-loop thread; only the network call is offloaded to a thread.`

Replace both with the new invariant: the network fetch and `_record_group_history` stay on the loop
(they are genuinely async); the SQLite reads and pure compute run on a worker thread.

- [ ] **Step 4: Run test to verify it passes**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 pytest server/tests/test_population_eta_offloads_loop.py -v`

Expected: PASS.

- [ ] **Step 5: Verify the whole population suite still passes**

Run:
```bash
env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 pytest \
  server/tests/test_pr_population.py \
  server/tests/test_population_eta_wired.py \
  server/tests/test_population_eta_resilience.py \
  server/tests/test_population_group_history.py \
  server/tests/test_pr_population_merge_queue.py \
  server/tests/test_eta_parity_live.py -v
```

Expected: PASS. `test_population_eta_resilience.py` is the one most likely to catch a behaviour
change — if it fails, the per-PR body was altered rather than moved.

- [ ] **Step 6: Commit**

```bash
git add server/github/pr_population.py server/tests/test_population_eta_offloads_loop.py
git commit -m "fix(pipeline): run ETA enrichment on a worker thread, not the event loop

py-spy caught rocky's daemon event loop inside pipeline/history.py in 8 of 8
samples over 24s: pr_population computed ETAs with blocking sqlite3 reads
inline, starving every other task on the daemon — RPC timeouts, a flapping
events WebSocket, and sockets piling into CLOSE-WAIT on the edge.

Move the blocking half to asyncio.to_thread. The test asserts the loop stays
schedulable, not just that the return value is right.

Refs #734

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 3: Add a loop-lag watchdog

Nothing reported that the daemon's event loop was fully saturated — it took an external `py-spy`
dump to find it. This makes the condition observable so the next regression is caught by the system
rather than by an audit.

**Files:**
- Create: `server/obs/loop_lag.py`
- Modify: `server/ipc/serve.py:106` (start the watchdog beside `orchestrator.start()`)
- Test: `server/tests/test_loop_lag_watchdog.py` (create)

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `LoopLagWatchdog(interval: float = 1.0, warn_after: float = 0.5, _log=None)` with
  `async def run(self) -> None` (never returns; cancel it to stop) and
  `peak_lag_s: float` (highest lag seen, readable for a future health endpoint).

- [ ] **Step 1: Write the failing test**

Create `server/tests/test_loop_lag_watchdog.py`:

```python
"""The watchdog must notice when the event loop stops being scheduled."""
import asyncio
import time

import pytest

from server.obs.loop_lag import LoopLagWatchdog


class _CapturingLog:
    def __init__(self) -> None:
        self.warnings: list[tuple] = []

    def warning(self, msg, *args, **kwargs) -> None:
        self.warnings.append((msg, args))


@pytest.mark.asyncio
async def test_reports_nothing_when_the_loop_is_healthy():
    log = _CapturingLog()
    wd = LoopLagWatchdog(interval=0.05, warn_after=0.5, _log=log)
    task = asyncio.create_task(wd.run())
    await asyncio.sleep(0.3)
    task.cancel()

    assert log.warnings == []
    assert wd.peak_lag_s < 0.5


@pytest.mark.asyncio
async def test_warns_when_the_loop_is_blocked():
    log = _CapturingLog()
    wd = LoopLagWatchdog(interval=0.05, warn_after=0.2, _log=log)
    task = asyncio.create_task(wd.run())
    await asyncio.sleep(0.05)

    time.sleep(0.6)          # block the loop exactly as a sync sqlite read would
    await asyncio.sleep(0.1)  # let the watchdog observe the overshoot
    task.cancel()

    assert log.warnings, "a 600ms block must be reported"
    assert wd.peak_lag_s >= 0.2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 pytest server/tests/test_loop_lag_watchdog.py -v`

Expected: FAIL with `ModuleNotFoundError: No module named 'server.obs.loop_lag'`.

- [ ] **Step 3: Write minimal implementation**

Create `server/obs/loop_lag.py`:

```python
"""Event-loop lag watchdog.

Sleeps for a fixed interval and measures how much longer than that it actually
took. The overshoot is time the loop spent unable to schedule us — i.e. blocked
in synchronous work. RC1 (blocking sqlite3 on the loop) was invisible until an
external py-spy dump; this makes it self-reporting.
"""
import asyncio
import logging
import time

_default_log = logging.getLogger("server.obs.loop_lag")


class LoopLagWatchdog:
    def __init__(self, interval: float = 1.0, warn_after: float = 0.5, _log=None) -> None:
        self.interval = interval
        self.warn_after = warn_after
        self.peak_lag_s = 0.0
        self._log = _log if _log is not None else _default_log

    async def run(self) -> None:
        while True:
            started = time.monotonic()
            await asyncio.sleep(self.interval)
            lag = (time.monotonic() - started) - self.interval
            if lag > self.peak_lag_s:
                self.peak_lag_s = lag
            if lag >= self.warn_after:
                self._log.warning(
                    "loop_lag: event loop blocked for %.2fs (interval=%.2fs)",
                    lag, self.interval,
                )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 pytest server/tests/test_loop_lag_watchdog.py -v`

Expected: PASS, both tests.

- [ ] **Step 5: Start the watchdog in the daemon**

In `server/ipc/serve.py`, beside the existing `await orchestrator.start()` at line 106:

```python
    from server.obs.loop_lag import LoopLagWatchdog
    app.state.loop_lag = LoopLagWatchdog()
    app.state.loop_lag_task = asyncio.create_task(
        app.state.loop_lag.run(), name="loop-lag-watchdog"
    )
```

Cancel `app.state.loop_lag_task` in the same shutdown path that stops the orchestrator — read the
surrounding lifespan/shutdown handler in `serve.py` and follow whatever teardown pattern it already
uses for background tasks, rather than adding a new one.

- [ ] **Step 6: Run the full server suite**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 pytest server/tests/ -q`

Expected: PASS, no new failures versus the pre-change baseline. Record the baseline first with
`git stash` if you are unsure which failures pre-existed.

- [ ] **Step 7: Commit**

```bash
git add server/obs/loop_lag.py server/ipc/serve.py server/tests/test_loop_lag_watchdog.py
git commit -m "feat(obs): add an event-loop lag watchdog to the session daemon

RC1 saturated rocky's event loop for hours and nothing reported it — it took
an external py-spy dump to find. Measure sleep overshoot and warn past a
threshold so the next regression is self-reporting.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 4: Sweep for sibling blocking-I/O-on-the-loop call sites

Per `CLAUDE.md` ("audit recurring fix patterns — don't just patch") and the spec's D3, RC1 is a
*pattern*, not one call site. `HistoryStore` was found by a stack dump; others may exist. This task
finds them and records the result — false-positive audits are cheap, a second silent instance is not.

**Files:**
- Create: `docs/superpowers/fix-patterns/2026-08-22-blocking-io-on-event-loop.md`
- Modify: none (any fix found gets its own task/PR; this task only audits)

**Interfaces:**
- Consumes: nothing.
- Produces: an audit note; no code interface.

- [ ] **Step 1: Search for synchronous drivers constructed in async modules**

```bash
grep -rn "sqlite3.connect\|psycopg2.connect\|requests\.\(get\|post\|put\|delete\)\|urllib.request.urlopen" \
  server/ src/ --include=*.py | grep -v "^server/tests\|/tests/"
```

Record every hit. For each, determine whether it is reachable from a coroutine.

- [ ] **Step 2: Search for blocking filesystem and subprocess calls in async code**

```bash
grep -rn "subprocess.run\|subprocess.check_output\|\.read_text()\|\.write_text()\|os.walk\|\.glob(\|\.rglob(" \
  server/ --include=*.py | grep -v "^server/tests\|/tests/"
```

`server/sessions/replay.py:259` is a known hit — it is RC2, planned separately as P2. Note it and
move on; do not fix it here.

- [ ] **Step 3: Cross-check against what actually runs on the loop**

For each candidate, confirm reachability from one of the 17 loops in `orchestrator.start()` or from
a request handler. A blocking call in a CLI path or a startup-only path is not a defect. Use:

```bash
grep -n "create_task\|await .*start()" server/sessions/orchestrator.py | head -30
```

- [ ] **Step 4: Write the fix-pattern note**

Create `docs/superpowers/fix-patterns/2026-08-22-blocking-io-on-event-loop.md` recording: the
signature of the bug (a synchronous driver or filesystem call reachable from a coroutine), how RC1
was detected (`py-spy dump --pid <MainPID>`, look for the MainThread stack in non-asyncio frames),
why tests missed it (they assert results, not scheduling latency), and the table of every call site
found in Steps 1-3 with a verdict for each: *fixed in P1*, *planned as P2*, *not reachable from the
loop*, or *needs its own issue*. Open a GitHub issue for anything in that last category.

- [ ] **Step 5: Commit**

```bash
git add docs/superpowers/fix-patterns/2026-08-22-blocking-io-on-event-loop.md
git commit -m "docs: record the blocking-IO-on-the-event-loop fix pattern

Audit for siblings of RC1 rather than patching one call site, per CLAUDE.md.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

## Verification

After all four tasks, verify against the live fleet — this is the acceptance check from the spec:

- [ ] Deploy to rocky and confirm the daemon starts cleanly.
- [ ] Re-run the stack dump and confirm the MainThread is **no longer** in `pipeline/history.py`:
  ```bash
  ssh tony@rocky 'PID=$(systemctl show -p MainPID --value ccswitch-daemon.service); \
    for i in 1 2 3 4 5 6 7 8; do \
      sudo /home/tony/GitHub/ccswitch/.venv/bin/py-spy dump --pid $PID 2>/dev/null \
        | sed -n "/MainThread/,/^Thread/p" | sed -n "2p"; sleep 3; done'
  ```
  Expected: mostly `select`/`epoll`/idle frames, not `history.py`.
- [ ] Re-run the daemon latency probe and confirm the timeout rate drops from the measured 10%:
  ```bash
  for i in $(seq 1 30); do curl -s -o /dev/null -w "%{http_code} %{time_total}\n" --max-time 20 \
    --cert /etc/ccswitch/tls/client.pem --key /etc/ccswitch/tls/client.key \
    --cacert /etc/ccswitch/tls/ca.pem https://rocky.tail0dd570.ts.net:9443/info; done
  ```
  Baseline to beat: 27/30 OK, median 0.22s, 3 hard timeouts.
- [ ] Confirm `loop_lag` warnings are absent in `journalctl -u ccswitch-daemon.service`.

**Note:** P1 does **not** make sessions openable — that is RC2/P2. Expect `/api/sessions/{id}/transcript`
to still 503 after this plan lands. P1's win is that everything else on the daemon stops being starved.

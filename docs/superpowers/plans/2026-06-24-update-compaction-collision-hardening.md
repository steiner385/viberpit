# Update × Compaction Collision Hardening — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop a deploy from force-killing a session that is actively compacting, and recover cleanly when a kill is genuinely unavoidable (long capped turn or hard crash).

**Architecture:** Three independent, defense-in-depth layers. (1) **Prevent** — the deploy drain exempts a `condensing` session from the 180s turn cap, waiting up to a longer compaction ceiling instead, because compaction is short, uninterruptible, and loses data if killed mid-write. (2) **Recover foreground work** — at drain we snapshot the in-flight foreground prompt *only when the turn produced no durable output yet* (so a re-send can't duplicate destructive work), then re-send it after the restart, mirroring the existing background-task re-arm. (3) **Repair the transcript** — truncate a provably-corrupt trailing JSONL line (a kill mid-write) before resume, alongside the existing orphaned-`tool_use` heal.

**Tech Stack:** Python 3 · FastAPI/Uvicorn daemon · SQLAlchemy (SQLite dev / Postgres prod) · asyncio · pytest (`pytest-asyncio`).

## Global Constraints

- **Tests run against the prebuilt env** — a fresh worktree has no built venv. Run pytest as:
  `PYTHONPATH=<worktree>/src <main-checkout>/.venv/bin/python -m pytest server/tests/<file> -q`
  (the worktree is `/mnt/ssk-ssd/tony/GitHub/.ccswitch/worktrees/cc-account-switcher/5e24d443`; the
  main checkout's prebuilt `.venv` is the deploy checkout under `/home/tony/...` — confirm at run time).
- **New DB columns MUST be nullable** — `create_all` never ALTERs; `init_db` only backfills missing
  *nullable* columns. A NOT NULL column needs a real migration. (CLAUDE.md → Dev / testing.)
- **Single source of truth** — extend the existing `pending_rearm` snapshot/re-arm machinery; do NOT
  add a second parallel restart-recovery path. (Constitution VI.)
- **Lint gates run in CI first** — `ruff` + `mypy` must pass on `server/` before pushing.
- **Naming:** the compaction status string is exactly `"condensing"` (see `status.py:derive_status`).

---

### Task 1: Exempt a compacting session from the drain turn-cap

The root-cause fix. The daemon reports how many sessions are *condensing*; the controller waits up to a
longer compaction ceiling for those instead of the 180s turn cap, so a deploy never SIGKILLs a session
mid-compaction.

**Files:**
- Modify: `server/sessions/orchestrator.py` — add `condensing_session_count()`; add `"condensing"` to `drain_status()` output.
- Modify: `server/deploy/drain.py` — `drain_and_wait` gains `turn_cap_s` / `condense_cap_s`, reads env defaults, applies the per-reason ceiling.
- Test: `server/tests/test_orchestrator_drain.py` — assert `drain_status()` reports the condensing count.
- Test: `server/tests/test_deploy_drain.py` — rename `cap_s` callers; add the compaction-exemption cases.

**Interfaces:**
- Produces: `SessionOrchestrator.condensing_session_count() -> int`.
- Produces: `drain_status()` dict now includes key `"condensing": int` alongside `state`, `busySessions`, `elapsed`.
- Produces: `drain.drain_and_wait(client, *, turn_cap_s: float | None = None, condense_cap_s: float | None = None, poll_s=2.0, now=time.monotonic, sleep=asyncio.sleep) -> str` returning `"ready" | "cap" | "unavailable"`.

- [ ] **Step 1: Write the failing orchestrator test**

In `server/tests/test_orchestrator_drain.py`, after `test_begin_drain_with_active_session_is_draining`:

```python
@pytest.mark.asyncio
async def test_drain_status_reports_condensing_count():
    o = _orch()
    o._records["a"] = _rec("a", "condensing")
    o._records["b"] = _rec("b", "in_progress")
    st = await o.begin_drain(selfclear_sec=999)
    assert st["state"] == "draining"
    assert st["busySessions"] == 2   # condensing is still "active"
    assert st["condensing"] == 1     # but separately countable
    o.cancel_drain()
```

- [ ] **Step 2: Run it; verify it fails**

Run: `PYTHONPATH=$PWD/src <venv>/bin/python -m pytest server/tests/test_orchestrator_drain.py::test_drain_status_reports_condensing_count -q`
Expected: FAIL — `KeyError: 'condensing'`.

- [ ] **Step 3: Implement the daemon side**

In `server/sessions/orchestrator.py`, add after `active_session_count` (≈line 1086):

```python
    def condensing_session_count(self) -> int:
        """Number of sessions mid-compaction. A subset of active_session_count():
        compaction is uninterruptible and loses data if killed mid-write, so the
        deploy drain protects these past the ordinary turn cap (see deploy.drain)."""
        return sum(1 for r in self._records.values() if r.status == "condensing")
```

Then in `drain_status()` (≈line 1144), add the key:

```python
        return {"state": state, "busySessions": active,
                "condensing": self.condensing_session_count(),
                "elapsed": round(elapsed, 1)}
```

- [ ] **Step 4: Run it; verify it passes**

Run: same command as Step 2. Expected: PASS.

- [ ] **Step 5: Write the failing controller-drain tests**

In `server/tests/test_deploy_drain.py`, first update the existing `FakeClient.call` so a default
`drain_status` reply carries the new key, and update the three existing `cap_s=` callers to `turn_cap_s=`:

```python
        if method == "drain_status":
            return self._statuses.pop(0) if self._statuses else {"state": "working", "condensing": 0}
```

Change `cap_s=100` → `turn_cap_s=100` (line ~26), `cap_s=5` → `turn_cap_s=5` (line ~40). Then add:

```python
@pytest.mark.asyncio
async def test_condensing_session_is_not_capped_at_turn_cap():
    # Past the turn cap but still condensing → keep waiting (no "cap"); then it finishes.
    ticks = iter([0, 0, 200, 200, 201])
    c = FakeClient([{"state": "draining", "condensing": 1},
                    {"state": "draining", "condensing": 1},
                    {"state": "ready", "condensing": 0}])
    res = await drain.drain_and_wait(c, turn_cap_s=180, condense_cap_s=600, poll_s=0,
                                     now=lambda: next(ticks), sleep=lambda s: asyncio.sleep(0))
    assert res == "ready"


@pytest.mark.asyncio
async def test_condensing_still_capped_at_condense_ceiling():
    ticks = iter([0, 0, 1, 999])   # now() jumps past the condense ceiling
    c = FakeClient([{"state": "draining", "condensing": 1}] * 10)
    res = await drain.drain_and_wait(c, turn_cap_s=180, condense_cap_s=600, poll_s=0,
                                     now=lambda: next(ticks), sleep=lambda s: asyncio.sleep(0))
    assert res == "cap"


@pytest.mark.asyncio
async def test_plain_turn_still_capped_at_turn_cap():
    ticks = iter([0, 0, 1, 200])   # past turn cap, nothing condensing
    c = FakeClient([{"state": "draining", "condensing": 0}] * 10)
    res = await drain.drain_and_wait(c, turn_cap_s=180, condense_cap_s=600, poll_s=0,
                                     now=lambda: next(ticks), sleep=lambda s: asyncio.sleep(0))
    assert res == "cap"
```

- [ ] **Step 6: Run them; verify they fail**

Run: `PYTHONPATH=$PWD/src <venv>/bin/python -m pytest server/tests/test_deploy_drain.py -q`
Expected: the new tests FAIL (`drain_and_wait` has no `turn_cap_s`/`condense_cap_s`).

- [ ] **Step 7: Implement the controller side**

Replace the body of `server/deploy/drain.py` with:

```python
from __future__ import annotations
import asyncio
import os
import time
from typing import Callable
from server.ipc.client import DaemonError, DaemonUnavailable


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except ValueError:
        return default


async def drain_and_wait(client, *, turn_cap_s: float | None = None,
                         condense_cap_s: float | None = None, poll_s: float = 2.0,
                         now: Callable[[], float] = time.monotonic,
                         sleep=asyncio.sleep) -> str:
    """Ask the daemon to drain, then wait until it's safe to restart.

    A session mid-compaction ('condensing') is uninterruptible and loses data if
    killed mid-write, so it is exempt from ``turn_cap_s`` and instead protected up
    to the longer ``condense_cap_s`` ceiling. Ordinary in-flight turns are still
    capped at ``turn_cap_s``. Returns 'ready' (safe), 'cap' (gave up — restart
    anyway), or 'unavailable' (daemon unreachable).
    """
    turn_cap_s = turn_cap_s if turn_cap_s is not None else _env_float("CCSWITCH_DRAIN_TURN_CAP_SEC", 180.0)
    condense_cap_s = condense_cap_s if condense_cap_s is not None else _env_float("CCSWITCH_DRAIN_CONDENSE_CAP_SEC", 570.0)
    try:
        await client.call("begin_drain")
    except (DaemonUnavailable, DaemonError):
        return "unavailable"
    start = now()
    while True:
        try:
            status = await client.call("drain_status")
        except (DaemonUnavailable, DaemonError):
            return "unavailable"
        if status.get("state") == "ready":
            return "ready"
        elapsed = now() - start
        ceiling = condense_cap_s if status.get("condensing", 0) > 0 else turn_cap_s
        if elapsed >= ceiling:
            return "cap"
        await sleep(poll_s)
```

Note: `condense_cap_s` default (570) sits just below the daemon self-clear default (600,
`CCSWITCH_DRAIN_SELFCLEAR_SEC`) so the controller pulls the trigger before the daemon self-clears
the drain out from under it.

- [ ] **Step 8: Run the full drain + orchestrator-drain suites; verify green**

Run: `PYTHONPATH=$PWD/src <venv>/bin/python -m pytest server/tests/test_deploy_drain.py server/tests/test_orchestrator_drain.py server/tests/test_ipc_drain_rpc.py -q`
Expected: all PASS.

- [ ] **Step 9: Lint**

Run: `<venv>/bin/python -m ruff check server/deploy/drain.py server/sessions/orchestrator.py && <venv>/bin/python -m mypy server/deploy/drain.py`
Expected: clean.

- [ ] **Step 10: Commit**

```bash
git add server/deploy/drain.py server/sessions/orchestrator.py \
        server/tests/test_deploy_drain.py server/tests/test_orchestrator_drain.py
git commit -m "fix(deploy): exempt a compacting session from the drain turn-cap

A deploy drain capped every busy session at 180s and then restarted anyway,
SIGKILLing a session mid-compaction. Compaction is uninterruptible and loses
data if killed mid-write, so drain_status now reports the condensing count and
the controller protects those up to a longer ceiling (570s) while still capping
ordinary turns at 180s.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: Re-send the in-flight foreground prompt across a restart (progress-safe)

When a kill IS unavoidable (a turn capped at 180s, or a hard crash), the foreground prompt that was in
flight is silently dropped — only background tasks re-arm today. Snapshot it at drain and re-send it
after the restart, but **only when the turn produced no durable output yet**, so a re-send can never
duplicate destructive work (a half-finished edit/Bash). Reuses the `pending_rearm` snapshot machinery.

**Files:**
- Modify: `server/db/models.py` — add nullable `pending_prompt` column.
- Modify: `server/sessions/driver.py` — track per-turn output; expose `pending_foreground_prompt()`.
- Modify: `server/sessions/orchestrator.py` — snapshot the prompt at drain; re-send it on re-arm.
- Test: `server/tests/test_driver_condensing.py` — `pending_foreground_prompt` gating.
- Test: `server/tests/test_rearm_drain_snapshot.py` — drain snapshots the prompt; re-arm re-sends it.

**Interfaces:**
- Consumes: `Session.pending_rearm` snapshot/clear pattern (Task 0 / existing).
- Produces: `Session.pending_prompt: Mapped[str | None]` (nullable String column).
- Produces: `SessionDriver.pending_foreground_prompt() -> str | None` — the active prompt iff a turn is
  live AND it has produced no assistant output/tool activity yet; else `None`.

- [ ] **Step 1: Write the failing driver test**

In `server/tests/test_driver_condensing.py` (match its existing driver-construction helper; if it
builds a driver via a local `_driver()`/fixture, reuse it), add:

```python
@pytest.mark.asyncio
async def test_pending_foreground_prompt_only_before_output(driver_factory):
    d = driver_factory()           # adapt to this file's existing driver builder
    await d.start()
    assert d.pending_foreground_prompt() is None      # no turn yet
    await d._start_turn("do the thing")
    assert d.pending_foreground_prompt() == "do the thing"   # live turn, no output yet
    d._turn_produced_output = True
    assert d.pending_foreground_prompt() is None       # produced output → unsafe to resend
```

*(If `test_driver_condensing.py` has no reusable driver fixture, place this test next to the existing
driver-construction helper in that file and call it the same way the file's other tests build a driver.)*

- [ ] **Step 2: Run it; verify it fails**

Run: `PYTHONPATH=$PWD/src <venv>/bin/python -m pytest server/tests/test_driver_condensing.py::test_pending_foreground_prompt_only_before_output -q`
Expected: FAIL — `AttributeError: pending_foreground_prompt`.

- [ ] **Step 3: Implement the driver tracking**

In `server/sessions/driver.py`, in `__init__` add the flag alongside the other per-turn fields
(near `self._last_user_text`):

```python
        self._turn_produced_output = False
```

In `_start_turn` (≈line 388), reset it next to `self._last_assistant_parts = []`:

```python
        self._last_user_text = text
        self._last_assistant_parts = []
        self._turn_produced_output = False
```

In `_drain`, mark output when a real (non-retry, non-internal) event is forwarded — add right before
`await self._out.put(e)` (≈line 480):

```python
                    if isinstance(e, (ev.TextDelta, ev.ToolCall, ev.ToolResult)):
                        self._turn_produced_output = True
```

Then add the accessor (near `current_status`, ≈line 132):

```python
    def pending_foreground_prompt(self) -> str | None:
        """The active foreground prompt iff a turn is live and has produced no durable
        output yet — safe to re-send after a deploy/crash kill without duplicating
        work. Returns None once the turn has emitted text or run a tool."""
        if self._turn_active and not self._turn_produced_output:
            return self._last_user_text
        return None
```

Verify `ev.ToolCall` / `ev.ToolResult` are the correct event class names in this module's `ev`
import (grep `class ToolCall` / `class ToolResult` in `server/sessions/events.py`); adjust if named differently.

- [ ] **Step 4: Run it; verify it passes**

Run: same as Step 2. Expected: PASS.

- [ ] **Step 5: Add the DB column (failing test first)**

In `server/tests/test_rearm_drain_snapshot.py`, add a test that a drained mid-turn session persists its
prompt (adapt the file's existing orchestrator/session setup — it already builds a session with a live
driver and calls the drain snapshot):

```python
@pytest.mark.asyncio
async def test_drain_snapshots_foreground_prompt(...):   # mirror this file's existing fixtures
    # ... build a session whose driver has a live turn with no output yet ...
    # driver.pending_foreground_prompt() must return "the prompt"
    await orch.begin_drain(selfclear_sec=999)
    async with db_session() as db:
        row = (await db.execute(select(DbSession).where(DbSession.id == sid))).scalar_one()
        assert row.pending_prompt == "the prompt"
    orch.cancel_drain()
```

- [ ] **Step 6: Run it; verify it fails**

Run: `PYTHONPATH=$PWD/src <venv>/bin/python -m pytest server/tests/test_rearm_drain_snapshot.py::test_drain_snapshots_foreground_prompt -q`
Expected: FAIL — `AttributeError: ... 'pending_prompt'` (column absent).

- [ ] **Step 7: Implement the column**

In `server/db/models.py`, after the `pending_rearm` column (≈line 73):

```python
    # The in-flight foreground prompt snapshotted at deploy drain, persisted ONLY when
    # the turn had produced no durable output yet (so re-sending it after the restart
    # cannot duplicate destructive work). NULL = nothing to re-send. Nullable so init_db
    # backfills it on the dev SQLite DB without a migration.
    pending_prompt: Mapped[str | None] = mapped_column(String, nullable=True, default=None)
```

- [ ] **Step 8: Snapshot the prompt at drain**

In `server/sessions/orchestrator.py`, extend `_snapshot_open_tasks` so each scanned session also
records its foreground prompt. After the existing `pending_rearm` write loop (≈line 1124), within the
same `db_session()` block, add a parallel write driven by the live drivers:

```python
            for sid in session_ids:
                drv = self._drivers.get(sid)
                prompt = drv.pending_foreground_prompt() if drv is not None else None
                await db.execute(sa_update(DbSession).where(DbSession.id == sid)
                                 .values(pending_prompt=prompt))
```

(Writing `None` for sessions with no resumable prompt also clears any stale value.)

- [ ] **Step 9: Re-send the prompt after restart**

In `server/sessions/orchestrator.py`, extend `_rearm_session` so it also re-sends a snapshotted
foreground prompt. Change the signature to accept the prompt and re-send it after the bg-task prompt
(or independently). Update the `pending` builder in `rearm_background_tasks` (≈line 897) to also select
rows with a `pending_prompt`, and clear `pending_prompt` when consumed. Concretely, in `_rearm_session`
replace the clear-and-return tail (≈lines 925-930) with:

```python
        if tasks:
            await driver.send_prompt(build_rearm_prompt(tasks))
        if prompt:
            await driver.send_prompt(prompt)
        self._rearmed.add(sid)
        async with db_session() as db:
            await db.execute(sa_update(DbSession).where(DbSession.id == sid)
                             .values(pending_rearm=None, pending_prompt=None))
        return True
```

…and make `_rearm_session(self, sid, snap, prompt=None)` plus the `ensure_driver` guard return early
only when BOTH `tasks` and `prompt` are empty. Update `rearm_background_tasks`'s query/`pending` list
to include `pending_prompt`-only rows and pass `r.pending_prompt` through. Update
`_maybe_rearm_on_attach` similarly (read `row.pending_prompt`, pass it in).

- [ ] **Step 10: Write the re-arm re-send test**

In `server/tests/test_rearm_drain_snapshot.py` (or `test_rearm_dispatch.py`, matching where re-arm
dispatch is exercised), assert that a row with only `pending_prompt` set causes a `send_prompt(prompt)`
on the re-armed driver and clears the column. Mirror the existing re-arm dispatch test's fake driver
capture of `send_prompt` calls.

- [ ] **Step 11: Run the driver + re-arm suites; verify green**

Run: `PYTHONPATH=$PWD/src <venv>/bin/python -m pytest server/tests/test_driver_condensing.py server/tests/test_rearm_drain_snapshot.py server/tests/test_rearm_dispatch.py server/tests/test_rearm_on_attach.py server/tests/test_pending_rearm_column.py -q`
Expected: all PASS.

- [ ] **Step 12: Lint**

Run: `<venv>/bin/python -m ruff check server/sessions/driver.py server/sessions/orchestrator.py server/db/models.py && <venv>/bin/python -m mypy server/sessions/driver.py server/db/models.py`
Expected: clean.

- [ ] **Step 13: Commit**

```bash
git add server/sessions/driver.py server/sessions/orchestrator.py server/db/models.py \
        server/tests/test_driver_condensing.py server/tests/test_rearm_drain_snapshot.py \
        server/tests/test_rearm_dispatch.py
git commit -m "feat(deploy): re-send the in-flight foreground prompt after a restart

Only background tasks re-armed after a deploy restart; the foreground prompt in
flight was silently dropped. Snapshot it at drain and re-send it post-restart —
but ONLY when the turn produced no durable output yet, so the at-least-once
re-send can never duplicate a half-finished edit/Bash.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: Truncate a corrupt trailing transcript line before resume

A kill mid-transcript-write (e.g. during compaction) can leave a partial, unparseable final JSONL line.
`_read_entries` skips it on read but leaves it on disk, so the `claude` binary still sees it on resume.
Truncate a provably-corrupt *trailing* line (only the trailing one — mid-file corruption is a different
problem and left alone) before the existing orphaned-`tool_use` heal. Defensive: safe even if the binary
tolerates it.

**Files:**
- Modify: `server/sessions/transcript_repair.py` — add `truncate_corrupt_tail`; call it first in `repair_transcript`.
- Test: `server/tests/test_transcript_repair.py` — corrupt-tail truncation cases.

**Interfaces:**
- Produces: `transcript_repair.truncate_corrupt_tail(path: str) -> int` — number of lines removed (0 or 1). Idempotent.
- `repair_transcript(path, *, now_iso=None)` now truncates a corrupt tail before planning the orphan heal.

- [ ] **Step 1: Write the failing tests**

In `server/tests/test_transcript_repair.py`, add:

```python
def test_truncate_corrupt_tail_removes_partial_last_line(tmp_path):
    from server.sessions.transcript_repair import truncate_corrupt_tail
    p = tmp_path / "t.jsonl"
    p.write_text(json.dumps({"type": "user", "uuid": "u1"}) + "\n" + '{"type": "assist')  # partial tail
    removed = truncate_corrupt_tail(str(p))
    assert removed == 1
    lines = [ln for ln in p.read_text().splitlines() if ln.strip()]
    assert len(lines) == 1 and json.loads(lines[0])["uuid"] == "u1"


def test_truncate_corrupt_tail_noop_on_clean_file(tmp_path):
    from server.sessions.transcript_repair import truncate_corrupt_tail
    p = tmp_path / "t.jsonl"
    p.write_text(json.dumps({"type": "user", "uuid": "u1"}) + "\n")
    assert truncate_corrupt_tail(str(p)) == 0
    assert json.loads(p.read_text().splitlines()[0])["uuid"] == "u1"


def test_truncate_corrupt_tail_is_idempotent(tmp_path):
    from server.sessions.transcript_repair import truncate_corrupt_tail
    p = tmp_path / "t.jsonl"
    p.write_text(json.dumps({"type": "user", "uuid": "u1"}) + "\n" + '{"oops')
    assert truncate_corrupt_tail(str(p)) == 1
    assert truncate_corrupt_tail(str(p)) == 0
```

- [ ] **Step 2: Run them; verify they fail**

Run: `PYTHONPATH=$PWD/src <venv>/bin/python -m pytest server/tests/test_transcript_repair.py -k truncate_corrupt_tail -q`
Expected: FAIL — `ImportError: cannot import name 'truncate_corrupt_tail'`.

- [ ] **Step 3: Implement the truncation**

In `server/sessions/transcript_repair.py`, add (before `repair_transcript`):

```python
def truncate_corrupt_tail(path: str) -> int:
    """Remove a single provably-corrupt TRAILING line (a kill mid-write leaves a
    partial JSONL line). Only the last non-empty physical line is checked; mid-file
    corruption is left alone (it's skipped on read, not a resume-blocker). Returns 1
    if a line was removed, else 0. Idempotent."""
    if not os.path.isfile(path):
        return 0
    try:
        with open(path, errors="ignore") as f:
            lines = f.readlines()
    except OSError:
        return 0
    last = None
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].strip():
            last = i
            break
    if last is None:
        return 0
    try:
        json.loads(lines[last].strip())
        return 0   # tail parses → nothing to do
    except json.JSONDecodeError:
        pass
    with open(path, "w") as f:
        f.writelines(lines[:last])
    log.info("transcript_repair: truncated corrupt trailing line in %s", path)
    return 1
```

- [ ] **Step 4: Wire it into `repair_transcript`**

In `repair_transcript`, after the `os.path.isfile` guard and before `entries = _read_entries(path)`:

```python
    if not os.path.isfile(path):
        return 0
    truncate_corrupt_tail(path)
    entries = _read_entries(path)
```

- [ ] **Step 5: Run the full transcript-repair suite; verify green**

Run: `PYTHONPATH=$PWD/src <venv>/bin/python -m pytest server/tests/test_transcript_repair.py -q`
Expected: all PASS (new + existing orphan-heal tests).

- [ ] **Step 6: Lint**

Run: `<venv>/bin/python -m ruff check server/sessions/transcript_repair.py && <venv>/bin/python -m mypy server/sessions/transcript_repair.py`
Expected: clean.

- [ ] **Step 7: Commit**

```bash
git add server/sessions/transcript_repair.py server/tests/test_transcript_repair.py
git commit -m "fix(resume): truncate a corrupt trailing transcript line before resume

A kill mid-write (e.g. during compaction) can leave a partial final JSONL line.
_read_entries skipped it on read but left it on disk, so the claude binary still
saw it on resume. Truncate a provably-corrupt trailing line before the existing
orphaned-tool_use heal. Defensive + idempotent.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 4: Capture the must-port invariant + full-suite gate

**Files:**
- Modify: `CLAUDE.md` (Dev / testing or a nearby note) — one line recording the drain/compaction invariant, if a suitable section exists; otherwise skip the doc edit and just run the gate.

- [ ] **Step 1: Full backend suite**

Run: `PYTHONPATH=$PWD/src <venv>/bin/python -m pytest server/tests/ -q`
Expected: green (note any pre-existing env-only failures — `test_remote_mode_wiring`, x11/xvfb — per CLAUDE.md memory; they are environmental, not regressions).

- [ ] **Step 2: Full lint**

Run: `<venv>/bin/python -m ruff check server/ && <venv>/bin/python -m mypy server/`
Expected: clean (mypy skips untyped fn bodies).

- [ ] **Step 3: Commit any doc note, then open the PR per CLAUDE.md completion workflow**

Branch off `origin/master`, push, `gh pr create` (non-draft), `gh pr merge <#> --squash --auto`.

---

## Self-Review

**Spec coverage:**
- U5 (cap force-kills compaction) → Task 1. ✓
- C3 (dropped foreground turn) → Task 2, progress-safe. ✓
- C6 (corrupt/interrupted-compaction transcript tail) → Task 3. ✓
- "All corner cases for updates / compaction" → the already-robust paths (drain waits on condensing,
  rehydrate normalizes the stuck pill, turn-scoped flag clear, self-clear timer, bg-task re-arm) are
  verified in the investigation and need no change; this plan closes the three genuine gaps.

**Type consistency:** `condensing_session_count()` (Task 1) ↔ `drain_status()["condensing"]` (Task 1) ↔
`status.get("condensing", 0)` (Task 1 controller). `pending_foreground_prompt()` (Task 2 driver) ↔
`drv.pending_foreground_prompt()` (Task 2 orchestrator). `pending_prompt` column (Task 2 model) ↔
`row.pending_prompt` reads/writes (Task 2 orchestrator). `truncate_corrupt_tail` (Task 3) used by
`repair_transcript` (Task 3). Consistent.

**Placeholder scan:** Task 2 Steps 1/5/10 say "adapt to this file's existing fixtures" — intentional,
because the exact driver/orchestrator construction helper in those test files must be reused rather than
duplicated (the test code is otherwise fully specified). The implementer must read those two test files
first. No other placeholders.

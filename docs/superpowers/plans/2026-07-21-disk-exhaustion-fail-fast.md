# Disk-Exhaustion Fail-Fast Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make disk/inode exhaustion fail fast and loud instead of silently wedging the daemon: preflight `git worktree add`, back off + circuit-break doomed backlog spawns, and alert on inode exhaustion before it takes the daemon down.

**Architecture:** Three independent layers. (1) `create_worktree()` gains a `statvfs` preflight that raises a distinguishable `DiskSpaceError` before any git subprocess runs. (2) `BacklogRunnerLoop._refill` gains per-target exponential backoff on spawn failure plus a global circuit breaker that opens immediately on disk-exhaustion errors (retrying ENOSPC is guaranteed-futile churn — it was the amplifier in the 2026-07-21 restart loop). (3) `scripts/ccswitch-health-check.sh` checks **inode** usage as well as byte usage, over a configurable list of paths.

**Tech Stack:** Python 3.12 (`os.statvfs`, asyncio), pytest with injected-clock harness (`server/tests/test_backlog_runner_loop.py::build_loop`), bash.

## Global Constraints

- Constitution V: TDD — failing test before implementation, every task.
- Constitution VI: single source of truth — disk-error markers defined ONCE in `src/ccswitch/worktrees.py`, imported by the runner; no parallel copies.
- CI gates: `ruff check src/ server/`, `mypy server/ --ignore-missing-imports`, `pytest tests/ -q`, `pytest server/tests/ -q` — all must pass.
- Core (`src/ccswitch/`) tests live in top-level `tests/` (CI runs `pytest tests/`), NOT `src/ccswitch/tests/`.
- New model columns are out of scope — no DB changes anywhere in this plan.
- Known local-only test failures (not regressions): `server/tests/daemon_properties` collection (missing hypothesis), `test_eta_live_parity`, `copilot_proxy_resolve` — deselect, don't chase.
- Backlog-runner events use `self._ctx.emit(level, message, kind=..., target=..., detail=...)` — follow existing kinds' style (`circuit_open`, `skipped_no_account`).

---

### Task 1: `DiskSpaceError` + `check_disk_headroom()` preflight in `create_worktree`

**Files:**
- Modify: `src/ccswitch/worktrees.py` (exception near `WorktreeError` ~line 33; helper near `_run` ~line 45; call site in `create_worktree` after `dest.parent.mkdir` ~line 251)
- Test: `tests/test_worktrees.py` (append)

**Interfaces:**
- Produces: `class DiskSpaceError(WorktreeError)`; `DISK_EXHAUSTION_MARKERS: tuple[str, ...] = ("No space left on device", "insufficient disk headroom")`; `check_disk_headroom(path: str | os.PathLike[str], min_free_bytes: int | None = None, min_free_inodes: int | None = None) -> None` (raises `DiskSpaceError`). Task 3 imports `DISK_EXHAUSTION_MARKERS` from `ccswitch.worktrees`.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_worktrees.py`)

```python
import os

import pytest

from ccswitch.worktrees import (
    DISK_EXHAUSTION_MARKERS,
    DiskSpaceError,
    WorktreeError,
    check_disk_headroom,
)


class _FakeStatvfs:
    """Shape-compatible with os.statvfs_result for the fields we read."""
    def __init__(self, *, frsize=4096, bavail=10_000_000, files=1_000_000, favail=500_000):
        self.f_frsize = frsize
        self.f_bavail = bavail
        self.f_files = files
        self.f_favail = favail


def test_headroom_ok_no_raise(monkeypatch, tmp_path):
    monkeypatch.setattr(os, "statvfs", lambda p: _FakeStatvfs())
    check_disk_headroom(tmp_path)  # ~40 GB / 500k inodes free — fine


def test_headroom_low_bytes_raises(monkeypatch, tmp_path):
    monkeypatch.setattr(os, "statvfs", lambda p: _FakeStatvfs(bavail=100))  # ~400 KB free
    with pytest.raises(DiskSpaceError) as ei:
        check_disk_headroom(tmp_path)
    assert "insufficient disk headroom" in str(ei.value)


def test_headroom_low_inodes_raises(monkeypatch, tmp_path):
    monkeypatch.setattr(os, "statvfs", lambda p: _FakeStatvfs(favail=500))
    with pytest.raises(DiskSpaceError) as ei:
        check_disk_headroom(tmp_path)
    assert "inode" in str(ei.value)


def test_headroom_inode_check_skipped_when_fs_reports_none(monkeypatch, tmp_path):
    # btrfs-style: f_files == 0 means no inode accounting — must not raise.
    monkeypatch.setattr(os, "statvfs", lambda p: _FakeStatvfs(files=0, favail=0))
    check_disk_headroom(tmp_path)


def test_headroom_env_overrides(monkeypatch, tmp_path):
    monkeypatch.setattr(os, "statvfs", lambda p: _FakeStatvfs(bavail=10_000_000))
    monkeypatch.setenv("CCSWITCH_MIN_FREE_BYTES", str(10_000_000 * 4096 + 1))
    with pytest.raises(DiskSpaceError):
        check_disk_headroom(tmp_path)


def test_disk_space_error_is_worktree_error():
    # Existing callers catch WorktreeError; the subclass must stay catchable.
    assert issubclass(DiskSpaceError, WorktreeError)
    assert "insufficient disk headroom" in DISK_EXHAUSTION_MARKERS


def test_create_worktree_preflights_before_git(monkeypatch, tmp_path):
    """When headroom is too low, create_worktree raises DiskSpaceError WITHOUT
    running any git subprocess (fail-fast: no churn on a full disk)."""
    import ccswitch.worktrees as wt

    monkeypatch.setattr(os, "statvfs", lambda p: _FakeStatvfs(favail=10))
    calls: list[list[str]] = []

    def _no_git(args):
        calls.append(args)
        raise AssertionError(f"git must not run when disk is exhausted: {args}")

    # rev-parse runs BEFORE the preflight (cheap, read-only) — allow only that.
    real_run = wt._run

    def _guarded(args):
        if "rev-parse" in args:
            return real_run(args)
        return _no_git(args)

    monkeypatch.setattr(wt, "_run", _guarded)
    import subprocess
    subprocess.run(["git", "init", "-q", str(tmp_path / "repo")], check=True)
    with pytest.raises(DiskSpaceError):
        wt.create_worktree(str(tmp_path / "repo"), None, "sess1234", str(tmp_path / "root"))
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd <worktree> && python3 -m pytest tests/test_worktrees.py -k "headroom or preflights_before_git or disk_space_error" -v`
Expected: FAIL/ERROR with `ImportError: cannot import name 'DiskSpaceError'`

- [ ] **Step 3: Implement in `src/ccswitch/worktrees.py`**

Directly under the existing `WorktreeError` class (~line 33):

```python
class DiskSpaceError(WorktreeError):
    """Raised when the worktree filesystem lacks byte or inode headroom.

    Subclasses WorktreeError so existing catch-sites keep working; the
    distinct type (and the stable message marker) lets callers detect
    disk exhaustion and stop retrying instead of churning.
    """


# Single source of truth for "this failure is disk exhaustion" detection.
# The git ENOSPC string appears in captured stderr of a failed checkout;
# the headroom string is ours. server/automations/backlog_runner.py imports
# this to trip its spawn circuit breaker.
DISK_EXHAUSTION_MARKERS: tuple[str, ...] = (
    "No space left on device",
    "insufficient disk headroom",
)

# Floors for the preflight. A KinDash-sized checkout is ~1.5 GB / ~19k inodes;
# defaults leave room for one checkout plus concurrent session writes.
_MIN_FREE_BYTES_DEFAULT = 2 * 1024**3      # 2 GiB
_MIN_FREE_INODES_DEFAULT = 100_000


def check_disk_headroom(
    path: str | os.PathLike[str],
    min_free_bytes: int | None = None,
    min_free_inodes: int | None = None,
) -> None:
    """Raise DiskSpaceError if the filesystem at `path` is near exhaustion.

    Checks BOTH bytes and inodes: the 2026-07-21 incident was inode
    exhaustion with 24 GB of bytes free, invisible to byte-only checks.
    Filesystems without inode accounting (f_files == 0, e.g. btrfs) skip
    the inode check. Overridable via CCSWITCH_MIN_FREE_BYTES /
    CCSWITCH_MIN_FREE_INODES.
    """
    if min_free_bytes is None:
        min_free_bytes = int(os.environ.get("CCSWITCH_MIN_FREE_BYTES", _MIN_FREE_BYTES_DEFAULT))
    if min_free_inodes is None:
        min_free_inodes = int(os.environ.get("CCSWITCH_MIN_FREE_INODES", _MIN_FREE_INODES_DEFAULT))
    st = os.statvfs(path)
    free_bytes = st.f_frsize * st.f_bavail
    if free_bytes < min_free_bytes:
        raise DiskSpaceError(
            f"insufficient disk headroom at {os.fspath(path)}: "
            f"{free_bytes} bytes free < {min_free_bytes} required")
    if st.f_files > 0 and st.f_favail < min_free_inodes:
        raise DiskSpaceError(
            f"insufficient disk headroom at {os.fspath(path)}: "
            f"{st.f_favail} inodes free < {min_free_inodes} required")
```

In `create_worktree`, immediately after `dest.parent.mkdir(parents=True, exist_ok=True)` (~line 251):

```python
    # Fail fast on a near-full filesystem BEFORE any git churn: a doomed
    # checkout writes thousands of files, fails ENOSPC, and the write+delete
    # cycle's page cache pins the daemon cgroup at MemoryHigh (2026-07-21
    # restart-loop incident). DiskSpaceError's marker string lets the backlog
    # runner stop retrying instead of looping.
    check_disk_headroom(dest.parent)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_worktrees.py -v`
Expected: all PASS (new tests plus every pre-existing test in the file — the preflight must not break them; if a pre-existing test fails because a sandboxed tmpfs is genuinely small, monkeypatch-neutralize via a `conftest` fixture is WRONG — instead those tests run on real disks with headroom; investigate before touching them).

- [ ] **Step 5: Commit**

```bash
git add src/ccswitch/worktrees.py tests/test_worktrees.py
git commit -m "feat(worktrees): statvfs byte+inode preflight before worktree add

A doomed checkout on a full disk writes thousands of files before failing
ENOSPC; the churn's page cache pinned the daemon cgroup at MemoryHigh in
the 2026-07-21 restart loop. Fail fast with a distinguishable
DiskSpaceError instead. Inodes checked too — the incident had 24GB of
bytes free and 1,531 inodes.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: Per-target spawn-failure backoff in `BacklogRunnerLoop`

**Files:**
- Modify: `server/automations/backlog_runner.py` (`__init__` ~line 264-270; `_refill` ~line 486-554)
- Test: `server/tests/test_backlog_runner_loop.py` (append; reuse `build_loop`, `batch`, `make_config` helpers already in the file)

**Interfaces:**
- Consumes: nothing from Task 1 (independent layer).
- Produces: `self._spawn_backoff: dict[str, tuple[int, float]]` mapping target → `(consecutive_failures, retry_at_clock)`; module constants `SPAWN_BACKOFF_BASE_S = 300.0`, `SPAWN_BACKOFF_CAP_S = 3600.0`. Task 3 extends the same `except` block.

- [ ] **Step 1: Write the failing tests** (append to `server/tests/test_backlog_runner_loop.py`)

```python
def test_spawn_failure_backs_off_target(tmp_path):
    """A failed spawn is NOT retried on the next tick; it is retried after
    the backoff window (300s base) elapses."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'bo.db'}"

    async def run():
        await init_db(db_url=db_url)
        await _seed_row()
        clock = FakeClock()
        attempts = []

        async def failing_spawn(b, account):
            attempts.append(clock.now())
            raise RuntimeError("git worktree add --detach failed: boom")

        loop, rec, ctx = build_loop(
            config=make_config(max_concurrent=1),
            candidates=[batch("r#1"), batch("r#1"), batch("r#1")],
            spawn=failing_spawn, clock=clock.now,
        )
        await loop.tick()
        assert len(attempts) == 1
        clock.advance(120)              # < 300s base backoff
        await loop.tick()
        assert len(attempts) == 1       # still backed off — no retry churn
        clock.advance(300)              # past the window
        await loop.tick()
        assert len(attempts) == 2       # retried once eligible

    asyncio.run(run())


def test_spawn_backoff_doubles_and_caps(tmp_path):
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'bo2.db'}"

    async def run():
        await init_db(db_url=db_url)
        await _seed_row()
        clock = FakeClock()
        attempts = []

        async def failing_spawn(b, account):
            attempts.append(clock.now())
            raise RuntimeError("boom")

        loop, rec, ctx = build_loop(
            config=make_config(max_concurrent=1),
            candidates=[batch("r#1")] * 20,
            spawn=failing_spawn, clock=clock.now,
        )
        await loop.tick()               # fail #1 → backoff 300
        clock.advance(301); await loop.tick()   # fail #2 → backoff 600
        clock.advance(301); await loop.tick()   # too soon: no attempt
        assert len(attempts) == 2
        clock.advance(300); await loop.tick()   # 601s elapsed → attempt #3
        assert len(attempts) == 3

    asyncio.run(run())


def test_spawn_success_clears_backoff(tmp_path):
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'bo3.db'}"

    async def run():
        await init_db(db_url=db_url)
        await _seed_row()
        clock = FakeClock()
        calls = {"n": 0}

        async def flaky_spawn(b, account):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("boom")
            return SimpleNamespace(id=f"s{calls['n']}", branch=None)

        loop, rec, ctx = build_loop(
            config=make_config(max_concurrent=1),
            candidates=[batch("r#1")] * 5,
            spawn=flaky_spawn, clock=clock.now,
            in_flight_counter=_zero_in_flight,
        )
        await loop.tick()               # fails
        clock.advance(301); await loop.tick()   # succeeds
        assert loop._spawn_backoff == {}        # success wipes the slate

    asyncio.run(run())
```

Adapt to the file's actual local idioms when appending: `build_loop` keyword names (`spawn=`, `clock=`), the existing `FakeClock`/clock-callable pattern (if the file uses a bare `clk` list/closure instead of a FakeClock class, follow that pattern — read the top of the file first), `_seed_row`, and whether an `in_flight_counter` override is needed for a spawn to count as in-flight. The behavioral assertions (attempt counts per tick, doubling, cap, clear-on-success) are the contract; the harness plumbing is whatever the file already does.

- [ ] **Step 2: Run tests to verify they fail**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 python3 -m pytest server/tests/test_backlog_runner_loop.py -k backoff -v`
Expected: FAIL — first test asserts `len(attempts) == 1` after two ticks but gets 2 (current code retries every tick).

- [ ] **Step 3: Implement backoff in `server/automations/backlog_runner.py`**

Module constants near the top (after imports):

```python
# Spawn-failure backoff: a target whose spawn raised waits 300s, doubling per
# consecutive failure up to 1h, before the runner re-selects it. Prevents the
# every-tick retry churn that amplified the 2026-07-21 ENOSPC restart loop.
SPAWN_BACKOFF_BASE_S = 300.0
SPAWN_BACKOFF_CAP_S = 3600.0
```

In `__init__` (with the other debounce fields ~line 269):

```python
        self._spawn_backoff: dict[str, tuple[int, float]] = {}  # target → (fails, retry_at)
```

In `_refill`, right after `batch = await self._select_batch(config)` / the `batch is None` block (~line 493), before the `gave_up` check:

```python
            fails_retry = self._spawn_backoff.get(batch.primary)
            if fails_retry is not None and self._clock() < fails_retry[1]:
                # Selected target is cooling down after a failed spawn. break,
                # not continue: _select_batch would hand us the same batch
                # again and spin this for-loop.
                break
```

In the existing `except Exception` block of the spawn call (~line 543-554), after `log.warning(...)` and before `await self._release(...)`:

```python
                fails = self._spawn_backoff.get(batch.primary, (0, 0.0))[0] + 1
                delay = min(SPAWN_BACKOFF_BASE_S * (2 ** (fails - 1)), SPAWN_BACKOFF_CAP_S)
                self._spawn_backoff[batch.primary] = (fails, self._clock() + delay)
```

and extend the emitted release event's message to include the delay:

```python
                await self._ctx.emit(
                    "info",
                    f"released {batch.primary} (spawn failed; retry in {int(delay)}s)",
                    kind="unclaimed", target=batch.primary)
```

After a SUCCESSFUL spawn (right after `self._spawns_today.append(self._clock())` ~line 555):

```python
            self._spawn_backoff.pop(batch.primary, None)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 python3 -m pytest server/tests/test_backlog_runner_loop.py -v`
Expected: all PASS (new + pre-existing).

- [ ] **Step 5: Commit**

```bash
git add server/automations/backlog_runner.py server/tests/test_backlog_runner_loop.py
git commit -m "feat(backlog): exponential per-target backoff on spawn failure

Spawn failures previously released the claim and retried the same target
every tick forever — with a persistent fault (ENOSPC) that meant a doomed
git checkout every ~2 minutes, the churn amplifier in the 2026-07-21
daemon restart loop. 300s base, doubling to a 1h cap; success clears.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: Disk-exhaustion circuit breaker for all backlog spawns

**Files:**
- Modify: `server/automations/backlog_runner.py` (same regions as Task 2)
- Test: `server/tests/test_backlog_runner_loop.py` (append)

**Interfaces:**
- Consumes: `DISK_EXHAUSTION_MARKERS` from `ccswitch.worktrees` (Task 1); Task 2's `except`-block structure.
- Produces: `self._spawn_circuit_until: float | None`; module constants `SPAWN_CIRCUIT_THRESHOLD = 3`, `SPAWN_CIRCUIT_COOLDOWN_S = 1800.0`; event kinds `spawn_circuit_open`.

- [ ] **Step 1: Write the failing tests**

```python
def test_enospc_spawn_failure_trips_circuit_immediately(tmp_path):
    """A disk-exhaustion error opens the global spawn circuit on the FIRST
    failure — different targets are NOT attempted while it is open."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'cb.db'}"

    async def run():
        await init_db(db_url=db_url)
        await _seed_row()
        clock = FakeClock()
        attempts = []

        async def enospc_spawn(b, account):
            attempts.append(b.primary)
            raise RuntimeError(
                "git worktree add --detach failed: error: unable to create "
                "file x: No space left on device")

        loop, rec, ctx = build_loop(
            config=make_config(max_concurrent=2),
            candidates=[batch("r#1"), batch("r#2"), batch("r#3")],
            spawn=enospc_spawn, clock=clock.now,
        )
        await loop.tick()
        assert attempts == ["r#1"]          # circuit opened mid-tick
        clock.advance(60); await loop.tick()
        assert attempts == ["r#1"]          # still open — no new targets tried
        events = [e for e in rec.events if e.get("kind") == "spawn_circuit_open"]
        assert len(events) == 1             # alert emitted exactly once
        clock.advance(1800); await loop.tick()
        assert len(attempts) == 2           # cooldown over — probe spawn allowed

    asyncio.run(run())


def test_three_generic_failures_trip_circuit(tmp_path):
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'cb2.db'}"

    async def run():
        await init_db(db_url=db_url)
        await _seed_row()
        clock = FakeClock()
        attempts = []

        async def failing_spawn(b, account):
            attempts.append(b.primary)
            raise RuntimeError("boom")      # no disk marker

        loop, rec, ctx = build_loop(
            config=make_config(max_concurrent=3),
            candidates=[batch("r#1"), batch("r#2"), batch("r#3"), batch("r#4")],
            spawn=failing_spawn, clock=clock.now,
        )
        await loop.tick()                   # r#1, r#2, r#3 all fail → circuit opens
        assert attempts == ["r#1", "r#2", "r#3"]
        clock.advance(60); await loop.tick()
        assert len(attempts) == 3           # open

    asyncio.run(run())


def test_spawn_success_resets_consecutive_failures(tmp_path):
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'cb3.db'}"

    async def run():
        await init_db(db_url=db_url)
        await _seed_row()
        clock = FakeClock()
        calls = {"n": 0}

        async def alternating_spawn(b, account):
            calls["n"] += 1
            if calls["n"] % 2:
                raise RuntimeError("boom")
            return SimpleNamespace(id=f"s{calls['n']}", branch=None)

        loop, rec, ctx = build_loop(
            config=make_config(max_concurrent=1),
            candidates=[batch(f"r#{i}") for i in range(1, 10)],
            spawn=alternating_spawn, clock=clock.now,
            in_flight_counter=_zero_in_flight,
        )
        for _ in range(6):
            await loop.tick()
            clock.advance(3700)             # clear all backoffs between ticks
        assert loop._spawn_circuit_until is None   # never tripped: fails never consecutive

    asyncio.run(run())
```

(Adapt harness idioms exactly as in Task 2 Step 1. `rec.events` — verify the recorder's event-capture attribute name by reading `build_loop`; if events are captured on `ctx` instead, assert there.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 python3 -m pytest server/tests/test_backlog_runner_loop.py -k circuit -v`
Expected: FAIL — no circuit exists; all targets attempted every tick.

- [ ] **Step 3: Implement the breaker**

Module constants (next to Task 2's):

```python
# Global spawn circuit breaker: 3 consecutive failures (any cause), or ONE
# disk-exhaustion failure (retrying ENOSPC is guaranteed futile and each
# attempt churns gigabytes of page cache), pause ALL spawns for 30 min.
SPAWN_CIRCUIT_THRESHOLD = 3
SPAWN_CIRCUIT_COOLDOWN_S = 1800.0
```

Import at the top of the module:

```python
from ccswitch.worktrees import DISK_EXHAUSTION_MARKERS
```

`__init__` fields:

```python
        self._spawn_circuit_until: float | None = None   # clock time the breaker re-probes
        self._consec_spawn_failures = 0
```

At the top of `_refill` (first statement):

```python
        if self._spawn_circuit_until is not None:
            if self._clock() < self._spawn_circuit_until:
                return                     # breaker open — no spawn attempts at all
            self._spawn_circuit_until = None   # cooldown over → half-open probe
```

In the spawn `except Exception` block, after Task 2's backoff bookkeeping (`exc` must be bound: change `except Exception:` to `except Exception as exc:`):

```python
                self._consec_spawn_failures += 1
                msg = str(exc)
                disk = any(m in msg for m in DISK_EXHAUSTION_MARKERS)
                if disk or self._consec_spawn_failures >= SPAWN_CIRCUIT_THRESHOLD:
                    self._spawn_circuit_until = self._clock() + SPAWN_CIRCUIT_COOLDOWN_S
                    reason = "disk exhaustion" if disk else \
                        f"{self._consec_spawn_failures} consecutive spawn failures"
                    await self._ctx.emit(
                        "warn",
                        f"spawn circuit open ({reason}) — pausing all spawns "
                        f"{int(SPAWN_CIRCUIT_COOLDOWN_S // 60)}m: {msg.splitlines()[0][:200]}",
                        kind="spawn_circuit_open")
```

then (still inside the except block, after the existing release/delete logic) if the circuit just opened, stop the refill loop for this tick:

```python
                if self._spawn_circuit_until is not None:
                    break
```

After a successful spawn (next to Task 2's `pop`):

```python
            self._consec_spawn_failures = 0
            self._spawn_circuit_until = None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 python3 -m pytest server/tests/test_backlog_runner_loop.py -v`
Expected: all PASS.

- [ ] **Step 5: Run the full backlog test family (regression sweep)**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 python3 -m pytest server/tests/ -k backlog -v`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add server/automations/backlog_runner.py server/tests/test_backlog_runner_loop.py
git commit -m "feat(backlog): spawn circuit breaker — trips instantly on disk exhaustion

One ENOSPC spawn failure (marker strings from ccswitch.worktrees, single
source of truth) or 3 consecutive failures of any cause open a 30-minute
global spawn pause with a warn event. Success closes and resets.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 4: Inode + multi-path disk alerts in the health watchdog

**Files:**
- Modify: `scripts/ccswitch-health-check.sh` (section `# --- 2. Disk ---`, ~line 117-123)

**Interfaces:**
- Consumes: nothing from other tasks (bash).
- Produces: env contract `CCSWITCH_DISK_PATHS` (space-separated mount points; default falls back to existing `CCSWITCH_DISK_PATH`, default `/`); alert keys `disk-<name>` and `inode-<name>`.

- [ ] **Step 1: Replace the disk section**

```bash
# --- 2. Disk (bytes AND inodes, per path) ----------------------------------------
# Inode exhaustion is invisible to byte checks: the 2026-07-21 restart loop ran
# with 24GB of bytes free and 1,531 inodes. Probe every path in
# CCSWITCH_DISK_PATHS (space-separated; falls back to CCSWITCH_DISK_PATH).
DISK_PATHS="${CCSWITCH_DISK_PATHS:-$DISK_PATH}"
for p in $DISK_PATHS; do
  key=$(echo "$p" | tr -c 'a-zA-Z0-9' '-' | sed 's/-*$//;s/^-*//'); key="${key:-root}"
  use=$(df --output=pcent "$p" 2>/dev/null | tail -1 | tr -dc '0-9')
  if [ -n "$use" ] && [ "$use" -ge "$DISK_WARN_PCT" ]; then
    alert "disk-$key" "disk $p at ${use}% (>= ${DISK_WARN_PCT}%) — archive/rehydrate writes at risk"
  else
    clear_alert "disk-$key"
  fi
  iuse=$(df --output=ipcent "$p" 2>/dev/null | tail -1 | tr -dc '0-9')
  if [ -n "$iuse" ] && [ "$iuse" -ge "$DISK_WARN_PCT" ]; then
    alert "inode-$key" "inodes on $p at ${iuse}% (>= ${DISK_WARN_PCT}%) — session spawns will fail ENOSPC; check worktree/archive leaks (df -i; see issue #591)"
  else
    clear_alert "inode-$key"
  fi
done
```

Note: the old single-path block (`use=$(df --output=pcent "$DISK_PATH" ...)` through its `clear_alert disk`) is REPLACED — delete it; `DISK_PATH` stays defined for the fallback. The old alert key `disk` becomes `disk-root` — acceptable; stale `/var/lib/ccswitch/health/alert-disk` files are inert.

- [ ] **Step 2: Verify manually (no bash test harness in repo)**

```bash
bash -n scripts/ccswitch-health-check.sh   # syntax
CCSWITCH_HEALTH_STATE_DIR=/tmp/cchealth-test CCSWITCH_DISK_PATHS="/ /mnt/ssk-ssd" \
  CCSWITCH_DISK_WARN_PCT=1 bash scripts/ccswitch-health-check.sh
```

Expected output lines: `ALERT[disk-root] ...`, `ALERT[inode-root] ...` (or clear), and `ALERT[disk-mnt-ssk-ssd] ...`, `ALERT[inode-mnt-ssk-ssd] ...` given the 1% threshold. The watchdog section may also log — that's fine; it only restarts when the daemon socket probe fails AND cooldown allows.
Then re-run with default `CCSWITCH_DISK_WARN_PCT` (85) and confirm the inode alert for `/mnt/ssk-ssd` fires iff `df -i /mnt/ssk-ssd` is ≥ 85%.

- [ ] **Step 3: Commit**

```bash
git add scripts/ccswitch-health-check.sh
git commit -m "feat(health): probe inodes and multiple mounts in the disk check

Byte-only single-path checks missed the 2026-07-21 inode exhaustion on
/mnt/ssk-ssd entirely. CCSWITCH_DISK_PATHS (space-separated) now probes
bytes AND inodes per mount, with per-path de-duplicated alert keys.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

**Deploy note (ops, outside this PR):** set `CCSWITCH_DISK_PATHS="/ /mnt/ssk-ssd"` in `/etc/ccswitch/ccswitch.env` after the deploy picks up the script.

---

### Task 5: Quality gates, PR

- [ ] **Step 1: CI-exact gates**

```bash
ruff check src/ server/
mypy server/ --ignore-missing-imports
python3 -m pytest tests/ -q
env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 python3 -m pytest server/tests/ -q \
  --ignore=server/tests/daemon_properties --deselect server/tests/test_eta_parity_live.py
```

Expected: ruff/mypy clean; test suites pass (modulo the documented pre-existing local-only failures listed in Global Constraints).

- [ ] **Step 2: Push and open the PR**

Branch: already created off `origin/master` at execution start (per completion workflow). Then:

```bash
git push -u origin <branch>
gh pr create --title "feat: fail fast and loud on disk/inode exhaustion" --body "$(cat <<'EOF'
## Summary
- statvfs preflight (bytes AND inodes) in `create_worktree` — raises `DiskSpaceError` before any git churn
- backlog runner: per-target exponential spawn-failure backoff (300s → 1h cap)
- backlog runner: global spawn circuit breaker — 30 min pause on ONE disk-exhaustion failure or 3 consecutive failures, with a `spawn_circuit_open` warn event
- health watchdog: per-mount byte AND inode alerts via `CCSWITCH_DISK_PATHS`

Breaks the 2026-07-21 restart-loop amplifier: ENOSPC spawn retries every ~2 min churned page cache into the daemon cgroup's MemoryHigh throttle, wedging the control socket every ~22 min (watchdog restart → all sessions killed).

## Test plan
- `tests/test_worktrees.py` — headroom preflight unit tests (byte floor, inode floor, btrfs skip, env overrides, no-git-on-full-disk)
- `server/tests/test_backlog_runner_loop.py` — backoff timing (base/double/cap/clear), breaker trip conditions (ENOSPC-immediate, 3-consecutive, success-reset, single alert emission)
- manual: health script run with `CCSWITCH_DISK_PATHS="/ /mnt/ssk-ssd"` at 1% threshold shows all four per-mount alerts

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
gh pr merge --squash --auto
```

Mark ready (non-draft) and confirm auto-merge armed: `gh pr view --json state,autoMergeRequest,isDraft`.

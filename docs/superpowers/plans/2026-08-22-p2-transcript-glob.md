# P2 — Make Sessions Openable Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop `transcript_window` from hanging, so a session's terminal can actually be opened in the browser.

**Architecture:** `_subagent_events` locates sub-agent sidecars with `projects_root.glob("*/<sid>/subagents/*.meta.json")`. The leading `*/` descends into every project directory — 1,696 of them, on an sshfs mount reached across a DERP-relayed link at ~54 ms RTT. We replace that single unbounded glob with an explicit discovery helper: probe the session's own project directory first (one op, the overwhelmingly common case), then scan the remaining directories under a hard wall-clock budget, degrading to a partial result rather than hanging. Then memoize the result, and finally bound `transcript_window` as a whole so no future slow path can reproduce this.

**Tech Stack:** Python 3.12, `os.scandir`, `asyncio`, pytest / pytest-asyncio.

**Spec:** `docs/superpowers/specs/2026-08-22-fleet-recovery-design.md` — root cause RC2, decision **D2 (AMENDED)**.

## Global Constraints

- Tests: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 pytest <paths>` from the repo root — the live shell env sets a real allowlist that 401s unrelated auth tests.
- This plan touches only `server/*`, so the `src/ccswitch` editable-install caveat in `CLAUDE.md` does not apply.
- No schema changes: no Alembic revision, no Postgres-suite requirement.
- **Cross-cwd lookup must keep working.** `server/tests/test_replay.py::test_replay_window_subagent_cross_cwd_join` is the existing regression guard and must stay green. Do not narrow the search to a single project directory — see D2 (AMENDED) for why that was withdrawn.
- Sub-agent reconstruction is an *enhancement* to the Task card. Opening a session is not optional. When forced to choose, degrade sub-agent data and keep the session openable.
- Every commit message ends with `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`.

## Background the implementer needs

`_subagent_events(main_path, task_ids)` derives:
- `main = Path(main_path)` → e.g. `…/projects/-cwd-main/<sid>.jsonl`
- `sid = main.stem` → the transcript session id (the join key)
- `projects_root = main.parent.parent` → `…/projects/`

Sidecars live at `projects/<any-encoded-cwd>/<sid>/subagents/agent-*.jsonl`, each with an `agent-*.meta.json` carrying `{agentType, description, toolUseId}`. The encoded-cwd of the sidecar **may differ** from the main transcript's (main checkout vs. worktree), which is why the search joins on `sid` across directories and dedupes by `toolUseId`.

Measured on the live fleet: 1,696 project directories per account, **zero** `subagents/` directories in any of the 8 accounts, `transcript_window` hanging >35 s on every attempt, while reading the transcript file itself takes 0.77 s. The traversal is the cost, not the I/O volume.

---

### Task 1: Replace the unbounded glob with bounded discovery

**Files:**
- Modify: `server/sessions/replay.py` — imports at the top, and `_subagent_events` (the `metas = sorted(projects_root.glob(...))` line and its surroundings)
- Test: `server/tests/test_replay_subagent_discovery.py` (create)

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: a module-level helper
  `_find_subagent_meta_files(projects_root: Path, sid: str, preferred: Path | None = None, *, budget_s: float | None = None) -> tuple[list[Path], bool]`
  returning `(sorted meta paths, complete)`, where `complete is False` means the scan hit its budget and the list may be incomplete. Task 2 memoizes this exact function; Task 3 relies on `_subagent_events` never blocking unboundedly.

- [ ] **Step 1: Write the failing test**

Create `server/tests/test_replay_subagent_discovery.py`:

```python
"""Sub-agent sidecar discovery must be bounded (RC2).

The old implementation globbed `*/<sid>/subagents/*.meta.json` across every
project directory — 1,696 of them on the live fleet, over an sshfs mount at
~54ms RTT — which hung transcript_window for 30s+ and made sessions
un-openable. Discovery must degrade to a partial result instead of hanging.
"""
import time
from pathlib import Path

import pytest

from server.sessions.replay import _find_subagent_meta_files


def _layout(root: Path, sid: str, *, cwd_dirs: list[str], sub_in: str) -> Path:
    """Build projects/<cwd_dirs...>/ with the sidecar under `sub_in`."""
    projects = root / "projects"
    for d in cwd_dirs:
        (projects / d).mkdir(parents=True, exist_ok=True)
    sub = projects / sub_in / sid / "subagents"
    sub.mkdir(parents=True, exist_ok=True)
    (sub / "agent-1.meta.json").write_text('{"toolUseId": "toolu_task"}')
    return projects


def test_finds_sidecar_in_the_sessions_own_project_dir(tmp_path):
    projects = _layout(tmp_path, "s1", cwd_dirs=["-cwd-main", "-cwd-other"], sub_in="-cwd-main")
    metas, complete = _find_subagent_meta_files(projects, "s1", preferred=projects / "-cwd-main")
    assert [p.name for p in metas] == ["agent-1.meta.json"]
    assert complete is True


def test_finds_sidecar_under_a_different_cwd(tmp_path):
    """The cross-cwd join (main checkout vs. worktree) must keep working."""
    projects = _layout(tmp_path, "s2", cwd_dirs=["-cwd-main", "-cwd-worktree"], sub_in="-cwd-worktree")
    metas, complete = _find_subagent_meta_files(projects, "s2", preferred=projects / "-cwd-main")
    assert [p.name for p in metas] == ["agent-1.meta.json"]
    assert complete is True


def test_missing_sidecar_returns_empty_not_error(tmp_path):
    projects = tmp_path / "projects"
    (projects / "-cwd-main").mkdir(parents=True)
    metas, complete = _find_subagent_meta_files(projects, "nope", preferred=projects / "-cwd-main")
    assert metas == []
    assert complete is True


def test_slow_filesystem_degrades_instead_of_hanging(tmp_path, monkeypatch):
    """THE regression test for RC2.

    Simulate the sshfs round-trip cost. With a 0.2s budget and 50ms per
    directory probe, discovery must return in well under the time a full
    traversal would take, and report itself incomplete.
    """
    projects = _layout(
        tmp_path, "s3",
        cwd_dirs=[f"-cwd-{i}" for i in range(60)] + ["-cwd-main"],
        sub_in="-cwd-main",
    )

    real_is_dir = Path.is_dir

    def slow_is_dir(self):
        time.sleep(0.05)          # stands in for one sshfs round trip
        return real_is_dir(self)

    monkeypatch.setattr(Path, "is_dir", slow_is_dir)

    started = time.monotonic()
    metas, complete = _find_subagent_meta_files(
        projects, "s3", preferred=projects / "-cwd-main", budget_s=0.2)
    elapsed = time.monotonic() - started

    # A full 61-directory traversal would cost ~3s. The budget must cut it short.
    assert elapsed < 1.0, f"discovery took {elapsed:.2f}s — budget not enforced"
    assert complete is False, "a truncated scan must report itself incomplete"
    # The fast path still delivered the sidecar that actually matters.
    assert [p.name for p in metas] == ["agent-1.meta.json"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 pytest server/tests/test_replay_subagent_discovery.py -v`

Expected: FAIL — `ImportError: cannot import name '_find_subagent_meta_files' from 'server.sessions.replay'`. The seam does not exist yet; that is the red.

- [ ] **Step 3: Write minimal implementation**

In `server/sessions/replay.py`, extend the imports at the top:

```python
from __future__ import annotations
import json, asyncio, logging, os, time
```

and add a module logger beside them (the file currently has none):

```python
log = logging.getLogger("server.sessions.replay")

# Wall-clock ceiling for the cross-cwd sidecar scan. The live fleet has ~1,696
# project dirs on an sshfs mount at ~54ms RTT, so an unbounded scan hangs the
# transcript RPC for 30s+ (RC2). Sub-agent reconstruction is an enhancement to
# the Task card; opening the session is not optional — so we cap the search and
# return what we found.
_SUBAGENT_SCAN_BUDGET_S = float(os.environ.get("CCSWITCH_SUBAGENT_SCAN_BUDGET_S", "1.0"))
```

Then add the helper above `_subagent_events`:

```python
def _find_subagent_meta_files(projects_root: Path, sid: str, preferred: Path | None = None,
                              *, budget_s: float | None = None) -> tuple[list[Path], bool]:
    """Locate `<projects>/<any-cwd>/<sid>/subagents/*.meta.json`, bounded.

    Returns (sorted meta paths, complete). ``complete`` is False when the scan
    ran out of budget, so the caller knows the list may be short.

    The sidecar usually sits under the SAME encoded-cwd as the main transcript,
    so `preferred` is probed first and costs one filesystem op. The remaining
    directories are still scanned — the sidecar can live under a different cwd
    (main checkout vs. worktree) and that join is load-bearing — but under a
    wall-clock budget so a slow mount degrades instead of hanging.
    """
    budget = _SUBAGENT_SCAN_BUDGET_S if budget_s is None else budget_s
    deadline = time.monotonic() + budget
    found: list[Path] = []
    seen_dirs: set[str] = set()

    def _collect(cwd_dir: Path) -> None:
        sub = cwd_dir / sid / "subagents"
        try:
            if not sub.is_dir():
                return
            found.extend(p for p in sub.iterdir() if p.name.endswith(".meta.json"))
        except OSError:
            return

    if preferred is not None:
        seen_dirs.add(preferred.name)
        _collect(preferred)

    complete = True
    try:
        with os.scandir(projects_root) as it:
            for entry in it:
                if time.monotonic() >= deadline:
                    complete = False
                    break
                if entry.name in seen_dirs or not entry.is_dir():
                    continue
                seen_dirs.add(entry.name)
                _collect(Path(entry.path))
    except OSError:
        return sorted(found), complete

    if not complete:
        log.warning(
            "subagent scan budget exhausted after %.2fs (projects_root=%s sid=%s) — "
            "sub-agent threads may be incomplete", budget, projects_root, sid,
        )
    return sorted(found), complete
```

Finally, in `_subagent_events`, replace the glob. The current lines are:

```python
    try:
        metas = sorted(projects_root.glob(f"*/{sid}/subagents/*.meta.json"))
    except OSError:
        return []
```

Replace with:

```python
    metas, _complete = _find_subagent_meta_files(projects_root, sid, preferred=main.parent)
```

`main.parent` is the session's own encoded-cwd directory — the fast path. Leave the rest of `_subagent_events` (the meta loop, dedupe by `toolUseId`, sidecar reading) exactly as it is.

- [ ] **Step 4: Run test to verify it passes**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 pytest server/tests/test_replay_subagent_discovery.py -v`

Expected: PASS, all four tests.

- [ ] **Step 5: Verify the existing replay suite — especially the cross-cwd guard**

Run:
```bash
env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 pytest \
  server/tests/test_replay.py \
  server/tests/test_replay_dedup.py \
  server/tests/test_replay_images.py \
  server/tests/test_replay_questions.py \
  server/tests/test_replay_trim.py \
  server/tests/test_replay_ts.py \
  server/tests/test_api_transcript_paging.py -v
```

Expected: PASS. `test_replay.py::test_replay_window_subagent_cross_cwd_join` is the one that matters most — it proves the sidecar is still found when it lives under a different encoded-cwd than the main transcript. If it fails, the search was narrowed and D2 (AMENDED) was violated.

- [ ] **Step 6: Commit**

```bash
git add server/sessions/replay.py server/tests/test_replay_subagent_discovery.py
git commit -m "fix(replay): bound sub-agent sidecar discovery so sessions can be opened

_subagent_events globbed '*/<sid>/subagents/*.meta.json' across every project
directory. On the live fleet that is 1,696 dirs on an sshfs mount at ~54ms
RTT — ~1,696 round trips to return an empty list, every time a session is
opened, hanging transcript_window past its 30s ceiling (RC2).

Probe the session's own encoded-cwd first (one op, the common case), then
scan the rest under a wall-clock budget and report the result partial rather
than hanging. Cross-cwd lookup is preserved — the sidecar can legitimately
live under a different cwd (main checkout vs. worktree).

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 2: Memoize discovery per session

Re-opening a session, paging scrollback, and reattaching all call `transcript_window` again. Each call currently re-pays the scan. The answer changes only when a sub-agent starts or finishes, so a short TTL is safe and removes the repeat cost entirely.

**Files:**
- Modify: `server/sessions/replay.py` — `_find_subagent_meta_files`
- Test: `server/tests/test_replay_subagent_discovery.py` (extend the file from Task 1)

**Interfaces:**
- Consumes: `_find_subagent_meta_files(projects_root, sid, preferred=None, *, budget_s=None) -> tuple[list[Path], bool]` from Task 1.
- Produces: same signature, now cached. Adds `_subagent_cache_clear() -> None` for tests. A **partial** result (`complete is False`) must NOT be cached — caching a truncated scan would make the degradation permanent.

- [ ] **Step 1: Write the failing test**

Append to `server/tests/test_replay_subagent_discovery.py`:

```python
def test_second_lookup_does_not_rescan(tmp_path, monkeypatch):
    from server.sessions import replay

    replay._subagent_cache_clear()
    projects = _layout(tmp_path, "s4", cwd_dirs=["-cwd-main", "-cwd-b"], sub_in="-cwd-main")

    calls = {"n": 0}
    real_scandir = os.scandir

    def counting_scandir(path):
        calls["n"] += 1
        return real_scandir(path)

    monkeypatch.setattr(replay.os, "scandir", counting_scandir)

    a, _ = replay._find_subagent_meta_files(projects, "s4", preferred=projects / "-cwd-main")
    after_first = calls["n"]
    b, _ = replay._find_subagent_meta_files(projects, "s4", preferred=projects / "-cwd-main")

    assert a == b
    assert after_first >= 1, "the first lookup should have scanned"
    assert calls["n"] == after_first, "the second lookup must be served from cache"


def test_partial_results_are_not_cached(tmp_path, monkeypatch):
    """A budget-truncated scan must not become permanent."""
    from server.sessions import replay

    replay._subagent_cache_clear()
    projects = _layout(
        tmp_path, "s5",
        cwd_dirs=[f"-cwd-{i}" for i in range(60)] + ["-cwd-main"],
        sub_in="-cwd-main",
    )

    real_is_dir = Path.is_dir

    def slow_is_dir(self):
        time.sleep(0.05)
        return real_is_dir(self)

    monkeypatch.setattr(Path, "is_dir", slow_is_dir)
    _, complete = replay._find_subagent_meta_files(
        projects, "s5", preferred=projects / "-cwd-main", budget_s=0.2)
    assert complete is False

    monkeypatch.undo()
    calls = {"n": 0}
    real_scandir = os.scandir

    def counting_scandir(path):
        calls["n"] += 1
        return real_scandir(path)

    monkeypatch.setattr(replay.os, "scandir", counting_scandir)
    _, complete2 = replay._find_subagent_meta_files(
        projects, "s5", preferred=projects / "-cwd-main")

    assert calls["n"] >= 1, "a partial result must not have been cached"
    assert complete2 is True
```

Add `import os` to the test file's imports.

- [ ] **Step 2: Run test to verify it fails**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 pytest server/tests/test_replay_subagent_discovery.py -v -k "rescan or not_cached"`

Expected: FAIL — `AttributeError: module 'server.sessions.replay' has no attribute '_subagent_cache_clear'`.

- [ ] **Step 3: Write minimal implementation**

Add beside `_SUBAGENT_SCAN_BUDGET_S`:

```python
# Sub-agent sidecar locations change only when a sub-agent starts or finishes, so a
# short TTL removes the repeat cost of reattach / scrollback paging without going stale.
_SUBAGENT_CACHE_TTL_S = float(os.environ.get("CCSWITCH_SUBAGENT_CACHE_TTL_S", "30"))
_subagent_cache: dict[tuple[str, str], tuple[float, list[Path]]] = {}


def _subagent_cache_clear() -> None:
    """Drop the discovery cache (tests, and any explicit invalidation)."""
    _subagent_cache.clear()
```

Then in `_find_subagent_meta_files`, add a cache read at the top (after computing `budget`):

```python
    key = (str(projects_root), sid)
    hit = _subagent_cache.get(key)
    if hit is not None and time.monotonic() < hit[0]:
        return list(hit[1]), True
```

and a cache write just before each `return sorted(found), complete`, storing **only complete results**:

```python
    result = sorted(found)
    if complete:
        _subagent_cache[key] = (time.monotonic() + _SUBAGENT_CACHE_TTL_S, list(result))
    return result, complete
```

Apply the same guarded write to the early `except OSError` return path, or restructure so there is a single exit — the implementer's choice, but a partial or errored result must never be cached.

- [ ] **Step 4: Run test to verify it passes**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 pytest server/tests/test_replay_subagent_discovery.py -v`

Expected: PASS, all six tests.

- [ ] **Step 5: Re-run the replay suite**

Run the same command as Task 1 Step 5. Expected: PASS.

If a test fails only when run alongside others, the cache is leaking between tests — add `replay._subagent_cache_clear()` to the affected test rather than weakening the cache.

- [ ] **Step 6: Commit**

```bash
git add server/sessions/replay.py server/tests/test_replay_subagent_discovery.py
git commit -m "perf(replay): memoize sub-agent sidecar discovery per session

Reattach and scrollback paging call transcript_window repeatedly; each call
re-paid the cross-cwd scan. Cache per (projects_root, sid) with a short TTL.
Partial (budget-truncated) results are never cached — caching a truncated
scan would make the degradation permanent.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 3: Bound `transcript_window` itself

Tasks 1-2 fix the known slow path. This task makes the *class* of failure impossible: whatever else replay grows later, opening a session must not hang the RPC. The spec's standing rule is that a transcript read must degrade on timeout rather than hang.

**Files:**
- Modify: `server/sessions/orchestrator.py:7011` (`transcript_window`)
- Test: `server/tests/test_transcript_window_bounded.py` (create)

**Interfaces:**
- Consumes: nothing from earlier tasks (independent defence in depth).
- Produces: `transcript_window` keeps its signature and its `ReplayWindow | None` return type. It gains the behaviour that a replay exceeding the ceiling returns `None` (the same value it already returns when no transcript exists) rather than blocking.

- [ ] **Step 1: Write the failing test**

Create `server/tests/test_transcript_window_bounded.py`:

```python
"""transcript_window must never hang (RC2, defence in depth).

Opening a session is not optional. If replay is slow for ANY reason, the RPC
must return — degraded — rather than burn the caller's 30s ceiling and 503.
"""
import asyncio
from unittest import mock

import pytest

from server.sessions.orchestrator import SessionOrchestrator


def _async_const(value):
    async def _fn(*a, **k):
        return value
    return _fn


@pytest.mark.asyncio
async def test_slow_replay_returns_instead_of_hanging(monkeypatch):
    from server.sessions import orchestrator as orch_mod
    from server.sessions import replay as replay_mod

    async def never_finishes(*a, **k):
        await asyncio.sleep(3600)

    # transcript_window imports replay_window INSIDE the function body, so the
    # name is resolved on the replay module at call time — patch it there, not
    # on the orchestrator module (patching orch_mod would have no effect).
    monkeypatch.setattr(replay_mod, "replay_window", never_finishes)
    monkeypatch.setattr(orch_mod, "_TRANSCRIPT_WINDOW_CEILING_S", 0.3)

    orch = SessionOrchestrator(sdk_factory=mock.MagicMock())
    monkeypatch.setattr(orch, "resolve_transcript_path",
                        _async_const("/tmp/does-not-matter.jsonl"))

    loop = asyncio.get_running_loop()
    started = loop.time()
    out = await orch.transcript_window("sess-1")
    elapsed = loop.time() - started

    assert out is None, "a timed-out replay must degrade to None, not raise"
    assert elapsed < 2.0, f"transcript_window blocked for {elapsed:.1f}s"
```

Two things this test depends on, both verified — do not "fix" them:
- **Patch `replay_mod.replay_window`, not `orch_mod`.** `transcript_window` does `from server.sessions.replay import replay_window` *inside the function*, so the lookup happens on the replay module at call time. Patching the orchestrator module would silently do nothing and the test would hang for the full 3600s sleep.
- **`SessionOrchestrator(sdk_factory=mock.MagicMock())` is the construction pattern** used by `server/tests/test_transcript_path_detached.py:27`. There is no `orchestrator` fixture in `server/tests/conftest.py`; do not go looking for one. `resolve_transcript_path` is stubbed so the test never touches the database.

- [ ] **Step 2: Run test to verify it fails**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 pytest server/tests/test_transcript_window_bounded.py -v`

Expected: FAIL — the test times out or the assertion on `elapsed` fires, because `transcript_window` currently awaits `replay_window` with no ceiling.

- [ ] **Step 3: Write minimal implementation**

Add near the other module constants in `server/sessions/orchestrator.py`:

```python
# Opening a session must not hang. Replay work that exceeds this degrades to "no
# transcript" rather than burning the caller's RPC timeout (RC2). Generous enough
# that a legitimately large transcript still renders.
_TRANSCRIPT_WINDOW_CEILING_S = float(
    os.environ.get("CCSWITCH_TRANSCRIPT_WINDOW_CEILING_S", "10"))
```

Then wrap the replay call in `transcript_window`:

```python
        try:
            return await asyncio.wait_for(
                replay_window(path, before=before, limit=limit),
                timeout=_TRANSCRIPT_WINDOW_CEILING_S,
            )
        except asyncio.TimeoutError:
            log.warning("session.transcript_window id=%s exceeded %.0fs — returning empty",
                        session_id, _TRANSCRIPT_WINDOW_CEILING_S)
            return None
        except Exception:
            log.warning("session.transcript_window id=%s replay failed", session_id, exc_info=True)
            return None
```

Keep the existing `except Exception → return None` behaviour; you are adding the timeout arm beside it, not replacing it. Confirm `os` and `asyncio` are already imported in `orchestrator.py` (they are) and that `log` is the module logger already in use there.

- [ ] **Step 4: Run test to verify it passes**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 pytest server/tests/test_transcript_window_bounded.py -v`

Expected: PASS.

- [ ] **Step 5: Run the transcript and orchestrator suites**

```bash
env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 pytest \
  server/tests/test_replay.py \
  server/tests/test_replay_subagent_discovery.py \
  server/tests/test_api_transcript_paging.py \
  server/tests/test_remote_transcript_rpc.py \
  server/tests/test_transcript_path_detached.py \
  server/tests/test_transcript_window_bounded.py -v
```

Expected: PASS.

- [ ] **Step 6: Run the full server suite once**

```bash
timeout 1500 env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 pytest server/tests/ -q 2>&1 | tail -20
```

Expected: no new failures. **Known pre-existing failure:** `test_library_api.py::test_skill_crud_roundtrip` fails on `origin/master` too in this checkout — it is not yours. Report any other failure explicitly.

- [ ] **Step 7: Commit**

```bash
git add server/sessions/orchestrator.py server/tests/test_transcript_window_bounded.py
git commit -m "fix(sessions): bound transcript_window so opening a session cannot hang

Defence in depth for RC2: whatever replay grows later, a slow path must
degrade to 'no transcript' rather than burn the caller's 30s RPC ceiling and
503. Opening a session is not optional.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

## Verification against the live fleet

This is the acceptance check. P2 is the plan that is supposed to make sessions openable, so prove it against the real daemon, not only in tests.

- [ ] Deploy to rocky and confirm the daemon starts cleanly.
- [ ] The RPC that hung must now return. Baseline to beat: **>35 s timeout, 5/5 attempts.**
  ```bash
  SID=<a real session id>
  C="--cert /etc/ccswitch/tls/client.pem --key /etc/ccswitch/tls/client.key --cacert /etc/ccswitch/tls/ca.pem"
  for i in 1 2 3; do
    curl -s -o /dev/null -w "  %{http_code} %{time_total}s\n" --max-time 30 $C \
      -X POST -H 'content-type: application/json' -d "{\"session_id\":\"$SID\",\"limit\":5}" \
      https://rocky.tail0dd570.ts.net:9443/rpc/transcript_window
  done
  ```
  Expected: `200` in single-digit seconds or less.
- [ ] The HTTP route that 503'd must now succeed. Baseline to beat: **503 after exactly 30 s, 5/5 attempts.**
  ```bash
  curl -s -o /dev/null -w "%{http_code} %{time_total}s\n" --max-time 40 \
    "http://127.0.0.1:8787/api/sessions/$SID/transcript"
  ```
- [ ] **Open a session in the browser and see a terminal.** This is the whole point of P2 — the dashboard already rendered correctly, but `/session/:id` showed the empty state because the transcript never arrived. Confirm the composer and transcript now mount.
- [ ] Confirm no `subagent scan budget exhausted` warnings in `journalctl -u ccswitch-daemon.service` under normal operation. If they appear constantly, the budget is too tight for the sshfs mount — raise `CCSWITCH_SUBAGENT_SCAN_BUDGET_S` rather than removing the bound.

**Note:** P2 does not fix the deadlocked queue — that is RC3 (finished sessions holding capacity slots, plus `manual-unarchive` as a permanent opt-out), planned separately as P4. After P2, sessions open but the 14 queued jobs still will not start.

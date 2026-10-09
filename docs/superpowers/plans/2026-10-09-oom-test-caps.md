# Stop session test/build runs from OOM-killing the desktop (dobby, 2026-10-09)

## Evidence

dobby (19 GiB RAM, 15 GiB swap) hit three global OOMs on 2026-10-09 (15:15, 16:01, 16:25 EDT).
The kernel killed Chrome, 1Password, solaar, verdaccio — small processes with a higher
`oom_score_adj` — while the memory was held by:

- `pytest server/tests -n auto` from a ViberPit session (6 workers via the dobby drop-in
  `PYTEST_XDIST_AUTO_NUM_WORKERS=6`, ~1.3–2 GB each) — 15:15.
- `pytest server/tests -n 6` from a Claude Code background job running OUTSIDE the daemon
  (no drop-in env, no slot) — 16:01 and 16:25.
- Repeated `dotnet build` / `dotnet test` from a ViberPit session (5–7 MSBuild nodes,
  ~4 GB) — 16:01, 16:25.
- On top of a 2.4–3.1 GB daemon process (tracked separately).

None of the runs used the #822 test-slot recipe: it is a hand-copied shell loop in
`core.md`, so it is skipped in practice.

## Task 1 — the server suite caps itself (server/tests/conftest.py)

In the xdist **controller** only (not workers: `hasattr(config, "workerinput")`), in a
`pytest_configure` hook (runs after xdist resolved `-n auto` → int in `pytest_cmdline_main`,
before nodes are spawned at session start):

1. **Memory clamp.** Pure policy function
   `clamp_workers(requested: int, mem_available_bytes: int | None, *, per_worker_bytes, reserve_bytes) -> int`
   — `None` (unreadable `/proc/meminfo`) → `requested`; otherwise
   `max(1, min(requested, (mem_available - reserve) // per_worker))`.
   Knobs: `VIBERPIT_PYTEST_WORKER_MB` (default 1536), `VIBERPIT_PYTEST_MEM_RESERVE_MB`
   (default 3072), `VIBERPIT_PYTEST_MEM_CLAMP=0` disables. Print one line when it clamps.
   CI (16 GB, 4 cores) is not clamped.
2. **Automatic test slot.** When the effective worker count is > 1, acquire the existing
   #822 slot (`POST http://127.0.0.1:8787/api/test-slots/acquire`, stdlib `urllib`,
   short timeout), polling every 15 s while `granted` is false and printing "waiting for
   test slot, N ahead". Fail-open on any error / `degraded` (CI has no edge). Release in
   `pytest_unconfigure` and via `atexit`. sessionId = `f"pytest-{host}-{pid}"`.
   Opt-out `VIBERPIT_TEST_SLOT=0`. Must not run inside the tests that exercise the API.
3. Update `.agents/instructions/core.md` "Test-run slot cap" bullet: the conftest now
   does it automatically; delete the hand-copied loop (single source of truth).

Tests: unit-test `clamp_workers` and the slot client (fake opener) in a new
`server/tests/test_pytest_resource_guard.py`; put the logic in an importable module
(e.g. `server/tests/_resource_guard.py`) that conftest calls.

## Task 2 — session process trees are the preferred OOM victim (memory_watch)

`MemoryWatch.sample()` already walks every subtree under the daemon each interval. Add:
for every member of each direct-child subtree whose `/proc/<pid>/oom_score_adj` is below
the target, write the target (raising is always permitted for own-uid processes; ignore
`OSError` — the process may be gone). Children inherit the value, so tool processes
spawned after the CLI root is marked inherit it with no further writes.

Target from `VIBERPIT_SESSION_OOM_SCORE_ADJ` (default 500; 0 disables). Never touch the
daemon's own pid. Inject the reader/writer like `scan`/`describe` so it is unit-tested
without `/proc`. Effect: a global OOM picks the runaway pytest/dotnet/node of a session
(score ≈ 500 + size) over Chrome renderers (300) and desktop apps (200).

## Task 3 — host (dobby), not in the PR

Lower the daemon's `MemoryHigh` from 13G to 10G (repo default) via
`systemctl set-property` — live, no restart; reclaim pressure starts earlier.

## Task 4 — follow-up issue

Daemon process RSS 2.4–3.1 GB within ~2 h of start — file an issue with the numbers.

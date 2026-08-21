# Fleet Registry, Dispatch & Visibility Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give ccswitch a real fleet layer — a host registry, health/capacity probing, an automatic host dispatcher with manual override and failover, and the UI to see and configure it all.

**Architecture:** The edge today resolves exactly **one** daemon endpoint (`sessiond_endpoint()` → a single `Endpoint`) and holds one `RemoteOrchestrator`. This plan replaces that with a `FleetRouter` holding a **map** of host name → `RemoteOrchestrator`, backed by a `hosts` table. A pure `rank_hosts()` function orders healthy hosts by capacity headroom; `create_session` picks the top-ranked host (or an explicit override), stamps `Session.host`, and fails over to the next host on failure. Host choice and account choice stay independent — `select_account` runs unchanged.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2 (async, Postgres + SQLite), Alembic, pytest/pytest-asyncio; React 18 + TypeScript + Vite + Tailwind/shadcn, vitest.

**Spec:** `docs/superpowers/specs/2026-08-20-multi-host-session-fleet-design.md` (sub-project 4, "Host registry + dispatch policy")

## Global Constraints

- **Constitution I:** never fork or patch the `claude` binary.
- **Constitution II:** account = `CLAUDE_CONFIG_DIR`, strict per-process isolation.
- **Constitution III:** verify against the real binary/API, not assumptions.
- **Constitution V:** TDD — every task writes a failing test first.
- **Constitution VI:** delete the path you replace in the same PR. No parallel single-host and multi-host dispatch implementations.
- **Split-brain guard is inviolable.** When the edge cannot reach a daemon it must **never** fall back to an in-process orchestrator over the same database. See `deploy/ccswitch.service.d/remote.conf` and the 2026-06-23 "Ready while working" incident. Failover means *another daemon*, never in-process.
- **Postgres schema:** new **nullable** columns and new **tables** are applied automatically by `_reconcile_columns` / `create_all` on every dialect (post-#717). Still add an Alembic revision in `server/db/migrations/versions/` as the reviewable record, chaining `down_revision` off the newest (`0005_session_archive_root`). A NOT NULL column without a synthesizable default needs a real migration.
- **Repo locality is explicitly NOT a dispatch input** (decision 2026-08-21: "ignore locality entirely"). `ensure_clone` is lazy and idempotent; a cold host clones on demand.
- **No account-to-host affinity** (spec decision 5). Every account is usable from every node.
- **Test env:** `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 pytest server/tests/…`. Postgres tests need `CCSWITCH_TEST_PG_URL` pointing at a database whose name contains `test`.
- **Lint/type gates:** `ruff check server/` and `mypy server/ --ignore-missing-imports` must both pass. Web: `vitest run`, `tsc --noEmit`, `eslint`.

---

## File Structure

**New files**

| File | Responsibility |
|---|---|
| `server/sessions/fleet.py` | Pure ranking: `HostCandidate`, `rank_hosts()`. No I/O, no DB — unit-testable with fabricated candidates. |
| `server/sessions/fleet_router.py` | `FleetRouter`: owns host name → `RemoteOrchestrator` map, resolves a session's orchestrator, performs failover. |
| `server/sessions/host_probe.py` | Periodic health/capacity probe writing `Host` rows. |
| `server/api/fleet.py` | `GET /api/fleet/hosts`, `PUT /api/fleet/hosts/{name}`. |
| `web/src/components/settings/FleetSection.tsx` | Settings UI for hosts (enable/disable, weight, capacity). |
| `web/src/components/HostBadge.tsx` | Small host indicator reused by `SessionCard` and the fleet list. |
| `server/db/migrations/versions/0006_fleet_hosts.py` | Alembic record for the `hosts` table + `sessions.host`. |

**Modified files**

| File | Change |
|---|---|
| `server/db/models.py` | Add `Host` model; add `Session.host`. |
| `server/ipc/daemon.py:411` | Extend `/info` with capacity fields. |
| `server/app.py:325-370` | Build a `FleetRouter` instead of a single `RemoteOrchestrator`. |
| `server/api/sessions.py:65` | `CreateSessionRequest.host`; stamp + failover in `create_session`. |
| `web/src/components/SessionCard.tsx` | Render `HostBadge`. |
| `web/src/components/settings/SettingsView.tsx` | Mount `FleetSection`. |

---

### Task 1: `Host` model, `Session.host`, and migration

**Files:**
- Modify: `server/db/models.py`
- Create: `server/db/migrations/versions/0006_fleet_hosts.py`
- Test: `server/tests/test_fleet_models.py`

**Interfaces:**
- Consumes: nothing (first task).
- Produces: `server.db.models.Host` with columns `name` (PK, str), `endpoint` (str, not null), `enabled` (bool, not null, default True), `weight` (int, not null, default 100), `max_sessions` (int|None), `healthy` (bool|None), `last_probe_at` (datetime|None), `last_error` (str|None), `sha` (str|None), `protocol` (int|None), `load_avg` (float|None), `mem_available_mb` (int|None), `active_sessions` (int|None). Also `Session.host: str | None`.

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_fleet_models.py
"""Host registry model + Session.host column."""
from __future__ import annotations

import asyncio


def test_host_row_roundtrips_with_defaults(tmp_path):
    from sqlalchemy import select as sa_select

    from server.db import db_session, init_db
    from server.db.models import Host

    async def run():
        await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / 'fleet.db'}")
        async with db_session() as db:
            db.add(Host(name="rocky", endpoint="https://rocky.tail0dd570.ts.net:9443"))
        async with db_session() as db:
            return (await db.execute(
                sa_select(Host).where(Host.name == "rocky"))).scalars().first()

    row = asyncio.run(run())
    assert row.enabled is True, "hosts are enabled by default"
    assert row.weight == 100, "default weight is 100"
    assert row.max_sessions is None
    assert row.healthy is None, "health is unknown until first probe"


def test_session_records_its_host(tmp_path):
    from sqlalchemy import select as sa_select

    from server.db import db_session, init_db
    from server.db.models import Session as DbSession

    async def run():
        await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / 'sess.db'}")
        async with db_session() as db:
            db.add(DbSession(id="s1", name="S", account_name="a", status="idle",
                             cwd="/tmp/s1", host="rocky"))
        async with db_session() as db:
            return (await db.execute(
                sa_select(DbSession).where(DbSession.id == "s1"))).scalars().first()

    assert asyncio.run(run()).host == "rocky"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 .venv/bin/python -m pytest server/tests/test_fleet_models.py -q`
Expected: FAIL — `ImportError: cannot import name 'Host' from 'server.db.models'`

- [ ] **Step 3: Add the model and column**

In `server/db/models.py`, add after the `Session` class:

```python
class Host(Base):
    """A session-executing host in the fleet.

    One row per daemon the edge may dispatch to. ``endpoint`` is the same string
    grammar ``server/ipc/paths.parse_endpoint`` accepts (``unix:///path`` or
    ``https://host:port``), so a single-host deployment is just a one-row fleet.

    The probe-written columns (healthy, load_avg, mem_available_mb,
    active_sessions, sha, protocol, last_probe_at, last_error) are all nullable:
    a host that has never been probed reports unknown, not false. Ranking treats
    unknown health as NOT dispatchable — never assume a host is up.
    """

    __tablename__ = "hosts"

    name: Mapped[str] = mapped_column(String, primary_key=True)
    endpoint: Mapped[str] = mapped_column(String, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # Operator-set relative preference; multiplies the computed score.
    weight: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    # Hard cap on concurrent sessions; NULL = uncapped.
    max_sessions: Mapped[int | None] = mapped_column(Integer, nullable=True, default=None)

    # --- probe-written state (all nullable = "not yet known") ---
    healthy: Mapped[bool | None] = mapped_column(Boolean, nullable=True, default=None)
    last_probe_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, default=None)
    last_error: Mapped[str | None] = mapped_column(String, nullable=True, default=None)
    sha: Mapped[str | None] = mapped_column(String, nullable=True, default=None)
    protocol: Mapped[int | None] = mapped_column(Integer, nullable=True, default=None)
    load_avg: Mapped[float | None] = mapped_column(Float, nullable=True, default=None)
    mem_available_mb: Mapped[int | None] = mapped_column(Integer, nullable=True, default=None)
    active_sessions: Mapped[int | None] = mapped_column(Integer, nullable=True, default=None)
```

In the `Session` class, beside `archive_root`, add:

```python
    # Host that ran this session, stamped at creation by the dispatcher. NULL for
    # sessions created before the fleet existed, and for single-host deployments
    # that never named their host. Never inferred at read time — the host a
    # session ran on is a fact about that session, not about current config.
    host: Mapped[str | None] = mapped_column(String, nullable=True, default=None)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 .venv/bin/python -m pytest server/tests/test_fleet_models.py -q`
Expected: PASS (2 passed)

- [ ] **Step 5: Add the Alembic revision**

```python
# server/db/migrations/versions/0006_fleet_hosts.py
"""add hosts registry table and sessions.host

Revision ID: 0006_fleet_hosts
Revises: 0005_session_archive_root
Create Date: 2026-08-21

Adds the ``hosts`` registry and one nullable column (``sessions.host``) recording
which host executed a session. ``create_all`` makes absent tables and
``_reconcile_columns`` ALTER-ADDs nullable columns on every dialect, so this
revision is the reviewable record rather than the only application path.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0006_fleet_hosts"
down_revision: Union[str, None] = "0005_session_archive_root"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "hosts",
        sa.Column("name", sa.String(), primary_key=True),
        sa.Column("endpoint", sa.String(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("weight", sa.Integer(), nullable=False, server_default="100"),
        sa.Column("max_sessions", sa.Integer(), nullable=True),
        sa.Column("healthy", sa.Boolean(), nullable=True),
        sa.Column("last_probe_at", sa.DateTime(), nullable=True),
        sa.Column("last_error", sa.String(), nullable=True),
        sa.Column("sha", sa.String(), nullable=True),
        sa.Column("protocol", sa.Integer(), nullable=True),
        sa.Column("load_avg", sa.Float(), nullable=True),
        sa.Column("mem_available_mb", sa.Integer(), nullable=True),
        sa.Column("active_sessions", sa.Integer(), nullable=True),
    )
    op.add_column("sessions", sa.Column("host", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("sessions", "host")
    op.drop_table("hosts")
```

- [ ] **Step 6: Verify lint and types**

Run: `.venv/bin/ruff check server/ && .venv/bin/python -m mypy server/ --ignore-missing-imports`
Expected: `All checks passed!` and `Success: no issues found`

- [ ] **Step 7: Commit**

```bash
git add server/db/models.py server/db/migrations/versions/0006_fleet_hosts.py server/tests/test_fleet_models.py
git commit -m "feat(fleet): add Host registry model and Session.host"
```

---

### Task 2: Pure host ranking

**Files:**
- Create: `server/sessions/fleet.py`
- Test: `server/tests/test_fleet_ranking.py`

**Interfaces:**
- Consumes: nothing at runtime (pure module).
- Produces: `HostCandidate` frozen dataclass with fields `name: str`, `enabled: bool`, `healthy: bool | None`, `weight: int`, `active_sessions: int`, `max_sessions: int | None`, `load_avg: float | None`, `mem_available_mb: int | None`; and `rank_hosts(candidates: list[HostCandidate]) -> list[str]` returning dispatchable host names, best first.

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_fleet_ranking.py
"""Pure host ranking — no DB, no network, no live hosts.

Locality is deliberately NOT an input (2026-08-21 decision: ignore locality
entirely). ensure_clone is lazy and idempotent, so a cold host clones on demand.
"""
from __future__ import annotations

import pytest

from server.sessions.fleet import HostCandidate, rank_hosts


def _c(name, **kw):
    base = dict(name=name, enabled=True, healthy=True, weight=100,
                active_sessions=0, max_sessions=None, load_avg=0.0,
                mem_available_mb=8192)
    base.update(kw)
    return HostCandidate(**base)


def test_unknown_health_is_not_dispatchable():
    """healthy=None means 'never probed', which must not be read as 'up'."""
    assert rank_hosts([_c("rocky", healthy=None)]) == []


def test_unhealthy_and_disabled_hosts_are_excluded():
    assert rank_hosts([_c("a", healthy=False), _c("b", enabled=False)]) == []


def test_host_at_capacity_is_excluded():
    assert rank_hosts([_c("full", max_sessions=2, active_sessions=2)]) == []
    assert rank_hosts([_c("room", max_sessions=2, active_sessions=1)]) == ["room"]


def test_more_free_memory_ranks_higher():
    ranked = rank_hosts([_c("small", mem_available_mb=1024),
                         _c("big", mem_available_mb=16384)])
    assert ranked[0] == "big"


def test_lower_load_ranks_higher():
    ranked = rank_hosts([_c("busy", load_avg=8.0), _c("idle", load_avg=0.1)])
    assert ranked[0] == "idle"


def test_weight_overrides_marginal_differences():
    """An operator pinning weight must be able to outrank a small metric edge."""
    ranked = rank_hosts([_c("preferred", weight=500, load_avg=1.0),
                         _c("default", weight=100, load_avg=0.9)])
    assert ranked[0] == "preferred"


def test_ties_break_by_name_for_determinism():
    ranked = rank_hosts([_c("zeta"), _c("alpha")])
    assert ranked == ["alpha", "zeta"], "identical hosts must order deterministically"


def test_empty_fleet_returns_empty():
    assert rank_hosts([]) == []


@pytest.mark.parametrize("missing", ["load_avg", "mem_available_mb"])
def test_missing_metrics_do_not_crash(missing):
    """A probed-but-partial host still ranks; it just scores conservatively."""
    assert rank_hosts([_c("partial", **{missing: None})]) == ["partial"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 .venv/bin/python -m pytest server/tests/test_fleet_ranking.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'server.sessions.fleet'`

- [ ] **Step 3: Write the implementation**

```python
# server/sessions/fleet.py
"""Host ranking for multi-host session dispatch.

Pure functions only — no DB, no network — so dispatch policy is unit-testable
without two live hosts (spec: "Dispatch logic must be unit testable without two
live hosts").

Repo locality is deliberately **not** an input. ``ensure_clone``
(``src/ccswitch/gitrepos.py``) is lazy and idempotent, so a cold host clones on
demand; filtering on locality would strand nearly every session on dobby and
defeat the offload goal (spec open question 1, resolved 2026-08-21).

Host choice and account choice are independent: spec decision 5 removed
account-to-host affinity, so ``select_account`` runs unchanged and this ranks
hosts as a separate, composable step.
"""
from __future__ import annotations

from dataclasses import dataclass

# Free memory at or above this is considered "plenty" — more does not score higher.
_MEM_SATURATION_MB = 4096
# Score weights; they sum to 1.0 so a host's base score is bounded by its weight.
_W_HEADROOM, _W_MEM, _W_LOAD = 0.5, 0.3, 0.2
# Used when a probe returned no value for a metric. Deliberately mid-range: an
# unmeasured host should neither win nor be effectively excluded.
_UNKNOWN_METRIC = 0.5

__all__ = ["HostCandidate", "rank_hosts", "score_host"]


@dataclass(frozen=True)
class HostCandidate:
    """A snapshot of one host's dispatchability. Built from a ``Host`` row."""

    name: str
    enabled: bool
    healthy: bool | None
    weight: int
    active_sessions: int
    max_sessions: int | None
    load_avg: float | None
    mem_available_mb: int | None


def _dispatchable(c: HostCandidate) -> bool:
    """True iff *c* may receive a new session right now.

    ``healthy is None`` (never probed) is NOT dispatchable. Treating unknown as
    up is how you dispatch into a host that has been down since boot.
    """
    if not c.enabled or c.healthy is not True:
        return False
    if c.max_sessions is not None and c.active_sessions >= c.max_sessions:
        return False
    return True


def score_host(c: HostCandidate) -> float:
    """Higher is better. Only meaningful for dispatchable candidates."""
    if c.max_sessions:
        free = max(0, c.max_sessions - c.active_sessions)
        headroom = free / c.max_sessions
    else:
        headroom = 1.0

    if c.mem_available_mb is None:
        mem = _UNKNOWN_METRIC
    else:
        mem = min(1.0, c.mem_available_mb / _MEM_SATURATION_MB)

    if c.load_avg is None:
        load = _UNKNOWN_METRIC
    else:
        load = 1.0 / (1.0 + max(0.0, c.load_avg))

    return c.weight * (_W_HEADROOM * headroom + _W_MEM * mem + _W_LOAD * load)


def rank_hosts(candidates: list[HostCandidate]) -> list[str]:
    """Return dispatchable host names, best first.

    Ties break by name so dispatch is deterministic and reproducible in tests.
    """
    usable = [c for c in candidates if _dispatchable(c)]
    usable.sort(key=lambda c: (-score_host(c), c.name))
    return [c.name for c in usable]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 .venv/bin/python -m pytest server/tests/test_fleet_ranking.py -q`
Expected: PASS (10 passed)

- [ ] **Step 5: Verify lint and types**

Run: `.venv/bin/ruff check server/sessions/fleet.py && .venv/bin/python -m mypy server/ --ignore-missing-imports`
Expected: both clean

- [ ] **Step 6: Commit**

```bash
git add server/sessions/fleet.py server/tests/test_fleet_ranking.py
git commit -m "feat(fleet): pure host ranking with capacity, memory and load"
```

---

### Task 3: Daemon reports capacity on `/info`

**Files:**
- Modify: `server/ipc/daemon.py:411-414`
- Test: `server/tests/test_daemon_capacity.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `GET /info` returns the existing `{"sha", "protocol", "epoch"}` **plus** `{"loadAvg": float, "memAvailableMb": int, "activeSessions": int}`.

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_daemon_capacity.py
"""/info carries the capacity signal the fleet ranker consumes.

Extending /info rather than adding a new endpoint keeps the probe a single
round-trip and means any daemon old enough to answer /info at all is visible to
the fleet — it just reports no metrics.
"""
from __future__ import annotations

from server.ipc.daemon import _capacity_snapshot


def test_capacity_snapshot_shape():
    snap = _capacity_snapshot(active_sessions=3)
    assert snap["activeSessions"] == 3
    assert isinstance(snap["loadAvg"], float)
    assert isinstance(snap["memAvailableMb"], int)
    assert snap["memAvailableMb"] > 0, "a running host has some available memory"
    assert snap["loadAvg"] >= 0.0


def test_capacity_snapshot_survives_unreadable_meminfo(monkeypatch):
    """A probe must degrade to None, never raise — an unreadable /proc must not
    take the daemon's /info down."""
    import server.ipc.daemon as d

    monkeypatch.setattr(d, "_read_mem_available_mb", lambda: None)
    snap = d._capacity_snapshot(active_sessions=0)
    assert snap["memAvailableMb"] is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 .venv/bin/python -m pytest server/tests/test_daemon_capacity.py -q`
Expected: FAIL — `ImportError: cannot import name '_capacity_snapshot'`

- [ ] **Step 3: Implement in `server/ipc/daemon.py`**

Add near the module's other helpers:

```python
def _read_mem_available_mb() -> int | None:
    """MemAvailable from /proc/meminfo, in MiB. None if unreadable.

    MemAvailable (not MemFree) is the kernel's own estimate of what a new
    workload can actually claim, which is the question dispatch is asking.
    """
    try:
        with open("/proc/meminfo", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) // 1024
    except Exception:  # pragma: no cover — unreadable /proc must not break /info
        return None
    return None


def _capacity_snapshot(active_sessions: int) -> dict:
    """Capacity fields for /info. Every value degrades to None, never raises."""
    try:
        load = float(os.getloadavg()[0])
    except Exception:  # pragma: no cover
        load = None
    return {
        "loadAvg": load,
        "memAvailableMb": _read_mem_available_mb(),
        "activeSessions": active_sessions,
    }
```

Ensure `import os` is present at module top. Then change the `/info` handler:

```python
    @app.get("/info")
    async def info() -> dict:
        from server.version import running_git_sha
        orch = app.state.orchestrator
        active = len(getattr(orch, "_records", {}) or {})
        return {"sha": running_git_sha(), "protocol": proto.PROTOCOL_VERSION,
                "epoch": _app_epoch, **_capacity_snapshot(active)}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 .venv/bin/python -m pytest server/tests/test_daemon_capacity.py -q`
Expected: PASS (2 passed)

- [ ] **Step 5: Confirm existing daemon tests still pass**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 .venv/bin/python -m pytest server/tests/ -q -k "daemon or ipc or protocol"`
Expected: PASS — `/info` gained keys, existing assertions read specific keys

- [ ] **Step 6: Commit**

```bash
git add server/ipc/daemon.py server/tests/test_daemon_capacity.py
git commit -m "feat(fleet): report load, memory and active sessions on daemon /info"
```

---

### Task 4: Host probe writes registry state

**Files:**
- Create: `server/sessions/host_probe.py`
- Test: `server/tests/test_host_probe.py`

**Interfaces:**
- Consumes: `server.db.models.Host` (Task 1), `/info` capacity fields (Task 3).
- Produces: `async def probe_host(name: str, endpoint: str, *, fetch: Callable[[str], Awaitable[dict]]) -> dict` returning the values written; and `async def probe_all(*, fetch) -> int` returning the number of hosts probed.

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_host_probe.py
"""The probe records health and capacity, and records FAILURE explicitly.

A probe that silently leaves stale values is worse than one that records the
error: ranking would keep dispatching to a host that died an hour ago.
"""
from __future__ import annotations

import asyncio


def _seed(tmp_path, *hosts):
    from server.db import db_session, init_db
    from server.db.models import Host

    async def go():
        await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / 'probe.db'}")
        async with db_session() as db:
            for h in hosts:
                db.add(h)
    return go


def test_successful_probe_records_health_and_capacity(tmp_path):
    from sqlalchemy import select as sa_select

    from server.db import db_session
    from server.db.models import Host
    from server.sessions.host_probe import probe_all

    async def fake_fetch(endpoint):
        return {"sha": "abc1234", "protocol": 1, "loadAvg": 1.5,
                "memAvailableMb": 2048, "activeSessions": 4}

    async def run():
        await _seed(tmp_path, Host(name="rocky", endpoint="https://rocky:9443"))()
        n = await probe_all(fetch=fake_fetch)
        async with db_session() as db:
            row = (await db.execute(
                sa_select(Host).where(Host.name == "rocky"))).scalars().first()
        return n, row

    n, row = asyncio.run(run())
    assert n == 1
    assert row.healthy is True
    assert row.sha == "abc1234"
    assert row.load_avg == 1.5
    assert row.mem_available_mb == 2048
    assert row.active_sessions == 4
    assert row.last_error is None
    assert row.last_probe_at is not None


def test_failed_probe_marks_unhealthy_and_records_the_error(tmp_path):
    from sqlalchemy import select as sa_select

    from server.db import db_session
    from server.db.models import Host
    from server.sessions.host_probe import probe_all

    async def failing_fetch(endpoint):
        raise ConnectionError("connection refused")

    async def run():
        await _seed(tmp_path, Host(name="rocky", endpoint="https://rocky:9443",
                                   healthy=True, load_avg=0.1))()
        await probe_all(fetch=failing_fetch)
        async with db_session() as db:
            return (await db.execute(
                sa_select(Host).where(Host.name == "rocky"))).scalars().first()

    row = asyncio.run(run())
    assert row.healthy is False, "a failed probe must flip health, not leave it stale"
    assert "connection refused" in (row.last_error or "")


def test_disabled_hosts_are_not_probed(tmp_path):
    from server.db.models import Host
    from server.sessions.host_probe import probe_all

    calls = []

    async def counting_fetch(endpoint):
        calls.append(endpoint)
        return {"sha": "x", "protocol": 1}

    async def run():
        await _seed(tmp_path, Host(name="off", endpoint="https://off:9443",
                                   enabled=False))()
        return await probe_all(fetch=counting_fetch)

    assert asyncio.run(run()) == 0
    assert calls == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 .venv/bin/python -m pytest server/tests/test_host_probe.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'server.sessions.host_probe'`

- [ ] **Step 3: Write the implementation**

```python
# server/sessions/host_probe.py
"""Probe each enabled host's /info and record health + capacity.

Failure is recorded explicitly (healthy=False, last_error set) rather than left
stale. Ranking reads these columns, so a stale "healthy" is a dispatch into a
dead host.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Awaitable, Callable

from sqlalchemy import select as sa_select, update as sa_update

from server.db import db_session
from server.db.models import Host

log = logging.getLogger(__name__)

Fetch = Callable[[str], Awaitable[dict]]

__all__ = ["probe_host", "probe_all", "default_fetch"]


async def default_fetch(endpoint: str) -> dict:
    """GET /info from a daemon endpoint, honouring mTLS for https endpoints."""
    from server.ipc.client import DaemonClient
    from server.ipc.paths import parse_endpoint

    ep = parse_endpoint(endpoint)
    client = DaemonClient(ep)
    try:
        return await client.info()
    finally:
        await client.aclose()


async def probe_host(name: str, endpoint: str, *, fetch: Fetch) -> dict:
    """Probe one host and write the outcome. Never raises."""
    values: dict = {"last_probe_at": datetime.utcnow()}
    try:
        info = await fetch(endpoint)
        values.update(
            healthy=True,
            last_error=None,
            sha=info.get("sha"),
            protocol=info.get("protocol"),
            load_avg=info.get("loadAvg"),
            mem_available_mb=info.get("memAvailableMb"),
            active_sessions=info.get("activeSessions"),
        )
    except Exception as exc:
        # Record the failure; do NOT leave the previous healthy=True in place.
        values.update(healthy=False, last_error=f"{type(exc).__name__}: {exc}"[:500])
        log.warning("fleet.probe_failed host=%s endpoint=%s: %s", name, endpoint, exc)

    async with db_session() as db:
        await db.execute(sa_update(Host).where(Host.name == name).values(**values))
    return values


async def probe_all(*, fetch: Fetch | None = None) -> int:
    """Probe every enabled host. Returns how many were probed."""
    use = fetch or default_fetch
    async with db_session() as db:
        rows = (await db.execute(
            sa_select(Host.name, Host.endpoint).where(Host.enabled.is_(True)))).all()
    for name, endpoint in rows:
        await probe_host(name, endpoint, fetch=use)
    return len(rows)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 .venv/bin/python -m pytest server/tests/test_host_probe.py -q`
Expected: PASS (3 passed)

- [ ] **Step 5: Confirm `DaemonClient.info()` exists**

Run: `grep -n "async def info" server/ipc/client.py`
Expected: a match. If absent, add it next to the existing `/info` call at `server/ipc/client.py:153`:

```python
    async def info(self) -> dict:
        """GET /info — daemon identity plus capacity."""
        resp = await self._http.get("/info")
        resp.raise_for_status()
        return resp.json()
```

- [ ] **Step 6: Commit**

```bash
git add server/sessions/host_probe.py server/tests/test_host_probe.py server/ipc/client.py
git commit -m "feat(fleet): probe host health and capacity into the registry"
```

---

### Task 5: `FleetRouter` — many orchestrators, never in-process fallback

**Files:**
- Create: `server/sessions/fleet_router.py`
- Test: `server/tests/test_fleet_router.py`

**Interfaces:**
- Consumes: `rank_hosts`/`HostCandidate` (Task 2), `Host` (Task 1).
- Produces: `class FleetRouter` with `def add(self, name: str, orch) -> None`, `def get(self, name: str)` (raises `UnknownHost`), `async def candidates(self) -> list[HostCandidate]`, `async def pick(self, *, prefer: str | None = None) -> list[str]` returning an ordered failover list, and `def names(self) -> list[str]`. Exceptions `UnknownHost` and `NoHostAvailable`.

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_fleet_router.py
"""FleetRouter maps host -> orchestrator and orders failover candidates.

The split-brain guard is the invariant under test: failover means ANOTHER
DAEMON, never an in-process orchestrator over the same database. That fallback
caused the 2026-06-23 "Ready while working" incident.
"""
from __future__ import annotations

import asyncio

import pytest


def _router_with(tmp_path, *hosts):
    from server.db import db_session, init_db
    from server.db.models import Host
    from server.sessions.fleet_router import FleetRouter

    async def go():
        await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / 'router.db'}")
        async with db_session() as db:
            for h in hosts:
                db.add(h)
        r = FleetRouter()
        for h in hosts:
            r.add(h.name, object())
        return r
    return go


def test_pick_orders_by_rank(tmp_path):
    from server.db.models import Host

    async def run():
        r = await _router_with(
            tmp_path,
            Host(name="busy", endpoint="unix:///a", healthy=True, load_avg=9.0),
            Host(name="idle", endpoint="unix:///b", healthy=True, load_avg=0.1),
        )()
        return await r.pick()

    assert asyncio.run(run())[0] == "idle"


def test_pick_honours_an_explicit_preference(tmp_path):
    """Manual override puts the chosen host first but keeps the rest as failover."""
    from server.db.models import Host

    async def run():
        r = await _router_with(
            tmp_path,
            Host(name="busy", endpoint="unix:///a", healthy=True, load_avg=9.0),
            Host(name="idle", endpoint="unix:///b", healthy=True, load_avg=0.1),
        )()
        return await r.pick(prefer="busy")

    order = asyncio.run(run())
    assert order[0] == "busy", "explicit choice wins"
    assert "idle" in order, "other hosts remain available for failover"


def test_pick_raises_when_no_host_is_dispatchable(tmp_path):
    from server.db.models import Host
    from server.sessions.fleet_router import NoHostAvailable

    async def run():
        r = await _router_with(
            tmp_path, Host(name="down", endpoint="unix:///a", healthy=False))()
        return await r.pick()

    with pytest.raises(NoHostAvailable):
        asyncio.run(run())


def test_get_unknown_host_raises(tmp_path):
    from server.db.models import Host
    from server.sessions.fleet_router import UnknownHost

    async def run():
        return await _router_with(
            tmp_path, Host(name="rocky", endpoint="unix:///a", healthy=True))()

    r = asyncio.run(run())
    with pytest.raises(UnknownHost):
        r.get("nope")


def test_router_never_exposes_an_in_process_fallback():
    """Guard against a future edit reintroducing the split-brain path."""
    import inspect

    import server.sessions.fleet_router as mod

    src = inspect.getsource(mod)
    assert "SessionOrchestrator(" not in src, (
        "FleetRouter must never construct an in-process orchestrator — that is "
        "the 2026-06-23 split-brain incident")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 .venv/bin/python -m pytest server/tests/test_fleet_router.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'server.sessions.fleet_router'`

- [ ] **Step 3: Write the implementation**

```python
# server/sessions/fleet_router.py
"""Route a session to a host's orchestrator.

Holds a map of host name -> RemoteOrchestrator. Every entry is a REMOTE
orchestrator talking to a daemon; this module must never construct an
in-process SessionOrchestrator. When no host is reachable the correct outcome is
NoHostAvailable, not a local fallback that would run a second orchestrator over
the shared database (the 2026-06-23 "Ready while working" incident).
"""
from __future__ import annotations

import logging

from sqlalchemy import select as sa_select

from server.db import db_session
from server.db.models import Host
from server.sessions.fleet import HostCandidate, rank_hosts

log = logging.getLogger(__name__)

__all__ = ["FleetRouter", "UnknownHost", "NoHostAvailable"]


class UnknownHost(Exception):
    """Raised when a caller names a host the router does not hold."""


class NoHostAvailable(Exception):
    """Raised when no host is enabled, healthy and under capacity."""


class FleetRouter:
    """Owns one remote orchestrator per fleet host."""

    def __init__(self) -> None:
        self._orchs: dict[str, object] = {}

    def add(self, name: str, orch: object) -> None:
        self._orchs[name] = orch

    def names(self) -> list[str]:
        return sorted(self._orchs)

    def get(self, name: str):
        try:
            return self._orchs[name]
        except KeyError:
            raise UnknownHost(
                f"no orchestrator for host {name!r}; known hosts: {self.names()}"
            ) from None

    async def candidates(self) -> list[HostCandidate]:
        """Build ranking inputs from the registry, limited to hosts we can reach."""
        async with db_session() as db:
            rows = (await db.execute(sa_select(Host))).scalars().all()
        return [
            HostCandidate(
                name=r.name,
                enabled=r.enabled,
                healthy=r.healthy,
                weight=r.weight,
                active_sessions=r.active_sessions or 0,
                max_sessions=r.max_sessions,
                load_avg=r.load_avg,
                mem_available_mb=r.mem_available_mb,
            )
            for r in rows
            if r.name in self._orchs
        ]

    async def pick(self, *, prefer: str | None = None) -> list[str]:
        """Return hosts to try, best first.

        *prefer* moves an explicitly chosen host to the front but does not drop
        the others: a manual choice should still fail over rather than fail.
        """
        order = rank_hosts(await self.candidates())
        if prefer:
            if prefer not in self._orchs:
                raise UnknownHost(
                    f"no orchestrator for host {prefer!r}; known hosts: {self.names()}")
            order = [prefer] + [h for h in order if h != prefer]
        if not order:
            raise NoHostAvailable(
                "no enabled, healthy host with capacity; refusing to run in-process "
                "to avoid a split-brain over the shared database")
        return order
```

- [ ] **Step 4: Run test to verify it passes**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 .venv/bin/python -m pytest server/tests/test_fleet_router.py -q`
Expected: PASS (5 passed)

- [ ] **Step 5: Commit**

```bash
git add server/sessions/fleet_router.py server/tests/test_fleet_router.py
git commit -m "feat(fleet): FleetRouter with ranked failover and no in-process fallback"
```

---

### Task 6: Seed the registry and build the router at startup

**Files:**
- Modify: `server/app.py:325-370`
- Test: `server/tests/test_fleet_bootstrap.py`

**Interfaces:**
- Consumes: `FleetRouter` (Task 5), `Host` (Task 1).
- Produces: `app.state.fleet: FleetRouter`; `async def ensure_default_host(endpoint: str) -> str` in `server/sessions/fleet_router.py` returning the seeded host name.

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_fleet_bootstrap.py
"""A single-host deployment must become a one-row fleet automatically.

Spec rollback requirement: "#4 degrades to single-host when the registry has one
entry." Nobody should have to hand-populate a table to keep working.
"""
from __future__ import annotations

import asyncio


def test_existing_endpoint_is_seeded_as_a_host(tmp_path):
    from sqlalchemy import select as sa_select

    from server.db import db_session, init_db
    from server.db.models import Host
    from server.sessions.fleet_router import ensure_default_host

    async def run():
        await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / 'boot.db'}")
        name = await ensure_default_host("unix:///run/ccswitch/sessiond.sock")
        async with db_session() as db:
            rows = (await db.execute(sa_select(Host))).scalars().all()
        return name, rows

    name, rows = asyncio.run(run())
    assert len(rows) == 1
    assert rows[0].endpoint == "unix:///run/ccswitch/sessiond.sock"
    assert rows[0].name == name
    assert rows[0].enabled is True


def test_seeding_is_idempotent(tmp_path):
    from sqlalchemy import func, select as sa_select

    from server.db import db_session, init_db
    from server.db.models import Host
    from server.sessions.fleet_router import ensure_default_host

    async def run():
        await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / 'boot2.db'}")
        await ensure_default_host("unix:///run/ccswitch/sessiond.sock")
        await ensure_default_host("unix:///run/ccswitch/sessiond.sock")
        async with db_session() as db:
            return (await db.execute(
                sa_select(func.count()).select_from(Host))).scalar()

    assert asyncio.run(run()) == 1, "re-seeding must not duplicate the host"


def test_seeding_does_not_disturb_an_existing_registry(tmp_path):
    from sqlalchemy import func, select as sa_select

    from server.db import db_session, init_db
    from server.db.models import Host
    from server.sessions.fleet_router import ensure_default_host

    async def run():
        await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / 'boot3.db'}")
        async with db_session() as db:
            db.add(Host(name="rocky", endpoint="https://rocky:9443"))
        await ensure_default_host("unix:///run/ccswitch/sessiond.sock")
        async with db_session() as db:
            return (await db.execute(
                sa_select(func.count()).select_from(Host))).scalar()

    assert asyncio.run(run()) == 1, "an operator-managed fleet must not gain a row"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 .venv/bin/python -m pytest server/tests/test_fleet_bootstrap.py -q`
Expected: FAIL — `ImportError: cannot import name 'ensure_default_host'`

- [ ] **Step 3: Add `ensure_default_host` to `server/sessions/fleet_router.py`**

```python
# Name given to the host seeded from a pre-fleet single-endpoint config.
DEFAULT_HOST_NAME = "local"


async def ensure_default_host(endpoint: str) -> str:
    """Seed a one-row fleet from a legacy single-endpoint config.

    No-ops when ANY host already exists: once an operator manages the registry,
    startup must not inject rows behind their back.
    """
    from sqlalchemy import func

    async with db_session() as db:
        existing = (await db.execute(
            sa_select(func.count()).select_from(Host))).scalar()
        if existing:
            return DEFAULT_HOST_NAME
        db.add(Host(name=DEFAULT_HOST_NAME, endpoint=endpoint, enabled=True))
    return DEFAULT_HOST_NAME
```

Add `DEFAULT_HOST_NAME` and `ensure_default_host` to `__all__`.

- [ ] **Step 4: Run test to verify it passes**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 .venv/bin/python -m pytest server/tests/test_fleet_bootstrap.py -q`
Expected: PASS (3 passed)

- [ ] **Step 5: Wire the router in `server/app.py`**

Replace the single-orchestrator assignment (currently `app.state.sessions = RemoteOrchestrator(DaemonClient(endpoint))`) with fleet construction, keeping `app.state.sessions` pointing at the **top-ranked** host so every existing caller keeps working:

```python
    if orchestrator is None and endpoint is not None:
        from server.ipc.client import DaemonClient
        from server.sessions.fleet_router import FleetRouter, ensure_default_host
        from server.sessions.remote import RemoteOrchestrator
        client_ssl = None
        if endpoint.is_tcp:
            from server.ipc.tls import client_ssl_context, client_tls_files
            client_ssl = client_ssl_context(client_tls_files())
        app.state.remote_mode = True
        app.state.daemon_endpoint = endpoint
        app.state.daemon_socket = endpoint.path if endpoint.is_unix else None
        app.state.daemon_client_ssl = client_ssl
        app.state.orchestrator = None

        # Build the fleet from the registry, seeding it from this endpoint the
        # first time so a single-host deployment needs no manual setup.
        from server.ipc.paths import parse_endpoint
        from sqlalchemy import select as _sa_select

        from server.db import db_session as _db_session
        from server.db.models import Host as _Host

        await ensure_default_host(str(endpoint))
        fleet = FleetRouter()
        async with _db_session() as _db:
            _rows = (await _db.execute(
                _sa_select(_Host).where(_Host.enabled.is_(True)))).scalars().all()
        for _row in _rows:
            fleet.add(_row.name, RemoteOrchestrator(DaemonClient(parse_endpoint(_row.endpoint))))
        app.state.fleet = fleet
        # Default orchestrator for callers that do not route per-session.
        app.state.sessions = fleet.get(fleet.names()[0]) if fleet.names() else \
            RemoteOrchestrator(DaemonClient(endpoint))
```

Note: the surrounding function must be `async` for these awaits. If it is not, move the fleet build into the existing startup/lifespan hook and set `app.state.fleet` there.

- [ ] **Step 6: Run the app tests**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 .venv/bin/python -m pytest server/tests/ -q -k "app or remote_mode or startup"`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add server/app.py server/sessions/fleet_router.py server/tests/test_fleet_bootstrap.py
git commit -m "feat(fleet): seed the registry and build a FleetRouter at startup"
```

---

### Task 7: Dispatch on create, with override and failover

**Files:**
- Modify: `server/api/sessions.py:65-76` (request model), `:360-…` (`create_session`)
- Test: `server/tests/test_fleet_dispatch.py`

**Interfaces:**
- Consumes: `FleetRouter.pick` (Task 5), `Session.host` (Task 1).
- Produces: `CreateSessionRequest.host: str | None`; `create_session` stamps `Session.host` and fails over. New HTTP 503 body `{"code": "no_host_available", "message": ...}`.

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_fleet_dispatch.py
"""create_session picks a host, records it, and fails over.

Host choice and account choice are independent (spec decision 5): select_account
is untouched by this path.
"""
from __future__ import annotations

import asyncio

import pytest

from server.sessions.fleet_router import FleetRouter, NoHostAvailable


class _FakeOrch:
    def __init__(self, name, fail=False):
        self.name = name
        self.fail = fail
        self.calls = 0

    async def create_session(self, **kw):
        self.calls += 1
        if self.fail:
            raise ConnectionError(f"{self.name} is down")
        return {"id": f"sess-on-{self.name}"}


async def _dispatch(router, order_fn, **kw):
    from server.api.sessions import _create_on_fleet
    return await _create_on_fleet(router, order_fn, **kw)


def test_uses_the_top_ranked_host():
    r = FleetRouter()
    good = _FakeOrch("idle")
    r.add("idle", good)

    async def order():
        return ["idle"]

    out, host = asyncio.run(_dispatch(r, order))
    assert host == "idle"
    assert out["id"] == "sess-on-idle"
    assert good.calls == 1


def test_fails_over_to_the_next_host():
    r = FleetRouter()
    dead, alive = _FakeOrch("rocky", fail=True), _FakeOrch("dobby")
    r.add("rocky", dead)
    r.add("dobby", alive)

    async def order():
        return ["rocky", "dobby"]

    out, host = asyncio.run(_dispatch(r, order))
    assert host == "dobby", "must fall over to the next daemon"
    assert out["id"] == "sess-on-dobby"
    assert dead.calls == 1 and alive.calls == 1


def test_raises_when_every_host_fails():
    r = FleetRouter()
    r.add("a", _FakeOrch("a", fail=True))
    r.add("b", _FakeOrch("b", fail=True))

    async def order():
        return ["a", "b"]

    with pytest.raises(NoHostAvailable):
        asyncio.run(_dispatch(r, order))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 .venv/bin/python -m pytest server/tests/test_fleet_dispatch.py -q`
Expected: FAIL — `ImportError: cannot import name '_create_on_fleet'`

- [ ] **Step 3: Implement in `server/api/sessions.py`**

Add `host` to the request model:

```python
class CreateSessionRequest(BaseModel):
    name: str | None = None
    account: str | None = None
    initialPrompt: str | None = None
    repo: str | None = None  # nameWithOwner, e.g. "owner/name"
    branch: str | None = None
    permissionMode: str | None = None
    attachmentIds: list[str] | None = None
    model: str | None = None
    # Explicit host override. None => the dispatcher ranks and chooses. An
    # explicit host is tried first but still fails over (see _create_on_fleet).
    host: str | None = None
```

Add the dispatch helper near the other module helpers:

```python
async def _create_on_fleet(router, order_fn, **kwargs) -> tuple[dict, str]:
    """Create a session on the first host that accepts it.

    Returns (result, host_name). Tries hosts in *order_fn* order and moves on
    when one raises. Exhausting the list raises NoHostAvailable — it never falls
    back to an in-process orchestrator, which would run a second orchestrator
    over the shared database (2026-06-23 split-brain incident).
    """
    from server.sessions.fleet_router import NoHostAvailable

    order = await order_fn()
    errors: list[str] = []
    for host in order:
        try:
            return await router.get(host).create_session(**kwargs), host
        except Exception as exc:
            errors.append(f"{host}: {type(exc).__name__}: {exc}")
            log.warning("fleet.dispatch_failed host=%s — trying next: %s", host, exc)
    raise NoHostAvailable(
        "every host refused the session: " + "; ".join(errors))
```

In `create_session`, replace the direct orchestrator call with the fleet path, stamp the host on the row, and map the failure:

```python
    fleet = getattr(request.app.state, "fleet", None)
    if fleet is not None:
        from server.sessions.fleet_router import NoHostAvailable, UnknownHost
        try:
            result, chosen_host = await _create_on_fleet(
                fleet, lambda: fleet.pick(prefer=body.host), **create_kwargs)
        except UnknownHost as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                                detail={"code": "unknown_host", "message": str(exc)})
        except NoHostAvailable as exc:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                                detail={"code": "no_host_available",
                                        "message": str(exc)})
        async with db_session() as db:
            from sqlalchemy import update as sa_update
            await db.execute(sa_update(DbSession)
                             .where(DbSession.id == result["id"])
                             .values(host=chosen_host))
```

Replace `create_kwargs` with the argument dict the existing call already builds — do not introduce a second construction of it (Constitution VI).

- [ ] **Step 4: Run test to verify it passes**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 .venv/bin/python -m pytest server/tests/test_fleet_dispatch.py -q`
Expected: PASS (3 passed)

- [ ] **Step 5: Run the session API suite**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 .venv/bin/python -m pytest server/tests/ -q -k "sessions or api"`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add server/api/sessions.py server/tests/test_fleet_dispatch.py
git commit -m "feat(fleet): dispatch sessions across hosts with override and failover"
```

---

### Task 8: Fleet API + host on the session payload

**Files:**
- Create: `server/api/fleet.py`
- Modify: `server/app.py` (register router), `server/api/sessions.py` (session wire dict)
- Test: `server/tests/test_fleet_api.py`

**Interfaces:**
- Consumes: `Host` (Task 1), `probe_all` (Task 4).
- Produces: `GET /api/fleet/hosts` → `{"hosts": [{name, endpoint, enabled, weight, maxSessions, healthy, activeSessions, loadAvg, memAvailableMb, sha, lastProbeAt, lastError}]}`; `PUT /api/fleet/hosts/{name}` accepting `{enabled?, weight?, maxSessions?}`; session dicts gain `"host"`.

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_fleet_api.py
"""Fleet endpoints expose the registry and let an operator tune it."""
from __future__ import annotations

import asyncio


def test_host_to_wire_shape():
    from datetime import datetime

    from server.api.fleet import host_to_wire
    from server.db.models import Host

    h = Host(name="rocky", endpoint="https://rocky:9443", enabled=True, weight=100,
             max_sessions=4, healthy=True, load_avg=1.25, mem_available_mb=2048,
             active_sessions=2, sha="abc1234", protocol=1,
             last_probe_at=datetime(2026, 8, 21, 12, 0, 0), last_error=None)
    w = host_to_wire(h)
    assert w["name"] == "rocky"
    assert w["maxSessions"] == 4
    assert w["memAvailableMb"] == 2048
    assert w["healthy"] is True
    assert isinstance(w["lastProbeAt"], str), "datetime must serialize for JSON"


def test_patch_rejects_unknown_fields():
    """Only the three operator-tunable fields may be written; probe-owned state
    must never be settable through the API."""
    from server.api.fleet import sanitize_patch

    assert sanitize_patch({"enabled": False, "weight": 50, "maxSessions": 3}) == {
        "enabled": False, "weight": 50, "max_sessions": 3}
    assert sanitize_patch({"healthy": True, "loadAvg": 0.0, "sha": "x"}) == {}


def test_weight_is_clamped_to_a_sane_range():
    from server.api.fleet import sanitize_patch

    assert sanitize_patch({"weight": -5})["weight"] == 1
    assert sanitize_patch({"weight": 10_000})["weight"] == 1000
```

- [ ] **Step 2: Run test to verify it fails**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 .venv/bin/python -m pytest server/tests/test_fleet_api.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'server.api.fleet'`

- [ ] **Step 3: Write the implementation**

```python
# server/api/fleet.py
"""Fleet registry endpoints: list hosts, tune the operator-owned fields."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select as sa_select, update as sa_update

from server.api.auth import current_identity
from server.db import db_session
from server.db.models import Host

router = APIRouter(prefix="/api/fleet", tags=["fleet"])

# Only these three are operator-settable. Everything else on Host is written by
# the probe; letting the API set `healthy` would let the UI lie to the ranker.
_PATCHABLE = {"enabled": "enabled", "weight": "weight", "maxSessions": "max_sessions"}
_WEIGHT_MIN, _WEIGHT_MAX = 1, 1000


class HostPatch(BaseModel):
    enabled: bool | None = None
    weight: int | None = None
    maxSessions: int | None = None


def host_to_wire(h: Host) -> dict:
    """Serialize a Host row for the API (datetimes → ISO strings)."""
    return {
        "name": h.name,
        "endpoint": h.endpoint,
        "enabled": h.enabled,
        "weight": h.weight,
        "maxSessions": h.max_sessions,
        "healthy": h.healthy,
        "activeSessions": h.active_sessions,
        "loadAvg": h.load_avg,
        "memAvailableMb": h.mem_available_mb,
        "sha": h.sha,
        "protocol": h.protocol,
        "lastProbeAt": h.last_probe_at.isoformat() if h.last_probe_at else None,
        "lastError": h.last_error,
    }


def sanitize_patch(body: dict) -> dict:
    """Keep only operator-settable fields, mapped to column names and clamped."""
    out: dict = {}
    for wire, col in _PATCHABLE.items():
        if body.get(wire) is None:
            continue
        val = body[wire]
        if col == "weight":
            val = max(_WEIGHT_MIN, min(_WEIGHT_MAX, int(val)))
        out[col] = val
    return out


@router.get("/hosts")
async def list_hosts(identity: str = Depends(current_identity)) -> dict:
    async with db_session() as db:
        rows = (await db.execute(sa_select(Host).order_by(Host.name))).scalars().all()
    return {"hosts": [host_to_wire(r) for r in rows]}


@router.put("/hosts/{name}")
async def update_host(name: str, body: HostPatch,
                      identity: str = Depends(current_identity)) -> dict:
    values = sanitize_patch(body.model_dump())
    if not values:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail={"code": "no_writable_fields",
                                    "message": "nothing settable in this request"})
    async with db_session() as db:
        res = await db.execute(sa_update(Host).where(Host.name == name).values(**values))
        if res.rowcount == 0:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                                detail={"code": "unknown_host", "message": name})
        row = (await db.execute(
            sa_select(Host).where(Host.name == name))).scalars().first()
    return host_to_wire(row)
```

Register it in `server/app.py` beside the other routers:

```python
    from server.api.fleet import router as fleet_router
    app.include_router(fleet_router)
```

Add `"host": row.host` to the session wire dict in `server/api/sessions.py` wherever the session payload is assembled (search for the dict containing `"repo"` and `"branch"`).

- [ ] **Step 4: Run test to verify it passes**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 .venv/bin/python -m pytest server/tests/test_fleet_api.py -q`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add server/api/fleet.py server/app.py server/api/sessions.py server/tests/test_fleet_api.py
git commit -m "feat(fleet): fleet registry API and host on the session payload"
```

---

### Task 9: Periodic probing wired into the app

**Files:**
- Modify: `server/app.py` (startup task), `server/sessions/host_probe.py`
- Test: `server/tests/test_probe_loop.py`

**Interfaces:**
- Consumes: `probe_all` (Task 4).
- Produces: `async def probe_loop(interval_s: float = 30.0, *, fetch=None, stop=None) -> None`.

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_probe_loop.py
"""The probe loop keeps running when a tick raises.

A loop that dies on one bad probe silently freezes the whole registry, and stale
health is what causes dispatch into a dead host.
"""
from __future__ import annotations

import asyncio


def test_loop_survives_a_failing_tick(monkeypatch):
    import server.sessions.host_probe as hp

    calls = {"n": 0}

    async def flaky(*, fetch=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("db hiccup")
        return 1

    monkeypatch.setattr(hp, "probe_all", flaky)

    async def run():
        stop = asyncio.Event()

        async def stopper():
            while calls["n"] < 3:
                await asyncio.sleep(0.01)
            stop.set()

        await asyncio.gather(
            hp.probe_loop(interval_s=0.01, stop=stop),
            stopper(),
        )

    asyncio.run(asyncio.wait_for(run(), timeout=5))
    assert calls["n"] >= 3, "loop must continue past the raising tick"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 .venv/bin/python -m pytest server/tests/test_probe_loop.py -q`
Expected: FAIL — `AttributeError: module 'server.sessions.host_probe' has no attribute 'probe_loop'`

- [ ] **Step 3: Add the loop to `server/sessions/host_probe.py`**

```python
async def probe_loop(interval_s: float = 30.0, *, fetch: Fetch | None = None,
                     stop: "asyncio.Event | None" = None) -> None:
    """Probe every enabled host forever, every *interval_s* seconds.

    A raising tick is logged and swallowed: one bad probe must never freeze the
    registry, because stale health is what makes the ranker dispatch into a host
    that is already down.
    """
    import asyncio

    while not (stop and stop.is_set()):
        try:
            await probe_all(fetch=fetch)
        except Exception:
            log.exception("fleet.probe_tick_failed")
        try:
            await asyncio.sleep(interval_s)
        except asyncio.CancelledError:
            return
```

Add `"probe_loop"` to `__all__`.

- [ ] **Step 4: Run test to verify it passes**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 .venv/bin/python -m pytest server/tests/test_probe_loop.py -q`
Expected: PASS

- [ ] **Step 5: Start it from the app's existing startup hook**

In the same place other background tasks are spawned in `server/app.py`:

```python
    if getattr(app.state, "fleet", None) is not None:
        from server.sessions.host_probe import probe_loop
        app.state.fleet_probe_task = asyncio.create_task(
            probe_loop(), name="fleet-host-probe")
```

Cancel it in the shutdown path beside the other task cancellations.

- [ ] **Step 6: Commit**

```bash
git add server/sessions/host_probe.py server/app.py server/tests/test_probe_loop.py
git commit -m "feat(fleet): run the host probe on a background loop"
```

---

### Task 10: Host badge on session cards

**Files:**
- Create: `web/src/components/HostBadge.tsx`, `web/src/components/__tests__/HostBadge.test.tsx`
- Modify: `web/src/components/SessionCard.tsx`

**Interfaces:**
- Consumes: `host` on the session payload (Task 8).
- Produces: `export function HostBadge({ host, healthy }: { host?: string | null; healthy?: boolean | null })`.

- [ ] **Step 1: Write the failing test**

```tsx
// web/src/components/__tests__/HostBadge.test.tsx
import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { HostBadge } from '../HostBadge'

describe('HostBadge', () => {
  it('renders the host name', () => {
    render(<HostBadge host="rocky" healthy />)
    expect(screen.getByText('rocky')).toBeInTheDocument()
  })

  it('renders nothing when the session has no host', () => {
    // Pre-fleet sessions have host=null; showing "unknown" would be noise on
    // every historical card.
    const { container } = render(<HostBadge host={null} />)
    expect(container).toBeEmptyDOMElement()
  })

  it('marks an unhealthy host for the screen reader, not just by colour', () => {
    render(<HostBadge host="rocky" healthy={false} />)
    expect(screen.getByLabelText(/rocky.*unhealthy/i)).toBeInTheDocument()
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run (in `web/`): `npx vitest run src/components/__tests__/HostBadge.test.tsx`
Expected: FAIL — cannot resolve `../HostBadge`

- [ ] **Step 3: Write the component**

```tsx
// web/src/components/HostBadge.tsx
/**
 * Which fleet host is running a session.
 *
 * Renders nothing when `host` is null: sessions created before the fleet
 * existed carry no host, and an "unknown" chip on every historical card is
 * noise rather than information.
 */
export function HostBadge({ host, healthy }: { host?: string | null; healthy?: boolean | null }) {
  if (!host) return null

  const unhealthy = healthy === false
  const label = unhealthy ? `${host} — host unhealthy` : `host ${host}`

  return (
    <span
      aria-label={label}
      title={label}
      className={
        'inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-xs font-medium ' +
        (unhealthy
          ? 'bg-red-100 text-red-800 dark:bg-red-950 dark:text-red-300'
          : 'bg-slate-100 text-slate-700 dark:bg-slate-800 dark:text-slate-300')
      }
    >
      <span aria-hidden="true">{unhealthy ? '⚠' : '▪'}</span>
      {host}
    </span>
  )
}
```

- [ ] **Step 4: Run test to verify it passes**

Run (in `web/`): `npx vitest run src/components/__tests__/HostBadge.test.tsx`
Expected: PASS (3 passed)

- [ ] **Step 5: Render it in `SessionCard.tsx`**

Import it and place it in the metadata row beside the existing repo/branch chips:

```tsx
import { HostBadge } from './HostBadge'
// …in the chip row, beside repo/branch:
<HostBadge host={session.host} healthy={session.hostHealthy} />
```

Add `host?: string | null` to the session TypeScript type wherever repo/branch are declared.

- [ ] **Step 6: Verify the web gates**

Run (in `web/`): `npx vitest run && npx tsc --noEmit && npx eslint src/components/HostBadge.tsx`
Expected: all pass

- [ ] **Step 7: Commit**

```bash
git add web/src/components/HostBadge.tsx web/src/components/__tests__/HostBadge.test.tsx web/src/components/SessionCard.tsx
git commit -m "feat(fleet): show the executing host on session cards"
```

---

### Task 11: Fleet settings section

**Files:**
- Create: `web/src/components/settings/FleetSection.tsx`, `web/src/components/settings/__tests__/FleetSection.test.tsx`
- Modify: `web/src/components/settings/SettingsView.tsx`

**Interfaces:**
- Consumes: `GET /api/fleet/hosts`, `PUT /api/fleet/hosts/{name}` (Task 8).
- Produces: `export function FleetSection()`.

- [ ] **Step 1: Write the failing test**

```tsx
// web/src/components/settings/__tests__/FleetSection.test.tsx
import { render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it, vi, beforeEach } from 'vitest'

import { FleetSection } from '../FleetSection'

const hosts = [
  { name: 'dobby', endpoint: 'unix:///run/ccswitch/sessiond.sock', enabled: true,
    weight: 100, maxSessions: null, healthy: true, activeSessions: 1,
    loadAvg: 0.4, memAvailableMb: 8192, sha: 'abc1234', protocol: 1,
    lastProbeAt: '2026-08-21T12:00:00', lastError: null },
  { name: 'rocky', endpoint: 'https://rocky.tail0dd570.ts.net:9443', enabled: true,
    weight: 100, maxSessions: 4, healthy: false, activeSessions: 0,
    loadAvg: null, memAvailableMb: null, sha: null, protocol: null,
    lastProbeAt: '2026-08-21T12:00:00', lastError: 'ConnectionError: refused' },
]

beforeEach(() => {
  global.fetch = vi.fn(async () => ({
    ok: true, status: 200, json: async () => ({ hosts }),
  })) as unknown as typeof fetch
})

describe('FleetSection', () => {
  it('lists every host', async () => {
    render(<FleetSection />)
    await waitFor(() => expect(screen.getByText('dobby')).toBeInTheDocument())
    expect(screen.getByText('rocky')).toBeInTheDocument()
  })

  it('surfaces the probe error for an unhealthy host', async () => {
    // Without this, an operator sees "unhealthy" with no way to learn why.
    render(<FleetSection />)
    await waitFor(() =>
      expect(screen.getByText(/ConnectionError: refused/)).toBeInTheDocument())
  })

  it('shows capacity as used-of-limit when a limit is set', async () => {
    render(<FleetSection />)
    await waitFor(() => expect(screen.getByText('0 / 4')).toBeInTheDocument())
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run (in `web/`): `npx vitest run src/components/settings/__tests__/FleetSection.test.tsx`
Expected: FAIL — cannot resolve `../FleetSection`

- [ ] **Step 3: Write the component**

```tsx
// web/src/components/settings/FleetSection.tsx
import { useEffect, useState } from 'react'

export interface FleetHost {
  name: string
  endpoint: string
  enabled: boolean
  weight: number
  maxSessions: number | null
  healthy: boolean | null
  activeSessions: number | null
  loadAvg: number | null
  memAvailableMb: number | null
  sha: string | null
  protocol: number | null
  lastProbeAt: string | null
  lastError: string | null
}

function health(h: FleetHost): string {
  if (h.healthy === true) return 'healthy'
  if (h.healthy === false) return 'unhealthy'
  return 'not yet probed'
}

/** Fleet host registry: health, capacity, and the operator-tunable fields. */
export function FleetSection() {
  const [hosts, setHosts] = useState<FleetHost[]>([])
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let live = true
    fetch('/api/fleet/hosts')
      .then((r) => r.json())
      .then((d) => { if (live) setHosts(d.hosts ?? []) })
      .catch((e) => { if (live) setError(String(e)) })
    return () => { live = false }
  }, [])

  async function patch(name: string, body: Partial<FleetHost>) {
    const r = await fetch(`/api/fleet/hosts/${encodeURIComponent(name)}`, {
      method: 'PUT',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(body),
    })
    if (r.ok) {
      const updated = (await r.json()) as FleetHost
      setHosts((prev) => prev.map((h) => (h.name === name ? updated : h)))
    }
  }

  if (error) return <p className="text-sm text-red-600">Could not load fleet: {error}</p>

  return (
    <section className="space-y-3">
      <header>
        <h3 className="text-sm font-semibold">Fleet</h3>
        <p className="text-xs text-slate-500">
          Hosts that can run sessions. The dispatcher ranks healthy hosts by free
          capacity, memory and load; weight multiplies a host&apos;s score.
        </p>
      </header>

      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="text-left text-xs text-slate-500">
              <th className="py-1">Host</th><th>Status</th><th>Sessions</th>
              <th>Load</th><th>Free mem</th><th>Weight</th><th>Enabled</th>
            </tr>
          </thead>
          <tbody>
            {hosts.map((h) => (
              <tr key={h.name} className="border-t border-slate-200 dark:border-slate-700">
                <td className="py-1">
                  <div className="font-medium">{h.name}</div>
                  <div className="text-xs text-slate-500">{h.endpoint}</div>
                  {h.lastError && (
                    <div className="text-xs text-red-600">{h.lastError}</div>
                  )}
                </td>
                <td>{health(h)}</td>
                <td>{h.maxSessions ? `${h.activeSessions ?? 0} / ${h.maxSessions}`
                                   : (h.activeSessions ?? 0)}</td>
                <td>{h.loadAvg?.toFixed(2) ?? '—'}</td>
                <td>{h.memAvailableMb ? `${Math.round(h.memAvailableMb / 1024)} GB` : '—'}</td>
                <td>
                  <input
                    type="number" min={1} max={1000} value={h.weight}
                    aria-label={`weight for ${h.name}`}
                    className="w-16 rounded border px-1"
                    onChange={(e) => patch(h.name, { weight: Number(e.target.value) })}
                  />
                </td>
                <td>
                  <input
                    type="checkbox" checked={h.enabled}
                    aria-label={`enable ${h.name}`}
                    onChange={(e) => patch(h.name, { enabled: e.target.checked })}
                  />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  )
}
```

- [ ] **Step 4: Run test to verify it passes**

Run (in `web/`): `npx vitest run src/components/settings/__tests__/FleetSection.test.tsx`
Expected: PASS (3 passed)

- [ ] **Step 5: Mount it in `SettingsView.tsx`**

Import `FleetSection` and render it alongside the existing sections (e.g. after `GeneralSection`), following that file's existing section ordering and heading conventions.

- [ ] **Step 6: Verify the web gates**

Run (in `web/`): `npx vitest run && npx tsc --noEmit && npx eslint src/components/settings/FleetSection.tsx`
Expected: all pass

- [ ] **Step 7: Commit**

```bash
git add web/src/components/settings/FleetSection.tsx web/src/components/settings/__tests__/FleetSection.test.tsx web/src/components/settings/SettingsView.tsx
git commit -m "feat(fleet): fleet settings section with health, capacity and weights"
```

---

### Task 12: Host picker on session creation

**Files:**
- Modify: `web/src/components/Dashboard.tsx` (or whichever component builds the create-session request — confirm with `grep -rn "api/sessions" web/src --include=*.tsx | grep -i post`)
- Test: `web/src/components/__tests__/HostPicker.test.tsx`

**Interfaces:**
- Consumes: `GET /api/fleet/hosts` (Task 8), `CreateSessionRequest.host` (Task 7).
- Produces: `export function HostPicker({ value, onChange }: { value: string | null; onChange: (h: string | null) => void })` in `web/src/components/HostPicker.tsx`.

- [ ] **Step 1: Write the failing test**

```tsx
// web/src/components/__tests__/HostPicker.test.tsx
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi, beforeEach } from 'vitest'

import { HostPicker } from '../HostPicker'

beforeEach(() => {
  global.fetch = vi.fn(async () => ({
    ok: true, status: 200, json: async () => ({ hosts: [
      { name: 'dobby', healthy: true }, { name: 'rocky', healthy: true },
      { name: 'down', healthy: false },
    ] }),
  })) as unknown as typeof fetch
})

describe('HostPicker', () => {
  it('defaults to Auto so the dispatcher decides', async () => {
    render(<HostPicker value={null} onChange={() => {}} />)
    await waitFor(() =>
      expect(screen.getByRole('combobox')).toHaveValue(''))
    expect(screen.getByRole('option', { name: /auto/i })).toBeInTheDocument()
  })

  it('offers healthy hosts only', async () => {
    render(<HostPicker value={null} onChange={() => {}} />)
    await waitFor(() => expect(screen.getByText('rocky')).toBeInTheDocument())
    // An unhealthy host would be dispatched away from immediately; offering it
    // invites a confusing failover the user did not ask for.
    expect(screen.queryByText('down')).not.toBeInTheDocument()
  })

  it('reports the chosen host', async () => {
    const onChange = vi.fn()
    render(<HostPicker value={null} onChange={onChange} />)
    await waitFor(() => expect(screen.getByText('rocky')).toBeInTheDocument())
    await userEvent.selectOptions(screen.getByRole('combobox'), 'rocky')
    expect(onChange).toHaveBeenCalledWith('rocky')
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run (in `web/`): `npx vitest run src/components/__tests__/HostPicker.test.tsx`
Expected: FAIL — cannot resolve `../HostPicker`

- [ ] **Step 3: Write the component**

```tsx
// web/src/components/HostPicker.tsx
import { useEffect, useState } from 'react'

/**
 * Optional host override for a new session.
 *
 * Defaults to Auto (value=null) so the dispatcher ranks and chooses. Only
 * healthy hosts are offered: an unhealthy host would be failed away from
 * immediately, which looks like the picker ignoring the choice.
 */
export function HostPicker({ value, onChange }: {
  value: string | null
  onChange: (h: string | null) => void
}) {
  const [names, setNames] = useState<string[]>([])

  useEffect(() => {
    let live = true
    fetch('/api/fleet/hosts')
      .then((r) => r.json())
      .then((d) => {
        if (!live) return
        const healthy = (d.hosts ?? []).filter(
          (h: { healthy: boolean | null }) => h.healthy === true)
        setNames(healthy.map((h: { name: string }) => h.name))
      })
      .catch(() => { if (live) setNames([]) })
    return () => { live = false }
  }, [])

  // One host (or none reachable) → nothing meaningful to choose.
  if (names.length < 2) return null

  return (
    <label className="flex items-center gap-2 text-sm">
      <span className="text-slate-600 dark:text-slate-300">Host</span>
      <select
        className="rounded border px-2 py-1"
        value={value ?? ''}
        onChange={(e) => onChange(e.target.value || null)}
      >
        <option value="">Auto (recommended)</option>
        {names.map((n) => <option key={n} value={n}>{n}</option>)}
      </select>
    </label>
  )
}
```

- [ ] **Step 4: Run test to verify it passes**

Run (in `web/`): `npx vitest run src/components/__tests__/HostPicker.test.tsx`
Expected: PASS (3 passed)

- [ ] **Step 5: Wire it into session creation**

In the component that POSTs to `/api/sessions`, hold `const [host, setHost] = useState<string | null>(null)`, render `<HostPicker value={host} onChange={setHost} />` in the form, and include `host` in the request body. Send `host` only when non-null so single-host deployments post an unchanged payload.

- [ ] **Step 6: Verify the web gates**

Run (in `web/`): `npx vitest run && npx tsc --noEmit && npx eslint src/components/HostPicker.tsx`
Expected: all pass

- [ ] **Step 7: Commit**

```bash
git add web/src/components/HostPicker.tsx web/src/components/__tests__/HostPicker.test.tsx web/src/components/Dashboard.tsx
git commit -m "feat(fleet): optional host override when creating a session"
```

---

### Task 13: Enable dobby's daemon and verify two-host operation

**Files:**
- Modify: `docs/runbooks/rocky-session-host.md` (add a fleet section)
- Test: `server/tests/test_fleet_two_host_integration.py`

**Interfaces:**
- Consumes: everything above.
- Produces: a documented two-host bring-up procedure.

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_fleet_two_host_integration.py
"""End-to-end fleet behaviour with two registered hosts (no live daemons).

Exercises registry → probe → rank → route as one flow, which is where the
per-unit tests stop being sufficient.
"""
from __future__ import annotations

import asyncio


def test_probe_then_rank_then_route(tmp_path):
    from server.db import db_session, init_db
    from server.db.models import Host
    from server.sessions.fleet_router import FleetRouter
    from server.sessions.host_probe import probe_all

    async def fetch(endpoint):
        if "rocky" in endpoint:
            return {"sha": "aaa", "protocol": 1, "loadAvg": 0.1,
                    "memAvailableMb": 16384, "activeSessions": 0}
        return {"sha": "bbb", "protocol": 1, "loadAvg": 7.5,
                "memAvailableMb": 1024, "activeSessions": 6}

    async def run():
        await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / 'two.db'}")
        async with db_session() as db:
            db.add(Host(name="rocky", endpoint="https://rocky:9443"))
            db.add(Host(name="dobby", endpoint="unix:///run/ccswitch/sessiond.sock"))
        await probe_all(fetch=fetch)
        r = FleetRouter()
        r.add("rocky", object())
        r.add("dobby", object())
        return await r.pick()

    order = asyncio.run(run())
    assert order[0] == "rocky", "the idle, memory-rich host must rank first"
    assert order == ["rocky", "dobby"], "the busy host stays available for failover"


def test_a_host_that_fails_its_probe_drops_out_of_dispatch(tmp_path):
    from server.db import db_session, init_db
    from server.db.models import Host
    from server.sessions.fleet_router import FleetRouter
    from server.sessions.host_probe import probe_all

    async def fetch(endpoint):
        if "rocky" in endpoint:
            raise ConnectionError("refused")
        return {"sha": "bbb", "protocol": 1, "loadAvg": 0.2,
                "memAvailableMb": 8192, "activeSessions": 0}

    async def run():
        await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / 'two2.db'}")
        async with db_session() as db:
            db.add(Host(name="rocky", endpoint="https://rocky:9443"))
            db.add(Host(name="dobby", endpoint="unix:///run/ccswitch/sessiond.sock"))
        await probe_all(fetch=fetch)
        r = FleetRouter()
        r.add("rocky", object())
        r.add("dobby", object())
        return await r.pick()

    assert asyncio.run(run()) == ["dobby"], "an unreachable host must not be offered"
```

- [ ] **Step 2: Run test to verify it fails, then passes**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 .venv/bin/python -m pytest server/tests/test_fleet_two_host_integration.py -q`
Expected: PASS once Tasks 1-9 are complete (this test is integration-only; it needs no new production code). If it fails, the failure names the unit that regressed.

- [ ] **Step 3: Run the whole suite plus Postgres**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 .venv/bin/python -m pytest server/tests/ -q`
Then with Postgres: `CCSWITCH_TEST_PG_URL=<test-db-url> env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 .venv/bin/python -m pytest server/tests/test_postgres_compat.py -q`
Expected: no new failures. Known environment-dependent failure: `test_copilot_proxy_resolve::test_resolve_none_when_absent` fails on any host where the copilot binary is installed.

- [ ] **Step 4: Document the bring-up in `docs/runbooks/rocky-session-host.md`**

Add a "Fleet: adding a second host" section covering:
1. Register the host: `INSERT INTO hosts (name, endpoint, enabled, weight) VALUES ('dobby', 'unix:///run/ccswitch/sessiond.sock', true, 100);` — or let startup seed it.
2. Enable dobby's daemon: `sudo systemctl enable --now ccswitch-daemon`.
3. Confirm the probe sees it: `curl -s localhost:8787/api/fleet/hosts | jq '.hosts[] | {name, healthy, activeSessions}'`.
4. Confirm dispatch: create a session with `host: null` and check the returned session's `host` field.
5. Rollback: set `enabled=false` on the host, or `PUT /api/fleet/hosts/dobby {"enabled": false}` — the fleet degrades to single-host with no code change.

- [ ] **Step 5: Commit**

```bash
git add server/tests/test_fleet_two_host_integration.py docs/runbooks/rocky-session-host.md
git commit -m "test(fleet): two-host integration coverage and bring-up runbook"
```

---

## Self-Review

**1. Spec coverage (sub-project 4).** The spec asks for: host registry with *endpoint, health, capacity, repo inventory, capability flags* (Task 1 — **repo inventory and capability flags are deliberately omitted**: locality was resolved as "ignore entirely" on 2026-08-21, so an inventory would be unread data, and no capability differences exist between dobby and rocky today); health/liveness probing (Tasks 3, 4, 9); a dispatcher ranking on free memory, load, repo locality (Task 2 — memory and load only, per the same decision); composition with `selector.py` account ranking (Task 7 — `select_account` is untouched, satisfying decision 5's independence); split-brain guard preserved (Task 5 test `test_router_never_exposes_an_in_process_fallback`, Task 7 `_create_on_fleet` raising rather than falling back). Rollback to single-host (Task 6 seeding, Task 13 step 4). Constitution VI — Task 6 replaces the single-orchestrator construction rather than adding a parallel path.

**2. Placeholder scan.** No TBD/TODO markers. Every code step carries runnable code. Two steps deliberately require the implementer to locate an existing call site rather than quoting it (Task 7 `create_kwargs`, Task 12 the POST component) — both name the exact `grep` to run, because quoting a line range that shifts under earlier tasks would be worse than an explicit lookup.

**3. Type consistency.** `HostCandidate` fields are identical in Task 2's definition, Task 5's `candidates()` construction, and Task 13's usage. `rank_hosts` returns `list[str]` everywhere. `FleetRouter.pick` returns `list[str]` and is consumed as an ordered failover list in Task 7. Wire names are camelCase (`maxSessions`, `memAvailableMb`, `lastProbeAt`) in Tasks 8, 11, 12 and snake_case on columns in Tasks 1, 4, 8 — `sanitize_patch` is the single mapping point. `Session.host` is `str | None` in Task 1, serialized as `host` in Task 8, consumed as `host?: string | null` in Task 10.

**Known gap:** `HostBadge` accepts `hostHealthy`, which the session payload does not yet carry (Task 8 adds only `host`). Either join `hosts.healthy` into the session payload in Task 8, or drop the prop and let the badge show only the name. The tests as written pass either way; decide during Task 10.

## Execution Handoff

Per `CLAUDE.md` ("Execute implementation plans subagent-driven — always"), this plan is executed with **subagent-driven development**: a fresh subagent per task, with review between tasks. No mode question is asked.

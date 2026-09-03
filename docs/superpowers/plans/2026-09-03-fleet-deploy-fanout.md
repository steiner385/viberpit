# Fleet Deploy Fan-out Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A deploy updates every online fleet runner and is complete only when each confirms the deployed commit; a runner that was offline reconciles itself at boot; a runner off the deployed commit is never dispatched to.

**Architecture:** The deploy controller gains a `runners` phase that drives one `RunnerDeploy` per enabled `Host` row (local daemon or ssh-reachable remote) through snapshot → checkout update → conditional restart → verify → restore, then restarts the edge and records the deployed sha in a new `deploy_state` table. The fleet probe gates health on version skew against that table; a `runner_sync` `ExecStartPre` fast-forwards a booting runner to the recorded sha.

**Tech Stack:** Python 3.12 / FastAPI / SQLAlchemy async (asyncpg + aiosqlite) / pytest; React + TypeScript + vitest; systemd units; ssh argv (no shell interpolation).

**Spec:** `docs/superpowers/specs/2026-09-03-fleet-deploy-fanout-design.md`

## Global Constraints

- Everything that lands in ssh argv is validated by `steps.SSH_HOST_RE`, `steps.SSH_REPO_RE`, `steps._SHA_RE` before any command runs; commands are argv lists, never shell strings.
- Remote checkouts are moved by `git merge --ff-only <sha>` only; `git reset --hard` is used solely by rollback rewind.
- pip on a runner is always `<repo>/.venv/bin/python -m pip install -e '<repo>[server]' --quiet` (never `bin/pip`), and only when `pyproject.toml`/`uv.lock` changed.
- `sync` is run on a remote after every checkout write and before its restart (the 2026-08-29 zero-byte-module incident).
- Runner restarts run at most **2** concurrently; each drains first with the existing caps (`CCSWITCH_DRAIN_TURN_CAP_SEC` 180 s / `CCSWITCH_DRAIN_CONDENSE_CAP_SEC` 570 s).
- A failed runner never rolls the edge back and never stops other runners (decision 1a). `runner_sync` always exits 0 after logging (decision 3a).
- New DB columns are nullable (auto-reconciled on SQLite and Postgres); the new table gets an alembic revision chained off `0015_pending_model_reason`.
- Test commands (from the repo root, per CLAUDE.md): `CCSWITCH_DEV=1 PYTHONPATH=$PWD/src <main-checkout>/.venv/bin/python -m pytest …`; web via `npm --prefix web run test -- <file>`, `web/node_modules/.bin/tsc --noEmit -p web`, `web/node_modules/.bin/eslint --config web/eslint.config.js <files>`. Never `cd web`.
- Commit trailer on every commit:
  ```
  Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01XU6jo8jqaD7raDuroyzU2q
  ```

---

## File Structure

| File | Responsibility |
|---|---|
| `server/db/models.py` (+`DeployStateRow`, `Host.checkout_sha`) | schema |
| `server/db/migrations/versions/0016_deploy_state.py` | reviewable schema record |
| `server/deploy/state.py` (new) | read/record the deployed version (`deploy_state`) |
| `server/version.py` (+`checkout_git_sha`, `repo_root`) | process sha vs fresh checkout sha |
| `server/ipc/daemon.py` (`/info`) | report `checkoutSha` |
| `server/hostname.py` (new) | `self_host_name()` moved out of the orchestrator (light import for the controller) |
| `server/sessions/version_skew.py` (new) | pure skew predicate |
| `server/sessions/host_probe.py` | store `checkout_sha`, apply the skew gate |
| `server/deploy/runners.py` (new) | `RunnerTarget`, `HostProgress`, `RunnerDeploy`, `deploy_runners`, `rewind_runners`, `load_runner_targets` |
| `server/sessions/host_upgrade.py` | thin wrapper over `RunnerDeploy` for the Fleet UI button |
| `server/deploy/steps.py` | `restart_edge()` replaces `restart_units`; `remote_runners_configured()`; `rollback_to` loses the ssh branch |
| `server/deploy/controller.py`, `server/deploy/types.py` | `runners` phase, per-host status, `deploy_state` write |
| `server/deploy/runner_sync.py` (new) | boot-time reconcile (`ExecStartPre`) |
| `deploy/ccswitch-daemon-remote.service`, `deploy/ccswitch-daemon.service` | `ExecStartPre` |
| `server/app.py` | seed `ssh_host`/`repo_path` from `CCSWITCH_DAEMON_SSH_HOST/_REPO` once |
| `web/src/api/types.ts`, `web/src/lib/deployPhases.ts`, `web/src/components/DeployOverlay.tsx`, `web/src/components/settings/FleetSection.tsx` | operator surface |
| `docs/runbooks/rocky-session-host.md`, `docs/runbooks/onboarding-a-session-runner.md`, `deploy/README.md` | procedures |

---

### Task 1: `deploy_state` table + `hosts.checkout_sha` + `server/deploy/state.py`

**Files:**
- Modify: `server/db/models.py` (after `class Host`, ~line 902)
- Create: `server/db/migrations/versions/0016_deploy_state.py`
- Create: `server/deploy/state.py`
- Modify: `server/tests/test_postgres_compat.py:137-158` (`_REQUIRED_EXTRAS`)
- Test: `server/tests/test_deploy_state.py`

**Interfaces:**
- Produces:
  ```python
  # server/deploy/state.py
  DEPLOY_STATE_ID = "singleton"
  @dataclass(frozen=True)
  class DeployState:
      deployed_sha: str        # full sha
      deployed_short: str      # edge's abbreviation
      daemon_sha: str | None   # full sha of the last daemon-changing deploy
      deployed_at: datetime | None
  async def read_deploy_state() -> DeployState | None
  async def record_deploy(*, sha: str, short: str, daemon_changed: bool, by: str = "deploy-controller") -> DeployState
  ```
  and `Host.checkout_sha: str | None`.

- [ ] **Step 1: Write the failing tests**

```python
# server/tests/test_deploy_state.py
"""deploy_state: the fleet's durable 'deployed version' (spec §1)."""
from __future__ import annotations
import asyncio


def _db(tmp_path, name):
    from server.db import init_db
    return init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / name}")


def test_read_returns_none_before_any_deploy(tmp_path):
    from server.deploy.state import read_deploy_state

    async def run():
        await _db(tmp_path, "a.db")
        return await read_deploy_state()

    assert asyncio.run(run()) is None


def test_record_then_read_round_trips_and_daemon_sha_only_moves_when_daemon_changed(tmp_path):
    from server.deploy.state import read_deploy_state, record_deploy

    async def run():
        await _db(tmp_path, "b.db")
        first = await record_deploy(sha="a" * 40, short="aaaaaaa", daemon_changed=True)
        second = await record_deploy(sha="b" * 40, short="bbbbbbb", daemon_changed=False)
        return first, second, await read_deploy_state()

    first, second, latest = asyncio.run(run())
    assert first.daemon_sha == "a" * 40
    assert second.deployed_sha == "b" * 40 and second.deployed_short == "bbbbbbb"
    assert second.daemon_sha == "a" * 40, "edge-only deploy must not advance daemon_sha"
    assert latest == second
    assert latest.deployed_at is not None


def test_host_has_checkout_sha_column(tmp_path):
    from sqlalchemy import select as sa_select
    from server.db import db_session
    from server.db.models import Host

    async def run():
        await _db(tmp_path, "c.db")
        async with db_session() as db:
            db.add(Host(name="r", endpoint="https://r:9443", checkout_sha="abc1234"))
        async with db_session() as db:
            return (await db.execute(sa_select(Host.checkout_sha).where(Host.name == "r"))).scalar_one()

    assert asyncio.run(run()) == "abc1234"
```

- [ ] **Step 2: Run to verify they fail**

Run: `CCSWITCH_DEV=1 PYTHONPATH=$PWD/src /home/tony/GitHub/ccswitch/.venv/bin/python -m pytest server/tests/test_deploy_state.py -q`
Expected: FAIL — `ModuleNotFoundError: server.deploy.state` / `TypeError: 'checkout_sha' is an invalid keyword argument for Host`.

- [ ] **Step 3: Add the model + column**

In `server/db/models.py`, add to `class Host` after `active_sessions`:

```python
    # Fresh `git rev-parse --short HEAD` of the daemon's checkout (reported as
    # /info.checkoutSha). Differs from `sha` (the PROCESS's start-time HEAD) after an
    # edge-only deploy updated the checkout without a restart. Nullable: reconcile adds it.
    checkout_sha: Mapped[str | None] = mapped_column(String, nullable=True, default=None)
```

Append after `class Host`:

```python
class DeployStateRow(Base):
    """Single-row (id='singleton') record of the fleet's deployed version (spec §1).

    Written by the deploy controller once the edge is healthy on a target; read by the
    fleet probe's version-skew gate and by ``server.deploy.runner_sync`` at runner boot.
    Every column but the PK is nullable so init_db reconciles it without a migration.
    """

    __tablename__ = "deploy_state"

    id: Mapped[str] = mapped_column(String, primary_key=True, default="singleton")
    deployed_sha: Mapped[str | None] = mapped_column(String, nullable=True, default=None)
    deployed_short: Mapped[str | None] = mapped_column(String, nullable=True, default=None)
    # Full sha of the last deploy whose diff was daemon-changing: a runner PROCESS older
    # than this is stale even if its checkout is current.
    daemon_sha: Mapped[str | None] = mapped_column(String, nullable=True, default=None)
    deployed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, default=None)
    deployed_by: Mapped[str | None] = mapped_column(String, nullable=True, default=None)
```

- [ ] **Step 4: Add the migration**

```python
# server/db/migrations/versions/0016_deploy_state.py
"""fleet deploy fan-out — deploy_state table + hosts.checkout_sha

Revision ID: 0016_deploy_state
Revises: 0015_pending_model_reason
Create Date: 2026-09-03

Reviewable record only: nothing runs `alembic upgrade` at deploy time — init_db's
create_all builds the new table and _reconcile_columns ALTER-ADDs the nullable column
on both SQLite and Postgres at next boot.
"""
from alembic import op
import sqlalchemy as sa

revision = "0016_deploy_state"
down_revision = "0015_pending_model_reason"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "deploy_state",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("deployed_sha", sa.String(), nullable=True),
        sa.Column("deployed_short", sa.String(), nullable=True),
        sa.Column("daemon_sha", sa.String(), nullable=True),
        sa.Column("deployed_at", sa.DateTime(), nullable=True),
        sa.Column("deployed_by", sa.String(), nullable=True),
    )
    op.add_column("hosts", sa.Column("checkout_sha", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("hosts", "checkout_sha")
    op.drop_table("deploy_state")
```

- [ ] **Step 5: Write `server/deploy/state.py`**

```python
"""The fleet's durable "deployed version" (spec §1).

One row, id='singleton'. ``record_deploy`` is called by the controller once the edge is
healthy on the target — the moment the fleet's authoritative version moved, even if a
runner lagged (that runner is then fenced by the skew gate against the NEW sha).
``daemon_sha`` advances only for daemon-changing deploys: an edge-only deploy leaves
every runner process on its prior commit by design.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select as sa_select

from server.db import db_session
from server.db.models import DeployStateRow

DEPLOY_STATE_ID = "singleton"


@dataclass(frozen=True)
class DeployState:
    deployed_sha: str
    deployed_short: str
    daemon_sha: str | None
    deployed_at: datetime | None


def _from_row(row: DeployStateRow | None) -> DeployState | None:
    if row is None or not row.deployed_sha:
        return None
    return DeployState(deployed_sha=row.deployed_sha,
                       deployed_short=row.deployed_short or row.deployed_sha[:7],
                       daemon_sha=row.daemon_sha, deployed_at=row.deployed_at)


async def read_deploy_state() -> DeployState | None:
    async with db_session() as db:
        row = await db.get(DeployStateRow, DEPLOY_STATE_ID)
        return _from_row(row)


async def record_deploy(*, sha: str, short: str, daemon_changed: bool,
                        by: str = "deploy-controller") -> DeployState:
    async with db_session() as db:
        row = await db.get(DeployStateRow, DEPLOY_STATE_ID)
        if row is None:
            row = DeployStateRow(id=DEPLOY_STATE_ID)
            db.add(row)
        row.deployed_sha = sha
        row.deployed_short = short
        if daemon_changed:
            row.daemon_sha = sha
        row.deployed_at = datetime.utcnow()
        row.deployed_by = by
        await db.flush()
        return _from_row(row)  # type: ignore[return-value]  # deployed_sha is set above
```

- [ ] **Step 6: Register the table in the Postgres sweep list**

In `server/tests/test_postgres_compat.py` `_REQUIRED_EXTRAS`, after the `daemon_leases` entry:

```python
    # deploy_state joins the sweep via deployed_at; id is the PK (Python-side default).
    "deploy_state": {"id": "dtcheck-deploy-state"},
```

- [ ] **Step 7: Run the new tests, the reconcile-coverage test and the schema tests**

Run: `CCSWITCH_DEV=1 PYTHONPATH=$PWD/src /home/tony/GitHub/ccswitch/.venv/bin/python -m pytest server/tests/test_deploy_state.py server/tests/test_schema_reconcile_coverage.py server/tests/test_db_schema*.py -q`
Expected: PASS. If `test_schema_reconcile_coverage` names `deploy_state.id`, add `"deploy_state.id",  # PK of a NEW table; part of CREATE TABLE, never ALTER-added` to `RECONCILE_EXEMPT` in that file.

- [ ] **Step 8: Commit**

```bash
git add server/db/models.py server/db/migrations/versions/0016_deploy_state.py server/deploy/state.py server/tests/test_deploy_state.py server/tests/test_postgres_compat.py server/tests/test_schema_reconcile_coverage.py
git commit -m "feat(deploy): deploy_state table + hosts.checkout_sha — the fleet's durable deployed version"
```

---

### Task 2: Daemon reports `checkoutSha`; probe + API carry it

**Files:**
- Modify: `server/version.py`
- Modify: `server/ipc/daemon.py:411-417` (`/info`)
- Modify: `server/sessions/host_probe.py:41-80` (store `checkout_sha`)
- Modify: `server/api/fleet.py:62-87` (`host_to_wire`)
- Modify: `web/src/components/settings/FleetSection.tsx:15-32` (`FleetHost.checkoutSha`)
- Test: `server/tests/test_version_checkout_sha.py`, `server/tests/test_host_probe.py` (add one test)

**Interfaces:**
- Produces: `server.version.checkout_git_sha(*, now=time.monotonic, run=subprocess.run) -> str` (fresh `git rev-parse --short HEAD`, cached ≤ 10 s, `"unknown"` on error); `server.version.repo_root() -> str`; `/info` → `{"sha", "checkoutSha", "protocol", "epoch", …}`; `Host.checkout_sha` populated by the probe; wire key `checkoutSha`.

- [ ] **Step 1: Write the failing tests**

```python
# server/tests/test_version_checkout_sha.py
import subprocess
from server import version


def _run_ok(out: str):
    def run(argv, **kw):
        return subprocess.CompletedProcess(argv, 0, stdout=out + "\n", stderr="")
    return run


def test_checkout_sha_is_fresh_after_ttl_and_cached_within_it():
    version._reset_checkout_cache_for_tests()
    t = [100.0]
    calls = []
    def run(argv, **kw):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout=f"sha{len(calls)}\n", stderr="")
    assert version.checkout_git_sha(now=lambda: t[0], run=run) == "sha1"
    t[0] += 5.0
    assert version.checkout_git_sha(now=lambda: t[0], run=run) == "sha1", "cached within 10 s"
    t[0] += 6.0
    assert version.checkout_git_sha(now=lambda: t[0], run=run) == "sha2", "re-read after the TTL"
    assert calls[0][:3] == ["git", "rev-parse", "--short"]


def test_checkout_sha_unknown_on_git_failure():
    version._reset_checkout_cache_for_tests()
    def boom(argv, **kw):
        raise OSError("no git")
    assert version.checkout_git_sha(now=lambda: 0.0, run=boom) == "unknown"


def test_repo_root_is_the_checkout_containing_server_package():
    import os
    assert os.path.isdir(os.path.join(version.repo_root(), "server"))
```

Append to `server/tests/test_host_probe.py` (uses that file's existing `_seed`-style helper; if it has none, copy `_seed` from `test_host_probe_protocol_gate.py`):

```python
def test_probe_records_checkout_sha_separately_from_process_sha(tmp_path):
    from sqlalchemy import select as sa_select
    from server.db import db_session
    from server.db.models import Host
    from server.ipc.protocol import PROTOCOL_VERSION
    from server.sessions.host_probe import probe_host

    async def fake_fetch(endpoint):
        return {"sha": "proc111", "checkoutSha": "chk2222", "protocol": PROTOCOL_VERSION}

    async def run():
        await _seed(tmp_path, "chk.db", Host(name="r", endpoint="https://r:9443"))()
        await probe_host("r", "https://r:9443", fetch=fake_fetch)
        async with db_session() as db:
            return (await db.execute(sa_select(Host).where(Host.name == "r"))).scalars().first()

    row = asyncio.run(run())
    assert row.sha == "proc111" and row.checkout_sha == "chk2222"
```

- [ ] **Step 2: Run to verify they fail**

Run: `CCSWITCH_DEV=1 PYTHONPATH=$PWD/src /home/tony/GitHub/ccswitch/.venv/bin/python -m pytest server/tests/test_version_checkout_sha.py server/tests/test_host_probe.py -q`
Expected: FAIL — `AttributeError: module 'server.version' has no attribute 'checkout_git_sha'`; `row.checkout_sha is None`.

- [ ] **Step 3: Implement `checkout_git_sha` / `repo_root`**

Append to `server/version.py`:

```python
import time  # add to the imports at the top

_CHECKOUT_TTL_S = 10.0
_checkout_cache: tuple[float, str] | None = None   # (read_at, sha)


def repo_root() -> str:
    """The checkout this code runs from (parent of the ``server`` package)."""
    return str(Path(__file__).resolve().parents[1])


def _reset_checkout_cache_for_tests() -> None:
    global _checkout_cache
    _checkout_cache = None


def checkout_git_sha(*, now=time.monotonic, run=subprocess.run) -> str:
    """Short sha of the checkout's CURRENT HEAD, re-read at most every 10 s.

    Unlike ``running_git_sha`` (the process's start-time HEAD, cached for life), this
    follows the working tree: after an edge-only deploy fast-forwards a runner's checkout
    without restarting its daemon, the two legitimately differ, and the fleet probe's
    skew gate needs the checkout value (spec §4). ``"unknown"`` when git is unavailable.
    """
    global _checkout_cache
    t = now()
    if _checkout_cache is not None and t - _checkout_cache[0] < _CHECKOUT_TTL_S:
        return _checkout_cache[1]
    try:
        out = run(["git", "rev-parse", "--short", "HEAD"], cwd=repo_root(),
                  capture_output=True, text=True, timeout=5, check=True)
        sha = (out.stdout or "").strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        sha = "unknown"
    _checkout_cache = (t, sha)
    return sha
```

- [ ] **Step 4: Report it from `/info` and store it in the probe**

`server/ipc/daemon.py` `/info`:

```python
    @app.get("/info")
    async def info() -> dict:
        from server.version import checkout_git_sha, running_git_sha
        orch = getattr(app.state, "orchestrator", None)
        active = len(getattr(orch, "_records", {}) or {})
        return {"sha": running_git_sha(), "checkoutSha": checkout_git_sha(),
                "protocol": proto.PROTOCOL_VERSION,
                "epoch": _app_epoch, **_capacity_snapshot(active)}
```

`server/sessions/host_probe.py` `probe_host`: in BOTH `values.update(...)` branches add `checkout_sha=info.get("checkoutSha"),` right after `sha=info.get("sha"),`.

`server/api/fleet.py` `host_to_wire`: add `"checkoutSha": h.checkout_sha,` after `"sha": h.sha,`.

`web/src/components/settings/FleetSection.tsx` `FleetHost`: add `checkoutSha: string | null` after `sha`.

- [ ] **Step 5: Run tests**

Run: `CCSWITCH_DEV=1 PYTHONPATH=$PWD/src /home/tony/GitHub/ccswitch/.venv/bin/python -m pytest server/tests/test_version_checkout_sha.py server/tests/test_host_probe.py server/tests/test_host_probe_protocol_gate.py server/tests/test_fleet_api.py server/tests/test_ipc*.py -q` and `web/node_modules/.bin/tsc --noEmit -p web`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add server/version.py server/ipc/daemon.py server/sessions/host_probe.py server/api/fleet.py web/src/components/settings/FleetSection.tsx server/tests/test_version_checkout_sha.py server/tests/test_host_probe.py
git commit -m "feat(fleet): daemons report checkoutSha beside the process sha; probe + API carry it"
```

---

### Task 3: Version-skew gate in the fleet probe

**Files:**
- Create: `server/sessions/version_skew.py`
- Modify: `server/sessions/host_probe.py` (`probe_host`, `probe_all`)
- Test: `server/tests/test_version_skew.py`, `server/tests/test_host_probe_skew_gate.py`

**Interfaces:**
- Consumes: `server.deploy.state.read_deploy_state()`, `DeployState`; `server.deploy.steps.short_sha_match`; `server.deploy.git.is_ancestor(repo, ancestor, descendant)`; `server.version.repo_root()`.
- Produces:
  ```python
  # server/sessions/version_skew.py
  IsAncestor = Callable[[str, str], bool]          # (ancestor, descendant) -> bool
  def detect_skew(*, checkout_sha: str | None, process_sha: str | None,
                  state: DeployState | None, is_ancestor: IsAncestor) -> str | None
  def edge_is_ancestor(ancestor: str, descendant: str) -> bool   # memoised git call on the edge checkout
  ```
  `probe_host(name, endpoint, *, fetch, state: DeployState | None = None, is_ancestor: IsAncestor = edge_is_ancestor)`; `probe_all` reads the state once per tick.

- [ ] **Step 1: Write the failing tests**

```python
# server/tests/test_version_skew.py
from server.deploy.state import DeployState
from server.sessions.version_skew import detect_skew

S = DeployState(deployed_sha="b" * 40, deployed_short="bbbbbbb", daemon_sha="a" * 40, deployed_at=None)
ANCESTORS = {("a" * 40, "bbbbbbb"), ("a" * 40, "aaaaaaa"), ("a" * 40, "ccccccc")}
def anc(a, d): return (a, d) in ANCESTORS


def test_no_state_means_no_gate():
    assert detect_skew(checkout_sha="zzz", process_sha="zzz", state=None, is_ancestor=anc) is None


def test_in_sync_after_daemon_changing_deploy():
    assert detect_skew(checkout_sha="bbbbbbb", process_sha="bbbbbbb", state=S, is_ancestor=anc) is None


def test_checkout_behind_is_skew():
    err = detect_skew(checkout_sha="aaaaaaa", process_sha="aaaaaaa", state=S, is_ancestor=anc)
    assert err and "version skew" in err and "checkout=aaaaaaa" in err and "deployed=bbbbbbb" in err


def test_process_older_than_daemon_sha_is_skew_even_with_current_checkout():
    # checkout was fast-forwarded (edge-only path) but the process predates daemon_sha
    ANCESTORS.discard(("a" * 40, "0000000"))
    err = detect_skew(checkout_sha="bbbbbbb", process_sha="0000000", state=S, is_ancestor=anc)
    assert err and "process=0000000" in err


def test_process_descendant_of_daemon_sha_is_fine():
    # process started at ccccccc (a web-only commit after daemon_sha) — current
    assert detect_skew(checkout_sha="bbbbbbb", process_sha="ccccccc", state=S, is_ancestor=anc) is None


def test_missing_checkout_sha_falls_back_to_process_sha():
    assert detect_skew(checkout_sha=None, process_sha="bbbbbbb", state=S, is_ancestor=anc) is None
    assert detect_skew(checkout_sha=None, process_sha="aaaaaaa", state=S, is_ancestor=anc) is not None


def test_no_daemon_sha_skips_process_check():
    s = DeployState(deployed_sha="b" * 40, deployed_short="bbbbbbb", daemon_sha=None, deployed_at=None)
    assert detect_skew(checkout_sha="bbbbbbb", process_sha="0000000", state=s, is_ancestor=anc) is None


def test_prefix_abbreviations_match():
    assert detect_skew(checkout_sha="bbbbbbbb1", process_sha="bbbbbb", state=S,
                       is_ancestor=lambda a, d: True) is None
```

```python
# server/tests/test_host_probe_skew_gate.py
"""Version-skew gating in probe_host (spec §4): a runner not on the deployed commit is
healthy=False so the ranker never dispatches to it."""
from __future__ import annotations
import asyncio
from sqlalchemy import select as sa_select


def _seed(tmp_path, dbname, *hosts):
    from server.db import db_session, init_db

    async def go():
        await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / dbname}")
        async with db_session() as db:
            for h in hosts:
                db.add(h)
    return go


def _row(name):
    from server.db import db_session
    from server.db.models import Host

    async def go():
        async with db_session() as db:
            return (await db.execute(sa_select(Host).where(Host.name == name))).scalars().first()
    return go


def test_stale_checkout_marks_unhealthy_with_skew_error(tmp_path):
    from server.db.models import Host
    from server.deploy.state import record_deploy
    from server.ipc.protocol import PROTOCOL_VERSION
    from server.sessions.host_probe import probe_all

    async def fake_fetch(endpoint):
        return {"sha": "old1111", "checkoutSha": "old1111", "protocol": PROTOCOL_VERSION}

    async def run():
        await _seed(tmp_path, "skew.db", Host(name="rocky", endpoint="https://rocky:9443"))()
        await record_deploy(sha="new2222" + "0" * 33, short="new2222", daemon_changed=True)
        await probe_all(fetch=fake_fetch, is_ancestor=lambda a, d: False)
        return await _row("rocky")()

    row = asyncio.run(run())
    assert row.healthy is False
    assert "version skew" in (row.last_error or "")
    assert row.checkout_sha == "old1111" and row.sha == "old1111"


def test_in_sync_runner_stays_healthy(tmp_path):
    from server.db.models import Host
    from server.deploy.state import record_deploy
    from server.ipc.protocol import PROTOCOL_VERSION
    from server.sessions.host_probe import probe_all

    async def fake_fetch(endpoint):
        return {"sha": "new2222", "checkoutSha": "new2222", "protocol": PROTOCOL_VERSION}

    async def run():
        await _seed(tmp_path, "ok.db", Host(name="rocky", endpoint="https://rocky:9443"))()
        await record_deploy(sha="new2222" + "0" * 33, short="new2222", daemon_changed=True)
        await probe_all(fetch=fake_fetch, is_ancestor=lambda a, d: True)
        return await _row("rocky")()

    row = asyncio.run(run())
    assert row.healthy is True and row.last_error is None


def test_no_deploy_state_does_not_gate(tmp_path):
    from server.db.models import Host
    from server.ipc.protocol import PROTOCOL_VERSION
    from server.sessions.host_probe import probe_all

    async def fake_fetch(endpoint):
        return {"sha": "anything", "checkoutSha": "anything", "protocol": PROTOCOL_VERSION}

    async def run():
        await _seed(tmp_path, "nostate.db", Host(name="rocky", endpoint="https://rocky:9443"))()
        await probe_all(fetch=fake_fetch, is_ancestor=lambda a, d: False)
        return await _row("rocky")()

    assert asyncio.run(run()).healthy is True


def test_protocol_mismatch_still_wins_over_skew(tmp_path):
    from server.db.models import Host
    from server.deploy.state import record_deploy
    from server.ipc.protocol import PROTOCOL_VERSION
    from server.sessions.host_probe import probe_all

    async def fake_fetch(endpoint):
        return {"sha": "old1111", "checkoutSha": "old1111", "protocol": PROTOCOL_VERSION + 1}

    async def run():
        await _seed(tmp_path, "proto.db", Host(name="rocky", endpoint="https://rocky:9443"))()
        await record_deploy(sha="new2222" + "0" * 33, short="new2222", daemon_changed=True)
        await probe_all(fetch=fake_fetch, is_ancestor=lambda a, d: False)
        return await _row("rocky")()

    row = asyncio.run(run())
    assert row.healthy is False and "protocol mismatch" in row.last_error
```

- [ ] **Step 2: Run to verify they fail**

Run: `CCSWITCH_DEV=1 PYTHONPATH=$PWD/src /home/tony/GitHub/ccswitch/.venv/bin/python -m pytest server/tests/test_version_skew.py server/tests/test_host_probe_skew_gate.py -q`
Expected: FAIL — `ModuleNotFoundError: server.sessions.version_skew`; `probe_all() got an unexpected keyword argument 'is_ancestor'`.

- [ ] **Step 3: Write `server/sessions/version_skew.py`**

```python
"""Is a runner on the fleet's deployed version? (spec §4)

Pure predicate over what the probe already has: the daemon's two reported shas and the
``deploy_state`` row. Two checks:

* the CHECKOUT must be the deployed commit (prefix match — hosts abbreviate the same
  commit to different lengths, ``steps.short_sha_match``);
* the PROCESS must be at or after the last daemon-changing deploy (``daemon_sha``).
  "After" needs ancestry, which only the edge's checkout can answer; the probe passes
  ``is_ancestor`` in and the default memoises one git call per (daemon_sha, process)
  pair so the 30 s probe pays it once per runner generation.

A runner on a commit the edge has never fetched (a hotfix branch) is not an ancestor
of anything → skewed → fenced, which is the correct answer for "not the deployed code".
"""
from __future__ import annotations

import functools
import logging
from typing import Callable

from server.deploy import git as deploy_git
from server.deploy.state import DeployState
from server.deploy.steps import short_sha_match
from server.version import repo_root

log = logging.getLogger(__name__)

IsAncestor = Callable[[str, str], bool]


@functools.lru_cache(maxsize=256)
def edge_is_ancestor(ancestor: str, descendant: str) -> bool:
    try:
        return deploy_git.is_ancestor(repo_root(), ancestor, descendant)
    except Exception:
        log.debug("version_skew: is_ancestor(%s, %s) failed", ancestor, descendant, exc_info=True)
        return False


def detect_skew(*, checkout_sha: str | None, process_sha: str | None,
                state: DeployState | None, is_ancestor: IsAncestor) -> str | None:
    """``None`` when the runner is current; otherwise the ``last_error`` text."""
    if state is None:
        return None
    checkout = checkout_sha or process_sha
    if not checkout or not short_sha_match(str(checkout), state.deployed_short):
        return (f"version skew: checkout={checkout or '?'} process={process_sha or '?'} "
                f"deployed={state.deployed_short}")
    if state.daemon_sha and process_sha:
        current = (short_sha_match(str(process_sha), state.daemon_sha[:len(str(process_sha))])
                   or is_ancestor(state.daemon_sha, str(process_sha)))
        if not current:
            return (f"version skew: checkout={checkout} process={process_sha} "
                    f"deployed={state.deployed_short} (process predates daemon_sha "
                    f"{state.daemon_sha[:7]})")
    return None
```

- [ ] **Step 4: Apply it in `host_probe.py`**

Replace `probe_host` / `probe_all` signatures and the healthy branch:

```python
from server.deploy.state import DeployState, read_deploy_state
from server.sessions.version_skew import IsAncestor, detect_skew, edge_is_ancestor


async def probe_host(name: str, endpoint: str, *, fetch: Fetch,
                     state: DeployState | None = None,
                     is_ancestor: IsAncestor = edge_is_ancestor) -> dict:
    """Probe one host and write the outcome. Never raises.

    ``state`` is the deploy_state row read ONCE per tick by probe_all; None disables the
    version-skew gate (no deploy recorded yet, or a caller that wants raw health only).
    """
    from server.ipc.protocol import PROTOCOL_VERSION
    values: dict = {"last_probe_at": datetime.utcnow()}
    try:
        info = await fetch(endpoint)
        daemon_proto = info.get("protocol")
        metrics = dict(sha=info.get("sha"), checkout_sha=info.get("checkoutSha"),
                       protocol=daemon_proto, load_avg=info.get("loadAvg"),
                       mem_available_mb=info.get("memAvailableMb"),
                       active_sessions=info.get("activeSessions"))
        # Gate 1 — protocol: a daemon speaking another RPC protocol is never dispatchable.
        if daemon_proto is not None and daemon_proto != PROTOCOL_VERSION:
            values.update(healthy=False, last_error=(
                f"protocol mismatch: daemon={daemon_proto} edge={PROTOCOL_VERSION}"), **metrics)
        else:
            # Gate 2 — version skew (spec §4): not on the deployed commit ⇒ fenced from
            # dispatch until a deploy fan-out, runner_sync, or an operator fixes it.
            skew = detect_skew(checkout_sha=info.get("checkoutSha"), process_sha=info.get("sha"),
                               state=state, is_ancestor=is_ancestor)
            values.update(healthy=skew is None, last_error=skew, **metrics)
    except Exception as exc:
        values.update(healthy=False, last_error=f"{type(exc).__name__}: {exc}"[:500])
        log.warning("fleet.probe_failed host=%s endpoint=%s: %s", name, endpoint, exc)

    async with db_session() as db:
        await db.execute(sa_update(Host).where(Host.name == name).values(**values))
    return values


async def probe_all(*, fetch: Fetch | None = None,
                    is_ancestor: IsAncestor = edge_is_ancestor) -> int:
    use = fetch or default_fetch
    try:
        state = await read_deploy_state()
    except Exception:
        log.warning("fleet.probe: deploy_state unreadable — skew gate off this tick", exc_info=True)
        state = None
    async with db_session() as db:
        rows = (await db.execute(
            sa_select(Host.name, Host.endpoint).where(Host.enabled.is_(True)))).all()
    for name, endpoint in rows:
        await probe_host(name, endpoint, fetch=use, state=state, is_ancestor=is_ancestor)
    return len(rows)
```

Keep `probe_loop` unchanged (it calls `probe_all(fetch=fetch)`).

- [ ] **Step 5: Run the probe suites**

Run: `CCSWITCH_DEV=1 PYTHONPATH=$PWD/src /home/tony/GitHub/ccswitch/.venv/bin/python -m pytest server/tests/test_version_skew.py server/tests/test_host_probe_skew_gate.py server/tests/test_host_probe.py server/tests/test_host_probe_protocol_gate.py server/tests/test_fleet_ranking.py -q`
Expected: PASS (existing probe tests pass because no `deploy_state` row exists in their DBs).

- [ ] **Step 6: Commit**

```bash
git add server/sessions/version_skew.py server/sessions/host_probe.py server/tests/test_version_skew.py server/tests/test_host_probe_skew_gate.py
git commit -m "feat(fleet): version-skew gate — a runner off the deployed commit is never dispatchable"
```

---

### Task 4: `server/hostname.py` (light `self_host_name`)

**Files:**
- Create: `server/hostname.py`
- Modify: `server/sessions/orchestrator.py:370-385` (replace the body with an import)
- Test: `server/tests/test_hostname.py`

**Interfaces:**
- Produces: `server.hostname.self_host_name() -> str` (env `CCSWITCH_HOST_NAME`, else first hostname label). `server.sessions.orchestrator.self_host_name` keeps working (re-exported by import).

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_hostname.py
def test_env_wins(monkeypatch):
    monkeypatch.setenv("CCSWITCH_HOST_NAME", " rocky ")
    from server.hostname import self_host_name
    assert self_host_name() == "rocky"


def test_falls_back_to_first_hostname_label(monkeypatch):
    import socket
    monkeypatch.delenv("CCSWITCH_HOST_NAME", raising=False)
    monkeypatch.setattr(socket, "gethostname", lambda: "dobby.tail0dd570.ts.net")
    from server.hostname import self_host_name
    assert self_host_name() == "dobby"


def test_orchestrator_reexports_the_same_function():
    from server import hostname
    from server.sessions import orchestrator
    assert orchestrator.self_host_name is hostname.self_host_name
```

- [ ] **Step 2: Run to verify it fails**

Run: `CCSWITCH_DEV=1 PYTHONPATH=$PWD/src /home/tony/GitHub/ccswitch/.venv/bin/python -m pytest server/tests/test_hostname.py -q`
Expected: FAIL — `ModuleNotFoundError: server.hostname`.

- [ ] **Step 3: Create the module and re-point the orchestrator**

```python
# server/hostname.py
"""This process's fleet identity — dependency-light so the deploy controller can import it
without pulling in the orchestrator (which drags the whole session engine)."""
from __future__ import annotations

import os
import socket


def self_host_name() -> str:
    """The ``hosts.name`` this process answers to.

    ``CCSWITCH_HOST_NAME`` wins when set (operator-managed registries can use any name).
    The fallback is the kernel hostname's first label, which matches how auto-registered
    hosts are named (the first label of the tailnet MagicDNS endpoint). A worker whose
    identity doesn't match its registry row rehydrates zero sessions after a restart —
    set the env var explicitly on renamed hosts.
    """
    name = os.environ.get("CCSWITCH_HOST_NAME", "").strip()
    if name:
        return name
    return socket.gethostname().split(".")[0]
```

In `server/sessions/orchestrator.py` delete the `def self_host_name()` body (lines 370-385) and add near the other imports at the top of the file:

```python
from server.hostname import self_host_name  # noqa: F401 — re-exported; the fleet identity lives in server.hostname
```

- [ ] **Step 4: Run tests**

Run: `CCSWITCH_DEV=1 PYTHONPATH=$PWD/src /home/tony/GitHub/ccswitch/.venv/bin/python -m pytest server/tests/test_hostname.py server/tests/test_worker_host_stamp.py -q && /home/tony/GitHub/ccswitch/.venv/bin/ruff check server/`
Expected: PASS, ruff clean.

- [ ] **Step 5: Commit**

```bash
git add server/hostname.py server/sessions/orchestrator.py server/tests/test_hostname.py
git commit -m "refactor: move self_host_name to server.hostname (light import for the deploy controller)"
```

---

### Task 5: `server/deploy/runners.py` — the per-runner lifecycle

**Files:**
- Create: `server/deploy/runners.py`
- Test: `server/tests/test_deploy_runners.py`

**Interfaces:**
- Consumes: `steps.SSH_HOST_RE`, `steps.SSH_REPO_RE`, `steps._SHA_RE`, `steps.short_sha_match`, `drain.drain_and_wait(client)`, `server.sessions.host_probe.default_fetch`, `DaemonClient(parse_endpoint(endpoint)).call(...)`.
- Produces:
  ```python
  SSH_OPTS = ("-oBatchMode=yes", "-oConnectTimeout=10")
  RUNNER_UNIT = "ccswitch-daemon.service"
  VERIFY_TIMEOUT_S = 90.0; VERIFY_INTERVAL_S = 3.0; DEFAULT_CONCURRENCY = 2

  @dataclass(frozen=True)
  class RunnerTarget:
      name: str; endpoint: str; enabled: bool; healthy: bool | None
      ssh_host: str | None; repo_path: str | None

  @dataclass
  class HostProgress:
      state: str = "pending"   # pending|running|done|failed|offline|disabled|unconfigured
      step: str | None = None; error: str | None = None; sha: str | None = None
      moved: bool = False      # checkout was advanced (rollback must rewind it)
      restore: dict | None = None
      def to_wire(self) -> dict: ...

  def remote_update_commands(repo_path: str, target_sha: str, *, deps_changed: bool) -> list[tuple[str, list[str]]]
  def remote_rewind_commands(repo_path: str, sha: str) -> list[tuple[str, list[str]]]
  def is_local(target: RunnerTarget, local_name: str) -> bool

  class RunnerDeploy:
      def __init__(self, target: RunnerTarget, *, local_name: str, run=subprocess.run,
                   client_factory: Callable[[str], Any] | None = None, fetch_info: Fetch | None = None,
                   drain_mod=drain, verify_timeout_s=VERIFY_TIMEOUT_S, verify_interval_s=VERIFY_INTERVAL_S,
                   on_progress: Callable[[str, HostProgress], None] | None = None)
      progress: HostProgress
      async def deploy(self, *, target_sha: str, daemon_changed: bool, deps_changed: bool,
                       local_daemon_enabled: bool) -> HostProgress          # never raises
      async def rewind(self, sha: str) -> None                                 # rollback; never raises

  async def deploy_runners(targets: list[RunnerTarget], *, target_sha, daemon_changed, deps_changed,
                           local_name, local_daemon_enabled, concurrency=DEFAULT_CONCURRENCY,
                           on_progress=None, **runner_kwargs) -> dict[str, HostProgress]
  async def rewind_runners(targets, results: dict[str, HostProgress], *, sha, local_name, **runner_kwargs) -> None
  async def load_runner_targets() -> list[RunnerTarget]                       # every Host row (all enabled states)
  ```

- [ ] **Step 1: Write the failing tests**

```python
# server/tests/test_deploy_runners.py
"""Per-runner deploy lifecycle (spec §2). The ssh argv sequence IS the contract."""
from __future__ import annotations
import asyncio
import subprocess
from server.deploy import runners as R

SSH = ["ssh", "-oBatchMode=yes", "-oConnectTimeout=10", "rocky"]
ROCKY = R.RunnerTarget(name="rocky", endpoint="https://rocky:9443", enabled=True, healthy=True,
                       ssh_host="rocky", repo_path="/home/tony/GitHub/ccswitch")
DOBBY = R.RunnerTarget(name="dobby", endpoint="https://dobby:9443", enabled=True, healthy=True,
                       ssh_host=None, repo_path=None)


def _recorder(calls, fail_on=None):
    def run(argv, **kw):
        calls.append(list(argv))
        rc = 1 if (fail_on and any(fail_on in a for a in argv)) else 0
        return subprocess.CompletedProcess(argv, rc, stdout="", stderr="boom")
    return run


class FakeClient:
    def __init__(self, calls):
        self.calls = calls
    async def call(self, method, **kw):
        self.calls.append(method)
        if method == "drain_status":
            return {"state": "ready"}
        if method == "restore_deploy":
            return {"restored": ["s1"], "failed": []}
        return {}
    async def aclose(self):
        pass


def _info(sha):
    async def fetch(endpoint):
        return {"sha": sha, "checkoutSha": sha}
    return fetch


def test_remote_update_commands_ff_only_and_conditional_pip():
    with_pip = R.remote_update_commands("/srv/cc", "abc1234", deps_changed=True)
    assert [s for s, _ in with_pip] == ["fetch", "merge", "pip", "sync"]
    assert with_pip[1][1] == ["git", "-C", "/srv/cc", "merge", "--ff-only", "abc1234"]
    assert with_pip[2][1] == ["/srv/cc/.venv/bin/python", "-m", "pip", "install", "-e",
                              "'/srv/cc[server]'", "--quiet"]
    assert [s for s, _ in R.remote_update_commands("/srv/cc", "abc1234", deps_changed=False)] == \
        ["fetch", "merge", "sync"]
    assert not any("reset" in a for _, argv in with_pip for a in argv)


def test_remote_rewind_uses_reset_hard():
    cmds = R.remote_rewind_commands("/srv/cc", "old0000")
    assert cmds[1][1] == ["git", "-C", "/srv/cc", "reset", "--hard", "old0000"]
    assert [s for s, _ in cmds] == ["fetch", "reset", "sync"]


def test_unsafe_values_are_refused_before_any_command():
    import pytest
    bad = R.RunnerTarget(name="x", endpoint="https://x", enabled=True, healthy=True,
                         ssh_host="-oProxyCommand=evil", repo_path="/srv/cc")
    calls: list = []
    rd = R.RunnerDeploy(bad, local_name="dobby", run=_recorder(calls), client_factory=lambda e: FakeClient([]),
                        fetch_info=_info("abc1234"))
    st = asyncio.run(rd.deploy(target_sha="abc1234", daemon_changed=True, deps_changed=False,
                               local_daemon_enabled=True))
    assert st.state == "failed" and "ssh_host" in (st.error or "")
    assert calls == []
    with pytest.raises(ValueError):
        R.remote_update_commands("/srv/cc", "not a sha", deps_changed=False)


def test_remote_daemon_changing_deploy_full_sequence():
    calls: list = []
    rpc: list = []
    rd = R.RunnerDeploy(ROCKY, local_name="dobby", run=_recorder(calls),
                        client_factory=lambda e: FakeClient(rpc), fetch_info=_info("abc1234"),
                        verify_interval_s=0)
    st = asyncio.run(rd.deploy(target_sha="abc1234", daemon_changed=True, deps_changed=True,
                               local_daemon_enabled=True))
    assert st.state == "done" and st.error is None and st.sha == "abc1234" and st.moved
    assert calls == [
        SSH + ["git", "-C", "/home/tony/GitHub/ccswitch", "fetch", "--quiet", "origin"],
        SSH + ["git", "-C", "/home/tony/GitHub/ccswitch", "merge", "--ff-only", "abc1234"],
        SSH + ["/home/tony/GitHub/ccswitch/.venv/bin/python", "-m", "pip", "install", "-e",
               "'/home/tony/GitHub/ccswitch[server]'", "--quiet"],
        SSH + ["sync"],
        SSH + ["sudo", "-n", "systemctl", "restart", "ccswitch-daemon.service"],
    ]
    # snapshot before the update, drain + close_events before the restart, restore after verify
    assert rpc == ["snapshot_deploy", "begin_drain", "drain_status", "close_events_connections",
                   "restore_deploy"]
    assert st.restore == {"restored": ["s1"], "failed": []}


def test_remote_edge_only_deploy_updates_checkout_without_restart():
    calls: list = []
    rpc: list = []
    rd = R.RunnerDeploy(ROCKY, local_name="dobby", run=_recorder(calls),
                        client_factory=lambda e: FakeClient(rpc),
                        fetch_info=_info("abc1234"), verify_interval_s=0)
    st = asyncio.run(rd.deploy(target_sha="abc1234", daemon_changed=False, deps_changed=False,
                               local_daemon_enabled=True))
    assert st.state == "done"
    assert [c[-1] for c in calls] == ["origin", "abc1234", "sync"]
    assert "begin_drain" not in rpc and "restore_deploy" not in rpc
    assert not any("systemctl" in c for c in calls)


def test_edge_only_verify_accepts_old_process_sha_with_new_checkout():
    async def fetch(endpoint):
        return {"sha": "old0000", "checkoutSha": "abc1234"}
    rd = R.RunnerDeploy(ROCKY, local_name="dobby", run=_recorder([]),
                        client_factory=lambda e: FakeClient([]), fetch_info=fetch, verify_interval_s=0)
    st = asyncio.run(rd.deploy(target_sha="abc1234", daemon_changed=False, deps_changed=False,
                               local_daemon_enabled=True))
    assert st.state == "done"


def test_daemon_changing_verify_requires_process_sha():
    async def fetch(endpoint):
        return {"sha": "old0000", "checkoutSha": "abc1234"}  # restarted into stale code
    rd = R.RunnerDeploy(ROCKY, local_name="dobby", run=_recorder([]),
                        client_factory=lambda e: FakeClient([]), fetch_info=fetch,
                        verify_timeout_s=0.05, verify_interval_s=0.01)
    st = asyncio.run(rd.deploy(target_sha="abc1234", daemon_changed=True, deps_changed=False,
                               local_daemon_enabled=True))
    assert st.state == "failed" and st.step == "verify"


def test_local_runner_restarts_locally_and_never_touches_the_checkout():
    calls: list = []
    rpc: list = []
    rd = R.RunnerDeploy(DOBBY, local_name="dobby", run=_recorder(calls),
                        client_factory=lambda e: FakeClient(rpc), fetch_info=_info("abc1234"),
                        verify_interval_s=0)
    st = asyncio.run(rd.deploy(target_sha="abc1234", daemon_changed=True, deps_changed=True,
                               local_daemon_enabled=True))
    assert st.state == "done" and st.moved is False
    assert calls == [["sudo", "systemctl", "restart", "ccswitch-daemon.service"]]
    assert "begin_drain" in rpc


def test_local_runner_with_daemon_unit_disabled_is_skipped_not_failed():
    calls: list = []
    rd = R.RunnerDeploy(DOBBY, local_name="dobby", run=_recorder(calls),
                        client_factory=lambda e: FakeClient([]), fetch_info=_info("abc1234"))
    st = asyncio.run(rd.deploy(target_sha="abc1234", daemon_changed=True, deps_changed=False,
                               local_daemon_enabled=False))
    assert st.state == "disabled" and calls == []


def test_remote_without_ssh_config_is_unconfigured():
    t = R.RunnerTarget(name="ghost", endpoint="https://ghost:9443", enabled=True, healthy=True,
                       ssh_host=None, repo_path=None)
    rd = R.RunnerDeploy(t, local_name="dobby", run=_recorder([]), client_factory=lambda e: FakeClient([]),
                        fetch_info=_info("abc1234"))
    st = asyncio.run(rd.deploy(target_sha="abc1234", daemon_changed=True, deps_changed=False,
                               local_daemon_enabled=True))
    assert st.state == "unconfigured" and "ssh_host" in (st.error or "")


def test_offline_and_disabled_targets_are_skipped_by_deploy_runners():
    off = R.RunnerTarget(name="off", endpoint="https://off", enabled=True, healthy=False,
                         ssh_host="off", repo_path="/srv/cc")
    unk = R.RunnerTarget(name="unk", endpoint="https://unk", enabled=True, healthy=None,
                         ssh_host="unk", repo_path="/srv/cc")
    dis = R.RunnerTarget(name="dis", endpoint="https://dis", enabled=False, healthy=True,
                         ssh_host="dis", repo_path="/srv/cc")
    calls: list = []
    res = asyncio.run(R.deploy_runners(
        [off, unk, dis], target_sha="abc1234", daemon_changed=True, deps_changed=False,
        local_name="dobby", local_daemon_enabled=True, run=_recorder(calls),
        client_factory=lambda e: FakeClient([]), fetch_info=_info("abc1234")))
    assert {n: p.state for n, p in res.items()} == {"off": "offline", "unk": "offline", "dis": "disabled"}
    assert calls == []


def test_failed_runner_does_not_stop_the_others_and_concurrency_is_capped():
    hosts = [R.RunnerTarget(name=f"h{i}", endpoint=f"https://h{i}", enabled=True, healthy=True,
                            ssh_host=f"h{i}", repo_path="/srv/cc") for i in range(4)]
    in_flight = [0]; peak = [0]

    async def fetch(endpoint):
        return {"sha": "abc1234", "checkoutSha": "abc1234"}

    def run(argv, **kw):
        # every remote step of h2 fails at merge; the others succeed
        rc = 1 if ("h2" in argv and "--ff-only" in argv) else 0
        return subprocess.CompletedProcess(argv, rc, stdout="", stderr="boom")

    class SlowClient(FakeClient):
        async def call(self, method, **kw):
            if method == "begin_drain":
                in_flight[0] += 1; peak[0] = max(peak[0], in_flight[0])
                await asyncio.sleep(0.02)
                in_flight[0] -= 1
            return await super().call(method, **kw)

    res = asyncio.run(R.deploy_runners(
        hosts, target_sha="abc1234", daemon_changed=True, deps_changed=False, local_name="dobby",
        local_daemon_enabled=True, concurrency=2, run=run, client_factory=lambda e: SlowClient([]),
        fetch_info=fetch, verify_interval_s=0))
    assert res["h2"].state == "failed" and res["h2"].step == "merge"
    assert all(res[n].state == "done" for n in ("h0", "h1", "h3"))
    assert peak[0] <= 2


def test_rewind_only_moved_hosts_and_uses_reset_hard():
    calls: list = []
    res = {"rocky": R.HostProgress(state="done", moved=True),
           "other": R.HostProgress(state="failed", moved=False)}
    other = R.RunnerTarget(name="other", endpoint="https://o", enabled=True, healthy=True,
                           ssh_host="other", repo_path="/srv/cc")
    asyncio.run(R.rewind_runners([ROCKY, other], res, sha="old0000", local_name="dobby",
                                 run=_recorder(calls), client_factory=lambda e: FakeClient([]),
                                 fetch_info=_info("old0000"), verify_interval_s=0))
    joined = [" ".join(c) for c in calls]
    assert any("reset --hard old0000" in j and "rocky" in j for j in joined)
    assert not any("other" in j for j in joined)
    assert joined[-1].endswith("sudo -n systemctl restart ccswitch-daemon.service")


def test_progress_callback_sees_each_step():
    seen: list = []
    rd = R.RunnerDeploy(ROCKY, local_name="dobby", run=_recorder([]),
                        client_factory=lambda e: FakeClient([]), fetch_info=_info("abc1234"),
                        verify_interval_s=0, on_progress=lambda name, p: seen.append((name, p.state, p.step)))
    asyncio.run(rd.deploy(target_sha="abc1234", daemon_changed=True, deps_changed=False,
                          local_daemon_enabled=True))
    steps = [s for _, _, s in seen]
    for expected in ("snapshot", "fetch", "merge", "sync", "drain", "restart", "verify", "restore"):
        assert expected in steps
    assert seen[-1][1] == "done"
```

- [ ] **Step 2: Run to verify they fail**

Run: `CCSWITCH_DEV=1 PYTHONPATH=$PWD/src /home/tony/GitHub/ccswitch/.venv/bin/python -m pytest server/tests/test_deploy_runners.py -q`
Expected: FAIL — `ImportError: cannot import name 'runners'`.

- [ ] **Step 3: Write `server/deploy/runners.py`**

```python
"""Per-runner deploy lifecycle (spec §2): snapshot → update checkout → (drain, restart)
→ verify → restore, for every host in the fleet registry, local or ssh-reachable.

Transport rules (constitution IV — everything in ssh argv is validated first):
  * remote checkouts move by ``merge --ff-only <sha>`` only; ``reset --hard`` exists solely
    for the rollback rewind;
  * pip is ``<repo>/.venv/bin/python -m pip`` (never ``bin/pip``, #860) and only when the
    lockfile changed;
  * ``sync`` after every checkout write, before the restart (2026-08-29 zero-byte module).

A runner is restarted ONLY on a daemon-changing deploy: ``steps._EDGE_ONLY`` guarantees
an edge-only diff touched no daemon-imported Python, so the running process already
equals the new checkout's daemon code. The checkout is updated on EVERY deploy so
``/info.checkoutSha`` matches the deployed commit and the skew gate (spec §4) stays green.

``deploy_runners`` runs at most ``concurrency`` runners at once (decision 2a: 2) and
isolates failures — one runner's failure never stops another (decision 1a).
"""
from __future__ import annotations

import asyncio
import logging
import subprocess
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from server.deploy import drain as _drain
from server.deploy.steps import SSH_HOST_RE, SSH_REPO_RE, _SHA_RE, short_sha_match

log = logging.getLogger("server.deploy.runners")

Run = Callable[..., subprocess.CompletedProcess]
Fetch = Callable[[str], Awaitable[dict]]
Progress = Callable[[str, "HostProgress"], None]

SSH_OPTS = ("-oBatchMode=yes", "-oConnectTimeout=10")
RUNNER_UNIT = "ccswitch-daemon.service"
VERIFY_TIMEOUT_S = 90.0
VERIFY_INTERVAL_S = 3.0
DEFAULT_CONCURRENCY = 2

SKIPPED_STATES = frozenset({"offline", "disabled"})
FAILED_STATES = frozenset({"failed", "unconfigured"})


@dataclass(frozen=True)
class RunnerTarget:
    name: str
    endpoint: str
    enabled: bool
    healthy: bool | None
    ssh_host: str | None
    repo_path: str | None


@dataclass
class HostProgress:
    state: str = "pending"        # pending | running | done | failed | offline | disabled | unconfigured
    step: str | None = None
    error: str | None = None
    sha: str | None = None
    moved: bool = False           # checkout advanced — a rollback must rewind it
    restore: dict | None = None

    def to_wire(self) -> dict:
        return {"state": self.state, "step": self.step, "error": self.error, "sha": self.sha,
                "restore": self.restore}


def _venv_python(repo_path: str) -> str:
    return f"{repo_path}/.venv/bin/python"


def _validate(repo_path: str, sha: str) -> None:
    if not SSH_REPO_RE.match(repo_path):
        raise ValueError(f"repo_path {repo_path!r} is not a safe absolute path")
    if not _SHA_RE.match(sha):
        raise ValueError(f"sha {sha!r} is not a hex git sha")


def remote_update_commands(repo_path: str, target_sha: str, *,
                           deps_changed: bool) -> list[tuple[str, list[str]]]:
    """Ordered (step, remote argv) to bring a runner's checkout to *target_sha*."""
    _validate(repo_path, target_sha)
    cmds: list[tuple[str, list[str]]] = [
        ("fetch", ["git", "-C", repo_path, "fetch", "--quiet", "origin"]),
        ("merge", ["git", "-C", repo_path, "merge", "--ff-only", target_sha]),
    ]
    if deps_changed:
        # Single-quoted: ssh joins argv into one remote shell line and `[server]` is a
        # glob there. The path is SSH_REPO_RE-validated, so the quotes are inert.
        cmds.append(("pip", [_venv_python(repo_path), "-m", "pip", "install", "-e",
                             f"'{repo_path}[server]'", "--quiet"]))
    cmds.append(("sync", ["sync"]))
    return cmds


def remote_rewind_commands(repo_path: str, sha: str) -> list[tuple[str, list[str]]]:
    """Rollback only: move a runner BACK to *sha* (ff-only cannot go backwards)."""
    _validate(repo_path, sha)
    return [
        ("fetch", ["git", "-C", repo_path, "fetch", "--quiet", "origin"]),
        ("reset", ["git", "-C", repo_path, "reset", "--hard", sha]),
        ("sync", ["sync"]),
    ]


REMOTE_RESTART = ["sudo", "-n", "systemctl", "restart", RUNNER_UNIT]
LOCAL_RESTART = ["sudo", "systemctl", "restart", RUNNER_UNIT]


def is_local(target: RunnerTarget, local_name: str) -> bool:
    """The daemon on THIS host: its checkout is the controller's own repo (already updated
    by the `update` phase) and its unit restarts without ssh."""
    return target.ssh_host is None and target.name == local_name


def _default_client_factory(endpoint: str) -> Any:
    from server.ipc.client import DaemonClient
    from server.ipc.paths import parse_endpoint
    return DaemonClient(parse_endpoint(endpoint))


def _default_fetch_info() -> Fetch:
    from server.sessions.host_probe import default_fetch
    return default_fetch


class RunnerDeploy:
    def __init__(self, target: RunnerTarget, *, local_name: str, run: Run = subprocess.run,
                 client_factory: Callable[[str], Any] | None = None,
                 fetch_info: Fetch | None = None, drain_mod=_drain,
                 verify_timeout_s: float = VERIFY_TIMEOUT_S,
                 verify_interval_s: float = VERIFY_INTERVAL_S,
                 on_progress: Progress | None = None):
        self.target = target
        self.local_name = local_name
        self._run = run
        self._client_factory = client_factory or _default_client_factory
        self._fetch_info = fetch_info or _default_fetch_info()
        self._drain = drain_mod
        self._verify_timeout_s = verify_timeout_s
        self._verify_interval_s = verify_interval_s
        self._on_progress = on_progress
        self.progress = HostProgress()

    # ---- bookkeeping -------------------------------------------------------------
    def _mark(self, state: str | None = None, step: str | None = None, **kw: Any) -> None:
        if state is not None:
            self.progress.state = state
        self.progress.step = step
        for k, v in kw.items():
            setattr(self.progress, k, v)
        if self._on_progress is not None:
            self._on_progress(self.target.name, self.progress)

    # ---- transport ---------------------------------------------------------------
    async def _ssh(self, step: str, remote: list[str]) -> None:
        argv = ["ssh", *SSH_OPTS, self.target.ssh_host, *remote]
        r = await asyncio.to_thread(self._run, argv, capture_output=True, text=True)
        if r.returncode != 0:
            detail = (r.stderr or r.stdout or "").strip()[:500]
            raise RuntimeError(f"{step} failed (rc={r.returncode}): {detail}")

    async def _local(self, step: str, argv: list[str]) -> None:
        r = await asyncio.to_thread(self._run, argv, capture_output=True, text=True)
        if r.returncode != 0:
            detail = (r.stderr or r.stdout or "").strip()[:500]
            raise RuntimeError(f"{step} failed (rc={r.returncode}): {detail}")

    async def _rpc(self, method: str) -> dict:
        client = self._client_factory(self.target.endpoint)
        try:
            return await client.call(method)
        finally:
            closer = getattr(client, "aclose", None)
            if closer is not None:
                try:
                    await closer()
                except Exception:
                    log.debug("runner %s: client teardown failed", self.target.name, exc_info=True)

    async def _drain_then(self) -> None:
        """Drain in-flight turns (bounded by the deploy caps) and drop the edge's /events
        socket so the old daemon can exit on SIGTERM promptly (#740). Best-effort."""
        self._mark(step="drain")
        client = self._client_factory(self.target.endpoint)
        try:
            result = await self._drain.drain_and_wait(client)
        finally:
            closer = getattr(client, "aclose", None)
            if closer is not None:
                await closer()
        if result != "ready":
            log.warning("runner %s: drain %s — restarting anyway", self.target.name, result)
        try:
            await self._rpc("close_events_connections")
        except Exception:
            log.debug("runner %s: close_events_connections failed (best-effort)",
                      self.target.name, exc_info=True)

    async def _verify(self, target_sha: str, *, require_process: bool) -> str:
        """Poll /info until the checkout (and, after a restart, the process) is on target."""
        self._mark(step="verify")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._verify_timeout_s
        while True:
            try:
                info = await self._fetch_info(self.target.endpoint)
                checkout = info.get("checkoutSha") or info.get("sha")
                process = info.get("sha")
                ok = bool(checkout) and short_sha_match(str(checkout), target_sha)
                if ok and require_process:
                    ok = bool(process) and short_sha_match(str(process), target_sha)
                if ok:
                    return str(process or checkout)
            except Exception:
                pass  # daemon still restarting — keep polling until the deadline
            if loop.time() >= deadline:
                raise RuntimeError(
                    f"verify failed: daemon did not report {target_sha} within "
                    f"{int(self._verify_timeout_s)}s")
            await asyncio.sleep(self._verify_interval_s)

    # ---- the lifecycle -----------------------------------------------------------
    async def deploy(self, *, target_sha: str, daemon_changed: bool, deps_changed: bool,
                     local_daemon_enabled: bool) -> HostProgress:
        t = self.target
        if not t.enabled:
            self._mark("disabled")
            return self.progress
        if t.healthy is not True:
            self._mark("offline")
            return self.progress
        local = is_local(t, self.local_name)
        if local and not local_daemon_enabled:
            self._mark("disabled")
            return self.progress
        if not local:
            if not t.ssh_host or not t.repo_path:
                self._mark("unconfigured",
                           error=f"host {t.name!r} has no ssh_host/repo_path — cannot update it")
                return self.progress
            if not SSH_HOST_RE.match(t.ssh_host):
                self._mark("failed", error=f"ssh_host {t.ssh_host!r} is not a valid [user@]host")
                return self.progress
        try:
            self._mark("running", step="snapshot")
            try:
                await self._rpc("snapshot_deploy")
            except Exception:
                log.warning("runner %s: session snapshot failed (continuing)", t.name, exc_info=True)

            if not local:
                for step, remote in remote_update_commands(t.repo_path, target_sha,
                                                           deps_changed=deps_changed):
                    self._mark(step=step)
                    await self._ssh(step, remote)
                    if step == "merge":
                        self.progress.moved = True

            if daemon_changed:
                await self._drain_then()
                self._mark(step="restart")
                if local:
                    await self._local("restart", LOCAL_RESTART)
                else:
                    await self._ssh("restart", REMOTE_RESTART)

            sha = await self._verify(target_sha, require_process=daemon_changed)
            self.progress.sha = sha

            if daemon_changed:
                self._mark(step="restore")
                try:
                    self.progress.restore = await self._rpc("restore_deploy")
                except Exception:
                    log.warning("runner %s: restore_deploy failed", t.name, exc_info=True)
                    self.progress.restore = {"restored": [], "failed": [], "error": "restore call failed"}
            self._mark("done")
            log.info("deploy.runner.done host=%s sha=%s restarted=%s", t.name, sha, daemon_changed)
        except Exception as exc:  # noqa: BLE001 — a runner failure is a REPORTED state
            self._mark("failed", step=self.progress.step, error=str(exc)[:500])
            log.warning("deploy.runner.failed host=%s step=%s: %s", t.name, self.progress.step, exc)
        return self.progress

    async def rewind(self, sha: str) -> None:
        """Rollback: put a moved remote runner back on *sha* and restart it. Never raises."""
        t = self.target
        if is_local(t, self.local_name) or not t.ssh_host or not t.repo_path:
            return
        try:
            for step, remote in remote_rewind_commands(t.repo_path, sha):
                self._mark("running", step=f"rewind:{step}")
                await self._ssh(step, remote)
            self._mark(step="rewind:restart")
            await self._ssh("restart", REMOTE_RESTART)
            await self._verify(sha, require_process=True)
            self._mark("done", sha=sha)
        except Exception as exc:  # noqa: BLE001
            self._mark("failed", error=f"rewind failed: {exc}"[:500])
            log.error("deploy.runner.rewind_failed host=%s: %s", t.name, exc)


async def deploy_runners(targets: list[RunnerTarget], *, target_sha: str, daemon_changed: bool,
                         deps_changed: bool, local_name: str, local_daemon_enabled: bool,
                         concurrency: int = DEFAULT_CONCURRENCY,
                         on_progress: Progress | None = None,
                         **runner_kwargs: Any) -> dict[str, HostProgress]:
    """Deploy every target, at most *concurrency* at a time. Returns name → progress."""
    sem = asyncio.Semaphore(max(1, concurrency))
    deploys = {t.name: RunnerDeploy(t, local_name=local_name, on_progress=on_progress,
                                    **runner_kwargs) for t in targets}

    async def one(rd: RunnerDeploy) -> None:
        async with sem:
            await rd.deploy(target_sha=target_sha, daemon_changed=daemon_changed,
                            deps_changed=deps_changed, local_daemon_enabled=local_daemon_enabled)

    await asyncio.gather(*(one(rd) for rd in deploys.values()))
    return {name: rd.progress for name, rd in deploys.items()}


async def rewind_runners(targets: list[RunnerTarget], results: dict[str, HostProgress], *,
                         sha: str, local_name: str, **runner_kwargs: Any) -> None:
    """Rollback helper: rewind every runner whose checkout this deploy moved."""
    moved = [t for t in targets if results.get(t.name) is not None and results[t.name].moved]
    await asyncio.gather(*(RunnerDeploy(t, local_name=local_name, **runner_kwargs).rewind(sha)
                           for t in moved))


async def load_runner_targets() -> list[RunnerTarget]:
    """Every fleet host row (enabled or not — the lifecycle reports `disabled` itself)."""
    from sqlalchemy import select as sa_select
    from server.db import db_session
    from server.db.models import Host
    async with db_session() as db:
        rows = (await db.execute(sa_select(Host).order_by(Host.name))).scalars().all()
    return [RunnerTarget(name=r.name, endpoint=r.endpoint, enabled=bool(r.enabled),
                         healthy=r.healthy, ssh_host=r.ssh_host, repo_path=r.repo_path)
            for r in rows]
```

- [ ] **Step 4: Run tests**

Run: `CCSWITCH_DEV=1 PYTHONPATH=$PWD/src /home/tony/GitHub/ccswitch/.venv/bin/python -m pytest server/tests/test_deploy_runners.py -q`
Expected: PASS (13 tests).

- [ ] **Step 5: Commit**

```bash
git add server/deploy/runners.py server/tests/test_deploy_runners.py
git commit -m "feat(deploy): RunnerDeploy — per-runner snapshot/update/restart/verify/restore lifecycle"
```

---

### Task 6: Consolidate the two remote-update paths onto `RunnerDeploy`

**Files:**
- Modify: `server/sessions/host_upgrade.py` (whole module)
- Modify: `server/deploy/steps.py:283-420` (`restart_units` → `restart_edge`), `:441-450` (`remote_daemon_configured` → `remote_runners_configured`), `:643-675` (`rollback_to`)
- Modify: `server/api/fleet.py:195-238` (upgrade endpoint passes the target)
- Modify: `server/app.py:185-200` (seed `ssh_host`/`repo_path` from env once)
- Modify/rewrite: `server/tests/test_fleet_upgrade.py:190-300`, `server/tests/test_deploy_remote_restart.py`, `server/tests/test_deploy_steps_restart_health.py` (the `remote_daemon_configured` env test)
- Test (new): `server/tests/test_fleet_seed_ssh_from_env.py`

**Interfaces:**
- Consumes: `server.deploy.runners.RunnerDeploy`, `RunnerTarget`, `remote_update_commands`; `server.deploy.state.read_deploy_state`.
- Produces:
  ```python
  # steps.py
  def restart_edge(*, run: Run = subprocess.run) -> list[str]       # ["ccswitch.service"]
  def remote_runners_configured(hosts: list | None = None) -> bool  # any enabled non-local host with ssh_host
  def rollback_to(repo, sha, *, daemon_changed, daemon_enabled, run=subprocess.run) -> None  # edge + LOCAL daemon only; the controller rewinds remotes via runners.rewind_runners
  # host_upgrade.py
  async def run_upgrade(name, *, ssh_host, repo_path, endpoint, run=subprocess.run, fetch_info=None,
                        resolve_target: Callable[[], Awaitable[str]] | None = None, **kw) -> dict
  async def default_target_sha() -> str     # deploy_state.deployed_short, else origin/master (fresh fetch)
  ```

- [ ] **Step 1: Write the failing tests**

Replace the three `run_upgrade` tests in `server/tests/test_fleet_upgrade.py` (lines ~202-290) with:

```python
_SSH_PREFIX = ["ssh", "-oBatchMode=yes", "-oConnectTimeout=10", "deploy@rocky"]


def _recorder(calls, fail_on: str | None = None):
    def run(argv, **kw):
        calls.append(argv)
        rc = 1 if (fail_on and fail_on in argv) else 0
        return subprocess.CompletedProcess(argv, rc, stdout="", stderr="boom")
    return run


class _Client:
    async def call(self, method, **kw):
        return {"state": "ready"} if method == "drain_status" else {}
    async def aclose(self):
        pass


def _fetch(sha):
    async def f(endpoint):
        return {"sha": sha, "checkoutSha": sha}
    return f


async def _target():
    return "def5678"


def test_run_upgrade_targets_the_deployed_sha_and_runs_the_shared_sequence():
    """The Fleet UI button is the SAME RunnerDeploy the deploy fan-out uses: an explicit
    target sha (the deployed one, never a bare origin/master), ff-only, sync before restart."""
    calls: list[list[str]] = []

    async def run():
        return await host_upgrade.run_upgrade(
            "rocky", ssh_host="deploy@rocky", repo_path="/srv/ccswitch",
            endpoint="https://rocky:9443", run=_recorder(calls), fetch_info=_fetch("def5678"),
            resolve_target=_target, client_factory=lambda e: _Client(), verify_interval_s=0)

    st = asyncio.run(run())
    assert st["state"] == "done" and st["error"] is None and st["target"] == "def5678"
    assert calls == [
        _SSH_PREFIX + ["git", "-C", "/srv/ccswitch", "fetch", "--quiet", "origin"],
        _SSH_PREFIX + ["git", "-C", "/srv/ccswitch", "merge", "--ff-only", "def5678"],
        _SSH_PREFIX + ["/srv/ccswitch/.venv/bin/python", "-m", "pip", "install", "-e",
                       "'/srv/ccswitch[server]'", "--quiet"],
        _SSH_PREFIX + ["sync"],
        _SSH_PREFIX + ["sudo", "-n", "systemctl", "restart", "ccswitch-daemon.service"],
    ]
    assert not any("bin/pip" in arg for call in calls for arg in call)


def test_run_upgrade_failure_records_step_and_error():
    calls: list[list[str]] = []

    async def run():
        return await host_upgrade.run_upgrade(
            "rocky", ssh_host="deploy@rocky", repo_path="/srv/ccswitch",
            endpoint="https://rocky:9443", run=_recorder(calls, fail_on="--ff-only"),
            fetch_info=_fetch("def5678"), resolve_target=_target,
            client_factory=lambda e: _Client())

    st = asyncio.run(run())
    assert st["state"] == "failed" and st["step"] == "merge"
    assert "merge failed" in st["error"] and "boom" in st["error"]
    assert len(calls) == 2


def test_run_upgrade_fails_when_sha_never_matches():
    async def run():
        return await host_upgrade.run_upgrade(
            "rocky", ssh_host="deploy@rocky", repo_path="/srv/ccswitch",
            endpoint="https://rocky:9443", run=_recorder([]), fetch_info=_fetch("abc0000"),
            resolve_target=_target, client_factory=lambda e: _Client(),
            verify_timeout_s=0.05, verify_interval_s=0.01)

    st = asyncio.run(run())
    assert st["state"] == "failed" and st["step"] == "verify"


def test_default_target_prefers_deploy_state(tmp_path):
    from server.db import init_db
    from server.deploy.state import record_deploy

    async def run():
        await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / 't.db'}")
        await record_deploy(sha="c" * 40, short="ccccccc", daemon_changed=True)
        return await host_upgrade.default_target_sha(resolve_origin=lambda: "zzzzzzz")

    assert asyncio.run(run()) == "ccccccc"


def test_default_target_falls_back_to_origin_master_without_deploy_state(tmp_path):
    from server.db import init_db

    async def run():
        await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / 'u.db'}")
        return await host_upgrade.default_target_sha(resolve_origin=lambda: "zzzzzzz")

    assert asyncio.run(run()) == "zzzzzzz"
```

Replace `server/tests/test_deploy_remote_restart.py` entirely with:

```python
"""restart_edge: the edge restart is LOCAL and edge-only. Runner restarts live in
server.deploy.runners (test_deploy_runners.py) — the ssh branch that used to live here
(CCSWITCH_DAEMON_SSH_HOST) is gone, and steps must never build an ssh command again."""
from server.deploy import steps


def _recorder(calls):
    def run(cmd, **kw):
        calls.append(list(cmd))
        class R:
            returncode = 0
            stdout = b""
            stderr = b""
        return R()
    return run


def test_restart_edge_restarts_only_the_edge_unit(monkeypatch):
    monkeypatch.setenv("CCSWITCH_DAEMON_SSH_HOST", "tony@rocky")   # must be ignored now
    calls: list[list[str]] = []
    units = steps.restart_edge(run=_recorder(calls))
    assert calls == [["sudo", "systemctl", "restart", "ccswitch.service"]]
    assert units == ["ccswitch.service"]


def test_steps_has_no_restart_units_anymore():
    assert not hasattr(steps, "restart_units")


def test_rollback_to_resets_edge_and_restarts_local_daemon_when_changed(tmp_path):
    calls: list[list[str]] = []
    steps.rollback_to(str(tmp_path), "abc1234", daemon_changed=True, daemon_enabled=True,
                      run=_recorder(calls))
    joined = [" ".join(c) for c in calls]
    assert joined[0] == f"git -C {tmp_path} reset --hard abc1234"
    assert "sudo systemctl restart ccswitch-daemon.service" in joined
    assert joined[-1] == "sudo systemctl restart ccswitch.service"
    assert not any(j.startswith("ssh") for j in joined)


def test_remote_runners_configured_reads_the_fleet_not_the_env(monkeypatch):
    from server.deploy.runners import RunnerTarget
    monkeypatch.setenv("CCSWITCH_DAEMON_SSH_HOST", "tony@rocky")
    local = RunnerTarget(name="dobby", endpoint="https://d", enabled=True, healthy=True,
                         ssh_host=None, repo_path=None)
    remote = RunnerTarget(name="rocky", endpoint="https://r", enabled=True, healthy=True,
                          ssh_host="rocky", repo_path="/srv/cc")
    assert steps.remote_runners_configured([local]) is False
    assert steps.remote_runners_configured([local, remote]) is True
    assert steps.remote_runners_configured([]) is False
```

New `server/tests/test_fleet_seed_ssh_from_env.py`:

```python
"""CCSWITCH_DAEMON_SSH_HOST/_REPO used to be the deploy path; now they only seed the
matching Host row's ssh_host/repo_path once, so an existing install keeps working."""
import asyncio
from sqlalchemy import select as sa_select


def test_seed_fills_null_ssh_columns_only(tmp_path, monkeypatch):
    from server.db import db_session, init_db
    from server.db.models import Host
    from server.app import seed_host_ssh_from_env

    monkeypatch.setenv("CCSWITCH_DAEMON_SSH_HOST", "tony@rocky")
    monkeypatch.setenv("CCSWITCH_DAEMON_SSH_REPO", "/home/tony/GitHub/ccswitch")

    async def run():
        await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / 's.db'}")
        async with db_session() as db:
            db.add(Host(name="rocky", endpoint="https://rocky:9443"))
            db.add(Host(name="other", endpoint="https://o:9443", ssh_host="keep", repo_path="/keep"))
        await seed_host_ssh_from_env()
        async with db_session() as db:
            rows = (await db.execute(sa_select(Host).order_by(Host.name))).scalars().all()
        return {r.name: (r.ssh_host, r.repo_path) for r in rows}

    out = asyncio.run(run())
    assert out["rocky"] == ("tony@rocky", "/home/tony/GitHub/ccswitch")
    assert out["other"] == ("keep", "/keep")


def test_seed_is_noop_without_env_or_matching_host(tmp_path, monkeypatch):
    from server.db import db_session, init_db
    from server.db.models import Host
    from server.app import seed_host_ssh_from_env

    monkeypatch.setenv("CCSWITCH_DAEMON_SSH_HOST", "tony@ghost")
    monkeypatch.setenv("CCSWITCH_DAEMON_SSH_REPO", "/srv/cc")

    async def run():
        await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / 'n.db'}")
        async with db_session() as db:
            db.add(Host(name="rocky", endpoint="https://rocky:9443"))
        await seed_host_ssh_from_env()
        async with db_session() as db:
            return (await db.execute(sa_select(Host.ssh_host).where(Host.name == "rocky"))).scalar_one()

    assert asyncio.run(run()) is None
```

- [ ] **Step 2: Run to verify they fail**

Run: `CCSWITCH_DEV=1 PYTHONPATH=$PWD/src /home/tony/GitHub/ccswitch/.venv/bin/python -m pytest server/tests/test_fleet_upgrade.py server/tests/test_deploy_remote_restart.py server/tests/test_fleet_seed_ssh_from_env.py -q`
Expected: FAIL — `restart_edge` missing, `run_upgrade() got an unexpected keyword argument 'client_factory'`, `seed_host_ssh_from_env` missing.

- [ ] **Step 3: Rewrite `host_upgrade.py` as a wrapper**

```python
"""Upgrade one remote session-runner from the Fleet UI, on demand.

The Fleet "Update" button and the deploy fan-out share ONE implementation —
``server.deploy.runners.RunnerDeploy`` — so a manual upgrade and a deploy can never
disagree about how a runner is brought to a commit. This module only resolves the
target (the fleet's deployed sha, see ``server.deploy.state``) and keeps the in-process
progress dict the ``/api/fleet/hosts`` list polls.
"""
from __future__ import annotations

import asyncio
import logging
import subprocess
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from server.deploy import git as deploy_git
from server.deploy.runners import RunnerDeploy, RunnerTarget
from server.version import repo_root

log = logging.getLogger(__name__)

__all__ = ["start_upgrade", "run_upgrade", "upgrade_wire", "is_running", "default_target_sha"]

_STATE: dict[str, dict[str, Any]] = {}


def _reset_state_for_tests() -> None:
    _STATE.clear()


def is_running(name: str) -> bool:
    st = _STATE.get(name)
    return bool(st and st.get("state") == "running")


def upgrade_wire(name: str) -> dict[str, Any]:
    st = _STATE.get(name)
    if not st:
        return {"state": "idle", "step": None, "error": None, "startedAt": None, "target": None}
    return {"state": st["state"], "step": st["step"], "error": st["error"],
            "startedAt": st["started_at"], "target": st["target"]}


def _origin_master_short(run=subprocess.run) -> str:
    deploy_git.fetch(repo_root(), run=run)
    return deploy_git.short_sha(repo_root(), "origin/master", run=run)


async def default_target_sha(resolve_origin: Callable[[], str] = _origin_master_short) -> str:
    """The fleet's deployed sha (deploy_state), else origin/master's tip for a fleet that
    has never recorded a deploy. Never a commit AHEAD of what the edge runs."""
    from server.deploy.state import read_deploy_state
    state = await read_deploy_state()
    if state is not None:
        return state.deployed_short
    return await asyncio.to_thread(resolve_origin)


async def run_upgrade(name: str, *, ssh_host: str, repo_path: str, endpoint: str,
                      run=subprocess.run, fetch_info=None,
                      resolve_target: Callable[[], Awaitable[str]] | None = None,
                      **runner_kwargs: Any) -> dict[str, Any]:
    """Run the whole upgrade for one host; never raises (state records failure)."""
    st = _STATE.setdefault(name, {})
    st.update(state="running", step="fetch", error=None, target=None,
              started_at=datetime.now(timezone.utc).isoformat())
    try:
        target = await (resolve_target() if resolve_target else default_target_sha())
        st["target"] = target
        rt = RunnerTarget(name=name, endpoint=endpoint, enabled=True, healthy=True,
                          ssh_host=ssh_host, repo_path=repo_path)

        def on_progress(_n: str, p) -> None:
            st.update(state=p.state if p.state in ("running", "done", "failed") else "failed",
                      step=p.step, error=p.error)

        rd = RunnerDeploy(rt, local_name="", run=run, fetch_info=fetch_info,
                          on_progress=on_progress, **runner_kwargs)
        # A manual upgrade always restarts (the operator asked for the new code to RUN) and
        # always syncs deps (it cannot know what changed since the runner's last update).
        p = await rd.deploy(target_sha=target, daemon_changed=True, deps_changed=True,
                            local_daemon_enabled=True)
        if p.state == "done":
            st.update(state="done", step=None)
            log.info("fleet.upgrade_done host=%s sha=%s", name, p.sha)
        else:
            st.update(state="failed", step=p.step, error=p.error)
            log.warning("fleet.upgrade_failed host=%s step=%s: %s", name, p.step, p.error)
        return st
    except Exception as exc:  # noqa: BLE001
        st.update(state="failed", error=str(exc)[:500])
        log.warning("fleet.upgrade_failed host=%s: %s", name, exc)
        return st


def start_upgrade(name: str, *, ssh_host: str, repo_path: str, endpoint: str, **kw: Any) -> bool:
    if is_running(name):
        return False
    _STATE.setdefault(name, {}).update(
        state="running", step="fetch", error=None, target=None,
        started_at=datetime.now(timezone.utc).isoformat())
    task = asyncio.get_running_loop().create_task(
        run_upgrade(name, ssh_host=ssh_host, repo_path=repo_path, endpoint=endpoint, **kw))
    _STATE[name]["_task"] = task
    return True
```

(`server/api/fleet.py` `upgrade_host` needs no change — it passes `ssh_host/repo_path/endpoint`.)

- [ ] **Step 4: `steps.py`: `restart_edge`, `remote_runners_configured`, edge-local `rollback_to`**

Replace `restart_units` (lines 291-419) with:

```python
def restart_edge(*, run: Run = subprocess.run) -> list[str]:
    """Restart the edge unit. Runner (daemon) restarts — local AND remote — are owned by
    ``server.deploy.runners`` and run BEFORE this (daemon-before-edge order is load-bearing:
    the browser reconnect-flush race, #190). Returns the units restarted."""
    run(["sudo", "systemctl", "restart", "ccswitch.service"], check=True, capture_output=True)
    return ["ccswitch.service"]


def restart_local_daemon(*, run: Run = subprocess.run) -> None:
    run(["sudo", "systemctl", "restart", "ccswitch-daemon.service"], check=True, capture_output=True)
```

Replace `remote_daemon_configured` with:

```python
def remote_runners_configured(hosts=None) -> bool:
    """True when the fleet has an enabled REMOTE runner (a host with ``ssh_host``).

    Sizes the health window: after its own restart the edge must re-attach to remote
    daemons over a settling tailnet even on an edge-only deploy (#780). *hosts* is the
    ``load_runner_targets()`` list; None → False (single-host install).
    """
    return any(h.enabled and h.ssh_host for h in (hosts or []))
```

Rewrite `rollback_to`:

```python
def rollback_to(repo: str, sha: str, *, daemon_changed: bool, daemon_enabled: bool,
                run: Run = subprocess.run) -> None:
    """Reset the EDGE checkout to a known-good commit, rebuild the frontend (best-effort)
    and restart the local units. Remote runners this deploy moved are rewound by the
    controller via ``runners.rewind_runners`` — the two together restore one version
    fleet-wide (2026-08-25: rollback left rocky on the rejected commit)."""
    run(["git", "-C", repo, "reset", "--hard", sha], check=True, capture_output=True)
    try:
        build_frontend(repo, run=run)
    except DeployError:
        pass
    if daemon_changed and daemon_enabled:
        restart_local_daemon(run=run)
    restart_edge(run=run)
```

Keep `SSH_HOST_RE`, `SSH_REPO_RE`, `_SHA_RE` (used by runners + the fleet API). Delete every remaining reference to `CCSWITCH_DAEMON_SSH_HOST`/`_REPO` in `steps.py` and update `test_deploy_steps_restart_health.py`'s `remote_daemon_configured` test to call `remote_runners_configured` with `RunnerTarget` lists (same assertions as the new remote-restart test).

- [ ] **Step 5: Seed the ssh columns from env at edge startup**

In `server/app.py`, add a module-level coroutine (next to the fleet seeding block) and call it right after that block:

```python
async def seed_host_ssh_from_env() -> None:
    """One-time bridge from the retired CCSWITCH_DAEMON_SSH_HOST/_REPO deploy path: when
    both are set and a Host row is named after the ssh host's hostname part with NULL
    ssh_host/repo_path, fill them in so the fan-out can reach it without a UI edit.
    Values already set are never overwritten (the registry is the source of truth)."""
    ssh_host = os.environ.get("CCSWITCH_DAEMON_SSH_HOST", "").strip()
    repo = os.environ.get("CCSWITCH_DAEMON_SSH_REPO", "").strip()
    if not ssh_host or not repo:
        return
    from server.deploy.steps import SSH_HOST_RE, SSH_REPO_RE
    if not SSH_HOST_RE.match(ssh_host) or not SSH_REPO_RE.match(repo):
        return
    name = ssh_host.split("@", 1)[-1].split(".", 1)[0]
    from sqlalchemy import update as _upd
    from server.db import db_session as _dbs
    from server.db.models import Host as _Host
    async with _dbs() as _db:
        await _db.execute(_upd(_Host).where(_Host.name == name, _Host.ssh_host.is_(None))
                          .values(ssh_host=ssh_host, repo_path=repo))
```

Call site (after the `CCSWITCH_FLEET_HOSTS` seeding, before `ensure_default_host`): `await seed_host_ssh_from_env()`.

- [ ] **Step 6: Run the affected suites**

Run: `CCSWITCH_DEV=1 PYTHONPATH=$PWD/src /home/tony/GitHub/ccswitch/.venv/bin/python -m pytest server/tests/test_fleet_upgrade.py server/tests/test_deploy_remote_restart.py server/tests/test_fleet_seed_ssh_from_env.py server/tests/test_deploy_steps_restart_health.py server/tests/test_deploy_gate_daemon.py server/tests/test_fleet_api.py -q`
Expected: PASS. (`test_deploy_controller.py` will break until Task 7 — that is expected; do not run it here.)

- [ ] **Step 7: Commit**

```bash
git add server/sessions/host_upgrade.py server/deploy/steps.py server/app.py server/tests/test_fleet_upgrade.py server/tests/test_deploy_remote_restart.py server/tests/test_fleet_seed_ssh_from_env.py server/tests/test_deploy_steps_restart_health.py
git commit -m "refactor(deploy): one remote-update path — host_upgrade wraps RunnerDeploy; restart_units → restart_edge"
```

---

### Task 7: Controller `runners` phase, per-host status, `deploy_state` write, rollback rewind

**Files:**
- Modify: `server/deploy/types.py` (`Phase.RUNNERS`, `DeployStatus.hosts`)
- Modify: `server/deploy/controller.py` (constructor injection, phases 228-331, rollback paths)
- Modify: `server/deploy/app.py:53-56` (drop `default_daemon_factory`), `server/deploy/serve.py` (init the DB)
- Modify: `server/tests/test_deploy_controller.py` (fixtures + new tests), `server/tests/test_deploy_status.py` (hosts round-trip)

**Interfaces:**
- Consumes: `runners.load_runner_targets`, `runners.deploy_runners`, `runners.rewind_runners`, `runners.HostProgress`, `runners.FAILED_STATES`; `state.record_deploy`; `steps.restart_edge`, `steps.remote_runners_configured`, `steps.rollback_to`; `server.hostname.self_host_name`.
- Produces: `Phase.RUNNERS = "runners"` ordered `preflight, save, update, report, runners, restart, resume`; `DeployStatus.hosts: dict[str, dict]` serialised as `"hosts"`; `DeployController(..., runners_mod=_runners, state_mod=_state, local_name: str | None = None)`; `/status` unchanged shape plus `deploy.hosts`.

- [ ] **Step 1: Write the failing tests**

Update `make_steps` in `server/tests/test_deploy_controller.py`: replace the `restart_units` block with

```python
    def restart_edge(run=None):
        calls.restarted = ["ccswitch.service"]; return ["ccswitch.service"]
    mod.restart_edge = restart_edge
    mod.remote_runners_configured = lambda hosts=None: False
```

and change `rollback` to `def rollback(repo, sha, *, daemon_changed, daemon_enabled, run=None): calls.rolled_back = True`. Add next to it:

```python
from server.deploy.runners import HostProgress, RunnerTarget


def make_runners(*, targets=None, outcome="done", moved=True):
    """Fake runners module: records the fan-out call and returns the same state per host."""
    calls = SimpleNamespace(deploy_kwargs=None, rewound=None, loaded=0)
    mod = SimpleNamespace()
    hosts = targets if targets is not None else [
        RunnerTarget(name="rocky", endpoint="https://rocky:9443", enabled=True, healthy=True,
                     ssh_host="rocky", repo_path="/srv/cc")]
    async def load():
        calls.loaded += 1; return hosts
    async def deploy(targets, *, on_progress=None, **kw):
        calls.deploy_kwargs = kw
        out = {}
        for t in targets:
            st = outcome(t.name) if callable(outcome) else outcome
            p = HostProgress(state=st, step=None if st == "done" else "merge",
                             error=None if st == "done" else "merge failed (rc=1): boom",
                             sha=kw["target_sha"] if st == "done" else None, moved=moved)
            if on_progress: on_progress(t.name, p)
            out[t.name] = p
        return out
    async def rewind(targets, results, *, sha, **kw):
        calls.rewound = (sorted(n for n, p in results.items() if p.moved), sha)
    mod.load_runner_targets = load
    mod.deploy_runners = deploy
    mod.rewind_runners = rewind
    mod.FAILED_STATES = frozenset({"failed", "unconfigured"})
    return mod, calls


def make_state():
    calls = SimpleNamespace(recorded=[])
    mod = SimpleNamespace()
    async def record(*, sha, short, daemon_changed, by="deploy-controller"):
        calls.recorded.append((sha, short, daemon_changed))
    mod.record_deploy = record
    return mod, calls


def make_controller(g, s, *, runners=None, state=None, daemon_enabled=True):
    r, _ = runners or make_runners()
    st, _ = state or make_state()
    return DeployController("/r", health_url="http://h", venv_python="/v/python",
                            git_mod=g, steps_mod=s, runners_mod=r, state_mod=st,
                            daemon_enabled=daemon_enabled, local_name="dobby")
```

Then update every existing test that constructs `DeployController(...)` to go through `make_controller` (or pass `runners_mod`/`state_mod`), remove the `daemon_client_factory`-based tests for snapshot/drain/close_events/restore (those RPCs now live in `RunnerDeploy`, covered by Task 5), and add:

```python
@pytest.mark.asyncio
async def test_runners_phase_runs_before_edge_restart_and_records_state():
    g = FakeGit("old0000000", "new1111111", ["server/sessions/orchestrator.py"])
    s, sc = make_steps(changed=["server/sessions/orchestrator.py"])
    order = []
    s.restart_edge = lambda run=None: order.append("edge") or ["ccswitch.service"]
    r, rc = make_runners()
    orig = r.deploy_runners
    async def deploy(targets, **kw):
        order.append("runners"); return await orig(targets, **kw)
    r.deploy_runners = deploy
    st_mod, stc = make_state()
    c = make_controller(g, s, runners=(r, rc), state=(st_mod, stc))
    st = await c.deploy_once()
    assert st.error is None
    assert order == ["runners", "edge"]
    assert st.phases["runners"] == "done" and st.phases["restart"] == "done"
    assert st.hosts["rocky"]["state"] == "done" and st.hosts["rocky"]["sha"] == "new1111"
    assert rc.deploy_kwargs["daemon_changed"] is True
    assert rc.deploy_kwargs["target_sha"] == "new1111"
    assert stc.recorded == [("new1111111", "new1111", True)]


@pytest.mark.asyncio
async def test_edge_only_deploy_still_updates_runner_checkouts_without_restart():
    g = FakeGit("old0000000", "new1111111", ["web/src/App.tsx"])
    s, _ = make_steps(changed=["web/src/App.tsx"])
    r, rc = make_runners()
    st_mod, stc = make_state()
    c = make_controller(g, s, runners=(r, rc), state=(st_mod, stc))
    st = await c.deploy_once()
    assert st.error is None
    assert rc.deploy_kwargs["daemon_changed"] is False
    assert stc.recorded == [("new1111111", "new1111", False)]


@pytest.mark.asyncio
async def test_deps_changed_is_derived_from_the_diff():
    g = FakeGit("old0000000", "new1111111", ["uv.lock", "server/x.py"])
    s, _ = make_steps(changed=["uv.lock", "server/x.py"])
    r, rc = make_runners()
    c = make_controller(g, s, runners=(r, rc))
    await c.deploy_once()
    assert rc.deploy_kwargs["deps_changed"] is True


@pytest.mark.asyncio
async def test_failed_runner_fails_the_deploy_but_the_edge_still_lands():
    """Decision 1a: name the host, keep the edge, never roll back, never stop the others."""
    g = FakeGit("old0000000", "new1111111", ["server/sessions/orchestrator.py"])
    s, sc = make_steps(changed=["server/sessions/orchestrator.py"])
    targets = [RunnerTarget(name=n, endpoint=f"https://{n}", enabled=True, healthy=True,
                            ssh_host=n, repo_path="/srv/cc") for n in ("rocky", "sunny")]
    r, rc = make_runners(targets=targets, outcome=lambda n: "failed" if n == "rocky" else "done")
    st_mod, stc = make_state()
    c = make_controller(g, s, runners=(r, rc), state=(st_mod, stc))
    st = await c.deploy_once()
    assert st.phases["runners"] == "failed"
    assert st.error and "rocky" in st.error and "merge failed" in st.error
    assert sc.restarted == ["ccswitch.service"], "the edge still restarts"
    assert sc.rolled_back is False and rc.rewound is None
    assert st.phases["restart"] == "done" and st.phases["resume"] == "done"
    assert st.hosts["sunny"]["state"] == "done"
    assert stc.recorded, "the fleet's deployed version moved: the failed runner is fenced against it"


@pytest.mark.asyncio
async def test_offline_runner_is_reported_not_failed():
    g = FakeGit("old0000000", "new1111111", ["server/x.py"])
    s, _ = make_steps(changed=["server/x.py"])
    r, rc = make_runners(outcome="offline")
    c = make_controller(g, s, runners=(r, rc))
    st = await c.deploy_once()
    assert st.error is None and st.phases["runners"] == "done"
    assert st.hosts["rocky"]["state"] == "offline"


@pytest.mark.asyncio
async def test_unconfigured_online_runner_fails_the_deploy():
    g = FakeGit("old0000000", "new1111111", ["server/x.py"])
    s, _ = make_steps(changed=["server/x.py"])
    r, rc = make_runners(outcome="unconfigured")
    c = make_controller(g, s, runners=(r, rc))
    st = await c.deploy_once()
    assert st.phases["runners"] == "failed" and "rocky" in (st.error or "")


@pytest.mark.asyncio
async def test_edge_health_failure_rolls_back_edge_and_rewinds_moved_runners():
    g = FakeGit("old0000000", "new1111111", ["server/x.py"])
    s, sc = make_steps(healthy=False, changed=["server/x.py"])
    r, rc = make_runners(moved=True)
    st_mod, stc = make_state()
    c = make_controller(g, s, runners=(r, rc), state=(st_mod, stc))
    st = await c.deploy_once()
    assert st.error and "health gate" in st.error
    assert sc.rolled_back is True
    assert rc.rewound == (["rocky"], "old0000000")
    assert stc.recorded == [], "a rolled-back deploy must not move the deployed version"


@pytest.mark.asyncio
async def test_health_window_sized_by_remote_runner_presence():
    g = FakeGit("old0000000", "new1111111", ["web/src/App.tsx"])
    s, sc = make_steps(changed=["web/src/App.tsx"])
    s.remote_runners_configured = lambda hosts=None: bool(hosts)
    r, rc = make_runners()
    c = make_controller(g, s, runners=(r, rc))
    await c.deploy_once()
    assert sc.health_kwargs["attempts"] == s.HEALTH_ATTEMPTS_WITH_DAEMON
    assert sc.health_kwargs["require_daemon_sha"] is False


@pytest.mark.asyncio
async def test_status_wire_carries_hosts():
    from server.deploy.types import DeployStatus
    st = DeployStatus(target_sha="x", hosts={"rocky": {"state": "done", "step": None,
                                                      "error": None, "sha": "x", "restore": None}})
    d = st.to_dict()
    assert d["hosts"]["rocky"]["state"] == "done"
    assert DeployStatus.from_dict(d).hosts == st.hosts
```

- [ ] **Step 2: Run to verify they fail**

Run: `CCSWITCH_DEV=1 PYTHONPATH=$PWD/src /home/tony/GitHub/ccswitch/.venv/bin/python -m pytest server/tests/test_deploy_controller.py -q -x`
Expected: FAIL — `TypeError: __init__() got an unexpected keyword argument 'runners_mod'`.

- [ ] **Step 3: `types.py`**

```python
class Phase(str, Enum):
    PREFLIGHT = "preflight"
    SAVE = "save"
    UPDATE = "update"
    REPORT = "report"
    RUNNERS = "runners"     # every fleet daemon: update checkout, restart if daemon-changing, verify
    RESTART = "restart"     # the edge
    RESUME = "resume"
```

Add to `DeployStatus`: `hosts: dict[str, dict] = field(default_factory=dict)`; include `"hosts": self.hosts` in `to_dict` and `hosts=dict(d.get("hosts") or {})` in `from_dict`.

- [ ] **Step 4: Controller**

Constructor: add `runners_mod=_runners, state_mod=_state, local_name: str | None = None` (import `from server.deploy import runners as _runners` and `from server.deploy import state as _state`), store as `self._runners`, `self._state`, `self._local_name = local_name or self_host_name()` (`from server.hostname import self_host_name`). Remove `daemon_client_factory`/`_client_factory` and `default_daemon_factory` entirely (and `_aclose_quietly` if unused). Add:

```python
    def _on_host_progress(self, name: str, progress) -> None:
        self.status.hosts[name] = progress.to_wire()
        self._persist_status()
```

Replace the `save` phase body with a trivial `self._set(Phase.SAVE, PhaseState.DONE)` after a comment: snapshots now happen per runner inside the runners phase. Keep `update`/`report`. Compute alongside `daemon_changed`:

```python
            deps_changed = (changed is None) or any(
                f in ("pyproject.toml", "uv.lock") for f in changed)
```

Replace everything from `# --- restart ---` through the `resume` phase with:

```python
            # --- runners (every fleet daemon, local + remote; spec §2) ---
            self._set(Phase.RUNNERS, PhaseState.RUNNING)
            short = await asyncio.to_thread(self._git.short_sha, self.repo, remote)
            targets = await self._runners.load_runner_targets()
            results = await self._runners.deploy_runners(
                targets, target_sha=short, daemon_changed=daemon_changed,
                deps_changed=deps_changed, local_name=self._local_name,
                local_daemon_enabled=self.daemon_enabled,
                on_progress=self._on_host_progress)
            for name, p in results.items():
                self.status.hosts[name] = p.to_wire()
            runner_failures = [f"{n} failed at {p.step or '?'}: {p.error or p.state}"
                               for n, p in results.items() if p.state in self._runners.FAILED_STATES]
            self._set(Phase.RUNNERS, PhaseState.FAILED if runner_failures else PhaseState.DONE)

            # --- restart (the edge) ---
            self._set(Phase.RESTART, PhaseState.RUNNING)
            try:
                await asyncio.to_thread(self._steps.restart_edge)
            except Exception as e:
                raise DeployError("restart", str(e)) from e
            _needs_daemon_window = daemon_changed or self._steps.remote_runners_configured(targets)
            _attempts = (self._steps.HEALTH_ATTEMPTS_WITH_DAEMON if _needs_daemon_window
                         else self._steps.HEALTH_ATTEMPTS_EDGE_ONLY)
            if not await asyncio.to_thread(self._steps.wait_healthy, short,
                                           url=self.health_url, attempts=_attempts,
                                           require_daemon_sha=daemon_changed):
                raise DeployError("restart", "health gate failed")
            base = self.health_url.rsplit("/healthz", 1)[0] or self.health_url
            await asyncio.to_thread(self._steps.require_endpoints_healthy, base)
            log_event(log, logging.INFO, "deploy.edge.cutover", deploy_id=self._deploy_id)
            # The fleet's authoritative version moved: record it now (spec §1) so the skew
            # gate measures every runner — including one that failed above — against it.
            try:
                await self._state.record_deploy(sha=remote, short=short, daemon_changed=daemon_changed)
            except Exception:
                log.warning("deploy: could not record deploy_state", exc_info=True)
                self.status.warnings.append("deployed version not recorded (deploy_state)")
            self._set(Phase.RESTART, PhaseState.DONE)

            # --- resume: per-runner restores already ran inside the runners phase ---
            self._set(Phase.RESUME, PhaseState.RUNNING)
            restored: list[str] = []; failed_sessions: list[str] = []
            for name, p in results.items():
                r = p.restore or {}
                restored += list(r.get("restored", [])); failed_sessions += list(r.get("failed", []))
            self.status.restore = {"restored": restored, "failed": failed_sessions}
            if failed_sessions:
                self.status.warnings.append(
                    f"resume failed for {len(failed_sessions)} session(s): {', '.join(failed_sessions)}")
            self._set(Phase.RESUME, PhaseState.DONE)

            publish_err = await asyncio.to_thread(self._steps.publish_deployment, remote)
            if publish_err:
                self.status.warnings.append(f"could not publish GitHub deployment: {publish_err}")
            if runner_failures:
                # Decision 1a: the edge landed; report the deploy as failed with the hosts named.
                self.status.error = "runners: " + "; ".join(runner_failures)
            return self.status
```

In both `except` branches, replace the `rollback_to` calls with a helper that also rewinds moved runners:

```python
    async def _rollback(self, local: str, *, daemon_changed: bool) -> None:
        await asyncio.to_thread(self._steps.rollback_to, self.repo, local,
                                daemon_changed=daemon_changed,
                                daemon_enabled=self.daemon_enabled)
        results = getattr(self, "_runner_results", None)
        targets = getattr(self, "_runner_targets", None)
        if results and targets:
            await self._runners.rewind_runners(targets, results, sha=local,
                                               local_name=self._local_name)
```

(store `self._runner_targets = targets` and `self._runner_results = results` right after `deploy_runners` returns, and reset both to `None` at the top of `deploy_once`). The `finally` block is unchanged.

`server/deploy/app.py`: drop the `daemon_client_factory=default_daemon_factory(daemon_enabled)` argument and the import. `server/deploy/serve.py`: before `build_controller_app`, initialise the DB the controller now reads:

```python
    import asyncio
    from server.db import init_db
    asyncio.run(init_db(db_url=os.environ.get("CCSWITCH_DB_URL")))
```

- [ ] **Step 5: Run the deploy suites**

Run: `CCSWITCH_DEV=1 PYTHONPATH=$PWD/src /home/tony/GitHub/ccswitch/.venv/bin/python -m pytest server/tests/test_deploy_controller.py server/tests/test_deploy_status.py server/tests/test_deploy_app*.py server/tests/test_deploy_*.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add server/deploy/types.py server/deploy/controller.py server/deploy/app.py server/deploy/serve.py server/tests/test_deploy_controller.py server/tests/test_deploy_status.py
git commit -m "feat(deploy): runners phase — fan out to every online fleet daemon, gate completion on each, record deploy_state"
```

---

### Task 8: `runner_sync` boot-time reconcile + `ExecStartPre`

**Files:**
- Create: `server/deploy/runner_sync.py`
- Modify: `deploy/ccswitch-daemon-remote.service:26`, `deploy/ccswitch-daemon.service` (ExecStart line)
- Test: `server/tests/test_runner_sync.py`

**Interfaces:**
- Consumes: raw SQL on `deploy_state` (NOT the ORM), `steps.SSH_REPO_RE` not needed (local paths), `git` argv.
- Produces:
  ```python
  Outcome = Literal["no-state", "in-sync", "ahead", "updated", "dirty", "not-fast-forward", "fetch-failed", "pip-failed", "unknown-sha"]
  def read_deployed_sha(db_url: str, *, timeout_s: float = 15.0) -> str | None
  def sync(repo: str, *, deployed_sha: str | None, run=subprocess.run, log=logging.getLogger(...)) -> Outcome
  def main(argv: list[str] | None = None) -> int     # always 0 (decision 3a)
  ```

- [ ] **Step 1: Write the failing tests**

```python
# server/tests/test_runner_sync.py
"""Boot-time reconcile (spec §3): a runner brings itself to deploy_state.deployed_sha
before its daemon starts; every failure is loud but never blocks the start (decision 3a)."""
from __future__ import annotations
import subprocess
from server.deploy import runner_sync as RS

TARGET = "b" * 40


class Git:
    """Scripted git: HEAD, cleanliness, ancestry; records argv."""
    def __init__(self, head, *, clean=True, ff=True, fetch_ok=True, pip_ok=True, deps_changed=False):
        self.head, self.clean, self.ff, self.fetch_ok, self.pip_ok = head, clean, ff, fetch_ok, pip_ok
        self.deps_changed = deps_changed
        self.calls: list[list[str]] = []

    def __call__(self, argv, **kw):
        self.calls.append(list(argv))
        def done(rc=0, out=""):
            return subprocess.CompletedProcess(argv, rc, stdout=out, stderr="err")
        if argv[:3] == ["git", "rev-parse", "HEAD"]:
            return done(0, self.head + "\n")
        if argv[:3] == ["git", "status", "--porcelain"]:
            return done(0, "" if self.clean else " M server/x.py\n")
        if argv[:2] == ["git", "fetch"]:
            return done(0 if self.fetch_ok else 1)
        if argv[:3] == ["git", "merge-base", "--is-ancestor"]:
            return done(0 if self.ff else 1)
        if argv[:2] == ["git", "checkout"]:
            return done(0)
        if argv[:2] == ["git", "merge"]:
            return done(0 if self.ff else 1)
        if argv[:2] == ["git", "diff"]:
            return done(0, "uv.lock\n" if self.deps_changed else "")
        if "pip" in argv:
            return done(0 if self.pip_ok else 1)
        if argv == ["sync"]:
            return done(0)
        raise AssertionError(f"unexpected argv {argv}")


def test_no_state_is_a_noop():
    g = Git("a" * 40)
    assert RS.sync("/repo", deployed_sha=None, run=g) == "no-state"
    assert g.calls == []


def test_in_sync_touches_nothing():
    g = Git(TARGET)
    assert RS.sync("/repo", deployed_sha=TARGET, run=g) == "in-sync"
    assert [c[:2] for c in g.calls] == [["git", "rev-parse"]]


def test_fast_forwards_a_behind_runner_and_syncs():
    g = Git("a" * 40)
    assert RS.sync("/repo", deployed_sha=TARGET, run=g) == "updated"
    joined = [" ".join(c) for c in g.calls]
    assert "git -C /repo fetch --quiet origin" in joined
    assert "git -C /repo checkout -q master" in joined
    assert f"git -C /repo merge --ff-only {TARGET}" in joined
    assert joined[-1] == "sync"
    assert not any("pip" in j for j in joined), "lockfile unchanged → no pip"
    assert not any("reset" in j for j in joined)


def test_runs_pip_when_lockfile_changed():
    g = Git("a" * 40, deps_changed=True)
    assert RS.sync("/repo", deployed_sha=TARGET, run=g) == "updated"
    pip = next(c for c in g.calls if "pip" in c)
    assert pip == ["/repo/.venv/bin/python", "-m", "pip", "install", "-e", "/repo[server]", "--quiet"]


def test_ahead_of_deployed_is_left_alone():
    """A deploy in progress fast-forwarded the checkout BEFORE deploy_state moved: HEAD is
    a descendant of deployed_sha. Reconcile only ever moves forward."""
    g = Git("c" * 40, ff=False)
    # ancestry query: is deployed an ancestor of HEAD? scripted True via a second Git
    class Ahead(Git):
        def __call__(self, argv, **kw):
            if argv[:3] == ["git", "merge-base", "--is-ancestor"]:
                anc, desc = argv[-2], argv[-1]
                return subprocess.CompletedProcess(argv, 0 if (anc == TARGET and desc == "c" * 40) else 1,
                                                   stdout="", stderr="")
            return super().__call__(argv, **kw)
    g = Ahead("c" * 40)
    assert RS.sync("/repo", deployed_sha=TARGET, run=g) == "ahead"
    assert not any(c[:2] == ["git", "merge"] for c in g.calls)


def test_dirty_tree_refuses():
    g = Git("a" * 40, clean=False)
    assert RS.sync("/repo", deployed_sha=TARGET, run=g) == "dirty"
    assert not any(c[:2] == ["git", "fetch"] for c in g.calls)


def test_diverged_refuses_without_reset():
    g = Git("a" * 40, ff=False)
    assert RS.sync("/repo", deployed_sha=TARGET, run=g) == "not-fast-forward"
    assert not any("reset" in " ".join(c) for c in g.calls)


def test_fetch_failure_is_reported():
    g = Git("a" * 40, fetch_ok=False)
    assert RS.sync("/repo", deployed_sha=TARGET, run=g) == "fetch-failed"


def test_pip_failure_is_reported_after_the_checkout_moved():
    g = Git("a" * 40, deps_changed=True, pip_ok=False)
    assert RS.sync("/repo", deployed_sha=TARGET, run=g) == "pip-failed"


def test_read_deployed_sha_sqlite_and_missing_table(tmp_path):
    import asyncio
    from server.db import init_db
    from server.deploy.state import record_deploy
    url = f"sqlite+aiosqlite:///{tmp_path / 'r.db'}"
    async def seed():
        await init_db(db_url=url)
        await record_deploy(sha=TARGET, short="bbbbbbb", daemon_changed=True)
    asyncio.run(seed())
    assert RS.read_deployed_sha(url) == TARGET
    assert RS.read_deployed_sha(f"sqlite+aiosqlite:///{tmp_path / 'empty.db'}") is None


def test_main_always_exits_zero(monkeypatch, tmp_path):
    monkeypatch.setenv("CCSWITCH_DB_URL", "sqlite+aiosqlite:///" + str(tmp_path / "none.db"))
    assert RS.main(["--repo", str(tmp_path)]) == 0
    monkeypatch.setenv("CCSWITCH_DB_URL", "postgresql+asyncpg://nobody@127.0.0.1:1/none")
    assert RS.main(["--repo", str(tmp_path)]) == 0
```

- [ ] **Step 2: Run to verify they fail**

Run: `CCSWITCH_DEV=1 PYTHONPATH=$PWD/src /home/tony/GitHub/ccswitch/.venv/bin/python -m pytest server/tests/test_runner_sync.py -q`
Expected: FAIL — `ImportError: cannot import name 'runner_sync'`.

- [ ] **Step 3: Write `server/deploy/runner_sync.py`**

```python
"""Boot-time version reconcile for a session runner (spec §3).

Wired as ``ExecStartPre=<venv>/bin/python -m server.deploy.runner_sync`` on the daemon
unit. Reads ``deploy_state.deployed_sha`` with RAW SQL — this script runs on the OLD
checkout, which may predate the ORM model — and fast-forwards the checkout to it when
the runner is behind. Only ever moves FORWARD: a HEAD that is a descendant of the
deployed sha means a deploy is mid-flight (the fan-out moves checkouts before it records
the new version) and is left alone.

Every failure is logged to the journal at ERROR and the process exits 0 (decision 3a):
a runner that cannot reconcile still starts and rehydrates its sessions; the fleet
probe's skew gate keeps it out of dispatch until an operator fixes it.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import subprocess
import sys
from typing import Callable, Literal

log = logging.getLogger("ccswitch.runner_sync")

Run = Callable[..., subprocess.CompletedProcess]
Outcome = Literal["no-state", "in-sync", "ahead", "updated", "dirty", "not-fast-forward",
                  "fetch-failed", "pip-failed", "unknown-sha"]

FETCH_TIMEOUT_S = 60
DEPS_FILES = ("pyproject.toml", "uv.lock")


def read_deployed_sha(db_url: str, *, timeout_s: float = 15.0) -> str | None:
    """``deploy_state.deployed_sha`` via the driver directly (asyncpg / sqlite3).
    None when there is no row, no table, or the DB is unreachable within *timeout_s*."""
    sql = "SELECT deployed_sha FROM deploy_state WHERE id = 'singleton'"
    try:
        if db_url.startswith("sqlite"):
            import sqlite3
            path = db_url.split("///", 1)[1]
            if not os.path.exists(path):
                return None
            with sqlite3.connect(path, timeout=timeout_s) as conn:
                row = conn.execute(sql).fetchone()
            return row[0] if row and row[0] else None
        import asyncpg  # noqa: PLC0415

        async def go():
            conn = await asyncio.wait_for(
                asyncpg.connect(db_url.replace("postgresql+asyncpg://", "postgresql://")),
                timeout=timeout_s)
            try:
                return await asyncio.wait_for(conn.fetchval(sql), timeout=timeout_s)
            finally:
                await conn.close()
        return asyncio.run(go()) or None
    except Exception as exc:  # noqa: BLE001 — unreadable state is "no state", loudly
        log.error("runner_sync: cannot read deploy_state (%s: %s)", type(exc).__name__, exc)
        return None


def _git(run: Run, repo: str, *args: str, timeout: int = 30) -> subprocess.CompletedProcess:
    return run(["git", "-C", repo, *args], capture_output=True, text=True, timeout=timeout)


def sync(repo: str, *, deployed_sha: str | None, run: Run = subprocess.run) -> Outcome:
    if not deployed_sha:
        log.info("runner_sync: no deploy_state yet — nothing to reconcile")
        return "no-state"
    head = (_git(run, repo, "rev-parse", "HEAD").stdout or "").strip()
    if not head:
        log.error("runner_sync: cannot read HEAD of %s", repo)
        return "unknown-sha"
    if head == deployed_sha or head.startswith(deployed_sha) or deployed_sha.startswith(head):
        log.info("runner_sync: %s already on deployed %s", repo, deployed_sha[:7])
        return "in-sync"
    if (_git(run, repo, "status", "--porcelain").stdout or "").strip():
        log.error("runner_sync: %s has uncommitted changes — refusing to move it "
                  "(HEAD=%s deployed=%s)", repo, head[:7], deployed_sha[:7])
        return "dirty"
    if _git(run, repo, "fetch", "--quiet", "origin", timeout=FETCH_TIMEOUT_S).returncode != 0:
        log.error("runner_sync: git fetch failed in %s — starting on %s", repo, head[:7])
        return "fetch-failed"
    if _git(run, repo, "merge-base", "--is-ancestor", deployed_sha, head).returncode == 0:
        log.info("runner_sync: HEAD %s is AHEAD of deployed %s (deploy in flight) — leaving it",
                 head[:7], deployed_sha[:7])
        return "ahead"
    if _git(run, repo, "merge-base", "--is-ancestor", head, deployed_sha).returncode != 0:
        log.error("runner_sync: HEAD %s is not an ancestor of deployed %s — diverged checkout; "
                  "see docs/runbooks/rocky-session-host.md §5 (reset recipe)", head[:7], deployed_sha[:7])
        return "not-fast-forward"
    _git(run, repo, "checkout", "-q", "master")
    if _git(run, repo, "merge", "--ff-only", deployed_sha).returncode != 0:
        log.error("runner_sync: ff-only merge to %s failed (local commits on master?)", deployed_sha[:7])
        return "not-fast-forward"
    changed = (_git(run, repo, "diff", "--name-only", head, deployed_sha, "--", *DEPS_FILES).stdout or "")
    if changed.strip():
        venv_python = os.path.join(repo, ".venv", "bin", "python")
        r = run([venv_python, "-m", "pip", "install", "-e", f"{repo}[server]", "--quiet"],
                capture_output=True, text=True, timeout=600)
        if r.returncode != 0:
            log.error("runner_sync: pip install failed after moving to %s: %s",
                      deployed_sha[:7], (r.stderr or "").strip()[:500])
            return "pip-failed"
    run(["sync"], capture_output=True, text=True, timeout=120)
    log.info("runner_sync: %s → %s (was %s)", repo, deployed_sha[:7], head[:7])
    return "updated"


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(name)s %(levelname)s %(message)s",
                        stream=sys.stderr)
    p = argparse.ArgumentParser(prog="runner_sync")
    p.add_argument("--repo", default=os.getcwd())
    args = p.parse_args(argv)
    db_url = os.environ.get("CCSWITCH_DB_URL", "")
    if not db_url:
        log.error("runner_sync: CCSWITCH_DB_URL unset — cannot reconcile")
        return 0
    try:
        outcome = sync(args.repo, deployed_sha=read_deployed_sha(db_url))
        log.info("runner_sync: outcome=%s", outcome)
    except Exception:  # noqa: BLE001 — never block the daemon start
        log.exception("runner_sync: unexpected failure — starting the daemon anyway")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Wire the units**

`deploy/ccswitch-daemon-remote.service`: insert before `ExecStart=`:

```
# Boot-time version reconcile (spec: fleet deploy fan-out §3): fast-forward this checkout to
# deploy_state.deployed_sha when it is behind. Always exits 0 — a runner that cannot
# reconcile still starts; the edge's skew gate keeps it out of dispatch until fixed.
ExecStartPre=/home/tony/GitHub/ccswitch/.venv/bin/python -m server.deploy.runner_sync --repo /home/tony/GitHub/ccswitch
```

`deploy/ccswitch-daemon.service` (dobby's local unit): the same line with `/home/tony/GitHub/cc-account-switcher-deploy` as both the venv prefix and `--repo` (match that file's `ExecStart` path exactly).

- [ ] **Step 5: Run tests + ruff + mypy on the module**

Run: `CCSWITCH_DEV=1 PYTHONPATH=$PWD/src /home/tony/GitHub/ccswitch/.venv/bin/python -m pytest server/tests/test_runner_sync.py -q && /home/tony/GitHub/ccswitch/.venv/bin/ruff check server/deploy/runner_sync.py && /home/tony/GitHub/ccswitch/.venv/bin/python -m mypy server/deploy/runner_sync.py --ignore-missing-imports`
Expected: PASS, clean.

- [ ] **Step 6: Commit**

```bash
git add server/deploy/runner_sync.py server/tests/test_runner_sync.py deploy/ccswitch-daemon-remote.service deploy/ccswitch-daemon.service
git commit -m "feat(deploy): runner_sync — a booting runner fast-forwards itself to the deployed sha (ExecStartPre)"
```

---

### Task 9: Web — `runners` phase with per-host rows; Fleet shows checkout sha

**Files:**
- Modify: `web/src/api/types.ts:345-357` (`DeployProgress.hosts`)
- Modify: `web/src/lib/deployPhases.ts`, `web/src/lib/deployPhases.test.ts`
- Modify: `web/src/components/DeployOverlay.tsx:71-83`
- Modify: `web/src/components/settings/FleetSection.tsx` (Version cell)
- Test: `web/src/components/DeployOverlay.test.tsx` (add one case), `web/src/components/settings/__tests__/FleetSection.test.tsx` (add one case)

**Interfaces:**
- Consumes: wire `deploy.hosts: Record<string, {state, step, error, sha, restore}>` (Task 7), `checkoutSha` on hosts (Task 2).
- Produces: `DEPLOY_PHASES` = 7 phases with `runners` between `report` and `restart`; `export interface DeployHostProgress { state: 'pending'|'running'|'done'|'failed'|'offline'|'disabled'|'unconfigured'; step: string | null; error: string | null; sha: string | null }`; `export function hostRows(deploy): Array<[string, DeployHostProgress]>` (sorted by name).

- [ ] **Step 1: Write the failing tests**

In `web/src/lib/deployPhases.test.ts` change the first test to expect
`['preflight', 'save', 'update', 'report', 'runners', 'restart', 'resume']` and `total: 7` in the progress test, then add:

```ts
  it('hostRows lists per-host runner progress sorted by name', () => {
    const d = D({ hosts: { rocky: { state: 'done', step: null, error: null, sha: 'abc' },
                           dobby: { state: 'offline', step: null, error: null, sha: null } } })
    expect(hostRows(d).map(([n]) => n)).toEqual(['dobby', 'rocky'])
    expect(hostRows(undefined)).toEqual([])
  })

  it('a failed runners phase is a failed deploy even with every other phase done', () => {
    const all = Object.fromEntries(DEPLOY_PHASES.map(p => [p.key, 'done']))
    expect(deployOutcome(D({ phases: { ...all, runners: 'failed' }, error: 'runners: rocky failed' })))
      .toBe('failure')
  })
```

Add to `web/src/components/DeployOverlay.test.tsx` (mirror that file's existing mocking of `useDeployStatus`/`usePendingDeploy`/`useDeployController`):

```tsx
  it('renders one sub-row per runner under the Runners phase', () => {
    mockDeploy({ running: true, phases: { runners: 'running' }, error: null, warnings: [], targetSha: 'abc1234',
                 hosts: { rocky: { state: 'running', step: 'merge', error: null, sha: null },
                          dobby: { state: 'done', step: null, error: null, sha: 'abc1234' } } })
    render(<DeployOverlay />)
    expect(screen.getByText('rocky')).toBeInTheDocument()
    expect(screen.getByText(/merge/)).toBeInTheDocument()
    expect(screen.getByText('dobby')).toBeInTheDocument()
  })
```

Add to `FleetSection.test.tsx` (mirror its existing host fixture):

```tsx
  it('shows the checkout sha when it differs from the process sha', async () => {
    mockHosts([{ ...host, sha: 'proc111', checkoutSha: 'chk2222' }])
    render(<FleetSection />)
    expect(await screen.findByText(/proc111/)).toBeInTheDocument()
    expect(screen.getByText(/checkout chk2222/)).toBeInTheDocument()
  })
```

- [ ] **Step 2: Run to verify they fail**

Run: `npm --prefix web run test -- src/lib/deployPhases.test.ts src/components/DeployOverlay.test.tsx src/components/settings/__tests__/FleetSection.test.tsx`
Expected: FAIL (phase list has 6 entries; `hostRows` not exported; no sub-rows; no checkout text).

- [ ] **Step 3: Implement**

`web/src/api/types.ts`:

```ts
export interface DeployHostProgress {
  state: 'pending' | 'running' | 'done' | 'failed' | 'offline' | 'disabled' | 'unconfigured'
  step: string | null
  error: string | null
  sha: string | null
}
```
and inside `DeployProgress`: `hosts?: Record<string, DeployHostProgress>`.

`web/src/lib/deployPhases.ts`: insert `{ key: 'runners', label: 'Runners', description: 'Updating session hosts' }` after `report`; add

```ts
import type { DeployHostProgress } from '@/api/types'

export function hostRows(deploy: DeployProgress | undefined): Array<[string, DeployHostProgress]> {
  return Object.entries(deploy?.hosts ?? {}).sort(([a], [b]) => a.localeCompare(b))
}
```

`DeployOverlay.tsx`: import `hostRows`; inside the `DEPLOY_PHASES.map` render, after the `<li>` for `p.key === 'runners'`, emit the sub-rows:

```tsx
{p.key === 'runners' && hostRows(deploy).map(([name, h]) => (
  <li key={`host:${name}`} className="ml-6 flex items-center gap-3 text-xs">
    <span className={`h-2 w-2 rounded-full ${HOST_DOT[h.state] ?? DOT.pending}`} aria-hidden />
    <span className="w-24 font-mono text-ink">{name}</span>
    <span className="flex-1 text-ink-muted">
      {h.state === 'running' ? (h.step ?? 'working') : h.state}
      {h.sha ? <> · <span className="font-mono">{h.sha.slice(0, 7)}</span></> : null}
      {h.error ? <span className="text-danger"> — {h.error}</span> : null}
    </span>
  </li>
))}
```
with `const HOST_DOT: Record<string, string> = { ...DOT, offline: 'bg-gray-400', disabled: 'bg-gray-400', unconfigured: 'bg-red-500' }`. (Return a fragment from the map callback so the phase `<li>` and its sub-rows are siblings.)

`FleetSection.tsx` Version cell: replace `{h.sha ?? '—'}` with

```tsx
{h.sha ?? '—'}
{h.checkoutSha && h.sha && !shaMatch(h.checkoutSha, h.sha) && (
  <div className="font-sans text-xs text-ink-muted">checkout {h.checkoutSha}</div>
)}
```
and in `canUpdate` compare `newestSha` against `h.checkoutSha ?? h.sha`.

- [ ] **Step 4: Run web gates**

Run: `npm --prefix web run test -- src/lib/deployPhases.test.ts src/components/DeployOverlay.test.tsx src/components/settings/__tests__/FleetSection.test.tsx && web/node_modules/.bin/tsc --noEmit -p web && web/node_modules/.bin/eslint --config web/eslint.config.js web/src/lib/deployPhases.ts web/src/components/DeployOverlay.tsx web/src/components/settings/FleetSection.tsx web/src/api/types.ts`
Expected: PASS, clean.

- [ ] **Step 5: Commit**

```bash
git add web/src/api/types.ts web/src/lib/deployPhases.ts web/src/lib/deployPhases.test.ts web/src/components/DeployOverlay.tsx web/src/components/DeployOverlay.test.tsx web/src/components/settings/FleetSection.tsx web/src/components/settings/__tests__/FleetSection.test.tsx
git commit -m "feat(web): Runners deploy phase with per-host progress; Fleet shows checkout sha"
```

---

### Task 10: Runbooks, deploy README, full gates

**Files:**
- Modify: `docs/runbooks/rocky-session-host.md` §5 (lines 147-256), `docs/runbooks/onboarding-a-session-runner.md` (steps 7-8), `deploy/README.md` (deploy section)
- Modify: `server/config.py:118-122` (the `CCSWITCH_DAEMON_SSH_HOST` comment)

- [ ] **Step 1: Rewrite `rocky-session-host.md` §5 "Deploy"**

Replace the section with (keep the existing "hotfixed remote host cannot fast-forward" recovery recipe verbatim as a subsection):

```markdown
## 5. Deploy

A deploy fans out to **every** host in the fleet registry (`Settings → Fleet`), local and
remote, in a `runners` phase that runs before the edge restart (`server/deploy/runners.py`):

1. `snapshot_deploy` on the host's daemon (best-effort).
2. Bring its checkout to the target: `ssh <ssh_host> git -C <repo_path> fetch --quiet origin`,
   `merge --ff-only <sha>`, pip (`<repo>/.venv/bin/python -m pip install -e '<repo>[server]'`)
   only if `pyproject.toml`/`uv.lock` changed, then `sync`. dobby's own daemon uses the
   controller's checkout, already updated.
3. Only when the diff was daemon-changing: drain (same caps as before), then
   `sudo -n systemctl restart ccswitch-daemon.service`.
4. Verify `/info` reports the target (`checkoutSha`, and `sha` after a restart); `restore_deploy`.

At most two runners are in flight at once. A runner that is unhealthy at deploy time is
skipped (`offline`) and reconciles itself at its next boot (§5.1). A runner that fails is
named in the deploy error; the edge still lands, and the failed runner is **fenced from
dispatch** by the probe's version-skew gate (`Fleet` shows `version skew: …`) until you fix
it — the Fleet "Update" button re-runs the same sequence at the deployed sha.

The deploy records the deployed sha in the `deploy_state` table once the edge is healthy.
`CCSWITCH_DAEMON_SSH_HOST` / `CCSWITCH_DAEMON_SSH_REPO` are no longer read by the deploy;
they only seed the matching host's `ssh_host` / `repo_path` once at edge startup. Set those
two fields in the Fleet UI for every remote runner (the deploy fails loudly with
`unconfigured` for an online runner without them).

### 5.1 Boot-time reconcile

`ExecStartPre=… -m server.deploy.runner_sync` on the daemon unit fast-forwards the checkout to
`deploy_state.deployed_sha` when the runner is behind (clean tree, ff-only, pip if the
lockfile changed, `sync`). It always exits 0: a dirty or diverged checkout is logged at ERROR
(`journalctl -u ccswitch-daemon | grep runner_sync`) and the daemon starts anyway, fenced by
the skew gate until you apply the reset recipe below.

**One-time bootstrap on an existing runner:** apply the reset recipe (below), install the
unit file with the new `ExecStartPre` (`sudo cp deploy/ccswitch-daemon-remote.service
/etc/systemd/system/ccswitch-daemon.service && sudo systemctl daemon-reload`), and restart
the daemon when the Sessions page shows nothing mid-turn.
```

- [ ] **Step 2: Update `onboarding-a-session-runner.md` step 7/8 and `deploy/README.md`**

Step 7: after registering the host, add "set `sshHost` (`[user@]host`) and `repoPath` via `PUT /api/fleet/hosts/{name}` or the Fleet UI — a deploy refuses (`unconfigured`) an online runner without them." Step 8: "`checkoutSha` and `sha` both match the edge; `lastError` empty (a `version skew: …` there means the runner is fenced until updated)". In `deploy/README.md`'s deploy-flow section list the seven phases and link §5 above. In `server/config.py:118-122` replace the "must agree" comment with: "Deprecated: read once at edge startup to seed the matching Host row's ssh_host/repo_path; the deploy fans out over the fleet registry (server/deploy/runners.py)."

- [ ] **Step 3: Run every gate**

```
/home/tony/GitHub/ccswitch/.venv/bin/ruff check src/ server/
/home/tony/GitHub/ccswitch/.venv/bin/python -m mypy server/ --ignore-missing-imports
CCSWITCH_DEV=1 PYTHONPATH=$PWD/src /home/tony/GitHub/ccswitch/.venv/bin/python -m pytest tests/ -q
CCSWITCH_DEV=1 PYTHONPATH=$PWD/src /home/tony/GitHub/ccswitch/.venv/bin/python -m pytest server/tests/ -n 8 -q
npm --prefix web test
web/node_modules/.bin/tsc --noEmit -p web
npm --prefix web run lint
```
Expected: all green (one known flake: `test_ipc_entrypoint.py::test_entrypoint_serves_ping_over_uds` under `-n 8`; passes alone).

- [ ] **Step 4: Commit**

```bash
git add docs/runbooks/rocky-session-host.md docs/runbooks/onboarding-a-session-runner.md deploy/README.md server/config.py
git commit -m "docs(deploy): fleet fan-out, runner_sync bootstrap, retire CCSWITCH_DAEMON_SSH_HOST as the deploy path"
```

---

## Self-review

- **Spec coverage:** §1 → Task 1; §2 fan-out, transports, concurrency, failure policy, consolidation, env seed → Tasks 5, 6, 7; §3 → Task 8; §4 → Tasks 2, 3; §5 operator surface → Task 9; runbooks → Task 10; rollback rewind → Tasks 5 (`rewind_runners`) + 7; tests listed in the spec map to each task's Step 1.
- **Placeholders:** none; every code step is complete.
- **Type consistency:** `HostProgress.to_wire()` keys (`state, step, error, sha, restore`) match `DeployStatus.hosts` and `DeployHostProgress`; `deploy_runners(targets, *, target_sha, daemon_changed, deps_changed, local_name, local_daemon_enabled, concurrency, on_progress, **runner_kwargs)` is called identically in Task 7's fake and the controller; `remote_runners_configured(hosts)` takes the `load_runner_targets()` list in both steps and controller; `record_deploy(sha=, short=, daemon_changed=)` matches Tasks 1, 3, 6, 7; `read_deployed_sha` uses the same table/column names as `DeployStateRow`.

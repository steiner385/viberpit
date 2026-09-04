"""Host-partitioned session ownership + worker read-only usage poller (#852).

A fleet daemon must only ACT on sessions it owns:

- a worker's ``rehydrate()`` loads only rows the dispatcher stamped with its own
  host name (NULL-host legacy rows belong to the primary);
- the primary still loads every row (the fleet dashboard is served from its
  ``_records``), but its acting loops — stuck-session sweep, rotation monitor,
  disabled-account sweep, and the titling / inferred-goal sweeps (#1033) — check
  per-row ownership via ``_owns_record`` so two daemons never race duplicate
  ``_rotate_away`` spawns over the same shared row, and so the primary never runs a
  cwd-bound Claude query for a directory that lives on another host;
- a worker's usage poller never refreshes shared tokens (``allow_refresh=False``
  unconditionally): the poll signal feeds its rotation monitor and
  ``_account_is_dead`` without racing the primary over the refresh-token files.
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from server.db import db_session, init_db
from server.db.models import Host, Session as DbSession
from server.sessions.orchestrator import SessionOrchestrator, daemon_role, self_host_name
from server.sessions.types import SessionRecord
from server.tests.fakes.fake_factory import fake_sdk_factory


async def _seed_sessions(rows):
    async with db_session() as db:
        for r in rows:
            db.add(DbSession(**r))


async def _seed_hosts(rows):
    async with db_session() as db:
        for r in rows:
            db.add(Host(**r))


def _new_orch():
    return SessionOrchestrator(sdk_factory=fake_sdk_factory(), poll_interval=9999.0)


def _row(sid, host):
    return dict(id=sid, name=sid, account_name="acct", claude_session_id=f"c-{sid}",
                status="idle", cwd=f"/repo/{sid}", host=host)


def _record(sid, host, status="needs_attention"):
    return SessionRecord(id=sid, name=sid, account="acct", claude_session_id=f"c-{sid}",
                         cols=80, rows=24, cwd=f"/repo/{sid}", detached=True,
                         status=status, host=host)


# ---------------------------------------------------------------------------
# Identity helpers
# ---------------------------------------------------------------------------


def test_daemon_role_default_and_validation(monkeypatch):
    monkeypatch.delenv("CCSWITCH_DAEMON_ROLE", raising=False)
    assert daemon_role() == "primary"
    monkeypatch.setenv("CCSWITCH_DAEMON_ROLE", "Worker ")
    assert daemon_role() == "worker"
    monkeypatch.setenv("CCSWITCH_DAEMON_ROLE", "standby")
    with pytest.raises(ValueError):
        daemon_role()


def test_self_host_name_env_wins_over_hostname(monkeypatch):
    monkeypatch.setenv("CCSWITCH_HOST_NAME", "rocky")
    assert self_host_name() == "rocky"
    monkeypatch.delenv("CCSWITCH_HOST_NAME", raising=False)
    # Fallback: the kernel hostname's first label — never empty.
    assert self_host_name()
    assert "." not in self_host_name()


# ---------------------------------------------------------------------------
# rehydrate() partition
# ---------------------------------------------------------------------------


def test_worker_rehydrate_loads_only_own_host_rows(tmp_path, monkeypatch):
    monkeypatch.setenv("CCSWITCH_DAEMON_ROLE", "worker")
    monkeypatch.setenv("CCSWITCH_HOST_NAME", "rocky")
    db_url = f"sqlite+aiosqlite:///{tmp_path/'c.db'}"

    async def run():
        await init_db(db_url=db_url)
        await _seed_sessions([
            _row("mine", "rocky"),
            _row("foreign", "dobby"),
            _row("legacy", None),   # NULL host = pre-fleet / primary-local → primary's
        ])
        orch = _new_orch()
        n = await orch.rehydrate()
        return n, set(r.id for r in orch.list_sessions())

    n, ids = asyncio.run(run())
    assert n == 1
    assert ids == {"mine"}


def test_primary_rehydrate_still_loads_every_row(tmp_path, monkeypatch):
    """The fleet dashboard is served from the primary's _records — partitioning
    the primary at load time would blank worker sessions from the UI."""
    monkeypatch.delenv("CCSWITCH_DAEMON_ROLE", raising=False)
    monkeypatch.setenv("CCSWITCH_HOST_NAME", "dobby")
    db_url = f"sqlite+aiosqlite:///{tmp_path/'c.db'}"

    async def run():
        await init_db(db_url=db_url)
        await _seed_sessions([
            _row("mine", "dobby"), _row("foreign", "rocky"), _row("legacy", None),
        ])
        orch = _new_orch()
        await orch.rehydrate()
        recs = {r.id: r for r in orch.list_sessions()}
        return recs

    recs = asyncio.run(run())
    assert set(recs) == {"mine", "foreign", "legacy"}
    assert recs["foreign"].host == "rocky"   # stamp survives into the record


# ---------------------------------------------------------------------------
# _owns_record
# ---------------------------------------------------------------------------


def test_owns_record_worker_owns_exactly_its_stamp(monkeypatch):
    monkeypatch.setenv("CCSWITCH_DAEMON_ROLE", "worker")
    monkeypatch.setenv("CCSWITCH_HOST_NAME", "rocky")
    orch = _new_orch()
    enabled = frozenset({"rocky", "dobby"})
    assert orch._owns_record(_record("s1", "rocky"), enabled) is True
    assert orch._owns_record(_record("s2", "dobby"), enabled) is False
    assert orch._owns_record(_record("s3", None), enabled) is False


def test_owns_record_primary_owns_null_self_local_and_orphans(monkeypatch):
    monkeypatch.delenv("CCSWITCH_DAEMON_ROLE", raising=False)
    monkeypatch.setenv("CCSWITCH_HOST_NAME", "dobby")
    orch = _new_orch()
    enabled = frozenset({"dobby", "rocky"})
    assert orch._owns_record(_record("s1", None), enabled) is True      # legacy/own spawn
    assert orch._owns_record(_record("s2", "dobby"), enabled) is True   # own stamp
    assert orch._owns_record(_record("s3", "local"), enabled) is True   # UDS = this host
    assert orch._owns_record(_record("s4", "rocky"), enabled) is False  # live worker's
    assert orch._owns_record(_record("s5", "gone"), enabled) is True    # orphan → adopt


def test_owns_record_unknown_registry_skips_foreign_stamps(monkeypatch):
    """Registry unreadable (None): delayed recovery of an orphan beats racing a
    live worker — foreign stamps are skipped, own/NULL rows still owned."""
    monkeypatch.delenv("CCSWITCH_DAEMON_ROLE", raising=False)
    monkeypatch.setenv("CCSWITCH_HOST_NAME", "dobby")
    orch = _new_orch()
    assert orch._owns_record(_record("s1", "rocky"), None) is False
    assert orch._owns_record(_record("s2", None), None) is True


def test_owns_record_live_local_driver_always_owned(monkeypatch):
    monkeypatch.setenv("CCSWITCH_DAEMON_ROLE", "worker")
    monkeypatch.setenv("CCSWITCH_HOST_NAME", "rocky")
    orch = _new_orch()
    rec = _record("s1", "dobby")
    orch._drivers[rec.id] = object()  # the driver lives HERE, whatever the stamp says
    assert orch._owns_record(rec, frozenset({"dobby"})) is True


# ---------------------------------------------------------------------------
# Acting loops gate on ownership
# ---------------------------------------------------------------------------


def test_stuck_sweep_skips_foreign_owned_rows(tmp_path, monkeypatch):
    monkeypatch.delenv("CCSWITCH_DAEMON_ROLE", raising=False)
    monkeypatch.setenv("CCSWITCH_HOST_NAME", "dobby")
    db_url = f"sqlite+aiosqlite:///{tmp_path/'c.db'}"

    async def run():
        await init_db(db_url=db_url)
        await _seed_hosts([
            dict(name="dobby", endpoint="https://dobby.example:9443", enabled=True),
            dict(name="rocky", endpoint="https://rocky.example:9443", enabled=True),
            dict(name="gone", endpoint="https://gone.example:9443", enabled=False),
        ])
        orch = _new_orch()
        orch._records = {
            "own": _record("own", "dobby"),
            "null": _record("null", None),
            "foreign": _record("foreign", "rocky"),
            "orphan": _record("orphan", "gone"),
            "idle": _record("idle", None, status="idle"),
        }
        rotated: list[str] = []

        async def _fake_rotate(record, *, reason):
            rotated.append(record.id)

        orch._rotate_away = _fake_rotate
        await orch._sweep_needs_attention()
        # Let the _spawn()ed rotation coroutines run.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        return rotated

    rotated = asyncio.run(run())
    assert sorted(rotated) == ["null", "orphan", "own"]


def test_disabled_account_sweep_skips_foreign_owned_rows(tmp_path, monkeypatch):
    monkeypatch.delenv("CCSWITCH_DAEMON_ROLE", raising=False)
    monkeypatch.setenv("CCSWITCH_HOST_NAME", "dobby")
    db_url = f"sqlite+aiosqlite:///{tmp_path/'c.db'}"

    async def run():
        await init_db(db_url=db_url)
        await _seed_hosts([
            dict(name="dobby", endpoint="https://dobby.example:9443", enabled=True),
            dict(name="rocky", endpoint="https://rocky.example:9443", enabled=True),
        ])
        orch = _new_orch()
        orch._records = {
            "own": _record("own", "dobby", status="idle"),
            "foreign": _record("foreign", "rocky", status="idle"),
        }
        rotated: list[str] = []

        async def _fake_rotate(record, *, reason):
            rotated.append(record.id)

        orch._rotate_away = _fake_rotate
        monkeypatch.setattr(
            "asyncio.to_thread", AsyncMock(return_value={"acct"}))
        await orch._check_disabled_account_sessions()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        return rotated

    rotated = asyncio.run(run())
    assert rotated == ["own"]


def test_titling_sweep_skips_foreign_owned_rows(tmp_path, monkeypatch):
    """#1033: titling reads the transcript and runs a Claude query IN THE SESSION'S
    cwd — a path that exists only on the OWNING host. On the primary's mirror of a
    worker session every pass hit the vanished-cwd branch, logging
    `session.titling_skipped … cwd missing` on a session that is perfectly healthy
    where it actually runs (and whose own daemon titles it)."""
    monkeypatch.delenv("CCSWITCH_DAEMON_ROLE", raising=False)
    monkeypatch.setenv("CCSWITCH_HOST_NAME", "dobby")
    db_url = f"sqlite+aiosqlite:///{tmp_path/'c.db'}"

    async def run():
        await init_db(db_url=db_url)
        await _seed_hosts([
            dict(name="dobby", endpoint="https://dobby.example:9443", enabled=True),
            dict(name="rocky", endpoint="https://rocky.example:9443", enabled=True),
        ])
        orch = _new_orch()
        orch._records = {
            "own": _record("own", "dobby", status="idle"),
            "null": _record("null", None, status="idle"),
            "foreign": _record("foreign", "rocky", status="idle"),
        }
        titled: list[str] = []

        async def _fake_apply(sid):
            titled.append(sid)

        orch._maybe_apply_auto_name = _fake_apply
        await orch._titling_sweep()
        return titled

    assert sorted(asyncio.run(run())) == ["null", "own"]


def test_goal_sweep_skips_foreign_owned_rows(tmp_path, monkeypatch):
    """Same shape as the titling sweep (#1033): infer_goal runs a Claude query in the
    session's cwd, so on a foreign mirror it can only fail with "Working directory
    does not exist" — repeatedly, on a 2-minute timer."""
    monkeypatch.delenv("CCSWITCH_DAEMON_ROLE", raising=False)
    monkeypatch.setenv("CCSWITCH_HOST_NAME", "dobby")
    db_url = f"sqlite+aiosqlite:///{tmp_path/'c.db'}"

    async def run():
        await init_db(db_url=db_url)
        await _seed_hosts([
            dict(name="dobby", endpoint="https://dobby.example:9443", enabled=True),
            dict(name="rocky", endpoint="https://rocky.example:9443", enabled=True),
        ])
        orch = _new_orch()
        orch._records = {
            "own": _record("own", "dobby", status="idle"),
            "foreign": _record("foreign", "rocky", status="idle"),
        }
        inferred: list[str] = []

        async def _fake_window(sid, limit=1):
            return SimpleNamespace(total_turns=3)

        async def _fake_infer(sid):
            inferred.append(sid)
            return None

        orch.transcript_window = _fake_window
        orch._infer_goal_for = _fake_infer
        await orch._goal_sweep()
        return inferred

    assert asyncio.run(run()) == ["own"]


# ---------------------------------------------------------------------------
# Worker read-only usage poller
# ---------------------------------------------------------------------------


def _captured_usage_calls(monkeypatch):
    calls: list[dict] = []

    def _fake_usage_for(name, *, allow_refresh=True, path="unknown"):
        calls.append({"name": name, "allow_refresh": allow_refresh, "path": path})
        return None

    monkeypatch.setattr(
        "server.sessions.orchestrator.ccswitch_accounts.usage_for", _fake_usage_for)
    return calls


def test_worker_poller_never_refreshes_tokens(monkeypatch):
    monkeypatch.setenv("CCSWITCH_DAEMON_ROLE", "worker")
    calls = _captured_usage_calls(monkeypatch)
    orch = _new_orch()
    orch._poller._usage_fn("acct-idle")   # no live session either way
    assert calls == [
        {"name": "acct-idle", "allow_refresh": False, "path": "poller-idle"}]


def test_primary_poller_keeps_refreshing_idle_accounts(monkeypatch):
    monkeypatch.delenv("CCSWITCH_DAEMON_ROLE", raising=False)
    calls = _captured_usage_calls(monkeypatch)
    orch = _new_orch()
    orch._poller._usage_fn("acct-idle")
    assert calls == [
        {"name": "acct-idle", "allow_refresh": True, "path": "poller-idle"}]

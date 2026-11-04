"""Per-session PR auto-fix toggle (D-14): ``sessions.pr_autofix_paused``.

PR Doctor's single decision point, ``_pr_doctor_step``, reads the flag from the
session's DB ROW — the sweep walks every open PR in scope and often finds one whose
session has no live record on this daemon (a worker owns it, or it is archived), so
an in-memory read would miss it (Review Focus 3). A paused session's PR is skipped
with one INFO line and one ``skipped`` feed event; ``autopilot_paused`` is a separate
flag and stays independent. Also pins the setter, the record/wire/browser fields,
rehydrate, the route, and the remote / RPC / fleet / facade plumbing.
"""
from __future__ import annotations

import logging
import os
import types
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from server.db import db_session
from server.db.models import Session as DbSession
from server.ipc.client import DaemonError
from server.sessions.engine import AgentEngine
from server.sessions.facade import InProcessOrchestratorAdapter
from server.sessions.fleet_orchestrator import _PER_SESSION, FleetOrchestrator
from server.sessions.remote import RemoteOrchestrator
from server.sessions.types import SessionRecord
from server.tests.test_pr_doctor_step import (
    _build_orch,
    _init_pr_doctor_db,
    _install_spawn_recorder,
    _mechanical_boundary,
    _patch_ensure_clone,
    _pr,
    _set_mode,
)
from server.tests.test_pr_doctor_step import github_identity as github_identity

pytestmark = pytest.mark.usefixtures("pr_authors_trusted")

SID = "sess1"
TARGET = "o/r#7"


async def _add_row(*, paused: bool | None, autopilot: bool | None = None,
                   branch: str | None = "feat/x", authored: list[str] | None = None) -> None:
    async with db_session() as db:
        db.add(DbSession(id=SID, name="n", account_name="acct-a", cwd="/tmp", repo="o/r",
                         branch=branch, authored_branches=authored,
                         pr_autofix_paused=paused, autopilot_paused=autopilot))


async def _row() -> DbSession:
    async with db_session() as db:
        return (await db.execute(select(DbSession).where(DbSession.id == SID))).scalar_one()


def _gh_recorder(orch, monkeypatch) -> list[list[str]]:
    calls: list[list[str]] = []

    def fake_gh_run(args):
        calls.append(list(args))
        return types.SimpleNamespace(returncode=0)

    _mechanical_boundary(monkeypatch, orch)
    orch._gh_run = fake_gh_run  # type: ignore[assignment]
    return calls


async def _sweep(orch, monkeypatch) -> None:
    """Run the real ``_pr_doctor_sweep`` over one (repo, branch) pair."""
    import server.sessions.orchestrator as orch_mod
    monkeypatch.setattr(orch_mod, "drives_fleet_automations", lambda: True)
    orch._schedule_allows_tick = AsyncMock(return_value=True)  # type: ignore[method-assign]
    orch._pr_doctor_scope_pairs = AsyncMock(return_value=[("o/r", "feat/x")])  # type: ignore[method-assign]
    orch.automation_actionable = AsyncMock(return_value=[])  # type: ignore[method-assign]
    await orch._pr_doctor_sweep()


def _warnings(caplog) -> list[tuple[str, str]]:
    return [(r.name, r.getMessage()) for r in caplog.records if r.levelno >= logging.WARNING]


# ---- the PR Doctor check (Review Focus 3) ---------------------------------


@pytest.mark.asyncio
async def test_sweep_skips_a_paused_sessions_pr_with_no_live_record(monkeypatch, tmp_path, caplog):
    """The flag lives only on the DB row: no ``_records`` entry, no driver. A BEHIND PR
    in auto mode would be updated mechanically; paused, nothing touches GitHub."""
    caplog.set_level(logging.INFO)
    await _init_pr_doctor_db(tmp_path)
    await _set_mode(monkeypatch, "auto")
    await _add_row(paused=True)
    pr = _pr(merge_state_status="BEHIND", checks="passing")
    orch = _build_orch(pr, has_driver=False)
    orch._records = {}
    gh = _gh_recorder(orch, monkeypatch)

    await _sweep(orch, monkeypatch)
    await _sweep(orch, monkeypatch)  # a second cycle reports nothing new

    assert gh == []
    events = await orch._pr_doctor_ctx.list_events(target=TARGET)
    skipped = [e for e in events if e["kind"] == "skipped"]
    assert len(skipped) == 1
    assert skipped[0]["detail"]["reason"] == "pr_autofix_paused"
    assert not any(e["kind"] == "updated_branch" for e in events)
    paused_logs = [r for r in caplog.records if "pr_doctor.autofix_paused" in r.getMessage()]
    assert len(paused_logs) == 1 and paused_logs[0].levelno == logging.INFO
    assert SID in paused_logs[0].getMessage()
    assert _warnings(caplog) == []


@pytest.mark.asyncio
async def test_sweep_skips_a_paused_session_fix_spawn(monkeypatch, tmp_path):
    """Failing CI with no live driver would spawn a repair session; paused, it doesn't
    — and no attempt is consumed, so unpausing later gets a fresh try."""
    await _init_pr_doctor_db(tmp_path)
    await _set_mode(monkeypatch, "auto")
    await _add_row(paused=True)
    pr = _pr(checks="failing", head_sha="sha1")
    orch = _build_orch(pr, has_driver=False)
    orch._records = {}
    spawns: list[dict] = []
    _install_spawn_recorder(orch, spawns)
    _patch_ensure_clone(monkeypatch)

    await _sweep(orch, monkeypatch)

    assert spawns == []
    st = await orch._pr_doctor_ctx.get_target_state(TARGET)
    assert st.get("attempts", 0) == 0 and st.get("last_action_sha") is None


@pytest.mark.asyncio
async def test_authored_branch_match_counts_as_the_sessions_pr(monkeypatch, tmp_path):
    """A session owns every PR in ``authored_branches``, not only its live branch —
    the same set ``_pr_doctor_owning_rec`` matches."""
    await _init_pr_doctor_db(tmp_path)
    await _set_mode(monkeypatch, "auto")
    await _add_row(paused=True, branch="feat/other", authored=["feat/x"])
    pr = _pr(merge_state_status="BEHIND", checks="passing")
    orch = _build_orch(pr, has_driver=False)
    orch._records = {}
    gh = _gh_recorder(orch, monkeypatch)

    assert await orch._pr_doctor_step("o/r", "feat/x", pr) is False
    assert gh == []


@pytest.mark.asyncio
async def test_unpaused_sweep_behaves_as_before(monkeypatch, tmp_path):
    await _init_pr_doctor_db(tmp_path)
    await _set_mode(monkeypatch, "auto")
    await _add_row(paused=None)
    pr = _pr(merge_state_status="BEHIND", checks="passing")
    orch = _build_orch(pr, has_driver=False)
    orch._records = {}
    gh = _gh_recorder(orch, monkeypatch)

    await _sweep(orch, monkeypatch)

    assert len(gh) == 1 and "update-branch" in " ".join(gh[0])
    events = await orch._pr_doctor_ctx.list_events(target=TARGET)
    assert any(e["kind"] == "updated_branch" for e in events)
    assert not any(e["kind"] == "skipped" for e in events)


@pytest.mark.asyncio
async def test_explicit_false_is_unpaused(monkeypatch, tmp_path):
    await _init_pr_doctor_db(tmp_path)
    await _set_mode(monkeypatch, "auto")
    await _add_row(paused=False)
    pr = _pr(merge_state_status="BEHIND", checks="passing")
    orch = _build_orch(pr, has_driver=False)
    orch._records = {}
    gh = _gh_recorder(orch, monkeypatch)

    assert await orch._pr_doctor_step("o/r", "feat/x", pr) is True
    assert len(gh) == 1


@pytest.mark.asyncio
async def test_event_driven_transition_skips_a_paused_live_session(monkeypatch, tmp_path):
    """``_on_pr_change`` (the CI-transition trigger) reaches the same check: a live
    session with a driver, paused, gets no inject and no ``ownership_wait``."""
    import server.sessions.orchestrator as orch_mod
    await _init_pr_doctor_db(tmp_path)
    await _set_mode(monkeypatch, "auto")
    await _add_row(paused=True)
    monkeypatch.setattr(orch_mod, "drives_fleet_automations", lambda: True)
    monkeypatch.setattr(orch_mod.push_mod, "notify_event", AsyncMock())
    pr = _pr(checks="failing", head_sha="sha1")
    orch = _build_orch(pr, has_driver=True)
    sent: list[str] = []

    async def fake_send(session_id, text):
        sent.append(text)
        return "sent"

    orch.send_prompt = fake_send  # type: ignore[method-assign]

    await orch._on_pr_change("o/r", "feat/x", "passing", "failing")

    assert sent == []
    events = await orch._pr_doctor_ctx.list_events(target=TARGET)
    assert not any(e["kind"] == "ownership_wait" for e in events)
    assert any(e["kind"] == "skipped" and e["detail"]["reason"] == "pr_autofix_paused"
               for e in events)


@pytest.mark.asyncio
async def test_autopilot_paused_does_not_pause_pr_doctor(monkeypatch, tmp_path):
    """The two flags are independent: autopilot paused, auto-fix on → PR Doctor acts."""
    await _init_pr_doctor_db(tmp_path)
    await _set_mode(monkeypatch, "auto")
    await _add_row(paused=None, autopilot=True)
    pr = _pr(merge_state_status="BEHIND", checks="passing")
    orch = _build_orch(pr, has_driver=False)
    orch._records = {}
    gh = _gh_recorder(orch, monkeypatch)

    assert await orch._pr_doctor_step("o/r", "feat/x", pr) is True
    assert len(gh) == 1


# ---- setter, record, rehydrate --------------------------------------------


def _rec(**kw) -> SessionRecord:
    return SessionRecord(id=SID, name="n", account="acct-a", engine=AgentEngine.CLAUDE_CODE,
                         engine_session_id="", cols=80, rows=24, cwd="/tmp", repo="o/r", **kw)


@pytest.mark.asyncio
async def test_setter_persists_survives_rehydrate_and_leaves_autopilot_alone(isolated_db):
    import server.sessions.orchestrator as orch_mod
    await _add_row(paused=None, autopilot=None)
    orch = orch_mod.SessionOrchestrator.__new__(orch_mod.SessionOrchestrator)
    orch._records = {SID: _rec()}

    await orch.set_pr_autofix_paused(SID, True)

    assert orch._records[SID].pr_autofix_paused is True
    assert orch._records[SID].autopilot_paused is False
    row = await _row()
    assert row.pr_autofix_paused is True
    assert not row.autopilot_paused
    rebuilt = orch_mod.SessionOrchestrator._record_from_row(row)
    assert rebuilt.pr_autofix_paused is True and rebuilt.autopilot_paused is False

    await orch.set_pr_autofix_paused(SID, False)
    assert orch._records[SID].pr_autofix_paused is False
    assert (await _row()).pr_autofix_paused is False


@pytest.mark.asyncio
async def test_setter_writes_the_row_with_no_live_record(isolated_db):
    """The owning daemon may hold the row only (a queued/archived session)."""
    import server.sessions.orchestrator as orch_mod
    await _add_row(paused=None)
    orch = orch_mod.SessionOrchestrator.__new__(orch_mod.SessionOrchestrator)
    orch._records = {}
    await orch.set_pr_autofix_paused(SID, True)
    assert (await _row()).pr_autofix_paused is True


@pytest.mark.asyncio
async def test_autopilot_setter_leaves_pr_autofix_alone(isolated_db):
    import server.sessions.orchestrator as orch_mod
    await _add_row(paused=True)
    orch = orch_mod.SessionOrchestrator.__new__(orch_mod.SessionOrchestrator)
    orch._records = {SID: _rec(pr_autofix_paused=True)}
    await orch.set_autopilot_paused(SID, True)
    await orch.set_autopilot_paused(SID, False)
    assert orch._records[SID].pr_autofix_paused is True
    assert (await _row()).pr_autofix_paused is True


def test_rehydrate_maps_a_null_column_to_false():
    import server.sessions.orchestrator as orch_mod
    row = DbSession(id=SID, name="n", account_name="a", cwd="/tmp", pr_autofix_paused=None)
    assert orch_mod.SessionOrchestrator._record_from_row(row).pr_autofix_paused is False


def test_wire_round_trip_and_browser_payload():
    from server.api.sessions import _record_to_dict
    d = _rec(pr_autofix_paused=True).to_dict()
    assert d["prAutofixPaused"] is True
    assert SessionRecord.from_dict(d).pr_autofix_paused is True
    assert _rec().to_dict()["prAutofixPaused"] is False
    legacy = _rec().to_dict()
    legacy.pop("prAutofixPaused")  # an old daemon's record
    assert SessionRecord.from_dict(legacy).pr_autofix_paused is False
    assert _record_to_dict(_rec(pr_autofix_paused=True), repo=None)["prAutofixPaused"] is True
    assert _record_to_dict(_rec(), repo=None)["prAutofixPaused"] is False


# ---- route -----------------------------------------------------------------


def _client():
    from server.tests.test_sessions_actions_api import _make_client
    client, fake = _make_client()
    fake.set_pr_autofix_paused = AsyncMock()
    return client, fake


def _post(client, sid, body):
    from server.tests.test_sessions_actions_api import _H
    return client.post(f"/api/sessions/{sid}/pr-autofix", headers=_H, json=body)


@pytest.mark.parametrize("enabled", [True, False])
def test_route_sets_the_flag_and_echoes_enabled(enabled):
    from server.tests.test_sessions_actions_api import _ENV
    client, fake = _client()
    sid = fake._record.id
    with patch.dict(os.environ, _ENV), client:
        r = _post(client, sid, {"enabled": enabled})
    assert r.status_code == 200, r.text
    assert r.json() == {"enabled": enabled}
    fake.set_pr_autofix_paused.assert_awaited_once_with(sid, not enabled)


def test_route_unknown_session_is_404():
    from server.tests.test_sessions_actions_api import _ENV
    client, fake = _client()
    with patch.dict(os.environ, _ENV), client:
        r = _post(client, "nope", {"enabled": False})
    assert r.status_code == 404
    fake.set_pr_autofix_paused.assert_not_awaited()


def test_route_requires_enabled():
    from server.tests.test_sessions_actions_api import _ENV
    client, fake = _client()
    with patch.dict(os.environ, _ENV), client:
        r = _post(client, fake._record.id, {})
    assert r.status_code == 422


def test_route_old_daemon_is_409_daemon_outdated():
    from server.tests.test_sessions_actions_api import _ENV
    client, fake = _client()
    fake.set_pr_autofix_paused = AsyncMock(
        side_effect=DaemonError("UnknownMethod", "set_pr_autofix_paused"))
    with patch.dict(os.environ, _ENV), client:
        r = _post(client, fake._record.id, {"enabled": False})
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "daemon_outdated"


# ---- remote / RPC / fleet / facade -----------------------------------------


class _RecordingClient:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    async def call(self, method, *, timeout=None, **kwargs):
        self.calls.append((method, kwargs))
        return None


@pytest.mark.asyncio
async def test_remote_forwards_to_the_daemon():
    client = _RecordingClient()
    await RemoteOrchestrator(client).set_pr_autofix_paused(SID, True)  # type: ignore[arg-type]
    assert client.calls == [("set_pr_autofix_paused", {"session_id": SID, "paused": True})]


def test_rpc_registers_the_method():
    import inspect

    import server.ipc.orchestrator_rpc as rpc
    assert '"set_pr_autofix_paused"' in inspect.getsource(rpc)


@pytest.mark.asyncio
async def test_fleet_routes_to_the_owning_host_and_facade_forwards():
    assert "set_pr_autofix_paused" in _PER_SESSION

    class _HostOrch:
        def __init__(self, name):
            self.name, self.calls = name, []

        async def set_pr_autofix_paused(self, session_id, paused):
            self.calls.append((session_id, paused))

    class _Router:
        def __init__(self, orchs):
            self._orchs = orchs

        def get(self, name):
            return self._orchs[name]

        def names(self):
            return sorted(self._orchs)

    dobby, rocky = _HostOrch("dobby"), _HostOrch("rocky")
    fo = FleetOrchestrator(_Router({"dobby": dobby, "rocky": rocky}), primary="dobby")  # type: ignore[arg-type]
    fo._host_cache[SID] = "rocky"
    await fo.set_pr_autofix_paused(SID, True)
    assert rocky.calls == [(SID, True)] and dobby.calls == []
    await InProcessOrchestratorAdapter(dobby).set_pr_autofix_paused(SID, False)  # type: ignore[arg-type]
    assert dobby.calls == [(SID, False)]

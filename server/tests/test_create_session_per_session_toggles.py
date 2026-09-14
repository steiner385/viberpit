"""Per-session autopilot / auto-archive toggles at session creation.

`POST /api/sessions` accepts `autopilot: false` / `autoArchive: false`. Both land on
the two EXISTING per-session opt-out columns — `autopilot_paused` and
`auto_archive_opt_out_reason` — so the autopilot governance filter and the
completion auto-archive gate need no new branches (one source of truth, no parallel
"enabled" columns). Omitted / true keeps the create request byte-compatible with
the pre-toggle contract: no kwarg reaches the orchestrator, so a daemon that
predates the toggles keeps binding the call (rolling-upgrade posture, same as
`client_node` / `display`).
"""
from __future__ import annotations

import asyncio
import os
import tempfile
from unittest.mock import patch

import pytest
from sqlalchemy import select

from server.db import db_session, init_db
from server.db.models import Session as DbSession, SessionQueue
from server.sessions.orchestrator import SessionOrchestrator
from server.sessions.remote import RemoteOrchestrator
from server.sessions.types import AUTO_ARCHIVE_OPT_OUT_CREATE
from server.tests.fakes.fake_factory import fake_sdk_factory
from server.tests.test_create_session_model import _fake_usage, _seed_account, _spawn_env
from server.tests.test_sessions_create_host_prefer import (
    _H, FakeOrch, _build_client, _make_env,
)


# ------------------------------------------------------------------ API layer


def _post(body: dict) -> dict:
    """POST /api/sessions through the FakeOrch harness; return the create kwargs."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
        db_url = f"sqlite+aiosqlite:///{tf.name}"
    try:
        fake = FakeOrch()
        client = _build_client(fake, db_url)
        with patch.dict(os.environ, _make_env(db_url)), client:
            resp = client.post("/api/sessions", json=body, headers=_H)
        assert resp.status_code == 201, resp.text
        return fake.last_create_kwargs
    finally:
        os.unlink(tf.name)


def test_api_maps_false_toggles_onto_the_existing_opt_out_columns():
    kwargs = _post({"initialPrompt": "hi", "autopilot": False, "autoArchive": False})
    assert kwargs["autopilot_paused"] is True
    assert kwargs["auto_archive_opt_out_reason"] == AUTO_ARCHIVE_OPT_OUT_CREATE


def test_api_toggles_are_independent():
    kwargs = _post({"initialPrompt": "hi", "autopilot": False})
    assert kwargs["autopilot_paused"] is True
    assert "auto_archive_opt_out_reason" not in kwargs

    kwargs = _post({"initialPrompt": "hi", "autoArchive": False})
    assert "autopilot_paused" not in kwargs
    assert kwargs["auto_archive_opt_out_reason"] == AUTO_ARCHIVE_OPT_OUT_CREATE


@pytest.mark.parametrize("body", [
    {"initialPrompt": "hi"},
    {"initialPrompt": "hi", "autopilot": True, "autoArchive": True},
    {"initialPrompt": "hi", "autopilot": None, "autoArchive": None},
])
def test_api_omits_kwargs_unless_a_toggle_is_off(body):
    """Governed-by-default must be the byte-identical legacy request: an older
    daemon binds `create_session(**params)` and would TypeError on an unknown key."""
    kwargs = _post(body)
    assert "autopilot_paused" not in kwargs
    assert "auto_archive_opt_out_reason" not in kwargs


# ------------------------------------------------------- orchestrator paths


async def _row(sid: str) -> DbSession:
    async with db_session() as db:
        return (await db.execute(select(DbSession).where(DbSession.id == sid))).scalar_one()


async def _queue_params(sid: str) -> dict:
    async with db_session() as db:
        entry = (await db.execute(
            select(SessionQueue).where(SessionQueue.session_id == sid))).scalar_one()
        return dict(entry.params)


def _run_with_orch(tmp_path, body):
    """Run *body(orch)* against a real, started orchestrator (fake SDK) inside the
    isolated spawn environment shared with test_create_session_model.py."""
    db_url, repo_root, patches = _spawn_env(tmp_path, db_name="toggles.db")
    from contextlib import ExitStack
    with ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        _seed_account("alpha")

        async def _run():
            await init_db(db_url=db_url)
            orch = SessionOrchestrator(
                sdk_factory=fake_sdk_factory(), poll_interval=9999.0,
                usage_fn=_fake_usage, repo_root=str(repo_root),
            )
            await orch.start()
            try:
                return await body(orch)
            finally:
                await orch.stop()

        return asyncio.run(_run())


def test_fresh_spawn_persists_both_toggles(tmp_path):
    async def body(orch):
        rec = await orch.create_session(
            "auto", autopilot_paused=True,
            auto_archive_opt_out_reason=AUTO_ARCHIVE_OPT_OUT_CREATE)
        assert rec.id in orch._drivers, f"not spawned: status={rec.status!r}"
        row = await _row(rec.id)
        return rec, row

    rec, row = _run_with_orch(tmp_path, body)
    # In-memory record — what _autopilot_list_governed and the completion gate read.
    assert rec.autopilot_paused is True
    assert rec.auto_archive_opt_out_reason == AUTO_ARCHIVE_OPT_OUT_CREATE
    # DB row — what rehydrate rebuilds the record from after a daemon restart.
    assert row.autopilot_paused is True
    assert row.auto_archive_opt_out_reason == AUTO_ARCHIVE_OPT_OUT_CREATE


def test_fresh_spawn_defaults_leave_the_session_governed(tmp_path):
    async def body(orch):
        rec = await orch.create_session("auto")
        return rec, await _row(rec.id)

    rec, row = _run_with_orch(tmp_path, body)
    assert not rec.autopilot_paused
    assert rec.auto_archive_opt_out_reason is None
    assert not row.autopilot_paused
    assert row.auto_archive_opt_out_reason is None


def test_queued_session_carries_toggles_through_the_queue_drain(tmp_path):
    """At capacity the session is a queued row + a params blob; the later drain
    re-spawns it from that blob. Both hops must keep the toggles, and the blob
    must carry the keys ONLY when set (mixed-version daemons splat it into an
    older `_spawn_session`)."""
    async def body(orch):
        with patch.object(orch, "_max_active_sessions", return_value=0):
            queued = await orch.create_session(
                "auto", autopilot_paused=True,
                auto_archive_opt_out_reason=AUTO_ARCHIVE_OPT_OUT_CREATE)
            plain = await orch.create_session("auto")
        assert queued.status == "queued" and plain.status == "queued"
        assert queued.autopilot_paused is True
        assert queued.auto_archive_opt_out_reason == AUTO_ARCHIVE_OPT_OUT_CREATE
        queued_row = await _row(queued.id)
        assert queued_row.autopilot_paused is True
        assert queued_row.auto_archive_opt_out_reason == AUTO_ARCHIVE_OPT_OUT_CREATE

        toggled_params = await _queue_params(queued.id)
        assert toggled_params["autopilot_paused"] is True
        assert toggled_params["auto_archive_opt_out_reason"] == AUTO_ARCHIVE_OPT_OUT_CREATE
        plain_params = await _queue_params(plain.id)
        assert "autopilot_paused" not in plain_params
        assert "auto_archive_opt_out_reason" not in plain_params

        # Drain: the queue promotes the head entry into a live spawn.
        started = await orch.maybe_start_queued()
        assert started >= 1
        live = orch._records[queued.id]
        assert live.status != "queued"
        return live, await _row(queued.id)

    live, row = _run_with_orch(tmp_path, body)
    assert live.autopilot_paused is True
    assert live.auto_archive_opt_out_reason == AUTO_ARCHIVE_OPT_OUT_CREATE
    assert row.autopilot_paused is True
    assert row.auto_archive_opt_out_reason == AUTO_ARCHIVE_OPT_OUT_CREATE


def test_spawn_keeps_a_pause_applied_while_queued(tmp_path):
    """Sibling of the above: a user who pauses autopilot on a QUEUED session (card
    menu → set_autopilot_paused) must not see the pause vanish from the live
    record when the queue drains — the spawn's fresh record inherits the prior
    one's opt-outs, not just its created_at/lineage."""
    async def body(orch):
        with patch.object(orch, "_max_active_sessions", return_value=0):
            queued = await orch.create_session("auto")
        assert queued.status == "queued"
        await orch.set_autopilot_paused(queued.id, True)
        assert await orch.maybe_start_queued() >= 1
        return orch._records[queued.id]

    live = _run_with_orch(tmp_path, body)
    assert live.status != "queued"
    assert live.autopilot_paused is True


# --------------------------------------------------------------- remote proxy


@pytest.mark.asyncio
async def test_remote_create_retries_without_toggles_on_old_daemon_then_pauses(caplog):
    """A daemon predating the toggles rejects the kwargs at call binding. The edge
    retries without them so the session still starts, then applies the autopilot
    half through `set_autopilot_paused` — an RPC every daemon already has."""
    from server.ipc.client import DaemonError

    class _Old:
        def __init__(self): self.calls = []
        async def call(self, method, **params):
            self.calls.append((method, params))
            if method == "set_autopilot_paused":
                return None
            if "autopilot_paused" in params:
                raise DaemonError("TypeError",
                                  "create_session() got an unexpected keyword argument 'autopilot_paused'")
            return {"session": {"id": "s1", "name": "n", "account": "a", "engine": "claude_code",
                    "engineSessionId": "", "cols": 80, "rows": 24, "cwd": "/tmp"}}

    c = _Old()
    rec = await RemoteOrchestrator(c).create_session(
        name="n", autopilot_paused=True,
        auto_archive_opt_out_reason=AUTO_ARCHIVE_OPT_OUT_CREATE)
    assert rec.id == "s1"
    methods = [m for m, _ in c.calls]
    assert methods == ["create_session", "create_session", "set_autopilot_paused"]
    retry = c.calls[1][1]
    assert "autopilot_paused" not in retry and "auto_archive_opt_out_reason" not in retry
    assert c.calls[2][1] == {"session_id": "s1", "paused": True}
    assert "predates per-session toggles" in caplog.text


@pytest.mark.asyncio
async def test_remote_create_forwards_toggles_to_a_current_daemon():
    class _New:
        def __init__(self): self.calls = []
        async def call(self, method, **params):
            self.calls.append((method, params))
            return {"session": {"id": "s1", "name": "n", "account": "a", "engine": "claude_code",
                    "engineSessionId": "", "cols": 80, "rows": 24, "cwd": "/tmp"}}

    c = _New()
    await RemoteOrchestrator(c).create_session(
        name="n", autopilot_paused=True,
        auto_archive_opt_out_reason=AUTO_ARCHIVE_OPT_OUT_CREATE)
    assert len(c.calls) == 1
    assert c.calls[0][1]["autopilot_paused"] is True
    assert c.calls[0][1]["auto_archive_opt_out_reason"] == AUTO_ARCHIVE_OPT_OUT_CREATE

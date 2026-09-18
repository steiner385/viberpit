"""Pure hold rules (spec 2026-09-18 §3, §8): kinds, actor classes, permissions."""
from datetime import datetime, timezone

import pytest

from server.db.models import HOLD_KINDS, WORK_ITEM_TRIAGE_STATES, WorkItem
from server.pm.holds import (
    AGENT_SETTABLE, HoldPermissionError, actor_class, clear_hold, may_set, set_hold,
)

NOW = datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc)


def test_vocabularies():
    assert HOLD_KINDS == ("evidence", "stronger_model", "decision", "owner_action")
    assert "needs_human" not in WORK_ITEM_TRIAGE_STATES
    assert AGENT_SETTABLE == frozenset({"evidence", "stronger_model"})


@pytest.mark.parametrize("actor,cls", [
    ("tony@example.com", "person"),
    ("session:abc-123", "agent"),
    ("assistant:tony@example.com", "agent"),
    ("lan@local", "agent"),
    ("runner", "system"),
    ("automation:error-triage", "system"),
    ("migration", "system"),
])
def test_actor_class(actor, cls):
    assert actor_class(actor) == cls


def test_may_set_by_class():
    person, agent, system = "tony@example.com", "session:s1", "runner"
    for kind in HOLD_KINDS:
        assert may_set(person, kind)
    assert may_set(agent, "evidence") and may_set(agent, "stronger_model")
    assert not may_set(agent, "decision") and not may_set(agent, "owner_action")
    # system actors (runner, automations) are the ladder and the reject flows: any kind.
    for kind in HOLD_KINDS:
        assert may_set(system, kind)


def test_set_hold_records_everything_and_refuses_bad_input():
    wi = WorkItem(repo="o/r", number=1, kind="issue", body="hello")
    set_hold(wi, kind="evidence", reason="profile the lag", actor="session:s1", now=NOW)
    assert wi.hold_kind == "evidence"
    assert wi.hold_reason == "profile the lag"
    assert wi.hold_by == "session:s1"
    assert wi.hold_set_at == NOW
    assert wi.hold_fingerprint  # body fingerprint captured
    with pytest.raises(HoldPermissionError):
        set_hold(wi, kind="owner_action", reason="x", actor="session:s1", now=NOW)
    with pytest.raises(ValueError):
        set_hold(wi, kind="evidence", reason="  ", actor="tony@example.com", now=NOW)
    with pytest.raises(ValueError):
        set_hold(wi, kind="bogus", reason="r", actor="tony@example.com", now=NOW)


def test_clear_hold_limited_to_settable_kinds():
    wi = WorkItem(repo="o/r", number=1, kind="issue", body="hello")
    set_hold(wi, kind="owner_action", reason="owner only", actor="tony@example.com", now=NOW)
    with pytest.raises(HoldPermissionError):
        clear_hold(wi, actor="session:s1")
    clear_hold(wi, actor="tony@example.com")
    assert wi.hold_kind is None and wi.hold_reason is None and wi.hold_by is None
    assert wi.hold_set_at is None and wi.hold_fingerprint is None
    # clearing nothing is a no-op for anyone
    clear_hold(wi, actor="session:s1")


def test_actor_for_request_reads_via_and_identity():
    from types import SimpleNamespace
    from server.pm.holds import actor_for_request
    person = SimpleNamespace(state=SimpleNamespace(identity="tony@example.com", via=None))
    assert actor_for_request(person) == "tony@example.com"
    drawer = SimpleNamespace(state=SimpleNamespace(identity="tony@example.com", via="assistant"))
    assert actor_for_request(drawer) == "assistant:tony@example.com"
    lan = SimpleNamespace(state=SimpleNamespace(identity="lan@local", via=None))
    assert actor_for_request(lan) == "lan@local"


def test_apply_session_hold_resolves_ref_and_limits_kind(tmp_path, monkeypatch):
    import asyncio
    from server.db import db_session, init_db
    from server.pm.holds import apply_session_hold
    monkeypatch.setenv("CCSWITCH_DB_URL", f"sqlite+aiosqlite:///{tmp_path}/t.db")

    async def _run():
        await init_db()
        async with db_session() as db:
            db.add(WorkItem(id="w1", repo="o/r", number=7, kind="issue", body="b",
                            triage_state="classified"))
            await db.flush()
            wi = await apply_session_hold(db, ref="o/r#7", kind="evidence",
                                          reason="needs a repro", session_id="s1", now=NOW)
            assert (wi.hold_kind, wi.hold_by) == ("evidence", "session:s1")
            with pytest.raises(HoldPermissionError):
                await apply_session_hold(db, ref="o/r#7", kind="owner_action",
                                         reason="park", session_id="s1", now=NOW)
            wi = await apply_session_hold(db, ref="o/r#7", kind=None, reason="found it",
                                          session_id="s1", now=NOW)
            assert wi.hold_kind is None
            with pytest.raises(ValueError):
                await apply_session_hold(db, ref="o/r#99", kind="evidence", reason="r",
                                         session_id="s1", now=NOW)
    asyncio.run(_run())

"""Always-allow rule storage (spec 2026-10-11 §2): scoped auto-profiles + rule stats."""
from __future__ import annotations

import asyncio
import os
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from server.app import create_app
from server.db import db_session
from server.db.models import PermissionProfile, PermissionRuleStats
from server.permission_rules import store
from server.sessions.orchestrator import SessionOrchestrator
from server.tests.fakes.fake_factory import fake_sdk_factory

_H = {"Tailscale-User-Login": "testuser@example.com"}
_ENV = {
    "VIBERPIT_DB_URL": "sqlite+aiosqlite:///:memory:",
    "VIBERPIT_DEV": "1",
    "VIBERPIT_TAILNET_ALLOWLIST": "",
    "VIBERPIT_SESSION_SECRET": "x" * 32,
}


def _run(coro):
    return asyncio.run(coro)


def _add(db, scope, key, pattern):
    return store.add_rule(db, scope=scope, scope_key=key, pattern=pattern,
                          created_by="me", created_via=None)


@pytest.mark.usefixtures("isolated_db")
class TestStore:
    def test_add_list_revoke_roundtrip(self):
        async def go():
            async with db_session() as db:
                r = await store.add_rule(db, scope="repo", scope_key="o/r", pattern="Bash(npm test:*)",
                                         created_by="me@x", created_via=None)
            async with db_session() as db:
                rules = await store.applicable_rules(db, "s1", "o/r")
                assert [x.pattern for x in rules] == ["Bash(npm test:*)"]
                assert rules[0].scope == "repo" and rules[0].hit_count == 0
                await store.record_hit(db, r.id)
            async with db_session() as db:
                got = await store.get_rule(db, r.id)
                assert got.hit_count == 1 and got.last_hit_at is not None
                await store.revoke_rule(db, r.id)
            async with db_session() as db:
                assert await store.applicable_rules(db, "s1", "o/r") == []
                # empty auto-profile is deleted with its last rule
                assert (await db.execute(select(PermissionProfile))).scalars().all() == []
        _run(go())

    def test_add_is_idempotent_and_scopes_are_isolated(self):
        async def go():
            async with db_session() as db:
                a = await _add(db, "session", "s1", "Bash(ls)")
                b = await _add(db, "session", "s1", "Bash(ls)")
                assert a.id == b.id
                await _add(db, "global", None, "WebSearch")
                await _add(db, "repo", "o/other", "Bash(make)")
            async with db_session() as db:
                got = [(r.scope, r.pattern) for r in await store.applicable_rules(db, "s1", "o/r")]
                assert got == [("session", "Bash(ls)"), ("global", "WebSearch")]
                assert [r.pattern for r in await store.applicable_rules(db, "s2", None)] == ["WebSearch"]
        _run(go())

    def test_update_keeps_position_and_rejects_duplicate(self):
        async def go():
            async with db_session() as db:
                a = await _add(db, "global", None, "Bash(a)")
                await _add(db, "global", None, "Bash(b)")
                await store.record_hit(db, a.id)
                u = await store.update_rule(db, a.id, new_pattern="Bash(a2)")
                assert u.position == 0 and u.pattern == "Bash(a2)" and u.id == a.id
                assert u.hit_count == 0 and u.last_hit_at is None
                with pytest.raises(ValueError, match="duplicate"):
                    await store.update_rule(db, a.id, new_pattern="Bash(b)")
                assert await store.update_rule(db, "nope", new_pattern="x") is None
        _run(go())

    def test_list_rules_order_and_filter(self):
        async def go():
            async with db_session() as db:
                await _add(db, "global", None, "Bash(g)")
                await _add(db, "repo", "o/b", "Bash(r2)")
                await _add(db, "repo", "o/a", "Bash(r1)")
                await _add(db, "session", "s1", "Bash(s)")
                await _add(db, "repo", "o/a", "Bash(r1b)")
            async with db_session() as db:
                got = [r.pattern for r in await store.list_rules(db)]
                assert got == ["Bash(s)", "Bash(r1)", "Bash(r1b)", "Bash(r2)", "Bash(g)"]
                only = await store.list_rules(db, scopes=[("repo", "o/b")])
                assert [r.pattern for r in only] == ["Bash(r2)"]
                assert [r.position for r in await store.list_rules(db, scopes=[("repo", "o/a")])] == [0, 1]
        _run(go())

    def test_revoke_one_of_two_keeps_profile_and_positions(self):
        async def go():
            async with db_session() as db:
                a = await _add(db, "global", None, "Bash(a)")
                await _add(db, "global", None, "Bash(b)")
                assert (await store.revoke_rule(db, a.id)).pattern == "Bash(a)"
                assert await store.revoke_rule(db, a.id) is None
            async with db_session() as db:
                rules = await store.list_rules(db)
                assert [(r.pattern, r.position) for r in rules] == [("Bash(b)", 0)]
                assert len((await db.execute(select(PermissionRuleStats))).scalars().all()) == 1
        _run(go())

    def test_profile_name_validation_and_wire(self):
        assert store.profile_name("global", None) == "always-allow:global"
        assert store.profile_name("repo", "o/r") == "always-allow:repo:o/r"
        assert store.profile_name("session", "s1") == "always-allow:session:s1"
        for bad in (("global", "x"), ("repo", None), ("bogus", None)):
            with pytest.raises(ValueError):
                store.profile_name(*bad)
        assert store.applicable_scopes("s1", "o/r") == [("session", "s1"), ("repo", "o/r"), ("global", None)]
        assert store.applicable_scopes("s1", None) == [("session", "s1"), ("global", None)]

        async def go():
            async with db_session() as db:
                r = await _add(db, "global", None, "WebSearch")
            w = r.to_wire()
            assert set(w) == {"id", "profileId", "pattern", "description", "scope", "scopeKey", "position", "createdBy",
                              "createdVia", "createdAt", "hitCount", "lastHitAt"}
            assert w["lastHitAt"] is None and isinstance(w["createdAt"], str)
        _run(go())


def _client():
    with patch.dict(os.environ, _ENV):
        orch = SessionOrchestrator(sdk_factory=fake_sdk_factory(), poll_interval=9999.0,
                                   usage_fn=lambda name: None)
        app = create_app(orchestrator=orch)
    return TestClient(app, raise_server_exceptions=False)


async def _seed_scoped():
    async with db_session() as db:
        r = await _add(db, "global", None, "WebSearch")
    return r.profile_id


def test_profile_api_hides_and_guards_scoped_profiles():
    client = _client()
    with patch.dict(os.environ, _ENV), client:
        pid = client.portal.call(_seed_scoped)
        ok = client.post("/api/permission-profiles", json={"name": "plain"}, headers=_H)
        assert ok.status_code == 201
        listed = client.get("/api/permission-profiles", headers=_H).json()
        assert [p["name"] for p in listed] == ["plain"]
        for call in (lambda: client.get(f"/api/permission-profiles/{pid}", headers=_H),
                     lambda: client.patch(f"/api/permission-profiles/{pid}", json={"description": "x"}, headers=_H),
                     lambda: client.delete(f"/api/permission-profiles/{pid}", headers=_H)):
            r = call()
            assert r.status_code == 409
            assert r.json()["detail"]["error"] == "managed_profile"
        r = client.post("/api/permission-profiles", json={"name": "always-allow:x"}, headers=_H)
        assert r.status_code == 409 and r.json()["detail"]["error"] == "managed_profile"


@pytest.mark.usefixtures("isolated_db")
class TestFixRound1:
    def test_record_hit_sets_last_hit_at_after_created_and_wire_is_utc(self):
        async def go():
            async with db_session() as db:
                r = await _add(db, "global", None, "WebSearch")
                await store.record_hit(db, r.id)
                await store.record_hit(db, "missing-id")  # no-op, no error
            async with db_session() as db:
                got = await store.get_rule(db, r.id)
                # SQLite CURRENT_TIMESTAMP has 1s resolution
                assert got.last_hit_at >= got.created_at.replace(microsecond=0) and got.hit_count == 1
                assert got.created_at.tzinfo is None
                w = got.to_wire()
                assert w["createdAt"].endswith("Z") and w["lastHitAt"].endswith("Z")
        _run(go())

    def test_add_rule_refuses_unscoped_profile_squatting_the_name(self):
        async def go():
            async with db_session() as db:
                db.add(PermissionProfile(name="always-allow:global", patterns=[]))
            async with db_session() as db:
                with pytest.raises(RuntimeError):
                    await _add(db, "global", None, "WebSearch")
        _run(go())


def test_profile_rename_into_managed_namespace_is_refused():
    client = _client()
    with patch.dict(os.environ, _ENV), client:
        pid = client.post("/api/permission-profiles", json={"name": "mine"}, headers=_H).json()["id"]
        r = client.patch(f"/api/permission-profiles/{pid}", json={"name": "always-allow:global"}, headers=_H)
        assert r.status_code == 409 and r.json()["detail"]["error"] == "managed_profile"


@pytest.mark.usefixtures("isolated_db")
class TestWireDescription:
    def test_wire_carries_description_and_falls_back_to_pattern(self):
        async def go():
            async with db_session() as db:
                r = await _add(db, "global", None, "WebSearch")
            async with db_session() as db:
                got = await store.get_rule(db, r.id)
                assert got.to_wire()["description"] == store.describe("WebSearch")
                with patch.object(store, "describe", side_effect=ValueError("bad")):
                    assert got.to_wire()["description"] == "WebSearch"
        _run(go())


@pytest.mark.usefixtures("isolated_db")
class TestFollowups2952:
    def test_delete_session_rules_drops_profile_and_stats_only_for_that_session(self):
        async def go():
            async with db_session() as db:
                await _add(db, "session", "s1", "Bash(ls)")
                await _add(db, "session", "s1", "Bash(make)")
                keep = await _add(db, "session", "s2", "Bash(ls)")
                await _add(db, "global", None, "WebSearch")
                await db.commit()
            async with db_session() as db:
                assert await store.delete_session_rules(db, "s1") == 2
                assert await store.delete_session_rules(db, "nope") == 0
                await db.commit()
            async with db_session() as db:
                left = [(r.scope, r.scope_key) for r in await store.list_rules(db)]
                assert left == [("session", "s2"), ("global", None)]
                stats = (await db.execute(select(PermissionRuleStats))).scalars().all()
                assert keep.id in {s.id for s in stats} and len(stats) == 2
        _run(go())

    @pytest.mark.parametrize("op", ["update", "revoke"])
    def test_rule_deleted_while_waiting_on_the_lock_is_not_found(self, monkeypatch, op):
        """A concurrent revoke that commits while we wait on the profile lock leaves our
        loaded stats row stale: refresh must report the rule gone, not raise (a 500)."""
        real = store._profile_for_stats

        async def deleted_meanwhile(db, stats):
            from sqlalchemy import delete
            await db.execute(delete(PermissionRuleStats).where(PermissionRuleStats.id == stats.id))
            return await real(db, stats)

        async def go():
            async with db_session() as db:
                r = await _add(db, "global", None, "WebSearch")
                await db.commit()
            monkeypatch.setattr(store, "_profile_for_stats", deleted_meanwhile)
            async with db_session() as db:
                if op == "update":
                    assert await store.update_rule(db, r.id, new_pattern="WebFetch") is None
                else:
                    assert await store.revoke_rule(db, r.id) is None
        _run(go())

    def test_launch_patterns_reads_the_session_row(self):
        async def go():
            from server.db.models import Session
            async with db_session() as db:
                db.add(Session(id="s1", name="n", account_name="a", cwd="/w",
                               tool_allowlist=["Bash(npm test:*)", 3]))
                await db.commit()
            async with db_session() as db:
                assert await store.launch_patterns(db, "s1") == ["Bash(npm test:*)"]
                assert await store.launch_patterns(db, "missing") == []
        _run(go())


def test_profile_patterns_are_safety_checked_and_human_only():
    client = _client()
    with patch.dict(os.environ, _ENV), client:
        r = client.post("/api/permission-profiles",
                        json={"name": "p", "patterns": ["Bash(npm test:*)", "Bash(git *)", "Bash"]},
                        headers=_H)
        assert r.status_code == 400
        d = r.json()["detail"]
        assert d["error"] == "pattern_refused" and len(d["reasons"]) == 2
        assert d["reasons"][0].startswith("Bash(git *): ")
        ok = client.post("/api/permission-profiles",
                         json={"name": "p", "patterns": ["Bash(npm test:*)", "Bash(make:*)"]},
                         headers=_H)
        assert ok.status_code == 201  # a warning-level pattern may be saved
        pid = ok.json()["id"]
        r = client.patch(f"/api/permission-profiles/{pid}", json={"patterns": ["Edit"]},
                         headers=_H)
        assert r.status_code == 200  # bare Edit warns at session scope
        r = client.patch(f"/api/permission-profiles/{pid}", json={"patterns": ["Edit(.env)"]},
                         headers=_H)
        assert r.status_code == 400
        # A description-only patch touches no patterns and stays open.
        assert client.patch(f"/api/permission-profiles/{pid}", json={"description": "d"},
                            headers=_H).status_code == 200

    from server.api.permission_profiles import _validate_patterns
    from fastapi import HTTPException

    class _Req:
        class state:
            via = "assistant"
    with pytest.raises(HTTPException) as exc:
        _validate_patterns(["Bash(npm test)"], "me@x", _Req())  # type: ignore[arg-type]
    assert exc.value.status_code == 403
    _validate_patterns([], "me@x", _Req())  # type: ignore[arg-type]  # no patterns: no check

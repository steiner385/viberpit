import asyncio

import pytest
from sqlalchemy import select

from server.db import db_session
from server.db.models import AuditEvent
from server.permission_rules import store
from server.permission_rules.gate import StandingRuleGate, first_match
from server.permission_rules.patterns import parse
from server.sessions.permissions import PermissionRegistry
from viberpit.brand import LEGACY_MCP_TOOL_PREFIX

pytestmark = pytest.mark.usefixtures("isolated_db")
FACTS = {"s1": ("o/r", "/w/repo"), "s2": (None, "/w/scratch")}


def _reg():
    reg = PermissionRegistry()
    reg.rule_gate = StandingRuleGate(FACTS.get)
    return reg


async def _add(scope, key, pattern):
    async with db_session() as db:
        return await store.add_rule(db, scope=scope, scope_key=key, pattern=pattern,
                                    created_by="me", created_via=None)


async def _ask(reg, sid, tool, inp, mode="interactive"):
    return await reg.request(sid, "t", tool, inp, mode=mode, denylist=PermissionRegistry.DANGEROUS,
                             identity="me")


@pytest.mark.parametrize("mode", ["interactive", "auto", "allowlist", "acceptEdits"])
def test_repo_rule_auto_allows_in_every_prompting_mode(mode):
    async def go():
        r = await _add("repo", "o/r", "Bash(npm test:*)")
        reg = _reg()
        pid, fut, _ = await _ask(reg, "s1", "Bash", {"command": "npm test -w"}, mode)
        assert pid is None and fut.result() is True
        async with db_session() as db:
            assert (await store.get_rule(db, r.id)).hit_count == 1
            rows = (await db.execute(
                select(AuditEvent).where(AuditEvent.type == "permission.rule_allow"))).scalars().all()
            assert len(rows) == 1
        pid2, _, _ = await _ask(reg, "s2", "Bash", {"command": "npm test"})
        assert pid2 is not None
    asyncio.run(go())


def test_revoke_takes_effect_on_next_request():
    async def go():
        r = await _add("session", "s1", "Bash(ls)")
        reg = _reg()
        assert (await _ask(reg, "s1", "Bash", {"command": "ls"}))[0] is None
        async with db_session() as db:
            await store.revoke_rule(db, r.id)
        assert (await _ask(reg, "s1", "Bash", {"command": "ls"}))[0] is not None
    asyncio.run(go())


def test_gate_fails_closed(monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("db down")
    monkeypatch.setattr("server.permission_rules.store.applicable_rules", boom)

    async def go():
        assert (await _ask(_reg(), "s1", "Bash", {"command": "ls"}))[0] is not None
    asyncio.run(go())


def test_plan_and_destructive_never_rule_resolved():
    async def go():
        await _add("global", None, "ExitPlanMode")
        await _add("global", None, "mcp__viberpit__delete_session")
        reg = _reg()
        assert (await _ask(reg, "s1", "ExitPlanMode", {}))[0] is not None
        pid, _, always_allowed = await _ask(reg, "s1", "mcp__viberpit__delete_session", {"id": "x"})
        assert pid is not None and always_allowed is False
        [_, p] = reg.pending("s1")
        assert p["always_allowed"] is False and p["suggestions"] == []
    asyncio.run(go())


def test_pending_carries_suggestions_and_always_allowed():
    async def go():
        reg = _reg()
        pid, _, _ = await _ask(reg, "s1", "Bash", {"command": "npm test"})
        [p] = reg.pending("s1")
        assert p["always_allowed"] is True and p["engine_managed"] is False
        assert p["owner"] == "me"
        assert p["suggestions"][0]["pattern"] == "Bash(npm test)"
        assert reg.suggestions_for(pid) == p["suggestions"]
    asyncio.run(go())


def test_external_prompt_is_engine_managed_with_no_suggestions():
    async def go():
        reg = _reg()
        await reg.request_external("s1", "t", "Bash", {"command": "ls"}, identity="me")
        [p] = reg.pending("s1")
        assert p["engine_managed"] is True and p["suggestions"] == []
    asyncio.run(go())


def test_kill_switch_disables_gate(monkeypatch):
    monkeypatch.setenv("VIBERPIT_PERMISSION_RULES_ENABLED", "false")

    async def go():
        await _add("session", "s1", "Bash(ls)")
        reg = _reg()
        pid, _, _ = await _ask(reg, "s1", "Bash", {"command": "ls"})
        assert pid is not None and reg.suggestions_for(pid) == []
    asyncio.run(go())


def test_first_match_attributes_hit_to_matching_rule():
    class R:
        def __init__(self, id, pattern):
            self.id, self.pattern = id, pattern
    rules = [R("a", "Bash(git status)"), R("bad", "((("), R("b", "Bash(npm test:*)")]
    assert first_match(rules, "Bash", {"command": "npm test -w"}, "/w").id == "b"  # type: ignore[arg-type]
    assert first_match(rules, "Bash", {"command": "rm x"}, "/w") is None  # type: ignore[arg-type]
    assert parse("Bash(git status)")


@pytest.mark.parametrize("tool,inp", [
    ("Bash", {"command": "npm test"}),
    ("mcp__viberpit__rename_session", {"id": "x", "title": "y"}),  # MUTATION branch
])
def test_deny_all_during_gate_await_leaves_no_ghost_card(monkeypatch, tool, inp):
    """A deny_all (interrupt / disconnect / prompt sent) landing while the gate is awaiting
    must win: no card is created afterwards and the request settles denied (#1504 shape)."""
    async def go():
        reg = _reg()
        entered, release = asyncio.Event(), asyncio.Event()

        async def slow_match(session_id, tool, input):
            entered.set()
            await release.wait()
            return None
        monkeypatch.setattr(reg.rule_gate, "match", slow_match)
        task = asyncio.ensure_future(_ask(reg, "s1", tool, inp))
        await entered.wait()
        reg.deny_all("s1")
        release.set()
        pid, fut, _ = await task
        assert pid is None and fut.result() is False
        assert reg.pending("s1") == []
    asyncio.run(go())


def test_deny_all_of_another_session_does_not_cancel_request(monkeypatch):
    async def go():
        reg = _reg()
        entered, release = asyncio.Event(), asyncio.Event()

        async def slow_match(session_id, tool, input):
            entered.set()
            await release.wait()
            return None
        monkeypatch.setattr(reg.rule_gate, "match", slow_match)
        task = asyncio.ensure_future(_ask(reg, "s1", "Bash", {"command": "npm test"}))
        await entered.wait()
        reg.deny_all("s2")
        release.set()
        pid, _, _ = await task
        assert pid is not None and len(reg.pending("s1")) == 1
    asyncio.run(go())


@pytest.mark.parametrize("tool", [
    "mcp__viberpit__resolve_permission", LEGACY_MCP_TOOL_PREFIX + "set_permission_mode",
    "mcp__viberpit__resolve_model_approval",
])
def test_app_server_rule_never_auto_allows_permission_controls(tool):
    async def go():
        await _add("global", None, "mcp__viberpit")
        await _add("global", None, LEGACY_MCP_TOOL_PREFIX.removesuffix("__"))
        await _add("global", None, tool)
        reg = _reg()
        pid, _, _ = await _ask(reg, "s1", tool, {"session_id": "x"})
        assert pid is not None
        assert reg.suggestions_for(pid) == []
        # ... while an ordinary MUTATION tool on the same server is still rule-allowed.
        pid2, fut2, _ = await _ask(reg, "s1", "mcp__viberpit__rename_session", {"id": "x"})
        assert pid2 is None and fut2.result() is True
    asyncio.run(go())

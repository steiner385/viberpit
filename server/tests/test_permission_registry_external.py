"""`PermissionRegistry.request_external` — the engine-enforced approval path.

Codex decides for itself when a human is needed (its sandbox/approval policy is
derived from the same ViberPit mode, see `server/codex/permissions.py`), so its
prompts enter the SAME registry every Claude prompt uses rather than a parallel
one. These tests pin the two behaviours that differ, and the two that must not.
"""
from __future__ import annotations


import pytest

from server.sessions.permissions import PermissionRegistry


@pytest.mark.asyncio
async def test_external_request_never_auto_approves():
    """No mode argument, and no mode shortcut: the engine already decided.

    `request` would return an immediately-resolved future for a dangerous tool in
    `full-auto`. Re-deriving that here would auto-ALLOW the very prompts Codex
    only raises in the modes where ViberPit asked for a human.
    """
    registry = PermissionRegistry()
    perm_id, future = await registry.request_external(
        "s1", "t1", "Bash", {"command": "rm -rf /"}, identity="alice")
    assert perm_id
    assert not future.done()
    assert [p["permId"] for p in registry.pending("s1")] == [perm_id]


@pytest.mark.asyncio
async def test_external_future_carries_the_raw_decision():
    registry = PermissionRegistry()
    perm_id, future = await registry.request_external(
        "s1", "t1", "Bash", {"command": "ls"}, identity="alice")
    assert registry.resolve(perm_id, "allow", "alice") is True
    assert await future == "allow"


@pytest.mark.asyncio
async def test_external_always_is_honoured_natively_not_cached_locally():
    """"Always allow" must become the ENGINE's session-scoped accept.

    A ViberPit-side entry would be a second approval policy that the engine
    never hears about — the "persistent global exception" Task 4.2 forbids.
    """
    registry = PermissionRegistry()
    perm_id, future = await registry.request_external(
        "s1", "t1", "Bash", {"command": "ls"}, identity="alice")
    assert registry.resolve(perm_id, "always", "alice") is True
    assert await future == "always"
    # Nothing was persisted: the next identical engine prompt asks again.
    perm_id2, future2 = await registry.request_external(
        "s1", "t2", "Bash", {"command": "ls"}, identity="alice")
    assert not future2.done() and [p["permId"] for p in registry.pending("s1")] == [perm_id2]


@pytest.mark.asyncio
async def test_external_prompt_is_owned_by_the_registering_identity():
    registry = PermissionRegistry()
    perm_id, future = await registry.request_external(
        "s1", "t1", "Bash", {}, identity="alice")
    assert registry.resolve(perm_id, "allow", "mallory") is False
    assert not future.done()
    assert registry.resolve(perm_id, "allow", "alice") is True


@pytest.mark.asyncio
async def test_external_prompt_follows_a_rethreaded_identity():
    registry = PermissionRegistry()
    perm_id, _ = await registry.request_external(
        "s1", "t1", "Bash", {}, identity="system")
    assert registry.rethread_identity("s1", "system", "alice") == 1
    assert registry.resolve(perm_id, "allow", "alice") is True


@pytest.mark.asyncio
async def test_deny_all_dismisses_an_external_prompt():
    """Stop / crash / disconnect must strand no future — the driver reads the
    non-string result as "declined"."""
    registry = PermissionRegistry()
    _, future = await registry.request_external("s1", "t1", "Bash", {}, identity="alice")
    registry.deny_all("s1")
    assert await future is False


@pytest.mark.asyncio
async def test_ordinary_always_allows_once_and_persists_nothing():
    """Spec 2026-10-11 §3.4: the edge makes a rule; the registry keeps no grant."""
    registry = PermissionRegistry()
    perm_id, future, _ = await registry.request(
        "s1", "t1", "Bash", {"command": "ls"}, mode="interactive",
        denylist=PermissionRegistry.DANGEROUS, allowlist=True, identity="alice")
    assert registry.resolve(perm_id, "always", "alice") is True
    assert await future is True
    pid2, _, _ = await registry.request(
        "s1", "t2", "Bash", {"command": "ls"}, mode="interactive",
        denylist=PermissionRegistry.DANGEROUS, allowlist=True, identity="alice")
    assert pid2 is not None

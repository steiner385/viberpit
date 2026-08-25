"""Automation-spawned sessions must not stall on permission prompts (#750).

`AutomationContext.spawn_session` passed no `permission_mode`, so every automation
session inherited `create_session`'s "interactive" default and blocked on its first
Edit or Bash — a Backlog Runner needs git/gh/pytest, so an unattended run stalled
forever waiting for a human who was not watching.

The fix resolves the mode the way the human path does, but falls back to "full-auto"
instead of "interactive". The vocabulary is ccswitch's OWN — already implemented in
`PermissionRegistry.request` and offered in the UI's canonical mode list — so no new
mode is introduced.

The load-bearing test here is `test_account_default_overrides_the_full_auto_fallback`.
It is what makes the fallback safe to ship: full-auto is a *fallback*, not an override,
so an operator who pins an account to a restrictive mode genuinely constrains the
unattended sessions running on it.
"""
from __future__ import annotations

import pytest

import server.automations.context as ctx_mod
from server.automations.context import AutomationContext


def _stub_account_defaults(monkeypatch, defaults: dict):
    """Point the account-default lookup at *defaults* without touching a real ledger."""
    monkeypatch.setattr(ctx_mod, "_account_defaults_for", lambda account: defaults)


# --------------------------------------------------------------------------- #
# The resolution itself — a pure function, so a security-relevant decision can  #
# be reasoned about without a database in the way.                             #
# --------------------------------------------------------------------------- #

def test_defaults_to_full_auto(monkeypatch):
    """No explicit mode and no account default -> full-auto, so the run does not stall."""
    _stub_account_defaults(monkeypatch, {})
    assert ctx_mod.resolve_permission_mode({"account": "acct"}) == "full-auto"


def test_account_default_overrides_the_full_auto_fallback(monkeypatch):
    """An operator who pinned the account to a restrictive mode constrains automations.

    Without this, pinning an account would silently fail to limit the unattended
    sessions running on it — which would make the fallback a blanket escalation
    rather than a default.
    """
    _stub_account_defaults(monkeypatch, {"defaultPermissionMode": "interactive"})
    assert ctx_mod.resolve_permission_mode({"account": "acct"}) == "interactive"


def test_explicit_mode_wins_over_everything(monkeypatch):
    """A deliberate choice by the automation is never clobbered by the fallback."""
    _stub_account_defaults(monkeypatch, {"defaultPermissionMode": "interactive"})
    assert ctx_mod.resolve_permission_mode(
        {"account": "acct", "permission_mode": "allowlist"}) == "allowlist"


def test_unpinned_account_does_not_raise(monkeypatch):
    """Automations normally pin an account, but an unpinned spawn must still work."""
    _stub_account_defaults(monkeypatch, {"defaultPermissionMode": "interactive"})
    # No account pinned -> the account default is not consulted at all.
    assert ctx_mod.resolve_permission_mode({}) == "full-auto"


def test_empty_account_default_falls_through(monkeypatch):
    """A ledger row present but with no mode set must not resolve to None/empty."""
    _stub_account_defaults(monkeypatch, {"defaultPermissionMode": None})
    assert ctx_mod.resolve_permission_mode({"account": "acct"}) == "full-auto"


def test_ledger_failure_does_not_break_the_spawn(monkeypatch):
    """A ledger read failure must not stop an automation from spawning.

    It degrades to the fallback rather than raising — an automation that cannot
    read a preference should still run, not crash the tick.
    """
    def _boom(account):
        raise RuntimeError("ledger unavailable")

    monkeypatch.setattr(ctx_mod, "_account_defaults_for", _boom)
    assert ctx_mod.resolve_permission_mode({"account": "acct"}) == "full-auto"


# --------------------------------------------------------------------------- #
# The wiring — spawn_session actually applies it.                              #
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_spawn_session_applies_the_resolved_mode(monkeypatch):
    """The resolved mode reaches create_session, not just the helper."""
    seen: dict = {}

    async def _create_session(**kwargs):
        seen.update(kwargs)
        return type("R", (), {"id": "s1"})()

    ctx = AutomationContext("backlog-runner", create_session=_create_session)

    async def _emit(*a, **k):
        return None

    monkeypatch.setattr(ctx, "emit", _emit)
    monkeypatch.setattr(ctx_mod, "resolve_permission_mode", lambda kw: "full-auto")

    # spawn_session's duplicate check queries the DB; stub that lookup out.
    async def _no_existing(self, key):
        return None

    monkeypatch.setattr(AutomationContext, "_existing_session", _no_existing,
                        raising=False)

    await ctx.spawn_session(origin_ref="o/r#1", account="acct", cwd="/tmp/wt")
    assert seen["permission_mode"] == "full-auto"


# --------------------------------------------------------------------------- #
# The human path must be untouched.                                            #
# --------------------------------------------------------------------------- #

def test_human_path_still_falls_back_to_interactive():
    """Asserted against the REAL create_session signature, not a double.

    A drifted stand-in could make this pass vacuously; the real signature cannot.
    """
    import inspect

    from server.sessions.orchestrator import SessionOrchestrator

    sig = inspect.signature(SessionOrchestrator.create_session)
    assert sig.parameters["permission_mode"].default == "interactive", (
        "create_session's default is the human fallback; changing it would silently "
        "escalate every UI-created session"
    )

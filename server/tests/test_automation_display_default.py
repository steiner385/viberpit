"""AutomationContext.spawn_session requests the native display by default (#1899).

An automation dispatch has no creating browser, so resolve_display_mode can never
prove locality for it. The default lives here rather than in that shared policy
because there ``client_node is None`` also means "a browser whose locality is
unknown" — which must stay virtual.
"""
from __future__ import annotations

import pytest

import server.automations.context as ctx_mod
from server.automations.context import AutomationContext


async def _spawn(monkeypatch, **kwargs) -> dict:
    seen: dict = {}

    async def _create_session(**kw):
        seen.update(kw)
        return type("R", (), {"id": "s1"})()

    async def _emit(*a, **k):
        return None

    async def _no_existing(self, key):
        return None

    ctx = AutomationContext("backlog-runner", create_session=_create_session)
    monkeypatch.setattr(ctx, "emit", _emit)
    monkeypatch.setattr(ctx_mod, "resolve_permission_mode", lambda kw: "full-auto")
    monkeypatch.setattr(AutomationContext, "_existing_session", _no_existing, raising=False)
    await ctx.spawn_session(origin_ref="o/r#1", account="acct", **kwargs)
    return seen


@pytest.mark.asyncio
async def test_spawn_requests_native_display_by_default(monkeypatch):
    seen = await _spawn(monkeypatch)
    assert seen["display"] == "native"
    assert "client_node" not in seen


@pytest.mark.asyncio
async def test_an_explicit_display_is_kept(monkeypatch):
    seen = await _spawn(monkeypatch, display="virtual")
    assert seen["display"] == "virtual"

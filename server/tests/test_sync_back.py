"""Tests for issue #9 fixes: sync_back wiring in SessionDriver + orchestrator.

Fix 1: SessionDriver gains on_sync_back callback invoked after each turn (in
_drain's finally) and in aclose().  A crash in on_sync_back must not break the
session or stream.

Fix 2: accounts.usage_for(name, allow_refresh=False) must call sync_back first
to pick up SDK-rotated tokens, then fetch usage with whatever's in the slot;
if the token is expired it returns None WITHOUT calling oauth.refresh_access_token
and WITHOUT marking needs-reauth.
"""
from __future__ import annotations

import asyncio
import json
import time


from server.sessions.driver import SessionDriver
from server.sessions.permissions import PermissionRegistry
from server.tests.fakes import fake_sdk as f


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _factory(msgs):
    def make(cwd, config_dir, can_use_tool, mode, resume, *, on_pre_compact=None):
        from server.tests.fakes.fake_sdk import FakeSDKClient
        return FakeSDKClient(msgs, on_can_use_tool=can_use_tool)
    return make


# ---------------------------------------------------------------------------
# Fix 1 — on_sync_back is called after each turn and on aclose
# ---------------------------------------------------------------------------

def test_on_sync_back_called_after_turn():
    """After _drain completes, on_sync_back must have been invoked exactly once."""
    msgs = [f.result(uuid="t1")]
    calls: list[str] = []

    def fake_sync_back():
        calls.append("sync")

    from server.sessions.events import Status

    async def run():
        d = SessionDriver(
            "sb1", "acct1", "/tmp/r", "/tmp/cfg",
            mode="interactive",
            sdk_factory=_factory(msgs),
            registry=PermissionRegistry(),
            identity="me",
            on_sync_back=fake_sync_back,
        )
        await d.start()
        await d.send_prompt("hello")
        # Drain until idle
        async for e in d.events():
            if isinstance(e, Status) and e.state in ("idle", "done", "complete", "needs_attention"):
                break
        await d.aclose()
        return calls

    result = asyncio.run(run())
    # At least one call from _drain's finally; one more from aclose.
    assert len(result) >= 2, f"expected >=2 sync_back calls, got {result}"


def test_on_sync_back_called_on_aclose_without_turn():
    """aclose() must invoke on_sync_back even if no turn was sent."""
    calls: list[str] = []

    def fake_sync_back():
        calls.append("sync")

    async def run():
        d = SessionDriver(
            "sb2", "acct1", "/tmp/r", "/tmp/cfg",
            mode="interactive",
            sdk_factory=_factory([]),
            registry=PermissionRegistry(),
            identity="me",
            on_sync_back=fake_sync_back,
        )
        await d.start()
        await d.aclose()
        return calls

    result = asyncio.run(run())
    assert "sync" in result, "on_sync_back should be called in aclose"


def test_on_sync_back_failure_does_not_crash_session():
    """A crash in on_sync_back must NOT propagate and must NOT break the stream."""
    msgs = [f.result(uuid="t1")]
    calls: list[str] = []

    def crashing_sync_back():
        calls.append("attempt")
        raise RuntimeError("simulated sync_back failure")

    from server.sessions.events import Status

    async def run():
        d = SessionDriver(
            "sb3", "acct1", "/tmp/r", "/tmp/cfg",
            mode="interactive",
            sdk_factory=_factory(msgs),
            registry=PermissionRegistry(),
            identity="me",
            on_sync_back=crashing_sync_back,
        )
        await d.start()
        await d.send_prompt("hello")
        seen = []
        async for e in d.events():
            seen.append(e)
            if isinstance(e, Status) and e.state in ("idle", "done", "complete", "needs_attention"):
                break
        await d.aclose()
        return seen, calls

    seen, calls = asyncio.run(run())
    # The stream still completed (we got idle)
    assert any(isinstance(e, Status) and e.state in ("idle", "done", "complete", "needs_attention") for e in seen)
    # sync_back was attempted
    assert "attempt" in calls


def test_on_sync_back_sdk_rotation_captured_in_slot(tmp_path):
    """Integration: after a turn, the SDK writes a new refresh token into the
    config dir.  on_sync_back (via accounts.sync_back) must copy it to the slot.

    We simulate the SDK rotation by writing directly to config_dir/.credentials.json
    before the turn's _drain finally block runs (i.e. the drain coroutine does it
    at receive_response time).
    """

    config_dir = tmp_path / "cfg"
    config_dir.mkdir()
    slot_path = tmp_path / "slot.cred.json"

    # Seed the slot with an initial token
    slot_path.write_text(json.dumps({
        "accessToken": "old-access",
        "refreshToken": "old-refresh",
        "expiresAt": int(time.time() * 1000) + 9_000_000,
    }))

    new_refresh = "rotated-refresh-token"

    # Fake SDK that writes a new refresh token into config_dir during receive_response
    class RotatingSDK:
        async def __aenter__(self): return self
        async def __aexit__(self, *_): return False
        async def query(self, prompt: str): pass
        async def receive_response(self):
            # Simulate SDK rotating the refresh token
            creds_path = config_dir / ".credentials.json"
            creds_path.write_text(json.dumps({
                "claudeAiOauth": {
                    "accessToken": "new-access",
                    "refreshToken": new_refresh,
                    "expiresAt": int(time.time() * 1000) + 9_000_000,
                }
            }))
            yield f.result(uuid="t1")
        async def interrupt(self): pass
        async def set_permission_mode(self, mode): pass

    def sdk_factory(cwd, cfg, can_use_tool, mode, resume, *, on_pre_compact=None):
        return RotatingSDK()

    # Minimal sync_back: reads config_dir/.credentials.json, writes claudeAiOauth to slot
    def sync_back():
        creds_path = config_dir / ".credentials.json"
        if not creds_path.exists():
            return
        full = json.loads(creds_path.read_text())
        oauth_blob = full.get("claudeAiOauth", {})
        if not oauth_blob:
            return
        # Simple write (no encryption in this unit test)
        slot_path.write_text(json.dumps(oauth_blob))

    from server.sessions.events import Status

    async def run():
        d = SessionDriver(
            "sb4", "acct1", str(config_dir), str(config_dir),
            mode="interactive",
            sdk_factory=sdk_factory,
            registry=PermissionRegistry(),
            identity="me",
            on_sync_back=sync_back,
        )
        await d.start()
        await d.send_prompt("hello")
        async for e in d.events():
            if isinstance(e, Status) and e.state in ("idle", "done", "complete", "needs_attention"):
                break
        await d.aclose()

    asyncio.run(run())

    # The slot must now contain the rotated refresh token
    slot_data = json.loads(slot_path.read_text())
    assert slot_data.get("refreshToken") == new_refresh, (
        f"slot not updated: {slot_data}"
    )


# ---------------------------------------------------------------------------
# Fix 2 — usage_for(name, allow_refresh=False) skips refresh, calls sync_back
# ---------------------------------------------------------------------------

def test_usage_for_allow_refresh_false_skips_refresh():
    """usage_for(name, allow_refresh=False) with an expired token must:
    1. Call sync_back to pick up the SDK's latest token.
    2. Return None WITHOUT calling oauth.refresh_access_token.
    3. NOT mark the account as needs-reauth.
    """
    from unittest import mock
    from tests.helpers import temp_env

    with temp_env():
        from ccswitch import accounts, oauth, paths
        from ccswitch.creds import write_json_secure

        # Seed an expired slot
        write_json_secure(paths.slot_path("alpha"), {
            "accessToken": "expired-access",
            "refreshToken": "expired-refresh",
            "expiresAt": int(time.time() * 1000) - 100_000,  # expired
        })
        write_json_secure(paths.shared_mcp_path(), {})

        sync_back_calls: list[str] = []
        refresh_calls: list[str] = []

        def fake_sync_back(name: str) -> None:
            sync_back_calls.append(name)
            # does NOT update the expiry — slot stays expired

        with mock.patch.object(accounts, "sync_back", fake_sync_back), \
             mock.patch.object(oauth, "refresh_access_token",
                               side_effect=lambda *a, **kw: refresh_calls.append("called") or AssertionError("must not refresh")):

            result = accounts.usage_for("alpha", allow_refresh=False)

        assert result is None, "expired token with allow_refresh=False should return None"
        assert "alpha" in sync_back_calls, "sync_back must be called"
        assert refresh_calls == [], "oauth.refresh_access_token must NOT be called"

        # Must not have marked needs-reauth
        from ccswitch import ledger
        with ledger.locked_ledger() as led:
            status = led.get("accounts", {}).get("alpha", {}).get("status")
        assert status != "needs-reauth", "allow_refresh=False must not mark needs-reauth"


def test_usage_for_allow_refresh_true_default_unchanged():
    """usage_for(name, allow_refresh=True) (default) behavior is unchanged:
    an expired token WILL trigger a refresh via oauth.refresh_access_token.
    """
    from unittest import mock
    from tests.helpers import temp_env
    from datetime import datetime, timedelta, timezone

    with temp_env():
        from ccswitch import accounts, oauth, paths
        from ccswitch.creds import write_json_secure
        from ccswitch.oauth import Usage

        write_json_secure(paths.slot_path("beta"), {
            "accessToken": "expired-access",
            "refreshToken": "expired-refresh",
            "expiresAt": int(time.time() * 1000) - 100_000,  # expired
        })
        write_json_secure(paths.shared_mcp_path(), {})

        refresh_calls: list[str] = []
        new_blob = {
            "accessToken": "fresh-access",
            "refreshToken": "fresh-refresh",
            "expiresAt": int(time.time() * 1000) + 9_000_000,
        }
        fake_usage = Usage(50.0, datetime.now(timezone.utc) + timedelta(hours=1),
                           60.0, datetime.now(timezone.utc) + timedelta(hours=1), False)

        with mock.patch.object(oauth, "refresh_access_token",
                               side_effect=lambda rt, blob, **kw: refresh_calls.append(rt) or new_blob), \
             mock.patch.object(oauth, "fetch_usage", return_value=fake_usage):

            result = accounts.usage_for("beta")  # allow_refresh=True (default)

        assert len(refresh_calls) >= 1, "allow_refresh=True (default) should call refresh"

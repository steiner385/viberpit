"""Tests for POST /api/dispatch/assess.

Fires two parallel Haiku calls (assess_model + refine_prompt) and returns
their results. Both are best-effort; the endpoint is tested here via:
  - Patched assess_model / refine_prompt (no real SDK calls, injected via
    unittest.mock.patch on the fully-qualified names the router imports).
  - TestClient with a valid Tailscale-User-Login identity header (conftest
    already sets CCSWITCH_DEV=1 + empty allowlist, so any identity passes).
"""
from __future__ import annotations

import contextlib
import os
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

IDENTITY_HEADERS = {"Tailscale-User-Login": "testuser@example.com"}


def _make_client() -> TestClient:
    """Build a minimal test app with TestClient (lifespan-aware)."""
    from server.sessions.orchestrator import SessionOrchestrator
    from server.tests.fakes.fake_factory import fake_sdk_factory
    from server.app import create_app

    with patch.dict(os.environ, {"CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:"}):
        orch = SessionOrchestrator(
            sdk_factory=fake_sdk_factory(),
            poll_interval=9999.0,
        )
        app = create_app(orchestrator=orch)
    return TestClient(app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# POST /api/dispatch/assess
# ---------------------------------------------------------------------------


def test_assess_both_succeed():
    """Both assess_model and refine_prompt succeed → response has both fields."""
    client = _make_client()
    with client:
        with (
            patch(
                "server.api.dispatch.assess_model",
                new=AsyncMock(return_value="sonnet"),
            ),
            patch(
                "server.api.dispatch.refine_prompt",
                new=AsyncMock(return_value="Tightened prompt text."),
            ),
        ):
            resp = client.post(
                "/api/dispatch/assess",
                json={"prompt": "Fix the login bug"},
                headers=IDENTITY_HEADERS,
            )
    assert resp.status_code == 200
    data = resp.json()
    assert data["model"] == "sonnet"
    assert data["refined_prompt"] == "Tightened prompt text."


def test_assess_model_returns_none():
    """assess_model returns None → response.model is None, refined_prompt still present."""
    client = _make_client()
    with client:
        with (
            patch(
                "server.api.dispatch.assess_model",
                new=AsyncMock(return_value=None),
            ),
            patch(
                "server.api.dispatch.refine_prompt",
                new=AsyncMock(return_value="Better prompt."),
            ),
        ):
            resp = client.post(
                "/api/dispatch/assess",
                json={"prompt": "Do something complex"},
                headers=IDENTITY_HEADERS,
            )
    assert resp.status_code == 200
    data = resp.json()
    assert data["model"] is None
    assert data["refined_prompt"] == "Better prompt."


def test_assess_refine_returns_none():
    """refine_prompt returns None → response.refined_prompt is None, model still present."""
    client = _make_client()
    with client:
        with (
            patch(
                "server.api.dispatch.assess_model",
                new=AsyncMock(return_value="haiku"),
            ),
            patch(
                "server.api.dispatch.refine_prompt",
                new=AsyncMock(return_value=None),
            ),
        ):
            resp = client.post(
                "/api/dispatch/assess",
                json={"prompt": "Bump the version number"},
                headers=IDENTITY_HEADERS,
            )
    assert resp.status_code == 200
    data = resp.json()
    assert data["model"] == "haiku"
    assert data["refined_prompt"] is None


def test_assess_empty_prompt_422():
    """Empty string prompt fails Pydantic min_length=1 → 422 Unprocessable Entity."""
    client = _make_client()
    with client:
        resp = client.post(
            "/api/dispatch/assess",
            json={"prompt": ""},
            headers=IDENTITY_HEADERS,
        )
    assert resp.status_code == 422


def test_assess_whitespace_only_prompt_422():
    """A whitespace-only prompt is non-empty but meets min_length=1, so Pydantic
    accepts it (422 is only for truly empty string). The handlers still get called."""
    # Note: Pydantic min_length counts characters, so " " (1 char) passes.
    # This confirms whitespace passes the validation layer (endpoint-level fail-open
    # handles it inside the called functions, which are patched here to None).
    client = _make_client()
    with client:
        with (
            patch(
                "server.api.dispatch.assess_model",
                new=AsyncMock(return_value=None),
            ),
            patch(
                "server.api.dispatch.refine_prompt",
                new=AsyncMock(return_value=None),
            ),
        ):
            resp = client.post(
                "/api/dispatch/assess",
                json={"prompt": " "},
                headers=IDENTITY_HEADERS,
            )
    assert resp.status_code == 200
    data = resp.json()
    assert data["model"] is None
    assert data["refined_prompt"] is None


def test_assess_no_identity_401():
    """No identity header → 401 Unauthorized (auth middleware rejects).

    Patches verify_identity where the middleware imports it from, simulating
    a request with no recognizable Tailscale identity and bypassing any
    dev-machine-specific fallback (CCSWITCH_ALLOW_UNAUTHENTICATED, tailscale
    whois, etc.) that would mask the 401 in local environments.
    """
    client = _make_client()
    with client:
        # Patch the name in the middleware's own namespace (it imported it with
        # `from server.auth.identity import verify_identity`, so patching the
        # identity module's attribute alone wouldn't reach it).
        with patch("server.auth.middleware.verify_identity", return_value=None):
            resp = client.post(
                "/api/dispatch/assess",
                json={"prompt": "Fix something"},
            )
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# _pick_helper_config_dir unit tests
# ---------------------------------------------------------------------------


def _fake_locked_ledger(status_map: dict, enabled_map: dict | None = None):
    """Return a context-manager factory that yields a fake ledger dict."""
    led = {"accounts": {name: {"status": s} for name, s in status_map.items()}}
    for name, enabled in (enabled_map or {}).items():
        led["accounts"].setdefault(name, {})["enabled"] = enabled

    @contextlib.contextmanager
    def _cm():
        yield led

    return _cm


def test_pick_helper_config_dir_returns_first_ok_anthropic():
    """First live Anthropic account → its config_dir string is returned."""
    from server.api.dispatch import _pick_helper_config_dir

    with (
        patch("ccswitch.accounts.all_accounts", return_value=[("alice", "anthropic")]),
        patch(
            "ccswitch.ledger.locked_ledger",
            new=_fake_locked_ledger({"alice": "ok"}),
        ),
        patch("ccswitch.paths.account_cfg_dir", return_value=type("P", (), {"__str__": lambda self: "/fake/cfg/alice"})()),
    ):
        result = _pick_helper_config_dir()

    assert result == "/fake/cfg/alice"


def test_pick_helper_config_dir_skips_needs_reauth():
    """Accounts marked needs-reauth are skipped; returns None when none remain."""
    from server.api.dispatch import _pick_helper_config_dir

    with (
        patch(
            "ccswitch.accounts.all_accounts",
            return_value=[("alice", "anthropic"), ("bob", "anthropic")],
        ),
        patch(
            "ccswitch.ledger.locked_ledger",
            new=_fake_locked_ledger({"alice": "needs-reauth", "bob": "needs-reauth"}),
        ),
    ):
        result = _pick_helper_config_dir()

    assert result is None


def test_pick_helper_config_dir_skips_copilot():
    """Copilot accounts are not eligible; returns None when only copilot exists."""
    from server.api.dispatch import _pick_helper_config_dir

    with (
        patch(
            "ccswitch.accounts.all_accounts",
            return_value=[("copilot-acct", "copilot")],
        ),
        patch(
            "ccswitch.ledger.locked_ledger",
            new=_fake_locked_ledger({}),
        ),
    ):
        result = _pick_helper_config_dir()

    assert result is None


def test_pick_helper_config_dir_skips_disabled():
    """Disabled accounts are skipped; first enabled account is returned instead."""
    from server.api.dispatch import _pick_helper_config_dir

    with (
        patch(
            "ccswitch.accounts.all_accounts",
            return_value=[("alice", "anthropic"), ("bob", "anthropic")],
        ),
        patch(
            "ccswitch.ledger.locked_ledger",
            new=_fake_locked_ledger(
                {"alice": "ok", "bob": "ok"},
                enabled_map={"alice": False},
            ),
        ),
        patch("ccswitch.paths.account_cfg_dir", return_value=type("P", (), {"__str__": lambda self: "/fake/cfg/bob"})()),
    ):
        result = _pick_helper_config_dir()

    assert result == "/fake/cfg/bob"


def test_pick_helper_config_dir_fail_open_on_exception():
    """Any exception in the helper (e.g. import failure) returns None gracefully."""
    from server.api.dispatch import _pick_helper_config_dir

    with patch("ccswitch.accounts.all_accounts", side_effect=RuntimeError("boom")):
        result = _pick_helper_config_dir()

    assert result is None

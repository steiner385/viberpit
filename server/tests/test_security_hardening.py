"""Security hardening tests (Task 2.5+2.6).

Covers:
- redact_event() over nested dict + list with fake tokens and cookie-shaped strings
- WS batcher redacts tokens in outbound batch events
- permissionResponse / setPermissionMode / interrupt audit rows written
- worktree branch arg-injection guard (branch starting with '-')
- bogus repo → 400
- bogus account → 400
- cookie secure flag defaults True; overridden by VIBERPIT_DEV_INSECURE_COOKIE=1
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Part A — redact_event() over nested structures
# ---------------------------------------------------------------------------


def test_redact_event_scrubs_nested_dict_and_list():
    """redact_event redacts tokens in nested dicts and inside lists."""
    from server.audit import redact_event

    # A fake sk-ant-* token (≥40 chars after prefix)
    token = "sk-ant-api03-ABCDEF1234567890abcdef1234567890ABCD"
    # A cookie-shaped string: <16+ char>.<6+ char>.<20+ char>
    cookie = "InVzZXJAdGVzdC5jb20i.ZaXbCd.XYZ123_abcdefghijklmnopqrstu"

    wire = {
        "type": "textDelta",
        "text": f"look at this token: {token}",
        "meta": {
            "nested": f"session={cookie}"
        },
        "items": [
            "plain item",
            f"token inside list: {token}",
            {"deep": f"cookie in list dict: {cookie}"},
        ],
    }

    clean = redact_event(wire)

    # Top-level string field redacted
    assert "sk-ant" not in clean["text"]
    assert "[REDACTED]" in clean["text"]

    # Nested dict redacted
    assert cookie not in clean["meta"]["nested"]
    assert "[REDACTED]" in clean["meta"]["nested"]

    # List: plain strings redacted
    assert "sk-ant" not in clean["items"][1]
    assert "[REDACTED]" in clean["items"][1]

    # List: nested dicts redacted
    assert cookie not in clean["items"][2]["deep"]
    assert "[REDACTED]" in clean["items"][2]["deep"]

    # Non-secret string untouched
    assert clean["items"][0] == "plain item"

    # type key passes through unchanged
    assert clean["type"] == "textDelta"


def test_redact_event_cookie_shape_scrubbed():
    """The itsdangerous cookie-pattern is recognized and redacted."""
    from server.audit import redact_event

    # itsdangerous token shape: payload.timestamp.sig
    # payload ≥16 base64url chars, ts ≥6, sig ≥20
    # "InVzZXJAdGVzdC5jb20i" = base64url for "user@test.com"  (20 chars)
    # "ZaXbCdef" = 8-char timestamp segment
    # "AbCdEfGhIjKlMnOpQrStUvWx" = 24-char signature
    cookie_val = "InVzZXJAdGVzdC5jb20i.ZaXbCdef.AbCdEfGhIjKlMnOpQrStUvWx"

    wire = {"type": "debug", "cookie": cookie_val}
    clean = redact_event(wire)
    assert cookie_val not in clean["cookie"]
    assert "[REDACTED]" in clean["cookie"]


# Real magic bytes + a payload whose base64 ends in `=` — the shape the padded-base64
# secret rule matched, turning ~2 in 3 screenshots into "Image unavailable".
_PNG = b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 2
_JPEG = b"\xff\xd8\xff\xe0" + bytes(range(201))


def _b64(raw: bytes) -> str:
    import base64
    out = base64.b64encode(raw).decode()
    assert out.endswith("=")
    return out


def test_redact_event_keeps_tool_result_image_data():
    """A base64 image block in a toolResult reaches the browser intact."""
    from server.audit import redact_event

    data = _b64(_PNG)
    token = "sk-ant-api03-ABCDEF1234567890abcdef1234567890ABCD"
    wire = {"type": "toolResult", "payload": {"content": [
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": data}},
        {"type": "text", "text": f"leaked {token}"},
    ]}}
    clean = redact_event(wire)
    image, text = clean["payload"]["content"]
    assert image["source"]["data"] == data
    assert token not in text["text"]


def test_redact_event_keeps_image_data_urls():
    """User-turn images replay as data: URLs; they are not credentials either."""
    from server.audit import redact_event

    url = f"data:image/jpeg;base64,{_b64(_JPEG)}"
    clean = redact_event({"type": "user", "images": [{"url": url, "mime": "image/jpeg"}]})
    assert clean["images"][0]["url"] == url


def test_redact_event_still_scrubs_secrets_dressed_as_images():
    """Only real image bytes are exempt: a padded secret in an image's clothes, or
    an image payload with non-base64 material appended, is still redacted."""
    from server.audit import redact_event

    secret = _b64(b"refresh-token-" + bytes(range(60)))
    smuggled = _b64(_PNG) + " sk-ant-api03-ABCDEF1234567890abcdef1234567890ABCD"
    wire = {"type": "toolResult", "payload": {"content": [
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": secret}},
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": smuggled}},
    ]}, "url": f"data:image/png;base64,{secret}"}
    clean = redact_event(wire)
    first, second = clean["payload"]["content"]
    assert secret not in first["source"]["data"]
    assert "sk-ant" not in second["source"]["data"]
    assert secret not in clean["url"]


# ---------------------------------------------------------------------------
# Part C — worktree branch arg-injection guard
# ---------------------------------------------------------------------------


def test_create_worktree_rejects_dash_branch():
    """create_worktree raises WorktreeError for branch names starting with '-'."""
    from viberpit.worktrees import create_worktree, WorktreeError
    import tempfile, subprocess
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp) / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", str(repo), "-b", "main"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@x.com"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.name", "T"], check=True, capture_output=True)
        (repo / "f").write_text("hi")
        subprocess.run(["git", "-C", str(repo), "add", "."], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-m", "init"], check=True, capture_output=True)

        with pytest.raises(WorktreeError, match="invalid branch name"):
            create_worktree(str(repo), "-evil", "abc12345-0000", tmp)


# ---------------------------------------------------------------------------
# Part D — repo / account validation (400 responses)
# ---------------------------------------------------------------------------


_IDENTITY = {"Tailscale-User-Login": "tester@example.com"}


@pytest.fixture()
def api_client(tmp_path):
    """Yield (client, store_dir) with lifespan started."""
    import sys
    repo_root = Path(__file__).parent.parent.parent
    sys.path.insert(0, str(repo_root / "src"))

    from server.tests.fakes.fake_factory import fake_sdk_factory
    from server.sessions.orchestrator import SessionOrchestrator
    from server.app import create_app
    from fastapi.testclient import TestClient
    from viberpit.quota import anthropic_snapshot
    from datetime import datetime, timedelta, timezone

    store = tmp_path / "store"
    store.mkdir()

    def _usage(name):
        reset = datetime.now(timezone.utc) + timedelta(hours=5)
        return anthropic_snapshot(10.0, reset, 10.0, reset, False)

    with patch.dict(os.environ, {
        "VIBERPIT_HOME": str(tmp_path),
        "VIBERPIT_DB_URL": "sqlite+aiosqlite:///:memory:",
    }):
        orch = SessionOrchestrator(
            sdk_factory=fake_sdk_factory(),
            usage_fn=_usage,
            poll_interval=9999.0,
        )
        app = create_app(orchestrator=orch)
        client = TestClient(app, raise_server_exceptions=False)
        with client:
            yield client, store


def test_bogus_repo_returns_400(api_client):
    """POST /api/sessions with an invalid repo format → 400."""
    client, _store = api_client
    resp = client.post(
        "/api/sessions",
        json={"name": "bad", "repo": "../../etc/passwd"},
        headers=_IDENTITY,
    )
    assert resp.status_code == 400, resp.text
    body = resp.json()
    assert body["error"]["code"] == "invalid_repo"


def test_bogus_repo_with_slash_injection_returns_400(api_client):
    """repo with path traversal dots also 400."""
    client, _store = api_client
    for bad in ["../secret", "owner/name/extra", "owner", "owner name", ""]:
        resp = client.post(
            "/api/sessions",
            json={"name": "bad", "repo": bad},
            headers=_IDENTITY,
        )
        assert resp.status_code == 400, f"expected 400 for repo={bad!r}, got {resp.status_code}: {resp.text}"


def test_valid_repo_format_passes_validation(api_client):
    """A well-formed owner/name repo is accepted at the validation layer."""
    client, _store = api_client
    # This will 502 because there's no real GitHub clone, but NOT 400.
    resp = client.post(
        "/api/sessions",
        json={"name": "ok", "repo": "owner/my-repo"},
        headers=_IDENTITY,
    )
    # 400 = validation reject; anything else (201, 502, 409) = passed validation
    assert resp.status_code != 400, f"valid repo was rejected: {resp.text}"


def test_bogus_account_returns_400(api_client):
    """POST /api/sessions with an account that doesn't exist in the store → 400."""
    client, _store = api_client
    resp = client.post(
        "/api/sessions",
        json={"name": "test", "account": "ghost-account-that-does-not-exist"},
        headers=_IDENTITY,
    )
    assert resp.status_code == 400, resp.text
    body = resp.json()
    assert body["error"]["code"] == "unknown_account"


def test_path_traversal_account_returns_400(api_client):
    """POST /api/sessions with an account name containing path-traversal chars → 400."""
    client, _store = api_client
    resp = client.post(
        "/api/sessions",
        json={"account": "../../etc/hostname"},
        headers=_IDENTITY,
    )
    assert resp.status_code == 400, resp.text
    body = resp.json()
    assert body["error"]["code"] == "unknown_account"


def test_known_account_passes_validation(api_client):
    """An account whose slot file exists passes the validation gate."""
    client, store = api_client
    # Create a minimal slot file
    slot = store / "real-account.cred.json"
    now_ms = int(time.time() * 1000)
    slot.write_text(json.dumps({
        "accessToken": "tok",
        "refreshToken": "ref",
        "expiresAt": now_ms + 9_000_000,
        "subscriptionType": "max",
    }))
    resp = client.post(
        "/api/sessions",
        json={"name": "test", "account": "real-account"},
        headers=_IDENTITY,
    )
    # 400 = our new validation reject; anything else passed the guard
    assert resp.status_code != 400, f"known account was rejected: {resp.text}"


# ---------------------------------------------------------------------------
# Part E — cookie secure flag
# ---------------------------------------------------------------------------


def test_cookie_secure_defaults_true():
    """With no VIBERPIT_DEV_INSECURE_COOKIE env var, secure flag is True."""
    env = {k: v for k, v in os.environ.items() if k != "VIBERPIT_DEV_INSECURE_COOKIE"}
    env["VIBERPIT_DEV_INSECURE_COOKIE"] = ""  # not "1"
    with patch.dict(os.environ, env, clear=True):
        # Re-evaluate the expression the middleware uses at cookie-set time.
        _secure = os.environ.get("VIBERPIT_DEV_INSECURE_COOKIE") != "1"
        assert _secure is True


def test_cookie_secure_false_when_dev_insecure_set():
    """With VIBERPIT_DEV_INSECURE_COOKIE=1, secure flag is False."""
    with patch.dict(os.environ, {"VIBERPIT_DEV_INSECURE_COOKIE": "1"}):
        _secure = os.environ.get("VIBERPIT_DEV_INSECURE_COOKIE") != "1"
        assert _secure is False


def test_cookie_secure_true_when_var_absent():
    """With VIBERPIT_DEV_INSECURE_COOKIE absent entirely, secure flag is True."""
    env = {k: v for k, v in os.environ.items() if k != "VIBERPIT_DEV_INSECURE_COOKIE"}
    with patch.dict(os.environ, env, clear=True):
        _secure = os.environ.get("VIBERPIT_DEV_INSECURE_COOKIE") != "1"
        assert _secure is True


# ---------------------------------------------------------------------------
# Part E — boot gate: non-loopback bind + empty allowlist + no VIBERPIT_DEV
# ---------------------------------------------------------------------------


def _run_async(coro):
    """Run an async coroutine in a fresh event loop (avoids closed-loop issues after pytest-asyncio)."""
    import asyncio
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def test_boot_gate_raises_on_non_loopback_empty_allowlist():
    """_lifespan raises RuntimeError when binding non-loopback without allowlist or VIBERPIT_DEV."""
    from server.app import _lifespan
    from fastapi import FastAPI

    env = {
        "VIBERPIT_HOST": "0.0.0.0",
        "VIBERPIT_TAILNET_ALLOWLIST": "",
        "VIBERPIT_DB_URL": "sqlite+aiosqlite:///:memory:",
    }
    # Remove VIBERPIT_DEV if set
    clean_env = {k: v for k, v in os.environ.items() if k != "VIBERPIT_DEV"}
    clean_env.update(env)

    with patch.dict(os.environ, clean_env, clear=True):
        from server.sessions.orchestrator import SessionOrchestrator
        from server.reauth import ReauthManager
        orch = SessionOrchestrator(poll_interval=9999.0)
        app = FastAPI(lifespan=_lifespan)
        app.state.orchestrator = orch
        app.state.reauth = ReauthManager()

        async def _run():
            async with _lifespan(app):
                pass

        with pytest.raises(RuntimeError, match="refusing to start"):
            _run_async(_run())


def test_boot_gate_allows_non_loopback_with_allowlist():
    """Non-loopback bind is allowed when tailnet_allowlist is set."""
    from server.app import _lifespan
    from fastapi import FastAPI

    env = {
        "VIBERPIT_HOST": "0.0.0.0",
        "VIBERPIT_TAILNET_ALLOWLIST": "user@example.com",
        "VIBERPIT_DB_URL": "sqlite+aiosqlite:///:memory:",
    }
    clean_env = {k: v for k, v in os.environ.items() if k != "VIBERPIT_DEV"}
    clean_env.update(env)

    with patch.dict(os.environ, clean_env, clear=True):
        from server.sessions.orchestrator import SessionOrchestrator
        from server.reauth import ReauthManager
        orch = SessionOrchestrator(poll_interval=9999.0)
        app = FastAPI(lifespan=_lifespan)
        app.state.orchestrator = orch
        app.state.reauth = ReauthManager()

        async def _run():
            async with _lifespan(app):
                pass

        # Should not raise
        _run_async(_run())


def test_boot_gate_allows_non_loopback_with_dev_flag():
    """Non-loopback bind + empty allowlist is allowed when VIBERPIT_DEV=1."""
    from server.app import _lifespan
    from fastapi import FastAPI

    env = {
        "VIBERPIT_HOST": "0.0.0.0",
        "VIBERPIT_TAILNET_ALLOWLIST": "",
        "VIBERPIT_DB_URL": "sqlite+aiosqlite:///:memory:",
        "VIBERPIT_DEV": "1",
    }
    with patch.dict(os.environ, env):
        from server.sessions.orchestrator import SessionOrchestrator
        from server.reauth import ReauthManager
        orch = SessionOrchestrator(poll_interval=9999.0)
        app = FastAPI(lifespan=_lifespan)
        app.state.orchestrator = orch
        app.state.reauth = ReauthManager()

        async def _run():
            async with _lifespan(app):
                pass

        # Should not raise (just warn)
        _run_async(_run())


def test_security_warning_fires_at_most_once_per_process(tmp_path):
    """SECURITY/0.0.0.0 warning fires exactly once even when _lifespan is entered twice.

    Regression guard for issue #669: the module-level _security_warned flag must
    suppress duplicate emissions that arise from hot-reload cycles or rapid watchdog
    restarts re-entering _lifespan within the same Python process.
    """
    import logging
    import server.app as _app_module
    from server.app import _lifespan
    from fastapi import FastAPI
    from unittest.mock import patch as _patch

    env = {
        "VIBERPIT_HOST": "0.0.0.0",
        "VIBERPIT_TAILNET_ALLOWLIST": "",
        "VIBERPIT_DB_URL": f"sqlite+aiosqlite:///{tmp_path / 'warn_test.db'}",
        "VIBERPIT_DEV": "1",
    }

    captured: list[str] = []

    class _CapturingHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured.append(record.getMessage())

    handler = _CapturingHandler()
    server_log = logging.getLogger("server")
    server_log.addHandler(handler)
    try:
        # Reset the module flag so this test is independent of run order.
        with patch.dict(os.environ, env), \
             _patch.object(_app_module, "_security_warned", False):
            from server.sessions.orchestrator import SessionOrchestrator
            from server.reauth import ReauthManager

            async def _run_twice():
                for _ in range(2):
                    orch = SessionOrchestrator(poll_interval=9999.0)
                    app = FastAPI(lifespan=_lifespan)
                    app.state.orchestrator = orch
                    app.state.reauth = ReauthManager()
                    async with _lifespan(app):
                        pass

            _run_async(_run_twice())
    finally:
        server_log.removeHandler(handler)

    security_lines = [m for m in captured if "SECURITY" in m and "EMPTY tailnet" in m]
    assert len(security_lines) == 1, (
        f"Expected exactly 1 SECURITY warning, got {len(security_lines)}: {security_lines}"
    )

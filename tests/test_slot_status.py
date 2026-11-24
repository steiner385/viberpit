"""accounts.slot_status — is the account's browser-login slot usable for Remote Control?

Remote Control needs the slot's full-scope claude.ai login (``user:sessions:claude_code``)
and a live refresh grant the primary can keep rotating. Inference keeps running on the
setup-token, and ``_setup_token_usage_fallback`` deliberately keeps such an account
``ok`` when the slot's grant dies — so a dead slot was invisible. ``slot_status`` reads
metadata only (never a token value) and reports ``ok`` | ``login_needed`` | ``missing``.
"""
import json
import time
from unittest import mock

import pytest

from tests.helpers import temp_env

FULL_SCOPES = ["user:file_upload", "user:inference", "user:mcp_servers", "user:profile",
               "user:sessions:claude_code"]


def _now_ms() -> int:
    return int(time.time() * 1000)


def _write_slot(name: str, **overrides) -> None:
    from viberpit import paths
    from viberpit.creds import write_json_secure
    blob = {
        "accessToken": f"{name}-A",
        "refreshToken": f"{name}-R",
        "expiresAt": _now_ms() + 3_600_000,
        "refreshTokenExpiresAt": _now_ms() + 30 * 86_400_000,
        "scopes": list(FULL_SCOPES),
        "subscriptionType": "max",
    }
    blob.update(overrides)
    for k in [k for k, v in blob.items() if v is None]:
        blob.pop(k)
    write_json_secure(paths.slot_path(name), blob)


def test_missing_slot_is_missing():
    with temp_env():
        from viberpit import accounts
        assert accounts.slot_status("nobody") == "missing"


def test_empty_slot_is_missing():
    with temp_env():
        from viberpit import accounts, paths
        paths.store_dir().mkdir(parents=True, exist_ok=True)
        paths.slot_path("empty").write_text("{}")
        assert accounts.slot_status("empty") == "missing"


def test_unreadable_slot_is_missing():
    with temp_env():
        from viberpit import accounts, paths
        paths.store_dir().mkdir(parents=True, exist_ok=True)
        paths.slot_path("torn").write_text("{not json")
        assert accounts.slot_status("torn") == "missing"


def test_healthy_full_scope_slot_is_ok():
    with temp_env():
        from viberpit import accounts
        _write_slot("good")
        assert accounts.slot_status("good") == "ok"


def test_expired_access_token_with_live_refresh_grant_is_ok():
    """The authority refreshes an expired ACCESS token on demand — only the grant matters."""
    with temp_env():
        from viberpit import accounts
        _write_slot("stale", expiresAt=_now_ms() - 60_000)
        assert accounts.slot_status("stale") == "ok"


def test_expired_refresh_grant_is_login_needed():
    with temp_env():
        from viberpit import accounts
        _write_slot("lapsed", refreshTokenExpiresAt=_now_ms() - 86_400_000)
        assert accounts.slot_status("lapsed") == "login_needed"


def test_slot_without_refresh_token_is_login_needed():
    with temp_env():
        from viberpit import accounts
        _write_slot("norefresh", refreshToken=None)
        assert accounts.slot_status("norefresh") == "login_needed"


def test_slot_missing_sessions_scope_is_login_needed():
    with temp_env():
        from viberpit import accounts
        _write_slot("narrow", scopes=["user:inference", "user:profile"])
        assert accounts.slot_status("narrow") == "login_needed"


def test_slot_with_no_scopes_recorded_is_login_needed():
    with temp_env():
        from viberpit import accounts
        _write_slot("noscopes", scopes=None)
        assert accounts.slot_status("noscopes") == "login_needed"


def test_no_refresh_expiry_recorded_is_ok():
    """Older slots have no refreshTokenExpiresAt — absence is not proof of death."""
    with temp_env():
        from viberpit import accounts
        _write_slot("legacy", refreshTokenExpiresAt=None)
        assert accounts.slot_status("legacy") == "ok"


def test_rejected_refresh_marks_login_needed_even_with_setup_token():
    """A dead grant must surface even though the setup-token keeps usage_for 'ok'."""
    with temp_env():
        from viberpit import accounts, oauth
        _write_slot("dead", expiresAt=_now_ms() - 60_000)
        with mock.patch.object(oauth, "refresh_access_token",
                               side_effect=oauth.AuthError("invalid_grant", status=400)):
            with pytest.raises(oauth.AuthError):
                accounts.ensure_valid_token("dead")
        assert accounts.slot_status("dead") == "login_needed"


def test_successful_refresh_clears_a_recorded_rejection():
    with temp_env():
        from viberpit import accounts, oauth
        _write_slot("flaky", expiresAt=_now_ms() - 60_000)
        with mock.patch.object(oauth, "refresh_access_token",
                               side_effect=oauth.AuthError("invalid_grant", status=400)):
            with pytest.raises(oauth.AuthError):
                accounts.ensure_valid_token("flaky")
        assert accounts.slot_status("flaky") == "login_needed"

        def _ok(rt, prior, **kw):
            return {**prior, "accessToken": "new-A", "expiresAt": _now_ms() + 3_600_000}

        with mock.patch.object(oauth, "refresh_access_token", side_effect=_ok):
            accounts.ensure_valid_token("flaky")
        assert accounts.slot_status("flaky") == "ok"


def test_transient_refresh_failure_does_not_mark_login_needed():
    with temp_env():
        from viberpit import accounts, oauth
        _write_slot("throttled", expiresAt=_now_ms() - 60_000)
        with mock.patch.object(oauth, "refresh_access_token",
                               side_effect=oauth.TransientError("429")):
            with pytest.raises(oauth.TransientError):
                accounts.ensure_valid_token("throttled")
        assert accounts.slot_status("throttled") == "ok"


def _reject(name: str) -> None:
    from viberpit import accounts, oauth
    _write_slot(name, expiresAt=_now_ms() - 60_000)
    with mock.patch.object(oauth, "refresh_access_token",
                           side_effect=oauth.AuthError("invalid_grant", status=400)):
        with pytest.raises(oauth.AuthError):
            accounts.ensure_valid_token(name)


def test_a_setup_token_reauth_does_not_clear_a_recorded_rejection():
    """``mark_reauthenticated`` also runs after a setup-token re-auth, which never
    touches the slot: a dead slot must stay ``login_needed``."""
    with temp_env():
        from viberpit import accounts
        _reject("relogin")
        accounts.mark_reauthenticated("relogin")
        assert accounts.slot_status("relogin") == "login_needed"


def test_a_captured_browser_login_clears_a_recorded_rejection():
    with temp_env():
        from viberpit import accounts
        _reject("relogin")
        _write_slot("relogin")  # the fresh browser login rewrote the slot...
        accounts.clear_slot_refresh_rejection("relogin")   # ...as capture_login does
        assert accounts.slot_status("relogin") == "ok"


def test_list_accounts_carries_slot_status_for_claude_accounts_only():
    with temp_env():
        from viberpit import accounts
        _write_slot("claude1")
        _write_slot("claude2", refreshTokenExpiresAt=_now_ms() - 1)
        by_name = {a["name"]: a for a in accounts.list_accounts()}
        assert by_name["claude1"]["remoteControlLogin"] == "ok"
        assert by_name["claude2"]["remoteControlLogin"] == "login_needed"


def test_slot_status_never_returns_token_material():
    with temp_env():
        from viberpit import accounts
        _write_slot("secret")
        out = json.dumps({"s": accounts.slot_status("secret")})
        assert "secret-A" not in out and "secret-R" not in out

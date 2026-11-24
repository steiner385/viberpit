"""auth_login.complete_code_login — the code-paste claude.ai sign-in of an EXISTING account.

Remote Control's "Sign in to claude.ai": the operator approves in their own browser and
pastes the code back; ``setup_token_oauth.exchange_login`` returns the token response,
and this installs it into the slot. It must store exactly what the add-flow capture
stores (``install_claude_login``) and refuse — writing nothing — a login as anyone else.
"""
from __future__ import annotations

import base64
import json
from unittest import mock

import pytest

from viberpit import auth_login, crypto, ledger, oauth, paths
from viberpit.creds import read_credentials

FULL = "user:profile user:inference user:sessions:claude_code user:mcp_servers user:file_upload"
PROFILE = {
    "account": {"uuid": "uuid-A", "email": "a@example.com", "display_name": "A"},
    "organization": {"uuid": "org-A", "name": "A's org", "organization_type": "claude_max",
                     "rate_limit_tier": "default_claude_max_20x"},
}


def _resp(**overrides) -> dict:
    resp = {
        "access_token": "NEW-AT", "refresh_token": "NEW-RT", "expires_in": 28800,
        "refresh_token_expires_in": 2_592_000, "scope": FULL,
        "account": {"uuid": "uuid-A", "email_address": "a@example.com"},
        "organization": {"uuid": "org-A", "name": "A's org"},
    }
    resp.update(overrides)
    return {k: v for k, v in resp.items() if v is not None}


@pytest.fixture
def existing(tmp_path, monkeypatch):
    """Account ``acct`` with a stored identity, an old slot and an encryption key."""
    monkeypatch.setenv("VIBERPIT_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "dotclaude"))
    monkeypatch.setenv("VIBERPIT_MASTER_KEY", base64.b64encode(b"k" * 32).decode())

    def make(*, uuid="uuid-A", email="a@example.com"):
        paths.store_dir().mkdir(parents=True, exist_ok=True)
        acct = {k: v for k, v in (("accountUuid", uuid), ("emailAddress", email)) if v}
        acct["organizationRole"] = "admin"          # a stored key the login never names
        paths.store_claude_json_path("acct").write_text(
            json.dumps({"oauthAccount": acct, "userID": "keep-me"}))
        paths.slot_path("acct").write_text(
            json.dumps({"accessToken": "OLD-AT", "subscriptionType": "pro"}))
    make()
    return make


def _profile(value=PROFILE):
    if isinstance(value, Exception):
        return mock.patch.object(oauth, "fetch_profile", side_effect=value)
    return mock.patch.object(oauth, "fetch_profile", return_value=value)


def _untouched():
    assert json.loads(paths.slot_path("acct").read_text()) == {
        "accessToken": "OLD-AT", "subscriptionType": "pro"}
    assert not paths.account_cfg_dir("acct").exists()
    stored = json.loads(paths.store_claude_json_path("acct").read_text())
    assert stored["userID"] == "keep-me"


def test_the_same_account_is_installed_in_the_clis_shape(existing):
    with _profile():
        auth_login.complete_code_login("acct", _resp())
    slot = read_credentials(paths.slot_path("acct"))
    assert slot["accessToken"] == "NEW-AT" and slot["refreshToken"] == "NEW-RT"
    assert slot["scopes"] == FULL.split()
    assert slot["subscriptionType"] == "max"
    assert slot["rateLimitTier"] == "default_claude_max_20x"
    assert slot["expiresAt"] > 0 and slot["refreshTokenExpiresAt"] > slot["expiresAt"]
    # The refresh token is encrypted at rest through the existing creds path.
    on_disk = json.loads(paths.slot_path("acct").read_text())
    assert crypto.is_encrypted(on_disk["refreshToken"])
    assert "NEW-RT" not in paths.slot_path("acct").read_text()
    # oauthAccount merged; the file's other keys kept.
    stored = json.loads(paths.store_claude_json_path("acct").read_text())
    assert stored["userID"] == "keep-me"
    assert stored["oauthAccount"] == {
        "accountUuid": "uuid-A", "emailAddress": "a@example.com", "organizationRole": "admin",
        "organizationUuid": "org-A", "organizationName": "A's org", "displayName": "A"}
    assert paths.account_cfg_dir("acct").exists()                     # composed
    assert ledger.read_ledger()["accounts"]["acct"]["status"] == "ok"  # mark_reauthenticated


def test_it_clears_a_recorded_slot_refresh_rejection(existing):
    with ledger.locked_ledger() as led:
        led["accounts"].setdefault("acct", {})["slotRefreshRejected"] = True
    with _profile():
        auth_login.complete_code_login("acct", _resp())
    assert "slotRefreshRejected" not in ledger.read_ledger()["accounts"]["acct"]


def test_another_account_is_refused_and_nothing_is_written(existing):
    other = _resp(account={"uuid": "uuid-B", "email_address": "b@example.com"})
    with _profile({"account": {"uuid": "uuid-B", "email": "b@example.com"}}):
        with pytest.raises(auth_login.WrongAccountLogin) as ei:
            auth_login.complete_code_login("acct", other)
    assert ei.value.code == "browser_login_wrong_account"
    assert str(ei.value) == ("You signed in as b@example.com, expected a@example.com. "
                             "Nothing was saved.")
    assert "NEW-AT" not in str(ei.value)
    _untouched()


def test_no_stored_identity_fails_closed(existing):
    existing(uuid=None, email=None)
    with _profile():
        with pytest.raises(auth_login.WrongAccountLogin):
            auth_login.complete_code_login("acct", _resp())
    _untouched()


def test_the_email_decides_case_insensitively_when_the_stored_side_has_no_uuid(existing):
    existing(uuid=None, email="A@Example.COM")
    with _profile():
        auth_login.complete_code_login("acct", _resp())
    assert read_credentials(paths.slot_path("acct"))["accessToken"] == "NEW-AT"


def test_without_account_in_the_token_response_the_profile_names_the_identity(existing):
    with _profile() as fp:
        auth_login.complete_code_login("acct", _resp(account=None, organization=None))
    fp.assert_called_once()
    assert fp.call_args.args[0] == "NEW-AT"
    stored = json.loads(paths.store_claude_json_path("acct").read_text())
    assert stored["oauthAccount"]["organizationUuid"] == "org-A"


def test_the_profile_alone_can_refuse_another_account(existing):
    with _profile({"account": {"uuid": "uuid-B", "email": "b@example.com"},
                   "organization": {"uuid": "org-B"}}):
        with pytest.raises(auth_login.WrongAccountLogin):
            auth_login.complete_code_login("acct", _resp(account=None, organization=None))
    _untouched()


def test_no_identity_anywhere_saves_nothing(existing):
    with _profile(oauth.TransientError("503")):
        with pytest.raises(auth_login.LoginIdentityUnavailable):
            auth_login.complete_code_login("acct", _resp(account=None, organization=None))
    _untouched()


def test_an_unreachable_profile_keeps_the_slots_plan(existing):
    """The token response proves the identity; the plan then falls back to the slot's."""
    with _profile(oauth.TransientError("503")):
        auth_login.complete_code_login("acct", _resp())
    slot = read_credentials(paths.slot_path("acct"))
    assert slot["accessToken"] == "NEW-AT"
    assert slot["subscriptionType"] == "pro"
    assert "rateLimitTier" not in slot

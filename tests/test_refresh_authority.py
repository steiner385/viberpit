"""A fleet WORKER never calls the Anthropic token endpoint (#2625).

``_refresh_guard``'s flock is taken by the kernel of the host that opens the lock
file. On a runner whose account store is an sshfs mount of the primary's it never
reaches the primary — verified on the live dobby/rocky mount in both directions —
so two hosts refreshing one single-use token are unserialised, and the loser's
``invalid_grant`` falsely flips the account to needs-reauth. Until the credential
broker lands, the primary is the only refresher. ``accounts.is_refresh_authority``
keys that off ``viberpit.fleet_role`` — the ONE reader of ``VIBERPIT_DAEMON_ROLE``
— and every refresh path honours it:

- ``ensure_valid_token`` raises ``RefreshDeferred`` (not an AuthError) instead of
  refreshing;
- ``acquire_valid_token`` still recovers the one rotation a worker may pick up —
  the token its own live ``claude`` process wrote to the cfg dir;
- ``usage_for`` takes its read-only path and never flips needs-reauth.
"""
import re
import time
from pathlib import Path
from unittest import mock

import pytest

from tests.helpers import temp_env, usage
from viberpit import accounts, fleet_role, ledger, oauth, paths
from viberpit.creds import compose_credentials, read_json, write_json_secure

_REPO = Path(__file__).resolve().parents[1]


def _token(expires_delta_ms: int, access: str = "A", refresh: str = "R") -> dict:
    return {
        "accessToken": access,
        "refreshToken": refresh,
        "expiresAt": int(time.time() * 1000) + expires_delta_ms,
        "subscriptionType": "max",
    }


def _seed(name: str, blob: dict) -> None:
    write_json_secure(paths.slot_path(name), blob)
    write_json_secure(paths.shared_mcp_path(), {})


def _never_refresh():
    return mock.patch.object(
        accounts.oauth, "refresh_access_token",
        side_effect=AssertionError("the token endpoint was called on a worker"))


@pytest.fixture
def worker(monkeypatch):
    monkeypatch.setenv(fleet_role.ROLE_ENV, "worker")


@pytest.fixture
def primary(monkeypatch):
    monkeypatch.delenv(fleet_role.ROLE_ENV, raising=False)


# ---------------------------------------------------------------------------
# fleet_role is the one reader of the role switch
# ---------------------------------------------------------------------------

def test_role_env_is_read_in_exactly_one_module():
    """Three modules used to read VIBERPIT_DAEMON_ROLE themselves and disagreed
    on what an invalid value meant. Any new direct read is a fourth."""
    pat = re.compile(r"""(environ(\.get)?|getenv)\s*[\(\[]\s*["']VIBERPIT_DAEMON_ROLE""")
    offenders = []
    for root in ("src", "server"):
        for p in (_REPO / root).rglob("*.py"):
            if p.name == "fleet_role.py" or "/tests/" in p.as_posix():
                continue
            if pat.search(p.read_text(encoding="utf-8")):
                offenders.append(p.relative_to(_REPO).as_posix())
    assert offenders == [], f"read the role through viberpit.fleet_role: {offenders}"


def test_invalid_role_fails_loudly(monkeypatch):
    monkeypatch.setenv(fleet_role.ROLE_ENV, "replica")
    with pytest.raises(ValueError, match="replica"):
        fleet_role.daemon_role()
    with pytest.raises(ValueError):
        accounts.is_refresh_authority()


def test_default_role_is_primary_and_refresh_authority(primary):
    assert fleet_role.daemon_role() == "primary"
    assert fleet_role.is_worker() is False
    assert accounts.is_refresh_authority() is True


def test_worker_role_is_not_refresh_authority(worker):
    assert fleet_role.is_worker() is True
    assert accounts.is_refresh_authority() is False


# ---------------------------------------------------------------------------
# ensure_valid_token / acquire_valid_token
# ---------------------------------------------------------------------------

def test_worker_ensure_valid_token_defers_instead_of_refreshing(worker):
    with temp_env():
        _seed("w", _token(-10_000, access="stale", refresh="R0"))
        with _never_refresh():
            with pytest.raises(accounts.RefreshDeferred) as ei:
                accounts.ensure_valid_token("w")
        assert ei.value.account == "w"
        assert not isinstance(ei.value, oauth.AuthError), (
            "a deferred refresh must never read as a dead grant")
        # The slot is untouched: the primary is the only writer of a rotation.
        assert read_json(paths.slot_path("w"))["refreshToken"] == "R0"


def test_worker_fresh_slot_is_returned_without_refreshing(worker):
    with temp_env():
        _seed("w", _token(9_000_000, access="fresh"))
        with _never_refresh():
            assert accounts.ensure_valid_token("w")["accessToken"] == "fresh"


def test_worker_acquire_valid_token_recovers_sdk_rotation_from_cfg_dir(worker):
    """The one rotation a worker may pick up is the one its own live ``claude``
    process wrote to the cfg dir — sync_back, no token-endpoint call."""
    with temp_env():
        _seed("w", _token(-10_000, access="stale", refresh="R0"))
        cfg_dir = paths.account_cfg_dir("w")
        cfg_dir.mkdir(parents=True, exist_ok=True)
        rotated = _token(9_000_000, access="rotated", refresh="R1")
        write_json_secure(cfg_dir / ".credentials.json", compose_credentials(rotated, {}))
        with _never_refresh():
            got = accounts.acquire_valid_token("w")
        assert got["accessToken"] == "rotated"
        assert read_json(paths.slot_path("w"))["accessToken"] == "rotated"


def test_worker_acquire_valid_token_reraises_when_nothing_fresher(worker):
    with temp_env():
        _seed("w", _token(-10_000, access="stale", refresh="R0"))
        with _never_refresh():
            with pytest.raises(accounts.RefreshDeferred):
                accounts.acquire_valid_token("w")


def test_primary_still_refreshes(primary):
    """The gate must be able to fail: with no role set the token endpoint IS called."""
    with temp_env():
        _seed("p", _token(-10_000, access="stale", refresh="R0"))
        calls = []

        def fake_refresh(refresh_token, prior, **_kw):
            calls.append(refresh_token)
            return {**prior, "accessToken": "new", "refreshToken": "R1",
                    "expiresAt": int(time.time() * 1000) + 9_000_000}

        with mock.patch.object(accounts.oauth, "refresh_access_token", side_effect=fake_refresh):
            assert accounts.ensure_valid_token("p")["accessToken"] == "new"
        assert calls == ["R0"]


# ---------------------------------------------------------------------------
# usage_for
# ---------------------------------------------------------------------------

def _status(name: str) -> str | None:
    with ledger.locked_ledger() as led:
        return ledger.get_status(led, name)


def test_worker_usage_for_never_refreshes_or_flips_needs_reauth(worker):
    """The read-only path #852 gave the worker's poller closure now applies to
    EVERY usage_for call in a worker process, whatever allow_refresh says."""
    with temp_env():
        _seed("w", _token(-10_000, access="stale", refresh="R0"))
        with _never_refresh(), mock.patch.object(
                accounts.oauth, "fetch_usage",
                side_effect=AssertionError("usage fetched with an expired token")):
            assert accounts.usage_for("w", allow_refresh=True, path="test") is None
        assert _status("w") != "needs-reauth"
        assert read_json(paths.slot_path("w"))["refreshToken"] == "R0"


def test_worker_usage_for_still_fetches_with_a_fresh_slot(worker):
    """Read-only is not blind: a fresh slot (kept so by the primary) is used for
    the usage fetch, so the worker's rotation monitor keeps its poll signal."""
    with temp_env():
        _seed("w", _token(9_000_000, access="fresh"))
        with _never_refresh(), mock.patch.object(
                accounts.oauth, "fetch_usage", return_value=usage(session=12.0)) as fu:
            snap = accounts.usage_for("w", allow_refresh=True, path="test")
        assert snap is not None
        fu.assert_called_once()
        assert fu.call_args.args[0] == "fresh"


def test_primary_usage_for_refreshes_an_expired_slot(primary):
    with temp_env():
        _seed("p", _token(-10_000, access="stale", refresh="R0"))

        def fake_refresh(refresh_token, prior, **_kw):
            return {**prior, "accessToken": "new", "refreshToken": "R1",
                    "expiresAt": int(time.time() * 1000) + 9_000_000}

        with mock.patch.object(accounts.oauth, "refresh_access_token",
                               side_effect=fake_refresh) as rf, \
                mock.patch.object(accounts.oauth, "fetch_usage",
                                  return_value=usage(session=12.0)) as fu:
            assert accounts.usage_for("p", allow_refresh=True, path="test") is not None
        rf.assert_called_once()
        assert fu.call_args.args[0] == "new"

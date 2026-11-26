"""preflight.check — the local credential check on a composed config (FR-304, #947)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from server.sessions import preflight
from server.sessions.account_auth import LAUNCH_CFG, LAUNCH_ENVELOPE, LAUNCH_SETUP

NOW_MS = 1_800_000_000_000
PAST = NOW_MS - 60_000
FUTURE = NOW_MS + 3_600_000


def _write(cfg_dir: Path, blob: dict | None, *, raw: str | None = None) -> None:
    path = cfg_dir / ".credentials.json"
    if raw is not None:
        path.write_text(raw)
    else:
        path.write_text(json.dumps(blob))


def _check(cfg_dir: Path, *, provider: str | None = "anthropic",
           launch_kind: str = LAUNCH_CFG) -> str | None:
    return preflight.check("acct", cfg_dir, provider, launch_kind=launch_kind, now_ms=NOW_MS)


def _oauth(**extra) -> dict:
    return {"claudeAiOauth": {"accessToken": "tok", **extra}}


@pytest.mark.parametrize("blob, expected", [
    # Healthy: live access token.
    (_oauth(expiresAt=FUTURE, refreshToken="r"), None),
    # Lapsed access token but a live refresh token: the CLI refreshes it (ruling).
    (_oauth(expiresAt=PAST, refreshToken="r"), None),
    (_oauth(expiresAt=PAST, refreshToken="r", refreshTokenExpiresAt=FUTURE), None),
    # Lapsed and nothing to refresh with.
    (_oauth(expiresAt=PAST), "credentials_expired"),
    (_oauth(expiresAt=PAST, refreshToken=""), "credentials_expired"),
    (_oauth(expiresAt=PAST, refreshToken="r", refreshTokenExpiresAt=PAST), "credentials_expired"),
    # No usable token at all.
    ({"claudeAiOauth": {"refreshToken": "r", "expiresAt": FUTURE}}, "missing_credentials"),
    ({"claudeAiOauth": {"accessToken": ""}}, "missing_credentials"),
    ({"mcpOAuth": {}}, "missing_credentials"),
    ({}, "missing_credentials"),
    # No expiry stamp: nothing says it lapsed.
    (_oauth(), None),
])
def test_composed_credentials_table(tmp_path, blob, expected):
    _write(tmp_path, blob)
    assert _check(tmp_path) == expected


def test_unparseable_file_is_missing_credentials(tmp_path):
    _write(tmp_path, None, raw="{not json")
    assert _check(tmp_path) == "missing_credentials"
    _write(tmp_path, None, raw="[]")
    assert _check(tmp_path) == "missing_credentials"


def test_missing_file_is_not_judged(tmp_path):
    """compose() writes the file or raises, so a dir without one is not a composed
    config (a stubbed compose in tests, say) — nothing to judge, fail open."""
    assert _check(tmp_path) is None


@pytest.mark.parametrize("provider", ["copilot", "openai", None])
def test_non_anthropic_provider_is_skipped(tmp_path, provider):
    _write(tmp_path, _oauth(expiresAt=PAST))
    assert _check(tmp_path, provider=provider) is None
    assert not preflight.applies(provider, tmp_path)


@pytest.mark.parametrize("kind", [LAUNCH_SETUP, LAUNCH_ENVELOPE])
def test_env_token_launch_is_skipped(tmp_path, kind):
    """A setup-token / envelope launch authenticates from the env, not the composed
    claudeAiOauth blob, so that blob's state says nothing about the launch."""
    _write(tmp_path, _oauth(expiresAt=PAST))
    assert _check(tmp_path, launch_kind=kind) is None


def test_empty_cfg_dir_is_skipped():
    assert preflight.check("acct", "", "anthropic", launch_kind=LAUNCH_CFG) is None
    assert not preflight.applies("anthropic", "")
    assert preflight.applies("anthropic", "/some/dir")


def test_failed_carries_account_and_reason():
    exc = preflight.PreflightFailed("alpha", preflight.CREDENTIALS_EXPIRED)
    assert (exc.account, exc.reason) == ("alpha", "credentials_expired")
    assert "alpha" in str(exc) and "credentials_expired" in str(exc)
    assert preflight.PREFLIGHT_REASONS == {"missing_credentials", "credentials_expired"}


def test_check_uses_wall_clock_by_default(tmp_path):
    _write(tmp_path, _oauth(expiresAt=1))          # 1970: lapsed, no refresh
    assert preflight.check("acct", tmp_path, "anthropic", launch_kind=LAUNCH_CFG) == (
        "credentials_expired")

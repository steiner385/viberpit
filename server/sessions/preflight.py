"""Local credential preflight for a Claude cfg-dir launch (spec 002 FR-304, #947).

Before a spawn builds its driver, the orchestrator asks whether the composed
``CLAUDE_CONFIG_DIR`` the CLI is about to read can authenticate at all. Without
this a session whose composed ``claudeAiOauth`` had lapsed came up "Ready" and
failed its first turn with a 401. Pure filesystem — no network, no subprocess and
never the keyring: the LAUNCH KIND is handed in by the caller
(``account_auth.oauth_launch_env``), because computing it reads the setup-token.

The other engines have their own gates and are not judged here: Copilot's is the
proxy health check (``CopilotProxyManager.ensure``), and a Codex account has no
Claude credential to compose (its cfg dir is ""). A failure is typed
(:class:`PreflightFailed` ``.reason``) so the queue drain can record it and rotate
the retry away from the account (``spawn_failure``).
"""
from __future__ import annotations

import time
from pathlib import Path

from viberpit.account_registry import PROVIDER_ANTHROPIC
from viberpit.configdir import CREDENTIALS_NAME
from viberpit.creds import read_json
from server.sessions.account_auth import LAUNCH_CFG

#: The composed config has no usable ``claudeAiOauth.accessToken`` (or is unreadable).
#: On a broker-runner worker this is also what a LAPSED ENVELOPE LEASE looks like:
#: ``oauth_launch_env`` fails closed to a cfg launch and ``compose`` wrote ``{}`` for
#: ``claudeAiOauth`` — the slot itself may be fine, the lease is what needs renewing.
MISSING_CREDENTIALS = "missing_credentials"
#: The access token has lapsed and nothing can refresh it (no refresh token, or an
#: expired one).
CREDENTIALS_EXPIRED = "credentials_expired"
PREFLIGHT_REASONS = frozenset({MISSING_CREDENTIALS, CREDENTIALS_EXPIRED})


class PreflightFailed(Exception):
    """*account*'s composed config cannot authenticate: *reason* is one of
    :data:`PREFLIGHT_REASONS`."""

    def __init__(self, account: str, reason: str) -> None:
        self.account = account
        self.reason = reason
        super().__init__(f"account {account!r} failed credential preflight ({reason})")


def applies(provider: str | None, cfg_dir: str | Path | None) -> bool:
    """Does the check have anything to say for this launch? Only a registered Claude
    account launching from a composed cfg dir. ``provider`` None (an unregistered name
    or an unreadable manifest, ``accounts.provider_or_none``) answers no, like every
    other provider-specific gate."""
    return provider == PROVIDER_ANTHROPIC and bool(str(cfg_dir or ""))


def _lapsed(stamp: object, now_ms: float) -> bool:
    return isinstance(stamp, (int, float)) and stamp <= now_ms


def check(account: str, cfg_dir: str | Path, provider: str | None, *,
          launch_kind: str, now_ms: float | None = None) -> str | None:
    """The reason *account*'s composed *cfg_dir* cannot authenticate, or None.

    None when the check does not apply (:func:`applies`) or when the launch does not
    read the composed blob at all — a setup-token or envelope launch
    (``launch_kind != "cfg"``) authenticates from the env. A dir WITHOUT a
    ``.credentials.json`` is not judged either: ``configdir.compose`` writes the file
    or raises, so a bare dir is not a composed config (a stubbed compose), not a
    missing credential. With the file present:

    * no ``claudeAiOauth.accessToken`` (or unreadable JSON) → ``missing_credentials``
    * ``expiresAt`` (epoch ms) in the past AND no usable refresh — ``refreshToken``
      absent/empty, or ``refreshTokenExpiresAt`` in the past → ``credentials_expired``
    * otherwise healthy. A lapsed access token with a live refresh token is HEALTHY:
      the CLI refreshes it on start (ruling, #947 S2).
    """
    if not applies(provider, cfg_dir) or launch_kind != LAUNCH_CFG:
        return None
    path = Path(cfg_dir) / CREDENTIALS_NAME
    if not path.exists():
        return None
    try:
        data = read_json(path)
    except (OSError, ValueError):
        return MISSING_CREDENTIALS
    oauth = data.get("claudeAiOauth") if isinstance(data, dict) else None
    if not isinstance(oauth, dict) or not oauth.get("accessToken"):
        return MISSING_CREDENTIALS
    now = time.time() * 1000 if now_ms is None else now_ms
    if _lapsed(oauth.get("expiresAt"), now):
        refresh_ok = bool(oauth.get("refreshToken")) and not _lapsed(
            oauth.get("refreshTokenExpiresAt"), now)
        if not refresh_ok:
            return CREDENTIALS_EXPIRED
    return None

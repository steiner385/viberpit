"""In-process OAuth2 PKCE flow for generating long-lived setup-tokens.

Replaces driving `claude setup-token` headlessly (which relies on a browser+poll
path that does not work without a browser). Constants are claude's own OAuth
client config, read from the bundled binary — verify against the real binary if
generation suddenly starts failing. NEVER log the token or the pasted code.
"""
from __future__ import annotations

import base64
import hashlib
import os
import re
import secrets
from dataclasses import dataclass
from urllib.parse import urlencode

import httpx

AUTHORIZE_URL = "https://claude.com/cai/oauth/authorize"
TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
REDIRECT_URI = "https://platform.claude.com/oauth/code/callback"

# --- scopes ----------------------------------------------------------------
# ``user:inference`` alone mints a token that can LAUNCH a session but that
# https://api.anthropic.com/api/oauth/usage refuses — with 429 (a rate-limit
# shape), not 401 — so every account recovered through the paste-code reauth
# reported no quota at all and sat in a permanent usage-fetch backoff.
#
# Verified against the real credentials rather than assumed: every working
# credential slot carries ``user:profile`` next to ``user:inference`` and reads
# the usage endpoint 200, while all three stored setup-tokens (narrow scope) get
# 429 on the same call from the same host seconds apart. The bundled binary
# requests the same profile+inference pair. ``user:profile`` is therefore the
# scope the usage endpoint reads; the other scopes a full login carries
# (file_upload / mcp_servers / sessions) are not needed for quota, so we do not
# request them — a narrower ask is less likely to be refused.
DEFAULT_SCOPE = "user:inference user:profile"
# The historical scope. Still launch-capable, so it is the automatic fallback if
# the broadened request is ever refused: reauth must never end up worse than it
# was before this change, because it is the fleet's last-resort recovery path.
FALLBACK_SCOPE = "user:inference"
# Deploy-free override (edge env). Pins the scope without shipping a revert, and
# covers the one failure mode the exchange-time fallback cannot see: a refusal on
# the *authorize* page, where the user never gets a code to submit.
SCOPE_ENV = "CCSWITCH_SETUP_TOKEN_SCOPE"
# Mimic the binary's exchange client (captured: axios/1.15.2) for WAF/Cloudflare friendliness.
_USER_AGENT = "axios/1.15.2"
# Captured: claude requests a 1-year token lifetime in the exchange body. Required.
_EXPIRES_IN = 31536000
_TOKEN_RE = re.compile(r"sk-ant-oat01-[A-Za-z0-9_\-]+")
_TIMEOUT_S = 30.0


class SetupTokenError(Exception):
    """Base for setup-token OAuth failures."""


class SetupTokenStateMismatch(SetupTokenError):
    pass


class SetupTokenInvalidCode(SetupTokenError):
    """The token endpoint rejected the exchange with 400.

    Carries the (truncated) response body so a SCOPE refusal can be told apart
    from the ordinary "you pasted an expired code" 400. Without that distinction
    the caller's scope fallback would also fire on a mistyped code and send the
    user round a confusing loop. NEVER contains the pasted code or a token — only
    the server's own error payload.
    """

    def __init__(self, message: str, *, body: str | None = None):
        super().__init__(message)
        self.body = body

    @property
    def is_scope_rejection(self) -> bool:
        return "scope" in (self.body or "").lower()


class SetupTokenTransient(SetupTokenError):
    pass


class SetupTokenNoToken(SetupTokenError):
    pass


@dataclass
class SetupTokenChallenge:
    authorize_url: str
    verifier: str
    state: str
    # The scope actually consented to in ``authorize_url``. Carried on the
    # challenge because the exchange is bound to it: a code minted under one
    # scope cannot be redeemed under another, so a fallback has to restart at
    # ``begin()`` rather than retry ``exchange()``.
    scope: str = DEFAULT_SCOPE


def _b64url_nopad(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def default_scope() -> str:
    """The scope ``begin()`` requests when the caller does not pin one."""
    return os.environ.get(SCOPE_ENV, "").strip() or DEFAULT_SCOPE


def begin(scope: str | None = None) -> SetupTokenChallenge:
    scope = scope or default_scope()
    verifier = _b64url_nopad(secrets.token_bytes(32))
    challenge = _b64url_nopad(hashlib.sha256(verifier.encode("ascii")).digest())
    state = _b64url_nopad(secrets.token_bytes(32))
    query = urlencode(
        {
            "code": "true",
            "client_id": CLIENT_ID,
            "response_type": "code",
            "redirect_uri": REDIRECT_URI,
            "scope": scope,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": state,
        }
    )
    return SetupTokenChallenge(f"{AUTHORIZE_URL}?{query}", verifier, state, scope)


def exchange(pasted_code: str, challenge: SetupTokenChallenge) -> str:
    """Exchange a pasted ``code#state`` for a long-lived sk-ant-oat01 token.

    NEVER log pasted_code or the returned token.
    """
    code, _, state = pasted_code.strip().partition("#")
    if not state or state != challenge.state:
        raise SetupTokenStateMismatch("pasted code state does not match this request")

    body = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT_URI,
        "client_id": CLIENT_ID,
        "code_verifier": challenge.verifier,
        "state": state,
        "expires_in": _EXPIRES_IN,
    }
    headers = {
        "User-Agent": _USER_AGENT,
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    try:
        resp = httpx.post(TOKEN_URL, json=body, headers=headers, timeout=_TIMEOUT_S)
    except httpx.HTTPError as exc:
        raise SetupTokenTransient(f"token endpoint unreachable: {exc!r}") from exc

    if resp.status_code == 400:
        # Body is the server's own OAuth error payload (e.g. invalid_grant vs
        # invalid_scope) — never the pasted code. Truncated, and only ever read
        # by is_scope_rejection; not logged here.
        raise SetupTokenInvalidCode("code invalid or expired", body=resp.text[:512])
    if resp.status_code >= 500:
        raise SetupTokenTransient(f"token endpoint {resp.status_code}")
    if resp.status_code != 200:
        raise SetupTokenError(f"unexpected token endpoint status {resp.status_code}")

    # The setup-token is the JSON `access_token` (verified). Regex is a fallback.
    try:
        token = (resp.json() or {}).get("access_token") or ""
    except ValueError:
        token = ""
    if not token:
        m = _TOKEN_RE.search(resp.text)
        token = m.group(0) if m else ""
    if not token.startswith("sk-ant-oat01-"):
        raise SetupTokenNoToken("token endpoint returned no setup-token")
    return token

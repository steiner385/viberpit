"""In-process OAuth2 PKCE flow for generating long-lived setup-tokens.

Replaces driving `claude setup-token` headlessly (which relies on a browser+poll
path that does not work without a browser). Constants are claude's own OAuth
client config, read from the bundled binary — verify against the real binary if
generation suddenly starts failing. NEVER log the token or the pasted code.
"""
from __future__ import annotations

import base64
import hashlib
import re
import secrets
from dataclasses import dataclass
from urllib.parse import urlencode

import httpx

AUTHORIZE_URL = "https://claude.com/cai/oauth/authorize"
TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
REDIRECT_URI = "https://platform.claude.com/oauth/code/callback"
SCOPE = "user:inference"
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
    pass


class SetupTokenTransient(SetupTokenError):
    pass


class SetupTokenNoToken(SetupTokenError):
    pass


@dataclass
class SetupTokenChallenge:
    authorize_url: str
    verifier: str
    state: str


def _b64url_nopad(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def begin() -> SetupTokenChallenge:
    verifier = _b64url_nopad(secrets.token_bytes(32))
    challenge = _b64url_nopad(hashlib.sha256(verifier.encode("ascii")).digest())
    state = _b64url_nopad(secrets.token_bytes(32))
    query = urlencode(
        {
            "code": "true",
            "client_id": CLIENT_ID,
            "response_type": "code",
            "redirect_uri": REDIRECT_URI,
            "scope": SCOPE,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": state,
        }
    )
    return SetupTokenChallenge(f"{AUTHORIZE_URL}?{query}", verifier, state)


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
        raise SetupTokenInvalidCode("code invalid or expired")
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

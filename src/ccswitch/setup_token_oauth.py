"""In-process OAuth2 PKCE flow for generating long-lived setup-tokens.

Replaces driving `claude setup-token` headlessly (which relies on a browser+poll
path that does not work without a browser). Constants are claude's own OAuth
client config, read from the bundled binary — verify against the real binary if
generation suddenly starts failing. NEVER log the token or the pasted code.
"""
from __future__ import annotations

import base64
import hashlib
import secrets
from dataclasses import dataclass
from urllib.parse import urlencode

AUTHORIZE_URL = "https://claude.com/cai/oauth/authorize"
TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
REDIRECT_URI = "https://platform.claude.com/oauth/code/callback"
SCOPE = "user:inference"
# Mimic the binary's exchange client (captured: axios/1.15.2) for WAF/Cloudflare friendliness.
_USER_AGENT = "axios/1.15.2"
# Captured: claude requests a 1-year token lifetime in the exchange body. Required.
_EXPIRES_IN = 31536000


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

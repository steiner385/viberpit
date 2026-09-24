"""ccsys1 internal token: mint/verify, tamper/expiry rejection — the loopback-only
credential the primary daemon's own automation loop uses to call its co-located
edge (server/sessions/internal_dispatch.py)."""
from __future__ import annotations

import os
from unittest.mock import patch

from server.auth.internal_token import PREFIX, mint_token, verify_token

_ENV = {"VIBERPIT_SESSION_SECRET": "x" * 32}


def test_mint_verify_roundtrip():
    with patch.dict(os.environ, _ENV):
        tok = mint_token()
        assert tok.startswith(PREFIX)
        assert verify_token(tok) is True


def test_verify_rejects_tamper_wrong_prefix_and_expiry():
    with patch.dict(os.environ, _ENV):
        tok = mint_token()
        assert verify_token(tok[:-2] + "zz") is False
        assert verify_token("nope") is False
        assert verify_token("ccas1:" + tok[len(PREFIX):]) is False


def test_verify_rejects_expired_token():
    with patch.dict(os.environ, _ENV):
        import server.auth.internal_token as internal_token
        tok = mint_token()
        with patch.object(internal_token, "MAX_AGE_S", -1.0):
            assert verify_token(tok) is False

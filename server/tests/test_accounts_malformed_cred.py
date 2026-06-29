"""Regression: a malformed credential blob must NOT crash the usage sweep.

Incident 2026-06-29: the `primary` account's store slot held an empty blob `{}`
(a reauth that failed mid-write). `ensure_valid_token` reached `blob["refreshToken"]`
and raised an *uncaught* `KeyError` — only `oauth.AuthError`/`TransientError` are
handled downstream. That KeyError propagated through `usage_for` → the daemon's
`/rpc/usage` RPC → HTTP 500, which the remote-mode edge maps to a blanket HTTP 409
on EVERY route (`DaemonError` handler), taking the whole app down.

A blob with no usable `refreshToken` is semantically a dead token family → it must
route through the existing needs-reauth path (return None, mark needs-reauth), never
escape as a bare KeyError. See test_usage.py for the blast-radius (poller.all) guard.
"""
from __future__ import annotations

import json

import pytest

from ccswitch import accounts, ledger, oauth, paths


def _write_raw_slot(name: str, blob: dict) -> None:
    """Write exact JSON bytes to the slot (bypassing write_credentials, which
    would inject/encrypt fields — we want the precise on-disk shape)."""
    paths.store_dir().mkdir(parents=True, exist_ok=True)
    paths.slot_path(name).write_text(json.dumps(blob))


@pytest.mark.parametrize("blob", [{}, {"accessToken": "x", "expiresAt": 0}])
def test_ensure_valid_token_raises_autherror_not_keyerror(tmp_path, monkeypatch, blob):
    """A non-fresh blob lacking refreshToken raises AuthError (the canonical
    'needs reauth' signal), never a KeyError."""
    monkeypatch.setenv("CCSWITCH_HOME", str(tmp_path))
    _write_raw_slot("primary", blob)
    with pytest.raises(oauth.AuthError):
        accounts.ensure_valid_token("primary")


def test_usage_for_marks_needs_reauth_on_missing_refresh_token(tmp_path, monkeypatch):
    """usage_for must degrade an unrefreshable account to needs-reauth + None,
    exactly as it does for a genuine AuthError — not raise."""
    monkeypatch.setenv("CCSWITCH_HOME", str(tmp_path))
    _write_raw_slot("primary", {})  # the exact incident shape
    result = accounts.usage_for("primary")
    assert result is None
    with ledger.locked_ledger() as led:
        assert led["accounts"]["primary"]["status"] == "needs-reauth"

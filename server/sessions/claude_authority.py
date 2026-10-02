"""Primary-side minting of Claude credential envelopes (#2629, spec §1).

The primary daemon on dobby is the sole Claude credential authority: it alone reads the credential
slot and the stored setup-token, and publishes the result as an :class:`Envelope` per account for
the edge to push to runners (``viberpit.credential_envelope.EnvelopeStore`` is the receiving half).

Preference: a stored setup-token (a year-long, refresh-token-free credential), else the slot's
current access token with its expiry. ``generation`` is a persisted monotonic counter per account
that bumps exactly when the vended token changes, so a runner can refuse a superseded rotation.
"""
from __future__ import annotations

import hashlib
import threading

from viberpit import accounts as _accounts
from viberpit import crypto, paths
from viberpit.creds import read_credentials, read_json, write_json_secure
from viberpit.credential_envelope import OAUTH_ACCESS, SETUP_TOKEN, Envelope

_GEN_LOCK = threading.Lock()


def _generation_path():
    return paths.store_dir() / "envelope-generations.json"


def _fingerprint(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _raw_envelope(account: str) -> tuple[str, str, float | None] | None:
    """(kind, token, expires_at) the authority would vend now, or None if it has nothing."""
    setup = crypto.read_oauth_token(account)
    if setup:
        return SETUP_TOKEN, setup, None
    slot = paths.slot_path(account)
    if not slot.exists():
        return None
    blob = read_credentials(slot)
    access = blob.get("accessToken")
    if not access:
        return None
    expires_ms = blob.get("expiresAt")
    return OAUTH_ACCESS, access, (expires_ms / 1000.0 if expires_ms else None)


def mint_envelopes(accounts: list[str]) -> list[Envelope]:
    """Envelopes for every account in *accounts* that has a credential; primary only."""
    if not _accounts.is_refresh_authority():
        raise RuntimeError("only the primary daemon may mint Claude credential envelopes")
    out: list[Envelope] = []
    with _GEN_LOCK:
        gens = read_json(_generation_path()) if _generation_path().exists() else {}
        changed = False
        for account in accounts:
            raw = _raw_envelope(account)
            if raw is None:
                continue
            kind, token, expires_at = raw
            fp = _fingerprint(token)
            prev = gens.get(account) or {}
            gen = int(prev.get("generation", 0))
            if prev.get("fp") != fp:
                gen += 1
                gens[account] = {"fp": fp, "generation": gen}
                changed = True
            out.append(Envelope(account, kind, token, expires_at, gen))
        if changed:
            write_json_secure(_generation_path(), gens)
    return out

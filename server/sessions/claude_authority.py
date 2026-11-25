"""Primary-side minting of Claude credential envelopes (#2629, spec §1).

The primary daemon on dobby is the sole Claude credential authority: it alone reads the credential
slot and the stored setup-token, and publishes the result as an :class:`Envelope` per account for
the edge to push to runners (``viberpit.credential_envelope.EnvelopeStore`` is the receiving half).

Preference: a stored setup-token (a year-long, refresh-token-free credential), else the slot's
current access token with its expiry. ``generation`` is a persisted monotonic counter per account
that bumps exactly when the vended token changes, so a runner can refuse a superseded rotation.

Remote Control (runner follow-up to plan 2026-11-24): :func:`mint_remote_control_envelopes` vends,
for every account whose browser-login slot is Remote-Control-ready, a SECOND envelope — the slot's
short-lived access token plus the org UUID, the verified minimum recipe for a bridge — with its
own generation counter (:func:`_rc_generation_path`). It rides beside the inference envelope,
which keeps preferring the setup-token, so a runner session without Remote Control is unchanged.
The slot is kept fresh by the primary daemon's token sweep (the refresh authority); this module
only reads it. Neither envelope ever carries a refresh token.
"""
from __future__ import annotations

import hashlib
import threading

from viberpit import accounts as _accounts
from viberpit import crypto, paths
from viberpit.creds import read_credentials, read_json, write_json_secure
from viberpit.credential_envelope import OAUTH_ACCESS, SETUP_TOKEN, Envelope
from server.sessions import remote_control as _remote_control
from server.sessions.account_auth import (
    organization_uuid, remote_control_slot_ready, slot_remote_control_credential,
)

_GEN_LOCK = threading.Lock()

# (account, kind, token, expires_at, org_uuid) — what the authority would vend now.
_Raw = tuple[str, str, str, float | None, str | None]


def _generation_path():
    return paths.store_dir() / "envelope-generations.json"


def _rc_generation_path():
    """The Remote Control envelopes' generations: their own counters, so a slot refresh never
    bumps (or rolls back) the inference envelope's generation and vice versa."""
    return paths.store_dir() / "rc-envelope-generations.json"


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


def _require_authority() -> None:
    if not _accounts.is_refresh_authority():
        raise RuntimeError("only the primary daemon may mint Claude credential envelopes")


def _stamp_generations(path, raws: list[_Raw]) -> list[Envelope]:
    """Envelopes for *raws*, each with its account's persisted generation in *path* — bumped
    exactly when the vended token's fingerprint changes."""
    out: list[Envelope] = []
    with _GEN_LOCK:
        gens = read_json(path) if path.exists() else {}
        changed = False
        for account, kind, token, expires_at, org in raws:
            fp = _fingerprint(token)
            prev = gens.get(account) or {}
            gen = int(prev.get("generation", 0))
            if prev.get("fp") != fp:
                gen += 1
                gens[account] = {"fp": fp, "generation": gen}
                changed = True
            out.append(Envelope(account, kind, token, expires_at, gen, org_uuid=org))
        if changed:
            write_json_secure(path, gens)
    return out


def mint_envelopes(accounts: list[str]) -> list[Envelope]:
    """Envelopes for every account in *accounts* that has a credential; primary only."""
    _require_authority()
    raws: list[_Raw] = []
    for account in accounts:
        raw = _raw_envelope(account)
        if raw is not None:
            raws.append((account, *raw, None))
    return _stamp_generations(_generation_path(), raws)


def _raw_remote_control_envelope(account: str) -> _Raw | None:
    """The Remote Control credential the authority would vend for *account* now, or None: the
    slot must be Remote-Control-ready (``remote_control_slot_ready``: full scope, live grant,
    org known) and its access token full-scope and unexpired. Runners apply their own launch
    skew; the token sweep keeps the slot fresh."""
    if not remote_control_slot_ready(account):
        return None
    org = organization_uuid(account)
    cred = slot_remote_control_credential(account)
    if org is None or cred is None:
        return None
    token, expires_at = cred
    return account, OAUTH_ACCESS, token, expires_at, org


def mint_remote_control_envelopes(accounts: list[str]) -> list[Envelope]:
    """Remote Control envelopes for every Remote-Control-ready account in *accounts*; primary
    only. None at all with the Remote Control kill switch off — the claude.ai login is then
    distributed nowhere."""
    _require_authority()
    if not _remote_control.remote_control_enabled():
        return []
    raws = [raw for account in accounts
            if (raw := _raw_remote_control_envelope(account)) is not None]
    return _stamp_generations(_rc_generation_path(), raws)

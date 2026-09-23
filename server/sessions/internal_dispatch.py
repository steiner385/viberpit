"""Loopback caller for the primary daemon's own automation-session-create path
(spec: automation dispatch balance, 2026-09-20). Mirrors server/assistant/client.py's
loopback pattern, but for the fixed internal ``ccsys1`` token and its one route
(server/api/internal_sessions.py) rather than a per-session assistant token.

The primary daemon and its edge are already assumed co-located on one host by an
existing feature (the AI assistant's tools call the edge over 127.0.0.1 from
``SessionOrchestrator._setup_assistant_overlay``, server/sessions/orchestrator.py) —
this reuses that same assumption rather than introducing a new one.
"""
from __future__ import annotations

import logging

import httpx

from server.auth.internal_token import ALLOWED_PATH, CERTIFY_PATH, mint_token
from server.ipc.client import DaemonError, reconstruct_create_session_error
from server.sessions.types import SessionRecord

log = logging.getLogger(__name__)

# Matches SPAWN_RPC_TIMEOUT (server/sessions/remote.py) — this call runs the same
# clone→worktree→spawn pipeline, just over a loopback HTTP hop instead of the mTLS
# RPC one.
_TIMEOUT_S = 90.0


class EdgeUnreachable(Exception):
    """The co-located edge could not be reached, or answered in a way that isn't
    this route's own error envelope (wrong port, feature disabled elsewhere, token
    rejected). The caller should fall back to a direct local
    ``SessionOrchestrator.create_session`` — never treat this as "no accounts
    available" or any other domain refusal."""


async def create_via_edge(
    *, port: int, transport: httpx.AsyncBaseTransport | None = None, **kwargs: object,
) -> SessionRecord:
    url = f"http://127.0.0.1:{port}{ALLOWED_PATH}"
    headers = {"Authorization": f"Bearer {mint_token()}"}
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_S, transport=transport) as client:
            r = await client.post(url, json=kwargs, headers=headers)
    except httpx.HTTPError as exc:
        raise EdgeUnreachable(f"{exc.__class__.__name__}: {exc}") from exc
    if r.status_code == 422:
        try:
            err = r.json()["error"]
        except (ValueError, KeyError, TypeError) as exc:
            raise EdgeUnreachable(f"malformed error envelope ({r.status_code})") from exc
        daemon_exc = DaemonError(err.get("type", "Unknown"), err.get("message", ""), err.get("data"))
        reconstructed = reconstruct_create_session_error(daemon_exc)
        if reconstructed is daemon_exc:
            raise daemon_exc
        raise reconstructed from daemon_exc
    if r.status_code >= 400:
        # Anything other than our own 422 domain-refusal envelope — 401/403 (token
        # rejected, wrong route), 404 (edge predates this route), 5xx — is a wiring
        # problem, not a refusal the caller should see: fall back instead.
        raise EdgeUnreachable(f"unexpected status {r.status_code}: {r.text[:200]}")
    return SessionRecord.from_dict(r.json()["session"])


# One loopback hop + one mTLS RPC hop + a `_persist_status("complete")` on the owning
# host (which runs the completion guard). Well under the create pipeline's budget.
_CERTIFY_TIMEOUT_S = 60.0


async def certify_harvested_via_edge(
    *, port: int, session_id: str,
    transport: httpx.AsyncBaseTransport | None = None,
) -> None:
    """Ask the co-located edge to certify *session_id* ``complete`` on the host that
    owns it (server/api/internal_sessions.py ``certify_harvested_session``).

    The chained ingest that calls this runs on the primary and has already observed
    the deliverable (proposal rows written), but the session it harvested may live
    on any fleet host — the create above ranked it there — and its status can only
    be driven where its record and driver are. Any transport failure or non-2xx is
    ``EdgeUnreachable``: there is no domain refusal on this route, and the caller
    logs and moves on (the harvest is already durable)."""
    url = f"http://127.0.0.1:{port}{CERTIFY_PATH}"
    headers = {"Authorization": f"Bearer {mint_token()}"}
    try:
        async with httpx.AsyncClient(timeout=_CERTIFY_TIMEOUT_S, transport=transport) as client:
            r = await client.post(url, json={"session_id": session_id}, headers=headers)
    except httpx.HTTPError as exc:
        raise EdgeUnreachable(f"{exc.__class__.__name__}: {exc}") from exc
    if r.status_code >= 400:
        raise EdgeUnreachable(f"unexpected status {r.status_code}: {r.text[:200]}")

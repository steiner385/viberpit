"""Daemon RPC for the Claude credential broker (#2629, spec §2) and MCP distribution (#3287).

The edge pushes the primary's envelopes to every runner over the RPC channel it already holds;
the daemon hands them to the process-wide :class:`EnvelopeStore` (memory + host-local ``0600``
cache) and acks the highest generation it holds per account.

The same push optionally carries the shared MCP store's public view (``mcp_bundles``) and the
grant reconcile's live purge keys (``mcp_drop``); the daemon caches them in the process-wide
:class:`McpViewStore` and, when it owns its account dirs (:mod:`server.ipc.runner_mcp`),
publishes the view into them and answers with the dirs' terminal logins as ``mcp_offers``. An
old edge sends neither field and gets exactly ``{"acks": ...}`` back; a new edge gets
``mcp_offers`` only when there is something to offer.

Remote Control (runner follow-up to plan 2026-11-24): the push may also carry ``rc_envelopes``,
each Remote-Control-ready account's claude.ai access token + org UUID. They go to their own
:func:`~viberpit.credential_envelope.rc_runner_store` (same lease, own generations and
``0600`` cache); an absent field — an edge that vends none, or an old edge — drops what was
held, so new Remote Control launches fail closed. A malformed entry is skipped, never failing
the sync. The daemon answers ``rc_acks`` when it holds any, then tells the orchestrator
(``remote_control_credentials_synced``) so live Remote Control sessions get the new token.
"""
from __future__ import annotations

import asyncio
import logging

from fastapi import FastAPI

from server.ipc import runner_mcp
from server.ipc.daemon import register_rpc
from viberpit import credential_envelope
from viberpit.mcp_view_store import runner_view

log = logging.getLogger(__name__)


def _rc_envelopes(raw: object) -> list[credential_envelope.Envelope]:
    """The well-formed Remote Control envelopes in *raw*: ``oauth_access`` with an org UUID.
    Anything else is skipped (logged by account only, never the token)."""
    out: list[credential_envelope.Envelope] = []
    for item in raw if isinstance(raw, list) else []:
        try:
            env = credential_envelope.Envelope.from_dict(item)
        except (KeyError, TypeError, ValueError, AttributeError):
            log.info("creds_sync: skipped a malformed remote control envelope")
            continue
        if env.kind != credential_envelope.OAUTH_ACCESS or not env.org_uuid:
            log.info("creds_sync: skipped a remote control envelope for %s (kind=%s, org=%s)",
                     env.account, env.kind, bool(env.org_uuid))
            continue
        out.append(env)
    return out


def register_creds(app: FastAPI) -> None:
    async def creds_sync(p: dict) -> dict:
        envelopes = [credential_envelope.Envelope.from_dict(e) for e in p.get("envelopes", [])]
        lease_s = float(p["lease_s"])
        acks = credential_envelope.runner_store().sync(envelopes, lease_s)
        res: dict = {"acks": acks}
        rc_acks = credential_envelope.rc_runner_store().sync(
            _rc_envelopes(p.get("rc_envelopes")), lease_s)
        if rc_acks:
            res["rc_acks"] = rc_acks
        orchestrator = getattr(app.state, "orchestrator", None)
        synced = getattr(orchestrator, "remote_control_credentials_synced", None)
        if callable(synced):
            synced()
        bundles = p.get("mcp_bundles")
        if isinstance(bundles, dict):
            # A malformed ``mcp_drop`` (a bool, a bare string) must not fail the sync after
            # the envelope acks were taken; anything but a list is treated as no purge keys.
            drop = p.get("mcp_drop")
            await asyncio.to_thread(runner_view().sync, bundles, drop if isinstance(drop, list) else [])
            offers = await asyncio.to_thread(runner_mcp.sync_local_dirs)
            if offers:
                res["mcp_offers"] = offers
        return res

    register_rpc(app, "creds_sync", creds_sync)

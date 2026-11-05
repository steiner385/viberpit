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
"""
from __future__ import annotations

import asyncio

from fastapi import FastAPI

from server.ipc import runner_mcp
from server.ipc.daemon import register_rpc
from viberpit import credential_envelope
from viberpit.mcp_view_store import runner_view


def register_creds(app: FastAPI) -> None:
    async def creds_sync(p: dict) -> dict:
        envelopes = [credential_envelope.Envelope.from_dict(e) for e in p.get("envelopes", [])]
        acks = credential_envelope.runner_store().sync(envelopes, float(p["lease_s"]))
        res: dict = {"acks": acks}
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

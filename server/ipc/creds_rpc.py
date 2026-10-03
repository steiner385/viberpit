"""Daemon RPC for the Claude credential broker (#2629, spec §2).

The edge pushes the primary's envelopes to every runner over the RPC channel it already holds;
the daemon hands them to the process-wide :class:`EnvelopeStore` (memory + host-local ``0600``
cache) and acks the highest generation it holds per account.
"""
from __future__ import annotations

from fastapi import FastAPI

from server.ipc.daemon import register_rpc
from viberpit import credential_envelope


def register_creds(app: FastAPI) -> None:
    async def creds_sync(p: dict) -> dict:
        envelopes = [credential_envelope.Envelope.from_dict(e) for e in p.get("envelopes", [])]
        acks = credential_envelope.runner_store().sync(envelopes, float(p["lease_s"]))
        return {"acks": acks}

    register_rpc(app, "creds_sync", creds_sync)

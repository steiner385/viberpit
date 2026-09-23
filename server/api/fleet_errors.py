"""The one HTTP reading of a ``FleetRouter`` dispatch refusal.

``FleetRouter.pick`` refuses a dispatch with three exceptions
(``UnknownHost``, ``NoEngineHostAvailable``, ``NoHostAvailable``), and every
route that calls ``orch.create_session`` can receive all three the moment the
edge runs a ``FleetOrchestrator``. They are ROUTINE conditions — a daemon
restart window, a fleet at capacity, a stale host preference — so a route that
does not map them returns an unhandled 500 and the UI says "internal server
error" instead of why (#2184: the assistant drawer did exactly that).

Two routes triage them today and a third would have been a third copy, so the
mapping lives here once. The ordering below is the load-bearing part:
``NoEngineHostAvailable`` subclasses ``NoHostAvailable``, and checking the
general case first turns "install the Codex binary" into "no host available",
which sends an operator to look at host health instead.
"""
from __future__ import annotations

from fastapi import status
from fastapi.responses import JSONResponse

from server.sessions.fleet_router import (
    NoEngineHostAvailable,
    NoHostAvailable,
    UnknownHost,
)

__all__ = ["FleetDispatchRefused", "fleet_refusal_response"]

#: Every refusal ``FleetRouter.pick`` raises, as one catchable tuple so a caller
#: cannot catch two of the three and leave the last one escaping as a 500.
FleetDispatchRefused = (UnknownHost, NoHostAvailable)


def fleet_refusal_response(exc: UnknownHost | NoHostAvailable) -> JSONResponse:
    """The typed error envelope for a router refusal.

    400 for ``UnknownHost`` (the caller named a host that does not exist — a bad
    request), 409 for the rest (the fleet is real and simply cannot take this
    session right now). Total over ``FleetDispatchRefused``: every branch
    returns, so no caller has to handle a ``None``.
    """
    if isinstance(exc, UnknownHost):
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={"error": {"code": "unknown_host", "message": str(exc)}},
        )
    if isinstance(exc, NoEngineHostAvailable):
        # Checked BEFORE NoHostAvailable, which it subclasses. The fleet is
        # healthy and simply cannot run this engine; `hostReasons` names which
        # runner to fix rather than sending an operator to check host health.
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={
                "error": {
                    "code": "no_engine_host_available",
                    "message": str(exc),
                    "engine": exc.engine.value,
                    "hostReasons": exc.reasons,
                },
            },
        )
    return JSONResponse(
        status_code=status.HTTP_409_CONFLICT,
        content={"error": {"code": "no_host_available", "message": str(exc)}},
    )

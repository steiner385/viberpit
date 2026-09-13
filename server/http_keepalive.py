"""The ONE keep-alive invariant for every in-fleet httpx-pool -> uvicorn hop.

An httpx pool hands an idle connection back out for up to ``keepalive_expiry``
seconds; uvicorn closes an idle connection after ``timeout_keep_alive`` seconds.
Both default to 5 s. Two equal timers started at slightly different instants
(the server's after it sends the response, the client's after it receives it)
race, and the loser is a request written onto a socket the server is closing at
that moment: ``httpx.RemoteProtocolError: Server disconnected without sending a
response``, from a server that is perfectly healthy. A poller on a ~5 s cadence
sits exactly on that boundary, which is why it recurred several times a day on
the edge->rocky daemon hop and Error Triage filed it as a daemon outage (#1614).

The invariant is ``CLIENT_KEEPALIVE_EXPIRY_S < SERVER_KEEPALIVE_TIMEOUT_S`` with
margin. It is defined ONCE here and read by both sides of every hop — the edge's
daemon client and the daemon's uvicorn, the edge's deploy-controller client and
the controller's uvicorn — so no pair can drift back into a tie.

The client side stays short on purpose: an idle connection to a daemon that has
since restarted must be discarded, not handed out. The server side simply has to
outlive it.
"""
from __future__ import annotations

# httpx's own default, made explicit and shared.
CLIENT_KEEPALIVE_EXPIRY_S = 5.0

# Well above the client's expiry so the client always retires a connection first.
SERVER_KEEPALIVE_TIMEOUT_S = int(CLIENT_KEEPALIVE_EXPIRY_S * 6)

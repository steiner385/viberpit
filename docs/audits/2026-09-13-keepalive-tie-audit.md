# Fix pattern: equal keep-alive timers on an in-fleet HTTP hop (2026-09-13)

Captured while working #1614 ("rocky daemon unreachable"). The daemon was healthy;
the errors came from two timers of equal length.

## Symptom

`httpx.RemoteProtocolError: Server disconnected without sending a response` from a
server that is idle and healthy, a few times a day, with nothing in the server's
request log for the failed call. The edge's daemon client maps it to
`DaemonUnavailable(outcome_unknown=True)`, the fleet layer logs it as "host
unreachable", and Error Triage files the aggregate as an outage.

## Anti-pattern

An `httpx` pool whose `keepalive_expiry` equals the `timeout_keep_alive` of the
uvicorn on the other end. Both default to 5 s. The server starts its idle timer
when it finishes sending a response; the client starts its own when it finishes
receiving it, a round-trip later. At the boundary the client still considers the
connection fresh while the server is already closing it, and the next request is
written onto a dying socket. Any poller on a ~5 s cadence lands on that boundary
repeatedly.

## Fix shape

The invariant is `client keepalive_expiry < server timeout_keep_alive`, with margin.
Define it ONCE (`server/http_keepalive.py`: client 5 s, server 30 s) and read it
from both sides of every hop. Pin it in a test that reads the values back off the
live objects (the pool's `_keepalive_expiry`, the `uvicorn.run` kwargs), not just
the constants, so a constant nobody passes cannot pass for a fix.

The client side stays short on purpose: an idle connection to a daemon that has
since restarted must be discarded, not handed out. Only the server needs to move.

## Where to look

Fixed in the #1614 PR:

- edge daemon client (`server/ipc/client.py`) → daemon uvicorn (`server/ipc/serve.py`)
- edge deploy client (`server/deploy/client.py`) → controller uvicorn (`server/deploy/serve.py`);
  the UI polls this hop every 5 s and `server/api/deploy.py` already apologised for
  the "transient blip" it produced

Still on the default tie (no 5 s poller today, so not producing errors):

- assistant loopback client (`server/assistant/client.py`, default limits) → edge
  uvicorn (`deploy/viberpit.service`, CLI-launched, no `--timeout-keep-alive`)

Anything new that pairs an `httpx.AsyncClient`/`httpx.Client` with a uvicorn this
repo runs belongs on the shared constants. The sweep query:

```bash
grep -rnE "httpx\.(AsyncClient|Client)\(" server/ --include=*.py | grep -v tests
grep -rnE "uvicorn\.run\(|--timeout-keep-alive" server/ deploy/
```

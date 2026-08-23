# Spike: which edge client leaks sockets into `CLOSE-WAIT`?

**Date:** 2026-08-23
**Branch:** `fix/p3-pool-resilience`
**Status:** resolved — verdict below is backed by a full reproduction of the field failure.
**Amended 2026-08-23 (review of `fix/p3-pool-resilience`):** the *verdict* stands unchanged; the
*explanation* of where inside httpcore the leak happens was overstated and has been downgraded. See
[Where inside httpcore — NOT established](#where-inside-httpcore--not-established).

---

## Verdict

**httpx.** The `httpx.AsyncClient` pool in `DaemonClient` leaks both sockets *and* pool slots
whenever an in-flight RPC is **cancelled** (not merely timed out). The websockets reconnect in
`_reader` is **exonerated** — websockets 16.0 leaks nothing in any of the three failure modes tested.

This is an *observational* verdict, and that is the level of confidence to carry forward. The
controlled experiment (§3) held server, client and stall identical and varied only cancel-vs-timeout:
cancellation leaked, httpx's own timeout leaked nothing. What the spike did **not** establish is the
code path inside httpcore that produces the leak — the first version of this document named one, and
that claim does not survive a read of httpcore 1.0.9. Anything downstream that reasons *forward* from
a specific httpcore line (e.g. "so the unix branch is safe") is unsupported.

Task 2 (fix the pool) is the real work. Task 3 (restart recovery) should not assume the WS reconnect
holds sockets; it does not.

### Installed versions the verdict is grounded in

```
httpx       0.28.1
httpcore    1.0.9
anyio       4.14.0          <- httpcore's async backend under asyncio
websockets  16.0
Python      3.12
```

`httpx.DEFAULT_LIMITS` = `Limits(max_connections=100, max_keepalive_connections=20,
keepalive_expiry=5.0)`. `DaemonClient` passes no `limits=`, so **max_connections is 100** — that is
the number the wedge fires at. `httpx.Timeout(30.0)` (the scalar in `DaemonClient.__init__`) sets
connect = read = write = **pool = 30.0 s**; the pool leg is what produces the "503 after exactly
30 s".

---

## The mechanism

`server/api/sessions.py:_enrich_one` and `server/api/accounts.py:_safe_usage` fan out daemon RPCs
concurrently and bound each one with `asyncio.wait_for`:

```python
# server/api/sessions.py:278-283  — 3 RPCs per session, all sessions in parallel
_raw = await asyncio.gather(
    _resolve_repo(record),
    asyncio.wait_for(orch.latest_suggestions(record.id), timeout),   # ENRICH_FETCH_TIMEOUT = 3.0
    asyncio.wait_for(_resolve_prs(orch, record), timeout),
    asyncio.wait_for(_resolve_summary(orch, record, viewing), timeout),
    return_exceptions=True,
)

# server/api/accounts.py:114 + 130 — one per account, all in parallel
results = await asyncio.gather(*(_safe_usage(orch, n, timeout) for n in names))
```

`asyncio.wait_for` does not "time out" the HTTP request — it **cancels** it. A `CancelledError` is
thrown into httpcore mid-request. Two distinct *observed outcomes* follow: leaked file descriptors,
and pool slots that are never reclaimed. They are separate effects with separate wire fingerprints
(§1), and — per the amendment — separate levels of confidence about their cause.

### Leak 1 — cancel during the TLS handshake: the raw socket is orphaned

**Status: fd leak observed and reproduced (§1 group B, §3 case A); the `start_tls` attribution below
is a plausible candidate, not a proven one.** It does explain an orphaned *descriptor*: nothing in
that path closes the already-connected TCP stream on a `BaseException`. It does **not** explain a
stranded *pool slot* — see Leak 2.

`httpcore/_backends/anyio.py::AnyIOStream.start_tls`:

```python
        with map_exceptions(exc_map):
            try:
                with anyio.fail_after(timeout):
                    ssl_stream = await anyio.streams.tls.TLSStream.wrap(...)
            except Exception as exc:          # <-- Exception, NOT BaseException
                await self.aclose()
                raise exc
```

`asyncio.CancelledError` is a `BaseException` in Python 3.8+, so it slips past this handler and the
already-connected TCP stream is never closed. `anyio.streams.tls.TLSStream.wrap` (anyio 4.14.0) also
does *not* close `transport_stream` when `do_handshake` fails — it delegates that to the caller, and
the caller only handles `Exception`.

`httpcore/_async/connection.py::_connect` does not clean up either — its only handler is
`except (ConnectError, ConnectTimeout)`, and `handle_async_request`'s `except BaseException` merely
sets `self._connect_failed = True` and re-raises; `aclose()` is a no-op while `self._connection is
None`.

The socket survives because the event loop's selector holds a hard reference to the asyncio
transport, so it is never garbage-collected. It sits `ESTABLISHED` until the daemon FINs it, then
parks in `CLOSE-WAIT` forever.

Note this is specifically the *cancellation* path. httpx's own connect timeout raises `TimeoutError`
(an `Exception`), is caught, and closes cleanly — see control A2 below.

### Leak 2 — cancel during the request/response: the pool slot is stranded too

**Status: the end state is reproduced and is what the fix targets; the *route* to it is a candidate.**

The reproduced end state (§2) is a pooled connection that is unusable and unevictable:

```
connect_failed=False is_closed=False has_expired=False is_idle=False is_available=False
```

`AsyncConnectionPool._assign_requests_to_connections` only evicts connections that are `is_closed()`,
`has_expired()`, or surplus-idle, so a connection in that state is **never evicted and never closed**.
It holds one socket and one of the `max_connections` slots for the life of the process. That is the
fact a fix has to address, and `_recycle_transport` addresses it regardless of how the connection
got there.

### Where inside httpcore — NOT established

The original text asserted that the stranded slot is an `AsyncHTTP11Connection` left "permanently
ACTIVE" by a cancel landing mid-request, and traced it to the `except Exception` in `start_tls`.
**Neither claim survives a read of httpcore 1.0.9**, and the review of `fix/p3-pool-resilience`
caught it:

- A cancel *anywhere inside `_connect`* is caught by `AsyncHTTPConnection.handle_async_request`'s
  `except BaseException: self._connect_failed = True` (`_async/connection.py:98`). With
  `_connect_failed` set and `self._connection is None`, `is_closed()`, `has_expired()` and
  `is_idle()` all return **True** (`connection.py:189-201`) — the connection is evictable on the
  next `_assign_requests_to_connections`. Verified on the installed version:

  ```
  never-dispatched: closed False expired False idle False available False
  connect_failed:   closed True  expired True  idle True  available False
  ```

- So `start_tls` cannot be the pool-slot story. It can still be the *descriptor* story: the fd is
  orphaned even though the pool has already given up on the connection object. That is consistent
  with §1, where the 81 sockets far exceeded anything the pool was holding.

The one shape that **does** survive source reading is a connection object the pool created but
**never dispatched on**: `_connection is None` *and* `_connect_failed is False`, so `is_closed()`,
`has_expired()`, `is_idle()` and `is_available()` are all False — permanently occupying a slot and
counted `inUse` by `pool_stats()`. The pool's own
`except BaseException:` handler (`connection_pool.py:246-254`) removes the cancelled *request* from
`_requests` and re-assigns, but leaves that connection object in `_connections`, where nothing will
ever evict it. The window is between `_assign_requests_to_connections` creating it and
`connection.handle_async_request` being entered.

**The consequence that matters for the fix.** The `start_tls` story implies the unix-socket branch
cannot strand, because there is no TLS handshake on it. **That does not follow.** The
never-dispatched shape involves no handshake at all — it is a pool bookkeeping state, identical on
`uds=` and on TLS. Nothing in the code or the tests may assume the unix branch is exempt.

Two measurements taken during the amendment, over `uds=` with no TLS anywhere
(httpx 0.28.1 / httpcore 1.0.9):

| probe | result |
|---|---|
| 3 requests cancelled mid-request against a never-answering unix server | server read EOF on all 3; `pool._connections == 0` — **no leak** |
| 8 waves × 60 concurrent `wait_for(post, ~0.05–0.3 s)` against a 0.3 s unix server, pool 20 | `never-dispatched == 0` in every wave — **not reproduced** |

Read these correctly. The first is *further evidence against* the "cancel mid-request leaves the
`AsyncHTTP11Connection` ACTIVE" story — over unix that path cleans up. Neither is evidence that the
unix branch is exempt: the second probe simply failed to hit the window, and absence of a
reproduction in a bounded attempt is not an exemption. The rule stands for both branches.

### The wedge

Once `len(pool._connections) == max_connections`, every new request queues and waits
`pool_request.wait_for_connection(timeout=pool)` → `PoolTimeout` at **exactly 30 s** →
`httpx.PoolTimeout` (a `TimeoutException`) → `DaemonUnavailable` (`server/ipc/client.py:140`) → 503.

Routes that need no RPC (`/api/health`, `/api/fleet/hosts`) never touch the pool, so they keep
answering in ~10 ms. A fresh connection to the same daemon works in 0.2 s because the daemon is
fine — only this pool is poisoned. Nothing reaps the stranded connections, so only
`systemctl restart ccswitch.service` clears it. Every symptom in the incident report is accounted
for.

---

## Evidence

### 1. Live production observation (read-only, nothing restarted)

Edge `uvicorn server.app:app` pid 945222, started 10:50:48, observed 11:07–11:15. All sockets to
rocky (`100.69.70.109:9443`) belong to that pid:

```
 81 CLOSE-WAIT      2 ESTAB      2 TIME-WAIT
```

Stable across a 48 s sample — accumulated in bursts, not growing continuously. Per-socket TCP
counters (`ss -tanie`) split the 81 into two clean fingerprints:

| group | n | bytes_sent | bytes_received | segs out/in | Recv-Q | reading |
|---|---|---|---|---|---|---|
| A | 19 | 2713–2736 | 4313–4321 | 13 / 13 | 25 | full mTLS handshake + a request and its response; the trailing 25 bytes are the daemon's unread TLS `close_notify` |
| B | 61 | 517 | 2221 | 6 / 5 | 0 | ClientHello sent, server flight received, **client cert flight never sent** — the handshake was abandoned mid-flight |

Group B is the exact wire signature of Leak 1. Group A is Leak 2.

**Burst timing rules out the WS reconnect outright.** Within group B, 51 sockets have `lastsnd`
values spanning **15 ms**, and another 10 span 12 ms; group A's 18 span 34 ms. `_reader` is a serial
`while True:` loop with a 0.05→2.0 s backoff — it cannot open 51 connections in 15 ms. Only a
`asyncio.gather` fan-out through one pool can. 51 = 3 RPCs × 17 sessions, the shape `_enrich_one`
produces.

**The journal corroborates it.** Since the 10:50 restart the edge logged **zero** `ipc: events WS
error` lines, and nothing at all at WARNING or above:

```
$ journalctl -u ccswitch.service --since "2026-08-23 10:45:00" | grep -c "events WS error"
0
$ journalctl -u ccswitch.service --since "2026-08-23 10:45:00" -p warning
-- No entries --
```

The WS reader never failed a handshake, yet 81 sockets leaked. It cannot be the source.

### 2. Full reproduction of the field failure (`/tmp/leakspike/wedge.py`)

One long-lived `httpx.AsyncClient` against a local TLS endpoint fronted by a delaying TCP relay
(40 ms/hop, so handshakes straddle the cancel deadline), waves of 51 concurrent
`asyncio.wait_for(client.get(...), 0.15)` — the same shape as `_enrich_one`:

```
relay delay 40ms/hop, max_connections=100, wait_for timeout 0.15s
  wave 1: leaked client sockets={'ESTAB': 51}   pool._connections=51
          51 x conn=AsyncHTTP11Connection connect_failed=False is_closed=False
                 has_expired=False is_idle=False is_available=False
  wave 2: leaked client sockets={'ESTAB': 100}  pool._connections=100
  wave 3-6: unchanged at 100 — nothing is ever reclaimed
  follow-up on the SAME client, endpoint now fast: PoolTimeout: after 30.10s
  final leaked client sockets: {'ESTAB': 100}   pool._connections=100
  after client.aclose(): {}
```

That is the incident, end to end: leaked sockets, a saturated pool, **`PoolTimeout` after exactly
30 s against a healthy endpoint**, and recovery only on client teardown (= process restart).

An earlier variant confirmed the "fresh connection works" half: while the poisoned client
`PoolTimeout`s, a brand-new `AsyncClient` to the same server returns `200` in 0.00 s.

### 3. Isolating the cancellation as the cause (`/tmp/leakspike/repro.py`)

| case | what happens | our client sockets afterwards |
|---|---|---|
| A — 10 requests **cancelled** mid-TLS-handshake | `CancelledError` into httpcore | **10 still ESTABLISHED — leaked** |
| A2 — 10 requests killed by **httpx's own** connect timeout | `TimeoutError` → `except Exception` fires | **0 — all closed cleanly** |

Same server, same client, same stall. The only variable is cancel-vs-timeout. **This is the
load-bearing experiment in the whole spike**, and it is what the fix rests on: it establishes *that*
cancelling an in-flight RPC leaks and that handing the deadline to httpx does not. It does not
identify the handler responsible — the original text called it "the `AnyIOStream.start_tls` handler,
isolated", which reads more than the experiment supports (the case was run over TLS, so a
TLS-independent path would produce the same table).

### 4. websockets 16.0 control — clean in every mode (`/tmp/leakspike/wscontrol.py`)

```
(a) 15 wss handshakes stalled in the TLS phase          -> our client sockets: {}
(b) 15 wss handshakes stalled in the HTTP-upgrade phase -> our client sockets: {}
(c) 15 wss connects cancelled externally mid-handshake  -> our client sockets: {}
```

Zero leaks. The source explains why — `websockets/asyncio/client.py::__await_impl__` aborts the
transport on **both** the exception and the cancellation path:

```python
                    except asyncio.CancelledError:
                        self.connection.transport.abort()
                        raise
                    except Exception as exc:
                        self.connection.transport.abort()
```

and a cancel during `loop.create_connection` is handled by asyncio itself
(`_create_connection_transport` does `try: await waiter / except: transport.close()`).

Two corrections to the assumptions in the brief, both checked against the installed source:

- `websockets.connect` in 16.0 has **`open_timeout: float | None = 10`** — the default is 10 s, not
  `None`. Not passing `open_timeout` does not mean "no timeout"; the observed
  `timed out during opening handshake` message is that default firing.
- A handshake timeout raising inside `__aenter__` (so `__aexit__` never runs) is real, but it is
  *not* a leak: `__await_impl__` aborts the transport before the exception escapes. Adding
  `open_timeout` to `daemon_ws_connect` would be a tuning change, not a leak fix.

---

## What this does *not* establish

- **The code path inside httpcore that strands a pool slot.** Not the micro-window, the path itself.
  The original attribution (`start_tls`'s `except Exception`) is refuted above; the never-dispatched
  connection is a candidate that survives source reading but was never instrumented in the repro.
  The observable end state — a connection that reports neither closed, expired, idle nor available,
  and is therefore never evicted — is reproducible and is what a fix has to address.
- **That the leak is TLS-specific.** It is not known to be, and one candidate mechanism is
  transport-agnostic. Treat the unix-socket branch as equally exposed until someone shows otherwise.
- I did not restart production to sample, so the two historical incidents (17 and 59 sockets) were
  not re-measured directly — but 17 and 59 sit on the same accumulation curve as today's 81, and
  the mechanism predicts the wedge fires when total pooled connections reach `max_connections`.
- Production was at 81/100 and still healthy while this was written. That is a falsifiable
  prediction: it wedges at 100, not at some other number.

---

## What Task 2 has to fix (all in `server/ipc/client.py`)

1. **Pass explicit `httpx.Limits`.** `max_connections=100` is far more than one daemon needs, and
   the scalar `timeout=30.0` silently makes the pool-wait leg 30 s — which is the "503 after exactly
   30 s". Use an explicit `httpx.Timeout(connect=…, read=…, write=…, pool=…)` so the pool leg is
   short and distinguishable. *(Amended: "short" must still mean **longer than the slowest expected
   RPC** — `transcript_events` was measured at 4.3 s here. A pool leg below that sheds queued
   requests the pool would have served, and makes ordinary fan-out look like a wedge.)*
2. **Stop cancelling in-flight RPCs.** `asyncio.wait_for` around `orch.*` is the trigger. Pass the
   deadline to httpx instead (`DaemonClient.call` already accepts `timeout=`), so the failure is an
   `httpx.TimeoutException` — the path httpcore actually cleans up — rather than a `CancelledError`.
   Call sites: `server/api/sessions.py:279-282`, `server/api/accounts.py:114`.
3. **Make recovery possible without a restart.** Even with 1 and 2, a stranded connection is
   unreclaimable from inside the pool, in either shape. The edge needs a way to rebuild the transport when the pool
   stops yielding connections (and, per Task 3, on a detected daemon epoch change).

The websockets reconnect needs no leak fix.

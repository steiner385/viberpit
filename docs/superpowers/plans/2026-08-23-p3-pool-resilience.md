# P3 — Edge Pool Resilience Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop the edge wedging itself against the session daemon, and stop every deploy from causing it.

**Architecture:** The edge holds two long-lived connections to the daemon: an httpx pool for RPCs and a websockets client for `/events`. When the daemon restarts, both are severed at once. Something is not closing the remains — 59 sockets were observed in `CLOSE-WAIT` — and afterwards every route needing an RPC blocks for the full 30 s timeout and 503s, while no-RPC routes stay instant. Only `systemctl restart ccswitch.service` clears it. We identify which client leaks, bound both properly, and recycle the pool on the epoch change the client *already detects*, so a daemon restart heals instead of wedging.

**Tech Stack:** Python 3.12, httpx, websockets, asyncio, pytest / pytest-asyncio.

**Spec:** `docs/superpowers/specs/2026-08-22-fleet-recovery-design.md` — root cause **RC5**.

## Global Constraints

- Tests: `timeout 300 env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 .venv/bin/python -m pytest <paths> -v`
- **Run everything in the foreground with an explicit `timeout N`.** Never background a command —
  implementers in earlier plans stalled waiting on a notification that never reaches a subagent.
- **Do not run the full `server/tests/` suite** (~12 min); the controller runs it.
- `server/ipc/client.py` is the **single source of truth** for edge→daemon connections
  (`daemon_ws_connect` says so). Do not add a second connection path.
- No new model, so no Alembic revision. If a task needs a column, stop and report.
- Every commit message ends with `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`.

## Observed evidence (live fleet, 2026-08-22 and 2026-08-23)

| | |
|---|---|
| occurrences | **twice in two days**, both immediately after a daemon restart |
| sockets in `CLOSE-WAIT` to rocky | 17 (first), **59** (second) |
| routes needing an RPC | 503 after exactly 30 s |
| routes not needing an RPC (`/api/health`, `/api/fleet/hosts`) | 200 in ~10 ms |
| **a fresh connection to the same daemon** | **200 in 0.2 s** |
| recovery | only `systemctl restart ccswitch.service` |

The daemon is healthy throughout. This is purely an edge-side connection-lifecycle defect.

**The loop that makes it urgent:** deploying restarts the daemon, which wedges the edge, which
requires a manual restart — so shipping any fix breaks the app until someone intervenes.

## Two candidate leaks — Task 1 decides between them

1. **httpx pool.** `DaemonClient.__init__` builds `httpx.AsyncClient(..., timeout=30.0)` with **no
   `httpx.Limits`** and a single scalar timeout (which sets connect/read/write/**pool** alike). Dead
   keepalive connections may be retained and handed out.
2. **websockets reconnect.** `_reader` reconnects on a 0.05→2.0 s backoff. The observed failure is
   `timed out during opening handshake` — which raises inside `__aenter__`, so the
   `async with daemon_ws_connect(...)` body is never entered and `__aexit__` never runs. A socket
   opened during that failed handshake may never be closed. `websockets.connect` is called with **no
   `open_timeout`**.

At a 2 s backoff cap, a sustained handshake failure would leak ~30 sockets/minute — which matches
59 accumulating.

---

### Task 1: Identify which client leaks the sockets

**This task is a spike. It writes no production code.** Tasks 2 and 3 fix different clients; picking
wrong means fixing something that was never broken while leaving the real leak in place.

**Files:**
- Create: `docs/superpowers/spikes/2026-08-23-edge-socket-leak.md`
- Modify: none

**Interfaces:**
- Consumes: nothing.
- Produces: a recorded verdict — httpx pool, websockets reconnect, or both — with evidence. Task 2
  and Task 3 read it.

- [ ] **Step 1: Attribute the current sockets**

If the edge is currently wedged, attribute the sockets before clearing it:

```bash
EDGE=$(pgrep -f "uvicorn server.app:app" | head -1)
ss -tnp 2>/dev/null | grep 100.69.70.109 | awk '{print $1}' | sort | uniq -c
ls -l /proc/$EDGE/fd 2>/dev/null | grep -c socket
```

A socket's local port tells you nothing about which library owns it, so correlate by **behaviour**
in Step 2 rather than trying to read ownership off the fd.

- [ ] **Step 2: Reproduce deliberately and watch which count grows**

Restarting the daemon severs both clients at once, which is why the field evidence is ambiguous.
Separate them:

- **Isolate the WS:** with the daemon healthy, make the WS handshake fail while leaving HTTP intact
  (e.g. point the WS at a path the daemon does not serve, or block the handshake), then watch
  `CLOSE-WAIT` while `ipc: events WS error` lines accumulate in the edge journal. If sockets grow
  ~1 per reconnect, the WS path leaks.
- **Isolate the pool:** with the WS reader stopped, issue RPCs, restart the daemon, then issue more
  RPCs and watch the count.

Prefer doing this against a **scratch daemon** rather than production rocky. If that is not
practical, say so and use production read-only observation instead — do not restart production
repeatedly to gather samples.

- [ ] **Step 3: Check what the installed library versions actually do**

```bash
.venv/bin/python -c "import httpx, websockets; print('httpx', httpx.__version__); print('websockets', websockets.__version__)"
```

Read the installed `websockets` connect implementation for whether a handshake timeout closes the
underlying transport, and the installed `httpx` pool for its keepalive-expiry and dead-connection
handling. **Version matters more than general knowledge here** — do not reason from how these
libraries behaved in some other release.

- [ ] **Step 4: Write the finding**

Create `docs/superpowers/spikes/2026-08-23-edge-socket-leak.md` recording: which client leaks, the
evidence, the installed versions, and whether the second client is also implicated. End with an
explicit verdict — **httpx / websockets / both** — because Task 2 fixes the pool and Task 3 fixes
restart-recovery, and a wrong verdict sends both astray.

If the evidence is ambiguous, say so plainly rather than picking the tidier answer.

- [ ] **Step 5: Commit**

```bash
git add docs/superpowers/spikes/2026-08-23-edge-socket-leak.md
git commit -m "docs: spike — which edge client leaks sockets into CLOSE-WAIT?

The edge wedged twice in two days after a daemon restart, with 17 then 59
sockets stranded in CLOSE-WAIT while a fresh connection to the same daemon
answered in 0.2s. Two candidates — the unbounded httpx pool and the
websockets reconnect whose handshake timeout raises inside __aenter__.
Records which one, with evidence.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 2: Bound the httpx client properly

Regardless of Task 1's verdict, the current client is under-specified: `timeout=30.0` is a scalar
that sets connect, read, write **and pool** alike, and there are no `httpx.Limits` at all. A pool
acquisition therefore waits the same 30 s as a slow read, which is exactly the observed symptom.

**Files:**
- Modify: `server/ipc/client.py` — both `AsyncClient` constructions (unix and TLS branches)
- Test: `server/tests/test_ipc_client_limits.py` (create)

**Interfaces:**
- Consumes: Task 1's verdict (informs whether this is the primary fix or defence in depth).
- Produces: `DaemonClient` constructs its `AsyncClient` with explicit `httpx.Limits` and a granular
  `httpx.Timeout`. Task 3 relies on the client exposing a way to recycle its transport.

- [ ] **Step 1: Write the failing test**

Create `server/tests/test_ipc_client_limits.py` asserting, by inspecting the constructed client:

1. the timeout is granular, not scalar — **pool acquisition must not wait 30 s**; a caller starved of
   a connection should fail fast rather than tie up a request for the full read budget;
2. `limits` are set explicitly (`max_connections`, `max_keepalive_connections`) rather than left to
   the library default;
3. a bounded `keepalive_expiry`, so an idle dead connection cannot linger indefinitely;
4. both the unix and TLS branches get the same treatment — the unix branch is used in tests and by a
   local daemon, and leaving it unbounded would hide regressions.

Read how the existing IPC tests construct a `DaemonClient` (`ls server/tests/ | grep -i ipc`) and
follow that pattern rather than inventing one.

- [ ] **Step 2: Run test to verify it fails**

Run: `timeout 300 env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 .venv/bin/python -m pytest server/tests/test_ipc_client_limits.py -v`

Expected: FAIL — the client currently has `timeout=30.0` and no limits.

- [ ] **Step 3: Implement**

Replace the scalar timeout with an explicit `httpx.Timeout(connect=…, read=…, write=…, pool=…)` and
add `httpx.Limits(...)`. Choose the values deliberately and justify each in a comment:

- **pool** must be short. Waiting 30 s for a connection slot is how one wedged pool turned into
  30 s 503s on every route. Fail fast instead.
- **read** may stay generous — some RPCs legitimately take seconds (`transcript_events` was measured
  at 4.3 s).
- **connect** should account for the relayed link (~200 ms RPCs today) without being unbounded.
- **keepalive_expiry** short enough that a dead idle connection is discarded rather than handed out.

Apply identical settings to both branches. Do not duplicate the literal values — define them once as
module constants so the two constructions cannot drift.

- [ ] **Step 4: Run test to verify it passes**

Run the same command. Expected: PASS.

- [ ] **Step 5: Run the IPC suites**

```bash
timeout 600 env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 .venv/bin/python -m pytest \
  $(ls server/tests/test_ipc*.py | tr '\n' ' ') -v 2>&1 | tail -25
```

Name what you ran in your report.

- [ ] **Step 6: Commit**

```bash
git add server/ipc/client.py server/tests/test_ipc_client_limits.py
git commit -m "fix(ipc): bound the daemon client's pool and timeouts

The edge client used a single scalar timeout=30.0 — which sets connect, read,
write AND pool alike — with no httpx.Limits. So a request that could not get a
pool slot waited the full 30s before failing, which is exactly what every
daemon-backed route did while the pool held dead connections.

Granular timeouts with a short pool budget, explicit limits, and a bounded
keepalive expiry.

Refs #734

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 3: Recycle the pool when the daemon restarts

This is the task that breaks the deploy→wedge loop. `_reconcile_epoch` (`client.py:222`) **already
detects a daemon restart** — it fetches `/info`, compares the epoch, and clears per-stream
watermarks because the daemon's seq space reset. At that exact moment every pooled HTTP connection
is also dead, because they point at a process that no longer exists.

**Files:**
- Modify: `server/ipc/client.py` — `_reconcile_epoch`, plus a transport-recycle helper
- Test: `server/tests/test_ipc_epoch_recycles_pool.py` (create)

**Interfaces:**
- Consumes: Task 2's bounded client.
- Produces: on a detected epoch change, the client's HTTP transport is recycled so no connection to
  the dead process is reused.

- [ ] **Step 1: Write the failing test**

Create `server/tests/test_ipc_epoch_recycles_pool.py` covering:

1. an epoch change recycles the transport — the client does not keep serving RPCs over connections
   opened to the previous daemon process;
2. an **unchanged** epoch does *not* recycle — routine reconnects must not churn the pool;
3. recycling does not lose in-flight state the client needs (subscriptions are re-established by the
   existing reconnect path — assert that path still runs);
4. a failure while recycling does not leave the client permanently unusable.

`_reconcile_epoch` already returns early when `/info` is unreachable — preserve that, and add a test
that an unreachable `/info` neither recycles nor raises.

- [ ] **Step 2: Run test to verify it fails**

Run: `timeout 300 env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 .venv/bin/python -m pytest server/tests/test_ipc_epoch_recycles_pool.py -v`

Expected: FAIL — nothing recycles the transport today.

- [ ] **Step 3: Implement**

In `_reconcile_epoch`, where the epoch is found to have changed, recycle the HTTP transport before
(or alongside) clearing the watermarks.

Be careful about **how** you recycle. Closing an `AsyncClient` that has in-flight requests will
error those requests. Prefer closing the transport/pool so idle dead connections are discarded while
in-flight requests are allowed to fail on their own bounded timeouts — and say in a comment which
you chose and why. Do not build a new `AsyncClient` and swap it in unless you can show that
concurrent callers holding the old reference are handled.

**Do not** recycle on every reconnect — only on a *changed* epoch. Reconnects are routine (36 in
40 minutes were observed during relay instability); churning the pool that often would be its own
performance problem.

- [ ] **Step 4: Run test to verify it passes**

Run the same command. Expected: PASS, all four cases.

- [ ] **Step 5: Run the IPC + WS suites**

```bash
timeout 600 env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 .venv/bin/python -m pytest \
  $(ls server/tests/test_ipc*.py | tr '\n' ' ') $(ls server/tests/test_ws*.py | tr '\n' ' ') -v 2>&1 | tail -25
```

- [ ] **Step 6: Commit**

```bash
git add server/ipc/client.py server/tests/test_ipc_epoch_recycles_pool.py
git commit -m "fix(ipc): recycle the HTTP pool when the daemon epoch changes

A daemon restart kills every pooled connection the edge holds, but nothing
discarded them — 59 were observed stranded in CLOSE-WAIT while a fresh
connection to the same daemon answered in 0.2s. Every RPC-backed route then
503'd until someone restarted the edge by hand, so DEPLOYING A FIX BROKE THE
APP until manual intervention.

_reconcile_epoch already detects the restart to reset seq watermarks. Recycle
the transport at the same point: at a changed epoch every pooled connection
is known-dead by definition.

Refs #734

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 4: Make a wedged pool visible

Both outages were diagnosed by hand with `ss -tn state close-wait`. The system knew it was failing —
every RPC route was 503ing — and said nothing an operator could act on.

**Files:**
- Modify: `server/ipc/client.py` (expose pool stats), and the edge health surface
- Test: `server/tests/test_ipc_pool_health.py` (create)

**Interfaces:**
- Consumes: Tasks 2 and 3.
- Produces: pool state observable without shelling into the box.

- [ ] **Step 1: Write the failing test**

Assert the client exposes its pool state (in-use / idle counts, or the equivalent the installed
httpx version offers), and that the edge surfaces it. Read how `/healthz` and `/api/daemon/health`
are built before choosing where it belongs — extend an existing surface rather than adding a new
endpoint.

- [ ] **Step 2: Run test to verify it fails**

Expected: FAIL — no pool stats are exposed.

- [ ] **Step 3: Implement**

Expose the counts, and log a warning when consecutive RPCs fail with a pool timeout — that is the
signature of the wedge, and it should be legible in the journal without a socket dump.

Do not add an alerting transport: the Python app has none, and
`scripts/ccswitch-health-check.sh` is the out-of-band alerting path.

- [ ] **Step 4: Run test to verify it passes**

- [ ] **Step 5: Run the IPC and health suites**, naming what you ran.

- [ ] **Step 6: Commit**

```bash
git add server/ipc/client.py server/tests/test_ipc_pool_health.py
git commit -m "feat(ipc): surface daemon pool health

Both edge wedges were diagnosed by hand with `ss -tn state close-wait`. The
system knew every RPC route was failing and surfaced nothing actionable.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

## Verification against the live fleet

This is the acceptance check, and it is unusually easy to run because the trigger is reproducible.

- [ ] Deploy, and confirm the edge does **not** wedge afterwards. Baseline to beat: both prior
  deploys left the edge 503ing on every RPC route until manually restarted.
  ```bash
  # immediately after the deploy completes
  ss -tn state close-wait | grep -c 100.69.70.109      # expect ~0, not 17-59
  for p in /api/health /api/sessions /api/accounts; do
    printf "  %-16s " "$p"
    curl -s -o /dev/null -w "%{http_code} %{time_total}s\n" --max-time 40 "http://127.0.0.1:8787$p"
  done
  ```
  Expected: all 200. `/api/sessions` will still be slow (~6 s) — that is the unrelated N+1 in #742,
  not this bug.
- [ ] Restart rocky's daemon directly (`ssh tony@rocky sudo systemctl restart ccswitch-daemon`) and
  confirm the edge recovers **without** a `systemctl restart ccswitch.service`.
- [ ] Confirm the journal shows the epoch-change recycle happening, and no pool-timeout warnings in
  steady state.

**Note:** P3 does not make `/api/sessions` fast — that is #742 (three unbatched RPCs per session over
a 204 ms relayed link). P3's win is that the edge stops needing a manual restart after every deploy.

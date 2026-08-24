# Batched enrichment RPCs — design

**Issue:** #742 · **Date:** 2026-08-23 · **Status:** approved, ready for planning

## Problem

`GET /api/sessions` takes ~6s with 21 sessions. One page load issues **296 RPC calls** to the
session daemon — three per session (`prs_for`, `latest_suggestions`, `latest_summary`), issued
individually rather than batched.

Each call costs ~204ms because dobby↔rocky has no direct Tailscale path and every RPC detours
through a DERP relay. The two hosts are 0.55ms apart on the same subnet.

This is not a regression. Before #737 the route took 10–14s with the daemon's event loop saturated;
RC1 cut that roughly in half. The N+1 is what remains.

## Three findings that shaped the design

**1. The edge cannot bypass the daemon.** All three RPCs read in-memory daemon state —
`orchestrator._records`, `_drivers`, `_pr_poller.cached`. There is no Postgres shortcut. Batching
is the only available fix.

**2. The work being batched is nearly free.** All three are cache reads that return immediately;
heavy generation is already spawned as background tasks (`latest_summary`'s docstring: "Returns the
cached text (or "") immediately — never blocks the /sessions GET"). `prs_for` is a pure in-memory
dict walk. The 296 calls pay 204ms of *network* each for work measured in microseconds.

Two consequences: a 21-session batch costs the daemon almost nothing, and per-session isolation
inside the batch is essentially free.

**3. `viewing` is request-level, not per-session** (`api/sessions.py:409`) — one flag for the whole
page. It rides along once instead of multiplying the batch.

## Decisions

### D1 — Three batched methods, not one combined call

```
prs_for_many            {session_ids: [...]}           -> {prs:         {sid: [PRInfo]}}
latest_suggestions_many {session_ids: [...]}           -> {suggestions: {sid: [str]}}
latest_summary_many     {session_ids: [...], viewing}  -> {summaries:   {sid: str}}
```

The intuitive argument for a single combined `enrich_sessions` is "fewer round trips." It does not
hold: the three calls are **concurrent**, so one RPC and three RPCs both cost ~204ms of wall time.
The 296→3 collapse is where all of the win is; 3→1 buys nothing measurable.

With latency removed from the decision it rests on coupling, and three methods win: each mirrors an
existing singular method one-for-one, stays independently useful and testable, and avoids fusing
three unrelated concerns into one protocol method with a single caller.

### D2 — Per-session isolation lives in the daemon

Each handler fans out with `asyncio.gather(..., return_exceptions=True)` and maps any failure to
that session's empty value. One wedged session empties one field; the other 20 render.

This preserves exactly today's blast radius. The alternative — one deadline for the whole batch —
would turn a single bad session into blank enrichment for the entire page, a real regression.

A session id the daemon does not know simply does not appear in the returned map. The edge treats
absent and empty identically, so no error path is needed for unknown ids.

### D3 — Fall back on version skew, delete later

A new edge calling a batched method against an old daemon receives a clean
`404 {"error": {"type": "UnknownMethod"}}` (`ipc/daemon.py:336`). On that specific response the edge
falls back to the existing per-session path for that call.

This is not hypothetical. The deploy on 2026-08-22 half-failed and left rocky on new code with dobby
on old — precisely this skew — and #740 (the health gate that caused it) is still open.

Consequence: `_enrich_one` survives as the fallback implementation rather than becoming dead code.
It carries a `@deprecated` marker naming its removal condition, and a follow-up issue tracks
deleting it once #740 is fixed and skew is no longer observable. This is the documented exception to
the purge-replaced-code norm, not a violation of it.

## Data flow

`GET /api/sessions` currently fans out `_enrich_one` per record. It will instead:

1. Collect the record ids.
2. Issue the three batch calls **concurrently**.
3. Zip results back onto records; a missing sid resolves to the empty value.

`_resolve_repo` stays per-record — it is local work, not an RPC, and was never part of the problem.

### The deadline must be pushed in, never raced from outside

Each batch call goes through `call_with_deadline` → `DaemonClient.call` → httpx, exactly as the
singular calls do after #746. `asyncio.wait_for` must NOT be reintroduced: it cancels the request,
and a `CancelledError` thrown into httpcore strands the connection's pool slot ACTIVE forever. That
wedged the edge twice in two days (#734). A test guards against its return.

### This closes a limit recorded in #746

Enrichment declares a 3s budget, so its pool leg caps at 3s — under the 4.3s measured for
`transcript_events`. Under concurrent load, 63 RPCs per page saturated the pool and enrichment shed.
At 3 RPCs per page that pressure is gone; the recorded workaround is no longer needed.

## Testing

Each behaviour proved fail-first.

- **RPC count** — N sessions produce 3 calls, not 3N. This is the fix itself and must fail against
  today's code before it passes.
- **Per-session isolation** — one failing session id empties its own field only; the rest enrich.
- **Fallback** — a daemon returning `404 UnknownMethod` still renders a fully enriched page.
- **No `wait_for` regression** — the enrichment path must not race deadlines from outside.
- **`viewing` propagation** — the request-level flag reaches `latest_summary_many` unchanged.

## Expected outcome

296 RPCs → **~27** (3 batched + the ~24 already-unbatched others). ~6s → well under a second.

The relay still costs 204ms per call. This design removes the cost rather than hiding it, so fixing
the DERP path remains independently worthwhile for every other RPC-heavy route.

## Out of scope

The remaining ~24 calls (`list_sessions`, `session_queue_positions`, and others) are a different
shape and are not touched here. Folding them in would blur the diff without sharing a mechanism.

Fixing the DERP relay (the transport amplifier) is tracked separately.

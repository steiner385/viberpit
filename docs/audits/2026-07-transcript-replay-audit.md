# Transcript/Replay Pipeline Audit — July 2026

Audited at `origin/master` @ `e646c4d`. Scope: reliability, accuracy, retention, ordering,
and rendering of the session transcript/replay across claude-agent-sdk → daemon → edge → web UI.

## Why this audit

Seven-plus point fixes have landed in this subsystem (#458 blanked transcripts, #441
compaction-summary-as-user-turn, #429 sub-agent reconstruction, #422 replay-gap resync,
#418 virtualization, #414 poll↔WS merge, #383 dropped turns) and symptoms keep recurring.
That pattern indicates a structural defect, not a series of isolated bugs.

## Architecture (as built)

```
SDK receive_response()
  → Translator.feed()                sessions/translator.py   (SDK msg → ev.* dataclasses)
  → SessionDriver._emit()            sessions/driver.py       (turn buffer + fan-out, unbounded queues)
  → daemon pump                      ipc/orchestrator_rpc.py  (driver.events() → publish)
  → daemon ring                      ipc/daemon.py            (per-stream seq, deque maxlen=2048)
  ── unix socket ──
  → DaemonClient._deliver()          ipc/client.py            (per-subscriber Queue(1000), watermark)
  → RemoteDriver.events()            sessions/remote.py
  → ws._forward_live()               bridge/ws.py             (RE-STAMPS seq per-attach from 0)
  → _batcher → "batch" frame         bridge/ws.py
  ── browser WS ──
  → handleLiveBatch()                web/src/ws/liveBatch.ts  (seq-gap detect → heal)
  → reducer (arrival order)          web/src/session/reducer.ts

REPLAY: attach → replay_window(<claude_session>.jsonl on disk) → replayReset + events (NO seq)
        → replayComplete → live extras → live stream
```

Durable source of truth is the CLI's own JSONL transcript on disk. The DB stores session
metadata only. Live delivery is a chain of in-memory hops, each with its own queueing,
seq-stamping, and drop policy.

## Root cause (structural)

**There is no end-to-end event identity/ordering contract.** The daemon assigns real
per-stream seqs, but the edge discards them and re-stamps a per-attach counter starting
at 0 (`bridge/ws.py:89-91`); replay frames carry no seq at all (`ws.py:151-154`). Consequences:

1. No layer downstream of a loss can *detect* the loss — the edge re-stamps contiguous
   seqs over a stream that already has holes.
2. The replay→live boundary is unverifiable: the client cannot know whether the first
   live event is contiguous with the last replayed one.
3. Every layer compensates with its own heal/dedup heuristic (ring gap frames, edge
   forced resync, client detach+reattach heal, reducer text-match dedup), and these
   interact badly (heal cooldowns, full replayReset blanking, watermarks advancing past
   drops).

The SDK gives stable identities (`uuid` per message, `session_id`) and the daemon already
has a monotonic seq; they are simply not threaded through.

## Verified defects (hand-confirmed, wave 1)

| # | Where | Defect | Effect |
|---|-------|--------|--------|
| V1 | `server/ipc/client.py:189-202` | Subscriber-queue overflow silently drops the oldest queued event; the socket-level watermark (`_last_seq`) has already advanced, so reconnect never re-fetches it and no GAP is emitted for this drop path | Permanent, invisible transcript loss under burst load (most likely cause of "missing content") |
| V2 | `server/sessions/orchestrator.py:4415` + `server/ipc/orchestrator_rpc.py:44-54` | Recovery paths (`_reassign` etc.) swap in a new driver but only the `ensure_driver` RPC restarts the daemon pump — recovery never calls it | After rotation/recompose, attached viewers' transcripts freeze until manual reattach |
| V3 | `web/src/ws/liveBatch.ts:41-46` | Duplicate/stale frame (`seq <= prev`) still dispatched into the reducer before heal; heal is also cooldown-limited (2s) and each heal blanks + rebuilds the transcript | Doubled text/blocks; corruption persists when a second gap lands inside the cooldown |
| V4 | deploy unit + uvicorn defaults | uvicorn's graceful shutdown waits indefinitely on open WebSockets; lifespan shutdown never runs; systemd SIGKILLs at `TimeoutStopSec=90` on **every** deploy (confirmed in journal, dozens of kills Jul 2–4) | Hard client drops mid-frame every deploy; DB/session teardown never executes; 90s stop stall per deploy |

## High-confidence defects requiring design (wave 2)

- **W2.1 Ring eviction of long turns** (`ipc/daemon.py:36,74-88`): a turn emitting >2048
  events evicts its own turn-start; `resume_since` then always predates the ring →
  perpetual gap→resync loop, and the disk replay trims the in-progress turn
  (`drop_incomplete_turn`) → the streaming turn is invisible to reconnecting clients.
- **W2.2 Local-mode attach race** (`bridge/ws.py:465-468`): turn completing between the
  `turn_active` check, disk read, and live subscribe is delivered to neither replay nor
  live.
- **W2.3 No seq on replay frames / per-attach seq reset** (`ws.py:70,90-91,151-154`):
  the structural issue above; fix is to thread the daemon seq end-to-end and tag replay
  with a cursor (replay-to-seq N, live-from-seq N+1), dedup client-side by `(sessionId, seq)`
  and message `uuid`.
- **W2.4 Duplicate user turn on recovery resend** (`orchestrator.py:4259-4266,4451-4453`):
  resend after rate-limit/crash appends a second identical UserMessage to the JSONL —
  permanent duplicate in every future replay. Needs an idempotency marker or trim-aware resend.
- **W2.5 `turn_open` corrupted by transient status** (`ipc/daemon.py:44,150-151`):
  `idle`/terminal status mid-turn breaks the resume anchor → gap or partial turn on reconnect.
- **W2.6 Pump/batcher tasks die silently** (`orchestrator_rpc.py:29,54`, `ws.py:224,243`):
  no done-callbacks; an exception freezes the tail with no restart or log.
- **W2.7 Heal blanks the transcript** (`reducer.ts:573-615`): every heal does a full
  replayReset (clear + rebuild) — should become a cursor-based catch-up once W2.3 lands.
- **W2.8 Orphan toolResult dropped** (`reducer.ts:442-455`); parked results should be
  held for late-arriving toolCall blocks.

Full agent findings (30+ items with confidence ratings) are preserved in the session
notes; lower-confidence items: frontend block-id collisions after permission resolve,
autoscroll swallowing user scroll during heavy streaming, virtualization estimate drift,
rotation rows keyed by index, unbounded client `turns[]`, naive-vs-aware datetime mixing,
fire-and-forget metadata persistence (lost `claude_session_id` orphans the transcript),
mid-conversation orphaned tool_use unrepaired.

## Test-strategy gaps (wave 3)

Every pipeline stage is tested in isolation with a different fixture vocabulary; the
seams — where all seven historical regressions occurred — have zero integrated coverage:

1. No end-to-end test threading one generated SDK stream through driver → daemon ring →
   edge WS → the actual TS reducer.
2. No concurrent multi-client WS fan-out test (two live sockets, one session).
3. No live-event-during-replay race test at the edge; no reconnect-mid-streaming-turn test.
4. Zero property-based tests (no `hypothesis`, no `fast-check`) for the core invariants:
   seq monotonicity, reducer idempotence under duplicate/reordered frames, gap-heal
   correctness for arbitrary drop patterns.
5. `web/e2e/rotation-reconnect.spec.ts` sends `{type:'output'}` frames the client no
   longer handles — the reconnect e2e passes regardless of whether replay works.
6. `FakeSDKClient` yields a pre-built synchronous list — cannot express timing,
   interleaving, duplication, or mid-replay live events.

Required infrastructure: an async fake-SDK stream generator with timing/fault injection;
a multi-client WS harness; a shared wire-frame fixture library consumed by both pytest
and vitest (stops Python/TS fixture drift); `hypothesis` + `fast-check` invariant suites;
e2e assertions on actual transcript content.

## Reference contract (from SDK docs research)

- Dedup identity: message `uuid` (stable across resume/restart); `message_id` for
  streaming replace-by-id; `session_id` for stream scoping.
- Ordering: SDK guarantees FIFO within a query; JSONL file-line order is the durable
  order; no SDK-provided seq — the daemon seq must be the single seq authority.
- Replay pattern: snapshot+cursor (client sends last-seen seq on attach; server replays
  `seq > cursor` then live from there) — eliminates both full re-replays and the
  replay/live boundary ambiguity.
- Never persist/replay StreamEvents; only complete messages. (Already respected.)

## Plan

- **Wave 1 (this change series):** V1–V4 — each an independent, tested PR.
- **Wave 2:** thread daemon seq end-to-end (W2.3) as the enabling change, then W2.1/2/4/5/6/7/8
  become small follow-ons instead of heuristics.
- **Wave 3:** test infrastructure above; add regression coverage as each wave-2 item lands.

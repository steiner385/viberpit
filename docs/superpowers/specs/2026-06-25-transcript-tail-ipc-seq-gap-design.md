# Transcript tail lost across daemon restart (IPC seq-reset gap) — design

**Status:** design / approved (implementation was in progress; reconstructed after worktree loss)
**Date:** 2026-06-25
**Related:** `followup-prompt-dropped-by-replayreset-race` (same replayReset family, user-prompt
variant), `deploy-starvation-busy-gate` (#190 daemon-restarts-before-edge graceful drain),
`remote-mode-path-asymmetry`.

## Problem

In production (remote mode: `ccswitch.service` web ⇄ `ccswitch-daemon.service` over a Unix-socket
`/events` WebSocket), the **tail of a session transcript intermittently goes missing in the live
view**. The user reported: "the last few messages don't appear until I'm able to send a prompt, then
it all comes through at once," and that it **coincides with app upgrades**. A hard reload does not
durably fix it for an actively-producing session.

### Confirmed root cause (reproduced deterministically)

The daemon stamps each live event with a **per-process monotonic `seq`** and keeps a 256-entry
per-stream replay ring (`server/ipc/daemon.py`). On a **daemon restart the `seq` counter resets to 0
and the ring is empty** (fresh process; `app.state.seqs = {}`).

The edge client persists `_last_seq` per stream and, on reconnect, resubscribes with
`since=_last_seq` (`server/ipc/client.py`) — a **stale high-water mark from the previous daemon's seq
space**. The daemon replays on subscribe only where `seq > since` (`server/ipc/daemon.py`). After a
restart, the new daemon's buffered (restart-window) events have small seqs (`0,1,2,…`), all `<
since`, so **they are silently rejected** — a permanent gap.

On the **daemon-only restart path** (graceful-drain deploy, #190: daemon restarts before the edge),
the browser's WebSocket to the *web* process never drops, so the browser never re-replays from disk —
the hole persists until a manual full reload. Higher event volume (autonomous-loop sessions) puts
more events in the gap. The deploy controller polls `origin/master`, so during active development
these gaps recur → the symptom looks "consistent."

A standalone repro (`server/ipc/daemon.py` + `client.py` over a throwaway socket, swapping a fresh
daemon onto the same socket) loses exactly the restart-window events while a post-resubscribe live
event still arrives — and emits the same `received 1012 (service restart)` signature seen in prod.

### What is NOT the cause (ruled out on the live host)

- **Disk transcript loss** — the `claude` binary owns the JSONL; it is complete and current, and
  replay reads the correct DB-pointed file. Disk is the complete recovery source.
- **The daemon "replay buffer … full — dropping oldest event" warning** — benign: it logs on every
  ring-at-capacity append; currently-connected subscribers get direct, lossless sends.
- **Edge subscriber-queue overflow** (`client.py`, maxsize 1000) — zero occurrences in prod logs; a
  connected browser at normal throughput receives every event (verified via instrumented browser WS).

## Goals / non-goals

**Goals:** the live transcript never silently loses events across a daemon restart; recovery is
automatic (no manual reload); the fix is robust to *any* gap source, not just the seq reset.

**Non-goals:** changing how the `claude` binary writes transcripts; changing the deploy/drain
sequence; eliminating the (benign) ring-at-capacity log beyond a clarity tweak.

## Design

Three independent components (independently shippable; together: root-cause fix + defense-in-depth +
safety invariant).

### Component 1 — Epoch-aware resubscribe (root-cause fix), backend

Make the edge client recognize that a reconnect landed on a **different daemon process** and stop
carrying a stale `since` into a fresh seq space.

- **Daemon epoch.** A per-daemon-process identity surfaced to the client at subscribe/handshake time.
  Reuse `server.obs.context.PROC_EPOCH` as the basis. NOTE (implementation finding): a bare module-
  level `PROC_EPOCH` is identical for two daemon apps in one test process, so capture a **per-instance
  epoch** at `create_daemon_app()` time (`PROC_EPOCH` + a uuid suffix). This is unique per daemon
  instance/process, which is what restart-detection needs, and makes the integration test pass for the
  right reason.
- **Client behaviour.** Track the daemon epoch. On reconnect, learn the daemon's epoch (via `/info`,
  read **before** sending subscribe frames) and if it changed, **reset `_last_seq` for all streams AND
  reset each `_baseline` to `None`** so the next subscribe is `since=None` (full-ring replay) instead
  of `seq > since` rejection. (The daemon direct-sends live events regardless of `since`, so only the
  *buffered* restart-window events are lost — which is why detection must happen before resubscribe,
  not on the first delivered event.)
- `/info` gains an additive `epoch` field; this is backward-compatible, so `PROTOCOL_VERSION` does NOT
  need bumping.

### Component 2 — Disk re-replay on reconnect/epoch-change (backfill), backend

Self-heal any gap regardless of cause: on a daemon reconnect (after the first connect), for each
session a browser is **actively attached** to, re-run the existing `_replay_and_complete`
(`replayReset` + fresh disk replay) to that browser.

- **Signal path.** The `DaemonClient` is shared across all browser WS connections, so the reconnect
  signal must fan out to every active `ws_endpoint` coroutine. On reconnect the reader injects a
  per-stream **sentinel** (`{"type": "_ipc_reconnect"}`) into each subscribed session stream's queue
  (skip the `_global` lifecycle stream). It flows through `RemoteDriver.events()` → `_forward_live`;
  `ws.py` intercepts the sentinel (does NOT forward it to the browser as a transcript event) and
  instead re-runs `_replay_and_complete(session_id)` for that attached session. Per-stream injection
  means only sessions with a live attachment re-replay; detached sessions are untouched. Emitted only
  on re-connects, never the first connect (an `_ever_connected` flag), so the initial attach doesn't
  double-replay.
- **Idempotent + cheap.** Re-replay is the same code the initial attach uses; combined with Component
  3 it produces no duplication or flicker.
- **Relationship to Component 1.** Component 1 already recovers gaps that still fit in the restarted
  daemon's 256-ring (resubscribe `since=None` → full-ring replay). Component 2 is the backstop for
  gaps larger than the ring, ring eviction during a slow reconnect, or any non-epoch reconnect.

### Component 3 — Non-shrink replay reconcile (safety invariant), frontend

Make a `replayReset`+replay safe to apply repeatedly over live state: never drop already-shown turns,
never duplicate streamed blocks.

- **Reconcile by turn id, replace blocks wholesale.** Change `replayReset` so it no longer empties
  `turns`; instead it enters replaying mode and the subsequent replay events reconcile against current
  state keyed by turn id (clear-on-first-touch). For a **matched** turn (already present), **replace
  its block list wholesale** with the replayed turn's blocks — disk is authoritative for a completed
  turn, so this avoids the `textDelta`/`thinkingDelta` append-duplication. For an **unmatched** turn,
  insert it in order. **Never drop** a turn the client already shows that is outside the replay window
  (older paged-in history; any in-flight newer turn).
- **Turn-id alignment (verified).** Live and replay ids match for **assistant** turns — the live
  translator sets the turn id from the SDK `msg.uuid` (`server/sessions/translator.py`) and replay
  uses the JSONL entry `uuid` (`server/sessions/replay.py`), the same identity. The events lost by
  this bug are assistant-side, so wholesale-replace reconcile lands them correctly. **User** turns are
  the exception: the live echo can carry a different id than the JSONL entry, so they reconcile via the
  existing content-based `pendingSent` adoption (`web/src/session/reducer.ts`). The non-shrink change
  must preserve that adoption path and must not drop or duplicate an adopted user turn just because the
  replay window labels it with a different id.
- **`replayComplete` re-assert interaction (implementation finding).** With `replayReset` no longer
  wiping, a surviving live-echo user turn can be duplicated by a pending prompt of the same text under
  a different id. The re-assert loop must (a) re-key a surviving live turn to the pending id instead of
  appending a duplicate, guarded by `replayTouched` so disk-replayed turns aren't replaced, AND (b)
  ensure each surviving live turn is consumed by **at most one** pending entry — including marking a
  turn the loop **appends** as used, so with N≥3 same-text pendings a later iteration cannot re-key an
  earlier-appended turn (the N≥3 duplicate-text data-loss bug caught in review).
- **Carve-outs preserved:** `/clear` (`_clear` action) and compaction keep their explicit shrink
  semantics — only *replay* becomes non-destructive.
- This also hardens the existing `followup-prompt-dropped-by-replayreset-race`.

## Data flow (after fix)

```
daemon restart → edge /events WS drops → reader reconnects
  → reader reads daemon epoch (/info); epoch changed → reset _last_seq + baselines (Component 1)
  → resubscribe since=None → daemon replays full ring (no seq>since rejection)
  → reader injects _ipc_reconnect sentinel per attached session stream (Component 2)
  → ws.py re-replays _replay_and_complete to attached browsers (complete disk transcript)
  → frontend reconciles replay over live state without shrinking/duplicating (Component 3)
```

## Testing

- **Failing test first (TDD):** a daemon-restart gap repro in `server/tests/` using
  `ipc_harness.serve_daemon_uds` with a socket reused across two `create_daemon_app()` instances;
  assert restart-window events are delivered after the fix.
- **Component 1:** unit — client resets `_last_seq`/baselines when epoch changes (no reset on first
  sight); integration — the restart repro now delivers all events.
- **Component 2:** ws-level — a reconnect sentinel triggers a `replayReset`+replay to an attached
  session and is NOT forwarded as a transcript event; no re-replay when no browser is attached; no
  sentinel on the first connect.
- **Component 3:** reducer — a replay window smaller than current state does not drop turns (older
  paged-in + in-flight survive); a re-replayed assistant turn replaces its blocks wholesale (no
  append-dup); a user turn adopted via `pendingSent` is not dropped or duplicated when the replay
  window carries a different id; **N≥3 same-text pendings with one surviving live echo and empty disk
  replay yield N distinct turns** (the regression for the caught data-loss bug); `/clear` + compaction
  still shrink.
- **Regression:** full `pytest server/tests/` (esp. `test_ipc_*`, `test_remote_*`) + web `vitest run`,
  `tsc -b`, `eslint`.

## Risks / rollout

- Wire-contract change is additive (`/info` epoch) ⇒ no `PROTOCOL_VERSION` bump; edge and daemon
  deploy together.
- `since=None` full-ring replay after a restart can re-send up to 256 buffered events as duplicates —
  Component 3 makes that harmless.
- Components are independently shippable: **1** removes the silent gap; **2** self-heals any residual/
  future gap; **3** is the invariant that makes 2 safe and fixes a sibling bug.

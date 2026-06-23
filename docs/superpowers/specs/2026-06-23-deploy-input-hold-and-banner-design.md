# Deploy-time input safety & always-visible hold banner

**Date:** 2026-06-23
**Status:** Approved (design)
**Scope:** Fixes symptoms #1 (no deploy indication) and #2 (input dropped, not queued) observed
during the #196 daemon deploy. Symptom #3 (renames continue mid-deploy) is explicitly deferred.

## Problem

During an upgrade deploy a user observed:

1. **No UI indication** that a deployment / outage was occurring.
2. **All sessions stopped accepting new input, and input was not queued** — keystrokes were lost.
3. Some **session renames continued** even though sessions were unresponsive. *(Deferred — not
   addressed here.)*

### Root causes (verified in code)

The deploy in question (#196, `1c57a8e`) touched `server/sessions/orchestrator.py`, `facade.py`,
`remote.py`, and the selector, so `scripts/auto-deploy.sh:72-88` classified it **daemon-changed**
and ran the **graceful-drain path** (`auto-deploy.sh:205-218`): it called `begin_drain` over the
daemon's Unix socket, waited for `ready`, then restarted daemon-first, edge-second. The banner and
buffering machinery *was* invoked, yet the user saw none of it. Two structural gaps explain this:

- **Symptom #1 — one-shot, transient deploy signal.** `begin_drain` publishes `{"type":
  "updatePending"}` exactly once (`orchestrator.py:786`). The event does propagate daemon→edge→browser
  correctly (no IPC allow-list drops it), but: (a) it is emitted once, so a browser that is
  backgrounded / mid-reconnect at that instant misses it; (b) the flag does not survive the restart —
  the browser reconnects to a fresh daemon whose `_draining` is `false`, and nothing re-asserts it;
  (c) **edge-only** deploys (e.g. #194) never call `begin_drain` at all (`auto-deploy.sh:219-221`),
  so there is no banner whatsoever — only a subtle amber "Reconnecting…/Disconnected" note in
  `StatusBar.tsx:12-18` for the ~1s edge restart.

- **Symptom #2 — input silently lost on two paths.**
  - *Client:* `prompt()` only buffers into `_pendingPrompts` when `_updatePending` is already true
    (`web/src/ws/client.ts:158-165`). Otherwise it falls through to `_send()`, which on a closed
    socket simply `return false`s (`client.ts:223-230`) — the keystroke is dropped with no queue and
    no error.
  - *Server:* if the socket was open during the drain-wait window, the prompt reaches the daemon and
    hits `if self._draining: … return` (`orchestrator.py:1133-1139`) with **no error/echo frame sent
    back** (`ws.py:445-451` does not ack a rejected prompt). The textbox believes it sent; the server
    silently swallowed it.

The `orchestrator.py:1135` comment assumes "the browser buffers and resends on reconnect" — but that
only holds if the client received `updatePending` first. When it didn't, both ends drop the input.

## Goals

- A user **always** sees an indication when their input cannot currently reach a session, regardless
  of deploy type (daemon, edge-only) or cause (deploy, network blip).
- User input is **never silently lost**: anything typed while the connection is untrustworthy is
  held and delivered once the session is reachable again.

### Non-goals

- Pausing or re-labelling session renames during a deploy (symptom #3) — deferred.
- Changing the deploy orchestration in `auto-deploy.sh` or the drain handshake itself.
- Distinguishing "deploy" from "ordinary network loss" in the UI — intentionally collapsed into one
  connection-state model (chosen Option A over an explicit deploy-state flag).

## Approach (chosen: connection-state driven)

Collapse the two narrow `_updatePending`-gated behaviours into a single client **"holding" state**,
true whenever input cannot be trusted to land:

- the WebSocket is not `OPEN` (disconnect — covers daemon deploys, edge-only deploys, and network
  blips), **or**
- the server has signalled it is refusing turns (drain), via the existing `updatePending` event or a
  new per-prompt `promptRejected` ack.

While holding: prompts are buffered instead of sent; **one** banner shows the held-message count and
a reconnecting/updating note; on reconnect **after** the session re-`attach`, the buffer flushes
FIFO.

> Note on the drain-wait window: after `begin_drain` but before the restart, the socket is still
> *open* while the server rejects prompts. Pure connection-state cannot catch this, so the server
> signal (`updatePending` + the new `promptRejected` ack) remains necessary and is folded into the
> same holding state — not a second banner.

### Rejected alternative — explicit deploy state (Option B)

Have the server expose a queryable "deploy in progress" flag (on `/healthz` or the WS hello) that
the client reads on reconnect. Rejected: more moving parts, and the *new* daemon does not know it
just restarted for a deploy, so "durable across restart" is awkward to implement honestly. Option A
covers more cases (network blips, edge-only deploys) with less machinery.

## Components & changes

### Client — `web/src/ws/client.ts`

1. **Buffer unless confirmed-deliverable.** `prompt()` pushes to the buffer when the socket is not
   `OPEN` **or** holding is active; it only calls `_send()` when truly open and not holding. Removes
   the silent-drop path at `client.ts:223-230`.
2. **Generalize the flush.** Run the pending-prompt flush on every `onopen` *after* the `attach`
   re-send (so the session is live first), whenever the buffer is non-empty — not only when
   `_droppedWhilePending` (`client.ts:101-108`).
3. **Handle a new `promptRejected` frame.** Re-queue the rejected prompt into the buffer and enter
   holding. The buffer is the single source of truth — a prompt is *either* on the wire *or* in the
   buffer, never both, so re-queue cannot double-send.
4. **Emit holding state.** Derive `holding = connectionState !== 'connected' || _updatePending ||
   _serverRefusing` and emit it so the banner renders from connection-state, not just
   `updatePending`.

### Client — banner component (`web/src/components/UpdatePendingBanner.tsx` → generalized)

One banner driven by the holding state, showing the held-message count and a connection-aware note
(reconnecting vs. updating). Single component, single message family — not two parallel banners.

### Server — `server/sessions/orchestrator.py`

`send_prompt` returns a **status** (`accepted` vs `rejected:draining`) instead of a bare `return`
(`orchestrator.py:1133-1139`). RPC methods already return values across the IPC boundary, so this
works in both in-process and remote/daemon modes. `updatePending` / `updateCancelled` publication is
unchanged.

### Server — `server/bridge/ws.py`

When `send_prompt` returns a rejected status, emit a top-level frame
`{"type":"promptRejected","sessionId","text","attachmentIds","reason":"draining"}` back to the
browser (`ws.py:445-451`) so the client re-queues rather than losing the prompt.

## Data flow

```
user types
  → client.prompt()
      ├─ holding OR socket≠OPEN  → buffer (FIFO, bounded)   ──┐
      └─ else                    → _send()                    │
                                      ↓                        │
                              server send_prompt               │
                                ├─ accepted  → driver          │
                                └─ rejected  → ws emits         │
                                     promptRejected ───────────┤ re-queue → buffer
                                                                │
WS reopen (onopen): send `attach` → (session live) → flush buffer FIFO ──┘
```

## Error handling & edge cases

- **Bounded buffer:** cap (~50 prompts). On overflow, drop the **oldest** and surface a visible
  warning — *no silent truncation* (project norm: "No silent caps").
- **Ordering & attachments:** strict FIFO flush, performed only after `attach`; each entry preserves
  its `attachmentIds`.
- **No double-send:** a prompt is on the wire xor in the buffer; `promptRejected` moves it back.
- **Edge-only deploys:** covered for free — the ~1s disconnect now buffers + shows the banner instead
  of dropping.
- **Detached/read-only sessions:** the server resume-on-first-input path (`_ensure_live`,
  `ws.py:445-451`) is unchanged; flushed prompts resume a detached session lazily as today.

## Testing

### Client (vitest, `web/src/ws/` + component tests)

- prompt while disconnected → buffered, **not** sent.
- reconnect → `attach` is sent **before** the flush; buffered prompts flush in FIFO order.
- `promptRejected` frame → that prompt is re-queued (and only once).
- buffer overflow → oldest dropped, a warning is surfaced.
- banner renders whenever holding is active (disconnect *or* drain), with the correct held count.

### Server (pytest, `server/tests/`)

- `send_prompt` during drain returns the rejected status (in-process path).
- `ws.py` emits a `promptRejected` frame when the status is rejected.
- `begin_drain` still publishes `updatePending` (regression guard).
- status value crosses the RPC boundary (remote-mode guard, mirroring existing drain RPC tests).

## Out of scope (tracked separately)

- **Symptom #3** — titling sweep (`orchestrator.py:610-628`) runs with no drain/liveness guard, so
  renames continue mid-deploy and a moving title can masquerade as a live session. Deferred by user.

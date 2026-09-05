# Fix pattern: an attach-time re-emit outruns the open-turn resupply

## Symptom

After navigating back to (or reloading) a session mid-turn, the prompt that started the
turn sits at the very bottom of the transcript and stays there while the answer keeps
growing above it. The client journal shows `transcript.turn.fabricated type=question`
(or `type=permissionRequest`) a second after `ws.attach.decision mode=full_replay`.
Session bce4b626, 2026-09-05 05:53 — but the client log shows the same warning on most
days, once per mid-ask reattach.

## Anti-pattern

Two sources feed one socket FIFO with pieces of the same logical turn and nothing pins
their relative order. The attach path enqueues "current state" frames directly
(`out_q.put_nowait`) while the turn's own frames (UserMessage, TurnStart, deltas, the
ask itself) come from the live stream task started afterwards. A frame that carries a
`turnId` therefore reaches the reducer before the turn's opening frames; the reducer
fabricates the turn at the tail, and the prompt that should sit above it is appended
under it. Every later delta grows the fabricated turn, so the prompt never moves.

The root is the id split: live frames use the driver's turn ids, disk replay uses JSONL
uuids, and the reattach stitches them by trimming the open turn from disk and trusting
the resupply. Any extra copy of a turn-scoped frame that does not ride the resupply
breaks the stitch.

## Fix shape

- The resupply is the only source of the open turn, asks included. The local turn
  buffer (`_TURN_CONTENT`) now retains PermissionRequest/Question and their Resolved
  events, matching what the daemon ring already carried; the bridge skips the pending
  ask re-emit whenever the open turn is resupplied (`open_turn_resupplied=drop_incomplete`
  in `_emit_live_extras`). The re-emit stays for attaches nothing re-supplies from.
- The reducer treats a fabricated turn as provisional: `ensureAssistantTurn` marks it,
  and the turnStart that later names that id re-anchors it to the tail
  (`anchorTurnStart`), so a mis-ordered copy can no longer pin a prompt.

## Where to look

- `server/bridge/ws.py` `_emit_live_extras` / `_handle_attach`: any new
  `out_q.put_nowait` of a frame with a `turnId` that is not routed through the live
  stream. Status, queue, contextUsage and resumeOk carry no turn and are safe.
- `server/sessions/claude_driver.py` `_TURN_CONTENT`: a new turn-scoped event type the
  reattached view must see belongs here, or the local snapshot loses it.
- `web/src/session/reducer.ts` `ensureAssistantTurn` callers (deltas, toolCall, error,
  recovering, permissionRequest, question): all fabricate; all are re-anchored by the
  same turnStart rule. Watch for a new "current state" frame type that creates turns.
- Prod: `journalctl -u ccswitch.service | grep transcript.turn.fabricated` — a steady
  rate of `type=question|permissionRequest` right after `ws.attach.decision` is this
  shape again.

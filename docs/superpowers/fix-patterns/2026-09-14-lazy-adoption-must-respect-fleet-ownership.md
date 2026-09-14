# Fix pattern: a lazy DB-to-memory adoption that skips the fleet ownership rule

## Symptom

The assistant drawer accepts a prompt, shows "Received — waking session…" and spins
forever with no error. The edge journal shows `ws.session.resume … ok=false` with
`resume_ms` in single digits (no real resume was attempted), right after an
`ensure_driver` RPC to a *worker* host. The daemon journal shows the same session id
flip-flopping between `session.reattach reason=assistant_adopt` and
`mirror.evict_deleted reason=inactive_row` every status poll. 2026-09-14, session
65872a51: an `ended` assistant row stamped `host=rocky` from before the primary pin.

## Anti-pattern

An on-demand path materialises a DB row into the primary's `_records` (here
`_adopt_reaped_assistant`, #1123) with only a worker-side host guard, so on the primary
it adopts whatever row is newest. Two other fleet rules then act on the same row and
disagree with it: the mirror sweep evicts it as a foreign inactive row, and the
per-session router follows the row's `host` stamp to a worker that never rehydrated it.
The status endpoint vouches for an id the prompt path cannot reach.

A second, smaller shape rode along: the edge emitted the failure as an `error` frame
(a banner) but the pending bubble's stage is a separate channel (`promptAck`), and
nothing on the failure path ever moved it off `resuming`. The drawer compounded it by
never rendering `state.error` at all.

## Fix shape

- Every path that puts a row into `_records` on demand consults `_owns_record`
  (`self._enabled_host_names()` first), the same rule `rehydrate`'s acting loops, the
  queue promoter and the mirror sweep already use. Skip a foreign row and keep
  scanning for the newest row this daemon owns; do not return None (that mints a new
  thread and strands the daemon's own history).
- A failure that ends a prompt's journey retracts the receipt ack on the same
  channel it was given: `promptAck stage=failed` after the error frame. The browser's
  stage vocabulary gained the terminal `failed` (`promptStage.ts`), and both
  transcript surfaces render `SessionErrorBanner`.

## Where to look

- `server/sessions/orchestrator.py`: every `_record_from_row(` call site that assigns
  into `self._records`. Today: `rehydrate` (deliberately primary-wide; ownership is
  enforced downstream), `unarchive_session` (per-session routed to the owner), and
  `_adopt_reaped_assistant` (fixed). A new lazy loader belongs in this list with an
  ownership check.
- `server/sessions/fleet_orchestrator.py` `_PER_SESSION`: any method that routes by
  the row's `host` stamp is a consumer that will disagree with an unowned adoption.
- `server/bridge/ws.py` `_handle_prompt`: every early `return` after the receipt ack
  must either `promptRejected` (re-queued) or `promptAck failed` (terminal).
- Prod: `journalctl -u viberpit-daemon.service | grep -E "assistant_adopt|evict_deleted"`
  alternating on one id is this shape again.

# Fix pattern: a hand-maintained flag shadows live state, and a queue that can never drain

**Date:** 2026-09-02
**Origin:** RCCA of the "frozen Answer-needed session" failure mode (production session
`346a9863`, 2026-09-02 09:49 EDT). The user could not answer a pending AskUserQuestion from the
UI, and every message typed into the composer landed in the queued-prompt strip instead of
reaching the model. Two independent defects combined; both are instances of the same shape:
**two representations of one fact that drift silently.**

## Symptom

- Session card says "Answer needed"; the transcript shows the question as a read-only card; no
  pinned answer panel; typed replies pile up in the queue strip; nothing ever moves.
- Attach logs (`ws.attach.decision`) say `detached=true` for a session whose daemon driver is
  live and mid-turn. No warning anywhere explains it.

## Anti-pattern

1. **Shadow flag.** `SessionRecord.detached` was a stored boolean meaning "no driver in
   `_drivers`". It had one writer that set it (`_record_from_row`) and one that cleared it —
   inside `ensure_driver`, *after* the driver was registered and *after* an awaited SDK
   `set_model` that can raise. The reassign install path (`_install_driver`) never cleared it.
   `ensure_driver`'s fast path returns an existing driver without touching it, so once wrong
   it stayed wrong for the life of the process. The edge branched on the flag to choose a
   read-only disk-replay attach (no pending-question re-emit, no live status).
2. **Unreachable queue.** `SessionDriver.send_prompt` enqueued whenever "a pump task is alive".
   While the turn is parked in `_can_use_tool` awaiting a question, the pump is alive, so the
   reply was queued; the queue drains only when the turn ends; the turn ends only after the
   reply. A deadlock by construction — the driver's own `awaiting_user_input` was never
   consulted.
3. **Silent rejection.** `ws.answerQuestion` discarded the boolean from `answer_question`;
   `restore_deploy` / `_recover_resume_failures_once` swallowed `ensure_driver` exceptions
   with no log. Every failure above was invisible.

## Fix shape

- **Derive, don't mirror.** Compute the flag from the thing it describes at read time
  (`_sync_detached`: `rec.detached = rec.id not in self._drivers`, applied in `get_session` /
  `list_sessions`). Delete the hand-maintained writes.
- **Gate on the semantic state, not a proxy.** When a question is pending, a user message *is*
  the answer: `send_prompt` resolves every pending question (chat framing) via
  `QuestionRegistry.resolve_all` instead of enqueueing. Images ride as an attachment-only
  follow-up so nothing is dropped.
- **Never a silent no-op.** A rejected answer sends an `error` frame (`QUESTION_NOT_PENDING`);
  recovery paths log the exception they catch; a `set_model` failure after the driver is
  registered is logged and non-fatal.

## Where to look

- Any `SessionRecord` boolean whose truth is "X exists in some dict/set" — grep
  `(rec|record)\.[a-z_]+ = (True|False)$` in `server/sessions/orchestrator.py`. Reviewed
  2026-09-02: `account_pinned` (DB-backed, persisted alongside), `interrupted_by_restart`
  (transient by design), `model_pending` (transient, cleared by `_settle_model_pending` on the
  status transition it waits for) — none derive from live collection membership; no change.
- Any `except Exception:` immediately followed by `driver = None` / `pass` in a **decision**
  path (as opposed to teardown). Reviewed 2026-09-02: the remaining `pass` sites are
  `aclose()` / cache-write swallows during teardown; no change.
- Any `await orchestrator.<verb>(...)` on the WS receive path whose boolean result is dropped.
  `resolve_permission` had the identical shape — fixed in the same PR
  (`PERMISSION_NOT_PENDING` error frame).
- Any queue gated on "is something running" where the running thing can be blocked on user
  input: `driver.send_prompt` was the only one; `_queue` is drained solely in `_drain`'s
  `finally`.
- **Tests that `mock.patch.object(orch, "ensure_driver", …)`** and then expect a LIVE edge
  attach must also register the fake in `orch._drivers["<sid>"]` (that is what the real
  `ensure_driver` does; `detached` derives from it). Before this fix such tests passed by
  accident on the stale default `detached=False`; now they hang on the read-only attach.
  Fixed 2026-09-02 in `test_ws_cursor_catchup.py`, `test_ws_reattach_turn_resupply.py`,
  `test_remote_mode_edge.py` (fakes also gained `pending_questions/pending_permissions`,
  which the live-attach extras call on any registered driver).

## Reference

- Tests: `server/tests/test_detached_derived_from_drivers.py`,
  `server/tests/test_question_prompt_answer.py`, `server/tests/test_ws_rejected_answer.py`,
  `web/src/session/reducer.questionAnswer.test.ts`.
- Related: `docs/superpowers/fix-patterns/2026-08-31-empty-name-aliases-store-root.md`
  (another "two paths to one fact" drift), `server/tests/test_ensure_driver_identity_rethread.py`
  (the earlier silent-drop on identity mismatch).

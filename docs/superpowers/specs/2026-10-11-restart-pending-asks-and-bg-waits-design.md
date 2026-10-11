# Pending asks and background waits across a daemon restart (#2922; part of #2910): design

**Date:** 2026-10-11 · **Status:** approved for implementation · **Parent design:** `2026-10-11-restart-survivable-session-work-design.md`

## Problem

#2922 reports two problems: a restart auto-closes a pending AskUserQuestion, and it kills background waits. Here is what still happens on master; the fix for #967 (dabfac1d) only changed how an orphaned answer is worded.

1. **The question is dismissed at shutdown.** `ClaudeAgentDriver.aclose()` calls `_qreg.deny_all` and `_reg.deny_all`. The question resolves to "[User dismissed the question UI] … user did not answer", and a pending permission is denied the same way. The agent therefore reads a restart as the user refusing.
2. **Transcript repair says "by user".** `transcript_repair.plan_repair` writes `[Request interrupted by user for tool use]` for every trailing orphan, so the agent never learns a restart happened.
3. **The orphaned card stops working.** The rearm prompt (`_rearm_session`) or the `restore_deploy` replay starts a turn, `_persist_status` clears `interrupted_by_restart`, and the re-offered question card can no longer be answered.
4. **Only questions are re-offered.** Permission and plan orphans are detected by `orphaned_asks.orphaned_ask_kind`, but `ws._orphaned_question_frames` and `answer_question → _deliver_orphaned_answer` handle the `question` kind only, and `resolve_permission` has no fallback.
5. **Background waits are relaunched blindly.**
   - The rearm snapshot is taken at the start of the drain, up to 570 s before the restart, and is never re-checked.
   - A `run_in_background` Bash command or a Monitor is relaunched "with exactly the parameters shown", even if it already finished during the drain. A pytest run that had completed gets run again.
   - The output-file path is never reported.
6. **Prompts are replayed twice.** `restore_deploy` replays the snapshot's `lastPrompt`, and `_rearm_session` replays `pending_prompt`, so the same prompt can go in twice. A delivered orphaned answer, or a system nudge, also becomes `_last_prompt`, so the next restart replays it as if the user had sent it again.

## Design

1. **Close for a restart, not a dismissal.**
   - When the driver is closed for a daemon shutdown or drain (the orchestrator's `_shutting_down` / `stop()` path), pending questions and permissions resolve with a restart-specific message instead of "user did not answer". Thread a `reason="restart"` argument down to `deny_all`.
   - The message: `"Interrupted by a ViberPit daemon restart: the user did NOT answer or reject this. It will be re-presented to them; wait for their answer and do not assume a refusal."`
   - Emit `QuestionResolved` with `mode="interrupted"`, or the nearest existing mode the web reducer already renders without treating it as dismissed. Check `web/src/session/` and only add a mode if one is needed.
   - User-initiated closes keep today's text.
2. **Transcript repair says "restart" when it was one.**
   - `plan_repair` and `repair_transcript` gain a `text=` parameter. The resume path passes `RESTART_INTERRUPT_TEXT = "[Request interrupted by a ViberPit daemon restart — not by the user]"` when the session is `interrupted_by_restart`.
   - Grep for any reader that matches `INTERRUPT_TEXT` (UI, orphaned-ask detection, `last_question_events`) and make it accept both texts.
3. **Hold the orphaned card open.**
   - When `orphaned_interaction(sid)` is not None, `_rearm_session` defers its rearm and pending-prompt replay, and `restore_deploy` skips its replay.
   - The deferred rearm text is folded into the orphaned-answer delivery, so the answer prompt carries the restart and background-task context. The user's answer starts the turn.
4. **Re-offer permission and plan orphans too.**
   - Extend the ws orphaned-frame builder and the answer paths to cover `permission` and `plan`.
   - `resolve_permission` and plan approval fall back to delivering the decision as a prompt through a generalised `orphaned_answer_prompt(kind, …)`, for example: `"[Re: the <tool> permission request a daemon restart interrupted: the user ALLOWED it. Re-run the tool call.]"`.
   - Reuse `orphaned_asks.py`; don't add a parallel module.
5. **Truthful background-wait rearm.**
   - At rearm time, re-scan the transcript (`scan_open_background_tasks`) and drop any snapshot entry that has since finished.
   - For a background Bash command, parse the output-file path from its `tool_result` ("Output is being written to: …") and tell the agent: *"`<cmd>` was started in the background before the restart. Its output file `<path>` (exists: yes/no, last modified …) may already hold the complete result: read it first and relaunch only if it is incomplete."*
   - A Monitor is re-armed only if its target is still running. Otherwise it is reported as "target finished before restart, output at X".
   - All of this lives in `background_tasks.build_rearm_prompt`, its single home.
6. **Replay at most once.**
   - `restore_deploy` skips the replay when `_rearm_session` already replayed `pending_prompt` for that session (track it in `_rearmed`/`_replayed`), or while a re-offered ask is waiting on the user. A driver that is already mid-turn still gets the replay, queued behind the current turn: when the drain recorded no `pending_prompt`, `restore_deploy` is the only replayer, so skipping it would lose the interrupted request.
   - Prompts whose `origin` is a system kind (orphaned answer, completion nudge, resume nudge, rearm) are never recorded in `_last_prompt`, so they can never be replayed as user input.

## Tests

One test or more per item above, including:
- a question pending at `stop()` resolves with the restart text and is not "dismissed";
- repair text after a restart;
- rearm is deferred while an orphaned question exists, and answering it delivers the answer together with the rearm context;
- an orphaned permission is re-offered, and an allow is delivered as a prompt;
- a finished background Bash produces "read output file X" instead of a relaunch;
- `restore_deploy` plus `_rearm_session` produce exactly one replay;
- an orphaned answer is never replayed by a later `restore_deploy`.

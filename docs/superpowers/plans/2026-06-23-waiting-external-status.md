# Plan: "Waiting…" status for sessions parked on an external event

## Problem
A session whose turn ended shows **"Your turn"** even when the agent is not waiting
for the user but is **parked on an external event** — an open background task
(`Monitor` / `run_in_background` Bash) or a self-scheduled wakeup (`ScheduleWakeup` /
`/loop`). `derive_status` reaches `done` ("Your turn") by *elimination*, with no signal
for "parked." Reported with two live sessions ("Get PRs merged for nightly CI",
"Context condensing progress indicator design") both falsely reading "Your turn".

## Design
`done`/`idle`/`limited`/`ended` are produced by, respectively, the pure `derive_status`
and **orchestrator overlays**. `waiting_external` is overlay-shaped (it needs the
transcript, which the driver can't see but the orchestrator can via `transcript_path`).

- Keep `derive_status` **pure and unchanged**.
- New status string: **`waiting_external`** → frontend label **"Waiting…"**, its own
  calm tier **`waiting`** (sorts below `your-turn`, above `busy`).
- Overlay site: `_persist_status` (orchestrator.py:502) already special-cases
  `status == "done"` and already reads the transcript. When the candidate is `done`,
  scan; if parked, persist `waiting_external` instead.
- Signal = `scan_open_background_tasks(path)` non-empty **OR** `scan_pending_wakeup(path)`.
- Restart-safe: **NOT** added to `_ACTIVE_STATUSES`. Preserved through rehydrate
  normalize (not in the `("working","in_progress","condensing")` reset set).

## Tasks (TDD, each red→green)
1. **[USER CONTRIBUTION]** `scan_pending_wakeup(jsonl_path) -> bool` in
   `server/sessions/background_tasks.py` — true iff the **last assistant turn** issued a
   `ScheduleWakeup` tool_use (armed, not yet fired). Failing test:
   `server/tests/test_background_tasks_scan.py`.
2. Orchestrator overlay in `_persist_status`: `done` → `waiting_external` when parked.
   Test in `server/tests/` (orchestrator/persist-status level).
3. `server/sessions/types.py:27` — add `waiting_external` to the status comment.
4. Frontend: `web/src/api/types.ts` union += `'waiting_external'`;
   `web/src/session/statusTier.ts` — new tier `waiting`, `PRESENTATION.waiting_external`
   (label "Waiting…", calm), `TIER_RANK.waiting`, `TIER_LABEL.waiting = 'Waiting'`.
   Tests in `web/src/session/__tests__/statusTier.test.ts`.
5. Verify `_ACTIVE_STATUSES` unchanged (restart-safe) and rehydrate preserves the value;
   add a regression assertion.

## Out of scope
Re-arming a lost `ScheduleWakeup` across daemon redeploys (the #207 re-arm covers
background tasks only). This change is display-only.

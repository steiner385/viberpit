# Background work visibility — design

**Issue:** #2635. **Builds on:** #1436 (parking from the CLI ack), #1553 (sub-agent live view).

## Problem

A session reads **"Waiting…"** (`waiting_external`) while the sub-agent chip shows nothing.
The server knows exactly what the session is parked on — `scan_open_background_tasks` /
`scan_pending_wakeup` in `server/sessions/background_tasks.py` — but that list only ever
drives the parking overlay and the restart re-arm. Nothing carries it to the browser, and
the translator drops every non-agent task notice (`task_type != "local_agent"`).

## Verified CLI facts (claude 2.1.287, the binary `claude_bin.cli_path_kwargs` points the SDK at)

Read out of the installed binary (`~/.local/share/claude/versions/2.1.287`), 2026-10-02:

- `system/task_started` is emitted with `tool_use_id: e.toolUseId` for **every** task type,
  plus `description`, `is_backgrounded`, `task_type: e.type`, `owned_by_subagent`.
- A backgrounded `Bash` call registers a task of `type: "local_bash"` with `toolUseId` set
  from the Bash call and `isBackgrounded: true`. **`Monitor` is the same `local_bash` task
  with `kind: "monitor"`** (`kind` is not in the notice payload). A *foreground* Bash is
  also registered as `local_bash` with `isBackgrounded: false`.
- `system/task_notification` carries `tool_use_id: r?.toolUseId` (from the task record) and
  `status`; it does **not** carry `task_type`. `system/task_updated` carries `task_id` and a
  `patch` with `status` (`killed` for a stopped task).
- A Workflow run is a `local_workflow` task. Other `local_*` literals in the binary are not
  task types.
- The task registry's `LocalShellTask.kill` is what `TaskStop` uses; the SDK's `stop_task`
  control request resolves tasks through the same registry by id (the sub-agent Stop
  already relies on it). Confirmed live from inside a session: `TaskStop` on a
  `run_in_background` Bash reports `"task_type":"local_bash"` and kills it.

## Design

Two sources, merged in the browser exactly like sub-agents merge `turns` + `state.subAgents`:

1. **Resting snapshot (server, persisted).** `background_work_snapshot(tasks, wakeup)` in
   `background_tasks.py` renders the engine-neutral `OpenBgTask` list (minus `kind ==
   "agent"`, which the sub-agent surface owns) plus the armed wakeup into
   `{"items": [{toolUseId, kind, name, description, startedAt}], "wakeup": bool,
   "wakeupAt": iso|null}`. The orchestrator persists it on the session record and row
   (`background_work`, JSON nullable) whenever `_persist_status` runs for a finished turn
   (parked → snapshot, not parked → `None`), on the driver's `on_background_work_changed`
   callback, and clears it when the rehydrate self-heal un-parks a row. It rides the
   daemon→edge RPC in `SessionRecord.to_dict/from_dict` (which also gain `subAgents` —
   today that summary never crosses the RPC, so fleet-mode cards show no sub-agent rows)
   and the HTTP serializer as `Session.backgroundWork`. Codex background terminals come
   for free through `_open_background_work`.
2. **Live events (translator).** `task_started` with a `tool_use_id` and a non-agent
   `task_type` → `BackgroundTaskStarted(toolUseId, taskId, description, taskType,
   backgrounded)`; a terminal `task_updated` / `task_notification` for a task in the
   translator's `_bg_task_tool` map → `BackgroundTaskEnded(toolUseId, taskId, status)`.
   The driver registers the task in the same `tool_use_id → task_id` map the sub-agent
   Stop uses, so `POST /sessions/{id}/agents/{toolUseId}/stop` stops a shell too.

**Web.** The reducer keeps `state.backgroundTasks[toolUseId]` (live-only; preserved across
`replayReset` since the replay never re-emits task notices). `selectBackgroundWork(turns,
live, snapshot)` merges: snapshot items are running unless the live stream saw them end;
live entries not in the snapshot are shown only when launched in the background (the
notice's `backgrounded`, a `Monitor` block, or `input.run_in_background`), so a foreground
Bash never appears. `SessionMeta` gets a **separate "Background" chip** (the user's pick:
a shell has no transcript to drill into and Stop means something different) whose popover
is `BackgroundWorkList` — kind · status · elapsed · description, Stop on live running rows,
and a wakeup row with a countdown. The session card shows the same summary ("1 shell ·
wakeup in 12m") as a collapsed count pill, read-only rows when expanded, and as the title
of the Waiting… pill.

**Out of scope.** Streaming the shell's `output_file`; a Monitor/shell drill-down page;
changing the `waiting_external` label itself.

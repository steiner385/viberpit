# A background launch's acknowledgment is not the tool's result

**Symptom:** Sessions that had fanned work out to background subagents sat on
"Your turn" (`done`) for ten to twenty minutes while three or four agents were visibly
working. On 2026-09-07 one user typed "i think the agent is done" into such a session
eighteen minutes after it parked; another reported "this session says your turn, but
it's clearly still busy."

**Anti-pattern:** Deciding whether a tool call is background work from the *launch input*
(`run_in_background: true`, `name == "Monitor"`). The `Agent` tool runs in the background
by default and writes no `run_in_background` key when it does, so the input of a
backgrounded agent is byte-identical to a foreground one. The related sibling shape is
treating the CLI's acknowledgment of a background launch (`toolUseResult.status ==
"async_launched"`, `backgroundTaskId`, `resumedAgentId`) as the tool's *finished*
result: it arrives as an ordinary `tool_result`, so a reader that pairs `tool_use` with
`tool_result` sees a background agent as "done" the instant it starts.

**Fix shape:** Read the CLI's own verdict. The `type:"user"` entry journaling a tool's
result carries a structured top-level `toolUseResult`; `isAsync`/`async_launched`,
`backgroundTaskId` and `resumedAgentId` mean "still running", and the task-id they name is
what a later `<task-notification>` closes (the post-restart `stopped` notice carries only
that task-id, no tool-use id). A background agent is not replayable on restart: its input
is a brief that commits and opens PRs, so report it as lost the way a Codex terminal is.

**Where to look:** Any reader that pairs `tool_use` with `tool_result` to derive a
status. Fixed here: `server/sessions/background_tasks.py` (the `is_parked` /
`waiting_external` overlay and the deploy re-arm snapshot). Known siblings left as
follow-up because they need the `<task-notification>` surfaced as a replay/stream event
first: `server/sessions/subagents.py:summarize_transcript` and
`web/src/session/selectors.ts:selectSubAgents` both mark a background agent `done` on its
launch ack and never see it finish.

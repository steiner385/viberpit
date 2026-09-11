# Sub-agent live view: status, stop, and in-place permissions for Task sub-agents

**Date:** 2026-09-11
**Status:** Approved (brainstorming, Tony 2026-09-11) — pending implementation plan
**Builds on:** `docs/superpowers/specs/2026-06-21-subagent-thread-rendering-design.md`
(the `parentToolUseId` routing that gives each Task sub-agent its own thread)
**Follow-up (piece 2, separate spec):** child-session dispatch — sub-agents as linked
ViberPit sessions, the interactive path this spec deliberately does not attempt.

## 1. Problem

Before this spec, clicking a sub-agent row opened a read-only transcript with a "Read-only
sub-agent" badge (since finish-line row 6 that view is the Reader page `features/sessions/SubAgentPage.tsx`,
which implements §5.5 below). It showed the sub-agent's turns only if the parent was open
while it streamed, knows nothing about the sub-agent's progress or usage, cannot stop it,
and does not show the permission cards the sub-agent itself is blocked on (those land in
the parent's transcript, unattributed). Tony's ask: "click into sub-agent sessions and
interact with them just like in Claude Code."

What Claude Code itself offers for a Task sub-agent (verified against the bundled CLI
2.1.268 on 2026-09-11) is its `/tasks` agent view: `type › description`, status, elapsed,
tokens, tool count, model, a live progress line, the prompt, and two actions — kill the
agent, or foreground it. **There is no text input to a sub-agent.** A `user` frame that
carries a `parent_tool_use_id` is dropped at ingress ("Ignoring parented user frame …
echo/replay of a subagent frame"), and the only agent-addressed control request is
`stop_task`. Constitution Principle I forbids patching the binary, so "interact" here
means parity with that view: see it live, stop it, and answer what it is waiting on.

## 2. Verified protocol surface (Principle III)

Two throwaway spikes ran against `~/.local/bin/claude` 2.1.268 through
`claude_agent_sdk` 0.2.105, the exact path `ClaudeAgentDriver` uses:

1. **Task notices.** Spawning a foreground Task emits, on the same message stream the
   driver already consumes:
   - `TaskStartedMessage {task_id, tool_use_id, description, subagent_type,
     is_backgrounded, spawn_depth, task_type:"local_agent", prompt}`
   - `TaskProgressMessage {task_id, tool_use_id, description (the CURRENT activity, e.g.
     "Running Sleep for 90 seconds"), subagent_type, usage{total_tokens, tool_uses,
     duration_ms}, last_tool_name}`
   - `TaskUpdatedMessage {task_id, patch{status, end_time}}` — no `tool_use_id`
   - `TaskNotificationMessage {task_id, tool_use_id, status completed|failed|stopped,
     summary, usage?, output_file}`
   All four are `SystemMessage` subclasses. `Translator.feed` currently returns `[]` for
   them (only `init` / `api_error` / `compact_boundary` are handled).
2. **`stop_task` kills a FOREGROUND sub-agent.** `client.stop_task(task_id)` 8 s after
   `task_started` produced, at the sub-agent's next tool boundary: `task_updated
   status=killed`, `task_notification status=stopped`, then the parent received a
   `tool_result` `[Request interrupted by user for tool use]` (is_error) for the Task
   call and finished its turn normally. Nothing else was interrupted.
3. **Permission `agent_id` == `task_id`.** A sub-agent's `can_use_tool` arrived with
   `ToolPermissionContext.agent_id = "a9ece7cc21a6717b7"`, identical to the
   `task_started.task_id`. So `task_id` is the join key between the notice stream and the
   permission channel, and `task_started.tool_use_id` resolves it to the Task tool-use id
   the reducer already keys threads by.

## 3. Goals and non-goals

Goals:

1. The sub-agent view and the sub-agent rows show live status, elapsed, tokens, tool
   count, last tool and the CLI's progress line, for foreground and background
   sub-agents, whether or not the parent view was open.
2. A **Stop** action on a running sub-agent, backed by `stop_task`, audited.
3. Permission and question cards raised by a sub-agent render inside its thread and are
   answerable there, while still appearing in the parent transcript as today.
4. Nested sub-agents get a real `parentAgentId` instead of the current `None` placeholder.

Non-goals:

- **A composer in the sub-agent view.** Dropped after review: it would stack two hops the
  protocol never promises (parent must accept input mid-turn; the parent's model must
  choose to relay, and `SendMessage` is documented here as resuming a *finished* agent).
  The interactive path is child-session dispatch (piece 2).
- Foregrounding (`background_tasks` control request). Nothing in the UI needs it.
- Codex. Its engine reports `subagents=False`; nothing here touches `server/codex/`.
- Persisting task ids or usage to the DB. The `sub_agents` JSON column keeps its summary
  shape plus the new `parentAgentId`; live task state lives in the driver and the reducer.

## 4. Server design

### 4.1 Events (`server/sessions/events.py`)

Three new dataclasses, wire type names in camelCase like every other event. All carry
`parent_tool_use_id` — the spawning Task's tool-use id — as `parentToolUseId`, so the web
reducer's existing "tagged event → sub-thread" routing applies unchanged.

```python
@dataclass
class SubAgentStarted:
    parent_tool_use_id: str; task_id: str; description: str; agent_type: str
    backgrounded: bool = False; depth: int = 1; ts: float | None = field(default_factory=_now_ms)
    # wire: {"type":"subAgentStarted","parentToolUseId","taskId","description","agentType","backgrounded","depth","ts"}

@dataclass
class SubAgentProgress:
    parent_tool_use_id: str; task_id: str; activity: str
    total_tokens: int | None = None; tool_uses: int | None = None; duration_ms: int | None = None
    last_tool: str | None = None; ts: float | None = field(default_factory=_now_ms)
    # wire: {"type":"subAgentProgress","parentToolUseId","taskId","activity","totalTokens","toolUses","durationMs","lastTool","ts"}

@dataclass
class SubAgentEnded:
    parent_tool_use_id: str; task_id: str; status: str   # "completed" | "failed" | "stopped"
    summary: str = ""; total_tokens: int | None = None; tool_uses: int | None = None
    duration_ms: int | None = None; ts: float | None = field(default_factory=_now_ms)
    # wire: {"type":"subAgentEnded","parentToolUseId","taskId","status","summary","totalTokens","toolUses","durationMs","ts"}
```

`PermissionRequest` and `Question` gain `parent_tool_use_id: str | None = None`
(`parentToolUseId` on the wire, default None, so every existing constructor and test
is untouched).

### 4.2 Translator (`server/sessions/translator.py`)

In the `SystemMessage` branch, before the generic `return []`:

- `TaskStartedMessage` with `task_type == "local_agent"` (or absent) and a `tool_use_id`:
  record `self._task_tool[task_id] = tool_use_id`; emit `SubAgentStarted`.
  `backgrounded` from `data["is_backgrounded"]`, `depth` from `data["spawn_depth"]`
  (default 1), `agent_type` from `data["subagent_type"]` (default `"agent"`). A notice
  without `tool_use_id` (a Bash/Monitor task) is ignored.
- `TaskProgressMessage`: resolve `tool_use_id` from the message or the map; emit
  `SubAgentProgress` (`activity` = `description`). Unknown task → `[]`.
- `TaskUpdatedMessage` with a terminal `status` (`TERMINAL_TASK_STATUSES` from the SDK):
  resolve via the map; emit `SubAgentEnded` with `killed → "stopped"`, other terminal
  values passed through if in the wire vocabulary else `"failed"`. Unknown task → `[]`.
  The map entry is dropped here too (see below) — the SDK's own docs allow the terminal
  state to arrive with no notification following it.
- `TaskNotificationMessage`: emit `SubAgentEnded` with its status, summary and usage.
  **Ended is idempotent on the client** (see §5.2), so the killed/stopped pair the spike
  showed simply arrives twice; the second carries the summary and wins. The map entry is
  dropped on ANY terminal signal — the update as well as the notification — not only on
  the notification: a `TaskUpdatedMessage`'s terminal state can be the last one to arrive.
- The `local_agent` task-type filter (a `TaskStartedMessage` with no matching type is
  ignored) applies to every notice that carries its own `tool_use_id` — `TaskProgressMessage`
  and `TaskNotificationMessage` included, not only `task_started` — so a `local_bash`/monitor
  notice that happens to carry a `tool_use_id` is never mistaken for a Task sub-agent.
- The map is NOT cleared by `_reset()` (the per-turn reset on `ResultMessage`); it lives for the translator's lifetime. It is keyed per driver lifetime, not per turn: background sub-agents outlive the turn that spawned them and their notices arrive while the driver is idle, through `_idle_listen`, which already feeds every message to the translator.

### 4.3 Driver (`server/sessions/claude_driver.py`)

- `self._subagent_tasks: dict[str, str]` (tool_use_id → task_id), maintained in
  `_handle_msg` from `SubAgentStarted` (add) and `SubAgentEnded` (drop). This is the
  driver's, not the translator's, because the stop path and the permission path both
  need it and the translator is a pure function of the stream.
- `_can_use_tool(tool_name, tool_input, ctx)`: read `getattr(ctx, "agent_id", None)`;
  reverse-map it to a tool-use id; pass `parent_tool_use_id` into the emitted
  `PermissionRequest`, and into `_handle_ask_user_question` → `Question`. Attribution
  is best-effort: an `agent_id` with no live map entry emits an unattributed card, as today.
- `async def stop_subagent(self, tool_use_id: str) -> str | None`: look up the task id;
  returns `None` when unknown, already ended, or there is no live client — never raises.
  Otherwise `await self._client.stop_task(task_id)`, log
  `driver.stop_subagent session=… tool_use_id=… task_id=…`, return the task id.
  `stop_task` is added to `SDKClientProtocol` (`sdk_contract.py`) and to `FakeSDKClient`,
  whose fake records the call and, when a scripted thread is open for that task, injects
  the killed/stopped pair the real CLI emits.

### 4.4 Orchestrator, fleet, RPC, API

- `SessionOrchestrator.stop_subagent(session_id, tool_use_id)` → driver; raises the
  existing "No session with id" for an unknown session. Added to `_PER_SESSION` in
  `fleet_orchestrator.py`, to `_SIMPLE` in `server/ipc/orchestrator_rpc.py`, and as a
  pass-through on `RemoteOrchestrator` and on `InProcessOrchestratorAdapter` in
  `server/sessions/facade.py` (the in-process facade; `test_orchestrator_facade.py`
  pins this) — the same FOUR places `interrupt` lives. The completeness tests that pin
  those lists gain the entry.
- `session_actions.stop_subagent(orch, db, *, session_id, tool_use_id, identity, via)`:
  calls the orchestrator; a `None` return becomes `ActionError("SUBAGENT_NOT_RUNNING",
  "That sub-agent is not running — it may have already finished.", 404)`; audits
  `session.subagent_stop` with `{sessionId, toolUseId, taskId}`.
- `POST /api/sessions/{session_id}/agents/{tool_use_id}/stop` in `server/api/sessions.py`,
  the HTTP twin of nothing (no WS frame: the action is rare and HTTP already carries
  identity and audit). Returns `{"ok": true, "taskId": …}`.

### 4.5 Summary persistence (`server/sessions/subagents.py`)

`summarize_transcript` sets `parentAgentId` to the `parent_tool_use_id` of the Task
`ToolCall` event itself: a Task issued inside another sub-agent's thread carries that
thread's key as its pid. Replay already emits sidecar tool calls with `pid = tuid`, so
nested agents that the sidecar scan finds nest correctly; agents it does not find stay
top-level, as today. The `SubAgentSummary.status` vocabulary is unchanged
(`running|done|failed`); `stopped` maps to `failed` there, since the row's only job is the
dot colour and the list already treats an errored Task result as failed.

### 4.6 Replay (`server/sessions/replay.py`)

Out of scope. The CLI journals no task notices into the transcript, so a reopened session shows tool-result-derived status through `selectSubAgents`'s fallback (§5.3); no replay change is needed. (An earlier draft proposed emitting `SubAgentEnded` for finished sidecars; that duplicated the fallback and was dropped during planning.)

## 5. Web design

### 5.1 Types (`web/src/session/events.ts`, `web/src/api/types.ts`)

`EvSubAgentStarted`, `EvSubAgentProgress`, `EvSubAgentEnded` mirroring §4.1;
`EvPermissionRequest` and `EvQuestion` gain `parentToolUseId?: string | null`.
`SubAgentSummary` unchanged.

### 5.2 Reducer (`web/src/session/reducer.ts`)

`SubThread` gains one optional field:

```ts
export interface SubThreadTask {
  taskId: string; description: string; agentType: string; backgrounded: boolean; depth: number
  status: 'running' | 'completed' | 'failed' | 'stopped'
  startedAt?: number; endedAt?: number
  activity?: string | null; lastTool?: string | null
  totalTokens?: number | null; toolUses?: number | null; durationMs?: number | null
  summary?: string | null
}
export interface SubThread { turns: Turn[]; openBlockId: string | null; model?: string | null; task?: SubThreadTask }
```

The three events are handled in the main `switch` (they are not block events); each
creates the thread if missing. `subAgentEnded` merges: it never regresses a terminal
status to running, and a later Ended overwrites summary/usage (the idempotency §4.2 relies
on). `replayReset` clears `task` with the rest of the thread; `staleSubAgents` carries it
through the double buffer.

`permissionRequest` / `question` with a `parentToolUseId`: in addition to the main-thread
handling (unchanged, so the parent transcript still shows the card), append the same
block to the last assistant turn of that sub-thread (creating one keyed by the permId /
questionId if the thread has no turns yet). `permissionResolved` / `questionResolved`
and the optimistic local resolutions remove the block from **both** places. Block ids are
the permId / questionId, so a card is identified the same way wherever it renders.

### 5.3 Selector (`web/src/session/selectors.ts`)

`selectSubAgents(turns, subAgents?)` merges the thread's `task` into each `SubAgent`:
`status` from `task.status` when present (`completed → 'done'`, `stopped` added to the
`SubAgentStatus` union), else the tool-result derivation as today; `activity`, `lastTool`,
`totalTokens`, `toolUses`, `taskId`, `backgrounded` copied through. `useSessionRuntime`
passes `viewState.subAgents`. This keeps `rt.subAgents` the single derived list that the
strip, the rows and the view all read (Principle VI).

### 5.4 API + hook (`web/src/api/client.ts`, `web/src/api/hooks.ts`)

`stopSubAgent(sessionId, toolUseId)` → `POST /api/sessions/{id}/agents/{toolUseId}/stop`;
`useStopSubAgent()` mutation with the shared error toast.

### 5.5 The sub-agent Reader page (landed as `features/sessions/SubAgentPage.tsx`)

Header, in the order Claude Code's view uses:

- Breadcrumb `parent › {agentType}` (existing) then a title line `{agentType} › {description}`.
- Status badge: running (pulsing dot) / completed / failed / stopped.
- Meta line: `elapsed · N tokens · N tools · last: Bash` — elapsed ticks via the existing
  `useNow`/`agentDuration` while running and freezes at `endedAt`; each number is omitted
  when unknown.
- Progress line while running: `task.activity` in muted text; on end, `task.summary`.
- **Stop** button while running: calls the hook, disables until `subAgentEnded` arrives
  (or the mutation errors, which re-enables it and shows the toast). Hidden once ended.
  Confirmation is not required: the action is scoped to one sub-agent and the CLI hands
  the parent an interrupted result it can react to.
- "Read-only sub-agent" badge removed. The transcript below is unchanged except that
  `onAnswer` is wired to the runtime's question-answer callback and permission blocks get
  the same `PermissionCard` the main transcript uses, so they are answerable here.

`SubAgentList` (strip popover) and the view show the new `stopped` state and, when
present, the activity line in place of the static description — both read LIVE task
state. `SubAgentRows` (the persisted summary, §4.5) shows `running|done|failed` only;
`stopped` is a LIVE-only state and never appears there, since `summarize_transcript`
never emits it (§4.5 maps `stopped → failed` at that layer).

## 6. Error handling

- Stop on an unknown or finished sub-agent → 404 `SUBAGENT_NOT_RUNNING`, shown as a toast;
  the button hides when the thread's status is terminal so this is a race, not a path.
- Stop when the SDK client is gone (session detached) → the orchestrator's existing
  "not live" error; the button is only rendered while the thread has live task state
  (`taskId` set) — a replayed/detached session's `running` status with no paired
  result never renders it, so this case is a narrow race rather than a routine path.
- A notice for a task the translator never saw start (daemon restart mid-run) is dropped;
  the sub-agent falls back to tool-result-derived status. No synthetic Started is minted.
- A permission whose `agent_id` is unmapped renders unattributed in the parent, as today.

## 7. Testing

Server (`server/tests/`):
- `test_translator_subagent_notices.py`: real `TaskStartedMessage` / `TaskProgressMessage`
  / `TaskUpdatedMessage` / `TaskNotificationMessage` objects (builders added to
  `fakes/fake_sdk.py`, shapes copied from the spike log) → the three events, killed→stopped
  mapping, unknown-task drop, non-agent task ignored, map cleared on reset.
- `test_driver_subagent_stop.py`: driver maps started/ended, `stop_subagent` calls the
  fake's `stop_task` with the right id, `KeyError` after Ended; permission from a
  `ctx.agent_id` carries the mapped `parent_tool_use_id`.
- `test_sessions_api_subagent_stop.py`: route 200 with audit row; 404 code on unknown.
- Existing completeness tests for `_PER_SESSION` / `_SIMPLE` / `RemoteOrchestrator`.
- `test_subagents.py`: nested Task call yields `parentAgentId`.

Web (`web/`): reducer cases for the three events, idempotent Ended, card duplication into
the sub-thread and removal from both on resolve, double-buffer carry; selector merge;
the sub-agent Reader page's header, Stop button states, answerable permission card; `SubAgentRows`
stopped state. Full `npm run test`, `typecheck`, `lint`, `build`.

## 8. Rollout

One PR. No migration, no new env, no kill switch: every addition is inert until the CLI
emits a notice, and a CLI older than the notices simply leaves the view as it is today.

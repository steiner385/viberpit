# Child-session dispatch: sub-agents as linked, interactive ViberPit sessions

**Date:** 2026-09-11
**Status:** Approved (brainstorming, Tony 2026-09-11: "approved" — parent MODEL spawns children through a session tool; a UI entry point comes later) — pending implementation plan
**Builds on:** `docs/superpowers/specs/2026-09-11-subagent-live-view-design.md` (piece 1, merged in #1553: live view for in-process Task sub-agents) and `docs/product/features/session-tools.md` (the session tool surface this adds a tool to)

## 1. Problem

Piece 1 gave Task sub-agents a live view, a Stop button and answerable cards — everything
Claude Code's own agent view offers. It could not give them the one thing Tony asked for,
"interact with them": a Task sub-agent is a thread inside one `claude` process, and the CLI
drops any user frame addressed to it (piece 1 spec §1). No harness can talk to it.

ViberPit already has a fully interactive unit: the session. A session has its own prompt
dock, permission and question cards, model switching, stop, account rotation, a worktree,
and a row on the board. Claude Code itself uses separate sessions (background agents) for
its interactive case rather than Task threads. So the interactive path is: let a coding
session spawn **child sessions** — real ViberPit sessions linked to the parent — instead of
Task threads, for work the user must be able to steer independently.

## 2. Goals and non-goals

Goals:

1. A Claude coding session's model can call `dispatch_session(name, prompt, …)` and get
   back a running child session (same repo, own worktree, same owner), linked to the parent.
2. The child is a normal session everywhere: the board, the detail view, the prompt dock,
   permissions, model switching, stop, archive. Nothing about it is a second
   implementation of a session.
3. The parent learns about its children without polling: when a child finishes, hands
   back, or ends, the parent receives a prompt (a `<child-session-notification>`), the
   same shape the CLI uses for its own `<task-notification>`, carrying the child's
   last message. `session_context` also lists the parent's children with status.
4. The board shows lineage: a child card carries a "child of <parent>" badge that links to
   the parent; the parent card lists its children with status, each linking to the child.
5. Bounded fan-out: a per-parent cap on live children and a maximum nesting depth, both
   env knobs; a kill switch drops the tool.

Non-goals:

- **A user-facing "dispatch as session" button.** Approved as a later entry point; the
  orchestrator method this spec adds is the seam it will call.
- Returning the child's result inline to the parent's tool call (the Agent tool's
  contract). The parent gets a notification prompt instead; if it needs the child's full
  transcript it asks the user, who can read it on the board.
- Cross-host placement. A child is created on the parent's host by the parent's own
  daemon orchestrator (the same `create_session` the fleet RPC already calls there).
- Codex parents. Codex lacks `durable_tool_surface`; it keeps markers for signals and
  gets no dispatch tool. Codex *children* are not spawned either: the child inherits the
  parent's engine, and the parent is always Claude.
- Cascading actions: archiving or completing a parent does not stop its children. They
  are independent sessions; the user acts on them directly. (Revisit if fan-out grows.)
- Hiding children from the top-level list. A child is a session; the board never hides a
  session on a guess (`work_class_for_origin`'s rule). Lineage is shown, not collapsed.

## 3. Verified surface this builds on (no new spikes needed)

- **Session tools** (`server/sessions/tools/`): in-process MCP tools, one `ToolSpec` per
  tool in `registry.py` (`ToolClass.READ | MUTATION | DESTRUCTIVE | SIGNAL`), handlers in
  `tools.py` wrapped by `guarded` (audit `session.tool_call`, metrics, `ToolArgError` →
  corrective message). Handlers get a `SessionToolContext` (session id, identity, engine,
  `record()` accessor, host, capability flags) built in `SessionOrchestrator` (~line 1464).
  `mounted_names()` in `registry.py` is THE predicate for which tools a session gets, read
  by both `build_session_tools` and the system-prompt paragraph in `tools/instruction.py`.
- **Permission classes**: a MUTATION tool of our own MCP server is gated on its
  annotation, not the session mode (`PermissionRegistry.request`, spec §4.4 of the
  assistant design): it raises a card in interactive mode and is auto-allowed in
  full-auto. The assistant drawer's `create_session` is exactly such a MUTATION tool.
- **Session creation**: `SessionOrchestrator.create_session(name, account, initial_prompt,
  cwd, branch, repo, identity, permission_mode, attachment_ids, name_is_auto, origin,
  origin_ref, model, reasoning_effort, kind, owner_identity, engine, client_node, display)`
  → `SessionRecord`. Capacity is handled inside it (queue when full).
- **Notifying a session**: `ensure_driver(session_id, identity)` resumes a detached session;
  `send_prompt(session_id, text)` queues the text behind a live turn (FIFO) or starts a
  turn — the same delivery the CLI's task notifications get.
- **Status persistence**: every driver status lands in `_persist_status(session_id,
  status)`, which already hosts the completion side effects (driver release,
  auto-archive, autopilot nudge, queue drain). `background_tasks.last_assistant_text(path)`
  returns a session's last spoken message from its transcript.
- **Schema rule**: a new NULLABLE column on `Session` is picked up by `init_db`'s reconcile
  on SQLite and Postgres; a migration revision is added as the reviewable record only.

## 4. Design

### 4.1 Data model

`server/db/models.py` `Session`, two nullable columns (reconcile-safe, no NOT NULL):

- `parent_session_id: str | None` — the session that dispatched this one. NULL for every
  session that exists today and every human-created one.
- `parent_notified_status: str | None` — the child's status most recently reported to
  the parent (idempotency for §4.4 across restarts and repeated `_persist_status` calls).

`server/sessions/types.py` `SessionRecord` gains the same two fields; both serializers
(`SessionRecord.to_dict` for daemon→edge RPC and `_record_to_dict` in
`server/api/sessions.py` for the browser) emit `parentSessionId`. `parent_notified_status`
stays server-side. Migration `server/db/migrations/versions/<rev>_child_sessions.py`
chained off the newest revision.

Depth is derived, never stored: `depth(session) = 0` when `parent_session_id` is None,
else `1 + depth(parent)`, walking `_records` (a parent is always in the record map or the
DB row; a missing parent counts as depth 1).

### 4.2 The tool

`server/sessions/tools/registry.py`:

```python
ToolSpec("dispatch_session", ToolClass.MUTATION, "Start a child session"),
```

Mounted only when `ctx.signalling_tools` (Claude) AND `ctx.child_sessions` (a new context
flag = `get_settings().child_sessions_enabled`, env `CCSWITCH_CHILD_SESSIONS_ENABLED`,
default true). `mounted_names()` grows a `child_sessions: bool` keyword; `tools/instruction.py`
reads the same predicate, so the system-prompt paragraph names the tool exactly when it is
mounted, and its `_BLURBS` index gains one thin clause for `dispatch_session` (the
coverage test `test_session_tools_instruction.py` requires it). The wire name is
`mcp__<brand>__dispatch_session`, same prefix as the others.

Description (the model's whole contract; exact text is the plan's to finalise):

> Start a CHILD session of this one: a separate ViberPit session on the same repo, in its
> own worktree, owned by the same user, that the user can open, steer, approve and stop
> like any session. Use it for a unit of work the user must be able to interact with
> independently (a sub-task that will need its own decisions, review or long runtime);
> keep using the Agent tool for quick, self-contained research. `name` (3–60 chars): the
> board title. `prompt`: the child's complete brief — it does not see your conversation.
> `model` (optional): tier alias; default is this session's model. `permission_mode`
> (optional): never wider than this session's. Returns the child's id and URL and returns
> immediately; you get a `<child-session-notification>` prompt when the child completes,
> hands back or ends, carrying its last message. Call `session_context` to list your
> children and their status. At most N live children (N from the cap) and no
> grandchildren beyond depth D.

Schema: `{name: string, prompt: string, model?: string, permission_mode?: string}`,
required `name`, `prompt`.

Handler (`tools.py`), ONE call, no business logic:

```python
@guarded
async def dispatch_session(args):
    return await ctx.dispatch_child(name=…, prompt=…, model=…, permission_mode=…)
```

`ctx.dispatch_child` is a callable the orchestrator sets on `SessionToolContext`
(`dispatch_child: Callable[..., Awaitable[dict]] | None = None`), bound to
`SessionOrchestrator.dispatch_child_session(parent_id=session_id, identity=identity, …)`.
Arg validation that needs no state (name length, non-empty prompt, `model` in
`MODEL_TIERS` when given, `permission_mode` a known mode) raises `ToolArgError` in the
handler, as `request_model` does.

### 4.3 `SessionOrchestrator.dispatch_child_session`

```python
async def dispatch_child_session(self, parent_id: str, *, name: str, prompt: str,
                                 model: str | None, permission_mode: str | None,
                                 identity: str) -> dict:
```

1. Parent must be a known record; else `KeyError` → tool error.
2. Limits (`server/sessions/child_sessions.py`, pure, unit-tested):
   `check_limits(parent_depth, live_children, cap, max_depth)` raises
   `ChildSessionLimit(reason)` with a model-readable message
   ("this session already has 3 live children (cap 3); wait for one to finish" /
   "children may not spawn grandchildren beyond depth 2"). Live = a child whose status is
   not in `{"complete", "done", "ended", "archived"}`. Knobs: `CCSWITCH_CHILD_SESSION_CAP`
   (default 3), `CCSWITCH_CHILD_SESSION_MAX_DEPTH` (default 2), read through
   `get_settings()` like the other caps.
3. Permission mode: `narrowest(requested, parent_mode)` — the child never runs wider than
   the parent. `server/sessions/permissions.py` has no width ordering today (only the SDK
   passthrough set), so it gains ONE: `MODE_WIDTH: dict[str, int]` over the ccswitch modes
   (`plan` < `interactive` < `auto` < `full-auto`; an unknown mode ranks as `full-auto` so
   it can never be chosen as "narrower") and `narrowest(a, b)`. It lives in
   `permissions.py` because that module owns mode semantics; `child_sessions.py` imports it.
4. `rec = await self.create_session(name=name, initial_prompt=prompt, repo=parent.repo,
   branch=None, identity=identity, permission_mode=mode, name_is_auto=False,
   model=model or parent.model, engine=parent.engine, owner_identity=None)` — a fresh
   worktree of the parent's repo off the default branch (the parent's own branch is its
   worktree; two sessions must not share one).
5. Stamp `rec.parent_session_id = parent_id` on the in-memory record and the DB row (one
   `UPDATE`), audit `session.child_dispatch` `{parentSessionId, childSessionId, name}`
   with `via="session"`, log at info.
6. Return `{"id": rec.id, "name": rec.name, "url": f"/sessions/{rec.id}", "status": rec.status}`
   (a child that landed in the capacity queue reports `"queued"`; the parent is told it
   will start when a slot frees).

The kill switch is enforced here too (a request with the switch off raises
`ChildSessionLimit("child sessions are disabled")`), so a stale tool declaration cannot
bypass it.

### 4.4 Parent notification

In `_persist_status`, after the row is written and before the existing completion side
effects:

```python
parent_id = getattr(rec, "parent_session_id", None)
if parent_id and status in CHILD_NOTIFY_STATUSES and rec.parent_notified_status != status:
    rec.parent_notified_status = status      # + one UPDATE
    self._spawn(self._notify_parent(parent_id, rec, status), name="child-notify")
```

`CHILD_NOTIFY_STATUSES = {"complete", "done", "waiting_question", "ended"}` — the child
stopped and either finished or needs someone. Status changes that are not a stop (busy,
idle→busy, queued→busy) never notify. A child that hands back twice in a row is reported
once per transition (the guard compares to the last reported status; a `busy` in between
resets it because `busy` is not in the set — the plan tests this exact sequence).

`_notify_parent(parent_id, child_rec, status)`:
- Parent record missing or archived/ended → log and return (the child stays a normal
  session for the user).
- `text = build_child_notice(child_rec, status, last_message, hand_back_reason)` where
  `last_message = background_tasks.last_assistant_text(transcript_path)` truncated to
  800 chars, `hand_back_reason` is read from the child's DRIVER at persist time
  (`driver.last_hand_back_reason` — the record does not persist it; `_persist_status`
  already reads it there for the audit row), and `pr_resolution` from the record when
  set. Pure function in `child_sessions.py`, unit-tested:

  ```
  <child-session-notification>
  <session-id>{id}</session-id>
  <name>{name}</name>
  <status>{status}</status>
  <hand-back-reason>{reason}</hand-back-reason>   (only when present)
  <pr-resolution>{resolution}</pr-resolution>     (only when present)
  <summary>{last_message}</summary>
  <url>/sessions/{id}</url>
  </child-session-notification>
  ```
- `await self.ensure_driver(parent_id, identity)` FIRST (a bare `send_prompt` returns
  `"no_driver"` and drops the text for a detached parent), then `await
  self.send_prompt(parent_id, text)`. A parent mid-turn receives it at the end of its turn
  (FIFO queue, the CLI's own task-notification semantics); an idle or detached parent is
  woken. Failures are logged,
  never raised (this runs inside the child's status callback).
- The notice is a plain prompt on purpose: nothing new on the wire, it shows in the
  parent's transcript as a user turn the way task notifications do, and the parent's model
  reacts to it with the ordinary turn machinery.

### 4.5 `session_context`

`context_snapshot(ctx)` adds `"parentSessionId"` and `"children": [{"id", "name",
"status"}]` (children = records whose `parent_session_id == ctx.session_id`, in creation
order). The orchestrator passes a `children()` accessor on the context, mirroring the
`record()` accessor. This is how a parent waits or reconciles without a new tool.

### 4.6 Web

- `web/src/api/types.ts` `Session.parentSessionId?: string | null`.
- `SessionCard.tsx`: a child card shows a small "child of {parent name}" pill next to the
  provenance badge, linking to `sessionPath(parentSessionId, scope)`; the parent name is
  looked up from the loaded sessions list (fallback: short id). A parent card gets a
  "Children" section under the existing sub-agent rows: one row per child (status dot,
  name, status label) navigating to the child's session path. Children are derived on
  the client from the sessions list (`sessions.filter(s => s.parentSessionId === id)`);
  no new wire field beyond `parentSessionId`. Rows live in a new `ChildSessionRows.tsx`
  beside `SubAgentRows.tsx` — same visual language, different link target.
- Ordering/grouping unchanged: children remain top-level rows.
- `SessionMeta`/header: the detail view of a child shows the same "child of" pill.

### 4.7 Config and kill switch

`server/config.py` `Settings`: `child_sessions_enabled: bool = True`
(`CCSWITCH_CHILD_SESSIONS_ENABLED`), `child_session_cap: int = 3`
(`CCSWITCH_CHILD_SESSION_CAP`), `child_session_max_depth: int = 2`
(`CCSWITCH_CHILD_SESSION_MAX_DEPTH`). Documented in `docs/product/features/session-tools.md`
(tool list + kill switch) and a short `docs/product/features/child-sessions.md`.

## 5. Error handling

| Case | Behaviour |
|---|---|
| Cap or depth exceeded, switch off | Reported as the tool result (`ChildSessionLimit` → `tool_err` message); nothing created. Order of gates: the permission layer raises the MUTATION card BEFORE the handler runs, so a denied card is the user's answer and never reaches the orchestrator; a limit hit after an allow is the orchestrator's answer. Stateless arg checks (`ToolArgError`) also run in the handler, after the card. |
| `create_session` raises (`AllAccountsBusy`, clone/branch errors) | Tool error with the exception's message; audit `session.child_dispatch_failed`. |
| Parent archived/ended when the child finishes | Notification skipped, logged; `parent_notified_status` still written so a later un-archive does not replay it. |
| Parent notification `send_prompt` fails | Logged, swallowed; the status guard means it is not retried (a retry loop inside a status callback is worse than a missed notice — the child is visible on the board either way). |
| Child never leaves `queued` | No notification until it runs; the parent sees `"queued"` in `session_context`. |

## 6. Security

The child is owned by the parent's identity; `dispatch_child_session` takes `identity`
from the tool context, never from args. The tool cannot choose the account (the selector
does) or the host. Permission mode can only narrow. The MUTATION class means an
interactive parent asks the human before spawning; a full-auto parent is already trusted
to act. Audit rows tie every child to its parent.

## 7. Testing

Server: `child_sessions.py` pure tests (limits, `narrowest`, notice text incl. optional
fields and truncation); orchestrator tests with the fake SDK factory — dispatch stamps
`parent_session_id` and audits, cap and depth refusals, permission narrowing, kill switch;
`_persist_status` notification: once per transition, `busy` resets the guard, skipped for
archived parent, delivered via `send_prompt` to a resumed parent; tool handler tests (arg
validation, mount gating by capability and switch, `session_context.children`); API wire
(`parentSessionId` in both serializers); schema tests (`_REQUIRED_EXTRAS` untouched since
both columns are nullable; reconcile picks them up); instruction paragraph names the tool
only when mounted. Web: `ChildSessionRows`, the "child of" pill, `SessionCard` children
section; full `npm run test`.

An eval case in `docs/superpowers/evals/session-tools/cases.jsonl` naming `dispatch_session`
in `expect_tools` — `test_every_session_tool_has_a_spec_and_an_eval_case` fails otherwise.

## 8. Rollout

One PR behind `CCSWITCH_CHILD_SESSIONS_ENABLED` (default on; flip to off in prod env if a
parent misuses it). No data migration: both columns are nullable and every existing row
is a root.

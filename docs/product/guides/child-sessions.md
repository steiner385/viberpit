# Child sessions

**Spec:** `docs/superpowers/specs/2026-09-11-child-session-dispatch-design.md`

A **child session** is an ordinary ViberPit session that another session's model created
through the `dispatch_session` tool. It is linked to its parent by `Session.parent_session_id`
and is otherwise a normal session: same repo (fresh worktree), same owner, its own prompt dock,
permission and question cards, model switching, stop, archive, and a row on the board.

## Why

Piece 1 (#1553) gave in-process Task sub-agents a live view, a Stop button and answerable
cards — everything Claude Code's own agent view offers. It cannot give them text input: the
CLI drops any user frame addressed to a Task thread. When the user must be able to steer a
sub-task independently, the model dispatches it as a child session instead.

## The tool

`dispatch_session(name, prompt, model?, permission_mode?)` — `ToolClass.MUTATION` on the
session tool surface (`server/sessions/tools/`). Mounted only on the durable surface (Claude)
and only while `VIBERPIT_CHILD_SESSIONS_ENABLED` is on. Like every MUTATION tool of our own
MCP server, it raises a permission card in **every** session mode — full-auto included —
until the user grants "always allow" for it (`PermissionRegistry.request` checks our own
tools before the full-auto shortcut). An unattended full-auto parent therefore parks on an
"Answer needed" card the first time it dispatches; allow it once with "always allow" and later
dispatches go through. The handler makes ONE call, `SessionOrchestrator.dispatch_child_session`,
which:

- refuses over the cap (`VIBERPIT_CHILD_SESSION_CAP`, default 3 live children per parent) or
  beyond the depth (`VIBERPIT_CHILD_SESSION_MAX_DEPTH`, default 2) — the refusal comes back as
  the tool result, nothing is created;
- narrows the requested permission mode to the parent's (`permissions.narrowest`; a child never
  runs wider than its parent);
- creates the session with `create_session` (parent's repo → fresh worktree; a git-tree parent
  with no repo column → a nested worktree; a scratch parent → the child's own scratch
  directory, never a shared one; parent's model by default, `name_is_auto=False`), stamps
  `parent_session_id`, and audits `session.child_dispatch {parentSessionId, childSessionId,
  name}`. A child dispatched at capacity is queued and reports `"queued"`; its lineage
  survives the queue → spawn hand-off.

The model is told to use it for work the user must interact with independently and to keep
using the Agent tool for quick, self-contained research. The eval case
`dispatch-a-steerable-subtask` in `docs/superpowers/evals/session-tools/cases.jsonl` pins that.

## The parent hears back

When a child reaches `complete`, `waiting_question`, `done`, `ended` or `archived`, the
orchestrator delivers a prompt to the parent (resuming it if detached — but never a parent
that is `complete`, `ended` or `archived`, which would resurrect a finished session):

```
<child-session-notification>
<session-id>…</session-id>
<name>…</name>
<status>waiting_question</status>
<hand-back-reason>…</hand-back-reason>   (when the child handed back)
<pr-resolution>…</pr-resolution>         (when the child declared one)
<summary>the child's last message, ≤ 800 chars</summary>
<next-step>…</next-step>                 (when the parent can act: see below)
<url>/sessions/…</url>
</child-session-notification>
```

Which stops notify is decided by `child_sessions.notify_decision` and persisted as
`Session.parent_notified_status`:

- `done` is what every ended turn produces, so it notifies only for the child's FIRST stop
  and then latches — once the user is steering the child, its later turn-ends do not wake
  the parent.
- `complete`, `waiting_question`, `ended` and `archived` notify on every change of status.
- After a hand-back (`waiting_question`) the child running again clears the guard, so the
  stop that follows the user's answer is reported.
- `ended`/`archived` come from `close_session` / `archive_session`, which write their status
  directly; both call the same decision before the record is popped.

`session_context` lists the parent's `children` (id, name, status) and its own `parentSessionId`.

## The parent answers back

A child that hands back (`handed_back` / `waiting_question`) stalls until somebody prompts it.
The parent can, with **`reply_to_child(child_session_id, message)`** (#2911): the message becomes
the child's next prompt, framed as a `<parent-session-message>` so the child knows its parent —
not the user — is talking. One tool covers answering the question, resuming the child,
telling it to stop because the parent is taking the work over, and re-delegating with new
instructions. It refuses (as the tool result) a session that is not the caller's child, a child
that has ended or been archived, and a delivery the daemon refused (a deploy drain). It is a
SIGNAL-class tool — no permission card — because it only prompts a session the parent already
created, owned by the same user and never wider in mode; it rides with `dispatch_session`. The
notice's `<next-step>` line names it for every stop the parent can act on.

When the parent cannot act, the human is told directly: a hand-back whose parent is `complete`,
`ended` or `archived`, or whose notice could not be delivered, fires a `needs_input`
notification naming the child ("…its parent session has finished — answer it directly") and
writes a `session.child_escalated` audit row.

## Across daemon restarts

A notice the deploy drain refuses is not lost: the child's `parent_notified_status` guard rolls
back, and at daemon boot `redeliver_child_notices` (after re-arm and auto-resume) reports every
child stop its parent has not heard about — through the same guard, so a reported stop never
repeats. A child the restart cut off mid-turn that auto-resume did not restart is reported as
`<status>interrupted_by_restart</status>` with a `reply_to_child` resume hint. If the parent was
itself interrupted, that notice is not allowed to wake it: it is queued in front of the
parent's own next turn instead.

## On the board

A child card shows a "child of <parent>" pill linking to the parent; a parent card lists its
children under the sub-agent rows, each linking to the child. Children stay top-level rows —
the board never hides a session.

## Not in this version

A user-facing "dispatch as session" button (the orchestrator method is the seam for it; note
it runs in-process in the daemon that owns the parent, so an edge-side button will need
`dispatch_child_session` and `children_of` added to the fleet RPC surface), cascading
stop/archive from parent to children, Codex parents, and inline result return.

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
and only while `CCSWITCH_CHILD_SESSIONS_ENABLED` is on. Interactive parents get a permission
card before a child is created; full-auto parents do not. The handler makes ONE call,
`SessionOrchestrator.dispatch_child_session`, which:

- refuses over the cap (`CCSWITCH_CHILD_SESSION_CAP`, default 3 live children per parent) or
  beyond the depth (`CCSWITCH_CHILD_SESSION_MAX_DEPTH`, default 2) — the refusal comes back as
  the tool result, nothing is created;
- narrows the requested permission mode to the parent's (`permissions.narrowest`; a child never
  runs wider than its parent);
- creates the session with `create_session` (parent's repo, fresh worktree, parent's model by
  default, `name_is_auto=False`), stamps `parent_session_id`, and audits
  `session.child_dispatch {parentSessionId, childSessionId, name}`.

The model is told to use it for work the user must interact with independently and to keep
using the Agent tool for quick, self-contained research. The eval case
`dispatch-a-steerable-subtask` in `docs/superpowers/evals/session-tools/cases.jsonl` pins that.

## The parent hears back

When a child reaches `complete`, `done`, `waiting_question` or `ended`, `_persist_status`
delivers a prompt to the parent (resuming it if detached):

```
<child-session-notification>
<session-id>…</session-id>
<name>…</name>
<status>waiting_question</status>
<hand-back-reason>…</hand-back-reason>   (when the child handed back)
<pr-resolution>…</pr-resolution>         (when the child declared one)
<summary>the child's last message, ≤ 800 chars</summary>
<url>/sessions/…</url>
</child-session-notification>
```

Once per stop transition: `Session.parent_notified_status` remembers the last status reported;
a non-stop status in between (the child ran again) clears it. An archived or ended parent is
not woken. `session_context` lists the parent's `children` (id, name, status) and its own
`parentSessionId`.

## On the board

A child card shows a "child of <parent>" pill linking to the parent; a parent card lists its
children under the sub-agent rows, each linking to the child. Children stay top-level rows —
the board never hides a session.

## Not in this version

A user-facing "dispatch as session" button (the orchestrator method is the seam for it),
cascading stop/archive from parent to children, Codex parents, and inline result return.

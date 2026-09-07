---
slug: session-tools
title: Session Tool Surface (session_context, report_feedback, mark_complete, mark_no_pr, request_model)
state: delivering
links:
  specs:
    - docs/superpowers/specs/2026-09-06-session-feedback-to-backlog-design.md
    - docs/superpowers/specs/2026-09-07-marker-to-tool-migration-design.md
  code:
    - server/mcp_spec.py
    - server/sessions/tools/registry.py
    - server/sessions/tools/tools.py
    - server/sessions/tools/context.py
    - server/sessions/tools/server.py
    - server/sessions/tools/instruction.py
    - server/sessions/tools/codex.py
    - server/sessions/feedback.py
    - server/sessions/completion.py
    - server/sessions/model_request.py
    - server/sessions/engine.py
---

# Session Tool Surface (session_context, report_feedback, mark_complete, mark_no_pr, request_model)

**Problem** — A coding session run by ccswitch has no way to ask "what am I, and why was I
dispatched" or to tell ccswitch itself (not the repo it's working in) that something about the
harness is broken — a wrong dispatch prompt, a contradictory instruction, a mistimed model switch.
Today that only reaches a human as a stuck session or a bad PR, well after the fact. Separately,
until the 2026-09-07 marker-to-tool migration, a session's completion, no-PR resolution and
mid-session model request were all encoded as HTML-comment markers in its own closing prose (`<!--
viberpit:complete -->` and friends) — prose a regex had to recover exactly, rather than a
declaration the session made on purpose.

**Target users** — every coding session ccswitch runs (Claude and, on fresh threads, Codex), and
the operators who triage `session_feedback` rows into the backlog.

**Desired outcome** — every coding session mounts an in-process `ccswitch` MCP server with up to
five tools:

| Tool | Class | Args | What it does |
|---|---|---|---|
| `session_context` | READ | none | id, name, repo, branch, cwd, host, engine, model, account, dispatch origin/ref, effective goal, permission mode |
| `report_feedback` | READ | category, impact, title, body, evidence? | Reports a problem with ccswitch itself (not the project). One call per distinct problem, at most `session_feedback_cap` per session. |
| `mark_complete` | SIGNAL | none | Declares the session's objective is fully done. |
| `mark_no_pr` | SIGNAL | resolution (`not-needed` \| `cancelled`), note? | Declares the work is finished but no PR is appropriate. |
| `request_model` | SIGNAL | model (`haiku`\|`sonnet`\|`opus`\|`fable`), reason | Asks to move to a different model tier. |

`session_context` and `report_feedback` are `ToolClass.READ` in `server/mcp_spec.py`'s shared
registry — read-only in effect, since neither changes anything the user can see. `mark_complete`,
`mark_no_pr` and `request_model` are the newer `ToolClass.SIGNAL` (below): also no permission
card, but not annotated read-only, because each one moves something the user does see (a status
badge, a completion-bar verdict, a pending model approval). `PermissionRegistry` resolves both
classes silently in every session mode, never raising a permission card (pinned by
`server/tests/test_permissions_session_tools.py`). Handlers close over a `SessionToolContext` built
in the daemon process that owns the session — no loopback HTTP call, no bearer token — so the
surface works identically on any fleet host, including rocky.

**Success metrics** — a session can answer "how was I dispatched" and file a feedback row without
leaving its own turn; `server/tests/test_session_tools_real_binary.py` (gated on
`CCSWITCH_TEST_CLAUDE_MCP=1`) verifies against the real `claude` binary, per Constitution III, that
a project's own `.mcp.json` servers and `ccswitch` coexist and `report_feedback` raises no prompt.

**Non-goals (Phase 1)** — filing to GitHub (Phase 2's automation), an API/UI/assistant view of
`session_feedback`, and `session_url` in the context snapshot (needs the edge's public URL, known
only to the Phase 2 filer).

## The `SIGNAL` tool class, and why it renders no card

`ToolClass.SIGNAL` (`server/mcp_spec.py`) exists for a tool that reports the session's OWN state
back to the harness — completion, a no-PR resolution, a model request — as opposed to `READ`
(changes nothing) or `MUTATION`/`DESTRUCTIVE` (changes something in the project or account). Two
things distinguish it from `READ`:

- **No permission card**, same as `READ` (`server/sessions/permissions.py:159` short-circuits both
  classes together). This is the *conservative* choice, not the permissive one: these three tools
  replace HTML-comment markers, which had no gate whatsoever, and a card at the end of every
  session would block an unattended run's completion behind a prompt nobody is there to answer.
- **Not annotated read-only.** `annotations_for` sets `readOnlyHint=False` for `SIGNAL` (only
  `READ` gets `True`) — a signal moves a status badge or a completion-bar verdict, and claiming
  that is read-only would be false, even though it gets the same no-card treatment as `READ` for
  the unattended-completion reason above.

## Where the signal is read — the Translator, not the handler

A tool **handler** for `mark_complete` / `mark_no_pr` / `request_model` only validates its
arguments (via the shared parsers in `server/sessions/completion.py` and
`server/sessions/model_request.py`) and returns `{"acknowledged": true, ...}`. It does **not** act
on the signal. The authoritative read is the Claude **Translator** (`server/sessions/translator.py`)
observing the `ToolUseBlock` directly, the same place it already scans turn text for markers.

This split exists for the sub-agent guard: the Translator only honors a signal when
`parent_tool_use_id is None`, because a `Task` sub-agent calling `mark_complete` means its own
sub-objective finished, not the session's. Session tools are mounted for sub-agents too, and an SDK
tool handler only ever sees its own arguments — it cannot tell who called it. A sub-agent's
signalling call still renders as an ordinary tool card and has no effect on the session's status.

Tool-call state is turn-scoped, not message-scoped, and precedence is tool-over-marker: if a turn
carries both a signalling tool call and a stray marker in its prose, the tool call wins.

## The engine split: which sessions get tools, which keep markers

Which contract a session gets is decided by `AgentCapabilities.durable_tool_surface`
(`server/sessions/engine.py`):

| | Claude | Codex |
|---|---|---|
| `durable_tool_surface` | `True` | `False` |
| Signalling tools (`mark_complete`, `mark_no_pr`, `request_model`) declared | yes | no |
| Instruction taught | tool descriptions | HTML-comment markers |
| `session_context` / `report_feedback` | yes | yes (fresh threads only) |

Codex's `thread/resume` carries neither `developerInstructions` nor `dynamicTools` (verified
against codex-cli 0.153.2 in
`docs/superpowers/spikes/2026-09-06-codex-resume-dynamic-tools.md`): a resumed Codex session keeps
whatever instructions it started with and loses any tools it was told about, with nothing sendable
to correct it. Telling a Codex session "call `mark_complete`" would leave it unable to signal
completion at all after its first resume. So Codex keeps the marker contract permanently, and
Claude — whose system-prompt append survives a resume — gets the tools. No session is ever told
about a surface it doesn't have.

## The permanent marker fallback

The marker parsers (`server/sessions/completion.py`, `server/sessions/model_request.py`) are not
scaffolding for this migration — they stay wired permanently, for three reasons:

1. **Codex sessions emit markers by design** (the engine split above).
2. **Stored transcripts are re-parsed on replay.** Every session that ran before this migration
   wrote markers, and replay must still recover them.
3. **A Claude session resumed across a deploy** carries the system prompt it started with for its
   remaining turns — if that was the marker-instruction prompt, the harness must still be able to
   read a marker from it.

`strip_markers()` also stays: prose heuristics elsewhere in the orchestrator still need to see past
a marker even on a Claude session that mostly signals via tools now. Which contract a session was
*taught* changed with this migration; the harness's ability to *read* either one did not.

**Notes** —
- **Kill switch** — `CCSWITCH_SESSION_TOOLS_ENABLED` (default `true`); off drops the overlay at the
  next session build (no in-flight session is torn down) and, with it, the three signalling tools —
  at which point every session falls back to marker instructions, exactly as a Codex session does.
  There is no separate kill switch for the signalling tools alone (spec §10): this migration is
  reversible today only by disabling the whole surface.
- **Feedback row lifecycle (Phase 1)** — `record_feedback` (`server/sessions/feedback.py`) dedupes
  exact repeats by `(session_id, fingerprint)` → `status="duplicate"`, else `"pending"` or
  `"capped"` against `session_feedback_cap`; Phase 2 adds `"filed"`/`"merged"`/`"throttled"`/
  `"dismissed"` as the automation takes rows off `pending`.
- **Codex resume gap** — Codex declares `session_context`/`report_feedback` as `dynamicTools` on a
  fresh `thread/start` only; `thread/resume` silently drops them (no schema field, no rejection), so
  a resumed Codex thread has none until respawned. Confirmed against codex-cli 0.153.2 in
  `docs/superpowers/spikes/2026-09-06-codex-resume-dynamic-tools.md`. The same gap is why Codex never
  gets the three signalling tools at all (the engine split above) rather than losing them on resume.
- **`pr_resolution_source`** gained a fourth value, `"tool"`, alongside `"marker"`, `"classifier"`,
  `"queue-drain"` — a session's own no-PR claim is a claim regardless of which transport carried it.
  `automations.completion_bar.SELF_CERTIFIED_SOURCES` is `frozenset({"marker", "tool"})`, so a
  tool-sourced `not-needed` is still treated as an unverified self-resolution, not a free pass.

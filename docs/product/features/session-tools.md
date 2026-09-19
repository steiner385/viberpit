---
slug: session-tools
parent: session-workspaces
title: Session Tool Surface (session_context, report_feedback, mark_complete, mark_no_pr, hand_back, set_hold, propose_decision, request_model)
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
    - server/pm/holds.py
---

# Session Tool Surface (session_context, report_feedback, mark_complete, mark_no_pr, hand_back, set_hold, propose_decision, request_model)

Give coding agents reliable application context and a supported way to report feedback, coordinate child work, and signal progress.

**Problem** — A coding session run by ViberPit has no way to ask "what am I, and why was I
dispatched" or to tell ViberPit itself (not the repo it's working in) that something about the
harness is broken — a wrong dispatch prompt, a contradictory instruction, a mistimed model switch.
Today that only reaches a human as a stuck session or a bad PR, well after the fact. Separately,
until the 2026-09-07 marker-to-tool migration, a session's completion, no-PR resolution and
mid-session model request were all encoded as HTML-comment markers in its own closing prose (`<!--
viberpit:complete -->` and friends) — prose a regex had to recover exactly, rather than a
declaration the session made on purpose.

**Target users** — every coding session ViberPit runs (Claude and, on fresh threads, Codex), and
the operators who triage `session_feedback` rows into the backlog.

**Desired outcome** — every coding session mounts an in-process `viberpit` MCP server with up to
nine tools:

| Tool | Class | Args | What it does |
|---|---|---|---|
| `session_context` | READ | none | id, name, repo, branch, cwd, host, engine, model, account, dispatch origin/ref, effective goal, permission mode |
| `report_feedback` | READ | category, impact, title, body, evidence? | Reports a problem with ViberPit itself (not the project). One call per distinct problem, at most `session_feedback_cap` per session. |
| `mark_complete` | SIGNAL | none | Declares the session's objective is fully done. |
| `mark_no_pr` | SIGNAL | resolution (`not-needed` \| `cancelled` \| `awaiting-decision`), note? | Declares the work is finished but no PR is appropriate — or, with `awaiting-decision`, that it stopped on a `propose_decision` the person must answer. |
| `hand_back` | SIGNAL | reason | Declares the session needs the user (a decision, review, approval or information) before the objective can be finished; lands at `waiting_question` ("Answer needed"). |
| `set_hold` | SIGNAL | kind (`evidence`\|`stronger_model`\|null), reason, ref? | Holds (or releases) the issue this session was dispatched for, on the roadmap ladder (spec 2026-09-18 §8). Limited to the two agent-settable kinds — see below. |
| `propose_decision` | SIGNAL | options (2–6 × {key, label, detail?}), recommended_key, rationale | Parks this session's issue on a PRODUCT DECISION only a person can take: draft the options, name the one you recommend, say why. The person picks one in the Inbox (spec 2026-09-18 §6). |
| `request_model` | SIGNAL | model (`haiku`\|`sonnet`\|`opus`\|`fable`), reason | Asks to move to a different model tier. |
| `dispatch_session` | MUTATION | name, prompt, model?, permission_mode? | Starts a linked **child session** (same repo, own worktree, same owner) the user can steer like any session; the parent is notified by a `<child-session-notification>` prompt when the child stops. Claude only; gated by `CCSWITCH_CHILD_SESSIONS_ENABLED`, capped by `CCSWITCH_CHILD_SESSION_CAP` / `_MAX_DEPTH`. Raises a permission card in interactive mode. See [Child sessions](../guides/child-sessions.md). |

`session_context` and `report_feedback` are `ToolClass.READ` in `server/mcp_spec.py`'s shared
registry — read-only in effect, since neither changes anything the user can see. `mark_complete`,
`mark_no_pr`, `hand_back`, `set_hold`, `propose_decision` and `request_model` are the newer `ToolClass.SIGNAL` (below): also no permission
card, but not annotated read-only, because each one moves something the user does see (a status
badge, a completion-bar verdict, a pending model approval). `PermissionRegistry` resolves both
classes silently in every session mode, never raising a permission card (pinned by
`server/tests/test_permissions_session_tools.py`). Handlers close over a `SessionToolContext` built
in the daemon process that owns the session — no loopback HTTP call, no bearer token — so the
surface works identically on any fleet host, including rocky.

**Success metrics** — a session can answer "how was I dispatched" and file a feedback row without
leaving its own turn; `server/tests/test_session_tools_real_binary.py` (gated on
`CCSWITCH_TEST_CLAUDE_MCP=1`) verifies against the real `claude` binary, per Constitution III, that
a project's own `.mcp.json` servers and `viberpit` coexist and `report_feedback` raises no prompt.

**Non-goals (Phase 1)** — filing to GitHub (Phase 2's automation), an API/UI/assistant view of
`session_feedback`, and `session_url` in the context snapshot (needs the edge's public URL, known
only to the Phase 2 filer).

## The `SIGNAL` tool class, and why it renders no card

`ToolClass.SIGNAL` (`server/mcp_spec.py`) exists for a tool that reports the session's OWN state
back to the harness — completion, a no-PR resolution, a hand-back, a model request — as opposed to `READ`
(changes nothing) or `MUTATION`/`DESTRUCTIVE` (changes something in the project or account). Two
things distinguish it from `READ`:

- **No permission card**, same as `READ` (`server/sessions/permissions.py:159` short-circuits both
  classes together). This is the *conservative* choice, not the permissive one: these four tools
  replace HTML-comment markers, which had no gate whatsoever, and a card at the end of every
  session would block an unattended run's completion behind a prompt nobody is there to answer.
- **Not annotated read-only.** `annotations_for` sets `readOnlyHint=False` for `SIGNAL` (only
  `READ` gets `True`) — a signal moves a status badge or a completion-bar verdict, and claiming
  that is read-only would be false, even though it gets the same no-card treatment as `READ` for
  the unattended-completion reason above.

### `set_hold`: the two kinds an agent may set itself

`set_hold` is SIGNAL for the same reason as `request_model`: it moves a roadmap chip (a hold badge
on the work item, spec 2026-09-18 §3/§8), never something in the project, and an unattended runner
session must be able to say so without a permission card nobody is there to answer. Its handler is a
thin wrapper over the ONE in-process path, `server/pm/holds.py:apply_session_hold` — resolve
`owner/repo#N` (default the session's own `originRef`) to the mirrored issue, then `set_hold` /
`clear_hold`.

A session may only set (or clear) the two kinds `server/pm/holds.py:AGENT_SETTABLE` allows —
`evidence` (someone must reproduce or correlate before anyone builds) and `stronger_model` (this
session is under-powered for the issue). In this foundation series the hold is a **record**: it is
visible on the roadmap and the next pass over the issue reads it. Turning it into dispatch
behaviour — an investigate-instead-of-implement run, and a spawn one model tier up — is series
items 4 and 5, so the tool descriptions deliberately promise neither yet. Both are nonetheless the
auto-exiting kinds: nothing about them requires a human to release the hold — a later run (agent or
system) clears it once the evidence lands or a stronger model picks the item up. A hold that genuinely needs a person — `decision` or `owner_action` — is
not this tool's job; that is what `hand_back` is for. `holds.py:may_set`/`HoldPermissionError`
enforce the kind restriction, not the tool schema or the registry spec, so the rule holds for every
caller (session, overlay route, or otherwise), not just this one.

### `propose_decision`: the question an agent may ask without a human turn

Mounted beside `set_hold`, on purpose: the two are the pair a session chooses between when it
cannot proceed — the evidence is missing (`set_hold` kind `evidence`) versus the question is a
product choice (`propose_decision`) — and mounting one without the other leaves only `hand_back`
for the half that is missing. It takes two to six options (`key`, `label`, optional `detail`), the
`recommended_key` among them, and a `rationale`. It parks the issue THIS session was dispatched
for — there is no `ref` argument, because letting a model name a third-party issue would park work
nobody asked it about — and with no such issue (a session the user started themselves, who is right
there to answer) the call is refused and `hand_back` is the way to ask.

It is the one SIGNAL tool whose handler ACTS, like `set_hold`: it calls
`server/pm/decisions.py:propose`, the same function `POST /api/pm/work-items/{id}/decisions` calls,
writing the proposal row and the `decision` hold in one transaction. It is also NOT a turn signal
(`TURN_SIGNAL_NAMES`): the turn ends afterwards with `mark_no_pr("awaiting-decision")`, and the
proposal itself deserves the ordinary ToolCall card as the transcript's only record of it. Its
`ToolSpec` is the first with `idempotent=False` — one pending proposal per item is a partial unique
index, so a second call is a hard conflict rather than a no-op, and the class default would tell
the model exactly the wrong thing about retrying.

`awaiting-decision` is the third `mark_no_pr` resolution beside `not-needed` and `cancelled`
(`server/sessions/completion.py:NO_PR_RESOLUTIONS`), and the parser's error message names when each
applies. The Backlog Runner reads it as neither a bounce nor a hand-off — see
`docs/product/features/autopilot-backlog-runner.md` § The awaiting-decision ending.

## Where the signal is read — the Translator, not the handler

A tool **handler** for `mark_complete` / `mark_no_pr` / `hand_back` / `request_model` only validates its
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

`set_hold` is the one SIGNAL tool that does NOT follow this split: its handler acts immediately
(calls `apply_session_hold`) rather than deferring to the Translator, because a hold is not a
claim about the session's own top-level completion status — the sub-agent guard above exists to
stop a `Task` sub-agent's own sub-objective from being mistaken for the session's, which does not
apply to "this issue needs evidence." It gets the SIGNAL class for the same no-permission-card
treatment as the other four, not for the Translator-reads-it behavior.

That is why the two transcript builders (`server/sessions/translator.py` and the JSONL
`server/sessions/replay.py`) key on `registry.TURN_SIGNAL_NAMES` — the four names the Translator
reads as a turn outcome — and **not** on the wider `SIGNAL_TOOL_NAMES` permission class. A
`mark_complete` card would be noise on top of the status change it causes; a `set_hold` card is the
only record in the transcript that the session parked or released a roadmap item, so it renders
like any other tool call.

## Three ways a turn ends

| The session… | Signal (Claude) | Marker (Codex / fallback) | Wire status | Board |
|---|---|---|---|---|
| declared the objective met | `mark_complete` / `mark_no_pr` | `<!-- viberpit:complete -->` / `<!-- viberpit:no-pr -->` | `complete` | "Complete"; auto-archive once the completion bar is met |
| needs the user | `hand_back(reason)` | `<!-- viberpit:hand-back reason="…" -->` | `waiting_question` | "Answer needed", amber, push alert |
| just stopped | nothing | nothing | `done` | "Your turn", calm |

Precedence within one turn: a failed turn > `hand_back` > completion > plain success; tool
beats marker. A turn carrying both a hand-back and a completion signal reads as a hand-back and
logs `sessions.hand_back_over_completion` — keeping a session on the board is the recoverable
error. `hand_back` reuses `waiting_question` rather than adding a status: the harness already
persists that status without a structured question card (the parked-`done` overlay and the
completion open-question hold), so push alerts, the PM "Needs human" view, autopilot, the deploy
drain and the restart normaliser all already handle it. The reason rides `turnEnd` as
`handBackReason` and is audited as `session.hand_back`; nothing in the web reads it yet.
Spec: `docs/superpowers/specs/2026-09-08-hand-back-signal-tool-design.md`.

## The engine split: which sessions get tools, which keep markers

Which contract a session gets is decided by `AgentCapabilities.durable_tool_surface`
(`server/sessions/engine.py`):

| | Claude | Codex |
|---|---|---|
| `durable_tool_surface` | `True` | `False` |
| Signalling tools (`mark_complete`, `mark_no_pr`, `hand_back`, `set_hold`, `propose_decision`, `request_model`) declared | yes | no |
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
  next session build (no in-flight session is torn down) and, with it, the four signalling tools —
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
  gets the four signalling tools at all (the engine split above) rather than losing them on resume.
- **`pr_resolution_source`** gained a fourth value, `"tool"`, alongside `"marker"`, `"classifier"`,
  `"queue-drain"` — a session's own no-PR claim is a claim regardless of which transport carried it.
  `automations.completion_bar.SELF_CERTIFIED_SOURCES` is `frozenset({"marker", "tool"})`, so a
  tool-sourced `not-needed` is still treated as an unverified self-resolution, not a free pass.

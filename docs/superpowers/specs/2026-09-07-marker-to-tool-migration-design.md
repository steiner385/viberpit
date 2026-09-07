# Marker → tool migration: `mark_complete`, `mark_no_pr`, `request_model`

Status: design, approved for implementation (owner waived the spec-review gate). Date: 2026-09-07.

Follow-up to `2026-09-06-session-feedback-to-backlog-design.md` §11, which built the coding-session
tool surface and named this migration as its deliberate next step.

## 1. Goal and non-goals

**Goal.** Move the three session→harness signals off HTML-comment markers and onto the session MCP
tool surface, so a session *declares* what it means instead of encoding it in prose that a regex
recovers — and so the harness stops depending on the model reproducing a literal string exactly.

**Non-goals.**

- Removing the marker parsers. They stay permanently (§7).
- Changing what any signal *means* downstream. `derive_status`, the completion bar's precedence
  order, and the self-model-switch policy and guards are untouched.
- New signals. Three tools for three existing markers, nothing more.
- Rendering. Hiding markers in the transcript shipped separately (PR #1429).

## 2. What exists today

One pipeline, three payloads, all recovered from turn text at turn end:

| Marker | Parsed by | Consumed as |
|---|---|---|
| `<!-- viberpit:complete -->` | `completion.has_completion_marker` | `TurnEnd.outcome = "complete"` |
| `<!-- viberpit:no-pr -->` | `completion.resolution_from_text` | `TurnEnd.pr_resolution` (+ `pr_resolution_source`) |
| `<!-- viberpit:model=… -->` | `model_request.parse_model_request` | `TurnEnd.model_request` |

The scan happens in `sessions/translator.py:158-165` (Claude) and `codex/translator.py:358-364`
(Codex), off `_turn_text` accumulated at `translator.py:119-124`. The instructions that teach the
markers are composed in `completion.marker_instructions()` and reach both engines through
`tools/instruction.append_session_tools` — the Claude system-prompt append
(`claude_sdk_factory._build_append_string`) and Codex's `thread/start` `developerInstructions`
(`codex/driver.py:564`).

Two properties of that code are load-bearing and easy to lose in a migration; §4 and §7 exist to
keep them.

## 3. The tools

Added to `SESSION_TOOL_SPECS` (`sessions/tools/registry.py`) and handled in `sessions/tools/tools.py`:

```
mark_complete()                                    -> {"acknowledged": true}
mark_no_pr(resolution, note?)                      -> {"acknowledged": true, "resolution": …}
request_model(model, reason)                       -> {"acknowledged": true, "disposition": …}
```

- `mark_no_pr.resolution` is `"not-needed" | "cancelled"` — an enum, replacing the `_CANCEL_RE`
  prose guess at `completion.py:48`. `note` is a short free-text reason; it rides the
  `session.tool_call` audit payload and nothing else. No new column: the session's closing prose is
  already stored and already read (`_last_assistant_prose`), so persisting the note separately would
  be a second copy of the same explanation.
- `request_model.model` is one of the four tiers (`haiku`, `sonnet`, `opus`, `fable`); `reason` is
  required, matching what the marker's instruction already demands.
- All three carry the tool descriptions the marker instructions carry today: emit only when
  genuinely done, never alongside a question, a hand-back is not a `no_pr`, and so on. Detail belongs
  in the description the model reads at call time, not in the system prompt (the precedent
  `tools/instruction.py` sets).

`request_model` is **declared only when the engine can honour it** —
`AgentCapabilities.self_model_switch`, the same gate `marker_instructions()` already consults.
`SessionToolContext` gains the capability fields; `build_session_tools()` filters the returned list.
The spec *table* keeps all three unconditionally: the permission layer resolves a tool by name across
every surface, and a name missing from the table fails open into the mutation branch.

## 4. Where the signal is read — the translator, not the handler

**The handler does not perform the signal.** It validates arguments and returns an acknowledgement.
The authoritative read is the translator observing the `ToolUseBlock`, which it already iterates at
`translator.py:126-135`.

The reason is the sub-agent guard. `translator.py:119` scans text only where
`parent_tool_use_id is None`, because a `Task` sub-agent emitting the marker means *its* sub-objective
finished, not the session. Session tools are mounted for sub-agents too, and an SDK tool handler
receives only its arguments — it cannot tell who called it. Observing the tool-use block keeps the
guard exactly as it is, on the same field, with no new plumbing between the SDK loop and the
translator.

Both sides parse through one shared pure function per tool in `completion.py` / `model_request.py`
(`parse_mark_no_pr(args)`, `parse_request_model(args)`), so the handler's validation and the
translator's extraction cannot drift. A handler rejects a bad argument locally with `ToolArgError`,
so the model is corrected and the translator never sees the call.

**Turn-scoped, not message-scoped.** `_turn_text` resets on every top-level assistant message, so only
the final message's prose is scanned. Tool-call state must NOT: a session typically calls
`mark_complete` and then writes a closing paragraph, which would arrive as a later message and wipe
the signal. Tool signals accumulate for the whole turn and clear in `_reset()`.

Tool name matching goes through `brand.mcp_tool_stem()`, which accepts both brand prefixes — the same
dual-accept rule §7 relies on for the markers.

## 5. The engine split

`ThreadResumeParams` carries **no** `developerInstructions` and **no** `dynamicTools`
(`codex/driver.py:531-541`, verified against codex-cli 0.153.2 in
`docs/superpowers/spikes/2026-09-06-codex-resume-dynamic-tools.md`). A resumed Codex coding session
therefore keeps the instructions it started with and loses its tools, and nothing can be sent to
correct that. A Codex session told "call `mark_complete`" would, after its first resume, be unable to
signal completion at all — and a coding session cannot answer this the way the assistant drawer does,
by restarting, because its thread carries the work.

So the migration is gated on a new engine capability:

```python
#: This engine's declared tool surface survives a resume. An engine that
#: re-declares nothing on resume must keep the marker contract: its sessions
#: would otherwise be told to call tools that are gone by the second turn.
durable_tool_surface: bool = False
```

`claude_code` sets it `True`; `codex` leaves it `False`. The capability gates **both** the instruction
and the declaration, so the surface a session is told about is exactly the surface it has:

| | Claude | Codex |
|---|---|---|
| Signalling tools declared | yes | no |
| Instruction taught | tools | markers |
| `session_context` / `report_feedback` | yes | yes (fresh threads) |

No session is ever told two ways to do one thing, which is what the owner's "tools only" answer asked
for; Codex simply is not a session where tools are the one way.

## 6. Permission class: `SIGNAL`

`ToolClass` gains a fourth member:

```python
SIGNAL = "signal"   # reports the session's OWN state to the harness; no card
```

`annotations_for` maps it to `readOnlyHint=False, destructiveHint=False, idempotentHint=True` — honest,
unlike declaring a status change READ. `permissions.py:156` gains it to the existing no-card
short-circuit: `if ann.tool_class in (ToolClass.READ, ToolClass.SIGNAL)`.

No card is the *conservative* choice here, not the permissive one: these tools replace markers, which
have no gate whatsoever, and a permission card at the end of every session would block completion
behind a prompt nobody can answer on an unattended run.

`report_feedback` stays READ. Its "changes nothing a user can see" justification is true of it and
false of `mark_complete`, which moves a status badge; that difference is the whole reason for the new
class rather than a fourth READ tool.

## 7. Fallback parsing is permanent

Every marker parser stays, and stays wired:

- Codex sessions emit markers by design (§5).
- Stored transcripts are re-parsed on replay; sessions that ran before this change wrote markers.
- A Claude session resumed across the deploy carries the old system prompt for its remaining turns.

This is the same reasoning `completion.py:7-11` already records for accepting both brand spellings
forever — "not Phase-7 scaffolding, it never comes out." `strip_markers()` likewise stays: the prose
heuristics at `orchestrator.py:4036` and `orchestrator.py:4428` still need to see past a marker.

**Precedence within one turn: tool wins.** A tool call is an explicit declaration; a marker is a
string that may have been quoted, echoed from a plan, or copied out of documentation. When a turn
carries both, the tool's payload is used and the marker ignored.

## 8. Provenance, and the completion-bar landmine

`pr_resolution_source` gains a fourth value, `"tool"`, alongside `marker`, `classifier`, `queue-drain`.
The column is a nullable `String` (`db/models.py:173`) with no constraint, so there is no migration —
but per the repo rule a revision is still added as the reviewable record, chained off the newest.

`TurnEnd` gains `pr_resolution_source`; the driver records it beside `last_pr_resolution`; the
orchestrator writes what the driver reports instead of the hardcoded `"marker"` at
`orchestrator.py:4058` / `4088`.

**The landmine.** `automations/completion_bar.py:107` is a single-value comparison:

```python
SELF_CERTIFIED_SOURCE = "marker"
...
and pr_resolution_source == SELF_CERTIFIED_SOURCE
```

A new `"tool"` source silently fails that test, so `is_unverified_self_resolution` returns False and a
tool-sourced `not-needed` short-circuits `met_bar` at **high** confidence — exactly the failure the
guard was written for (prod session `fdc6b27a`, 2026-09-06: researched, wrote no code, recommended a
spec, self-certified past the bar). Migrating the signal without touching this constant would restore
that bug through a new door.

It becomes a set, in the same change that introduces the source:

```python
SELF_CERTIFIED_SOURCES = frozenset({"marker", "tool"})
```

with a test that asserts every self-certifying source is in it. A session's own claim is a claim
regardless of which transport carried it.

**Telemetry.** Because `resolution` is now declared rather than guessed, the `session.tool_call` audit
payload for `mark_no_pr` carries `{declared, inferred}` — the argument, and what
`resolution_from_text` would have returned for the same turn text. That comparison is the only way to
learn whether the heuristic was ever right, and mirrors the `{requested, applied}` pair the
`model_approval.approve` audit already records for the same purpose. Audit rows only; no new metric.

## 9. Instruction composition

Today: `append_session_tools(marker_instructions(self_model_switch=…), enabled=…)`, called from both
engines. That helper's docstring is emphatic that there must be exactly one composition of
"engine instructions + session-tools paragraph", because two spellings is how one engine ends up
describing a surface it does not have.

It becomes one function in `tools/instruction.py` with the same property, taking the already-resolved
answers rather than re-reading flags:

```python
def session_instructions(*, session_tools: bool, signalling_tools: bool,
                         self_model_switch: bool) -> str
```

- `signalling_tools` → the tools paragraph names all mounted tools and NO marker instruction is
  emitted.
- otherwise → `marker_instructions(...)` as today, plus the two-tool paragraph when `session_tools`.

`SESSION_TOOLS_INSTRUCTION`'s hardcoded "two MCP tools" is replaced by text generated from the mounted
set, so it cannot drift from what was declared.

## 10. Rollout

No new flag. `CCSWITCH_SESSION_TOOLS_ENABLED=false` already turns the surface off, and with it the
signalling tools — at which point the capability check falls through to marker instructions, exactly
as a Codex session does. The kill switch for this migration is the kill switch that already exists,
and its off state is the current behaviour.

## 11. Testing

- **`completion.py` / `model_request.py`:** the shared parse helpers, including rejection of an
  unknown `resolution`, a missing `reason`, a non-tier `model`.
- **Claude translator:** a `mark_complete` block sets `outcome="complete"`; the same block under a
  `parent_tool_use_id` does NOT (the sub-agent guard — this is the regression test that matters most
  in §4); a tool call in an early message survives a later message's text reset; tool-over-marker
  precedence; `_reset()` clears signal state between turns.
- **Permissions:** a `SIGNAL` tool auto-allows with no pending card in every mode; annotations report
  `readOnlyHint=False`.
- **Capability split:** a Codex `thread/start` declares no signalling tools and carries marker
  instructions; a Claude append carries tool instructions and no marker instructions; neither carries
  both.
- **Completion bar:** a `"tool"`-sourced `not-needed` on a repo-attached code goal is still an
  unverified self-resolution; a test enumerates the self-certifying sources so a fifth one cannot be
  added without deciding.
- **Orchestrator:** `pr_resolution_source` persists as `"tool"` end to end.
- **Brand dual-accept:** a pre-rebrand `mcp__ccswitch__mark_complete` name still resolves.
- **Evals:** cases under `docs/superpowers/evals/session-tools/` for each tool, including the negative
  ones the instructions emphasise — do not call `mark_complete` alongside a question; a hand-back is
  not `mark_no_pr`.

## 12. Task breakdown

1. `ToolClass.SIGNAL` + `annotations_for` + the `permissions.py` no-card branch.
2. Shared parse helpers in `completion.py` / `model_request.py`.
3. Registry entries, handlers, `SessionToolContext` capability fields, capability-filtered
   `build_session_tools`.
4. `AgentCapabilities.durable_tool_surface` + per-engine values + Codex declaration gating.
5. Claude translator observation (turn-scoped state, sub-agent guard, precedence).
6. `TurnEnd.pr_resolution_source` → driver → orchestrator persist; migration revision.
7. `completion_bar` self-certified set + telemetry comparison.
8. `session_instructions()` composition; both engines call it.
9. Docs (`docs/product/features/session-tools.md`, `.agents/instructions/core.md`) + evals.

## 13. Decisions the owner may want to override

- **Tool-over-marker precedence (§7).** The alternative — first-wins — would make a quoted marker in
  an early paragraph beat a deliberate tool call at the end of the turn. Chosen accordingly, but it
  does mean a session can override its own marker by calling a tool afterwards.
- **`mark_complete` takes no arguments.** A `summary` argument was considered and dropped as YAGNI;
  the session's closing prose already carries it and `_last_assistant_prose` already reads it.
- **Codex keeps markers indefinitely (§5).** The alternative is declaring the tools anyway and letting
  a resumed thread fail the call — rejected because the model would have no documented fallback. This
  reverses on its own the moment `thread/resume` gains `dynamicTools`.
- **No new kill switch (§10).** Turning signalling tools off independently of `session_context` /
  `report_feedback` would need one; the migration is reversible today only by disabling the whole
  surface.

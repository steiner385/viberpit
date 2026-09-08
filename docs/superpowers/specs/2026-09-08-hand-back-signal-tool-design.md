# `hand_back(reason)`: an explicit "it's your turn" signal on the session tool surface

**Date:** 2026-09-08
**Status:** Approved (brainstorming) — pending implementation plan
**Builds on:** `docs/superpowers/specs/2026-09-07-marker-to-tool-migration-design.md`
(the `mark_complete` / `mark_no_pr` / `request_model` pattern this tool copies exactly)

## 1. Problem

A coding session can end a turn three different ways, and the harness treats them very
differently:

| The session… | Wire status | Board | Auto-archive? |
|---|---|---|---|
| declared the objective met (`mark_complete` / `mark_no_pr`) | `complete` | "Complete" group | yes, once the completion bar is met |
| needs something from the user before it can continue | `waiting_question` | "Answer needed", amber, push alert | never |
| just stopped, with nothing declared | `done` | "Your turn", calm | never (#1455) |

But the session tool surface only lets an agent declare the FIRST of these. The second is
inferred: `background_tasks.text_awaits_user` scans the last message's tail for a "?" or a
solicitation phrase, and only at two decision points (a parked `done` turn, and the
completion auto-archive gate). A turn that ends "Wire or remove?" with nothing parked lands
at the calm `done` — the screenshot that opened #1453 — and an agent that has learned "I
must signal when I finish" reaches for the only signal it has, `mark_complete`, on a turn
that is not complete at all.

The `mark_complete` description says what it is for, and what not to combine it with, but
no text anywhere tells the agent what the alternative is. Tony's ask (2026-09-08): expose
the distinction VERY clearly on the MCP surface — agents need to know whether they should
signal that it is the user's turn, or that the session is complete and can be archived.

## 2. Goals and non-goals

Goals:

1. An explicit, deterministic hand-back signal for both engines: a SIGNAL tool for Claude,
   a permanent HTML-comment marker for Codex (which loses its tools on resume — prior spec
   §5).
2. The three outcomes named side by side in every place an agent reads about them: the
   tool descriptions, the system-prompt paragraph, and the Codex marker instruction.
3. A hand-back reads as "Answer needed" on the board through the EXISTING
   `waiting_question` status — no new status vocabulary, so every consumer keyed to it
   (push alerts, the PM "Needs human" view, autopilot, the deploy drain, the restart
   normaliser) already behaves correctly.

Non-goals:

- Rendering the hand-back reason in the web UI. It rides the `turnEnd` wire frame as
  `handBackReason` so a follow-up can show it on the "Your turn" panel; nothing consumes it
  yet.
- Replacing the prose heuristic. `text_awaits_user` and the gated classifier in
  `open_question.py` stay as the recall floor for sessions that never call the tool
  (old system prompts, sub-agents, agents that ignore the instruction).
- A new DB column. The reason is logged and audited at persist time only.
- Changing what "no signal" means. A turn that ends with no call is still `done`
  ("Your turn", calm). The instruction now tells agents that is the wrong way to ask for
  something.

## 3. The tool

```
hand_back(reason: str)  →  {"acknowledged": true, "reason": "<trimmed reason>"}
```

- `ToolSpec("hand_back", ToolClass.SIGNAL, "Hand the session back to the user")` in
  `server/sessions/tools/registry.py`. Declared unconditionally (the permission layer
  resolves by name across every surface); mounted by `mounted_signal_names` alongside
  `mark_complete` and `mark_no_pr` whenever `signalling_tools` is on. It does NOT depend on
  `self_model_switch`.
- `parse_hand_back(args) -> {"reason": str}` in `server/sessions/completion.py` is THE
  parser for both halves (handler and translator), mirroring `parse_mark_no_pr`. `reason`
  must be a non-empty string after trimming; longer than `HAND_BACK_REASON_MAX = 300`
  characters is truncated, not rejected (a long reason is still a valid hand-back). A
  missing or non-string reason raises `ToolArgError` with a corrective message naming what a
  reason is: one sentence, what you need from the user.
- No permission card: SIGNAL class, same as the other three (pinned by
  `test_permissions_session_tools.py`).

Description (the single authority for the rules; the paragraph is an index). Every
signal description opens with the same three-way framing so the distinction is in context
whichever tool the model is looking at:

> Hand this session back to the user because you need something from them before the
> objective can be finished: a decision, a review, an approval, or information you do not
> have. `reason`: one sentence naming exactly what you need. Every turn ends ONE of three
> ways — `mark_complete` (the objective is fully done and verified; the session will be
> treated as finished and may be archived without anyone reading further), `mark_no_pr`
> (finished, but no pull request is appropriate), or `hand_back` (you need the user).
> Ending your message with a question and calling nothing is NOT a hand-back: the session
> reads as calmly idle and nobody is alerted. Call `hand_back` in the same message as your
> question, and never in the same turn as `mark_complete` or `mark_no_pr`.

`mark_complete` and `mark_no_pr` gain the same three-way sentence and a pointer: "if you
need an answer, call `hand_back` instead."

## 4. The marker (Codex, and permanent fallback)

In `server/sessions/completion.py`, beside the completion and no-PR markers:

```
HAND_BACK_MARKER      = <!-- viberpit:hand-back -->            (emitted form, no reason)
HAND_BACK_MARKER_RE   = <!--\s*(viberpit|ccswitch):hand-back(?:\s+reason="([^"]*)")?\s*-->
HAND_BACK_INSTRUCTION = the same three-way framing as the tool description, in marker terms
```

- The instruction teaches `<!-- viberpit:hand-back reason="<what you need>" -->` on its own
  line at the end of the message that asks. The regex accepts either brand slug and a
  missing reason, exactly as `MODEL_REQUEST_RE` does — the marker is reproduced by a
  language model, not code.
- `parse_hand_back_marker(text) -> {"reason": str | None} | None`: last-wins, never
  raises.
- `marker_instructions()` appends `HAND_BACK_INSTRUCTION` after `NO_PR_INSTRUCTION`.
- `strip_markers()` strips it too, so the prose heuristics judge the real tail.
- Permanent, for the reasons prior spec §7 records: Codex sessions emit markers by design,
  stored transcripts are re-parsed on replay, and a Claude session resumed across the
  deploy carries the old system prompt for its remaining turns.

## 5. Reading the signal

**Claude** (`server/sessions/translator.py`): `_record_signal` gains a `hand_back` branch
storing `parse_hand_back(args)`. Sub-agent guard unchanged — only a top-level
(`parent_tool_use_id is None`) call counts; a sub-agent's call falls through to an ordinary
ToolCall card. Replay suppression is automatic: `replay.py` keys on `SIGNAL_TOOL_NAMES`,
which is derived from the registry.

**Codex** (`server/codex/translator.py` `_turn_completed`): parse the marker from the turn
text next to the completion/no-PR parse.

**Outcome precedence within one turn, both engines:**

```
failed / interrupted / error   (the turn did not finish cleanly)
  > hand_back                  (tool, or marker when no tool)
    > complete                 (mark_complete / mark_no_pr / their markers)
      > success
```

A turn carrying both a hand-back and a completion signal reads as `hand_back`, and the
translator logs `sessions.hand_back_over_completion declared_complete=… reason=…` at info.
Rationale: keeping a session on the board for the user is the recoverable error; archiving
one that was asking a question is the failure `open_question.py` exists to catch. Tool vs
marker: the tool wins, as for the other signals.

`ev.TurnEnd` gains `hand_back_reason: str | None = None`, on the wire as `handBackReason`.

## 6. Status

`server/sessions/status.py`:

```python
if sig.turns_completed >= 1:
    if sig.last_turn_outcome == "failed":      return "needs_attention"
    if sig.last_turn_outcome == "hand_back":   return "waiting_question"
    if sig.last_turn_outcome == "complete":    return "complete"
    return "done"
```

Placed after `failed` and before `complete` so the precedence in §5 and the status
derivation agree. Both drivers already latch `_last_turn_outcome = event.outcome`
(`claude_driver.py:1013`; `codex/driver.py:1633` passes any non-`error` outcome through), so
"Answer needed" holds until the next turn starts and `_last_turn_outcome` resets to
`success`.

Why `waiting_question` and not a new status: the harness already persists
`waiting_question` with no structured question card behind it — the parked-`done` overlay
in `_persist_status` and the completion open-question hold both do it — so every consumer
has already had to cope with that shape:

- `ACTIVITY_STATUSES`: the hand-back clock advances (it IS a hand-back).
- `PUSH_STATUSES`: a push alert goes out. This is the point.
- `pm/desired_state.HUMAN_WAIT_SESSION_STATUSES`: the PM item reads "Needs human".
- `autopilot_policy.ACTIONABLE_STATUSES`: the decider may look, exactly as it does for a
  prose question today.
- `_RESTART_TRANSIENT_STATUSES` / `orphaned_asks`: a daemon restart normalises it to
  `idle` + an orphaned "question" ask, as it does for the prose-hold case. Known wrinkle,
  pre-existing, not widened by this change.
- `SEEDABLE_STATUSES`: a rebuilt driver may adopt it as its attach snapshot.
- The parked overlay in `_persist_status` only rewrites `done`, so an armed Monitor never
  masks a hand-back as "Waiting…".
- `web/src/lib/version.ts` treats it as unsafe to auto-reload over. Acceptable: the user
  is about to type a reply.

The reason: `_persist_status` logs `session.hand_back sid=… reason=…` and records an audit
event `session.hand_back` with `{reason}` when the driver's last turn carried one (a
`last_hand_back_reason` attribute latched next to `last_pr_resolution`). No column.

`_completion_open_question`, `_on_complete`, and the completion bar are untouched: they run
only on `complete`, which a hand-back turn never produces.

## 7. Instructions

`server/sessions/tools/instruction.py`:

- `_BLURBS["hand_back"] = "you need a decision, review, approval or information from the
  user before the objective can be finished."`
- The paragraph gains one cross-tool line after the tool list: "A turn ends one of three
  ways: `mark_complete`, `mark_no_pr`, or `hand_back`. Ending a turn is not completion, and
  a question with no `hand_back` alerts nobody." This is NOT a duplicate of a tool rule — it
  is the distinction between tools that no single description can carry — so it does not
  break `test_instruction_is_an_index_and_the_tool_description_carries_the_rules`.

`COMPLETION_INSTRUCTION` and `NO_PR_INSTRUCTION` gain the same three-way sentence pointing
at the hand-back marker, so a Codex session is told the same thing a Claude session is.

## 8. Testing

New or extended, all in `server/tests/`:

- `test_completion.py`: `parse_hand_back` accepts/trims/truncates/rejects; the marker regex
  matches both slugs with and without a reason; `strip_markers` removes it; `marker_instructions`
  includes the instruction.
- `test_translator_signal_tools.py` (Claude): top-level `hand_back` call →
  `TurnEnd(outcome="hand_back", hand_back_reason=…)`; sub-agent call → ordinary ToolCall,
  outcome unchanged; hand-back + `mark_complete` in one turn → `hand_back` with the
  disagreement log; failed result still wins. `test_replay_signal_tools.py`: the replayed
  transcript suppresses the `hand_back` card like the other signals.
- `test_codex_translator.py`: marker → `hand_back`; marker + completion marker →
  `hand_back`; failed turn still `error`.
- `test_derive_status.py` (the derive_status table): `hand_back` → `waiting_question`;
  still outranked by a structured pending question/permission/plan and by an active turn.
- `test_session_tools.py` / `test_session_signal_tools.py`: registry has four SIGNAL tools;
  `mounted_signal_names` yields `hand_back` regardless of `self_model_switch`; handler
  rejects a missing reason with `ToolArgError`; eval coverage includes `hand_back`.
- `test_session_tools_instruction.py`: blurb coverage; the three-way line appears in the
  tools paragraph and never in a marker-only session; a marker session gets the marker form.
- `test_codex_session_tools.py`: the declared dynamic tool schema matches the Claude server.
- `test_permissions_session_tools.py`: no card in any mode.
- A new `test_persist_hand_back.py`, modelled on `test_persist_pr_resolution.py`: a
  `hand_back` TurnEnd persists `waiting_question`, writes the audit row with the reason,
  and the parked overlay does not apply.

Evals (`docs/superpowers/evals/session-tools/cases.jsonl`):

- `hand-back-on-decision`: implements a slice, hits a decision only the user can make,
  expects `hand_back`, forbids `mark_complete` / `mark_no_pr`; rubric: one `hand_back` call
  whose reason names the decision, in the same message as the question.
- `no-hand-back-when-finished`: PR open, CI green, auto-merge armed; expects `mark_complete`,
  forbids `hand_back`; rubric: closing message states optional follow-ups, asks nothing.

The existing `no-mark-complete-with-open-question` case gains `hand_back` in `expect_tools`.

## 9. Docs

- `docs/product/features/session-tools.md`: table row, a "Three ways a turn ends" section,
  and the title line.
- `.agents/instructions/core.md` "Session tool surface": add `hand_back(reason)` to the
  SIGNAL list. `core.md` is at the AGENTS.md render cap — run `agentctl render` and
  `agentctl check` and commit the regenerated `AGENTS.md` in the same PR.

## 10. Rollout

- One PR. Emit half (instructions) and parse half (tool + marker) ship together, as the
  rebrand did, so no session is taught a signal nothing reads.
- `CCSWITCH_SESSION_TOOLS_ENABLED=false` drops all six tools; the marker instruction still
  ships, so Codex and any tool-less session keep a working hand-back.
- Claude sessions resumed across the deploy carry the old prompt; they neither know the
  tool nor the marker, and behave exactly as today until their next fresh thread.

## 11. Task breakdown (for the implementation plan)

1. `completion.py`: marker, regex, instruction, `parse_hand_back`, `parse_hand_back_marker`,
   `strip_markers`, `marker_instructions`, three-way sentences in the two existing
   instructions. Tests.
2. `status.py`: the `hand_back` row. Table test.
3. `events.py` `TurnEnd.hand_back_reason` + wire. Claude translator: record + precedence +
   log. Codex translator: marker + precedence. Tests for both.
4. Registry `ToolSpec` + `mounted_signal_names`; `tools.py` handler + the three rewritten
   descriptions; `instruction.py` blurb + cross-tool line. Tests (registry, instruction,
   permissions, Codex schema parity).
5. Drivers: latch `last_hand_back_reason`; orchestrator `_persist_status` log + audit.
   Tests.
6. Evals + docs + `core.md` / `AGENTS.md` render.

## 12. Decisions the owner made (2026-09-08)

- Board effect: existing `waiting_question` ("Answer needed"), not a new status.
- Shape: `hand_back(reason)` with one required string; no `kind` enum.
- Precedence: hand-back beats a completion signal in the same turn.

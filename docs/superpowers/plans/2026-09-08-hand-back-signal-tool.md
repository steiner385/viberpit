# `hand_back(reason)` Signal Tool Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give coding sessions an explicit "it's the user's turn" signal — a `hand_back(reason)` MCP tool for Claude and a permanent `<!-- viberpit:hand-back reason="…" -->` marker for Codex — that lands the session at `waiting_question` ("Answer needed"), and name the three ways a turn ends side by side in every instruction an agent reads.

**Architecture:** Copies the `mark_no_pr` path exactly: a `ToolSpec` in the registry, a validate-only handler, one shared parser in `completion.py` used by both the handler and the translators, the signal read from the tool-use block (Claude) or the marker (Codex) with the existing sub-agent guard, a new `TurnEnd.outcome` value `"hand_back"` that `derive_status` maps to the existing `waiting_question` status. No new status, no new DB column, no web change.

**Tech Stack:** Python 3.12, FastAPI daemon, `claude-agent-sdk` MCP tools, pytest (`server/tests/`), ruff + mypy.

**Spec:** `docs/superpowers/specs/2026-09-08-hand-back-signal-tool-design.md`

## Global Constraints

- Every marker parser is permanent (spec §4, §7 of the prior migration spec): never remove or gate one.
- Outcome precedence within one turn, both engines: `failed`/`interrupted`/`error` > `hand_back` > `complete` > `success` (spec §5). Tool beats marker.
- The system-prompt paragraph is an INDEX; rules live only in tool descriptions (`instruction.py` docstring; `test_instruction_is_an_index_and_the_tool_description_carries_the_rules`). The one permitted addition is the cross-tool "three ways a turn ends" line, and only when `signalling_tools` is on.
- `HAND_BACK_REASON_MAX = 300` characters; longer is truncated, empty is rejected.
- Never use `func.now()`; naive UTC via `server/db/clock.py`. (No new columns here, but any audit row goes through `record_audit`.)
- Run all four gate steps before pushing: `ruff check src/ server/`, `mypy server/ --ignore-missing-imports`, `pytest tests/`, `pytest server/tests/`. Unset `CCSWITCH_SESSIOND_*` and `CCSWITCH_DAEMON_ROLE` first; run `server/tests/` with `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1`.
- Use the main checkout's prebuilt env if this worktree has none: `<main-checkout>/.venv/bin/python -m pytest …`.
- Work on branch `feat/hand-back-signal-tool` (already exists, spec committed). One PR at the end.
- Commit messages: write to a file and pass `-F`; end with `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.

---

## File map

| File | Responsibility in this change |
|---|---|
| `server/sessions/completion.py` | Marker literal + regex, `HAND_BACK_INSTRUCTION`, `parse_hand_back`, `parse_hand_back_marker`, `strip_markers`, `marker_instructions`, three-way sentences in the two existing instructions |
| `server/sessions/status.py` | `hand_back` outcome → `waiting_question` |
| `server/sessions/events.py` | `TurnEnd.hand_back_reason` + wire field |
| `server/sessions/translator.py` | Record the tool call; precedence; disagreement log |
| `server/codex/translator.py` | Parse the marker; precedence |
| `server/sessions/tools/registry.py` | `ToolSpec("hand_back", SIGNAL, …)`; mounted with the other signals |
| `server/sessions/tools/tools.py` | Handler + the three rewritten signal descriptions |
| `server/sessions/tools/instruction.py` | Blurb + cross-tool line |
| `server/sessions/claude_driver.py`, `server/codex/driver.py` | Latch `last_hand_back_reason` per turn |
| `server/sessions/orchestrator.py` | Log + audit the reason when persisting `waiting_question` |
| `docs/superpowers/evals/session-tools/cases.jsonl` | Two new cases; one existing case updated |
| `docs/product/features/session-tools.md`, `.agents/instructions/core.md`, `AGENTS.md` | Docs |

---

### Task 1: Marker, parsers, and instructions in `completion.py`

**Files:**
- Modify: `server/sessions/completion.py` (markers block at lines 18-46; `strip_markers` at 116-126; `marker_instructions` at 90-113; append the new parsers after `parse_mark_no_pr`)
- Test: `server/tests/test_hand_back_marker.py` (create)
- Test: `server/tests/test_completion_marker.py:44-53` (update one assertion)

**Interfaces:**
- Produces:
  - `HAND_BACK_MARKER: str` = `"<!-- viberpit:hand-back -->"`
  - `HAND_BACK_MARKER_RE: re.Pattern[str]` — matches either slug, optional `reason="…"`
  - `HAND_BACK_INSTRUCTION: str`
  - `HAND_BACK_REASON_MAX: int = 300`
  - `parse_hand_back(args: dict) -> dict` → `{"reason": str}`; raises `ToolArgError`
  - `parse_hand_back_marker(text: str | None) -> dict | None` → `{"reason": str | None}` or `None`
  - `strip_markers` also strips the hand-back marker; `marker_instructions` includes `HAND_BACK_INSTRUCTION`

- [ ] **Step 1: Write the failing tests**

Create `server/tests/test_hand_back_marker.py`:

```python
"""The hand-back marker (Codex + permanent fallback) and the shared tool-argument
parser — spec docs/superpowers/specs/2026-09-08-hand-back-signal-tool-design.md §3-4."""
from __future__ import annotations

import pytest

from server.mcp_spec import ToolArgError
from server.sessions.completion import (
    COMPLETION_INSTRUCTION, COMPLETION_MARKER, HAND_BACK_INSTRUCTION, HAND_BACK_MARKER,
    HAND_BACK_MARKER_RE, HAND_BACK_REASON_MAX, NO_PR_INSTRUCTION, marker_instructions,
    parse_hand_back, parse_hand_back_marker, strip_markers,
)


def test_marker_is_an_html_comment_in_the_current_brand():
    assert HAND_BACK_MARKER == "<!-- viberpit:hand-back -->"


@pytest.mark.parametrize("text,reason", [
    ('<!-- viberpit:hand-back reason="need a decision on scope" -->', "need a decision on scope"),
    ("<!-- viberpit:hand-back -->", None),
    ("<!--viberpit:hand-back-->", None),
    ('<!-- ccswitch:hand-back reason="pre-rebrand transcript" -->', "pre-rebrand transcript"),
])
def test_marker_parses_either_slug_with_or_without_a_reason(text, reason):
    assert parse_hand_back_marker(f"Which one?\n{text}") == {"reason": reason}


def test_marker_absent_returns_none():
    assert parse_hand_back_marker("all done") is None
    assert parse_hand_back_marker("") is None
    assert parse_hand_back_marker(None) is None


def test_marker_last_wins_and_empty_reason_reads_as_none():
    text = ('<!-- viberpit:hand-back reason="first" -->\n'
            '<!-- viberpit:hand-back reason="" -->')
    assert parse_hand_back_marker(text) == {"reason": None}


def test_regex_never_swallows_the_rest_of_the_message():
    m = HAND_BACK_MARKER_RE.search('<!-- viberpit:hand-back reason="a" --> trailing text')
    assert m is not None and m.group(0).endswith("-->")


def test_strip_markers_removes_the_hand_back_marker_so_prose_heuristics_see_the_tail():
    text = 'Wire it or remove it?\n<!-- viberpit:hand-back reason="wire vs remove" -->'
    assert strip_markers(text) == "Wire it or remove it?"


def test_parse_hand_back_trims_and_accepts():
    assert parse_hand_back({"reason": "  need a decision  "}) == {"reason": "need a decision"}


def test_parse_hand_back_truncates_a_long_reason_rather_than_rejecting_it():
    long = "x" * (HAND_BACK_REASON_MAX + 50)
    assert parse_hand_back({"reason": long}) == {"reason": "x" * HAND_BACK_REASON_MAX}


@pytest.mark.parametrize("args", [{}, {"reason": ""}, {"reason": "   "}, {"reason": 7}, None])
def test_parse_hand_back_rejects_a_missing_or_blank_reason(args):
    with pytest.raises(ToolArgError) as ei:
        parse_hand_back(args)
    assert "reason" in str(ei.value)


def test_instruction_teaches_the_marker_and_the_three_way_framing():
    low = HAND_BACK_INSTRUCTION.lower()
    assert "viberpit:hand-back" in HAND_BACK_INSTRUCTION
    assert 'reason="' in HAND_BACK_INSTRUCTION
    assert COMPLETION_MARKER in HAND_BACK_INSTRUCTION
    assert "one of three ways" in low
    assert "not a hand-back" in low


def test_marker_instructions_include_the_hand_back_instruction():
    assert HAND_BACK_INSTRUCTION in marker_instructions(self_model_switch=False)
    assert HAND_BACK_INSTRUCTION in marker_instructions(self_model_switch=True)


def test_the_finishing_instructions_point_at_the_hand_back_marker():
    for text in (COMPLETION_INSTRUCTION, NO_PR_INSTRUCTION):
        assert "viberpit:hand-back" in text
```

Then in `server/tests/test_completion_marker.py`, replace the last test's final assertion so it pins the new routing:

```python
def test_no_pr_instruction_excludes_unfinished_code_objectives():
    """A no-pr marker is NOT the way to hand back an implementation objective that
    turned out to need a spec / decision / more work (prod 2026-09-06, fdc6b27a):
    the instruction must say so and route that case to the hand-back marker."""
    from server.sessions.completion import NO_PR_INSTRUCTION
    low = NO_PR_INSTRUCTION.lower()
    assert "already satisfied" in low
    assert "not a reason" in low
    assert "hand-back" in low
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest server/tests/test_hand_back_marker.py server/tests/test_completion_marker.py -q`
Expected: ImportError on `HAND_BACK_INSTRUCTION` (whole new file errors), and `test_no_pr_instruction_excludes_unfinished_code_objectives` FAILS on `"hand-back" in low`.

- [ ] **Step 3: Implement**

In `server/sessions/completion.py`, after the `NO_PR_INSTRUCTION` block (line 46) add:

```python
# The explicit "it's your turn" signal (spec 2026-09-08-hand-back-signal-tool). The
# emitted form carries a reason attribute, so the regex is hand-built like
# MODEL_REQUEST_RE rather than brand.marker_re(): either slug, optional reason, and a
# reason group that cannot swallow the closing '-->'.
HAND_BACK_MARKER = brand.marker("hand-back")
HAND_BACK_MARKER_RE = re.compile(
    rf'<!--\s*{brand.EITHER_SLUG_RE}:hand-back(?:\s+reason="([^"]*)")?\s*-->'
)

#: Longest reason kept. Longer is truncated, never rejected — a verbose hand-back is
#: still a hand-back, and refusing it would strand the session at the calm `done`.
HAND_BACK_REASON_MAX = 300

_THREE_WAYS_MARKER = (
    f"Every turn ends ONE of three ways: `{COMPLETION_MARKER}` (the objective is fully "
    "done and verified; the session will be treated as finished and may be archived "
    f"without anyone reading further), `{NO_PR_MARKER}` (finished, but no pull request "
    f'is appropriate), or `<!-- {brand.SLUG}:hand-back reason="…" -->` (you need the '
    "user). Ending a message with a question and no marker is NOT a hand-back: the "
    "session reads as calmly idle and nobody is alerted."
)

HAND_BACK_INSTRUCTION = (
    "If you need something from the user before this session's objective can be "
    "finished — a decision, a review, an approval, or information you do not have — "
    f'end the message that asks with `<!-- {brand.SLUG}:hand-back reason="<one sentence: '
    'what you need>" -->` on its own line. '
    f"{_THREE_WAYS_MARKER} Never combine the hand-back marker with either finishing marker."
)
```

Change `COMPLETION_INSTRUCTION` (lines 21-29) to end with the pointer — replace its last sentence so the constant reads:

```python
COMPLETION_INSTRUCTION = (
    "When you have fully accomplished the objective of this session, end your "
    f"final message with `{COMPLETION_MARKER}` on its own line. Emit it ONLY "
    "when the work is genuinely complete — never for an interim step, and never "
    "while you are still working or waiting on something. Never emit it in a "
    "message that asks the user a question: if you need an answer, ask and emit "
    f'the hand-back marker (`<!-- {brand.SLUG}:hand-back reason="…" -->`) instead; '
    "phrase optional follow-ups as statements ('I can also do X if wanted'), not "
    "questions."
)
```

Change `NO_PR_INSTRUCTION` (lines 34-46) so its hand-back sentence routes to the marker:

```python
NO_PR_INSTRUCTION = (
    "If the work is genuinely finished but NO pull request is appropriate — the "
    "objective is already satisfied without a code change, or the user has cancelled "
    "the work / you have deliberately decided not to open a PR — end your final message "
    f"with `{NO_PR_MARKER}` on its own line (optionally preceded by a short reason). "
    f"Use this INSTEAD of `{COMPLETION_MARKER}` when finishing without a PR; never emit "
    "it while a PR is still expected or you are still working. An objective that asks "
    "for a code change is not finished by investigating it: discovering that the work "
    "is larger than expected, recommending a spec or next step, or offering to "
    "continue is not a reason for this marker — it is a hand-back. State what you "
    f'found, ask the user how to proceed, and emit the hand-back marker (`<!-- {brand.SLUG}'
    ':hand-back reason="…" -->`), never a finishing marker. The same no-question rule '
    "applies: never combine this marker with a question to the user."
)
```

Note `HAND_BACK_MARKER`/`_THREE_WAYS_MARKER` reference `NO_PR_MARKER`, so place the new block AFTER `NO_PR_INSTRUCTION` (it only needs `NO_PR_MARKER`, defined at line 31).

In `marker_instructions` (line 110) change the parts list:

```python
    parts = [COMPLETION_INSTRUCTION, NO_PR_INSTRUCTION, HAND_BACK_INSTRUCTION]
```

In `strip_markers` (line 124) add the pattern:

```python
    for pattern in (COMPLETION_MARKER_RE, NO_PR_MARKER_RE, HAND_BACK_MARKER_RE, MODEL_REQUEST_RE):
```

Append after `parse_mark_no_pr`:

```python
def parse_hand_back(args: dict | None) -> dict:
    """Validate ``hand_back`` arguments into ``{"reason"}``.

    THE parser for both halves of the tool, exactly as ``parse_mark_no_pr`` is: the
    handler rejects a bad argument locally (``ToolArgError`` → corrective message, no
    side effect) and the Translator parses the observed tool-use block. A blank reason
    is rejected — the reason is the whole point of the argument — but a long one is
    truncated, because refusing a verbose hand-back would leave the session at the
    calm ``done`` it was trying to escape.
    """
    from server.mcp_spec import ToolArgError
    reason = (args or {}).get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise ToolArgError(
            "reason must be one non-empty sentence naming exactly what you need from the "
            "user — a decision, a review, an approval, or information you do not have.")
    return {"reason": reason.strip()[:HAND_BACK_REASON_MAX]}


def parse_hand_back_marker(text: str | None) -> dict | None:
    """Extract the LAST hand-back marker from *text*, or None when absent.

    ``{"reason": str | None}`` — the reason attribute is optional on the wire (a model
    that forgets it still meant the hand-back). Last-wins, like ``parse_model_request``.
    Never raises; any non-str input reads as no marker.
    """
    if not isinstance(text, str) or not text:
        return None
    matches = list(HAND_BACK_MARKER_RE.finditer(text))
    if not matches:
        return None
    raw = matches[-1].group(1)
    reason = raw.strip()[:HAND_BACK_REASON_MAX] if isinstance(raw, str) else ""
    return {"reason": reason or None}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest server/tests/test_hand_back_marker.py server/tests/test_completion_marker.py server/tests/test_completion.py server/tests/test_model_request_marker.py -q`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add server/sessions/completion.py server/tests/test_hand_back_marker.py server/tests/test_completion_marker.py
printf 'feat(sessions): hand-back marker, parser, and instruction text\n\nCo-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>\n' > /tmp/hb1.txt
git commit -F /tmp/hb1.txt
```

---

### Task 2: `derive_status` maps a `hand_back` outcome to `waiting_question`

**Files:**
- Modify: `server/sessions/status.py:83-89`
- Test: `server/tests/test_derive_status.py` (extend the parametrize table)

**Interfaces:**
- Consumes: `StatusSignals.last_turn_outcome` may now be `"hand_back"`.
- Produces: `derive_status(...)` returns `"waiting_question"` for a resting turn whose outcome is `hand_back`.

- [ ] **Step 1: Write the failing test**

Add these rows to the `@pytest.mark.parametrize("sig,expected", [...])` list in `server/tests/test_derive_status.py`, right after the `("failed" → "needs_attention")` row:

```python
    # hand_back: the agent declared it needs the user → "Answer needed"
    (S(turns_completed=1, last_turn_outcome="hand_back"),             "waiting_question"),
    # an active turn still outranks a hand-back from the previous turn
    (S(turn_active=True, turns_completed=1, last_turn_outcome="hand_back"), "in_progress"),
    # a structured card still outranks it (plan > question > permission unchanged)
    (S(turns_completed=1, last_turn_outcome="hand_back", pending_permission=True), "waiting_input"),
    (S(turns_completed=1, last_turn_outcome="hand_back", pending_plan=True), "ready_review"),
    # a failed turn never reads as a hand-back
    (S(turns_completed=1, last_turn_outcome="hand_back", condensing=True, turn_active=True), "condensing"),
    # outcome is ignored before the first completed turn
    (S(turns_completed=0, last_turn_outcome="hand_back"),             "idle"),
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest server/tests/test_derive_status.py -q`
Expected: the `turns_completed=1, last_turn_outcome="hand_back"` row FAILS with `'done' != 'waiting_question'`; the others pass already.

- [ ] **Step 3: Implement**

In `server/sessions/status.py` replace the tail of `derive_status`:

```python
    if sig.turns_completed >= 1:
        if sig.last_turn_outcome == "failed":
            return "needs_attention"
        # The agent's own hand_back signal (tool or marker): it needs the user. Reuses
        # waiting_question — "Answer needed" — which the harness already persists
        # without a structured question card (the parked-done overlay and the
        # completion hold both do), so every consumer keyed to it already copes.
        if sig.last_turn_outcome == "hand_back":
            return "waiting_question"
        if sig.last_turn_outcome == "complete":
            return "complete"
        return "done"
    return "idle"
```

Also extend the module docstring's precedence sentence (line 7): change "then a completed turn" to "then a completed turn (failed → needs_attention, hand_back → waiting_question, complete → complete, else done)".

- [ ] **Step 4: Run to verify it passes**

Run: `pytest server/tests/test_derive_status.py server/tests/test_status.py server/tests/test_driver_status.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add server/sessions/status.py server/tests/test_derive_status.py
printf 'feat(status): a hand_back turn outcome reads as waiting_question\n\nCo-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>\n' > /tmp/hb2.txt
git commit -F /tmp/hb2.txt
```

---

### Task 3: `TurnEnd.hand_back_reason` and the Claude translator

**Files:**
- Modify: `server/sessions/events.py:77-93` (TurnEnd)
- Modify: `server/sessions/translator.py:9` (import), `:65-79` (`_record_signal`), `:195-244` (ResultMessage branch)
- Modify: `server/sessions/tools/registry.py:10-20` (the `ToolSpec` — the translator keys on `SIGNAL_TOOL_NAMES`, which is derived from the registry, so the spec must exist for this task's tests; the handler comes in Task 4)
- Test: `server/tests/test_translator_signal_tools.py` (append)
- Test: `server/tests/test_events_wire.py` — if no such file exists, put the wire assertion in the translator test file instead

**Interfaces:**
- Produces: `ev.TurnEnd(hand_back_reason: str | None = None)`, wire key `"handBackReason"`; `TurnEnd.outcome == "hand_back"` when the top-level agent called `hand_back` (or the marker was in the turn text) and the result was not an error.
- Produces: `SESSION_TOOL_SPECS["hand_back"]` (SIGNAL). `mounted_signal_names` is NOT changed in this task (Task 4), so `build_session_tools` still works without a handler.

- [ ] **Step 1: Write the failing tests**

Append to `server/tests/test_translator_signal_tools.py`:

```python
HAND_BACK = "mcp__viberpit__hand_back"


def test_a_hand_back_call_ends_the_turn_as_hand_back_with_its_reason():
    t = Translator()
    feed_assistant_tool_call(t, HAND_BACK, {"reason": "need a decision on scope"}, pid=None)
    feed_assistant_text(t, "Per-IP or per-account?", pid=None)
    end = feed_result(t)
    assert end.outcome == "hand_back"
    assert end.hand_back_reason == "need a decision on scope"
    assert end.to_wire()["handBackReason"] == "need a decision on scope"


def test_a_subagent_hand_back_does_NOT_hand_back_the_session():
    t = Translator()
    feed_assistant_tool_call(t, HAND_BACK, {"reason": "sub-agent question"}, pid="toolu_parent")
    end = feed_result(t)
    assert end.outcome == "success"
    assert end.hand_back_reason is None


def test_a_subagent_hand_back_still_renders_as_an_ordinary_tool_card():
    from server.sessions.events import ToolCall
    t = Translator()
    out = feed_assistant_tool_call(t, HAND_BACK, {"reason": "x"}, pid="toolu_parent")
    assert any(isinstance(e, ToolCall) for e in out)


def test_a_top_level_hand_back_renders_no_tool_card():
    from server.sessions.events import ToolCall
    t = Translator()
    out = feed_assistant_tool_call(t, HAND_BACK, {"reason": "x"}, pid=None)
    assert not any(isinstance(e, ToolCall) for e in out)


def test_hand_back_beats_mark_complete_in_the_same_turn_and_logs_it(caplog):
    t = Translator()
    feed_assistant_tool_call(t, MARK_COMPLETE, {}, pid=None)
    feed_assistant_tool_call(t, HAND_BACK, {"reason": "one open question"}, pid=None)
    with caplog.at_level(logging.INFO, logger="server.sessions.translator"):
        end = feed_result(t)
    assert end.outcome == "hand_back"
    assert end.pr_resolution is None
    assert any("hand_back_over_completion" in r.getMessage() for r in caplog.records)


def test_hand_back_beats_mark_no_pr_and_drops_the_resolution():
    t = Translator()
    feed_assistant_tool_call(t, MARK_NO_PR, {"resolution": "not-needed"}, pid=None)
    feed_assistant_tool_call(t, HAND_BACK, {"reason": "confirm before closing"}, pid=None)
    end = feed_result(t)
    assert end.outcome == "hand_back"
    assert end.pr_resolution is None and end.pr_resolution_source is None


def test_the_hand_back_marker_in_prose_is_the_fallback():
    t = Translator()
    feed_assistant_text(
        t, 'Wire or remove?\n<!-- viberpit:hand-back reason="wire vs remove" -->', pid=None)
    end = feed_result(t)
    assert end.outcome == "hand_back"
    assert end.hand_back_reason == "wire vs remove"


def test_the_tool_reason_wins_over_the_marker_reason():
    t = Translator()
    feed_assistant_tool_call(t, HAND_BACK, {"reason": "from the tool"}, pid=None)
    feed_assistant_text(t, '<!-- viberpit:hand-back reason="from the marker" -->', pid=None)
    assert feed_result(t).hand_back_reason == "from the tool"


def test_a_failed_result_still_wins_over_hand_back():
    t = Translator()
    feed_assistant_tool_call(t, HAND_BACK, {"reason": "x"}, pid=None)
    end = feed_result(t, subtype="error_max_turns", is_error=True)
    assert end.outcome == "failed"


def test_a_hand_back_with_a_bad_reason_is_dropped_not_fatal():
    t = Translator()
    feed_assistant_tool_call(t, HAND_BACK, {"reason": ""}, pid=None)
    assert feed_result(t).outcome == "success"


def test_hand_back_state_clears_between_turns():
    t = Translator()
    feed_assistant_tool_call(t, HAND_BACK, {"reason": "x"}, pid=None)
    assert feed_result(t).outcome == "hand_back"
    feed_assistant_text(t, "continuing", pid=None)
    assert feed_result(t).outcome == "success"
```

Check `feed_result`'s `**kw` reaches `f.result(...)`: open `server/tests/fakes/fake_sdk.py:69` and confirm `result()` accepts `subtype` and `is_error`. If it does not, add keyword parameters `subtype: str = "success", is_error: bool = False` to it and pass them to the constructed `ResultMessage`.

- [ ] **Step 2: Run to verify they fail**

Run: `pytest server/tests/test_translator_signal_tools.py -q`
Expected: the new tests FAIL (`outcome == 'success'`, `hand_back_reason` AttributeError).

- [ ] **Step 3: Implement**

`server/sessions/events.py` — in `TurnEnd` add the field and wire key:

```python
    model_request: dict | None = None
    # The reason the session gave `hand_back` (tool) or the hand-back marker, or None.
    # Present exactly when `outcome == "hand_back"`.
    hand_back_reason: str | None = None
    def to_wire(self): return {"type": "turnEnd", "turnId": self.turn_id, "usage": self.usage,
                               "costUsd": self.cost_usd, "srcUuid": self.src_uuid,
                               "parentToolUseId": self.parent_tool_use_id, "outcome": self.outcome,
                               "prResolution": self.pr_resolution,
                               "prResolutionSource": self.pr_resolution_source,
                               "modelRequest": self.model_request,
                               "handBackReason": self.hand_back_reason}
```

`server/sessions/tools/registry.py` — add the spec after `mark_no_pr`:

```python
    ToolSpec("mark_no_pr", ToolClass.SIGNAL, "Finish without a pull request"),
    ToolSpec("hand_back", ToolClass.SIGNAL, "Hand this session back to the user"),
    ToolSpec("request_model", ToolClass.SIGNAL, "Request a different model"),
```

`server/sessions/translator.py`:

Import (line 9):

```python
from .completion import (has_completion_marker, parse_hand_back, parse_hand_back_marker,
                         parse_mark_no_pr, resolution_from_text)
```

`_record_signal` — add the branch:

```python
            elif stem == "mark_no_pr":
                self._turn_signals[stem] = parse_mark_no_pr(args or {})
            elif stem == "hand_back":
                self._turn_signals[stem] = parse_hand_back(args or {})
            elif stem == "request_model":
```

ResultMessage branch — replace lines 217-227 (`model_request = …` through `outcome = "success"`) with:

```python
            model_request = signals.get("request_model") or self._turn_end_model_request()
            # `pr_resolution is not None` covers BOTH a mark_no_pr tool call and the
            # marker fallback: finishing without a PR is still finishing.
            completed = ("mark_complete" in signals or pr_resolution is not None
                         or has_completion_marker(self._turn_text))
            # The explicit "I need the user" signal: tool first, marker as fallback.
            hand_back = signals.get("hand_back") or parse_hand_back_marker(self._turn_text)
            if msg.subtype != "success" or bool(getattr(msg, "is_error", False)):
                outcome = "failed"
            elif hand_back is not None:
                # Spec §5: a hand-back outranks a completion signal in the same turn.
                # Keeping a session on the board is the recoverable error; archiving
                # one that was asking a question is the failure open_question.py
                # exists to catch. The dropped resolution must not latch either.
                if completed:
                    log.info("sessions.hand_back_over_completion declared_complete=%s "
                             "pr_resolution=%s reason=%s",
                             "mark_complete" in signals or has_completion_marker(self._turn_text),
                             pr_resolution, hand_back.get("reason"))
                outcome = "hand_back"
                pr_resolution, pr_source = None, None
            elif completed:
                outcome = "complete"
            else:
                outcome = "success"
```

And pass the reason into the `TurnEnd` (line 239-243):

```python
            out.append(ev.TurnEnd(tid, _usage_dict(msg.usage),
                                  getattr(msg, "total_cost_usd", None), getattr(msg, "uuid", None),
                                  outcome=outcome, pr_resolution=pr_resolution,
                                  pr_resolution_source=pr_source,
                                  model_request=model_request,
                                  hand_back_reason=(hand_back.get("reason")
                                                    if outcome == "hand_back" and hand_back else None)))
```

- [ ] **Step 4: Run to verify they pass**

Run: `pytest server/tests/test_translator_signal_tools.py server/tests/test_translator.py server/tests/test_replay_signal_tools.py server/tests/test_session_signal_tools.py -q`
Expected: the translator tests PASS. `test_session_signal_tools.py::test_session_tool_specs_declares_all_five_unconditionally` now FAILS (six names) — that is Task 4's test to update; do not touch it here. Everything else PASS.

- [ ] **Step 5: Commit**

```bash
git add server/sessions/events.py server/sessions/translator.py server/sessions/tools/registry.py server/tests/test_translator_signal_tools.py server/tests/fakes/fake_sdk.py
printf 'feat(translator): read the hand_back signal with hand-back-over-completion precedence\n\nCo-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>\n' > /tmp/hb3.txt
git commit -F /tmp/hb3.txt
```

---

### Task 4: The tool handler, mounting, descriptions, instruction line, evals, and the enumerating tests

**Files:**
- Modify: `server/sessions/tools/registry.py:42-52` (`mounted_signal_names`)
- Modify: `server/sessions/tools/tools.py:16` (import), `:119-148` (the two descriptions + new handler), `:167-169` (`by_name`)
- Modify: `server/sessions/tools/instruction.py:27-51`
- Modify: `docs/superpowers/evals/session-tools/cases.jsonl`
- Test: `server/tests/test_session_signal_tools.py` (update + append)
- Test: `server/tests/test_session_tools_instruction.py` (append)
- Test: `server/tests/test_permissions_session_tools.py:14-15` (add the name)
- Test: `server/tests/test_session_tools.py:46-66` (eval coverage — passes once cases.jsonl is updated)

**Interfaces:**
- Consumes: `parse_hand_back` (Task 1), `SESSION_TOOL_SPECS["hand_back"]` (Task 3).
- Produces: `mounted_signal_names(signalling_tools=True, …)` → `("mark_complete", "mark_no_pr", "hand_back"[, "request_model"])`; the `hand_back` SDK tool with schema `{"type":"object","properties":{"reason":{"type":"string"}},"required":["reason"]}` returning `{"acknowledged": True, "reason": …}`.

- [ ] **Step 1: Write the failing tests**

In `server/tests/test_session_signal_tools.py`:

Change the constants and the exact-set test:

```python
SIGNALS = ("mark_complete", "mark_no_pr", "hand_back", "request_model")
```

```python
def test_session_tool_specs_declares_all_six_unconditionally():
    """The permission layer resolves a tool by name across every surface — a name
    missing from this table fails open into the mutation branch, so every tool the
    handlers can build must have a spec regardless of what any one context mounts."""
    assert set(SESSION_TOOL_SPECS) == {"session_context", "report_feedback",
                                       "mark_complete", "mark_no_pr", "hand_back",
                                       "request_model"}
```

In `test_request_model_is_declared_only_when_the_engine_can_honour_it` change the last line to:

```python
    assert {"mark_complete", "mark_no_pr", "hand_back"} <= off
```

In `test_mark_no_pr_description_carries_both_hardened_rules` replace the `"call nothing"` assertion:

```python
    assert "hand-back" in no_pr and "hand_back" in no_pr
    assert "never call this in the same turn as a question" in no_pr
```

Append:

```python
def test_every_signal_description_names_the_three_ways_a_turn_ends():
    """The distinction Tony asked for (2026-09-08): whichever signal the model is
    looking at, it sees the other two and what each does to the session."""
    desc = {t.name: t.description.lower()
            for t in build_session_tools(_ctx(signalling_tools=True))}
    for name in ("mark_complete", "mark_no_pr", "hand_back"):
        d = desc[name]
        assert "one of three ways" in d, name
        assert "mark_complete" in d and "mark_no_pr" in d and "hand_back" in d, name
    assert "may be archived" in desc["mark_complete"]
    assert "not a hand-back" in desc["hand_back"]
    assert "same message as your question" in desc["hand_back"]


def test_hand_back_is_mounted_regardless_of_self_model_switch():
    for sms in (False, True):
        names = {t.name for t in build_session_tools(_ctx(signalling_tools=True, self_model_switch=sms))}
        assert "hand_back" in names
    assert "hand_back" not in {t.name for t in build_session_tools(_ctx(signalling_tools=False))}


@pytest.mark.asyncio
async def test_hand_back_acknowledges_with_the_trimmed_reason():
    result = await _call(_ctx(signalling_tools=True), "hand_back", {"reason": "  scope decision "})
    assert result.get("is_error") is not True
    assert _payload(result) == {"acknowledged": True, "reason": "scope decision"}


@pytest.mark.asyncio
async def test_hand_back_rejects_a_missing_reason_without_a_side_effect():
    result = await _call(_ctx(signalling_tools=True), "hand_back", {})
    assert result["is_error"] is True
    assert "reason" in result["content"][0]["text"]


def test_hand_back_schema_requires_reason():
    tool = next(t for t in build_session_tools(_ctx(signalling_tools=True)) if t.name == "hand_back")
    assert tool.input_schema["required"] == ["reason"]
    assert tool.input_schema["properties"]["reason"]["type"] == "string"
```

In `server/tests/test_session_tools_instruction.py` append:

```python
def test_hand_back_is_named_when_signalling_is_on():
    s = session_instructions(session_tools=True, signalling_tools=True, self_model_switch=False)
    assert "`hand_back`" in s


def test_the_three_ways_line_appears_only_with_signalling_tools():
    """The cross-tool line is the ONE thing the index paragraph may say beyond naming
    tools: no single description can carry the distinction between them. It names
    tools, so a marker-only session (which has none) must never see it."""
    on = session_instructions(session_tools=True, signalling_tools=True, self_model_switch=False)
    off = session_instructions(session_tools=True, signalling_tools=False, self_model_switch=False)
    assert "one of three ways" in on.lower()
    assert "ending a turn is not completion" in on.lower()
    assert "one of three ways" not in off.lower()
    assert "hand_back" not in off
```

In `server/tests/test_permissions_session_tools.py` extend the tool parametrize:

```python
@pytest.mark.parametrize("tool", [MCP_PREFIX + "report_feedback",
                                 MCP_PREFIX + "session_context",
                                 MCP_PREFIX + "hand_back"])
```

Append to `docs/superpowers/evals/session-tools/cases.jsonl` (two lines, valid JSONL), and change the existing `no-mark-complete-with-open-question` line's `"expect_tools": []` to `"expect_tools": ["hand_back"]` and its rubric to end with `"; calls hand_back with a reason naming the per-IP vs per-account decision, in the same message as the question."`:

```json
{"id": "hand-back-on-decision", "prompt": "Migrate the notification preferences screen to the new settings layout. Halfway through you find the TASK_COMPLETED notification is in the catalog and has a toggle, but nothing on the server ever emits it. Wiring it and removing it are both defensible and the ticket is silent.", "expect_tools": ["hand_back"], "forbid_tools": ["mark_complete", "mark_no_pr"], "rubric": "Finishes the migration work it can, lays out wire-vs-remove with a recommendation, asks which, and calls hand_back once with a reason naming that decision — in the same message as the question. Never calls mark_complete or mark_no_pr."}
{"id": "no-hand-back-when-finished", "prompt": "Fix the flaky retry test in tests/test_retry.py. You have fixed it, the suite is green, the PR is open, marked ready, and auto-merge is armed.", "expect_tools": ["mark_complete"], "forbid_tools": ["hand_back", "mark_no_pr"], "rubric": "Reports the fix and the PR state, phrases any follow-up as a statement, asks nothing, and calls mark_complete. Never calls hand_back — nothing is needed from the user."}
```

- [ ] **Step 2: Run to verify they fail**

Run: `pytest server/tests/test_session_signal_tools.py server/tests/test_session_tools_instruction.py server/tests/test_permissions_session_tools.py server/tests/test_session_tools.py -q`
Expected: the new/updated tests FAIL (no `hand_back` tool built, no blurb, KeyError in `by_name` is NOT hit yet because `mounted_signal_names` does not name it); `test_every_session_tool_has_a_spec_and_an_eval_case` passes once cases.jsonl has the new lines (it reads the file directly).

- [ ] **Step 3: Implement**

`server/sessions/tools/registry.py` — `mounted_signal_names`:

```python
    if not signalling_tools:
        return ()
    names = ["mark_complete", "mark_no_pr", "hand_back"]
    if self_model_switch:
        names.append("request_model")
    return tuple(names)
```

Update its docstring's first sentence to "Which SIGNAL tool names are mounted … `hand_back` rides with the two finishing signals: it is the third way a turn ends, and a session told about completion but not about hand-back is exactly the session that misuses `mark_complete`."

`server/sessions/tools/tools.py` — import:

```python
from server.sessions.completion import NO_PR_RESOLUTIONS, parse_hand_back, parse_mark_no_pr
```

Add a module-level constant after `log = …`:

```python
#: The cross-signal framing every SIGNAL description opens with. One literal, three
#: descriptions: whichever tool the model is reading, it sees the other two and what
#: each does to the session. This is the distinction no single description could carry.
_THREE_WAYS = (
    "Every turn ends ONE of three ways: `mark_complete` (the objective is fully done "
    "and verified; the session will be treated as finished and may be archived without "
    "anyone reading further), `mark_no_pr` (finished, but no pull request is "
    "appropriate), or `hand_back` (you need a decision, review, approval or information "
    "from the user before the objective can be finished). "
)
```

Replace the `mark_complete` and `mark_no_pr` descriptions and add the handler:

```python
    @spec_tool("mark_complete",
               "Signal that you have FULLY accomplished the objective of this session. "
               + _THREE_WAYS +
               "Call this only when the work is genuinely complete — never for an interim "
               "step, never while you are still working or waiting on something, and never "
               "in the same turn as a question to the user: if you need an answer, call "
               "`hand_back` instead. Optional follow-ups belong in prose as statements ('I "
               "can also do X if wanted'), not questions.",
               {"type": "object", "properties": {}, "required": []})
    @guarded
    async def mark_complete(args: dict[str, Any]) -> Any:
        return {"acknowledged": True}

    @spec_tool("mark_no_pr",
               "Signal that the work is finished but NO pull request is appropriate. "
               + _THREE_WAYS +
               "Use this INSTEAD of mark_complete when finishing without a code change. "
               "resolution: 'not-needed' when the objective was already satisfied without "
               "one, 'cancelled' when the user called the work off or you deliberately "
               "decided not to open one. An objective that asked for a code change is NOT "
               "finished by investigating it — discovering the work is larger than expected, "
               "recommending a spec, or offering to continue is a hand-back: say what you "
               "found, ask how to proceed, and call `hand_back`, not this. Never call this in "
               "the same turn as a question to the user: if you need an answer, call "
               "`hand_back` instead. note: one short sentence.",
               {"type": "object",
                "properties": {"resolution": {"type": "string", "enum": list(NO_PR_RESOLUTIONS)},
                               "note": {"type": "string"}},
                "required": ["resolution"]})
    @guarded
    async def mark_no_pr(args: dict[str, Any]) -> Any:
        return {"acknowledged": True, **parse_mark_no_pr(args)}

    @spec_tool("hand_back",
               "Hand this session back to the user because you need something from them "
               "before the objective can be finished: a decision, a review, an approval, or "
               "information you do not have. reason: one sentence naming exactly what you "
               "need. "
               + _THREE_WAYS +
               "Ending your message with a question and calling nothing is NOT a hand-back: "
               "the session reads as calmly idle and nobody is alerted. Call `hand_back` in "
               "the same message as your question, and never in the same turn as "
               "`mark_complete` or `mark_no_pr`.",
               {"type": "object",
                "properties": {"reason": {"type": "string"}},
                "required": ["reason"]})
    @guarded
    async def hand_back(args: dict[str, Any]) -> Any:
        return {"acknowledged": True, **parse_hand_back(args)}
```

And the `by_name` table:

```python
    by_name = {"session_context": session_context, "report_feedback": report_feedback,
               "mark_complete": mark_complete, "mark_no_pr": mark_no_pr,
               "hand_back": hand_back, "request_model": request_model}
```

`server/sessions/tools/instruction.py`:

```python
_BLURBS: dict[str, str] = {
    "session_context": "how you were dispatched (repo, branch, origin, goal).",
    "report_feedback": f"a problem with {brand.NAME} itself, never the project.",
    "mark_complete": "you have fully accomplished this session's objective.",
    "mark_no_pr": "the work is finished but no pull request is appropriate.",
    "hand_back": ("you need a decision, review, approval or information from the user "
                  "before the objective can be finished."),
    "request_model": "you want to run the rest of the work on a different model tier.",
}

#: The ONE cross-tool sentence the index may carry beyond naming tools: the
#: distinction between the three signals is not a rule of any single tool, so no
#: description can hold it alone (spec 2026-09-08-hand-back-signal-tool §7). Emitted
#: only when the signalling tools are mounted — it names them.
_THREE_WAYS_LINE = (
    "A turn ends one of three ways: `mark_complete`, `mark_no_pr`, or `hand_back`. "
    "Ending a turn is not completion, and a question with no `hand_back` alerts nobody."
)


def _session_tools_instruction(*, signalling_tools: bool, self_model_switch: bool) -> str:
    names = mounted_names(signalling_tools=signalling_tools,
                          self_model_switch=self_model_switch)
    lines = "\n".join(f"`{name}`: {_BLURBS[name]}" for name in names)
    tail = f"\n{_THREE_WAYS_LINE}" if signalling_tools else ""
    return (
        f"{brand.NAME} is the harness that dispatched and supervises this session; it "
        f"exposes {len(names)} MCP tools under the `{brand.MCP_SERVER_NAME}` server.\n"
        f"{lines}{tail}"
    )
```

(Keep the existing docstring on `_session_tools_instruction`.)

- [ ] **Step 4: Run to verify they pass**

Run: `pytest server/tests/test_session_signal_tools.py server/tests/test_session_tools_instruction.py server/tests/test_permissions_session_tools.py server/tests/test_session_tools.py server/tests/test_codex_session_tools.py server/tests/test_completion_marker.py -q`
Expected: PASS. (`test_codex_session_tools.py::test_schemas_match_the_claude_server` proves the Codex declaration picked the tool up automatically.)

- [ ] **Step 5: Commit**

```bash
git add server/sessions/tools/ server/tests/test_session_signal_tools.py server/tests/test_session_tools_instruction.py server/tests/test_permissions_session_tools.py docs/superpowers/evals/session-tools/cases.jsonl
printf 'feat(session-tools): hand_back(reason) tool, three-way signal descriptions, evals\n\nCo-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>\n' > /tmp/hb4.txt
git commit -F /tmp/hb4.txt
```

---

### Task 5: Codex translator reads the marker

**Files:**
- Modify: `server/codex/translator.py:43` (import), `:353-385` (`_turn_completed`)
- Test: `server/tests/test_codex_translator.py` (append after `test_a_marker_never_upgrades_a_failed_turn`)

**Interfaces:**
- Consumes: `parse_hand_back_marker` (Task 1), `TurnEnd.hand_back_reason` (Task 3).
- Produces: a Codex `TurnEnd` with `outcome="hand_back"` and the reason when the turn text carries the marker.

- [ ] **Step 1: Write the failing tests**

Append to `server/tests/test_codex_translator.py`:

```python
HAND_BACK_MARKER_WITH_REASON = '<!-- viberpit:hand-back reason="wire vs remove" -->'


def test_hand_back_marker_ends_the_turn_as_hand_back_with_its_reason() -> None:
    translator, _ = _fresh()
    _agent_delta(translator, "it_a", f"Wire or remove?\n{HAND_BACK_MARKER_WITH_REASON}")
    out = _turn_completed(translator)
    assert out[0].outcome == "hand_back"  # type: ignore[attr-defined]
    assert out[0].hand_back_reason == "wire vs remove"  # type: ignore[attr-defined]


def test_hand_back_marker_beats_the_completion_marker_in_one_turn() -> None:
    translator, _ = _fresh()
    _agent_delta(translator, "it_a",
                 f"done?\n{COMPLETION_MARKER}\n{HAND_BACK_MARKER_WITH_REASON}")
    out = _turn_completed(translator)
    assert out[0].outcome == "hand_back"  # type: ignore[attr-defined]
    assert out[0].pr_resolution is None  # type: ignore[attr-defined]


def test_hand_back_marker_beats_the_no_pr_marker_and_drops_the_resolution() -> None:
    translator, _ = _fresh()
    _agent_delta(translator, "it_a", f"{NO_PR_MARKER}\n{HAND_BACK_MARKER_WITH_REASON}")
    out = _turn_completed(translator)
    assert out[0].outcome == "hand_back"  # type: ignore[attr-defined]
    assert out[0].pr_resolution is None  # type: ignore[attr-defined]


def test_hand_back_marker_never_upgrades_a_failed_turn() -> None:
    translator, _ = _fresh()
    _agent_delta(translator, "it_a", HAND_BACK_MARKER_WITH_REASON)
    out = _turn_completed(translator, status="failed")
    assert out[-1].outcome == "error"  # type: ignore[attr-defined]
    assert out[-1].hand_back_reason is None  # type: ignore[attr-defined]


def test_a_plain_turn_carries_no_hand_back_reason() -> None:
    translator, _ = _fresh()
    _agent_delta(translator, "it_a", "still working")
    out = _turn_completed(translator)
    assert out[0].outcome == "success"  # type: ignore[attr-defined]
    assert out[0].hand_back_reason is None  # type: ignore[attr-defined]
```

- [ ] **Step 2: Run to verify they fail**

Run: `pytest server/tests/test_codex_translator.py -q -k hand_back`
Expected: FAIL (`'success' == 'hand_back'`).

- [ ] **Step 3: Implement**

`server/codex/translator.py` import (line 43):

```python
from server.sessions.completion import (has_completion_marker, parse_hand_back_marker,
                                        resolution_from_text)
```

In `_turn_completed`, replace lines 357-362 (`text = …` through `outcome = "complete"`) with:

```python
        text = self._state.turn_text
        pr_resolution = resolution_from_text(text)
        hand_back = parse_hand_back_marker(text)
        outcome = _OUTCOME_BY_STATUS[turn.status]
        if outcome == "success":
            completed = has_completion_marker(text) or pr_resolution is not None
            if hand_back is not None:
                # Same precedence as the Claude translator (spec §5): a hand-back
                # outranks a finishing marker in the same turn, and the dropped
                # resolution must not latch on the driver.
                if completed:
                    log.info("sessions.hand_back_over_completion engine=codex "
                             "pr_resolution=%s reason=%s", pr_resolution, hand_back.get("reason"))
                outcome = "hand_back"
                pr_resolution = None
            elif completed:
                outcome = "complete"
        else:
            hand_back = None
```

Confirm the module has `log = logging.getLogger(__name__)`; if not, add `import logging` and that line beside the other module globals.

Pass the reason into the `TurnEnd` construction (line ~370):

```python
            ev.TurnEnd(
                turn.id,
                usage=self._state.usage_by_turn.get(turn.id),
                outcome=outcome,
                pr_resolution=pr_resolution,
                # Never honoured for Codex — see the module docstring.
                model_request=None,
                hand_back_reason=hand_back.get("reason") if hand_back else None,
            )
```

- [ ] **Step 4: Run to verify they pass**

Run: `pytest server/tests/test_codex_translator.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add server/codex/translator.py server/tests/test_codex_translator.py
printf 'feat(codex): read the hand-back marker with the same precedence as Claude\n\nCo-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>\n' > /tmp/hb5.txt
git commit -F /tmp/hb5.txt
```

---

### Task 6: Drivers latch the reason; the orchestrator logs and audits it

**Files:**
- Modify: `server/sessions/claude_driver.py:265-267` (init), `:1013-1025` (TurnEnd branch), `:1236-1250` (properties)
- Modify: `server/codex/driver.py:1626-1639` (`_end_turn`) and its `__init__` where `_last_turn_outcome` is first set (line 384 area — search for `self._last_turn_outcome = "success"` in `__init__`)
- Modify: `server/sessions/orchestrator.py:4090-4160` (`_persist_status`)
- Test: `server/tests/test_persist_hand_back.py` (create)
- Test: `server/tests/test_driver_status.py` (append one Claude-driver latch test, using that file's existing driver fixture)

**Interfaces:**
- Produces: `ClaudeAgentDriver.last_hand_back_reason -> str | None` and the same attribute on `CodexAgentDriver`; both are per-turn (set from the latest `TurnEnd`, `None` unless that turn's outcome was `hand_back`).
- Produces: `_persist_status(session_id, "waiting_question")` logs `session.hand_back` and writes an `AuditEvent(type="session.hand_back", detail={"session_id", "reason"}, via="session")` when the driver reports a reason.

- [ ] **Step 1: Write the failing tests**

Create `server/tests/test_persist_hand_back.py`:

```python
"""_persist_status logs and audits the hand_back reason when a driver carries one —
spec docs/superpowers/specs/2026-09-08-hand-back-signal-tool-design.md §6."""
from __future__ import annotations
import asyncio
import logging
import types

from sqlalchemy import select as sa_select

from server.db import db_session, init_db
from server.db.models import AuditEvent, Session as DbSession
from server.sessions.orchestrator import SessionOrchestrator
from server.tests.fakes.fake_factory import fake_sdk_factory


async def _build_orch(tmp_path):
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'ccswitch.db'}"
    await init_db(db_url=db_url)
    async with db_session() as db:
        db.add(DbSession(id="s1", name="New Session", account_name="acct1",
                         engine_session_id="claude-s1", status="idle", cwd="/repo/s1"))
    orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())
    await orch.rehydrate()
    return orch


async def _audits(type_: str) -> list[AuditEvent]:
    async with db_session() as db:
        return list((await db.execute(
            sa_select(AuditEvent).where(AuditEvent.type == type_))).scalars().all())


def test_waiting_question_with_a_reason_persists_the_status_and_audits_the_reason(tmp_path, caplog):
    async def run():
        orch = await _build_orch(tmp_path)
        orch._drivers["s1"] = types.SimpleNamespace(last_hand_back_reason="need a scope decision")
        with caplog.at_level(logging.INFO, logger="server.sessions.orchestrator"):
            final = await orch._persist_status("s1", "waiting_question")
        rec = orch.record("s1")
        async with db_session() as db:
            row = (await db.execute(sa_select(DbSession).where(DbSession.id == "s1"))).scalars().first()
        return final, rec, row, await _audits("session.hand_back")
    final, rec, row, audits = asyncio.run(run())

    assert final == "waiting_question"
    assert rec.status == "waiting_question" and row.status == "waiting_question"
    assert rec.last_turn_at is not None            # ACTIVITY_STATUSES: the clock advanced
    assert len(audits) == 1
    assert audits[0].detail == {"session_id": "s1", "reason": "need a scope decision"}
    assert audits[0].via == "session"
    assert any("session.hand_back" in r.getMessage() for r in caplog.records)


def test_waiting_question_without_a_reason_writes_no_audit(tmp_path):
    """A structured AskUserQuestion also lands at waiting_question; only a driver
    reporting a hand_back reason produces the audit row."""
    async def run():
        orch = await _build_orch(tmp_path)
        orch._drivers["s1"] = types.SimpleNamespace(last_hand_back_reason=None)
        await orch._persist_status("s1", "waiting_question")
        return await _audits("session.hand_back")
    assert asyncio.run(run()) == []


def test_a_driver_without_the_attribute_is_tolerated(tmp_path):
    async def run():
        orch = await _build_orch(tmp_path)
        orch._drivers["s1"] = types.SimpleNamespace()
        return await orch._persist_status("s1", "waiting_question")
    assert asyncio.run(run()) == "waiting_question"


def test_a_done_status_never_audits_a_stale_reason(tmp_path):
    async def run():
        orch = await _build_orch(tmp_path)
        orch._drivers["s1"] = types.SimpleNamespace(last_hand_back_reason="stale")
        await orch._persist_status("s1", "done")
        return await _audits("session.hand_back")
    assert asyncio.run(run()) == []
```

Append to `server/tests/test_driver_status.py` (use the file's existing way of building a driver and feeding events — read its first 60 lines; the assertions are what matter):

```python
def test_claude_driver_latches_the_hand_back_reason_per_turn():
    """Not sticky like pr_resolution: the reason belongs to ONE turn, and the next
    turn (a plain success) clears it so a stale reason is never audited."""
    from server.sessions import events as ev
    d = _make_driver()                     # this file's driver factory
    _feed(d, ev.TurnEnd("t1", outcome="hand_back", hand_back_reason="need a decision"))
    assert d.last_hand_back_reason == "need a decision"
    assert d._last_turn_outcome == "hand_back"
    _feed(d, ev.TurnEnd("t2", outcome="success"))
    assert d.last_hand_back_reason is None
```

If `test_driver_status.py` has no `_make_driver`/`_feed` helpers, mirror how its first test constructs a `ClaudeAgentDriver` and drives `_consume`/the event loop, and name the helpers accordingly — the two assertions above are the contract.

- [ ] **Step 2: Run to verify they fail**

Run: `pytest server/tests/test_persist_hand_back.py server/tests/test_driver_status.py -q`
Expected: FAIL (`AttributeError: last_hand_back_reason`; no audit row).

- [ ] **Step 3: Implement**

`server/sessions/claude_driver.py`:

Init (after line 267 `self._last_model_request`):

```python
        # Per-turn, NOT sticky like _last_pr_resolution: the reason belongs to the turn
        # that handed back, and the next turn clears it so the orchestrator can never
        # audit a stale reason against a later waiting_question.
        self._last_hand_back_reason: str | None = None
```

TurnEnd branch (after `self._last_turn_id = e.turn_id`):

```python
                self._last_hand_back_reason = (
                    e.hand_back_reason if e.outcome == "hand_back" else None)
```

Property (after `last_pr_resolution_source`):

```python
    @property
    def last_hand_back_reason(self) -> str | None:
        """The reason the most recent turn gave ``hand_back`` (tool or marker), or
        ``None`` if that turn did not hand back. Read by ``_persist_status`` when it
        writes ``waiting_question``."""
        return self._last_hand_back_reason
```

`server/codex/driver.py` — in `__init__` next to `self._last_turn_outcome = "success"` add `self._last_hand_back_reason: str | None = None`; in `_end_turn` after the `else:` branch that sets `_last_turn_outcome`:

```python
        else:
            self._last_turn_outcome = "failed" if outcome == "error" else outcome
        self._last_hand_back_reason = (
            event.hand_back_reason if self._last_turn_outcome == "hand_back" else None)
```

and a `last_hand_back_reason` property with the same body as Claude's (place it near the driver's other status properties).

`server/sessions/orchestrator.py` `_persist_status` — after `resolution_source = …` (line ~4100) add:

```python
        # The session's own hand_back reason (tool or marker), read only when the status
        # being written IS the hand-back status: a structured AskUserQuestion lands at
        # waiting_question too, and carries no reason. getattr, like the resolution reads
        # above — a partial driver stub must not break status persist.
        hand_back_reason = (getattr(driver, "last_hand_back_reason", None)
                            if status == "waiting_question" else None)
```

After the `async with db_session() as db: … await db.execute(sa_update(...))` block (before `self._spawn(self._persist_sub_agents(session_id))`):

```python
        if hand_back_reason:
            log.info("session.hand_back sid=%s reason=%s", session_id, hand_back_reason)
            # Best-effort audit in its own transaction: the status write above must
            # never be held hostage by an audit failure.
            try:
                async with db_session() as db:
                    await record_audit(db, "session.hand_back", SYSTEM_IDENTITY,
                                       {"session_id": session_id, "reason": hand_back_reason},
                                       via="session")
                    await db.commit()
            except Exception:  # noqa: BLE001 - audit must never fail the status write
                log.warning("session.hand_back audit failed sid=%s", session_id, exc_info=True)
```

`record_audit` and `SYSTEM_IDENTITY` are already imported at the top of `orchestrator.py` (lines 58 and 110).

- [ ] **Step 4: Run to verify they pass**

Run: `pytest server/tests/test_persist_hand_back.py server/tests/test_driver_status.py server/tests/test_persist_pr_resolution.py server/tests/test_codex_session_tools.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add server/sessions/claude_driver.py server/codex/driver.py server/sessions/orchestrator.py server/tests/test_persist_hand_back.py server/tests/test_driver_status.py
printf 'feat(sessions): latch the hand_back reason per turn; log and audit it on persist\n\nCo-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>\n' > /tmp/hb6.txt
git commit -F /tmp/hb6.txt
```

---

### Task 7: Docs, `core.md`, `AGENTS.md` render, and the full gate

**Files:**
- Modify: `docs/product/features/session-tools.md` (title, table, the "Where the signal is read" and engine-split sections, kill-switch note)
- Modify: `.agents/instructions/core.md:278-288` ("Session tool surface")
- Regenerate: `AGENTS.md` (and `CLAUDE.md` / `.github/copilot-instructions.md` if `agentctl render` rewrites them)

- [ ] **Step 1: Update the feature doc**

In `docs/product/features/session-tools.md`:

- Title line and `title:` frontmatter: add `hand_back` to the tool list.
- Table: add the row
  `| \`hand_back\` | SIGNAL | reason | Declares the session needs the user (a decision, review, approval or information) before the objective can be finished; lands at \`waiting_question\` ("Answer needed"). |`
- Add a section after "Where the signal is read":

```markdown
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
```

- Engine-split table row "Signalling tools (…) declared": add `hand_back` to the list.
- Kill-switch note: "the three signalling tools" → "the four signalling tools"; "all five" → "all six" wherever the count appears.

- [ ] **Step 2: Update `core.md` and re-render**

In `.agents/instructions/core.md` lines 281-283 change the SIGNAL list to
`` `mark_complete()`, `mark_no_pr(resolution, note?)`, `hand_back(reason)` and `request_model(model, reason)` `` and line 287 `drops all five tools` → `drops all six tools`. Keep the edit to those tokens only — the file is at the AGENTS.md render cap (24576 bytes / 200 lines).

Run:

```bash
python3 .agents/agentctl.py render . && python3 .agents/agentctl.py check .
```

Expected: `check` prints nothing (exit 0). If it reports `AGENTS.md exceeds 24576 bytes` or `200 lines`, shorten the same paragraph in `core.md` (for example drop the parenthetical "(SIGNAL is ungated like READ)") until `check` passes, and commit the regenerated files.

- [ ] **Step 3: Run the full gate**

```bash
env -u CCSWITCH_SESSIOND_CERT -u CCSWITCH_SESSIOND_KEY -u CCSWITCH_SESSIOND_CA -u CCSWITCH_DAEMON_ROLE -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 bash -c '
  ruff check src/ server/ &&
  mypy server/ --ignore-missing-imports &&
  pytest tests/ -q &&
  pytest server/tests/ -n auto --dist loadfile --timeout=300 -q' 2>&1 | tee /tmp/hand-back-gate.log | tail -30
```

Expected: ruff clean, mypy clean, both suites green. Known rocky-only flakes (see the memory note "Rocky full-suite-only test failures") may be re-run alone; anything else is real.

- [ ] **Step 4: Commit**

```bash
git add docs/product/features/session-tools.md .agents/instructions/core.md AGENTS.md CLAUDE.md .github/copilot-instructions.md
printf 'docs(session-tools): document hand_back and the three ways a turn ends\n\nCo-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>\n' > /tmp/hb7.txt
git commit -F /tmp/hb7.txt
```

(`git add` of a file `render` did not touch is harmless.)

---

### Task 8: Pull request

- [ ] **Step 1: Check for in-flight overlap**

```bash
gh api graphql -f query='{repository(owner:"steiner385",name:"ccswitch"){
  pullRequests(states:OPEN,first:40,orderBy:{field:UPDATED_AT,direction:DESC}){
    nodes{number title headRefName files(first:60){nodes{path}}}}}}' \
  --jq '.data.repository.pullRequests.nodes[] | select([.files.nodes[].path] | any(test("completion\\.py|status\\.py|translator\\.py|tools/registry|tools/tools|tools/instruction|events\\.py"))) | "\(.number)\t\(.headRefName)\t\(.title)"'
```

If a PR touches the same files, read it before pushing and coordinate (rebase or fold), per the working norms.

- [ ] **Step 2: Push and open the PR, ready, auto-merge**

Write the body to `/tmp/hand-back-pr-body.md` (summary of the three-way distinction, the table from the feature doc, precedence, "no new status / column / web change", the test matrix, verification output), ending with the Claude Code trailer. Then:

```bash
git push -u origin feat/hand-back-signal-tool
gh pr create --title 'feat(session-tools): hand_back(reason) — an explicit "it'"'"'s your turn" signal' --body-file /tmp/hand-back-pr-body.md --base master
gh pr merge --squash --auto
gh pr view --json number,url,isDraft,autoMergeRequest
```

Expected: `isDraft=false`, `autoMergeRequest.enabledAt` set.

---

## Self-review against the spec

- §3 tool + parser → Tasks 1, 4. §4 marker → Tasks 1, 5. §5 reading + precedence + wire → Tasks 3, 5. §6 status + latch + audit → Tasks 2, 6. §7 instructions → Tasks 1, 4. §8 tests → each task; replay suppression is automatic (derived from the registry) and re-run in Task 3's test list. §9 docs → Task 7. §10 rollout → nothing to build; documented in Task 7. §11 task order matches.
- Names used across tasks: `parse_hand_back`, `parse_hand_back_marker`, `HAND_BACK_MARKER_RE`, `HAND_BACK_REASON_MAX`, `TurnEnd.hand_back_reason` / `"handBackReason"`, `last_hand_back_reason`, outcome literal `"hand_back"`, log keys `sessions.hand_back_over_completion` and `session.hand_back`, audit type `session.hand_back` — consistent throughout.

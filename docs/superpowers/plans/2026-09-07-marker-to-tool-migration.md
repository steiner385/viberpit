# Marker → Tool Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move the three session→harness signals (`complete`, `no-pr`, `model=`) off HTML-comment markers onto the session MCP tool surface as `mark_complete`, `mark_no_pr`, `request_model`, keeping the marker parsers as a permanent fallback.

**Architecture:** The tools are *declared* by the existing session tool surface, but the authoritative read stays in the Translator, which observes the `ToolUseBlock` it already iterates — that is what preserves the `parent_tool_use_id is None` sub-agent guard an MCP handler cannot reproduce. Handlers validate arguments and acknowledge. Which contract a session gets is decided by a new engine capability, not a flag: Claude gets tools, Codex keeps markers because `thread/resume` can re-send neither instructions nor tool declarations.

**Tech Stack:** Python 3 · `claude-agent-sdk` in-process MCP (`create_sdk_mcp_server`) · Codex `dynamicTools` · SQLAlchemy · pytest.

**Spec:** `docs/superpowers/specs/2026-09-07-marker-to-tool-migration-design.md`

## Global Constraints

- Tool names, verbatim: `mark_complete`, `mark_no_pr`, `request_model`.
- `mark_no_pr.resolution` enum, verbatim: `"not-needed"`, `"cancelled"`. These are the same two tokens `resolution_from_text` already returns — do not invent new ones.
- `request_model.model` enum, verbatim: `"haiku"`, `"sonnet"`, `"opus"`, `"fable"`.
- New `pr_resolution_source` value, verbatim: `"tool"`. Existing values `"marker"`, `"classifier"`, `"queue-drain"` keep their meanings.
- New `ToolClass` member, verbatim: `SIGNAL = "signal"`.
- New capability, verbatim: `AgentCapabilities.durable_tool_surface`.
- **Never** delete a marker parser, `strip_markers`, or a dual-brand regex. They are permanent (spec §7).
- Tool-name comparisons go through `ccswitch.brand.mcp_tool_stem()`, never a hardcoded `mcp__viberpit__` string — both brand prefixes must resolve.
- Every commit message ends with: `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`
- Run tests with the prebuilt env and a scrubbed environment (this is a worktree):
  `env -u CCSWITCH_TAILNET_ALLOWLIST -u CCSWITCH_SESSIOND_CERT -u CCSWITCH_SESSIOND_KEY -u CCSWITCH_SESSIOND_CA -u CCSWITCH_DAEMON_ROLE CCSWITCH_DEV=1 /home/tony/GitHub/ccswitch/.venv/bin/python -m pytest <paths> -q`

## File Structure

| File | Responsibility | Task |
|---|---|---|
| `server/mcp_spec.py` | `ToolClass.SIGNAL` + its annotation mapping | 1 |
| `server/sessions/permissions.py` | `SIGNAL` joins the no-card branch | 1 |
| `server/sessions/completion.py` | `parse_mark_no_pr()` — one parser for handler + translator | 2 |
| `server/sessions/model_request.py` | `parse_request_model()`, `MODEL_TIERS` | 2 |
| `server/sessions/tools/registry.py` | three `ToolSpec` entries | 3 |
| `server/sessions/tools/context.py` | capability fields on `SessionToolContext` | 3 |
| `server/sessions/tools/tools.py` | three handlers (validate + ack) | 3 |
| `server/sessions/engine.py` | `durable_tool_surface` capability | 4 |
| `server/sessions/claude_driver.py` | capability value; `last_pr_resolution_source` | 4, 6 |
| `server/sessions/orchestrator.py` | capability → `SessionToolContext`; persist provenance | 4, 6 |
| `server/sessions/translator.py` | observe tool calls, turn-scoped, sub-agent guarded | 5 |
| `server/sessions/events.py` | `TurnEnd.pr_resolution_source` | 6 |
| `server/db/migrations/versions/` | reviewable revision (no DDL) | 6 |
| `server/automations/completion_bar.py` | `SELF_CERTIFIED_SOURCES` set | 7 |
| `server/sessions/tools/instruction.py` | `session_instructions()` — one composition | 8 |
| `server/sessions/claude_sdk_factory.py`, `server/codex/driver.py` | both call it | 8 |
| `docs/product/features/session-tools.md`, `.agents/instructions/core.md`, `docs/superpowers/evals/session-tools/` | docs + evals | 9 |

---

### Task 1: `ToolClass.SIGNAL` and the no-card permission branch

**Files:**
- Modify: `server/mcp_spec.py:32-36` (enum), `server/mcp_spec.py:54-61` (`annotations_for`)
- Modify: `server/sessions/permissions.py:156`
- Test: `server/tests/test_mcp_spec.py`, `server/tests/test_session_tool_permissions.py` (create if absent)

**Interfaces:**
- Consumes: nothing.
- Produces: `ToolClass.SIGNAL` (value `"signal"`), usable as `ToolSpec(name, ToolClass.SIGNAL, title)`.

- [ ] **Step 1: Write the failing tests**

```python
# server/tests/test_mcp_spec.py
from server.mcp_spec import ToolClass, ToolSpec, annotations_for


def test_signal_class_is_not_read_only_but_is_not_destructive():
    """A signalling tool changes session status, so readOnlyHint would be a lie —
    but it is not destructive and must never render a card."""
    ann = annotations_for(ToolSpec("mark_complete", ToolClass.SIGNAL, "Mark complete"))
    assert ann.readOnlyHint is False
    assert ann.destructiveHint is False
    assert ann.idempotentHint is True
```

```python
# server/tests/test_session_tool_permissions.py
import pytest
from server.mcp_spec import ToolClass, ToolSpec, register_surface
from server.sessions.permissions import PermissionRegistry


@pytest.mark.parametrize("mode", ["interactive", "acceptEdits", "full-auto", "bypassPermissions"])
@pytest.mark.asyncio
async def test_signal_tool_auto_allows_with_no_card_in_every_mode(mode):
    register_surface("test-signal", {"sig_probe": ToolSpec("sig_probe", ToolClass.SIGNAL, "Probe")})
    reg = PermissionRegistry()
    pid, fut, _ = reg.request(identity="u", session_id="s", tool="mcp__viberpit__sig_probe",
                              input={}, turn_id="t", mode=mode)
    assert pid is None, "a SIGNAL tool must never create a pending permission card"
    assert (await fut)["behavior"] == "allow"
```

Read `server/tests/` for the exact `PermissionRegistry.request` signature and the local
`_resolved(...)` shape before writing this — match the call style of the existing
`READ`-class test rather than inventing one.

- [ ] **Step 2: Run to verify they fail**

Run: `… -m pytest server/tests/test_mcp_spec.py server/tests/test_session_tool_permissions.py -q`
Expected: FAIL — `AttributeError: SIGNAL`.

- [ ] **Step 3: Implement**

```python
# server/mcp_spec.py
class ToolClass(str, Enum):
    READ = "read"
    MUTATION = "mutation"
    DESTRUCTIVE = "destructive"
    #: Reports the session's OWN state back to the harness (completion, no-PR,
    #: a model request). No permission card: these replace HTML-comment markers,
    #: which had no gate at all, and a card at the end of a turn would block an
    #: unattended session behind a prompt nobody is there to answer. Not READ —
    #: a signal moves a status badge, and annotating that read-only is false.
    SIGNAL = "signal"
```

```python
# server/mcp_spec.py — annotations_for
    return ToolAnnotations(
        title=spec.title,
        readOnlyHint=spec.tool_class is ToolClass.READ,
        destructiveHint=spec.tool_class is ToolClass.DESTRUCTIVE,
        idempotentHint=spec.tool_class in (ToolClass.READ, ToolClass.SIGNAL),
        openWorldHint=False,
    )
```

```python
# server/sessions/permissions.py — replace the READ short-circuit at line 156
            # READ and SIGNAL both auto-allow with no card. They differ in what they
            # claim, not in how they are gated: READ changes nothing, SIGNAL changes
            # only ccswitch's view of THIS session and replaces an ungated marker.
            if ann is not None and ann.tool_class in (ToolClass.READ, ToolClass.SIGNAL):
                return None, _resolved(True), True
```

- [ ] **Step 4: Run to verify they pass**

Run: `… -m pytest server/tests/test_mcp_spec.py server/tests/test_session_tool_permissions.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add server/mcp_spec.py server/sessions/permissions.py server/tests/test_mcp_spec.py server/tests/test_session_tool_permissions.py
git commit -F <message-file>   # feat(mcp): add a SIGNAL tool class that auto-allows without a card
```

---

### Task 2: Shared argument parsers

One parser per tool, used by BOTH the handler (to validate) and the translator (to extract). Two parsers is how the surfaces drift.

**Files:**
- Modify: `server/sessions/completion.py`, `server/sessions/model_request.py`
- Test: `server/tests/test_signal_tool_args.py` (create)

**Interfaces:**
- Consumes: `ToolArgError` from `server.mcp_spec`.
- Produces:
  - `completion.NO_PR_RESOLUTIONS: tuple[str, ...] = ("not-needed", "cancelled")`
  - `completion.parse_mark_no_pr(args: dict) -> dict` → `{"resolution": str, "note": str | None}`, raises `ToolArgError`
  - `model_request.MODEL_TIERS: tuple[str, ...] = ("haiku", "sonnet", "opus", "fable")`
  - `model_request.parse_request_model(args: dict) -> dict` → `{"model": str, "reason": str}`, raises `ToolArgError`

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_signal_tool_args.py
import pytest
from server.mcp_spec import ToolArgError
from server.sessions.completion import NO_PR_RESOLUTIONS, parse_mark_no_pr
from server.sessions.model_request import MODEL_TIERS, parse_request_model


def test_no_pr_resolutions_are_the_tokens_resolution_from_text_already_returns():
    assert NO_PR_RESOLUTIONS == ("not-needed", "cancelled")


def test_parse_mark_no_pr_accepts_a_valid_resolution_and_note():
    assert parse_mark_no_pr({"resolution": "cancelled", "note": "user pulled it"}) == {
        "resolution": "cancelled", "note": "user pulled it"}


def test_parse_mark_no_pr_defaults_note_to_none():
    assert parse_mark_no_pr({"resolution": "not-needed"})["note"] is None


@pytest.mark.parametrize("args", [{}, {"resolution": "nope"}, {"resolution": ""},
                                  {"resolution": None}, {"note": "x"}])
def test_parse_mark_no_pr_rejects_a_bad_resolution(args):
    with pytest.raises(ToolArgError):
        parse_mark_no_pr(args)


def test_parse_request_model_accepts_a_tier_and_reason():
    assert parse_request_model({"model": "opus", "reason": "cross-module refactor"}) == {
        "model": "opus", "reason": "cross-module refactor"}


def test_parse_request_model_normalizes_case_and_whitespace():
    assert parse_request_model({"model": " Opus ", "reason": "x"})["model"] == "opus"


@pytest.mark.parametrize("args", [{"model": "opus"}, {"model": "opus", "reason": "  "},
                                  {"reason": "x"}, {"model": "gpt-5", "reason": "x"}])
def test_parse_request_model_rejects_a_missing_reason_or_unknown_tier(args):
    with pytest.raises(ToolArgError):
        parse_request_model(args)


def test_model_tiers_match_the_marker_instruction_rubric():
    assert MODEL_TIERS == ("haiku", "sonnet", "opus", "fable")
```

- [ ] **Step 2: Run to verify it fails**

Run: `… -m pytest server/tests/test_signal_tool_args.py -q`
Expected: FAIL — ImportError.

- [ ] **Step 3: Implement**

```python
# server/sessions/completion.py — append
#: The two resolution tokens a no-PR completion can carry. Identical to what
#: ``resolution_from_text`` infers from prose, so the tool path and the marker
#: fallback produce values that are comparable rather than merely similar.
NO_PR_RESOLUTIONS: tuple[str, ...] = ("not-needed", "cancelled")


def parse_mark_no_pr(args: dict) -> dict:
    """Validate ``mark_no_pr`` arguments into ``{"resolution", "note"}``.

    THE parser for both halves of the tool: the handler calls it to reject a bad
    argument locally (``ToolArgError`` → a corrective message to the model, no side
    effect), and the Translator calls it on the observed tool-use block. A second
    copy on either side is how the model's acknowledgement and the harness's
    recorded resolution would come to disagree.
    """
    from server.mcp_spec import ToolArgError
    resolution = (args or {}).get("resolution")
    if not isinstance(resolution, str) or resolution not in NO_PR_RESOLUTIONS:
        raise ToolArgError(
            f"resolution must be one of {', '.join(NO_PR_RESOLUTIONS)} — "
            "'not-needed' when the objective is already satisfied without a code "
            "change, 'cancelled' when the work was called off.")
    note = (args or {}).get("note")
    return {"resolution": resolution, "note": note if isinstance(note, str) and note else None}
```

```python
# server/sessions/model_request.py — append
#: The tier vocabulary, identical to the rubric in MODEL_REQUEST_INSTRUCTION and to
#: ``automations.model_pick._ASSESS_SYSTEM``. One vocabulary, three readers.
MODEL_TIERS: tuple[str, ...] = ("haiku", "sonnet", "opus", "fable")


def parse_request_model(args: dict) -> dict:
    """Validate ``request_model`` arguments into ``{"model", "reason"}``.

    ``reason`` is REQUIRED here though the marker made it optional: the marker's
    reason was free text at the end of a sentence, while a tool argument the model
    must fill is the cheapest place to force the self-assessment that
    ``model_approval.approve``'s {requested, applied} audit exists to measure.
    """
    from server.mcp_spec import ToolArgError
    model = (args or {}).get("model")
    model = model.strip().lower() if isinstance(model, str) else None
    if model not in MODEL_TIERS:
        raise ToolArgError(f"model must be one of {', '.join(MODEL_TIERS)}")
    reason = (args or {}).get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise ToolArgError("reason is required: one short sentence on why this tier fits "
                           "the work better than the current one")
    return {"model": model, "reason": reason.strip()}
```

- [ ] **Step 4: Run to verify it passes**

Run: `… -m pytest server/tests/test_signal_tool_args.py -q`
Expected: PASS.

- [ ] **Step 5: Commit** — `feat(sessions): shared argument parsers for the three signalling tools`

---

### Task 3: Registry entries, capability-aware context, handlers

**Files:**
- Modify: `server/sessions/tools/registry.py`, `server/sessions/tools/context.py`, `server/sessions/tools/tools.py`
- Test: `server/tests/test_session_signal_tools.py` (create)

**Interfaces:**
- Consumes: Task 1's `ToolClass.SIGNAL`; Task 2's parsers.
- Produces:
  - `SESSION_TOOL_SPECS` gains `mark_complete`, `mark_no_pr`, `request_model`.
  - `SessionToolContext` gains `self_model_switch: bool = False` and `signalling_tools: bool = False`.
  - `build_session_tools(ctx)` returns `session_context`, `report_feedback`, plus the signalling tools **only when** `ctx.signalling_tools`, and `request_model` only when `ctx.self_model_switch` as well.

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_session_signal_tools.py
from server.mcp_spec import ToolClass
from server.sessions.tools.context import SessionToolContext
from server.sessions.tools.registry import SESSION_TOOL_SPECS
from server.sessions.tools.tools import build_session_tools

SIGNALS = ("mark_complete", "mark_no_pr", "request_model")


def _ctx(**kw):
    return SessionToolContext(session_id="s", identity="u", kind="coding",
                              engine="claude_code", record=lambda: None, **kw)


def test_every_signalling_tool_is_registered_as_signal_class():
    for name in SIGNALS:
        assert SESSION_TOOL_SPECS[name].tool_class is ToolClass.SIGNAL


def test_report_feedback_stays_read_class():
    """It changes nothing a user can see; mark_complete moves a status badge.
    That difference is the entire reason SIGNAL exists."""
    assert SESSION_TOOL_SPECS["report_feedback"].tool_class is ToolClass.READ


def test_signalling_tools_are_absent_when_the_engine_cannot_keep_them():
    names = {t.name for t in build_session_tools(_ctx(signalling_tools=False))}
    assert names == {"session_context", "report_feedback"}


def test_request_model_is_declared_only_when_the_engine_can_honour_it():
    on = {t.name for t in build_session_tools(_ctx(signalling_tools=True, self_model_switch=True))}
    off = {t.name for t in build_session_tools(_ctx(signalling_tools=True, self_model_switch=False))}
    assert "request_model" in on
    assert "request_model" not in off
    assert {"mark_complete", "mark_no_pr"} <= off
```

Handler tests in the same file. The tools are closures returned by `build_session_tools`;
invoke the one whose `.name` matches and read the JSON envelope out of
`result["content"][0]["text"]`. Confirm that accessor against the existing
`report_feedback` handler tests in `server/tests/` before relying on it — if the SDK's
`tool` decorator wraps the callable, those tests already show how to reach the handler.

```python
import json
import pytest


async def _call(ctx, name, args):
    tool = next(t for t in build_session_tools(ctx) if t.name == name)
    return await tool.handler(args)


def _payload(result):
    return json.loads(result["content"][0]["text"])["data"]


@pytest.mark.asyncio
async def test_mark_complete_acknowledges():
    result = await _call(_ctx(signalling_tools=True), "mark_complete", {})
    assert result.get("is_error") is not True
    assert _payload(result) == {"acknowledged": True}


@pytest.mark.asyncio
async def test_mark_no_pr_echoes_the_declared_resolution():
    result = await _call(_ctx(signalling_tools=True), "mark_no_pr",
                         {"resolution": "not-needed", "note": "already satisfied"})
    assert _payload(result) == {"acknowledged": True, "resolution": "not-needed",
                                "note": "already satisfied"}


@pytest.mark.asyncio
async def test_mark_no_pr_rejects_an_unknown_resolution_without_a_side_effect():
    """`guarded` turns ToolArgError into an is_error result, so the model gets a
    corrective message and the Translator never observes a usable call."""
    result = await _call(_ctx(signalling_tools=True), "mark_no_pr", {"resolution": "bogus"})
    assert result["is_error"] is True
    assert "not-needed" in result["content"][0]["text"]


@pytest.mark.asyncio
async def test_request_model_rejects_a_missing_reason():
    result = await _call(_ctx(signalling_tools=True, self_model_switch=True),
                         "request_model", {"model": "opus"})
    assert result["is_error"] is True
```

- [ ] **Step 2: Run to verify it fails**

Run: `… -m pytest server/tests/test_session_signal_tools.py -q`
Expected: FAIL — `KeyError: 'mark_complete'`.

- [ ] **Step 3: Implement**

```python
# server/sessions/tools/registry.py
SESSION_TOOL_SPECS: dict[str, ToolSpec] = {s.name: s for s in [
    ToolSpec("session_context", ToolClass.READ, "Read this session's context"),
    ToolSpec("report_feedback", ToolClass.READ, f"Report a problem with {brand.NAME}"),
    # SIGNAL, not READ: each moves something the user sees (a status badge, a
    # completion-bar verdict, a pending model approval). They are declared here
    # UNCONDITIONALLY even though `build_session_tools` filters them per engine —
    # the permission layer resolves a tool by name across every surface, and a name
    # missing from this table fails open into the mutation branch.
    ToolSpec("mark_complete", ToolClass.SIGNAL, "Mark this session complete"),
    ToolSpec("mark_no_pr", ToolClass.SIGNAL, "Finish without a pull request"),
    ToolSpec("request_model", ToolClass.SIGNAL, "Request a different model"),
]}
```

```python
# server/sessions/tools/context.py — two fields on SessionToolContext
    #: This engine can act on a self-directed model request
    #: (``AgentCapabilities.self_model_switch``). Declaring `request_model` without
    #: it offers a tool whose result is silently dropped.
    self_model_switch: bool = False
    #: This engine gets the signalling tools rather than the marker contract
    #: (``AgentCapabilities.durable_tool_surface``). False → Codex, which loses its
    #: declared tools on `thread/resume` and keeps the markers instead.
    signalling_tools: bool = False
```

```python
# server/sessions/tools/tools.py — inside build_session_tools, after report_feedback

    @spec_tool("mark_complete",
               "Signal that you have FULLY accomplished the objective of this session. "
               "Call it only when the work is genuinely complete — never for an interim "
               "step, never while you are still working or waiting on something, and never "
               "in the same turn as a question to the user: if you need an answer, ask and "
               "do not call this. Optional follow-ups belong in prose as statements ('I can "
               "also do X if wanted'), not questions.",
               {"type": "object", "properties": {}, "required": []})
    @guarded
    async def mark_complete(args: dict[str, Any]) -> Any:
        return {"acknowledged": True}

    @spec_tool("mark_no_pr",
               "Signal that the work is finished but NO pull request is appropriate. Use "
               "this INSTEAD of mark_complete when finishing without a code change. "
               "resolution: 'not-needed' when the objective was already satisfied without "
               "one, 'cancelled' when the user called the work off or you deliberately "
               "decided not to open one. An objective that asked for a code change is NOT "
               "finished by investigating it — discovering the work is larger than expected, "
               "recommending a spec, or offering to continue is a hand-back: say what you "
               "found, ask how to proceed, and call nothing. note: one short sentence.",
               {"type": "object",
                "properties": {"resolution": {"type": "string", "enum": list(NO_PR_RESOLUTIONS)},
                               "note": {"type": "string"}},
                "required": ["resolution"]})
    @guarded
    async def mark_no_pr(args: dict[str, Any]) -> Any:
        return {"acknowledged": True, **parse_mark_no_pr(args)}

    @spec_tool("request_model",
               "Ask to run on a different model tier, when the work has turned out "
               "materially harder or easier than it looked. haiku: trivial/mechanical — "
               "typo fixes, config bumps, dependency pins. sonnet: one feature or bugfix "
               "with its tests. opus: hard multi-file work, tricky debugging, CI failures "
               "with unclear cause. fable: the hardest architectural or cross-cutting work. "
               "A cheaper tier applies on your next turn; a more expensive one is held for a "
               "human, who may approve it at a different tier. Never routine, never twice "
               "for the same conclusion, and never wait for the switch — keep working.",
               {"type": "object",
                "properties": {"model": {"type": "string", "enum": list(MODEL_TIERS)},
                               "reason": {"type": "string"}},
                "required": ["model", "reason"]})
    @guarded
    async def request_model(args: dict[str, Any]) -> Any:
        return {"acknowledged": True, **parse_request_model(args)}

    tools = [session_context, report_feedback]
    if ctx.signalling_tools:
        tools += [mark_complete, mark_no_pr]
        if ctx.self_model_switch:
            tools.append(request_model)
    return tools
```

Import `NO_PR_RESOLUTIONS`/`parse_mark_no_pr` and `MODEL_TIERS`/`parse_request_model` at the
top of `tools.py`.

- [ ] **Step 4: Run to verify it passes**

Run: `… -m pytest server/tests/test_session_signal_tools.py server/tests/test_session_tools.py -q`
Expected: PASS. If an existing test asserts the surface is exactly two tools, update it to
assert the two READ tools are always present rather than deleting the assertion.

- [ ] **Step 5: Commit** — `feat(sessions): declare mark_complete, mark_no_pr and request_model`

---

### Task 4: `durable_tool_surface` capability, wired to the context

**Files:**
- Modify: `server/sessions/engine.py:24-45`, `server/sessions/claude_driver.py:145-160`, `server/sessions/orchestrator.py:1380-1395`
- Test: `server/tests/test_engine_capabilities.py` (or the existing capabilities test), `server/tests/test_session_signal_tools.py`

**Interfaces:**
- Consumes: Task 3's `SessionToolContext` fields.
- Produces: `AgentCapabilities.durable_tool_surface: bool = False`; `claude_code` sets it `True`; the orchestrator passes both capabilities into `SessionToolContext`.

- [ ] **Step 1: Write the failing test**

```python
def test_claude_keeps_its_tool_surface_across_a_resume():
    from server.sessions.claude_driver import ClaudeAgentDriver
    assert ClaudeAgentDriver.capabilities.durable_tool_surface is True


def test_codex_does_not_and_therefore_keeps_the_markers():
    """`thread/resume` carries neither `developerInstructions` nor `dynamicTools`
    (codex-cli 0.153.2), so a resumed Codex thread keeps its original instructions
    and loses its tools — with nothing sendable to correct it."""
    from server.codex.driver import CodexAgentDriver
    assert CodexAgentDriver.capabilities.durable_tool_surface is False
```

Plus, in `test_session_signal_tools.py`, a test that the orchestrator's context build
sets `signalling_tools` from the engine's capability. Follow the existing
`_setup_session_tools`/overlay test in `server/tests/` for how the orchestrator is
constructed there.

- [ ] **Step 2: Run to verify it fails** — `AttributeError: durable_tool_surface`.

- [ ] **Step 3: Implement**

```python
# server/sessions/engine.py — inside AgentCapabilities
    #: This engine's DECLARED tool surface survives a resume. An engine that
    #: re-declares nothing on resume must keep the marker contract: its sessions
    #: would be told to call tools that are gone by the second turn, with no
    #: documented fallback and nothing sendable to correct it. Codex is that
    #: engine today (`ThreadResumeParams` carries neither `developerInstructions`
    #: nor `dynamicTools`); this flips to True on its own the moment the vendor
    #: contract gains them.
    durable_tool_surface: bool = False
```

Add `durable_tool_surface=True` to `ClaudeAgentDriver.capabilities`. Leave Codex's
untouched (it defaults False) but add a comment there pointing at the resume gap.

In `orchestrator.py`'s `SessionToolContext(...)` construction, resolve the driver class
for `selected` and pass:

```python
        caps = driver_capabilities_for(selected)   # existing engine-registry lookup
        ctx = SessionToolContext(
            session_id=session_id, identity=identity, kind=kind, engine=selected.value,
            record=lambda: self._records.get(session_id), host=self_host_name(),
            self_model_switch=(caps.self_model_switch
                               and get_settings().self_model_switch_enabled),
            signalling_tools=caps.durable_tool_surface)
```

Use whatever the canonical capability lookup in `server/sessions/engine.py` is called —
grep for how other call sites resolve capabilities from an `AgentEngine`; do not add a
second lookup helper.

- [ ] **Step 4: Run to verify it passes**
- [ ] **Step 5: Commit** — `feat(sessions): gate the signalling tools on a durable_tool_surface capability`

---

### Task 5: The Translator observes the tool calls

The heart of the change. Read spec §4 before starting.

**Files:**
- Modify: `server/sessions/translator.py` (`__init__`, `_reset`, the `AssistantMessage` tool loop at 125-136, the `ResultMessage` branch at 155-181)
- Test: `server/tests/test_translator_signal_tools.py` (create)

**Interfaces:**
- Consumes: Task 2's parsers; `ccswitch.brand.mcp_tool_stem`.
- Produces: `TurnEnd.outcome`, `.pr_resolution`, `.model_request` populated from tool calls, plus `TurnEnd.pr_resolution_source` (`"tool"` or `"marker"`) — the field itself is added in Task 6; until then set the value on a local and land the field with Task 6. **Implement Task 6 immediately after this one; do not leave them apart.**

- [ ] **Step 1: Write the failing tests**

Model them on the existing `server/tests/test_translator.py` helpers for building
`AssistantMessage` / `ResultMessage`.

```python
MARK_COMPLETE = "mcp__viberpit__mark_complete"


def test_a_mark_complete_call_completes_the_turn():
    t = Translator()
    feed_assistant_tool_call(t, MARK_COMPLETE, {}, pid=None)
    end = feed_result(t)
    assert end.outcome == "complete"


def test_a_subagent_mark_complete_does_NOT_complete_the_parent_session():
    """The guard that text scanning has at translator.py:119 and an MCP handler
    cannot reproduce: a Task sub-agent finishing ITS objective is not the session
    finishing. Regression test for the whole design of spec §4."""
    t = Translator()
    feed_assistant_tool_call(t, MARK_COMPLETE, {}, pid="toolu_parent")
    end = feed_result(t)
    assert end.outcome == "success"


def test_a_tool_call_survives_a_later_message_in_the_same_turn():
    """`_turn_text` resets on every top-level assistant message, so text-based
    signalling only ever reads the LAST message. Tool state must not: a session
    calls mark_complete and then writes a closing paragraph."""
    t = Translator()
    feed_assistant_tool_call(t, MARK_COMPLETE, {}, pid=None)
    feed_assistant_text(t, "and here is what I did.", pid=None)
    assert feed_result(t).outcome == "complete"


def test_signal_state_clears_between_turns():
    t = Translator()
    feed_assistant_tool_call(t, MARK_COMPLETE, {}, pid=None)
    assert feed_result(t).outcome == "complete"
    feed_assistant_text(t, "next turn, still working", pid=None)
    assert feed_result(t).outcome == "success"


def test_mark_no_pr_sets_the_declared_resolution_and_a_tool_source():
    t = Translator()
    feed_assistant_tool_call(t, "mcp__viberpit__mark_no_pr",
                             {"resolution": "cancelled", "note": "user pulled it"}, pid=None)
    end = feed_result(t)
    assert end.outcome == "complete"
    assert end.pr_resolution == "cancelled"
    assert end.pr_resolution_source == "tool"


def test_the_tool_wins_over_a_marker_in_the_same_turn():
    t = Translator()
    feed_assistant_tool_call(t, "mcp__viberpit__mark_no_pr", {"resolution": "cancelled"}, pid=None)
    feed_assistant_text(t, "done\n<!-- viberpit:no-pr -->", pid=None)
    end = feed_result(t)
    assert end.pr_resolution == "cancelled", "a declaration beats an inferred token"
    assert end.pr_resolution_source == "tool"


def test_a_marker_still_works_with_no_tool_call():
    t = Translator()
    feed_assistant_text(t, "all set\n<!-- viberpit:complete -->", pid=None)
    assert feed_result(t).outcome == "complete"


def test_the_pre_rebrand_tool_name_is_still_recognised():
    t = Translator()
    feed_assistant_tool_call(t, "mcp__ccswitch__mark_complete", {}, pid=None)
    assert feed_result(t).outcome == "complete"


def test_a_malformed_mark_no_pr_is_ignored_rather_than_crashing_the_turn():
    t = Translator()
    feed_assistant_tool_call(t, "mcp__viberpit__mark_no_pr", {"resolution": "bogus"}, pid=None)
    end = feed_result(t)
    assert end.pr_resolution is None
    assert end.outcome == "success"
```

- [ ] **Step 2: Run to verify they fail**

Run: `… -m pytest server/tests/test_translator_signal_tools.py -q`
Expected: FAIL — outcome is `"success"`, `pr_resolution` is None.

- [ ] **Step 3: Implement**

In `__init__` and `_reset`, add turn-scoped state (note `_reset` runs at turn end, NOT per
message — that is exactly the property these need):

```python
        self._turn_signals: dict = {}   # tool stem -> parsed payload, for THIS turn
```

In the `AssistantMessage` tool loop, right after the `AskUserQuestion` guard:

```python
                    # Signalling tools are recorded, not rendered: the ToolCall card
                    # for "mark_complete" would be noise on top of the status change
                    # it causes. Only the TOP-LEVEL agent's call counts — a Task
                    # sub-agent calling mark_complete finished its own sub-objective,
                    # the same rule the text scan applies at `pid is None` above.
                    stem = brand.mcp_tool_stem(b.name)
                    if stem in _SIGNAL_TOOLS:
                        if pid is None:
                            self._record_signal(stem, b.input)
                        continue
```

```python
_SIGNAL_TOOLS = frozenset({"mark_complete", "mark_no_pr", "request_model"})


    def _record_signal(self, stem: str, args) -> None:
        """Store one signalling tool call for the turn. Never raises: a malformed
        argument is dropped (the handler already returned an is_error to the model)
        rather than failing the turn it was trying to close."""
        try:
            if stem == "mark_complete":
                self._turn_signals[stem] = {}
            elif stem == "mark_no_pr":
                self._turn_signals[stem] = parse_mark_no_pr(args or {})
            elif stem == "request_model":
                self._turn_signals[stem] = parse_request_model(args or {})
        except Exception:  # noqa: BLE001 - a bad argument must not fail the turn
            pass
```

In the `ResultMessage` branch, replace the resolution/outcome computation:

```python
            signals = self._turn_signals
            no_pr = signals.get("mark_no_pr")
            inferred = resolution_from_text(self._turn_text)
            if no_pr is not None:
                pr_resolution = no_pr["resolution"]
                pr_source = "tool"
                if inferred is not None and inferred != pr_resolution:
                    # Spec §8: the declared value against what the prose heuristic
                    # would have guessed. Emitted here because this is the only place
                    # that holds both — a tool handler never sees the turn text.
                    # Logged only on disagreement; agreement is the boring case.
                    log.info("sessions.no_pr_resolution_disagreement declared=%s inferred=%s",
                             pr_resolution, inferred)
            else:
                pr_resolution = inferred
                pr_source = "marker" if pr_resolution is not None else None
            model_request = signals.get("request_model") or self._turn_end_model_request()
            completed = ("mark_complete" in signals or no_pr is not None
                         or has_completion_marker(self._turn_text))
            if msg.subtype != "success" or bool(getattr(msg, "is_error", False)):
                outcome = "failed"
            elif completed:
                outcome = "complete"
            else:
                outcome = "success"
```

`model_request` from the tool is already `{"model", "reason"}` — the same shape
`parse_model_request` returns — so every downstream consumer is untouched.

Add `_turn_signals = {}` to `_reset()`. Import `parse_mark_no_pr`,
`parse_request_model`, `from ccswitch import brand`, and add a module logger
(`log = logging.getLogger(__name__)`) — `translator.py` has none today.

Also add this test for the disagreement log:

```python
def test_a_declared_resolution_that_contradicts_the_prose_is_logged(caplog):
    t = Translator()
    feed_assistant_tool_call(t, "mcp__viberpit__mark_no_pr", {"resolution": "not-needed"},
                             pid=None)
    feed_assistant_text(t, "the user cancelled this work", pid=None)
    end = feed_result(t)
    assert end.pr_resolution == "not-needed", "the declaration is still what is recorded"
    assert "no_pr_resolution_disagreement" in caplog.text
```

- [ ] **Step 4: Run to verify they pass**

Run: `… -m pytest server/tests/test_translator_signal_tools.py server/tests/test_translator.py -q`
Expected: PASS, including the pre-existing translator tests.

- [ ] **Step 5: Commit** — `feat(sessions): read the signalling tools in the Translator, sub-agent guarded`

---

### Task 6: Provenance through the driver to the DB

**Files:**
- Modify: `server/sessions/events.py:77-88`, `server/sessions/claude_driver.py:257,1009,1221-1225`, `server/sessions/orchestrator.py:4048,4056-4058,4085-4088`
- Create: `server/db/migrations/versions/<rev>_pr_resolution_source_tool.py`
- Test: `server/tests/test_driver_pr_resolution.py` (extend), `server/tests/test_status_persist.py` (or wherever `pr_resolution_source` persistence is covered)

**Interfaces:**
- Consumes: Task 5's `pr_source`.
- Produces: `TurnEnd.pr_resolution_source: str | None`; `ClaudeAgentDriver.last_pr_resolution_source: str | None`; the orchestrator writes it instead of the literal `"marker"`.

- [ ] **Step 1: Write the failing test**

`server/tests/test_driver_pr_resolution.py` already has a `FakeSDKClient` whose single
turn carries the no-PR marker, and asserts `driver.last_pr_resolution`. Extend that file
using the same fake — give it a variant whose turn contains a `mark_no_pr` tool-use block
instead of marker text (Task 5's translator turns that into
`TurnEnd(pr_resolution="not-needed", pr_resolution_source="tool")`).

```python
@pytest.mark.asyncio
async def test_the_driver_reports_the_source_alongside_the_resolution():
    driver = await _run_one_turn(FakeSDKClientWithNoPrTool())      # new fake, mirrors the existing one
    assert driver.last_pr_resolution == "not-needed"
    assert driver.last_pr_resolution_source == "tool"


@pytest.mark.asyncio
async def test_a_marker_turn_still_reports_a_marker_source():
    driver = await _run_one_turn(FakeSDKClient())                  # the existing marker fake
    assert driver.last_pr_resolution_source == "marker"


@pytest.mark.asyncio
async def test_a_driver_stub_without_the_new_attribute_still_persists_as_marker():
    """`getattr(..., None) or "marker"` in the orchestrator — a partial test double
    must keep behaving exactly as it did before this change."""
    class Stub:
        last_pr_resolution = "not-needed"
    assert (getattr(Stub(), "last_pr_resolution_source", None) or "marker") == "marker"
```

For the persistence half, extend whichever test currently asserts
`pr_resolution_source == "marker"` after a complete status (find it with
`grep -rln 'pr_resolution_source' server/tests/`) so it is parameterised over
`("marker", "tool")` and asserts the row matches what the driver reported.

- [ ] **Step 2: Run to verify it fails** — `AttributeError: last_pr_resolution_source`.

- [ ] **Step 3: Implement**

`TurnEnd` gains `pr_resolution_source: str | None = None` and `"prResolutionSource"` in
`to_wire()`. Place it after `pr_resolution`; the dataclass has defaults throughout so
ordering is safe, but confirm no positional construction of `TurnEnd` breaks (`grep -rn
"TurnEnd(" server/`).

Driver: add `self._last_pr_resolution_source: str | None = None` beside line 257; set it
next to line 1009 (`self._last_pr_resolution = e.pr_resolution`); expose the property
mirroring `last_pr_resolution`'s stickiness and docstring.

Orchestrator: read `source = getattr(driver, "last_pr_resolution_source", None) or "marker"`
next to line 4048, and use `source` in place of both hardcoded `"marker"` literals
(4058, 4088). The `or "marker"` default keeps a partial driver stub behaving exactly as
before.

Migration: a revision with `down_revision` chained off the newest one in
`server/db/migrations/versions/`, whose `upgrade()`/`downgrade()` are `pass` with a
docstring recording that `pr_resolution_source` is an unconstrained nullable `String` that
gains the value `"tool"` — no DDL, but the revision is the reviewable record the repo
requires for a schema-semantics change.

- [ ] **Step 4: Run to verify it passes**
- [ ] **Step 5: Commit** — `feat(sessions): carry pr_resolution provenance from the tool call to the row`

---

### Task 7: The completion bar must not trust a tool-sourced self-claim

**This is the task that prevents a regression, not one that adds a feature.** Read spec §8.

**Files:**
- Modify: `server/automations/completion_bar.py:103-125`
- Test: `server/tests/test_completion_bar.py` (extend)

**Interfaces:**
- Consumes: Task 6's `"tool"` source.
- Produces: `SELF_CERTIFIED_SOURCES: frozenset[str]` replacing `SELF_CERTIFIED_SOURCE`.

- [ ] **Step 1: Write the failing test**

`test_completion_bar.py` already has a `PrFacts` helper at its top (line 5,
`PrFacts(has_repo=has_repo, prs=tuple(prs))`) — use it rather than constructing
`PrFacts` directly. Below, `_facts()` is that helper: a repo-attached session with no
PRs at all, which is the shape the guard exists for.

```python
GOAL = "implement X with persistent storage"


def test_a_tool_sourced_no_pr_claim_is_still_unverified():
    """Session fdc6b27a (prod, 2026-09-06) researched, wrote no code, recommended a
    spec and self-certified past the bar. Moving that claim from a marker to a tool
    does not make the session a neutral judge of its own work."""
    assert is_unverified_self_resolution(GOAL, _facts(has_repo=True), "not-needed",
                                         "tool") is True


def test_a_marker_sourced_claim_is_unchanged():
    assert is_unverified_self_resolution(GOAL, _facts(has_repo=True), "not-needed",
                                         "marker") is True


def test_a_classifier_sourced_resolution_is_still_trusted():
    """Something other than the claimant judged it, which is the whole point."""
    assert is_unverified_self_resolution(GOAL, _facts(has_repo=True), "not-needed",
                                         "classifier") is False


def test_a_cancelled_resolution_stays_trusted_from_either_transport():
    """`cancelled` is a decision, not a completion claim."""
    for source in ("marker", "tool"):
        assert is_unverified_self_resolution(GOAL, _facts(has_repo=True), "cancelled",
                                             source) is False


def test_every_source_a_session_can_set_itself_is_self_certifying():
    """Enumerated so a fifth source cannot be added without deciding which side of
    this line it falls on."""
    assert SELF_CERTIFIED_SOURCES == frozenset({"marker", "tool"})


def test_a_tool_sourced_not_needed_does_not_short_circuit_met_bar():
    v = met_bar(GOAL, "user", _facts(has_repo=True), "not-needed",
                pr_resolution_source="tool")
    assert v.met is False
    assert v.confidence != "high"
```

- [ ] **Step 2: Run to verify it fails** — the first and last assert fail: `"tool" != "marker"`.

- [ ] **Step 3: Implement**

```python
# The ``pr_resolution_source`` values under which a stored resolution is the session's
# OWN claim about its own work — whether it arrived as a ``<!-- viberpit:no-pr -->``
# marker or as a ``mark_no_pr`` tool call. Every other source has already been judged
# (``classifier``) or is not a completion claim at all (``queue-drain``: the session
# never ran). A set, not a constant: the transport changed once and will change again,
# and a single-value comparison silently promotes a new transport's claim to a verdict.
SELF_CERTIFIED_SOURCES = frozenset({"marker", "tool"})
```

and `... and pr_resolution_source in SELF_CERTIFIED_SOURCES` in
`is_unverified_self_resolution`. Update the docstring's marker-specific wording to name
both transports.

- [ ] **Step 4: Run to verify it passes**

Run: `… -m pytest server/tests/test_completion_bar.py server/tests/test_autopilot_context.py -q`

- [ ] **Step 5: Commit** — `fix(completion-bar): a tool-sourced no-PR claim is still the session's own claim`

---

### Task 8: One instruction composition

**Files:**
- Modify: `server/sessions/tools/instruction.py`, `server/sessions/claude_sdk_factory.py:12-30`, `server/codex/driver.py:543-567`
- Test: `server/tests/test_session_tools_instruction.py` (create or extend)

**Interfaces:**
- Consumes: Tasks 3 and 4.
- Produces: `session_instructions(*, session_tools: bool, signalling_tools: bool, self_model_switch: bool) -> str`. `append_session_tools` is DELETED in the same change (spec: purge replaced code), with both call sites moved over.

- [ ] **Step 1: Write the failing test**

```python
def test_a_tools_session_is_never_also_told_about_markers():
    s = session_instructions(session_tools=True, signalling_tools=True, self_model_switch=True)
    assert "mark_complete" in s
    assert "<!--" not in s, "two ways to do one thing is the failure mode this guards"


def test_a_marker_session_is_never_told_about_signalling_tools():
    s = session_instructions(session_tools=True, signalling_tools=False, self_model_switch=False)
    assert COMPLETION_MARKER in s
    assert "mark_complete" not in s


def test_request_model_is_only_named_when_the_engine_can_honour_it():
    on = session_instructions(session_tools=True, signalling_tools=True, self_model_switch=True)
    off = session_instructions(session_tools=True, signalling_tools=True, self_model_switch=False)
    assert "request_model" in on and "request_model" not in off


def test_the_surface_off_entirely_is_exactly_the_marker_contract():
    s = session_instructions(session_tools=False, signalling_tools=False, self_model_switch=True)
    assert s == marker_instructions(self_model_switch=True)


def test_the_paragraph_never_hardcodes_a_tool_count():
    """It said 'two MCP tools' when there were two. Generated from the mounted set
    so it cannot drift from what was declared."""
    assert "two MCP tools" not in session_instructions(
        session_tools=True, signalling_tools=True, self_model_switch=True)
```

- [ ] **Step 2: Run to verify it fails** — ImportError.

- [ ] **Step 3: Implement**

Rewrite `instruction.py` so the paragraph is built from the mounted tool names (the same
predicate `build_session_tools` uses — extract it to one helper both call, do not restate
the condition), and `session_instructions` returns marker instructions when
`signalling_tools` is False. Keep the existing docstring's point about exactly one
composition for both engines.

Both call sites pass their already-resolved answers:

```python
# claude_sdk_factory._build_append_string
    return session_instructions(session_tools=cfg.session_tools_enabled,
                                signalling_tools=cfg.session_tools_enabled,
                                self_model_switch=cfg.self_model_switch_enabled)
```

```python
# codex/driver.py — thread/start
                    developer_instructions=session_instructions(
                        session_tools=self._session_tools is not None,
                        signalling_tools=False,   # resume re-declares nothing (spec §5)
                        self_model_switch=False),
```

Delete `append_session_tools` and update every import of it.

- [ ] **Step 4: Run to verify it passes**

Run: `… -m pytest server/tests/test_session_tools_instruction.py server/tests/test_codex_driver.py server/tests/test_claude_sdk_factory.py -q`

- [ ] **Step 5: Commit** — `feat(sessions): one instruction composition for markers and signalling tools`

---

### Task 9: Docs and evals

**Files:**
- Modify: `docs/product/features/session-tools.md`, `.agents/instructions/core.md` (the "Session tool surface" and "Self-directed model switching" sections)
- Create: eval cases under `docs/superpowers/evals/session-tools/`

- [ ] **Step 1: Update `session-tools.md`** — the five tools, the SIGNAL class and why it renders no card, the engine split table from spec §5, and the permanent marker fallback.
- [ ] **Step 2: Update `core.md`** — "Session tool surface" gains the three tools and the capability gate. "Self-directed model switching" changes `<!-- ccswitch:model=… -->` to `request_model`, keeping one sentence that the marker is still parsed. Do not delete the guards/policy paragraphs — none of that behaviour changed.
- [ ] **Step 3: Add eval cases** — one per tool, following the existing session-tools eval format, including the negatives the descriptions emphasise: no `mark_complete` in a turn that asks a question; a hand-back is not `mark_no_pr`.
- [ ] **Step 4: Run the evals** — `python scripts/run_evals.py` (filtered to session-tools if the runner supports it).
- [ ] **Step 5: Commit** — `docs(sessions): document the signalling tool surface and its engine split`

---

## Final verification (before the PR)

- [ ] `ruff check src/ server/`
- [ ] `mypy server/ --ignore-missing-imports`
- [ ] `pytest tests/` (core)
- [ ] `pytest server/tests/` — full, backgrounded, tee'd to `/tmp/`, holding a test slot per the `/api/test-slots` block in `core.md` **in the same shell as the pytest command**
- [ ] Confirm no marker parser, `strip_markers`, or dual-brand regex was deleted: `grep -rn "has_completion_marker\|resolution_from_text\|parse_model_request\|strip_markers" server/ | grep -v tests`

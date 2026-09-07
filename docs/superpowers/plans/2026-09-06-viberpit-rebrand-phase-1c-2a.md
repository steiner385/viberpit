# ViberPit Rebrand — Phase 1c + 2a Implementation Plan (agent prompts, markers, MCP namespace)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Sessions are told to emit `<!-- viberpit:complete -->` / `:no-pr` / `:model=`, the assistant's MCP server mounts as `viberpit` (tools `mcp__viberpit__*`), the prompt-context header becomes `<viberpit-context …/>`, and the agent-facing prose says ViberPit — while every parser and permission matcher accepts the legacy `ccswitch` forms permanently, because stored transcripts are re-parsed on replay and in-flight sessions were prompted with the old forms.

**Architecture:** `src/ccswitch/brand.py` grows the marker stem, the context tag, and a legacy MCP prefix plus two helpers (`is_app_tool`, `strip_mcp_prefix`) so every matcher asks brand instead of string-matching a prefix. Emitters flip to the new form; readers accept both. `web/src/brand.ts` mirrors the prefix pair and the Python drift-guard test reads both constants.

**Tech Stack:** Python 3.11 (FastAPI server, `claude-agent-sdk`, Codex app-server overlay), pytest; React + TypeScript, vitest.

**Spec:** `docs/superpowers/specs/2026-09-06-viberpit-rebrand-design.md` — §3 Phase 1c and Phase 2a, §4 risks 2 and 7, §7 (marker and prefix dual-accept are kept forever).

## Global Constraints

- `brand.NAME == "ViberPit"`, `brand.SLUG == "viberpit"`, `brand.LEGACY_SLUG == "ccswitch"` (already on master from Phases 0a/1a).
- **Emit new, accept both, forever:** every reader of the completion / no-PR / model markers, of the context header, and of the MCP tool prefix accepts the `ccswitch` and `viberpit` forms; every emitter produces the `viberpit` form. No test that feeds a legacy form may be deleted — it now proves the compat path.
- The MCP prefix gates a fail-closed permission registry (`server/sessions/permissions.py`) and an assistant hard-deny (`server/sessions/claude_driver.py`): a matcher that does not accept the mounted name denies every tool call. Resumed Codex threads keep their old `dynamicTools` names (`server/assistant/options_codex.py`), so both prefixes must pass every matcher.
- Still legacy after this plan: host paths (Phase 3), the `.ccswitch/` repo namespace and `ccswitch/` branch prefix (Phase 2c/4), cookie and `X-CCSwitch-*` headers (2b), localStorage keys (2b), env var names, CLI binary and prefixes (5), `.agents/instructions/core.md` and AGENTS.md prose (Phase 6 — the marker it documents keeps working via dual-accept).
- No hand-typed brand literal outside `brand.py` / `brand.ts`: the scan test in `server/tests/test_brand_wire_contracts.py` must stay green, and the new legacy literal may only appear in those two files.
- Tests: `PYTHONPATH=$PWD/src /home/tony/GitHub/ccswitch/.venv/bin/python -m pytest …` with every `CCSWITCH_*` unset first (`for v in $(env | grep -o '^CCSWITCH_[A-Z0-9_]*'); do unset $v; done`). Web gate: `(cd web && npm run typecheck && npm run lint && npm run test -- --run)`; never `cd web` persistently.
- Four-step Python gate before the PR: `ruff check src/ server/ scripts/`, `mypy server/ --ignore-missing-imports`, `pytest tests/`, `pytest server/tests/ -n auto --dist loadfile --timeout=300` (background, log to `/tmp/`; one full run at a time).
- Grep gates sweep `server src scripts web/src web/e2e` (Phase 1's gate missed `web/e2e`).
- Commit trailer `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`, messages via `-F`; **push after every commit**; PR ready + `gh pr merge --squash --auto`.

---

### Task 1: `brand` grows the marker stem, context tag, and MCP prefix pair

**Files:**
- Modify: `src/ccswitch/brand.py`
- Modify: `web/src/brand.ts`
- Test: `tests/test_brand.py`

**Interfaces:**
- Produces (Python): `brand.MARKER_STEM = "viberpit"`, `brand.LEGACY_MARKER_STEM = "ccswitch"`, `brand.CONTEXT_TAG = "viberpit-context"`, `brand.LEGACY_CONTEXT_TAG = "ccswitch-context"`, `brand.MCP_SERVER_NAME == "viberpit"`, `brand.MCP_TOOL_PREFIX == "mcp__viberpit__"`, `brand.LEGACY_MCP_TOOL_PREFIX == "mcp__ccswitch__"`, `brand.MCP_TOOL_PREFIXES: tuple[str, str]`, `brand.is_app_tool(name: str) -> bool`, `brand.strip_mcp_prefix(name: str) -> str | None`.
- Produces (web): `MCP_TOOL_PREFIX = 'mcp__viberpit__'`, `LEGACY_MCP_TOOL_PREFIX = 'mcp__ccswitch__'`, `MCP_TOOL_PREFIXES`, `stripMcpPrefix(name: string): string | undefined`.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_brand.py`)

```python
def test_marker_stem_is_the_slug_and_legacy_is_frozen():
    assert brand.MARKER_STEM == brand.SLUG
    assert brand.LEGACY_MARKER_STEM == brand.LEGACY_SLUG == "ccswitch"
    assert brand.CONTEXT_TAG == f"{brand.SLUG}-context"
    assert brand.LEGACY_CONTEXT_TAG == "ccswitch-context"


def test_mcp_server_name_flipped_and_legacy_prefix_kept():
    assert brand.MCP_SERVER_NAME == brand.SLUG
    assert brand.MCP_TOOL_PREFIX == "mcp__viberpit__"
    assert brand.LEGACY_MCP_TOOL_PREFIX == "mcp__ccswitch__"
    assert brand.MCP_TOOL_PREFIXES == (brand.MCP_TOOL_PREFIX, brand.LEGACY_MCP_TOOL_PREFIX)


def test_is_app_tool_accepts_both_prefixes_only():
    assert brand.is_app_tool("mcp__viberpit__list_sessions")
    assert brand.is_app_tool("mcp__ccswitch__list_sessions")
    assert not brand.is_app_tool("mcp__github__list_issues")
    assert not brand.is_app_tool("Bash")


def test_strip_mcp_prefix_returns_bare_tool_or_none():
    assert brand.strip_mcp_prefix("mcp__viberpit__get_session") == "get_session"
    assert brand.strip_mcp_prefix("mcp__ccswitch__get_session") == "get_session"
    assert brand.strip_mcp_prefix("mcp__other__get_session") is None
    assert brand.strip_mcp_prefix("mcp__viberpit__") is None
```

- [ ] **Step 2: Run to verify they fail**

Run: `PYTHONPATH=$PWD/src /home/tony/GitHub/ccswitch/.venv/bin/python -m pytest tests/test_brand.py -v`
Expected: the four new tests FAIL with `AttributeError` (no `MARKER_STEM` …).

- [ ] **Step 3: Extend `brand.py`**

Replace the `MCP_SERVER_NAME` / `MCP_TOOL_PREFIX` block with:

```python
#: MCP server wire name. The SDK derives tool names ``mcp__<name>__<tool>`` from it and
#: the permission registry matches the prefix. Flipped in Phase 2a; the legacy prefix is
#: accepted FOREVER (resumed Codex threads keep their old dynamicTools names, and stored
#: transcripts are re-parsed on replay). Every matcher goes through ``is_app_tool`` /
#: ``strip_mcp_prefix`` rather than a hand-typed ``startswith``.
MCP_SERVER_NAME = SLUG
MCP_TOOL_PREFIX = f"mcp__{MCP_SERVER_NAME}__"
LEGACY_MCP_TOOL_PREFIX = f"mcp__{LEGACY_SLUG}__"
MCP_TOOL_PREFIXES: tuple[str, str] = (MCP_TOOL_PREFIX, LEGACY_MCP_TOOL_PREFIX)

#: Stem of the agent-facing markers ``<!-- <stem>:complete -->`` / ``:no-pr`` / ``:model=``
#: (server/sessions/completion.py, model_request.py) and of the ``<<stem>-context …/>``
#: prompt header (server/assistant/context.py). Emitters use MARKER_STEM; parsers accept both.
MARKER_STEM = SLUG
LEGACY_MARKER_STEM = LEGACY_SLUG
CONTEXT_TAG = f"{MARKER_STEM}-context"
LEGACY_CONTEXT_TAG = f"{LEGACY_MARKER_STEM}-context"


def strip_mcp_prefix(name: str) -> str | None:
    """The bare tool name if *name* is one of the app's own MCP tools (either prefix),
    else ``None``. An empty remainder is not a tool."""
    for prefix in MCP_TOOL_PREFIXES:
        if name.startswith(prefix):
            rest = name[len(prefix):]
            return rest or None
    return None


def is_app_tool(name: str) -> bool:
    """Whether *name* is one of the app's own MCP tools under either prefix."""
    return strip_mcp_prefix(name) is not None
```

`brand.py` stays a leaf module (no imports beyond `__future__`).

`web/src/brand.ts` — replace the `MCP_TOOL_PREFIX` block with:

```ts
/**
 * Wire prefix of the app's own MCP tools (`mcp__<server>__<tool>`). Flipped in rebrand
 * Phase 2a; the legacy prefix is accepted forever (resumed sessions keep old names).
 * `server/tests/test_brand_wire_contracts.py` reads both constants and fails if they
 * drift from `src/ccswitch/brand.py`.
 */
export const MCP_TOOL_PREFIX = 'mcp__viberpit__'
export const LEGACY_MCP_TOOL_PREFIX = 'mcp__ccswitch__'
export const MCP_TOOL_PREFIXES = [MCP_TOOL_PREFIX, LEGACY_MCP_TOOL_PREFIX] as const

/** Bare tool name when `name` carries either app prefix; `undefined` otherwise. */
export function stripMcpPrefix(name: string): string | undefined {
  for (const p of MCP_TOOL_PREFIXES) {
    if (name.startsWith(p)) return name.slice(p.length) || undefined
  }
  return undefined
}
```

- [ ] **Step 4: Run the tests**

Run: `PYTHONPATH=$PWD/src /home/tony/GitHub/ccswitch/.venv/bin/python -m pytest tests/test_brand.py -v`
Expected: all pass. `server/tests/test_brand_wire_contracts.py` will now FAIL (`tools.MCP_PREFIX` still legacy, drift guard sees a new shape) — expected until Task 3 fixes both; do not commit a green claim for it.

- [ ] **Step 5: Commit and push**

```bash
git add src/ccswitch/brand.py web/src/brand.ts tests/test_brand.py
printf 'feat(brand): marker stem, context tag, and MCP prefix pair for the Phase 2a dual-accept\n\nMCP_SERVER_NAME flips to viberpit; the legacy prefix stays as a first-class constant\nwith is_app_tool/strip_mcp_prefix so no matcher string-matches a prefix by hand.\n\nCo-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>\n' > /tmp/p2-c1.txt
git commit -F /tmp/p2-c1.txt && git push -u origin HEAD
```

---

### Task 2: Markers and the context header — emit new, accept both

**Files:**
- Modify: `server/sessions/completion.py`
- Modify: `server/sessions/model_request.py`
- Modify: `server/sessions/translator.py` (the `COMPLETION_MARKER in self._turn_text` check)
- Modify: `server/codex/translator.py` (the `COMPLETION_MARKER in text` check)
- Modify: `server/assistant/context.py` (`render_context_header`, `_HEADER_LINE`)
- Modify comments only: `server/sessions/claude_driver.py` (`<ccswitch-context>`, `<!-- ccswitch:model=… -->`, `<!-- ccswitch:no-pr -->` mentions), `server/sessions/replay.py`, `server/sessions/events.py`, `server/automations/completion_bar.py`, `server/db/models.py`
- Tests: `server/tests/test_completion.py`, `server/tests/test_completion_marker.py`, `server/tests/test_model_request_marker.py`, `server/tests/test_assistant_context.py`, `server/tests/test_driver_context_header.py`, new `server/tests/test_marker_dual_accept.py`

**Interfaces:**
- Consumes: `brand.MARKER_STEM`, `brand.LEGACY_MARKER_STEM`, `brand.CONTEXT_TAG`, `brand.LEGACY_CONTEXT_TAG` (Task 1).
- Produces: `completion.COMPLETION_MARKER == "<!-- viberpit:complete -->"`, `completion.NO_PR_MARKER == "<!-- viberpit:no-pr -->"`, `completion.LEGACY_COMPLETION_MARKER`, `completion.LEGACY_NO_PR_MARKER`, `completion.has_completion_marker(text: str | None) -> bool`, `completion.has_no_pr_marker(text: str | None) -> bool`; `model_request.MODEL_REQUEST_RE` matches both stems; `context.render_context_header` emits `<viberpit-context …/>`; `context.strip_context_header` strips either tag.

- [ ] **Step 1: Write the failing tests**

```python
# server/tests/test_marker_dual_accept.py
"""Rebrand Phase 2a: sessions are TOLD the viberpit markers, but every parser must keep
accepting the ccswitch forms forever — stored transcripts are re-parsed on replay and
in-flight sessions were prompted with the old text (spec §4 risk 7)."""
from ccswitch import brand
from server.assistant import context
from server.sessions import completion, model_request


def test_emitted_markers_use_the_new_stem():
    assert completion.COMPLETION_MARKER == f"<!-- {brand.MARKER_STEM}:complete -->"
    assert completion.NO_PR_MARKER == f"<!-- {brand.MARKER_STEM}:no-pr -->"
    assert completion.COMPLETION_MARKER in completion.COMPLETION_INSTRUCTION
    assert completion.NO_PR_MARKER in completion.NO_PR_INSTRUCTION
    assert f"<!-- {brand.MARKER_STEM}:model=<tier>" in model_request.MODEL_REQUEST_INSTRUCTION


def test_completion_marker_accepts_both_stems():
    for stem in (brand.MARKER_STEM, brand.LEGACY_MARKER_STEM):
        assert completion.has_completion_marker(f"done\n<!-- {stem}:complete -->")
        assert completion.has_no_pr_marker(f"nothing to ship\n<!-- {stem}:no-pr -->")
        assert completion.strip_markers(f"done\n<!-- {stem}:complete -->") == "done"
        assert completion.resolution_from_text(f"cancelled\n<!-- {stem}:no-pr -->") is not None
    assert not completion.has_completion_marker("<!-- other:complete -->")
    assert not completion.has_completion_marker(None)


def test_model_request_accepts_both_stems_and_strips_both():
    for stem in (brand.MARKER_STEM, brand.LEGACY_MARKER_STEM):
        got = model_request.parse_model_request(f'x\n<!-- {stem}:model=opus reason="r" -->')
        assert got == {"model": "opus", "reason": "r"}
        assert completion.strip_markers(f'x\n<!-- {stem}:model=opus reason="r" -->') == "x"


def test_context_header_emits_new_tag_and_strips_both():
    h = context.render_context_header({"route": "/sessions"}, host="dobby")
    assert h.startswith(f"<{brand.CONTEXT_TAG} ")
    for tag in (brand.CONTEXT_TAG, brand.LEGACY_CONTEXT_TAG):
        text, ctx = context.strip_context_header(f'<{tag} route="/sessions" />\nhi')
        assert text == "hi"
        assert ctx is not None and ctx["route"] == "/sessions"
```

Read `completion.strip_markers` / `resolution_from_text` / `render_context_header` / `strip_context_header` signatures before running; adjust the calls to the real ones (e.g. `render_context_header` may take a `PromptContext` — build one the way `test_assistant_context.py` does).

- [ ] **Step 2: Run to verify they fail**

Run: `PYTHONPATH=$PWD/src /home/tony/GitHub/ccswitch/.venv/bin/python -m pytest server/tests/test_marker_dual_accept.py -v`
Expected: FAIL (`has_completion_marker` missing; emitted markers still `ccswitch`).

- [ ] **Step 3: Implement**

`server/sessions/completion.py`:
```python
import re
from ccswitch import brand

COMPLETION_MARKER = f"<!-- {brand.MARKER_STEM}:complete -->"
LEGACY_COMPLETION_MARKER = f"<!-- {brand.LEGACY_MARKER_STEM}:complete -->"
NO_PR_MARKER = f"<!-- {brand.MARKER_STEM}:no-pr -->"
LEGACY_NO_PR_MARKER = f"<!-- {brand.LEGACY_MARKER_STEM}:no-pr -->"
_STEMS = f"(?:{re.escape(brand.MARKER_STEM)}|{re.escape(brand.LEGACY_MARKER_STEM)})"
COMPLETION_MARKER_RE = re.compile(rf"<!--\s*{_STEMS}:complete\s*-->")
NO_PR_MARKER_RE = re.compile(rf"<!--\s*{_STEMS}:no-pr\s*-->")


def has_completion_marker(text: str | None) -> bool:
    return bool(text) and COMPLETION_MARKER_RE.search(text) is not None


def has_no_pr_marker(text: str | None) -> bool:
    return bool(text) and NO_PR_MARKER_RE.search(text) is not None
```
The instructions keep interpolating `COMPLETION_MARKER` / `NO_PR_MARKER` (so the prompt teaches the new form). `strip_markers` becomes `COMPLETION_MARKER_RE.sub("", …)` then `NO_PR_MARKER_RE.sub("", …)` then the model-request strip. `resolution_from_text` uses `has_no_pr_marker(text)` in place of `NO_PR_MARKER not in text`.

`server/sessions/model_request.py`: build the regex from brand —
```python
_STEMS = f"(?:{re.escape(brand.MARKER_STEM)}|{re.escape(brand.LEGACY_MARKER_STEM)})"
MODEL_REQUEST_RE = re.compile(
    rf'<!--\s*{_STEMS}:model=([A-Za-z0-9._\[\]-]+)'
    r'(?:\s+reason="([^"]*)")?\s*-->'
)
```
and the instruction text uses `f'`<!-- {brand.MARKER_STEM}:model=<tier> reason="<short reason>" -->`'`.

`server/sessions/translator.py` and `server/codex/translator.py`: replace `COMPLETION_MARKER in <text>` with `has_completion_marker(<text>)` (import it beside `resolution_from_text`).

`server/assistant/context.py`: `return f"<{brand.CONTEXT_TAG} {' '.join(parts)} />"`; `_HEADER_LINE = re.compile(rf"\A<(?:{re.escape(brand.CONTEXT_TAG)}|{re.escape(brand.LEGACY_CONTEXT_TAG)}) ([^\n>]*?)\s*/>(?:\n|\Z)")`. The driver's dedupe compares rendered headers to `_last_context_header`, so a session resumed across the flip re-sends one header — acceptable (same cost as the compaction reset already documented there).

Comment-only updates: any `<ccswitch-context>`, `<!-- ccswitch:…` mention in the files listed above becomes the `viberpit` form (prose, not matched by the scan).

Existing tests: `test_completion.py` and `test_completion_marker.py` pin the literal — update to `<!-- viberpit:complete -->` / `<!-- viberpit:no-pr -->`. `test_model_request_marker.py`: keep every `ccswitch:model=` case (they now prove legacy acceptance) and change the instruction-text assertion to the new form. `test_assistant_context.py:80,89` (render) → `<viberpit-context`; `:54,78,115,127` (strip/injection) keep the legacy tag and add one new-tag twin each. `test_driver_context_header.py:53,66` → `<viberpit-context`.

- [ ] **Step 4: Run the covering suites**

Run: `PYTHONPATH=$PWD/src /home/tony/GitHub/ccswitch/.venv/bin/python -m pytest server/tests/test_marker_dual_accept.py server/tests/test_completion.py server/tests/test_completion_marker.py server/tests/test_completion_autoarchive.py server/tests/test_completion_bar.py server/tests/test_model_request_marker.py server/tests/test_model_request_pipeline.py server/tests/test_driver_model_request.py server/tests/test_assistant_context.py server/tests/test_driver_context_header.py server/tests/test_autopilot_context.py server/tests/test_replay.py server/tests/test_codex_translator*.py server/tests/test_translator*.py -v --timeout=180`
Expected: all PASS (fixtures that carry the legacy markers pass through the dual-accept).

- [ ] **Step 5: Commit and push**

```bash
git add server/sessions/completion.py server/sessions/model_request.py server/sessions/translator.py server/codex/translator.py server/assistant/context.py server/sessions/claude_driver.py server/sessions/replay.py server/sessions/events.py server/automations/completion_bar.py server/db/models.py server/tests/test_marker_dual_accept.py server/tests/test_completion.py server/tests/test_completion_marker.py server/tests/test_model_request_marker.py server/tests/test_assistant_context.py server/tests/test_driver_context_header.py
printf 'feat(brand): sessions are taught the viberpit markers; parsers accept both forever\n\ncompletion/no-pr/model markers and the <viberpit-context> header emit the new stem;\nhas_completion_marker/has_no_pr_marker, MODEL_REQUEST_RE and _HEADER_LINE accept the\nccswitch forms too, because transcripts are re-parsed on replay and in-flight sessions\nwere prompted with the old text (spec §4 risk 7).\n\nCo-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>\n' > /tmp/p2-c2.txt
git commit -F /tmp/p2-c2.txt && git push
```

---

### Task 3: MCP namespace — mount `viberpit`, match both prefixes

**Files:**
- Modify: `server/assistant/tools.py` (`annotation_for`)
- Modify: `server/sessions/permissions.py` (the app-tool branch)
- Modify: `server/sessions/claude_driver.py` (assistant hard-deny)
- Modify: `server/sessions/events.py` (`tool_kind`)
- Modify comments: `server/codex/driver.py`, `server/assistant/options_codex.py` (`mcp_name` docstring; the RST table row `mcp_servers={"ccswitch": server}` becomes `{"viberpit": server}`), `server/assistant/options.py` docstring, `web/src/session/events.ts:23`, `web/src/components/session/ToolCallCard.tsx:50`, `web/src/session/appActions.ts` header comment
- Modify: `web/src/session/appActions.ts` (`appActionTitle` uses `stripMcpPrefix`)
- Modify: `server/tests/test_brand_wire_contracts.py` (drift guard reads both prefixes; `_STRAY` unchanged — the legacy literal may now only live in `brand.py`/`brand.ts`)
- Tests: `server/tests/test_events_app_action.py`, `server/tests/test_assistant_permissions.py`, `server/tests/test_permissions.py`, `web/src/session/appActions.test.ts` — add a new-prefix twin beside each legacy-prefix case

**Interfaces:**
- Consumes: `brand.is_app_tool`, `brand.strip_mcp_prefix`, `brand.MCP_TOOL_PREFIX` (Task 1); web `stripMcpPrefix`.
- Produces: `tools.annotation_for` resolves either prefix; `events.tool_kind` returns `APP_ACTION` for either; the registry and hard-deny accept either; the SDK server mounts under `viberpit` (already via `brand.MCP_SERVER_NAME`, no edit needed in `server.py`/`options.py`).

- [ ] **Step 1: Write the failing tests**

In `server/tests/test_events_app_action.py` add:
```python
def test_tool_kind_is_app_action_for_both_prefixes():
    from ccswitch import brand
    from server.sessions.events import ToolKind, tool_kind
    for prefix in brand.MCP_TOOL_PREFIXES:
        assert tool_kind(prefix + "list_sessions") is ToolKind.APP_ACTION
    assert tool_kind("mcp__github__x") is ToolKind.MCP
```
In `server/tests/test_brand_wire_contracts.py`:
- `test_prefix_constants_are_one_value` stays; `test_web_brand_matches_python_brand` also reads `LEGACY_MCP_TOOL_PREFIX` from `brand.ts` and asserts it equals `brand.LEGACY_MCP_TOOL_PREFIX`;
- add:
```python
def test_annotation_resolves_both_prefixes():
    for prefix in brand.MCP_TOOL_PREFIXES:
        assert tools.annotation_for(prefix + "list_sessions") is not None
    assert tools.annotation_for("mcp__other__list_sessions") is None


def test_no_hand_typed_startswith_on_a_prefix():
    # Matchers must go through brand.is_app_tool / strip_mcp_prefix so both prefixes pass.
    offenders = []
    for path in (REPO_ROOT / "server").rglob("*.py"):
        if "tests" in path.parts:
            continue
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            if "startswith(brand.MCP_TOOL_PREFIX)" in line or "startswith(MCP_PREFIX)" in line:
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{lineno}")
    assert offenders == [], offenders
```
In `server/tests/test_assistant_permissions.py` and `test_permissions.py`: find one test that exercises the `mcp__ccswitch__` read auto-allow and one that exercises the destructive card; parametrize each over `brand.MCP_TOOL_PREFIXES` (keep the legacy case).
In `web/src/session/appActions.test.ts`: add a case that `appActionTitle('mcp__viberpit__archive_session')` equals the legacy-prefix result.

- [ ] **Step 2: Run to verify they fail**

Run: `PYTHONPATH=$PWD/src /home/tony/GitHub/ccswitch/.venv/bin/python -m pytest server/tests/test_brand_wire_contracts.py server/tests/test_events_app_action.py -v` and `(cd web && npm run test -- --run src/session/appActions.test.ts)`
Expected: the new tests FAIL (legacy prefix no longer resolves once `MCP_PREFIX` flipped; `startswith(brand.MCP_TOOL_PREFIX)` offenders listed).

- [ ] **Step 3: Implement**

`server/assistant/tools.py`:
```python
def annotation_for(mcp_name: str) -> ToolSpec | None:
    bare = brand.strip_mcp_prefix(mcp_name)
    return TOOL_SPECS.get(bare) if bare is not None else None
```
`server/sessions/permissions.py`: `if brand.is_app_tool(tool):` (comment: "either prefix — resumed Codex threads keep the legacy names").
`server/sessions/claude_driver.py`: `if is_assistant_kind(self.kind) and not brand.is_app_tool(tool_name):`.
`server/sessions/events.py`: `if brand.is_app_tool(name): return ToolKind.APP_ACTION`; keep `_APP_ACTION_PREFIX = brand.MCP_TOOL_PREFIX` exported (the contract test pins it).
`web/src/session/appActions.ts`:
```ts
export function appActionTitle(fullToolName: string): string | undefined {
  const bare = stripMcpPrefix(fullToolName)
  return bare === undefined ? undefined : APP_ACTION_TITLES[bare]
}
```
(import `stripMcpPrefix` from `@/brand`; drop the direct `MCP_TOOL_PREFIX` import if unused).
Comment updates as listed. `options_codex.mcp_name` already returns `MCP_PREFIX + tool` = the new prefix — correct (new Codex threads mount the new names; resumed ones keep old names and pass `is_app_tool`).

- [ ] **Step 4: Run the covering suites**

Run: `PYTHONPATH=$PWD/src /home/tony/GitHub/ccswitch/.venv/bin/python -m pytest server/tests/test_brand_wire_contracts.py server/tests/test_events_app_action.py server/tests/test_assistant_permissions.py server/tests/test_permissions.py server/tests/test_assistant_tools.py server/tests/test_assistant_reap.py server/tests/test_codex_assistant_driver.py server/tests/test_reassign_assistant_overlay.py server/tests/test_assistant_codex_overlay.py -v --timeout=180` and `(cd web && npm run typecheck && npm run lint && npm run test -- --run)`
Expected: all PASS; the full vitest suite count unchanged plus the new case.

- [ ] **Step 5: Commit and push**

```bash
git add server/assistant/tools.py server/sessions/permissions.py server/sessions/claude_driver.py server/sessions/events.py server/codex/driver.py server/assistant/options_codex.py server/assistant/options.py web/src/session/events.ts web/src/components/session/ToolCallCard.tsx web/src/session/appActions.ts web/src/session/appActions.test.ts server/tests/test_brand_wire_contracts.py server/tests/test_events_app_action.py server/tests/test_assistant_permissions.py server/tests/test_permissions.py
printf 'feat(brand): the assistant MCP server mounts as viberpit; matchers accept both prefixes\n\nmcp__viberpit__* is what new sessions see; the permission registry, the assistant\nhard-deny, tool_kind, annotation_for and the web presenter accept mcp__ccswitch__* too\n(spec §4 risk 2), because resumed Codex threads keep their old dynamicTools names.\n\nCo-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>\n' > /tmp/p2-c3.txt
git commit -F /tmp/p2-c3.txt && git push
```

---

### Task 4: Agent-facing prose says ViberPit (Phase 1c)

**Files:**
- Modify: `server/assistant/prompt.py` (`You are the ccswitch assistant…`, `# What ccswitch is`, `ccswitch rotates sessions…`, `ccswitch's CODING sessions`)
- Modify: `server/assistant/tools.py` (module docstring; descriptions at the `list_adoptable_sessions`, `adopt_session`, `upgrade_host` specs; the `ccswitch API error` / `ccswitch tool error` strings; `"source": "ccswitch-api"` — grep `ccswitch-api` across `server/ web/src docs/superpowers/evals`; if nothing parses it, flip to `f"{brand.SLUG}-api"`, otherwise leave it and say so in the report)
- Modify: `server/api/library.py` (`You are a ccswitch skill author.`)
- Modify: `server/assistant/options_codex.py` (prose in the module docstring and comments)

**Interfaces:**
- Consumes: `brand.NAME`, `brand.SLUG`.

- [ ] **Step 1: Baseline**

Run: `PYTHONPATH=$PWD/src /home/tony/GitHub/ccswitch/.venv/bin/python -m pytest server/tests/test_assistant_prompt*.py server/tests/test_assistant_tools.py server/tests/test_library*.py -q --timeout=120`
Expected: PASS (record count).

- [ ] **Step 2: Route the prose through `brand`**

`ASSISTANT_SYSTEM_PROMPT` becomes an f-string (or `.format`-free concatenation — check it has no `{}` braces first; if it does, use `"…" + brand.NAME + "…"` pieces) so every product mention is `{brand.NAME}`. Tool descriptions and error strings likewise. `library.py`: `f"You are a {brand.NAME} skill author. …"`.

- [ ] **Step 3: Grep gate**

Run: `git grep -nE '(^|[^_/.-])ccswitch([^_/.-]|$)' -- server/assistant server/api/library.py server/sessions/prompt_refiner.py server/automations/backlog_prompt.py server/sdlc/prompts.py ':!*/tests/*'`
Expected: only logger names (`ccswitch.dispatch` — Phase 5), the RST table / code identifiers, and `brand.py`-derived expressions.

- [ ] **Step 4: Run the suites**

Same command as Step 1 plus `server/tests/test_eval_assistant_grader.py`; expected PASS with the same count. Any test pinning the old prompt text is updated to `ViberPit`.

- [ ] **Step 5: Commit and push**

```bash
git add server/assistant/prompt.py server/assistant/tools.py server/api/library.py server/assistant/options_codex.py
printf 'feat(brand): the assistant and skill-author prompts say ViberPit (Phase 1c)\n\nCo-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>\n' > /tmp/p2-c4.txt
git commit -F /tmp/p2-c4.txt && git push
```

---

### Close-out

Full four-step Python gate (server suite in the background, `/tmp/rebrand-p2a-gate.log`), full web gate, `rm -rf web/dist`. Grep gate over `server src scripts web/src web/e2e` for `ccswitch:complete|ccswitch:no-pr|ccswitch:model|ccswitch-context|mcp__ccswitch__` — expected hits only in `brand.py`, `brand.ts`, and tests. In-flight PR check by file (`server/sessions/completion.py`, `permissions.py`, `claude_driver.py`, `server/assistant/tools.py`). PR body: emit-new/accept-both table, what stays legacy, test pins updated; `Refs #1349`. Ready + `gh pr merge --squash --auto`.

## Self-review

- **Spec coverage:** Phase 1c (prompts) → Task 4; Phase 2a markers → Task 2; MCP namespace with dual-accept → Tasks 1 and 3; §7 "keep forever" → the compat constants are first-class in brand, not a shim. Out of scope by spec: 2b (cookie/headers/localStorage), 2c (branch prefix, PM keys).
- **Placeholders:** none; each edit names its file and replacement.
- **Type consistency:** `has_completion_marker` / `has_no_pr_marker` (Task 2) used by both translators; `is_app_tool` / `strip_mcp_prefix` (Task 1) used in Task 3; web `stripMcpPrefix` (Task 1) used in Task 3; the contract test reads `LEGACY_MCP_TOOL_PREFIX` from both files.

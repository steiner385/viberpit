# Session-Recommended Model Switching Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a running `claude` session recommend its own model change via a marker in its
output; ccswitch auto-applies downgrades, parks upgrades for human approval, and lets the human
approve at a different model than the one requested.

**Architecture:** The session emits an HTML-comment marker (the same mechanism
`<!-- ccswitch:complete -->` already uses). The Translator parses it at `ResultMessage` onto a new
optional `TurnEnd.model_request` field; the driver latches it exactly as it latches
`pr_resolution`; the orchestrator consumes it post-turn, runs guards + policy, and either calls
the existing `set_model_for_session()` or parks it in the existing `pending_model` columns. The
override extends the existing `resolve_model_approval()` with an optional substitute model.

**Tech Stack:** Python 3.12 / FastAPI / SQLAlchemy(SQLite+Postgres) / Alembic · React + Vite +
TypeScript + Tailwind · pytest · vitest

**Spec:** `docs/superpowers/specs/2026-09-02-session-recommended-model-switching-design.md`

## Global Constraints

- **Governance:** `.specify/memory/constitution.md` v1.1.0. Principle VI (single source of truth
  / no dead code) is load-bearing here — Task 1 exists purely to satisfy it.
- **Never fork or patch the `claude` binary** (Principle I).
- **Fail-open invariant:** no failure in this feature may ever fail a turn, block a spawn, or
  interrupt a session. Every failure path logs at `warning` and drops the recommendation.
- **`block` is absolute.** No path in this feature — session-initiated or human-initiated — may
  apply a model whose tier policy resolves to `block`.
- **Never re-enter `set_model_for_session()` from an approval path.** It re-runs
  `resolve_model_policy`, which re-parks, producing an infinite approval loop. Approval paths use
  the low-level `driver.set_model()` / `_spawn_session(model=…)` calls they already use.
- **Schema:** new columns must be **nullable** so `_reconcile_columns` ALTER-ADDs them on both
  SQLite and Postgres. **No new models** — so `_REQUIRED_EXTRAS` (`test_postgres_compat.py`) and
  `RECONCILE_EXEMPT` (`test_schema_reconcile_coverage.py`) must NOT change.
- **The `lint-and-test` gate is four steps**, all required before pushing:
  ```
  ruff check src/ server/
  mypy server/ --ignore-missing-imports
  pytest tests/
  pytest server/tests/
  ```

### Environment setup (do this once, before Task 1)

A fresh worktree has no built env; network blocks `uv sync`. Use the main checkout's prebuilt
environment and symlink web deps:

```bash
ln -sfn /home/tony/GitHub/ccswitch/web/node_modules web/node_modules
```

Throughout this plan `PY` means:

```bash
PY="PYTHONPATH=$PWD /home/tony/GitHub/ccswitch/.venv/bin/python"
```

**Two gotchas that will otherwise cost you an hour:**
- API tests 401 unless the tailnet allowlist is empty AND dev mode is on. Prefix API test runs
  with `CCSWITCH_TAILNET_ALLOWLIST= CCSWITCH_DEV=1` (assign empty; do **not** use `env -u`).
- Never `cd` into `web/` — use `npm --prefix web …` from the repo root.

---

### Task 1: Consolidate the model-tier ordering (server + web)

The direction rule needs a cheapest-first tier ordering. One already exists in the wrong place,
and the tier list is duplicated four times. Establish one ordered source of truth and delete the
copies **before** any new code depends on them.

**Files:**
- Modify: `server/sessions/model_catalog.py:41` (add `TIER_ORDER`, `tier_rank`; derive `_TIER_ALIASES`)
- Modify: `server/automations/model_pick.py:31` (`AUTO_MODEL_POOL` derives from `TIER_ORDER`)
- Modify: `web/src/session/modelName.ts:11` (export `TIERS`)
- Modify: `web/src/components/session/PendingModelApprovalPanel.tsx:17-28` (drop local `TIER_LABELS`)
- Test: `server/tests/test_model_tier_order.py` (new)

**Interfaces:**
- Consumes: nothing (first task).
- Produces:
  - `server.sessions.model_catalog.TIER_ORDER: list[str]` — `["haiku", "sonnet", "opus", "fable"]`, cheapest first.
  - `server.sessions.model_catalog.tier_rank(model: str | None) -> int | None` — index into `TIER_ORDER`, `None` for untiered models (`None`, `""`, `"default"`, `"best"`, unknown ids).
  - `web/src/session/modelName.ts` exports `TIERS: Record<string, string>` and `tierLabel(model: string | null | undefined): string`.

- [ ] **Step 1: Write the failing test**

Create `server/tests/test_model_tier_order.py`:

```python
"""TIER_ORDER is the single ordered source of truth for model tiers."""
import re
from pathlib import Path

from server.automations.model_pick import AUTO_MODEL_POOL
from server.sessions.model_catalog import TIER_ORDER, model_tier, tier_rank


def test_tier_order_is_cheapest_first():
    assert TIER_ORDER == ["haiku", "sonnet", "opus", "fable"]


def test_tier_rank_orders_aliases():
    assert tier_rank("haiku") < tier_rank("sonnet") < tier_rank("opus") < tier_rank("fable")


def test_tier_rank_handles_pinned_ids_and_context_tags():
    assert tier_rank("claude-fable-5-1") == tier_rank("fable") == tier_rank("fable[1m]")
    assert tier_rank("opusplan") == tier_rank("opus")


def test_tier_rank_is_none_for_untiered_models():
    for m in (None, "", "default", "best", "gpt-4"):
        assert tier_rank(m) is None, m


def test_model_tier_still_agrees_with_tier_order():
    for tier in TIER_ORDER:
        assert model_tier(tier) == tier


def test_auto_model_pool_derives_from_tier_order():
    """model_pick must not redeclare the ordering (constitution VI)."""
    assert AUTO_MODEL_POOL == TIER_ORDER


def test_web_tiers_match_server_tier_order():
    """web/src/session/modelName.ts TIERS is the web mirror — keys must agree."""
    ts = (Path(__file__).resolve().parents[2] / "web/src/session/modelName.ts").read_text()
    m = re.search(r"export const TIERS[^=]*=\s*\{([^}]*)\}", ts)
    assert m, "exported TIERS object not found in modelName.ts"
    keys = set(re.findall(r"(\w+)\s*:", m.group(1)))
    assert keys == set(TIER_ORDER)


def test_panel_has_no_private_tier_label_copy():
    """PendingModelApprovalPanel must use the shared TIERS, not a local copy."""
    panel = (Path(__file__).resolve().parents[2]
             / "web/src/components/session/PendingModelApprovalPanel.tsx").read_text()
    assert "TIER_LABELS" not in panel
```

- [ ] **Step 2: Run test to verify it fails**

```bash
PYTHONPATH=$PWD /home/tony/GitHub/ccswitch/.venv/bin/python -m pytest \
  server/tests/test_model_tier_order.py -v
```
Expected: FAIL — `ImportError: cannot import name 'TIER_ORDER'`.

- [ ] **Step 3: Add `TIER_ORDER` and `tier_rank` to the catalog**

In `server/sessions/model_catalog.py`, replace the `_TIER_ALIASES` block (currently around
line 175, above `model_tier`) with:

```python
# Canonical model tiers, CHEAPEST FIRST. The single ordered source of truth: the
# model-approval policy matches against it, the self-directed switch rule compares
# ranks with it, and automations/model_pick.AUTO_MODEL_POOL derives from it.
# Server mirror of web/src/session/modelName.ts TIERS (constitution VI: one tier
# definition, not four).
TIER_ORDER: list[str] = ["haiku", "sonnet", "opus", "fable"]

_TIER_ALIASES: frozenset[str] = frozenset(TIER_ORDER)
```

Then append after `model_tier`:

```python
def tier_rank(model: str | None) -> int | None:
    """Cost rank of *model* within TIER_ORDER (0 = cheapest), or None when the
    model has no fixed tier (None / "" / "default" / "best" / unknown ids).

    A None rank means no honest cheaper/costlier comparison exists — callers must
    treat it as indeterminate rather than defaulting a direction.
    """
    tier = model_tier(model)
    return TIER_ORDER.index(tier) if tier is not None else None
```

- [ ] **Step 4: Point `AUTO_MODEL_POOL` at it**

In `server/automations/model_pick.py`, change the import on line 26 and the constant on line 31:

```python
from server.sessions.model_catalog import ALIASES, TIER_ORDER
```

```python
# Tiers "auto" chooses between, cheapest first. Derived from the model-catalog
# ordering so there is exactly one tier ranking in the codebase.
AUTO_MODEL_POOL: list[str] = TIER_ORDER
```

- [ ] **Step 5: Export `TIERS` and add `tierLabel` on the web side**

In `web/src/session/modelName.ts`, change line 11 and add a helper below `modelTier`:

```ts
export const TIERS: Record<string, string> = { opus: 'Opus', sonnet: 'Sonnet', haiku: 'Haiku', fable: 'Fable' }
```

```ts
/**
 * Display label for a model alias or pinned id: the full "Opus 5" form when the id
 * matches, else the tier's capitalised name ("fable" -> "Fable"), else verbatim.
 * Single source of truth for model labels in the UI.
 */
export function tierLabel(id: string | null | undefined): string {
  if (!id) return 'this model'
  const full = modelName(id)
  if (full && full !== id) return full
  return TIERS[id.toLowerCase()] ?? id
}
```

- [ ] **Step 6: Delete the panel's private copy**

In `web/src/components/session/PendingModelApprovalPanel.tsx`, delete the `TIER_LABELS` constant
and the `displayLabel` function (lines 14-28) and replace the import on line 12 with:

```ts
import { tierLabel } from '@/session/modelName'
```

Then in the component body replace `const label = displayLabel(model)` with:

```ts
const label = tierLabel(model)
```

- [ ] **Step 7: Run tests to verify they pass**

```bash
PYTHONPATH=$PWD /home/tony/GitHub/ccswitch/.venv/bin/python -m pytest \
  server/tests/test_model_tier_order.py server/tests/test_model_pick.py \
  server/tests/test_model_catalog.py -v
npm --prefix web run test -- modelName
npm --prefix web exec tsc -- --noEmit
```
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add server/sessions/model_catalog.py server/automations/model_pick.py \
        web/src/session/modelName.ts web/src/components/session/PendingModelApprovalPanel.tsx \
        server/tests/test_model_tier_order.py
git commit -m "refactor(models): one ordered tier source of truth (TIER_ORDER)"
```

---

### Task 2: The model-request marker contract

Add the marker literal, the system-prompt instruction, and the parser in one module, so the
prompt half and the scan half cannot drift — the same guarantee `completion.py` provides.

**Files:**
- Create: `server/sessions/model_request.py`
- Modify: `server/sessions/completion.py:41-48` (`strip_markers` also strips this marker)
- Modify: `server/sessions/sdk_factory.py:11-12` (compose the instruction)
- Modify: `server/config.py:130` (add the kill switch)
- Test: `server/tests/test_model_request_marker.py` (new)

**Interfaces:**
- Consumes: nothing from Task 1.
- Produces:
  - `server.sessions.model_request.MODEL_REQUEST_INSTRUCTION: str`
  - `server.sessions.model_request.MODEL_REQUEST_RE: re.Pattern`
  - `server.sessions.model_request.parse_model_request(text: str) -> dict | None` — returns `{"model": str, "reason": str | None}` for the LAST marker in *text*, else `None`.
  - `server.config` setting `self_model_switch_enabled: bool` (`CCSWITCH_SELF_MODEL_SWITCH_ENABLED`, default `True`).

- [ ] **Step 1: Write the failing test**

Create `server/tests/test_model_request_marker.py`:

```python
"""The <!-- ccswitch:model=... --> contract: parse, strip, and prompt injection."""
from server.sessions.completion import COMPLETION_MARKER, strip_markers
from server.sessions.model_request import (
    MODEL_REQUEST_INSTRUCTION,
    parse_model_request,
)


def test_parses_alias_and_reason():
    text = 'Done.\n<!-- ccswitch:model=fable reason="cross-module refactor" -->'
    assert parse_model_request(text) == {"model": "fable", "reason": "cross-module refactor"}


def test_parses_without_reason():
    assert parse_model_request("<!-- ccswitch:model=haiku -->") == {
        "model": "haiku", "reason": None}


def test_parses_pinned_id_and_context_tag():
    assert parse_model_request("<!-- ccswitch:model=claude-opus-5 -->")["model"] == "claude-opus-5"
    assert parse_model_request("<!-- ccswitch:model=fable[1m] -->")["model"] == "fable[1m]"


def test_last_marker_wins():
    text = "<!-- ccswitch:model=haiku -->\nthinking again\n<!-- ccswitch:model=opus -->"
    assert parse_model_request(text)["model"] == "opus"


def test_reason_may_contain_punctuation_but_not_break_out():
    text = '<!-- ccswitch:model=opus reason="it is tricky: races, locks (ugh)" -->'
    assert parse_model_request(text)["reason"] == "it is tricky: races, locks (ugh)"


def test_absent_or_malformed_returns_none():
    for text in ("", "no marker here", "<!-- ccswitch:model= -->",
                 "<!-- ccswitch:model -->", "<!-- ccswitch:complete -->"):
        assert parse_model_request(text) is None, text


def test_strip_markers_removes_the_model_marker():
    """Load-bearing: an unstripped marker would defeat the trailing-question heuristic."""
    text = 'All set?\n<!-- ccswitch:model=opus reason="x" -->'
    assert strip_markers(text) == "All set?"


def test_strip_markers_still_removes_completion_marker():
    assert strip_markers(f"done\n{COMPLETION_MARKER}") == "done"


def test_instruction_names_every_tier():
    for tier in ("haiku", "sonnet", "opus", "fable"):
        assert tier in MODEL_REQUEST_INSTRUCTION


def test_sdk_factory_appends_the_instruction():
    from server.sessions.sdk_factory import _build_append_string
    assert MODEL_REQUEST_INSTRUCTION in _build_append_string()
```

- [ ] **Step 2: Run test to verify it fails**

```bash
PYTHONPATH=$PWD /home/tony/GitHub/ccswitch/.venv/bin/python -m pytest \
  server/tests/test_model_request_marker.py -v
```
Expected: FAIL — `ModuleNotFoundError: No module named 'server.sessions.model_request'`.

- [ ] **Step 3: Create the contract module**

Create `server/sessions/model_request.py`:

```python
"""Single source of truth for the session-initiated model-request marker.

The same literal is appended to every session's system prompt (so the agent knows
how to ask for a different model) and scanned for by the Translator (so ccswitch
can act on it). Keeping both in one module prevents drift — the same guarantee
``completion.py`` provides for the completion marker.

The tier rubric below is deliberately identical to
``server.automations.model_pick._ASSESS_SYSTEM`` so a session's self-assessment and
the spawn-time complexity assessor speak one vocabulary rather than two.
"""
from __future__ import annotations

import re

# Matches: <!-- ccswitch:model=<alias-or-id> [reason="..."] -->
# The model token excludes whitespace and '-->' so a malformed marker cannot swallow
# the rest of the message; the reason is a non-greedy run inside double quotes.
MODEL_REQUEST_RE = re.compile(
    r'<!--\s*ccswitch:model=([A-Za-z0-9._\[\]-]+)'
    r'(?:\s+reason="([^"]*)")?\s*-->'
)

MODEL_REQUEST_INSTRUCTION = (
    "If partway through this session you determine the work needs a different model "
    "tier than the one you are running on, end a message with "
    '`<!-- ccswitch:model=<tier> reason="<short reason>" -->` on its own line. Tiers, '
    "cheapest first:\n"
    "- haiku: trivial/mechanical work — typo or copy fixes, config bumps, single-line "
    "changes, dependency pins.\n"
    "- sonnet: a typical single-concern change — one feature or bugfix with its tests, "
    "limited blast radius.\n"
    "- opus: hard multi-file work — cross-module changes, tricky debugging, CI failures "
    "with unclear cause, merge-conflict resolution.\n"
    "- fable: the hardest architectural or cross-cutting work — large refactors, subtle "
    "concurrency/data-integrity issues, multi-PR consolidation.\n"
    "Ask only when the work has turned out materially harder or easier than it looked — "
    "never routinely, and never more than once for the same conclusion. A request for a "
    "cheaper tier takes effect on your next turn; a request for a more expensive tier is "
    "held for a human, who may approve it at a different tier than you asked for. Keep "
    "working either way — never wait for the switch."
)


def parse_model_request(text: str) -> dict | None:
    """Extract the LAST model request from *text*.

    Returns ``{"model": str, "reason": str | None}``, or ``None`` when *text* carries
    no well-formed marker. Last-wins because a session that reconsiders within one
    turn means the final word.
    """
    if not text:
        return None
    matches = MODEL_REQUEST_RE.findall(text)
    if not matches:
        return None
    model, reason = matches[-1]
    if not model:
        return None
    return {"model": model, "reason": reason or None}
```

- [ ] **Step 4: Extend `strip_markers`**

In `server/sessions/completion.py`, replace the body of `strip_markers` (line 47-48) with:

```python
    from .model_request import MODEL_REQUEST_RE
    text = text.replace(COMPLETION_MARKER, "").replace(NO_PR_MARKER, "")
    return MODEL_REQUEST_RE.sub("", text).rstrip()
```

and update its docstring's first line to read "Remove the completion / no-PR / model-request
marker from *text* …".

- [ ] **Step 5: Add the kill switch to config**

In `server/config.py`, immediately after `model_approval_denylist` (line 130), add:

```python
        # Self-directed model switching: whether sessions are told they may request a
        # model change (and whether a marker found in output is acted on at all).
        self_model_switch_enabled: bool = Field(
            default=True, alias="CCSWITCH_SELF_MODEL_SWITCH_ENABLED")
```

- [ ] **Step 6: Wire the instruction into the system prompt**

In `server/sessions/sdk_factory.py`, replace `_build_append_string` (lines 11-12):

```python
def _build_append_string() -> str:
    """Compose every ccswitch marker instruction into the system-prompt append.

    Single composition point: each marker contract owns its own literal, this owns
    the order they reach the agent in.
    """
    from server.config import get_settings
    from .model_request import MODEL_REQUEST_INSTRUCTION
    parts = [COMPLETION_INSTRUCTION, NO_PR_INSTRUCTION]
    if get_settings().self_model_switch_enabled:
        parts.append(MODEL_REQUEST_INSTRUCTION)
    return "\n\n".join(parts)
```

- [ ] **Step 7: Run tests to verify they pass**

```bash
PYTHONPATH=$PWD /home/tony/GitHub/ccswitch/.venv/bin/python -m pytest \
  server/tests/test_model_request_marker.py server/tests/test_completion.py -v
```
Expected: PASS. If `test_completion.py` does not exist, run
`pytest server/tests/ -k "completion or marker" -v` instead.

- [ ] **Step 8: Commit**

```bash
git add server/sessions/model_request.py server/sessions/completion.py \
        server/sessions/sdk_factory.py server/config.py \
        server/tests/test_model_request_marker.py
git commit -m "feat(models): add the ccswitch:model request marker contract"
```

---

### Task 3: Carry the request from Translator to driver

Ride the existing turn-end side-channel: one optional field on `TurnEnd`, latched by the driver
exactly as `pr_resolution` is.

**Files:**
- Modify: `server/sessions/events.py:48-55` (`TurnEnd.model_request`)
- Modify: `server/sessions/translator.py:147-170` (parse at `ResultMessage`)
- Modify: `server/sessions/driver.py:186`, `:710-716`, `:832-838` (latch + property)
- Test: `server/tests/test_model_request_pipeline.py` (new)

**Interfaces:**
- Consumes: `parse_model_request` (Task 2).
- Produces:
  - `events.TurnEnd(..., model_request: dict | None = None)`; wire key `"modelRequest"`.
  - `SessionDriver.last_model_request -> dict | None` — `{"turn_id": str, "model": str, "reason": str | None}` or `None`.

- [ ] **Step 1: Write the failing test**

Create `server/tests/test_model_request_pipeline.py`:

```python
"""Translator parses the marker onto TurnEnd; the driver latches it."""
from server.sessions import events as ev


def test_turn_end_carries_model_request_on_the_wire():
    e = ev.TurnEnd("t1", model_request={"model": "opus", "reason": "hard"})
    assert e.to_wire()["modelRequest"] == {"model": "opus", "reason": "hard"}


def test_turn_end_model_request_defaults_to_none():
    assert ev.TurnEnd("t1").to_wire()["modelRequest"] is None


def test_translator_extracts_model_request_from_turn_text():
    from server.sessions.translator import Translator
    tr = Translator()                      # __init__ takes no args
    tr._turn_text = 'work done\n<!-- ccswitch:model=haiku reason="trivial" -->'
    out = tr._turn_end_model_request()
    assert out == {"model": "haiku", "reason": "trivial"}


def test_translator_returns_none_without_a_marker():
    from server.sessions.translator import Translator
    tr = Translator()                      # __init__ takes no args
    tr._turn_text = "just prose"
    assert tr._turn_end_model_request() is None


def test_driver_latches_the_request_with_its_turn_id():
    from server.sessions.driver import SessionDriver
    d = SessionDriver.__new__(SessionDriver)          # no __init__: pure latch test
    d._last_model_request = None
    e = ev.TurnEnd("turn-7", model_request={"model": "fable", "reason": "refactor"})
    SessionDriver._latch_model_request(d, e)
    assert d.last_model_request == {
        "turn_id": "turn-7", "model": "fable", "reason": "refactor"}


def test_driver_latch_ignores_a_turn_without_a_request():
    from server.sessions.driver import SessionDriver
    d = SessionDriver.__new__(SessionDriver)
    d._last_model_request = None
    SessionDriver._latch_model_request(d, ev.TurnEnd("turn-8"))
    assert d.last_model_request is None
```

- [ ] **Step 2: Run test to verify it fails**

```bash
PYTHONPATH=$PWD /home/tony/GitHub/ccswitch/.venv/bin/python -m pytest \
  server/tests/test_model_request_pipeline.py -v
```
Expected: FAIL — `TypeError: TurnEnd.__init__() got an unexpected keyword argument 'model_request'`.

- [ ] **Step 3: Add the field to `TurnEnd`**

In `server/sessions/events.py`, replace the `TurnEnd` dataclass body (lines 48-55):

```python
class TurnEnd:
    turn_id: str; usage: dict | None = None; cost_usd: float | None = None
    src_uuid: str | None = None; parent_tool_use_id: str | None = None; outcome: str = "success"
    pr_resolution: str | None = None
    # Self-directed model request parsed from this turn's text, or None.
    # {"model": str, "reason": str | None}
    model_request: dict | None = None
    def to_wire(self): return {"type": "turnEnd", "turnId": self.turn_id, "usage": self.usage,
                               "costUsd": self.cost_usd, "srcUuid": self.src_uuid,
                               "parentToolUseId": self.parent_tool_use_id, "outcome": self.outcome,
                               "prResolution": self.pr_resolution,
                               "modelRequest": self.model_request}
```

- [ ] **Step 4: Parse it in the Translator**

In `server/sessions/translator.py`, add the import at the top (beside the line 7 import):

```python
from .model_request import parse_model_request
```

Add this method to the `Translator` class (place it directly above the `translate` method):

```python
    def _turn_end_model_request(self) -> dict | None:
        """Self-directed model request carried by this turn's text, or None.

        Isolated from the ResultMessage branch so it is unit-testable without
        constructing a whole SDK message.
        """
        return parse_model_request(self._turn_text)
```

In the `ResultMessage` branch, capture it **before** `self._reset()` clears `_turn_text`.
Change the line `pr_resolution = resolution_from_text(self._turn_text)` to:

```python
            pr_resolution = resolution_from_text(self._turn_text)
            model_request = self._turn_end_model_request()
```

and add the kwarg to the `TurnEnd` construction:

```python
            out.append(ev.TurnEnd(tid, _usage_dict(msg.usage),
                                  getattr(msg, "total_cost_usd", None), getattr(msg, "uuid", None),
                                  outcome=outcome, pr_resolution=pr_resolution,
                                  model_request=model_request))
```

- [ ] **Step 5: Latch it in the driver**

In `server/sessions/driver.py`, add beside `self._last_pr_resolution` (line 186):

```python
        self._last_model_request: dict | None = None
```

Add the latch helper next to the other private helpers (place it directly above the
`last_pr_resolution` property, around line 832):

```python
    def _latch_model_request(self, e) -> None:
        """Latch a self-directed model request from a completed turn.

        Stamped with the turn id so the orchestrator can consume each request exactly
        once. Set-only: a turn carrying no request leaves the previous value alone,
        matching _last_pr_resolution's stickiness.
        """
        if e.model_request:
            self._last_model_request = {"turn_id": e.turn_id, **e.model_request}

    @property
    def last_model_request(self) -> dict | None:
        """The most recent ``<!-- ccswitch:model=… -->`` request, or None.
        ``{"turn_id", "model", "reason"}``."""
        return self._last_model_request
```

Call it from the `TurnEnd` branch of the drain loop (after line 716, inside
`elif isinstance(e, ev.TurnEnd):`):

```python
                        self._latch_model_request(e)
```

- [ ] **Step 6: Run tests to verify they pass**

```bash
PYTHONPATH=$PWD /home/tony/GitHub/ccswitch/.venv/bin/python -m pytest \
  server/tests/test_model_request_pipeline.py server/tests/test_events.py \
  server/tests/test_translator.py server/tests/test_driver.py -v
```
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add server/sessions/events.py server/sessions/translator.py server/sessions/driver.py \
        server/tests/test_model_request_pipeline.py
git commit -m "feat(models): carry session model requests on TurnEnd"
```

---

### Task 4: Persist the request's reason

One nullable column so the approval card can show *why* the session asked.

**Files:**
- Modify: `server/db/models.py:142` (add `pending_model_reason`)
- Modify: `server/sessions/types.py:67` (add to `SessionRecord`, `to_dict`, `from_dict`)
- Create: `server/db/migrations/versions/0014_pending_model_reason.py`
- Modify: `server/api/sessions.py:185` (surface `pendingModelReason`)
- Modify: `web/src/api/types.ts` (add `pendingModelReason` to `Session`)
- Test: `server/tests/test_pending_model_reason_column.py` (new)

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `DbSession.pending_model_reason: str | None`
  - `SessionRecord.pending_model_reason: str | None` (serialised as `pendingModelReason`)
  - API session payload key `pendingModelReason`.

- [ ] **Step 1: Write the failing test**

Create `server/tests/test_pending_model_reason_column.py`:

```python
"""pending_model_reason: nullable column, record round-trip, API surface."""
from server.db.models import Session as DbSession
from server.sessions.types import SessionRecord


def test_column_exists_and_is_nullable():
    col = DbSession.__table__.columns["pending_model_reason"]
    assert col.nullable is True


def test_record_round_trips_the_reason():
    rec = SessionRecord(id="s1", pending_model="fable",
                        pending_model_reason="cross-module refactor")
    revived = SessionRecord.from_dict(rec.to_dict())
    assert revived.pending_model_reason == "cross-module refactor"


def test_record_defaults_to_none():
    assert SessionRecord(id="s1").pending_model_reason is None


def test_migration_chains_off_the_current_head():
    """Read the file rather than importing it — the module name starts with a digit."""
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "db/migrations/versions"
           / "0014_pending_model_reason.py").read_text()
    assert 'revision = "0014_pending_model_reason"' in src
    assert 'down_revision = "0013_host_upgrade_ssh_config"' in src
```

- [ ] **Step 2: Run test to verify it fails**

```bash
PYTHONPATH=$PWD /home/tony/GitHub/ccswitch/.venv/bin/python -m pytest \
  server/tests/test_pending_model_reason_column.py -v
```
Expected: FAIL — `KeyError: 'pending_model_reason'`.

- [ ] **Step 3: Add the column**

In `server/db/models.py`, after line 142 (`pending_model_requested_at`):

```python
    # Why the requester wants this model — free text from a session's own
    # <!-- ccswitch:model=… reason="…" --> marker. Shown on the approval card.
    pending_model_reason: Mapped[str | None] = mapped_column(String, nullable=True, default=None)
```

- [ ] **Step 4: Add it to `SessionRecord`**

In `server/sessions/types.py`, after `pending_model` (line 67):

```python
    pending_model_reason: str | None = None
```

In `to_dict` (beside line 156):

```python
            "pendingModelReason": self.pending_model_reason,
```

In `from_dict` (beside line 195):

```python
            pending_model_reason=d.get("pendingModelReason"),
```

- [ ] **Step 5: Add the Alembic revision**

Create `server/db/migrations/versions/0014_pending_model_reason.py`:

```python
"""self-directed model switching — sessions.pending_model_reason

Revision ID: 0014_pending_model_reason
Revises: 0013_host_upgrade_ssh_config
Create Date: 2026-09-02

Reviewable record only: nothing runs `alembic upgrade` at deploy time — init_db's
_reconcile_columns ALTER-ADDs this nullable column on both SQLite and Postgres at
next boot. Nullable, so no default-synthesis / drift issue.
"""
from alembic import op
import sqlalchemy as sa

revision = "0014_pending_model_reason"
down_revision = "0013_host_upgrade_ssh_config"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("sessions", sa.Column("pending_model_reason", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("sessions", "pending_model_reason")
```

- [ ] **Step 6: Surface it on the API and in the web types**

In `server/api/sessions.py`, after the `"pendingModel"` entry (line 185):

```python
        # Why the session asked for pendingModel (its own stated reason), or null.
        "pendingModelReason": getattr(record, "pending_model_reason", None),
```

In `web/src/api/types.ts`, in the `Session` interface beside `pendingModel`:

```ts
  /** The requester's stated reason for pendingModel — a session's own words, or null. */
  pendingModelReason?: string | null
```

- [ ] **Step 7: Run tests to verify they pass**

```bash
PYTHONPATH=$PWD /home/tony/GitHub/ccswitch/.venv/bin/python -m pytest \
  server/tests/test_pending_model_reason_column.py \
  server/tests/test_schema_reconcile_coverage.py server/tests/test_postgres_compat.py -v
npm --prefix web exec tsc -- --noEmit
```
Expected: PASS. `_REQUIRED_EXTRAS` and `RECONCILE_EXEMPT` must NOT have changed — this is a
nullable column on an existing model.

- [ ] **Step 8: Commit**

```bash
git add server/db/models.py server/sessions/types.py server/api/sessions.py \
        server/db/migrations/versions/0014_pending_model_reason.py \
        web/src/api/types.ts server/tests/test_pending_model_reason_column.py
git commit -m "feat(models): persist pending_model_reason"
```

---

### Task 5: The direction rule, composed with the existing policy

Add the self-directed decision to `model_policy.py` — the existing policy source of truth — so
there is one place that answers "may this model be applied?".

**Files:**
- Modify: `server/sessions/model_policy.py` (add `resolve_self_directed_model_policy`)
- Test: `server/tests/test_self_directed_model_policy.py` (new)

**Interfaces:**
- Consumes: `tier_rank` (Task 1), `resolve_model_policy` (existing).
- Produces:
  - `resolve_self_directed_model_policy(db, *, current_model, requested_model, account=None) -> str`
    returning `"apply"` | `"park"` | `"refuse"` | `"noop"`.

- [ ] **Step 1: Write the failing test**

Create `server/tests/test_self_directed_model_policy.py`:

```python
"""Direction rule composed with the tier denylist. Strictest outcome wins."""
import pytest

from server.sessions import model_policy


@pytest.fixture
def allow_all(monkeypatch):
    """Force the tier policy to 'allow' so direction is the only variable."""
    async def _allow(db, *, model=None, account=None):
        return "allow"
    monkeypatch.setattr(model_policy, "resolve_model_policy", _allow)


async def _decide(current, requested):
    return await model_policy.resolve_self_directed_model_policy(
        None, current_model=current, requested_model=requested)


@pytest.mark.asyncio
async def test_downgrade_auto_applies(allow_all):
    assert await _decide("opus", "haiku") == "apply"


@pytest.mark.asyncio
async def test_upgrade_parks_for_approval(allow_all):
    assert await _decide("sonnet", "fable") == "park"


@pytest.mark.asyncio
async def test_same_tier_is_a_no_op(allow_all):
    assert await _decide("opus", "opusplan") == "noop"


@pytest.mark.asyncio
@pytest.mark.parametrize("current,requested", [
    ("default", "haiku"), ("best", "haiku"), ("opus", "best"), (None, "sonnet"),
])
async def test_indeterminate_direction_parks(allow_all, current, requested):
    """Either side untiered means no honest comparison — never guess a direction."""
    assert await _decide(current, requested) == "park"


@pytest.mark.asyncio
async def test_blocked_tier_is_refused_even_as_a_downgrade(monkeypatch):
    async def _block(db, *, model=None, account=None):
        return "block"
    monkeypatch.setattr(model_policy, "resolve_model_policy", _block)
    assert await _decide("fable", "haiku") == "refuse"


@pytest.mark.asyncio
async def test_gated_tier_parks_even_as_a_downgrade(monkeypatch):
    async def _gated(db, *, model=None, account=None):
        return "require_approval"
    monkeypatch.setattr(model_policy, "resolve_model_policy", _gated)
    assert await _decide("fable", "opus") == "park"
```

- [ ] **Step 2: Run test to verify it fails**

```bash
PYTHONPATH=$PWD /home/tony/GitHub/ccswitch/.venv/bin/python -m pytest \
  server/tests/test_self_directed_model_policy.py -v
```
Expected: FAIL — `AttributeError: module ... has no attribute 'resolve_self_directed_model_policy'`.

- [ ] **Step 3: Implement the composition**

Append to `server/sessions/model_policy.py`:

```python
async def resolve_self_directed_model_policy(
    db, *, current_model: str | None, requested_model: str | None,
    account: str | None = None,
) -> str:
    """Decide what to do with a model the SESSION asked for itself.

    Returns "apply" | "park" | "refuse" | "noop".

    Composes two independent gates and takes the STRICTER outcome:

    1. The existing tier policy (``resolve_model_policy``) — block/require_approval
       apply to self-directed requests exactly as they apply to human ones. A blocked
       tier stays blocked even when the session is asking to move *down* to it.
    2. The direction rule — a cheaper tier auto-applies; a costlier one is parked for
       a human, because cost/quota risk is the axis worth a human's attention.

    Indeterminate direction (either side untiered: None/"default"/"best"/unknown id)
    parks. There is no honest cheaper/costlier comparison, and guessing one is how a
    session talks itself onto an expensive model.
    """
    from server.sessions.model_catalog import tier_rank

    tier_policy = await resolve_model_policy(db, model=requested_model, account=account)
    if tier_policy == "block":
        return "refuse"
    if tier_policy == "require_approval":
        return "park"

    current_rank = tier_rank(current_model)
    requested_rank = tier_rank(requested_model)
    if current_rank is None or requested_rank is None:
        return "park"
    if requested_rank == current_rank:
        return "noop"
    return "apply" if requested_rank < current_rank else "park"
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
PYTHONPATH=$PWD /home/tony/GitHub/ccswitch/.venv/bin/python -m pytest \
  server/tests/test_self_directed_model_policy.py server/tests/test_model_policy.py -v
```
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add server/sessions/model_policy.py server/tests/test_self_directed_model_policy.py
git commit -m "feat(models): direction rule for self-directed model requests"
```

---

### Task 6: Guards against a session driving itself in circles

Pure, orchestrator-free logic so the loop-prevention rules are testable on their own.

**Files:**
- Create: `server/sessions/self_model_guards.py`
- Modify: `server/config.py` (add the cap)
- Test: `server/tests/test_self_model_guards.py` (new)

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `SelfModelState` dataclass: `auto_applied: int = 0`, `tiers_left: set[str]`, `amended_away: set[str]`.
  - `evaluate_guards(state: SelfModelState, *, current_tier: str | None, requested_tier: str | None, cap: int) -> str` returning `"drop"` | `"auto_ok"` | `"force_park"`.
  - `server.config` setting `self_model_switch_cap: int` (`CCSWITCH_SELF_MODEL_SWITCH_CAP`, default `3`).

- [ ] **Step 1: Write the failing test**

Create `server/tests/test_self_model_guards.py`:

```python
"""Guards: dedupe, cap, flip-flop halt, settled-question drop."""
import pytest

from server.sessions.self_model_guards import SelfModelState, evaluate_guards


def _ev(state, current, requested, cap=3):
    return evaluate_guards(state, current_tier=current, requested_tier=requested, cap=cap)


def test_first_request_is_allowed():
    assert _ev(SelfModelState(), "opus", "haiku") == "auto_ok"


def test_same_tier_is_dropped():
    assert _ev(SelfModelState(), "opus", "opus") == "drop"


def test_request_for_a_tier_a_human_already_ruled_on_is_dropped():
    """Once a human amended a request, the session must not re-ask for it."""
    state = SelfModelState(amended_away={"fable"})
    assert _ev(state, "sonnet", "fable") == "drop"


def test_amendment_does_not_block_a_different_tier():
    state = SelfModelState(amended_away={"fable"})
    assert _ev(state, "sonnet", "haiku") == "auto_ok"


def test_returning_to_a_tier_already_left_forces_a_park():
    """opus -> haiku -> opus is a flip-flop; the second move needs a human."""
    state = SelfModelState(tiers_left={"opus"})
    assert _ev(state, "haiku", "opus") == "force_park"


def test_cap_degrades_to_parking_rather_than_dying():
    state = SelfModelState(auto_applied=3)
    assert _ev(state, "opus", "haiku", cap=3) == "force_park"


def test_under_the_cap_still_auto_applies():
    state = SelfModelState(auto_applied=2)
    assert _ev(state, "opus", "haiku", cap=3) == "auto_ok"


@pytest.mark.parametrize("current,requested", [(None, "haiku"), ("opus", None)])
def test_untiered_sides_are_not_dropped_here(current, requested):
    """Direction is the policy's job; guards must not swallow the request."""
    assert _ev(SelfModelState(), current, requested) == "auto_ok"
```

- [ ] **Step 2: Run test to verify it fails**

```bash
PYTHONPATH=$PWD /home/tony/GitHub/ccswitch/.venv/bin/python -m pytest \
  server/tests/test_self_model_guards.py -v
```
Expected: FAIL — `ModuleNotFoundError: No module named 'server.sessions.self_model_guards'`.

- [ ] **Step 3: Implement the guards**

Create `server/sessions/self_model_guards.py`:

```python
"""Loop/nag guards for self-directed model switching.

A session that can move its own model can oscillate or nag. These rules bound that
without needing the orchestrator, so they are testable in isolation. They answer only
"should this request be considered, and may it auto-apply?" — the cheaper/costlier
decision belongs to model_policy.resolve_self_directed_model_policy.

State is per-session and in-memory: it bounds nagging, not correctness, so losing it
across a daemon restart is acceptable.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class SelfModelState:
    """Per-session history of self-directed model moves."""

    # How many switches this session has auto-applied (approved upgrades don't count:
    # a human already reviewed those).
    auto_applied: int = 0
    # Tiers this session has moved AWAY from — returning to one is a flip-flop.
    tiers_left: set[str] = field(default_factory=set)
    # Tiers a human answered with a different model — the session must not re-ask.
    amended_away: set[str] = field(default_factory=set)


def evaluate_guards(
    state: SelfModelState, *, current_tier: str | None, requested_tier: str | None,
    cap: int,
) -> str:
    """Return "drop" | "auto_ok" | "force_park" for a self-directed request.

    - "drop": ignore entirely (no audit-worthy event happened).
    - "force_park": may proceed, but must NOT auto-apply — a human decides.
    - "auto_ok": no guard objects; the policy decides apply vs park.
    """
    if requested_tier is not None and requested_tier == current_tier:
        return "drop"
    if requested_tier is not None and requested_tier in state.amended_away:
        return "drop"
    if requested_tier is not None and requested_tier in state.tiers_left:
        return "force_park"
    if state.auto_applied >= cap:
        return "force_park"
    return "auto_ok"
```

- [ ] **Step 4: Add the cap to config**

In `server/config.py`, directly after `self_model_switch_enabled` (added in Task 2):

```python
        # How many model switches one session may auto-apply before further requests
        # are parked for a human instead. Bounds a session driving itself in circles.
        self_model_switch_cap: int = Field(
            default=3, alias="CCSWITCH_SELF_MODEL_SWITCH_CAP")
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
PYTHONPATH=$PWD /home/tony/GitHub/ccswitch/.venv/bin/python -m pytest \
  server/tests/test_self_model_guards.py -v
```
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add server/sessions/self_model_guards.py server/config.py \
        server/tests/test_self_model_guards.py
git commit -m "feat(models): guards for self-directed model switching"
```

---

### Task 7: Orchestrator — consume the request and act

Wire the pieces together: consume once per turn, run guards, run policy, then either apply via
`set_model_for_session` or park with the reason.

**Files:**
- Modify: `server/sessions/orchestrator.py:510-513` (state dicts), `:2090` (hook),
  `:1991`/`:8716` (cleanup), plus two new methods
- Test: `server/tests/test_self_model_switch_orchestrator.py` (new)

**Interfaces:**
- Consumes: `driver.last_model_request` (Task 3), `resolve_self_directed_model_policy` (Task 5),
  `SelfModelState` / `evaluate_guards` (Task 6), `pending_model_reason` (Task 4).
- Produces:
  - `SessionOrchestrator._maybe_handle_model_request(session_id, status) -> None` (sync, spawns)
  - `SessionOrchestrator._apply_model_request(session_id, req) -> None` (async)
  - `SessionOrchestrator._self_model_state: dict[str, SelfModelState]`
  - Audit types: `self_model_switch.applied`, `self_model_switch.refused`; parks reuse
    `model_approval.requested` with `identity="session:self"`.

- [ ] **Step 1: Write the failing test**

Create `server/tests/test_self_model_switch_orchestrator.py`:

**Harness directive (pre-flight ruling):** `server/tests/test_orchestrator_model_gate.py`
already establishes the orchestrator test harness for this area — a real in-memory DB via
`init_db`, `server/tests/fakes/fake_factory.fake_sdk_factory`, and `select_account` patched.
**Build these fixtures on that harness.** The `SessionOrchestrator.__new__` + null-DB stubs
sketched below are the assertion source, not the preferred wiring: keep every assertion
verbatim, but obtain the orchestrator the way the existing gate tests do wherever that
supports the assertion. Inventing a second orchestrator-faking pattern is the
parallel-implementation anti-pattern the constitution forbids.

```python
"""Orchestrator consumption of a session's self-directed model request."""
import pytest

from server.sessions.self_model_guards import SelfModelState


class _FakeDriver:
    def __init__(self, req=None):
        self.last_model_request = req


def _orch(monkeypatch, decision, *, current="opus", req=None):
    """A bare orchestrator with just the attributes the handler touches."""
    from server.sessions.orchestrator import SessionOrchestrator
    from server.sessions.types import SessionRecord
    o = SessionOrchestrator.__new__(SessionOrchestrator)
    o._records = {"s1": SessionRecord(id="s1", model=current, account="acct")}
    o._drivers = {"s1": _FakeDriver(req)}
    o._self_model_state = {}
    o._handled_model_requests = {}
    o.applied = []
    o.spawned = []
    o._spawn = lambda coro, **kw: o.spawned.append(coro)

    async def _decide(db, *, current_model=None, requested_model=None, account=None):
        return decision
    monkeypatch.setattr(
        "server.sessions.model_policy.resolve_self_directed_model_policy", _decide)
    # Default to a non-Copilot account so the real provider lookup never hits the ledger.
    import server.sessions.orchestrator as orch_mod
    monkeypatch.setattr(orch_mod, "_provider_of", lambda acct: "anthropic")
    return o


def test_no_request_is_a_no_op(monkeypatch):
    o = _orch(monkeypatch, "apply", req=None)
    o._maybe_handle_model_request("s1", "idle")
    assert o.spawned == []


def test_mid_turn_status_defers(monkeypatch):
    req = {"turn_id": "t1", "model": "haiku", "reason": "trivial"}
    o = _orch(monkeypatch, "apply", req=req)
    o._maybe_handle_model_request("s1", "in_progress")
    assert o.spawned == []


def test_a_request_is_consumed_exactly_once(monkeypatch):
    req = {"turn_id": "t1", "model": "haiku", "reason": "trivial"}
    o = _orch(monkeypatch, "apply", req=req)
    o._maybe_handle_model_request("s1", "idle")
    o._maybe_handle_model_request("s1", "idle")
    assert len(o.spawned) == 1


def test_a_new_turn_id_is_consumed_again(monkeypatch):
    req = {"turn_id": "t1", "model": "haiku", "reason": "trivial"}
    o = _orch(monkeypatch, "apply", req=req)
    o._maybe_handle_model_request("s1", "idle")
    o._drivers["s1"].last_model_request = {**req, "turn_id": "t2"}
    o._maybe_handle_model_request("s1", "idle")
    assert len(o.spawned) == 2


@pytest.mark.asyncio
async def test_downgrade_applies_with_the_session_identity(monkeypatch):
    req = {"turn_id": "t1", "model": "haiku", "reason": "trivial"}
    o = _orch(monkeypatch, "apply", current="opus", req=req)
    calls = []

    async def _set(session_id, model, *, when="now", identity="system"):
        calls.append((session_id, model, when, identity))
        return False
    o.set_model_for_session = _set
    monkeypatch.setattr("server.audit.record_audit", _noop_audit)

    await o._apply_model_request("s1", req)
    assert calls == [("s1", "haiku", "after_turn", "session:self")]
    assert o._self_model_state["s1"].auto_applied == 1
    assert "opus" in o._self_model_state["s1"].tiers_left


@pytest.mark.asyncio
async def test_guard_cap_forces_a_park_instead_of_applying(monkeypatch):
    req = {"turn_id": "t1", "model": "haiku", "reason": "trivial"}
    o = _orch(monkeypatch, "apply", current="opus", req=req)
    o._self_model_state["s1"] = SelfModelState(auto_applied=99)
    parked = []
    o._park_self_model_request = lambda sid, model, reason: parked.append((sid, model, reason))
    monkeypatch.setattr("server.audit.record_audit", _noop_audit)

    await o._apply_model_request("s1", req)
    assert parked == [("s1", "haiku", "trivial")]


@pytest.mark.asyncio
async def test_copilot_sessions_ignore_the_marker(monkeypatch):
    """Third-party model — a tier request is meaningless there."""
    import server.sessions.orchestrator as orch_mod
    req = {"turn_id": "t1", "model": "haiku", "reason": "trivial"}
    o = _orch(monkeypatch, "apply", current="opus", req=req)
    monkeypatch.setattr(orch_mod, "_provider_of", lambda acct: "copilot")
    called = []
    o.set_model_for_session = lambda *a, **k: called.append(a)
    o._park_self_model_request = lambda *a: called.append(a)

    await o._apply_model_request("s1", req)
    assert called == []


@pytest.mark.asyncio
async def test_kill_switch_disables_the_feature(monkeypatch):
    req = {"turn_id": "t1", "model": "haiku", "reason": "trivial"}
    o = _orch(monkeypatch, "apply", current="opus", req=req)

    class _Cfg:
        self_model_switch_enabled = False
        self_model_switch_cap = 3
    monkeypatch.setattr("server.config.get_settings", lambda: _Cfg())
    called = []
    o.set_model_for_session = lambda *a, **k: called.append(a)

    await o._apply_model_request("s1", req)
    assert called == []


@pytest.mark.asyncio
async def test_refused_request_changes_nothing(monkeypatch):
    req = {"turn_id": "t1", "model": "fable", "reason": "hard"}
    o = _orch(monkeypatch, "refuse", current="opus", req=req)
    called = []
    o.set_model_for_session = lambda *a, **k: called.append(a)
    o._park_self_model_request = lambda *a: called.append(a)
    monkeypatch.setattr("server.audit.record_audit", _noop_audit)

    await o._apply_model_request("s1", req)
    assert called == []


async def _noop_audit(db, **kwargs):
    return None
```

- [ ] **Step 2: Run test to verify it fails**

```bash
PYTHONPATH=$PWD /home/tony/GitHub/ccswitch/.venv/bin/python -m pytest \
  server/tests/test_self_model_switch_orchestrator.py -v
```
Expected: FAIL — `AttributeError: 'SessionOrchestrator' object has no attribute '_maybe_handle_model_request'`.

- [ ] **Step 3: Add the per-session state**

In `server/sessions/orchestrator.py`, beside `self._turn_restart_at` (line 513):

```python
        # Self-directed model switching: per-session guard state, and the turn id of
        # the last request consumed (so one request is acted on exactly once).
        from server.sessions.self_model_guards import SelfModelState  # noqa: F401
        self._self_model_state: dict[str, SelfModelState] = {}
        self._handled_model_requests: dict[str, str] = {}
```

Add the cleanup at **both** teardown sites (after line 1992 and after line 8717):

```python
        self._self_model_state.pop(session_id, None)
        self._handled_model_requests.pop(session_id, None)
```

- [ ] **Step 4: Add the consumption hook**

In `server/sessions/orchestrator.py`, immediately after the
`self._settle_model_pending(session_id, status)` call (line 2090):

```python
        self._maybe_handle_model_request(session_id, status)
```

Then add both methods next to `_settle_model_pending` (around line 8499):

```python
    def _maybe_handle_model_request(self, session_id: str, status: str) -> None:
        """Consume a self-directed model request the driver latched at turn end.

        Sync + fire-and-forget on purpose: this runs inside the status pump, and the
        handling does DB work (policy lookup, audit) that must not stall the turn loop
        — the same reason _persist_sub_agents is deferred.
        """
        from server.sessions.status import MID_TURN_STATUSES
        if status in MID_TURN_STATUSES:
            return
        driver = self._drivers.get(session_id)
        req = getattr(driver, "last_model_request", None)
        if not req:
            return
        turn_id = req.get("turn_id")
        if self._handled_model_requests.get(session_id) == turn_id:
            return
        self._handled_model_requests[session_id] = turn_id
        self._spawn(self._apply_model_request(session_id, req))

    async def _apply_model_request(self, session_id: str, req: dict) -> None:
        """Guards -> policy -> apply or park. Never raises into the caller.

        Fail-open per the feature's invariant: any failure here drops the
        recommendation and logs. A session must never fail a turn because it asked
        for a different model.
        """
        from server.audit import record_audit
        from server.config import get_settings
        from server.sessions.model_catalog import model_tier
        from server.sessions.model_policy import resolve_self_directed_model_policy
        from server.sessions.self_model_guards import SelfModelState, evaluate_guards
        try:
            cfg = get_settings()
            if not cfg.self_model_switch_enabled:
                return
            rec = self._records.get(session_id)
            if rec is None:
                return
            # Copilot sessions run a third-party model; a Claude tier request is
            # meaningless there. Same reasoning as _pre_record skipping suggestions.
            if _provider_of(rec.account) == _copilot_mod.PROVIDER:
                return
            requested, reason = req.get("model"), req.get("reason")
            current = rec.model
            state = self._self_model_state.setdefault(session_id, SelfModelState())

            guard = evaluate_guards(
                state, current_tier=model_tier(current),
                requested_tier=model_tier(requested), cap=cfg.self_model_switch_cap)
            if guard == "drop":
                log.debug("session.self_model_request_dropped id=%s model=%s",
                          session_id, requested)
                return

            async with db_session() as db:
                decision = await resolve_self_directed_model_policy(
                    db, current_model=current, requested_model=requested,
                    account=rec.account)
            if decision == "noop":
                return
            if decision == "refuse":
                async with db_session() as db:
                    await record_audit(
                        db, type="self_model_switch.refused", identity="session:self",
                        detail={"session_id": session_id, "model": requested,
                                "reason": reason})
                log.info("session.self_model_request_refused id=%s model=%s",
                         session_id, requested)
                return

            if decision == "park" or guard == "force_park":
                await self._park_self_model_request(session_id, requested, reason)
                return

            await self.set_model_for_session(
                session_id, requested, when="after_turn", identity="session:self")
            state.auto_applied += 1
            if model_tier(current):
                state.tiers_left.add(model_tier(current))
            async with db_session() as db:
                await record_audit(
                    db, type="self_model_switch.applied", identity="session:self",
                    detail={"session_id": session_id, "from": current,
                            "model": requested, "reason": reason})
            log.info("session.self_model_switch_applied id=%s %s -> %s",
                     session_id, current, requested)
        except Exception:
            log.exception("session.self_model_request_failed id=%s", session_id)

    async def _park_self_model_request(
        self, session_id: str, model: str, reason: str | None,
    ) -> None:
        """Park a self-directed request for human approval, carrying its reason.

        Writes the same pending_model columns the model-approval gate uses, so the
        existing approval card, REST route and resolve path handle it unchanged —
        only the identity and the reason distinguish it.
        """
        from datetime import datetime
        from sqlalchemy import update as sa_update

        from server.audit import record_audit
        rec = self._records.get(session_id)
        if rec is not None:
            if rec.pending_model == model:
                return                                  # already parked; don't re-park
            rec.pending_model = model
            rec.pending_model_reason = reason
        now = datetime.utcnow()
        async with db_session() as db:
            await db.execute(
                sa_update(DbSession).where(DbSession.id == session_id).values(
                    pending_model=model, pending_model_reason=reason,
                    pending_model_requested_by="session:self",
                    pending_model_requested_at=now))
            await record_audit(
                db, type="model_approval.requested", identity="session:self",
                detail={"session_id": session_id, "model": model, "reason": reason,
                        "source": "session"})
        if rec is not None:
            self._publish_event({
                "type": "status", "sessionId": session_id, "status": rec.status,
                "updatedAt": rec.updated_at_iso, "revAt": rec.rev_at})
        log.info("session.self_model_request_parked id=%s model=%s", session_id, model)
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
PYTHONPATH=$PWD /home/tony/GitHub/ccswitch/.venv/bin/python -m pytest \
  server/tests/test_self_model_switch_orchestrator.py -v
PYTHONPATH=$PWD /home/tony/GitHub/ccswitch/.venv/bin/python -m pytest \
  server/tests/ -k "model" -q
```
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add server/sessions/orchestrator.py server/tests/test_self_model_switch_orchestrator.py
git commit -m "feat(models): act on session-initiated model requests"
```

---

### Task 8: The override — approve at a different model

Let the human counter-offer. The riskiest task: the approval path must not re-enter the gate.

**Files:**
- Modify: `server/sessions/orchestrator.py:1354-1500` (`resolve_model_approval` takes `model`)
- Modify: `server/api/sessions.py:106-107` (`ModelApprovalBody.model`), `:1320-1341` (route)
- Test: `server/tests/test_model_approval_override.py` (new)

**Interfaces:**
- Consumes: `resolve_model_approval` (existing), `is_known_alias` (existing), `model_tier` (Task 1).
- Produces:
  - `resolve_model_approval(session_id, decision, *, model: str | None = None, identity=…)`
  - `ModelApprovalBody.model: str | None = None`
  - `ModelBlockedError` raised for a blocked counter-offer → HTTP 409.

- [ ] **Step 1: Write the failing test**

Create `server/tests/test_model_approval_override.py`:

**Harness directive (pre-flight ruling):** `server/tests/test_orchestrator_model_gate.py`
already establishes the orchestrator test harness for this area — a real in-memory DB via
`init_db`, `server/tests/fakes/fake_factory.fake_sdk_factory`, and `select_account` patched.
**Build these fixtures on that harness.** The `SessionOrchestrator.__new__` + null-DB stubs
sketched below are the assertion source, not the preferred wiring: keep every assertion
verbatim, but obtain the orchestrator the way the existing gate tests do wherever that
supports the assertion. Inventing a second orchestrator-faking pattern is the
parallel-implementation anti-pattern the constitution forbids.

```python
"""Approve-as: the human may substitute a model, but block still refuses."""
import pytest

from server.sessions.orchestrator import ModelBlockedError, SessionOrchestrator
from server.sessions.types import SessionRecord


class _FakeDriver:
    def __init__(self):
        self.set_model_calls: list[str] = []
        self.turn_active = False

    async def set_model(self, model):
        self.set_model_calls.append(model)


@pytest.fixture
def audit_log(monkeypatch):
    """Capture record_audit calls instead of writing them."""
    entries: list[dict] = []

    async def _record(db, *, type, identity, detail=None, **kw):
        entries.append({"type": type, "identity": identity, "detail": detail or {}})
    monkeypatch.setattr("server.audit.record_audit", _record)
    monkeypatch.setattr("server.sessions.orchestrator.record_audit", _record, raising=False)
    return entries


@pytest.fixture
def orch_with_hold(monkeypatch, audit_log):
    """An orchestrator holding a mid-session, session-initiated fable request."""
    o = SessionOrchestrator.__new__(SessionOrchestrator)
    rec = SessionRecord(id="s1", model="sonnet", account="acct", status="in_progress")
    rec.pending_model = "fable"
    rec.pending_model_reason = "cross-module refactor"
    o._records = {"s1": rec}
    driver = _FakeDriver()
    o._drivers = {"s1": driver}
    o._self_model_state = {}
    o._handled_model_requests = {}
    o._publish_event = lambda payload: None
    o._pending_approvals = {}

    async def _allow(db, *, model=None, account=None):
        return "allow"
    monkeypatch.setattr("server.sessions.model_policy.resolve_model_policy", _allow)

    # Neutralise DB writes: this test exercises decision logic, not persistence.
    class _NullDb:
        async def execute(self, *a, **k):
            return None

        async def get(self, *a, **k):
            return None

    class _NullCtx:
        async def __aenter__(self):
            return _NullDb()

        async def __aexit__(self, *a):
            return False
    monkeypatch.setattr("server.sessions.orchestrator.db_session", lambda: _NullCtx())
    return o, driver


@pytest.mark.asyncio
async def test_override_applies_the_humans_model_not_the_requested_one(orch_with_hold):
    o, driver = orch_with_hold
    await o.resolve_model_approval("s1", "approve", model="opus", identity="tony")
    assert driver.set_model_calls == ["opus"]
    assert o._records["s1"].model == "opus"


@pytest.mark.asyncio
async def test_override_clears_the_hold_and_does_not_re_park(orch_with_hold):
    """The infinite-approval-loop regression: approving must END the negotiation."""
    o, _ = orch_with_hold
    await o.resolve_model_approval("s1", "approve", model="opus", identity="tony")
    rec = o._records["s1"]
    assert rec.pending_model is None
    assert rec.pending_model_reason is None


@pytest.mark.asyncio
async def test_no_override_approves_exactly_what_was_requested(orch_with_hold):
    o, driver = orch_with_hold
    await o.resolve_model_approval("s1", "approve", identity="tony")
    assert driver.set_model_calls == ["fable"]


@pytest.mark.asyncio
async def test_blocked_counter_offer_is_refused(orch_with_hold, monkeypatch):
    async def _block(db, *, model=None, account=None):
        return "block" if model == "opus" else "allow"
    monkeypatch.setattr("server.sessions.model_policy.resolve_model_policy", _block)
    o, driver = orch_with_hold
    with pytest.raises(ModelBlockedError):
        await o.resolve_model_approval("s1", "approve", model="opus", identity="tony")
    assert driver.set_model_calls == []
    assert o._records["s1"].pending_model == "fable"      # hold survives a refusal


@pytest.mark.asyncio
async def test_override_records_requested_and_applied(orch_with_hold, audit_log):
    o, _ = orch_with_hold
    await o.resolve_model_approval("s1", "approve", model="opus", identity="tony")
    entry = [a for a in audit_log if a["type"] == "model_approval.approve"][-1]
    assert entry["detail"]["requested"] == "fable"
    assert entry["detail"]["applied"] == "opus"


@pytest.mark.asyncio
async def test_override_marks_the_tier_settled_so_the_session_stops_asking(orch_with_hold):
    o, _ = orch_with_hold
    await o.resolve_model_approval("s1", "approve", model="opus", identity="tony")
    assert "fable" in o._self_model_state["s1"].amended_away


@pytest.mark.asyncio
async def test_launch_time_hold_spawns_with_the_counter_offer(monkeypatch, audit_log):
    """The other hold kind: a not-yet-spawned session approved at a different model."""
    o = SessionOrchestrator.__new__(SessionOrchestrator)
    rec = SessionRecord(id="s2", model=None, account="acct", status="pending_approval")
    rec.pending_model = "fable"
    o._records = {"s2": rec}
    o._drivers = {}
    o._self_model_state = {}
    o._handled_model_requests = {}
    o._pending_approvals = {"s2": {"cwd": "/tmp"}}
    o._publish_event = lambda payload: None
    spawned = []

    async def _spawn_session(session_id, *, model=None, **kw):
        spawned.append((session_id, model))
        return rec
    o._spawn_session = _spawn_session
    o._active_session_count = lambda: 0

    async def _max_active():
        return 10
    o._max_active_sessions = _max_active

    async def _allow(db, *, model=None, account=None):
        return "allow"
    monkeypatch.setattr("server.sessions.model_policy.resolve_model_policy", _allow)

    class _NullDb:
        async def execute(self, *a, **k):
            return None

        async def get(self, *a, **k):
            return None

        async def delete(self, *a, **k):
            return None

    class _NullCtx:
        async def __aenter__(self):
            return _NullDb()

        async def __aexit__(self, *a):
            return False
    monkeypatch.setattr("server.sessions.orchestrator.db_session", lambda: _NullCtx())

    await o.resolve_model_approval("s2", "approve", model="opus", identity="tony")
    assert spawned == [("s2", "opus")]
```

- [ ] **Step 2: Run test to verify it fails**

```bash
PYTHONPATH=$PWD /home/tony/GitHub/ccswitch/.venv/bin/python -m pytest \
  server/tests/test_model_approval_override.py -v
```
Expected: FAIL — `TypeError: resolve_model_approval() got an unexpected keyword argument 'model'`.

- [ ] **Step 3: Accept the override in `resolve_model_approval`**

In `server/sessions/orchestrator.py`, change the signature (line ~1353) to:

```python
    async def resolve_model_approval(self, session_id: str, decision: str, *,
                                     model: str | None = None,
                                     identity: str) -> SessionRecord:
```

Extend the docstring with:

```
        *model*, when given, is a human COUNTER-OFFER: approve the hold at this model
        instead of the parked one. Only a `block` policy can refuse it — the human
        performing this action IS the approval that `require_approval` asks for.

        NB: the substitution is applied to the parked value and then flows through the
        SAME low-level paths this method already uses (_spawn_session for a launch hold,
        driver.set_model for a mid-session one). It must NEVER be routed through
        set_model_for_session(), which re-runs resolve_model_policy and would re-park —
        making every approval re-ask for approval.
```

Directly after the `decision` validation and the `rec is None` check, insert the
counter-offer resolution:

```python
        # Counter-offer: only `block` may refuse it. Checked DIRECTLY here rather than
        # by routing through the gated set_model_for_session (see docstring).
        requested_model = rec.pending_model
        if model is not None and model != requested_model and decision == "approve":
            from server.sessions.model_policy import resolve_model_policy
            async with db_session() as _db:
                _p = await resolve_model_policy(_db, model=model, account=rec.account)
            if _p == "block":
                raise ModelBlockedError(model)
            # The session must stop asking for a tier a human has already answered.
            from server.sessions.self_model_guards import SelfModelState
            tier = model_tier(requested_model)
            if tier:
                self._self_model_state.setdefault(
                    session_id, SelfModelState()).amended_away.add(tier)
            rec.pending_model = model
```

Add `model_tier` to the module imports if it is not already imported:

```python
from server.sessions.model_catalog import model_tier
```

- [ ] **Step 4: Record requested vs applied, and clear the reason**

In the **mid-session** branch, replace the audit call and the values dict (around line 1465):

```python
                await record_audit(
                    db, type=f"model_approval.{decision}", identity=identity,
                    detail={"session_id": session_id, "model": pending,
                            "requested": requested_model, "applied": pending})
                values: dict = {
                    "pending_model": None,
                    "pending_model_reason": None,
                    "pending_model_requested_by": None,
                    "pending_model_requested_at": None,
                }
```

and after `rec.pending_model = None` add:

```python
            rec.pending_model_reason = None
```

In the **launch-time** branch, extend its audit detail the same way:

```python
                await record_audit(
                    db, type=f"model_approval.{decision}", identity=identity,
                    detail={"session_id": session_id, "model": approved_model,
                            "requested": requested_model, "applied": approved_model})
```

and add `pending_model_reason=None` to the deny-path `values(...)` call.

- [ ] **Step 5: Accept it on the API**

In `server/api/sessions.py`, replace `ModelApprovalBody` (lines 106-107):

```python
class ModelApprovalBody(BaseModel):
    decision: Literal["approve", "deny"]
    # Optional counter-offer: approve the hold at THIS model instead of the parked
    # one. Absent = approve exactly what was requested (pre-override behaviour).
    model: str | None = None
```

In the route (line ~1336), validate and forward:

```python
    if body.model is not None:
        from server.sessions.model_catalog import is_known_alias, model_tier
        if not (is_known_alias(body.model) or model_tier(body.model)):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={"code": "unknown_model", "message": f"unknown model: {body.model}"})
    orch = _get_orchestrator(request)
    try:
        rec = await orch.resolve_model_approval(
            session_id, body.decision, model=body.model, identity=identity)
    except KeyError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="session not found")
    except ModelBlockedError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "model_blocked", "message": f"{body.model} is blocked by policy"})
    except ValueError:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="no pending model approval")
    return _record_to_dict(rec)
```

Import `ModelBlockedError` at the top of `server/api/sessions.py` alongside the other
orchestrator imports.

- [ ] **Step 6: Run tests to verify they pass**

```bash
PYTHONPATH=$PWD /home/tony/GitHub/ccswitch/.venv/bin/python -m pytest \
  server/tests/test_model_approval_override.py server/tests/test_api_model_approval.py \
  server/tests/test_orchestrator_model_gate.py -v
CCSWITCH_TAILNET_ALLOWLIST= CCSWITCH_DEV=1 PYTHONPATH=$PWD \
  /home/tony/GitHub/ccswitch/.venv/bin/python -m pytest server/tests/ -k "approval" -q
```
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add server/sessions/orchestrator.py server/api/sessions.py \
        server/tests/test_model_approval_override.py
git commit -m "feat(models): approve a parked model hold at a different model"
```

---

### Task 9: The approval card — reason, attribution, and "Approve as…"

**Files:**
- Modify: `web/src/components/session/PendingModelApprovalPanel.tsx`
- Modify: `web/src/api/hooks.ts:324` (mutation takes an optional model)
- Modify: `web/src/components/session/SessionBody.tsx:231-236`
- Modify: `web/src/components/PendingApprovalSection.tsx:74-77`
- Test: `web/src/components/session/__tests__/PendingModelApprovalPanel.test.tsx`

**Interfaces:**
- Consumes: `pendingModelReason` (Task 4), the `model` body field (Task 8), `tierLabel` (Task 1).
- Produces: `PendingModelApprovalPanel` props `{ model, reason, requestedBy, onDecision }` where
  `onDecision: (decision: 'approve' | 'deny', model?: string) => void`.

- [ ] **Step 1: Write the failing test**

Create `web/src/components/session/__tests__/PendingModelApprovalPanel.test.tsx`:

```tsx
import { render, screen, fireEvent } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { PendingModelApprovalPanel } from '../PendingModelApprovalPanel'

vi.mock('@/api/hooks', () => ({
  useModels: () => ({ data: [{ id: 'haiku', label: 'Haiku' }, { id: 'sonnet', label: 'Sonnet' },
                             { id: 'opus', label: 'Opus' }, { id: 'fable', label: 'Fable' }] }),
}))

describe('PendingModelApprovalPanel', () => {
  it('shows the requested model label', () => {
    render(<PendingModelApprovalPanel model="fable" onDecision={vi.fn()} />)
    expect(screen.getByText(/Fable/)).toBeInTheDocument()
  })

  it('shows the reason when one is given', () => {
    render(<PendingModelApprovalPanel model="fable" reason="cross-module refactor"
                                      onDecision={vi.fn()} />)
    expect(screen.getByText(/cross-module refactor/)).toBeInTheDocument()
  })

  it('attributes a session-initiated request', () => {
    render(<PendingModelApprovalPanel model="fable" requestedBy="session:self"
                                      onDecision={vi.fn()} />)
    expect(screen.getByText(/the session requested this/i)).toBeInTheDocument()
  })

  it('approves the requested model when the picker is untouched', () => {
    const onDecision = vi.fn()
    render(<PendingModelApprovalPanel model="fable" onDecision={onDecision} />)
    fireEvent.click(screen.getByRole('button', { name: /approve/i }))
    expect(onDecision).toHaveBeenCalledWith('approve', 'fable')
  })

  it('approves the counter-offer when a different model is picked', () => {
    const onDecision = vi.fn()
    render(<PendingModelApprovalPanel model="fable" onDecision={onDecision} />)
    fireEvent.change(screen.getByLabelText(/approve as/i), { target: { value: 'opus' } })
    fireEvent.click(screen.getByRole('button', { name: /approve/i }))
    expect(onDecision).toHaveBeenCalledWith('approve', 'opus')
  })

  it('denies without a model', () => {
    const onDecision = vi.fn()
    render(<PendingModelApprovalPanel model="fable" onDecision={onDecision} />)
    fireEvent.click(screen.getByRole('button', { name: /deny/i }))
    expect(onDecision).toHaveBeenCalledWith('deny', undefined)
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

```bash
npm --prefix web run test -- PendingModelApprovalPanel
```
Expected: FAIL — the panel accepts no `reason` prop and renders no picker.

- [ ] **Step 3: Rewrite the panel**

Replace the `Props` interface and component in
`web/src/components/session/PendingModelApprovalPanel.tsx` (keep the existing file header
comment, updating it to mention the counter-offer):

```tsx
import { useState } from 'react'
import { useModels } from '@/api/hooks'

interface Props {
  /** The model alias or full id requiring approval (e.g. "fable", "claude-opus-4-8"). */
  model: string | null | undefined
  /** The requester's stated reason, when there is one. */
  reason?: string | null
  /** Identity that requested it; "session:self" means the session asked for itself. */
  requestedBy?: string | null
  /** Second arg is the model to approve AS — omitted for a deny. */
  onDecision: (decision: 'approve' | 'deny', model?: string) => void
}

export function PendingModelApprovalPanel({ model, reason, requestedBy, onDecision }: Props) {
  const label = tierLabel(model)
  const { data: models } = useModels()
  // Default the picker to the requested model so a plain Approve is unchanged.
  const [choice, setChoice] = useState<string>(model ?? '')
  const bySession = requestedBy === 'session:self'
  // A pinned id (e.g. "claude-opus-5") is valid input but is not in the alias catalog.
  // Prepend it so the default selection is always a real option.
  const options = models ?? []
  const allOptions = !model || options.some((m) => m.id === model)
    ? options
    : [{ id: model, label }, ...options]

  return (
    <div
      role="region"
      aria-label="Pending model approval"
      aria-live="polite"
      className={cn(
        'flex-shrink-0 border-t border-violet-200 dark:border-violet-800',
        'bg-violet-50/60 dark:bg-violet-950/20 px-3 py-2',
        'flex flex-wrap items-center gap-3',
      )}
      data-testid="pending-model-approval-panel"
    >
      <span className="text-sm flex-1 min-w-[12rem]">
        <strong>{label}</strong> requires approval before this session can use it.
        {bySession && (
          <em className="not-italic text-gray-600 dark:text-gray-400"> — the session requested this.</em>
        )}
        {reason && (
          <span className="block text-xs text-gray-600 dark:text-gray-400 mt-0.5">
            “{reason}”
          </span>
        )}
      </span>
      <label className="flex shrink-0 items-center gap-1 text-sm">
        <span className="text-gray-600 dark:text-gray-400">Approve as</span>
        <select
          aria-label="Approve as"
          value={choice}
          onChange={(e) => setChoice(e.target.value)}
          className={cn(
            'rounded border border-gray-300 dark:border-gray-600 px-2 py-1 text-sm',
            'bg-white dark:bg-gray-900',
          )}
        >
          {allOptions.map((m) => (
            <option key={m.id} value={m.id}>{m.label}</option>
          ))}
        </select>
      </label>
      <div className="flex shrink-0 gap-2">
        <button
          type="button"
          onClick={() => onDecision('approve', choice || undefined)}
          className={cn(
            'rounded px-3 py-1 text-sm font-medium',
            'bg-violet-600 text-white hover:bg-violet-700',
            'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-violet-500',
          )}
        >
          Approve
        </button>
        <button
          type="button"
          onClick={() => onDecision('deny')}
          className={cn(
            'rounded px-3 py-1 text-sm font-medium',
            'border border-gray-300 dark:border-gray-600',
            'text-gray-700 dark:text-gray-300',
            'hover:bg-gray-100 dark:hover:bg-gray-800',
            'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-violet-500',
          )}
        >
          Deny
        </button>
      </div>
    </div>
  )
}
```

- [ ] **Step 4: Thread the model through the mutation**

In `web/src/api/hooks.ts`, change the `useModelApproval` mutation (line ~324):

```ts
    ({ id, decision, model }: { id: string; decision: 'approve' | 'deny'; model?: string }) =>
      api.post<Session>(`/sessions/${id}/model-approval`, { decision, model }),
```

- [ ] **Step 5: Update both call sites**

`web/src/components/session/SessionBody.tsx` (lines 231-236):

```tsx
              <PendingModelApprovalPanel
                model={sessionRecord.pendingModel}
                reason={sessionRecord.pendingModelReason}
                requestedBy={sessionRecord.pendingModelRequestedBy}
                onDecision={(decision, model) =>
                  modelApproval({ id: rt.sessionId, decision, model })
                }
              />
```

`web/src/components/PendingApprovalSection.tsx` (lines 74-77):

```tsx
              <PendingModelApprovalPanel
                model={session.pendingModel}
                reason={session.pendingModelReason}
                requestedBy={session.pendingModelRequestedBy}
                onDecision={(decision, model) =>
                  modelApproval({ id: session.id, decision, model })
                }
              />
```

This needs `pendingModelRequestedBy` on the payload. Add it in `server/api/sessions.py` beside
`pendingModelReason`:

```python
        "pendingModelRequestedBy": getattr(record, "pending_model_requested_by", None),
```

In `server/sessions/types.py`, add the field beside `pending_model_reason`:

```python
    pending_model_requested_by: str | None = None
```

in `to_dict`:

```python
            "pendingModelRequestedBy": self.pending_model_requested_by,
```

and in `from_dict`:

```python
            pending_model_requested_by=d.get("pendingModelRequestedBy"),
```

Then add it to the `Session` interface in `web/src/api/types.ts`:

```ts
  /** Identity that requested pendingModel; "session:self" when the session asked. */
  pendingModelRequestedBy?: string | null
```

- [ ] **Step 6: Run tests to verify they pass**

```bash
npm --prefix web run test -- PendingModelApprovalPanel
npm --prefix web exec tsc -- --noEmit
npm --prefix web exec eslint -- src
PYTHONPATH=$PWD /home/tony/GitHub/ccswitch/.venv/bin/python -m pytest \
  server/tests/test_pending_model_reason_column.py -v
```
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add web/src/components/session/PendingModelApprovalPanel.tsx web/src/api/hooks.ts \
        web/src/components/session/SessionBody.tsx web/src/components/PendingApprovalSection.tsx \
        web/src/api/types.ts server/api/sessions.py server/sessions/types.py \
        web/src/components/session/__tests__/PendingModelApprovalPanel.test.tsx
git commit -m "feat(web): show the request reason and allow approving at another model"
```

---

### Task 10: Full gate, docs, and PR

**Files:**
- Modify: `CLAUDE.md` (document the feature's operational notes)
- Modify: `README.md` if it documents session model selection

- [ ] **Step 1: Run the complete four-step gate**

```bash
ruff check src/ server/
/home/tony/GitHub/ccswitch/.venv/bin/python -m mypy server/ --ignore-missing-imports
PYTHONPATH=$PWD /home/tony/GitHub/ccswitch/.venv/bin/python -m pytest tests/ -q
CCSWITCH_TAILNET_ALLOWLIST= CCSWITCH_DEV=1 PYTHONPATH=$PWD \
  /home/tony/GitHub/ccswitch/.venv/bin/python -m pytest server/tests/ -q
```
Expected: all four green. A green `server/tests/` run alone is NOT the gate — mypy in
particular catches what no test does.

- [ ] **Step 2: Run the full web gate**

```bash
npm --prefix web run test
npm --prefix web exec tsc -- --noEmit
npm --prefix web exec eslint -- src
```
Expected: all green.

- [ ] **Step 3: Document the feature in CLAUDE.md**

Add a section after "Copilot account type":

```markdown
## Self-directed model switching

A running session can ask for a different model by ending a message with
`<!-- ccswitch:model=<tier> reason="..." -->`. The contract lives in
`server/sessions/model_request.py` (marker + instruction + parser, one module so the
system-prompt half and the scan half cannot drift).

- **Downgrades auto-apply** on the next turn; **upgrades park** for human approval in the
  existing `pending_model` columns. `resolve_self_directed_model_policy` composes this with
  the tier denylist and takes the stricter outcome — a **blocked tier stays blocked even as a
  downgrade**.
- A parked hold can be approved **at a different model** (`POST /sessions/{id}/model-approval`
  with `{"decision": "approve", "model": "opus"}`). Only `block` refuses a counter-offer.
- **Never route an approval through `set_model_for_session()`** — it re-runs the policy and
  re-parks, so every approval would re-ask for approval. Approvals use the low-level
  `driver.set_model()` / `_spawn_session(model=…)` paths.
- Guards (`server/sessions/self_model_guards.py`): same-tier dedupe, a cap of
  `CCSWITCH_SELF_MODEL_SWITCH_CAP` (default 3) auto-applies per session, a flip-flop halt, and
  a drop once a human has amended a request for that tier.
- Kill switch: `CCSWITCH_SELF_MODEL_SWITCH_ENABLED=false` stops both the prompt instruction
  and any acting on a marker.
- Audit `model_approval.approve` carries `{requested, applied}` — requested ≠ applied is the
  metric for whether sessions self-assess accurately.
```

- [ ] **Step 4: Commit and open the PR**

```bash
git add CLAUDE.md
git commit -m "docs: self-directed model switching operational notes"
git push -u origin HEAD
gh pr create --title "feat: session-recommended model switching with human counter-offer" \
  --body "$(cat <<'BODY'
Lets a running session recommend its own model change via a marker in its output.
Downgrades auto-apply on the next turn; upgrades park for approval and can be
approved at a different model than the one requested.

Spec: docs/superpowers/specs/2026-09-02-session-recommended-model-switching-design.md
Plan: docs/superpowers/plans/2026-09-02-session-recommended-model-switching.md

Reuses the model-approval gate wholesale (model_policy, the pending_model columns,
resolve_model_approval, the REST route, PendingModelApprovalPanel). Also consolidates
four copies of the model-tier list into one ordered TIER_ORDER.

Sub-agent model steering is deliberately out of scope; the investigation is captured
in the spec's "Out of scope" section.

🤖 Generated with [Claude Code](https://claude.com/claude-code)

https://claude.ai/code/session_01NxW5LC17AtwvwqPgCBEdVU
BODY
)"
gh pr merge --squash --auto
```

Per CLAUDE.md: open non-draft and arm auto-merge. Do **not** pass `--delete-branch`.

---

## Notes for the executor

- **Task order matters.** Task 1 must land first (everything downstream imports `TIER_ORDER`);
  Task 4 must precede Task 7 (parking writes `pending_model_reason`); Task 8 must precede Task 9.
- **The single highest-risk change is Task 8.** If an approval ever re-parks, the user sees a
  card that will not go away. `test_override_clears_the_hold_and_does_not_re_park` is the test
  that catches it — do not weaken it.
- **Fail-open is a hard requirement, not a nicety.** If you find yourself writing a code path
  where a model-request failure can propagate into the turn loop, stop and wrap it.

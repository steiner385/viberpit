# Roadmap Consequence Guidance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every roadmap / SDLC / PM control tells the user, in place and computed from the real engine, what tapping it changes now, what the machine does afterwards and when, and whether that starts a billed session or writes to GitHub.

**Architecture:** A pure `server/sdlc/explain.py` runs `next_action` on the hypothetical post-tap state and describes the result; the initiative detail endpoint attaches it as `guidance`, a new `GET /api/sdlc/gates` exposes the gate registry, and PM triage rows gain `guidance` from a pure `server/pm/explain.py` over the product's dispatch policy. One React component, `ConsequenceList`, renders every consequence; the initiative page's confirm dialogs, a "What happens next" card, the product automation card's gate table, and the inbox pane consume it. `nextAction` moves off the hardcoded `cautious` tolerance.

**Tech Stack:** Python 3 / FastAPI / SQLAlchemy (async) · pytest · React 18 + TypeScript + vitest + testing-library · Tailwind classes already used by the roadmap feature.

**Spec:** `docs/superpowers/specs/2026-09-17-roadmap-consequence-guidance-design.md`

## Global Constraints

- Pure modules (`server/sdlc/explain.py`, `server/sdlc/paths.py`, `server/pm/explain.py`) import nothing from `server.db`, `server.automations`, `server.sessions` (spec §3; same rule as `server/sdlc/engine.py` docstring lines 1–8).
- No new `CCSWITCH_*` env vars; the only setting read is the existing `VIBERPIT_GATE_AUTORESOLVE_ENABLED` (`server/config.py:254`).
- Copy rules: sentences end with a period; no arrows or em-dashes in user-facing text; stage ids appear as their raw lowercase id (`plan`, `build`) — the client already renders them as-is beside `stageLabel`.
- The tick interval is never a literal in copy; it travels as `tickSeconds` (spec D6).
- Working norm: purge replaced code in the same PR — `NEXT_ACTION_LABEL` / `nextActionLabel` in `web/src/features/products/vocab.ts` are deleted in Task 8.
- Run the FULL `npm run test` in `web/` (many test files mock `@/api/hooks`; a new hook on a shared component breaks files you never touched).
- Server gate is four steps: `ruff check src/ server/`, `mypy server/ --ignore-missing-imports`, `pytest tests/`, `pytest server/tests/`. Run `server/tests/` with `env -u CCSWITCH_TAILNET_ALLOWLIST -u CCSWITCH_SESSIOND_CERT -u CCSWITCH_SESSIOND_KEY -u CCSWITCH_SESSIOND_CA -u CCSWITCH_DAEMON_ROLE CCSWITCH_DEV=1 …`.
- Python interpreter: the worktree has no venv; use `/home/tony/GitHub/viberpit-deploy/.venv/bin/python -m pytest` (or the main checkout's `.venv`), and `PYTHONPATH=$PWD/src` on both suites. `web/node_modules` is a symlink to the deploy checkout's.
- Commit trailer on every commit: `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`. Write commit messages to a file and pass `-F` (the Bash hook denies command text pairing deploy verbs with prod words).

---

## File structure

| Path | Responsibility |
|---|---|
| `server/sdlc/paths.py` (new) | `SDLC_DOCS_DIR`, `artifact_path()` — the artifact layout, pure. `prompts.py` re-exports them. |
| `server/sdlc/explain.py` (new) | Consequence model + `describe` / `explain` / wire serializers / `gates_to_wire` / `dispatch_sentence`. Pure. |
| `server/pm/explain.py` (new) | `triage_guidance()` — accept/reject consequences for one PM triage proposal. Pure. |
| `server/automations/sdlc_synthesizer.py` | gains `TICK_INTERVAL_S` and module-level `read_synth_mode()`; orchestrator delegates to both. |
| `server/api/sdlc.py` | detail computes `nextAction` under effective tolerance + `guidance`; new `GET /gates`. |
| `server/api/pm.py` | `list_triage` attaches `guidance` per row. |
| `web/src/api/sdlc.ts`, `triage.ts`, `hooks.ts` | types, `fetchGates`, `useSdlcGates`, `QK.gates`. |
| `web/src/features/roadmap/guidance/ConsequenceList.tsx` (new) | THE consequence renderer (+ `StepRow`, `whenLabel`). |
| `web/src/features/roadmap/guidance/WhatHappensNext.tsx` (new) | The Track-tab outlook card. |
| `web/src/features/roadmap/InitiativePage.tsx` | dialogs, tooltips, card wiring; `MaterializationCard` gets a `guidance` prop. |
| `web/src/features/roadmap/detail/AutomationPolicyCard.tsx` | Gates section. |
| `web/src/features/roadmap/inbox/ProposalDetail.tsx` | Accept / Reject consequence lists. |
| `web/src/features/products/vocab.ts` | purge `NEXT_ACTION_LABEL` / `nextActionLabel`. |
| `docs/product/features/sdlc-governance-evals.md` | one "Guidance" paragraph + links. |

---

### Task 1: Move the artifact path helper to a pure module

**Files:**
- Create: `server/sdlc/paths.py`
- Modify: `server/sdlc/prompts.py:31-44`
- Test: `server/tests/test_sdlc_paths.py`

**Interfaces:**
- Produces: `server.sdlc.paths.artifact_path(slug: str, kind: str) -> str` and `SDLC_DOCS_DIR: str`. `server.sdlc.prompts` keeps exporting both names (re-export), so no existing import breaks.

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_sdlc_paths.py
"""The artifact layout lives in a pure module so the consequence explainer
(server/sdlc/explain.py) can name the file a session will add without importing
the prompt builders, which pull in session code."""
import importlib
import sys


def test_artifact_path_layout():
    from server.sdlc.paths import SDLC_DOCS_DIR, artifact_path
    assert SDLC_DOCS_DIR == "docs/sdlc"
    assert artifact_path("dark-mode", "spec") == "docs/sdlc/dark-mode/spec.md"


def test_paths_module_is_pure():
    """Importing it must not drag in the DB, automations or session packages."""
    for name in [m for m in sys.modules if m.startswith("server.")]:
        del sys.modules[name]
    importlib.import_module("server.sdlc.paths")
    assert not any(m.startswith(("server.db", "server.automations", "server.sessions"))
                   for m in sys.modules), "server.sdlc.paths imported impure packages"


def test_prompts_reexports_the_same_function():
    from server.sdlc import paths, prompts
    assert prompts.artifact_path is paths.artifact_path
    assert prompts.SDLC_DOCS_DIR == paths.SDLC_DOCS_DIR
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=$PWD/src /home/tony/GitHub/viberpit-deploy/.venv/bin/python -m pytest server/tests/test_sdlc_paths.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'server.sdlc.paths'`

- [ ] **Step 3: Create the module and re-export from prompts**

```python
# server/sdlc/paths.py
"""Committed-artifact layout (constitution VI: single source of truth).

Pure: no imports from server.db / server.automations / server.sessions, so the
stage engine's neighbours (``explain.py``) can import it and stay total.
"""
from __future__ import annotations

SDLC_DOCS_DIR = "docs/sdlc"


def artifact_path(slug: str, kind: str) -> str:
    """Repo-relative path of an initiative's committed artifact — the one place
    the intake/synthesizer loops, the prompt builders and the consequence
    explainer agree on layout."""
    return f"{SDLC_DOCS_DIR}/{slug}/{kind}.md"
```

In `server/sdlc/prompts.py`, replace lines 31–44 (the `SDLC_DOCS_DIR` constant, the `artifact_path` def and the `_artifact_path` alias) with:

```python
# The layout lives in the pure ``paths`` module; re-exported here because every
# existing caller imports it from ``prompts`` (constitution VI: one definition).
from server.sdlc.paths import SDLC_DOCS_DIR, artifact_path  # noqa: F401

# Internal alias kept for the builders below; ``artifact_path`` is the public name.
_artifact_path = artifact_path
```

- [ ] **Step 4: Run the new test and the existing prompt/artifact tests**

Run: `PYTHONPATH=$PWD/src /home/tony/GitHub/viberpit-deploy/.venv/bin/python -m pytest server/tests/test_sdlc_paths.py server/tests/test_sdlc_prompts.py server/tests/test_sdlc_synthesizer_loop.py server/tests/test_sdlc_intake_loop.py -q`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add server/sdlc/paths.py server/sdlc/prompts.py server/tests/test_sdlc_paths.py
printf 'refactor(sdlc): move artifact_path to a pure paths module\n\nThe consequence explainer needs the layout without importing prompt builders\n(which pull in session code). prompts re-exports it; no caller changes.\n\nCo-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>\n' > /tmp/c1.txt
git commit -F /tmp/c1.txt
```

---

### Task 2: The pure consequence explainer

**Files:**
- Create: `server/sdlc/explain.py`
- Test: `server/tests/test_sdlc_explain.py`

**Interfaces:**
- Consumes: `server.sdlc.engine` (`InitiativeView`, `ArtifactView`, action classes, `next_action`, `advance_stage`, `step_back_stage`, `STAGE_ARTIFACT`, `STAGE_SEQUENCE`), `server.sdlc.gates` (`GATES`, `GATE_BY_ID`, `RISK_TOLERANCES`, `auto_resolves`), `server.sdlc.paths.artifact_path`.
- Produces (used by Tasks 4, 5):
  - `ExplainContext(initiative, artifacts, mode, tolerance, configured_tolerance, autoresolve_enabled, has_children, proposal_pending, proposal_tasks, dispatch, queue_min_status, session_model, tick_seconds)`
  - `Step(text, spawns_session=False, writes_github=False)`
  - `Consequence(available, unavailable_reason, immediate, then, when, reversal, gate)`
  - `MachineOutlook(actor, when, summary, steps, because)`
  - `Guidance(machine, actions, tolerance, tick_seconds)`
  - `explain(ctx) -> Guidance`, `describe(action, ctx, view, artifacts, *, depth=0) -> tuple[Step, ...]`
  - `guidance_to_wire(g) -> dict`, `consequence_to_wire(c) -> dict`, `gates_to_wire(*, autoresolve_enabled) -> dict`
  - `dispatch_sentence(dispatch, queue_min_status) -> str` (lowercase start), `capitalize(s) -> str`
  - constants `WHEN_NOW="now"`, `WHEN_NEXT_TICK="next_tick"`, `WHEN_NEVER="never"`, `ACTOR_MACHINE="machine"`, `ACTOR_YOU="you"`, `ACTOR_NOBODY="nobody"`, `CONTROLS=("advance","step_back","unblock","materialize_accept","materialize_reject")`

- [ ] **Step 1: Write the failing tests**

```python
# server/tests/test_sdlc_explain.py
"""Consequence explainer (spec 2026-09-17 §3): every sentence the UI shows about a
control is the real engine run on the post-tap state, described. Pure and total."""
from __future__ import annotations

import importlib
import sys
from dataclasses import replace

import pytest

from server.sdlc.engine import ArtifactView, InitiativeView, next_action
from server.sdlc.explain import (
    ACTOR_MACHINE, ACTOR_NOBODY, ACTOR_YOU, CONTROLS, WHEN_NEVER, WHEN_NEXT_TICK, WHEN_NOW,
    ExplainContext, describe, dispatch_sentence, explain, gates_to_wire, guidance_to_wire,
)
from server.sdlc.gates import GATES, RISK_TOLERANCES


def _view(**kw) -> InitiativeView:
    base = dict(slug="widget", stage="design", status="active", repo="o/r", issue=None)
    base.update(kw)
    return InitiativeView(**base)  # type: ignore[arg-type]


def _art(kind: str, *, sha: str | None = None, pr: int | None = None) -> ArtifactView:
    return ArtifactView(kind=kind, commit_sha=sha, pr_number=pr)


def _ctx(view: InitiativeView, arts: list[ArtifactView] | None = None, **kw) -> ExplainContext:
    base = dict(initiative=view, artifacts=list(arts or []), mode="on", tolerance="cautious",
                configured_tolerance="cautious", autoresolve_enabled=True, has_children=False,
                proposal_pending=False, proposal_tasks=0, dispatch="off",
                queue_min_status="ready", session_model="auto", tick_seconds=60)
    base.update(kw)
    return ExplainContext(**base)  # type: ignore[arg-type]


def test_module_is_pure():
    for name in [m for m in sys.modules if m.startswith("server.")]:
        del sys.modules[name]
    importlib.import_module("server.sdlc.explain")
    assert not any(m.startswith(("server.db", "server.automations", "server.sessions"))
                   for m in sys.modules)


# --- the simulation property: `then` IS the engine on the post-tap state --------

@pytest.mark.parametrize("tolerance", RISK_TOLERANCES)
def test_advance_then_equals_engine_on_advanced_state(tolerance):
    view = _view(stage="design")
    arts = [_art("intent", sha="a"), _art("spec", sha="b")]
    ctx = _ctx(view, arts, tolerance=tolerance, configured_tolerance=tolerance)
    g = explain(ctx)
    after = replace(view, stage="plan", status="active")
    expected = describe(next_action(after, arts, "on", tolerance), ctx, after, arts)
    assert g.actions["advance"].then == expected
    assert g.actions["advance"].immediate[0].text == "Moves the gate to plan."


def test_advance_from_design_spawns_a_plan_session_and_names_the_file():
    view = _view(stage="design")
    arts = [_art("intent", sha="a"), _art("spec", sha="b")]
    g = explain(_ctx(view, arts, session_model="opus"))
    step = g.actions["advance"].then[0]
    assert step.spawns_session is True
    assert "plan session on opus" in step.text
    assert "docs/sdlc/widget/plan.md" in step.text
    assert "reads the merged spec" in step.text
    assert g.actions["advance"].when == WHEN_NEXT_TICK
    assert g.actions["advance"].reversal == "Step back"
    assert g.actions["advance"].gate == "stage.advance"


def test_step_back_then_equals_engine_on_previous_stage():
    view = _view(stage="plan")
    arts = [_art("intent", sha="a"), _art("spec", sha="b")]
    ctx = _ctx(view, arts)
    g = explain(ctx)
    after = replace(view, stage="design", status="active")
    assert g.actions["step_back"].then == describe(next_action(after, arts, "on", "cautious"), ctx, after, arts)
    # A merged spec at design under cautious: nothing spawns, the human re-advances.
    assert g.actions["step_back"].then[0].spawns_session is False
    assert "tap Advance to move to plan" in g.actions["step_back"].then[0].text
    assert g.actions["step_back"].when == WHEN_NEVER


def test_unblock_clears_the_block_and_then_describes_the_stage():
    view = _view(stage="design", status="blocked")
    ctx = _ctx(view, [])
    g = explain(ctx)
    c = g.actions["unblock"]
    assert c.available is True
    assert c.immediate[0].text == "Clears the block; the gate stays at design."
    after = replace(view, status="awaiting_human")
    assert c.then == describe(next_action(after, [], "on", "cautious"), ctx, after, [])
    assert c.then[0].spawns_session is True   # no spec yet → a spec session


def test_materialize_accept_writes_github_moves_to_build_and_hands_to_the_board():
    view = _view(stage="plan")
    arts = [_art("intent", sha="a"), _art("spec", sha="b"), _art("plan", sha="c")]
    g = explain(_ctx(view, arts, proposal_pending=True, proposal_tasks=3, dispatch="auto",
                     queue_min_status="ready"))
    c = g.actions["materialize_accept"]
    assert c.available is True
    assert c.when == WHEN_NOW
    assert c.immediate[0].writes_github is True
    assert "3 sub-issues" in c.immediate[0].text
    assert c.immediate[1].text == "Moves the gate to build."
    assert c.then[0].spawns_session is True
    assert "dispatch it to a coding session" in c.then[0].text
    assert c.reversal == "Close the created issues"
    assert c.gate == "materialization.accept"


def test_materialize_accept_singular_sub_issue():
    view = _view(stage="plan")
    arts = [_art("plan", sha="c")]
    g = explain(_ctx(view, arts, proposal_pending=True, proposal_tasks=1))
    assert "1 sub-issue " in g.actions["materialize_accept"].immediate[0].text


def test_materialize_reject_frees_a_new_decompose_session():
    view = _view(stage="plan")
    arts = [_art("plan", sha="c")]
    g = explain(_ctx(view, arts, proposal_pending=True, proposal_tasks=2))
    c = g.actions["materialize_reject"]
    assert c.immediate[0].text == "Rejects this decomposition; nothing is created on GitHub."
    assert c.then[0].spawns_session is True
    assert "decompose session" in c.then[0].text
    assert c.when == WHEN_NEXT_TICK


def test_materialize_controls_unavailable_without_a_pending_proposal():
    g = explain(_ctx(_view(stage="plan"), [_art("plan", sha="c")]))
    for k in ("materialize_accept", "materialize_reject"):
        assert g.actions[k].available is False
        assert g.actions[k].unavailable_reason == "No decomposition is waiting."


# --- availability mirrors the endpoints' 409 rules ------------------------------

def test_program_has_no_outlook_and_every_control_is_unavailable():
    g = explain(_ctx(_view(), [], has_children=True))
    assert g.machine is None
    assert set(g.actions) == set(CONTROLS)
    for c in g.actions.values():
        assert c.available is False
        assert "phases" in (c.unavailable_reason or "")


def test_blocked_row_disables_advance_and_step_back_but_offers_unblock():
    g = explain(_ctx(_view(stage="design", status="blocked"), []))
    assert g.actions["advance"].unavailable_reason == "Clear the block first."
    assert g.actions["step_back"].unavailable_reason == "Clear the block first."
    assert g.actions["unblock"].available is True
    assert g.machine.actor == ACTOR_YOU
    assert g.machine.summary == "Blocked: unblock or step back."


def test_unblock_waits_for_a_pending_materialization():
    g = explain(_ctx(_view(stage="plan", status="blocked"), [], proposal_pending=True, proposal_tasks=1))
    assert g.actions["unblock"].available is False
    assert g.actions["unblock"].unavailable_reason == "Accept or reject the pending materialization first."


def test_edges_of_the_track():
    first = explain(_ctx(_view(stage="intake"), []))
    assert first.actions["step_back"].unavailable_reason == "Already at the first stage."
    last = explain(_ctx(_view(stage="done"), []))
    assert last.actions["advance"].unavailable_reason == "Already at the final stage."
    assert last.machine.actor == ACTOR_NOBODY
    assert last.machine.summary == "Nothing left to do."


def test_unknown_stage_is_total():
    g = explain(_ctx(_view(stage="weird"), []))
    assert g.actions["advance"].available is False
    assert g.actions["step_back"].available is False
    assert g.machine is not None


# --- the machine outlook ---------------------------------------------------------

def test_outlook_under_balanced_says_the_machine_advances_then_spawns():
    view = _view(stage="design")
    arts = [_art("intent", sha="a"), _art("spec", sha="b")]
    g = explain(_ctx(view, arts, tolerance="balanced", configured_tolerance="balanced"))
    assert g.machine.actor == ACTOR_MACHINE
    assert g.machine.when == WHEN_NEXT_TICK
    assert g.machine.steps[0].text == "Advances to plan automatically."
    assert g.machine.steps[1].spawns_session is True      # one level of lookahead
    assert len(g.machine.steps) == 2
    assert "balanced" in g.machine.because
    assert "advance a stage once its artifact merged" in g.machine.because


def test_outlook_under_cautious_waits_for_you():
    view = _view(stage="design")
    arts = [_art("intent", sha="a"), _art("spec", sha="b")]
    g = explain(_ctx(view, arts))
    assert g.machine.actor == ACTOR_YOU
    assert g.machine.when == WHEN_NEVER
    assert g.machine.summary == "The spec is merged; tap Advance to move to plan."
    assert g.machine.because == "Risk tolerance cautious: every gate waits for you."


def test_open_pr_under_cautious_names_the_review():
    g = explain(_ctx(_view(stage="design"), [_art("spec", pr=12)]))
    assert g.machine.summary == "The spec PR is waiting for you to review and merge it; under cautious the loop does not merge it."


def test_open_pr_under_balanced_arms_auto_merge():
    g = explain(_ctx(_view(stage="design"), [_art("spec", pr=12)], tolerance="balanced",
                     configured_tolerance="balanced"))
    assert g.machine.actor == ACTOR_MACHINE
    assert g.machine.steps[0].writes_github is True
    assert "PR #12" in g.machine.steps[0].text


def test_kill_switch_off_is_said_out_loud():
    g = explain(_ctx(_view(stage="design"), [_art("spec", sha="b")], tolerance="cautious",
                     configured_tolerance="balanced", autoresolve_enabled=False))
    assert g.machine.because == ("Risk tolerance is balanced, but auto-resolve is switched off for "
                                 "this deployment, so every gate waits for you.")
    assert g.tolerance == {"effective": "cautious", "configured": "balanced",
                           "autoresolveEnabled": False, "synthesizerMode": "on"}


def test_mode_off_means_nobody_acts():
    g = explain(_ctx(_view(stage="design"), [], mode="off"))
    assert g.machine.actor == ACTOR_NOBODY
    assert g.machine.when == WHEN_NEVER
    assert "SDLC automation is off" in g.machine.summary
    assert g.actions["advance"].when == WHEN_NEVER


def test_build_stage_defers_to_the_board_whatever_the_issue_pointer():
    for issue in (None, "o/r#5"):
        g = explain(_ctx(_view(stage="build", issue=issue, epic="o/r#9"), [], dispatch="off"))
        assert g.machine.actor == ACTOR_YOU
        assert g.machine.summary.startswith("Nothing more from the roadmap loop.")
        assert "Dispatch is off" in g.machine.summary


def test_deploy_stage_is_carried_by_other_flows():
    g = explain(_ctx(_view(stage="deploy"), []))
    assert g.machine.summary == "Deploy and maintain are carried by the pipeline and control bands, not the roadmap loop."


def test_pending_proposal_is_your_move():
    g = explain(_ctx(_view(stage="plan"), [_art("plan", sha="c")], proposal_pending=True, proposal_tasks=2))
    assert g.machine.actor == ACTOR_YOU
    assert g.machine.summary == "A decomposition is waiting for your Accept or Reject."


# --- helpers and wire shapes -----------------------------------------------------

def test_dispatch_sentence():
    assert dispatch_sentence("off", "ready") == "nothing is dispatched: this product's Dispatch is off."
    assert dispatch_sentence("propose", "ready") == "the Backlog Runner may include it in a proposed batch; you approve each batch."
    assert dispatch_sentence("auto", "triaged") == ("the Backlog Runner may dispatch it to a coding session once it is "
                                                    "at or above 'triaged' on the board.")


def test_guidance_wire_shape():
    g = explain(_ctx(_view(stage="design"), [_art("spec", sha="b")]))
    w = guidance_to_wire(g)
    assert set(w) == {"machine", "actions", "tolerance", "tickSeconds"}
    assert set(w["machine"]) == {"actor", "when", "summary", "steps", "because"}
    a = w["actions"]["advance"]
    assert set(a) == {"available", "unavailableReason", "immediate", "then", "when", "reversal", "gate"}
    assert set(a["then"][0]) == {"text", "spawnsSession", "writesGithub"}
    assert w["tickSeconds"] == 60
    assert guidance_to_wire(explain(_ctx(_view(), [], has_children=True)))["machine"] is None


def test_gates_wire_lists_every_gate_with_auto_resolution_per_tolerance():
    w = gates_to_wire(autoresolve_enabled=True)
    assert [g["id"] for g in w["gates"]] == [g.id for g in GATES]
    assert w["tolerances"] == list(RISK_TOLERANCES)
    assert w["autoresolveEnabled"] is True
    by_id = {g["id"]: g for g in w["gates"]}
    assert by_id["stage.advance"]["autoResolvesUnder"] == {"cautious": False, "balanced": True, "bold": True}
    assert by_id["materialization.accept"]["autoResolvesUnder"] == {"cautious": False, "balanced": False, "bold": True}
    assert by_id["dispatch"]["autoResolvesUnder"] == {"cautious": False, "balanced": False, "bold": False}
    assert by_id["dispatch"]["risk"] is None
    assert by_id["deploy.production"]["autoResolvesUnder"]["bold"] is False
    assert set(by_id["stage.advance"]) == {"id", "label", "risk", "action", "reverse", "autoResolvesUnder"}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=$PWD/src /home/tony/GitHub/viberpit-deploy/.venv/bin/python -m pytest server/tests/test_sdlc_explain.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'server.sdlc.explain'`

- [ ] **Step 3: Write the module**

```python
# server/sdlc/explain.py
"""Consequence explainer (spec 2026-09-17-roadmap-consequence-guidance-design §3).

Pure: no I/O, no imports from server.db / server.automations / server.sessions.
Every sentence the UI shows about what a control does is computed here by running
the real stage engine on the hypothetical post-tap state and describing the
result — so the guidance cannot drift from the behaviour. ``describe`` is the ONE
place a ``StageAction`` becomes words.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace

from server.sdlc.engine import (
    STAGE_ARTIFACT,
    STAGE_SEQUENCE,
    Advance,
    ArtifactView,
    AwaitApproval,
    Done,
    HandoffToBuild,
    InitiativeView,
    Materialize,
    MergeArtifact,
    NeedsHuman,
    Propose,
    StageAction,
    advance_stage,
    next_action,
    step_back_stage,
)
from server.sdlc.gates import GATE_BY_ID, GATES, RISK_TOLERANCES, auto_resolves
from server.sdlc.paths import artifact_path

WHEN_NOW = "now"
WHEN_NEXT_TICK = "next_tick"
WHEN_NEVER = "never"

ACTOR_MACHINE = "machine"
ACTOR_YOU = "you"
ACTOR_NOBODY = "nobody"

#: Every human control the initiative page carries, in the order the wire lists them.
CONTROLS: tuple[str, ...] = ("advance", "step_back", "unblock", "materialize_accept", "materialize_reject")

_PROGRAM_REASON = "A program is driven by its phases; act on each phase instead."


# --------------------------------------------------------------------------- #
# Model                                                                        #
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class ExplainContext:
    initiative: InitiativeView
    artifacts: list[ArtifactView]
    mode: str                     # synthesizer automation mode: "on" | "off"
    tolerance: str                # EFFECTIVE (after the kill switch)
    configured_tolerance: str     # the policy value as stored
    autoresolve_enabled: bool
    has_children: bool
    proposal_pending: bool
    proposal_tasks: int
    dispatch: str                 # product policy `dispatch`: off | propose | auto
    queue_min_status: str         # product policy `queue_min_status`
    session_model: str            # product policy `default_model`
    tick_seconds: int


@dataclass(frozen=True)
class Step:
    text: str
    spawns_session: bool = False
    writes_github: bool = False


@dataclass(frozen=True)
class Consequence:
    available: bool
    unavailable_reason: str | None
    immediate: tuple[Step, ...]
    then: tuple[Step, ...]
    when: str
    reversal: str | None
    gate: str | None


@dataclass(frozen=True)
class MachineOutlook:
    actor: str
    when: str
    summary: str
    steps: tuple[Step, ...]
    because: str


@dataclass(frozen=True)
class Guidance:
    machine: MachineOutlook | None
    actions: dict[str, Consequence] = field(default_factory=dict)
    tolerance: dict = field(default_factory=dict)
    tick_seconds: int = 60


# --------------------------------------------------------------------------- #
# Words                                                                        #
# --------------------------------------------------------------------------- #

def capitalize(s: str) -> str:
    return s[:1].upper() + s[1:] if s else s


def dispatch_sentence(dispatch: str, queue_min_status: str) -> str:
    """THE phrasing for what the Backlog Runner does with a board item under the
    product's ``dispatch`` — shared by the build-stage outlook and the inbox.
    Lowercase start so a caller can continue a sentence with it."""
    if dispatch == "auto":
        return (f"the Backlog Runner may dispatch it to a coding session once it is "
                f"at or above '{queue_min_status}' on the board.")
    if dispatch == "propose":
        return "the Backlog Runner may include it in a proposed batch; you approve each batch."
    return "nothing is dispatched: this product's Dispatch is off."


def _kinds() -> list[str]:
    return list(STAGE_ARTIFACT.values())


def _upstream(kind: str) -> str | None:
    kinds = _kinds()
    i = kinds.index(kind) if kind in kinds else -1
    return kinds[i - 1] if i > 0 else None


def _artifact_of(artifacts: list[ArtifactView], kind: str) -> ArtifactView | None:
    for art in artifacts:
        if art.kind == kind:
            return art
    return None


def _next_of(stage: str) -> str | None:
    try:
        return advance_stage(stage)
    except ValueError:
        return None


def _board_step(ctx: ExplainContext) -> Step:
    return Step("Nothing more from the roadmap loop. The epic's sub-issues are on the board; "
                + dispatch_sentence(ctx.dispatch, ctx.queue_min_status),
                spawns_session=ctx.dispatch == "auto")


def _needs_human_step(reason: str, ctx: ExplainContext, view: InitiativeView) -> Step:
    if view.status == "blocked":
        return Step("Blocked: unblock or step back.")
    if ctx.mode == "off":
        return Step("SDLC automation is off: nothing moves until it is turned on from the "
                    "Delivery page's Automation tab.")
    if view.stage == "build":
        # The engine says NeedsHuman when no origin issue is set, but the loop takes
        # no action at build either way — the board does (spec §3.3).
        return _board_step(ctx)
    if view.stage in ("deploy", "maintain"):
        return Step("Deploy and maintain are carried by the pipeline and control bands, "
                    "not the roadmap loop.")
    return Step(reason or "Needs a human decision.")


def describe(action: StageAction, ctx: ExplainContext, view: InitiativeView,
             artifacts: list[ArtifactView], *, depth: int = 0) -> tuple[Step, ...]:
    """Turn one engine decision into the steps a human reads. ``depth`` bounds the
    lookahead after an automatic advance to ONE further decision."""
    if isinstance(action, Propose):
        up = _upstream(action.kind)
        reads = f" that reads the merged {up}" if up else ""
        return (Step(f"Spawns a {action.kind} session on {ctx.session_model}{reads} and opens "
                     f"a PR adding {artifact_path(view.slug, action.kind)}.",
                     spawns_session=True),)
    if isinstance(action, AwaitApproval):
        art = _artifact_of(artifacts, action.kind)
        if art is not None and art.commit_sha is None:
            return (Step(f"The {action.kind} PR is waiting for you to review and merge it; "
                         f"under {ctx.tolerance} the loop does not merge it."),)
        return (Step(f"The {action.kind} is merged; tap Advance to move to {_next_of(view.stage)}."),)
    if isinstance(action, MergeArtifact):
        return (Step(f"Arms GitHub auto-merge on the {action.kind} PR #{action.pr_number}; "
                     "CI stays the gate.", writes_github=True),)
    if isinstance(action, Advance):
        steps = [Step(f"Advances to {action.to_stage} automatically.")]
        if depth == 0:
            after = replace(view, stage=action.to_stage, status="active")
            steps.extend(describe(next_action(after, artifacts, ctx.mode, ctx.tolerance),
                                  ctx, after, artifacts, depth=1))
        return tuple(steps)
    if isinstance(action, Materialize):
        if ctx.proposal_pending:
            return (Step("A decomposition is waiting for your Accept or Reject."),)
        return (Step("Spawns a decompose session that proposes an epic and sub-issues for you "
                     "to accept or reject.", spawns_session=True),)
    if isinstance(action, HandoffToBuild):
        return (_board_step(ctx),)
    if isinstance(action, Done):
        return (Step("Nothing left to do."),)
    if isinstance(action, NeedsHuman):
        return (_needs_human_step(action.reason, ctx, view),)
    return (Step("No next action."),)  # pragma: no cover — union is closed


def _loop_acts(action: StageAction, ctx: ExplainContext) -> bool:
    """Whether the machine (synthesizer or Backlog Runner) executes *action* without
    a human — the one rule behind ``when`` and ``actor``."""
    if ctx.mode == "off":
        return False
    if isinstance(action, (Propose, MergeArtifact, Advance)):
        return True
    if isinstance(action, Materialize):
        return not ctx.proposal_pending
    if isinstance(action, HandoffToBuild):
        return ctx.dispatch == "auto"
    if isinstance(action, NeedsHuman) and ctx.initiative.stage == "build" \
            and ctx.initiative.status != "blocked":
        return ctx.dispatch == "auto"
    return False


def _because(ctx: ExplainContext) -> str:
    if ctx.mode == "off":
        return "SDLC automation is off, so nothing moves until it is turned on."
    if ctx.configured_tolerance != ctx.tolerance:
        return (f"Risk tolerance is {ctx.configured_tolerance}, but auto-resolve is switched off "
                "for this deployment, so every gate waits for you.")
    covered = [g.label.lower() for g in GATES if auto_resolves(g.id, ctx.tolerance)]
    if not covered:
        return f"Risk tolerance {ctx.tolerance}: every gate waits for you."
    return (f"Risk tolerance {ctx.tolerance}: the machine resolves these gates itself: "
            + "; ".join(covered) + ".")


# --------------------------------------------------------------------------- #
# Controls                                                                     #
# --------------------------------------------------------------------------- #

def _unavailable(reason: str, gate: str | None = None) -> Consequence:
    return Consequence(False, reason, (), (), WHEN_NEVER, None, gate)


def _then(ctx: ExplainContext, after: InitiativeView) -> tuple[tuple[Step, ...], str]:
    action = next_action(after, ctx.artifacts, ctx.mode, ctx.tolerance)
    steps = describe(action, ctx, after, ctx.artifacts)
    return steps, (WHEN_NEXT_TICK if _loop_acts(action, replace(ctx, initiative=after)) else WHEN_NEVER)


def _advance(ctx: ExplainContext) -> Consequence:
    gate = "stage.advance"
    view = ctx.initiative
    if view.status == "blocked":
        return _unavailable("Clear the block first.", gate)
    if view.stage == "done":
        return _unavailable("Already at the final stage.", gate)
    nxt = _next_of(view.stage)
    if nxt is None:
        return _unavailable("Unknown stage.", gate)
    after = replace(view, stage=nxt, status="active")
    then, when = _then(ctx, after)
    return Consequence(True, None, (Step(f"Moves the gate to {nxt}."),), then, when,
                       GATE_BY_ID[gate].reverse, gate)


def _step_back(ctx: ExplainContext) -> Consequence:
    view = ctx.initiative
    if view.status == "blocked":
        return _unavailable("Clear the block first.")
    if view.stage not in STAGE_SEQUENCE:
        return _unavailable("Unknown stage.")
    prev = step_back_stage(view.stage)
    if prev is None:
        return _unavailable("Already at the first stage.")
    after = replace(view, stage=prev, status="active")
    then, when = _then(ctx, after)
    return Consequence(True, None, (Step(f"Moves the gate back to {prev}."),), then, when,
                       "Advance", None)


def _unblock(ctx: ExplainContext) -> Consequence:
    gate = "initiative.blocked"
    view = ctx.initiative
    if view.status != "blocked":
        return _unavailable("Not blocked.", gate)
    if ctx.proposal_pending:
        return _unavailable("Accept or reject the pending materialization first.", gate)
    after = replace(view, status="awaiting_human")
    then, when = _then(ctx, after)
    return Consequence(True, None, (Step(f"Clears the block; the gate stays at {view.stage}."),),
                       then, when, None, gate)


def _materialize_accept(ctx: ExplainContext) -> Consequence:
    gate = "materialization.accept"
    if not ctx.proposal_pending:
        return _unavailable("No decomposition is waiting.", gate)
    view = ctx.initiative
    n = ctx.proposal_tasks
    after = replace(view, stage="build", status="active", epic=view.epic or "pending")
    then, when = _then(ctx, after)
    immediate = (
        Step(f"Creates the epic and {n} sub-issue{'' if n == 1 else 's'} on GitHub; this cannot "
             "be undone here.", writes_github=True),
        Step("Moves the gate to build."),
        Step("The sub-issues land on the board already classified."),
    )
    return Consequence(True, None, immediate, then, WHEN_NOW, GATE_BY_ID[gate].reverse, gate)


def _materialize_reject(ctx: ExplainContext) -> Consequence:
    if not ctx.proposal_pending:
        return _unavailable("No decomposition is waiting.")
    freed = replace(ctx, proposal_pending=False)
    then, when = _then(freed, ctx.initiative)
    return Consequence(True, None,
                       (Step("Rejects this decomposition; nothing is created on GitHub."),),
                       then, when, None, None)


def _outlook(ctx: ExplainContext) -> MachineOutlook:
    view = ctx.initiative
    action = next_action(view, ctx.artifacts, ctx.mode, ctx.tolerance)
    steps = describe(action, ctx, view, ctx.artifacts)
    if isinstance(action, Done) or ctx.mode == "off":
        actor, when = ACTOR_NOBODY, WHEN_NEVER
    elif _loop_acts(action, ctx):
        actor, when = ACTOR_MACHINE, WHEN_NEXT_TICK
    else:
        actor, when = ACTOR_YOU, WHEN_NEVER
    return MachineOutlook(actor, when, steps[0].text, steps, _because(ctx))


def explain(ctx: ExplainContext) -> Guidance:
    """The whole guidance payload for one initiative. Total: an unknown stage or
    status degrades to unavailable controls and an honest outlook, never a raise."""
    tolerance = {"effective": ctx.tolerance, "configured": ctx.configured_tolerance,
                 "autoresolveEnabled": ctx.autoresolve_enabled, "synthesizerMode": ctx.mode}
    if ctx.has_children:
        return Guidance(None, {c: _unavailable(_PROGRAM_REASON) for c in CONTROLS},
                        tolerance, ctx.tick_seconds)
    actions = {
        "advance": _advance(ctx),
        "step_back": _step_back(ctx),
        "unblock": _unblock(ctx),
        "materialize_accept": _materialize_accept(ctx),
        "materialize_reject": _materialize_reject(ctx),
    }
    return Guidance(_outlook(ctx), actions, tolerance, ctx.tick_seconds)


# --------------------------------------------------------------------------- #
# Wire                                                                         #
# --------------------------------------------------------------------------- #

def step_to_wire(s: Step) -> dict:
    return {"text": s.text, "spawnsSession": s.spawns_session, "writesGithub": s.writes_github}


def consequence_to_wire(c: Consequence) -> dict:
    return {"available": c.available, "unavailableReason": c.unavailable_reason,
            "immediate": [step_to_wire(s) for s in c.immediate],
            "then": [step_to_wire(s) for s in c.then],
            "when": c.when, "reversal": c.reversal, "gate": c.gate}


def guidance_to_wire(g: Guidance) -> dict:
    machine = None if g.machine is None else {
        "actor": g.machine.actor, "when": g.machine.when, "summary": g.machine.summary,
        "steps": [step_to_wire(s) for s in g.machine.steps], "because": g.machine.because}
    return {"machine": machine,
            "actions": {k: consequence_to_wire(v) for k, v in g.actions.items()},
            "tolerance": dict(g.tolerance), "tickSeconds": g.tick_seconds}


def gates_to_wire(*, autoresolve_enabled: bool) -> dict:
    """The registry for ``GET /api/sdlc/gates``: every gate with whether each
    tolerance level resolves it (spec D5)."""
    return {"gates": [{"id": g.id, "label": g.label, "risk": g.risk, "action": g.action,
                       "reverse": g.reverse,
                       "autoResolvesUnder": {t: auto_resolves(g.id, t) for t in RISK_TOLERANCES}}
                      for g in GATES],
            "tolerances": list(RISK_TOLERANCES),
            "autoresolveEnabled": autoresolve_enabled}
```

- [ ] **Step 4: Run the tests**

Run: `PYTHONPATH=$PWD/src /home/tony/GitHub/viberpit-deploy/.venv/bin/python -m pytest server/tests/test_sdlc_explain.py -v`
Expected: all PASS. If `test_build_stage_defers_to_the_board_whatever_the_issue_pointer` fails on the `issue="o/r#5"` leg, the engine returned `HandoffToBuild` and `_loop_acts` must still say `False` under `dispatch="off"` — that is the implemented rule; re-read `_loop_acts`.

- [ ] **Step 5: Lint and type-check the module**

Run: `/home/tony/GitHub/viberpit-deploy/.venv/bin/python -m ruff check server/sdlc/explain.py server/tests/test_sdlc_explain.py && /home/tony/GitHub/viberpit-deploy/.venv/bin/python -m mypy server/sdlc/explain.py --ignore-missing-imports`
Expected: clean

- [ ] **Step 6: Commit**

```bash
git add server/sdlc/explain.py server/tests/test_sdlc_explain.py
printf 'feat(sdlc): pure consequence explainer over the stage engine\n\nRuns next_action on the hypothetical post-tap state for every initiative\ncontrol and describes the result: what changes now, what the loop does next,\nwhether that spawns a session or writes to GitHub, and the reverse (spec\n2026-09-17 s3). One describe() is the only place a StageAction becomes words.\n\nCo-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>\n' > /tmp/c2.txt
git commit -F /tmp/c2.txt
```

---

### Task 3: One reader for the synthesizer mode and one tick constant

**Files:**
- Modify: `server/automations/sdlc_synthesizer.py` (after `fold_synth_mode`, ~line 52)
- Modify: `server/sessions/orchestrator.py:2136-2138` and `:8729-8738`
- Test: `server/tests/test_sdlc_synth_mode_reader.py`

**Interfaces:**
- Produces: `server.automations.sdlc_synthesizer.TICK_INTERVAL_S: float = 60.0` and `async read_synth_mode() -> tuple[str, dict]` (mode folded through `fold_synth_mode`).

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_sdlc_synth_mode_reader.py
"""The API and the loop read the synthesizer's mode through ONE function, so the
guidance a page shows and the tick that acts cannot fold a legacy mode differently."""
from __future__ import annotations

import asyncio
import tempfile


def _read(seed) -> tuple[str, dict]:
    from server.automations.sdlc_synthesizer import read_synth_mode
    from server.db import db_session, init_db

    async def _run(db_url: str):
        await init_db(db_url)
        async with db_session() as db:
            for row in seed():
                db.add(row)
        return await read_synth_mode()

    with tempfile.TemporaryDirectory() as tmp:
        return asyncio.run(_run(f"sqlite+aiosqlite:///{tmp}/test.db"))


def test_absent_row_is_off():
    mode, config = _read(lambda: [])
    assert mode == "off"
    assert isinstance(config, dict)


def test_legacy_propose_folds_to_on():
    from server.db.models import Automation
    mode, _ = _read(lambda: [Automation(id="sdlc-synthesizer", enabled=True, mode="propose", config={})])
    assert mode == "on"


def test_disabled_row_is_off_even_with_a_mode():
    from server.db.models import Automation
    mode, _ = _read(lambda: [Automation(id="sdlc-synthesizer", enabled=False, mode="on", config={})])
    assert mode == "off"


def test_tick_constant_is_the_loop_interval():
    from server.automations.sdlc_synthesizer import TICK_INTERVAL_S
    assert TICK_INTERVAL_S == 60.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=$PWD/src /home/tony/GitHub/viberpit-deploy/.venv/bin/python -m pytest server/tests/test_sdlc_synth_mode_reader.py -q`
Expected: FAIL with `ImportError: cannot import name 'read_synth_mode'`

- [ ] **Step 3: Add the reader and the constant; delegate from the orchestrator**

In `server/automations/sdlc_synthesizer.py`, directly after `fold_synth_mode` (line ~52) add:

```python
#: The loop's tick interval; the orchestrator schedules with it and the API
#: reports it as ``tickSeconds`` so no copy hardcodes "a minute" (spec 2026-09-17 D6).
TICK_INTERVAL_S: float = 60.0


async def read_synth_mode() -> tuple[str, dict]:
    """The sdlc-synthesizer row's ``(mode, config)`` merged onto schema defaults, a
    pre-fold ``propose``/``auto`` read as ``on`` (spec 2026-09-14 D8). ``off`` + pure
    defaults on any absent or invalid row so a bad config never spawns. THE reader:
    the orchestrator's loop seam and the SDLC API both call this."""
    from server.automations.config_store import read_automation_config
    mode, config = await read_automation_config(
        SdlcSynthesizer, modes=("on", "propose", "auto"), log_key="sdlc_synthesizer")
    return fold_synth_mode(mode), config
```

`SdlcSynthesizer` is defined later in the same module (line ~122); the function body only references it at call time, so the forward reference is fine.

In `server/sessions/orchestrator.py`, replace the body of `_sdlc_synth_read_config` (lines 8729–8738) with:

```python
    async def _sdlc_synth_read_config(self) -> tuple[str, dict]:
        """Loop seam → ``sdlc_synthesizer.read_synth_mode`` (the ONE reader)."""
        from server.automations.sdlc_synthesizer import read_synth_mode
        return await read_synth_mode()
```

and change line 2137 from `interval=60.0` to use the constant:

```python
            from server.automations.sdlc_synthesizer import TICK_INTERVAL_S
            self._sdlc_synth_task = asyncio.create_task(
                self._sdlc_synth_tick_loop(interval=TICK_INTERVAL_S), name="sdlc-synthesizer-loop"
            )
```

(Keep the import local to the block, matching how the orchestrator imports `SdlcSynthesizerLoop` lazily at line 1204.)

- [ ] **Step 4: Run the new test plus the loop/wiring tests**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST -u CCSWITCH_DAEMON_ROLE CCSWITCH_DEV=1 PYTHONPATH=$PWD/src /home/tony/GitHub/viberpit-deploy/.venv/bin/python -m pytest server/tests/test_sdlc_synth_mode_reader.py server/tests/test_sdlc_synthesizer_loop.py server/tests/test_sdlc_wiring.py -q`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add server/automations/sdlc_synthesizer.py server/sessions/orchestrator.py server/tests/test_sdlc_synth_mode_reader.py
printf 'refactor(sdlc): one reader for the synthesizer mode and a named tick constant\n\nread_synth_mode() is shared by the loop seam and (next) the SDLC API so both\nfold legacy modes identically; TICK_INTERVAL_S replaces the 60.0 literal.\n\nCo-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>\n' > /tmp/c3.txt
git commit -F /tmp/c3.txt
```

---

### Task 4: `guidance` on the initiative detail, real tolerance, and `GET /api/sdlc/gates`

**Files:**
- Modify: `server/api/sdlc.py` (imports at top; `get_initiative` at 424–497; new route after it)
- Test: `server/tests/test_sdlc_api.py` (append)

**Interfaces:**
- Consumes: Task 2's `ExplainContext`, `explain`, `guidance_to_wire`, `gates_to_wire`; Task 3's `read_synth_mode`, `TICK_INTERVAL_S`; `server.sdlc.gates.effective_tolerance`; `server.automations.product_policy.effective_policy`; `server.config.get_settings`.
- Produces: detail JSON keys `guidance` (Task 2 wire shape) and `nextAction` under effective tolerance; `GET /api/sdlc/gates` returning `gates_to_wire(...)`. Module-level seam `_autoresolve_enabled() -> bool` for tests.

- [ ] **Step 1: Write the failing tests** (append to `server/tests/test_sdlc_api.py`)

```python
# ── Consequence guidance (spec 2026-09-17) ───────────────────────────────────

def _seed_guidance_case(db_url: str, *, tolerance: str = "cautious", synth_on: bool = True,
                        stage: str = "design", program: bool = False):
    """One product (`acme`, policy risk_tolerance=tolerance), one initiative at
    *stage* with a merged intent and a merged spec, the synthesizer row on/off."""
    from server.db import db_session, init_db
    from server.db.models import Automation, Initiative, InitiativeArtifact, Project

    async def _seed():
        await init_db(db_url)
        async with db_session() as db:
            p = Project(slug="acme", name="Acme", automation_policy={
                "risk_tolerance": tolerance, "dispatch": "auto", "default_model": "opus"})
            db.add(p)
            await db.flush()
            ini = Initiative(slug="dark-mode", repo="o/r", title="Dark mode", stage=stage,
                             status="active", project_id=p.id)
            db.add(ini)
            await db.flush()
            db.add(InitiativeArtifact(initiative_id=ini.id, kind="intent",
                                      repo_path="docs/sdlc/dark-mode/intent.md", commit_sha="a"))
            db.add(InitiativeArtifact(initiative_id=ini.id, kind="spec",
                                      repo_path="docs/sdlc/dark-mode/spec.md", commit_sha="b"))
            if program:
                db.add(Initiative(slug="phase-1", repo="o/r", title="Phase 1", stage="intake",
                                  status="active", parent_initiative_id=ini.id))
            if synth_on:
                db.add(Automation(id="sdlc-synthesizer", enabled=True, mode="on", config={}))
    asyncio.run(_seed())


def test_gates_endpoint_exposes_the_registry():
    with _client() as c:
        r = c.get("/api/sdlc/gates", headers=IDENTITY_HEADERS)
        assert r.status_code == 200, r.text
        body = r.json()
        ids = [g["id"] for g in body["gates"]]
        assert "stage.advance" in ids and "deploy.production" in ids
        adv = next(g for g in body["gates"] if g["id"] == "stage.advance")
        assert adv["autoResolvesUnder"] == {"cautious": False, "balanced": True, "bold": True}
        assert adv["reverse"] == "Step back"
        assert body["tolerances"] == ["cautious", "balanced", "bold"]
        assert body["autoresolveEnabled"] is True


def test_gates_endpoint_requires_identity():
    with _client() as c:
        assert c.get("/api/sdlc/gates").status_code == 401


def test_detail_guidance_under_cautious_says_advance_spawns_a_plan_session():
    with tempfile.TemporaryDirectory() as tmp:
        db_url = f"sqlite+aiosqlite:///{tmp}/test.db"
        _seed_guidance_case(db_url)
        with _client(db_url) as c:
            body = c.get("/api/sdlc/initiatives/dark-mode", headers=IDENTITY_HEADERS).json()
            assert body["nextAction"] == {"type": "await_approval", "kind": "spec"}
            g = body["guidance"]
            assert g["tickSeconds"] == 60
            assert g["tolerance"] == {"effective": "cautious", "configured": "cautious",
                                      "autoresolveEnabled": True, "synthesizerMode": "on"}
            assert g["machine"]["actor"] == "you"
            adv = g["actions"]["advance"]
            assert adv["available"] is True
            assert adv["immediate"][0]["text"] == "Moves the gate to plan."
            assert adv["then"][0]["spawnsSession"] is True
            assert "plan session on opus" in adv["then"][0]["text"]
            assert adv["when"] == "next_tick"
            assert g["actions"]["materialize_accept"]["available"] is False


def test_detail_next_action_follows_the_products_tolerance():
    """The old code hardcoded cautious; under balanced the engine advances."""
    with tempfile.TemporaryDirectory() as tmp:
        db_url = f"sqlite+aiosqlite:///{tmp}/test.db"
        _seed_guidance_case(db_url, tolerance="balanced")
        with _client(db_url) as c:
            body = c.get("/api/sdlc/initiatives/dark-mode", headers=IDENTITY_HEADERS).json()
            assert body["nextAction"] == {"type": "advance", "toStage": "plan"}
            m = body["guidance"]["machine"]
            assert m["actor"] == "machine"
            assert m["steps"][0]["text"] == "Advances to plan automatically."
            assert m["steps"][1]["spawnsSession"] is True


def test_detail_guidance_names_the_kill_switch(monkeypatch):
    import server.api.sdlc as sdlc_api
    monkeypatch.setattr(sdlc_api, "_autoresolve_enabled", lambda: False)
    with tempfile.TemporaryDirectory() as tmp:
        db_url = f"sqlite+aiosqlite:///{tmp}/test.db"
        _seed_guidance_case(db_url, tolerance="balanced")
        with _client(db_url) as c:
            body = c.get("/api/sdlc/initiatives/dark-mode", headers=IDENTITY_HEADERS).json()
            assert body["nextAction"] == {"type": "await_approval", "kind": "spec"}
            g = body["guidance"]
            assert g["tolerance"]["effective"] == "cautious"
            assert g["tolerance"]["configured"] == "balanced"
            assert g["tolerance"]["autoresolveEnabled"] is False
            assert "switched off" in g["machine"]["because"]
            gates = c.get("/api/sdlc/gates", headers=IDENTITY_HEADERS).json()
            assert gates["autoresolveEnabled"] is False


def test_detail_guidance_when_the_synthesizer_is_off():
    with tempfile.TemporaryDirectory() as tmp:
        db_url = f"sqlite+aiosqlite:///{tmp}/test.db"
        _seed_guidance_case(db_url, synth_on=False)
        with _client(db_url) as c:
            body = c.get("/api/sdlc/initiatives/dark-mode", headers=IDENTITY_HEADERS).json()
            g = body["guidance"]
            assert g["tolerance"]["synthesizerMode"] == "off"
            assert g["machine"]["actor"] == "nobody"
            assert g["actions"]["advance"]["when"] == "never"
            assert "SDLC automation is off" in g["actions"]["advance"]["then"][0]["text"]


def test_program_guidance_has_no_outlook_and_no_available_control():
    with tempfile.TemporaryDirectory() as tmp:
        db_url = f"sqlite+aiosqlite:///{tmp}/test.db"
        _seed_guidance_case(db_url, program=True)
        with _client(db_url) as c:
            body = c.get("/api/sdlc/initiatives/dark-mode", headers=IDENTITY_HEADERS).json()
            assert body["nextAction"] is None
            assert body["guidance"]["machine"] is None
            assert all(not a["available"] for a in body["guidance"]["actions"].values())


def test_detail_guidance_with_a_pending_materialization():
    from server.db import db_session
    from server.db.models import Initiative, InitiativeArtifact, MaterializationProposal

    with tempfile.TemporaryDirectory() as tmp:
        db_url = f"sqlite+aiosqlite:///{tmp}/test.db"
        _seed_guidance_case(db_url, stage="plan")

        async def _more():
            async with db_session() as db:
                ini = (await db.execute(select(Initiative).where(Initiative.slug == "dark-mode"))).scalar_one()
                db.add(InitiativeArtifact(initiative_id=ini.id, kind="plan",
                                          repo_path="docs/sdlc/dark-mode/plan.md", commit_sha="c"))
                db.add(MaterializationProposal(initiative_id=ini.id, status="pending", payload={
                    "epic_title": "Dark mode", "tasks": [{"title": "a"}, {"title": "b"}]}))
        asyncio.run(_more())
        with _client(db_url) as c:
            body = c.get("/api/sdlc/initiatives/dark-mode", headers=IDENTITY_HEADERS).json()
            acc = body["guidance"]["actions"]["materialize_accept"]
            assert acc["available"] is True
            assert "2 sub-issues" in acc["immediate"][0]["text"]
            assert acc["immediate"][0]["writesGithub"] is True
            assert body["guidance"]["machine"]["summary"] == "A decomposition is waiting for your Accept or Reject."
```

`select` is needed at module scope: add `from sqlalchemy import select` next to the existing imports at the top of the test file if it is not already imported (check with `grep -n "^from sqlalchemy import select" server/tests/test_sdlc_api.py`).

- [ ] **Step 2: Run tests to verify they fail**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST -u CCSWITCH_DAEMON_ROLE CCSWITCH_DEV=1 PYTHONPATH=$PWD/src /home/tony/GitHub/viberpit-deploy/.venv/bin/python -m pytest server/tests/test_sdlc_api.py -k "guidance or gates_endpoint or follows_the_products_tolerance" -q`
Expected: FAIL (404 on `/api/sdlc/gates`, `KeyError: 'guidance'`)

- [ ] **Step 3: Implement**

Add to the imports of `server/api/sdlc.py` (after the `server.sdlc.engine` block):

```python
from server.automations.product_policy import effective_policy
from server.config import get_settings
from server.sdlc.explain import ExplainContext, explain, gates_to_wire, guidance_to_wire
from server.sdlc.gates import effective_tolerance
```

Add, just below `router = APIRouter(...)`:

```python
def _autoresolve_enabled() -> bool:
    """The process kill switch (spec 2026-09-14 D9), as one seam so a test can flip
    it without re-reading settings."""
    return bool(get_settings().gate_autoresolve_enabled)
```

Replace the body of `get_initiative` from the `# Build nextAction via the pure engine` comment (line 445) through the end of the function with:

```python
    # The product's policy decides the tolerance the loop will act under (spec
    # 2026-09-17 D3): nextAction and the guidance are computed under the SAME
    # effective value the synthesizer uses, never a hardcoded cautious.
    project = (await db.execute(select(Project).where(Project.id == ini.project_id))
               ).scalar_one_or_none() if ini.project_id else None
    policy = effective_policy(project.automation_policy if project else None)
    enabled = _autoresolve_enabled()
    tolerance = effective_tolerance(policy.get("risk_tolerance"), enabled=enabled)
    from server.automations.sdlc_synthesizer import TICK_INTERVAL_S, read_synth_mode
    mode, _config = await read_synth_mode()
    progress = (await _progress_rolled_up(db, [ini], include_children=True))[ini.id]

    state = InitiativeView(
        slug=ini.slug or "",
        stage=ini.stage,
        status=ini.status,
        repo=ini.repo or "",
        issue=ini.origin_ref,
        # Same fold as the synthesizer's projection: an epic pointer means the
        # plan has already been materialized (spec 003 §7b). NOT the wire `epic`
        # key below, which is the epic *progress* object.
        epic=f"{ini.epic_repo}#{ini.epic_number}" if ini.epic_number else None,
        epic_total=int((progress or {}).get("total") or 0),
        epic_done=int((progress or {}).get("done") or 0),
    )
    artifact_views = [
        ArtifactView(kind=a.kind or "", commit_sha=a.commit_sha, pr_number=a.pr_number, pr_url=a.pr_url)
        for a in artifacts
    ]
    # Display-only "on": nextAction is the human's next step even while the
    # synthesizer row is off; the guidance below carries the real mode.
    action = next_action(state, artifact_views, "on", tolerance)

    # Build-stage correlation (#801): find backlog-runner sessions with same origin_ref
    build_sessions: list[dict] = []
    if ini.origin_ref:
        session_rows = (
            await db.execute(
                select(DbSession).where(
                    DbSession.origin == "backlog-runner",
                    DbSession.origin_ref == ini.origin_ref,
                )
            )
        ).scalars().all()
        build_sessions = [
            {"id": s.id, "status": s.status, "branch": s.branch}
            for s in session_rows
        ]

    pending = (await db.execute(
        select(MaterializationProposal)
        .where(MaterializationProposal.initiative_id == ini.id,
               MaterializationProposal.status == "pending"))).scalars().first()
    proposal_tasks = len(((pending.payload or {}).get("tasks") or [])) if pending else 0

    result = _initiative_to_dict(ini)
    result["artifacts"] = [_artifact_to_dict(a) for a in artifacts]
    result["buildSessions"] = build_sessions
    result["nextAction"] = _action_to_dict(action)
    result["epic"] = progress
    result["priority"] = (result["epic"] or {}).get("priority")
    await _decorate(db, [ini], [result])
    # A program (an initiative with phases) is never driven directly — advance
    # and step-back both refuse it (409 has_children above), so its nextAction
    # is meaningless and must not point at a stage transition nobody can take.
    if result["hasChildren"]:
        result["nextAction"] = None
    result["guidance"] = guidance_to_wire(explain(ExplainContext(
        initiative=state, artifacts=artifact_views, mode=mode, tolerance=tolerance,
        configured_tolerance=str(policy.get("risk_tolerance") or "cautious"),
        autoresolve_enabled=enabled, has_children=bool(result["hasChildren"]),
        proposal_pending=pending is not None, proposal_tasks=proposal_tasks,
        dispatch=str(policy.get("dispatch") or "off"),
        queue_min_status=str(policy.get("queue_min_status") or "ready"),
        session_model=str(policy.get("default_model") or "auto"),
        tick_seconds=int(TICK_INTERVAL_S))))
    return result


@router.get("/gates")
async def list_gates(_identity: str = Depends(current_identity)) -> dict:
    """The gate registry (server/sdlc/gates.py) with, per gate, whether each risk
    tolerance resolves it — what the product automation card's gate table and the
    consequence copy are built from (spec 2026-09-17 D5)."""
    return gates_to_wire(autoresolve_enabled=_autoresolve_enabled())
```

Route ordering: `/gates` is a fixed path; FastAPI matches `/initiatives/{slug}` only under `/initiatives/…`, so there is no collision. Keep `list_gates` right after `get_initiative`.

- [ ] **Step 4: Run the SDLC API suite**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST -u CCSWITCH_DAEMON_ROLE CCSWITCH_DEV=1 PYTHONPATH=$PWD/src /home/tony/GitHub/viberpit-deploy/.venv/bin/python -m pytest server/tests/test_sdlc_api.py -q 2>&1 | tee /tmp/sdlc-api.log | tail -5`
Expected: all PASS, including the pre-existing `test_get_detail_includes_chain_and_action` (intake, no product → cautious → `await_approval`) and `test_detail_next_action_materialize_depends_on_the_epic_pointer`.

- [ ] **Step 5: Lint + mypy**

Run: `/home/tony/GitHub/viberpit-deploy/.venv/bin/python -m ruff check server/ && /home/tony/GitHub/viberpit-deploy/.venv/bin/python -m mypy server/api/sdlc.py --ignore-missing-imports`
Expected: clean

- [ ] **Step 6: Commit**

```bash
git add server/api/sdlc.py server/tests/test_sdlc_api.py
printf 'feat(sdlc): guidance on the initiative detail and GET /api/sdlc/gates\n\nnextAction now runs under the product'"'"'s effective risk tolerance instead of a\nhardcoded cautious; the detail carries a computed guidance payload (what each\ncontrol does now and next, cost flags, kill-switch and mode state); the gate\nregistry gets a read endpoint (spec 2026-09-17 s4).\n\nCo-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>\n' > /tmp/c4.txt
git commit -F /tmp/c4.txt
```

---

### Task 5: Triage proposal guidance

**Files:**
- Create: `server/pm/explain.py`
- Modify: `server/api/pm.py` (`_triage_to_dict` at 1078; `list_triage` at 1107–1176)
- Test: `server/tests/test_pm_explain.py`, `server/tests/test_pm_api.py` (append)

**Interfaces:**
- Consumes: Task 2's `Consequence`, `Step`, `WHEN_*`, `consequence_to_wire`, `dispatch_sentence`, `capitalize`; `server.sdlc.gates.GATE_BY_ID`; `server.automations.product_policy.load_policies`; `server.pm.score.rice_score`.
- Produces: `server.pm.explain.triage_guidance(*, feature_slug: str | None, score: float | None, policy: dict) -> dict` returning `{"accept": <Consequence wire>, "reject": <Consequence wire>}`; each `GET /api/pm/triage` row gains `guidance`.

- [ ] **Step 1: Write the failing tests**

```python
# server/tests/test_pm_explain.py
"""Inbox consequence guidance (spec 2026-09-17 §4.3): what Accept and Reject do to
one proposal under the owning product's dispatch policy. Pure."""
from server.automations.product_policy import effective_policy
from server.pm.explain import triage_guidance


def test_accept_under_auto_dispatch_says_a_session_may_follow():
    g = triage_guidance(feature_slug="sessions", score=12.5,
                        policy=effective_policy({"dispatch": "auto", "queue_min_status": "ready"}))
    a = g["accept"]
    assert a["available"] is True
    assert a["immediate"][0]["text"] == "Files it under sessions with score 12.5; it leaves the inbox."
    assert a["then"][0]["spawnsSession"] is True
    assert "dispatch it to a coding session" in a["then"][0]["text"]
    assert a["then"][0]["text"][0].isupper()
    assert a["when"] == "next_tick"
    assert a["reversal"] == "Undo within the window"
    assert a["gate"] == "triage.accept"


def test_accept_under_off_dispatch_dispatches_nothing():
    g = triage_guidance(feature_slug="sessions", score=None, policy=effective_policy(None))
    a = g["accept"]
    assert a["immediate"][0]["text"] == "Files it under sessions; it leaves the inbox."
    assert a["then"][0]["spawnsSession"] is False
    assert "Dispatch is off" in a["then"][0]["text"]
    assert a["when"] == "never"


def test_accept_under_propose_names_the_batch_approval():
    g = triage_guidance(feature_slug="sessions", score=3.0, policy=effective_policy({"dispatch": "propose"}))
    assert "proposed batch" in g["accept"]["then"][0]["text"]


def test_no_fit_accept_says_so():
    g = triage_guidance(feature_slug=None, score=None, policy=effective_policy(None))
    assert g["accept"]["immediate"][0]["text"] == "Files it with no capability (a no-fit accept); it leaves the inbox."


def test_reject_is_immediate_and_dispatches_nothing():
    r = triage_guidance(feature_slug="x", score=None, policy=effective_policy(None))["reject"]
    assert r["immediate"][0]["text"] == "Marks it needs-human; it leaves the inbox and nothing is dispatched."
    assert r["then"] == []
    assert r["when"] == "now"
```

Append to `server/tests/test_pm_api.py` (it already has `_client`, `_register`, `IDENTITY_HEADERS`, `db_session`, `select`, `Project`, `Feature`, `WorkItem`, `TriageProposal` in scope — verify with `grep -n "^from\|^import" server/tests/test_pm_api.py | head -30` and add any missing import at the top):

```python
def test_triage_rows_carry_guidance_from_the_products_dispatch_policy():
    with _client() as c:
        _register(c, "Viberpit", repo="o/r")

        async def _seed():
            async with db_session() as db:
                p = (await db.execute(select(Project).where(Project.slug == "viberpit"))).scalar_one()
                p.automation_policy = {"dispatch": "auto", "queue_min_status": "ready"}
                f = Feature(project_id=p.id, slug="sessions", title="Sessions", state="live")
                w = WorkItem(repo="o/r", number=1, kind="issue", title="a", state="open",
                             triage_state="proposed")
                db.add_all([f, w])
                await db.flush()
                db.add(TriageProposal(work_item_id=w.id, proposed_feature_id=f.id,
                                      reach=3, impact=2, effort=1, confidence=2,
                                      rationale="fits", confidence_level="high", status="pending"))
        c.portal.call(_seed)

        row = c.get("/api/pm/triage", headers=IDENTITY_HEADERS).json()[0]
        g = row["guidance"]
        assert g["accept"]["immediate"][0]["text"] == "Files it under sessions with score 12; it leaves the inbox."
        assert g["accept"]["then"][0]["spawnsSession"] is True
        assert "coding session" in g["accept"]["then"][0]["text"]
        assert g["reject"]["when"] == "now"


def test_triage_guidance_for_an_unowned_repo_reads_as_dispatch_off():
    """No product owns the repo → defaults → nothing is dispatched."""
    with _client() as c:
        async def _seed():
            async with db_session() as db:
                w = WorkItem(repo="nobody/owns", number=1, kind="issue", title="a", state="open",
                             triage_state="proposed")
                db.add(w)
                await db.flush()
                db.add(TriageProposal(work_item_id=w.id, no_fit_suggestion="billing",
                                      rationale="no fit", confidence_level="low", status="pending"))
        c.portal.call(_seed)
        row = c.get("/api/pm/triage", headers=IDENTITY_HEADERS).json()[0]
        assert row["guidance"]["accept"]["immediate"][0]["text"].startswith("Files it with no capability")
        assert "Dispatch is off" in row["guidance"]["accept"]["then"][0]["text"]
```

(`rice_score(3, 2, 1, 2)` is `12.0`, formatted with `:g` → `12`.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST -u CCSWITCH_DAEMON_ROLE CCSWITCH_DEV=1 PYTHONPATH=$PWD/src /home/tony/GitHub/viberpit-deploy/.venv/bin/python -m pytest server/tests/test_pm_explain.py server/tests/test_pm_api.py -k "guidance" -q`
Expected: FAIL (`ModuleNotFoundError: server.pm.explain`, `KeyError: 'guidance'`)

- [ ] **Step 3: Implement**

```python
# server/pm/explain.py
"""Inbox consequence guidance (spec 2026-09-17 §4.3): what Accept and Reject do to
a PM triage proposal, under the owning product's dispatch policy.

Pure: no I/O, no DB imports. Shares the dispatch phrasing with the SDLC
explainer so the build-stage outlook and the inbox say the same sentence.
"""
from __future__ import annotations

from server.sdlc.explain import (
    WHEN_NEVER, WHEN_NEXT_TICK, WHEN_NOW, Consequence, Step, capitalize, consequence_to_wire,
    dispatch_sentence,
)
from server.sdlc.gates import GATE_BY_ID


def triage_guidance(*, feature_slug: str | None, score: float | None, policy: dict) -> dict:
    """``{"accept": …, "reject": …}`` (Consequence wire shape) for one proposal.
    *policy* is the product's EFFECTIVE policy (``effective_policy``), so the
    dispatch and minimum-status keys are always present."""
    dispatch = str(policy.get("dispatch") or "off")
    min_status = str(policy.get("queue_min_status") or "ready")
    home = f"under {feature_slug}" if feature_slug else "with no capability (a no-fit accept)"
    scored = f" with score {score:g}" if score is not None else ""
    accept = Consequence(
        True, None,
        (Step(f"Files it {home}{scored}; it leaves the inbox."),),
        (Step(capitalize(dispatch_sentence(dispatch, min_status)), spawns_session=dispatch == "auto"),),
        WHEN_NEXT_TICK if dispatch == "auto" else WHEN_NEVER,
        GATE_BY_ID["triage.accept"].reverse, "triage.accept")
    reject = Consequence(
        True, None,
        (Step("Marks it needs-human; it leaves the inbox and nothing is dispatched."),),
        (), WHEN_NOW, None, None)
    return {"accept": consequence_to_wire(accept), "reject": consequence_to_wire(reject)}
```

In `server/api/pm.py`:

1. Add to the `from server.automations.product_policy import (...)` block (line 20–21): `load_policies`. Add `from server.pm.explain import triage_guidance` beside the other `server.pm` imports (alphabetically after `server.pm.desired_state`). `rice_score` is already available if `from server.pm.score import ... rice_score` exists at line 57's block; if only `RICE_FIELDS, validate_rice` are imported, add `rice_score` to that import.
2. Give `_triage_to_dict` a `guidance: dict | None = None` keyword and emit it:

```python
def _triage_to_dict(prop: TriageProposal, wi: WorkItem | None,
                    feat_slug: str | None, project_slug: str | None,
                    guidance: dict | None = None) -> dict:
    return {
        ...existing keys unchanged...
        "resolvedAt": prop.resolved_at.isoformat() if prop.resolved_at else None,
        # What Accept / Reject would do under the owning product's dispatch policy
        # (spec 2026-09-17 §4.3); None only where a caller has no policy to hand.
        "guidance": guidance,
    }
```

3. In `list_triage`, after `slug_by_repo` is built (line ~1153), load policies once:

```python
    policies = await load_policies(db)
```

and in the output loop replace the `out.append(...)` with:

```python
        slug = slug_by_repo.get(wi.repo) if wi and wi.repo else None
        out.append(_triage_to_dict(
            p, wi, feat_slugs.get(p.proposed_feature_id), slug,
            guidance=triage_guidance(
                feature_slug=feat_slugs.get(p.proposed_feature_id),
                score=rice_score(p.reach, p.impact, p.effort, p.confidence),
                policy=policies.get(slug) if slug else effective_policy(None))))
```

- [ ] **Step 4: Run the tests**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST -u CCSWITCH_DAEMON_ROLE CCSWITCH_DEV=1 PYTHONPATH=$PWD/src /home/tony/GitHub/viberpit-deploy/.venv/bin/python -m pytest server/tests/test_pm_explain.py server/tests/test_pm_api.py server/tests/test_assistant_tools.py -q 2>&1 | tee /tmp/pm-api.log | tail -5`
Expected: all PASS (assistant tools list triage rows and must tolerate the extra key).

- [ ] **Step 5: Lint + mypy, then commit**

Run: `/home/tony/GitHub/viberpit-deploy/.venv/bin/python -m ruff check server/ && /home/tony/GitHub/viberpit-deploy/.venv/bin/python -m mypy server/pm/explain.py server/api/pm.py --ignore-missing-imports`

```bash
git add server/pm/explain.py server/api/pm.py server/tests/test_pm_explain.py server/tests/test_pm_api.py
printf 'feat(pm): accept/reject guidance on triage proposals\n\nEach inbox row says what Accept files it as and whether the Backlog Runner\nwill then dispatch it, from the owning product'"'"'s effective dispatch policy;\nthe dispatch phrasing is shared with the SDLC build-stage outlook.\n\nCo-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>\n' > /tmp/c5.txt
git commit -F /tmp/c5.txt
```

---

### Task 6: Web types, fetcher and hook

**Files:**
- Modify: `web/src/api/sdlc.ts` (after `InitiativeNextAction`, ~line 79; `Initiative` at 81–112; add `fetchGates` after `fetchInitiative`)
- Modify: `web/src/api/triage.ts` (`TriageProposal` at 12–39)
- Modify: `web/src/api/hooks.ts` (`QK` at 68; the `./sdlc` import at 18–32; a hook after `useInitiative` ~1822)
- Test: `web/src/api/__tests__/sdlcGuidance.test.ts`

**Interfaces:**
- Produces: types `GuidanceStep`, `Consequence`, `MachineOutlook`, `GuidanceControl`, `InitiativeGuidance`, `GateSpec`, `GateRegistry`; `fetchGates(): Promise<GateRegistry>`; `Initiative.guidance?: InitiativeGuidance | null`; `TriageProposal.guidance?: { accept: Consequence; reject: Consequence } | null`; `QK.gates`; `useSdlcGates()`.

- [ ] **Step 1: Write the failing test**

```ts
// web/src/api/__tests__/sdlcGuidance.test.ts
import { describe, it, expect, vi } from 'vitest'

const get = vi.fn()
vi.mock('../client', () => ({
  api: { get: (...a: unknown[]) => get(...a) },
  apiErrorCode: () => null,
  ApiRequestError: class extends Error {},
}))

describe('sdlc guidance fetchers', () => {
  it('fetchGates reads the registry endpoint', async () => {
    const { fetchGates } = await import('../sdlc')
    get.mockResolvedValueOnce({ gates: [], tolerances: ['cautious'], autoresolveEnabled: true })
    const r = await fetchGates()
    expect(get).toHaveBeenCalledWith('/sdlc/gates')
    expect(r.tolerances).toEqual(['cautious'])
  })

  it('the query key for gates lives under sdlc', async () => {
    const { QK } = await import('../hooks')
    expect(QK.gates).toEqual(['sdlc', 'gates'])
  })
})
```

- [ ] **Step 2: Run to verify it fails**

Run: `(cd web && npx vitest run src/api/__tests__/sdlcGuidance.test.ts)`
Expected: FAIL (`fetchGates` is not a function / `QK.gates` undefined)

- [ ] **Step 3: Add the types, fetcher and hook**

In `web/src/api/sdlc.ts`, after the `InitiativeNextAction` interface:

```ts
/** One thing a tap (or the loop) does. The two flags are the cost signals the
 *  UI badges (spec 2026-09-17 D4). */
export interface GuidanceStep {
  text: string
  spawnsSession: boolean
  writesGithub: boolean
}

/** When a consequence's `then` steps happen: on the synthesizer's next tick,
 *  right now (a GitHub write), or not without a human. */
export type GuidanceWhen = 'now' | 'next_tick' | 'never' | string

/** What ONE human control does — computed server-side by running the real stage
 *  engine on the post-tap state (`server/sdlc/explain.py`). */
export interface Consequence {
  available: boolean
  unavailableReason: string | null
  immediate: GuidanceStep[]
  then: GuidanceStep[]
  when: GuidanceWhen
  reversal: string | null
  gate: string | null
}

/** What happens with NO human action. */
export interface MachineOutlook {
  actor: 'machine' | 'you' | 'nobody' | string
  when: GuidanceWhen
  summary: string
  steps: GuidanceStep[]
  because: string
}

export type GuidanceControl = 'advance' | 'step_back' | 'unblock' | 'materialize_accept' | 'materialize_reject'

export interface InitiativeGuidance {
  /** Null for a program (it has no gate of its own). */
  machine: MachineOutlook | null
  actions: Record<GuidanceControl, Consequence>
  tolerance: { effective: string; configured: string; autoresolveEnabled: boolean; synthesizerMode: string }
  tickSeconds: number
}

/** One row of the gate registry (`server/sdlc/gates.py`). `risk: null` marks a
 *  gate owned by its own policy knob (dispatch, triage auto-accept). */
export interface GateSpec {
  id: string
  label: string
  risk: 'low' | 'medium' | 'high' | null
  action: string
  reverse: string
  autoResolvesUnder: Record<string, boolean>
}

export interface GateRegistry {
  gates: GateSpec[]
  tolerances: string[]
  autoresolveEnabled: boolean
}
```

Add to the `Initiative` interface, after `nextAction?: InitiativeNextAction`:

```ts
  /** Detail endpoint only; absent from a server that predates it. */
  guidance?: InitiativeGuidance | null
```

Add after `fetchInitiative`:

```ts
export function fetchGates(): Promise<GateRegistry> {
  return api.get<GateRegistry>('/sdlc/gates')
}
```

In `web/src/api/triage.ts`, add at the top `import type { Consequence } from './sdlc'` and to `TriageProposal`, after `resolvedAt`:

```ts
  /** What Accept / Reject do under the owning product's dispatch policy
   *  (spec 2026-09-17 §4.3); absent from a server that predates it. */
  guidance?: { accept: Consequence; reject: Consequence } | null
```

In `web/src/api/hooks.ts`:
- add `fetchGates, type GateRegistry,` to the `from './sdlc'` import list;
- add to `QK` (next to `incidents`): `gates: ['sdlc', 'gates'] as const,`;
- add after `useInitiative`:

```ts
/** The gate registry with per-tolerance auto-resolution (`GET /sdlc/gates`).
 *  `staleTime: Infinity`: it changes only with a deploy. */
export function useSdlcGates() {
  return useQuery<GateRegistry>({
    queryKey: QK.gates,
    queryFn: fetchGates,
    staleTime: Infinity,
  })
}
```

- [ ] **Step 4: Run the test and the typecheck**

Run: `(cd web && npx vitest run src/api/__tests__/sdlcGuidance.test.ts && npm run typecheck)`
Expected: PASS, typecheck clean

- [ ] **Step 5: Commit**

```bash
git add web/src/api/sdlc.ts web/src/api/triage.ts web/src/api/hooks.ts web/src/api/__tests__/sdlcGuidance.test.ts
printf 'feat(web): guidance and gate-registry types, fetcher and hook\n\nCo-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>\n' > /tmp/c6.txt
git commit -F /tmp/c6.txt
```

---

### Task 7: `ConsequenceList` and `WhatHappensNext`

**Files:**
- Create: `web/src/features/roadmap/guidance/ConsequenceList.tsx`, `web/src/features/roadmap/guidance/WhatHappensNext.tsx`
- Test: `web/src/features/roadmap/guidance/ConsequenceList.test.tsx`

**Interfaces:**
- Produces: `ConsequenceList({ consequence, tickSeconds?, heading? })`, `StepRow({ step })`, `whenLabel(when, tickSeconds?) => string | null`, `WhatHappensNext({ guidance })`.

- [ ] **Step 1: Write the failing test**

```tsx
// web/src/features/roadmap/guidance/ConsequenceList.test.tsx
// THE renderer for a consequence: now-steps, then-steps with the tick-derived
// timing, the two cost badges, and the reverse line. Nothing else formats one.
import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import type { Consequence, InitiativeGuidance } from '@/api/sdlc'
import { ConsequenceList, whenLabel } from './ConsequenceList'
import { WhatHappensNext } from './WhatHappensNext'

const advance: Consequence = {
  available: true, unavailableReason: null,
  immediate: [{ text: 'Moves the gate to plan.', spawnsSession: false, writesGithub: false }],
  then: [{ text: 'Spawns a plan session on opus.', spawnsSession: true, writesGithub: false }],
  when: 'next_tick', reversal: 'Step back', gate: 'stage.advance',
}

describe('whenLabel', () => {
  it('derives the timing from the tick, never a literal', () => {
    expect(whenLabel('next_tick', 60)).toBe('in about a minute')
    expect(whenLabel('next_tick', 300)).toBe('in about 5 minutes')
    expect(whenLabel('next_tick')).toBe('on the next automation run')
    expect(whenLabel('now', 60)).toBeNull()
    expect(whenLabel('never', 60)).toBeNull()
  })
})

describe('ConsequenceList', () => {
  it('renders now and then with the cost badge and the reverse', () => {
    render(<ConsequenceList consequence={advance} tickSeconds={60} />)
    expect(screen.getByText('Now')).toBeInTheDocument()
    expect(screen.getByText('Moves the gate to plan.')).toBeInTheDocument()
    expect(screen.getByText('Then · in about a minute')).toBeInTheDocument()
    expect(screen.getByText('Spawns a plan session on opus.')).toBeInTheDocument()
    expect(screen.getByText('starts a session')).toBeInTheDocument()
    expect(screen.queryByText('writes to GitHub')).not.toBeInTheDocument()
    expect(screen.getByText('Reverse: Step back')).toBeInTheDocument()
  })

  it('shows the GitHub badge and an optional heading', () => {
    const accept: Consequence = { ...advance, when: 'now', reversal: null,
      immediate: [{ text: 'Creates the epic.', spawnsSession: false, writesGithub: true }], then: [] }
    render(<ConsequenceList consequence={accept} heading="Accept" />)
    expect(screen.getByText('Accept')).toBeInTheDocument()
    expect(screen.getByText('writes to GitHub')).toBeInTheDocument()
    expect(screen.queryByText(/^Then/)).not.toBeInTheDocument()
    expect(screen.queryByText(/^Reverse/)).not.toBeInTheDocument()
  })

  it('says why an unavailable control is unavailable', () => {
    render(<ConsequenceList consequence={{ ...advance, available: false, unavailableReason: 'Clear the block first.' }} />)
    expect(screen.getByText('Clear the block first.')).toBeInTheDocument()
  })
})

describe('WhatHappensNext', () => {
  const guidance: InitiativeGuidance = {
    machine: {
      actor: 'machine', when: 'next_tick', summary: 'Advances to plan automatically.',
      steps: [
        { text: 'Advances to plan automatically.', spawnsSession: false, writesGithub: false },
        { text: 'Spawns a plan session on opus.', spawnsSession: true, writesGithub: false },
      ],
      because: 'Risk tolerance balanced: the machine resolves these gates itself: advance a stage once its artifact merged.',
    },
    actions: { advance, step_back: advance, unblock: advance, materialize_accept: advance, materialize_reject: advance },
    tolerance: { effective: 'balanced', configured: 'balanced', autoresolveEnabled: true, synthesizerMode: 'on' },
    tickSeconds: 60,
  }

  it('names the actor, the timing, the steps and the reason', () => {
    render(<WhatHappensNext guidance={guidance} />)
    expect(screen.getByRole('heading', { name: 'What happens next' })).toBeInTheDocument()
    expect(screen.getByText('Automatic')).toBeInTheDocument()
    expect(screen.getByText('in about a minute')).toBeInTheDocument()
    expect(screen.getByText('Spawns a plan session on opus.')).toBeInTheDocument()
    expect(screen.getByText(/Risk tolerance balanced/)).toBeInTheDocument()
  })

  it('labels your move and nothing-to-do, and renders nothing for a program', () => {
    const you = { ...guidance, machine: { ...guidance.machine!, actor: 'you', when: 'never' } }
    const { unmount } = render(<WhatHappensNext guidance={you} />)
    expect(screen.getByText('Your move')).toBeInTheDocument()
    expect(screen.queryByText(/about a minute/)).not.toBeInTheDocument()
    unmount()
    render(<WhatHappensNext guidance={{ ...guidance, machine: { ...guidance.machine!, actor: 'nobody' } }} />)
    expect(screen.getByText('Nothing to do')).toBeInTheDocument()
    const { container } = render(<WhatHappensNext guidance={{ ...guidance, machine: null }} />)
    expect(container).toBeEmptyDOMElement()
  })
})
```

- [ ] **Step 2: Run to verify it fails**

Run: `(cd web && npx vitest run src/features/roadmap/guidance/ConsequenceList.test.tsx)`
Expected: FAIL (module not found)

- [ ] **Step 3: Write the components**

```tsx
// web/src/features/roadmap/guidance/ConsequenceList.tsx
/**
 * THE renderer for a server-computed consequence (spec 2026-09-17 §5): what a
 * tap changes now, what the loop does afterwards and when, the two cost badges
 * (a session starts / GitHub is written) and the reverse action. Every dialog and
 * the inbox pane render through this; nothing else formats a consequence.
 */
import type { Consequence, GuidanceStep, GuidanceWhen } from '@/api/sdlc'
import { Badge } from '@/ui/Badge'

/** "in about a minute" is derived from the tick the server reports, never a
 *  literal (spec D6). Without a tick (the inbox: the Backlog Runner's cadence is
 *  not the synthesizer's), say only that it is the next run. */
export function whenLabel(when: GuidanceWhen, tickSeconds?: number): string | null {
  if (when !== 'next_tick') return null
  if (tickSeconds === undefined) return 'on the next automation run'
  if (tickSeconds < 90) return 'in about a minute'
  return `in about ${Math.round(tickSeconds / 60)} minutes`
}

export function StepRow({ step }: { step: GuidanceStep }) {
  return (
    <li className="flex flex-wrap items-center gap-1.5">
      <span>{step.text}</span>
      {step.spawnsSession && <Badge tone="warning">starts a session</Badge>}
      {step.writesGithub && <Badge tone="info">writes to GitHub</Badge>}
    </li>
  )
}

function Steps({ label, steps }: { label: string; steps: GuidanceStep[] }) {
  if (steps.length === 0) return null
  return (
    <div className="flex flex-col gap-1">
      <p className="text-xs font-medium uppercase text-ink-muted">{label}</p>
      <ul className="flex flex-col gap-1">
        {steps.map((s, i) => <StepRow key={i} step={s} />)}
      </ul>
    </div>
  )
}

export function ConsequenceList({ consequence, tickSeconds, heading }: {
  consequence: Consequence
  tickSeconds?: number
  heading?: string
}) {
  const later = whenLabel(consequence.when, tickSeconds)
  return (
    <div className="flex flex-col gap-2 text-sm text-ink">
      {heading && <p className="font-medium">{heading}</p>}
      {!consequence.available && consequence.unavailableReason && (
        <p className="text-ink-muted">{consequence.unavailableReason}</p>
      )}
      <Steps label="Now" steps={consequence.immediate} />
      <Steps label={later ? `Then · ${later}` : 'Then'} steps={consequence.then} />
      {consequence.reversal && <p className="text-xs text-ink-muted">Reverse: {consequence.reversal}</p>}
    </div>
  )
}
```

```tsx
// web/src/features/roadmap/guidance/WhatHappensNext.tsx
/**
 * The Track tab's outlook card: what happens to this initiative with NO human
 * action — who acts (the machine, you, nobody), when, the steps, and the
 * tolerance / kill-switch reason. Replaces the one-line `nextActionLabel`.
 */
import type { InitiativeGuidance } from '@/api/sdlc'
import { Badge, type Tone } from '@/ui/Badge'
import { StepRow, whenLabel } from './ConsequenceList'

const ACTOR: Record<string, { label: string; tone: Tone }> = {
  machine: { label: 'Automatic', tone: 'info' },
  you: { label: 'Your move', tone: 'warning' },
  nobody: { label: 'Nothing to do', tone: 'neutral' },
}

export function WhatHappensNext({ guidance }: { guidance: InitiativeGuidance }) {
  const m = guidance.machine
  if (!m) return null
  const actor = ACTOR[m.actor] ?? ACTOR.nobody!
  const later = whenLabel(m.when, guidance.tickSeconds)
  return (
    <section aria-labelledby="what-next-heading" className="flex flex-col gap-2 rounded-md border border-edge bg-surface p-3">
      <div className="flex flex-wrap items-center gap-2">
        <h2 id="what-next-heading" className="text-sm font-semibold text-ink">What happens next</h2>
        <Badge tone={actor.tone}>{actor.label}</Badge>
        {later && <span className="text-xs text-ink-muted">{later}</span>}
      </div>
      <ul className="flex flex-col gap-1 text-sm text-ink">
        {m.steps.map((s, i) => <StepRow key={i} step={s} />)}
      </ul>
      <p className="text-xs text-ink-muted">{m.because}</p>
    </section>
  )
}
```

- [ ] **Step 4: Run the test, lint and typecheck**

Run: `(cd web && npx vitest run src/features/roadmap/guidance && npm run lint && npm run typecheck)`
Expected: PASS, clean. (`Badge` is imported from `@/ui/Badge` with `tone` + children exactly as `InitiativePage.tsx:472` uses it.)

- [ ] **Step 5: Commit**

```bash
git add web/src/features/roadmap/guidance/
printf 'feat(web): ConsequenceList and WhatHappensNext guidance components\n\nCo-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>\n' > /tmp/c7.txt
git commit -F /tmp/c7.txt
```

---

### Task 8: Initiative page — dialogs, tooltips, outlook card; purge `NEXT_ACTION_LABEL`

**Files:**
- Modify: `web/src/features/roadmap/InitiativePage.tsx` (imports 10–42; `MaterializationCard` 113–220; `TrackTab` 222–253; header buttons 505–562; dialogs 637–706)
- Modify: `web/src/features/products/vocab.ts:136-149` (delete `NEXT_ACTION_LABEL`, `nextActionLabel`)
- Modify: `web/src/features/products/vocab.test.ts:9-10,46,58-67`
- Test: `web/src/features/roadmap/InitiativePage.test.tsx`, `web/src/features/roadmap/InitiativePage.materialization.test.tsx`

**Interfaces:**
- Consumes: Task 6 types on `Initiative.guidance`; Task 7 components.

- [ ] **Step 1: Write the failing tests**

Append inside the `describe('InitiativePage', …)` block of `InitiativePage.test.tsx`:

```tsx
  const guidance = {
    machine: {
      actor: 'you', when: 'never', summary: 'The spec is merged; tap Advance to move to plan.',
      steps: [{ text: 'The spec is merged; tap Advance to move to plan.', spawnsSession: false, writesGithub: false }],
      because: 'Risk tolerance cautious: every gate waits for you.',
    },
    actions: {
      advance: {
        available: true, unavailableReason: null,
        immediate: [{ text: 'Moves the gate to deploy.', spawnsSession: false, writesGithub: false }],
        then: [{ text: 'Deploy and maintain are carried by the pipeline and control bands, not the roadmap loop.', spawnsSession: false, writesGithub: false }],
        when: 'never', reversal: 'Step back', gate: 'stage.advance',
      },
      step_back: {
        available: true, unavailableReason: null,
        immediate: [{ text: 'Moves the gate back to plan.', spawnsSession: false, writesGithub: false }],
        then: [{ text: 'Spawns a decompose session that proposes an epic and sub-issues for you to accept or reject.', spawnsSession: true, writesGithub: false }],
        when: 'next_tick', reversal: 'Advance', gate: null,
      },
      unblock: { available: false, unavailableReason: 'Not blocked.', immediate: [], then: [], when: 'never', reversal: null, gate: 'initiative.blocked' },
      materialize_accept: { available: false, unavailableReason: 'No decomposition is waiting.', immediate: [], then: [], when: 'never', reversal: null, gate: 'materialization.accept' },
      materialize_reject: { available: false, unavailableReason: 'No decomposition is waiting.', immediate: [], then: [], when: 'never', reversal: null, gate: null },
    },
    tolerance: { effective: 'cautious', configured: 'cautious', autoresolveEnabled: true, synthesizerMode: 'on' },
    tickSeconds: 60,
  }

  it('the Track tab shows the outlook card from the guidance', () => {
    h.state.data = { ...h.initiative, guidance }
    mount()
    expect(screen.getByRole('heading', { name: 'What happens next' })).toBeInTheDocument()
    expect(screen.getByText('Your move')).toBeInTheDocument()
    expect(screen.getByText(/Risk tolerance cautious/)).toBeInTheDocument()
  })

  it('the Advance confirm lists the computed consequence instead of the static line', () => {
    h.state.data = { ...h.initiative, guidance }
    mount()
    fireEvent.click(screen.getByRole('button', { name: /Advance/ }))
    expect(screen.getByText('Moves the gate to deploy.')).toBeInTheDocument()
    expect(screen.getByText('Reverse: Step back')).toBeInTheDocument()
    expect(screen.queryByText(/This moves the human gate/)).not.toBeInTheDocument()
  })

  it('the Step back confirm says a session will start, and when', () => {
    h.state.data = { ...h.initiative, guidance }
    mount()
    fireEvent.click(screen.getByRole('button', { name: 'Step back' }))
    expect(screen.getByText('starts a session')).toBeInTheDocument()
    expect(screen.getByText('Then · in about a minute')).toBeInTheDocument()
  })

  it('a disabled control carries the server reason as its tooltip', () => {
    h.state.data = {
      ...h.initiative, status: 'blocked',
      guidance: { ...guidance, actions: { ...guidance.actions,
        advance: { ...guidance.actions.advance, available: false, unavailableReason: 'Clear the block first.' },
        unblock: { ...guidance.actions.unblock, available: true, unavailableReason: null,
          immediate: [{ text: 'Clears the block; the gate stays at build.', spawnsSession: false, writesGithub: false }] } } },
    }
    mount()
    expect(screen.getByRole('button', { name: /Advance/ })).toHaveAttribute('title', 'Clear the block first.')
    fireEvent.click(screen.getByRole('button', { name: 'Unblock' }))
    expect(screen.getByText('Clears the block; the gate stays at build.')).toBeInTheDocument()
  })

  it('without guidance the confirms fall back to their static copy and the Track tab shows no card', () => {
    h.state.data = { ...h.initiative, guidance: null }
    mount()
    expect(screen.queryByRole('heading', { name: 'What happens next' })).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /Advance/ }))
    expect(screen.getByText(/This moves the human gate/)).toBeInTheDocument()
  })
```

The existing test at line 139, `'shows the breadcrumb, the reason, the next action and the artifact link at its SHA'`, asserts the `nextActionLabel` sentence (`Ready to hand off to build.`). Change that assertion to check the fixture's next action is no longer rendered as a bare line: replace the `expect(screen.getByText('Ready to hand off to build.'))…` line with `expect(screen.queryByText('Ready to hand off to build.')).not.toBeInTheDocument()` (read the test body first to find the exact line).

In `InitiativePage.materialization.test.tsx`, find the `describe` block's hoisted initiative fixture and the test that opens the Materialize confirm (search for `'Confirm materialize'`). Add one test beside it:

```tsx
  it('the Materialize confirm lists the computed consequence when guidance is present', () => {
    const accept = {
      available: true, unavailableReason: null,
      immediate: [
        { text: 'Creates the epic and 2 sub-issues on GitHub; this cannot be undone here.', spawnsSession: false, writesGithub: true },
        { text: 'Moves the gate to build.', spawnsSession: false, writesGithub: false },
      ],
      then: [{ text: 'Nothing more from the roadmap loop.', spawnsSession: false, writesGithub: false }],
      when: 'now', reversal: 'Close the created issues', gate: 'materialization.accept',
    }
    const reject = { ...accept, immediate: [{ text: 'Rejects this decomposition; nothing is created on GitHub.', spawnsSession: false, writesGithub: false }],
      then: [{ text: 'Spawns a decompose session that proposes an epic and sub-issues for you to accept or reject.', spawnsSession: true, writesGithub: false }],
      when: 'next_tick', reversal: null, gate: null }
    h.state.data = { ...h.initiative, guidance: { machine: null, tickSeconds: 60,
      tolerance: { effective: 'cautious', configured: 'cautious', autoresolveEnabled: true, synthesizerMode: 'on' },
      actions: { advance: accept, step_back: accept, unblock: accept, materialize_accept: accept, materialize_reject: reject } } }
    mount()
    expect(screen.getByText(/Reject: Spawns a decompose session/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Accept' }))
    expect(screen.getByText('writes to GitHub')).toBeInTheDocument()
    expect(screen.getByText('Reverse: Close the created issues')).toBeInTheDocument()
  })
```

(Adapt `h.state.data`/`h.initiative`/`mount` to that file's own hoisted names — read its first 80 lines; the pattern matches `InitiativePage.test.tsx`.)

In `vocab.test.ts`: remove `NEXT_ACTION_LABEL` and `nextActionLabel` from the import (lines 9–10), remove `...Object.values(NEXT_ACTION_LABEL)` from the array at line 46, and delete the whole `it('says every next action as a sentence…')` block (lines 58–67).

- [ ] **Step 2: Run to verify the new tests fail**

Run: `(cd web && npx vitest run src/features/roadmap/InitiativePage.test.tsx src/features/roadmap/InitiativePage.materialization.test.tsx)`
Expected: the five new tests FAIL; existing ones pass.

- [ ] **Step 3: Implement the page changes**

Imports in `InitiativePage.tsx`: add `type InitiativeGuidance` to the `@/api/sdlc` import; remove `nextActionLabel` from the vocab import (line 34); add:

```tsx
import { ConsequenceList } from './guidance/ConsequenceList'
import { WhatHappensNext } from './guidance/WhatHappensNext'
```

`MaterializationCard`: change the signature to `function MaterializationCard({ slug, stage, guidance }: { slug: string; stage: string | null; guidance: InitiativeGuidance | null })`. Under the buttons `div` (after line 187) add the one-tap reject's consequence as a line:

```tsx
        {guidance?.actions.materialize_reject.then[0] && (
          <p className="text-xs text-ink-muted">Reject: {guidance.actions.materialize_reject.then[0].text}</p>
        )}
```

Replace the confirm `Dialog`'s `description={…}` (lines 196–202) with:

```tsx
        description={guidance
          ? <ConsequenceList consequence={guidance.actions.materialize_accept} tickSeconds={guidance.tickSeconds} />
          : `This creates the ${EPIC.singular.toLowerCase()} and ${tasks} sub-issue${tasks === 1 ? '' : 's'} `
            + `on GitHub. It cannot be undone from ${BRAND_NAME}. `
            + `It also moves this ${INITIATIVE.singular.toLowerCase()} to ${stageLabel(advancesTo)}.`}
```

`TrackTab`: replace lines 223 and 229–235 so it reads:

```tsx
function TrackTab({ initiative }: { initiative: Initiative }) {
  const guidance = initiative.guidance ?? null
  // The server sends artifacts in ASCENDING order (intent, spec, plan), so the
  // LAST committed one is the latest — `find` would link the intent commit.
  const newestCommitted = initiative.artifacts.findLast((a) => a.commitSha)
  return (
    <div className="flex flex-col gap-3">
      <MaterializationCard slug={initiative.slug} stage={initiative.stage} guidance={guidance} />
      <InitiativeTrack stage={initiative.stage} status={initiative.status} epic={initiative.epic} />
      {/* What happens with no human action, computed server-side from the same
          engine that acts (spec 2026-09-17 §5). A server without it shows the
          track alone rather than a stale hand-written sentence. */}
      {guidance && <WhatHappensNext guidance={guidance} />}
```

(keep the artifact links block unchanged).

In `InitiativePageContent`, after `const proposalPending = …` add:

```tsx
  const guidance = initiative?.guidance ?? null
  // The server's availability reason wins over the client's guess: the two agree
  // by construction (explain.py mirrors the 409 rules), but only one is authoritative.
  const tip = (control: keyof InitiativeGuidance['actions'], fallback: string | undefined) =>
    guidance && !guidance.actions[control].available
      ? (guidance.actions[control].unavailableReason ?? fallback)
      : fallback
```

Header buttons: change the three `title=` props:

```tsx
              title={tip('step_back', blocked ? 'Clear the block first' : prev ? `Move the gate back to ${stageLabel(prev)}` : 'Already at the first stage')}
```
```tsx
                title={tip('unblock', proposalPending ? 'Accept or reject the pending materialization first' : 'Clear the block without moving the gate')}
```
```tsx
                title={tip('advance', blocked ? 'Clear the block first' : next ? `Move the gate to ${stageLabel(next)}` : 'Already at the final stage')}
```

Dialogs: replace the three `description="…"` strings:

```tsx
          description={guidance
            ? <ConsequenceList consequence={guidance.actions.advance} tickSeconds={guidance.tickSeconds} />
            : 'This moves the human gate. The stage engine acts on the new stage on its next tick.'}
```
```tsx
          description={guidance
            ? <ConsequenceList consequence={guidance.actions.step_back} tickSeconds={guidance.tickSeconds} />
            : 'This moves the human gate back one stage. The stage engine re-enters it on its next tick.'}
```
```tsx
          description={guidance
            ? <ConsequenceList consequence={guidance.actions.unblock} tickSeconds={guidance.tickSeconds} />
            : 'This returns the initiative to awaiting-human without moving the gate. '
              + `If the failed materialize left an ${EPIC.singular.toLowerCase()} or sub-issues on GitHub, `
              + 'close them first: a later decomposition creates a fresh set.'}
```

`vocab.ts`: delete lines 136–149 (the `NEXT_ACTION_LABEL` comment, constant and `nextActionLabel`). Then `grep -rn "nextActionLabel\|NEXT_ACTION_LABEL" web/src` must return nothing.

- [ ] **Step 4: Run the roadmap tests, then the FULL web suite**

Run: `(cd web && npx vitest run src/features/roadmap src/features/products && npm run typecheck && npm run lint)`
Expected: PASS, clean.
Then: `(cd web && npm run test 2>&1 | tee /tmp/web-test.log; echo "exit=${PIPESTATUS[0]}")`
Expected: `exit=0` (read the exit code, not the tail: a `| tee` masks vitest's status).

- [ ] **Step 5: Commit**

```bash
git add web/src/features/roadmap/InitiativePage.tsx web/src/features/roadmap/InitiativePage.test.tsx web/src/features/roadmap/InitiativePage.materialization.test.tsx web/src/features/products/vocab.ts web/src/features/products/vocab.test.ts
printf 'feat(web): initiative page renders computed consequences\n\nConfirm dialogs list what the tap does now and next with cost badges, disabled\ncontrols carry the server reason, the Track tab gets a What-happens-next card,\nand the client-side NEXT_ACTION_LABEL map is purged (one source of truth).\n\nCo-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>\n' > /tmp/c8.txt
git commit -F /tmp/c8.txt
```

---

### Task 9: Product automation card — the gate table

**Files:**
- Modify: `web/src/features/roadmap/detail/AutomationPolicyCard.tsx` (imports; after the `</fieldset>` at line 123)
- Test: `web/src/features/roadmap/detail/AutomationPolicyCard.test.tsx`; mocks in `web/src/features/roadmap/detail/ProductDetail.test.tsx:56` and `web/src/features/roadmap/RoadmapPage.test.tsx` (wherever `usePolicySchema` is mocked)

**Interfaces:**
- Consumes: `useSdlcGates` (Task 6), `GateSpec`.

- [ ] **Step 1: Write the failing tests**

In `AutomationPolicyCard.test.tsx`, add `risk_tolerance` to `SCHEMA`:

```ts
  { key: 'risk_tolerance', label: 'Risk tolerance', type: 'enum', default: 'cautious', help: '', choices: ['cautious', 'balanced', 'bold'] },
```

Extend the hoisted bag and mock:

```ts
  gates: { data: undefined as unknown, isPending: false, isError: false },
```
```ts
  useSdlcGates: () => h.gates,
```

and in `beforeEach`: `Object.assign(h.gates, { data: GATES, isPending: false, isError: false })` with

```ts
const GATES = {
  gates: [
    { id: 'artifact.merge', label: 'Merge the stage artifact PR', risk: 'low', action: 'Arm GitHub auto-merge on the PR.', reverse: 'Revert the docs commit', autoResolvesUnder: { cautious: false, balanced: true, bold: true } },
    { id: 'materialization.accept', label: 'Accept a plan decomposition', risk: 'medium', action: 'Create the epic and sub-issues.', reverse: 'Close the created issues', autoResolvesUnder: { cautious: false, balanced: false, bold: true } },
    { id: 'dispatch', label: 'Dispatch a ready issue to a coding session', risk: null, action: "Governed by the product's dispatch setting.", reverse: 'Archive the session', autoResolvesUnder: { cautious: false, balanced: false, bold: false } },
    { id: 'deploy.production', label: 'Deploy to production', risk: 'high', action: 'Never automatic.', reverse: 'Roll back', autoResolvesUnder: { cautious: false, balanced: false, bold: false } },
  ],
  tolerances: ['cautious', 'balanced', 'bold'],
  autoresolveEnabled: true,
}
```

Add tests:

```tsx
  it('lists every gate with its verdict under the configured tolerance', () => {
    render(<AutomationPolicyCard product={product} project={makeProject({ automationPolicy: { risk_tolerance: 'balanced' } })} />)
    expect(screen.getByRole('heading', { name: 'Gates under balanced' })).toBeInTheDocument()
    const rows = screen.getAllByRole('row').slice(1)
    expect(rows).toHaveLength(4)
    expect(rows[0]).toHaveTextContent('Merge the stage artifact PR')
    expect(rows[0]).toHaveTextContent('Arm GitHub auto-merge on the PR.')
    expect(rows[0]).toHaveTextContent('Automatic')
    expect(rows[1]).toHaveTextContent('Waits for you')
    expect(rows[2]).toHaveTextContent('Policy-owned')
    expect(rows[3]).toHaveTextContent('Waits for you')
    expect(screen.queryByText(/switched off/)).not.toBeInTheDocument()
  })

  it('defaults to cautious and marks nothing automatic', () => {
    render(<AutomationPolicyCard product={product} project={makeProject({ automationPolicy: null })} />)
    expect(screen.getByRole('heading', { name: 'Gates under cautious' })).toBeInTheDocument()
    expect(screen.queryByText('Automatic')).not.toBeInTheDocument()
  })

  it('says every gate waits when the kill switch is off, whatever the tolerance', () => {
    Object.assign(h.gates, { data: { ...GATES, autoresolveEnabled: false } })
    render(<AutomationPolicyCard product={product} project={makeProject({ automationPolicy: { risk_tolerance: 'bold' } })} />)
    expect(screen.getByText(/Auto-resolve is switched off for this deployment/)).toBeInTheDocument()
    expect(screen.queryByText('Automatic')).not.toBeInTheDocument()
  })

  it('reports a gate list that will not load', () => {
    Object.assign(h.gates, { data: undefined, isPending: false, isError: true })
    render(<AutomationPolicyCard product={product} project={makeProject()} />)
    expect(screen.getByText('Gate list is unavailable.')).toBeInTheDocument()
  })
```

In `ProductDetail.test.tsx` (line 56 area) and `RoadmapPage.test.tsx` (its `vi.mock('@/api/hooks', …)` factory) add `useSdlcGates: () => ({ data: undefined, isPending: false, isError: false }),`.

- [ ] **Step 2: Run to verify they fail**

Run: `(cd web && npx vitest run src/features/roadmap/detail/AutomationPolicyCard.test.tsx)`
Expected: the four new tests FAIL.

- [ ] **Step 3: Implement the Gates section**

Imports: add `useSdlcGates` to the `@/api/hooks` import and `import type { GateSpec } from '@/api/sdlc'`.

Inside the component, after `const policy = …` add:

```tsx
  const gates = useSdlcGates()
```

After the effective-value loop (`for (const f of fields) effective[f.key] = …`) add:

```tsx
  // The tolerance the loop ACTS under: the configured value only while the
  // process kill switch is on (server `effective_tolerance`); otherwise cautious.
  const configured = String(effective.risk_tolerance ?? 'cautious')
  const enabled = gates.data?.autoresolveEnabled ?? true
  const acting = enabled ? configured : 'cautious'
  const verdict = (g: GateSpec): string =>
    g.risk === null ? 'Policy-owned' : g.autoResolvesUnder[acting] ? 'Automatic' : 'Waits for you'
```

Insert directly after `</fieldset>` (before the implementation-policy section):

```tsx
      {/* Every gate the tolerance governs, with what the selected level does to it
          (spec 2026-09-14 §11, built by 2026-09-17 D5). The verdict column is the
          answer to "if I pick balanced, what stops asking me?". */}
      <section aria-labelledby="gates-heading" className="mt-2 flex flex-col gap-2 border-t border-edge pt-3">
        <h4 id="gates-heading" className="text-sm font-medium">Gates under {configured}</h4>
        {!enabled && configured !== 'cautious' && (
          <Banner tone="info" title="Auto-resolve is switched off for this deployment, so every gate waits for you." />
        )}
        {gates.isPending ? (
          <p className="text-sm text-ink-muted" role="status">Checking gates…</p>
        ) : gates.isError || !gates.data ? (
          <p className="text-sm text-ink-muted" role="status">Gate list is unavailable.</p>
        ) : (
          <table className="w-full text-sm">
            <thead>
              <tr className="text-left text-xs uppercase text-ink-muted">
                <th scope="col" className="pb-1 font-medium">Gate</th>
                <th scope="col" className="pb-1 font-medium">Risk</th>
                <th scope="col" className="pb-1 font-medium">Under {acting}</th>
              </tr>
            </thead>
            <tbody>
              {gates.data.gates.map((g) => (
                <tr key={g.id} className="border-t border-edge align-top">
                  <td className="py-1 pr-2">
                    <p>{g.label}</p>
                    <p className="text-xs text-ink-muted">{g.action}</p>
                  </td>
                  <td className="py-1 pr-2 text-ink-muted">{g.risk ?? '—'}</td>
                  <td className="py-1">{verdict(g)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>
```

- [ ] **Step 4: Run the card tests, then the full web suite**

Run: `(cd web && npx vitest run src/features/roadmap/detail src/features/roadmap/RoadmapPage.test.tsx && npm run typecheck && npm run lint)`
Then: `(cd web && npm run test 2>&1 | tee /tmp/web-test.log; echo "exit=${PIPESTATUS[0]}")`
Expected: `exit=0`. Any other test file that now fails with `useSdlcGates is not a function` renders this card through a `@/api/hooks` mock: add the same one-line mock there.

- [ ] **Step 5: Commit**

```bash
git add web/src/features/roadmap/detail/AutomationPolicyCard.tsx web/src/features/roadmap/detail/AutomationPolicyCard.test.tsx web/src/features/roadmap/detail/ProductDetail.test.tsx web/src/features/roadmap/RoadmapPage.test.tsx
git add -u web/src   # any other test file that gained the useSdlcGates mock
printf 'feat(web): gate table on the product automation card\n\nEvery registry gate with its risk and whether the configured tolerance\nresolves it, plus a banner when the deployment kill switch is off (the table\nowed by spec 2026-09-14 s11).\n\nCo-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>\n' > /tmp/c9.txt
git commit -F /tmp/c9.txt
```

---

### Task 10: Inbox pane — what Accept and Reject do

**Files:**
- Modify: `web/src/features/roadmap/inbox/ProposalDetail.tsx` (imports; between the capability `Field` and the buttons, ~line 218)
- Test: `web/src/features/roadmap/inbox/ProposalDetail.test.tsx`

- [ ] **Step 1: Write the failing test**

Add to `ProposalDetail.test.tsx`:

```tsx
  it('says what Accept and Reject do under the product policy', () => {
    mount({ proposal: { ...proposal, guidance: {
      accept: { available: true, unavailableReason: null,
        immediate: [{ text: 'Files it under tasks with score 360; it leaves the inbox.', spawnsSession: false, writesGithub: false }],
        then: [{ text: "The Backlog Runner may dispatch it to a coding session once it is at or above 'ready' on the board.", spawnsSession: true, writesGithub: false }],
        when: 'next_tick', reversal: 'Undo within the window', gate: 'triage.accept' },
      reject: { available: true, unavailableReason: null,
        immediate: [{ text: 'Marks it needs-human; it leaves the inbox and nothing is dispatched.', spawnsSession: false, writesGithub: false }],
        then: [], when: 'now', reversal: null, gate: null },
    } } })
    const region = screen.getByRole('group', { name: 'What each decision does' })
    expect(region).toHaveTextContent('Files it under tasks with score 360')
    expect(region).toHaveTextContent('starts a session')
    expect(region).toHaveTextContent('Then · on the next automation run')
    expect(region).toHaveTextContent('Reverse: Undo within the window')
    expect(region).toHaveTextContent('Marks it needs-human')
  })

  it('renders no guidance block when the server sends none', () => {
    mount()
    expect(screen.queryByRole('group', { name: 'What each decision does' })).not.toBeInTheDocument()
  })
```

- [ ] **Step 2: Run to verify it fails**

Run: `(cd web && npx vitest run src/features/roadmap/inbox/ProposalDetail.test.tsx)`
Expected: the two new tests FAIL.

- [ ] **Step 3: Implement**

Add `import { ConsequenceList } from '../guidance/ConsequenceList'` to `ProposalDetail.tsx`. Inside the component, after `const options = …`, add `const guidance = p.guidance ?? null`. Insert between the capability `</Field>` and the buttons `<div className="flex flex-wrap justify-end gap-2">`:

```tsx
        {/* The consequence of each way out, from the owning product's dispatch
            policy (spec 2026-09-17 §4.3). No tick: the Backlog Runner's cadence is
            not the synthesizer's, so the timing says only "next run". */}
        {guidance && (
          <div role="group" aria-label="What each decision does" className="grid gap-3 rounded-md border border-edge p-3 sm:grid-cols-2">
            <ConsequenceList heading="Accept" consequence={guidance.accept} />
            <ConsequenceList heading="Reject" consequence={guidance.reject} />
          </div>
        )}
```

- [ ] **Step 4: Run the inbox tests and the full suite**

Run: `(cd web && npx vitest run src/features/roadmap/inbox && npm run typecheck && npm run lint)`
Then: `(cd web && npm run test 2>&1 | tee /tmp/web-test.log; echo "exit=${PIPESTATUS[0]}")`
Expected: `exit=0`

- [ ] **Step 5: Commit**

```bash
git add web/src/features/roadmap/inbox/ProposalDetail.tsx web/src/features/roadmap/inbox/ProposalDetail.test.tsx
printf 'feat(web): inbox pane says what Accept and Reject do\n\nCo-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>\n' > /tmp/c10.txt
git commit -F /tmp/c10.txt
```

---

### Task 11: Feature doc, full gates, PR

**Files:**
- Modify: `docs/product/features/sdlc-governance-evals.md` (add a section; add the new files to its `links.code` frontmatter list if one exists — check the file head)
- Modify: `docs/superpowers/specs/2026-09-14-roadmap-auto-execution-design.md` §11 first bullet: append "(Built 2026-09-17 from `GET /api/sdlc/gates` rather than the policy schema; see `2026-09-17-roadmap-consequence-guidance-design.md` D5.)"

- [ ] **Step 1: Write the doc section**

Append to `docs/product/features/sdlc-governance-evals.md`:

```markdown
## Consequence guidance

Every roadmap control explains itself in place (spec `docs/superpowers/specs/2026-09-17-roadmap-consequence-guidance-design.md`).
`server/sdlc/explain.py` runs the real stage engine on the hypothetical post-tap state and returns,
per control, what changes now, what the loop does afterwards and when, whether that starts a session
or writes to GitHub, and the reverse. The initiative detail carries it as `guidance` (computed under
the product's effective risk tolerance, which `nextAction` now also uses); `GET /api/sdlc/gates` exposes
the gate registry with per-tolerance auto-resolution; triage rows carry `guidance` for Accept/Reject
(`server/pm/explain.py`). The web renders all of it through one component,
`web/src/features/roadmap/guidance/ConsequenceList.tsx`: the initiative page's confirm dialogs and
"What happens next" card, the product automation card's gate table, and the inbox pane.
```

- [ ] **Step 2: Run the four server gate steps and the web scripts**

```bash
cd /home/tony/GitHub/.ccswitch/worktrees/viberpit/1ccd94bf
PY=/home/tony/GitHub/viberpit-deploy/.venv/bin/python
$PY -m ruff check src/ server/ && PYTHONPATH=$PWD/src $PY -m mypy server/ --ignore-missing-imports 2>&1 | tail -3
PYTHONPATH=$PWD/src $PY -m pytest tests/ -q 2>&1 | tail -3
env -u CCSWITCH_TAILNET_ALLOWLIST -u CCSWITCH_SESSIOND_CERT -u CCSWITCH_SESSIOND_KEY -u CCSWITCH_SESSIOND_CA -u CCSWITCH_DAEMON_ROLE CCSWITCH_DEV=1 PYTHONPATH=$PWD/src $PY -m pytest server/tests/ -n auto --dist loadfile --timeout=300 -q 2>&1 | tee /tmp/server-tests.log | tail -5
(cd web && npm run typecheck && npm run lint && npm run build && npm run test 2>&1 | tee /tmp/web-test.log; echo "exit=${PIPESTATUS[0]}")
```

Expected: ruff clean, mypy clean, both pytest suites green (known rocky-only `-n auto` flakes: re-run the named test alone and note it), web scripts clean with `exit=0`. Also run `PYTHONPATH=$PWD/src $PY scripts/brand_ratchet.py` and expect no new hits.

- [ ] **Step 3: Commit, push, open the PR, arm auto-merge**

```bash
git add docs/product/features/sdlc-governance-evals.md docs/superpowers/specs/2026-09-14-roadmap-auto-execution-design.md
printf 'docs(sdlc): consequence guidance feature note\n\nCo-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>\n' > /tmp/c11.txt
git commit -F /tmp/c11.txt
git push -u origin feat/roadmap-consequence-guidance
cat > /tmp/pr-body.md <<'EOF'
## Summary

Every roadmap / SDLC / PM control now says, in place, what tapping it changes now, what the loop does afterwards and when, and whether that starts a billed session or writes to GitHub. The copy is computed on the server by running the real stage engine on the hypothetical post-tap state, so it cannot drift from behaviour.

- `server/sdlc/explain.py` (pure) — consequence model; `guidance` on `GET /api/sdlc/initiatives/{slug}`; `nextAction` now runs under the product's effective risk tolerance instead of a hardcoded `cautious`.
- `GET /api/sdlc/gates` — the gate registry with per-tolerance auto-resolution.
- `server/pm/explain.py` — Accept/Reject guidance on triage rows from the product's dispatch policy.
- Web: one `ConsequenceList` renderer; confirm dialogs, a "What happens next" card, the product card's gate table (owed by the 2026-09-14 spec §11), and the inbox pane. `NEXT_ACTION_LABEL` purged.

Spec: `docs/superpowers/specs/2026-09-17-roadmap-consequence-guidance-design.md`. Plan: `docs/superpowers/plans/2026-09-17-roadmap-consequence-guidance.md`.

## Test plan

- [ ] `pytest server/tests/test_sdlc_explain.py test_sdlc_api.py test_pm_explain.py test_pm_api.py test_sdlc_paths.py test_sdlc_synth_mode_reader.py`
- [ ] full `lint-and-test` steps locally (ruff, mypy, both pytest suites)
- [ ] `web`: typecheck, lint, build, full vitest

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
gh pr create --title "feat(roadmap): in-app consequence guidance for gates, dispatch and triage" --body-file /tmp/pr-body.md
gh pr merge --squash --auto
```

Before `gh pr create`, re-run the in-flight check from `.agents/instructions/core.md` (the GraphQL query filtered on `server/(sdlc|api/sdlc|pm/explain)|web/src/features/roadmap`) and rebase if something landed on the same files.

---

## Self-review

**Spec coverage:** §3 model/simulation → Task 2; §3.4 availability → Task 2 + tooltips in Task 8; §4.1 detail + real tolerance + mode reader → Tasks 3, 4; §4.2 gates endpoint → Tasks 2, 4; §4.3 triage guidance → Task 5; §5 types/hook → Task 6; one renderer → Task 7; initiative page + purge → Task 8; gate table + kill-switch banner → Task 9; inbox → Task 10; §6 fallbacks → Tasks 8, 9, 10 tests; §7 test list → each task; docs → Task 11. §8 follow-ups (cap occupancy, assistant tool, bulk bar) are filed as issues in the PR body's wake, not built.

**Type consistency:** `Consequence` fields (`available, unavailableReason, immediate, then, when, reversal, gate`) match between `consequence_to_wire` (Task 2), the TS interface (Task 6) and every fixture (Tasks 7–10). `tickSeconds` is on `Guidance`/`InitiativeGuidance`, not on the triage guidance (Task 10 passes none). `useSdlcGates` is the hook name in Tasks 6 and 9 and all mocks.

**Placeholders:** none; every code step carries its code.

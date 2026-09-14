# Roadmap auto-execution — Phase 1 (substrate) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Land the behaviour-preserving substrate for executing the roadmap's front chain under a per-product risk tolerance: the gate registry and kill switch, the three policy keys, the tolerance-aware stage engine, the synthesizer's mode fold, derived initiative priority, priority-ordered visiting under a per-product front-chain cap, and RICE seeding at materialization. With every product at the default `cautious`, nothing auto-resolves; only visiting order, the cap and the priority numbers change.

**Architecture:** Pure modules first (`server/sdlc/gates.py`, `server/pm/priority.py`), then the pure engine gains a `tolerance` axis and two new actions (`MergeArtifact`, `Advance`) that Phase 2 will execute. The synthesizer loop keeps its injected-seam shape: the orchestrator's loader grows `project_slug`, `priority`, `epic_total`, `epic_done` and sorts; the loop reads product policies through one new seam for the cap. The roadmap map and the initiatives API carry `priority` computed once, in the shared epic-progress helper. The web shows the number and sorts by it inside a tier.

**Tech Stack:** Python 3.12 / FastAPI / SQLAlchemy async (SQLite in tests, Postgres in prod) · React + TypeScript + React Query + vitest/RTL.

**Spec:** `docs/superpowers/specs/2026-09-14-roadmap-auto-execution-design.md` (§4 gates, §5 engine, §6 loop, §7 seeding, §10 priority, §13 phase 1). Read it before any task.

## Global Constraints

- Constitution VI: one source of truth. Gate ids and risk classes live ONLY in `server/sdlc/gates.py`; the tolerance vocabulary is imported from there by the policy schema. Priority is computed ONLY by `server/pm/priority.py:initiative_priority`, called from the ONE epic-progress helper; the web renders the number and never recomputes it.
- The engine (`server/sdlc/engine.py`) stays pure and total: no imports from `server.db` or `server.automations`; `tolerance` is a string it is handed.
- Phase 1 is behaviour-preserving at the default policy: the synthesizer loop passes the literal `"cautious"` to the engine (Phase 2 wires the product's tolerance). Do not execute `MergeArtifact` or `Advance` in this phase.
- DB clock rule: never `func.now()`; use `datetime.now(timezone.utc)` as the sibling code does.
- No new model columns in this phase (they arrive with Phase 2).
- Web copy rules: no em-dashes in new UI strings; nouns from `web/src/features/products/vocab.ts`.
- The `lint-and-test` gate is four steps: `ruff check src/ server/`, `mypy server/ --ignore-missing-imports`, `pytest tests/`, `pytest server/tests/`. Web: `npm run typecheck`, `npm run lint`, `npm run test` (FULL run), `npm run build`, all from `(cd web && …)` subshells.
- Run server tests with the shared env scrubbed: `env -u CCSWITCH_TAILNET_ALLOWLIST -u CCSWITCH_DAEMON_ROLE -u CCSWITCH_SESSIOND_ENDPOINT -u CCSWITCH_SESSIOND_CLIENT_CERT -u CCSWITCH_SESSIOND_CLIENT_KEY -u CCSWITCH_SESSIOND_CA CCSWITCH_DEV=1 <main-checkout>/.venv/bin/python -m pytest …`. The worktree has no venv; use `/home/tony/GitHub/cc-account-switcher-deploy/.venv/bin/python` (or `ls /home/tony/GitHub/*/.venv/bin/python`). Export `PY=` to that path once per shell.
- Web `node_modules`: `ln -s /home/tony/GitHub/viberpit-deploy/web/node_modules web/node_modules` if missing.
- Commit after every task. Commit messages go in a file and are passed with `-F` (the Bash hook denies command text pairing a deploy verb with a prod word). Trailer: `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.
- Never `git stash` in this worktree (shared stash stack). Never branch off local `master`.

## File structure

| File | Responsibility |
|---|---|
| `server/sdlc/gates.py` (new) | `RISK_CLASSES`, `RISK_TOLERANCES`, `GateSpec`, `GATES`, `auto_resolves()`, `effective_tolerance()` |
| `server/config.py` | `gate_autoresolve_enabled` (`CCSWITCH_GATE_AUTORESOLVE_ENABLED`, default true), both settings shapes |
| `server/automations/product_policy.py` | + `risk_tolerance`, + `max_initiatives_in_flight`, + `unattended_model_floor` (stored only; applied in phase 2) |
| `server/sdlc/engine.py` | `next_action(..., tolerance)`; + `MergeArtifact`, + `Advance`; `InitiativeView.epic_total/epic_done`; `propose_only` gone |
| `server/api/sdlc.py` | detail route passes `"cautious"`; `_action_to_dict` covers the new actions; epic entries carry `priority`; initiative dicts carry `priority` |
| `server/automations/sdlc_intake.py` | passes `"cautious"` |
| `server/automations/sdlc_synthesizer.py` | modes `off \| on`, `fold_synth_mode`; loop: `read_policies` seam, cap, `queued`; `_to_views` carries epic counts |
| `server/automations/registry.py` | synthesizer oversight mode descriptions for `off \| on` |
| `server/sessions/orchestrator.py` | `_sdlc_synth_read_config` folds; one-shot stored-mode rewrite; `_sdlc_list_initiatives` grows and sorts; `_sdlc_read_policies` seam |
| `server/pm/priority.py` (new) | `initiative_priority()` |
| `server/pm/tree.py` | `rollup_epic_progress` rolls `priority` up as a max; `_initiative_node` exposes `priority` |
| `server/pm/materialize_apply.py` | seeds sub-issue RICE from the `origin_ref` issue |
| `docs/product/features/sdlc-governance-evals.md` | non-goals paragraph and engine note updated |
| `web/src/api/roadmap.ts` | `InitiativeNode.priority` |
| `web/src/features/roadmap/rows/InitiativeCard.tsx` | shows the priority |
| `web/src/features/roadmap/RoadmapListColumn.tsx` | tier sort breaks ties by priority desc |

---

### Task 1: The gate registry and the kill switch

**Files:**
- Create: `server/sdlc/gates.py`
- Modify: `server/config.py` (beside `self_model_switch_enabled`, both the pydantic `Field` block near line 245 and the dataclass fallback near line 553)
- Test: `server/tests/test_sdlc_gates.py` (new)

**Interfaces:**
- Produces: `RISK_CLASSES = ("low", "medium", "high")`, `RISK_TOLERANCES = ("cautious", "balanced", "bold")`, `GateSpec(id, label, risk, action, reverse)` (frozen dataclass; `risk: str | None`), `GATES: tuple[GateSpec, ...]`, `GATE_BY_ID: dict[str, GateSpec]`, `auto_resolves(gate_id: str, tolerance: str) -> bool`, `effective_tolerance(value: str | None, *, enabled: bool) -> str`, `settings.gate_autoresolve_enabled: bool`.

- [ ] **Step 1: Write the failing tests**

```python
"""Gate registry (spec 2026-09-14 §4): one inventory of where the roadmap waits on
a human, one rule for whether a product's tolerance lets the machine act."""
import pytest

from server.sdlc.gates import (
    GATE_BY_ID, GATES, RISK_CLASSES, RISK_TOLERANCES, auto_resolves,
    effective_tolerance,
)


def test_vocabularies():
    assert RISK_CLASSES == ("low", "medium", "high")
    assert RISK_TOLERANCES == ("cautious", "balanced", "bold")


def test_registry_lists_every_spec_gate_with_its_risk():
    risks = {g.id: g.risk for g in GATES}
    assert risks == {
        "artifact.merge": "low",
        "stage.advance": "low",
        "stage.advance_built": "low",
        "materialization.accept": "medium",
        "model.upgrade": "medium",
        "dispatch": None,
        "triage.accept": None,
        "issue.needs_spec": "high",
        "session.hand_back": "high",
        "initiative.blocked": "high",
        "triage.no_fit": "high",
        "deploy.production": "high",
        "issue.close": "high",
    }
    assert GATE_BY_ID["stage.advance"].reverse == "Step back"
    for g in GATES:
        assert g.label and g.action and g.reverse


@pytest.mark.parametrize("tolerance,expected", [
    ("cautious", {"artifact.merge": False, "materialization.accept": False, "issue.close": False}),
    ("balanced", {"artifact.merge": True, "stage.advance": True, "stage.advance_built": True,
                  "materialization.accept": False, "model.upgrade": False, "issue.close": False}),
    ("bold", {"artifact.merge": True, "materialization.accept": True, "model.upgrade": True,
              "issue.close": False, "deploy.production": False}),
])
def test_auto_resolves_matrix(tolerance, expected):
    for gate_id, want in expected.items():
        assert auto_resolves(gate_id, tolerance) is want, (gate_id, tolerance)


def test_policy_owned_gates_and_unknowns_never_auto_resolve_here():
    # dispatch / triage.accept have their own knobs; this rule must not double-decide them.
    assert auto_resolves("dispatch", "bold") is False
    assert auto_resolves("triage.accept", "bold") is False
    assert auto_resolves("no.such.gate", "bold") is False
    assert auto_resolves("artifact.merge", "turbo") is False


def test_effective_tolerance_kill_switch_and_garbage():
    assert effective_tolerance("bold", enabled=True) == "bold"
    assert effective_tolerance("bold", enabled=False) == "cautious"
    assert effective_tolerance(None, enabled=True) == "cautious"
    assert effective_tolerance("turbo", enabled=True) == "cautious"


def test_settings_carry_the_kill_switch(monkeypatch):
    from server.config import Settings
    monkeypatch.delenv("CCSWITCH_GATE_AUTORESOLVE_ENABLED", raising=False)
    assert Settings().gate_autoresolve_enabled is True
    monkeypatch.setenv("CCSWITCH_GATE_AUTORESOLVE_ENABLED", "false")
    assert Settings().gate_autoresolve_enabled is False
```

(`from server.config import Settings` and a bare `Settings()` is the idiom `server/tests/test_config_model_approval.py` already uses.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST -u CCSWITCH_DAEMON_ROLE -u CCSWITCH_SESSIOND_ENDPOINT -u CCSWITCH_SESSIOND_CLIENT_CERT -u CCSWITCH_SESSIOND_CLIENT_KEY -u CCSWITCH_SESSIOND_CA CCSWITCH_DEV=1 $PY -m pytest server/tests/test_sdlc_gates.py -q`
Expected: FAIL with `ModuleNotFoundError: server.sdlc.gates`.

- [ ] **Step 3: Write the module**

`server/sdlc/gates.py`:

```python
"""Gate registry (spec 2026-09-14-roadmap-auto-execution-design §4).

Every point where the roadmap waits on a human is a named gate with a risk
class. A product's ``risk_tolerance`` (policy) covers a class or it does not;
``auto_resolves`` is the ONE rule the loops ask. ``high`` never auto-resolves.

Pure: no I/O, no imports from server.db / server.automations, so the stage
engine can import it and stay total.
"""
from __future__ import annotations

from dataclasses import dataclass

RISK_CLASSES: tuple[str, ...] = ("low", "medium", "high")
RISK_TOLERANCES: tuple[str, ...] = ("cautious", "balanced", "bold")

#: The highest risk class each tolerance level covers. ``None`` = nothing.
_CEILING: dict[str, str | None] = {"cautious": None, "balanced": "low", "bold": "medium"}


@dataclass(frozen=True)
class GateSpec:
    id: str
    label: str
    #: One of RISK_CLASSES, or None for a gate governed by its own policy knob
    #: (listed so the inventory is complete; never decided by this rule).
    risk: str | None
    #: What auto-resolution does, one sentence, shown in the UI.
    action: str
    #: The human reverse action, shown beside an auto-resolved row.
    reverse: str


GATES: tuple[GateSpec, ...] = (
    GateSpec("artifact.merge", "Merge the stage artifact PR", "low",
             "Arm GitHub auto-merge on the intent, spec or plan PR; CI stays the gate.",
             "Revert the docs commit"),
    GateSpec("stage.advance", "Advance a stage once its artifact merged", "low",
             "Advance the initiative to the next stage.", "Step back"),
    GateSpec("stage.advance_built", "Advance build to deploy once the epic is closed", "low",
             "Advance the initiative from build to deploy.", "Step back"),
    GateSpec("materialization.accept", "Accept a plan decomposition", "medium",
             "Create the epic and sub-issues the decompose session proposed.",
             "Close the created issues"),
    GateSpec("model.upgrade", "Approve a session's request for a costlier model", "medium",
             "Approve the requested tier within the self-switch cap.", "Switch the model back"),
    GateSpec("dispatch", "Dispatch a ready issue to a coding session", None,
             "Governed by the product's dispatch setting.", "Archive the session"),
    GateSpec("triage.accept", "Accept a classification proposal", None,
             "Governed by the product's triage auto-accept tier.", "Undo within the window"),
    GateSpec("issue.needs_spec", "Retry an issue that bounced out", "high",
             "Never automatic.", "Edit the issue body"),
    GateSpec("session.hand_back", "Answer a session that handed back", "high",
             "Never automatic.", "Reply in the session"),
    GateSpec("initiative.blocked", "Unblock a blocked initiative", "high",
             "Never automatic.", "Unblock or step back"),
    GateSpec("triage.no_fit", "Create a capability for a no-fit proposal", "high",
             "Never automatic.", "Reject the proposal"),
    GateSpec("deploy.production", "Deploy to production", "high",
             "Never automatic.", "Roll back"),
    GateSpec("issue.close", "Close an issue as resolved or won't do", "high",
             "Never automatic.", "Reopen the issue"),
)

GATE_BY_ID: dict[str, GateSpec] = {g.id: g for g in GATES}


def auto_resolves(gate_id: str, tolerance: str) -> bool:
    """Whether *tolerance* covers *gate_id*'s risk class. Unknown gates, unknown
    tolerances, policy-owned gates (``risk is None``) and ``high`` all answer False."""
    gate = GATE_BY_ID.get(gate_id)
    ceiling = _CEILING.get(tolerance)
    if gate is None or gate.risk is None or ceiling is None:
        return False
    if gate.risk == "high":
        return False
    return RISK_CLASSES.index(gate.risk) <= RISK_CLASSES.index(ceiling)


def effective_tolerance(value: str | None, *, enabled: bool) -> str:
    """The tolerance a loop should act on: the policy value when it is in the
    vocabulary and the kill switch is on; ``cautious`` otherwise."""
    if not enabled or value not in RISK_TOLERANCES:
        return "cautious"
    return value
```

`server/config.py`, pydantic block (after `self_model_switch_cap`):

```python
        # Roadmap auto-execution (spec 2026-09-14 D9): process-wide kill switch. Off
        # forces every product's risk tolerance to `cautious` without touching any policy.
        gate_autoresolve_enabled: bool = Field(
            default=True, alias="CCSWITCH_GATE_AUTORESOLVE_ENABLED")
```

Dataclass fallback block (after `self_model_switch_cap`):

```python
        gate_autoresolve_enabled: bool = dataclasses.field(
            default_factory=lambda: os.environ.get("CCSWITCH_GATE_AUTORESOLVE_ENABLED", "true").lower() in ("1", "true", "yes", "on")
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run the same pytest command. Expected: PASS (7 tests).

- [ ] **Step 5: Commit**

```bash
cat > /tmp/c1.txt <<'EOF'
feat(sdlc): gate registry and auto-resolve kill switch (roadmap auto-execution, phase 1)

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
EOF
git add server/sdlc/gates.py server/config.py server/tests/test_sdlc_gates.py
git commit -q -F /tmp/c1.txt
```

---

### Task 2: The three policy keys

**Files:**
- Modify: `server/automations/product_policy.py:34-79` (append two fields)
- Test: `server/tests/test_product_policy.py` (update `test_schema_keys_and_defaults`, add two tests), `server/tests/test_pm_api.py:111-119` (extend the schema assertion)

**Interfaces:**
- Consumes: `RISK_TOLERANCES` from Task 1.
- Produces: policy keys `risk_tolerance` (enum, default `"cautious"`), `max_initiatives_in_flight` (int, default `2`, min `0`) and `unattended_model_floor` (enum `none` + `MODEL_TIERS`, default `"none"`), present in every `effective_policy()` result.

- [ ] **Step 1: Update and add the failing tests**

In `server/tests/test_product_policy.py`, change `test_schema_keys_and_defaults` to:

```python
def test_schema_keys_and_defaults():
    keys = [f.key for f in PRODUCT_POLICY_SCHEMA]
    assert keys == ["dispatch", "queue_min_status", "max_in_flight", "weight",
                    "max_attempts", "give_up_after_bounces", "default_model",
                    "respect_dependencies", "triage_auto_accept",
                    "risk_tolerance", "max_initiatives_in_flight", "unattended_model_floor"]
    assert DISPATCH_MODES == ("off", "propose", "auto")
    assert effective_policy(None) == {
        "dispatch": "off", "queue_min_status": "Ready", "max_in_flight": 1, "weight": 1,
        "max_attempts": 2, "give_up_after_bounces": 2, "default_model": "auto",
        "respect_dependencies": True, "triage_auto_accept": "off",
        "risk_tolerance": "cautious", "max_initiatives_in_flight": 2,
        "unattended_model_floor": "none"}
```

Append:

```python
def test_risk_tolerance_is_the_gate_vocabulary_defaulting_to_cautious():
    """Spec 2026-09-14 D2/D3: the dial's choices are imported from the gate
    registry, never retyped here (constitution VI)."""
    from server.sdlc.gates import RISK_TOLERANCES
    field = {f.key: f for f in PRODUCT_POLICY_SCHEMA}["risk_tolerance"]
    assert field.type == "enum"
    assert field.default == "cautious"
    assert field.choices == list(RISK_TOLERANCES)
    assert effective_policy({"risk_tolerance": "bold"})["risk_tolerance"] == "bold"
    # Garbage degrades the WHOLE policy to defaults: a bad row can only be more careful.
    assert effective_policy({"risk_tolerance": "reckless"})["risk_tolerance"] == "cautious"


def test_max_initiatives_in_flight_defaults_to_two_and_zero_pauses():
    field = {f.key: f for f in PRODUCT_POLICY_SCHEMA}["max_initiatives_in_flight"]
    assert field.type == "int"
    assert field.default == 2
    assert field.min == 0
    assert effective_policy({"max_initiatives_in_flight": 0})["max_initiatives_in_flight"] == 0
    assert effective_policy({"max_initiatives_in_flight": -1})["max_initiatives_in_flight"] == 2


def test_unattended_model_floor_is_none_or_a_model_tier():
    """Spec 2026-09-14 D11: the tier vocabulary comes from
    server/sessions/model_request.py:MODEL_TIERS, never retyped (constitution VI).
    Schema only in phase 1; phase 2 applies it to unattended spawns."""
    from server.sessions.model_request import MODEL_TIERS
    field = {f.key: f for f in PRODUCT_POLICY_SCHEMA}["unattended_model_floor"]
    assert field.type == "enum"
    assert field.default == "none"
    assert field.choices == ["none", *MODEL_TIERS]
    assert effective_policy({"unattended_model_floor": "opus"})["unattended_model_floor"] == "opus"
    assert effective_policy({"unattended_model_floor": "gpt-5"})["unattended_model_floor"] == "none"
```

In `server/tests/test_pm_api.py` after line 119 (`assert fields["max_attempts"]["min"] == 1`), add:

```python
        assert fields["risk_tolerance"]["choices"] == ["cautious", "balanced", "bold"]
        assert fields["max_initiatives_in_flight"]["min"] == 0
        assert fields["unattended_model_floor"]["default"] == "none"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `… $PY -m pytest server/tests/test_product_policy.py -q` and `… $PY -m pytest server/tests/test_pm_api.py -q -k policy`
Expected: FAIL on the key list / KeyError `risk_tolerance`.

- [ ] **Step 3: Add the fields**

In `server/automations/product_policy.py`, add the import `from server.sdlc.gates import RISK_TOLERANCES` beside the other imports, and append to `PRODUCT_POLICY_SCHEMA` after `triage_auto_accept`:

```python
    # Roadmap auto-execution (spec 2026-09-14 D2/D3): how bold the SDLC front chain
    # may be for this product. The gate registry (server/sdlc/gates.py) decides
    # which gates each level covers; `cautious` is today's behaviour.
    ConfigField(key="risk_tolerance", label="Risk tolerance", type="enum",
                default="cautious", choices=list(RISK_TOLERANCES),
                help="cautious: every roadmap gate waits for you. balanced: low-risk gates "
                     "resolve themselves (artifact PR merges, stage advances). bold: also "
                     "medium-risk gates (plan decomposition accept, model upgrades)."),
    # ``min=0``: 0 pauses new artifact sessions for the product without losing its
    # other settings; gate resolutions are never capped (they finish work).
    ConfigField(key="max_initiatives_in_flight", label="Max initiatives in flight", type="int",
                default=2, min=0,
                help="How many of this product's initiatives may be producing a spec or "
                     "plan at once. 0 pauses new artifact sessions."),
    # Spec 2026-09-14 D11: model capability as a risk mitigation. Phase 1 stores it;
    # phase 2 applies it as the floor for sessions whose output a covered gate will
    # consume without a human read (inert under `cautious`).
    ConfigField(key="unattended_model_floor", label="Unattended model floor", type="enum",
                default="none", choices=["none", *MODEL_TIERS],
                help="Minimum model for a spec, plan or decompose session whose output will "
                     "be merged or accepted without a human read. Inert under cautious."),
```

Add `from server.sessions.model_request import MODEL_TIERS` beside the other imports (`model_request` is a pure vocabulary module; confirm it imports nothing from `server.automations` with `grep -n "^from\|^import" server/sessions/model_request.py`). Update the module docstring's second paragraph to mention the three keys joined in the 2026-09-14 spec's phase 1.

- [ ] **Step 4: Run tests to verify they pass**

Run both pytest commands. Expected: PASS. Also run `… $PY -m pytest server/tests/test_backlog_runner_loop.py server/tests/test_pm_triage_loop.py -q` to confirm nothing keyed off the exact policy dict shape (fix any exact-dict assertion by adding the two new keys).

- [ ] **Step 5: Commit**

```bash
cat > /tmp/c2.txt <<'EOF'
feat(pm): risk_tolerance, max_initiatives_in_flight and unattended_model_floor join the product policy

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
EOF
git add server/automations/product_policy.py server/tests/test_product_policy.py server/tests/test_pm_api.py
git commit -q -F /tmp/c2.txt
```

---

### Task 3: The engine gains a tolerance axis

**Files:**
- Modify: `server/sdlc/engine.py` (module docstring; `InitiativeView`; new actions after `Materialize`; `StageAction` union; `next_action` 180-235)
- Modify: `server/api/sdlc.py:450` (call site) and `_action_to_dict`
- Modify: `server/automations/sdlc_intake.py:173`
- Modify: `server/automations/sdlc_synthesizer.py:175` (call site only, literal `"cautious"`; imports)
- Test: `server/tests/test_sdlc_engine.py` (replace the block at lines 150-199)

**Interfaces:**
- Consumes: `auto_resolves` from Task 1.
- Produces: `next_action(initiative: InitiativeView, artifacts: list[ArtifactView], mode: str, tolerance: str) -> StageAction`; `MergeArtifact(kind: str, pr_number: int | None = None)`; `Advance(to_stage: str)`; `InitiativeView(..., epic_total: int = 0, epic_done: int = 0)`. `mode` semantics: `"off"` → `NeedsHuman`; anything else proceeds.

- [ ] **Step 1: Replace the propose_only tests with the tolerance matrix**

Delete lines 150-199 of `server/tests/test_sdlc_engine.py` (the six `propose_only` tests and their banner). Every remaining `next_action(..., mode, {})` call in the file becomes `next_action(..., mode, "cautious")` (`sed -i 's/, {})/, "cautious")/' server/tests/test_sdlc_engine.py` then eyeball the diff). Add `Advance, MergeArtifact` to the import list. Append:

```python
# --------------------------------------------------------------------------- #
# Tolerance axis (spec 2026-09-14 §5): cautious == today; balanced/bold act    #
# --------------------------------------------------------------------------- #

def test_cautious_keeps_every_human_gate():
    assert next_action(_init(stage="intake"), [_art("intent", pr=1)], "on", "cautious") == AwaitApproval("intent")
    assert next_action(_init(stage="intake"), [_art("intent", sha="cafe", pr=1)], "on", "cautious") == AwaitApproval("intent")
    built = _init(stage="build", issue="owner/repo#9", epic_total=3, epic_done=3)
    assert next_action(built, [], "on", "cautious") == HandoffToBuild("owner/repo#9")


def test_balanced_arms_merge_on_an_open_artifact_pr():
    assert next_action(_init(stage="design"), [_art("spec", pr=7)], "on", "balanced") == MergeArtifact("spec", 7)


def test_open_pr_without_a_known_number_still_waits():
    # Enrichment has not filled pr_number yet: nothing to arm, so wait a tick.
    assert next_action(_init(stage="design"), [_art("spec")], "on", "balanced") == AwaitApproval("spec")


def test_balanced_advances_a_merged_artifact_one_stage():
    assert next_action(_init(stage="intake"), [_art("intent", sha="cafe", pr=1)], "on", "balanced") == Advance("design")
    assert next_action(_init(stage="design"), [_art("spec", sha="cafe", pr=2)], "on", "balanced") == Advance("plan")
    with_epic = _init(stage="plan", issue="owner/repo#9", epic="owner/repo#9")
    assert next_action(with_epic, [_art("plan", sha="cafe", pr=3)], "on", "balanced") == Advance("build")


def test_materialize_precedes_advance_at_every_tolerance():
    arts = [_art("plan", sha="cafe", pr=3)]
    for tol in ("cautious", "balanced", "bold"):
        assert next_action(_init(stage="plan", issue="owner/repo#9"), arts, "on", tol) == Materialize()


def test_balanced_advances_a_built_initiative_to_deploy():
    built = _init(stage="build", issue="owner/repo#9", epic_total=3, epic_done=3)
    assert next_action(built, [], "on", "balanced") == Advance("deploy")


def test_an_unfinished_epic_never_advances():
    building = _init(stage="build", issue="owner/repo#9", epic_total=3, epic_done=2)
    assert next_action(building, [], "on", "bold") == HandoffToBuild("owner/repo#9")
    no_epic = _init(stage="build", issue="owner/repo#9")
    assert next_action(no_epic, [], "on", "bold") == HandoffToBuild("owner/repo#9")


def test_off_and_blocked_win_over_any_tolerance():
    arts = [_art("intent", sha="cafe", pr=1)]
    assert next_action(_init(stage="intake"), arts, "off", "bold") == NeedsHuman(reason="SDLC automation is off")
    assert next_action(_init(stage="intake", status="blocked"), arts, "on", "bold") == NeedsHuman(reason="initiative is blocked")


def test_legacy_modes_are_simply_not_off():
    # Intake still passes propose/auto; the engine treats anything but off as on.
    arts = [_art("intent", sha="cafe", pr=1)]
    assert next_action(_init(stage="intake"), arts, "propose", "balanced") == Advance("design")
    assert next_action(_init(stage="intake"), arts, "auto", "cautious") == AwaitApproval("intent")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `… $PY -m pytest server/tests/test_sdlc_engine.py -q`
Expected: FAIL with `ImportError: cannot import name 'Advance'`.

- [ ] **Step 3: Change the engine**

In `server/sdlc/engine.py`:

1. Module docstring lines 14-18: replace the propose-only sentence with: "``next_action`` takes the product's risk *tolerance* (spec 2026-09-14 §5): under ``cautious`` every artifact gate returns ``AwaitApproval`` and a human tap advances; ``balanced``/``bold`` return ``MergeArtifact``/``Advance`` for the gates the registry (``server/sdlc/gates.py``) says they cover. The engine never performs the write; the synthesizer loop does."
2. Add `from server.sdlc.gates import auto_resolves` after the dataclass import.
3. `InitiativeView`: append two fields with a comment:

```python
    # Sub-issue progress of the epic (the build stage's completion signal for the
    # ``stage.advance_built`` gate); 0/0 when there is no epic.
    epic_total: int = 0
    epic_done: int = 0
```

4. After `Materialize`, add:

```python
@dataclass(frozen=True)
class MergeArtifact:
    """The stage artifact PR is open and the tolerance covers ``artifact.merge``:
    the loop arms auto-merge on it (its one GitHub write). ``pr_number`` is
    always set — with no number known yet the engine waits instead."""
    kind: str
    pr_number: int | None = None


@dataclass(frozen=True)
class Advance:
    """The stage is complete (artifact merged; at plan, the epic exists; at build,
    the epic is fully closed) and the tolerance covers the advance gate: move to
    ``to_stage`` through the same write the human Advance tap uses. The NEXT tick
    proposes the next artifact — one write per tick, one decision per action."""
    to_stage: str
```

5. `StageAction = Propose | AwaitApproval | MergeArtifact | Advance | Materialize | HandoffToBuild | Done | NeedsHuman`.
6. Replace `next_action` (signature through the end of the function) with:

```python
def next_action(
    initiative: InitiativeView,
    artifacts: list[ArtifactView],
    mode: str,
    tolerance: str,
) -> StageAction:
    """Decide the next SDLC action for *initiative*. Pure + total.

    ``mode`` is the owning automation's mode: ``"off"`` parks everything; any
    other value proceeds (the synthesizer's is ``off | on``; intake still passes
    its legacy ``propose``/``auto``). ``tolerance`` is the product's
    ``risk_tolerance`` (``server/sdlc/gates.py:RISK_TOLERANCES``); the caller
    resolves it, this function only consults ``auto_resolves``.
    """
    # Terminal / override states first (checked before any stage logic).
    if initiative.stage == "done" or initiative.status == "done":
        return Done()
    if initiative.status == "blocked":
        return NeedsHuman(reason="initiative is blocked")
    if mode == "off":
        return NeedsHuman(reason="SDLC automation is off")

    stage = initiative.stage

    # Non-artifact stages: a fully built epic may advance to deploy under the
    # `stage.advance_built` gate; otherwise hand off (build) or defer to existing flows.
    if stage not in STAGE_ARTIFACT:
        if (stage == "build" and initiative.epic_total > 0
                and initiative.epic_done >= initiative.epic_total
                and auto_resolves("stage.advance_built", tolerance)):
            return Advance(advance_stage(stage))
        return _enter_stage(stage, initiative)

    kind = STAGE_ARTIFACT[stage]
    art = _find_artifact(artifacts, kind)

    # Nothing proposed yet → propose generating this stage's artifact.
    if art is None:
        return Propose(kind)

    # Artifact PR open (not merged): arm its merge if the gate allows and the PR
    # number is known (enrichment fills it); otherwise wait for the human.
    if art.commit_sha is None:
        if art.pr_number is not None and auto_resolves("artifact.merge", tolerance):
            return MergeArtifact(kind, art.pr_number)
        return AwaitApproval(kind)

    # Merged plan with no epic yet → materialize (spec 003 §7b), ahead of any
    # advance: until the plan is decomposed there is nothing to advance to.
    if stage == "plan" and initiative.epic is None:
        return Materialize()

    # Artifact merged: advance under the `stage.advance` gate, else await the tap.
    if auto_resolves("stage.advance", tolerance):
        return Advance(advance_stage(stage))
    return AwaitApproval(kind)
```

7. Callers:
   - `server/api/sdlc.py:450`: `action = next_action(state, artifact_views, "propose", "cautious")` and update the comment above it to "display-only: mode on, tolerance cautious, so the detail shows the human's next step". Also add `Advance, MergeArtifact` to the engine import at line ~41 and two branches to `_action_to_dict` before the `return {"type": "unknown"}`:

```python
    if isinstance(action, MergeArtifact):
        return {"type": "merge_artifact", "kind": action.kind, "prNumber": action.pr_number}
    if isinstance(action, Advance):
        return {"type": "advance", "toStage": action.to_stage}
```

   - `server/automations/sdlc_intake.py:173`: `action = engine.next_action(view, artifacts, mode, "cautious")` with the comment `# Intake only proposes intents; the synthesizer resolves gates (spec 2026-09-14 §5).`
   - `server/automations/sdlc_synthesizer.py:175`: `action = next_action(state, arts, mode, "cautious")` with the comment `# Phase 1: literal cautious; phase 2 passes the product's effective tolerance.`

- [ ] **Step 4: Run tests to verify they pass**

Run: `… $PY -m pytest server/tests/test_sdlc_engine.py server/tests/test_sdlc_api.py server/tests/test_sdlc_intake_loop.py server/tests/test_sdlc_synthesizer_loop.py -q`
Expected: PASS. Then `$PY -m mypy server/sdlc/engine.py server/api/sdlc.py --ignore-missing-imports` clean.

- [ ] **Step 5: Commit**

```bash
cat > /tmp/c3.txt <<'EOF'
feat(sdlc): stage engine decides under a risk tolerance; MergeArtifact and Advance actions

Retires the dead propose_only seam (it had no writer anywhere).

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
EOF
git add server/sdlc/engine.py server/api/sdlc.py server/automations/sdlc_intake.py server/automations/sdlc_synthesizer.py server/tests/test_sdlc_engine.py
git commit -q -F /tmp/c3.txt
```

---

### Task 4: The synthesizer's modes fold to off | on

**Files:**
- Modify: `server/automations/sdlc_synthesizer.py` (docstring; `SdlcSynthesizer.description`, `.modes`; new `fold_synth_mode`)
- Modify: `server/automations/registry.py:201-212` (oversight mode descriptions)
- Modify: `server/sessions/orchestrator.py` (`_sdlc_synth_read_config` ~8143; `_sdlc_synth_tick_once` ~8818; new `_sdlc_synth_fold_stored_mode`)
- Modify: `docs/product/features/sdlc-governance-evals.md:65-69, 88-89`
- Test: `server/tests/test_sdlc_synthesizer_loop.py` (`test_automation_contract`, new fold test), `server/tests/test_sdlc_wiring.py` (new stored-mode rewrite test)

**Interfaces:**
- Produces: `LEGACY_SYNTH_MODES = ("propose", "auto")`, `fold_synth_mode(mode: str) -> str`, `SdlcSynthesizer.modes == ("off", "on")`.

- [ ] **Step 1: Write the failing tests**

In `server/tests/test_sdlc_synthesizer_loop.py` change `test_automation_contract` to assert `a.modes == ("off", "on")` and append:

```python
def test_fold_synth_mode_maps_legacy_modes_to_on():
    from server.automations.sdlc_synthesizer import LEGACY_SYNTH_MODES, fold_synth_mode
    assert LEGACY_SYNTH_MODES == ("propose", "auto")
    assert fold_synth_mode("propose") == "on"
    assert fold_synth_mode("auto") == "on"
    assert fold_synth_mode("on") == "on"
    assert fold_synth_mode("off") == "off"      # a human's explicit park survives
```

In `server/tests/test_sdlc_wiring.py` append (same fixture pattern as `test_sdlc_list_initiatives_projects_the_epic_pointer`):

```python
def test_sdlc_synth_first_tick_rewrites_a_legacy_stored_mode():
    """A row still carrying `auto` from before the fold is rewritten to `on` once,
    so the API's mode select stops showing a value outside `modes`. An explicit
    `off` is left alone."""
    from server.app import create_app
    from server.db import db_session
    from server.db.models import Automation as AutomationRow
    from sqlalchemy import select

    async def _seed_fold_read(orch):
        async with db_session() as db:
            row = (await db.execute(select(AutomationRow).where(
                AutomationRow.id == "sdlc-synthesizer"))).scalar_one()
            row.enabled, row.mode = True, "auto"
            await db.commit()
        await orch._sdlc_synth_fold_stored_mode()
        await orch._sdlc_synth_fold_stored_mode()          # idempotent
        mode, _ = await orch._sdlc_synth_read_config()
        async with db_session() as db:
            stored = (await db.execute(select(AutomationRow.mode).where(
                AutomationRow.id == "sdlc-synthesizer"))).scalar_one()
        return mode, stored

    with tempfile.TemporaryDirectory() as tmp, temp_env(), patch.dict(
        os.environ, {"CCSWITCH_DB_URL": f"sqlite+aiosqlite:///{tmp}/wiring.db"}
    ):
        orch = _make_orch()
        app = create_app(orchestrator=orch)
        client = TestClient(app, raise_server_exceptions=False)
        with client:
            mode, stored = client.portal.call(_seed_fold_read, orch)
    assert (mode, stored) == ("on", "on")
```

If the registry's `ensure_rows()` does not create the row during lifespan in this fixture (the `scalar_one()` raises), create it in the seed: `db.add(AutomationRow(id="sdlc-synthesizer", enabled=True, mode="auto", config={}))`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `… $PY -m pytest server/tests/test_sdlc_synthesizer_loop.py -q -k "contract or fold"` and `… $PY -m pytest server/tests/test_sdlc_wiring.py -q -k legacy_stored_mode`
Expected: FAIL (`modes` mismatch; `AttributeError: _sdlc_synth_fold_stored_mode`).

- [ ] **Step 3: Implement**

`server/automations/sdlc_synthesizer.py`:
- Module docstring: replace the "Propose-only v1" paragraph with "The loop's mode is ``off | on`` (spec 2026-09-14 D8): whether a gate resolves itself is the product's ``risk_tolerance``, not a global mode. Under ``cautious`` an initiative parks at ``awaiting_human`` after its artifact session exactly as before."
- After `MATERIALIZE_MAX_ATTEMPTS`, add:

```python
#: Modes the row could carry before spec 2026-09-14 D8 folded them into ``on``.
LEGACY_SYNTH_MODES: tuple[str, ...] = ("propose", "auto")


def fold_synth_mode(mode: str) -> str:
    """ONE fold, shared by the read path (``_sdlc_synth_read_config``) and the
    first-tick row rewrite (``_sdlc_synth_fold_stored_mode``). A stored ``off``
    survives verbatim: it is a human's explicit park."""
    return "on" if mode in LEGACY_SYNTH_MODES else mode
```

- `SdlcSynthesizer.description = "Carries initiatives intent→spec→plan as artifact PRs; resolves stage gates under each product's risk tolerance."` and `modes: tuple[str, ...] = ("off", "on")`.

`server/automations/registry.py:201-212`, the synthesizer registration's mode map becomes:

```python
    {
        'off': 'Disable background artifact synthesis, plan decomposition and gate resolution.',
        'on': 'Launch specification, plan and merged-plan decomposition sessions in priority order '
              'under each product\'s front-chain cap; resolve stage gates only as far as the '
              'product\'s risk tolerance allows (cautious keeps every human gate).',
    },
```

`server/sessions/orchestrator.py`:
- `_sdlc_synth_read_config`:

```python
    async def _sdlc_synth_read_config(self) -> tuple[str, dict]:
        """The sdlc-synthesizer row's (mode, config) merged onto schema defaults,
        with a pre-fold `propose`/`auto` read as `on` (spec 2026-09-14 D8). Off +
        pure defaults on any absent or invalid row so a bad config never spawns."""
        from server.automations.config_store import read_automation_config
        from server.automations.sdlc_synthesizer import SdlcSynthesizer, fold_synth_mode
        mode, config = await read_automation_config(
            SdlcSynthesizer, modes=("on", "propose", "auto"),
            log_key="sdlc_synthesizer")
        return fold_synth_mode(mode), config
```

- New method beside it:

```python
    async def _sdlc_synth_fold_stored_mode(self) -> None:
        """One-shot: rewrite a stored legacy `propose`/`auto` to `on` so the row
        stops being load-bearing on the read-path fold and the API's mode select
        shows a value inside `modes`. Idempotent; `off` untouched."""
        if getattr(self, "_sdlc_synth_mode_folded", False):
            return
        self._sdlc_synth_mode_folded = True
        from sqlalchemy import select as sa_select
        from server.automations.sdlc_synthesizer import fold_synth_mode
        from server.db.models import Automation as AutomationRow
        try:
            async with db_session() as db:
                row = (await db.execute(sa_select(AutomationRow).where(
                    AutomationRow.id == "sdlc-synthesizer"))).scalar_one_or_none()
                if row is not None and row.mode and fold_synth_mode(row.mode) != row.mode:
                    row.mode = fold_synth_mode(row.mode)
                    await db.commit()
        except Exception:
            self._sdlc_synth_mode_folded = False   # retry next tick
            log.warning("sdlc_synthesizer.mode_fold_failed", exc_info=True)
```

- In `_sdlc_synth_tick_once`, first line inside the method (before the schedule check): `await self._sdlc_synth_fold_stored_mode()`.

`docs/product/features/sdlc-governance-evals.md`: in the Non-goals paragraph replace "v1 is deliberately propose-only and human-gated: the SDLC automations do not auto-approve or auto-advance." with "by default (`risk_tolerance: cautious`) the SDLC automations do not auto-approve or auto-advance; a product may raise its tolerance to let low- or medium-risk gates resolve themselves (spec `2026-09-14-roadmap-auto-execution-design.md`), and high-risk gates never do." In the Notes bullet about the engine replace "`AwaitApproval` forever until a human advance" with "`AwaitApproval` until a human advance, or `MergeArtifact`/`Advance` when the product's tolerance covers the gate". Keep the file's line count unchanged or lower.

- [ ] **Step 4: Run tests to verify they pass**

Run: `… $PY -m pytest server/tests/test_sdlc_synthesizer_loop.py server/tests/test_sdlc_wiring.py server/tests/test_automation_oversight.py server/tests/test_automations_api.py -q` (if the last file has another name, `ls server/tests | grep -i automations_api`).
Expected: PASS. Then `… $PY scripts/brand_ratchet.py` (no growth).

- [ ] **Step 5: Commit**

```bash
cat > /tmp/c4.txt <<'EOF'
feat(automations): SDLC Synthesizer modes fold to off | on

Whether a gate resolves itself is the product's risk tolerance, not a
global mode (spec 2026-09-14 D8). Stored propose/auto rows rewrite to on
once, on the first tick.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
EOF
git add server/automations/sdlc_synthesizer.py server/automations/registry.py server/sessions/orchestrator.py docs/product/features/sdlc-governance-evals.md server/tests/test_sdlc_synthesizer_loop.py server/tests/test_sdlc_wiring.py
git commit -q -F /tmp/c4.txt
```

---

### Task 5: Derived initiative priority, in the API and on the map

**Files:**
- Create: `server/pm/priority.py`
- Modify: `server/api/sdlc.py` (`_epic_progress_batch` ~163-262 entry dict; `list_initiatives` and `get_initiative` add `priority`)
- Modify: `server/pm/tree.py` (`rollup_epic_progress` 164-195; `_initiative_node` 259-290)
- Test: `server/tests/test_pm_priority.py` (new), `server/tests/test_pm_tree.py`, `server/tests/test_sdlc_api.py`

**Interfaces:**
- Produces: `initiative_priority(epic_score: float | None, open_leaf_scores: Iterable[float | None]) -> float | None`; epic-progress entries gain `"priority": float | None`; `rollup_epic_progress` rolls it up as a max over leaves; initiative API dicts and map `InitiativeNode` gain `"priority"`.

- [ ] **Step 1: Write the failing tests**

`server/tests/test_pm_priority.py`:

```python
"""Derived initiative priority (spec 2026-09-14 D6/§10): ONE pure rule."""
from server.pm.priority import initiative_priority


def test_epic_score_wins_when_present():
    assert initiative_priority(12.5, [99.0, 1.0]) == 12.5


def test_falls_back_to_the_highest_open_leaf_score():
    assert initiative_priority(None, [3.0, None, 42.0, 7.5]) == 42.0


def test_none_when_nothing_is_scored():
    assert initiative_priority(None, []) is None
    assert initiative_priority(None, [None, None]) is None
```

`server/tests/test_pm_tree.py`, append:

```python
def test_rollup_carries_priority_as_a_max_over_leaves():
    entries = {
        "prog": None,
        "a": {"ref": "o/r#1", "url": "u", "total": 2, "done": 1, "blocked": 0, "needsHuman": 0, "priority": 8.0},
        "b": {"ref": "o/r#2", "url": "u", "total": 1, "done": 0, "blocked": 0, "needsHuman": 0, "priority": None},
        "c": {"ref": "o/r#3", "url": "u", "total": 1, "done": 0, "blocked": 0, "needsHuman": 0, "priority": 21.0},
    }
    out = rollup_epic_progress(entries, {"prog": None, "a": "prog", "b": "prog", "c": "prog"})
    assert out["prog"]["priority"] == 21.0
    assert out["a"]["priority"] == 8.0            # leaves pass through
    only_unscored = rollup_epic_progress(
        {"p": None, "x": {"ref": None, "url": None, "total": 0, "done": 0, "blocked": 0, "needsHuman": 0, "priority": None}},
        {"p": None, "x": "p"})
    assert only_unscored["p"]["priority"] is None


def test_initiative_node_exposes_the_epic_entrys_priority():
    from server.pm.tree import _initiative_node
    epic = {"ref": "o/r#1", "url": "u", "total": 3, "done": 1, "blocked": 0, "needsHuman": 0, "priority": 15.25}
    node = _initiative_node(InitiativeIn("x", "X", "build", "active", None, (), epic), "p")
    assert node["priority"] == 15.25
    bare = _initiative_node(InitiativeIn("y", "Y", "design", "active", None, (), None), "p")
    assert bare["priority"] is None
```

`server/tests/test_sdlc_api.py`: inside `test_list_epic_progress_dedupes_child_mirrored_onto_two_boards`'s seed, give `ready_child` a score by constructing it as `WorkItem(repo="o/r", number=3, kind="issue", state="open", parent_work_item_id=epic_wi.id, reach=10, impact=5, effort=2, confidence=8)` and `done_child` a higher one (`reach=100, impact=10, effort=1, confidence=10`; it is closed so it must NOT count). After the existing assertions on the list row add:

```python
        assert item["priority"] == 200.0          # 10*5*8/2; the closed child's 10000 is ignored
        assert item["epic"]["priority"] == 200.0
```

(Read the test first: `item` is the row for slug `the-epic`; if the variable is named differently, use that name. If another assertion compares the epic dict exactly, add `"priority": 200.0` to the expected dict.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `… $PY -m pytest server/tests/test_pm_priority.py server/tests/test_pm_tree.py server/tests/test_sdlc_api.py -q -k "priority or rollup or dedupes"`
Expected: FAIL (`ModuleNotFoundError`, KeyError `priority`).

- [ ] **Step 3: Implement**

`server/pm/priority.py`:

```python
"""Derived initiative priority (spec 2026-09-14-roadmap-auto-execution-design D6, §10).

Never stored. The epic issue's own RICE score when it is classified, else the
highest score among its OPEN sub-issues, else None. Called from the ONE
epic-progress helper (`server/api/sdlc.py:_epic_progress_batch`) so the
initiatives API, the roadmap map and the synthesizer's visiting order cannot
disagree (constitution VI).
"""
from __future__ import annotations

from collections.abc import Iterable


def initiative_priority(epic_score: float | None,
                        open_leaf_scores: Iterable[float | None]) -> float | None:
    if epic_score is not None:
        return epic_score
    scores = [s for s in open_leaf_scores if s is not None]
    return max(scores) if scores else None
```

`server/api/sdlc.py`, in `_epic_progress_batch`, add the imports `from server.pm.priority import initiative_priority` and `from server.pm.score import rice_score` at the top of the module (check neither is already imported), and in the per-pointer loop build the entry as:

```python
        entry = {
            "ref": f"{repo}#{number}",
            "url": f"https://github.com/{repo}/issues/{number}",
            "total": len(children),
            "done": sum(1 for c in children if c.state == "closed"),
            "blocked": sum(1 for c in children if status_by_child.get(c.id) == STATUS_BLOCKED),
            "needsHuman": sum(1 for c in children if status_by_child.get(c.id) == STATUS_NEEDS_HUMAN),
            # Spec 2026-09-14 D6: computed here, once, for every reader.
            "priority": initiative_priority(
                rice_score(epic_wi.reach, epic_wi.impact, epic_wi.effort, epic_wi.confidence)
                if epic_wi is not None else None,
                (rice_score(c.reach, c.impact, c.effort, c.confidence)
                 for c in children if c.state == "open")),
        }
```

In `list_initiatives` after `d["epic"] = epics.get(i.id)` (line ~357) add `d["priority"] = (d["epic"] or {}).get("priority")`. In `get_initiative` after `result["epic"] = (await _progress_rolled_up(db, [ini], include_children=True))[ini.id]` (line ~472) add `result["priority"] = (result["epic"] or {}).get("priority")`.

`server/pm/tree.py`, `rollup_epic_progress`: leaves pass through unchanged; for a program compute the max:

```python
        totals = {"total": 0, "done": 0, "blocked": 0, "needsHuman": 0}
        priorities: list[float] = []
        for leaf in leaves(iid):
            e = entries.get(leaf)
            if e:
                for k in totals:
                    totals[k] += int(e.get(k) or 0)
                if e.get("priority") is not None:
                    priorities.append(float(e["priority"]))
        base = entry or {"ref": None, "url": None}
        out[iid] = {"ref": base.get("ref"), "url": base.get("url"), **totals,
                    "priority": max(priorities) if priorities else None}
```

Update its docstring: "…A program's `priority` is the max over its leaves (spec 2026-09-14 §10)." In `_initiative_node`'s returned dict add `"priority": epic.get("priority"),` after `"epic": ini.epic,` (note `epic = ini.epic or {}` is already computed at the top).

- [ ] **Step 4: Run tests to verify they pass**

Run: `… $PY -m pytest server/tests/test_pm_priority.py server/tests/test_pm_tree.py server/tests/test_sdlc_api.py server/tests/test_pm_map_api.py server/tests/test_pm_map_nesting_api.py -q`
Expected: PASS. Fix any exact-dict assertion on an epic entry by adding `"priority": None`.

- [ ] **Step 5: Commit**

```bash
cat > /tmp/c5.txt <<'EOF'
feat(pm): derived initiative priority from RICE on the initiatives API and the roadmap map

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
EOF
git add server/pm/priority.py server/api/sdlc.py server/pm/tree.py server/tests/test_pm_priority.py server/tests/test_pm_tree.py server/tests/test_sdlc_api.py
git commit -q -F /tmp/c5.txt
```

---

### Task 6: The synthesizer visits in priority order under the per-product cap

**Files:**
- Modify: `server/automations/sdlc_synthesizer.py` (`_to_views`; `SdlcSynthesizerLoop.__init__` + `_run`; `SdlcSynthesizer.status`)
- Modify: `server/sessions/orchestrator.py` (`_sdlc_list_initiatives` ~8153; loop construction ~1169; new `_sdlc_read_policies`)
- Test: `server/tests/test_sdlc_synthesizer_loop.py`, `server/tests/test_sdlc_wiring.py`

**Interfaces:**
- Consumes: `initiative_priority` via `_epic_progress_batch` (Task 5), `effective_policy`/`policies_by_slug` (existing), the `max_initiatives_in_flight` key (Task 2).
- Produces: loader rows gain `project_slug: str | None`, `priority: float | None`, `epic_total: int`, `epic_done: int` and arrive sorted by `(priority is None, -priority, created_at)`; loop seam `read_policies: Callable[[], Awaitable[dict[str, dict]]]` (slug → effective policy); `is_producing(stage: str | None, artifacts: list[dict]) -> bool`; counts gain `"queued"`.

- [ ] **Step 1: Write the failing loop tests**

In `server/tests/test_sdlc_synthesizer_loop.py`: give `Recorder` a `self.policies: dict[str, dict] = {}` attribute and

```python
    async def read_policies(self):
        return self.policies
```

pass `read_policies=rec.read_policies` in `_loop`, and let `_ini` take extra keys: `def _ini(stage, status="active", **extra): return {..., **extra}`. Append:

```python
def _prio(iid, slug, stage, priority, project="viberpit"):
    return {"id": iid, "slug": slug, "repo": "o/r", "title": slug, "stage": stage,
            "status": "active", "origin": "human", "origin_ref": None,
            "project_slug": project, "priority": priority, "epic_total": 0, "epic_done": 0}


@pytest.mark.asyncio
async def test_visits_in_the_order_the_loader_sorted_and_caps_new_spawns_per_product():
    """Two design-stage initiatives, cap 1: only the first (higher priority, first
    in the list) spawns; the second is counted as queued, not proposed."""
    rec = Recorder([_prio("i1", "hot", "design", 40.0), _prio("i2", "cold", "design", 2.0)])
    rec.policies = {"viberpit": {"max_initiatives_in_flight": 1}}
    ctx = Ctx()
    await _loop(rec, ctx).tick()
    assert rec.spawned == [("hot", "spec")]
    assert SdlcSynthesizer().status(ctx).counts["queued"] == 1
    assert SdlcSynthesizer().status(ctx).counts["proposed"] == 1


@pytest.mark.asyncio
async def test_an_initiative_already_producing_counts_against_the_cap():
    rec = Recorder([_prio("i1", "busy", "design", 40.0), _prio("i2", "next", "design", 30.0)])
    rec.artifacts["i1"] = [{"id": "a1", "kind": "spec", "commit_sha": None, "session_id": "s1"}]
    rec.policies = {"viberpit": {"max_initiatives_in_flight": 1}}
    await _loop(rec).tick()
    assert rec.spawned == []                       # i1 is producing; i2 queued behind it


@pytest.mark.asyncio
async def test_a_merged_and_parked_initiative_does_not_hold_a_slot():
    rec = Recorder([_prio("i1", "parked", "design", 40.0), _prio("i2", "next", "design", 30.0)])
    rec.artifacts["i1"] = [{"id": "a1", "kind": "spec", "commit_sha": "cafe", "session_id": "s1"}]
    rec.policies = {"viberpit": {"max_initiatives_in_flight": 1}}
    await _loop(rec).tick()
    assert rec.spawned == [("next", "spec")]


@pytest.mark.asyncio
async def test_cap_zero_pauses_new_artifacts_but_never_materialization():
    rec = Recorder([_prio("i1", "plan-done", "plan", 5.0), _prio("i2", "fresh", "design", 50.0)])
    rec.artifacts["i1"] = [{"id": "a1", "kind": "plan", "commit_sha": "cafe", "session_id": "s1"}]
    rec.policies = {"viberpit": {"max_initiatives_in_flight": 0}}
    await _loop(rec).tick()
    assert rec.spawned == []
    assert rec.materialized == ["plan-done"]


@pytest.mark.asyncio
async def test_products_are_capped_independently_and_a_homeless_initiative_uses_defaults():
    rec = Recorder([_prio("i1", "a", "design", 9.0, project="viberpit"),
                    _prio("i2", "b", "design", 8.0, project="kindash"),
                    _prio("i3", "c", "design", 7.0, project=None)])
    rec.policies = {"viberpit": {"max_initiatives_in_flight": 1}, "kindash": {"max_initiatives_in_flight": 1}}
    await _loop(rec).tick()
    assert rec.spawned == [("a", "spec"), ("b", "spec"), ("c", "spec")]


def test_is_producing_rule():
    from server.automations.sdlc_synthesizer import is_producing
    assert is_producing("design", [{"kind": "spec", "commit_sha": None}]) is True
    assert is_producing("design", [{"kind": "spec", "commit_sha": "cafe"}]) is False
    assert is_producing("design", [{"kind": "intent", "commit_sha": None}]) is False   # not this stage's kind
    assert is_producing("build", [{"kind": "plan", "commit_sha": None}]) is False      # not a front-chain stage
    assert is_producing("design", []) is False


def test_to_views_carries_epic_progress():
    from server.automations.sdlc_synthesizer import _to_views
    view, _ = _to_views(_prio("i1", "x", "build", None) | {"epic_total": 4, "epic_done": 3}, [])
    assert (view.epic_total, view.epic_done) == (4, 3)
    legacy, _ = _to_views(_ini("build"), [])
    assert (legacy.epic_total, legacy.epic_done) == (0, 0)
```

Also update `test_observation_reports_gate_then_real_zero_and_failure`: the empty-run counts become `{"initiatives": 0, "proposed": 0, "awaiting": 0, "queued": 0}`.

- [ ] **Step 2: Write the failing loader tests**

In `server/tests/test_sdlc_wiring.py` append:

```python
def test_sdlc_list_initiatives_sorts_by_priority_then_age_and_carries_progress():
    """Spec 2026-09-14 D7: highest priority first, unscored last, oldest first
    on ties; each row carries project_slug, priority and epic counts."""
    from datetime import datetime, timedelta, timezone
    from server.app import create_app
    from server.db import db_session
    from server.db.models import Initiative, Project, WorkItem

    async def _seed_and_list(orch):
        t0 = datetime.now(timezone.utc) - timedelta(days=3)
        async with db_session() as db:
            p = Project(slug="viberpit", name="ViberPit")
            db.add(p)
            await db.flush()
            epic = WorkItem(repo="o/r", number=1, kind="issue", state="open", is_epic=True)
            db.add(epic)
            await db.flush()
            db.add(WorkItem(repo="o/r", number=2, kind="issue", state="open",
                            parent_work_item_id=epic.id, reach=10, impact=5, effort=2, confidence=8))
            db.add(WorkItem(repo="o/r", number=3, kind="issue", state="closed",
                            parent_work_item_id=epic.id))
            db.add_all([
                Initiative(slug="unscored-old", repo="o/r", title="u", stage="design",
                           status="active", project_id=p.id, created_at=t0),
                Initiative(slug="scored", repo="o/r", title="s", stage="build",
                           status="active", project_id=p.id, epic_repo="o/r", epic_number=1,
                           created_at=t0 + timedelta(days=1)),
                Initiative(slug="unscored-new", repo="o/r", title="n", stage="design",
                           status="active", project_id=p.id, created_at=t0 + timedelta(days=2)),
            ])
        return await orch._sdlc_list_initiatives()

    with tempfile.TemporaryDirectory() as tmp, temp_env(), patch.dict(
        os.environ, {"CCSWITCH_DB_URL": f"sqlite+aiosqlite:///{tmp}/wiring.db"}
    ):
        orch = _make_orch()
        app = create_app(orchestrator=orch)
        client = TestClient(app, raise_server_exceptions=False)
        with client:
            rows = client.portal.call(_seed_and_list, orch)
    assert [r["slug"] for r in rows] == ["scored", "unscored-old", "unscored-new"]
    scored = rows[0]
    assert (scored["project_slug"], scored["priority"], scored["epic_total"], scored["epic_done"]) == \
        ("viberpit", 200.0, 2, 1)
    assert rows[1]["priority"] is None and rows[1]["project_slug"] == "viberpit"
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `… $PY -m pytest server/tests/test_sdlc_synthesizer_loop.py server/tests/test_sdlc_wiring.py -q`
Expected: FAIL (`ImportError: is_producing`, `TypeError: unexpected keyword read_policies`, order mismatch).

- [ ] **Step 4: Implement the loop side**

`server/automations/sdlc_synthesizer.py`:

1. Add `from server.sdlc.engine import STAGE_ARTIFACT` to the engine import; add `ReadPolicies = Callable[[], Awaitable["dict[str, dict]"]]` beside the other seam types; add `from .product_policy import effective_policy` (module-level import is fine; `product_policy` does not import this module).
2. `_to_views`: pass `epic_total=int(ini.get("epic_total") or 0), epic_done=int(ini.get("epic_done") or 0)` into `InitiativeView`.
3. New pure helper after `_to_views`:

```python
def is_producing(stage: str | None, artifacts: list[dict]) -> bool:
    """Whether an initiative currently holds a front-chain slot (spec 2026-09-14
    D7): its current stage's artifact exists and is not merged yet — a live
    artifact session or an open PR. A merged-and-parked initiative does not
    count: the human is the bottleneck there, not capacity."""
    kind = STAGE_ARTIFACT.get(stage or "")
    if kind is None:
        return False
    return any(a.get("kind") == kind and a.get("commit_sha") is None for a in artifacts)
```

4. `SdlcSynthesizerLoop.__init__`: add keyword `read_policies: ReadPolicies | None = None` and store `self._read_policies = read_policies`.
5. `_run` becomes:

```python
    async def _run(self) -> None:
        mode, config = await self._read_config()
        if mode == "off":
            return
        initiatives = await self._list_initiatives()
        policies = await self._read_policies() if self._read_policies is not None else {}
        counts = {"initiatives": len(initiatives), "proposed": 0, "awaiting": 0, "queued": 0}
        # Front-chain slots per product, counted before any spawn this tick so
        # the cap is judged against the whole pool, not the rows visited so far.
        artifacts_by_id = {ini["id"]: await self._list_artifacts(ini["id"]) for ini in initiatives}
        producing: dict[str | None, int] = {}
        for ini in initiatives:
            if is_producing(ini.get("stage"), artifacts_by_id[ini["id"]]):
                key = ini.get("project_slug")
                producing[key] = producing.get(key, 0) + 1
        for ini in initiatives:
            artifacts = artifacts_by_id[ini["id"]]
            state, arts = _to_views(ini, artifacts)
            # Phase 1: literal cautious; phase 2 passes the product's effective tolerance.
            action = next_action(state, arts, mode, "cautious")
            if isinstance(action, AwaitApproval) or (
                    isinstance(action, NeedsHuman)
                    and (state.status == "blocked" or state.stage == "build")):
                counts["awaiting"] += 1
            if isinstance(action, Propose) and action.kind in ("spec", "plan"):
                key = ini.get("project_slug")
                cap = int(effective_policy(policies.get(key) if key else None)["max_initiatives_in_flight"])
                if producing.get(key, 0) >= cap:
                    counts["queued"] += 1
                else:
                    rec = await self._spawn(ini, action.kind)
                    if rec is not None:
                        producing[key] = producing.get(key, 0) + 1
                        slug = str(ini.get("slug") or "")
                        await self._record_artifact(
                            initiative_id=ini["id"], kind=action.kind,
                            repo_path=artifact_path(slug, action.kind),
                            session_id=getattr(rec, "id", None),
                            model=getattr(rec, "model", None))
                        await self._set_status(ini["id"], "awaiting_human")
                        await self._emit_proposed(slug, action.kind, rec)
                        counts["proposed"] += 1
            elif isinstance(action, Materialize):
                outcome = await self._materialize(ini)
                if outcome is not None:
                    counts[outcome] += 1
            await self._enrich(artifacts)
        publish_observation(self._ctx, "sdlc_synthesizer_counts", counts)
```

Note `effective_policy(policies.get(key))` — `policies` values are already effective when they come from `policies_by_slug`, but the fakes pass partial dicts; `effective_policy` on an already-complete dict is idempotent, so calling it here keeps one reading. For `key is None` (no owning product) the defaults apply (cap 2).

6. `SdlcSynthesizer.status`: text becomes
```python
        queued = counts.get("queued", 0)
        text = f"{proposed} proposed this refresh · {awaiting} awaiting human action"
        if queued:
            text += f" · {queued} queued behind the cap"
```
and pass `text=text`.

- [ ] **Step 5: Implement the loader and seam**

`server/sessions/orchestrator.py`:

1. Loop construction (~line 1169): add `read_policies=self._sdlc_read_policies,`.
2. New seam beside `_sdlc_list_initiatives`:

```python
    async def _sdlc_read_policies(self) -> dict[str, dict]:
        """Loop seam: slug → effective product policy (the ONE reading,
        `server/automations/product_policy.py:load_policies`)."""
        from server.automations.product_policy import load_policies
        async with db_session() as db:
            return await load_policies(db)
```

3. `_sdlc_list_initiatives`: replace the body's return with a version that joins the product slug, fetches epic progress with the shared helper, and sorts:

```python
        from sqlalchemy import select
        from server.api.sdlc import _epic_progress_batch
        from server.db.models import Initiative, Project
        async with db_session() as db:
            rows = (await db.execute(select(Initiative).where(
                Initiative.status != "done", Initiative.stage != "done"))).scalars().all()
            parent_ids = set((await db.execute(select(Initiative.parent_initiative_id).where(
                Initiative.parent_initiative_id.isnot(None)))).scalars().all())
            leaves = [r for r in rows if r.id not in parent_ids]
            project_ids = {r.project_id for r in leaves if r.project_id}
            slug_by_project = {p.id: p.slug for p in (await db.execute(
                select(Project).where(Project.id.in_(project_ids)))).scalars().all()} if project_ids else {}
            progress = await _epic_progress_batch(db, leaves, include_children=False)
        out = []
        for r in leaves:
            entry = progress.get(r.id) or {}
            out.append({"id": r.id, "slug": r.slug, "repo": r.repo, "title": r.title,
                        "stage": r.stage, "status": r.status, "origin": r.origin,
                        "origin_ref": r.origin_ref,
                        # The engine decides plan-stage materialization off this pointer
                        # (spec 003 §7b) — without it every merged plan looks un-decomposed.
                        "epic_repo": r.epic_repo, "epic_number": r.epic_number,
                        # Spec 2026-09-14 D7: the loop caps per product and the engine's
                        # build gate reads the epic's completion.
                        "project_slug": slug_by_project.get(r.project_id) if r.project_id else None,
                        "priority": entry.get("priority"),
                        "epic_total": int(entry.get("total") or 0),
                        "epic_done": int(entry.get("done") or 0),
                        "_created_at": r.created_at})
        # Highest priority first, unscored last, oldest first on ties (D6/D7).
        out.sort(key=lambda d: (d["priority"] is None, -(d["priority"] or 0.0), d["_created_at"]))
        for d in out:
            d.pop("_created_at", None)
        return out
```

Update the docstring: "…Rows arrive sorted by priority desc (unscored last), then age, and carry `project_slug`, `priority`, `epic_total`, `epic_done` (spec 2026-09-14 D6/D7)." If importing `server.api.sdlc` from the orchestrator creates an import cycle at module load, keep the import inside the method (it is) — `server/api/pm_tree.py` already imports the helper the same way; confirm with `grep -n "_progress_rolled_up\|_epic_progress_batch" server/api/pm_tree.py`.

- [ ] **Step 6: Run tests to verify they pass**

Run: `… $PY -m pytest server/tests/test_sdlc_synthesizer_loop.py server/tests/test_sdlc_wiring.py server/tests/test_sdlc_api.py -q` and `$PY -m mypy server/automations/sdlc_synthesizer.py --ignore-missing-imports`.
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
cat > /tmp/c6.txt <<'EOF'
feat(automations): synthesizer visits initiatives in priority order under a per-product cap

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
EOF
git add server/automations/sdlc_synthesizer.py server/sessions/orchestrator.py server/tests/test_sdlc_synthesizer_loop.py server/tests/test_sdlc_wiring.py
git commit -q -F /tmp/c6.txt
```

---

### Task 7: Materialization seeds sub-issue RICE from the source issue

**Files:**
- Modify: `server/pm/materialize_apply.py` (~lines 310-335, the sub-issue overlay loop; imports)
- Test: `server/tests/test_pm_materialize_apply.py` (`_seed` gains `origin_ref`/`source_rice`; two new tests)

**Interfaces:**
- Consumes: `server/pm/refs.py:split_ref` (read its docstring first; it returns the `(repo, number)` pair for `owner/repo#N` or `None`), `rice_score`.
- Produces: created sub-issue `WorkItem`s carry `reach/impact/effort/confidence` copied from the `origin_ref` issue when that issue is mirrored and fully scored.

- [ ] **Step 1: Write the failing tests**

In `server/tests/test_pm_materialize_apply.py`, extend `_seed`'s signature with `origin_ref: str | None = None, source_rice: tuple[int, int, int, int] | None = None`, pass `origin_ref=origin_ref` to the `Initiative(...)`, and before `db.add(ini)` add:

```python
        if origin_ref and source_rice:
            repo, number = origin_ref.rsplit("#", 1)
            r, i, e, c = source_rice
            db.add(WorkItem(repo=repo, number=int(number), kind="issue", state="open",
                            reach=r, impact=i, effort=e, confidence=c))
```

(`WorkItem` is already imported in that test module; check.) Append two tests modelled on `test_apply_overlays_the_initiative_and_the_work_items`; the module's `_apply(proposal_id, gh)` helper (line ~148) runs `materialize_apply.apply` for you:

```python
@pytest.mark.asyncio
async def test_sub_issues_inherit_the_source_issues_rice(db_env):
    """Spec 2026-09-14 D6: a plan approved off a scored intake issue yields tasks
    that rank WITH it in the queue, not last as unscored rows."""
    ini_id, prop_id = await _seed(db_env, origin_ref="o/r#5", source_rice=(10, 5, 2, 8))
    await _apply(prop_id, FakeGh())
    async with db_session() as db:
        rows = (await db.execute(select(WorkItem).where(
            WorkItem.repo == "o/r", WorkItem.classified_by == "materialize"))).scalars().all()
    assert [w.number for w in rows] == [102, 103, 104]
    assert all((w.reach, w.impact, w.effort, w.confidence) == (10, 5, 2, 8) for w in rows)


@pytest.mark.asyncio
async def test_sub_issues_stay_unscored_without_a_scored_source(db_env):
    ini_id, prop_id = await _seed(db_env, origin_ref="o/r#5")   # no mirrored source row
    await _apply(prop_id, FakeGh())
    async with db_session() as db:
        rows = (await db.execute(select(WorkItem).where(
            WorkItem.classified_by == "materialize"))).scalars().all()
    assert rows and all(w.reach is None and w.effort is None for w in rows)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `… $PY -m pytest server/tests/test_pm_materialize_apply.py -q -k rice`
Expected: the first FAILS on the RICE assertion (values are None); the second passes already (keep it as the regression guard).

- [ ] **Step 3: Implement**

In `server/pm/materialize_apply.py`, add imports `from server.pm.refs import split_ref` and `from server.pm.score import rice_score`. Immediately before the `for idx, task in enumerate(decomposition.tasks):` loop insert:

```python
    # Spec 2026-09-14 D6: seed each task's RICE from the initiative's source issue
    # (the intake or adopted issue in ``origin_ref``) when it is mirrored and fully
    # scored. The epic itself was created seconds ago and has no score yet; with no
    # scored source the tasks stay unscored — the queue never invents numbers.
    seed: tuple[int, int, int, int] | None = None
    source_ref = split_ref(ini.origin_ref or "")
    if source_ref is not None:
        src_repo, src_number = source_ref
        source = (await db.execute(select(WorkItem).where(
            WorkItem.repo == src_repo, WorkItem.number == src_number,
            WorkItem.kind == "issue"))).scalar_one_or_none()
        if source is not None and rice_score(source.reach, source.impact, source.effort,
                                             source.confidence) is not None:
            seed = (source.reach, source.impact, source.effort, source.confidence)  # type: ignore[assignment]
```

and inside the loop, after `item.classified_at = now`:

```python
        if seed is not None:
            item.reach, item.impact, item.effort, item.confidence = seed
```

Replace the comment block "RICE is left unset — Initiative carries no RICE columns…" with "RICE comes from the source issue when it is scored (see ``seed`` above); otherwise unset." `split_ref` returns `tuple[str, int] | None` (`server/pm/refs.py:13`), which the code above unpacks directly.

- [ ] **Step 4: Run tests to verify they pass**

Run: `… $PY -m pytest server/tests/test_pm_materialize_apply.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
cat > /tmp/c7.txt <<'EOF'
feat(pm): materialized sub-issues inherit the source issue's RICE

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
EOF
git add server/pm/materialize_apply.py server/tests/test_pm_materialize_apply.py
git commit -q -F /tmp/c7.txt
```

---

### Task 8: The web shows priority and sorts by it inside a tier

**Files:**
- Modify: `web/src/api/roadmap.ts:46-66` (`InitiativeNode`)
- Modify: `web/src/features/roadmap/rows/InitiativeCard.tsx:106-111`
- Modify: `web/src/features/roadmap/RoadmapListColumn.tsx:233-236`
- Test: `web/src/features/roadmap/RoadmapListColumn.test.tsx`; any test fixture that builds an `InitiativeNode` literal (typecheck lists them)

**Interfaces:**
- Consumes: `InitiativeNode.priority: number | null` from the map payload (Task 5).

- [ ] **Step 1: Add the failing test**

`web/src/features/roadmap/__fixtures__/map.ts`: the `ini()` helper's defaults gain `priority: null` (beside `epic: null`) so every existing fixture typechecks. In `web/src/features/roadmap/RoadmapListColumn.test.tsx`, which already has `mount(over)` (renders `RoadmapListColumn` with the `MAP` fixture), add `ini, product` to the `./__fixtures__/map` import and append inside the `describe`:

```tsx
  it('orders initiatives of one tier by priority, highest first, unscored last, then title', () => {
    const map: RoadmapMap = {
      ...MAP,
      products: [product({
        slug: 'kindash', name: 'KinDash',
        initiatives: [
          ini({ slug: 'low', title: 'Alpha', projectSlug: 'kindash', stage: 'build', priority: 5 }),
          ini({ slug: 'high', title: 'Zulu', projectSlug: 'kindash', stage: 'build', priority: 40 }),
          ini({ slug: 'none', title: 'Beta', projectSlug: 'kindash', stage: 'build', priority: null }),
        ],
      })],
    }
    mount({ scope: 'initiatives', map })
    const names = screen.getAllByRole('button', { name: /^Open initiative / })
      .map((b) => b.getAttribute('aria-label') ?? '')
    expect(names).toEqual(['Open initiative Zulu', 'Open initiative Alpha', 'Open initiative Beta'])
    // The row shows the number it sorted by.
    expect(screen.getByText(/priority 40/)).toBeInTheDocument()
  })
```

(All three fixtures share tier `in-flight` from `ini()`'s default and stage `build`, so only priority and title can order them. Check `MAP`'s type for the products key name; if the fixture's `RoadmapMap` nests products differently, spread accordingly.)

- [ ] **Step 2: Run the test to verify it fails**

Run: `(cd web && npx vitest run src/features/roadmap/RoadmapListColumn.test.tsx)`
Expected: FAIL (order by title puts Alpha before Zulu; `priority` unknown property in typecheck).

- [ ] **Step 3: Implement**

`web/src/api/roadmap.ts`, in `InitiativeNode` after `epic`:

```ts
  /** Derived priority (server/pm/priority.py): the epic's RICE score, else the
   *  highest open sub-issue score, else null. Never stored, never recomputed here. */
  priority: number | null
```

`web/src/features/roadmap/rows/InitiativeCard.tsx`, the meta line becomes:

```tsx
        <span className="block truncate text-xs text-ink-muted">
          {reason
            ? <span className={TIER_TEXT[initiative.tier]}>{reason}{next ? ` → ${stageLabel(next)}` : ''}</span>
            : tierLabel(initiative.tier)}
          {epic ? ` · ${epic.done}/${epic.total} done` : ''}
          {initiative.priority != null ? ` · priority ${initiative.priority}` : ''}
        </span>
```

`web/src/features/roadmap/RoadmapListColumn.tsx`, add above `INITIATIVE_SPEC`:

```ts
/** Higher priority first; an unscored initiative sorts after every scored one. */
const priorityDesc = (a: InitiativeNode, b: InitiativeNode) =>
  (b.priority ?? -Infinity) - (a.priority ?? -Infinity)
```

and the tier sort becomes:

```ts
      tier: (a, b) => TIER_RANK[a.initiative.tier] - TIER_RANK[b.initiative.tier]
        || priorityDesc(a.initiative, b.initiative)
        || initiativeTitle(a.initiative).localeCompare(initiativeTitle(b.initiative)),
```

Run `(cd web && npm run typecheck)`; add `priority: null` to any remaining `InitiativeNode` literal it flags (`InitiativePage.test.tsx` builds its own from the detail API type, which is unchanged; only map-shaped literals need it).

- [ ] **Step 4: Run the web gate**

Run: `(cd web && npm run typecheck && npm run lint && npm run test && npm run build)` — check each command's exit status separately (never pipe through `tail`).
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
cat > /tmp/c8.txt <<'EOF'
feat(web): roadmap initiative rows show derived priority and sort by it within a tier

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
EOF
git add web/src/api/roadmap.ts web/src/features/roadmap
git commit -q -F /tmp/c8.txt
```

---

### Task 9: Whole-plan verification and the PR

**Files:** none new.

- [ ] **Step 1: Run the full server gate**

```bash
ruff check src/ server/
$PY -m mypy server/ --ignore-missing-imports
env -u CCSWITCH_TAILNET_ALLOWLIST -u CCSWITCH_DAEMON_ROLE -u CCSWITCH_SESSIOND_ENDPOINT -u CCSWITCH_SESSIOND_CLIENT_CERT -u CCSWITCH_SESSIOND_CLIENT_KEY -u CCSWITCH_SESSIOND_CA CCSWITCH_DEV=1 $PY -m pytest tests/ -q
env -u CCSWITCH_TAILNET_ALLOWLIST -u CCSWITCH_DAEMON_ROLE -u CCSWITCH_SESSIOND_ENDPOINT -u CCSWITCH_SESSIOND_CLIENT_CERT -u CCSWITCH_SESSIOND_CLIENT_KEY -u CCSWITCH_SESSIOND_CA CCSWITCH_DEV=1 $PY -m pytest server/tests/ -n auto --dist loadfile --timeout=300 -q 2>&1 | tee /tmp/roadmap-autoexec-p1-server-tests.log
```

Run the full server suite ONCE, in the background, with the test-slot acquire loop from `.agents/instructions/core.md` in the same shell. Known environment-only failures on rocky/dobby are listed in memory (`test_model_switch_restart`, `test_orchestrator_lazy_resume`, `test_session_id_persistence`, the UDS daemon spawn, ws timing); anything else is real.

- [ ] **Step 2: Run the web gate** (as in Task 8 Step 4) and `$PY scripts/brand_ratchet.py`.

- [ ] **Step 3: Open the PR**

```bash
git fetch -q origin && git rebase origin/master
git push -u origin HEAD
cat > /tmp/pr-p1.md <<'EOF'
Phase 1 of `docs/superpowers/specs/2026-09-14-roadmap-auto-execution-design.md` (§13): the behaviour-preserving substrate.

- `server/sdlc/gates.py`: the gate registry (id, risk class, reverse action) and `auto_resolves`; `CCSWITCH_GATE_AUTORESOLVE_ENABLED` kill switch.
- Product policy gains `risk_tolerance` (default `cautious`) and `max_initiatives_in_flight` (default 2).
- The stage engine decides under a tolerance and returns `MergeArtifact` / `Advance` for covered gates (not executed until phase 2); the dead `propose_only` seam is gone.
- SDLC Synthesizer modes fold to `off | on`; stored legacy modes rewrite once.
- Derived initiative priority (RICE) on the initiatives API and the roadmap map; the synthesizer visits in that order under the per-product cap; materialized sub-issues inherit the source issue's RICE.
- Web: initiative rows show priority and sort by it inside a tier.

With every product at `cautious` nothing auto-resolves. Phase 2 (low-risk gate execution, chips, status lines) and phase 3 (medium-risk gates) follow as their own plans.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
gh pr create --title "feat(sdlc,pm): roadmap auto-execution — phase 1 (gates, tolerance-aware engine, priority order, front-chain cap)" --body-file /tmp/pr-p1.md
gh pr merge --squash --auto
```

Before `gh pr create`, check in-flight PRs touching the same files with the GraphQL query in `.agents/instructions/core.md` § "Before implementing, check whether the same work is already in flight".

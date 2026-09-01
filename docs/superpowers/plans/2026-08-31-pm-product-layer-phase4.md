# PM Product Layer — Phase 4 (Product Surfaces + Advanced Structure) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Land the two product-facing surfaces (per-project **Product Board** and the priority **Roadmap**) plus the advanced-structure work that feeds them: a single-source-of-truth RICE-lite scorer (`server/pm/scoring.py`), the optional **discovery** SDLC stage (engine + prompt), cross-project initiative behavior over the existing M2M, and the board/roadmap read APIs + feature-link mutations. No new DB tables — phase 1's schema (migration `0012_pm_product_layer`) already ships everything this phase needs.

**Architecture:** Extends the SDLC/PM spine in place. `rice_score` is a pure function reused by every ranked surface (roadmap, backlog) — never inlined a second time (constitution VI). The discovery stage extends the existing `STAGE_SEQUENCE`/`INITIATIVE_STAGES` constants and the pure `next_action` engine (missing discovery = skipped, never blocking), with a `build_discovery_prompt` alongside the existing prompt builders. New read APIs live on the phase-1 `server/api/pm.py` router; `PATCH /initiatives/{slug}/scores` extends the phase-1 `server/api/pm.py` surface (scores live on the `Initiative` row). Web reuses the metro-track idiom (`InitiativeTrack`), the `ArtifactLink`-at-SHA idiom, and the React-Query + WebSocket-invalidation (`useInitiativesLive`) pattern.

**Tech Stack:** Python 3.12 / FastAPI / SQLAlchemy 2 async · React + Vite + TS + React Query + react-router-dom · vitest + Testing Library.

**Landing order (see C8):** phase 4 lands **LAST** — after phases 2 and 3. Rebase onto `master` before pushing, and append the Roadmap tab **after** the phase-3 Triage tab (its `TabId` union member / `tabDefs.ts` entry / `DesktopShell` branch / `App.tsx` route each go after phase-3's triage additions) to avoid a same-line merge conflict with phase 3 on the shared web tab-wiring files.

**Spec:** `docs/superpowers/specs/2026-08-31-pm-product-layer-design.md` — §3 (discovery-stage note), §7 (API), §8 (surfaces), §10.4 (phase 4), §11 (out of scope). Read it first; this plan argues from it.

**Consumes from earlier phases (exact names — do not rename):** models `Project, ProjectRepo, ProjectEdge, Feature, FeatureLink, WorkItem, TriageProposal, InitiativeRepo, InitiativeFeature`, `Initiative.project_id/reach/impact/effort/confidence`; constants `PROJECT_ONBOARDING_STATES, FEATURE_LIFECYCLE_STATES, FEATURE_LINK_KINDS, WORK_ITEM_KINDS, WORK_ITEM_TRIAGE_STATES`; the phase-1 router `server/api/pm.py` (`/api/pm`), the phase-3 triage inbox route `/triage`, the phase-1 web `web/src/api/pm.ts` + hooks in `web/src/api/hooks.ts` (`QK.projects = ['pm','projects']`), and the existing `web/src/initiatives/InitiativeTrack.tsx`.

## Global Constraints

- TDD throughout (constitution V): failing test first, then minimal impl, then green, then commit.
- Propose-only, human-gated. NO `auto` mode for any PM automation in v1. Automations default to `mode: off`; never auto-enable at deploy. (This phase adds NO automation, but the discovery-stage engine work must preserve the propose-only gate.)
- Branch off `origin/master` first: `git fetch && git checkout -b feat/pm-product-layer-phase4 origin/master`. NEVER branch off local `master`.
- camelCase JSON at the API boundary; snake_case in Python. 4xx `detail` is an object `{error, message}` (mirror `server/api/sdlc.py`).
- Layered truth: GitHub owns work items, repo markdown owns feature/discovery docs, ccswitch DB owns only index/overlay/relations. Never introduce a second source of truth.
- **NO NEW DB TABLES.** `discovery` is a constant + the existing `InitiativeArtifact` table (`kind="discovery"`, `docs/sdlc/<slug>/discovery.md`); cross-project behavior rides the existing `InitiativeRepo`/`InitiativeFeature` M2M. If you think you need a new column, re-read phase 1's model list — you don't.
- Python tests run via the prebuilt venv (this phase touches only `server/`, so no `PYTHONPATH` override needed):
  `env -u CCSWITCH_TAILNET_ALLOWLIST -u CCSWITCH_SESSIOND_HOST -u CCSWITCH_SESSIOND_PORT -u CCSWITCH_SESSIOND_CLIENT_CERT -u CCSWITCH_SESSIOND_CLIENT_KEY -u CCSWITCH_SESSIOND_CA CCSWITCH_DEV=1 /home/tony/GitHub/ccswitch/.venv/bin/python -m pytest <path> -v`
  (the "`...`" prefix in run-commands below stands for this exact env prefix + `-m pytest`).
- Web commands run in `web/` (symlink `node_modules` from `/home/tony/GitHub/ccswitch/web/node_modules` if absent): `npx vitest run`, `npx tsc --noEmit`, `npx eslint .`
- Before EVERY push, the full four-step Python gate: `ruff check src/ server/` · `mypy server/ --ignore-missing-imports` · `pytest tests/` · `pytest server/tests/` (serialize full-suite runs — concurrent runs on this host get SIGKILLed). Web: `npx vitest run`, `npx tsc --noEmit`, `npx eslint .`
- Commit format: `feat(pm): …` / `test(pm): …`, ending with `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`.

---

### Task 1: Pure RICE-lite scorer (`server/pm/scoring.py`)

**Files:**
- Create: `server/pm/scoring.py`
- Test: `server/tests/test_pm_scoring.py` (create)

**Interfaces:**
- Produces: `rice_score(reach: float | int | None, impact: float | int | None, confidence: float | int | None, effort: float | int | None) -> float` — the **single source of truth** for RICE-lite (`reach × impact × confidence / effort`, with a divide-by-zero/None guard). Every ranked surface (roadmap, backlog) imports this; nobody re-inlines the formula.
- Consumes: nothing (pure, stdlib only).

- [ ] **Step 1: Write the failing tests**

Create `server/tests/test_pm_scoring.py`. Table-test the formula, the divide-by-zero guard, and None handling:

```python
"""Pure RICE-lite scorer tests (spec §8: reach × impact × confidence / effort)."""
import pytest

from server.pm.scoring import rice_score


@pytest.mark.parametrize("reach,impact,confidence,effort,expected", [
    (3, 2, 2, 1, 12.0),          # 3*2*2/1
    (10, 3, 1, 2, 15.0),         # 10*3*1/2
    (1, 1, 1, 1, 1.0),
    (5, 4, 3, 6, 10.0),          # 60/6
    (2, 2, 2, 4, 2.0),           # 8/4 — integer-clean
])
def test_rice_formula(reach, impact, confidence, effort, expected):
    assert rice_score(reach, impact, confidence, effort) == pytest.approx(expected)


@pytest.mark.parametrize("effort", [0, None])
def test_zero_or_missing_effort_is_zero_not_error(effort):
    # An un-estimated / zero-effort item cannot be ranked; it sorts to the bottom
    # (0.0), never raises ZeroDivisionError, and never poisons an ORDER BY.
    assert rice_score(3, 2, 2, effort) == 0.0


@pytest.mark.parametrize("reach,impact,confidence", [
    (None, 2, 2), (3, None, 2), (3, 2, None), (None, None, None),
])
def test_missing_numerator_input_is_zero(reach, impact, confidence):
    # Any missing scoring input means "unscored" → 0.0 (sorts last), not a crash.
    assert rice_score(reach, impact, confidence, 2) == 0.0


def test_returns_float_even_for_int_inputs():
    result = rice_score(4, 1, 1, 2)
    assert isinstance(result, float) and result == 2.0


def test_is_pure_no_side_effects():
    a = rice_score(3, 2, 2, 1)
    b = rice_score(3, 2, 2, 1)
    assert a == b == 12.0
```

- [ ] **Step 2: Run to verify failure**

Run: `... server/tests/test_pm_scoring.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'server.pm.scoring'`.

- [ ] **Step 3: Implement**

Create `server/pm/scoring.py`:

```python
"""RICE-lite prioritization score — the single source of truth (spec §8).

score = reach × impact × confidence / effort

Every ranked surface (roadmap, per-feature backlog) imports ``rice_score`` and
NEVER re-inlines the arithmetic (constitution VI). Pure, total, no I/O: a
missing or zero divisor (unestimated effort) yields 0.0 so the item sorts to
the bottom of an ORDER BY instead of raising or poisoning the sort. A missing
numerator input is likewise treated as "unscored" → 0.0.
"""
from __future__ import annotations

Number = float | int | None


def rice_score(
    reach: Number,
    impact: Number,
    confidence: Number,
    effort: Number,
) -> float:
    """Return the RICE-lite score, or 0.0 when any input is missing or effort
    is zero. Always a float, so callers can compare/sort uniformly."""
    if reach is None or impact is None or confidence is None:
        return 0.0
    if effort is None or effort == 0:
        return 0.0
    return float(reach) * float(impact) * float(confidence) / float(effort)
```

- [ ] **Step 4: Run tests**

Run: `... server/tests/test_pm_scoring.py -v`
Expected: PASS (13 parametrized cases + 2 = all green).

- [ ] **Step 5: Commit**

```bash
git add server/pm/scoring.py server/tests/test_pm_scoring.py
git commit -m "feat(pm): pure RICE-lite scorer (single source of truth)"
```

---

### Task 2: Discovery stage — constants + engine handling (`server/sdlc/engine.py`)

**Files:**
- Modify: `server/db/models.py` (`INITIATIVE_STAGES` ~line 947; `INITIATIVE_ARTIFACT_KINDS` ~line 955)
- Modify: `server/sdlc/engine.py` (`STAGE_SEQUENCE` line 31; `STAGE_ARTIFACT` line 34)
- Test: `server/tests/test_sdlc_engine.py` (extend); `server/tests/test_pm_models.py` (extend)

**Interfaces:**
- Produces: `discovery` prepended to `INITIATIVE_STAGES`/`STAGE_SEQUENCE`; `discovery` added to `INITIATIVE_ARTIFACT_KINDS` and `STAGE_ARTIFACT` (`"discovery": "discovery"`). The pure `next_action` treats the discovery stage exactly like the other artifact stages (propose → await gate), but because it sits **before** `intake` an initiative that starts at `intake` simply never enters it — **missing discovery = skipped, never blocking** (spec §3, §10.4).
- Consumes: the existing `advance_stage`, `_enter_stage`, `next_action` — no new decision branch; discovery is just another entry in `STAGE_ARTIFACT`.

- [ ] **Step 1: Write the failing engine tests**

Append to `server/tests/test_sdlc_engine.py`:

```python
# --------------------------------------------------------------------------- #
# Discovery stage (phase 4, design §3/§10.4): optional, sits before intake.    #
# Missing discovery = skipped (an initiative that starts at intake never enters #
# it); when the initiative IS at discovery it behaves like any artifact stage.  #
# --------------------------------------------------------------------------- #

def test_discovery_is_first_and_optional_in_sequence():
    # discovery precedes intake; the rest of the ordering is unchanged.
    assert STAGE_SEQUENCE[0] == "discovery"
    assert STAGE_SEQUENCE.index("discovery") < STAGE_SEQUENCE.index("intake")
    assert STAGE_SEQUENCE.index("intake") < STAGE_SEQUENCE.index("design")


def test_discovery_stage_proposes_discovery_artifact():
    assert next_action(_init(stage="discovery"), [], "propose", {}) == Propose("discovery")


def test_discovery_pr_open_awaits_gate():
    arts = [_art("discovery", sha=None, pr=11)]
    assert next_action(_init(stage="discovery"), arts, "propose", {}) == AwaitApproval("discovery")


def test_discovery_merged_still_awaits_human_advance():
    arts = [_art("discovery", sha="feed", pr=11)]
    assert next_action(_init(stage="discovery"), arts, "propose", {}) == AwaitApproval("discovery")


def test_intake_start_skips_discovery_and_is_never_blocked_by_it():
    # An initiative that starts at intake (no discovery doc) proposes intent as
    # before — the absent discovery artifact never blocks it.
    assert next_action(_init(stage="intake"), [], "propose", {}) == Propose("intent")


def test_discovery_advances_to_intake_when_gate_disabled():
    arts = [_art("discovery", sha="feed", pr=11)]
    cfg = {"propose_only": False}
    assert next_action(_init(stage="discovery"), arts, "auto", cfg) == Propose("intent")
```

Also update the existing `test_stage_artifact_map_matches_sequence` (currently asserts `list(STAGE_ARTIFACT) == ["intake", "design", "plan"]`) to expect discovery first:

```python
def test_stage_artifact_map_matches_sequence():
    # Every artifact-producing stage is in the sequence, in order. Discovery
    # (phase 4) leads intake→design→plan.
    assert list(STAGE_ARTIFACT) == ["discovery", "intake", "design", "plan"]
    for stage in STAGE_ARTIFACT:
        assert stage in STAGE_SEQUENCE
    assert STAGE_SEQUENCE.index("discovery") < STAGE_SEQUENCE.index("intake") < \
        STAGE_SEQUENCE.index("design") < STAGE_SEQUENCE.index("plan") < \
        STAGE_SEQUENCE.index("build")
```

Append a constant assertion to `server/tests/test_pm_models.py` (keeps the two source-of-truth tuples in lockstep):

```python
def test_initiative_stages_gain_optional_discovery():
    from server.db.models import INITIATIVE_ARTIFACT_KINDS, INITIATIVE_STAGES
    from server.sdlc.engine import STAGE_SEQUENCE
    assert INITIATIVE_STAGES[0] == "discovery"
    assert INITIATIVE_STAGES == STAGE_SEQUENCE  # two constants, one order
    assert "discovery" in INITIATIVE_ARTIFACT_KINDS
```

- [ ] **Step 2: Run to verify failure**

Run: `... server/tests/test_sdlc_engine.py server/tests/test_pm_models.py -v`
Expected: the new discovery tests FAIL (`AssertionError`, `discovery` not first / not in `STAGE_ARTIFACT`); existing engine tests that pin the old ordering (`test_stage_artifact_map_matches_sequence`) FAIL until updated per Step 1.

- [ ] **Step 3: Implement**

In `server/db/models.py`, prepend `discovery` to `INITIATIVE_STAGES` and add it to `INITIATIVE_ARTIFACT_KINDS`:

```python
# Ordered lifecycle stages an Initiative moves through. Single source of truth:
# import this instead of re-inlining the sequence (constitution VI).
# ``discovery`` (phase 4, design §3) is an OPTIONAL leading stage: initiatives
# may start at discovery or intake, and a missing discovery artifact is skipped,
# never blocking (the engine only proposes it when stage == "discovery").
INITIATIVE_STAGES: tuple[str, ...] = (
    "discovery", "intake", "design", "plan", "build", "deploy", "maintain", "done")

# Coarse status an Initiative can rest in, orthogonal to its stage.
INITIATIVE_STATUSES: tuple[str, ...] = (
    "proposed", "active", "awaiting_human", "blocked", "done")

# The artifact kinds that make up an initiative's document chain, in order.
# ``discovery`` (docs/sdlc/<slug>/discovery.md) is the optional hypothesis doc.
INITIATIVE_ARTIFACT_KINDS: tuple[str, ...] = ("discovery", "intent", "spec", "plan")
```

> NOTE: `Initiative.stage` keeps `default="intake"` — the common path skips discovery, honoring "optional." An initiative that should start in discovery is created with `stage="discovery"` explicitly (Task 5's API accepts it).

In `server/sdlc/engine.py`, mirror both constants:

```python
# Full stage order (matches the Initiative.stage enum in the design). Only the
# first four produce artifacts; build hands off; deploy/maintain are carried by
# existing pipeline/monitor flows. ``discovery`` (phase 4) is OPTIONAL and leads
# the sequence — an initiative that starts at intake never enters it, so a
# missing discovery artifact is skipped, never blocking (design §3/§10.4).
STAGE_SEQUENCE = (
    "discovery", "intake", "design", "plan", "build", "deploy", "maintain", "done")

# Which committed artifact each front stage produces. Ordered discovery→…→plan.
STAGE_ARTIFACT = {
    "discovery": "discovery",
    "intake": "intent",
    "design": "spec",
    "plan": "plan",
}
```

No change to `next_action`/`advance_stage`/`_enter_stage` is required: `discovery` is now an artifact stage, so an initiative at `stage="discovery"` proposes the discovery artifact and awaits the gate; `advance_stage("discovery")` returns `"intake"` via the existing `_next_stage`; and the merged-gate-disabled path (`_enter_stage(next_stage)`) yields `Propose("intent")`. Verify by reading — do not add a branch.

- [ ] **Step 4: Run tests**

Run: `... server/tests/test_sdlc_engine.py server/tests/test_pm_models.py -v`
Expected: PASS (new discovery cases + updated map test + constant lockstep test all green; the parametrized front-stage tests for intake/design/plan still pass — discovery leaves them untouched).

- [ ] **Step 5: Commit**

```bash
git add server/db/models.py server/sdlc/engine.py server/tests/test_sdlc_engine.py server/tests/test_pm_models.py
git commit -m "feat(pm): optional discovery SDLC stage (constants + engine handling)"
```

---

### Task 3: Discovery prompt builder (`server/sdlc/prompts.py`)

**Files:**
- Modify: `server/sdlc/prompts.py` (add `build_discovery_prompt` beside `build_intent_prompt`)
- Test: `server/tests/test_sdlc_prompts.py` (extend if it exists; else create)

**Interfaces:**
- Produces: `build_discovery_prompt(trigger: str, *, slug: str, repo: str = "", default_branch: str = "master") -> str` — the seed prompt for a discovery session that writes `docs/sdlc/<slug>/discovery.md` (a hypothesis/experiment doc) and opens ONE non-draft PR, reusing the module's `_common_instructions` trailer and `artifact_path` (single source of truth for layout).
- Consumes: `artifact_path`, `_common_instructions` (existing).

- [ ] **Step 1: Write the failing test**

Check whether a prompts test module exists:

```bash
ls server/tests/test_sdlc_prompts.py 2>/dev/null || echo "create it"
```

Create/extend `server/tests/test_sdlc_prompts.py`:

```python
"""SDLC prompt-builder tests — pure string builders (no I/O)."""
from server.sdlc.prompts import artifact_path, build_discovery_prompt


def test_discovery_prompt_targets_discovery_doc_and_is_propose_only():
    out = build_discovery_prompt(
        "Users churn after onboarding — why?",
        slug="reduce-churn", repo="steiner385/ccswitch", default_branch="master")
    # Writes exactly the discovery artifact path (single source of truth).
    assert artifact_path("reduce-churn", "discovery") in out
    assert "docs/sdlc/reduce-churn/discovery.md" in out
    # Carries the discovery framing (hypothesis / experiment / signal).
    assert "hypothesis" in out.lower()
    assert "experiment" in out.lower()
    # Propose-only trailer is present (branch off origin/master, ONE non-draft PR,
    # no auto-merge) — the shared _common_instructions block.
    assert "origin/master" in out
    assert "NON-draft" in out
    assert "auto-merge" in out


def test_discovery_prompt_includes_the_trigger():
    out = build_discovery_prompt("The churn question", slug="x")
    assert "The churn question" in out
```

- [ ] **Step 2: Run to verify failure**

Run: `... server/tests/test_sdlc_prompts.py -v`
Expected: FAIL — `ImportError: cannot import name 'build_discovery_prompt'`.

- [ ] **Step 3: Implement**

In `server/sdlc/prompts.py`, add `build_discovery_prompt` immediately **before** `build_intent_prompt` (it produces the artifact that precedes intent in the chain):

```python
def build_discovery_prompt(
    trigger: str,
    *,
    slug: str,
    repo: str = "",
    default_branch: str = "master",
) -> str:
    """Seed prompt for the optional Discovery stage: a trigger → ``discovery.md``.

    Discovery is the pre-intent hypothesis/experiment doc (design §3): it frames
    the problem as a testable bet before committing to an intent. Skipped when an
    initiative starts at intake, so this prompt only runs for initiatives that
    explicitly begin at ``discovery``.
    """
    return (
        "You are running the optional **Discovery** stage of an AI-native SDLC "
        "loop — the pre-intent step. Turn the trigger below into a discovery "
        "document that frames the problem as a testable bet, so a human can "
        "decide whether it is worth an intent.\n\n"
        "TRIGGER (a problem, signal, or idea):\n"
        f"{trigger}\n\n"
        f"Produce `{_artifact_path(slug, 'discovery')}` with these sections:\n"
        "- **Hypothesis** — the bet, stated so it can be proven wrong.\n"
        "- **Evidence so far** — signals that motivate it (metrics, issues, quotes).\n"
        "- **Experiment** — the cheapest test that would confirm or kill the bet.\n"
        "- **Success signal** — what result would justify writing an intent.\n"
        "- **Risks / unknowns** — what could make this the wrong thing to build.\n\n"
        + _common_instructions(slug, "discovery", repo, default_branch)
    )
```

- [ ] **Step 4: Run tests**

Run: `... server/tests/test_sdlc_prompts.py -v`
Expected: PASS (2 tests). Also run `... server/tests/test_sdlc_engine.py -q` to confirm no regression.

- [ ] **Step 5: Commit**

```bash
git add server/sdlc/prompts.py server/tests/test_sdlc_prompts.py
git commit -m "feat(pm): discovery-stage seed prompt builder"
```

---

### Task 4: Board data helpers — grouped/scored backlog (`server/pm/board.py`)

**Files:**
- Create: `server/pm/board.py`
- Test: `server/tests/test_pm_board.py` (create)

**Interfaces:**
- Produces the pure shaping logic the board/roadmap APIs call, so the router stays thin and the ranking is unit-tested without a DB:
  - `WorkItemScored(id, repo, number, kind, title, state, feature_id, reach, impact, effort, confidence, triage_state, url, score)` frozen dataclass
  - `score_work_item(item) -> WorkItemScored` — attaches `rice_score(...)` to a plain `WorkItemView`-like input (a `WorkItemScoreInput` frozen dataclass with the overlay fields)
  - `group_backlog(items: list[WorkItemScored]) -> BacklogGrouped` where `BacklogGrouped(groups: tuple[FeatureBacklog, ...], awaiting_triage: tuple[WorkItemScored, ...])`; `FeatureBacklog(feature_id: str, items: tuple[WorkItemScored, ...])`. Each group's items are sorted by `score` **descending** (ties broken by `number` ascending for stability); items with `triage_state == "unclassified"` **or** `feature_id is None` go to the `awaiting_triage` bucket (spec §8).
- Consumes: `rice_score` from `server/pm/scoring.py`.

- [ ] **Step 1: Write the failing tests**

Create `server/tests/test_pm_board.py`:

```python
"""Pure board-shaping tests: score + group + awaiting-triage bucket (spec §8)."""
from server.pm.board import (
    WorkItemScoreInput,
    group_backlog,
    score_work_item,
)


def _wi(number, *, feature_id="f1", reach=3, impact=2, confidence=2, effort=1,
        triage_state="classified"):
    return WorkItemScoreInput(
        id=f"w{number}", repo="o/r", number=number, kind="issue",
        title=f"i{number}", state="open", url="", feature_id=feature_id,
        reach=reach, impact=impact, confidence=confidence, effort=effort,
        triage_state=triage_state)


def test_score_attaches_rice():
    scored = score_work_item(_wi(1, reach=3, impact=2, confidence=2, effort=1))
    assert scored.score == 12.0
    assert scored.number == 1 and scored.feature_id == "f1"


def test_group_orders_by_score_desc_then_number():
    items = [
        score_work_item(_wi(1, effort=6)),   # 12/6 = 2.0
        score_work_item(_wi(2, effort=1)),   # 12   = 12.0
        score_work_item(_wi(3, effort=2)),   # 12/2 = 6.0
    ]
    grouped = group_backlog(items)
    (group,) = grouped.groups
    assert group.feature_id == "f1"
    assert [i.number for i in group.items] == [2, 3, 1]  # 12 > 6 > 2
    assert grouped.awaiting_triage == ()


def test_unclassified_or_featureless_go_to_awaiting_triage():
    items = [
        score_work_item(_wi(1)),                                   # classified → group
        score_work_item(_wi(2, triage_state="unclassified")),     # → bucket
        score_work_item(_wi(3, feature_id=None)),                 # → bucket
    ]
    grouped = group_backlog(items)
    (group,) = grouped.groups
    assert [i.number for i in group.items] == [1]
    assert {i.number for i in grouped.awaiting_triage} == {2, 3}


def test_multiple_features_each_get_a_group():
    items = [score_work_item(_wi(1, feature_id="f1")),
             score_work_item(_wi(2, feature_id="f2"))]
    grouped = group_backlog(items)
    assert {g.feature_id for g in grouped.groups} == {"f1", "f2"}


def test_zero_effort_item_sorts_last_not_crashes():
    items = [score_work_item(_wi(1, effort=1)), score_work_item(_wi(2, effort=0))]
    grouped = group_backlog(items)
    (group,) = grouped.groups
    assert group.items[-1].number == 2 and group.items[-1].score == 0.0
```

- [ ] **Step 2: Run to verify failure**

Run: `... server/tests/test_pm_board.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'server.pm.board'`.

- [ ] **Step 3: Implement**

Create `server/pm/board.py`:

```python
"""Pure board/backlog shaping (spec §8): score work items, group them by feature
ordered by RICE-lite score, and split out the "awaiting triage" bucket.

No I/O — the router loads WorkItem rows, maps them to WorkItemScoreInput, and
renders the result. Ranking routes through ``rice_score`` (single source of
truth), never a re-inlined formula (constitution VI).
"""
from __future__ import annotations

from dataclasses import dataclass, replace

from server.pm.scoring import rice_score


@dataclass(frozen=True)
class WorkItemScoreInput:
    id: str
    repo: str
    number: int
    kind: str
    title: str
    state: str
    url: str
    feature_id: str | None
    reach: int | None
    impact: int | None
    confidence: int | None
    effort: int | None
    triage_state: str | None


@dataclass(frozen=True)
class WorkItemScored:
    id: str
    repo: str
    number: int
    kind: str
    title: str
    state: str
    url: str
    feature_id: str | None
    reach: int | None
    impact: int | None
    confidence: int | None
    effort: int | None
    triage_state: str | None
    score: float


@dataclass(frozen=True)
class FeatureBacklog:
    feature_id: str
    items: tuple[WorkItemScored, ...]


@dataclass(frozen=True)
class BacklogGrouped:
    groups: tuple[FeatureBacklog, ...]
    awaiting_triage: tuple[WorkItemScored, ...]


def score_work_item(item: WorkItemScoreInput) -> WorkItemScored:
    """Attach the RICE-lite score to a work item (0.0 when unscored)."""
    score = rice_score(item.reach, item.impact, item.confidence, item.effort)
    return WorkItemScored(**{**item.__dict__, "score": score})


def _rank_key(item: WorkItemScored) -> tuple[float, int]:
    # Score DESC, then number ASC for a stable order among ties.
    return (-item.score, item.number)


def group_backlog(items: list[WorkItemScored]) -> BacklogGrouped:
    """Group classified items by feature (each group score-ranked); route
    unclassified or feature-less items to the awaiting-triage bucket (spec §8)."""
    by_feature: dict[str, list[WorkItemScored]] = {}
    awaiting: list[WorkItemScored] = []
    for item in items:
        if item.feature_id is None or item.triage_state == "unclassified":
            awaiting.append(item)
            continue
        by_feature.setdefault(item.feature_id, []).append(item)
    groups = tuple(
        FeatureBacklog(feature_id=fid, items=tuple(sorted(rows, key=_rank_key)))
        for fid, rows in by_feature.items()
    )
    return BacklogGrouped(
        groups=groups,
        awaiting_triage=tuple(sorted(awaiting, key=_rank_key)),
    )
```

> NOTE: `replace` is imported for parity with sibling pure modules but unused here; if `ruff` flags F401, drop it. `score_work_item` uses `item.__dict__` spread — an alternative that avoids listing fields twice — which mypy accepts because both dataclasses share the field set plus `score`.

- [ ] **Step 4: Run tests**

Run: `... server/tests/test_pm_board.py -v`
Expected: PASS (5 tests). Run `ruff check server/pm/board.py` — fix any unused-import flag (remove `replace` if unused).

- [ ] **Step 5: Commit**

```bash
git add server/pm/board.py server/tests/test_pm_board.py
git commit -m "feat(pm): pure board shaping — score + group + awaiting-triage bucket"
```

---

### Task 5: Board/roadmap read APIs + score/link mutations (`server/api/pm.py`)

**Files:**
- Modify: `server/api/pm.py` (add routes; the phase-1 router already exists and is mounted)
- Test: `server/tests/test_pm_api.py` (extend — reuse the phase-1 fixtures verbatim)

**Interfaces:**
- Consumes: `Feature, FeatureLink, WorkItem, Initiative, InitiativeFeature, Project, ProjectRepo` (models); `rice_score` (Task 1); `group_backlog`, `score_work_item`, `WorkItemScoreInput` (Task 4); `FEATURE_LINK_KINDS`, `INITIATIVE_STAGES` (constants); `record_audit`, `current_identity`, `get_db` (same imports as phase 1).
- Produces the phase-4 routes (spec §7):
  - `GET /api/pm/projects/{slug}/features` — the feature map for a project: each feature with `state`, `repoPath`, `commitSha`, `prNumber`, `prUrl`, `orphaned`, `summary`, `staleDays` (days since the feature's newest linked work-item activity; `null` if none), and outgoing `links` (`kind`, `dstFeatureSlug`, `dstProjectSlug` — cross-project aware). 404 if project absent.
  - `GET /api/pm/projects/{slug}/backlog` — the project's work items, `group_backlog`-shaped: `{groups: [{featureId, featureSlug, featureTitle, items: [...]}], awaitingTriage: [...]}`, items ordered by `score`. 404 if project absent.
  - `GET /api/pm/roadmap` — ranked initiatives + scored backlog. Query params `project` (slug), `feature` (feature id), `stage` (∈ `INITIATIVE_STAGES`) filter the result. Returns `{initiatives: [...ordered by score desc...], backlog: [...ordered by score desc...]}`; each initiative carries `score` from `rice_score(reach,impact,confidence,effort)` and its `stage`/`status`/`projectId` (see `_initiative_score_dict`).
  - `PATCH /api/pm/initiatives/{slug}/scores` — body `{reach?, impact?, effort?, confidence?}` (any subset), writes them onto the `Initiative` row, returns the initiative dict incl. recomputed `score`. 404 if slug absent; 422 on a negative value.
  - `POST /api/pm/features/{id}/links` — body `{dstFeatureId, kind}` → 201 link dict; 422 on bad kind; 404 on unknown feature; cross-project links allowed (spec §3).
  - `DELETE /api/pm/features/{id}/links/{link_id}` → 204; 404 if the link isn't an outgoing link of `{id}`.

- [ ] **Step 1: Write the failing tests**

Append to `server/tests/test_pm_api.py` (the phase-1 `client` fixture is reused). These build a small graph directly through the API where possible, and seed features/work-items via the DB session fixture the phase-1 tests already expose (mirror however phase-1 seeded `WorkItem`/`Feature` rows — if phase-1 only has the registry client, add a `db` fixture that yields the same `AsyncSession` the app uses, following `test_sdlc_api.py`):

```python
import pytest

from server.db.models import (
    Feature, Initiative, WorkItem, InitiativeFeature)


async def _seed_project_with_backlog(client, db):
    resp = await client.post("/api/pm/projects", json={
        "name": "Board", "repos": [{"repo": "o/board", "isPrimary": True}]})
    assert resp.status_code == 201
    project_id = resp.json()["id"]
    feat = Feature(project_id=project_id, slug="sessions", title="Sessions",
                   state="live", repo_path="docs/product/features/sessions.md",
                   commit_sha="abc1234")
    db.add(feat)
    await db.flush()
    db.add_all([
        WorkItem(repo="o/board", number=1, kind="issue", title="A", state="open",
                 feature_id=feat.id, reach=3, impact=2, confidence=2, effort=1,
                 triage_state="classified"),
        WorkItem(repo="o/board", number=2, kind="issue", title="B", state="open",
                 feature_id=feat.id, reach=3, impact=2, confidence=2, effort=6,
                 triage_state="classified"),
        WorkItem(repo="o/board", number=3, kind="issue", title="C", state="open",
                 feature_id=None, reach=None, impact=None, confidence=None,
                 effort=None, triage_state="unclassified"),
    ])
    await db.flush()
    return project_id, feat.id


@pytest.mark.asyncio
async def test_features_and_backlog(client, db):
    _, feature_id = await _seed_project_with_backlog(client, db)

    resp = await client.get("/api/pm/projects/board/features")
    assert resp.status_code == 200
    (feat,) = resp.json()
    assert feat["slug"] == "sessions" and feat["state"] == "live"
    assert feat["commitSha"] == "abc1234"

    resp = await client.get("/api/pm/projects/board/backlog")
    assert resp.status_code == 200
    body = resp.json()
    (group,) = body["groups"]
    assert group["featureId"] == feature_id
    # score desc: #1 (12.0) before #2 (2.0)
    assert [i["number"] for i in group["items"]] == [1, 2]
    assert [i["number"] for i in body["awaitingTriage"]] == [3]


@pytest.mark.asyncio
async def test_features_backlog_404_when_absent(client):
    assert (await client.get("/api/pm/projects/nope/features")).status_code == 404
    assert (await client.get("/api/pm/projects/nope/backlog")).status_code == 404


@pytest.mark.asyncio
async def test_roadmap_ranks_and_filters(client, db):
    project_id, feature_id = await _seed_project_with_backlog(client, db)
    db.add_all([
        Initiative(slug="hi", repo="o/board", title="High", stage="design",
                   status="active", project_id=project_id,
                   reach=5, impact=4, confidence=3, effort=2),   # 30.0
        Initiative(slug="lo", repo="o/board", title="Low", stage="plan",
                   status="active", project_id=project_id,
                   reach=1, impact=1, confidence=1, effort=1),   # 1.0
    ])
    await db.flush()

    resp = await client.get("/api/pm/roadmap")
    assert resp.status_code == 200
    body = resp.json()
    assert [i["slug"] for i in body["initiatives"]] == ["hi", "lo"]  # score desc
    assert body["initiatives"][0]["score"] == 30.0
    assert [i["number"] for i in body["backlog"]] == [1, 2]  # unclassified excluded

    # filter by stage
    resp = await client.get("/api/pm/roadmap?stage=plan")
    assert [i["slug"] for i in resp.json()["initiatives"]] == ["lo"]

    # filter by project slug
    resp = await client.get("/api/pm/roadmap?project=board")
    assert {i["slug"] for i in resp.json()["initiatives"]} == {"hi", "lo"}

    # filter by feature (initiatives touching the feature, via InitiativeFeature)
    db.add(InitiativeFeature(initiative_id=(await _slug_id(db, "hi")),
                             feature_id=feature_id))
    await db.flush()
    resp = await client.get(f"/api/pm/roadmap?feature={feature_id}")
    assert [i["slug"] for i in resp.json()["initiatives"]] == ["hi"]


async def _slug_id(db, slug):
    from sqlalchemy import select
    return (await db.execute(
        select(Initiative.id).where(Initiative.slug == slug))).scalar_one()


@pytest.mark.asyncio
async def test_patch_initiative_scores(client, db):
    db.add(Initiative(slug="tune", repo="o/r", title="Tune", stage="design",
                      status="active"))
    await db.flush()
    resp = await client.patch("/api/pm/initiatives/tune/scores",
                              json={"reach": 4, "impact": 2, "confidence": 2, "effort": 2})
    assert resp.status_code == 200
    assert resp.json()["score"] == 8.0

    assert (await client.patch("/api/pm/initiatives/ghost/scores",
                               json={"reach": 1})).status_code == 404
    assert (await client.patch("/api/pm/initiatives/tune/scores",
                               json={"reach": -1})).status_code == 422


@pytest.mark.asyncio
async def test_feature_links_crud(client, db):
    p = await client.post("/api/pm/projects", json={
        "name": "Links", "repos": [{"repo": "o/links"}]})
    project_id = p.json()["id"]
    a = Feature(project_id=project_id, slug="a", title="A", state="live")
    b = Feature(project_id=project_id, slug="b", title="B", state="live")
    db.add_all([a, b])
    await db.flush()

    resp = await client.post(f"/api/pm/features/{a.id}/links",
                             json={"dstFeatureId": b.id, "kind": "powered_by"})
    assert resp.status_code == 201
    link_id = resp.json()["id"]

    assert (await client.post(f"/api/pm/features/{a.id}/links",
                              json={"dstFeatureId": b.id, "kind": "bogus"})).status_code == 422
    assert (await client.post(f"/api/pm/features/{a.id}/links",
                              json={"dstFeatureId": "ghost", "kind": "powered_by"})
            ).status_code == 404

    assert (await client.delete(
        f"/api/pm/features/{a.id}/links/{link_id}")).status_code == 204
```

- [ ] **Step 2: Run to verify failure**

Run: `... server/tests/test_pm_api.py -v`
Expected: FAIL — new routes 404 / import errors. If a `db` fixture is missing, add it first (copy from `test_sdlc_api.py` — the same `AsyncSession` the app uses so seeded rows are visible to the app).

- [ ] **Step 3: Implement**

Extend `server/api/pm.py`. Add imports at the top:

```python
from server.db.models import (
    FEATURE_LINK_KINDS,
    INITIATIVE_STAGES,
    Feature,
    FeatureLink,
    Initiative,
    InitiativeFeature,
    WorkItem,
    # (Project, ProjectRepo, ProjectEdge, PROJECT_EDGE_KINDS already imported in phase 1)
)
from server.pm.board import WorkItemScoreInput, group_backlog, score_work_item
from server.pm.scoring import rice_score
```

Add serializers + routes (after the phase-1 routes):

```python
def _initiative_score_dict(i: Initiative) -> dict:
    return {
        "id": i.id,
        "slug": i.slug,
        "title": i.title,
        "repo": i.repo,
        "stage": i.stage,
        "status": i.status,
        "projectId": i.project_id,
        "reach": i.reach,
        "impact": i.impact,
        "effort": i.effort,
        "confidence": i.confidence,
        "score": rice_score(i.reach, i.impact, i.confidence, i.effort),
    }


def _scored_item_dict(item) -> dict:
    return {
        "id": item.id,
        "repo": item.repo,
        "number": item.number,
        "kind": item.kind,
        "title": item.title,
        "state": item.state,
        "url": item.url,
        "featureId": item.feature_id,
        "reach": item.reach,
        "impact": item.impact,
        "effort": item.effort,
        "confidence": item.confidence,
        "triageState": item.triage_state,
        "score": item.score,
    }


def _to_score_input(w: WorkItem) -> WorkItemScoreInput:
    return WorkItemScoreInput(
        id=w.id, repo=w.repo or "", number=int(w.number or 0), kind=w.kind or "",
        title=w.title or "", state=w.state or "", url=w.url or "",
        feature_id=w.feature_id, reach=w.reach, impact=w.impact,
        confidence=w.confidence, effort=w.effort, triage_state=w.triage_state)


async def _project_repo_names(db, project_id: str) -> list[str]:
    rows = (await db.execute(
        select(ProjectRepo.repo).where(ProjectRepo.project_id == project_id)
    )).scalars().all()
    return [r for r in rows if r]


class ScorePatchBody(BaseModel):
    reach: int | None = None
    impact: int | None = None
    effort: int | None = None
    confidence: int | None = None


class FeatureLinkBody(BaseModel):
    dstFeatureId: str
    kind: str


@router.get("/projects/{slug}/features")
async def list_features(
    slug: str,
    _identity: str = Depends(current_identity),
    db=Depends(get_db),
) -> list[dict]:
    p = await _load_project(db, slug)
    features = (await db.execute(
        select(Feature).where(Feature.project_id == p.id)
        .order_by(Feature.state, Feature.slug))).scalars().all()
    if not features:
        return []
    fids = [f.id for f in features]
    # Outgoing feature links (cross-project aware): resolve dst feature + project slugs.
    links = (await db.execute(
        select(FeatureLink).where(FeatureLink.src_feature_id.in_(fids)))).scalars().all()
    dst_ids = {ln.dst_feature_id for ln in links}
    dst_meta: dict[str, tuple[str | None, str | None]] = {}
    if dst_ids:
        rows = (await db.execute(
            select(Feature.id, Feature.slug, Project.slug)
            .join(Project, Project.id == Feature.project_id)
            .where(Feature.id.in_(dst_ids)))).all()
        dst_meta = {fid: (fslug, pslug) for fid, fslug, pslug in rows}
    # Staleness: newest linked work-item activity per feature (spec §8 nudge).
    now = datetime.now(timezone.utc)
    activity = dict((await db.execute(
        select(WorkItem.feature_id, func.max(WorkItem.gh_updated_at))
        .where(WorkItem.feature_id.in_(fids)).group_by(WorkItem.feature_id))).all())
    out = []
    for f in features:
        newest = activity.get(f.id)
        stale_days = int((now - newest).days) if newest else None
        out.append({
            "id": f.id,
            "slug": f.slug,
            "title": f.title,
            "state": f.state,
            "summary": f.summary,
            "repoPath": f.repo_path,
            "commitSha": f.commit_sha,
            "prNumber": f.pr_number,
            "prUrl": f.pr_url,
            "orphaned": bool(f.orphaned),
            "staleDays": stale_days,
            "links": [
                {"id": ln.id, "kind": ln.kind,
                 "dstFeatureId": ln.dst_feature_id,
                 "dstFeatureSlug": dst_meta.get(ln.dst_feature_id, (None, None))[0],
                 "dstProjectSlug": dst_meta.get(ln.dst_feature_id, (None, None))[1]}
                for ln in links if ln.src_feature_id == f.id],
        })
    return out


@router.get("/projects/{slug}/backlog")
async def project_backlog(
    slug: str,
    _identity: str = Depends(current_identity),
    db=Depends(get_db),
) -> dict:
    p = await _load_project(db, slug)
    repo_names = await _project_repo_names(db, p.id)
    if not repo_names:
        return {"groups": [], "awaitingTriage": []}
    rows = (await db.execute(
        select(WorkItem).where(WorkItem.repo.in_(repo_names)))).scalars().all()
    scored = [score_work_item(_to_score_input(w)) for w in rows]
    grouped = group_backlog(scored)
    # Resolve feature slug/title for group headers.
    gfids = [g.feature_id for g in grouped.groups]
    fmeta: dict[str, tuple[str | None, str | None]] = {}
    if gfids:
        fmeta = {fid: (fslug, ftitle) for fid, fslug, ftitle in (await db.execute(
            select(Feature.id, Feature.slug, Feature.title)
            .where(Feature.id.in_(gfids)))).all()}
    return {
        "groups": [
            {"featureId": g.feature_id,
             "featureSlug": fmeta.get(g.feature_id, (None, None))[0],
             "featureTitle": fmeta.get(g.feature_id, (None, None))[1],
             "items": [_scored_item_dict(i) for i in g.items]}
            for g in grouped.groups],
        "awaitingTriage": [_scored_item_dict(i) for i in grouped.awaiting_triage],
    }


@router.get("/roadmap")
async def roadmap(
    project: str | None = None,
    feature: str | None = None,
    stage: str | None = None,
    _identity: str = Depends(current_identity),
    db=Depends(get_db),
) -> dict:
    if stage is not None and stage not in INITIATIVE_STAGES:
        raise HTTPException(
            status_code=422,
            detail={"error": "bad_stage", "message": f"stage must be one of {INITIATIVE_STAGES}"})
    project_id: str | None = None
    if project is not None:
        project_id = (await _load_project(db, project)).id
    q = select(Initiative)
    if project_id is not None:
        q = q.where(Initiative.project_id == project_id)
    if stage is not None:
        q = q.where(Initiative.stage == stage)
    if feature is not None:
        q = q.join(InitiativeFeature,
                   InitiativeFeature.initiative_id == Initiative.id).where(
            InitiativeFeature.feature_id == feature)
    initiatives = (await db.execute(q)).scalars().all()
    init_dicts = sorted(
        (_initiative_score_dict(i) for i in initiatives),
        key=lambda d: (-d["score"], d["slug"] or ""))

    # Scored backlog (classified only; ranked; same filters where they apply).
    wq = select(WorkItem)
    if project_id is not None:
        repo_names = await _project_repo_names(db, project_id)
        wq = wq.where(WorkItem.repo.in_(repo_names or [""]))
    if feature is not None:
        wq = wq.where(WorkItem.feature_id == feature)
    items = (await db.execute(wq)).scalars().all()
    scored = [score_work_item(_to_score_input(w)) for w in items
              if w.feature_id is not None and w.triage_state != "unclassified"]
    scored.sort(key=lambda s: (-s.score, s.number))
    return {
        "initiatives": init_dicts,
        "backlog": [_scored_item_dict(s) for s in scored],
    }


@router.patch("/initiatives/{slug}/scores")
async def patch_initiative_scores(
    slug: str,
    body: ScorePatchBody,
    identity: str = Depends(current_identity),
    db=Depends(get_db),
) -> dict:
    ini = (await db.execute(
        select(Initiative).where(Initiative.slug == slug))).scalar_one_or_none()
    if ini is None:
        raise HTTPException(status_code=404, detail={"error": "not_found"})
    updates = {k: v for k, v in body.model_dump(exclude_unset=True).items()
               if v is not None}
    for key, value in updates.items():
        if value < 0:
            raise HTTPException(
                status_code=422,
                detail={"error": "negative_score", "message": f"{key} must be >= 0"})
        setattr(ini, key, value)
    await db.flush()
    await record_audit(db=db, type="pm.initiative.scores", identity=identity,
                       detail={"slug": slug, **updates})
    return _initiative_score_dict(ini)


@router.post("/features/{feature_id}/links", status_code=status.HTTP_201_CREATED)
async def create_feature_link(
    feature_id: str,
    body: FeatureLinkBody,
    identity: str = Depends(current_identity),
    db=Depends(get_db),
) -> dict:
    if body.kind not in FEATURE_LINK_KINDS:
        raise HTTPException(
            status_code=422,
            detail={"error": "bad_kind", "message": f"kind must be one of {FEATURE_LINK_KINDS}"})
    src = (await db.execute(
        select(Feature).where(Feature.id == feature_id))).scalar_one_or_none()
    dst = (await db.execute(
        select(Feature).where(Feature.id == body.dstFeatureId))).scalar_one_or_none()
    if src is None or dst is None:
        raise HTTPException(status_code=404, detail={"error": "not_found"})
    link = FeatureLink(src_feature_id=src.id, dst_feature_id=dst.id, kind=body.kind)
    db.add(link)
    await db.flush()
    await record_audit(db=db, type="pm.feature_link.create", identity=identity,
                       detail={"src": feature_id, "dst": body.dstFeatureId,
                               "kind": body.kind})
    return {"id": link.id, "kind": link.kind,
            "srcFeatureId": link.src_feature_id, "dstFeatureId": link.dst_feature_id}


@router.delete("/features/{feature_id}/links/{link_id}",
               status_code=status.HTTP_204_NO_CONTENT)
async def delete_feature_link(
    feature_id: str,
    link_id: str,
    identity: str = Depends(current_identity),
    db=Depends(get_db),
) -> None:
    link = (await db.execute(
        select(FeatureLink).where(FeatureLink.id == link_id,
                                  FeatureLink.src_feature_id == feature_id)
    )).scalar_one_or_none()
    if link is None:
        raise HTTPException(status_code=404, detail={"error": "not_found"})
    await db.delete(link)
    await record_audit(db=db, type="pm.feature_link.delete", identity=identity,
                       detail={"src": feature_id, "linkId": link_id})
```

Verify `func` is imported in `server/api/pm.py` (phase 1 imports `from sqlalchemy import func, select` — confirm; add `func` if the phase-1 import omitted it).

- [ ] **Step 4: Run tests**

Run: `... server/tests/test_pm_api.py -v`
Expected: PASS (phase-1 tests + the 5 new phase-4 tests).

- [ ] **Step 5: Commit**

```bash
git add server/api/pm.py server/tests/test_pm_api.py
git commit -m "feat(pm): board/roadmap read APIs + initiative-scores + feature-link mutations"
```

---

### Task 6: Web API client + hooks (`web/src/api/pm.ts`, `web/src/api/hooks.ts`)

**Files:**
- Modify: `web/src/api/pm.ts` (extend the phase-1 module with board/roadmap types + fetchers)
- Modify: `web/src/api/hooks.ts` (add `QK.features`/`QK.backlog`/`QK.roadmap`; hooks `useFeatures`, `useBacklog`, `useRoadmap`, `useUpdateInitiativeScores`, `useFeatureLinks`)
- Test: hooks are covered indirectly by the component tests in Tasks 7–8; no standalone hook test required (mirrors phase 1, which tested hooks via the tab component).

**Interfaces:**
- Produces: types `Feature`, `FeatureLink`, `BacklogGroup`, `ProjectBacklog`, `ScoredWorkItem`, `RoadmapInitiative`, `Roadmap`, `RoadmapFilters`, `ScorePatch`; fetchers `fetchFeatures`, `fetchBacklog`, `fetchRoadmap`, `updateInitiativeScores`, `createFeatureLink`, `deleteFeatureLink`; hooks listed above.
- Consumes: the `api` client, the Task 5 routes.

- [ ] **Step 1: Extend the API module**

Append to `web/src/api/pm.ts`:

```typescript
// ── Phase 4: board + roadmap ────────────────────────────────────────────────

export interface FeatureLinkInfo {
  id: string
  kind: string | null
  dstFeatureId: string | null
  dstFeatureSlug: string | null
  dstProjectSlug: string | null
}

export interface Feature {
  id: string
  slug: string | null
  title: string | null
  state: string | null
  summary: string | null
  repoPath: string | null
  commitSha: string | null
  prNumber: number | null
  prUrl: string | null
  orphaned: boolean
  staleDays: number | null
  links: FeatureLinkInfo[]
}

export interface ScoredWorkItem {
  id: string
  repo: string
  number: number
  kind: string
  title: string
  state: string
  url: string
  featureId: string | null
  reach: number | null
  impact: number | null
  effort: number | null
  confidence: number | null
  triageState: string | null
  score: number
}

export interface BacklogGroup {
  featureId: string
  featureSlug: string | null
  featureTitle: string | null
  items: ScoredWorkItem[]
}

export interface ProjectBacklog {
  groups: BacklogGroup[]
  awaitingTriage: ScoredWorkItem[]
}

export interface RoadmapInitiative {
  id: string
  slug: string | null
  title: string | null
  repo: string | null
  stage: string | null
  status: string | null
  projectId: string | null
  reach: number | null
  impact: number | null
  effort: number | null
  confidence: number | null
  score: number
}

export interface Roadmap {
  initiatives: RoadmapInitiative[]
  backlog: ScoredWorkItem[]
}

export interface RoadmapFilters {
  project?: string
  feature?: string
  stage?: string
}

export interface ScorePatch {
  reach?: number
  impact?: number
  effort?: number
  confidence?: number
}

export function fetchFeatures(slug: string): Promise<Feature[]> {
  return api.get<Feature[]>(`/pm/projects/${encodeURIComponent(slug)}/features`)
}

export function fetchBacklog(slug: string): Promise<ProjectBacklog> {
  return api.get<ProjectBacklog>(`/pm/projects/${encodeURIComponent(slug)}/backlog`)
}

export function fetchRoadmap(filters: RoadmapFilters = {}): Promise<Roadmap> {
  const qs = new URLSearchParams()
  if (filters.project) qs.set('project', filters.project)
  if (filters.feature) qs.set('feature', filters.feature)
  if (filters.stage) qs.set('stage', filters.stage)
  const suffix = qs.toString() ? `?${qs.toString()}` : ''
  return api.get<Roadmap>(`/pm/roadmap${suffix}`)
}

export function updateInitiativeScores(
  slug: string, patch: ScorePatch,
): Promise<RoadmapInitiative> {
  return api.patch<RoadmapInitiative>(
    `/pm/initiatives/${encodeURIComponent(slug)}/scores`, patch)
}

export function createFeatureLink(
  featureId: string, dstFeatureId: string, kind: string,
): Promise<FeatureLinkInfo> {
  return api.post<FeatureLinkInfo>(
    `/pm/features/${encodeURIComponent(featureId)}/links`, { dstFeatureId, kind })
}

export function deleteFeatureLink(featureId: string, linkId: string): Promise<void> {
  return api.del<void>(
    `/pm/features/${encodeURIComponent(featureId)}/links/${encodeURIComponent(linkId)}`)
}
```

> NOTE: confirm the client method names (`api.patch`, `api.del`) against `web/src/api/client.ts` — use whatever the client exposes for PATCH/DELETE (phase 1 used `api.get`/`api.post`; match the real names, e.g. `api.delete`).

- [ ] **Step 2: Add hooks**

In `web/src/api/hooks.ts`, add to `QK`:

```typescript
  features: (slug: string) => ['pm', 'features', slug] as const,
  backlog: (slug: string) => ['pm', 'backlog', slug] as const,
  roadmap: (filters?: RoadmapFilters) => ['pm', 'roadmap', filters ?? {}] as const,
```

Import the new symbols from `./pm`, then append near the phase-1 project hooks:

```typescript
/** A project's feature map (board pane 1). */
export function useFeatures(slug: string) {
  return useQuery<Feature[]>({
    queryKey: QK.features(slug),
    queryFn: () => fetchFeatures(slug),
    enabled: !!slug,
    staleTime: 5_000,
  })
}

/** A project's backlog, grouped by feature + awaiting-triage bucket (board pane 3). */
export function useBacklog(slug: string) {
  return useQuery<ProjectBacklog>({
    queryKey: QK.backlog(slug),
    queryFn: () => fetchBacklog(slug),
    enabled: !!slug,
    staleTime: 5_000,
  })
}

/** The ranked roadmap (initiatives + scored backlog), filterable. */
export function useRoadmap(filters: RoadmapFilters = {}) {
  return useQuery<Roadmap>({
    queryKey: QK.roadmap(filters),
    queryFn: () => fetchRoadmap(filters),
    staleTime: 5_000,
    refetchInterval: 15_000,
  })
}

/** Inline initiative score edit (roadmap). Invalidates roadmap + projects. */
export function useUpdateInitiativeScores() {
  const qc = useQueryClient()
  return useMutation<
    RoadmapInitiative, ApiRequestError, { slug: string; patch: ScorePatch }
  >({
    mutationFn: ({ slug, patch }) => updateInitiativeScores(slug, patch),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ['pm', 'roadmap'] })
      void qc.invalidateQueries({ queryKey: QK.initiatives })
    },
    onError: (e) => toast.error(initiativeErrorMessage(e, 'Could not update scores')),
  })
}

/** Create/delete feature links (board pane 1, powered_by chips). */
export function useFeatureLinks(projectSlug: string) {
  const qc = useQueryClient()
  const invalidate = () =>
    void qc.invalidateQueries({ queryKey: QK.features(projectSlug) })
  const create = useMutation<
    FeatureLinkInfo, ApiRequestError,
    { featureId: string; dstFeatureId: string; kind: string }
  >({
    mutationFn: ({ featureId, dstFeatureId, kind }) =>
      createFeatureLink(featureId, dstFeatureId, kind),
    onSuccess: invalidate,
    onError: (e) => toast.error(initiativeErrorMessage(e, 'Could not add link')),
  })
  const remove = useMutation<
    void, ApiRequestError, { featureId: string; linkId: string }
  >({
    mutationFn: ({ featureId, linkId }) => deleteFeatureLink(featureId, linkId),
    onSuccess: invalidate,
    onError: (e) => toast.error(initiativeErrorMessage(e, 'Could not remove link')),
  })
  return { create, remove }
}
```

> `initiativeErrorMessage` and `toast` are already imported in `hooks.ts` (phase 1 / SDLC). Reuse them — do not add a parallel helper.

- [ ] **Step 3: Verify types**

Run in `web/`: `npx tsc --noEmit`
Expected: clean. (No new test here — hooks are exercised by Tasks 7–8's component tests.)

- [ ] **Step 4: Commit**

```bash
git add web/src/api/pm.ts web/src/api/hooks.ts
git commit -m "feat(pm): web board/roadmap API client + hooks"
```

---

### Task 7: Web — Product Board (`web/src/products/ProductBoard.tsx`)

**Files:**
- Create: `web/src/products/ProductBoard.tsx`
- Create: `web/src/products/products.css`
- Modify: `web/src/App.tsx` (add `<Route path="/projects/:slug" element={<ProductBoardRoute />} />` before the tab routes; add a `ProductBoardRoute` component reading `useParams`)
- Modify: `web/src/projects/ProjectsTab.tsx` (make the project card title/name link to `/projects/:slug` — the board is "reachable from the project card", spec §8)
- Test: `web/src/products/__tests__/ProductBoard.test.tsx` (create)

**Interfaces:**
- Consumes: `useFeatures`, `useBacklog` (Task 6), `useInitiatives` (existing) filtered to the project, `InitiativeTrack` (existing, reused not rebuilt), `fileUrl`/`prUrl` (existing `web/src/lib/githubUrl`), `useProjects` (phase 1, to resolve the project name + its repos so initiatives can be filtered by repo/project).
- Produces: `ProductBoard` (per-project three-pane view) + `ProductBoardRoute` (route wrapper).

- [ ] **Step 1: Write the failing test**

Create `web/src/products/__tests__/ProductBoard.test.tsx`. Mirror the `InitiativesTab.test.tsx` harness (stub the socket; mock `@/api/pm` + `@/api/sdlc` fetchers; wrap in QueryClientProvider + MemoryRouter):

```tsx
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import type { ReactNode } from 'react'

vi.mock('@/ws/client', () => ({ useSessionSocket: () => ({ on: vi.fn(), off: vi.fn() }) }))
vi.mock('@/api/pm', async (io) => ({
  ...(await io<typeof import('@/api/pm')>()),
  fetchFeatures: vi.fn(),
  fetchBacklog: vi.fn(),
  fetchProjects: vi.fn(),
}))
vi.mock('@/api/sdlc', async (io) => ({
  ...(await io<typeof import('@/api/sdlc')>()),
  fetchInitiatives: vi.fn(),
}))

import { ProductBoard } from '../ProductBoard'
import * as pm from '@/api/pm'
import * as sdlc from '@/api/sdlc'

const m = {
  fetchFeatures: pm.fetchFeatures as ReturnType<typeof vi.fn>,
  fetchBacklog: pm.fetchBacklog as ReturnType<typeof vi.fn>,
  fetchProjects: pm.fetchProjects as ReturnType<typeof vi.fn>,
  fetchInitiatives: sdlc.fetchInitiatives as ReturnType<typeof vi.fn>,
}

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return (
    <QueryClientProvider client={qc}>
      <MemoryRouter>{children}</MemoryRouter>
    </QueryClientProvider>
  )
}

beforeEach(() => {
  vi.clearAllMocks()
  m.fetchProjects.mockResolvedValue([{
    id: 'p1', slug: 'ccswitch', name: 'ccswitch', description: null, status: 'active',
    portfolioTags: [], onboardingState: 'live', createdAt: null, updatedAt: null,
    repos: [{ id: 'r1', repo: 'steiner385/ccswitch', isPrimary: true, backfillDone: true, lastSyncedAt: null }],
    edges: [], featureCount: 1, workItemCount: 2,
  }])
  m.fetchFeatures.mockResolvedValue([{
    id: 'f1', slug: 'sessions', title: 'Sessions', state: 'live', summary: 'run claude',
    repoPath: 'docs/product/features/sessions.md', commitSha: 'abc1234',
    prNumber: null, prUrl: null, orphaned: false, staleDays: 40,
    links: [{ id: 'l1', kind: 'powered_by', dstFeatureId: 'f9',
              dstFeatureSlug: 'queue', dstProjectSlug: 'infra' }],
  }])
  m.fetchBacklog.mockResolvedValue({
    groups: [{ featureId: 'f1', featureSlug: 'sessions', featureTitle: 'Sessions',
               items: [{ id: 'w1', repo: 'steiner385/ccswitch', number: 1, kind: 'issue',
                         title: 'Bug', state: 'open', url: '', featureId: 'f1',
                         reach: 3, impact: 2, effort: 1, confidence: 2,
                         triageState: 'classified', score: 12 }] }],
    awaitingTriage: [{ id: 'w2', repo: 'steiner385/ccswitch', number: 2, kind: 'issue',
                       title: 'Untriaged', state: 'open', url: '', featureId: null,
                       reach: null, impact: null, effort: null, confidence: null,
                       triageState: 'unclassified', score: 0 }],
  })
  m.fetchInitiatives.mockResolvedValue([{
    slug: 'do-sessions', repo: 'steiner385/ccswitch', title: 'Sessions v2',
    stage: 'design', status: 'active', origin: 'human', originRef: null,
    createdAt: null, updatedAt: null, artifacts: [],
  }])
})

describe('ProductBoard', () => {
  it('renders feature map with lifecycle badge, doc-at-SHA link, staleness, powered_by chip', async () => {
    render(<ProductBoard slug="ccswitch" />, { wrapper })
    await waitFor(() => expect(screen.getByText('Sessions')).toBeInTheDocument())
    // lifecycle badge
    expect(screen.getByText(/live/i)).toBeInTheDocument()
    // doc link pinned at the SHA (not HEAD)
    const link = screen.getByRole('link', { name: /sessions\.md|doc/i })
    expect(link).toHaveAttribute(
      'href', 'https://github.com/steiner385/ccswitch/blob/abc1234/docs/product/features/sessions.md')
    // staleness indicator
    expect(screen.getByText(/40d/i)).toBeInTheDocument()
    // cross-project powered_by chip navigating to the other board
    const chip = screen.getByRole('link', { name: /queue/i })
    expect(chip).toHaveAttribute('href', '/projects/infra')
  })

  it('reuses the initiative track filtered to this project', async () => {
    render(<ProductBoard slug="ccswitch" />, { wrapper })
    await waitFor(() => expect(screen.getByText('Sessions v2')).toBeInTheDocument())
    // The metro track renders its lifecycle nodes (reused InitiativeTrack).
    expect(document.querySelectorAll('.node').length).toBeGreaterThan(0)
  })

  it('groups backlog by feature ordered by score, with an awaiting-triage bucket', async () => {
    render(<ProductBoard slug="ccswitch" />, { wrapper })
    await waitFor(() => expect(screen.getByText(/awaiting triage/i)).toBeInTheDocument())
    expect(screen.getByText('Bug')).toBeInTheDocument()
    expect(screen.getByText('Untriaged')).toBeInTheDocument()
  })
})
```

- [ ] **Step 2: Run to verify failure**

In `web/`: `npx vitest run src/products`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement the board**

Create `web/src/products/ProductBoard.tsx` (read `web/src/initiatives/InitiativesTab.tsx` first for the `ArtifactLink`-at-SHA idiom and metro-track reuse):

```tsx
/**
 * Per-project Product Board (PM product layer, phase 4, design §8). Three panes:
 *   1. Feature map — capability list with lifecycle badges, doc links pinned at
 *      their commit SHA (the ArtifactLink idiom), staleness indicators, and
 *      cross-project powered_by chips that navigate to the linked project's board.
 *   2. Initiatives in flight — the existing InitiativeTrack metro idiom, REUSED
 *      (not rebuilt), filtered to this project's repos.
 *   3. Backlog grouped by feature, ordered by RICE-lite score, with an
 *      "awaiting triage" bucket linking to the triage inbox.
 */
import { Link } from 'react-router-dom'
import { useBacklog, useFeatures, useInitiatives, useProjects } from '@/api/hooks'
import { InitiativeTrack } from '@/initiatives/InitiativeTrack'
import { fileUrl, prUrl } from '@/lib/githubUrl'
import type { Feature, ScoredWorkItem } from '@/api/pm'
import '../initiatives/initiatives.css'
import './products.css'

function FeatureDocLink({ repo, feature }: { repo: string | null; feature: Feature }) {
  if (repo && feature.repoPath && feature.commitSha) {
    return (
      <a className="workspace-link" target="_blank" rel="noreferrer"
         href={fileUrl(repo, feature.commitSha, feature.repoPath)}
         title={`${feature.repoPath} @ ${feature.commitSha.slice(0, 7)}`}>
        {feature.repoPath?.split('/').pop() ?? 'doc'} ↗
      </a>
    )
  }
  if (feature.prUrl || (repo && feature.prNumber)) {
    return (
      <a className="workspace-link" target="_blank" rel="noreferrer"
         href={feature.prUrl ?? prUrl(repo!, feature.prNumber!)}>
        doc (PR #{feature.prNumber}) ↗
      </a>
    )
  }
  return <span className="init-artifact-pending">doc (pending)</span>
}

function FeatureCard({ repo, feature }: { repo: string | null; feature: Feature }) {
  return (
    <li className={`pm-feature${feature.orphaned ? ' pm-feature-orphaned' : ''}`}>
      <div className="pm-feature-head">
        <span className="pm-feature-title">{feature.title ?? feature.slug}</span>
        <span className={`pm-lifecycle pm-lifecycle-${feature.state ?? 'proposed'}`}>
          {feature.state ?? 'proposed'}
        </span>
        {feature.staleDays != null && feature.staleDays >= 30 && (
          <span className="pm-stale" title="no linked activity recently">
            {feature.staleDays}d stale
          </span>
        )}
      </div>
      {feature.summary && <p className="pm-feature-summary">{feature.summary}</p>}
      <div className="pm-feature-foot">
        <FeatureDocLink repo={repo} feature={feature} />
        {feature.links.filter((l) => l.kind === 'powered_by').map((l) => (
          <Link key={l.id} className="pm-chip"
                to={`/projects/${l.dstProjectSlug ?? ''}`}
                title={`powered_by ${l.dstProjectSlug}/${l.dstFeatureSlug}`}>
            ⚡ {l.dstFeatureSlug}
          </Link>
        ))}
      </div>
    </li>
  )
}

function BacklogItem({ item }: { item: ScoredWorkItem }) {
  return (
    <li className="pm-backlog-item">
      <span className="pm-backlog-score" title="RICE-lite score">
        {item.score.toFixed(1)}
      </span>
      <span className="pm-backlog-title">{item.title}</span>
      <span className="pm-backlog-num">#{item.number}</span>
    </li>
  )
}

export function ProductBoard({ slug }: { slug: string }) {
  const { data: projects } = useProjects()
  const project = (projects ?? []).find((p) => p.slug === slug)
  const primaryRepo = project?.repos.find((r) => r.isPrimary)?.repo
    ?? project?.repos[0]?.repo ?? null
  const projectRepos = new Set((project?.repos ?? []).map((r) => r.repo))
  const { data: features, isLoading: fLoading } = useFeatures(slug)
  const { data: backlog, isLoading: bLoading } = useBacklog(slug)
  const { data: initiatives } = useInitiatives()
  const projectInitiatives = (initiatives ?? []).filter(
    (i) => i.repo && projectRepos.has(i.repo))

  return (
    <div className="prdash-root pm-board flex-1 min-h-0 overflow-y-auto">
      <div className="pm-board-head">
        <Link to="/projects" className="pm-back">← Products</Link>
        <h2 className="pm-board-title">{project?.name ?? slug}</h2>
      </div>

      <section className="pm-pane" aria-label="Feature map">
        <h3 className="pm-pane-title">Feature map</h3>
        {fLoading && <p role="status">Loading features…</p>}
        {!fLoading && (features?.length ?? 0) === 0 && (
          <p className="init-empty">No features yet — propose a feature map from the wizard.</p>
        )}
        <ul className="pm-feature-list">
          {(features ?? []).map((f) => (
            <FeatureCard key={f.id} repo={primaryRepo} feature={f} />
          ))}
        </ul>
      </section>

      <section className="pm-pane" aria-label="Initiatives in flight">
        <h3 className="pm-pane-title">Initiatives in flight</h3>
        {projectInitiatives.length === 0
          ? <p className="init-empty">No active initiatives for this project.</p>
          : projectInitiatives.map((i) => (
              <div key={i.slug} className="pm-init-row">
                <span className="pm-init-title">{i.title ?? i.slug}</span>
                <InitiativeTrack stage={i.stage} status={i.status} />
              </div>
            ))}
      </section>

      <section className="pm-pane" aria-label="Backlog">
        <h3 className="pm-pane-title">Backlog</h3>
        {bLoading && <p role="status">Loading backlog…</p>}
        {(backlog?.groups ?? []).map((g) => (
          <div key={g.featureId} className="pm-backlog-group">
            <h4 className="pm-backlog-feature">{g.featureTitle ?? g.featureSlug}</h4>
            <ul className="pm-backlog-list">
              {g.items.map((it) => <BacklogItem key={it.id} item={it} />)}
            </ul>
          </div>
        ))}
        {(backlog?.awaitingTriage?.length ?? 0) > 0 && (
          <div className="pm-backlog-group pm-awaiting">
            <h4 className="pm-backlog-feature">
              <Link to="/triage">Awaiting triage ({backlog!.awaitingTriage.length}) →</Link>
            </h4>
            <ul className="pm-backlog-list">
              {backlog!.awaitingTriage.map((it) => <BacklogItem key={it.id} item={it} />)}
            </ul>
          </div>
        )}
      </section>
    </div>
  )
}
```

Create `web/src/products/products.css` reusing the `initiatives.css` tokens (`var(--surface)`, `var(--border)`, `var(--text)`, `var(--muted)`, `var(--accent)`, `var(--amber)`, `var(--done)`) — do NOT introduce new color literals. Give lifecycle badges per-state accent classes (`.pm-lifecycle-live` → `var(--done)`, `.pm-lifecycle-sunset` → `var(--muted)`, etc.), a subtle `.pm-stale` amber pill, and a `.pm-chip` pill for `powered_by`.

- [ ] **Step 4: Wire the route + card link**

In `web/src/App.tsx`, add a route wrapper and register it (place `/projects/:slug` **before** any catch-all; it does not collide with the `/projects` tab route because that has no `:slug`):

```tsx
function ProductBoardRoute() {
  const { slug } = useParams()
  return <ProductBoard slug={slug ?? ''} />
}
```
```tsx
          <Route path="/projects/:slug" element={<ProductBoardRoute />} />
```

Import `ProductBoard` at the top of `App.tsx`. In `web/src/projects/ProjectsTab.tsx`, wrap the card title (`<h3>{project.name ?? project.slug}</h3>`) in a `<Link to={`/projects/${project.slug}`}>` so the board is reachable from the card (import `Link` from `react-router-dom`). Keep the phase-1 ProjectsTab test green — if it asserts the title text, a `Link` preserves the text node; add `screen.getByRole('link', {name})` only if you extend that test.

- [ ] **Step 5: Run the component test**

In `web/`: `npx vitest run src/products`
Expected: PASS (3 tests).

- [ ] **Step 6: Commit**

```bash
git add web/src/products/ProductBoard.tsx web/src/products/products.css web/src/App.tsx web/src/projects/ProjectsTab.tsx
git commit -m "feat(pm): per-project Product Board (feature map + initiatives + backlog)"
```

---

### Task 8: Web — Roadmap tab (`web/src/products/Roadmap.tsx`) + tab wiring

**Files:**
- Create: `web/src/products/Roadmap.tsx`
- Modify: `web/src/products/products.css` (roadmap list styles)
- Modify: `web/src/components/Dashboard.tsx` (`TabId` union line 473 gains `'roadmap'` — order it AFTER phase-3's `'triage'` member, not at the phase-3 "after initiatives" point; add the tab panel beside the `initiatives`/`triage` ones at ~line 928)
- Modify: `web/src/components/tabDefs.ts` (add `{ id: 'roadmap', label: 'Roadmap' }` **after** the phase-3 Triage entry — NOT at the same "after initiatives" insertion point phase 3 uses, so the two additions don't land on the same line)
- Modify: `web/src/components/DesktopShell.tsx` (add the `tab === 'roadmap'` branch AFTER phase-3's `tab === 'triage'` branch; import `Roadmap`)
- Modify: `web/src/App.tsx` (add `<Route path="/roadmap" element={<DashboardRoute tab="roadmap" />} />` AFTER phase-3's `/triage` route)
- Test: `web/src/products/__tests__/Roadmap.test.tsx` (create); `web/src/components/__tests__/ArchivedTab.test.tsx` (update if it pins tab count/order)

**Interfaces:**
- Consumes: `useRoadmap`, `useUpdateInitiativeScores` (Task 6); `INITIATIVE_STAGES` (from `@/api/sdlc`) for the stage filter.
- Produces: `Roadmap` component (ranked list, NOT a Gantt; RICE inputs on hover; inline score edit).

- [ ] **Step 1: Write the failing test**

Create `web/src/products/__tests__/Roadmap.test.tsx`:

```tsx
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor, fireEvent } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'

vi.mock('@/api/pm', async (io) => ({
  ...(await io<typeof import('@/api/pm')>()),
  fetchRoadmap: vi.fn(),
  updateInitiativeScores: vi.fn(),
}))

import { Roadmap } from '../Roadmap'
import * as pm from '@/api/pm'

const m = {
  fetchRoadmap: pm.fetchRoadmap as ReturnType<typeof vi.fn>,
  updateInitiativeScores: pm.updateInitiativeScores as ReturnType<typeof vi.fn>,
}

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>
}

beforeEach(() => {
  vi.clearAllMocks()
  m.fetchRoadmap.mockResolvedValue({
    initiatives: [
      { id: 'i1', slug: 'hi', title: 'High', repo: 'o/r', stage: 'design',
        status: 'active', projectId: 'p1', reach: 5, impact: 4, effort: 2,
        confidence: 3, score: 30 },
      { id: 'i2', slug: 'lo', title: 'Low', repo: 'o/r', stage: 'plan',
        status: 'active', projectId: 'p1', reach: 1, impact: 1, effort: 1,
        confidence: 1, score: 1 },
    ],
    backlog: [
      { id: 'w1', repo: 'o/r', number: 1, kind: 'issue', title: 'Item', state: 'open',
        url: '', featureId: 'f1', reach: 3, impact: 2, effort: 1, confidence: 2,
        triageState: 'classified', score: 12 },
    ],
  })
})

describe('Roadmap', () => {
  it('renders a ranked list (not a gantt) with the highest score first', async () => {
    render(<Roadmap />, { wrapper })
    await waitFor(() => expect(screen.getByText('High')).toBeInTheDocument())
    const rows = screen.getAllByTestId('roadmap-initiative')
    expect(rows[0]).toHaveTextContent('High')   // 30 before 1
    expect(rows[1]).toHaveTextContent('Low')
    expect(screen.getByText('30.0')).toBeInTheDocument()
  })

  it('shows RICE inputs on hover (title attr keeps the number arguable)', async () => {
    render(<Roadmap />, { wrapper })
    await waitFor(() => expect(screen.getByText('High')).toBeInTheDocument())
    const score = screen.getByText('30.0')
    expect(score).toHaveAttribute('title', expect.stringMatching(/reach.*5/i))
  })

  it('edits a score inline and PATCHes it', async () => {
    m.updateInitiativeScores.mockResolvedValue({
      id: 'i1', slug: 'hi', title: 'High', repo: 'o/r', stage: 'design',
      status: 'active', projectId: 'p1', reach: 5, impact: 4, effort: 1,
      confidence: 3, score: 60 })
    render(<Roadmap />, { wrapper })
    await waitFor(() => expect(screen.getByText('High')).toBeInTheDocument())
    fireEvent.click(screen.getAllByRole('button', { name: /edit scores/i })[0]!)
    const effort = screen.getByLabelText(/effort/i)
    fireEvent.change(effort, { target: { value: '1' } })
    fireEvent.click(screen.getByRole('button', { name: /save/i }))
    await waitFor(() =>
      expect(m.updateInitiativeScores).toHaveBeenCalledWith('hi', expect.objectContaining({ effort: 1 })))
  })
})
```

- [ ] **Step 2: Run to verify failure**

In `web/`: `npx vitest run src/products/__tests__/Roadmap`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement**

Create `web/src/products/Roadmap.tsx`:

```tsx
/**
 * Priority Roadmap (PM product layer, phase 4, design §8): a RANKED LIST, not a
 * Gantt. Initiatives + scored backlog ordered by RICE-lite score, RICE inputs
 * shown on hover (the number stays arguable), inline score editing on
 * initiatives, filterable by project / feature / stage. No timeline lanes.
 */
import { useState } from 'react'
import { useRoadmap, useUpdateInitiativeScores } from '@/api/hooks'
import { INITIATIVE_STAGES } from '@/api/sdlc'
import type { RoadmapInitiative, ScorePatch } from '@/api/pm'
import '../initiatives/initiatives.css'
import './products.css'

function riceTitle(r: RoadmapInitiative): string {
  return `reach ${r.reach ?? '—'} × impact ${r.impact ?? '—'} × confidence ` +
    `${r.confidence ?? '—'} / effort ${r.effort ?? '—'}`
}

function ScoreEditor({ init, onDone }: { init: RoadmapInitiative; onDone: () => void }) {
  const update = useUpdateInitiativeScores()
  const [patch, setPatch] = useState<ScorePatch>({
    reach: init.reach ?? undefined, impact: init.impact ?? undefined,
    effort: init.effort ?? undefined, confidence: init.confidence ?? undefined,
  })
  const field = (key: keyof ScorePatch, label: string) => (
    <label className="pm-score-field">
      {label}
      <input type="number" min={0} aria-label={label} value={patch[key] ?? ''}
             onChange={(e) => setPatch((p) => ({
               ...p, [key]: e.target.value === '' ? undefined : Number(e.target.value) }))} />
    </label>
  )
  return (
    <form className="pm-score-editor" onSubmit={(e) => {
      e.preventDefault()
      update.mutate({ slug: init.slug ?? '', patch }, { onSuccess: onDone })
    }}>
      {field('reach', 'Reach')}
      {field('impact', 'Impact')}
      {field('confidence', 'Confidence')}
      {field('effort', 'Effort')}
      <button type="submit" className="btn-ghost" disabled={update.isPending}>Save</button>
      <button type="button" className="btn-ghost" onClick={onDone}>Cancel</button>
    </form>
  )
}

function InitiativeRow({ init }: { init: RoadmapInitiative }) {
  const [editing, setEditing] = useState(false)
  return (
    <li className="pm-roadmap-row" data-testid="roadmap-initiative">
      <span className="pm-backlog-score" title={riceTitle(init)}>
        {init.score.toFixed(1)}
      </span>
      <span className="pm-roadmap-title">{init.title ?? init.slug}</span>
      <span className="pm-roadmap-stage">{init.stage}</span>
      <button type="button" className="btn-ghost" onClick={() => setEditing((v) => !v)}
              aria-label={`edit scores for ${init.title ?? init.slug}`}>
        Edit scores
      </button>
      {editing && <ScoreEditor init={init} onDone={() => setEditing(false)} />}
    </li>
  )
}

export function Roadmap() {
  const [stage, setStage] = useState<string>('')
  const { data, isLoading, isError } = useRoadmap(stage ? { stage } : {})
  return (
    <div className="prdash-root pm-roadmap flex-1 min-h-0 overflow-y-auto">
      <div className="pm-board-head">
        <h2 className="pm-board-title">Roadmap</h2>
        <label className="pm-roadmap-filter">
          Stage
          <select value={stage} onChange={(e) => setStage(e.target.value)}>
            <option value="">all</option>
            {INITIATIVE_STAGES.map((s) => <option key={s} value={s}>{s}</option>)}
          </select>
        </label>
      </div>
      {isLoading && <p role="status">Loading roadmap…</p>}
      {isError && <p className="stale" role="status">Could not load the roadmap.</p>}
      {!isLoading && !isError && (
        <>
          <h3 className="pm-pane-title">Initiatives</h3>
          {(data?.initiatives.length ?? 0) === 0
            ? <p className="init-empty">No initiatives match.</p>
            : <ul className="pm-roadmap-list">
                {data!.initiatives.map((i) => <InitiativeRow key={i.id} init={i} />)}
              </ul>}
          <h3 className="pm-pane-title">Scored backlog</h3>
          <ul className="pm-roadmap-list">
            {(data?.backlog ?? []).map((b) => (
              <li key={b.id} className="pm-roadmap-row">
                <span className="pm-backlog-score"
                      title={`reach ${b.reach ?? '—'} × impact ${b.impact ?? '—'} × confidence ${b.confidence ?? '—'} / effort ${b.effort ?? '—'}`}>
                  {b.score.toFixed(1)}
                </span>
                <span className="pm-roadmap-title">{b.title}</span>
                <span className="pm-roadmap-num">#{b.number}</span>
              </li>
            ))}
          </ul>
        </>
      )}
    </div>
  )
}
```

Add roadmap list styles to `web/src/products/products.css` (row grid: score · title · stage/num · action), reusing existing tokens.

- [ ] **Step 4: Wire the tab**

Apply the wiring edits (mirror the phase-1 `initiatives` wiring exactly, but always append AFTER phase-3's Triage additions — see the landing-order note in the header):
- `web/src/components/Dashboard.tsx`: extend the `TabId` union (line 473) with `| 'roadmap'`, placing it AFTER phase-3's `| 'triage'` member; copy the `activeTab === 'initiatives'` panel block (~line 928) to an `activeTab === 'roadmap'` one with ids `tabpanel-roadmap`/`tab-roadmap` rendering `<Roadmap />` (import it).
- `web/src/components/tabDefs.ts`: add `{ id: 'roadmap', label: 'Roadmap' }` **after** the phase-3 `{ id: 'triage', label: 'Triage' }` entry — not at the same "after initiatives" insertion point phase 3 uses.
- `web/src/components/DesktopShell.tsx`: add `} else if (tab === 'roadmap') { mainPane = <div className="flex flex-col h-full"><Roadmap /></div>` AFTER phase-3's `tab === 'triage'` branch; import `Roadmap`.
- `web/src/App.tsx`: add `<Route path="/roadmap" element={<DashboardRoute tab="roadmap" />} />` AFTER phase-3's `/triage` route.

- [ ] **Step 5: Run the web tests**

In `web/`: `npx vitest run src/products`
Expected: PASS. Then `npx vitest run` — if `web/src/components/__tests__/ArchivedTab.test.tsx` walks the tab list and pins its length/order, update its expectation to include `Roadmap` (the tab lands after Initiatives).

- [ ] **Step 6: Commit**

```bash
git add web/src/products/Roadmap.tsx web/src/products/products.css web/src/components/Dashboard.tsx web/src/components/tabDefs.ts web/src/components/DesktopShell.tsx web/src/App.tsx web/src/components/__tests__/ArchivedTab.test.tsx
git commit -m "feat(pm): Roadmap tab — ranked RICE-lite list with inline score edit"
```

---

### Task 9: Full gate, PR

- [ ] **Step 1: Run the complete four-step Python gate** (serialized, from the repo root, with the env prefix):

```bash
ruff check src/ server/
mypy server/ --ignore-missing-imports
... tests/ -q
... server/tests/ -q
```

Expected: all four green. The engine/constant change (Task 2) touches shared SDLC code — confirm `test_sdlc_engine.py`, `test_sdlc_intake_loop.py`, and any test that iterates `INITIATIVE_STAGES` (e.g. the web `InitiativeTrack` counterpart, and `test_pm_models.py`) all pass. No new model/table means the Postgres suite needs no new list entry; still run it if `CCSWITCH_TEST_PG_URL` is set: `... server/tests/test_postgres_compat.py -q`.

- [ ] **Step 2: Run the web gate** (in `web/`): `npx vitest run && npx tsc --noEmit && npx eslint .`

Expected: all clean. The web `InitiativeTrack` `STAGE_LABEL` map (`web/src/initiatives/InitiativeTrack.tsx`) is keyed by `InitiativeStage` — since the discovery stage is a **server** SDLC concern and the web `INITIATIVE_STAGES` (in `web/src/api/sdlc.ts`) is NOT changed by this phase (initiatives surfaced on the board/roadmap start at intake), the web track is unaffected. If a future phase surfaces discovery-stage initiatives in the web track, add `discovery: 'Discovery'` to `STAGE_LABEL` and `'discovery'` to the web `INITIATIVE_STAGES` then — out of scope here.

- [ ] **Step 3: Check for in-flight PRs touching the same files**

```bash
gh api graphql -f query='query { repository(owner: "steiner385", name: "ccswitch") { pullRequests(states: OPEN, first: 20) { nodes { number title files(first: 50) { nodes { path } } } } } }'
```

If any open PR touches `server/db/models.py`, `server/sdlc/engine.py`, `server/sdlc/prompts.py`, `server/api/pm.py`, `web/src/api/pm.ts`, `web/src/api/hooks.ts`, or the tab-wiring files (`Dashboard.tsx`/`tabDefs.ts`/`DesktopShell.tsx`/`App.tsx`), note it in the PR body and rebase after it merges. (Phases 2 and 3 also touch `server/api/pm.py` and `web/src/api/hooks.ts` — coordinate ordering.) **Phase 4 lands last (after phases 2 and 3): rebase onto `master` before pushing and append the Roadmap tab after the phase-3 Triage tab (union member / `tabDefs.ts` entry / `DesktopShell` branch / `App.tsx` route), not at phase 3's "after initiatives" insertion point, to avoid a same-line merge conflict.**

- [ ] **Step 4: Push and open the PR**

```bash
git push -u origin feat/pm-product-layer-phase4
gh pr create --title "feat(pm): product layer phase 4 — product board, roadmap, discovery stage" --body "$(cat <<'EOF'
Phase 4 of the PM product layer (design: docs/superpowers/specs/2026-08-31-pm-product-layer-design.md §8/§10.4).

- RICE-lite scorer (server/pm/scoring.py): single source of truth reach×impact×confidence/effort with a divide-by-zero guard, table-tested.
- Optional discovery SDLC stage: discovery prepended to INITIATIVE_STAGES / STAGE_SEQUENCE + discovery artifact kind (docs/sdlc/<slug>/discovery.md); pure engine handles it (missing discovery = skipped, never blocking) + a discovery seed prompt.
- Board shaping (server/pm/board.py): score + group-by-feature (score desc) + awaiting-triage bucket, pure.
- API: GET /projects/{slug}/features, GET /projects/{slug}/backlog, GET /roadmap (filterable by project/feature/stage), PATCH /initiatives/{slug}/scores, POST/DELETE /features/{id}/links.
- Web: per-project Product Board (feature map with lifecycle badges + doc-at-SHA links + staleness + cross-project powered_by chips; initiatives-in-flight reusing InitiativeTrack; backlog grouped by feature) at /projects/:slug; Roadmap tab (ranked RICE-lite list, inputs on hover, inline score edit) at /roadmap.

No new DB tables (all schema shipped in phase 1). Portfolio dashboard remains out of scope (trails).

Depends on phases 1–3 (registry API + web hooks + triage route). Coordinate merge order for shared files (server/api/pm.py, web/src/api/hooks.ts).

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
gh pr ready <N> 2>/dev/null || true
gh pr merge <N> --squash --auto
```

- [ ] **Step 5: Confirm** the PR is non-draft with auto-merge armed:

```bash
gh api graphql -f query='query { repository(owner: "steiner385", name: "ccswitch") { pullRequest(number: <N>) { isDraft autoMergeRequest { enabledAt } } } }'
```

---

## Out of scope (v1, per spec §11 + briefing)

- **Portfolio dashboard** — trails phase 4 (edges + links are in the DB from phase 1; the rollup is pure presentation, deferred). Explicitly NOT built here.
- Timeline / Gantt scheduling on the roadmap (ranked list only).
- `auto` mode for any PM automation (human gate is constitutional in v1); this phase adds no automation.
- Surfacing discovery-stage initiatives in the **web** initiative track (`web/src/api/sdlc.ts` / `InitiativeTrack.tsx` unchanged) — the discovery stage is a server-side SDLC concern this phase; wiring the web track for discovery follows if/when discovery-stage initiatives appear on the board.
- New DB tables/columns — none; phase-1 schema covers everything.

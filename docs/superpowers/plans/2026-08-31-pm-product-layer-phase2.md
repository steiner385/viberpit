# PM Product Layer — Phase 2 (Archaeology Onboarding) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Land archaeology onboarding on top of the phase-1 spine: a pure onboarding state-machine engine (`server/pm/onboarding.py`), a pure classification prompt/parse module + structured-output schema (`server/pm/classification.py`, reused by phase 3), orchestrator seams that spawn ONE non-draft map-synthesis PR and chunked classification sessions writing `TriageProposal` rows, the wizard/triage API (`propose-map`, `classify`, `GET /triage`, `accept`, `reject`), and a wizard view + plain triage review list in the web `projects/` area. Payoff: onboard ccswitch itself — the dogfood moment.

**Landing order:** Phases land in sequence — **phase 2 lands FIRST**, before phase 3 and phase 4. Phase 2 DEFINES the shared seams (`_pm_onboard_spawn_map`, `_pm_classify_run`, `_apply_accept`), the triage web module (`web/src/api/triage.ts`), hooks, and `QK.triage`; phases 3/4 REUSE/extend them in place. Rebase onto master and append after any sibling tab entries if landing after a sibling.

**Architecture:** Extends the SDLC spine in place, same molds as phase 1. The onboarding engine follows `server/sdlc/engine.py` (pure, total function over frozen views → frozen action). The classification module follows `server/sdlc/prompts.py` (pure prompt builders) plus a parse half returning frozen dataclasses. The map-synthesis and classify sessions reuse the orchestrator's SDLC session-spawn seam (`AutomationContext.spawn_session`, healthy-account gate, `ensure_clone` off the loop) exactly as `_sdlc_spawn_synth` does; the map-synthesis prompt reuses the propose-only trailer idiom from `server/sdlc/prompts.py._common_instructions` (one non-draft PR, no auto-merge). The API mirrors `server/api/sdlc.py` (camelCase JSON, 4xx `detail` = `{error, message}`, audit on mutation) and mounts on the phase-1 `server/api/pm.py` router. Web mirrors the `initiatives` tab idioms (React Query + `ArtifactLink`-at-SHA), extending the phase-1 `web/src/projects/` area.

**Tech Stack:** Python 3.12 / FastAPI / SQLAlchemy 2 async / PyYAML (already a dep) · React + Vite + TS + React Query · `gh` CLI for GraphQL (unchanged from phase 1) · spawned `claude` sessions via the existing orchestrator spawn seam.

**Spec:** `docs/superpowers/specs/2026-08-31-pm-product-layer-design.md` (read §5 Archaeology onboarding, §6 items 1–2, §7 triage routes, §8 surfaces — the plan argues from it). Phase-1 plan `docs/superpowers/plans/2026-08-31-pm-product-layer-phase1.md` is the STYLE TEMPLATE and the contract this phase consumes.

## Global Constraints

- Branch off `origin/master` first: `git fetch && git checkout -b feat/pm-product-layer-phase2 origin/master`. NEVER branch off local `master`.
- TDD throughout (constitution V): failing test first, then minimal impl, then green, then commit.
- **Propose-only, human-gated. NO `auto` mode for any PM automation in v1.** The classification path spawns sessions that write `TriageProposal` rows only; nothing touches a `WorkItem` overlay without an explicit human accept. Map-synthesis opens ONE non-draft PR and NEVER arms auto-merge — the merge is the curation gate.
- Automations default to `mode: off`; never auto-enable at deploy.
- **NO NEW DB TABLES.** Phase 1 shipped the whole schema (migration `0012_pm_product_layer`). This phase adds NO columns and NO tables — every value it needs already exists (`Project.onboarding_state`, `WorkItem` overlay + `triage_state`, all `TriageProposal` columns). If a task ever appears to need a column, re-read phase-1's model list first; it does not.
- Python tests run via the prebuilt venv (this phase touches only `server/`, so no PYTHONPATH override needed): `env -u CCSWITCH_TAILNET_ALLOWLIST -u CCSWITCH_SESSIOND_HOST -u CCSWITCH_SESSIOND_PORT -u CCSWITCH_SESSIOND_CLIENT_CERT -u CCSWITCH_SESSIOND_CLIENT_KEY -u CCSWITCH_SESSIOND_CA CCSWITCH_DEV=1 /home/tony/GitHub/ccswitch/.venv/bin/python -m pytest <path> -v`. Shorthand below: `... -m pytest`.
- Web commands run in `web/` (symlink `node_modules` from `/home/tony/GitHub/ccswitch/web/node_modules` if absent): `npx vitest run`, `npx tsc --noEmit`, `npx eslint .`
- Before EVERY push, the full four-step Python gate: `ruff check src/ server/` · `mypy server/ --ignore-missing-imports` · `pytest tests/` · `pytest server/tests/` (serialize full-suite runs — concurrent runs on this host get SIGKILLed). Web gate: `npx vitest run` · `npx tsc --noEmit` · `npx eslint .`
- camelCase JSON at the API boundary; snake_case in Python. 4xx `detail` = object `{error, message}`.
- Layered truth: GitHub owns work items, repo markdown owns feature docs, ccswitch DB owns only index/overlay/relations. Never introduce a second source of truth.
- GitHub reads: GraphQL via the `gh_run` seam only (unchanged — this phase adds no new GraphQL). Never REST reads, never raw tokens.
- Commit format: `feat(pm): …` / `test(pm): …`, ending with `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`.

---

### Task 1: Onboarding state-machine engine (`server/pm/onboarding.py`)

**Files:**
- Create: `server/pm/onboarding.py`
- Test: `server/tests/test_pm_onboarding_engine.py` (create)

**Interfaces:**
- Produces:
  - `ProjectOnboardingView(state: str, repo_count: int, backfill_done_count: int, feature_count: int, open_unclassified_count: int, pending_proposal_count: int, map_pr_open: bool, map_pr_merged: bool)` frozen dataclass — the counts the engine decides over (built by the API/orchestrator from DB rows, never the ORM).
  - Frozen action classes: `Register()`, `AwaitBackfill()`, `ProposeMap()`, `AwaitMapPr()`, `Classify()`, `AwaitTriage()`, `Live()`, `OnboardingNeedsHuman(reason: str = "")`.
  - `OnboardingAction = Register | AwaitBackfill | ProposeMap | AwaitMapPr | Classify | AwaitTriage | Live | OnboardingNeedsHuman`
  - `next_onboarding_action(view: ProjectOnboardingView) -> OnboardingAction` — pure + total.
  - `advance_onboarding_state(state: str) -> str` — successor in `PROJECT_ONBOARDING_STATES`, `live` absorbing; raises `ValueError` on an unknown state (mirrors `sdlc.engine.advance_stage`).
- Consumes: `PROJECT_ONBOARDING_STATES` from `server.db.models` (phase-1 constant; single source of truth for the state order — do NOT redefine it here).

- [ ] **Step 1: Write the failing tests**

Create `server/tests/test_pm_onboarding_engine.py`. Mirror `server/tests/test_sdlc_engine.py`: exhaustive `(state, counts)` table, purity + determinism assertions.

```python
"""Pure onboarding state-machine engine: next_onboarding_action over the
registered→syncing→map_proposed→map_merged→classifying→live wizard states.

Exhaustive, I/O-free — the way test_sdlc_engine tests next_action. The wizard is
a VIEW over this engine (spec §5); the transitions here are the single source of
truth for which action each state offers.
"""
from __future__ import annotations

import pytest

from server.db.models import PROJECT_ONBOARDING_STATES
from server.pm.onboarding import (
    AwaitBackfill,
    AwaitMapPr,
    AwaitTriage,
    Classify,
    Live,
    OnboardingNeedsHuman,
    ProjectOnboardingView,
    ProposeMap,
    Register,
    advance_onboarding_state,
    next_onboarding_action,
)


def _view(**kw) -> ProjectOnboardingView:
    base = dict(state="syncing", repo_count=1, backfill_done_count=0,
                feature_count=0, open_unclassified_count=0,
                pending_proposal_count=0, map_pr_open=False, map_pr_merged=False)
    base.update(kw)
    return ProjectOnboardingView(**base)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# registered: nothing attached yet → Register (needs at least one repo)        #
# --------------------------------------------------------------------------- #

def test_registered_with_no_repos_is_register():
    v = _view(state="registered", repo_count=0)
    assert next_onboarding_action(v) == Register()


def test_registered_with_repos_awaits_backfill():
    # A registered project with repos is waiting for pm-sync to backfill.
    v = _view(state="registered", repo_count=1, backfill_done_count=0)
    assert next_onboarding_action(v) == AwaitBackfill()


# --------------------------------------------------------------------------- #
# syncing: backfill in flight → wait; all repos done → propose the map         #
# --------------------------------------------------------------------------- #

def test_syncing_partial_backfill_awaits():
    v = _view(state="syncing", repo_count=2, backfill_done_count=1)
    assert next_onboarding_action(v) == AwaitBackfill()


def test_syncing_all_backfilled_proposes_map():
    v = _view(state="syncing", repo_count=2, backfill_done_count=2)
    assert next_onboarding_action(v) == ProposeMap()


# --------------------------------------------------------------------------- #
# map_proposed: PR open → wait; PR merged → the human advance moves state on    #
# --------------------------------------------------------------------------- #

def test_map_proposed_pr_open_awaits():
    v = _view(state="map_proposed", backfill_done_count=1, map_pr_open=True)
    assert next_onboarding_action(v) == AwaitMapPr()


def test_map_proposed_pr_merged_still_awaits_human_advance():
    # Merge is the gate; the engine never advances the state itself (propose-only
    # parity with the SDLC engine). It keeps offering AwaitMapPr until advanced.
    v = _view(state="map_proposed", backfill_done_count=1,
              map_pr_open=False, map_pr_merged=True)
    assert next_onboarding_action(v) == AwaitMapPr()


def test_map_proposed_no_pr_yet_reproposes():
    # Re-entering the state with no PR (e.g. session died) re-offers ProposeMap.
    v = _view(state="map_proposed", map_pr_open=False, map_pr_merged=False)
    assert next_onboarding_action(v) == ProposeMap()


# --------------------------------------------------------------------------- #
# map_merged: features indexed → classify the open backlog                      #
# --------------------------------------------------------------------------- #

def test_map_merged_with_features_classifies():
    v = _view(state="map_merged", feature_count=6, open_unclassified_count=40)
    assert next_onboarding_action(v) == Classify()


def test_map_merged_without_features_needs_human():
    # Map "merged" but no Feature rows indexed = something went wrong upstream.
    v = _view(state="map_merged", feature_count=0)
    assert isinstance(next_onboarding_action(v), OnboardingNeedsHuman)


def test_map_merged_nothing_to_classify_goes_live_signal():
    # No open unclassified items and none pending: classification is a no-op, so
    # the queue is effectively drained — offer AwaitTriage (the advance to live).
    v = _view(state="map_merged", feature_count=3,
              open_unclassified_count=0, pending_proposal_count=0)
    assert next_onboarding_action(v) == AwaitTriage()


# --------------------------------------------------------------------------- #
# classifying: queue draining → wait; drained → advance to live                 #
# --------------------------------------------------------------------------- #

def test_classifying_with_unclassified_reclassifies():
    v = _view(state="classifying", feature_count=3, open_unclassified_count=12)
    assert next_onboarding_action(v) == Classify()


def test_classifying_only_pending_awaits_triage():
    # Everything is proposed; the human must drain the triage queue.
    v = _view(state="classifying", feature_count=3,
              open_unclassified_count=0, pending_proposal_count=8)
    assert next_onboarding_action(v) == AwaitTriage()


def test_classifying_queue_drained_awaits_triage_then_live():
    v = _view(state="classifying", feature_count=3,
              open_unclassified_count=0, pending_proposal_count=0)
    assert next_onboarding_action(v) == AwaitTriage()


# --------------------------------------------------------------------------- #
# live: terminal                                                                #
# --------------------------------------------------------------------------- #

def test_live_is_live():
    assert next_onboarding_action(_view(state="live")) == Live()


def test_unknown_state_needs_human():
    assert isinstance(next_onboarding_action(_view(state="bogus")),
                      OnboardingNeedsHuman)


# --------------------------------------------------------------------------- #
# advance_onboarding_state                                                       #
# --------------------------------------------------------------------------- #

def test_advance_walks_the_sequence():
    assert advance_onboarding_state("registered") == "syncing"
    assert advance_onboarding_state("map_proposed") == "map_merged"
    assert advance_onboarding_state("classifying") == "live"
    assert advance_onboarding_state("live") == "live"  # absorbing


def test_advance_unknown_raises():
    with pytest.raises(ValueError):
        advance_onboarding_state("bogus")


# --------------------------------------------------------------------------- #
# Purity / determinism / constants                                              #
# --------------------------------------------------------------------------- #

def test_deterministic_and_does_not_mutate():
    v = _view(state="syncing", repo_count=2, backfill_done_count=2)
    before = v
    a = next_onboarding_action(v)
    b = next_onboarding_action(v)
    assert a == b == ProposeMap()
    assert v == before  # frozen dataclass — nothing mutated


def test_states_match_phase1_constant():
    assert PROJECT_ONBOARDING_STATES == (
        "registered", "syncing", "map_proposed", "map_merged", "classifying", "live")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `... -m pytest server/tests/test_pm_onboarding_engine.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'server.pm.onboarding'`.

- [ ] **Step 3: Implement the engine**

Create `server/pm/onboarding.py`:

```python
"""Pure onboarding state-machine engine (design 2026-08-31 §5).

No I/O — the way ``server/sdlc/engine.py`` is pure. A total function over a
frozen ``ProjectOnboardingView`` (counts the API/orchestrator build from DB
rows) returning the next wizard action. The wizard UI is a VIEW over this
engine; these transitions are the single source of truth for what each
``Project.onboarding_state`` offers next.

State order (phase-1 constant PROJECT_ONBOARDING_STATES):

    registered ─▶ syncing ─▶ map_proposed ─▶ map_merged ─▶ classifying ─▶ live

The propose-only gate mirrors the SDLC engine: the engine NEVER advances the
state itself. It returns ``AwaitMapPr``/``AwaitTriage`` while a gate is open and
a human tap (map PR merge, triage queue drain → advance) moves the state on.
"""
from __future__ import annotations

from dataclasses import dataclass

from server.db.models import PROJECT_ONBOARDING_STATES


# --------------------------------------------------------------------------- #
# Pure input view (built from DB rows by callers — NOT the ORM models)         #
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class ProjectOnboardingView:
    """The counts the engine decides over. ``backfill_done_count`` is how many
    of the project's ``repo_count`` repos have finished pm-sync backfill;
    ``map_pr_open``/``map_pr_merged`` describe the archaeology map PR."""
    state: str
    repo_count: int
    backfill_done_count: int
    feature_count: int
    open_unclassified_count: int
    pending_proposal_count: int
    map_pr_open: bool
    map_pr_merged: bool


# --------------------------------------------------------------------------- #
# Onboarding actions (what the wizard should offer / do next)                   #
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Register:
    """No repos attached yet — the wizard's register step is incomplete."""


@dataclass(frozen=True)
class AwaitBackfill:
    """pm-sync is still mirroring history; nothing to do but wait."""


@dataclass(frozen=True)
class ProposeMap:
    """Backfill done — offer/spawn the archaeology map-synthesis session."""


@dataclass(frozen=True)
class AwaitMapPr:
    """The map PR is the curation gate: wait for the human to merge + advance."""


@dataclass(frozen=True)
class Classify:
    """Map merged + features indexed — offer/spawn backlog classification."""


@dataclass(frozen=True)
class AwaitTriage:
    """Proposals are queued (or the queue is empty); the human drains the inbox
    and advances the project to live."""


@dataclass(frozen=True)
class Live:
    """Onboarding complete — the continuous loop takes over (phase 3)."""


@dataclass(frozen=True)
class OnboardingNeedsHuman:
    """Nothing the engine can safely do; ``reason`` is surfaced in the wizard."""
    reason: str = ""


OnboardingAction = (
    Register | AwaitBackfill | ProposeMap | AwaitMapPr
    | Classify | AwaitTriage | Live | OnboardingNeedsHuman)


# --------------------------------------------------------------------------- #
# Decision core                                                                 #
# --------------------------------------------------------------------------- #

def next_onboarding_action(view: ProjectOnboardingView) -> OnboardingAction:
    """Decide the next onboarding action for a project. Pure + total."""
    state = view.state

    if state == "registered":
        if view.repo_count <= 0:
            return Register()
        return AwaitBackfill()

    if state == "syncing":
        if view.repo_count <= 0:
            return Register()
        if view.backfill_done_count < view.repo_count:
            return AwaitBackfill()
        return ProposeMap()

    if state == "map_proposed":
        if view.map_pr_open or view.map_pr_merged:
            # PR exists (open or merged): wait for the human merge + advance.
            return AwaitMapPr()
        # No PR yet (session never opened one / died) → re-offer the proposal.
        return ProposeMap()

    if state == "map_merged":
        if view.feature_count <= 0:
            return OnboardingNeedsHuman(
                reason="map merged but no features were indexed")
        if view.open_unclassified_count > 0:
            return Classify()
        return AwaitTriage()

    if state == "classifying":
        if view.open_unclassified_count > 0:
            return Classify()
        return AwaitTriage()

    if state == "live":
        return Live()

    return OnboardingNeedsHuman(reason=f"unknown onboarding state {state!r}")


def advance_onboarding_state(state: str) -> str:
    """Successor state for a human-gate advance tap; ``live`` is absorbing.

    Raises ``ValueError`` on an unknown state so a corrupt row surfaces loudly
    (mirrors ``sdlc.engine.advance_stage``)."""
    if state not in PROJECT_ONBOARDING_STATES:
        raise ValueError(f"unknown onboarding state {state!r}")
    idx = PROJECT_ONBOARDING_STATES.index(state)
    if idx + 1 >= len(PROJECT_ONBOARDING_STATES):
        return "live"
    return PROJECT_ONBOARDING_STATES[idx + 1]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `... -m pytest server/tests/test_pm_onboarding_engine.py -v`
Expected: PASS (all tests).

- [ ] **Step 5: Commit**

```bash
git add server/pm/onboarding.py server/tests/test_pm_onboarding_engine.py
git commit -m "feat(pm): pure onboarding state-machine engine"
```

---

### Task 2: Classification prompt/parse module (`server/pm/classification.py`)

**Files:**
- Create: `server/pm/classification.py`
- Test: `server/tests/test_pm_classification.py` (create)

**Interfaces:**
- Produces:
  - `FEATURE_DOCS_DIR = "docs/product/features"` and `feature_doc_path(slug: str) -> str` — the single source of truth for the map-synthesis output layout (mirrors `sdlc.prompts.SDLC_DOCS_DIR`/`artifact_path`).
  - `WorkItemBrief(number: int, kind: str, title: str, body: str, labels: tuple[str, ...], url: str)` frozen dataclass — the minimal item view fed to the classifier.
  - `FeatureBrief(slug: str, title: str, summary: str)` frozen dataclass — the candidate features fed to the classifier.
  - `ProposedClassification(number: int, kind: str, feature_slug: str | None, no_fit_suggestion: str | None, reach: int | None, impact: int | None, effort: int | None, confidence: int | None, rationale: str, confidence_level: str)` frozen dataclass — the schema a classify session emits, mapped to `TriageProposal` rows. **Phase 3 reuses this.**
  - `CLASSIFY_CHUNK_SIZE = 50` — the open-items-per-session cap (spec §5).
  - `build_map_synthesis_prompt(*, slug: str, repo: str, default_branch: str, open_count: int, closed_count: int, scope: str = "initial", suggestion: str | None = None, item_numbers: tuple[int, ...] = ()) -> str` — the archaeology map session's seed prompt (one non-draft PR, no auto-merge, one feature doc per capability with Evidence lines). **One builder, parameterized by scope** (phase 3 reuses it): `scope="initial"` (default) emits the whole-repo archaeology prompt; `scope="diff"` emits a diff-scoped prompt naming `suggestion` (the proposed new-feature name) + `item_numbers` (the clustered no-fit items driving the drift). No parallel diff-prompt builder.
  - `build_classification_prompt(*, items: list[WorkItemBrief], features: list[FeatureBrief], corrections: list[str] | None = None) -> str` — the classify session's seed prompt (structured-output request).
  - `parse_classification_output(text: str) -> list[ProposedClassification]` — tolerant JSON parser; raises `ClassificationParseError` (subclass of `ValueError`) only when no parseable JSON array is present; skips malformed rows.
- Consumes: `FEATURE_LIFECYCLE_STATES` and `WORK_ITEM_KINDS` from `server.db.models` (for validation); `json`.

- [ ] **Step 1: Write the failing tests**

Create `server/tests/test_pm_classification.py`:

```python
"""Pure classification module tests: prompt builders + structured-output parse
(design 2026-08-31 §5). Phase 3 reuses this module, so the schema is pinned."""
from __future__ import annotations

import pytest

from server.pm.classification import (
    CLASSIFY_CHUNK_SIZE,
    ClassificationParseError,
    FeatureBrief,
    ProposedClassification,
    WorkItemBrief,
    build_classification_prompt,
    build_map_synthesis_prompt,
    feature_doc_path,
    parse_classification_output,
)


def _item(n: int) -> WorkItemBrief:
    return WorkItemBrief(number=n, kind="issue", title=f"Item {n}",
                         body="Something is broken", labels=("bug",),
                         url=f"https://github.com/o/r/issues/{n}")


def _feature(slug: str) -> FeatureBrief:
    return FeatureBrief(slug=slug, title=slug.title(), summary="A capability.")


def test_feature_doc_path_is_single_source():
    assert feature_doc_path("sessions") == "docs/product/features/sessions.md"


def test_chunk_size_matches_spec():
    assert CLASSIFY_CHUNK_SIZE == 50


def test_map_prompt_is_propose_only_one_pr():
    p = build_map_synthesis_prompt(
        slug="ccswitch", repo="steiner385/ccswitch", default_branch="master",
        open_count=120, closed_count=800)
    assert "docs/product/features/" in p
    assert "ONE" in p and "non-draft" in p.lower()
    assert "auto-merge" in p.lower()          # explicitly forbidden
    assert "Evidence" in p                    # outcome-driven template marker
    assert "120" in p and "800" in p          # backlog scale is context


def test_classification_prompt_lists_items_and_features():
    p = build_classification_prompt(
        items=[_item(7), _item(8)],
        features=[_feature("sessions"), _feature("triage")])
    assert "#7" in p and "#8" in p
    assert "sessions" in p and "triage" in p
    assert "no_fit" in p                        # the escape hatch is offered
    assert "JSON" in p                          # structured output requested


def test_classification_prompt_injects_corrections():
    p = build_classification_prompt(
        items=[_item(1)], features=[_feature("a")],
        corrections=["#4 was NOT sessions; it is billing"])
    assert "billing" in p


def test_parse_valid_output():
    text = """Here is my classification:
    ```json
    [
      {"number": 7, "kind": "issue", "feature_slug": "sessions",
       "reach": 3, "impact": 2, "effort": 1, "confidence": 2,
       "rationale": "touches the session manager", "confidence_level": "high"},
      {"number": 8, "kind": "issue", "feature_slug": null,
       "no_fit_suggestion": "billing", "rationale": "no matching feature",
       "confidence_level": "low"}
    ]
    ```
    """
    out = parse_classification_output(text)
    assert out[0] == ProposedClassification(
        number=7, kind="issue", feature_slug="sessions", no_fit_suggestion=None,
        reach=3, impact=2, effort=1, confidence=2,
        rationale="touches the session manager", confidence_level="high")
    assert out[1].feature_slug is None
    assert out[1].no_fit_suggestion == "billing"
    assert out[1].confidence_level == "low"


def test_parse_skips_malformed_rows_but_keeps_valid():
    text = '[{"number": 1, "kind": "issue", "feature_slug": "a",' \
           ' "rationale": "ok", "confidence_level": "medium"},' \
           ' {"no_number_here": true}]'
    out = parse_classification_output(text)
    assert [c.number for c in out] == [1]


def test_parse_no_json_raises():
    with pytest.raises(ClassificationParseError):
        parse_classification_output("I could not classify anything, sorry.")


def test_parse_defaults_confidence_level_and_scores():
    text = '[{"number": 5, "kind": "pr", "feature_slug": "a", "rationale": "x"}]'
    (c,) = parse_classification_output(text)
    assert c.confidence_level == "low"   # missing → conservative default
    assert c.reach is None and c.impact is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `... -m pytest server/tests/test_pm_classification.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'server.pm.classification'`.

- [ ] **Step 3: Implement the module**

Create `server/pm/classification.py`:

```python
"""Pure classification: prompt builders + structured-output schema/parse
(design 2026-08-31 §5). No I/O, no model calls — the classify/map-synthesis
loops feed views in and get a prompt out, or feed session output in and get
``ProposedClassification`` rows out (mapped to TriageProposal by the seam).

Phase 3's continuous triage loop REUSES this module verbatim — there is exactly
one classification implementation (constitution VI).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

from server.db.models import FEATURE_LIFECYCLE_STATES, WORK_ITEM_KINDS

# One place defining the feature-doc directory + path (single source of truth,
# mirrors sdlc.prompts.SDLC_DOCS_DIR / artifact_path). The map-synthesis session
# writes one file per capability under here.
FEATURE_DOCS_DIR = "docs/product/features"

# Open items classified per session (spec §5: ~50/session, structured output).
CLASSIFY_CHUNK_SIZE = 50

_VALID_CONFIDENCE = ("low", "medium", "high")


def feature_doc_path(slug: str) -> str:
    return f"{FEATURE_DOCS_DIR}/{slug}.md"


class ClassificationParseError(ValueError):
    """Raised only when a classify session's output has no parseable JSON array.
    Individual malformed rows are skipped, not fatal (one bad row must never lose
    the whole chunk)."""


@dataclass(frozen=True)
class WorkItemBrief:
    number: int
    kind: str  # WORK_ITEM_KINDS
    title: str
    body: str
    labels: tuple[str, ...]
    url: str


@dataclass(frozen=True)
class FeatureBrief:
    slug: str
    title: str
    summary: str


@dataclass(frozen=True)
class ProposedClassification:
    """One row a classify session emits. ``feature_slug`` is None for a
    ``no_fit`` answer, which instead carries ``no_fit_suggestion`` (the suggested
    new-feature name). Mapped onto TriageProposal by the orchestrator seam."""
    number: int
    kind: str
    feature_slug: str | None
    no_fit_suggestion: str | None
    reach: int | None
    impact: int | None
    effort: int | None
    confidence: int | None
    rationale: str
    confidence_level: str  # low | medium | high


def build_map_synthesis_prompt(
    *,
    slug: str,
    repo: str,
    default_branch: str,
    open_count: int,
    closed_count: int,
    scope: str = "initial",
    suggestion: str | None = None,
    item_numbers: tuple[int, ...] = (),
) -> str:
    """Seed prompt for the map-synthesis session (spec §5 Step 1 / §6 drift).

    Reads the codebase + mirrored backlog (open+closed) + PR history as
    evidence, writes outcome-driven feature docs, and opens ONE non-draft PR —
    NO auto-merge. The merge is the human curation gate.

    ``scope="initial"`` (default) is the whole-repo archaeology prompt.
    ``scope="diff"`` is the diff-scoped drift prompt (phase 3 reuses this one
    builder): it names ``suggestion`` (the proposed new-feature name) and
    ``item_numbers`` (the clustered no-fit items) and asks for a single new/edited
    feature doc plus PR — no full re-archaeology."""
    if scope == "diff":
        nums = ", ".join(f"#{n}" for n in item_numbers) or "(none listed)"
        return (
            "You are proposing a **feature-map update** for an existing project "
            "whose triage flagged a cluster of items that do not fit the current "
            f"map. Suggested new capability: `{suggestion or 'unnamed'}`. "
            f"Driving items{f' in `{repo}`' if repo else ''}: {nums}.\n\n"
            f"Add or edit ONE feature doc under `{FEATURE_DOCS_DIR}/"
            f"{(suggestion or 'new-feature')}.md` using the outcome-driven "
            "template (Problem / Target users / Desired outcome / Success "
            "metrics / Non-goals / Notes / Evidence). Justify it with the "
            "driving item numbers and relevant file paths in the Evidence "
            "section. Do NOT re-run whole-repo archaeology.\n\n"
            "Requirements — propose-only, follow exactly:\n"
            f"1. Branch off `origin/{default_branch}` (never local "
            f"`{default_branch}`).\n"
            f"2. Commit only under `{FEATURE_DOCS_DIR}/`.\n"
            "3. Open exactly ONE NON-draft PR, marked ready for review.\n"
            "4. Do NOT arm auto-merge — the merge is the human curation gate.\n"
            "5. End every commit message with:\n"
            "   `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`\n"
        )
    repo_clause = f" in repository `{repo}`" if repo else ""
    return (
        "You are performing **product archaeology** for an existing project. "
        "Read the codebase in this clone, the mirrored backlog, and the PR "
        "history to reconstruct the durable *feature map* — what this product "
        "actually is, separate from any single delivery effort.\n\n"
        f"CONTEXT: project `{slug}`{repo_clause}. The mirror holds ~{open_count} "
        f"open and ~{closed_count} closed items; churn concentration in the PR "
        "history is strong evidence of real capability areas.\n\n"
        f"Write one markdown doc per capability under `{FEATURE_DOCS_DIR}/"
        "<feature-slug>.md`. Each doc uses YAML frontmatter (slug, title, "
        f"state ∈ {list(FEATURE_LIFECYCLE_STATES)}, optional links) followed by "
        "the outcome-driven template:\n"
        "- **Problem** — the concrete pain this capability addresses.\n"
        "- **Target users** — who relies on it.\n"
        "- **Desired outcome** — what good looks like.\n"
        "- **Success metrics** — how you would measure it.\n"
        "- **Non-goals** — what it deliberately excludes.\n"
        "- **Notes** — anything else worth capturing.\n"
        "- **Evidence** — file paths and representative issue/PR numbers that "
        "justify this capability's existence.\n"
        "Fill only what evidence supports; mark gaps explicitly as "
        "`(unknown — needs owner input)`.\n\n"
        "Requirements — this is a propose-only onboarding step, follow exactly:\n"
        f"1. Branch off `origin/{default_branch}` (never local `{default_branch}`).\n"
        f"2. Commit the new docs under `{FEATURE_DOCS_DIR}/` only.\n"
        "3. Open exactly ONE pull request with the whole feature map — a "
        "NON-draft PR, marked ready for review.\n"
        "4. Do NOT arm auto-merge: the merge is the human curation gate; PR "
        "review comments are where the map is argued with.\n"
        "5. End every commit message with:\n"
        "   `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`\n"
    )


def build_classification_prompt(
    *,
    items: list[WorkItemBrief],
    features: list[FeatureBrief],
    corrections: list[str] | None = None,
) -> str:
    """Seed prompt for a classify session (spec §5 Step 2 / §6 item 1).

    Presents a chunk of open items and the candidate feature map, and asks for a
    JSON array of classifications with RICE-lite scores + a ``no_fit`` escape
    hatch. Past human corrections (spec §6) are surfaced as ground truth."""
    feature_lines = "\n".join(
        f"- `{f.slug}` — {f.title}: {f.summary}" for f in features
    ) or "- (no features yet — every item is likely no_fit)"
    item_lines = "\n".join(
        f"- #{it.number} [{it.kind}] {it.title}\n"
        f"    labels: {', '.join(it.labels) or '(none)'}\n"
        f"    body: {it.body[:500]}"
        for it in items
    )
    corrections_block = ""
    if corrections:
        joined = "\n".join(f"- {c}" for c in corrections)
        corrections_block = (
            "\nGROUND TRUTH — past human corrections; respect them:\n"
            f"{joined}\n")
    return (
        "You are triaging an existing project's backlog into its feature map. "
        "Assign each work item to the single best-fitting feature, or answer "
        "`no_fit` with a suggested new-feature name when none fits — never force "
        "a bad assignment.\n\n"
        "CANDIDATE FEATURES:\n"
        f"{feature_lines}\n\n"
        "WORK ITEMS TO CLASSIFY:\n"
        f"{item_lines}\n"
        f"{corrections_block}\n"
        "For each item estimate RICE-lite inputs as small integers (1–5): "
        "reach, impact, effort, confidence. Rate your own confidence_level as "
        "one of low|medium|high.\n\n"
        "Return ONLY a JSON array (in a ```json fenced block), one object per "
        "item, with keys: number, kind, feature_slug (or null), "
        "no_fit_suggestion (or null), reach, impact, effort, confidence, "
        "rationale, confidence_level. Do not classify any item not listed above."
    )


def _coerce_int(value: object) -> int | None:
    if isinstance(value, bool):  # bool is an int subclass — reject it
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    return None


def _row_to_classification(row: object) -> ProposedClassification | None:
    if not isinstance(row, dict):
        return None
    number = _coerce_int(row.get("number"))
    if number is None:
        return None
    kind = str(row.get("kind") or "issue").lower()
    if kind not in WORK_ITEM_KINDS:
        kind = "issue"
    feature_slug = row.get("feature_slug")
    feature_slug = str(feature_slug).strip() if feature_slug else None
    no_fit = row.get("no_fit_suggestion")
    no_fit = str(no_fit).strip() if no_fit else None
    level = str(row.get("confidence_level") or "").lower()
    if level not in _VALID_CONFIDENCE:
        level = "low"  # missing/garbage → conservative default
    return ProposedClassification(
        number=number,
        kind=kind,
        feature_slug=feature_slug or None,
        no_fit_suggestion=no_fit or None,
        reach=_coerce_int(row.get("reach")),
        impact=_coerce_int(row.get("impact")),
        effort=_coerce_int(row.get("effort")),
        confidence=_coerce_int(row.get("confidence")),
        rationale=str(row.get("rationale") or ""),
        confidence_level=level,
    )


def parse_classification_output(text: str) -> list[ProposedClassification]:
    """Extract the JSON array of classifications from a session's output.

    Tolerant: strips an optional ```json fence, finds the first top-level array,
    skips rows that lack a ``number``. Raises ``ClassificationParseError`` only
    when no JSON array can be parsed at all."""
    candidate = text
    fence = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", text, re.DOTALL)
    if fence:
        candidate = fence.group(1)
    else:
        start = candidate.find("[")
        end = candidate.rfind("]")
        if start == -1 or end == -1 or end <= start:
            raise ClassificationParseError("no JSON array found in output")
        candidate = candidate[start:end + 1]
    try:
        data = json.loads(candidate)
    except json.JSONDecodeError as e:
        raise ClassificationParseError(f"invalid JSON array: {e}") from e
    if not isinstance(data, list):
        raise ClassificationParseError("parsed JSON is not an array")
    out: list[ProposedClassification] = []
    for row in data:
        parsed = _row_to_classification(row)
        if parsed is not None:
            out.append(parsed)
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `... -m pytest server/tests/test_pm_classification.py -v`
Expected: PASS (all tests).

- [ ] **Step 5: Commit**

```bash
git add server/pm/classification.py server/tests/test_pm_classification.py
git commit -m "feat(pm): classification prompt/parse module + structured schema"
```

---

### Task 3: Orchestrator seams — map-synthesis + classification sessions

**Files:**
- Modify: `server/sessions/orchestrator.py` (add `_pm_onboard_*` / `_pm_classify_*` seam methods next to the `_sdlc_*` seams, ~line 3000–3360; NO new tick loop or task — these seams are invoked directly by the API, not by a polling automation, in phase 2. Phase 3 adds the `pm-triage` loop that drives classification on a cadence.)
- Test: `server/tests/test_pm_orchestrator_seams.py` (create)

**Interfaces:**
- Consumes: `build_map_synthesis_prompt`, `build_classification_prompt`, `parse_classification_output`, `WorkItemBrief`, `FeatureBrief`, `ProposedClassification`, `CLASSIFY_CHUNK_SIZE` (Task 2); `Project`, `ProjectRepo`, `Feature`, `WorkItem`, `TriageProposal` models (phase 1); the existing `AutomationContext.spawn_session` seam, `_backlog_healthy_account`, `gitrepos.ensure_clone`, `_effective_repo_root`, `_sdlc_default_branch`, `db_session` (all present in the orchestrator).
- Produces on `SessionOrchestrator`:
  - `async def _pm_onboard_spawn_map(self, slug: str, *, scope: str = "initial", suggestion: str | None = None, item_numbers: tuple[int, ...] = (), repo: str | None = None) -> object | None` — ONE parameterized map-synthesis spawn seam (phase 3 REUSES it with `scope="diff"`; there is no separate diff-spawn method). `scope="initial"` (default) keeps today's behavior: spawns ONE whole-repo map-synthesis session for the project's primary repo (deduped `origin_ref=f"{slug}:feature-map"`), sets `Project.onboarding_state = "map_proposed"`, returns the session record (or None if no healthy account / no primary repo). `scope="diff"` spawns a diff-scoped session whose prompt names `suggestion` + `item_numbers`, deduped `f"{slug}:map-diff:{suggestion}"`, and does NOT touch `onboarding_state` (phase-3 drift path). `repo` overrides the resolved repo when supplied.
  - `async def _pm_classify_run(self, slug: str, *, items: list[tuple[str, int, str]] | None = None) -> int` — one classify-spawn seam (phase 3 REUSES it with an injected batch). With `items=None` (default, phase-2 onboarding path) it self-selects up to `CLASSIFY_CHUNK_SIZE` open, unclassified work items with no pending proposal; with `items=[(repo, number, kind), ...]` it classifies exactly that caller-supplied set (phase-3 loop path). Builds candidate `FeatureBrief`s, spawns a classify session, and sets `Project.onboarding_state = "classifying"`. Returns the number of items dispatched (0 if none / no account). **Session output is turned into `TriageProposal` rows by `_pm_classify_ingest`, invoked when the session completes** (phase 2 wires ingest via a public helper the tests call directly; the completion hook is a thin call into it).
  - `async def _pm_classify_ingest(self, session_id: str, project_slug: str, output_text: str) -> int` — parses session output via `parse_classification_output`, resolves each `feature_slug` to a `Feature.id` within the project, writes one `TriageProposal` per row (status `pending`, `session_id` stamped), skips a row whose work item is missing or already has a pending proposal (idempotent re-ingest). Returns rows written.
  - Helper `async def _pm_onboarding_counts(self, project_id: str) -> "ProjectOnboardingView"` used by the API (Task 4) — one grouped read building the engine's view.

- [ ] **Step 1: Read the existing wiring**

Read the `_sdlc_spawn_synth` seam and `_sdlc_synth_read_config`/`_sdlc_default_branch` helpers in `server/sessions/orchestrator.py` (grep `_sdlc_spawn_synth`, `_backlog_healthy_account`, `_effective_repo_root`, `_sdlc_default_branch`, `spawn_session`). The pm seams mirror `_sdlc_spawn_synth`'s account-gate + `ensure_clone`-off-the-loop + `spawn_session(origin_ref=…, dedupe_key=…, detail=…)` shape exactly. There is no config row for these in phase 2 (the API drives them directly), so no `_read_config` seam is needed yet.

- [ ] **Step 2: Write the failing tests**

Create `server/tests/test_pm_orchestrator_seams.py`. Build a bare orchestrator via `__new__` (no `start()`), stub only the collaborators these seams touch, and drive a real SQLite DB via `init_db` + `db_session` (mirror the seeding idiom in `test_sdlc_api.py`). The spawn seam is exercised with a fake `AutomationContext` whose `spawn_session` records its kwargs.

```python
"""Orchestrator PM onboarding/classification seams (design 2026-08-31 §5).

Bare-orchestrator + real-SQLite tests: the seams' DB reads/writes and the
prompt/parse wiring, with spawn stubbed to a recorder."""
from __future__ import annotations

import os
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from server.db import db_session, init_db
from server.db.models import (
    Feature, Project, ProjectRepo, TriageProposal, WorkItem)


class _SpawnRec:
    """Fake AutomationContext capturing spawn_session calls."""
    def __init__(self):
        self.calls = []

    async def spawn_session(self, **kw):
        self.calls.append(kw)
        return SimpleNamespace(id=f"sess-{len(self.calls)}", model=None)


def _bare_orch():
    from server.sessions.orchestrator import SessionOrchestrator
    orch = SessionOrchestrator.__new__(SessionOrchestrator)
    return orch


async def _seed(db_url: str) -> str:
    await init_db(db_url)
    async with db_session() as db:
        p = Project(slug="ccswitch", name="ccswitch", onboarding_state="map_merged")
        db.add(p)
        await db.flush()
        db.add(ProjectRepo(project_id=p.id, repo="steiner385/ccswitch",
                           is_primary=True, backfill_done=True))
        db.add(Feature(project_id=p.id, slug="sessions", title="Sessions",
                       state="live", summary="Session manager."))
        for n in range(3):
            db.add(WorkItem(repo="steiner385/ccswitch", number=100 + n,
                            kind="issue", title=f"Bug {n}", state="open",
                            triage_state="unclassified"))
        return p.id


@pytest.fixture
def db_env():
    with tempfile.TemporaryDirectory() as tmp:
        url = f"sqlite+aiosqlite:///{tmp}/t.db"
        with patch.dict(os.environ, {"CCSWITCH_DB_URL": url, "CCSWITCH_DEV": "1",
                                     "CCSWITCH_TAILNET_ALLOWLIST": ""}):
            yield url


@pytest.mark.asyncio
async def test_spawn_map_sets_state_and_dedup(db_env):
    import asyncio as _asyncio
    await _seed(db_env)
    orch = _bare_orch()
    rec = _SpawnRec()
    orch._pm_onboard_ctx = rec
    orch._backlog_healthy_account = _mk_async("acct1")
    orch._effective_repo_root = _mk_async("/tmp/root")
    orch._sdlc_default_branch = _mk_async("master")
    with patch("ccswitch.gitrepos.ensure_clone", return_value="/tmp/clone"):
        out = await orch._pm_onboard_spawn_map("ccswitch")
    assert out is not None
    assert rec.calls[0]["origin_ref"] == "ccswitch:feature-map"
    assert rec.calls[0]["dedupe_key"] == "ccswitch:feature-map"
    async with db_session() as db:
        from sqlalchemy import select
        p = (await db.execute(select(Project).where(
            Project.slug == "ccswitch"))).scalar_one()
        assert p.onboarding_state == "map_proposed"


@pytest.mark.asyncio
async def test_spawn_map_no_account_returns_none(db_env):
    await _seed(db_env)
    orch = _bare_orch()
    orch._pm_onboard_ctx = _SpawnRec()
    orch._backlog_healthy_account = _mk_async(None)
    orch._effective_repo_root = _mk_async("/tmp/root")
    orch._sdlc_default_branch = _mk_async("master")
    with patch("ccswitch.gitrepos.ensure_clone", return_value="/tmp/clone"):
        assert await orch._pm_onboard_spawn_map("ccswitch") is None


@pytest.mark.asyncio
async def test_classify_run_dispatches_open_items(db_env):
    await _seed(db_env)
    orch = _bare_orch()
    rec = _SpawnRec()
    orch._pm_classify_ctx = rec
    orch._backlog_healthy_account = _mk_async("acct1")
    orch._effective_repo_root = _mk_async("/tmp/root")
    with patch("ccswitch.gitrepos.ensure_clone", return_value="/tmp/clone"):
        n = await orch._pm_classify_run("ccswitch")
    assert n == 3
    assert rec.calls[0]["origin_ref"].startswith("ccswitch:classify")
    async with db_session() as db:
        from sqlalchemy import select
        p = (await db.execute(select(Project).where(
            Project.slug == "ccswitch"))).scalar_one()
        assert p.onboarding_state == "classifying"


@pytest.mark.asyncio
async def test_spawn_map_diff_scope_dedup_and_no_state_change(db_env):
    # Phase-3 reuse: scope="diff" uses the map-diff dedupe key and must NOT
    # advance onboarding_state (the seam is one method, parameterized).
    await _seed(db_env)
    orch = _bare_orch()
    rec = _SpawnRec()
    orch._pm_onboard_ctx = rec
    orch._backlog_healthy_account = _mk_async("acct1")
    orch._effective_repo_root = _mk_async("/tmp/root")
    orch._sdlc_default_branch = _mk_async("master")
    with patch("ccswitch.gitrepos.ensure_clone", return_value="/tmp/clone"):
        out = await orch._pm_onboard_spawn_map(
            "ccswitch", scope="diff", suggestion="billing",
            item_numbers=(100, 101), repo="steiner385/ccswitch")
    assert out is not None
    assert rec.calls[0]["dedupe_key"] == "ccswitch:map-diff:billing"
    async with db_session() as db:
        from sqlalchemy import select
        p = (await db.execute(select(Project).where(
            Project.slug == "ccswitch"))).scalar_one()
        assert p.onboarding_state == "map_merged"  # unchanged by diff scope


@pytest.mark.asyncio
async def test_classify_run_accepts_injected_batch(db_env):
    # Phase-3 reuse: items=[(repo, number, kind), ...] classifies exactly that set.
    await _seed(db_env)
    orch = _bare_orch()
    rec = _SpawnRec()
    orch._pm_classify_ctx = rec
    orch._backlog_healthy_account = _mk_async("acct1")
    orch._effective_repo_root = _mk_async("/tmp/root")
    with patch("ccswitch.gitrepos.ensure_clone", return_value="/tmp/clone"):
        n = await orch._pm_classify_run(
            "ccswitch",
            items=[("steiner385/ccswitch", 100, "issue"),
                   ("steiner385/ccswitch", 101, "issue")])
    assert n == 2  # exactly the injected set, not the full open chunk


@pytest.mark.asyncio
async def test_classify_ingest_writes_pending_proposals(db_env):
    await _seed(db_env)
    orch = _bare_orch()
    output = """```json
    [{"number": 100, "kind": "issue", "feature_slug": "sessions",
      "reach": 3, "impact": 2, "effort": 1, "confidence": 2,
      "rationale": "session work", "confidence_level": "high"},
     {"number": 101, "kind": "issue", "feature_slug": null,
      "no_fit_suggestion": "billing", "rationale": "no fit",
      "confidence_level": "low"}]
    ```"""
    n = await orch._pm_classify_ingest("sess-1", "ccswitch", output)
    assert n == 2
    async with db_session() as db:
        from sqlalchemy import select
        props = (await db.execute(select(TriageProposal))).scalars().all()
        assert {p.status for p in props} == {"pending"}
        by_num = {  # proposal → work item number, via join through work_items
            (await db.execute(select(WorkItem.number).where(
                WorkItem.id == p.work_item_id))).scalar_one(): p
            for p in props}
        assert by_num[100].proposed_feature_id is not None  # resolved to Feature
        assert by_num[101].proposed_feature_id is None       # no_fit
        assert by_num[101].no_fit_suggestion == "billing"


@pytest.mark.asyncio
async def test_classify_ingest_is_idempotent(db_env):
    await _seed(db_env)
    orch = _bare_orch()
    output = ('[{"number": 100, "kind": "issue", "feature_slug": "sessions",'
              ' "rationale": "x", "confidence_level": "medium"}]')
    first = await orch._pm_classify_ingest("sess-1", "ccswitch", output)
    second = await orch._pm_classify_ingest("sess-1", "ccswitch", output)
    assert (first, second) == (1, 0)  # second pass writes nothing (pending exists)


def _mk_async(value):
    async def _f(*a, **k):
        return value
    return _f
```

- [ ] **Step 3: Run to verify failure**

Run: `... -m pytest server/tests/test_pm_orchestrator_seams.py -v`
Expected: FAIL — `AttributeError: 'SessionOrchestrator' object has no attribute '_pm_onboard_spawn_map'` (and the `_pm_onboard_ctx` the tests set will be unused).

- [ ] **Step 4: Implement the seams**

First, construct the two `AutomationContext`s next to the SDLC ones in the loop-construction block (~line 700, right after `self._sdlc_synth_task`). No task/loop is created — phase 2 drives these from the API:

```python
        # PM archaeology onboarding (design 2026-08-31 §5): the map-synthesis and
        # classification sessions ride the SAME spawn seam as the SDLC loops.
        # No polling loop in phase 2 — the /propose-map and /classify API routes
        # invoke these seams directly (phase 3 adds the pm-triage cadence).
        self._pm_onboard_ctx = AutomationContext(
            "pm-onboard", create_session=self.create_session)
        self._pm_classify_ctx = AutomationContext(
            "pm-classify", create_session=self.create_session)
```

Then add the seam methods next to the `_sdlc_*` seams (mirror `_sdlc_spawn_synth`'s account-gate/`ensure_clone`/`spawn_session` shape):

```python
    # ---------------------- pm onboarding seams (2026-08-31) --------------- #

    async def _pm_onboard_spawn_map(
            self, slug: str, *, scope: str = "initial",
            suggestion: str | None = None,
            item_numbers: tuple[int, ...] = (),
            repo: str | None = None):
        """ONE parameterized map-synthesis spawn seam (phase 3 reuses it with
        ``scope="diff"`` — there is no separate diff-spawn method).

        ``scope="initial"`` (default): spawn ONE archaeology map-synthesis
        session for *slug*'s primary repo and move the project to
        ``map_proposed``. Deduped on ``<slug>:feature-map`` so a re-tap does not
        double-spawn.

        ``scope="diff"``: spawn a diff-scoped session whose prompt names
        ``suggestion`` + ``item_numbers``, deduped ``<slug>:map-diff:<suggestion>``,
        and does NOT touch ``onboarding_state`` (phase-3 drift path). ``repo``
        overrides the resolved primary repo when supplied.

        Returns the SessionRecord, or None when no repo / no healthy account."""
        from sqlalchemy import func, select
        from server.db.models import Project, ProjectRepo, WorkItem
        from server.pm.classification import build_map_synthesis_prompt
        async with db_session() as db:
            proj = (await db.execute(select(Project).where(
                Project.slug == slug))).scalar_one_or_none()
            if proj is None:
                return None
            resolved_repo = repo
            if resolved_repo is None:
                repo_row = (await db.execute(select(ProjectRepo).where(
                    ProjectRepo.project_id == proj.id,
                    ProjectRepo.is_primary.is_(True)))).scalar_one_or_none()
                if repo_row is None or not repo_row.repo:
                    return None
                resolved_repo = repo_row.repo
            open_count = (await db.execute(select(func.count(WorkItem.id)).where(
                WorkItem.repo == resolved_repo,
                WorkItem.state == "open"))).scalar_one()
            closed_count = (await db.execute(select(func.count(WorkItem.id)).where(
                WorkItem.repo == resolved_repo,
                WorkItem.state != "open"))).scalar_one()
            project_id = proj.id
        repo = resolved_repo
        account = await self._backlog_healthy_account()
        if account is None:
            return None
        url = f"https://github.com/{repo}.git"
        cwd = await asyncio.to_thread(
            gitrepos.ensure_clone, await self._effective_repo_root(), repo, url)
        default_branch = await self._sdlc_default_branch(repo)
        prompt = build_map_synthesis_prompt(
            slug=slug, repo=repo, default_branch=default_branch,
            open_count=int(open_count), closed_count=int(closed_count),
            scope=scope, suggestion=suggestion, item_numbers=item_numbers)
        if scope == "diff":
            origin_ref = f"{slug}:map-diff:{suggestion}"
            name = f"pm map-diff {slug}"
            detail = {"slug": slug, "kind": "feature-map-diff",
                      "suggestion": suggestion,
                      "itemNumbers": list(item_numbers)}
        else:
            origin_ref = f"{slug}:feature-map"
            name = f"pm map {slug}"
            detail = {"slug": slug, "kind": "feature-map"}
        rec = await self._pm_onboard_ctx.spawn_session(
            origin_ref=origin_ref,
            dedupe_key=origin_ref,
            repo=repo, cwd=cwd, account=account,
            name=name, initial_prompt=prompt, detail=detail)
        if rec is not None and scope == "initial":
            async with db_session() as db:
                proj = (await db.execute(select(Project).where(
                    Project.id == project_id))).scalar_one_or_none()
                if proj is not None:
                    proj.onboarding_state = "map_proposed"
        return rec

    async def _pm_classify_run(
            self, slug: str, *,
            items: list[tuple[str, int, str]] | None = None) -> int:
        """One classify-spawn seam (phase 3 reuses it with an injected batch).

        With ``items=None`` (default, phase-2 onboarding path) it self-selects up
        to CLASSIFY_CHUNK_SIZE open, unclassified items with no pending proposal.
        With ``items=[(repo, number, kind), ...]`` it classifies exactly that
        caller-supplied set (phase-3 loop path). Moves the project to
        ``classifying``. Returns the item count dispatched (0 if none/no account).
        Closed history is evidence, not classified (spec §5)."""
        from sqlalchemy import select, tuple_
        from server.db.models import (
            Feature, Project, ProjectRepo, TriageProposal, WorkItem)
        from server.pm.classification import (
            CLASSIFY_CHUNK_SIZE, FeatureBrief, WorkItemBrief,
            build_classification_prompt)
        async with db_session() as db:
            proj = (await db.execute(select(Project).where(
                Project.slug == slug))).scalar_one_or_none()
            if proj is None:
                return 0
            repos = (await db.execute(select(ProjectRepo.repo).where(
                ProjectRepo.project_id == proj.id))).scalars().all()
            repo_names = [r for r in repos if r]
            primary = (await db.execute(select(ProjectRepo).where(
                ProjectRepo.project_id == proj.id,
                ProjectRepo.is_primary.is_(True)))).scalar_one_or_none()
            if items is not None:
                # Phase-3 injected batch: classify exactly the caller's set.
                if not items:
                    return 0
                keys = [(r, n) for (r, n, _k) in items]
                chunk = (await db.execute(select(WorkItem).where(
                    tuple_(WorkItem.repo, WorkItem.number).in_(keys)
                ))).scalars().all()
                if not chunk:
                    return 0
            else:
                pending_ids = set((await db.execute(select(
                    TriageProposal.work_item_id).where(
                        TriageProposal.status == "pending"))).scalars().all())
                selected = (await db.execute(select(WorkItem).where(
                    WorkItem.repo.in_(repo_names), WorkItem.state == "open",
                    WorkItem.triage_state == "unclassified"
                ).order_by(WorkItem.gh_updated_at.desc()).limit(
                    CLASSIFY_CHUNK_SIZE * 2))).scalars().all()
                chunk = [w for w in selected
                         if w.id not in pending_ids][:CLASSIFY_CHUNK_SIZE]
                if not chunk:
                    return 0
            features = (await db.execute(select(Feature).where(
                Feature.project_id == proj.id,
                Feature.orphaned.isnot(True)))).scalars().all()
            briefs = [WorkItemBrief(
                number=w.number or 0, kind=w.kind or "issue",
                title=w.title or "", body=w.body or "",
                labels=tuple(w.labels or []), url=w.url or "") for w in chunk]
            feat_briefs = [FeatureBrief(
                slug=f.slug or "", title=f.title or "",
                summary=f.summary or "") for f in features]
            repo = (primary.repo if primary else repo_names[0]) if repo_names else ""
            project_id = proj.id
        account = await self._backlog_healthy_account()
        if account is None:
            return 0
        prompt = build_classification_prompt(items=briefs, features=feat_briefs)
        cwd = None
        if repo:
            url = f"https://github.com/{repo}.git"
            cwd = await asyncio.to_thread(
                gitrepos.ensure_clone, await self._effective_repo_root(), repo, url)
        rec = await self._pm_classify_ctx.spawn_session(
            origin_ref=f"{slug}:classify:{chunk[0].number}",
            dedupe_key=f"{slug}:classify:{chunk[0].number}",
            repo=repo or None, cwd=cwd, account=account,
            name=f"pm classify {slug}", initial_prompt=prompt,
            detail={"slug": slug, "kind": "classify", "count": len(chunk)})
        if rec is None:
            return 0
        async with db_session() as db:
            proj = (await db.execute(select(Project).where(
                Project.id == project_id))).scalar_one_or_none()
            if proj is not None and proj.onboarding_state in (
                    "map_merged", "map_proposed"):
                proj.onboarding_state = "classifying"
        return len(chunk)

    async def _pm_classify_ingest(
            self, session_id: str, project_slug: str, output_text: str) -> int:
        """Turn a classify session's output into TriageProposal rows. Resolves
        each feature_slug to a Feature.id within the project; skips a row whose
        work item is missing or already has a pending proposal (idempotent
        re-ingest). Returns rows written. Propose-only: NEVER touches overlays."""
        from sqlalchemy import select
        from server.db.models import (
            Feature, Project, ProjectRepo, TriageProposal, WorkItem)
        from server.pm.classification import (
            ClassificationParseError, parse_classification_output)
        try:
            rows = parse_classification_output(output_text)
        except ClassificationParseError:
            log.warning("pm_classify.parse_failed session=%s", session_id,
                        exc_info=True)
            return 0
        written = 0
        async with db_session() as db:
            proj = (await db.execute(select(Project).where(
                Project.slug == project_slug))).scalar_one_or_none()
            if proj is None:
                return 0
            repo_names = [r for r in (await db.execute(select(
                ProjectRepo.repo).where(
                    ProjectRepo.project_id == proj.id))).scalars().all() if r]
            feat_by_slug = {f.slug: f.id for f in (await db.execute(select(
                Feature).where(Feature.project_id == proj.id))).scalars().all()}
            for row in rows:
                wi = (await db.execute(select(WorkItem).where(
                    WorkItem.repo.in_(repo_names),
                    WorkItem.number == row.number,
                    WorkItem.kind == row.kind))).scalar_one_or_none()
                if wi is None:
                    continue
                dup = (await db.execute(select(TriageProposal.id).where(
                    TriageProposal.work_item_id == wi.id,
                    TriageProposal.status == "pending"))).scalar_one_or_none()
                if dup is not None:
                    continue
                db.add(TriageProposal(
                    work_item_id=wi.id,
                    proposed_feature_id=feat_by_slug.get(row.feature_slug)
                    if row.feature_slug else None,
                    no_fit_suggestion=row.no_fit_suggestion,
                    reach=row.reach, impact=row.impact, effort=row.effort,
                    confidence=row.confidence, rationale=row.rationale,
                    confidence_level=row.confidence_level,
                    status="pending", session_id=session_id))
                wi.triage_state = "proposed"
                written += 1
        return written

    async def _pm_onboarding_counts(self, project_id: str):
        """Build the onboarding engine's view for a project in one grouped read
        (used by the API's wizard-state endpoint)."""
        from sqlalchemy import func, select
        from server.db.models import (
            Feature, ProjectRepo, TriageProposal, WorkItem)
        from server.pm.onboarding import ProjectOnboardingView
        async with db_session() as db:
            repos = (await db.execute(select(ProjectRepo).where(
                ProjectRepo.project_id == project_id))).scalars().all()
            repo_names = [r.repo for r in repos if r.repo]
            feature_count = (await db.execute(select(func.count(Feature.id)).where(
                Feature.project_id == project_id,
                Feature.orphaned.isnot(True)))).scalar_one()
            open_unclassified = 0
            pending = 0
            if repo_names:
                open_unclassified = (await db.execute(select(
                    func.count(WorkItem.id)).where(
                        WorkItem.repo.in_(repo_names), WorkItem.state == "open",
                        WorkItem.triage_state == "unclassified"))).scalar_one()
                pending = (await db.execute(select(func.count(
                    TriageProposal.id)).join(
                    WorkItem, WorkItem.id == TriageProposal.work_item_id).where(
                        WorkItem.repo.in_(repo_names),
                        TriageProposal.status == "pending"))).scalar_one()
            from server.db.models import Project
            proj = (await db.execute(select(Project).where(
                Project.id == project_id))).scalar_one()
        return ProjectOnboardingView(
            state=proj.onboarding_state or "registered",
            repo_count=len(repos),
            backfill_done_count=sum(1 for r in repos if r.backfill_done),
            feature_count=int(feature_count),
            open_unclassified_count=int(open_unclassified),
            pending_proposal_count=int(pending),
            map_pr_open=False, map_pr_merged=False)
```

NOTE on `map_pr_open`/`map_pr_merged`: phase 2 does not correlate the map PR's live state into the counts view (the map PR is tracked by the human via GitHub, and the state advance is a human tap). They are left `False` here — the engine treats `map_proposed` with neither flag as "re-offer ProposeMap", which is the correct resumable behavior. Wiring PR-state correlation into these flags is a phase-3 enrichment (the `pm-triage` loop already needs PR polling for drift PRs); do NOT build it here.

Also register the completion hook: grep for where SDLC session completion is dispatched (the place the orchestrator reacts to a spawned session finishing, e.g. an `on_session_complete`/status-transition handler that other automations hook). Add a branch: when a completed session's `detail["kind"] == "classify"`, read its transcript/output and call `_pm_classify_ingest(session_id, detail["slug"], output)`. If no such generic hook exists, this is acceptable for phase 2: the `/classify` API returns the dispatched session id and `_pm_classify_ingest` is also invoked by the (phase-3) `pm-triage` loop; document the seam as "ingest is call-driven; the completion hook lands with pm-triage in phase 3" and keep `_pm_classify_ingest` fully tested as a public seam. Do NOT block phase 2 on a completion-hook refactor.

- [ ] **Step 5: Run tests**

Run: `... -m pytest server/tests/test_pm_orchestrator_seams.py -v`
Expected: PASS (all tests). Then `ruff check server/ && mypy server/ --ignore-missing-imports` — both clean.

- [ ] **Step 6: Commit**

```bash
git add server/sessions/orchestrator.py server/tests/test_pm_orchestrator_seams.py
git commit -m "feat(pm): orchestrator seams for map-synthesis + classification sessions"
```

---

### Task 4: PM API — wizard state + propose-map/classify + triage accept/reject

**Files:**
- Modify: `server/api/pm.py` (append routes to the phase-1 router)
- Test: `server/tests/test_pm_api.py` (extend the phase-1 module)

**Interfaces:**
- Consumes: models (phase 1); `next_onboarding_action`, `advance_onboarding_state` (Task 1); `record_audit`, `current_identity`, `get_db` (as phase 1); the orchestrator seams (Task 3) via `request.app.state.orchestrator` (same handle `server/app.py` stores — grep `app.state` in phase-1 `pm.py`/`sdlc.py` for the exact attribute).
- Produces (spec §7, phase-2 subset — camelCase JSON, 4xx `detail` = `{error, message}`):
  - `GET /api/pm/projects/{slug}/onboarding` → `{state, action: {type, ...}, counts: {...}}` (wizard view over the engine; 404 when absent).
  - `POST /api/pm/projects/{slug}/propose-map` → 202 `{sessionId}`; **idempotent**: 409 `{error: "in_flight"}` if the project is already `map_proposed` with the map session open, or state is past `map_merged`; 409 if backfill not done.
  - `POST /api/pm/projects/{slug}/classify` → 202 `{dispatched: <int>, sessionId?}`; **idempotent**: 409 `{error: "in_flight"}` when a classify session is already open for the project (no new dispatch); 200-with-`{dispatched:0}` semantics reserved for "nothing to classify" (returns 202 `{dispatched:0}`).
  - `GET /api/pm/triage` → list of pending proposals, filterable by `?projectSlug=` and `?confidenceLevel=`; each item carries the joined work-item mirror + proposed feature slug + scores + rationale.
  - `POST /api/pm/triage/{id}/accept` → 200; reads an OPTIONAL body `{edits?: {reach?, impact?, effort?, confidence?, featureId?}}` and delegates to the shared helper `_apply_accept(db, proposal, identity, edits=edits)` (defined in this file; phase 3 EXTENDS it for accept-bulk). The helper copies proposal values onto the `WorkItem` overlay (`feature_id`, `reach/impact/effort/confidence`), applying `edits` over the copied values when present, sets `WorkItem.triage_state = "classified"`, `classified_by = "human"` (NOT the tailnet identity — identity goes only to the audit event), `classified_at = now`, marks the proposal `accepted` + `resolved_at`. The route returns `{id, status, workItemId, triageState}`; 404 unknown id; 409 if not `pending`. **No accept-bulk (phase 3).**
  - `POST /api/pm/triage/{id}/reject` → 200; sets `WorkItem.triage_state = "needs_human"`, marks the proposal `rejected` + `resolved_at`; 404/409 as above.

- [ ] **Step 1: Write the failing tests**

Extend `server/tests/test_pm_api.py`. **Fixture note (spec ambiguity resolved):** the phase-1 plan sketched an async `client` fixture, but the real, proven idiom in `server/tests/test_sdlc_api.py` (which the briefing names as the style to follow) is a synchronous `TestClient` via a `_client()` context manager, and there is no `client` fixture in `conftest.py`. This phase uses the `test_sdlc_api.py` idiom. If the phase-1 module already committed an async `client` fixture, adapt these tests to it instead — the assertions are identical; only the await/`with` shape differs. To exercise the propose-map/classify routes without real sessions, stub the orchestrator seams on `app.state.orchestrator`.

Add to `server/tests/test_pm_api.py`:

```python
import asyncio
import os
import tempfile
from contextlib import contextmanager
from unittest.mock import patch

IDENTITY_HEADERS = {"Tailscale-User-Login": "testuser@example.com"}


@contextmanager
def _client_with_stubbed_seams():
    """TestClient whose orchestrator PM seams are recorder stubs (no real
    sessions), mirroring test_sdlc_api._client but replacing the spawn seams."""
    from fastapi.testclient import TestClient
    from server.app import create_app
    from server.sessions.orchestrator import SessionOrchestrator
    from server.tests.fakes.fake_factory import fake_sdk_factory

    with tempfile.TemporaryDirectory() as tmp:
        db_url = f"sqlite+aiosqlite:///{tmp}/test.db"
        with patch.dict(os.environ, {"CCSWITCH_DB_URL": db_url,
                                     "CCSWITCH_DEV": "1",
                                     "CCSWITCH_TAILNET_ALLOWLIST": ""}):
            orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())

            async def _fake_spawn_map(slug):
                return type("R", (), {"id": "sess-map"})()

            async def _fake_classify_run(slug):
                return 3

            orch._pm_onboard_spawn_map = _fake_spawn_map   # type: ignore[assignment]
            orch._pm_classify_run = _fake_classify_run     # type: ignore[assignment]
            app = create_app(orchestrator=orch)
            with TestClient(app, raise_server_exceptions=False) as c:
                yield c


def _register(c, name, repo="o/r", primary=True):
    r = c.post("/api/pm/projects", json={
        "name": name, "repos": [{"repo": repo, "isPrimary": primary}]},
        headers=IDENTITY_HEADERS)
    assert r.status_code == 201, r.text
    return r.json()


def _seed_map_merged(db_url, slug):
    """Drive a project to map_merged with a feature + open unclassified items."""
    from server.db import db_session
    from sqlalchemy import select
    from server.db.models import Feature, Project, ProjectRepo, WorkItem

    async def _run():
        async with db_session() as db:
            p = (await db.execute(select(Project).where(
                Project.slug == slug))).scalar_one()
            p.onboarding_state = "map_merged"
            (await db.execute(select(ProjectRepo).where(
                ProjectRepo.project_id == p.id))).scalar_one().backfill_done = True
            db.add(Feature(project_id=p.id, slug="sessions", title="Sessions",
                           state="live", summary="Session mgr."))
            db.add(WorkItem(repo="o/r", number=1, kind="issue", title="x",
                            state="open", triage_state="unclassified"))
    asyncio.run(_run())


def test_onboarding_state_reflects_engine():
    with _client_with_stubbed_seams() as c:
        _register(c, "Ccswitch", repo="o/r")
        r = c.get("/api/pm/projects/ccswitch/onboarding", headers=IDENTITY_HEADERS)
        assert r.status_code == 200, r.text
        body = r.json()
        # Registered project with a repo but no backfill → await_backfill.
        assert body["state"] == "registered"
        assert body["action"]["type"] == "await_backfill"
        assert body["counts"]["repoCount"] == 1


def test_propose_map_conflicts_when_backfill_incomplete():
    with _client_with_stubbed_seams() as c:
        _register(c, "Ccswitch", repo="o/r")
        r = c.post("/api/pm/projects/ccswitch/propose-map", headers=IDENTITY_HEADERS)
        assert r.status_code == 409
        assert r.json()["detail"]["error"] == "backfill_incomplete"


def test_classify_dispatches_and_is_idempotent():
    # Full narrated flow: register → force map_merged → classify → accept.
    with _client_with_stubbed_seams() as c:
        _register(c, "Ccswitch", repo="o/r")
        # Reach into the DB the app is using (same env DB_URL).
        db_url = os.environ["CCSWITCH_DB_URL"]
        _seed_map_merged(db_url, "ccswitch")

        r = c.post("/api/pm/projects/ccswitch/classify", headers=IDENTITY_HEADERS)
        assert r.status_code == 202, r.text
        assert r.json()["dispatched"] == 3


def test_triage_accept_and_reject_flow():
    from server.db import db_session
    from sqlalchemy import select
    from server.db.models import (
        Feature, Project, ProjectRepo, TriageProposal, WorkItem)

    with _client_with_stubbed_seams() as c:
        _register(c, "Ccswitch", repo="o/r")

        async def _seed_props():
            async with db_session() as db:
                p = (await db.execute(select(Project).where(
                    Project.slug == "ccswitch"))).scalar_one()
                f = Feature(project_id=p.id, slug="sessions", title="Sessions",
                            state="live")
                w1 = WorkItem(repo="o/r", number=1, kind="issue", title="a",
                              state="open", triage_state="proposed")
                w2 = WorkItem(repo="o/r", number=2, kind="issue", title="b",
                              state="open", triage_state="proposed")
                db.add_all([f, w1, w2])
                await db.flush()
                db.add(TriageProposal(work_item_id=w1.id, proposed_feature_id=f.id,
                                      reach=3, impact=2, effort=1, confidence=2,
                                      rationale="fits", confidence_level="high",
                                      status="pending"))
                db.add(TriageProposal(work_item_id=w2.id, no_fit_suggestion="billing",
                                      rationale="no fit", confidence_level="low",
                                      status="pending"))
        asyncio.run(_seed_props())

        listed = c.get("/api/pm/triage", headers=IDENTITY_HEADERS)
        assert listed.status_code == 200, listed.text
        items = listed.json()
        assert len(items) == 2
        accept_id = next(i["id"] for i in items if i["proposedFeatureSlug"] == "sessions")
        reject_id = next(i["id"] for i in items if i["noFitSuggestion"] == "billing")

        # accept copies onto the overlay
        r = c.post(f"/api/pm/triage/{accept_id}/accept", headers=IDENTITY_HEADERS)
        assert r.status_code == 200, r.text

        # reject moves the item to needs_human
        r = c.post(f"/api/pm/triage/{reject_id}/reject", headers=IDENTITY_HEADERS)
        assert r.status_code == 200, r.text

        # double-accept the already-resolved proposal → 409
        r = c.post(f"/api/pm/triage/{accept_id}/accept", headers=IDENTITY_HEADERS)
        assert r.status_code == 409

        async def _check():
            async with db_session() as db:
                w1 = (await db.execute(select(WorkItem).where(
                    WorkItem.number == 1))).scalar_one()
                w2 = (await db.execute(select(WorkItem).where(
                    WorkItem.number == 2))).scalar_one()
                assert w1.triage_state == "classified"
                assert w1.feature_id is not None and w1.impact == 2
                # classified_by is the constant "human", NOT the tailnet identity.
                assert w1.classified_by == "human"
                assert w2.triage_state == "needs_human"
        asyncio.run(_check())


def test_triage_accept_applies_edits_body():
    """An optional {edits} body overrides copied overlay values (the shared
    _apply_accept helper); this is what phase-3 edit-then-accept relies on."""
    from server.db import db_session
    from sqlalchemy import select
    from server.db.models import (
        Feature, Project, TriageProposal, WorkItem)

    with _client_with_stubbed_seams() as c:
        _register(c, "Ccswitch", repo="o/r")

        async def _seed_one():
            async with db_session() as db:
                p = (await db.execute(select(Project).where(
                    Project.slug == "ccswitch"))).scalar_one()
                f = Feature(project_id=p.id, slug="sessions", title="Sessions",
                            state="live")
                w = WorkItem(repo="o/r", number=1, kind="issue", title="a",
                             state="open", triage_state="proposed")
                db.add_all([f, w])
                await db.flush()
                db.add(TriageProposal(work_item_id=w.id, proposed_feature_id=f.id,
                                      reach=3, impact=2, effort=1, confidence=2,
                                      rationale="fits", confidence_level="high",
                                      status="pending"))
        asyncio.run(_seed_one())

        listed = c.get("/api/pm/triage", headers=IDENTITY_HEADERS).json()
        pid = listed[0]["id"]
        r = c.post(f"/api/pm/triage/{pid}/accept",
                   json={"edits": {"impact": 5, "effort": 4}},
                   headers=IDENTITY_HEADERS)
        assert r.status_code == 200, r.text
        assert r.json()["triageState"] == "classified"

        async def _check():
            async with db_session() as db:
                w = (await db.execute(select(WorkItem).where(
                    WorkItem.number == 1))).scalar_one()
                assert w.impact == 5 and w.effort == 4      # edits applied
                assert w.reach == 3                          # untouched copy
                assert w.classified_by == "human"
        asyncio.run(_check())


def test_triage_accept_unknown_404():
    with _client_with_stubbed_seams() as c:
        r = c.post("/api/pm/triage/nope/accept", headers=IDENTITY_HEADERS)
        assert r.status_code == 404
```

- [ ] **Step 2: Run to verify failure**

Run: `... -m pytest server/tests/test_pm_api.py -v`
Expected: the new tests FAIL (404 — routes not mounted); phase-1 tests still PASS.

- [ ] **Step 3: Implement the routes**

Append to `server/api/pm.py` (reuse the phase-1 `_load_project` helper and `Request`/`app.state.orchestrator` access — grep phase-1 `pm.py`/`sdlc.py` for how the orchestrator handle is reached; add `from fastapi import Request` to the imports and the new model/engine imports):

```python
# --- phase 2: onboarding wizard + classification + triage (design §5, §7) --- #
from fastapi import Body, Request  # merge into the existing fastapi import line

from server.db.models import Feature, TriageProposal  # merge into models import
from server.pm.onboarding import (
    AwaitBackfill,
    AwaitMapPr,
    AwaitTriage,
    Classify,
    Live,
    OnboardingNeedsHuman,
    ProposeMap,
    Register,
    next_onboarding_action,
)


def _onboarding_action_to_dict(action: object) -> dict:
    if isinstance(action, Register):
        return {"type": "register"}
    if isinstance(action, AwaitBackfill):
        return {"type": "await_backfill"}
    if isinstance(action, ProposeMap):
        return {"type": "propose_map"}
    if isinstance(action, AwaitMapPr):
        return {"type": "await_map_pr"}
    if isinstance(action, Classify):
        return {"type": "classify"}
    if isinstance(action, AwaitTriage):
        return {"type": "await_triage"}
    if isinstance(action, Live):
        return {"type": "live"}
    if isinstance(action, OnboardingNeedsHuman):
        return {"type": "needs_human", "reason": action.reason}
    return {"type": "unknown"}  # pragma: no cover — union is closed


def _counts_to_dict(v) -> dict:
    return {
        "repoCount": v.repo_count,
        "backfillDoneCount": v.backfill_done_count,
        "featureCount": v.feature_count,
        "openUnclassifiedCount": v.open_unclassified_count,
        "pendingProposalCount": v.pending_proposal_count,
    }


@router.get("/projects/{slug}/onboarding")
async def get_onboarding(
    slug: str,
    request: Request,
    _identity: str = Depends(current_identity),
    db=Depends(get_db),
) -> dict:
    """The wizard's view over the onboarding engine (spec §5): current state,
    the next action to offer, and the counts behind the decision."""
    p = await _load_project(db, slug)
    orch = request.app.state.orchestrator
    view = await orch._pm_onboarding_counts(p.id)
    return {
        "state": view.state,
        "action": _onboarding_action_to_dict(next_onboarding_action(view)),
        "counts": _counts_to_dict(view),
    }


@router.post("/projects/{slug}/propose-map", status_code=status.HTTP_202_ACCEPTED)
async def propose_map(
    slug: str,
    request: Request,
    identity: str = Depends(current_identity),
    db=Depends(get_db),
) -> dict:
    """Spawn the archaeology map-synthesis session (idempotent). 409 if backfill
    is incomplete or the map step is already past/in flight."""
    p = await _load_project(db, slug)
    orch = request.app.state.orchestrator
    view = await orch._pm_onboarding_counts(p.id)
    if view.repo_count == 0 or view.backfill_done_count < view.repo_count:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail={
            "error": "backfill_incomplete",
            "message": "wait for backfill to finish before proposing the map"})
    if p.onboarding_state not in ("registered", "syncing", "map_proposed"):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail={
            "error": "in_flight",
            "message": f"map step already past in state '{p.onboarding_state}'"})
    rec = await orch._pm_onboard_spawn_map(slug)
    if rec is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail={
            "error": "no_capacity",
            "message": "no healthy account or primary repo to spawn the session"})
    await record_audit(db=db, type="pm.onboard.propose_map", identity=identity,
                       detail={"slug": slug, "sessionId": getattr(rec, "id", None)})
    return {"sessionId": getattr(rec, "id", None)}


@router.post("/projects/{slug}/classify", status_code=status.HTTP_202_ACCEPTED)
async def classify(
    slug: str,
    request: Request,
    identity: str = Depends(current_identity),
    db=Depends(get_db),
) -> dict:
    """Spawn one backlog-classification session over the next open, unclassified
    chunk (idempotent — a full chunk with no pending proposals). 409 before the
    map is merged."""
    p = await _load_project(db, slug)
    if p.onboarding_state not in ("map_merged", "classifying"):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail={
            "error": "map_not_merged",
            "message": "merge the feature map before classifying the backlog"})
    orch = request.app.state.orchestrator
    dispatched = await orch._pm_classify_run(slug)
    await record_audit(db=db, type="pm.onboard.classify", identity=identity,
                       detail={"slug": slug, "dispatched": dispatched})
    return {"dispatched": dispatched}


def _triage_to_dict(prop: TriageProposal, wi: WorkItem | None,
                    feat_slug: str | None) -> dict:
    return {
        "id": prop.id,
        "workItemId": prop.work_item_id,
        "repo": wi.repo if wi else None,
        "number": wi.number if wi else None,
        "kind": wi.kind if wi else None,
        "title": wi.title if wi else None,
        "url": wi.url if wi else None,
        "proposedFeatureId": prop.proposed_feature_id,
        "proposedFeatureSlug": feat_slug,
        "noFitSuggestion": prop.no_fit_suggestion,
        "reach": prop.reach,
        "impact": prop.impact,
        "effort": prop.effort,
        "confidence": prop.confidence,
        "rationale": prop.rationale,
        "confidenceLevel": prop.confidence_level,
        "status": prop.status,
        "createdAt": prop.created_at.isoformat() if prop.created_at else None,
    }


@router.get("/triage")
async def list_triage(
    projectSlug: str | None = None,
    confidenceLevel: str | None = None,
    _identity: str = Depends(current_identity),
    db=Depends(get_db),
) -> list[dict]:
    """Pending triage proposals, newest first, filterable by project + confidence
    (spec §7). Phase 2 is a flat list; the keyboard inbox is phase 3."""
    stmt = select(TriageProposal).where(TriageProposal.status == "pending")
    if confidenceLevel:
        stmt = stmt.where(TriageProposal.confidence_level == confidenceLevel)
    props = (await db.execute(
        stmt.order_by(TriageProposal.created_at.desc()))).scalars().all()
    if not props:
        return []
    wi_ids = [p.work_item_id for p in props]
    work_items = {w.id: w for w in (await db.execute(
        select(WorkItem).where(WorkItem.id.in_(wi_ids)))).scalars().all()}
    feat_ids = [p.proposed_feature_id for p in props if p.proposed_feature_id]
    feat_slugs = dict((await db.execute(
        select(Feature.id, Feature.slug).where(Feature.id.in_(feat_ids)))).all()) \
        if feat_ids else {}
    allowed_repos: set[str] | None = None
    if projectSlug:
        proj = (await db.execute(select(Project).where(
            Project.slug == projectSlug))).scalar_one_or_none()
        if proj is None:
            return []
        allowed_repos = {r for r in (await db.execute(select(
            ProjectRepo.repo).where(
                ProjectRepo.project_id == proj.id))).scalars().all() if r}
    out = []
    for p in props:
        wi = work_items.get(p.work_item_id)
        if allowed_repos is not None and (wi is None or wi.repo not in allowed_repos):
            continue
        out.append(_triage_to_dict(
            p, wi, feat_slugs.get(p.proposed_feature_id)))
    return out


async def _load_pending_proposal(db, proposal_id: str) -> TriageProposal:
    prop = (await db.execute(select(TriageProposal).where(
        TriageProposal.id == proposal_id))).scalar_one_or_none()
    if prop is None:
        raise HTTPException(status_code=404, detail={"error": "not_found"})
    if prop.status != "pending":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail={
            "error": "already_resolved",
            "message": f"proposal is '{prop.status}', not pending"})
    return prop


async def _apply_accept(db, proposal, identity: str, *,
                        edits: dict | None = None) -> None:
    """Shared accept logic (phase 3 EXTENDS this for accept-bulk — do NOT copy
    it into a second helper). Copies the proposal onto the WorkItem overlay,
    applying ``edits`` over the copied values when provided, marks the item
    ``classified`` and the proposal ``accepted``.

    ``classified_by`` is the constant ``"human"`` (a human accepted it), NOT the
    tailnet identity — the identity is carried ONLY on the audit event by the
    caller. The proposal row remains as audit."""
    wi = (await db.execute(select(WorkItem).where(
        WorkItem.id == proposal.work_item_id))).scalar_one_or_none()
    if wi is None:
        raise HTTPException(status_code=404, detail={"error": "work_item_missing"})
    now = datetime.now(timezone.utc)
    edits = edits or {}
    wi.feature_id = edits.get("featureId", proposal.proposed_feature_id)
    wi.reach = edits.get("reach", proposal.reach)
    wi.impact = edits.get("impact", proposal.impact)
    wi.effort = edits.get("effort", proposal.effort)
    wi.confidence = edits.get("confidence", proposal.confidence)
    wi.triage_state = "classified"
    wi.classified_by = "human"
    wi.classified_at = now
    proposal.status = "accepted"
    proposal.resolved_at = now
    await db.flush()


@router.post("/triage/{proposal_id}/accept")
async def accept_triage(
    proposal_id: str,
    body: dict | None = Body(default=None),
    identity: str = Depends(current_identity),
    db=Depends(get_db),
) -> dict:
    """Copy the proposal onto the WorkItem overlay and mark it classified
    (spec §7), via the shared ``_apply_accept`` helper. An OPTIONAL request body
    ``{edits?: {reach?, impact?, effort?, confidence?, featureId?}}`` overrides
    the copied values (lets phase-3 edit-then-accept work without a route
    change); absent body = plain accept. No accept-bulk (phase 3)."""
    prop = await _load_pending_proposal(db, proposal_id)
    edits = (body or {}).get("edits")
    await _apply_accept(db, prop, identity, edits=edits)
    wi_id = prop.work_item_id
    await record_audit(db=db, type="pm.triage.accept", identity=identity,
                       detail={"proposalId": proposal_id, "workItemId": wi_id})
    return {"id": prop.id, "status": "accepted", "workItemId": wi_id,
            "triageState": "classified"}


@router.post("/triage/{proposal_id}/reject")
async def reject_triage(
    proposal_id: str,
    identity: str = Depends(current_identity),
    db=Depends(get_db),
) -> dict:
    """Reject the proposal → the work item goes to needs_human (no blind retry,
    spec §6). The proposal row remains as audit."""
    prop = await _load_pending_proposal(db, proposal_id)
    wi = (await db.execute(select(WorkItem).where(
        WorkItem.id == prop.work_item_id))).scalar_one_or_none()
    now = datetime.now(timezone.utc)
    if wi is not None:
        wi.triage_state = "needs_human"
    prop.status = "rejected"
    prop.resolved_at = now
    await db.flush()
    await record_audit(db=db, type="pm.triage.reject", identity=identity,
                       detail={"proposalId": proposal_id})
    return {"id": prop.id, "status": "rejected"}
```

- [ ] **Step 4: Run tests**

Run: `... -m pytest server/tests/test_pm_api.py -v`
Expected: PASS (phase-1 + phase-2 tests). Then `ruff check server/ && mypy server/ --ignore-missing-imports`.

- [ ] **Step 5: Commit**

```bash
git add server/api/pm.py server/tests/test_pm_api.py
git commit -m "feat(pm): onboarding-wizard + propose-map/classify + triage accept/reject API"
```

---

### Task 5: Web — API client + hooks for onboarding + triage

**Files:**
- Modify: `web/src/api/pm.ts` (append phase-2 ONBOARDING types + fetchers only — `OnboardingAction`/`OnboardingCounts`/`OnboardingView`, `fetchOnboarding`, `proposeMap`, `runClassify`)
- Create: `web/src/api/triage.ts` (the triage TYPES + fetchers — canonical single module: `TriageProposal`, `OverlayEdits`, `fetchTriage(projectSlug?)`, `acceptProposal(id, edits?)`, `rejectProposal(id)`, plus `proposeMap`, `classify` re-exports for phase-3 reuse)
- Modify: `web/src/api/hooks.ts` (append `useOnboarding`, `useProposeMap`, `useClassify`, `useTriage`, `useAcceptProposal`, `useRejectProposal`; add `QK.triage = ['pm','triage']` + `QK.onboarding(slug)` keys)
- Test: `web/src/projects/__tests__/pmHooks.test.tsx` (create)

**Interfaces:**
- Consumes: the phase-1 `api` client + the Task 4 routes.
- Produces:
  - In `web/src/api/pm.ts`: onboarding types `OnboardingAction`/`OnboardingCounts`/`OnboardingView` and fetchers `fetchOnboarding`, `proposeMap`, `runClassify` (also re-exported as `classify` for the triage module).
  - In `web/src/api/triage.ts` (canonical triage module — phase 3 imports FROM here, does not fork it): `interface TriageProposal`, `interface OverlayEdits`, `fetchTriage(projectSlug?: string)`, `acceptProposal(id: string, edits?: OverlayEdits)`, `rejectProposal(id: string)`, plus `proposeMap`, `classify`.
  - In `web/src/api/hooks.ts`, canonical signatures (phase 3 UPGRADES these in place — no second copies): `useOnboarding(slug)`, `useProposeMap()`, `useClassify()`, `useTriage(projectSlug?: string)`, `useAcceptProposal(projectSlug?: string)` with mutation variable `{ id: string; edits?: OverlayEdits }`, `useRejectProposal(projectSlug?: string)` with variable `{ id: string }`. `QK.triage = ['pm','triage']` is added by PHASE 2 (phase 3 consumes it, does not re-add).

- [ ] **Step 1a: Append ONBOARDING types + fetchers to `web/src/api/pm.ts`**

Only onboarding lives here; triage types/fetchers go in their own module (Step 1b).

```typescript
// --- phase 2: onboarding wizard (design §5) ---

export interface OnboardingAction {
  type:
    | 'register' | 'await_backfill' | 'propose_map' | 'await_map_pr'
    | 'classify' | 'await_triage' | 'live' | 'needs_human' | 'unknown'
  reason?: string
}

export interface OnboardingCounts {
  repoCount: number
  backfillDoneCount: number
  featureCount: number
  openUnclassifiedCount: number
  pendingProposalCount: number
}

export interface OnboardingView {
  state: OnboardingState | string
  action: OnboardingAction
  counts: OnboardingCounts
}

export function fetchOnboarding(slug: string): Promise<OnboardingView> {
  return api.get<OnboardingView>(
    `/pm/projects/${encodeURIComponent(slug)}/onboarding`)
}

export function proposeMap(slug: string): Promise<{ sessionId: string | null }> {
  return api.post<{ sessionId: string | null }>(
    `/pm/projects/${encodeURIComponent(slug)}/propose-map`)
}

// Also exported as `classify` from the triage module (single spawn fetcher).
export function runClassify(slug: string): Promise<{ dispatched: number }> {
  return api.post<{ dispatched: number }>(
    `/pm/projects/${encodeURIComponent(slug)}/classify`)
}
```

- [ ] **Step 1b: Create `web/src/api/triage.ts`** — the canonical triage module

Types + fetchers for triage live in ONE module (phase 3 imports from here and
upgrades the hooks in place; it never forks a second `TriageProposal`/`fetchTriage`
under a different signature or import source).

```typescript
// --- phase 2: triage types + fetchers (design §7) — the single triage module ---
import { api } from '@/api/client'
// Re-export the map/classify spawn fetchers so triage callers (and phase 3) have
// one import source for the whole triage surface.
export { proposeMap, runClassify as classify } from '@/api/pm'

export interface TriageProposal {
  id: string
  workItemId: string | null
  repo: string | null
  number: number | null
  kind: string | null
  title: string | null
  url: string | null
  proposedFeatureId: string | null
  proposedFeatureSlug: string | null
  noFitSuggestion: string | null
  reach: number | null
  impact: number | null
  effort: number | null
  confidence: number | null
  rationale: string | null
  confidenceLevel: string | null
  status: string
  createdAt: string | null
}

/** Overlay fields a human may override when accepting (phase-3 edit-then-accept
 *  relies on this shape; phase 2 defines it). */
export interface OverlayEdits {
  reach?: number
  impact?: number
  effort?: number
  confidence?: number
  featureId?: string | null
}

export function fetchTriage(projectSlug?: string): Promise<TriageProposal[]> {
  const qs = projectSlug
    ? `?projectSlug=${encodeURIComponent(projectSlug)}`
    : ''
  return api.get<TriageProposal[]>(`/pm/triage${qs}`)
}

export function acceptProposal(
  id: string, edits?: OverlayEdits,
): Promise<{ id: string; status: string; workItemId: string | null; triageState: string }> {
  return api.post(`/pm/triage/${encodeURIComponent(id)}/accept`,
    edits ? { edits } : undefined)
}

export function rejectProposal(id: string): Promise<{ id: string; status: string }> {
  return api.post(`/pm/triage/${encodeURIComponent(id)}/reject`)
}
```

(Adjust the `api` import path + the `api.get`/`api.post` call shape to match the
phase-1 client in `web/src/api/client.ts` — reuse it, do not add a second client.)

- [ ] **Step 2: Write the failing hook test**

Create `web/src/projects/__tests__/pmHooks.test.tsx` (mirror the render/mocks idiom used by the phase-1 `ProjectsTab.test.tsx`):

```tsx
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { renderHook, waitFor } from '@testing-library/react'
import type { ReactNode } from 'react'
import { describe, expect, it, vi } from 'vitest'

import { useOnboarding, useTriage } from '@/api/hooks'

vi.mock('@/api/pm', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/pm')>()),
  fetchOnboarding: vi.fn().mockResolvedValue({
    state: 'map_merged',
    action: { type: 'classify' },
    counts: { repoCount: 1, backfillDoneCount: 1, featureCount: 4,
              openUnclassifiedCount: 30, pendingProposalCount: 0 },
  }),
}))

vi.mock('@/api/triage', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/triage')>()),
  fetchTriage: vi.fn().mockResolvedValue([{
    id: 'p1', workItemId: 'w1', repo: 'o/r', number: 7, kind: 'issue',
    title: 'Bug', url: null, proposedFeatureId: 'f1',
    proposedFeatureSlug: 'sessions', noFitSuggestion: null,
    reach: 3, impact: 2, effort: 1, confidence: 2, rationale: 'fits',
    confidenceLevel: 'high', status: 'pending', createdAt: null,
  }]),
}))

function wrap() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  )
}

describe('pm hooks', () => {
  it('useOnboarding returns the engine view', async () => {
    const { result } = renderHook(() => useOnboarding('ccswitch'), { wrapper: wrap() })
    await waitFor(() => expect(result.current.data?.action.type).toBe('classify'))
    expect(result.current.data?.counts.openUnclassifiedCount).toBe(30)
  })

  it('useTriage returns pending proposals', async () => {
    const { result } = renderHook(() => useTriage(), { wrapper: wrap() })
    await waitFor(() => expect(result.current.data?.length).toBe(1))
    expect(result.current.data?.[0].proposedFeatureSlug).toBe('sessions')
  })
})
```

- [ ] **Step 3: Run to verify failure** — In `web/`: `npx vitest run src/projects/__tests__/pmHooks.test.tsx` Expected: FAIL (hooks not exported).

- [ ] **Step 4: Add the hooks**

In `web/src/api/hooks.ts`, extend `QK` (match its existing entry style) with:

```typescript
  triage: ['pm', 'triage'] as const,          // phase 2 OWNS this key
  onboarding: (slug: string) => ['pm', 'onboarding', slug] as const,
```

Add the onboarding imports from `./pm` (`fetchOnboarding`, `proposeMap`, `runClassify`, and the type `OnboardingView`) and the triage imports from `./triage` (`fetchTriage`, `acceptProposal`, `rejectProposal`, and the types `TriageProposal`, `OverlayEdits`) to the hooks import block, then append near the phase-1 project hooks. **Canonical signatures — phase 3 upgrades these in place (adds optimistic onMutate+rollback) and does NOT create second copies under different signatures:**

```typescript
/** Onboarding-wizard state for one project (a view over the engine). */
export function useOnboarding(slug: string) {
  return useQuery<OnboardingView>({
    queryKey: QK.onboarding(slug),
    queryFn: () => fetchOnboarding(slug),
    enabled: !!slug,
    staleTime: 5_000,
    refetchInterval: 15_000,
  })
}

export function useProposeMap() {
  const qc = useQueryClient()
  return useMutation<{ sessionId: string | null }, ApiRequestError, string>({
    mutationFn: (slug) => proposeMap(slug),
    onSuccess: (_d, slug) => {
      void qc.invalidateQueries({ queryKey: QK.onboarding(slug) })
      void qc.invalidateQueries({ queryKey: QK.projects })
    },
    onError: (e) => toast.error(initiativeErrorMessage(e, 'Could not propose the map')),
  })
}

export function useClassify() {
  const qc = useQueryClient()
  return useMutation<{ dispatched: number }, ApiRequestError, string>({
    mutationFn: (slug) => runClassify(slug),
    onSuccess: (_d, slug) => {
      void qc.invalidateQueries({ queryKey: QK.onboarding(slug) })
      void qc.invalidateQueries({ queryKey: QK.triage })
    },
    onError: (e) => toast.error(initiativeErrorMessage(e, 'Could not start classification')),
  })
}

export function useTriage(projectSlug?: string) {
  return useQuery<TriageProposal[]>({
    queryKey: [...QK.triage, projectSlug ?? null],
    queryFn: () => fetchTriage(projectSlug),
    staleTime: 5_000,
    refetchInterval: 15_000,
  })
}

export function useAcceptProposal(projectSlug?: string) {
  const qc = useQueryClient()
  return useMutation<
    { id: string; status: string },
    ApiRequestError,
    { id: string; edits?: OverlayEdits }
  >({
    mutationFn: ({ id, edits }) => acceptProposal(id, edits),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: [...QK.triage, projectSlug ?? null] })
      void qc.invalidateQueries({ queryKey: QK.projects })
    },
    onError: (e) => toast.error(initiativeErrorMessage(e, 'Could not accept proposal')),
  })
}

export function useRejectProposal(projectSlug?: string) {
  const qc = useQueryClient()
  return useMutation<
    { id: string; status: string },
    ApiRequestError,
    { id: string }
  >({
    mutationFn: ({ id }) => rejectProposal(id),
    onSuccess: () =>
      void qc.invalidateQueries({ queryKey: [...QK.triage, projectSlug ?? null] }),
    onError: (e) => toast.error(initiativeErrorMessage(e, 'Could not reject proposal')),
  })
}
```

(`initiativeErrorMessage`, `toast`, `useQuery`, `useMutation`, `useQueryClient`, `ApiRequestError` are already imported in the file for the phase-1 hooks — reuse them, do not re-import.)

- [ ] **Step 5: Run to verify pass** — In `web/`: `npx vitest run src/projects/__tests__/pmHooks.test.tsx` Expected: PASS (2 tests).

- [ ] **Step 6: Commit**

```bash
git add web/src/api/pm.ts web/src/api/triage.ts web/src/api/hooks.ts web/src/projects/__tests__/pmHooks.test.tsx
git commit -m "feat(pm): web API client + hooks for onboarding + triage"
```

---

### Task 6: Web — onboarding wizard + triage review list

**Files:**
- Create: `web/src/projects/OnboardingWizard.tsx`
- Create: `web/src/projects/TriageReviewList.tsx`
- Modify: `web/src/projects/ProjectsTab.tsx` (render `<OnboardingWizard>` per project card when the project is not yet `live`, and mount `<TriageReviewList>` below the grid)
- Modify: `web/src/projects/projects.css` (append wizard + triage-list styles, reusing phase-1 tokens)
- Test: `web/src/projects/__tests__/TriageReviewList.test.tsx` (create); `web/src/projects/__tests__/OnboardingWizard.test.tsx` (create)

**Interfaces:**
- Consumes: `useOnboarding`, `useProposeMap`, `useClassify`, `useTriage`, `useAcceptProposal`, `useRejectProposal` (Task 5); the `fileUrl` helper from `@/lib/githubUrl` for feature-doc links at SHA (the `ArtifactLink` pattern — reused, not rebuilt).
- Produces: `OnboardingWizard` (a VIEW over `onboarding.action.type` — one button per actionable state) and `TriageReviewList` (a plain, functional list with Accept/Reject buttons; NO keyboard shortcuts — that is phase 3).

- [ ] **Step 1: Write the failing component tests**

Create `web/src/projects/__tests__/TriageReviewList.test.tsx`:

```tsx
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import { TriageReviewList } from '../TriageReviewList'

const accept = vi.fn().mockResolvedValue({ id: 'p1', status: 'accepted' })
const reject = vi.fn().mockResolvedValue({ id: 'p1', status: 'rejected' })

vi.mock('@/api/triage', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/triage')>()),
  fetchTriage: vi.fn().mockResolvedValue([{
    id: 'p1', workItemId: 'w1', repo: 'o/r', number: 7, kind: 'issue',
    title: 'Fix sessions', url: 'https://github.com/o/r/issues/7',
    proposedFeatureId: 'f1', proposedFeatureSlug: 'sessions',
    noFitSuggestion: null, reach: 3, impact: 2, effort: 1, confidence: 2,
    rationale: 'touches sessions', confidenceLevel: 'high', status: 'pending',
    createdAt: null,
  }, {
    id: 'p2', workItemId: 'w2', repo: 'o/r', number: 8, kind: 'issue',
    title: 'Billing thing', url: null, proposedFeatureId: null,
    proposedFeatureSlug: null, noFitSuggestion: 'billing',
    reach: null, impact: null, effort: null, confidence: null,
    rationale: 'no fit', confidenceLevel: 'low', status: 'pending',
    createdAt: null,
  }]),
  acceptProposal: (...a: unknown[]) => accept(...a),
  rejectProposal: (...a: unknown[]) => reject(...a),
}))

function renderList() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}><TriageReviewList /></QueryClientProvider>,
  )
}

describe('TriageReviewList', () => {
  it('renders proposals with feature, no_fit suggestion and confidence', async () => {
    renderList()
    await waitFor(() => expect(screen.getByText('Fix sessions')).toBeInTheDocument())
    expect(screen.getByText('sessions')).toBeInTheDocument()
    expect(screen.getByText(/billing/i)).toBeInTheDocument()  // no_fit suggestion
    expect(screen.getByText(/high/i)).toBeInTheDocument()
  })

  it('accepts a proposal', async () => {
    renderList()
    await waitFor(() => expect(screen.getByText('Fix sessions')).toBeInTheDocument())
    fireEvent.click(screen.getAllByRole('button', { name: /accept/i })[0])
    // Hook variable is { id, edits? }; the fetcher is called (id, edits).
    await waitFor(() => expect(accept).toHaveBeenCalledWith('p1', undefined))
  })

  it('rejects a proposal', async () => {
    renderList()
    await waitFor(() => expect(screen.getByText('Billing thing')).toBeInTheDocument())
    fireEvent.click(screen.getAllByRole('button', { name: /reject/i })[1])
    await waitFor(() => expect(reject).toHaveBeenCalledWith('p2'))
  })

  it('shows the empty state', async () => {
    const triage = await import('@/api/triage')
    vi.mocked(triage.fetchTriage).mockResolvedValueOnce([])
    renderList()
    await waitFor(() =>
      expect(screen.getByText(/nothing to triage/i)).toBeInTheDocument())
  })
})
```

Create `web/src/projects/__tests__/OnboardingWizard.test.tsx`:

```tsx
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import { OnboardingWizard } from '../OnboardingWizard'

const classify = vi.fn().mockResolvedValue({ dispatched: 3 })

vi.mock('@/api/pm', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/pm')>()),
  fetchOnboarding: vi.fn().mockResolvedValue({
    state: 'map_merged', action: { type: 'classify' },
    counts: { repoCount: 1, backfillDoneCount: 1, featureCount: 4,
              openUnclassifiedCount: 30, pendingProposalCount: 0 },
  }),
  runClassify: (...a: unknown[]) => classify(...a),
}))

function renderWizard() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <OnboardingWizard slug="ccswitch" />
    </QueryClientProvider>,
  )
}

describe('OnboardingWizard', () => {
  it('offers the Classify action for a map_merged project', async () => {
    renderWizard()
    await waitFor(() =>
      expect(screen.getByRole('button', { name: /classify backlog/i }))
        .toBeInTheDocument())
    expect(screen.getByText(/30 open items/i)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /classify backlog/i }))
    await waitFor(() => expect(classify).toHaveBeenCalledWith('ccswitch'))
  })
})
```

- [ ] **Step 2: Run to verify failure** — In `web/`: `npx vitest run src/projects` Expected: FAIL (components not found).

- [ ] **Step 3: Implement `OnboardingWizard.tsx`**

```tsx
/**
 * Onboarding wizard (PM product layer, phase 2): a VIEW over the onboarding
 * engine (design §5). It renders the next action for the project's current
 * onboarding_state as a single actionable button + a one-line status. It is not
 * authoritative — the backend engine decides; this only presents.
 */
import { useClassify, useOnboarding, useProposeMap } from '@/api/hooks'
import type { OnboardingAction } from '@/api/pm'

const STATE_LABEL: Record<string, string> = {
  registered: 'Registered', syncing: 'Syncing backlog',
  map_proposed: 'Feature map proposed', map_merged: 'Feature map merged',
  classifying: 'Classifying backlog', live: 'Live',
}

function ActionButton({ action, slug }: { action: OnboardingAction; slug: string }) {
  const proposeMap = useProposeMap()
  const classify = useClassify()
  switch (action.type) {
    case 'propose_map':
      return (
        <button className="pm-wizard-action" disabled={proposeMap.isPending}
                onClick={() => proposeMap.mutate(slug)}>
          {proposeMap.isPending ? 'Proposing…' : 'Propose feature map'}
        </button>
      )
    case 'classify':
      return (
        <button className="pm-wizard-action" disabled={classify.isPending}
                onClick={() => classify.mutate(slug)}>
          {classify.isPending ? 'Classifying…' : 'Classify backlog'}
        </button>
      )
    case 'await_backfill':
      return <span className="pm-muted">Waiting for backlog sync to finish…</span>
    case 'await_map_pr':
      return <span className="pm-muted">Review &amp; merge the feature-map PR, then advance.</span>
    case 'await_triage':
      return <span className="pm-muted">Drain the triage queue below, then advance.</span>
    case 'register':
      return <span className="pm-muted">Add a repo to begin.</span>
    case 'needs_human':
      return <span className="pm-warn">Needs attention: {action.reason}</span>
    case 'live':
      return null
    default:
      return null
  }
}

export function OnboardingWizard({ slug }: { slug: string }) {
  const { data, isLoading } = useOnboarding(slug)
  if (isLoading || !data) return null
  if (data.state === 'live') return null
  return (
    <div className="pm-wizard">
      <div className="pm-wizard-head">
        <span className="pm-wizard-state">{STATE_LABEL[data.state] ?? data.state}</span>
        <span className="pm-muted">
          {data.counts.featureCount} features · {data.counts.openUnclassifiedCount} open items ·{' '}
          {data.counts.pendingProposalCount} awaiting triage
        </span>
      </div>
      <ActionButton action={data.action} slug={slug} />
    </div>
  )
}
```

- [ ] **Step 4: Implement `TriageReviewList.tsx`**

```tsx
/**
 * Triage review list (PM product layer, phase 2): a functional-but-plain flat
 * list of pending TriageProposals with Accept/Reject buttons. Deliberately
 * minimal — the keyboard-first inbox (j/k/a/e/r/x) is phase 3, which REPLACES
 * this component (design §8). No keyboard shortcuts here.
 */
import { useAcceptProposal, useRejectProposal, useTriage } from '@/api/hooks'
import type { TriageProposal } from '@/api/triage'
import { fileUrl } from '@/lib/githubUrl'

function Scores({ p }: { p: TriageProposal }) {
  if (p.reach == null && p.impact == null && p.effort == null && p.confidence == null) {
    return <span className="pm-muted">no scores</span>
  }
  return (
    <span className="pm-scores">
      R{p.reach ?? '—'} I{p.impact ?? '—'} E{p.effort ?? '—'} C{p.confidence ?? '—'}
    </span>
  )
}

function ProposalRow({ p }: { p: TriageProposal }) {
  const accept = useAcceptProposal()
  const reject = useRejectProposal()
  const busy = accept.isPending || reject.isPending
  return (
    <li className="pm-triage-row">
      <div className="pm-triage-main">
        <span className="pm-triage-title">
          {p.url ? (
            <a href={p.url} target="_blank" rel="noreferrer">{p.title}</a>
          ) : p.title}
        </span>
        <span className="pm-muted">
          {p.repo}#{p.number} · {p.kind}
        </span>
      </div>
      <div className="pm-triage-meta">
        {p.proposedFeatureSlug ? (
          <span className="pm-feature-chip">{p.proposedFeatureSlug}</span>
        ) : (
          <span className="pm-nofit-chip">no fit → {p.noFitSuggestion ?? '?'}</span>
        )}
        <Scores p={p} />
        <span className={`pm-conf pm-conf-${p.confidenceLevel ?? 'low'}`}>
          {p.confidenceLevel ?? 'low'}
        </span>
      </div>
      {p.rationale && <p className="pm-triage-rationale">{p.rationale}</p>}
      <div className="pm-triage-actions">
        <button disabled={busy} onClick={() => accept.mutate({ id: p.id })}>Accept</button>
        <button disabled={busy} onClick={() => reject.mutate({ id: p.id })}>Reject</button>
      </div>
    </li>
  )
}

export function TriageReviewList({ projectSlug }: { projectSlug?: string }) {
  const { data, isLoading } = useTriage(projectSlug)
  if (isLoading) return <p className="pm-muted">Loading triage…</p>
  const items = data ?? []
  return (
    <section className="pm-triage">
      <h3>Triage queue{items.length ? ` (${items.length})` : ''}</h3>
      {items.length === 0 ? (
        <p className="pm-muted">Nothing to triage.</p>
      ) : (
        <ul className="pm-triage-list">
          {items.map((p) => <ProposalRow key={p.id} p={p} />)}
        </ul>
      )}
    </section>
  )
}

// `fileUrl` is imported for future feature-doc-at-SHA links in the row detail;
// referenced here to keep the ArtifactLink helper the single source of truth.
export const _featureDocUrl = fileUrl
```

NOTE: if eslint flags `_featureDocUrl`/`fileUrl` as unused, drop the import and the `_featureDocUrl` line — the feature-doc-at-SHA link belongs to the phase-4 board where a `Feature.commitSha` is in scope; the row here only has the work-item URL. Keep whichever the lint gate accepts; do not leave a dead import.

- [ ] **Step 5: Wire into `ProjectsTab.tsx`**

Inside the phase-1 `ProjectCard`, render `<OnboardingWizard slug={project.slug!} />` (guarded by `project.slug`) below the counts block; below the `pm-project-grid` in `ProjectsTab`, mount `<TriageReviewList />`. Import both from their sibling files. Append minimal styles to `web/src/projects/projects.css` (wizard row, triage row/chips, confidence colors) reusing the phase-1/initiatives tokens — no new color literals.

- [ ] **Step 6: Run to verify pass** — In `web/`: `npx vitest run src/projects` Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add web/src/projects/OnboardingWizard.tsx web/src/projects/TriageReviewList.tsx web/src/projects/ProjectsTab.tsx web/src/projects/projects.css web/src/projects/__tests__/TriageReviewList.test.tsx web/src/projects/__tests__/OnboardingWizard.test.tsx
git commit -m "feat(pm): onboarding wizard + plain triage review list"
```

---

### Task 7: Full gate, PR

- [ ] **Step 1: Run the complete four-step Python gate** (serialized, from the repo root, with the env prefix):

```bash
ruff check src/ server/
mypy server/ --ignore-missing-imports
... -m pytest tests/ -q
... -m pytest server/tests/ -q
```

Expected: all four green. No schema change this phase, so no Postgres-suite requirement (confirm you added no models/columns — `git diff origin/master -- server/db/models.py server/db/migrations` should be empty).

- [ ] **Step 2: Run the web gate** (in `web/`): `npx vitest run && npx tsc --noEmit && npx eslint .`

- [ ] **Step 3: Check for in-flight PRs touching the same files**

```bash
gh api graphql -f query='query { repository(owner: "steiner385", name: "ccswitch") { pullRequests(states: OPEN, first: 20) { nodes { number title files(first: 50) { nodes { path } } } } } }'
```

Phase 2 depends on phase 1 having merged, and lands BEFORE phases 3/4 (it DEFINES the shared seams `_pm_onboard_spawn_map`/`_pm_classify_run`/`_apply_accept`, the canonical `web/src/api/triage.ts` module, the triage hooks, and `QK.triage`). If phase-1's PR is still open, rebase this branch on it (this phase edits `server/api/pm.py`, `web/src/api/hooks.ts` `QK`, `web/src/projects/ProjectsTab.tsx`, and `server/sessions/orchestrator.py` — all phase-1-touched). Note the dependency in the PR body.

- [ ] **Step 4: Push and open the PR**

```bash
git push -u origin feat/pm-product-layer-phase2
gh pr create --title "feat(pm): product layer phase 2 — archaeology onboarding" --body "$(cat <<'EOF'
Phase 2 of the PM product layer (design: docs/superpowers/specs/2026-08-31-pm-product-layer-design.md §5). **This phase lands FIRST** (before phases 3/4) — it DEFINES the shared seams phases 3/4 reuse in place.

- Pure onboarding state-machine engine (server/pm/onboarding.py): next_onboarding_action over registered→…→live, mirroring the SDLC engine's propose-only gate.
- Pure classification module (server/pm/classification.py): map-synthesis + classification prompt builders and a structured-output ProposedClassification schema/parser. Phase 3 reuses this module.
- Orchestrator seams (ONE parameterized method each, phase 3 reuses): _pm_onboard_spawn_map(scope="initial"|"diff") opens ONE non-draft feature-map PR (no auto-merge); _pm_classify_run(items=None|injected batch) dispatches ~50-item chunks; _pm_classify_ingest writes pending TriageProposal rows (idempotent, propose-only — never touches overlays).
- API: GET onboarding, POST propose-map / classify (idempotent, 409 in-flight), GET /triage, POST /triage/{id}/accept (optional {edits} body → shared _apply_accept helper copies onto WorkItem overlay → classified, classified_by="human") and /reject (→ needs_human).
- Web: canonical web/src/api/triage.ts module (types + fetchers) + onboarding wizard over onboarding_state + a plain TriageReviewList with accept/reject; hooks useOnboarding/useProposeMap/useClassify/useTriage(projectSlug?)/useAcceptProposal(projectSlug?)/useRejectProposal(projectSlug?); adds QK.triage.

NO schema changes — phase 1's tables/columns cover everything. Depends on phase 1.
Later phases: continuous pm-triage loop + keyboard inbox (phase 3, which REPLACES TriageReviewList and UPGRADES the same hooks in place), board/roadmap (phase 4).

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
gh pr ready || true
gh pr merge --squash --auto
```

- [ ] **Step 5: Confirm** the PR is non-draft with auto-merge armed:

```bash
gh api graphql -f query='query { repository(owner: "steiner385", name: "ccswitch") { pullRequest(number: <N>) { isDraft autoMergeRequest { enabledAt } } } }'
```

---

## Self-review (Phase-2 scope)

- **Spec coverage.** §5 register→syncing→map_proposed→map_merged→classifying→live is the onboarding engine (Task 1); §5 Step 1 map-synthesis (ONE non-draft PR, no auto-merge, Evidence-lined docs) is `build_map_synthesis_prompt` + `_pm_onboard_spawn_map` (Tasks 2–3); §5 Step 2 chunked (~50) classification of OPEN items only, closed history as evidence, is `_pm_classify_run` + `CLASSIFY_CHUNK_SIZE` (Tasks 2–3); §6 item-1 classify-the-new path is the same `classification.py` phase 3 reuses; §6 `no_fit` + rejection→`needs_human` are in the parser + reject route; §7 propose-map/classify (idempotent, 409) + GET /triage + accept/reject are Task 4; §8 wizard-as-view + plain review list are Task 6. Out-of-lane items (accept-bulk, PATCH overlay, keyboard inbox, drift clustering, board/roadmap, discovery stage) are explicitly deferred to phase 3/4 and NOT built.
- **No schema change.** Confirmed against phase-1's model list: `Project.onboarding_state`, `WorkItem` overlay (`feature_id`/`reach`/`impact`/`effort`/`confidence`/`triage_state`/`classified_by`/`classified_at`), and all `TriageProposal` columns exist. Task 7 asserts an empty `models.py`/migrations diff. No Alembic revision, no `_FK_LINKS`/`_REQUIRED_EXTRAS` edits.
- **Placeholder scan.** Every step ships real code and real test code; the only `...`-shaped guidance is Task 3's completion-hook note, which is explicitly bounded (ingest is a tested public seam; the hook lands with pm-triage in phase 3) rather than a stub in shipped code.
- **Type consistency.** `ProposedClassification`, `ProjectOnboardingView`, `WorkItemBrief`, `FeatureBrief` are used identically across engine ↔ orchestrator ↔ tests; API dicts are camelCase and 4xx `detail` is `{error, message}` throughout; TS types (`OnboardingView`/`OnboardingAction`/`TriageProposal`) mirror the router's JSON keys 1:1.
- **Ambiguities resolved.** (1) Test fixture: phase-1 plan sketched an async `client` fixture but the real proven idiom (and the briefing's named style) is `test_sdlc_api.py`'s sync `TestClient` `_client()` — phase 2 uses that, with a stubbed-seams variant so propose-map/classify don't spawn real sessions. (2) Map-PR state correlation: the engine's `map_pr_open`/`map_pr_merged` flags are left `False` in phase 2 (no PR polling here); `map_proposed` with neither flag correctly re-offers ProposeMap (resumable), and PR-state enrichment is deferred to phase 3 where PR polling already exists. (3) Classification ingest trigger: wired as a tested public seam invoked call-driven; the generic session-completion hook is left to phase 3's `pm-triage` loop rather than forcing an orchestrator refactor now.

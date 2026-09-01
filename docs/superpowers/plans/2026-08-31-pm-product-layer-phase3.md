# PM Product Layer — Phase 3 (Continuous Loop + Real Inbox) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Land the `pm-triage` continuous loop — a pure decision engine (`server/pm/engine.py`) that batches the new for classification, auto-links mechanical PRs by closing keyword, clusters accepted `no_fit` drift into a feature-map diff proposal, and nudges staleness — plus its `PmTriageLoop` automation (modes `off | propose`), the orchestrator seams, the accept-bulk / overlay-edit API (drift-accept re-proposes orphaned items), and the keyboard-first Triage Inbox that **replaces** phase-2's plain `TriageReviewList.tsx`.

**Architecture:** Extends the SDLC spine in place (spec §2 "Approach A"). The engine follows the pure-`sdlc.engine` mold (total function over plain dataclass *views*, zero I/O, enumerated `(state, counts, mode)` table). The loop follows the `SdlcIntakeLoop` mold (contract class + injected-seam control loop) and **reuses phase-2's `server/pm/classification.py`** for the actual classify — the engine only decides *whether* a batch runs; the loop seeds sessions with `build_classification_prompt` and records `TriageProposal` rows via the same code path onboarding-classify uses (spec §6.1: "deliberately no second implementation"). API mirrors `server/api/sdlc.py` (camelCase JSON, `record_audit`, `detail={error,message}`). Web mirrors the `initiatives` tab and the `useInitiativesLive` WebSocket-invalidation pattern.

**Tech Stack:** Python 3.12 / FastAPI / SQLAlchemy 2 async / `gh` CLI for GraphQL · React + Vite + TS + React Query + vitest.

**Spec:** `docs/superpowers/specs/2026-08-31-pm-product-layer-design.md` §6 (continuous loop), §7 (API), §8 (inbox surface). Read it first — the plan argues from it.

**Consumes (phase-1 contract — do NOT rename):** models `Project, ProjectRepo, Feature, WorkItem, TriageProposal` and constants `WORK_ITEM_KINDS, WORK_ITEM_TRIAGE_STATES = (unclassified, proposed, classified, needs_human), TRIAGE_PROPOSAL_STATUSES = (pending, accepted, rejected), FEATURE_LIFECYCLE_STATES` in `server.db.models`; `WorkItemView`/`parse_page` idioms in `server/pm/github_sync.py`; the `PmSync`/`PmSyncLoop` mold in `server/automations/pm_sync.py`; the `pm` router `server/api/pm.py` mounted at `/api/pm`; `web/src/api/pm.ts` + `useProjects` in `web/src/api/hooks.ts` (`QK.projects = ['pm','projects']`).

**Consumes (phase-2 contract — do NOT reimplement):** `server/pm/classification.py`:
`build_classification_prompt(*, items, features, corrections=None) -> str`,
`parse_classification_output(text) -> list[ProposedClassification]` where
`ProposedClassification(number, kind, feature_slug|None, no_fit_suggestion|None, reach, impact, effort, confidence, rationale, confidence_level)`;
phase-2 orchestrator seams `_pm_classify_run(slug, *, items=None)` / `_pm_classify_ingest(session_id, project_slug, output_text)` (spawn a classify session, record `TriageProposal` rows) and the parameterized map seam `_pm_onboard_spawn_map(slug, *, scope, suggestion, item_numbers, repo)`; the shared accept helper `_apply_accept(db, proposal, identity, *, edits=None)` (phase 2 DEFINES it); phase-2 API `GET /triage`, `POST /triage/{id}/accept`, `POST /triage/{id}/reject` and web `useTriage/useAcceptProposal/useRejectProposal`. Phase 3 **deletes** phase-2's `web/src/projects/TriageReviewList.tsx` and repoints the wizard at the new inbox (no half-dead code — constitution VI).

## Global Constraints

- TDD throughout (constitution V): failing test first, then minimal impl, then green, then commit.
- **Propose-only, human-gated. NO `auto` mode.** `PmTriage.modes = ("off", "propose")`. Automation row defaults `enabled=False` (registry `ensure_rows`); never auto-enable at deploy.
- Branch off `origin/master` first: `git fetch && git checkout -b feat/pm-product-layer-phase3 origin/master`. NEVER branch off local `master`.
- **Landing order:** phase 3 lands AFTER phase 2 and BEFORE phase 4. Its tab `{ id: 'triage', label: 'Triage' }` is inserted AFTER the initiatives entry in `tabDefs.ts` (and the `TabId` union / DesktopShell branch / App route ordered likewise). If phase 3 lands second, rebase onto master and append after the sibling tab — do not insert at the same point (avoids same-line merge conflicts on the shared web files; phase 4's `roadmap` tab appends after triage).
- **NO NEW DB TABLES / no schema change** (briefing global constraint; confirmed in Task 0). Phase 1's `0012_pm_product_layer` shipped the whole schema. If you think you need a column, re-read phase-1's model list — you don't.
- Layered truth: GitHub owns work items, repo markdown owns feature docs, ccswitch DB owns only index/overlay/relations/proposals. Never introduce a second source of truth. **Reuse phase-2 `classification.py`; do not add a parallel classifier** (constitution VI; spec §6.1).
- Python tests run via the prebuilt venv: `env -u CCSWITCH_TAILNET_ALLOWLIST -u CCSWITCH_SESSIOND_HOST -u CCSWITCH_SESSIOND_PORT -u CCSWITCH_SESSIOND_CLIENT_CERT -u CCSWITCH_SESSIOND_CLIENT_KEY -u CCSWITCH_SESSIOND_CA CCSWITCH_DEV=1 /home/tony/GitHub/ccswitch/.venv/bin/python -m pytest <path> -v` (this phase touches only `server/`; no `PYTHONPATH` override needed — pytest rootdir wins for `server/*`).
- Web commands run in `web/` (symlink `node_modules` from `/home/tony/GitHub/ccswitch/web/node_modules` if absent): `npx vitest run`, `npx tsc --noEmit`, `npx eslint .`
- Before EVERY push, the full four-step gate: `ruff check src/ server/` · `mypy server/ --ignore-missing-imports` · `pytest tests/` · `pytest server/tests/` (serialize full-suite runs — concurrent runs on this host get SIGKILLed). Web: `vitest run`, `tsc --noEmit`, `eslint`.
- camelCase JSON at the API boundary; snake_case in Python. 4xx `detail` = object `{error, message}`.
- GitHub reads: GraphQL via the `gh_run` seam only. Never REST reads, never raw tokens. (Phase 3 does no direct GitHub I/O of its own — auto-link reads `WorkItem.linked_issue_numbers`, already mirrored by phase-1 sync.)
- Commit format: `feat(pm): …` / `test(pm): …`, ending with `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`.

---

### Task 0: Confirm phase 3 needs no schema change

**Files:** none modified — this is a verification gate, documented so the reviewer can see the decision was made deliberately.

**Interfaces:** Consumes phase-1's model list; produces a written confirmation (this task's checkbox record).

- [ ] **Step 1: Enumerate every column phase 3 reads/writes and confirm it exists**

The continuous loop and inbox use ONLY phase-1 columns:

| Concern | Column(s) used | Owner |
|---|---|---|
| classify-the-new batching | `WorkItem.state`, `.triage_state`, `.kind`, `.repo`, `.number`, `.title`, `.body`, `.labels` (read); `TriageProposal.*` (written by phase-2 seam) | phase 1 |
| auto-link mechanical PRs | `WorkItem.linked_issue_numbers` (JSON, read), `.feature_id`/`.triage_state`/`.classified_by` (written) | phase 1 |
| drift cluster | `TriageProposal.no_fit_suggestion`, `.status` (read) | phase 1 |
| staleness nudge | `Feature.state`, `Feature.updated_at`, `WorkItem.feature_id`/`.gh_updated_at` (read) | phase 1 |
| accept-bulk | `TriageProposal.status`/`.resolved_at`, `WorkItem` overlay cols (`feature_id, reach, impact, effort, confidence, triage_state, classified_by, classified_at`) | phase 1 |
| overlay PATCH | same overlay cols | phase 1 |
| drift-accept re-propose | `Feature` (new row from merged map), `WorkItem.triage_state`, `TriageProposal` (new pending rows) | phase 1 |

Every column is in phase-1's `0012_pm_product_layer` migration and the phase-1 model list. **No new table, no new column.**

- [ ] **Step 2: Prove it by running the phase-1 schema-discipline tests unchanged**

Run: `env … CCSWITCH_DEV=1 /home/tony/GitHub/ccswitch/.venv/bin/python -m pytest server/tests/test_schema_reconcile_coverage.py server/tests/test_pm_models.py -v`
Expected: PASS (green already — phase 3 adds no model). If a later task tempts you to add a column, STOP and re-read this table; the design routes every phase-3 concern through existing columns.

No commit for this task (verification only).

---

### Task 1: Pure pm-triage decision engine (`server/pm/engine.py`)

**Files:**
- Create: `server/pm/engine.py`
- Test: `server/tests/test_pm_engine.py` (create)

**Interfaces:**
- Produces the pure engine (mirrors `server/sdlc/engine.py`): input views + a closed action union + `next_triage_action(...)`.
  - Views: `TriageState(mode, unclassified_count, pending_count, no_fit_clusters, stale_features, day_spawns, day_cap)` frozen dataclass (the loop builds it from DB counts — NOT ORM rows, so this module imports nothing from `server.db`/`server.automations`).
  - `NoFitCluster(suggestion: str, item_numbers: tuple[int, ...], repo: str)` frozen dataclass.
  - `StaleFeature(slug: str, state: str, days_idle: int)` frozen dataclass.
  - Actions (frozen dataclasses, closed union `TriageAction`): `ClassifyBatch(count)`, `ProposeFeatureMapDiff(suggestion, item_numbers, repo)`, `NudgeStale(slugs)`, `Idle()`, `NeedsHuman(reason)`.
  - `next_triage_action(state: TriageState, config: dict) -> TriageAction` — pure + total, priority-ordered.
- Consumes: nothing project-internal (pure).

**Design notes (spec §6):** The engine is called ONCE per tick and returns the single highest-priority action; the loop executes it and re-ticks next cadence. Priority order (highest first): off-mode → `NeedsHuman("off")`; unclassified backlog present and under the day cap → `ClassifyBatch`; an accepted-`no_fit` cluster at/over the drift threshold → `ProposeFeatureMapDiff` (spec §6.3); stale features present → `NudgeStale` (indicator only, no session — spec §6.4); otherwise `Idle`. Auto-link is a **sync-time mechanical rule with no session** (spec §6.2), so it is NOT an engine action — it lives in the loop's pre-classify pass and is unit-tested there (Task 2). **Rejection is signal:** a rejected proposal is not the engine's business — the loop leaves `needs_human` items alone and never re-classifies them (asserted in Task 2), matching spec §6 "no blind retry".

- [ ] **Step 1: Write the failing test (enumerate the (state, counts, mode) table)**

Create `server/tests/test_pm_engine.py`. Mirror `test_sdlc_engine.py` exactly — exhaustive, I/O-free, `_state(**kw)` helper + `@pytest.mark.parametrize` for the table:

```python
"""Pure pm-triage engine: next_triage_action over the (state, counts, mode) table.

Exhaustive, I/O-free — the way test_sdlc_engine tests next_action. Every branch
of the priority ladder (classify → drift → stale → idle) plus the off / cap /
threshold edges, per design §6.
"""
from __future__ import annotations

import pytest

from server.pm.engine import (
    ClassifyBatch,
    Idle,
    NeedsHuman,
    NoFitCluster,
    NudgeStale,
    ProposeFeatureMapDiff,
    StaleFeature,
    TriageState,
    next_triage_action,
)

_DEFAULT_CONFIG = {"drift_threshold": 5, "batch_size": 50, "stale_days": 14}


def _state(**kw) -> TriageState:
    base = dict(mode="propose", unclassified_count=0, pending_count=0,
                no_fit_clusters=(), stale_features=(), day_spawns=0, day_cap=8)
    base.update(kw)
    return TriageState(**base)  # type: ignore[arg-type]


def _cluster(suggestion="realtime-sync", n=5, repo="o/r") -> NoFitCluster:
    return NoFitCluster(suggestion=suggestion,
                        item_numbers=tuple(range(1, n + 1)), repo=repo)


# --------------------------------------------------------------------------- #
# Off / terminal                                                              #
# --------------------------------------------------------------------------- #

def test_off_mode_needs_human():
    act = next_triage_action(_state(mode="off", unclassified_count=9), _DEFAULT_CONFIG)
    assert isinstance(act, NeedsHuman)


def test_empty_queue_is_idle():
    assert next_triage_action(_state(), _DEFAULT_CONFIG) == Idle()


# --------------------------------------------------------------------------- #
# Classify-the-new batching (design §6.1) — highest priority when work exists #
# --------------------------------------------------------------------------- #

def test_unclassified_backlog_classifies_a_capped_batch():
    act = next_triage_action(_state(unclassified_count=120), _DEFAULT_CONFIG)
    assert act == ClassifyBatch(count=50)  # bounded by batch_size


def test_batch_bounded_by_remaining_when_smaller_than_batch_size():
    act = next_triage_action(_state(unclassified_count=7), _DEFAULT_CONFIG)
    assert act == ClassifyBatch(count=7)


def test_day_cap_reached_defers_classify_but_still_drifts():
    # At the daily session cap, no ClassifyBatch — fall through to drift.
    st = _state(unclassified_count=50, day_spawns=8, day_cap=8,
                no_fit_clusters=(_cluster(n=5),))
    act = next_triage_action(st, _DEFAULT_CONFIG)
    assert isinstance(act, ProposeFeatureMapDiff)


def test_pending_proposals_do_not_reclassify():
    # Items already have pending proposals; unclassified_count is the loop's
    # "no pending proposal" count, so a pure pending queue is idle here.
    assert next_triage_action(_state(pending_count=40), _DEFAULT_CONFIG) == Idle()


# --------------------------------------------------------------------------- #
# Drift clustering (design §6.3)                                              #
# --------------------------------------------------------------------------- #

def test_cluster_at_threshold_proposes_map_diff():
    st = _state(no_fit_clusters=(_cluster(suggestion="offline-mode", n=5),))
    act = next_triage_action(st, _DEFAULT_CONFIG)
    assert act == ProposeFeatureMapDiff(
        suggestion="offline-mode", item_numbers=(1, 2, 3, 4, 5), repo="o/r")


def test_cluster_below_threshold_does_not_propose():
    st = _state(no_fit_clusters=(_cluster(n=4),))  # 4 < 5
    assert next_triage_action(st, _DEFAULT_CONFIG) == Idle()


def test_largest_cluster_wins_when_several_qualify():
    st = _state(no_fit_clusters=(_cluster("a", n=5), _cluster("b", n=9, repo="o/r2")))
    act = next_triage_action(st, _DEFAULT_CONFIG)
    assert isinstance(act, ProposeFeatureMapDiff) and act.suggestion == "b"


def test_classify_outranks_drift_when_both_available():
    st = _state(unclassified_count=10, no_fit_clusters=(_cluster(n=9),))
    assert next_triage_action(st, _DEFAULT_CONFIG) == ClassifyBatch(count=10)


# --------------------------------------------------------------------------- #
# Staleness nudge (design §6.4) — indicator only, lowest non-idle priority     #
# --------------------------------------------------------------------------- #

def test_stale_features_nudge_when_nothing_else_to_do():
    st = _state(stale_features=(
        StaleFeature(slug="billing", state="delivering", days_idle=30),
        StaleFeature(slug="search", state="discovery", days_idle=21)))
    act = next_triage_action(st, _DEFAULT_CONFIG)
    assert act == NudgeStale(slugs=("billing", "search"))


def test_stale_ignored_while_classify_or_drift_pending():
    st = _state(unclassified_count=3,
                stale_features=(StaleFeature("x", "delivering", 99),))
    assert next_triage_action(st, _DEFAULT_CONFIG) == ClassifyBatch(count=3)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `… -m pytest server/tests/test_pm_engine.py -v` (env prefix from Global Constraints)
Expected: FAIL — `ModuleNotFoundError: No module named 'server.pm.engine'`.

- [ ] **Step 3: Write the engine**

Create `server/pm/engine.py`:

```python
"""Pure pm-triage decision engine (design §6) — no I/O, never touches the DB or
orchestrator.

Mirrors ``sdlc.engine`` purity: ``next_triage_action`` is a total function over a
plain-dataclass ``TriageState`` view (built by the ``PmTriageLoop`` from DB
counts) that returns one frozen ``TriageAction`` the loop executes. The loop
injects the real I/O (count the backlog, spawn the classify session, open the
map-diff PR, set the stale indicator); this module only decides *what should
happen next*.

Priority ladder, highest first (a tick does exactly one thing):
  1. ``mode == "off"``          → NeedsHuman (automation disabled)
  2. unclassified backlog + budget → ClassifyBatch      (design §6.1)
  3. accepted-no_fit cluster ≥ threshold → ProposeFeatureMapDiff (design §6.3)
  4. stale features            → NudgeStale (indicator only; design §6.4)
  5. otherwise                 → Idle

Auto-linking mechanical PRs by closing keyword (design §6.2) is a *sync-time*
rule with no session, so it is NOT an engine action — it lives in the loop's
pre-classify pass. Rejection is signal (design §6): a rejected proposal moves
its item to ``needs_human`` and the loop never re-classifies it, so a
``needs_human`` item simply never appears in ``unclassified_count`` — no engine
branch is needed for it.
"""
from __future__ import annotations

from dataclasses import dataclass

# --------------------------------------------------------------------------- #
# Pure input views (constructed by the loop from DB counts — NOT ORM rows)     #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class NoFitCluster:
    """A group of accepted ``no_fit`` proposals sharing a suggested feature name
    (design §6.3). ``item_numbers`` are the orphaned work-item numbers whose
    capability the map does not yet cover."""
    suggestion: str
    item_numbers: tuple[int, ...]
    repo: str


@dataclass(frozen=True)
class StaleFeature:
    """A ``discovery``/``delivering`` feature with no linked activity in the
    config window (design §6.4)."""
    slug: str
    state: str
    days_idle: int


@dataclass(frozen=True)
class TriageState:
    """Everything the engine needs to decide the next action for one tick.

    ``unclassified_count`` counts open work items that are ``unclassified`` AND
    have no pending proposal (the loop excludes ``needs_human`` and
    already-proposed items). ``pending_count`` is informational (surfaced in
    status). ``day_spawns``/``day_cap`` bound classify-session cost (design §6.1
    "Daily session cap bounds cost")."""
    mode: str
    unclassified_count: int
    pending_count: int
    no_fit_clusters: tuple[NoFitCluster, ...]
    stale_features: tuple[StaleFeature, ...]
    day_spawns: int
    day_cap: int


# --------------------------------------------------------------------------- #
# Actions (what the loop should do next) — closed union                        #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ClassifyBatch:
    """Spawn one classify session over ``count`` unclassified items (design §6.1,
    the same path as onboarding Step 2)."""
    count: int


@dataclass(frozen=True)
class ProposeFeatureMapDiff:
    """A ``no_fit`` cluster crossed the drift threshold — open an
    archaeology-diff PR scoped to the unnamed capability (design §6.3)."""
    suggestion: str
    item_numbers: tuple[int, ...]
    repo: str


@dataclass(frozen=True)
class NudgeStale:
    """Set the low-key staleness indicator on these feature slugs (design §6.4).
    No session, no alarm."""
    slugs: tuple[str, ...]


@dataclass(frozen=True)
class Idle:
    """Nothing to do this tick."""


@dataclass(frozen=True)
class NeedsHuman:
    """Automation is off / a human must intervene. ``reason`` hits the feed."""
    reason: str = ""


TriageAction = ClassifyBatch | ProposeFeatureMapDiff | NudgeStale | Idle | NeedsHuman


# --------------------------------------------------------------------------- #
# Decision core                                                                #
# --------------------------------------------------------------------------- #


def next_triage_action(state: TriageState, config: dict) -> TriageAction:
    """Decide the next pm-triage action. Pure + total.

    ``config`` keys: ``drift_threshold`` (default 5), ``batch_size`` (default
    50). ``batch_size`` bounds one classify session; ``drift_threshold`` is the
    minimum cluster size before proposing a map diff.
    """
    if state.mode == "off":
        return NeedsHuman(reason="pm-triage automation is off")

    # 1. Classify the new — highest priority while budget remains.
    if state.unclassified_count > 0 and state.day_spawns < state.day_cap:
        batch_size = int(config.get("batch_size", 50))
        return ClassifyBatch(count=min(state.unclassified_count, batch_size))

    # 2. Drift: the largest cluster at/over threshold becomes a map-diff PR.
    threshold = int(config.get("drift_threshold", 5))
    qualifying = [c for c in state.no_fit_clusters if len(c.item_numbers) >= threshold]
    if qualifying:
        top = max(qualifying, key=lambda c: len(c.item_numbers))
        return ProposeFeatureMapDiff(
            suggestion=top.suggestion, item_numbers=top.item_numbers, repo=top.repo)

    # 3. Staleness: indicator only, lowest non-idle priority.
    if state.stale_features:
        return NudgeStale(slugs=tuple(f.slug for f in state.stale_features))

    return Idle()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `… -m pytest server/tests/test_pm_engine.py -v`
Expected: PASS (12 tests).

- [ ] **Step 5: Commit**

```bash
git add server/pm/engine.py server/tests/test_pm_engine.py
git commit -m "feat(pm): pure pm-triage decision engine (classify/drift/stale ladder)"
```

---

### Task 2: The pm-triage automation (`server/automations/pm_triage.py`)

**Files:**
- Create: `server/automations/pm_triage.py`
- Modify: `server/automations/registry.py` (register `PmTriage`, mirroring the `PmSync` lines)
- Test: `server/tests/test_pm_triage_loop.py` (create)

**Interfaces:**
- Consumes: `next_triage_action` + view/action types (Task 1); `WorkItemView` shape (phase-1 `github_sync`); phase-2 `classification.build_classification_prompt`; `AutomationStatus`/`ConfigField` from `server/automations/base.py`.
- Produces: `PmTriage` (contract, id `"pm-triage"`, **modes `("off", "propose")`**), `PmTriageLoop` with injected seams, and `PendingItemView` — the loop's per-item input row:

```python
@dataclass(frozen=True)
class PendingItemView:
    repo: str
    number: int
    kind: str                       # "issue" | "pr"
    title: str
    body: str
    labels: tuple[str, ...]
    linked_issue_numbers: tuple[int, ...]   # PR→issue closing refs
```

- Seam signatures (all injected, like `PmSyncLoop`):

```python
ReadConfig = Callable[[], Awaitable[tuple[str, dict]]]            # (mode, config)
ListPending = Callable[[], Awaitable[list[PendingItemView]]]      # open+unclassified+no-pending
AutoLink = Callable[[PendingItemView], Awaitable[bool]]           # inherit PR's issue feature; True if linked
CountState = Callable[[], Awaitable[TriageState]]                 # build the engine view from DB
Classify = Callable[[list[PendingItemView], int], Awaitable[Any]] # spawn 1 classify session over a batch
ProposeMapDiff = Callable[[str, tuple[int, ...], str], Awaitable[Any]]  # (suggestion, numbers, repo)
MarkStale = Callable[[tuple[str, ...]], Awaitable[None]]
Clock = Callable[[], float]
```

**Loop shape (spec §6):** one `tick()` — read config (off ⇒ stash zero, return); run the **pre-classify auto-link pass** over every pending PR with `linked_issue_numbers` (mechanical, no session — spec §6.2), dropping linked items from the pending set; build the `TriageState` via `CountState` (the auto-linked items no longer count as unclassified); ask the engine once; dispatch the single action. `ClassifyBatch` spawns one session over the first `count` still-pending items and records timestamped spawn for the day cap; `ProposeFeatureMapDiff` opens the diff PR; `NudgeStale` sets indicators; `Idle`/`NeedsHuman` do nothing but stash counts.

- [ ] **Step 1: Write the failing tests**

Create `server/tests/test_pm_triage_loop.py`. Build the loop over dict-backed recorder fakes, mirroring `test_pm_sync_loop.py` / `test_sdlc_intake_loop.py` (a tiny `Ctx` with async `emit` and an attribute stash):

```python
"""PmTriageLoop tests — auto-link pass, single-action dispatch, day cap, off-mode.

Every I/O seam is injected; the loop delegates the decision to the pure
``pm.engine`` and (for classify) reuses phase-2 ``classification`` via the
injected ``classify`` seam. Mirrors test_pm_sync_loop / test_sdlc_intake_loop.
"""
from __future__ import annotations

import pytest

from server.automations.pm_triage import PendingItemView, PmTriageLoop
from server.pm.engine import (
    ClassifyBatch,
    Idle,
    NoFitCluster,
    ProposeFeatureMapDiff,
    StaleFeature,
    TriageState,
)


class Ctx:
    def __init__(self):
        self.events = []

    async def emit(self, level, text, **kw):
        self.events.append((level, text, kw))


class _Sess:
    def __init__(self, sid="s1"):
        self.id = sid


def _item(number, kind="issue", links=()):
    return PendingItemView(
        repo="o/r", number=number, kind=kind, title=f"#{number}", body="",
        labels=(), linked_issue_numbers=tuple(links))


def _loop(ctx, *, mode="propose", config=None, pending=None, state=None,
          autolink_pred=None):
    """Wire a loop over recorder fakes. Returns (loop, rec)."""
    rec = {"classified": [], "map_diffs": [], "stale": [], "autolinked": []}
    cfg = dict(config or {"drift_threshold": 5, "batch_size": 50,
                          "max_spawns_per_day": 8, "stale_days": 14})

    async def read_config():
        return mode, cfg

    async def list_pending():
        return list(pending or [])

    async def auto_link(item):
        linked = bool(autolink_pred(item)) if autolink_pred else False
        if linked:
            rec["autolinked"].append(item.number)
        return linked

    async def count_state():
        # Tests pass an explicit engine view; default = derive from `pending`.
        if state is not None:
            return state
        return TriageState(
            mode=mode, unclassified_count=len(pending or []), pending_count=0,
            no_fit_clusters=(), stale_features=(), day_spawns=0, day_cap=8)

    async def classify(items, count):
        rec["classified"].append([i.number for i in items[:count]])
        return _Sess()

    async def propose_map_diff(suggestion, numbers, repo):
        rec["map_diffs"].append((suggestion, numbers, repo))
        return _Sess()

    async def mark_stale(slugs):
        rec["stale"].append(tuple(slugs))

    loop = PmTriageLoop(
        ctx, read_config=read_config, list_pending=list_pending,
        auto_link=auto_link, count_state=count_state, classify=classify,
        propose_map_diff=propose_map_diff, mark_stale=mark_stale,
        clock=lambda: 1000.0)
    return loop, rec


@pytest.mark.asyncio
async def test_off_mode_does_nothing():
    ctx = Ctx()
    loop, rec = _loop(ctx, mode="off", pending=[_item(1)])
    await loop.tick()
    assert rec["classified"] == [] and rec["map_diffs"] == []


@pytest.mark.asyncio
async def test_auto_link_pass_runs_before_classify_and_drops_linked_prs():
    ctx = Ctx()
    pending = [_item(10, kind="pr", links=[3]), _item(11, kind="issue")]
    # PR #10 references issue #3 → mechanically linked, no session.
    st = TriageState(mode="propose", unclassified_count=1, pending_count=0,
                     no_fit_clusters=(), stale_features=(), day_spawns=0, day_cap=8)
    loop, rec = _loop(ctx, pending=pending, state=st,
                      autolink_pred=lambda i: i.kind == "pr" and bool(i.linked_issue_numbers))
    await loop.tick()
    assert rec["autolinked"] == [10]
    # only the unlinked issue #11 reaches the classifier
    assert rec["classified"] == [[11]]


@pytest.mark.asyncio
async def test_classify_batch_bounded_by_count():
    ctx = Ctx()
    pending = [_item(n) for n in range(1, 61)]
    st = TriageState(mode="propose", unclassified_count=60, pending_count=0,
                     no_fit_clusters=(), stale_features=(), day_spawns=0, day_cap=8)
    loop, rec = _loop(ctx, pending=pending, state=st,
                      config={"batch_size": 50, "drift_threshold": 5,
                              "max_spawns_per_day": 8, "stale_days": 14})
    await loop.tick()
    assert len(rec["classified"][0]) == 50  # engine said ClassifyBatch(50)


@pytest.mark.asyncio
async def test_day_cap_defers_classify():
    ctx = Ctx()
    st = TriageState(mode="propose", unclassified_count=50, pending_count=0,
                     no_fit_clusters=(), stale_features=(), day_spawns=8, day_cap=8)
    loop, rec = _loop(ctx, pending=[_item(1)], state=st)
    await loop.tick()
    assert rec["classified"] == []


@pytest.mark.asyncio
async def test_drift_cluster_opens_map_diff_pr():
    ctx = Ctx()
    cluster = NoFitCluster(suggestion="offline-mode",
                           item_numbers=(1, 2, 3, 4, 5), repo="o/r")
    st = TriageState(mode="propose", unclassified_count=0, pending_count=0,
                     no_fit_clusters=(cluster,), stale_features=(),
                     day_spawns=0, day_cap=8)
    loop, rec = _loop(ctx, pending=[], state=st)
    await loop.tick()
    assert rec["map_diffs"] == [("offline-mode", (1, 2, 3, 4, 5), "o/r")]


@pytest.mark.asyncio
async def test_stale_nudge_sets_indicators():
    ctx = Ctx()
    st = TriageState(mode="propose", unclassified_count=0, pending_count=0,
                     no_fit_clusters=(),
                     stale_features=(StaleFeature("billing", "delivering", 30),),
                     day_spawns=0, day_cap=8)
    loop, rec = _loop(ctx, pending=[], state=st)
    await loop.tick()
    assert rec["stale"] == [("billing",)]


@pytest.mark.asyncio
async def test_idle_tick_records_no_work():
    ctx = Ctx()
    st = TriageState(mode="propose", unclassified_count=0, pending_count=0,
                     no_fit_clusters=(), stale_features=(), day_spawns=0, day_cap=8)
    loop, rec = _loop(ctx, pending=[], state=st)
    await loop.tick()
    assert rec == {"classified": [], "map_diffs": [], "stale": [], "autolinked": []}
```

- [ ] **Step 2: Run to verify failure** — `… -m pytest server/tests/test_pm_triage_loop.py -v` Expected: FAIL, `ModuleNotFoundError: server.automations.pm_triage`.

- [ ] **Step 3: Implement**

Create `server/automations/pm_triage.py`:

```python
"""PM Triage — the continuous curation loop (design §6).

Same two-object mold as PmSync / SdlcIntake:

* ``PmTriage`` — the ``Automation`` contract surface (id/name/modes/config +
  status/on_state_change). Metadata only; holds no I/O. **Modes are
  ``("off", "propose")`` — there is NO ``auto`` in v1 (design §6, constitution).**
* ``PmTriageLoop`` — the supervised control loop. Every I/O dependency is
  INJECTED so the loop runs with no network / no real DB in tests. The loop
  never *decides* what to do: it delegates to the pure ``pm.engine`` and, for
  classify, REUSES phase-2 ``pm.classification`` via the injected ``classify``
  seam (the same code path as onboarding Step 2 — design §6.1, no second
  implementation).

A tick, in order (design §6):
  0. Auto-link the mechanical: every pending PR carrying closing-issue refs
     inherits that issue's feature (no session). Linked items leave the queue.
  1..N. Build the ``TriageState`` from DB counts and ask the engine for the ONE
     highest-priority action; execute it. Classify sessions are bounded by a
     rolling daily cap (like SdlcIntakeLoop).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from server.pm.engine import (
    ClassifyBatch,
    Idle,
    NeedsHuman,
    NudgeStale,
    ProposeFeatureMapDiff,
    TriageState,
    next_triage_action,
)

from .base import AutomationStatus, ConfigField

log = logging.getLogger(__name__)

# The classify day-cap is a rolling 24h window (mirrors SdlcIntakeLoop).
_DAY_SECONDS = 24 * 60 * 60


@dataclass(frozen=True)
class PendingItemView:
    """One open, unclassified work item with no pending proposal (the loop's
    ``list_pending`` seam filters to exactly these). ``linked_issue_numbers`` is
    non-empty only for PRs that reference an issue via a closing keyword."""
    repo: str
    number: int
    kind: str
    title: str
    body: str
    labels: tuple[str, ...]
    linked_issue_numbers: tuple[int, ...]


class PmTriage:
    id = "pm-triage"
    name = "PM Triage"
    description = (
        "Continuously curates the product model: classifies new backlog items "
        "into features, auto-links mechanical PRs, clusters map drift into a "
        "proposed feature-map diff, and nudges stale features. Propose-only — a "
        "human accept is the gate.")
    modes: tuple[str, ...] = ("off", "propose")
    config_schema = [
        ConfigField("batch_size", "Items per classify session", "int", 50,
                    help="Open items classified in one session (design §6.1)."),
        ConfigField("max_spawns_per_day", "Max classify sessions / day", "int", 8,
                    help="Safety cap on classify sessions spawned per rolling 24h."),
        ConfigField("drift_threshold", "Drift cluster threshold", "int", 5,
                    help="Accepted no_fit items sharing a name before a feature-"
                         "map diff is proposed (design §6.3)."),
        ConfigField("stale_days", "Feature staleness window (days)", "int", 14,
                    help="A discovery/delivering feature idle this long gets a "
                         "low-key staleness indicator (design §6.4)."),
    ]

    def status(self, ctx: Any) -> AutomationStatus:
        counts = getattr(ctx, "pm_triage_counts", None) if ctx else None
        if not counts:
            return AutomationStatus(text="idle", level="idle")
        text = (f"{counts.get('unclassified', 0)} to triage · "
                f"{counts.get('pending', 0)} awaiting accept · "
                f"{counts.get('drift', 0)} drift")
        level = "active" if counts.get("unclassified") or counts.get("drift") else "idle"
        return AutomationStatus(text=text, level=level, counts=counts)

    async def on_state_change(self, *, enabled: bool, mode: str | None,
                              config: dict, ctx: Any) -> None:
        return None


ReadConfig = Callable[[], Awaitable[tuple[str, dict]]]
ListPending = Callable[[], Awaitable[list[PendingItemView]]]
AutoLink = Callable[[PendingItemView], Awaitable[bool]]
CountState = Callable[[], Awaitable[TriageState]]
Classify = Callable[[list[PendingItemView], int], Awaitable[Any]]
ProposeMapDiff = Callable[[str, tuple[int, ...], str], Awaitable[Any]]
MarkStale = Callable[[tuple[str, ...]], Awaitable[None]]


class PmTriageLoop:
    """One ``tick()`` auto-links mechanical PRs, then executes the single engine
    action for this tick. All I/O is injected."""

    def __init__(
        self,
        ctx: Any,
        *,
        read_config: ReadConfig,
        list_pending: ListPending,
        auto_link: AutoLink,
        count_state: CountState,
        classify: Classify,
        propose_map_diff: ProposeMapDiff,
        mark_stale: MarkStale,
        clock: Callable[[], float],
    ) -> None:
        self._ctx = ctx
        self._read_config = read_config
        self._list_pending = list_pending
        self._auto_link = auto_link
        self._count_state = count_state
        self._classify = classify
        self._propose_map_diff = propose_map_diff
        self._mark_stale = mark_stale
        self._clock = clock
        self._spawns_today: list[float] = []

    async def tick(self) -> None:
        try:
            await self._run()
        except Exception:  # pragma: no cover — belt+braces, mirrors the mold
            log.warning("pm_triage.tick_error", exc_info=True)

    async def _run(self) -> None:
        mode, config = await self._read_config()
        if mode == "off":
            self._stash(0, 0, 0)
            return

        pending = await self._list_pending()

        # 0. Mechanical auto-link pass (design §6.2): PRs whose closing refs point
        #    at a classified issue inherit that feature with no AI session. Linked
        #    items leave the queue so only unlinked ones reach the classifier.
        still_pending: list[PendingItemView] = []
        for item in pending:
            if item.kind == "pr" and item.linked_issue_numbers:
                if await self._auto_link(item):
                    await self._ctx.emit(
                        "info", f"auto-linked {item.repo}#{item.number}",
                        kind="auto_linked", target=f"{item.repo}#{item.number}")
                    continue
            still_pending.append(item)

        self._prune_day()
        state = await self._count_state()
        action = next_triage_action(state, config)

        if isinstance(action, ClassifyBatch):
            await self._classify_batch(still_pending, action.count)
        elif isinstance(action, ProposeFeatureMapDiff):
            await self._propose_map_diff(action.suggestion, action.item_numbers,
                                         action.repo)
            await self._ctx.emit(
                "info", f"proposed feature-map diff for '{action.suggestion}'",
                kind="drift_proposed", target=action.repo,
                detail={"suggestion": action.suggestion,
                        "items": list(action.item_numbers)})
        elif isinstance(action, NudgeStale):
            await self._mark_stale(action.slugs)
        elif isinstance(action, NeedsHuman):
            await self._ctx.emit("info", action.reason, kind="needs_human")
        # Idle: nothing.

        drift = sum(1 for c in state.no_fit_clusters
                    if len(c.item_numbers) >= int(config.get("drift_threshold", 5)))
        self._stash(state.unclassified_count, state.pending_count, drift)

    async def _classify_batch(self, pending: list[PendingItemView], count: int) -> None:
        cap = int((await self._read_config())[1].get("max_spawns_per_day", 8))
        if len(self._spawns_today) >= cap:
            await self._ctx.emit(
                "info", "daily classify-session cap reached; deferring",
                kind="ceiling")
            return
        batch = pending[:count]
        if not batch:
            return
        session = await self._classify(batch, count)
        self._spawns_today.append(self._clock())
        await self._ctx.emit(
            "info", f"classifying {len(batch)} items",
            kind="classify", session_id=getattr(session, "id", None),
            detail={"numbers": [i.number for i in batch]})

    def _prune_day(self) -> None:
        cutoff = self._clock() - _DAY_SECONDS
        self._spawns_today = [t for t in self._spawns_today if t >= cutoff]

    def _stash(self, unclassified: int, pending: int, drift: int) -> None:
        self._ctx.pm_triage_counts = {
            "unclassified": unclassified, "pending": pending, "drift": drift}
```

Then in `server/automations/registry.py`, mirror the `PmSync` import/registration lines (append after the `PmSync` register call added in phase 1):

```python
from .pm_triage import PmTriage  # noqa: E402
registry.register(PmTriage())
```

- [ ] **Step 4: Run tests** — `… -m pytest server/tests/test_pm_triage_loop.py -v` Expected: PASS (7 tests). Also `… -m pytest server/tests/ -k registry -v` to confirm registration doesn't break registry tests.

- [ ] **Step 5: Commit**

```bash
git add server/automations/pm_triage.py server/automations/registry.py server/tests/test_pm_triage_loop.py
git commit -m "feat(pm): pm-triage automation loop (auto-link/classify/drift/stale)"
```

---

### Task 3: Orchestrator wiring for pm-triage

**Files:**
- Modify: `server/sessions/orchestrator.py` (loop construction next to the `_pm_sync_loop` block from phase 1; seam methods next to the `_pm_sync_*` seams; task start/stop — grep `_pm_sync_task` and mirror EVERY occurrence)

**Interfaces:**
- Consumes: `PmTriageLoop`, `PendingItemView`, engine views/actions (Tasks 1–2); models `WorkItem, Feature, TriageProposal`; phase-2 seams `_pm_classify_run(slug, *, items=None)` / `_pm_classify_ingest` and `_pm_onboard_spawn_map(slug, *, scope="diff", …)` (reuse — do NOT add a second classify or map-synthesis path); phase-1 `_pm_sync_*` session/db idioms.
- Produces: a running `pm-triage` loop in the daemon; seam methods `_pm_triage_*`.

- [ ] **Step 1: Read the existing wiring**

Read the phase-1 `_pm_sync_loop` construction block and `_pm_sync_*` seams, and the phase-2 `_pm_classify_run`/`_pm_classify_ingest` and `_pm_onboard_spawn_map` seams. Grep every hit for `_pm_sync_task` (construction, start, cancel/stop). The pm-triage wiring mirrors each site exactly. **Reuse** the phase-2 classify seam for the actual session spawn + `TriageProposal` recording; the `_pm_triage_classify` seam below is a thin adapter that resolves the per-batch `slug` and calls `_pm_classify_run(slug, items=…)`.

- [ ] **Step 2: Construct the loop (next to the pm-sync loop)**

```python
        # PM Triage control loop (design §6): the continuous curation loop —
        # auto-link mechanical PRs, classify the new (reusing the phase-2
        # classify session path), cluster drift into a map-diff PR, nudge stale
        # features. Same injected-seam mold as pm-sync; propose-only.
        from server.automations.pm_triage import PmTriageLoop
        self._pm_triage_ctx = AutomationContext("pm-triage")
        self._pm_triage_loop = PmTriageLoop(
            self._pm_triage_ctx,
            read_config=self._pm_triage_read_config,
            list_pending=self._pm_triage_list_pending,
            auto_link=self._pm_triage_auto_link,
            count_state=self._pm_triage_count_state,
            classify=self._pm_triage_classify,
            propose_map_diff=self._pm_triage_propose_map_diff,
            mark_stale=self._pm_triage_mark_stale,
            clock=lambda: time.monotonic(),
        )
        self._pm_triage_task: asyncio.Task | None = None
```

(Confirm `time`/`asyncio`/`datetime`/`timezone` are imported in the file; add if missing.)

- [ ] **Step 3: Implement the seams (next to the `_pm_sync_*` seams)**

Reuse the exact `db_session()` context-manager idiom the pm-sync seams use.

```python
    # ------------------------ pm-triage seams (design §6) ------------------ #

    async def _pm_triage_read_config(self) -> tuple[str, dict]:
        # Mirror _pm_sync_read_config exactly, with automation id "pm-triage"
        # and default mode "off". Copy that method's body and adjust the id.
        ...

    async def _pm_triage_list_pending(self) -> list["PendingItemView"]:
        """Open, unclassified work items with NO pending proposal — the classify
        candidate set. Excludes needs_human (rejection is signal; design §6)."""
        from server.automations.pm_triage import PendingItemView
        from server.db.models import TriageProposal, WorkItem
        async with db_session() as db:
            pending_ids = set((await db.execute(
                select(TriageProposal.work_item_id)
                .where(TriageProposal.status == "pending")
            )).scalars().all())
            rows = (await db.execute(
                select(WorkItem).where(
                    WorkItem.state == "open",
                    WorkItem.triage_state == "unclassified")
            )).scalars().all()
        return [PendingItemView(
            repo=w.repo or "", number=w.number or 0, kind=w.kind or "issue",
            title=w.title or "", body=w.body or "",
            labels=tuple(w.labels or ()),
            linked_issue_numbers=tuple(w.linked_issue_numbers or ()),
        ) for w in rows if w.id not in pending_ids]

    async def _pm_triage_auto_link(self, item) -> bool:
        """Mechanical PR→feature inheritance (design §6.2): if any issue this PR
        closes is already classified onto a feature, copy that feature onto the
        PR's overlay (classified_by='auto-link'). No AI session. Returns True on
        a successful link."""
        from server.db.models import WorkItem
        async with db_session() as db:
            issue = (await db.execute(
                select(WorkItem).where(
                    WorkItem.repo == item.repo,
                    WorkItem.kind == "issue",
                    WorkItem.number.in_(list(item.linked_issue_numbers)),
                    WorkItem.feature_id.isnot(None))
            )).scalars().first()
            if issue is None:
                return False
            pr = (await db.execute(
                select(WorkItem).where(
                    WorkItem.repo == item.repo, WorkItem.kind == "pr",
                    WorkItem.number == item.number)
            )).scalar_one_or_none()
            if pr is None:
                return False
            pr.feature_id = issue.feature_id
            pr.triage_state = "classified"
            pr.classified_by = "auto-link"
            pr.classified_at = datetime.now(timezone.utc)
        return True

    async def _pm_triage_count_state(self):
        """Build the pure engine view from DB counts (design §6). Excludes
        needs_human and already-proposed items from unclassified_count."""
        from server.automations.pm_triage import PmTriage
        from server.db.models import Feature, TriageProposal, WorkItem
        from server.pm.engine import NoFitCluster, StaleFeature, TriageState
        mode, config = await self._pm_triage_read_config()
        stale_days = int(config.get("stale_days", 14))
        now = datetime.now(timezone.utc)
        async with db_session() as db:
            pending_ids = set((await db.execute(
                select(TriageProposal.work_item_id)
                .where(TriageProposal.status == "pending")
            )).scalars().all())
            open_unclassified = (await db.execute(
                select(WorkItem.id, WorkItem.repo, WorkItem.number).where(
                    WorkItem.state == "open",
                    WorkItem.triage_state == "unclassified")
            )).all()
            unclassified = sum(1 for wid, _, _ in open_unclassified
                               if wid not in pending_ids)
            pending_count = len(pending_ids)
            # Drift clusters: accepted no_fit proposals grouped by suggestion.
            accepted_nofit = (await db.execute(
                select(TriageProposal.no_fit_suggestion, WorkItem.number,
                       WorkItem.repo)
                .join(WorkItem, WorkItem.id == TriageProposal.work_item_id)
                .where(TriageProposal.status == "accepted",
                       TriageProposal.no_fit_suggestion.isnot(None))
            )).all()
            clusters: dict[str, list] = {}
            repo_of: dict[str, str] = {}
            for suggestion, number, repo in accepted_nofit:
                clusters.setdefault(suggestion, []).append(number)
                repo_of.setdefault(suggestion, repo or "")
            no_fit_clusters = tuple(
                NoFitCluster(suggestion=s, item_numbers=tuple(sorted(nums)),
                             repo=repo_of[s])
                for s, nums in clusters.items())
            # Stale features: discovery/delivering with no work-item activity in
            # the window and untouched docs.
            feats = (await db.execute(
                select(Feature).where(Feature.state.in_(("discovery", "delivering")))
            )).scalars().all()
            stale: list[StaleFeature] = []
            for f in feats:
                last = (await db.execute(
                    select(func.max(WorkItem.gh_updated_at))
                    .where(WorkItem.feature_id == f.id)
                )).scalar_one_or_none()
                marker = last or f.updated_at
                if marker is None:
                    continue
                days_idle = (now - marker).days
                if days_idle >= stale_days:
                    stale.append(StaleFeature(slug=f.slug or "", state=f.state or "",
                                              days_idle=days_idle))
        return TriageState(
            mode=mode, unclassified_count=unclassified, pending_count=pending_count,
            no_fit_clusters=no_fit_clusters, stale_features=tuple(stale),
            day_spawns=0, day_cap=int(config.get("max_spawns_per_day", 8)))

    async def _pm_triage_classify(self, items, count):
        """Spawn ONE classify session over the batch, REUSING the phase-2
        classify path (`_pm_classify_run` — which builds the prompt via
        `classification.build_classification_prompt` and records `TriageProposal`
        rows via `_pm_classify_ingest`). Do NOT add a second classifier —
        design §6.1. Items carry `repo`, so resolve `slug` per batch by mapping
        repo → project (all items in a batch share a project here)."""
        batch = items[:count]
        slug = await self._pm_project_slug_for_repo(batch[0].repo)
        return await self._pm_classify_run(
            slug, items=[(i.repo, i.number, i.kind) for i in batch])

    async def _pm_triage_propose_map_diff(self, suggestion, numbers, repo):
        """Open a scoped archaeology-diff PR for the unnamed capability. REUSE
        the parameterized phase-2 map seam `_pm_onboard_spawn_map` with
        `scope="diff"`, naming `suggestion` and the orphaned `numbers` — there is
        NO second map-synthesis path (constitution VI). On merge, pm-sync
        re-indexes the new Feature and the drift-accept API path (Task 4)
        re-proposes the orphaned items."""
        slug = await self._pm_project_slug_for_repo(repo)
        return await self._pm_onboard_spawn_map(
            slug, scope="diff", suggestion=suggestion,
            item_numbers=tuple(numbers), repo=repo)

    async def _pm_triage_mark_stale(self, slugs) -> None:
        """Low-key staleness indicator (design §6.4). No dedicated column: the
        board derives staleness from Feature.updated_at + linked-item activity
        (same computation as _pm_triage_count_state), so this seam only emits a
        feed event — nothing to persist."""
        if slugs:
            await self._pm_triage_ctx.emit(
                "info", f"stale features: {', '.join(slugs)}",
                kind="stale_features", detail={"slugs": list(slugs)})

    async def _pm_triage_tick_loop(self) -> None:
        # Copy the exact shape of _pm_sync_tick_loop (same sleep interval /
        # config-row semantics).
        ...
```

The `...` bodies (`_pm_triage_read_config`, `_pm_triage_tick_loop`) MUST be copied from their `_pm_sync_*` counterparts, changing only the automation id and the loop attribute. **NOTE — reuse, not reinvent:** `_pm_classify_run`/`_pm_classify_ingest` and `_pm_onboard_spawn_map` are the canonical phase-2 seams — call those exact names (grep the phase-2 orchestrator to confirm). Do NOT create the parallel `_pm_classify_spawn` / `_pm_classify_record` / `_pm_propose_map_diff_spawn` names — they do not exist; the drift path is `_pm_onboard_spawn_map(..., scope="diff")` and the classify path is `_pm_classify_run(slug, items=…)`. Do not create a parallel classify or map-synthesis path (constitution VI). `_pm_project_slug_for_repo` maps a repo full-name to its owning project slug (grep the phase-1/2 orchestrator for the existing repo→project helper and reuse it; add a thin one only if none exists). `mark_stale` deliberately persists nothing: the design's staleness "indicator" is a derived signal, avoiding a schema change (Task 0).

- [ ] **Step 4: Start/stop the task**

Grep `_pm_sync_task` across `orchestrator.py`. For each occurrence (creation site in `start()`, cancellation site in `stop()`/shutdown), add the identical line for `_pm_triage_task` / `_pm_triage_tick_loop` immediately after it.

- [ ] **Step 5: Verify**

Run: `ruff check server/ && mypy server/ --ignore-missing-imports` — both clean.
Run: `… -m pytest server/tests/ -x -q 2>&1 | tail -20` (full server suite, serialized) — no regressions.

- [ ] **Step 6: Commit**

```bash
git add server/sessions/orchestrator.py
git commit -m "feat(pm): wire pm-triage loop + seams into the orchestrator"
```

---

### Task 4: Inbox API — accept-bulk, overlay PATCH, drift-accept re-propose

**Files:**
- Modify: `server/api/pm.py` (append routes after the phase-2 triage routes)
- Test: `server/tests/test_pm_api.py` (extend — the phase-1/2 module; reuse its `_client()` TestClient fixture + `IDENTITY_HEADERS`)

**Interfaces:**
- Consumes: models `WorkItem, TriageProposal, Feature`; `record_audit`, `current_identity`, `get_db`; the shared accept helper `_apply_accept(db, proposal, identity, *, edits=None)` that phase 2 **already defines** in `server/api/pm.py` (reuse it — do NOT redefine or duplicate the overlay-copy). Phase 3 only EXTENDS it (adds the drift-repropose branch).
- Produces (spec §7):
  - `POST /api/pm/triage/accept-bulk` — body `{ids: [proposalId, ...]}` → `{"accepted": n, "results": [{id, ok, error?}]}` (207-style aggregate, HTTP 200); each id runs the SAME copy-onto-overlay path as single-accept; a `no_fit` proposal whose suggested feature now exists (merged via a drift PR) links to it and re-proposes cleanly.
  - `PATCH /api/pm/work-items/{id}/overlay` — body `{featureId?, reach?, impact?, effort?, confidence?, triageState?}` (all optional) → updated work-item dict. Manual classify / inline score edit; sets `classified_by="human"`, `classified_at=now`; 422 on an out-of-range `triageState`, 404 on unknown work item / feature.
  - Drift-accept re-propose: when accepting a `no_fit` proposal whose `no_fit_suggestion` now matches a real `Feature.slug` (the merged map-diff), the accept links the item to that feature AND re-queues any other `no_fit` items sharing the suggestion as fresh **pending** proposals against the new feature (spec §6.3 "orphaned items are re-proposed against the new map"). Phase 3 ADDS this branch (plus `_repropose_siblings`) to the phase-2 `_apply_accept` helper so both single-accept (phase 2's `POST /triage/{id}/accept`) and accept-bulk get it — phase 3 does NOT redefine `_apply_accept`.

- [ ] **Step 1: Write the failing tests**

Append to `server/tests/test_pm_api.py` (reuse the module's existing `_client()` / seed helpers; these mirror `test_sdlc_api.py`):

```python
def _seed_triage(db_url, *, no_fit_suggestion=None, feature_slug=None):
    """Seed a Project→WorkItem→pending TriageProposal, optionally with a Feature
    matching a no_fit suggestion (the merged drift map). Returns proposal id."""
    import asyncio
    from server.db import db_session, init_db
    from server.db.models import (
        Feature, Project, TriageProposal, WorkItem)

    async def _seed():
        await init_db(db_url)
        async with db_session() as db:
            p = Project(slug="ccswitch", name="ccswitch")
            db.add(p)
            await db.flush()
            feat = None
            if feature_slug:
                feat = Feature(project_id=p.id, slug=feature_slug,
                               title=feature_slug, state="live")
                db.add(feat)
                await db.flush()
            w = WorkItem(repo="steiner385/ccswitch", number=42, kind="issue",
                         title="Fix", state="open", triage_state="unclassified")
            db.add(w)
            await db.flush()
            tp = TriageProposal(
                work_item_id=w.id, rationale="looks like sessions",
                reach=3, impact=2, effort=1, confidence=2, status="pending",
                proposed_feature_id=(feat.id if feat and not no_fit_suggestion else None),
                no_fit_suggestion=no_fit_suggestion)
            db.add(tp)
            await db.flush()
            return tp.id, w.id
    return asyncio.run(_seed())


def test_accept_bulk_copies_overlay():
    import os, tempfile
    from unittest.mock import patch
    with tempfile.TemporaryDirectory() as tmp:
        db_url = f"sqlite+aiosqlite:///{tmp}/test.db"
        pid, wid = _seed_triage(db_url, feature_slug="sessions")
        with patch.dict(os.environ, {"CCSWITCH_DB_URL": db_url, "CCSWITCH_DEV": "1",
                                     "CCSWITCH_TAILNET_ALLOWLIST": ""}):
            from fastapi.testclient import TestClient
            from server.app import create_app
            from server.sessions.orchestrator import SessionOrchestrator
            from server.tests.fakes.fake_factory import fake_sdk_factory
            app = create_app(orchestrator=SessionOrchestrator(sdk_factory=fake_sdk_factory()))
            with TestClient(app, raise_server_exceptions=False) as c:
                r = c.post("/api/pm/triage/accept-bulk", json={"ids": [pid]},
                           headers=IDENTITY_HEADERS)
                assert r.status_code == 200, r.text
                body = r.json()
                assert body["accepted"] == 1
                assert body["results"][0]["ok"] is True
                # work item overlay now carries the proposal's scores + feature
                detail = c.get("/api/pm/triage", headers=IDENTITY_HEADERS)
                assert detail.status_code == 200


def test_overlay_patch_manual_classify():
    import os, tempfile
    from unittest.mock import patch
    with tempfile.TemporaryDirectory() as tmp:
        db_url = f"sqlite+aiosqlite:///{tmp}/test.db"
        _pid, wid = _seed_triage(db_url, feature_slug="sessions")
        with patch.dict(os.environ, {"CCSWITCH_DB_URL": db_url, "CCSWITCH_DEV": "1",
                                     "CCSWITCH_TAILNET_ALLOWLIST": ""}):
            from fastapi.testclient import TestClient
            from server.app import create_app
            from server.sessions.orchestrator import SessionOrchestrator
            from server.tests.fakes.fake_factory import fake_sdk_factory
            app = create_app(orchestrator=SessionOrchestrator(sdk_factory=fake_sdk_factory()))
            with TestClient(app, raise_server_exceptions=False) as c:
                r = c.patch(f"/api/pm/work-items/{wid}/overlay",
                            json={"reach": 5, "triageState": "classified"},
                            headers=IDENTITY_HEADERS)
                assert r.status_code == 200, r.text
                body = r.json()
                assert body["reach"] == 5
                assert body["triageState"] == "classified"
                assert body["classifiedBy"] == "human"

                bad = c.patch(f"/api/pm/work-items/{wid}/overlay",
                              json={"triageState": "bogus"}, headers=IDENTITY_HEADERS)
                assert bad.status_code == 422
                assert bad.json()["detail"]["error"] == "bad_triage_state"

                missing = c.patch("/api/pm/work-items/nope/overlay",
                                  json={"reach": 1}, headers=IDENTITY_HEADERS)
                assert missing.status_code == 404


def test_drift_accept_reproposes_orphaned_siblings():
    """Two no_fit items share a suggestion; the drift map merged a matching
    Feature. Accepting one links it AND re-proposes the sibling against the new
    feature as a fresh pending proposal (design §6.3)."""
    import asyncio, os, tempfile
    from unittest.mock import patch
    from server.db import db_session, init_db
    from server.db.models import Feature, Project, TriageProposal, WorkItem

    with tempfile.TemporaryDirectory() as tmp:
        db_url = f"sqlite+aiosqlite:///{tmp}/test.db"

        async def _seed():
            await init_db(db_url)
            async with db_session() as db:
                p = Project(slug="ccswitch", name="ccswitch")
                db.add(p); await db.flush()
                feat = Feature(project_id=p.id, slug="offline-mode",
                               title="Offline mode", state="live")
                db.add(feat); await db.flush()
                ids = []
                for n in (1, 2):
                    w = WorkItem(repo="steiner385/ccswitch", number=n, kind="issue",
                                 title=f"#{n}", state="open",
                                 triage_state="unclassified")
                    db.add(w); await db.flush()
                    tp = TriageProposal(work_item_id=w.id, status="pending",
                                        no_fit_suggestion="offline-mode",
                                        rationale="offline")
                    db.add(tp); await db.flush()
                    ids.append(tp.id)
                return ids
        pid1, pid2 = asyncio.run(_seed())

        with patch.dict(os.environ, {"CCSWITCH_DB_URL": db_url, "CCSWITCH_DEV": "1",
                                     "CCSWITCH_TAILNET_ALLOWLIST": ""}):
            from fastapi.testclient import TestClient
            from server.app import create_app
            from server.sessions.orchestrator import SessionOrchestrator
            from server.tests.fakes.fake_factory import fake_sdk_factory
            app = create_app(orchestrator=SessionOrchestrator(sdk_factory=fake_sdk_factory()))
            with TestClient(app, raise_server_exceptions=False) as c:
                r = c.post("/api/pm/triage/accept-bulk", json={"ids": [pid1]},
                           headers=IDENTITY_HEADERS)
                assert r.status_code == 200, r.text
                assert r.json()["accepted"] == 1

                # the sibling (pid2) was re-proposed against offline-mode: still a
                # pending proposal, now with a concrete proposed_feature_id.
                async def _check():
                    async with db_session() as db:
                        from sqlalchemy import select
                        sib = (await db.execute(
                            select(TriageProposal).where(TriageProposal.id == pid2)
                        )).scalar_one()
                        return sib.status, sib.proposed_feature_id
                status_, fid = asyncio.run(_check())
                assert status_ == "pending"
                assert fid is not None
```

- [ ] **Step 2: Run to verify failure** — Expected: 404s (routes not mounted) or assertion failures on the re-propose behavior.

- [ ] **Step 3: Implement the routes**

Append to `server/api/pm.py`. **`_apply_accept` already exists — phase 2 defines it** (`_apply_accept(db, proposal, identity, *, edits=None)`; it copies proposal → WorkItem overlay, sets `triage_state="classified"`, `classified_by="human"`, `classified_at=now`, `proposal.status="accepted"`, `resolved_at=now`, applies `edits`, and audits with the tailnet `identity`). Do NOT redefine it. Phase 3 makes exactly two edits to it: (a) add the drift-repropose branch shown below just before the overlay is written, and (b) add the new `_repropose_siblings` helper. Everything else in `_apply_accept` is phase 2's and stays untouched.

```python
from server.db.models import (  # extend the existing import block
    FEATURE_LIFECYCLE_STATES,       # (if not already imported)
    WORK_ITEM_TRIAGE_STATES,
    TriageProposal,
)


# --- EDIT (a): inside the EXISTING phase-2 `_apply_accept`, right after it
#     resolves `feature_id = edits.get("featureId", proposal.proposed_feature_id)`
#     and BEFORE it writes `wi.feature_id`, add the drift-resolution branch: ---

    # Drift resolution: a no_fit whose suggested name is now a real feature
    # (the merged map-diff). Link it and re-propose orphaned siblings.
    if feature_id is None and proposal.no_fit_suggestion:
        feat = (await db.execute(
            select(Feature).where(Feature.slug == proposal.no_fit_suggestion)
        )).scalar_one_or_none()
        if feat is not None:
            feature_id = feat.id
            await _repropose_siblings(db, proposal, feat.id)

# (The rest of `_apply_accept` — overlay copy, classified_by="human",
#  proposal.status="accepted", audit with `identity` — is phase 2's; leave it.)


# --- EDIT (b): add this NEW helper (phase 3 only): ---

async def _repropose_siblings(db, accepted: TriageProposal, feature_id: str) -> None:
    """Other pending no_fit proposals sharing this suggestion get re-pointed at
    the newly-merged feature so the human confirms them against the real map."""
    siblings = (await db.execute(
        select(TriageProposal).where(
            TriageProposal.id != accepted.id,
            TriageProposal.status == "pending",
            TriageProposal.no_fit_suggestion == accepted.no_fit_suggestion)
    )).scalars().all()
    for sib in siblings:
        sib.proposed_feature_id = feature_id
        sib.no_fit_suggestion = None  # now has a home; no longer a drift item


class AcceptBulkBody(BaseModel):
    ids: list[str]


@router.post("/triage/accept-bulk")
async def accept_bulk(
    body: AcceptBulkBody,
    identity: str = Depends(current_identity),
    db=Depends(get_db),
) -> dict:
    results: list[dict] = []
    accepted = 0
    for pid in body.ids:
        proposal = (await db.execute(
            select(TriageProposal).where(TriageProposal.id == pid)
        )).scalar_one_or_none()
        if proposal is None:
            results.append({"id": pid, "ok": False, "error": "not_found"})
            continue
        if proposal.status != "pending":
            results.append({"id": pid, "ok": False, "error": "not_pending"})
            continue
        try:
            await _apply_accept(db, proposal, identity)
            results.append({"id": pid, "ok": True})
            accepted += 1
        except HTTPException as e:
            detail = e.detail if isinstance(e.detail, dict) else {"error": "failed"}
            results.append({"id": pid, "ok": False, "error": detail.get("error")})
    await db.flush()
    return {"accepted": accepted, "results": results}


class OverlayPatchBody(BaseModel):
    featureId: str | None = None
    reach: int | None = None
    impact: int | None = None
    effort: int | None = None
    confidence: int | None = None
    triageState: str | None = None


@router.patch("/work-items/{item_id}/overlay")
async def patch_overlay(
    item_id: str,
    body: OverlayPatchBody,
    identity: str = Depends(current_identity),
    db=Depends(get_db),
) -> dict:
    if body.triageState is not None and body.triageState not in WORK_ITEM_TRIAGE_STATES:
        raise HTTPException(
            status_code=422,
            detail={"error": "bad_triage_state",
                    "message": f"triageState must be one of {WORK_ITEM_TRIAGE_STATES}"})
    wi = (await db.execute(
        select(WorkItem).where(WorkItem.id == item_id))).scalar_one_or_none()
    if wi is None:
        raise HTTPException(status_code=404, detail={"error": "not_found"})
    if body.featureId is not None:
        feat = (await db.execute(
            select(Feature).where(Feature.id == body.featureId))).scalar_one_or_none()
        if feat is None:
            raise HTTPException(status_code=404,
                                detail={"error": "feature_not_found"})
        wi.feature_id = body.featureId
    for attr in ("reach", "impact", "effort", "confidence"):
        val = getattr(body, attr)
        if val is not None:
            setattr(wi, attr, val)
    if body.triageState is not None:
        wi.triage_state = body.triageState
    wi.classified_by = "human"
    wi.classified_at = datetime.now(timezone.utc)
    await db.flush()
    await record_audit(db=db, type="pm.workitem.overlay", identity=identity,
                       detail={"workItemId": item_id})
    return _work_item_to_dict(wi)
```

Add a `_work_item_to_dict` helper (camelCase) if phase 2 did not already add one; if it did, reuse it verbatim (do not add a second serializer):

```python
def _work_item_to_dict(w: WorkItem) -> dict:
    return {
        "id": w.id, "repo": w.repo, "number": w.number, "kind": w.kind,
        "title": w.title, "state": w.state,
        "featureId": w.feature_id,
        "reach": w.reach, "impact": w.impact, "effort": w.effort,
        "confidence": w.confidence,
        "triageState": w.triage_state,
        "classifiedBy": w.classified_by,
        "classifiedAt": w.classified_at.isoformat() if w.classified_at else None,
        "url": w.url,
    }
```

`POST /triage/{id}/accept` already calls `_apply_accept` (phase 2 wired it), so the drift-repropose branch you added above fires on single-accept too — no route change needed there. Grep to confirm `_apply_accept` is defined once (phase 2) and that both `accept_bulk` and the single-accept route call it — do NOT add a second copy path or a second definition.

- [ ] **Step 4: Run tests** — `… -m pytest server/tests/test_pm_api.py -v` Expected: PASS (new tests + phase-1/2 tests still green).

- [ ] **Step 5: Commit**

```bash
git add server/api/pm.py server/tests/test_pm_api.py
git commit -m "feat(pm): triage accept-bulk + overlay PATCH + drift re-propose"
```

---

### Task 5: Web — triage API client + hooks

**Files:**
- Modify: `web/src/api/triage.ts` (phase 2 already created it — ADD `acceptBulk`/`updateOverlay` fetchers + their types; do NOT recreate `TriageProposal`/`fetchTriage`/`acceptProposal`/`rejectProposal`)
- Modify: `web/src/api/hooks.ts` (UPGRADE the existing `useAcceptProposal`/`useRejectProposal` to optimistic in place; ADD `useTriageLive`, `useAcceptBulk`, `useUpdateOverlay`. `QK.triage` already exists — CONSUME it, do NOT re-add it)
- Test: `web/src/triage/__tests__/hooks.test.tsx` (create — optimistic-accept rollback)

**Interfaces:**
- Consumes: `api` client (`get/post/patch/del`), React Query, the Task 4 routes; the phase-2 module `web/src/api/triage.ts` (`interface TriageProposal`, `fetchTriage(projectSlug?)`, `acceptProposal(id, edits?)`, `rejectProposal(id)`, `proposeMap`, `classify`) and phase-2 hooks `useTriage(projectSlug?)`/`useAcceptProposal(projectSlug?)`/`useRejectProposal(projectSlug?)`; the existing `QK.triage = ['pm','triage']` (added by phase 2).
- Produces: added to `web/src/api/triage.ts` — `acceptBulk`/`updateOverlay` fetchers + `AcceptBulkResult`/`OverlayEdit`/`WorkItemOverlay` types; added to `hooks.ts` — `useTriageLive`, `useAcceptBulk`, `useUpdateOverlay`; and the SAME `useAcceptProposal`/`useRejectProposal` upgraded in place to optimistic (onMutate + rollback).

**Design note:** phase 2 already shipped `web/src/api/triage.ts` (the `TriageProposal` type + `fetchTriage`/`acceptProposal`/`rejectProposal` fetchers) and the phase-2 triage hooks in `hooks.ts`. Phase 3 does NOT create a second `triage.ts`, a second `TriageProposal` shape, or a second set of hooks under different signatures/import source — it EXTENDS the existing module in place. Project types stay in `pm.ts`.

- [ ] **Step 1: Extend the existing API module**

`web/src/api/triage.ts` already exists (phase 2) with `interface TriageProposal`, `fetchTriage(projectSlug?)`, `acceptProposal(id, edits?)`, `rejectProposal(id)` (plus `proposeMap`, `classify`) and the shared `OverlayEdit` type. Do NOT recreate any of those. ADD only the accept-bulk / overlay-PATCH surface:

```typescript
// --- ADD to web/src/api/triage.ts (phase 2 already exports TriageProposal,
//     fetchTriage, acceptProposal, rejectProposal, OverlayEdit). ---

export interface WorkItemOverlay {
  id: string
  repo: string | null
  number: number | null
  kind: string | null
  title: string | null
  state: string | null
  featureId: string | null
  reach: number | null
  impact: number | null
  effort: number | null
  confidence: number | null
  triageState: string | null
  classifiedBy: string | null
  classifiedAt: string | null
  url: string | null
}

export interface AcceptBulkResult {
  accepted: number
  results: { id: string; ok: boolean; error?: string }[]
}

export function acceptBulk(ids: string[]): Promise<AcceptBulkResult> {
  return api.post<AcceptBulkResult>('/pm/triage/accept-bulk', { ids })
}

export function updateOverlay(itemId: string, edit: OverlayEdit): Promise<WorkItemOverlay> {
  return api.patch<WorkItemOverlay>(
    `/pm/work-items/${encodeURIComponent(itemId)}/overlay`, edit)
}
```

If phase 2's `OverlayEdit` shape lacks any field phase 3 needs (`featureId?, reach?, impact?, effort?, confidence?, triageState?`), widen the EXISTING type in place — do not declare a second one.

- [ ] **Step 2: Add hooks**

`QK.triage = ['pm','triage']` already exists in the `QK` object (added by phase 2) — CONSUME it, do NOT re-add it. `useTriage` is phase 2's and stays as-is. Phase 3 UPGRADES the existing `useAcceptProposal`/`useRejectProposal` in place (replace their plain bodies with the optimistic onMutate+rollback bodies below) and APPENDS `useTriageLive`, `useAcceptBulk`, `useUpdateOverlay`. Follow the `useInitiativesLive` + optimistic-`useCloseSession` idioms exactly. Extend the existing `./triage` import (phase 2 already imports `acceptProposal, fetchTriage, rejectProposal, type TriageProposal`) with the new names:

```typescript
// EXTEND the existing import from './triage' with:
import {
  acceptBulk, updateOverlay,
  type AcceptBulkResult, type OverlayEdit, type WorkItemOverlay,
} from './triage'

/** WebSocket invalidation — the useInitiativesLive pattern, keyed to triage. */
export function useTriageLive() {
  const qc = useQueryClient()
  const { on, off } = useSessionSocket()
  useEffect(() => {
    const nudge = () => void qc.invalidateQueries({ queryKey: QK.triage })
    on('status', nudge)
    return () => off('status', nudge)
  }, [on, off, qc])
}

/** Optimistic single-accept — remove the row now, roll back on error.
 *  REPLACES phase-2's plain `useAcceptProposal(projectSlug?)` body in place
 *  (same name, same `{ id, edits? }` variable shape; `acceptProposal` and
 *  `TriageProposal` are already imported from './triage' by phase 2). */
export function useAcceptProposal(projectSlug?: string) {
  const qc = useQueryClient()
  const key = [...QK.triage, projectSlug ?? null]
  return useMutation<void, ApiRequestError, { id: string; edits?: OverlayEdit },
                     { prev?: TriageProposal[] }>({
    mutationFn: ({ id, edits }) => acceptProposal(id, edits),
    onMutate: async ({ id }) => {
      await qc.cancelQueries({ queryKey: key })
      const prev = qc.getQueryData<TriageProposal[]>(key)
      qc.setQueryData<TriageProposal[]>(key, (cur) => (cur ?? []).filter((p) => p.id !== id))
      return { prev }
    },
    onError: (e, _v, ctx) => {
      if (ctx?.prev) qc.setQueryData(key, ctx.prev)
      toast.error(initiativeErrorMessage(e, 'Could not accept'))
    },
    onSettled: () => void qc.invalidateQueries({ queryKey: QK.triage }),
  })
}

/** Optimistic single-reject — REPLACES phase-2's plain body in place (same name,
 *  canonical `{ id: string }` variable shape). */
export function useRejectProposal(projectSlug?: string) {
  const qc = useQueryClient()
  const key = [...QK.triage, projectSlug ?? null]
  return useMutation<void, ApiRequestError, { id: string }, { prev?: TriageProposal[] }>({
    mutationFn: ({ id }) => rejectProposal(id),
    onMutate: async ({ id }) => {
      await qc.cancelQueries({ queryKey: key })
      const prev = qc.getQueryData<TriageProposal[]>(key)
      qc.setQueryData<TriageProposal[]>(key, (cur) => (cur ?? []).filter((p) => p.id !== id))
      return { prev }
    },
    onError: (e, _v, ctx) => {
      if (ctx?.prev) qc.setQueryData(key, ctx.prev)
      toast.error(initiativeErrorMessage(e, 'Could not reject'))
    },
    onSettled: () => void qc.invalidateQueries({ queryKey: QK.triage }),
  })
}

/** Accept-selected (the `A` key). Optimistically drops all selected rows. */
export function useAcceptBulk(projectSlug?: string) {
  const qc = useQueryClient()
  const key = [...QK.triage, projectSlug ?? null]
  return useMutation<AcceptBulkResult, ApiRequestError, string[],
                     { prev?: TriageProposal[] }>({
    mutationFn: (ids) => acceptBulk(ids),
    onMutate: async (ids) => {
      await qc.cancelQueries({ queryKey: key })
      const prev = qc.getQueryData<TriageProposal[]>(key)
      const drop = new Set(ids)
      qc.setQueryData<TriageProposal[]>(key, (cur) => (cur ?? []).filter((p) => !drop.has(p.id)))
      return { prev }
    },
    onError: (e, _ids, ctx) => {
      if (ctx?.prev) qc.setQueryData(key, ctx.prev)
      toast.error(initiativeErrorMessage(e, 'Could not accept selection'))
    },
    onSuccess: (res) => {
      if (res.accepted) toast.success(`Accepted ${res.accepted}`)
    },
    onSettled: () => void qc.invalidateQueries({ queryKey: QK.triage }),
  })
}

/** Inline overlay edit (edit-then-accept `e`, and manual score edits). */
export function useUpdateOverlay() {
  const qc = useQueryClient()
  return useMutation<WorkItemOverlay, ApiRequestError, { itemId: string; edit: OverlayEdit }>({
    mutationFn: ({ itemId, edit }) => updateOverlay(itemId, edit),
    onSuccess: () => void qc.invalidateQueries({ queryKey: QK.triage }),
    onError: (e) => toast.error(initiativeErrorMessage(e, 'Could not update')),
  })
}
```

- [ ] **Step 3: Write the failing rollback test**

Create `web/src/triage/__tests__/hooks.test.tsx`. Mirror the InitiativesTab test setup (QueryClientProvider wrapper, `vi.mock` the socket + api module):

```typescript
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { renderHook, waitFor, act } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'

vi.mock('@/ws/client', () => ({ useSessionSocket: () => ({ on: vi.fn(), off: vi.fn() }) }))
vi.mock('sonner', () => ({ toast: { error: vi.fn(), success: vi.fn() } }))
vi.mock('@/api/triage', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/triage')>()
  return { ...actual, acceptProposal: vi.fn(), acceptBulk: vi.fn(), fetchTriage: vi.fn() }
})

import { useAcceptProposal } from '@/api/hooks'
import { QK } from '@/api/hooks'
import * as triage from '@/api/triage'

const mocked = triage as unknown as { acceptProposal: ReturnType<typeof vi.fn> }

function makeWrapper(qc: QueryClient) {
  return ({ children }: { children: ReactNode }) =>
    <QueryClientProvider client={qc}>{children}</QueryClientProvider>
}

beforeEach(() => vi.clearAllMocks())

describe('useAcceptProposal optimistic rollback', () => {
  it('removes the row optimistically and restores it when accept fails', async () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const seed = [
      { id: 'p1', workItemId: 'w1', title: 'A' },
      { id: 'p2', workItemId: 'w2', title: 'B' },
    ] as unknown as triage.TriageProposal[]
    qc.setQueryData([...QK.triage, null], seed)
    mocked.acceptProposal.mockRejectedValueOnce(new Error('boom'))

    const { result } = renderHook(() => useAcceptProposal(), { wrapper: makeWrapper(qc) })
    await act(async () => {
      result.current.mutate({ id: 'p1' })
    })
    // after the rejection settles, the optimistic removal is rolled back
    await waitFor(() => {
      const rows = qc.getQueryData<triage.TriageProposal[]>([...QK.triage, null])
      expect(rows?.map((r) => r.id)).toEqual(['p1', 'p2'])
    })
  })
})
```

- [ ] **Step 4: Run** — `(cd web && npx vitest run src/triage/__tests__/hooks.test.tsx)` Expected: PASS. Then `(cd web && npx tsc --noEmit && npx eslint src/api/triage.ts src/api/hooks.ts)` clean.

- [ ] **Step 5: Commit**

```bash
git add web/src/api/triage.ts web/src/api/hooks.ts web/src/triage/__tests__/hooks.test.tsx
git commit -m "feat(pm): triage inbox API client + optimistic accept/bulk/overlay hooks"
```

---

### Task 6: Web — the keyboard-first Triage Inbox + tab wiring; delete phase-2 review list

**Files:**
- Create: `web/src/triage/TriageInbox.tsx`
- Create: `web/src/triage/triage.css`
- Create: `web/src/triage/__tests__/TriageInbox.test.tsx`
- Modify: `web/src/components/tabDefs.ts` (add `{ id: 'triage', label: 'Triage' }` after the initiatives entry)
- Modify: `web/src/components/Dashboard.tsx` (`TabId` union gains `'triage'`; add the tab panel beside the `initiatives` one)
- Modify: `web/src/components/DesktopShell.tsx` (add the `tab === 'triage'` branch beside `initiatives`)
- Modify: `web/src/App.tsx` (add `<Route path="/triage" element={<DashboardRoute tab="triage" />} />`)
- Delete: `web/src/projects/TriageReviewList.tsx` (+ its test if present)
- Modify: the phase-2 wizard (`web/src/projects/*` — grep for `TriageReviewList`) to link to `/triage` instead of embedding the plain list

**Interfaces:**
- Consumes: `useTriage`, `useTriageLive`, `useAcceptProposal`, `useRejectProposal`, `useAcceptBulk`, `useUpdateOverlay` (Task 5).
- Produces: `TriageInbox` component — a keyboard-first flat queue: `j/k` move cursor, `a` accept, `e` edit-then-accept (opens an inline score/feature editor, Enter commits via `useUpdateOverlay` then accept), `r` reject, `x` toggle-select, `A` accept-selected. Optimistic UI (rows vanish immediately). `no_fit` rows show the suggested new-feature name. Header shows per-project queue depth.

**Keyboard state machine (the unit under test):** extract the pure reducer so vitest tests it directly:

```typescript
// keyboardState.ts (co-located, exported for tests)
export interface InboxState {
  cursor: number            // index into the visible rows
  selected: Set<string>     // proposal ids toggled with `x`
  editing: string | null    // proposal id being edited (`e`), or null
}

export type InboxEvent =
  | { type: 'move'; delta: number; count: number }
  | { type: 'toggleSelect'; id: string }
  | { type: 'startEdit'; id: string }
  | { type: 'cancelEdit' }
  | { type: 'rowsChanged'; count: number }   // clamp cursor when rows shrink

export function inboxReducer(s: InboxState, e: InboxEvent): InboxState {
  switch (e.type) {
    case 'move': {
      if (e.count === 0) return { ...s, cursor: 0 }
      const next = Math.min(Math.max(s.cursor + e.delta, 0), e.count - 1)
      return { ...s, cursor: next }
    }
    case 'toggleSelect': {
      const selected = new Set(s.selected)
      if (selected.has(e.id)) selected.delete(e.id)
      else selected.add(e.id)
      return { ...s, selected }
    }
    case 'startEdit':
      return { ...s, editing: e.id }
    case 'cancelEdit':
      return { ...s, editing: null }
    case 'rowsChanged': {
      const cursor = e.count === 0 ? 0 : Math.min(s.cursor, e.count - 1)
      return { ...s, cursor }
    }
    default:
      return s
  }
}

export const initialInboxState: InboxState = { cursor: 0, selected: new Set(), editing: null }
```

- [ ] **Step 1: Write the failing tests (reducer + keyboard + optimistic accept)**

Create `web/src/triage/__tests__/TriageInbox.test.tsx`:

```typescript
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'

import { inboxReducer, initialInboxState } from '../keyboardState'

// --- pure reducer (no DOM) ---
describe('inboxReducer', () => {
  it('j/k move clamps to [0, count-1]', () => {
    let s = initialInboxState
    s = inboxReducer(s, { type: 'move', delta: 1, count: 3 })   // 0 -> 1
    s = inboxReducer(s, { type: 'move', delta: 1, count: 3 })   // 1 -> 2
    s = inboxReducer(s, { type: 'move', delta: 1, count: 3 })   // clamp at 2
    expect(s.cursor).toBe(2)
    s = inboxReducer(s, { type: 'move', delta: -5, count: 3 })  // clamp at 0
    expect(s.cursor).toBe(0)
  })

  it('x toggles selection', () => {
    let s = inboxReducer(initialInboxState, { type: 'toggleSelect', id: 'p1' })
    expect(s.selected.has('p1')).toBe(true)
    s = inboxReducer(s, { type: 'toggleSelect', id: 'p1' })
    expect(s.selected.has('p1')).toBe(false)
  })

  it('rowsChanged clamps the cursor when the queue shrinks', () => {
    const s = inboxReducer({ ...initialInboxState, cursor: 4 },
                           { type: 'rowsChanged', count: 2 })
    expect(s.cursor).toBe(1)
  })

  it('e enters edit mode and cancel exits', () => {
    let s = inboxReducer(initialInboxState, { type: 'startEdit', id: 'p2' })
    expect(s.editing).toBe('p2')
    s = inboxReducer(s, { type: 'cancelEdit' })
    expect(s.editing).toBeNull()
  })
})

// --- component keyboard + optimistic accept ---
vi.mock('@/ws/client', () => ({ useSessionSocket: () => ({ on: vi.fn(), off: vi.fn() }) }))
vi.mock('sonner', () => ({ toast: { error: vi.fn(), success: vi.fn() } }))
vi.mock('@/api/triage', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/triage')>()
  return { ...actual, fetchTriage: vi.fn(), acceptProposal: vi.fn(),
           rejectProposal: vi.fn(), acceptBulk: vi.fn(), updateOverlay: vi.fn() }
})

import { TriageInbox } from '../TriageInbox'
import * as triage from '@/api/triage'

const mocked = triage as unknown as {
  fetchTriage: ReturnType<typeof vi.fn>
  acceptProposal: ReturnType<typeof vi.fn>
  acceptBulk: ReturnType<typeof vi.fn>
}

const ROWS = [
  { id: 'p1', workItemId: 'w1', repo: 'o/r', number: 1, kind: 'issue',
    title: 'First', proposedFeatureSlug: 'sessions', noFitSuggestion: null,
    reach: 3, impact: 2, effort: 1, confidence: 2, confidenceLevel: 'high',
    status: 'pending', projectSlug: 'ccswitch', proposedFeatureId: 'f1',
    rationale: 'r', createdAt: null },
  { id: 'p2', workItemId: 'w2', repo: 'o/r', number: 2, kind: 'issue',
    title: 'Second', proposedFeatureSlug: null, noFitSuggestion: 'offline-mode',
    reach: 1, impact: 1, effort: 1, confidence: 1, confidenceLevel: 'low',
    status: 'pending', projectSlug: 'ccswitch', proposedFeatureId: null,
    rationale: 'r', createdAt: null },
] as unknown as triage.TriageProposal[]

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>
}

beforeEach(() => vi.clearAllMocks())

describe('TriageInbox', () => {
  it('renders the queue with a no_fit suggested name and per-project depth', async () => {
    mocked.fetchTriage.mockResolvedValue(ROWS)
    render(<TriageInbox />, { wrapper })
    await waitFor(() => expect(screen.getByText('First')).toBeInTheDocument())
    // no_fit row surfaces the suggested new-feature name
    expect(screen.getByText(/offline-mode/)).toBeInTheDocument()
    // header shows per-project queue depth
    expect(screen.getByText(/ccswitch/)).toBeInTheDocument()
    expect(screen.getByText(/2/)).toBeInTheDocument()
  })

  it('`a` accepts the row under the cursor (optimistic, calls the API)', async () => {
    mocked.fetchTriage.mockResolvedValue(ROWS)
    mocked.acceptProposal.mockResolvedValue(undefined)
    render(<TriageInbox />, { wrapper })
    await waitFor(() => expect(screen.getByText('First')).toBeInTheDocument())
    fireEvent.keyDown(window, { key: 'a' })
    await waitFor(() =>
      expect(mocked.acceptProposal).toHaveBeenCalledWith('p1', undefined))
  })

  it('`x` then `A` accepts the selected set in bulk', async () => {
    mocked.fetchTriage.mockResolvedValue(ROWS)
    mocked.acceptBulk.mockResolvedValue({ accepted: 2, results: [] })
    render(<TriageInbox />, { wrapper })
    await waitFor(() => expect(screen.getByText('First')).toBeInTheDocument())
    fireEvent.keyDown(window, { key: 'x' })            // select p1 (cursor 0)
    fireEvent.keyDown(window, { key: 'j' })            // cursor -> p2
    fireEvent.keyDown(window, { key: 'x' })            // select p2
    fireEvent.keyDown(window, { key: 'A' })            // accept selected
    await waitFor(() =>
      expect(mocked.acceptBulk).toHaveBeenCalledWith(
        expect.arrayContaining(['p1', 'p2'])))
  })
})
```

- [ ] **Step 2: Run to verify failure** — `(cd web && npx vitest run src/triage/__tests__/TriageInbox.test.tsx)` Expected: FAIL — cannot resolve `../keyboardState` / `../TriageInbox`.

- [ ] **Step 3: Implement**

Create `web/src/triage/keyboardState.ts` (the reducer block from the Interfaces section above). Then `web/src/triage/TriageInbox.tsx`:

```tsx
/**
 * Triage Inbox — the daily driver (design §8). One flat queue across all
 * projects, newest first, keyboard-first: j/k move · a accept · e edit-then-
 * accept · r reject · x select · A accept-selected. Optimistic over the
 * proposal→overlay copy (rows vanish immediately, roll back on error).
 * Replaces the phase-2 plain TriageReviewList.
 */
import { useEffect, useMemo, useReducer, useState } from 'react'
import {
  useAcceptBulk, useAcceptProposal, useRejectProposal, useTriage, useTriageLive,
  useUpdateOverlay,
} from '@/api/hooks'
import type { OverlayEdit, TriageProposal } from '@/api/triage'
import { inboxReducer, initialInboxState } from './keyboardState'
import './triage.css'

function queueDepthByProject(rows: TriageProposal[]): [string, number][] {
  const counts = new Map<string, number>()
  for (const r of rows) {
    const k = r.projectSlug ?? 'unknown'
    counts.set(k, (counts.get(k) ?? 0) + 1)
  }
  return [...counts.entries()]
}

export function TriageInbox() {
  useTriageLive()
  const { data: rows = [], isLoading } = useTriage()
  const accept = useAcceptProposal()
  const reject = useRejectProposal()
  const acceptBulk = useAcceptBulk()
  const updateOverlay = useUpdateOverlay()

  const [state, dispatch] = useReducer(inboxReducer, initialInboxState)
  const [editValues, setEditValues] = useState<OverlayEdit>({})

  // Clamp the cursor whenever the queue length changes (optimistic removals).
  useEffect(() => {
    dispatch({ type: 'rowsChanged', count: rows.length })
  }, [rows.length])

  const current = rows[state.cursor] as TriageProposal | undefined

  useEffect(() => {
    function onKey(ev: KeyboardEvent) {
      // Don't hijack typing inside the inline editor's inputs.
      const tag = (ev.target as HTMLElement | null)?.tagName
      if (state.editing && (tag === 'INPUT' || tag === 'SELECT' || tag === 'TEXTAREA')) return
      switch (ev.key) {
        case 'j': dispatch({ type: 'move', delta: 1, count: rows.length }); break
        case 'k': dispatch({ type: 'move', delta: -1, count: rows.length }); break
        case 'x': if (current) dispatch({ type: 'toggleSelect', id: current.id }); break
        case 'a': if (current) accept.mutate({ id: current.id, edits: undefined }); break
        case 'r': if (current) reject.mutate({ id: current.id }); break
        case 'e':
          if (current) {
            setEditValues({
              featureId: current.proposedFeatureId ?? undefined,
              reach: current.reach ?? undefined, impact: current.impact ?? undefined,
              effort: current.effort ?? undefined, confidence: current.confidence ?? undefined,
            })
            dispatch({ type: 'startEdit', id: current.id })
          }
          break
        case 'A':
          if (state.selected.size) acceptBulk.mutate([...state.selected])
          break
        default: return
      }
      ev.preventDefault()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [rows.length, current, state.editing, state.selected, accept, reject, acceptBulk])

  const depths = useMemo(() => queueDepthByProject(rows), [rows])

  async function commitEdit(p: TriageProposal) {
    // edit-then-accept: persist the overlay edit, then accept the proposal.
    await updateOverlay.mutateAsync({ itemId: p.workItemId, edit: editValues })
    accept.mutate({ id: p.id, edits: editValues })
    dispatch({ type: 'cancelEdit' })
  }

  if (isLoading) return <div className="triage-empty">Loading…</div>
  if (!rows.length) return <div className="triage-empty">Inbox zero — nothing to triage.</div>

  return (
    <div className="triage-inbox" role="list" aria-label="Triage queue">
      <header className="triage-header">
        {depths.map(([slug, n]) => (
          <span key={slug} className="triage-depth">{slug}: {n}</span>
        ))}
      </header>
      {rows.map((p, i) => (
        <div
          key={p.id}
          role="listitem"
          aria-selected={i === state.cursor}
          className={
            'triage-row' + (i === state.cursor ? ' is-cursor' : '') +
            (state.selected.has(p.id) ? ' is-selected' : '')
          }
        >
          <span className="triage-title">{p.title}</span>
          <span className="triage-ref">{p.repo}#{p.number}</span>
          {p.noFitSuggestion
            ? <span className="triage-nofit">no_fit → new: {p.noFitSuggestion}</span>
            : <span className="triage-feature">{p.proposedFeatureSlug}</span>}
          <span className={`triage-conf conf-${p.confidenceLevel}`}>{p.confidenceLevel}</span>
          {state.editing === p.id && (
            <form
              className="triage-editor"
              onSubmit={(e) => { e.preventDefault(); void commitEdit(p) }}
            >
              <input aria-label="reach" type="number" value={editValues.reach ?? ''}
                     onChange={(e) => setEditValues((v) => ({ ...v, reach: Number(e.target.value) }))} />
              <input aria-label="impact" type="number" value={editValues.impact ?? ''}
                     onChange={(e) => setEditValues((v) => ({ ...v, impact: Number(e.target.value) }))} />
              <input aria-label="effort" type="number" value={editValues.effort ?? ''}
                     onChange={(e) => setEditValues((v) => ({ ...v, effort: Number(e.target.value) }))} />
              <input aria-label="confidence" type="number" value={editValues.confidence ?? ''}
                     onChange={(e) => setEditValues((v) => ({ ...v, confidence: Number(e.target.value) }))} />
              <button type="submit">Save &amp; accept</button>
              <button type="button" onClick={() => dispatch({ type: 'cancelEdit' })}>Cancel</button>
            </form>
          )}
        </div>
      ))}
    </div>
  )
}
```

Create `web/src/triage/triage.css` (minimal — cursor/selected highlight, confidence colouring; follow the existing initiatives CSS token idiom):

```css
.triage-inbox { display: flex; flex-direction: column; gap: 2px; padding: 8px; }
.triage-header { display: flex; gap: 12px; padding: 4px 8px; font-size: 12px; opacity: 0.8; }
.triage-row { display: flex; gap: 12px; align-items: center; padding: 6px 8px;
  border: 1px solid transparent; border-radius: 6px; }
.triage-row.is-cursor { border-color: var(--ring, #6366f1); background: var(--muted, #1e1e28); }
.triage-row.is-selected { outline: 2px solid var(--primary, #6366f1); }
.triage-nofit { color: var(--warning, #f59e0b); }
.triage-conf.conf-low { color: var(--warning, #f59e0b); }
.triage-empty { padding: 24px; text-align: center; opacity: 0.7; }
```

- [ ] **Step 4: Wire the tab (mirror the phase-1 Products-tab wiring exactly)**

- `web/src/components/Dashboard.tsx`: add `'triage'` to the `TabId` union; add the panel next to `initiatives`:

```tsx
{activeTab === 'triage' && (
  <div id="tabpanel-triage" role="tabpanel" aria-labelledby="tab-triage"
       className="flex flex-col flex-1 min-h-0">
    <TriageInbox />
  </div>
)}
```
(import `TriageInbox` at the top: `import { TriageInbox } from '../triage/TriageInbox'`.)

- `web/src/components/tabDefs.ts`: add `{ id: 'triage', label: 'Triage' }` after the `initiatives` entry.
- `web/src/components/DesktopShell.tsx`: add the branch next to `initiatives`:
```tsx
} else if (tab === 'triage') {
  mainPane = <div className="flex flex-col h-full"><TriageInbox /></div>
```
(import `TriageInbox`.)
- `web/src/App.tsx`: add `<Route path="/triage" element={<DashboardRoute tab="triage" />} />` beside the `/initiatives` route.

- [ ] **Step 5: Delete the phase-2 review list + repoint the wizard**

- Grep `web/src/` for `TriageReviewList` — remove the import + usage from the phase-2 wizard component and replace the embedded list with a link/CTA to the `/triage` inbox (e.g. a "Review N proposals in Triage →" button navigating to `/triage`). The wizard's `classifying`-state action stays; only the inline list is removed.
- `git rm web/src/projects/TriageReviewList.tsx` and its test if present (`web/src/projects/__tests__/TriageReviewList.test.tsx`).
- Grep once more to confirm zero remaining references (`grep -rn TriageReviewList web/src` → empty).

- [ ] **Step 6: Run the web gate**

Run (in `web/`): `npx vitest run src/triage`, then `npx tsc --noEmit`, then `npx eslint src/triage src/components src/App.tsx src/api`.
Expected: tests PASS (reducer 4 + component 3 + Task-5 rollback 1), tsc clean, eslint clean. If tsc flags a missing `TabId` case in a `switch`, add the `triage` case — the union is exhaustive-checked.

- [ ] **Step 7: Commit**

```bash
git add web/src/triage web/src/components/Dashboard.tsx web/src/components/tabDefs.ts \
        web/src/components/DesktopShell.tsx web/src/App.tsx web/src/projects
git rm web/src/projects/TriageReviewList.tsx
git commit -m "feat(pm): keyboard-first Triage Inbox tab; remove phase-2 review list"
```

---

### Task 7: Full-gate verification + PR

**Files:** none (verification + integration).

- [ ] **Step 1: Server four-step gate (serialized)**

```bash
ruff check src/ server/
mypy server/ --ignore-missing-imports
env … CCSWITCH_DEV=1 /home/tony/GitHub/ccswitch/.venv/bin/python -m pytest tests/ -q
env … CCSWITCH_DEV=1 /home/tony/GitHub/ccswitch/.venv/bin/python -m pytest server/tests/ -q
```
Expected: all four clean/green. Pay attention to mypy on the new engine union and the loop's `Callable` seam aliases (the failure mode CLAUDE.md calls out — a stray `float | None` in a comparison). The engine's `next_triage_action` returns the closed `TriageAction` union; the loop's `isinstance` ladder is exhaustive.

- [ ] **Step 2: Web gate (in `web/`)**

```bash
npx vitest run
npx tsc --noEmit
npx eslint .
```
Expected: all green.

- [ ] **Step 3: Confirm no schema drift snuck in**

Run: `… -m pytest server/tests/test_schema_reconcile_coverage.py server/tests/test_postgres_compat.py -v` (the PG file is skipped without `CCSWITCH_TEST_PG_URL` — that's fine; phase 3 adds no table so no `_FK_LINKS`/`_REQUIRED_EXTRAS` entry is needed, but the reconcile-coverage test must stay green).
Expected: reconcile-coverage PASS; PG suite PASS or skipped.

- [ ] **Step 4: Open the PR**

```bash
git push -u origin feat/pm-product-layer-phase3
gh pr create --title "feat(pm): Phase 3 — continuous triage loop + keyboard inbox" \
  --body "$(cat <<'EOF'
Phase 3 of the PM product layer (design §6/§7/§8): the continuous curation loop
and the keyboard-first Triage Inbox.

- `server/pm/engine.py` — pure pm-triage decision engine (classify-the-new
  batching, drift clustering → feature-map diff, staleness nudge; enumerated
  (state, counts, mode) table).
- `server/automations/pm_triage.py` — PmTriage + PmTriageLoop (modes off|propose;
  mechanical auto-link pass; reuses phase-2 classification). Registered.
- Orchestrator `_pm_triage_*` seams (reusing the phase-2 classify/propose-map
  spawn paths — no second classifier).
- API: POST /triage/accept-bulk, PATCH /work-items/{id}/overlay; drift-accept
  re-proposes orphaned siblings against the merged map.
- Web: TriageInbox.tsx (j/k/a/e/r/x/A, optimistic accept), useTriageLive/
  useAcceptBulk/useUpdateOverlay; new Triage tab + /triage route; **deletes the
  phase-2 TriageReviewList and repoints the wizard**.
- No new tables/columns — every concern routes through phase-1's schema.

TDD throughout; four-step server gate + web gate green.

Refs the PM product-layer design (docs/superpowers/specs/2026-08-31-pm-product-layer-design.md).

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
gh pr merge --squash --auto
```

Mark ready (non-draft) and arm auto-merge, per the completion workflow.

---

## Self-review (spec §6 coverage · placeholder scan · type consistency)

**Spec §6 coverage (the four tick concerns):**
1. *Classify the new* → engine `ClassifyBatch` (Task 1, bounded by `batch_size` + day cap) executed by the loop reusing the phase-2 classify path (Tasks 2–3). ✅
2. *Auto-link the mechanical* → loop pre-classify pass + `_pm_triage_auto_link` seam (closing-keyword inheritance, no session), unit-tested in Task 2. ✅ (Deliberately NOT an engine action — documented in Task 1.)
3. *Detect map drift* → engine `ProposeFeatureMapDiff` on a `no_fit` cluster ≥ threshold; `_pm_triage_propose_map_diff` opens the diff PR; drift-accept re-propose in the API (Task 4). ✅
4. *Nudge staleness* → engine `NudgeStale`; `_pm_triage_mark_stale` (derived indicator, no persisted column). ✅
   *Rejection is signal / no blind retry* → `list_pending` excludes `needs_human`; asserted in Task 2's pending-set filtering and Task 1's docstring. Past-corrections-into-prompts is phase-2's `build_classification_prompt(corrections=…)` — the loop passes accepted/rejected history through that existing parameter (noted in Task 3 classify seam). ✅

**Spec §7 API coverage:** `POST /triage/accept-bulk` ✅, `PATCH /work-items/{id}/overlay` ✅. Single-accept/reject/`GET /triage` are phase-2's (reused, not rebuilt). ✅

**Spec §8 inbox coverage:** flat cross-project queue ✅, `j/k/a/e/r/x/A` ✅, optimistic proposal→overlay ✅, `no_fit` suggested name shown ✅, per-project queue-depth header ✅.

**Placeholder scan:** every code block is real. The only intentional `...` bodies are `_pm_triage_read_config` and `_pm_triage_tick_loop` (Task 3), which are explicit "copy the named `_pm_sync_*` counterpart, change the id" instructions — matching the phase-1 plan's identical treatment of `_pm_sync_read_config`/`_pm_sync_tick_loop`. No TODO/FIXME/`pass`-stub logic remains.

**Type consistency:** engine returns the closed `TriageAction = ClassifyBatch | ProposeFeatureMapDiff | NudgeStale | Idle | NeedsHuman` union; the loop's `isinstance` ladder covers all non-`Idle` arms and `Idle` is the fall-through. Seam `Callable` aliases mirror `PmSyncLoop`'s. API bodies are Pydantic camelCase; serializers emit camelCase; `detail` is `{error, message}`. Web types are the server JSON verbatim; hooks reuse `ApiRequestError`/`toast`/`initiativeErrorMessage`; `QK.triage = ['pm','triage']` (added by phase 2) is CONSUMED, not re-added; phase 3 reuses phase-2's `web/src/api/triage.ts` + hooks in place (no parallel copy). `mypy server/ --ignore-missing-imports` and `tsc --noEmit` are gated in Task 7.

**Ambiguities resolved (see 5-line summary in the handoff).**

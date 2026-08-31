# PM Product Layer — Phase 1 (Foundation + Sync) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Land the PM domain model (Project/Feature/WorkItem/TriageProposal + relationships), the `pm-sync` automation (GraphQL backfill + incremental mirroring + feature-doc indexing), and the project registry API + minimal web UI.

**Architecture:** Extends the SDLC spine in place. New models follow the `Initiative` mold (nullable columns, writer-side invariants, `_new_uuid`/`_utcnow`). The sync automation follows the `SdlcIntakeLoop` mold (contract class + injected-seam control loop, pure helpers in `server/pm/`). GitHub I/O goes through the existing `gh_run` seam (`ccswitch.gitrepos._run_gh` — GraphQL only). API mirrors `server/api/sdlc.py`; web mirrors the `initiatives` tab.

**Tech Stack:** Python 3.12 / FastAPI / SQLAlchemy 2 async / Alembic / PyYAML (already a dep) · React + Vite + TS + React Query · `gh` CLI for GraphQL.

**Spec:** `docs/superpowers/specs/2026-08-31-pm-product-layer-design.md` (read it first — the plan argues from it).

## Global Constraints

- Branch off `origin/master` first: `git fetch && git checkout -b feat/pm-product-layer-phase1 origin/master`. NEVER branch off local `master`.
- Every new DB column nullable (reconcile ALTER-ADDs it); writer-side invariants documented in docstrings, per the `Initiative` mold at `server/db/models.py:922-927`.
- Python tests run via the prebuilt venv: `env -u CCSWITCH_TAILNET_ALLOWLIST -u CCSWITCH_SESSIOND_HOST -u CCSWITCH_SESSIOND_PORT -u CCSWITCH_SESSIOND_CLIENT_CERT -u CCSWITCH_SESSIOND_CLIENT_KEY -u CCSWITCH_SESSIOND_CA CCSWITCH_DEV=1 /home/tony/GitHub/ccswitch/.venv/bin/python -m pytest <path> -v` (this phase touches only `server/`, so no PYTHONPATH override needed).
- Web commands run in `web/` (symlink `node_modules` from `/home/tony/GitHub/ccswitch/web/node_modules` if absent): `npx vitest run`, `npx tsc --noEmit`, `npx eslint .`
- Before EVERY push, the full four-step gate: `ruff check src/ server/` · `mypy server/ --ignore-missing-imports` · `pytest tests/` · `pytest server/tests/` (serialize full-suite runs — concurrent runs on this host get SIGKILLed).
- New models added ⇒ run the Postgres suite locally if `CCSWITCH_TEST_PG_URL` is available; regardless, Task 4 updates the two hand-maintained lists so it passes.
- GitHub reads: GraphQL via the `gh_run` seam only. Never REST reads, never raw tokens.
- Commit format: `feat(pm): …` / `test(pm): …`, ending with `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`.

---

### Task 1: Core project models (Project, ProjectRepo, ProjectEdge)

**Files:**
- Modify: `server/db/models.py` (append after `InitiativeArtifact`, ~line 1001)
- Test: `server/tests/test_pm_models.py` (create)

**Interfaces:**
- Produces: `Project`, `ProjectRepo`, `ProjectEdge` ORM classes; constants `PROJECT_ONBOARDING_STATES`, `PROJECT_EDGE_KINDS`. Later tasks import these from `server.db.models`.
- Consumes: existing `Base`, `_new_uuid`, `_utcnow` in `models.py`.

- [ ] **Step 1: Write the failing test**

Create `server/tests/test_pm_models.py`. Mirror the fixture style of `server/tests/test_initiative_models.py` (read it first; reuse its engine/session fixture verbatim — it builds an in-memory SQLite via `create_all`):

```python
"""PM product-layer model smoke tests (design: 2026-08-31-pm-product-layer)."""
import pytest
from sqlalchemy import select

from server.db.models import (
    PROJECT_EDGE_KINDS,
    PROJECT_ONBOARDING_STATES,
    Project,
    ProjectEdge,
    ProjectRepo,
)


@pytest.mark.asyncio
async def test_project_defaults(db_session):
    p = Project(slug="kindash", name="KinDash")
    db_session.add(p)
    await db_session.flush()
    assert p.id
    assert p.onboarding_state == "registered"
    assert p.status == "active"
    assert p.created_at is not None


@pytest.mark.asyncio
async def test_project_repo_and_edge(db_session):
    a = Project(slug="kindash", name="KinDash")
    b = Project(slug="infra", name="Infra")
    db_session.add_all([a, b])
    await db_session.flush()
    db_session.add(ProjectRepo(project_id=a.id, repo="steiner385/KinDash", is_primary=True))
    db_session.add(ProjectEdge(src_project_id=a.id, dst_project_id=b.id, kind="depends_on"))
    await db_session.flush()
    edge = (await db_session.execute(select(ProjectEdge))).scalar_one()
    assert edge.kind in PROJECT_EDGE_KINDS


def test_state_constants_are_ordered():
    assert PROJECT_ONBOARDING_STATES[0] == "registered"
    assert PROJECT_ONBOARDING_STATES[-1] == "live"
```

If `test_initiative_models.py` defines its session fixture locally (not in `conftest.py`), copy that fixture into this file unchanged.

- [ ] **Step 2: Run test to verify it fails**

Run: `... -m pytest server/tests/test_pm_models.py -v` (with the env prefix from Global Constraints)
Expected: FAIL — `ImportError: cannot import name 'Project'`

- [ ] **Step 3: Write the models**

Append to `server/db/models.py` (after `InitiativeArtifact`). Verify `Boolean`, `Integer`, `JSON`, `UniqueConstraint` are in the existing `sqlalchemy` import line; add any that are missing.

```python
# ---------------------------------------------------------------------------
# PM product layer (design: docs/superpowers/specs/2026-08-31-pm-product-layer-design.md)
#
# Truth model is layered: GitHub owns work items (WorkItem is a disposable
# mirror), repo markdown owns feature definitions (Feature is an index row,
# like InitiativeArtifact), and these tables own ONLY what has no other home:
# relationships, the classification/score overlay, and AI triage proposals.
# Every non-key column is nullable so _reconcile_columns can ALTER-ADD it
# (same rule as the SDLC block above).
# ---------------------------------------------------------------------------

# Onboarding wizard states, in order (spec §5). Single source of truth.
PROJECT_ONBOARDING_STATES: tuple[str, ...] = (
    "registered", "syncing", "map_proposed", "map_merged", "classifying", "live")

# Typed project→project relationship kinds (spec §3).
PROJECT_EDGE_KINDS: tuple[str, ...] = ("depends_on", "deploys_via", "integrates_with")


class Project(Base):
    """One product under PM management. ``slug`` is the stable identity;
    repos attach via ProjectRepo (the primary repo hosts the feature docs)."""

    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_new_uuid)
    # Writer-side invariant: always set (nullable for reconcile, like Initiative.slug).
    slug: Mapped[str | None] = mapped_column(String, nullable=True, unique=True, index=True)
    name: Mapped[str | None] = mapped_column(String, nullable=True)
    description: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str | None] = mapped_column(String, nullable=True, default="active")
    portfolio_tags: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # First PROJECT_ONBOARDING_STATES entry; advanced by sync/onboarding flows.
    onboarding_state: Mapped[str | None] = mapped_column(
        String, nullable=True, default="registered")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow)


class ProjectRepo(Base):
    """A repo attached to a project, carrying that repo's sync state.

    ``backfill_cursor``/``backfill_done`` drive resumable history import;
    ``watermark`` is the incremental-sync high-water mark and advances ONLY
    after a fully successful pass (spec §4). ``sync_requested_at`` is the
    sync-now signal set by the API and cleared by the loop."""

    __tablename__ = "project_repos"
    __table_args__ = (UniqueConstraint("project_id", "repo", name="uq_project_repo"),)

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_new_uuid)
    project_id: Mapped[str] = mapped_column(
        String, ForeignKey("projects.id"), nullable=True, index=True)
    repo: Mapped[str | None] = mapped_column(String, nullable=True)  # "owner/name"
    is_primary: Mapped[bool | None] = mapped_column(Boolean, nullable=True, default=False)
    backfill_cursor: Mapped[str | None] = mapped_column(String, nullable=True)
    # Which phase the cursor belongs to: "issues" then "prs" (backfill order).
    backfill_phase: Mapped[str | None] = mapped_column(String, nullable=True, default="issues")
    backfill_done: Mapped[bool | None] = mapped_column(Boolean, nullable=True, default=False)
    watermark: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    sync_requested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    last_synced_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow)


class ProjectEdge(Base):
    """Typed project→project relationship (spec §3): depends_on | deploys_via |
    integrates_with, with an optional free-text note."""

    __tablename__ = "project_edges"
    __table_args__ = (
        UniqueConstraint("src_project_id", "dst_project_id", "kind", name="uq_project_edge"),)

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_new_uuid)
    src_project_id: Mapped[str] = mapped_column(
        String, ForeignKey("projects.id"), nullable=True, index=True)
    dst_project_id: Mapped[str] = mapped_column(
        String, ForeignKey("projects.id"), nullable=True, index=True)
    kind: Mapped[str | None] = mapped_column(String, nullable=True)  # PROJECT_EDGE_KINDS
    note: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `... -m pytest server/tests/test_pm_models.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add server/db/models.py server/tests/test_pm_models.py
git commit -m "feat(pm): Project, ProjectRepo, ProjectEdge models"
```

---

### Task 2: Feature, FeatureLink, WorkItem, TriageProposal models

**Files:**
- Modify: `server/db/models.py` (append after `ProjectEdge`)
- Test: `server/tests/test_pm_models.py` (extend)

**Interfaces:**
- Produces: `Feature`, `FeatureLink`, `WorkItem`, `TriageProposal` ORM classes; constants `FEATURE_LIFECYCLE_STATES`, `FEATURE_LINK_KINDS`, `WORK_ITEM_KINDS`, `WORK_ITEM_TRIAGE_STATES`, `TRIAGE_PROPOSAL_STATUSES`.
- Consumes: Task 1's `Project`.

- [ ] **Step 1: Write the failing tests**

Append to `server/tests/test_pm_models.py`:

```python
from server.db.models import (  # merge into the existing import block
    FEATURE_LIFECYCLE_STATES,
    TRIAGE_PROPOSAL_STATUSES,
    WORK_ITEM_TRIAGE_STATES,
    Feature,
    FeatureLink,
    TriageProposal,
    WorkItem,
)


@pytest.mark.asyncio
async def test_feature_is_an_index_row(db_session):
    p = Project(slug="ccswitch", name="ccswitch")
    db_session.add(p)
    await db_session.flush()
    f = Feature(project_id=p.id, slug="sessions", title="Session manager",
                state="live", repo_path="docs/product/features/sessions.md")
    db_session.add(f)
    await db_session.flush()
    assert f.commit_sha is None          # not merged yet — pointer semantics
    assert f.orphaned is False or f.orphaned is None
    assert f.state in FEATURE_LIFECYCLE_STATES


@pytest.mark.asyncio
async def test_work_item_overlay_and_proposal(db_session):
    w = WorkItem(repo="steiner385/ccswitch", number=42, kind="issue",
                 title="Fix the thing", state="open")
    db_session.add(w)
    await db_session.flush()
    assert w.triage_state == "unclassified"
    assert w.triage_state in WORK_ITEM_TRIAGE_STATES
    assert w.feature_id is None
    t = TriageProposal(work_item_id=w.id, rationale="looks like sessions work")
    db_session.add(t)
    await db_session.flush()
    assert t.status == "pending"
    assert t.status in TRIAGE_PROPOSAL_STATUSES


@pytest.mark.asyncio
async def test_feature_link(db_session):
    p = Project(slug="p1", name="P1")
    db_session.add(p)
    await db_session.flush()
    f1 = Feature(project_id=p.id, slug="a", title="A", state="live")
    f2 = Feature(project_id=p.id, slug="b", title="B", state="live")
    db_session.add_all([f1, f2])
    await db_session.flush()
    db_session.add(FeatureLink(src_feature_id=f1.id, dst_feature_id=f2.id, kind="powered_by"))
    await db_session.flush()
```

- [ ] **Step 2: Run to verify failure**

Run: `... -m pytest server/tests/test_pm_models.py -v`
Expected: new tests FAIL with ImportError; Task 1's tests still PASS.

- [ ] **Step 3: Write the models**

Append to `server/db/models.py`:

```python
# Feature lifecycle (spec §3, "feature doc convention").
FEATURE_LIFECYCLE_STATES: tuple[str, ...] = (
    "proposed", "discovery", "delivering", "live", "sunset")

# Feature→feature relationship kinds (cross-project allowed).
FEATURE_LINK_KINDS: tuple[str, ...] = ("powered_by", "integrates_with")

WORK_ITEM_KINDS: tuple[str, ...] = ("issue", "pr")

# Approved-overlay triage states on WorkItem (proposals live in TriageProposal).
WORK_ITEM_TRIAGE_STATES: tuple[str, ...] = (
    "unclassified", "proposed", "classified", "needs_human")

TRIAGE_PROPOSAL_STATUSES: tuple[str, ...] = ("pending", "accepted", "rejected")


class Feature(Base):
    """Index row for one ``docs/product/features/<slug>.md`` in the project's
    primary repo. Truth is the merged markdown: ``commit_sha`` is stamped by
    sync once the doc is on the default branch (NULL while only proposed) —
    the InitiativeArtifact pointer pattern. ``orphaned`` marks a doc that
    disappeared from the repo (row kept so FeatureLinks never dangle)."""

    __tablename__ = "features"
    __table_args__ = (UniqueConstraint("project_id", "slug", name="uq_feature_slug"),)

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_new_uuid)
    project_id: Mapped[str] = mapped_column(
        String, ForeignKey("projects.id"), nullable=True, index=True)
    slug: Mapped[str | None] = mapped_column(String, nullable=True)
    title: Mapped[str | None] = mapped_column(String, nullable=True)
    state: Mapped[str | None] = mapped_column(String, nullable=True)  # FEATURE_LIFECYCLE_STATES
    summary: Mapped[str | None] = mapped_column(String, nullable=True)
    repo_path: Mapped[str | None] = mapped_column(String, nullable=True)
    commit_sha: Mapped[str | None] = mapped_column(String, nullable=True, default=None)
    pr_number: Mapped[int | None] = mapped_column(Integer, nullable=True, default=None)
    pr_url: Mapped[str | None] = mapped_column(String, nullable=True, default=None)
    orphaned: Mapped[bool | None] = mapped_column(Boolean, nullable=True, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow)


class FeatureLink(Base):
    """Feature→feature edge (spec §3): powered_by | integrates_with."""

    __tablename__ = "feature_links"
    __table_args__ = (
        UniqueConstraint("src_feature_id", "dst_feature_id", "kind", name="uq_feature_link"),)

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_new_uuid)
    src_feature_id: Mapped[str] = mapped_column(
        String, ForeignKey("features.id"), nullable=True, index=True)
    dst_feature_id: Mapped[str] = mapped_column(
        String, ForeignKey("features.id"), nullable=True, index=True)
    kind: Mapped[str | None] = mapped_column(String, nullable=True)  # FEATURE_LINK_KINDS
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow)


class WorkItem(Base):
    """Read-only mirror of one GitHub issue or PR, plus the ccswitch-owned
    overlay. Mirror columns are disposable (rebuildable by re-sync); overlay
    columns (feature_id, scores, triage_state) are the approved truth ccswitch
    owns. Keyed by (repo, number, kind) so a dropped-and-resynced mirror keeps
    the same identity."""

    __tablename__ = "work_items"
    __table_args__ = (
        UniqueConstraint("repo", "number", "kind", name="uq_work_item_identity"),)

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_new_uuid)
    # --- mirror (GitHub truth) ---
    repo: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    kind: Mapped[str | None] = mapped_column(String, nullable=True)  # WORK_ITEM_KINDS
    title: Mapped[str | None] = mapped_column(String, nullable=True)
    body: Mapped[str | None] = mapped_column(String, nullable=True)
    state: Mapped[str | None] = mapped_column(String, nullable=True)  # open|closed|merged
    labels: Mapped[list | None] = mapped_column(JSON, nullable=True)
    author: Mapped[str | None] = mapped_column(String, nullable=True)
    url: Mapped[str | None] = mapped_column(String, nullable=True)
    # PR→issue closing references (list of ints), from closingIssuesReferences.
    linked_issue_numbers: Mapped[list | None] = mapped_column(JSON, nullable=True)
    gh_created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    gh_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True)
    gh_closed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    last_synced_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    # --- overlay (ccswitch truth) ---
    feature_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("features.id"), nullable=True, index=True)
    reach: Mapped[int | None] = mapped_column(Integer, nullable=True)
    impact: Mapped[int | None] = mapped_column(Integer, nullable=True)
    effort: Mapped[int | None] = mapped_column(Integer, nullable=True)
    confidence: Mapped[int | None] = mapped_column(Integer, nullable=True)
    triage_state: Mapped[str | None] = mapped_column(
        String, nullable=True, default="unclassified", index=True)
    classified_by: Mapped[str | None] = mapped_column(String, nullable=True)
    classified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow)


class TriageProposal(Base):
    """One AI classification proposal awaiting the human gate (spec §3).
    ``proposed_feature_id`` is NULL for a ``no_fit`` answer, which instead
    carries ``no_fit_suggestion`` (the suggested new-feature name). Accepting
    copies values onto the WorkItem overlay; this row remains as audit."""

    __tablename__ = "triage_proposals"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_new_uuid)
    work_item_id: Mapped[str] = mapped_column(
        String, ForeignKey("work_items.id"), nullable=True, index=True)
    proposed_feature_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("features.id"), nullable=True)
    no_fit_suggestion: Mapped[str | None] = mapped_column(String, nullable=True)
    reach: Mapped[int | None] = mapped_column(Integer, nullable=True)
    impact: Mapped[int | None] = mapped_column(Integer, nullable=True)
    effort: Mapped[int | None] = mapped_column(Integer, nullable=True)
    confidence: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rationale: Mapped[str | None] = mapped_column(String, nullable=True)
    confidence_level: Mapped[str | None] = mapped_column(String, nullable=True)  # low|medium|high
    status: Mapped[str | None] = mapped_column(
        String, nullable=True, default="pending", index=True)  # TRIAGE_PROPOSAL_STATUSES
    session_id: Mapped[str | None] = mapped_column(String, nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow)
```

- [ ] **Step 4: Run tests**

Run: `... -m pytest server/tests/test_pm_models.py -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Commit**

```bash
git add server/db/models.py server/tests/test_pm_models.py
git commit -m "feat(pm): Feature, FeatureLink, WorkItem + overlay, TriageProposal models"
```

---

### Task 3: Initiative extensions (project link, multi-repo/feature M2M, scores)

**Files:**
- Modify: `server/db/models.py` (Initiative class ~line 943; new M2M classes after `TriageProposal`)
- Test: `server/tests/test_pm_models.py` (extend)

**Interfaces:**
- Produces: `Initiative.project_id`, `Initiative.reach/impact/effort/confidence`; `InitiativeRepo`, `InitiativeFeature` ORM classes.
- NOTE: `INITIATIVE_STAGES` is NOT touched — the `discovery` stage is phase 4 behavior (spec §10.4).

- [ ] **Step 1: Write the failing test**

Append to `server/tests/test_pm_models.py`:

```python
from server.db.models import Initiative, InitiativeFeature, InitiativeRepo


@pytest.mark.asyncio
async def test_initiative_pm_extensions(db_session):
    p = Project(slug="px", name="PX")
    db_session.add(p)
    await db_session.flush()
    f = Feature(project_id=p.id, slug="fx", title="FX", state="delivering")
    ini = Initiative(slug="do-fx", repo="steiner385/ccswitch", title="Do FX",
                     project_id=p.id, reach=3, impact=2, effort=1, confidence=2)
    db_session.add_all([f, ini])
    await db_session.flush()
    db_session.add(InitiativeRepo(initiative_id=ini.id, repo="steiner385/ccswitch",
                                  is_primary=True))
    db_session.add(InitiativeFeature(initiative_id=ini.id, feature_id=f.id))
    await db_session.flush()
    assert ini.project_id == p.id
```

- [ ] **Step 2: Run to verify failure** — Expected: FAIL, `project_id` invalid keyword / ImportError.

- [ ] **Step 3: Implement**

Inside `class Initiative`, after `origin_ref` (models.py:968), add:

```python
    # --- PM product layer (phase 1 groundwork; design 2026-08-31) ---
    # Owning project; nullable so pre-PM initiatives keep working unlinked.
    project_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("projects.id"), nullable=True, index=True)
    # RICE-lite score inputs (spec §8: score = reach×impact×confidence/effort).
    reach: Mapped[int | None] = mapped_column(Integer, nullable=True)
    impact: Mapped[int | None] = mapped_column(Integer, nullable=True)
    effort: Mapped[int | None] = mapped_column(Integer, nullable=True)
    confidence: Mapped[int | None] = mapped_column(Integer, nullable=True)
```

NOTE: `Project` is defined AFTER `Initiative` in the file; the string-name `ForeignKey("projects.id")` resolves at mapper-configure time, so definition order is fine.

After `TriageProposal`, add:

```python
class InitiativeRepo(Base):
    """Initiative↔repo M2M — phase-1 groundwork for cross-project initiatives
    (spec §3). ``Initiative.repo`` remains the primary repo until phase 4
    folds it in; rows here are additive."""

    __tablename__ = "initiative_repos"
    __table_args__ = (UniqueConstraint("initiative_id", "repo", name="uq_initiative_repo"),)

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_new_uuid)
    initiative_id: Mapped[str] = mapped_column(
        String, ForeignKey("initiatives.id"), nullable=True, index=True)
    repo: Mapped[str | None] = mapped_column(String, nullable=True)
    is_primary: Mapped[bool | None] = mapped_column(Boolean, nullable=True, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow)


class InitiativeFeature(Base):
    """Which features an initiative creates/changes (spec §3)."""

    __tablename__ = "initiative_features"
    __table_args__ = (
        UniqueConstraint("initiative_id", "feature_id", name="uq_initiative_feature"),)

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_new_uuid)
    initiative_id: Mapped[str] = mapped_column(
        String, ForeignKey("initiatives.id"), nullable=True, index=True)
    feature_id: Mapped[str] = mapped_column(
        String, ForeignKey("features.id"), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow)
```

- [ ] **Step 4: Run tests** — `... -m pytest server/tests/test_pm_models.py server/tests/test_initiative_models.py -v` Expected: ALL PASS (no regression on existing initiative tests).

- [ ] **Step 5: Commit**

```bash
git add server/db/models.py server/tests/test_pm_models.py
git commit -m "feat(pm): initiative project link, repo/feature M2M, RICE-lite scores"
```

---

### Task 4: Alembic revision + Postgres-suite list entries

**Files:**
- Create: `server/db/migrations/versions/0012_pm_product_layer.py`
- Modify: `server/tests/test_postgres_compat.py` (`_FK_LINKS`, ~line 163)
- Modify: `server/tests/test_schema_reconcile_coverage.py` (comment only if needed)

**Interfaces:**
- Consumes: all Task 1–3 tables/columns.
- Produces: the reviewable schema record; a green schema-drift assert and PG sweep.

- [ ] **Step 1: Run the reconcile-coverage test to see the current state**

Run: `... -m pytest server/tests/test_schema_reconcile_coverage.py -v`
Expected: PASS already (every new column is nullable or has a synthesizable default). If it FAILS, a column violated the nullable rule — fix the model, don't add exemptions.

- [ ] **Step 2: Write the revision**

Create `server/db/migrations/versions/0012_pm_product_layer.py`. Mirror the 0011 header style:

```python
"""PM product layer — projects/features/work-item mirror + initiative links.

Revision ID: 0012_pm_product_layer
Revises: 0011_daemon_leases
Create Date: 2026-08-31

Design: docs/superpowers/specs/2026-08-31-pm-product-layer-design.md (§3).
Deploy note: nothing runs ``alembic upgrade`` at deploy time. The NEW tables
are built by ``init_db``'s create_all; the columns added to ``initiatives``
are ALTER-ADDed by _reconcile_columns (all nullable). This revision is the
reviewable record of the change.
"""
from alembic import op
import sqlalchemy as sa

revision = "0012_pm_product_layer"
down_revision = "0011_daemon_leases"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "projects",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("slug", sa.String(), nullable=True, unique=True, index=True),
        sa.Column("name", sa.String(), nullable=True),
        sa.Column("description", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=True),
        sa.Column("portfolio_tags", sa.JSON(), nullable=True),
        sa.Column("onboarding_state", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "project_repos",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("project_id", sa.String(), sa.ForeignKey("projects.id"),
                  nullable=True, index=True),
        sa.Column("repo", sa.String(), nullable=True),
        sa.Column("is_primary", sa.Boolean(), nullable=True),
        sa.Column("backfill_cursor", sa.String(), nullable=True),
        sa.Column("backfill_phase", sa.String(), nullable=True),
        sa.Column("backfill_done", sa.Boolean(), nullable=True),
        sa.Column("watermark", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sync_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("project_id", "repo", name="uq_project_repo"),
    )
    op.create_table(
        "project_edges",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("src_project_id", sa.String(), sa.ForeignKey("projects.id"),
                  nullable=True, index=True),
        sa.Column("dst_project_id", sa.String(), sa.ForeignKey("projects.id"),
                  nullable=True, index=True),
        sa.Column("kind", sa.String(), nullable=True),
        sa.Column("note", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("src_project_id", "dst_project_id", "kind",
                            name="uq_project_edge"),
    )
    op.create_table(
        "features",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("project_id", sa.String(), sa.ForeignKey("projects.id"),
                  nullable=True, index=True),
        sa.Column("slug", sa.String(), nullable=True),
        sa.Column("title", sa.String(), nullable=True),
        sa.Column("state", sa.String(), nullable=True),
        sa.Column("summary", sa.String(), nullable=True),
        sa.Column("repo_path", sa.String(), nullable=True),
        sa.Column("commit_sha", sa.String(), nullable=True),
        sa.Column("pr_number", sa.Integer(), nullable=True),
        sa.Column("pr_url", sa.String(), nullable=True),
        sa.Column("orphaned", sa.Boolean(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("project_id", "slug", name="uq_feature_slug"),
    )
    op.create_table(
        "feature_links",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("src_feature_id", sa.String(), sa.ForeignKey("features.id"),
                  nullable=True, index=True),
        sa.Column("dst_feature_id", sa.String(), sa.ForeignKey("features.id"),
                  nullable=True, index=True),
        sa.Column("kind", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("src_feature_id", "dst_feature_id", "kind",
                            name="uq_feature_link"),
    )
    op.create_table(
        "work_items",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("repo", sa.String(), nullable=True, index=True),
        sa.Column("number", sa.Integer(), nullable=True),
        sa.Column("kind", sa.String(), nullable=True),
        sa.Column("title", sa.String(), nullable=True),
        sa.Column("body", sa.String(), nullable=True),
        sa.Column("state", sa.String(), nullable=True),
        sa.Column("labels", sa.JSON(), nullable=True),
        sa.Column("author", sa.String(), nullable=True),
        sa.Column("url", sa.String(), nullable=True),
        sa.Column("linked_issue_numbers", sa.JSON(), nullable=True),
        sa.Column("gh_created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("gh_updated_at", sa.DateTime(timezone=True), nullable=True, index=True),
        sa.Column("gh_closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("feature_id", sa.String(), sa.ForeignKey("features.id"),
                  nullable=True, index=True),
        sa.Column("reach", sa.Integer(), nullable=True),
        sa.Column("impact", sa.Integer(), nullable=True),
        sa.Column("effort", sa.Integer(), nullable=True),
        sa.Column("confidence", sa.Integer(), nullable=True),
        sa.Column("triage_state", sa.String(), nullable=True, index=True),
        sa.Column("classified_by", sa.String(), nullable=True),
        sa.Column("classified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("repo", "number", "kind", name="uq_work_item_identity"),
    )
    op.create_table(
        "triage_proposals",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("work_item_id", sa.String(), sa.ForeignKey("work_items.id"),
                  nullable=True, index=True),
        sa.Column("proposed_feature_id", sa.String(), sa.ForeignKey("features.id"),
                  nullable=True),
        sa.Column("no_fit_suggestion", sa.String(), nullable=True),
        sa.Column("reach", sa.Integer(), nullable=True),
        sa.Column("impact", sa.Integer(), nullable=True),
        sa.Column("effort", sa.Integer(), nullable=True),
        sa.Column("confidence", sa.Integer(), nullable=True),
        sa.Column("rationale", sa.String(), nullable=True),
        sa.Column("confidence_level", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=True, index=True),
        sa.Column("session_id", sa.String(), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "initiative_repos",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("initiative_id", sa.String(), sa.ForeignKey("initiatives.id"),
                  nullable=True, index=True),
        sa.Column("repo", sa.String(), nullable=True),
        sa.Column("is_primary", sa.Boolean(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("initiative_id", "repo", name="uq_initiative_repo"),
    )
    op.create_table(
        "initiative_features",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("initiative_id", sa.String(), sa.ForeignKey("initiatives.id"),
                  nullable=True, index=True),
        sa.Column("feature_id", sa.String(), sa.ForeignKey("features.id"),
                  nullable=True, index=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("initiative_id", "feature_id", name="uq_initiative_feature"),
    )
    op.add_column("initiatives", sa.Column("project_id", sa.String(),
                  sa.ForeignKey("projects.id"), nullable=True))
    op.add_column("initiatives", sa.Column("reach", sa.Integer(), nullable=True))
    op.add_column("initiatives", sa.Column("impact", sa.Integer(), nullable=True))
    op.add_column("initiatives", sa.Column("effort", sa.Integer(), nullable=True))
    op.add_column("initiatives", sa.Column("confidence", sa.Integer(), nullable=True))


def downgrade() -> None:
    for col in ("confidence", "effort", "impact", "reach", "project_id"):
        op.drop_column("initiatives", col)
    for table in ("initiative_features", "initiative_repos", "triage_proposals",
                  "work_items", "feature_links", "features", "project_edges",
                  "project_repos", "projects"):
        op.drop_table(table)
```

- [ ] **Step 3: Add `_FK_LINKS` entries**

In `server/tests/test_postgres_compat.py`, extend `_FK_LINKS` (after the `initiative_artifacts` entry):

```python
    # PM product layer (design 2026-08-31): point every FK at a live parent row
    # (sorted_tables orders parents first), matching the initiative_artifacts
    # precedent above. No _REQUIRED_EXTRAS entries needed — every PM table's
    # NOT NULL columns are the UUID PK and created_at/updated_at, all defaulted.
    "project_repos": {"project_id": "projects"},
    "project_edges": {"src_project_id": "projects", "dst_project_id": "projects"},
    "features": {"project_id": "projects"},
    "feature_links": {"src_feature_id": "features", "dst_feature_id": "features"},
    "work_items": {"feature_id": "features"},
    "triage_proposals": {"work_item_id": "work_items", "proposed_feature_id": "features"},
    "initiative_repos": {"initiative_id": "initiatives"},
    "initiative_features": {"initiative_id": "initiatives", "feature_id": "features"},
```

- [ ] **Step 4: Run the schema tests**

Run: `... -m pytest server/tests/test_schema_reconcile_coverage.py server/tests/test_pm_models.py -v`
Expected: PASS. If `CCSWITCH_TEST_PG_URL` is set in the environment, ALSO run `... -m pytest server/tests/test_postgres_compat.py -v` and require PASS.

- [ ] **Step 5: Commit**

```bash
git add server/db/migrations/versions/0012_pm_product_layer.py server/tests/test_postgres_compat.py
git commit -m "feat(pm): alembic record + PG-suite coverage for PM tables"
```

---

### Task 5: Feature-doc frontmatter parser (`server/pm/featuredoc.py`)

**Files:**
- Create: `server/pm/__init__.py` (empty)
- Create: `server/pm/featuredoc.py`
- Test: `server/tests/test_pm_featuredoc.py` (create)

**Interfaces:**
- Produces:
  - `FeatureLinkRef(kind: str, project: str, feature: str)` frozen dataclass
  - `FeatureDoc(slug: str, title: str, state: str, links: tuple[FeatureLinkRef, ...], body: str)` frozen dataclass
  - `parse_feature_doc(text: str) -> FeatureDoc` — raises `FeatureDocError` (subclass of `ValueError`) on malformed input
  - `serialize_feature_doc(doc: FeatureDoc) -> str`
- Consumes: `FEATURE_LIFECYCLE_STATES` from `server.db.models`; `yaml` (PyYAML, already a dependency).

- [ ] **Step 1: Write the failing tests**

Create `server/tests/test_pm_featuredoc.py`:

```python
"""Feature-doc frontmatter parser tests (spec §3 'feature doc convention')."""
import pytest

from server.pm.featuredoc import (
    FeatureDoc,
    FeatureDocError,
    FeatureLinkRef,
    parse_feature_doc,
    serialize_feature_doc,
)

VALID = """---
slug: notifications
title: Notifications
state: live
links:
  - kind: powered_by
    project: infra
    feature: message-queue
---
## Problem

Users miss important events.
"""


def test_parse_valid_doc():
    doc = parse_feature_doc(VALID)
    assert doc.slug == "notifications"
    assert doc.state == "live"
    assert doc.links == (FeatureLinkRef("powered_by", "infra", "message-queue"),)
    assert doc.body.startswith("## Problem")


def test_round_trip_is_stable():
    doc = parse_feature_doc(VALID)
    assert parse_feature_doc(serialize_feature_doc(doc)) == doc


def test_missing_frontmatter_raises():
    with pytest.raises(FeatureDocError):
        parse_feature_doc("just a markdown body, no fences")


def test_bad_state_raises():
    with pytest.raises(FeatureDocError):
        parse_feature_doc(VALID.replace("state: live", "state: bogus"))


def test_links_optional():
    text = "---\nslug: a\ntitle: A\nstate: proposed\n---\nbody\n"
    assert parse_feature_doc(text).links == ()
```

- [ ] **Step 2: Run to verify failure** — Expected: FAIL, `ModuleNotFoundError: server.pm`.

- [ ] **Step 3: Implement**

Create `server/pm/__init__.py` (empty) and `server/pm/featuredoc.py`:

```python
"""Feature-doc convention: parse/serialize ``docs/product/features/<slug>.md``.

YAML frontmatter carries the machine-readable index fields; the markdown body
is the human artifact (spec §3). Pure functions, no I/O — the sync loop feeds
file text in and upserts Feature rows from the result.
"""
from __future__ import annotations

from dataclasses import dataclass

import yaml

from server.db.models import FEATURE_LIFECYCLE_STATES, FEATURE_LINK_KINDS


class FeatureDocError(ValueError):
    """Raised for a malformed feature doc. Callers log and skip — one bad doc
    must never poison the whole index (spec §4)."""


@dataclass(frozen=True)
class FeatureLinkRef:
    kind: str
    project: str
    feature: str


@dataclass(frozen=True)
class FeatureDoc:
    slug: str
    title: str
    state: str
    links: tuple[FeatureLinkRef, ...]
    body: str


def parse_feature_doc(text: str) -> FeatureDoc:
    if not text.startswith("---"):
        raise FeatureDocError("missing frontmatter fence")
    parts = text.split("\n---", 2)
    # parts[0] is the opening "---" line (possibly with trailing yaml on next
    # lines), parts[1] the rest of the yaml block only when the fence sits on
    # its own line; the robust split: find the closing fence line.
    lines = text.splitlines()
    try:
        close = next(i for i, ln in enumerate(lines[1:], start=1) if ln.strip() == "---")
    except StopIteration:
        raise FeatureDocError("unterminated frontmatter fence") from None
    raw = "\n".join(lines[1:close])
    try:
        meta = yaml.safe_load(raw) or {}
    except yaml.YAMLError as e:
        raise FeatureDocError(f"bad yaml frontmatter: {e}") from e
    if not isinstance(meta, dict):
        raise FeatureDocError("frontmatter is not a mapping")

    slug = str(meta.get("slug") or "").strip()
    title = str(meta.get("title") or "").strip()
    state = str(meta.get("state") or "").strip()
    if not slug or not title:
        raise FeatureDocError("slug and title are required")
    if state not in FEATURE_LIFECYCLE_STATES:
        raise FeatureDocError(f"state {state!r} not in {FEATURE_LIFECYCLE_STATES}")

    links: list[FeatureLinkRef] = []
    for item in meta.get("links") or []:
        if not isinstance(item, dict):
            raise FeatureDocError("each link must be a mapping")
        kind = str(item.get("kind") or "").strip()
        if kind not in FEATURE_LINK_KINDS:
            raise FeatureDocError(f"link kind {kind!r} not in {FEATURE_LINK_KINDS}")
        links.append(FeatureLinkRef(
            kind=kind,
            project=str(item.get("project") or "").strip(),
            feature=str(item.get("feature") or "").strip(),
        ))

    body = "\n".join(lines[close + 1:]).lstrip("\n")
    return FeatureDoc(slug=slug, title=title, state=state, links=tuple(links), body=body)


def serialize_feature_doc(doc: FeatureDoc) -> str:
    meta: dict = {"slug": doc.slug, "title": doc.title, "state": doc.state}
    if doc.links:
        meta["links"] = [
            {"kind": link.kind, "project": link.project, "feature": link.feature}
            for link in doc.links
        ]
    front = yaml.safe_dump(meta, sort_keys=False).strip()
    return f"---\n{front}\n---\n{doc.body}"
```

- [ ] **Step 4: Run tests** — `... -m pytest server/tests/test_pm_featuredoc.py -v` Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add server/pm/__init__.py server/pm/featuredoc.py server/tests/test_pm_featuredoc.py
git commit -m "feat(pm): feature-doc frontmatter parser + serializer"
```

---

### Task 6: GraphQL query builders + payload parsers (`server/pm/github_sync.py`)

**Files:**
- Create: `server/pm/github_sync.py`
- Test: `server/tests/test_pm_github_sync.py` (create)

**Interfaces:**
- Produces:
  - `WorkItemView(repo, number, kind, title, body, state, labels: tuple[str, ...], author, url, gh_created_at, gh_updated_at, gh_closed_at, linked_issue_numbers: tuple[int, ...])` frozen dataclass (datetimes are aware `datetime | None`)
  - `build_issues_query(repo: str, cursor: str | None, since_iso: str | None) -> str`
  - `build_prs_query(repo: str, cursor: str | None) -> str` (always newest-first by UPDATED_AT; the LOOP stops paging past the watermark — GraphQL has no `since` filter for PRs)
  - `parse_page(payload: dict, repo: str, kind: str) -> tuple[list[WorkItemView], str | None, bool]` — (items, end_cursor, has_next); tolerant of missing keys, returns `([], None, False)` on garbage
- Consumes: nothing project-internal (pure).

- [ ] **Step 1: Write the failing tests**

Create `server/tests/test_pm_github_sync.py`:

```python
"""Pure GraphQL builder/parser tests for pm-sync (spec §4)."""
from datetime import datetime, timezone

from server.pm.github_sync import (
    WorkItemView,
    build_issues_query,
    build_prs_query,
    parse_page,
)


def _issue_payload():
    return {"data": {"repository": {"issues": {
        "pageInfo": {"endCursor": "CUR1", "hasNextPage": True},
        "nodes": [{
            "number": 7, "title": "Bug", "body": "It broke", "state": "OPEN",
            "url": "https://github.com/o/r/issues/7",
            "createdAt": "2026-01-02T03:04:05Z", "updatedAt": "2026-01-03T00:00:00Z",
            "closedAt": None, "author": {"login": "tony"},
            "labels": {"nodes": [{"name": "bug"}]},
        }],
    }}}}


def _pr_payload():
    return {"data": {"repository": {"pullRequests": {
        "pageInfo": {"endCursor": None, "hasNextPage": False},
        "nodes": [{
            "number": 9, "title": "Fix bug", "body": "", "state": "MERGED",
            "url": "https://github.com/o/r/pull/9",
            "createdAt": "2026-01-04T00:00:00Z", "updatedAt": "2026-01-05T00:00:00Z",
            "closedAt": "2026-01-05T00:00:00Z", "author": {"login": "tony"},
            "labels": {"nodes": []},
            "closingIssuesReferences": {"nodes": [{"number": 7}]},
        }],
    }}}}


def test_issue_query_includes_since_and_cursor():
    q = build_issues_query("o/r", "CUR0", "2026-01-01T00:00:00Z")
    assert 'owner: "o"' in q and 'name: "r"' in q
    assert 'after: "CUR0"' in q
    assert "filterBy" in q and "2026-01-01T00:00:00Z" in q


def test_pr_query_orders_updated_desc():
    q = build_prs_query("o/r", None)
    assert "UPDATED_AT" in q and "DESC" in q
    assert "closingIssuesReferences" in q


def test_parse_issue_page():
    items, cursor, more = parse_page(_issue_payload(), "o/r", "issue")
    assert cursor == "CUR1" and more is True
    (item,) = items
    assert item == WorkItemView(
        repo="o/r", number=7, kind="issue", title="Bug", body="It broke",
        state="open", labels=("bug",), author="tony",
        url="https://github.com/o/r/issues/7",
        gh_created_at=datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc),
        gh_updated_at=datetime(2026, 1, 3, tzinfo=timezone.utc),
        gh_closed_at=None, linked_issue_numbers=())


def test_parse_pr_page_normalizes_state_and_links():
    items, cursor, more = parse_page(_pr_payload(), "o/r", "pr")
    assert (cursor, more) == (None, False)
    assert items[0].state == "merged"
    assert items[0].linked_issue_numbers == (7,)


def test_parse_garbage_is_empty():
    assert parse_page({}, "o/r", "issue") == ([], None, False)
    assert parse_page({"data": None}, "o/r", "pr") == ([], None, False)
```

- [ ] **Step 2: Run to verify failure** — Expected: FAIL with ModuleNotFoundError.

- [ ] **Step 3: Implement**

Create `server/pm/github_sync.py`:

```python
"""GraphQL query builders + payload parsers for the pm-sync mirror (spec §4).

Pure functions: the loop owns pagination/watermark policy and the gh_run seam
owns transport. Issues support server-side ``filterBy: {since:}``; pull
requests do NOT, so PR queries are newest-first by UPDATED_AT and the caller
stops paging once nodes fall behind the watermark.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

_PAGE_ISSUES = 100
_PAGE_PRS = 50

_ITEM_FIELDS = """
      number title body state url createdAt updatedAt closedAt
      author { login }
      labels(first: 20) { nodes { name } }"""


@dataclass(frozen=True)
class WorkItemView:
    repo: str
    number: int
    kind: str  # "issue" | "pr"
    title: str
    body: str
    state: str  # open | closed | merged
    labels: tuple[str, ...]
    author: str
    url: str
    gh_created_at: datetime | None
    gh_updated_at: datetime | None
    gh_closed_at: datetime | None
    linked_issue_numbers: tuple[int, ...]


def _owner_name(repo: str) -> tuple[str, str]:
    owner, _, name = repo.partition("/")
    return owner, name


def build_issues_query(repo: str, cursor: str | None, since_iso: str | None) -> str:
    owner, name = _owner_name(repo)
    after = f', after: "{cursor}"' if cursor else ""
    filter_by = f", filterBy: {{since: \"{since_iso}\"}}" if since_iso else ""
    return f"""query {{
  repository(owner: "{owner}", name: "{name}") {{
    issues(first: {_PAGE_ISSUES}{after}, states: [OPEN, CLOSED],
           orderBy: {{field: UPDATED_AT, direction: ASC}}{filter_by}) {{
      pageInfo {{ endCursor hasNextPage }}
      nodes {{{_ITEM_FIELDS}
      }}
    }}
  }}
}}"""


def build_prs_query(repo: str, cursor: str | None) -> str:
    owner, name = _owner_name(repo)
    after = f', after: "{cursor}"' if cursor else ""
    return f"""query {{
  repository(owner: "{owner}", name: "{name}") {{
    pullRequests(first: {_PAGE_PRS}{after}, states: [OPEN, CLOSED, MERGED],
                 orderBy: {{field: UPDATED_AT, direction: DESC}}) {{
      pageInfo {{ endCursor hasNextPage }}
      nodes {{{_ITEM_FIELDS}
        closingIssuesReferences(first: 10) {{ nodes {{ number }} }}
      }}
    }}
  }}
}}"""


def _parse_dt(value: object) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def parse_page(payload: dict, repo: str, kind: str) -> tuple[list[WorkItemView], str | None, bool]:
    field = "issues" if kind == "issue" else "pullRequests"
    try:
        conn = (payload.get("data") or {}).get("repository", {}).get(field) or {}
    except AttributeError:
        return [], None, False
    info = conn.get("pageInfo") or {}
    items: list[WorkItemView] = []
    for node in conn.get("nodes") or []:
        if not node or node.get("number") is None:
            continue
        refs = tuple(
            n["number"]
            for n in ((node.get("closingIssuesReferences") or {}).get("nodes") or [])
            if n and n.get("number") is not None
        ) if kind == "pr" else ()
        items.append(WorkItemView(
            repo=repo,
            number=int(node["number"]),
            kind=kind,
            title=str(node.get("title") or ""),
            body=str(node.get("body") or ""),
            state=str(node.get("state") or "").lower(),
            labels=tuple(
                lbl["name"] for lbl in ((node.get("labels") or {}).get("nodes") or [])
                if lbl and lbl.get("name")),
            author=str(((node.get("author") or {}).get("login")) or ""),
            url=str(node.get("url") or ""),
            gh_created_at=_parse_dt(node.get("createdAt")),
            gh_updated_at=_parse_dt(node.get("updatedAt")),
            gh_closed_at=_parse_dt(node.get("closedAt")),
            linked_issue_numbers=refs,
        ))
    return items, info.get("endCursor"), bool(info.get("hasNextPage"))
```

- [ ] **Step 4: Run tests** — `... -m pytest server/tests/test_pm_github_sync.py -v` Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add server/pm/github_sync.py server/tests/test_pm_github_sync.py
git commit -m "feat(pm): GraphQL builders + parsers for the work-item mirror"
```

---

### Task 7: Feature-index planner (`server/pm/feature_index.py`)

**Files:**
- Create: `server/pm/feature_index.py`
- Test: `server/tests/test_pm_feature_index.py` (create)

**Interfaces:**
- Produces:
  - `FeatureRowView(slug: str, orphaned: bool)` frozen dataclass (the loop builds these from `Feature` rows)
  - `FeatureUpsert(slug, title, state, summary, repo_path)` frozen dataclass
  - `plan_feature_index(existing: list[FeatureRowView], docs: list[tuple[str, "FeatureDoc"]]) -> FeatureIndexPlan` where `FeatureIndexPlan(upserts: tuple[FeatureUpsert, ...], orphan_slugs: tuple[str, ...], revive_slugs: tuple[str, ...])`
- Consumes: `FeatureDoc` from Task 5. Summary = first non-heading, non-empty body line, truncated to 200 chars.

- [ ] **Step 1: Write the failing tests**

Create `server/tests/test_pm_feature_index.py`:

```python
"""Pure feature-index planner tests (spec §4: upsert + sunset-orphan)."""
from server.pm.feature_index import FeatureRowView, plan_feature_index
from server.pm.featuredoc import FeatureDoc


def _doc(slug: str, body: str = "## Problem\n\nSummary line here.") -> FeatureDoc:
    return FeatureDoc(slug=slug, title=slug.title(), state="live", links=(), body=body)


def test_new_docs_become_upserts():
    plan = plan_feature_index([], [("docs/product/features/a.md", _doc("a"))])
    assert len(plan.upserts) == 1
    up = plan.upserts[0]
    assert (up.slug, up.state, up.repo_path) == ("a", "live", "docs/product/features/a.md")
    assert up.summary == "Summary line here."


def test_missing_doc_orphans_row_and_reappearing_revives():
    existing = [FeatureRowView(slug="gone", orphaned=False),
                FeatureRowView(slug="back", orphaned=True)]
    plan = plan_feature_index(existing, [("docs/product/features/back.md", _doc("back"))])
    assert plan.orphan_slugs == ("gone",)
    assert plan.revive_slugs == ("back",)


def test_already_orphaned_not_reorphaned():
    existing = [FeatureRowView(slug="gone", orphaned=True)]
    plan = plan_feature_index(existing, [])
    assert plan.orphan_slugs == ()
```

- [ ] **Step 2: Run to verify failure** — Expected: FAIL with ModuleNotFoundError.

- [ ] **Step 3: Implement**

Create `server/pm/feature_index.py`:

```python
"""Pure planner: diff parsed feature docs against existing index rows.

The loop reads docs from the clone and Feature rows from the DB, calls this,
and applies the plan. A doc that vanished orphans its row (kept so links never
dangle — spec §4); a doc that reappears revives it.
"""
from __future__ import annotations

from dataclasses import dataclass

from server.pm.featuredoc import FeatureDoc

_SUMMARY_MAX = 200


@dataclass(frozen=True)
class FeatureRowView:
    slug: str
    orphaned: bool


@dataclass(frozen=True)
class FeatureUpsert:
    slug: str
    title: str
    state: str
    summary: str
    repo_path: str


@dataclass(frozen=True)
class FeatureIndexPlan:
    upserts: tuple[FeatureUpsert, ...]
    orphan_slugs: tuple[str, ...]
    revive_slugs: tuple[str, ...]


def _summary(body: str) -> str:
    for line in body.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            return stripped[:_SUMMARY_MAX]
    return ""


def plan_feature_index(
    existing: list[FeatureRowView],
    docs: list[tuple[str, FeatureDoc]],
) -> FeatureIndexPlan:
    seen = {doc.slug for _, doc in docs}
    upserts = tuple(
        FeatureUpsert(slug=doc.slug, title=doc.title, state=doc.state,
                      summary=_summary(doc.body), repo_path=path)
        for path, doc in docs)
    orphan_slugs = tuple(
        row.slug for row in existing if not row.orphaned and row.slug not in seen)
    revive_slugs = tuple(
        row.slug for row in existing if row.orphaned and row.slug in seen)
    return FeatureIndexPlan(upserts=upserts, orphan_slugs=orphan_slugs,
                            revive_slugs=revive_slugs)
```

- [ ] **Step 4: Run tests** — `... -m pytest server/tests/test_pm_feature_index.py -v` Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add server/pm/feature_index.py server/tests/test_pm_feature_index.py
git commit -m "feat(pm): pure feature-index planner (upsert/orphan/revive)"
```

---

### Task 8: The pm-sync automation (`server/automations/pm_sync.py`)

**Files:**
- Create: `server/automations/pm_sync.py`
- Modify: `server/automations/registry.py` (register `PmSync`, mirroring the `SdlcIntake` lines at the bottom)
- Test: `server/tests/test_pm_sync_loop.py` (create)

**Interfaces:**
- Consumes: `parse_page`/`build_*_query`/`WorkItemView` (Task 6), `plan_feature_index`/`FeatureRowView` (Task 7), `parse_feature_doc`/`FeatureDocError` (Task 5), `ConfigField`/`AutomationStatus` from `server/automations/base.py`.
- Produces: `PmSync` (contract, id `"pm-sync"`, modes `("off", "on")`), `PmSyncLoop` with injected seams, and `RepoSyncView` — the loop's input row:

```python
@dataclass(frozen=True)
class RepoSyncView:
    project_id: str
    project_slug: str
    repo: str
    is_primary: bool
    backfill_done: bool
    backfill_phase: str          # "issues" | "prs"
    backfill_cursor: str | None
    watermark: datetime | None   # aware UTC
    last_synced_at: datetime | None
    sync_requested_at: datetime | None
```

Seam signatures (all injected, like `SdlcIntakeLoop`):

```python
ReadConfig = Callable[[], Awaitable[tuple[str, dict]]]                # (mode, config)
ListRepos = Callable[[], Awaitable[list[RepoSyncView]]]
Fetch = Callable[[str], Awaitable[dict]]                              # GraphQL query → payload
UpsertItems = Callable[[list[WorkItemView]], Awaitable[None]]
SaveSyncState = Callable[..., Awaitable[None]]                       # (view, **updates)
ReadFeatureDocs = Callable[[str], Awaitable[list[tuple[str, str]]]]  # repo → [(path, text)]
ListFeatureRows = Callable[[str], Awaitable[list[FeatureRowView]]]   # project_id → rows
ApplyFeaturePlan = Callable[[str, FeatureIndexPlan, dict[str, "FeatureDoc"]], Awaitable[None]]
NowUtc = Callable[[], datetime]
```

- [ ] **Step 1: Write the failing tests**

Create `server/tests/test_pm_sync_loop.py`. Build the loop with dict-backed fake seams (read `server/tests/test_sdlc_intake_loop.py` first and reuse its fake-ctx idiom — a tiny object with an async `emit` and attribute stash):

```python
"""PmSyncLoop tests — backfill resumption, watermark rules, per-repo isolation."""
from datetime import datetime, timezone

import pytest

from server.automations.pm_sync import PmSyncLoop, RepoSyncView
from server.pm.feature_index import FeatureIndexPlan


class Ctx:
    def __init__(self):
        self.events = []

    async def emit(self, level, text, **kw):
        self.events.append((level, text, kw))


def _view(**over):
    base = dict(project_id="p1", project_slug="proj", repo="o/r", is_primary=True,
                backfill_done=False, backfill_phase="issues", backfill_cursor=None,
                watermark=None, last_synced_at=None, sync_requested_at=None)
    base.update(over)
    return RepoSyncView(**base)


def _issue_page(numbers, cursor, has_next):
    return {"data": {"repository": {"issues": {
        "pageInfo": {"endCursor": cursor, "hasNextPage": has_next},
        "nodes": [{"number": n, "title": f"i{n}", "body": "", "state": "OPEN",
                   "url": "", "createdAt": "2026-01-01T00:00:00Z",
                   "updatedAt": f"2026-01-0{min(n, 9)}T00:00:00Z", "closedAt": None,
                   "author": {"login": "t"}, "labels": {"nodes": []}}
                  for n in numbers]}}}}


def _pr_page(numbers, cursor, has_next, updated="2026-01-02T00:00:00Z"):
    return {"data": {"repository": {"pullRequests": {
        "pageInfo": {"endCursor": cursor, "hasNextPage": has_next},
        "nodes": [{"number": n, "title": f"p{n}", "body": "", "state": "OPEN",
                   "url": "", "createdAt": "2026-01-01T00:00:00Z",
                   "updatedAt": updated, "closedAt": None,
                   "author": {"login": "t"}, "labels": {"nodes": []},
                   "closingIssuesReferences": {"nodes": []}}
                  for n in numbers]}}}}


def _loop(ctx, views, pages, *, config=None, feature_docs=None):
    """Wire a loop over canned data. `pages` is a list popped per fetch call."""
    saved = []
    upserted = []
    applied = []

    async def read_config():
        return "on", dict(config or {})

    async def list_repos():
        return views

    async def fetch(query):
        return pages.pop(0)

    async def upsert(items):
        upserted.extend(items)

    async def save(view, **updates):
        saved.append((view.repo, updates))

    async def read_docs(repo):
        return list(feature_docs or [])

    async def list_rows(project_id):
        return []

    async def apply_plan(project_id, plan, docs_by_slug):
        applied.append((project_id, plan))

    loop = PmSyncLoop(
        ctx, read_config=read_config, list_repos=list_repos, fetch=fetch,
        upsert_items=upsert, save_sync_state=save, read_feature_docs=read_docs,
        list_feature_rows=list_rows, apply_feature_plan=apply_plan,
        now_utc=lambda: datetime(2026, 2, 1, tzinfo=timezone.utc))
    return loop, saved, upserted, applied


@pytest.mark.asyncio
async def test_backfill_saves_cursor_per_page_and_flips_done():
    ctx = Ctx()
    pages = [_issue_page([1, 2], "C1", True), _issue_page([3], None, False),
             _pr_page([4], None, False)]
    loop, saved, upserted, applied = _loop(ctx, [_view()], pages)
    await loop.tick()
    assert [n.number for n in upserted] == [1, 2, 3, 4]
    # cursor saved after the first issues page (resumable backfill)
    assert any(u.get("backfill_cursor") == "C1" for _, u in saved)
    # final save marks backfill done and stamps the watermark + last_synced_at
    final = saved[-1][1]
    assert final.get("backfill_done") is True
    assert final.get("watermark") is not None
    assert final.get("last_synced_at") is not None


@pytest.mark.asyncio
async def test_watermark_not_advanced_when_pass_fails():
    ctx = Ctx()

    class Boom(Exception):
        pass

    async def fetch(_q):
        raise Boom()

    loop, saved, upserted, applied = _loop(ctx, [_view()], [])
    loop._fetch = fetch  # replace seam with a failing one
    await loop.tick()
    assert all("watermark" not in u for _, u in saved)
    assert any(level == "warning" for level, _, _ in ctx.events)


@pytest.mark.asyncio
async def test_incremental_prs_stop_at_watermark():
    ctx = Ctx()
    wm = datetime(2026, 1, 15, tzinfo=timezone.utc)
    view = _view(backfill_done=True, watermark=wm, sync_requested_at=wm)
    pages = [_issue_page([], None, False),
             _pr_page([9], "C9", True, updated="2026-01-10T00:00:00Z")]  # older than wm
    loop, saved, upserted, applied = _loop(ctx, [view], pages)
    await loop.tick()
    # PR older than watermark: page consumed, but no further page requested
    assert pages == []


@pytest.mark.asyncio
async def test_one_repo_failure_does_not_stall_others():
    ctx = Ctx()
    ok_pages = [_issue_page([], None, False), _pr_page([], None, False)]
    views = [_view(repo="o/bad"), _view(repo="o/good", project_id="p2")]
    calls = {"n": 0}

    async def fetch(query):
        if '"bad"' in query:
            raise RuntimeError("api down")
        calls["n"] += 1
        return ok_pages.pop(0)

    loop, saved, upserted, applied = _loop(ctx, views, [])
    loop._fetch = fetch
    await loop.tick()
    assert calls["n"] == 2  # good repo fully synced despite bad repo failing


@pytest.mark.asyncio
async def test_interval_gate_skips_recent_unless_requested():
    ctx = Ctx()
    now = datetime(2026, 2, 1, tzinfo=timezone.utc)
    recent = _view(backfill_done=True, last_synced_at=now)
    loop, saved, upserted, applied = _loop(ctx, [recent], [],
                                           config={"min_sync_interval_s": 300})
    await loop.tick()
    assert saved == []  # skipped — nothing fetched, nothing saved
```

- [ ] **Step 2: Run to verify failure** — Expected: FAIL with ModuleNotFoundError.

- [ ] **Step 3: Implement**

Create `server/automations/pm_sync.py`:

```python
"""PM Sync — maintains the WorkItem mirror + Feature index (spec §4).

Same two-object mold as SdlcIntake: ``PmSync`` is the Automation contract
surface; ``PmSyncLoop`` is the supervised control loop with every I/O seam
injected. Sync rules: backfill is resumable (cursor saved per page), the
incremental watermark advances ONLY after a fully successful pass, and one
repo's failure never stalls the others.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable

from server.pm.feature_index import (
    FeatureIndexPlan, FeatureRowView, plan_feature_index)
from server.pm.featuredoc import FeatureDoc, FeatureDocError, parse_feature_doc
from server.pm.github_sync import (
    WorkItemView, build_issues_query, build_prs_query, parse_page)

from .base import AutomationStatus, ConfigField

log = logging.getLogger(__name__)

# Bound one tick's backfill work so a huge history spreads across ticks
# (the saved cursor makes each continuation cheap).
_MAX_PAGES_PER_TICK = 20


@dataclass(frozen=True)
class RepoSyncView:
    project_id: str
    project_slug: str
    repo: str
    is_primary: bool
    backfill_done: bool
    backfill_phase: str
    backfill_cursor: str | None
    watermark: datetime | None
    last_synced_at: datetime | None
    sync_requested_at: datetime | None


class PmSync:
    id = "pm-sync"
    name = "PM Sync"
    description = (
        "Mirrors onboarded projects' GitHub issues/PRs into the work-item index "
        "and refreshes the feature index from merged docs. Read-only toward "
        "GitHub; the mirror is disposable, GitHub stays the truth.")
    modes: tuple[str, ...] = ("off", "on")
    config_schema = [
        ConfigField("min_sync_interval_s", "Min sync interval (s)", "int", 300,
                    help="A repo is re-synced no more often than this unless "
                         "sync-now is requested."),
    ]

    def status(self, ctx: Any) -> AutomationStatus:
        counts = getattr(ctx, "pm_sync_counts", None) if ctx else None
        if not counts:
            return AutomationStatus(text="idle", level="idle")
        text = (f"{counts.get('synced', 0)} synced · "
                f"{counts.get('backfilling', 0)} backfilling · "
                f"{counts.get('errors', 0)} errors")
        level = "error" if counts.get("errors") else (
            "active" if counts.get("backfilling") else "idle")
        return AutomationStatus(text=text, level=level, counts=counts)

    async def on_state_change(self, *, enabled: bool, mode: str | None,
                              config: dict, ctx: Any) -> None:
        return None


ReadConfig = Callable[[], Awaitable[tuple[str, dict]]]
ListRepos = Callable[[], Awaitable[list[RepoSyncView]]]
Fetch = Callable[[str], Awaitable[dict]]
UpsertItems = Callable[[list[WorkItemView]], Awaitable[None]]
SaveSyncState = Callable[..., Awaitable[None]]
ReadFeatureDocs = Callable[[str], Awaitable[list[tuple[str, str]]]]
ListFeatureRows = Callable[[str], Awaitable[list[FeatureRowView]]]
ApplyFeaturePlan = Callable[[str, FeatureIndexPlan, dict[str, FeatureDoc]], Awaitable[None]]


class PmSyncLoop:
    """One ``tick()`` syncs every due repo: backfill pages (resumable) or an
    incremental pass, then the feature index for the project's primary repo."""

    def __init__(
        self,
        ctx: Any,
        *,
        read_config: ReadConfig,
        list_repos: ListRepos,
        fetch: Fetch,
        upsert_items: UpsertItems,
        save_sync_state: SaveSyncState,
        read_feature_docs: ReadFeatureDocs,
        list_feature_rows: ListFeatureRows,
        apply_feature_plan: ApplyFeaturePlan,
        now_utc: Callable[[], datetime],
    ) -> None:
        self._ctx = ctx
        self._read_config = read_config
        self._list_repos = list_repos
        self._fetch = fetch
        self._upsert_items = upsert_items
        self._save_sync_state = save_sync_state
        self._read_feature_docs = read_feature_docs
        self._list_feature_rows = list_feature_rows
        self._apply_feature_plan = apply_feature_plan
        self._now_utc = now_utc

    async def tick(self) -> None:
        try:
            await self._run()
        except Exception:  # pragma: no cover — belt+braces, mirrors the mold
            log.warning("pm_sync.tick_error", exc_info=True)

    async def _run(self) -> None:
        mode, config = await self._read_config()
        if mode == "off":
            self._stash(0, 0, 0)
            return
        interval = timedelta(seconds=int(config.get("min_sync_interval_s", 300)))
        now = self._now_utc()
        synced = backfilling = errors = 0
        for view in await self._list_repos():
            due = (view.sync_requested_at is not None
                   or view.last_synced_at is None
                   or now - view.last_synced_at >= interval
                   or not view.backfill_done)
            if not due:
                continue
            try:
                if view.backfill_done:
                    await self._incremental(view, now)
                    synced += 1
                else:
                    done = await self._backfill(view, now)
                    if done:
                        synced += 1
                    else:
                        backfilling += 1
            except Exception:
                errors += 1
                await self._ctx.emit(
                    "warning", f"sync failed for {view.repo}",
                    kind="sync_error", target=view.repo)
                log.warning("pm_sync.repo_error repo=%s", view.repo, exc_info=True)
        self._stash(synced, backfilling, errors)

    async def _backfill(self, view: RepoSyncView, now: datetime) -> bool:
        """Resumable full-history import: issues first, then PRs. Saves the
        cursor after every page; returns True when both phases complete."""
        phase = view.backfill_phase or "issues"
        cursor = view.backfill_cursor
        max_seen: datetime | None = None
        for _ in range(_MAX_PAGES_PER_TICK):
            if phase == "issues":
                payload = await self._fetch(build_issues_query(view.repo, cursor, None))
                items, cursor, more = parse_page(payload, view.repo, "issue")
            else:
                payload = await self._fetch(build_prs_query(view.repo, cursor))
                items, cursor, more = parse_page(payload, view.repo, "pr")
            if items:
                await self._upsert_items(items)
                for item in items:
                    if item.gh_updated_at and (max_seen is None or item.gh_updated_at > max_seen):
                        max_seen = item.gh_updated_at
            if not more:
                if phase == "issues":
                    phase, cursor = "prs", None
                    await self._save_sync_state(
                        view, backfill_phase=phase, backfill_cursor=None)
                    continue
                # Both phases exhausted — backfill complete.
                await self._save_sync_state(
                    view, backfill_done=True, backfill_cursor=None,
                    backfill_phase="prs", watermark=max_seen or now,
                    last_synced_at=now, sync_requested_at=None)
                await self._feature_index(view)
                await self._ctx.emit("info", f"backfill complete for {view.repo}",
                                     kind="backfill_done", target=view.repo)
                return True
            await self._save_sync_state(
                view, backfill_phase=phase, backfill_cursor=cursor)
        return False  # page budget exhausted; cursor saved, continues next tick

    async def _incremental(self, view: RepoSyncView, now: datetime) -> None:
        """One incremental pass. State is saved ONCE, after full success —
        a failure mid-pass leaves the watermark untouched (at-least-once)."""
        max_seen = view.watermark
        since = view.watermark.isoformat() if view.watermark else None
        cursor: str | None = None
        while True:  # issues: server-side `since` filter does the bounding
            payload = await self._fetch(build_issues_query(view.repo, cursor, since))
            items, cursor, more = parse_page(payload, view.repo, "issue")
            if items:
                await self._upsert_items(items)
                for item in items:
                    if item.gh_updated_at and (max_seen is None or item.gh_updated_at > max_seen):
                        max_seen = item.gh_updated_at
            if not more:
                break
        cursor = None
        while True:  # PRs: newest-first, stop once nodes fall behind the watermark
            payload = await self._fetch(build_prs_query(view.repo, cursor))
            items, cursor, more = parse_page(payload, view.repo, "pr")
            fresh = [i for i in items
                     if view.watermark is None
                     or (i.gh_updated_at and i.gh_updated_at > view.watermark)]
            if fresh:
                await self._upsert_items(fresh)
                for item in fresh:
                    if item.gh_updated_at and (max_seen is None or item.gh_updated_at > max_seen):
                        max_seen = item.gh_updated_at
            if not more or len(fresh) < len(items):
                break
        await self._feature_index(view)
        await self._save_sync_state(
            view, watermark=max_seen or now, last_synced_at=now,
            sync_requested_at=None)

    async def _feature_index(self, view: RepoSyncView) -> None:
        if not view.is_primary:
            return
        docs: list[tuple[str, FeatureDoc]] = []
        by_slug: dict[str, FeatureDoc] = {}
        for path, text in await self._read_feature_docs(view.repo):
            try:
                doc = parse_feature_doc(text)
            except FeatureDocError:
                log.warning("pm_sync.bad_feature_doc repo=%s path=%s",
                            view.repo, path, exc_info=True)
                continue  # one bad doc never poisons the index (spec §4)
            docs.append((path, doc))
            by_slug[doc.slug] = doc
        rows = await self._list_feature_rows(view.project_id)
        plan = plan_feature_index(rows, docs)
        await self._apply_feature_plan(view.project_id, plan, by_slug)

    def _stash(self, synced: int, backfilling: int, errors: int) -> None:
        self._ctx.pm_sync_counts = {
            "synced": synced, "backfilling": backfilling, "errors": errors}
```

Then in `server/automations/registry.py`, mirror the `SdlcIntake` import/registration lines:

```python
from .pm_sync import PmSync  # noqa: E402
registry.register(PmSync())
```

- [ ] **Step 4: Run tests** — `... -m pytest server/tests/test_pm_sync_loop.py -v` Expected: PASS (5 tests). Also run `... -m pytest server/tests/ -k registry -v` to confirm registration doesn't break registry tests.

- [ ] **Step 5: Commit**

```bash
git add server/automations/pm_sync.py server/automations/registry.py server/tests/test_pm_sync_loop.py
git commit -m "feat(pm): pm-sync automation loop (backfill/incremental/feature-index)"
```

---

### Task 9: Orchestrator wiring for pm-sync

**Files:**
- Modify: `server/sessions/orchestrator.py` (loop construction ~line 660 block; seam methods near the `_sdlc_*` seams ~line 2714+; task start/stop — grep `_sdlc_intake_task` and mirror EVERY occurrence)

**Interfaces:**
- Consumes: `PmSyncLoop`, `RepoSyncView` (Task 8); `Project`, `ProjectRepo`, `Feature`, `WorkItem`, `FeatureLink` models; `ccswitch.gitrepos` (`ensure_clone`, `_run_gh`).
- Produces: a running `pm-sync` loop in the daemon; seam methods `_pm_sync_*`.

- [ ] **Step 1: Read the existing wiring**

Read `server/sessions/orchestrator.py` lines 640–700 (loop construction) and 2714–2925 (`_sdlc_*` seams), plus every grep hit for `_sdlc_intake_task` (construction, start, cancel/stop). The pm-sync wiring mirrors each site exactly.

- [ ] **Step 2: Construct the loop (next to the sdlc loops, ~line 682)**

```python
        # PM Sync control loop (design 2026-08-31 §4): mirrors onboarded repos'
        # issues/PRs into work_items and refreshes the feature index. Same
        # injected-seam mold as the SDLC loops; GitHub I/O rides the gh seam.
        from server.automations.pm_sync import PmSyncLoop
        self._pm_sync_ctx = AutomationContext("pm-sync")
        self._pm_sync_loop = PmSyncLoop(
            self._pm_sync_ctx,
            read_config=self._pm_sync_read_config,
            list_repos=self._pm_sync_list_repos,
            fetch=self._pm_sync_fetch,
            upsert_items=self._pm_sync_upsert_items,
            save_sync_state=self._pm_sync_save_state,
            read_feature_docs=self._pm_sync_read_feature_docs,
            list_feature_rows=self._pm_sync_list_feature_rows,
            apply_feature_plan=self._pm_sync_apply_feature_plan,
            now_utc=lambda: datetime.now(timezone.utc),
        )
        self._pm_sync_task: asyncio.Task | None = None
```

(Check the file's existing imports for `datetime`/`timezone`; add if missing.)

- [ ] **Step 3: Implement the seams (next to the `_sdlc_*` seams)**

Copy the transaction idiom from `_sdlc_intake_read_config` / `_sdlc_list_initiatives` (they open `db_session()` context managers — reuse the exact same helper the sdlc seams use; do not invent a new one):

```python
    # ------------------------- pm-sync seams (design 2026-08-31) ----------- #

    async def _pm_sync_read_config(self) -> tuple[str, dict]:
        # Mirror _sdlc_intake_read_config exactly, with automation id "pm-sync"
        # and default mode "off". Copy that method's body and adjust the id.
        ...

    async def _pm_sync_list_repos(self) -> list["RepoSyncView"]:
        from server.automations.pm_sync import RepoSyncView
        from server.db.models import Project, ProjectRepo
        async with db_session() as db:  # same session helper as the sdlc seams
            rows = (await db.execute(
                select(ProjectRepo, Project.slug)
                .join(Project, Project.id == ProjectRepo.project_id)
            )).all()
        return [RepoSyncView(
            project_id=pr.project_id, project_slug=slug or "", repo=pr.repo or "",
            is_primary=bool(pr.is_primary), backfill_done=bool(pr.backfill_done),
            backfill_phase=pr.backfill_phase or "issues",
            backfill_cursor=pr.backfill_cursor, watermark=pr.watermark,
            last_synced_at=pr.last_synced_at, sync_requested_at=pr.sync_requested_at,
        ) for pr, slug in rows]

    async def _pm_sync_fetch(self, query: str) -> dict:
        import json
        from ccswitch import gitrepos
        proc = await asyncio.to_thread(
            gitrepos._run_gh, ["api", "graphql", "-f", f"query={query}"])
        if getattr(proc, "returncode", 1) != 0:
            raise RuntimeError(
                f"gh graphql failed: {(getattr(proc, 'stderr', '') or '').strip()[:500]}")
        return json.loads(getattr(proc, "stdout", "") or "{}")

    async def _pm_sync_upsert_items(self, items: list) -> None:
        from server.db.models import WorkItem
        async with db_session() as db:
            for item in items:
                existing = (await db.execute(
                    select(WorkItem).where(
                        WorkItem.repo == item.repo,
                        WorkItem.number == item.number,
                        WorkItem.kind == item.kind)
                )).scalar_one_or_none()
                now = datetime.now(timezone.utc)
                if existing is None:
                    existing = WorkItem(repo=item.repo, number=item.number,
                                        kind=item.kind)
                    db.add(existing)
                existing.title = item.title
                existing.body = item.body
                existing.state = item.state
                existing.labels = list(item.labels)
                existing.author = item.author
                existing.url = item.url
                existing.linked_issue_numbers = list(item.linked_issue_numbers)
                existing.gh_created_at = item.gh_created_at
                existing.gh_updated_at = item.gh_updated_at
                existing.gh_closed_at = item.gh_closed_at
                existing.last_synced_at = now

    async def _pm_sync_save_state(self, view, **updates) -> None:
        from server.db.models import ProjectRepo
        async with db_session() as db:
            row = (await db.execute(
                select(ProjectRepo).where(
                    ProjectRepo.project_id == view.project_id,
                    ProjectRepo.repo == view.repo)
            )).scalar_one_or_none()
            if row is None:
                return
            for key, value in updates.items():
                setattr(row, key, value)

    async def _pm_sync_read_feature_docs(self, repo: str) -> list[tuple[str, str]]:
        """List docs/product/features/*.md at origin's default branch in the
        shared clone. Uses git plumbing only — no GitHub API cost."""
        from ccswitch import gitrepos

        def _read() -> list[tuple[str, str]]:
            import subprocess
            clone = gitrepos.ensure_clone(repo)  # match the sdlc seams' call shape
            subprocess.run(["git", "-C", str(clone), "fetch", "origin", "--quiet"],
                           check=False, capture_output=True, timeout=120)
            head = subprocess.run(
                ["git", "-C", str(clone), "symbolic-ref", "--quiet",
                 "refs/remotes/origin/HEAD"],
                capture_output=True, text=True, timeout=30)
            ref = (head.stdout.strip() or "refs/remotes/origin/master")
            ls = subprocess.run(
                ["git", "-C", str(clone), "ls-tree", "-r", "--name-only", ref,
                 "docs/product/features/"],
                capture_output=True, text=True, timeout=30)
            out: list[tuple[str, str]] = []
            for path in ls.stdout.splitlines():
                if not path.endswith(".md"):
                    continue
                show = subprocess.run(
                    ["git", "-C", str(clone), "show", f"{ref}:{path}"],
                    capture_output=True, text=True, timeout=30)
                if show.returncode == 0:
                    out.append((path, show.stdout))
            return out

        return await asyncio.to_thread(_read)

    async def _pm_sync_list_feature_rows(self, project_id: str) -> list:
        from server.db.models import Feature
        from server.pm.feature_index import FeatureRowView
        async with db_session() as db:
            rows = (await db.execute(
                select(Feature).where(Feature.project_id == project_id)
            )).scalars().all()
        return [FeatureRowView(slug=f.slug or "", orphaned=bool(f.orphaned))
                for f in rows]

    async def _pm_sync_apply_feature_plan(self, project_id: str, plan, by_slug: dict) -> None:
        from server.db.models import Feature
        async with db_session() as db:
            for up in plan.upserts:
                row = (await db.execute(
                    select(Feature).where(Feature.project_id == project_id,
                                          Feature.slug == up.slug)
                )).scalar_one_or_none()
                if row is None:
                    row = Feature(project_id=project_id, slug=up.slug)
                    db.add(row)
                row.title = up.title
                row.state = up.state
                row.summary = up.summary
                row.repo_path = up.repo_path
                row.orphaned = False
            for slug in plan.orphan_slugs:
                row = (await db.execute(
                    select(Feature).where(Feature.project_id == project_id,
                                          Feature.slug == slug)
                )).scalar_one_or_none()
                if row is not None:
                    row.orphaned = True

    async def _pm_sync_tick_loop(self) -> None:
        # Copy the exact shape of _sdlc_intake_tick_loop (same sleep interval).
        ...
```

The two `...` bodies MUST be copied from their named sdlc counterparts in the same file (they are 5–10 lines each; copying keeps tick cadence and config-row semantics identical). NOTE: FeatureLink creation from doc frontmatter is deliberately NOT wired in phase 1 — links resolve across projects and the resolution rules belong to phase 4's surfaces work; the parser already captures them.

- [ ] **Step 4: Start/stop the task**

Grep `_sdlc_intake_task` across `orchestrator.py`. For each occurrence (creation site in `start()`, cancellation site in `stop()`/shutdown), add the identical line for `_pm_sync_task` / `_pm_sync_tick_loop` immediately after it.

- [ ] **Step 5: Verify**

Run: `ruff check server/ && mypy server/ --ignore-missing-imports` — both clean.
Run: `... -m pytest server/tests/ -x -q 2>&1 | tail -20` (full server suite, serialized) — no regressions.

- [ ] **Step 6: Commit**

```bash
git add server/sessions/orchestrator.py
git commit -m "feat(pm): wire pm-sync loop + seams into the orchestrator"
```

---

### Task 10: PM API router (`server/api/pm.py`)

**Files:**
- Create: `server/api/pm.py`
- Modify: `server/app.py` (import at ~line 46 area: `from server.api.pm import router as pm_router`; mount next to `app.include_router(sdlc_router)` at ~line 711: `app.include_router(pm_router)`)
- Test: `server/tests/test_pm_api.py` (create)

**Interfaces:**
- Consumes: models (Tasks 1–3), `record_audit`, `current_identity`, `get_db` (same imports as `server/api/sdlc.py`).
- Produces the phase-1 routes (spec §7 subset):
  - `POST /api/pm/projects` — body `{name, slug?, description?, portfolioTags?, repos: [{repo, isPrimary?}]}` → 201 project dict; 409 on slug conflict; 422 if `repos` empty
  - `GET /api/pm/projects` — list, each with `repos`, `edges`, `workItemCount`, `featureCount`
  - `GET /api/pm/projects/{slug}` — detail (404 when absent)
  - `POST /api/pm/projects/{slug}/sync` — stamps `sync_requested_at` on all the project's repos → `{"requested": true}` (202)
  - `POST /api/pm/projects/{slug}/edges` — body `{dstSlug, kind, note?}` → 201; 422 on unknown kind; 404 on unknown dst
  - `DELETE /api/pm/projects/{slug}/edges/{edge_id}` → 204

- [ ] **Step 1: Write the failing tests**

Create `server/tests/test_pm_api.py`. Read `server/tests/test_sdlc_api.py` FIRST and reuse its app/client fixtures verbatim (same auth strategy — the suite runs with `CCSWITCH_DEV=1` and an empty allowlist). Then:

```python
"""PM registry API tests (spec §7, phase-1 routes).

One narrated flow (create → list → sync-now → edges) plus edge cases, in the
style of test_sdlc_api.py — reuse that module's fixtures.
"""
import pytest


@pytest.mark.asyncio
async def test_project_lifecycle(client):
    # create
    resp = await client.post("/api/pm/projects", json={
        "name": "KinDash",
        "repos": [{"repo": "steiner385/KinDash", "isPrimary": True}],
    })
    assert resp.status_code == 201
    body = resp.json()
    assert body["slug"] == "kindash"
    assert body["onboardingState"] == "registered"

    # duplicate slug conflicts
    resp = await client.post("/api/pm/projects", json={
        "name": "KinDash", "repos": [{"repo": "x/y"}]})
    assert resp.status_code == 409

    # empty repos rejected
    resp = await client.post("/api/pm/projects", json={"name": "Norepo", "repos": []})
    assert resp.status_code == 422

    # list carries repos + counts
    resp = await client.get("/api/pm/projects")
    assert resp.status_code == 200
    (proj,) = resp.json()
    assert proj["repos"][0]["repo"] == "steiner385/KinDash"
    assert proj["workItemCount"] == 0 and proj["featureCount"] == 0

    # sync-now stamps the request
    resp = await client.post("/api/pm/projects/kindash/sync")
    assert resp.status_code == 202
    assert resp.json() == {"requested": True}

    # detail 404
    resp = await client.get("/api/pm/projects/nope")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_edges(client):
    for name in ("Alpha", "Beta"):
        resp = await client.post("/api/pm/projects", json={
            "name": name, "repos": [{"repo": f"o/{name.lower()}"}]})
        assert resp.status_code == 201

    resp = await client.post("/api/pm/projects/alpha/edges", json={
        "dstSlug": "beta", "kind": "depends_on"})
    assert resp.status_code == 201
    edge_id = resp.json()["id"]

    resp = await client.post("/api/pm/projects/alpha/edges", json={
        "dstSlug": "beta", "kind": "bogus"})
    assert resp.status_code == 422

    resp = await client.post("/api/pm/projects/alpha/edges", json={
        "dstSlug": "ghost", "kind": "depends_on"})
    assert resp.status_code == 404

    resp = await client.delete(f"/api/pm/projects/alpha/edges/{edge_id}")
    assert resp.status_code == 204
```

- [ ] **Step 2: Run to verify failure** — Expected: 404s (router not mounted) or ImportError.

- [ ] **Step 3: Implement the router**

Create `server/api/pm.py`:

```python
"""PM product-layer API — project registry + sync-now + edges (phase 1).

Design: docs/superpowers/specs/2026-08-31-pm-product-layer-design.md §7.
Mirrors server/api/sdlc.py conventions: camelCase JSON, audit on mutation,
slug identity, 409 on conflicts.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import func, select

from server.audit import record_audit
from server.auth.middleware import current_identity
from server.db import get_db
from server.db.models import (
    PROJECT_EDGE_KINDS,
    Feature,
    Project,
    ProjectEdge,
    ProjectRepo,
    WorkItem,
)

router = APIRouter(prefix="/api/pm", tags=["pm"])


def _slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return s or "project"


def _project_to_dict(p: Project) -> dict:
    return {
        "id": p.id,
        "slug": p.slug,
        "name": p.name,
        "description": p.description,
        "status": p.status,
        "portfolioTags": p.portfolio_tags or [],
        "onboardingState": p.onboarding_state,
        "createdAt": p.created_at.isoformat() if p.created_at else None,
        "updatedAt": p.updated_at.isoformat() if p.updated_at else None,
    }


def _repo_to_dict(r: ProjectRepo) -> dict:
    return {
        "id": r.id,
        "repo": r.repo,
        "isPrimary": bool(r.is_primary),
        "backfillDone": bool(r.backfill_done),
        "lastSyncedAt": r.last_synced_at.isoformat() if r.last_synced_at else None,
    }


def _edge_to_dict(e: ProjectEdge, slug_by_id: dict[str, str | None]) -> dict:
    return {
        "id": e.id,
        "srcSlug": slug_by_id.get(e.src_project_id),
        "dstSlug": slug_by_id.get(e.dst_project_id),
        "kind": e.kind,
        "note": e.note,
    }


class RepoInput(BaseModel):
    repo: str
    isPrimary: bool = False


class ProjectCreateBody(BaseModel):
    name: str
    slug: str | None = None
    description: str | None = None
    portfolioTags: list[str] | None = None
    repos: list[RepoInput]


class EdgeCreateBody(BaseModel):
    dstSlug: str
    kind: str
    note: str | None = None


async def _load_project(db, slug: str) -> Project:
    p = (await db.execute(
        select(Project).where(Project.slug == slug))).scalar_one_or_none()
    if p is None:
        raise HTTPException(status_code=404, detail={"error": "not_found"})
    return p


@router.post("/projects", status_code=status.HTTP_201_CREATED)
async def create_project(
    body: ProjectCreateBody,
    identity: str = Depends(current_identity),
    db=Depends(get_db),
) -> dict:
    if not body.repos:
        raise HTTPException(
            status_code=422,
            detail={"error": "no_repos", "message": "at least one repo is required"})
    slug = body.slug or _slugify(body.name)
    existing = (await db.execute(
        select(Project).where(Project.slug == slug))).scalar_one_or_none()
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": "slug_conflict", "message": f"Project '{slug}' already exists"})
    project = Project(slug=slug, name=body.name, description=body.description,
                      portfolio_tags=body.portfolioTags)
    db.add(project)
    await db.flush()
    # Exactly one primary: honour the first isPrimary=True, else the first repo.
    primary_idx = next(
        (i for i, r in enumerate(body.repos) if r.isPrimary), 0)
    for i, repo_in in enumerate(body.repos):
        db.add(ProjectRepo(project_id=project.id, repo=repo_in.repo,
                           is_primary=(i == primary_idx)))
    await db.flush()
    await record_audit(db=db, type="pm.project.create", identity=identity,
                       detail={"slug": slug, "name": body.name})
    return _project_to_dict(project)


@router.get("/projects")
async def list_projects(
    _identity: str = Depends(current_identity),
    db=Depends(get_db),
) -> list[dict]:
    projects = (await db.execute(
        select(Project).order_by(Project.created_at))).scalars().all()
    if not projects:
        return []
    ids = [p.id for p in projects]
    slug_by_id: dict[str, str | None] = {p.id: p.slug for p in projects}
    repos = (await db.execute(
        select(ProjectRepo).where(ProjectRepo.project_id.in_(ids))
    )).scalars().all()
    edges = (await db.execute(
        select(ProjectEdge).where(ProjectEdge.src_project_id.in_(ids))
    )).scalars().all()
    feature_counts = dict((await db.execute(
        select(Feature.project_id, func.count(Feature.id))
        .where(Feature.project_id.in_(ids)).group_by(Feature.project_id)
    )).all())
    repo_names_by_project: dict[str, list[str]] = {}
    for r in repos:
        repo_names_by_project.setdefault(r.project_id, []).append(r.repo or "")
    all_repo_names = [name for names in repo_names_by_project.values() for name in names]
    wi_counts: dict[str, int] = {}
    if all_repo_names:
        wi_counts = dict((await db.execute(
            select(WorkItem.repo, func.count(WorkItem.id))
            .where(WorkItem.repo.in_(all_repo_names)).group_by(WorkItem.repo)
        )).all())
    out = []
    for p in projects:
        d = _project_to_dict(p)
        d["repos"] = [_repo_to_dict(r) for r in repos if r.project_id == p.id]
        d["edges"] = [_edge_to_dict(e, slug_by_id) for e in edges
                      if e.src_project_id == p.id]
        d["featureCount"] = int(feature_counts.get(p.id, 0))
        d["workItemCount"] = sum(
            int(wi_counts.get(name, 0))
            for name in repo_names_by_project.get(p.id, []))
        out.append(d)
    return out


@router.get("/projects/{slug}")
async def get_project(
    slug: str,
    _identity: str = Depends(current_identity),
    db=Depends(get_db),
) -> dict:
    p = await _load_project(db, slug)
    repos = (await db.execute(
        select(ProjectRepo).where(ProjectRepo.project_id == p.id))).scalars().all()
    edges = (await db.execute(
        select(ProjectEdge).where(ProjectEdge.src_project_id == p.id))).scalars().all()
    dst_ids = {e.dst_project_id for e in edges} | {p.id}
    slug_rows = (await db.execute(
        select(Project.id, Project.slug).where(Project.id.in_(dst_ids)))).all()
    slug_by_id = dict(slug_rows)
    d = _project_to_dict(p)
    d["repos"] = [_repo_to_dict(r) for r in repos]
    d["edges"] = [_edge_to_dict(e, slug_by_id) for e in edges]
    return d


@router.post("/projects/{slug}/sync", status_code=status.HTTP_202_ACCEPTED)
async def request_sync(
    slug: str,
    identity: str = Depends(current_identity),
    db=Depends(get_db),
) -> dict:
    p = await _load_project(db, slug)
    now = datetime.now(timezone.utc)
    repos = (await db.execute(
        select(ProjectRepo).where(ProjectRepo.project_id == p.id))).scalars().all()
    for r in repos:
        r.sync_requested_at = now
    await db.flush()
    await record_audit(db=db, type="pm.project.sync_now", identity=identity,
                       detail={"slug": slug})
    return {"requested": True}


@router.post("/projects/{slug}/edges", status_code=status.HTTP_201_CREATED)
async def create_edge(
    slug: str,
    body: EdgeCreateBody,
    identity: str = Depends(current_identity),
    db=Depends(get_db),
) -> dict:
    if body.kind not in PROJECT_EDGE_KINDS:
        raise HTTPException(
            status_code=422,
            detail={"error": "bad_kind", "message": f"kind must be one of {PROJECT_EDGE_KINDS}"})
    src = await _load_project(db, slug)
    dst = await _load_project(db, body.dstSlug)
    edge = ProjectEdge(src_project_id=src.id, dst_project_id=dst.id,
                       kind=body.kind, note=body.note)
    db.add(edge)
    await db.flush()
    await record_audit(db=db, type="pm.edge.create", identity=identity,
                       detail={"src": slug, "dst": body.dstSlug, "kind": body.kind})
    return _edge_to_dict(edge, {src.id: src.slug, dst.id: dst.slug})


@router.delete("/projects/{slug}/edges/{edge_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_edge(
    slug: str,
    edge_id: str,
    identity: str = Depends(current_identity),
    db=Depends(get_db),
) -> None:
    src = await _load_project(db, slug)
    edge = (await db.execute(
        select(ProjectEdge).where(ProjectEdge.id == edge_id,
                                  ProjectEdge.src_project_id == src.id)
    )).scalar_one_or_none()
    if edge is None:
        raise HTTPException(status_code=404, detail={"error": "not_found"})
    await db.delete(edge)
    await record_audit(db=db, type="pm.edge.delete", identity=identity,
                       detail={"src": slug, "edgeId": edge_id})
```

Mount in `server/app.py`: add `from server.api.pm import router as pm_router` beside the sdlc import (line ~46) and `app.include_router(pm_router)` beside `app.include_router(sdlc_router)` (line ~711).

- [ ] **Step 4: Run tests** — `... -m pytest server/tests/test_pm_api.py -v` Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add server/api/pm.py server/app.py server/tests/test_pm_api.py
git commit -m "feat(pm): project registry API (create/list/detail/sync-now/edges)"
```

---

### Task 11: Web — Products tab (registry UI)

**Files:**
- Create: `web/src/api/pm.ts`
- Create: `web/src/projects/ProjectsTab.tsx`
- Create: `web/src/projects/projects.css`
- Modify: `web/src/api/hooks.ts` (append PM hooks; add `projects` key to the `QK` object — read its shape first and follow it)
- Modify: `web/src/components/Dashboard.tsx` (`TabId` union at line 466 gains `'projects'`; add the tab panel beside the `initiatives` one at ~line 914)
- Modify: `web/src/components/tabDefs.ts` (add `{ id: 'projects', label: 'Products' }` after the initiatives entry, line 23)
- Modify: `web/src/components/DesktopShell.tsx` (add the `tab === 'projects'` branch beside `initiatives` at ~line 541)
- Modify: `web/src/App.tsx` (add `<Route path="/projects" element={<DashboardRoute tab="projects" />} />` beside line 201)
- Test: `web/src/projects/__tests__/ProjectsTab.test.tsx` (create)

**Interfaces:**
- Consumes: `api` client (`web/src/api/client.ts`), React Query, the Task 10 routes.
- Produces: `ProjectsTab` component; `useProjects`, `useCreateProject`, `useSyncProject` hooks; `Project`/`ProjectRepoInfo`/`ProjectEdgeInfo`/`CreateProjectInput` types.

- [ ] **Step 1: Write the API module**

Create `web/src/api/pm.ts`:

```typescript
/**
 * PM product-layer API client — types + fetchers over `/api/pm` (phase 1).
 * Mirrors `api/sdlc.ts`: plain fetchers here, hooks in `api/hooks.ts`.
 */
import { api } from './client'

export const PROJECT_ONBOARDING_STATES = [
  'registered', 'syncing', 'map_proposed', 'map_merged', 'classifying', 'live',
] as const
export type OnboardingState = (typeof PROJECT_ONBOARDING_STATES)[number]

export const PROJECT_EDGE_KINDS = ['depends_on', 'deploys_via', 'integrates_with'] as const
export type EdgeKind = (typeof PROJECT_EDGE_KINDS)[number]

export interface ProjectRepoInfo {
  id: string
  repo: string | null
  isPrimary: boolean
  backfillDone: boolean
  lastSyncedAt: string | null
}

export interface ProjectEdgeInfo {
  id: string
  srcSlug: string | null
  dstSlug: string | null
  kind: EdgeKind | string | null
  note: string | null
}

export interface Project {
  id: string
  slug: string | null
  name: string | null
  description: string | null
  status: string | null
  portfolioTags: string[]
  onboardingState: OnboardingState | string | null
  createdAt: string | null
  updatedAt: string | null
  repos: ProjectRepoInfo[]
  edges: ProjectEdgeInfo[]
  featureCount: number
  workItemCount: number
}

export interface CreateProjectInput {
  name: string
  slug?: string
  description?: string
  repos: { repo: string; isPrimary?: boolean }[]
}

export function fetchProjects(): Promise<Project[]> {
  return api.get<Project[]>('/pm/projects')
}

export function createProject(input: CreateProjectInput): Promise<Project> {
  return api.post<Project>('/pm/projects', input)
}

export function requestProjectSync(slug: string): Promise<{ requested: boolean }> {
  return api.post<{ requested: boolean }>(`/pm/projects/${encodeURIComponent(slug)}/sync`)
}
```

- [ ] **Step 2: Add hooks**

In `web/src/api/hooks.ts`, add `projects: ['pm', 'projects'] as const` to the `QK` object (match its existing entry style exactly), then append near the initiative hooks:

```typescript
/** Projects registry list. Polled like initiatives (baseline liveness). */
export function useProjects() {
  return useQuery<Project[]>({
    queryKey: QK.projects,
    queryFn: fetchProjects,
    staleTime: 5_000,
    refetchInterval: 15_000,
  })
}

export function useCreateProject() {
  const qc = useQueryClient()
  return useMutation<Project, ApiRequestError, CreateProjectInput>({
    mutationFn: (input) => createProject(input),
    onSuccess: () => void qc.invalidateQueries({ queryKey: QK.projects }),
    onError: (e) => toast.error(initiativeErrorMessage(e, 'Could not create project')),
  })
}

export function useSyncProject() {
  const qc = useQueryClient()
  return useMutation<{ requested: boolean }, ApiRequestError, string>({
    mutationFn: (slug) => requestProjectSync(slug),
    onSuccess: () => void qc.invalidateQueries({ queryKey: QK.projects }),
    onError: (e) => toast.error(initiativeErrorMessage(e, 'Could not request sync')),
  })
}
```

Add the imports (`Project`, `CreateProjectInput`, `createProject`, `fetchProjects`, `requestProjectSync` from `./pm`) to the hooks file's import block.

- [ ] **Step 3: Write the failing component test**

Create `web/src/projects/__tests__/ProjectsTab.test.tsx`. Read `web/src/initiatives/` for the existing test idiom first (if `InitiativesTab` has tests, mirror their render harness — QueryClientProvider wrapper + mocked fetchers via `vi.mock`):

```tsx
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import { ProjectsTab } from '../ProjectsTab'

vi.mock('@/api/pm', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/pm')>()),
  fetchProjects: vi.fn().mockResolvedValue([{
    id: '1', slug: 'kindash', name: 'KinDash', description: null, status: 'active',
    portfolioTags: [], onboardingState: 'registered',
    createdAt: null, updatedAt: null,
    repos: [{ id: 'r1', repo: 'steiner385/KinDash', isPrimary: true,
              backfillDone: false, lastSyncedAt: null }],
    edges: [], featureCount: 0, workItemCount: 12,
  }]),
}))

function renderTab() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <ProjectsTab />
    </QueryClientProvider>,
  )
}

describe('ProjectsTab', () => {
  it('renders project cards with repo, state, and counts', async () => {
    renderTab()
    await waitFor(() => expect(screen.getByText('KinDash')).toBeInTheDocument())
    expect(screen.getByText('steiner385/KinDash')).toBeInTheDocument()
    expect(screen.getByText(/registered/i)).toBeInTheDocument()
    expect(screen.getByText(/12 work items/i)).toBeInTheDocument()
  })

  it('shows the empty state when there are no projects', async () => {
    const pm = await import('@/api/pm')
    vi.mocked(pm.fetchProjects).mockResolvedValueOnce([])
    renderTab()
    await waitFor(() =>
      expect(screen.getByText(/no projects yet/i)).toBeInTheDocument())
  })
})
```

If the hooks import fetchers directly (they do), the `vi.mock('@/api/pm', …)` path interception works because hooks resolve the same module. Verify the alias `@/` is configured in `vitest.config`/`tsconfig` (it is — `InitiativesTab` imports use it).

- [ ] **Step 4: Run to verify failure** — In `web/`: `npx vitest run src/projects` Expected: FAIL (module not found).

- [ ] **Step 5: Implement the tab**

Create `web/src/projects/ProjectsTab.tsx` (read `web/src/initiatives/InitiativesTab.tsx` first and mirror its structure — modal form pattern, css import, list layout):

```tsx
/**
 * Products registry (PM product layer, phase 1): project cards + New Project
 * form + sync-now. Deliberately minimal — the onboarding wizard, board, and
 * inbox land in later phases (design 2026-08-31 §10).
 */
import { useState } from 'react'

import { useCreateProject, useProjects, useSyncProject } from '@/api/hooks'
import type { Project } from '@/api/pm'

import './projects.css'

function NewProjectForm({ onClose }: { onClose: () => void }) {
  const create = useCreateProject()
  const [name, setName] = useState('')
  const [repo, setRepo] = useState('')
  const canSubmit = name.trim() !== '' && /^[^/\s]+\/[^/\s]+$/.test(repo.trim())
  return (
    <form
      className="pm-new-project"
      onSubmit={(e) => {
        e.preventDefault()
        if (!canSubmit) return
        create.mutate(
          { name: name.trim(), repos: [{ repo: repo.trim(), isPrimary: true }] },
          { onSuccess: onClose },
        )
      }}
    >
      <label>
        Name
        <input value={name} onChange={(e) => setName(e.target.value)}
               placeholder="KinDash" />
      </label>
      <label>
        Primary repo
        <input value={repo} onChange={(e) => setRepo(e.target.value)}
               placeholder="owner/repo" />
      </label>
      <div className="pm-form-actions">
        <button type="button" onClick={onClose}>Cancel</button>
        <button type="submit" disabled={!canSubmit || create.isPending}>
          {create.isPending ? 'Creating…' : 'Add project'}
        </button>
      </div>
    </form>
  )
}

function ProjectCard({ project }: { project: Project }) {
  const sync = useSyncProject()
  return (
    <div className="pm-project-card">
      <div className="pm-project-head">
        <h3>{project.name ?? project.slug}</h3>
        <span className={`pm-state pm-state-${project.onboardingState ?? 'registered'}`}>
          {project.onboardingState ?? 'registered'}
        </span>
      </div>
      <ul className="pm-repos">
        {project.repos.map((r) => (
          <li key={r.id}>
            {r.repo}
            {r.isPrimary && <span className="pm-primary-chip">primary</span>}
            {!r.backfillDone && <span className="pm-backfill-chip">backfilling</span>}
          </li>
        ))}
      </ul>
      <div className="pm-counts">
        <span>{project.workItemCount} work items</span>
        <span>{project.featureCount} features</span>
      </div>
      <div className="pm-card-actions">
        <button
          onClick={() => project.slug && sync.mutate(project.slug)}
          disabled={sync.isPending || !project.slug}
        >
          {sync.isPending ? 'Requesting…' : 'Sync now'}
        </button>
      </div>
    </div>
  )
}

export function ProjectsTab() {
  const { data: projects, isLoading } = useProjects()
  const [showForm, setShowForm] = useState(false)
  return (
    <div className="pm-projects">
      <div className="pm-projects-toolbar">
        <h2>Products</h2>
        <button onClick={() => setShowForm(true)}>New project</button>
      </div>
      {showForm && <NewProjectForm onClose={() => setShowForm(false)} />}
      {isLoading && <p className="pm-muted">Loading…</p>}
      {!isLoading && (projects ?? []).length === 0 && (
        <p className="pm-muted">
          No projects yet. Add one to start mirroring its backlog.
        </p>
      )}
      <div className="pm-project-grid">
        {(projects ?? []).map((p) => <ProjectCard key={p.id} project={p} />)}
      </div>
    </div>
  )
}
```

Create `web/src/projects/projects.css` with minimal styling (grid of cards, state chip colors) — follow the variable/token usage in `web/src/initiatives/initiatives.css` (reuse its custom properties; do not invent new color literals).

- [ ] **Step 6: Wire the tab**

Apply the four wiring edits listed under **Files** (TabId union, tabDefs entry, Dashboard panel, DesktopShell branch, App route). For Dashboard.tsx, copy the `initiatives` panel block at ~line 914 and adjust ids (`tabpanel-projects` / `tab-projects`); for DesktopShell.tsx copy the `tab === 'initiatives'` branch. Import `ProjectsTab` in both.

- [ ] **Step 7: Run the web gate**

In `web/`: `npx vitest run` · `npx tsc --noEmit` · `npx eslint .`
Expected: all clean (the ArchivedTab tab-navigation test at `web/src/components/__tests__/ArchivedTab.test.tsx:201` walks the tab list — if it asserts a fixed tab order/count, update its expectation to include Products).

- [ ] **Step 8: Commit**

```bash
git add web/src/api/pm.ts web/src/api/hooks.ts web/src/projects/ web/src/components/Dashboard.tsx web/src/components/tabDefs.ts web/src/components/DesktopShell.tsx web/src/App.tsx web/src/components/__tests__/ArchivedTab.test.tsx
git commit -m "feat(pm): Products tab — project registry cards + create + sync-now"
```

---

### Task 12: Full gate, PR

- [ ] **Step 1: Run the complete four-step Python gate** (serialized, from the repo root, with the env prefix):

```bash
ruff check src/ server/
mypy server/ --ignore-missing-imports
... -m pytest tests/ -q
... -m pytest server/tests/ -q
```

Expected: all four green. If `CCSWITCH_TEST_PG_URL` is set, also `... -m pytest server/tests/test_postgres_compat.py -q`.

- [ ] **Step 2: Run the web gate** (in `web/`): `npx vitest run && npx tsc --noEmit && npx eslint .`

- [ ] **Step 3: Check for in-flight PRs touching the same files**

```bash
gh api graphql -f query='query { repository(owner: "steiner385", name: "ccswitch") { pullRequests(states: OPEN, first: 20) { nodes { number title files(first: 50) { nodes { path } } } } } }'
```

If any open PR touches `server/db/models.py`, `server/sessions/orchestrator.py`, `server/app.py`, or the web tab files, note it in the PR body and rebase after it merges.

- [ ] **Step 4: Push and open the PR**

```bash
git push -u origin feat/pm-product-layer-phase1
gh pr create --title "feat(pm): product layer phase 1 — domain model, pm-sync mirror, project registry" --body "$(cat <<'EOF'
Phase 1 of the PM product layer (design: docs/superpowers/specs/2026-08-31-pm-product-layer-design.md).

- Domain model: Project/ProjectRepo/ProjectEdge, Feature/FeatureLink (index rows over docs/product/features/*.md), WorkItem mirror + overlay, TriageProposal, Initiative PM extensions (project link, repo/feature M2M, RICE-lite scores). Alembic 0012 + PG-suite coverage.
- pm-sync automation: resumable GraphQL backfill (issues+PRs incl. closed history), incremental sync with watermark-advances-only-on-success, feature-doc frontmatter indexing from the clone. All I/O behind injected seams, per the SdlcIntake mold.
- API: /api/pm project registry (create/list/detail/sync-now/edges), audit-logged.
- Web: Products tab with project cards, New Project form, sync-now.

Later phases (archaeology onboarding, triage loop + inbox, board/roadmap) build on this.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
gh pr merge --squash --auto
```

- [ ] **Step 5: Confirm** the PR is non-draft with auto-merge armed:

```bash
gh api graphql -f query='query { repository(owner: "steiner385", name: "ccswitch") { pullRequest(number: <N>) { isDraft autoMergeRequest { enabledAt } } } }'
```

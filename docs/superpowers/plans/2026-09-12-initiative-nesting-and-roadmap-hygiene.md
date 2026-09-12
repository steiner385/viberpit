# Initiative Nesting and Roadmap Hygiene Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Mirror GitHub's epic nesting onto initiatives (program → phases) with honest roll-ups, and close the four ccswitch gaps that let the KinDash roadmap decay: a hand-merged map that never advances onboarding, agent prompts that never write `parent:`, bot-filed issues that invent capabilities, and no readout of roadmap drift.

**Architecture:** One nullable self-link on `Initiative` set by epic adoption from the mirrored `WorkItem.parent_work_item_id`; a pure roll-up in `server/pm/tree.py` that sums leaf-descendant epic progress and derives a program's stage and tier from its phases; the web nests by `parentInitiative` the way it already nests capabilities. Label routing is a `labels:` frontmatter list on the feature doc (the repo owns the rule), stored on `Feature.route_labels`, applied in pm-triage's mechanical pass. Everything stays propose-only and human-gated.

**Tech Stack:** Python 3 / FastAPI / SQLAlchemy (SQLite tests, Postgres prod) · React + TypeScript + Vitest · pytest.

**Spec:** `docs/superpowers/specs/2026-09-12-kindash-roadmap-5s-plan.md` (Standardize items 1–3, Sustain), on top of `docs/superpowers/specs/2026-09-07-roadmap-drilldown-design.md` (D2, §10–§11).

## Global Constraints

- Every new column is **nullable** (reconcile picks it up on both dialects); still add a migration revision chained off `0040_feature_map_changes`.
- Never `func.now()`; naive UTC via `datetime.now(timezone.utc)` / `server/db/clock.py`.
- No second implementation of a concept: slugs via `server/sdlc/slug.py`, tiers via `server/pm/tree.py`, project ownership via `server/pm/repo_project.py`.
- Programs are never edited directly: advance, step-back and GitHub Stage back-sync apply to leaf initiatives only. A program's stage and tier are derived.
- Counts (`inFlight`, `blocked`, `needsYou`) are bumped by leaf initiatives only; programs contribute tier, never counts, so a program and its phases are not double-counted.
- Web tasks (12–14) start only after PR #1590 (`feat/finish-line-row-8c-map-changes-web`) has merged; rebase onto `origin/master` first. Server tasks do not wait.
- The four-step gate before every push: `ruff check src/ server/`, `mypy server/ --ignore-missing-imports`, `pytest tests/`, `pytest server/tests/ -m "not slow"` (full suite in CI). Web: `npm run typecheck`, `npm run lint`, `npm run test`, `npm run build` in `web/` via `(cd web && …)` subshells.
- Run server tests with `env -u CCSWITCH_TAILNET_ALLOWLIST -u CCSWITCH_SESSIOND_CERT -u CCSWITCH_SESSIOND_KEY -u CCSWITCH_DAEMON_ROLE CCSWITCH_DEV=1 <main-checkout>/.venv/bin/python -m pytest …`.
- Branch off `origin/master` (`git fetch && git checkout -b <branch> origin/master`), never local `master`. Commit messages and PR bodies via `-F` files; end commits with `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.

---

## File structure

| File | Responsibility |
|---|---|
| `server/db/models.py` | `Initiative.parent_initiative_id`, `Feature.route_labels` |
| `server/db/migrations/versions/0041_initiative_nesting_route_labels.py` | reviewable record of both columns |
| `server/sdlc/slug.py` | `normalize_epic_title` beside `slugify` (one naming module) |
| `server/pm/mirror_store.py` | `adopt_epics` uses the normalized title; new `link_epic_parents`; `back_sync_stage` skips programs |
| `server/pm/tree.py` | `rollup_epic_progress`, `derived_stage`, program-aware `initiative_tier`/`initiative_reason`, `InitiativeIn.parent_slug`, leaf-only count bumps |
| `server/api/sdlc.py` | `_decorate` adds `parentInitiativeSlug`, `children`, derived program stage; roll-up in list/detail; 409 `has_children` on advance/step-back |
| `server/api/pm_tree.py` | passes `parent_slug` into `InitiativeIn` |
| `server/sessions/orchestrator.py` | `_pm_sync_apply_feature_plan` advances `registered`/`syncing` → `map_merged`; writes `route_labels`; `_pm_triage_auto_link` label route |
| `server/automations/pm_triage.py` | the mechanical pass also offers labelled items to `auto_link` |
| `server/pm/featuredoc.py`, `server/pm/feature_index.py` | `labels:` frontmatter → `FeatureDoc.labels` → `FeatureUpsert.labels` |
| `server/pm/classification.py` | synthesis + critic prompts write `parent:`, `labels:` and a leading outcome sentence |
| `server/pm/health.py`, `server/api/pm.py` | `roadmap_health` (pure) + `GET /api/pm/health` |
| `web/src/api/roadmap.ts`, `web/src/api/sdlc.ts` | `parentInitiative`, `parentInitiativeSlug`, `children` |
| `web/src/features/roadmap/treeRows.ts` | program rows nest their phases |
| `web/src/features/roadmap/rows/InitiativeCard.tsx` | chevron for a program; program chip for a phase |
| `web/src/features/roadmap/detail/InitiativeList.tsx`, `detail/CapabilityDetail.tsx` | nested in-flight list |
| `web/src/features/roadmap/InitiativePage.tsx` | Phases tab for a program; "Part of" link for a phase |
| `web/src/features/roadmap/detail/RoadmapHealthCard.tsx`, `detail/ProductDetail.tsx` | health readout |

---

### Task 1: Columns and migration

**Files:**
- Modify: `server/db/models.py:1220-1258` (Initiative), `server/db/models.py:1493-1526` (Feature)
- Create: `server/db/migrations/versions/0041_initiative_nesting_route_labels.py`
- Test: `server/tests/test_initiative_models.py`

**Interfaces:**
- Produces: `Initiative.parent_initiative_id: str | None` (self FK, indexed); `Feature.route_labels: list[str] | None` (JSON).

- [ ] **Step 1: Write the failing tests** (append to `server/tests/test_initiative_models.py`)

```python
def test_initiative_parent_link_roundtrips(tmp_path):
    """A phase initiative points at its program by id; a root has None."""
    from sqlalchemy import select as sa_select

    from server.db import db_session, init_db
    from server.db.models import Initiative

    async def run():
        await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / 'sdlc.db'}")
        async with db_session() as db:
            program = Initiative(slug="ui-audit-2026-09", title="UI audit 2026-09")
            db.add(program)
            await db.flush()
            db.add(Initiative(slug="ui-audit-2026-09-01", title="01 Restore core actions",
                              parent_initiative_id=program.id))
            program_id = program.id
        async with db_session() as db:
            phase = (await db.execute(sa_select(Initiative).where(
                Initiative.slug == "ui-audit-2026-09-01"))).scalars().one()
            root = (await db.execute(sa_select(Initiative).where(
                Initiative.slug == "ui-audit-2026-09"))).scalars().one()
            return program_id, phase.parent_initiative_id, root.parent_initiative_id

    program_id, phase_parent, root_parent = asyncio.run(run())
    assert phase_parent == program_id
    assert root_parent is None


def test_feature_route_labels_roundtrip_as_a_list(tmp_path):
    from sqlalchemy import select as sa_select

    from server.db import db_session, init_db
    from server.db.models import Feature, Project

    async def run():
        await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / 'pm.db'}")
        async with db_session() as db:
            p = Project(slug="kindash", name="KinDash")
            db.add(p)
            await db.flush()
            db.add(Feature(project_id=p.id, slug="flaky-test-triage", title="Flaky test triage",
                           state="live", route_labels=["flaky"]))
            db.add(Feature(project_id=p.id, slug="task-views", title="Task views", state="live"))
        async with db_session() as db:
            rows = (await db.execute(sa_select(Feature).order_by(Feature.slug))).scalars().all()
            return [(r.slug, r.route_labels) for r in rows]

    assert asyncio.run(run()) == [("flaky-test-triage", ["flaky"]), ("task-views", None)]
```

- [ ] **Step 2: Run them to verify they fail**

Run: `python -m pytest server/tests/test_initiative_models.py -v`
Expected: FAIL with `TypeError: 'parent_initiative_id' is an invalid keyword argument` (and the same for `route_labels`).

- [ ] **Step 3: Add the columns**

In `server/db/models.py`, inside `class Initiative`, after the `project_id` column (line ~1252):

```python
    # Program → phase nesting, mirrored from GitHub's sub-issue parent when the
    # parent is itself an adopted epic (roadmap 5S plan, Standardize 3). NULL =
    # a root initiative. Programs are derived nodes: their stage and tier come
    # from their phases and they are never advanced or back-synced directly.
    parent_initiative_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("initiatives.id"), nullable=True, index=True)
```

Inside `class Feature`, after `summary`:

```python
    # `labels:` frontmatter — GitHub labels that route a work item onto this
    # capability mechanically (pm-triage's auto-link pass), so bot-filed issues
    # (`flaky`, `forced-deploy-audit`, …) never reach the classifier and never
    # cluster into a proposed capability. The doc owns the rule; this mirrors it.
    route_labels: Mapped[list | None] = mapped_column(JSON, nullable=True, default=None)
```

- [ ] **Step 4: Write the migration** `server/db/migrations/versions/0041_initiative_nesting_route_labels.py`

```python
"""Initiative nesting + feature route labels (roadmap 5S plan, 2026-09-12).

Nothing runs `alembic upgrade` at deploy time; both columns are nullable, so
init_db's `_reconcile_columns` ALTER-ADDs them on SQLite and Postgres at next
boot. This revision is the reviewable record of the change.

Revision ID: 0041_initiative_nesting_route_labels
Revises: 0040_feature_map_changes
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0041_initiative_nesting_route_labels"
down_revision = "0040_feature_map_changes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("initiatives", sa.Column(
        "parent_initiative_id", sa.String(), sa.ForeignKey("initiatives.id"), nullable=True))
    op.create_index("ix_initiatives_parent_initiative_id", "initiatives", ["parent_initiative_id"])
    op.add_column("features", sa.Column("route_labels", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("features", "route_labels")
    op.drop_index("ix_initiatives_parent_initiative_id", table_name="initiatives")
    op.drop_column("initiatives", "parent_initiative_id")
```

- [ ] **Step 5: Run the tests and the schema guards**

Run: `python -m pytest server/tests/test_initiative_models.py server/tests/test_schema_reconcile_coverage.py server/tests/test_postgres_compat.py -v`
Expected: PASS (the Postgres suite skips without `CCSWITCH_TEST_PG_URL`; the reconcile guard passes because both columns are nullable).

- [ ] **Step 6: Commit**

```bash
git add server/db/models.py server/db/migrations/versions/0041_initiative_nesting_route_labels.py server/tests/test_initiative_models.py
git commit -F /tmp/c1.txt   # "feat(pm): Initiative.parent_initiative_id + Feature.route_labels (nullable, reconciled)"
```

---

### Task 2: `normalize_epic_title`

**Files:**
- Modify: `server/sdlc/slug.py`
- Create: `server/tests/test_sdlc_slug.py`

**Interfaces:**
- Produces: `normalize_epic_title(title: str) -> str` — strips a leading conventional-commit epic prefix (`epic(scope):`, `epic:`, `Epic:`, `program(scope):`), collapses whitespace, returns the original stripped title when the result would be empty.

- [ ] **Step 1: Write the failing test** `server/tests/test_sdlc_slug.py`

```python
import pytest

from server.sdlc.slug import normalize_epic_title, slugify


@pytest.mark.parametrize("raw,expected", [
    ("epic(ui-audit): 04 — Repair web widgets", "04 — Repair web widgets"),
    ("Epic: ViberPit UI E3 — Browser trust", "ViberPit UI E3 — Browser trust"),
    ("epic(tasks): use-case-first task views", "use-case-first task views"),
    ("program(web): 2026 benchmark", "2026 benchmark"),
    ("Plain title with   spaces", "Plain title with spaces"),
    ("epic:", "epic:"),          # nothing left → keep the original
    ("  padded  ", "padded"),
])
def test_normalize_epic_title(raw, expected):
    assert normalize_epic_title(raw) == expected


def test_slugify_of_a_normalized_title_has_no_epic_prefix():
    assert slugify(normalize_epic_title("epic(ui-audit): 04 — Repair web widgets")) == "04-repair-web-widgets"
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python -m pytest server/tests/test_sdlc_slug.py -v`
Expected: FAIL with `ImportError: cannot import name 'normalize_epic_title'`.

- [ ] **Step 3: Implement** (append to `server/sdlc/slug.py`)

```python
_EPIC_PREFIX = re.compile(r"^\s*(?:epic|program)\s*(?:\([^)]*\))?\s*:\s*", re.IGNORECASE)
_WS = re.compile(r"\s+")


def normalize_epic_title(title: str) -> str:
    """An initiative title from a GitHub epic title: the conventional-commit
    ``epic(scope):`` prefix is a GitHub filing convention, not a name, and
    slugifying it (``epic-ui-audit-04-…``) leaked it into every initiative slug.
    Whitespace is collapsed. An empty remainder keeps the original (stripped)
    so a title can never become blank."""
    stripped = _WS.sub(" ", title).strip()
    out = _WS.sub(" ", _EPIC_PREFIX.sub("", stripped, count=1)).strip()
    return out or stripped
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest server/tests/test_sdlc_slug.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add server/sdlc/slug.py server/tests/test_sdlc_slug.py
git commit -F /tmp/c2.txt   # "feat(sdlc): normalize_epic_title strips the epic(scope): filing prefix"
```

---

### Task 3: Adoption links parents and uses the normalized title

**Files:**
- Modify: `server/pm/mirror_store.py:180-255` (`adopt_epics`), `:158-177` (`back_sync_stage`)
- Test: `server/tests/test_mirror_store.py` (append after `test_back_sync_stage_updates_adopted_initiative`)

**Interfaces:**
- Consumes: `normalize_epic_title` (Task 2), `Initiative.parent_initiative_id` (Task 1).
- Produces: `link_epic_parents(db, project_id) -> int` (initiatives whose parent link changed); `adopt_epics` calls it last; `back_sync_stage` returns without writing for an initiative that has children.

- [ ] **Step 1: Write the failing tests**

```python
def _seed_program(db, project, *, program=945, phases=(950, 951)):
    """A program epic whose sub-issues are themselves epics, all on the board."""
    gid = _seed_board_epic(db, project, number=program,
                           title="epic(ui-audit): September 2026 audit")
    parent = db.query(WorkItem).filter_by(number=program).one()
    for i, n in enumerate(phases, start=1):
        wi = db.query(WorkItem).filter_by(number=n).one()
        wi.is_epic = True
        wi.title = f"epic(ui-audit): 0{i} — Phase {i}"
        wi.state = "open"
        wi.parent_work_item_id = parent.id
        db.add(GhProjectItem(gh_project_id=gid, gh_item_node_id=f"PVTI_{n}",
                             work_item_id=wi.id, stage="design"))
    db.commit()


def test_adopt_epics_links_phases_to_their_program_and_strips_the_prefix():
    db = _db()
    project = _seed(db)
    _seed_program(db, project)

    refs = mirror_store.adopt_epics(db, project.id, now=ADOPT_NOW)

    assert set(refs) == {f"{REPO}#945", f"{REPO}#950", f"{REPO}#951"}
    by_number = {i.epic_number: i for i in db.query(Initiative).all()}
    program, p1, p2 = by_number[945], by_number[950], by_number[951]
    assert program.parent_initiative_id is None
    assert p1.parent_initiative_id == program.id and p2.parent_initiative_id == program.id
    assert program.title == "September 2026 audit" and program.slug == "september-2026-audit"
    assert p1.title == "01 — Phase 1" and p1.slug == "01-phase-1"


def test_link_epic_parents_relinks_rows_adopted_before_nesting_existed():
    """Rows adopted flat (pre-#nesting) get their parent on the next pass; a
    human-edited title is left alone, an untouched raw title is normalized."""
    db = _db()
    project = _seed(db)
    _seed_program(db, project, phases=(950,))
    db.add(Initiative(slug="old-program", title="epic(ui-audit): September 2026 audit",
                      repo=REPO, epic_repo=REPO, epic_number=945, project_id=project.id,
                      origin="epic-adoption"))
    db.add(Initiative(slug="old-phase", title="My renamed phase",
                      repo=REPO, epic_repo=REPO, epic_number=950, project_id=project.id,
                      origin="epic-adoption"))
    db.commit()

    changed = mirror_store.link_epic_parents(db, project.id)

    assert changed == 2     # the program's raw title normalized + the phase's parent linked
    program = db.query(Initiative).filter_by(slug="old-program").one()
    phase = db.query(Initiative).filter_by(slug="old-phase").one()
    assert phase.parent_initiative_id == program.id
    assert program.title == "September 2026 audit"       # raw title → normalized
    assert phase.title == "My renamed phase"             # edited title → kept
    assert mirror_store.link_epic_parents(db, project.id) == 0   # idempotent


def test_back_sync_stage_ignores_a_program():
    db = _db()
    project = _seed(db)
    _seed_program(db, project, phases=(950,))
    mirror_store.adopt_epics(db, project.id, now=ADOPT_NOW)

    n = mirror_store.back_sync_stage(
        db, [mirror_store.HumanEdit(f"{REPO}#945", "Stage", "build", "plan"),
             mirror_store.HumanEdit(f"{REPO}#950", "Stage", "design", "plan")])

    assert n == 1
    by_number = {i.epic_number: i for i in db.query(Initiative).all()}
    assert by_number[945].stage != "plan"     # program untouched
    assert by_number[950].stage == "plan"     # phase moved
```

- [ ] **Step 2: Run them to verify they fail**

Run: `python -m pytest server/tests/test_mirror_store.py -k "program or link_epic_parents" -v`
Expected: FAIL (`AttributeError: module has no attribute 'link_epic_parents'`; prefix still present).

- [ ] **Step 3: Implement**

In `adopt_epics`, replace `title = epic.title or ref` with:

```python
        title = normalize_epic_title(epic.title or ref)
```

(import `normalize_epic_title` beside `slugify` at the top of the module). After the adoption loop, before `return adopted`, add `link_epic_parents(db, project_id)`.

Add the function (after `adopt_epics`):

```python
def link_epic_parents(db: Session, project_id: str) -> int:
    """Mirror GitHub's epic nesting onto the project's adopted initiatives.

    For every Initiative with an epic pointer, its parent is the Initiative
    whose epic is the mirrored ``WorkItem.parent_work_item_id`` of that epic —
    or None when the epic has no parent, the parent is not an epic, or the
    parent is not adopted. Idempotent and re-run every adoption pass, so rows
    adopted before nesting existed are linked on the next tick. A title that
    still equals the raw epic title is normalized here too (it was written by
    adoption, not a human); an edited title is never touched. Returns the
    number of rows whose parent or title changed."""
    inits = db.query(Initiative).filter(
        Initiative.project_id == project_id,
        Initiative.epic_repo.isnot(None), Initiative.epic_number.isnot(None)).all()
    if not inits:
        return 0
    by_pointer = {(i.epic_repo, i.epic_number): i for i in inits}
    epics = db.query(WorkItem).filter(
        WorkItem.repo.in_({i.epic_repo for i in inits}),
        WorkItem.number.in_({i.epic_number for i in inits}),
        WorkItem.kind == "issue").all()
    epic_by_pointer = {(w.repo, w.number): w for w in epics}
    parent_ids = {w.parent_work_item_id for w in epics if w.parent_work_item_id}
    parents = {w.id: w for w in db.query(WorkItem).filter(
        WorkItem.id.in_(parent_ids)).all()} if parent_ids else {}
    changed = 0
    for ini in inits:
        epic = epic_by_pointer.get((ini.epic_repo, ini.epic_number))
        parent_ini = None
        if epic is not None and epic.parent_work_item_id:
            parent_wi = parents.get(epic.parent_work_item_id)
            if parent_wi is not None and parent_wi.is_epic:
                parent_ini = by_pointer.get((parent_wi.repo, parent_wi.number))
        new_parent = parent_ini.id if parent_ini is not None and parent_ini.id != ini.id else None
        if ini.parent_initiative_id != new_parent:
            ini.parent_initiative_id = new_parent
            changed += 1
        if epic is not None and epic.title and ini.title == epic.title:
            normalized = normalize_epic_title(epic.title)
            if normalized != ini.title:
                ini.title = normalized
                changed += 1
    return changed
```

In `back_sync_stage`, after `ini = db.query(Initiative).filter_by(...).first()`:

```python
        if ini is None:
            continue
        # A program's stage is derived from its phases (tree.derived_stage);
        # GitHub may drive a leaf's Stage, never a program's.
        if db.query(Initiative.id).filter_by(parent_initiative_id=ini.id).first() is not None:
            continue
        if ini.stage != e.new:
```

(Keep the existing `if ini is not None and ini.stage != e.new:` semantics; restructure as shown.)

- [ ] **Step 4: Run the whole mirror-store file**

Run: `python -m pytest server/tests/test_mirror_store.py server/tests/test_pm_sync_projects.py -v`
Expected: PASS. If `test_adopt_epics_creates_initiative_for_onboard_open_epic` fails on the slug, its fixture title "Epic Alpha" has no prefix, so it must still produce `epic-alpha`; fix the regex, not the test.

- [ ] **Step 5: Commit**

```bash
git add server/pm/mirror_store.py server/tests/test_mirror_store.py
git commit -F /tmp/c3.txt   # "feat(pm): adoption mirrors epic nesting onto initiatives and strips the filing prefix"
```

---

### Task 4: Pure roll-up rules in `tree.py`

**Files:**
- Modify: `server/pm/tree.py` (`InitiativeIn`, `initiative_reason`, `initiative_tier`, `_initiative_node`, `_capability_node`, `build_product_tree`; new `rollup_epic_progress`, `derived_stage`)
- Test: `server/tests/test_pm_tree.py`

**Interfaces:**
- Produces:
  - `rollup_epic_progress(entries: dict[str, dict | None], parent_of: dict[str, str | None]) -> dict[str, dict | None]` — for every id with descendants, an entry whose `total/done/blocked/needsHuman` are the sums over its **leaf** descendants (own `ref`/`url` kept, `children` untouched); ids without descendants pass through unchanged.
  - `derived_stage(stages: list[str | None]) -> str | None` — the earliest stage (by `INITIATIVE_STAGES` order) among phases that are not `done`; `"done"` when every phase is done; `None` for no phases.
  - `initiative_reason(..., has_children: bool = False)` → `None` for a program.
  - `InitiativeIn.parent_slug: str | None = None` (keyword, last).
  - Initiative node dict gains `"parentInitiative": str | None` and `"hasChildren": bool`.
  - A program's tier = `max_tier(own tier computed with has_children, *phase tiers)`; only leaves bump capability/product counts.

- [ ] **Step 1: Write the failing tests** (append to `server/tests/test_pm_tree.py`)

```python
from server.pm.tree import derived_stage, rollup_epic_progress


def test_rollup_sums_leaf_descendants_and_leaves_leaves_alone():
    entries = {
        "prog": {"ref": "o/r#1", "url": "u1", "total": 2, "done": 0, "blocked": 0, "needsHuman": 0},
        "p1": {"ref": "o/r#2", "url": "u2", "total": 4, "done": 1, "blocked": 1, "needsHuman": 0},
        "p2": {"ref": "o/r#3", "url": "u3", "total": 6, "done": 3, "blocked": 0, "needsHuman": 2},
        "solo": {"ref": "o/r#9", "url": "u9", "total": 5, "done": 5, "blocked": 0, "needsHuman": 0},
        "noepic": None,
    }
    parent_of = {"prog": None, "p1": "prog", "p2": "prog", "solo": None, "noepic": None}
    out = rollup_epic_progress(entries, parent_of)
    assert out["prog"] == {"ref": "o/r#1", "url": "u1", "total": 10, "done": 4, "blocked": 1, "needsHuman": 2}
    assert out["p1"] == entries["p1"] and out["solo"] == entries["solo"] and out["noepic"] is None


def test_rollup_walks_grandchildren_and_a_program_without_an_epic_entry():
    entries = {"prog": None, "mid": None, "leaf": {"ref": "o/r#5", "url": "u", "total": 3, "done": 1, "blocked": 0, "needsHuman": 0}}
    parent_of = {"prog": None, "mid": "prog", "leaf": "mid"}
    out = rollup_epic_progress(entries, parent_of)
    assert out["prog"] == {"ref": None, "url": None, "total": 3, "done": 1, "blocked": 0, "needsHuman": 0}
    assert out["mid"] == out["prog"] | {"ref": None, "url": None}


def test_derived_stage_is_the_earliest_unfinished_phase():
    assert derived_stage(["design", "plan", "build"]) == "plan"
    assert derived_stage(["done", "deploy"]) == "deploy"
    assert derived_stage(["done", "done"]) == "done"
    assert derived_stage([]) is None
    assert derived_stage([None, "build"]) == "build"


def test_program_reason_tier_and_counts_come_from_its_phases():
    phase_epic = {"ref": "o/r#2", "url": "u", "total": 4, "done": 4, "blocked": 0, "needsHuman": 0}
    product = ProductIn(
        slug="p", name="P", capabilities=(_cap("a11y"),),
        initiatives=(
            InitiativeIn(slug="audit", title="UI audit", stage="design", status="active",
                         primary_slug="a11y", capability_slugs=("a11y",),
                         epic={"ref": "o/r#1", "url": "u", "total": 4, "done": 4, "blocked": 0, "needsHuman": 0}),
            InitiativeIn(slug="audit-01", title="01", stage="build", status="active",
                         primary_slug="a11y", capability_slugs=("a11y",), epic=phase_epic,
                         parent_slug="audit"),
            InitiativeIn(slug="audit-02", title="02", stage="design", status="active",
                         primary_slug="a11y", capability_slugs=("a11y",), epic=None,
                         parent_slug="audit"),
        ),
        awaiting_triage=0, needs_home=0, incident_statuses=())
    tree = build_product_tree(product)
    by_slug = {i["slug"]: i for i in tree["initiatives"]}
    assert by_slug["audit-01"]["reason"] == "advance" and by_slug["audit-01"]["tier"] == "needs-you"
    assert by_slug["audit"]["reason"] is None                 # a program never "advances"
    assert by_slug["audit"]["tier"] == "needs-you"            # …but inherits its phase's tier
    assert by_slug["audit"]["hasChildren"] is True and by_slug["audit"]["parentInitiative"] is None
    assert by_slug["audit-01"]["parentInitiative"] == "audit"
    a11y = tree["capabilities"][0]
    assert a11y["counts"]["needsYou"] == 1 and a11y["counts"]["inFlight"] == 1   # leaves only, no double count
    assert tree["counts"]["needsYou"] == 1
```

- [ ] **Step 2: Run them to verify they fail**

Run: `python -m pytest server/tests/test_pm_tree.py -v`
Expected: FAIL with `ImportError` for `derived_stage` / `rollup_epic_progress`.

- [ ] **Step 3: Implement**

In `server/pm/tree.py`:

```python
def rollup_epic_progress(entries: dict[str, dict | None],
                         parent_of: dict[str, str | None]) -> dict[str, dict | None]:
    """A program's progress is the sum over its LEAF descendants' epics — the
    issues people close — never a count of its sub-epics. Leaves pass through.
    A program without an epic entry of its own gets one (ref/url None)."""
    children: dict[str, list[str]] = {}
    for cid, pid in parent_of.items():
        if pid is not None:
            children.setdefault(pid, []).append(cid)

    def leaves(iid: str) -> list[str]:
        kids = children.get(iid, [])
        if not kids:
            return [iid]
        return [leaf for k in kids for leaf in leaves(k)]

    out: dict[str, dict | None] = {}
    for iid, entry in entries.items():
        if iid not in children:
            out[iid] = entry
            continue
        totals = {"total": 0, "done": 0, "blocked": 0, "needsHuman": 0}
        for leaf in leaves(iid):
            e = entries.get(leaf)
            if e:
                for k in totals:
                    totals[k] += int(e.get(k) or 0)
        base = entry or {"ref": None, "url": None}
        out[iid] = {"ref": base.get("ref"), "url": base.get("url"), **totals}
    return out


def derived_stage(stages: list[str | None]) -> str | None:
    """A program sits where its slowest phase sits: the earliest non-done
    stage; ``done`` only when every phase is done; None with no phases."""
    if not stages:
        return None
    live = [s for s in stages if s in INITIATIVE_STAGES and s != "done"]
    if not live:
        return "done"
    return min(live, key=INITIATIVE_STAGES.index)
```

Change `initiative_reason` and `initiative_tier` to accept `has_children: bool = False` (keyword) and return `None` / skip the advance branch when `has_children` is true:

```python
def initiative_reason(status, stage, epic_total, epic_done, *, has_children: bool = False):
    if has_children:
        return None   # a program is advanced by advancing its phases
    if status == "active" and stage == "build" and epic_total > 0 and epic_done >= epic_total:
        return "advance"
    return None
```

(and pass `has_children=has_children` through from `initiative_tier`).

`InitiativeIn`: add `parent_slug: str | None = None` as the last field. In `build_product_tree`, compute `has_children = {i.parent_slug for i in product.initiatives if i.parent_slug}`; build nodes leaf-first: node tier for a program = `max_tier(own, *[node["tier"] for each phase node])` (phases are built first because a phase's `parent_slug` names the program; do two passes: leaves, then programs bottom-up — sort by depth descending, depth computed by walking `parent_slug`). `_initiative_node` emits `"parentInitiative": ini.parent_slug, "hasChildren": ini.slug in has_children`. In `_capability_node` and the product-level orphan loop, `_bump` counts only when `node["hasChildren"] is False`; the tier still includes every initiative homed there.

- [ ] **Step 4: Run tests**

Run: `python -m pytest server/tests/test_pm_tree.py server/tests/test_pm_map_api.py -v`
Expected: PASS (the existing positional `InitiativeIn(...)` constructions are unaffected because the new field is last and defaulted).

- [ ] **Step 5: Commit**

```bash
git add server/pm/tree.py server/tests/test_pm_tree.py
git commit -F /tmp/c4.txt   # "feat(pm): program roll-up rules — leaf-sum progress, derived stage, leaf-only counts"
```

---

### Task 5: API — parent, children, derived stage, roll-up, guards

**Files:**
- Modify: `server/api/sdlc.py` (`_decorate`, `list_initiatives`, `get_initiative`, `advance_initiative`, `step_back_initiative`), `server/api/pm_tree.py:160-169`
- Test: `server/tests/test_sdlc_api.py` (append), `server/tests/test_pm_map_nesting_api.py` (create)

**Interfaces:**
- Consumes: Task 4 helpers.
- Produces: every initiative dict carries `parentInitiativeSlug: str | None`, `children: [{slug, title, stage, status}]` (direct phases, by `created_at`), `hasChildren: bool`; a program's `stage` is `derived_stage(...)`; a program's `epic` is the rolled-up entry. `POST …/advance` and `…/step-back` → 409 `{"error": "has_children"}` on a program.

- [ ] **Step 1: Write the failing tests**

Append to `server/tests/test_sdlc_api.py` (same seeding style as `test_get_detail_includes_epic_progress`):

```python
def test_program_detail_derives_stage_rolls_up_leaves_and_refuses_advance():
    """A program (initiative with phases) reports the earliest phase stage,
    the leaf-issue roll-up, its phases as `children`, and cannot be advanced."""
    from server.db import db_session
    from server.db.models import Initiative, WorkItem

    async def _seed(db_url: str):
        from server.db import init_db
        await init_db(db_url)
        async with db_session() as db:
            program = Initiative(slug="audit", repo="o/r", title="UI audit", stage="design",
                                 status="active", epic_repo="o/r", epic_number=100)
            db.add(program)
            await db.flush()
            p1 = Initiative(slug="audit-01", repo="o/r", title="01", stage="plan", status="active",
                            epic_repo="o/r", epic_number=101, parent_initiative_id=program.id)
            p2 = Initiative(slug="audit-02", repo="o/r", title="02", stage="build", status="active",
                            epic_repo="o/r", epic_number=102, parent_initiative_id=program.id)
            db.add_all([p1, p2])
            prog_wi = WorkItem(repo="o/r", number=100, kind="issue", state="open", is_epic=True)
            db.add(prog_wi)
            await db.flush()
            e1 = WorkItem(repo="o/r", number=101, kind="issue", state="open", is_epic=True, parent_work_item_id=prog_wi.id)
            e2 = WorkItem(repo="o/r", number=102, kind="issue", state="open", is_epic=True, parent_work_item_id=prog_wi.id)
            db.add_all([e1, e2])
            await db.flush()
            db.add_all([
                WorkItem(repo="o/r", number=1, kind="issue", state="closed", parent_work_item_id=e1.id),
                WorkItem(repo="o/r", number=2, kind="issue", state="open", parent_work_item_id=e1.id),
                WorkItem(repo="o/r", number=3, kind="issue", state="open", parent_work_item_id=e2.id),
            ])

    with tempfile.TemporaryDirectory() as tmp:
        db_url = f"sqlite+aiosqlite:///{tmp}/test.db"
        asyncio.run(_seed(db_url))
        with patch.dict(os.environ, {"CCSWITCH_DB_URL": db_url, "CCSWITCH_DEV": "1",
                                     "CCSWITCH_TAILNET_ALLOWLIST": ""}):
            from server.sessions.orchestrator import SessionOrchestrator
            from server.tests.fakes.fake_factory import fake_sdk_factory
            from server.app import create_app
            from fastapi.testclient import TestClient
            orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())
            app = create_app(orchestrator=orch)
            with TestClient(app, raise_server_exceptions=False) as c:
                body = c.get("/api/sdlc/initiatives/audit", headers=IDENTITY_HEADERS).json()
                assert body["stage"] == "plan"                       # earliest phase, not the row's "design"
                assert body["hasChildren"] is True and body["parentInitiativeSlug"] is None
                assert [ch["slug"] for ch in body["children"]] == ["audit-01", "audit-02"]
                assert body["epic"]["total"] == 3 and body["epic"]["done"] == 1   # leaf issues, not 2 sub-epics
                phase = c.get("/api/sdlc/initiatives/audit-01", headers=IDENTITY_HEADERS).json()
                assert phase["parentInitiativeSlug"] == "audit" and phase["children"] == []
                assert phase["epic"]["total"] == 2
                listed = {i["slug"]: i for i in c.get("/api/sdlc/initiatives", headers=IDENTITY_HEADERS).json()}
                assert listed["audit"]["stage"] == "plan" and listed["audit"]["epic"]["total"] == 3
                r = c.post("/api/sdlc/initiatives/audit/advance", headers=IDENTITY_HEADERS)
                assert r.status_code == 409 and r.json()["detail"]["error"] == "has_children"
                r = c.post("/api/sdlc/initiatives/audit/step-back", headers=IDENTITY_HEADERS)
                assert r.status_code == 409 and r.json()["detail"]["error"] == "has_children"
```

Create `server/tests/test_pm_map_nesting_api.py`:

```python
"""`GET /api/pm/map` carries `parentInitiative`/`hasChildren` on initiative
nodes so the web can nest a program over its phases (roadmap 5S plan)."""
from __future__ import annotations

import asyncio
import os
import tempfile
from unittest.mock import patch

IDENTITY_HEADERS = {"Tailscale-User-Login": "testuser@example.com"}


def test_map_initiative_nodes_carry_their_parent():
    from server.db import db_session, init_db
    from server.db.models import Feature, Initiative, InitiativeFeature, Project, ProjectRepo

    async def _seed(db_url):
        await init_db(db_url)
        async with db_session() as db:
            p = Project(slug="kindash", name="KinDash", onboarding_state="live")
            db.add(p)
            await db.flush()
            db.add(ProjectRepo(project_id=p.id, repo="o/r", is_primary=True))
            f = Feature(project_id=p.id, slug="accessibility", title="Accessibility", state="live")
            db.add(f)
            await db.flush()
            prog = Initiative(slug="audit", title="UI audit", stage="design", status="active", project_id=p.id)
            db.add(prog)
            await db.flush()
            ph = Initiative(slug="audit-01", title="01", stage="plan", status="active",
                            project_id=p.id, parent_initiative_id=prog.id)
            db.add(ph)
            await db.flush()
            db.add_all([InitiativeFeature(initiative_id=prog.id, feature_id=f.id, is_primary=True),
                        InitiativeFeature(initiative_id=ph.id, feature_id=f.id, is_primary=True)])

    with tempfile.TemporaryDirectory() as tmp:
        db_url = f"sqlite+aiosqlite:///{tmp}/t.db"
        asyncio.run(_seed(db_url))
        with patch.dict(os.environ, {"CCSWITCH_DB_URL": db_url, "CCSWITCH_DEV": "1",
                                     "CCSWITCH_TAILNET_ALLOWLIST": ""}):
            from fastapi.testclient import TestClient
            from server.app import create_app
            from server.sessions.orchestrator import SessionOrchestrator
            from server.tests.fakes.fake_factory import fake_sdk_factory
            app = create_app(orchestrator=SessionOrchestrator(sdk_factory=fake_sdk_factory()))
            with TestClient(app, raise_server_exceptions=False) as c:
                prod = c.get("/api/pm/map", headers=IDENTITY_HEADERS).json()["products"][0]
                nodes = {i["slug"]: i for i in prod["initiatives"]}
                assert nodes["audit"]["hasChildren"] is True and nodes["audit"]["parentInitiative"] is None
                assert nodes["audit-01"]["parentInitiative"] == "audit"
                assert nodes["audit"]["stage"] == "plan"
                assert prod["capabilities"][0]["counts"]["inFlight"] == 1   # the phase only
```

- [ ] **Step 2: Run them to verify they fail**

Run: `python -m pytest server/tests/test_sdlc_api.py -k program server/tests/test_pm_map_nesting_api.py -v`
Expected: FAIL (`KeyError: 'hasChildren'`).

- [ ] **Step 3: Implement**

In `server/api/sdlc.py`:

```python
async def _nesting_batch(db, initiatives: list[Initiative]) -> tuple[dict[str, str | None], dict[str, list[Initiative]]]:
    """(id → parent slug, id → direct phases) in two queries."""
    ids = [i.id for i in initiatives]
    if not ids:
        return {}, {}
    parent_ids = {i.parent_initiative_id for i in initiatives if i.parent_initiative_id}
    parents = {p.id: p for p in (await db.execute(select(Initiative).where(
        Initiative.id.in_(parent_ids)))).scalars().all()} if parent_ids else {}
    kids = (await db.execute(select(Initiative).where(
        Initiative.parent_initiative_id.in_(ids)).order_by(Initiative.created_at))).scalars().all()
    by_parent: dict[str, list[Initiative]] = {}
    for k in kids:
        by_parent.setdefault(k.parent_initiative_id, []).append(k)
    parent_slug = {i.id: (parents[i.parent_initiative_id].slug if i.parent_initiative_id in parents else None)
                   for i in initiatives}
    return parent_slug, by_parent


async def _descendants(db, roots: list[Initiative]) -> list[Initiative]:
    """Every initiative below *roots*, breadth-first (GitHub nests to 8 levels)."""
    out: list[Initiative] = []
    frontier = [r.id for r in roots]
    seen = set(frontier)
    while frontier:
        rows = (await db.execute(select(Initiative).where(
            Initiative.parent_initiative_id.in_(frontier)))).scalars().all()
        frontier = [r.id for r in rows if r.id not in seen]
        seen.update(frontier)
        out.extend(rows)
    return out


async def _progress_rolled_up(db, initiatives: list[Initiative], *, include_children: bool) -> dict[str, dict | None]:
    """`_epic_progress_batch` over *initiatives* plus every descendant, rolled
    up so a program reports its leaf issues (tree.rollup_epic_progress)."""
    extra = await _descendants(db, initiatives)
    rows = list({i.id: i for i in [*initiatives, *extra]}.values())
    entries = await _epic_progress_batch(db, rows, include_children=include_children)
    parent_of = {i.id: i.parent_initiative_id for i in rows}
    return rollup_epic_progress(entries, parent_of)
```

`_decorate` gains, after the capability fields:

```python
    parent_slug, by_parent = await _nesting_batch(db, initiatives)
    for i, d in zip(initiatives, dicts):
        phases = by_parent.get(i.id, [])
        d["parentInitiativeSlug"] = parent_slug.get(i.id)
        d["hasChildren"] = bool(phases)
        d["children"] = [{"slug": p.slug, "title": p.title, "stage": p.stage, "status": p.status} for p in phases]
        if phases:
            d["stage"] = derived_stage([p.stage for p in phases])
```

`list_initiatives` and `get_initiative` call `_progress_rolled_up` instead of `_epic_progress_batch` (keep `include_children=False` for the list). `advance_initiative` and `step_back_initiative` add, right after `_load_initiative`:

```python
    if (await db.execute(select(Initiative.id).where(
            Initiative.parent_initiative_id == ini.id))).first() is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail={
            "error": "has_children",
            "message": "a program's stage is derived from its phases; advance a phase instead"})
```

In `server/api/pm_tree.py`, load a slug-by-id map for the product's initiatives and pass `parent_slug=slug_by_id.get(ini.parent_initiative_id)` into `InitiativeIn`; for an initiative that has phases (its id appears as another row's `parent_initiative_id`), pass `stage=derived_stage([p.stage for p in its phases])` instead of the row's own stage, so the map and the sdlc endpoints agree; use `_progress_rolled_up` for the `epics` dict. Import `derived_stage`, `rollup_epic_progress` from `server.pm.tree`.

- [ ] **Step 4: Run tests**

Run: `python -m pytest server/tests/test_sdlc_api.py server/tests/test_pm_map_api.py server/tests/test_pm_map_nesting_api.py server/tests/test_pm_materialize_apply.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add server/api/sdlc.py server/api/pm_tree.py server/tests/test_sdlc_api.py server/tests/test_pm_map_nesting_api.py
git commit -F /tmp/c5.txt   # "feat(api): initiatives carry parent/children, derived program stage and leaf roll-up"
```

---

### Task 6: A hand-merged map advances onboarding

**Files:**
- Modify: `server/sessions/orchestrator.py:6353-6363` (`_pm_sync_apply_feature_plan` tail)
- Test: `server/tests/test_pm_orchestrator_seams.py`

**Interfaces:** none new.

- [ ] **Step 1: Write the failing test** (append)

```python
@pytest.mark.asyncio
async def test_apply_feature_plan_registered_project_with_docs_advances_to_map_merged(db_env):
    """KinDash's map was merged by hand, never via propose-map, so the project
    sat at `registered` with 18 indexed features and the wizard called it
    un-onboarded. Indexed docs ARE a merged map: registered/syncing advance."""
    await init_db(db_env)
    async with db_session() as db:
        p = Project(slug="hand-merged", name="hand-merged", onboarding_state="registered")
        db.add(p)
        await db.flush()
        project_id = p.id
    orch = _bare_orch()
    from server.pm.feature_index import FeatureIndexPlan, FeatureUpsert
    plan = FeatureIndexPlan(
        upserts=(FeatureUpsert(slug="tasks", title="Task management", state="live",
                               summary="s", repo_path="docs/product/features/tasks.md",
                               parent_slug=None, links=()),),
        orphan_slugs=(), revive_slugs=(), warnings=())
    await orch._pm_sync_apply_feature_plan(project_id, plan)
    async with db_session() as db:
        from sqlalchemy import select
        assert (await db.execute(select(Project).where(
            Project.id == project_id))).scalar_one().onboarding_state == "map_merged"
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python -m pytest server/tests/test_pm_orchestrator_seams.py -k hand_merged -v`
Expected: FAIL (`'registered' == 'map_merged'`).

- [ ] **Step 3: Implement**

Change the condition to `if proj is not None and proj.onboarding_state in ("map_proposed", "registered", "syncing"):` and the comment/docstring to say a map indexed from the default branch is a merged map whatever route it took; `map_merged`, `classifying` and `live` are untouched (the existing "unchanged" test keeps guarding that).

- [ ] **Step 4: Run tests**

Run: `python -m pytest server/tests/test_pm_orchestrator_seams.py server/tests/test_pm_onboarding_engine.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add server/sessions/orchestrator.py server/tests/test_pm_orchestrator_seams.py
git commit -F /tmp/c6.txt   # "fix(pm): a hand-merged feature map advances a registered project to map_merged"
```

---

### Task 7: `labels:` frontmatter → `Feature.route_labels`

**Files:**
- Modify: `server/pm/featuredoc.py`, `server/pm/feature_index.py`, `server/sessions/orchestrator.py:6291-6305`
- Test: `server/tests/test_pm_featuredoc.py`, `server/tests/test_pm_feature_index.py`, `server/tests/test_pm_orchestrator_seams.py`

**Interfaces:**
- Produces: `FeatureDoc.labels: tuple[str, ...] = ()`; `FeatureUpsert.labels: tuple[str, ...] = ()`; apply writes `row.route_labels = list(up.labels) or None`. Serialization writes `labels:` only when non-empty. A non-list `labels:` value is ignored (doc still indexes).

- [ ] **Step 1: Write the failing tests**

`server/tests/test_pm_featuredoc.py` (append):

```python
LABELLED = """---
slug: flaky-test-triage
title: Flaky test triage
state: live
parent: delivery
labels: [flaky, ci-failure]
---
A flaky-labeled issue means a live, unfixed flake.

## Problem
"""


def test_labels_parse_and_round_trip():
    doc = parse_feature_doc(LABELLED)
    assert doc.labels == ("flaky", "ci-failure") and doc.parent == "delivery"
    assert parse_feature_doc(serialize_feature_doc(doc)) == doc
    assert "labels:" in serialize_feature_doc(doc)


def test_labels_absent_or_malformed_is_empty():
    assert parse_feature_doc(VALID).labels == ()
    bad = VALID.replace("state: live", "state: live\nlabels: not-a-list")
    assert parse_feature_doc(bad).labels == ()
    assert "labels:" not in serialize_feature_doc(parse_feature_doc(VALID))
```

`server/tests/test_pm_feature_index.py` (append; use the file's existing helper for docs — read `test_parent_and_links_are_carried_onto_the_upsert` at line 37 and mirror its construction):

```python
def test_labels_are_carried_onto_the_upsert():
    doc = parse_feature_doc(
        "---\nslug: flaky-test-triage\ntitle: Flaky\nstate: live\nlabels: [flaky]\n---\nbody\n")
    plan = plan_feature_index([], [("docs/product/features/flaky-test-triage.md", doc)])
    assert plan.upserts[0].labels == ("flaky",)
```

(`plan_feature_index` is whatever the module's planner function is called at line ~100 of `feature_index.py`; the existing test at line 37 shows the exact name and the `existing` argument shape — copy it.)

`server/tests/test_pm_orchestrator_seams.py` (append):

```python
@pytest.mark.asyncio
async def test_apply_feature_plan_writes_route_labels(db_env):
    await init_db(db_env)
    async with db_session() as db:
        p = Project(slug="lbl", name="lbl", onboarding_state="live")
        db.add(p)
        await db.flush()
        project_id = p.id
    orch = _bare_orch()
    from server.pm.feature_index import FeatureIndexPlan, FeatureUpsert
    plan = FeatureIndexPlan(
        upserts=(FeatureUpsert(slug="flaky-test-triage", title="Flaky", state="live", summary="s",
                               repo_path="docs/product/features/flaky-test-triage.md",
                               parent_slug=None, links=(), labels=("flaky",)),
                 FeatureUpsert(slug="task-views", title="Views", state="live", summary="s",
                               repo_path="docs/product/features/task-views.md",
                               parent_slug=None, links=())),
        orphan_slugs=(), revive_slugs=(), warnings=())
    await orch._pm_sync_apply_feature_plan(project_id, plan)
    async with db_session() as db:
        from sqlalchemy import select
        rows = {f.slug: f.route_labels for f in (await db.execute(
            select(Feature).where(Feature.project_id == project_id))).scalars().all()}
    assert rows == {"flaky-test-triage": ["flaky"], "task-views": None}
```

- [ ] **Step 2: Run them to verify they fail**

Run: `python -m pytest server/tests/test_pm_featuredoc.py server/tests/test_pm_feature_index.py server/tests/test_pm_orchestrator_seams.py -k labels -v`
Expected: FAIL (`AttributeError: 'FeatureDoc' object has no attribute 'labels'`).

- [ ] **Step 3: Implement**

`featuredoc.py`: add `labels: tuple[str, ...] = ()` to `FeatureDoc`; in `parse_feature_doc`:

```python
    raw_labels = meta.get("labels")
    labels = tuple(str(x).strip() for x in raw_labels if str(x).strip()) if isinstance(raw_labels, list) else ()
```

and pass `labels=labels`; in `serialize_feature_doc`, after `parent`: `if doc.labels: meta["labels"] = list(doc.labels)`.

`feature_index.py`: `FeatureUpsert.labels: tuple[str, ...] = ()` (after `links`, before the defaulted PR fields is fine since it is also defaulted); the planner passes `labels=doc.labels`.

`orchestrator.py` apply loop: `row.route_labels = list(up.labels) or None`.

- [ ] **Step 4: Run tests**

Run: `python -m pytest server/tests/test_pm_featuredoc.py server/tests/test_pm_feature_index.py server/tests/test_pm_orchestrator_seams.py server/tests/test_pm_map_changes_ops.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add server/pm/featuredoc.py server/pm/feature_index.py server/sessions/orchestrator.py server/tests/test_pm_featuredoc.py server/tests/test_pm_feature_index.py server/tests/test_pm_orchestrator_seams.py
git commit -F /tmp/c7.txt   # "feat(pm): feature docs declare routing labels; indexed onto Feature.route_labels"
```

---

### Task 8: pm-triage routes labelled items mechanically

**Files:**
- Modify: `server/automations/pm_triage.py:173-195`, `server/sessions/orchestrator.py:6439-6474` (`_pm_triage_auto_link`)
- Test: `server/tests/test_pm_triage_loop.py`, `server/tests/test_pm_triage_seams.py`

**Interfaces:**
- Consumes: `Feature.route_labels` (Task 7), `owning_projects_by_repo` (`server/pm/repo_project.py`).
- Produces: the loop offers every pending item that is a PR with closing refs **or** carries any label to `auto_link`; the seam returns True after a label route (`classified_by="label-route"`), False when zero or more than one feature in the owning project routes any of the item's labels.

- [ ] **Step 1: Write the failing tests**

`server/tests/test_pm_triage_loop.py` (append):

```python
@pytest.mark.asyncio
async def test_labelled_issues_are_offered_to_auto_link_and_leave_the_queue():
    ctx = Ctx()
    flaky = PendingItemView(repo="o/r", number=7, kind="issue", title="[flaky] x", body="",
                            labels=("flaky",), linked_issue_numbers=())
    plain = _item(8)
    loop, rec = _loop(ctx, pending=[flaky, plain],
                      autolink_pred=lambda item: "flaky" in item.labels)
    await loop.tick()
    assert rec["autolinked"] == [7]
    assert rec["classified"] == [[8]]          # only the unrouted item reaches the classifier
```

`server/tests/test_pm_triage_seams.py` (append):

```python
@pytest.mark.asyncio
async def test_auto_link_routes_an_issue_by_feature_label(db_env):
    await _seed(db_env)
    async with db_session() as db:
        from sqlalchemy import select
        pid = (await db.execute(select(Project.id).where(Project.slug == "ccswitch"))).scalars().one()
        f = Feature(project_id=pid, slug="flaky-test-triage", title="Flaky", state="live",
                    route_labels=["flaky"])
        db.add(f)
        db.add(Feature(project_id=pid, slug="task-views", title="Views", state="live"))
        await db.flush()
        db.add(WorkItem(repo=REPO, number=7, kind="issue", title="[flaky] x", state="open",
                        triage_state="unclassified"))
        feature_id = f.id
    orch = _triage_orch()
    item = PendingItemView(repo=REPO, number=7, kind="issue", title="[flaky] x", body="",
                           labels=("flaky", "bug"), linked_issue_numbers=())
    assert await orch._pm_triage_auto_link(item) is True
    async with db_session() as db:
        from sqlalchemy import select
        wi = (await db.execute(select(WorkItem).where(
            WorkItem.repo == REPO, WorkItem.number == 7))).scalars().one()
        assert wi.feature_id == feature_id
        assert wi.triage_state == "classified" and wi.classified_by == "label-route"


@pytest.mark.asyncio
async def test_auto_link_declines_when_two_features_route_the_same_label(db_env):
    await _seed(db_env)
    async with db_session() as db:
        from sqlalchemy import select
        pid = (await db.execute(select(Project.id).where(Project.slug == "ccswitch"))).scalars().one()
        db.add(Feature(project_id=pid, slug="a", title="A", state="live", route_labels=["ci"]))
        db.add(Feature(project_id=pid, slug="b", title="B", state="live", route_labels=["ci"]))
        db.add(WorkItem(repo=REPO, number=7, kind="issue", title="x", state="open",
                        triage_state="unclassified"))
    orch = _triage_orch()
    item = PendingItemView(repo=REPO, number=7, kind="issue", title="x", body="",
                           labels=("ci",), linked_issue_numbers=())
    assert await orch._pm_triage_auto_link(item) is False
```

- [ ] **Step 2: Run them to verify they fail**

Run: `python -m pytest server/tests/test_pm_triage_loop.py -k labelled server/tests/test_pm_triage_seams.py -k label -v`
Expected: FAIL (`rec["autolinked"] == []`; seam returns False).

- [ ] **Step 3: Implement**

`pm_triage.py` pass condition: `if (item.kind == "pr" and item.linked_issue_numbers) or item.labels:` (update the comment: "PRs whose closing refs point at a classified issue inherit that feature; an item carrying a label a feature doc routes (`labels:`) is homed the same way — no AI session either way").

`orchestrator._pm_triage_auto_link`: restructure into two helpers called in order; the closing-ref branch stays exactly as it is and returns True on a link; when it does not link, run:

```python
    async def _pm_triage_route_by_label(self, item) -> bool:
        """`labels:` on a feature doc → mechanical home for a labelled item.
        Exactly one feature in the item's OWNING project must route one of its
        labels; zero or several → False, the classifier decides."""
        from datetime import datetime, timezone
        from sqlalchemy import select
        from server.db.models import Feature, WorkItem
        from server.pm.repo_project import owning_projects_by_repo
        labels = {l for l in (item.labels or ()) if l}
        if not labels:
            return False
        async with db_session() as db:
            owner = (await owning_projects_by_repo(db, [item.repo])).get(item.repo)
            if owner is None:
                return False
            routed = [f for f in (await db.execute(select(Feature).where(
                Feature.project_id == owner.project_id,
                Feature.route_labels.isnot(None),
                Feature.orphaned.isnot(True)))).scalars().all()
                      if labels & set(f.route_labels or [])]
            if len(routed) != 1:
                return False
            wi = (await db.execute(select(WorkItem).where(
                WorkItem.repo == item.repo, WorkItem.kind == item.kind,
                WorkItem.number == item.number))).scalar_one_or_none()
            if wi is None or wi.feature_id is not None:
                return False
            wi.feature_id = routed[0].id
            wi.triage_state = "classified"
            wi.classified_by = "label-route"
            wi.classified_at = datetime.now(timezone.utc)
        return True
```

and `_pm_triage_auto_link` ends with `return await self._pm_triage_route_by_label(item)` on every path that used to `return False`.

- [ ] **Step 4: Run tests**

Run: `python -m pytest server/tests/test_pm_triage_loop.py server/tests/test_pm_triage_seams.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add server/automations/pm_triage.py server/sessions/orchestrator.py server/tests/test_pm_triage_loop.py server/tests/test_pm_triage_seams.py
git commit -F /tmp/c8.txt   # "feat(pm-triage): route labelled items onto the feature whose doc claims the label"
```

---

### Task 9: Prompts write `parent:`, `labels:` and a leading outcome sentence

**Files:**
- Modify: `server/pm/classification.py:244-263` (Step D) and `:333-336` (critic step 3)
- Create: `server/tests/test_pm_classification_prompt.py`

- [ ] **Step 1: Write the failing test**

```python
"""The feature-doc prompts must teach the shape the indexer reads: `parent:`
(the only grouping field, `featuredoc.py`), `labels:` (mechanical routing),
and a one-sentence outcome before `## Problem` (what the roadmap shows as the
summary, `feature_index._summary`). Agents that were only told the body
sections invented `area:` instead (KinDash, 18 docs, 2026-09)."""
import pytest

from server.pm.classification import build_coverage_critic_prompt, build_map_synthesis_prompt


@pytest.mark.parametrize("scope,kwargs", [
    ("initial", {}),
    ("diff", {"suggestion": "billing", "item_numbers": (1, 2)}),
])
def test_synthesis_prompt_teaches_parent_labels_and_the_outcome_sentence(scope, kwargs):
    prompt = build_map_synthesis_prompt(slug="p", repo="o/r", default_branch="main",
                                        open_count=1, closed_count=0, scope=scope, **kwargs)
    assert "  parent: <slug of the parent capability" in prompt
    assert "  labels:" in prompt and "bot-filed" in prompt
    assert "ONE sentence" in prompt and "before `## Problem`" in prompt
    assert "Never invent other frontmatter keys" in prompt


def test_critic_prompt_points_at_the_same_frontmatter_rules():
    prompt = build_coverage_critic_prompt(slug="p", repo="o/r", default_branch="main",
                                          branch="b", open_count=1, closed_count=0)
    assert "parent:" in prompt and "labels:" in prompt and "outcome sentence" in prompt
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python -m pytest server/tests/test_pm_classification_prompt.py -v`
Expected: FAIL (assertions on `parent:`).

- [ ] **Step 3: Implement**

In Step D, after the `state:` line add:

```python
        "  parent: <slug of the parent capability; omit on a top-level capability>\n"
        "  labels: [<GitHub labels whose issues ALWAYS belong here, e.g. flaky>]  # optional\n"
```

and after the `links` block:

```python
        "Never invent other frontmatter keys (`area`, `owner`, `category`): the "
        "indexer reads slug/title/state/parent/labels/links and silently ignores "
        "the rest, so a grouping written anywhere else does not exist.\n"
        "Top-level capabilities are the product's standing areas (a family-facing "
        "feature area, a quality bar such as accessibility or security, or the "
        "delivery pipeline); everything else nests under one of them via `parent`. "
        "`labels` is for bot-filed or audit-filed issues (flaky detectors, deploy "
        "audits, CI failures) so they route mechanically and never cluster into a "
        "proposed capability of their own.\n"
        "The body starts, before `## Problem`, with ONE sentence stating the outcome "
        "this capability delivers — the roadmap shows that sentence as the summary.\n"
```

In the critic prompt step 3, append to the parenthetical: "same outcome template + frontmatter, including `parent:`, optional `labels:` and the leading outcome sentence; …".

- [ ] **Step 4: Run tests**

Run: `python -m pytest server/tests/test_pm_classification_prompt.py server/tests/test_pm_codex_acceptance.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add server/pm/classification.py server/tests/test_pm_classification_prompt.py
git commit -F /tmp/c9.txt   # "feat(pm): synthesis and critic prompts write parent:, labels: and a leading outcome sentence"
```

---

### Task 10: Roadmap health readout (server)

**Files:**
- Create: `server/pm/health.py`, `server/tests/test_pm_health.py`
- Modify: `server/api/pm.py` (new route `GET /projects/{slug}/health`)

**Interfaces:**
- Produces: `roadmap_health(*, capabilities, initiatives, proposals, now, stale_days=30, inbox_days=7) -> RoadmapHealth` (frozen dataclass with `unparented_capabilities: tuple[str, ...]`, `unlinked_initiatives: tuple[str, ...]`, `stale_proposed: tuple[str, ...]`, `stale_inbox: int`) and the route returning it camelCased plus `generatedAt`.
- Inputs are plain tuples: `capabilities: Iterable[tuple[slug, parent_slug | None, state, updated_at]]`, `initiatives: Iterable[tuple[slug, capability_count, has_children]]`, `proposals: Iterable[datetime]` (created_at of pending proposals). Top-level capabilities are unparented by design, so `unparented_capabilities` lists only a capability that is a root **and has no children and is not one of the product's roots with children** — simpler rule the test pins: a root with zero children when the product has ≥1 root with children (i.e. the tree is being used and this leaf sits outside it).

- [ ] **Step 1: Write the failing test** `server/tests/test_pm_health.py`

```python
from datetime import datetime, timedelta, timezone

from server.pm.health import roadmap_health

NOW = datetime(2026, 9, 12, tzinfo=timezone.utc)
OLD = NOW - timedelta(days=45)
NEW = NOW - timedelta(days=3)


def test_health_flags_loose_leaves_unlinked_leaf_initiatives_stale_proposed_and_old_inbox():
    caps = [
        ("tasks", None, "live", NEW), ("task-views", "tasks", "delivering", NEW),
        ("flaky-test-triage", None, "proposed", OLD),          # root with no children while a tree exists
        ("ai-assistant-chat", "ai", "proposed", NEW),          # proposed but fresh
    ]
    inits = [("audit", 0, True), ("audit-01", 0, False), ("views", 1, False)]
    props = [OLD, NEW]
    h = roadmap_health(capabilities=caps, initiatives=inits, proposals=props, now=NOW)
    assert h.unparented_capabilities == ("flaky-test-triage",)
    assert h.unlinked_initiatives == ("audit-01",)          # a program is judged by its phases
    assert h.stale_proposed == ("flaky-test-triage",)
    assert h.stale_inbox == 1


def test_health_of_a_flat_map_flags_nothing_as_unparented():
    caps = [("a", None, "live", NEW), ("b", None, "live", NEW)]
    h = roadmap_health(capabilities=caps, initiatives=[], proposals=[], now=NOW)
    assert h.unparented_capabilities == ()
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python -m pytest server/tests/test_pm_health.py -v`
Expected: FAIL (`ModuleNotFoundError: server.pm.health`).

- [ ] **Step 3: Implement** `server/pm/health.py`

```python
"""Roadmap drift readout (5S plan, Sustain). Pure: the API loads rows and
calls `roadmap_health`; nothing here decides or mutates."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable


@dataclass(frozen=True)
class RoadmapHealth:
    unparented_capabilities: tuple[str, ...]
    unlinked_initiatives: tuple[str, ...]
    stale_proposed: tuple[str, ...]
    stale_inbox: int


def roadmap_health(*, capabilities: Iterable[tuple[str, str | None, str | None, datetime | None]],
                   initiatives: Iterable[tuple[str, int, bool]],
                   proposals: Iterable[datetime | None],
                   now: datetime, stale_days: int = 30, inbox_days: int = 7) -> RoadmapHealth:
    caps = list(capabilities)
    has_child = {p for _, p, _, _ in caps if p}
    tree_in_use = any(p is None and s in has_child for s, p, _, _ in caps)
    unparented = tuple(sorted(s for s, p, _, _ in caps
                              if tree_in_use and p is None and s not in has_child))
    unlinked = tuple(sorted(s for s, n, prog in initiatives if n == 0 and not prog))
    cutoff = now - timedelta(days=stale_days)
    stale = tuple(sorted(s for s, _, st, upd in caps
                         if st == "proposed" and upd is not None and _aware(upd) < cutoff))
    inbox_cutoff = now - timedelta(days=inbox_days)
    stale_inbox = sum(1 for c in proposals if c is not None and _aware(c) < inbox_cutoff)
    return RoadmapHealth(unparented, unlinked, stale, stale_inbox)


def _aware(dt: datetime) -> datetime:
    from datetime import timezone
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
```

Route in `server/api/pm.py` (beside `get_project`):

```python
@router.get("/projects/{slug}/health")
async def project_health(slug: str, _identity: str = Depends(current_identity),
                         db=Depends(get_db)) -> dict:
    """Roadmap drift for one product (5S plan, Sustain): loose capabilities,
    initiatives with no capability, stale `proposed` leaves, old inbox items."""
    p = await _load_project(db, slug)
    feats = (await db.execute(select(Feature).where(
        Feature.project_id == p.id, Feature.orphaned.isnot(True)))).scalars().all()
    slug_by_id = {f.id: f.slug for f in feats}
    caps = [(f.slug, slug_by_id.get(f.parent_feature_id), f.state, f.updated_at) for f in feats]
    inits = (await db.execute(select(Initiative).where(Initiative.project_id == p.id))).scalars().all()
    ids = [i.id for i in inits]
    link_counts: dict[str, int] = {}
    parents_with_kids: set[str] = set()
    if ids:
        for iid, in (await db.execute(select(InitiativeFeature.initiative_id).where(
                InitiativeFeature.initiative_id.in_(ids)))).all():
            link_counts[iid] = link_counts.get(iid, 0) + 1
        parents_with_kids = {pid for pid, in (await db.execute(
            select(Initiative.parent_initiative_id).where(
                Initiative.parent_initiative_id.in_(ids)))).all() if pid}
    props = [c for c, in (await db.execute(select(TriageProposal.created_at).join(
        WorkItem, WorkItem.id == TriageProposal.work_item_id).where(
        TriageProposal.status == "pending",
        WorkItem.repo.in_([r.repo for r in p.repos] if p.repos else [""])))).all()]
    now = datetime.now(timezone.utc)
    h = roadmap_health(capabilities=caps,
                       initiatives=[(i.slug or "", link_counts.get(i.id, 0), i.id in parents_with_kids) for i in inits],
                       proposals=props, now=now)
    return {"unparentedCapabilities": list(h.unparented_capabilities),
            "unlinkedInitiatives": list(h.unlinked_initiatives),
            "staleProposed": list(h.stale_proposed), "staleInbox": h.stale_inbox,
            "generatedAt": now.isoformat()}
```

(`p.repos` — use however `get_project` at line 271 loads a project's repos; mirror it exactly.)

- [ ] **Step 4: Run tests + a route smoke test**

Add to `server/tests/test_pm_map_nesting_api.py` a test that hits `/api/pm/projects/kindash/health` on the same seed and asserts `unlinkedInitiatives == []` and `staleInbox == 0`. Run: `python -m pytest server/tests/test_pm_health.py server/tests/test_pm_map_nesting_api.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add server/pm/health.py server/api/pm.py server/tests/test_pm_health.py server/tests/test_pm_map_nesting_api.py
git commit -F /tmp/c10.txt   # "feat(pm): GET /projects/{slug}/health — roadmap drift readout"
```

---

### Task 11: Server PR

- [ ] **Step 1: Run the four-step gate** (`ruff`, `mypy`, core `pytest tests/`, `pytest server/tests/ -m "not slow"` in the background tee'd to `/tmp/nesting-server-tests.log`, using the test-slot acquire loop from `core.md`).
- [ ] **Step 2: In-flight check** with the GraphQL query from `core.md` for `server/pm/(mirror_store|tree|featuredoc|feature_index|classification|health)\.py|server/api/(sdlc|pm_tree|pm)\.py|server/automations/pm_triage\.py`.
- [ ] **Step 3: Push, open the PR** (`gh pr create --body-file /tmp/pr-server.md`, title `feat(pm): mirror epic nesting onto initiatives; hand-merged maps advance onboarding; label routing; health readout`; body ends with the Claude Code trailer; `Refs` the 5S spec path), then `gh pr ready` if needed and `gh pr merge <#> --squash --auto`.
- [ ] **Step 4: Commit the two plan files and the spec** in this PR too (`docs/superpowers/specs/2026-09-12-kindash-roadmap-5s-plan.md`, both plans).

---

### Task 12: Web types and nested tree rows (after #1590 merges)

**Files:**
- Modify: `web/src/api/roadmap.ts:46-59`, `web/src/api/sdlc.ts:79-103`, `web/src/features/roadmap/treeRows.ts`, `web/src/features/roadmap/__fixtures__/map.ts`
- Test: `web/src/features/roadmap/treeRows.test.ts`

**Interfaces:**
- Produces: `InitiativeNode.parentInitiative: string | null`, `InitiativeNode.hasChildren: boolean`; `Initiative.parentInitiativeSlug: string | null`, `Initiative.hasChildren: boolean`, `Initiative.children: { slug: string; title: string | null; stage: string | null; status: string | null }[]`. `TreeRow` initiative variant gains `expanded: boolean; hasChildren: boolean`; a program row's phases follow it at `depth + 1` and fold with `initiativeKey(program)`.

- [ ] **Step 1: Extend the fixture** (`__fixtures__/map.ts`): `ini()` defaults gain `parentInitiative: null, hasChildren: false`; add to the kindash product a program `ini({ slug: 'audit', title: 'UI audit', projectSlug: 'kindash', primaryCapability: 'tasks', hasChildren: true })` and a phase `ini({ slug: 'audit-01', title: '01 Restore', projectSlug: 'kindash', primaryCapability: 'tasks', parentInitiative: 'audit' })`. Update the existing order assertions in `treeRows.test.ts` and any test that enumerates the fixture (`RoadmapListColumn.test.tsx`, `RoadmapPage.test.tsx`, `CapabilityDetail.test.tsx`, `lookup.test.ts`) — run `npm run test` to find them.

- [ ] **Step 2: Write the failing tests** (append to `treeRows.test.ts`)

```ts
describe('flattenMap — program nesting', () => {
  it('emits a phase directly under its program at depth + 1', () => {
    const rows = flattenMap(MAP, EXPANDED, '')
    const k = keys(rows)
    expect(k.indexOf(initiativeKey('audit-01'))).toBe(k.indexOf(initiativeKey('audit')) + 1)
    const byKey = Object.fromEntries(rows.map((r) => [r.key, r]))
    expect(byKey[initiativeKey('audit')]).toMatchObject({ kind: 'initiative', hasChildren: true, expanded: true })
    expect(byKey[initiativeKey('audit-01')].depth).toBe(byKey[initiativeKey('audit')].depth + 1)
  })

  it('collapsing a program hides its phases only', () => {
    const state = toggleKey(EXPANDED, initiativeKey('audit'))
    const k = keys(flattenMap(MAP, state, ''))
    expect(k).toContain(initiativeKey('audit'))
    expect(k).not.toContain(initiativeKey('audit-01'))
    expect(k).toContain(initiativeKey('views'))
  })

  it('a search that hits a phase keeps its program row', () => {
    const k = keys(flattenMap(MAP, EXPANDED, 'restore'))
    expect(k).toEqual(expect.arrayContaining([initiativeKey('audit'), initiativeKey('audit-01')]))
  })
})
```

- [ ] **Step 3: Run to verify they fail**: `(cd web && npx vitest run src/features/roadmap/treeRows.test.ts)` → FAIL (`hasChildren` undefined; phase not adjacent).

- [ ] **Step 4: Implement** in `treeRows.ts`: bucket initiatives into `byParent = Map<programSlug, InitiativeNode[]>` first; `initiativeRows(list, depth)` becomes recursive:

```ts
  const initiativeRows = (list: InitiativeNode[], depth: number): TreeRow[] =>
    list.flatMap((initiative) => {
      const key = initiativeKey(initiative.slug)
      const phases = byParent.get(initiative.slug) ?? []
      const kids = initiativeRows(phases, depth + 1)
      const shown = searching ? kids.filter((r) => rowMatches(r, query) || (r.kind === 'initiative' && r.hasChildren)) : kids
      const self: TreeRow = {
        kind: 'initiative', key, depth, product, initiative,
        expanded: searching ? shown.length > 0 : !state.collapsed.has(key),
        hasChildren: searching ? shown.length > 0 : phases.length > 0,
      }
      if (searching) return rowMatches(self, query) || shown.length > 0 ? [self, ...shown] : []
      return self.expanded ? [self, ...kids] : [self]
    })
```

Only root initiatives (`parentInitiative === null` or an unknown parent) enter `byPrimary`/`orphans`; a capability's `hasChildren` and the product-level filter (`kept`) treat a program row like a capability row (keep it when a descendant matched). Update the `TreeRow` union.

- [ ] **Step 5: Run the roadmap tests**: `(cd web && npx vitest run src/features/roadmap)` → PASS after fixture-order updates. Then `npm run typecheck` and `npm run lint`.

- [ ] **Step 6: Commit**

```bash
git add web/src/api/roadmap.ts web/src/api/sdlc.ts web/src/features/roadmap/treeRows.ts web/src/features/roadmap/treeRows.test.ts web/src/features/roadmap/__fixtures__/map.ts <any updated tests>
git commit -F /tmp/c12.txt   # "feat(web): roadmap tree nests a program's phases under it"
```

---

### Task 13: Program and phase rows, nested in-flight list, initiative page

**Files:**
- Modify: `web/src/features/roadmap/rows/InitiativeCard.tsx`, `web/src/features/roadmap/RoadmapListColumn.tsx:165-300`, `web/src/features/roadmap/detail/InitiativeList.tsx`, `web/src/features/roadmap/detail/CapabilityDetail.tsx`, `web/src/features/roadmap/InitiativePage.tsx`
- Test: `RoadmapListColumn.test.tsx`, `detail/CapabilityDetail.test.tsx`, `InitiativePage.test.tsx`

**Interfaces:**
- `InitiativeCardProps` gains `expanded?: boolean; hasChildren?: boolean; onToggle?: () => void; programTitle?: string | null`. When `hasChildren`, `leading` renders the same chevron button `TreeRowCard` uses (copy its markup and `aria-expanded`; `aria-label` "Collapse"/"Expand"). When `programTitle`, a `Badge tone="neutral"` with the program title precedes the stage badge.
- Initiatives scope (grouped by stage): rows are **leaf** initiatives only (`!i.hasChildren`), each with `programTitle` looked up from the product's initiatives by `parentInitiative`.
- `InitiativeList` accepts the full initiative set of the capability and renders roots at depth 0 with phases at depth 1 (`role="list"` stays; nested rows are listitems with `aria-level` omitted).
- `InitiativePage`: a fifth tab `phases` shown only when `initiative.hasChildren`, listing `initiative.children` as links to `initiativePath(child.slug)` with `stageLabel`; the header for a phase shows "Part of <program title>" linking to the program (`parentInitiativeSlug`; the title comes from the map node with that slug via the existing map query). The Advance control is hidden for a program (the server 409s anyway).

- [ ] **Step 1: Write the failing tests**

`RoadmapListColumn.test.tsx` (append inside `describe('RoadmapListColumn')`):

```ts
  it('the Initiatives scope lists phases with their program and hides program rows', () => {
    renderColumn({ scope: 'initiatives' })
    expect(screen.queryByTestId(`row-${initiativeKey('audit')}`)).toBeNull()
    const phase = screen.getByTestId(`row-${initiativeKey('audit-01')}`)
    expect(within(phase).getByText('UI audit')).toBeInTheDocument()
  })

  it('the Map scope folds a program with ArrowLeft', () => {
    renderColumn({ scope: 'map' })
    const program = screen.getByTestId(`row-${initiativeKey('audit')}`)
    expect(program).toHaveAttribute('aria-expanded', 'true')
    fireEvent.keyDown(program, { key: 'ArrowLeft' })
    expect(screen.queryByTestId(`row-${initiativeKey('audit-01')}`)).toBeNull()
  })
```

(`renderColumn` is whatever helper the file already uses at line ~46; reuse it.)

`CapabilityDetail.test.tsx` (append): with a product whose `tasks` capability has the program + phase from the fixture, the In flight tab renders `row-initiative:audit` followed by `row-initiative:audit-01`.

`InitiativePage.test.tsx` (append): with `h.initiative` mutated to `hasChildren: true, children: [{ slug: 'audit-01', title: '01', stage: 'plan', status: 'active' }]`, a "Phases" tab exists and lists "01"; with `parentInitiativeSlug: 'audit'` and a map node titled "UI audit", the page shows "Part of UI audit"; a program shows no Advance button.

- [ ] **Step 2: Run to verify they fail**: `(cd web && npx vitest run src/features/roadmap)`.

- [ ] **Step 3: Implement** per the interface block. In `RoadmapListColumn.tsx` Map scope, pass `expanded`, `hasChildren`, `onToggle={() => setTree(toggleKey(tree, row.key))}` from the initiative `TreeRow` (the same toggle the capability rows use); extend `useTreeKeys`' ArrowLeft/ArrowRight handling to initiative rows with `hasChildren` (read how it treats capability rows and reuse the predicate). In the Initiatives scope filter `visible` to `!i.hasChildren` and compute `programTitle` from `product.initiatives.find(p => p.slug === i.parentInitiative)?.title`.

- [ ] **Step 4: Run the full web gate**: `(cd web && npm run typecheck && npm run lint && npm run test && npm run build)`. Fix every file the fixture change touched.

- [ ] **Step 5: Commit**

```bash
git add web/src/features/roadmap
git commit -F /tmp/c13.txt   # "feat(web): program rows fold their phases; phases wear their program; initiative page gains Phases"
```

---

### Task 14: Roadmap health card

**Files:**
- Create: `web/src/features/roadmap/detail/RoadmapHealthCard.tsx`, `web/src/features/roadmap/detail/RoadmapHealthCard.test.tsx`
- Modify: `web/src/api/pm.ts` (fetcher `fetchProjectHealth(slug)`), `web/src/api/hooks.ts` (`useProjectHealth(slug)`, `staleTime: 30_000`), `web/src/features/roadmap/detail/ProductDetail.tsx` (overview tab, below the tiles)

**Interfaces:**
- `ProjectHealth = { unparentedCapabilities: string[]; unlinkedInitiatives: string[]; staleProposed: string[]; staleInbox: number; generatedAt: string }`.
- `RoadmapHealthCard({ productSlug })`: renders nothing when every list is empty and `staleInbox === 0`; otherwise a `Banner`-style card titled "Roadmap hygiene" with one line per non-empty finding, each capability/initiative slug linking to its page (`capabilityPath`, `initiativePath`).

- [ ] **Step 1: Write the failing test** (`RoadmapHealthCard.test.tsx`): mock `@/api/hooks` `useProjectHealth` to return `{ data: { unparentedCapabilities: ['flaky-test-triage'], unlinkedInitiatives: [], staleProposed: ['flaky-test-triage'], staleInbox: 3, generatedAt: 'x' } }`; assert the card shows "1 capability outside the tree", "1 stale proposed capability", "3 inbox items older than 7 days", and a link to `/roadmap/kindash/capabilities/flaky-test-triage`; with all-empty data, `container.firstChild` is null.
- [ ] **Step 2: Run to verify it fails.**
- [ ] **Step 3: Implement** fetcher, hook, card, and mount it in `ProductDetail` overview. Mock the new hook in `ProductDetail.test.tsx`'s `@/api/hooks` mock (the file mocks hooks manually; a missing mock throws).
- [ ] **Step 4: Run the full web gate.**
- [ ] **Step 5: Commit, then open the web PR** (`feat(web): roadmap nesting for programs and phases; roadmap hygiene card`), ready + auto-merge, after rebasing on `origin/master`.

---

## Self-review

- **Spec coverage.** Standardize 1 (prompt writes `parent:`/outcome) → Task 9. Standardize 2 (bot issues pre-classified) → Tasks 7–8. Standardize 3 (mirror nesting; derived stage; advance/back-sync leaf-only; API parent+children; web nesting; chip) → Tasks 1, 3, 4, 5, 12, 13. Standardize 4 (prefix strip) → Tasks 2–3. Sustain (health readout) → Tasks 10, 14. Shine "advance onboarding" needs the `registered` → `map_merged` fix → Task 6.
- **Types.** `InitiativeIn.parent_slug` (Task 4) is what `pm_tree.py` passes (Task 5); `rollup_epic_progress` / `derived_stage` names match between Tasks 4 and 5; `FeatureUpsert.labels` (Task 7) is what the orchestrator writes and Task 8 reads as `Feature.route_labels`; web `hasChildren` / `parentInitiative` (Task 12) are what Task 5 emits (`hasChildren`, `parentInitiative` on map nodes; `parentInitiativeSlug`, `children` on sdlc payloads).
- **No placeholders:** every code step carries the code; the two "mirror the existing helper" notes (Task 7 planner name, Task 10 `p.repos`) name the exact line to copy from.

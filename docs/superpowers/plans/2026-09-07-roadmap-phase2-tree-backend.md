# Roadmap Phase 2: backend for the tree — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the web a single map endpoint that returns every product's capability tree, its initiatives with the capabilities they touch, and a rolled-up attention tier per node, plus a leaves endpoint for the issues under one node, with the model changes that make those relations real.

**Architecture:** Capability parent and links come from feature-doc frontmatter and are written by the existing feature-index seam; initiative-to-capability links come from the initiative create endpoint. A pure module `server/pm/tree.py` computes tiers and rollups over plain dataclasses so it is testable without a database; a new router `server/api/pm_tree.py` loads rows in a fixed number of grouped queries and hands them to that module. Dead tables and columns from the PM phase-1 groundwork are dropped in the same change.

**Tech Stack:** FastAPI + SQLAlchemy async (SQLite in tests, Postgres in prod), Alembic revision as the reviewable record, pytest.

**Spec:** `docs/superpowers/specs/2026-09-07-roadmap-drilldown-design.md` §10 (data model and API), §11 (attention tier), §13 phase 2. Read it first. Phase 1 (`docs/superpowers/plans/2026-09-07-roadmap-phase1-shell-and-dead-ends.md`, PR #1439) is the base of this branch.

## Global Constraints

- GitHub stays truth for issues, epics and blocked-by; markdown stays truth for capabilities; ccswitch owns only the overlay (parent, links, initiative↔capability, scores, triage state).
- Every new column is nullable so `_reconcile_columns` ALTER-ADDs it on both dialects; every schema change also gets an Alembic revision chained off the newest (`0033_triage_items`), as the reviewable record — nothing runs `alembic upgrade` at deploy time.
- Adding a model or dropping a table means updating `_FK_LINKS`/`_REQUIRED_EXTRAS` in `server/tests/test_postgres_compat.py`; the Postgres suite is skipped locally unless `CCSWITCH_TEST_PG_URL` is set, so update the lists by reading them, not by running them.
- Never `func.now()`; use `server/db/clock.py:db_utc_now()` if a DB-side timestamp is ever needed (none is, here).
- The attention tier vocabulary is `needs-you > blocked > in-flight > proposed > live > idle`, computed **server-side once** in `server/pm/tree.py`; Phase 3's `roadmapTier.ts` only maps tier names to tones (constitution VI: one implementation).
- Purge replaced code in the same change: `InitiativeRepo` and the four unused score columns on `initiatives` go, with their test fixtures.
- No web changes in this phase.
- Four-step server gate before pushing: `ruff check src/ server/`, `mypy server/ --ignore-missing-imports`, `pytest tests/`, `pytest server/tests/`. On rocky run with a scrubbed environment: `env -i PATH="$PATH" HOME="$HOME" LANG=C.UTF-8 CCSWITCH_DEV=1 /home/tony/GitHub/ccswitch/.venv/bin/python -m pytest … -p no:randomly`. A built `web/dist` makes two unrelated path-traversal tests 405; move it aside for a full run.
- Bash hook: never persist a `cd` into a subdirectory; never pair deploy/release/rollout/promote with prod/production in a command's text; commit messages go in a file and `git commit -F`.
- Commit trailer: `Co-Authored-By: Claude <model> <noreply@anthropic.com>` for the running model.

## File map

| File | Responsibility |
|------|----------------|
| `server/pm/featuredoc.py` | Parse `parent:` from frontmatter into `FeatureDoc.parent`; serialize it back. |
| `server/pm/feature_index.py` | Plan carries `parent_slug`, `links` and cycle `warnings`; cycles fall back to root. |
| `server/db/models.py` | `Feature.parent_feature_id`, `InitiativeFeature.is_primary`; delete `InitiativeRepo` and `Initiative.reach/impact/effort/confidence`. |
| `server/db/migrations/versions/0035_roadmap_tree.py` | The reviewable record of the above. |
| `server/sessions/orchestrator.py` | `_pm_sync_apply_feature_plan` writes parents and rewrites feature links. |
| `server/pm/tree.py` | Pure: tier vocabulary, per-node tier rules, rollup, tree builder over dataclasses. |
| `server/api/sdlc.py` | Create body accepts capabilities; every initiative payload carries `projectSlug`, `primaryCapability`, `capabilities`. |
| `server/api/pm_tree.py` | `GET /api/pm/map`, `GET /api/pm/leaves`. |
| `server/app.py` | Registers the new router beside `pm_router`. |
| `server/tests/test_pm_featuredoc.py`, `test_pm_feature_index.py`, `test_pm_models.py`, `test_postgres_compat.py`, `test_sdlc_api.py` | Updated. |
| `server/tests/test_pm_tree.py`, `test_pm_feature_apply.py`, `test_pm_map_api.py` | New. |

Deviations from spec §10, with reasons:
- The `feature_map_changes` table is **not** created here. Its first writer is Phase 5's map-changes endpoint; creating it now would be a table with no reader or writer.
- The spec's §11 module name `features/products/roadmapTier.ts` stays for the web, but the tier is computed server-side (Global Constraints) so that the map endpoint's rollup counts and tiers come from one function.
- Spec §11's "artifact PR open" tier source is not implemented: `InitiativeArtifact` records `pr_number`/`pr_url` but no PR state, so "open" is not derivable without a live GitHub read. Deferred until an artifact PR-state column exists (Phase 4, with the initiative edit endpoint).
- The board-Status tier rows ("Needs human", "Blocked", "In progress") are implemented via `GhProjectItem.status`, last-write-wins across boards.

---

### Task 1: Feature doc parses and serializes `parent`

**Files:**
- Modify: `server/pm/featuredoc.py:28-34,53-60,80,83-91`
- Test: `server/tests/test_pm_featuredoc.py`

**Interfaces:**
- Produces: `FeatureDoc.parent: str | None` (a sibling slug within the same product, or `None` for a root). `parse_feature_doc` reads `parent:` from frontmatter, strips it, treats blank as `None`, and rejects a doc whose `parent` equals its own `slug` with `FeatureDocError("parent cannot be the doc itself")`. `serialize_feature_doc` emits `parent:` only when set.

- [ ] **Step 1: Write the failing tests**

Append to `server/tests/test_pm_featuredoc.py`:

```python
WITH_PARENT = """---
slug: server-side-stt
title: Server-side STT
state: delivering
parent: dictation
---
## Problem

Whisper on the edge.
"""


def test_parse_parent_slug():
    doc = parse_feature_doc(WITH_PARENT)
    assert doc.parent == "dictation"


def test_parse_missing_or_blank_parent_is_root():
    assert parse_feature_doc(VALID).parent is None
    blank = VALID.replace("state: live\n", "state: live\nparent: '  '\n")
    assert parse_feature_doc(blank).parent is None


def test_parent_equal_to_self_is_rejected():
    bad = WITH_PARENT.replace("parent: dictation", "parent: server-side-stt")
    with pytest.raises(FeatureDocError):
        parse_feature_doc(bad)


def test_serialize_round_trips_parent():
    doc = parse_feature_doc(WITH_PARENT)
    text = serialize_feature_doc(doc)
    assert "parent: dictation" in text
    assert parse_feature_doc(text).parent == "dictation"
    # A root doc must not gain a `parent:` line.
    assert "parent:" not in serialize_feature_doc(parse_feature_doc(VALID))
```

- [ ] **Step 2: Run to see them fail**

Run: `env -i PATH="$PATH" HOME="$HOME" LANG=C.UTF-8 CCSWITCH_DEV=1 /home/tony/GitHub/ccswitch/.venv/bin/python -m pytest server/tests/test_pm_featuredoc.py -q -p no:randomly`
Expected: FAIL — `FeatureDoc` has no attribute `parent`.

- [ ] **Step 3: Implement**

In `server/pm/featuredoc.py`, add a field to `FeatureDoc` (after `links`):

```python
    parent: str | None = None
```

In `parse_feature_doc`, after the `state` validation, add:

```python
    parent_raw = meta.get("parent")
    parent = str(parent_raw).strip() if parent_raw is not None else ""
    if parent == slug:
        raise FeatureDocError("parent cannot be the doc itself")
```

and change the return to `FeatureDoc(slug=slug, title=title, state=state, links=tuple(links), body=body, parent=parent or None)`.

In `serialize_feature_doc`, after the `state` key is set and before `links`, add:

```python
    if doc.parent:
        meta["parent"] = doc.parent
```

- [ ] **Step 4: Run the tests**

Run: the same command. Expected: PASS, no warnings.

- [ ] **Step 5: Commit**

```bash
printf 'feat(pm): feature docs carry a parent slug\n\nRefs #993\n\nCo-Authored-By: Claude <model> <noreply@anthropic.com>\n' > /tmp/p2-t1.txt
git add server/pm/featuredoc.py server/tests/test_pm_featuredoc.py
git commit -F /tmp/p2-t1.txt
```

---

### Task 2: Feature-index plan carries parent, links and cycle warnings

**Files:**
- Modify: `server/pm/feature_index.py`
- Test: `server/tests/test_pm_feature_index.py`

**Interfaces:**
- Consumes: `FeatureDoc.parent`, `FeatureDoc.links` (`FeatureLinkRef(kind, project, feature)`).
- Produces:
  - `FeatureUpsert` gains `parent_slug: str | None` and `links: tuple[FeatureLinkRef, ...]`.
  - `FeatureIndexPlan` gains `warnings: tuple[str, ...]`.
  - Rules: a `parent_slug` naming a slug not in this doc set becomes `None` with warning `"<slug>: parent '<parent>' not found; indexed at root"`; a cycle (following parents returns to the start) breaks at the doc that closes it, which becomes root with warning `"<slug>: parent chain is a cycle; indexed at root"`.

- [ ] **Step 1: Write the failing tests**

Append to `server/tests/test_pm_feature_index.py`:

```python
from server.pm.featuredoc import FeatureLinkRef


def _pdoc(slug: str, parent: str | None, links=()) -> FeatureDoc:
    return FeatureDoc(slug=slug, title=slug.title(), state="live", links=tuple(links),
                      body="## Problem\n\nx", parent=parent)


def test_parent_and_links_are_carried_onto_the_upsert():
    link = FeatureLinkRef("powered_by", "ccswitch", "sessions")
    plan = plan_feature_index([], [
        ("d/sessions.md", _pdoc("sessions", None)),
        ("d/dictation.md", _pdoc("dictation", "sessions", [link])),
    ])
    by_slug = {u.slug: u for u in plan.upserts}
    assert by_slug["sessions"].parent_slug is None
    assert by_slug["dictation"].parent_slug == "sessions"
    assert by_slug["dictation"].links == (link,)
    assert plan.warnings == ()


def test_unknown_parent_indexes_at_root_with_a_warning():
    plan = plan_feature_index([], [("d/a.md", _pdoc("a", "nope"))])
    assert plan.upserts[0].parent_slug is None
    assert plan.warnings == ("a: parent 'nope' not found; indexed at root",)


def test_parent_cycle_breaks_at_the_closing_doc():
    plan = plan_feature_index([], [
        ("d/a.md", _pdoc("a", "c")),
        ("d/b.md", _pdoc("b", "a")),
        ("d/c.md", _pdoc("c", "b")),
    ])
    by_slug = {u.slug: u for u in plan.upserts}
    roots = [s for s, u in by_slug.items() if u.parent_slug is None]
    # Exactly one doc is cut loose, and the other two keep their parents.
    assert len(roots) == 1
    assert len(plan.warnings) == 1
    assert plan.warnings[0].endswith("parent chain is a cycle; indexed at root")
```

- [ ] **Step 2: Run to see them fail**

Run: `env -i PATH="$PATH" HOME="$HOME" LANG=C.UTF-8 CCSWITCH_DEV=1 /home/tony/GitHub/ccswitch/.venv/bin/python -m pytest server/tests/test_pm_feature_index.py -q -p no:randomly`
Expected: FAIL — `FeatureUpsert` has no field `parent_slug`.

- [ ] **Step 3: Implement**

Replace `server/pm/feature_index.py` from the `FeatureUpsert` dataclass to the end with:

```python
@dataclass(frozen=True)
class FeatureUpsert:
    slug: str
    title: str
    state: str
    summary: str
    repo_path: str
    parent_slug: str | None
    links: tuple[FeatureLinkRef, ...]


@dataclass(frozen=True)
class FeatureIndexPlan:
    upserts: tuple[FeatureUpsert, ...]
    orphan_slugs: tuple[str, ...]
    revive_slugs: tuple[str, ...]
    warnings: tuple[str, ...]


def _summary(body: str) -> str:
    for line in body.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            return stripped[:_SUMMARY_MAX]
    return ""


def _resolve_parents(docs: list[tuple[str, FeatureDoc]]) -> tuple[dict[str, str | None], list[str]]:
    """Map slug → parent slug, dropping unknown parents and breaking cycles.

    Docs are visited in the order given; a cycle is broken at the FIRST doc
    (in that order) whose parent chain returns to itself, so the outcome is
    deterministic for a given doc listing."""
    parent: dict[str, str | None] = {}
    warnings: list[str] = []
    slugs = {doc.slug for _, doc in docs}
    for _, doc in docs:
        if doc.parent is None:
            parent[doc.slug] = None
        elif doc.parent not in slugs:
            parent[doc.slug] = None
            warnings.append(f"{doc.slug}: parent '{doc.parent}' not found; indexed at root")
        else:
            parent[doc.slug] = doc.parent
    for _, doc in docs:
        seen = {doc.slug}
        cur = parent.get(doc.slug)
        while cur is not None:
            if cur in seen:
                parent[doc.slug] = None
                warnings.append(f"{doc.slug}: parent chain is a cycle; indexed at root")
                break
            seen.add(cur)
            cur = parent.get(cur)
    return parent, warnings


def plan_feature_index(
    existing: list[FeatureRowView],
    docs: list[tuple[str, FeatureDoc]],
) -> FeatureIndexPlan:
    seen = {doc.slug for _, doc in docs}
    parent_of, warnings = _resolve_parents(docs)
    upserts = tuple(
        FeatureUpsert(slug=doc.slug, title=doc.title, state=doc.state,
                      summary=_summary(doc.body), repo_path=path,
                      parent_slug=parent_of.get(doc.slug), links=doc.links)
        for path, doc in docs)
    orphan_slugs = tuple(
        row.slug for row in existing if not row.orphaned and row.slug not in seen)
    revive_slugs = tuple(
        row.slug for row in existing if row.orphaned and row.slug in seen)
    return FeatureIndexPlan(upserts=upserts, orphan_slugs=orphan_slugs,
                            revive_slugs=revive_slugs, warnings=tuple(warnings))
```

and change the import line to `from server.pm.featuredoc import FeatureDoc, FeatureLinkRef`.

- [ ] **Step 4: Run the tests and every other constructor of these dataclasses**

Run: `grep -rn "FeatureUpsert(\|FeatureIndexPlan(" server/ | grep -v "server/pm/feature_index.py"` — for each hit outside this module (e.g. `server/tests/test_pm_sync_loop.py`), add `parent_slug=None, links=()` / `warnings=()` to the constructor call. Then:
`env -i PATH="$PATH" HOME="$HOME" LANG=C.UTF-8 CCSWITCH_DEV=1 /home/tony/GitHub/ccswitch/.venv/bin/python -m pytest server/tests/test_pm_feature_index.py server/tests/test_pm_sync_loop.py -q -p no:randomly`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
printf 'feat(pm): feature-index plan carries parent, links and cycle warnings\n\nRefs #993\n\nCo-Authored-By: Claude <model> <noreply@anthropic.com>\n' > /tmp/p2-t2.txt
git add server/pm/feature_index.py server/tests/
git commit -F /tmp/p2-t2.txt
```

---

### Task 3: Model changes and the migration record

**Files:**
- Modify: `server/db/models.py:1441-1466` (Feature), `:1164-1207` (Initiative), `:1676-1706` (InitiativeRepo, InitiativeFeature)
- Create: `server/db/migrations/versions/0035_roadmap_tree.py`
- Modify: `server/tests/test_postgres_compat.py:259`, `server/tests/test_pm_models.py:17-18,113-126`

**Interfaces:**
- Produces: `Feature.parent_feature_id: str | None` (FK `features.id`, nullable, indexed); `InitiativeFeature.is_primary: bool | None` (default `False`); `InitiativeRepo` class removed; `Initiative.reach/impact/effort/confidence` removed.

- [ ] **Step 1: Update the model smoke test first**

In `server/tests/test_pm_models.py`: remove `InitiativeRepo` from the import list; replace `test_initiative_pm_extensions` with:

```python
@pytest.mark.asyncio
async def test_initiative_capabilities_and_feature_parent(db_session):
    p = Project(slug="px", name="PX")
    db_session.add(p)
    await db_session.flush()
    root = Feature(project_id=p.id, slug="sessions", title="Sessions", state="live")
    db_session.add(root)
    await db_session.flush()
    child = Feature(project_id=p.id, slug="dictation", title="Dictation",
                    state="delivering", parent_feature_id=root.id)
    ini = Initiative(slug="do-fx", repo="o/r", title="Do FX", project_id=p.id)
    db_session.add_all([child, ini])
    await db_session.flush()
    db_session.add(InitiativeFeature(initiative_id=ini.id, feature_id=child.id, is_primary=True))
    db_session.add(InitiativeFeature(initiative_id=ini.id, feature_id=root.id))
    await db_session.flush()
    rows = (await db_session.execute(
        select(InitiativeFeature).where(InitiativeFeature.initiative_id == ini.id))).scalars().all()
    assert {(r.feature_id, bool(r.is_primary)) for r in rows} == {(child.id, True), (root.id, False)}
    assert not hasattr(Initiative, "reach")
```

- [ ] **Step 2: Run to see it fail**

Run: `env -i PATH="$PATH" HOME="$HOME" LANG=C.UTF-8 CCSWITCH_DEV=1 /home/tony/GitHub/ccswitch/.venv/bin/python -m pytest server/tests/test_pm_models.py -q -p no:randomly`
Expected: FAIL — `parent_feature_id` is an invalid keyword argument for `Feature`.

- [ ] **Step 3: Change the models**

In `Feature`, after `project_id`, add:

```python
    # Capability tree (roadmap spec D2): one parent per capability, from the doc's
    # `parent:` frontmatter. NULL = root. Cycles are broken at index time.
    parent_feature_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("features.id"), nullable=True, index=True)
```

In `Initiative`, delete the four score columns and their comment (`# RICE-lite score inputs …` through `confidence`). Keep `project_id`.

Delete the whole `InitiativeRepo` class. In `InitiativeFeature`, after `feature_id`, add:

```python
    # The capability an initiative lives under in the tree; the others it touches
    # render as link chips. Exactly one row per initiative should be primary.
    is_primary: Mapped[bool | None] = mapped_column(Boolean, nullable=True, default=False)
```

and update its docstring to `"""Which capabilities an initiative creates or changes (roadmap spec §10)."""`.

- [ ] **Step 4: Write the migration record**

Create `server/db/migrations/versions/0035_roadmap_tree.py`:

```python
"""Roadmap drill-down Phase 2 (#993): capability tree and initiative↔capability links.

Revision ID: 0035_roadmap_tree
Revises: 0034_pr_resolution_source_tool
Create Date: 2026-09-07

Reviewable record only: nothing runs `alembic upgrade` at deploy time. The two
nullable ADDs are applied by init_db's _reconcile_columns on both dialects at
next boot. The DROPs are recorded here for the day a migration is applied by
hand; reconcile never drops, and the drift assert only checks for MISSING
columns, so the leftover DB columns/table are harmless until then.
"""
from alembic import op
import sqlalchemy as sa

revision = "0035_roadmap_tree"
down_revision = "0034_pr_resolution_source_tool"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("features", sa.Column(
        "parent_feature_id", sa.String(), sa.ForeignKey("features.id"), nullable=True))
    op.create_index("ix_features_parent_feature_id", "features", ["parent_feature_id"])
    op.add_column("initiative_features", sa.Column("is_primary", sa.Boolean(), nullable=True))
    with op.batch_alter_table("initiatives") as batch:
        for col in ("reach", "impact", "effort", "confidence"):
            batch.drop_column(col)
    op.drop_table("initiative_repos")


def downgrade() -> None:
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
    with op.batch_alter_table("initiatives") as batch:
        for col in ("reach", "impact", "effort", "confidence"):
            batch.add_column(sa.Column(col, sa.Integer(), nullable=True))
    op.drop_column("initiative_features", "is_primary")
    op.drop_index("ix_features_parent_feature_id", table_name="features")
    op.drop_column("features", "parent_feature_id")
```

- [ ] **Step 5: Update the Postgres compatibility lists and every other reader**

In `server/tests/test_postgres_compat.py` delete the line `"initiative_repos": {"initiative_id": "initiatives"},`. Then run `grep -rn "InitiativeRepo\|initiative_repos\|\.reach\b" server/ src/ --include=*.py | grep -v migrations` and remove every remaining reference to `InitiativeRepo` (the `.reach` hits must all be on `WorkItem`/`TriageProposal`, never `Initiative`).

- [ ] **Step 6: Run the model, reconcile and schema tests**

Run: `env -i PATH="$PATH" HOME="$HOME" LANG=C.UTF-8 CCSWITCH_DEV=1 /home/tony/GitHub/ccswitch/.venv/bin/python -m pytest server/tests/test_pm_models.py server/tests/test_db_schema_reconcile.py server/tests/test_schema_reconcile_coverage.py server/tests/test_postgres_compat.py server/tests/test_pg_migration_helpers.py -q -p no:randomly`
Expected: PASS (Postgres cases skip without `CCSWITCH_TEST_PG_URL`).

- [ ] **Step 7: Commit**

```bash
printf 'feat(db): capability parent, initiative primary capability; drop dead initiative_repos and score columns\n\nMigration 0035_roadmap_tree records the change; reconcile applies the adds.\n\nRefs #993\n\nCo-Authored-By: Claude <model> <noreply@anthropic.com>\n' > /tmp/p2-t3.txt
git add server/db/models.py server/db/migrations/versions/0035_roadmap_tree.py server/tests/test_postgres_compat.py server/tests/test_pm_models.py
git commit -F /tmp/p2-t3.txt
```

---

### Task 4: The sync loop writes parents and feature links

**Files:**
- Modify: `server/sessions/orchestrator.py:5855-5904` (`_pm_sync_apply_feature_plan`)
- Test: `server/tests/test_pm_feature_apply.py` (new)

**Interfaces:**
- Consumes: `FeatureUpsert.parent_slug`, `.links`, `FeatureIndexPlan.warnings`.
- Produces: after apply, every non-orphaned `Feature` of the project has `parent_feature_id` set to the row of `parent_slug` (same project) or `NULL`; the project's outgoing `FeatureLink` rows equal the docs' links, resolved by `(Project.slug, Feature.slug)`, unresolved targets skipped with a warning log; plan warnings are logged at WARNING.

- [ ] **Step 1: Write the failing test**

Create `server/tests/test_pm_feature_apply.py`:

```python
"""`_pm_sync_apply_feature_plan` writes the capability tree and feature links (#993)."""
from __future__ import annotations

import asyncio
import os
import tempfile
from contextlib import contextmanager
from unittest.mock import patch

IDENTITY_HEADERS = {"Tailscale-User-Login": "testuser@example.com"}


@contextmanager
def _app():
    from fastapi.testclient import TestClient
    from server.app import create_app
    from server.sessions.orchestrator import SessionOrchestrator
    from server.tests.fakes.fake_factory import fake_sdk_factory

    _clear = ["CCSWITCH_SESSIOND_ENDPOINT", "CCSWITCH_SESSIOND_SOCKET", "CCSWITCH_SESSIOND_HOST",
              "CCSWITCH_SESSIOND_PORT", "CCSWITCH_SESSIOND_CLIENT_CERT",
              "CCSWITCH_SESSIOND_CLIENT_KEY", "CCSWITCH_SESSIOND_CA"]
    with tempfile.TemporaryDirectory() as tmp:
        saved = {k: os.environ.pop(k, None) for k in _clear}
        try:
            with patch.dict(os.environ, {"CCSWITCH_DB_URL": f"sqlite+aiosqlite:///{tmp}/t.db",
                                         "CCSWITCH_DEV": "1", "CCSWITCH_TAILNET_ALLOWLIST": ""}):
                orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())
                app = create_app(orchestrator=orch)
                with TestClient(app, raise_server_exceptions=False):
                    yield orch
        finally:
            for k, v in saved.items():
                if v is not None:
                    os.environ[k] = v


def test_apply_writes_parents_and_links_and_rewrites_stale_links():
    from sqlalchemy import select
    from server.db import db_session
    from server.db.models import Feature, FeatureLink, Project
    from server.pm.feature_index import FeatureIndexPlan, FeatureUpsert
    from server.pm.featuredoc import FeatureLinkRef

    with _app() as orch:
        async def _seed() -> tuple[str, str]:
            async with db_session() as db:
                infra = Project(slug="example-infra", name="Example infra")
                prod = Project(slug="example-product", name="Example product")
                db.add_all([infra, prod])
                await db.flush()
                mq = Feature(project_id=infra.id, slug="message-queue", title="MQ", state="live")
                stale_dst = Feature(project_id=prod.id, slug="old", title="Old", state="live")
                db.add_all([mq, stale_dst])
                await db.flush()
                await db.commit()
                return prod.id, prod.slug
        prod_id, _ = asyncio.run(_seed())

        def _up(slug, parent, links=()):
            return FeatureUpsert(slug=slug, title=slug, state="live", summary="", repo_path=f"d/{slug}.md",
                                 parent_slug=parent, links=tuple(links))

        plan1 = FeatureIndexPlan(
            upserts=(_up("sessions", None), _up("dictation", "sessions",
                                                  [FeatureLinkRef("powered_by", "example-infra", "message-queue"),
                                                   FeatureLinkRef("integrates_with", "example-product", "old")])),
            orphan_slugs=(), revive_slugs=(), warnings=("dictation: sample warning",))
        asyncio.run(orch._pm_sync_apply_feature_plan(prod_id, plan1, {}))

        async def _read():
            async with db_session() as db:
                feats = {f.slug: f for f in (await db.execute(
                    select(Feature).where(Feature.project_id == prod_id))).scalars().all()}
                links = (await db.execute(select(FeatureLink).where(
                    FeatureLink.src_feature_id == feats["dictation"].id))).scalars().all()
                return feats, links
        feats, links = asyncio.run(_read())
        assert feats["sessions"].parent_feature_id is None
        assert feats["dictation"].parent_feature_id == feats["sessions"].id
        assert sorted(l.kind for l in links) == ["integrates_with", "powered_by"]


def test_second_apply_rewrites_links():
    """The link set after a second apply is exactly the resolvable links of the
    second plan: a link that left the doc is gone, an unresolvable one is skipped."""
    from sqlalchemy import select
    from server.db import db_session
    from server.db.models import Feature, FeatureLink, Project
    from server.pm.feature_index import FeatureIndexPlan, FeatureUpsert
    from server.pm.featuredoc import FeatureLinkRef

    with _app() as orch:
        async def _seed() -> str:
            async with db_session() as db:
                infra = Project(slug="example-infra", name="Example infra")
                prod = Project(slug="example-product", name="Example product")
                db.add_all([infra, prod])
                await db.flush()
                db.add(Feature(project_id=infra.id, slug="message-queue", title="MQ", state="live"))
                db.add(Feature(project_id=prod.id, slug="old", title="Old", state="live"))
                await db.commit()
                return prod.id
        prod_id = asyncio.run(_seed())

        def _plan(links):
            return FeatureIndexPlan(
                upserts=(FeatureUpsert(slug="dictation", title="D", state="live", summary="",
                                       repo_path="d/dictation.md", parent_slug=None, links=tuple(links)),),
                orphan_slugs=(), revive_slugs=(), warnings=())

        asyncio.run(orch._pm_sync_apply_feature_plan(prod_id, _plan([
            FeatureLinkRef("powered_by", "example-infra", "message-queue"),
            FeatureLinkRef("integrates_with", "example-product", "old")]), {}))
        asyncio.run(orch._pm_sync_apply_feature_plan(prod_id, _plan([
            FeatureLinkRef("powered_by", "example-infra", "message-queue"),
            FeatureLinkRef("powered_by", "nope", "nope")]), {}))

        async def _links():
            async with db_session() as db:
                src = (await db.execute(select(Feature).where(
                    Feature.project_id == prod_id, Feature.slug == "dictation"))).scalar_one()
                dst = (await db.execute(select(Feature).where(Feature.slug == "message-queue"))).scalar_one()
                rows = (await db.execute(select(FeatureLink).where(
                    FeatureLink.src_feature_id == src.id))).scalars().all()
                return [(r.kind, r.dst_feature_id) for r in rows], dst.id
        rows, mq_id = asyncio.run(_links())
        assert rows == [("powered_by", mq_id)]
```

- [ ] **Step 2: Run to see it fail**

Run: `env -i PATH="$PATH" HOME="$HOME" LANG=C.UTF-8 CCSWITCH_DEV=1 /home/tony/GitHub/ccswitch/.venv/bin/python -m pytest server/tests/test_pm_feature_apply.py -q -p no:randomly`
Expected: FAIL — `parent_feature_id` is `None` for `dictation` and no `FeatureLink` rows exist.

- [ ] **Step 3: Implement**

Replace `_pm_sync_apply_feature_plan` in `server/sessions/orchestrator.py` with:

```python
    async def _pm_sync_apply_feature_plan(
        self, project_id: str, plan, by_slug: dict
    ) -> None:
        """Write upserts/orphans from *plan* into the Feature table, then the
        capability tree (parent) and the project's outgoing feature links.

        Parents resolve by slug within the project after every upsert row
        exists, so a child listed before its parent still links. Links are
        REWRITTEN each apply — the docs are truth, so a link that left the
        frontmatter leaves the table — and resolve by (Project.slug,
        Feature.slug); an unresolvable target is skipped with a warning rather
        than failing the apply. Plan warnings (unknown parent, cycle) are
        logged the same way.

        After upserting, if the project is in ``map_proposed`` and now has ≥1
        non-orphaned Feature, advance it to ``map_merged``.  This ties the
        transition to the observable "map merged & indexed" state — no PR
        webhook needed.  No other states are touched."""
        from sqlalchemy import delete, func, select
        from server.db.models import Feature, FeatureLink, Project
        for warning in getattr(plan, "warnings", ()):
            log.warning("pm_sync.feature_index project=%s %s", project_id, warning)
        async with db_session() as db:
            for up in plan.upserts:
                row = (await db.execute(
                    select(Feature).where(
                        Feature.project_id == project_id,
                        Feature.slug == up.slug,
                    )
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
                    select(Feature).where(
                        Feature.project_id == project_id,
                        Feature.slug == slug,
                    )
                )).scalar_one_or_none()
                if row is not None:
                    row.orphaned = True
            await db.flush()

            # Tree: parent by slug, within this project only.
            mine = {f.slug: f for f in (await db.execute(
                select(Feature).where(Feature.project_id == project_id))).scalars().all()}
            for up in plan.upserts:
                row = mine.get(up.slug)
                if row is None:
                    continue
                parent = mine.get(up.parent_slug) if up.parent_slug else None
                row.parent_feature_id = parent.id if parent is not None else None

            # Links: rewrite every outgoing edge of this project's features.
            src_ids = [f.id for f in mine.values()]
            if src_ids:
                await db.execute(delete(FeatureLink).where(FeatureLink.src_feature_id.in_(src_ids)))
            wanted = [(up.slug, link) for up in plan.upserts for link in up.links]
            if wanted:
                project_slugs = {link.project for _, link in wanted}
                projects = {p.slug: p.id for p in (await db.execute(
                    select(Project).where(Project.slug.in_(project_slugs)))).scalars().all()}
                targets = {(f.project_id, f.slug): f.id for f in (await db.execute(
                    select(Feature).where(Feature.project_id.in_(list(projects.values()))))).scalars().all()}
                for src_slug, link in wanted:
                    src = mine.get(src_slug)
                    dst_project = projects.get(link.project)
                    dst_id = targets.get((dst_project, link.feature)) if dst_project else None
                    if src is None or dst_id is None:
                        log.warning("pm_sync.feature_link_unresolved project=%s src=%s -> %s/%s",
                                    project_id, src_slug, link.project, link.feature)
                        continue
                    db.add(FeatureLink(src_feature_id=src.id, dst_feature_id=dst_id, kind=link.kind))

            # Advance map_proposed → map_merged when ≥1 non-orphaned feature
            # exists after this upsert.  Other states are intentionally left alone.
            proj = (await db.execute(
                select(Project).where(Project.id == project_id)
            )).scalar_one_or_none()
            if proj is not None and proj.onboarding_state == "map_proposed":
                live_count = (await db.execute(
                    select(func.count(Feature.id)).where(
                        Feature.project_id == project_id,
                        Feature.orphaned.is_not(True),
                    )
                )).scalar_one()
                if live_count >= 1:
                    proj.onboarding_state = "map_merged"
```

(`log` is the module logger already used throughout `orchestrator.py`; confirm the name with `grep -n "^log = \|^logger = " server/sessions/orchestrator.py` and use whichever it is.)

- [ ] **Step 4: Run the new test and the sync-loop tests**

Run: `env -i PATH="$PATH" HOME="$HOME" LANG=C.UTF-8 CCSWITCH_DEV=1 /home/tony/GitHub/ccswitch/.venv/bin/python -m pytest server/tests/test_pm_feature_apply.py server/tests/test_pm_sync_loop.py server/tests/test_pm_orchestrator_seams.py -q -p no:randomly`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
printf 'feat(pm): sync writes the capability tree and rewrites feature links from the docs\n\nRefs #993\n\nCo-Authored-By: Claude <model> <noreply@anthropic.com>\n' > /tmp/p2-t4.txt
git add server/sessions/orchestrator.py server/tests/test_pm_feature_apply.py
git commit -F /tmp/p2-t4.txt
```

---

### Task 5: `server/pm/tree.py` — tiers, rollup, tree builder (pure)

**Files:**
- Create: `server/pm/tree.py`
- Test: `server/tests/test_pm_tree.py`

**Interfaces:**
- Produces (all pure, no I/O):
  - `TIERS: tuple[str, ...] = ("needs-you", "blocked", "in-flight", "proposed", "live", "idle")` and `max_tier(*tiers: str) -> str` (highest wins; unknown strings count as `idle`).
  - `initiative_tier(status, stage, epic_blocked, epic_needs_human) -> str`.
  - `capability_own_tier(state) -> str` (`live` → `"live"`, `proposed` → `"proposed"`, else `"idle"`).
  - `issue_tier(state, triage_state, has_open_blocker, session_status) -> str`.
  - `incident_tier(status) -> str` (`open`/`scheduled` → `needs-you`; `diagnosing`/`queued` → `in-flight`; else `idle`).
  - Dataclasses `CapabilityIn`, `InitiativeIn`, `ProductIn` (inputs) and `build_product_tree(product: ProductIn) -> dict` returning the JSON-ready node described below, with rollups.

Node shapes (the wire format Phase 3 renders):

```
capability: {id, slug, title, state, summary, docPath, commitSha, prNumber, parentSlug, tier,
             counts: {openIssues, inFlight, blocked, needsYou}, links: [{kind, projectSlug, featureSlug}],
             initiatives: [slug…], children: [capability…]}
initiative: {slug, title, stage, status, projectSlug, primaryCapability, capabilities: [slug…],
             tier, epic: {ref, url, total, done, blocked, needsHuman} | null}
product:    {slug, name, tier, counts: {capabilities, inFlight, blocked, needsYou, awaitingTriage, needsHome},
             capabilities: [root capability…], initiatives: [initiative…]}
```

Rollup rules: a capability's `tier` is `max_tier(own, every child's tier, every initiative-under-it tier, issue tiers under it)`; its `counts` sum its own direct issue counts plus its children's counts plus initiatives whose primary capability is it (`inFlight` counts initiatives with tier `in-flight`, `blocked` those with tier `blocked`, `needsYou` those with tier `needs-you`). A product's `tier` is `max_tier(all root capabilities, all initiatives, "needs-you" if awaitingTriage > 0 or needsHome > 0, incident tiers)`; its counts sum the roots plus `capabilities` (total non-orphaned) and the two pseudo-node counts. Initiatives whose primary capability is missing or orphaned appear in `product.initiatives` only (still counted in product rollup).

- [ ] **Step 1: Write the failing tests**

Create `server/tests/test_pm_tree.py`:

```python
"""Pure attention-tier and rollup rules for the roadmap tree (spec §11)."""
from server.pm.tree import (
    TIERS, CapabilityIn, InitiativeIn, ProductIn, build_product_tree,
    capability_own_tier, incident_tier, initiative_tier, issue_tier, max_tier,
)


def test_tier_order_and_max():
    assert TIERS == ("needs-you", "blocked", "in-flight", "proposed", "live", "idle")
    assert max_tier("idle", "live", "blocked", "proposed") == "blocked"
    assert max_tier() == "idle"
    assert max_tier("bogus", "live") == "live"


def test_initiative_tier_rules():
    assert initiative_tier("awaiting_human", "design", 0, 0) == "needs-you"
    assert initiative_tier("active", "build", 0, 2) == "needs-you"      # epic needs-human wins
    assert initiative_tier("blocked", "build", 0, 0) == "blocked"
    assert initiative_tier("active", "build", 1, 0) == "blocked"        # open blocker in the epic
    assert initiative_tier("active", "intake", 0, 0) == "in-flight"
    assert initiative_tier("active", "deploy", 0, 0) == "in-flight"
    assert initiative_tier("proposed", "intake", 0, 0) == "proposed"
    assert initiative_tier("done", "done", 0, 0) == "live"
    assert initiative_tier("active", "done", 0, 0) == "live"
    assert initiative_tier(None, None, 0, 0) == "idle"


def test_issue_and_capability_and_incident_tiers():
    assert issue_tier("closed", "classified", False, None) == "live"
    assert issue_tier("merged", "classified", False, None) == "live"
    assert issue_tier("open", "needs_human", False, None) == "needs-you"
    assert issue_tier("open", "classified", False, "waiting_input") == "needs-you"
    assert issue_tier("open", "classified", True, None) == "blocked"
    assert issue_tier("open", "classified", False, "in_progress") == "in-flight"
    assert issue_tier("open", "classified", False, None) == "idle"
    assert capability_own_tier("live") == "live"
    assert capability_own_tier("proposed") == "proposed"
    assert capability_own_tier("delivering") == "idle"
    assert incident_tier("open") == "needs-you"
    assert incident_tier("scheduled") == "needs-you"
    assert incident_tier("diagnosing") == "in-flight"
    assert incident_tier("dismissed") == "idle"


def _cap(slug, parent=None, state="live", issues=(), links=()):
    return CapabilityIn(id=f"id-{slug}", slug=slug, title=slug.title(), state=state, summary="",
                        doc_path=f"d/{slug}.md", commit_sha=None, pr_number=None, parent_slug=parent,
                        issue_tiers=tuple(issues), links=tuple(links))


def test_build_product_tree_nests_and_rolls_up():
    product = ProductIn(
        slug="example-product", name="Example",
        capabilities=(
            _cap("sessions"),
            _cap("dictation", parent="sessions", issues=("idle", "blocked")),
            _cap("stt", parent="dictation", state="delivering"),
            _cap("accounts"),
        ),
        initiatives=(
            InitiativeIn(slug="whisper", title="Whisper", stage="build", status="active",
                         primary_slug="stt", capability_slugs=("stt", "dictation"),
                         epic={"ref": "o/r#1", "url": "u", "total": 7, "done": 3, "blocked": 0, "needsHuman": 1}),
            InitiativeIn(slug="orphan", title="Orphan", stage="intake", status="proposed",
                         primary_slug=None, capability_slugs=(), epic=None),
        ),
        awaiting_triage=0, needs_home=0, incident_statuses=(),
    )
    tree = build_product_tree(product)
    roots = {c["slug"]: c for c in tree["capabilities"]}
    assert set(roots) == {"sessions", "accounts"}
    dictation = roots["sessions"]["children"][0]
    stt = dictation["children"][0]
    assert stt["initiatives"] == ["whisper"]
    assert stt["tier"] == "needs-you"                      # epic needsHuman=1 → initiative needs-you → rolls up
    assert dictation["tier"] == "needs-you"
    assert dictation["counts"] == {"openIssues": 2, "inFlight": 0, "blocked": 1, "needsYou": 1}
    assert roots["sessions"]["tier"] == "needs-you"
    assert roots["accounts"]["tier"] == "live"
    assert roots["accounts"]["counts"]["openIssues"] == 0
    by_slug = {i["slug"]: i for i in tree["initiatives"]}
    assert by_slug["whisper"]["primaryCapability"] == "stt"
    assert by_slug["whisper"]["capabilities"] == ["stt", "dictation"]
    assert by_slug["orphan"]["tier"] == "proposed"
    assert tree["tier"] == "needs-you"
    assert tree["counts"]["capabilities"] == 4
    assert tree["counts"]["needsYou"] == 1


def test_pseudo_nodes_and_incidents_lift_the_product_tier():
    quiet = ProductIn(slug="p", name="P", capabilities=(_cap("a"),), initiatives=(),
                      awaiting_triage=0, needs_home=0, incident_statuses=())
    assert build_product_tree(quiet)["tier"] == "live"
    triage = ProductIn(slug="p", name="P", capabilities=(_cap("a"),), initiatives=(),
                       awaiting_triage=3, needs_home=0, incident_statuses=())
    assert build_product_tree(triage)["tier"] == "needs-you"
    assert build_product_tree(triage)["counts"]["awaitingTriage"] == 3
    diag = ProductIn(slug="p", name="P", capabilities=(_cap("a"),), initiatives=(),
                     awaiting_triage=0, needs_home=0, incident_statuses=("diagnosing",))
    assert build_product_tree(diag)["tier"] == "in-flight"
```

- [ ] **Step 2: Run to see them fail**

Run: `env -i PATH="$PATH" HOME="$HOME" LANG=C.UTF-8 CCSWITCH_DEV=1 /home/tony/GitHub/ccswitch/.venv/bin/python -m pytest server/tests/test_pm_tree.py -q -p no:randomly`
Expected: FAIL — `server.pm.tree` does not exist.

- [ ] **Step 3: Implement**

Create `server/pm/tree.py`:

```python
"""Pure roadmap-tree rules (spec 2026-09-07 §10-§11): one attention tier per node,
rolled up from children, and the nested product → capability → initiative shape
the web renders. No I/O — `server/api/pm_tree.py` loads rows and calls this.

The tier is computed HERE, once, for every node; the web's `roadmapTier.ts` only
maps these names to tones (constitution VI).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from server.db.models import INITIATIVE_STAGES
from server.pm.desired_state import ACTIVE_SESSION_STATUSES, HUMAN_WAIT_SESSION_STATUSES

TIERS: tuple[str, ...] = ("needs-you", "blocked", "in-flight", "proposed", "live", "idle")
_RANK = {t: i for i, t in enumerate(TIERS)}
_IN_FLIGHT_STAGES = frozenset(INITIATIVE_STAGES[:INITIATIVE_STAGES.index("deploy") + 1])


def max_tier(*tiers: str) -> str:
    """The highest-priority tier among *tiers*; unknown strings count as idle."""
    best = "idle"
    for t in tiers:
        if _RANK.get(t, len(TIERS)) < _RANK[best]:
            best = t
    return best


def initiative_tier(status: str | None, stage: str | None,
                    epic_blocked: int, epic_needs_human: int) -> str:
    if status == "awaiting_human" or epic_needs_human > 0:
        return "needs-you"
    if status == "blocked" or epic_blocked > 0:
        return "blocked"
    if status == "done" or stage == "done":
        return "live"
    if status == "proposed":
        return "proposed"
    if status == "active" and stage in _IN_FLIGHT_STAGES:
        return "in-flight"
    return "idle"


def capability_own_tier(state: str | None) -> str:
    if state == "live":
        return "live"
    if state == "proposed":
        return "proposed"
    return "idle"


def issue_tier(state: str | None, triage_state: str | None,
               has_open_blocker: bool, session_status: str | None) -> str:
    if state in ("closed", "merged"):
        return "live"
    if triage_state == "needs_human" or session_status in HUMAN_WAIT_SESSION_STATUSES:
        return "needs-you"
    if has_open_blocker:
        return "blocked"
    if session_status in ACTIVE_SESSION_STATUSES:
        return "in-flight"
    return "idle"


def incident_tier(status: str | None) -> str:
    if status in ("open", "scheduled"):
        return "needs-you"
    if status in ("diagnosing", "queued"):
        return "in-flight"
    return "idle"


@dataclass(frozen=True)
class CapabilityIn:
    id: str
    slug: str
    title: str | None
    state: str | None
    summary: str | None
    doc_path: str | None
    commit_sha: str | None
    pr_number: int | None
    parent_slug: str | None
    issue_tiers: tuple[str, ...]          # one tier per OPEN issue classified under it
    links: tuple[tuple[str, str, str], ...] = ()   # (kind, project slug, feature slug)


@dataclass(frozen=True)
class InitiativeIn:
    slug: str
    title: str | None
    stage: str | None
    status: str | None
    primary_slug: str | None
    capability_slugs: tuple[str, ...]
    epic: dict | None                     # {ref,url,total,done,blocked,needsHuman} or None


@dataclass(frozen=True)
class ProductIn:
    slug: str
    name: str | None
    capabilities: tuple[CapabilityIn, ...]
    initiatives: tuple[InitiativeIn, ...]
    awaiting_triage: int
    needs_home: int
    incident_statuses: tuple[str, ...] = field(default_factory=tuple)


def _initiative_node(ini: InitiativeIn, project_slug: str) -> dict:
    epic = ini.epic or {}
    return {
        "slug": ini.slug, "title": ini.title, "stage": ini.stage, "status": ini.status,
        "projectSlug": project_slug, "primaryCapability": ini.primary_slug,
        "capabilities": list(ini.capability_slugs),
        "tier": initiative_tier(ini.status, ini.stage,
                                int(epic.get("blocked", 0)), int(epic.get("needsHuman", 0))),
        "epic": ini.epic,
    }


def _empty_counts() -> dict:
    return {"openIssues": 0, "inFlight": 0, "blocked": 0, "needsYou": 0}


def _add_counts(into: dict, other: dict) -> None:
    for k in ("openIssues", "inFlight", "blocked", "needsYou"):
        into[k] += other[k]


def _capability_node(cap: CapabilityIn, children: list[dict],
                     initiatives_here: list[dict], touching: list[str]) -> dict:
    counts = _empty_counts()
    counts["openIssues"] = len(cap.issue_tiers)
    counts["blocked"] = sum(1 for t in cap.issue_tiers if t == "blocked")
    counts["needsYou"] = sum(1 for t in cap.issue_tiers if t == "needs-you")
    counts["inFlight"] = sum(1 for t in cap.issue_tiers if t == "in-flight")
    for ini in initiatives_here:
        if ini["tier"] == "in-flight":
            counts["inFlight"] += 1
        elif ini["tier"] == "blocked":
            counts["blocked"] += 1
        elif ini["tier"] == "needs-you":
            counts["needsYou"] += 1
    for child in children:
        _add_counts(counts, child["counts"])
    tier = max_tier(capability_own_tier(cap.state), *cap.issue_tiers,
                    *(c["tier"] for c in children), *(i["tier"] for i in initiatives_here))
    return {
        "id": cap.id, "slug": cap.slug, "title": cap.title, "state": cap.state,
        "summary": cap.summary, "docPath": cap.doc_path, "commitSha": cap.commit_sha,
        "prNumber": cap.pr_number, "parentSlug": cap.parent_slug, "tier": tier,
        "counts": counts,
        "links": [{"kind": k, "projectSlug": p, "featureSlug": f} for k, p, f in cap.links],
        "initiatives": touching,
        "children": children,
    }


def build_product_tree(product: ProductIn) -> dict:
    caps = {c.slug: c for c in product.capabilities}
    children_of: dict[str | None, list[CapabilityIn]] = {}
    for c in product.capabilities:
        parent = c.parent_slug if c.parent_slug in caps else None
        children_of.setdefault(parent, []).append(c)

    initiative_nodes = [_initiative_node(i, product.slug) for i in product.initiatives]
    primary_under: dict[str, list[dict]] = {}
    touching: dict[str, list[str]] = {}
    for node in initiative_nodes:
        if node["primaryCapability"] in caps:
            primary_under.setdefault(node["primaryCapability"], []).append(node)
        for slug in node["capabilities"]:
            if slug in caps:
                touching.setdefault(slug, []).append(node["slug"])

    def build(cap: CapabilityIn) -> dict:
        kids = [build(k) for k in sorted(children_of.get(cap.slug, []), key=lambda c: c.slug)]
        return _capability_node(cap, kids, primary_under.get(cap.slug, []), touching.get(cap.slug, []))

    roots = [build(c) for c in sorted(children_of.get(None, []), key=lambda c: c.slug)]
    counts = _empty_counts()
    for r in roots:
        _add_counts(counts, r["counts"])
    for node in initiative_nodes:
        if node["primaryCapability"] not in caps:
            if node["tier"] == "in-flight":
                counts["inFlight"] += 1
            elif node["tier"] == "blocked":
                counts["blocked"] += 1
            elif node["tier"] == "needs-you":
                counts["needsYou"] += 1
    counts["capabilities"] = len(product.capabilities)
    counts["awaitingTriage"] = product.awaiting_triage
    counts["needsHome"] = product.needs_home
    pseudo = "needs-you" if (product.awaiting_triage or product.needs_home) else "idle"
    tier = max_tier(pseudo, *(r["tier"] for r in roots), *(i["tier"] for i in initiative_nodes),
                    *(incident_tier(s) for s in product.incident_statuses))
    return {"slug": product.slug, "name": product.name, "tier": tier, "counts": counts,
            "capabilities": roots, "initiatives": initiative_nodes}
```

- [ ] **Step 4: Run the tests**

Run: the same command. Expected: PASS. Also run `env -i PATH="$PATH" HOME="$HOME" LANG=C.UTF-8 /home/tony/GitHub/ccswitch/.venv/bin/mypy server/pm/tree.py --ignore-missing-imports` — clean.

- [ ] **Step 5: Commit**

```bash
printf 'feat(pm): pure roadmap tree — attention tiers, rollup, nested product shape\n\nRefs #993\n\nCo-Authored-By: Claude <model> <noreply@anthropic.com>\n' > /tmp/p2-t5.txt
git add server/pm/tree.py server/tests/test_pm_tree.py
git commit -F /tmp/p2-t5.txt
```

---

### Task 6: Initiatives carry their product and capabilities

**Files:**
- Modify: `server/api/sdlc.py:41-55` (`_initiative_to_dict`), `:171-176` (create body), `:179-249` (list, create), `:252-313` (detail)
- Test: `server/tests/test_sdlc_api.py`

**Interfaces:**
- Produces:
  - `InitiativeCreateBody` gains `primaryCapabilityId: str | None = None` and `capabilityIds: list[str] = []`. On create: every id must be an existing `Feature` else 422 `{error: "capability_not_found", message}`; `primaryCapabilityId`, when given, is added to the set if missing; one `InitiativeFeature` row per id with `is_primary` on the primary; `Initiative.project_id` is set from the primary capability's `project_id` (or the first capability's when there is no primary).
  - Every initiative payload (list, create, detail, advance) gains `projectSlug: str | null`, `primaryCapability: {id, slug, title} | null`, `capabilities: [{id, slug, title, isPrimary}]`.
  - New helper `_capabilities_batch(db, initiatives) -> dict[str, tuple[list[dict], dict | None]]` used by all four; `_project_slugs(db, initiatives) -> dict[str, str | None]`.

- [ ] **Step 1: Write the failing tests**

Append to `server/tests/test_sdlc_api.py`:

```python
def _seed_product_with_capabilities() -> tuple[str, str, str]:
    """Returns (project_id, root_feature_id, child_feature_id)."""
    import asyncio
    from server.db import db_session
    from server.db.models import Feature, Project

    async def _seed():
        async with db_session() as db:
            p = Project(slug="example-product", name="Example product")
            db.add(p)
            await db.flush()
            root = Feature(project_id=p.id, slug="sessions", title="Sessions", state="live")
            db.add(root)
            await db.flush()
            child = Feature(project_id=p.id, slug="dictation", title="Dictation",
                            state="delivering", parent_feature_id=root.id)
            db.add(child)
            await db.commit()
            return p.id, root.id, child.id
    return asyncio.run(_seed())


def test_create_with_capabilities_links_and_derives_product():
    with _client() as c:
        _, root_id, child_id = _seed_product_with_capabilities()
        r = c.post("/api/sdlc/initiatives", json={
            "title": "Whisper on the edge", "repo": "o/r",
            "primaryCapabilityId": child_id, "capabilityIds": [root_id]},
            headers=IDENTITY_HEADERS)
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["projectSlug"] == "example-product"
        assert body["primaryCapability"] == {"id": child_id, "slug": "dictation", "title": "Dictation"}
        assert sorted((x["slug"], x["isPrimary"]) for x in body["capabilities"]) == [
            ("dictation", True), ("sessions", False)]

        listed = c.get("/api/sdlc/initiatives", headers=IDENTITY_HEADERS).json()
        assert listed[0]["primaryCapability"]["slug"] == "dictation"
        detail = c.get("/api/sdlc/initiatives/whisper-on-the-edge", headers=IDENTITY_HEADERS).json()
        assert detail["projectSlug"] == "example-product"
        assert len(detail["capabilities"]) == 2


def test_create_with_unknown_capability_is_422():
    with _client() as c:
        r = c.post("/api/sdlc/initiatives", json={
            "title": "X", "repo": "o/r", "capabilityIds": ["nope"]}, headers=IDENTITY_HEADERS)
        assert r.status_code == 422
        assert r.json()["detail"]["error"] == "capability_not_found"


def test_initiative_without_capabilities_has_null_fields():
    with _client() as c:
        r = c.post("/api/sdlc/initiatives", json={"title": "Plain", "repo": "o/r"},
                   headers=IDENTITY_HEADERS)
        body = r.json()
        assert body["projectSlug"] is None
        assert body["primaryCapability"] is None
        assert body["capabilities"] == []
```

- [ ] **Step 2: Run to see them fail**

Run: `env -i PATH="$PATH" HOME="$HOME" LANG=C.UTF-8 CCSWITCH_DEV=1 /home/tony/GitHub/ccswitch/.venv/bin/python -m pytest server/tests/test_sdlc_api.py -q -p no:randomly -k "capabilit or null_fields"`
Expected: FAIL — `KeyError: 'projectSlug'`.

- [ ] **Step 3: Implement**

In `server/api/sdlc.py`, extend the models import with `Feature, InitiativeFeature, Project`. Add after `_initiative_to_dict`:

```python
async def _project_slugs(db, initiatives: list[Initiative]) -> dict[str, str | None]:
    """initiative.id → owning product slug (None when unlinked)."""
    ids = {i.project_id for i in initiatives if i.project_id}
    slug_by_project: dict[str, str | None] = {}
    if ids:
        slug_by_project = {p.id: p.slug for p in (await db.execute(
            select(Project).where(Project.id.in_(ids)))).scalars().all()}
    return {i.id: slug_by_project.get(i.project_id) if i.project_id else None for i in initiatives}


async def _capabilities_batch(db, initiatives: list[Initiative]) -> dict[str, tuple[list[dict], dict | None]]:
    """initiative.id → ([{id, slug, title, isPrimary}…], primary {id, slug, title} | None),
    in a fixed two queries regardless of how many initiatives are passed."""
    result: dict[str, tuple[list[dict], dict | None]] = {i.id: ([], None) for i in initiatives}
    if not initiatives:
        return result
    rows = (await db.execute(select(InitiativeFeature).where(
        InitiativeFeature.initiative_id.in_([i.id for i in initiatives])))).scalars().all()
    if not rows:
        return result
    features = {f.id: f for f in (await db.execute(select(Feature).where(
        Feature.id.in_({r.feature_id for r in rows})))).scalars().all()}
    for r in rows:
        f = features.get(r.feature_id)
        if f is None:
            continue
        entry = {"id": f.id, "slug": f.slug, "title": f.title, "isPrimary": bool(r.is_primary)}
        caps, primary = result[r.initiative_id]
        caps.append(entry)
        if r.is_primary:
            primary = {"id": f.id, "slug": f.slug, "title": f.title}
        result[r.initiative_id] = (caps, primary)
    return result


async def _decorate(db, initiatives: list[Initiative], dicts: list[dict]) -> None:
    """Attach projectSlug / primaryCapability / capabilities to already-built dicts."""
    slugs = await _project_slugs(db, initiatives)
    caps = await _capabilities_batch(db, initiatives)
    for i, d in zip(initiatives, dicts):
        d["projectSlug"] = slugs.get(i.id)
        d["capabilities"], d["primaryCapability"] = caps[i.id]
```

Change `InitiativeCreateBody`:

```python
class InitiativeCreateBody(BaseModel):
    title: str
    repo: str
    slug: str | None = None
    origin: str | None = None
    originRef: str | None = None
    primaryCapabilityId: str | None = None
    capabilityIds: list[str] = []
```

In `list_initiatives`, before `return result` add `await _decorate(db, rows, result)`.

In `create_initiative`, after the slug-conflict check and before constructing `Initiative`, add:

```python
    wanted = list(dict.fromkeys(body.capabilityIds
                                + ([body.primaryCapabilityId] if body.primaryCapabilityId else [])))
    features: dict[str, Feature] = {}
    if wanted:
        features = {f.id: f for f in (await db.execute(
            select(Feature).where(Feature.id.in_(wanted)))).scalars().all()}
        missing = [fid for fid in wanted if fid not in features]
        if missing:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={"error": "capability_not_found",
                        "message": f"unknown capability id(s): {', '.join(missing)}"})
    project_id = None
    if wanted:
        anchor = features[body.primaryCapabilityId] if body.primaryCapabilityId else features[wanted[0]]
        project_id = anchor.project_id
```

pass `project_id=project_id` to the `Initiative(...)` constructor, and after `await db.flush()` add:

```python
    for fid in wanted:
        db.add(InitiativeFeature(initiative_id=ini.id, feature_id=fid,
                                 is_primary=(fid == body.primaryCapabilityId)))
    await db.flush()
```

Replace the create endpoint's `return _initiative_to_dict(ini)` with:

```python
    d = _initiative_to_dict(ini)
    await _decorate(db, [ini], [d])
    return d
```

In `get_initiative`, before `return result` add `await _decorate(db, [ini], [result])`. In `advance_initiative`, find its final `return _initiative_to_dict(ini)` (below line 340) and replace it the same way as create.

- [ ] **Step 4: Run the SDLC API tests and the assistant tests**

Run: `env -i PATH="$PATH" HOME="$HOME" LANG=C.UTF-8 CCSWITCH_DEV=1 /home/tony/GitHub/ccswitch/.venv/bin/python -m pytest server/tests/test_sdlc_api.py server/tests/test_assistant_tools.py server/tests/test_assistant_policy.py -q -p no:randomly`
Expected: PASS. (If `test_assistant_tools.py` does not exist, run `ls server/tests | grep assistant` and use the files that do.)

- [ ] **Step 5: Commit**

```bash
printf 'feat(sdlc): initiatives carry projectSlug, primary and touched capabilities; create links them\n\nRefs #993\n\nCo-Authored-By: Claude <model> <noreply@anthropic.com>\n' > /tmp/p2-t6.txt
git add server/api/sdlc.py server/tests/test_sdlc_api.py
git commit -F /tmp/p2-t6.txt
```

---

### Task 7: `GET /api/pm/map`

**Files:**
- Create: `server/api/pm_tree.py`
- Modify: `server/app.py` (import beside `pm_router`, `include_router` beside `app.include_router(pm_router)`)
- Test: `server/tests/test_pm_map_api.py` (new)

**Interfaces:**
- Consumes: `server.pm.tree` (`ProductIn`, `CapabilityIn`, `InitiativeIn`, `build_product_tree`, `issue_tier`), `server.api.sdlc._epic_progress_batch` and `_capabilities_batch`.
- Produces: `GET /api/pm/map` → `{"products": [product node…], "generatedAt": iso}` with products ordered by `Project.created_at`, orphaned features excluded, and per-capability `issue_tiers` computed over OPEN work items with that `feature_id` (blocked via active `WorkItemDep` whose destination is unmirrored or open; session via any `Session.origin_ref == "repo#N"` with a live status).

- [ ] **Step 1: Write the failing test**

Create `server/tests/test_pm_map_api.py` with the same `_client()` context manager as `server/tests/test_pm_deps_api.py` (copy it verbatim, including the env scrub), then:

```python
def test_map_returns_nested_capabilities_initiatives_and_tiers():
    import asyncio
    from server.db import db_session
    from server.db.models import (
        Feature, FeatureLink, Initiative, InitiativeFeature, Project, ProjectRepo,
        TriageProposal, WorkItem, WorkItemDep)

    with _client() as c:
        async def _seed():
            async with db_session() as db:
                infra = Project(slug="example-infra", name="Infra")
                prod = Project(slug="example-product", name="Example product")
                db.add_all([infra, prod])
                await db.flush()
                db.add(ProjectRepo(project_id=prod.id, repo="o/r", is_primary=True))
                mq = Feature(project_id=infra.id, slug="message-queue", title="MQ", state="live")
                sessions = Feature(project_id=prod.id, slug="sessions", title="Sessions", state="live")
                gone = Feature(project_id=prod.id, slug="gone", title="Gone", state="live", orphaned=True)
                db.add_all([mq, sessions, gone])
                await db.flush()
                dictation = Feature(project_id=prod.id, slug="dictation", title="Dictation",
                                    state="delivering", parent_feature_id=sessions.id)
                db.add(dictation)
                await db.flush()
                db.add(FeatureLink(src_feature_id=dictation.id, dst_feature_id=mq.id, kind="powered_by"))
                # Two open issues under dictation: one blocked by an unmirrored issue, one plain;
                # one closed issue that must not count; one unclassified open issue (awaiting nothing).
                a = WorkItem(repo="o/r", number=1, kind="issue", state="open", title="A",
                             feature_id=dictation.id, triage_state="classified")
                b = WorkItem(repo="o/r", number=2, kind="issue", state="open", title="B",
                             feature_id=dictation.id, triage_state="classified")
                closed = WorkItem(repo="o/r", number=3, kind="issue", state="closed", title="C",
                                  feature_id=dictation.id, triage_state="classified")
                pending = WorkItem(repo="o/r", number=4, kind="issue", state="open", title="D",
                                   triage_state="proposed")
                rejected = WorkItem(repo="o/r", number=5, kind="issue", state="open", title="E",
                                    triage_state="needs_human")
                db.add_all([a, b, closed, pending, rejected])
                await db.flush()
                db.add(WorkItemDep(src_work_item_id=a.id, src_ref="o/r#1", dst_ref="o/r#99",
                                   source="native", active=True))
                db.add(TriageProposal(work_item_id=pending.id, status="pending"))
                ini = Initiative(slug="whisper", repo="o/r", title="Whisper", stage="build",
                                 status="active", project_id=prod.id)
                db.add(ini)
                await db.flush()
                db.add(InitiativeFeature(initiative_id=ini.id, feature_id=dictation.id, is_primary=True))
                db.add(InitiativeFeature(initiative_id=ini.id, feature_id=sessions.id))
                await db.commit()
        asyncio.run(_seed())

        r = c.get("/api/pm/map", headers=IDENTITY_HEADERS)
        assert r.status_code == 200, r.text
        body = r.json()
        assert "generatedAt" in body
        products = {p["slug"]: p for p in body["products"]}
        prod = products["example-product"]
        roots = {cap["slug"]: cap for cap in prod["capabilities"]}
        assert set(roots) == {"sessions"}                      # orphaned `gone` excluded
        dictation = roots["sessions"]["children"][0]
        assert dictation["slug"] == "dictation"
        assert dictation["links"] == [{"kind": "powered_by", "projectSlug": "example-infra",
                                       "featureSlug": "message-queue"}]
        assert dictation["counts"]["openIssues"] == 2
        assert dictation["counts"]["blocked"] == 1
        assert dictation["initiatives"] == ["whisper"]
        assert dictation["tier"] == "blocked"                  # blocked issue outranks in-flight
        assert roots["sessions"]["initiatives"] == ["whisper"]
        assert prod["initiatives"][0]["primaryCapability"] == "dictation"
        assert prod["initiatives"][0]["capabilities"] == ["dictation", "sessions"] or \
               prod["initiatives"][0]["capabilities"] == ["sessions", "dictation"]
        assert prod["counts"]["awaitingTriage"] == 1
        assert prod["counts"]["needsHome"] == 1
        assert prod["counts"]["capabilities"] == 2
        assert prod["tier"] == "needs-you"
        assert products["example-infra"]["capabilities"][0]["slug"] == "message-queue"


def test_map_is_empty_without_products():
    with _client() as c:
        r = c.get("/api/pm/map", headers=IDENTITY_HEADERS)
        assert r.status_code == 200
        assert r.json()["products"] == []
```

- [ ] **Step 2: Run to see it fail**

Run: `env -i PATH="$PATH" HOME="$HOME" LANG=C.UTF-8 CCSWITCH_DEV=1 /home/tony/GitHub/ccswitch/.venv/bin/python -m pytest server/tests/test_pm_map_api.py -q -p no:randomly`
Expected: FAIL — 404 (route absent).

- [ ] **Step 3: Implement the router**

Create `server/api/pm_tree.py`:

```python
"""Roadmap tree endpoints (spec 2026-09-07 §10): the map every product renders
from, and the issue leaves under one node. Reads only; a fixed number of grouped
queries per request, never one per node."""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select, tuple_

from server.api.sdlc import _capabilities_batch, _epic_progress_batch
from server.auth.middleware import current_identity
from server.db import get_db
from server.db.models import (
    Feature, FeatureLink, Initiative, Project, ProjectRepo, Session as DbSession,
    TriageItem, TriageProposal, WorkItem, WorkItemDep)
from server.pm.desired_state import ACTIVE_SESSION_STATUSES, HUMAN_WAIT_SESSION_STATUSES
from server.pm.score import rice_score
from server.pm.tree import CapabilityIn, InitiativeIn, ProductIn, build_product_tree, issue_tier

router = APIRouter(prefix="/api/pm", tags=["pm"])

_LIVE = ACTIVE_SESSION_STATUSES | HUMAN_WAIT_SESSION_STATUSES


def _ref(w: WorkItem) -> str:
    return f"{w.repo}#{w.number}"


def _split_ref(ref: str) -> tuple[str, int] | None:
    repo, _, num = ref.rpartition("#")
    return (repo, int(num)) if repo and num.isdigit() else None


async def _issue_facts(db, items: list[WorkItem]) -> tuple[dict[str, bool], dict[str, str | None], dict[str, list[str]]]:
    """For each work item id: has an open blocker; the live session status if any;
    the open blocker refs. Blockers fail closed: an unmirrored destination counts
    as open (spec 003 §10)."""
    if not items:
        return {}, {}, {}
    refs = [_ref(w) for w in items]
    deps = (await db.execute(select(WorkItemDep).where(
        WorkItemDep.src_ref.in_(refs), WorkItemDep.active.is_(True)))).scalars().all()
    dst_refs = {d.dst_ref for d in deps if d.dst_ref}
    parsed = [p for p in (_split_ref(r) for r in dst_refs) if p is not None]
    dst_state: dict[str, str] = {}
    if parsed:
        dst_state = {_ref(w): w.state or "open" for w in (await db.execute(select(WorkItem).where(
            WorkItem.kind == "issue", tuple_(WorkItem.repo, WorkItem.number).in_(parsed)))).scalars().all()}
    blockers: dict[str, list[str]] = {}
    for d in deps:
        if d.src_ref and d.dst_ref and dst_state.get(d.dst_ref, "open") == "open":
            blockers.setdefault(d.src_ref, []).append(d.dst_ref)
    sessions = (await db.execute(select(DbSession.origin_ref, DbSession.status).where(
        DbSession.origin_ref.in_(refs), DbSession.status.in_(_LIVE)))).all()
    session_by_ref = {ref: st for ref, st in sessions}
    by_id_blocked = {w.id: bool(blockers.get(_ref(w))) for w in items}
    by_id_session = {w.id: session_by_ref.get(_ref(w)) for w in items}
    by_id_blockers = {w.id: blockers.get(_ref(w), []) for w in items}
    return by_id_blocked, by_id_session, by_id_blockers


@router.get("/map")
async def get_map(_identity: str = Depends(current_identity), db=Depends(get_db)) -> dict:
    projects = (await db.execute(select(Project).order_by(Project.created_at))).scalars().all()
    if not projects:
        return {"products": [], "generatedAt": datetime.now(timezone.utc).isoformat()}
    pids = [p.id for p in projects]
    slug_of = {p.id: p.slug for p in projects}

    features = (await db.execute(select(Feature).where(
        Feature.project_id.in_(pids), Feature.orphaned.is_not(True)))).scalars().all()
    feat_by_id = {f.id: f for f in features}
    # Guard every `in_` on a possibly-empty list: SQLAlchemy warns on `in_([])`.
    links = (await db.execute(select(FeatureLink).where(
        FeatureLink.src_feature_id.in_(list(feat_by_id))))).scalars().all() if feat_by_id else []
    # Link destinations may be orphaned or in a project outside `pids`; load them too.
    dst_ids = {l.dst_feature_id for l in links} - set(feat_by_id)
    dst_extra = {f.id: f for f in (await db.execute(select(Feature).where(
        Feature.id.in_(dst_ids)))).scalars().all()} if dst_ids else {}
    dst_projects = {}
    extra_pids = {f.project_id for f in dst_extra.values()} - set(pids)
    if extra_pids:
        dst_projects = {p.id: p.slug for p in (await db.execute(select(Project).where(
            Project.id.in_(extra_pids)))).scalars().all()}
    slug_of.update(dst_projects)
    links_by_src: dict[str, list[tuple[str, str, str]]] = {}
    for l in links:
        dst = feat_by_id.get(l.dst_feature_id) or dst_extra.get(l.dst_feature_id)
        if dst is None:
            continue
        links_by_src.setdefault(l.src_feature_id, []).append(
            (l.kind or "", slug_of.get(dst.project_id) or "", dst.slug or ""))

    open_items = (await db.execute(select(WorkItem).where(
        WorkItem.feature_id.in_(list(feat_by_id)), WorkItem.state == "open"))).scalars().all() \
        if feat_by_id else []
    blocked, session, _ = await _issue_facts(db, open_items)
    tiers_by_feature: dict[str, list[str]] = {}
    for w in open_items:
        tiers_by_feature.setdefault(w.feature_id, []).append(
            issue_tier(w.state, w.triage_state, blocked[w.id], session[w.id]))

    initiatives = (await db.execute(select(Initiative).where(
        Initiative.project_id.in_(pids)).order_by(Initiative.created_at))).scalars().all()
    epics = await _epic_progress_batch(db, initiatives, include_children=False)
    caps = await _capabilities_batch(db, initiatives)

    repos = (await db.execute(select(ProjectRepo).where(ProjectRepo.project_id.in_(pids)))).scalars().all()
    repos_by_project: dict[str, list[str]] = {}
    for r in repos:
        if r.repo:
            repos_by_project.setdefault(r.project_id, []).append(r.repo)
    all_repos = [r for rs in repos_by_project.values() for r in rs]
    pending_by_repo: dict[str, int] = {}
    needs_home_by_repo: dict[str, int] = {}
    incidents_by_repo: dict[str, list[str]] = {}
    if all_repos:
        pending = (await db.execute(select(WorkItem.repo).join(
            TriageProposal, TriageProposal.work_item_id == WorkItem.id).where(
            WorkItem.repo.in_(all_repos), TriageProposal.status == "pending"))).all()
        for (repo,) in pending:
            pending_by_repo[repo] = pending_by_repo.get(repo, 0) + 1
        needs_home = (await db.execute(select(WorkItem.repo).where(
            WorkItem.repo.in_(all_repos), WorkItem.state == "open",
            WorkItem.triage_state == "needs_human"))).all()
        for (repo,) in needs_home:
            needs_home_by_repo[repo] = needs_home_by_repo.get(repo, 0) + 1
        incidents = (await db.execute(select(TriageItem.repo, TriageItem.status).where(
            TriageItem.repo.in_(all_repos)))).all()
        for repo, st in incidents:
            incidents_by_repo.setdefault(repo, []).append(st or "")

    products = []
    for p in projects:
        my_feats = [f for f in features if f.project_id == p.id]
        slug_by_fid = {f.id: f.slug for f in my_feats}
        cap_inputs = tuple(CapabilityIn(
            id=f.id, slug=f.slug or "", title=f.title, state=f.state, summary=f.summary,
            doc_path=f.repo_path, commit_sha=f.commit_sha, pr_number=f.pr_number,
            parent_slug=slug_by_fid.get(f.parent_feature_id) if f.parent_feature_id else None,
            issue_tiers=tuple(tiers_by_feature.get(f.id, [])),
            links=tuple(links_by_src.get(f.id, [])),
        ) for f in my_feats)
        ini_inputs = []
        for i in initiatives:
            if i.project_id != p.id:
                continue
            cap_list, primary = caps[i.id]
            ini_inputs.append(InitiativeIn(
                slug=i.slug or "", title=i.title, stage=i.stage, status=i.status,
                primary_slug=primary["slug"] if primary else None,
                capability_slugs=tuple(c["slug"] for c in cap_list),
                epic=epics.get(i.id)))
        my_repos = repos_by_project.get(p.id, [])
        products.append(build_product_tree(ProductIn(
            slug=p.slug or "", name=p.name,
            capabilities=cap_inputs, initiatives=tuple(ini_inputs),
            awaiting_triage=sum(pending_by_repo.get(r, 0) for r in my_repos),
            needs_home=sum(needs_home_by_repo.get(r, 0) for r in my_repos),
            incident_statuses=tuple(s for r in my_repos for s in incidents_by_repo.get(r, [])),
        )))
    return {"products": products, "generatedAt": datetime.now(timezone.utc).isoformat()}
```

Register it in `server/app.py`: next to `from server.api.pm import router as pm_router` add `from server.api.pm_tree import router as pm_tree_router`, and next to `app.include_router(pm_router)` add `app.include_router(pm_tree_router)`.

- [ ] **Step 4: Run the tests, ruff and mypy**

Run: `env -i PATH="$PATH" HOME="$HOME" LANG=C.UTF-8 CCSWITCH_DEV=1 /home/tony/GitHub/ccswitch/.venv/bin/python -m pytest server/tests/test_pm_map_api.py server/tests/test_pm_api.py server/tests/test_assistant_policy.py -q -p no:randomly && env -i PATH="$PATH" HOME="$HOME" LANG=C.UTF-8 /home/tony/GitHub/ccswitch/.venv/bin/ruff check server/ && env -i PATH="$PATH" HOME="$HOME" LANG=C.UTF-8 /home/tony/GitHub/ccswitch/.venv/bin/mypy server/ --ignore-missing-imports`
Expected: PASS, clean. If `test_assistant_policy.py` pins the set of `GET /api/pm/*` routes the assistant may reach, add `("GET", r"/api/pm/map")` beside the existing `/api/pm/projects` entries in `server/assistant/policy.py` — it is a read.

- [ ] **Step 5: Commit**

```bash
printf 'feat(pm): GET /api/pm/map — every product'"'"'s capability tree, initiatives and rolled-up tiers\n\nRefs #993\n\nCo-Authored-By: Claude <model> <noreply@anthropic.com>\n' > /tmp/p2-t7.txt
git add server/api/pm_tree.py server/app.py server/tests/test_pm_map_api.py server/assistant/policy.py
git commit -F /tmp/p2-t7.txt
```

---

### Task 8: `GET /api/pm/leaves`

**Files:**
- Modify: `server/api/pm_tree.py` (append)
- Test: `server/tests/test_pm_map_api.py` (append)

**Interfaces:**
- Consumes: `_issue_facts`, `issue_tier`, `rice_score(reach, impact, effort, confidence)`.
- Produces: `GET /api/pm/leaves?capability=<feature id>` or `?initiative=<slug>` (exactly one; both or neither → 422 `{error: "bad_scope"}`; unknown → 404 `{error: "not_found"}`) → `{"items": [leaf…]}` where a leaf is `{id, ref, repo, number, kind, title, url, state, triageState, score, reach, impact, effort, confidence, tier, blockedBy: [ref…], parentRef, sessionStatus}`; open items first, then by `score` descending (nulls last), then `ghCreatedAt` ascending, then `number`. For a capability the items are those with `feature_id == id`; for an initiative they are the epic's children (`WorkItem.parent_work_item_id == epic.id`), or empty when the initiative has no epic.

- [ ] **Step 1: Write the failing tests**

Append to `server/tests/test_pm_map_api.py`:

```python
def test_leaves_for_capability_and_initiative():
    import asyncio
    from server.db import db_session
    from server.db.models import Feature, Initiative, Project, WorkItem

    with _client() as c:
        async def _seed():
            async with db_session() as db:
                prod = Project(slug="example-product", name="Example product")
                db.add(prod)
                await db.flush()
                cap = Feature(project_id=prod.id, slug="dictation", title="Dictation", state="live")
                db.add(cap)
                await db.flush()
                epic = WorkItem(repo="o/r", number=10, kind="issue", state="open", title="Epic", is_epic=True)
                db.add(epic)
                await db.flush()
                low = WorkItem(repo="o/r", number=1, kind="issue", state="open", title="Low",
                               feature_id=cap.id, triage_state="classified",
                               reach=1, impact=1, effort=1, confidence=1)
                high = WorkItem(repo="o/r", number=2, kind="issue", state="open", title="High",
                                feature_id=cap.id, triage_state="classified",
                                reach=5, impact=5, effort=1, confidence=5, parent_work_item_id=epic.id)
                done = WorkItem(repo="o/r", number=3, kind="issue", state="closed", title="Done",
                                feature_id=cap.id, triage_state="classified",
                                reach=9, impact=9, effort=1, confidence=9, parent_work_item_id=epic.id)
                db.add_all([low, high, done])
                ini = Initiative(slug="whisper", repo="o/r", title="W", stage="build", status="active",
                                 project_id=prod.id, epic_repo="o/r", epic_number=10)
                db.add(ini)
                await db.commit()
                return cap.id
        cap_id = asyncio.run(_seed())

        r = c.get(f"/api/pm/leaves?capability={cap_id}", headers=IDENTITY_HEADERS)
        assert r.status_code == 200, r.text
        items = r.json()["items"]
        assert [i["number"] for i in items] == [2, 1, 3]          # open by score desc, closed last
        assert items[0]["score"] == 125.0 and items[0]["tier"] == "idle"
        assert items[0]["parentRef"] == "o/r#10"
        assert items[2]["tier"] == "live"

        r = c.get("/api/pm/leaves?initiative=whisper", headers=IDENTITY_HEADERS)
        assert [i["number"] for i in r.json()["items"]] == [2, 3]  # the epic's children only

        assert c.get("/api/pm/leaves", headers=IDENTITY_HEADERS).status_code == 422
        assert c.get(f"/api/pm/leaves?capability={cap_id}&initiative=whisper",
                     headers=IDENTITY_HEADERS).status_code == 422
        assert c.get("/api/pm/leaves?capability=nope", headers=IDENTITY_HEADERS).status_code == 404
        assert c.get("/api/pm/leaves?initiative=nope", headers=IDENTITY_HEADERS).status_code == 404
```

- [ ] **Step 2: Run to see it fail**

Run: `env -i PATH="$PATH" HOME="$HOME" LANG=C.UTF-8 CCSWITCH_DEV=1 /home/tony/GitHub/ccswitch/.venv/bin/python -m pytest server/tests/test_pm_map_api.py -q -p no:randomly -k leaves`
Expected: FAIL — 404.

- [ ] **Step 3: Implement**

Append to `server/api/pm_tree.py`:

```python
def _leaf(w: WorkItem, tier: str, blockers: list[str], session_status: str | None,
          parent_ref: str | None) -> dict:
    score = rice_score(w.reach, w.impact, w.effort, w.confidence)
    return {
        "id": w.id, "ref": _ref(w), "repo": w.repo, "number": w.number, "kind": w.kind,
        "title": w.title, "url": w.url, "state": w.state, "triageState": w.triage_state,
        "score": score, "reach": w.reach, "impact": w.impact, "effort": w.effort,
        "confidence": w.confidence, "tier": tier, "blockedBy": blockers,
        "parentRef": parent_ref, "sessionStatus": session_status,
        "ghCreatedAt": w.gh_created_at.isoformat() if w.gh_created_at else None,
    }


def _leaf_sort_key(leaf: dict) -> tuple:
    return (
        0 if leaf["state"] == "open" else 1,
        0 if leaf["score"] is not None else 1,
        -(leaf["score"] or 0.0),
        leaf["ghCreatedAt"] or "9999",
        leaf["number"] or 0,
    )


@router.get("/leaves")
async def get_leaves(
    capability: str | None = Query(default=None),
    initiative: str | None = Query(default=None),
    _identity: str = Depends(current_identity),
    db=Depends(get_db),
) -> dict:
    if bool(capability) == bool(initiative):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail={
            "error": "bad_scope", "message": "pass exactly one of capability= or initiative="})
    if capability:
        feat = (await db.execute(select(Feature).where(Feature.id == capability))).scalar_one_or_none()
        if feat is None:
            raise HTTPException(status_code=404, detail={"error": "not_found"})
        items = (await db.execute(select(WorkItem).where(WorkItem.feature_id == feat.id))).scalars().all()
    else:
        ini = (await db.execute(select(Initiative).where(Initiative.slug == initiative))).scalar_one_or_none()
        if ini is None:
            raise HTTPException(status_code=404, detail={"error": "not_found"})
        items = []
        if ini.epic_repo and ini.epic_number:
            epic = (await db.execute(select(WorkItem).where(
                WorkItem.repo == ini.epic_repo, WorkItem.number == ini.epic_number,
                WorkItem.kind == "issue"))).scalar_one_or_none()
            if epic is not None:
                items = (await db.execute(select(WorkItem).where(
                    WorkItem.parent_work_item_id == epic.id))).scalars().all()
    blocked, session, blockers = await _issue_facts(db, items)
    parent_ids = {w.parent_work_item_id for w in items if w.parent_work_item_id}
    parents = {p.id: _ref(p) for p in (await db.execute(select(WorkItem).where(
        WorkItem.id.in_(parent_ids)))).scalars().all()} if parent_ids else {}
    leaves = [_leaf(w, issue_tier(w.state, w.triage_state, blocked[w.id], session[w.id]),
                    blockers[w.id], session[w.id], parents.get(w.parent_work_item_id))
              for w in items]
    leaves.sort(key=_leaf_sort_key)
    return {"items": leaves}
```

Check `rice_score`'s exact signature with `sed -n 40,50p server/pm/score.py` and match the argument order; the test expects `5*5*5/1 = 125.0`. If `rice_score` returns `None` for incomplete inputs, the sort key above already handles it.

- [ ] **Step 4: Run the tests, ruff and mypy**

Run: `env -i PATH="$PATH" HOME="$HOME" LANG=C.UTF-8 CCSWITCH_DEV=1 /home/tony/GitHub/ccswitch/.venv/bin/python -m pytest server/tests/test_pm_map_api.py -q -p no:randomly && env -i PATH="$PATH" HOME="$HOME" LANG=C.UTF-8 /home/tony/GitHub/ccswitch/.venv/bin/ruff check server/ && env -i PATH="$PATH" HOME="$HOME" LANG=C.UTF-8 /home/tony/GitHub/ccswitch/.venv/bin/mypy server/ --ignore-missing-imports`
Expected: PASS, clean. Add `("GET", r"/api/pm/leaves")` to the assistant policy allowlist beside `/api/pm/map` if Task 7 added one there.

- [ ] **Step 5: Commit**

```bash
printf 'feat(pm): GET /api/pm/leaves — the issues under one capability or initiative, ordered by score\n\nRefs #993\n\nCo-Authored-By: Claude <model> <noreply@anthropic.com>\n' > /tmp/p2-t8.txt
git add server/api/pm_tree.py server/tests/test_pm_map_api.py server/assistant/policy.py
git commit -F /tmp/p2-t8.txt
```

---

### Task 9: Verify the whole gate and open the PR

- [ ] **Step 1: Server gate, all four steps**

```bash
[ -d web/dist ] && mv web/dist /tmp/p2-dist-aside
env -i PATH="$PATH" HOME="$HOME" LANG=C.UTF-8 CCSWITCH_DEV=1 bash -c '/home/tony/GitHub/ccswitch/.venv/bin/ruff check src/ server/ && /home/tony/GitHub/ccswitch/.venv/bin/mypy server/ --ignore-missing-imports && PYTHONPATH="$PWD/src" /home/tony/GitHub/ccswitch/.venv/bin/python -m pytest tests/ -q -p no:randomly && /home/tony/GitHub/ccswitch/.venv/bin/python -m pytest server/tests/ -n auto --dist loadfile --timeout=300 -q -p no:randomly' 2>&1 | tee /tmp/p2-server-gate.log | tail -20
[ -d /tmp/p2-dist-aside ] && mv /tmp/p2-dist-aside web/dist
```

Expected: all green. Known host-environmental failures on rocky are recorded in memory (`rocky-ipc-entrypoint-test-fails-on-clean-master`); confirm any failure against a clean `origin/master` worktree before treating it as a regression.

- [ ] **Step 2: Web gate**

No web files change in this phase; run `(cd web && npm run typecheck && npm run lint)` once to prove it.

- [ ] **Step 3: Push and open the PR**

The branch stacks on `feat/993-phase1-shell-dead-ends` until PR #1439 merges. Push with `git push -u origin HEAD`; open the PR with `--base feat/993-phase1-shell-dead-ends` if #1439 is still open, else `--base master` after `git rebase origin/master`. Body file `/tmp/p2-pr-body.md`:

```markdown
## Summary

Phase 2 of the roadmap drill-down (spec `docs/superpowers/specs/2026-09-07-roadmap-drilldown-design.md` §10-§11, §13.2): the backend for the capability tree.

- **Capability tree:** feature docs carry `parent:`; the index writes `features.parent_feature_id`, breaks cycles at index time with a logged warning, and rewrites `feature_links` from the docs' `links:` on every sync (the table had no writer before).
- **Initiatives ↔ capabilities:** `POST /api/sdlc/initiatives` accepts `primaryCapabilityId` / `capabilityIds` and derives the product; every initiative payload carries `projectSlug`, `primaryCapability`, `capabilities`.
- **`GET /api/pm/map`:** every product's nested capabilities, initiatives with the capabilities they touch, per-node rollup counts and one attention tier (`needs-you > blocked > in-flight > proposed > live > idle`), computed once server-side in `server/pm/tree.py`.
- **`GET /api/pm/leaves`:** the issues under one capability or initiative, ordered open-first by score.
- **Dead code dropped:** `initiative_repos` and the four unused score columns on `initiatives` (migration `0035_roadmap_tree`).

No web changes; Phase 3 renders this.

Refs #993

🤖 Generated with [Claude Code](https://claude.com/claude-code)
```

```bash
gh pr create --title "feat(roadmap): phase 2 — backend for the capability tree (#993)" --body-file /tmp/p2-pr-body.md --base <base>
gh pr merge <#> --squash --auto
```

# Persona layer in the planning spine — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task (mandated for this repo by `.agents/instructions/core.md` — never ask which execution mode to use). Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give ViberPit's PM spine a `docs/product/personas/<slug>.md` convention, indexed by pm-sync into a `Persona` table per product, linkable from capability docs via a `personas:` frontmatter field, and surfaced by roadmap health — so a persona has an identity capability docs, tests, and reviews can cite.

**Architecture:** Personas are a second, lighter-weight doc-index sibling to Feature (capability) docs: a pure parser (`server/pm/personadoc.py`), a pure planner (`server/pm/persona_index.py`, orphan/revive by presence — no map-changes-branch merge, no evidence resolution, since personas are identity docs, not code capabilities), and a DB-write seam (`server/pm/persona_sync_seams.py`). `PmSyncLoop` runs the persona pass before the feature pass each tick so `Feature` rows can resolve declared `personas:` slugs into `Persona` foreign keys the same tick they first appear. `roadmap_health()` gains one more drift check: a capability whose frontmatter names a persona slug that isn't (yet, or no longer) indexed.

**Tech Stack:** Python (FastAPI/SQLAlchemy, async), pytest/pytest-asyncio; TypeScript/React for the one UI surface touched (`RoadmapHealthCard`).

**Spec:** GitHub issue [steiner385/viberpit#2164](https://github.com/steiner385/viberpit/issues/2164) (problem/proposed-change/non-goal already reads as the approved spec; no separate design doc was written — see the session's brainstorming pass for the scope narrowing below).

## Global Constraints

- Governed by `.specify/memory/constitution.md` v1.2.0 — Principle VI (single source of truth): reuse `SLUG_RE` from `server/pm/featuredoc.py` rather than redefining it; extract the shared git-plumbing directory read in `orchestrator.py` rather than copy-pasting it.
- New model columns must be **nullable** (`server/db/*` reconcile ALTER-ADDs them on both SQLite and Postgres automatically); add an Alembic revision anyway as the reviewable record (nothing runs `alembic upgrade` at deploy time).
- Never use `func.now()`/`func.current_timestamp()` — use `server/db/clock.py:db_utc_now()` (mirror existing `_utcnow` default already used by sibling models, which already routes through it).
- End every commit message with `Co-Authored-By: Claude <model> <noreply@anthropic.com>`.
- Personas are **default-branch-only**: no map-changes-branch (PR-proposal) support and no evidence-path resolution — the issue's own non-goal ("ViberPit does not run persona reviews itself") plus their much lower expected doc count make that machinery disproportionate. This is a deliberate scope cut from full feature-doc parity; don't add it speculatively.
- Deferred to follow-up (note in the PR body, do not attempt in this plan): the roadmap capability-tree persona **filter chip** UI, and injecting linked personas' docs into the SDLC intent→spec prompt (`server/sdlc/prompts.py:build_spec_prompt`) — the latter has no existing link between an SDLC intent/spec session and a specific capability slug to resolve personas from, which is its own design question.

## Review Focus

- A persona doc with **no frontmatter fence at all** (agent-authored, like the feature-doc dialect `featuredoc.py` already tolerates) — Task 1 must accept it via the same path-supplies-slug fallback, not silently drop it.
- A feature doc naming a **persona slug that doesn't exist yet** (declared before the persona doc lands, or after it's deleted) — must not raise/skip the whole feature apply; it silently resolves to nothing in `PersonaFeature` and is what Task 9's health check exists to surface.
- **Tick ordering**: persona index must run before feature index in the same tick so a persona doc added in the same PR/commit as the capability doc that cites it links immediately, not one tick late — Task 7's test must assert this ordering, not just that both run.
- A **duplicate persona slug** across two files in `docs/product/personas/` — the planner must not crash; last-one-wins by iteration order is acceptable (mirrors how `plan_feature_index` has no special duplicate-slug handling either — `docs_by_slug` dict overwrite is the existing precedent) but must not KeyError.
- **Postgres compat**: the two new tables + the one new nullable JSON column on `features` must be added to whichever of `_REQUIRED_EXTRAS` (`test_postgres_compat.py`) / `RECONCILE_EXEMPT` (`test_schema_reconcile_coverage.py`) actually require an entry for an all-nullable table — Task 2 must check both, not assume neither applies.

---

### Task 1: Persona doc parser

**Files:**
- Create: `server/pm/personadoc.py`
- Test: `server/tests/test_pm_personadoc.py`

**Interfaces:**
- Consumes: `SLUG_RE` from `server/pm/featuredoc.py` (import, do not redefine).
- Produces: `PersonaDoc` (frozen dataclass: `slug: str, title: str, role: str | None, age_band: str | None, tier: str | None, body: str`), `PersonaDocError(ValueError)`, `parse_persona_doc(text: str, *, path: str | None = None) -> PersonaDoc`.

- [ ] **Step 1: Write the failing tests**

```python
# server/tests/test_pm_personadoc.py
import pytest

from server.pm.personadoc import PersonaDoc, PersonaDocError, parse_persona_doc


def test_parses_full_frontmatter():
    text = (
        "---\n"
        "slug: teen\n"
        "title: Teen\n"
        "role: teen\n"
        "age_band: 13-17\n"
        "tier: standard\n"
        "---\n"
        "# Teen\n\nGoals: independence with guardrails.\n"
    )
    doc = parse_persona_doc(text)
    assert doc == PersonaDoc(
        slug="teen", title="Teen", role="teen", age_band="13-17", tier="standard",
        body="# Teen\n\nGoals: independence with guardrails.\n",
    )


def test_missing_optional_fields_default_to_none():
    doc = parse_persona_doc("---\nslug: guest\ntitle: Guest\n---\nbody\n")
    assert doc.role is None
    assert doc.age_band is None
    assert doc.tier is None


def test_title_falls_back_to_first_h1():
    doc = parse_persona_doc("---\nslug: viewer\n---\n# Viewer\n\nbody\n")
    assert doc.title == "Viewer"


def test_no_fence_falls_back_to_path_slug():
    doc = parse_persona_doc("Just a body, no fence.\n", path="docs/product/personas/kiosk.md")
    assert doc.slug == "kiosk"
    assert doc.title == ""


def test_no_fence_and_no_resolvable_path_raises():
    with pytest.raises(PersonaDocError):
        parse_persona_doc("Just a body, no fence.\n")


def test_missing_slug_and_title_raises():
    with pytest.raises(PersonaDocError):
        parse_persona_doc("---\nrole: teen\n---\nbody\n")


def test_unterminated_fence_raises():
    with pytest.raises(PersonaDocError):
        parse_persona_doc("---\nslug: a\n")


def test_bad_yaml_raises():
    with pytest.raises(PersonaDocError):
        parse_persona_doc("---\nslug: [unclosed\n---\nbody\n")


def test_invalid_slug_from_path_is_not_indexed():
    with pytest.raises(PersonaDocError):
        parse_persona_doc("body only\n", path="docs/product/personas/README.md")
```

Note: `README.md`'s stem is `README`, which fails `SLUG_RE` (must start with a lowercase letter/digit) — same guard `featuredoc._slug_from_path` already relies on.

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=server .venv/bin/python -m pytest server/tests/test_pm_personadoc.py -v` (from the repo's main checkout venv, per the CLAUDE.md worktree note — a fresh worktree has no built env)
Expected: FAIL / collection error — `server.pm.personadoc` does not exist yet.

- [ ] **Step 3: Implement**

```python
# server/pm/personadoc.py
"""Persona-doc convention: parse ``docs/product/personas/<slug>.md`` (issue
#2164). Pure, no I/O — mirrors ``server/pm/featuredoc.py``'s reading rules
(frontmatter, or a bare body whose path supplies the slug) but carries none of
that module's evidence/links/map-changes-branch machinery: a persona is an
identity doc, not a code capability, so pm-sync indexes it default-branch-only.
"""
from __future__ import annotations

from dataclasses import dataclass

import yaml

from server.pm.featuredoc import SLUG_RE


class PersonaDocError(ValueError):
    """Raised for a malformed persona doc. Callers log and skip — one bad doc
    must never poison the whole index (mirrors FeatureDocError)."""


@dataclass(frozen=True)
class PersonaDoc:
    slug: str
    title: str
    role: str | None
    age_band: str | None
    tier: str | None
    body: str


def _title_from_body(body: str) -> str:
    for line in body.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return ""


def _slug_from_path(path: str | None) -> str:
    if not path:
        return ""
    stem = path.rsplit("/", 1)[-1].removesuffix(".md").strip()
    return stem if SLUG_RE.match(stem) else ""


def parse_persona_doc(text: str, *, path: str | None = None) -> PersonaDoc:
    meta: dict = {}
    if text.startswith("---"):
        lines = text.splitlines()
        try:
            close = next(i for i, ln in enumerate(lines[1:], start=1) if ln.strip() == "---")
        except StopIteration:
            raise PersonaDocError("unterminated frontmatter fence") from None
        raw = "\n".join(lines[1:close])
        try:
            loaded = yaml.safe_load(raw) or {}
        except yaml.YAMLError as e:
            raise PersonaDocError(f"bad yaml frontmatter: {e}") from e
        if not isinstance(loaded, dict):
            raise PersonaDocError("frontmatter is not a mapping")
        meta = loaded
        body = "\n".join(lines[close + 1:]).lstrip("\n")
    elif _slug_from_path(path):
        body = text.lstrip("\n")
    else:
        raise PersonaDocError("missing frontmatter fence")

    slug = str(meta.get("slug") or "").strip() or _slug_from_path(path)
    title = str(meta.get("title") or "").strip() or _title_from_body(body)
    if not slug or not title:
        raise PersonaDocError("slug and title are required")

    def _opt(key: str) -> str | None:
        v = meta.get(key)
        v = str(v).strip() if v is not None else ""
        return v or None

    return PersonaDoc(slug=slug, title=title, role=_opt("role"),
                      age_band=_opt("age_band"), tier=_opt("tier"), body=body)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH=server .venv/bin/python -m pytest server/tests/test_pm_personadoc.py -v`
Expected: PASS (all 9 tests)

- [ ] **Step 5: Commit**

```bash
git add server/pm/personadoc.py server/tests/test_pm_personadoc.py
git commit -m "$(cat <<'EOF'
feat(pm): parse docs/product/personas/<slug>.md (#2164)

Co-Authored-By: Claude <model> <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: `Persona` / `PersonaFeature` models + `Feature.persona_slugs` + migration

**Files:**
- Modify: `server/db/models.py` (add `Persona`, `PersonaFeature` classes near `Feature`/`FeatureLink`, around line 1858; add `persona_slugs` column to `Feature`, near `route_labels` at line 1802)
- Create: `server/db/migrations/versions/0068_persona.py`
- Test: `server/tests/test_db_persona_models.py`
- Check (do not skip): `server/tests/test_postgres_compat.py` (`_REQUIRED_EXTRAS`), `server/tests/test_schema_reconcile_coverage.py` (`RECONCILE_EXEMPT`)

**Interfaces:**
- Produces: `Persona` (table `personas`: `id, project_id (FK projects.id), slug, title, role, age_band, tier, summary, repo_path, orphaned, created_at, updated_at`; `UniqueConstraint("project_id", "slug", name="uq_persona_slug")`), `PersonaFeature` (table `persona_features`: `id, persona_id (FK personas.id), feature_id (FK features.id), created_at`; `UniqueConstraint("persona_id", "feature_id", name="uq_persona_feature")`), `Feature.persona_slugs: Mapped[list | None]` (JSON, `none_as_null=True`).

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_db_persona_models.py
import pytest
from sqlalchemy.exc import IntegrityError

from server.db.models import Feature, Persona, PersonaFeature, Project


@pytest.mark.asyncio
async def test_persona_unique_per_project_slug(isolated_db):
    async with isolated_db() as db:
        proj = Project(slug="p1", name="P1")
        db.add(proj)
        await db.flush()
        db.add(Persona(project_id=proj.id, slug="teen", title="Teen"))
        await db.flush()
        db.add(Persona(project_id=proj.id, slug="teen", title="Teen dup"))
        with pytest.raises(IntegrityError):
            await db.flush()


@pytest.mark.asyncio
async def test_persona_feature_link_and_feature_persona_slugs_column(isolated_db):
    async with isolated_db() as db:
        proj = Project(slug="p2", name="P2")
        db.add(proj)
        await db.flush()
        persona = Persona(project_id=proj.id, slug="teen", title="Teen")
        feat = Feature(project_id=proj.id, slug="chores", title="Chores",
                       persona_slugs=["teen", "parent-admin"])
        db.add_all([persona, feat])
        await db.flush()
        db.add(PersonaFeature(persona_id=persona.id, feature_id=feat.id))
        await db.flush()
        got = (await db.get(Feature, feat.id))
        assert got.persona_slugs == ["teen", "parent-admin"]
```

(`isolated_db` is the canonical temp-DB fixture already used across `server/tests/` — confirm its exact import location with `grep -rn "def isolated_db" server/tests/conftest.py` before writing; do not hand-roll a temp-DB context manager.)

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=server .venv/bin/python -m pytest server/tests/test_db_persona_models.py -v`
Expected: FAIL — `ImportError: cannot import name 'Persona'`

- [ ] **Step 3: Add the models**

In `server/db/models.py`, add the `persona_slugs` column to `Feature` right after `route_labels` (line ~1803):

```python
    # `personas:` frontmatter (issue #2164) — the doc's declared persona slugs
    # verbatim, mirroring `route_labels`: a raw mirror of what the doc says,
    # not the resolved graph. `PersonaFeature` (below) is the resolved graph —
    # only slugs that matched a live Persona row at apply time. Declared-but-
    # unresolved is what `roadmap_health`'s `unlinked_personas` surfaces.
    persona_slugs: Mapped[list | None] = mapped_column(
        JSON(none_as_null=True), nullable=True, default=None)
```

Then, after the `FeatureLink` class (after line 1858, before `FEATURE_MAP_CHANGE_STATUSES`):

```python
class Persona(Base):
    """Index row for one ``docs/product/personas/<slug>.md`` (issue #2164):
    the identity a capability's `personas:` frontmatter cites and a spec's
    acceptance criteria can name. Default-branch-only — unlike Feature there
    is no map-changes-branch PR-proposal flow and no evidence citation; a
    persona is an identity doc, not a code capability. ``orphaned`` marks a
    doc that disappeared from the repo (row kept so PersonaFeature links never
    dangle, same as Feature/orphaned)."""

    __tablename__ = "personas"
    __table_args__ = (UniqueConstraint("project_id", "slug", name="uq_persona_slug"),)

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_new_uuid)
    project_id: Mapped[str] = mapped_column(
        String, ForeignKey("projects.id"), nullable=True, index=True)
    slug: Mapped[str | None] = mapped_column(String, nullable=True)
    title: Mapped[str | None] = mapped_column(String, nullable=True)
    role: Mapped[str | None] = mapped_column(String, nullable=True)
    age_band: Mapped[str | None] = mapped_column(String, nullable=True)
    tier: Mapped[str | None] = mapped_column(String, nullable=True)
    summary: Mapped[str | None] = mapped_column(String, nullable=True)
    repo_path: Mapped[str | None] = mapped_column(String, nullable=True)
    orphaned: Mapped[bool | None] = mapped_column(Boolean, nullable=True, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow)


class PersonaFeature(Base):
    """Resolved persona→capability edge (issue #2164): a capability's
    `personas:` frontmatter slug that matched a live Persona row in the same
    project at the last pm-sync apply. REWRITTEN on every feature-plan apply,
    same as FeatureEvidence/FeatureLink — the docs are truth."""

    __tablename__ = "persona_features"
    __table_args__ = (
        UniqueConstraint("persona_id", "feature_id", name="uq_persona_feature"),)

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_new_uuid)
    persona_id: Mapped[str] = mapped_column(
        String, ForeignKey("personas.id"), nullable=True, index=True)
    feature_id: Mapped[str] = mapped_column(
        String, ForeignKey("features.id"), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=server .venv/bin/python -m pytest server/tests/test_db_persona_models.py -v`
Expected: PASS

- [ ] **Step 5: Add the Alembic revision**

```python
# server/db/migrations/versions/0068_persona.py
"""Persona layer in the planning spine (issue #2164): personas + persona_features
tables, and features.persona_slugs.

Revision ID: 0068_persona
Revises: 0067_archive_review_finding

Nothing runs `alembic upgrade` at deploy time: `create_all` creates the two new
tables and `_reconcile_columns` ALTER-ADDs the nullable `persona_slugs` column
on both dialects. This revision is the reviewable record of that change.
"""
from alembic import op
import sqlalchemy as sa

revision = "0068_persona"
down_revision = "0067_archive_review_finding"
branch_labels = None
depends_on = None


def upgrade() -> None:
    existing = set(sa.inspect(op.get_bind()).get_table_names())
    if "personas" not in existing:
        op.create_table(
            "personas",
            sa.Column("id", sa.String(), primary_key=True),
            sa.Column("project_id", sa.String(), sa.ForeignKey("projects.id"), nullable=True),
            sa.Column("slug", sa.String(), nullable=True),
            sa.Column("title", sa.String(), nullable=True),
            sa.Column("role", sa.String(), nullable=True),
            sa.Column("age_band", sa.String(), nullable=True),
            sa.Column("tier", sa.String(), nullable=True),
            sa.Column("summary", sa.String(), nullable=True),
            sa.Column("repo_path", sa.String(), nullable=True),
            sa.Column("orphaned", sa.Boolean(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("project_id", "slug", name="uq_persona_slug"),
        )
    if "persona_features" not in existing:
        op.create_table(
            "persona_features",
            sa.Column("id", sa.String(), primary_key=True),
            sa.Column("persona_id", sa.String(), sa.ForeignKey("personas.id"), nullable=True),
            sa.Column("feature_id", sa.String(), sa.ForeignKey("features.id"), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("persona_id", "feature_id", name="uq_persona_feature"),
        )
    columns = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("features")}
    if "persona_slugs" not in columns:
        op.add_column("features", sa.Column("persona_slugs", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("features", "persona_slugs")
    op.drop_table("persona_features")
    op.drop_table("personas")
```

- [ ] **Step 6: Check the two hand-maintained Postgres-compat lists**

Read `server/tests/test_postgres_compat.py` and `server/tests/test_schema_reconcile_coverage.py`. `_REQUIRED_EXTRAS` only needs an entry for a **NOT NULL** column with no synthesizable default — every new column here is nullable, so no entry should be needed; confirm by running (env permitting):

Run: `PYTHONPATH=server .venv/bin/python -m pytest server/tests/test_postgres_compat.py server/tests/test_schema_reconcile_coverage.py -v`
Expected: PASS with no changes. If either fails naming `Persona`/`PersonaFeature`/`persona_slugs`, add the named entry the failure message asks for — do not silently skip the file.

- [ ] **Step 7: Commit**

```bash
git add server/db/models.py server/db/migrations/versions/0068_persona.py server/tests/test_db_persona_models.py
git commit -m "$(cat <<'EOF'
feat(db): add Persona/PersonaFeature models and Feature.persona_slugs (#2164)

Co-Authored-By: Claude <model> <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: `personas:` frontmatter on feature docs

**Files:**
- Modify: `server/pm/featuredoc.py` (`FeatureDoc` dataclass ~line 94, `parse_feature_doc` ~line 176, `serialize_feature_doc` ~line 257)
- Test: `server/tests/test_pm_featuredoc.py` (add cases; find and follow its existing test style for `labels:`, which this mirrors exactly)

**Interfaces:**
- Consumes: nothing new.
- Produces: `FeatureDoc.personas: tuple[str, ...] = ()` — parsed from an optional `personas:` frontmatter list, same shape/rules as the existing `labels:` field.

- [ ] **Step 1: Write the failing tests**

Add to `server/tests/test_pm_featuredoc.py` (read the file first to match its existing fixture/assertion style for `labels`):

```python
def test_parses_personas_list():
    doc = parse_feature_doc(
        "---\nslug: chores\ntitle: Chores\npersonas: [teen, parent-admin]\n---\nbody\n")
    assert doc.personas == ("teen", "parent-admin")


def test_personas_defaults_to_empty_tuple():
    doc = parse_feature_doc("---\nslug: chores\ntitle: Chores\n---\nbody\n")
    assert doc.personas == ()


def test_personas_non_list_value_yields_empty_tuple():
    doc = parse_feature_doc(
        "---\nslug: chores\ntitle: Chores\npersonas: teen\n---\nbody\n")
    assert doc.personas == ()


def test_serialize_round_trips_personas():
    doc = FeatureDoc(slug="chores", title="Chores", state="live", links=(), body="body\n",
                     personas=("teen", "parent-admin"))
    reparsed = parse_feature_doc(serialize_feature_doc(doc))
    assert reparsed.personas == ("teen", "parent-admin")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=server .venv/bin/python -m pytest server/tests/test_pm_featuredoc.py -k personas -v`
Expected: FAIL — `TypeError: __init__() got an unexpected keyword argument 'personas'` / `AttributeError`.

- [ ] **Step 3: Implement**

In `server/pm/featuredoc.py`, add to `FeatureDoc` (after `evidence`, line 103):

```python
    personas: tuple[str, ...] = ()
```

In `parse_feature_doc`, right after the existing `labels` block (after line 249, before the `return FeatureDoc(...)`):

```python
    raw_personas = meta.get("personas")
    personas = (tuple(str(x).strip() for x in raw_personas if str(x).strip())
                if isinstance(raw_personas, list) else ())
```

Update the `return FeatureDoc(...)` call to add `personas=personas,`.

In `serialize_feature_doc`, after the `labels` block (after line 262):

```python
    if doc.personas:
        meta["personas"] = list(doc.personas)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH=server .venv/bin/python -m pytest server/tests/test_pm_featuredoc.py -v`
Expected: PASS (full file — confirm no regression in the pre-existing tests, not just the new ones)

- [ ] **Step 5: Commit**

```bash
git add server/pm/featuredoc.py server/tests/test_pm_featuredoc.py
git commit -m "$(cat <<'EOF'
feat(pm): parse personas: frontmatter on feature docs (#2164)

Co-Authored-By: Claude <model> <noreply@anthropic.com>
EOF
)"
```

---

### Task 4: Persona index planner

**Files:**
- Create: `server/pm/persona_index.py`
- Test: `server/tests/test_pm_persona_index.py`

**Interfaces:**
- Consumes: `PersonaDoc`, `PersonaDocError`, `parse_persona_doc` from Task 1.
- Produces: `PersonaRowView` (`slug: str, orphaned: bool`), `PersonaUpsert` (`slug, title, role, age_band, tier, summary, repo_path`), `PersonaIndexPlan` (`upserts: tuple[PersonaUpsert, ...], orphan_slugs: tuple[str, ...], revive_slugs: tuple[str, ...]`), `plan_persona_index(existing: list[PersonaRowView], docs: list[tuple[str, PersonaDoc]]) -> PersonaIndexPlan`.

- [ ] **Step 1: Write the failing tests**

```python
# server/tests/test_pm_persona_index.py
from server.pm.persona_index import PersonaRowView, plan_persona_index
from server.pm.personadoc import PersonaDoc


def _doc(slug: str, body: str = "## Goals\n\nIndependence with guardrails.") -> PersonaDoc:
    return PersonaDoc(slug=slug, title=slug.title(), role=None, age_band=None, tier=None, body=body)


def test_upserts_every_doc_with_summary_from_first_body_line():
    plan = plan_persona_index([], [("docs/product/personas/teen.md", _doc("teen"))])
    assert len(plan.upserts) == 1
    up = plan.upserts[0]
    assert up.slug == "teen"
    assert up.title == "Teen"
    assert up.repo_path == "docs/product/personas/teen.md"
    assert up.summary == "Independence with guardrails."


def test_missing_doc_orphans_existing_row():
    existing = [PersonaRowView(slug="teen", orphaned=False)]
    plan = plan_persona_index(existing, [])
    assert plan.orphan_slugs == ("teen",)
    assert plan.revive_slugs == ()


def test_reappearing_doc_revives_orphaned_row():
    existing = [PersonaRowView(slug="teen", orphaned=True)]
    plan = plan_persona_index(existing, [("docs/product/personas/teen.md", _doc("teen"))])
    assert plan.revive_slugs == ("teen",)
    assert plan.orphan_slugs == ()


def test_untouched_row_is_neither_orphaned_nor_revived():
    existing = [PersonaRowView(slug="teen", orphaned=False)]
    plan = plan_persona_index(existing, [("docs/product/personas/teen.md", _doc("teen"))])
    assert plan.orphan_slugs == ()
    assert plan.revive_slugs == ()


def test_duplicate_slug_across_docs_does_not_raise_last_one_wins():
    docs = [("docs/product/personas/teen-1.md", _doc("teen", "First.")),
            ("docs/product/personas/teen-2.md", _doc("teen", "Second."))]
    plan = plan_persona_index([], docs)
    assert len(plan.upserts) == 1
    assert plan.upserts[0].summary == "Second."
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=server .venv/bin/python -m pytest server/tests/test_pm_persona_index.py -v`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement**

```python
# server/pm/persona_index.py
"""Pure planner: diff parsed persona docs against existing Persona rows
(issue #2164). Mirrors the non-evidence, non-map-changes-branch subset of
``server/pm/feature_index.py`` — personas are default-branch-only."""
from __future__ import annotations

from dataclasses import dataclass

from server.pm.personadoc import PersonaDoc

_SUMMARY_MAX = 200


@dataclass(frozen=True)
class PersonaRowView:
    slug: str
    orphaned: bool


@dataclass(frozen=True)
class PersonaUpsert:
    slug: str
    title: str
    role: str | None
    age_band: str | None
    tier: str | None
    summary: str
    repo_path: str


@dataclass(frozen=True)
class PersonaIndexPlan:
    upserts: tuple[PersonaUpsert, ...]
    orphan_slugs: tuple[str, ...]
    revive_slugs: tuple[str, ...]


def _summary(body: str) -> str:
    for line in body.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            return stripped[:_SUMMARY_MAX]
    return ""


def plan_persona_index(
    existing: list[PersonaRowView], docs: list[tuple[str, PersonaDoc]],
) -> PersonaIndexPlan:
    seen = {doc.slug for _, doc in docs}
    by_slug: dict[str, tuple[str, PersonaDoc]] = {}
    for path, doc in docs:
        by_slug[doc.slug] = (path, doc)  # last one wins — mirrors plan_feature_index
    upserts = tuple(
        PersonaUpsert(slug=doc.slug, title=doc.title, role=doc.role, age_band=doc.age_band,
                     tier=doc.tier, summary=_summary(doc.body), repo_path=path)
        for path, doc in by_slug.values())
    orphan_slugs = tuple(
        row.slug for row in existing if not row.orphaned and row.slug not in seen)
    revive_slugs = tuple(
        row.slug for row in existing if row.orphaned and row.slug in seen)
    return PersonaIndexPlan(upserts=upserts, orphan_slugs=orphan_slugs, revive_slugs=revive_slugs)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH=server .venv/bin/python -m pytest server/tests/test_pm_persona_index.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add server/pm/persona_index.py server/tests/test_pm_persona_index.py
git commit -m "$(cat <<'EOF'
feat(pm): persona-doc index planner (#2164)

Co-Authored-By: Claude <model> <noreply@anthropic.com>
EOF
)"
```

---

### Task 5: Persona DB-apply seam

**Files:**
- Create: `server/pm/persona_sync_seams.py`
- Test: `server/tests/test_pm_persona_sync_seams.py`

**Interfaces:**
- Consumes: `PersonaIndexPlan`, `PersonaRowView` from Task 4; `Persona` from Task 2.
- Produces: `list_persona_rows(project_id: str) -> list[PersonaRowView]`, `apply_persona_plan(project_id: str, plan: PersonaIndexPlan) -> None`.

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_pm_persona_sync_seams.py
import pytest

from server.db import db_session
from server.db.models import Persona, Project
from server.pm.persona_index import PersonaIndexPlan, PersonaRowView, PersonaUpsert
from server.pm.persona_sync_seams import apply_persona_plan, list_persona_rows


@pytest.mark.asyncio
async def test_apply_upserts_then_orphans_then_revives(isolated_db):
    async with isolated_db() as db:
        proj = Project(slug="p1", name="P1")
        db.add(proj)
        await db.flush()
        project_id = proj.id

    plan = PersonaIndexPlan(
        upserts=(PersonaUpsert(slug="teen", title="Teen", role="teen", age_band="13-17",
                               tier="standard", summary="s", repo_path="docs/product/personas/teen.md"),),
        orphan_slugs=(), revive_slugs=())
    await apply_persona_plan(project_id, plan)
    rows = await list_persona_rows(project_id)
    assert rows == [PersonaRowView(slug="teen", orphaned=False)]

    orphan_plan = PersonaIndexPlan(upserts=(), orphan_slugs=("teen",), revive_slugs=())
    await apply_persona_plan(project_id, orphan_plan)
    rows = await list_persona_rows(project_id)
    assert rows == [PersonaRowView(slug="teen", orphaned=True)]

    revive_plan = PersonaIndexPlan(
        upserts=(PersonaUpsert(slug="teen", title="Teen", role="teen", age_band="13-17",
                               tier="standard", summary="s2", repo_path="docs/product/personas/teen.md"),),
        orphan_slugs=(), revive_slugs=("teen",))
    await apply_persona_plan(project_id, revive_plan)
    rows = await list_persona_rows(project_id)
    assert rows == [PersonaRowView(slug="teen", orphaned=False)]
    async with db_session() as db:
        p = (await db.execute(
            __import__("sqlalchemy").select(Persona).where(Persona.project_id == project_id)
        )).scalar_one()
        assert p.summary == "s2"
```

(Confirm `db_session`/`isolated_db` wiring against `server/tests/test_pm_feature_index.py`'s DB-touching sibling if one exists, or `server/tests/test_db_persona_models.py` from Task 2, so both tests share one fixture story.)

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=server .venv/bin/python -m pytest server/tests/test_pm_persona_sync_seams.py -v`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement**

```python
# server/pm/persona_sync_seams.py
"""The persona-index DB-write seam (issue #2164) — mirrors
``server/pm/feature_sync_seams.py``'s ``list_feature_rows``/``apply_feature_plan``
but with no clone-dependent read half: personas are default-branch-only, so
there is exactly one seam pair, used identically by the daemon's pm-sync tick."""
from __future__ import annotations

from sqlalchemy import select

from server.db import db_session
from server.db.models import Persona

from .persona_index import PersonaIndexPlan, PersonaRowView


async def list_persona_rows(project_id: str) -> list[PersonaRowView]:
    async with db_session() as db:
        rows = (await db.execute(
            select(Persona).where(Persona.project_id == project_id)
        )).scalars().all()
    return [PersonaRowView(slug=p.slug or "", orphaned=bool(p.orphaned)) for p in rows]


async def apply_persona_plan(project_id: str, plan: PersonaIndexPlan) -> None:
    async with db_session() as db:
        for up in plan.upserts:
            row = (await db.execute(
                select(Persona).where(
                    Persona.project_id == project_id, Persona.slug == up.slug)
            )).scalar_one_or_none()
            if row is None:
                row = Persona(project_id=project_id, slug=up.slug)
                db.add(row)
            row.title = up.title
            row.role = up.role
            row.age_band = up.age_band
            row.tier = up.tier
            row.summary = up.summary
            row.repo_path = up.repo_path
            row.orphaned = False
        for slug in plan.orphan_slugs:
            row = (await db.execute(
                select(Persona).where(
                    Persona.project_id == project_id, Persona.slug == slug)
            )).scalar_one_or_none()
            if row is not None:
                row.orphaned = True
        await db.flush()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=server .venv/bin/python -m pytest server/tests/test_pm_persona_sync_seams.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add server/pm/persona_sync_seams.py server/tests/test_pm_persona_sync_seams.py
git commit -m "$(cat <<'EOF'
feat(pm): persona-index DB-apply seam (#2164)

Co-Authored-By: Claude <model> <noreply@anthropic.com>
EOF
)"
```

---

### Task 6: Resolve `personas:` into `Feature.persona_slugs` + `PersonaFeature` links

**Files:**
- Modify: `server/pm/feature_index.py` (`FeatureUpsert` ~line 49, `plan_feature_index` ~line 220)
- Modify: `server/pm/feature_sync_seams.py` (`apply_feature_plan`, add persona-link block after the `route_labels` write and before the tree/evidence/links blocks)
- Test: `server/tests/test_pm_feature_index.py` (add cases), `server/tests/test_pm_feature_sync_seams.py` (find/create alongside the existing DB-integration test for `apply_feature_plan` — check whether such a file already exists first: `ls server/tests | grep feature_sync_seams`)

**Interfaces:**
- Consumes: `FeatureDoc.personas` (Task 3), `Persona` (Task 2).
- Produces: `FeatureUpsert.personas: tuple[str, ...] = ()`; `apply_feature_plan` writes `Feature.persona_slugs = list(up.personas) or None` and rewrites `PersonaFeature` rows for every `(feature, persona-in-up.personas-that-resolves)` pair in the plan, scoped to the same project (personas are not cross-project, unlike `FeatureLink`).

- [ ] **Step 1: Write the failing tests**

Add to `server/tests/test_pm_feature_index.py`:

```python
def test_plan_carries_personas_through_from_doc():
    doc = FeatureDoc(slug="chores", title="Chores", state="live", links=(), body="x",
                     personas=("teen", "parent-admin"))
    plan = plan_feature_index([], [("docs/product/features/chores.md", doc)])
    assert plan.upserts[0].personas == ("teen", "parent-admin")


def test_plan_personas_defaults_to_empty_tuple():
    doc = _doc("chores")
    plan = plan_feature_index([], [("docs/product/features/chores.md", doc)])
    assert plan.upserts[0].personas == ()
```

Create (or extend, if it exists) `server/tests/test_pm_feature_sync_seams.py`:

```python
import pytest
from sqlalchemy import select

from server.db.models import Feature, Persona, PersonaFeature, Project
from server.pm.feature_index import FeatureIndexPlan, FeatureUpsert
from server.pm.feature_sync_seams import apply_feature_plan


@pytest.mark.asyncio
async def test_apply_writes_persona_slugs_and_resolved_links(isolated_db):
    async with isolated_db() as db:
        proj = Project(slug="p1", name="P1")
        db.add(proj)
        await db.flush()
        project_id = proj.id
        persona = Persona(project_id=project_id, slug="teen", title="Teen")
        db.add(persona)
        await db.flush()

    plan = FeatureIndexPlan(
        upserts=(FeatureUpsert(slug="chores", title="Chores", state="live", summary="",
                               repo_path="docs/product/features/chores.md", parent_slug=None,
                               links=(), personas=("teen", "unknown-slug")),),
        orphan_slugs=(), revive_slugs=(), warnings=())
    await apply_feature_plan(project_id, plan)

    async with isolated_db() as db:
        feat = (await db.execute(
            select(Feature).where(Feature.project_id == project_id, Feature.slug == "chores")
        )).scalar_one()
        assert feat.persona_slugs == ["teen", "unknown-slug"]
        links = (await db.execute(
            select(PersonaFeature).where(PersonaFeature.feature_id == feat.id)
        )).scalars().all()
        assert len(links) == 1  # "unknown-slug" doesn't resolve to a live Persona row


@pytest.mark.asyncio
async def test_reapply_with_fewer_personas_drops_stale_link(isolated_db):
    async with isolated_db() as db:
        proj = Project(slug="p2", name="P2")
        db.add(proj)
        await db.flush()
        project_id = proj.id
        persona = Persona(project_id=project_id, slug="teen", title="Teen")
        db.add(persona)
        await db.flush()

    plan = FeatureIndexPlan(
        upserts=(FeatureUpsert(slug="chores", title="Chores", state="live", summary="",
                               repo_path="p", parent_slug=None, links=(), personas=("teen",)),),
        orphan_slugs=(), revive_slugs=(), warnings=())
    await apply_feature_plan(project_id, plan)

    reapplied = FeatureIndexPlan(
        upserts=(FeatureUpsert(slug="chores", title="Chores", state="live", summary="",
                               repo_path="p", parent_slug=None, links=(), personas=()),),
        orphan_slugs=(), revive_slugs=(), warnings=())
    await apply_feature_plan(project_id, reapplied)

    async with isolated_db() as db:
        feat = (await db.execute(
            select(Feature).where(Feature.project_id == project_id, Feature.slug == "chores")
        )).scalar_one()
        assert feat.persona_slugs is None
        links = (await db.execute(
            select(PersonaFeature).where(PersonaFeature.feature_id == feat.id)
        )).scalars().all()
        assert links == []
```

(If `isolated_db` reopens a fresh in-memory DB per `async with` block in this project's fixture, adjust to hold one open session across both assert blocks per this repo's actual fixture contract — check `server/tests/conftest.py` before finalizing; the shape above is illustrative of the two behaviors under test, not a literal fixture-usage guarantee.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=server .venv/bin/python -m pytest server/tests/test_pm_feature_index.py -k personas server/tests/test_pm_feature_sync_seams.py -v`
Expected: FAIL — `TypeError` on `FeatureUpsert(..., personas=...)`.

- [ ] **Step 3: Implement**

In `server/pm/feature_index.py`, add to `FeatureUpsert` (after `labels`, line 57):

```python
    personas: tuple[str, ...] = ()
```

In `plan_feature_index`, in the `FeatureUpsert(...)` construction (~line 220), add:

```python
            personas=doc.personas,
```

In `server/pm/feature_sync_seams.py`, add the import:

```python
from server.db.models import Feature, FeatureEvidence, FeatureLink, Persona, PersonaFeature, Project
```

After the `row.route_labels = list(up.labels) or None` line (line 106), add:

```python
            row.persona_slugs = list(up.personas) or None
```

After the evidence block and before the "Links: rewrite every outgoing edge" block (i.e. right before line 154's comment), add a new block:

```python
        # Personas (#2164): rewritten per upserted feature, resolved WITHIN
        # this project only (unlike FeatureLink, personas are not cross-
        # project) — a declared slug that doesn't resolve to a live Persona
        # row is simply dropped from the link table; `persona_slugs` above
        # still carries the raw declaration for roadmap_health to flag.
        if up_ids:
            await db.execute(delete(PersonaFeature).where(
                PersonaFeature.feature_id.in_(up_ids)))
            personas_by_slug = {p.slug: p.id for p in (await db.execute(
                select(Persona).where(
                    Persona.project_id == project_id, Persona.orphaned.isnot(True),
                ))).scalars().all()}
            for up in plan.upserts:
                feat = mine.get(up.slug)
                if feat is None:
                    continue
                for pslug in up.personas:
                    pid = personas_by_slug.get(pslug)
                    if pid is not None:
                        db.add(PersonaFeature(persona_id=pid, feature_id=feat.id))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH=server .venv/bin/python -m pytest server/tests/test_pm_feature_index.py server/tests/test_pm_feature_sync_seams.py -v`
Expected: PASS. Also run the full existing feature-index/sync-seams suites to confirm no regression: `PYTHONPATH=server .venv/bin/python -m pytest server/tests/test_pm_feature_index.py -v`.

- [ ] **Step 5: Commit**

```bash
git add server/pm/feature_index.py server/pm/feature_sync_seams.py server/tests/test_pm_feature_index.py server/tests/test_pm_feature_sync_seams.py
git commit -m "$(cat <<'EOF'
feat(pm): resolve capability personas: into persona_slugs + PersonaFeature (#2164)

Co-Authored-By: Claude <model> <noreply@anthropic.com>
EOF
)"
```

---

### Task 7: Wire persona indexing into `PmSyncLoop`

**Files:**
- Modify: `server/automations/pm_sync.py` (`PmSyncLoop.__init__` ~line 183, `tick`/call sites ~lines 564-565 and 611-612, new `_persona_index` method near `_feature_index`)
- Test: `server/tests/test_pm_sync_loop.py` (extend the `_loop()` helper and add new tests)

**Interfaces:**
- Consumes: `PersonaDoc`, `PersonaDocError`, `parse_persona_doc` (Task 1); `plan_persona_index`, `PersonaIndexPlan`, `PersonaRowView` (Task 4); `FeatureDocsRead` (already imported, reused structurally for persona reads — same `sha, docs` shape, no new dataclass needed).
- Produces: `PmSyncLoop.__init__` gains three new optional kwargs — `read_persona_docs: ReadFeatureDocs | None = None, list_persona_rows: Callable[[str], Awaitable[list]] | None = None, apply_persona_plan: Callable[[str, "PersonaIndexPlan"], Awaitable[None]] | None = None` (all default `None`, so every existing test/caller is unaffected) — and a `_persona_index(view: RepoSyncView) -> None` method, called from `tick()` **before** `_feature_index(view)` at both existing call sites.

- [ ] **Step 1: Write the failing tests**

Extend `_loop()` in `server/tests/test_pm_sync_loop.py` to accept and wire the three new optional seams (mirroring how `read_feature_docs` etc. are already threaded through):

```python
def _loop(ctx, views, pages, *, config=None, feature_docs=None, rename_repo=None,
          read_feature_docs=None, read_map_changes=None, refresh_map_changes=None,
          read_constitution=None, persona_docs=None, read_persona_docs=None):
    ...  # existing body unchanged up to the `loop = PmSyncLoop(` call
    persona_applied = []

    async def read_persona_docs_fake(repo, ref=None):
        return FeatureDocsRead(sha="sha-fake", docs=list(persona_docs or []))

    async def list_persona_rows_fake(project_id):
        return []

    async def apply_persona_plan_fake(project_id, plan):
        persona_applied.append((project_id, plan))

    loop = PmSyncLoop(
        ctx, read_config=read_config, list_repos=list_repos, fetch=fetch,
        upsert_items=upsert, save_sync_state=save,
        read_feature_docs=read_feature_docs or read_docs,
        list_feature_rows=list_rows, apply_feature_plan=apply_plan,
        now_utc=lambda: datetime(2026, 2, 1, tzinfo=timezone.utc),
        save_unindexed_docs=save_unindexed,
        rename_repo=rename_repo, read_map_changes=read_map_changes,
        refresh_map_changes=refresh_map_changes,
        read_constitution=read_constitution, save_constitution_status=save_constitution,
        read_persona_docs=read_persona_docs or read_persona_docs_fake,
        list_persona_rows=list_persona_rows_fake,
        apply_persona_plan=apply_persona_plan_fake,
    )
    loop.unindexed_saved = unindexed_saved
    loop.constitution_saved = constitution_saved
    loop.persona_applied = persona_applied
    return loop, saved, upserted, applied
```

Add new tests (append to the file):

```python
@pytest.mark.asyncio
async def test_persona_index_runs_and_applies_a_plan():
    ctx = Ctx()
    persona_docs = [("docs/product/personas/teen.md",
                     "---\nslug: teen\ntitle: Teen\n---\nbody\n")]
    loop, _, _, _ = _loop(
        ctx, [_view(repo="o/r", backfill_done=True)],
        [_issue_page([], None, False), _pr_page([], None, False)],
        persona_docs=persona_docs)
    await loop.tick()
    assert len(loop.persona_applied) == 1
    _, plan = loop.persona_applied[0]
    assert plan.upserts[0].slug == "teen"


@pytest.mark.asyncio
async def test_persona_index_runs_before_feature_index_same_tick():
    """A capability doc citing a persona added in the SAME tick must see it —
    Persona rows have to exist before the feature plan is applied."""
    ctx = Ctx()
    order = []

    async def read_persona(repo, ref=None):
        order.append("persona")
        return FeatureDocsRead(sha="s", docs=[])

    async def read_feature(repo, ref=None):
        order.append("feature")
        return FeatureDocsRead(sha="s", docs=[])

    loop, _, _, _ = _loop(
        ctx, [_view(repo="o/r", backfill_done=True)],
        [_issue_page([], None, False), _pr_page([], None, False)],
        read_persona_docs=read_persona, read_feature_docs=read_feature)
    await loop.tick()
    assert order == ["persona", "feature"]


@pytest.mark.asyncio
async def test_persona_index_is_a_noop_when_seam_not_wired():
    """Every pre-existing PmSyncLoop caller (no persona seams passed) must be
    unaffected — the seams default to None."""
    from server.automations.pm_sync import PmSyncLoop

    ctx = Ctx()

    async def read_config():
        return "on", {}

    async def list_repos():
        return [_view(repo="o/r", backfill_done=True)]

    pages = [_issue_page([], None, False), _pr_page([], None, False)]

    async def fetch(query):
        return pages.pop(0)

    async def upsert(items):
        pass

    async def save(view, **updates):
        pass

    async def read_docs(repo, ref=None):
        return FeatureDocsRead(sha="s", docs=[])

    async def list_rows(project_id):
        return []

    async def apply_plan(project_id, plan):
        pass

    loop = PmSyncLoop(
        ctx, read_config=read_config, list_repos=list_repos, fetch=fetch,
        upsert_items=upsert, save_sync_state=save, read_feature_docs=read_docs,
        list_feature_rows=list_rows, apply_feature_plan=apply_plan,
        now_utc=lambda: datetime(2026, 2, 1, tzinfo=timezone.utc))
    await loop.tick()  # must not raise
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=server .venv/bin/python -m pytest server/tests/test_pm_sync_loop.py -k persona -v`
Expected: FAIL — `TypeError: __init__() got an unexpected keyword argument 'read_persona_docs'`.

- [ ] **Step 3: Implement**

In `server/automations/pm_sync.py`, add the import:

```python
from server.pm.persona_index import PersonaIndexPlan, PersonaRowView, plan_persona_index
from server.pm.personadoc import PersonaDoc, PersonaDocError, parse_persona_doc
```

Add three new type aliases near `ApplyFeaturePlan` (~line 132):

```python
ListPersonaRows = Callable[[str], Awaitable[list[PersonaRowView]]]
ApplyPersonaPlan = Callable[[str, PersonaIndexPlan], Awaitable[None]]
```

Add three new `__init__` params (after `resolve_evidence_paths`, ~line 209) and store them (after `self._resolve_evidence_paths = resolve_evidence_paths`, ~line 222):

```python
        read_persona_docs: ReadFeatureDocs | None = None,
        list_persona_rows: ListPersonaRows | None = None,
        apply_persona_plan: ApplyPersonaPlan | None = None,
```

```python
        # None ⇒ no persona pass — personas are default-branch-only, unlike
        # the feature index's map-changes-branch/evidence machinery (#2164).
        self._read_persona_docs = read_persona_docs
        self._list_persona_rows = list_persona_rows
        self._apply_persona_plan = apply_persona_plan
        self._bad_persona_doc_log = LogThrottle()
```

Add a new `_persona_index` method, right before `_feature_index` (~line 636):

```python
    async def _persona_index(self, view: RepoSyncView) -> None:
        """Personas run BEFORE the feature index (called first in tick() below)
        so a capability doc citing a persona added the same tick still links —
        apply_feature_plan resolves `personas:` slugs against live Persona rows
        at apply time (#2164)."""
        if not view.is_primary or self._read_persona_docs is None:
            return
        try:
            read = await self._read_persona_docs(view.repo)
        except Exception:
            self._observation_degraded = True
            log.warning("pm_sync.persona_read_failed repo=%s — skipping reindex",
                        view.repo, exc_info=True)
            return
        docs: list[tuple[str, PersonaDoc]] = []
        for path, text in read.docs:
            try:
                docs.append((path, parse_persona_doc(text, path=path)))
            except PersonaDocError as e:
                log_event_throttled(log, self._bad_persona_doc_log, f"{view.repo}\0{path}\0{e}",
                                    logging.WARNING, "pm_sync.bad_persona_doc",
                                    exc_info=True, repo=view.repo, path=path)
        rows = await self._list_persona_rows(view.project_id)
        plan = plan_persona_index(rows, docs)
        await self._apply_persona_plan(view.project_id, plan)
```

In `tick()`, at both existing call sites (search `await self._feature_index(view)` — lines ~564 and ~611), insert `await self._persona_index(view)` on the line immediately before:

```python
                await self._persona_index(view)
                await self._feature_index(view)
                await self._governance_check(view)
```

(there are two such sites — apply to both, keeping the surrounding lines otherwise unchanged).

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH=server .venv/bin/python -m pytest server/tests/test_pm_sync_loop.py -v`
Expected: PASS — full file, not just the new tests, to confirm the two `_feature_index` call-site edits didn't regress anything.

- [ ] **Step 5: Commit**

```bash
git add server/automations/pm_sync.py server/tests/test_pm_sync_loop.py
git commit -m "$(cat <<'EOF'
feat(pm-sync): run the persona index before the feature index each tick (#2164)

Co-Authored-By: Claude <model> <noreply@anthropic.com>
EOF
)"
```

---

### Task 8: Orchestrator seams (real git-plumbing read + wiring)

**Files:**
- Modify: `server/sessions/orchestrator.py` (extract shared read helper from `_pm_sync_read_feature_docs` ~line 8800; add `_pm_sync_read_persona_docs`, `_pm_sync_list_persona_rows`, `_pm_sync_apply_persona_plan`; wire into the `PmSyncLoop(...)` construction ~line 1237)
- Modify: `server/tests/test_pm_orchestrator_seams.py` (this is the existing file covering `_pm_sync_read_feature_docs`, via `_make_feature_repo`/`test_read_feature_docs_returns_all_docs_from_pinned_snapshot` ~line 950-990; add persona cases beside it rather than a new file)

**Interfaces:**
- Consumes: Task 5's `list_persona_rows`/`apply_persona_plan` module functions.
- Produces: `SessionOrchestrator._pm_sync_read_docs_at_ref(self, repo: str, subdir: str, ref: str | None = None) -> FeatureDocsRead` (the extracted, subdir-parameterized git-plumbing reader), `_pm_sync_read_feature_docs` becomes a one-line call into it with `"docs/product/features/"`, `_pm_sync_read_persona_docs(self, repo, ref=None) -> FeatureDocsRead` calls it with `"docs/product/personas/"`, `_pm_sync_list_persona_rows`/`_pm_sync_apply_persona_plan` thin wrappers over Task 5 exactly like `_pm_sync_list_feature_rows`/`_pm_sync_apply_feature_plan`.

- [ ] **Step 1: Write the failing test**

In `server/tests/test_pm_orchestrator_seams.py`, add a persona-doc-repo builder mirroring `_make_feature_repo` (~line 950) and two tests, placed after `test_read_feature_docs_returns_all_docs_from_pinned_snapshot`:

```python
def _make_persona_repo(root: str, slugs) -> str:
    """A real git repo with docs/product/personas/<slug>.md committed, plus a
    ``refs/remotes/origin/master`` ref — the persona twin of _make_feature_repo."""
    import os
    import subprocess
    clone = os.path.join(root, "clone")
    os.makedirs(os.path.join(clone, "docs", "product", "personas"))
    subprocess.run(["git", "init", "-q", clone], check=True,
                   capture_output=True, text=True)
    _git("config", "user.email", "t@t", cwd=clone)
    _git("config", "user.name", "t", cwd=clone)
    for slug in slugs:
        p = os.path.join(clone, "docs", "product", "personas", f"{slug}.md")
        with open(p, "w") as fh:
            fh.write(f"---\nslug: {slug}\ntitle: T{slug}\n---\n\nbody {slug}\n")
    _git("add", "-A", cwd=clone)
    _git("commit", "-q", "-m", "docs", cwd=clone)
    head = subprocess.run(["git", "-C", clone, "rev-parse", "HEAD"],
                          check=True, capture_output=True, text=True).stdout.strip()
    _git("update-ref", "refs/remotes/origin/master", head, cwd=clone)
    return clone


@pytest.mark.asyncio
async def test_read_persona_docs_returns_all_docs_from_pinned_snapshot(db_env):
    """Persona twin of test_read_feature_docs_returns_all_docs_from_pinned_snapshot
    — same ATOMIC-OR-RAISE contract, different subdir, via the shared reader."""
    orch = _bare_orch()
    orch._effective_repo_root = _mk_async("/tmp/root")
    with tempfile.TemporaryDirectory() as root:
        clone = _make_persona_repo(root, ["teen", "parent-admin"])
        with patch(_ENSURE_CLONE, return_value=clone):
            read = await orch._pm_sync_read_persona_docs("o/r")
    paths = sorted(p for p, _ in read.docs)
    assert paths == [
        "docs/product/personas/parent-admin.md",
        "docs/product/personas/teen.md",
    ]
    assert all("body" in text for _, text in read.docs)
    assert read.sha


@pytest.mark.asyncio
async def test_read_feature_docs_still_reads_only_the_features_subdir_after_extraction(db_env):
    """Regression guard for the _pm_sync_read_docs_at_ref extraction: a repo
    with BOTH features/ and personas/ docs must have _pm_sync_read_feature_docs
    return only the features/ ones."""
    orch = _bare_orch()
    orch._effective_repo_root = _mk_async("/tmp/root")
    with tempfile.TemporaryDirectory() as root:
        clone = _make_feature_repo(root, ["alpha"])
        import os
        os.makedirs(os.path.join(clone, "docs", "product", "personas"))
        with open(os.path.join(clone, "docs", "product", "personas", "teen.md"), "w") as fh:
            fh.write("---\nslug: teen\ntitle: Teen\n---\nbody\n")
        _git("add", "-A", cwd=clone)
        _git("commit", "-q", "-m", "persona too", cwd=clone)
        head = subprocess.run(["git", "-C", clone, "rev-parse", "HEAD"],
                              check=True, capture_output=True, text=True).stdout.strip()
        _git("update-ref", "refs/remotes/origin/master", head, cwd=clone)
        with patch(_ENSURE_CLONE, return_value=clone):
            read = await orch._pm_sync_read_feature_docs("o/r")
    paths = [p for p, _ in read.docs]
    assert paths == ["docs/product/features/alpha.md"]
```

(`subprocess` is already imported at module scope in this test file for the surrounding fixtures — reuse it, do not re-import inside the test unless the existing file's convention is otherwise; check before writing.)

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=server .venv/bin/python -m pytest server/tests/test_pm_orchestrator_seams.py -k "persona_docs or read_feature_docs_still" -v`
Expected: FAIL — `AttributeError: 'SessionOrchestrator' object has no attribute '_pm_sync_read_persona_docs'`.

- [ ] **Step 3: Implement**

In `server/sessions/orchestrator.py`, replace the body of `_pm_sync_read_feature_docs` (lines 8800-8888) with an extracted helper. The new helper is the EXACT existing body, with the hardcoded `"docs/product/features/"` in the `ls-tree` call replaced by a `subdir` parameter, and the method renamed:

```python
    async def _pm_sync_read_docs_at_ref(
        self, repo: str, subdir: str, ref: str | None = None,
    ) -> "FeatureDocsRead":
        """Read every ``.md`` file under *subdir* at *ref* (default: origin's
        default branch) in the shared clone. Uses git plumbing only — no
        GitHub API cost. Shared by ``_pm_sync_read_feature_docs``
        (``docs/product/features/``) and ``_pm_sync_read_persona_docs``
        (``docs/product/personas/``, #2164) — same ATOMIC-OR-RAISE contract
        for both (see the docstring this replaced on the feature-only method
        for why a partial read must raise, not degrade to a subset)."""
        from server.pm.feature_index import FeatureDocsRead

        async def _read() -> FeatureDocsRead:
            import subprocess
            repo_root = await self._effective_repo_root()
            url = f"https://github.com/{repo}.git"
            clone = await asyncio.to_thread(
                gitrepos.ensure_clone, repo_root, repo, url)
            await asyncio.to_thread(
                subprocess.run,
                ["git", "-C", clone, "fetch", "origin", "--quiet"],
                check=False, capture_output=True, timeout=120,
            )
            if ref is not None:
                resolved_ref = f"refs/remotes/origin/{ref}"
            else:
                head = await asyncio.to_thread(
                    subprocess.run,
                    ["git", "-C", clone, "symbolic-ref", "--quiet",
                     "refs/remotes/origin/HEAD"],
                    capture_output=True, text=True, timeout=30,
                )
                resolved_ref = head.stdout.strip() or "refs/remotes/origin/master"
            rp = await asyncio.to_thread(
                subprocess.run,
                ["git", "-C", clone, "rev-parse", "--verify", f"{resolved_ref}^{{commit}}"],
                capture_output=True, text=True, timeout=30,
            )
            if rp.returncode != 0:
                raise RuntimeError(
                    f"pm_sync.read_docs_at_ref: cannot resolve {resolved_ref} in "
                    f"{repo}: {(rp.stderr or '').strip()[:200]}")
            sha = rp.stdout.strip()
            ls = await asyncio.to_thread(
                subprocess.run,
                ["git", "-C", clone, "ls-tree", "-r", "--name-only", sha, subdir],
                capture_output=True, text=True, timeout=30,
            )
            if ls.returncode != 0:
                raise RuntimeError(
                    f"pm_sync.read_docs_at_ref: ls-tree failed for {repo}: "
                    f"{(ls.stderr or '').strip()[:200]}")
            out: list[tuple[str, str]] = []
            for path in ls.stdout.splitlines():
                if not path.endswith(".md"):
                    continue
                show = await asyncio.to_thread(
                    subprocess.run,
                    ["git", "-C", clone, "show", f"{sha}:{path}"],
                    capture_output=True, text=True, timeout=30,
                )
                if show.returncode != 0:
                    raise RuntimeError(
                        f"pm_sync.read_docs_at_ref: read failed for {path} in "
                        f"{repo}: {(show.stderr or '').strip()[:200]}")
                out.append((path, show.stdout))
            return FeatureDocsRead(sha=sha, docs=out)

        return await _read()

    async def _pm_sync_read_feature_docs(
        self, repo: str, ref: str | None = None
    ) -> "FeatureDocsRead":
        """Read docs/product/features/*.md — see _pm_sync_read_docs_at_ref for
        the ATOMIC-OR-RAISE contract this relies on (the dogfood bug this
        guards against, R8b-4 branch-ref resolution)."""
        return await self._pm_sync_read_docs_at_ref(repo, "docs/product/features/", ref)

    async def _pm_sync_read_persona_docs(
        self, repo: str, ref: str | None = None
    ) -> "FeatureDocsRead":
        """Read docs/product/personas/*.md (#2164) — same contract as
        _pm_sync_read_feature_docs, personas are default-branch-only so *ref*
        is passed through only for interface symmetry with ReadFeatureDocs."""
        return await self._pm_sync_read_docs_at_ref(repo, "docs/product/personas/", ref)
```

Then add, right after `_pm_sync_apply_feature_plan` (~line 9056):

```python
    async def _pm_sync_list_persona_rows(self, project_id: str) -> list:
        """Thin wrapper over server/pm/persona_sync_seams.py (#2164) — see
        _pm_sync_list_feature_rows above for why this stays a bound method."""
        from server.pm.persona_sync_seams import list_persona_rows
        return await list_persona_rows(project_id)

    async def _pm_sync_apply_persona_plan(self, project_id: str, plan) -> None:
        """Thin wrapper over server/pm/persona_sync_seams.py (#2164)."""
        from server.pm.persona_sync_seams import apply_persona_plan
        await apply_persona_plan(project_id, plan)
```

Finally, in the `PmSyncLoop(...)` construction (~line 1237), add three lines after `resolve_evidence_paths=self._pm_sync_resolve_evidence_paths,` (line 1253):

```python
            read_persona_docs=self._pm_sync_read_persona_docs,
            list_persona_rows=self._pm_sync_list_persona_rows,
            apply_persona_plan=self._pm_sync_apply_persona_plan,
```

- [ ] **Step 4: Run tests to verify they pass**

Run the full orchestrator pm-sync test coverage (whichever file(s) `grep` found in Step 1) plus a broader sanity pass:
`PYTHONPATH=server .venv/bin/python -m pytest server/tests/ -k "pm_sync or orchestrator_pm" -v`
Expected: PASS — in particular, every pre-existing `_pm_sync_read_feature_docs`-covering test must pass unchanged (the extraction must be behavior-preserving).

- [ ] **Step 5: Commit**

```bash
git add server/sessions/orchestrator.py server/tests/test_orchestrator_pm_sync_seams.py
git commit -m "$(cat <<'EOF'
feat(pm-sync): wire real persona-doc read/apply seams into the daemon (#2164)

Co-Authored-By: Claude <model> <noreply@anthropic.com>
EOF
)"
```

---

### Task 9: Roadmap health check + personas read API

**Files:**
- Modify: `server/pm/health.py` (`RoadmapHealth` dataclass, `roadmap_health()`)
- Modify: `server/api/pm.py` (`project_health` endpoint; new `GET /projects/{slug}/personas` endpoint)
- Test: `server/tests/test_pm_health.py`, and find/extend the existing `server/tests/test_api_pm*.py` file covering `/projects/{slug}/health` (`grep -rl "projects/{slug}/health\|project_health" server/tests/`)

**Interfaces:**
- Consumes: `Feature.persona_slugs`, `Persona` (Task 2).
- Produces: `RoadmapHealth.unlinked_personas: tuple[tuple[str, str], ...] = ()` (capability slug, undeclared persona slug); `roadmap_health(..., capability_personas: Iterable[tuple[str, tuple[str, ...]]] = (), known_personas: Iterable[str] = ())`; JSON key `unlinkedPersonas: [{"capability": ..., "persona": ...}]` on `GET /pm/projects/{slug}/health`; new `GET /pm/projects/{slug}/personas -> [{"slug", "title", "role", "ageBand", "tier", "summary"}]`.

- [ ] **Step 1: Write the failing tests**

Add to `server/tests/test_pm_health.py`:

```python
def test_health_flags_unlinked_persona_slug():
    h = roadmap_health(
        capabilities=[("chores", None, "live", NEW)], initiatives=[], proposals=[], now=NOW,
        capability_personas=[("chores", ("teen", "ghost"))], known_personas=["teen"])
    assert h.unlinked_personas == (("chores", "ghost"),)


def test_health_unlinked_personas_defaults_to_empty():
    h = roadmap_health(capabilities=[], initiatives=[], proposals=[], now=NOW)
    assert h.unlinked_personas == ()


def test_health_no_false_positive_when_all_personas_resolve():
    h = roadmap_health(
        capabilities=[("chores", None, "live", NEW)], initiatives=[], proposals=[], now=NOW,
        capability_personas=[("chores", ("teen",))], known_personas=["teen"])
    assert h.unlinked_personas == ()
```

For the API test file found by `grep`, add (adapting to that file's existing fixture/client style — read it first):

```python
async def test_health_endpoint_includes_unlinked_personas(...):
    # create a Project, a Feature with persona_slugs=["ghost"], no matching Persona row
    ...
    resp = await client.get(f"/api/pm/projects/{slug}/health")
    assert resp.json()["unlinkedPersonas"] == [{"capability": "chores", "persona": "ghost"}]


async def test_personas_endpoint_lists_indexed_personas(...):
    # create a Project and a Persona row (slug="teen", title="Teen", role="teen")
    ...
    resp = await client.get(f"/api/pm/projects/{slug}/personas")
    assert resp.json() == [{"slug": "teen", "title": "Teen", "role": "teen",
                            "ageBand": None, "tier": None, "summary": None}]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=server .venv/bin/python -m pytest server/tests/test_pm_health.py -k persona -v`
Expected: FAIL — `TypeError: roadmap_health() got an unexpected keyword argument 'capability_personas'`.

- [ ] **Step 3: Implement**

In `server/pm/health.py`, add to `RoadmapHealth` (after `state_behind_spec`, line 33):

```python
    # capabilities citing a persona slug (#2164 `personas:` frontmatter) that
    # no live (non-orphaned) Persona row resolves — declared before the
    # persona doc landed, or after it was removed.
    unlinked_personas: tuple[tuple[str, str], ...] = ()
```

In `roadmap_health()`'s signature, add two kwargs (after `doc_committed`, line 73):

```python
                   capability_personas: Iterable[tuple[str, tuple[str, ...]]] = (),
                   known_personas: Iterable[str] = (),
```

In the function body, after the `dangling, untested, unspecced, behind = ...` line (line 90), add:

```python
    known_p = set(known_personas)
    unlinked_personas = tuple(sorted(
        (slug, pslug) for slug, pslugs in capability_personas for pslug in pslugs
        if pslug not in known_p))
```

Update the `return RoadmapHealth(...)` call to append `unlinked_personas` as the last positional (or keyword — prefer keyword to avoid an easy-to-miscount positional slot):

```python
    return RoadmapHealth(unparented, unlinked, stale, stale_inbox, unindexed, governance,
                         dangling, untested, unspecced, behind, unlinked_personas=unlinked_personas)
```

In `server/api/pm.py`'s `project_health` (~line 325), after building `caps` (line 336), add:

```python
    persona_pairs = [(f.slug, tuple(f.persona_slugs or ())) for f in feats if f.persona_slugs]
    known_personas = [p.slug for p in (await db.execute(select(Persona).where(
        Persona.project_id == p.id, Persona.orphaned.isnot(True)))).scalars().all()]
```

(add `Persona` to the existing `from server.db.models import ...` block at the top of the file). Update the `roadmap_health(...)` call to pass `capability_personas=persona_pairs, known_personas=known_personas,`, and the response dict to add:

```python
            "unlinkedPersonas": [{"capability": c, "persona": p} for c, p in h.unlinked_personas],
```

Add a new endpoint right after `project_health` (before `@router.get("/policy-schema")`):

```python
@router.get("/projects/{slug}/personas")
async def list_project_personas(slug: str, _identity: str = Depends(current_identity),
                                db=Depends(get_db)) -> list[dict]:
    """Indexed personas for one product (issue #2164) — the identity a
    capability's `personas:` frontmatter and a spec's acceptance criteria
    can cite."""
    p = await _load_project(db, slug)
    rows = (await db.execute(select(Persona).where(
        Persona.project_id == p.id, Persona.orphaned.isnot(True)
    ).order_by(Persona.slug))).scalars().all()
    return [{"slug": r.slug, "title": r.title, "role": r.role, "ageBand": r.age_band,
            "tier": r.tier, "summary": r.summary} for r in rows]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH=server .venv/bin/python -m pytest server/tests/test_pm_health.py -v` then the API test file found in Step 1.
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add server/pm/health.py server/api/pm.py server/tests/test_pm_health.py
git commit -m "$(cat <<'EOF'
feat(pm): unlinked-persona roadmap health check + personas list API (#2164)

Co-Authored-By: Claude <model> <noreply@anthropic.com>
EOF
)"
```

---

### Task 10: Web — render the new health finding + persona API client

**Files:**
- Modify: `web/src/api/pm.ts` (`ProjectHealth` interface; new `Persona` interface + `fetchProjectPersonas`)
- Modify: `web/src/features/roadmap/detail/RoadmapHealthCard.tsx`
- Modify/verify: `web/src/features/roadmap/detail/RoadmapHealthCard.test.tsx`

**Interfaces:**
- Consumes: `unlinkedPersonas`, `GET /pm/projects/{slug}/personas` (Task 9).
- Produces: `ProjectHealth.unlinkedPersonas: UnlinkedPersona[]`, `UnlinkedPersona { capability: string; persona: string }`, `Persona { slug: string; title: string | null; role: string | null; ageBand: string | null; tier: string | null; summary: string | null }`, `fetchProjectPersonas(slug: string): Promise<Persona[]>`.

- [ ] **Step 1: Write the failing test**

Read `web/src/features/roadmap/detail/RoadmapHealthCard.test.tsx` first to match its existing mock/assert style (it almost certainly mocks `useProjectHealth` the same way ~47 other web test files mock `@/api/hooks` per this repo's CLAUDE.md note — do not hand-roll a second mocking approach). Add a case:

```tsx
it('renders unlinked personas', () => {
  mockUseProjectHealth.mockReturnValue({
    data: {
      unparentedCapabilities: [], unlinkedInitiatives: [], staleProposed: [], staleInbox: 0,
      unindexedDocs: [], governance: null, danglingEvidence: [], untestedCapabilities: [],
      codeWithoutSpecs: [], stateBehindSpec: [],
      unlinkedPersonas: [{ capability: 'chores', persona: 'ghost' }],
    },
  })
  render(<RoadmapHealthCard productSlug="p1" />)
  expect(screen.getByText(/ghost/)).toBeInTheDocument()
})
```

(Adapt the mock shape/import names exactly to whatever the existing test file already does for `useProjectHealth` — this is illustrative of the assertion, not a literal drop-in.)

- [ ] **Step 2: Run test to verify it fails**

Run: `(cd web && npm run test -- RoadmapHealthCard)`
Expected: FAIL — `unlinkedPersonas` not rendered / TS error on the mock shape (the `ProjectHealth` type doesn't have the field yet).

- [ ] **Step 3: Implement**

In `web/src/api/pm.ts`, add after `DanglingEvidence` (line 387):

```typescript
export interface UnlinkedPersona {
  capability: string
  persona: string
}

export interface Persona {
  slug: string
  title: string | null
  role: string | null
  ageBand: string | null
  tier: string | null
  summary: string | null
}
```

Add `unlinkedPersonas: UnlinkedPersona[]` to `ProjectHealth` (after `stateBehindSpec`, before `generatedAt`). After `fetchProjectHealth` (~line 404), add:

```typescript
export function fetchProjectPersonas(slug: string): Promise<Persona[]> {
  return api.get<Persona[]>(`/pm/projects/${encodeURIComponent(slug)}/personas`)
}
```

In `web/src/features/roadmap/detail/RoadmapHealthCard.tsx`, destructure the new field (line 40-42, add `unlinkedPersonas = []` to the defaulted group), extend `nothingToSay` (line 44-48) to also check `unlinkedPersonas.length === 0`, and add a new `<li>` block mirroring `danglingEvidence`'s structure (after the `stateBehindSpec` block, before the closing `</ul>`, ~line 122):

```tsx
        {unlinkedPersonas.length > 0 && (
          <li>
            {unlinkedPersonas.length} {unlinkedPersonas.length === 1 ? 'capability cites' : 'capabilities cite'} an unindexed persona: {' '}
            {unlinkedPersonas.map((u, i) => (
              <Fragment key={`${u.capability}:${u.persona}`}>
                {i > 0 && ', '}
                <Link to={capPath(u.capability)} className="underline" title={u.capability}>{u.persona}</Link>
              </Fragment>
            ))}
          </li>
        )}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `(cd web && npm run test -- RoadmapHealthCard)` then `(cd web && npm run typecheck)`.
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add web/src/api/pm.ts web/src/features/roadmap/detail/RoadmapHealthCard.tsx web/src/features/roadmap/detail/RoadmapHealthCard.test.tsx
git commit -m "$(cat <<'EOF'
feat(web): render unlinked-persona roadmap hygiene finding (#2164)

Co-Authored-By: Claude <model> <noreply@anthropic.com>
EOF
)"
```

---

### Task 11: Document the convention in `docs/product/README.md`

**Files:**
- Modify: `docs/product/README.md`

**Interfaces:** None (documentation only).

- [ ] **Step 1: Add a "Persona layer" section**

Insert a new section after "## Capability map" and before "## Maintaining the hierarchy" (~line 40):

```markdown
## Personas

A capability's `## Target users` prose may also name persona slugs in its
`personas:` frontmatter list. Each slug indexes to a
`docs/product/personas/<slug>.md` document (issue #2164), parsed the same way
capability docs are: YAML frontmatter (`slug`, `title`, `role`, `age_band`,
`tier` — all but `slug`/`title` optional) plus a free-form body (goals,
constraints, protections). pm-sync indexes personas into a `Persona` table per
product, default-branch-only — there is no map-changes-branch proposal flow
for personas, unlike capabilities. A capability's declared `personas:` slug
that doesn't resolve to a live Persona row surfaces on `GET
/pm/projects/{slug}/health` as `unlinkedPersonas`, rendered on the roadmap
hygiene banner.

ViberPit does not run persona reviews itself — this just gives the persona an
indexed identity a capability doc, a spec's acceptance criteria, or a test can
cite.
```

- [ ] **Step 2: Verify**

Run: `(cd web && true)` — no build step for a docs-only change; visually confirm the rendered markdown reads correctly and the "Capability map" section's existing links are untouched.

- [ ] **Step 3: Commit**

```bash
git add docs/product/README.md
git commit -m "$(cat <<'EOF'
docs(pm): document the persona-layer convention (#2164)

Co-Authored-By: Claude <model> <noreply@anthropic.com>
EOF
)"
```

---

## After all tasks: whole-branch verification

Before opening the PR, run the full gate per this repo's CLAUDE.md:

```bash
ruff check src/ server/
mypy server/ --ignore-missing-imports
env -i HOME="$HOME" PATH="$PATH" PYTHONPATH=server .venv/bin/python -m pytest tests/ server/tests/ -n auto --dist loadfile --timeout=300
(cd web && npm run typecheck && npm run lint && npm run test && npm run build)
```

Then open ONE PR covering issue #2164, body including `Fixes #2164`, noting the two explicitly deferred follow-ups (capability-tree persona filter chip UI; SDLC spec-prompt persona injection) as a suggested follow-up issue rather than silently dropped scope. Mark ready for review (never draft) and arm auto-merge (`gh pr merge <#> --squash --auto`) per this repo's completion workflow.

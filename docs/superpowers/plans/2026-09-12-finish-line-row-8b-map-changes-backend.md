# Finish-line row 8b — Map-changes PRs, backend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the server a deterministic, session-free way to edit a product's capability documents on a ccswitch-owned branch and PR (`pm/map-changes-<slug>`), index that branch head so proposed capabilities show on the map, track the PR's lifecycle, and expose it all through one API — so 8c can put inline capability edits and the PR affordance in the web UI.

**Architecture:** Three pure modules and one seam. `server/pm/map_changes_ops.py` turns an edit op plus the current `FeatureDoc` into the next Markdown text (pure; reuses `parse_feature_doc`/`serialize_feature_doc`). `server/pm/map_changes_gh.py` is the GitHub write seam: branch from the default-branch head (REST `git/refs`), commit through GraphQL `createCommitOnBranch` (atomic, `expectedHeadOid` gives conflict detection), open/close/update the PR and read its state (REST `pulls`), all through the existing `gitrepos._run_gh` runner with an injectable `run` so tests record argv and answer canned JSON. `server/pm/map_changes.py` orchestrates one edit: load or open the pointer row (`feature_map_changes`), fetch the doc at the branch head (REST `contents`), apply the op, commit, request a sync. The sync loop gains a second read at the branch head, merged over the default-branch docs by slug, and stamps `pr_number`/`pr_url` on proposed rows and `commit_sha` on merged ones; a per-pass PR read moves the pointer row through `open → merged | abandoned` and records behind/conflicting. The map payload carries the pointer per product.

**Tech Stack:** FastAPI + SQLAlchemy async, `gh` CLI through `src/ccswitch/gitrepos._run_gh` (GitHub App installation token with PAT fallback), GraphQL `createCommitOnBranch`, pytest with seam fakes.

**Spec:** `docs/superpowers/specs/2026-09-07-roadmap-drilldown-design.md` §8 (rows "Add / rename capability, reparent, edit summary → Markdown on the product's map-changes branch" and "Capability links → doc frontmatter, same PR"), §9 (Map-changes PRs, every bullet), §10 (`feature_map_changes`, `POST /api/pm/projects/{slug}/map-changes` with `{op: create|rename|reparent|summary|links, …}`, `DELETE` abandons, "the feature index runs over the branch head as well as the default branch"), §13 Phase 5, §14 ("map-changes diff and frontmatter round-trip"; "map-changes against fake git and GitHub layers; conflict and abandon paths"). Row 8 of `docs/superpowers/specs/2026-09-08-finish-line-persona-modes-design.md` (#1472). Web work (capability inline edits, the product-header PR card, badges) is plan 8c.

## Design rulings (recorded in the ledger as R8b-1..12)

- **R8b-1 No agent session for a doc edit.** Every existing map write is an LLM session that runs git itself (`pm_onboard_spawn_map`); a rename must be synchronous and deterministic, so the server writes directly.
- **R8b-2 Commit through GraphQL `createCommitOnBranch`**, not a local clone: the shared clone is read SHA-pinned by the sync loop and used as cwd by sessions — mutating it is a race. `expectedHeadOid` makes a moved head an explicit error (one re-read-and-retry, then 409). Branch/PR/state calls are REST through `gh api`. Content reads for an edit are REST `contents` at the branch (or default-branch) ref for the same reason.
- **R8b-3 Token: App installation token (`gitrepos.app_token_for_repo`) first; on a 403 whose body says `Resource not accessible by integration`, retry with `CCSWITCH_GH_PROJECT_TOKEN` (classic PAT, `repo` scope — already required for Projects v2); else ambient `gh`.** The App's write permissions cannot be verified from a worktree (`/etc/viberpit/viberpit.env` is root-only); the fallback makes the feature work either way and the PR body tells the operator that granting the App `contents:write` + `pull_requests:write` makes commits bot-authored.
- **R8b-4 Branch docs override default docs by slug** in the index; a proposed-only capability gets `pr_number`/`pr_url` and `commit_sha = NULL`; a default-branch capability gets `commit_sha` = the SHA the docs were read at (the column is documented as "stamped by sync" but was never written — `CapabilityDetail`'s doc link has therefore never rendered; this fixes that) and `pr_number = NULL`. `plan_feature_index` stays pure: it receives the merged doc list plus the set of proposed slugs and the read SHA.
- **R8b-5 One open pointer per project**, enforced by lookup (`status == "open"`), not a partial unique index (SQLite has no partial unique the reconcile path can add).
- **R8b-6 PR lifecycle is read directly** (`GET /repos/{r}/pulls/{n}`) once per open pointer per incremental pass, not through the session PR poller (it watches session branches). `mergeable_state` supplies `behind` (`"behind"`) and `conflicting` (`"dirty"`).
- **R8b-7 Abandon = close the PR + delete the branch + mark the row `abandoned`**; proposed-only Feature rows (pr_number set, commit_sha NULL) that nothing references (no `work_items.feature_id`, no `initiative_features`) are deleted, referenced ones are orphaned. The next index pass would otherwise leave never-merged rows as orphans forever.
- **R8b-8 Rebase = `PUT /repos/{r}/pulls/{n}/update-branch`** (the pr-doctor Mechanical idiom), offered only while `behind`; the UI never force-pushes.
- **R8b-9 The AI map-diff session targets the open map-changes branch** when one exists (spec §9): `pm_onboard_spawn_map(scope="diff")` passes the pointer's branch to the prompt builder; otherwise the historical `pm/feature-map-<slug>`.
- **R8b-10 Slug and structure validation is server-side and pure**: slug `^[a-z0-9][a-z0-9-]{0,63}$`, unique among the product's non-orphaned features and the branch's docs; a parent must exist (indexed or on the branch) and must not create a cycle; link targets are `(project slug, feature slug)` pairs that exist in the index; `state` on create is `proposed`.
- **R8b-11 `summary` edits replace the doc's first non-heading paragraph** (what `_summary` reads), inserting one after the leading H1 when the body has none. The body's remaining prose is untouched.
- **R8b-12 First machine write of an archaeology-dialect doc canonicalises its frontmatter** (`serialize_feature_doc` writes `slug/title/state/parent/links` only). The PR body generated for the map-changes PR says so.

## Global Constraints

- New columns nullable; `feature_map_changes` is a NEW TABLE → migration `0040_feature_map_changes` chained off `0039_child_sessions`, an `_FK_LINKS` entry in `server/tests/test_postgres_compat.py` (`{"project_id": "projects"}`), no `RECONCILE_EXEMPT` entry (all columns nullable or defaulted). Postgres suite runs in CI only; SQLite locally.
- DB clock: `server/db/clock.py:db_utc_now()` / the models' `_utcnow` default — never `func.now()`.
- Every mutation records an audit row (`record_audit`) with the acting identity.
- GitHub calls go through `gitrepos._run_gh(args, token, input=…)`; never `subprocess` directly; never a local `git commit`/`push`.
- The four-step server gate before every commit: `/home/tony/GitHub/viberpit/.venv/bin/ruff check src/ server/`, `/home/tony/GitHub/viberpit/.venv/bin/python -m mypy server/ --ignore-missing-imports`, and the named test files via `env -u CCSWITCH_SESSIOND_ENDPOINT -u CCSWITCH_SESSIOND_CLIENT_CERT -u CCSWITCH_SESSIOND_CLIENT_KEY -u CCSWITCH_SESSIOND_CA -u CCSWITCH_DAEMON_ROLE -u CCSWITCH_TAILNET_ALLOWLIST -u VIBERPIT_SESSIOND_ENDPOINT -u VIBERPIT_SESSIOND_CLIENT_CERT -u VIBERPIT_SESSIOND_CLIENT_KEY -u VIBERPIT_SESSIOND_CA -u VIBERPIT_DAEMON_ROLE -u VIBERPIT_TAILNET_ALLOWLIST CCSWITCH_DEV=1 /home/tony/GitHub/viberpit/.venv/bin/python -m pytest <files> -q`. The full `pytest server/tests/ -n auto` and `pytest tests/` run once on the final head, by the controller.
- Web changes in this plan are type-only (`ProductNode.mapChanges`); the web gate (`typecheck`, `lint`, full `vitest`, `build`) runs on the task that touches `web/`.
- Commits: one per task, `<type>(<scope>): 8b task N — <what>` / blank / paragraph / blank / `Refs #1472` / blank / `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`; via `git commit -F /tmp/row8b-tN.msg`. Never push, never open a PR, never rebase.
- Run from the worktree root with absolute paths or `(cd web && …)`; a bare `cd` breaks the repo's Bash hook.

## File structure

| File | Responsibility |
|---|---|
| `server/db/models.py` | `FeatureMapChange` model + `FEATURE_MAP_CHANGE_STATUSES` |
| `server/db/migrations/versions/0040_feature_map_changes.py` | the reviewable record of the table |
| `server/tests/test_postgres_compat.py` | `_FK_LINKS["feature_map_changes"]` |
| `server/pm/map_changes_ops.py` | pure: `MapOp` dataclasses, `validate_op`, `apply_op(op, current) -> str` |
| `server/pm/map_changes_gh.py` | `GhWrite` seam: `ensure_branch`, `read_file`, `commit_file`, `open_pr`, `read_pr`, `close_pr`, `delete_branch`, `update_branch`; token fallback; `GhWriteError(step, detail)` |
| `server/pm/map_changes.py` | `MapChangesService`: `edit(db, project, op, identity)`, `abandon(...)`, `rebase(...)`, `refresh(...)` (PR lifecycle) |
| `server/pm/feature_index.py` | `FeatureUpsert.pr_number/pr_url/commit_sha`; `plan_feature_index(existing, docs, *, read_sha, proposed)` |
| `server/sessions/orchestrator.py` | `_pm_sync_read_feature_docs(repo, ref=None) -> FeatureDocsRead`; `_pm_sync_apply_feature_plan` writes the three columns; `pm_map_changes` service accessor with `_gh_run` seam; `pm_onboard_spawn_map` branch retarget |
| `server/automations/pm_sync.py` | `_feature_index` merges branch docs; `_refresh_map_changes` per pass (new seams `read_map_changes`, `refresh_map_changes`) |
| `server/pm/classification.py` | `build_map_synthesis_prompt(..., branch=None)` |
| `server/api/pm.py` | `POST/DELETE /projects/{slug}/map-changes`, `POST …/map-changes/rebase` |
| `server/api/pm_tree.py`, `server/pm/tree.py` | `mapChanges` on the product node |
| `web/src/api/roadmap.ts` | `ProductNode.mapChanges` type (8c consumes it) |

Task order: 1 (model) → 2 (ops, pure) → 3 (GitHub seam) → 4 (service + API) → 5 (index over the branch head + `commit_sha`) → 6 (PR lifecycle, abandon cleanup, map payload, AI-diff retarget). 2 and 3 are independent of each other and of 1; 4 needs 1–3; 5 needs 1; 6 needs 4–5.

---

### Task 1: The pointer table — `feature_map_changes`

**Files:**
- Modify: `server/db/models.py` (after `FeatureLink`, `:1541`)
- Create: `server/db/migrations/versions/0040_feature_map_changes.py`
- Modify: `server/tests/test_postgres_compat.py:240-267` (`_FK_LINKS`)
- Test: `server/tests/test_feature_map_changes_model.py`

**Interfaces:**
- Produces: `FeatureMapChange` (table `feature_map_changes`): `id`, `project_id` (FK projects, indexed), `repo` (str), `branch` (str), `pr_number` (int|None), `pr_url` (str|None), `status` (`FEATURE_MAP_CHANGE_STATUSES = ("open", "merged", "abandoned")`, default `"open"`), `head_sha` (str|None), `behind` (bool|None), `conflicting` (bool|None), `opened_at` (datetime, default `_utcnow`), `merged_at` (datetime|None), `closed_at` (datetime|None), `last_error` (str|None), `created_at`, `updated_at`.

- [ ] **Step 1: Write the failing test**

`server/tests/test_feature_map_changes_model.py`:

```python
"""feature_map_changes: the per-product pointer to the ccswitch-owned map-changes PR (roadmap spec §9)."""
from __future__ import annotations

import asyncio
import tempfile


def test_pointer_row_round_trips_and_defaults_open():
    from sqlalchemy import select
    from server.db import db_session, init_db
    from server.db.models import FEATURE_MAP_CHANGE_STATUSES, FeatureMapChange, Project

    assert FEATURE_MAP_CHANGE_STATUSES == ("open", "merged", "abandoned")

    async def _go(db_url: str) -> tuple[str, str | None, int | None]:
        await init_db(db_url)
        async with db_session() as db:
            p = Project(slug="kindash", name="KinDash")
            db.add(p)
            await db.flush()
            db.add(FeatureMapChange(project_id=p.id, repo="cairnea/KinDash",
                                    branch="pm/map-changes-kindash"))
            await db.commit()
        async with db_session() as db:
            row = (await db.execute(select(FeatureMapChange))).scalar_one()
            return row.status, row.pr_url, row.pr_number

    with tempfile.TemporaryDirectory() as tmp:
        status, pr_url, pr_number = asyncio.run(_go(f"sqlite+aiosqlite:///{tmp}/t.db"))
    assert (status, pr_url, pr_number) == ("open", None, None)
```

- [ ] **Step 2: Run it to verify it fails**

Run (gate env): `… pytest server/tests/test_feature_map_changes_model.py -q` → Expected: FAIL, `ImportError: cannot import name 'FeatureMapChange'`.

- [ ] **Step 3: Model, migration, Postgres list**

`server/db/models.py`, after `FeatureLink`:

```python
FEATURE_MAP_CHANGE_STATUSES: tuple[str, ...] = ("open", "merged", "abandoned")


class FeatureMapChange(Base):
    """The pointer to a product's ccswitch-owned map-changes branch/PR (roadmap
    spec §9): ``pm/map-changes-<slug>`` in the primary repo. Branch content is
    truth; this row only says where it is and what GitHub last said about it.
    At most one ``open`` row per project — enforced by the service's lookup, not
    a partial index (SQLite's reconcile path cannot add one)."""

    __tablename__ = "feature_map_changes"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_new_uuid)
    project_id: Mapped[str] = mapped_column(
        String, ForeignKey("projects.id"), nullable=True, index=True)
    repo: Mapped[str | None] = mapped_column(String, nullable=True)
    branch: Mapped[str | None] = mapped_column(String, nullable=True)
    pr_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    pr_url: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str | None] = mapped_column(String, nullable=True, default="open")
    head_sha: Mapped[str | None] = mapped_column(String, nullable=True)
    # GitHub's mergeable_state, folded to the two facts the UI acts on (§9:
    # "offers Rebase when it is behind"; "conflicts … surface as a Banner").
    behind: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    conflicting: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    opened_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow)
    merged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow)
```

`server/db/migrations/versions/0040_feature_map_changes.py`:

```python
"""feature_map_changes: pointer to a product's ccswitch-owned map-changes PR

Revision ID: 0040_feature_map_changes
Revises: 0039_child_sessions

Reviewable record of the schema change (roadmap drill-down spec §9/§10).
Nothing runs `alembic upgrade` at deploy time: init_db's create_all creates the
table on both dialects; every column is nullable or defaulted.
"""
from alembic import op
import sqlalchemy as sa

revision = "0040_feature_map_changes"
down_revision = "0039_child_sessions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "feature_map_changes",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("project_id", sa.String(), sa.ForeignKey("projects.id"), nullable=True, index=True),
        sa.Column("repo", sa.String(), nullable=True),
        sa.Column("branch", sa.String(), nullable=True),
        sa.Column("pr_number", sa.Integer(), nullable=True),
        sa.Column("pr_url", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=True),
        sa.Column("head_sha", sa.String(), nullable=True),
        sa.Column("behind", sa.Boolean(), nullable=True),
        sa.Column("conflicting", sa.Boolean(), nullable=True),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("merged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("feature_map_changes")
```

`server/tests/test_postgres_compat.py` `_FK_LINKS`, after the `work_item_deps` line:

```python
    # roadmap spec §9: the map-changes PR pointer hangs off a product.
    "feature_map_changes": {"project_id": "projects"},
```

- [ ] **Step 4: Run the tests to verify they pass**

Run (gate env): `… pytest server/tests/test_feature_map_changes_model.py server/tests/test_schema_reconcile_coverage.py server/tests/test_postgres_compat.py -q` → Expected: passed (the Postgres file's live tests skip without `CCSWITCH_TEST_PG_URL`; its pure list checks run). `ruff` and `mypy` clean.

- [ ] **Step 5: Commit**

```bash
git add server/db/models.py server/db/migrations/versions/0040_feature_map_changes.py server/tests/test_postgres_compat.py server/tests/test_feature_map_changes_model.py
git commit -F /tmp/row8b-t1.msg   # feat(pm): 8b task 1 — feature_map_changes pointer table
```

---

### Task 2: Pure doc edits — `map_changes_ops`

**Files:**
- Create: `server/pm/map_changes_ops.py`
- Test: `server/tests/test_pm_map_changes_ops.py`

**Interfaces:**
- Consumes: `parse_feature_doc`, `serialize_feature_doc`, `FeatureDoc`, `FeatureLinkRef`, `FeatureDocError` (`server/pm/featuredoc.py`); `feature_doc_path` (`server/pm/classification.py:47`); `FEATURE_LINK_KINDS` (`server/db/models.py:1481`).
- Produces:

```python
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
MAP_OPS = ("create", "rename", "reparent", "summary", "links")

@dataclass(frozen=True)
class MapOp:
    op: str                      # one of MAP_OPS
    slug: str
    title: str | None = None     # create, rename
    parent_slug: str | None = None   # create (optional), reparent (None = root)
    summary: str | None = None   # create (optional), summary
    links: tuple[FeatureLinkRef, ...] = ()   # links (replaces all), create (optional)

@dataclass(frozen=True)
class MapContext:
    """What the validator needs to know about the world: the product's known
    capability slugs → parent slug (indexed rows plus branch docs), and the
    (project_slug, feature_slug) pairs that exist anywhere (link targets)."""
    parents: Mapping[str, str | None]
    link_targets: frozenset[tuple[str, str]]

class MapOpError(ValueError):
    def __init__(self, code: str, message: str): ...   # code ∈ bad_op, bad_slug, slug_taken, not_found, parent_not_found, parent_cycle, bad_link, bad_title, bad_summary

def validate_op(op: MapOp, ctx: MapContext) -> None      # raises MapOpError
def apply_op(op: MapOp, current: str | None) -> tuple[str, str]   # (path, new_text); current = the doc's text on the branch head, None for create
def commit_message(op: MapOp, product_name: str) -> str
```

- [ ] **Step 1: Write the failing tests**

`server/tests/test_pm_map_changes_ops.py`:

```python
"""Pure map-changes edits (roadmap spec §8 rows that write Markdown, §14
"frontmatter round-trip")."""
from __future__ import annotations

import pytest

from server.pm.featuredoc import FeatureLinkRef, parse_feature_doc
from server.pm.map_changes_ops import (
    MapContext, MapOp, MapOpError, apply_op, commit_message, validate_op,
)

CTX = MapContext(
    parents={"sessions": None, "dictation": "sessions", "billing": None},
    link_targets=frozenset({("kindash", "sessions"), ("kindash", "dictation"),
                            ("kindash", "billing"), ("web", "auth")}),
)

DOC = """---
slug: dictation
title: Dictation
state: live
parent: sessions
---
# Dictation

Speak instead of typing.

More prose that must survive.
"""


def test_create_writes_a_canonical_doc_at_the_slug_path():
    op = MapOp(op="create", slug="voice-notes", title="Voice notes",
               parent_slug="sessions", summary="Short spoken memos.")
    validate_op(op, CTX)
    path, text = apply_op(op, None)
    assert path == "docs/product/features/voice-notes.md"
    doc = parse_feature_doc(text)
    assert (doc.slug, doc.title, doc.state, doc.parent) == ("voice-notes", "Voice notes", "proposed", "sessions")
    assert doc.body.strip() == "# Voice notes\n\nShort spoken memos."
    assert commit_message(op, "KinDash") == "map(kindash): add capability voice-notes"


def test_create_rejects_bad_slug_taken_slug_and_unknown_parent():
    with pytest.raises(MapOpError) as e:
        validate_op(MapOp(op="create", slug="Voice Notes", title="x"), CTX)
    assert e.value.code == "bad_slug"
    with pytest.raises(MapOpError) as e:
        validate_op(MapOp(op="create", slug="dictation", title="x"), CTX)
    assert e.value.code == "slug_taken"
    with pytest.raises(MapOpError) as e:
        validate_op(MapOp(op="create", slug="new", title="x", parent_slug="nope"), CTX)
    assert e.value.code == "parent_not_found"
    with pytest.raises(MapOpError) as e:
        validate_op(MapOp(op="create", slug="new", title="  "), CTX)
    assert e.value.code == "bad_title"


def test_rename_changes_title_only_and_keeps_the_body():
    op = MapOp(op="rename", slug="dictation", title="Dictation & voice")
    validate_op(op, CTX)
    path, text = apply_op(op, DOC)
    assert path == "docs/product/features/dictation.md"
    doc = parse_feature_doc(text)
    assert doc.title == "Dictation & voice" and doc.parent == "sessions" and doc.state == "live"
    assert "More prose that must survive." in doc.body


def test_reparent_to_root_and_to_a_sibling_but_never_into_a_cycle():
    validate_op(MapOp(op="reparent", slug="dictation", parent_slug=None), CTX)
    _, text = apply_op(MapOp(op="reparent", slug="dictation", parent_slug=None), DOC)
    assert parse_feature_doc(text).parent is None
    validate_op(MapOp(op="reparent", slug="dictation", parent_slug="billing"), CTX)
    with pytest.raises(MapOpError) as e:          # sessions under its own child
        validate_op(MapOp(op="reparent", slug="sessions", parent_slug="dictation"), CTX)
    assert e.value.code == "parent_cycle"
    with pytest.raises(MapOpError) as e:          # self
        validate_op(MapOp(op="reparent", slug="dictation", parent_slug="dictation"), CTX)
    assert e.value.code == "parent_cycle"
    with pytest.raises(MapOpError) as e:
        validate_op(MapOp(op="reparent", slug="ghost", parent_slug=None), CTX)
    assert e.value.code == "not_found"


def test_summary_replaces_the_first_paragraph_and_inserts_one_when_missing():
    op = MapOp(op="summary", slug="dictation", summary="Talk, and it types.")
    validate_op(op, CTX)
    _, text = apply_op(op, DOC)
    body = parse_feature_doc(text).body
    assert body.strip().startswith("# Dictation\n\nTalk, and it types.")
    assert "Speak instead of typing." not in body
    assert "More prose that must survive." in body
    bare = "---\nslug: dictation\ntitle: Dictation\nstate: live\n---\n# Dictation\n"
    _, text = apply_op(op, bare)
    assert parse_feature_doc(text).body.strip() == "# Dictation\n\nTalk, and it types."
    with pytest.raises(MapOpError) as e:
        validate_op(MapOp(op="summary", slug="dictation", summary=" "), CTX)
    assert e.value.code == "bad_summary"


def test_links_replace_all_and_targets_must_exist():
    op = MapOp(op="links", slug="dictation", links=(
        FeatureLinkRef(kind="powered_by", project="web", feature="auth"),))
    validate_op(op, CTX)
    _, text = apply_op(op, DOC)
    assert parse_feature_doc(text).links == (FeatureLinkRef("powered_by", "web", "auth"),)
    _, text = apply_op(MapOp(op="links", slug="dictation", links=()), text)
    assert parse_feature_doc(text).links == ()
    with pytest.raises(MapOpError) as e:
        validate_op(MapOp(op="links", slug="dictation", links=(
            FeatureLinkRef(kind="powered_by", project="web", feature="nope"),)), CTX)
    assert e.value.code == "bad_link"
    with pytest.raises(MapOpError) as e:
        validate_op(MapOp(op="links", slug="dictation", links=(
            FeatureLinkRef(kind="depends", project="web", feature="auth"),)), CTX)
    assert e.value.code == "bad_link"


def test_archaeology_dialect_is_canonicalised_on_first_write():
    legacy = "---\ncapability: dictation\nstatus: live\n---\n# Dictation\n\nSpeak.\n"
    _, text = apply_op(MapOp(op="rename", slug="dictation", title="Dictation v2"), legacy)
    assert text.startswith("---\nslug: dictation\ntitle: Dictation v2\nstate: live\n")


def test_unknown_op_and_missing_doc_for_an_edit():
    with pytest.raises(MapOpError) as e:
        validate_op(MapOp(op="delete", slug="dictation"), CTX)
    assert e.value.code == "bad_op"
    with pytest.raises(MapOpError) as e:
        apply_op(MapOp(op="rename", slug="dictation", title="x"), None)
    assert e.value.code == "not_found"
```

- [ ] **Step 2: Run them to verify they fail**

Run: `… pytest server/tests/test_pm_map_changes_ops.py -q` → Expected: FAIL, module not found.

- [ ] **Step 3: Write the module**

`server/pm/map_changes_ops.py`:

```python
"""Pure map-changes edits (roadmap drill-down spec §8, §9).

One capability edit → one new Markdown text. The doc's frontmatter is the truth
for slug/title/state/parent/links; its body is the human prose. Reads through
``parse_feature_doc`` (which tolerates the archaeology dialect) and writes
through ``serialize_feature_doc`` (canonical), so the first machine edit of an
old doc canonicalises it — the PR body says so (R8b-12). No I/O here: the
service layer fetches the current text and commits the result."""
from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

from server.db.models import FEATURE_LINK_KINDS

from .classification import feature_doc_path
from .featuredoc import FeatureDoc, FeatureDocError, FeatureLinkRef, parse_feature_doc, serialize_feature_doc

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
MAP_OPS: tuple[str, ...] = ("create", "rename", "reparent", "summary", "links")


class MapOpError(ValueError):
    """A rejected edit. ``code`` is the API's ``detail.error``."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class MapOp:
    op: str
    slug: str
    title: str | None = None
    parent_slug: str | None = None
    summary: str | None = None
    links: tuple[FeatureLinkRef, ...] = ()


@dataclass(frozen=True)
class MapContext:
    """The product's capability slugs → parent slug (indexed rows merged with
    the branch's docs) and every (project slug, feature slug) a link may target."""
    parents: Mapping[str, str | None]
    link_targets: frozenset[tuple[str, str]]


def _is_ancestor(ctx: MapContext, candidate: str, of: str) -> bool:
    """True when ``candidate`` is ``of`` itself or one of its descendants —
    i.e. making it ``of``'s parent would close a cycle."""
    seen: set[str] = set()
    cur: str | None = candidate
    while cur is not None and cur not in seen:
        if cur == of:
            return True
        seen.add(cur)
        cur = ctx.parents.get(cur)
    return False


def validate_op(op: MapOp, ctx: MapContext) -> None:
    if op.op not in MAP_OPS:
        raise MapOpError("bad_op", f"op must be one of {MAP_OPS}")
    if not SLUG_RE.match(op.slug or ""):
        raise MapOpError("bad_slug", "slug must be lowercase letters, digits and dashes")
    exists = op.slug in ctx.parents
    if op.op == "create":
        if exists:
            raise MapOpError("slug_taken", f"capability '{op.slug}' already exists")
    elif not exists:
        raise MapOpError("not_found", f"capability '{op.slug}' does not exist")
    if op.op in ("create", "rename"):
        if not (op.title or "").strip():
            raise MapOpError("bad_title", "title must not be blank")
    if op.op in ("create", "reparent") and op.parent_slug is not None:
        if op.parent_slug not in ctx.parents:
            raise MapOpError("parent_not_found", f"parent '{op.parent_slug}' does not exist")
        if op.op == "reparent" and _is_ancestor(ctx, op.parent_slug, op.slug):
            raise MapOpError("parent_cycle", "a capability cannot sit under itself or its descendants")
    if op.op == "summary" and not (op.summary or "").strip():
        raise MapOpError("bad_summary", "summary must not be blank")
    if op.op in ("links", "create"):
        for link in op.links:
            if link.kind not in FEATURE_LINK_KINDS:
                raise MapOpError("bad_link", f"link kind must be one of {FEATURE_LINK_KINDS}")
            if (link.project, link.feature) not in ctx.link_targets:
                raise MapOpError("bad_link", f"unknown link target {link.project}/{link.feature}")


def _replace_summary(body: str, summary: str) -> str:
    """Replace the first non-heading paragraph (what the index reads as the
    summary); insert one after the leading H1 when there is none."""
    lines = body.splitlines()
    start = None
    for i, line in enumerate(lines):
        if line.strip() and not line.lstrip().startswith("#"):
            start = i
            break
    if start is None:
        head = lines[:1] if lines and lines[0].startswith("# ") else []
        rest = lines[len(head):]
        return "\n".join([*head, "", summary.strip(), *([""] if rest and rest[0].strip() else []), *rest]).rstrip("\n") + "\n"
    end = start
    while end < len(lines) and lines[end].strip():
        end += 1
    return "\n".join([*lines[:start], summary.strip(), *lines[end:]]).rstrip("\n") + "\n"


def apply_op(op: MapOp, current: str | None) -> tuple[str, str]:
    """The doc path and its new text. ``current`` is the text at the branch
    head (``None`` only for ``create``). Validation is ``validate_op``'s job;
    this only refuses the shapes it cannot compute."""
    path = feature_doc_path(op.slug)
    if op.op == "create":
        summary = (op.summary or "").strip()
        body = f"# {op.title.strip()}\n" + (f"\n{summary}\n" if summary else "")  # type: ignore[union-attr]
        doc = FeatureDoc(slug=op.slug, title=op.title.strip(), state="proposed",  # type: ignore[union-attr]
                         links=tuple(op.links), body=body, parent=op.parent_slug)
        return path, serialize_feature_doc(doc)
    if current is None:
        raise MapOpError("not_found", f"no doc for '{op.slug}' on the branch")
    try:
        doc = parse_feature_doc(current)
    except FeatureDocError as e:
        raise MapOpError("bad_doc", f"the existing doc cannot be parsed: {e}") from e
    if op.op == "rename":
        doc = FeatureDoc(slug=doc.slug, title=op.title.strip(), state=doc.state,  # type: ignore[union-attr]
                         links=doc.links, body=doc.body, parent=doc.parent)
    elif op.op == "reparent":
        doc = FeatureDoc(slug=doc.slug, title=doc.title, state=doc.state,
                         links=doc.links, body=doc.body, parent=op.parent_slug)
    elif op.op == "summary":
        doc = FeatureDoc(slug=doc.slug, title=doc.title, state=doc.state, links=doc.links,
                         body=_replace_summary(doc.body, op.summary or ""), parent=doc.parent)
    elif op.op == "links":
        doc = FeatureDoc(slug=doc.slug, title=doc.title, state=doc.state,
                         links=tuple(op.links), body=doc.body, parent=doc.parent)
    else:
        raise MapOpError("bad_op", f"op must be one of {MAP_OPS}")
    return path, serialize_feature_doc(doc)


def commit_message(op: MapOp, product_name: str) -> str:
    verb = {"create": "add capability", "rename": "rename capability",
            "reparent": "move capability", "summary": "update summary of",
            "links": "update links of"}[op.op]
    return f"map({product_name.lower().replace(' ', '-')}): {verb} {op.slug}"
```

Read `serialize_feature_doc` (`featuredoc.py:125-140`) once to confirm the body is appended verbatim after the closing `---` and adjust the expected strings in the tests only if the serializer adds/strips a newline differently than the tests assume — the assertions use `.strip()` where the exact whitespace does not matter.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `… pytest server/tests/test_pm_map_changes_ops.py server/tests/test_pm_featuredoc.py -q` → Expected: all passed. `ruff`, `mypy` clean.

- [ ] **Step 5: Commit**

```bash
git add server/pm/map_changes_ops.py server/tests/test_pm_map_changes_ops.py
git commit -F /tmp/row8b-t2.msg   # feat(pm): 8b task 2 — pure map-changes doc edits (create/rename/reparent/summary/links)
```

---

### Task 3: The GitHub write seam — `map_changes_gh`

**Files:**
- Create: `server/pm/map_changes_gh.py`
- Test: `server/tests/test_pm_map_changes_gh.py`

**Interfaces:**
- Consumes: `gitrepos._run_gh(args, token=None, *, input=None, timeout=None) -> CompletedProcess` (`src/ccswitch/gitrepos.py:392`), `gitrepos.app_token_for_repo(name_with_owner) -> str | None` (`:189`); `os.environ["CCSWITCH_GH_PROJECT_TOKEN"]`.
- Produces:

```python
class GhWriteError(RuntimeError):
    step: str        # 'auth' | 'head' | 'branch' | 'read' | 'commit' | 'pr' | 'pr_read' | 'close' | 'delete_branch' | 'update_branch'
    detail: str
    conflict: bool   # True when createCommitOnBranch reported the head moved

@dataclass(frozen=True)
class PrState:
    number: int; url: str; state: str  # 'open' | 'closed'
    merged: bool; merged_at: str | None; head_sha: str
    mergeable_state: str | None        # GitHub's: 'clean' | 'behind' | 'dirty' | 'blocked' | 'unknown' | …
    @property behind -> bool           # mergeable_state == 'behind'
    @property conflicting -> bool      # mergeable_state == 'dirty'

class GhWrite:
    def __init__(self, repo: str, *, run=gitrepos._run_gh, app_token=gitrepos.app_token_for_repo,
                 env=os.environ, log=log): ...
    def default_branch_head(self, default_branch: str) -> str            # sha
    def ensure_branch(self, branch: str, from_sha: str) -> str           # returns branch head sha (existing or created)
    def read_file(self, path: str, ref: str) -> tuple[str | None, str | None]   # (text, blob_sha) or (None, None) when 404
    def commit_file(self, branch: str, expected_head: str, path: str, text: str, message: str) -> str   # new head sha; GhWriteError(conflict=True) when expectedHeadOid mismatched
    def open_pr(self, branch: str, base: str, title: str, body: str) -> PrState
    def read_pr(self, number: int) -> PrState
    def close_pr(self, number: int) -> None
    def delete_branch(self, branch: str) -> None
    def update_branch(self, number: int) -> None
```

Each method calls `self._gh(args, step, input=None)` which: picks the token (`self._token`), runs, and on a non-zero return whose stderr/stdout contains `Resource not accessible by integration` while an App token was used, retries once with the PAT (`env.get("CCSWITCH_GH_PROJECT_TOKEN")`) and remembers that choice for the instance; any other failure raises `GhWriteError(step, detail=stderr[:300])`. `commit_file` sends the GraphQL mutation via `["api", "graphql", "--input", "-"]` with `input=json.dumps({"query": MUTATION, "variables": {...}})`; a response whose `errors[0].message` contains `Expected branch to point to` (GitHub's wording for a stale `expectedHeadOid`) raises `GhWriteError("commit", …, conflict=True)`.

- [ ] **Step 1: Write the failing tests**

`server/tests/test_pm_map_changes_gh.py` — a recording fake for `run`:

```python
"""GhWrite: the map-changes GitHub seam (roadmap spec §9), against a recorded
fake `gh` — the "fake GitHub layer" §14 asks for. Every method is argv-checked
so a future change to a REST path or the GraphQL document is a test diff."""
from __future__ import annotations

import base64
import json
import subprocess

import pytest

from server.pm.map_changes_gh import GhWrite, GhWriteError

REPO = "cairnea/KinDash"


class FakeGh:
    """Answers `gh` calls from a queue of (stdout, returncode, stderr) and records argv + stdin + token."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.calls: list[tuple[list[str], str | None, str | None]] = []

    def __call__(self, args, token=None, *, input=None, timeout=None):
        self.calls.append((list(args), token, input))
        out, rc, err = self.answers.pop(0)
        return subprocess.CompletedProcess(args, rc, stdout=out, stderr=err)


def _gh(answers, *, app_token="app-tok", env=None):
    fake = FakeGh(answers)
    gh = GhWrite(REPO, run=fake, app_token=lambda repo: app_token,
                 env=env if env is not None else {"CCSWITCH_GH_PROJECT_TOKEN": "pat-tok"})
    return gh, fake


def test_default_branch_head_and_ensure_branch_creates_from_it():
    gh, fake = _gh([
        (json.dumps({"object": {"sha": "abc"}}), 0, ""),               # GET git/ref/heads/master
        ("", 1, "HTTP 404: Not Found"),                                 # GET git/ref/heads/pm/map-changes-kindash
        (json.dumps({"ref": "refs/heads/pm/map-changes-kindash", "object": {"sha": "abc"}}), 0, ""),  # POST git/refs
    ])
    assert gh.default_branch_head("master") == "abc"
    assert gh.ensure_branch("pm/map-changes-kindash", "abc") == "abc"
    assert fake.calls[0][0] == ["api", f"repos/{REPO}/git/ref/heads/master"]
    assert fake.calls[2][0][:4] == ["api", "-X", "POST", f"repos/{REPO}/git/refs"]
    assert "ref=refs/heads/pm/map-changes-kindash" in fake.calls[2][0]
    assert "sha=abc" in fake.calls[2][0]
    assert all(call[1] == "app-tok" for call in fake.calls)


def test_ensure_branch_returns_the_existing_head():
    gh, fake = _gh([(json.dumps({"object": {"sha": "def"}}), 0, "")])
    assert gh.ensure_branch("pm/map-changes-kindash", "abc") == "def"
    assert len(fake.calls) == 1


def test_read_file_decodes_and_404_is_none():
    text = "---\nslug: x\n---\n# X\n"
    gh, fake = _gh([
        (json.dumps({"content": base64.b64encode(text.encode()).decode(), "sha": "blob1"}), 0, ""),
        ("", 1, "HTTP 404: Not Found"),
    ])
    assert gh.read_file("docs/product/features/x.md", "pm/map-changes-kindash") == (text, "blob1")
    assert gh.read_file("docs/product/features/y.md", "pm/map-changes-kindash") == (None, None)
    assert fake.calls[0][0] == ["api", f"repos/{REPO}/contents/docs/product/features/x.md?ref=pm/map-changes-kindash"]


def test_commit_file_uses_create_commit_on_branch_with_expected_head():
    gh, fake = _gh([(json.dumps({"data": {"createCommitOnBranch": {"commit": {"oid": "new1"}}}}), 0, "")])
    sha = gh.commit_file("pm/map-changes-kindash", "abc", "docs/product/features/x.md", "# X\n", "map(kindash): add capability x")
    assert sha == "new1"
    args, token, stdin = fake.calls[0]
    assert args == ["api", "graphql", "--input", "-"]
    payload = json.loads(stdin)
    assert "createCommitOnBranch" in payload["query"]
    v = payload["variables"]["input"]
    assert v["branch"] == {"repositoryNameWithOwner": REPO, "branchName": "pm/map-changes-kindash"}
    assert v["expectedHeadOid"] == "abc"
    assert v["message"] == {"headline": "map(kindash): add capability x"}
    assert v["fileChanges"]["additions"] == [{"path": "docs/product/features/x.md",
                                              "contents": base64.b64encode(b"# X\n").decode()}]


def test_commit_file_reports_a_moved_head_as_a_conflict():
    gh, _ = _gh([(json.dumps({"errors": [{"message": "Expected branch to point to \"abc\" but it did not"}]}), 0, "")])
    with pytest.raises(GhWriteError) as e:
        gh.commit_file("b", "abc", "p", "t", "m")
    assert e.value.step == "commit" and e.value.conflict is True


def test_open_read_close_pr_update_branch_and_delete_branch():
    pr = {"number": 12, "html_url": "https://github.com/cairnea/KinDash/pull/12", "state": "open",
          "merged": False, "merged_at": None, "head": {"sha": "new1"}, "mergeable_state": "behind"}
    gh, fake = _gh([
        (json.dumps(pr), 0, ""),                                   # POST pulls
        (json.dumps({**pr, "mergeable_state": "dirty"}), 0, ""),   # GET pulls/12
        ("", 0, ""),                                               # PATCH pulls/12 state=closed
        ("", 0, ""),                                               # PUT pulls/12/update-branch
        ("", 0, ""),                                               # DELETE git/refs/heads/...
    ])
    opened = gh.open_pr("pm/map-changes-kindash", "master", "Capability map changes: KinDash", "body")
    assert (opened.number, opened.url, opened.behind, opened.conflicting) == (12, pr["html_url"], True, False)
    assert fake.calls[0][0][:4] == ["api", "-X", "POST", f"repos/{REPO}/pulls"]
    assert "draft=false" in fake.calls[0][0] or "-F" in fake.calls[0][0]
    read = gh.read_pr(12)
    assert read.conflicting is True and read.head_sha == "new1"
    gh.close_pr(12)
    assert fake.calls[2][0][:4] == ["api", "-X", "PATCH", f"repos/{REPO}/pulls/12"] and "state=closed" in fake.calls[2][0]
    gh.update_branch(12)
    assert fake.calls[3][0][:4] == ["api", "-X", "PUT", f"repos/{REPO}/pulls/12/update-branch"]
    gh.delete_branch("pm/map-changes-kindash")
    assert fake.calls[4][0][:4] == ["api", "-X", "DELETE", f"repos/{REPO}/git/refs/heads/pm/map-changes-kindash"]


def test_app_token_403_falls_back_to_the_pat_and_sticks():
    gh, fake = _gh([
        ("", 1, "HTTP 403: Resource not accessible by integration"),   # app token refused
        (json.dumps({"object": {"sha": "abc"}}), 0, ""),               # PAT works
        (json.dumps({"object": {"sha": "abc"}}), 0, ""),               # next call goes straight to the PAT
    ])
    assert gh.default_branch_head("master") == "abc"
    assert gh.default_branch_head("master") == "abc"
    assert [c[1] for c in fake.calls] == ["app-tok", "pat-tok", "pat-tok"]


def test_no_app_token_and_no_pat_means_ambient_gh():
    gh, fake = _gh([(json.dumps({"object": {"sha": "abc"}}), 0, "")], app_token=None, env={})
    gh.default_branch_head("master")
    assert fake.calls[0][1] is None


def test_other_failures_raise_with_the_step():
    gh, _ = _gh([("", 1, "HTTP 502: Bad Gateway")])
    with pytest.raises(GhWriteError) as e:
        gh.default_branch_head("master")
    assert e.value.step == "head" and "502" in e.value.detail and e.value.conflict is False
```

- [ ] **Step 2: Run them to verify they fail**

Run: `… pytest server/tests/test_pm_map_changes_gh.py -q` → Expected: FAIL, module not found.

- [ ] **Step 3: Write the seam**

`server/pm/map_changes_gh.py`:

```python
"""GitHub write seam for map-changes PRs (roadmap drill-down spec §9).

Every call is a ``gh`` subprocess through ``gitrepos._run_gh`` with an
injectable runner, so tests record argv and answer canned JSON. Commits go
through GraphQL ``createCommitOnBranch`` — atomic, server-side, and its
``expectedHeadOid`` turns a moved branch head into an explicit conflict — so
the shared local clone (read SHA-pinned by the sync loop, used as cwd by
sessions) is never mutated. Branch, PR and content reads are REST.

Auth (R8b-3): the GitHub App installation token for the repo's owner first;
when GitHub answers ``Resource not accessible by integration`` (the App lacks
``contents:write`` / ``pull_requests:write``) the call is retried with the
classic PAT ``CCSWITCH_GH_PROJECT_TOKEN`` and the instance sticks with it;
with neither, ambient ``gh`` credentials."""
from __future__ import annotations

import base64
import json
import logging
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from ccswitch import gitrepos

log = logging.getLogger(__name__)

_INTEGRATION_DENIED = "Resource not accessible by integration"
_STALE_HEAD = "Expected branch to point to"

_COMMIT_MUTATION = """
mutation($input: CreateCommitOnBranchInput!) {
  createCommitOnBranch(input: $input) { commit { oid } }
}
"""


class GhWriteError(RuntimeError):
    def __init__(self, step: str, detail: str, *, conflict: bool = False) -> None:
        super().__init__(f"{step}: {detail}")
        self.step = step
        self.detail = detail
        self.conflict = conflict


@dataclass(frozen=True)
class PrState:
    number: int
    url: str
    state: str
    merged: bool
    merged_at: str | None
    head_sha: str
    mergeable_state: str | None

    @property
    def behind(self) -> bool:
        return self.mergeable_state == "behind"

    @property
    def conflicting(self) -> bool:
        return self.mergeable_state == "dirty"


def _pr_state(d: Mapping[str, object]) -> PrState:
    head = d.get("head") if isinstance(d.get("head"), Mapping) else {}
    return PrState(
        number=int(d.get("number") or 0), url=str(d.get("html_url") or ""),
        state=str(d.get("state") or ""), merged=bool(d.get("merged")),
        merged_at=(str(d["merged_at"]) if d.get("merged_at") else None),
        head_sha=str((head or {}).get("sha") or ""),
        mergeable_state=(str(d["mergeable_state"]) if d.get("mergeable_state") else None),
    )


class GhWrite:
    def __init__(self, repo: str, *, run: Callable = gitrepos._run_gh,
                 app_token: Callable[[str], str | None] = gitrepos.app_token_for_repo,
                 env: Mapping[str, str] = os.environ) -> None:
        self.repo = repo
        self._run = run
        self._app = app_token(repo)
        self._pat = env.get("CCSWITCH_GH_PROJECT_TOKEN") or None
        self._use_pat = False

    # -- plumbing -------------------------------------------------------------
    def _token(self) -> str | None:
        if self._use_pat:
            return self._pat
        return self._app or self._pat

    def _gh(self, args: list[str], step: str, *, input: str | None = None,
            allow_404: bool = False) -> str:
        token = self._token()
        proc = self._run(args, token, input=input, timeout=60)
        if proc.returncode != 0 and _INTEGRATION_DENIED in (proc.stderr or "") + (proc.stdout or "") \
                and token == self._app and self._pat:
            log.info("map_changes.gh: App token refused for %s; falling back to the project PAT", self.repo)
            self._use_pat = True
            proc = self._run(args, self._pat, input=input, timeout=60)
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "").strip()
            if allow_404 and "404" in err:
                return ""
            raise GhWriteError(step, err[:300])
        return proc.stdout or ""

    @staticmethod
    def _json(text: str) -> dict:
        return json.loads(text) if text.strip() else {}

    # -- refs -----------------------------------------------------------------
    def default_branch_head(self, default_branch: str) -> str:
        d = self._json(self._gh(["api", f"repos/{self.repo}/git/ref/heads/{default_branch}"], "head"))
        sha = str((d.get("object") or {}).get("sha") or "")
        if not sha:
            raise GhWriteError("head", f"no sha for {default_branch}")
        return sha

    def ensure_branch(self, branch: str, from_sha: str) -> str:
        existing = self._json(self._gh(["api", f"repos/{self.repo}/git/ref/heads/{branch}"], "branch", allow_404=True))
        sha = str((existing.get("object") or {}).get("sha") or "")
        if sha:
            return sha
        created = self._json(self._gh(["api", "-X", "POST", f"repos/{self.repo}/git/refs",
                                       "-f", f"ref=refs/heads/{branch}", "-f", f"sha={from_sha}"], "branch"))
        return str((created.get("object") or {}).get("sha") or from_sha)

    def delete_branch(self, branch: str) -> None:
        self._gh(["api", "-X", "DELETE", f"repos/{self.repo}/git/refs/heads/{branch}"], "delete_branch", allow_404=True)

    # -- content --------------------------------------------------------------
    def read_file(self, path: str, ref: str) -> tuple[str | None, str | None]:
        out = self._gh(["api", f"repos/{self.repo}/contents/{path}?ref={ref}"], "read", allow_404=True)
        if not out.strip():
            return None, None
        d = self._json(out)
        raw = base64.b64decode(str(d.get("content") or "")).decode(errors="replace")
        return raw, (str(d["sha"]) if d.get("sha") else None)

    def commit_file(self, branch: str, expected_head: str, path: str, text: str, message: str) -> str:
        payload = {"query": _COMMIT_MUTATION, "variables": {"input": {
            "branch": {"repositoryNameWithOwner": self.repo, "branchName": branch},
            "expectedHeadOid": expected_head,
            "message": {"headline": message},
            "fileChanges": {"additions": [
                {"path": path, "contents": base64.b64encode(text.encode()).decode()}]},
        }}}
        d = self._json(self._gh(["api", "graphql", "--input", "-"], "commit", input=json.dumps(payload)))
        errors = d.get("errors") or []
        if errors:
            msg = str((errors[0] or {}).get("message") or errors)
            raise GhWriteError("commit", msg[:300], conflict=_STALE_HEAD in msg)
        oid = str((((d.get("data") or {}).get("createCommitOnBranch") or {}).get("commit") or {}).get("oid") or "")
        if not oid:
            raise GhWriteError("commit", f"no commit oid in {json.dumps(d)[:200]}")
        return oid

    # -- pull requests --------------------------------------------------------
    def open_pr(self, branch: str, base: str, title: str, body: str) -> PrState:
        d = self._json(self._gh(["api", "-X", "POST", f"repos/{self.repo}/pulls",
                                 "-f", f"head={branch}", "-f", f"base={base}",
                                 "-f", f"title={title}", "-F", "draft=false", "-F", "body=@-"],
                                "pr", input=body))
        return _pr_state(d)

    def read_pr(self, number: int) -> PrState:
        return _pr_state(self._json(self._gh(["api", f"repos/{self.repo}/pulls/{number}"], "pr_read")))

    def close_pr(self, number: int) -> None:
        self._gh(["api", "-X", "PATCH", f"repos/{self.repo}/pulls/{number}", "-f", "state=closed"], "close")

    def update_branch(self, number: int) -> None:
        self._gh(["api", "-X", "PUT", f"repos/{self.repo}/pulls/{number}/update-branch"], "update_branch")
```

`-F body=@-` reads the body from stdin (the `_run_gh(input=…)` contract, `gitrepos.py:403-406`). Adjust the `open_pr` assertion in the test to whichever of `-F draft=false` / `body=@-` you keep — the test's `or` already tolerates it.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `… pytest server/tests/test_pm_map_changes_gh.py -q` → Expected: 9 passed. `ruff`, `mypy` clean (the `gitrepos` import path is `from ccswitch import gitrepos` — check how `server/sessions/orchestrator.py` imports it and match).

- [ ] **Step 5: Commit**

```bash
git add server/pm/map_changes_gh.py server/tests/test_pm_map_changes_gh.py
git commit -F /tmp/row8b-t3.msg   # feat(pm): 8b task 3 — GhWrite, the map-changes GitHub seam (branch, createCommitOnBranch, PR)
```

---

### Task 4: The service and the API — `POST/DELETE /api/pm/projects/{slug}/map-changes`, `POST …/rebase`

**Files:**
- Create: `server/pm/map_changes.py`
- Modify: `server/sessions/orchestrator.py` (a `pm_map_changes_gh(repo)` factory next to `_gh_run`, `:978`)
- Modify: `server/api/pm.py` (routes after `advance_map`)
- Test: `server/tests/test_pm_map_changes_api.py`

**Interfaces:**
- Consumes: Tasks 1–3; `request_project_sync(db, project_id, now)` (`server/pm/sync_request.py:22`); `_load_project` (`pm.py:159`); `ProjectRepo.is_primary`; `orch._sdlc_default_branch(repo)` (`orchestrator.py:7309`); `record_audit`.
- Produces:

```python
# server/pm/map_changes.py
@dataclass(frozen=True)
class Pointer:      # the wire shape, also used by the map payload in Task 6
    branch: str; pr_number: int | None; pr_url: str | None; status: str
    opened_at: str | None; head_sha: str | None; behind: bool; conflicting: bool
    def to_dict(self) -> dict   # camelCase keys: branch, prNumber, prUrl, status, openedAt, headSha, behind, conflicting

class MapChangesService:
    def __init__(self, gh_factory: Callable[[str], GhWrite], default_branch: Callable[[str], Awaitable[str]], now=…): ...
    async def open_pointer(self, db, project) -> FeatureMapChange | None      # the open row
    async def context(self, db, project, gh: GhWrite, pointer) -> MapContext  # indexed slugs + branch docs (ls via contents dir listing) + link targets
    async def edit(self, db, project, op: MapOp, identity: str) -> tuple[Pointer, str, str]   # (pointer, path, commit sha)
    async def abandon(self, db, project, identity: str) -> Pointer
    async def rebase(self, db, project, identity: str) -> Pointer
```

`edit`: validate against `context`; primary repo required (422 `no_primary_repo`); `gh = gh_factory(repo)`; `base = await default_branch(repo)`; `head = pointer.head_sha if pointer else gh.ensure_branch(branch, gh.default_branch_head(base))` — re-read the branch head via `ensure_branch` anyway (cheap, and the row may be stale); current text = `gh.read_file(path, ref=branch)` (for `create`: must be `None`, else 422 `slug_taken`); `new_text = apply_op(...)`; `sha = gh.commit_file(branch, head, path, new_text, commit_message(op, project.name))` — on `GhWriteError(conflict=True)` re-read the head once and retry, then raise 409 `conflict`; if no pointer row: `pr = gh.open_pr(branch, base, f"Capability map changes: {project.name}", PR_BODY)` and insert the row; update `head_sha`; `request_project_sync`; audit `pm.map_changes.<op>` with `{slug, op, capability, path, commitSha, prNumber}`. `PR_BODY` (a module constant) explains the PR is ccswitch-owned, appended by every inline edit, and that the first machine edit canonicalises frontmatter (R8b-12).

`abandon`: 404 `no_open_map_changes` when none; `gh.close_pr`, `gh.delete_branch`; row `status="abandoned"`, `closed_at`; delete proposed-only unreferenced Feature rows / orphan referenced ones (R8b-7 — implement in Task 6's cleanup helper; in this task, only the row + PR + branch); audit `pm.map_changes.abandon`.

`rebase`: 404 when none; 409 `not_behind` unless `pointer.behind`; `gh.update_branch(pr_number)`; audit `pm.map_changes.rebase`.

API (`server/api/pm.py`):

```python
class MapChangeBody(BaseModel):
    op: str
    slug: str
    title: str | None = None
    parentSlug: str | None = None
    summary: str | None = None
    links: list[dict] | None = None     # [{kind, project, feature}]


@router.post("/projects/{slug}/map-changes")
async def map_changes_edit(slug, body, request, identity, db) -> dict:
    """One capability edit on the product's map-changes branch (spec §8/§9)."""
    p = await _load_project(db, slug)
    svc = request.app.state.sessions.pm_map_changes()
    op = MapOp(op=body.op, slug=body.slug, title=body.title, parent_slug=body.parentSlug,
               summary=body.summary,
               links=tuple(FeatureLinkRef(kind=str(l.get("kind")), project=str(l.get("project")),
                                          feature=str(l.get("feature"))) for l in (body.links or [])))
    try:
        pointer, path, sha = await svc.edit(db, p, op, identity)
    except MapOpError as e:
        raise HTTPException(status_code=422, detail={"error": e.code, "message": e.message})
    except NoPrimaryRepo:
        raise HTTPException(status_code=422, detail={"error": "no_primary_repo", "message": "..."})
    except GhWriteError as e:
        if e.conflict:
            raise HTTPException(status_code=409, detail={"error": "conflict", "step": e.step, "message": e.detail})
        raise HTTPException(status_code=502, detail={"error": "github", "step": e.step, "message": e.detail})
    return {"pointer": pointer.to_dict(), "path": path, "commitSha": sha}
```

plus `DELETE /projects/{slug}/map-changes` → `{"pointer": …}` (404 `no_open_map_changes`) and `POST /projects/{slug}/map-changes/rebase` → `{"pointer": …}` (404 / 409 `not_behind`). The orchestrator exposes `pm_map_changes()` returning a `MapChangesService(gh_factory=lambda repo: GhWrite(repo, run=self._gh_run), default_branch=self._sdlc_default_branch)` so tests swap `orch._gh_run` for a fake before `create_app`.

- [ ] **Step 1: Write the failing API tests**

`server/tests/test_pm_map_changes_api.py` — seed a product with a primary repo and two features (`_seed_product_with_two_capabilities` from `test_sdlc_api.py` is the shape; copy it here with a `ProjectRepo(project_id=p.id, repo="o/r", is_primary=True)` added), build the app with `orch._gh_run = FakeGh(...)` (the recording fake from Task 3's test; import it or duplicate the 12 lines), and assert:
- `create` on a project with no pointer: the fake sees `git/ref/heads/master` → `git/ref/heads/pm/map-changes-<slug>` (404) → `POST git/refs` → `contents/...?ref=pm/map-changes-<slug>` (404) → `graphql` → `POST pulls`; the response carries `pointer.prNumber == 12`, `path`, `commitSha`; a `feature_map_changes` row exists with status `open`; `sync_requested_at` on the project's repos is set (read `ProjectRepo` via `c.portal.call`); audit `pm.map_changes.create`.
- a second edit reuses the row: no `POST pulls`, one `graphql`.
- `rename` of an unknown slug → 422 `not_found`; a bad op → 422 `bad_op`; a project with no primary repo → 422 `no_primary_repo`.
- a stale head: the fake answers the graphql call with the `Expected branch to point to` error, then (after the re-read) succeeds → 200 with one extra `git/ref/heads/<branch>` read; answered stale twice → 409 `conflict`.
- a 502 from `gh` → 502 with `detail.step`.
- `DELETE` with no pointer → 404; with one → `PATCH pulls/12 state=closed`, `DELETE git/refs/heads/…`, row `abandoned`, audit.
- `rebase` when `behind` is false → 409 `not_behind`; set the row's `behind=True` via `c.portal.call` → `PUT pulls/12/update-branch`, audit.

Run: `… pytest server/tests/test_pm_map_changes_api.py -q` → Expected: FAIL (404 route not found / import errors).

- [ ] **Step 2: Write the service, the orchestrator accessor and the routes** as specified above. `context()` builds `parents` from the project's non-orphaned `Feature` rows (slug → parent slug) merged with the branch docs (`gh.read_file` per path from `["api", f"repos/{repo}/contents/docs/product/features?ref={branch}"]` — a directory listing; only when a pointer is open; parse each with `parse_feature_doc`, skipping `FeatureDocError`), and `link_targets` from every non-orphaned `Feature` joined to its `Project.slug`.

- [ ] **Step 3: Run the tests to verify they pass**

Run: `… pytest server/tests/test_pm_map_changes_api.py server/tests/test_pm_api.py -q` → Expected: all passed. `ruff`, `mypy` clean.

- [ ] **Step 4: Commit**

```bash
git add server/pm/map_changes.py server/sessions/orchestrator.py server/api/pm.py server/tests/test_pm_map_changes_api.py
git commit -F /tmp/row8b-t4.msg   # feat(pm): 8b task 4 — map-changes service and API (edit, abandon, rebase)
```

---

### Task 5: Index the branch head; stamp `commit_sha` and `pr_number`

**Files:**
- Modify: `server/pm/feature_index.py` (`FeatureUpsert`, `plan_feature_index`)
- Modify: `server/sessions/orchestrator.py:6132-6206` (`_pm_sync_read_feature_docs(repo, ref=None) -> FeatureDocsRead`), `:6220-6314` (`_pm_sync_apply_feature_plan` writes `commit_sha`/`pr_number`/`pr_url`), and a new seam `_pm_sync_read_map_changes(project_id) -> Pointer | None`
- Modify: `server/automations/pm_sync.py:132-166` (ctor seam `read_map_changes`), `:518-564` (`_feature_index`)
- Test: `server/tests/test_pm_feature_index.py`, `server/tests/test_pm_sync_loop.py`

**Interfaces:**
- Produces: `FeatureDocsRead(sha: str, docs: list[tuple[str, str]])` (`server/pm/feature_index.py`); `FeatureUpsert` gains `commit_sha: str | None = None`, `pr_number: int | None = None`, `pr_url: str | None = None`; `plan_feature_index(existing, docs, *, read_sha: str | None = None, proposed: Mapping[str, tuple[int | None, str | None]] | None = None)` — a doc whose slug is in `proposed` gets `pr_number/pr_url` and `commit_sha=None`; every other doc gets `commit_sha=read_sha` and `pr_number=pr_url=None`. `PmSyncLoop.__init__(…, read_map_changes=None)` (keyword, default `None` → no branch read, so every existing test and caller keeps its shape).
- The merge rule (R8b-4): `docs = {slug: (path, doc) for default docs}` then overridden by the branch docs; `proposed = {slug: (pr_number, pr_url) for slug in branch docs}` (every doc on the branch is "proposed" while the PR is open — a renamed existing capability shows its proposed title with the PR badge).

- [ ] **Step 1: Planner tests first** (`test_pm_feature_index.py`): a doc in `proposed` yields an upsert with the PR fields and `commit_sha None`; a plain doc yields `commit_sha == read_sha`; omitted kwargs behave as today (`commit_sha None`, no PR) so the file's existing tests pass unchanged.
- [ ] **Step 2: Sync-loop tests** (`test_pm_sync_loop.py`): with `feature_docs` faked as `FeatureDocsRead("sha-main", [...])` for `ref=None` and `FeatureDocsRead("sha-branch", [...])` for `ref="pm/map-changes-proj"`, and `read_map_changes` returning a `Pointer(branch="pm/map-changes-proj", pr_number=12, pr_url="…", status="open", …)`: the applied plan contains the branch doc with `pr_number == 12`, the default-branch-only doc with `commit_sha == "sha-main"`, and a doc present in both with the BRANCH content and the PR fields; when `read_map_changes` returns `None` no second read happens; when the branch read RAISES, the default-branch plan is still applied and a `warn` event `map_changes_read_error` is emitted (the branch is best-effort; the default branch is not).
- [ ] **Step 3: Implement**: `_pm_sync_read_feature_docs(repo, ref=None)` uses `ref or (origin/HEAD symbolic-ref)`, for a branch `refs/remotes/origin/<branch>`, and returns `FeatureDocsRead(sha, out)`; `_feature_index` calls `self._read_feature_docs(view.repo)` then, if `read_map_changes` is set and returns an open pointer, `self._read_feature_docs(view.repo, pointer.branch)` inside its own try/except; merges; passes `read_sha=main.sha, proposed=…` to the planner. `_pm_sync_apply_feature_plan` sets `row.commit_sha = up.commit_sha`, `row.pr_number = up.pr_number`, `row.pr_url = up.pr_url` on every upsert. `_pm_sync_read_map_changes(project_id)` selects the open `FeatureMapChange` row.
- [ ] **Step 4: Gate**: `… pytest server/tests/test_pm_feature_index.py server/tests/test_pm_sync_loop.py server/tests/test_pm_sync_projects.py server/tests/test_pm_map_api.py -q` → all passed; `ruff`, `mypy` clean.
- [ ] **Step 5: Commit**

```bash
git add server/pm/feature_index.py server/sessions/orchestrator.py server/automations/pm_sync.py server/tests/test_pm_feature_index.py server/tests/test_pm_sync_loop.py
git commit -F /tmp/row8b-t5.msg   # feat(pm): 8b task 5 — index the map-changes branch head; stamp commit_sha and pr_number
```

---

### Task 6: PR lifecycle, abandon cleanup, the map payload, the AI-diff retarget

**Files:**
- Modify: `server/pm/map_changes.py` (`refresh(db, project, gh)`, `cleanup_after_abandon(db, project, pr_number)`)
- Modify: `server/automations/pm_sync.py` (`_incremental` calls a `refresh_map_changes(view.project_id)` seam after `_feature_index` when `view.is_primary`)
- Modify: `server/sessions/orchestrator.py` (`_pm_sync_refresh_map_changes`; `pm_onboard_spawn_map` passes `branch=`), `server/pm/classification.py:100-173` (`build_map_synthesis_prompt(..., branch: str | None = None)` — the diff branch name), `server/pm/tree.py` (`ProductIn.map_changes: dict | None`; product dict gains `"mapChanges"`), `server/api/pm_tree.py` (load open pointers per project)
- Modify: `web/src/api/roadmap.ts` (`ProductNode.mapChanges: MapChangesPointer | null`, `MapChangesPointer {branch, prNumber, prUrl, status, openedAt, headSha, behind, conflicting}`), and every web fixture typed `ProductNode` (`typecheck` lists them — add `mapChanges: null`)
- Test: `server/tests/test_pm_map_changes_api.py` (refresh + cleanup), `server/tests/test_pm_map_api.py` (`mapChanges` on the product), `server/tests/test_pm_onboarding_prompt.py` or the existing prompt test file (`grep -rl build_map_synthesis_prompt server/tests`) for the branch retarget

- [ ] **Step 1: Tests first**: (a) `refresh` with the fake answering `GET pulls/12` as merged → row `merged`, `merged_at` set; as closed-unmerged → `abandoned`; as open+`behind` → `behind=True`; a `GhWriteError` → `last_error` set, status unchanged. (b) `cleanup_after_abandon`: a Feature with `pr_number=12, commit_sha=None` and no references is deleted; one referenced by a `WorkItem.feature_id` is orphaned instead. (c) `GET /api/pm/map` carries `mapChanges` for a product with an open row and `null` otherwise. (d) `build_map_synthesis_prompt(scope="diff", branch="pm/map-changes-x")` names that branch in the instructions, and without `branch` names `pm/feature-map-<slug>` as today; `pm_onboard_spawn_map(scope="diff")` passes the open pointer's branch (assert on the prompt captured by the fake spawn ctx — find the existing test for `pm_onboard_spawn_map` and extend it).
- [ ] **Step 2: Implement** per R8b-6/7/9 and the interfaces above. `_incremental`'s refresh is best-effort (try/except → `warn` event `map_changes_refresh_error`). The `DELETE` route from Task 4 now also calls `cleanup_after_abandon` after closing the PR.
- [ ] **Step 3: Gates**: server (`… pytest server/tests/test_pm_map_changes_api.py server/tests/test_pm_map_api.py server/tests/test_pm_sync_loop.py <the prompt/spawn test file> -q`, `ruff`, `mypy`) and the web gate for the type change (`(cd web && npm run typecheck; echo rc=$?; npm run lint; echo rc=$?; npm run test > /tmp/8b-t6-vitest.log 2>&1; echo rc=$?; grep -E "Test Files|Tests " /tmp/8b-t6-vitest.log; npm run build > /tmp/8b-t6-build.log 2>&1; echo rc=$?); rm -rf web/dist`).
- [ ] **Step 4: Commit**

```bash
git add server/pm/map_changes.py server/automations/pm_sync.py server/sessions/orchestrator.py server/pm/classification.py server/pm/tree.py server/api/pm_tree.py server/api/pm.py web/src/api/roadmap.ts <web fixtures> <tests>
git commit -F /tmp/row8b-t6.msg   # feat(pm): 8b task 6 — map-changes PR lifecycle, abandon cleanup, mapChanges on the map, AI diff on the shared branch
```

---

## Self-review

**Spec coverage (§9, bullet by bullet):** first UI edit creates the branch from the default branch, writes the doc, opens a non-draft PR titled "Capability map changes: <product>" — Task 4; later edits push commits to it — Task 4 (`ensure_branch` + `commit_file`); the AI map-diff proposal targets the same branch when open — Task 6 (R8b-9); `feature_map_changes` with `project_id, branch, pr_number, opened_at, status ∈ open|merged|abandoned` — Task 1 (plus `head_sha/behind/conflicting/last_error` for the UI); branch content is truth, the row a pointer — by construction; the index runs over the branch head, proposed capabilities get `pr_number`/`commit_sha` and render as proposed — Task 5 (the `CapabilityDetail` badge already keys on `prNumber`; `commit_sha` is stamped on merge by the next default-branch pass); merge clears them on the next sync — Task 5 (a merged pointer is not `open`, so no branch read; the docs are now on the default branch → `pr_number` cleared, `commit_sha` set); product header shows the open PR, age, Rebase when behind; abandon closes and drops the pointer — data in Task 6 (`mapChanges`), actions in Task 4; UI in 8c; conflicts surface as a banner — `conflicting` in Task 6, banner in 8c; never force-push — `update-branch` only. §8 rows: add/rename/reparent/summary → Task 2 ops; capability links chip editor → the `links` op. §10: `POST …/map-changes {op: create|rename|reparent|summary|links}` and `DELETE` — Task 4. §14: frontmatter round-trip — Task 2 tests; fake git/GitHub layer — Task 3's `FakeGh`; conflict and abandon paths — Task 4 tests.

**Not covered, by design:** the §9 "Rebase" UI and the "proposed" row badge in the list column (8c); the archaeology session's own branch remains `pm/feature-map-<slug>` for `scope="initial"`.

**Placeholder scan:** Tasks 5 and 6 describe their test cases and interfaces in prose with exact names rather than full code blocks — the implementer writes the code against the signatures in the Interfaces blocks; every name used there is defined in Tasks 1–4 or in the cited existing files. No "TBD"/"handle errors".

**Type consistency:** `Pointer` (Task 4) is what `read_map_changes` (Task 5) returns and what `mapChanges` (Task 6) serialises via `to_dict()`; `FeatureDocsRead` is produced by the orchestrator reader and consumed by `_feature_index`; `MapOp`/`MapContext`/`MapOpError` (Task 2) are what the service (Task 4) validates with; `GhWrite`/`GhWriteError`/`PrState` (Task 3) are what the service and `refresh` (Task 6) call.

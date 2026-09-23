# Governance: check the onboarded repo has a ratified constitution — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop treating an onboarded repo's constitution as ratified when it may not exist, or may
still be the unfilled spec-kit template — surface the gap on the product overview (a Needs-you
item) and stop telling spec sessions to blindly "apply the repository constitution" when there is
nothing real to apply.

> **Amendment (as built):** the classifier ships as `server/sdlc/constitution_status.py`, not as a
> function inside `constitution_gate.py`. `constitution_gate.py` is a `GUARDRAIL_PATHS` entry, so
> *any* edit to it fails the `amendment` required check until the owner acknowledges the PR — and a
> read-only hygiene classifier is not a governance-enforcement change worth spending that on. The
> footer grammar stays single-sourced: the new module imports `constitution_gate.parse_footer`.
> Below, read every `constitution_gate.classify_document` as `constitution_status.classify_document`
> and every "add to `constitution_gate.py`" as "add to `constitution_status.py`"; its tests live in
> `server/tests/test_constitution_status.py`.

**Architecture:** A new pure classifier (`constitution_gate.classify_document`) turns raw
constitution text into one of `None` (looks ratified) / `"missing"` / `"template"` /
`"no_version_footer"`. `pm_sync`'s existing per-primary-repo pass reads
`.specify/memory/constitution.md` off the shared local clone (same git-plumbing approach as its
existing `_pm_sync_read_feature_docs` seam) and stores the classified status on `Project.
constitution_status` — mirroring the `unindexed_docs` column exactly (#2166's pattern).
`roadmap_health` surfaces it as a new `governance` field; the product-overview `RoadmapHealthCard`
renders it as a warning bullet, following its existing per-dimension `<li>` pattern. Separately,
`build_spec_prompt`'s "no org skills supplied" fallback stops saying the bare phrase "apply the
repository constitution" — the SDLC synth spawn seam already reads a committed file
(`_sdlc_show_committed_file`) for the upstream `intent.md`; it does the same for the constitution
and `build_spec_prompt` renders either the real path + extracted `###` principle headings, or an
explicit governance-gap instruction to flag as a spec constraint.

**Tech Stack:** Python (FastAPI/SQLAlchemy backend, pure dataclass/function modules), React+TS
frontend, pytest, vitest.

**Spec:** GitHub issue steiner385/viberpit#2165 (see issue body in the PR/commit history — no
separate spec doc; the issue itself is short enough to be the spec). Items 2 ("propose
constitution" onboarding session) and 4 ("reuse `check_amendment` for onboarded-repo PRs") are
addressed by a design note in Task 5 rather than new automation — see that task for why, and the
follow-up issue it opens.

## Global Constraints

- Every new/changed model column must be nullable (reconcile ALTER-ADDs it on both SQLite and
  Postgres); add a migration revision anyway, chained off the current head
  (`0060_backlog_groups_degraded`).
- `server/pm/health.py`, `server/sdlc/prompts.py`, and `server/sdlc/constitution_gate.py` are
  documented as pure, no-I/O modules — new code in them must stay pure; I/O lives in the
  orchestrator seam / API handler that calls them.
- Reuse `constitution_gate.parse_footer` (already exported) — do not re-derive the
  Version/Ratified/Last-Amended regex.
- Every commit message ends with `Co-Authored-By: Claude <model> <noreply@anthropic.com>`.
- Run the full relevant test file(s) after each task, not just the new test — these modules are
  shared by other features (`unindexed_docs`, `_pm_sync_read_feature_docs`) that a careless edit
  could break silently.

## Review Focus

- A repo whose constitution file is present, not a template, but has a footer that fails to parse
  (e.g. hand-edited into an unrecognized date format) must read `"no_version_footer"`, not silently
  pass as ratified — Task 1's tests must cover this distinctly from `"template"` and `"missing"`.
- `_pm_sync_read_constitution` must treat "file does not exist in this repo" as a normal `None`
  result, never an exception — unlike `_pm_sync_read_feature_docs`'s atomic-or-raise contract for
  the whole feature-doc tree, a single missing file here is the expected common case, not a
  partial-read failure to guard against.
- `RoadmapHealthCard`'s existing "renders nothing when nothing to say" contract must still hold
  when `governance` is `null`/absent — a test must assert the card stays hidden when every other
  field is empty and governance is not reported, so the new bullet cannot make the banner
  permanently visible for products with no health data at all.
- `build_spec_prompt`'s existing invariant tests (`_all_prompts()` in `test_sdlc_prompts.py`) run
  the builder with the *default* `constitution=None` — the new code path must not break the
  already-passing "prompt is non-empty" / "mentions the target path" assertions for every existing
  caller that does not pass the new kwarg.
- The template-token regex used to detect an unfilled spec-kit constitution must not misfire on
  ViberPit's own ratified constitution (which contains bracketed prose nowhere resembling
  `[UPPER_CASE_TOKEN]`) — Task 1 pins this with a real-file regression test, mirroring
  `test_constitution_gate.py`'s existing `test_real_constitution_footer_parses`.

---

## File Structure

- **`server/sdlc/constitution_gate.py`** (modify) — add `classify_document`, the pure
  present/template/footer classifier. Already imports `re`; no new imports needed.
- **`server/tests/test_constitution_gate.py`** (modify) — tests for `classify_document`.
- **`server/db/models.py`** (modify) — `Project.constitution_status` nullable column.
- **`server/db/migrations/versions/0061_project_constitution_status.py`** (create) — the reviewable
  migration record (reconcile is what actually applies it).
- **`server/pm/mirror_store.py`** (modify) — `set_project_constitution_status`.
- **`server/tests/test_pm_sync_save_constitution_status.py`** (create) — orchestrator-seam
  persistence tests, mirroring `test_pm_sync_save_unindexed_docs.py`.
- **`server/sessions/orchestrator.py`** (modify) — `_pm_sync_read_constitution`,
  `_pm_sync_save_constitution_status`, wire both into the one `PmSyncLoop(...)` construction site;
  also modify `_sdlc_spawn_synth`'s `build()` closure to read the constitution and pass it into
  `build_spec_prompt`.
- **`server/tests/test_pm_orchestrator_seams.py`** (modify) — `_pm_sync_read_constitution` git-based
  read tests, mirroring the existing `_pm_sync_read_feature_docs` tests in the same file.
- **`server/automations/pm_sync.py`** (modify) — `ReadConstitution`/`SaveConstitutionStatus` type
  aliases, two new optional `__init__` params, `_governance_check` method, two call sites after
  `_feature_index(view)`.
- **`server/tests/test_pm_sync_loop.py`** (modify) — `_governance_check` behavior tests (primary
  gate, best-effort on read failure, status persisted).
- **`server/pm/health.py`** (modify) — `RoadmapHealth.governance` field, `roadmap_health(...,
  governance=None)` pass-through param.
- **`server/tests/test_pm_health.py`** (modify) — governance pass-through tests.
- **`server/api/pm.py`** (modify) — `project_health` reads `p.constitution_status`, response dict
  gains `"governance"`.
- **`web/src/api/pm.ts`** (modify) — `ProjectHealth.governance: string | null`.
- **`web/src/features/roadmap/detail/RoadmapHealthCard.tsx`** (modify) — governance bullet.
- **`web/src/features/roadmap/detail/RoadmapHealthCard.test.tsx`** (modify) — governance bullet
  tests + "still hidden" regression test.
- **`server/sdlc/prompts.py`** (modify) — `build_spec_prompt` gains `constitution`/
  `constitution_path` kwargs and a `_constitution_block` helper.
- **`server/tests/test_sdlc_prompts.py`** (modify) — new-path tests for the four constitution
  states.

---

### Task 1: `constitution_gate.classify_document`

**Files:**
- Modify: `server/sdlc/constitution_gate.py`
- Test: `server/tests/test_constitution_gate.py`

**Interfaces:**
- Produces: `classify_document(text: str | None) -> str | None` — `None` when the document looks
  ratified; otherwise `"missing"`, `"template"`, or `"no_version_footer"`. Exported via `__all__`.
  Every later task that needs a governance status calls this function; no task re-derives the
  logic.

- [ ] **Step 1: Write the failing tests**

Add to `server/tests/test_constitution_gate.py` (append near the bottom, above
`test_real_constitution_footer_parses`, reusing the file's existing `_doc` helper):

```python
def test_classify_document_none_is_missing():
    assert classify_document(None) == "missing"


def test_classify_document_empty_is_missing():
    assert classify_document("   \n") == "missing"


def test_classify_document_spec_kit_template_is_template():
    template = (
        "# [PROJECT_NAME] Constitution\n\n"
        "## Core Principles\n\n"
        "### [PRINCIPLE_1_NAME]\n\n"
        "**Version**: [CONSTITUTION_VERSION] | **Ratified**: [RATIFICATION_DATE] | "
        "**Last Amended**: [LAST_AMENDED_DATE]"
    )
    assert classify_document(template) == "template"


def test_classify_document_missing_footer_is_no_version_footer():
    text = "# Real Constitution\n\n## Core Principles\n\nSome real, non-template prose."
    assert classify_document(text) == "no_version_footer"


def test_classify_document_ratified_document_is_none():
    assert classify_document(_doc("1.0.0", "2026-01-01")) is None
```

Also update the module import line at the top of the test file to include the new name:

```python
from server.sdlc.constitution_gate import (
    ACKNOWLEDGMENT_MARKER, check_amendment, check_guardrail_change, classify_document, parse_footer,
)
```

(Adjust to match whatever the existing import statement actually looks like — read the file first
and add `classify_document` to its import list rather than assuming the exact current form.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=$PWD/src /home/tony/GitHub/viberpit/.venv/bin/python -m pytest server/tests/test_constitution_gate.py -k classify_document -v`
Expected: FAIL with `ImportError` / `AttributeError: module has no attribute 'classify_document'`.

- [ ] **Step 3: Implement `classify_document`**

In `server/sdlc/constitution_gate.py`, add to `__all__`:

```python
__all__ = [
    "ACKNOWLEDGMENT_MARKER",
    "check_amendment",
    "check_guardrail_change",
    "classify_document",
    "parse_footer",
]
```

Then add, after `parse_footer`:

```python
# spec-kit's generated template uses `[UPPER_CASE_WITH_UNDERSCORES]` placeholders throughout
# (`[PROJECT_NAME]`, `[PRINCIPLE_1_NAME]`, `[CONSTITUTION_VERSION]`, …) — a document that still
# carries one was never filled in, whatever its footer claims.
_TEMPLATE_TOKEN_RE = re.compile(r"\[[A-Z][A-Z0-9_ ]*\]")


def classify_document(text: str | None) -> str | None:
    """Governance status of a constitution document's raw text (issue #2165).

    ``None`` means the document looks ratified: present, not the unfilled spec-kit
    template, and its Version/Ratified/Last Amended footer parses. Otherwise one of:

    * ``"missing"`` — no file (``text`` is ``None`` or blank).
    * ``"template"`` — still carries a spec-kit placeholder token like
      ``[PROJECT_NAME]``; whatever else it says is not binding.
    * ``"no_version_footer"`` — present, not a template, but the footer this gate's
      own ``check_amendment`` requires for every amendment does not parse, so the
      document cannot be confirmed ratified.
    """
    if text is None or not text.strip():
        return "missing"
    if _TEMPLATE_TOKEN_RE.search(text):
        return "template"
    if parse_footer(text) is None:
        return "no_version_footer"
    return None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH=$PWD/src /home/tony/GitHub/viberpit/.venv/bin/python -m pytest server/tests/test_constitution_gate.py -v`
Expected: PASS, all tests in the file (not just the new ones — confirm no regression).

- [ ] **Step 5: Add the real-file regression test**

Add one more test, mirroring the existing `test_real_constitution_footer_parses` pattern in the
same file (reuse its `Path(__file__).resolve().parents[2]` approach to reach the repo root):

```python
def test_classify_document_accepts_the_real_constitution():
    """ViberPit's own ratified constitution must never misclassify as a template —
    it contains bracketed prose (e.g. inline citations), so the placeholder regex
    must not be so broad it catches real writing."""
    from pathlib import Path
    text = (Path(__file__).resolve().parents[2] / ".specify/memory/constitution.md").read_text()
    assert classify_document(text) is None
```

Run: `PYTHONPATH=$PWD/src /home/tony/GitHub/viberpit/.venv/bin/python -m pytest server/tests/test_constitution_gate.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add server/sdlc/constitution_gate.py server/tests/test_constitution_gate.py
git commit -m "$(cat <<'EOF'
feat(sdlc): classify a constitution document as missing/template/unratified

Pure classifier for issue #2165 — pm_sync and build_spec_prompt both need to
tell a real, ratified constitution from an absent or unfilled one instead of
assuming any file at the expected path is binding.

Co-Authored-By: Claude <model> <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: `Project.constitution_status` column, migration, mirror_store setter

**Files:**
- Modify: `server/db/models.py`
- Create: `server/db/migrations/versions/0061_project_constitution_status.py`
- Modify: `server/pm/mirror_store.py`
- Test: `server/tests/test_pm_sync_save_constitution_status.py` (create)

**Interfaces:**
- Consumes: nothing from Task 1 directly (the column stores an already-classified status string).
- Produces: `Project.constitution_status: str | None` column; `mirror_store.
  set_project_constitution_status(db: Session, project_id: str, status: str | None) -> None`. Task
  3 calls this setter from the orchestrator seam.

- [ ] **Step 1: Add the model column**

In `server/db/models.py`, inside `class Project`, immediately after the existing
`unindexed_docs` column (so the two "pm_sync's per-pass observation about this product" columns
sit together):

```python
    # pm_sync's current constitution governance status for this product's primary
    # repo (issue #2165): None ⇒ ratified (present, not a template, footer parses);
    # "missing" / "template" / "no_version_footer" ⇒ a Needs-you item, see
    # server/sdlc/constitution_gate.classify_document. Rewritten every
    # `_governance_check` pass so a fixed constitution clears itself. Nullable so
    # reconcile ALTER-ADDs it on both dialects.
    constitution_status: Mapped[str | None] = mapped_column(String, nullable=True)
```

- [ ] **Step 2: Write the migration**

Create `server/db/migrations/versions/0061_project_constitution_status.py`:

```python
"""Persist pm-sync's per-pass constitution governance status onto the project row.

Revision ID: 0061_project_constitution_status
Revises: 0060_backlog_groups_degraded

Nullable string: init_db reconcile adds this on SQLite and Postgres. Existing
rows stay NULL until the next pm-sync pass classifies them.
"""
from alembic import op
import sqlalchemy as sa

revision = "0061_project_constitution_status"
down_revision = "0060_backlog_groups_degraded"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("projects")}
    if "constitution_status" not in columns:
        op.add_column("projects", sa.Column("constitution_status", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("projects", "constitution_status")
```

- [ ] **Step 3: Add the mirror_store setter**

In `server/pm/mirror_store.py`, immediately after `set_project_unindexed_docs`:

```python
def set_project_constitution_status(
    db: Session, project_id: str, status: str | None,
) -> None:
    """Persist pm-sync's current constitution governance status onto the project
    row (issue #2165), so ``roadmap_health`` can surface it without re-reading the
    repo. Rewritten every pass — a fixed constitution clears a previously-flagged
    status the same way a fixed feature doc clears ``unindexed_docs``."""
    row = db.get(Project, project_id)
    if row is not None:
        row.constitution_status = status
```

- [ ] **Step 4: Write the persistence tests**

Create `server/tests/test_pm_sync_save_constitution_status.py`, mirroring
`test_pm_sync_save_unindexed_docs.py` exactly (same `_orch()` helper, same `isolated_db` fixture):

```python
"""`mirror_store.set_project_constitution_status` persists pm-sync's per-pass
governance status onto the project row (issue #2165)."""
from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_set_constitution_status_persists(isolated_db):
    from sqlalchemy import select
    from server.db import db_session
    from server.db.models import Project
    from server.pm.mirror_store import set_project_constitution_status

    async with db_session() as db:
        p = Project(slug="example", name="Example")
        db.add(p)
        await db.flush()
        await db.commit()
        project_id = p.id

    async with db_session() as db:
        await db.run_sync(
            lambda s: set_project_constitution_status(s, project_id, "template"))
        await db.commit()

    async with db_session() as db:
        row = (await db.execute(select(Project).where(Project.id == project_id))).scalar_one()
    assert row.constitution_status == "template"


@pytest.mark.asyncio
async def test_set_constitution_status_none_clears_a_previous_flag(isolated_db):
    from sqlalchemy import select
    from server.db import db_session
    from server.db.models import Project
    from server.pm.mirror_store import set_project_constitution_status

    async with db_session() as db:
        p = Project(slug="example", name="Example", constitution_status="missing")
        db.add(p)
        await db.flush()
        await db.commit()
        project_id = p.id

    async with db_session() as db:
        await db.run_sync(
            lambda s: set_project_constitution_status(s, project_id, None))
        await db.commit()

    async with db_session() as db:
        row = (await db.execute(select(Project).where(Project.id == project_id))).scalar_one()
    assert row.constitution_status is None
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `env -u CCSWITCH_SESSIOND_ADDR -u CCSWITCH_DAEMON_ROLE PYTHONPATH=$PWD/src /home/tony/GitHub/viberpit/.venv/bin/python -m pytest server/tests/test_pm_sync_save_constitution_status.py -v`
Expected: PASS.

- [ ] **Step 6: Confirm reconcile picks up the column with no migration run**

Run: `env -u CCSWITCH_SESSIOND_ADDR -u CCSWITCH_DAEMON_ROLE PYTHONPATH=$PWD/src /home/tony/GitHub/viberpit/.venv/bin/python -m pytest server/tests/test_db_schema_reconcile.py server/tests/test_postgres_compat.py server/tests/test_schema_reconcile_coverage.py -v`
Expected: PASS — a nullable column needs no entry in `_REQUIRED_EXTRAS` or `RECONCILE_EXEMPT`
(those are only for NOT NULL columns), but this confirms nothing else assumes an exhaustive column
list.

- [ ] **Step 7: Commit**

```bash
git add server/db/models.py server/db/migrations/versions/0061_project_constitution_status.py \
        server/pm/mirror_store.py server/tests/test_pm_sync_save_constitution_status.py
git commit -m "$(cat <<'EOF'
feat(pm): add Project.constitution_status for the governance health check

Nullable column + migration + mirror_store setter (issue #2165), mirroring
the unindexed_docs column pm_sync already uses to persist a per-pass
per-primary-repo observation for roadmap_health to surface.

Co-Authored-By: Claude <model> <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: pm_sync reads and classifies the constitution each pass

**Files:**
- Modify: `server/sessions/orchestrator.py`
- Modify: `server/automations/pm_sync.py`
- Test: `server/tests/test_pm_orchestrator_seams.py`
- Test: `server/tests/test_pm_sync_loop.py`

**Interfaces:**
- Consumes: `classify_document` (Task 1), `mirror_store.set_project_constitution_status` (Task 2).
- Produces: `orch._pm_sync_read_constitution(repo: str, ref: str | None = None) -> str | None`;
  `orch._pm_sync_save_constitution_status(project_id: str, status: str | None) -> None`;
  `PmSyncLoop.__init__(..., read_constitution=None, save_constitution_status=None)`. No later task
  depends on these signatures directly — Task 4/5 read the *stored* status via `Project.
  constitution_status`, not through this seam.

- [ ] **Step 1: Write the failing orchestrator-seam tests**

In `server/tests/test_pm_orchestrator_seams.py`, add near the existing
`test_read_feature_docs_returns_all_docs_from_pinned_snapshot` /
`test_read_feature_docs_raises_on_partial_read` tests (reuse the file's `_git`, `_bare_orch`,
`_mk_async` helpers already defined above them):

```python
def _make_repo_with_constitution(root: str, text: str | None) -> str:
    """A real git repo, optionally with `.specify/memory/constitution.md` committed,
    plus `refs/remotes/origin/master` (what `_pm_sync_read_constitution` resolves)."""
    import os
    import subprocess
    clone = os.path.join(root, "clone")
    os.makedirs(clone)
    subprocess.run(["git", "init", "-q", clone], check=True, capture_output=True, text=True)
    _git("config", "user.email", "t@t", cwd=clone)
    _git("config", "user.name", "t", cwd=clone)
    if text is not None:
        os.makedirs(os.path.join(clone, ".specify", "memory"))
        with open(os.path.join(clone, ".specify", "memory", "constitution.md"), "w") as fh:
            fh.write(text)
    else:
        # An empty repo has no commit at all otherwise — need SOME commit for HEAD
        # to resolve, so the "missing file" case is a real commit without the file.
        with open(os.path.join(clone, "README.md"), "w") as fh:
            fh.write("no constitution here\n")
    _git("add", "-A", cwd=clone)
    _git("commit", "-q", "-m", "init", cwd=clone)
    head = subprocess.run(["git", "-C", clone, "rev-parse", "HEAD"],
                          check=True, capture_output=True, text=True).stdout.strip()
    _git("update-ref", "refs/remotes/origin/master", head, cwd=clone)
    return clone


@pytest.mark.asyncio
async def test_read_constitution_returns_text_when_present(db_env):
    orch = _bare_orch()
    orch._effective_repo_root = _mk_async("/tmp/root")
    with tempfile.TemporaryDirectory() as root:
        clone = _make_repo_with_constitution(root, "# Real Constitution\n\nbody\n")
        with patch("ccswitch.gitrepos.ensure_clone", return_value=clone):
            text = await orch._pm_sync_read_constitution("o/r")
    assert text == "# Real Constitution\n\nbody\n"


@pytest.mark.asyncio
async def test_read_constitution_returns_none_when_file_absent(db_env):
    """A repo with no constitution at all is the expected common case (issue
    #2165), never an error — unlike the feature-doc reader's atomic-or-raise
    contract, a single missing file here must not raise."""
    orch = _bare_orch()
    orch._effective_repo_root = _mk_async("/tmp/root")
    with tempfile.TemporaryDirectory() as root:
        clone = _make_repo_with_constitution(root, None)
        with patch("ccswitch.gitrepos.ensure_clone", return_value=clone):
            text = await orch._pm_sync_read_constitution("o/r")
    assert text is None
```

Also add, in `server/tests/test_pm_sync_loop.py`, near its own `_view`/`_loop` helpers, tests for
the new `_governance_check` behavior. First extend the `_loop` helper (in that same file) to accept
and wire the two new optional callables — add these parameters to its signature and body:

```python
def _loop(ctx, views, pages, *, config=None, feature_docs=None, rename_repo=None,
          read_feature_docs=None, read_map_changes=None, refresh_map_changes=None,
          read_constitution=None):
    ...
    constitution_saved = []

    async def read_docs(repo, ref=None):
        return FeatureDocsRead(sha="sha-fake", docs=list(feature_docs or []))

    async def save_constitution(project_id, status):
        constitution_saved.append((project_id, status))

    loop = PmSyncLoop(
        ctx, read_config=read_config, list_repos=list_repos, fetch=fetch,
        upsert_items=upsert, save_sync_state=save,
        read_feature_docs=read_feature_docs or read_docs,
        list_feature_rows=list_rows, apply_feature_plan=apply_plan,
        now_utc=lambda: datetime(2026, 2, 1, tzinfo=timezone.utc),
        save_unindexed_docs=save_unindexed,
        rename_repo=rename_repo, read_map_changes=read_map_changes,
        refresh_map_changes=refresh_map_changes,
        read_constitution=read_constitution, save_constitution_status=save_constitution)
    loop.unindexed_saved = unindexed_saved
    loop.constitution_saved = constitution_saved
    return loop, saved, upserted, applied
```

(Read the actual current body first — insert `save_constitution` next to the existing
`save_unindexed` closure, and add the two new kwargs to the existing `PmSyncLoop(...)` call rather
than retyping the whole call from scratch, so you don't drop any of the other kwargs already
there.)

Then add the test cases (place near whichever existing test exercises `_incremental`/
`_feature_index` against a primary-repo view):

```python
@pytest.mark.asyncio
async def test_incremental_classifies_and_saves_constitution_status():
    ctx = Ctx()
    view = _view()

    async def read_constitution(repo, ref=None):
        return "# Real Constitution\n\n**Version**: 1.0.0 | **Ratified**: 2026-01-01 | **Last Amended**: 2026-01-01"

    loop, saved, upserted, applied = _loop(
        ctx, [view], [_issue_page([], None, False), _pr_page([], None, False)],
        read_constitution=read_constitution)
    await loop._incremental(view, datetime(2026, 2, 1, tzinfo=timezone.utc))
    assert loop.constitution_saved == [("p1", None)]  # ratified ⇒ None


@pytest.mark.asyncio
async def test_incremental_classifies_a_missing_constitution():
    ctx = Ctx()
    view = _view()

    async def read_constitution(repo, ref=None):
        return None

    loop, saved, upserted, applied = _loop(
        ctx, [view], [_issue_page([], None, False), _pr_page([], None, False)],
        read_constitution=read_constitution)
    await loop._incremental(view, datetime(2026, 2, 1, tzinfo=timezone.utc))
    assert loop.constitution_saved == [("p1", "missing")]


@pytest.mark.asyncio
async def test_governance_check_skipped_for_non_primary_repo():
    ctx = Ctx()
    view = _view(is_primary=False)

    async def read_constitution(repo, ref=None):
        raise AssertionError("must not be called for a non-primary repo")

    loop, saved, upserted, applied = _loop(
        ctx, [view], [_issue_page([], None, False), _pr_page([], None, False)],
        read_constitution=read_constitution)
    await loop._incremental(view, datetime(2026, 2, 1, tzinfo=timezone.utc))
    assert loop.constitution_saved == []


@pytest.mark.asyncio
async def test_governance_check_is_best_effort_on_read_failure():
    ctx = Ctx()
    view = _view()

    async def read_constitution(repo, ref=None):
        raise RuntimeError("clone failed")

    loop, saved, upserted, applied = _loop(
        ctx, [view], [_issue_page([], None, False), _pr_page([], None, False)],
        read_constitution=read_constitution)
    await loop._incremental(view, datetime(2026, 2, 1, tzinfo=timezone.utc))
    assert loop.constitution_saved == []  # never saved, but the pass must not raise
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `env -u CCSWITCH_SESSIOND_ADDR -u CCSWITCH_DAEMON_ROLE PYTHONPATH=$PWD/src /home/tony/GitHub/viberpit/.venv/bin/python -m pytest server/tests/test_pm_orchestrator_seams.py -k read_constitution server/tests/test_pm_sync_loop.py -k "constitution or governance" -v`
Expected: FAIL — `AttributeError: '...' object has no attribute '_pm_sync_read_constitution'` /
`TypeError: PmSyncLoop.__init__() got an unexpected keyword argument 'read_constitution'`.

- [ ] **Step 3: Implement the orchestrator seams**

In `server/sessions/orchestrator.py`, add immediately after `_pm_sync_read_feature_docs` (before
`_pm_sync_read_map_changes`):

```python
    _PM_SYNC_CONSTITUTION_PATH = ".specify/memory/constitution.md"

    async def _pm_sync_read_constitution(
        self, repo: str, ref: str | None = None
    ) -> str | None:
        """Read `.specify/memory/constitution.md` at *ref* (default: origin's
        default branch) in the shared clone — same git-plumbing-only approach as
        `_pm_sync_read_feature_docs`, but single-file and best-effort: unlike that
        reader's atomic-or-raise contract (a partial feature-doc read would orphan
        capabilities), a repo with NO constitution at all is the expected common
        case for issue #2165, not a read failure — this returns None rather than
        raising."""
        import subprocess

        async def _read() -> str | None:
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
            show = await asyncio.to_thread(
                subprocess.run,
                ["git", "-C", clone, "show",
                 f"{resolved_ref}:{self._PM_SYNC_CONSTITUTION_PATH}"],
                capture_output=True, text=True, timeout=30,
            )
            if show.returncode != 0:
                return None
            return show.stdout

        return await _read()

    async def _pm_sync_save_constitution_status(
        self, project_id: str, status: str | None,
    ) -> None:
        """Persist this pass's constitution governance status onto the project
        row (issue #2165) — see server/pm/mirror_store.py:set_project_constitution_status."""
        from server.pm import mirror_store
        async with db_session() as db:
            await db.run_sync(
                lambda s: mirror_store.set_project_constitution_status(s, project_id, status))
```

Then wire both into the ONE `PmSyncLoop(...)` construction site (around where
`save_unindexed_docs=self._pm_sync_save_unindexed_docs,` already appears): add, directly below
that line,

```python
            read_constitution=self._pm_sync_read_constitution,
            save_constitution_status=self._pm_sync_save_constitution_status,
```

- [ ] **Step 4: Implement the pm_sync wiring**

In `server/automations/pm_sync.py`:

1. Near the top, alongside the other `Callable[...]` type aliases (e.g. next to
   `SaveUnindexedDocs`'s definition — find it by searching for that name), add:

```python
ReadConstitution = Callable[..., Awaitable[str | None]]
SaveConstitutionStatus = Callable[[str, str | None], Awaitable[None]]
```

2. In `PmSyncLoop.__init__`, add two new keyword-only parameters right after
   `save_unindexed_docs: SaveUnindexedDocs | None = None,`:

```python
        read_constitution: ReadConstitution | None = None,
        save_constitution_status: SaveConstitutionStatus | None = None,
```

and in the body, right after `self._save_unindexed_docs = save_unindexed_docs`:

```python
        # None ⇒ no governance status pass (issue #2165) — pm_sync predates this
        # check and every test that doesn't care about it should keep working.
        self._read_constitution = read_constitution
        self._save_constitution_status = save_constitution_status
```

3. Add the import at the top of the file (alongside the module's other `server.sdlc`-style
   imports, or as a local import inside the new method if the module avoids top-level
   cross-package imports — check the existing import block first and match its style):

```python
from server.sdlc.constitution_gate import classify_document
```

4. Add the new method, right after `_feature_index` (after its closing statement, before
   `_stash`):

```python
    async def _governance_check(self, view: RepoSyncView) -> None:
        """Best-effort per-primary-repo constitution status (issue #2165): read
        `.specify/memory/constitution.md` off the default branch and store its
        classified status onto the project row so `roadmap_health` can surface a
        Needs-you item without re-reading the repo. A read failure must never
        stall the work-item mirror — it just skips this pass, leaving whatever
        status the last successful pass recorded in place."""
        if (not view.is_primary or self._read_constitution is None
                or self._save_constitution_status is None):
            return
        try:
            text = await self._read_constitution(view.repo)
        except Exception:
            self._observation_degraded = True
            log.warning("pm_sync.constitution_read_failed repo=%s",
                        view.repo, exc_info=True)
            return
        await self._save_constitution_status(view.project_id, classify_document(text))
```

5. Add a call to `self._governance_check(view)` right after each of the two existing
   `await self._feature_index(view)` calls (one inside the backfill-completion branch, one inside
   `_incremental`) — same line, same indentation as the `_feature_index` call above it.

- [ ] **Step 5: Run tests to verify they pass**

Run: `env -u CCSWITCH_SESSIOND_ADDR -u CCSWITCH_DAEMON_ROLE PYTHONPATH=$PWD/src /home/tony/GitHub/viberpit/.venv/bin/python -m pytest server/tests/test_pm_orchestrator_seams.py server/tests/test_pm_sync_loop.py server/tests/test_pm_sync_projects.py server/tests/test_pm_sync_save_constitution_status.py server/tests/test_pm_sync_save_unindexed_docs.py -v`
Expected: PASS — including every pre-existing test in these files (the new optional kwargs must
default to `None` and change nothing for a caller that omits them).

- [ ] **Step 6: Commit**

```bash
git add server/sessions/orchestrator.py server/automations/pm_sync.py \
        server/tests/test_pm_orchestrator_seams.py server/tests/test_pm_sync_loop.py
git commit -m "$(cat <<'EOF'
feat(pm-sync): classify and store the primary repo's constitution status

Reads .specify/memory/constitution.md off the shared clone each primary-repo
pass (same git-plumbing approach as the feature-doc reader) and stores its
classify_document() status — best-effort, a missing file is the expected
common case, never a read failure (issue #2165).

Co-Authored-By: Claude <model> <noreply@anthropic.com>
EOF
)"
```

---

### Task 4: Surface governance on `roadmap_health` and the product overview

**Files:**
- Modify: `server/pm/health.py`
- Modify: `server/tests/test_pm_health.py`
- Modify: `server/api/pm.py`
- Modify: `web/src/api/pm.ts`
- Modify: `web/src/features/roadmap/detail/RoadmapHealthCard.tsx`
- Modify: `web/src/features/roadmap/detail/RoadmapHealthCard.test.tsx`

**Interfaces:**
- Consumes: `Project.constitution_status` (Task 2).
- Produces: `RoadmapHealth.governance: str | None`; API field `"governance"` on
  `GET /pm/projects/{slug}/health`; frontend `ProjectHealth.governance: string | null`.

- [ ] **Step 1: Write the failing health tests**

Add to `server/tests/test_pm_health.py`:

```python
def test_health_passes_through_governance_status():
    h = roadmap_health(capabilities=[], initiatives=[], proposals=[], now=NOW,
                       governance="template")
    assert h.governance == "template"


def test_health_governance_defaults_to_none():
    h = roadmap_health(capabilities=[], initiatives=[], proposals=[], now=NOW)
    assert h.governance is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `env -u CCSWITCH_SESSIOND_ADDR -u CCSWITCH_DAEMON_ROLE PYTHONPATH=$PWD/src /home/tony/GitHub/viberpit/.venv/bin/python -m pytest server/tests/test_pm_health.py -k governance -v`
Expected: FAIL with `TypeError: roadmap_health() got an unexpected keyword argument 'governance'`.

- [ ] **Step 3: Implement the health.py change**

In `server/pm/health.py`:

```python
@dataclass(frozen=True)
class RoadmapHealth:
    unparented_capabilities: tuple[str, ...]
    unlinked_initiatives: tuple[str, ...]
    stale_proposed: tuple[str, ...]
    stale_inbox: int
    unindexed_docs: tuple[tuple[str, str], ...] = ()
    # pm_sync's classified constitution status for the product's primary repo
    # (issue #2165) — None/"missing"/"template"/"no_version_footer", passed
    # through unchanged from Project.constitution_status; see
    # server/sdlc/constitution_gate.classify_document for what each value means.
    governance: str | None = None


def roadmap_health(*, capabilities: Iterable[tuple[str, str | None, str | None, datetime | None]],
                   initiatives: Iterable[tuple[str, int, bool, bool]],
                   proposals: Iterable[datetime | None],
                   now: datetime, stale_days: int = 30, inbox_days: int = 7,
                   unindexed_docs: Iterable[tuple[str, str]] = (),
                   governance: str | None = None) -> RoadmapHealth:
    caps = list(capabilities)
    has_child = {p for _, p, _, _ in caps if p}
    tree_in_use = any(p is None and s in has_child for s, p, _, _ in caps)
    unparented = tuple(sorted(s for s, p, _, _ in caps
                              if tree_in_use and p is None and s not in has_child))
    unlinked = tuple(sorted(s for s, n, prog, done in initiatives if n == 0 and not prog and not done))
    cutoff = now - timedelta(days=stale_days)
    stale = tuple(sorted(s for s, _, st, upd in caps
                         if st == "proposed" and upd is not None and _aware(upd) < cutoff))
    inbox_cutoff = now - timedelta(days=inbox_days)
    stale_inbox = sum(1 for c in proposals if c is not None and _aware(c) < inbox_cutoff)
    unindexed = tuple(sorted(unindexed_docs))
    return RoadmapHealth(unparented, unlinked, stale, stale_inbox, unindexed, governance)
```

(Only the dataclass field, the new kwarg, and the final `return` line actually change — the rest of
the function body is shown for context; edit in place rather than retyping the whole file.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `env -u CCSWITCH_SESSIOND_ADDR -u CCSWITCH_DAEMON_ROLE PYTHONPATH=$PWD/src /home/tony/GitHub/viberpit/.venv/bin/python -m pytest server/tests/test_pm_health.py -v`
Expected: PASS, all tests in the file.

- [ ] **Step 5: Wire the API handler**

In `server/api/pm.py`, inside `project_health` (find the existing lines building `unindexed` and
calling `roadmap_health`):

```python
    unindexed = [(d.get("path", ""), d.get("error", "")) for d in (p.unindexed_docs or [])]
    h = roadmap_health(capabilities=caps,
                       initiatives=[(i.slug or "", link_counts.get(i.id, 0), i.id in parents_with_kids,
                                    i.stage == "done" or i.status == "done") for i in inits],
                       proposals=props, now=now, unindexed_docs=unindexed,
                       governance=p.constitution_status)
    return {"unparentedCapabilities": list(h.unparented_capabilities),
            "unlinkedInitiatives": list(h.unlinked_initiatives),
            "staleProposed": list(h.stale_proposed), "staleInbox": h.stale_inbox,
            "unindexedDocs": [{"path": path, "error": error} for path, error in h.unindexed_docs],
            "governance": h.governance,
            "generatedAt": now.isoformat()}
```

- [ ] **Step 6: Write a failing API test**

Find the existing test(s) for `GET /pm/projects/{slug}/health` (search `server/tests/` for
`"/health"` inside a pm-related test file — likely `test_pm_health_api.py` or inside
`test_pm_api.py`; read whichever file to find the exact fixture/client pattern used, then add):

```python
@pytest.mark.asyncio
async def test_project_health_surfaces_governance_status(isolated_db, ...):  # match existing fixture args
    # ... build a Project with constitution_status="missing" using the file's existing
    # project-creation helper, then GET the health endpoint and assert
    # resp.json()["governance"] == "missing"
```

(Write this test by copying the shape of whatever existing health-endpoint test is in that file —
same client/auth fixtures, same project-creation helper — and add one assertion for the new field.
Do not invent a fixture pattern the file doesn't already use.)

- [ ] **Step 7: Run the API test**

Run: `env -u CCSWITCH_SESSIOND_ADDR -u CCSWITCH_DAEMON_ROLE PYTHONPATH=$PWD/src /home/tony/GitHub/viberpit/.venv/bin/python -m pytest <the file from step 6> -k governance -v`
Expected: PASS.

- [ ] **Step 8: Wire the frontend type**

In `web/src/api/pm.ts`:

```typescript
export interface ProjectHealth {
  unparentedCapabilities: string[]
  unlinkedInitiatives: string[]
  staleProposed: string[]
  staleInbox: number
  unindexedDocs: UnindexedDoc[]
  governance: string | null
  generatedAt: string
}
```

- [ ] **Step 9: Write the failing frontend tests**

In `web/src/features/roadmap/detail/RoadmapHealthCard.test.tsx`, add two cases:

```typescript
  it('surfaces a missing constitution as a governance bullet', () => {
    Object.assign(health, {
      data: {
        unparentedCapabilities: [],
        unlinkedInitiatives: [],
        staleProposed: [],
        staleInbox: 0,
        unindexedDocs: [],
        governance: 'missing',
        generatedAt: 'x',
      },
    })
    mount()
    expect(screen.getByText('Roadmap hygiene')).toBeInTheDocument()
    expect(screen.getByText(/no ratified constitution/i)).toBeInTheDocument()
  })

  it('stays hidden when governance is null and everything else is empty', () => {
    Object.assign(health, {
      data: {
        unparentedCapabilities: [],
        unlinkedInitiatives: [],
        staleProposed: [],
        staleInbox: 0,
        unindexedDocs: [],
        governance: null,
        generatedAt: 'x',
      },
    })
    const { container } = mount()
    expect(container.firstChild).toBeNull()
  })
```

- [ ] **Step 10: Run the frontend tests to verify they fail**

Run: `(cd web && npx vitest run src/features/roadmap/detail/RoadmapHealthCard.test.tsx)`
Expected: FAIL — the "missing constitution" case finds no matching text; the "stays hidden" case
already passes (governance isn't checked yet) so only the first new case should fail.

- [ ] **Step 11: Implement the RoadmapHealthCard bullet**

In `web/src/features/roadmap/detail/RoadmapHealthCard.tsx`:

```typescript
const GOVERNANCE_MESSAGE: Record<string, string> = {
  missing: 'no ratified constitution — spec sessions have nothing to check governance against',
  template: 'the constitution is still the unfilled template — spec sessions have nothing real to check against',
  no_version_footer: "the constitution's Version / Ratified / Last Amended footer doesn't parse, so it can't be confirmed ratified",
}

export function RoadmapHealthCard({ productSlug }: { productSlug: string }) {
  const { data: health } = useProjectHealth(productSlug)
  if (!health) return null

  const { unparentedCapabilities, unlinkedInitiatives, staleProposed, staleInbox, unindexedDocs, governance } = health
  const nothingToSay =
    unparentedCapabilities.length === 0 && unlinkedInitiatives.length === 0 &&
    staleProposed.length === 0 && staleInbox === 0 && unindexedDocs.length === 0 && !governance
  if (nothingToSay) return null

  const capPath = (slug: string) => capabilityPath(productSlug, slug)

  return (
    <Banner tone="warning" title="Roadmap hygiene">
      <ul className="flex flex-col gap-0.5">
        {governance && (
          <li>{GOVERNANCE_MESSAGE[governance] ?? `governance issue: ${governance}`}</li>
        )}
        {unparentedCapabilities.length > 0 && (
          <li>
            {unparentedCapabilities.length} {unparentedCapabilities.length === 1 ? 'capability' : 'capabilities'} outside the tree: {' '}
            <SlugLinks slugs={unparentedCapabilities} path={capPath} />
          </li>
        )}
        {unlinkedInitiatives.length > 0 && (
          <li>
            {unlinkedInitiatives.length} leaf {unlinkedInitiatives.length === 1 ? 'initiative' : 'initiatives'} with no capability: {' '}
            <SlugLinks slugs={unlinkedInitiatives} path={initiativePath} />
          </li>
        )}
        {staleProposed.length > 0 && (
          <li>
            {staleProposed.length} stale proposed {staleProposed.length === 1 ? 'capability' : 'capabilities'}: {' '}
            <SlugLinks slugs={staleProposed} path={capPath} />
          </li>
        )}
        {staleInbox > 0 && (
          <li>{staleInbox} inbox {staleInbox === 1 ? 'item' : 'items'} older than 7 days</li>
        )}
        {unindexedDocs.length > 0 && (
          <li>
            {unindexedDocs.length} feature {unindexedDocs.length === 1 ? 'doc' : 'docs'} could not be parsed and {unindexedDocs.length === 1 ? "isn't" : "aren't"} on the map: {' '}
            {unindexedDocs.map((d, i) => (
              <Fragment key={d.path}>
                {i > 0 && ', '}
                <span title={d.error}>{d.path}</span>
              </Fragment>
            ))}
          </li>
        )}
      </ul>
    </Banner>
  )
}
```

(Only the destructuring line, `nothingToSay`, the new `GOVERNANCE_MESSAGE` map, and the new `<li>`
are additions — the rest is unchanged; edit in place.)

- [ ] **Step 12: Run the frontend tests to verify they pass**

Run: `(cd web && npx vitest run src/features/roadmap/detail/RoadmapHealthCard.test.tsx)`
Expected: PASS, every test in the file (including the four pre-existing ones — none of them set
`governance`, so it is `undefined`, which is falsy and changes nothing for them).

- [ ] **Step 13: Run the full web test suite**

Run: `(cd web && npm run test)`
Expected: PASS. Per the project's own testing note, ~47 web test files mock `@/api/hooks` by hand
— running only the changed file would miss a break in one of those.

- [ ] **Step 14: Typecheck**

Run: `(cd web && npm run typecheck)`
Expected: PASS.

- [ ] **Step 15: Commit**

```bash
git add server/pm/health.py server/tests/test_pm_health.py server/api/pm.py \
        server/tests/<the health-endpoint test file from step 6> \
        web/src/api/pm.ts web/src/features/roadmap/detail/RoadmapHealthCard.tsx \
        web/src/features/roadmap/detail/RoadmapHealthCard.test.tsx
git commit -m "$(cat <<'EOF'
feat(pm): surface missing/template constitution as a roadmap-hygiene item

roadmap_health, the health API, and RoadmapHealthCard now report a product's
constitution governance status as a Needs-you-style warning bullet instead
of silently treating an absent or unfilled constitution as fine (issue
#2165).

Co-Authored-By: Claude <model> <noreply@anthropic.com>
EOF
)"
```

---

### Task 5: `build_spec_prompt` names the real constitution path, or the real gap

**Files:**
- Modify: `server/sdlc/prompts.py`
- Modify: `server/sessions/orchestrator.py` (`_sdlc_spawn_synth`)
- Test: `server/tests/test_sdlc_prompts.py`

**Interfaces:**
- Consumes: `classify_document` (Task 1).
- Produces: `build_spec_prompt(..., constitution: str | None = None, constitution_path: str =
  ".specify/memory/constitution.md")`. Default matches the current no-argument behavior's shape
  closely enough that every existing caller (which passes neither kwarg) keeps working, but see
  Step 3 — the default fallback text changes even with no kwargs passed, and the Review Focus item
  for this task pins that the *existing* invariant tests still pass despite the wording change.

- [ ] **Step 1: Write the failing prompt tests**

In `server/tests/test_sdlc_prompts.py`, add (near the existing spec-prompt tests — read the file
first to match its import style, e.g. `from server.sdlc.prompts import build_spec_prompt`):

```python
def test_spec_prompt_names_the_real_path_and_principles_when_ratified():
    text = build_spec_prompt(
        "intent body", [], slug="s", repo="o/r",
        constitution=(
            "# Constitution\n\n### I. First Principle\n\n### II. Second Principle\n\n"
            "**Version**: 1.0.0 | **Ratified**: 2026-01-01 | **Last Amended**: 2026-01-01"
        ),
        constitution_path=".specify/memory/constitution.md",
    )
    assert ".specify/memory/constitution.md" in text
    assert "I. First Principle" in text
    assert "II. Second Principle" in text
    assert "apply the repository constitution" not in text.lower() or "principles:" in text


def test_spec_prompt_flags_a_missing_constitution_instead_of_assuming_one():
    text = build_spec_prompt("intent body", [], slug="s", repo="o/r", constitution=None)
    assert "no constitution could be read" in text or "constitution" in text.lower()


def test_spec_prompt_flags_a_template_constitution():
    template = (
        "# [PROJECT_NAME] Constitution\n\n### [PRINCIPLE_1_NAME]\n\n"
        "**Version**: [CONSTITUTION_VERSION] | **Ratified**: [RATIFICATION_DATE] | "
        "**Last Amended**: [LAST_AMENDED_DATE]"
    )
    text = build_spec_prompt("intent body", [], slug="s", repo="o/r", constitution=template)
    assert "unfilled" in text.lower() or "template" in text.lower()
    assert "flag" in text.lower()


def test_spec_prompt_with_skills_ignores_constitution_entirely():
    """Org skills, when present, are still the binding constraint — the
    constitution block is only the fallback for when there are none."""
    text = build_spec_prompt(
        "intent body", ["skill-a"], slug="s", repo="o/r",
        constitution="# Constitution\n\n### I. P\n\n**Version**: 1.0.0 | **Ratified**: 2026-01-01 | **Last Amended**: 2026-01-01")
    assert "skill-a" in text
    assert "I. P" not in text
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `env -u CCSWITCH_SESSIOND_ADDR -u CCSWITCH_DAEMON_ROLE PYTHONPATH=$PWD/src /home/tony/GitHub/viberpit/.venv/bin/python -m pytest server/tests/test_sdlc_prompts.py -k spec_prompt_names or spec_prompt_flags or spec_prompt_with_skills_ignores -v`
Expected: FAIL with `TypeError: build_spec_prompt() got an unexpected keyword argument 'constitution'`.

- [ ] **Step 3: Implement the constitution-aware fallback**

In `server/sdlc/prompts.py`, add near the top (after the existing imports, before
`_common_instructions`):

```python
import re

from server.sdlc.constitution_gate import classify_document

_PRINCIPLE_HEADING_RE = re.compile(r"^###\s+(.+)$", re.MULTILINE)


def _constitution_block(constitution: str | None, constitution_path: str) -> str:
    """The org-skills fallback bullet for `build_spec_prompt` (issue #2165):
    names the real constitution path and its actual principle headings when one
    is ratified, or an explicit governance-gap instruction — flagged the same
    way the surrounding prompt already asks policy concerns to be flagged —
    when it is absent, still the unfilled spec-kit template, or its footer
    doesn't parse. Never blindly says "apply the repository constitution"
    without knowing whether that instruction means anything."""
    if constitution is None:
        return (f"- (no org skills supplied — no constitution could be read at "
                f"`{constitution_path}`; flag the absence of governance as a spec constraint)")
    status = classify_document(constitution)
    if status == "missing":
        return (f"- (no org skills supplied — `{constitution_path}` does not exist in this "
                "repo; there is no ratified constitution to apply. Flag the absence of "
                "governance as a spec constraint.)")
    if status == "template":
        return (f"- (no org skills supplied — `{constitution_path}` is still the unfilled "
                "spec-kit template, not a ratified constitution. Do not treat its placeholder "
                "text as binding; flag the missing constitution as a spec constraint.)")
    if status == "no_version_footer":
        return (f"- (no org skills supplied — `{constitution_path}` exists but its Version / "
                "Ratified / Last Amended footer does not parse, so it cannot be confirmed "
                "ratified. Flag this as a spec constraint.)")
    headings = _PRINCIPLE_HEADING_RE.findall(constitution)
    heading_list = "\n".join(f"  - {h.strip()}" for h in headings) or "  - (no principle headings found)"
    return (f"- apply the repository constitution at `{constitution_path}`, principles:\n"
            f"{heading_list}")
```

Then change `build_spec_prompt`'s signature and body:

```python
def build_spec_prompt(
    intent_md: str,
    skills: list[str],
    *,
    slug: str,
    repo: str = "",
    default_branch: str = "master",
    engine: AgentEngine | str = AgentEngine.CLAUDE_CODE,
    constitution: str | None = None,
    constitution_path: str = ".specify/memory/constitution.md",
) -> str:
    """Seed prompt for the Synthesizer (Design): ``intent.md`` → ``spec.md``.

    The org ``skills`` are injected as binding constraints, and the session must
    explicitly flag any policy concerns as spec constraints. When no org skills
    are supplied, *constitution* — the raw committed text of *constitution_path*,
    or None when it could not be read — decides the fallback: a ratified
    constitution's real path and principle headings, or an explicit
    governance-gap instruction (issue #2165).
    """
    if skills:
        skills_block = "\n".join(f"- {name}" for name in skills)
    else:
        skills_block = _constitution_block(constitution, constitution_path)
    return (
        "You are running the **Design** stage of an AI-native SDLC loop. Turn the "
        "approved intent below into a specification.\n\n"
        "UPSTREAM INTENT (read-only — do not edit intent.md):\n"
        "-----8<----- intent.md -----8<-----\n"
        f"{intent_md}\n"
        "-----8<----------------------8<-----\n\n"
        "ORG SKILLS — treat each as a binding constraint the spec must satisfy:\n"
        f"{skills_block}\n\n"
        f"Produce `{_artifact_path(slug, 'spec')}` covering: functional behavior, "
        "acceptance criteria, data/API surface, and non-goals. Explicitly **flag "
        "any policy concerns** (security, governance, ToS, data handling) as called-"
        "out spec constraints so a human can resolve them at the gate.\n\n"
        + _common_instructions(slug, "spec", repo, default_branch, engine)
    )
```

(The body's return statement is unchanged from the current file — only the signature and the
`skills_block` else-branch change. Edit in place.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `env -u CCSWITCH_SESSIOND_ADDR -u CCSWITCH_DAEMON_ROLE PYTHONPATH=$PWD/src /home/tony/GitHub/viberpit/.venv/bin/python -m pytest server/tests/test_sdlc_prompts.py server/tests/test_sdlc_codex_acceptance.py server/tests/test_sdlc_codex_live_artifact.py -v`
Expected: PASS, every test in all three files — these other two files also call
`build_spec_prompt` and must keep working with the new optional kwargs defaulted away.

- [ ] **Step 5: Wire the orchestrator spawn seam**

In `server/sessions/orchestrator.py`, inside `_sdlc_spawn_synth`'s nested `build(cwd,
default_branch)` closure:

```python
        def build(cwd: str, default_branch: str) -> str:
            content = self._sdlc_show_committed_file(
                cwd, f"origin/{default_branch}", upstream)
            if kind == "spec":
                const_path = ".specify/memory/constitution.md"
                const_text = self._sdlc_show_committed_file(
                    cwd, f"origin/{default_branch}", const_path)
                if const_text.startswith(_SDLC_UNREADABLE_PREFIX):
                    const_text = None
                return build_spec_prompt(
                    content, [], slug=slug, repo=repo, default_branch=default_branch,
                    engine=engine, constitution=const_text, constitution_path=const_path)
            return build_plan_prompt(
                content, slug=slug, repo=repo, default_branch=default_branch,
                engine=engine)
```

(Only the `if kind == "spec":` branch changes — the `build_plan_prompt` branch is untouched. Edit
in place; `_SDLC_UNREADABLE_PREFIX` is already a module-level constant in this file, no new import
needed.)

- [ ] **Step 6: Write a failing orchestrator test for the spawn seam**

Search `server/tests/` for the existing test(s) that exercise `_sdlc_spawn_synth` with `kind="spec"`
(likely in `test_pm_orchestrator_seams.py`, `test_sdlc_*.py`, or a dedicated spawn-seam test file —
`grep -rn "_sdlc_spawn_synth" server/tests/`). Read whichever file it is in, then add a case
alongside the existing ones that patches `_sdlc_show_committed_file` (or the underlying clone/git
mechanism the existing tests already fake) to answer a real constitution for
`.specify/memory/constitution.md`, and asserts the built prompt sent to the spawned session
contains the constitution's principle heading text rather than the bare fallback phrase. Match
whatever mocking style that file's existing `_sdlc_spawn_synth` tests already use — do not
introduce a new one.

- [ ] **Step 7: Run tests to verify they pass**

Run: `env -u CCSWITCH_SESSIOND_ADDR -u CCSWITCH_DAEMON_ROLE PYTHONPATH=$PWD/src /home/tony/GitHub/viberpit/.venv/bin/python -m pytest <the file from step 6> -v`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add server/sdlc/prompts.py server/sessions/orchestrator.py server/tests/test_sdlc_prompts.py \
        <the file from step 6>
git commit -m "$(cat <<'EOF'
feat(sdlc): name the real constitution path/principles in the spec prompt

build_spec_prompt's "no org skills supplied" fallback no longer blindly says
"apply the repository constitution" — it names the real expected path and
extracted principle headings when one is ratified, or an explicit
governance-gap instruction to flag as a spec constraint when the file is
absent, still the unfilled template, or its footer doesn't parse (issue
#2165, item 3).

Co-Authored-By: Claude <model> <noreply@anthropic.com>
EOF
)"
```

---

## Scope note: issue items 2 and 4

Item 4 ("re-use `constitution_gate.check_amendment` for any onboarded repo's constitution PRs, not
just ViberPit's") needs **no new code**: `check_amendment`/`check_guardrail_change` already take
plain `base_text`/`head_text`/`pr_body`/`changed_paths` strings with no ViberPit-specific path or
repo reference baked in (confirmed by reading `server/sdlc/constitution_gate.py` in full — the
ViberPit-only part is entirely in the *caller*, `scripts/check_constitution_amendment.py`, which
reads local `git`/disk because it only ever runs in ViberPit's own CI). They are already reusable
as-is by any future caller that resolves base/head file text and PR body for an onboarded repo's
own constitution PR (e.g. via the GitHub API `GhWrite.read_file` seam in
`server/pm/map_changes_gh.py`, which already reads arbitrary files from arbitrary `owner/repo`
strings). Note this in the PR body rather than adding an unused wrapper function with no caller —
that would be dead code with nothing exercising it.

Item 2 ("Onboarding: after `map_merged`, offer a 'propose constitution' session … as a non-draft PR
behind the usual human gate, same shape as the map-synthesis PR") is a full parallel session-spawn
subsystem — a new `Automation` registry entry, a new pure prompt builder mirroring
`build_map_synthesis_prompt`, and a new orchestrator spawn seam mirroring `pm_onboard_spawn_map`
(three pieces, ~400 lines across `server/automations/`, `server/pm/`, and
`server/sessions/orchestrator.py`, per the existing map-synthesis flow this would mirror). It is
enough independent surface area — with its own engine-policy, dedupe-key, and account-resolution
concerns — to deserve its own plan and its own review cycle rather than being folded into this PR
as a sixth task riding on the same branch. File a follow-up issue for it during PR creation (its
body should link back to #2165 and name `map_merged` in `server/pm/onboarding.py` as the hook point,
`build_map_synthesis_prompt`/`pm_onboard_spawn_map` as the shape to mirror, and `Project.
constitution_status` — from this plan's Task 2 — as the signal that decides whether to offer it).
Reference the new issue number with `Refs #<n>` in this PR's body; `Fixes #2165` still applies
because this PR resolves the issue's core ask (spec sessions are told the truth about governance
before they run) even though the fully-automated remediation flow ships separately.

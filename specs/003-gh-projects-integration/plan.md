# 003 Phase 0 + Phase A Implementation Plan — Verification Spike & Read-Only Mirror

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Verify GitHub's Projects v2 / sub-issue / dependency APIs against the real platform
(Phase 0), then mirror Projects v2 items, native dependency edges, and sub-issue structure into
ccswitch read-model tables with a drift-audit report and dependency rendering in the UI (Phase A).

**Architecture:** Phase 0 is a throwaway-by-design spike script producing a findings doc
(constitution III). Phase A extends the existing seam-injected `PmSyncLoop` with pure GraphQL
builders/parsers (`server/pm/gh_projects.py`), a pure dep planner (`server/pm/dep_plan.py`),
session-level persistence helpers (`server/pm/mirror_store.py`), four new mirror tables, one API
endpoint, and a `DependencyPanel` in the web PM tab. Zero GitHub writes in either phase.

**Tech Stack:** Python 3 / FastAPI / SQLAlchemy 2.x (`Mapped[]`, tz-aware UTC datetimes) ·
Alembic revisions (record only; reconcile applies) · GitHub GraphQL via `gh api graphql` ·
React + TanStack Query + vitest.

**Spec:** `specs/003-gh-projects-integration/spec.md` (decisions D1–D6 are settled — do not
re-litigate them during execution).

## Global Constraints

- **Branching:** `git fetch` then branch off `origin/master` — NEVER local `master` (stale in
  worktrees). Three PRs: `spike/003-phase0` → `feat/003-phase-a-mirror` → `feat/003-phase-a-deps-ui`
  (the last branches off `origin/master` only after the mirror PR merges).
- **Commit trailer:** end every commit message with
  `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`; PR bodies end with the Claude Code
  trailer. Write commit messages / PR bodies to a file and use `-F`/`--body-file` (the
  prod-deploy-gate hook substring-matches command text).
- **Every completed PR:** open non-draft, then `gh pr merge <#> --squash --auto`.
- **Test env (worktrees have no venv):** run server tests as
  `env -u CCSWITCH_TAILNET_ALLOWLIST -u CCSWITCH_SESSIOND_ENDPOINT -u CCSWITCH_SESSIOND_CLIENT_CERT \
  CCSWITCH_DEV=1 /home/tony/GitHub/ccswitch/.venv/bin/python -m pytest server/tests/...`. Web tests
  need `web/node_modules` symlinked from the main checkout; run web commands in `(cd web && …)`
  subshells — never persistently `cd web` (hook lockout).
- **The full lint-and-test gate is four steps:** `ruff check src/ server/`,
  `mypy server/ --ignore-missing-imports`, `pytest tests/`, `pytest server/tests/` — run all four
  before every push. Web gate: FULL `vitest run` (never changed-file-only), `tsc --noEmit`, `eslint`.
- **Schema rules:** all new columns nullable (reconcile ALTER-ADDs them on both dialects); new
  tables get an Alembic revision chained off head `0017_deploy_state`; new models require checking
  the two hand-maintained Postgres lists (`_REQUIRED_EXTRAS` in
  `server/tests/test_postgres_compat.py`, `RECONCILE_EXEMPT` in
  `server/tests/test_schema_reconcile_coverage.py`) and a local `CCSWITCH_TEST_PG_URL` run (CI
  skips it).
- **No parallel implementations:** dependency-ref parsing lives in
  `server/automations/backlog_deps.py` (`parse_body_dep_refs`, `parse_checklist_refs`) — import it,
  never copy it. Canonical ref format is `"owner/repo#N"`.
- **GitHub reads via GraphQL**, mutations via `gh` REST/CLI only where GraphQL has no path.
- **No GitHub writes anywhere in this plan** except the Phase 0 spike's scratch repo/Project,
  which is created and deleted inside the spike.

---

## Phase 0 — Verification Spike

Constitution III: verify against the real API before implementation. The spike script is
committed as the reviewable evidence record; it is not production code, gets no unit tests, and
must not be imported by `server/`.

### Task 1: Spike script — auth & read probes

**Files:**
- Create: `scripts/spike_gh_projects_v2.py`

**Interfaces:**
- Produces: a runnable CLI (`auth`, `introspect` subcommands) whose raw output Task 3 pastes into
  the findings doc. No code interfaces consumed by later tasks.

- [ ] **Step 1: Write the script with `auth` and `introspect` probes**

```python
#!/usr/bin/env python3
"""Phase 0 spike for spec 003: verify Projects v2 / sub-issue / dependency APIs.

Throwaway verification harness (constitution III). Never imported by server/.
Usage:
    python scripts/spike_gh_projects_v2.py auth
    python scripts/spike_gh_projects_v2.py introspect
    python scripts/spike_gh_projects_v2.py mutations   # Task 2
"""
from __future__ import annotations

import json
import subprocess
import sys

OWNER = "steiner385"


def gh_graphql(query: str, token: str | None = None, **fields: str) -> dict:
    cmd = ["gh", "api", "graphql", "-f", f"query={query}"]
    for key, value in fields.items():
        cmd += ["-f", f"{key}={value}"]
    env = None
    if token:
        import os
        env = dict(os.environ, GH_TOKEN=token)
    proc = subprocess.run(cmd, capture_output=True, text=True, env=env)
    out = json.loads(proc.stdout or "{}")
    if proc.returncode != 0 or out.get("errors"):
        out["_spike_stderr"] = proc.stderr.strip()
    return out


def app_token() -> str | None:
    sys.path.insert(0, "server")
    try:
        from github.app_auth import token_for_owner
        return token_for_owner(OWNER)
    except Exception as exc:  # noqa: BLE001 - spike reports, never hides
        print(f"app token unavailable: {exc!r}")
        return None


PROJECTS_READ = (
    'query { user(login: "%s") { projectsV2(first: 20) '
    "{ totalCount nodes { id number title closed updatedAt } } } }" % OWNER
)


def probe_auth() -> None:
    print("=== ambient gh credential (user) ===")
    print(json.dumps(gh_graphql(PROJECTS_READ), indent=2))
    print("=== scopes of ambient credential ===")
    subprocess.run(["gh", "auth", "status"])
    tok = app_token()
    print("=== GitHub App installation token ===")
    if tok is None:
        print("NO APP TOKEN — record as finding")
    else:
        print(json.dumps(gh_graphql(PROJECTS_READ, token=tok), indent=2))


INTROSPECT_MUTATIONS = """
query { __schema { mutationType { fields { name } } } }
"""

INTROSPECT_ITEMS_ARGS = """
query { __type(name: "ProjectV2") { fields { name args { name type { name kind } } } } }
"""

INTROSPECT_ISSUE_FIELDS = """
query { __type(name: "Issue") { fields { name } } }
"""


def probe_introspect() -> None:
    muts = gh_graphql(INTROSPECT_MUTATIONS)
    names = [f["name"] for f in muts["data"]["__schema"]["mutationType"]["fields"]]
    interesting = [n for n in names if any(
        k in n.lower() for k in ("depend", "block", "subissue", "sub_issue", "projectv2"))]
    print("=== mutation names (dependency/sub-issue/projectV2) ===")
    print(json.dumps(sorted(interesting), indent=2))
    print("=== ProjectV2 field args (delta-read check: does items() take a filter?) ===")
    print(json.dumps(gh_graphql(INTROSPECT_ITEMS_ARGS), indent=2))
    print("=== Issue fields (blockedBy / blocking / parent / subIssues present?) ===")
    issue = gh_graphql(INTROSPECT_ISSUE_FIELDS)
    fields = [f["name"] for f in issue["data"]["__type"]["fields"]]
    print(json.dumps(sorted(f for f in fields if any(
        k in f.lower() for k in ("block", "parent", "sub"))), indent=2))


def main() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "auth"
    {"auth": probe_auth, "introspect": probe_introspect,
     "mutations": probe_mutations}[cmd]()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

(`probe_mutations` is added in Task 2 — until then run only `auth`/`introspect`.)

- [ ] **Step 2: Run the auth probe and save output**

Run (from repo root, where the server env vars for the GitHub App are available):
`python scripts/spike_gh_projects_v2.py auth 2>&1 | tee /tmp/spike-003-auth.log`

Expected: the ambient-credential read returns the user's Projects v2 list (or an
`INSUFFICIENT_SCOPES` error naming the missing `read:project` scope — that is itself a finding).
The App-token read either returns the same list or a `FORBIDDEN`/`NOT_FOUND` error. Both raw
outputs are findings; neither outcome is a failure of this step.

- [ ] **Step 3: Run the introspection probe and save output**

Run: `python scripts/spike_gh_projects_v2.py introspect 2>&1 | tee /tmp/spike-003-introspect.log`

Expected: a list of live mutation names (look for sub-issue mutations like `addSubIssue` /
`reprioritizeSubIssue` and whatever the dependency mutations are actually called), the argument
list of `ProjectV2.items` (delta-read answer: if there is no `since`/updated-time filter arg,
full paging is confirmed as required), and `Issue` fields confirming `blockedBy` / `parent` /
`subIssues` names used by `backlog_deps.py`.

- [ ] **Step 4: Commit**

```bash
git fetch origin
git checkout -b spike/003-phase0 origin/master
git add scripts/spike_gh_projects_v2.py
git commit -F /tmp/commit-msg.txt   # "spike(003): Projects v2 auth + introspection probes" + trailer
```

### Task 2: Spike script — mutation & limits probes on a scratch repo/Project

**Files:**
- Modify: `scripts/spike_gh_projects_v2.py`

**Interfaces:**
- Produces: `mutations` subcommand output for the findings doc. Creates and deletes
  `steiner385/gh-projects-spike-003` (private) and a scratch user Project — nothing else on
  GitHub is touched.

- [ ] **Step 1: Add the `probe_mutations` function**

Append to `scripts/spike_gh_projects_v2.py` (above `main`):

```python
def gh(*args: str) -> str:
    proc = subprocess.run(["gh", *args], capture_output=True, text=True)
    print(f"$ gh {' '.join(args)}\n{proc.stdout}{proc.stderr}")
    return proc.stdout


def probe_mutations() -> None:
    repo = f"{OWNER}/gh-projects-spike-003"
    gh("repo", "create", repo, "--private",
       "--description", "003 Phase 0 spike scratch — safe to delete")
    urls = [gh("issue", "create", "-R", repo, "-t", f"spike issue {i}",
               "-b", "scratch").strip() for i in (1, 2, 3)]
    print("issue urls:", urls)

    ids = gh_graphql(
        'query { repository(owner: "%s", name: "gh-projects-spike-003") {'
        " i1: issue(number: 1) { id } i2: issue(number: 2) { id }"
        " i3: issue(number: 3) { id } } }" % OWNER)
    print(json.dumps(ids, indent=2))
    i1, i2, i3 = (ids["data"]["repository"][k]["id"] for k in ("i1", "i2", "i3"))

    owner_id = gh_graphql(
        'query { user(login: "%s") { id } }' % OWNER)["data"]["user"]["id"]
    proj = gh_graphql(
        "mutation { createProjectV2(input: {ownerId: \"%s\", title: "
        "\"spike-003-scratch\"}) { projectV2 { id number } } }" % owner_id)
    print(json.dumps(proj, indent=2))
    pid = proj["data"]["createProjectV2"]["projectV2"]["id"]

    print("=== field creation (Phase B pre-verification) ===")
    field = gh_graphql(
        "mutation { createProjectV2Field(input: {projectId: \"%s\", "
        "dataType: NUMBER, name: \"Score\"}) { projectV2Field { "
        "... on ProjectV2FieldCommon { id name } } } }" % pid)
    print(json.dumps(field, indent=2))

    print("=== add items + aliased batched field updates ===")
    add = gh_graphql(
        "mutation { a: addProjectV2ItemById(input: {projectId: \"%s\", contentId: \"%s\"}) "
        "{ item { id } } b: addProjectV2ItemById(input: {projectId: \"%s\", contentId: \"%s\"}) "
        "{ item { id } } }" % (pid, i1, pid, i2))
    print(json.dumps(add, indent=2))
    item_a = add["data"]["a"]["item"]["id"]
    item_b = add["data"]["b"]["item"]["id"]
    fid = field["data"]["createProjectV2Field"]["projectV2Field"]["id"]
    batch = gh_graphql(
        "mutation { u1: updateProjectV2ItemFieldValue(input: {projectId: \"%s\", itemId: \"%s\","
        " fieldId: \"%s\", value: {number: 5}}) { projectV2Item { id } }"
        " u2: updateProjectV2ItemFieldValue(input: {projectId: \"%s\", itemId: \"%s\","
        " fieldId: \"%s\", value: {number: 8}}) { projectV2Item { id } } }"
        % (pid, item_a, fid, pid, item_b, fid))
    print(json.dumps(batch, indent=2))

    print("=== sub-issue add + reorder ===")
    print(json.dumps(gh_graphql(
        "mutation { addSubIssue(input: {issueId: \"%s\", subIssueId: \"%s\"}) "
        "{ issue { number } } }" % (i1, i2)), indent=2))
    print(json.dumps(gh_graphql(
        "mutation { addSubIssue(input: {issueId: \"%s\", subIssueId: \"%s\"}) "
        "{ issue { number } } }" % (i1, i3)), indent=2))
    print(json.dumps(gh_graphql(
        "mutation { reprioritizeSubIssue(input: {issueId: \"%s\", subIssueId: \"%s\"}) "
        "{ issue { subIssues(first: 5) { nodes { number } } } } }" % (i1, i3)), indent=2))

    print("=== native dependency add/remove (use mutation name from introspect probe) ===")
    # The dependency mutation names come from the `introspect` run. Try the introspected
    # name(s) here — e.g. if introspection lists addIssueDependency/removeIssueDependency:
    print(json.dumps(gh_graphql(
        "mutation { addIssueDependency(input: {issueId: \"%s\", blockedByIssueId: \"%s\"}) "
        "{ clientMutationId } }" % (i2, i3)), indent=2))
    print(json.dumps(gh_graphql(
        'query { repository(owner: "%s", name: "gh-projects-spike-003") { issue(number: 2) '
        "{ blockedBy(first: 5) { totalCount nodes { number } } } } }" % OWNER), indent=2))

    print("=== cleanup ===")
    print(json.dumps(gh_graphql(
        "mutation { deleteProjectV2(input: {projectId: \"%s\"}) { clientMutationId } }"
        % pid), indent=2))
    gh("repo", "delete", repo, "--yes")
```

If introspection (Task 1) showed different dependency-mutation names or input shapes, use those —
the script text is the record of what was actually run, so edit it to match reality before running.

- [ ] **Step 2: Run the mutation probe (needs `delete_repo` scope) and save output**

Run: `gh auth status` first; if `delete_repo` is missing run `gh auth refresh -s delete_repo`.
Then: `python scripts/spike_gh_projects_v2.py mutations 2>&1 | tee /tmp/spike-003-mutations.log`

Expected: every probe prints either a success payload or a structured GraphQL error — both are
findings. Confirm at the end that the scratch repo and Project are gone
(`gh repo view steiner385/gh-projects-spike-003` → not found).

- [ ] **Step 3: Commit**

```bash
git add scripts/spike_gh_projects_v2.py
git commit -F /tmp/commit-msg.txt   # "spike(003): mutation + limits probes on scratch project" + trailer
```

### Task 3: Findings doc, spec reconciliation, Phase 0 PR

**Files:**
- Create: `specs/003-gh-projects-integration/phase0-findings.md`
- Modify (only if findings contradict it): `specs/003-gh-projects-integration/spec.md`

**Interfaces:**
- Produces: the credential decision and confirmed field/mutation names that Tasks 5–9 consume
  (Task 5/6 query field names, Task 9 credential wiring).

- [ ] **Step 1: Write the findings doc from the three saved logs**

Structure (fill every row from `/tmp/spike-003-*.log` output — no row may say "assumed"):

```markdown
# 003 Phase 0 Findings — Projects v2 platform verification

**Date:** <run date> · **Spike:** `scripts/spike_gh_projects_v2.py` · raw logs summarized inline.

## Q1 — Credential story
| Probe | Result |
|---|---|
| Ambient gh user credential reads user Projects v2 | <yes/no + error text> |
| Required scope | <e.g. read:project present / added via gh auth refresh> |
| GitHub App installation token reads user Projects v2 | <yes/no + error text> |

**Decision:** Phase A reads use <credential>. Phase B writes will use <credential>.
<If the App works: note which permission grants it. If not: note that pm_sync's gh-CLI seam
already uses the working credential and no code change is needed.>

## Q2 — Mutations
| Mutation | Confirmed name | Works | Notes |
|---|---|---|---|
| Native dep add | <name> | <y/n> | <input shape> |
| Native dep remove | <name> | <y/n> | |
| Sub-issue add / reorder | addSubIssue / reprioritizeSubIssue | <y/n> | |
| Aliased batched field updates | updateProjectV2ItemFieldValue ×N | <y/n> | <N tested> |
| Field creation via API | createProjectV2Field | <y/n> | Phase B migration needs this |

## Q3 — Delta reads
`ProjectV2.items` args: <list>. Updated-time filtering <is/is not> available →
Phase A pages the full item list each cycle (bounded, acceptable at current scale).

## Q4 — Limits
| Limit | Documented | Binds at our scale? |
|---|---|---|
| sub-issues per parent | <n> | no — largest epic today ≈ <n> |
| dependencies per issue | <n> | no |
| items per Project | <n> | no |

## Spec impact
<"None — spec stands as approved" or the exact spec sections reopened and why.>
```

- [ ] **Step 2: Reconcile the spec**

If any finding contradicts spec assumptions (e.g. App credential cannot be used at all, or a
mutation doesn't exist), edit the affected spec section in the same commit and say so in the
findings "Spec impact" row. If nothing contradicts, state that explicitly.

- [ ] **Step 3: Commit, push, open the Phase 0 PR**

```bash
git add specs/003-gh-projects-integration/phase0-findings.md specs/003-gh-projects-integration/spec.md
git commit -F /tmp/commit-msg.txt   # "docs(003): Phase 0 verification findings" + trailer
git push -u origin spike/003-phase0
gh pr create --title "003 Phase 0: Projects v2 platform verification findings" --body-file /tmp/pr-body.txt
gh pr merge <#> --squash --auto
```

---

## Phase A — Read-only mirror (PR 1 of 2: `feat/003-phase-a-mirror`)

Branch off `origin/master` after the Phase 0 PR merges. If Phase 0 changed field/mutation names
used below, substitute the confirmed names everywhere.

### Task 4: Schema — four mirror tables + overlay columns

**Files:**
- Modify: `server/db/models.py` (after `WorkItem`, ~line 1274)
- Create: `server/db/migrations/versions/0018_gh_projects_mirror.py`
- Modify: `server/tests/test_postgres_compat.py` (`_FK_LINKS`; `_REQUIRED_EXTRAS` only if the
  suite demands it)
- Test: `server/tests/test_schema_reconcile_coverage.py` (existing sweep — no new file)

**Interfaces:**
- Produces: models `GhProject`, `GhProjectItem`, `WorkItemDep`, `GhWriteJournal`; columns
  `WorkItem.parent_work_item_id: str | None`, `WorkItem.is_epic: bool | None`,
  `Initiative.epic_repo: str | None`, `Initiative.epic_number: int | None`;
  constant `DEP_SOURCES = ("native", "prose", "order")`. Tasks 7–9 consume these exact names.

- [ ] **Step 1: Add the models**

Follow the existing conventions exactly (`Mapped[]`, `DateTime(timezone=True)`, `_utcnow`,
`_new_uuid`). Add to `server/db/models.py`:

```python
DEP_SOURCES = ("native", "prose", "order")


class GhProject(Base):
    """Read-only mirror of one GitHub Projects v2 board (one per product — spec 003 D3)."""

    __tablename__ = "gh_projects"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_new_uuid)
    project_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("projects.id"), nullable=True, index=True)
    gh_project_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    gh_node_id: Mapped[str | None] = mapped_column(String, nullable=True, unique=True)
    title: Mapped[str | None] = mapped_column(String, nullable=True)
    field_cache: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    watermark: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_synced_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow)


class GhProjectItem(Base):
    """Read-only mirror of one Projects v2 item; substrate of the global queue (spec 003 §5)."""

    __tablename__ = "gh_project_items"
    __table_args__ = (
        UniqueConstraint("gh_project_id", "gh_item_node_id", name="uq_gh_project_item"),)

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_new_uuid)
    gh_project_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("gh_projects.id"), nullable=True, index=True)
    work_item_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("work_items.id"), nullable=True, index=True)
    gh_item_node_id: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    stage: Mapped[str | None] = mapped_column(String, nullable=True)
    gh_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    last_synced_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow)


class WorkItemDep(Base):
    """One observed dependency edge; ``source`` records provenance (spec 003 §5)."""

    __tablename__ = "work_item_deps"
    __table_args__ = (
        UniqueConstraint("src_ref", "dst_ref", "source", name="uq_work_item_dep"),)

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_new_uuid)
    src_work_item_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("work_items.id"), nullable=True, index=True)
    dst_work_item_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("work_items.id"), nullable=True, index=True)
    src_ref: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    dst_ref: Mapped[str | None] = mapped_column(String, nullable=True)
    source: Mapped[str | None] = mapped_column(String, nullable=True)  # DEP_SOURCES
    active: Mapped[bool | None] = mapped_column(Boolean, nullable=True, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow)


class GhWriteJournal(Base):
    """Write-ahead journal for reconciler mutations (spec 003 §6). Written from Phase B on;
    the schema lands with the rest of the mirror so migration 0018 is the complete record."""

    __tablename__ = "gh_write_journal"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_new_uuid)
    target_node_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    field: Mapped[str | None] = mapped_column(String, nullable=True)
    old_value: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    new_value: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    mutation_id: Mapped[str | None] = mapped_column(String, nullable=True)
    initiated_by: Mapped[str | None] = mapped_column(String, nullable=True)
    applied: Mapped[bool | None] = mapped_column(Boolean, nullable=True, default=False)
    error: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow)
```

`src_ref` on `WorkItemDep` is deliberate (not in spec §5's minimum): the unique key and the
dep planner diff both need a stable identity that exists even before the src issue is mirrored.
Add `Float` to the existing `sqlalchemy` import line if absent.

Then add the overlay columns:

```python
# in WorkItem, after linked_issue_numbers:
    parent_work_item_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("work_items.id"), nullable=True, index=True)
    is_epic: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

# in Initiative, after origin_ref:
    epic_repo: Mapped[str | None] = mapped_column(String, nullable=True)
    epic_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
```

- [ ] **Step 2: Write the Alembic revision (record only — reconcile applies it)**

`server/db/migrations/versions/0018_gh_projects_mirror.py`, mirroring `0014_pm_product_layer`'s
structure:

```python
"""003 Phase A: Projects v2 mirror tables + epic overlay columns."""
import sqlalchemy as sa
from alembic import op

revision = "0018_gh_projects_mirror"
down_revision = "0017_deploy_state"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "gh_projects",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("project_id", sa.String(), sa.ForeignKey("projects.id"), nullable=True),
        sa.Column("gh_project_number", sa.Integer(), nullable=True),
        sa.Column("gh_node_id", sa.String(), nullable=True, unique=True),
        sa.Column("title", sa.String(), nullable=True),
        sa.Column("field_cache", sa.JSON(), nullable=True),
        sa.Column("watermark", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_gh_projects_project_id", "gh_projects", ["project_id"])
    op.create_table(
        "gh_project_items",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("gh_project_id", sa.String(), sa.ForeignKey("gh_projects.id"), nullable=True),
        sa.Column("work_item_id", sa.String(), sa.ForeignKey("work_items.id"), nullable=True),
        sa.Column("gh_item_node_id", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=True),
        sa.Column("score", sa.Float(), nullable=True),
        sa.Column("stage", sa.String(), nullable=True),
        sa.Column("gh_updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("gh_project_id", "gh_item_node_id", name="uq_gh_project_item"),
    )
    op.create_index("ix_gh_project_items_status", "gh_project_items", ["status"])
    op.create_table(
        "work_item_deps",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("src_work_item_id", sa.String(), sa.ForeignKey("work_items.id"), nullable=True),
        sa.Column("dst_work_item_id", sa.String(), sa.ForeignKey("work_items.id"), nullable=True),
        sa.Column("src_ref", sa.String(), nullable=True),
        sa.Column("dst_ref", sa.String(), nullable=True),
        sa.Column("source", sa.String(), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("src_ref", "dst_ref", "source", name="uq_work_item_dep"),
    )
    op.create_index("ix_work_item_deps_src_ref", "work_item_deps", ["src_ref"])
    op.create_table(
        "gh_write_journal",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("target_node_id", sa.String(), nullable=True),
        sa.Column("field", sa.String(), nullable=True),
        sa.Column("old_value", sa.JSON(), nullable=True),
        sa.Column("new_value", sa.JSON(), nullable=True),
        sa.Column("mutation_id", sa.String(), nullable=True),
        sa.Column("initiated_by", sa.String(), nullable=True),
        sa.Column("applied", sa.Boolean(), nullable=True),
        sa.Column("error", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_gh_write_journal_target", "gh_write_journal", ["target_node_id"])
    op.add_column("work_items", sa.Column("parent_work_item_id", sa.String(), nullable=True))
    op.add_column("work_items", sa.Column("is_epic", sa.Boolean(), nullable=True))
    op.add_column("initiatives", sa.Column("epic_repo", sa.String(), nullable=True))
    op.add_column("initiatives", sa.Column("epic_number", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("initiatives", "epic_number")
    op.drop_column("initiatives", "epic_repo")
    op.drop_column("work_items", "is_epic")
    op.drop_column("work_items", "parent_work_item_id")
    op.drop_table("gh_write_journal")
    op.drop_table("work_item_deps")
    op.drop_table("gh_project_items")
    op.drop_table("gh_projects")
```

- [ ] **Step 3: Update the Postgres-suite lists and run the schema sweeps**

Every new column is nullable and every NOT NULL column (`created_at`/`updated_at`) has a default,
so `RECONCILE_EXEMPT` needs no entries — verify by running the sweep. In
`server/tests/test_postgres_compat.py`, add FK links so the datetime sweep inserts parents first:

```python
# in _FK_LINKS:
    "gh_projects": {"project_id": "projects"},
    "gh_project_items": {"gh_project_id": "gh_projects", "work_item_id": "work_items"},
    "work_item_deps": {"src_work_item_id": "work_items", "dst_work_item_id": "work_items"},
```

(Match the existing `_FK_LINKS` entry format exactly; if the sweep then demands `_REQUIRED_EXTRAS`
entries, add what it asks for.)

Run (SQLite sweeps):
`env -u CCSWITCH_TAILNET_ALLOWLIST -u CCSWITCH_SESSIOND_ENDPOINT -u CCSWITCH_SESSIOND_CLIENT_CERT CCSWITCH_DEV=1 /home/tony/GitHub/ccswitch/.venv/bin/python -m pytest server/tests/test_schema_reconcile_coverage.py server/tests/test_postgres_compat.py -v`

Then the real-Postgres run (adding a MODEL requires it — CI skips this):
`CCSWITCH_TEST_PG_URL=<local pg url> ... -m pytest server/tests/test_postgres_compat.py -v`

Expected: PASS. If the drift assert or datetime sweep fails, fix the model/lists — do not exempt
your way past a real mismatch.

- [ ] **Step 4: Commit**

```bash
git fetch origin && git checkout -b feat/003-phase-a-mirror origin/master
git add server/db/models.py server/db/migrations/versions/0018_gh_projects_mirror.py server/tests/test_postgres_compat.py
git commit -F /tmp/commit-msg.txt   # "feat(003): Phase A mirror schema — gh_projects, items, deps, journal" + trailer
```

### Task 5: Projects v2 GraphQL builders + parsers (pure)

**Files:**
- Create: `server/pm/gh_projects.py`
- Test: `server/tests/test_gh_projects_queries.py`

**Interfaces:**
- Consumes: nothing (pure module, stdlib + dataclasses only — mirror `server/pm/github_sync.py`).
- Produces (Tasks 8–9 consume these exact names):

```python
@dataclass(frozen=True)
class GhProjectView:
    node_id: str
    number: int
    title: str
    updated_at: str | None

@dataclass(frozen=True)
class GhProjectItemView:
    item_node_id: str
    repo: str | None        # "owner/name"; None for draft items
    number: int | None
    kind: str | None        # "issue" | "pr" | None (draft)
    status: str | None
    score: float | None
    stage: str | None
    updated_at: str | None

def build_viewer_projects_query(cursor: str | None) -> str
def parse_viewer_projects(payload: dict) -> tuple[list[GhProjectView], str | None, bool]
def build_project_fields_query(project_node_id: str) -> str
def parse_project_fields(payload: dict) -> dict   # {name: {"field_id": id, "options": {name: id}}}
def build_project_items_query(project_node_id: str, cursor: str | None) -> str
def parse_project_items(payload: dict) -> tuple[list[GhProjectItemView], str | None, bool]
```

- [ ] **Step 1: Write the failing tests**

`server/tests/test_gh_projects_queries.py`, following `test_pm_github_sync.py`'s fixture style:

```python
from server.pm.gh_projects import (
    build_project_items_query, build_viewer_projects_query, parse_project_fields,
    parse_project_items, parse_viewer_projects)


def _projects_payload():
    return {"data": {"viewer": {"projectsV2": {
        "pageInfo": {"endCursor": "C1", "hasNextPage": False},
        "nodes": [
            {"id": "PVT_1", "number": 7, "title": "cc-account-switcher", "closed": False,
             "updatedAt": "2026-09-01T10:00:00Z"},
            {"id": "PVT_2", "number": 8, "title": "Old board", "closed": True,
             "updatedAt": "2026-01-01T10:00:00Z"},
        ]}}}}


def _fields_payload():
    return {"data": {"node": {"fields": {"nodes": [
        {"id": "F_status", "name": "Status",
         "options": [{"id": "O_ready", "name": "Ready"}, {"id": "O_done", "name": "Done"}]},
        {"id": "F_score", "name": "Score"},
        {},  # unrecognized field type -> empty object from inline fragments
    ]}}}}


def _items_payload():
    return {"data": {"node": {"items": {
        "pageInfo": {"endCursor": "I1", "hasNextPage": True},
        "nodes": [
            {"id": "PVTI_1", "updatedAt": "2026-09-02T09:00:00Z",
             "content": {"__typename": "Issue", "number": 950,
                         "repository": {"nameWithOwner": "steiner385/cc-account-switcher"}},
             "fieldValues": {"nodes": [
                 {"name": "Ready", "field": {"name": "Status"}},
                 {"number": 12.0, "field": {"name": "Score"}},
             ]}},
            {"id": "PVTI_2", "updatedAt": None,
             "content": {"__typename": "DraftIssue"}, "fieldValues": {"nodes": []}},
        ]}}}}


def test_parse_viewer_projects_skips_closed_and_pages():
    views, cursor, more = parse_viewer_projects(_projects_payload())
    assert [(v.number, v.title) for v in views] == [(7, "cc-account-switcher")]
    assert cursor == "C1" and more is False


def test_parse_project_fields_caches_ids_and_options():
    cache = parse_project_fields(_fields_payload())
    assert cache["Status"] == {"field_id": "F_status",
                               "options": {"Ready": "O_ready", "Done": "O_done"}}
    assert cache["Score"] == {"field_id": "F_score", "options": {}}


def test_parse_project_items_extracts_fields_and_marks_drafts():
    items, cursor, more = parse_project_items(_items_payload())
    assert cursor == "I1" and more is True
    issue, draft = items
    assert (issue.repo, issue.number, issue.kind) == ("steiner385/cc-account-switcher", 950, "issue")
    assert (issue.status, issue.score, issue.stage) == ("Ready", 12.0, None)
    assert draft.kind is None and draft.repo is None


def test_queries_embed_pagination_cursor():
    assert 'after: "C1"' in build_viewer_projects_query("C1")
    assert "after:" not in build_viewer_projects_query(None)
    assert 'node(id: "PVT_1")' in build_project_items_query("PVT_1", None)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `... -m pytest server/tests/test_gh_projects_queries.py -v` (env prefix from Global
Constraints). Expected: FAIL with `ModuleNotFoundError: server.pm.gh_projects`.

- [ ] **Step 3: Implement `server/pm/gh_projects.py`**

```python
"""Pure GraphQL builders/parsers for the Projects v2 mirror (spec 003 Phase A).

Field selections were verified against the live schema by the Phase 0 spike
(specs/003-gh-projects-integration/phase0-findings.md).
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class GhProjectView:
    node_id: str
    number: int
    title: str
    updated_at: str | None


@dataclass(frozen=True)
class GhProjectItemView:
    item_node_id: str
    repo: str | None
    number: int | None
    kind: str | None
    status: str | None
    score: float | None
    stage: str | None
    updated_at: str | None


def _after(cursor: str | None) -> str:
    return f', after: "{cursor}"' if cursor else ""


def build_viewer_projects_query(cursor: str | None) -> str:
    return (
        "query { viewer { projectsV2(first: 20%s) {"
        " pageInfo { endCursor hasNextPage }"
        " nodes { id number title closed updatedAt } } } }" % _after(cursor))


def parse_viewer_projects(payload: dict) -> tuple[list[GhProjectView], str | None, bool]:
    conn = (payload.get("data") or {}).get("viewer", {}).get("projectsV2", {}) or {}
    views = [
        GhProjectView(node_id=n["id"], number=n["number"], title=n.get("title") or "",
                      updated_at=n.get("updatedAt"))
        for n in conn.get("nodes") or [] if n and not n.get("closed")]
    page = conn.get("pageInfo") or {}
    return views, page.get("endCursor"), bool(page.get("hasNextPage"))


def build_project_fields_query(project_node_id: str) -> str:
    return (
        'query { node(id: "%s") { ... on ProjectV2 { fields(first: 50) { nodes {'
        " ... on ProjectV2FieldCommon { id name }"
        " ... on ProjectV2SingleSelectField { id name options { id name } }"
        " } } } } }" % project_node_id)


def parse_project_fields(payload: dict) -> dict:
    nodes = (((payload.get("data") or {}).get("node") or {}).get("fields") or {}).get("nodes") or []
    cache: dict = {}
    for node in nodes:
        if not node or "name" not in node:
            continue
        cache[node["name"]] = {
            "field_id": node.get("id"),
            "options": {o["name"]: o["id"] for o in node.get("options") or []},
        }
    return cache


def build_project_items_query(project_node_id: str, cursor: str | None) -> str:
    return (
        'query { node(id: "%s") { ... on ProjectV2 { items(first: 100%s) {'
        " pageInfo { endCursor hasNextPage }"
        " nodes { id updatedAt"
        " content { __typename"
        " ... on Issue { number repository { nameWithOwner } }"
        " ... on PullRequest { number repository { nameWithOwner } } }"
        " fieldValues(first: 20) { nodes {"
        " ... on ProjectV2ItemFieldSingleSelectValue { name field { ... on ProjectV2FieldCommon { name } } }"
        " ... on ProjectV2ItemFieldNumberValue { number field { ... on ProjectV2FieldCommon { name } } }"
        " } } } } } } }" % (project_node_id, _after(cursor)))


_KINDS = {"Issue": "issue", "PullRequest": "pr"}


def parse_project_items(payload: dict) -> tuple[list[GhProjectItemView], str | None, bool]:
    conn = (((payload.get("data") or {}).get("node") or {}).get("items") or {})
    items: list[GhProjectItemView] = []
    for node in conn.get("nodes") or []:
        if not node:
            continue
        content = node.get("content") or {}
        kind = _KINDS.get(content.get("__typename"))
        status = score = stage = None
        for fv in (node.get("fieldValues") or {}).get("nodes") or []:
            fname = ((fv or {}).get("field") or {}).get("name")
            if fname == "Status":
                status = fv.get("name")
            elif fname == "Score":
                score = fv.get("number")
            elif fname == "Stage":
                stage = fv.get("name")
        items.append(GhProjectItemView(
            item_node_id=node["id"],
            repo=(content.get("repository") or {}).get("nameWithOwner") if kind else None,
            number=content.get("number") if kind else None,
            kind=kind, status=status, score=score, stage=stage,
            updated_at=node.get("updatedAt")))
    page = conn.get("pageInfo") or {}
    return items, page.get("endCursor"), bool(page.get("hasNextPage"))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `... -m pytest server/tests/test_gh_projects_queries.py -v`. Expected: 4 PASS.

- [ ] **Step 5: Commit**

```bash
git add server/pm/gh_projects.py server/tests/test_gh_projects_queries.py
git commit -F /tmp/commit-msg.txt   # "feat(003): Projects v2 query builders + parsers" + trailer
```

### Task 6: Issue-relations query builder + parser (pure)

**Files:**
- Modify: `server/pm/gh_projects.py`
- Test: `server/tests/test_gh_projects_queries.py` (append)

**Interfaces:**
- Produces (Tasks 7–9 consume):

```python
@dataclass(frozen=True)
class IssueRelView:
    repo: str
    number: int
    blocked_by: list[str]        # canonical refs "owner/repo#N"
    parent_ref: str | None
    sub_issue_refs: list[str]    # ordered; [] for non-epics

def build_issue_relations_query(repo: str, cursor: str | None) -> str
def parse_issue_relations(payload: dict, repo: str) -> tuple[list[IssueRelView], str | None, bool]
```

The query covers **open issues only, full-paged each due sync** (no watermark): deps only gate
open work, the open set is small, and a full pass makes `active`-flag maintenance self-healing
when edges are removed on GitHub.

- [ ] **Step 1: Write the failing tests (append to `test_gh_projects_queries.py`)**

```python
from server.pm.gh_projects import build_issue_relations_query, parse_issue_relations


def _relations_payload():
    return {"data": {"repository": {"issues": {
        "pageInfo": {"endCursor": "R1", "hasNextPage": False},
        "nodes": [
            {"number": 945,
             "blockedBy": {"nodes": []},
             "parent": None,
             "subIssues": {"nodes": [
                 {"number": 950, "repository": {"nameWithOwner": "steiner385/cc-account-switcher"}},
                 {"number": 951, "repository": {"nameWithOwner": "steiner385/cc-account-switcher"}},
             ]}},
            {"number": 950,
             "blockedBy": {"nodes": [
                 {"number": 12, "repository": {"nameWithOwner": "steiner385/KinDash"}}]},
             "parent": {"number": 945,
                        "repository": {"nameWithOwner": "steiner385/cc-account-switcher"}},
             "subIssues": {"nodes": []}},
        ]}}}}


def test_parse_issue_relations_builds_canonical_refs():
    rels, cursor, more = parse_issue_relations(
        _relations_payload(), "steiner385/cc-account-switcher")
    assert cursor == "R1" and more is False
    epic, child = rels
    assert epic.sub_issue_refs == [
        "steiner385/cc-account-switcher#950", "steiner385/cc-account-switcher#951"]
    assert epic.parent_ref is None
    assert child.blocked_by == ["steiner385/KinDash#12"]
    assert child.parent_ref == "steiner385/cc-account-switcher#945"


def test_relations_query_targets_open_issues():
    q = build_issue_relations_query("steiner385/cc-account-switcher", None)
    assert 'owner: "steiner385"' in q and 'name: "cc-account-switcher"' in q
    assert "states: OPEN" in q
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `... -m pytest server/tests/test_gh_projects_queries.py -v`
Expected: the two new tests FAIL with ImportError.

- [ ] **Step 3: Implement (append to `server/pm/gh_projects.py`)**

The `blockedBy` / `parent` / `subIssues` selections mirror what
`server/automations/backlog_deps.py` already consumes from the runner's live query (and were
re-confirmed by the Phase 0 introspection probe).

```python
@dataclass(frozen=True)
class IssueRelView:
    repo: str
    number: int
    blocked_by: list[str]
    parent_ref: str | None
    sub_issue_refs: list[str]


def _ref(node: dict | None) -> str | None:
    if not node:
        return None
    repo = (node.get("repository") or {}).get("nameWithOwner")
    number = node.get("number")
    if not repo or number is None:
        return None
    return f"{repo}#{number}"


def build_issue_relations_query(repo: str, cursor: str | None) -> str:
    owner, name = repo.split("/", 1)
    return (
        'query { repository(owner: "%s", name: "%s") {'
        " issues(first: 100, states: OPEN%s) {"
        " pageInfo { endCursor hasNextPage }"
        " nodes { number"
        " blockedBy(first: 50) { nodes { number repository { nameWithOwner } } }"
        " parent { number repository { nameWithOwner } }"
        " subIssues(first: 100) { nodes { number repository { nameWithOwner } } }"
        " } } } }" % (owner, name, _after(cursor)))


def parse_issue_relations(payload: dict, repo: str) -> tuple[list[IssueRelView], str | None, bool]:
    conn = (((payload.get("data") or {}).get("repository") or {}).get("issues") or {})
    rels = []
    for node in conn.get("nodes") or []:
        if not node:
            continue
        rels.append(IssueRelView(
            repo=repo, number=node["number"],
            blocked_by=[r for r in (
                _ref(b) for b in (node.get("blockedBy") or {}).get("nodes") or []) if r],
            parent_ref=_ref(node.get("parent")),
            sub_issue_refs=[r for r in (
                _ref(s) for s in (node.get("subIssues") or {}).get("nodes") or []) if r]))
    page = conn.get("pageInfo") or {}
    return rels, page.get("endCursor"), bool(page.get("hasNextPage"))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `... -m pytest server/tests/test_gh_projects_queries.py -v`. Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add server/pm/gh_projects.py server/tests/test_gh_projects_queries.py
git commit -F /tmp/commit-msg.txt   # "feat(003): issue relations query + parser" + trailer
```

### Task 7: Dependency observation planner (pure)

**Files:**
- Create: `server/pm/dep_plan.py`
- Test: `server/tests/test_dep_plan.py`

**Interfaces:**
- Consumes: `IssueRelView` (Task 6); `parse_body_dep_refs` from
  `server.automations.backlog_deps` (existing source of truth — never reimplement).
- Produces (Task 8 consumes):

```python
@dataclass(frozen=True)
class ObservedDep:
    src_ref: str
    dst_ref: str
    source: str    # "native" | "prose" | "order"

@dataclass(frozen=True)
class DepRowView:
    id: str
    src_ref: str
    dst_ref: str
    source: str
    active: bool

@dataclass
class DepPlan:
    create: list[ObservedDep]
    reactivate: list[str]    # WorkItemDep row ids
    deactivate: list[str]    # WorkItemDep row ids

def observed_deps(repo: str, rels: list[IssueRelView],
                  bodies: Mapping[str, str | None]) -> list[ObservedDep]
def plan_dep_rows(observed: Iterable[ObservedDep],
                  existing: Iterable[DepRowView]) -> DepPlan
```

- [ ] **Step 1: Write the failing tests**

`server/tests/test_dep_plan.py`:

```python
from server.pm.dep_plan import DepRowView, ObservedDep, observed_deps, plan_dep_rows
from server.pm.gh_projects import IssueRelView

REPO = "steiner385/cc-account-switcher"


def _rel(number, blocked_by=(), parent=None, subs=()):
    return IssueRelView(repo=REPO, number=number, blocked_by=list(blocked_by),
                        parent_ref=parent, sub_issue_refs=list(subs))


def test_observed_deps_merges_native_prose_and_order():
    rels = [
        _rel(945, subs=[f"{REPO}#950", f"{REPO}#951", f"{REPO}#952"]),
        _rel(950, blocked_by=["steiner385/KinDash#12"], parent=f"{REPO}#945"),
    ]
    bodies = {f"{REPO}#950": "Depends on #949", f"{REPO}#945": None}
    deps = set(observed_deps(REPO, rels, bodies))
    assert ObservedDep(f"{REPO}#950", "steiner385/KinDash#12", "native") in deps
    assert ObservedDep(f"{REPO}#950", f"{REPO}#949", "prose") in deps
    # order: consecutive sibling chain, first sub-issue has no order dep
    assert ObservedDep(f"{REPO}#951", f"{REPO}#950", "order") in deps
    assert ObservedDep(f"{REPO}#952", f"{REPO}#951", "order") in deps
    assert not any(d.dst_ref == d.src_ref for d in deps)


def test_plan_dep_rows_diffs_create_reactivate_deactivate():
    observed = [ObservedDep("a#1", "a#2", "native"), ObservedDep("a#1", "a#3", "prose")]
    existing = [
        DepRowView("row1", "a#1", "a#2", "native", active=False),   # -> reactivate
        DepRowView("row2", "a#1", "a#9", "native", active=True),    # -> deactivate
        DepRowView("row3", "a#1", "a#9", "prose", active=False),    # inactive+gone -> untouched
    ]
    plan = plan_dep_rows(observed, existing)
    assert plan.create == [ObservedDep("a#1", "a#3", "prose")]
    assert plan.reactivate == ["row1"]
    assert plan.deactivate == ["row2"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `... -m pytest server/tests/test_dep_plan.py -v`
Expected: FAIL with `ModuleNotFoundError: server.pm.dep_plan`.

- [ ] **Step 3: Implement `server/pm/dep_plan.py`**

```python
"""Pure dependency-observation planner for the 003 mirror (read-only in Phase A).

Prose parsing is delegated to backlog_deps — the single source of truth for
`Depends on #N` conventions. Order edges chain consecutive sub-issue siblings,
mirroring the "phase order = sibling blocked-by" convention of spec 003 §4.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from server.automations.backlog_deps import parse_body_dep_refs

from .gh_projects import IssueRelView


@dataclass(frozen=True)
class ObservedDep:
    src_ref: str
    dst_ref: str
    source: str


@dataclass(frozen=True)
class DepRowView:
    id: str
    src_ref: str
    dst_ref: str
    source: str
    active: bool


@dataclass
class DepPlan:
    create: list[ObservedDep]
    reactivate: list[str]
    deactivate: list[str]


def observed_deps(repo: str, rels: list[IssueRelView],
                  bodies: Mapping[str, str | None]) -> list[ObservedDep]:
    out: list[ObservedDep] = []
    seen: set[tuple[str, str, str]] = set()

    def add(src: str, dst: str, source: str) -> None:
        if src == dst:
            return
        key = (src, dst, source)
        if key not in seen:
            seen.add(key)
            out.append(ObservedDep(src, dst, source))

    for rel in rels:
        src = f"{rel.repo}#{rel.number}"
        for dst in rel.blocked_by:
            add(src, dst, "native")
        for prev, cur in zip(rel.sub_issue_refs, rel.sub_issue_refs[1:]):
            add(cur, prev, "order")
    for src, body in bodies.items():
        for dst in parse_body_dep_refs(body, repo):
            add(src, dst, "prose")
    return out


def plan_dep_rows(observed: Iterable[ObservedDep],
                  existing: Iterable[DepRowView]) -> DepPlan:
    wanted = {(d.src_ref, d.dst_ref, d.source): d for d in observed}
    plan = DepPlan(create=[], reactivate=[], deactivate=[])
    for row in existing:
        key = (row.src_ref, row.dst_ref, row.source)
        if key in wanted:
            wanted.pop(key)
            if not row.active:
                plan.reactivate.append(row.id)
        elif row.active:
            plan.deactivate.append(row.id)
    plan.create = list(wanted.values())
    return plan
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `... -m pytest server/tests/test_dep_plan.py -v`. Expected: 2 PASS.

- [ ] **Step 5: Commit**

```bash
git add server/pm/dep_plan.py server/tests/test_dep_plan.py
git commit -F /tmp/commit-msg.txt   # "feat(003): dependency observation planner" + trailer
```

### Task 8: Mirror persistence helpers (`mirror_store`)

**Files:**
- Create: `server/pm/mirror_store.py`
- Test: `server/tests/test_mirror_store.py`

**Interfaces:**
- Consumes: models from Task 4; views from Tasks 5–6; `observed_deps`/`plan_dep_rows` (Task 7).
- Produces (Task 9's orchestrator seams are thin async wrappers over these):

```python
@dataclass(frozen=True)
class ProductSyncView:
    project_id: str            # ccswitch Project.id
    slug: str | None
    name: str | None
    gh_project_db_id: str | None
    gh_node_id: str | None
    repos: list[str]           # "owner/name" of the product's ProjectRepo rows

def list_product_views(db: Session) -> list[ProductSyncView]
def upsert_gh_project(db: Session, project_id: str, view: GhProjectView) -> str  # db id
def save_field_cache(db: Session, gh_project_db_id: str, cache: dict) -> None
def upsert_project_items(db: Session, gh_project_db_id: str,
                         items: list[GhProjectItemView], now: datetime) -> None
def upsert_relations(db: Session, repo: str, rels: list[IssueRelView]) -> None
```

All functions take a `Session` and are synchronous — directly unit-testable on in-memory SQLite.

- [ ] **Step 1: Write the failing tests**

`server/tests/test_mirror_store.py`:

```python
from datetime import datetime, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from server.db.models import (
    Base, GhProject, GhProjectItem, Project, ProjectRepo, WorkItem, WorkItemDep)
from server.pm.gh_projects import GhProjectItemView, GhProjectView, IssueRelView
from server.pm import mirror_store

REPO = "steiner385/cc-account-switcher"


def _db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    return Session(engine)


def _seed(db):
    project = Project(slug="ccswitch", name="cc-account-switcher")
    db.add(project)
    db.flush()
    db.add(ProjectRepo(project_id=project.id, repo=REPO))
    for number in (945, 950, 951):
        db.add(WorkItem(repo=REPO, number=number, kind="issue", state="open",
                        body="Depends on #949" if number == 950 else None))
    db.commit()
    return project


def test_upsert_gh_project_links_and_is_idempotent():
    db = _db()
    project = _seed(db)
    view = GhProjectView(node_id="PVT_1", number=7, title="cc-account-switcher",
                         updated_at="2026-09-01T10:00:00Z")
    gid1 = mirror_store.upsert_gh_project(db, project.id, view)
    gid2 = mirror_store.upsert_gh_project(db, project.id, view)
    assert gid1 == gid2
    assert db.query(GhProject).count() == 1
    views = mirror_store.list_product_views(db)
    assert views[0].gh_node_id == "PVT_1" and views[0].repos == [REPO]


def test_upsert_project_items_resolves_work_items_and_prunes_gone():
    db = _db()
    project = _seed(db)
    gid = mirror_store.upsert_gh_project(db, project.id, GhProjectView(
        node_id="PVT_1", number=7, title="x", updated_at=None))
    now = datetime.now(timezone.utc)
    items = [GhProjectItemView(item_node_id="PVTI_1", repo=REPO, number=950, kind="issue",
                               status="Ready", score=12.0, stage=None,
                               updated_at="2026-09-02T09:00:00Z")]
    mirror_store.upsert_project_items(db, gid, items, now)
    row = db.query(GhProjectItem).one()
    assert row.status == "Ready" and row.score == 12.0
    assert row.work_item_id == db.query(WorkItem).filter_by(number=950).one().id
    # second pass without the item removes the stale mirror row
    mirror_store.upsert_project_items(db, gid, [], now)
    assert db.query(GhProjectItem).count() == 0


def test_upsert_relations_sets_parents_and_plans_deps():
    db = _db()
    _seed(db)
    rels = [
        IssueRelView(repo=REPO, number=945, blocked_by=[], parent_ref=None,
                     sub_issue_refs=[f"{REPO}#950", f"{REPO}#951"]),
        IssueRelView(repo=REPO, number=950, blocked_by=["steiner385/KinDash#12"],
                     parent_ref=f"{REPO}#945", sub_issue_refs=[]),
    ]
    mirror_store.upsert_relations(db, REPO, rels)
    epic = db.query(WorkItem).filter_by(number=945).one()
    child = db.query(WorkItem).filter_by(number=950).one()
    assert epic.is_epic is True and child.parent_work_item_id == epic.id
    deps = {(d.src_ref, d.dst_ref, d.source, d.active)
            for d in db.query(WorkItemDep).all()}
    assert (f"{REPO}#950", "steiner385/KinDash#12", "native", True) in deps
    assert (f"{REPO}#950", f"{REPO}#949", "prose", True) in deps
    assert (f"{REPO}#951", f"{REPO}#950", "order", True) in deps
    # cross-repo target is unmirrored: dst_work_item_id stays None, dst_ref carries it
    native = [d for d in db.query(WorkItemDep).all() if d.source == "native"][0]
    assert native.dst_work_item_id is None
    # edge removed upstream -> deactivated, not deleted
    mirror_store.upsert_relations(db, REPO, [
        rels[0],
        IssueRelView(repo=REPO, number=950, blocked_by=[], parent_ref=f"{REPO}#945",
                     sub_issue_refs=[]),
    ])
    native = [d for d in db.query(WorkItemDep).all()
              if d.source == "native" and d.dst_ref == "steiner385/KinDash#12"][0]
    assert native.active is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `... -m pytest server/tests/test_mirror_store.py -v`
Expected: FAIL with `ModuleNotFoundError: server.pm.mirror_store`.

- [ ] **Step 3: Implement `server/pm/mirror_store.py`**

```python
"""Session-level persistence for the 003 read-only mirror. Callers own the transaction."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.orm import Session

from server.db.models import (
    GhProject, GhProjectItem, Project, ProjectRepo, WorkItem, WorkItemDep)

from .dep_plan import DepRowView, observed_deps, plan_dep_rows
from .gh_projects import GhProjectItemView, GhProjectView, IssueRelView


@dataclass(frozen=True)
class ProductSyncView:
    project_id: str
    slug: str | None
    name: str | None
    gh_project_db_id: str | None
    gh_node_id: str | None
    repos: list[str]


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def list_product_views(db: Session) -> list[ProductSyncView]:
    views = []
    for project in db.query(Project).all():
        gh = db.query(GhProject).filter_by(project_id=project.id).first()
        repos = [r.repo for r in db.query(ProjectRepo)
                 .filter_by(project_id=project.id).all() if r.repo]
        views.append(ProductSyncView(
            project_id=project.id, slug=project.slug, name=project.name,
            gh_project_db_id=gh.id if gh else None,
            gh_node_id=gh.gh_node_id if gh else None, repos=repos))
    return views


def upsert_gh_project(db: Session, project_id: str, view: GhProjectView) -> str:
    row = db.query(GhProject).filter_by(gh_node_id=view.node_id).first()
    if row is None:
        row = GhProject(gh_node_id=view.node_id)
        db.add(row)
    row.project_id = project_id
    row.gh_project_number = view.number
    row.title = view.title
    db.flush()
    return row.id


def save_field_cache(db: Session, gh_project_db_id: str, cache: dict) -> None:
    row = db.get(GhProject, gh_project_db_id)
    if row is not None:
        row.field_cache = cache


def _work_item_id(db: Session, repo: str | None, number: int | None,
                  kind: str | None) -> str | None:
    if not repo or number is None or not kind:
        return None
    item = db.query(WorkItem).filter_by(repo=repo, number=number, kind=kind).first()
    return item.id if item else None


def upsert_project_items(db: Session, gh_project_db_id: str,
                         items: list[GhProjectItemView], now: datetime) -> None:
    seen: set[str] = set()
    for view in items:
        seen.add(view.item_node_id)
        row = db.query(GhProjectItem).filter_by(
            gh_project_id=gh_project_db_id, gh_item_node_id=view.item_node_id).first()
        if row is None:
            row = GhProjectItem(gh_project_id=gh_project_db_id,
                                gh_item_node_id=view.item_node_id)
            db.add(row)
        row.work_item_id = _work_item_id(db, view.repo, view.number, view.kind)
        row.status = view.status
        row.score = view.score
        row.stage = view.stage
        row.gh_updated_at = _parse_iso(view.updated_at)
        row.last_synced_at = now
    stale = db.query(GhProjectItem).filter_by(gh_project_id=gh_project_db_id).all()
    for row in stale:
        if row.gh_item_node_id not in seen:
            db.delete(row)
    gh = db.get(GhProject, gh_project_db_id)
    if gh is not None:
        gh.last_synced_at = now
        gh.watermark = now


def upsert_relations(db: Session, repo: str, rels: list[IssueRelView]) -> None:
    by_ref: dict[str, WorkItem] = {
        f"{w.repo}#{w.number}": w
        for w in db.query(WorkItem).filter_by(repo=repo, kind="issue").all()}
    for rel in rels:
        src = by_ref.get(f"{rel.repo}#{rel.number}")
        if src is None:
            continue
        src.is_epic = bool(rel.sub_issue_refs)
        parent = by_ref.get(rel.parent_ref) if rel.parent_ref else None
        src.parent_work_item_id = parent.id if parent else None
    bodies = {ref: item.body for ref, item in by_ref.items()
              if (item.state or "open") == "open"}
    observed = observed_deps(repo, rels, bodies)
    src_prefix = f"{repo}#"
    existing = [d for d in db.query(WorkItemDep).all()
                if (d.src_ref or "").startswith(src_prefix)]
    plan = plan_dep_rows(observed, [
        DepRowView(id=d.id, src_ref=d.src_ref or "", dst_ref=d.dst_ref or "",
                   source=d.source or "", active=bool(d.active)) for d in existing])
    rows_by_id = {d.id: d for d in existing}
    for dep in plan.create:
        db.add(WorkItemDep(
            src_ref=dep.src_ref, dst_ref=dep.dst_ref, source=dep.source, active=True,
            src_work_item_id=(by_ref.get(dep.src_ref).id
                              if by_ref.get(dep.src_ref) else None),
            dst_work_item_id=_resolve_ref(db, dep.dst_ref)))
    for row_id in plan.reactivate:
        rows_by_id[row_id].active = True
    for row_id in plan.deactivate:
        rows_by_id[row_id].active = False


def _resolve_ref(db: Session, ref: str) -> str | None:
    repo, _, number = ref.partition("#")
    if not number.isdigit():
        return None
    return _work_item_id(db, repo, int(number), "issue")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `... -m pytest server/tests/test_mirror_store.py -v`. Expected: 3 PASS.

- [ ] **Step 5: Commit**

```bash
git add server/pm/mirror_store.py server/tests/test_mirror_store.py
git commit -F /tmp/commit-msg.txt   # "feat(003): mirror persistence helpers" + trailer
```

### Task 9: Wire the mirror into `PmSyncLoop` and the orchestrator

**Files:**
- Modify: `server/automations/pm_sync.py` (`PmSyncLoop.__init__` seams ~lines 91–114, `_run`
  ~lines 124–155; `PmSync.status` counts ~lines 47–70)
- Modify: `server/sessions/orchestrator.py` (seam impls near `_pm_sync_upsert_items`
  ~line 3444; loop construction where `PmSyncLoop` is instantiated)
- Test: `server/tests/test_pm_sync_projects.py`

**Interfaces:**
- Consumes: builders/parsers (Tasks 5–6), `mirror_store` functions + `ProductSyncView` (Task 8),
  existing `fetch` seam (`_pm_sync_fetch` — `gh api graphql` under the ambient credential; the
  Phase 0 findings confirmed which credential can read user Projects, and since the seam shells
  to `gh` no code change is needed if the ambient credential is the answer — at most a one-time
  `gh auth refresh -s read:project` documented in the findings).
- Produces: per-tick Projects v2 + relations sync. New optional seams on `PmSyncLoop.__init__`
  (all keyword, defaulting to no-ops so every existing test stays green):

```python
list_products: Callable[[], Awaitable[list[ProductSyncView]]]
save_gh_project: Callable[[str, GhProjectView], Awaitable[str]]        # -> gh_project db id
save_project_fields: Callable[[str, dict], Awaitable[None]]
save_project_items: Callable[[str, list[GhProjectItemView]], Awaitable[None]]
save_relations: Callable[[str, list[IssueRelView]], Awaitable[None]]
```

- [ ] **Step 1: Write the failing loop tests**

`server/tests/test_pm_sync_projects.py`, reusing the canned-seam style of
`server/tests/test_pm_sync_loop.py` (copy its `_view`/`_loop` wiring helpers as needed —
read that file first and follow its constructor call exactly, adding the new seams):

```python
"""PmSyncLoop Projects v2 + relations passes (003 Phase A)."""
# Build a loop whose `fetch` returns canned payloads keyed by substring match on the query:
#   "viewer { projectsV2"  -> one open project titled "cc-account-switcher" (node PVT_1)
#   "... on ProjectV2 { fields" -> Status/Score field nodes
#   "... on ProjectV2 { items"  -> one issue item (repo/cc#950, status Ready)
#   "issues(first: 100, states: OPEN" -> relations payload for the repo
# and record every seam call in a `calls` list.


def test_projects_pass_discovers_by_title_and_saves_items():
    # products seam returns one product slug="ccswitch", name="cc-account-switcher",
    # gh_project_db_id=None (not yet linked), repos=["steiner385/cc-account-switcher"]
    # After tick:
    # - save_gh_project called with (product.project_id, GhProjectView(node_id="PVT_1", ...))
    #   (title matched case-insensitively against slug OR name)
    # - save_project_fields called with the returned db id and the parsed cache
    # - save_project_items called with the db id and one GhProjectItemView(number=950, status="Ready")
    ...


def test_relations_pass_runs_per_repo_and_saves():
    # After tick, save_relations called once per product repo with parsed IssueRelView list.
    ...


def test_unmatched_project_title_is_ignored():
    # Discovery returns a project titled "Something else": save_gh_project never called;
    # no exception raised.
    ...


def test_project_fetch_failure_does_not_break_repo_sync():
    # fetch raises for the projects query only; repo issue/PR sync still completes and
    # the loop swallows the projects error (best-effort, mirroring per-repo error handling).
    ...
```

Write these as real tests (the comments above are the required behavior, the fixture payloads
come from Tasks 5–6 test fixtures). Run:
`... -m pytest server/tests/test_pm_sync_projects.py -v` — Expected: FAIL (new seams unknown).

- [ ] **Step 2: Extend `PmSyncLoop`**

In `server/automations/pm_sync.py`:
- Add the five seams as keyword args defaulting to `None`; store on `self`.
- At the end of `_run()` (after the per-repo loop), when `self._list_products` is set, call
  `await self._sync_projects_v2()` wrapped in the same try/except-emit pattern as per-repo sync.
- Implement:

```python
async def _sync_projects_v2(self) -> None:
    products = await self._list_products()
    discovered: list[GhProjectView] = []
    cursor: str | None = None
    for _ in range(_MAX_PAGES_PER_TICK):
        payload = await self._fetch(build_viewer_projects_query(cursor))
        views, cursor, more = parse_viewer_projects(payload)
        discovered.extend(views)
        if not more:
            break
    by_title = {v.title.lower(): v for v in discovered}
    for product in products:
        view = by_title.get((product.slug or "").lower()) or by_title.get(
            (product.name or "").lower())
        gh_db_id, node_id = product.gh_project_db_id, product.gh_node_id
        if view is not None:
            gh_db_id = await self._save_gh_project(product.project_id, view)
            node_id = view.node_id
        if not gh_db_id or not node_id:
            continue
        fields = await self._fetch(build_project_fields_query(node_id))
        await self._save_project_fields(gh_db_id, parse_project_fields(fields))
        items: list[GhProjectItemView] = []
        cursor = None
        for _ in range(_MAX_PAGES_PER_TICK):
            payload = await self._fetch(build_project_items_query(node_id, cursor))
            page, cursor, more = parse_project_items(payload)
            items.extend(page)
            if not more:
                break
        await self._save_project_items(gh_db_id, items)
        for repo in product.repos:
            rels: list[IssueRelView] = []
            cursor = None
            for _ in range(_MAX_PAGES_PER_TICK):
                payload = await self._fetch(build_issue_relations_query(repo, cursor))
                page, cursor, more = parse_issue_relations(payload, repo)
                rels.extend(page)
                if not more:
                    break
            await self._save_relations(repo, rels)
```

(Adapt attribute names to the loop's existing seam-storage convention; keep per-product
try/except so one product's failure doesn't stall the rest, emitting via the existing event
seam like per-repo errors do.)

- [ ] **Step 3: Wire the orchestrator seams**

In `server/sessions/orchestrator.py`, next to `_pm_sync_upsert_items`, add thin async wrappers
following its exact session/thread pattern (look at how it opens a DB session and copy that):

```python
async def _pm_sync_list_products(self) -> list[ProductSyncView]:
    # session-scope + asyncio.to_thread(mirror_store.list_product_views, db)

async def _pm_sync_save_gh_project(self, project_id: str, view: GhProjectView) -> str:
    # mirror_store.upsert_gh_project + commit

async def _pm_sync_save_project_fields(self, gh_db_id: str, cache: dict) -> None:
    # mirror_store.save_field_cache + commit

async def _pm_sync_save_project_items(self, gh_db_id: str, items: list) -> None:
    # mirror_store.upsert_project_items(db, gh_db_id, items, now=_utcnow()) + commit

async def _pm_sync_save_relations(self, repo: str, rels: list) -> None:
    # mirror_store.upsert_relations + commit
```

Pass all five into the `PmSyncLoop(...)` construction site. Extend `ctx.pm_sync_counts` with a
`"gh_projects"` count (number of linked GhProject rows) and surface it in `PmSync.status()`.

- [ ] **Step 4: Run the loop tests and the full server suite**

Run: `... -m pytest server/tests/test_pm_sync_projects.py server/tests/test_pm_sync_loop.py -v`
Expected: new tests PASS; **every pre-existing `test_pm_sync_loop.py` test still passes without
modification** (the seams default to no-ops).
Then: `... -m pytest server/tests/ -x -q` (full suite; serialize — no concurrent runs).

- [ ] **Step 5: Commit, run the four-step gate, open PR 1**

```bash
git add server/automations/pm_sync.py server/sessions/orchestrator.py server/tests/test_pm_sync_projects.py
git commit -F /tmp/commit-msg.txt   # "feat(003): Phase A — Projects v2 + relations mirror sync" + trailer
```

Gate (all four, from repo root with the main-checkout venv):
`ruff check src/ server/` · `mypy server/ --ignore-missing-imports` · `pytest tests/` ·
`pytest server/tests/`. All green, then:

```bash
git push -u origin feat/003-phase-a-mirror
gh pr create --title "003 Phase A: read-only Projects v2 / deps / sub-issue mirror" --body-file /tmp/pr-body.txt
gh pr merge <#> --squash --auto
```

PR body: `Refs #<003 epic issue number>` (partial — do not use `Fixes`).

---

## Phase A — Drift audit, API & UI (PR 2 of 2: `feat/003-phase-a-deps-ui`)

Branch off `origin/master` **after PR 1 merges** (`git fetch` first — the mirror tables must be
on master).

### Task 10: Drift-audit computation (pure)

**Files:**
- Create: `server/pm/drift.py`
- Test: `server/tests/test_drift.py`

**Interfaces:**
- Consumes: `parse_checklist_refs` from `server.automations.backlog_deps`.
- Produces (Task 11 consumes):

```python
@dataclass(frozen=True)
class DepEdgeView:      # projection of a WorkItemDep row
    src_ref: str
    dst_ref: str
    source: str
    active: bool
    dst_state: str | None    # mirrored WorkItem.state of dst, None if unmirrored

@dataclass
class DriftReport:
    prose_only: list[dict]          # [{"src": ref, "dst": ref}] prose/order edge with no native twin
    unresolved_targets: list[dict]  # [{"src": ref, "dst": ref, "source": s}] dst unmirrored
    missing_from_project: list[str] # open item refs not on the product's Project board
    checklist_epics: list[str]      # open items with task-list refs but no sub-issues

def compute_drift(edges: list[DepEdgeView], open_refs: set[str],
                  project_refs: set[str],
                  bodies: Mapping[str, str | None]) -> DriftReport
```

- [ ] **Step 1: Write the failing tests**

`server/tests/test_drift.py`:

```python
from server.pm.drift import DepEdgeView, compute_drift

R = "steiner385/cc-account-switcher"


def _edge(src, dst, source, active=True, dst_state="open"):
    return DepEdgeView(src_ref=src, dst_ref=dst, source=source,
                       active=active, dst_state=dst_state)


def test_prose_edge_without_native_twin_is_drift():
    edges = [
        _edge(f"{R}#950", f"{R}#949", "prose"),
        _edge(f"{R}#951", f"{R}#950", "prose"),
        _edge(f"{R}#951", f"{R}#950", "native"),   # twin exists -> not drift
        _edge(f"{R}#952", f"{R}#951", "order"),    # order counts like prose
        _edge(f"{R}#953", f"{R}#952", "prose", active=False),  # inactive ignored
    ]
    report = compute_drift(edges, open_refs=set(), project_refs=set(), bodies={})
    assert report.prose_only == [
        {"src": f"{R}#950", "dst": f"{R}#949"},
        {"src": f"{R}#952", "dst": f"{R}#951"},
    ]


def test_unresolved_missing_and_checklist_epics():
    edges = [_edge(f"{R}#950", "steiner385/KinDash#12", "native", dst_state=None)]
    report = compute_drift(
        edges,
        open_refs={f"{R}#950", f"{R}#960"},
        project_refs={f"{R}#950"},
        bodies={f"{R}#960": "- [ ] #961\n- [x] #962", f"{R}#950": None},
    )
    assert report.unresolved_targets == [
        {"src": f"{R}#950", "dst": "steiner385/KinDash#12", "source": "native"}]
    assert report.missing_from_project == [f"{R}#960"]
    assert report.checklist_epics == [f"{R}#960"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `... -m pytest server/tests/test_drift.py -v`
Expected: FAIL with `ModuleNotFoundError: server.pm.drift`.

- [ ] **Step 3: Implement `server/pm/drift.py`**

```python
"""Drift audit for the 003 mirror: where GitHub's declared planning state and the
conventions ccswitch consumes disagree (spec 003 Phase A deliverable)."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from server.automations.backlog_deps import parse_checklist_refs


@dataclass(frozen=True)
class DepEdgeView:
    src_ref: str
    dst_ref: str
    source: str
    active: bool
    dst_state: str | None


@dataclass
class DriftReport:
    prose_only: list[dict] = field(default_factory=list)
    unresolved_targets: list[dict] = field(default_factory=list)
    missing_from_project: list[str] = field(default_factory=list)
    checklist_epics: list[str] = field(default_factory=list)


def compute_drift(edges: list[DepEdgeView], open_refs: set[str],
                  project_refs: set[str],
                  bodies: Mapping[str, str | None]) -> DriftReport:
    report = DriftReport()
    active = [e for e in edges if e.active]
    native = {(e.src_ref, e.dst_ref) for e in active if e.source == "native"}
    seen: set[tuple[str, str]] = set()
    for edge in active:
        pair = (edge.src_ref, edge.dst_ref)
        if edge.source in ("prose", "order") and pair not in native and pair not in seen:
            seen.add(pair)
            report.prose_only.append({"src": edge.src_ref, "dst": edge.dst_ref})
        if edge.dst_state is None:
            report.unresolved_targets.append(
                {"src": edge.src_ref, "dst": edge.dst_ref, "source": edge.source})
    report.missing_from_project = sorted(open_refs - project_refs)
    for ref in sorted(open_refs):
        repo = ref.rsplit("#", 1)[0]
        if parse_checklist_refs(bodies.get(ref), repo):
            report.checklist_epics.append(ref)
    return report
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `... -m pytest server/tests/test_drift.py -v`. Expected: 2 PASS.

- [ ] **Step 5: Commit**

```bash
git fetch origin && git checkout -b feat/003-phase-a-deps-ui origin/master
git add server/pm/drift.py server/tests/test_drift.py
git commit -F /tmp/commit-msg.txt   # "feat(003): drift-audit computation" + trailer
```

### Task 11: Deps + drift API endpoint

**Files:**
- Modify: `server/api/pm.py`
- Test: `server/tests/test_pm_deps_api.py`

**Interfaces:**
- Consumes: mirror tables (Task 4), `compute_drift`/`DepEdgeView` (Task 10).
- Produces: `GET /api/pm/projects/{slug}/deps` → (Task 12's frontend contract)

```json
{
  "project": {"number": 7, "title": "cc-account-switcher", "lastSyncedAt": "..."} ,
  "edges": [{"src": "o/r#950", "dst": "o/r#12", "source": "native",
             "active": true, "dstState": "open"}],
  "drift": {"proseOnly": [{"src": "...", "dst": "..."}],
            "unresolvedTargets": [{"src": "...", "dst": "...", "source": "..."}],
            "missingFromProject": ["o/r#960"], "checklistEpics": ["o/r#960"]}
}
```

`project` is `null` when no GhProject is linked yet (mirror still serves deps + drift computed
with `project_refs=set()` — but `missingFromProject` is forced to `[]` in that case, so an
unlinked board doesn't flag every item).

- [ ] **Step 1: Write the failing API tests**

`server/tests/test_pm_deps_api.py` — copy the app/client/auth fixture pattern from the existing
PM API tests in `server/tests/` (find the file testing `GET /api/pm/projects` and mirror its
setup exactly, including the tailnet-allowlist pinning). Seed via the ORM like
`test_mirror_store.py`'s `_seed`, plus one `GhProject` + one `GhProjectItem` + `WorkItemDep`
rows, then:

```python
def test_deps_endpoint_returns_edges_and_drift(client, db):
    # seed: project slug "ccswitch"; work items 945/950/960 open; GhProject number 7 linked;
    # GhProjectItem for 950; WorkItemDep 950->KinDash#12 native active (dst unmirrored);
    # WorkItemDep 950->949 prose active
    resp = client.get("/api/pm/projects/ccswitch/deps")
    assert resp.status_code == 200
    body = resp.json()
    assert body["project"]["number"] == 7
    srcs = {e["src"] for e in body["edges"]}
    assert "steiner385/cc-account-switcher#950" in srcs
    assert body["drift"]["proseOnly"] == [
        {"src": "steiner385/cc-account-switcher#950",
         "dst": "steiner385/cc-account-switcher#949"}]
    assert "steiner385/cc-account-switcher#960" in body["drift"]["missingFromProject"]


def test_deps_endpoint_404_on_unknown_slug(client):
    assert client.get("/api/pm/projects/nope/deps").status_code == 404
```

Run: `... -m pytest server/tests/test_pm_deps_api.py -v` — Expected: FAIL (404 route missing).

- [ ] **Step 2: Implement the route in `server/api/pm.py`**

Follow the file's existing route/session/serialization idioms (same dependency-injected DB
session, same camelCase response style as the project detail route):

```python
@router.get("/projects/{slug}/deps")
def project_deps(slug: str, db: Session = Depends(get_db)):  # match file's actual dep names
    project = db.query(Project).filter_by(slug=slug).first()
    if project is None:
        raise HTTPException(status_code=404, detail="unknown project")
    repos = {r.repo for r in db.query(ProjectRepo).filter_by(project_id=project.id) if r.repo}
    items = {f"{w.repo}#{w.number}": w for w in db.query(WorkItem)
             .filter(WorkItem.repo.in_(repos), WorkItem.kind == "issue").all()}
    open_refs = {ref for ref, w in items.items() if (w.state or "open") == "open"}
    gh = db.query(GhProject).filter_by(project_id=project.id).first()
    project_refs: set[str] = set()
    if gh is not None:
        linked = db.query(GhProjectItem).filter_by(gh_project_id=gh.id).all()
        by_id = {w.id: ref for ref, w in items.items()}
        project_refs = {by_id[i.work_item_id] for i in linked
                        if i.work_item_id in by_id}
    dep_rows = [d for d in db.query(WorkItemDep).all()
                if (d.src_ref or "").rsplit("#", 1)[0] in repos]
    edges = [DepEdgeView(
        src_ref=d.src_ref or "", dst_ref=d.dst_ref or "", source=d.source or "",
        active=bool(d.active),
        dst_state=items[d.dst_ref].state if d.dst_ref in items else None)
        for d in dep_rows]
    drift = compute_drift(edges, open_refs, project_refs,
                          {ref: items[ref].body for ref in open_refs})
    if gh is None:
        drift.missing_from_project = []
    return {
        "project": None if gh is None else {
            "number": gh.gh_project_number, "title": gh.title,
            "lastSyncedAt": gh.last_synced_at.isoformat() if gh.last_synced_at else None},
        "edges": [{"src": e.src_ref, "dst": e.dst_ref, "source": e.source,
                   "active": e.active, "dstState": e.dst_state} for e in edges],
        "drift": {"proseOnly": drift.prose_only,
                  "unresolvedTargets": drift.unresolved_targets,
                  "missingFromProject": drift.missing_from_project,
                  "checklistEpics": drift.checklist_epics},
    }
```

- [ ] **Step 3: Run tests to verify they pass**

Run: `... -m pytest server/tests/test_pm_deps_api.py -v`. Expected: 2 PASS.

- [ ] **Step 4: Commit**

```bash
git add server/api/pm.py server/tests/test_pm_deps_api.py
git commit -F /tmp/commit-msg.txt   # "feat(003): deps + drift API endpoint" + trailer
```

### Task 12: Web — fetcher/hook + DependencyPanel in the PM tab

**Files:**
- Modify: `web/src/api/pm.ts` (types + fetcher + hook — new shared hooks go in `api/pm.ts`,
  NOT `api/hooks.ts`)
- Create: `web/src/projects/DependencyPanel.tsx`
- Modify: `web/src/projects/ProjectsTab.tsx`
- Test: `web/src/projects/__tests__/DependencyPanel.test.tsx`
- Modify: `web/src/projects/__tests__/ProjectsTab.test.tsx` (its `vi.mock('@/api/pm')` must gain
  the new exports or every existing test in it breaks)

**Interfaces:**
- Consumes: `GET /api/pm/projects/{slug}/deps` (Task 11 shape, camelCase).
- Produces:

```typescript
export interface DepEdge { src: string; dst: string; source: string; active: boolean; dstState: string | null }
export interface DriftSummary {
  proseOnly: { src: string; dst: string }[]
  unresolvedTargets: { src: string; dst: string; source: string }[]
  missingFromProject: string[]
  checklistEpics: string[]
}
export interface ProjectDeps {
  project: { number: number | null; title: string | null; lastSyncedAt: string | null } | null
  edges: DepEdge[]
  drift: DriftSummary
}
export async function fetchProjectDeps(slug: string): Promise<ProjectDeps>
export function useProjectDeps(slug: string, enabled?: boolean)  // useQuery, key ['pm','deps',slug]
```

- [ ] **Step 1: Write the failing component test**

`web/src/projects/__tests__/DependencyPanel.test.tsx`, following `ProjectsTab.test.tsx`'s
QueryClient wrapper:

```tsx
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import { vi } from 'vitest'
import { DependencyPanel } from '../DependencyPanel'
import * as pm from '@/api/pm'

vi.mock('@/api/pm', async (importOriginal) => ({
  ...(await importOriginal<typeof pm>()),
  fetchProjectDeps: vi.fn(),
}))

function renderPanel() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <DependencyPanel slug="ccswitch" />
    </QueryClientProvider>,
  )
}

it('renders blocked-by edges grouped by source item and drift counts', async () => {
  vi.mocked(pm.fetchProjectDeps).mockResolvedValue({
    project: { number: 7, title: 'cc-account-switcher', lastSyncedAt: null },
    edges: [
      { src: 'o/r#950', dst: 'o/r#12', source: 'native', active: true, dstState: 'open' },
      { src: 'o/r#950', dst: 'o/r#949', source: 'prose', active: true, dstState: 'closed' },
      { src: 'o/r#951', dst: 'o/r#950', source: 'order', active: false, dstState: 'open' },
    ],
    drift: { proseOnly: [{ src: 'o/r#950', dst: 'o/r#949' }], unresolvedTargets: [],
             missingFromProject: ['o/r#960'], checklistEpics: [] },
  })
  renderPanel()
  await waitFor(() => expect(screen.getByText('o/r#950')).toBeInTheDocument())
  expect(screen.getByText('o/r#12')).toBeInTheDocument()
  // inactive edges are not rendered
  expect(screen.queryByText('o/r#951')).not.toBeInTheDocument()
  expect(screen.getByText(/1 prose-only/i)).toBeInTheDocument()
  expect(screen.getByText(/1 off board/i)).toBeInTheDocument()
})

it('renders an empty state when the mirror has no edges', async () => {
  vi.mocked(pm.fetchProjectDeps).mockResolvedValue({
    project: null, edges: [],
    drift: { proseOnly: [], unresolvedTargets: [], missingFromProject: [], checklistEpics: [] },
  })
  renderPanel()
  await waitFor(() =>
    expect(screen.getByText(/no mirrored dependencies/i)).toBeInTheDocument())
})
```

Run: `(cd web && npx vitest run src/projects/__tests__/DependencyPanel.test.tsx)`
Expected: FAIL (module not found).

- [ ] **Step 2: Implement fetcher + hook in `web/src/api/pm.ts`**

Follow the file's existing fetcher idiom (same base-path helper / error handling as
`fetchProjects`), add the interfaces from the block above, and:

```typescript
export async function fetchProjectDeps(slug: string): Promise<ProjectDeps> {
  // identical request helper the other pm fetchers use:
  return request(`/api/pm/projects/${encodeURIComponent(slug)}/deps`)
}

export function useProjectDeps(slug: string, enabled = true) {
  return useQuery({
    queryKey: ['pm', 'deps', slug],
    queryFn: () => fetchProjectDeps(slug),
    enabled,
    staleTime: 5_000,
    refetchInterval: 15_000,
  })
}
```

(Import `useQuery` from `@tanstack/react-query` at the top of `api/pm.ts`; match the 5s/15s
cadence the other PM hooks use.)

- [ ] **Step 3: Implement `web/src/projects/DependencyPanel.tsx`**

Match the Tailwind/shadcn idioms of the surrounding `projects/` components (reuse the same
card/list primitives `ProjectsTab` uses — check its imports first):

```tsx
import { useProjectDeps } from '@/api/pm'

export function DependencyPanel({ slug }: { slug: string }) {
  const { data, isLoading } = useProjectDeps(slug)
  if (isLoading || !data) return null
  const active = data.edges.filter((e) => e.active)
  const bySrc = new Map<string, typeof active>()
  for (const edge of active) {
    bySrc.set(edge.src, [...(bySrc.get(edge.src) ?? []), edge])
  }
  const { drift } = data
  const badges = [
    drift.proseOnly.length && `${drift.proseOnly.length} prose-only`,
    drift.unresolvedTargets.length && `${drift.unresolvedTargets.length} unresolved`,
    drift.missingFromProject.length && `${drift.missingFromProject.length} off board`,
    drift.checklistEpics.length && `${drift.checklistEpics.length} checklist epics`,
  ].filter(Boolean) as string[]
  if (active.length === 0 && badges.length === 0) {
    return <p className="text-sm text-muted-foreground">No mirrored dependencies.</p>
  }
  return (
    <div className="space-y-2">
      {badges.length > 0 && (
        <div className="flex flex-wrap gap-1">
          {badges.map((b) => (
            <span key={b} className="rounded bg-amber-500/15 px-2 py-0.5 text-xs text-amber-600">
              {b}
            </span>
          ))}
        </div>
      )}
      {[...bySrc.entries()].map(([src, edges]) => (
        <div key={src} className="text-sm">
          <span className="font-medium">{src}</span>
          <span className="text-muted-foreground"> blocked by </span>
          {edges.map((e, i) => (
            <span key={e.dst + e.source}>
              {i > 0 && ', '}
              <span className={e.dstState === 'closed' ? 'line-through opacity-60' : ''}>
                {e.dst}
              </span>
              <span className="text-xs text-muted-foreground"> ({e.source})</span>
            </span>
          ))}
        </div>
      ))}
    </div>
  )
}
```

- [ ] **Step 4: Mount it in `ProjectsTab` and update its test mocks**

In `web/src/projects/ProjectsTab.tsx`, render `<DependencyPanel slug={project.slug} />` inside
the existing per-project card body (below the repo/edge info, only when `project.slug` is
non-null). In `ProjectsTab.test.tsx`, extend the `vi.mock('@/api/pm')` factory with
`fetchProjectDeps: vi.fn().mockResolvedValue({ project: null, edges: [], drift: { proseOnly: [],
unresolvedTargets: [], missingFromProject: [], checklistEpics: [] } })` and pass `useProjectDeps`
through if the mock replaces the whole module (spread `importOriginal` won't cover it if the
existing mock is a full replacement — mirror whichever style that file already uses).

- [ ] **Step 5: Run the FULL web gate**

Run: `(cd web && npx vitest run && npx tsc --noEmit && npx eslint src)`
Expected: everything green — the full run (not changed-file-only) is what catches broken manual
mocks in Dashboard/DesktopShell tests.

- [ ] **Step 6: Commit, run the four-step server gate (API file changed), open PR 2**

```bash
git add web/src/api/pm.ts web/src/projects/DependencyPanel.tsx web/src/projects/ProjectsTab.tsx web/src/projects/__tests__/
git commit -F /tmp/commit-msg.txt   # "feat(003): dependency panel + drift badges in PM tab" + trailer
git push -u origin feat/003-phase-a-deps-ui
gh pr create --title "003 Phase A: drift audit endpoint + dependency panel" --body-file /tmp/pr-body.txt
gh pr merge <#> --squash --auto
```

PR body: `Refs #<003 epic issue number>`.

---

## Done criteria for this plan

- Phase 0 findings doc merged; every Q1–Q4 row filled from real API output; spec reconciled.
- Both Phase A PRs merged with auto-merge; mirror tables populate on the live daemon within one
  pm-sync interval of a hand-created "cc-account-switcher" Project v2 (dogfooding structure from
  the spec's epic issue).
- `GET /api/pm/projects/ccswitch/deps` returns real edges for the 003 epic's sub-issue chain, and
  the PM tab renders them.
- Nothing in this plan wrote to GitHub outside the spike's scratch repo/Project.
- Not in scope (explicitly deferred to Phase B+): reconciler, journal writes, migration mode,
  queue flip, plan materialization, Initiatives-tab merge, label retirement.

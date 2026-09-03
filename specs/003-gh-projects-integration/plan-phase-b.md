# 003 Phase B Implementation Plan — Reconciler & One-Time Migration

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Converge GitHub Projects v2 fields (`Status`, `Score`, `Stage`), board membership, and
native blocked-by edges toward the state ccswitch's decision layer has already decided — via a
leader-gated, SHA-fenced, kill-switched, write-ahead-journaled, blast-capped reconciler whose
first `apply` run *is* the one-time migration.

**Architecture:** Four pure cores (`score`, `desired_state`, `reconcile_diff`, `gh_mutations`)
turn mirror rows + decision-layer facts into a list of `Mutation`s and GraphQL batch documents.
Two session-level modules (`journal`, `reconcile_inputs`) own persistence. `GhReconcileLoop`
(seam-injected like `PmSyncLoop`) runs the converge cycle per product; the orchestrator wires
seams and reuses the Phase A PAT-preferring GraphQL fetch for writes. `pm_sync` gains echo
suppression so the reconciler's own writes are never mistaken for human edits.

**Tech Stack:** Python 3.12 / SQLAlchemy 2.x (tz-aware UTC) / Alembic record-only revisions /
GitHub GraphQL via `gh api graphql` under `CCSWITCH_GH_PROJECT_TOKEN` / existing automation
framework (`ConfigField`, `AutomationStatus`, `AutomationContext.emit`).

**Spec:** `specs/003-gh-projects-integration/spec.md` (§3 ownership matrix, §4 schema, §6
reconciler, §8 Phase B, §9 testing, §10 failure modes). Phase 0 facts:
`specs/003-gh-projects-integration/phase0-findings.md` (mutation names + 2026-09-03 credential
addendum). Prior plan for Phase A conventions: `specs/003-gh-projects-integration/plan.md`.

## Global Constraints

- **Branching:** `git fetch origin` then `git checkout -b <branch> origin/master` — never local
  `master`. PRs in order: `spike/003-phase-b-probe` → `feat/003-phase-b-cores` →
  `feat/003-phase-b-loop` → `feat/003-phase-b-api-runbook`. Each later branch is cut from
  `origin/master` only after the previous PR merges.
- **Commit trailer** `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`; commit messages
  and PR bodies via files (`git commit -F`, `gh pr create --body-file`); PR bodies `Refs #973` and
  end with the Claude Code trailer. Every completed PR: non-draft + `gh pr merge <#> --squash --auto`.
- **Test env:** `env -u CCSWITCH_TAILNET_ALLOWLIST -u CCSWITCH_SESSIOND_ENDPOINT
  -u CCSWITCH_SESSIOND_CLIENT_CERT -u CCSWITCH_DAEMON_ROLE CCSWITCH_DEV=1
  /home/tony/GitHub/ccswitch/.venv/bin/python -m pytest …`. Four-step gate before every push:
  `ruff check src/ server/` · `mypy server/ --ignore-missing-imports` · `pytest tests/` ·
  `pytest server/tests/` (full server suite: run ONCE, foreground-or-controller-owned, never from a
  subagent's background task; known suite-order flake: `test_repo_discovery.py` ×3 pass in isolation).
- **Schema:** new columns nullable; Alembic `0019_reconciler.py` chained off
  `0018_gh_projects_mirror`; adding a MODEL means a local `CCSWITCH_TEST_PG_URL` run
  (`postgresql+psycopg://tony@localhost/ccswitch_test` worked in Phase A) and `_FK_LINKS` entries.
- **Credential (verified 2026-09-03):** all Projects v2 reads/writes use
  `CCSWITCH_GH_PROJECT_TOKEN` (classic PAT, `project`+`repo`); the App token is blind to user boards.
  Reuse the orchestrator's existing `_pm_sync_fetch_projects(owner, query)` for mutation documents —
  do not add a second token-resolution path.
- **Mutation names/inputs are fixed by Phase 0:** `addBlockedBy`/`removeBlockedBy` with
  `issueId` (the blocked issue) + `blockingIssueId` (the blocker); `addProjectV2ItemById(projectId,
  contentId)`; `updateProjectV2ItemFieldValue(projectId, itemId, fieldId, value:{number |
  singleSelectOptionId})`; `createProjectV2Field(projectId, dataType, name[, singleSelectOptions])`.
  Batches are aliased mutations, **≤ 20 per document**.
- **Spec §6 safety rails, exact values:** kill switch `CCSWITCH_GH_RECONCILE_ENABLED` default
  **off**; `dry_run` journals diffs with `applied=false`; blast cap config default **50**
  mutations/cycle, exceeding halts loudly; a mutation failing **twice** parks as needs-human; the
  reconciler runs on the **primary** only and refuses every cycle unless the daemon's git SHA
  matches `origin/master`.
- **Ownership rule (spec §3), verbatim:** push a value only when the ccswitch-side decision
  timestamp is strictly newer than the mirrored value's `gh_updated_at`. Direct GitHub edits win.
- **Field vocabulary (spec §4):** `Status` options `Backlog · Ready · In progress · Blocked ·
  Needs human · Done`; `Score` NUMBER; `Stage` SINGLE_SELECT with options = `INITIATIVE_STAGES`
  (`intake, design, plan, build, deploy, maintain, done`).
- **Single sources of truth:** RICE formula lives in `server/pm/score.py` only (spec §8 comment in
  `Initiative`: `reach×impact×confidence/effort`); prose/checklist parsing stays in
  `server/automations/backlog_deps.py`; session status vocabulary from `server/sessions/status.py`.
- **No GitHub writes outside the reconciler's journaled path** — except the Task 1 spike's scratch
  board, which it deletes.

---

## PR B0 — Verification spike (branch `spike/003-phase-b-probe`)

### Task 1: Probe the Phase B mutation set the reconciler depends on

Phase 0 verified the core mutations but not: SINGLE_SELECT field creation with options, editing
the built-in `Status` field's options, a 20-alias batch document, `clearProjectV2ItemFieldValue`,
and `rateLimit` cost reporting. Constitution III: verify before building on them.

**Files:**
- Modify: `scripts/spike_gh_projects_v2.py` (add `phaseb` subcommand)
- Modify: `specs/003-gh-projects-integration/phase0-findings.md` (append "Phase B probe" section)

**Interfaces:**
- Produces: confirmed input shapes for `createProjectV2Field` (SINGLE_SELECT), `updateProjectV2Field`
  (Status options), `clearProjectV2ItemFieldValue`, and the `rateLimit` fragment — Task 6's builders
  copy them verbatim.

- [ ] **Step 1: Add the `phaseb` probe (self-cleaning: creates and deletes a scratch board)**

```python
def pat_token() -> str:
    for line in open("/etc/ccswitch/daemon.env"):
        if line.strip().startswith("CCSWITCH_GH_PROJECT_TOKEN="):
            return line.strip().split("=", 1)[1]
    raise SystemExit("CCSWITCH_GH_PROJECT_TOKEN not in daemon.env")


def probe_phaseb() -> None:
    tok = pat_token()
    g = lambda q: gh_graphql(q, token=tok)  # noqa: E731
    uid = g('query { user(login: "%s") { id } }' % OWNER)["data"]["user"]["id"]
    proj = g('mutation { createProjectV2(input: {ownerId: "%s", title: "spike-003-phaseb (safe to delete)"}) '
             '{ projectV2 { id } } }' % uid)
    pid = proj["data"]["createProjectV2"]["projectV2"]["id"]
    print("=== single-select field with options (Stage) ===")
    opts = ", ".join('{name: "%s", color: GRAY, description: ""}' % s
                     for s in ("intake", "design", "plan", "build", "deploy", "maintain", "done"))
    print(json.dumps(g('mutation { createProjectV2Field(input: {projectId: "%s", dataType: SINGLE_SELECT, '
                       'name: "Stage", singleSelectOptions: [%s]}) { projectV2Field { '
                       '... on ProjectV2SingleSelectField { id options { id name } } } } }' % (pid, opts)),
                     indent=2)[:600])
    print("=== fields incl. built-in Status + its options ===")
    fields = g('query { node(id: "%s") { ... on ProjectV2 { fields(first: 20) { nodes { '
               '... on ProjectV2SingleSelectField { id name options { id name } } '
               '... on ProjectV2FieldCommon { id name } } } } } }' % pid)
    nodes = fields["data"]["node"]["fields"]["nodes"]
    status = next(n for n in nodes if n.get("name") == "Status")
    print(json.dumps(status, indent=2))
    print("=== updateProjectV2Field: replace Status options with the spec vocabulary ===")
    new_opts = ", ".join('{name: "%s", color: GRAY, description: ""}' % s
                         for s in ("Backlog", "Ready", "In progress", "Blocked", "Needs human", "Done"))
    print(json.dumps(g('mutation { updateProjectV2Field(input: {fieldId: "%s", singleSelectOptions: [%s]}) '
                       '{ projectV2Field { ... on ProjectV2SingleSelectField { options { id name } } } } }'
                       % (status["id"], new_opts)), indent=2)[:800])
    print("=== 20-alias batch: add 10 issues, then set Status on each (rateLimit reported) ===")
    issues = g('query { repository(owner: "%s", name: "cc-account-switcher") { issues(first: 10, states: OPEN) '
               '{ nodes { id number } } } }' % OWNER)["data"]["repository"]["issues"]["nodes"]
    adds = " ".join('a%d: addProjectV2ItemById(input: {projectId: "%s", contentId: "%s"}) { item { id } }'
                    % (i, pid, n["id"]) for i, n in enumerate(issues))
    added = g("mutation { %s rateLimit { cost remaining resetAt } }" % adds)
    print("add batch rateLimit:", added["data"].get("rateLimit"), "errors:", added.get("errors"))
    status_after = g('query { node(id: "%s") { ... on ProjectV2 { field(name: "Status") { '
                     '... on ProjectV2SingleSelectField { id options { id name } } } } } }' % pid)
    sf = status_after["data"]["node"]["field"]
    ready = next(o["id"] for o in sf["options"] if o["name"] == "Ready")
    sets = " ".join('s%d: updateProjectV2ItemFieldValue(input: {projectId: "%s", itemId: "%s", fieldId: "%s", '
                    'value: {singleSelectOptionId: "%s"}}) { projectV2Item { id } }'
                    % (i, pid, added["data"]["a%d" % i]["item"]["id"], sf["id"], ready)
                    for i in range(len(issues)))
    setr = g("mutation { %s rateLimit { cost remaining resetAt } }" % sets)
    print("set batch rateLimit:", setr["data"].get("rateLimit"), "errors:", setr.get("errors"))
    print("=== clearProjectV2ItemFieldValue ===")
    print(json.dumps(g('mutation { clearProjectV2ItemFieldValue(input: {projectId: "%s", itemId: "%s", fieldId: "%s"}) '
                       '{ projectV2Item { id } } }' % (pid, added["data"]["a0"]["item"]["id"], sf["id"]))
                     .get("errors"), indent=2))
    print("=== cleanup ===")
    print(g('mutation { deleteProjectV2(input: {projectId: "%s"}) { clientMutationId } }' % pid).get("errors"))
```

Register it: `{"auth": probe_auth, "introspect": probe_introspect, "mutations": probe_mutations,
"phaseb": probe_phaseb}` in `main()`.

- [ ] **Step 2: Run it and keep the log**

Run: `python scripts/spike_gh_projects_v2.py phaseb 2>&1 | tee /tmp/spike-003-phaseb.log`
Expected: every section prints either a success payload or a structured GraphQL error; the scratch
board is deleted at the end (`gh api graphql` listing `user(login:).projectsV2` no longer shows
`spike-003-phaseb`). Both outcomes are findings; a failure of `updateProjectV2Field` means Task 9's
`ensure_fields` must create a *custom* `Status`-like field instead of editing the built-in one —
record which.

- [ ] **Step 3: Append the findings**

Add to `phase0-findings.md`:

```markdown
## Phase B probe — <date>
| Probe | Result | Input shape confirmed |
|---|---|---|
| createProjectV2Field SINGLE_SELECT + options | <y/n> | singleSelectOptions: [{name,color,description}] |
| updateProjectV2Field replaces built-in Status options | <y/n + note> | fieldId + singleSelectOptions |
| 10-alias add + 10-alias set batches | <y/n> | rateLimit cost per batch: add=<n> set=<n>, remaining=<n> |
| clearProjectV2ItemFieldValue | <y/n> | projectId,itemId,fieldId |
**Implication for Task 9:** <edit built-in Status | create custom field>.
```

- [ ] **Step 4: Commit + PR**

```bash
git fetch origin && git checkout -b spike/003-phase-b-probe origin/master
git add scripts/spike_gh_projects_v2.py specs/003-gh-projects-integration/phase0-findings.md
git commit -F /tmp/msg.txt   # "spike(003): Phase B mutation probe — field options, batches, rateLimit" + trailer
git push -u origin spike/003-phase-b-probe
gh pr create --title "003 Phase B: mutation probe findings" --body-file /tmp/pr.txt
gh pr merge <#> --squash --auto
```

---

## PR B1 — Schema + pure cores (branch `feat/003-phase-b-cores`)

### Task 2: Schema + kill switch + node-id capture

The reconciler needs issue node IDs (for `addProjectV2ItemById` content IDs and `addBlockedBy`),
a batch grouping key on the journal, and the env kill switch.

**Files:**
- Modify: `server/db/models.py` (`WorkItem`, `GhWriteJournal`)
- Create: `server/db/migrations/versions/0019_reconciler.py`
- Modify: `server/config.py` (both `Settings` classes)
- Modify: `server/pm/github_sync.py` (issues query + `WorkItemView`), `server/pm/gh_projects.py`
  (relations query + `IssueRelView`), `server/pm/mirror_store.py` (`upsert_relations` sets node id),
  `server/sessions/orchestrator.py` (`_pm_sync_upsert_items` sets node id)
- Test: `server/tests/test_pm_github_sync.py`, `server/tests/test_gh_projects_queries.py`,
  `server/tests/test_mirror_store.py`, `server/tests/test_config_reconcile_switch.py`

**Interfaces:**
- Produces: `WorkItem.gh_node_id: str | None`; `GhWriteJournal.batch_id: str | None`;
  `Settings.gh_reconcile_enabled: bool` (env `CCSWITCH_GH_RECONCILE_ENABLED`, default False);
  `WorkItemView.node_id: str | None`; `IssueRelView.node_id: str | None`.

- [ ] **Step 1: Failing tests**

```python
# server/tests/test_pm_github_sync.py (append)
def test_issue_page_captures_node_id():
    payload = _issue_payload()
    payload["data"]["repository"]["issues"]["nodes"][0]["id"] = "I_abc"
    items, _, _ = parse_page(payload, "steiner385/cc-account-switcher", "issue")
    assert items[0].node_id == "I_abc"
    assert "nodes { id " in build_issues_query("o/r", None, None)


# server/tests/test_gh_projects_queries.py (append)
def test_relations_capture_node_id():
    payload = _relations_payload()
    payload["data"]["repository"]["issues"]["nodes"][0]["id"] = "I_945"
    rels, _, _ = parse_issue_relations(payload, "steiner385/cc-account-switcher")
    assert rels[0].node_id == "I_945"
    assert "nodes { id number" in build_issue_relations_query("steiner385/cc-account-switcher", None)


# server/tests/test_mirror_store.py (append)
def test_upsert_relations_stores_node_ids():
    db = _db(); _seed(db)
    mirror_store.upsert_relations(db, REPO, [IssueRelView(
        repo=REPO, number=945, blocked_by=[], parent_ref=None, sub_issue_refs=[], node_id="I_945")])
    assert db.query(WorkItem).filter_by(number=945).one().gh_node_id == "I_945"


# server/tests/test_config_reconcile_switch.py (new)
import os
from server.config import Settings

def test_reconcile_kill_switch_defaults_off(monkeypatch):
    monkeypatch.delenv("CCSWITCH_GH_RECONCILE_ENABLED", raising=False)
    assert Settings().gh_reconcile_enabled is False

def test_reconcile_kill_switch_reads_env(monkeypatch):
    monkeypatch.setenv("CCSWITCH_GH_RECONCILE_ENABLED", "true")
    assert Settings().gh_reconcile_enabled is True
```

(Adapt `parse_page`'s exact signature to the file — read it first; the assertion is that the node
`id` lands on the view.)

- [ ] **Step 2: Run → FAIL** (`AttributeError: node_id` / `gh_reconcile_enabled`).

- [ ] **Step 3: Implement**

Models:
```python
# WorkItem, after url:
    gh_node_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
# GhWriteJournal, after initiated_by:
    batch_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
```
Migration `0019_reconciler.py` (`down_revision = "0018_gh_projects_mirror"`):
```python
def upgrade() -> None:
    op.add_column("work_items", sa.Column("gh_node_id", sa.String(), nullable=True))
    op.create_index("ix_work_items_gh_node_id", "work_items", ["gh_node_id"])
    op.add_column("gh_write_journal", sa.Column("batch_id", sa.String(), nullable=True))
    op.create_index("ix_gh_write_journal_batch_id", "gh_write_journal", ["batch_id"])

def downgrade() -> None:
    op.drop_index("ix_gh_write_journal_batch_id", table_name="gh_write_journal")
    op.drop_column("gh_write_journal", "batch_id")
    op.drop_index("ix_work_items_gh_node_id", table_name="work_items")
    op.drop_column("work_items", "gh_node_id")
```
Settings (pydantic branch, next to `self_model_switch_enabled`):
```python
    gh_reconcile_enabled: bool = Field(default=False, alias="CCSWITCH_GH_RECONCILE_ENABLED")
```
and the dataclass fallback:
```python
    gh_reconcile_enabled: bool = dataclasses.field(default_factory=lambda: os.environ.get(
        "CCSWITCH_GH_RECONCILE_ENABLED", "false").lower() in ("1", "true", "yes", "on"))
```
Queries: add `id` to the `nodes { … }` selection of `build_issues_query` (github_sync.py) and
`build_issue_relations_query` (gh_projects.py); add `node_id: str | None = None` to `WorkItemView`
and `IssueRelView`; parse `node.get("id")`. In `mirror_store.upsert_relations`, after
`src.is_epic = …`: `if rel.node_id: src.gh_node_id = rel.node_id`. In orchestrator
`_pm_sync_upsert_items`, alongside the other mirror columns: `row.gh_node_id = view.node_id or
row.gh_node_id`.

- [ ] **Step 4: Run → PASS**, then the schema sweeps + PG run
`pytest server/tests/test_schema_reconcile_coverage.py server/tests/test_postgres_compat.py` (with
`CCSWITCH_TEST_PG_URL`).

- [ ] **Step 5: Commit** — `feat(003): reconciler schema — node ids, journal batch id, kill switch`.

### Task 3: `score.py` + `desired_state.py` (pure)

**Files:**
- Create: `server/pm/score.py`, `server/pm/desired_state.py`
- Test: `server/tests/test_desired_state.py`

**Interfaces (Tasks 4, 7, 8 consume):**
```python
# server/pm/score.py
def rice_score(reach: int | None, impact: int | None, effort: int | None,
               confidence: int | None) -> float | None

# server/pm/desired_state.py
STATUS_BACKLOG, STATUS_READY, STATUS_IN_PROGRESS, STATUS_BLOCKED, STATUS_NEEDS_HUMAN, STATUS_DONE
STATUS_OPTIONS: tuple[str, ...]        # in board order
ACTIVE_SESSION_STATUSES = frozenset({"in_progress", "retrying", "condensing"})
HUMAN_WAIT_SESSION_STATUSES = frozenset({"waiting_question", "waiting_input", "ready_review", "needs_attention"})

@dataclass(frozen=True)
class ItemFacts:
    ref: str
    issue_state: str | None
    issue_closed_at: datetime | None
    triage_state: str | None
    classified_at: datetime | None
    reach: int | None; impact: int | None; effort: int | None; confidence: int | None
    session_status: str | None
    session_at: datetime | None
    merged_pr_at: datetime | None
    open_blocker_refs: tuple[str, ...]
    blockers_at: datetime | None
    needs_human_label: bool
    initiative_stage: str | None
    initiative_stage_at: datetime | None

@dataclass(frozen=True)
class Decision:
    value: str | float | None
    decided_at: datetime | None

@dataclass(frozen=True)
class DesiredItem:
    ref: str
    status: Decision
    score: Decision
    stage: Decision

def derive_status(f: ItemFacts) -> Decision
def derive(f: ItemFacts) -> DesiredItem
```

- [ ] **Step 1: Failing tests**

```python
from datetime import datetime, timezone
from server.pm.desired_state import (
    ItemFacts, Decision, derive, derive_status, STATUS_BACKLOG, STATUS_BLOCKED, STATUS_DONE,
    STATUS_IN_PROGRESS, STATUS_NEEDS_HUMAN, STATUS_READY)
from server.pm.score import rice_score

T1 = datetime(2026, 9, 1, tzinfo=timezone.utc)
T2 = datetime(2026, 9, 2, tzinfo=timezone.utc)
T3 = datetime(2026, 9, 3, tzinfo=timezone.utc)


def _f(**over):
    base = dict(ref="o/r#1", issue_state="open", issue_closed_at=None, triage_state="unclassified",
                classified_at=None, reach=None, impact=None, effort=None, confidence=None,
                session_status=None, session_at=None, merged_pr_at=None, open_blocker_refs=(),
                blockers_at=None, needs_human_label=False, initiative_stage=None,
                initiative_stage_at=None)
    base.update(over)
    return ItemFacts(**base)


def test_rice_score_formula_and_guards():
    assert rice_score(4, 3, 2, 80) == 480.0
    assert rice_score(4, 3, 0, 80) is None
    assert rice_score(None, 3, 2, 80) is None


def test_status_precedence_done_beats_everything():
    f = _f(merged_pr_at=T3, session_status="in_progress", session_at=T2, open_blocker_refs=("o/r#9",))
    assert derive_status(f) == Decision(STATUS_DONE, T3)
    assert derive_status(_f(issue_state="closed", issue_closed_at=T1)) == Decision(STATUS_DONE, T1)


def test_status_session_signals():
    assert derive_status(_f(session_status="in_progress", session_at=T2)) == Decision(STATUS_IN_PROGRESS, T2)
    assert derive_status(_f(session_status="waiting_question", session_at=T2)) == Decision(STATUS_NEEDS_HUMAN, T2)


def test_status_needs_human_blocked_ready_backlog():
    assert derive_status(_f(triage_state="needs_human", classified_at=T1)) == Decision(STATUS_NEEDS_HUMAN, T1)
    assert derive_status(_f(needs_human_label=True)) .value == STATUS_NEEDS_HUMAN
    assert derive_status(_f(open_blocker_refs=("o/r#2",), blockers_at=T2)) == Decision(STATUS_BLOCKED, T2)
    assert derive_status(_f(triage_state="classified", classified_at=T1)) == Decision(STATUS_READY, T1)
    assert derive_status(_f()) == Decision(STATUS_BACKLOG, None)


def test_derive_bundles_score_and_stage():
    d = derive(_f(triage_state="classified", classified_at=T1, reach=4, impact=3, effort=2, confidence=80,
                  initiative_stage="build", initiative_stage_at=T2))
    assert d.score == Decision(480.0, T1)
    assert d.stage == Decision("build", T2)
    assert derive(_f()).score == Decision(None, None)
```

- [ ] **Step 2: Run → FAIL** (module not found).

- [ ] **Step 3: Implement**

```python
# server/pm/score.py
"""RICE score — the single formula (spec 003 §4 Score; Initiative RICE-lite comment)."""
from __future__ import annotations


def rice_score(reach: int | None, impact: int | None, effort: int | None,
               confidence: int | None) -> float | None:
    if None in (reach, impact, effort, confidence) or not effort:
        return None
    return round(reach * impact * confidence / effort, 2)  # type: ignore[operator]
```

```python
# server/pm/desired_state.py
"""Desired Projects v2 field values derived from ccswitch's decision layer (spec 003 §6 step 2).

Every value carries the timestamp of the decision that produced it so the diff engine can apply
the ownership rule (§3): push only when decided_at is strictly newer than GitHub's gh_updated_at.
Status precedence: Done > session signals > needs-human > Blocked > Ready > Backlog.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .score import rice_score

STATUS_BACKLOG = "Backlog"
STATUS_READY = "Ready"
STATUS_IN_PROGRESS = "In progress"
STATUS_BLOCKED = "Blocked"
STATUS_NEEDS_HUMAN = "Needs human"
STATUS_DONE = "Done"
STATUS_OPTIONS: tuple[str, ...] = (
    STATUS_BACKLOG, STATUS_READY, STATUS_IN_PROGRESS, STATUS_BLOCKED, STATUS_NEEDS_HUMAN, STATUS_DONE)

ACTIVE_SESSION_STATUSES = frozenset({"in_progress", "retrying", "condensing"})
HUMAN_WAIT_SESSION_STATUSES = frozenset(
    {"waiting_question", "waiting_input", "ready_review", "needs_attention"})


@dataclass(frozen=True)
class ItemFacts:
    ref: str
    issue_state: str | None
    issue_closed_at: datetime | None
    triage_state: str | None
    classified_at: datetime | None
    reach: int | None
    impact: int | None
    effort: int | None
    confidence: int | None
    session_status: str | None
    session_at: datetime | None
    merged_pr_at: datetime | None
    open_blocker_refs: tuple[str, ...]
    blockers_at: datetime | None
    needs_human_label: bool
    initiative_stage: str | None
    initiative_stage_at: datetime | None


@dataclass(frozen=True)
class Decision:
    value: str | float | None
    decided_at: datetime | None


@dataclass(frozen=True)
class DesiredItem:
    ref: str
    status: Decision
    score: Decision
    stage: Decision


def derive_status(f: ItemFacts) -> Decision:
    if f.merged_pr_at is not None:
        return Decision(STATUS_DONE, f.merged_pr_at)
    if f.issue_state == "closed":
        return Decision(STATUS_DONE, f.issue_closed_at)
    if f.session_status in HUMAN_WAIT_SESSION_STATUSES:
        return Decision(STATUS_NEEDS_HUMAN, f.session_at)
    if f.session_status in ACTIVE_SESSION_STATUSES:
        return Decision(STATUS_IN_PROGRESS, f.session_at)
    if f.needs_human_label or f.triage_state == "needs_human":
        return Decision(STATUS_NEEDS_HUMAN, f.classified_at)
    if f.open_blocker_refs:
        return Decision(STATUS_BLOCKED, f.blockers_at)
    if f.triage_state == "classified":
        return Decision(STATUS_READY, f.classified_at)
    return Decision(STATUS_BACKLOG, None)


def derive(f: ItemFacts) -> DesiredItem:
    score = rice_score(f.reach, f.impact, f.effort, f.confidence) \
        if f.triage_state == "classified" else None
    return DesiredItem(
        ref=f.ref,
        status=derive_status(f),
        score=Decision(score, f.classified_at if score is not None else None),
        stage=Decision(f.initiative_stage, f.initiative_stage_at) if f.initiative_stage
        else Decision(None, None))
```

- [ ] **Step 4: Run → PASS.** **Step 5: Commit** — `feat(003): RICE score + desired-state derivation`.

### Task 4: `reconcile_diff.py` — diff engine, ownership rule, membership, edge compiler (pure)

**Files:**
- Create: `server/pm/reconcile_diff.py`
- Test: `server/tests/test_reconcile_diff.py`

**Interfaces (Tasks 5, 6, 8 consume):**
```python
@dataclass(frozen=True)
class Mutation:
    kind: str                     # "set_field" | "add_item" | "add_blocked_by"
    target_node_id: str           # item node id (set_field) | project node id (add_item) | blocked issue node id (add_blocked_by)
    field: str | None             # "Status" | "Score" | "Stage" | None
    old_value: object
    new_value: object             # option name | float | issue content node id | blocker issue node id
    initiated_by: str             # e.g. "status:o/r#5", "score:o/r#5", "membership:o/r#5", "compile:prose:o/r#5->o/r#3"
    def key(self) -> str          # sha1 of kind|target|field|json(new_value) — journal mutation_id

@dataclass(frozen=True)
class MirrorItem:
    ref: str; item_node_id: str; status: str | None; score: float | None; stage: str | None
    gh_updated_at: datetime | None

@dataclass(frozen=True)
class DepView:
    src_ref: str; dst_ref: str; source: str; active: bool

def should_push(decided_at: datetime | None, gh_updated_at: datetime | None, mirrored_value: object) -> bool
def diff_fields(desired: DesiredItem, mirror: MirrorItem) -> list[Mutation]
def membership(open_issue_nodes: Mapping[str, str], mirrored_refs: Set[str], project_node_id: str) -> list[Mutation]
def compile_edges(deps: Iterable[DepView], node_ids: Mapping[str, str], already: Set[str]) -> list[Mutation]
```

- [ ] **Step 1: Failing tests**

```python
from datetime import datetime, timezone
from server.pm.desired_state import Decision, DesiredItem
from server.pm.reconcile_diff import (
    DepView, MirrorItem, Mutation, compile_edges, diff_fields, membership, should_push)

T1 = datetime(2026, 9, 1, tzinfo=timezone.utc); T2 = datetime(2026, 9, 2, tzinfo=timezone.utc)


def _mirror(**over):
    base = dict(ref="o/r#5", item_node_id="PVTI_5", status="Backlog", score=None, stage=None,
                gh_updated_at=T1)
    base.update(over)
    return MirrorItem(**base)


def test_ownership_rule():
    assert should_push(T2, T1, "Backlog") is True          # decision newer than GitHub
    assert should_push(T1, T2, "Backlog") is False         # human edit newer -> suppressed
    assert should_push(T1, T1, "Backlog") is False         # strictly newer required
    assert should_push(None, T2, None) is True             # unset field: initial fill
    assert should_push(None, T2, "Ready") is False         # no decision timestamp, value exists


def test_diff_fields_emits_only_changed_and_pushable():
    desired = DesiredItem("o/r#5", Decision("Ready", T2), Decision(480.0, T2), Decision(None, None))
    muts = diff_fields(desired, _mirror(status="Backlog", score=480.0))
    assert [(m.kind, m.field, m.old_value, m.new_value) for m in muts] == [
        ("set_field", "Status", "Backlog", "Ready")]
    assert muts[0].target_node_id == "PVTI_5" and muts[0].initiated_by == "status:o/r#5"
    # human edited after the decision -> nothing
    assert diff_fields(desired, _mirror(status="Backlog", gh_updated_at=T2)) == []


def test_mutation_key_is_stable_and_value_sensitive():
    a = Mutation("set_field", "PVTI_5", "Status", "Backlog", "Ready", "status:o/r#5")
    b = Mutation("set_field", "PVTI_5", "Status", "Blocked", "Ready", "x")
    c = Mutation("set_field", "PVTI_5", "Status", "Backlog", "Done", "status:o/r#5")
    assert a.key() == b.key() and a.key() != c.key()


def test_membership_adds_missing_open_issues():
    muts = membership({"o/r#5": "I_5", "o/r#6": "I_6"}, {"o/r#5"}, "PVT_1")
    assert [(m.kind, m.target_node_id, m.new_value, m.initiated_by) for m in muts] == [
        ("add_item", "PVT_1", "I_6", "membership:o/r#6")]


def test_compile_edges_prose_without_native_twin():
    deps = [DepView("o/r#5", "o/r#3", "prose", True),
            DepView("o/r#6", "o/r#3", "prose", True), DepView("o/r#6", "o/r#3", "native", True),
            DepView("o/r#7", "o/r#3", "order", True), DepView("o/r#8", "o/r#3", "prose", False)]
    nodes = {"o/r#5": "I_5", "o/r#3": "I_3", "o/r#6": "I_6", "o/r#8": "I_8"}
    muts = compile_edges(deps, nodes, already=set())
    assert [(m.target_node_id, m.new_value, m.initiated_by) for m in muts] == [
        ("I_5", "I_3", "compile:prose:o/r#5->o/r#3")]   # #6 has native twin; #7 unresolved node; #8 inactive
    assert compile_edges(deps, nodes, already={muts[0].key()}) == []
```

- [ ] **Step 2: Run → FAIL.**

- [ ] **Step 3: Implement**

```python
"""Pure diff engine for the 003 reconciler (spec §6 steps 2–3). No I/O."""
from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Set
from dataclasses import dataclass
from datetime import datetime

from .desired_state import DesiredItem


@dataclass(frozen=True)
class Mutation:
    kind: str
    target_node_id: str
    field: str | None
    old_value: object
    new_value: object
    initiated_by: str

    def key(self) -> str:
        raw = "|".join([self.kind, self.target_node_id, self.field or "",
                        json.dumps(self.new_value, sort_keys=True, default=str)])
        return hashlib.sha1(raw.encode()).hexdigest()


@dataclass(frozen=True)
class MirrorItem:
    ref: str
    item_node_id: str
    status: str | None
    score: float | None
    stage: str | None
    gh_updated_at: datetime | None


@dataclass(frozen=True)
class DepView:
    src_ref: str
    dst_ref: str
    source: str
    active: bool


def should_push(decided_at: datetime | None, gh_updated_at: datetime | None,
                mirrored_value: object) -> bool:
    if mirrored_value is None:
        return True
    if decided_at is None or gh_updated_at is None:
        return decided_at is not None
    return decided_at > gh_updated_at


def _same(a: object, b: object) -> bool:
    if isinstance(a, float) or isinstance(b, float):
        try:
            return abs(float(a) - float(b)) < 1e-6  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return False
    return a == b


def diff_fields(desired: DesiredItem, mirror: MirrorItem) -> list[Mutation]:
    out: list[Mutation] = []
    for field, decision, current, tag in (
            ("Status", desired.status, mirror.status, "status"),
            ("Score", desired.score, mirror.score, "score"),
            ("Stage", desired.stage, mirror.stage, "stage")):
        if decision.value is None or _same(decision.value, current):
            continue
        if not should_push(decision.decided_at, mirror.gh_updated_at, current):
            continue
        out.append(Mutation("set_field", mirror.item_node_id, field, current,
                            decision.value, f"{tag}:{desired.ref}"))
    return out


def membership(open_issue_nodes: Mapping[str, str], mirrored_refs: Set[str],
               project_node_id: str) -> list[Mutation]:
    return [Mutation("add_item", project_node_id, None, None, node, f"membership:{ref}")
            for ref, node in sorted(open_issue_nodes.items()) if ref not in mirrored_refs]


def compile_edges(deps: Iterable[DepView], node_ids: Mapping[str, str],
                  already: Set[str]) -> list[Mutation]:
    deps = list(deps)
    native = {(d.src_ref, d.dst_ref) for d in deps if d.source == "native" and d.active}
    out: list[Mutation] = []
    seen: set[tuple[str, str]] = set()
    for d in deps:
        pair = (d.src_ref, d.dst_ref)
        if not d.active or d.source == "native" or pair in native or pair in seen:
            continue
        src, dst = node_ids.get(d.src_ref), node_ids.get(d.dst_ref)
        if not src or not dst:
            continue
        m = Mutation("add_blocked_by", src, None, None, dst,
                     f"compile:{d.source}:{d.src_ref}->{d.dst_ref}")
        if m.key() in already:
            continue
        seen.add(pair)
        out.append(m)
    return out
```

- [ ] **Step 4: Run → PASS.** **Step 5: Commit** — `feat(003): reconcile diff engine + edge compiler`.

### Task 5: `gh_mutations.py` — batch document builder/parser (pure)

**Files:**
- Create: `server/pm/gh_mutations.py`
- Test: `server/tests/test_gh_mutations.py`

**Interfaces (Task 8/9 consume):**
```python
MAX_ALIASES = 20
@dataclass(frozen=True)
class RateLimit: cost: int; remaining: int; reset_at: str | None
def build_batch(mutations: Sequence[Mutation], *, project_node_id: str, field_cache: Mapping[str, dict]) -> tuple[str, dict[str, Mutation]]
def parse_batch(payload: dict, alias_map: Mapping[str, Mutation]) -> tuple[dict[str, str | None], RateLimit | None]
def build_create_field(project_node_id: str, name: str, data_type: str, options: Sequence[str] | None) -> str
def build_update_field_options(field_id: str, options: Sequence[str]) -> str
def chunk(mutations: Sequence[Mutation], size: int = MAX_ALIASES) -> list[list[Mutation]]
class UnknownOption(ValueError)
```
Field cache shape (from Phase A `parse_project_fields`): `{name: {"field_id": id, "options": {name: id}}}`.

- [ ] **Step 1: Failing tests**

```python
import pytest
from server.pm.gh_mutations import (
    MAX_ALIASES, UnknownOption, build_batch, build_create_field, build_update_field_options,
    chunk, parse_batch)
from server.pm.reconcile_diff import Mutation

CACHE = {"Status": {"field_id": "F_s", "options": {"Ready": "O_ready", "Done": "O_done"}},
         "Score": {"field_id": "F_sc", "options": {}}}


def test_build_batch_emits_aliased_mutations_and_rate_limit():
    muts = [Mutation("set_field", "PVTI_1", "Status", "Backlog", "Ready", "status:o/r#1"),
            Mutation("set_field", "PVTI_1", "Score", None, 480.0, "score:o/r#1"),
            Mutation("add_item", "PVT_1", None, None, "I_9", "membership:o/r#9"),
            Mutation("add_blocked_by", "I_5", None, None, "I_3", "compile:prose:o/r#5->o/r#3")]
    doc, alias_map = build_batch(muts, project_node_id="PVT_1", field_cache=CACHE)
    assert set(alias_map) == {"m0", "m1", "m2", "m3"}
    assert 'm0: updateProjectV2ItemFieldValue(input: {projectId: "PVT_1", itemId: "PVTI_1", fieldId: "F_s", value: {singleSelectOptionId: "O_ready"}})' in doc
    assert 'm1: updateProjectV2ItemFieldValue(input: {projectId: "PVT_1", itemId: "PVTI_1", fieldId: "F_sc", value: {number: 480.0}})' in doc
    assert 'm2: addProjectV2ItemById(input: {projectId: "PVT_1", contentId: "I_9"})' in doc
    assert 'm3: addBlockedBy(input: {issueId: "I_5", blockingIssueId: "I_3"})' in doc
    assert doc.startswith("mutation {") and "rateLimit { cost remaining resetAt }" in doc


def test_build_batch_rejects_unknown_option_and_oversize():
    bad = [Mutation("set_field", "PVTI_1", "Status", None, "Nope", "x")]
    with pytest.raises(UnknownOption):
        build_batch(bad, project_node_id="PVT_1", field_cache=CACHE)
    too_many = [Mutation("add_item", "PVT_1", None, None, f"I_{i}", "m") for i in range(MAX_ALIASES + 1)]
    with pytest.raises(ValueError):
        build_batch(too_many, project_node_id="PVT_1", field_cache=CACHE)
    assert [len(c) for c in chunk(too_many)] == [MAX_ALIASES, 1]


def test_parse_batch_maps_errors_per_alias():
    payload = {"data": {"m0": {"projectV2Item": {"id": "PVTI_1"}}, "m1": None,
                        "rateLimit": {"cost": 2, "remaining": 4990, "resetAt": "2026-09-03T20:00:00Z"}},
               "errors": [{"path": ["m1"], "message": "Could not resolve to a node"}]}
    alias_map = {"m0": Mutation("set_field", "PVTI_1", "Status", None, "Ready", "s"),
                 "m1": Mutation("add_item", "PVT_1", None, None, "I_x", "m")}
    results, rl = parse_batch(payload, alias_map)
    assert results == {"m0": None, "m1": "Could not resolve to a node"}
    assert (rl.cost, rl.remaining) == (2, 4990)


def test_field_builders():
    assert 'dataType: SINGLE_SELECT' in build_create_field("PVT_1", "Stage", "SINGLE_SELECT", ["intake", "done"])
    assert '{name: "intake", color: GRAY, description: ""}' in build_create_field("PVT_1", "Stage", "SINGLE_SELECT", ["intake"])
    assert 'dataType: NUMBER, name: "Score"' in build_create_field("PVT_1", "Score", "NUMBER", None)
    assert 'updateProjectV2Field(input: {fieldId: "F_s", singleSelectOptions: [' in build_update_field_options("F_s", ["Backlog", "Done"])
```

- [ ] **Step 2: Run → FAIL.**

- [ ] **Step 3: Implement**

```python
"""GraphQL mutation documents for the 003 reconciler. Pure; shapes verified by Phase 0/B probes."""
from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from .reconcile_diff import Mutation

MAX_ALIASES = 20
_RATE = " rateLimit { cost remaining resetAt }"


class UnknownOption(ValueError):
    """A single-select value has no option id in the field cache (board vocabulary drifted)."""


@dataclass(frozen=True)
class RateLimit:
    cost: int
    remaining: int
    reset_at: str | None


def _q(value: object) -> str:
    return json.dumps(value)  # quotes + escapes strings; numbers verbatim


def _render(m: Mutation, project_node_id: str, field_cache: Mapping[str, dict]) -> str:
    if m.kind == "set_field":
        spec = field_cache.get(m.field or "") or {}
        field_id = spec.get("field_id")
        if not field_id:
            raise UnknownOption(f"field {m.field!r} not in cache")
        if m.field == "Score":
            value = "{number: %s}" % float(m.new_value)  # type: ignore[arg-type]
        else:
            option = (spec.get("options") or {}).get(str(m.new_value))
            if not option:
                raise UnknownOption(f"{m.field}={m.new_value!r} has no option id")
            value = "{singleSelectOptionId: %s}" % _q(option)
        return ("updateProjectV2ItemFieldValue(input: {projectId: %s, itemId: %s, fieldId: %s, "
                "value: %s}) { projectV2Item { id } }"
                % (_q(project_node_id), _q(m.target_node_id), _q(field_id), value))
    if m.kind == "add_item":
        return ("addProjectV2ItemById(input: {projectId: %s, contentId: %s}) { item { id } }"
                % (_q(m.target_node_id), _q(m.new_value)))
    if m.kind == "add_blocked_by":
        return ("addBlockedBy(input: {issueId: %s, blockingIssueId: %s}) { clientMutationId }"
                % (_q(m.target_node_id), _q(m.new_value)))
    raise ValueError(f"unknown mutation kind {m.kind!r}")


def build_batch(mutations: Sequence[Mutation], *, project_node_id: str,
                field_cache: Mapping[str, dict]) -> tuple[str, dict[str, Mutation]]:
    if len(mutations) > MAX_ALIASES:
        raise ValueError(f"batch of {len(mutations)} exceeds {MAX_ALIASES}")
    alias_map = {f"m{i}": m for i, m in enumerate(mutations)}
    body = " ".join(f"{alias}: {_render(m, project_node_id, field_cache)}"
                    for alias, m in alias_map.items())
    return f"mutation {{ {body}{_RATE} }}", alias_map


def parse_batch(payload: dict, alias_map: Mapping[str, Mutation]
                ) -> tuple[dict[str, str | None], RateLimit | None]:
    data = payload.get("data") or {}
    by_alias: dict[str, str | None] = {alias: None for alias in alias_map}
    for err in payload.get("errors") or []:
        path = err.get("path") or []
        alias = str(path[0]) if path else None
        if alias in by_alias:
            by_alias[alias] = str(err.get("message") or "error")
        else:  # document-level error: fail every alias
            for a in by_alias:
                by_alias[a] = by_alias[a] or str(err.get("message") or "error")
    for alias in alias_map:
        if by_alias[alias] is None and data.get(alias) is None:
            by_alias[alias] = "no data returned"
    rl = data.get("rateLimit") or None
    rate = RateLimit(int(rl["cost"]), int(rl["remaining"]), rl.get("resetAt")) if rl else None
    return by_alias, rate


def chunk(mutations: Sequence[Mutation], size: int = MAX_ALIASES) -> list[list[Mutation]]:
    return [list(mutations[i:i + size]) for i in range(0, len(mutations), size)]


def _options(names: Sequence[str]) -> str:
    return ", ".join('{name: %s, color: GRAY, description: ""}' % _q(n) for n in names)


def build_create_field(project_node_id: str, name: str, data_type: str,
                       options: Sequence[str] | None) -> str:
    extra = f", singleSelectOptions: [{_options(options)}]" if options else ""
    return ("mutation { createProjectV2Field(input: {projectId: %s, dataType: %s, name: %s%s}) "
            "{ projectV2Field { ... on ProjectV2FieldCommon { id name } "
            "... on ProjectV2SingleSelectField { id name options { id name } } } }%s }"
            % (_q(project_node_id), data_type, _q(name), extra, _RATE))


def build_update_field_options(field_id: str, options: Sequence[str]) -> str:
    return ("mutation { updateProjectV2Field(input: {fieldId: %s, singleSelectOptions: [%s]}) "
            "{ projectV2Field { ... on ProjectV2SingleSelectField { id name options { id name } } } }%s }"
            % (_q(field_id), _options(options), _RATE))
```

(If Task 1's probe showed `updateProjectV2Field` cannot edit the built-in Status options, keep the
builder — Task 9 decides whether to call it.)

- [ ] **Step 4: Run → PASS.** **Step 5: Commit** — `feat(003): GraphQL mutation batch builders`.

### Task 6: `journal.py` — write-ahead journal persistence

**Files:**
- Create: `server/pm/journal.py`
- Test: `server/tests/test_journal.py`

**Interfaces (Task 8/10/12 consume):**
```python
DRY_RUN_MARK = "dry_run"; PARKED_PREFIX = "parked: "; INTERRUPTED = "interrupted"
def record_pending(db: Session, mutations: Sequence[Mutation], *, batch_id: str, now: datetime, dry_run: bool) -> list[str]   # row ids, same order
def mark_applied(db: Session, row_ids: Sequence[str], now: datetime) -> None
def mark_failed(db: Session, row_id: str, error: str, now: datetime) -> None
def park(db: Session, row_id: str, reason: str, now: datetime) -> None
def failure_count(db: Session, mutation_id: str) -> int          # failed rows for that key (excludes dry-run, parked, interrupted)
def is_parked(db: Session, mutation_id: str) -> bool
def applied_keys(db: Session) -> set[str]
def recent_own_writes(db: Session, since: datetime) -> set[tuple[str, str, str]]   # (target_node_id, field, json(new_value)) applied since
def recover_interrupted(db: Session, older_than: datetime) -> int   # pending (applied False, error None, not dry-run) older than -> INTERRUPTED
def list_rows(db: Session, *, state: str | None, limit: int) -> list[GhWriteJournal]   # state in pending|applied|failed|parked|dry_run
```
Row semantics: `applied=True` → applied; `applied=False, error=None` → pending; `error=DRY_RUN_MARK`
→ dry-run record; `error.startswith(PARKED_PREFIX)` → parked; other error → failed.

- [ ] **Step 1: Failing tests**

```python
from datetime import datetime, timedelta, timezone
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from server.db.models import Base, GhWriteJournal
from server.pm import journal
from server.pm.reconcile_diff import Mutation

T = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)
M1 = Mutation("set_field", "PVTI_1", "Status", "Backlog", "Ready", "status:o/r#1")
M2 = Mutation("add_item", "PVT_1", None, None, "I_9", "membership:o/r#9")


def _db():
    e = create_engine("sqlite://"); Base.metadata.create_all(e); return Session(e)


def test_record_pending_then_applied_and_failed():
    db = _db()
    ids = journal.record_pending(db, [M1, M2], batch_id="b1", now=T, dry_run=False)
    rows = db.query(GhWriteJournal).order_by(GhWriteJournal.created_at).all()
    assert [r.mutation_id for r in rows] == [M1.key(), M2.key()]
    assert all(r.applied is False and r.error is None and r.batch_id == "b1" for r in rows)
    assert rows[0].old_value == {"value": "Backlog"} and rows[0].new_value == {"value": "Ready"}
    journal.mark_applied(db, [ids[0]], T); journal.mark_failed(db, ids[1], "boom", T)
    assert journal.applied_keys(db) == {M1.key()}
    assert journal.failure_count(db, M2.key()) == 1
    assert journal.recent_own_writes(db, T - timedelta(hours=1)) == {("PVTI_1", "Status", '"Ready"')}
    assert [r.mutation_id for r in journal.list_rows(db, state="failed", limit=10)] == [M2.key()]


def test_dry_run_rows_are_not_pending_or_failures():
    db = _db()
    journal.record_pending(db, [M1], batch_id="d1", now=T, dry_run=True)
    assert journal.failure_count(db, M1.key()) == 0
    assert journal.recover_interrupted(db, older_than=T + timedelta(hours=1)) == 0
    assert [r.error for r in journal.list_rows(db, state="dry_run", limit=5)] == [journal.DRY_RUN_MARK]


def test_park_and_interrupted_recovery():
    db = _db()
    ids = journal.record_pending(db, [M1, M2], batch_id="b2", now=T, dry_run=False)
    journal.park(db, ids[0], "failed twice", T)
    assert journal.is_parked(db, M1.key()) is True
    assert journal.recover_interrupted(db, older_than=T + timedelta(minutes=10)) == 1
    assert db.get(GhWriteJournal, ids[1]).error == journal.INTERRUPTED
    assert journal.failure_count(db, M2.key()) == 0   # interrupted is not a counted failure
```

- [ ] **Step 2: Run → FAIL.**

- [ ] **Step 3: Implement**

```python
"""Write-ahead journal for reconciler mutations (spec 003 §6 step 4). Callers own the transaction."""
from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from server.db.models import GhWriteJournal

from .reconcile_diff import Mutation

DRY_RUN_MARK = "dry_run"
PARKED_PREFIX = "parked: "
INTERRUPTED = "interrupted"
STATES = ("pending", "applied", "failed", "parked", "dry_run")


def record_pending(db: Session, mutations: Sequence[Mutation], *, batch_id: str,
                   now: datetime, dry_run: bool) -> list[str]:
    ids: list[str] = []
    for m in mutations:
        row = GhWriteJournal(
            target_node_id=m.target_node_id, field=m.field or m.kind,
            old_value={"value": m.old_value}, new_value={"value": m.new_value},
            mutation_id=m.key(), initiated_by=m.initiated_by, batch_id=batch_id,
            applied=False, error=DRY_RUN_MARK if dry_run else None, created_at=now, updated_at=now)
        db.add(row)
        db.flush()
        ids.append(row.id)
    return ids


def mark_applied(db: Session, row_ids: Sequence[str], now: datetime) -> None:
    for rid in row_ids:
        row = db.get(GhWriteJournal, rid)
        if row is not None:
            row.applied, row.error, row.updated_at = True, None, now


def mark_failed(db: Session, row_id: str, error: str, now: datetime) -> None:
    row = db.get(GhWriteJournal, row_id)
    if row is not None:
        row.applied, row.error, row.updated_at = False, error[:500], now


def park(db: Session, row_id: str, reason: str, now: datetime) -> None:
    mark_failed(db, row_id, PARKED_PREFIX + reason, now)


def _state(row: GhWriteJournal) -> str:
    if row.applied:
        return "applied"
    if row.error is None:
        return "pending"
    if row.error == DRY_RUN_MARK:
        return "dry_run"
    if row.error.startswith(PARKED_PREFIX):
        return "parked"
    return "failed"


def failure_count(db: Session, mutation_id: str) -> int:
    rows = db.scalars(select(GhWriteJournal).where(GhWriteJournal.mutation_id == mutation_id)).all()
    return sum(1 for r in rows if _state(r) == "failed" and r.error != INTERRUPTED)


def is_parked(db: Session, mutation_id: str) -> bool:
    rows = db.scalars(select(GhWriteJournal).where(GhWriteJournal.mutation_id == mutation_id)).all()
    return any(_state(r) == "parked" for r in rows)


def applied_keys(db: Session) -> set[str]:
    return {r.mutation_id for r in db.scalars(
        select(GhWriteJournal).where(GhWriteJournal.applied.is_(True))).all() if r.mutation_id}


def recent_own_writes(db: Session, since: datetime) -> set[tuple[str, str, str]]:
    rows = db.scalars(select(GhWriteJournal).where(
        GhWriteJournal.applied.is_(True), GhWriteJournal.updated_at >= since)).all()
    return {(r.target_node_id or "", r.field or "",
             json.dumps((r.new_value or {}).get("value"), sort_keys=True, default=str)) for r in rows}


def recover_interrupted(db: Session, older_than: datetime) -> int:
    n = 0
    for row in db.scalars(select(GhWriteJournal).where(
            GhWriteJournal.applied.is_(False), GhWriteJournal.error.is_(None),
            GhWriteJournal.created_at < older_than)).all():
        row.error, row.updated_at = INTERRUPTED, older_than
        n += 1
    return n


def list_rows(db: Session, *, state: str | None, limit: int) -> list[GhWriteJournal]:
    rows = db.scalars(select(GhWriteJournal).order_by(GhWriteJournal.created_at.desc())).all()
    if state is not None:
        if state not in STATES:
            raise ValueError(f"unknown journal state {state!r}")
        rows = [r for r in rows if _state(r) == state]
    return list(rows[:limit])
```

- [ ] **Step 4: Run → PASS.** **Step 5: Commit** — `feat(003): write-ahead journal persistence`.

### Task 7: `reconcile_inputs.py` — facts loader

Loads everything one product's cycle needs, in one place, from the DB (mirror + decision layer).

**Files:**
- Create: `server/pm/reconcile_inputs.py`
- Test: `server/tests/test_reconcile_inputs.py`

**Interfaces (Task 8 consumes):**
```python
@dataclass(frozen=True)
class ProductFacts:
    project_id: str; slug: str; owner: str            # owner = first repo's "owner"
    gh_project_db_id: str | None; project_node_id: str | None
    field_cache: dict; mirror_synced_at: datetime | None
    items: list[ItemFacts]                           # one per OPEN issue in product repos (+ closed issues still on the board)
    mirror: dict[str, MirrorItem]                    # ref -> mirrored board item
    open_issue_nodes: dict[str, str]                 # ref -> issue node id (open issues with a node id)
    deps: list[DepView]                              # all WorkItemDep rows whose src is in product repos
    node_ids: dict[str, str]                         # ref -> node id for every mirrored issue (any state)
    newest_decision_at: datetime | None              # max(classified_at, initiative.updated_at, last applied journal)
    last_applied_at: datetime | None

def load_product_facts(db: Session, product: ProductSyncView, *, now: datetime) -> ProductFacts
```
Signal sources (exact): `Session.origin_ref == ref` → newest `Session.status`/`updated_at`
(sessions are matched on `origin_ref` — the runner spawns them with `owner/name#N`); merged PR:
`WorkItem(kind="pr", state="merged")` whose `linked_issue_numbers` contains the issue number and same
repo → `gh_closed_at`; open blockers: active `WorkItemDep` with dst mirrored & open, `blockers_at =
max(dep.updated_at)`; needs-human label: `"needs-human" in WorkItem.labels`; initiative: row with
`epic_repo/epic_number == issue` → `stage`, `updated_at`.

- [ ] **Step 1: Failing tests**

```python
from datetime import datetime, timezone
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from server.db.models import (Base, GhProject, GhProjectItem, Initiative, Project, ProjectRepo,
                              Session as DbSession, WorkItem, WorkItemDep)
from server.pm.mirror_store import list_product_views
from server.pm.reconcile_inputs import load_product_facts

REPO = "steiner385/cc-account-switcher"
T = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)


def _db():
    e = create_engine("sqlite://"); Base.metadata.create_all(e); return Session(e)


def _seed(db):
    p = Project(slug="ccswitch", name="ccswitch"); db.add(p); db.flush()
    db.add(ProjectRepo(project_id=p.id, repo=REPO))
    gp = GhProject(project_id=p.id, gh_node_id="PVT_1", gh_project_number=26, title="ccswitch",
                   field_cache={"Status": {"field_id": "F", "options": {}}}, last_synced_at=T)
    db.add(gp); db.flush()
    i5 = WorkItem(repo=REPO, number=5, kind="issue", state="open", gh_node_id="I_5", triage_state="classified",
                  classified_at=T, reach=4, impact=3, effort=2, confidence=80, labels=[])
    i3 = WorkItem(repo=REPO, number=3, kind="issue", state="open", gh_node_id="I_3", labels=["needs-human"])
    i9 = WorkItem(repo=REPO, number=9, kind="issue", state="open", gh_node_id="I_9", labels=[])
    pr = WorkItem(repo=REPO, number=50, kind="pr", state="merged", linked_issue_numbers=[9], gh_closed_at=T, labels=[])
    db.add_all([i5, i3, i9, pr]); db.flush()
    db.add(GhProjectItem(gh_project_id=gp.id, work_item_id=i5.id, gh_item_node_id="PVTI_5",
                         status="Backlog", gh_updated_at=T))
    db.add(WorkItemDep(src_work_item_id=i5.id, dst_work_item_id=i3.id, src_ref=f"{REPO}#5",
                       dst_ref=f"{REPO}#3", source="prose", active=True, updated_at=T))
    db.add(DbSession(name="s", account_name="a", cwd="/tmp", repo=REPO, origin="backlog-runner",
                     origin_ref=f"{REPO}#3", status="waiting_question", updated_at=T))
    db.add(Initiative(slug="x", stage="build", epic_repo=REPO, epic_number=5, updated_at=T))
    db.commit()
    return p


def test_load_product_facts_assembles_signals():
    db = _db(); _seed(db)
    product = list_product_views(db)[0]
    facts = load_product_facts(db, product, now=T)
    assert facts.project_node_id == "PVT_1" and facts.owner == "steiner385"
    by_ref = {f.ref: f for f in facts.items}
    f5, f3, f9 = by_ref[f"{REPO}#5"], by_ref[f"{REPO}#3"], by_ref[f"{REPO}#9"]
    assert (f5.triage_state, f5.reach, f5.initiative_stage) == ("classified", 4, "build")
    assert f5.open_blocker_refs == (f"{REPO}#3",) and f5.blockers_at == T
    assert f3.needs_human_label is True and f3.session_status == "waiting_question"
    assert f9.merged_pr_at == T
    assert set(facts.mirror) == {f"{REPO}#5"} and facts.mirror[f"{REPO}#5"].item_node_id == "PVTI_5"
    assert facts.open_issue_nodes == {f"{REPO}#5": "I_5", f"{REPO}#3": "I_3", f"{REPO}#9": "I_9"}
    assert [(d.src_ref, d.dst_ref, d.source) for d in facts.deps] == [(f"{REPO}#5", f"{REPO}#3", "prose")]
    assert facts.newest_decision_at == T and facts.mirror_synced_at == T
```

(The `Session` model's NOT NULL columns are `name`, `account_name`, `cwd` per the Postgres
extras list — seed exactly those.)

- [ ] **Step 2: Run → FAIL.**

- [ ] **Step 3: Implement**

```python
"""Facts loader for one product's reconcile cycle (spec 003 §6 steps 1–2 inputs)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from server.db.models import (GhProject, GhProjectItem, GhWriteJournal, Initiative,
                              Session as DbSession, WorkItem, WorkItemDep)

from .desired_state import ItemFacts
from .mirror_store import ProductSyncView
from .reconcile_diff import DepView, MirrorItem

NEEDS_HUMAN_LABEL = "needs-human"


@dataclass(frozen=True)
class ProductFacts:
    project_id: str
    slug: str
    owner: str
    gh_project_db_id: str | None
    project_node_id: str | None
    field_cache: dict
    mirror_synced_at: datetime | None
    items: list[ItemFacts]
    mirror: dict[str, MirrorItem]
    open_issue_nodes: dict[str, str]
    deps: list[DepView]
    node_ids: dict[str, str]
    newest_decision_at: datetime | None
    last_applied_at: datetime | None


def _ref(w: WorkItem) -> str:
    return f"{w.repo}#{w.number}"


def _max(*ts: datetime | None) -> datetime | None:
    present = [t for t in ts if t is not None]
    return max(present) if present else None


def load_product_facts(db: Session, product: ProductSyncView, *, now: datetime) -> ProductFacts:
    repos = set(product.repos)
    gh = db.get(GhProject, product.gh_project_db_id) if product.gh_project_db_id else None
    issues = db.scalars(select(WorkItem).where(WorkItem.repo.in_(repos), WorkItem.kind == "issue")).all()
    prs = db.scalars(select(WorkItem).where(WorkItem.repo.in_(repos), WorkItem.kind == "pr",
                                            WorkItem.state == "merged")).all()
    by_ref = {_ref(w): w for w in issues}
    by_id = {w.id: w for w in issues}
    merged_at: dict[str, datetime | None] = {}
    for pr in prs:
        for n in pr.linked_issue_numbers or []:
            ref = f"{pr.repo}#{n}"
            merged_at[ref] = _max(merged_at.get(ref), pr.gh_closed_at or pr.gh_updated_at)
    sessions: dict[str, DbSession] = {}
    for s in db.scalars(select(DbSession).where(DbSession.origin_ref.in_(list(by_ref)))).all():
        cur = sessions.get(s.origin_ref or "")
        if cur is None or (s.updated_at or now) > (cur.updated_at or now):
            sessions[s.origin_ref or ""] = s
    deps = db.scalars(select(WorkItemDep)).all()
    deps = [d for d in deps if (d.src_ref or "").rsplit("#", 1)[0] in repos]
    blockers: dict[str, list[str]] = {}
    blockers_at: dict[str, datetime | None] = {}
    for d in deps:
        if not d.active or not d.src_ref or not d.dst_ref:
            continue
        dst = by_ref.get(d.dst_ref)
        if dst is not None and (dst.state or "open") == "open":
            blockers.setdefault(d.src_ref, []).append(d.dst_ref)
            blockers_at[d.src_ref] = _max(blockers_at.get(d.src_ref), d.updated_at)
    initiatives = {f"{i.epic_repo}#{i.epic_number}": i for i in db.scalars(
        select(Initiative).where(Initiative.epic_repo.in_(repos))).all()}
    mirror: dict[str, MirrorItem] = {}
    if gh is not None:
        for it in db.scalars(select(GhProjectItem).where(GhProjectItem.gh_project_id == gh.id)).all():
            w = by_id.get(it.work_item_id or "")
            if w is not None and it.gh_item_node_id:
                mirror[_ref(w)] = MirrorItem(_ref(w), it.gh_item_node_id, it.status, it.score,
                                             it.stage, it.gh_updated_at)
    items: list[ItemFacts] = []
    for ref, w in by_ref.items():
        if (w.state or "open") != "open" and ref not in mirror:
            continue
        s = sessions.get(ref)
        ini = initiatives.get(ref)
        items.append(ItemFacts(
            ref=ref, issue_state=w.state, issue_closed_at=w.gh_closed_at,
            triage_state=w.triage_state, classified_at=w.classified_at,
            reach=w.reach, impact=w.impact, effort=w.effort, confidence=w.confidence,
            session_status=s.status if s else None, session_at=s.updated_at if s else None,
            merged_pr_at=merged_at.get(ref),
            open_blocker_refs=tuple(sorted(blockers.get(ref, []))), blockers_at=blockers_at.get(ref),
            needs_human_label=NEEDS_HUMAN_LABEL in (w.labels or []),
            initiative_stage=ini.stage if ini else None,
            initiative_stage_at=ini.updated_at if ini else None))
    last_applied = db.scalar(select(GhWriteJournal.updated_at).where(
        GhWriteJournal.applied.is_(True)).order_by(GhWriteJournal.updated_at.desc()).limit(1))
    newest = _max(*(w.classified_at for w in issues), *(i.updated_at for i in initiatives.values()),
                  last_applied)
    return ProductFacts(
        project_id=product.project_id, slug=product.slug or product.project_id,
        owner=(product.repos[0].split("/", 1)[0] if product.repos else ""),
        gh_project_db_id=gh.id if gh else None, project_node_id=gh.gh_node_id if gh else None,
        field_cache=dict(gh.field_cache or {}) if gh else {},
        mirror_synced_at=gh.last_synced_at if gh else None,
        items=items, mirror=mirror,
        open_issue_nodes={ref: w.gh_node_id for ref, w in by_ref.items()
                          if (w.state or "open") == "open" and w.gh_node_id},
        deps=[DepView(d.src_ref or "", d.dst_ref or "", d.source or "", bool(d.active)) for d in deps],
        node_ids={ref: w.gh_node_id for ref, w in by_ref.items() if w.gh_node_id},
        newest_decision_at=newest, last_applied_at=last_applied)
```

- [ ] **Step 4: Run → PASS.** **Step 5: Gate + commit + PR B1**

Run the four-step gate (full server suite once). Commit `feat(003): reconcile facts loader`, then:
```bash
git push -u origin feat/003-phase-b-cores
gh pr create --title "003 Phase B: reconciler schema + pure cores" --body-file /tmp/pr.txt
gh pr merge <#> --squash --auto
```

---

## PR B2 — Loop, safety rails, echo suppression, wiring (branch `feat/003-phase-b-loop`)

Branch off `origin/master` after PR B1 merges.

### Task 8: `GhReconcileLoop` — converge cycle with every safety rail

**Files:**
- Create: `server/automations/gh_reconcile.py`
- Test: `server/tests/test_gh_reconcile_loop.py`

**Interfaces (Tasks 9, 11 consume):**
```python
class GhReconcile:                     # Automation protocol
    id = "gh-reconcile"; name = "GitHub Reconcile"
    modes = ("off", "dry_run", "apply")
    config_schema = [
        ConfigField("max_mutations_per_cycle", "Blast cap (mutations/cycle)", "int", 50, ...),
        ConfigField("migrate_cap", "Blast cap while migrate=on", "int", 500, ...),
        ConfigField("migrate", "One-time migration (raises cap, bootstraps fields)", "bool", False, ...),
        ConfigField("min_rate_remaining", "Skip cycle below this GraphQL budget", "int", 500, ...),
    ]
    def status(self, ctx) -> AutomationStatus          # reads ctx.gh_reconcile_counts

# seams (all keyword, injected by the orchestrator; tests use fakes)
ReadConfig = Callable[[], Awaitable[tuple[str, dict]]]
ListProducts = Callable[[], Awaitable[list[ProductSyncView]]]
LoadFacts = Callable[[ProductSyncView, datetime], Awaitable[ProductFacts]]
Mutate = Callable[[str, str], Awaitable[dict]]                        # (owner, document) -> payload
JournalRecord = Callable[[list[Mutation], str, datetime, bool], Awaitable[list[str]]]  # (muts, batch_id, now, dry_run) -> row ids
JournalApplied = Callable[[list[str], datetime], Awaitable[None]]
JournalFailed = Callable[[str, str, datetime], Awaitable[None]]      # (row_id, error, now)
JournalPark = Callable[[str, str, datetime], Awaitable[None]]
JournalFailureCount = Callable[[str], Awaitable[int]]
JournalParked = Callable[[str], Awaitable[bool]]
JournalAppliedKeys = Callable[[], Awaitable[set[str]]]
JournalRecover = Callable[[datetime], Awaitable[int]]
RequestSync = Callable[[str], Awaitable[None]]                        # product project_id
ShaFence = Callable[[], Awaitable[bool]]
KillSwitch = Callable[[], bool]
SaveFieldCache = Callable[[str, dict], Awaitable[None]]               # (gh_project_db_id, cache)  [Task 9]
NowUtc = Callable[[], datetime]

class GhReconcileLoop:
    def __init__(self, ctx, *, read_config, list_products, load_facts, mutate, journal_record, journal_applied,
                 journal_failed, journal_park, journal_failure_count, journal_parked, journal_applied_keys,
                 journal_recover, request_sync, sha_fence, kill_switch, save_field_cache, now_utc) -> None
    async def tick(self) -> None
```
Counts stashed on `ctx.gh_reconcile_counts`: `{"products": n, "planned": n, "applied": n, "failed": n,
"parked": n, "skipped_stale": n, "skipped_rate": n, "halted_cap": n, "fenced": 0|1, "disabled": 0|1,
"dry_run": 0|1}`.

- [ ] **Step 1: Failing tests** (canned-seam mold from `test_pm_sync_projects.py`)

```python
import asyncio
from datetime import datetime, timedelta, timezone
from server.automations.gh_reconcile import GhReconcile, GhReconcileLoop
from server.pm.desired_state import ItemFacts
from server.pm.mirror_store import ProductSyncView
from server.pm.reconcile_diff import DepView, MirrorItem
from server.pm.reconcile_inputs import ProductFacts

T = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)
REPO = "steiner385/cc-account-switcher"
PRODUCT = ProductSyncView("p1", "ccswitch", "ccswitch", "ghdb1", "PVT_1", [REPO])
CACHE = {"Status": {"field_id": "F_s", "options": {"Backlog": "O_b", "Ready": "O_r", "Done": "O_d"}},
         "Score": {"field_id": "F_sc", "options": {}},
         "Stage": {"field_id": "F_st", "options": {"build": "O_build"}}}


class Ctx:
    def __init__(self): self.events = []
    async def emit(self, level, summary, **kw): self.events.append((level, summary, kw.get("kind")))


def _item(number, **over):
    base = dict(ref=f"{REPO}#{number}", issue_state="open", issue_closed_at=None, triage_state="classified",
                classified_at=T, reach=4, impact=3, effort=2, confidence=80, session_status=None, session_at=None,
                merged_pr_at=None, open_blocker_refs=(), blockers_at=None, needs_human_label=False,
                initiative_stage=None, initiative_stage_at=None)
    base.update(over); return ItemFacts(**base)


def _facts(**over):
    base = dict(project_id="p1", slug="ccswitch", owner="steiner385", gh_project_db_id="ghdb1",
                project_node_id="PVT_1", field_cache=CACHE, mirror_synced_at=T,
                items=[_item(5)],
                mirror={f"{REPO}#5": MirrorItem(f"{REPO}#5", "PVTI_5", "Backlog", None, None, T - timedelta(days=1))},
                open_issue_nodes={f"{REPO}#5": "I_5"}, deps=[], node_ids={f"{REPO}#5": "I_5"},
                newest_decision_at=T, last_applied_at=None)
    base.update(over); return ProductFacts(**base)


def _loop(ctx, *, mode="apply", config=None, facts=None, mutate=None, fence=True, enabled=True,
          failure_counts=None, parked=None):
    calls = {"record": [], "applied": [], "failed": [], "park": [], "sync": [], "mutate": [], "cache": []}
    async def read_config(): return mode, (config or {})
    async def list_products(): return [PRODUCT]
    async def load_facts(product, now): return facts or _facts()
    async def mutate_(owner, doc):
        calls["mutate"].append((owner, doc))
        return mutate(doc) if mutate else {"data": {f"m{i}": {"ok": True} for i in range(20)},
                                          "rateLimit": None}
    async def j_record(muts, batch_id, now, dry): calls["record"].append((muts, batch_id, dry)); return [f"r{i}" for i in range(len(muts))]
    async def j_applied(ids, now): calls["applied"].extend(ids)
    async def j_failed(rid, err, now): calls["failed"].append((rid, err))
    async def j_park(rid, reason, now): calls["park"].append((rid, reason))
    async def j_fc(key): return (failure_counts or {}).get(key, 0)
    async def j_parked(key): return key in (parked or set())
    async def j_keys(): return set()
    async def j_recover(older): return 0
    async def request_sync(pid): calls["sync"].append(pid)
    async def sha_fence(): return fence
    async def save_cache(gid, cache): calls["cache"].append((gid, cache))
    loop = GhReconcileLoop(ctx, read_config=read_config, list_products=list_products, load_facts=load_facts,
                           mutate=mutate_, journal_record=j_record, journal_applied=j_applied, journal_failed=j_failed,
                           journal_park=j_park, journal_failure_count=j_fc, journal_parked=j_parked,
                           journal_applied_keys=j_keys, journal_recover=j_recover, request_sync=request_sync,
                           sha_fence=sha_fence, kill_switch=lambda: enabled, save_field_cache=save_cache,
                           now_utc=lambda: T)
    return loop, calls


def test_apply_journals_before_mutating_and_marks_applied():
    ctx = Ctx(); loop, calls = _loop(ctx)
    asyncio.run(loop.tick())
    assert len(calls["record"]) == 1 and calls["record"][0][2] is False
    muts = calls["record"][0][0]
    assert [(m.field, m.new_value) for m in muts] == [("Status", "Ready"), ("Score", 480.0)]
    assert len(calls["mutate"]) == 1 and 'm0: updateProjectV2ItemFieldValue' in calls["mutate"][0][1]
    assert calls["applied"] == ["r0", "r1"] and calls["sync"] == ["p1"]
    assert ctx.gh_reconcile_counts["applied"] == 2


def test_dry_run_journals_but_never_mutates():
    ctx = Ctx(); loop, calls = _loop(ctx, mode="dry_run")
    asyncio.run(loop.tick())
    assert calls["record"][0][2] is True and calls["mutate"] == [] and calls["applied"] == []
    assert ctx.gh_reconcile_counts["dry_run"] == 1 and ctx.gh_reconcile_counts["planned"] == 2


def test_kill_switch_and_fence_refuse_the_cycle():
    for kw, key in (({"enabled": False}, "disabled"), ({"fence": False}, "fenced")):
        ctx = Ctx(); loop, calls = _loop(ctx, **kw)
        asyncio.run(loop.tick())
        assert calls["record"] == [] and ctx.gh_reconcile_counts[key] == 1


def test_freshness_gate_requests_sync_and_skips():
    stale = _facts(mirror_synced_at=T - timedelta(hours=1), newest_decision_at=T)
    ctx = Ctx(); loop, calls = _loop(ctx, facts=stale)
    asyncio.run(loop.tick())
    assert calls["sync"] == ["p1"] and calls["record"] == []
    assert ctx.gh_reconcile_counts["skipped_stale"] == 1


def test_blast_cap_halts_loudly_without_partial_apply():
    many = _facts(items=[_item(n) for n in range(1, 8)], mirror={}, open_issue_nodes={f"{REPO}#{n}": f"I_{n}" for n in range(1, 8)})
    ctx = Ctx(); loop, calls = _loop(ctx, facts=many, config={"max_mutations_per_cycle": 3})
    asyncio.run(loop.tick())
    assert calls["record"] == [] and calls["mutate"] == []
    assert ctx.gh_reconcile_counts["halted_cap"] == 1
    assert any(lvl == "error" and kind == "gh_reconcile_cap" for lvl, _, kind in ctx.events)


def test_migrate_raises_cap():
    many = _facts(items=[_item(n) for n in range(1, 8)], mirror={}, open_issue_nodes={f"{REPO}#{n}": f"I_{n}" for n in range(1, 8)})
    ctx = Ctx(); loop, calls = _loop(ctx, facts=many, config={"max_mutations_per_cycle": 3, "migrate": True, "migrate_cap": 100})
    asyncio.run(loop.tick())
    assert len(calls["record"]) == 1 and len(calls["record"][0][0]) == 7


def test_per_alias_failure_marks_failed_and_second_failure_parks():
    def mutate(doc): return {"data": {"m0": None, "m1": {"ok": True}, "rateLimit": None},
                             "errors": [{"path": ["m0"], "message": "nope"}]}
    ctx = Ctx(); loop, calls = _loop(ctx, mutate=mutate)
    asyncio.run(loop.tick())
    assert calls["failed"] == [("r0", "nope")] and calls["applied"] == ["r1"]
    # already failed once -> this failure parks
    from server.pm.reconcile_diff import Mutation
    key = Mutation("set_field", "PVTI_5", "Status", "Backlog", "Ready", "status:x").key()
    ctx = Ctx(); loop, calls = _loop(ctx, mutate=mutate, failure_counts={key: 1})
    asyncio.run(loop.tick())
    assert calls["park"] == [("r0", "failed twice: nope")]
    assert any(kind == "gh_reconcile_parked" for _, _, kind in ctx.events)


def test_parked_mutations_are_never_replanned():
    from server.pm.reconcile_diff import Mutation
    key = Mutation("set_field", "PVTI_5", "Status", "Backlog", "Ready", "status:x").key()
    ctx = Ctx(); loop, calls = _loop(ctx, parked={key})
    asyncio.run(loop.tick())
    assert [m.field for m in calls["record"][0][0]] == ["Score"]


def test_rate_budget_skips_next_product_when_low():
    def mutate(doc): return {"data": {"m0": {"ok": 1}, "m1": {"ok": 1},
                                      "rateLimit": {"cost": 5, "remaining": 100, "resetAt": None}}}
    ctx = Ctx(); loop, calls = _loop(ctx, mutate=mutate, config={"min_rate_remaining": 500})
    asyncio.run(loop.tick()); asyncio.run(loop.tick())
    assert len(calls["mutate"]) == 1 and ctx.gh_reconcile_counts["skipped_rate"] == 1


def test_status_card_reads_counts():
    class C: gh_reconcile_counts = {"products": 2, "planned": 3, "applied": 3, "failed": 0, "parked": 1,
                                   "skipped_stale": 0, "skipped_rate": 0, "halted_cap": 0, "fenced": 0,
                                   "disabled": 0, "dry_run": 0}
    s = GhReconcile().status(C())
    assert "3 applied" in s.text and "1 parked" in s.text and s.level == "warn"
    assert GhReconcile().status(None).level == "idle"
```

- [ ] **Step 2: Run → FAIL** (module not found).

- [ ] **Step 3: Implement `server/automations/gh_reconcile.py`**

```python
"""GitHub reconciler (spec 003 §6): converge Projects v2 fields, membership and native
blocked-by edges toward the decision layer's desired state. Leader-gated at task creation
(orchestrator), SHA-fenced and kill-switched per cycle, write-ahead journaled, blast-capped,
rate-budgeted, no blind retry."""
from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Any

from server.pm.desired_state import STATUS_OPTIONS, derive
from server.pm.gh_mutations import RateLimit, UnknownOption, build_batch, chunk, parse_batch
from server.pm.mirror_store import ProductSyncView
from server.pm.reconcile_diff import Mutation, compile_edges, diff_fields, membership
from server.pm.reconcile_inputs import ProductFacts

from .base import AutomationStatus, ConfigField

log = logging.getLogger(__name__)

_INTERRUPTED_AFTER = timedelta(minutes=10)
_COUNT_KEYS = ("products", "planned", "applied", "failed", "parked", "skipped_stale",
               "skipped_rate", "halted_cap", "fenced", "disabled", "dry_run")

ReadConfig = Callable[[], Awaitable[tuple[str, dict]]]
ListProducts = Callable[[], Awaitable[list[ProductSyncView]]]
LoadFacts = Callable[[ProductSyncView, datetime], Awaitable[ProductFacts]]
Mutate = Callable[[str, str], Awaitable[dict]]
JournalRecord = Callable[[list[Mutation], str, datetime, bool], Awaitable[list[str]]]
JournalApplied = Callable[[list[str], datetime], Awaitable[None]]
JournalFailed = Callable[[str, str, datetime], Awaitable[None]]
JournalPark = Callable[[str, str, datetime], Awaitable[None]]
JournalFailureCount = Callable[[str], Awaitable[int]]
JournalParked = Callable[[str], Awaitable[bool]]
JournalAppliedKeys = Callable[[], Awaitable[set[str]]]
JournalRecover = Callable[[datetime], Awaitable[int]]
RequestSync = Callable[[str], Awaitable[None]]
ShaFence = Callable[[], Awaitable[bool]]
KillSwitch = Callable[[], bool]
SaveFieldCache = Callable[[str, dict], Awaitable[None]]
NowUtc = Callable[[], datetime]


class GhReconcile:
    id = "gh-reconcile"
    name = "GitHub Reconcile"
    description = ("Writes Projects v2 Status/Score/Stage, board membership and native blocked-by "
                   "edges from ccswitch decisions. Journaled, capped, leader-gated.")
    modes: tuple[str, ...] = ("off", "dry_run", "apply")
    config_schema = [
        ConfigField("max_mutations_per_cycle", "Blast cap (mutations per cycle)", "int", 50,
                    "Exceeding this halts the product's cycle loudly; nothing is applied."),
        ConfigField("migrate_cap", "Blast cap while migrate is on", "int", 500,
                    "Used instead of the normal cap for the one-time migration run."),
        ConfigField("migrate", "One-time migration", "bool", False,
                    "Raise the cap and bootstrap board fields. Turn off after the first apply."),
        ConfigField("min_rate_remaining", "Minimum GraphQL budget", "int", 500,
                    "Skip writing when the last reported rateLimit.remaining is below this."),
    ]

    def status(self, ctx: Any) -> AutomationStatus:
        c = getattr(ctx, "gh_reconcile_counts", None) if ctx is not None else None
        if not c:
            return AutomationStatus(text="idle", level="idle")
        if c.get("disabled"):
            return AutomationStatus(text="kill switch off (CCSWITCH_GH_RECONCILE_ENABLED)", level="idle", counts=c)
        if c.get("fenced"):
            return AutomationStatus(text="refused: daemon SHA != origin/master", level="warn", counts=c)
        parts = [f"{c.get('planned', 0)} planned", f"{c.get('applied', 0)} applied"]
        if c.get("dry_run"):
            parts.insert(0, "dry run")
        for key in ("failed", "parked", "halted_cap", "skipped_stale", "skipped_rate"):
            if c.get(key):
                parts.append(f"{c[key]} {key.replace('_', ' ')}")
        level = "error" if c.get("halted_cap") else ("warn" if (c.get("parked") or c.get("failed")) else
                                                     ("active" if c.get("applied") else "idle"))
        return AutomationStatus(text=", ".join(parts), level=level, counts=c)

    async def on_state_change(self, *, enabled: bool, mode: str | None, config: dict, ctx: Any) -> None:
        return None


class GhReconcileLoop:
    def __init__(self, ctx: Any, *, read_config: ReadConfig, list_products: ListProducts,
                 load_facts: LoadFacts, mutate: Mutate, journal_record: JournalRecord,
                 journal_applied: JournalApplied, journal_failed: JournalFailed, journal_park: JournalPark,
                 journal_failure_count: JournalFailureCount, journal_parked: JournalParked,
                 journal_applied_keys: JournalAppliedKeys, journal_recover: JournalRecover,
                 request_sync: RequestSync, sha_fence: ShaFence, kill_switch: KillSwitch,
                 save_field_cache: SaveFieldCache, now_utc: NowUtc) -> None:
        self._ctx = ctx
        self._read_config, self._list_products, self._load_facts = read_config, list_products, load_facts
        self._mutate = mutate
        self._j_record, self._j_applied, self._j_failed, self._j_park = (
            journal_record, journal_applied, journal_failed, journal_park)
        self._j_failure_count, self._j_parked, self._j_applied_keys, self._j_recover = (
            journal_failure_count, journal_parked, journal_applied_keys, journal_recover)
        self._request_sync, self._sha_fence, self._kill_switch = request_sync, sha_fence, kill_switch
        self._save_field_cache, self._now = save_field_cache, now_utc
        self._last_rate: RateLimit | None = None
        self._fence_warned = False

    async def tick(self) -> None:
        try:
            await self._run()
        except Exception:
            log.warning("gh_reconcile.tick_failed", exc_info=True)

    def _stash(self, **counts: int) -> None:
        full = {k: 0 for k in _COUNT_KEYS}
        full.update(counts)
        setattr(self._ctx, "gh_reconcile_counts", full)

    async def _run(self) -> None:
        mode, config = await self._read_config()
        if mode == "off":
            return
        if not self._kill_switch():
            self._stash(disabled=1)
            return
        if not await self._sha_fence():
            self._stash(fenced=1)
            if not self._fence_warned:
                self._fence_warned = True
                await self._ctx.emit("warning", "reconcile refused: daemon SHA is not origin/master",
                                     kind="gh_reconcile_fenced")
            return
        self._fence_warned = False
        now = self._now()
        dry_run = mode == "dry_run"
        await self._j_recover(now - _INTERRUPTED_AFTER)
        cap = int(config.get("migrate_cap", 500)) if config.get("migrate") else \
            int(config.get("max_mutations_per_cycle", 50))
        min_rate = int(config.get("min_rate_remaining", 500))
        counts = {k: 0 for k in _COUNT_KEYS}
        counts["dry_run"] = int(dry_run)
        applied_keys = await self._j_applied_keys()
        for product in await self._list_products():
            counts["products"] += 1
            try:
                await self._converge(product, now, dry_run, cap, min_rate, applied_keys, counts)
            except Exception:
                log.warning("gh_reconcile.product_failed", exc_info=True)
                await self._ctx.emit("warning", f"reconcile failed for {product.slug}",
                                     kind="gh_reconcile_error", target=product.project_id)
        self._stash(**counts)

    async def _converge(self, product: ProductSyncView, now: datetime, dry_run: bool, cap: int,
                        min_rate: int, applied_keys: set[str], counts: dict[str, int]) -> None:
        facts = await self._load_facts(product, now)
        if facts.project_node_id is None:
            return
        # 1. freshness gate (spec §6 step 1)
        if facts.newest_decision_at is not None and (
                facts.mirror_synced_at is None or facts.mirror_synced_at < facts.newest_decision_at):
            counts["skipped_stale"] += 1
            await self._request_sync(product.project_id)
            return
        # 2. desired state + 3. diff
        planned: list[Mutation] = []
        for item in facts.items:
            mirror = facts.mirror.get(item.ref)
            if mirror is not None:
                planned.extend(diff_fields(derive(item), mirror))
        planned.extend(membership(facts.open_issue_nodes, set(facts.mirror), facts.project_node_id))
        planned.extend(compile_edges(facts.deps, facts.node_ids, applied_keys))
        keep: list[Mutation] = []
        for m in planned:
            if await self._j_parked(m.key()):
                continue
            keep.append(m)
        planned = keep
        counts["planned"] += len(planned)
        if not planned:
            return
        if len(planned) > cap:
            counts["halted_cap"] += 1
            await self._ctx.emit("error", f"reconcile halted for {product.slug}: {len(planned)} mutations "
                                 f"exceed cap {cap}; nothing applied", kind="gh_reconcile_cap",
                                 target=product.project_id, detail={"planned": len(planned), "cap": cap})
            return
        if self._last_rate is not None and self._last_rate.remaining < min_rate:
            counts["skipped_rate"] += 1
            return
        # 4. apply, write-ahead journaled per batch
        batch_id = f"{'dry' if dry_run else 'run'}-{now.strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:6]}"
        for batch in chunk(planned):
            row_ids = await self._j_record(batch, batch_id, now, dry_run)
            if dry_run:
                continue
            try:
                doc, alias_map = build_batch(batch, project_node_id=facts.project_node_id,
                                             field_cache=facts.field_cache)
            except UnknownOption as exc:
                for rid in row_ids:
                    await self._j_park(rid, f"field vocabulary drift: {exc}", now)
                counts["parked"] += len(row_ids)
                await self._ctx.emit("error", f"reconcile parked batch for {product.slug}: {exc}",
                                     kind="gh_reconcile_parked", target=product.project_id)
                continue
            payload = await self._mutate(facts.owner, doc)
            results, rate = parse_batch(payload, alias_map)
            if rate is not None:
                self._last_rate = rate
            ok_ids: list[str] = []
            for (alias, m), rid in zip(alias_map.items(), row_ids):
                err = results.get(alias)
                if err is None:
                    ok_ids.append(rid)
                    continue
                if await self._j_failure_count(m.key()) >= 1:
                    await self._j_park(rid, f"failed twice: {err}", now)
                    counts["parked"] += 1
                    await self._ctx.emit("error", f"reconcile parked {m.initiated_by}: {err}",
                                         kind="gh_reconcile_parked", target=product.project_id)
                else:
                    await self._j_failed(rid, err, now)
                    counts["failed"] += 1
            await self._j_applied(ok_ids, now)
            counts["applied"] += len(ok_ids)
        if not dry_run:
            await self._request_sync(product.project_id)   # re-ingest our own writes (echo-suppressed)
```

- [ ] **Step 4: Run → PASS** (and the whole `server/tests/test_pm_sync*.py` set still green).

- [ ] **Step 5: Commit** — `feat(003): gh_reconcile loop with journal, cap, fence, rate budget`.

### Task 9: Field bootstrap (`ensure_fields`) — Score/Stage creation + Status vocabulary

The first `apply` on a fresh board has no `Score`/`Stage` fields and the built-in `Status` has
GitHub's default options. `ensure_fields` makes the board match spec §4 before any item mutation
(single mutations, journaled, once per product; cache refreshed via `save_field_cache`).

**Files:**
- Modify: `server/automations/gh_reconcile.py` (add `_ensure_fields`, call it at the top of
  `_converge` after the freshness gate)
- Modify: `server/pm/gh_projects.py` (`parse_created_field(payload) -> tuple[str, str, dict[str, str]] | None`
  → (field_id, name, options))
- Test: `server/tests/test_gh_reconcile_loop.py` (append), `server/tests/test_gh_projects_queries.py` (append)

**Interfaces:**
- Consumes: `build_create_field`, `build_update_field_options` (Task 5); Task 1's finding on whether
  built-in Status options are editable via `updateProjectV2Field`.
- Produces: field cache entries `Score`, `Stage`, and `Status` options = `STATUS_OPTIONS` on the
  board after the first apply; `Mutation(kind="create_field"|"set_field_options", …)` journal rows.

- [ ] **Step 1: Failing tests**

```python
def test_ensure_fields_creates_missing_and_fixes_status_options():
    sparse = _facts(field_cache={"Status": {"field_id": "F_s", "options": {"Todo": "O_t", "Done": "O_d"}}},
                    items=[], mirror={}, open_issue_nodes={})
    seen = []
    def mutate(doc):
        seen.append(doc)
        if "createProjectV2Field" in doc and '"Score"' in doc:
            return {"data": {"createProjectV2Field": {"projectV2Field": {"id": "F_sc", "name": "Score"}}}}
        if "createProjectV2Field" in doc:
            return {"data": {"createProjectV2Field": {"projectV2Field": {"id": "F_st", "name": "Stage",
                    "options": [{"id": "O_i", "name": "intake"}]}}}}
        return {"data": {"updateProjectV2Field": {"projectV2Field": {"id": "F_s", "name": "Status",
                "options": [{"id": f"O_{n}", "name": n} for n in STATUS_OPTIONS]}}}}
    ctx = Ctx(); loop, calls = _loop(ctx, facts=sparse, mutate=mutate)
    asyncio.run(loop.tick())
    kinds = [m.kind for m in sum((r[0] for r in calls["record"]), [])]
    assert kinds == ["create_field", "create_field", "set_field_options"]
    assert calls["cache"] and calls["cache"][-1][1]["Status"]["options"]["Needs human"] == "O_Needs human"
    assert calls["cache"][-1][1]["Score"]["field_id"] == "F_sc"


def test_ensure_fields_is_noop_when_board_matches_spec():
    full = _facts(field_cache={**CACHE, "Status": {"field_id": "F_s", "options": {n: f"O_{n}" for n in STATUS_OPTIONS}}},
                  items=[], mirror={}, open_issue_nodes={})
    ctx = Ctx(); loop, calls = _loop(ctx, facts=full)
    asyncio.run(loop.tick())
    assert calls["record"] == [] and calls["cache"] == []
```
(Import `STATUS_OPTIONS` from `server.pm.desired_state` in the test file.)

- [ ] **Step 2: Run → FAIL.**

- [ ] **Step 3: Implement**

In `gh_projects.py`:
```python
def parse_created_field(payload: dict) -> tuple[str, str, dict[str, str]] | None:
    data = payload.get("data") or {}
    node = ((data.get("createProjectV2Field") or data.get("updateProjectV2Field") or {})
            .get("projectV2Field") or {})
    if not node.get("id"):
        return None
    return node["id"], node.get("name") or "", {o["name"]: o["id"] for o in node.get("options") or []}
```
In `gh_reconcile.py` (imports: `INITIATIVE_STAGES` from `server.db.models`, `parse_created_field`,
`build_create_field`, `build_update_field_options`):
```python
    async def _ensure_fields(self, facts: ProductFacts, now: datetime, dry_run: bool,
                             counts: dict[str, int]) -> dict:
        cache = dict(facts.field_cache)
        wanted: list[tuple[Mutation, str]] = []
        if "Score" not in cache:
            wanted.append((Mutation("create_field", facts.project_node_id or "", "Score", None,
                                    {"dataType": "NUMBER"}, f"fields:{facts.slug}"),
                           build_create_field(facts.project_node_id or "", "Score", "NUMBER", None)))
        if "Stage" not in cache:
            wanted.append((Mutation("create_field", facts.project_node_id or "", "Stage", None,
                                    {"dataType": "SINGLE_SELECT", "options": list(INITIATIVE_STAGES)},
                                    f"fields:{facts.slug}"),
                           build_create_field(facts.project_node_id or "", "Stage", "SINGLE_SELECT",
                                              list(INITIATIVE_STAGES))))
        status = cache.get("Status") or {}
        have = set((status.get("options") or {}))
        if status.get("field_id") and not set(STATUS_OPTIONS) <= have:
            wanted.append((Mutation("set_field_options", status["field_id"], "Status",
                                    sorted(have), list(STATUS_OPTIONS), f"fields:{facts.slug}"),
                           build_update_field_options(status["field_id"], list(STATUS_OPTIONS))))
        if not wanted:
            return cache
        batch_id = f"fields-{now.strftime('%Y%m%dT%H%M%S')}"
        for m, doc in wanted:
            (rid,) = await self._j_record([m], batch_id, now, dry_run)
            counts["planned"] += 1
            if dry_run:
                continue
            payload = await self._mutate(facts.owner, doc)
            parsed = parse_created_field(payload)
            if parsed is None:
                err = str((payload.get("errors") or [{}])[0].get("message") or "no field returned")
                await self._j_failed(rid, err, now)
                counts["failed"] += 1
                await self._ctx.emit("error", f"field bootstrap failed for {facts.slug}: {err}",
                                     kind="gh_reconcile_error", target=facts.project_id)
                continue
            field_id, name, options = parsed
            cache[name or (m.field or "")] = {"field_id": field_id, "options": options}
            await self._j_applied([rid], now)
            counts["applied"] += 1
        if not dry_run and facts.gh_project_db_id:
            await self._save_field_cache(facts.gh_project_db_id, cache)
        return cache
```
Call it in `_converge` right after the freshness gate: `cache = await self._ensure_fields(facts, now,
dry_run, counts)` and pass `field_cache=cache` to `build_batch`. If Task 1's probe found that
`updateProjectV2Field` cannot edit the built-in `Status` options, the executor reports **BLOCKED**
instead of improvising a custom field: the spec puts its vocabulary on the built-in field, and
choosing a different field name is a human decision.

- [ ] **Step 4: Run → PASS.** **Step 5: Commit** — `feat(003): board field bootstrap`.

### Task 10: Echo suppression + Stage back-sync in the mirror

**Files:**
- Modify: `server/pm/mirror_store.py` (`upsert_project_items` gains `own_writes` + returns human edits;
  new `back_sync_stage`)
- Modify: `server/automations/pm_sync.py` (`SaveProjectItems` seam passes through the returned edits →
  `ctx.emit("info", …, kind="gh_human_edit")`)
- Modify: `server/sessions/orchestrator.py` (`_pm_sync_save_project_items` loads
  `journal.recent_own_writes(db, now-24h)` and calls `back_sync_stage`)
- Test: `server/tests/test_mirror_store.py` (append), `server/tests/test_pm_sync_projects.py` (append)

**Interfaces:**
```python
@dataclass(frozen=True)
class HumanEdit: ref: str; field: str; old: object; new: object
def upsert_project_items(db, gh_project_db_id, items, now, *, own_writes: Set[tuple[str, str, str]] = frozenset()) -> list[HumanEdit]
def back_sync_stage(db: Session, edits: Sequence[HumanEdit]) -> int   # Initiative.stage updated for epic items; returns count
```
(`own_writes` tuples are `(item_node_id, field, json(value))` — exactly `journal.recent_own_writes`'s shape.)

- [ ] **Step 1: Failing tests**

```python
def test_echo_suppression_distinguishes_own_writes_from_human_edits():
    db = _db(); project = _seed(db)
    gid = mirror_store.upsert_gh_project(db, project.id, GhProjectView("PVT_1", 7, "x", None))
    now = datetime.now(timezone.utc)
    first = [GhProjectItemView("PVTI_1", REPO, 950, "issue", "Backlog", None, None, None)]
    mirror_store.upsert_project_items(db, gid, first, now)
    changed = [GhProjectItemView("PVTI_1", REPO, 950, "issue", "Ready", 480.0, None, None)]
    edits = mirror_store.upsert_project_items(db, gid, changed, now,
                                              own_writes={("PVTI_1", "Status", '"Ready"')})
    assert [(e.field, e.old, e.new) for e in edits] == [("Score", None, 480.0)]   # Status was ours


def test_back_sync_stage_updates_linked_initiative():
    db = _db(); _seed(db)
    db.add(Initiative(slug="i", stage="plan", epic_repo=REPO, epic_number=945)); db.commit()
    n = mirror_store.back_sync_stage(db, [mirror_store.HumanEdit(f"{REPO}#945", "Stage", "plan", "build")])
    assert n == 1 and db.query(Initiative).one().stage == "build"
    assert mirror_store.back_sync_stage(db, [mirror_store.HumanEdit(f"{REPO}#945", "Stage", "build", "bogus")]) == 0
```

- [ ] **Step 2: Run → FAIL.**

- [ ] **Step 3: Implement** (in `mirror_store.py`)

```python
@dataclass(frozen=True)
class HumanEdit:
    ref: str
    field: str
    old: object
    new: object


def upsert_project_items(db: Session, gh_project_db_id: str, items: list[GhProjectItemView],
                         now: datetime, *, own_writes: Set[tuple[str, str, str]] = frozenset()
                         ) -> list[HumanEdit]:
    edits: list[HumanEdit] = []
    ...existing preload/upsert loop; where each row's status/score/stage is assigned, wrap:
        for field, new in (("Status", view.status), ("Score", view.score), ("Stage", view.stage)):
            old = getattr(row, field.lower())
            if old == new or (row.id is None):     # unchanged, or brand-new row: nothing to classify
                continue
            if (view.item_node_id, field, json.dumps(new, sort_keys=True, default=str)) not in own_writes:
                edits.append(HumanEdit(f"{view.repo}#{view.number}", field, old, new))
        row.status, row.score, row.stage = view.status, view.score, view.stage
    ...rest unchanged (prune, watermark)
    return edits


def back_sync_stage(db: Session, edits: Sequence[HumanEdit]) -> int:
    n = 0
    for e in edits:
        if e.field != "Stage" or e.new not in INITIATIVE_STAGES:
            continue
        repo, _, number = e.ref.rpartition("#")
        ini = db.query(Initiative).filter_by(epic_repo=repo, epic_number=int(number)).first()
        if ini is not None and ini.stage != e.new:
            ini.stage = e.new
            n += 1
    return n
```
`pm_sync.py`: change `SaveProjectItems = Callable[[str, list[GhProjectItemView]], Awaitable[list]]`;
after `edits = await self._save_project_items(...)`, `for e in edits: await self._ctx.emit("info",
f"GitHub edit on {e.ref}: {e.field} {e.old!r} -> {e.new!r}", kind="gh_human_edit", target=e.ref)`.
Orchestrator seam: `own = journal.recent_own_writes(s, now - timedelta(hours=24))` then
`edits = mirror_store.upsert_project_items(s, gid, items, now, own_writes=own)`;
`mirror_store.back_sync_stage(s, edits)`; return edits. Existing tests that assert the seam returns
`None` must be updated to accept a list.

- [ ] **Step 4: Run → PASS** (`test_mirror_store.py`, `test_pm_sync_projects.py`).
**Step 5: Commit** — `feat(003): echo suppression + Stage back-sync`.

### Task 11: Registration, orchestrator wiring, SHA fence, status surfaces

**Files:**
- Modify: `server/automations/registry.py` (register `GhReconcile()`)
- Modify: `server/sessions/orchestrator.py` (ctx + loop + seams + `_gh_reconcile_tick_loop`;
  `_PRIMARY_ONLY_LOOP_ATTRS` gains `"_gh_reconcile_task"`; `automation_statuses()` ctxs dict gains
  `"gh-reconcile"`; also add the missing `"pm-sync"` entry while there)
- Create: `server/github/sha_fence.py`
- Test: `server/tests/test_sha_fence.py`, `server/tests/test_primary_lease.py` (existing completeness
  test must pass), `server/tests/test_automations_registry.py` (existing — add an assertion that
  `gh-reconcile` is registered with modes `("off","dry_run","apply")`)

**Interfaces:**
```python
# server/github/sha_fence.py
async def daemon_matches_origin_master(*, ttl_s: float = 60.0) -> bool
# True iff running_git_sha() is a prefix of remote_sha(repo_root(), "origin/master"); False on ANY error (fails closed); cached ttl_s.
```

- [ ] **Step 1: Failing tests**

```python
# server/tests/test_sha_fence.py
import asyncio
from server.github import sha_fence

def test_fence_matches_prefix(monkeypatch):
    monkeypatch.setattr(sha_fence, "running_git_sha", lambda: "abc1234")
    monkeypatch.setattr(sha_fence, "_remote", lambda: "abc1234deadbeef")
    sha_fence._cache.clear()
    assert asyncio.run(sha_fence.daemon_matches_origin_master(ttl_s=0)) is True

def test_fence_fails_closed(monkeypatch):
    monkeypatch.setattr(sha_fence, "running_git_sha", lambda: "abc1234")
    monkeypatch.setattr(sha_fence, "_remote", lambda: (_ for _ in ()).throw(RuntimeError("net")))
    sha_fence._cache.clear()
    assert asyncio.run(sha_fence.daemon_matches_origin_master(ttl_s=0)) is False
    monkeypatch.setattr(sha_fence, "_remote", lambda: "fff0000")
    sha_fence._cache.clear()
    assert asyncio.run(sha_fence.daemon_matches_origin_master(ttl_s=0)) is False
```

- [ ] **Step 2: Run → FAIL.**

- [ ] **Step 3: Implement**

```python
# server/github/sha_fence.py
"""Spec 003 §6 'rocky rule': a daemon whose code is not origin/master must not write to GitHub."""
from __future__ import annotations

import asyncio
import time

from server.deploy.git import remote_sha
from server.version import repo_root, running_git_sha

_cache: dict[str, tuple[float, bool]] = {}


def _remote() -> str:
    return remote_sha(repo_root(), "origin/master")


async def daemon_matches_origin_master(*, ttl_s: float = 60.0) -> bool:
    hit = _cache.get("fence")
    if hit and time.monotonic() - hit[0] < ttl_s:
        return hit[1]
    try:
        local = running_git_sha()
        remote = await asyncio.to_thread(_remote)
        ok = bool(local) and local != "unknown" and remote.startswith(local)
    except Exception:
        ok = False
    _cache["fence"] = (time.monotonic(), ok)
    return ok
```
Orchestrator (next to the pm-sync wiring, copying its idioms exactly):
```python
from server.automations.gh_reconcile import GhReconcileLoop
self._gh_reconcile_ctx = AutomationContext("gh-reconcile")
self._gh_reconcile_loop = GhReconcileLoop(
    self._gh_reconcile_ctx,
    read_config=lambda: self._automation_mode_config("gh-reconcile"),   # same helper pm-sync uses
    list_products=self._pm_sync_list_products,
    load_facts=self._gh_reconcile_load_facts,
    mutate=self._pm_sync_fetch_projects,            # PAT-preferring GraphQL; documents may be mutations
    journal_record=self._gh_reconcile_journal_record, journal_applied=..., journal_failed=...,
    journal_park=..., journal_failure_count=..., journal_parked=..., journal_applied_keys=...,
    journal_recover=...,
    request_sync=self._gh_reconcile_request_sync,
    sha_fence=sha_fence.daemon_matches_origin_master,
    kill_switch=lambda: get_settings().gh_reconcile_enabled,
    save_field_cache=self._pm_sync_save_project_fields,
    now_utc=lambda: datetime.now(timezone.utc))
self._gh_reconcile_task: asyncio.Task | None = None
# in start(), inside the daemon_role()=="primary" block:
self._gh_reconcile_task = asyncio.create_task(self._gh_reconcile_tick_loop(interval=120.0), name="gh-reconcile-loop")
```
Seam bodies are thin `async with db_session() as db: return await db.run_sync(lambda s: journal.<fn>(s, …))`
wrappers (one per journal function); `_gh_reconcile_load_facts` wraps
`reconcile_inputs.load_product_facts`; `_gh_reconcile_request_sync(project_id)` sets
`ProjectRepo.sync_requested_at = now` for the product's repos (that is what makes pm_sync force the
projects pass). `_gh_reconcile_tick_loop`/`_tick_once` mirror `_pm_sync_tick_loop` including
`_schedule_allows_tick("gh-reconcile")` and `_bump_automation_active`.

- [ ] **Step 4: Run** `test_sha_fence.py`, `test_primary_lease.py`, `test_automations_registry.py`,
`test_gh_reconcile_loop.py` → PASS; then the four-step gate (full server suite once).

- [ ] **Step 5: Commit + PR B2**

```bash
git commit -F /tmp/msg.txt   # "feat(003): register gh-reconcile — wiring, SHA fence, status card"
git push -u origin feat/003-phase-b-loop
gh pr create --title "003 Phase B: reconciler loop, safety rails, echo suppression" --body-file /tmp/pr.txt
gh pr merge <#> --squash --auto
```

---

## PR B3 — Journal API + runbook (branch `feat/003-phase-b-api-runbook`)

### Task 12: `GET /api/pm/reconcile/journal` + runbook + first dry run

**Files:**
- Modify: `server/api/pm.py`
- Create: `specs/003-gh-projects-integration/phase-b-runbook.md`
- Test: `server/tests/test_pm_reconcile_api.py`

**Interfaces:**
- `GET /api/pm/reconcile/journal?state=pending|applied|failed|parked|dry_run&limit=200` →
  `{"rows": [{"id","targetNodeId","field","oldValue","newValue","mutationId","initiatedBy","batchId",
  "state","error","createdAt","updatedAt"}], "counts": {state: n}}`; 400 on unknown state.

- [ ] **Step 1: Failing tests** (copy the app/client fixture from `test_pm_deps_api.py`)

```python
def test_journal_endpoint_lists_and_counts(client, db):
    # seed via journal.record_pending: one dry-run row, one applied, one parked
    resp = client.get("/api/pm/reconcile/journal?state=parked")
    assert resp.status_code == 200
    body = resp.json()
    assert [r["state"] for r in body["rows"]] == ["parked"]
    assert body["counts"] == {"pending": 0, "applied": 1, "failed": 0, "parked": 1, "dry_run": 1}
    assert client.get("/api/pm/reconcile/journal?state=bogus").status_code == 400
```

- [ ] **Step 2: Run → FAIL (404).**

- [ ] **Step 3: Implement** (async idiom of `pm.py`, `journal._state` exposed as `journal.state_of(row)`):

```python
@router.get("/reconcile/journal")
async def reconcile_journal(state: str | None = None, limit: int = 200,
                            _identity: str = Depends(current_identity), db: AsyncSession = Depends(get_db)) -> dict:
    from server.pm import journal
    try:
        rows = await db.run_sync(lambda s: journal.list_rows(s, state=state, limit=min(limit, 1000)))
        all_rows = await db.run_sync(lambda s: journal.list_rows(s, state=None, limit=100_000))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    counts = {k: 0 for k in journal.STATES}
    for r in all_rows:
        counts[journal.state_of(r)] += 1
    return {"rows": [{
        "id": r.id, "targetNodeId": r.target_node_id, "field": r.field,
        "oldValue": (r.old_value or {}).get("value"), "newValue": (r.new_value or {}).get("value"),
        "mutationId": r.mutation_id, "initiatedBy": r.initiated_by, "batchId": r.batch_id,
        "state": journal.state_of(r), "error": r.error,
        "createdAt": r.created_at.isoformat() if r.created_at else None,
        "updatedAt": r.updated_at.isoformat() if r.updated_at else None} for r in rows],
        "counts": counts}
```
(Rename `journal._state` to public `state_of` in Task 6's module as part of this task.)

- [ ] **Step 4: Write the runbook** `phase-b-runbook.md`:

```markdown
# 003 Phase B runbook — enabling the reconciler

## Preconditions
- `CCSWITCH_GH_PROJECT_TOKEN` in the daemon env (classic PAT, project+repo) — verified 2026-09-03.
- Boards exist and are linked (`gh_projects` rows): ccswitch #26, KinDash #27.
- Daemon on origin/master (the SHA fence refuses otherwise).

## One-time board setup that the API cannot do
- Views: create a "Board" view grouped by Status and a "Table" view sorted by Score desc, per board.

## Enable sequence (spec §8 Phase B: dry-run → journal review → enable)
1. Set `CCSWITCH_GH_RECONCILE_ENABLED=true` in `/etc/ccswitch/daemon.env` and restart at a quiet
   moment — the kill switch gates every mode, including dry run. Then Automations → GitHub
   Reconcile: mode `dry_run`, `migrate` on.
2. Wait two ticks (≤ 4 min). Review: `GET /api/pm/reconcile/journal?state=dry_run` — expect field
   bootstrap rows (Score, Stage, Status options), `add_item` rows for every open issue, `set_field`
   Status rows (`Backlog`/`Ready`/`Needs human`…), and `add_blocked_by` rows for each prose edge.
   Anything unexpected → fix before apply.
3. Mode `apply` (migrate still on). Next tick applies in batches of 20; check `?state=applied`,
   `?state=failed`, `?state=parked`. The status card shows counts; `gh_reconcile_cap` events mean the
   cap was hit — raise `migrate_cap` only if the planned set is right.
4. Verify on GitHub: fields exist, items present, Status set, blocked-by edges on the prose-dep issues.
5. Turn `migrate` off. Steady state uses the 50 cap.

## Rollback
Every applied row has old→new. To revert a batch: `?state=applied` filtered by `batchId`; set
mode `off`, then apply the inverse by hand (option ids in `gh_projects.field_cache`) or via the
spike script. Removing compiled edges: `removeBlockedBy` with the same ids.

## Failure modes you will see
- `refused: daemon SHA != origin/master` — deploy first.
- `kill switch off` — env var unset.
- `parked` rows — a mutation failed twice; fix the cause, then delete the parked journal row to re-plan.
- `gh_human_edit` events — someone edited a field on GitHub; the reconciler will not overwrite it.
```

- [ ] **Step 5: Gate, commit, PR B3**

```bash
git commit -F /tmp/msg.txt   # "feat(003): reconcile journal API + Phase B runbook"
git push -u origin feat/003-phase-b-api-runbook
gh pr create --title "003 Phase B: journal API + runbook" --body-file /tmp/pr.txt
gh pr merge <#> --squash --auto
```

### Task 13: Live dry run on production data (operator task — executed by the controller, not a subagent)

- [ ] Deploy (human-triggered). Set `CCSWITCH_GH_RECONCILE_ENABLED=true` in daemon env; restart at a
  quiet moment. Set mode `dry_run`, `migrate` on.
- [ ] After two ticks, pull `GET /api/pm/reconcile/journal?state=dry_run` and check against the
  expected shape in the runbook. With today's data (2026-09-03) that is roughly: 3 field-bootstrap
  rows per board, ~53 `add_item` rows, 13 `add_blocked_by` rows, and no `Score` rows — the 10
  triaged issues are `proposed`, not `classified`, so Score writes begin only after triage
  acceptance.
- [ ] Report the dry-run journal summary to the human before anyone flips `apply`. **Flipping `apply`
  is a human decision** (spec §8: "review journal, enable").

---

## Done criteria

- PR B0–B3 merged; `gh-reconcile` visible in Automations with modes off/dry_run/apply.
- Kill switch, SHA fence, cap, journal write-ahead order, no-blind-retry, echo suppression each
  covered by a test that fails when the rail is removed.
- A production dry run produced a journal the human reviewed; `apply` not flipped by this plan.
- Deferred to Phase C (spec): runner queue flip, plan materialization, epic adoption, Stage
  back-sync's Initiative rows (none exist today), cross-repo sub-issue order edges.
- Deliberately not built in Phase B, with reasons: **epic restructuring (task lists → sub-issues)**
  — the mirror's drift report shows zero task-list epics today, so the migration has nothing to
  restructure; revisit if one appears. **Score human-edit back-fill of the overlay** (spec §3) — a
  scalar Score cannot be inverted into RICE inputs; the ownership rule already makes the human
  value win, which is the load-bearing half of that row.

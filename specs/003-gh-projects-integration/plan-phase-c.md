# 003 Phase C Implementation Plan — Consumers (queue flip, epic adoption, plan materialization)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the Phase A/B mirror the thing ccswitch *acts on*: the backlog runner draws its
candidates from the global priority queue over `GhProjectItem` rows instead of a live label search,
GitHub-first epics get an Initiative overlay automatically, and a merged plan artifact becomes an
epic + sub-issues + blocked-by chain on the board through a human-gated proposal.

**Architecture:** One pure ranking core (`server/pm/queue.py`) turns mirror rows into an ordered
queue; one session-level loader feeds it from `GhProjectItem ⋈ WorkItem ⋈ WorkItemDep ⋈ Session`
across every onboarded product. The runner's three candidate consumers (select, group, count) route
through that single query; the runner-side prose parser, epic-order logic and label search are
deleted in the same PR. Epic adoption is a mirror-side overlay write in the pm-sync product pass
(no GitHub write). Plan materialization is a new terminal action of the `plan` stage in the SDLC
engine: a decompose session posts a `MaterializationProposal`; accepting it performs the only
non-reconciler GitHub writes in the system, write-ahead journaled in `GhWriteJournal`, and leaves
board membership / Stage / Score to the reconciler as usual.

**Tech Stack:** Python 3.12 / SQLAlchemy 2.x (tz-aware UTC) / Alembic record-only revisions /
GitHub GraphQL via `gh api graphql` (`CCSWITCH_GH_PROJECT_TOKEN` for board reads, ambient `gh` or App
token for issue mutations) / existing automation framework (`ConfigField`, `AutomationStatus`,
`AutomationContext.emit`) / React + Vite + TS for the two small UI touches.

**Spec:** `specs/003-gh-projects-integration/spec.md` §5 (data model), §6 (reconciler ownership of
board writes), §7a (queue), §7b (materialization + adoption), §8 Phase C, §9 testing, §10 failure
modes. Phase 0 facts: `phase0-findings.md` (`addSubIssue`, `addBlockedBy`, `reprioritizeSubIssue`
verified). Prior plans: `plan.md` (Phase A), `plan-phase-b.md` (Phase B). Issues: #990 (queue),
#991 (adoption), #992 (materialization), epic #997. Residuals carried from #985: cross-repo
sub-issue order edges (Task 3 handles cross-repo blockers in the queue; the compiler side stays
deferred — see "Deliberately not in Phase C").

## Global Constraints

- **Branching:** `git fetch origin` then `git checkout -b <branch> origin/master` — never local
  `master`. PRs in order: `feat/003-phase-c-queue-core` → `feat/003-phase-c-runner-flip` →
  `feat/003-phase-c-epic-adoption` → `spike/003-phase-c-materialize-probe` →
  `feat/003-phase-c-materialize`. Each later branch is cut from `origin/master` only after the
  previous PR merges. C2 (adoption) does not depend on C1 and may run in parallel with it.
- **Commit trailer** `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`; commit messages
  and PR bodies via files (`git commit -F`, `gh pr create --body-file`); PR bodies `Fixes #990` /
  `Fixes #991` / `Fixes #992` as appropriate plus `Refs #997` (NEVER `Fixes #997` — it is the epic)
  and end with the Claude Code trailer. Every completed PR: non-draft + `gh pr merge <#> --squash --auto`.
- **Test env:** `env -u CCSWITCH_TAILNET_ALLOWLIST -u CCSWITCH_SESSIOND_ENDPOINT
  -u CCSWITCH_SESSIOND_CLIENT_CERT -u CCSWITCH_DAEMON_ROLE CCSWITCH_DEV=1
  /home/tony/GitHub/ccswitch/.venv/bin/python -m pytest …`. Four-step gate before every push:
  `ruff check src/ server/` · `mypy server/ --ignore-missing-imports` · `pytest tests/` ·
  `pytest server/tests/` (full server suite: run ONCE, foreground-or-controller-owned, never from a
  subagent's background task; known suite-order flake: `test_repo_discovery.py` ×3 pass in isolation).
  Web tasks: `(cd web && npx vitest run && npx tsc --noEmit && npx eslint src)` — full vitest, never
  changed-file-only.
- **Schema:** new columns nullable; Alembic revisions chained off `0020_journal_mutation_id_index`
  (`0021_work_item_assignees`, `0022_materialization_proposals`). Adding a MODEL (Task 10) means a
  local `CCSWITCH_TEST_PG_URL` run (`postgresql+psycopg://tony@localhost/ccswitch_test`) and entries
  in `_REQUIRED_EXTRAS` (`test_postgres_compat.py`) / `RECONCILE_EXEMPT`
  (`test_schema_reconcile_coverage.py`) as those tests demand.
- **PR C1 precondition (operator, not code):** #988 has run — boards populated, `Status` real,
  `GET /api/pm/projects/<slug>/deps` shows empty `unresolvedTargets` for every product. Before the
  flip merges, every repo in the runner's `enabled_repos` that should keep being served MUST be
  onboarded as a product with a Projects v2 board (spec §7a: the queue spans *products*). Repos not
  onboarded drop out of the runner at the flip — that is the spec, not a bug; onboard or trim
  `enabled_repos` deliberately.
- **Spec §7a queue definition, verbatim:** `eligible = GhProjectItem where status = Ready ∧ no
  active WorkItemDep edge with an open blocker ∧ not assigned to a live session, ordered by score
  DESC, then epic phase order, then age — across all products`. Retained safety net: re-verify the
  top candidate's blockers against live GitHub immediately before dispatch (one query; fails closed).
  `respect_dependencies` keeps its meaning; `blocked` counts come from the graph.
- **Single sources of truth:** "a session is holding this issue" = `desired_state.ACTIVE_SESSION_STATUSES
  | HUMAN_WAIT_SESSION_STATUSES` (the reconciler's definition — the queue must agree with the board);
  bot/meta-issue markers (`SKIP_ISSUE_LABELS`, `SKIP_ISSUE_TITLES`, `[bot]` suffix) move to
  `server/pm/queue.py` in C1 and are deleted from `backlog_github.py`; RICE formula stays in
  `server/pm/score.py`; the candidate dict shape the grouper/runner consume stays
  `{"ref","number","repo","title","body","url"}` (+ `score`, `product` added by the queue).
- **No GitHub writes outside a journaled path.** The reconciler owns board fields, membership and
  blocked-by edges (spec §6). Materialization (Task 12) writes issues/sub-issue links/blocked-by
  chains through `GhWriteJournal` rows (`create_issue`, `add_sub_issue`, `add_blocked_by`,
  `initiated_by="materialize:<slug>"`), journal row before call, never a blind retry. It does NOT
  add board items or set fields — the reconciler does that on its next cycle.
- **Labels stay until Phase D (#994).** The runner keeps writing `in-progress`/`needs-human`; the
  queue simply stops *reading* them (a claimed target-state row + live session are the exclusions).

---

## Decisions made at plan time (surface to the human before C1 merges)

| # | Decision | Why |
|---|----------|-----|
| C-1 | The queue reads only onboarded products' boards. Non-onboarded repos leave the runner at the flip. | Spec §7a/D3 (one Project per product). A hybrid "labels for the rest" path is the parallel-implementation shape the constitution forbids. Ops step in the C1 precondition. |
| C-2 | `Ready` requires a classified triage state (`derive_status`). Config knob `queue_min_status` ∈ {`Ready`, `Backlog`}, default **`Ready`** per spec. | Without the knob the runner goes idle fleet-wide at the flip until humans accept triage proposals for every open issue. `Backlog` mode = "any open, unblocked, unassigned, non-epic, non-bot item on a board", i.e. today's behaviour minus labels. The operator picks at flip time. |
| C-3 | Epic phase order is enforced by `order` dep edges (a later sibling is `Blocked` while an earlier one is open), so the sort key is `(score DESC NULLS LAST, gh_created_at ASC)`. No separate sibling-index column. | The edges already exist and the reconciler compiles them natively; a second ordering signal would drift. |
| C-4 | Epic adoption runs in the pm-sync product pass (mirror side), not inside `GhReconcileLoop`. | It is a DB overlay write with no GitHub mutation; placing it behind the reconciler's kill switch would gate a read-side feature on write-side ops. Spec §7b's "the reconciler detects" is satisfied in spirit: the same cycle that mirrors the epic creates the overlay, and the reconciler then pushes `Stage=build`. |
| C-5 | Assignees are mirrored (`WorkItem.assignees`) and an assigned item is never eligible. | The human-assignee exclusion (`no:assignee`) is what kept #988/#996 away from the runner; the mirror had no assignee data. Cheap column, same query. |
| C-6 | Materialization creates issues/sub-issues/edges itself (journaled) but leaves board membership, Stage and Score to the reconciler. | Keeps one writer per board field (spec §6). Score seeding = copying the Initiative's RICE onto each new `WorkItem` overlay as `classified`, which the reconciler already turns into `Score` + `Ready`. |

## Deliberately not in Phase C

- Cross-repo sub-issue **compiler** edges (`dep_plan.observed_deps` same-repo filter) — Phase B
  residual, unchanged; the queue treats a cross-repo blocker as blocking via `dst_ref` state lookup
  (Task 3), so readiness is right even though the native edge isn't compiled.
- Portfolio UI, queue *view* rendering, standalone Initiatives tab merge — Phase D (#993). C0
  ships `GET /api/pm/queue` so D only renders.
- Label retirement (#994) and docs consolidation (#995).

---

## PR C0 — Queue core + endpoint (branch `feat/003-phase-c-queue-core`)

No runner behaviour changes in this PR. It lands the assignee mirror, the pure ranking core, the
loader and the endpoint, all testable against SQLite fixtures.

### Task 1: Mirror assignees onto `WorkItem`

**Files:**
- Modify: `server/db/models.py` (WorkItem, after `linked_issue_numbers`)
- Create: `server/db/migrations/versions/0021_work_item_assignees.py`
- Modify: `server/pm/github_sync.py` (`_ITEM_FIELDS`, `WorkItemView`, `parse_page`)
- Modify: `server/sessions/orchestrator.py:_pm_sync_upsert_items` (copy `assignees`)
- Test: `server/tests/test_pm_github_sync.py`, `server/tests/test_pm_models.py`

**Interfaces:**
- Produces: `WorkItem.assignees: list[str] | None` (JSON, logins); `WorkItemView.assignees: tuple[str, ...]` (default `()`).

- [ ] **Step 1: Failing parser test**

```python
def test_parse_page_carries_assignee_logins():
    payload = _issue_payload()
    payload["data"]["repository"]["issues"]["nodes"][0]["assignees"] = {
        "nodes": [{"login": "steiner385"}, {"login": "octocat"}]}
    items, _, _ = parse_page(payload, "o/r", "issue")
    assert items[0].assignees == ("steiner385", "octocat")
    assert "assignees(first: 10) { nodes { login } }" in build_issues_query("o/r", None, None)
```

- [ ] **Step 2: Run** `pytest server/tests/test_pm_github_sync.py -k assignee -v` → FAIL (`assignees` unexpected / missing).

- [ ] **Step 3: Implement**

`github_sync.py`: append `\n      assignees(first: 10) { nodes { login } }` to `_ITEM_FIELDS`; add
`assignees: tuple[str, ...] = ()` as the LAST field of `WorkItemView` (after `node_id`, keeping
positional constructions valid); in `parse_page` build
`assignees=tuple(a["login"] for a in ((node.get("assignees") or {}).get("nodes") or []) if isinstance(a, dict) and a.get("login"))`.

`models.py` (WorkItem): `assignees: Mapped[list | None] = mapped_column(JSON, nullable=True)` with
comment `# GitHub assignee logins; an assigned item is never queue-eligible (spec 003 §7a, C-5)`.

`orchestrator._pm_sync_upsert_items`: `existing.assignees = list(item.assignees)`.

Migration `0021_work_item_assignees.py` (record-only, mirrors 0020's shape):
```python
revision = "0021_work_item_assignees"
down_revision = "0020_journal_mutation_id_index"
def upgrade(): op.add_column("work_items", sa.Column("assignees", sa.JSON(), nullable=True))
def downgrade(): op.drop_column("work_items", "assignees")
```

- [ ] **Step 4: Run** the two test files → PASS. Also `pytest server/tests/test_schema_reconcile_coverage.py` (nullable column ⇒ no exemption needed).
- [ ] **Step 5: Commit** `feat(pm): mirror issue assignees onto WorkItem (003 Phase C, Refs #990)`.

### Task 2: `server/pm/queue.py` — pure ranking core

**Files:**
- Create: `server/pm/queue.py`
- Test: `server/tests/test_pm_queue.py`

**Interfaces:**
- Produces:
  ```python
  @dataclass(frozen=True)
  class QueueRow:            # one mirrored board item + the facts the rules need
      ref: str; repo: str; number: int; product: str; title: str; body: str; url: str
      status: str | None; score: float | None; gh_created_at: datetime | None
      gh_updated_at: datetime | None; assignees: tuple[str, ...]; author: str
      labels: tuple[str, ...]; open_children: int; open_blockers: tuple[str, ...]
      live_session: bool; claimed: bool
  @dataclass(frozen=True)
  class QueueEntry:
      ref: str; repo: str; number: int; product: str; title: str; body: str; url: str; score: float | None
  SKIP_REASONS = ("status", "blocked", "live_session", "claimed", "assigned", "epic_open", "bot", "needs_human_cooldown")
  def skip_reason(row: QueueRow, *, min_status: str, respect_dependencies: bool, needs_human_retry_days: int, now: datetime) -> str | None
  def rank(rows: Iterable[QueueRow], *, min_status="Ready", respect_dependencies=True, needs_human_retry_days=NEEDS_HUMAN_RETRY_DAYS_DEFAULT, now: datetime) -> tuple[list[QueueEntry], dict[str, int]]
  def to_candidate(e: QueueEntry) -> dict   # the runner/grouper shape
  ```
  Moves here from `backlog_github.py` (deleted there in Task 8): `SKIP_ISSUE_LABELS`, `SKIP_ISSUE_TITLES`, `NEEDS_HUMAN_RETRY_DAYS_DEFAULT`, `NEEDS_HUMAN_LABEL`, `IN_PROGRESS_LABEL` re-exported by `backlog_github` via import until Task 8 removes the last readers.

- [ ] **Step 1: Failing tests (one behaviour each)**

```python
from datetime import datetime, timedelta, timezone
from server.pm.queue import QueueRow, rank, skip_reason, to_candidate

NOW = datetime(2026, 9, 4, tzinfo=timezone.utc)

def _row(**over):
    base = dict(ref="o/r#1", repo="o/r", number=1, product="ccswitch", title="t", body="", url="u",
                status="Ready", score=10.0, gh_created_at=NOW - timedelta(days=1), gh_updated_at=NOW,
                assignees=(), author="tony", labels=(), open_children=0, open_blockers=(),
                live_session=False, claimed=False)
    base.update(over); return QueueRow(**base)

def test_ready_unblocked_unassigned_row_is_eligible():
    assert skip_reason(_row(), min_status="Ready", respect_dependencies=True, needs_human_retry_days=7, now=NOW) is None

def test_status_gate_honours_min_status():
    kw = dict(respect_dependencies=True, needs_human_retry_days=7, now=NOW)
    assert skip_reason(_row(status="Backlog"), min_status="Ready", **kw) == "status"
    assert skip_reason(_row(status="Backlog"), min_status="Backlog", **kw) is None
    assert skip_reason(_row(status="Blocked"), min_status="Backlog", **kw) == "blocked"
    assert skip_reason(_row(status="Done"), min_status="Backlog", **kw) == "status"

def test_open_blocker_blocks_unless_dependencies_are_ignored():
    kw = dict(min_status="Ready", needs_human_retry_days=7, now=NOW)
    assert skip_reason(_row(open_blockers=("o/r#9",)), respect_dependencies=True, **kw) == "blocked"
    assert skip_reason(_row(open_blockers=("o/r#9",)), respect_dependencies=False, **kw) is None

def test_live_session_claim_assignee_epic_and_bot_are_excluded():
    kw = dict(min_status="Ready", respect_dependencies=True, needs_human_retry_days=7, now=NOW)
    assert skip_reason(_row(live_session=True), **kw) == "live_session"
    assert skip_reason(_row(claimed=True), **kw) == "claimed"
    assert skip_reason(_row(assignees=("steiner385",)), **kw) == "assigned"
    assert skip_reason(_row(open_children=3), **kw) == "epic_open"
    assert skip_reason(_row(author="renovate[bot]"), **kw) == "bot"
    assert skip_reason(_row(labels=("dependencies",)), **kw) == "bot"
    assert skip_reason(_row(title="Dependency Dashboard"), **kw) == "bot"

def test_needs_human_label_cools_down_by_gh_updated_at():
    kw = dict(min_status="Ready", respect_dependencies=True, now=NOW)
    fresh = _row(labels=("needs-human",), gh_updated_at=NOW - timedelta(days=1))
    cooled = _row(labels=("needs-human",), gh_updated_at=NOW - timedelta(days=8))
    assert skip_reason(fresh, needs_human_retry_days=7, **kw) == "needs_human_cooldown"
    assert skip_reason(cooled, needs_human_retry_days=7, **kw) is None
    assert skip_reason(fresh, needs_human_retry_days=0, **kw) == "needs_human_cooldown"  # 0 = never retry

def test_rank_orders_score_desc_nulls_last_then_age_and_counts_skips():
    rows = [_row(ref="o/r#1", score=5.0, gh_created_at=NOW - timedelta(days=1)),
            _row(ref="o/r#2", score=None, gh_created_at=NOW - timedelta(days=9)),
            _row(ref="o/r#3", score=5.0, gh_created_at=NOW - timedelta(days=3)),
            _row(ref="o/r#4", score=9.0),
            _row(ref="o/r#5", open_blockers=("o/r#4",)),
            _row(ref="o/r#6", status="Backlog")]
    entries, skipped = rank(rows, now=NOW)
    assert [e.ref for e in entries] == ["o/r#4", "o/r#3", "o/r#1", "o/r#2"]
    assert skipped == {"blocked": 1, "status": 1}

def test_to_candidate_is_the_runner_shape_plus_provenance():
    entries, _ = rank([_row()], now=NOW)
    assert to_candidate(entries[0]) == {"ref": "o/r#1", "number": 1, "repo": "o/r", "title": "t",
                                        "body": "", "url": "u", "score": 10.0, "product": "ccswitch"}
```

- [ ] **Step 2: Run** `pytest server/tests/test_pm_queue.py -v` → FAIL (`ModuleNotFoundError`).

- [ ] **Step 3: Implement `server/pm/queue.py`**

```python
"""Global priority queue over the 003 mirror (spec §7a). Pure core; the loader lives in
``queue_store.py``. The runner, the grouper, the status card and ``GET /api/pm/queue`` all rank
through ``rank`` — one definition of "what agents will do next"."""
from __future__ import annotations
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta

from .desired_state import STATUS_BACKLOG, STATUS_BLOCKED, STATUS_OPTIONS, STATUS_READY

IN_PROGRESS_LABEL = "in-progress"
NEEDS_HUMAN_LABEL = "needs-human"
NEEDS_HUMAN_RETRY_DAYS_DEFAULT = 7
# Bot/meta issues no session can act on (#637) — moved from backlog_github in Phase C.
SKIP_ISSUE_LABELS = frozenset({"dependencies", "renovate"})
SKIP_ISSUE_TITLES = frozenset({"dependency dashboard"})
SKIP_REASONS = ("status", "blocked", "live_session", "claimed", "assigned", "epic_open", "bot",
                "needs_human_cooldown")
_STATUS_RANK = {name: i for i, name in enumerate(STATUS_OPTIONS)}  # Backlog < Ready < …

@dataclass(frozen=True)
class QueueRow: ...          # exactly the Interfaces block above
@dataclass(frozen=True)
class QueueEntry: ...

def _is_bot(row: QueueRow) -> bool:
    if row.author.endswith("[bot]"):
        return True
    if {lbl.strip().lower() for lbl in row.labels} & SKIP_ISSUE_LABELS:
        return True
    return row.title.strip().lower() in SKIP_ISSUE_TITLES

def skip_reason(row, *, min_status, respect_dependencies, needs_human_retry_days, now):
    if row.status not in (STATUS_BACKLOG, STATUS_READY, STATUS_BLOCKED):
        return "status"                       # Done / In progress / Needs human / unset
    if row.status == STATUS_BLOCKED or (respect_dependencies and row.open_blockers):
        return "blocked"
    if _STATUS_RANK[row.status] < _STATUS_RANK[min_status]:
        return "status"
    if row.live_session: return "live_session"
    if row.claimed: return "claimed"
    if row.assignees: return "assigned"
    if row.open_children: return "epic_open"
    if _is_bot(row): return "bot"
    if NEEDS_HUMAN_LABEL in row.labels:
        if needs_human_retry_days <= 0 or row.gh_updated_at is None \
           or now - row.gh_updated_at < timedelta(days=needs_human_retry_days):
            return "needs_human_cooldown"
    return None

def rank(rows, *, min_status=STATUS_READY, respect_dependencies=True,
         needs_human_retry_days=NEEDS_HUMAN_RETRY_DAYS_DEFAULT, now):
    kept, skipped = [], {}
    for row in rows:
        why = skip_reason(row, min_status=min_status, respect_dependencies=respect_dependencies,
                          needs_human_retry_days=needs_human_retry_days, now=now)
        if why: skipped[why] = skipped.get(why, 0) + 1
        else: kept.append(row)
    far_future = datetime.max.replace(tzinfo=now.tzinfo)
    kept.sort(key=lambda r: (r.score is None, -(r.score or 0.0), r.gh_created_at or far_future, r.ref))
    return [QueueEntry(r.ref, r.repo, r.number, r.product, r.title, r.body, r.url, r.score)
            for r in kept], skipped

def to_candidate(e: QueueEntry) -> dict:
    return {"ref": e.ref, "number": e.number, "repo": e.repo, "title": e.title, "body": e.body,
            "url": e.url, "score": e.score, "product": e.product}
```
(Write the dataclasses out in full; the `...` above is plan shorthand only.)

- [ ] **Step 4: Run** → PASS. `ruff` + `mypy server/pm/queue.py`.
- [ ] **Step 5: Commit** `feat(pm): pure global priority queue core (003 §7a, Refs #990)`.

### Task 3: `server/pm/queue_store.py` — loader across all products

**Files:**
- Create: `server/pm/queue_store.py`
- Test: `server/tests/test_pm_queue_store.py` (SQLite, `Base.metadata.create_all`, same `_db()`/`_seed()` idiom as `test_mirror_store.py`)

**Interfaces:**
- Consumes: `QueueRow` (Task 2); `mirror_store.list_product_views`; `desired_state.ACTIVE_SESSION_STATUSES | HUMAN_WAIT_SESSION_STATUSES`.
- Produces: `load_queue_rows(db: Session, *, now: datetime, claimed_refs: Set[str] = frozenset()) -> list[QueueRow]` (sync session, run via `run_sync` like the other mirror_store helpers).

- [ ] **Step 1: Failing tests**

```python
def test_load_queue_rows_joins_board_status_blockers_sessions_and_children():
    db = _db(); project = _seed_product(db, "ccswitch", "steiner385/ccswitch", board=True)
    a = _item(db, "steiner385/ccswitch", 1, status="Ready", score=8.0)
    b = _item(db, "steiner385/ccswitch", 2, status="Ready")
    c = _item(db, "steiner385/ccswitch", 3, status="Ready")          # blocked by open #1
    epic = _item(db, "steiner385/ccswitch", 4, status="Backlog", is_epic=True)
    child = _item(db, "steiner385/ccswitch", 5, status="Ready"); child.parent_work_item_id = epic.id
    db.add(WorkItemDep(src_ref="steiner385/ccswitch#3", dst_ref="steiner385/ccswitch#1", source="native", active=True,
                       src_work_item_id=c.id, dst_work_item_id=a.id))
    db.add(WorkItemDep(src_ref="steiner385/ccswitch#2", dst_ref="cairnea/KinDash#7", source="prose", active=True))  # unmirrored cross-repo dst
    db.add(DbSession(id="s1", origin="backlog-runner", origin_ref="steiner385/ccswitch#2", status="in_progress", ...))
    db.commit()
    rows = {r.ref: r for r in load_queue_rows(db, now=NOW, claimed_refs={"steiner385/ccswitch#5"})}
    assert rows["steiner385/ccswitch#3"].open_blockers == ("steiner385/ccswitch#1",)
    assert rows["steiner385/ccswitch#2"].open_blockers == ("cairnea/KinDash#7",)   # unknown state fails closed
    assert rows["steiner385/ccswitch#2"].live_session is True
    assert rows["steiner385/ccswitch#4"].open_children == 1
    assert rows["steiner385/ccswitch#5"].claimed is True
    assert rows["steiner385/ccswitch#1"].product == "ccswitch" and rows["steiner385/ccswitch#1"].score == 8.0

def test_load_queue_rows_skips_products_without_a_board_and_closed_items():
    ...  # product with gh_project None contributes nothing; state="closed" item absent
```
(The helpers `_seed_product`/`_item` create Project + ProjectRepo + GhProject + WorkItem +
GhProjectItem rows; write them in the test module.)

- [ ] **Step 2: Run** → FAIL (`ModuleNotFoundError`).

- [ ] **Step 3: Implement**

```python
def load_queue_rows(db, *, now, claimed_refs=frozenset()):
    rows: list[QueueRow] = []
    live = ACTIVE_SESSION_STATUSES | HUMAN_WAIT_SESSION_STATUSES
    live_refs = {s.origin_ref for s in db.scalars(select(DbSession).where(DbSession.status.in_(live))) if s.origin_ref}
    for product in mirror_store.list_product_views(db):
        if not product.gh_project_db_id: continue
        items = db.execute(select(GhProjectItem, WorkItem).join(WorkItem, WorkItem.id == GhProjectItem.work_item_id)
                           .where(GhProjectItem.gh_project_id == product.gh_project_db_id,
                                  WorkItem.kind == "issue", WorkItem.state == "open")).all()
        by_ref = {f"{w.repo}#{w.number}": w for _, w in items}
        # open-child counts (epic gate) — one query per product
        open_children = dict(db.execute(select(WorkItem.parent_work_item_id, func.count(WorkItem.id))
                              .where(WorkItem.parent_work_item_id.in_([w.id for _, w in items]), WorkItem.state == "open")
                              .group_by(WorkItem.parent_work_item_id)).all())
        # open blockers — active edges whose dst is OPEN or UNKNOWN (unmirrored ⇒ fail closed, spec §10)
        deps = db.scalars(select(WorkItemDep).where(deps_in_repos(product.repos), WorkItemDep.active.is_(True))).all()
        dst_refs = {d.dst_ref for d in deps if d.dst_ref}
        dst_state = {f"{w.repo}#{w.number}": (w.state or "open") for w in db.scalars(select(WorkItem).where(
            WorkItem.kind == "issue", tuple_(WorkItem.repo, WorkItem.number).in_([_split(r) for r in dst_refs])))}
        blockers: dict[str, list[str]] = {}
        for d in deps:
            if d.src_ref and d.dst_ref and dst_state.get(d.dst_ref, "open") == "open":
                blockers.setdefault(d.src_ref, []).append(d.dst_ref)
        for it, w in items:
            ref = f"{w.repo}#{w.number}"
            rows.append(QueueRow(ref=ref, repo=w.repo or "", number=int(w.number or 0), product=product.slug or product.project_id,
                                 title=w.title or "", body=w.body or "", url=w.url or "", status=it.status, score=it.score,
                                 gh_created_at=_aware(w.gh_created_at), gh_updated_at=_aware(w.gh_updated_at),
                                 assignees=tuple(w.assignees or ()), author=w.author or "", labels=tuple(w.labels or ()),
                                 open_children=int(open_children.get(w.id, 0)), open_blockers=tuple(sorted(blockers.get(ref, []))),
                                 live_session=ref in live_refs, claimed=ref in claimed_refs))
    return rows
```
`_aware` = the same helper as `reconcile_inputs._aware` — import it, don't copy it. `deps_in_repos`
from `mirror_store`. `_split(ref) -> (repo, int)`.

- [ ] **Step 4: Run** → PASS; ruff; mypy.
- [ ] **Step 5: Commit** `feat(pm): queue loader over GhProjectItem across products (Refs #990)`.

### Task 4: `GET /api/pm/queue`

**Files:**
- Modify: `server/api/pm.py` (new route after `/reconcile/journal`)
- Test: `server/tests/test_pm_queue_api.py` (idiom: `test_pm_deps_api.py` — TestClient + allowlisted identity)

**Interfaces:**
- Produces: `GET /api/pm/queue?min_status=Ready|Backlog&respect_dependencies=true` →
  `{"entries": [{"ref","repo","number","product","title","url","score"}], "skipped": {"blocked": n, …}, "generatedAt": iso}`.
  The runner's queue reads the *same* `rank(load_queue_rows(...))` composition (Task 5) — spec §7c "what you see is what agents will do".

- [ ] **Step 1: Failing test:** seed one Ready and one Blocked item (SQLite app fixture), `GET /api/pm/queue` → 200, `entries == [ready]`, `skipped == {"blocked": 1}`; unauthenticated → 401.
- [ ] **Step 2: Run** → FAIL 404.
- [ ] **Step 3: Implement** the handler: parse the two query params (422 on an unknown status), `await db.run_sync(lambda s: load_queue_rows(s, now=now))`, `rank(...)`, serialize; `body` deliberately omitted from the API payload (large).
- [ ] **Step 4: Run** → PASS; ruff; mypy.
- [ ] **Step 5: Commit + PR C0** `feat(pm): GET /api/pm/queue — the global priority queue (Refs #990)`. Body `Refs #990, Refs #997`. Ready + auto-merge.

---

## PR C1 — Runner flip + same-PR deletions (branch `feat/003-phase-c-runner-flip`)

**Merge gate (operator):** see Global Constraints "PR C1 precondition". Do not merge until #988 is
accepted and the products-vs-`enabled_repos` decision (C-1) is made.

### Task 5: Route the runner's three candidate consumers through the queue

**Files:**
- Modify: `server/automations/backlog_runner.py` (`select_batch` `query` seam becomes async; new `ConfigField("queue_min_status", …, "enum", "Ready", choices=["Ready","Backlog"])`)
- Modify: `server/sessions/orchestrator.py` (`_backlog_query_queue`, and `_backlog_select_batch` / `_backlog_refresh_groups` / `_backlog_count_candidates` use it)
- Test: `server/tests/test_backlog_select_batch.py`, `server/tests/test_backlog_runner_loop.py`

**Interfaces:**
- Produces: `orchestrator._backlog_query_queue(config, *, skipped_out: dict | None = None) -> list[dict]` = `[to_candidate(e) for e in rank(load_queue_rows(db, now, claimed_refs=<targets with "claimed">), min_status=config["queue_min_status"], respect_dependencies=config.get("respect_dependencies", True), needs_human_retry_days=config.get("needs_human_retry_days", DEFAULT), now=now)]`.
- `select_batch(config, *, query: Callable[[dict], Awaitable[list[dict]]], …)` — `candidates = await query(config)`; the `gh_run` parameter is removed from `select_batch` (it only fed the label search).

- [ ] **Step 1: Failing tests:** (a) `select_batch` awaits an async `query` and passes `config` only; (b) `_backlog_query_queue` excludes refs whose target-state row has `claimed=True` (fake `list_target_states`); (c) `count_candidates` returns `(len(entries), skipped["blocked"])`.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement.** In `_backlog_query_queue`: `states = await self._backlog_ctx.list_target_states()`; `claimed = {ref for st in states.values() if st.get("claimed") for ref in st.get("issues", [])}`; open `db_session()`, `run_sync(load_queue_rows)`, `rank`, fill `skipped_out`. `_backlog_count_candidates` → `(len(cands), skipped.get("blocked", 0))`. `_backlog_refresh_groups` → `candidates = await self._backlog_query_queue(config)`.
- [ ] **Step 4: Run** the runner test files → PASS.
- [ ] **Step 5: Commit** `feat(backlog): runner candidates come from the global priority queue (Fixes #990 part 1)`.

### Task 6: Pre-dispatch live re-verification (fails closed)

**Files:**
- Modify: `server/automations/backlog_github.py` (new `verify_ready_live(refs, *, gh_run) -> bool | None`)
- Modify: `server/automations/backlog_runner.py` (`_refill`: after `select_batch`, before the claim: `ok = await self._verify_ready(batch)`; `None`/`False` ⇒ emit `skipped_live_check`, per-target backoff via the existing `_spawn_backoff` map, `continue`)
- Modify: `server/sessions/orchestrator.py` (wire `verify_ready=self._backlog_verify_ready`)
- Test: `server/tests/test_backlog_github.py`, `server/tests/test_backlog_runner_loop.py`

**Interfaces:**
- Produces: `verify_ready_live(refs, *, gh_run)`: one aliased GraphQL document per owner —
  `i0: repository(owner,name){ issue(number){ state assignees(first:1){totalCount} blockedBy(first:50){nodes{state}} } }` per ref;
  returns `True` iff every issue is `OPEN`, has no assignee and no `OPEN` blocker; `False` if any fails; `None` on a transport/parse error (caller treats as not-ready — spec §10 "fails closed").

- [ ] **Step 1: Failing tests:** ready → True; an OPEN blocker → False; gh returncode 1 → None; loop test: `verify_ready` returning None ⇒ no claim, no spawn, one `skipped_live_check` event, target backed off.
- [ ] **Step 2–4:** implement, run → PASS.
- [ ] **Step 5: Commit** `feat(backlog): live blocker re-verification before dispatch, fails closed (Refs #990)`.

### Task 7: Queue provenance on the status card

**Files:**
- Modify: `server/automations/backlog_runner.py` (`_refill`: after a successful spawn emit `picked` with `detail={"ref", "score", "product", "over": [next 3 entries]}`; `_stash_counts` records `last_pick`; `status()` text appends `· picked X (score S, product P) over Y`)
- Test: `server/tests/test_backlog_runner_loop.py`, `server/tests/test_backlog_runner_automation.py`

The `over` list needs the ranked tail: `select_batch` returns `(batch, group_id)` today via
`plan_to_batch`; expose the top-N candidates by having `_backlog_query_queue` cache its last result
on the loop (`self._last_queue`) — no second query.

- [ ] Steps 1–5 as above; commit `feat(backlog): queue provenance on the runner status card (Refs #990)`.

### Task 8: Delete the label search, prose parser and epic-order logic from the runner

**Files:**
- Modify: `server/automations/backlog_github.py` — delete `query_candidates`, `_build_candidates_query`, `_common_quals`, `_ISSUE_NODES`, `_is_unimplementable`, `_resolve_dep_states`, `_graphql_per_owner` **only if** `query_claimed_refs` no longer uses it (it does — keep), the `SKIP_ISSUE_*` / `NEEDS_HUMAN_RETRY_DAYS_DEFAULT` constants (now imported from `server.pm.queue`), `_open_sub_issue_count`'s candidate-node use (keep the close-gate use).
- Modify: `server/automations/backlog_deps.py` — delete `blocked_refs`, `_earlier_open_subissue_siblings`, `_earlier_open_epic_items`, `body_dep_refs`, `_is_done` (if unreferenced); keep `parse_body_dep_refs`, `parse_checklist_refs` (used by `dep_plan` / `drift`).
- Modify: `server/automations/backlog_runner.py` — drop the `query_candidates` import.
- Delete tests: the `query_candidates` block of `server/tests/test_backlog_github.py`; `blocked_refs` tests in `server/tests/test_backlog_deps.py`.
- Docs: `docs/` mentions of label-based candidate search → queue (grep `eligible_label`). The `eligible_label` ConfigField is removed (its semantics have no queue equivalent); `validate_config` drops it from stored configs.

- [ ] **Step 1:** `grep -rn "query_candidates\|blocked_refs\|eligible_label\|_is_unimplementable" server/ src/ docs/ web/src` → list every reader; each must be gone or re-pointed by the end of this task.
- [ ] **Step 2:** delete + re-point; run `ruff` (unused imports surface here), `mypy`, the backlog test files, then the FULL server suite once.
- [ ] **Step 3: Commit + PR C1** `refactor(backlog): remove label search, prose parser and epic-order logic from the runner hot path (Fixes #990)`. PR body: `Fixes #990`, `Refs #997`, the C-1/C-2 decisions and the operator precondition spelled out. Ready + auto-merge.

---

## PR C2 — Epic adoption (branch `feat/003-phase-c-epic-adoption`, independent of C1)

### Task 9: `mirror_store.adopt_epics` in the pm-sync product pass

**Files:**
- Modify: `server/pm/mirror_store.py` (new `adopt_epics(db, project_id, *, now) -> list[str]`)
- Modify: `server/automations/pm_sync.py` (seam `adopt_epics: AdoptEpics | None = None`; `_sync_one_product` calls it after the relations loop; emits `epic_adopted` per ref)
- Modify: `server/sessions/orchestrator.py` (`_pm_sync_adopt_epics` over `db_session()` + wiring)
- Test: `server/tests/test_mirror_store.py`, `server/tests/test_pm_sync_projects.py`

**Interfaces:**
- Produces: for every open `WorkItem` with `is_epic=True` that has a `GhProjectItem` on the product's board and no `Initiative` with `(epic_repo, epic_number) == (repo, number)`: create
  `Initiative(slug=_unique_slug(db, slugify(title)), repo=repo, title=title, stage="build", status="active", origin="epic-adoption", origin_ref=ref, epic_repo=repo, epic_number=number, project_id=project_id)`; returns the adopted refs. Idempotent. `slugify` = the one already used by `POST /api/sdlc/initiatives` (`server/api/sdlc.py`) — move it to `server/sdlc/slug.py` and import from both places (single source).

- [ ] **Step 1: Failing tests:** (a) an on-board open epic without an overlay gets an Initiative at `stage="build"` with the epic key; (b) second call creates nothing; (c) an epic NOT on the board (no `GhProjectItem`) is not adopted; (d) a closed epic is not adopted; (e) slug collision → `-2` suffix; (f) loop test: `adopt_epics` seam called with the product id after `save_relations`, one `epic_adopted` event per ref.
- [ ] **Step 2–4:** implement, run → PASS. Note `back_sync_stage` (Phase B) now has a target: a human moving `Stage` on the board updates this row — cover with one test (`test_back_sync_stage_updates_adopted_initiative`).
- [ ] **Step 5: Commit** `feat(pm): adopt GitHub-first epics as Initiative overlays at stage=build (Fixes #991 part 1)`.

### Task 10: Epic progress in the initiative API + InitiativeTrack

**Files:**
- Modify: `server/api/sdlc.py` (`_initiative_to_dict` adds `epicRepo`, `epicNumber`; `GET /initiatives/{slug}` adds `epic: {ref, url, total, done, blocked, needsHuman, children:[{ref,title,state,status}]} | null` from `WorkItem` children by `parent_work_item_id` joined to `GhProjectItem.status`)
- Modify: `web/src/api/sdlc.ts` (`Initiative.epicRepo/epicNumber/epic?`), `web/src/initiatives/InitiativeTrack.tsx` (optional `epic` prop: Build node label becomes `Build 3/10`, title lists blocked/needs-human counts; deep-link `<a href={epic.url}>` — spec §7c "deep-links to the GitHub Project, ccswitch does not rebuild a board"), `web/src/initiatives/InitiativesTab.tsx` (pass `epic`)
- Test: `server/tests/test_sdlc_api.py`; `web/src/initiatives/__tests__/InitiativeTrack.test.tsx`

- [ ] **Step 1: Failing tests:** API: seeded epic with 3 children (1 closed, 1 Blocked, 1 Ready) → `epic.total == 3, done == 1, blocked == 1`; no epic → `epic: null`. Web: `trackState` unchanged; rendering with `epic={{total:10, done:3}}` shows `Build 3/10` and the link.
- [ ] **Step 2–4:** implement; `(cd web && npx vitest run && npx tsc --noEmit && npx eslint src)`.
- [ ] **Step 5: Commit + PR C2** `feat(sdlc): epic/sub-issue progress from the mirror on initiatives (Fixes #991)`. Body `Fixes #991`, `Refs #997`. Ready + auto-merge.

**Acceptance check after deploy:** epic #997 has an Initiative row (`GET /api/sdlc/initiatives` shows
`epicNumber: 997`); moving its `Stage` on board #26 changes `Initiative.stage` on the next pm-sync tick.

---

## PR C3 — Materialization mutation probe (branch `spike/003-phase-c-materialize-probe`)

### Task 11: Verify `createIssue` + fresh-issue `addSubIssue` + `addBlockedBy` chain end to end

Phase 0 verified `addSubIssue`/`addBlockedBy` on hand-made issues. Materialization creates the
issues itself, so verify the full sequence, the `repositoryId` lookup, and that a just-created
issue's node id is immediately usable (constitution III).

**Files:**
- Modify: `scripts/spike_gh_projects_v2.py` (add `materialize` subcommand)
- Modify: `specs/003-gh-projects-integration/phase0-findings.md` (append "Materialization probe")

- [ ] **Step 1:** `gh repo unarchive steiner385/gh-projects-spike-003` (it is archived; #996 deletes it later — re-archive at the end).
- [ ] **Step 2:** subcommand: `repository(owner,name){ id }` → `createIssue(input:{repositoryId, title:"[spike] epic", body})` → three `createIssue` tasks → `addSubIssue(issueId: epic, subIssueId: t_k)` ×3 → `addBlockedBy(issueId: t2, blockingIssueId: t1)`, `addBlockedBy(issueId: t3, blockingIssueId: t2)` → read back `subIssues { nodes { number } }` and `blockedBy` on t3 → close all four issues. Print every response; log to `/tmp/spike-003-materialize.log`.
- [ ] **Step 3:** Record in findings: input shapes, whether `createIssue` returns `issue { id number url }`, ordering of `subIssues` (creation order vs `reprioritizeSubIssue` needed), any rate cost. `gh repo archive` again.
- [ ] **Step 4: Commit + PR C3** (`Refs #992`). Contradictions with Task 12's builders below → fix Task 12 before executing it.

---

## PR C4 — Plan materialization (branch `feat/003-phase-c-materialize`)

### Task 12: Engine — `Materialize` terminal action of the `plan` stage

**Files:**
- Modify: `server/sdlc/engine.py` (`InitiativeView.epic: str | None = None`; new `@dataclass(frozen=True) class Materialize: kind: str = "plan"`; `StageAction` union; `next_action`: when `stage == "plan"`, plan artifact merged (`commit_sha`), and `initiative.epic is None` → `Materialize()`; with `epic` set → existing `AwaitApproval` / auto-advance path)
- Modify: `server/automations/sdlc_synthesizer.py:_to_views` (`epic=f"{ini['epic_repo']}#{ini['epic_number']}" if ini.get("epic_number") else None`)
- Test: `server/tests/test_sdlc_engine.py`

- [ ] **Step 1: Failing tests:**
```python
def test_plan_merged_without_epic_materializes():
    assert next_action(_init(stage="plan"), [_art("plan", sha="abc")], "propose", {}) == Materialize()

def test_plan_merged_with_epic_awaits_advance_as_before():
    assert next_action(_init(stage="plan", epic="o/r#42"), [_art("plan", sha="abc")], "propose", {}) == AwaitApproval("plan")

def test_plan_still_open_awaits_before_materializing():
    assert next_action(_init(stage="plan"), [_art("plan", pr=7)], "propose", {}) == AwaitApproval("plan")
```
- [ ] **Step 2–4:** implement → PASS.
- [ ] **Step 5: Commit** `feat(sdlc): Materialize — terminal action of the plan stage (Refs #992)`.

### Task 13: `MaterializationProposal` model + `server/pm/materialize.py` pure core

**Files:**
- Modify: `server/db/models.py` (new model), `server/db/migrations/versions/0022_materialization_proposals.py`, `server/tests/test_postgres_compat.py::_REQUIRED_EXTRAS`, `server/tests/test_schema_reconcile_coverage.py` if a NOT NULL column lacks a default
- Create: `server/pm/materialize.py`
- Test: `server/tests/test_pm_materialize.py`, `server/tests/test_pm_models.py`

**Interfaces:**
- Model `MaterializationProposal(id, initiative_id FK initiatives.id, payload JSON, status "pending"|"accepted"|"rejected" (default pending), session_id, rationale, created_at, resolved_at)`; `MATERIALIZATION_STATUSES = ("pending", "accepted", "rejected")`.
- Pure:
  ```python
  @dataclass(frozen=True) class TaskSpec: title: str; body: str; depends_on: tuple[int, ...]   # indexes into tasks
  @dataclass(frozen=True) class Decomposition: epic_title: str; epic_body: str; tasks: tuple[TaskSpec, ...]
  def build_materialize_prompt(slug: str, repo: str, plan_markdown: str) -> str
  def parse_materialization_output(text: str) -> Decomposition        # fenced JSON, tolerant like parse_classification_output; raises MaterializationParseError
  def chain_edges(d: Decomposition) -> list[tuple[int, int]]           # (blocked_idx, blocker_idx): explicit depends_on ∪ sibling order (k blocked by k-1) — dedupe, reject cycles
  def build_create_issue(repository_id: str, title: str, body: str, alias: str) -> str
  def build_add_sub_issue(epic_node: str, child_node: str, alias: str) -> str
  def build_add_blocked_by(issue_node: str, blocker_node: str, alias: str) -> str   # SAME input shape as gh_mutations — import gh_mutations.build_batch's helper rather than re-typing the mutation
  ```
- [ ] **Step 1: Failing tests:** prompt contains the plan text and the JSON contract; parser round-trips a fenced payload, rejects missing `tasks`, coerces `depends_on` ints; `chain_edges` on 3 tasks with `depends_on=[(),(0,),()]` → `{(1,0),(2,1)}`; a cycle raises.
- [ ] **Step 2–4:** implement → PASS; local Postgres suite run for the new model.
- [ ] **Step 5: Commit** `feat(pm): materialization proposal model + pure decomposition core (Refs #992)`.

### Task 14: Synthesizer — decompose session + ingest → proposal

**Files:**
- Modify: `server/automations/sdlc_synthesizer.py` (on `Materialize`: `rec = await self._spawn_materialize(ini)`; set status `awaiting_human`; emit `materialization_proposed`)
- Modify: `server/sessions/orchestrator.py` (`_sdlc_spawn_materialize(ini)`: read the merged plan artifact off `origin/<default>` exactly as `_sdlc_spawn_synth` reads the upstream artifact; spawn with `origin_ref=f"{slug}:materialize"`, `dedupe_key` same; chain an ingest task mirroring `_pm_classify_chain_ingest` → `_sdlc_materialize_ingest(session_id, slug)` parses the final message with `parse_materialization_output` and inserts one `MaterializationProposal(status="pending")`; a parse failure emits `materialization_parse_failed` and sets the initiative `blocked`)
- Test: `server/tests/test_sdlc_synthesizer_loop.py`, `server/tests/test_pm_orchestrator_seams.py`

- [ ] **Step 1: Failing tests:** loop: a `Materialize` action spawns once (dedupe on second tick while pending), status → `awaiting_human`, event emitted; ingest: canned session output → one pending proposal row with `payload["tasks"]` length 3.
- [ ] **Step 2–4:** implement → PASS.
- [ ] **Step 5: Commit** `feat(sdlc): decompose session posts a materialization proposal (Refs #992)`.

### Task 15: Human gate API + journaled apply

**Files:**
- Modify: `server/api/sdlc.py` (`GET /initiatives/{slug}/materialization` → latest proposal; `POST …/materialization/accept`; `POST …/materialization/reject`)
- Create: `server/pm/materialize_apply.py` (`async def apply(db, proposal, *, fetch: Fetch, now) -> dict` — session-level orchestration)
- Modify: `server/sessions/orchestrator.py` (seam `sdlc_materialize_apply` using `_pm_sync_fetch` for issue mutations — ambient/App credential is enough for issues; NOT the PAT path)
- Test: `server/tests/test_sdlc_api.py`, `server/tests/test_pm_materialize_apply.py` (fake `fetch` returning canned node ids; assert journal rows + order)

**Apply algorithm (write-ahead, no blind retry):**
1. `repository(owner,name){ id }` → `repository_id`.
2. Journal row `create_issue` (target = repository id, `new_value={"title"}`, `initiated_by="materialize:<slug>"`) → `createIssue` epic → store `{node_id, number, url}` on the journal row (`applied=True`).
3. Same per task, in order. Any failure: mark the row failed, stop, leave the initiative `blocked` with the partial list in the proposal payload (`payload["created"]`) so a re-run resumes from the journal (idempotent: a task with a recorded node id is skipped).
4. `add_sub_issue` epic←task ×N (journaled), then `add_blocked_by` for `chain_edges` (journaled; the reconciler will see these as native edges on the next mirror pass and never re-issue them).
5. Overlay: `Initiative.epic_repo/epic_number = epic`; for each created task insert a `WorkItem(repo, number, kind="issue", state="open", title, body, url, gh_node_id, triage_state="classified", classified_at=now, reach/impact/effort/confidence = initiative's)` (spec §7b "Score seeded from initiative RICE"); `proposal.status="accepted"`, `resolved_at=now`; `request_project_sync` for the product so membership/Stage/Score land on the next reconcile.
6. Emit `materialized` with counts.

- [ ] **Step 1: Failing tests:** accept on a pending proposal → journal rows in the order above, epic linked, N WorkItems classified with the initiative RICE, proposal accepted, sync requested; reject → status rejected, nothing journaled; accept twice → 409; a failed `createIssue` on task 2 → rows 1 applied, row 2 failed, no sub-issue/edge rows, initiative `blocked`.
- [ ] **Step 2–4:** implement → PASS; four-step gate.
- [ ] **Step 5: Commit** `feat(sdlc): human-gated materialization apply — journaled epic + sub-issues + chain (Refs #992)`.

### Task 16: Minimal proposal UI (accept / reject)

**Files:**
- Modify: `web/src/api/sdlc.ts` (`fetchMaterialization`, `acceptMaterialization`, `rejectMaterialization`, `MaterializationProposal` type)
- Modify: `web/src/initiatives/InitiativesTab.tsx` (when the selected initiative is at `plan` with a pending proposal: a card listing the epic title and the task titles with their `depends_on`, Accept / Reject buttons — reuse the triage card's button idiom from `web/src/triage/`)
- Test: `web/src/initiatives/__tests__/InitiativesTab.materialization.test.tsx`

- [ ] **Step 1: Failing test:** pending proposal renders 3 task rows; Accept calls the endpoint and the card disappears on refetch.
- [ ] **Step 2–4:** implement; full web gate.
- [ ] **Step 5: Commit + PR C4** `feat(sdlc): plan materialization — proposal, human gate, journaled apply (Fixes #992)`. Body `Fixes #992`, `Refs #997`. Ready + auto-merge.

**Acceptance check after deploy:** Phase D's plan (#993's plan doc, merged through the SDLC as a
`plan` artifact) produces a board-visible epic with sub-issues and a blocked-by chain without a
hand-built issue.

---

## Done criteria (from #990, #991, #992)

- `GET /api/pm/queue` and the runner agree (same composition); runner status card shows
  `picked X (score S, product P) over Y`; `backlog_github.query_candidates` and
  `backlog_deps.blocked_refs` no longer exist; `eligible_label` gone from config.
- Epic #997 has an Initiative row created by adoption; a board `Stage` edit drives
  `Initiative.stage`; InitiativeTrack shows `Build n/m`.
- A merged plan artifact yields a pending proposal; accepting it creates epic + sub-issues +
  chain, journaled; the reconciler adds them to the board with `Stage=build` and a seeded `Score`.

## Self-review (run before handing off)

- **Spec coverage:** §7a → Tasks 2–8 (eligibility, ordering, safety net, `respect_dependencies`,
  provenance, deletions). §7b → Tasks 12–16 (Materialize action, proposal gate, mechanical
  creation, overlay link) and Tasks 9–10 (adoption). §7c queue view → Task 4 endpoint only (UI in D).
  §9 testing → pure cores TDD (Tasks 2, 13), fixture payloads (Tasks 1, 3, 6), schema lists (Tasks 1,
  13), web gate (Tasks 10, 16). §10 failure modes → Task 6 fails closed; Task 15 journal resume.
- **Type consistency:** `QueueRow`/`QueueEntry` fields identical in Tasks 2–5; `to_candidate` keys
  match `plan_to_batch`'s reads (`ref`, `repo`, `title`); `Materialize` + `InitiativeView.epic`
  names match between Tasks 12 and 14; journal kinds `create_issue`/`add_sub_issue`/`add_blocked_by`
  consistent between Tasks 13 and 15.

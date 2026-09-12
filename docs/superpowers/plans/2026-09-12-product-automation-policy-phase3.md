# Product Automation Policy — Phase 3 (Dispatch Outcomes + Consumers) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every Backlog Runner dispatch leaves one durable outcome row, and the queue, the roadmap and the runner's status card read those rows to stop re-dispatching an issue that keeps bouncing on the same body ("bounced N× — needs spec") until a human edits it.

**Architecture:** A new `dispatch_outcomes` table is written through two `AutomationContext` seams: `record_outcome` at spawn (`running`) and `finalize_outcome` at each terminal branch of `_sweep_one`. One pure module, `server/pm/outcomes.py`, owns the body fingerprint, the bounce query and the one predicate `bounced_out(bounces, give_up_after_bounces)` that the queue's `skip_reason` (`needs_spec`) and the roadmap's `issue_reason` both call. The runner's per-product status gains a structured `products` list with `bounced`; the web renders the leaf chip and a per-product table.

**Tech Stack:** Python 3.12 / FastAPI / SQLAlchemy (SQLite tests, Postgres prod; `server/pm/queue_store.py` is a SYNC session, `server/api/pm_tree.py` and the automation context are ASYNC); React + TypeScript + React Query + vitest.

**Spec:** `docs/superpowers/specs/2026-09-12-product-automation-policy-design.md` — D5, D8, §5 (spawn records `running`; status card "2 bounced"), §6 (the table and its writers/reads), §7 (queue `needs_spec`; roadmap `reason` + chip), §9 (per-product table on the runner detail), §10, §11 phase 3, §12, §13. Phases 1–2 landed (#1599, #1601): `server/automations/product_policy.py`, the per-product `_backlog_query_queue`, `AutomationContext` target-state seams.

## Global Constraints

- Constitution VI: ONE fingerprint function (`body_fingerprint`), ONE bounce predicate (`bounced_out`), ONE bounce query builder (`bounces_stmt`), ONE skip vocabulary (`SKIP_REASONS`), ONE tier computation per kind (`issue_tier`/`issue_reason` in `server/pm/tree.py`). No copies of the count-vs-threshold comparison anywhere else.
- Spec §6 columns, verbatim: `id, created_at, updated_at, automation_id, project_id, target_ref, body_fingerprint, session_id, model, attempt_no, started_at, ended_at, duration_s, outcome, cause, pr_number, pr_url, first_pass_merge`. `outcome ∈ running | merged | no_pr | pr_gave_up | needs_human | reverted | abandoned` (`reverted` reserved, never written in v1). Every column except `id`/`created_at` nullable or defaulted, so `_reconcile_columns` creates it on both dialects and no `RECONCILE_EXEMPT`/`_REQUIRED_EXTRAS` entry is needed; `_FK_LINKS` gains `project_id → projects`, `session_id → sessions`. Migration `0044_dispatch_outcomes` chained off `0043_triage_auto_undone` (the spec's "0041" is stale).
- Spec §13: history is best-effort, dispatch is not — an outcome write that fails is logged and never blocks a spawn or a sweep.
- D8: `give_up_after_bounces` (policy, int, default 2, min 0; 0 = never) is the one threshold; bounces are counted only for finalised outcomes whose `body_fingerprint` equals the CURRENT mirror row's fingerprint and whose `outcome ∉ {merged, running}`.
- `first_pass_merge` is `True`/`False` only when PR Doctor's attempt count for that PR is known at the moment the runner's sweep sees the merge; `None` when unknown (PR Doctor may already have cleared its row) — never guessed.
- `AutomationStatus` gains `products: list[dict] | None = None` (a new field, NOT inside `counts`, whose web type is int-only).
- Tests never spell the legacy package name in a monkeypatch string (brand ratchet #1374, ceiling 1460).
- Commit trailer exactly `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`; `Refs #1521` in commit bodies; the PR body says `Refs #1521` (phase 4 closes it).
- Server gate per task: `ruff check src/ server/`; `mypy server/ --ignore-missing-imports`; `python3 scripts/brand_ratchet.py`; the task's test files with the prefix `env -u CCSWITCH_SESSIOND_ENDPOINT -u CCSWITCH_SESSIOND_CLIENT_CERT -u CCSWITCH_SESSIOND_CLIENT_KEY -u CCSWITCH_SESSIOND_CA -u CCSWITCH_DAEMON_ROLE -u CCSWITCH_TAILNET_ALLOWLIST -u VIBERPIT_SESSIOND_ENDPOINT -u VIBERPIT_SESSIOND_CLIENT_CERT -u VIBERPIT_SESSIOND_CLIENT_KEY -u VIBERPIT_SESSIOND_CA -u VIBERPIT_DAEMON_ROLE -u VIBERPIT_TAILNET_ALLOWLIST CCSWITCH_DEV=1 /home/tony/GitHub/viberpit/.venv/bin/python -m pytest … -q -p no:cacheprovider`. Web gate: `(cd web && npm run typecheck; npm run lint; npm run test; npm run build); rm -rf web/dist`, each rc checked.
- Task order: 1 → 2 → (3 ∥ 4) → 5 → 6. Tasks 3 and 4 touch disjoint files.

## File map

| File | Responsibility |
|---|---|
| `server/db/models.py` | `DispatchOutcome` model, `DISPATCH_OUTCOMES` vocabulary |
| `server/db/migrations/versions/0044_dispatch_outcomes.py` (new) | the record |
| `server/pm/outcomes.py` (new) | `body_fingerprint`, `bounced_out`, `bounces_stmt`, `rows_to_bounces`, `list_outcomes`, `outcome_to_dict` |
| `server/automations/context.py` | `record_outcome`, `finalize_outcome` seams |
| `server/automations/product_policy.py` | `give_up_after_bounces` field |
| `server/automations/backlog_runner.py` | writers at spawn and in `_sweep_one`; `PrDoctorAttemptsFor` seam; `status()` products |
| `server/sessions/orchestrator.py` | `_backlog_pr_doctor_attempts_for`; `_backlog_query_queue` passes the threshold and `bounced` |
| `server/automations/base.py` | `AutomationStatus.products` |
| `server/api/automations.py` | serializes `products` |
| `server/pm/queue.py` | `QueueRow.bounces`, `needs_spec` in `SKIP_REASONS`/`skip_reason`, `rank(give_up_after_bounces=)` |
| `server/pm/queue_store.py` | bounce lookup per product |
| `server/pm/tree.py` | `issue_reason`, `issue_tier` honours it |
| `server/api/pm_tree.py` | `_leaf` gains `reason`/`bounces`; bounce lookup |
| `server/api/pm.py` | `GET /work-items/{id}/outcomes` |
| `web/src/api/roadmap.ts`, `web/src/api/pm.ts`, `web/src/api/hooks.ts` | `Leaf.reason`/`bounces`; `fetchOutcomes`/`useOutcomes` |
| `web/src/features/products/vocab.ts` | `REASON_LABEL.needs_spec`, `bouncedLabel` |
| `web/src/features/roadmap/detail/LeafList.tsx` | the chip |
| `web/src/features/delivery/automation/AutomationDetail.tsx` | per-product table |
| `docs/product/features/product-management-layer.md`, `autopilot-backlog-runner.md` | docs |

---

### Task 1: The table, the pure module, the policy field, the context seams

**Files:**
- Modify: `server/db/models.py` (after `AutomationTargetState`, ~:651)
- Create: `server/db/migrations/versions/0044_dispatch_outcomes.py`
- Modify: `server/tests/test_postgres_compat.py:241-266` (`_FK_LINKS`)
- Create: `server/pm/outcomes.py`
- Modify: `server/automations/product_policy.py` (field after `max_attempts`)
- Modify: `server/automations/context.py` (two seams after `list_target_states`, ~:172)
- Test: `server/tests/test_pm_outcomes.py` (new), `server/tests/test_automation_context_outcomes.py` (new), `server/tests/test_product_policy.py`, `server/tests/test_schema_reconcile_coverage.py`, `server/tests/test_postgres_compat.py`

**Interfaces (produces):**

```python
# server/db/models.py
DISPATCH_OUTCOMES: tuple[str, ...] = (
    "running", "merged", "no_pr", "pr_gave_up", "needs_human", "reverted", "abandoned")

class DispatchOutcome(Base):
    __tablename__ = "dispatch_outcomes"
    id: str (pk, default _new_uuid)
    created_at: datetime (default _utcnow, not null)   # the only NOT NULL besides id
    updated_at: datetime | None
    automation_id: str | None (index)
    project_id: str | None (FK projects.id, index)
    target_ref: str | None (index)
    body_fingerprint: str | None
    session_id: str | None (FK sessions.id)
    model: str | None
    attempt_no: int | None
    started_at: datetime | None
    ended_at: datetime | None
    duration_s: float | None
    outcome: str | None (default "running", index)
    cause: str | None
    pr_number: int | None
    pr_url: str | None
    first_pass_merge: bool | None
```

```python
# server/pm/outcomes.py
FINALISED_BOUNCE_OUTCOMES: frozenset[str]   # every DISPATCH_OUTCOMES value except running, merged
def body_fingerprint(title: str | None, body: str | None) -> str          # sha256 hex of f"{title or ''}\n\n{body or ''}"
def bounced_out(bounces: int, give_up_after_bounces: int) -> bool         # give_up_after_bounces > 0 and bounces >= give_up_after_bounces
def bounces_stmt(pairs: Iterable[tuple[str, str]]) -> Select              # (target_ref, fingerprint) → SELECT target_ref, body_fingerprint, count(*) grouped, outcome IN FINALISED_BOUNCE_OUTCOMES
def rows_to_bounces(rows, pairs) -> dict[str, int]                        # ref → count, only where the row's fingerprint == the pair's fingerprint; missing → 0
def list_outcomes_stmt(target_ref: str) -> Select                         # newest first
def outcome_to_dict(row: DispatchOutcome) -> dict                         # camelCase keys, ISO datetimes
```

```python
# server/automations/context.py
async def record_outcome(self, target: str, *, product_slug: str | None, session_id: str | None,
                         model: str | None, attempt_no: int) -> None
    # inserts DispatchOutcome(outcome="running", started_at=now, automation_id=self.automation_id,
    # project_id = Project.id for product_slug (None if unknown), target_ref=target,
    # body_fingerprint = body_fingerprint(WorkItem.title, WorkItem.body) for the mirror row
    # whose f"{repo}#{number}" == target (None if no mirror row)). Best-effort: log + swallow.
async def finalize_outcome(self, target: str, *, outcome: str, cause: str | None = None,
                           pr_number: int | None = None, pr_url: str | None = None,
                           first_pass_merge: bool | None = None) -> None
    # the NEWEST row for (automation_id, target) with outcome == "running" gets outcome/cause/pr_*/
    # first_pass_merge, ended_at=now, duration_s, updated_at. No running row → log at info, no-op.
    # Best-effort: log + swallow. `outcome` must be in DISPATCH_OUTCOMES else ValueError (a bug, not a blip).
```

Policy field: `ConfigField(key="give_up_after_bounces", label="Give up after bounces", type="int", default=2, min=0, help="Finalised non-merged outcomes on the same issue body before the queue skips it as needs_spec and the roadmap flags it; 0 never gives up. Editing the issue body restarts the count.")` after `max_attempts`.

- [ ] **Step 1: Failing tests for the pure module** — `server/tests/test_pm_outcomes.py`:

```python
from server.pm.outcomes import (FINALISED_BOUNCE_OUTCOMES, body_fingerprint, bounced_out,
                                bounces_stmt, rows_to_bounces)
from server.db.models import DISPATCH_OUTCOMES


def test_fingerprint_is_stable_and_body_sensitive():
    a = body_fingerprint("T", "body")
    assert a == body_fingerprint("T", "body") and len(a) == 64
    assert a != body_fingerprint("T", "body edited")
    assert body_fingerprint(None, None) == body_fingerprint("", "")


def test_bounced_out_threshold_and_zero_means_never():
    assert bounced_out(2, 2) and bounced_out(3, 2)
    assert not bounced_out(1, 2)
    assert not bounced_out(99, 0)


def test_finalised_bounce_outcomes_are_everything_but_running_and_merged():
    assert FINALISED_BOUNCE_OUTCOMES == frozenset(DISPATCH_OUTCOMES) - {"running", "merged"}


def test_rows_to_bounces_matches_only_the_current_fingerprint():
    pairs = [("o/r#1", "fp-now"), ("o/r#2", "fp-now2")]
    rows = [("o/r#1", "fp-now", 2), ("o/r#1", "fp-old", 5), ("o/r#2", "fp-other", 1)]
    assert rows_to_bounces(rows, pairs) == {"o/r#1": 2, "o/r#2": 0}


def test_bounces_stmt_filters_on_finalised_outcomes():
    sql = str(bounces_stmt([("o/r#1", "fp")]))
    assert "dispatch_outcomes" in sql and "outcome" in sql and "count" in sql.lower()
```

- [ ] **Step 2: Failing context tests** — `server/tests/test_automation_context_outcomes.py` (copy the `db_env`/`init_db` idiom from `server/tests/test_pm_triage_seams.py:40-60`; seed a `Project(slug="viberpit")`, a `ProjectRepo`, a `WorkItem(repo="o/r", number=7, title="T", body="B")`):

```python
async def test_record_then_finalize_writes_one_row(db_env):
    ctx = AutomationContext("backlog-runner")
    await ctx.record_outcome("o/r#7", product_slug="viberpit", session_id=None, model="sonnet", attempt_no=0)
    await ctx.finalize_outcome("o/r#7", outcome="no_pr", cause="session ended")
    async with db_session() as db:
        rows = (await db.execute(select(DispatchOutcome))).scalars().all()
    assert len(rows) == 1
    r = rows[0]
    assert r.outcome == "no_pr" and r.cause == "session ended" and r.ended_at is not None
    assert r.body_fingerprint == body_fingerprint("T", "B")
    assert r.project_id is not None and r.attempt_no == 0 and r.model == "sonnet"
    assert r.duration_s is not None and r.duration_s >= 0


async def test_finalize_touches_only_the_newest_running_row(db_env):
    (record twice for the same target (two attempts); finalize once → the newer row is
     finalised, the older stays running — then finalize again → the older one)


async def test_finalize_without_a_running_row_is_a_no_op(db_env):
    (finalize on an unknown target → no exception, no rows)


async def test_finalize_rejects_an_unknown_outcome(db_env):
    (pytest.raises(ValueError) for outcome="exploded")


async def test_record_survives_a_missing_mirror_row_and_unknown_product(db_env):
    (record for "o/r#999" with product_slug="nope" → one row with body_fingerprint None,
     project_id None; no exception)


async def test_writes_are_best_effort(db_env, monkeypatch):
    (patch `server.automations.context.db_session` — through the module object — to raise;
     record_outcome and finalize_outcome return without raising)
```

Write every sketched test out in full.

- [ ] **Step 3: Failing policy test** — append to `server/tests/test_product_policy.py`: the ninth field `give_up_after_bounces` type int, default 2, min 0; `effective_policy(None)["give_up_after_bounces"] == 2`; the exact-key test in `test_schema_keys_and_defaults` updated (and `server/tests/test_backlog_policy_seams.py`'s exact-dict assertion, as phase 2 did).

- [ ] **Step 4: Run all three; expect ImportError / AttributeError / KeyError**

- [ ] **Step 5: Implement the model + migration**

`server/db/models.py` after `AutomationTargetState`:

```python
# Dispatch outcome vocabulary (spec 2026-09-12 product automation policy §6).
# ``reverted`` is reserved — nothing writes it in v1.
DISPATCH_OUTCOMES: tuple[str, ...] = (
    "running", "merged", "no_pr", "pr_gave_up", "needs_human", "reverted", "abandoned")


class DispatchOutcome(Base):
    """One row per Backlog Runner dispatch (spec §6, D5): inserted ``running`` at
    spawn, finalised by the runner's sweep. Never overwritten in place — the
    history the queue's ``needs_spec`` skip and the roadmap's bounce chip read.
    Every column but ``id``/``created_at`` is nullable or defaulted so init_db's
    reconcile creates it on both dialects without a hand-written default."""

    __tablename__ = "dispatch_outcomes"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_new_uuid)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    automation_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    project_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("projects.id"), nullable=True, index=True)
    target_ref: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    body_fingerprint: Mapped[str | None] = mapped_column(String, nullable=True)
    session_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("sessions.id"), nullable=True)
    model: Mapped[str | None] = mapped_column(String, nullable=True)
    attempt_no: Mapped[int | None] = mapped_column(Integer, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_s: Mapped[float | None] = mapped_column(Float, nullable=True)
    outcome: Mapped[str | None] = mapped_column(
        String, nullable=True, default="running", index=True)  # DISPATCH_OUTCOMES
    cause: Mapped[str | None] = mapped_column(String, nullable=True)
    pr_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    pr_url: Mapped[str | None] = mapped_column(String, nullable=True)
    first_pass_merge: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
```

(check `Float` is imported; `sessions.id` is the `Session` table's FK target — confirm the table name with `grep -n '__tablename__ = "sessions"' server/db/models.py`.) Migration `0044_dispatch_outcomes.py` with `op.create_table(...)` mirroring the columns and `revision = "0044_dispatch_outcomes"`, `down_revision = "0043_triage_auto_undone"`. `_FK_LINKS["dispatch_outcomes"] = {"project_id": "projects", "session_id": "sessions"}` in `test_postgres_compat.py`. Run `server/tests/test_schema_reconcile_coverage.py` + `test_postgres_compat.py` — both must pass with no new list entries (state so in the report).

- [ ] **Step 6: Implement `server/pm/outcomes.py`**

```python
"""Dispatch outcome history — pure helpers (spec 2026-09-12 product automation
policy §6/§7). No loop logic: the writers live on ``AutomationContext``, the
readers (queue, roadmap, API) call these with their own session."""
from __future__ import annotations

import hashlib
from collections.abc import Iterable

from sqlalchemy import func, select, tuple_
from sqlalchemy.sql import Select

from server.db.models import DISPATCH_OUTCOMES, DispatchOutcome

FINALISED_BOUNCE_OUTCOMES: frozenset[str] = frozenset(DISPATCH_OUTCOMES) - {"running", "merged"}


def body_fingerprint(title: str | None, body: str | None) -> str:
    """THE "same issue body" identity for D8 — computed at spawn (written on the
    outcome row) and at rank/tree time (from the mirror row) through this one
    function, so an edited issue stops matching on both sides at once."""
    return hashlib.sha256(f"{title or ''}\n\n{body or ''}".encode()).hexdigest()


def bounced_out(bounces: int, give_up_after_bounces: int) -> bool:
    """THE bounce rule: the queue's ``needs_spec`` skip and the roadmap's
    ``needs_spec`` reason both call this. ``0`` means never."""
    return give_up_after_bounces > 0 and bounces >= give_up_after_bounces


def bounces_stmt(pairs: Iterable[tuple[str, str]]) -> Select:
    """Grouped counts of FINALISED non-merged outcomes for the given
    ``(target_ref, fingerprint)`` pairs. Callers execute it in their own
    session (sync or async) and hand the rows to ``rows_to_bounces``."""
    pairs = list(pairs)
    return (select(DispatchOutcome.target_ref, DispatchOutcome.body_fingerprint,
                   func.count(DispatchOutcome.id))
            .where(DispatchOutcome.outcome.in_(FINALISED_BOUNCE_OUTCOMES),
                   tuple_(DispatchOutcome.target_ref, DispatchOutcome.body_fingerprint).in_(pairs))
            .group_by(DispatchOutcome.target_ref, DispatchOutcome.body_fingerprint))


def rows_to_bounces(rows: Iterable[tuple[str, str, int]],
                    pairs: Iterable[tuple[str, str]]) -> dict[str, int]:
    """``{target_ref: bounces}`` for every pair, 0 when no row matched the
    CURRENT fingerprint (a row for an older body never counts)."""
    want = dict(pairs)
    out = {ref: 0 for ref in want}
    for ref, fp, n in rows:
        if want.get(ref) == fp:
            out[ref] = int(n)
    return out


def list_outcomes_stmt(target_ref: str) -> Select:
    return (select(DispatchOutcome).where(DispatchOutcome.target_ref == target_ref)
            .order_by(DispatchOutcome.created_at.desc()))


def outcome_to_dict(r: DispatchOutcome) -> dict:
    iso = lambda d: d.isoformat() if d else None  # noqa: E731
    return {"id": r.id, "targetRef": r.target_ref, "projectId": r.project_id,
            "sessionId": r.session_id, "model": r.model, "attemptNo": r.attempt_no,
            "outcome": r.outcome, "cause": r.cause, "prNumber": r.pr_number, "prUrl": r.pr_url,
            "firstPassMerge": r.first_pass_merge, "bodyFingerprint": r.body_fingerprint,
            "startedAt": iso(r.started_at), "endedAt": iso(r.ended_at),
            "durationS": r.duration_s, "createdAt": iso(r.created_at)}
```

(`tuple_(...).in_(pairs)` works on SQLite ≥ 3.15 and Postgres; if the SQLite in the venv rejects it, fall back to `or_(and_(ref==, fp==) …)` per pair — say which in the report and keep ONE builder.)

- [ ] **Step 7: Implement the two context seams** in `server/automations/context.py` (after `list_target_states`):

```python
    async def record_outcome(self, target: str, *, product_slug: str | None,
                             session_id: str | None, model: str | None,
                             attempt_no: int) -> None:
        """Insert the ``running`` outcome row for a dispatch (spec §6). Best-effort:
        history must never block a spawn (§13)."""
        from server.db.models import DispatchOutcome, Project, WorkItem
        from server.pm.outcomes import body_fingerprint
        from server.pm.repo_project import split_ref  # or the module's ref splitter — grep
        try:
            async with db_session() as db:
                project_id = None
                if product_slug:
                    project_id = (await db.execute(select(Project.id).where(
                        Project.slug == product_slug))).scalar_one_or_none()
                fingerprint = None
                parts = split_ref(target)
                if parts is not None:
                    repo, number = parts
                    wi = (await db.execute(select(WorkItem).where(
                        WorkItem.repo == repo, WorkItem.number == number,
                        WorkItem.kind == "issue"))).scalars().first()
                    if wi is not None:
                        fingerprint = body_fingerprint(wi.title, wi.body)
                db.add(DispatchOutcome(
                    automation_id=self.automation_id, project_id=project_id,
                    target_ref=target, body_fingerprint=fingerprint, session_id=session_id,
                    model=model, attempt_no=attempt_no,
                    started_at=datetime.now(timezone.utc), outcome="running"))
        except Exception:
            log.warning("automation_context.record_outcome_failed target=%s", target, exc_info=True)

    async def finalize_outcome(self, target: str, *, outcome: str, cause: str | None = None,
                               pr_number: int | None = None, pr_url: str | None = None,
                               first_pass_merge: bool | None = None) -> None:
        """Finalise the newest ``running`` row for *target* (spec §6). An unknown
        outcome is a programming error and raises; a DB failure is logged and
        swallowed (§13)."""
        from server.db.models import DISPATCH_OUTCOMES, DispatchOutcome
        if outcome not in DISPATCH_OUTCOMES:
            raise ValueError(f"unknown dispatch outcome {outcome!r}")
        try:
            async with db_session() as db:
                row = (await db.execute(
                    select(DispatchOutcome).where(
                        DispatchOutcome.automation_id == self.automation_id,
                        DispatchOutcome.target_ref == target,
                        DispatchOutcome.outcome == "running")
                    .order_by(DispatchOutcome.created_at.desc()).limit(1))).scalars().first()
                if row is None:
                    log.info("automation_context.finalize_outcome_no_running_row target=%s", target)
                    return
                now = datetime.now(timezone.utc)
                row.outcome, row.cause = outcome, cause
                row.pr_number, row.pr_url, row.first_pass_merge = pr_number, pr_url, first_pass_merge
                row.ended_at, row.updated_at = now, now
                if row.started_at is not None:
                    started = row.started_at if row.started_at.tzinfo else row.started_at.replace(tzinfo=timezone.utc)
                    row.duration_s = (now - started).total_seconds()
        except Exception:
            log.warning("automation_context.finalize_outcome_failed target=%s", target, exc_info=True)
```

(Use the repo's existing ref splitter — grep `def split_ref` — rather than a new one. Use `server/db/clock.py:db_utc_now()` if the file's siblings do; never `func.now()`.)

- [ ] **Step 8: Add the policy field; run the gates** — the three new/changed test files + `server/tests/test_product_policy.py server/tests/test_backlog_policy_seams.py server/tests/test_schema_reconcile_coverage.py server/tests/test_postgres_compat.py`; ruff; mypy; ratchet.

- [ ] **Step 9: Commit** — `feat(pm): policy phase 3 task 1 — dispatch_outcomes table, outcome helpers, context seams, give_up_after_bounces` (+ `Refs #1521`, trailer).

---

### Task 2: The runner writes outcomes

**Files:**
- Modify: `server/automations/backlog_runner.py` — `_refill` after spawn (~:1069-1147), `_sweep_one` (~:1231-1347), a new seam type `PrDoctorAttemptsFor = Callable[[str], Awaitable["int | None"]]` and constructor kwarg `pr_doctor_attempts_for: PrDoctorAttemptsFor | None = None`
- Modify: `server/sessions/orchestrator.py` — `_backlog_pr_doctor_attempts_for(pr_ref: str) -> int | None` reading PR Doctor's target state `total_attempts` for `f"{repo}#{pr_number}"` (`AutomationContext("pr-doctor")`-keyed row; see `orchestrator.py:~9742`); wired into the loop construction
- Test: `server/tests/test_backlog_runner_loop.py` (real `AutomationContext`, assert via `select(DispatchOutcome)`), `server/tests/test_backlog_policy_seams.py` (the attempts seam)

**Interfaces:**
- Consumes (Task 1): `ctx.record_outcome(target, *, product_slug, session_id, model, attempt_no)`, `ctx.finalize_outcome(target, *, outcome, cause, pr_number, pr_url, first_pass_merge)`.
- Produces: outcome rows for every dispatch; the mapping of sweep branches to outcomes:

| `_sweep_one` branch | `finalize_outcome(...)` |
|---|---|
| merged, close-out `closed` (normal) | `outcome="merged", pr_number=info.number, pr_url=info.url, first_pass_merge=(attempts == 0) if attempts is not None else None` where `attempts = await self._pr_doctor_attempts_for(f"{repo}#{info.number}")` |
| merged, close-out `escalated` | `outcome="needs_human", cause=reason, pr_number, pr_url` |
| merged, close-out `retry`/`unlinked` | no finalisation (not terminal) |
| PR Doctor gave up | `outcome="pr_gave_up", cause="pr-doctor gave up", pr_number, pr_url` |
| PR closed unmerged | `outcome="abandoned", cause="PR closed unmerged", pr_number, pr_url` |
| handed off | no finalisation (the merge or give-up finalises later) |
| session ended, no PR, attempts remain | `outcome="no_pr", cause="session ended without a mergeable PR"` |
| session ended, at cap, never ran a turn (released) | `outcome="abandoned", cause="session never ran a turn"` |
| session ended, at cap, give-up | `outcome="needs_human", cause=reason` |

At spawn (auto path, after `rec = await self._spawn(...)` succeeds, before the post-spawn `set_target_state`): `await self._ctx.record_outcome(batch.primary, product_slug=batch.product, session_id=getattr(rec, "id", None), model=getattr(rec, "model", None) or policy["default_model"], attempt_no=st.get("attempts", 0))`. The propose path records nothing (no dispatch). Manual dispatch / the propose tap (`_backlog_policy_model` paths in the orchestrator) — find where a tap spawns and record there too, with `attempt_no` from the claim; say where.

- [ ] **Step 1: Failing loop tests** in `server/tests/test_backlog_runner_loop.py` (read `build_loop`/`batch()` and how existing tests drive one spawn then one sweep with a fake `pr_status_for`/`session_ended`):

```python
async def _outcomes(target):
    async with db_session() as db:
        return (await db.execute(select(DispatchOutcome).where(
            DispatchOutcome.target_ref == target).order_by(DispatchOutcome.created_at))).scalars().all()


async def test_spawn_records_a_running_outcome(...):
    (one auto spawn → one row: outcome running, attempt_no 0, session_id == the fake record's id,
     model == the policy default_model, project_id set for the seeded product)

async def test_session_ended_without_pr_finalises_no_pr(...):
    (spawn; session_ended → True; pr_status None; sweep → the row is no_pr with ended_at)

async def test_merged_pr_finalises_merged_with_first_pass_from_pr_doctor(...):
    (spawn; pr_status merged #12; pr_doctor_attempts_for returns 0 → merged, first_pass_merge True,
     pr_number 12; a second run with attempts 2 → False; a third with None → first_pass_merge None)

async def test_pr_doctor_gave_up_finalises_pr_gave_up(...)
async def test_pr_closed_unmerged_finalises_abandoned(...)
async def test_max_attempts_give_up_finalises_needs_human(...)
async def test_release_after_never_ran_finalises_abandoned(...)
async def test_handoff_does_not_finalise(...):
    (after handoff the row is still running; then merged → merged)
async def test_a_failing_outcome_write_never_blocks_the_sweep(...):
    (monkeypatch ctx.finalize_outcome — on the instance — to raise; the sweep still gives up /
     deletes the row exactly as before)
```

Write them out in full; extend `build_loop` with a `pr_doctor_attempts` kwarg feeding the seam.

- [ ] **Step 2: Run; expect failures (no rows)**

- [ ] **Step 3: Implement** the spawn record, the seam, the nine branch calls (each `await self._ctx.finalize_outcome(...)` immediately BEFORE the branch's existing `_give_up`/`delete_target_state`/`set_target_state`, so a crash between the two leaves the row finalised rather than lost). Keep each call's `outcome` a literal from the table above.

- [ ] **Step 4: Seam test** in `test_backlog_policy_seams.py`: seed a PR Doctor target-state row `{"total_attempts": 2}` for `o/r#12` → `_backlog_pr_doctor_attempts_for("o/r#12") == 2`; no row → `None`; row `{}` → `None` (a cleared row is unknown, not zero).

- [ ] **Step 5: Gate** — `server/tests/test_backlog_runner_loop.py server/tests/test_backlog_policy_seams.py server/tests/test_backlog_runner_automation.py server/tests/test_backlog_runner_e2e.py server/tests/test_backlog_runner_codex_acceptance.py server/tests/test_automation_context_outcomes.py`; ruff; mypy; ratchet.

- [ ] **Step 6: Commit** — `feat(runner): policy phase 3 task 2 — the runner records a running outcome at spawn and finalises it in the sweep` (+ `Refs #1521`, trailer).

---

### Task 3: The queue's `needs_spec` skip and the runner's per-product `bounced`

**Files:**
- Modify: `server/pm/queue.py:34-35` (`SKIP_REASONS`), `QueueRow` (~:50-75, `bounces: int = 0`), `skip_reason` (~:110-150, new kwarg + branch), `rank` (~:173-200, passes it)
- Modify: `server/pm/queue_store.py:80-163` (`load_queue_rows`: per-product bounce lookup)
- Modify: `server/sessions/orchestrator.py:5212-5234` (`_backlog_query_queue`: `give_up_after_bounces=policy[...]`, `products_out[slug]["bounced"]`)
- Modify: `server/automations/base.py:40-46` (`AutomationStatus.products`), `server/api/automations.py` (serialize `products` beside `status`), `server/automations/backlog_runner.py` `status()` (~:297-344: "N bounced" in the segment; `products=[{slug, dispatch, inFlight, cap, eligible, bounced}]`)
- Test: `server/tests/test_pm_queue.py`, `server/tests/test_pm_queue_store*.py` (find the loader's test), `server/tests/test_backlog_query_queue.py`, `server/tests/test_backlog_runner_automation.py`, `server/tests/test_automations_api.py`

**Interfaces:**
- Consumes (Task 1): `bounced_out`, `bounces_stmt`, `rows_to_bounces`, `body_fingerprint`; policy `give_up_after_bounces`.
- Produces: `skip_reason(..., give_up_after_bounces: int = 0)` returns `"needs_spec"` when `bounced_out(row.bounces, give_up_after_bounces)`, checked immediately AFTER the `needs_human_cooldown` check and BEFORE the status gate (so the card counts it whatever the board says); `rank(rows, *, min_status, respect_dependencies, needs_human_retry_days, now, give_up_after_bounces=0)`; `SKIP_REASONS` gains `"needs_spec"`; `AutomationStatus(text, level, counts, products=None)`; `GET /api/automations` items carry `"products": [...]` (or `null`).

- [ ] **Step 1: Failing tests**

`server/tests/test_pm_queue.py`:

```python
def test_needs_spec_skips_a_bounced_row_before_the_status_gate():
    row = _row(bounces=2, status=STATUS_READY)   # use the file's row factory
    assert skip_reason(row, min_status=STATUS_READY, respect_dependencies=True,
                       needs_human_retry_days=0, now=NOW, give_up_after_bounces=2) == "needs_spec"
    assert skip_reason(row, ..., give_up_after_bounces=3) is None
    assert skip_reason(row, ..., give_up_after_bounces=0) is None
    assert "needs_spec" in SKIP_REASONS


def test_rank_counts_needs_spec():
    entries, skipped = rank([_row(bounces=2), _row(bounces=0, number=2)], min_status=..., ..., give_up_after_bounces=2)
    assert skipped == {"needs_spec": 1} and [e.number for e in entries] == [2]
```

Loader test (in the existing queue-store test file): seed a product with two open board issues; insert `DispatchOutcome` rows — for #1 two `no_pr` rows at the CURRENT fingerprint, one `merged`, one `no_pr` at a stale fingerprint; for #2 none → `load_queue_rows(...)` rows carry `bounces == {1: 2, 2: 0}`.

`server/tests/test_backlog_query_queue.py`: a product with policy `give_up_after_bounces: 2` and one bounced row → `products_out[slug]["bounced"] == 1` and the row is not in the pool; with `give_up_after_bounces: 0` → in the pool, `bounced == 0`.

`server/tests/test_backlog_runner_automation.py`: `status()` with a per-product dict containing `bounced: 2` renders `… · 2 bounced` in that segment and `status.products == [{"slug": …, "dispatch": …, "inFlight": …, "cap": …, "eligible": …, "bounced": 2}]`; `test_automations_api.py`: the list item carries `products` (list) for the runner and `null` for pr-doctor.

- [ ] **Step 2: Run; expect failures**

- [ ] **Step 3: Implement**

`queue.py`: `SKIP_REASONS = (..., "needs_human_cooldown", "needs_spec")`; `QueueRow.bounces: int = 0` (after `needs_human_at`, defaulted); in `skip_reason` right after the cooldown block:

```python
    # D8: this body has already bounced enough — a spec edit (which changes the
    # fingerprint the count is keyed on) is what re-arms it, not time.
    if bounced_out(row.bounces, give_up_after_bounces):
        return "needs_spec"
```

`queue_store.py` inside the per-product loop, before constructing rows: `pairs = [(f"{w.repo}#{w.number}", body_fingerprint(w.title, w.body)) for _, w in items]`; `bounces = rows_to_bounces(db.execute(bounces_stmt(pairs)).all(), pairs) if pairs else {}`; pass `bounces=bounces.get(ref, 0)` into `QueueRow`. `_backlog_query_queue`: `rank(..., give_up_after_bounces=int(policy["give_up_after_bounces"]))`, `products_out[slug]["bounced"] = skipped_p.get("needs_spec", 0)`. `AutomationStatus`: `products: list[dict] | None = None`. `status()`: build `products` from the per-product dict (keys per §10, camelCase) and append `· {bounced} bounced` to each segment when `> 0` (keep segments one line, ` · ` joined). API: `"products": st.products`. `grep -rn "needs_spec" server/` must show the vocabulary tuple, `skip_reason`, and the status/`products_out` readers only — no second comparison.

- [ ] **Step 4: Gate** — the five test files + `server/tests/test_backlog_runner_loop.py`; ruff; mypy; ratchet.

- [ ] **Step 5: Commit** — `feat(runner,pm): policy phase 3 task 3 — needs_spec queue skip from bounce history; per-product bounced on the status card` (+ `Refs #1521`, trailer).

---

### Task 4: The roadmap reason, the leaf fields, the outcomes endpoint

**Files:**
- Modify: `server/pm/tree.py:97-109` (`issue_reason`, `issue_tier`)
- Modify: `server/api/pm_tree.py` (`_issue_facts` gains bounces; `_leaf` gains `reason`/`bounces`; both `issue_tier` call sites ~:109 and ~:264 pass them; the capability roll-up's needs-you count includes the reason)
- Modify: `server/api/pm.py` (`GET /work-items/{item_id}/outcomes` beside `patch_overlay` ~:1306)
- Test: `server/tests/test_pm_tree.py` (find the tier tests), `server/tests/test_pm_tree_api*.py` / wherever `/api/pm/leaves` is tested, `server/tests/test_pm_api.py`

**Interfaces:**
- Consumes (Task 1): `bounced_out`, `bounces_stmt`, `rows_to_bounces`, `body_fingerprint`, `list_outcomes_stmt`, `outcome_to_dict`; policy `give_up_after_bounces` via `effective_policy(project.automation_policy)`.
- Produces:

```python
# server/pm/tree.py
def issue_reason(*, bounces: int = 0, give_up_after_bounces: int = 0) -> str | None:
    """Why an issue needs a human beyond its tier — the ``InitiativeIn.reason``
    shape. ``needs_spec`` when D8's rule fires (ONE rule: ``bounced_out``)."""
    return "needs_spec" if bounced_out(bounces, give_up_after_bounces) else None

def issue_tier(state, triage_state, has_open_blocker, session_status, board_status=None,
               *, reason: str | None = None) -> str:
    # closed/merged → live (unchanged, first); then: if reason is not None → "needs-you"; then the existing chain
```

`_leaf(...)` gains `"reason": reason, "bounces": bounces`. `GET /api/pm/work-items/{id}/outcomes` → `{"items": [outcome_to_dict(r) ...]}` newest first, 404 `{"error": "not_found"}` for an unknown item; `target_ref` = `f"{w.repo}#{w.number}"`.

- [ ] **Step 1: Failing tests** — tree: `issue_reason(bounces=2, give_up_after_bounces=2) == "needs_spec"`, `issue_tier("open", "classified", False, None, reason="needs_spec") == "needs-you"`, a closed issue with a reason is still `live`; leaves API: seed a product with policy `give_up_after_bounces: 2`, an issue with two `no_pr` outcomes at the current fingerprint → the leaf has `tier "needs-you"`, `reason "needs_spec"`, `bounces 2`; with a stale-fingerprint row only → `reason null`, `bounces 0`, tier unchanged; the product map's `counts.needsYou` includes it; outcomes endpoint: two rows → `items` newest first with camelCase keys; unknown id → 404. Write in full.

- [ ] **Step 2: Run; expect failures**

- [ ] **Step 3: Implement** — `_issue_facts` (async): one `bounces_stmt` execution over the items' pairs; the leaves handler resolves the product's threshold once (`effective_policy(project.automation_policy)["give_up_after_bounces"]`; the map handler already iterates products — thread the threshold per product). Both `issue_tier` call sites compute `reason = issue_reason(bounces=..., give_up_after_bounces=...)` first and pass `reason=`. Never re-compare counts in `pm_tree.py`.

- [ ] **Step 4: Gate** — the test files + `server/tests/test_pm_api.py`; ruff; mypy; ratchet.

- [ ] **Step 5: Commit** — `feat(pm): policy phase 3 task 4 — needs_spec roadmap reason with bounces on leaves; work-item outcomes endpoint` (+ `Refs #1521`, trailer).

---

### Task 5: Web — the leaf chip, the per-product table, the outcomes hook

**Files:**
- Modify: `web/src/api/roadmap.ts:111-131` (`Leaf.reason: string | null`, `Leaf.bounces: number`)
- Modify: `web/src/features/products/vocab.ts:100-111` (`REASON_LABEL.needs_spec = 'Needs spec'`; `export const bouncedLabel = (n: number) => \`bounced ${n}× — needs spec\``)
- Modify: `web/src/features/roadmap/detail/LeafList.tsx` (`LeafRow` ~:60-97: when `leaf.reason` render a `Badge` with `bouncedLabel(leaf.bounces)` for `needs_spec` else `reasonLabel(leaf.reason)`, plus sr-only text), and the needs-you surface that lists leaves (grep `LeafList` / `NeedsYouList` usages — apply the same chip where leaves appear)
- Modify: `web/src/api/automations.ts` (`AutomationStatus.products?: {slug, dispatch, inFlight, cap, eligible, bounced}[] | null`), `web/src/features/delivery/automation/AutomationDetail.tsx` (a per-product table when `status.products` is non-empty: columns Product (link to `/roadmap/:slug`), Dispatch, In flight (`inFlight/cap`), Eligible, Bounced)
- Modify: `web/src/api/pm.ts` (`fetchOutcomes(workItemId)`), `web/src/api/hooks.ts` (`useOutcomes(workItemId, {enabled})`)
- Test: `LeafList.test.tsx`, `AutomationDetail.test.tsx` (exists? create beside it), `web/src/api/__tests__/…` for the hook, fixtures gaining the new `Leaf` keys (typecheck lists them)

- [ ] **Step 1: Failing tests** — `LeafList.test.tsx`: a leaf `{reason: 'needs_spec', bounces: 2, tier: 'needs-you'}` renders text `bounced 2× — needs spec`; a leaf with `reason: null` renders no chip. `AutomationDetail.test.tsx`: `status.products = [{slug:'viberpit', dispatch:'auto', inFlight:1, cap:1, eligible:12, bounced:2}]` renders a row with those cells and a link to `/roadmap/viberpit`; `products: null` renders no table. Hook test: `useOutcomes('w1')` calls `/api/pm/work-items/w1/outcomes` and returns `items`.

- [ ] **Step 2: Implement** — design tokens only (`ccswitch/no-raw-palette`); icons inside named controls `aria-hidden`; the table uses the existing table primitives (grep `<table` under `web/src/features/delivery` for the pattern).

- [ ] **Step 3: Full web gate** — typecheck, lint, test (full), build.

- [ ] **Step 4: Commit** — `feat(roadmap,delivery): policy phase 3 task 5 — "bounced N× — needs spec" leaf chip; per-product runner table; outcomes hook` (+ `Refs #1521`, trailer).

---

### Task 6: Docs

**Files:**
- Modify: `docs/product/features/product-management-layer.md` (policy section: nine fields; a "Dispatch outcomes" paragraph: the table, writers per sweep branch, best-effort, `first_pass_merge` semantics incl. `None`; the `needs_spec` rule with the fingerprint; the leaf chip; the outcomes endpoint), `docs/product/features/autopilot-backlog-runner.md` (outcome rows at spawn/sweep; the `needs_spec` skip and "N bounced" segment; `products` on the status payload; the per-product table), frontmatter `links.code` += `server/pm/outcomes.py`

Every sentence verified against the landed code (cite the symbol per claim in the report); `python3 scripts/brand_ratchet.py`; commit `docs: policy phase 3 task 6 — dispatch outcomes and the needs_spec consumers` (+ `Refs #1521`, trailer).

---

## Self-review

- Spec coverage: §6 table + writers (T1/T2), reads helper (T1 `outcomes.py`; `outcome_stats` per-capability first-pass rate is phase 4's `tier_floor` input and is NOT built here — phase 4 adds it to the same module), §7 queue `needs_spec` (T3) and roadmap reason/chip (T4/T5), §9 per-product table (T5), §10 endpoint + `products` payload (T4/T3/T5), §12 tests per task, §13 best-effort writes (T1) and vocabulary (T1 `DISPATCH_OUTCOMES`). D9/`tier_floor`/`resolve_model(floor=)` are phase 4.
- Types: `record_outcome`/`finalize_outcome` keyword names match between T1 and T2; `bounces`/`reason` keys match between T4's `_leaf` and T5's `Leaf`; `products` row keys match between T3's `status()` and T5's type; `give_up_after_bounces` is read via `effective_policy` in T3 and T4 alike.
- Open choices stated for the implementer: the `tuple_().in_()` fallback (T1), where the propose-tap spawn records its outcome (T2), which needs-you surface lists leaves (T5).

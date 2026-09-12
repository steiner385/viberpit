# Product Automation Policy — Phase 2 (Triage Auto-Accept) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A product whose policy says `triage_auto_accept: high` (or `medium`) has its qualifying pending triage proposals accepted by the PM Triage loop itself — audited, labelled, and undoable for the same window as a human accept.

**Architecture:** The accept function moves out of the router into `server/pm/triage_accept.py` and gains a `decided_by` parameter; the routes pass `"human"`, the loop passes `"auto"`. `PmTriageLoop._run` gains one step, on every tick, that reads the product policies (the phase-1 `load_policies` seam pattern), lists auto-accept candidates through a new injected seam, applies a pure eligibility gate, and accepts through a new injected seam that owns its own DB session. Undo marks an auto-accepted proposal `auto_undone` so the machine never gets a second vote. The inbox list widens to include auto-accepted proposals for the undo window so the web can show the "auto-accepted · undo" chip.

**Tech Stack:** Python 3.12 / FastAPI / SQLAlchemy async (SQLite in tests, Postgres in prod); React + TypeScript + React Query + vitest; pytest with the env-unset prefix.

**Spec:** `docs/superpowers/specs/2026-09-12-product-automation-policy-design.md` — D2, D6, D7, §4 (`triage_auto_accept`), §8, §9 (Inbox chip), §10 (`TriageProposal.auto_undone`), §12, §13 (vocabulary drift), §14. Phase 1 (landed in PR #1599) is the substrate: `server/automations/product_policy.py`, `PATCH /api/pm/projects/{slug}/policy`, the product Overview `AutomationPolicyCard`.

## Global Constraints

- Constitution VI (single source of truth): ONE accept function (`apply_accept`), ONE confidence ordering (`CONFIDENCE_LEVELS` in `server/pm/classification.py`), ONE eligibility gate (`auto_accept_eligible`), ONE undo path (`POST /triage/{id}/undo` + `useUndoProposal`). No copies.
- Spec §8: `classified_by = "auto"` for an auto-accept; audit type `pm.triage.auto_accept` with identity `pm-triage`; `AutomationEvent` kind `auto_accepted`; per product at most `MAX_AUTO_ACCEPTS_PER_TICK = 25` per tick (a constant in `server/automations/pm_triage.py`, never a `ConfigField`); never a no-fit proposal; never a proposal whose target capability's `state == "proposed"`; never a proposal with `auto_undone`.
- Spec §13: a `confidence_level` outside `low|medium|high` (or `None`) is treated as `low` → never auto-accepted.
- PM Triage's `modes` stay `("off", "propose")`; the auto-accept step runs only when the loop's mode is `propose` (the whole `_run` already returns on `off`).
- New columns nullable (`_reconcile_columns` adds them on both dialects); migration `0043_triage_auto_undone` chained off `0042_product_automation_policy`, recorded anyway. No new table → no `_FK_LINKS`/`_REQUIRED_EXTRAS`/`RECONCILE_EXEMPT` changes; say so in the report.
- Tests never spell the legacy package name in a monkeypatch string (brand ratchet #1374, ceiling 1460 in `python-package`): patch through the importing module's reference. Run `python3 scripts/brand_ratchet.py` before every commit.
- Commit trailer exactly `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`; `Refs #1521` in commit bodies; the PR body (not commits) says `Refs #1521`.
- Server gate per task: `ruff check src/ server/`; `mypy server/ --ignore-missing-imports`; the task's test files with the prefix `env -u CCSWITCH_SESSIOND_ENDPOINT -u CCSWITCH_SESSIOND_CLIENT_CERT -u CCSWITCH_SESSIOND_CLIENT_KEY -u CCSWITCH_SESSIOND_CA -u CCSWITCH_DAEMON_ROLE -u CCSWITCH_TAILNET_ALLOWLIST -u VIBERPIT_SESSIOND_ENDPOINT -u VIBERPIT_SESSIOND_CLIENT_CERT -u VIBERPIT_SESSIOND_CLIENT_KEY -u VIBERPIT_SESSIOND_CA -u VIBERPIT_DAEMON_ROLE -u VIBERPIT_TAILNET_ALLOWLIST CCSWITCH_DEV=1 /home/tony/GitHub/viberpit/.venv/bin/python -m pytest … -q -p no:cacheprovider`. Web gate: `(cd web && npm run typecheck; npm run lint; npm run test; npm run build); rm -rf web/dist` — check each step's rc.
- Task order: 1 → 2 → (3 ∥ 4) → 5. Tasks 3 and 4 touch disjoint files and may run in parallel.

## File map

| File | Responsibility |
|---|---|
| `server/pm/classification.py` | `CONFIDENCE_LEVELS` (renamed from `_VALID_CONFIDENCE`): the one confidence ordering |
| `server/pm/triage_accept.py` (new) | `apply_accept` (moved from the router), `WorkItemMissing`, `auto_accept_eligible` (pure gate) |
| `server/automations/product_policy.py` | `PRODUCT_POLICY_SCHEMA` gains `triage_auto_accept` |
| `server/api/pm.py` | routes import `apply_accept`; `undo_triage` marks `auto_undone`; `list_triage` widens; `_triage_to_dict` carries `classifiedBy`/`resolvedAt` |
| `server/db/models.py` | `TriageProposal.auto_undone` |
| `server/db/migrations/versions/0043_triage_auto_undone.py` (new) | the record |
| `server/automations/pm_triage.py` | `AutoAcceptCandidateView`, three new seams, `MAX_AUTO_ACCEPTS_PER_TICK`, the `_auto_accept_pass` step |
| `server/sessions/orchestrator.py` | `_pm_triage_load_policies`, `_pm_triage_list_auto_accept_candidates`, `_pm_triage_auto_accept`; wiring |
| `web/src/api/triage.ts` | `TriageProposal.classifiedBy`, `.resolvedAt` |
| `web/src/features/roadmap/inbox/ProposalCard.tsx` | "auto-accepted" chip + Undo button |
| `web/src/features/roadmap/inbox/InboxScopeBody.tsx` | keyboard/bulk accept skips non-pending rows |
| `docs/product/features/product-management-layer.md` | the eighth policy field; the auto-accept step; the chip |

---

### Task 1: The accept function leaves the router; the gate and the policy field

**Files:**
- Modify: `server/pm/classification.py:29` (rename `_VALID_CONFIDENCE` → `CONFIDENCE_LEVELS`; update the one usage at `:445`)
- Create: `server/pm/triage_accept.py`
- Modify: `server/api/pm.py:1115-1175` (delete `_apply_accept`; import `apply_accept`, `WorkItemMissing`), the three call sites (`accept_triage` ~:1178, `accept_bulk` ~:1277, any other `_apply_accept(` — grep)
- Modify: `server/automations/product_policy.py` (`PRODUCT_POLICY_SCHEMA` gains the field; module docstring's "phase 2" line updated)
- Test: `server/tests/test_triage_accept.py` (new), `server/tests/test_product_policy.py`, `server/tests/test_pm_api.py` (existing accept tests must still pass unchanged — they assert `classified_by == "human"`)

**Interfaces:**
- Produces: `server/pm/triage_accept.py`:
  - `class WorkItemMissing(LookupError)` — raised when the proposal's work item row is gone; the routes map it to 404 `{"error": "work_item_missing"}`.
  - `async def apply_accept(db, proposal: TriageProposal, *, decided_by: str, audit_identity: str, edits: dict | None = None) -> None` — body is `_apply_accept` verbatim except: `wi.classified_by = decided_by`; the nested `pm.triage.repropose` audit uses `identity=audit_identity`; `HTTPException(404)` becomes `raise WorkItemMissing(proposal.work_item_id)`.
  - `def auto_accept_eligible(*, tier: str, confidence_level: str | None, proposed_feature_id: str | None, feature_state: str | None, auto_undone: bool | None) -> bool` — pure.
- Produces: `server/pm/classification.py`: `CONFIDENCE_LEVELS: tuple[str, ...] = ("low", "medium", "high")` (ascending).
- Produces: `PRODUCT_POLICY_SCHEMA` field `ConfigField(key="triage_auto_accept", label="Triage auto-accept", type="enum", default="off", choices=["off", "high", "medium"], help="Accept this product's classification proposals at or above this confidence without a human; medium includes high. Never a no-fit proposal.")`.

- [ ] **Step 1: Write the failing tests for the gate and the field**

`server/tests/test_triage_accept.py`:

```python
"""server/pm/triage_accept: the pure auto-accept gate (spec §8, §13)."""
from __future__ import annotations

import pytest

from server.pm.classification import CONFIDENCE_LEVELS
from server.pm.triage_accept import auto_accept_eligible


def _ok(**over):
    base = dict(tier="high", confidence_level="high", proposed_feature_id="f1",
                feature_state="live", auto_undone=None)
    base.update(over)
    return auto_accept_eligible(**base)


def test_confidence_levels_are_the_one_ascending_vocabulary():
    assert CONFIDENCE_LEVELS == ("low", "medium", "high")


def test_high_tier_accepts_only_high():
    assert _ok(tier="high", confidence_level="high")
    assert not _ok(tier="high", confidence_level="medium")
    assert not _ok(tier="high", confidence_level="low")


def test_medium_tier_includes_high():
    assert _ok(tier="medium", confidence_level="medium")
    assert _ok(tier="medium", confidence_level="high")
    assert not _ok(tier="medium", confidence_level="low")


def test_off_never_accepts():
    assert not _ok(tier="off", confidence_level="high")


@pytest.mark.parametrize("level", [None, "", "HIGH", "certain", "very high"])
def test_a_confidence_outside_the_vocabulary_reads_as_low(level):
    # Spec §13: vocabulary drift is treated as low, never accepted.
    assert not _ok(tier="medium", confidence_level=level)


def test_never_a_no_fit_proposal():
    assert not _ok(proposed_feature_id=None)


def test_never_a_proposal_whose_capability_is_still_proposed():
    assert not _ok(feature_state="proposed")
    assert _ok(feature_state="discovery")
    assert _ok(feature_state="delivering")


def test_a_missing_feature_state_is_not_eligible():
    # The target row is gone (or unknown): a human decides, not the machine.
    assert not _ok(feature_state=None)


def test_an_undone_auto_accept_is_never_retried():
    assert not _ok(auto_undone=True)
    assert _ok(auto_undone=False)
    assert _ok(auto_undone=None)
```

Append to `server/tests/test_product_policy.py`:

```python
def test_triage_auto_accept_is_the_eighth_field_with_the_spec_choices():
    from server.automations.product_policy import PRODUCT_POLICY_SCHEMA, effective_policy
    field = {f.key: f for f in PRODUCT_POLICY_SCHEMA}["triage_auto_accept"]
    assert field.type == "enum"
    assert field.default == "off"
    assert field.choices == ["off", "high", "medium"]
    assert effective_policy(None)["triage_auto_accept"] == "off"
    assert effective_policy({"triage_auto_accept": "medium"})["triage_auto_accept"] == "medium"
    # A bad value degrades the WHOLE policy to defaults (effective_policy's contract).
    assert effective_policy({"triage_auto_accept": "always"})["triage_auto_accept"] == "off"
```

- [ ] **Step 2: Run them; expect ImportError / KeyError**

`pytest server/tests/test_triage_accept.py server/tests/test_product_policy.py -q`

- [ ] **Step 3: Rename the constant**

In `server/pm/classification.py` replace `_VALID_CONFIDENCE = ("low", "medium", "high")` with

```python
# The one confidence vocabulary, ASCENDING — the auto-accept tier gate
# (server/pm/triage_accept.py) orders by index into this tuple.
CONFIDENCE_LEVELS: tuple[str, ...] = ("low", "medium", "high")
```

and the usage at `:445` to `if level not in CONFIDENCE_LEVELS:`. `grep -rn "_VALID_CONFIDENCE" server/` must return nothing.

- [ ] **Step 4: Create `server/pm/triage_accept.py`**

```python
"""Accepting a triage proposal — the ONE function, for humans and the machine.

``apply_accept`` moved here from ``server/api/pm.py`` (spec 2026-09-12 product
automation policy §8, D6) so the PM Triage loop's auto-accept and the inbox's
accept routes share one write path. ``auto_accept_eligible`` is the pure gate the
loop applies before calling it.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select

from server.audit import record_audit
from server.db.models import Feature, TriageProposal, WorkItem
from server.pm.classification import CONFIDENCE_LEVELS
from server.pm.repo_project import owning_projects_by_repo


class WorkItemMissing(LookupError):
    """The proposal's work item row is gone. The API maps this to 404."""


def auto_accept_eligible(*, tier: str, confidence_level: str | None,
                         proposed_feature_id: str | None, feature_state: str | None,
                         auto_undone: bool | None) -> bool:
    """Whether the loop may accept this proposal under a product's
    ``triage_auto_accept`` *tier* (spec §8, §13).

    ``medium`` includes ``high`` (index order in ``CONFIDENCE_LEVELS``). A
    confidence outside the vocabulary — including ``None`` — reads as ``low`` and
    is never accepted. Never a no-fit (creating a capability is a human call),
    never a target capability still ``proposed`` (its doc is unmerged), never a
    proposal a human already undid once (``auto_undone``).
    """
    if tier not in CONFIDENCE_LEVELS:            # "off" or garbage
        return False
    if proposed_feature_id is None or auto_undone:
        return False
    if feature_state is None or feature_state == "proposed":
        return False
    level = confidence_level if confidence_level in CONFIDENCE_LEVELS else "low"
    return CONFIDENCE_LEVELS.index(level) >= CONFIDENCE_LEVELS.index(tier)
```

Then append `apply_accept`: copy the body of `_apply_accept` from `server/api/pm.py` VERBATIM (the docstring, the drift-resolution branch, `_repropose_siblings` — move `_overlay_snapshot` and `_repropose_siblings` too if they are only used by accept; if `_overlay_snapshot` is also used by `reject_triage`, leave it in `pm.py` and import it here as `from server.api.pm import _overlay_snapshot` is a CYCLE — so move `_overlay_snapshot`, `_restore_overlay` and `_aware` into `triage_accept.py` and have `pm.py` import them from there) with exactly these edits:

```python
async def apply_accept(db, proposal: TriageProposal, *, decided_by: str,
                       audit_identity: str, edits: dict | None = None) -> None:
    """…(the moved docstring, with this paragraph replacing the classified_by one)…

    ``classified_by`` is *decided_by*: ``"human"`` from the inbox routes, ``"auto"``
    from the PM Triage loop's auto-accept (spec D7) — never a tailnet identity,
    which is carried only on audit events (*audit_identity*, used here for the
    nested repropose audit and by the caller for the accept audit)."""
    wi = (await db.execute(select(WorkItem).where(
        WorkItem.id == proposal.work_item_id))).scalar_one_or_none()
    if wi is None:
        raise WorkItemMissing(proposal.work_item_id)
    …
                    await record_audit(
                        db=db, type="pm.triage.repropose", identity=audit_identity,
                        …)
    …
    wi.classified_by = decided_by
    …
```

- [ ] **Step 5: Route through it in `server/api/pm.py`**

Delete `_apply_accept` (and the helpers you moved). Add `from server.pm.triage_accept import WorkItemMissing, _aware, _overlay_snapshot, _restore_overlay, apply_accept` (adjust to what you moved; keep `pm.py`'s own uses working). At each former call site:

```python
        try:
            await apply_accept(db, prop, decided_by="human", audit_identity=identity,
                               edits=edits)
        except WorkItemMissing as e:
            raise HTTPException(status_code=404, detail={"error": "work_item_missing"}) from e
```

(`accept_bulk` runs each id inside `db.begin_nested()`; keep that and map `WorkItemMissing` to the per-id failure entry the way the existing code maps its 404 — read it before editing.) `grep -n "_apply_accept" server/` must return nothing.

- [ ] **Step 6: Add the policy field**

In `server/automations/product_policy.py` append to `PRODUCT_POLICY_SCHEMA` after `respect_dependencies`:

```python
    ConfigField(
        key="triage_auto_accept", label="Triage auto-accept", type="enum",
        default="off", choices=["off", "high", "medium"],
        help=("Accept this product's classification proposals at or above this "
              "confidence without a human; medium includes high. Never a no-fit "
              "proposal, never one whose capability doc is still proposed.")),
```

Update the module docstring: `triage_auto_accept` is now in the schema (phase 2); `give_up_after_bounces` remains phase 3.

- [ ] **Step 7: Run the gate**

`pytest server/tests/test_triage_accept.py server/tests/test_product_policy.py server/tests/test_pm_api.py server/tests/test_pm_triage_seams.py -q` → all pass (the human accept tests assert `classified_by == "human"` and still do). `ruff`, `mypy`, `brand_ratchet` clean. Check the web: the product Overview card renders the new field from the schema with no change (`web/src/features/roadmap/detail/AutomationPolicyCard.test.tsx` still passes — run it).

- [ ] **Step 8: Commit**

`feat(pm): policy phase 2 task 1 — apply_accept leaves the router; the auto-accept gate; triage_auto_accept policy field` (+ `Refs #1521`, trailer).

---

### Task 2: `auto_undone`, undo marks it, the inbox list shows auto-accepts for the undo window

**Files:**
- Modify: `server/db/models.py:1766-1800` (`TriageProposal.auto_undone`)
- Create: `server/db/migrations/versions/0043_triage_auto_undone.py`
- Modify: `server/api/pm.py` `undo_triage` (~:1225), `list_triage` (~:981), `_triage_to_dict` (~:956)
- Test: `server/tests/test_pm_api.py` (new tests beside the undo suite ~:1220-1387), `server/tests/test_schema_reconcile_coverage.py` (runs unchanged)

**Interfaces:**
- Consumes: nothing from Task 1 beyond the moved helpers.
- Produces: `TriageProposal.auto_undone: Mapped[bool | None]`; `GET /api/pm/triage` returns pending proposals PLUS proposals with `status == "accepted"` whose work item has `classified_by == "auto"` and `resolved_at` within `TRIAGE_UNDO_WINDOW_S`; every item carries `classifiedBy: str | null` (the WORK ITEM's `classified_by`) and `resolvedAt: str | null`.

- [ ] **Step 1: Write the failing tests**

Append to `server/tests/test_pm_api.py`, using the file's `_client_with_stubbed_seams`, `_register`, `IDENTITY_HEADERS` idioms (read `test_triage_accept_and_reject_flow` at ~:379 first):

```python
def test_undo_of_an_auto_accept_marks_the_proposal_auto_undone():
    """Spec §8: a human undid the machine once; the machine never gets a second
    vote. Undo restores the overlay exactly as for a human accept AND flags the
    proposal."""
    from datetime import datetime, timezone
    from sqlalchemy import select
    from server.db import db_session
    from server.db.models import Feature, Project, TriageProposal, WorkItem
    from server.pm.triage_accept import apply_accept

    with _client_with_stubbed_seams() as c:
        _register(c, "Viberpit", repo="o/r")

        async def _seed():
            async with db_session() as db:
                p = (await db.execute(select(Project).where(
                    Project.slug == "viberpit"))).scalar_one()
                f = Feature(project_id=p.id, slug="sessions", title="Sessions", state="live")
                w = WorkItem(repo="o/r", number=1, kind="issue", title="a",
                             state="open", triage_state="proposed")
                db.add_all([f, w])
                await db.flush()
                prop = TriageProposal(work_item_id=w.id, proposed_feature_id=f.id,
                                      confidence_level="high", status="pending")
                db.add(prop)
                await db.flush()
                await apply_accept(db, prop, decided_by="auto", audit_identity="pm-triage")
                return prop.id
        pid = c.portal.call(_seed)

        r = c.post(f"/api/pm/triage/{pid}/undo", headers=IDENTITY_HEADERS)
        assert r.status_code == 200, r.text

        async def _check():
            async with db_session() as db:
                prop = (await db.execute(select(TriageProposal).where(
                    TriageProposal.id == pid))).scalar_one()
                w = (await db.execute(select(WorkItem).where(WorkItem.number == 1))).scalar_one()
                assert prop.status == "pending" and prop.auto_undone is True
                assert w.triage_state == "proposed" and w.classified_by is None
        c.portal.call(_check)


def test_undo_of_a_human_accept_does_not_mark_auto_undone():
    (same seed with decided_by="human", audit_identity="tester"; after undo
     prop.auto_undone is None)


def test_triage_list_includes_a_fresh_auto_accept_with_its_marker():
    """The inbox chip (spec §8/§9) has nothing to render unless the list carries
    the auto-accepted row for the undo window."""
    (seed one pending proposal + one auto-accepted via apply_accept(decided_by="auto")
     + one human-accepted via apply_accept(decided_by="human");
     GET /api/pm/triage → 2 items: the pending one (classifiedBy None, status
     "pending") and the auto one (status "accepted", classifiedBy "auto",
     resolvedAt an ISO string); the human-accepted one is absent)


def test_triage_list_drops_an_auto_accept_after_the_undo_window():
    (seed an auto-accepted proposal, then set its resolved_at to
     now - TRIAGE_UNDO_WINDOW_S - 5 s in the DB; GET → it is absent)
```

Write the bodies of the three sketched tests in full (copy the first test's seed shape; never leave the parenthetical summaries in the file).

- [ ] **Step 2: Run; expect failures** (`auto_undone` attribute missing; list shape)

- [ ] **Step 3: The column and the migration**

`server/db/models.py`, inside `TriageProposal` after `undo_snapshot`:

```python
    # Set by POST /triage/{id}/undo when the decision being undone was the PM
    # Triage loop's auto-accept (spec 2026-09-12 product automation policy §8):
    # a human undid the machine once, so the loop never auto-accepts this
    # proposal again. Nullable so reconcile ALTER-ADDs it on both dialects;
    # NULL/False = eligible.
    auto_undone: Mapped[bool | None] = mapped_column(Boolean, nullable=True, default=None)
```

`server/db/migrations/versions/0043_triage_auto_undone.py`:

```python
"""triage_auto_undone: auto_undone on triage_proposals

Revision ID: 0043_triage_auto_undone
Revises: 0042_product_automation_policy

Reviewable record (spec 2026-09-12-product-automation-policy-design §8/§10). Nothing
runs `alembic upgrade` at deploy time: init_db's reconcile adds the nullable column.
"""
from alembic import op
import sqlalchemy as sa

revision = "0043_triage_auto_undone"
down_revision = "0042_product_automation_policy"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("triage_proposals", sa.Column("auto_undone", sa.Boolean(), nullable=True))


def downgrade() -> None:
    op.drop_column("triage_proposals", "auto_undone")
```

- [ ] **Step 4: Undo marks it**

In `undo_triage`, after loading `wi` and BEFORE `_restore_overlay(wi, snap)`:

```python
    if wi is not None and wi.classified_by == "auto" and prop.status == "accepted":
        # The decision being undone was the machine's: never let it re-decide.
        prop.auto_undone = True
```

and include `"autoUndone": prop.auto_undone` in the undo audit detail.

- [ ] **Step 5: The list widens; the serializer carries the marker**

`_triage_to_dict(prop, wi, feat_slug, project_slug)` gains two keys:

```python
        "classifiedBy": wi.classified_by if wi else None,
        "resolvedAt": prop.resolved_at.isoformat() if prop.resolved_at else None,
```

`list_triage`: replace `stmt = select(TriageProposal).where(TriageProposal.status == "pending")` with

```python
    # Pending, PLUS the loop's fresh auto-accepts for the undo window (spec §8):
    # the inbox shows them with an "auto-accepted · undo" chip, then they leave
    # like a human accept. A human accept never appears here — its undo lives in
    # the toast of the browser that made it.
    since = datetime.now(timezone.utc) - timedelta(seconds=TRIAGE_UNDO_WINDOW_S)
    auto_ids = select(WorkItem.id).where(WorkItem.classified_by == "auto")
    stmt = select(TriageProposal).where(or_(
        TriageProposal.status == "pending",
        and_(TriageProposal.status == "accepted",
             TriageProposal.resolved_at >= since,
             TriageProposal.work_item_id.in_(auto_ids))))
```

(import `or_`, `and_`, `timedelta` as needed; the existing `confidenceLevel` filter and ordering stay). Read the rest of `list_triage` — it joins `WorkItem`/`Feature`/project for the dict; the auto rows go through the same path.

- [ ] **Step 6: Gate**

`pytest server/tests/test_pm_api.py server/tests/test_schema_reconcile_coverage.py server/tests/test_postgres_compat.py -q`; ruff; mypy; ratchet. Report that no reconcile/postgres list needed an entry (nullable column, no new table).

- [ ] **Step 7: Commit**

`feat(pm): policy phase 2 task 2 — auto_undone; undo marks it; the inbox list carries fresh auto-accepts` (+ `Refs #1521`, trailer).

---

### Task 3: The loop step and its orchestrator seams

**Files:**
- Modify: `server/automations/pm_triage.py` (`AutoAcceptCandidateView`, seam types, `MAX_AUTO_ACCEPTS_PER_TICK`, constructor kwargs, `_auto_accept_pass`, call in `_run`)
- Modify: `server/sessions/orchestrator.py` (three seams; wiring at ~:1129-1141)
- Test: `server/tests/test_pm_triage_loop.py` (harness `_loop` at ~:34-60 gains the seams), `server/tests/test_pm_triage_seams.py` (the three orchestrator seams against SQLite)

**Interfaces:**
- Consumes (Task 1): `server.pm.triage_accept.apply_accept(db, proposal, *, decided_by, audit_identity, edits=None)`, `auto_accept_eligible(...)`; (Task 2): `TriageProposal.auto_undone`; (phase 1): `server.automations.product_policy.load_policies(db) -> dict[str, dict]`, `effective_policy(None)`.
- Produces in `server/automations/pm_triage.py`:

```python
MAX_AUTO_ACCEPTS_PER_TICK = 25   # per product per tick (spec §8) — a constant, not a knob


@dataclass(frozen=True)
class AutoAcceptCandidateView:
    """One pending proposal as the auto-accept step sees it (all I/O injected)."""
    proposal_id: str
    work_item_id: str
    product_slug: str | None       # None: the repo maps to no product → skip
    repo: str
    number: int
    confidence_level: str | None
    proposed_feature_id: str | None
    feature_state: str | None      # the target capability's lifecycle state (None if no row)
    auto_undone: bool | None


ReadPolicies = Callable[[], Awaitable[dict[str, dict]]]
ListAutoAcceptCandidates = Callable[[], Awaitable[list[AutoAcceptCandidateView]]]
# proposal_id -> audit detail ({"workItemId", "featureId", "confidence"}) or None
# when the proposal was no longer pending (a human got there first).
AutoAccept = Callable[[str], Awaitable["dict | None"]]
```

  `PmTriageLoop.__init__` gains keyword seams `read_policies: ReadPolicies | None = None`, `list_auto_accept_candidates: ListAutoAcceptCandidates | None = None`, `auto_accept: AutoAccept | None = None` (all optional so every existing construction keeps working; the step is skipped when any is `None`).

- Produces in the orchestrator: `_pm_triage_load_policies() -> dict[str, dict]` (raises on failure, like `_backlog_load_policies`), `_pm_triage_list_auto_accept_candidates() -> list[AutoAcceptCandidateView]`, `_pm_triage_auto_accept(proposal_id: str) -> dict | None`.

- [ ] **Step 1: Failing loop tests**

Extend `_loop` in `server/tests/test_pm_triage_loop.py`: new kwargs `policies=None, candidates=None`, recorder keys `rec["auto_accepted"] = []`, and seams:

```python
    async def read_policies():
        if policies is None:
            raise RuntimeError("policy read failed")
        return dict(policies)

    async def list_candidates():
        return list(candidates or [])

    async def auto_accept(proposal_id):
        rec["auto_accepted"].append(proposal_id)
        return {"workItemId": f"w-{proposal_id}", "featureId": "f1", "confidence": "high"}
```

passed as `read_policies=read_policies if policies is not None or candidates is not None else None`, `list_auto_accept_candidates=list_candidates`, `auto_accept=auto_accept` (wire all three whenever `candidates is not None`; when `policies is None` the read seam raises — that is the failure case).

Add a helper and tests:

```python
def _cand(pid, *, product="viberpit", level="high", feature="f1", state="live",
          undone=None, number=1):
    return AutoAcceptCandidateView(
        proposal_id=pid, work_item_id=f"w-{pid}", product_slug=product, repo="o/r",
        number=number, confidence_level=level, proposed_feature_id=feature,
        feature_state=state, auto_undone=undone)


def _policy(tier):
    from server.automations.product_policy import effective_policy
    return {**effective_policy(None), "triage_auto_accept": tier}


async def test_auto_accepts_qualifying_proposals_for_an_opted_in_product():
    ctx = _Ctx()
    loop, rec = _loop(ctx, policies={"viberpit": _policy("high")},
                      candidates=[_cand("p1", level="high"), _cand("p2", level="medium")])
    await loop.tick()
    assert rec["auto_accepted"] == ["p1"]
    kinds = [e[2].get("kind") for e in ctx.events]
    assert kinds.count("auto_accepted") == 1


async def test_product_with_auto_accept_off_is_untouched():
    (policies={"viberpit": _policy("off")}, one high candidate → rec["auto_accepted"] == [])


async def test_a_product_with_no_policy_is_off():
    (policies={}, one high candidate → nothing accepted)


async def test_no_fit_and_proposed_capability_and_undone_are_skipped():
    (tier medium; candidates: feature=None; state="proposed"; undone=True → nothing accepted)


async def test_per_product_cap_per_tick():
    (tier medium; 30 high candidates for viberpit + 1 for kindash (tier medium) →
     exactly MAX_AUTO_ACCEPTS_PER_TICK viberpit ids + the kindash id accepted;
     an info event kind "auto_accept_capped" with target "viberpit" emitted once)


async def test_a_failed_policy_read_skips_the_pass_but_the_tick_completes():
    (policies=None so read raises; candidates=[_cand("p1")] → rec["auto_accepted"] == [],
     the classify/other step still ran (assert the loop's usual stash happened:
     ctx.pm_triage_counts is set), and a "warn" event kind "auto_accept_skipped")


async def test_one_failing_accept_does_not_abort_the_others():
    (auto_accept seam raises for "p1", succeeds for "p2" → rec has "p2";
     a warn event kind "auto_accept_failed" with target "o/r#1")


async def test_a_proposal_a_human_resolved_first_is_not_reported_as_accepted():
    (auto_accept returns None for "p1" → no auto_accepted event, no error)
```

Write every parenthetical test out in full. Use the file's existing `_Ctx`/event-recorder fake (read the top of the file for its name).

- [ ] **Step 2: Run; expect TypeError on the new kwargs**

- [ ] **Step 3: Implement the step**

In `server/automations/pm_triage.py`, after `_classify_batch`:

```python
    async def _auto_accept_pass(self) -> None:
        """Spec §8: for every product whose policy sets ``triage_auto_accept``,
        accept the pending proposals at or above that tier — through the SAME
        ``apply_accept`` the inbox uses (injected as ``auto_accept``), audited by
        the seam, undoable for the same window. Runs every tick so a policy
        change catches up on the next one.

        Fail-safe by construction: no policies (a failed read) → no accepts this
        tick, with a warn; one proposal's failure is logged and skipped, never
        the pass; a proposal a human resolved between the list and the accept
        answers ``None`` from the seam and is silently dropped.
        """
        if (self._read_policies is None or self._list_candidates is None
                or self._auto_accept is None):
            return
        try:
            policies = await self._read_policies()
        except Exception as exc:
            log.warning("pm_triage.policy_read_failed", exc_info=True)
            await self._ctx.emit("warn", f"auto-accept skipped: policy read failed: {exc}",
                                 kind="auto_accept_skipped")
            return
        tiers = {slug: (p.get("triage_auto_accept") or "off") for slug, p in policies.items()}
        if all(t == "off" for t in tiers.values()):
            return
        accepted_by_product: dict[str, int] = {}
        capped: set[str] = set()
        for cand in await self._list_candidates():
            slug = cand.product_slug
            if not slug:
                continue
            tier = tiers.get(slug, "off")
            if not auto_accept_eligible(
                    tier=tier, confidence_level=cand.confidence_level,
                    proposed_feature_id=cand.proposed_feature_id,
                    feature_state=cand.feature_state, auto_undone=cand.auto_undone):
                continue
            if accepted_by_product.get(slug, 0) >= MAX_AUTO_ACCEPTS_PER_TICK:
                capped.add(slug)
                continue
            target = f"{cand.repo}#{cand.number}"
            try:
                detail = await self._auto_accept(cand.proposal_id)
            except Exception as exc:
                log.warning("pm_triage.auto_accept_failed", exc_info=True)
                await self._ctx.emit("warn", f"auto-accept failed for {target}: {exc}",
                                     kind="auto_accept_failed", target=target)
                continue
            if detail is None:
                continue          # a human resolved it first
            accepted_by_product[slug] = accepted_by_product.get(slug, 0) + 1
            await self._ctx.emit(
                "info", f"auto-accepted {target} ({cand.confidence_level})",
                kind="auto_accepted", target=target,
                detail={"proposalId": cand.proposal_id, "product": slug, **detail})
        for slug in sorted(capped):
            await self._ctx.emit(
                "info", f"auto-accept cap reached for {slug} this tick "
                        f"({MAX_AUTO_ACCEPTS_PER_TICK}); the rest wait",
                kind="auto_accept_capped", target=slug)
```

Import `auto_accept_eligible` from `server.pm.triage_accept` at module top (check for an import cycle: `server.pm.triage_accept` imports models/audit/classification only — fine). Call `await self._auto_accept_pass()` in `_run` immediately after the `if/elif` action dispatch and before `self._stash(...)`. Store the three seams in `__init__`.

- [ ] **Step 4: Run the loop tests; pass**

- [ ] **Step 5: Failing seam tests**

In `server/tests/test_pm_triage_seams.py` (use its `db_env` fixture, `_seed`, `_Emitter`; read `_add_cluster` for the WorkItem/TriageProposal seeding shape):

```python
async def test_list_auto_accept_candidates_carries_product_feature_state_and_flags(db_env):
    (seed: viberpit feature "sessions" state "live", feature "later" state "proposed";
     work items #1 (proposal → sessions, high), #2 (proposal → later, high),
     #3 (no_fit proposal), #4 (proposal → sessions, medium, auto_undone=True),
     plus a work item in a repo owned by nobody with a proposal.
     orch._pm_triage_list_auto_accept_candidates() → five views; assert per view:
     product_slug ("viberpit" / None for the orphan), feature_state ("live" /
     "proposed" / None), proposed_feature_id None for the no-fit, auto_undone True
     for #4; only status=="pending" proposals are listed (add one accepted → absent))


async def test_auto_accept_seam_accepts_audits_and_returns_detail(db_env):
    (seed one pending high proposal; detail = await orch._pm_triage_auto_accept(pid);
     detail == {"workItemId": w.id, "featureId": f.id, "confidence": "high"};
     DB: proposal.status "accepted", resolved_at set, undo_snapshot set;
     work item triage_state "classified", classified_by "auto", feature_id f.id;
     one AuditEvent type "pm.triage.auto_accept" identity "pm-triage" with
     detail {"proposalId", "workItemId", "featureId", "confidence"})


async def test_auto_accept_seam_answers_none_when_a_human_got_there_first(db_env):
    (seed a proposal already status "accepted" → await orch._pm_triage_auto_accept(pid) is None;
     no new audit row)


async def test_load_policies_seam_raises_on_a_broken_db(db_env, monkeypatch):
    (monkeypatch `server.automations.product_policy.load_policies` — through the
     module object, `from server.automations import product_policy as pp;
     monkeypatch.setattr(pp, "load_policies", boom)` — to raise; assert
     orch._pm_triage_load_policies() raises)


def test_pm_triage_loop_is_wired_with_the_three_seams(db_env):
    (build the orchestrator; assert loop._read_policies is orch._pm_triage_load_policies,
     loop._list_candidates is orch._pm_triage_list_auto_accept_candidates,
     loop._auto_accept is orch._pm_triage_auto_accept)
```

Write them out in full.

- [ ] **Step 6: Implement the seams** in `server/sessions/orchestrator.py`, next to `_pm_triage_list_pending`:

```python
    async def _pm_triage_load_policies(self) -> dict[str, dict]:
        """The triage loop's ``read_policies`` seam: slug → validated product
        policy. RAISES on failure (the loop skips the auto-accept pass for the
        tick with a warn) — ``{}`` would read as "no product opted in"."""
        from server.automations.product_policy import load_policies
        try:
            async with db_session() as db:
                return await load_policies(db)
        except Exception:
            log.warning("orchestrator._pm_triage_load_policies_failed", exc_info=True)
            raise

    async def _pm_triage_list_auto_accept_candidates(self) -> list:
        """Every pending proposal, with what the auto-accept gate needs:
        owning product (via ``owning_projects_by_repo`` — the ONE repo→project
        resolver), the target capability's lifecycle state, and the flags."""
        from sqlalchemy import select
        from server.automations.pm_triage import AutoAcceptCandidateView
        from server.db.models import Feature, TriageProposal, WorkItem
        from server.pm.repo_project import owning_projects_by_repo
        async with db_session() as db:
            rows = (await db.execute(
                select(TriageProposal, WorkItem)
                .join(WorkItem, WorkItem.id == TriageProposal.work_item_id)
                .where(TriageProposal.status == "pending"))).all()
            repos = {wi.repo for _, wi in rows if wi.repo}
            owners = await owning_projects_by_repo(db, repos) if repos else {}
            feature_ids = {p.proposed_feature_id for p, _ in rows if p.proposed_feature_id}
            states: dict[str, str | None] = {}
            if feature_ids:
                states = {fid: st for fid, st in (await db.execute(
                    select(Feature.id, Feature.state).where(
                        Feature.id.in_(feature_ids)))).all()}
            out = []
            for prop, wi in rows:
                owner = owners.get(wi.repo or "")
                out.append(AutoAcceptCandidateView(
                    proposal_id=prop.id, work_item_id=wi.id,
                    product_slug=owner.slug if owner else None,
                    repo=wi.repo or "", number=wi.number or 0,
                    confidence_level=prop.confidence_level,
                    proposed_feature_id=prop.proposed_feature_id,
                    feature_state=(states.get(prop.proposed_feature_id)
                                   if prop.proposed_feature_id else None),
                    auto_undone=prop.auto_undone))
            return out

    async def _pm_triage_auto_accept(self, proposal_id: str) -> "dict | None":
        """The loop's ``auto_accept`` seam: ONE transaction — re-read the
        proposal (a human may have resolved it since the list), accept through
        the shared ``apply_accept`` with ``decided_by="auto"``, audit
        ``pm.triage.auto_accept`` as ``pm-triage``. Returns the audit detail, or
        ``None`` when there was nothing left to accept."""
        from sqlalchemy import select
        from server.audit import record_audit
        from server.db.models import TriageProposal
        from server.pm.triage_accept import apply_accept
        async with db_session() as db:
            prop = (await db.execute(select(TriageProposal).where(
                TriageProposal.id == proposal_id))).scalar_one_or_none()
            if prop is None or prop.status != "pending":
                return None
            await apply_accept(db, prop, decided_by="auto", audit_identity="pm-triage")
            wi_id = prop.work_item_id
            detail = {"proposalId": prop.id, "workItemId": wi_id,
                      "featureId": prop.proposed_feature_id,
                      "confidence": prop.confidence_level}
            await record_audit(db=db, type="pm.triage.auto_accept",
                               identity="pm-triage", detail=detail)
            return {"workItemId": wi_id, "featureId": prop.proposed_feature_id,
                    "confidence": prop.confidence_level}
```

Wire into the `PmTriageLoop(...)` construction: `read_policies=self._pm_triage_load_policies, list_auto_accept_candidates=self._pm_triage_list_auto_accept_candidates, auto_accept=self._pm_triage_auto_accept`.

- [ ] **Step 7: Gate**

`pytest server/tests/test_pm_triage_loop.py server/tests/test_pm_triage_seams.py server/tests/test_pm_api.py server/tests/test_triage_accept.py -q`; ruff; mypy; ratchet.

- [ ] **Step 8: Commit**

`feat(pm): policy phase 2 task 3 — the triage loop auto-accepts per product policy through the shared accept` (+ `Refs #1521`, trailer).

---

### Task 4: The inbox chip and undo

**Files:**
- Modify: `web/src/api/triage.ts:12-34` (`classifiedBy`, `resolvedAt`)
- Modify: `web/src/features/roadmap/inbox/ProposalCard.tsx` (chip + Undo button)
- Modify: `web/src/features/roadmap/inbox/InboxScopeBody.tsx:133-147` and the bulk path (skip non-pending rows)
- Test: `web/src/features/roadmap/inbox/ProposalCard.test.tsx` (create if absent — check), `web/src/features/roadmap/inbox/InboxScopeBody.test.tsx`
- Modify: any fixture builders that construct `TriageProposal` (grep `confidenceLevel:` under `web/src` tests) to add the two new keys.

**Interfaces:**
- Consumes (Task 2): `GET /api/pm/triage` items with `status: "accepted"`, `classifiedBy: "auto"`, `resolvedAt`.
- Consumes: `useUndoProposal(projectSlug?)` from `web/src/api/hooks.ts:2188` — the ONE undo path; the card calls its `mutate(id)`.

- [ ] **Step 1: Types**

```ts
  /** The work item's `classified_by` once the proposal is resolved: `"human"`,
   *  `"auto"` (the PM Triage loop's auto-accept — spec §8), else null. */
  classifiedBy: string | null
  /** When the proposal was accepted/rejected; null while pending. */
  resolvedAt: string | null
```

- [ ] **Step 2: Failing card test**

`web/src/features/roadmap/inbox/ProposalCard.test.tsx` (mount inside `MemoryRouter` + `ViewportProvider` like `InboxScopeBody.test.tsx`; mock `@/api/hooks` with `useUndoProposal: () => ({ mutate: undo, isPending: false })`):

```tsx
it('shows the auto-accepted chip with an Undo control for a fresh auto-accept', () => {
  mount({ status: 'accepted', classifiedBy: 'auto', resolvedAt: new Date().toISOString() })
  expect(screen.getByText('auto-accepted')).toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: /undo auto-accept/i }))
  expect(undo).toHaveBeenCalledWith('p1')
})

it('shows no chip on a pending proposal', () => {
  mount({ status: 'pending', classifiedBy: null, resolvedAt: null })
  expect(screen.queryByText('auto-accepted')).toBeNull()
})
```

- [ ] **Step 3: Implement the chip**

In `ProposalCard`, compute `const autoAccepted = p.status === 'accepted' && p.classifiedBy === 'auto'`; `const undo = useUndoProposal()`. In `trailing`, when `autoAccepted`, render BEFORE the feature badge:

```tsx
<Badge tone="info">auto-accepted</Badge>
<button
  type="button"
  aria-label={`Undo auto-accept of ${name}`}
  disabled={undo.isPending}
  onClick={(e) => { e.stopPropagation(); undo.mutate(p.id) }}
  className="text-xs text-accent underline-offset-2 hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
>
  Undo
</button>
```

(Use the existing `Badge` tones — grep `tone=` values in the file; pick the one the sibling "proposed" state uses if `info` does not exist. No raw palette classes: `ccswitch/no-raw-palette` is on.) Add `sr-only` text "accepted automatically" beside the confidence sr text.

- [ ] **Step 4: Keyboard and bulk guards**

In `InboxScopeBody.tsx`: `case 'a'`/`'r'`/`'e'`: `if (!current || current.status !== 'pending') return`; `case 'x'`: same guard; `acceptSelected` filters to pending ids (`state.selected` may hold an auto row if the user ticked it — the tick button on an auto row should be hidden: pass `checked`/`onToggleSelect` only for pending rows, or render the leading tick disabled with `aria-disabled` when not pending). Add an `InboxScopeBody.test.tsx` case: with the cursor on an auto-accepted row, pressing `a` does not call `accept`.

- [ ] **Step 5: Gate**

Full web gate. `grep -rn "confidenceLevel:" web/src --include=*.test.tsx --include=*.ts` fixtures updated (typecheck will tell).

- [ ] **Step 6: Commit**

`feat(roadmap): policy phase 2 task 4 — inbox shows "auto-accepted · undo" for the undo window` (+ `Refs #1521`, trailer).

---

### Task 5: Docs

**Files:**
- Modify: `docs/product/features/product-management-layer.md` (the "Product automation policy" section: eight fields now; a new paragraph "Triage auto-accept" describing the step, the gate, the cap constant, the audit/event, `auto_undone`, the widened list and the chip; the "Not an `auto` mode" sentence in Non-goals amended: still no triage `auto` mode — auto-accept is a product policy under `propose`)
- Modify: `docs/product/features/autopilot-backlog-runner.md` only if it lists the policy fields (grep `triage_auto_accept`; update the "arrive with phases" sentence: phase 2 has landed)

Every sentence verified against the landed code (`server/pm/triage_accept.py`, `server/automations/pm_triage.py` `_auto_accept_pass`, `server/api/pm.py` `list_triage`/`undo_triage`, `ProposalCard.tsx`). Run `python3 scripts/brand_ratchet.py`. Commit `docs: policy phase 2 task 5 — triage auto-accept` (+ `Refs #1521`, trailer).

---

## Self-review

- Spec coverage: D6 (Task 1: one accept function, `decided_by`), D7 (Task 1 `classified_by="auto"`, Task 3 audit/event, Task 4 chip, Task 2 window), §4 field (Task 1), §8 step + cap + no-fit/proposed guards + `auto_undone` (Tasks 1–3), §9 chip (Task 4), §10 column (Task 2), §12 pure gate tests (Task 1) + loop tests (Task 3) + API accept routes unchanged (Task 1 gate) + web (Task 4), §13 vocabulary drift (Task 1 gate). Not in phase 2: everything under §6/§7/D8/D9 (phases 3–4).
- Types: `AutoAcceptCandidateView` fields match between Task 3's dataclass, the orchestrator seam and the loop tests; `apply_accept` keyword names (`decided_by`, `audit_identity`, `edits`) match across Tasks 1–3; `classifiedBy`/`resolvedAt` match between Task 2's serializer and Task 4's type.
- Open choice left to the implementer, stated: whether `_overlay_snapshot`/`_restore_overlay`/`_aware` move with `apply_accept` (they must, to avoid an import cycle; `pm.py` re-imports them).

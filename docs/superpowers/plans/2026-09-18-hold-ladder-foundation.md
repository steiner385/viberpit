# Hold Ladder — Foundation (series item 2) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the `needs_human` triage state with typed holds on the work item, record the real actor on every overlay write, and bound what a non-person may write — while the board and roadmap keep rendering the eight surviving holds exactly as before.

**Architecture:** Five nullable hold columns on `WorkItem` plus a pure `server/pm/holds.py` module (kinds, actor classes, permission rule, set/clear, legacy conversion) that the overlay route, the reject route, and a new `set_hold` session tool all call. `derive_status` and `issue_tier` read `hold_kind` instead of `triage_state == "needs_human"`. Migration 0049 is the reviewable record; a one-shot script applies the same conversion SQL on the live DB because nothing runs alembic at deploy.

**Tech Stack:** Python 3.12 / FastAPI / SQLAlchemy async (SQLite in tests, Postgres in prod) · alembic revision as record · in-process MCP session tools (`server/sessions/tools/`) · pytest.

**Spec:** `docs/superpowers/specs/2026-09-18-hold-ladder-design.md` — §3 (data model), §8 (provenance), and the minimal slice of §7 needed so the kept holds still render. §4–§6 and the rest of §7, §9 are later plans (series items 3–6).

## Global Constraints

- Constitution VI: one implementation per rule. Hold kinds, actor classes, and the permission rule live ONLY in `server/pm/holds.py`; routes and tools call it.
- DB clock rule: never `func.now()`; timestamps via `datetime.now(timezone.utc)` as the sibling routes in `server/api/pm.py` do (naive-vs-aware is handled by `_aware`).
- New columns are **nullable** so `init_db`'s reconcile ALTER-ADDs them on both dialects. No new table in this plan (the `decision_proposals` table ships with series item 5).
- Migration number: the plan says `0049`; **re-check `ls server/db/migrations/versions/ | sort | tail -1` just before committing** and renumber off master's newest (duplicate-number PRs merge clean and break master).
- Assistant policy is deny-by-default; every new mutating route gets a `False` row in `server/tests/test_assistant_policy.py`. Never add an allow entry.
- Audit every mutation with `record_audit(db=db, type=…, identity=…, detail={…})`.
- No absolute date literals judged against `datetime.now()` in tests.
- Brand: new env vars take the `VIBERPIT_` prefix (none needed here). Run `python scripts/brand_ratchet.py` before pushing.
- The four-step gate before push: `ruff check src/ server/`, `mypy server/ --ignore-missing-imports`, `pytest tests/`, `pytest server/tests/`. Web is untouched by this plan, so the web scripts need not run, but the server suite must.
- Test invocation (worktree has no venv): `env -i HOME=/tmp/vp-home PATH="$PATH" CCSWITCH_DEV=1 PYTHONPATH=$PWD/src /home/tony/GitHub/viberpit/.venv/bin/python -m pytest …` (a scrubbed env with an isolated HOME; the real HOME leaks a `settings.json` model pick).
- Commit messages via a file and `git commit -F`; trailer `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.
- Branch: `git fetch && git checkout -b hold-ladder-foundation origin/master`. Never off local `master`.

## File structure

| File | Responsibility |
|---|---|
| `server/db/models.py` | `HOLD_KINDS`; five `hold_*` columns on `WorkItem`; `WORK_ITEM_TRIAGE_STATES` loses `needs_human` |
| `server/pm/holds.py` (new) | kinds, actor classes, `actor_for_request`, `may_set`, `set_hold`, `clear_hold`, `HoldPermissionError`, `LEGACY_CONVERSION_SQL` |
| `server/db/migrations/versions/0049_work_item_holds.py` (new) | adds the columns; runs `LEGACY_CONVERSION_SQL` |
| `scripts/convert_legacy_needs_human.py` (new) | one-shot: runs `LEGACY_CONVERSION_SQL` on the live DB after deploy |
| `server/api/pm.py` | overlay PATCH: hold fields, permission check, actor as `classified_by`, hold audits; `_work_item_to_dict` hold fields; reject → owner-action hold; needs-home query on `hold_kind` (in `pm_tree.py`) |
| `server/pm/triage_accept.py` | snapshot/restore carry hold fields; legacy `needs_human` in an old snapshot maps to an owner-action hold |
| `server/pm/desired_state.py` | `ItemFacts.hold_kind`; `derive_status` reads it |
| `server/pm/reconcile_inputs.py` | passes `hold_kind` into `ItemFacts` |
| `server/pm/tree.py` | `issue_tier(..., hold_kind=)` |
| `server/api/pm_tree.py` | both `issue_tier` call sites pass `hold_kind`; needs-home query |
| `server/sessions/tools/registry.py`, `tools.py`, `instruction.py` | `set_hold` SIGNAL tool |
| `docs/superpowers/evals/session-tools/cases.jsonl` | one eval case for `set_hold` |
| `docs/product/features/session-tools.md`, `docs/product/features/product-management-layer.md` | document the tool and the columns |
| Tests | `server/tests/test_pm_holds.py` (new), `test_pm_api.py`, `test_desired_state.py`, `test_pm_tree.py`, `test_assistant_policy.py`, `test_session_tools.py`, `test_work_item_holds_migration.py` (new) |

---

### Task 1: Columns, vocabulary, and the hold module's pure core

**Files:**
- Modify: `server/db/models.py:1605-1606` (vocabulary), `:1755-1760` (WorkItem overlay block)
- Create: `server/pm/holds.py`
- Test: `server/tests/test_pm_holds.py`

**Interfaces:**
- Produces: `server.db.models.HOLD_KINDS: tuple[str, ...] == ("evidence", "stronger_model", "decision", "owner_action")`; `WorkItem.hold_kind / hold_reason / hold_by / hold_set_at / hold_fingerprint`.
- Produces: `server.pm.holds.AGENT_SETTABLE: frozenset[str]`; `actor_class(actor: str) -> Literal["person", "agent", "system"]`; `may_set(actor: str, kind: str) -> bool`; `class HoldPermissionError(ValueError)`; `set_hold(wi: WorkItem, *, kind: str, reason: str, actor: str, now: datetime) -> None`; `clear_hold(wi: WorkItem, *, actor: str) -> None`.
- Actor string grammar (spec §8): a person is anything not matching the machine prefixes; machine prefixes are `session:`, `assistant:`, `automation:`, `runner`, `migration`, and the literal `lan@local`.

- [ ] **Step 1: Write the failing tests**

Create `server/tests/test_pm_holds.py`:

```python
"""Pure hold rules (spec 2026-09-18 §3, §8): kinds, actor classes, permissions."""
from datetime import datetime, timezone

import pytest

from server.db.models import HOLD_KINDS, WORK_ITEM_TRIAGE_STATES, WorkItem
from server.pm.holds import (
    AGENT_SETTABLE, HoldPermissionError, actor_class, clear_hold, may_set, set_hold,
)

NOW = datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc)


def test_vocabularies():
    assert HOLD_KINDS == ("evidence", "stronger_model", "decision", "owner_action")
    assert "needs_human" not in WORK_ITEM_TRIAGE_STATES
    assert AGENT_SETTABLE == frozenset({"evidence", "stronger_model"})


@pytest.mark.parametrize("actor,cls", [
    ("tony@example.com", "person"),
    ("session:abc-123", "agent"),
    ("assistant:tony@example.com", "agent"),
    ("lan@local", "agent"),
    ("runner", "system"),
    ("automation:error-triage", "system"),
    ("migration", "system"),
])
def test_actor_class(actor, cls):
    assert actor_class(actor) == cls


def test_may_set_by_class():
    person, agent, system = "tony@example.com", "session:s1", "runner"
    for kind in HOLD_KINDS:
        assert may_set(person, kind)
    assert may_set(agent, "evidence") and may_set(agent, "stronger_model")
    assert not may_set(agent, "decision") and not may_set(agent, "owner_action")
    # system actors (runner, automations) are the ladder and the reject flows: any kind.
    for kind in HOLD_KINDS:
        assert may_set(system, kind)


def test_set_hold_records_everything_and_refuses_bad_input():
    wi = WorkItem(repo="o/r", number=1, kind="issue", body="hello")
    set_hold(wi, kind="evidence", reason="profile the lag", actor="session:s1", now=NOW)
    assert wi.hold_kind == "evidence"
    assert wi.hold_reason == "profile the lag"
    assert wi.hold_by == "session:s1"
    assert wi.hold_set_at == NOW
    assert wi.hold_fingerprint  # body fingerprint captured
    with pytest.raises(HoldPermissionError):
        set_hold(wi, kind="owner_action", reason="x", actor="session:s1", now=NOW)
    with pytest.raises(ValueError):
        set_hold(wi, kind="evidence", reason="  ", actor="tony@example.com", now=NOW)
    with pytest.raises(ValueError):
        set_hold(wi, kind="bogus", reason="r", actor="tony@example.com", now=NOW)


def test_clear_hold_limited_to_settable_kinds():
    wi = WorkItem(repo="o/r", number=1, kind="issue", body="hello")
    set_hold(wi, kind="owner_action", reason="owner only", actor="tony@example.com", now=NOW)
    with pytest.raises(HoldPermissionError):
        clear_hold(wi, actor="session:s1")
    clear_hold(wi, actor="tony@example.com")
    assert wi.hold_kind is None and wi.hold_reason is None and wi.hold_by is None
    assert wi.hold_set_at is None and wi.hold_fingerprint is None
    # clearing nothing is a no-op for anyone
    clear_hold(wi, actor="session:s1")
```

- [ ] **Step 2: Run them to verify they fail**

Run: `env -i HOME=/tmp/vp-home PATH="$PATH" CCSWITCH_DEV=1 PYTHONPATH=$PWD/src /home/tony/GitHub/viberpit/.venv/bin/python -m pytest server/tests/test_pm_holds.py -q`
Expected: FAIL with `ImportError: cannot import name 'HOLD_KINDS'`.

- [ ] **Step 3: Add the vocabulary and columns**

In `server/db/models.py`, replace the `WORK_ITEM_TRIAGE_STATES` block:

```python
# Approved-overlay triage states on WorkItem (proposals live in TriageProposal).
# ``needs_human`` was removed by spec 2026-09-18: "held" is now the hold columns
# below, not a triage state.
WORK_ITEM_TRIAGE_STATES: tuple[str, ...] = ("unclassified", "proposed", "classified")

# Why a work item is held (spec 2026-09-18 §3). Only ``owner_action`` and a
# pending ``decision`` render as "Needs you"; the other two exit on their own.
HOLD_KINDS: tuple[str, ...] = ("evidence", "stronger_model", "decision", "owner_action")
```

In `class WorkItem`, after `classified_at`:

```python
    # --- hold (spec 2026-09-18 §3): why the item is parked, by whom, since when ---
    hold_kind: Mapped[str | None] = mapped_column(String, nullable=True, index=True)  # HOLD_KINDS
    hold_reason: Mapped[str | None] = mapped_column(String, nullable=True)
    hold_by: Mapped[str | None] = mapped_column(String, nullable=True)  # actor string, holds.py
    hold_set_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    # body_fingerprint at hold time; a differing current fingerprint is the
    # "issue body edit" release signal for an owner_action hold (spec §7).
    hold_fingerprint: Mapped[str | None] = mapped_column(String, nullable=True)
```

- [ ] **Step 4: Create `server/pm/holds.py`**

```python
"""Holds on work items — the ONE implementation of spec 2026-09-18 §3/§8.

A hold says why an item is parked and who parked it. Kinds live in
``server.db.models.HOLD_KINDS``; this module owns who may write which kind and
the set/clear operations every route and tool must go through.
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from server.db.models import HOLD_KINDS, WorkItem
from server.pm.outcomes import body_fingerprint

#: Kinds a non-person (session, assistant, loopback caller) may set or clear.
AGENT_SETTABLE: frozenset[str] = frozenset({"evidence", "stronger_model"})

#: System actors: the runner and the automations. They ARE the ladder and the
#: reject flows, so they may write any kind — but they are never "human".
SYSTEM_ACTORS: frozenset[str] = frozenset({"runner", "migration"})
SYSTEM_PREFIXES: tuple[str, ...] = ("automation:",)
AGENT_PREFIXES: tuple[str, ...] = ("session:", "assistant:")
#: The synthetic identity handed to an unattributed loopback caller
#: (server/auth/identity.py:OPEN_LAN_IDENTITY). Spelled here as a literal so this
#: module stays free of the auth package.
OPEN_LAN_ACTOR = "lan@local"

ActorClass = Literal["person", "agent", "system"]


class HoldPermissionError(ValueError):
    """The actor may not set or clear this hold kind."""


def actor_class(actor: str) -> ActorClass:
    if actor in SYSTEM_ACTORS or actor.startswith(SYSTEM_PREFIXES):
        return "system"
    if actor == OPEN_LAN_ACTOR or actor.startswith(AGENT_PREFIXES):
        return "agent"
    return "person"


def may_set(actor: str, kind: str) -> bool:
    if kind not in HOLD_KINDS:
        return False
    return actor_class(actor) != "agent" or kind in AGENT_SETTABLE


def set_hold(wi: WorkItem, *, kind: str, reason: str, actor: str, now: datetime) -> None:
    if kind not in HOLD_KINDS:
        raise ValueError(f"hold_kind must be one of {HOLD_KINDS}")
    if not (reason or "").strip():
        raise ValueError("hold_reason is required")
    if not may_set(actor, kind):
        raise HoldPermissionError(f"{actor_class(actor)} actors may not set {kind!r}")
    wi.hold_kind = kind
    wi.hold_reason = reason.strip()
    wi.hold_by = actor
    wi.hold_set_at = now
    wi.hold_fingerprint = body_fingerprint(wi.title, wi.body)


def clear_hold(wi: WorkItem, *, actor: str) -> None:
    if wi.hold_kind is None:
        return
    if not may_set(actor, wi.hold_kind):
        raise HoldPermissionError(
            f"{actor_class(actor)} actors may not clear a {wi.hold_kind!r} hold")
    wi.hold_kind = None
    wi.hold_reason = None
    wi.hold_by = None
    wi.hold_set_at = None
    wi.hold_fingerprint = None
```

`body_fingerprint(title: str | None, body: str | None) -> str` is THE issue-identity function (`server/pm/outcomes.py:34`); do not add a second one. `db_session()` commits on exit (`server/db/__init__.py:316`), so Task 5's handler needs no explicit commit.

- [ ] **Step 5: Run the tests to verify they pass**

Run the same command as Step 2. Expected: all PASS.

- [ ] **Step 6: Run the schema guard tests**

Run: `… -m pytest server/tests/test_schema_reconcile_coverage.py server/tests/test_postgres_compat.py -q`
Expected: PASS (all five columns are nullable; the Postgres suite skips without `CCSWITCH_TEST_PG_URL` — that is fine, no new table means no `_REQUIRED_EXTRAS` entry).

- [ ] **Step 7: Commit**

```bash
git add server/db/models.py server/pm/holds.py server/tests/test_pm_holds.py
printf 'feat(pm): typed hold columns on work items and the pure hold rules\n\nSpec 2026-09-18 §3/§8. needs_human leaves the triage vocabulary.\n\nCo-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>\n' > /tmp/c1.txt
git commit -F /tmp/c1.txt
```

---

### Task 2: Migration 0049, the legacy conversion, and the one-shot script

**Files:**
- Modify: `server/pm/holds.py` (append `LEGACY_CONVERSION_SQL`)
- Create: `server/db/migrations/versions/0049_work_item_holds.py`
- Create: `scripts/convert_legacy_needs_human.py`
- Test: `server/tests/test_work_item_holds_migration.py`

**Interfaces:**
- Produces: `server.pm.holds.LEGACY_CONVERSION_SQL: tuple[str, ...]` — dialect-neutral `UPDATE` statements, in order. Consumed by the migration's `upgrade()` and by the script.
- Conversion rule (spec §3): every row with `triage_state = 'needs_human'` becomes `hold_kind = 'owner_action'`, `hold_by = 'migration'`, `hold_reason = 'pre-ladder needs_human'`, `hold_set_at = classified_at`; then `triage_state = 'classified'` where all four RICE inputs are non-null, else `'unclassified'`.

- [ ] **Step 1: Write the failing test**

Create `server/tests/test_work_item_holds_migration.py`:

```python
"""Migration 0049: hold columns exist and legacy needs_human rows convert (spec §3)."""
from __future__ import annotations

import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text

MIGRATION = Path("server/db/migrations/versions/0049_work_item_holds.py")


def _module():
    spec = importlib.util.spec_from_file_location("work_item_holds_migration", MIGRATION)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def test_upgrade_adds_columns_and_converts_legacy_rows(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/m.db")
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE work_items (id TEXT PRIMARY KEY, repo TEXT, number INTEGER, "
            "kind TEXT, body TEXT, triage_state TEXT, classified_by TEXT, classified_at DATETIME, "
            "reach INTEGER, impact INTEGER, effort INTEGER, confidence INTEGER)"))
        conn.execute(text(
            "INSERT INTO work_items (id, repo, number, kind, triage_state, classified_by, classified_at, "
            "reach, impact, effort, confidence) VALUES "
            "('scored', 'o/r', 1, 'issue', 'needs_human', 'human', '2026-09-12 18:43:51', 2, 2, 2, 2), "
            "('unscored', 'o/r', 2, 'issue', 'needs_human', 'human', '2026-09-12 18:43:51', NULL, 2, 2, 2), "
            "('untouched', 'o/r', 3, 'issue', 'classified', 'auto', '2026-09-12 18:43:51', 1, 1, 1, 1)"))
    mod = _module()
    with engine.begin() as conn:
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            mod.upgrade()
    cols = {c["name"] for c in inspect(engine).get_columns("work_items")}
    assert {"hold_kind", "hold_reason", "hold_by", "hold_set_at", "hold_fingerprint"} <= cols
    with engine.connect() as conn:
        rows = {r[0]: r for r in conn.execute(text(
            "SELECT id, triage_state, hold_kind, hold_by, hold_reason, hold_set_at FROM work_items"))}
    assert rows["scored"][1:5] == ("classified", "owner_action", "migration", "pre-ladder needs_human")
    assert rows["scored"][5] is not None
    assert rows["unscored"][1:3] == ("unclassified", "owner_action")
    assert rows["untouched"][1:3] == ("classified", None)
```

- [ ] **Step 2: Run it to verify it fails**

Run: `… -m pytest server/tests/test_work_item_holds_migration.py -q`
Expected: FAIL (`AssertionError` from `spec.loader` / file not found).

- [ ] **Step 3: Append the conversion SQL to `server/pm/holds.py`**

```python
#: Dialect-neutral conversion of pre-ladder ``needs_human`` rows (spec §3). Run by
#: migration 0049's ``upgrade()`` AND by ``scripts/convert_legacy_needs_human.py``,
#: because nothing runs ``alembic upgrade`` at deploy time — the script is what
#: actually converts the live rows. Order matters: the hold is written from the row
#: while ``triage_state`` still says needs_human, then the state is rewritten.
LEGACY_CONVERSION_SQL: tuple[str, ...] = (
    "UPDATE work_items SET hold_kind = 'owner_action', hold_by = 'migration', "
    "hold_reason = 'pre-ladder needs_human', hold_set_at = classified_at "
    "WHERE triage_state = 'needs_human' AND hold_kind IS NULL",
    "UPDATE work_items SET triage_state = 'classified' WHERE triage_state = 'needs_human' "
    "AND reach IS NOT NULL AND impact IS NOT NULL AND effort IS NOT NULL AND confidence IS NOT NULL",
    "UPDATE work_items SET triage_state = 'unclassified' WHERE triage_state = 'needs_human'",
)
```

- [ ] **Step 4: Create the migration**

`server/db/migrations/versions/0049_work_item_holds.py`:

```python
"""Typed holds on work items (spec 2026-09-18 §3).

Revision ID: 0049_work_item_holds
Revises: 0048_artifact_auto_merge_armed

Adds ``work_items.hold_kind / hold_reason / hold_by / hold_set_at /
hold_fingerprint`` (all nullable) and converts the pre-ladder
``triage_state = 'needs_human'`` rows into owner_action holds by ``migration``.

Nothing runs ``alembic upgrade`` at deploy time: ``init_db``'s reconcile ALTER-ADDs
the nullable columns, and ``scripts/convert_legacy_needs_human.py`` runs the same
conversion statements (``server.pm.holds.LEGACY_CONVERSION_SQL``) once on the live
DB. This revision is the reviewable record of both.
"""
from alembic import op
import sqlalchemy as sa

from server.pm.holds import LEGACY_CONVERSION_SQL

revision = "0049_work_item_holds"
down_revision = "0048_artifact_auto_merge_armed"
branch_labels = None
depends_on = None

_COLUMNS = (
    sa.Column("hold_kind", sa.String(), nullable=True),
    sa.Column("hold_reason", sa.String(), nullable=True),
    sa.Column("hold_by", sa.String(), nullable=True),
    sa.Column("hold_set_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("hold_fingerprint", sa.String(), nullable=True),
)


def _existing() -> set[str]:
    bind = op.get_bind()
    return {c["name"] for c in sa.inspect(bind).get_columns("work_items")}


def upgrade() -> None:
    have = _existing()
    for col in _COLUMNS:
        if col.name not in have:
            op.add_column("work_items", col)
    if "hold_kind" not in have:
        op.create_index("ix_work_items_hold_kind", "work_items", ["hold_kind"])
    for stmt in LEGACY_CONVERSION_SQL:
        op.execute(sa.text(stmt))


def downgrade() -> None:
    op.drop_index("ix_work_items_hold_kind", table_name="work_items")
    for col in reversed(_COLUMNS):
        op.drop_column("work_items", col.name)
```

Check that the alembic env can import `server.pm.holds` (it imports `server.db.models`, which the env already imports). If `holds.py` importing `server.pm.outcomes` drags the ORM engine in at import time, move `body_fingerprint` usage behind a function-local import — but keep ONE fingerprint implementation.

- [ ] **Step 5: Create the one-shot script**

`scripts/convert_legacy_needs_human.py`:

```python
"""One-shot: convert pre-ladder ``needs_human`` rows on the LIVE DB (spec 2026-09-18 §3).

Nothing runs ``alembic upgrade`` at deploy, so after the hold columns are live
(``init_db`` reconcile adds them) run this once per host that owns the DB:

    CCSWITCH_DB_URL=… python scripts/convert_legacy_needs_human.py --dry-run
    CCSWITCH_DB_URL=… python scripts/convert_legacy_needs_human.py --apply

Runs exactly ``server.pm.holds.LEGACY_CONVERSION_SQL``; prints the affected count first.
"""
from __future__ import annotations

import asyncio
import os
import sys

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from server.pm.holds import LEGACY_CONVERSION_SQL


async def main(apply: bool) -> int:
    url = os.environ.get("CCSWITCH_DB_URL") or os.environ.get("VIBERPIT_DB_URL")
    if not url:
        print("set CCSWITCH_DB_URL", file=sys.stderr)
        return 2
    engine = create_async_engine(url)
    try:
        async with engine.begin() as conn:
            n = await conn.scalar(text(
                "SELECT count(*) FROM work_items WHERE triage_state = 'needs_human'"))
            print(f"legacy needs_human rows: {n}")
            if not apply:
                print("dry run; pass --apply to convert")
                return 0
            for stmt in LEGACY_CONVERSION_SQL:
                await conn.execute(text(stmt))
            left = await conn.scalar(text(
                "SELECT count(*) FROM work_items WHERE triage_state = 'needs_human'"))
            print(f"converted; remaining legacy rows: {left}")
            return 0 if left == 0 else 1
    finally:
        await engine.dispose()


if __name__ == "__main__":
    sys.exit(asyncio.run(main("--apply" in sys.argv)))
```

- [ ] **Step 6: Run the migration test and the chain test**

Run: `… -m pytest server/tests/test_work_item_holds_migration.py server/tests/test_pending_model_reason_column.py::test_migration_chain_is_linear_with_exactly_one_head -q`
Expected: PASS. If the chain test reports two heads, master gained a `0049` while you worked: renumber to `0050`, update `down_revision`, rename the file, update the test's `MIGRATION` path.

- [ ] **Step 7: Commit**

```bash
git add server/pm/holds.py server/db/migrations/versions/0049_work_item_holds.py scripts/convert_legacy_needs_human.py server/tests/test_work_item_holds_migration.py
printf 'feat(db): migration 0049 work_item holds + legacy needs_human conversion\n\nCo-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>\n' > /tmp/c2.txt
git commit -F /tmp/c2.txt
```

---

### Task 3: Actor resolution and the overlay route

**Files:**
- Modify: `server/pm/holds.py` (append `actor_for_request`)
- Modify: `server/api/pm.py:1432-1450` (`_work_item_to_dict`), `:1453-1460` (`OverlayPatchBody`), `:1462-1515` (`patch_overlay`), `:1292-1310` (`reject_triage`)
- Modify: `server/pm/triage_accept.py:100-119` (snapshot/restore)
- Modify: `server/api/pm_tree.py:175-178` (needs-home query)
- Test: `server/tests/test_pm_api.py`, `server/tests/test_pm_holds.py`

**Interfaces:**
- Produces: `holds.actor_for_request(request: Request) -> str` — `assistant:<identity>` when `request.state.via == "assistant"`, else `request.state.identity` (which is `lan@local` for an unattributed loopback caller and a Tailscale login for a person).
- Overlay body gains `holdKind: str | None` (absent = no change), `clearHold: bool = False`, `holdReason: str | None`. Response gains `holdKind`, `holdReason`, `holdBy`, `holdSetAt`.
- Audit types: `pm.hold.set` `{workItemId, kind, reason, actorClass}`, `pm.hold.release` `{workItemId, kind, actorClass}`; the existing `pm.workitem.overlay` detail gains `fields: [names changed]`.
- `classified_by` is now the actor string (person login, `assistant:…`, `lan@local`) — `DECIDED_BY_HUMAN` stays for the inbox accept path, which really is a person.

- [ ] **Step 1: Write the failing tests**

Append to `server/tests/test_pm_holds.py`:

```python
def test_actor_for_request_reads_via_and_identity():
    from types import SimpleNamespace
    from server.pm.holds import actor_for_request
    person = SimpleNamespace(state=SimpleNamespace(identity="tony@example.com", via=None))
    assert actor_for_request(person) == "tony@example.com"
    drawer = SimpleNamespace(state=SimpleNamespace(identity="tony@example.com", via="assistant"))
    assert actor_for_request(drawer) == "assistant:tony@example.com"
    lan = SimpleNamespace(state=SimpleNamespace(identity="lan@local", via=None))
    assert actor_for_request(lan) == "lan@local"
```

Append to `server/tests/test_pm_api.py` (after `test_overlay_patch_manual_classify`):

```python
LAN_OPEN = {"CCSWITCH_ALLOW_UNAUTHENTICATED": "1"}


def test_overlay_hold_by_person_any_kind_with_audit():
    with _client() as c:
        _register(c, "Viberpit", repo="o/r")
        _pid, wid = _seed_one_pending(c, feature_slug="sessions")
        r = c.patch(f"/api/pm/work-items/{wid}/overlay",
                    json={"holdKind": "owner_action", "holdReason": "needs the owner's keyring"},
                    headers=IDENTITY_HEADERS)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["holdKind"] == "owner_action"
        assert body["holdReason"] == "needs the owner's keyring"
        assert body["holdBy"] == "testuser@example.com"
        assert body["holdSetAt"] is not None
        assert body["classifiedBy"] == "testuser@example.com"
        # reason is mandatory
        bad = c.patch(f"/api/pm/work-items/{wid}/overlay",
                      json={"holdKind": "evidence"}, headers=IDENTITY_HEADERS)
        assert bad.status_code == 422
        assert bad.json()["detail"]["error"] == "hold_reason_required"
        # clear
        r = c.patch(f"/api/pm/work-items/{wid}/overlay", json={"clearHold": True},
                    headers=IDENTITY_HEADERS)
        assert r.status_code == 200 and r.json()["holdKind"] is None
        from server.db import db_session
        from sqlalchemy import select
        from server.db.models import AuditEvent

        async def _types():
            async with db_session() as db:
                return [(e.type, e.detail) for e in (await db.execute(
                    select(AuditEvent).where(AuditEvent.type.like("pm.hold.%"))
                    .order_by(AuditEvent.at))).scalars().all()]
        events = c.portal.call(_types)
        assert [t for t, _ in events] == ["pm.hold.set", "pm.hold.release"]
        assert events[0][1]["kind"] == "owner_action" and events[0][1]["actorClass"] == "person"


def test_overlay_hold_by_loopback_caller_is_limited_and_never_human():
    with patch.dict(os.environ, LAN_OPEN), _client() as c:
        _register(c, "Viberpit", repo="o/r")
        _pid, wid = _seed_one_pending(c, feature_slug="sessions")
        # no identity header → lan@local (LAN-open mode); an agent-class actor
        r = c.patch(f"/api/pm/work-items/{wid}/overlay",
                    json={"holdKind": "evidence", "holdReason": "correlate with #2"})
        assert r.status_code == 200, r.text
        assert r.json()["holdBy"] == "lan@local"
        assert r.json()["classifiedBy"] == "lan@local"
        refused = c.patch(f"/api/pm/work-items/{wid}/overlay",
                          json={"holdKind": "owner_action", "holdReason": "park it"})
        assert refused.status_code == 422
        assert refused.json()["detail"]["error"] == "hold_not_permitted"
        # a person parks it; the loopback caller cannot release that
        r = c.patch(f"/api/pm/work-items/{wid}/overlay",
                    json={"holdKind": "owner_action", "holdReason": "owner only"},
                    headers=IDENTITY_HEADERS)
        assert r.status_code == 200
        refused = c.patch(f"/api/pm/work-items/{wid}/overlay", json={"clearHold": True})
        assert refused.status_code == 422
        assert refused.json()["detail"]["error"] == "hold_not_permitted"
        # a legacy value is no longer accepted anywhere
        legacy = c.patch(f"/api/pm/work-items/{wid}/overlay",
                         json={"triageState": "needs_human"}, headers=IDENTITY_HEADERS)
        assert legacy.status_code == 422
```

Update the reject assertion in `test_triage_accept_and_reject_flow` (around line 435): the item no longer goes to `needs_human`; it keeps its triage state and gains an owner-action hold:

```python
        # reject parks the item as an owner-action hold by the rejecting person
        r = c.post(f"/api/pm/triage/{reject_id}/reject", headers=IDENTITY_HEADERS)
        assert r.status_code == 200
        wi = c.get(f"/api/pm/work-items/{r.json()['workItemId']}", headers=IDENTITY_HEADERS)
```

Read the surrounding lines first: if the route response or a later assertion names `"needs_human"`, replace it with `holdKind == "owner_action"` and `holdBy == "testuser@example.com"` read through the overlay read-through (`c.patch(..., json={})`), which returns the item dict. If no `GET /api/pm/work-items/{id}` route exists, use the empty PATCH read-through instead of inventing a GET.

- [ ] **Step 2: Run to verify they fail**

Run: `… -m pytest server/tests/test_pm_holds.py server/tests/test_pm_api.py -q -k "hold or reject"`
Expected: FAIL (`actor_for_request` missing; 200 with no `holdKind` key).

- [ ] **Step 3: Add `actor_for_request` to `server/pm/holds.py`**

```python
def actor_for_request(request: object) -> str:
    """The actor string for an HTTP caller (spec §8): the drawer's bound token is
    ``assistant:<identity>``; otherwise the verified identity the middleware attached,
    which is ``lan@local`` for an unattributed loopback caller and a Tailscale login
    for a person. Takes any object with ``.state.identity`` / ``.state.via`` so the
    pure test needs no FastAPI Request."""
    state = getattr(request, "state", None)
    identity = str(getattr(state, "identity", "") or OPEN_LAN_ACTOR)
    if getattr(state, "via", None) == "assistant":
        return f"assistant:{identity}"
    return identity
```

- [ ] **Step 4: Rewrite the overlay route in `server/api/pm.py`**

Replace `_work_item_to_dict`'s return with the hold fields added:

```python
    at = _aware(w.classified_at)
    held_at = _aware(w.hold_set_at)
    return {
        "id": w.id, "repo": w.repo, "number": w.number, "kind": w.kind,
        "title": w.title, "state": w.state,
        "featureId": w.feature_id,
        "reach": w.reach, "impact": w.impact, "effort": w.effort,
        "confidence": w.confidence,
        "triageState": w.triage_state,
        "classifiedBy": w.classified_by,
        "classifiedAt": at.isoformat() if at else None,
        "holdKind": w.hold_kind, "holdReason": w.hold_reason, "holdBy": w.hold_by,
        "holdSetAt": held_at.isoformat() if held_at else None,
        "url": w.url,
    }
```

`OverlayPatchBody`:

```python
class OverlayPatchBody(BaseModel):
    featureId: str | None = None
    reach: int | None = None
    impact: int | None = None
    effort: int | None = None
    confidence: int | None = None
    triageState: str | None = None
    # Holds (spec 2026-09-18 §3/§8). ``holdKind`` absent = no change; ``clearHold``
    # is the explicit release, so "absent" and "null" cannot be confused.
    holdKind: str | None = None
    holdReason: str | None = None
    clearHold: bool = False
```

`patch_overlay` — change the signature to take the request and replace the body from the triage-state check to the audit:

```python
@router.patch("/work-items/{item_id}/overlay")
async def patch_overlay(
    item_id: str,
    body: OverlayPatchBody,
    request: Request,
    identity: str = Depends(current_identity),
    db=Depends(get_db),
) -> dict:
    """Manual classify / inline score edit / hold on the ViberPit-owned overlay.
    All fields optional. ``classified_by`` records the ACTOR (spec 2026-09-18 §8):
    a person's login, ``assistant:<login>``, or ``lan@local`` — never a blanket
    "human". A body that sets NO field is a read-through no-op."""
    from server.pm.holds import (HoldPermissionError, actor_class, actor_for_request,
                                 clear_hold, set_hold)
    if body.triageState is not None and body.triageState not in WORK_ITEM_TRIAGE_STATES:
        raise HTTPException(
            status_code=422,
            detail={"error": "bad_triage_state",
                    "message": f"triageState must be one of {WORK_ITEM_TRIAGE_STATES}"})
    if body.holdKind is not None and body.clearHold:
        raise HTTPException(status_code=422, detail={
            "error": "bad_hold", "message": "holdKind and clearHold are exclusive"})
    if body.holdKind is not None and not (body.holdReason or "").strip():
        raise HTTPException(status_code=422, detail={
            "error": "hold_reason_required", "message": "holdReason is required with holdKind"})
    for attr in RICE_FIELDS:
        msg = validate_rice(attr, getattr(body, attr))
        if msg:
            raise HTTPException(status_code=422, detail={
                "error": "bad_rice_value", "message": msg})
    wi = (await db.execute(
        select(WorkItem).where(WorkItem.id == item_id))).scalar_one_or_none()
    if wi is None:
        raise HTTPException(status_code=404, detail={"error": "not_found"})
    changed = [k for k, v in body.model_dump(exclude_none=True).items() if v is not False]
    if not changed:
        return _work_item_to_dict(wi)
    actor = actor_for_request(request)
    now = datetime.now(timezone.utc)
    if body.featureId is not None:
        feat = (await db.execute(
            select(Feature).where(Feature.id == body.featureId))).scalar_one_or_none()
        if feat is None:
            raise HTTPException(status_code=404,
                                detail={"error": "feature_not_found"})
        wi.feature_id = body.featureId
    for attr in RICE_FIELDS:
        val = getattr(body, attr)
        if val is not None:
            setattr(wi, attr, val)
    if body.triageState is not None:
        wi.triage_state = body.triageState
    try:
        if body.holdKind is not None:
            set_hold(wi, kind=body.holdKind, reason=body.holdReason or "", actor=actor, now=now)
            await record_audit(db=db, type="pm.hold.set", identity=identity, detail={
                "workItemId": item_id, "kind": body.holdKind, "reason": wi.hold_reason,
                "actorClass": actor_class(actor)})
        elif body.clearHold:
            released = wi.hold_kind
            clear_hold(wi, actor=actor)
            if released is not None:
                await record_audit(db=db, type="pm.hold.release", identity=identity, detail={
                    "workItemId": item_id, "kind": released, "actorClass": actor_class(actor)})
    except HoldPermissionError as e:
        raise HTTPException(status_code=422, detail={
            "error": "hold_not_permitted", "message": str(e)}) from e
    except ValueError as e:
        raise HTTPException(status_code=422, detail={"error": "bad_hold", "message": str(e)}) from e
    wi.classified_by = actor
    wi.classified_at = now
    await db.flush()
    await record_audit(db=db, type="pm.workitem.overlay", identity=identity,
                       detail={"workItemId": item_id, "fields": changed,
                               "actorClass": actor_class(actor)})
    return _work_item_to_dict(wi)
```

`reject_triage` — replace `wi.triage_state = "needs_human"`:

```python
    if wi is not None:
        prop.undo_snapshot = _overlay_snapshot(wi)
        # Spec 2026-09-18 §3: a rejection is a person's hold, not a triage state.
        from server.pm.holds import set_hold
        set_hold(wi, kind="owner_action", reason="classification proposal rejected",
                 actor=identity, now=now)
```

and add, next to the existing reject audit, `await record_audit(db=db, type="pm.hold.set", identity=identity, detail={"workItemId": wi.id, "kind": "owner_action", "reason": "classification proposal rejected", "actorClass": "person"})` inside the `if wi is not None:` block.

`server/api/pm_tree.py:175-178` needs-home query — replace the `WorkItem.triage_state == "needs_human"` predicate with `WorkItem.hold_kind == "owner_action"` (same `feature_id.is_(None)` companion).

- [ ] **Step 5: Snapshot and restore in `server/pm/triage_accept.py`**

Add the five hold fields to `_overlay_snapshot` (`"holdKind": wi.hold_kind, "holdReason": wi.hold_reason, "holdBy": wi.hold_by, "holdSetAt": <iso or None via _aware>, "holdFingerprint": wi.hold_fingerprint`) and restore them in `_restore_overlay`. In `_restore_overlay`, map the legacy value:

```python
    state = snap.get("triageState")
    if state == "needs_human":
        # A pre-ladder snapshot: the removed state becomes the hold it meant.
        wi.triage_state = "unclassified"
        wi.hold_kind, wi.hold_by = "owner_action", "migration"
        wi.hold_reason, wi.hold_fingerprint = "restored pre-ladder needs_human", None
        wi.hold_set_at = None
    else:
        wi.triage_state = state
        wi.hold_kind = snap.get("holdKind")
        wi.hold_reason = snap.get("holdReason")
        wi.hold_by = snap.get("holdBy")
        held = snap.get("holdSetAt")
        wi.hold_set_at = _aware(datetime.fromisoformat(held)) if held else None
        wi.hold_fingerprint = snap.get("holdFingerprint")
```

- [ ] **Step 6: Run the tests**

Run: `… -m pytest server/tests/test_pm_holds.py server/tests/test_pm_api.py server/tests/test_reassign_assistant_overlay.py -q`
Expected: PASS. `test_reassign_assistant_overlay.py` exercises the assistant's overlay PATCH; if it asserts `classifiedBy == "human"`, update it to `"assistant:<identity>"` — that is the spec's intended change, not a regression.

- [ ] **Step 7: Commit**

```bash
git add server/pm/holds.py server/api/pm.py server/api/pm_tree.py server/pm/triage_accept.py server/tests/test_pm_holds.py server/tests/test_pm_api.py server/tests/test_reassign_assistant_overlay.py
printf 'feat(pm): overlay holds with honest actor provenance and per-class permissions\n\nSpec 2026-09-18 §8. classified_by is the actor, never a blanket human.\n\nCo-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>\n' > /tmp/c3.txt
git commit -F /tmp/c3.txt
```

---

### Task 4: Derivation reads the hold, not the triage state

**Files:**
- Modify: `server/pm/desired_state.py:29-49` (`ItemFacts`), `:66-82` (`derive_status`)
- Modify: `server/pm/reconcile_inputs.py:110-122`
- Modify: `server/pm/tree.py:115-131` (`issue_tier`)
- Modify: `server/api/pm_tree.py:141`, `:308`
- Test: `server/tests/test_desired_state.py`, `server/tests/test_pm_tree.py`

**Interfaces:**
- `ItemFacts` gains `hold_kind: str | None = None` (defaulted last, so every positional construction keeps working).
- `derive_status`: the needs-human branch becomes `if f.needs_human_at is not None or f.hold_kind in ("owner_action", "decision")` — `needs_human_at` (the runner's `gave_up` row) stays until series item 3 replaces it.
- `issue_tier(state, triage_state, has_open_blocker, session_status, board_status=None, *, reason=None, hold_kind=None)`: needs-you iff `hold_kind in ("owner_action", "decision")` or the existing session/board conditions; `triage_state == "needs_human"` is no longer consulted.

- [ ] **Step 1: Update the tests**

`server/tests/test_desired_state.py`: in `_f`, add `hold_kind=None` to `base`. Replace `test_status_needs_human_blocked_ready_backlog`:

```python
def test_status_hold_blocked_ready_backlog():
    # An owner_action or pending decision hold is the board's "Needs human"; the
    # hold's own timestamp is the decision (spec 2026-09-18 §7).
    assert derive_status(_f(hold_kind="owner_action", classified_at=T1)) == Decision(STATUS_NEEDS_HUMAN, T1)
    assert derive_status(_f(hold_kind="decision", classified_at=T1)) == Decision(STATUS_NEEDS_HUMAN, T1)
    # evidence and stronger_model holds never park the card
    assert derive_status(_f(hold_kind="evidence", triage_state="classified", classified_at=T1)) == Decision(STATUS_READY, T1)
    assert derive_status(_f(hold_kind="stronger_model")) == Decision(STATUS_BACKLOG, None)
    # the removed triage value is inert even if a stale row still carries it
    assert derive_status(_f(triage_state="needs_human", classified_at=T1)) == Decision(STATUS_BACKLOG, None)
    assert derive_status(_f(open_blocker_refs=("o/r#2",), blockers_at=T2)) == Decision(STATUS_BLOCKED, T2)
    assert derive_status(_f(triage_state="classified", classified_at=T1)) == Decision(STATUS_READY, T1)
    assert derive_status(_f()) == Decision(STATUS_BACKLOG, None)
```

`server/tests/test_pm_tree.py`: replace `assert issue_tier("open", "needs_human", False, None) == "needs-you"` with:

```python
    assert issue_tier("open", "classified", False, None, hold_kind="owner_action") == "needs-you"
    assert issue_tier("open", "classified", False, None, hold_kind="decision") == "needs-you"
    assert issue_tier("open", "classified", False, None, hold_kind="evidence") == "idle"
    assert issue_tier("open", "classified", False, None, hold_kind="stronger_model") == "idle"
    assert issue_tier("open", "needs_human", False, None) == "idle"   # removed value is inert
    assert issue_tier("closed", "classified", False, None, hold_kind="owner_action") == "live"
```

- [ ] **Step 2: Run to verify they fail**

Run: `… -m pytest server/tests/test_desired_state.py server/tests/test_pm_tree.py -q`
Expected: FAIL (`TypeError: unexpected keyword 'hold_kind'`).

- [ ] **Step 3: Implement**

`server/pm/desired_state.py` — add to `ItemFacts` after `initiative_stage_at`:

```python
    # Spec 2026-09-18 §3: the hold on the item. Only owner_action / decision park it.
    hold_kind: str | None = None
```

and in `derive_status` replace the needs-human branch:

```python
    if f.needs_human_at is not None or f.hold_kind in HELD_KINDS:
        # The give-up moment is the decision; a hold dates from its classification write.
        return Decision(STATUS_NEEDS_HUMAN, f.needs_human_at or f.classified_at)
```

with, near the top of the module:

```python
#: Hold kinds that park the card (spec 2026-09-18 §7). evidence / stronger_model exit
#: on their own and never reach the board.
HELD_KINDS: frozenset[str] = frozenset({"owner_action", "decision"})
```

`server/pm/reconcile_inputs.py` — add `hold_kind=w.hold_kind,` to the `ItemFacts(...)` construction.

`server/pm/tree.py` — `issue_tier`:

```python
def issue_tier(state: str | None, triage_state: str | None,
               has_open_blocker: bool, session_status: str | None,
               board_status: str | None = None, *,
               reason: str | None = None, hold_kind: str | None = None) -> str:
    if state in ("closed", "merged"):
        return "live"
    if reason is not None:
        return "needs-you"
    if (hold_kind in HELD_KINDS or session_status in HUMAN_WAIT_SESSION_STATUSES
            or board_status == STATUS_NEEDS_HUMAN):
        return "needs-you"
```

importing `HELD_KINDS` from `desired_state` next to the existing `STATUS_NEEDS_HUMAN` import. `triage_state` stays in the signature (callers pass it positionally) but is no longer read for needs-you; leave a one-line comment saying so.

`server/api/pm_tree.py` — both call sites: `issue_tier(w.state, w.triage_state, blocked[w.id], session[w.id], board_status[w.id], reason=reason, hold_kind=w.hold_kind)`.

- [ ] **Step 4: Run the tests plus the reconcile and tree API suites**

Run: `… -m pytest server/tests/test_desired_state.py server/tests/test_pm_tree.py server/tests/test_pm_tree_auto_resolved.py server/tests/test_reconcile_inputs.py server/tests/test_pm_queue_store.py -q` (skip any file that does not exist).
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add server/pm/desired_state.py server/pm/reconcile_inputs.py server/pm/tree.py server/api/pm_tree.py server/tests/test_desired_state.py server/tests/test_pm_tree.py
printf 'feat(pm): board status and roadmap tier derive from the hold kind\n\nSpec 2026-09-18 §7 (foundation slice).\n\nCo-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>\n' > /tmp/c4.txt
git commit -F /tmp/c4.txt
```

---

### Task 5: The `set_hold` session tool

**Files:**
- Modify: `server/sessions/tools/registry.py:9-28` (spec table), `:36-82` (mount predicate)
- Modify: `server/sessions/tools/tools.py` (handler + `by_name`)
- Modify: `server/sessions/tools/instruction.py` (blurb — read the file; every mounted name needs one, `test_every_mounted_name_has_a_blurb` enforces it)
- Modify: `docs/superpowers/evals/session-tools/cases.jsonl`, `docs/product/features/session-tools.md`
- Test: `server/tests/test_session_tools.py`, `server/tests/test_pm_holds.py`

**Interfaces:**
- `ToolSpec("set_hold", ToolClass.SIGNAL, "Hold or release this session's issue")` — SIGNAL because it moves a roadmap chip and must never raise a permission card in an unattended runner session (the same reasoning as `request_model`). Mounted with the signalling tools (Claude only; Codex uses the overlay endpoint over loopback and is recorded as `lan@local`).
- Handler args: `{"kind": "evidence" | "stronger_model" | null, "reason": str}`; `ref` optional `owner/repo#N`, default the session's `origin_ref`. Actor `session:<ctx.session_id>`. Returns `{"ref", "holdKind", "holdBy"}`.
- Produces: `holds.apply_session_hold(db, *, ref: str, kind: str | None, reason: str, session_id: str, now: datetime) -> WorkItem` — the ONE in-process path; raises `ToolArgError`-compatible `ValueError` for an unknown ref.

- [ ] **Step 1: Write the failing tests**

Append to `server/tests/test_session_tools.py` (read its imports and the `mounted_names` helpers first, then mirror the existing style):

```python
def test_set_hold_is_mounted_with_the_signalling_tools():
    from server.sessions.tools.registry import mounted_names
    assert "set_hold" in mounted_names(signalling_tools=True, self_model_switch=False)
    assert "set_hold" not in mounted_names(signalling_tools=False, self_model_switch=False)
```

Append to `server/tests/test_pm_holds.py`:

```python
def test_apply_session_hold_resolves_ref_and_limits_kind(tmp_path, monkeypatch):
    import asyncio
    from server.db import db_session, init_db
    from server.pm.holds import apply_session_hold
    monkeypatch.setenv("CCSWITCH_DB_URL", f"sqlite+aiosqlite:///{tmp_path}/t.db")

    async def _run():
        await init_db()
        async with db_session() as db:
            db.add(WorkItem(id="w1", repo="o/r", number=7, kind="issue", body="b",
                            triage_state="classified"))
            await db.flush()
            wi = await apply_session_hold(db, ref="o/r#7", kind="evidence",
                                          reason="needs a repro", session_id="s1", now=NOW)
            assert (wi.hold_kind, wi.hold_by) == ("evidence", "session:s1")
            with pytest.raises(HoldPermissionError):
                await apply_session_hold(db, ref="o/r#7", kind="owner_action",
                                         reason="park", session_id="s1", now=NOW)
            wi = await apply_session_hold(db, ref="o/r#7", kind=None, reason="found it",
                                          session_id="s1", now=NOW)
            assert wi.hold_kind is None
            with pytest.raises(ValueError):
                await apply_session_hold(db, ref="o/r#99", kind="evidence", reason="r",
                                         session_id="s1", now=NOW)
    asyncio.run(_run())
```

Check how other tests in `server/tests/` bootstrap a throwaway SQLite (`grep -ln "init_db" server/tests/test_pm_*.py | head -3`) and copy that idiom exactly if it differs from the `monkeypatch.setenv` + `init_db()` form above.

- [ ] **Step 2: Run to verify they fail**

Run: `… -m pytest server/tests/test_session_tools.py server/tests/test_pm_holds.py -q -k "set_hold or session_hold"`
Expected: FAIL.

- [ ] **Step 3: Implement**

`server/pm/holds.py` — append:

```python
async def apply_session_hold(db, *, ref: str, kind: str | None, reason: str,
                             session_id: str, now: datetime) -> WorkItem:
    """The session tool's one call: resolve ``owner/repo#N`` to the mirrored issue and
    set (``kind``) or clear (``kind is None``) its hold as ``session:<id>``."""
    from sqlalchemy import select
    from server.pm.refs import split_ref
    parts = split_ref(ref)
    if parts is None:
        raise ValueError(f"ref must look like owner/repo#123, got {ref!r}")
    repo, number = parts
    wi = (await db.execute(select(WorkItem).where(
        WorkItem.repo == repo, WorkItem.number == number,
        WorkItem.kind == "issue"))).scalar_one_or_none()
    if wi is None:
        raise ValueError(f"{ref} is not a mirrored issue")
    actor = f"session:{session_id}"
    if kind is None:
        clear_hold(wi, actor=actor)
    else:
        set_hold(wi, kind=kind, reason=reason, actor=actor, now=now)
    await db.flush()
    return wi
```

`server/sessions/tools/registry.py` — add after `request_model`:

```python
    # SIGNAL for the same reason as request_model: it moves a roadmap chip, never a
    # card — an unattended runner session must be able to say "this needs evidence"
    # or "this needs a stronger model" (spec 2026-09-18 §8) without a prompt nobody
    # is there to answer. Agents may only set the two auto-exiting kinds; holds.py
    # enforces that, not this table.
    ToolSpec("set_hold", ToolClass.SIGNAL, "Hold or release this session's issue"),
```

and in `mounted_signal_names`, append `"set_hold"` to `names` unconditionally when `signalling_tools` (after `hand_back`, before the `request_model` conditional).

`server/sessions/tools/tools.py` — add a handler after `request_model` and register it in `by_name`:

```python
    @spec_tool("set_hold",
               "Hold or release the issue this session was dispatched for, on the "
               f"{brand.NAME} roadmap. kind 'evidence': someone must reproduce or correlate "
               "before anyone builds (the next run investigates instead of implementing). "
               "kind 'stronger_model': you are under-powered for this; the next attempt "
               "runs one model tier up. kind null: release the hold you set (e.g. you "
               "found the cause and wrote it into the issue). reason: one sentence, "
               "required. ref (optional): owner/repo#N, default this session's issue. "
               "Only these two kinds are yours to set; a hold that needs the owner is "
               "hand_back's job.",
               {"type": "object",
                "properties": {"kind": {"type": ["string", "null"],
                                        "enum": ["evidence", "stronger_model", None]},
                               "reason": {"type": "string"},
                               "ref": {"type": "string"}},
                "required": ["kind", "reason"]})
    @guarded
    async def set_hold(args: dict[str, Any]) -> Any:
        from datetime import datetime, timezone
        from server.db import db_session
        from server.pm.holds import HoldPermissionError, apply_session_hold
        rec = ctx.record()
        ref = args.get("ref") or getattr(rec, "origin_ref", None)
        if not ref:
            raise ToolArgError("this session has no issue ref; pass ref=owner/repo#N")
        try:
            async with db_session() as db:
                wi = await apply_session_hold(
                    db, ref=str(ref), kind=args.get("kind"), reason=str(args.get("reason") or ""),
                    session_id=ctx.session_id, now=datetime.now(timezone.utc))
                await record_audit(db, "pm.hold.set" if wi.hold_kind else "pm.hold.release",
                                   ctx.identity, {"workItemId": wi.id, "kind": wi.hold_kind or args.get("kind"),
                                                  "reason": wi.hold_reason, "actorClass": "agent",
                                                  "session_id": ctx.session_id}, via="session")
        except (HoldPermissionError, ValueError) as e:
            raise ToolArgError(str(e)) from e
        return {"ref": ref, "holdKind": wi.hold_kind, "holdBy": wi.hold_by}
```

Check `record_audit`'s import in `tools.py` (it is used by `_audit_tool_call`, so it is already imported) and whether `db_session()` commits on exit (read `server/db/__init__.py`; if it does not, `await db.commit()` before leaving the block, matching `record_feedback`).

`server/sessions/tools/instruction.py` — add the blurb for `set_hold` in the same structure as `request_model`'s (read the file; the test `test_every_mounted_name_has_a_blurb` names the table).

`docs/superpowers/evals/session-tools/cases.jsonl` — append one line:

```json
{"id": "evidence-hold-not-hand-back", "prompt": "[dispatched for o/r#42, an auto-error report: 'loop_lag: event loop blocked for 1.6s'] Implement a fix.", "expect_tools": ["set_hold"], "forbid_tools": ["hand_back"], "rubric": "Recognises a duration-only warning has no cause to fix, calls set_hold with kind=evidence and a one-sentence reason naming what evidence is missing, and ends with mark_no_pr; does not hand_back to ask the user to investigate."}
```

`docs/product/features/session-tools.md` — add `set_hold` to the title line's tool list and a short subsection under "The `SIGNAL` tool class" explaining the two agent-settable kinds and why it is SIGNAL.

- [ ] **Step 4: Run the tool suites**

Run: `… -m pytest server/tests/test_session_tools.py server/tests/test_session_tools_instruction.py server/tests/test_session_tool_permissions.py server/tests/test_permissions_session_tools.py server/tests/test_pm_holds.py -q`
Expected: PASS. A permissions test that enumerates SIGNAL names by count will need `set_hold` added to its expected list.

- [ ] **Step 5: Commit**

```bash
git add server/pm/holds.py server/sessions/tools/ docs/superpowers/evals/session-tools/cases.jsonl docs/product/features/session-tools.md server/tests/test_session_tools.py server/tests/test_pm_holds.py
printf 'feat(session-tools): set_hold lets a session ask for evidence or a stronger model\n\nSpec 2026-09-18 §8. SIGNAL class, agent-settable kinds only.\n\nCo-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>\n' > /tmp/c5.txt
git commit -F /tmp/c5.txt
```

---

### Task 6: Assistant policy rows, docs, and the full gate

**Files:**
- Modify: `server/tests/test_assistant_policy.py:159` (row already `False` for the overlay PATCH — verify; no new route this plan)
- Modify: `docs/product/features/product-management-layer.md` (new "Holds" paragraph under the overlay/triage section)
- Modify: `AGENTS.md` only if `.agents/instructions/core.md` changed (it did not in this plan)

- [ ] **Step 1: Confirm the policy row and add the doc paragraph**

`grep -n "overlay" server/tests/test_assistant_policy.py` must show the PATCH row with `False`. It does today; nothing to add. Add to `docs/product/features/product-management-layer.md`, after the paragraph that describes `triage_state` (grep `triage_state` to find it):

```markdown
**Holds (spec `docs/superpowers/specs/2026-09-18-hold-ladder-design.md`, foundation).** A work item
may carry one hold: `hold_kind` (`evidence`, `stronger_model`, `decision`, `owner_action`),
`hold_reason` (mandatory), `hold_by` (the actor string: a person's login, `session:<id>`,
`assistant:<login>`, `lan@local`, `runner`, `automation:<id>`, `migration`), `hold_set_at`, and
`hold_fingerprint` (the body fingerprint at hold time). The former `needs_human` triage state is
gone: rejecting a classification proposal now writes an `owner_action` hold by the rejecting
person, and migration 0049 (applied on the live DB by `scripts/convert_legacy_needs_human.py`)
converted the surviving rows. Only `owner_action` and a pending `decision` derive the board's
"Needs human" and the roadmap's needs-you tier; the other two never park the card. Every overlay
write records its actor in `classified_by`; agents (sessions, the assistant, loopback callers)
may set or clear only `evidence` and `stronger_model` (`server/pm/holds.py`). The ladder, evidence
dispatch, decision proposals, and the rename to "Held" are the later series items of that spec.
```

- [ ] **Step 2: Run the four-step gate**

```bash
env -i HOME=/tmp/vp-home PATH="$PATH" CCSWITCH_DEV=1 PYTHONPATH=$PWD/src /home/tony/GitHub/viberpit/.venv/bin/python -m ruff check src/ server/
env -i HOME=/tmp/vp-home PATH="$PATH" CCSWITCH_DEV=1 PYTHONPATH=$PWD/src /home/tony/GitHub/viberpit/.venv/bin/python -m mypy server/ --ignore-missing-imports
env -i HOME=/tmp/vp-home PATH="$PATH" CCSWITCH_DEV=1 PYTHONPATH=$PWD/src /home/tony/GitHub/viberpit/.venv/bin/python -m pytest tests/ -q
env -i HOME=/tmp/vp-home PATH="$PATH" CCSWITCH_DEV=1 PYTHONPATH=$PWD/src /home/tony/GitHub/viberpit/.venv/bin/python -m pytest server/tests/ -n auto --dist loadfile --timeout=300 -q 2>&1 | tee /tmp/hold-foundation-server-tests.log | tail -15
/home/tony/GitHub/viberpit/.venv/bin/python scripts/brand_ratchet.py
```

Expected: all green. Known rocky-only `-n auto` flakes (UDS daemon spawn, reauth timing, two ws timing tests, codex overlay) re-run alone before being called pre-existing. Any test that still spells `"needs_human"` as a triage value is a real failure of this plan, not a flake: fix the call site, do not skip.

- [ ] **Step 3: Commit, re-check the migration number, push, PR**

```bash
ls server/db/migrations/versions/ | sort | tail -2     # still 0048 then 0049? else renumber (Task 2 Step 6)
git add docs/product/features/product-management-layer.md
printf 'docs(pm): describe work-item holds (foundation)\n\nCo-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>\n' > /tmp/c6.txt
git commit -F /tmp/c6.txt
git push -u origin hold-ladder-foundation
```

Before `gh pr create`, list open PRs touching `server/api/pm.py`, `server/pm/desired_state.py`, `server/pm/tree.py`, `server/db/models.py` with the GraphQL query in `.agents/instructions/core.md` § "check whether the same work is already in flight" and rebase or coordinate. Then PR body (via `--body-file`), ending with the Claude Code trailer:

```
## Summary
Series item 2 of docs/superpowers/specs/2026-09-18-hold-ladder-design.md: typed holds on work items, honest actor provenance on the overlay API, per-class hold permissions, the `set_hold` session tool, and the derivation reading `hold_kind` instead of the removed `needs_human` triage state. Migration 0049 is the record; `scripts/convert_legacy_needs_human.py` applies the conversion on the live DB after deploy (nothing runs alembic at deploy).

Refs #1540
```

`gh pr create` (no `--draft`), then `gh pr merge <#> --squash --auto`.

- [ ] **Step 4: After the merge deploys, convert the live rows**

On dobby, after `deploy/status` shows the merged sha live: `ssh dobby` and run `scripts/convert_legacy_needs_human.py --dry-run` then `--apply` from the deploy checkout with `CCSWITCH_DB_URL` from `/etc/viberpit/ccswitch.env` (sudo read). Expected: `legacy needs_human rows: 8` then `remaining legacy rows: 0`. Record the counts in a comment on the PR.

---

## Self-review

**Spec coverage (this plan's slice):** §3 columns → Task 1; vocabulary shrink → Task 1; reject → owner-action hold → Task 3; migration 0049 + conversion → Task 2; `_REQUIRED_EXTRAS` → not needed (no new table; noted in Task 1 Step 6); §8 actor resolution → Task 3; per-class permissions → Tasks 1, 3; mandatory reason → Task 1, 3; `set_hold` tool + eval case → Task 5; audit types → Tasks 3, 5; `False` policy row → Task 6 (already present); §7 minimal derivation → Task 4. Deferred by design to later plans: `decision_proposals` table and `propose_decision` (item 5), `dispatch_kind` (item 4), queue `held` skip and `needs_human_at` removal and release signals (item 3), rename (item 6).

**Type consistency:** `set_hold(wi, *, kind, reason, actor, now)` and `clear_hold(wi, *, actor)` are used identically in Tasks 1, 3, 5. `apply_session_hold(db, *, ref, kind, reason, session_id, now)` defined in Task 5 matches its test. `HELD_KINDS` is defined in `desired_state.py` and imported by `tree.py` (Task 4). `actor_for_request` (Task 3) reads `state.identity` and `state.via`, which `server/auth/middleware.py:259-260, 278` set.

**Placeholders:** none; every step carries its code or exact command. The `body_fingerprint` signature, `split_ref`, and `db_session` commit semantics were verified against the tree on 2026-09-18.

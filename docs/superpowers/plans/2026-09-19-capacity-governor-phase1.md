# Capacity governor phase 1 — readout, knobs, governor in monitor mode

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship the Capacity readout (`GET /api/capacity` + Delivery → Capacity), the two person-facing
knobs (per-product `priority_rank`, fleet `push_level`), the pin-or-auto `capacity_mode` policy field,
and a `capacity-governor` automation that computes a plan every tick in `monitor` mode. Nothing is
applied in this phase: the runner's behaviour is unchanged.

**Architecture:** A pure planner (`capacity_plan.py`) turns a readout into a plan. A readout builder
(`capacity_readout.py`) composes the runner's own count seams and `decide_implementation` so the page
can never disagree with admission. A supervised loop (`capacity_governor.py`, the `control_band_monitor`
mold, every I/O injected) writes the plan to a singleton `capacity_plans` row. The web gets a third
Delivery scope. Phase 2 (act mode) is a separate plan.

**Tech Stack:** Python 3.12 · FastAPI · SQLAlchemy (SQLite dev/tests, Postgres prod) · pytest ·
React + TypeScript + Vitest + React Query.

**Spec:** `docs/superpowers/specs/2026-09-19-capacity-governor-design.md` (§4 data model, §5 readout,
§6 governor, §7 UI, §9 tests, §10 phasing item 1).

## Global Constraints

- **Brand ratchet has zero slack on `python-package` (1337/1337).** No NEW file may contain the
  string `ccswitch`. The only place that needs `at_ceiling` is `server/api/accounts.py`, which already
  imports from that package: extend its existing import line, never add one. Run
  `python scripts/brand_ratchet.py` before every commit; never run `--update`.
- **New env settings take the `VIBERPIT_` prefix**: `VIBERPIT_CAPACITY_GOVERNOR_ENABLED`.
- **DB clock rule:** Python-side timestamps use `datetime.now(timezone.utc)` (the models' `_utcnow`);
  never `func.now()`.
- **Every new column is nullable** so `init_db` reconcile ALTER-ADDs it on both dialects. Migration
  numbers: check `ls server/db/migrations/versions/ | tail -1` right before committing; chain
  `down_revision` off the newest.
- **Push levels and reserves are fixed:** `PUSH_LEVELS = ("conserve", "steady", "max")`,
  `RESERVE_PCT = {"conserve": 40, "steady": 20, "max": 5}`.
- **Governor modes:** `("off", "monitor", "act")`; the registry default row is disabled (every
  automation row is), the deploy step enables it in `monitor`.
- **Tests:** server → `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 .venv/bin/python -m pytest <file> -q`
  from the checkout (see CLAUDE.md for the env scrub); web → `(cd web && npm run typecheck && npm run lint && npm run test)`.
- **Commit messages via `-F <file>`** (the Bash hook denies command text pairing a deploy word with a
  product word).

---

### Task 1: `Project.priority_rank` — column, migration, API

**Files:**
- Modify: `server/db/models.py` (class `Project`, after `automation_policy_updated_at`)
- Create: `server/db/migrations/versions/0055_project_priority_rank.py`
- Modify: `server/api/pm.py` (`_project_to_dict`, `ProjectUpdateBody`, `update_project`)
- Modify: `web/src/api/pm.ts` (`Project`, `UpdateProjectInput`)
- Test: `server/tests/test_project_priority_rank.py`

**Interfaces:**
- Produces: `Project.priority_rank: int | None`; wire field `priorityRank` on every project payload;
  `PATCH /api/pm/projects/{slug}` accepts `{"priorityRank": int | null}` (422 `bad_priority_rank`
  when `< 1`); audit `pm.project.update` with `changes.priorityRank`.

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_project_priority_rank.py
"""Spec 2026-09-19 §4: per-product priority rank, the person's first knob."""
from __future__ import annotations

from server.tests.test_pm_api import IDENTITY_HEADERS, _client, canonical_mutation_repositories  # noqa: F401


def _create(c, name: str, repo: str) -> dict:
    resp = c.post("/api/pm/projects", json={
        "name": name, "repos": [{"repo": repo, "isPrimary": True}],
    }, headers=IDENTITY_HEADERS)
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_priority_rank_defaults_null_and_patches():
    with _client() as c:
        body = _create(c, "KinDash", "steiner385/KinDash")
        assert body["priorityRank"] is None
        resp = c.patch("/api/pm/projects/kindash", json={"priorityRank": 1}, headers=IDENTITY_HEADERS)
        assert resp.status_code == 200, resp.text
        assert resp.json()["priorityRank"] == 1
        assert c.get("/api/pm/projects/kindash", headers=IDENTITY_HEADERS).json()["priorityRank"] == 1


def test_priority_rank_can_be_cleared_and_rejects_zero():
    with _client() as c:
        _create(c, "Web", "steiner385/web")
        c.patch("/api/pm/projects/web", json={"priorityRank": 2}, headers=IDENTITY_HEADERS)
        resp = c.patch("/api/pm/projects/web", json={"priorityRank": None}, headers=IDENTITY_HEADERS)
        assert resp.status_code == 200 and resp.json()["priorityRank"] is None
        resp = c.patch("/api/pm/projects/web", json={"priorityRank": 0}, headers=IDENTITY_HEADERS)
        assert resp.status_code == 422
        assert resp.json()["detail"]["error"] == "bad_priority_rank"
```

- [ ] **Step 2: Run it** — `pytest server/tests/test_project_priority_rank.py -q` → FAIL (`KeyError: 'priorityRank'`).

- [ ] **Step 3: Model + migration**

In `server/db/models.py`, after `automation_policy_updated_at` in `class Project`:

```python
    # Capacity governor (spec 2026-09-19 §4): the person's priority order. 1 = highest;
    # NULL = unranked (sorted after ranked products by created_at). Nullable so
    # reconcile ALTER-ADDs it on both dialects.
    priority_rank: Mapped[int | None] = mapped_column(Integer, nullable=True)
```

`server/db/migrations/versions/0055_project_priority_rank.py`:

```python
"""Per-product priority rank for the capacity governor.

Revision ID: 0055_project_priority_rank
Revises: 0054_triage_map_resolution

Nullable Integer: init_db reconcile adds this on SQLite and Postgres.
"""
from alembic import op
import sqlalchemy as sa

revision = "0055_project_priority_rank"
down_revision = "0054_triage_map_resolution"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("projects")}
    if "priority_rank" not in columns:
        op.add_column("projects", sa.Column("priority_rank", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("projects", "priority_rank")
```

- [ ] **Step 4: API**

`server/api/pm.py` — in `_project_to_dict` add `"priorityRank": p.priority_rank,` after
`"automationPolicyUpdatedAt"`. In `ProjectUpdateBody` add `priorityRank: int | None = None`. In
`update_project`, right after the `repo` validation block:

```python
    rank_set = "priorityRank" in body.model_fields_set
    if rank_set and body.priorityRank is not None and body.priorityRank < 1:
        raise HTTPException(status_code=422, detail={
            "error": "bad_priority_rank",
            "message": "priorityRank must be 1 or higher, or null to unrank"})
```

and after the `repo_changing` block (before `if not changes:`):

```python
    if rank_set and body.priorityRank != p.priority_rank:
        changes["priorityRank"] = {"from": p.priority_rank, "to": body.priorityRank}
        p.priority_rank = body.priorityRank
```

`web/src/api/pm.ts`: add `priorityRank: number | null` to `Project` and `priorityRank?: number | null`
to `UpdateProjectInput`.

- [ ] **Step 5: Run** the new test file and `server/tests/test_pm_api.py` → PASS; `(cd web && npm run typecheck)` → PASS.
- [ ] **Step 6: Commit** — `feat(pm): per-product priority_rank (capacity governor knob 1)`.

---

### Task 2: `capacity_mode` policy field

**Files:**
- Modify: `server/automations/product_policy.py` (`PRODUCT_POLICY_SCHEMA`)
- Modify: `server/tests/test_product_policy.py::test_schema_keys_and_defaults`

**Interfaces:**
- Produces: `effective_policy(...)["capacity_mode"] in ("auto", "pinned")`, default `"auto"`.
  `max_in_flight` label becomes `"Max in flight (pin)"`.

- [ ] **Step 1: Update the test** — in `test_schema_keys_and_defaults` insert `"capacity_mode"` after
  `"max_in_flight"` in the `keys` list and `"capacity_mode": "auto"` in the defaults dict.
- [ ] **Step 2: Run** `pytest server/tests/test_product_policy.py -q` → FAIL.
- [ ] **Step 3: Implement** — in `PRODUCT_POLICY_SCHEMA`, change the `max_in_flight` field to
  `label="Max in flight (pin)"` with help
  `"This product's concurrent runner sessions. Read as a hard cap while In-flight cap is pinned; "
  "the fallback while auto and no plan exists. 0 pauses issue dispatch without losing the rest of its policy."`
  and insert directly after it:

```python
    ConfigField(key="capacity_mode", label="In-flight cap", type="enum", default="auto",
                choices=["auto", "pinned"],
                help="auto: the capacity governor sets this product's in-flight cap from the "
                     "fleet's real headroom and your priority order. pinned: the value above "
                     "is a hard cap the governor respects."),
```

- [ ] **Step 4: Run** `pytest server/tests/test_product_policy.py server/tests/test_product_policy_edits.py server/tests/test_backlog_policy_seams.py -q` → PASS (the web renders the new field through the generic `ConfigInput`; no web change).
- [ ] **Step 5: Commit** — `feat(policy): capacity_mode auto|pinned on the product policy`.

---

### Task 3: `capacity_plans` row + store

**Files:**
- Modify: `server/db/models.py` (new model after `BacklogGroup`)
- Create: `server/db/migrations/versions/0056_capacity_plans.py`
- Create: `server/automations/capacity_store.py`
- Modify: `server/tests/test_postgres_compat.py` (`_FK_LINKS` gains `"capacity_plans": {},`)
- Test: `server/tests/test_capacity_store.py`

**Interfaces:**
- Produces: `async read_plan() -> dict | None`, `async write_plan(plan: dict) -> None`. The dict keys:
  `computedAt, pushLevel, applied, fleetConcurrency, perProduct, pending, budget, binding, reasons, defects`.

- [ ] **Step 1: Failing test**

```python
# server/tests/test_capacity_store.py
import asyncio
import pytest

from server.automations.capacity_store import read_plan, write_plan

pytestmark = pytest.mark.usefixtures("isolated_db")


def test_plan_round_trips_and_upserts():
    async def run():
        assert await read_plan() is None
        await write_plan({"pushLevel": "steady", "applied": False, "fleetConcurrency": 3,
                          "perProduct": {"kindash": 2}, "pending": {}, "budget": {"accounts": 4},
                          "binding": {"layer": "product", "code": "product_capacity_full", "text": "x"},
                          "reasons": ["kindash: 2"], "defects": []})
        first = await read_plan()
        assert first["fleetConcurrency"] == 3 and first["perProduct"] == {"kindash": 2}
        assert first["computedAt"] is not None
        await write_plan({**first, "fleetConcurrency": 5})
        assert (await read_plan())["fleetConcurrency"] == 5
    asyncio.run(run())
```

- [ ] **Step 2: Run** → FAIL (`ModuleNotFoundError`).
- [ ] **Step 3: Model** (append to `server/db/models.py` after `BacklogGroup`):

```python
class CapacityPlanRow(Base):
    """The capacity governor's current plan (spec 2026-09-19 §4, D5): ONE row,
    ``id = "current"``, rewritten every tick. Never the human's policy — the runner
    merges this over policy at read time (phase 2). Every non-key column nullable
    so reconcile creates it on both dialects."""

    __tablename__ = "capacity_plans"

    id: Mapped[str] = mapped_column(String, primary_key=True, default="current")
    computed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    push_level: Mapped[str | None] = mapped_column(String, nullable=True)
    applied: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    fleet_concurrency: Mapped[int | None] = mapped_column(Integer, nullable=True)
    per_product: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    pending: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    budget: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    binding: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    reasons: Mapped[list | None] = mapped_column(JSON, nullable=True)
    defects: Mapped[list | None] = mapped_column(JSON, nullable=True)
```

Migration `0056_capacity_plans.py` (down_revision `0055_project_priority_rank`): `op.create_table("capacity_plans", ...)`
guarded by `if "capacity_plans" not in sa.inspect(op.get_bind()).get_table_names():`, columns as above;
`downgrade` drops it.

- [ ] **Step 4: Store**

```python
# server/automations/capacity_store.py
"""Read/write the capacity governor's single plan row (spec 2026-09-19 §4)."""
from __future__ import annotations

from datetime import datetime, timezone

from server.db import db_session
from server.db.models import CapacityPlanRow

PLAN_ID = "current"


def _to_dict(row: CapacityPlanRow) -> dict:
    return {
        "computedAt": row.computed_at.isoformat() if row.computed_at else None,
        "pushLevel": row.push_level, "applied": bool(row.applied),
        "fleetConcurrency": row.fleet_concurrency,
        "perProduct": dict(row.per_product or {}), "pending": dict(row.pending or {}),
        "budget": dict(row.budget or {}), "binding": row.binding,
        "reasons": list(row.reasons or []), "defects": list(row.defects or []),
    }


async def read_plan() -> dict | None:
    async with db_session() as db:
        row = await db.get(CapacityPlanRow, PLAN_ID)
        return None if row is None else _to_dict(row)


async def write_plan(plan: dict) -> None:
    async with db_session() as db:
        row = await db.get(CapacityPlanRow, PLAN_ID)
        if row is None:
            row = CapacityPlanRow(id=PLAN_ID)
            db.add(row)
        row.computed_at = datetime.now(timezone.utc)
        row.push_level = plan.get("pushLevel")
        row.applied = bool(plan.get("applied"))
        row.fleet_concurrency = plan.get("fleetConcurrency")
        row.per_product = dict(plan.get("perProduct") or {})
        row.pending = dict(plan.get("pending") or {})
        row.budget = dict(plan.get("budget") or {})
        row.binding = plan.get("binding")
        row.reasons = list(plan.get("reasons") or [])
        row.defects = list(plan.get("defects") or [])
```

- [ ] **Step 5: Run** the store test, `server/tests/test_schema_reconcile_coverage.py`, and (if `CCSWITCH_TEST_PG_URL` is set) `server/tests/test_postgres_compat.py` → PASS.
- [ ] **Step 6: Commit** — `feat(capacity): capacity_plans row and store`.

---

### Task 4: the pure planner

**Files:**
- Create: `server/automations/capacity_plan.py`
- Test: `server/tests/test_capacity_plan.py`

**Interfaces:**
- Produces:

```python
PUSH_LEVELS = ("conserve", "steady", "max"); RESERVE_PCT = {"conserve": 40, "steady": 20, "max": 5}

@dataclass(frozen=True)
class AccountHeadroom: name: str; engine: str; metered: bool; available: bool; reason: str | None; next_reset_at: str | None
@dataclass(frozen=True)
class HostCapacity: name: str; enabled: bool; healthy: bool | None; active: int; max: int | None
@dataclass(frozen=True)
class ProductCapacity: slug: str; rank: int | None; created_at: str; dispatch: str; capacity_mode: str; pin: int; in_flight: int; eligible: int
@dataclass(frozen=True)
class CapacityInputs: global_cap: int; global_active: int; hosts: tuple[HostCapacity, ...]; accounts: tuple[AccountHeadroom, ...]; runner_ceiling: int; runner_in_flight: int; products: tuple[ProductCapacity, ...]
@dataclass(frozen=True)
class CapacityPlan: push_level: str; fleet_concurrency: int; per_product: dict[str, int]; pending: dict[str, int]; budget: dict; reasons: list[str]; limiting: str  # "runner"|"account"|"host"|"global"|"none"

def product_order(products) -> list[ProductCapacity]   # rank asc (None last), then created_at
def plan_capacity(inputs: CapacityInputs, *, push_level: str, sessions_per_account: int = 2, previous: CapacityPlan | None = None) -> CapacityPlan
def plan_from_dict(d: dict | None) -> CapacityPlan | None   # store row → CapacityPlan (pending/per_product only)
```

- [ ] **Step 1: Failing tests** (`server/tests/test_capacity_plan.py`): build helpers `_acct(name, available=True)`,
  `_host(name, active, max)`, `_prod(slug, rank, eligible, in_flight=0, mode="auto", pin=1, dispatch="auto")`,
  `_inputs(**overrides)` with defaults global_cap 10, active 0, one host (0/6), four available accounts,
  runner_ceiling 6, runner_in_flight 0, products kindash(1, 50), viberpit(2, 30), ghostdriver(3, 5). Tests:
  1. `test_reserve_follows_push_level`: `plan_capacity(i, push_level="max").budget["reservePct"] == 5`.
  2. `test_fleet_is_min_of_four_ceilings`: 4 accounts × 2 = 8, host free 6, global 10, runner 6 → `fleet_concurrency == 6`, `limiting == "runner"`; with `runner_ceiling=20, hosts=(_host("d",4,6),)` → 2 host free → fleet 2, `limiting == "host"`.
  3. `test_split_is_rank_ordered_round_robin`: fleet 6 over the three products → `{"kindash": 3, "viberpit": 2, "ghostdriver": 1}` after two ticks (call twice, passing the first result as `previous`).
  4. `test_pins_come_first`: viberpit `mode="pinned", pin=1` → viberpit 1 always, remaining split over the others.
  5. `test_never_below_in_flight_or_above_eligible`: ghostdriver eligible 1 → cap ≤ 1; kindash in_flight 4 with fleet 3 → kindash ≥ 4.
  6. `test_hysteresis_applies_on_second_tick`: first call with `previous=None` → `per_product` equals pins (carried), `pending` equals the proposal; second call with `previous=first` → `per_product == pending`.
  7. `test_zero_available_accounts_idles_with_reset`: all accounts `available=False, next_reset_at="2026-09-23T08:00:00+00:00"` → `fleet_concurrency == 0`, `limiting == "account"`, a reason contains `"08:00"`.
  8. `test_off_products_and_empty_backlogs_get_nothing`: `dispatch="off"` or `eligible=0` → slug absent from `per_product`.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** `server/automations/capacity_plan.py`:

```python
"""Pure capacity planner (spec 2026-09-19 §6.2). No I/O; table-tested."""
from __future__ import annotations

from dataclasses import dataclass, field

PUSH_LEVELS: tuple[str, ...] = ("conserve", "steady", "max")
RESERVE_PCT: dict[str, int] = {"conserve": 40, "steady": 20, "max": 5}
UNBOUNDED = 10**6


@dataclass(frozen=True)
class AccountHeadroom:
    name: str; engine: str; metered: bool; available: bool
    reason: str | None = None; next_reset_at: str | None = None


@dataclass(frozen=True)
class HostCapacity:
    name: str; enabled: bool; healthy: bool | None; active: int; max: int | None


@dataclass(frozen=True)
class ProductCapacity:
    slug: str; rank: int | None; created_at: str; dispatch: str; capacity_mode: str
    pin: int; in_flight: int; eligible: int


@dataclass(frozen=True)
class CapacityInputs:
    global_cap: int; global_active: int
    hosts: tuple[HostCapacity, ...]; accounts: tuple[AccountHeadroom, ...]
    runner_ceiling: int; runner_in_flight: int
    products: tuple[ProductCapacity, ...]


@dataclass(frozen=True)
class CapacityPlan:
    push_level: str
    fleet_concurrency: int
    per_product: dict[str, int]
    pending: dict[str, int]
    budget: dict
    reasons: list[str] = field(default_factory=list)
    limiting: str = "none"


def product_order(products) -> list[ProductCapacity]:
    return sorted(products, key=lambda p: (p.rank is None, p.rank or 0, p.created_at))


def plan_from_dict(d: dict | None) -> CapacityPlan | None:
    if not d:
        return None
    return CapacityPlan(push_level=d.get("pushLevel") or "steady",
                        fleet_concurrency=int(d.get("fleetConcurrency") or 0),
                        per_product={k: int(v) for k, v in (d.get("perProduct") or {}).items()},
                        pending={k: int(v) for k, v in (d.get("pending") or {}).items()},
                        budget=dict(d.get("budget") or {}))


def _fleet(inputs: CapacityInputs, sessions_per_account: int) -> tuple[int, str, dict]:
    available = [a for a in inputs.accounts if a.available]
    account_cap = len(available) * sessions_per_account
    host_free = 0
    for h in inputs.hosts:
        if not h.enabled or h.healthy is not True:
            continue
        host_free += UNBOUNDED if h.max is None else max(0, h.max - h.active)
    global_free = max(0, inputs.global_cap - inputs.global_active)
    candidates = {
        "runner": inputs.runner_ceiling,
        "account": account_cap,
        "host": host_free + inputs.runner_in_flight,
        "global": global_free + inputs.runner_in_flight,
    }
    limiting = min(candidates, key=lambda k: (candidates[k], ("runner", "account", "host", "global").index(k)))
    budget = {"accountsAvailable": len(available), "accountsTotal": len(inputs.accounts),
              "sessionsPerAccount": sessions_per_account, "accountCap": account_cap,
              "hostFree": min(host_free, UNBOUNDED), "globalFree": global_free,
              "runnerCeiling": inputs.runner_ceiling, "runnerInFlight": inputs.runner_in_flight}
    return max(0, candidates[limiting]), limiting, budget


def _split(fleet: int, products: list[ProductCapacity]) -> dict[str, int]:
    working = [p for p in products if p.dispatch != "off" and p.eligible > 0]
    caps: dict[str, int] = {}
    for p in working:
        if p.capacity_mode == "pinned":
            caps[p.slug] = min(p.pin, p.eligible)
            fleet -= caps[p.slug]
    auto = [p for p in working if p.capacity_mode != "pinned"]
    for p in auto:
        caps[p.slug] = 0
    changed = True
    while fleet > 0 and changed:
        changed = False
        for p in auto:
            if fleet <= 0:
                break
            if caps[p.slug] < p.eligible:
                caps[p.slug] += 1
                fleet -= 1
                changed = True
    for p in working:
        caps[p.slug] = max(caps[p.slug], p.in_flight)
    return caps


def plan_capacity(inputs: CapacityInputs, *, push_level: str, sessions_per_account: int = 2,
                  previous: CapacityPlan | None = None) -> CapacityPlan:
    if push_level not in PUSH_LEVELS:
        push_level = "steady"
    reserve = RESERVE_PCT[push_level]
    fleet, limiting, budget = _fleet(inputs, sessions_per_account)
    budget["reservePct"] = reserve
    ordered = product_order(inputs.products)
    proposed = _split(fleet, ordered)
    reasons: list[str] = []
    if fleet == 0 and limiting == "account":
        resets = sorted(a.next_reset_at for a in inputs.accounts if a.next_reset_at)
        reasons.append("no account has headroom under the %d%% reserve%s" % (
            reserve, f"; earliest reset {resets[0]}" if resets else ""))
    else:
        reasons.append(f"fleet {fleet}: limited by {limiting} "
                       f"(accounts {budget['accountsAvailable']}×{sessions_per_account}, "
                       f"host free {budget['hostFree']}, global free {budget['globalFree']}, "
                       f"runner ceiling {inputs.runner_ceiling})")
    per_product: dict[str, int] = {}
    pending: dict[str, int] = {}
    prev_caps = previous.per_product if previous else {}
    prev_pending = previous.pending if previous else {}
    by_slug = {p.slug: p for p in ordered}
    for slug, value in proposed.items():
        p = by_slug[slug]
        current = prev_caps.get(slug, p.pin)
        if p.capacity_mode == "pinned" or value == current or prev_pending.get(slug) == value:
            per_product[slug] = value
            reasons.append(f"{slug}: {value} (rank {p.rank if p.rank is not None else '—'}, "
                           f"{'pinned' if p.capacity_mode == 'pinned' else 'auto'}, {p.eligible} eligible)")
        else:
            per_product[slug] = current
            pending[slug] = value
            reasons.append(f"{slug}: {current} → {value} pending confirmation next tick")
    return CapacityPlan(push_level=push_level, fleet_concurrency=fleet, per_product=per_product,
                        pending=pending, budget=budget, reasons=reasons, limiting=limiting)
```

- [ ] **Step 4: Run** `pytest server/tests/test_capacity_plan.py -q` → PASS. Adjust test expectations only where the algorithm above is unambiguous (round robin gives kindash 2, viberpit 2, ghostdriver 1 with one slot left → kindash 3).
- [ ] **Step 5: Commit** — `feat(capacity): pure planner (fleet budget, rank-ordered split, hysteresis)`.

---

### Task 5: account headroom (in `server/api/accounts.py`, ratchet-safe)

**Files:**
- Modify: `server/api/accounts.py` (import line 36 → `from ccswitch.selector import at_ceiling, rank_accounts`; new function)
- Test: `server/tests/test_account_headroom.py`

**Interfaces:**
- Produces: `async account_headroom(orch, *, reserve_pct: float, timeout: float = USAGE_FETCH_TIMEOUT) -> list[dict]`
  with rows `{name, engine, provider, enabled, metered, available, reason, nextResetAt, windows}`.
  `metered` = usage present, `provider == "anthropic"`, `availability == AVAILABLE`. `available` =
  metered and enabled and `not at_ceiling(usage, 100 - reserve_pct)`.

- [ ] **Step 1: Failing test**

```python
# server/tests/test_account_headroom.py
import asyncio
from datetime import datetime, timezone

import pytest

from ccswitch.quota import AVAILABLE, QuotaSnapshot, QuotaWindow
from server.api import accounts as accounts_api


def _snap(weekly_pct: float, provider="anthropic"):
    return QuotaSnapshot(provider=provider, fetched_at=datetime.now(timezone.utc), availability=AVAILABLE,
                         windows=(QuotaWindow(key="session", label="session", used_pct=10.0),
                                  QuotaWindow(key="weekly", label="weekly", used_pct=weekly_pct,
                                              resets_at=datetime(2026, 9, 23, 8, tzinfo=timezone.utc))))


@pytest.fixture
def fake_accounts(monkeypatch):
    infos = [{"name": "a", "enabled": True, "provider": "anthropic"},
             {"name": "b", "enabled": True, "provider": "anthropic"},
             {"name": "c", "enabled": False, "provider": "anthropic"},
             {"name": "d", "enabled": True, "provider": "openai"}]
    usages = {"a": (_snap(50.0), None), "b": (_snap(90.0), None), "c": (_snap(1.0), None),
              "d": (_snap(1.0, provider="openai"), None)}
    monkeypatch.setattr(accounts_api, "list_accounts", lambda: infos)
    async def gather(orch, names, timeout):
        return {n: usages[n] for n in names}
    monkeypatch.setattr(accounts_api, "_gather_usages", gather)


def test_headroom_applies_reserve_enabled_and_metering(fake_accounts):
    rows = asyncio.run(accounts_api.account_headroom(object(), reserve_pct=20))
    by = {r["name"]: r for r in rows}
    assert by["a"]["available"] is True
    assert by["b"]["available"] is False and by["b"]["reason"] == "at_ceiling"
    assert by["b"]["nextResetAt"].startswith("2026-09-23T08:00")
    assert by["c"]["available"] is False and by["c"]["reason"] == "disabled"
    assert by["d"]["metered"] is False and by["d"]["available"] is False
```

- [ ] **Step 2: Run** → FAIL (`AttributeError: account_headroom`).
- [ ] **Step 3: Implement** (append to `server/api/accounts.py`, near `_gather_usages`):

```python
async def account_headroom(orch, *, reserve_pct: float,
                           timeout: float = USAGE_FETCH_TIMEOUT) -> list[dict]:
    """Per-account availability for the capacity governor (spec 2026-09-19 §5).

    Lives here, not in a new module: this file already imports the selector, and the
    brand ratchet has no slack for another import of the legacy package. ``available``
    uses ``at_ceiling`` with ``ceiling = 100 - reserve_pct`` so the governor and the
    account selector agree on what "usable" means; an unmetered engine (Codex,
    Copilot — D4) counts zero and says so.
    """
    infos = await asyncio.to_thread(list_accounts)
    usages = await _gather_usages(orch, [a["name"] for a in infos], timeout)
    ceiling = 100.0 - float(reserve_pct)
    rows: list[dict] = []
    for acct in infos:
        name = acct["name"]
        usage, _stale = usages.get(name, (None, None))
        enabled = bool(acct.get("enabled", True))
        metered = usage is not None and usage.provider == "anthropic" and usage.availability == AVAILABLE
        reason = None
        next_reset = None
        if not metered:
            reason = "not_metered"
        elif not enabled:
            reason = "disabled"
        elif at_ceiling(usage, ceiling):
            reason = "at_ceiling"
            resets = [w.resets_at for w in usage.windows_at_or_over(ceiling) if w.resets_at]
            next_reset = iso_utc(min(resets)) if resets else None
        rows.append({
            "name": name, "engine": acct.get("engine") or ("codex" if acct.get("provider") == "openai" else "claude_code"),
            "provider": acct.get("provider"), "enabled": enabled, "metered": metered,
            "available": reason is None, "reason": reason, "nextResetAt": next_reset,
            "windows": [{"key": w.key, "pct": w.used_pct, "resetsAt": iso_utc(w.resets_at)}
                        for w in (usage.windows if usage is not None else ())],
        })
    return rows
```

- [ ] **Step 4: Run** the test and `python scripts/brand_ratchet.py` (python-package must still read 1337/1337) → PASS.
- [ ] **Step 5: Commit** — `feat(accounts): account_headroom for the capacity governor`.

---

### Task 6: readout builder, wiring, `GET /api/capacity`

**Files:**
- Create: `server/automations/capacity_readout.py`
- Create: `server/automations/capacity_wiring.py`
- Create: `server/api/capacity.py`
- Modify: `server/app.py` (include the router next to `automations_router`)
- Modify: `server/sessions/orchestrator.py` (one method `capacity_readout`)
- Test: `server/tests/test_capacity_readout.py`, `server/tests/test_capacity_api.py`

**Interfaces:**
- Produces:

```python
@dataclass
class CapacitySources:   # every seam injected; capacity_wiring builds the real one
    global_cap: Callable[[], Awaitable[int]]
    global_active: Callable[[], int]
    hosts: Callable[[], Awaitable[list[dict]]]                 # host_to_wire dicts
    accounts: Callable[[float], Awaitable[list[dict]]]         # account_headroom(reserve_pct)
    runner_config: Callable[[], Awaitable[tuple[str, dict]]]   # orch._backlog_read_config
    target_states: Callable[[], Awaitable[dict[str, dict]]]    # orch._backlog_ctx.list_target_states
    candidates: Callable[[], Awaitable[tuple[int, int, int, dict | None]]]  # orch._backlog_count_candidates
    products: Callable[[], Awaitable[list[dict]]]              # {slug,name,priorityRank,createdAt,policy,primaryRepo}
    decide: Callable[[str, str], Awaitable[tuple[Any, dict]]]  # (repo, slug) -> (ImplementationDecision, policy)
    session_status: Callable[[str], Awaitable[str | None]]
    plan: Callable[[], Awaitable[dict | None]]                 # capacity_store.read_plan
    governor_config: Callable[[], Awaitable[tuple[str, dict]]] # (mode, config) of capacity-governor
    kill_switch: Callable[[], bool]

async def build_readout(src: CapacitySources) -> dict     # spec §5 shape
def readout_to_inputs(readout: dict) -> CapacityInputs     # for the planner
```

- [ ] **Step 1: Failing readout test** — `server/tests/test_capacity_readout.py` builds a
  `CapacitySources` of async lambdas: global cap 6 / active 2; one host `{"name":"dobby","enabled":True,"healthy":True,"activeSessions":2,"maxSessions":6}`;
  accounts two rows (one available); runner config `("on", {"max_concurrent": 6, "max_spawns_per_day": 500})`;
  target states `{"o/r#1": {"product": "viberpit", "session_id": "s1", "ownership_token": "t"}}`;
  candidates `(10, 0, 0, {"viberpit": {"dispatch": "auto", "eligible": 10, "cap": 1}})`;
  products `[{"slug": "viberpit", "name": "ViberPit", "priorityRank": 1, "createdAt": "2026-09-01", "policy": {"dispatch": "auto"}, "primaryRepo": "o/r"}]`;
  `decide` returning a fake decision object with `.constraints = [SimpleNamespace(code="product_capacity_full", state="refused", text="The product capacity limit is reached (1/1).")]`;
  `session_status` → `"archived"`; plan → None; governor config `("monitor", {"push_level": "steady", "sessions_per_account": 2})`; kill switch True.
  Assert: `r["products"][0]["constraint"] == "product_capacity_full"`, `r["products"][0]["inFlight"] == 1`,
  `r["defects"][0]["target"] == "o/r#1"`, `r["binding"]["layer"] == "defect"`, `r["accounts"]["available"] == 1`,
  `readout_to_inputs(r).products[0].eligible == 10`. Second test: `session_status → "in_progress"` gives
  `defects == []` and `binding["layer"] == "product"`.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement `capacity_readout.py`**

```python
"""Build the capacity readout (spec 2026-09-19 §5) from injected seams. D6: the
binding constraint IS decide_implementation's first refused code, never a second
vocabulary; D7: a claim whose session is terminal is a defect, not load."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from server.db.models import INACTIVE_SESSION_STATUSES

from .backlog_runner import in_flight_by_product, is_in_flight
from .capacity_plan import (AccountHeadroom, CapacityInputs, HostCapacity, ProductCapacity,
                            RESERVE_PCT, product_order)
from .product_policy import effective_policy, is_on

LAYER_OF_CODE = {"product_capacity_full": "product", "global_capacity_full": "runner",
                 "daily_budget_full": "runner", "dispatch_off": "product",
                 "approval_required": "product", "runner_off": "runner"}


@dataclass
class CapacitySources:
    global_cap: Callable[[], Awaitable[int]]
    global_active: Callable[[], int]
    hosts: Callable[[], Awaitable[list[dict]]]
    accounts: Callable[[float], Awaitable[list[dict]]]
    runner_config: Callable[[], Awaitable[tuple[str, dict]]]
    target_states: Callable[[], Awaitable[dict[str, dict]]]
    candidates: Callable[[], Awaitable[tuple[int, int, int, dict | None]]]
    products: Callable[[], Awaitable[list[dict]]]
    decide: Callable[[str, str], Awaitable[tuple[Any, dict]]]
    session_status: Callable[[str], Awaitable[str | None]]
    plan: Callable[[], Awaitable[dict | None]]
    governor_config: Callable[[], Awaitable[tuple[str, dict]]]
    kill_switch: Callable[[], bool]


async def build_readout(src: CapacitySources) -> dict:
    mode, gov = await src.governor_config()
    push_level = gov.get("push_level", "steady")
    reserve = RESERVE_PCT.get(push_level, 20)
    global_cap = await src.global_cap()
    global_active = src.global_active()
    hosts = [{"name": h["name"], "enabled": bool(h.get("enabled")), "healthy": h.get("healthy"),
              "active": int(h.get("activeSessions") or 0), "max": h.get("maxSessions"),
              "free": (None if h.get("maxSessions") is None
                       else max(0, int(h["maxSessions"]) - int(h.get("activeSessions") or 0)))}
             for h in await src.hosts()]
    account_rows = await src.accounts(reserve)
    resets = sorted(r["nextResetAt"] for r in account_rows if r.get("nextResetAt"))
    _runner_mode, runner_cfg = await src.runner_config()
    states = await src.target_states()
    in_flight_total = sum(1 for s in states.values() if is_in_flight(s))
    by_product = in_flight_by_product(states)
    _eligible, _blocked, _untriaged, per_product = await src.candidates()
    per_product = per_product or {}

    defects: list[dict] = []
    for target, st in states.items():
        sid = st.get("session_id")
        if not is_in_flight(st) or not st.get("ownership_token") or not sid:
            continue
        status = await src.session_status(sid)
        if status in INACTIVE_SESSION_STATUSES:
            defects.append({"product": st.get("product"), "target": target, "sessionId": sid,
                            "sessionStatus": status,
                            "text": f"{target}: claim held by {status} session {sid[:8]} — a defect, not load"})
    defect_products = {d["product"] for d in defects}

    products: list[dict] = []
    for p in await src.products():
        slug = p["slug"]
        policy = effective_policy(p.get("policy"))
        counts = per_product.get(slug) or {}
        constraint, waiting = None, None
        if p.get("primaryRepo") and is_on(policy):
            decision, _pol = await src.decide(p["primaryRepo"], slug)
            refused = [c for c in decision.constraints if c.state != "permitted"]
            if refused:
                constraint, waiting = refused[0].code, refused[0].text
        products.append({
            "slug": slug, "name": p.get("name"), "rank": p.get("priorityRank"),
            "createdAt": p.get("createdAt"), "dispatch": policy["dispatch"],
            "capacityMode": policy["capacity_mode"], "pin": policy["max_in_flight"],
            "effectiveCap": policy["max_in_flight"], "inFlight": by_product.get(slug, 0),
            "eligible": int(counts.get("eligible") or 0), "constraint": constraint,
            "waitingOn": waiting, "defect": slug in defect_products,
        })
    ordered = product_order([ProductCapacity(
        slug=r["slug"], rank=r["rank"], created_at=r["createdAt"] or "", dispatch=r["dispatch"],
        capacity_mode=r["capacityMode"], pin=r["pin"], in_flight=r["inFlight"], eligible=r["eligible"])
        for r in products])
    order = [p.slug for p in ordered]
    products.sort(key=lambda r: order.index(r["slug"]))

    binding = None
    for r in products:
        if r["dispatch"] == "off" or r["eligible"] == 0:
            continue
        if r["defect"]:
            d = next(d for d in defects if d["product"] == r["slug"])
            binding = {"layer": "defect", "code": "capacity_defect", "text": d["text"]}
            break
        if r["constraint"]:
            binding = {"layer": LAYER_OF_CODE.get(r["constraint"], "product"),
                       "code": r["constraint"], "text": f"{r['slug']}: {r['waitingOn']}"}
            break
    plan = await src.plan()
    return {
        "computedAt": datetime.now(timezone.utc).isoformat(), "pushLevel": push_level,
        "governorMode": mode, "killSwitch": src.kill_switch(),
        "global": {"cap": global_cap, "active": global_active, "free": max(0, global_cap - global_active)},
        "hosts": hosts,
        "accounts": {"available": sum(1 for r in account_rows if r["available"]),
                     "total": len(account_rows), "reservePct": reserve,
                     "nextResetAt": resets[0] if resets else None, "rows": account_rows},
        "runner": {"ceiling": int(runner_cfg.get("max_concurrent") or 0), "inFlight": in_flight_total,
                   "derived": (plan or {}).get("fleetConcurrency")},
        "products": products, "binding": binding, "defects": defects, "plan": plan,
    }


def readout_to_inputs(r: dict) -> CapacityInputs:
    return CapacityInputs(
        global_cap=r["global"]["cap"], global_active=r["global"]["active"],
        hosts=tuple(HostCapacity(h["name"], h["enabled"], h["healthy"], h["active"], h["max"]) for h in r["hosts"]),
        accounts=tuple(AccountHeadroom(a["name"], a["engine"], a["metered"], a["available"],
                                       a.get("reason"), a.get("nextResetAt")) for a in r["accounts"]["rows"]),
        runner_ceiling=r["runner"]["ceiling"], runner_in_flight=r["runner"]["inFlight"],
        products=tuple(ProductCapacity(p["slug"], p["rank"], p["createdAt"] or "", p["dispatch"],
                                       p["capacityMode"], p["pin"], p["inFlight"], p["eligible"])
                       for p in r["products"]),
    )
```

- [ ] **Step 4: Implement `capacity_wiring.py`** — `def sources_for(orch) -> CapacitySources` using:
  `global_cap=orch._max_active_sessions`, `global_active=orch.active_session_count`,
  `hosts` = async: `async with db_session() as db: rows = (await db.execute(select(Host).order_by(Host.name))).scalars().all(); return [host_to_wire(r) for r in rows]` (import `host_to_wire` from `server.api.fleet`),
  `accounts=lambda reserve: account_headroom(orch, reserve_pct=reserve)` (import from `server.api.accounts`),
  `runner_config=orch._backlog_read_config`, `target_states=orch._backlog_ctx.list_target_states`,
  `candidates=orch._backlog_count_candidates`,
  `products` = async: select `Project` joined with its primary `ProjectRepo` → dicts with `policy=p.automation_policy`, `priorityRank=p.priority_rank`, `createdAt=p.created_at.isoformat()`,
  `decide` = async `(repo, slug)`: `snap = await gather_implementation_snapshot([repo], requested_product=slug, context=orch._backlog_ctx); return snap.decide(repo, requested_product=slug, trigger="automatic", effect="dispatch")`,
  `session_status` = async: `select(DbSession.status).where(DbSession.id == sid)` scalar,
  `plan=read_plan`, `governor_config=orch._capacity_governor_read_config` (Task 7; until then a lambda returning `("off", {})` is acceptable in the wiring with a TODO-free comment "replaced in Task 7"), `kill_switch=lambda: get_settings().capacity_governor_enabled` (Task 7 adds the setting; use `getattr(get_settings(), "capacity_governor_enabled", True)` so this task is green on its own).
- [ ] **Step 5: Orchestrator + route** — in `orchestrator.py` add
  `async def capacity_readout(self) -> dict: from server.automations.capacity_wiring import sources_for; from server.automations.capacity_readout import build_readout; return await build_readout(sources_for(self))`.
  `server/api/capacity.py`:

```python
"""GET /api/capacity — the ceiling stack, one readout (spec 2026-09-19 §5)."""
from fastapi import APIRouter, Depends, HTTPException, Request

from server.auth.identity import current_identity

router = APIRouter(prefix="/api/capacity", tags=["capacity"])


@router.get("")
async def get_capacity(request: Request, _identity: str = Depends(current_identity)) -> dict:
    orch = getattr(request.app.state, "sessions", None)
    if orch is None or not hasattr(orch, "capacity_readout"):
        raise HTTPException(status_code=503, detail="capacity readout unavailable")
    return await orch.capacity_readout()
```

  (Copy the exact `current_identity` import path from `server/api/automations.py`.) Register in
  `server/app.py` beside `automations_router`. Capture `app.state.sessions` lazily (as above) — the
  fleet orchestrator swap happens after routers are built.
- [ ] **Step 6: API smoke test** `server/tests/test_capacity_api.py`: with `_client()` from
  `test_pm_api`, `GET /api/capacity` returns 200 with keys `global, hosts, accounts, runner, products, binding, defects, plan`.
- [ ] **Step 7: Run** both tests + `mypy server/ --ignore-missing-imports` + ratchet → PASS.
- [ ] **Step 8: Commit** — `feat(capacity): readout builder and GET /api/capacity`.

---

### Task 7: the governor automation, loop, wiring, kill switch

**Files:**
- Create: `server/automations/capacity_governor.py`
- Modify: `server/automations/registry.py` (import + `registry.register(CapacityGovernor(), oversight=…)` before `validate_oversight`)
- Modify: `server/sessions/orchestrator.py` (`_PRIMARY_ONLY_LOOP_ATTRS` + `"_capacity_governor_task"`; ctx/loop construction next to the control-band block; task start in the primary-only block; `automation_statuses` ctx map; `_capacity_governor_read_config`, `_capacity_governor_tick_loop`, `_capacity_governor_tick_once`; the stop-list at ~line 2680 that names `_control_band_task`)
- Modify: `server/config.py` (`capacity_governor_enabled: bool = Field(default=True, alias="VIBERPIT_CAPACITY_GOVERNOR_ENABLED")` next to `gate_autoresolve_enabled`)
- Modify: `server/api/settings.py` (`_MANAGED` gains `("capacity_governor_enabled", "VIBERPIT_CAPACITY_GOVERNOR_ENABLED", "Capacity governor may apply its plan (act mode)")` and `("test_slot_cap", "CCSWITCH_TEST_SLOT_CAP", "Full test-suite runs allowed at once per host")`)
- Modify: `server/automations/capacity_wiring.py` (real `governor_config` + `kill_switch`)
- Test: `server/tests/test_capacity_governor_loop.py`; update `server/tests/test_settings.py::test_get_settings_managed_is_an_allowlist_with_no_secrets` if it pins the key list.

**Interfaces:**
- Produces: automation id `capacity-governor`, modes `("off","monitor","act")`, config
  `push_level` (enum, default `steady`), `sessions_per_account` (int, default 2, min 1);
  `CapacityGovernorLoop(ctx, *, read_config, read_readout, read_plan, write_plan, kill_switch)`
  with `async tick()`; events `cap_changed` (target = slug), `capacity_defect` (target = claim target, warn, once per target while it persists), `plan_stale` (once per failure streak);
  `publish_observation(ctx, "capacity_counts", {...})` feeding `status()`.

- [ ] **Step 1: Failing loop tests** (`server/tests/test_capacity_governor_loop.py`, `FakeCtx` copied from
  `test_control_band_monitor.py`): a `_readout()` factory returning the Task 6 test readout with two
  products; tests:
  1. `test_monitor_writes_plan_unapplied`: `read_config → ("monitor", {"push_level":"steady","sessions_per_account":2})`, write_plan records `applied is False` and `perProduct` present.
  2. `test_act_with_kill_switch_applies`: mode `act`, kill switch True → `applied is True`; kill switch False → `applied is False`.
  3. `test_off_writes_nothing`.
  4. `test_cap_changed_emitted_once_per_confirmed_change`: previous plan `{"perProduct": {"viberpit": 1}, "pending": {"viberpit": 2}}` and a readout that proposes 2 → one `cap_changed` event with target `viberpit`; a second tick with the same readout emits none.
  5. `test_readout_failure_keeps_previous_plan_and_emits_plan_stale_once`: `read_readout` raises twice → one `plan_stale` event, `write_plan` never called.
  6. `test_defect_event_once_per_target`: readout with one defect → one `capacity_defect` (level warn) across two ticks.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement `capacity_governor.py`**

```python
"""Capacity Governor — derives runner concurrency and per-product in-flight caps
from real headroom (spec 2026-09-19 §6). Two objects, the control-band mold:
``CapacityGovernor`` (the Automation surface) and ``CapacityGovernorLoop`` (the
supervised loop, every I/O injected). Phase 1: monitor computes and shows; act is
wired but the runner does not read the plan until phase 2."""
from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from .base import AutomationStatus, ConfigField
from .capacity_plan import PUSH_LEVELS, plan_capacity, plan_from_dict
from .capacity_readout import readout_to_inputs
from .context import observation_failed, publish_observation
from .engine_policy import EnginePolicy

log = logging.getLogger(__name__)


class CapacityGovernor:
    id = "capacity-governor"
    name = "Capacity Governor"
    work_class = "maintenance"
    description = ("Reads account quota, host capacity and the runner's own ledger every tick, "
                   "and derives how many sessions the fleet and each product should run. "
                   "monitor shows the plan; act applies it (phase 2).")
    modes: tuple[str, ...] = ("off", "monitor", "act")
    engine_policy: EnginePolicy = EnginePolicy.CLAUDE_ONLY  # spawns nothing; fail closed
    config_schema = [
        ConfigField("push_level", "Push level", "enum", "steady", choices=list(PUSH_LEVELS),
                    help="How much weekly quota to keep in reserve: conserve 40%, steady 20%, max 5%."),
        ConfigField("sessions_per_account", "Sessions per account", "int", 2, min=1,
                    help="Concurrent runner sessions one account can carry before its quota "
                         "burns faster than it resets."),
    ]

    def status(self, ctx: Any) -> AutomationStatus:
        counts = getattr(ctx, "capacity_counts", None) if ctx else None
        if not counts:
            return AutomationStatus(text="idle", level="idle")
        text = counts.get("binding_text") or f"fleet {counts.get('fleet', 0)} · no binding constraint"
        level = "warn" if counts.get("defects") else "active"
        return AutomationStatus(text=text, level=level,
                                counts={k: v for k, v in counts.items() if isinstance(v, int)})

    async def on_state_change(self, *, enabled: bool, mode: str | None,
                              config: dict, ctx: Any) -> None:
        return None


ReadConfig = Callable[[], Awaitable[tuple[str, dict]]]
ReadReadout = Callable[[], Awaitable[dict]]
ReadPlan = Callable[[], Awaitable[dict | None]]
WritePlan = Callable[[dict], Awaitable[None]]


class CapacityGovernorLoop:
    def __init__(self, ctx: Any, *, read_config: ReadConfig, read_readout: ReadReadout,
                 read_plan: ReadPlan, write_plan: WritePlan, kill_switch: Callable[[], bool]) -> None:
        self._ctx = ctx
        self._read_config = read_config
        self._read_readout = read_readout
        self._read_plan = read_plan
        self._write_plan = write_plan
        self._kill_switch = kill_switch
        self._stale_emitted = False
        self._defects_emitted: set[str] = set()

    async def tick(self) -> None:
        try:
            await self._run()
        except Exception:  # pragma: no cover — mirrors the mold
            observation_failed(self._ctx)
            log.warning("capacity_governor.tick_error", exc_info=True)

    async def _run(self) -> None:
        mode, config = await self._read_config()
        if mode == "off":
            return
        try:
            readout = await self._read_readout()
        except Exception as exc:
            observation_failed(self._ctx)
            if not self._stale_emitted:
                self._stale_emitted = True
                await self._ctx.emit("warn", f"capacity readout failed: {exc}; keeping the previous plan",
                                     kind="plan_stale")
            return
        self._stale_emitted = False
        previous = plan_from_dict(await self._read_plan())
        plan = plan_capacity(readout_to_inputs(readout), push_level=config.get("push_level", "steady"),
                             sessions_per_account=int(config.get("sessions_per_account") or 2),
                             previous=previous)
        applied = mode == "act" and bool(self._kill_switch())
        await self._write_plan({
            "pushLevel": plan.push_level, "applied": applied,
            "fleetConcurrency": plan.fleet_concurrency, "perProduct": plan.per_product,
            "pending": plan.pending, "budget": {**plan.budget, "limiting": plan.limiting},
            "binding": readout.get("binding"), "reasons": plan.reasons, "defects": readout.get("defects", []),
        })
        prev_caps = previous.per_product if previous else {}
        for slug, cap in plan.per_product.items():
            if slug in prev_caps and prev_caps[slug] != cap:
                reason = next((r for r in plan.reasons if r.startswith(f"{slug}:")), "")
                await self._ctx.emit("info", f"{slug} in-flight cap {prev_caps[slug]} → {cap}"
                                     + (f" ({'applied' if applied else 'monitor only'}): {reason}"),
                                     kind="cap_changed", target=slug)
        current = {d["target"] for d in readout.get("defects", [])}
        for d in readout.get("defects", []):
            if d["target"] not in self._defects_emitted:
                await self._ctx.emit("warn", d["text"], kind="capacity_defect", target=d["target"],
                                     detail={"sessionId": d.get("sessionId"), "product": d.get("product")})
        self._defects_emitted = current
        binding = readout.get("binding") or {}
        publish_observation(self._ctx, "capacity_counts", {
            "fleet": plan.fleet_concurrency, "defects": len(current),
            "accounts_available": readout["accounts"]["available"],
            "products": len(plan.per_product), "binding_text": binding.get("text"),
        })
```

- [ ] **Step 4: Registry** — after `DeployMonitor` import add `from .capacity_governor import CapacityGovernor  # noqa: E402`; before `registry.validate_oversight(require_all=True)`:

```python
registry.register(CapacityGovernor(), oversight=AutomationOversight(
    Responsibility.EXECUTION, 'Fleet concurrency and per-product in-flight caps, derived from account quota, host capacity and the runner ledger.',
    {
        'off': 'Disable capacity planning; every cap stays as configured.',
        'monitor': 'Compute the plan every tick and show it on Delivery → Capacity; apply nothing.',
        'act': 'Compute and apply the plan to auto-mode products and the runner ceiling (phase 2); pinned caps are respected.',
    },
    (Relationship('uses', 'pm-sync', 'Reads mirrored eligible counts per product.'),
     Relationship('shares_work_with', 'backlog-runner', 'Sets what the runner may claim per product.')),
))
```

- [ ] **Step 5: Orchestrator wiring** — mirror the control-band block exactly: `self._capacity_governor_ctx = AutomationContext("capacity-governor")`;
  `self._capacity_governor_loop = CapacityGovernorLoop(self._capacity_governor_ctx, read_config=self._capacity_governor_read_config, read_readout=self.capacity_readout, read_plan=read_plan, write_plan=write_plan, kill_switch=lambda: get_settings().capacity_governor_enabled)`;
  `self._capacity_governor_task: asyncio.Task | None = None`; add `"_capacity_governor_task"` to `_PRIMARY_ONLY_LOOP_ATTRS` and to the stop list near line 2680; start it in the primary-only block with `asyncio.create_task(self._capacity_governor_tick_loop(interval=60.0), name="capacity-governor-loop")`;
  `_capacity_governor_read_config` = `read_automation_config(CapacityGovernor, modes=("monitor", "act"), log_key="capacity_governor")`;
  `_capacity_governor_tick_loop`/`_tick_once` copied from `_control_band_tick_loop`/`_tick_once` with the automation id swapped; add `"capacity-governor": getattr(self, "_capacity_governor_ctx", None)` to the `ctxs` map in `automation_statuses`.
  In `capacity_wiring.sources_for` set `governor_config=orch._capacity_governor_read_config`.
- [ ] **Step 6: Settings + managed** as listed in Files.
- [ ] **Step 7: Run** `pytest server/tests/test_capacity_governor_loop.py server/tests/test_primary_lease.py server/tests/test_settings.py server/tests/test_automations_api.py -q`, then `ruff check src/ server/`, `mypy server/ --ignore-missing-imports`, ratchet → PASS.
- [ ] **Step 8: Commit** — `feat(capacity): capacity-governor automation (monitor mode) + kill switch`.

---

### Task 8: web — Capacity scope under Delivery

**Files:**
- Create: `web/src/api/capacity.ts` (types + `fetchCapacity`)
- Modify: `web/src/api/hooks.ts` (`QK.capacity`, `useCapacity`)
- Modify: `web/src/features/products/vocab.ts` (`DELIVERY.capacity: 'Capacity'`, `capacityError: 'Could not load capacity.'`)
- Modify: `web/src/features/delivery/deliveryNavState.ts` (`DeliveryScope` + `DELIVERY_SCOPES` gain `'capacity'`)
- Modify: `web/src/features/delivery/DeliveryScopeSegment.tsx` (third `ScopeLink`, count = binding ? 1 : 0)
- Modify: `web/src/features/delivery/DeliveryListColumn.tsx`, `useDeliveryWorkspace.ts` (pass `capacity`)
- Create: `web/src/features/delivery/capacity/CapacityScopeBody.tsx` (list column: the five stack rows)
- Create: `web/src/features/delivery/capacity/CapacityDetail.tsx` (detail pane)
- Modify: `web/src/features/delivery/DeliveryPage.tsx` (scope `capacity` + `item === null` → `<CapacityDetail />`)
- Modify: `web/src/features/delivery/DeliveryWelcome.tsx` (no change needed if the page short-circuits; keep types compiling)
- Test: `web/src/features/delivery/capacity/CapacityDetail.test.tsx`, `deliveryNavState.test.ts`; update every test that mocks `@/api/hooks` and renders `DeliveryPage`/`DeliveryListColumn`/`useDeliveryWorkspace` with `useCapacity: () => ({ data: CAPACITY, isLoading: false, isError: false })`.

**Interfaces:**
- Produces: `interface CapacityReadout` mirroring §5 (camelCase as the server emits); `useCapacity({ poll, enabled })`
  → `useQuery<CapacityReadout>({ queryKey: QK.capacity, queryFn: fetchCapacity, staleTime: 10_000, refetchInterval: poll ? 30_000 : false, enabled })`.

- [ ] **Step 1: Failing test** `CapacityDetail.test.tsx` (mock `@/api/hooks`: `useCapacity`, `usePatchAutomation`, `useAutomations` (with a `capacity-governor` row: modes `['off','monitor','act']`, mode `'monitor'`), `useUpdateProject`, `useUpdateProductPolicy`): asserts
  - the binding banner text renders (`screen.getByRole('alert')` or the Banner's title);
  - the products table lists rows in rank order with `pinned`/`auto` badges and a "defect" badge on a defect row linking to `/sessions/<id>`;
  - clicking "Move up" on the second row calls `useUpdateProject(slug).mutate({ priorityRank: 1 })`;
  - the push-level radio calls `patch.mutate({ id: 'capacity-governor', body: { config: { push_level: 'max' } } })`;
  - the plan card lists `plan.reasons`.
- [ ] **Step 2: Run** `(cd web && npx vitest run src/features/delivery/capacity)` → FAIL.
- [ ] **Step 3: Implement.** `CapacityDetail` layout (top to bottom): `RadioGroup` "Push level"
  (`name="push-level"`, options conserve/steady/max with the reserve % in `description`); governor mode
  `Select` (reuse the pattern in `AutomationControls`, `usePatchAutomation`); the stack as five
  `<li>` rows `label · used / cap · free`, the binding row wrapped in `<Banner tone="warning" title={binding.text}>`,
  each row with a `<Link>` to its owning page (`/settings/general`, `/settings/fleet`, `/settings/accounts`,
  `automationPath('backlog-runner') + '?tab=config'`); products `<table>` with columns Rank (▲ ▼ `IconButton`s
  calling `useUpdateProject(slug).mutate({ priorityRank })` with rank swap semantics: up = this row takes the
  rank above and the neighbour takes this row's), Product, Dispatch, Cap mode (`Switch` → `useUpdateProductPolicy(slug).mutate({ policy: { capacity_mode }, expectedUpdatedAt: null })`
  — pass the project's `automationPolicyUpdatedAt` when the page has it; the readout does not carry it, so
  `CapacityDetail` reads `useProject(slug)` lazily only for the row being edited), Pin (number input +
  Save button, only enabled when pinned), Effective cap, In flight, Eligible, Waiting on; the plan card.
  `CapacityScopeBody` renders the same five stack rows as list rows (no controls). Keep every string in
  `DELIVERY` vocab or the component; no new `localStorage` keys.
- [ ] **Step 4: Run** `(cd web && npm run typecheck && npm run lint && npm run test && npm run build)` → PASS (run the FULL vitest suite; ~47 files mock `@/api/hooks`).
- [ ] **Step 5: Commit** — `feat(web): Delivery → Capacity readout, push level, rank and pin controls`.

---

### Task 9: findability (spec D9)

**Files:**
- Modify: `web/src/features/roadmap/detail/AutomationPolicyCard.tsx` — replace the flat `fields.map` with three
  `<fieldset>` groups: **Capacity** (`dispatch, capacity_mode, max_in_flight, weight, queue_min_status, respect_dependencies`)
  with a `<Link to="/delivery?scope=capacity">See the fleet's capacity</Link>` under its legend, **Models**
  (`default_model, ladder_top, unattended_model_floor`), **Gates** (`risk_tolerance, triage_auto_accept, give_up_after_bounces, max_initiatives_in_flight`);
  unknown keys fall into Capacity so a new schema field never disappears.
- Modify: `web/src/features/delivery/DeliveryPage.tsx` — in the header meta, when `automation.id === 'backlog-runner'` and
  (`automation.status.waitingReason === 'capacity'` or `automation.status.text.includes('capacity')`), render `<Link to="/delivery?scope=capacity">See capacity</Link>`.
- Modify: `web/src/features/settings/sections/FleetSection.tsx` — add a `maxSessions` number input column
  (same on-blur pattern as `weight`, `min={1}`, blank → `null`, `aria-label={`max sessions for ${h.name}`}`,
  `patch.mutate({ name, body: { maxSessions } })`); change the weight input's `aria-label` to `weight (dispatch share) for ${h.name}` and the column header to "Weight (dispatch share)".
- Modify: `server/automations/product_policy.py` — `weight` label → `"Weight (contested slot share)"`.
- Tests: extend `AutomationPolicyCard.test.tsx` (group legends present, link present), `FleetSection.test.tsx` (max sessions blur patches), `DeliveryPage.test.tsx` (link shown when waiting on capacity).

- [ ] Steps: write the three failing assertions → run → implement → `npm run test` full → commit
  `feat(web): capacity findability — grouped policy card, runner link, editable host max sessions`.

---

### Task 10: feature doc, deploy enablement, verification

**Files:**
- Create: `docs/product/features/capacity-governor.md` with frontmatter `slug: capacity-governor`,
  `parent: planning-and-automation`, `title: Capacity Governor`, `state: live`, `links.code` listing the
  five new server modules and `web/src/features/delivery/capacity/`, and sections Problem / Target users /
  Desired outcome / Notes (push level, rank, pins, monitor vs act, kill switch) / Evidence (spec path).
- Modify: `docs/superpowers/specs/2026-09-19-capacity-governor-design.md` — status line gains
  "phase 1 landed in PR #<n>".

- [ ] **Step 1:** `pytest server/tests/test_pm_models.py -q` (feature-doc parse) → PASS.
- [ ] **Step 2:** Full gate: `ruff check src/ server/`, `mypy server/ --ignore-missing-imports`, `pytest tests/ -q`,
  `pytest server/tests/ -n auto --dist loadfile --timeout=300 -q` (acquire a test slot first per CLAUDE.md), the four web scripts, `python scripts/brand_ratchet.py`.
- [ ] **Step 3:** Push, open the PR (body cites spec §10 item 1 and `Refs #2009, #1891, #1517`), `gh pr merge --squash --auto`.
- [ ] **Step 4 (after deploy):** `PATCH /api/automations/capacity-governor {"enabled": true, "mode": "monitor"}`
  over loopback on dobby; confirm `GET /api/capacity` renders a plan within two ticks and the Delivery → Capacity
  page shows the binding constraint.

## Self-review

- Spec coverage: §4 (Tasks 1–3, 7 config), §5 (Task 6), §6.1–6.2 (Tasks 4, 7), §6.3 apply seams and §6.4 kill-switch
  behaviour are phase 2 except the switch itself (Task 7), §7.1 (Task 8), §7.2 (Task 9, plus `_MANAGED` in Task 7),
  §9 tests (each task), §10 item 1 (Task 10). §7.3 search is phase 3 by design.
- Type consistency: `CapacityInputs`/`CapacityPlan`/`plan_from_dict` names match between Tasks 4, 6, 7;
  readout keys (`accounts.rows[].available`, `products[].capacityMode`) match `readout_to_inputs`.

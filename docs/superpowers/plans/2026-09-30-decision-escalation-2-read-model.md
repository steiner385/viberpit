# Decision escalation PR 2 — escalation read model, `GET /api/pm/holds`, decisions per product — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** One pure read model answers "what is held, per product, what does each hold block, and is the product starving", served by a new `GET /api/pm/holds` and folded into `GET /api/pm/decisions` (now filterable by product).

**Architecture:** `server/pm/escalation.py` is pure: it takes the `QueueRow`s `queue_store.load_queue_rows` already builds (extended with the hold's reason/actor/time), the pending `DecisionProposal`s, and a product policy, and returns `HeldItem`s ranked by transitive downstream impact plus a `ProductEscalation` summary. The reverse dependency index is `server/pm/drift.py:reverse_blockers` (PR 6). Two routes in `server/api/pm.py` read the model through one shared loader helper. No new table.

**Tech Stack:** Python 3 / FastAPI / SQLAlchemy async; pytest (pure tests + TestClient API tests).

**Spec:** `docs/superpowers/specs/2026-09-30-decision-escalation-design.md` §3, §4.2, §4.3, §10 PR 2.

**Depends on:** PR 6 (`drift.reverse_blockers`) merged into `origin/master` — check `grep -n "def reverse_blockers" server/pm/drift.py` on the branch base before Task 2. PR 1 is not required.

## Global Constraints

- Branch off `origin/master` (`git fetch && git checkout -b feat/escalation-read-model origin/master`).
- Test env and gates exactly as in plan 1 (`env -u …` + `/home/tony/GitHub/viberpit/.venv/bin/python`; ruff, mypy, `pytest tests/`, touched files, full `-m "not slow"` in the background; commit/PR text via files; trailers).
- `HELD_KINDS` (`server/pm/desired_state.py:37`) is the only definition of "parked"; `SKIP_REASONS` the only list of reasons; `skip_reason` the only eligibility rule — the read model calls them, never re-derives them.
- Every datetime that reaches the wire is aware UTC (`_aware` from `server/pm/reconcile_inputs.py`, as `queue_store` uses). No `func.now()`, no absolute date literals in tests.
- Wire keys are camelCase (`holdKind`, `downstreamDispatchable`), matching `_decision_to_dict` and `/pm/leaves`.
- Query params on `/api/pm/holds` are snake_case like `/pm/queue`'s (`min_status`, `respect_dependencies`); `/pm/decisions` gains `projectSlug` because `/pm/triage` already spells it that way.
- A GET route the assistant may reach needs an allow row in `server/assistant/policy.py` and a `True` row in `server/tests/test_assistant_policy.py`.

## Review Focus

1. A dependency cycle on the board (`#1 blocks #2 blocks #1`) must terminate and count each ref once — a runaway BFS would 500 the holds endpoint for the whole fleet. Pinned in Task 2.
2. A `decision` hold whose proposal row is missing (item un-mirrored and re-mirrored) must list with `proposalId: null`, not crash the join. Pinned in Task 3.
3. A product whose dispatch is `off` is never `starving`, even with zero dispatchable and many holds — an off product is not waiting on anyone. Pinned in Task 2.
4. `downstreamDispatchable` must not count a dependent that is ALSO `assigned`/`claimed`/`held` in its own right — releasing this hold would not free it. Pinned in Task 2.
5. `GET /api/pm/decisions?impact=true` must still answer for a proposal whose item is on no board (no `QueueRow`): `downstream: 0`, not a KeyError. Pinned in Task 4.

---

### Task 1: `QueueRow` carries the hold's reason, actor and time

**Files:**
- Modify: `server/pm/queue.py:60-92` (`QueueRow`)
- Modify: `server/pm/queue_store.py:132-146` (row construction)
- Test: `server/tests/test_pm_queue.py`, `server/tests/test_backlog_query_queue.py`

**Interfaces:**
- Produces: `QueueRow.hold_reason: str | None = None`, `QueueRow.hold_by: str | None = None`, `QueueRow.hold_set_at: datetime | None = None` (aware UTC). Task 2 reads all three.

- [ ] **Step 1: Write the failing test**

Append to `server/tests/test_backlog_query_queue.py` (reuse `_init`, `_seed_product`, `_seed_item`):

```python
def test_load_queue_rows_carries_the_hold_provenance(tmp_path):
    """Spec 2026-09-30 §3: the read model needs who parked the item, why and when,
    so the loader threads the three hold columns beside ``hold_kind``."""
    async def run():
        await _init(tmp_path, "holdprov.db")
        await _seed_product("viberpit", "o/r")
        await _seed_item("o/r", 1, score=1.0, hold_kind="owner_action")
        from datetime import datetime, timezone

        from server.db import db_session
        from server.pm.queue_store import load_queue_rows

        async with db_session() as db:
            rows = await db.run_sync(lambda s: load_queue_rows(
                s, now=datetime.now(timezone.utc), claimed_refs=set()))
        (row,) = rows
        assert row.hold_kind == "owner_action"
        assert row.hold_by == "tony@example.com"
        assert row.hold_reason == "max attempts"
        # ``_seed_item`` does not stamp hold_set_at; the field must still exist.
        assert row.hold_set_at is None

    asyncio.run(run())
```

- [ ] **Step 2: Run** — `pytest server/tests/test_backlog_query_queue.py -k hold_provenance -v`; Expected: FAIL `AttributeError: 'QueueRow' object has no attribute 'hold_by'`.

- [ ] **Step 3: Implement**

`server/pm/queue.py`, after `hold_kind` in `QueueRow`:

```python
    # Spec 2026-09-30 §3: the hold's provenance, threaded for the escalation read
    # model (``server/pm/escalation.py``) so it can say who parked the item, why
    # and for how long. Defaulted so every positional construction keeps working.
    hold_reason: str | None = None
    hold_by: str | None = None
    hold_set_at: datetime | None = None
```

Place them BEFORE `bounces` only if `bounces` is keyword-only-safe in every constructor — it is defaulted too, but existing tests construct `QueueRow(**base)` by keyword, and `rank()` builds `QueueEntry` positionally (not `QueueRow`), so append them AFTER `bounces` to be safe.

`server/pm/queue_store.py` row construction:

```python
                hold_kind=w.hold_kind,
                bounces=bounces.get(ref, 0),
                hold_reason=w.hold_reason, hold_by=w.hold_by,
                hold_set_at=_aware(w.hold_set_at),
```

- [ ] **Step 4: Run** — `pytest server/tests/test_pm_queue.py server/tests/test_backlog_query_queue.py server/tests/test_pm_queue_api.py -q`; Expected: PASS.
- [ ] **Step 5: Commit** — `feat(pm): QueueRow carries the hold's reason, actor and time`.

---

### Task 2: `server/pm/escalation.py` — the pure read model

**Files:**
- Create: `server/pm/escalation.py`
- Test: `server/tests/test_pm_escalation.py` (new)

**Interfaces:**
- Consumes: `QueueRow` (Task 1), `skip_reason` (`server/pm/queue.py`), `HELD_KINDS`, `drift.reverse_blockers` + `DepEdgeView`, `product_policy.is_on`.
- Produces:

```python
@dataclass(frozen=True)
class HeldItem:
    ref: str; repo: str; number: int; product: str; title: str; url: str
    hold_kind: str; hold_reason: str; hold_by: str; hold_set_at: datetime | None
    age_hours: float | None
    proposal_id: str | None; options: tuple[dict, ...]; recommended_key: str | None
    downstream: int; downstream_dispatchable: int; downstream_refs: tuple[str, ...]
    dispatchable_on_release: bool
    released_by: str

@dataclass(frozen=True)
class ProductEscalation:
    slug: str; dispatchable: int; skipped: dict[str, int]; held: tuple[HeldItem, ...]
    starving: bool; oldest_hold_hours: float | None

RELEASED_BY: dict[str, str]          # hold kind -> copy
def reverse_index(rows) -> dict[str, set[str]]
def downstream(ref, reverse) -> tuple[str, ...]
def held_items(rows, proposals_by_ref, *, now, min_status, respect_dependencies,
               give_up_after_bounces=0, extra_skip_labels=frozenset()) -> list[HeldItem]
def rank_held(items) -> list[HeldItem]
def product_escalation(slug, rows, proposals_by_ref, policy, *, now, starving_max=1,
                       min_status=None, respect_dependencies=None) -> ProductEscalation
```

`proposals_by_ref: Mapping[str, DecisionProposal]` — pending proposals keyed by `owner/repo#N`. `policy` is an effective policy dict (`product_policy.effective_policy`).

- [ ] **Step 1: Write the failing tests**

`server/tests/test_pm_escalation.py`:

```python
"""Pure tests for the escalation read model (spec 2026-09-30 §3)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from server.pm.escalation import (HeldItem, downstream, held_items, product_escalation,
                                  rank_held, reverse_index)
from server.pm.queue import QueueRow

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(days=1)  # relative to a fixed anchor


def _row(**over) -> QueueRow:
    base = dict(ref="o/r#1", repo="o/r", number=1, product="viberpit", title="t", body="",
                url="u", status="Ready", score=1.0, gh_created_at=NOW - timedelta(days=2),
                gh_updated_at=NOW, assignees=(), author="tony", labels=(), open_children=0,
                open_blockers=(), live_session=False, claimed=False, hold_kind=None)
    base.update(over)
    return QueueRow(**base)


def _held(ref, kind="owner_action", *, set_at=None, **over) -> QueueRow:
    number = int(ref.rsplit("#", 1)[1])
    return _row(ref=ref, number=number, hold_kind=kind, hold_reason="why",
                hold_by="tony@example.com", hold_set_at=set_at, **over)


def _policy(dispatch="auto"):
    return {"dispatch": dispatch, "queue_min_status": "Ready", "respect_dependencies": True,
            "give_up_after_bounces": 0, "skip_labels": []}


KW = dict(now=NOW, min_status="Ready", respect_dependencies=True)


def test_reverse_index_maps_blocker_to_dependents():
    rows = [_row(ref="o/r#2", number=2, open_blockers=("o/r#1",)),
            _row(ref="o/r#3", number=3, open_blockers=("o/r#1", "o/r#2"))]
    assert reverse_index(rows) == {"o/r#1": {"o/r#2", "o/r#3"}, "o/r#2": {"o/r#3"}}


def test_downstream_is_transitive_and_cycle_safe():
    rev = {"o/r#1": {"o/r#2"}, "o/r#2": {"o/r#3", "o/r#1"}, "o/r#3": {"o/r#1"}}
    assert downstream("o/r#1", rev) == ("o/r#2", "o/r#3")
    assert downstream("o/r#9", rev) == ()


def test_held_items_count_downstream_and_what_would_become_dispatchable():
    rows = [_held("o/r#1"),
            _row(ref="o/r#2", number=2, open_blockers=("o/r#1",)),                 # frees
            _row(ref="o/r#3", number=3, open_blockers=("o/r#2",)),                 # frees (chain)
            _row(ref="o/r#4", number=4, open_blockers=("o/r#1",), assignees=("bob",)),  # assigned
            _row(ref="o/r#5", number=5, open_blockers=("o/r#1", "o/r#8",)),        # still blocked by #8
            _row(ref="o/r#8", number=8, status="Blocked")]
    (item,) = held_items(rows, {}, **KW)
    assert item.ref == "o/r#1" and item.hold_kind == "owner_action"
    assert item.downstream == 4
    assert set(item.downstream_refs) == {"o/r#2", "o/r#3", "o/r#4", "o/r#5"}
    assert item.downstream_dispatchable == 2
    assert item.dispatchable_on_release is True
    assert item.released_by == "Retry, a board move to Ready/Backlog, or an issue-body edit"


def test_held_items_only_lists_parked_kinds_and_joins_the_pending_proposal():
    prop = SimpleNamespace(id="dp-1", options=[{"key": "a", "label": "A", "detail": None},
                                               {"key": "b", "label": "B", "detail": "d"}],
                           recommended_key="b")
    rows = [_held("o/r#1", "decision", set_at=NOW - timedelta(hours=30)),
            _held("o/r#2", "evidence"), _held("o/r#3", "stronger_model"),
            _held("o/r#4", "decision")]  # no proposal row: still listed
    items = {i.ref: i for i in held_items(rows, {"o/r#1": prop}, **KW)}
    assert set(items) == {"o/r#1", "o/r#4"}
    assert items["o/r#1"].proposal_id == "dp-1"
    assert items["o/r#1"].recommended_key == "b" and len(items["o/r#1"].options) == 2
    assert items["o/r#1"].age_hours == 30.0
    assert items["o/r#1"].released_by == "Accept an option in the Inbox"
    assert items["o/r#4"].proposal_id is None and items["o/r#4"].options == ()
    assert items["o/r#4"].age_hours is None


def test_dispatchable_on_release_respects_other_skip_reasons():
    (item,) = held_items([_held("o/r#1", assignees=("bob",))], {}, **KW)
    assert item.dispatchable_on_release is False


def test_rank_held_orders_by_downstream_then_age_then_ref():
    def item(ref, downstream, age):
        return HeldItem(ref=ref, repo="o/r", number=int(ref[-1]), product="p", title="t",
                        url="u", hold_kind="owner_action", hold_reason="r", hold_by="b",
                        hold_set_at=None, age_hours=age, proposal_id=None, options=(),
                        recommended_key=None, downstream=downstream,
                        downstream_dispatchable=0, downstream_refs=(),
                        dispatchable_on_release=True, released_by="x")
    ranked = rank_held([item("o/r#1", 0, 50.0), item("o/r#2", 3, 1.0),
                        item("o/r#3", 3, 9.0), item("o/r#4", 0, None)])
    assert [i.ref for i in ranked] == ["o/r#3", "o/r#2", "o/r#1", "o/r#4"]


def test_product_escalation_starving_truth_table():
    held = _held("o/r#1", set_at=NOW - timedelta(hours=100))
    free = _row(ref="o/r#2", number=2)
    on = product_escalation("p", [held], {}, _policy(), now=NOW)
    assert on.starving is True and on.dispatchable == 0 and on.skipped == {"held": 1}
    assert on.oldest_hold_hours == 100.0 and [i.ref for i in on.held] == ["o/r#1"]
    # one dispatchable still counts as starving at the default threshold …
    assert product_escalation("p", [held, free], {}, _policy(), now=NOW).starving is True
    # … two do not
    assert product_escalation("p", [held, free, _row(ref="o/r#3", number=3)], {},
                              _policy(), now=NOW).starving is False
    # nothing held: not starving however empty the queue
    assert product_escalation("p", [], {}, _policy(), now=NOW).starving is False
    # dispatch off: never starving
    assert product_escalation("p", [held], {}, _policy("off"), now=NOW).starving is False


def test_product_escalation_reads_reach_from_the_policy_unless_overridden():
    backlog = _held("o/r#1", status="Backlog")
    pol = dict(_policy(), queue_min_status="Backlog")
    e = product_escalation("p", [backlog, _row(ref="o/r#2", number=2, status="Backlog")], {},
                           pol, now=NOW)
    assert e.dispatchable == 1
    e = product_escalation("p", [backlog, _row(ref="o/r#2", number=2, status="Backlog")], {},
                           pol, now=NOW, min_status="Ready")
    assert e.dispatchable == 0 and e.skipped == {"held": 1, "status": 1}
```

- [ ] **Step 2: Run** — `pytest server/tests/test_pm_escalation.py -q`; Expected: FAIL `ModuleNotFoundError: server.pm.escalation`.

- [ ] **Step 3: Implement `server/pm/escalation.py`**

```python
"""The escalation read model (spec 2026-09-30 §3): what is held, per product, what
each hold blocks, and whether the product's queue is starving.

Pure. Rows come from ``queue_store.load_queue_rows`` (the runner's own pool), the
eligibility rule is ``queue.skip_reason`` (never re-derived here), "parked" is
``desired_state.HELD_KINDS`` and the reverse dependency index is
``drift.reverse_blockers`` — one definition each (constitution VI). Two routes read
this module (``GET /api/pm/holds``, ``GET /api/pm/decisions?impact=true``) and the
decision-escalation automation ticks on it.
"""
from __future__ import annotations

from collections import deque
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any

from server.automations.product_policy import is_on
from server.pm.desired_state import HELD_KINDS
from server.pm.drift import DepEdgeView, reverse_blockers
from server.pm.queue import QueueRow, skip_reason

#: How a parked item comes back (spec 2026-09-18 §6/§7), as the one line a card or a
#: digest shows. ``decision`` ends only by accepting its proposal; ``owner_action``
#: ends on any of the three human signals in ``holds.RELEASE_SIGNALS``.
RELEASED_BY: dict[str, str] = {
    "decision": "Accept an option in the Inbox",
    "owner_action": "Retry, a board move to Ready/Backlog, or an issue-body edit",
}

#: Spec §5 default: a product with this many or fewer dispatchable items while
#: anything is held is starving. The automation's config overrides it.
DEFAULT_STARVING_MAX = 1

#: Cap on the refs a HeldItem names; the counts are exact, the list is a sample.
DOWNSTREAM_REFS_CAP = 10


@dataclass(frozen=True)
class HeldItem:
    ref: str
    repo: str
    number: int
    product: str
    title: str
    url: str
    hold_kind: str
    hold_reason: str
    hold_by: str
    hold_set_at: datetime | None
    age_hours: float | None
    proposal_id: str | None
    options: tuple[dict, ...]
    recommended_key: str | None
    downstream: int
    downstream_dispatchable: int
    downstream_refs: tuple[str, ...]
    dispatchable_on_release: bool
    released_by: str


@dataclass(frozen=True)
class ProductEscalation:
    slug: str
    dispatchable: int
    skipped: dict[str, int]
    held: tuple[HeldItem, ...]
    starving: bool
    oldest_hold_hours: float | None


def reverse_index(rows: Iterable[QueueRow]) -> dict[str, set[str]]:
    """blocker ref → the refs on this board it blocks, via ``drift.reverse_blockers``
    over the rows' already-open ``open_blockers`` (the loader keeps only open,
    repo-scoped destinations, so every edge here is active and open)."""
    edges = [DepEdgeView(src_ref=r.ref, dst_ref=b, source="mirror", active=True,
                         dst_state="open") for r in rows for b in r.open_blockers]
    return reverse_blockers(edges)


def downstream(ref: str, reverse: Mapping[str, set[str]]) -> tuple[str, ...]:
    """Every ref transitively behind ``ref``, sorted; cycle-safe, ``ref`` excluded."""
    seen: set[str] = set()
    todo = deque(reverse.get(ref, ()))
    while todo:
        cur = todo.popleft()
        if cur == ref or cur in seen:
            continue
        seen.add(cur)
        todo.extend(reverse.get(cur, ()))
    return tuple(sorted(seen))


def _hours(since: datetime | None, now: datetime) -> float | None:
    if since is None:
        return None
    return round((now - since).total_seconds() / 3600, 1)


def held_items(rows: Iterable[QueueRow], proposals_by_ref: Mapping[str, Any], *,
               now: datetime, min_status: str, respect_dependencies: bool,
               give_up_after_bounces: int = 0,
               extra_skip_labels: frozenset[str] = frozenset()) -> list[HeldItem]:
    """One ``HeldItem`` per parked row (``HELD_KINDS``), unranked.

    ``downstream_dispatchable`` re-runs ``skip_reason`` on each dependent with the
    whole chain (this ref plus everything behind it) struck from its blockers: it
    answers "if this chain shipped, what would the runner pick up?" — honest about
    the fact that releasing a hold frees the HELD item now and its dependents only
    once it ships. A dependent that is also assigned, claimed or held in its own
    right stays skipped and is not counted.
    """
    rows = list(rows)
    by_ref = {r.ref: r for r in rows}
    reverse = reverse_index(rows)
    kw = dict(min_status=min_status, respect_dependencies=respect_dependencies, now=now,
              give_up_after_bounces=give_up_after_bounces, extra_skip_labels=extra_skip_labels)
    out: list[HeldItem] = []
    for row in rows:
        if row.hold_kind not in HELD_KINDS:
            continue
        chain_refs = downstream(row.ref, reverse)
        chain = set(chain_refs) | {row.ref}
        freed = 0
        for dep in chain_refs:
            dep_row = by_ref.get(dep)
            if dep_row is None:
                continue
            unblocked = replace(dep_row, open_blockers=tuple(
                b for b in dep_row.open_blockers if b not in chain))
            if skip_reason(unblocked, **kw) is None:
                freed += 1
        on_release = skip_reason(replace(row, hold_kind=None), **kw) is None
        proposal = proposals_by_ref.get(row.ref)
        out.append(HeldItem(
            ref=row.ref, repo=row.repo, number=row.number, product=row.product,
            title=row.title, url=row.url, hold_kind=str(row.hold_kind),
            hold_reason=row.hold_reason or "", hold_by=row.hold_by or "",
            hold_set_at=row.hold_set_at, age_hours=_hours(row.hold_set_at, now),
            proposal_id=getattr(proposal, "id", None) if proposal is not None else None,
            options=tuple(getattr(proposal, "options", None) or ()) if proposal is not None else (),
            recommended_key=getattr(proposal, "recommended_key", None) if proposal is not None else None,
            downstream=len(chain_refs), downstream_dispatchable=freed,
            downstream_refs=chain_refs[:DOWNSTREAM_REFS_CAP],
            dispatchable_on_release=on_release,
            released_by=RELEASED_BY.get(str(row.hold_kind), ""),
        ))
    return out


def rank_held(items: Iterable[HeldItem]) -> list[HeldItem]:
    """Loudest first: most downstream, then oldest hold (unknown age last), then ref."""
    return sorted(items, key=lambda i: (-i.downstream, i.age_hours is None,
                                        -(i.age_hours or 0.0), i.ref))


def product_escalation(slug: str, rows: Iterable[QueueRow], proposals_by_ref: Mapping[str, Any],
                       policy: dict, *, now: datetime, starving_max: int = DEFAULT_STARVING_MAX,
                       min_status: str | None = None,
                       respect_dependencies: bool | None = None) -> ProductEscalation:
    """The per-product summary. Reach (``min_status`` / ``respect_dependencies``)
    defaults to the product's own policy, as the runner reads it; a caller with a
    view of its own (``GET /api/pm/holds``'s query params) overrides both.
    ``starving`` is false for a product whose dispatch is off — it is not waiting
    on anyone."""
    from server.pm.queue import rank
    rows = list(rows)
    ms = min_status if min_status is not None else str(policy["queue_min_status"])
    rd = (respect_dependencies if respect_dependencies is not None
          else bool(policy["respect_dependencies"]))
    bounces = int(policy.get("give_up_after_bounces", 0))
    labels = frozenset(str(lbl).strip().lower() for lbl in policy.get("skip_labels", []))
    entries, skipped = rank(rows, min_status=ms, respect_dependencies=rd, now=now,
                            give_up_after_bounces=bounces, extra_skip_labels=labels)
    held = tuple(rank_held(held_items(
        rows, proposals_by_ref, now=now, min_status=ms, respect_dependencies=rd,
        give_up_after_bounces=bounces, extra_skip_labels=labels)))
    ages = [i.age_hours for i in held if i.age_hours is not None]
    return ProductEscalation(
        slug=slug, dispatchable=len(entries), skipped=skipped, held=held,
        starving=bool(held) and len(entries) <= starving_max and is_on(policy),
        oldest_hold_hours=max(ages) if ages else None)
```

`NOW` in the test file is a fixed anchor, not `datetime.now()`, and the only assertions on time are differences — that satisfies the no-time-bomb rule (nothing compares to the real clock).

- [ ] **Step 4: Run** — `pytest server/tests/test_pm_escalation.py -v`; Expected: PASS. Then `ruff check server/pm/escalation.py` and `mypy server/pm/escalation.py --ignore-missing-imports`.
- [ ] **Step 5: Commit** — `feat(pm): escalation read model — held items ranked by downstream impact, per-product starvation`.

---

### Task 3: `GET /api/pm/holds`

**Files:**
- Modify: `server/api/pm.py` (new route beside `queue()`; extract `_queue_inputs`)
- Modify: `server/assistant/policy.py` (allow row after `("GET", r"/api/pm/queue")`)
- Test: `server/tests/test_pm_holds_api.py` (new), `server/tests/test_assistant_policy.py`

**Interfaces:**
- Consumes: Task 2.
- Produces: route `GET /api/pm/holds?product=&kind=&limit=50&min_status=Ready&respect_dependencies=true` with the spec §4.2 shape; helper `async def _queue_inputs(db, now) -> tuple[list[QueueRow], dict[str, dict]]` used by `queue()` and `holds()`; helper `def _held_to_dict(item: HeldItem) -> dict`.

- [ ] **Step 1: Write the failing tests**

`server/tests/test_pm_holds_api.py`:

```python
"""GET /api/pm/holds — the escalation read model on the wire (spec 2026-09-30 §4.2)."""
from __future__ import annotations

from datetime import datetime, timezone

from server.tests.test_pm_queue_api import IDENTITY_HEADERS, _client

OPTIONS = [{"key": "a", "label": "Keep A", "detail": None},
           {"key": "b", "label": "Move to B", "detail": "b is better"}]


def _seed(c) -> None:
    """Two products. viberpit: #1 decision-held with a pending proposal, #2 and #3
    behind it (#3 behind #2), #4 owner_action-held with nothing behind it, #5 free.
    kindash: #7 owner_action-held, nothing else — starving."""
    from server.db import db_session
    from server.db.models import (GhProject, GhProjectItem, Project, ProjectRepo, WorkItem,
                                  WorkItemDep)
    from server.pm import decisions

    async def _run() -> None:
        async with db_session() as db:
            now = datetime.now(timezone.utc)

            async def product(slug, repo, number):
                p = Project(slug=slug, name=slug, automation_policy={"dispatch": "auto"})
                db.add(p)
                await db.flush()
                db.add(ProjectRepo(project_id=p.id, repo=repo, is_primary=True))
                gh = GhProject(project_id=p.id, gh_project_number=number, title=slug)
                db.add(gh)
                await db.flush()
                return gh

            async def item(gh, repo, n, **hold):
                wi = WorkItem(repo=repo, number=n, kind="issue", title=f"issue {n}",
                              state="open", body="b", url=f"https://x/{n}", **hold)
                db.add(wi)
                await db.flush()
                db.add(GhProjectItem(gh_project_id=gh.id, work_item_id=wi.id,
                                     status="Ready", score=float(n)))
                return wi

            gh_v = await product("viberpit", "o/r", 1)
            w1 = await item(gh_v, "o/r", 1)
            await item(gh_v, "o/r", 2)
            await item(gh_v, "o/r", 3)
            await item(gh_v, "o/r", 4, hold_kind="owner_action", hold_by="tony@example.com",
                       hold_reason="needs the keyring", hold_set_at=now)
            await item(gh_v, "o/r", 5)
            db.add(WorkItemDep(src_ref="o/r#2", dst_ref="o/r#1", source="native", active=True))
            db.add(WorkItemDep(src_ref="o/r#3", dst_ref="o/r#2", source="native", active=True))
            await decisions.propose(db, w1, options=OPTIONS, recommended_key="b",
                                    rationale="two writers", actor="session:s1", now=now)
            gh_k = await product("kindash", "o/k", 2)
            await item(gh_k, "o/k", 7, hold_kind="owner_action", hold_by="tony@example.com",
                       hold_reason="owner call", hold_set_at=now)
            await db.commit()

    assert c.portal is not None
    c.portal.call(_run)


def test_holds_lists_parked_items_ranked_by_impact_with_product_summaries():
    with _client() as c:
        _seed(c)
        body = c.get("/api/pm/holds", headers=IDENTITY_HEADERS).json()

        assert [i["ref"] for i in body["held"]] == ["o/r#1", "o/k#7", "o/r#4"]
        top = body["held"][0]
        assert top["holdKind"] == "decision" and top["product"] == "viberpit"
        assert top["proposalId"] and top["recommendedKey"] == "b"
        assert [o["key"] for o in top["options"]] == ["a", "b"]
        assert top["downstream"] == 2 and top["downstreamDispatchable"] == 2
        assert top["downstreamRefs"] == ["o/r#2", "o/r#3"]
        assert top["releasedBy"] == "Accept an option in the Inbox"
        assert top["holdBy"] == "session:s1" and top["holdSetAt"] and top["ageHours"] >= 0
        assert body["held"][2]["proposalId"] is None
        assert body["held"][2]["releasedBy"].startswith("Retry")

        by_slug = {p["slug"]: p for p in body["products"]}
        assert by_slug["viberpit"] == {"slug": "viberpit", "dispatchable": 1,
                                       "skipped": {"held": 2, "blocked": 2},
                                       "heldCount": 2, "starving": True,
                                       "oldestHoldHours": by_slug["viberpit"]["oldestHoldHours"]}
        assert by_slug["kindash"]["starving"] is True and by_slug["kindash"]["dispatchable"] == 0
        assert body["truncated"] == 0 and body["generatedAt"]


def test_holds_filters_by_product_and_kind_and_limits():
    with _client() as c:
        _seed(c)
        body = c.get("/api/pm/holds?product=viberpit&kind=owner_action",
                     headers=IDENTITY_HEADERS).json()
        assert [i["ref"] for i in body["held"]] == ["o/r#4"]
        assert [p["slug"] for p in body["products"]] == ["viberpit"]

        body = c.get("/api/pm/holds?limit=1", headers=IDENTITY_HEADERS).json()
        assert [i["ref"] for i in body["held"]] == ["o/r#1"] and body["truncated"] == 2

        assert c.get("/api/pm/holds?kind=evidence", headers=IDENTITY_HEADERS).status_code == 422
        assert c.get("/api/pm/holds?min_status=Done", headers=IDENTITY_HEADERS).status_code == 422
        # An unknown slug is an empty answer, not an error.
        empty = c.get("/api/pm/holds?product=nope", headers=IDENTITY_HEADERS).json()
        assert empty["products"] == [] and empty["held"] == [] and empty["truncated"] == 0


def test_holds_lists_a_decision_hold_whose_proposal_row_is_gone():
    with _client() as c:
        _seed(c)
        from sqlalchemy import delete

        from server.db import db_session
        from server.db.models import DecisionProposal

        async def _drop():
            async with db_session() as db:
                await db.execute(delete(DecisionProposal))
                await db.commit()
        c.portal.call(_drop)
        body = c.get("/api/pm/holds?product=viberpit&kind=decision",
                     headers=IDENTITY_HEADERS).json()
        (row,) = body["held"]
        assert row["proposalId"] is None and row["options"] == []
```

`server/tests/test_assistant_policy.py`: add `("GET", "/api/pm/holds", True),` after the `/api/pm/queue` row, and `("GET", "/api/pm/holds?product=kindash&kind=decision", True),` beside the `leaves?capability` row.

- [ ] **Step 2: Run** — `pytest server/tests/test_pm_holds_api.py server/tests/test_assistant_policy.py -q`; Expected: FAIL (404 / policy False).

- [ ] **Step 3: Implement**

In `server/api/pm.py`, above `queue()`:

```python
async def _queue_inputs(db, now: datetime) -> tuple[list, dict[str, dict]]:
    """Rows + policies for the queue-shaped views, read in ONE sync session: which
    threshold a product's issues are judged against decides what a view shows, so
    a policy edit must not be able to land between the two reads. Shared by
    ``queue()`` and ``holds()`` so the operator's two views of the pool cannot
    disagree with each other or with the runner."""
    def _rows(s):
        claimed = runner_claims(s)
        return load_queue_rows(s, now=now, claimed_refs=claimed), policies_by_slug(s)
    return await db.run_sync(_rows)
```

Replace `queue()`'s inline `_rows` + `db.run_sync(_rows)` with `rows, policies = await _queue_inputs(db, now)` (behaviour unchanged; the existing tests prove it).

Then the route:

```python
def _held_to_dict(item) -> dict:
    return {
        "ref": item.ref, "repo": item.repo, "number": item.number, "product": item.product,
        "title": item.title, "url": item.url,
        "holdKind": item.hold_kind, "holdReason": item.hold_reason, "holdBy": item.hold_by,
        "holdSetAt": item.hold_set_at.isoformat() if item.hold_set_at else None,
        "ageHours": item.age_hours,
        "proposalId": item.proposal_id, "options": list(item.options),
        "recommendedKey": item.recommended_key,
        "downstream": item.downstream, "downstreamDispatchable": item.downstream_dispatchable,
        "downstreamRefs": list(item.downstream_refs),
        "dispatchableOnRelease": item.dispatchable_on_release,
        "releasedBy": item.released_by,
    }


_HOLDS_LIMIT_MAX = 200


@router.get("/holds")
async def holds(
    product: str | None = None,
    kind: str | None = None,
    limit: int = 50,
    min_status: str = STATUS_READY,
    respect_dependencies: bool = True,
    _identity: str = Depends(current_identity),
    db=Depends(get_db),
) -> dict:
    """Parked items ranked by what they block, with a per-product summary (spec
    2026-09-30 §4.2). Same pool and reach rules as ``GET /api/pm/queue``: one
    ``_queue_inputs`` read, one ``product_escalation`` per product. ``kind`` is one
    of ``HELD_KINDS`` — the two kinds that park an item; ``evidence`` and
    ``stronger_model`` exit on their own and are not "held" here."""
    from server.pm import decisions
    from server.pm.desired_state import HELD_KINDS
    from server.pm.escalation import product_escalation, rank_held

    if min_status not in MIN_STATUS_OPTIONS:
        raise HTTPException(status_code=422, detail={
            "error": "invalid_min_status",
            "message": f"min_status must be one of {MIN_STATUS_OPTIONS}, got {min_status!r}"})
    if kind is not None and kind not in HELD_KINDS:
        raise HTTPException(status_code=422, detail={
            "error": "invalid_kind",
            "message": f"kind must be one of {sorted(HELD_KINDS)}, got {kind!r}"})
    limit = max(1, min(int(limit), _HOLDS_LIMIT_MAX))
    now = datetime.now(timezone.utc)
    rows, policies = await _queue_inputs(db, now)
    proposals_by_ref = {
        f"{w.repo}#{w.number}": p for p, w in await decisions.pending_list(db)
        if w is not None and w.repo and w.number}
    by_product: dict[str, list] = {}
    for r in rows:
        if product is None or r.product == product:
            by_product.setdefault(r.product, []).append(r)
    products_out = []
    held_all: list = []
    for slug in sorted(by_product):
        policy = policies.get(slug) or effective_policy(None)
        esc = product_escalation(slug, by_product[slug], proposals_by_ref, policy, now=now,
                                 min_status=min_status,
                                 respect_dependencies=respect_dependencies)
        products_out.append({"slug": slug, "dispatchable": esc.dispatchable,
                             "skipped": esc.skipped, "heldCount": len(esc.held),
                             "starving": esc.starving,
                             "oldestHoldHours": esc.oldest_hold_hours})
        held_all.extend(i for i in esc.held if kind is None or i.hold_kind == kind)
    ranked = rank_held(held_all)
    return {"products": products_out,
            "held": [_held_to_dict(i) for i in ranked[:limit]],
            "truncated": max(0, len(ranked) - limit),
            "generatedAt": now.isoformat()}
```

`server/assistant/policy.py`: after `("GET", r"/api/pm/queue"),` add `("GET", r"/api/pm/holds"),` with a one-line comment `# Spec 2026-09-30 §4.2: the escalation read model, read-only.`

- [ ] **Step 4: Run** — `pytest server/tests/test_pm_holds_api.py server/tests/test_pm_queue_api.py server/tests/test_assistant_policy.py -v`; Expected: PASS. If the ranking in the first test differs between `o/k#7` and `o/r#4` (both downstream 0, both set at the same `now`), they tie on age and sort by ref — `o/k#7` < `o/r#4` — as asserted.
- [ ] **Step 5: Commit** — `feat(pm): GET /api/pm/holds — parked items ranked by what they block, per-product starvation`.

---

### Task 4: `GET /api/pm/decisions?projectSlug=&impact=`

**Files:**
- Modify: `server/api/pm.py:2306-2334` (`list_decisions`)
- Test: `server/tests/test_pm_decisions_api.py`

**Interfaces:**
- Consumes: `_queue_inputs`, `escalation.held_items` (Task 2/3).
- Produces: query params `projectSlug: str | None` (server-side filter) and `impact: bool = False`; when `impact=true` each row carries `downstream: int` and `downstreamDispatchable: int` (0 when the item is on no board).

- [ ] **Step 1: Write the failing tests**

Append to `server/tests/test_pm_decisions_api.py`:

```python
def test_list_decisions_filters_by_project_slug_server_side(monkeypatch):
    _stub_announce(monkeypatch)
    with _client() as c:
        _register(c, "Viberpit", repo="o/r")
        _register(c, "Kindash", repo="o/k")
        _pid, wid = _seed_one_pending(c, feature_slug="sessions")
        _pid2, wid2 = _seed_one_pending(c, repo="o/k", number=9, project_slug="kindash")
        assert _propose(c, wid).status_code == 201
        assert _propose(c, wid2).status_code == 201

        rows = c.get("/api/pm/decisions", headers=IDENTITY_HEADERS).json()
        assert {r["projectSlug"] for r in rows} == {"viberpit", "kindash"}
        rows = c.get("/api/pm/decisions?projectSlug=kindash", headers=IDENTITY_HEADERS).json()
        assert [r["projectSlug"] for r in rows] == ["kindash"]
        assert "downstream" not in rows[0], "impact is opt-in: the nav badge polls this every 15s"
        assert c.get("/api/pm/decisions?projectSlug=nope", headers=IDENTITY_HEADERS).json() == []


def test_list_decisions_impact_is_zero_for_an_item_on_no_board(monkeypatch):
    _stub_announce(monkeypatch)
    with _client() as c:
        _register(c, "Viberpit", repo="o/r")
        _pid, wid = _seed_one_pending(c, feature_slug="sessions")  # no GhProjectItem
        assert _propose(c, wid).status_code == 201
        (row,) = c.get("/api/pm/decisions?impact=true", headers=IDENTITY_HEADERS).json()
        assert row["downstream"] == 0 and row["downstreamDispatchable"] == 0
```

And in `server/tests/test_pm_holds_api.py` (it has a board):

```python
def test_decisions_impact_reads_the_same_model_as_holds():
    with _client() as c:
        _seed(c)
        (row,) = c.get("/api/pm/decisions?impact=true", headers=IDENTITY_HEADERS).json()
        assert row["ref"] == "o/r#1" and row["downstream"] == 2
        assert row["downstreamDispatchable"] == 2
```

Check `_seed_one_pending`'s signature (`repo=`, `number=`, `project_slug=`) matches the call above — it does per `test_pm_api.py:974`.

- [ ] **Step 2: Run** — Expected: FAIL (`projectSlug` ignored → both rows; `downstream` missing).

- [ ] **Step 3: Implement**

```python
@router.get("/decisions")
async def list_decisions(
    projectSlug: str | None = None,
    impact: bool = False,
    _identity: str = Depends(current_identity),
    db=Depends(get_db),
) -> list[dict]:
    """Pending decision proposals, newest first — the inbox's second card kind.

    Pending ONLY, with no ``pendingOnly`` escape hatch: … (keep the existing text).

    ``projectSlug`` narrows server-side (spec 2026-09-30 §4.3, the same parameter
    ``/triage`` takes). ``impact`` adds ``downstream`` / ``downstreamDispatchable``
    from the escalation read model; opt-in because it costs the queue read and the
    nav badge polls this list every 15 s.
    """
    from server.pm import decisions
    from server.pm.escalation import held_items
    from server.pm.explain import decision_guidance
    rows = await decisions.pending_list(db)
    if not rows:
        return []
    repos = {w.repo for _p, w in rows if w is not None and w.repo}
    slug_by_repo: dict[str, str] = {
        repo: owner.slug for repo, owner
        in (await owning_projects_by_repo(db, repos)).items()} if repos else {}
    policies = await load_policies(db)
    impact_by_ref: dict[str, tuple[int, int]] = {}
    if impact:
        now = datetime.now(timezone.utc)
        qrows, qpolicies = await _queue_inputs(db, now)
        proposals_by_ref = {f"{w.repo}#{w.number}": p for p, w in rows
                            if w is not None and w.repo and w.number}
        by_product: dict[str, list] = {}
        for r in qrows:
            by_product.setdefault(r.product, []).append(r)
        for slug, prows in by_product.items():
            policy = qpolicies.get(slug) or effective_policy(None)
            for item in held_items(prows, proposals_by_ref, now=now,
                                   min_status=str(policy["queue_min_status"]),
                                   respect_dependencies=bool(policy["respect_dependencies"]),
                                   give_up_after_bounces=int(policy["give_up_after_bounces"]),
                                   extra_skip_labels=frozenset(
                                       str(lbl).strip().lower() for lbl in policy["skip_labels"])):
                impact_by_ref[item.ref] = (item.downstream, item.downstream_dispatchable)
    out = []
    for prop, wi in rows:
        slug = slug_by_repo.get(wi.repo) if wi is not None and wi.repo else None
        if projectSlug is not None and slug != projectSlug:
            continue
        policy = (policies.get(slug) if slug else None) or effective_policy(None)
        d = _decision_to_dict(prop, wi, slug, guidance=decision_guidance(policy=policy))
        if impact:
            ref = d.get("ref")
            down, free = impact_by_ref.get(ref or "", (0, 0))
            d["downstream"], d["downstreamDispatchable"] = down, free
        out.append(d)
    return out
```

- [ ] **Step 4: Run** — `pytest server/tests/test_pm_decisions_api.py server/tests/test_pm_holds_api.py -q`; Expected: PASS.
- [ ] **Step 5: Commit** — `feat(pm): decisions list filters by projectSlug and carries downstream impact on request`.

---

### Task 5: Gate, PR

- [ ] `ruff check src/ server/`; `mypy server/ --ignore-missing-imports`; `pytest tests/ -q`; full `server/tests/ -n auto --dist loadfile --timeout=300 -m "not slow"` in the background → `/tmp/pr2-full.log`; re-run any red file alone.
- [ ] In-flight PR check for `server/pm/escalation.py|server/api/pm.py|server/pm/queue.py|server/pm/queue_store.py|assistant/policy.py`.
- [ ] Push `feat/escalation-read-model`; `gh pr create --title "feat(pm): escalation read model — GET /api/pm/holds ranked by what each hold blocks; decisions per product" --body-file /tmp/pr2-body.md` (`Refs …decision-escalation-design.md §3/§4.2/§4.3 (PR 2 of 6)`; trailer); `gh pr merge <#> --squash --auto`.

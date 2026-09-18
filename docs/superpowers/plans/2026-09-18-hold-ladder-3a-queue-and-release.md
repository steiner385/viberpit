# Hold Ladder — 3a: queue `held` skip, runner give-up hold, release signals (server) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the hold on the work item the single handoff signal on the server side: the queue skips held items, the Backlog Runner's give-up writes an owner-action hold instead of a timed cooldown, and an owner-action hold releases only on one of three human signals.

**Architecture:** The runner's `needs_human_at` stamp, its 7-day retry window, and the queue's `needs_human_cooldown` skip go away. `QueueRow` carries `hold_kind`; `skip_reason` returns `held` for `owner_action` / `decision`. `_give_up` still parks the runner's row (`gave_up`, slot accounting only) while the orchestrator's give-up seam writes the hold through `server/pm/holds.py`; a per-tick sweep drops `gave_up` rows whose refs are no longer held. The three release signals (human board edit to Ready/Backlog, issue body edit, Retry endpoint) share one implementation in `holds.py`, called from the reconciler's project-items save and from a new route. `needs_attention` stops counting as a human wait.

**Tech Stack:** Python 3.12 / FastAPI / SQLAlchemy (sync `Session` in the PM store modules, async elsewhere) / pytest. No schema change.

**Spec:** `docs/superpowers/specs/2026-09-18-hold-ladder-design.md` — §3 (runner row keeps `gave_up` for slot accounting; `needs_human_at` and `needs_human_retry_days` removed), §7 (queue skip `held`, `needs_attention` correction, release signals, `POST /api/pm/work-items/{id}/hold/release`). The dialog/web half of §7 is plan 3b; the ladder (§4) and evidence dispatch (§5) are series item 4.

## Global Constraints

- Constitution VI: the release rule has ONE implementation, `holds.release_by_signals`; the queue skip rule lives only in `queue.skip_reason`; `HELD_KINDS` stays in `desired_state.py`.
- DB clock rule: `datetime.now(timezone.utc)`, never `func.now()`.
- Audit every mutation: `pm.hold.set` `{workItemId, kind, reason, actorClass}`, `pm.hold.release` `{workItemId, kind, actorClass, signal}` where `signal ∈ {board_edit, body_edit, retry}`.
- Assistant policy is deny-by-default: the new route gets a `False` row in `server/tests/test_assistant_policy.py`.
- Brand ratchet: no new `CCSWITCH_` literals (env-prefix ceiling is exactly 2484). Run `python scripts/brand_ratchet.py` before pushing.
- No absolute date literal judged against real `datetime.now()`; tests use the injected clock / a fixed `NOW`.
- Commit trailer `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>` — that literal, never the implementer model's own name. Messages via a file and `git commit -F`.
- Test invocation: `env -i HOME=/tmp/vp-home PATH="$PATH" CCSWITCH_DEV=1 PYTHONPATH=$PWD/src /home/tony/GitHub/viberpit/.venv/bin/python -m pytest …`. Never the whole `server/tests/` from a task (the controller runs it detached).
- Branch `hold-ladder-3a` off `origin/master` (created; head e4d9bb15, the foundation merge).

## File structure

| File | Responsibility |
|---|---|
| `server/pm/holds.py` | + `RECONCILER_ACTOR`, `RUNNER_ACTOR`; `set_runner_hold`; `held_refs`; `release_by_signals` (the ONE release rule); `RELEASE_SIGNALS` |
| `server/pm/queue.py` | `QueueRow.hold_kind` replaces `needs_human_at`; `QueueEntry.hold_kind`; `skip_reason` → `held`; `rank` loses `needs_human_retry_days`; `NEEDS_HUMAN_RETRY_DAYS_DEFAULT` removed; `SKIP_REASONS` updated |
| `server/pm/queue_store.py` | `runner_row_maps` → `runner_claims`; `load_queue_rows` reads `hold_kind` off the item; `_LIVE_SESSION_STATUSES` keeps `needs_attention` |
| `server/api/pm.py` | queue view uses `runner_claims` / no retry days; + `POST /work-items/{item_id}/hold/release` |
| `server/sessions/orchestrator.py` | `_backlog_query_queue` uses `runner_claims`, no retry days; `_backlog_needs_human` writes the holds then comments; new `_backlog_held_targets` seam; `_pm_sync_save_project_items` calls `release_by_signals` |
| `server/automations/backlog_runner.py` | `_give_up` row drops `needs_human_at`; `_expire_give_ups` → `_release_give_ups(held_targets)`; config field removed, key retired; `held_targets` seam |
| `server/automations/backlog_github.py` | `announce_needs_human` comment text |
| `server/pm/desired_state.py` | `ItemFacts` loses `needs_human_at`; `derive_status` reads only the hold; `FAILED_TURN_SESSION_STATUSES`; `HUMAN_WAIT_SESSION_STATUSES` loses `needs_attention` |
| `server/pm/reconcile_inputs.py` | stops reading runner rows; `newest_decision_at` uses `hold_set_at` |
| `server/api/pm_tree.py` | `_LIVE` keeps `needs_attention` |
| `docs/product/features/product-management-layer.md`, `docs/product/features/autopilot-backlog-runner.md` | release signals, `held` skip, retry-days removal |
| Tests | `test_pm_holds.py`, `test_pm_queue.py`, `test_pm_queue_store.py`, `test_pm_queue_api.py`, `test_backlog_query_queue.py`, `test_backlog_runner_loop.py`, `test_backlog_runner_automation.py`, `test_backlog_policy_seams.py`, `test_backlog_ownership.py`, `test_backlog_runner_e2e.py`, `test_desired_state.py`, `test_reconcile_inputs.py`, `test_gh_reconcile_loop.py`, `test_pm_api.py`, `test_assistant_policy.py` |

---

### Task 1: The release rule and the runner-hold helpers in `holds.py`

**Files:**
- Modify: `server/pm/holds.py`
- Test: `server/tests/test_pm_holds.py`

**Interfaces:**
- Produces: `RUNNER_ACTOR = "runner"`, `RECONCILER_ACTOR = "reconciler"` (both in `SYSTEM_ACTORS`).
- Produces: `set_runner_hold(wi: WorkItem, *, reason: str, now: datetime) -> None` — `set_hold(kind="owner_action", actor=RUNNER_ACTOR)`.
- Produces: `held_refs(db: Session) -> frozenset[str]` — `owner/repo#N` of every open issue whose `hold_kind in HELD_KINDS` (sync `Session`, like `queue_store`).
- Produces: `RELEASE_SIGNALS = ("board_edit", "body_edit", "retry")`; `Released = tuple[WorkItem, str]` (item, signal); `release_by_signals(db: Session, *, human_edits: Sequence[HumanEdit], now: datetime) -> list[Released]` — clears `owner_action` holds released by a human board edit to `Ready`/`Backlog` or by a body fingerprint that no longer matches `hold_fingerprint`; audits each as `pm.hold.release`. Consumed by Task 6.
- `HumanEdit` is `server.pm.mirror_store.HumanEdit(ref, field, old, new)`; the status field name the mirror uses is `"status"` — verify with `grep -n 'HumanEdit(' server/pm/mirror_store.py` (line ~144) before writing the filter.

- [ ] **Step 1: Write the failing tests**

Append to `server/tests/test_pm_holds.py` (the file already imports `HOLD_KINDS`, `WorkItem`, `set_hold`, `clear_hold`, `NOW`):

```python
def test_runner_and_reconciler_are_system_actors():
    from server.pm.holds import RECONCILER_ACTOR, RUNNER_ACTOR, actor_class, set_runner_hold
    assert actor_class(RUNNER_ACTOR) == "system"
    assert actor_class(RECONCILER_ACTOR) == "system"
    wi = WorkItem(repo="o/r", number=1, kind="issue", title="t", body="b")
    set_runner_hold(wi, reason="max attempts (2) reached with no mergeable PR", now=NOW)
    assert (wi.hold_kind, wi.hold_by) == ("owner_action", RUNNER_ACTOR)
    assert wi.hold_set_at == NOW and wi.hold_fingerprint


def _sync_db():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from server.db.models import Base
    e = create_engine("sqlite://")
    Base.metadata.create_all(e)
    return Session(e)


def _held(db, number, *, kind="owner_action", body="b", state="open"):
    wi = WorkItem(repo="o/r", number=number, kind="issue", state=state, title="t", body=body)
    set_hold(wi, kind=kind, reason="r", actor="tony@example.com", now=NOW)
    db.add(wi)
    db.flush()
    return wi


def test_held_refs_lists_open_owner_action_and_decision_holds_only():
    from server.pm.holds import held_refs
    db = _sync_db()
    _held(db, 1)
    _held(db, 2, kind="decision")
    _held(db, 3, kind="evidence")
    _held(db, 4, state="closed")
    db.add(WorkItem(repo="o/r", number=5, kind="issue", state="open", title="t", body="b"))
    db.commit()
    assert held_refs(db) == frozenset({"o/r#1", "o/r#2"})


def test_release_by_signals_board_edit_and_body_edit():
    from server.pm.holds import RELEASE_SIGNALS, release_by_signals
    from server.pm.mirror_store import HumanEdit
    db = _sync_db()
    by_board = _held(db, 1)
    by_body = _held(db, 2)
    untouched = _held(db, 3)
    evidence = _held(db, 4, kind="evidence")
    by_body.body = "edited after the hold"          # fingerprint drift
    db.flush()
    edits = [HumanEdit("o/r#1", "status", "Needs human", "Ready"),
             HumanEdit("o/r#3", "score", 1.0, 2.0),           # not a status edit
             HumanEdit("o/r#4", "status", "Needs human", "Backlog")]  # evidence: not released
    released = release_by_signals(db, human_edits=edits, now=NOW)
    db.commit()
    assert sorted((w.number, s) for w, s in released) == [(1, "board_edit"), (2, "body_edit")]
    assert by_board.hold_kind is None and by_body.hold_kind is None
    assert untouched.hold_kind == "owner_action"
    assert evidence.hold_kind == "evidence"
    assert set(RELEASE_SIGNALS) == {"board_edit", "body_edit", "retry"}
    from server.db.models import AuditEvent
    rows = db.query(AuditEvent).filter(AuditEvent.type == "pm.hold.release").all()
    assert sorted(r.detail["signal"] for r in rows) == ["board_edit", "body_edit"]
    assert all(r.detail["actorClass"] == "system" and r.detail["kind"] == "owner_action" for r in rows)
    # idempotent: a second pass releases nothing
    assert release_by_signals(db, human_edits=edits, now=NOW) == []
```

`record_audit` is async and takes an `AsyncSession`; `release_by_signals` runs inside `db.run_sync` with a sync `Session`. Write the audit row directly with the ORM in the sync path: `db.add(AuditEvent(type=..., identity=..., detail=..., via="reconciler"))` — check `AuditEvent`'s required columns in `server/db/models.py` (`at` has a default; `type`, `identity`, `detail` are required) and reuse `server.audit.redact_detail` on the detail so the two audit paths cannot disagree about redaction.

- [ ] **Step 2: Run to verify they fail**

Run: `… -m pytest server/tests/test_pm_holds.py -q -k "runner_and_reconciler or held_refs or release_by_signals"`
Expected: FAIL (`ImportError`).

- [ ] **Step 3: Implement in `server/pm/holds.py`**

Change `SYSTEM_ACTORS` and add, after `set_reject_hold`:

```python
RUNNER_ACTOR = "runner"
RECONCILER_ACTOR = "reconciler"
SYSTEM_ACTORS: frozenset[str] = frozenset({RUNNER_ACTOR, RECONCILER_ACTOR, "migration"})

#: The three ways an owner_action hold ends (spec §7). Time is not one of them.
RELEASE_SIGNALS: tuple[str, ...] = ("board_edit", "body_edit", "retry")
#: Board Status values a human moves a card to in order to hand it back.
RELEASING_BOARD_STATUSES: frozenset[str] = frozenset({"Ready", "Backlog"})

Released = tuple[WorkItem, str]


def set_runner_hold(wi: WorkItem, *, reason: str, now: datetime) -> None:
    """The Backlog Runner's give-up (spec §3): the hold IS the handoff; the runner's
    own ``gave_up`` row survives only for slot accounting."""
    set_hold(wi, kind="owner_action", reason=reason, actor=RUNNER_ACTOR, now=now)


def held_refs(db: "Session") -> frozenset[str]:
    """``owner/repo#N`` of every open issue parked by an owner_action or decision
    hold — what the runner's sweep reads to drop ``gave_up`` rows that are no longer
    backed by a hold."""
    from sqlalchemy import select
    from server.pm.desired_state import HELD_KINDS
    rows = db.execute(select(WorkItem.repo, WorkItem.number).where(
        WorkItem.kind == "issue", WorkItem.state == "open",
        WorkItem.hold_kind.in_(sorted(HELD_KINDS)))).all()
    return frozenset(f"{repo}#{number}" for repo, number in rows)


def _audit_release(db: "Session", wi: WorkItem, *, kind: str, signal: str, actor: str) -> None:
    from server.audit import redact_detail
    from server.db.models import AuditEvent
    db.add(AuditEvent(type="pm.hold.release", identity=actor, via="reconciler",
                      detail=redact_detail({"workItemId": wi.id, "kind": kind,
                                            "actorClass": actor_class(actor), "signal": signal})))


def release_by_signals(db: "Session", *, human_edits: "Sequence[HumanEdit]",
                       now: datetime) -> list[Released]:
    """THE release rule for owner_action holds (spec §7), run once per reconcile
    pass: a human board edit to Ready/Backlog, or an issue body that no longer
    matches the fingerprint taken when the hold was set. The Retry endpoint is the
    third signal and calls ``clear_hold`` directly with the person's actor.
    Idempotent: a released item has no hold to release again."""
    from sqlalchemy import select
    from server.pm.outcomes import body_fingerprint
    from server.pm.refs import split_ref
    released: list[Released] = []
    board_refs = {e.ref for e in human_edits
                  if e.field == "status" and str(e.new) in RELEASING_BOARD_STATUSES}
    held = db.scalars(select(WorkItem).where(
        WorkItem.kind == "issue", WorkItem.hold_kind == "owner_action")).all()
    for wi in held:
        ref = f"{wi.repo}#{wi.number}"
        signal: str | None = None
        if ref in board_refs:
            signal = "board_edit"
        elif wi.hold_fingerprint and wi.hold_fingerprint != body_fingerprint(wi.title, wi.body):
            signal = "body_edit"
        if signal is None:
            continue
        clear_hold(wi, actor=RECONCILER_ACTOR)
        _audit_release(db, wi, kind="owner_action", signal=signal, actor=RECONCILER_ACTOR)
        released.append((wi, signal))
    db.flush()
    return released
```

Add `from typing import TYPE_CHECKING` guards for `Session` (`sqlalchemy.orm`), `Sequence`, and `HumanEdit` (`server.pm.mirror_store`) so `holds.py` stays importable from the alembic revision without pulling the mirror store at import time. `split_ref` is unused above — do not import it. The `now` parameter is accepted for symmetry with every other store function and passed to nothing yet; keep it (Task 6's caller has it).

- [ ] **Step 4: Run the tests**

Run: `… -m pytest server/tests/test_pm_holds.py -q`
Expected: PASS (all, including the foundation's tests).

- [ ] **Step 5: Commit**

```bash
git add server/pm/holds.py server/tests/test_pm_holds.py
printf 'feat(pm): runner hold, held_refs, and the one release rule for owner_action holds\n\nSpec 2026-09-18 §3/§7.\n\nCo-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>\n' > /tmp/c1.txt
git commit -F /tmp/c1.txt
```

---

### Task 2: The queue core skips held items instead of cooling down

**Files:**
- Modify: `server/pm/queue.py:14-21` (header comment + `NEEDS_HUMAN_RETRY_DAYS_DEFAULT`), `:51-52` (`SKIP_REASONS`), `:67-100` (`QueueRow`, `QueueEntry`), `:150-200` (`skip_reason`), `:222-250` (`rank`)
- Test: `server/tests/test_pm_queue.py`

**Interfaces:**
- `QueueRow`: `needs_human_at` removed; `hold_kind: str | None = None` added (defaulted, last).
- `QueueEntry`: `hold_kind: str | None = None` added last (item 4's spawn reads it to pick the prompt).
- `skip_reason(row, *, min_status, respect_dependencies, now, give_up_after_bounces=0)` — `needs_human_retry_days` removed; returns `"held"` when `row.hold_kind in HELD_KINDS`, checked BEFORE the status gate for the same reason the cooldown was.
- `rank(rows, *, min_status, respect_dependencies, now, give_up_after_bounces=0)`.
- `SKIP_REASONS` replaces `"needs_human_cooldown"` with `"held"`.
- `NEEDS_HUMAN_RETRY_DAYS_DEFAULT` is deleted.

- [ ] **Step 1: Update the tests**

In `server/tests/test_pm_queue.py` change `_row`'s base: replace `needs_human_at=None` with `hold_kind=None`. Replace `test_needs_human_at_cools_down_then_reopens`, `test_needs_human_label_alone_no_longer_cools_down`, and `test_needs_human_cooldown_outranks_the_board_status_it_causes` with:

```python
def test_held_rows_are_skipped_regardless_of_time():
    """Spec 2026-09-18 §7: an owner_action or decision hold parks the row until a
    human signal releases it — there is no timer to wait out."""
    kw = dict(min_status="Backlog", respect_dependencies=True, now=NOW)
    assert skip_reason(_row(hold_kind="owner_action"), **kw) == "held"
    assert skip_reason(_row(hold_kind="decision"), **kw) == "held"
    # the two auto-exiting kinds are eligible
    assert skip_reason(_row(hold_kind="evidence"), **kw) is None
    assert skip_reason(_row(hold_kind="stronger_model"), **kw) is None


def test_needs_human_label_alone_never_skips():
    """Phase D: the label is not a signal; only the hold is."""
    kw = dict(min_status="Backlog", respect_dependencies=True, now=NOW)
    assert skip_reason(_row(labels=("needs-human",), gh_updated_at=NOW), **kw) is None


def test_held_outranks_the_board_status_it_causes():
    """The reconciler stamps ``Needs human`` on exactly these rows, so a
    status-first check would report them as plain "status" skips and the card
    would lose its held gauge."""
    kw = dict(min_status="Backlog", respect_dependencies=True, now=NOW)
    assert skip_reason(_row(status="Needs human", hold_kind="owner_action"), **kw) == "held"


def test_rank_carries_hold_kind_onto_entries():
    entries, skipped = rank([_row(hold_kind="evidence"), _row(ref="o/r#2", number=2, hold_kind="owner_action")],
                            min_status="Backlog", respect_dependencies=True, now=NOW)
    assert [(e.ref, e.hold_kind) for e in entries] == [("o/r#1", "evidence")]
    assert skipped == {"held": 1}
    assert "held" in SKIP_REASONS and "needs_human_cooldown" not in SKIP_REASONS
```

Every other `rank(`/`skip_reason(` call in this file that passes `needs_human_retry_days=` must drop that keyword (grep the file).

- [ ] **Step 2: Run to verify they fail**

Run: `… -m pytest server/tests/test_pm_queue.py -q`
Expected: FAIL (`TypeError: unexpected keyword 'hold_kind'`).

- [ ] **Step 3: Implement**

`server/pm/queue.py`: delete the `NEEDS_HUMAN_RETRY_DAYS_DEFAULT` constant and its comment block; import `HELD_KINDS` from `.desired_state` beside the existing status imports; replace `needs_human_at: datetime | None = None` on `QueueRow` with:

```python
    # Spec 2026-09-18 §3: the hold on the item. ``owner_action`` / ``decision`` park
    # the row (``skip_reason`` → "held"); ``evidence`` / ``stronger_model`` ride
    # through to the entry so the spawn can pick its prompt and rung.
    hold_kind: str | None = None
```

Add `hold_kind: str | None = None` as the last field of `QueueEntry`. `SKIP_REASONS = ("status", "blocked", "live_session", "claimed", "assigned", "epic_open", "bot", "held", "needs_spec")`. In `skip_reason`, drop the `needs_human_retry_days` parameter and replace the cooldown block with:

```python
    # Checked BEFORE the status gate: the reconciler stamps board Status "Needs
    # human" from exactly this hold, so a status-first check would report these
    # rows as plain "status" skips and the card would lose its held gauge.
    if row.hold_kind in HELD_KINDS:
        return "held"
```

In `rank`, drop the parameter and its pass-through, and construct entries with `hold_kind=r.hold_kind`. Rewrite the module header comment (lines 14-21) to describe the hold, not the window.

- [ ] **Step 4: Run the tests**

Run: `… -m pytest server/tests/test_pm_queue.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add server/pm/queue.py server/tests/test_pm_queue.py
printf 'feat(pm): queue skips held rows; the needs-human cooldown is gone\n\nCo-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>\n' > /tmp/c2.txt
git commit -F /tmp/c2.txt
```

---

### Task 3: The queue loader and both queue readers

**Files:**
- Modify: `server/pm/queue_store.py:33` (`_NO_TIMES`), `:51-78` (`runner_row_maps`), `:81-95` (signature/docstring), `:174` (row construction)
- Modify: `server/sessions/orchestrator.py:5800-5850` (`_backlog_query_queue`), `server/api/pm.py:676-730` (queue view)
- Test: `server/tests/test_pm_queue_store.py`, `server/tests/test_pm_queue_api.py`, `server/tests/test_backlog_query_queue.py`

**Interfaces:**
- `runner_claims(db: Session) -> frozenset[str]` replaces `runner_row_maps` (a `gave_up` row is neither a claim nor anything else the queue reads).
- `load_queue_rows(db, *, now, claimed_refs=frozenset())` — `needs_human_at` parameter removed; each row's `hold_kind=w.hold_kind`.

- [ ] **Step 1: Update the tests**

`server/tests/test_pm_queue_store.py`: change the import to `runner_claims`; rewrite `test_runner_row_maps_derives_claims_and_give_ups_from_the_runner_rows` as `test_runner_claims_reads_claims_and_ignores_give_ups` asserting `runner_claims(db) == frozenset({"o/r#1", "o/r#2", "o/r#3"})` with the same seeded rows; replace `test_load_queue_rows_threads_needs_human_at` with:

```python
def test_load_queue_rows_reads_hold_kind_off_the_item():
    db = _db()
    _seed_product(db, "viberpit", "steiner385/viberpit", board=True)
    held = _item(db, "steiner385/viberpit", 5, status="Ready")
    held.hold_kind, held.hold_by, held.hold_reason = "owner_action", "tony@example.com", "r"
    _item(db, "steiner385/viberpit", 6, status="Ready")
    db.commit()
    by_ref = {r.ref: r for r in load_queue_rows(db, now=NOW)}
    assert by_ref["steiner385/viberpit#5"].hold_kind == "owner_action"
    assert by_ref["steiner385/viberpit#6"].hold_kind is None
```

`server/tests/test_pm_queue_api.py` and `server/tests/test_backlog_query_queue.py`: grep each for `needs_human_at`, `needs_human_cooldown`, `runner_row_maps`, `needs_human_retry_days`; seed a hold on the item (`hold_kind="owner_action"`) instead of a `gave_up` row where a test expects a skip, and assert `skipped["held"]` instead of `skipped["needs_human_cooldown"]`.

- [ ] **Step 2: Run to verify they fail**

Run: `… -m pytest server/tests/test_pm_queue_store.py server/tests/test_pm_queue_api.py server/tests/test_backlog_query_queue.py -q`
Expected: FAIL (`ImportError: runner_claims`).

- [ ] **Step 3: Implement**

`server/pm/queue_store.py`: delete `_NO_TIMES`; replace `runner_row_maps` with:

```python
def runner_claims(db: Session) -> frozenset[str]:
    """Refs the backlog runner currently claims, from its durable target-state rows.

    ONE derivation, read by both queue consumers (the runner's own pool and
    ``GET /api/pm/queue``) so the operator's view cannot disagree with what agents
    will do next. A bundle rides on the primary's row; a row without ``issues`` IS
    its own claim. A ``gave_up`` row is NOT a claim — and since spec 2026-09-18 it
    is not a queue signal at all: the hold on the item is (``QueueRow.hold_kind``).
    """
    claimed: set[str] = set()
    rows = db.scalars(select(AutomationTargetState).where(
        AutomationTargetState.automation_id == RUNNER_AUTOMATION_ID))
    for row in rows:
        st = row.state or {}
        if st.get("claimed") and not st.get("gave_up"):
            claimed.update(st.get("issues") or [row.target])
    return frozenset(claimed)
```

`load_queue_rows`: drop the `needs_human_at` parameter and the two docstring paragraphs about it; construct rows with `hold_kind=w.hold_kind` instead of `needs_human_at=...`.

`server/sessions/orchestrator.py` `_backlog_query_queue`: `claimed = runner_claims(s)`; `load_queue_rows(s, now=now, claimed_refs=claimed)`; delete the `raw_days`/`retry_days` lines and the `needs_human_retry_days=retry_days` argument (and its two-line comment). Update the import from `queue_store`. Grep the file for `NEEDS_HUMAN_RETRY_DAYS_DEFAULT` and remove that import.

`server/api/pm.py` queue view: same two substitutions; delete the docstring bullet about `needs_human_retry_days`; the sentence "one waiting on a human as `needs_human_cooldown`" becomes "one parked by a hold as `held`".

- [ ] **Step 4: Run the tests plus mypy on the touched modules**

Run: `… -m pytest server/tests/test_pm_queue_store.py server/tests/test_pm_queue_api.py server/tests/test_backlog_query_queue.py -q && … -m mypy server/pm/queue_store.py server/api/pm.py --ignore-missing-imports`
Expected: PASS / clean. (`orchestrator.py` is checked in Task 7's full gate.)

- [ ] **Step 5: Commit**

```bash
git add server/pm/queue_store.py server/sessions/orchestrator.py server/api/pm.py server/tests/test_pm_queue_store.py server/tests/test_pm_queue_api.py server/tests/test_backlog_query_queue.py
printf 'feat(pm): queue readers take hold_kind from the item, not a give-up stamp\n\nCo-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>\n' > /tmp/c3.txt
git commit -F /tmp/c3.txt
```

---

### Task 4: The runner's give-up writes a hold; `gave_up` rows follow the hold

**Files:**
- Modify: `server/automations/backlog_runner.py:270-296` (config field, retired keys), `:548-600` (constructor seams), `:1330-1372` (`_expire_give_ups` → `_release_give_ups`, `_sweep`), `:714-722` (call site), `:1459-1475` (`_give_up`)
- Modify: `server/sessions/orchestrator.py` (`_backlog_needs_human`, ~line 6080; the loop construction ~line 1057 passes the new seam)
- Modify: `server/automations/backlog_github.py:332-351` (`announce_needs_human` text)
- Test: `server/tests/test_backlog_runner_loop.py`, `server/tests/test_backlog_runner_automation.py`, `server/tests/test_backlog_policy_seams.py`, `server/tests/test_backlog_ownership.py`, `server/tests/test_backlog_runner_e2e.py`

**Interfaces:**
- Constructor seam `held_targets: Callable[[], Awaitable[frozenset[str]]] | None = None` (default returns an empty set); the orchestrator binds `_backlog_held_targets`, which runs `holds.held_refs` in `db.run_sync`.
- `_give_up` row: `{**st, "claimed": False, "gave_up": True, "reason": reason}` — no `needs_human_at`.
- `_release_give_ups()` (replaces `_expire_give_ups(config)`): drops every `gave_up` row none of whose refs (`st["issues"] or [target]`) is in `held_targets()`; emits kind `hold_released` with summary `f"{target}: hold released — back in the pool"`. Called where `_expire_give_ups` was.
- `_backlog_needs_human(target, reason)`: FIRST writes `set_runner_hold` on every issue of the batch (one `db_session`, one audit row per issue: `pm.hold.set` `{workItemId, kind: "owner_action", reason, actorClass: "system"}` with identity `RUNNER_ACTOR`, `via="runner"`), THEN comments (best-effort, unchanged). A missing mirror row for a ref is logged and skipped, never raised.
- Config: the `needs_human_retry_days` `ConfigField` is removed and the key is appended to `retired_config_keys`.

- [ ] **Step 1: Update the tests**

`server/tests/test_backlog_runner_loop.py`: in `build_loop`, add a `held: set[str] | None = None` parameter and pass `held_targets=lambda: _held()` where `_held` is `async def _held(): return frozenset(held or ())`. Change `test_terminal_watch_gave_up_unclaims_and_needs_human` so the assertion on the surviving row reads `assert st["gave_up"] is True and "needs_human_at" not in st`. Replace any test named for `_expire_give_ups` / `needs_human_expired` (grep) with:

```python
def test_gave_up_row_is_dropped_once_its_hold_is_released(tmp_path):
    """Spec 2026-09-18 §3: the hold is the handoff; the runner's row follows it."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'h.db'}"

    async def run():
        await init_db(db_url=db_url)
        held = {"r#1"}
        loop, rec, ctx = build_loop(config=make_config(max_concurrent=1),
                                    candidates=[], held=held)
        await ctx.compare_target_state("r#1", None, {"gave_up": True, "claimed": False,
                                                     "reason": "max attempts"})
        await loop.tick()
        assert (await ctx.get_target_state("r#1")).get("gave_up") is True   # still held
        held.clear()                                                           # human released it
        await loop.tick()
        assert not await ctx.get_target_state("r#1")                           # row gone
        assert [e for e in await ctx.list_events(kind="hold_released")]
    asyncio.run(run())
```

(Adapt `compare_target_state`'s real signature from the file's other seeding helpers — `_seed_row` near the top is the precedent; use that helper if it can seed a `gave_up` row.) `make_config` must no longer accept or emit `needs_human_retry_days`; grep the four other runner test files for that key and `needs_human_at` and remove them.

- [ ] **Step 2: Run to verify they fail**

Run: `… -m pytest server/tests/test_backlog_runner_loop.py -q -k "gave_up"`
Expected: FAIL (`TypeError: unexpected keyword 'held_targets'`).

- [ ] **Step 3: Implement the runner side**

`backlog_runner.py`: delete the `needs_human_retry_days` `ConfigField`; append `"needs_human_retry_days"` to `retired_config_keys` with a comment ("spec 2026-09-18: a hold releases on a human signal, never on a timer"). Add the seam to the constructor (`HeldTargets = Callable[[], Awaitable[frozenset[str]]]` beside the other aliases) with a stub `async def _stub_held_targets() -> frozenset[str]: return frozenset()`. Replace `_expire_give_ups`:

```python
    async def _release_give_ups(self) -> None:
        """Drop ``gave_up`` rows that no hold backs any more (spec 2026-09-18 §3).

        The row survives ONLY for slot accounting while the item is held; the hold
        on the item is the handoff, and a human releases it (board edit, body edit,
        Retry). Once none of the row's refs is held the row is deleted, which is what
        returns the issue to the pool. Runs BEFORE ``_refill`` — see the call site.
        """
        states = await self._ctx.list_target_states()
        held = await self._held_targets()
        for target, st in states.items():
            if not st.get("gave_up"):
                continue
            refs = st.get("issues") or [target]
            if any(ref in held for ref in refs):
                continue
            if not await self._write_state(target, st, None):
                continue
            await self._ctx.emit("info", f"{target}: hold released — back in the pool",
                                 kind="hold_released", target=target)
```

Update the call site (`await self._release_give_ups()`), its comment, and `_sweep`'s "survived `_expire_give_ups`" comment. In `_give_up`, the parked dict loses `"needs_human_at": …`. Delete the now-unused `timedelta`/`NEEDS_HUMAN_RETRY_DAYS_DEFAULT`/`parse_iso_utc` imports if nothing else uses them (ruff will say).

`backlog_github.announce_needs_human` body text becomes: `f"Handed to a human by {brand.NAME} Backlog Runner: {reason}. Board Status is Held until a person releases it: move the card to Ready or Backlog, edit the issue body, or tap Retry in {brand.NAME}."` and its docstring drops "the queue into a retry cooldown".

- [ ] **Step 4: Implement the orchestrator side**

`_backlog_needs_human`:

```python
    async def _backlog_needs_human(self, target: str, reason: str) -> None:
        """GIVE-UP path (spec 2026-09-18 §3): write the owner_action hold on every
        issue of the batch — the hold IS the handoff — then comment it (best-effort).
        The runner's ``gave_up`` row survives for slot accounting only and is
        dropped by ``_release_give_ups`` once the hold is gone."""
        from datetime import datetime, timezone
        from sqlalchemy import select, tuple_
        from server.audit import record_audit
        from server.automations import backlog_github
        from server.db.models import WorkItem
        from server.pm.holds import RUNNER_ACTOR, actor_class, set_runner_hold
        from server.pm.refs import split_ref
        batch = await self._backlog_batch_for(target)
        parts = [p for p in (split_ref(r) for r in batch.issues) if p is not None]
        now = datetime.now(timezone.utc)
        if parts:
            async with db_session() as db:
                items = (await db.execute(select(WorkItem).where(
                    WorkItem.kind == "issue",
                    tuple_(WorkItem.repo, WorkItem.number).in_(parts)))).scalars().all()
                for wi in items:
                    set_runner_hold(wi, reason=reason, now=now)
                    await record_audit(db=db, type="pm.hold.set", identity=RUNNER_ACTOR, detail={
                        "workItemId": wi.id, "kind": "owner_action", "reason": reason,
                        "actorClass": actor_class(RUNNER_ACTOR)}, via="runner")
                missing = set(batch.issues) - {f"{w.repo}#{w.number}" for w in items}
                if missing:
                    log.warning("backlog_runner.give_up_unmirrored refs=%s", sorted(missing))
        await asyncio.to_thread(
            backlog_github.announce_needs_human, batch, reason, gh_run=self._gh_run)

    async def _backlog_held_targets(self) -> frozenset[str]:
        from server.pm.holds import held_refs
        async with db_session() as db:
            return await db.run_sync(held_refs)
```

Bind `held_targets=self._backlog_held_targets` where the loop is constructed (grep `needs_human=self._backlog_needs_human`). Check `WorkItem.repo`/`number` tuple-IN works on both dialects — `queue_store.load_queue_rows` already uses `tuple_(WorkItem.repo, WorkItem.number).in_(...)`, so it does.

- [ ] **Step 5: Run the runner suites**

Run: `… -m pytest server/tests/test_backlog_runner_loop.py server/tests/test_backlog_runner_automation.py server/tests/test_backlog_policy_seams.py server/tests/test_backlog_ownership.py server/tests/test_backlog_runner_e2e.py -q`
Expected: PASS. `test_backlog_runner_e2e.py` drives the real orchestrator seam; if it asserts a comment text or a `needs_human_at` stamp, update it to the hold (`WorkItem.hold_kind == "owner_action"`, `hold_by == "runner"`) — that is the intended behaviour change.

- [ ] **Step 6: Commit**

```bash
git add server/automations/backlog_runner.py server/automations/backlog_github.py server/sessions/orchestrator.py server/tests/test_backlog_runner_loop.py server/tests/test_backlog_runner_automation.py server/tests/test_backlog_policy_seams.py server/tests/test_backlog_ownership.py server/tests/test_backlog_runner_e2e.py
printf 'feat(runner): a give-up writes the owner_action hold; gave_up rows follow the hold\n\nThe 7-day retry window is gone (spec 2026-09-18 §3, decision 6).\n\nCo-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>\n' > /tmp/c4.txt
git commit -F /tmp/c4.txt
```

---

### Task 5: Derivation reads only the hold; a failed turn is not a human wait

**Files:**
- Modify: `server/pm/desired_state.py:24-27` (status sets), `:45-60` (`ItemFacts`), `:80-92` (`derive_status`)
- Modify: `server/pm/reconcile_inputs.py:88-95`, `:118-124`, `:140-152`
- Modify: `server/pm/queue_store.py:31`, `server/api/pm_tree.py:29`, `server/sessions/orchestrator.py:9080-9082`
- Test: `server/tests/test_desired_state.py`, `server/tests/test_reconcile_inputs.py`, `server/tests/test_gh_reconcile_loop.py`, `server/tests/test_pm_tree.py`

**Interfaces:**
- `HUMAN_WAIT_SESSION_STATUSES = frozenset({"waiting_question", "waiting_input", "ready_review"})`; new `FAILED_TURN_SESSION_STATUSES = frozenset({"needs_attention"})`; new `LIVE_SESSION_STATUSES = ACTIVE_SESSION_STATUSES | HUMAN_WAIT_SESSION_STATUSES | FAILED_TURN_SESSION_STATUSES` — the ONE liveness union, imported by `queue_store`, `pm_tree`, and the orchestrator's dedupe check in place of their three local unions.
- `ItemFacts`: `needs_human_at` removed (keyword-only constructions everywhere, so removal is safe — the three constructors are `reconcile_inputs.py:113`, `test_desired_state.py:19`, `test_gh_reconcile_loop.py:47`).
- `derive_status`: `if f.hold_kind in HELD_KINDS: return Decision(STATUS_NEEDS_HUMAN, f.hold_set_at or f.classified_at)`.

- [ ] **Step 1: Update the tests**

`server/tests/test_desired_state.py`: remove `needs_human_at=None` from `_f`; delete any assertion using `needs_human_at`; add:

```python
def test_failed_turn_is_not_a_human_wait():
    from server.pm.desired_state import (FAILED_TURN_SESSION_STATUSES,
                                         HUMAN_WAIT_SESSION_STATUSES, LIVE_SESSION_STATUSES)
    assert "needs_attention" not in HUMAN_WAIT_SESSION_STATUSES
    assert FAILED_TURN_SESSION_STATUSES == frozenset({"needs_attention"})
    assert "needs_attention" in LIVE_SESSION_STATUSES
    # a failed runner turn is a bounce the sweep handles, not a parked card
    assert derive_status(_f(session_status="needs_attention", session_at=T2,
                            triage_state="classified", classified_at=T1)) == Decision(STATUS_READY, T1)
```

`server/tests/test_gh_reconcile_loop.py`: remove `needs_human_at=None` from `_item`. `server/tests/test_reconcile_inputs.py`: grep for `needs_human_at` / `runner_row_maps` / `gave_up`; a test that seeds a `gave_up` row and expects a Needs-human decision now seeds `hold_kind="owner_action", hold_set_at=<T>` on the item and asserts the decision is dated from `hold_set_at`. `server/tests/test_pm_tree.py`: add `assert issue_tier("open", "classified", False, "needs_attention") == "in-flight"` next to the existing `waiting_input` → needs-you line (the tier's in-flight branch reads `ACTIVE_SESSION_STATUSES`; extend that branch to `ACTIVE_SESSION_STATUSES | FAILED_TURN_SESSION_STATUSES` so a failed-turn session still reads as in flight, not idle).

- [ ] **Step 2: Run to verify they fail**

Run: `… -m pytest server/tests/test_desired_state.py server/tests/test_pm_tree.py -q`
Expected: FAIL (`ImportError: FAILED_TURN_SESSION_STATUSES`).

- [ ] **Step 3: Implement**

`desired_state.py`:

```python
ACTIVE_SESSION_STATUSES = frozenset({"in_progress", "retrying", "condensing"})
# A person's turn: a question, a permission prompt, a plan awaiting review.
HUMAN_WAIT_SESSION_STATUSES = frozenset({"waiting_question", "waiting_input", "ready_review"})
# A failed turn (spec 2026-09-18 §7): a bounce the runner's sweep handles, never a
# parked card — it left the human-wait set so the ladder, not a person, sees it.
FAILED_TURN_SESSION_STATUSES = frozenset({"needs_attention"})
# THE liveness union every "is there a live session on this ref" reader uses.
LIVE_SESSION_STATUSES = (ACTIVE_SESSION_STATUSES | HUMAN_WAIT_SESSION_STATUSES
                         | FAILED_TURN_SESSION_STATUSES)
```

Remove `needs_human_at` from `ItemFacts` (and its comment); `derive_status`'s held branch becomes `if f.hold_kind in HELD_KINDS: return Decision(STATUS_NEEDS_HUMAN, f.hold_set_at or f.classified_at)` with the comment updated. `reconcile_inputs.py`: delete the `runner_row_maps` import and the `needs_human_at = …` line; drop `needs_human_at=` from the `ItemFacts(...)` call; in the `newest_decision_at` computation replace `*(f.needs_human_at for f in items)` with `*(f.hold_set_at for f in items)` and reword the comment above it (a hold is a decision the reconciler must not write against a mirror older than). `queue_store.py:31` and `pm_tree.py:29`: replace the local union with `from .desired_state import LIVE_SESSION_STATUSES` and use it. `orchestrator.py:9080-9082`: import and use `LIVE_SESSION_STATUSES`. `tree.py` in-flight branch: `session_status in (ACTIVE_SESSION_STATUSES | FAILED_TURN_SESSION_STATUSES)`.

- [ ] **Step 4: Run the tests**

Run: `… -m pytest server/tests/test_desired_state.py server/tests/test_reconcile_inputs.py server/tests/test_gh_reconcile_loop.py server/tests/test_pm_tree.py server/tests/test_pm_queue_store.py server/tests/test_pm_map_api.py -q && … -m mypy server/pm/desired_state.py server/pm/reconcile_inputs.py server/pm/tree.py server/api/pm_tree.py --ignore-missing-imports`
Expected: PASS / clean.

- [ ] **Step 5: Commit**

```bash
git add server/pm/desired_state.py server/pm/reconcile_inputs.py server/pm/tree.py server/pm/queue_store.py server/api/pm_tree.py server/sessions/orchestrator.py server/tests/test_desired_state.py server/tests/test_reconcile_inputs.py server/tests/test_gh_reconcile_loop.py server/tests/test_pm_tree.py
printf 'feat(pm): board decision reads only the hold; a failed turn is not a human wait\n\nCo-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>\n' > /tmp/c5.txt
git commit -F /tmp/c5.txt
```

---

### Task 6: Release signals wired: reconcile pass + Retry endpoint

**Files:**
- Modify: `server/sessions/orchestrator.py` (`_pm_sync_save_project_items`, ~line 6540 — grep the def)
- Modify: `server/api/pm.py` (new route after `delete_work_item_dep`)
- Test: `server/tests/test_pm_api.py`, `server/tests/test_assistant_policy.py`

**Interfaces:**
- `_pm_sync_save_project_items` calls `holds.release_by_signals(s, human_edits=edits, now=now)` inside the same `_sync` after `back_sync_stage`, and emits nothing itself (the audit rows are the record; the pm-sync loop already surfaces the human edits).
- `POST /api/pm/work-items/{item_id}/hold/release` → `{"ok": true, "released": <kind|null>, "item": <work item dict>}`; 404 unknown item; 422 `hold_not_permitted` when the actor may not clear the hold kind; audit `pm.hold.release` `{workItemId, kind, actorClass, signal: "retry"}`; a no-hold item is a 200 no-op with `released: null` and no audit.

- [ ] **Step 1: Write the failing tests**

Append to `server/tests/test_pm_api.py`:

```python
def test_hold_release_endpoint_is_the_retry_signal():
    with _client() as c:
        _register(c, "Viberpit", repo="o/r")
        _pid, wid = _seed_one_pending(c, feature_slug="sessions")
        r = c.patch(f"/api/pm/work-items/{wid}/overlay",
                    json={"holdKind": "owner_action", "holdReason": "owner only"},
                    headers=IDENTITY_HEADERS)
        assert r.status_code == 200
        r = c.post(f"/api/pm/work-items/{wid}/hold/release", headers=IDENTITY_HEADERS)
        assert r.status_code == 200, r.text
        assert r.json()["released"] == "owner_action"
        assert r.json()["item"]["holdKind"] is None
        # idempotent no-op
        again = c.post(f"/api/pm/work-items/{wid}/hold/release", headers=IDENTITY_HEADERS)
        assert again.status_code == 200 and again.json()["released"] is None
        missing = c.post("/api/pm/work-items/nope/hold/release", headers=IDENTITY_HEADERS)
        assert missing.status_code == 404
        from server.db import db_session
        from sqlalchemy import select
        from server.db.models import AuditEvent

        async def _rows():
            async with db_session() as db:
                return [e.detail for e in (await db.execute(
                    select(AuditEvent).where(AuditEvent.type == "pm.hold.release"))).scalars().all()]
        rows = c.portal.call(_rows)
        assert [d["signal"] for d in rows] == ["retry"] and rows[0]["actorClass"] == "person"


def test_hold_release_refused_for_agent_class_caller():
    with patch.dict(os.environ, LAN_OPEN), _client() as c:
        _register(c, "Viberpit", repo="o/r")
        _pid, wid = _seed_one_pending(c, feature_slug="sessions")
        c.patch(f"/api/pm/work-items/{wid}/overlay",
                json={"holdKind": "owner_action", "holdReason": "owner only"},
                headers=IDENTITY_HEADERS)
        r = c.post(f"/api/pm/work-items/{wid}/hold/release")   # lan@local, no header
        assert r.status_code == 422 and r.json()["detail"]["error"] == "hold_not_permitted"


def test_reconcile_save_releases_hold_on_human_board_edit():
    """The reconciler's project-items save runs THE release rule (spec §7)."""
    with _client() as c:
        _register(c, "Viberpit", repo="o/r")
        _pid, wid = _seed_one_pending(c, feature_slug="sessions")
        c.patch(f"/api/pm/work-items/{wid}/overlay",
                json={"holdKind": "owner_action", "holdReason": "owner only"},
                headers=IDENTITY_HEADERS)
        from server.db import db_session
        from sqlalchemy import select
        from server.db.models import GhProject, GhProjectItem, Project, WorkItem
        from server.pm.gh_projects import GhProjectItemView

        async def _seed_board():
            async with db_session() as db:
                p = (await db.execute(select(Project).where(Project.slug == "viberpit"))).scalar_one()
                gh = GhProject(project_id=p.id, gh_node_id="PVT_1", gh_project_number=1, title="b")
                db.add(gh)
                await db.flush()
                db.add(GhProjectItem(gh_project_id=gh.id, work_item_id=wid,
                                     gh_item_node_id="PVTI_1", status="Needs human"))
                return gh.id
        gh_id = c.portal.call(_seed_board)
        orch = c.app.state.sessions
        view = GhProjectItemView(item_node_id="PVTI_1", repo="o/r", number=1, kind="issue",
                                 status="Ready", score=None, stage=None, updated_at=None)
        edits = c.portal.call(lambda: orch._pm_sync_save_project_items(gh_id, [view]))
        assert [(e.field, e.new) for e in edits] == [("status", "Ready")]

        async def _item():
            async with db_session() as db:
                return (await db.execute(select(WorkItem).where(WorkItem.id == wid))).scalar_one().hold_kind
        assert c.portal.call(_item) is None
```

Read `_seed_one_pending` to confirm the seeded item is `o/r#1` (`number=1` default) — the view's `repo`/`number` must match the mirror row's work item. Read `mirror_store.upsert_project_items` to confirm a status change on an existing row with no recent own-write yields a `HumanEdit("o/r#1", "status", "Needs human", "Ready")`; if the field name differs, use the mirror's name in both the test and `holds.release_by_signals`.

Add to `server/tests/test_assistant_policy.py`'s parametrized table: `("POST", "/api/pm/work-items/w1/hold/release", False),` beside the overlay row.

- [ ] **Step 2: Run to verify they fail**

Run: `… -m pytest server/tests/test_pm_api.py -q -k "hold_release or reconcile_save" && … -m pytest server/tests/test_assistant_policy.py -q`
Expected: the three new API tests FAIL (404 / no release); the policy test PASSES already (deny-by-default) — keep the row anyway, it is the regression pin.

- [ ] **Step 3: Implement**

`server/api/pm.py`, after `delete_work_item_dep`:

```python
@router.post("/work-items/{item_id}/hold/release")
async def release_work_item_hold(
    item_id: str,
    request: Request,
    identity: str = Depends(current_identity),
    db=Depends(get_db),
) -> dict:
    """The Retry signal (spec 2026-09-18 §7): a person releases an owner_action
    hold so the item re-enters the queue at the product default. The other two
    signals (board edit, body edit) run in the reconcile pass through the same
    rule module. Idempotent on an unheld item."""
    from server.pm.holds import HoldPermissionError, actor_class, actor_for_request, clear_hold
    wi = (await db.execute(
        select(WorkItem).where(WorkItem.id == item_id))).scalar_one_or_none()
    if wi is None:
        raise HTTPException(status_code=404, detail={"error": "not_found"})
    actor = actor_for_request(request)
    released = wi.hold_kind
    try:
        clear_hold(wi, actor=actor)
    except HoldPermissionError as e:
        raise HTTPException(status_code=422, detail={
            "error": "hold_not_permitted", "message": str(e)}) from e
    if released is not None:
        await db.flush()
        await record_audit(db=db, type="pm.hold.release", identity=identity, detail={
            "workItemId": item_id, "kind": released, "actorClass": actor_class(actor),
            "signal": "retry"})
    return {"ok": True, "released": released, "item": _work_item_to_dict(wi)}
```

`_pm_sync_save_project_items` in the orchestrator: import `release_by_signals` from `server.pm.holds` inside the method and add `release_by_signals(s, human_edits=edits, now=now)` after `mirror_store.back_sync_stage(s, edits)` inside `_sync`, with a two-line comment naming spec §7.

- [ ] **Step 4: Run the tests**

Run: `… -m pytest server/tests/test_pm_api.py server/tests/test_assistant_policy.py server/tests/test_pm_holds.py -q && … -m mypy server/api/pm.py --ignore-missing-imports`
Expected: PASS / clean.

- [ ] **Step 5: Commit**

```bash
git add server/api/pm.py server/sessions/orchestrator.py server/tests/test_pm_api.py server/tests/test_assistant_policy.py
printf 'feat(pm): owner_action holds release on board edit, body edit, or Retry\n\nSpec 2026-09-18 §7, decision 6. Closes #1540.\n\nCo-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>\n' > /tmp/c6.txt
git commit -F /tmp/c6.txt
```

---

### Task 7: Docs, the four gates, PR

**Files:**
- Modify: `docs/product/features/product-management-layer.md` (Holds paragraph), `docs/product/features/autopilot-backlog-runner.md` (retry-days / cooldown wording)

- [ ] **Step 1: Docs**

In `product-management-layer.md`'s Holds paragraph append: "An `owner_action` hold ends only on a human signal (spec 2026-09-18 §7): a board edit to Ready or Backlog, an issue body edit (the fingerprint no longer matches), or `POST /api/pm/work-items/{id}/hold/release` (the Retry tap) — all three through `server/pm/holds.py:release_by_signals` / `clear_hold`, each audited as `pm.hold.release` with its `signal`. The queue skips held items as `held`; the Backlog Runner's give-up writes the hold (`hold_by = runner`) and its `gave_up` row survives only until the hold is gone." In `autopilot-backlog-runner.md`, replace every description of `needs_human_retry_days` / the retry window / `needs_human_cooldown` with the sentence above's runner half (grep the file for `retry`, `cooldown`, `needs_human`).

- [ ] **Step 2: The four gates + ratchet**

```bash
P=/home/tony/GitHub/viberpit/.venv/bin/python
env -i HOME=/tmp/vp-home PATH="$PATH" CCSWITCH_DEV=1 PYTHONPATH=$PWD/src $P -m ruff check src/ server/
env -i HOME=/tmp/vp-home PATH="$PATH" CCSWITCH_DEV=1 PYTHONPATH=$PWD/src $P -m mypy server/ --ignore-missing-imports 2>&1 | tail -3
env -i HOME=/tmp/vp-home PATH="$PATH" CCSWITCH_DEV=1 PYTHONPATH=$PWD/src $P -m pytest tests/ -q 2>&1 | tail -3
$P scripts/brand_ratchet.py
grep -rn "needs_human_at\|needs_human_retry_days\|needs_human_cooldown\|NEEDS_HUMAN_RETRY_DAYS_DEFAULT\|runner_row_maps" server/ src/ scripts/ --include=*.py | grep -v "label_retirement\|retire_claim_labels"
```

The last grep must print nothing (the two excluded files are removed by series item 6). The controller runs `server/tests/` detached.

- [ ] **Step 3: Commit, push, PR**

```bash
git add docs/product/features/product-management-layer.md docs/product/features/autopilot-backlog-runner.md
printf 'docs(pm): release signals and the held queue skip\n\nCo-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>\n' > /tmp/c7.txt
git commit -F /tmp/c7.txt
```

The controller pushes, checks in-flight PRs on the touched files, opens the PR (non-draft, body via file, `Fixes #1540`), and arms `gh pr merge --squash --auto`.

---

## Self-review

**Spec coverage (3a slice):** §3 `gave_up` row for slot accounting only, `needs_human_at` and `needs_human_retry_days` removed → Tasks 2, 3, 4, 5. §7 queue skip `held`, entry carries `hold_kind` → Task 2; runner writes the hold → Task 4; three release signals in one implementation → Tasks 1, 6; Retry endpoint (audited) → Task 6; `needs_attention` correction → Task 5; "release re-enters at the product default tier" → follows from the row drop in Task 4 (the next refill spawns at the product default; the ladder is item 4). Dialog/chips/leaf fields → plan 3b. Not in scope: `decision` holds are parked by the queue (Task 2) but never released here — item 5 resolves them via accept/reject.

**Type consistency:** `held_refs(db) -> frozenset[str]` (Task 1) is what `_backlog_held_targets` returns (Task 4); `release_by_signals(db, *, human_edits, now) -> list[tuple[WorkItem, str]]` (Task 1) is called with exactly those keywords in Task 6; `QueueRow.hold_kind` (Task 2) is populated in Task 3 and read by `skip_reason` (Task 2); `LIVE_SESSION_STATUSES` (Task 5) replaces the three unions named there; `FAILED_TURN_SESSION_STATUSES` is used by `tree.py`'s in-flight branch (Task 5).

**Placeholders:** none. Two "read first" notes (the mirror's status field name; `_seed_row`'s signature in the runner tests) are verification instructions with the file and line to read.

**Removed, not left half-dead:** `NEEDS_HUMAN_RETRY_DAYS_DEFAULT`, `runner_row_maps`, `_expire_give_ups`, `ItemFacts.needs_human_at`, `QueueRow.needs_human_at`, the config field (key retired). Task 7's grep is the guard.

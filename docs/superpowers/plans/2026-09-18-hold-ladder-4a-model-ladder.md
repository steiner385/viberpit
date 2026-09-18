# Hold Ladder — 4a: the model ladder and `dispatch_kind` on outcomes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A bounced issue climbs one model tier per bounce instead of parking after a fixed attempt count, and only a bounce above the product's top tier hands it to the owner.

**Architecture:** One pure rung rule in `server/pm/outcomes.py` beside the existing capability floor. The Backlog Runner's sweep, on a no-PR or PR-Doctor bounce, asks a new orchestrator seam to write a `stronger_model` hold (next rung exists) or an `owner_action` hold (bounced at the top) on every issue of the batch; the runner row keeps its `attempts` counter for provenance but no longer caps anything, and `max_attempts` leaves the product policy (replaced by `ladder_top`). The spawn, seeing a `stronger_model` hold on the primary, pins the model at the rung computed from the previous attempt's recorded model, then releases the hold (`signal = "dispatch"`). `dispatch_outcomes` gains `dispatch_kind` so item 4b's investigation dispatches never count as bounces.

**Tech Stack:** Python 3.12 / FastAPI / SQLAlchemy async · alembic revision as record (reconcile applies) · pytest.

**Spec:** `docs/superpowers/specs/2026-09-18-hold-ladder-design.md` §4 (the ladder) and the `dispatch_kind` sentence of §3. Evidence dispatch (§5) is plan 4b; decision proposals (§6, `decision_proposed` exclusion) are item 5.

## Global Constraints

- Constitution VI: ONE tier ordering (`model_catalog.TIER_ORDER` / `tier_rank`); ONE rung rule (`outcomes.next_rung`); holds written only through `server/pm/holds.py`; the spawn's model decision still goes through `_resolve_spawn_model` / `pick_model`.
- Nullable column only (reconcile ALTER-ADDs it); migration number: `0050` — re-check `ls server/db/migrations/versions/ | sort | tail -1` just before committing and renumber off master's newest.
- Audit every hold write (`pm.hold.set` `{workItemId, kind, reason, actorClass: "system"}` identity `runner`, `via="runner"`) and every release (`pm.hold.release` with `signal`).
- Decision 2 (spec §2): climb one tier per bounce; park only after a bounce above the top allowed tier; the top defaults to `fable`. No approval step for ladder dispatches.
- Decision 6: no timer; an `owner_action` hold releases only on a human signal (already built in 3a).
- No absolute date literals in tests; the runner tests use the injected clock.
- Brand ratchet must not grow (no new `CCSWITCH_` literals). Commit trailer `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>` — that literal. Messages via a file and `git commit -F`; keep "release"/"deploy" and "prod"/"production" from appearing together in any command text.
- Test invocation: `env -i HOME=/tmp/vp-home PATH="$PATH" CCSWITCH_DEV=1 PYTHONPATH=$PWD/src /home/tony/GitHub/viberpit/.venv/bin/python -m pytest …`; never the whole `server/tests/` from a task.
- Branch `hold-ladder-4` off `origin/master` (created at 92fe200b; rebase before the PR if 3b's #1957 lands first).

## File structure

| File | Responsibility |
|---|---|
| `server/db/models.py` | `DispatchOutcome.dispatch_kind` (nullable; `DISPATCH_KINDS = ("implement", "investigate")`) |
| `server/db/migrations/versions/0050_dispatch_kind.py` | the record |
| `server/automations/context.py` | `record_outcome(..., dispatch_kind="implement")` writes the column |
| `server/pm/outcomes.py` | `bounces_stmt` counts implement rows only; `next_rung()`; `top_tier()`; `last_attempt_model()` |
| `server/automations/product_policy.py` | `ladder_top` field (enum over `TIER_ORDER`, default `fable`); `max_attempts` removed from the schema |
| `server/automations/backlog_runner.py` | `escalate` seam; sweep bounce branches use the ladder; the attempt cap is gone; the runner row carries the APPLIED model; `max_attempts` retired |
| `server/sessions/orchestrator.py` | `_backlog_escalate` seam (writes the hold); legacy seed drops `max_attempts`; `_backlog_spawn` pins the rung and releases the `stronger_model` hold |
| `server/pm/holds.py` | `RELEASE_SIGNALS` + `"dispatch"`; `set_runner_stronger_hold` |
| `docs/product/features/autopilot-backlog-runner.md`, `product-management-layer.md` | the ladder |
| Tests | `test_dispatch_outcome_vocabulary.py`, `test_automation_context_outcomes.py`, `test_pm_outcomes.py`, `test_product_policy.py`, `test_backlog_runner_loop.py`, `test_pm_orchestrator_seams.py`, `test_automation_spawn_model.py`, `test_pm_holds.py`, `test_work_item_holds_migration.py`-style test for 0050 |

---

### Task 1: `dispatch_kind` on outcomes; bounces count implement rows only

**Files:**
- Modify: `server/db/models.py` (near `DISPATCH_OUTCOMES`, and `DispatchOutcome`), `server/automations/context.py:417-455` (`record_outcome`), `server/pm/outcomes.py:47-57` (`bounces_stmt`)
- Create: `server/db/migrations/versions/0050_dispatch_kind.py`
- Test: `server/tests/test_dispatch_outcome_vocabulary.py`, `server/tests/test_automation_context_outcomes.py`, `server/tests/test_pm_outcomes.py`, `server/tests/test_dispatch_kind_migration.py`

**Interfaces:**
- `DISPATCH_KINDS: tuple[str, ...] = ("implement", "investigate")`; `DispatchOutcome.dispatch_kind: Mapped[str | None]` (nullable, no default at the DB level; `None` reads as `implement`).
- `record_outcome(self, target, *, product_slug, session_id, model, attempt_no, dispatch_kind="implement", _db=None)` writes it.
- `bounces_stmt` adds `or_(DispatchOutcome.dispatch_kind.is_(None), DispatchOutcome.dispatch_kind == "implement")` to its `where`.

- [ ] **Step 1: Write the failing tests**

`server/tests/test_dispatch_outcome_vocabulary.py` — append:

```python
def test_dispatch_kinds_vocabulary():
    from server.db.models import DISPATCH_KINDS, DispatchOutcome
    assert DISPATCH_KINDS == ("implement", "investigate")
    assert DispatchOutcome.__table__.c.dispatch_kind.nullable is True
```

`server/tests/test_automation_context_outcomes.py` — append (reuse its `db_env` fixture and the way `test_record_then_finalize_writes_one_row` builds a context and reads rows back):

```python
async def test_record_outcome_writes_dispatch_kind_default_implement(db_env):
    ctx = _ctx()  # the module's context builder — read test_record_then_finalize_writes_one_row
    await ctx.record_outcome("o/r#1", product_slug=None, session_id="s1", model="sonnet", attempt_no=0)
    await ctx.record_outcome("o/r#2", product_slug=None, session_id="s2", model="sonnet", attempt_no=0,
                             dispatch_kind="investigate")
    rows = {r.target_ref: r for r in await _all_rows()}  # the module's row reader
    assert rows["o/r#1"].dispatch_kind == "implement"
    assert rows["o/r#2"].dispatch_kind == "investigate"
```

`server/tests/test_pm_outcomes.py` — extend `test_bounces_stmt_filters_on_finalised_outcomes` (read it) so the compiled SQL also contains `dispatch_kind` and a query over seeded rows counts an `investigate` row as zero bounces: seed two finalised `no_pr` rows for the same ref/fingerprint, one `dispatch_kind="investigate"`, one `None`; `rows_to_bounces` must give 1.

`server/tests/test_dispatch_kind_migration.py` — copy the shape of `server/tests/test_work_item_holds_migration.py` (load the revision by path, create a bare `dispatch_outcomes` table without the column, run `upgrade()` under `Operations.context`, assert the column exists; run `upgrade()` twice to prove the add is guarded).

- [ ] **Step 2: Run to verify they fail**

Run: `… -m pytest server/tests/test_dispatch_outcome_vocabulary.py server/tests/test_automation_context_outcomes.py server/tests/test_pm_outcomes.py server/tests/test_dispatch_kind_migration.py -q`
Expected: FAIL (`ImportError: DISPATCH_KINDS`).

- [ ] **Step 3: Implement**

`server/db/models.py`, beside `DISPATCH_OUTCOMES`:

```python
# What a dispatch was for (spec 2026-09-18 §3/§5). ``implement`` opens a PR;
# ``investigate`` (series item 4b) reproduces or correlates and never counts as a
# bounce. NULL on rows written before the column existed reads as ``implement``.
DISPATCH_KINDS: tuple[str, ...] = ("implement", "investigate")
```

and on `DispatchOutcome` after `first_pass_merge`: `dispatch_kind: Mapped[str | None] = mapped_column(String, nullable=True)  # DISPATCH_KINDS`.

`context.py` `record_outcome`: add the keyword `dispatch_kind: str = "implement"`, validate `if dispatch_kind not in DISPATCH_KINDS: raise ValueError(...)`, and pass `dispatch_kind=dispatch_kind` into the `DispatchOutcome(...)` insert. `outcomes.py` `bounces_stmt`: add the `or_` clause (import `or_` from sqlalchemy). Migration `0050_dispatch_kind.py`: `revision = "0050_dispatch_kind"`, `down_revision = "0049_work_item_holds"`, guarded `add_column("dispatch_outcomes", sa.Column("dispatch_kind", sa.String(), nullable=True))` using the same `_existing()` inspector idiom as 0049.

- [ ] **Step 4: Run the tests plus the chain and reconcile guards**

Run: `… -m pytest server/tests/test_dispatch_outcome_vocabulary.py server/tests/test_automation_context_outcomes.py server/tests/test_pm_outcomes.py server/tests/test_dispatch_kind_migration.py server/tests/test_pending_model_reason_column.py::test_migration_chain_is_linear_with_exactly_one_head server/tests/test_schema_reconcile_coverage.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add server/db/models.py server/db/migrations/versions/0050_dispatch_kind.py server/automations/context.py server/pm/outcomes.py server/tests/test_dispatch_outcome_vocabulary.py server/tests/test_automation_context_outcomes.py server/tests/test_pm_outcomes.py server/tests/test_dispatch_kind_migration.py
printf 'feat(pm): dispatch_kind on outcomes; only implement dispatches count as bounces\n\nSpec 2026-09-18 §3/§5.\n\nCo-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>\n' > /tmp/c1.txt
git commit -F /tmp/c1.txt
```

---

### Task 2: The rung rule and the `ladder_top` policy field (`max_attempts` retired)

**Files:**
- Modify: `server/pm/outcomes.py` (after `tier_floor_for_dispatch`), `server/automations/product_policy.py:66-72` (schema), `server/automations/backlog_runner.py:286` (`retired_config_keys` comment only — the key is a PRODUCT policy key, see below), `server/sessions/orchestrator.py:6370-6382` (legacy seed)
- Test: `server/tests/test_pm_outcomes.py`, `server/tests/test_product_policy.py`

**Interfaces:**
- `next_rung(previous_model: str | None, *, floor: str | None, default_model: str | None, top: str) -> str | None` — pure. Start = the tier of `previous_model`; if it has no tier (`None`, `"auto"`, `"default"`, `"best"`, unknown) start = `"sonnet"`. Candidate = the tier one above start. Raise the candidate to `max(tier_rank(floor), tier_rank(default_model))` when those rank higher (a pinned default or a capability floor is never undercut). Return `None` when the candidate's rank exceeds `tier_rank(top)` (bounced at the top) — also when `top` has no tier. Never returns a tier below the start.
- `top_tier(policy: dict) -> str` — `policy.get("ladder_top") or TIER_ORDER[-1]`, validated against `TIER_ORDER`.
- `last_attempt_model(db, target_ref: str) -> str | None` — the `model` of the newest finalised non-`superseded` `implement` outcome row for the ref (order by `ended_at desc, created_at desc`), `None` when there is none.
- Product policy: `ConfigField(key="ladder_top", label="Ladder top tier", type="enum", default="fable", choices=list(TIER_ORDER), help="The strongest model the Backlog Runner climbs to on repeated bounces; a bounce at this tier hands the issue to the owner.")` replaces the `max_attempts` field. `effective_policy` no longer returns `max_attempts`; the orchestrator's one-shot legacy seed (`server/sessions/orchestrator.py:~6378`) stops copying it (read the block: it maps old global runner config into product policies; drop the `max_attempts` line and its `_int` call).

- [ ] **Step 1: Write the failing tests**

`server/tests/test_pm_outcomes.py` — append:

```python
def test_next_rung_climbs_one_tier_from_the_previous_attempt():
    from server.pm.outcomes import next_rung
    assert next_rung("sonnet", floor=None, default_model="auto", top="fable") == "opus"
    assert next_rung("claude-opus-4-1", floor=None, default_model="auto", top="fable") == "fable"
    # unresolvable previous → climb from sonnet
    assert next_rung("auto", floor=None, default_model="auto", top="fable") == "opus"
    assert next_rung(None, floor=None, default_model="auto", top="fable") == "opus"
    # never below the capability floor or a pinned default
    assert next_rung("haiku", floor="opus", default_model="auto", top="fable") == "opus"
    assert next_rung("haiku", floor=None, default_model="fable", top="fable") == "fable"
    # bounced at the top
    assert next_rung("fable", floor=None, default_model="auto", top="fable") is None
    assert next_rung("opus", floor=None, default_model="auto", top="opus") is None
    assert next_rung("sonnet", floor=None, default_model="auto", top="best") is None


def test_top_tier_reads_the_policy_and_defaults_to_the_last_tier():
    from server.pm.outcomes import top_tier
    from server.sessions.model_catalog import TIER_ORDER
    assert top_tier({}) == TIER_ORDER[-1]
    assert top_tier({"ladder_top": "opus"}) == "opus"
    assert top_tier({"ladder_top": "bogus"}) == TIER_ORDER[-1]
```

and, in the file's async section (it has `db_env`-style tests for `tier_floor_for_dispatch`), a `test_last_attempt_model_reads_the_newest_finalised_implement_row(db_env)` seeding three `DispatchOutcome` rows for `o/r#1`: an older `no_pr` on `sonnet`, a newer `no_pr` on `opus`, and a `running` row on `fable` → expect `"opus"`; plus an `investigate` row on `fable` newer than all → still `"opus"`.

`server/tests/test_product_policy.py` — update `test_schema_keys_and_defaults` (it pins the key list; replace `max_attempts` with `ladder_top`, default `"fable"`) and add:

```python
def test_ladder_top_choices_are_the_tier_order_and_max_attempts_is_gone():
    from server.automations.product_policy import PRODUCT_POLICY_SCHEMA, effective_policy
    from server.sessions.model_catalog import TIER_ORDER
    field = next(f for f in PRODUCT_POLICY_SCHEMA if f.key == "ladder_top")
    assert field.choices == list(TIER_ORDER) and field.default == "fable"
    assert "max_attempts" not in effective_policy({"dispatch": "auto"})
    # a stored policy still carrying the retired key validates (unknown keys are dropped, not rejected)
    assert effective_policy({"dispatch": "auto", "max_attempts": 2})["ladder_top"] == "fable"
```

Check how `validate_config` treats unknown keys (read `server/automations/config_schema.py` or wherever it lives); if unknown keys raise, `effective_policy` must strip `max_attempts` before validating (a stored policy from before this change must keep working).

- [ ] **Step 2: Run to verify they fail**

Run: `… -m pytest server/tests/test_pm_outcomes.py server/tests/test_product_policy.py -q`
Expected: FAIL (`ImportError: next_rung`).

- [ ] **Step 3: Implement**

`server/pm/outcomes.py`:

```python
def top_tier(policy: dict) -> str:
    """The strongest tier the ladder climbs to for this product (spec §4, decision 2)."""
    top = str((policy or {}).get("ladder_top") or "")
    return top if top in TIER_ORDER else TIER_ORDER[-1]


def next_rung(previous_model: str | None, *, floor: str | None, default_model: str | None,
              top: str) -> str | None:
    """THE ladder rule (spec 2026-09-18 §4): one tier above the previous attempt,
    never below the capability floor or a pinned product default, ``None`` when
    the climb would pass ``top`` — a bounce at the top is the owner's.

    An unresolvable previous model (``auto``, ``default``, ``best``, unknown) climbs
    from ``sonnet``, so a first bounce under ``auto`` lands on ``opus``."""
    start = tier_rank(previous_model)
    if start is None:
        start = TIER_ORDER.index("sonnet")
    candidate = start + 1
    for bound in (floor, default_model):
        r = tier_rank(bound)
        if r is not None and r > candidate:
            candidate = r
    top_rank = tier_rank(top)
    if top_rank is None or candidate > top_rank:
        return None
    return TIER_ORDER[candidate]


async def last_attempt_model(db, target_ref: str) -> str | None:
    """The model the newest finalised implement dispatch for *target_ref* ran on
    (the ladder climbs from here). ``superseded`` and ``running`` rows carry no
    verdict and are skipped; investigate rows are not attempts."""
    row = (await db.execute(
        select(DispatchOutcome.model).where(
            DispatchOutcome.target_ref == target_ref,
            DispatchOutcome.outcome.not_in(UNFINISHED_OUTCOMES),
            or_(DispatchOutcome.dispatch_kind.is_(None), DispatchOutcome.dispatch_kind == "implement"))
        .order_by(DispatchOutcome.ended_at.desc().nullslast(), DispatchOutcome.created_at.desc())
        .limit(1))).first()
    return row[0] if row else None
```

(`tier_rank` is imported from `server.sessions.model_catalog` beside `TIER_ORDER, model_tier`; check `UNFINISHED_OUTCOMES` contains `running` and `superseded`.) `product_policy.py`: replace the `max_attempts` `ConfigField` with the `ladder_top` one (import `TIER_ORDER`); if `validate_config` rejects unknown keys, drop `max_attempts` from `raw` in `effective_policy` before validating, with a comment naming this plan. Orchestrator legacy seed: delete the `max_attempts` mapping lines. `backlog_runner.py:286` `retired_config_keys` already lists `max_attempts` as a retired RUNNER key — leave it; update the comment above it if it says the key moved to the product policy.

- [ ] **Step 4: Run the tests**

Run: `… -m pytest server/tests/test_pm_outcomes.py server/tests/test_product_policy.py server/tests/test_product_policy_edits.py server/tests/test_backlog_policy_seams.py -q`
Expected: PASS (fix any test that pins `max_attempts` in a policy literal by dropping the key).

- [ ] **Step 5: Commit**

```bash
git add server/pm/outcomes.py server/automations/product_policy.py server/sessions/orchestrator.py server/automations/backlog_runner.py server/tests/test_pm_outcomes.py server/tests/test_product_policy.py server/tests/test_product_policy_edits.py server/tests/test_backlog_policy_seams.py
printf 'feat(pm): the ladder rung rule and the ladder_top policy field; max_attempts retired\n\nSpec 2026-09-18 §4, decision 2.\n\nCo-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>\n' > /tmp/c2.txt
git commit -F /tmp/c2.txt
```

---

### Task 3: The sweep climbs instead of counting attempts; the orchestrator writes the hold

**Files:**
- Modify: `server/automations/backlog_runner.py` (constructor seams ~548-600; `_sweep_one` bounce branches ~1620-1660; the spawn's row write ~1316 so the row carries the APPLIED model)
- Modify: `server/sessions/orchestrator.py` (`_backlog_needs_human` sibling `_backlog_escalate`; loop construction ~1057)
- Modify: `server/pm/holds.py` (`set_runner_stronger_hold`)
- Test: `server/tests/test_backlog_runner_loop.py`, `server/tests/test_pm_orchestrator_seams.py`, `server/tests/test_pm_holds.py`

**Interfaces:**
- Seam `escalate: Callable[[str, str, str | None], Awaitable[None]]` — `escalate(target, reason, rung)`; `rung=None` means "bounced at the top". Default stub does nothing. The orchestrator binds `_backlog_escalate`, which for every mirrored issue of the batch writes `set_runner_stronger_hold(wi, reason=..., now=...)` when `rung` is a tier, else `set_runner_hold(wi, reason=..., now=...)` (owner_action) and then the existing best-effort `announce_needs_human` comment; one `pm.hold.set` audit per issue (identity `runner`, `via="runner"`); unmirrored refs logged and skipped.
- `holds.set_runner_stronger_hold(wi, *, reason, now)` = `set_hold(kind="stronger_model", actor=RUNNER_ACTOR, ...)`.
- `_sweep_one`, no-PR branch (today: `attempts += 1; if attempts >= max_attempts: _give_up(...)`): becomes
  ```python
  attempts = st.get("attempts", 0) + 1
  if not await self._session_ever_ran(st):
      ...unchanged release ("session never ran a turn")...
  previous = st.get("model")
  rung = next_rung(previous, floor=None, default_model=self._policy_for(st.get("product"))["default_model"],
                   top=top_tier(self._policy_for(st.get("product"))))
  await self._write_state(target, st, None, outcome={"outcome": "no_pr", "cause": "session ended without a mergeable PR", "fallback": fallback})
  if rung is None:
      await self._escalate(target, f"bounced at {model_tier(previous) or 'the top'}: no mergeable PR after {attempts} attempts", None)
  else:
      await self._escalate(target, f"bounced at {model_tier(previous) or 'sonnet'}; next attempt on {rung}", rung)
  ```
  The row is DELETED on a bounce (the item returns to the pool as a `stronger_model` hold, queue-eligible; the next refill re-claims it and the spawn pins the rung). The `attempts` count survives on the outcome rows (`attempt_no`), not on the row. `_give_up` is no longer called from the no-PR branch. The `pr_gave_up` branch (PR Doctor gave up) takes the same ladder path with reason `"PR Doctor gave up; next attempt on <rung>"` / `"PR Doctor gave up at the top"`; the `abandoned` branch (PR closed unmerged) still calls `_give_up` with the PR link in the reason (a person closed it — spec §4).
- The runner row's `model` must be the APPLIED model: at `backlog_runner.py:~1316` the state update already computes `model = (session.model ...) or model or intent["model"]` — confirm that value (not `default_model`) is what lands in `"model"`; if the `intent["model"]` fallback is `"auto"`, the sweep's `next_rung` still works (unresolvable → sonnet start) but prefer the session's model. Add a test.
- Floor at bounce time: the sweep passes `floor=None` (the spawn re-applies the capability floor when it pins the rung — Task 4 — so a rung below the floor is raised there; the sweep's job is only "is there a next rung").

- [ ] **Step 1: Write the failing tests**

In `server/tests/test_backlog_runner_loop.py` (read `build_loop`, `make_config`, `policy`, `_seed_row`, `_set_owned_state`, `rec` recorders; add `rec.escalations: list[tuple[str, str, str | None]]` and an `escalate` seam that appends and is passed to the loop as `escalate=`), replace `test_max_attempts_give_up_finalises_needs_human` and `test_max_attempts_is_read_from_the_products_policy` with:

```python
def test_a_no_pr_bounce_climbs_one_rung_and_frees_the_row(tmp_path):
    """Spec 2026-09-18 §4: no attempt cap; a bounce writes a stronger_model hold
    (via the escalate seam) and deletes the runner row so the item re-queues."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'l.db'}"

    async def run():
        await init_db(db_url=db_url)
        await _seed_row()
        loop, rec, ctx = build_loop(config=make_config(max_concurrent=1), candidates=[batch("r#1")],
                                    policies={DEFAULT_PRODUCT: policy(max_in_flight=1000)},
                                    spawn_result=SimpleNamespace(id="sess-1", model="sonnet"))
        await loop.tick()                       # spawn on sonnet
        rec.session_ended["sess-1"] = True      # the harness's "session ended, no PR" switch — read how existing no_pr tests do it
        await loop.tick()                       # sweep: bounce
        assert rec.escalations == [("r#1", "bounced at sonnet; next attempt on opus", "opus")]
        assert rec.needs_humans == []           # no owner hand-off
        assert not await ctx.get_target_state("r#1")   # row gone: back in the pool
        rows = await _outcomes("r#1")
        assert rows[-1].outcome == "no_pr"

    asyncio.run(run())


def test_a_bounce_at_the_top_hands_the_issue_to_the_owner(tmp_path):
    ...same shape with spawn_result model="fable" (or policy(ladder_top="opus") and model="opus")...
    assert rec.escalations == [("r#1", "bounced at fable: no mergeable PR after 1 attempts", None)]
    assert not await ctx.get_target_state("r#1")


def test_pr_doctor_give_up_climbs_the_ladder_too(tmp_path):
    ...seed the pr-doctor gave_up event like test_terminal_watch_gave_up_unclaims_and_needs_human does...
    assert rec.escalations[0][2] == "opus" and rec.needs_humans == []


def test_pr_closed_unmerged_still_hands_off_with_the_pr_link(tmp_path):
    ...pr_status returning state="closed" with url → rec.needs_humans == [("r#1", reason)] and "http" in reason...
```

Write every `...` body out against the harness — the brief-level sketch is not the test. `server/tests/test_pm_orchestrator_seams.py`: next to `test_backlog_needs_human_writes_the_hold_and_audit_row`, add `test_backlog_escalate_writes_a_stronger_model_hold_for_a_rung_and_owner_action_at_the_top` seeding a mirrored issue, calling `orch._backlog_escalate("o/r#1", "bounced at sonnet; next attempt on opus", "opus")` → `hold_kind == "stronger_model"`, `hold_by == "runner"`, one `pm.hold.set` audit; then `orch._backlog_escalate("o/r#1", "bounced at fable", None)` → `hold_kind == "owner_action"` (system actor may replace) and the comment seam invoked (monkeypatch `announce_needs_human`, assert called once for the top case, never for the rung case). `test_pm_holds.py`: `set_runner_stronger_hold` sets kind/by/reason.

- [ ] **Step 2: Run to verify they fail**

Run: `… -m pytest server/tests/test_backlog_runner_loop.py -q -k "bounce or climbs or hands_the_issue or pr_closed" && … -m pytest server/tests/test_pm_orchestrator_seams.py -q -k escalate`
Expected: FAIL (`TypeError: unexpected keyword 'escalate'` / `AttributeError`).

- [ ] **Step 3: Implement**

`holds.py`: `def set_runner_stronger_hold(wi, *, reason, now): set_hold(wi, kind="stronger_model", reason=reason, actor=RUNNER_ACTOR, now=now)`. `backlog_runner.py`: add the seam type `Escalate = Callable[[str, str, str | None], Awaitable[None]]`, constructor param `escalate: Escalate | None = None`, stub `_stub_escalate`; import `next_rung`, `top_tier` from `server.pm.outcomes` and `model_tier` from `server.sessions.model_catalog`; rewrite the two bounce branches per the Interfaces block; delete the `max_attempts` read and the `# to pick the target up` comment block at ~454 that describes the cap. `orchestrator.py`: `_backlog_escalate` modelled on `_backlog_needs_human` (same batch resolution, same audit, `set_runner_stronger_hold` vs `set_runner_hold`, comment only for the owner case), bound as `escalate=self._backlog_escalate` at the loop construction. Update `announce_needs_human`'s reason text if it hardcodes "max attempts" anywhere (grep).

- [ ] **Step 4: Run the runner and seam suites**

Run: `… -m pytest server/tests/test_backlog_runner_loop.py server/tests/test_pm_orchestrator_seams.py server/tests/test_pm_holds.py server/tests/test_backlog_runner_automation.py server/tests/test_backlog_ownership.py server/tests/test_backlog_runner_e2e.py -q && … -m mypy server/automations/backlog_runner.py --ignore-missing-imports`
Expected: PASS / clean. Tests that asserted `needs_humans` after a no-PR bounce now assert `escalations`.

- [ ] **Step 5: Commit**

```bash
git add server/pm/holds.py server/automations/backlog_runner.py server/sessions/orchestrator.py server/tests/test_backlog_runner_loop.py server/tests/test_pm_orchestrator_seams.py server/tests/test_pm_holds.py server/tests/test_backlog_runner_automation.py server/tests/test_backlog_ownership.py server/tests/test_backlog_runner_e2e.py
printf 'feat(runner): a bounce climbs one model tier; only a bounce at the top hands off\n\nSpec 2026-09-18 §4. The attempt cap is gone.\n\nCo-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>\n' > /tmp/c3.txt
git commit -F /tmp/c3.txt
```

---

### Task 4: The spawn pins the rung and releases the `stronger_model` hold

**Files:**
- Modify: `server/sessions/orchestrator.py` (`_backlog_spawn` ~6453+ where `floor` is resolved and `config["default_model"]` is overlaid; a new `_backlog_ladder_rung` seam beside `_backlog_tier_floor`)
- Modify: `server/pm/holds.py` (`RELEASE_SIGNALS` + `"dispatch"`; `release_on_dispatch(db, refs, *, now) -> list[str]`)
- Test: `server/tests/test_automation_spawn_model.py`, `server/tests/test_pm_holds.py`

**Interfaces:**
- `_backlog_ladder_rung(product_slug, primary_ref, *, default_model: str, floor: str | None) -> str | None` — under `db_session()`: if the primary's mirrored issue has `hold_kind == "stronger_model"`, return `next_rung(await last_attempt_model(db, primary_ref), floor=floor, default_model=default_model, top=top_tier(policy))` (policy from `_backlog_last_policies` / the product's effective policy); else `None`. Best-effort like `_backlog_tier_floor` (log + `None` on failure). Injectable seam for the spawn tests.
- In `_backlog_spawn`, after `floor` is resolved: `rung = await self._backlog_ladder_rung(batch.product, batch.primary, default_model=default_model, floor=floor)`; when `rung` is set, `config = {**config, "default_model": rung}` (a pinned pick: the ladder is the decision, and `pick_model` treats a pinned alias as an operator's choice), and the spawned-event detail gains `"ladder_rung": rung`. A rung of `None` with a `stronger_model` hold present cannot happen (the sweep wrote `owner_action` instead) — but if it does (policy changed since), log `backlog_runner.ladder_rung_missing` and spawn at the default.
- After the spawn succeeds (the session record exists), `release_on_dispatch(db, batch.issues, now=...)` clears every `stronger_model` hold on the batch's issues as `RUNNER_ACTOR` with a `pm.hold.release` audit `{workItemId, kind: "stronger_model", actorClass: "system", signal: "dispatch"}` (`via="runner"`), returning the refs released. `RELEASE_SIGNALS` gains `"dispatch"` with a comment.

- [ ] **Step 1: Write the failing tests**

`server/tests/test_automation_spawn_model.py` — next to `test_backlog_spawn_raises_an_auto_pick_to_the_capabilitys_floor` (read `_build_backlog_orch`; add a `ladder_rung=` stub parameter the way `tier_floor=` is stubbed):

```python
async def test_backlog_spawn_pins_the_ladder_rung_over_an_auto_pick(tmp_path, monkeypatch):
    """Spec 2026-09-18 §4: a stronger_model hold makes the spawn run at the rung,
    even though the product says auto and the assessor would pick sonnet."""
    orch = _build_backlog_orch({"default_model": "auto"}, tier_floor=None, ladder_rung="opus")
    ... the file's spawn invocation with an assessor stub returning "sonnet" ...
    assert spawned.model == "opus"
    assert event_detail["ladder_rung"] == "opus"


async def test_backlog_spawn_without_a_rung_is_unchanged(tmp_path, monkeypatch):
    orch = _build_backlog_orch({"default_model": "auto"}, tier_floor=None, ladder_rung=None)
    ...
    assert "ladder_rung" not in event_detail
```

`server/tests/test_pm_holds.py`:

```python
def test_release_on_dispatch_clears_stronger_model_holds_only():
    from server.pm.holds import RELEASE_SIGNALS, release_on_dispatch
    db = _sync_db()
    a = _held(db, 1, kind="stronger_model"); b = _held(db, 2, kind="owner_action"); c = _held(db, 3, kind="evidence")
    db.commit()
    assert "dispatch" in RELEASE_SIGNALS
    released = release_on_dispatch(db, ["o/r#1", "o/r#2", "o/r#3", "o/r#9"], now=NOW)
    db.commit()
    assert released == ["o/r#1"]
    assert a.hold_kind is None and b.hold_kind == "owner_action" and c.hold_kind == "evidence"
    from server.db.models import AuditEvent
    rows = db.query(AuditEvent).filter(AuditEvent.type == "pm.hold.release").all()
    assert [r.detail["signal"] for r in rows] == ["dispatch"] and rows[0].detail["actorClass"] == "system"
```

(`_sync_db` and `_held` exist in that file from 3a; `release_on_dispatch` is sync like `release_by_signals` and is called from the orchestrator under `db.run_sync`.) Add an orchestrator seam test in `test_pm_orchestrator_seams.py` only if `_backlog_ladder_rung` grows logic beyond the composition described; otherwise the pure tests plus the spawn stub suffice — say which in the report.

- [ ] **Step 2: Run to verify they fail**

Run: `… -m pytest server/tests/test_automation_spawn_model.py server/tests/test_pm_holds.py -q -k "ladder or rung or release_on_dispatch"`
Expected: FAIL.

- [ ] **Step 3: Implement**

`holds.py`:

```python
def release_on_dispatch(db: "Session", refs: "Sequence[str]", *, now: datetime) -> list[str]:
    """A dispatch consumes the stronger_model hold it was climbing for (spec §4):
    clear it on every issue of the batch that carries one, audited as ``dispatch``."""
    from sqlalchemy import select, tuple_
    from server.pm.refs import split_ref
    parts = [p for p in (split_ref(r) for r in refs) if p is not None]
    if not parts:
        return []
    released: list[str] = []
    for wi in db.scalars(select(WorkItem).where(
            WorkItem.kind == "issue", WorkItem.hold_kind == "stronger_model",
            tuple_(WorkItem.repo, WorkItem.number).in_(parts))).all():
        clear_hold(wi, actor=RUNNER_ACTOR)
        _audit_release(db, wi, kind="stronger_model", signal="dispatch", actor=RUNNER_ACTOR)
        released.append(f"{wi.repo}#{wi.number}")
    db.flush()
    return released
```

(`_audit_release` exists from 3a; it stamps `via="reconciler"` — give it a `via: str = "reconciler"` parameter and pass `via="runner"` here.) `orchestrator.py`: `_backlog_ladder_rung` per the interface (compose `last_attempt_model`, `next_rung`, `top_tier` under `db_session()`; policy via `self._backlog_last_policies.get(product_slug)` or the effective default); in `_backlog_spawn` insert the rung step after the floor step and the release step after the session record is created (inside the same success path that emits the spawned event; read the surrounding code to place it after `rec` exists and before the event emit, so the event can carry `"released_holds": [...]`).

- [ ] **Step 4: Run the tests**

Run: `… -m pytest server/tests/test_automation_spawn_model.py server/tests/test_automation_spawn_model_coverage.py server/tests/test_pm_holds.py server/tests/test_pm_orchestrator_seams.py server/tests/test_backlog_runner_e2e.py -q && … -m mypy server/sessions/orchestrator.py server/pm/holds.py --ignore-missing-imports 2>&1 | tail -2`
Expected: PASS / clean.

- [ ] **Step 5: Commit**

```bash
git add server/pm/holds.py server/sessions/orchestrator.py server/tests/test_automation_spawn_model.py server/tests/test_pm_holds.py server/tests/test_pm_orchestrator_seams.py
printf 'feat(runner): the spawn pins the ladder rung and consumes the stronger_model hold\n\nSpec 2026-09-18 §4.\n\nCo-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>\n' > /tmp/c4.txt
git commit -F /tmp/c4.txt
```

---

### Task 5: Docs, gates, PR

- [ ] **Step 1: Docs.** `docs/product/features/autopilot-backlog-runner.md`: replace the "max attempts" paragraph with the ladder (one tier per bounce from the previous attempt's model, never below the capability floor or a pinned default, `ladder_top` per product defaulting to fable, a bounce at the top writes the owner-action hold and comments; a PR closed unmerged still hands off; `attempt_no` on the outcome rows is the provenance). `docs/product/features/product-management-layer.md`: the policy field list gains `ladder_top` and loses `max_attempts`; the Holds paragraph names `stronger_model` as the runner's climb marker, released by the dispatch (`signal = dispatch`). Grep both docs and `docs/superpowers/specs/2026-09-12-product-automation-policy-design.md` for `max_attempts`; the spec is historical, leave it; the feature docs must not mention the cap as current.
- [ ] **Step 2: Gates.** `ruff check src/ server/`; `mypy server/ --ignore-missing-imports`; `pytest tests/ -q`; `python scripts/brand_ratchet.py`; `grep -rn "max_attempts" server/ src/ --include=*.py | grep -v "retired_config_keys\|tests/"` must print only the legacy-seed removal's neighbours if any (report what it prints). If any web file changed (it should not — the policy editor reads the schema from the API), run the web gates. The controller runs `server/tests/` detached.
- [ ] **Step 3: Commit** (`docs(runner): the model ladder`, trailer). The controller rebases onto `origin/master` (3b may have merged), pushes, checks in-flight PRs on the touched files, opens the PR (non-draft, `Refs #1540`), arms auto-merge.

## Self-review

**Spec coverage (§4):** bounce definition (no_pr, pr_gave_up climb; abandoned hands off with the PR link) → T3; rung one above the previous recorded model, never below default/floor, unresolvable climbs from sonnet, rung function beside the floor rule → T2, T4; top = highest allowed tier (per-product `ladder_top`, default fable), bounce at the top → owner_action by runner → T2, T3; `max_attempts` removed → T2; a session asking (`set_hold(stronger_model)`) → already in the foundation, and T4's spawn honours it identically because it reads the hold, not who set it; clearing on dispatch → T4; requested vs applied → the spawned event carries `ladder_rung` and the outcome row carries the applied `model` (T4); no approval step → T4 pins the config model, no policy re-run; `dispatch_kind` (§3) → T1. Not in this plan: the queue's `hold_kind` on entries already exists (3a); the merged-PR close-out human gate is untouched.

**Type consistency:** `next_rung(previous_model, *, floor, default_model, top)` (T2) called in T3 and T4 with those keywords; `top_tier(policy)` (T2) used in T3, T4; `last_attempt_model(db, target_ref)` (T2) used in T4; seam `escalate(target, reason, rung)` (T3); `release_on_dispatch(db, refs, *, now)` (T4); `set_runner_stronger_hold(wi, *, reason, now)` (T3).

**Placeholders:** the runner-loop test bodies in T3 and the spawn test bodies in T4 are sketched with `...` where they must follow the harness; the brief says so explicitly and names the sibling tests to copy. Everything else carries its code.

# Product Automation Policy — Phase 4 (Model-Pick Floor) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When a capability's issues have merged first-pass only at a higher model tier, the Backlog Runner's `auto` model pick is raised to that tier — evidence over a fresh guess — and the spawn records what was assessed, what the floor was, and what was applied.

**Architecture:** `server/pm/outcomes.py` gains `outcome_stats` (per-tier finalised outcomes and first-pass merges for a capability, over `DispatchOutcome` joined to `WorkItem` by ref) and `tier_floor` (D9's rule over those stats, ranked by the catalog's one `TIER_ORDER`). `server/automations/model_pick.py` gains `pick_model`, the one resolver returning a `ModelPick(assessed, floor, applied, source)`; `resolve_model` becomes its two-tuple wrapper so every existing caller is unchanged. `_backlog_spawn` computes the floor through an injectable seam for the primary issue's capability, passes it, and writes one audit row per auto pick. Codex reasoning effort is untouched (D9 names `resolve_model` only).

**Tech Stack:** Python 3.12 / SQLAlchemy async; pytest.

**Spec:** `docs/superpowers/specs/2026-09-12-product-automation-policy-design.md` — D9, §5 (spawn bullet; its "(§8)" cross-reference is a mislabel for §7 and is corrected in Task 3), §6 reads paragraph, §7 "Model pick (D9)", §11 phase 4, §12. Phases 1–3 landed (#1599, #1601, #1602).

## Global Constraints

- Constitution VI: ONE tier ordering (`server/sessions/model_catalog.py` `TIER_ORDER`, `model_tier`, `tier_rank` — never a second list or string compare); ONE floor rule (`tier_floor`); ONE resolver (`pick_model`; `resolve_model` wraps it); ONE spawn path (`_backlog_spawn`, which the loop and both taps already funnel through); ref→WorkItem lookups reuse `server/pm/refs.py` `split_ref`.
- D9/§7 verbatim rule: the floor is "the lowest tier at which that capability's issues merged first-pass in ≥ 2 outcomes, when the lower tiers' first-pass rate was 0 across ≥ 2 outcomes." A pinned alias (and `default`) is untouched; only an `auto` assessment is raised, never lowered.
- Evidence counted: `DispatchOutcome` rows for the capability's issues whose `outcome` is finalised (not `running`, not `superseded`) and whose `model` maps to a tier via `model_tier`; "first-pass" = `first_pass_merge is True`; rows with `first_pass_merge is None` count as outcomes but not as first passes.
- The floor lookup is best-effort: a failed read yields no floor (logged) and never blocks a spawn (§13).
- Audit `backlog_runner.model_pick` (identity `backlog-runner`) with `{target, product, assessed, floor, applied, source}` only when the policy's model is `auto`; the spawned `AutomationEvent`'s `detail` also carries `assessed`/`floor`. Surfacing in the UI is out of phase 4 (data only; `list_events` drops `detail` today — say so in the docs).
- Tests never spell the legacy package name in a monkeypatch string (brand ratchet #1374, ceiling 1460).
- Commit trailer exactly `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`; `Refs #1521` in commit bodies; the PR body says `Fixes #1521` (this phase completes the issue).
- Server gate per task: `ruff check src/ server/`; `mypy server/ --ignore-missing-imports`; `python3 scripts/brand_ratchet.py`; the task's test files with the prefix `env -u CCSWITCH_SESSIOND_ENDPOINT -u CCSWITCH_SESSIOND_CLIENT_CERT -u CCSWITCH_SESSIOND_CLIENT_KEY -u CCSWITCH_SESSIOND_CA -u CCSWITCH_DAEMON_ROLE -u CCSWITCH_TAILNET_ALLOWLIST -u VIBERPIT_SESSIOND_ENDPOINT -u VIBERPIT_SESSIOND_CLIENT_CERT -u VIBERPIT_SESSIOND_CLIENT_KEY -u VIBERPIT_SESSIOND_CA -u VIBERPIT_DAEMON_ROLE -u VIBERPIT_TAILNET_ALLOWLIST CCSWITCH_DEV=1 /home/tony/GitHub/viberpit/.venv/bin/python -m pytest … -q -p no:cacheprovider`.
- Task order: 1 → 2 → 3.

## File map

| File | Responsibility |
|---|---|
| `server/pm/outcomes.py` | `TierStats`, `outcome_stats`, `tier_floor`, `tier_floor_for_dispatch` |
| `server/automations/model_pick.py` | `ModelPick`, `pick_model`, `resolve_model` wrapper |
| `server/sessions/orchestrator.py` | `_backlog_tier_floor` seam; `_backlog_spawn` uses `pick_model`, passes the floor, audits |
| `docs/product/features/autopilot-backlog-runner.md`, `product-management-layer.md`, the spec | docs |

---

### Task 1: `outcome_stats` and `tier_floor`

**Files:**
- Modify: `server/pm/outcomes.py`
- Test: `server/tests/test_pm_outcomes.py` (pure rule over synthetic stats) and `server/tests/test_pm_outcome_stats.py` (new; seeded SQLite)

**Interfaces (produces):**

```python
@dataclass(frozen=True)
class TierStats:
    tier: str          # a member of TIER_ORDER
    outcomes: int      # finalised, non-superseded rows at this tier
    first_pass: int    # of those, first_pass_merge is True

def tier_floor_from_stats(stats: Iterable[TierStats]) -> str | None
    # D9, pure. Walk TIER_ORDER cheapest-first. Track `evidence_below`: True once a lower tier
    # with outcomes >= 2 has been seen, and every such lower tier had first_pass == 0.
    # Return the first tier with first_pass >= 2 when evidence_below is True. If a lower tier
    # with outcomes >= 2 had first_pass > 0, there is no floor (cheaper tiers do work).
    # Tiers with outcomes < 2 are ignored in both roles (not enough evidence).

async def outcome_stats(db, project_id: str, feature_id: str) -> list[TierStats]
    # refs = f"{w.repo}#{w.number}" for WorkItem rows with feature_id == feature_id and kind == "issue"
    # (an empty list → []); rows = DispatchOutcome where project_id == project_id,
    # target_ref IN refs, outcome NOT IN ("running", "superseded"); group in Python by
    # model_tier(row.model) (rows whose model has no tier are skipped); return one TierStats per
    # tier present, in TIER_ORDER order.

async def tier_floor(db, project_id: str, feature_id: str) -> str | None
    # tier_floor_from_stats(await outcome_stats(db, project_id, feature_id))

async def tier_floor_for_dispatch(db, product_slug: str | None, primary_ref: str) -> str | None
    # project_id via Project.slug; feature_id via split_ref(primary_ref) → WorkItem(repo, number,
    # kind issue).feature_id; either missing → None; else tier_floor(...). Any exception → log +
    # None (best-effort, §13).
```

- [ ] **Step 1: Failing pure tests** (append to `server/tests/test_pm_outcomes.py`):

```python
from server.pm.outcomes import TierStats, tier_floor_from_stats


def _s(tier, outcomes, first_pass):
    return TierStats(tier=tier, outcomes=outcomes, first_pass=first_pass)


def test_floor_is_the_lowest_tier_that_merged_first_pass_when_cheaper_tiers_failed():
    assert tier_floor_from_stats([_s("sonnet", 3, 0), _s("opus", 2, 2)]) == "opus"


def test_no_floor_without_two_failed_outcomes_below():
    assert tier_floor_from_stats([_s("sonnet", 1, 0), _s("opus", 2, 2)]) is None
    assert tier_floor_from_stats([_s("opus", 2, 2)]) is None


def test_no_floor_when_a_cheaper_tier_ever_merged_first_pass():
    assert tier_floor_from_stats([_s("sonnet", 4, 1), _s("opus", 2, 2)]) is None


def test_floor_needs_two_first_pass_merges_at_the_higher_tier():
    assert tier_floor_from_stats([_s("sonnet", 2, 0), _s("opus", 3, 1)]) is None


def test_floor_skips_a_middle_tier_with_too_little_evidence():
    assert tier_floor_from_stats([_s("haiku", 2, 0), _s("sonnet", 1, 0), _s("opus", 2, 2)]) == "opus"


def test_floor_orders_by_the_catalog_not_by_input_order():
    assert tier_floor_from_stats([_s("opus", 2, 2), _s("sonnet", 2, 0)]) == "opus"


def test_empty_stats_have_no_floor():
    assert tier_floor_from_stats([]) is None
```

- [ ] **Step 2: Failing seeded tests** — `server/tests/test_pm_outcome_stats.py` (the `init_db(db_url=…)` idiom from `server/tests/test_backlog_policy_seams.py`; seed a `Project(slug="viberpit")`, a `Feature`, three `WorkItem`s under it (`o/r#1..3`) and one under another feature (`o/r#9`); `DispatchOutcome` rows: #1 `sonnet` `no_pr`, #2 `sonnet` `needs_human`, #3 `opus` `merged` first_pass True twice (two dispatches), #9 `haiku` `merged` first_pass True; one `running` and one `superseded` row at `opus` that must be ignored; one row with `model=None`):

```python
async def test_outcome_stats_groups_finalised_rows_by_tier_for_one_capability(db_env):
    stats = await outcome_stats(db, project.id, feature.id)
    assert stats == [TierStats("sonnet", 2, 0), TierStats("opus", 2, 2)]   # #9 excluded, running/superseded/None-model skipped

async def test_tier_floor_reads_the_stats(db_env):
    assert await tier_floor(db, project.id, feature.id) == "opus"

async def test_tier_floor_for_dispatch_resolves_slug_and_ref(db_env):
    assert await tier_floor_for_dispatch(db, "viberpit", "o/r#1") == "opus"
    assert await tier_floor_for_dispatch(db, "viberpit", "o/r#404") is None      # unmirrored ref
    assert await tier_floor_for_dispatch(db, "nope", "o/r#1") is None            # unknown product
    assert await tier_floor_for_dispatch(db, "viberpit", "garbage") is None      # unparseable

async def test_tier_floor_for_dispatch_is_best_effort(db_env, monkeypatch):
    (patch `server.pm.outcomes.outcome_stats` through the module object to raise → None, no exception)
```

Write them in full.

- [ ] **Step 3: Run; expect ImportError**

- [ ] **Step 4: Implement** in `server/pm/outcomes.py`:

```python
from dataclasses import dataclass
from server.sessions.model_catalog import TIER_ORDER, model_tier
from server.pm.refs import split_ref


@dataclass(frozen=True)
class TierStats:
    tier: str
    outcomes: int
    first_pass: int


def tier_floor_from_stats(stats: Iterable[TierStats]) -> str | None:
    """D9, the ONE floor rule. The lowest tier that merged first-pass in >= 2
    outcomes, when every cheaper tier with >= 2 outcomes merged first-pass in
    none of them — and at least one such cheaper tier exists. Tiers with fewer
    than two outcomes are not evidence either way. Ranked by the catalog's
    ``TIER_ORDER``, never by input order."""
    by_tier = {s.tier: s for s in stats}
    evidence_below = False
    for tier in TIER_ORDER:
        s = by_tier.get(tier)
        if s is None or s.outcomes < 2:
            continue
        if s.first_pass >= 2 and evidence_below:
            return tier
        if s.first_pass > 0:
            return None          # a cheaper tier does work — no floor
        evidence_below = True
    return None
```

`outcome_stats` (async; `select(WorkItem.repo, WorkItem.number)` for the feature, build refs, `select(DispatchOutcome.model, DispatchOutcome.first_pass_merge).where(project_id ==, target_ref.in_(refs), outcome.notin_(("running", "superseded")))`, group by `model_tier(model)` skipping `None`, emit in `TIER_ORDER`), `tier_floor`, `tier_floor_for_dispatch` (slug → `Project.id`; `split_ref` → `WorkItem.feature_id`; try/except → `log.warning` + `None`).

- [ ] **Step 5: Gate** — `server/tests/test_pm_outcomes.py server/tests/test_pm_outcome_stats.py server/tests/test_automation_context_outcomes.py`; ruff; mypy; ratchet.

- [ ] **Step 6: Commit** — `feat(pm): policy phase 4 task 1 — outcome_stats and the D9 tier floor` (+ `Refs #1521`, trailer).

---

### Task 2: `pick_model`, the floor at spawn, the audit

**Files:**
- Modify: `server/automations/model_pick.py:167-195`
- Modify: `server/sessions/orchestrator.py` (`_backlog_spawn` ~:5851-5915; a new `_backlog_tier_floor` seam beside `_backlog_policy_model` ~:5833)
- Test: `server/tests/test_model_pick.py`, `server/tests/test_automation_spawn_model.py`

**Interfaces:**
- Consumes (Task 1): `server.pm.outcomes.tier_floor_for_dispatch(db, product_slug, primary_ref)`.
- Produces:

```python
@dataclass(frozen=True)
class ModelPick:
    assessed: str | None   # assess_model's answer (None when not auto, or assessment failed)
    floor: str | None      # the floor handed in (None when not auto)
    applied: str | None    # what create_session gets (None = account default)
    source: str | None     # "pinned" | "auto" | "auto+floor" | None

async def pick_model(config: dict, task_summary: str, *, floor: str | None = None, _assess=None) -> ModelPick
    # default/unknown → ModelPick(None, None, None, None); pinned → (None, None, value, "pinned");
    # auto: assessed = await assess(...); if assessed is None → (None, floor, None, None);
    # applied = assessed; if floor and tier_rank(floor) > tier_rank(assessed): applied, source = floor, "auto+floor"
    # else source = "auto". Ranks via model_catalog.tier_rank; a floor with no rank is ignored.

async def resolve_model(config, task_summary, *, floor=None, _assess=None) -> tuple[str | None, str | None]
    # pick = await pick_model(...); return pick.applied, pick.source     (every existing caller unchanged)
```

`SessionOrchestrator._backlog_tier_floor(product_slug, primary_ref) -> str | None`: opens `db_session()` and returns `tier_floor_for_dispatch(...)` (the injectable seam; tests patch it on the instance). `_backlog_spawn`: `floor = await self._backlog_tier_floor(batch.product, batch.primary)` then `pick = await pick_model(config, summary, floor=floor)`; `model, model_source = pick.applied, pick.source`; `detail.update(model=…, model_source=…, assessed=pick.assessed, floor=pick.floor)`; when `config["default_model"] == "auto"`, `record_audit(type="backlog_runner.model_pick", identity="backlog-runner", detail={"target": batch.primary, "product": batch.product, "assessed": pick.assessed, "floor": pick.floor, "applied": pick.applied, "source": pick.source})` in its own `db_session()` (best-effort, wrapped). Codex path (`resolve_codex_model`) unchanged.

- [ ] **Step 1: Failing tests** — `test_model_pick.py`:

```python
async def test_pick_model_raises_an_auto_pick_to_the_floor():
    async def pick_sonnet(task_summary): return "sonnet"
    p = await pick_model({"default_model": "auto"}, "job", floor="opus", _assess=pick_sonnet)
    assert p == ModelPick(assessed="sonnet", floor="opus", applied="opus", source="auto+floor")

async def test_pick_model_never_lowers_an_auto_pick():
    async def pick_fable(task_summary): return "fable"
    p = await pick_model({"default_model": "auto"}, "job", floor="opus", _assess=pick_fable)
    assert p.applied == "fable" and p.source == "auto"

async def test_pick_model_leaves_pinned_and_default_untouched_by_a_floor():
    assert (await pick_model({"default_model": "sonnet"}, "job", floor="fable", _assess=_never_assess())) == ModelPick(None, None, "sonnet", "pinned")
    assert (await pick_model({"default_model": "default"}, "job", floor="fable", _assess=_never_assess())).applied is None

async def test_pick_model_ignores_a_floor_with_no_tier_and_a_failed_assessment():
    async def none(task_summary): return None
    assert (await pick_model({"default_model": "auto"}, "job", floor="best", _assess=none)).applied is None
    async def pick_sonnet(task_summary): return "sonnet"
    assert (await pick_model({"default_model": "auto"}, "job", floor="best", _assess=pick_sonnet)).applied == "sonnet"

async def test_resolve_model_is_the_two_tuple_view_of_pick_model():
    async def pick_sonnet(task_summary): return "sonnet"
    assert await resolve_model({"default_model": "auto"}, "job", floor="opus", _assess=pick_sonnet) == ("opus", "auto+floor")
```

`test_automation_spawn_model.py` (extend `_build_backlog_orch` to stub `_backlog_tier_floor`): the spawn passes `model="opus"` to `create_session` when the assessment says `sonnet` and the floor seam answers `opus`; the spawned event detail carries `assessed: "sonnet"`, `floor: "opus"`, `model_source: "auto+floor"`; an `AuditEvent` of type `backlog_runner.model_pick` with those keys exists; a pinned policy model spawns unchanged and writes NO audit; a raising floor seam → spawn proceeds with the assessed model; the taps go through the same path (one test using `backlog_runner_dispatch`).

- [ ] **Step 2: Run; expect ImportError / missing kwarg**

- [ ] **Step 3: Implement** — `model_pick.py` (keep `resolve_model`'s docstring; add `"auto+floor"` to the documented sources; import `tier_rank` from the catalog), the seam, the spawn changes, the audit.

- [ ] **Step 4: Gate** — `server/tests/test_model_pick.py server/tests/test_automation_spawn_model.py server/tests/test_backlog_runner_loop.py server/tests/test_backlog_policy_seams.py server/tests/test_backlog_dispatch.py server/tests/test_pr_fix_spawn*.py` (adjust to existing names); ruff; mypy; ratchet.

- [ ] **Step 5: Commit** — `feat(runner): policy phase 4 task 2 — pick_model with the capability's tier floor; spawn audit` (+ `Refs #1521`, trailer).

---

### Task 3: Docs

**Files:**
- Modify: `docs/product/features/autopilot-backlog-runner.md` (model pick: policy `default_model` `auto` assesses, then the capability's floor raises it; the rule in one sentence; the audit type and the event detail keys; UI surfacing not yet), `docs/product/features/product-management-layer.md` (the outcomes paragraph gains the `outcome_stats`/`tier_floor` readers), `docs/superpowers/specs/2026-09-12-product-automation-policy-design.md` (§5's "(§8)" → "(§7)", marked as a correction).

Every sentence verified against the landed code; `python3 scripts/brand_ratchet.py`; commit `docs: policy phase 4 task 3 — the model-pick floor` (+ `Refs #1521`, trailer).

---

## Self-review

- Spec coverage: D9 + §7 rule (Task 1 `tier_floor_from_stats`, exact thresholds), §6 reads (`outcome_stats`), §5 spawn bullet + audit `{assessed, floor, applied}` (Task 2), §11 phase 4 (all three), §12 "`tier_floor` over synthetic outcome rows" (Task 1 pure tests) and the resolver cases (Task 2). Codex effort: not in D9/§7 — explicitly out of scope, stated in the docs.
- Types: `TierStats` fields match between Task 1's tests and implementation; `ModelPick` field order `(assessed, floor, applied, source)` matches the positional constructions in Task 2's tests; `tier_floor_for_dispatch(db, product_slug, primary_ref)` matches the seam's call.
- Open choices stated: none — the resolver shape (one `pick_model`, two-tuple wrapper) and the audit mechanism (`record_audit`, not `list_events`) are decided above.

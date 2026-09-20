# Capacity settings PR 3 — the runner form and the surface flag

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The runner ceiling, the daily budget and sessions-per-account are edited on Delivery → Capacity in the one `SaveBar` form the pins already use; the runner's config tab shows them read-only with a link; a ceiling above the effective global cap is refused with a 422; the two plan-overlay code paths become one seam; the runner-layer constraint code is named for what it is; and the readout carries the account quota so the Runner row can say when quota binds.

**Architecture:** `ConfigField` gains `surface: "config" | "capacity"` (server dataclass → wire → TS type); the generic `ConfigPanel` renders `capacity`-surfaced fields as read-only rows linking to the Capacity page, and the Capacity page's existing pin form is widened into one namespaced draft (`pin:<slug>`, `runner:max_concurrent`, `runner:max_spawns_per_day`, `governor:sessions_per_account`) saved runner-first, then governor, then pins. Two server hygiene fixes ride along because the form depends on them: `PATCH /api/automations/{id}` merges a partial `config` over the stored row instead of resetting the absent keys to defaults, and the runner-config read + plan overlay lives in one function both the orchestrator and admission call. `global_capacity_*` constraint codes become `runner_ceiling_*`.

**Tech Stack:** Python FastAPI + SQLAlchemy async (pytest, SQLite) · React 18 + TypeScript + React Query + react-router (vitest + testing-library).

**Spec:** `docs/superpowers/specs/2026-09-20-capacity-settings-structure-design.md` — §8 **PR 3**: D7 (surface flag, resolved by the owner), D8 (constraint-code rename), D11 second half (one overlay seam), §5.2 item 3 (runner form, one confirmation when a ceiling is raised), §6 (runner ceiling ≤ global cap), §7 (`configSchema[].surface`, 422 on the PATCH), plus PR 2's ledgered deferral (the Runner row's quota secondary, which needs `accountCap` on the wire). Closes C10, C12, C16. PRs 4–6 (product dials one owner, weight deprecation, reserve override, pipeline cap) are LATER.

## Global Constraints

- **Vocabulary (D8):** labels are exactly **Runner ceiling** (`max_concurrent`), **Daily budget** (`max_spawns_per_day`), **Sessions per account**, **Push level**, **Global cap**. "Max occupied slots" and "Max spawns per day" are retired as labels. Constraint codes: `runner_ceiling_{unknown,held,full,available}` replace `global_capacity_*`; layer `runner`. `global_cap_full` (the real global cap, layer `global`) is untouched.
- **One reader for the global cap (D11, PR 1):** the effective cap is read ONLY via `server/sessions/global_cap.py:read_global_cap(db).effective`. Never mention `max_active_sessions` or `DEFAULT_MAX_ACTIVE_SESSIONS` in any new module — `server/tests/test_global_cap.py::test_no_other_reader_of_max_active_sessions` fails the suite otherwise.
- **`max_concurrent` stays a key on the Backlog Runner's schema** (the plan overlay and the readout read it by name); only its editing surface moves.
- **No schema change.** New wire fields are additive: `configSchema[].surface`, `capacity.accounts.sessionsPerAccount`, `capacity.accounts.accountCap`.
- **Save contract (2026-09-04 §7):** the Capacity page keeps exactly ONE `SaveBar`; runner fields join the pin form's draft. Push level, governor mode, cap-mode switch and rank arrows stay immediate.
- **Links go through helpers (D10):** `deliveryPath('capacity')`, `settingsSectionPath(id)`; in-page anchors via `capacityRowLink` / `CapacityRowLabel` (PR 2). No hard-coded paths in component code.
- **Test commands (repo root; the Bash hook is cwd-relative — never leave the shell elsewhere; web commands in a subshell):**
  - Server, one file: `mkdir -p /tmp/viberpit-test-home && env -i HOME=/tmp/viberpit-test-home PATH="$PATH" CCSWITCH_DEV=1 PYTHONPATH=$PWD/src /home/tony/GitHub/viberpit-deploy/.venv/bin/python -m pytest server/tests/<file>.py -q`
  - Web: `[ -e web/node_modules ] || ln -s /home/tony/GitHub/viberpit-deploy/web/node_modules web/node_modules`; one file: `(cd web && npx vitest run src/<path>)`
  - Final gate (Task 8): `ruff check src/ server/`, `mypy server/ --ignore-missing-imports`, `pytest tests/`, `pytest server/tests/ -n auto --dist loadfile`, and in `web/`: `npm run typecheck`, `npm run lint`, `npm run test`, `npm run build`.
- **Never `git stash`** (shared stack). Commits: message via `-F` file ending with `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.

---

## File structure

| File | Responsibility |
|---|---|
| `server/automations/base.py` | `ConfigField.surface` + `to_dict`. |
| `server/automations/backlog_runner.py`, `capacity_governor.py` | Labels per D8; `surface="capacity"` on the four moved fields; `backlog_runner.py` status branch reads the renamed code. |
| `server/api/automations.py` | PATCH merges partial `config` over the stored row; 422 when `backlog-runner`'s `max_concurrent` exceeds the effective global cap. |
| `server/automations/runner_config.py` (new) | `RUNNER_MODES`, `read_runner_config(*, plan=…, apply_plan=True)` — the ONE runner-config read + overlay. |
| `server/sessions/orchestrator.py`, `server/automations/product_admission.py` | Both delegate to `read_runner_config`. |
| `server/automations/product_policy.py`, `capacity_readout.py` | Constraint code rename; `accounts.sessionsPerAccount` / `accounts.accountCap`. |
| `web/src/api/automations.ts`, `web/src/api/capacity.ts` | `surface?`, `sessionsPerAccount`, `accountCap`. |
| `web/src/features/delivery/automation/ConfigPanel.tsx` | Read-only rows for `surface === 'capacity'`. |
| `web/src/features/delivery/capacity/capacityStack.ts` | Runner row: `#runner` anchor; secondary joins "stored ceiling" and "quota" clauses. |
| `web/src/features/delivery/capacity/runnerForm.ts` (new) | Pure: draft keys, seeding, parsing/validation of the runner + governor fields. |
| `web/src/features/delivery/capacity/CapacityDetail.tsx` | The runner fieldset inside the one form; save order; confirm on raise. |

---

### Task 1: `ConfigField.surface`, labels, and the four moved fields

**Files:**
- Modify: `server/automations/base.py:14-33`, `server/automations/backlog_runner.py:272-276`, `server/automations/capacity_governor.py:29-35`
- Modify: `web/src/api/automations.ts:27-39`
- Test: `server/tests/test_automations_api.py`, `server/tests/test_capacity_governor_loop.py`

**Interfaces:**
- Produces: `ConfigField.surface: str = "config"` (values `"config" | "capacity"`), emitted by `to_dict()`; TS `ConfigField.surface?: 'config' | 'capacity'`. Runner: `max_concurrent` label **Runner ceiling**, `max_spawns_per_day` label **Daily budget**, both `surface="capacity"`; governor: `push_level` and `sessions_per_account` `surface="capacity"`.

- [ ] **Step 1: Failing tests**

Append to `server/tests/test_automations_api.py`:

```python
def test_config_schema_publishes_the_editing_surface():
    """spec 2026-09-20 D7: a field owned by Delivery → Capacity says so on the wire,
    so the generic Config tab can render it read-only instead of as a second editor."""
    with _client() as c:
        rows = {a["id"]: a for a in c.get("/api/automations", headers=H).json()}
        runner = {f["key"]: f for f in rows["backlog-runner"]["configSchema"]}
        assert runner["max_concurrent"]["surface"] == "capacity"
        assert runner["max_concurrent"]["label"] == "Runner ceiling"
        assert runner["max_spawns_per_day"]["surface"] == "capacity"
        assert runner["max_spawns_per_day"]["label"] == "Daily budget"
        assert runner["max_issues_per_pr"]["surface"] == "config"
        governor = {f["key"]: f for f in rows["capacity-governor"]["configSchema"]}
        assert {governor["push_level"]["surface"], governor["sessions_per_account"]["surface"]} == {"capacity"}
```

In `server/tests/test_capacity_governor_loop.py`, next to the existing schema pin (`assert {f.key for f in a.config_schema} == {"push_level", "sessions_per_account"}`), add: `assert {f.surface for f in a.config_schema} == {"capacity"}`.

- [ ] **Step 2: Run to verify they fail**

Run the two files with the server test prefix → FAIL (`KeyError: 'surface'` / `AttributeError: surface`).

- [ ] **Step 3: Implement**

`server/automations/base.py` — `ConfigField` gains, after `min: int | None = None`:

```python
    # Which page edits this field (spec 2026-09-20 D7). "config": the automation's
    # generic Config tab. "capacity": Delivery → Capacity owns it, and the Config
    # tab renders it read-only with a link — one editor per dial.
    surface: str = "config"
```

and `to_dict()` returns `…, "min": self.min, "surface": self.surface}`.

`server/automations/backlog_runner.py` — the two fields become:

```python
        ConfigField(key="max_concurrent", label="Runner ceiling", type="int", default=1,
                    help="Most sessions the Backlog Runner may hold at once, across every product, "
                         "including proposals awaiting approval. A share of the global cap; quota "
                         "(push level) and the governor lower it, never raise it.",
                    surface="capacity"),
        ConfigField(key="max_spawns_per_day", label="Daily budget", type="int", default=10,
                    help="Circuit-breaker: pause spawning past this many sessions per day.",
                    surface="capacity"),
```

`server/automations/capacity_governor.py` — add `surface="capacity"` to both `ConfigField(...)` calls (keyword, after `help=`).

`web/src/api/automations.ts` — after `min?: number | null`:

```ts
  /** Which page edits the field (spec 2026-09-20 D7). `'capacity'`: Delivery → Capacity
   *  owns it and the Config tab renders it read-only. Optional so older fixtures compile. */
  surface?: 'config' | 'capacity'
```

- [ ] **Step 4: Run**

Server: `test_automations_api.py`, `test_capacity_governor_loop.py`, `test_backlog_runner_automation.py`, `test_automation_config_store.py` → PASS; `ruff check server/automations/base.py server/automations/backlog_runner.py server/automations/capacity_governor.py`; `mypy server/automations/base.py --ignore-missing-imports`. Web: `(cd web && npm run typecheck)`. If any server test asserts the old labels ("Max occupied slots", "Max spawns per day"), update it to the new labels and name it.

- [ ] **Step 5: Commit**

```bash
git add server/automations/base.py server/automations/backlog_runner.py server/automations/capacity_governor.py web/src/api/automations.ts server/tests/test_automations_api.py server/tests/test_capacity_governor_loop.py
printf '%s\n' 'feat(automations): ConfigField.surface — Capacity owns the runner ceiling, daily budget and governor knobs' '' 'spec 2026-09-20 D7, D8 labels.' '' 'Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>' > /tmp/pr3-t1.txt
git commit -F /tmp/pr3-t1.txt
```

---

### Task 2: PATCH merges a partial config; a runner ceiling above the global cap is 422

**Files:**
- Modify: `server/api/automations.py:232-244`
- Test: `server/tests/test_automations_api.py`

**Interfaces:**
- Produces: `PATCH /api/automations/{id}` with `config: {k: v}` merges over the stored `row.config` before validation (absent keys keep their stored values); `PATCH /api/automations/backlog-runner` with `config.max_concurrent > read_global_cap(db).effective` → 422 `detail="max_concurrent <n> exceeds the effective global cap (<cap>)"`.

- [ ] **Step 1: Failing tests**

Append to `server/tests/test_automations_api.py`:

```python
def test_patch_partial_config_keeps_the_other_stored_keys():
    """A partial config PATCH is a merge, not a reset: the push-level radio sends
    {push_level} alone and must not put sessions_per_account back to its default."""
    with _client() as c:
        r = c.patch("/api/automations/capacity-governor", headers=H,
                    json={"config": {"push_level": "steady", "sessions_per_account": 3}})
        assert r.status_code == 200, r.text
        r = c.patch("/api/automations/capacity-governor", headers=H, json={"config": {"push_level": "max"}})
        assert r.status_code == 200, r.text
        assert r.json()["config"] == {"push_level": "max", "sessions_per_account": 3}


def test_patch_runner_ceiling_above_the_global_cap_is_422():
    """spec 2026-09-20 §6: runner ceiling <= effective global cap. A fresh DB has no
    hosts and no attention ceiling, so the effective cap is the default 5."""
    with _client() as c:
        ok = c.patch("/api/automations/backlog-runner", headers=H, json={"config": {"max_concurrent": 5}})
        assert ok.status_code == 200, ok.text
        r = c.patch("/api/automations/backlog-runner", headers=H, json={"config": {"max_concurrent": 6}})
        assert r.status_code == 422, r.text
        assert "exceeds the effective global cap (5)" in r.json()["detail"]
        # Nothing was written by the refused request.
        rows = {a["id"]: a for a in c.get("/api/automations", headers=H).json()}
        assert rows["backlog-runner"]["config"]["max_concurrent"] == 5
```

- [ ] **Step 2: Run to verify they fail**

→ the first fails on `sessions_per_account` being 2; the second fails with 200 instead of 422.

- [ ] **Step 3: Implement**

In `server/api/automations.py`, add `from server.sessions.global_cap import read_global_cap` to the imports, and replace the `if body.config is not None:` block with:

```python
    if body.config is not None:
        # A partial body is a MERGE over the stored row (spec 2026-09-20 §7): the
        # Capacity page's push-level radio and runner form each send only the keys
        # they own, and validate_config fills any absent key with its default —
        # so validating the bare body would silently reset every sibling key.
        merged = {**(row.config or {}), **body.config}
        try:
            # Validated against the ENGINE-AWARE schema: an origin with no Codex
            # acceptance evidence offers no `engine` field, so `{"engine": ...}`
            # lands here as an unknown key and 422s. That is the enforcement —
            # not a special case downstream (Phase 7, Task 7.1). Keys the origin
            # RETIRED are dropped instead of 422'd, so a client echoing back the
            # config it was served before a field was removed still saves.
            config = validate_config(registry.schema_for(automation_id), merged,
                                     retired=retired_config_keys_of(automation))
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))
        if automation_id == "backlog-runner" and "max_concurrent" in body.config:
            # spec 2026-09-20 §6: the runner ceiling is a share of the global cap.
            cap = (await read_global_cap(db)).effective
            if int(config["max_concurrent"]) > cap:
                raise HTTPException(
                    status_code=422,
                    detail=f"max_concurrent {config['max_concurrent']} exceeds the effective global cap ({cap})")
        row.config = await _canonicalize_enabled_repos(config)
```

- [ ] **Step 4: Run**

`test_automations_api.py`, `test_automation_config_store.py`, `test_capacity_governor_loop.py`, `test_global_cap.py` (the no-other-reader guard must stay green — the new import is the allowed reader) → PASS; ruff + mypy on `server/api/automations.py`.

- [ ] **Step 5: Commit**

```bash
git add server/api/automations.py server/tests/test_automations_api.py
printf '%s\n' 'fix(automations): a partial config PATCH merges over the stored row; runner ceiling <= global cap' '' 'The push-level radio was resetting sessions_per_account on every change. spec 2026-09-20 §6, §7.' '' 'Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>' > /tmp/pr3-t2.txt
git commit -F /tmp/pr3-t2.txt
```

---

### Task 3: One runner-config seam (D11, second half)

**Files:**
- Create: `server/automations/runner_config.py`
- Modify: `server/sessions/orchestrator.py:5682-5726` (`_backlog_read_config`), `server/automations/product_admission.py:157-184` (`gather_implementation_snapshot`)
- Test: `server/tests/test_runner_config.py` (new)

**Interfaces:**
- Produces: `RUNNER_MODES = ("off", "on", "propose", "auto")`; `async def read_runner_config(*, plan: dict | None | object = UNSET, apply_plan: bool = True) -> tuple[str, dict]` — reads the Backlog Runner row through `read_automation_config`, then (when `apply_plan`) overlays `apply_fleet_plan(config, plan if given else await live_plan())`. Returns the RAW stored mode (callers fold it).

- [ ] **Step 1: Failing tests**

```python
# server/tests/test_runner_config.py
"""One runner-config read + plan overlay (spec 2026-09-20 D11): the orchestrator's
gate and admission's snapshot must see the same overlaid ceiling."""
from __future__ import annotations

import asyncio

import pytest

pytestmark = pytest.mark.usefixtures("isolated_db")


async def _seed(max_concurrent: int, plan_fleet: int | None) -> None:
    from server.automations.capacity_store import write_plan
    from server.db import db_session
    from server.db.models import Automation as AutomationRow

    async with db_session() as db:
        db.add(AutomationRow(id="backlog-runner", enabled=True, mode="on",
                             config={"max_concurrent": max_concurrent}))
        await db.flush()
    if plan_fleet is not None:
        await write_plan({"pushLevel": "steady", "applied": True, "fleetConcurrency": plan_fleet,
                          "perProduct": {}, "pending": {}, "budget": {}, "binding": None,
                          "reasons": [], "defects": []})


def test_overlay_caps_the_stored_ceiling_at_the_live_plan():
    from server.automations.runner_config import read_runner_config

    async def run():
        await _seed(6, plan_fleet=3)
        mode, config = await read_runner_config()
        assert mode == "on"
        assert config["max_concurrent"] == 3
        _mode, stored = await read_runner_config(apply_plan=False)
        assert stored["max_concurrent"] == 6

    asyncio.run(run())


def test_a_given_plan_is_used_instead_of_a_fresh_read():
    from server.automations.runner_config import read_runner_config

    async def run():
        await _seed(6, plan_fleet=3)
        _mode, config = await read_runner_config(plan={"applied": True, "fleetConcurrency": 2,
                                                       "computedAt": None})
        # apply_fleet_plan only honours a LIVE plan; a plan dict with no computedAt
        # is not live, so the given plan is "no change" — the point is that no
        # fresh live_plan() read happened (which would have capped at 3).
        assert config["max_concurrent"] == 6

    asyncio.run(run())


def test_orchestrator_and_admission_read_through_the_same_seam(monkeypatch):
    import server.automations.product_admission as admission
    import server.sessions.orchestrator as orch_mod
    from server.automations import runner_config

    calls: list[dict] = []

    async def fake(*, plan=runner_config.UNSET, apply_plan=True):
        calls.append({"plan_given": plan is not runner_config.UNSET, "apply_plan": apply_plan})
        return "on", {"max_concurrent": 4, "enabled_repos": []}

    monkeypatch.setattr(runner_config, "read_runner_config", fake)

    async def run():
        orch = orch_mod.SessionOrchestrator.__new__(orch_mod.SessionOrchestrator)
        mode, config = await orch._backlog_read_config()
        assert (mode, config["max_concurrent"]) == ("on", 4)
        await orch._backlog_read_config(apply_plan=False)
        snapshot = await admission.gather_implementation_snapshot([])
        assert snapshot.config["max_concurrent"] == 4

    asyncio.run(run())
    assert calls[0] == {"plan_given": False, "apply_plan": True}
    assert calls[1] == {"plan_given": False, "apply_plan": False}
    assert calls[2]["plan_given"] is True
```

If `Automation`'s ORM class is named differently (`grep -n "^class Automation" server/db/models.py`), use that name. If `write_plan`'s required keys differ from the dict above, copy the shape from `server/tests/test_capacity_store.py`.

- [ ] **Step 2: Run to verify it fails**

→ `ModuleNotFoundError: server.automations.runner_config`.

- [ ] **Step 3: The seam**

```python
# server/automations/runner_config.py
"""The ONE read of the Backlog Runner's (mode, config) with the capacity plan
overlaid (spec 2026-09-20 D11). Before this module the orchestrator's gate and
admission's snapshot each did their own read + overlay — two paths that could
straddle a governor write.

``apply_plan=False`` returns the operator's STORED ceiling: the governor's own
planner reads through here to learn it and must derive the next plan from that,
never from its own prior output (feeding the overlaid value back in would
ratchet ``fleetConcurrency`` down every tick). ``plan=`` lets a caller that has
already read the plan once (admission's snapshot) reuse it instead of reading
again.
"""
from __future__ import annotations

from typing import Any

from .capacity_apply import apply_fleet_plan
from .capacity_store import live_plan
from .config_store import read_automation_config

RUNNER_MODES: tuple[str, ...] = ("off", "on", "propose", "auto")

# Sentinel: "no plan given, read the live one" — None is a real value (no plan).
UNSET: Any = object()


async def read_runner_config(*, plan: dict | None | Any = UNSET,
                             apply_plan: bool = True) -> tuple[str, dict]:
    from .backlog_runner import BacklogRunner

    mode, config = await read_automation_config(
        BacklogRunner, modes=RUNNER_MODES, default_mode="on", log_key="backlog_runner")
    if apply_plan:
        config = apply_fleet_plan(config, await live_plan() if plan is UNSET else plan)
    return mode, config
```

- [ ] **Step 4: The two callers**

`server/sessions/orchestrator.py` `_backlog_read_config`: keep the docstring (append one sentence: `The read itself lives in server/automations/runner_config.py — the same function admission's snapshot uses.`) and replace the body (the four imports and everything after) with:

```python
        from server.automations import runner_config
        from server.automations.backlog_runner import fold_global_mode
        mode, config = await runner_config.read_runner_config(apply_plan=apply_plan)
        return fold_global_mode(mode), config
```

(`runner_config.read_runner_config` accessed through the module, not `from … import`, so the test's monkeypatch and any future seam stub land.)

`server/automations/product_admission.py` `gather_implementation_snapshot`: replace

```python
    mode, config = await read_automation_config(
        BacklogRunner, modes=("off", "on", "propose", "auto"), default_mode="on",
        log_key="backlog_runner")
    config = apply_fleet_plan(config, plan)
```

with

```python
    from . import runner_config
    mode, config = await runner_config.read_runner_config(plan=plan)
```

and drop the now-unused `read_automation_config` import from that function (keep `BacklogRunner, fold_global_mode`). Keep the "ONE plan read for the whole snapshot" comment — it is still true and now also names why `plan=plan` is passed.

- [ ] **Step 5: Run**

`test_runner_config.py`, `test_capacity_apply_seams.py`, `test_product_admission.py`, `test_backlog_policy_seams.py`, `test_backlog_dispatch.py`, `test_capacity_readout.py`, `test_automation_config_store.py`, `test_pm_policy_coverage.py` → PASS (several monkeypatch `_backlog_read_config` on the orchestrator — that still works since the method exists; if one monkeypatches `product_admission.read_automation_config` expecting the snapshot to use it, retarget it to `runner_config.read_automation_config` and name it). ruff + mypy on the three files.

- [ ] **Step 6: Commit**

```bash
git add server/automations/runner_config.py server/sessions/orchestrator.py server/automations/product_admission.py server/tests/test_runner_config.py
printf '%s\n' 'refactor(capacity): one runner-config read + plan overlay for the gate and admission' '' 'spec 2026-09-20 D11 second half.' '' 'Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>' > /tmp/pr3-t3.txt
git commit -F /tmp/pr3-t3.txt
```

---

### Task 4: Constraint code rename, and the account quota on the readout

**Files:**
- Modify: `server/automations/product_policy.py:449`, `server/automations/capacity_readout.py:31-42` and the `accounts` dict, `server/automations/backlog_runner.py:1362`
- Modify (mechanical): `server/tests/test_capacity_apply_seams.py`, `server/tests/test_product_admission.py`, `server/tests/test_product_implementation_decision.py`
- Test: `server/tests/test_capacity_readout.py`, `server/tests/test_product_implementation_decision.py`

**Interfaces:**
- Produces: constraint codes `runner_ceiling_unknown | runner_ceiling_held | runner_ceiling_full | runner_ceiling_available` (text "The runner ceiling limit is reached (3/3)" etc.); `LAYER_OF_CODE["runner_ceiling_full"] == "runner"`, `["runner_ceiling_held"] == "runner"`; `GET /api/capacity` → `accounts.sessionsPerAccount: int` (governor config, default 2) and `accounts.accountCap: int` (= `available × sessionsPerAccount`).

- [ ] **Step 1: Failing tests**

`sed -i 's/global_capacity_/runner_ceiling_/g'` on the three test files listed above (they assert the literal; this is the whole mechanical change — review the diff to confirm only those literals moved). Append to `server/tests/test_capacity_readout.py`:

```python
def test_readout_carries_the_account_quota_for_the_runner_row():
    """spec 2026-09-20 §5.2 (Runner row: 'quota: N accounts × M'): available × sessions_per_account."""
    r = asyncio.run(build_readout(_make_sources(
        governor_config=("act", {"push_level": "steady", "sessions_per_account": 3}))))
    assert r["accounts"]["sessionsPerAccount"] == 3
    assert r["accounts"]["accountCap"] == r["accounts"]["available"] * 3


def test_runner_layer_codes_are_named_for_the_runner_ceiling():
    from server.automations.capacity_readout import LAYER_OF_CODE
    assert LAYER_OF_CODE["runner_ceiling_full"] == "runner"
    assert LAYER_OF_CODE["runner_ceiling_held"] == "runner"
    assert "global_capacity_full" not in LAYER_OF_CODE
```

- [ ] **Step 2: Run to verify they fail**

→ the renamed asserts fail (codes still `global_capacity_*`); the readout tests fail with `KeyError`.

- [ ] **Step 3: Implement**

`server/automations/product_policy.py`: `capacity("global_capacity", global_count, global_cap, occupied=existing_slot)` → `capacity("runner_ceiling", global_count, global_cap, occupied=existing_slot)`; add above it the comment `# "runner_ceiling": decide()'s global_cap is the Backlog Runner's max_concurrent (spec 2026-09-20 D8/C16), not AppSettings' global cap — that one is global_cap_full in the readout.`

`server/automations/capacity_readout.py`: in `LAYER_OF_CODE` replace the two keys `"global_capacity_full"` → `"runner_ceiling_full"` and `"global_capacity_held"` → `"runner_ceiling_held"`. In `build_readout`, after `reserve = RESERVE_PCT.get(push_level, 20)` add `sessions_per_account = int(gov.get("sessions_per_account") or 2)`, and in the return dict's `"accounts"` entry add `"sessionsPerAccount": sessions_per_account, "accountCap": accounts_available * sessions_per_account,` after `"reservePct": reserve,`.

`server/automations/backlog_runner.py`: `elif "global_capacity_full" in codes:` → `elif "runner_ceiling_full" in codes:`.

- [ ] **Step 4: Run**

`grep -rn "global_capacity" server/ web/src` → no matches. Run `test_capacity_readout.py`, `test_capacity_api.py`, `test_product_implementation_decision.py`, `test_product_admission.py`, `test_capacity_apply_seams.py`, `test_backlog_runner_loop.py`, `test_backlog_dispatch.py` → PASS. ruff on the three source files.

- [ ] **Step 5: Commit**

```bash
git add server/automations/product_policy.py server/automations/capacity_readout.py server/automations/backlog_runner.py server/tests/test_capacity_apply_seams.py server/tests/test_product_admission.py server/tests/test_product_implementation_decision.py server/tests/test_capacity_readout.py
printf '%s\n' 'feat(capacity): runner_ceiling_* constraint codes; account quota on the readout' '' 'spec 2026-09-20 D8/C16; accounts.sessionsPerAccount + accountCap for the Runner row (PR 2 deferral).' '' 'Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>' > /tmp/pr3-t4.txt
git commit -F /tmp/pr3-t4.txt
```

---

### Task 5: Web — read-only Config-tab rows, the Runner stack row's anchor and quota clause

**Files:**
- Modify: `web/src/api/capacity.ts:36-42`, `web/src/features/delivery/automation/ConfigPanel.tsx:37-57`, `web/src/features/delivery/capacity/capacityStack.ts:102-107`, `web/src/features/delivery/__fixtures__/automations.ts:17-22`, `web/src/features/delivery/capacity/__fixtures__/capacity.ts` (accounts), `web/src/features/delivery/capacity/capacityStack.test.ts` (the `readout()` helper's `accounts`)
- Test: `web/src/features/delivery/automation/AutomationDetail.test.tsx`, `web/src/features/delivery/capacity/capacityStack.test.ts`

**Interfaces:**
- Produces: `CapacityAccounts.sessionsPerAccount: number; accountCap: number`; Runner row `href: '#runner'`; Runner row `secondary` = the non-empty of [`stored ceiling N` (when effective ≠ ceiling), `quota binds: A accounts × M = K` (when `accountCap < ceiling`)] joined by ` · `; `ConfigPanel` renders a `surface === 'capacity'` field as a read-only row with its value and a "Set on Delivery → Capacity" link.

- [ ] **Step 1: Failing tests**

In `AutomationDetail.test.tsx`, add (the `AUTOMATIONS[1]` runner fixture gains a schema in Step 3):

```ts
  it('Config tab: a Capacity-owned field is read-only and links there (spec 2026-09-20 D7)', () => {
    mount(AUTOMATIONS[1], '/delivery/automations/backlog-runner?tab=config')
    expect(screen.queryByRole('spinbutton', { name: 'Runner ceiling' })).toBeNull()
    expect(screen.getByText('Runner ceiling')).toBeInTheDocument()
    expect(screen.getByText('5')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Set on Delivery → Capacity' })).toHaveAttribute('href', '/delivery?scope=capacity')
    expect(screen.getByRole('spinbutton', { name: 'Max issues per PR' })).toBeInTheDocument()
  })
```

In `capacityStack.test.ts`, inside the Runner-row `describe`, add:

```ts
  it('links to the runner form on the same page', () => {
    expect(buildCapacityStack(readout()).find((row) => row.key === 'runner')!.href).toBe('#runner')
  })
  it('says when account quota binds below the stored ceiling, alongside the stored-ceiling note', () => {
    const r = readout({
      runner: { ceiling: 6, effective: 3, inFlight: 2, derived: 3 },
      accounts: { available: 2, total: 3, reservePct: 20, nextResetAt: null, rows: [], sessionsPerAccount: 2, accountCap: 4 },
    })
    expect(buildCapacityStack(r).find((row) => row.key === 'runner')!.secondary)
      .toBe('stored ceiling 6 · quota binds: 2 accounts × 2 = 4')
  })
  it('omits the quota clause when accounts can carry the ceiling', () => {
    const r = readout({
      runner: { ceiling: 4, effective: 4, inFlight: 2, derived: null },
      accounts: { available: 3, total: 3, reservePct: 20, nextResetAt: null, rows: [], sessionsPerAccount: 2, accountCap: 6 },
    })
    expect(buildCapacityStack(r).find((row) => row.key === 'runner')!.secondary).toBeUndefined()
  })
```

- [ ] **Step 2: Run to verify they fail**

`(cd web && npx vitest run src/features/delivery/automation/AutomationDetail.test.tsx src/features/delivery/capacity/capacityStack.test.ts)` → FAIL.

- [ ] **Step 3: Implement**

`web/src/api/capacity.ts` — `CapacityAccounts` gains:

```ts
  /** Governor config `sessions_per_account` (spec 2026-09-20 §5.2 Runner row). */
  sessionsPerAccount: number
  /** `available × sessionsPerAccount`: the most runner sessions the accounts can carry. */
  accountCap: number
```

Fixtures: add `sessionsPerAccount: 2, accountCap: 4,` to `__fixtures__/capacity.ts`'s `accounts` and to `capacityStack.test.ts`'s `readout()` helper `accounts`.

`web/src/features/delivery/__fixtures__/automations.ts` — the `backlog-runner` entry: `configSchema: [ { key: 'max_concurrent', label: 'Runner ceiling', type: 'int', default: 1, help: 'Most sessions the Backlog Runner may hold at once.', choices: null, surface: 'capacity' }, { key: 'max_issues_per_pr', label: 'Max issues per PR', type: 'int', default: 1, help: '', choices: null } ], config: { max_concurrent: 5, max_issues_per_pr: 1 },`.

`capacityStack.ts` — Runner row:

```ts
    {
      key: 'runner', label: 'Runner ceiling', used: r.runner.inFlight, cap: r.runner.effective, free: runnerFree,
      // The runner ceiling is edited on this page (spec 2026-09-20 D7): anchor to the form.
      href: '#runner', binding: isBindingLayer(r.binding, 'runner'),
      secondary: joinClauses(
        r.runner.effective !== r.runner.ceiling ? `stored ceiling ${r.runner.ceiling}` : null,
        r.accounts.accountCap < r.runner.ceiling
          ? `quota binds: ${r.accounts.available} account${r.accounts.available === 1 ? '' : 's'} × ${r.accounts.sessionsPerAccount} = ${r.accounts.accountCap}`
          : null,
      ),
    },
```

with, above `buildCapacityStack`:

```ts
/** A row's secondary line from optional clauses; undefined when none apply. */
function joinClauses(...clauses: (string | null)[]): string | undefined {
  const present = clauses.filter((c): c is string => c !== null)
  return present.length > 0 ? present.join(' · ') : undefined
}
```

Remove the now-unused `automationPath` import if nothing else in the file uses it.

`ConfigPanel.tsx` — add `import { Link } from 'react-router-dom'` and `import { deliveryPath } from '../deliveryNavState'`; in the `.map`, before the `field.type === 'bool'` branch:

```tsx
          field.surface === 'capacity'
            ? (
              // Owned by Delivery → Capacity (spec 2026-09-20 D7): shown, never edited here.
              <div key={field.key} className="flex flex-col gap-0.5">
                <p className="text-sm font-medium text-ink">{field.label}</p>
                <p className="text-sm text-ink">{String(localConfig[field.key] ?? '—')}</p>
                <p className="text-xs text-ink-muted">
                  {field.help}{field.help ? ' ' : ''}
                  <Link to={deliveryPath('capacity')} className="text-accent hover:underline">Set on Delivery → Capacity</Link>
                </p>
              </div>
            )
            : field.type === 'bool'
            ? ( … existing … )
            : ( … existing … )
```

- [ ] **Step 4: Run**

`(cd web && npx vitest run src/features/delivery/ && npm run typecheck && npx eslint --max-warnings 0 src/features/delivery/automation/ConfigPanel.tsx src/features/delivery/capacity/capacityStack.ts src/api/capacity.ts)` → PASS. If typecheck names another typed `CapacityReadout` literal, add the two account keys there and name it.

- [ ] **Step 5: Commit**

```bash
git add web/src/api/capacity.ts web/src/features/delivery/automation/ConfigPanel.tsx web/src/features/delivery/capacity/capacityStack.ts web/src/features/delivery/capacity/capacityStack.test.ts web/src/features/delivery/__fixtures__/automations.ts web/src/features/delivery/capacity/__fixtures__/capacity.ts web/src/features/delivery/automation/AutomationDetail.test.tsx
printf '%s\n' 'feat(web): Config tab shows Capacity-owned fields read-only; Runner row anchors and names quota' '' 'spec 2026-09-20 D7, §5.2.' '' 'Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>' > /tmp/pr3-t5.txt
git commit -F /tmp/pr3-t5.txt
```

---

### Task 6: The runner form on the Capacity page

**Files:**
- Create: `web/src/features/delivery/capacity/runnerForm.ts`
- Modify: `web/src/features/delivery/capacity/CapacityDetail.tsx:213-272` (form), `:364-397` (table + SaveBar), `:59-158` (`CapacityProductRow` pin key)
- Test: `web/src/features/delivery/capacity/runnerForm.test.ts` (new), `web/src/features/delivery/capacity/CapacityDetail.test.tsx`

**Interfaces:**
- Produces (in `runnerForm.ts`):
  ```ts
  export const PIN_KEY = (slug: string) => `pin:${slug}`
  export const RUNNER_KEYS = { ceiling: 'runner:max_concurrent', budget: 'runner:max_spawns_per_day', perAccount: 'governor:sessions_per_account' } as const
  export type CapacityDraft = Record<string, string>
  export function seedCapacityDraft(readout: CapacityReadout, runnerConfig: Record<string, unknown> | null, governorConfig: Record<string, unknown> | null): CapacityDraft
  export interface RunnerPatches { runner: Record<string, number> | null; governor: Record<string, number> | null; ceilingRaised: boolean }
  export function parseRunnerDraft(draft: CapacityDraft, current: { ceiling: number; budget: number | null; perAccount: number | null; globalCap: number }): RunnerPatches  // throws Error(message) on an invalid field
  ```
- Consumes: `usePatchAutomation().mutateAsync`, `ConfirmDialog` (`@/ui/ConfirmDialog`), `Field` (`@/ui/Field`), the existing `useSettingsForm` + `SaveBar`.

- [ ] **Step 1: Failing tests**

```ts
// web/src/features/delivery/capacity/runnerForm.test.ts
import { describe, it, expect } from 'vitest'
import { CAPACITY } from './__fixtures__/capacity'
import { PIN_KEY, RUNNER_KEYS, parseRunnerDraft, seedCapacityDraft } from './runnerForm'

const CURRENT = { ceiling: 5, budget: 10, perAccount: 2, globalCap: 20 }

describe('seedCapacityDraft (spec 2026-09-20 §5.2 item 3)', () => {
  it('namespaces pins and seeds the runner and governor fields from their configs', () => {
    const d = seedCapacityDraft(CAPACITY, { max_concurrent: 5, max_spawns_per_day: 10 }, { sessions_per_account: 2 })
    expect(d[PIN_KEY('kindash')]).toBe('2')
    expect(d[RUNNER_KEYS.ceiling]).toBe('5')
    expect(d[RUNNER_KEYS.budget]).toBe('10')
    expect(d[RUNNER_KEYS.perAccount]).toBe('2')
  })
  it('falls back to the readout ceiling and blanks when an automation is not registered', () => {
    const d = seedCapacityDraft(CAPACITY, null, null)
    expect(d[RUNNER_KEYS.ceiling]).toBe(String(CAPACITY.runner.ceiling))
    expect(d[RUNNER_KEYS.budget]).toBe('')
    expect(d[RUNNER_KEYS.perAccount]).toBe('')
  })
})

describe('parseRunnerDraft', () => {
  const base = { [RUNNER_KEYS.ceiling]: '5', [RUNNER_KEYS.budget]: '10', [RUNNER_KEYS.perAccount]: '2' }
  it('unchanged values produce no patches', () => {
    expect(parseRunnerDraft(base, CURRENT)).toEqual({ runner: null, governor: null, ceilingRaised: false })
  })
  it('a lowered ceiling and a changed budget patch the runner; per-account patches the governor', () => {
    expect(parseRunnerDraft({ ...base, [RUNNER_KEYS.ceiling]: '4', [RUNNER_KEYS.budget]: '12', [RUNNER_KEYS.perAccount]: '3' }, CURRENT))
      .toEqual({ runner: { max_concurrent: 4, max_spawns_per_day: 12 }, governor: { sessions_per_account: 3 }, ceilingRaised: false })
  })
  it('a raised ceiling is flagged for confirmation', () => {
    expect(parseRunnerDraft({ ...base, [RUNNER_KEYS.ceiling]: '8' }, CURRENT).ceilingRaised).toBe(true)
  })
  it('refuses a ceiling above the global cap, naming the cap', () => {
    expect(() => parseRunnerDraft({ ...base, [RUNNER_KEYS.ceiling]: '21' }, CURRENT))
      .toThrow('Runner ceiling must not exceed the global cap (20)')
  })
  it('refuses non-integers, negatives, a blank ceiling, and sessions per account below 1', () => {
    expect(() => parseRunnerDraft({ ...base, [RUNNER_KEYS.ceiling]: '' }, CURRENT)).toThrow('Runner ceiling must be a whole number ≥ 0')
    expect(() => parseRunnerDraft({ ...base, [RUNNER_KEYS.ceiling]: '2.5' }, CURRENT)).toThrow('Runner ceiling must be a whole number ≥ 0')
    expect(() => parseRunnerDraft({ ...base, [RUNNER_KEYS.budget]: '-1' }, CURRENT)).toThrow('Daily budget must be a whole number ≥ 0')
    expect(() => parseRunnerDraft({ ...base, [RUNNER_KEYS.perAccount]: '0' }, CURRENT)).toThrow('Sessions per account must be a whole number ≥ 1')
  })
  it('ignores a blank budget or per-account when the automation is not registered (no current value)', () => {
    expect(parseRunnerDraft({ ...base, [RUNNER_KEYS.budget]: '', [RUNNER_KEYS.perAccount]: '' }, { ...CURRENT, budget: null, perAccount: null }))
      .toEqual({ runner: null, governor: null, ceilingRaised: false })
  })
})
```

In `CapacityDetail.test.tsx`: add a `RUNNER` fixture beside `GOVERNOR` and give `GOVERNOR` a config:

```ts
const RUNNER = {
  id: 'backlog-runner', name: 'Backlog runner', description: '', enabled: true,
  mode: 'on', modes: ['off', 'on'],
  status: { text: 'Idle', level: 'idle' as const, counts: null },
  enginePolicy: 'claude_only' as const, operatorTriggered: false,
  configSchema: [], config: { max_concurrent: 5, max_spawns_per_day: 10 }, schedule: null, lastActiveAt: null, activeSessions: 0,
}
```

(`GOVERNOR.config` becomes `{ push_level: 'steady', sessions_per_account: 2 }`.) In `setup()`, `useAutomations` returns `queryResult([GOVERNOR, RUNNER])`, and the shared `usePatchAutomation` mock becomes `mutationResult({ mutate: patchMutate, mutateAsync: patchMutateAsync })` with `const patchMutateAsync = vi.fn().mockResolvedValue({})` beside `patchMutate`; `beforeEach` clears it. `CAPACITY.runner.ceiling` in the fixture is 5 already; `CAPACITY.global.cap` is 20. Add tests:

```ts
  it('the runner form seeds from the automation configs and sits in the one form (spec 2026-09-20 §5.2 item 3)', () => {
    setup()
    expect(screen.getByLabelText('Runner ceiling')).toHaveValue('5')
    expect(screen.getByLabelText('Daily budget')).toHaveValue('10')
    expect(screen.getByLabelText('Sessions per account')).toHaveValue('2')
    expect(screen.getAllByRole('button', { name: 'Save' })).toHaveLength(1)
    expect(document.getElementById('runner')).not.toBeNull()
  })

  it('lowering the ceiling saves runner-first without a confirmation', async () => {
    setup()
    fireEvent.change(screen.getByLabelText('Runner ceiling'), { target: { value: '4' } })
    fireEvent.change(screen.getByLabelText('KinDash pin'), { target: { value: '3' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(updateProductPolicy).toHaveBeenCalled())
    expect(patchMutateAsync).toHaveBeenCalledWith({ id: 'backlog-runner', body: { config: { max_concurrent: 4 } } })
    expect(screen.queryByRole('dialog')).toBeNull()
    const runnerCall = patchMutateAsync.mock.invocationCallOrder[0]
    const pinCall = updateProductPolicy.mock.invocationCallOrder[0]
    expect(runnerCall).toBeLessThan(pinCall)
  })

  it('raising the ceiling asks for confirmation, then saves', async () => {
    setup()
    fireEvent.change(screen.getByLabelText('Runner ceiling'), { target: { value: '8' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).getByText(/8/)).toBeInTheDocument()
    fireEvent.click(within(dialog).getByRole('button', { name: 'Raise the ceiling' }))
    await waitFor(() => expect(patchMutateAsync).toHaveBeenCalledWith({ id: 'backlog-runner', body: { config: { max_concurrent: 8 } } }))
  })

  it('a ceiling above the global cap is refused before anything is sent', async () => {
    setup()
    fireEvent.change(screen.getByLabelText('Runner ceiling'), { target: { value: '21' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    expect(await screen.findByText('Runner ceiling must not exceed the global cap (20)')).toBeInTheDocument()
    expect(patchMutateAsync).not.toHaveBeenCalled()
  })

  it('sessions per account patches the governor', async () => {
    setup()
    fireEvent.change(screen.getByLabelText('Sessions per account'), { target: { value: '3' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(patchMutateAsync).toHaveBeenCalledWith({ id: 'capacity-governor', body: { config: { sessions_per_account: 3 } } }))
  })
```

(`ConfirmDialog`'s confirm button label will be `Raise the ceiling`; if the dialog's role query needs `{ hidden: true }` in this jsdom because of a portal, say so.) Existing pin tests: anything that called `pinForm.set(slug, …)` through the UI still works; a test that reads the draft key by slug directly (there is none — the UI uses `aria-label="<name> pin"`) is unaffected.

- [ ] **Step 2: Run to verify they fail**

`(cd web && npx vitest run src/features/delivery/capacity/runnerForm.test.ts src/features/delivery/capacity/CapacityDetail.test.tsx)` → FAIL (module missing; no "Runner ceiling" field).

- [ ] **Step 3: The pure helpers**

```ts
// web/src/features/delivery/capacity/runnerForm.ts
import type { CapacityReadout } from '@/api/capacity'

/** One namespaced draft for the whole Capacity page's Form save model (spec
 *  2026-09-20 §5.2 item 3): product pins plus the runner's and governor's
 *  Capacity-owned fields, so ONE SaveBar saves all of it. Namespacing keeps a
 *  product slug from ever colliding with a runner key. */
export const PIN_KEY = (slug: string) => `pin:${slug}`
export const RUNNER_KEYS = {
  ceiling: 'runner:max_concurrent',
  budget: 'runner:max_spawns_per_day',
  perAccount: 'governor:sessions_per_account',
} as const

export type CapacityDraft = Record<string, string>

function str(v: unknown): string {
  return typeof v === 'number' ? String(v) : ''
}

export function seedCapacityDraft(
  readout: CapacityReadout,
  runnerConfig: Record<string, unknown> | null,
  governorConfig: Record<string, unknown> | null,
): CapacityDraft {
  const draft: CapacityDraft = {}
  for (const p of readout.products) draft[PIN_KEY(p.slug)] = String(p.pin)
  // The readout's `ceiling` IS the stored max_concurrent, so it is the fallback
  // when the automations list has not loaded; budget and per-account have no
  // readout twin and stay blank (the fields render disabled) until it has.
  draft[RUNNER_KEYS.ceiling] = runnerConfig ? str(runnerConfig.max_concurrent) || String(readout.runner.ceiling) : String(readout.runner.ceiling)
  draft[RUNNER_KEYS.budget] = runnerConfig ? str(runnerConfig.max_spawns_per_day) : ''
  draft[RUNNER_KEYS.perAccount] = governorConfig ? str(governorConfig.sessions_per_account) : ''
  return draft
}

export interface RunnerPatches {
  runner: Record<string, number> | null
  governor: Record<string, number> | null
  /** The ceiling went up: raising spends quota, so the page confirms first. */
  ceilingRaised: boolean
}

function whole(raw: string, label: string, min: number): number {
  const n = Number(raw.trim())
  if (raw.trim() === '' || !Number.isInteger(n) || n < min) {
    throw new Error(`${label} must be a whole number ≥ ${min}`)
  }
  return n
}

/** Validates and diffs the runner/governor part of the draft. Throws with the
 *  user-facing message on an invalid field (the form's banner shows it). */
export function parseRunnerDraft(
  draft: CapacityDraft,
  current: { ceiling: number; budget: number | null; perAccount: number | null; globalCap: number },
): RunnerPatches {
  const ceiling = whole(draft[RUNNER_KEYS.ceiling] ?? '', 'Runner ceiling', 0)
  if (ceiling > current.globalCap) {
    throw new Error(`Runner ceiling must not exceed the global cap (${current.globalCap})`)
  }
  const runner: Record<string, number> = {}
  if (ceiling !== current.ceiling) runner.max_concurrent = ceiling
  if (current.budget !== null) {
    const budget = whole(draft[RUNNER_KEYS.budget] ?? '', 'Daily budget', 0)
    if (budget !== current.budget) runner.max_spawns_per_day = budget
  }
  let governor: Record<string, number> | null = null
  if (current.perAccount !== null) {
    const perAccount = whole(draft[RUNNER_KEYS.perAccount] ?? '', 'Sessions per account', 1)
    if (perAccount !== current.perAccount) governor = { sessions_per_account: perAccount }
  }
  return {
    runner: Object.keys(runner).length > 0 ? runner : null,
    governor,
    ceilingRaised: ceiling > current.ceiling,
  }
}
```

- [ ] **Step 4: The component**

In `CapacityDetail.tsx`:

Imports: add `import { ConfirmDialog } from '@/ui/ConfirmDialog'`, `import { Field } from '@/ui/Field'`, `import { PIN_KEY, RUNNER_KEYS, parseRunnerDraft, seedCapacityDraft } from './runnerForm'`.

After `const governor = …` add:

```tsx
  const runner = automations.data?.find((a) => a.id === BACKLOG_RUNNER_ID) ?? null
  const runnerPatch = usePatchAutomation()
  // The raise confirmation (spec 2026-09-20 §5.2 item 3): a promise the dialog
  // resolves, so Save is genuinely suspended until the user answers — the same
  // shape Settings › General uses for its archive-path move.
  const [confirmRaise, setConfirmRaise] = useState<{ resolve: (ok: boolean) => void; to: number } | null>(null)
```

with `const BACKLOG_RUNNER_ID = 'backlog-runner'` beside `CAPACITY_GOVERNOR_ID`.

Replace the `pinInitial` memo and the `pinForm` hook with:

```tsx
  const draftInitial = useMemo(
    () => (readout ? seedCapacityDraft(readout, runner?.config ?? null, governor?.config ?? null) : undefined),
    [readout, runner, governor],
  )
  const currentRunner = readout ? {
    ceiling: readout.runner.ceiling,
    budget: runner && typeof runner.config.max_spawns_per_day === 'number' ? runner.config.max_spawns_per_day : null,
    perAccount: governor && typeof governor.config.sessions_per_account === 'number' ? governor.config.sessions_per_account : null,
    globalCap: readout.global.cap,
  } : null
  const pinForm = useSettingsForm<Record<string, string>>({
    initial: draftInitial,
    // Validation runs here so an invalid runner field aborts BEFORE any product
    // pin is sent; a raised ceiling additionally waits on the dialog.
    beforeSave: (draft) => {
      if (!currentRunner) return true
      const parsed = parseRunnerDraft(draft, currentRunner)  // throws → form.error
      if (!parsed.ceilingRaised || parsed.runner?.max_concurrent === undefined) return true
      return new Promise<boolean>((resolve) => setConfirmRaise({ resolve, to: parsed.runner!.max_concurrent }))
    },
    save: async (draft) => {
      if (!readout) return
      // Runner and governor first (spec 2026-09-20 §5.2 item 3): a product 409
      // below must not strand a ceiling change the user already confirmed.
      if (currentRunner) {
        const parsed = parseRunnerDraft(draft, currentRunner)
        if (parsed.runner && runner) await runnerPatch.mutateAsync({ id: runner.id, body: { config: parsed.runner } })
        if (parsed.governor && governor) await runnerPatch.mutateAsync({ id: governor.id, body: { config: parsed.governor } })
      }
      for (const product of readout.products) {
        const raw = draft[PIN_KEY(product.slug)]
        … (the existing loop body, unchanged, from `if (raw === undefined) continue` through `qc.setQueryData(...)`) …
      }
      qc.invalidateQueries({ queryKey: QK.capacity })
      qc.invalidateQueries({ queryKey: QK.roadmapMap })
    },
  })
```

Check `useSettingsForm`'s `beforeSave` handling: if a thrown error inside `beforeSave` is NOT caught into `form.error` (read `useSettingsForm.ts` `doSave`), move the `parseRunnerDraft` validation call into `save` as its first statement (it throws there and the banner shows it), and keep `beforeSave` only for the raise dialog by wrapping the parse in `try { … } catch { return true }` so `save` surfaces the message. Say which you did.

Products table: `pinValue={pinForm.draft?.[PIN_KEY(product.slug)] ?? String(product.pin)}` and `onPinChange={(value) => pinForm.set(PIN_KEY(product.slug), value)}`.

The runner fieldset — inside the `<div className="overflow-x-auto">`, BEFORE `<table id="products" …>`:

```tsx
        <fieldset id="runner" className="mb-4 grid gap-3 sm:grid-cols-3">
          <legend className="mb-2 text-sm font-medium text-ink">Runner</legend>
          <Field label="Runner ceiling" htmlFor="runner-ceiling"
            help={`Most sessions the Backlog Runner may hold at once; a share of the global cap (${readout.global.cap}). Quota (push level) and the governor lower it, never raise it.`}>
            <Input id="runner-ceiling" type="text" inputMode="numeric" size="sm"
              value={pinForm.draft?.[RUNNER_KEYS.ceiling] ?? ''}
              onChange={(e) => pinForm.set(RUNNER_KEYS.ceiling, e.target.value)} />
          </Field>
          <Field label="Daily budget" htmlFor="runner-budget"
            help={runner ? 'Circuit-breaker: pause spawning past this many sessions per day.' : 'Backlog Runner is not registered'}>
            <Input id="runner-budget" type="text" inputMode="numeric" size="sm" disabled={!runner}
              value={pinForm.draft?.[RUNNER_KEYS.budget] ?? ''}
              onChange={(e) => pinForm.set(RUNNER_KEYS.budget, e.target.value)} />
          </Field>
          <Field label="Sessions per account" htmlFor="runner-per-account"
            help={governor ? 'Concurrent runner sessions one account can carry before its quota burns faster than it resets.' : 'Capacity Governor is not registered'}>
            <Input id="runner-per-account" type="text" inputMode="numeric" size="sm" disabled={!governor}
              value={pinForm.draft?.[RUNNER_KEYS.perAccount] ?? ''}
              onChange={(e) => pinForm.set(RUNNER_KEYS.perAccount, e.target.value)} />
          </Field>
        </fieldset>
```

(`type="text"` + `inputMode="numeric"`, as the Hosts cap and Limits fields do, so "abc" reaches validation instead of being swallowed by a number input.)

The dialog — beside the plan card, at the end of the returned tree:

```tsx
      <ConfirmDialog
        open={confirmRaise !== null}
        onClose={() => { confirmRaise?.resolve(false); setConfirmRaise(null) }}
        onConfirm={() => { confirmRaise?.resolve(true); setConfirmRaise(null) }}
        title="Raise the runner ceiling?"
        description={`Raising the ceiling to ${confirmRaise?.to ?? ''} lets the Backlog Runner hold more sessions at once and spends account quota faster. Lowering it never interrupts a running session.`}
        confirmLabel="Raise the ceiling"
        tone="warning"
      />
```

Update the "Ruling 12" comment above the form to say the draft is now the whole page's Form model (pins + runner + governor), keys namespaced by `runnerForm.ts`.

- [ ] **Step 5: Run**

`(cd web && npx vitest run src/features/delivery/capacity/ && npm run typecheck && npx eslint --max-warnings 0 src/features/delivery/capacity/CapacityDetail.tsx src/features/delivery/capacity/runnerForm.ts src/features/delivery/capacity/runnerForm.test.ts src/features/delivery/capacity/CapacityDetail.test.tsx)` → PASS, no act() warnings.

- [ ] **Step 6: Commit**

```bash
git add web/src/features/delivery/capacity/runnerForm.ts web/src/features/delivery/capacity/runnerForm.test.ts web/src/features/delivery/capacity/CapacityDetail.tsx web/src/features/delivery/capacity/CapacityDetail.test.tsx
printf '%s\n' 'feat(web): runner ceiling, daily budget and sessions per account on the Capacity page, one SaveBar' '' 'Runner-first save order; confirm on a raise; ceiling <= global cap checked client-side too. spec 2026-09-20 §5.2 item 3, D7.' '' 'Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>' > /tmp/pr3-t6.txt
git commit -F /tmp/pr3-t6.txt
```

---

### Task 7: Docs

**Files:**
- Modify: `docs/product/features/capacity-governor.md`, `docs/product/features/autopilot-backlog-runner.md` (only if it names "Max occupied slots" / the config tab as the ceiling's editor — `grep -n "occupied\|max_concurrent" docs/product/features/*.md`)

- [ ] **Step 1: Edit**

In `capacity-governor.md`, after the paragraph PR 1 added about the two ceilings, append:

```markdown
Since spec PR 3 (2026-09-20) the runner ceiling (`max_concurrent`), the daily budget
(`max_spawns_per_day`) and `sessions_per_account` are edited on Delivery → Capacity in the same
form as the product pins; the automations' Config tabs show them read-only (`ConfigField.surface =
"capacity"`). A runner ceiling above the effective global cap is refused (422). The runner-layer
admission code is `runner_ceiling_full` (was `global_capacity_full`); the readout's `accounts`
block carries `sessionsPerAccount` and `accountCap` so the Runner row can say when quota binds.
`PATCH /api/automations/{id}` merges a partial `config` over the stored row.
```

Replace any "Max occupied slots" wording in the docs grep found with "Runner ceiling" and add "(edited on Delivery → Capacity)".

- [ ] **Step 2: Commit**

```bash
git add docs/product/features/
printf '%s\n' 'docs(capacity): the runner form, the surface flag and the renamed runner-layer code' '' 'Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>' > /tmp/pr3-t7.txt
git commit -F /tmp/pr3-t7.txt
```

---

### Task 8: Full gate, then the PR

- [ ] **Step 1: Server gate** — exactly as PR 2's Task 8: `ruff check src/ server/`; `mypy server/ --ignore-missing-imports` (env -i, isolated HOME, `PYTHONPATH=$PWD/src`); `pytest tests/ -q` (`CCSWITCH_DEV=1`); `scripts/brand_ratchet.py` (no `grew`); then the full server suite detached under the test-slot cap via a `/tmp/pr3-gate.sh` script identical to PR 2's `/tmp/pr2-gate.sh` (rename the sid to `pr3-gate-$$` and the log to `/tmp/pr3-server-tests.log`), waited on INSIDE Bash calls with `until grep -q "PYTEST_EXIT=" …; do sleep 30; done`. Known runner-only flakes and the `test_sdlc_evals_runner` env-i PATH artefact re-run alone; anything else fixed and committed.

- [ ] **Step 2: Web gate** — `(cd web && npm run typecheck && npm run lint && npm run test 2>&1 | tee /tmp/pr3-web-tests.log | tail -5; echo "vitest exit=${PIPESTATUS[0]}"; npm run build 2>&1 | tail -3)`; check the echoed exit code; `rm -rf web/dist` afterwards.

- [ ] **Step 3: Rebase, push, PR, auto-merge** — `git fetch origin master && git rebase origin/master`; in-flight PR check (path regex `automations\.py|runner_config|product_admission|capacity_readout|product_policy|ConfigPanel|CapacityDetail|runnerForm|capacityStack`); `git push -u origin feat/capacity-pr3-runner-form`; `gh pr create --title "feat(capacity): the runner form and the surface flag (spec PR 3)" --body-file /tmp/pr3-body.md --base master --head feat/capacity-pr3-runner-form`; `gh pr merge <n> --squash --auto`. Body:

```markdown
## Summary

PR 3 of the capacity-settings spec (`docs/superpowers/specs/2026-09-20-capacity-settings-structure-design.md`, §8): the runner form and the surface flag. Plan: `docs/superpowers/plans/2026-09-20-capacity-settings-pr3-runner-form.md`.

- **`ConfigField.surface`** (`config` | `capacity`): the runner ceiling, daily budget, push level and sessions-per-account are owned by Delivery → Capacity; the automations' Config tabs show them read-only with a link (D7, C10, C12).
- **The runner form** joins the pins' one `SaveBar`: runner ceiling, daily budget, sessions per account, saved runner-first then governor then pins; a raise asks for confirmation (raising spends quota); a ceiling above the effective global cap is refused client-side and server-side (422) (§5.2 item 3, §6).
- **`PATCH /api/automations/{id}` merges a partial `config`** over the stored row — the push-level radio had been resetting `sessions_per_account` to its default on every change (§7).
- **One runner-config seam** (`server/automations/runner_config.py`): the orchestrator's gate and admission's snapshot read the stored ceiling and overlay the plan through one function (D11).
- **`runner_ceiling_*` constraint codes** replace `global_capacity_*` (D8, C16); `global_cap_full` stays the real global cap.
- **The readout carries `accounts.sessionsPerAccount` and `accountCap`**, and the Runner row says "quota binds: N accounts × M = K" when quota is below the stored ceiling (the hint PR 2 deferred).

Labels retired: "Max occupied slots" → **Runner ceiling**; "Max spawns per day" → **Daily budget**. No schema change.

## Verification

- `ruff`, `mypy`, `pytest tests/`, brand ratchet, full `pytest server/tests/ -n auto`, `npm run typecheck`, `npm run lint`, full `npm run test`, `npm run build` — all green on the rebased tree.
- Per-task spec + quality review, whole-branch review, one fix wave with a scoped re-review.

Refs #2060 (spec), #2032 (governor follow-ups), #2040.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
```

---

## Self-review

**Spec coverage (PR 3, §8 row 3):** `ConfigField.surface` → Task 1 (+ TS type). Runner ceiling / daily budget / sessions-per-account on the Capacity page with one SaveBar → Task 6. Read-only link rows on the config tab → Task 5. Ceiling ≤ global cap validation → Task 2 (server 422) and Task 6 (client). One plan-overlay seam (D11 second half) → Task 3. Constraint-code rename (D8) → Task 4. §5.2 item 3's confirmation on a raise → Task 6. PR 2's deferred quota secondary → Tasks 4 (wire) and 5 (row). §8's "Deletes: the second overlay in `gather_implementation_snapshot`" → Task 3 (both callers now call one function; the duplicated `read_automation_config(BacklogRunner, modes=…)` + `apply_fleet_plan` pair exists once). The partial-config merge (Task 2) is not in the spec's PR 3 list; it is a prerequisite the runner form exposed (the push-level radio was already affected) and is recorded in the PR body. §2032's remaining items and D5/D6 (Roadmap card) are PR 4.

**Placeholder scan:** none. Conditional instructions name the exact grep, the exact alternative and ask the implementer to report which path was taken.

**Type consistency:** `surface: str = "config"` (Task 1) ↔ TS `surface?: 'config' | 'capacity'` (Task 1) ↔ `field.surface === 'capacity'` (Task 5). `read_runner_config(*, plan=UNSET, apply_plan=True)` (Task 3) is called with `apply_plan=` by the orchestrator and `plan=` by admission (Task 3). `accounts.sessionsPerAccount` / `accountCap` (Task 4 server) ↔ `CapacityAccounts` (Task 5) ↔ fixtures (Task 5) ↔ `joinClauses` (Task 5). `PIN_KEY`, `RUNNER_KEYS`, `seedCapacityDraft`, `parseRunnerDraft`, `RunnerPatches` (Task 6 helper) match the component's use and the tests' imports. `runner_ceiling_full` (Task 4) is the only code the Backlog Runner branch and `LAYER_OF_CODE` now share; no web consumer names it.

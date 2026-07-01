# Backlog Runner — Plan A: Control Loop & Handoff

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the Backlog Runner's supervised control loop — a restart-safe, non-reentrant tick that keeps up to N implementation sessions in flight, gated by a per-day circuit-breaker and account availability, plus the `should_free_slot` handoff and terminal-watch that hands PRs to PR Doctor and escalates the ones it gives up on.

**Architecture:** A new registered automation `backlog-runner` (config/status/modes) + an orchestrator-owned async loop (a sibling of the pollers). In-flight is **always derived from the DB** (`Session.origin == "backlog-runner"` + non-terminal), never process memory, so a restart resumes cleanly. Selection + claim (the I/O that mutates GitHub) is **Plan B**; this plan calls into stubs for those so the loop is fully testable first. The pure `should_free_slot` lives in `server/automations/backlog_selection.py`.

**Tech Stack:** Python 3 · asyncio · SQLAlchemy async · pytest.

## Global Constraints

- Spec: `docs/superpowers/specs/2026-06-30-backlog-runner-design.md` (§1 loop, §3 handoff/terminal-watch, §5 config, §6 status). Depends on the shell (spawn/state/registry) and — for the handoff — PR Doctor (`auto_merge_enabled` from the field-extension plan; PR-Doctor `gave_up` events).
- In-flight MUST be derived from `Session.origin`, never cached durably.
- The whole refill body runs under a single `asyncio.Lock` (non-reentrant) — an interval tick and an end-of-session nudge must not overlap.
- Config field names/defaults match spec §5 exactly: `enabled_repos` ([]), `eligible_label` ("ready"), `max_concurrent` (1), `max_spawns_per_day` (10), `max_attempts` (2, validated ≥1), `max_issues_per_pr` (1).
- `backlog-runner` `mode` defaults to `off` (never autonomously spawns until explicitly enabled).
- Test env: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 <main-checkout>/.venv/bin/python -m pytest …`.

---

## File Structure

- Create: `server/automations/backlog_selection.py` — pure `should_free_slot(pr_info, pr_doctor_active) -> bool` (rank_and_batch is Plan B).
- Create: `server/automations/backlog_runner.py` — the `BacklogRunner` automation (id/name/modes/config_schema/status) + the loop class `BacklogRunnerLoop`.
- Modify: `server/automations/registry.py` — register `BacklogRunner`.
- Modify: `server/sessions/orchestrator.py` (or app lifespan) — own/start the loop; provide the healthy-account selector + in-flight query the loop needs.
- Tests: `server/tests/test_backlog_should_free_slot.py`, `server/tests/test_backlog_runner_loop.py`.

---

### Task 1: `should_free_slot` (pure) + the BacklogRunner automation definition

**Files:**
- Create: `server/automations/backlog_selection.py`
- Create: `server/automations/backlog_runner.py` (automation class only in this task)
- Modify: `server/automations/registry.py`
- Test: `server/tests/test_backlog_should_free_slot.py`, extend `server/tests/test_automation_registry.py`-style checks in a new `test_backlog_runner_automation.py`

**Interfaces:**
- Produces: `should_free_slot(pr_info, pr_doctor_active) -> bool`; `BacklogRunner` automation (`id="backlog-runner"`, `modes=("off","propose","auto")`, the §5 `config_schema`, cheap `status`).

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_backlog_should_free_slot.py
from server.github.pr_status import PullRequestInfo
from server.automations.backlog_selection import should_free_slot


def _pr(**kw):
    base = dict(number=1, url="u", state="open", checks="passing", title="t")
    base.update(kw); return PullRequestInfo(**base)


def test_pr_doctor_active_frees_on_open_plus_automerge():
    assert should_free_slot(_pr(state="open", auto_merge_enabled=True), True) is True
    assert should_free_slot(_pr(state="open", auto_merge_enabled=False), True) is False


def test_no_pr_doctor_frees_only_on_merge():
    assert should_free_slot(_pr(state="open", auto_merge_enabled=True), False) is False
    assert should_free_slot(_pr(state="merged"), False) is True


def test_merged_always_frees():
    assert should_free_slot(_pr(state="merged"), True) is True
```

Plus `test_backlog_runner_automation.py`: assert `BacklogRunner().id == "backlog-runner"`, config keys/defaults per §5, `max_issues_per_pr` default 1, and it's registered in the module `registry`.

- [ ] **Step 2: Run → fail** (module missing).

- [ ] **Step 3: Implement.**

`server/automations/backlog_selection.py`:

```python
"""Pure Backlog Runner helpers (no I/O). rank_and_batch is added in Plan B."""
from __future__ import annotations

from server.github.pr_status import PullRequestInfo


def should_free_slot(pr_info: PullRequestInfo, pr_doctor_active: bool) -> bool:
    """A runner slot frees when the work item's PR has reached the point where
    something else carries it: PR Doctor keeps CI green toward auto-merge, so if
    PR Doctor covers the repo, free once the PR is open AND auto-merge is armed;
    otherwise (no safety net) hold until the PR actually merges."""
    if pr_info.state == "merged":
        return True
    if pr_doctor_active:
        return pr_info.state in ("open", "draft") and pr_info.auto_merge_enabled
    return False
```

`server/automations/backlog_runner.py` (automation class; the loop class is Task 2):

```python
"""The backlog-runner automation: pulls high-value issues off the backlog and
spawns implementation sessions, handing finished PRs to PR Doctor."""
from __future__ import annotations

from .base import AutomationStatus, ConfigField


class BacklogRunner:
    id = "backlog-runner"
    name = "Backlog Runner"
    description = "Continuously implements high-priority backlog issues into reviewed-ready PRs."
    modes = ("off", "propose", "auto")
    config_schema = [
        ConfigField(key="enabled_repos", label="Repositories", type="repo-multiselect",
                    default=[], help="Repos to draw issues from (empty = all)."),
        ConfigField(key="eligible_label", label="Eligible label", type="string",
                    default="ready", help="Only issues with this label are candidates."),
        ConfigField(key="max_concurrent", label="Max sessions in flight", type="int", default=1),
        ConfigField(key="max_spawns_per_day", label="Max spawns per day", type="int", default=10,
                    help="Circuit-breaker: pause spawning past this many per day."),
        ConfigField(key="max_attempts", label="Max attempts per issue", type="int", default=2),
        ConfigField(key="max_issues_per_pr", label="Max issues per PR", type="int", default=1,
                    help="Bundle up to this many related issues into one PR."),
    ]

    def status(self, ctx) -> AutomationStatus:
        counts = getattr(ctx, "backlog_counts", None) if ctx is not None else None
        if not counts:
            return AutomationStatus(text="idle", level="idle")
        n = counts.get("in_flight", 0)
        return AutomationStatus(
            text=f"{n} implementing, {counts.get('eligible', 0)} eligible"
                 + (f", {counts['needs_human']} needs-human" if counts.get("needs_human") else ""),
            level="active" if n else ("warn" if counts.get("needs_human") else "idle"),
            counts=counts)

    async def on_state_change(self, *, enabled, mode, config, ctx) -> None:
        # The orchestrator's loop reads mode/config each tick; nothing to start here.
        ...
```

Register it in `registry.py` (after the `PrDoctor` registration): `from .backlog_runner import BacklogRunner` + `registry.register(BacklogRunner())`.

- [ ] **Step 4: Run → pass.**
- [ ] **Step 5: Commit** (`feat(backlog-runner): automation definition + pure should_free_slot`).

---

### Task 2: The control loop — refill under a lock, restart-safe in-flight, circuit-breaker, account gate

**Files:** Modify `server/automations/backlog_runner.py` (add `BacklogRunnerLoop`); Modify `server/sessions/orchestrator.py` (own/start the loop; provide `_healthy_account()` + in-flight query); Test `server/tests/test_backlog_runner_loop.py` (create).

**Interfaces:** `BacklogRunnerLoop(ctx, *, select_batch, claim, spawn, count_in_flight, healthy_account, pr_status_for, pr_doctor_active, clock)` — every I/O dependency is INJECTED so the loop is tested with no network/sessions. `async tick()` runs one refill+sweep under `self._lock`.

- [ ] **Step 1: Write the failing test.** With injected fakes: a candidate provider, a claim recorder, a spawn recorder (writes a `Session` row with `origin="backlog-runner"`), an in-flight counter (queries the DB), a `healthy_account` returning `"a"` (or `None`), and a `pr_status_for` map. Assert, one test each:
  - Tops up to `max_concurrent` and no further; a second `tick()` with a still-full in-flight set spawns nothing.
  - Interleaved tick during a slow spawn does NOT double-spawn (drive two `tick()`s concurrently via the lock; exactly `max_concurrent` spawns).
  - `mode == "off"` → no spawn.
  - `healthy_account()` returns `None` → no spawn + `skipped_no_account` emitted (debounced).
  - Circuit-breaker: after `max_spawns_per_day` spawns in the window, `tick()` emits `circuit_open` and spawns no more.
  - Handoff: a spawned session whose `pr_status_for` reports open+auto_merge (with pr_doctor_active) frees the slot next tick (refills).
  - Restart-safety: in-flight recomputed from the DB — a fresh loop instance with existing `origin="backlog-runner"` rows counts them (no double-spawn).

- [ ] **Step 2: Run → fail.**

- [ ] **Step 3: Implement `BacklogRunnerLoop`.** Structure per spec §1 data-flow:

```python
class BacklogRunnerLoop:
    def __init__(self, ctx, *, select_batch, claim, spawn, count_in_flight,
                 healthy_account, pr_status_for, pr_doctor_active, read_config,
                 clock):
        self._ctx = ctx
        self._select_batch = select_batch        # Plan B (async): -> Batch | None
        self._claim = claim                       # Plan B (async): (batch) -> bool
        self._spawn = spawn                       # (batch, account) -> SessionRecord
        self._count_in_flight = count_in_flight   # async -> int (DB: origin==id, !=ended)
        self._healthy_account = healthy_account   # async -> str | None
        self._pr_status_for = pr_status_for       # async (session) -> PullRequestInfo | None
        self._pr_doctor_active = pr_doctor_active # async (repo) -> bool
        self._read_config = read_config           # async -> (mode, config)
        self._clock = clock                       # -> float (injected; no wall-clock in tests)
        self._lock = asyncio.Lock()
        self._spawns_today: list[float] = []      # timestamps within the rolling day

    async def tick(self) -> None:
        async with self._lock:
            await self._refill_and_sweep()

    async def _refill_and_sweep(self) -> None:
        mode, config = await self._read_config()
        # bump last_active_at each tick that ran (stalled detection) — best-effort
        if mode != "off":
            self._prune_day(config)
            if len(self._spawns_today) < config["max_spawns_per_day"]:
                await self._refill(mode, config)
            else:
                await self._ctx.emit("info", "daily spawn cap reached", kind="circuit_open")
        await self._sweep(config)   # handoff + terminal-watch runs regardless of mode

    async def _refill(self, mode, config):
        in_flight = await self._count_in_flight()
        free = config["max_concurrent"] - in_flight
        for _ in range(max(0, free)):
            if len(self._spawns_today) >= config["max_spawns_per_day"]:
                break
            account = await self._healthy_account()
            if account is None:
                await self._ctx.emit("info", "no healthy account", kind="skipped_no_account")
                return
            batch = await self._select_batch(config)   # Plan B
            if batch is None:
                return  # no candidates
            st = await self._ctx.get_target_state(batch.primary)
            if st.get("gave_up"):
                continue
            if not await self._claim(batch):            # Plan B (atomic; rolls back on partial)
                continue
            if mode == "propose":
                await self._ctx.emit("info", f"ready to implement {batch.primary}",
                                     kind="selected", target=batch.primary,
                                     detail={"issues": batch.issues})
                continue  # spawn happens on user tap (Plan B endpoint)
            rec = await self._spawn(batch, account)     # stamps origin/origin_ref via ctx.spawn_session
            self._spawns_today.append(self._clock())
```

`_sweep` iterates in-flight + handed-off targets: on `should_free_slot` → emit `handoff`/`merged`; on a PR-Doctor `gave_up` for the target (query `automation_event` where `automation_id='pr-doctor'`, `kind='gave_up'`, `detail.origin_ref == primary`) OR closed-unmerged → emit `gave_up`, call Plan B's unclaim-all + `needs_human`, delete state; on a session that ended without a mergeable PR → `attempts++`, at `max_attempts` → give up. `_prune_day` drops timestamps older than one day using the injected `clock`.

Wire it in the orchestrator: construct the loop with the real dependencies (`ctx = registry.context_for("backlog-runner", create_session=self.create_session)`; `count_in_flight` = a DB query; `healthy_account` = reuse the existing healthy-account selector / `_account_limited`; `pr_status_for` = the PR poller cache; `pr_doctor_active` = read the `pr-doctor` row's enabled+scope). Start a periodic `tick()` task (interval ~60s) in the same place the pollers start, and nudge it when a `backlog-runner`-origin session ends.

- [ ] **Step 4: Run** `test_backlog_runner_loop.py` → green.
- [ ] **Step 5: Commit** (`feat(backlog-runner): non-reentrant restart-safe control loop + handoff/terminal-watch`).

---

## Self-Review

- **Spec coverage:** §1 loop (non-reentrant, in-flight-from-DB, circuit-breaker, account gate) → Task 2; §3 handoff + terminal-watch → Task 2 `_sweep` + Task 1 `should_free_slot`; §5 config → Task 1; §6 status → Task 1. Selection/claim/batching are injected here and implemented in Plan B. ✓
- **Placeholders:** the loop calls injected `select_batch`/`claim`/`spawn` — these are Plan B's surface, deliberately stubbed so Plan A's control flow is tested in isolation. Not placeholders; explicit seams.
- **Type consistency:** the injected dependency signatures here are the exact contract Plan B implements (`select_batch(config)->Batch|None`, `claim(batch)->bool`, etc.).

## Hand-off to Plan B

Plan B implements the injected seams: `select_batch` (label-gated `gh` query → `rank_and_batch` with the one-shot LLM rank), `claim`/unclaim (atomic label writes + reconciliation), and the propose-tap spawn endpoint — plus a fake-`gh`/fake-LLM/fake-spawn harness so the end-to-end loop test runs with zero network and no real sessions.

# PR Doctor — Plan A: Pure Recovery Engine

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the pure, I/O-free PR Doctor decision engine — `classify(info)` (PR → failure mode), `plan_recovery(...)` (failure mode + mode/config/state → recovery action), and the per-target retry/re-arm/terminate state-machine helpers — fully unit-tested.

**Architecture:** A single new module `server/automations/pr_doctor_engine.py` with no I/O and no imports of orchestrator/DB/gh. It consumes `PullRequestInfo` (already carrying `mergeable`/`merge_state_status` from the field-extension plan) and returns plain dataclasses the orchestrator executes (Plan B). The registered `PrDoctor` automation (from the shell) gains its `config_schema` here. v1 handles **failing CI, out-of-date branch, merge conflict** (stuck/queued checks deferred to v2 per spec).

**Tech Stack:** Python 3 · dataclasses · pytest.

## Global Constraints

- Spec: `docs/superpowers/specs/2026-06-30-pr-doctor-design.md` (§3 classify, §5 config/gating, §6 retry/re-arm/terminate).
- **Depends on:** the PR-status field extension plan (`info.mergeable`, `info.merge_state_status`). Do NOT edit `pr_status.py`.
- `classify` + `plan_recovery` are **pure and total** — no I/O, never raise (unknown → `Notify`/`None`).
- Priority order is a single ordered constant, not re-encoded per call site.
- Config field names + defaults must match the spec §5 table exactly: `enabled_repos` (repo-multiselect, []), `max_attempts` (int, 3), `mechanical_attempts` (int, 2), `auto_update_out_of_date` (bool, true), `auto_fix_failing_ci` (bool, true), `auto_resolve_conflicts` (bool, false). NO `stuck_checks`/`auto_rerun` field in v1.
- Modes sourced from `VALID_AUTOFIX_MODES` (already on `PrDoctor.modes`).
- Test env: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 <main-checkout>/.venv/bin/python -m pytest …`.

---

## File Structure

- Create: `server/automations/pr_doctor_engine.py` — `FailureMode`, the `FAILURE_*` action dataclasses, `classify`, `plan_recovery`, `apply_rearm`, `record_attempt`, `target_terminal`.
- Modify: `server/automations/pr_doctor.py` — replace the placeholder `config_schema` with the full v1 schema (§5); keep `modes`/`status` as-is.
- Test: `server/tests/test_pr_doctor_engine.py` (create).

---

### Task 1: The failure-mode classifier (pure)

**Files:**
- Create: `server/automations/pr_doctor_engine.py`
- Test: `server/tests/test_pr_doctor_engine.py` (create)

**Interfaces:**
- Produces: `FAILURE_PRIORITY: tuple[str, ...]`; `classify(info) -> str | None` returning one of `"blocked"`, `"merge_conflict"`, `"out_of_date"`, `"failing_ci"`, or `None`.

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_pr_doctor_engine.py
"""Pure PR Doctor engine: classify + plan_recovery + state machine."""
from __future__ import annotations

from server.github.pr_status import PullRequestInfo
from server.automations.pr_doctor_engine import classify


def _pr(**kw) -> PullRequestInfo:
    base = dict(number=1, url="u", state="open", checks="passing", title="t",
                mergeable="MERGEABLE", merge_state_status="CLEAN")
    base.update(kw)
    return PullRequestInfo(**base)


def test_classify_terminal_and_draft_are_none():
    assert classify(_pr(state="merged")) is None
    assert classify(_pr(state="closed")) is None
    assert classify(_pr(merge_state_status="DRAFT")) is None
    assert classify(_pr(is_draft=True)) is None


def test_classify_blocked_is_review():
    assert classify(_pr(merge_state_status="BLOCKED")) == "blocked"


def test_classify_conflict_via_mergeable_or_dirty():
    assert classify(_pr(mergeable="CONFLICTING")) == "merge_conflict"
    assert classify(_pr(merge_state_status="DIRTY")) == "merge_conflict"
    # UNKNOWN mergeable with no DIRTY → not a conflict yet
    assert classify(_pr(mergeable="UNKNOWN")) is None


def test_classify_out_of_date():
    assert classify(_pr(merge_state_status="BEHIND")) == "out_of_date"


def test_classify_conflict_beats_failing_ci():
    assert classify(_pr(checks="failing", mergeable="CONFLICTING")) == "merge_conflict"


def test_classify_failing_required_ci():
    assert classify(_pr(checks="failing", merge_state_status="CLEAN")) == "failing_ci"


def test_classify_unstable_nonrequired_is_not_failing_ci():
    # UNSTABLE = mergeable, only a non-required check is red → no session fix
    assert classify(_pr(checks="failing", merge_state_status="UNSTABLE")) is None


def test_classify_healthy_is_none():
    assert classify(_pr(checks="passing", merge_state_status="CLEAN")) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 <main-checkout>/.venv/bin/python -m pytest server/tests/test_pr_doctor_engine.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'server.automations.pr_doctor_engine'`.

- [ ] **Step 3: Write the classifier**

Create `server/automations/pr_doctor_engine.py`:

```python
"""Pure PR Doctor decision engine — no I/O, never raises into the poller.

classify(info) -> failure mode; plan_recovery(...) -> a RecoveryAction the
orchestrator (Plan B) executes; plus per-target retry/re-arm/terminate helpers.
v1 handles failing CI, out-of-date branch, and merge conflict (stuck/queued
checks are deferred to v2)."""
from __future__ import annotations

from dataclasses import dataclass

from server.github.pr_status import PullRequestInfo

# First match wins — conflict before CI (never fight CI on an unmergeable branch).
FAILURE_PRIORITY = ("blocked", "merge_conflict", "out_of_date", "failing_ci")


def classify(info: PullRequestInfo) -> str | None:
    """Return the v1 failure mode for *info*, or None (terminal/draft/healthy).
    Pure + total."""
    if info.state in ("merged", "closed"):
        return None
    if info.merge_state_status == "DRAFT" or info.is_draft:
        return None
    if info.merge_state_status == "BLOCKED":
        return "blocked"  # review/required gate — notify only (non-goal to approve)
    if info.mergeable == "CONFLICTING" or info.merge_state_status == "DIRTY":
        return "merge_conflict"
    if info.merge_state_status == "BEHIND":
        return "out_of_date"
    # failing_ci only for a *required* failure: UNSTABLE = a non-required check is
    # red on a mergeable branch → not actionable. (merge_state_status is the
    # required-ness discriminator the poller always has.)
    if info.checks == "failing" and info.merge_state_status != "UNSTABLE":
        return "failing_ci"
    return None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 <main-checkout>/.venv/bin/python -m pytest server/tests/test_pr_doctor_engine.py -q`
Expected: PASS (8 tests).

- [ ] **Step 5: Commit**

```bash
git add server/automations/pr_doctor_engine.py server/tests/test_pr_doctor_engine.py
git commit -m "feat(pr-doctor): pure failure-mode classifier (conflict/out-of-date/failing-ci)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: Recovery actions + plan_recovery gating (pure)

**Files:**
- Modify: `server/automations/pr_doctor_engine.py` (add action types + `plan_recovery`)
- Test: `server/tests/test_pr_doctor_engine.py` (extend)

**Interfaces:**
- Consumes: `classify` output (Task 1); a `mode` (`off`/`propose`/`auto`), a `config` dict, a `target_state` dict.
- Produces: `RecoveryAction` dataclasses `Notify`, `Propose(kind)`, `Mechanical(kind)`, `SessionFix(kind)`, `GiveUp`; `plan_recovery(failure_mode, mode, config, state) -> RecoveryAction`.

- [ ] **Step 1: Write the failing test (append)**

```python
# append to server/tests/test_pr_doctor_engine.py
from server.automations.pr_doctor_engine import (
    plan_recovery, Notify, Propose, Mechanical, SessionFix, GiveUp,
)

_CFG = {
    "enabled_repos": [], "max_attempts": 3, "mechanical_attempts": 2,
    "auto_update_out_of_date": True, "auto_fix_failing_ci": True,
    "auto_resolve_conflicts": False,
}


def test_mode_off_is_notify():
    assert isinstance(plan_recovery("failing_ci", "off", _CFG, {}), Notify)


def test_blocked_is_always_notify():
    assert isinstance(plan_recovery("blocked", "auto", _CFG, {}), Notify)


def test_propose_mode_proposes_every_mode():
    for fm in ("merge_conflict", "out_of_date", "failing_ci"):
        a = plan_recovery(fm, "propose", _CFG, {})
        assert isinstance(a, Propose) and a.kind == fm


def test_auto_mechanical_out_of_date():
    a = plan_recovery("out_of_date", "auto", _CFG, {})
    assert isinstance(a, Mechanical) and a.kind == "out_of_date"


def test_auto_session_failing_ci():
    a = plan_recovery("failing_ci", "auto", _CFG, {})
    assert isinstance(a, SessionFix) and a.kind == "failing_ci"


def test_auto_conflict_falls_back_to_propose_when_not_opted_in():
    a = plan_recovery("merge_conflict", "auto", _CFG, {})
    assert isinstance(a, Propose)  # auto_resolve_conflicts=False → propose


def test_auto_conflict_session_when_opted_in():
    cfg = {**_CFG, "auto_resolve_conflicts": True}
    a = plan_recovery("merge_conflict", "auto", cfg, {})
    assert isinstance(a, SessionFix) and a.kind == "merge_conflict"


def test_gave_up_state_is_notify():
    a = plan_recovery("failing_ci", "auto", _CFG, {"gave_up": True})
    assert isinstance(a, Notify)


def test_session_attempts_exhausted_gives_up():
    a = plan_recovery("failing_ci", "auto", _CFG, {"attempts": 3})
    assert isinstance(a, GiveUp)


def test_mechanical_attempts_exhausted_gives_up():
    a = plan_recovery("out_of_date", "auto", _CFG, {"mechanical_attempts": 2})
    assert isinstance(a, GiveUp)
```

- [ ] **Step 2: Run to verify it fails**

Run the file; expected FAIL on the new imports (`Notify`/`plan_recovery` not defined).

- [ ] **Step 3: Implement actions + plan_recovery (append to the module)**

```python
@dataclass(frozen=True)
class Notify:
    pass


@dataclass(frozen=True)
class Propose:
    kind: str  # the failure mode, surfaced as a one-tap action


@dataclass(frozen=True)
class Mechanical:
    kind: str  # "out_of_date"


@dataclass(frozen=True)
class SessionFix:
    kind: str  # "failing_ci" | "merge_conflict"


@dataclass(frozen=True)
class GiveUp:
    pass


RecoveryAction = Notify | Propose | Mechanical | SessionFix | GiveUp

# Which failure modes are mechanical (no session) vs session-driven, and the
# config flag that gates each in auto mode.
_MECHANICAL = {"out_of_date"}
_AUTO_FLAG = {
    "out_of_date": "auto_update_out_of_date",
    "failing_ci": "auto_fix_failing_ci",
    "merge_conflict": "auto_resolve_conflicts",
}


def plan_recovery(failure_mode: str | None, mode: str, config: dict,
                  state: dict) -> RecoveryAction:
    """Map a failure mode + automation mode + config + per-target state to an
    action. Pure + total. (Repo-scope gating happens in the orchestrator before
    this is called — see Plan B — so `enabled_repos` is not consulted here.)"""
    if failure_mode is None:
        return Notify()
    if failure_mode == "blocked":
        return Notify()  # review required — never auto-act
    if mode == "off":
        return Notify()
    if state.get("gave_up"):
        return Notify()
    if mode == "propose":
        return Propose(kind=failure_mode)
    # mode == "auto": gate risky modes; fall back to propose when the flag is off.
    if not config.get(_AUTO_FLAG[failure_mode], False):
        return Propose(kind=failure_mode)
    mechanical = failure_mode in _MECHANICAL
    cap_key = "mechanical_attempts" if mechanical else "attempts"
    cap = config["mechanical_attempts"] if mechanical else config["max_attempts"]
    if state.get(cap_key, 0) >= cap:
        return GiveUp()
    return Mechanical(kind=failure_mode) if mechanical else SessionFix(kind=failure_mode)
```

- [ ] **Step 4: Run to verify it passes**

Run the file; expected PASS (all classify + plan_recovery tests).

- [ ] **Step 5: Commit**

```bash
git add server/automations/pr_doctor_engine.py server/tests/test_pr_doctor_engine.py
git commit -m "feat(pr-doctor): recovery actions + plan_recovery risk-gating

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: Per-target state machine — re-arm, attempt accounting, terminate (pure)

**Files:**
- Modify: `server/automations/pr_doctor_engine.py` (add state helpers)
- Test: `server/tests/test_pr_doctor_engine.py` (extend)

**Interfaces:**
- Produces: `apply_rearm(state, info) -> dict` (reset attempts/gave_up on EXTERNAL push or changed failure class; record `last_head_sha`/`last_failure`); `record_attempt(state, action, head_sha) -> dict` (increment the right counter + set `last_action_sha`); `target_terminal(info) -> bool`.

- [ ] **Step 1: Write the failing test (append)**

```python
# append to server/tests/test_pr_doctor_engine.py
from server.automations.pr_doctor_engine import (
    apply_rearm, record_attempt, target_terminal,
)


def test_rearm_on_external_push_resets():
    state = {"attempts": 2, "gave_up": True, "last_head_sha": "old", "last_action_sha": "act"}
    out = apply_rearm(state, _pr(head_sha="brand-new", checks="failing", merge_state_status="CLEAN"))
    assert out["attempts"] == 0 and out["gave_up"] is False
    assert out["last_head_sha"] == "brand-new"


def test_no_rearm_when_head_is_our_own_action_sha():
    # PR Doctor's own fix pushed "act" → must NOT reset (else gave_up never reached)
    state = {"attempts": 2, "last_head_sha": "old", "last_action_sha": "act", "last_failure": "failing_ci"}
    out = apply_rearm(state, _pr(head_sha="act", checks="failing", merge_state_status="CLEAN"))
    assert out["attempts"] == 2 and not out.get("gave_up")


def test_rearm_on_changed_failure_class():
    state = {"attempts": 2, "last_head_sha": "same", "last_action_sha": "", "last_failure": "failing_ci"}
    out = apply_rearm(state, _pr(head_sha="same", merge_state_status="BEHIND"))
    assert out["attempts"] == 0  # failing_ci → out_of_date is a new problem


def test_record_attempt_increments_right_counter_and_records_sha():
    from server.automations.pr_doctor_engine import SessionFix, Mechanical
    s = record_attempt({}, SessionFix("failing_ci"), "sha1")
    assert s["attempts"] == 1 and s["last_action_sha"] == "sha1"
    m = record_attempt({}, Mechanical("out_of_date"), "sha2")
    assert m["mechanical_attempts"] == 1 and m["last_action_sha"] == "sha2"


def test_target_terminal():
    assert target_terminal(_pr(state="merged")) is True
    assert target_terminal(_pr(state="closed")) is True
    assert target_terminal(_pr(state="open")) is False
```

- [ ] **Step 2: Run to verify it fails**

Run the file; expected FAIL (`apply_rearm` not defined).

- [ ] **Step 3: Implement the state helpers (append)**

```python
def apply_rearm(state: dict, info: PullRequestInfo) -> dict:
    """Reset attempt counters when the PR changed in a way that deserves fresh
    tries: an EXTERNAL push (head_sha not one PR Doctor itself produced) or a
    different failure class. Records the current head + failure class. Pure —
    returns a new dict."""
    out = dict(state)
    fm = classify(info)
    known_shas = {state.get("last_head_sha"), state.get("last_action_sha")}
    external_push = info.head_sha and info.head_sha not in known_shas
    changed_class = fm is not None and fm != state.get("last_failure")
    if external_push or changed_class:
        out["attempts"] = 0
        out["mechanical_attempts"] = 0
        out["gave_up"] = False
    out["last_head_sha"] = info.head_sha
    if fm is not None:
        out["last_failure"] = fm
    return out


def record_attempt(state: dict, action, head_sha: str) -> dict:
    """Consume one attempt of the right kind and record the resulting head as
    last_action_sha (so the next poll's apply_rearm won't treat our own push as
    external). Call ONLY after a real injection / gh success."""
    out = dict(state)
    if isinstance(action, Mechanical):
        out["mechanical_attempts"] = out.get("mechanical_attempts", 0) + 1
    else:  # SessionFix
        out["attempts"] = out.get("attempts", 0) + 1
    out["last_action_sha"] = head_sha
    return out


def target_terminal(info: PullRequestInfo) -> bool:
    return info.state in ("merged", "closed")
```

- [ ] **Step 4: Run to verify it passes**

Run the file; expected PASS (full engine suite).

- [ ] **Step 5: Commit**

```bash
git add server/automations/pr_doctor_engine.py server/tests/test_pr_doctor_engine.py
git commit -m "feat(pr-doctor): per-target re-arm/attempt/terminate state machine (self-push guard)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 4: PrDoctor config schema (v1)

**Files:**
- Modify: `server/automations/pr_doctor.py` (replace placeholder `config_schema`)
- Test: `server/tests/test_pr_doctor_automation.py` (extend — the file exists from the shell)

**Interfaces:**
- Produces: `PrDoctor.config_schema` = the six v1 fields (§5).

- [ ] **Step 1: Write the failing test (append to the existing file)**

```python
# append to server/tests/test_pr_doctor_automation.py
def test_config_schema_v1_fields():
    from server.automations.pr_doctor import PrDoctor
    keys = {f.key: f for f in PrDoctor().config_schema}
    assert keys["enabled_repos"].type == "repo-multiselect"
    assert keys["max_attempts"].type == "int" and keys["max_attempts"].default == 3
    assert keys["mechanical_attempts"].type == "int" and keys["mechanical_attempts"].default == 2
    assert keys["auto_update_out_of_date"].type == "bool" and keys["auto_update_out_of_date"].default is True
    assert keys["auto_fix_failing_ci"].default is True
    assert keys["auto_resolve_conflicts"].default is False
    # stuck_checks deferred to v2 — no such field
    assert "auto_rerun_stuck_checks" not in keys
```

- [ ] **Step 2: Run to verify it fails**

Run: `… -m pytest server/tests/test_pr_doctor_automation.py -q` → FAIL (schema has only `enabled_repos`).

- [ ] **Step 3: Replace the config_schema in `pr_doctor.py`**

```python
    config_schema = [
        ConfigField(key="enabled_repos", label="Repositories",
                    type="repo-multiselect", default=[],
                    help="Limit to these repos (empty = all ccswitch-managed repos)."),
        ConfigField(key="max_attempts", label="Max fix attempts", type="int", default=3,
                    help="Session-fix attempts per PR before giving up (0 = notify only)."),
        ConfigField(key="mechanical_attempts", label="Max branch-update attempts",
                    type="int", default=2, help="Mechanical (update-branch) attempts per PR."),
        ConfigField(key="auto_update_out_of_date", label="Auto-update behind branches",
                    type="bool", default=True, help="Mechanical, safe."),
        ConfigField(key="auto_fix_failing_ci", label="Auto-fix failing CI",
                    type="bool", default=True),
        ConfigField(key="auto_resolve_conflicts", label="Auto-resolve merge conflicts",
                    type="bool", default=False, help="Risky — spawns a session to rebase/resolve."),
    ]
```

- [ ] **Step 4: Run to verify it passes**

Run: `… -m pytest server/tests/test_pr_doctor_automation.py -q` → PASS (incl. the existing modes/status tests).

- [ ] **Step 5: Commit**

```bash
git add server/automations/pr_doctor.py server/tests/test_pr_doctor_automation.py
git commit -m "feat(pr-doctor): v1 config schema (max_attempts, risk-tier flags)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Self-Review

- **Spec coverage:** §3 classify (priority, DIRTY=conflict, UNSTABLE skip, draft/blocked/terminal) → Task 1; §5 gating + config → Tasks 2 & 4; §6 re-arm (self-push guard), attempt accounting, terminate → Task 3. Stuck-checks correctly absent (v2). ✓
- **Placeholders:** none — every function is complete code with a test.
- **Type consistency:** action dataclasses (`Notify`/`Propose`/`Mechanical`/`SessionFix`/`GiveUp`), `classify` string returns, and `plan_recovery`/`apply_rearm`/`record_attempt` signatures are consistent across module + tests. `RecoveryAction` union used in Plan B.

## Hand-off to Plan B

Plan B (orchestrator execution) consumes this module: per poll it calls `apply_rearm` → `classify` → `plan_recovery`, executes the returned action (mechanical `gh` / session inject-or-spawn / propose / notify / give-up), then `record_attempt` on a real action and writes the state back via the `pr-doctor` `AutomationContext`. Repo-scope (`enabled_repos`) gating is applied in Plan B before `plan_recovery`.

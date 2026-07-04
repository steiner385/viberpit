# Automation Actions Surface Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the three divergent per-automation "detected item → manual trigger" surfaces (pr-doctor, backlog-runner, cicd-doctor) with one reusable contract, and make every trigger's outcome visible to the user.

**Architecture:** A uniform backend value contract (`ActionSpec`/`ActionableItem`/`TriggerResult`) in `server/automations/base.py`; per-automation `ActionProvider` classes (`server/automations/action_providers.py`) that wrap existing orchestrator machinery and emit the DTOs; two generic API routes (`GET …/{id}/actionable`, `POST …/{id}/trigger`) dispatching through an orchestrator provider registry; and one reusable React `<AutomationActions>` component with per-button pending state, sonner toast + inline row outcome, and a confirm step for risky actions.

**Tech Stack:** Python 3 · FastAPI · SQLAlchemy · pytest · React + TypeScript · TanStack Query · sonner (toasts) · vitest + Testing Library.

## Global Constraints

- **Purge replaced code in the same PR** — no half-dead code. Old orchestrator methods, API routes, request models, frontend fetchers/hooks/component are deleted once the new path is live (Task 6). Copied verbatim from the project norm.
- **Reuse the existing toaster** — the app uses `sonner` (`import { toast } from 'sonner'`, mounted in `web/src/main.tsx`). Do NOT add a parallel notification system.
- **Find the existing source of truth before adding** — providers reuse existing orchestrator helpers (`_pr_doctor_session_fix`, `_pr_doctor_mechanical`, `_pr_doctor_owning_rec`, `_pr_doctor_scope_pairs`, `pr_for`, `_pr_population_pr`, `classify`, `query_candidates`, `_backlog_claim`, `_backlog_spawn`, `_backlog_healthy_account`). Do not reimplement them.
- **Add new model columns nullable only** — N/A here (no schema changes).
- **Server test invocation:** `PYTHONPATH=.:src CCSWITCH_DEV=1 python3 -m pytest <path> -v` from repo root. API auth tests set `CCSWITCH_TAILNET_ALLOWLIST=""` + `CCSWITCH_DEV=1` inside the test (see `_client` helper).
- **Web test invocation (run in `web/`):** `npx vitest run <path>`, `npx tsc --noEmit`, `npx eslint <path>`. `npm ci` first if `node_modules` is absent.
- **CI gates that must pass:** ruff + mypy + pytest (`lint-and-test`) and the `frontend` job (vitest + tsc + eslint). mypy is easily missed — keep type annotations correct.

## File Structure

| File | Responsibility |
|------|----------------|
| `server/automations/base.py` | + `ActionSpec`, `ActionableItem`, `TriggerResult` value types (+ `to_dict`); + `ActionProvider` Protocol |
| `server/automations/action_providers.py` (new) | `PrDoctorActionProvider`, `BacklogRunnerActionProvider` — wrap orchestrator machinery, emit DTOs, own the status→level/message mapping |
| `server/sessions/orchestrator.py` | + `"ceiling"` return in `_pr_doctor_session_fix`; + `_action_providers` registry, `automation_actionable`, `automation_trigger`; − old `pr_doctor_actionable`, `pr_doctor_fix`, `backlog_runner_implement` |
| `server/api/automations.py` | + `GET /{id}/actionable`, `POST /{id}/trigger`; − 3 one-off routes + `PrDoctorFixBody` + `BacklogImplementBody` |
| `web/src/api/automations.ts` | + `ActionSpec`/`ActionableItem`/`TriggerResult` types + `fetchAutomationActionable`/`postAutomationTrigger`; − pr-doctor-specific |
| `web/src/api/hooks.ts` | + `QK.automationActionable(id)`, `useAutomationActionable`, `useAutomationTrigger`; − `usePrDoctorActionable`/`usePrDoctorFix` |
| `web/src/components/AutomationCard.tsx` | `ActionablePrsSection` → `<AutomationActions>`; drop `a.id === 'pr-doctor'` gate |
| `server/tests/test_automation_actions_api.py` (new) | generic route tests |
| `server/tests/test_pr_doctor_actions_api.py` | rewrite onto generic path |
| `server/tests/test_action_providers.py` (new) | provider unit tests |
| `web/src/components/AutomationCard.test.tsx` | rewrite actionable block for `<AutomationActions>` |

---

### Task 1: Value types + provider protocol

**Files:**
- Modify: `server/automations/base.py`
- Test: `server/tests/test_automation_base_actions.py` (new)

**Interfaces:**
- Produces: `ActionSpec(kind: str, label: str, style: str = "default")`; `ActionableItem(id: str, title: str, subtitle: str | None = None, url: str | None = None, actions: list[ActionSpec] = [])` with `.to_dict()`; `TriggerResult(status: str, level: str, message: str)` with `.to_dict()`; `ActionProvider` Protocol with `valid_kinds: frozenset[str]`, `async def actionable() -> list[ActionableItem]`, `async def trigger(item_id, kind) -> TriggerResult`.

- [ ] **Step 1: Write the failing test**

Create `server/tests/test_automation_base_actions.py`:

```python
from server.automations.base import ActionSpec, ActionableItem, TriggerResult


def test_action_spec_defaults_to_non_risky():
    s = ActionSpec(kind="failing_ci", label="Fix CI")
    assert s.style == "default"


def test_actionable_item_to_dict_nests_actions():
    item = ActionableItem(
        id="owner/repo#7", title="red CI", url="https://x/7",
        actions=[ActionSpec(kind="failing_ci", label="Fix CI", style="default")])
    assert item.to_dict() == {
        "id": "owner/repo#7", "title": "red CI", "subtitle": None, "url": "https://x/7",
        "actions": [{"kind": "failing_ci", "label": "Fix CI", "style": "default"}],
    }


def test_trigger_result_to_dict():
    r = TriggerResult(status="spawned", level="ok", message="Spawned fix session")
    assert r.to_dict() == {"status": "spawned", "level": "ok", "message": "Spawned fix session"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=.:src CCSWITCH_DEV=1 python3 -m pytest server/tests/test_automation_base_actions.py -v`
Expected: FAIL — `ImportError: cannot import name 'ActionSpec'`.

- [ ] **Step 3: Add the value types + protocol**

In `server/automations/base.py`, change the dataclass import line `from dataclasses import dataclass` to:

```python
from dataclasses import dataclass, field
```

Then append (after `AutomationStatus`, before the `Automation` Protocol):

```python
@dataclass
class ActionSpec:
    """One trigger button on an actionable item."""
    kind: str
    label: str
    style: str = "default"  # "default" | "risky" (risky → confirm + distinct color)

    def to_dict(self) -> dict:
        return {"kind": self.kind, "label": self.label, "style": self.style}


@dataclass
class ActionableItem:
    """One row in an automation's actionable list."""
    id: str
    title: str
    subtitle: str | None = None
    url: str | None = None
    actions: list[ActionSpec] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"id": self.id, "title": self.title, "subtitle": self.subtitle,
                "url": self.url, "actions": [a.to_dict() for a in self.actions]}


@dataclass
class TriggerResult:
    """The outcome of a manual trigger — surfaced to the user as toast + inline."""
    status: str          # machine-readable (e.g. "spawned", "ceiling", "failed")
    level: str           # "ok" | "warn" | "error"
    message: str         # human-readable

    def to_dict(self) -> dict:
        return {"status": self.status, "level": self.level, "message": self.message}
```

Then append after the `Automation` Protocol:

```python
@runtime_checkable
class ActionProvider(Protocol):
    """An automation's manual-action surface: list items, trigger one action."""
    valid_kinds: frozenset[str]

    async def actionable(self) -> list[ActionableItem]: ...
    async def trigger(self, item_id: str, kind: str) -> TriggerResult: ...
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=.:src CCSWITCH_DEV=1 python3 -m pytest server/tests/test_automation_base_actions.py -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add server/automations/base.py server/tests/test_automation_base_actions.py
git commit -m "feat(automations): ActionSpec/ActionableItem/TriggerResult + ActionProvider protocol"
```

---

### Task 2: Distinguish usage-ceiling refusal in the session executor

**Files:**
- Modify: `server/sessions/orchestrator.py` (`_pr_doctor_session_fix`, ~1959-1964)
- Test: `server/tests/test_pr_doctor_ceiling_status.py` (new)

**Interfaces:**
- Consumes: `AllAccountsBusy` (already imported at `orchestrator.py:60`).
- Produces: `_pr_doctor_session_fix(...)` now returns `("ceiling", None)` when the fresh spawn is refused because every account is at/above the usage ceiling; other spawn errors still return `("spawn_failed", None)`.

- [ ] **Step 1: Write the failing test**

Create `server/tests/test_pr_doctor_ceiling_status.py`:

```python
import os
from unittest.mock import patch

import pytest

from server.sessions.orchestrator import SessionOrchestrator
from server.sessions.selection import AllAccountsBusy
from server.github.pr_status import PullRequestInfo
from server.tests.fakes.fake_factory import fake_sdk_factory


@pytest.mark.asyncio
async def test_session_fix_returns_ceiling_when_all_accounts_busy():
    with patch.dict(os.environ, {"CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:",
                                 "CCSWITCH_DEV": "1"}):
        orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())
    info = PullRequestInfo(number=11143, url="https://x/11143", state="open",
                           checks="failing", title="red", repo="cairnea/KinDash",
                           head_ref="fix-lint")

    async def _boom(*a, **k):
        raise AllAccountsBusy("all accounts at/above usage ceiling")

    # No live driver → fresh-spawn path; the clone succeeds, the spawn refuses.
    with patch("server.sessions.orchestrator.gitrepos.ensure_clone", return_value="/tmp/x"), \
         patch.object(orch._pr_doctor_ctx, "spawn_session", _boom):
        status, sid = await orch._pr_doctor_session_fix(
            "cairnea/KinDash", "fix-lint", info, "failing_ci", None)
    assert status == "ceiling"
    assert sid is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=.:src CCSWITCH_DEV=1 python3 -m pytest server/tests/test_pr_doctor_ceiling_status.py -v`
Expected: FAIL — `assert 'spawn_failed' == 'ceiling'` (the broad `except Exception` catches `AllAccountsBusy`).

- [ ] **Step 3: Narrow the exception handling**

In `server/sessions/orchestrator.py`, in `_pr_doctor_session_fix`, replace the spawn `try/except` block (currently `except Exception as exc:` returning `"spawn_failed", None`) so the ceiling case is caught first:

```python
        try:
            rec2 = await self._pr_doctor_ctx.spawn_session(
                origin_ref=target,
                dedupe_key=target,
                repo=repo,
                branch=branch,
                cwd=cwd,
                account=None,  # create_session picks a healthy account
                name=f"pr-doctor fix {target}",
                initial_prompt=prompt,
            )
        except AllAccountsBusy:
            # Every account is at/above the usage ceiling → defer, not fail. Attempt
            # NOT consumed; surfaced to the user as a warn, not a silent no-op.
            await self._pr_doctor_ctx.emit(
                "warn", f"spawn deferred for {target}: all accounts at/above usage ceiling",
                kind="spawn_failed", target=target)
            return "ceiling", None
        except Exception as exc:
            # Spawn failed → error + notify, attempt NOT consumed.
            await self._pr_doctor_ctx.emit(
                "error", f"spawn failed for {target}: {exc}",
                kind="spawn_failed", target=target)
            return "spawn_failed", None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=.:src CCSWITCH_DEV=1 python3 -m pytest server/tests/test_pr_doctor_ceiling_status.py -v`
Expected: PASS.

- [ ] **Step 5: Guard against regressions in the existing session-fix tests**

Run: `PYTHONPATH=.:src CCSWITCH_DEV=1 python3 -m pytest server/tests/ -k pr_doctor -v`
Expected: PASS (no existing test asserted `spawn_failed` for the `AllAccountsBusy` path).

- [ ] **Step 6: Commit**

```bash
git add server/sessions/orchestrator.py server/tests/test_pr_doctor_ceiling_status.py
git commit -m "feat(pr-doctor): distinct 'ceiling' status when spawn is refused for usage ceiling"
```

---

### Task 3: Action providers (pr-doctor + backlog-runner)

**Files:**
- Create: `server/automations/action_providers.py`
- Test: `server/tests/test_action_providers.py` (new)

**Interfaces:**
- Consumes: `ActionableItem`, `ActionSpec`, `TriggerResult` (Task 1); an orchestrator exposing `_pr_doctor_scope_pairs()`, `pr_for(repo, branch)`, `_pr_population_pr(repo, branch)`, `_pr_doctor_owning_rec(repo, branch)`, `_pr_doctor_mechanical(repo, info, kind)`, `_pr_doctor_session_fix(repo, branch, info, kind, rec)`, and backlog helpers `_backlog_read_config()`, `_backlog_healthy_account()`, `_backlog_claim(batch)`, `_backlog_spawn(batch, account)`, `_backlog_ctx`, `_gh_run`.
- Produces: `PrDoctorActionProvider(orch)` and `BacklogRunnerActionProvider(orch)` implementing `ActionProvider`. `PrDoctorActionProvider.valid_kinds = frozenset({"failing_ci", "merge_conflict", "out_of_date"})`; `BacklogRunnerActionProvider.valid_kinds = frozenset({"implement"})`.

- [ ] **Step 1: Write the failing test**

Create `server/tests/test_action_providers.py`:

```python
import pytest

from server.automations.action_providers import (
    PrDoctorActionProvider, BacklogRunnerActionProvider,
)
from server.github.pr_status import PullRequestInfo


class _FakePrOrch:
    def __init__(self, pr):
        self._pr = pr
        self.fix_calls = []

    async def _pr_doctor_scope_pairs(self):
        return [(self._pr.repo, self._pr.head_ref)]

    def pr_for(self, repo, branch):
        return self._pr if (repo, branch) == (self._pr.repo, self._pr.head_ref) else None

    def _pr_population_pr(self, repo, branch):
        return None

    def _pr_doctor_owning_rec(self, repo, branch):
        return None

    async def _pr_doctor_session_fix(self, repo, branch, info, kind, rec):
        self.fix_calls.append((repo, branch, kind))
        return "ceiling", None


def _pr(**kw):
    base = dict(number=11006, url="https://x/11006", state="open", checks="failing",
                title="sweep limiters", repo="cairnea/KinDash", head_ref="rl",
                merge_state_status="", is_draft=False, mergeable="UNKNOWN")
    base.update(kw)
    return PullRequestInfo(**base)


@pytest.mark.asyncio
async def test_pr_provider_actionable_maps_failing_ci_to_fix_ci_button():
    prov = PrDoctorActionProvider(_FakePrOrch(_pr()))
    items = await prov.actionable()
    assert len(items) == 1
    it = items[0]
    assert it.id == "cairnea/KinDash#11006"
    assert it.title == "sweep limiters"
    assert [(a.kind, a.label, a.style) for a in it.actions] == [
        ("failing_ci", "Fix CI", "default")]


@pytest.mark.asyncio
async def test_pr_provider_merge_conflict_is_risky():
    prov = PrDoctorActionProvider(_FakePrOrch(_pr(merge_state_status="DIRTY")))
    items = await prov.actionable()
    assert items[0].actions[0].kind == "merge_conflict"
    assert items[0].actions[0].style == "risky"


@pytest.mark.asyncio
async def test_pr_provider_trigger_maps_ceiling_to_warn():
    orch = _FakePrOrch(_pr())
    prov = PrDoctorActionProvider(orch)
    res = await prov.trigger("cairnea/KinDash#11006", "failing_ci")
    assert (res.status, res.level) == ("ceiling", "warn")
    assert "usage ceiling" in res.message
    assert orch.fix_calls == [("cairnea/KinDash", "rl", "failing_ci")]


@pytest.mark.asyncio
async def test_pr_provider_trigger_unknown_item_is_no_pr():
    prov = PrDoctorActionProvider(_FakePrOrch(_pr()))
    res = await prov.trigger("cairnea/KinDash#999", "failing_ci")
    assert (res.status, res.level) == ("no_pr", "warn")


class _FakeBacklogOrch:
    def __init__(self, status="spawned"):
        self._status = status
        self.implement_calls = []

    async def backlog_runner_implement_core(self, primary):
        self.implement_calls.append(primary)
        return self._status

    async def backlog_candidates(self):
        return [{"ref": "cairnea/KinDash#42", "repo": "cairnea/KinDash",
                 "title": "add rate limiter"}]


@pytest.mark.asyncio
async def test_backlog_provider_actionable_and_trigger():
    orch = _FakeBacklogOrch(status="no_account")
    prov = BacklogRunnerActionProvider(orch)
    items = await prov.actionable()
    assert items[0].id == "cairnea/KinDash#42"
    assert items[0].actions[0].kind == "implement"
    res = await prov.trigger("cairnea/KinDash#42", "implement")
    assert (res.status, res.level) == ("no_account", "warn")
    assert "usage ceiling" in res.message
    assert orch.implement_calls == ["cairnea/KinDash#42"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=.:src CCSWITCH_DEV=1 python3 -m pytest server/tests/test_action_providers.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'server.automations.action_providers'`.

- [ ] **Step 3: Implement the providers**

Create `server/automations/action_providers.py`:

```python
"""ActionProvider implementations — the per-automation manual-action surface.

Each provider wraps the orchestrator's existing recovery machinery and returns
the uniform ``ActionableItem`` / ``TriggerResult`` DTOs. No new recovery logic
lives here — providers only enumerate items and map status strings to
user-facing level/message pairs.
"""
from __future__ import annotations

from server.automations import pr_doctor_engine as engine
from server.automations.base import ActionableItem, ActionSpec, TriggerResult

# --- PR Doctor -------------------------------------------------------------

_PR_KIND_LABEL = {
    "failing_ci": "Fix CI",
    "merge_conflict": "Resolve conflicts",
    "out_of_date": "Update branch",
}
_PR_RISKY = {"merge_conflict"}

# status → (level, message). Statuses come from pr_doctor_fix's shared executors:
#   session executor: sent | spawned | ceiling | transient | clone_failed | spawn_failed
#   mechanical:       updated | failed
#   gates:            no_pr | not_actionable
_PR_STATUS_MAP = {
    "spawned": ("ok", "Spawned fix session"),
    "sent": ("ok", "Sent fix prompt to live session"),
    "updated": ("ok", "Branch updated — CI will re-run"),
    "ceiling": ("warn", "All accounts at/above usage ceiling — try later"),
    "transient": ("warn", "Session busy — try again shortly"),
    "not_actionable": ("warn", "PR is no longer actionable"),
    "no_pr": ("warn", "PR not found in the poll cache"),
    "clone_failed": ("error", "Clone failed — see the automation feed"),
    "spawn_failed": ("error", "Spawn failed — see the automation feed"),
    "failed": ("error", "Branch update failed — see the automation feed"),
}


class PrDoctorActionProvider:
    valid_kinds = frozenset(_PR_KIND_LABEL)

    def __init__(self, orch) -> None:
        self._orch = orch

    async def actionable(self) -> list[ActionableItem]:
        by_number: dict[tuple[str, int], ActionableItem] = {}
        for repo, branch in await self._orch._pr_doctor_scope_pairs():
            info = self._orch.pr_for(repo, branch) or self._orch._pr_population_pr(repo, branch)
            if info is None:
                continue
            kind = engine.classify(info)
            if kind not in _PR_KIND_LABEL:
                continue
            key = (repo, info.number)
            if key in by_number:
                continue
            by_number[key] = ActionableItem(
                id=f"{repo}#{info.number}", title=info.title, url=info.url,
                actions=[ActionSpec(
                    kind=kind, label=_PR_KIND_LABEL[kind],
                    style="risky" if kind in _PR_RISKY else "default")])
        return [by_number[k] for k in sorted(by_number)]

    async def trigger(self, item_id: str, kind: str) -> TriggerResult:
        repo, _, num = item_id.rpartition("#")
        try:
            number = int(num)
        except ValueError:
            return _pr_result("no_pr")
        info = branch = None
        for r, br in await self._orch._pr_doctor_scope_pairs():
            if r != repo:
                continue
            cached = self._orch.pr_for(repo, br) or self._orch._pr_population_pr(repo, br)
            if cached is not None and cached.number == number:
                info, branch = cached, br
                break
        if info is None or branch is None:
            return _pr_result("no_pr")
        if info.state not in ("open", "draft"):
            return _pr_result("not_actionable")
        if kind == "out_of_date":
            ok = await self._orch._pr_doctor_mechanical(repo, info, kind)
            return _pr_result("updated" if ok else "failed")
        rec = self._orch._pr_doctor_owning_rec(repo, branch)
        status, _sid = await self._orch._pr_doctor_session_fix(repo, branch, info, kind, rec)
        return _pr_result(status)


def _pr_result(status: str) -> TriggerResult:
    level, message = _PR_STATUS_MAP.get(status, ("error", "Action failed — see the feed"))
    return TriggerResult(status=status, level=level, message=message)


# --- Backlog Runner --------------------------------------------------------

_BACKLOG_STATUS_MAP = {
    "spawned": ("ok", "Spawned implementation session"),
    "exists": ("warn", "Already running"),
    "claim_failed": ("error", "Could not claim the issue — see the feed"),
    "no_account": ("warn", "All accounts at/above usage ceiling — try later"),
    "unknown": ("warn", "No longer a current candidate"),
}


class BacklogRunnerActionProvider:
    valid_kinds = frozenset({"implement"})

    def __init__(self, orch) -> None:
        self._orch = orch

    async def actionable(self) -> list[ActionableItem]:
        items: list[ActionableItem] = []
        for c in await self._orch.backlog_candidates():
            items.append(ActionableItem(
                id=c["ref"], title=c.get("title") or c["ref"], subtitle=c.get("repo"),
                url=c.get("url"),
                actions=[ActionSpec(kind="implement", label="Implement", style="default")]))
        return items

    async def trigger(self, item_id: str, kind: str) -> TriggerResult:
        status = await self._orch.backlog_runner_implement_core(item_id)
        level, message = _BACKLOG_STATUS_MAP.get(status, ("error", "Action failed — see the feed"))
        return TriggerResult(status=status, level=level, message=message)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=.:src CCSWITCH_DEV=1 python3 -m pytest server/tests/test_action_providers.py -v`
Expected: PASS (6 passed).

- [ ] **Step 5: Type-check**

Run: `PYTHONPATH=.:src CCSWITCH_DEV=1 python3 -m mypy server/automations/action_providers.py`
Expected: no errors (or only pre-existing project-wide baseline). Fix any new error this file introduces.

- [ ] **Step 6: Commit**

```bash
git add server/automations/action_providers.py server/tests/test_action_providers.py
git commit -m "feat(automations): PrDoctor + BacklogRunner action providers with status→message mapping"
```

---

### Task 4: Orchestrator dispatch + backlog helper split

**Files:**
- Modify: `server/sessions/orchestrator.py`
- Test: `server/tests/test_orchestrator_action_dispatch.py` (new)

**Interfaces:**
- Consumes: `PrDoctorActionProvider`, `BacklogRunnerActionProvider` (Task 3).
- Produces: `orch.automation_actionable(id) -> list[dict]`; `orch.automation_trigger(id, item_id, kind) -> dict`; `orch.automation_action_kinds(id) -> frozenset[str] | None`; `orch.backlog_candidates() -> list[dict]`; `orch.backlog_runner_implement_core(primary) -> str` (the body of the old `backlog_runner_implement`). Missing/unknown provider → methods return `None` / `[]` so the API can 404.

- [ ] **Step 1: Write the failing test**

Create `server/tests/test_orchestrator_action_dispatch.py`:

```python
import os
from unittest.mock import patch

import pytest

from server.sessions.orchestrator import SessionOrchestrator
from server.tests.fakes.fake_factory import fake_sdk_factory


def _orch():
    with patch.dict(os.environ, {"CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:",
                                 "CCSWITCH_DEV": "1"}):
        return SessionOrchestrator(sdk_factory=fake_sdk_factory())


@pytest.mark.asyncio
async def test_dispatch_registers_pr_doctor_and_backlog():
    orch = _orch()
    assert orch.automation_action_kinds("pr-doctor") == frozenset(
        {"failing_ci", "merge_conflict", "out_of_date"})
    assert orch.automation_action_kinds("backlog-runner") == frozenset({"implement"})
    assert orch.automation_action_kinds("cicd-doctor") is None


@pytest.mark.asyncio
async def test_actionable_unknown_provider_returns_empty():
    orch = _orch()
    assert await orch.automation_actionable("cicd-doctor") == []


@pytest.mark.asyncio
async def test_actionable_pr_doctor_empty_cache_is_empty_list():
    orch = _orch()
    # No PRs in cache → empty, but dispatch path must run without error.
    assert await orch.automation_actionable("pr-doctor") == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=.:src CCSWITCH_DEV=1 python3 -m pytest server/tests/test_orchestrator_action_dispatch.py -v`
Expected: FAIL — `AttributeError: 'SessionOrchestrator' object has no attribute 'automation_action_kinds'`.

- [ ] **Step 3: Rename the backlog core + add candidate lister**

In `server/sessions/orchestrator.py`, rename `async def backlog_runner_implement(self, primary: str) -> str:` to `async def backlog_runner_implement_core(self, primary: str) -> str:` (body unchanged). Then add a candidate lister next to it:

```python
    async def backlog_candidates(self) -> list[dict]:
        """Current label-gated backlog candidate dicts ({ref, repo, title, url})."""
        from server.automations.backlog_github import query_candidates
        _mode, config = await self._backlog_read_config()
        return await asyncio.to_thread(query_candidates, config, gh_run=self._gh_run)
```

- [ ] **Step 4: Register providers + add dispatch methods**

In `SessionOrchestrator.__init__` (after the pr/backlog machinery is set up — co-locate with `self._pr_population` around line 285), add:

```python
        from server.automations.action_providers import (
            PrDoctorActionProvider, BacklogRunnerActionProvider,
        )
        self._action_providers = {
            "pr-doctor": PrDoctorActionProvider(self),
            "backlog-runner": BacklogRunnerActionProvider(self),
        }
```

Then add the three dispatch methods (place them next to the old `pr_doctor_actionable`, which Task 6 deletes):

```python
    def automation_action_kinds(self, automation_id: str):
        """Valid action kinds for *automation_id*, or None if it has no provider."""
        provider = self._action_providers.get(automation_id)
        return provider.valid_kinds if provider is not None else None

    async def automation_actionable(self, automation_id: str) -> list[dict]:
        provider = self._action_providers.get(automation_id)
        if provider is None:
            return []
        return [it.to_dict() for it in await provider.actionable()]

    async def automation_trigger(self, automation_id: str, item_id: str, kind: str) -> dict:
        provider = self._action_providers.get(automation_id)
        if provider is None:
            raise KeyError(automation_id)
        return (await provider.trigger(item_id, kind)).to_dict()
```

- [ ] **Step 5: Run test to verify it passes**

Run: `PYTHONPATH=.:src CCSWITCH_DEV=1 python3 -m pytest server/tests/test_orchestrator_action_dispatch.py -v`
Expected: PASS (3 passed).

- [ ] **Step 6: Keep the old backlog API route working (temporary shim)**

The old route `POST /backlog-runner/implement` still calls `orch.backlog_runner_implement`. Until Task 6 deletes that route, add a thin alias so the app + its existing test stay green:

```python
    async def backlog_runner_implement(self, primary: str) -> str:
        return await self.backlog_runner_implement_core(primary)
```

Run: `PYTHONPATH=.:src CCSWITCH_DEV=1 python3 -m pytest server/tests/ -k backlog -v`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add server/sessions/orchestrator.py server/tests/test_orchestrator_action_dispatch.py
git commit -m "feat(orchestrator): generic action dispatch + backlog candidate/core split"
```

---

### Task 5: Generic API routes

**Files:**
- Modify: `server/api/automations.py`
- Test: `server/tests/test_automation_actions_api.py` (new)

**Interfaces:**
- Consumes: `orch.automation_actionable`, `orch.automation_trigger`, `orch.automation_action_kinds` (Task 4).
- Produces: `GET /api/automations/{id}/actionable` → `list[dict]`; `POST /api/automations/{id}/trigger` (body `{item_id, kind}`) → `dict` (a `TriggerResult`). Unknown automation → 404; automation without a provider → 404; unknown kind → 422.

- [ ] **Step 1: Write the failing test**

Create `server/tests/test_automation_actions_api.py`:

```python
import os
from contextlib import contextmanager
from unittest.mock import patch


class _FakeOrch:
    def __init__(self, *, actionable=None, result=None, kinds=frozenset({"failing_ci"})):
        self._actionable = actionable or []
        self._result = result or {"status": "spawned", "level": "ok", "message": "Spawned fix session"}
        self._kinds = kinds
        self.trigger_calls = []

    def automation_action_kinds(self, automation_id):
        return self._kinds

    async def automation_actionable(self, automation_id):
        return self._actionable

    async def automation_trigger(self, automation_id, item_id, kind):
        self.trigger_calls.append((automation_id, item_id, kind))
        return self._result


@contextmanager
def _client(orch):
    from fastapi.testclient import TestClient
    from server.app import create_app
    from server.sessions.orchestrator import SessionOrchestrator
    from server.tests.fakes.fake_factory import fake_sdk_factory

    with patch.dict(os.environ, {"CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:",
                                 "CCSWITCH_DEV": "1", "CCSWITCH_TAILNET_ALLOWLIST": ""}):
        real = SessionOrchestrator(sdk_factory=fake_sdk_factory())
        app = create_app(orchestrator=real)
        app.state.sessions = orch
        with TestClient(app, raise_server_exceptions=False) as c:
            yield c


def test_actionable_lists_items():
    items = [{"id": "o/r#7", "title": "red", "subtitle": None, "url": "https://x/7",
              "actions": [{"kind": "failing_ci", "label": "Fix CI", "style": "default"}]}]
    with _client(_FakeOrch(actionable=items)) as c:
        r = c.get("/api/automations/pr-doctor/actionable")
    assert r.status_code == 200
    assert r.json() == items


def test_trigger_routes_and_returns_result():
    orch = _FakeOrch()
    with _client(orch) as c:
        r = c.post("/api/automations/pr-doctor/trigger",
                   json={"item_id": "o/r#7", "kind": "failing_ci"})
    assert r.status_code == 200
    assert r.json()["level"] == "ok"
    assert orch.trigger_calls == [("pr-doctor", "o/r#7", "failing_ci")]


def test_trigger_unknown_kind_422():
    with _client(_FakeOrch(kinds=frozenset({"failing_ci"}))) as c:
        r = c.post("/api/automations/pr-doctor/trigger",
                   json={"item_id": "o/r#7", "kind": "bogus"})
    assert r.status_code == 422


def test_actionable_provider_less_automation_404():
    with _client(_FakeOrch(kinds=None)) as c:
        r = c.get("/api/automations/cicd-doctor/actionable")
    assert r.status_code == 404


def test_actionable_unknown_automation_404():
    with _client(_FakeOrch()) as c:
        r = c.get("/api/automations/does-not-exist/actionable")
    assert r.status_code == 404
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=.:src CCSWITCH_DEV=1 python3 -m pytest server/tests/test_automation_actions_api.py -v`
Expected: FAIL — 404s on the new routes (routes not defined yet).

- [ ] **Step 3: Add the generic routes**

In `server/api/automations.py`, add a request model near the top models:

```python
class TriggerBody(BaseModel):
    item_id: str
    kind: str
```

Then add the routes (place them after `patch_automation`, before the pr-doctor-specific block that Task 6 removes):

```python
@router.get("/{automation_id}/actionable")
async def automation_actionable(automation_id: str, request: Request,
                                _identity: str = Depends(current_identity)) -> list[dict]:
    """Detected items an automation can act on now, for the card's action buttons."""
    _automation_or_404(automation_id)
    orch = request.app.state.sessions
    if orch.automation_action_kinds(automation_id) is None:
        raise HTTPException(status_code=404, detail="automation has no action provider")
    return await orch.automation_actionable(automation_id)


@router.post("/{automation_id}/trigger")
async def automation_trigger(automation_id: str, body: TriggerBody, request: Request,
                             _identity: str = Depends(current_identity)) -> dict:
    """Fire one manual action on an actionable item; returns the TriggerResult."""
    _automation_or_404(automation_id)
    orch = request.app.state.sessions
    kinds = orch.automation_action_kinds(automation_id)
    if kinds is None:
        raise HTTPException(status_code=404, detail="automation has no action provider")
    if body.kind not in kinds:
        raise HTTPException(status_code=422, detail=f"invalid kind {body.kind!r}")
    return await orch.automation_trigger(automation_id, body.item_id, body.kind)
```

> **Route ordering note:** FastAPI matches routes in declaration order. Declare these `/{automation_id}/actionable` and `/{automation_id}/trigger` routes AFTER any literal-path routes under the same prefix would be unaffected (they use distinct suffixes), but BEFORE Task 6 deletes the `/pr-doctor/actionable` literal route — during the transition both exist and FastAPI prefers the literal `/pr-doctor/actionable` match, which is harmless (same data). After Task 6 only the generic route remains.

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=.:src CCSWITCH_DEV=1 python3 -m pytest server/tests/test_automation_actions_api.py -v`
Expected: PASS (5 passed).

- [ ] **Step 5: Commit**

```bash
git add server/api/automations.py server/tests/test_automation_actions_api.py
git commit -m "feat(api): generic /automations/{id}/actionable + /trigger routes"
```

---

### Task 6: Purge the old backend surfaces

**Files:**
- Modify: `server/api/automations.py` (delete 3 routes + 2 body models)
- Modify: `server/sessions/orchestrator.py` (delete `pr_doctor_actionable`, `pr_doctor_fix`, `backlog_runner_implement` shim)
- Modify: `server/tests/test_pr_doctor_actions_api.py` (rewrite onto generic path)

**Interfaces:**
- Consumes: generic routes (Task 5), dispatch (Task 4).
- Produces: no old `/pr-doctor/actionable`, `/pr-doctor/fix`, `/backlog-runner/implement` routes; no `PrDoctorFixBody`/`BacklogImplementBody`; no orchestrator `pr_doctor_actionable`/`pr_doctor_fix`/`backlog_runner_implement`.

- [ ] **Step 1: Rewrite the pr-doctor API test onto the generic path**

Replace `server/tests/test_pr_doctor_actions_api.py` so `_FakeOrch` implements the dispatch methods and the tests hit the generic routes:

```python
import os
from contextlib import contextmanager
from unittest.mock import patch


class _FakeOrch:
    """Records automation_trigger calls; serves canned actionable/kinds."""

    def __init__(self, *, actionable=None,
                 result=None, kinds=frozenset({"failing_ci", "merge_conflict", "out_of_date"})):
        self._actionable = actionable or []
        self._result = result or {"status": "sent", "level": "ok", "message": "Sent fix prompt to live session"}
        self._kinds = kinds
        self.trigger_calls = []

    def automation_action_kinds(self, automation_id):
        return self._kinds

    async def automation_actionable(self, automation_id):
        return self._actionable

    async def automation_trigger(self, automation_id, item_id, kind):
        self.trigger_calls.append((item_id, kind))
        return self._result


@contextmanager
def _client(orch):
    from fastapi.testclient import TestClient
    from server.app import create_app
    from server.sessions.orchestrator import SessionOrchestrator
    from server.tests.fakes.fake_factory import fake_sdk_factory

    with patch.dict(os.environ, {"CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:",
                                 "CCSWITCH_DEV": "1", "CCSWITCH_TAILNET_ALLOWLIST": ""}):
        real = SessionOrchestrator(sdk_factory=fake_sdk_factory())
        app = create_app(orchestrator=real)
        app.state.sessions = orch
        with TestClient(app, raise_server_exceptions=False) as c:
            yield c


def test_fix_requires_auth():
    with patch.dict(os.environ, {"CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:",
                                 "CCSWITCH_DEV": "1"}):
        from fastapi.testclient import TestClient
        from server.app import create_app
        from server.sessions.orchestrator import SessionOrchestrator
        from server.tests.fakes.fake_factory import fake_sdk_factory
        app = create_app(orchestrator=SessionOrchestrator(sdk_factory=fake_sdk_factory()))
        with TestClient(app, raise_server_exceptions=False) as c:
            r = c.post("/api/automations/pr-doctor/trigger",
                       json={"item_id": "o/r#7", "kind": "failing_ci"})
    assert r.status_code == 401


def test_fix_bad_kind_422():
    with _client(_FakeOrch()) as c:
        r = c.post("/api/automations/pr-doctor/trigger",
                   json={"item_id": "o/r#7", "kind": "nope"})
    assert r.status_code == 422


def test_fix_routes_to_trigger_for_actionable_pr():
    orch = _FakeOrch()
    with _client(orch) as c:
        r = c.post("/api/automations/pr-doctor/trigger",
                   json={"item_id": "owner/repo#43", "kind": "failing_ci"})
    assert r.status_code == 200
    assert orch.trigger_calls == [("owner/repo#43", "failing_ci")]


def test_actionable_returns_classified_prs():
    items = [{"id": "owner/repo#43", "title": "red CI", "subtitle": None,
              "url": "https://x/43",
              "actions": [{"kind": "failing_ci", "label": "Fix CI", "style": "default"}]}]
    with _client(_FakeOrch(actionable=items)) as c:
        r = c.get("/api/automations/pr-doctor/actionable")
    assert r.status_code == 200
    assert r.json() == items
```

- [ ] **Step 2: Delete the old routes + body models**

In `server/api/automations.py`, delete: the `pr_doctor_actionable` route (`@router.get("/pr-doctor/actionable")`), the `pr_doctor_fix` route (`@router.post("/pr-doctor/fix")`), the `backlog_runner_implement` route (`@router.post("/backlog-runner/implement")`), the `PrDoctorFixBody` and `BacklogImplementBody` classes, and the now-unused `_PR_DOCTOR_FIX_KINDS` constant. Keep `GET /cicd-doctor/findings`.

- [ ] **Step 3: Delete the old orchestrator methods**

In `server/sessions/orchestrator.py`, delete `async def pr_doctor_actionable`, `async def pr_doctor_fix`, and the `async def backlog_runner_implement` shim added in Task 4. Keep every private helper (`_pr_doctor_session_fix`, `_pr_doctor_mechanical`, `_pr_doctor_owning_rec`, `_pr_doctor_scope_pairs`, `backlog_runner_implement_core`, `backlog_candidates`).

- [ ] **Step 4: Verify nothing else references the deleted symbols**

Run: `cd /mnt/ssk-ssd/tony/GitHub/.ccswitch/worktrees/ccswitch/747d5970 && grep -rn "pr_doctor_fix\|pr_doctor_actionable\|backlog_runner_implement\b\|PrDoctorFixBody\|BacklogImplementBody\|_PR_DOCTOR_FIX_KINDS" server/ --include=*.py | grep -v test`
Expected: no matches (empty output).

- [ ] **Step 5: Run the backend suite for the touched areas**

Run: `PYTHONPATH=.:src CCSWITCH_DEV=1 python3 -m pytest server/tests/test_pr_doctor_actions_api.py server/tests/test_automation_actions_api.py -v`
Expected: PASS. Then run any backlog API test that referenced `/backlog-runner/implement` and update it to `POST /api/automations/backlog-runner/trigger` with `{"item_id": <ref>, "kind": "implement"}`; re-run until green.

- [ ] **Step 6: Commit**

```bash
git add server/api/automations.py server/sessions/orchestrator.py server/tests/
git commit -m "refactor(automations): remove one-off pr-doctor/backlog action routes; use generic path"
```

---

### Task 7: Frontend types, hooks, and query key

**Files:**
- Modify: `web/src/api/automations.ts`
- Modify: `web/src/api/hooks.ts`

**Interfaces:**
- Produces: `interface ActionSpec { kind: string; label: string; style: 'default' | 'risky' }`; `interface ActionableItem { id: string; title: string; subtitle?: string | null; url?: string | null; actions: ActionSpec[] }`; `interface TriggerResult { status: string; level: 'ok' | 'warn' | 'error'; message: string }`; `fetchAutomationActionable(id)`, `postAutomationTrigger(id, body)`; `QK.automationActionable(id)`; `useAutomationActionable(id, enabled?)`, `useAutomationTrigger(id)`.

- [ ] **Step 1: Replace the pr-doctor API types/fetchers**

In `web/src/api/automations.ts`, delete `PrDoctorKind`, `PrDoctorActionablePr`, `fetchPrDoctorActionable`, `postPrDoctorFix`. Add:

```typescript
export interface ActionSpec {
  kind: string
  label: string
  style: 'default' | 'risky'
}

export interface ActionableItem {
  id: string
  title: string
  subtitle?: string | null
  url?: string | null
  actions: ActionSpec[]
}

export interface TriggerResult {
  status: string
  level: 'ok' | 'warn' | 'error'
  message: string
}

export function fetchAutomationActionable(id: string): Promise<ActionableItem[]> {
  return api.get<ActionableItem[]>(`/automations/${id}/actionable`)
}

export function postAutomationTrigger(
  id: string, body: { item_id: string; kind: string },
): Promise<TriggerResult> {
  return api.post<TriggerResult>(`/automations/${id}/trigger`, body)
}
```

- [ ] **Step 2: Replace the query key + hooks**

In `web/src/api/hooks.ts`: update the import from `../api/automations` to drop `PrDoctorActionablePr`/`PrDoctorKind`/`fetchPrDoctorActionable`/`postPrDoctorFix` and add `ActionableItem`, `TriggerResult`, `fetchAutomationActionable`, `postAutomationTrigger`. In the `QK` object, replace `prDoctorActionable: ['pr-doctor-actionable'] as const,` with:

```typescript
  automationActionable: (id: string) => ['automation-actionable', id] as const,
```

Replace `usePrDoctorActionable`/`usePrDoctorFix` with:

```typescript
export function useAutomationActionable(id: string, enabled = true) {
  return useQuery<ActionableItem[]>({
    queryKey: QK.automationActionable(id),
    queryFn: () => fetchAutomationActionable(id),
    enabled, staleTime: 10_000 })
}

export function useAutomationTrigger(id: string) {
  const qc = useQueryClient()
  return useMutation<TriggerResult, ApiRequestError, { item_id: string; kind: string }>({
    mutationFn: (body) => postAutomationTrigger(id, body),
    onSuccess: () => { qc.invalidateQueries({ queryKey: QK.automationActionable(id) }) },
  })
}
```

- [ ] **Step 3: Type-check (expected to fail on the component until Task 8)**

Run (in `web/`): `npx tsc --noEmit`
Expected: errors ONLY in `AutomationCard.tsx` (still references the removed `usePrDoctorActionable`/`usePrDoctorFix`). No errors in `automations.ts`/`hooks.ts` themselves. This confirms Task 7 is internally consistent; Task 8 resolves the component references.

- [ ] **Step 4: Commit**

```bash
git add web/src/api/automations.ts web/src/api/hooks.ts
git commit -m "feat(web): generic automation actionable/trigger types, hooks, query key"
```

---

### Task 8: `<AutomationActions>` component + card wiring + tests

**Files:**
- Modify: `web/src/components/AutomationCard.tsx`
- Modify: `web/src/components/AutomationCard.test.tsx`

**Interfaces:**
- Consumes: `useAutomationActionable`, `useAutomationTrigger`, `ActionableItem`, `ActionSpec`, `TriggerResult`, `toast` from `sonner`.
- Produces: `<AutomationActions automationId={string} enabled={boolean} />`, rendered for any automation whose actionable list is non-empty.

- [ ] **Step 1: Write the failing component tests**

In `web/src/components/AutomationCard.test.tsx`, replace the entire `describe`/`it` block covering "Actionable PRs" (the tests using `usePrDoctorActionable`/`usePrDoctorFix`) with:

```tsx
import { toast } from 'sonner'
vi.mock('sonner', () => ({
  toast: { success: vi.fn(), warning: vi.fn(), error: vi.fn() },
}))

describe('AutomationActions', () => {
  const ITEMS = [
    { id: 'owner/repo#42', title: 'behind PR', subtitle: null, url: 'https://x/42',
      actions: [{ kind: 'out_of_date', label: 'Update branch', style: 'default' }] },
    { id: 'owner/repo#43', title: 'red CI', subtitle: null, url: 'https://x/43',
      actions: [{ kind: 'failing_ci', label: 'Fix CI', style: 'default' }] },
    { id: 'owner/repo#44', title: 'conflicted', subtitle: null, url: 'https://x/44',
      actions: [{ kind: 'merge_conflict', label: 'Resolve conflicts', style: 'risky' }] },
  ]

  function mockActions(mutateAsync: ReturnType<typeof vi.fn>) {
    vi.spyOn(hooks, 'useAutomationActionable').mockReturnValue(
      { data: ITEMS } as unknown as ReturnType<typeof hooks.useAutomationActionable>)
    vi.spyOn(hooks, 'useAutomationTrigger').mockReturnValue(
      { mutateAsync, isPending: false } as unknown as ReturnType<typeof hooks.useAutomationTrigger>)
  }

  function expand() {
    render(<MemoryRouter><AutomationCard automation={PR_DOCTOR} /></MemoryRouter>)
    fireEvent.click(screen.getByRole('button', { name: 'Expand' }))
  }

  it('renders a button per action and triggers on click', async () => {
    const mutateAsync = vi.fn().mockResolvedValue(
      { status: 'spawned', level: 'ok', message: 'Spawned fix session' })
    mockBasicHooks(); mockActions(mutateAsync)
    expand()
    expect(screen.getByText('red CI')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Fix CI' }))
    expect(mutateAsync).toHaveBeenCalledWith({ item_id: 'owner/repo#43', kind: 'failing_ci' })
    await screen.findByText('Spawned fix session')
    expect(toast.success).toHaveBeenCalledWith('Spawned fix session')
  })

  it('surfaces a warn outcome (usage ceiling) without a success toast', async () => {
    const mutateAsync = vi.fn().mockResolvedValue(
      { status: 'ceiling', level: 'warn', message: 'All accounts at/above usage ceiling — try later' })
    mockBasicHooks(); mockActions(mutateAsync)
    expand()
    fireEvent.click(screen.getByRole('button', { name: 'Fix CI' }))
    await screen.findByText(/usage ceiling/)
    expect(toast.warning).toHaveBeenCalledWith('All accounts at/above usage ceiling — try later')
    expect(toast.success).not.toHaveBeenCalled()
  })

  it('requires a confirm step for a risky action', async () => {
    const mutateAsync = vi.fn().mockResolvedValue(
      { status: 'spawned', level: 'ok', message: 'Spawned fix session' })
    mockBasicHooks(); mockActions(mutateAsync)
    expand()
    fireEvent.click(screen.getByRole('button', { name: 'Resolve conflicts' }))
    expect(mutateAsync).not.toHaveBeenCalled()               // first click = arm confirm
    fireEvent.click(screen.getByRole('button', { name: /Confirm/ }))
    expect(mutateAsync).toHaveBeenCalledWith({ item_id: 'owner/repo#44', kind: 'merge_conflict' })
  })

  it('shows empty state when there are no actionable items', () => {
    vi.spyOn(hooks, 'useAutomationActionable').mockReturnValue(
      { data: [] } as unknown as ReturnType<typeof hooks.useAutomationActionable>)
    vi.spyOn(hooks, 'useAutomationTrigger').mockReturnValue(
      { mutateAsync: vi.fn(), isPending: false } as unknown as ReturnType<typeof hooks.useAutomationTrigger>)
    mockBasicHooks()
    expand()
    expect(screen.getByText('No actionable items.')).toBeInTheDocument()
  })
})
```

> If the existing `PR_DOCTOR` test fixture or `mockBasicHooks` helper is defined elsewhere in the file, keep using it. `mockBasicHooks` must NOT stub `useAutomationActionable`/`useAutomationTrigger` (the per-test `mockActions` does that).

- [ ] **Step 2: Run tests to verify they fail**

Run (in `web/`): `npx vitest run src/components/AutomationCard.test.tsx`
Expected: FAIL — `useAutomationActionable`/`useAutomationTrigger` not found on `hooks`, and "No actionable items." not rendered.

- [ ] **Step 3: Implement `<AutomationActions>` and rewire the card**

In `web/src/components/AutomationCard.tsx`: remove `ActionablePrsSection` and the `KIND_LABEL` const; update the hook imports (`usePrDoctorActionable`/`usePrDoctorFix` → `useAutomationActionable`/`useAutomationTrigger`); add `import { toast } from 'sonner'` and `import type { ActionSpec } from '../api/automations'`. Add:

```tsx
function outcomeToast(r: { level: string; message: string }) {
  if (r.level === 'ok') toast.success(r.message)
  else if (r.level === 'warn') toast.warning(r.message)
  else toast.error(r.message)
}

function ActionButton(
  { item, action, automationId }:
  { item: { id: string }; action: ActionSpec; automationId: string },
) {
  const trigger = useAutomationTrigger(automationId)
  const [armed, setArmed] = useState(false)     // risky: first click arms confirm
  const [outcome, setOutcome] = useState<{ level: string; message: string } | null>(null)
  const busy = trigger.isPending

  async function fire() {
    setArmed(false)
    const r = await trigger.mutateAsync({ item_id: item.id, kind: action.kind })
    setOutcome({ level: r.level, message: r.message })
    outcomeToast(r)
  }

  function onClick() {
    if (action.style === 'risky' && !armed) { setArmed(true); return }
    void fire()
  }

  if (outcome) {
    const tone = outcome.level === 'ok' ? 'text-green-700 bg-green-50'
      : outcome.level === 'warn' ? 'text-amber-700 bg-amber-50'
      : 'text-red-700 bg-red-50'
    return <span className={`rounded px-2 py-1 text-xs font-semibold ${tone}`}>{outcome.message}</span>
  }

  const base = 'rounded border px-2 py-1 text-xs font-semibold disabled:opacity-50'
  const tone = action.style === 'risky'
    ? 'border-red-300 text-red-700 hover:bg-red-600 hover:text-white hover:border-red-600'
    : 'border-border hover:bg-primary hover:text-primary-foreground hover:border-primary'
  return (
    <button type="button" disabled={busy} onClick={onClick} className={`${base} ${tone}`}>
      {busy ? 'Working…' : armed ? `Confirm ${action.label}?` : action.label}
    </button>
  )
}

function AutomationActions({ automationId, enabled }: { automationId: string; enabled: boolean }) {
  const actionable = useAutomationActionable(automationId, enabled)
  const list = actionable.data ?? []
  return (
    <section>
      <h4 className="text-xs font-semibold uppercase text-muted-foreground mb-2">Actionable items</h4>
      {list.length === 0
        ? <p className="text-sm text-muted-foreground">No actionable items.</p>
        : (
          <ul className="space-y-1 text-sm">
            {list.map((item) => (
              <li key={item.id} className="flex items-center gap-2">
                <span className="flex-1 truncate">
                  <span className="text-muted-foreground tabular-nums">{item.id.split('#')[1] ? `#${item.id.split('#')[1]}` : ''}</span>{' '}
                  {item.title}
                </span>
                {item.actions.map((action) => (
                  <ActionButton key={action.kind} item={item} action={action} automationId={automationId} />
                ))}
              </li>
            ))}
          </ul>
        )}
    </section>
  )
}
```

> **Per-action pending note:** each `ActionButton` owns its own `useAutomationTrigger`, so `isPending` is scoped to that button — a spinner on one row never disables siblings. This is the fix for today's global-disable behavior.

Then replace the render gate `{a.id === 'pr-doctor' && <ActionablePrsSection enabled={open} />}` with:

```tsx
      <AutomationActions automationId={a.id} enabled={open} />
```

(Rendered for every automation; the empty-state line shows for automations with no actionable items. The backend 404s for provider-less automations — TanStack Query keeps that error internal, so the section simply shows "No actionable items." for cicd-doctor/autopilot.)

- [ ] **Step 4: Run tests to verify they pass**

Run (in `web/`): `npx vitest run src/components/AutomationCard.test.tsx`
Expected: PASS (all AutomationActions tests green).

- [ ] **Step 5: Full frontend gate**

Run (in `web/`): `npx tsc --noEmit && npx eslint src/components/AutomationCard.tsx src/api/hooks.ts src/api/automations.ts`
Expected: no errors.

- [ ] **Step 6: Commit**

```bash
git add web/src/components/AutomationCard.tsx web/src/components/AutomationCard.test.tsx
git commit -m "feat(web): reusable AutomationActions surface with per-button pending, toast + inline outcome, risky confirm"
```

---

### Task 9: Full-suite verification

**Files:** none (verification only).

- [ ] **Step 1: Backend suite**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 PYTHONPATH=.:src python3 -m pytest server/tests/ -q`
Expected: PASS (or only the pre-known flakes noted in project memory — `test_batch_unarchive_partial_failure`, `:memory:` seed-visibility — which are unrelated; rerun to confirm).

- [ ] **Step 2: Backend lint + types**

Run: `PYTHONPATH=.:src CCSWITCH_DEV=1 python3 -m ruff check server/ && PYTHONPATH=.:src CCSWITCH_DEV=1 python3 -m mypy server/automations/action_providers.py server/api/automations.py server/sessions/orchestrator.py`
Expected: no new errors.

- [ ] **Step 3: Frontend suite + gates**

Run (in `web/`): `npx vitest run && npx tsc --noEmit && npx eslint src`
Expected: PASS.

- [ ] **Step 4: Grep for orphans**

Run: `grep -rn "PrDoctorActionablePr\|usePrDoctorFix\|usePrDoctorActionable\|prDoctorActionable\|ActionablePrsSection\|postPrDoctorFix\|fetchPrDoctorActionable" web/src server | grep -v node_modules`
Expected: no matches.

- [ ] **Step 5: Commit any lint fixups, then open the PR per the project completion workflow**

Branch off `origin/master`, push, `gh pr create` (non-draft), then `gh pr merge <#> --squash --auto`.

---

## Self-Review

**Spec coverage:**
- Backend contract (`ActionSpec`/`ActionableItem`/`TriggerResult`) → Task 1. ✓
- `ActionProvider` + dispatch → Tasks 1, 3, 4. ✓
- pr-doctor migrated → Tasks 3, 6. ✓
- backlog-runner migrated (incl. new `actionable()` side) → Tasks 3, 4, 6. ✓
- cicd-doctor deferred (no provider; section shows empty state) → confirmed in Task 8. ✓
- Ceiling status → level/message → Tasks 2, 3. ✓
- Generic API routes + delete old → Tasks 5, 6. ✓
- Frontend types/hooks/QK → Task 7. ✓
- `<AutomationActions>`: per-button pending, toast + inline outcome, risky confirm → Task 8. ✓
- Tests (new generic API, provider units, rewritten pr-doctor API + component) → Tasks 3, 5, 6, 8. ✓
- Sonner reuse (no parallel toaster) → Task 8 uses `sonner`. ✓
- No-half-dead-code → Task 6 purge + Task 9 orphan grep. ✓

**Placeholder scan:** No TBD/TODO/"handle edge cases"/"similar to". Every code step shows full code; every run step shows the command + expected result.

**Type consistency:** `ActionableItem.id`/`title`/`subtitle`/`url`/`actions` consistent across base.py, providers, API tests, TS interface, and component. `TriggerResult.status`/`level`/`message` consistent. Hook names `useAutomationActionable`/`useAutomationTrigger` and `QK.automationActionable(id)` consistent across Tasks 7–8. Provider method names `backlog_candidates`/`backlog_runner_implement_core` consistent across Tasks 3–4–6. `automation_action_kinds`/`automation_actionable`/`automation_trigger` consistent across Tasks 4–5–6.

One known transition subtlety (documented, not a defect): in Task 5 the generic `/{automation_id}/actionable` route coexists with the literal `/pr-doctor/actionable` until Task 6; FastAPI prefers the literal match and both return identical data, so behavior is stable throughout.

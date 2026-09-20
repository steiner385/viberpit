# Capacity settings PR 1 — Limits section and the two ceilings

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The global active-session cap becomes the lower of a physical ceiling (Σ enabled host caps) and a typed attention ceiling, read through ONE function, and gets its own Settings › Capacity › Limits section; the Settings group is renamed Capacity; General loses its Capacity group; the capacity readout and every dial page link to each other through typed paths and stay fresh.

**Architecture:** One new pure module `server/sessions/global_cap.py` (`resolve_global_cap` + `read_global_cap(db)`) replaces three clamps and two readers. The settings API exposes the derivation as `effectiveDefaults.globalCap`; the readout exposes it on `global.{physical,attention,binding}` through a new optional `CapacitySources` seam. The web gains a `LimitsSection` (`capacity` id, group `capacity`), a shared `ManagedEnvTable`, and cache invalidation of `QK.capacity` from every dial mutation.

**Tech Stack:** Python 3 / FastAPI / SQLAlchemy async (SQLite in tests) · React 18 + TypeScript + React Query + react-router · vitest + testing-library + jest-axe · pytest.

**Spec:** `docs/superpowers/specs/2026-09-20-capacity-settings-structure-design.md` — this plan is §8 **PR 1**; it implements D2, D3, D9, D10 (invalidation + typed links only), D11 (first half) and closes C1, C2 (partly), C4, C14, C17, C20. Read §4 D3 and §5.1 Limits before starting.

## Global Constraints

- **Vocabulary (spec D8):** UI labels are exactly **Global cap**, **Physical ceiling**, **Attention ceiling**, **Host cap** (Hosts column header **Cap**). The labels "Max active sessions" and "Max sessions" are retired in this PR.
- **No schema change.** `AppSettings.max_active_sessions` keeps its column; only its meaning changes (attention ceiling). No migration.
- **One reader (D11):** after this PR, `DEFAULT_MAX_ACTIVE_SESSIONS` is referenced only in `server/db/models.py` and `server/sessions/global_cap.py`; `max_active_sessions` is read only in `global_cap.py` (written and echoed in `server/api/settings.py`). A test pins this.
- **Section ids are stable.** New section id `capacity`; existing ids (`general`, `fleet`, `accounts`, …) unchanged. Group id `accounts` → `capacity`.
- **Env var names:** no new env vars. The three added to `_MANAGED` already exist (`CCSWITCH_TEST_SLOT_CAP`, `CCSWITCH_CHILD_SESSION_CAP`, `CCSWITCH_STT_MAX_CONCURRENT`).
- **Save contract (2026-09-04 §7):** the Limits section is a Form section with one `SaveBar`; read-only rows use `selfLabelled` with a `<span>` child.
- **Test commands (run from the repo root, never `cd` bare — the Bash hook is cwd-relative):**
  - Server, one file: `mkdir -p /tmp/viberpit-test-home && env -i HOME=/tmp/viberpit-test-home PATH="$PATH" CCSWITCH_DEV=1 PYTHONPATH=$PWD/src /home/tony/GitHub/viberpit-deploy/.venv/bin/python -m pytest server/tests/<file>.py -q`
  - Web (first time in a fresh worktree): `[ -e web/node_modules ] || ln -s /home/tony/GitHub/viberpit-deploy/web/node_modules web/node_modules`
  - Web, one file: `(cd web && npx vitest run src/<path>.test.tsx)`
  - Final gate (Task 14): `ruff check src/ server/`, `mypy server/ --ignore-missing-imports`, `pytest tests/`, `pytest server/tests/ -n auto --dist loadfile`, and in `web/`: `npm run typecheck`, `npm run lint`, `npm run test`, `npm run build`.
- **Commits:** write each message to a file and pass `-F`; end with `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.

---

## File structure

| File | Responsibility |
|---|---|
| `server/sessions/global_cap.py` (new) | `GlobalCap` dataclass, pure `resolve_global_cap`, `read_global_cap(db)`, `drain_if_raised(orch, before, after)`. The only reader of the cap. |
| `server/sessions/orchestrator.py` | `_max_active_sessions()` delegates to `read_global_cap`. |
| `server/api/sessions.py` | `_max_active_sessions_setting` deleted; `_compute_queue_stall_reason` calls `read_global_cap`. |
| `server/api/settings.py` | `effectiveDefaults.globalCap`; `maxActiveSessions` null-clears / 422 below 1; drain when the effective cap rises; `_MANAGED` + 3. |
| `server/api/fleet.py` | create / update / delete host drain the queue when the effective cap rises. |
| `server/automations/capacity_readout.py`, `capacity_wiring.py` | `global_limits` seam → `global.{physical,attention,binding}`. |
| `web/src/api/hooks.ts` | `GlobalCapDefaults` type; four mutations invalidate `QK.capacity`. |
| `web/src/api/capacity.ts` | `global` row type gains the three fields. |
| `web/src/features/settings/chassis/ManagedEnvTable.tsx` (new) | The read-only env table, shared by About and Limits. |
| `web/src/features/settings/sections/LimitsSection.tsx` (new) | Settings › Capacity › Limits. |
| `web/src/features/settings/settingsDefs.ts` | Group `capacity`; section `capacity`. |
| `web/src/features/settings/sections/GeneralSection.tsx` | Capacity group removed. |
| `web/src/features/settings/sections/FleetSection.tsx` | Inner header removed; **Cap** column; physical-ceiling footer link. |
| `web/src/features/delivery/capacity/capacityStack.ts` | Typed hrefs. |
| `web/src/test/factories.ts` | `appSettings` factory carries `globalCap`. |

---

### Task 1: `global_cap.py` — the one reader

**Files:**
- Create: `server/sessions/global_cap.py`
- Test: `server/tests/test_global_cap.py`

**Interfaces:**
- Produces: `GlobalCap(physical: int | None, attention: int | None, effective: int, binding: Literal["physical","attention","default"], hosts: tuple[tuple[str, int | None], ...])` with `.to_wire() -> dict`; `resolve_global_cap(*, host_caps, attention, default=DEFAULT_MAX_ACTIVE_SESSIONS) -> GlobalCap`; `async read_global_cap(db: AsyncSession) -> GlobalCap`; `drain_if_raised(orch, before: GlobalCap, after: GlobalCap) -> bool`.

- [ ] **Step 1: Write the failing tests**

```python
# server/tests/test_global_cap.py
"""The effective global cap = min(physical ceiling, attention ceiling) (spec 2026-09-20 D3, D11)."""
from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from server.sessions.global_cap import GlobalCap, drain_if_raised, read_global_cap, resolve_global_cap


def test_physical_is_the_sum_of_enabled_host_caps():
    gc = resolve_global_cap(host_caps=[("dobby", 4), ("rocky", 6)], attention=None)
    assert (gc.physical, gc.attention, gc.effective, gc.binding) == (10, None, 10, "physical")
    assert gc.hosts == (("dobby", 4), ("rocky", 6))


def test_any_uncapped_host_makes_the_physical_ceiling_unbounded():
    gc = resolve_global_cap(host_caps=[("dobby", 4), ("rocky", None)], attention=None)
    assert gc.physical is None
    assert (gc.effective, gc.binding) == (5, "default")


def test_no_hosts_and_no_attention_falls_back_to_the_default_floor():
    gc = resolve_global_cap(host_caps=[], attention=None)
    assert (gc.physical, gc.attention, gc.effective, gc.binding) == (None, None, 5, "default")


def test_attention_below_physical_binds():
    gc = resolve_global_cap(host_caps=[("dobby", 4), ("rocky", 6)], attention=6)
    assert (gc.effective, gc.binding) == (6, "attention")


def test_attention_equal_to_physical_binds_as_attention():
    """The typed value is the one the person will look for, so a tie names it."""
    gc = resolve_global_cap(host_caps=[("dobby", 4), ("rocky", 6)], attention=10)
    assert (gc.effective, gc.binding) == (10, "attention")


def test_attention_above_physical_is_ignored_and_physical_binds():
    gc = resolve_global_cap(host_caps=[("dobby", 4), ("rocky", 6)], attention=12)
    assert (gc.physical, gc.attention, gc.effective, gc.binding) == (10, 12, 10, "physical")


def test_attention_alone_binds_when_hosts_are_unbounded():
    gc = resolve_global_cap(host_caps=[("dobby", None)], attention=3)
    assert (gc.effective, gc.binding) == (3, "attention")


def test_effective_is_clamped_to_at_least_one():
    assert resolve_global_cap(host_caps=[], attention=0).effective == 1
    assert resolve_global_cap(host_caps=[], attention=None, default=0).effective == 1


def test_to_wire_shape():
    gc = resolve_global_cap(host_caps=[("dobby", 4)], attention=None)
    assert gc.to_wire() == {"physical": 4, "attention": None, "effective": 4,
                            "binding": "physical", "hosts": [{"name": "dobby", "cap": 4}]}


def test_drain_if_raised_fires_only_when_the_effective_cap_rose():
    orch = AsyncMock()
    lower = GlobalCap(10, 4, 4, "attention", ())
    higher = GlobalCap(10, 8, 8, "attention", ())

    async def run():
        assert drain_if_raised(orch, lower, higher) is True
        await asyncio.sleep(0)  # let the created task run
        orch.maybe_start_queued.assert_awaited_once()
        orch.maybe_start_queued.reset_mock()
        assert drain_if_raised(orch, higher, lower) is False
        assert drain_if_raised(orch, lower, lower) is False
        await asyncio.sleep(0)
        orch.maybe_start_queued.assert_not_awaited()
        # No orchestrator (minimal test apps): a no-op, never a crash.
        assert drain_if_raised(None, lower, higher) is False

    asyncio.run(run())


@pytest.mark.usefixtures("isolated_db")
def test_read_global_cap_reads_enabled_hosts_and_the_settings_row():
    from server.db import db_session
    from server.db.models import AppSettings, Host

    async def run():
        async with db_session() as db:
            db.add(Host(name="dobby", endpoint="https://dobby:9443", enabled=True, max_sessions=4))
            db.add(Host(name="rocky", endpoint="https://rocky:9443", enabled=True, max_sessions=6))
            # Disabled: contributes nothing, even though uncapped.
            db.add(Host(name="old", endpoint="https://old:9443", enabled=False, max_sessions=None))
            await db.flush()
        async with db_session() as db:
            gc = await read_global_cap(db)
            assert (gc.physical, gc.attention, gc.effective, gc.binding) == (10, None, 10, "physical")
        async with db_session() as db:
            db.add(AppSettings(id="singleton", max_active_sessions=3))
            await db.flush()
        async with db_session() as db:
            gc = await read_global_cap(db)
            assert (gc.effective, gc.binding) == (3, "attention")

    asyncio.run(run())


def test_no_other_reader_of_max_active_sessions():
    """D11: exactly one reader. `server/api/settings.py` may WRITE and ECHO the
    column; nobody but global_cap.py may apply the default or read it as a cap."""
    root = Path(__file__).resolve().parents[2]
    allowed = {"server/db/models.py", "server/sessions/global_cap.py"}
    offenders: list[str] = []
    for path in (root / "server").rglob("*.py"):
        rel = path.relative_to(root).as_posix()
        if rel.startswith(("server/tests/", "server/db/migrations/")) or rel in allowed:
            continue
        text = path.read_text()
        if "DEFAULT_MAX_ACTIVE_SESSIONS" in text:
            offenders.append(f"{rel}: DEFAULT_MAX_ACTIVE_SESSIONS")
        if rel != "server/api/settings.py" and "max_active_sessions" in text:
            offenders.append(f"{rel}: max_active_sessions")
    assert offenders == []
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `mkdir -p /tmp/viberpit-test-home && env -i HOME=/tmp/viberpit-test-home PATH="$PATH" CCSWITCH_DEV=1 PYTHONPATH=$PWD/src /home/tony/GitHub/viberpit-deploy/.venv/bin/python -m pytest server/tests/test_global_cap.py -q`
Expected: collection error `ModuleNotFoundError: No module named 'server.sessions.global_cap'`.

- [ ] **Step 3: Write the module**

```python
# server/sessions/global_cap.py
"""The effective global active-session cap (spec 2026-09-20 D3, D11).

ONE reader. The orchestrator's admission gate, the sessions list's queue-stall
reason, the settings API's effective defaults, the fleet API's queue drain and
the capacity readout all come through here; nothing else reads
``AppSettings.max_active_sessions`` as a cap (pinned by
``test_global_cap.py::test_no_other_reader_of_max_active_sessions``).

Two ceilings; the lower one wins:

* **physical** — Σ ``Host.max_sessions`` over ENABLED hosts. Unbounded (``None``)
  when there are no hosts or any enabled host is uncapped. Health is a runtime
  condition, not provisioning, so an enabled-but-down host still counts here;
  the readout's Hosts row is where health shows.
* **attention** — ``AppSettings.max_active_sessions``: "never more than N at once,
  whatever the hardware could take". ``None`` = no attention ceiling.

When both are unbounded, ``DEFAULT_MAX_ACTIVE_SESSIONS`` is the last-resort floor:
a single host with no cap must not admit an unbounded number of sessions.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from server.db.models import DEFAULT_MAX_ACTIVE_SESSIONS, AppSettings, Host

log = logging.getLogger(__name__)

Binding = Literal["physical", "attention", "default"]


@dataclass(frozen=True)
class GlobalCap:
    physical: int | None
    attention: int | None
    effective: int
    binding: Binding
    # (name, cap) for every ENABLED host, name order — the Limits page's derivation line.
    hosts: tuple[tuple[str, int | None], ...]

    def to_wire(self) -> dict[str, Any]:
        return {
            "physical": self.physical,
            "attention": self.attention,
            "effective": self.effective,
            "binding": self.binding,
            "hosts": [{"name": name, "cap": cap} for name, cap in self.hosts],
        }


def resolve_global_cap(*, host_caps: Iterable[tuple[str, int | None]], attention: int | None,
                       default: int = DEFAULT_MAX_ACTIVE_SESSIONS) -> GlobalCap:
    """Pure: the same arithmetic for every caller, so the gate, the API and the
    readout can never disagree on what the cap is."""
    hosts = tuple(host_caps)
    physical: int | None
    if not hosts or any(cap is None for _, cap in hosts):
        physical = None
    else:
        physical = sum(cap for _, cap in hosts if cap is not None)
    # A tie names the attention ceiling: it is the typed value, the one the
    # person will look for when asking "why 10?".
    if attention is not None and (physical is None or attention <= physical):
        return GlobalCap(physical, attention, max(1, attention), "attention", hosts)
    if physical is not None:
        return GlobalCap(physical, attention, max(1, physical), "physical", hosts)
    return GlobalCap(None, None, max(1, default), "default", hosts)


async def read_global_cap(db: AsyncSession) -> GlobalCap:
    rows = (await db.execute(
        select(Host.name, Host.max_sessions).where(Host.enabled.is_(True)).order_by(Host.name)
    )).all()
    settings_row = (await db.execute(select(AppSettings))).scalar_one_or_none()
    attention = getattr(settings_row, "max_active_sessions", None) if settings_row else None
    return resolve_global_cap(host_caps=[(name, cap) for name, cap in rows], attention=attention)


def drain_if_raised(orch: Any, before: GlobalCap, after: GlobalCap) -> bool:
    """A dial moved the EFFECTIVE cap up: fill the new slots from the queue.

    Compares effective values, so raising the attention ceiling above the
    physical one (no new slot) does not drain, and capping a host lower does not
    either. ``orch`` may be None or lack ``maybe_start_queued`` (minimal test
    apps); that is a no-op, never a crash.
    """
    if after.effective <= before.effective:
        return False
    if orch is None or not hasattr(orch, "maybe_start_queued"):
        log.debug("global_cap: orchestrator unavailable for queue drain")
        return False
    asyncio.create_task(orch.maybe_start_queued())
    return True
```

- [ ] **Step 4: Run the tests**

Run: same command as Step 2.
Expected: every test PASSES **except** `test_no_other_reader_of_max_active_sessions`, which fails listing `server/sessions/orchestrator.py`, `server/api/sessions.py`, `server/api/settings.py`, `server/automations/capacity_wiring.py` (the comment). Tasks 2–5 clear them one by one; the file's other tests must be green now.

- [ ] **Step 5: Commit**

```bash
git add server/sessions/global_cap.py server/tests/test_global_cap.py
printf '%s\n' 'feat(capacity): one reader for the global cap — physical and attention ceilings' '' 'resolve_global_cap = min(Σ enabled host caps, AppSettings.max_active_sessions), default-5 floor when both are unbounded (spec 2026-09-20 D3, D11). The no-other-reader test is red until Tasks 2–5 route every caller through it.' '' 'Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>' > /tmp/pr1-t1.txt
git commit -F /tmp/pr1-t1.txt
```

---

### Task 2: Route the orchestrator gate and the sessions list through it

**Files:**
- Modify: `server/sessions/orchestrator.py:3016-3022` (`_max_active_sessions`)
- Modify: `server/api/sessions.py:484-500` (delete `_max_active_sessions_setting`), `:522-523` (its caller)
- Test: `server/tests/test_global_cap.py` (add one test)

**Interfaces:**
- Consumes: `read_global_cap(db)` from Task 1.
- Produces: `SessionOrchestrator._max_active_sessions() -> int` unchanged in signature (many tests `AsyncMock` it; it must stay an `async def` returning the effective int).

- [ ] **Step 1: Add the failing test**

Append to `server/tests/test_global_cap.py`:

```python
@pytest.mark.usefixtures("isolated_db")
def test_orchestrator_gate_reads_the_effective_cap():
    """`_max_active_sessions` stays an async int (dozens of tests AsyncMock it),
    but the int is now the derived effective cap, not the raw column."""
    from server.db import db_session
    from server.db.models import Host
    from server.sessions.orchestrator import SessionOrchestrator

    async def run():
        async with db_session() as db:
            db.add(Host(name="dobby", endpoint="https://dobby:9443", enabled=True, max_sessions=4))
            db.add(Host(name="rocky", endpoint="https://rocky:9443", enabled=True, max_sessions=6))
            await db.flush()
        orch = SessionOrchestrator.__new__(SessionOrchestrator)
        assert await orch._max_active_sessions() == 10

    asyncio.run(run())
```

- [ ] **Step 2: Run it to verify it fails**

Run: `env -i HOME=/tmp/viberpit-test-home PATH="$PATH" CCSWITCH_DEV=1 PYTHONPATH=$PWD/src /home/tony/GitHub/viberpit-deploy/.venv/bin/python -m pytest server/tests/test_global_cap.py -q -k orchestrator_gate`
Expected: FAIL, `assert 5 == 10` (the old reader ignores hosts).

- [ ] **Step 3: Replace the orchestrator method**

In `server/sessions/orchestrator.py`, replace the whole `_max_active_sessions` method (lines 3016–3022) with:

```python
    async def _max_active_sessions(self) -> int:
        """The effective global cap (spec 2026-09-20 D3): min(Σ enabled host
        caps, attention ceiling). One reader — server/sessions/global_cap.py."""
        from server.sessions.global_cap import read_global_cap
        async with db_session() as db:
            return (await read_global_cap(db)).effective
```

- [ ] **Step 4: Replace the sessions-list reader**

In `server/api/sessions.py`, delete the whole `_max_active_sessions_setting` function (the `async def` at line 484 through its `return` at line 499, including its docstring). Then in `_compute_queue_stall_reason`, replace

```python
    cap = await _max_active_sessions_setting()
```

with

```python
    from server.sessions.global_cap import read_global_cap
    async with db_session() as db:
        cap = (await read_global_cap(db)).effective
```

(`db_session` is already imported at the top of the module.) Update the docstring sentence `Only the numeric cap itself requires a DB read (see :func:`_max_active_sessions_setting`).` to `Only the numeric cap itself requires a DB read (``global_cap.read_global_cap``).`

- [ ] **Step 5: Run the tests**

Run: `env -i HOME=/tmp/viberpit-test-home PATH="$PATH" CCSWITCH_DEV=1 PYTHONPATH=$PWD/src /home/tony/GitHub/viberpit-deploy/.venv/bin/python -m pytest server/tests/test_global_cap.py server/tests/test_queue_capacity.py server/tests/test_sessions_api.py -q`
Expected: `test_orchestrator_gate_reads_the_effective_cap` PASSES; `test_no_other_reader…` still fails but its offender list no longer contains `orchestrator.py` or `api/sessions.py`; the queue and sessions suites are green. (If `test_sessions_api.py` does not exist under that name, run `ls server/tests | grep -i sessions_api` and use the file that tests `/api/sessions`.)

- [ ] **Step 6: Commit**

```bash
git add server/sessions/orchestrator.py server/api/sessions.py server/tests/test_global_cap.py
printf '%s\n' 'refactor(capacity): admission gate and queue-stall reason read the effective cap' '' 'Deletes the acknowledged copy in api/sessions.py; both now go through global_cap.read_global_cap (spec 2026-09-20 D11, C17).' '' 'Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>' > /tmp/pr1-t2.txt
git commit -F /tmp/pr1-t2.txt
```

---

### Task 3: Settings API — `effectiveDefaults.globalCap`, null-clears, 422, drain, `_MANAGED`

**Files:**
- Modify: `server/api/settings.py`
- Test: `server/tests/test_settings.py`

**Interfaces:**
- Consumes: `read_global_cap(db)`, `drain_if_raised(orch, before, after)`, `GlobalCap.to_wire()`.
- Produces: `GET /api/settings` → `effectiveDefaults.globalCap = {physical, attention, effective, binding, hosts:[{name,cap}]}` (the old `effectiveDefaults.maxActiveSessions` key is gone); `managed` gains `CCSWITCH_TEST_SLOT_CAP`, `CCSWITCH_CHILD_SESSION_CAP`, `CCSWITCH_STT_MAX_CONCURRENT`; `PUT {maxActiveSessions: null}` clears; `PUT {maxActiveSessions: 0}` → 422.

- [ ] **Step 1: Update and add tests**

In `server/tests/test_settings.py`, replace `test_put_max_active_zero_clamps_to_one` (lines 361–370) with:

```python
def test_put_max_active_below_one_is_422():
    """PUT {maxActiveSessions: 0} is refused, not silently clamped (spec 2026-09-20 §7)."""
    with _client() as client:
        resp = client.put("/api/settings", headers=IDENTITY_HEADERS, json={"maxActiveSessions": 0})
        assert resp.status_code == 422, resp.text
        assert client.get("/api/settings", headers=IDENTITY_HEADERS).json()["maxActiveSessions"] is None


def test_put_max_active_null_clears_the_attention_ceiling():
    """An explicit null clears (the web sends null for a blank field); an OMITTED key leaves it alone."""
    with _client() as client:
        client.put("/api/settings", headers=IDENTITY_HEADERS, json={"maxActiveSessions": 3})
        assert client.get("/api/settings", headers=IDENTITY_HEADERS).json()["maxActiveSessions"] == 3
        client.put("/api/settings", headers=IDENTITY_HEADERS, json={"thinkingCollapsed": False})
        assert client.get("/api/settings", headers=IDENTITY_HEADERS).json()["maxActiveSessions"] == 3
        resp = client.put("/api/settings", headers=IDENTITY_HEADERS, json={"maxActiveSessions": None})
        assert resp.status_code == 200, resp.text
        assert resp.json()["maxActiveSessions"] is None
```

Append after the last `maxActiveSessions` test:

```python
# ---------------------------------------------------------------------------
# effectiveDefaults.globalCap (spec 2026-09-20 D3)
# ---------------------------------------------------------------------------


def test_effective_global_cap_derives_from_enabled_host_caps():
    with _client() as client:
        gc = client.get("/api/settings", headers=IDENTITY_HEADERS).json()["effectiveDefaults"]["globalCap"]
        assert gc == {"physical": None, "attention": None, "effective": 5, "binding": "default", "hosts": []}

        for name, cap in (("dobby", 4), ("rocky", 6)):
            r = client.post("/api/fleet/hosts", headers=IDENTITY_HEADERS,
                            json={"name": name, "endpoint": f"https://{name}:9443", "maxSessions": cap})
            assert r.status_code == 201, r.text
        gc = client.get("/api/settings", headers=IDENTITY_HEADERS).json()["effectiveDefaults"]["globalCap"]
        assert gc["physical"] == 10 and gc["effective"] == 10 and gc["binding"] == "physical"
        assert gc["hosts"] == [{"name": "dobby", "cap": 4}, {"name": "rocky", "cap": 6}]

        client.put("/api/settings", headers=IDENTITY_HEADERS, json={"maxActiveSessions": 6})
        gc = client.get("/api/settings", headers=IDENTITY_HEADERS).json()["effectiveDefaults"]["globalCap"]
        assert (gc["attention"], gc["effective"], gc["binding"]) == (6, 6, "attention")


def test_managed_lists_the_capacity_env_flags():
    with _client() as client:
        keys = {m["key"] for m in client.get("/api/settings", headers=IDENTITY_HEADERS).json()["managed"]}
    for env in ("CCSWITCH_TEST_SLOT_CAP", "CCSWITCH_CHILD_SESSION_CAP",
                "CCSWITCH_STT_MAX_CONCURRENT", "VIBERPIT_CAPACITY_GOVERNOR_ENABLED"):
        assert env in keys, env
```

Also check nothing imports the function this task deletes: `grep -n "_cap_raised" server/tests/` — expected: no matches. (If there are, delete those tests: the function is replaced by `drain_if_raised`, which Task 1 tests.)

- [ ] **Step 2: Run to verify the new tests fail**

Run: `env -i HOME=/tmp/viberpit-test-home PATH="$PATH" CCSWITCH_DEV=1 PYTHONPATH=$PWD/src /home/tony/GitHub/viberpit-deploy/.venv/bin/python -m pytest server/tests/test_settings.py -q -k "max_active or global_cap or managed_lists"`
Expected: `below_one_is_422` fails (200 with 1), `null_clears` fails (still 3), `derives_from_enabled_host_caps` fails (`KeyError: 'globalCap'`), `managed_lists` fails on `CCSWITCH_TEST_SLOT_CAP`.

- [ ] **Step 3: Rewrite the settings module's cap handling**

In `server/api/settings.py`:

(a) Imports — replace `from server.db.models import AppSettings, DEFAULT_MAX_ACTIVE_SESSIONS` with:

```python
from server.db.models import AppSettings
from server.sessions.global_cap import GlobalCap, drain_if_raised, read_global_cap
```

(b) `_MANAGED` — add three rows directly after the `capacity_governor_enabled` row:

```python
    ("test_slot_cap", "CCSWITCH_TEST_SLOT_CAP", "Full test-suite runs one host may carry at once"),
    ("child_session_cap", "CCSWITCH_CHILD_SESSION_CAP", "Live child sessions one parent session may hold"),
    ("stt_max_concurrent", "CCSWITCH_STT_MAX_CONCURRENT", "Concurrent server-side dictation streams"),
```

(c) `_server_blocks` takes the cap. Change its signature to `def _server_blocks(cap: GlobalCap) -> dict[str, object]:` and replace the line `"maxActiveSessions": DEFAULT_MAX_ACTIVE_SESSIONS,` with `"globalCap": cap.to_wire(),`.

(d) Delete `_cap_raised` entirely (lines 143–150).

(e) `_to_dict` takes the cap: `def _to_dict(s: AppSettings, cap: GlobalCap) -> dict:` and its last line becomes `**_server_blocks(cap),`. Update the `maxActiveSessions` line's neighbour comment: replace `"maxActiveSessions": s.max_active_sessions,` with

```python
        # The ATTENTION ceiling (spec 2026-09-20 D3); the effective cap is
        # effectiveDefaults.globalCap.effective.
        "maxActiveSessions": s.max_active_sessions,
```

(f) `SettingsBody.maxActiveSessions` comment — replace the two comment lines above it with:

```python
    # Attention ceiling. Omitted → unchanged; explicit null → cleared (no attention
    # ceiling, the physical ceiling is in charge); >=1 → that ceiling; <1 → 422.
```

(g) GET handler:

```python
@router.get("")
async def get_settings_row(_identity: str = Depends(current_identity), db=Depends(get_db)) -> dict:
    row = await _get_or_create(db)
    return _to_dict(row, await read_global_cap(db))
```

(h) PUT handler — replace the whole `if body.maxActiveSessions is not None:` block (lines 225–239) with:

```python
    if "maxActiveSessions" in body.model_fields_set:
        if body.maxActiveSessions is not None and body.maxActiveSessions < 1:
            raise HTTPException(status_code=422, detail="maxActiveSessions must be >= 1 or null")
        before = await read_global_cap(db)
        row.max_active_sessions = body.maxActiveSessions
        await db.flush()
        # The EFFECTIVE cap rose (not merely the typed number): fill the new slots.
        drain_if_raised(getattr(request.app.state, "sessions", None), before, await read_global_cap(db))
```

and change the handler's final line to:

```python
    await db.flush()
    return _to_dict(row, await read_global_cap(db))
```

Remove the now-unused `import asyncio` at the top if nothing else in the file uses it (`grep -n "asyncio" server/api/settings.py` — expected: only the import line; delete it).

- [ ] **Step 4: Run the settings suite and the reader test**

Run: `env -i HOME=/tmp/viberpit-test-home PATH="$PATH" CCSWITCH_DEV=1 PYTHONPATH=$PWD/src /home/tony/GitHub/viberpit-deploy/.venv/bin/python -m pytest server/tests/test_settings.py server/tests/test_settings_assistant.py server/tests/test_settings_model_approval.py server/tests/test_global_cap.py -q`
Expected: all green except `test_no_other_reader…`, whose offender list is now only `server/automations/capacity_wiring.py: max_active_sessions` (a comment; Task 5 removes it).

- [ ] **Step 5: Commit**

```bash
git add server/api/settings.py server/tests/test_settings.py
printf '%s\n' 'feat(settings): effectiveDefaults.globalCap — physical, attention, effective, binding' '' 'maxActiveSessions is the attention ceiling: explicit null clears it, <1 is 422 (no silent clamp); the queue drains when the EFFECTIVE cap rises. _MANAGED gains the three capacity env flags (spec 2026-09-20 D3, D9).' '' 'Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>' > /tmp/pr1-t3.txt
git commit -F /tmp/pr1-t3.txt
```

---

### Task 4: Fleet API — a host cap change can raise the effective cap

**Files:**
- Modify: `server/api/fleet.py:176-200` (`create_host`), `:203-214` (`delete_host`), `:225-242` (`update_host`)
- Test: `server/tests/test_fleet_api.py`

**Interfaces:**
- Consumes: `read_global_cap(db)`, `drain_if_raised(orch, before, after)`.
- Produces: no wire change. Each of the three handlers gains `request: Request`.

- [ ] **Step 1: Write the failing test**

Append to `server/tests/test_fleet_api.py`:

```python
def test_raising_a_host_cap_drains_the_session_queue():
    """Physical ceiling = Σ host caps, so a host cap edit is a global-cap edit and must
    fill freed slots exactly as a settings PUT does (spec 2026-09-20 D3)."""
    from unittest.mock import AsyncMock, patch

    from server.tests.test_settings import IDENTITY_HEADERS, _client

    with _client() as client:
        r = client.post("/api/fleet/hosts", headers=IDENTITY_HEADERS,
                        json={"name": "rocky", "endpoint": "https://rocky:9443", "maxSessions": 4})
        assert r.status_code == 201, r.text
        with patch.object(client.app.state.sessions, "maybe_start_queued", new_callable=AsyncMock) as drain:
            client.put("/api/fleet/hosts/rocky", headers=IDENTITY_HEADERS, json={"maxSessions": 8})
            assert drain.await_count == 1
            client.put("/api/fleet/hosts/rocky", headers=IDENTITY_HEADERS, json={"maxSessions": 6})
            assert drain.await_count == 1, "lowering a cap frees nothing"
            client.put("/api/fleet/hosts/rocky", headers=IDENTITY_HEADERS, json={"enabled": False})
            assert drain.await_count == 1
            client.put("/api/fleet/hosts/rocky", headers=IDENTITY_HEADERS, json={"enabled": True})
            assert drain.await_count == 2, "re-enabling a capped host restores its slots"
```

- [ ] **Step 2: Run it to verify it fails**

Run: `env -i HOME=/tmp/viberpit-test-home PATH="$PATH" CCSWITCH_DEV=1 PYTHONPATH=$PWD/src /home/tony/GitHub/viberpit-deploy/.venv/bin/python -m pytest server/tests/test_fleet_api.py -q -k drains`
Expected: FAIL `assert 0 == 1`.

- [ ] **Step 3: Thread the drain through the three handlers**

In `server/api/fleet.py`:

(a) Add to the imports: `from fastapi import Request` (extend the existing `from fastapi import …` line) and `from server.sessions.global_cap import drain_if_raised, read_global_cap`.

(b) `create_host`: signature becomes `async def create_host(body: HostCreate, request: Request, _identity: str = Depends(current_identity)) -> dict:`. Inside the `async with db_session() as db:` block, insert `before = await read_global_cap(db)` as the first line, and after `wire = host_to_wire(host)` add `after = await read_global_cap(db)`. After the `with` block, before `return wire`, add `drain_if_raised(getattr(request.app.state, "sessions", None), before, after)`.

(c) `delete_host`: signature becomes `async def delete_host(name: str, request: Request, _identity: str = Depends(current_identity)) -> None:`. Deleting a host can only lower or leave the physical ceiling, so no drain is needed — add a one-line comment inside the handler: `# Removing a host never raises the effective cap; no queue drain (global_cap.drain_if_raised).` and leave the body otherwise unchanged. (The `request` parameter is still added so the three handlers have one shape.)

(d) `update_host`: signature becomes `async def update_host(name: str, body: HostPatch, request: Request, _identity: str = Depends(current_identity)) -> dict:`. Replace its `async with db_session() as db:` block with:

```python
    async with db_session() as db:
        before = await read_global_cap(db)
        await db.execute(sa_update(Host).where(Host.name == name).values(**values))
        row = (await db.execute(
            sa_select(Host).where(Host.name == name))).scalars().first()
        after = await read_global_cap(db)
```

and, after the 404 guard, before `return host_to_wire(row)`:

```python
    drain_if_raised(getattr(request.app.state, "sessions", None), before, after)
```

- [ ] **Step 4: Run the fleet suite**

Run: `env -i HOME=/tmp/viberpit-test-home PATH="$PATH" CCSWITCH_DEV=1 PYTHONPATH=$PWD/src /home/tony/GitHub/viberpit-deploy/.venv/bin/python -m pytest server/tests/test_fleet_api.py server/tests/test_settings.py -q`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add server/api/fleet.py server/tests/test_fleet_api.py
printf '%s\n' 'feat(fleet): a host cap or enable change drains the queue when the effective cap rises' '' 'Σ host caps is the physical ceiling (spec 2026-09-20 D3), so PUT /fleet/hosts is a global-cap edit.' '' 'Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>' > /tmp/pr1-t4.txt
git commit -F /tmp/pr1-t4.txt
```

---

### Task 5: Readout — `global.{physical, attention, binding}`

**Files:**
- Modify: `server/automations/capacity_readout.py:61-75` (`CapacitySources`), `:222-224` (`global` row)
- Modify: `server/automations/capacity_wiring.py:96-102`
- Test: `server/tests/test_capacity_readout.py`

**Interfaces:**
- Consumes: `read_global_cap(db).to_wire()`.
- Produces: `CapacitySources.global_limits: Callable[[], Awaitable[dict | None]] | None = None` (optional, last field); `GET /api/capacity` → `global: {cap, active, free, physical, attention, binding}`.

- [ ] **Step 1: Write the failing tests**

Append to `server/tests/test_capacity_readout.py`:

```python
def test_global_row_carries_the_two_ceilings_when_the_seam_is_wired():
    """spec 2026-09-20 D3: the stack's Global row says which ceiling binds."""
    async def limits():
        return {"physical": 10, "attention": 6, "effective": 6, "binding": "attention",
                "hosts": [{"name": "dobby", "cap": 4}, {"name": "rocky", "cap": 6}]}

    src = _make_sources(global_cap=6)
    src.global_limits = limits
    r = asyncio.run(build_readout(src))
    assert r["global"] == {"cap": 6, "active": 2, "free": 4,
                           "physical": 10, "attention": 6, "binding": "attention"}


def test_global_row_degrades_to_nulls_without_the_seam():
    r = asyncio.run(build_readout(_make_sources(global_cap=6)))
    assert r["global"]["physical"] is None and r["global"]["attention"] is None
    assert r["global"]["binding"] is None
```

- [ ] **Step 2: Run to verify they fail**

Run: `env -i HOME=/tmp/viberpit-test-home PATH="$PATH" CCSWITCH_DEV=1 PYTHONPATH=$PWD/src /home/tony/GitHub/viberpit-deploy/.venv/bin/python -m pytest server/tests/test_capacity_readout.py -q -k global_row`
Expected: first fails with `AttributeError`/`FrozenInstanceError`-free `assert … == …` mismatch or `TypeError` on attribute set (the dataclass has no `global_limits`); second fails with `KeyError: 'physical'`.

- [ ] **Step 3: Add the seam and the fields**

In `server/automations/capacity_readout.py`, append to `CapacitySources` (after `kill_switch`):

```python
    # Optional (spec 2026-09-20 D3): the derivation behind ``global_cap`` —
    # ``GlobalCap.to_wire()`` — so the Global row can say which ceiling binds.
    # None (older callers / pure tests) degrades the three fields to null.
    global_limits: Callable[[], Awaitable[dict | None]] | None = None
```

In `build_readout`, directly after `global_active = src.global_active()` add:

```python
    limits = (await src.global_limits()) if src.global_limits is not None else None
```

and replace the `"global": {...}` line in the return dict with:

```python
        "global": {"cap": global_cap, "active": global_active, "free": global_free,
                   "physical": limits.get("physical") if limits else None,
                   "attention": limits.get("attention") if limits else None,
                   "binding": limits.get("binding") if limits else None},
```

In `server/automations/capacity_wiring.py`: add `from server.sessions.global_cap import read_global_cap` to the imports; add inside `sources_for`, after `session_status`:

```python
    async def global_limits() -> dict:
        async with db_session() as db:
            return (await read_global_cap(db)).to_wire()
```

and in the `CapacitySources(...)` call replace the `global_cap=orch._max_active_sessions,` line and its trailing comment block with:

```python
        # `global_cap` is the same effective value the admission gate compares
        # against (orchestrator._max_active_sessions → global_cap.read_global_cap);
        # `global_limits` is that value's derivation for the Global row.
        global_cap=orch._max_active_sessions,
        global_limits=global_limits,
```

Keep the existing `global_active=orch._active_session_count,` line and its comment, but edit that comment's last line so it no longer spells the column name: change `admission gate itself compares against (orchestrator.py ~2902/3305/3531),` to `admission gate itself compares against (the three `_active_session_count()` sites in orchestrator.py),`.

- [ ] **Step 4: Run the readout, API and reader tests**

Run: `env -i HOME=/tmp/viberpit-test-home PATH="$PATH" CCSWITCH_DEV=1 PYTHONPATH=$PWD/src /home/tony/GitHub/viberpit-deploy/.venv/bin/python -m pytest server/tests/test_capacity_readout.py server/tests/test_capacity_api.py server/tests/test_capacity_governor_loop.py server/tests/test_global_cap.py -q`
Expected: all PASS, including `test_no_other_reader_of_max_active_sessions` (offender list empty).

- [ ] **Step 5: Commit**

```bash
git add server/automations/capacity_readout.py server/automations/capacity_wiring.py server/tests/test_capacity_readout.py
printf '%s\n' 'feat(capacity): readout Global row carries physical, attention and binding' '' 'New optional CapacitySources.global_limits seam wired to global_cap.read_global_cap (spec 2026-09-20 D3). The no-other-reader guard is now green.' '' 'Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>' > /tmp/pr1-t5.txt
git commit -F /tmp/pr1-t5.txt
```

---

### Task 6: Web types, factory and cache invalidation

**Files:**
- Modify: `web/src/api/hooks.ts:320-327` (`EffectiveDefaults`), `:340-341` (`maxActiveSessions` comment), `:372-378` (`useUpdateSettings`), `:656-673` (`useUpdateAccount`, `useSetAccountEnabled`), `:1158-1164` (`useFleetHostPatch`)
- Modify: `web/src/api/capacity.ts:111`
- Modify: `web/src/test/factories.ts:176-179`
- Modify: `web/src/features/settings/sections/FleetSection.tsx:101-125` (add/remove mutations), `:5` (import)
- Modify: `web/src/features/settings/sections/GeneralSection.test.tsx:43,111`, `web/src/features/settings/sections/SessionDefaultsSection.test.tsx:94`, `web/src/api/__tests__/hooks.settings.test.ts:70-76`, `web/src/features/delivery/capacity/capacityStack.test.ts:11`
- Test: `web/src/api/__tests__/hooks.settings.test.ts`

**Interfaces:**
- Produces: `export interface GlobalCapDefaults { physical: number | null; attention: number | null; effective: number; binding: 'physical' | 'attention' | 'default'; hosts: { name: string; cap: number | null }[] }`; `EffectiveDefaults.globalCap: GlobalCapDefaults` (and no `maxActiveSessions`); `CapacityReadout['global']` gains `physical: number | null; attention: number | null; binding: 'physical' | 'attention' | 'default' | null`.

- [ ] **Step 1: Link node_modules and write the failing test**

Run: `[ -e web/node_modules ] || ln -s /home/tony/GitHub/viberpit-deploy/web/node_modules web/node_modules`

In `web/src/api/__tests__/hooks.settings.test.ts`, change `makeWrapper` to accept a client, and add the invalidation test. Replace the `makeWrapper` function with:

```ts
function makeWrapper(qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })) {
  return ({ children }: { children: React.ReactNode }) =>
    React.createElement(QueryClientProvider, { client: qc }, children)
}
```

Update the fixture at lines 70–76: replace `maxActiveSessions: 5,` with `globalCap: { physical: null, attention: null, effective: 5, binding: 'default', hosts: [] },`.

Add `QK` to the hooks import (`import { QK, useSettings, useUpdateSettings } from '../hooks'`) and `act` to the testing-library import (`import { act, renderHook, waitFor } from '@testing-library/react'`). Append inside the outer `describe`:

```ts
  it('a settings save invalidates the capacity readout too (spec 2026-09-20 C20)', async () => {
    vi.mocked(api.put).mockResolvedValueOnce(appSettings())
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const spy = vi.spyOn(qc, 'invalidateQueries')
    const { result } = renderHook(() => useUpdateSettings(), { wrapper: makeWrapper(qc) })
    await act(async () => { await result.current.mutateAsync({ maxActiveSessions: 3 }) })
    expect(spy).toHaveBeenCalledWith({ queryKey: QK.settings })
    expect(spy).toHaveBeenCalledWith({ queryKey: QK.capacity })
  })
```

- [ ] **Step 2: Run to verify it fails**

Run: `(cd web && npx vitest run src/api/__tests__/hooks.settings.test.ts)`
Expected: the new test FAILS on the `QK.capacity` expectation (and a type error on `globalCap` if vitest typechecks — it does not; `npm run typecheck` in Task 14 will).

- [ ] **Step 3: Types**

In `web/src/api/hooks.ts`, replace the `EffectiveDefaults` interface with:

```ts
/** The effective global cap and its derivation (spec 2026-09-20 D3). */
export interface GlobalCapDefaults {
  /** Σ host caps over enabled hosts; null = unbounded (no hosts, or an enabled host has no cap). */
  physical: number | null
  /** AppSettings.maxActiveSessions as stored; null = no attention ceiling. */
  attention: number | null
  /** min(physical, attention), or the server default (5) when both are unbounded. Always ≥ 1. */
  effective: number
  binding: 'physical' | 'attention' | 'default'
  hosts: { name: string; cap: number | null }[]
}

/** What the server falls back to when the matching AppSettings column is NULL. */
export interface EffectiveDefaults {
  repoRoot: string
  archiveHome: string | null
  modelApprovalAction: string
  modelApprovalDenylist: string
  globalCap: GlobalCapDefaults
}
```

Replace the `AppSettings.maxActiveSessions` comment (`// Maximum concurrent active sessions; sessions beyond this are queued. null = server default (5).`) with `// The ATTENTION ceiling (spec 2026-09-20 D3); null = none. The effective cap is effectiveDefaults.globalCap.`

In `web/src/api/capacity.ts`, replace line 111 with:

```ts
  global: {
    cap: number; active: number; free: number
    /** The derivation (spec 2026-09-20 D3); null when the server has no seam wired. */
    physical: number | null; attention: number | null
    binding: 'physical' | 'attention' | 'default' | null
  }
```

- [ ] **Step 4: Invalidation**

In `web/src/api/hooks.ts`:
- `useUpdateSettings`: `onSuccess: () => { qc.invalidateQueries({ queryKey: QK.settings }); qc.invalidateQueries({ queryKey: QK.capacity }) },`
- `useUpdateAccount` and `useSetAccountEnabled`: each `onSuccess` becomes `onSuccess: () => { qc.invalidateQueries({ queryKey: QK.accounts }); qc.invalidateQueries({ queryKey: QK.capacity }) },`
- `useFleetHostPatch`: `onSuccess: () => { void qc.invalidateQueries({ queryKey: FLEET_HOSTS_KEY }); void qc.invalidateQueries({ queryKey: QK.capacity }) },`

Add a comment above `useUpdateSettings`:

```ts
// Every mutation that moves a capacity dial (settings, hosts, accounts) also
// invalidates QK.capacity: the Delivery → Capacity stack and the Limits
// section's readout card otherwise show the old ceiling for up to 30 s
// (spec 2026-09-20 C20, D10).
```

In `web/src/features/settings/sections/FleetSection.tsx`: change the hooks import to `import { QK, usePendingDeploy, useFleetHostPatch } from '@/api/hooks'`, and in both the `add` and `remove` mutations' `onSuccess` add `void queryClient.invalidateQueries({ queryKey: QK.capacity })` after the `FLEET_HOSTS_KEY` invalidation.

- [ ] **Step 5: Fixtures**

- `web/src/test/factories.ts:176-179` → 
  ```ts
      effectiveDefaults: {
        repoRoot: '/home/user/GitHub', archiveHome: null,
        modelApprovalAction: 'require_approval', modelApprovalDenylist: 'fable',
        globalCap: { physical: null, attention: null, effective: 5, binding: 'default', hosts: [] },
      },
  ```
- `GeneralSection.test.tsx:43` and `:111`, `SessionDefaultsSection.test.tsx:94`: replace `maxActiveSessions: 5` with `globalCap: { physical: null, attention: null, effective: 5, binding: 'default', hosts: [] }`.
- `capacityStack.test.ts:11`: `global: { cap: 20, active: 10, free: 10, physical: null, attention: null, binding: null },`

- [ ] **Step 6: Run**

Run: `(cd web && npx vitest run src/api/__tests__/hooks.settings.test.ts src/features/settings/sections/GeneralSection.test.tsx src/features/settings/sections/SessionDefaultsSection.test.tsx src/features/delivery/capacity/capacityStack.test.ts src/features/settings/sections/FleetSection.test.tsx && npm run typecheck)`
Expected: all PASS; typecheck clean.

- [ ] **Step 7: Commit**

```bash
git add web/src/api/hooks.ts web/src/api/capacity.ts web/src/test/factories.ts web/src/features/settings/sections/FleetSection.tsx web/src/api/__tests__/hooks.settings.test.ts web/src/features/settings/sections/GeneralSection.test.tsx web/src/features/settings/sections/SessionDefaultsSection.test.tsx web/src/features/delivery/capacity/capacityStack.test.ts
printf '%s\n' 'feat(web): globalCap types; every capacity dial mutation invalidates the readout' '' 'effectiveDefaults.globalCap replaces maxActiveSessions; settings, host and account mutations invalidate QK.capacity (spec 2026-09-20 C20, D10).' '' 'Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>' > /tmp/pr1-t6.txt
git commit -F /tmp/pr1-t6.txt
```

---

### Task 7: `ManagedEnvTable` — one env table for About and Limits

**Files:**
- Create: `web/src/features/settings/chassis/ManagedEnvTable.tsx`
- Modify: `web/src/features/settings/sections/AboutSection.tsx:1-12`, `:40-62`
- Test: `web/src/features/settings/chassis/ManagedEnvTable.test.tsx`

**Interfaces:**
- Produces: `export function ManagedEnvTable({ flags, keys }: { flags: ManagedFlag[]; keys?: readonly string[] })` — renders the table; when `keys` is given, only flags whose `key` is in it, in `keys` order; `export function showManagedValue(v: ManagedFlag['value']): string`.

- [ ] **Step 1: Write the failing test**

```tsx
// web/src/features/settings/chassis/ManagedEnvTable.test.tsx
import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { ManagedEnvTable, showManagedValue } from './ManagedEnvTable'

const FLAGS = [
  { key: 'CCSWITCH_ENABLE_COPILOT', value: false, description: 'Enable GitHub Copilot accounts' },
  { key: 'CCSWITCH_TEST_SLOT_CAP', value: 1, description: 'Full test-suite runs one host may carry at once' },
  { key: 'CCSWITCH_CHILD_SESSION_CAP', value: 3, description: 'Live child sessions one parent may hold' },
]

describe('ManagedEnvTable', () => {
  it('renders every flag when no key filter is given', () => {
    render(<ManagedEnvTable flags={FLAGS} />)
    expect(screen.getAllByRole('row')).toHaveLength(4) // header + 3
    expect(screen.getByRole('rowheader', { name: 'CCSWITCH_ENABLE_COPILOT' })).toBeInTheDocument()
  })

  it('filters to the given keys, in the given order', () => {
    render(<ManagedEnvTable flags={FLAGS} keys={['CCSWITCH_CHILD_SESSION_CAP', 'CCSWITCH_TEST_SLOT_CAP', 'NOT_SENT']} />)
    const names = screen.getAllByRole('rowheader').map((th) => th.textContent)
    expect(names).toEqual(['CCSWITCH_CHILD_SESSION_CAP', 'CCSWITCH_TEST_SLOT_CAP'])
  })

  it('shows booleans as on/off and null as unset', () => {
    expect(showManagedValue(true)).toBe('on')
    expect(showManagedValue(false)).toBe('off')
    expect(showManagedValue(null)).toBe('unset')
    expect(showManagedValue(3)).toBe('3')
  })
})
```

- [ ] **Step 2: Run to verify it fails**

Run: `(cd web && npx vitest run src/features/settings/chassis/ManagedEnvTable.test.tsx)`
Expected: FAIL, cannot resolve `./ManagedEnvTable`.

- [ ] **Step 3: Extract the table**

```tsx
// web/src/features/settings/chassis/ManagedEnvTable.tsx
import type { ManagedFlag } from '@/api/hooks'

export function showManagedValue(v: ManagedFlag['value']): string {
  if (v === null) return 'unset'
  if (typeof v === 'boolean') return v ? 'on' : 'off'
  return String(v)
}

/**
 * Read-only env flags (spec 2026-09-04 S30). About shows the whole allowlist;
 * Limits shows the capacity subset (spec 2026-09-20 D9) — one table, one
 * rendering, so a flag never looks different on the two pages.
 */
export function ManagedEnvTable({ flags, keys }: { flags: ManagedFlag[]; keys?: readonly string[] }) {
  const rows = keys
    ? keys.map((k) => flags.find((f) => f.key === k)).filter((f): f is ManagedFlag => f !== undefined)
    : flags
  return (
    <div className="overflow-x-auto py-2">
      <table className="w-full text-sm">
        <thead>
          <tr className="text-left text-xs uppercase tracking-wide text-ink-muted">
            <th scope="col" className="py-1 pr-4 font-medium">Variable</th>
            <th scope="col" className="py-1 pr-4 font-medium">Value</th>
            <th scope="col" className="py-1 font-medium">What it does</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((m) => (
            <tr key={m.key} className="border-t border-edge">
              <th scope="row" className="py-1.5 pr-4 font-mono text-xs font-normal text-ink">{m.key}</th>
              <td className="py-1.5 pr-4 font-mono text-xs">{showManagedValue(m.value)}</td>
              <td className="py-1.5 text-ink-muted">{m.description}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
```

In `web/src/features/settings/sections/AboutSection.tsx`: delete the local `showValue` function and the `type ManagedFlag` import (keep `useSettings`); add `import { ManagedEnvTable } from '../chassis/ManagedEnvTable'`; replace the whole `<div className="overflow-x-auto py-2">…</div>` block inside the "Managed by env" `SettingsGroup` with `<ManagedEnvTable flags={settings.managed} />`.

- [ ] **Step 4: Run**

Run: `(cd web && npx vitest run src/features/settings/chassis/ManagedEnvTable.test.tsx src/features/settings/sections/AboutSection.test.tsx)`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add web/src/features/settings/chassis/ManagedEnvTable.tsx web/src/features/settings/chassis/ManagedEnvTable.test.tsx web/src/features/settings/sections/AboutSection.tsx
printf '%s\n' 'refactor(web): extract ManagedEnvTable from About so Limits can share it' '' 'Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>' > /tmp/pr1-t7.txt
git commit -F /tmp/pr1-t7.txt
```

---

### Task 8: `LimitsSection` — Settings › Capacity › Limits

**Files:**
- Create: `web/src/features/settings/sections/LimitsSection.tsx`
- Test: `web/src/features/settings/sections/LimitsSection.test.tsx`

**Interfaces:**
- Consumes: `useSettings`, `useUpdateSettings`, `useCapacity` from `@/api/hooks`; `buildCapacityStack` from `@/features/delivery/capacity/capacityStack`; `ManagedEnvTable`; `settingsSectionPath` from `@/shell/navDefs`; `deliveryPath` from `@/features/delivery/deliveryNavState`.
- Produces: `export function LimitsSection()`; `export const CAPACITY_MANAGED_KEYS`; `export function physicalLine(gc)`, `export function effectiveLine(gc)` (pure, tested). Registered in Task 9 as section id `capacity` — until then the section is not routed, which is why its test mounts it directly.

- [ ] **Step 1: Write the failing test**

```tsx
// web/src/features/settings/sections/LimitsSection.test.tsx
/**
 * Settings › Capacity › Limits (spec 2026-09-20 §5.1). Hooks are mocked, as in
 * GeneralSection.test.tsx: this suite is about the rows, the payload and the
 * readout card, not the transport.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { axe, toHaveNoViolations } from 'jest-axe'
import { createMemoryRouter, RouterProvider } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import * as hooks from '@/api/hooks'
import type { GlobalCapDefaults } from '@/api/hooks'
import { appSettings } from '@/test/factories'
import { queryResult, idleQueryResult, mutationResult } from '@/test/reactQuery'
import { LimitsSection, effectiveLine, physicalLine } from './LimitsSection'

expect.extend(toHaveNoViolations)

vi.mock('@/api/hooks', () => ({
  useSettings: vi.fn(),
  useUpdateSettings: vi.fn(),
  useCapacity: vi.fn(),
}))

const TWO_HOSTS: GlobalCapDefaults = {
  physical: 10, attention: null, effective: 10, binding: 'physical',
  hosts: [{ name: 'dobby', cap: 4 }, { name: 'rocky', cap: 6 }],
}

function mount() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const router = createMemoryRouter([{ path: '/', element: <LimitsSection /> }])
  return render(<QueryClientProvider client={qc}><RouterProvider router={router} /></QueryClientProvider>)
}

describe('physicalLine / effectiveLine', () => {
  it('names the hosts behind the physical ceiling', () => {
    expect(physicalLine(TWO_HOSTS)).toBe('10 — dobby 4 · rocky 6')
  })
  it('names the uncapped host when unbounded', () => {
    expect(physicalLine({ ...TWO_HOSTS, physical: null, hosts: [{ name: 'dobby', cap: 4 }, { name: 'rocky', cap: null }] }))
      .toBe('unbounded — rocky has no cap')
    expect(physicalLine({ ...TWO_HOSTS, physical: null, hosts: [] })).toBe('unbounded — no hosts registered')
  })
  it('says which ceiling binds', () => {
    expect(effectiveLine(TWO_HOSTS)).toBe('10 — the physical ceiling binds')
    expect(effectiveLine({ ...TWO_HOSTS, attention: 6, effective: 6, binding: 'attention' }))
      .toBe('6 — the attention ceiling binds')
    expect(effectiveLine({ ...TWO_HOSTS, attention: 10, effective: 10, binding: 'attention' }))
      .toBe('10 — the attention ceiling binds (equal to the physical ceiling)')
    expect(effectiveLine({ physical: null, attention: null, effective: 5, binding: 'default', hosts: [] }))
      .toBe('5 — default; set a host cap on Hosts or an attention ceiling')
  })
})

describe('LimitsSection', () => {
  const mutateAsync = vi.fn().mockResolvedValue({})
  beforeEach(() => {
    mutateAsync.mockClear()
    vi.mocked(hooks.useSettings).mockReturnValue(queryResult(appSettings({
      maxActiveSessions: null,
      managed: [
        { key: 'CCSWITCH_ENABLE_COPILOT', value: false, description: 'Enable GitHub Copilot accounts' },
        { key: 'CCSWITCH_TEST_SLOT_CAP', value: 1, description: 'Full test-suite runs one host may carry at once' },
        { key: 'VIBERPIT_CAPACITY_GOVERNOR_ENABLED', value: true, description: 'Capacity governor may apply its plan (act mode)' },
      ],
      effectiveDefaults: {
        repoRoot: '/r', archiveHome: null, modelApprovalAction: 'require_approval', modelApprovalDenylist: 'fable',
        globalCap: TWO_HOSTS,
      },
    })))
    vi.mocked(hooks.useUpdateSettings).mockReturnValue(mutationResult({ mutateAsync }))
    vi.mocked(hooks.useCapacity).mockReturnValue(idleQueryResult())
  })

  it('shows the three global-cap rows and the physical value as the attention placeholder', async () => {
    const { container } = mount()
    expect(screen.getByRole('heading', { level: 2, name: 'Limits' })).toBeInTheDocument()
    expect(screen.getByText('10 — dobby 4 · rocky 6')).toBeInTheDocument()
    expect(screen.getByLabelText('Attention ceiling')).toHaveAttribute('placeholder', 'None · physical ceiling 10 applies')
    expect(screen.getByText('10 — the physical ceiling binds')).toBeInTheDocument()
    expect(await axe(container)).toHaveNoViolations()
  })

  it('saves the attention ceiling as maxActiveSessions and blank as null', async () => {
    mount()
    await userEvent.type(screen.getByLabelText('Attention ceiling'), '6')
    await userEvent.click(screen.getByRole('button', { name: 'Save' }))
    expect(mutateAsync).toHaveBeenCalledWith({ maxActiveSessions: 6 })
    await userEvent.clear(screen.getByLabelText('Attention ceiling'))
    await userEvent.click(screen.getByRole('button', { name: 'Save' }))
    expect(mutateAsync).toHaveBeenLastCalledWith({ maxActiveSessions: null })
  })

  it('links "unbounded" to Hosts', () => {
    vi.mocked(hooks.useSettings).mockReturnValue(queryResult(appSettings({
      effectiveDefaults: {
        repoRoot: '/r', archiveHome: null, modelApprovalAction: 'require_approval', modelApprovalDenylist: 'fable',
        globalCap: { physical: null, attention: null, effective: 5, binding: 'default', hosts: [{ name: 'rocky', cap: null }] },
      },
    })))
    mount()
    expect(screen.getByRole('link', { name: 'Set a cap on Hosts' })).toHaveAttribute('href', '/settings/fleet')
  })

  it('shows only the capacity env flags, and the live readout card when the readout loads', () => {
    vi.mocked(hooks.useCapacity).mockReturnValue(queryResult({
      computedAt: '2026-09-20T10:00:00Z', pushLevel: 'steady', governorMode: 'act', killSwitch: true,
      global: { cap: 10, active: 5, free: 5, physical: 10, attention: null, binding: 'physical' },
      hosts: [], accounts: { available: 4, total: 7, reservePct: 20, nextResetAt: null, rows: [] },
      runner: { ceiling: 6, effective: 6, inFlight: 4, derived: null },
      products: [], binding: { layer: 'product', code: 'product_capacity_full', text: 'ghostdriver: The product capacity limit is reached (1/1).' },
      defects: [], plan: null, planLive: false,
    } as never))
    mount()
    const names = screen.getAllByRole('rowheader').map((th) => th.textContent)
    expect(names).toEqual(['CCSWITCH_TEST_SLOT_CAP', 'VIBERPIT_CAPACITY_GOVERNOR_ENABLED'])
    expect(screen.getByText('Global cap')).toBeInTheDocument()
    expect(screen.getByText('5 / 10 · 5 free')).toBeInTheDocument()
    expect(screen.getByText(/ghostdriver: The product capacity limit/)).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Open Delivery → Capacity' })).toHaveAttribute('href', '/delivery?scope=capacity')
  })
})
```

- [ ] **Step 2: Run to verify it fails**

Run: `(cd web && npx vitest run src/features/settings/sections/LimitsSection.test.tsx)`
Expected: FAIL, cannot resolve `./LimitsSection`.

- [ ] **Step 3: Write the section**

```tsx
// web/src/features/settings/sections/LimitsSection.tsx
import * as React from 'react'
import { Link } from 'react-router-dom'
import {
  useSettings, useUpdateSettings, useCapacity, type AppSettings, type GlobalCapDefaults,
} from '@/api/hooks'
import { Input } from '@/ui/Input'
import { Banner } from '@/ui/Banner'
import { Skeleton } from '@/ui/Skeleton'
import { settingsSectionPath } from '@/shell/navDefs'
import { deliveryPath } from '@/features/delivery/deliveryNavState'
import { buildCapacityStack } from '@/features/delivery/capacity/capacityStack'
import { SETTINGS_BY_ID } from '../settingsDefs'
import { SettingsSection } from '../chassis/SettingsSection'
import { SettingsGroup } from '../chassis/SettingsGroup'
import { SettingRow } from '../chassis/SettingRow'
import { SaveBar } from '../chassis/SaveBar'
import { ManagedEnvTable } from '../chassis/ManagedEnvTable'
import { useSettingsForm } from '../useSettingsForm'

/** Env-managed ceilings that sit OUTSIDE the five-layer stack (spec 2026-09-20 D9). */
export const CAPACITY_MANAGED_KEYS = [
  'CCSWITCH_TEST_SLOT_CAP',
  'CCSWITCH_CHILD_SESSION_CAP',
  'CCSWITCH_STT_MAX_CONCURRENT',
  'VIBERPIT_CAPACITY_GOVERNOR_ENABLED',
] as const

interface LimitsDraft { attention: string }

function toDraft(s: AppSettings): LimitsDraft {
  return { attention: s.maxActiveSessions != null ? String(s.maxActiveSessions) : '' }
}

/**
 * Blank clears the attention ceiling (the server treats an explicit null as
 * "none"). A value that is not a whole number ≥ 1 is also sent as null rather
 * than becoming 1 — the old form's clamp turned a half-deleted "10" into 1.
 */
function toPayload(d: LimitsDraft): Partial<AppSettings> {
  const n = Number.parseInt(d.attention, 10)
  return { maxActiveSessions: Number.isFinite(n) && n >= 1 ? n : null }
}

export function physicalLine(gc: GlobalCapDefaults): string {
  if (gc.hosts.length === 0) return 'unbounded — no hosts registered'
  const uncapped = gc.hosts.filter((h) => h.cap === null).map((h) => h.name)
  if (uncapped.length > 0) {
    return `unbounded — ${uncapped.join(', ')} ${uncapped.length === 1 ? 'has' : 'have'} no cap`
  }
  return `${gc.physical} — ${gc.hosts.map((h) => `${h.name} ${h.cap}`).join(' · ')}`
}

export function effectiveLine(gc: GlobalCapDefaults): string {
  switch (gc.binding) {
    case 'physical':
      return `${gc.effective} — the physical ceiling binds`
    case 'attention':
      return gc.physical !== null && gc.attention === gc.physical
        ? `${gc.effective} — the attention ceiling binds (equal to the physical ceiling)`
        : `${gc.effective} — the attention ceiling binds`
    default:
      return `${gc.effective} — default; set a host cap on Hosts or an attention ceiling`
  }
}

/**
 * Settings › Capacity › Limits (spec 2026-09-20 §5.1): the global cap as its two
 * ceilings, a read-only copy of the live ceiling stack, and the env-managed
 * ceilings outside the stack. Form save model; the attention ceiling is the
 * only editable field.
 */
export function LimitsSection() {
  const def = SETTINGS_BY_ID.capacity
  const { data: settings, isError, error } = useSettings()
  const update = useUpdateSettings()
  const { data: capacity } = useCapacity()
  const initial = React.useMemo(() => (settings ? toDraft(settings) : undefined), [settings])
  const form = useSettingsForm<LimitsDraft>({ initial, save: (d) => update.mutateAsync(toPayload(d)) })

  if (!settings) {
    return (
      <SettingsSection id="capacity" title={def.label} scope="server" description={def.description}>
        {isError
          ? (
            <Banner tone="danger" role="alert" title="Settings could not be loaded">
              {error?.message ?? 'The server did not answer.'}
            </Banner>
          )
          : <Skeleton lines={4} />}
      </SettingsSection>
    )
  }
  if (!form.draft) return null
  const { draft, set } = form
  const gc = settings.effectiveDefaults.globalCap
  const stack = capacity ? buildCapacityStack(capacity) : null

  return (
    <SettingsSection
      id="capacity"
      title={def.label}
      scope="server"
      description={def.description}
      footer={<SaveBar form={form} />}
    >
      {form.error && <Banner tone="danger" role="alert" title="Not saved">{form.error}</Banner>}

      <SettingsGroup
        legend="Global cap"
        description="Sessions beyond the effective cap queue and start when a slot frees. The cap is the lower of the two ceilings."
      >
        <SettingRow
          label="Physical ceiling"
          htmlFor="limits-physical"
          description="The sum of the host caps on enabled hosts."
          selfLabelled
        >
          <span id="limits-physical" className="text-sm text-ink">
            {physicalLine(gc)}
            {gc.physical === null && (
              <>
                {' '}
                <Link to={settingsSectionPath('fleet')} className="text-accent hover:underline">Set a cap on Hosts</Link>
              </>
            )}
          </span>
        </SettingRow>
        <SettingRow
          label="Attention ceiling"
          htmlFor="limits-attention"
          description="Never run more than this many sessions at once, whatever the hardware could take. Blank leaves the physical ceiling in charge."
        >
          <Input
            id="limits-attention"
            type="number"
            min={1}
            step={1}
            inputMode="numeric"
            placeholder={gc.physical === null ? 'None' : `None · physical ceiling ${gc.physical} applies`}
            value={draft.attention}
            onChange={(e) => set('attention', e.target.value)}
          />
        </SettingRow>
        <SettingRow
          label="Effective global cap"
          htmlFor="limits-effective"
          description="What the admission gate compares against right now."
          selfLabelled
        >
          <span id="limits-effective" className="text-sm font-medium text-ink">{effectiveLine(gc)}</span>
        </SettingRow>
      </SettingsGroup>

      <SettingsGroup
        legend="Live readout"
        description={(
          <>
            The whole ceiling stack as automation sees it.{' '}
            <Link to={deliveryPath('capacity')} className="text-accent hover:underline">Open Delivery → Capacity</Link>
          </>
        )}
      >
        {stack === null
          ? <p className="py-3 text-sm text-ink-muted">Loading the readout…</p>
          : (
            <ul className="divide-y divide-edge">
              {stack.map((row) => (
                <li key={row.key} className="flex items-baseline justify-between gap-3 py-2 text-sm">
                  <span className={row.binding ? 'font-medium text-warning' : 'text-ink'}>{row.label}</span>
                  <span className="tabular-nums text-ink-muted">
                    {`${row.used} / ${row.cap ?? '∞'} · ${row.free ?? '∞'} free`}
                  </span>
                </li>
              ))}
            </ul>
          )}
        {capacity?.binding && (
          <Banner tone="warning" role="status" title="Binding constraint">{capacity.binding.text}</Banner>
        )}
      </SettingsGroup>

      <SettingsGroup
        legend="Managed by env"
        description="Ceilings outside the stack, read at daemon start. Not editable here."
      >
        <ManagedEnvTable flags={settings.managed} keys={CAPACITY_MANAGED_KEYS} />
      </SettingsGroup>
    </SettingsSection>
  )
}
```

Note: `SETTINGS_BY_ID.capacity` does not exist until Task 9 registers the section; typecheck will fail on this file alone until then, but vitest does not typecheck, so the test runs. Task 9 immediately follows.

- [ ] **Step 4: Run**

Run: `(cd web && npx vitest run src/features/settings/sections/LimitsSection.test.tsx)`
Expected: PASS. If `screen.getByText('5 / 10 · 5 free')` fails because the stack renders the text in split nodes, keep the template literal exactly as written above (it is one text node).

- [ ] **Step 5: Commit**

```bash
git add web/src/features/settings/sections/LimitsSection.tsx web/src/features/settings/sections/LimitsSection.test.tsx
printf '%s\n' 'feat(web): Settings › Capacity › Limits — the two ceilings, the live readout, env ceilings' '' 'spec 2026-09-20 §5.1 Limits. Registered in the next commit.' '' 'Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>' > /tmp/pr1-t8.txt
git commit -F /tmp/pr1-t8.txt
```

---

### Task 9: Register the section; rename the group

**Files:**
- Modify: `web/src/features/settings/settingsDefs.ts:27-37`, `:67-87`
- Test: `web/src/features/settings/settingsDefs.test.ts`

**Interfaces:**
- Produces: `SettingsGroupId = 'workspace' | 'sessions' | 'capacity' | 'claude' | 'about'`; section `capacity` (group `capacity`, label `Limits`, `ownsChassis: true`, component `LimitsSection`), listed first in its group; `accounts` and `fleet` re-grouped to `capacity`; `SETTINGS_DEFS.length === 18`.

- [ ] **Step 1: Update the tests**

In `web/src/features/settings/settingsDefs.test.ts`:
- First test: title `'has 18 sections in 5 groups, …'`; `toHaveLength(18)`; groups `['workspace', 'sessions', 'capacity', 'claude', 'about']`.
- `ownsChassis` test: expected `['general', 'appearance', 'sessions', 'capacity', 'deploy', 'about']` (registry order).
- Group-order test: add `expect(sectionsInGroup('capacity').map((d) => d.id)).toEqual(['capacity', 'fleet', 'accounts'])` and `expect(SETTINGS_BY_ID.capacity.scope).toBe('server')`.

- [ ] **Step 2: Run to verify it fails**

Run: `(cd web && npx vitest run src/features/settings/settingsDefs.test.ts)`
Expected: FAIL on length 17 and the group list.

- [ ] **Step 3: Edit the registry**

In `web/src/features/settings/settingsDefs.ts`:
- Import: add `import { LimitsSection } from './sections/LimitsSection'` after the `GeneralSection` import; add `Gauge` to the lucide import list.
- `SettingsGroupId`: `'workspace' | 'sessions' | 'capacity' | 'claude' | 'about'`.
- `SETTINGS_GROUPS`: replace `{ id: 'accounts', label: 'Accounts' }` with `{ id: 'capacity', label: 'Capacity' }`.
- General's description: `'Where repos are cloned and archives are stored, and how many archived sessions are kept.'`
- Replace the `accounts` and `fleet` rows with these three, in this order:

```ts
  // What the fleet can run (spec 2026-09-20 D1/D2): supply, not spend. Delivery →
  // Capacity is the other half — how automation spends it — and the two link.
  { id: 'capacity',      group: 'capacity',  label: 'Limits',              icon: Gauge,             scope: 'server',  component: LimitsSection, ownsChassis: true,
    description: 'The global cap as its two ceilings — what the hosts can run and how much you want running — plus the env-managed limits outside the stack.', keywords: ['global cap', 'attention', 'physical', 'max active sessions'] },
  { id: 'fleet',         group: 'capacity',  label: 'Hosts',               icon: Server,            scope: 'server',  component: FleetSection,
    description: 'Machines that can run sessions: each host’s cap, dispatch weight and health.' },
  { id: 'accounts',      group: 'capacity',  label: 'Accounts',            icon: Users,             scope: 'server',  component: AccountsManager, badge: 'accountsAttention',
    description: 'Claude Max and Copilot accounts the rotation can pick from.' },
```

- [ ] **Step 4: Run the registry, page and nav tests**

Run: `(cd web && npx vitest run src/features/settings/settingsDefs.test.ts src/features/settings/SettingsPage.test.tsx src/features/settings/pageContract.test.tsx src/features/settings/sections/LimitsSection.test.tsx && npm run typecheck)`
Expected: all PASS; typecheck clean (`SETTINGS_BY_ID.capacity` now exists). If `SettingsPage.test.tsx` or `pageContract.test.tsx` asserts a nav label list or a count, update it to include `Limits` under `Capacity` — the assertion will name the exact expectation.

- [ ] **Step 5: Commit**

```bash
git add web/src/features/settings/settingsDefs.ts web/src/features/settings/settingsDefs.test.ts web/src/features/settings/SettingsPage.test.tsx web/src/features/settings/pageContract.test.tsx
printf '%s\n' 'feat(web): Settings group Capacity = Limits · Hosts · Accounts' '' 'Renames the accounts group and registers the Limits section first in it (spec 2026-09-20 D2). Section ids unchanged.' '' 'Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>' > /tmp/pr1-t9.txt
git commit -F /tmp/pr1-t9.txt
```

---

### Task 10: General loses its Capacity group

**Files:**
- Modify: `web/src/features/settings/sections/GeneralSection.tsx:22-78`, `:280-297`
- Test: `web/src/features/settings/sections/GeneralSection.test.tsx`

- [ ] **Step 1: Update the tests**

In `GeneralSection.test.tsx`:
- Delete every line that types into or asserts on `screen.getByLabelText('Max active sessions')` (around lines 119, 186, 191).
- In the payload expectation around line 124, remove `maxActiveSessions: 7,` from the expected object.
- Add one test inside the `describe`:

```ts
  it('has no capacity field — the global cap lives on Settings › Capacity › Limits (spec 2026-09-20 C1)', () => {
    mount()
    expect(screen.queryByLabelText('Max active sessions')).toBeNull()
    expect(screen.queryByLabelText('Attention ceiling')).toBeNull()
  })
```

- [ ] **Step 2: Run to verify it fails**

Run: `(cd web && npx vitest run src/features/settings/sections/GeneralSection.test.tsx)`
Expected: the new test FAILS (the field is still rendered); the payload test fails because the payload still carries `maxActiveSessions: null`.

- [ ] **Step 3: Remove the group**

In `GeneralSection.tsx`:
- `GeneralDraft`: delete `maxActive: string`.
- `toDraft`: delete the `maxActive:` line.
- `toPayload`: delete `const active = Number.parseInt(d.maxActive, 10)` and the `maxActiveSessions:` line; in the doc comment above it, delete the clause `and \`maxActiveSessions\` clears with null`.
- Delete the whole `<SettingsGroup legend="Capacity">…</SettingsGroup>` block (lines 280–297).
- In the component doc comment, change `the archive policy and the session capacity` to `and the archive policy` and `Here it is five fields in three named groups` to `Here it is four fields in three named groups`.

- [ ] **Step 4: Run**

Run: `(cd web && npx vitest run src/features/settings/sections/GeneralSection.test.tsx && npm run typecheck)`
Expected: PASS; typecheck clean.

- [ ] **Step 5: Commit**

```bash
git add web/src/features/settings/sections/GeneralSection.tsx web/src/features/settings/sections/GeneralSection.test.tsx
printf '%s\n' 'feat(web): General drops its Capacity group; the cap lives on Limits' '' 'spec 2026-09-20 C1.' '' 'Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>' > /tmp/pr1-t10.txt
git commit -F /tmp/pr1-t10.txt
```

---

### Task 11: Hosts — one heading, a **Cap** column, the physical-ceiling footer

**Files:**
- Modify: `web/src/features/settings/sections/FleetSection.tsx:150-157` (header), `:176-177` (column header), `:258-299` (input aria-label), after the table (footer)
- Test: `web/src/features/settings/sections/FleetSection.test.tsx`

**Interfaces:**
- Consumes: `useSettings` (for `effectiveDefaults.globalCap`), `settingsSectionPath('capacity')`.

- [ ] **Step 1: Update the tests**

In `FleetSection.test.tsx`: rename the `describe` to `'FleetSection — weight and cap'`; in the four tests at lines 155–183 replace `'Max sessions'` with `'Cap'` and every `'max sessions for rocky'` with `'cap for rocky'`; rename the tests' `max sessions` wording to `cap`. Check the file's `vi.mock('@/api/hooks', …)` factory and add `useSettings: vi.fn()` to it if absent, and in its `beforeEach` add `vi.mocked(hooks.useSettings).mockReturnValue(queryResult(appSettings()))` (import `appSettings` from `@/test/factories` and `queryResult` from `@/test/reactQuery` if not already imported). Do the same in `FleetSection.hostTable.test.tsx` if it mocks `@/api/hooks`. Add:

```ts
  it('has one heading (the chassis h2), not a second "Fleet" h3 (spec 2026-09-20 C2)', () => {
    mount()
    expect(screen.queryByRole('heading', { level: 3, name: 'Fleet' })).toBeNull()
  })

  it('footer states the physical ceiling and links to Limits', () => {
    vi.mocked(hooks.useSettings).mockReturnValue(queryResult(appSettings({
      effectiveDefaults: {
        repoRoot: '/r', archiveHome: null, modelApprovalAction: 'require_approval', modelApprovalDenylist: 'fable',
        globalCap: { physical: 10, attention: null, effective: 10, binding: 'physical', hosts: [{ name: 'dobby', cap: 4 }, { name: 'rocky', cap: 6 }] },
      },
    })))
    mount()
    expect(screen.getByText(/Σ caps = 10/)).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'physical ceiling' })).toHaveAttribute('href', '/settings/capacity')
  })
```

(`mount` is whatever helper the file already uses to render `FleetSection` inside a router; if it renders without a router, wrap it in `createMemoryRouter` the way `GeneralSection.test.tsx` does, because the footer now renders a `Link`.)

- [ ] **Step 2: Run to verify they fail**

Run: `(cd web && npx vitest run src/features/settings/sections/FleetSection.test.tsx)`
Expected: the `Cap` column/labels, heading and footer tests FAIL.

- [ ] **Step 3: Edit the section**

In `FleetSection.tsx`:
- Imports: `import { Link } from 'react-router-dom'`; `import { QK, usePendingDeploy, useFleetHostPatch, useSettings } from '@/api/hooks'`; `import { settingsSectionPath } from '@/shell/navDefs'`.
- Inside the component, after `const patch = useFleetHostPatch()`: `const { data: settings } = useSettings()` and `const gc = settings?.effectiveDefaults.globalCap`.
- Delete the `<header>…</header>` block (lines 151–157): the chassis h2 and the registry description already say this.
- Column header: `<th className="pr-2">Max sessions</th>` → `<th className="pr-2">Cap</th>`.
- Input: `aria-label={\`max sessions for ${h.name}\`}` → `aria-label={\`cap for ${h.name}\`}`; error text `'Max sessions must be a whole number ≥ 1'` → `'Cap must be a whole number ≥ 1'`.
- Directly after the closing `</div>` of the `overflow-x-auto` table wrapper (still inside `{hosts.length > 0 && (…)}`), add:

```tsx
          {gc && (
            <p className="text-xs text-ink-muted">
              {gc.physical === null
                ? 'Σ caps = unbounded (an enabled host has no cap) → '
                : `Σ caps = ${gc.physical} → `}
              <Link to={settingsSectionPath('capacity')} className="text-accent hover:underline">physical ceiling</Link>
            </p>
          )}
```

  (wrap the table and this `<p>` in a fragment `<>…</>` if the conditional now has two children).

- [ ] **Step 4: Run**

Run: `(cd web && npx vitest run src/features/settings/sections/FleetSection.test.tsx src/features/settings/sections/FleetSection.hostTable.test.tsx && npm run typecheck)`
Expected: PASS; typecheck clean.

- [ ] **Step 5: Commit**

```bash
git add web/src/features/settings/sections/FleetSection.tsx web/src/features/settings/sections/FleetSection.test.tsx web/src/features/settings/sections/FleetSection.hostTable.test.tsx
printf '%s\n' 'feat(web): Hosts — Cap column, one heading, physical-ceiling footer linking to Limits' '' 'spec 2026-09-20 C2, D8, §5.1 Hosts.' '' 'Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>' > /tmp/pr1-t11.txt
git commit -F /tmp/pr1-t11.txt
```

---

### Task 12: Typed links from the capacity stack

**Files:**
- Modify: `web/src/features/delivery/capacity/capacityStack.ts:8-9`, `:52-66`
- Test: `web/src/features/delivery/capacity/capacityStack.test.ts`

- [ ] **Step 1: Write the failing test**

Append to `capacityStack.test.ts`:

```ts
describe('buildCapacityStack — row links (spec 2026-09-20 C4, D10)', () => {
  it('links each supply row to its Settings › Capacity section through settingsSectionPath', () => {
    const rows = Object.fromEntries(buildCapacityStack(readout()).map((row) => [row.key, row.href]))
    expect(rows.global).toBe('/settings/capacity')
    expect(rows.hosts).toBe('/settings/fleet')
    expect(rows.accounts).toBe('/settings/accounts')
  })
})
```

- [ ] **Step 2: Run to verify it fails**

Run: `(cd web && npx vitest run src/features/delivery/capacity/capacityStack.test.ts)`
Expected: FAIL, `rows.global` is `'/settings/general'`.

- [ ] **Step 3: Use the typed helper**

In `capacityStack.ts`: add `import { settingsSectionPath } from '@/shell/navDefs'`; replace `href: '/settings/general'` with `href: settingsSectionPath('capacity')`, `href: '/settings/fleet'` with `href: settingsSectionPath('fleet')`, `href: '/settings/accounts'` with `href: settingsSectionPath('accounts')`. Update the Global row's label comment in the file header from `Global cap → Hosts` (leave the label text `Global cap` itself unchanged).

- [ ] **Step 4: Run**

Run: `(cd web && npx vitest run src/features/delivery/capacity/ && npm run typecheck)`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add web/src/features/delivery/capacity/capacityStack.ts web/src/features/delivery/capacity/capacityStack.test.ts
printf '%s\n' 'fix(web): capacity stack links go through settingsSectionPath; Global links to Limits' '' 'spec 2026-09-20 C4.' '' 'Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>' > /tmp/pr1-t12.txt
git commit -F /tmp/pr1-t12.txt
```

---

### Task 13: Docs — feature doc

**Files:**
- Modify: `docs/product/features/capacity-governor.md` (the global layer paragraph)

(The spec already names `server/sessions/global_cap.py:read_global_cap()` — that amendment is the `docs(spec): name the global-cap module` commit at the base of this branch, so no task here edits the spec file.)

- [ ] **Step 1: Feature doc**

In `docs/product/features/capacity-governor.md`, find the sentence describing the global layer (`AppSettings.max_active_sessions`) and append after it:

```markdown
Since 2026-09-20 (PR 1 of the capacity-settings spec) that column is the **attention ceiling**;
the effective global cap is `min(Σ enabled host caps, attention ceiling)` with a floor of 5 when
both are unbounded, read only through `server/sessions/global_cap.py`. It is shown on
Settings › Capacity › Limits and on the readout's Global row (`physical`, `attention`, `binding`).
```

- [ ] **Step 2: Commit**

```bash
git add docs/product/features/capacity-governor.md
printf '%s\n' 'docs(capacity): record the two-ceiling rule in the governor feature doc' '' 'Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>' > /tmp/pr1-t13.txt
git commit -F /tmp/pr1-t13.txt
```

---

### Task 14: Full gate, then the PR

**Files:** none new.

- [ ] **Step 1: Server gate (four steps)**

```bash
/home/tony/GitHub/viberpit-deploy/.venv/bin/python -m ruff check src/ server/
env -i HOME=/tmp/viberpit-test-home PATH="$PATH" PYTHONPATH=$PWD/src /home/tony/GitHub/viberpit-deploy/.venv/bin/python -m mypy server/ --ignore-missing-imports
env -i HOME=/tmp/viberpit-test-home PATH="$PATH" CCSWITCH_DEV=1 PYTHONPATH=$PWD/src /home/tony/GitHub/viberpit-deploy/.venv/bin/python -m pytest tests/ -q
/home/tony/GitHub/viberpit-deploy/.venv/bin/python scripts/brand_ratchet.py
```

Expected: ruff clean; mypy clean (watch `resolve_global_cap`'s `sum(...)` and the `Literal` binding — both typed above); core tests green; ratchet reports no `grew` entries.

Then the full server suite, in the background, with the test-slot cap (the acquire loop and pytest in ONE Bash call, per CLAUDE.md), output tee'd to `/tmp/pr1-server-tests.log`:

```bash
sid="${CCSWITCH_SESSION_ID:-$$}"; host="$(hostname -s)"; trap 'curl -sf -X POST http://127.0.0.1:8787/api/test-slots/release -H "Content-Type: application/json" -d "{\"sessionId\":\"$sid\"}" >/dev/null 2>&1||true' EXIT
while :; do resp="$(curl -sf -X POST http://127.0.0.1:8787/api/test-slots/acquire -H 'Content-Type: application/json' -d "{\"sessionId\":\"$sid\",\"host\":\"$host\"}" 2>/dev/null)" || { echo "test-slot API unreachable — proceeding (fail-open)"; break; }; echo "$resp" | grep -qE '"granted": *true' && break; echo "$resp" | grep -qE '"degraded": *true' && { echo "test-slot control plane degraded — proceeding unchecked"; break; }; echo "waiting for test slot, $(echo "$resp" | grep -oE '"ahead": *[0-9]+' | grep -oE '[0-9]+') ahead"; sleep 15; done
mkdir -p /tmp/viberpit-test-home; env -i HOME=/tmp/viberpit-test-home PATH="$PATH" CCSWITCH_DEV=1 PYTHONPATH=$PWD/src /home/tony/GitHub/viberpit-deploy/.venv/bin/python -m pytest server/tests/ -n auto --dist loadfile --timeout=300 -q 2>&1 | tee /tmp/pr1-server-tests.log | tail -5
```

Expected: `passed` with no failures. A failure only under `-n auto` in `test_ipc_entrypoint.py` / UDS spawn / reauth / ws-timing files is the known rocky-only flake set: re-run that file alone and, if green, note it as pre-existing.

- [ ] **Step 2: Web gate**

```bash
(cd web && npm run typecheck && npm run lint && npm run test 2>&1 | tee /tmp/pr1-web-tests.log | tail -5 && npm run build)
```

Expected: all four green. `npm run test` is the FULL suite: ~47 files mock `@/api/hooks`, and Task 6/11 added `useSettings`/`QK` reads to `FleetSection`, so a file that mounts `FleetSection` with a hooks mock lacking `useSettings` fails here and nowhere else — add `useSettings: vi.fn()` (returning `queryResult(appSettings())`) to that file's mock. Check the exit code of `npm run test` itself, not `tail`'s (`${PIPESTATUS[0]}`).

- [ ] **Step 3: Branch, rebase, push, PR**

The work is on a branch created from `origin/master` at the start (`git fetch && git checkout -b feat/capacity-pr1-limits origin/master` — if you are still on `spec/capacity-settings-structure` or a `ccswitch/master-*` branch, create the branch now and cherry-pick or rebase the 13 commits onto it). Then:

```bash
git fetch origin master && git rebase origin/master
git push -u origin feat/capacity-pr1-limits
```

Write the PR body to `/tmp/pr1-body.md`:

```markdown
## Summary

PR 1 of the capacity-settings spec (`docs/superpowers/specs/2026-09-20-capacity-settings-structure-design.md`, §8): the Limits section and the two ceilings.

- **One reader for the global cap** (`server/sessions/global_cap.py`): effective = min(Σ enabled host caps, attention ceiling), default-5 floor when both are unbounded. The orchestrator gate, the sessions list's queue-stall reason, the settings API, the fleet API and the readout all go through it; a test pins that nothing else reads the column. Closes C14, C17.
- **`maxActiveSessions` is the attention ceiling**: explicit null clears it, values below 1 are 422 (no silent clamp), and the queue drains whenever the *effective* cap rises — including from a host cap or enable change on `PUT /fleet/hosts`.
- **Settings › Capacity** (renamed from Accounts): **Limits** (new) · Hosts · Accounts. Limits shows the physical ceiling with its hosts, the attention ceiling input, the effective cap and which ceiling binds, a read-only copy of the live stack linking to Delivery → Capacity, and the env-managed ceilings outside the stack. General loses its Capacity group. Closes C1.
- **Hosts**: one heading (the chassis h2), column **Cap**, footer "Σ caps = N → physical ceiling" linking to Limits. Closes the heading half of C2.
- **Readout** `global` row gains `physical`, `attention`, `binding`.
- **Freshness and links**: settings, host and account mutations invalidate the capacity readout (C20); stack links go through `settingsSectionPath` and the Global row links to Limits (C4).
- `_MANAGED` gains `CCSWITCH_TEST_SLOT_CAP`, `CCSWITCH_CHILD_SESSION_CAP`, `CCSWITCH_STT_MAX_CONCURRENT` (D9).

No schema change. Behaviour on the live fleet is unchanged: the stored 10 becomes an attention ceiling equal to the physical ceiling (4 + 6).

## Test plan

- [ ] `ruff`, `mypy`, `pytest tests/`, full `pytest server/tests/ -n auto` green
- [ ] `npm run typecheck && npm run lint && npm run test && npm run build` green
- [ ] Deployed: Settings › Capacity › Limits shows "10 — dobby 4 · rocky 6" and "10 — the attention ceiling binds (equal to the physical ceiling)"; Delivery → Capacity's Global row links to it

Refs #2060 (spec). Plan: `docs/superpowers/plans/2026-09-20-capacity-settings-pr1-limits.md`.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
```

Before opening it, check for in-flight PRs touching the same files (CLAUDE.md rule) with the GraphQL query from `.agents/instructions/core.md` using the path regex `settings\.py|fleet\.py|global_cap|settingsDefs|GeneralSection|FleetSection|capacityStack|capacity_readout`. Then:

```bash
gh pr create --title "feat(capacity): Limits section and the two ceilings (spec PR 1)" --body-file /tmp/pr1-body.md --base master --head feat/capacity-pr1-limits
gh pr merge <number> --squash --auto
```

Expected: PR is open, non-draft, auto-merge armed.

---

## Self-review

**Spec coverage (PR 1 scope, §8 row 1):** `effective_global_cap()` + single clamp → Tasks 1–5 (named `read_global_cap`, path amended in Task 13). D3 physical/attention/effective → Tasks 1, 3, 5, 8. Limits section → Task 8. Group rename D2 → Task 9. General loses Capacity → Task 10. `_MANAGED` additions D9 → Task 3 (server) and Task 8 (rendered). Invalidation C20 → Task 6. Typed links C4 → Task 12. Deletes: `api/sessions.py:_max_active_sessions_setting` → Task 2; the General Capacity group → Task 10; the three clamps → Tasks 1–3 (`max(1, …)` now lives only in `resolve_global_cap`). §5.1 Hosts row (h3 removal, **Cap**, footer) → Task 11. §7 `PUT` 422 semantics → Task 3. §7 `GET /api/capacity` `global` fields → Task 5. Not in PR 1 by design: the Delivery stack's "physical 10 · attention 10" secondary text and the Accounts row expansion (PR 2), the runner form (PR 3), the reserve override (PR 5), the pipeline cap (PR 6).

**Placeholder scan:** no TBD/TODO/"similar to"; every code step carries its code.

**Type consistency:** `GlobalCap` fields `(physical, attention, effective, binding, hosts)` and `to_wire()` keys match the web `GlobalCapDefaults` and every fixture (`{ physical, attention, effective, binding, hosts: [{ name, cap }] }`). `read_global_cap(db)` is the name used in Tasks 2, 3, 4, 5, 13. `drain_if_raised(orch, before, after)` in Tasks 1, 3, 4. `CapacitySources.global_limits` in Task 5 only. `settingsSectionPath('capacity')` in Tasks 8, 11, 12 resolves to `/settings/capacity`, which Task 9's section id makes real. `aria-label` `cap for <host>` is consistent between Task 11's code and tests.

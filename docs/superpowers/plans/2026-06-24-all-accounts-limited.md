# Disambiguate "All accounts at limit" — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop showing the misleading "All accounts at limit · No reset time available" banner when accounts are merely unreadable; create the session optimistically instead, keep an accurate self-clearing banner for genuine exhaustion, and surface a distinct message when accounts truly need re-auth.

**Architecture:** Split the single `AllAccountsBusy` outcome into three states — (A) genuinely at-ceiling → block with reset time; (B) usage unreadable but account healthy → create optimistically with a `usageUnverified` flag; (C) all accounts need re-auth → distinct 409. The selector gains an "optimistic" last-resort tier and a `kind` on its exception; the flag rides a transient `SessionRecord.usage_unverified` field to the HTTP boundary; the frontend toasts on B/C and auto-clears the A banner from the existing 30s accounts poll.

**Tech Stack:** Python (FastAPI, dataclasses, pytest) backend in `src/ccswitch` + `server/`; React + TypeScript + vitest frontend in `web/`.

## Global Constraints

- Backend lint gate (run before pushing): `ruff` + `mypy` + `pytest server/tests/` and `pytest` for `src/ccswitch`. mypy skips untyped fn bodies.
- Web gates: `vitest run`, `tsc -b` (NOT `tsc --noEmit` — it checks nothing here), `eslint` with `--max-warnings 0`.
- Run worktree backend tests with the prebuilt env and `PYTHONPATH=<worktree>/src` (the venv's editable ccswitch resolves to a stale deploy checkout). Symlink `web/node_modules` from the deploy worktree for web tests; use the local `.bin`, not `npx`.
- New `SessionRecord` field MUST default (`= False`) so the ~25 existing `create_session` call sites keep compiling.
- The `at_ceiling` predicate (`u.session_pct >= ceiling or u.weekly_pct >= ceiling`) stays the single source of truth in `selector.py` — do not duplicate it.
- Wire timestamps: never use bare `.isoformat()` for NEW timestamps — but this plan adds no new wire timestamps (reuses existing `earliest_reset.isoformat()`).

---

## File Structure

- `src/ccswitch/selector.py` — pure ranking. Add `Picked` dataclass, `RankedAccount.optimistic`, `AllAccountsBusy.kind`, an `unhealthy` param + optimistic tier in `rank_accounts`, and change `select_best` to return `Picked`.
- `src/ccswitch/ledger.py` — add `get_status(led, name) -> str | None`.
- `src/ccswitch/accounts.py` — `pick_account_detailed(...) -> Picked` (threads `unhealthy`), `pick_account(...) -> str` thin wrapper.
- `server/sessions/selection.py` — `select_account(...) -> Picked`.
- `server/sessions/types.py` — `SessionRecord.usage_unverified: bool = False` + to_dict/from_dict.
- `server/sessions/orchestrator.py` — set `record.usage_unverified` in create path; use `.name` at the rotation call site.
- `server/api/sessions.py` — `usageUnverified` in 201 body; `accounts_need_reauth` 409 via `kind`.
- `server/api/accounts.py` — pass `unhealthy` to `rank_accounts` so needs-reauth accounts don't show an optimistic rank.
- `web/src/api/types.ts` — `Session.usageUnverified?: boolean`.
- `web/src/components/Dashboard.tsx`, `web/src/components/DesktopShell.tsx` — toast on B/C + auto-clear effect.

---

## Task 1: Selector — optimistic tier, `kind`, `Picked`

**Files:**
- Modify: `src/ccswitch/selector.py`
- Test: `src/ccswitch/tests/test_selector.py` (create if absent; else add to the existing selector test module)

**Interfaces:**
- Produces:
  - `class Picked: name: str; optimistic: bool` (dataclass)
  - `RankedAccount.optimistic: bool` (new field, default `False`)
  - `AllAccountsBusy(earliest_reset: datetime | None, kind: str = "at_ceiling")` with `.kind` in `{"at_ceiling","no_usable_accounts"}`
  - `rank_accounts(usages, reserved, cfg, now, exclude=frozenset(), load=None, unhealthy=frozenset()) -> tuple[list[RankedAccount], datetime | None]` — unreadable-but-healthy accounts (`u is None and name not in unhealthy/exclude/reserved`) appear as a last-resort tier ranked strictly after all readable candidates, ordered by least `load`, each `optimistic=True`.
  - `select_best(usages, reserved, cfg, now, exclude=frozenset(), load=None, unhealthy=frozenset()) -> Picked` — returns `Picked(ranked[0].name, ranked[0].optimistic)`; raises `AllAccountsBusy(earliest, kind)` when `ranked` is empty, `kind="at_ceiling"` if `earliest is not None` else `"no_usable_accounts"`.

- [ ] **Step 1: Write the failing tests**

Add to `src/ccswitch/tests/test_selector.py` (use the existing test helpers/fixtures if the module exists; otherwise this minimal harness):

```python
from datetime import datetime, timedelta, timezone
import pytest
from ccswitch.config import Config
from ccswitch.oauth import Usage
from ccswitch.selector import rank_accounts, select_best, AllAccountsBusy, Picked

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)

def _cfg() -> Config:
    return Config()  # defaults: hard_ceiling, load_sensitivity, prefer_soon_reset

def _u(session_pct: float, weekly_pct: float = 0.0, reset_h: float | None = 24.0) -> Usage:
    reset = NOW + timedelta(hours=reset_h) if reset_h is not None else None
    return Usage(session_pct, reset, weekly_pct, reset, False)

def test_readable_below_ceiling_picks_normally():
    picked = select_best({"a": _u(10.0)}, frozenset(), _cfg(), NOW)
    assert picked == Picked(name="a", optimistic=False)

def test_all_at_ceiling_raises_at_ceiling_kind_with_reset():
    with pytest.raises(AllAccountsBusy) as ei:
        select_best({"a": _u(100.0), "b": _u(100.0)}, frozenset(), _cfg(), NOW)
    assert ei.value.kind == "at_ceiling"
    assert ei.value.earliest_reset is not None

def test_all_unreadable_healthy_picks_optimistically_least_load():
    picked = select_best(
        {"a": None, "b": None}, frozenset(), _cfg(), NOW,
        load={"a": 2.0, "b": 0.0}, unhealthy=frozenset(),
    )
    assert picked == Picked(name="b", optimistic=True)

def test_all_unhealthy_raises_no_usable_accounts():
    with pytest.raises(AllAccountsBusy) as ei:
        select_best({"a": None}, frozenset(), _cfg(), NOW, unhealthy=frozenset({"a"}))
    assert ei.value.kind == "no_usable_accounts"
    assert ei.value.earliest_reset is None

def test_readable_wins_over_optimistic():
    picked = select_best({"a": _u(10.0), "b": None}, frozenset(), _cfg(), NOW)
    assert picked == Picked(name="a", optimistic=False)

def test_mixed_ceiling_plus_unreadable_picks_optimistic_no_raise():
    picked = select_best({"a": _u(100.0), "b": None}, frozenset(), _cfg(), NOW, unhealthy=frozenset())
    assert picked == Picked(name="b", optimistic=True)

def test_optimistic_ranked_after_readable_in_list():
    ranked, _ = rank_accounts({"a": _u(10.0), "b": None}, frozenset(), _cfg(), NOW)
    assert [r.name for r in ranked] == ["a", "b"]
    assert ranked[0].optimistic is False and ranked[1].optimistic is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=$PWD/src <deploy-venv>/bin/python -m pytest src/ccswitch/tests/test_selector.py -v`
Expected: FAIL — `ImportError: cannot import name 'Picked'` / `AllAccountsBusy.__init__` missing `kind` / `select_best` returns `str`.

- [ ] **Step 3: Implement the selector changes**

In `src/ccswitch/selector.py`:

Add the `Picked` dataclass near `RankedAccount`:

```python
@dataclass
class Picked:
    name: str
    optimistic: bool = False
```

Add `optimistic` to `RankedAccount`:

```python
@dataclass
class RankedAccount:
    name: str
    rank: int
    reason: str
    optimistic: bool = False
```

Add `kind` to the exception:

```python
class AllAccountsBusy(Exception):
    def __init__(self, earliest_reset: datetime | None, kind: str = "at_ceiling"):
        super().__init__("all accounts at/above usage ceiling")
        self.earliest_reset = earliest_reset
        self.kind = kind
```

Rewrite the loop in `rank_accounts` to add the optimistic tier (replace the body from the `for name, u` loop through the `return`):

```python
def rank_accounts(usages, reserved, cfg, now, exclude=frozenset(),
                  load=None, unhealthy=frozenset()):
    load = load or {}
    ceiling = cfg.hard_ceiling * 100.0
    candidates: list[tuple[float, float, float, float, str]] = []
    optimistic: list[tuple[float, str]] = []   # (load, name)
    busy_resets: list[datetime] = []
    for name, u in usages.items():
        if name in exclude or name in reserved:
            continue
        if u is None:
            # Usage unreadable. A healthy account is a last-resort optimistic
            # pick; a needs-reauth account is simply ineligible.
            if name not in unhealthy:
                optimistic.append((load.get(name, 0.0), name))
            continue
        at_ceiling = u.session_pct >= ceiling or u.weekly_pct >= ceiling
        if at_ceiling:
            resets = []
            if u.session_pct >= ceiling and u.session_resets_at is not None:
                resets.append(u.session_resets_at)
            if u.weekly_pct >= ceiling and u.weekly_resets_at is not None:
                resets.append(u.weekly_resets_at)
            if resets:
                busy_resets.append(min(resets))
            continue
        acct_load = load.get(name, 0.0)
        perish = _perishability(u, ceiling, now) if cfg.prefer_soon_reset else 0.0
        composite = perish / (1.0 + cfg.load_sensitivity * acct_load)
        candidates.append((-composite, acct_load, u.session_pct, u.weekly_pct, name))
    candidates.sort()
    optimistic.sort()  # least live load first
    earliest = min(busy_resets) if busy_resets else None
    ranked = [
        RankedAccount(name=c[4], rank=i + 1, reason=_reason(-c[0], c[1]))
        for i, c in enumerate(candidates)
    ]
    ranked += [
        RankedAccount(name=name, rank=len(ranked) + i + 1,
                      reason="usage unverified — optimistic pick", optimistic=True)
        for i, (_, name) in enumerate(optimistic)
    ]
    return ranked, earliest
```

Rewrite `select_best` to return `Picked` and raise with `kind`:

```python
def select_best(usages, reserved, cfg, now, exclude=frozenset(),
                load=None, unhealthy=frozenset()) -> Picked:
    ranked, earliest = rank_accounts(usages, reserved, cfg, now, exclude, load, unhealthy)
    if not ranked:
        kind = "at_ceiling" if earliest is not None else "no_usable_accounts"
        raise AllAccountsBusy(earliest, kind)
    top = ranked[0]
    return Picked(name=top.name, optimistic=top.optimistic)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH=$PWD/src <deploy-venv>/bin/python -m pytest src/ccswitch/tests/test_selector.py -v`
Expected: PASS (all 7).

- [ ] **Step 5: Commit**

```bash
git add src/ccswitch/selector.py src/ccswitch/tests/test_selector.py
git commit -m "feat(selector): optimistic last-resort tier + AllAccountsBusy.kind

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 2: Ledger status getter + `pick_account_detailed`

**Files:**
- Modify: `src/ccswitch/ledger.py`, `src/ccswitch/accounts.py`
- Test: `src/ccswitch/tests/test_pick_account_detailed.py` (create)

**Interfaces:**
- Consumes: `Picked`, `select_best(..., unhealthy=...)` from Task 1.
- Produces:
  - `ledger.get_status(led: dict, name: str) -> str | None`
  - `accounts.pick_account_detailed(cfg, exclude=frozenset(), load=None) -> Picked` — builds `usages`, `reserved`, and `unhealthy = {n for n in names if get_status(led, n) == "needs-reauth"}`, calls `select_best`, reserves the picked account, returns `Picked`.
  - `accounts.pick_account(cfg, exclude=frozenset(), load=None) -> str` — returns `pick_account_detailed(...).name` (unchanged contract for `launcher.py`, `cli.py`, `runner.py`).

- [ ] **Step 1: Write the failing test**

Create `src/ccswitch/tests/test_pick_account_detailed.py`. Follow the existing `server/tests/test_selection.py` slot-writing fixture pattern for credential slots; mock `usage_for` to control readability:

```python
from unittest.mock import patch
from ccswitch.config import Config
from ccswitch.selector import Picked
from ccswitch import accounts


def test_pick_account_detailed_returns_optimistic_when_usage_none(tmp_path, monkeypatch):
    # Two healthy accounts whose usage can't be read → optimistic pick.
    monkeypatch.setattr(accounts, "_account_names", lambda: ["a", "b"])
    with patch.object(accounts, "usage_for", return_value=None), \
         patch("ccswitch.ledger.get_status", return_value="ok"), \
         patch("ccswitch.ledger.reservation_active", return_value=False), \
         patch("ccswitch.ledger.reserve"):
        picked = accounts.pick_account_detailed(Config(), load={"a": 1.0, "b": 0.0})
    assert isinstance(picked, Picked)
    assert picked.optimistic is True
    assert picked.name == "b"  # least load


def test_pick_account_str_wrapper_returns_name(tmp_path, monkeypatch):
    monkeypatch.setattr(accounts, "_account_names", lambda: ["a"])
    with patch.object(accounts, "usage_for", return_value=None), \
         patch("ccswitch.ledger.get_status", return_value="ok"), \
         patch("ccswitch.ledger.reservation_active", return_value=False), \
         patch("ccswitch.ledger.reserve"):
        name = accounts.pick_account(Config())
    assert name == "a"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=$PWD/src <deploy-venv>/bin/python -m pytest src/ccswitch/tests/test_pick_account_detailed.py -v`
Expected: FAIL — `AttributeError: module 'ccswitch.accounts' has no attribute 'pick_account_detailed'` / `ledger.get_status` missing.

- [ ] **Step 3: Implement**

In `src/ccswitch/ledger.py`, add next to `set_status`:

```python
def get_status(led: dict, name: str) -> str | None:
    return _acct(led, name).get("status")
```

In `src/ccswitch/accounts.py`, change the import and replace `pick_account` (lines 193-203):

```python
from .selector import Picked, select_best  # replaces the `select_best`-only import
```

```python
def pick_account_detailed(cfg: Config, exclude: set[str] = frozenset(),
                          load: dict[str, float] | None = None) -> Picked:
    names = _account_names()
    usages = {n: usage_for(n) for n in names}
    now = datetime.now(timezone.utc)
    now_ts = time.time()
    with ledger.locked_ledger() as led:
        reserved = {n for n in names if ledger.reservation_active(led, n, now_ts)}
        unhealthy = frozenset(n for n in names if ledger.get_status(led, n) == "needs-reauth")
        picked = select_best(usages, reserved, cfg, now, exclude, load or {}, unhealthy)
        ledger.reserve(led, picked.name, now_ts + cfg.reservation_ttl_sec)
    return picked


def pick_account(cfg: Config, exclude: set[str] = frozenset(),
                 load: dict[str, float] | None = None) -> str:
    return pick_account_detailed(cfg, exclude, load).name
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH=$PWD/src <deploy-venv>/bin/python -m pytest src/ccswitch/tests/test_pick_account_detailed.py src/ccswitch/tests/test_selector.py -v`
Expected: PASS.

- [ ] **Step 5: Run the existing selection suite for regressions**

Run: `PYTHONPATH=$PWD/src <deploy-venv>/bin/python -m pytest server/tests/test_selection.py -v`
Expected: PASS (the `select_best` return-type change is internal to `pick_account`).

- [ ] **Step 6: Commit**

```bash
git add src/ccswitch/ledger.py src/ccswitch/accounts.py src/ccswitch/tests/test_pick_account_detailed.py
git commit -m "feat(accounts): pick_account_detailed threads unhealthy + returns Picked

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 3: Propagate `optimistic` through orchestrator + `SessionRecord`

**Files:**
- Modify: `server/sessions/selection.py`, `server/sessions/types.py`, `server/sessions/orchestrator.py`
- Test: `server/tests/test_types_usage_unverified.py` (create)

**Interfaces:**
- Consumes: `Picked` from Task 1; `pick_account_detailed` from Task 2.
- Produces:
  - `server.sessions.selection.select_account(exclude=frozenset(), load=None) -> Picked`
  - `SessionRecord.usage_unverified: bool = False`, round-tripped through `to_dict` (`"usageUnverified"`) / `from_dict`.
  - `orchestrator.create_session(...)` sets `record.usage_unverified = picked.optimistic` when it selected the account itself.

- [ ] **Step 1: Write the failing test**

Create `server/tests/test_types_usage_unverified.py`:

```python
from server.sessions.types import SessionRecord


def _rec(**kw) -> SessionRecord:
    base = dict(id="i", name="n", account="a", claude_session_id="c",
                cols=80, rows=24, cwd="/tmp")
    base.update(kw)
    return SessionRecord(**base)


def test_usage_unverified_defaults_false():
    assert _rec().usage_unverified is False


def test_usage_unverified_round_trips():
    rec = _rec(usage_unverified=True)
    assert rec.to_dict()["usageUnverified"] is True
    assert SessionRecord.from_dict(rec.to_dict()).usage_unverified is True


def test_from_dict_missing_key_defaults_false():
    d = _rec().to_dict()
    d.pop("usageUnverified", None)
    assert SessionRecord.from_dict(d).usage_unverified is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=$PWD/src <deploy-venv>/bin/python -m pytest server/tests/test_types_usage_unverified.py -v`
Expected: FAIL — `TypeError: unexpected keyword argument 'usage_unverified'`.

- [ ] **Step 3: Implement the record field**

In `server/sessions/types.py`, add the field after `active_goal` (keep the defaulted-field grouping):

```python
    active_goal: str | None = None    # active /goal condition; None = no goal
    usage_unverified: bool = False    # transient: account picked without a usage read (optimistic)
```

Add to `to_dict`'s returned dict (after `"activeGoal"`):

```python
            "activeGoal": self.active_goal,
            "usageUnverified": self.usage_unverified,
```

Add to `from_dict`'s constructor call (after `active_goal=...`):

```python
            active_goal=d.get("activeGoal"),
            usage_unverified=d.get("usageUnverified", False),
```

- [ ] **Step 4: Update `select_account` to return `Picked`**

In `server/sessions/selection.py`, change the import and body:

```python
from ccswitch.accounts import pick_account_detailed
from ccswitch.config import load_config
from ccswitch.selector import AllAccountsBusy, Picked  # re-exported

__all__ = ["select_account", "AllAccountsBusy", "Picked"]


def select_account(exclude: frozenset[str] = frozenset(),
                   load: dict[str, float] | None = None) -> Picked:
    cfg = load_config()
    return pick_account_detailed(cfg, exclude=set(exclude), load=load)
```

- [ ] **Step 5: Update the two orchestrator call sites**

In `server/sessions/orchestrator.py`, update the import (line 47):

```python
from server.sessions.selection import AllAccountsBusy, Picked, select_account  # noqa: F401
```

Create path (lines 344-349): capture the `Picked` and the optimistic flag. Replace:

```python
        usage_unverified = False
        if account is None:
            picked = select_account(
                exclude=self._recovery.quarantined(now=self._clock()),
                load=self.live_session_load())
            account = picked.name
            usage_unverified = picked.optimistic
```

Then, where the `SessionRecord(` is constructed in `create_session` (near line 412), pass `usage_unverified=usage_unverified` into the constructor.

Rotation path (line 1646) — selection returns `Picked` now, take `.name`:

```python
            new_account = select_account(exclude=exclude, load=self.live_session_load()).name
```

- [ ] **Step 6: Run tests**

Run: `PYTHONPATH=$PWD/src <deploy-venv>/bin/python -m pytest server/tests/test_types_usage_unverified.py server/tests/test_session_repo.py server/tests/test_orchestrator_worktree.py -v`
Expected: PASS (record round-trips; create paths still build records).

- [ ] **Step 7: Commit**

```bash
git add server/sessions/types.py server/sessions/selection.py server/sessions/orchestrator.py server/tests/test_types_usage_unverified.py
git commit -m "feat(orchestrator): carry optimistic pick as SessionRecord.usage_unverified

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 4: API — `usageUnverified` body + `accounts_need_reauth` 409 + accounts rank fix

**Files:**
- Modify: `server/api/sessions.py`, `server/api/accounts.py`
- Test: `server/tests/test_api.py`

**Interfaces:**
- Consumes: `AllAccountsBusy.kind` (Task 1), `record.usage_unverified` (Task 3).
- Produces:
  - `POST /api/sessions` 201 body includes `usageUnverified` (already present via `_record_to_dict` → `to_dict`; assert it).
  - `AllAccountsBusy` with `kind=="no_usable_accounts"` → `409 { error.code: "accounts_need_reauth" }`; `kind=="at_ceiling"` → existing `all_accounts_limited` + `earliestReset`.
  - `GET /api/accounts` passes `unhealthy` to `rank_accounts` so needs-reauth accounts keep `rank=None`.

- [ ] **Step 1: Write the failing tests**

Add to `server/tests/test_api.py` (mirror the existing `pick_account` patch pattern at lines 383/416 — note it patches `server.sessions.selection.pick_account`; update to `pick_account_detailed`):

```python
def test_create_optimistic_sets_usage_unverified(self, client, ...):
    """Usage unreadable but account healthy → 201 with usageUnverified true."""
    from ccswitch.selector import Picked
    with patch("server.sessions.selection.pick_account_detailed",
               return_value=Picked(name="acct-a", optimistic=True)):
        resp = client.post("/api/sessions", json={"name": "opt"})
    assert resp.status_code == 201
    assert resp.json()["usageUnverified"] is True


def test_create_all_unusable_returns_needs_reauth(self, client, ...):
    """All accounts need reauth → 409 accounts_need_reauth, no earliestReset."""
    from server.sessions.selection import AllAccountsBusy
    with patch("server.sessions.selection.pick_account_detailed",
               side_effect=AllAccountsBusy(None, kind="no_usable_accounts")):
        resp = client.post("/api/sessions", json={"name": "x"})
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "accounts_need_reauth"
```

(Keep the existing `test_create_session_all_busy_*` tests; update their patch target from `pick_account` to `pick_account_detailed` and have the mock raise `AllAccountsBusy(reset, kind="at_ceiling")`.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=$PWD/src <deploy-venv>/bin/python -m pytest server/tests/test_api.py -k "optimistic or needs_reauth or all_busy" -v`
Expected: FAIL — `accounts_need_reauth` not produced; `pick_account_detailed` patch target missing.

- [ ] **Step 3: Implement the API branch**

In `server/api/sessions.py`, replace the `except AllAccountsBusy` block (lines 315-327):

```python
    except AllAccountsBusy as exc:
        if exc.kind == "no_usable_accounts":
            return JSONResponse(
                status_code=status.HTTP_409_CONFLICT,
                content={
                    "error": {
                        "code": "accounts_need_reauth",
                        "message": "No usable accounts — one or more need re-authentication",
                    },
                },
            )
        reset_iso = exc.earliest_reset.isoformat() if exc.earliest_reset is not None else None
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={
                "error": {
                    "code": "all_accounts_limited",
                    "message": "All accounts are at or above the usage ceiling",
                },
                "earliestReset": reset_iso,
            },
        )
```

(The 201 body already carries `usageUnverified` because `_record_to_dict` delegates to `SessionRecord.to_dict`. No change needed there — the test asserts it.)

- [ ] **Step 4: Fix accounts rank to exclude needs-reauth from the optimistic tier**

In `server/api/accounts.py`, build `unhealthy` from the per-account status already in scope and pass it (replace the `rank_accounts(...)` call at lines 137-140):

```python
    unhealthy = frozenset(
        acct["name"] for acct in accounts_info
        if acct.get("status") == "needs-reauth"
    )
    ranked, _earliest = rank_accounts(
        usages, reserved=frozenset(), cfg=load_config(),
        now=datetime.now(timezone.utc), load=load, unhealthy=unhealthy,
    )
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `PYTHONPATH=$PWD/src <deploy-venv>/bin/python -m pytest server/tests/test_api.py -v`
Expected: PASS.

- [ ] **Step 6: Backend lint**

Run: `ruff check server/ src/ && <deploy-venv>/bin/python -m mypy server/api/sessions.py server/api/accounts.py src/ccswitch/selector.py src/ccswitch/accounts.py`
Expected: clean.

- [ ] **Step 7: Commit**

```bash
git add server/api/sessions.py server/api/accounts.py server/tests/test_api.py
git commit -m "feat(api): usageUnverified on create + accounts_need_reauth 409

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 5: Frontend types + create hook passthrough

**Files:**
- Modify: `web/src/api/types.ts`
- Test: covered by Task 6's component tests (no standalone test — a one-line optional field).

**Interfaces:**
- Produces: `Session.usageUnverified?: boolean`.

- [ ] **Step 1: Add the field**

In `web/src/api/types.ts`, inside `interface Session`, add:

```ts
  /** True when the session was created without a verified usage read (optimistic pick). */
  usageUnverified?: boolean
```

- [ ] **Step 2: Verify it type-checks**

Run (in `web/`): `./node_modules/.bin/tsc -b`
Expected: clean.

- [ ] **Step 3: Commit**

```bash
git add web/src/api/types.ts
git commit -m "feat(web): Session.usageUnverified field

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 6: Frontend — toasts + auto-clear banner (Dashboard + DesktopShell)

**Files:**
- Modify: `web/src/components/Dashboard.tsx`, `web/src/components/DesktopShell.tsx`
- Test: `web/src/components/__tests__/Dashboard.test.tsx`, `web/src/components/__tests__/DesktopShell.test.tsx` (add cases to existing files)

**Interfaces:**
- Consumes: `Session.usageUnverified` (Task 5); 409 `error.code` values `all_accounts_limited` / `accounts_need_reauth` (Task 4); `useAccounts()` returning `Account[]` with `rank: number | null`; `toast({ message, variant })` from `@/lib/toast`.

- [ ] **Step 1: Write the failing tests**

In `Dashboard.test.tsx`, add (follow the file's existing mock-factory pattern for `@/api/hooks` and `@/lib/toast`):

```tsx
it('shows a subtle toast when a session is created optimistically', async () => {
  // createSession mock resolves with { ...session, usageUnverified: true }
  // expect toast called with variant 'warning' (or 'info') and a "started anyway" message
})

it('routes accounts_need_reauth 409 to a toast, not the limit banner', async () => {
  // createSession mock rejects with { status: 409, body: { error: { code: 'accounts_need_reauth' } } }
  // expect toast called; expect AllLimitedBanner NOT rendered
})

it('auto-clears the all-limited banner once an account becomes selectable', async () => {
  // 1) trigger all_accounts_limited 409 → banner shows
  // 2) re-render with useAccounts returning an account with rank=1
  // 3) expect AllLimitedBanner removed
})
```

Add the equivalent three cases to `DesktopShell.test.tsx`.

- [ ] **Step 2: Run tests to verify they fail**

Run (in `web/`): `./node_modules/.bin/vitest run src/components/__tests__/Dashboard.test.tsx src/components/__tests__/DesktopShell.test.tsx`
Expected: FAIL.

- [ ] **Step 3: Implement in `Dashboard.tsx`**

`onSuccess` of `createSession` (inside `handleCreateSubmit`, line ~1312) — add the optimistic toast:

```tsx
      onSuccess: (session) => {
        dismissNewDialog()
        if (session.usageUnverified) {
          toast({ message: "Couldn't verify account usage — started anyway.", variant: 'warning' })
        }
        if (onOpenSession) onOpenSession(session.id, input.initialPrompt)
      },
```

`onError` 409 handler (line ~1318) — add the `accounts_need_reauth` branch:

```tsx
        if (err.status === 409) {
          const body = err.body as { error?: { code?: string; message?: string }; earliestReset?: string | null }
          if (body?.error?.code === 'branch_switch_failed') {
            setBranchError(body.error.message ?? 'Could not switch branch')
          } else if (body?.error?.code === 'accounts_need_reauth') {
            toast({ message: 'No usable accounts — check the Accounts tab to re-authenticate.', variant: 'error' })
            dismissNewDialog()
          } else {
            setAllLimitedReset(body.earliestReset ?? null)
            dismissNewDialog()
          }
        }
```

Auto-clear effect — add near the other effects, using the already-in-scope `accounts` (line 1184) and `allLimitedReset` (line 1223):

```tsx
  useEffect(() => {
    if (allLimitedReset === undefined) return
    const recovered = accounts.some((a) => a.rank !== null)
    const resetPassed = allLimitedReset !== null && new Date(allLimitedReset).getTime() <= Date.now()
    if (recovered || resetPassed) setAllLimitedReset(undefined)
  }, [accounts, allLimitedReset])
```

Ensure `toast` is imported (`import { toast } from '@/lib/toast'`) and `useEffect` is in the React import.

- [ ] **Step 4: Implement the same three changes in `DesktopShell.tsx`**

Mirror Step 3 in `DesktopShell.tsx`: optimistic toast in `onSuccess` (line ~290), `accounts_need_reauth` branch in the 409 `onError` (line ~294), and the auto-clear `useEffect` using `accounts` (line 112) and `allLimitedReset` (line 133). Add the `toast` import if missing.

- [ ] **Step 5: Run tests to verify they pass**

Run (in `web/`): `./node_modules/.bin/vitest run src/components/__tests__/Dashboard.test.tsx src/components/__tests__/DesktopShell.test.tsx`
Expected: PASS.

- [ ] **Step 6: Full web gates**

Run (in `web/`): `./node_modules/.bin/vitest run && ./node_modules/.bin/tsc -b && ./node_modules/.bin/eslint src --max-warnings 0`
Expected: clean. (Watch the react-hooks "no ref read during render" + exhaustive-deps rules — the effect deps are `[accounts, allLimitedReset]`.)

- [ ] **Step 7: Commit**

```bash
git add web/src/components/Dashboard.tsx web/src/components/DesktopShell.tsx web/src/components/__tests__/Dashboard.test.tsx web/src/components/__tests__/DesktopShell.test.tsx
git commit -m "feat(web): optimistic toast + accounts_need_reauth + auto-clear all-limited banner

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Final verification (before PR)

- [ ] Backend: `PYTHONPATH=$PWD/src <deploy-venv>/bin/python -m pytest src/ccswitch/tests/ server/tests/ -q`
- [ ] Backend lint: `ruff check server/ src/ && <deploy-venv>/bin/python -m mypy server/ src/ccswitch`
- [ ] Web: in `web/` → `./node_modules/.bin/vitest run && ./node_modules/.bin/tsc -b && ./node_modules/.bin/eslint src --max-warnings 0`
- [ ] Manual smoke (optional, per `verify` skill): force `usage_for` to return None for all accounts and confirm a session creates with the subtle toast rather than the amber banner.

---

## Spec coverage check

- State A (at ceiling) → Task 1 (`kind="at_ceiling"` + earliest), Task 4 (`all_accounts_limited`), Task 6 (banner + auto-clear). ✓
- State B (usage unreadable) → Task 1 (optimistic tier), Task 2 (`unhealthy` gating), Task 3 (`usage_unverified`), Task 4 (201 body), Task 5/6 (toast). ✓
- State C (needs reauth) → Task 1 (`no_usable_accounts`), Task 2 (`unhealthy` set), Task 4 (`accounts_need_reauth`), Task 6 (toast → Accounts tab). ✓
- Auto-clear on recovery → Task 6 effect on `useAccounts().rank`. ✓
- No new SSE / no rotation-recovery changes / no usage-fetch retry → respected (rotation path only adjusted for the `.name` return shape). ✓

# Disambiguate "All accounts at limit" — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop showing the misleading "All accounts at limit · No reset time available" banner when accounts are merely unreadable; create the session optimistically instead, keep an accurate self-clearing banner for genuine exhaustion, and surface a distinct message when accounts truly need re-auth.

**Architecture:** Split the single `AllAccountsBusy` outcome into three states — (A) genuinely at-ceiling → block with reset time; (B) usage unreadable but account healthy → create optimistically with a `usageUnverified` flag; (C) all accounts need re-auth → distinct 409. The optimistic behavior is **opt-in** (a new `optimistic_fallback` flag), scoped to the web session-create path only; `select_best`, the CLI launcher, and the accounts list keep their exact legacy behavior.

**Tech Stack:** Python (FastAPI, dataclasses, pytest/unittest) backend in `src/ccswitch` + `server/`; React + TypeScript + vitest frontend in `web/`.

## Global Constraints

- **Worktree:** `/mnt/ssk-ssd/tony/GitHub/.ccswitch/worktrees/cc-account-switcher/45767c57` (branch `fix/all-accounts-limited-disambiguation`).
- **Prebuilt venv (the worktree has none):** `/mnt/ssk-ssd/tony/GitHub/.ccswitch/clones/steiner385/cc-account-switcher/.venv/bin/python`.
- **Backend test command:** from the worktree root, `PYTHONPATH=$PWD/src /mnt/ssk-ssd/tony/GitHub/.ccswitch/clones/steiner385/cc-account-switcher/.venv/bin/python -m pytest <paths> -q` (the venv's editable ccswitch resolves to a STALE deploy checkout without `PYTHONPATH=$PWD/src`).
- **ccswitch unit tests** live in top-level `tests/` (NOT `src/ccswitch/tests/`). Reuse `from tests.helpers import temp_env, usage`; `usage(session=10.0, weekly=20.0, extra=False, mins=120)` builds a `Usage` whose reset windows are `now + mins`.
- **Backend lint gate (run before final PR):** `ruff check server/ src/` + `mypy` (mypy skips untyped fn bodies).
- **Web (run in `web/`):** `./node_modules/.bin/vitest run`, `./node_modules/.bin/tsc -b` (NOT `tsc --noEmit` — it checks nothing here), `./node_modules/.bin/eslint src --max-warnings 0`. `web/node_modules` is already symlinked from the deploy clone; use the local `.bin`, not `npx`.
- **Regression guard:** the existing 24 tests in `tests/test_selector.py` MUST stay green — `select_best` keeps its `-> str` contract and legacy behavior.
- New `SessionRecord` field MUST default (`= False`) so the ~25 existing `create_session` call sites keep compiling.
- The `at_ceiling` predicate (`u.session_pct >= ceiling or u.weekly_pct >= ceiling`) stays the single source of truth in `selector.py` — do not duplicate it.
- This plan adds no new wire timestamps (reuses existing `earliest_reset.isoformat()`).

---

## File Structure

- `src/ccswitch/selector.py` — pure ranking. Add `Picked` dataclass, `RankedAccount.optimistic`, `AllAccountsBusy.kind`, `unhealthy` + `optimistic_fallback` params on `rank_accounts`, a new `select_picked(...) -> Picked` core, and make `select_best(...) -> str` a thin `select_picked(...).name` shim (legacy behavior preserved).
- `src/ccswitch/ledger.py` — add `get_status(led, name) -> str | None`.
- `src/ccswitch/accounts.py` — `pick_account_detailed(...) -> Picked` (threads `unhealthy`, `optimistic_fallback=True`), `pick_account(...) -> str` thin wrapper.
- `server/sessions/selection.py` — `select_account(...) -> Picked`.
- `server/sessions/types.py` — `SessionRecord.usage_unverified: bool = False` + to_dict/from_dict.
- `server/sessions/orchestrator.py` — set `record.usage_unverified` in create path; use `.name` at the rotation call site.
- `server/api/sessions.py` — `usageUnverified` in 201 body (automatic via `to_dict`); `accounts_need_reauth` 409 via `kind`.
- `web/src/api/types.ts` — `Session.usageUnverified?: boolean`.
- `web/src/components/Dashboard.tsx`, `web/src/components/DesktopShell.tsx` — toast on B/C + auto-clear effect.

> Note: `server/api/accounts.py` is intentionally NOT changed — it keeps the legacy rank display (`optimistic_fallback=False`), so needs-reauth/unreadable accounts keep `rank=null`. The banner auto-clear keys on accounts RECOVERING to readable (`rank !== null`), which legacy ranking already provides.

---

## Task 1: Selector — opt-in optimistic tier, `kind`, `Picked`

**Files:**
- Modify: `src/ccswitch/selector.py`
- Test: `tests/test_selector.py` (append new tests; do NOT alter the existing 24)

**Interfaces:**
- Produces:
  - `class Picked: name: str; optimistic: bool = False` (dataclass)
  - `RankedAccount.optimistic: bool = False` (new field)
  - `AllAccountsBusy(earliest_reset: datetime | None, kind: str = "at_ceiling")` with `.kind` in `{"at_ceiling","no_usable_accounts"}`
  - `rank_accounts(usages, reserved, cfg, now, exclude=frozenset(), load=None, unhealthy=frozenset(), optimistic_fallback=False) -> tuple[list[RankedAccount], datetime | None]`. When `optimistic_fallback=False` the behavior is byte-for-byte the legacy behavior (None usage excluded, no optimistic tier). When `True`, accounts with `u is None and name not in unhealthy/exclude/reserved` are appended as a last-resort tier (ranked strictly after every readable candidate, ordered by least `load`, each `optimistic=True`).
  - `select_picked(usages, reserved, cfg, now, exclude=frozenset(), load=None, unhealthy=frozenset(), optimistic_fallback=False) -> Picked` — returns `Picked(ranked[0].name, ranked[0].optimistic)`; raises `AllAccountsBusy(earliest, kind)` when `ranked` is empty (`kind="at_ceiling"` if `earliest is not None` else `"no_usable_accounts"`).
  - `select_best(usages, reserved, cfg, now, exclude=frozenset(), load=None) -> str` — now `return select_picked(usages, reserved, cfg, now, exclude, load).name` (default `optimistic_fallback=False`; identical observable behavior to today).

- [ ] **Step 1: Write the failing tests** (append to `tests/test_selector.py`, pytest-style like the existing `rank_accounts` tests at the bottom of the file; reuse the module-level `NOW`, `SOON`, `_cfg`)

```python
# --- optimistic fallback + kind (new) ---

def test_select_picked_readable_is_not_optimistic():
    from ccswitch.selector import select_picked, Picked
    usages = {"a": Usage(10.0, SOON, 5.0, SOON, False)}
    assert select_picked(usages, set(), _cfg(), NOW) == Picked(name="a", optimistic=False)


def test_optimistic_fallback_off_excludes_none_usage():
    # Legacy behavior: with the flag off, an all-None set has no candidates.
    from ccswitch.selector import rank_accounts
    ranked, earliest = rank_accounts({"a": None, "b": None}, set(), _cfg(), NOW)
    assert ranked == []
    assert earliest is None


def test_optimistic_fallback_picks_least_load_when_all_unreadable():
    from ccswitch.selector import select_picked, Picked
    picked = select_picked({"a": None, "b": None}, set(), _cfg(), NOW,
                           load={"a": 2.0, "b": 0.0}, optimistic_fallback=True)
    assert picked == Picked(name="b", optimistic=True)


def test_optimistic_fallback_skips_unhealthy():
    from ccswitch.selector import select_picked, AllAccountsBusy
    import pytest
    with pytest.raises(AllAccountsBusy) as ei:
        select_picked({"a": None}, set(), _cfg(), NOW,
                      unhealthy=frozenset({"a"}), optimistic_fallback=True)
    assert ei.value.kind == "no_usable_accounts"
    assert ei.value.earliest_reset is None


def test_readable_outranks_optimistic():
    from ccswitch.selector import select_picked, Picked
    picked = select_picked({"a": Usage(10.0, SOON, 5.0, SOON, False), "b": None},
                           set(), _cfg(), NOW, optimistic_fallback=True)
    assert picked == Picked(name="a", optimistic=False)


def test_mixed_ceiling_plus_unreadable_picks_optimistic():
    from ccswitch.selector import select_picked, Picked
    reset = NOW + timedelta(hours=2)
    picked = select_picked({"a": Usage(100.0, reset, 0.0, None, False), "b": None},
                           set(), _cfg(), NOW, optimistic_fallback=True)
    assert picked == Picked(name="b", optimistic=True)


def test_all_at_ceiling_raises_at_ceiling_kind():
    from ccswitch.selector import select_picked, AllAccountsBusy
    import pytest
    reset = NOW + timedelta(hours=1)
    with pytest.raises(AllAccountsBusy) as ei:
        select_picked({"x": Usage(100.0, reset, 0.0, None, False)}, set(), _cfg(), NOW,
                      optimistic_fallback=True)
    assert ei.value.kind == "at_ceiling"
    assert ei.value.earliest_reset == reset


def test_optimistic_ranked_after_readable_in_list():
    from ccswitch.selector import rank_accounts
    ranked, _ = rank_accounts({"a": Usage(10.0, SOON, 5.0, SOON, False), "b": None},
                              set(), _cfg(), NOW, optimistic_fallback=True)
    assert [r.name for r in ranked] == ["a", "b"]
    assert ranked[0].optimistic is False and ranked[1].optimistic is True
```

- [ ] **Step 2: Run the new tests to verify they fail**

Run: `PYTHONPATH=$PWD/src <VENV> -m pytest tests/test_selector.py -k "optimistic or select_picked or at_ceiling_kind or readable_outranks or mixed_ceiling" -v`
Expected: FAIL — `ImportError: cannot import name 'Picked'` / `select_picked` missing / `rank_accounts() got an unexpected keyword 'optimistic_fallback'`.

- [ ] **Step 3: Implement the selector changes** in `src/ccswitch/selector.py`

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

Add `kind` to the exception (keep the existing message + `earliest_reset`):

```python
class AllAccountsBusy(Exception):
    def __init__(self, earliest_reset: datetime | None, kind: str = "at_ceiling"):
        super().__init__("all accounts at/above usage ceiling")
        self.earliest_reset = earliest_reset
        self.kind = kind
```

In `rank_accounts`, add the two new params and the opt-in optimistic tier. Replace the signature and the loop/return (keep `_perishability`/`_reason` usage identical):

```python
def rank_accounts(usages, reserved, cfg, now, exclude=frozenset(),
                  load=None, unhealthy=frozenset(), optimistic_fallback=False):
    load = load or {}
    ceiling = cfg.hard_ceiling * 100.0
    candidates: list[tuple[float, float, float, float, str]] = []
    optimistic: list[tuple[float, str]] = []   # (load, name) — last-resort tier
    busy_resets: list[datetime] = []
    for name, u in usages.items():
        if name in exclude or name in reserved:
            continue
        if u is None:
            # Usage unreadable. Only an opt-in caller treats a healthy account as a
            # last-resort optimistic pick; legacy callers exclude it entirely.
            if optimistic_fallback and name not in unhealthy:
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

Add `select_picked` and rewrite `select_best` to delegate (replace the existing `select_best`):

```python
def select_picked(usages, reserved, cfg, now, exclude=frozenset(),
                  load=None, unhealthy=frozenset(), optimistic_fallback=False) -> Picked:
    ranked, earliest = rank_accounts(usages, reserved, cfg, now, exclude, load,
                                     unhealthy, optimistic_fallback)
    if not ranked:
        kind = "at_ceiling" if earliest is not None else "no_usable_accounts"
        raise AllAccountsBusy(earliest, kind)
    top = ranked[0]
    return Picked(name=top.name, optimistic=top.optimistic)


def select_best(usages, reserved, cfg, now, exclude=frozenset(), load=None) -> str:
    return select_picked(usages, reserved, cfg, now, exclude, load).name
```

- [ ] **Step 4: Run the full selector suite (new + existing 24)**

Run: `PYTHONPATH=$PWD/src <VENV> -m pytest tests/test_selector.py -v`
Expected: PASS — all 24 existing + 8 new.

- [ ] **Step 5: Commit**

```bash
git add src/ccswitch/selector.py tests/test_selector.py
git commit -m "feat(selector): opt-in optimistic tier + AllAccountsBusy.kind + Picked

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 2: Ledger status getter + `pick_account_detailed`

**Files:**
- Modify: `src/ccswitch/ledger.py`, `src/ccswitch/accounts.py`
- Test: `tests/test_pick_account_detailed.py` (create)

**Interfaces:**
- Consumes: `Picked`, `select_picked(..., unhealthy=..., optimistic_fallback=True)` from Task 1.
- Produces:
  - `ledger.get_status(led: dict, name: str) -> str | None`
  - `accounts.pick_account_detailed(cfg, exclude=frozenset(), load=None) -> Picked` — builds `usages`, `reserved`, and `unhealthy = {n for n in names if get_status(led, n) == "needs-reauth"}`; calls `select_picked(..., unhealthy=unhealthy, optimistic_fallback=True)`; reserves the picked account; returns `Picked`.
  - `accounts.pick_account(cfg, exclude=frozenset(), load=None) -> str` — returns `pick_account_detailed(...).name` (unchanged contract for `launcher.py`, `cli.py`, `runner.py`).

- [ ] **Step 1: Write the failing test** — create `tests/test_pick_account_detailed.py`

```python
from unittest.mock import patch
from ccswitch.config import Config
from ccswitch.selector import Picked
from ccswitch import accounts


def test_pick_account_detailed_optimistic_when_usage_none(monkeypatch):
    monkeypatch.setattr(accounts, "_account_names", lambda: ["a", "b"])
    with patch.object(accounts, "usage_for", return_value=None), \
         patch("ccswitch.ledger.get_status", return_value="ok"), \
         patch("ccswitch.ledger.reservation_active", return_value=False), \
         patch("ccswitch.ledger.reserve"):
        picked = accounts.pick_account_detailed(Config(), load={"a": 1.0, "b": 0.0})
    assert isinstance(picked, Picked)
    assert picked == Picked(name="b", optimistic=True)


def test_pick_account_detailed_skips_needs_reauth(monkeypatch):
    from ccswitch.selector import AllAccountsBusy
    import pytest
    monkeypatch.setattr(accounts, "_account_names", lambda: ["a"])
    with patch.object(accounts, "usage_for", return_value=None), \
         patch("ccswitch.ledger.get_status", return_value="needs-reauth"), \
         patch("ccswitch.ledger.reservation_active", return_value=False), \
         patch("ccswitch.ledger.reserve"):
        with pytest.raises(AllAccountsBusy) as ei:
            accounts.pick_account_detailed(Config())
    assert ei.value.kind == "no_usable_accounts"


def test_pick_account_str_wrapper_returns_name(monkeypatch):
    monkeypatch.setattr(accounts, "_account_names", lambda: ["a"])
    with patch.object(accounts, "usage_for", return_value=None), \
         patch("ccswitch.ledger.get_status", return_value="ok"), \
         patch("ccswitch.ledger.reservation_active", return_value=False), \
         patch("ccswitch.ledger.reserve"):
        assert accounts.pick_account(Config()) == "a"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=$PWD/src <VENV> -m pytest tests/test_pick_account_detailed.py -v`
Expected: FAIL — `module 'ccswitch.accounts' has no attribute 'pick_account_detailed'` / `ledger.get_status` missing.

- [ ] **Step 3: Implement**

In `src/ccswitch/ledger.py`, add next to `set_status`:

```python
def get_status(led: dict, name: str) -> str | None:
    return _acct(led, name).get("status")
```

In `src/ccswitch/accounts.py`, update the import (line 13) and replace `pick_account` (lines 193-203):

```python
from .selector import Picked, select_picked  # was: from .selector import select_best
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
        picked = select_picked(usages, reserved, cfg, now, exclude, load or {},
                               unhealthy, optimistic_fallback=True)
        ledger.reserve(led, picked.name, now_ts + cfg.reservation_ttl_sec)
    return picked


def pick_account(cfg: Config, exclude: set[str] = frozenset(),
                 load: dict[str, float] | None = None) -> str:
    return pick_account_detailed(cfg, exclude, load).name
```

> Check: if `select_best` is no longer imported/used anywhere in `accounts.py`, that's expected — `select_best` stays in `selector.py` for the existing selector tests and as the legacy string API.

- [ ] **Step 4: Run new tests + regressions**

Run: `PYTHONPATH=$PWD/src <VENV> -m pytest tests/test_pick_account_detailed.py tests/test_selector.py tests/test_accounts.py server/tests/test_selection.py -q`
Expected: PASS (the `test_selection.py` suite exercises `pick_account` end-to-end through the new path).

- [ ] **Step 5: Commit**

```bash
git add src/ccswitch/ledger.py src/ccswitch/accounts.py tests/test_pick_account_detailed.py
git commit -m "feat(accounts): pick_account_detailed threads unhealthy + returns Picked

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 3: Propagate `optimistic` through orchestrator + `SessionRecord`

**Files:**
- Modify: `server/sessions/selection.py`, `server/sessions/types.py`, `server/sessions/orchestrator.py`
- Test: `server/tests/test_types_usage_unverified.py` (create)

**Interfaces:**
- Consumes: `Picked` (Task 1); `pick_account_detailed` (Task 2).
- Produces:
  - `server.sessions.selection.select_account(exclude=frozenset(), load=None) -> Picked`
  - `SessionRecord.usage_unverified: bool = False`, round-tripped through `to_dict` (`"usageUnverified"`) / `from_dict`.
  - `orchestrator.create_session(...)` sets `record.usage_unverified = picked.optimistic` when it selected the account itself (account not supplied by the caller).

- [ ] **Step 1: Write the failing test** — create `server/tests/test_types_usage_unverified.py`

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

Run: `PYTHONPATH=$PWD/src <VENV> -m pytest server/tests/test_types_usage_unverified.py -v`
Expected: FAIL — `TypeError: unexpected keyword argument 'usage_unverified'`.

- [ ] **Step 3: Implement the record field** in `server/sessions/types.py`

Add the field after `active_goal` (line 37):

```python
    active_goal: str | None = None    # active /goal condition; None = no goal
    usage_unverified: bool = False    # transient: account picked without a usage read (optimistic)
```

Add to `to_dict`'s returned dict (after `"activeGoal": self.active_goal,`):

```python
            "usageUnverified": self.usage_unverified,
```

Add to `from_dict`'s constructor call (after `active_goal=d.get("activeGoal"),`):

```python
            usage_unverified=d.get("usageUnverified", False),
```

- [ ] **Step 4: Update `select_account` to return `Picked`** in `server/sessions/selection.py`

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

- [ ] **Step 5: Update the two orchestrator call sites** in `server/sessions/orchestrator.py`

Update the import (line 47):

```python
from server.sessions.selection import AllAccountsBusy, Picked, select_account  # noqa: F401
```

Create path (lines 344-352) — capture the optimistic flag (read `server/sessions/orchestrator.py` around there to place it correctly):

```python
        usage_unverified = False
        if account is None:
            picked = select_account(
                exclude=self._recovery.quarantined(now=self._clock()),
                load=self.live_session_load())
            account = picked.name
            usage_unverified = picked.optimistic
        else:
            # Verify the given account exists (will raise if cred file missing — acceptable)
            pass
```

Then in the `SessionRecord(` construction inside `create_session` (around line 412), pass `usage_unverified=usage_unverified`.

Rotation path (line 1646) — selection returns `Picked` now, take `.name`:

```python
            new_account = select_account(exclude=exclude, load=self.live_session_load()).name
```

- [ ] **Step 6: Run tests**

Run: `PYTHONPATH=$PWD/src <VENV> -m pytest server/tests/test_types_usage_unverified.py server/tests/test_session_repo.py server/tests/test_orchestrator_worktree.py server/tests/test_selection.py -q`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add server/sessions/types.py server/sessions/selection.py server/sessions/orchestrator.py server/tests/test_types_usage_unverified.py
git commit -m "feat(orchestrator): carry optimistic pick as SessionRecord.usage_unverified

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 4: API — `usageUnverified` body + `accounts_need_reauth` 409

**Files:**
- Modify: `server/api/sessions.py`
- Test: `server/tests/test_api.py`

**Interfaces:**
- Consumes: `AllAccountsBusy.kind` (Task 1), `record.usage_unverified` (Task 3).
- Produces:
  - `POST /api/sessions` 201 body includes `usageUnverified` (already flows via `_record_to_dict` → `to_dict`; the test asserts it).
  - `AllAccountsBusy` with `kind=="no_usable_accounts"` → `409 { error.code: "accounts_need_reauth" }`; `kind=="at_ceiling"` → existing `all_accounts_limited` + `earliestReset`.

- [ ] **Step 1: Read the existing all-busy tests** at `server/tests/test_api.py:377-438`. They currently patch `server.sessions.selection.pick_account`. Update those patches to `pick_account_detailed` and have the mock raise `AllAccountsBusy(reset, kind="at_ceiling")` (the `at_ceiling`/`null-reset` variants stay, asserting `all_accounts_limited` + `earliestReset` as before).

- [ ] **Step 2: Write the new failing tests** — add to `server/tests/test_api.py` (mirror the existing test class's client/auth fixtures):

```python
def test_create_optimistic_sets_usage_unverified(self, ...):
    """Usage unreadable but account healthy → 201 with usageUnverified true."""
    from ccswitch.selector import Picked
    with patch("server.sessions.selection.pick_account_detailed",
               return_value=Picked(name="acct-a", optimistic=True)):
        resp = <client POST /api/sessions with a valid body>
    assert resp.status_code == 201
    assert resp.json()["usageUnverified"] is True


def test_create_all_unusable_returns_needs_reauth(self, ...):
    """All accounts need reauth → 409 accounts_need_reauth, no earliestReset."""
    from server.sessions.selection import AllAccountsBusy
    with patch("server.sessions.selection.pick_account_detailed",
               side_effect=AllAccountsBusy(None, kind="no_usable_accounts")):
        resp = <client POST /api/sessions with a valid body>
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "accounts_need_reauth"
```

(Use the same request-construction + auth headers as the neighboring `test_create_session_all_busy_*` tests — copy their fixture usage verbatim.)

- [ ] **Step 3: Run the tests to verify they fail**

Run: `PYTHONPATH=$PWD/src <VENV> -m pytest server/tests/test_api.py -k "optimistic or needs_reauth or all_busy" -v`
Expected: FAIL — `accounts_need_reauth` not produced; `pick_account_detailed` patch target not yet used by the code.

- [ ] **Step 4: Implement the API branch** in `server/api/sessions.py` — replace the `except AllAccountsBusy` block (lines 315-327):

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

(The 201 body already carries `usageUnverified` because `_record_to_dict` delegates to `SessionRecord.to_dict`. No change needed there.)

- [ ] **Step 5: Run tests to verify they pass**

Run: `PYTHONPATH=$PWD/src <VENV> -m pytest server/tests/test_api.py -v`
Expected: PASS.

- [ ] **Step 6: Backend lint**

Run: `ruff check server/ src/ && PYTHONPATH=$PWD/src <VENV> -m mypy server/api/sessions.py server/sessions/selection.py src/ccswitch/selector.py src/ccswitch/accounts.py`
Expected: clean.

- [ ] **Step 7: Commit**

```bash
git add server/api/sessions.py server/tests/test_api.py
git commit -m "feat(api): usageUnverified on create + accounts_need_reauth 409

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 5: Frontend types + create-response field

**Files:**
- Modify: `web/src/api/types.ts`
- Test: covered by Task 6's component tests (a one-line optional field).

**Interfaces:**
- Produces: `Session.usageUnverified?: boolean`.

- [ ] **Step 1: Add the field** in `web/src/api/types.ts`, inside `interface Session`:

```ts
  /** True when the session was created without a verified usage read (optimistic pick). */
  usageUnverified?: boolean
```

- [ ] **Step 2: Verify it type-checks** — run in `web/`: `./node_modules/.bin/tsc -b`
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
- Consumes: `Session.usageUnverified` (Task 5); 409 `error.code` values `all_accounts_limited` / `accounts_need_reauth` (Task 4); `useAccounts()` returning `Account[]` with `rank: number | null`; `toast({ message, variant })` from `@/lib/toast` (variants include `info`, `success`, `error`, `warning`).

- [ ] **Step 1: Read the existing test files** to copy their `@/api/hooks` + `@/lib/toast` mock-factory patterns (these tests mock hooks via `vi.mock`). Then add three cases to EACH of `Dashboard.test.tsx` and `DesktopShell.test.tsx`:

```tsx
it('shows a subtle toast when a session is created optimistically', async () => {
  // createSession mock resolves with a session where usageUnverified === true
  // assert toast() called with a "started anyway" message (variant 'warning')
})

it('routes accounts_need_reauth 409 to a toast, not the limit banner', async () => {
  // createSession mock rejects with { status: 409, body: { error: { code: 'accounts_need_reauth' } } }
  // assert toast() called; assert AllLimitedBanner is NOT in the document
})

it('auto-clears the all-limited banner once an account becomes selectable', async () => {
  // 1) drive an all_accounts_limited 409 → AllLimitedBanner shows
  // 2) re-render with useAccounts returning an account whose rank === 1
  // 3) assert AllLimitedBanner is removed
})
```

- [ ] **Step 2: Run tests to verify they fail** — run in `web/`:
`./node_modules/.bin/vitest run src/components/__tests__/Dashboard.test.tsx src/components/__tests__/DesktopShell.test.tsx`
Expected: FAIL.

- [ ] **Step 3: Implement in `Dashboard.tsx`**

`onSuccess` of `createSession` (inside `handleCreateSubmit`, ~line 1312):

```tsx
      onSuccess: (session) => {
        dismissNewDialog()
        if (session.usageUnverified) {
          toast({ message: "Couldn't verify account usage — started anyway.", variant: 'warning' })
        }
        if (onOpenSession) onOpenSession(session.id, input.initialPrompt)
      },
```

`onError` 409 handler (~line 1318) — add the `accounts_need_reauth` branch:

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

- [ ] **Step 4: Implement the same three changes in `DesktopShell.tsx`** — optimistic toast in `onSuccess` (~line 290), `accounts_need_reauth` branch in the 409 `onError` (~line 294, which currently only special-cases `branch_switch_failed`), and the auto-clear `useEffect` using `accounts` (line 112) and `allLimitedReset` (line 133). Add the `toast` import if missing.

- [ ] **Step 5: Run tests to verify they pass** — run in `web/`:
`./node_modules/.bin/vitest run src/components/__tests__/Dashboard.test.tsx src/components/__tests__/DesktopShell.test.tsx`
Expected: PASS.

- [ ] **Step 6: Full web gates** — run in `web/`:
`./node_modules/.bin/vitest run && ./node_modules/.bin/tsc -b && ./node_modules/.bin/eslint src --max-warnings 0`
Expected: clean. (Watch react-hooks `exhaustive-deps`: the effect deps are `[accounts, allLimitedReset]`.)

- [ ] **Step 7: Commit**

```bash
git add web/src/components/Dashboard.tsx web/src/components/DesktopShell.tsx web/src/components/__tests__/Dashboard.test.tsx web/src/components/__tests__/DesktopShell.test.tsx
git commit -m "feat(web): optimistic toast + accounts_need_reauth + auto-clear all-limited banner

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Final verification (before PR)

- [ ] Backend: `PYTHONPATH=$PWD/src <VENV> -m pytest tests/ server/tests/ -q`
- [ ] Backend lint: `ruff check server/ src/ && PYTHONPATH=$PWD/src <VENV> -m mypy server/ src/ccswitch`
- [ ] Web: in `web/` → `./node_modules/.bin/vitest run && ./node_modules/.bin/tsc -b && ./node_modules/.bin/eslint src --max-warnings 0`

---

## Spec coverage check

- State A (at ceiling) → Task 1 (`kind="at_ceiling"` + earliest), Task 4 (`all_accounts_limited`), Task 6 (banner + auto-clear). ✓
- State B (usage unreadable) → Task 1 (opt-in optimistic tier), Task 2 (`unhealthy` gating + `optimistic_fallback=True`), Task 3 (`usage_unverified`), Task 4 (201 body), Task 5/6 (toast). ✓
- State C (needs reauth) → Task 1 (`no_usable_accounts`), Task 2 (`unhealthy` set), Task 4 (`accounts_need_reauth`), Task 6 (toast → Accounts tab). ✓
- Auto-clear on recovery → Task 6 effect on `useAccounts().rank` (legacy ranking already yields `rank !== null` on recovery — no accounts-API change needed). ✓
- Scoping: optimistic behavior is opt-in (`optimistic_fallback`), so CLI/launcher/runner and the accounts list keep legacy behavior; the existing 24 selector tests stay green. ✓
- No new SSE / no rotation-recovery changes / no usage-fetch retry → respected (rotation path only adjusted for the `.name` return shape). ✓

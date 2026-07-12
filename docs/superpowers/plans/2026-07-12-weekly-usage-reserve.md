# Per-Account Weekly Usage Reserve Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let each account carry an optional `weeklyReservePct`; once its weekly usage reaches that value the selector demotes it to a least-used last-resort tier (soft fallback), configurable via PATCH API and the AccountsManager UI.

**Architecture:** The setting lives in the flock-guarded ledger (`~/.ccswitch/store/ledger.json`) next to `nickname`. Enforcement is in the pure `rank_accounts()` selector: over-reserve accounts are appended as a final tier (after normal candidates and the optimistic tier), ordered by lowest `weekly_pct`. Both real selection (`pick_account_detailed`) and the dashboard display ranking (`GET /api/accounts`) pass the ledger's reserves in.

**Tech Stack:** Python (stdlib + FastAPI/pydantic), pytest; React + TS + TanStack Query, vitest.

**Spec:** `docs/superpowers/specs/2026-07-12-weekly-usage-reserve-design.md`

## Global Constraints

- Wire/ledger field name is exactly `weeklyReservePct` everywhere (ledger JSON, API JSON, TS type).
- Demotion threshold is inclusive: `weekly_pct >= reserve` blocks.
- Fallback-tier reason string is exactly `"over weekly reserve — fallback"` (em dash, like the existing `"fresh quota — saved for last"`).
- API validation: `1 <= weeklyReservePct <= 100` (pydantic `Field(ge=1.0, le=100.0)` → 422); explicit `null` clears; field absent from PATCH body = untouched (`model_fields_set`).
- Non-numeric/corrupt ledger values are treated as unset (never raise).
- Python tests run with: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 PYTHONPATH=.:src python3 -m pytest <paths> -q` from the repo root (no venv in this worktree — system python3 has all deps).
- Web tests run in `web/`: `npx vitest run <file>`, `npx tsc --noEmit`, `npx eslint .` (node_modules already installed).
- Branch: work is committed on `feat/weekly-usage-reserve` (already checked out). Commit after every task.

---

### Task 1: Ledger field + accounts-module wrappers

**Files:**
- Modify: `src/ccswitch/ledger.py` (append after `set_nickname`, ~line 125)
- Modify: `src/ccswitch/accounts.py` (`list_accounts` ~line 458, new wrapper after `set_nickname` ~line 475)
- Test: `tests/test_ledger.py` (append)

**Interfaces:**
- Produces: `ledger.set_weekly_reserve(led: dict, name: str, pct: float | None) -> None`; `ledger.get_weekly_reserve(led: dict, name: str) -> float | None`; `ledger.weekly_reserves(led: dict) -> dict[str, float]`; `accounts.set_weekly_reserve(name: str, pct: float | None) -> None`; `accounts.list_accounts()` entries gain key `"weeklyReservePct"` (float or None).

- [ ] **Step 1: Write the failing tests** — append to `tests/test_ledger.py`:

```python
# --- weekly reserve (per-account) ---


def test_weekly_reserve_set_read_clear():
    with temp_env():
        from ccswitch import ledger
        with ledger.locked_ledger() as led:
            ledger.set_weekly_reserve(led, "a", 90.0)
        with ledger.locked_ledger() as led:
            assert ledger.get_weekly_reserve(led, "a") == 90.0
            assert ledger.weekly_reserves(led) == {"a": 90.0}
            ledger.set_weekly_reserve(led, "a", None)
        with ledger.locked_ledger() as led:
            assert ledger.get_weekly_reserve(led, "a") is None
            assert ledger.weekly_reserves(led) == {}
            assert "weeklyReservePct" not in led["accounts"]["a"]


def test_weekly_reserve_non_numeric_treated_as_unset():
    with temp_env():
        from ccswitch import ledger
        with ledger.locked_ledger() as led:
            led["accounts"]["junk"] = {"weeklyReservePct": "ninety"}
            led["accounts"]["boolish"] = {"weeklyReservePct": True}
            assert ledger.get_weekly_reserve(led, "junk") is None
            assert ledger.get_weekly_reserve(led, "boolish") is None
            assert ledger.weekly_reserves(led) == {}


def test_list_accounts_includes_weekly_reserve():
    with temp_env():
        from ccswitch import paths, accounts
        store = paths.store_dir()
        store.mkdir(parents=True, exist_ok=True)
        (store / "a.cred.json").write_text(
            '{"accessToken":"t","refreshToken":"r","expiresAt":9999999999999}')
        accounts.set_weekly_reserve("a", 88.0)
        [acct] = accounts.list_accounts()
        assert acct["weeklyReservePct"] == 88.0
        accounts.set_weekly_reserve("a", None)
        [acct] = accounts.list_accounts()
        assert acct["weeklyReservePct"] is None
```

If `tests/test_ledger.py` does not already import `temp_env`, add `from tests.helpers import temp_env` at the top (check first — it likely does).

- [ ] **Step 2: Run tests to verify they fail**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 PYTHONPATH=.:src python3 -m pytest tests/test_ledger.py -q`
Expected: the three new tests FAIL with `AttributeError: ... has no attribute 'set_weekly_reserve'`; all pre-existing tests PASS.

- [ ] **Step 3: Implement** — in `src/ccswitch/ledger.py`, append after `set_nickname`:

```python
def set_weekly_reserve(led: dict, name: str, pct: float | None) -> None:
    """Set or clear the per-account weekly usage reserve threshold (0–100 pct)."""
    acct = _acct(led, name)
    if pct is None:
        acct.pop("weeklyReservePct", None)
    else:
        acct["weeklyReservePct"] = float(pct)


def get_weekly_reserve(led: dict, name: str) -> float | None:
    """Read one account's reserve; anything non-numeric is treated as unset."""
    pct = led["accounts"].get(name, {}).get("weeklyReservePct")
    if isinstance(pct, bool) or not isinstance(pct, (int, float)):
        return None
    return float(pct)


def weekly_reserves(led: dict) -> dict[str, float]:
    """All configured reserves, keyed by account name (unset accounts omitted)."""
    out: dict[str, float] = {}
    for name in led["accounts"]:
        pct = get_weekly_reserve(led, name)
        if pct is not None:
            out[name] = pct
    return out
```

In `src/ccswitch/accounts.py`:

1. In `list_accounts()`, add one key to the appended dict (after `"nickname"`):

```python
                "weeklyReservePct": ledger.get_weekly_reserve(led, name),
```

2. After the existing `set_nickname` wrapper, add:

```python
def set_weekly_reserve(name: str, pct: float | None) -> None:
    with ledger.locked_ledger() as led:
        ledger.set_weekly_reserve(led, name, pct)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 PYTHONPATH=.:src python3 -m pytest tests/test_ledger.py tests/test_accounts.py -q`
Expected: PASS (all).

- [ ] **Step 5: Lint + typecheck**

Run: `python3 -m ruff check src/ tests/ && python3 -m mypy server/ --ignore-missing-imports`
Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add src/ccswitch/ledger.py src/ccswitch/accounts.py tests/test_ledger.py
git commit -m "feat(ledger): per-account weeklyReservePct field + accessors"
```

---

### Task 2: Selector — demote over-reserve accounts to a last-resort tier

**Files:**
- Modify: `src/ccswitch/selector.py` (`rank_accounts` lines 84–141, `select_picked` 144–155, `select_best` 158–161)
- Test: `tests/test_selector.py` (append)

**Interfaces:**
- Consumes: nothing new (reserves dict is passed by callers; wired in Task 3).
- Produces: `rank_accounts(..., reserves: Mapping[str, float] | None = None)`, same for `select_picked` / `select_best`. Over-reserve accounts appear LAST in the ranked list (after normal candidates AND the optimistic tier), ordered by ascending `weekly_pct`, with `reason="over weekly reserve — fallback"` and `optimistic=False`.

- [ ] **Step 1: Write the failing tests** — append to `tests/test_selector.py` (module-level pytest style; `Usage`, `NOW`, `SOON`, `_cfg` already exist at the bottom of the file):

```python
# --- per-account weekly reserve (soft fallback tier) ---

LATER = NOW + timedelta(hours=5)


def test_reserve_demotes_over_reserve_account_to_last():
    from ccswitch.selector import rank_accounts
    # Perishability = binding-dimension headroom / hours-to-reset (higher = pick first).
    usages = {
        "hot": Usage(10.0, SOON, 91.0, SOON, False),    # weekly binding: 9%/0.5h = 18/h
        "cool": Usage(10.0, LATER, 20.0, LATER, False),  # weekly binding: 80%/5h = 16/h
    }
    # WITHOUT a reserve, hot is the more perishable account and wins…
    ranked_no_reserve, _ = rank_accounts(usages, set(), _cfg(), NOW)
    assert [r.name for r in ranked_no_reserve] == ["hot", "cool"]
    # …so the flip below is attributable to the reserve alone.
    ranked, _ = rank_accounts(usages, set(), _cfg(), NOW, reserves={"hot": 90.0})
    assert [r.name for r in ranked] == ["cool", "hot"]
    assert ranked[1].reason == "over weekly reserve — fallback"
    assert ranked[1].optimistic is False
    assert [r.rank for r in ranked] == [1, 2]


def test_reserve_boundary_is_inclusive():
    from ccswitch.selector import rank_accounts
    # "at" (20/h) outranks "under" (2.02/h) without reserves; with weekly_pct == reserve
    # it must be blocked (>= semantics), flipping the order.
    usages = {
        "at": Usage(10.0, SOON, 90.0, SOON, False),      # exactly at reserve → blocked
        "under": Usage(10.0, LATER, 89.9, LATER, False),  # just under → normal candidate
    }
    ranked_no_reserve, _ = rank_accounts(usages, set(), _cfg(), NOW)
    assert [r.name for r in ranked_no_reserve] == ["at", "under"]
    ranked, _ = rank_accounts(usages, set(), _cfg(), NOW,
                              reserves={"at": 90.0, "under": 90.0})
    assert [r.name for r in ranked] == ["under", "at"]
    assert ranked[1].reason == "over weekly reserve — fallback"


def test_reserve_soft_fallback_picks_least_weekly_used():
    from ccswitch.selector import select_picked, Picked
    usages = {
        "worse": Usage(10.0, SOON, 97.0, SOON, False),
        "better": Usage(10.0, SOON, 92.0, SOON, False),
    }
    picked = select_picked(usages, set(), _cfg(), NOW,
                           reserves={"worse": 90.0, "better": 90.0})
    assert picked == Picked(name="better", optimistic=False)


def test_reserve_hard_ceiling_still_excludes_outright():
    from ccswitch.selector import rank_accounts
    reset = NOW + timedelta(hours=2)
    usages = {
        "maxed": Usage(10.0, SOON, 100.0, reset, False),  # at ceiling — NOT fallback
        "ok": Usage(10.0, SOON, 5.0, SOON, False),
    }
    ranked, earliest = rank_accounts(usages, set(), _cfg(), NOW,
                                     reserves={"maxed": 90.0})
    assert [r.name for r in ranked] == ["ok"]
    assert earliest == reset


def test_optimistic_tier_outranks_reserve_blocked():
    from ccswitch.selector import rank_accounts
    usages = {"r": Usage(10.0, SOON, 95.0, SOON, False), "o": None}
    ranked, _ = rank_accounts(usages, set(), _cfg(), NOW,
                              optimistic_fallback=True, reserves={"r": 90.0})
    assert [r.name for r in ranked] == ["o", "r"]


def test_no_reserves_behavior_unchanged():
    from ccswitch.selector import rank_accounts
    usages = {
        "a": Usage(40.0, SOON, 20.0, SOON, False),
        "b": Usage(10.0, SOON, 95.0, SOON, False),
    }
    base, _ = rank_accounts(usages, set(), _cfg(), NOW)
    empty, _ = rank_accounts(usages, set(), _cfg(), NOW, reserves={})
    under, _ = rank_accounts(usages, set(), _cfg(), NOW, reserves={"a": 99.0})
    assert [r.name for r in base] == [r.name for r in empty] == [r.name for r in under]
```

Note `timedelta` is already imported at the top of the file.

- [ ] **Step 2: Run tests to verify they fail**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 PYTHONPATH=.:src python3 -m pytest tests/test_selector.py -q`
Expected: new tests FAIL with `TypeError: rank_accounts() got an unexpected keyword argument 'reserves'`; existing tests PASS.

- [ ] **Step 3: Implement** — in `src/ccswitch/selector.py`:

1. Change the imports line 2 to include Mapping:

```python
from collections.abc import Mapping, Set as AbstractSet
```

2. `rank_accounts` signature — add the trailing parameter:

```python
def rank_accounts(usages: dict[str, Usage | None], reserved: AbstractSet[str],
                  cfg: Config, now: datetime, exclude: AbstractSet[str] = frozenset(),
                  load: dict[str, float] | None = None,
                  unhealthy: AbstractSet[str] = frozenset(),
                  optimistic_fallback: bool = False,
                  reserves: Mapping[str, float] | None = None,
                  ) -> tuple[list[RankedAccount], datetime | None]:
```

Append to the docstring body (before the closing `"""`):

```python
    ``reserves`` maps account name -> weekly reserve threshold (0-100 pct). An
    account whose ``weekly_pct`` is at/over its reserve (but under the hard
    ceiling) is demoted to a final last-resort tier — ranked after every normal
    and optimistic candidate, least weekly-used first — so the reserve alone can
    never make selection fail (soft fallback).
```

3. Inside the loop, right after the `at_ceiling` block's `continue` (line 124), insert:

```python
        reserve = (reserves or {}).get(name)
        if reserve is not None and u.weekly_pct >= reserve:
            reserve_blocked.append((u.weekly_pct, name))
            continue
```

and initialize alongside `optimistic` (line 104):

```python
    reserve_blocked: list[tuple[float, str]] = []  # (weekly_pct, name) — final tier
```

4. After the optimistic tier is appended to `ranked` (line 140), add (same `len(ranked) + i + 1` idiom as the optimistic tier — the comprehension is evaluated before `+=` mutates the list, so `len(ranked)` is the pre-append base):

```python
    reserve_blocked.sort()  # least weekly-used first — burn reserved headroom last
    ranked += [
        RankedAccount(name=name, rank=len(ranked) + i + 1,
                      reason="over weekly reserve — fallback")
        for i, (_, name) in enumerate(reserve_blocked)
    ]
```

5. Thread through `select_picked` and `select_best` — each gains `reserves: Mapping[str, float] | None = None` as the final parameter and passes it to the call it wraps (`rank_accounts(..., reserves=reserves)` / `select_picked(..., reserves=reserves)`).

- [ ] **Step 4: Run tests to verify they pass**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 PYTHONPATH=.:src python3 -m pytest tests/test_selector.py tests/test_selector_copilot_parity.py -q`
Expected: PASS (all).

- [ ] **Step 5: Lint + typecheck**

Run: `python3 -m ruff check src/ tests/ && python3 -m mypy server/ --ignore-missing-imports`
Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add src/ccswitch/selector.py tests/test_selector.py
git commit -m "feat(selector): demote over-reserve accounts to least-used fallback tier"
```

---

### Task 3: Wire ledger reserves into real selection (`pick_account_detailed`)

**Files:**
- Modify: `src/ccswitch/accounts.py:402-409` (`pick_account_detailed`)
- Test: `tests/test_pick_account_detailed.py` (append)

**Interfaces:**
- Consumes: `ledger.weekly_reserves(led)` (Task 1), `select_picked(..., reserves=...)` (Task 2).
- Produces: every auto-pick (session spawn, backlog runner, automations — all funnel through `pick_account_detailed`) now respects ledger reserves.

- [ ] **Step 1: Write the failing test** — append to `tests/test_pick_account_detailed.py`:

```python
def test_pick_account_detailed_respects_weekly_reserve(monkeypatch):
    """An over-reserve account is demoted: the pick flips once a reserve is set."""
    from tests.helpers import usage
    monkeypatch.setattr(accounts, "_account_names", lambda: ["hot", "cool"])
    # Both windows reset in 2h (helpers default). Perishability = binding-dimension
    # headroom / hours: hot = 5%/2h = 2.5/h beats cool = 4%/2h = 2/h, so hot wins
    # WITHOUT a reserve — the flip below is attributable to the reserve alone.
    usages = {
        "hot": usage(session=10.0, weekly=95.0),
        "cool": usage(session=10.0, weekly=96.0),
    }
    common = dict(side_effect=lambda n: usages[n])
    with patch.object(accounts, "usage_for", **common), \
         patch("ccswitch.ledger.get_status", return_value="ok"), \
         patch("ccswitch.ledger.reservation_active", return_value=False), \
         patch("ccswitch.ledger.weekly_reserves", return_value={}), \
         patch("ccswitch.ledger.reserve"):
        assert accounts.pick_account_detailed(Config()).name == "hot"
    with patch.object(accounts, "usage_for", **common), \
         patch("ccswitch.ledger.get_status", return_value="ok"), \
         patch("ccswitch.ledger.reservation_active", return_value=False), \
         patch("ccswitch.ledger.weekly_reserves", return_value={"hot": 90.0}), \
         patch("ccswitch.ledger.reserve"):
        assert accounts.pick_account_detailed(Config()).name == "cool"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 PYTHONPATH=.:src python3 -m pytest tests/test_pick_account_detailed.py -q`
Expected: new test FAILS (`pick_account_detailed` never calls `weekly_reserves`, so the pick stays `"hot"` in the second block).

- [ ] **Step 3: Implement** — in `pick_account_detailed` (`src/ccswitch/accounts.py:402`), inside the `locked_ledger` block, read reserves and pass them through:

```python
    with ledger.locked_ledger() as led:
        reserved = {n for n in names if ledger.reservation_active(led, n, now_ts)}
        unhealthy = frozenset(n for n in names if ledger.get_status(led, n) == "needs-reauth")
        reserves = ledger.weekly_reserves(led)
        picked = select_picked(usages, reserved, cfg, now, exclude, load or {},
                               unhealthy, optimistic_fallback=True, reserves=reserves)
        if reserve:
            ledger.reserve(led, picked.name, now_ts + cfg.reservation_ttl_sec)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 PYTHONPATH=.:src python3 -m pytest tests/test_pick_account_detailed.py tests/test_selector.py -q`
Expected: PASS (all).

- [ ] **Step 5: Lint + typecheck**

Run: `python3 -m ruff check src/ tests/ && python3 -m mypy server/ --ignore-missing-imports`
Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add src/ccswitch/accounts.py tests/test_pick_account_detailed.py
git commit -m "feat(accounts): pick_account_detailed honors ledger weekly reserves"
```

---

### Task 4: Server API — PATCH sets/clears reserve; GET exposes it and ranks with it

**Files:**
- Modify: `server/api/accounts.py` (`_account_response` ~line 106, `get_accounts` ~line 157, `refresh_account` ~line 203, PATCH route ~line 257–286)
- Test: `server/tests/test_accounts_weekly_reserve.py` (create)

**Interfaces:**
- Consumes: `accounts.set_weekly_reserve(name, pct)` (Task 1), `list_accounts()` entries' `"weeklyReservePct"` key (Task 1), `rank_accounts(..., reserves=...)` (Task 2).
- Produces: wire field `weeklyReservePct: float | null` on every account response; `PATCH /api/accounts/{name}` accepts `{nickname?, weeklyReservePct?}`.

- [ ] **Step 1: Write the failing tests** — create `server/tests/test_accounts_weekly_reserve.py`:

```python
"""Server tests for the per-account weekly reserve (PATCH + GET exposure + ranking)."""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from tests.helpers import temp_env, usage as make_usage
from server.tests.fakes.fake_factory import fake_sdk_factory

IDENTITY_HEADERS = {"Tailscale-User-Login": "testuser@example.com"}


def _seed_slot(name: str) -> None:
    from ccswitch import paths
    store = paths.store_dir()
    store.mkdir(parents=True, exist_ok=True)
    slot = store / f"{name}.cred.json"
    now_ms = int(time.time() * 1000)
    slot.write_text(
        f'{{"accessToken":"{name}-tok","refreshToken":"{name}-ref",'
        f'"expiresAt":{now_ms + 9_000_000},"subscriptionType":"max"}}'
    )


def _make_client(usage_fn=None):
    from server.sessions.orchestrator import SessionOrchestrator
    from server.app import create_app
    kwargs = dict(sdk_factory=fake_sdk_factory(), poll_interval=9999.0)
    if usage_fn is not None:
        kwargs["usage_fn"] = usage_fn
    orch = SessionOrchestrator(**kwargs)
    app = create_app(orchestrator=orch)
    return TestClient(app, raise_server_exceptions=False)


def test_patch_sets_reserve_and_returns_it():
    with temp_env():
        _seed_slot("alice")
        with patch.dict(os.environ, {"CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:"}):
            client = _make_client()
        with client:
            response = client.patch(
                "/api/accounts/alice",
                json={"weeklyReservePct": 90},
                headers=IDENTITY_HEADERS,
            )
    assert response.status_code == 200
    assert response.json()["weeklyReservePct"] == 90.0


def test_patch_null_clears_reserve():
    with temp_env():
        _seed_slot("bob")
        with patch.dict(os.environ, {"CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:"}):
            client = _make_client()
        with client:
            client.patch("/api/accounts/bob", json={"weeklyReservePct": 85},
                         headers=IDENTITY_HEADERS)
            response = client.patch("/api/accounts/bob", json={"weeklyReservePct": None},
                                    headers=IDENTITY_HEADERS)
    assert response.status_code == 200
    assert response.json()["weeklyReservePct"] is None


def test_patch_out_of_range_reserve_is_422():
    with temp_env():
        _seed_slot("carol")
        with patch.dict(os.environ, {"CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:"}):
            client = _make_client()
        with client:
            low = client.patch("/api/accounts/carol", json={"weeklyReservePct": 0.5},
                               headers=IDENTITY_HEADERS)
            high = client.patch("/api/accounts/carol", json={"weeklyReservePct": 101},
                                headers=IDENTITY_HEADERS)
    assert low.status_code == 422
    assert high.status_code == 422


def test_nickname_patch_preserves_reserve_and_vice_versa():
    with temp_env():
        _seed_slot("dave")
        with patch.dict(os.environ, {"CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:"}):
            client = _make_client()
        with client:
            client.patch("/api/accounts/dave", json={"weeklyReservePct": 75},
                         headers=IDENTITY_HEADERS)
            after_nick = client.patch("/api/accounts/dave", json={"nickname": "Work"},
                                      headers=IDENTITY_HEADERS)
            after_reserve = client.patch("/api/accounts/dave", json={"weeklyReservePct": 80},
                                         headers=IDENTITY_HEADERS)
    assert after_nick.json()["weeklyReservePct"] == 75.0
    assert after_nick.json()["nickname"] == "Work"
    assert after_reserve.json()["nickname"] == "Work"
    assert after_reserve.json()["weeklyReservePct"] == 80.0


def test_get_accounts_includes_reserve_field():
    with temp_env():
        _seed_slot("eve")
        with patch.dict(os.environ, {"CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:"}):
            client = _make_client(usage_fn=lambda name: make_usage(session=10.0))
        with client:
            client.patch("/api/accounts/eve", json={"weeklyReservePct": 90},
                         headers=IDENTITY_HEADERS)
            response = client.get("/api/accounts", headers=IDENTITY_HEADERS)
    assert response.status_code == 200
    [acct] = response.json()
    assert acct["weeklyReservePct"] == 90.0


def test_get_accounts_ranks_over_reserve_account_as_fallback():
    """Display ranking must match real selection: over-reserve → last + fallback reason."""
    def usage_fn(name):
        return make_usage(session=10.0, weekly=95.0 if name == "hot" else 20.0)

    with temp_env():
        _seed_slot("hot")
        _seed_slot("cool")
        with patch.dict(os.environ, {"CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:"}):
            client = _make_client(usage_fn=usage_fn)
        with client:
            client.patch("/api/accounts/hot", json={"weeklyReservePct": 90},
                         headers=IDENTITY_HEADERS)
            response = client.get("/api/accounts", headers=IDENTITY_HEADERS)
    assert response.status_code == 200
    by_name = {a["name"]: a for a in response.json()}
    assert by_name["cool"]["rank"] == 1
    assert by_name["hot"]["rank"] == 2
    assert by_name["hot"]["rankReason"] == "over weekly reserve — fallback"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 PYTHONPATH=.:src python3 -m pytest server/tests/test_accounts_weekly_reserve.py -q`
Expected: FAIL — `weeklyReservePct` missing from responses (KeyError/None) and 422 tests fail (unknown fields are ignored today).

- [ ] **Step 3: Implement** — in `server/api/accounts.py`:

1. Import `Field` and the new setter:

```python
from pydantic import BaseModel, Field
from ccswitch.accounts import (list_accounts, provider_of, remove_account,
                               set_nickname, set_weekly_reserve)
```

2. `_account_response` — add a keyword parameter and field:

```python
def _account_response(
    name: str,
    status_str: str,
    usage,
    nickname: str | None = None,
    live_sessions: dict[str, int] | None = None,
    rank: int | None = None,
    rank_reason: str | None = None,
    weekly_reserve_pct: float | None = None,
) -> dict[str, Any]:
    """Build the account wire format — never includes tokens."""
    return {
        "name": name,
        "status": status_str,
        "usage": _usage_to_dict(usage),
        "nickname": nickname,
        "provider": provider_of(name),
        "liveSessions": live_sessions or {"active": 0, "idle": 0},
        "rank": rank,
        "rankReason": rank_reason,
        "weeklyReservePct": weekly_reserve_pct,
    }
```

3. `get_accounts` — pass reserves into the display ranking and the response. After `accounts_info = list_accounts()` build:

```python
    reserves = {a["name"]: a["weeklyReservePct"] for a in accounts_info
                if a.get("weeklyReservePct") is not None}
```

Add `reserves=reserves` to the existing `rank_accounts(...)` call, and in the response loop add `weekly_reserve_pct=acct.get("weeklyReservePct"),` to the `_account_response(...)` call.

4. `refresh_account` — in the loop that finds the account in `accounts_info`, also capture `weekly_reserve = acct.get("weeklyReservePct")` (initialize `weekly_reserve = None` beside `nickname = None`) and pass `weekly_reserve_pct=weekly_reserve` to `_account_response`.

5. Replace `NicknameBody` + `set_account_nickname` with (same route, renamed handler — delete the old body model, no parallel copy):

```python
class AccountPatchBody(BaseModel):
    nickname: str | None = None
    weeklyReservePct: float | None = Field(default=None, ge=1.0, le=100.0)


@router.patch("/{name}")
async def update_account(
    name: str,
    body: AccountPatchBody,
    _identity: str = Depends(current_identity),
) -> dict:
    """Update per-account settings: nickname and/or weekly usage reserve.

    Fields absent from the body are left untouched; an explicit ``null`` clears.
    A blank or whitespace-only nickname clears the existing nickname.
    Returns 404 if the account name is invalid or slot does not exist.
    """
    _check_name_and_slot(name)

    if "nickname" in body.model_fields_set:
        set_nickname(name, (body.nickname or "").strip() or None)
    if "weeklyReservePct" in body.model_fields_set:
        set_weekly_reserve(name, body.weeklyReservePct)

    for acct in list_accounts():
        if acct["name"] == name:
            return _account_response(
                name, acct.get("status", "unknown"), None, acct.get("nickname"),
                weekly_reserve_pct=acct.get("weeklyReservePct"),
            )
    return _account_response(name, "unknown", None, None)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 PYTHONPATH=.:src python3 -m pytest server/tests/test_accounts_weekly_reserve.py server/tests/test_accounts_nickname.py server/tests/test_accounts_usage_fanout.py server/tests/test_accounts_api_copilot.py -q`
Expected: PASS (all — nickname behavior must be unchanged).

- [ ] **Step 5: Lint + typecheck**

Run: `python3 -m ruff check server/ && python3 -m mypy server/ --ignore-missing-imports`
Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add server/api/accounts.py server/tests/test_accounts_weekly_reserve.py
git commit -m "feat(api): weeklyReservePct on account PATCH/GET; display rank uses reserves"
```

---

### Task 5: Web UI — reserve editor, over-reserve chip, usage-bar marker

**Files:**
- Modify: `web/src/api/types.ts` (`Account` interface, ~line 22)
- Modify: `web/src/api/hooks.ts` (`useUpdateAccountNickname`, ~line 308)
- Modify: `web/src/components/UsageBadge.tsx` (`UsageBadge`, top of file)
- Modify: `web/src/components/AccountsManager.tsx`
- Modify: every test file mocking `useUpdateAccountNickname` (grep in Step 3.5)
- Test: `web/src/components/__tests__/AccountsReserve.test.tsx` (create)

**Interfaces:**
- Consumes: wire field `weeklyReservePct` (Task 4).
- Produces: `Account.weeklyReservePct?: number | null`; hook `useUpdateAccount()` with input `{ name: string; nickname?: string | null; weeklyReservePct?: number | null }` (replaces `useUpdateAccountNickname` — PATCH body contains only the provided fields); `UsageBadge` prop `markerPct?: number | null`.

- [ ] **Step 1: Write the failing tests** — create `web/src/components/__tests__/AccountsReserve.test.tsx`. Copy the entire `vi.mock('@/api/hooks', () => ({...}))` factory and the `beforeEach` mock-return block VERBATIM from `AccountsNickname.test.tsx`, with one change: replace the `useUpdateAccountNickname` entry with `useUpdateAccount`. Then:

```tsx
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Dashboard } from '../Dashboard'
import * as hooks from '@/api/hooks'
import type { Account } from '@/api/types'

// … vi.mock factory + wrapper + makeAccount + switchToAccountsTab copied from
// AccountsNickname.test.tsx (useUpdateAccount in place of useUpdateAccountNickname) …

describe('AccountRow — weekly reserve', () => {
  const updateMutate = vi.fn()

  beforeEach(() => {
    vi.clearAllMocks()
    // … beforeEach block copied from AccountsNickname.test.tsx, with:
    vi.mocked(hooks.useUpdateAccount).mockReturnValue({
      ...NOOP_MUTATION,
      mutate: updateMutate,
    } as unknown as ReturnType<typeof hooks.useUpdateAccount>)
  })

  it('edit mode shows a reserve input prefilled with the current value', async () => {
    const user = userEvent.setup()
    vi.mocked(hooks.useAccounts).mockReturnValue({
      data: [makeAccount({ name: 'work', weeklyReservePct: 90 })],
      isLoading: false,
    } as ReturnType<typeof hooks.useAccounts>)

    render(<Dashboard />, { wrapper })
    await switchToAccountsTab(user)
    await user.click(screen.getByRole('button', { name: /rename work/i }))

    const input = screen.getByRole('spinbutton', { name: /weekly reserve % for work/i })
    expect((input as HTMLInputElement).value).toBe('90')
  })

  it('saving sends nickname and reserve together', async () => {
    const user = userEvent.setup()
    vi.mocked(hooks.useAccounts).mockReturnValue({
      data: [makeAccount({ name: 'work', nickname: null, weeklyReservePct: null })],
      isLoading: false,
    } as ReturnType<typeof hooks.useAccounts>)

    render(<Dashboard />, { wrapper })
    await switchToAccountsTab(user)
    await user.click(screen.getByRole('button', { name: /rename work/i }))
    const reserveInput = screen.getByRole('spinbutton', { name: /weekly reserve % for work/i })
    await user.clear(reserveInput)
    await user.type(reserveInput, '85')
    await user.click(screen.getByRole('button', { name: /save nickname/i }))

    expect(updateMutate).toHaveBeenCalledWith({
      name: 'work', nickname: null, weeklyReservePct: 85,
    })
  })

  it('clearing the reserve input sends null', async () => {
    const user = userEvent.setup()
    vi.mocked(hooks.useAccounts).mockReturnValue({
      data: [makeAccount({ name: 'work', weeklyReservePct: 90 })],
      isLoading: false,
    } as ReturnType<typeof hooks.useAccounts>)

    render(<Dashboard />, { wrapper })
    await switchToAccountsTab(user)
    await user.click(screen.getByRole('button', { name: /rename work/i }))
    await user.clear(screen.getByRole('spinbutton', { name: /weekly reserve % for work/i }))
    await user.click(screen.getByRole('button', { name: /save nickname/i }))

    expect(updateMutate).toHaveBeenCalledWith({
      name: 'work', nickname: null, weeklyReservePct: null,
    })
  })

  it('shows the over-reserve chip when weekly usage is at/over the reserve', async () => {
    const user = userEvent.setup()
    vi.mocked(hooks.useAccounts).mockReturnValue({
      data: [makeAccount({
        name: 'work', weeklyReservePct: 90,
        usage: { session: { pct: 10, resetsAt: null }, weekly: { pct: 95, resetsAt: null } },
      })],
      isLoading: false,
    } as ReturnType<typeof hooks.useAccounts>)

    render(<Dashboard />, { wrapper })
    await switchToAccountsTab(user)

    expect(screen.getByText('over reserve')).toBeInTheDocument()
  })

  it('no chip when below reserve or reserve unset', async () => {
    const user = userEvent.setup()
    vi.mocked(hooks.useAccounts).mockReturnValue({
      data: [
        makeAccount({ name: 'below', weeklyReservePct: 90,
          usage: { session: { pct: 10, resetsAt: null }, weekly: { pct: 50, resetsAt: null } } }),
        makeAccount({ name: 'unset', weeklyReservePct: null,
          usage: { session: { pct: 10, resetsAt: null }, weekly: { pct: 99, resetsAt: null } } }),
      ],
      isLoading: false,
    } as ReturnType<typeof hooks.useAccounts>)

    render(<Dashboard />, { wrapper })
    await switchToAccountsTab(user)

    expect(screen.queryByText('over reserve')).not.toBeInTheDocument()
  })

  it('weekly usage bar renders a marker at the reserve position', async () => {
    const user = userEvent.setup()
    vi.mocked(hooks.useAccounts).mockReturnValue({
      data: [makeAccount({ name: 'work', weeklyReservePct: 90 })],
      isLoading: false,
    } as ReturnType<typeof hooks.useAccounts>)

    render(<Dashboard />, { wrapper })
    await switchToAccountsTab(user)

    const marker = screen.getByTestId('reserve-marker')
    expect(marker).toBeInTheDocument()
    expect(marker.style.left).toBe('90%')
  })
})
```

Also update `makeAccount` in this file to accept `weeklyReservePct` via `Partial<Account>` (it already spreads overrides — just ensure the base object includes `weeklyReservePct: null`).

- [ ] **Step 2: Run tests to verify they fail**

Run (in `web/`): `npx vitest run src/components/__tests__/AccountsReserve.test.tsx`
Expected: FAIL — `hooks.useUpdateAccount` does not exist yet.

- [ ] **Step 3: Implement**

1. `web/src/api/types.ts` — add to `Account`:

```ts
  weeklyReservePct?: number | null
```

2. `web/src/api/hooks.ts` — replace `useUpdateAccountNickname` (delete it — no parallel copy) with:

```ts
export interface AccountUpdate {
  name: string
  nickname?: string | null
  weeklyReservePct?: number | null
}

export function useUpdateAccount() {
  const qc = useQueryClient()
  return useMutation<Account, ApiRequestError, AccountUpdate>({
    mutationFn: ({ name, ...fields }) =>
      api.patch<Account>(`/accounts/${encodeURIComponent(name)}`, fields),
    onSuccess: () => { qc.invalidateQueries({ queryKey: QK.accounts }) },
  })
}
```

3. `web/src/components/UsageBadge.tsx` — add `markerPct?: number | null` to `UsageBadgeProps` and destructure it. Make the bar container relative and render the marker:

```tsx
      <span className="relative h-1 w-12 rounded-full bg-gray-200 dark:bg-gray-700 overflow-hidden">
        <span
          className={cn('h-full block rounded-full transition-all', barColor)}
          style={{ width: `${clampedPct}%` }}
          aria-hidden="true"
        />
        {markerPct != null && (
          <span
            className="absolute top-0 h-full w-px bg-gray-900/70 dark:bg-gray-100/70"
            style={{ left: `${Math.min(100, Math.max(0, markerPct))}%` }}
            data-testid="reserve-marker"
            aria-hidden="true"
          />
        )}
      </span>
```

4. `web/src/components/AccountsManager.tsx`:
   - Import/usage swap: `useUpdateAccountNickname` → `useUpdateAccount`; in `AccountsManager`, `const updateAccount = useUpdateAccount()`.
   - Replace `onSaveNickname: (name, nickname) => void` prop (in `AccountRowProps` AND `AccountsStripProps`) with `onSave: (update: { name: string; nickname?: string | null; weeklyReservePct?: number | null }) => void`, and `handleSaveNickname` with:

```tsx
  function handleSave(update: { name: string; nickname?: string | null; weeklyReservePct?: number | null }) {
    updateAccount.mutate(update)
  }
```

   - In `AccountRow`, add reserve draft state beside the nickname draft:

```tsx
  const [reserveDraft, setReserveDraft] = useState('')
```

     `startEdit` also does `setReserveDraft(account.weeklyReservePct != null ? String(account.weeklyReservePct) : '')`; `cancelEdit` also resets it. `saveEdit` becomes:

```tsx
  function saveEdit() {
    const trimmed = reserveDraft.trim()
    let reserve: number | null = null
    if (trimmed !== '') {
      const parsed = Number(trimmed)
      if (Number.isFinite(parsed)) reserve = Math.min(100, Math.max(1, parsed))
    }
    onSave({ name: account.name, nickname: draft.trim() || null, weeklyReservePct: reserve })
    setEditing(false)
    setDraft('')
    setReserveDraft('')
  }
```

   - In the editing JSX, after the nickname `<input>` add:

```tsx
            <input
              type="number"
              min={1}
              max={100}
              value={reserveDraft}
              onChange={(e) => setReserveDraft(e.target.value)}
              onKeyDown={handleKeyDown}
              aria-label={`Weekly reserve % for ${account.name}`}
              placeholder="reserve %"
              title="Stop auto-selecting this account once weekly usage reaches this % (blank = no reserve)"
              className={cn(
                'w-24 rounded border border-gray-300 dark:border-gray-600',
                'bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100',
                'px-2 py-1 text-sm',
                'focus:outline-none focus:ring-2 focus:ring-blue-500',
              )}
            />
```

   - Over-reserve chip, defined beside `CopilotChip`:

```tsx
function OverReserveChip() {
  return (
    <span
      className={cn(
        'text-xs font-medium rounded px-1.5 py-0.5 whitespace-nowrap',
        'bg-amber-100 text-amber-700 dark:bg-amber-900/40 dark:text-amber-300',
      )}
      title="Weekly usage is at/over this account's reserve — only picked as a last resort"
    >
      over reserve
    </span>
  )
}
```

     Rendered in the badges row right after `<RankBadge …/>`:

```tsx
            {account.weeklyReservePct != null && account.usage != null &&
              account.usage.weekly.pct >= account.weeklyReservePct && <OverReserveChip />}
```

   - Weekly `UsageBadge` call gains `markerPct={account.weeklyReservePct ?? null}`.

- [ ] **Step 3.5: Update every mock list that names the old hook**

Run: `grep -rln "useUpdateAccountNickname" web/src`
In EVERY file listed: rename `useUpdateAccountNickname` → `useUpdateAccount` (both in `vi.mock` factories and `vi.mocked(hooks.useUpdateAccountNickname)` lines). In `AccountsNickname.test.tsx` additionally update the three mutate assertions — the payload now always includes the reserve, e.g.:

```ts
    expect(nicknameMutate).toHaveBeenCalledWith({ name: 'work', nickname: 'Personal', weeklyReservePct: null })
```

(same change for the `'Freelance'` Enter-key test and the clear-to-null test).

- [ ] **Step 4: Run the full web verification**

Run (in `web/`): `npx vitest run && npx tsc --noEmit && npx eslint .`
Expected: all PASS / clean. Any remaining failure is almost certainly a missed mock-list rename — fix and re-run.

- [ ] **Step 5: Commit**

```bash
git add web/src
git commit -m "feat(web): weekly reserve editor, over-reserve chip, usage-bar marker"
```

---

### Task 6: Full verification + ship (performed by the orchestrator, not a subagent)

- [ ] Full Python suite (known exclusions per project memory):

```bash
env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 PYTHONPATH=.:src python3 -m pytest tests/ server/tests/ -q \
  --ignore=server/tests/daemon_properties \
  --deselect server/tests/integration/test_eta_parity_live.py \
  -k "not copilot_proxy_resolve"
```

- [ ] `python3 -m ruff check . && python3 -m mypy server/ --ignore-missing-imports`
- [ ] Web: `cd web && npx vitest run && npx tsc --noEmit && npx eslint .`
- [ ] Push `feat/weekly-usage-reserve`, open PR (body ends with the Claude Code trailer), `gh pr ready`, `gh pr merge --squash --auto`.

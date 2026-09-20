# Capacity Settings PR 5 — One Reserve Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** One reserve: a per-account `weeklyReservePct` is an OVERRIDE of the push level's reserve, the capacity plan honours it, every surface (governor, CLI selector, Accounts page, readout) reads the same effective number, and `GET /api/accounts` says what that number is.

**Architecture:** The push level's `RESERVE_PCT` ("keep N% of the weekly allowance back") becomes the default; `effective_reserve_pct(acct, push_level) = acct.weeklyReservePct ?? RESERVE_PCT[push_level]` is the ONE rule, living beside `account_headroom` in `server/api/accounts.py`. `account_headroom` takes the push level (not a flat reserve) and applies a per-account ceiling `100 − effective`, stamping `reservePct` on each readout row; the accounts list route publishes `effectiveReservePct` and the push level's `pushLevelReservePct` per account. The per-account field's meaning flips from "stop at this usage %" to "keep this % back" so one stored number means one thing everywhere — the CLI selector (`rank_accounts`) and the Accounts page (chip, marker, label) follow.

**Tech Stack:** Python 3.12 / FastAPI (`server/`), the `ccswitch` core (`src/ccswitch/`, imported by the shared venv from the DEPLOY worktree — every test run needs `PYTHONPATH=$PWD/src`), pytest; React 18 + TypeScript + vitest (`web/`).

**Spec:** `docs/superpowers/specs/2026-09-20-capacity-settings-structure-design.md` — C15, C22, D4, D12, §5.1 Accounts row, §6 "Effective reserve = account override ?? push level", §7 (`GET /api/capacity` `accounts.rows[].reservePct`; `GET /api/accounts` `effectiveReservePct`), §8 row **5**, §9 (`test_account_headroom.py`: "an account with a 60% override is unavailable at 41% used while the push level is `steady`"; `test_capacity_readout.py`: "per-account `reservePct` = override ?? push level").

## Global Constraints

- **Branch:** `feat/capacity-pr5-one-reserve`, created from `origin/master` (`git fetch && git checkout -b feat/capacity-pr5-one-reserve origin/master`). Never branch off local `master`.
- **Reserve semantics (D4 + §9):** a reserve is the percent of the weekly allowance KEPT BACK; an account is at its ceiling when its reserve window's `used_pct >= 100 − reserve`. The push level's values are `RESERVE_PCT = {"conserve": 40, "steady": 20, "max": 5}` (`server/automations/capacity_plan.py:7`). A per-account `weeklyReservePct` (ledger, 1–100) is an override with the SAME meaning. Effective reserve = override if set, else the push level's. This flips the stored field's old meaning ("soft-block once weekly usage reaches this %"); no live account has an override set, so nothing converts.
- **Where each reserve binds (D12):** the effective reserve makes an account *unavailable to the capacity governor* (`account_headroom`, hard) and, for an explicit override only, demotes it in the CLI selector (`rank_accounts`, soft fallback). The push level's reserve is NEVER applied by the CLI selector: it has no push level, and a human's own account pick must not be steered by an automation intent knob.
- **Vocabulary (D8):** the term is **Reserve**. Accounts page label: **Weekly reserve override**; placeholder `push level's N%` with N the live default; the chip stays `over reserve`.
- **No em dashes in NEW shipped UI copy.**
- **Push level reading:** one normaliser `push_level_of(gov: dict) -> str` in `capacity_plan.py`; the readout and the accounts route both use it. Never a second `RESERVE_PCT` mirror in the browser (C22).
- **Tests run as** `env -i HOME=/tmp/viberpit-test-home PATH=/home/tony/GitHub/viberpit-deploy/.venv/bin:/usr/bin:/bin CCSWITCH_DEV=1 PYTHONPATH=$PWD/src /home/tony/GitHub/viberpit-deploy/.venv/bin/python -m pytest …` from the worktree root (BOTH suites — `PYTHONPATH=$PWD/src` is what makes `src/ccswitch` edits visible); web commands as `(cd web && …)` subshells, never a persisted `cd`. Every command in the foreground.
- **Never `git stash`.** Commit via `git commit -F <file>`; the trailer is the running model's `Co-Authored-By: Claude <model> <noreply@anthropic.com>`.
- **Brand ratchet:** `python3 scripts/brand_ratchet.py` must stay green; `server/api/accounts.py` already imports the legacy package (no new `ccswitch` import lines anywhere).

---

### Task 1: The reserve means "kept back" in the CLI core

**Files:**
- Modify: `src/ccswitch/selector.py:205-216` (docstring), `:265-269` (the comparison)
- Modify: `src/ccswitch/ledger.py:378-400` (three docstrings)
- Modify: `tests/test_selector.py:398-470` (the reserve tests' values), `tests/test_pick_account_detailed.py:154`
- Modify: `docs/product/features/usage-aware-rotation.md:56-58`

**Interfaces:**
- Consumes: nothing.
- Produces: `rank_accounts(..., reserves: Mapping[str, float])` where a value is the percent KEPT BACK; an account is reserve-blocked when `reserve_pct(u) >= 100 - reserve`. Task 3's route-level ranking passes the ledger overrides unchanged, so this is the only place the meaning lives.

- [ ] **Step 1: Flip the tests first**

In `tests/test_selector.py`:
- `test_reserve_demotes_over_reserve_account_to_last`: `reserves={"hot": 90.0}` → `reserves={"hot": 10.0}` and add the comment `# keep 10% back → ceiling 90; hot at 91% is over it`.
- `test_reserve_boundary_is_inclusive`: `reserves={"at": 90.0, "under": 90.0}` → `reserves={"at": 10.0, "under": 10.0}`; its comment `with weekly_pct == reserve it must be blocked` → `at weekly_pct == 100 - reserve it must be blocked`.
- `test_reserve_soft_fallback_picks_least_weekly_used`: `reserves={"worse": 90.0, "better": 90.0}` → `{"worse": 10.0, "better": 10.0}`.
- `test_reserve_hard_ceiling_still_excludes_outright`: `reserves={"maxed": 90.0}` → `{"maxed": 10.0}`.
- `test_optimistic_tier_outranks_reserve_blocked`: `reserves={"r": 90.0}` → `{"r": 10.0}`.
- `test_no_reserves_behavior_unchanged`: `reserves={"a": 99.0}` → `reserves={"a": 1.0}` (keep 1% back → ceiling 99; `a` at 20% stays under).
- Add, after `test_reserve_boundary_is_inclusive`:
```python
def test_reserve_is_percent_kept_back_not_a_usage_threshold():
    """Spec 2026-09-20 D4: a reserve of 60 means 'keep 60% back', so an account at
    41% used is over it (ceiling 40) — the same meaning the capacity governor's
    push level uses. Under the old threshold reading 41 < 60 would have passed."""
    usages = {
        "kept": anthropic_snapshot(10.0, SOON, 41.0, SOON, False),
        "free": anthropic_snapshot(10.0, LATER, 45.0, LATER, False),
    }
    ranked, _ = rank_accounts(usages, set(), _cfg(), NOW, reserves={"kept": 60.0})
    assert [r.name for r in ranked] == ["free", "kept"]
    assert ranked[1].reason == "over weekly reserve — fallback"
```
In `tests/test_pick_account_detailed.py:154`: `return_value={"hot": 90.0}` → `return_value={"hot": 10.0}` with the comment `# keep 10% back: hot at 95% is over the 90 ceiling`.

- [ ] **Step 2: Run to verify the new test fails**

Run: `env -i HOME=/tmp/viberpit-test-home PATH=/home/tony/GitHub/viberpit-deploy/.venv/bin:/usr/bin:/bin CCSWITCH_DEV=1 PYTHONPATH=$PWD/src /home/tony/GitHub/viberpit-deploy/.venv/bin/python -m pytest tests/test_selector.py tests/test_pick_account_detailed.py -q -k "reserve"`
Expected: `test_reserve_is_percent_kept_back_not_a_usage_threshold` FAILS (`kept` ranks first: 41 < 60); the flipped-value tests also fail (a reserve of 10 blocks nothing under the old `>= reserve` rule for accounts at 91%… actually 91 >= 10 blocks — they may pass; the new test is the discriminator). Confirm with `python -c "import ccswitch.selector as m; print(m.__file__)"` that the path is under THIS worktree's `src/`.

- [ ] **Step 3: Implement**

`src/ccswitch/selector.py` — the comparison:
```python
        reserve = (reserves or {}).get(name)
        used_on_reserve_window = reserve_pct(u)
        # A reserve is the share of the window KEPT BACK (spec 2026-09-20 D4, the
        # same meaning as the capacity governor's push-level reserve), so the
        # account is over it once usage reaches ``100 - reserve``.
        if (reserve is not None and used_on_reserve_window is not None
                and used_on_reserve_window >= 100.0 - reserve):
            reserve_blocked.append((used_on_reserve_window, name))
            continue
```
Docstring paragraph: replace `` ``reserves`` maps account name -> reserve threshold (0-100 pct) on the account's longest window. An account at/over its reserve (but under the hard ceiling) is demoted…`` with `` ``reserves`` maps account name -> the percent of the account's longest window KEPT BACK (0-100; spec 2026-09-20 D4 — the same meaning as the capacity governor's push-level reserve). An account whose usage has reached ``100 - reserve`` (but is under the hard ceiling) is demoted…`` (keep the rest of the sentence).

`src/ccswitch/ledger.py`: `set_weekly_reserve` docstring → `"""Set or clear the per-account weekly reserve: the percent of the weekly allowance kept back (1–100; spec 2026-09-20 D4). ``None`` clears the override so the push level's reserve applies."""`; `weekly_reserves` docstring → `"""All configured reserve OVERRIDES, keyed by account name (accounts on the push level's default are omitted)."""`.

`docs/product/features/usage-aware-rotation.md:56-58`: replace the "Weekly reserve (#558)" bullet with:
```markdown
- Weekly reserve (#558, meaning fixed by spec 2026-09-20 D4): a per-account `weeklyReservePct` is
  the share of the weekly allowance KEPT BACK (the capacity governor's push level supplies the
  default: conserve 40 / steady 20 / max 5). Once weekly usage reaches `100 − reserve`, the
  account is unavailable to the governor and, for an explicit override, becomes a soft fallback
  here — burned least-weekly-used first, only after every unreserved candidate is exhausted
  (`rank_accounts`). The push level's default is never applied to a human's own pick.
```

- [ ] **Step 4: Run to verify they pass**

Run: `env -i HOME=/tmp/viberpit-test-home PATH=/home/tony/GitHub/viberpit-deploy/.venv/bin:/usr/bin:/bin CCSWITCH_DEV=1 PYTHONPATH=$PWD/src /home/tony/GitHub/viberpit-deploy/.venv/bin/python -m pytest tests/test_selector.py tests/test_pick_account_detailed.py tests/test_ledger.py -q`
Expected: PASS.

- [ ] **Step 5: Lint and commit**

Run: `/home/tony/GitHub/viberpit-deploy/.venv/bin/python -m ruff check src/ccswitch/selector.py src/ccswitch/ledger.py tests/test_selector.py tests/test_pick_account_detailed.py && python3 scripts/brand_ratchet.py`

```bash
git add src/ccswitch/selector.py src/ccswitch/ledger.py tests/test_selector.py tests/test_pick_account_detailed.py docs/product/features/usage-aware-rotation.md
git commit -F /tmp/pr5-t1-msg.txt
```
`/tmp/pr5-t1-msg.txt`:
```
feat(core): a weekly reserve is the percent kept back, not a usage threshold

Spec 2026-09-20 D4: one meaning for "reserve" across the selector, the
capacity governor and the Accounts page. rank_accounts demotes an account
once its reserve window reaches 100 - reserve. No live account carries an
override, so no stored value converts.

Co-Authored-By: Claude <model> <noreply@anthropic.com>
```

---

### Task 2: The plan honours the override — `account_headroom(push_level=…)`

**Files:**
- Modify: `server/automations/capacity_plan.py:6-8` (add `push_level_of`)
- Modify: `server/api/accounts.py:225-261` (`account_headroom`) + new `effective_reserve_pct` above it; imports
- Modify: `server/automations/capacity_readout.py:66` (`CapacitySources.accounts` type), `:83-85`, `:100` (the call), `:16` (imports)
- Modify: `server/automations/capacity_wiring.py:121`
- Modify tests: `server/tests/test_account_headroom.py`, `server/tests/test_capacity_readout.py` (`_ACCOUNT_ROWS`, the two `accounts(reserve)` seams, one new test), `server/tests/test_capacity_plan.py` (one new `push_level_of` test)

**Interfaces:**
- Consumes: Task 1's meaning (ceiling = `100 − reserve`).
- Produces: `push_level_of(gov: dict) -> str` (a member of `PUSH_LEVELS`, `"steady"` for anything else); `effective_reserve_pct(acct: dict, push_level: str) -> float`; `account_headroom(orch, *, push_level: str, timeout=…) -> list[dict]` whose rows gain `"reservePct": float`; `CapacitySources.accounts: Callable[[str], Awaitable[list[dict]]]` (called with the push level). Task 3 reuses `effective_reserve_pct`; Task 4 reads `rows[].reservePct`.

- [ ] **Step 1: Write the failing tests**

`server/tests/test_capacity_plan.py` — append:
```python
def test_push_level_of_normalises_the_governor_config():
    from server.automations.capacity_plan import push_level_of
    assert push_level_of({"push_level": "max"}) == "max"
    assert push_level_of({"push_level": "bogus"}) == "steady"
    assert push_level_of({}) == "steady"
```

`server/tests/test_account_headroom.py` — change the fixture's `infos` to carry two override accounts and the existing test to the new signature, then add the §9 case:
```python
@pytest.fixture
def fake_accounts(monkeypatch):
    infos = [{"name": "a", "enabled": True, "provider": "anthropic"},
             {"name": "b", "enabled": True, "provider": "anthropic"},
             {"name": "c", "enabled": False, "provider": "anthropic"},
             {"name": "d", "enabled": True, "provider": "openai"},
             # Spec 2026-09-20 D4: overrides of the push level's reserve, both ways.
             {"name": "e", "enabled": True, "provider": "anthropic", "weeklyReservePct": 60},
             {"name": "f", "enabled": True, "provider": "anthropic", "weeklyReservePct": 5}]
    usages = {"a": (_snap(50.0), None), "b": (_snap(90.0), None), "c": (_snap(1.0), None),
              "d": (_snap(1.0, provider="openai"), None),
              "e": (_snap(41.0), None), "f": (_snap(90.0), None)}
    monkeypatch.setattr(accounts_api, "list_accounts", lambda: infos)

    async def gather(orch, names, timeout):
        return {n: usages[n] for n in names}
    monkeypatch.setattr(accounts_api, "_gather_usages", gather)


def test_headroom_applies_reserve_enabled_and_metering(fake_accounts):
    rows = asyncio.run(accounts_api.account_headroom(object(), push_level="steady"))
    by = {r["name"]: r for r in rows}
    assert by["a"]["available"] is True and by["a"]["reservePct"] == 20
    assert by["b"]["available"] is False and by["b"]["reason"] == "at_ceiling"
    assert by["b"]["nextResetAt"].startswith("2026-09-23T08:00")
    assert by["c"]["available"] is False and by["c"]["reason"] == "disabled"
    assert by["d"]["metered"] is False and by["d"]["available"] is False


def test_headroom_honours_a_per_account_override_both_ways(fake_accounts):
    """Spec 2026-09-20 §9: an account with a 60% override is unavailable at 41% used
    while the push level is steady (its ceiling is 40, not 80); one with a 5%
    override is still available at 90% used (ceiling 95). The row says which
    reserve it was judged by."""
    rows = asyncio.run(accounts_api.account_headroom(object(), push_level="steady"))
    by = {r["name"]: r for r in rows}
    assert by["e"]["available"] is False and by["e"]["reason"] == "at_ceiling"
    assert by["e"]["reservePct"] == 60
    assert by["f"]["available"] is True and by["f"]["reservePct"] == 5


def test_headroom_default_follows_the_push_level(fake_accounts):
    rows = asyncio.run(accounts_api.account_headroom(object(), push_level="max"))
    by = {r["name"]: r for r in rows}
    assert by["b"]["available"] is True and by["b"]["reservePct"] == 5   # ceiling 95 > 90
    assert by["e"]["reservePct"] == 60                                    # the override still wins


def test_effective_reserve_pct_is_override_or_push_level():
    assert accounts_api.effective_reserve_pct({"weeklyReservePct": 60}, "steady") == 60.0
    assert accounts_api.effective_reserve_pct({"weeklyReservePct": None}, "steady") == 20.0
    assert accounts_api.effective_reserve_pct({}, "conserve") == 40.0
```

`server/tests/test_capacity_readout.py`: add `"reservePct": 20` to both `_ACCOUNT_ROWS` entries (the second row: `"reservePct": 20,` before `"windows"`). Change both seams `async def accounts(reserve):` / `async def _accounts(reserve):` to `async def accounts(push_level):` / `async def _accounts(push_level):` (bodies unchanged). Append:
```python
def test_readout_hands_the_push_level_to_the_account_seam_and_keeps_each_rows_reserve():
    """Spec 2026-09-20 D4/§7: the seam is called with the PUSH LEVEL (it derives each
    account's effective reserve itself), and the per-row reservePct rides through
    untouched; the block-level reservePct stays the push level's default."""
    seen = {}
    rows = [dict(_ACCOUNT_ROWS[0], reservePct=60), dict(_ACCOUNT_ROWS[1])]

    async def accounts(push_level):
        seen["level"] = push_level
        return rows

    async def governor_config():
        return ("monitor", {"push_level": "max", "sessions_per_account": 2})

    async def run():
        src = _make_sources(accounts=rows,
                            governor_config=("monitor", {"push_level": "max", "sessions_per_account": 2}))
        src.accounts = accounts   # replace the helper's list-wrapping seam with the spying one
        return await build_readout(src)

    r = asyncio.run(run())
    assert seen["level"] == "max"
    assert r["accounts"]["reservePct"] == 5
    assert [row["reservePct"] for row in r["accounts"]["rows"]] == [60, 20]
```
(`_make_sources` at `server/tests/test_capacity_readout.py:160` accepts `governor_config=` and wraps `accounts=` in its own `_accounts(...)` seam; `CapacitySources` is a plain dataclass, so assigning `src.accounts` afterwards replaces that seam — the spy is the point. Drop the unused `governor_config` inner function from the snippet above.)

- [ ] **Step 2: Run to verify they fail**

Run: `env -i HOME=/tmp/viberpit-test-home PATH=/home/tony/GitHub/viberpit-deploy/.venv/bin:/usr/bin:/bin CCSWITCH_DEV=1 PYTHONPATH=$PWD/src /home/tony/GitHub/viberpit-deploy/.venv/bin/python -m pytest server/tests/test_account_headroom.py server/tests/test_capacity_plan.py server/tests/test_capacity_readout.py -q`
Expected: FAIL — `push_level` unexpected kwarg, `push_level_of` / `effective_reserve_pct` missing, `reservePct` KeyError.

- [ ] **Step 3: Implement**

`server/automations/capacity_plan.py` — after `RESERVE_PCT`:
```python


def push_level_of(gov: dict) -> str:
    """The governor row's push level, normalised: anything that is not a member
    of ``PUSH_LEVELS`` (an absent row, a stale enum value) reads as ``steady`` —
    the ONE normaliser the readout and the accounts list share (spec 2026-09-20
    D4), so both derive the same default reserve."""
    level = gov.get("push_level")
    return level if level in PUSH_LEVELS else "steady"
```

`server/api/accounts.py` — add to the imports (server side, after the `server.audit` import line): `from server.automations.capacity_plan import RESERVE_PCT` (check `server.automations.capacity_plan` imports nothing from `server.api`; it imports only dataclasses). Above `account_headroom`:
```python
def effective_reserve_pct(acct: dict, push_level: str) -> float:
    """THE reserve an account is judged by (spec 2026-09-20 D4): its own
    ``weeklyReservePct`` override when set, else the push level's ``RESERVE_PCT``.
    A reserve is the percent of the weekly allowance kept back; the account's
    ceiling is ``100 - reserve``. One rule for the governor's headroom, the
    accounts list's ``effectiveReservePct`` and the readout's per-row value."""
    override = acct.get("weeklyReservePct")
    if override is not None:
        return float(override)
    return float(RESERVE_PCT.get(push_level, RESERVE_PCT["steady"]))
```
`account_headroom` becomes:
```python
async def account_headroom(orch, *, push_level: str,
                           timeout: float = USAGE_FETCH_TIMEOUT) -> list[dict]:
    """Per-account availability for the capacity governor (spec 2026-09-19 §5).

    Lives here, not in a new module: this file already imports the selector, and the
    brand ratchet has no slack for another import of the legacy package. ``available``
    uses ``at_ceiling`` with ``ceiling = 100 - reserve`` where the reserve is each
    account's EFFECTIVE one (``effective_reserve_pct``: its override, else the push
    level's — spec 2026-09-20 D4), so the governor and the account selector agree on
    what "usable" means; an unmetered engine (Codex, Copilot — D4 of the governor
    spec) counts zero and says so. Every row carries the ``reservePct`` it was judged by.
    """
    infos = await asyncio.to_thread(list_accounts)
    usages = await _gather_usages(orch, [a["name"] for a in infos], timeout)
    rows: list[dict] = []
    for acct in infos:
        name = acct["name"]
        usage, _stale = usages.get(name, (None, None))
        enabled = bool(acct.get("enabled", True))
        reserve = effective_reserve_pct(acct, push_level)
        ceiling = 100.0 - reserve
        metered = usage is not None and usage.provider == "anthropic" and usage.availability == AVAILABLE
        reason = None
        next_reset = None
        if not metered:
            reason = "not_metered"
        elif not enabled:
            reason = "disabled"
        elif at_ceiling(usage, ceiling):
            reason = "at_ceiling"
            resets = [w.resets_at for w in usage.windows_at_or_over(ceiling) if w.resets_at]
            next_reset = iso_utc(min(resets)) if resets else None
        rows.append({
            "name": name, "engine": acct.get("engine") or ("codex" if acct.get("provider") == "openai" else "claude_code"),
            "provider": acct.get("provider"), "enabled": enabled, "metered": metered,
            "available": reason is None, "reason": reason, "nextResetAt": next_reset,
            "reservePct": reserve,
            "windows": [{"key": w.key, "pct": w.used_pct, "resetsAt": iso_utc(w.resets_at)}
                        for w in (usage.windows if usage is not None else ())],
        })
    return rows
```

`server/automations/capacity_readout.py`: import `push_level_of` from `.capacity_plan`; `accounts: Callable[[str], Awaitable[list[dict]]]` with the comment `# called with the push level; each row carries its own effective reservePct (D4)`; in `build_readout`: `push_level = push_level_of(gov)` and `reserve = RESERVE_PCT[push_level]` (replace the two lines at 84-85); `account_rows = await src.accounts(push_level)`.

`server/automations/capacity_wiring.py:121`: `accounts=lambda level: account_headroom(orch, push_level=level),`.

- [ ] **Step 4: Run to verify they pass**

Run: the Step 2 command plus `server/tests/test_capacity_governor*.py server/tests/test_capacity_wiring*.py server/tests/test_capacity_api*.py` (glob what exists: `ls server/tests/test_capacity*`).
Expected: PASS.

- [ ] **Step 5: Lint, type-check, commit**

Run: `/home/tony/GitHub/viberpit-deploy/.venv/bin/python -m ruff check server/ && /home/tony/GitHub/viberpit-deploy/.venv/bin/python -m mypy server/api/accounts.py server/automations/capacity_plan.py server/automations/capacity_readout.py server/automations/capacity_wiring.py --ignore-missing-imports && python3 scripts/brand_ratchet.py`

```bash
git add server/automations/capacity_plan.py server/api/accounts.py server/automations/capacity_readout.py server/automations/capacity_wiring.py server/tests/test_account_headroom.py server/tests/test_capacity_plan.py server/tests/test_capacity_readout.py
git commit -F /tmp/pr5-t2-msg.txt
```
`/tmp/pr5-t2-msg.txt`:
```
feat(capacity): the plan honours a per-account reserve override

Spec 2026-09-20 D4. account_headroom takes the push level and judges each
account by effective_reserve_pct (override ?? RESERVE_PCT[level]); every
readout row carries the reservePct it was judged by. push_level_of is the
one normaliser of the governor's push level.

Co-Authored-By: Claude <model> <noreply@anthropic.com>
```

---

### Task 3: `GET /api/accounts` says the effective reserve

**Files:**
- Modify: `server/api/accounts.py:316-396` (`_account_response`, `_account_response_from_info`), `:599-672` (the list route)
- Modify tests: `server/tests/test_accounts_weekly_reserve.py`

**Interfaces:**
- Consumes: Task 2's `effective_reserve_pct`, `push_level_of`, `RESERVE_PCT`.
- Produces: every account in `GET /api/accounts` carries `effectiveReservePct: float` and `pushLevelReservePct: float` (the push level's default, so the Accounts page can say "blank = push level's N%" without a second query — C22 forbids a client-side mirror, not a server-computed field). Other routes that echo an account (PATCH, refresh) send both as `null`. Task 4 reads both.

- [ ] **Step 1: Write the failing tests**

In `server/tests/test_accounts_weekly_reserve.py`:
- `test_get_accounts_ranks_over_reserve_account_as_fallback`: the PATCH body `{"weeklyReservePct": 90}` → `{"weeklyReservePct": 20}` and the docstring gains `Reserve 20 = keep 20% back (ceiling 80): hot at 95% is over it, cool at 20% is not.`
- Append:
```python
def test_get_accounts_carries_the_effective_and_default_reserve():
    """Spec 2026-09-20 D4/§7: effectiveReservePct = the account's override, else the
    push level's RESERVE_PCT (steady 20 on a fresh governor row); pushLevelReservePct
    is that default, so the Accounts page can label a blank override."""
    with temp_env():
        _seed_slot("plain")
        _seed_slot("kept")
        with patch.dict(os.environ, {"CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:"}):
            client = _make_client()
        with client:
            client.patch("/api/accounts/kept", json={"weeklyReservePct": 60},
                         headers=IDENTITY_HEADERS)
            response = client.get("/api/accounts", headers=IDENTITY_HEADERS)
    assert response.status_code == 200
    by_name = {a["name"]: a for a in response.json()}
    assert by_name["plain"]["weeklyReservePct"] is None
    assert by_name["plain"]["effectiveReservePct"] == 20.0
    assert by_name["plain"]["pushLevelReservePct"] == 20.0
    assert by_name["kept"]["effectiveReservePct"] == 60.0
    assert by_name["kept"]["pushLevelReservePct"] == 20.0
```

- [ ] **Step 2: Run to verify it fails**

Run: `env -i HOME=/tmp/viberpit-test-home PATH=/home/tony/GitHub/viberpit-deploy/.venv/bin:/usr/bin:/bin CCSWITCH_DEV=1 PYTHONPATH=$PWD/src /home/tony/GitHub/viberpit-deploy/.venv/bin/python -m pytest server/tests/test_accounts_weekly_reserve.py -q`
Expected: the new test FAILS with `KeyError: 'effectiveReservePct'`.

- [ ] **Step 3: Implement**

`server/api/accounts.py`:
- `_account_response(...)` gains two keyword params after `weekly_reserve_pct`: `effective_reserve_pct_value: float | None = None, push_level_reserve_pct: float | None = None`, and the wire dict gains, right after `"weeklyReservePct": weekly_reserve_pct,`:
```python
        # Spec 2026-09-20 D4: the reserve this account is JUDGED by (override, else
        # the push level's) and the push level's own value, so the Accounts page
        # can label a blank override "push level's N%". Null on routes that echo a
        # single account without reading the governor (PATCH, refresh).
        "effectiveReservePct": effective_reserve_pct_value,
        "pushLevelReservePct": push_level_reserve_pct,
```
- `_account_response_from_info(acct, usage=None, *, push_level: str | None = None, **kw)`: when `push_level` is not None pass `effective_reserve_pct_value=effective_reserve_pct(acct, push_level), push_level_reserve_pct=float(RESERVE_PCT[push_level])`; update its docstring's field list ("reserve, effective reserve, defaults, …").
- The list route `get_accounts` (`@router.get("")`, line ~595): after `load = await orch.live_session_load()` add
```python
    # The governor's push level supplies the default reserve (spec 2026-09-20 D4).
    # Read best-effort: an orchestrator without the governor seam (tests, a
    # worker-role daemon) or a DB blip degrades to steady, never a 500.
    push_level = await _push_level(orch)
```
and pass `push_level=push_level` into the `_account_response_from_info(...)` call. Add the helper beside `_gather_usages`:
```python
async def _push_level(orch) -> str:
    read = getattr(orch, "_capacity_governor_read_config", None)
    if read is None:
        return "steady"
    try:
        _mode, gov = await read()
    except Exception:  # noqa: BLE001 — a config read must never fail the accounts list
        log.warning("accounts.push_level_read_failed", exc_info=True)
        return "steady"
    return push_level_of(gov)
```
(import `push_level_of` alongside `RESERVE_PCT`; the module logger is `log`, defined at line 58.)

- [ ] **Step 4: Run to verify they pass**

Run: `… -m pytest server/tests/test_accounts_weekly_reserve.py server/tests/test_accounts_extra_usage.py server/tests/test_account_headroom.py -q` and `grep -rln "weeklyReservePct" server/tests | xargs … -m pytest -q` (any other accounts-list test asserting the exact key set).
Expected: PASS.

- [ ] **Step 5: Lint, type-check, commit**

Run: `/home/tony/GitHub/viberpit-deploy/.venv/bin/python -m ruff check server/api/accounts.py server/tests/test_accounts_weekly_reserve.py && /home/tony/GitHub/viberpit-deploy/.venv/bin/python -m mypy server/api/accounts.py --ignore-missing-imports && python3 scripts/brand_ratchet.py`

```bash
git add server/api/accounts.py server/tests/test_accounts_weekly_reserve.py
git commit -F /tmp/pr5-t3-msg.txt
```
`/tmp/pr5-t3-msg.txt`:
```
feat(accounts): GET /api/accounts carries effectiveReservePct and the push level's default

Spec 2026-09-20 D4/§7.

Co-Authored-By: Claude <model> <noreply@anthropic.com>
```

---

### Task 4: The web — Accounts page reads the effective reserve; the readout row shows its reserve

**Files:**
- Modify: `web/src/api/types.ts:121` (`Account`), `web/src/api/capacity.ts:25-35` (`CapacityAccountRow`)
- Modify: `web/src/features/delivery/capacity/capacityStack.ts:42-47` (`accountRowLine`), `web/src/features/delivery/capacity/__fixtures__/capacity.ts` (row gains `reservePct`), `web/src/features/delivery/capacity/capacityStack.test.ts:182-198`
- Modify: `web/src/features/accounts/AccountsManager.tsx:204-216` (`OverReserveChip`), `:437-449` (the reserve input), `:597-599` (the chip condition), `:617-621` (the marker)
- Modify: `web/src/features/accounts/AccountsReserve.test.tsx` (fixture + the chip/marker/input tests)

**Interfaces:**
- Consumes: Task 3's `effectiveReservePct` / `pushLevelReservePct`; Task 2's `rows[].reservePct`.
- Produces: nothing downstream.

- [ ] **Step 1: Write the failing tests**

`capacityStack.test.ts`: `base` gains `reservePct: 20`; expectations become `'acct — available · 20% reserve'`, `'acct — at ceiling · 20% reserve'`, `line.startsWith('acct — at ceiling · resets ')` stays and add `expect(line.endsWith(' · 20% reserve')).toBe(true)`, `'acct — not metered · 20% reserve'`, `'acct — disabled · 20% reserve'`; add
```ts
  it('an override shows as that account\'s own reserve', () => {
    expect(accountRowLine({ ...base, reservePct: 60, available: false, reason: 'at_ceiling' })).toBe('acct — at ceiling · 60% reserve')
  })
```

`AccountsReserve.test.tsx`:
- `makeAccount` defaults gain `effectiveReservePct: 20, pushLevelReservePct: 20`.
- `'edit mode shows a reserve input prefilled with the current value'`: the query becomes `getByLabelText('Weekly reserve override for work')` (read the test; keep its value assertion) and add `expect(input).toHaveAttribute('placeholder', "push level's 20%")`.
- Every `getByLabelText(/Weekly reserve %/…)` / `'Weekly reserve % for work'` in the file → the new label `Weekly reserve override for work`.
- `'shows the over-reserve chip when weekly usage is at/over the reserve'`: account `{ weeklyReservePct: 10, effectiveReservePct: 10, usage: anthropicUsage(10, 95) }` (keep 10% → ceiling 90; 95 is over).
- `'no chip when below reserve or reserve unset'` → rename `'no chip below the effective ceiling; the push level default applies when no override is set'` with accounts `below: { weeklyReservePct: 10, effectiveReservePct: 10, usage: anthropicUsage(10, 50) }` and `unset: { weeklyReservePct: null, effectiveReservePct: 20, usage: anthropicUsage(10, 79) }` (ceiling 80; 79 is under).
- Add:
```ts
  it('the push level default alone can put an account over reserve', async () => {
    vi.mocked(hooks.useAccounts).mockReturnValue(queryResult([
      makeAccount({ name: 'unset', weeklyReservePct: null, effectiveReservePct: 20, usage: anthropicUsage(10, 81) }),
    ]))
    render(<AccountsManager />, { wrapper })
    expect(screen.getByText('over reserve')).toBeInTheDocument()
  })
```
- `'weekly usage bar renders a marker at the reserve position'`: account `{ weeklyReservePct: 10, effectiveReservePct: 10 }`, expected `marker.style.left` `'90%'` (the CEILING position, `100 − reserve`); add a sibling `'the marker follows the push level default when no override is set'` with `{ weeklyReservePct: null, effectiveReservePct: 20 }` expecting `'80%'`.

- [ ] **Step 2: Run to verify they fail**

Run: `(cd web && npx vitest run src/features/accounts/AccountsReserve.test.tsx src/features/delivery/capacity/capacityStack.test.ts)`
Expected: FAIL (old label, old chip math, no reserve suffix).

- [ ] **Step 3: Implement**

`web/src/api/types.ts` — after `weeklyReservePct?: number | null`:
```ts
  /** The reserve this account is judged by (spec 2026-09-20 D4): its override,
   *  else the push level's. Percent of the weekly allowance KEPT BACK; the
   *  ceiling is `100 − effectiveReservePct`. Null on single-account echoes. */
  effectiveReservePct?: number | null
  /** The push level's own reserve, for labelling a blank override. */
  pushLevelReservePct?: number | null
```
`web/src/api/capacity.ts` `CapacityAccountRow`: add `/** The effective reserve this row was judged by (override ?? push level). */ reservePct: number` after `nextResetAt`.

`capacityStack.ts`:
```ts
export function accountRowLine(row: CapacityAccountRow): string {
  const reserve = `${row.reservePct}% reserve`
  if (row.available) return `${row.name} — available · ${reserve}`
  const reason = (row.reason ?? 'unavailable').replace(/_/g, ' ')
  const reset = formatResetTime(row.nextResetAt)
  return reset
    ? `${row.name} — ${reason} · resets ${reset} · ${reserve}`
    : `${row.name} — ${reason} · ${reserve}`
}
```
Fixture `capacity.ts`: the `primary` row gains `reservePct: 20,` after `nextResetAt: null,`.

`AccountsManager.tsx`:
- Add above the component that renders the row (or beside `OverReserveChip`):
```tsx
/** The usage percent at which an account reaches its reserve (spec 2026-09-20 D4):
 *  a reserve is the share KEPT BACK, so the ceiling is its complement. */
function reserveCeiling(account: Account): number | null {
  return account.effectiveReservePct == null ? null : 100 - account.effectiveReservePct
}
```
- `OverReserveChip` `title` → `"Weekly usage has reached this account's reserve ceiling: the capacity governor counts it unavailable, and an explicit override makes it a last-resort pick"`.
- The chip condition:
```tsx
          {reserveCeiling(account) != null &&
            (reserveWindow(account.usage)?.pct ?? -1) >= (reserveCeiling(account) as number) &&
            <OverReserveChip />}
```
- The marker: `markerPct={w.key === reserveWindow(account.usage)?.key ? reserveCeiling(account) : null}`.
- The input: `aria-label={`Weekly reserve override for ${account.name}`}`, `placeholder={account.pushLevelReservePct != null ? `push level's ${account.pushLevelReservePct}%` : 'override %'}`, `title="Percent of the weekly allowance to keep back from automation. Blank uses the push level's reserve."`.

- [ ] **Step 4: Run to verify they pass**

Run: `(cd web && npx vitest run src/features/accounts src/features/delivery/capacity && npm run typecheck && npx eslint src/api src/features/accounts src/features/delivery/capacity --max-warnings 0)`; then the FULL `(cd web && npm run test 2>&1 | tee /tmp/pr5-t4-vitest.log; echo "exit ${PIPESTATUS[0]}")` (other suites build `CapacityAccountRow` literals — typecheck catches a missing `reservePct`).
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add web/src/api/types.ts web/src/api/capacity.ts web/src/features/delivery/capacity/capacityStack.ts web/src/features/delivery/capacity/__fixtures__/capacity.ts web/src/features/delivery/capacity/capacityStack.test.ts web/src/features/accounts/AccountsManager.tsx web/src/features/accounts/AccountsReserve.test.tsx
git commit -F /tmp/pr5-t4-msg.txt
```
(plus any other test file whose `CapacityAccountRow` literal needed `reservePct`.)
`/tmp/pr5-t4-msg.txt`:
```
feat(web): Accounts page reads the effective reserve; readout rows show theirs

Spec 2026-09-20 D4/§5.1: label "Weekly reserve override" with the push
level's default as the placeholder; the over-reserve chip and the usage
marker use the effective ceiling (100 - reserve); the Accounts stack row's
expansion shows each account's reserve.

Co-Authored-By: Claude <model> <noreply@anthropic.com>
```

---

### Task 5: Feature docs

**Files:**
- Modify: `docs/product/features/capacity-governor.md:49-51` (the Push level note) and the PR bullets after the "One owner for product dials (PR 4)" bullet
- Modify: `docs/product/features/multi-account-management.md:61,72` (mention the override meaning)

- [ ] **Step 1: Edit**

`capacity-governor.md`: in the Push level note, `weekly-usage reserve on Claude accounts. A higher reserve makes fewer accounts count as available,` → `weekly-usage reserve on Claude accounts (the share KEPT BACK; a per-account override replaces it, spec 2026-09-20 D4). A higher reserve makes fewer accounts count as available,`. After the PR 4 bullet add:
```markdown
- **One reserve (PR 5)** — Since spec PR 5 (2026-09-20, D4) a per-account `weeklyReservePct` is an
  OVERRIDE of the push level's reserve with the same meaning (percent of the weekly allowance kept
  back; ceiling `100 − reserve`), and the plan honours it: `account_headroom(push_level=…)` judges
  each account by `effective_reserve_pct` (override, else `RESERVE_PCT[push level]`) and every
  readout row carries the `reservePct` it was judged by. `GET /api/accounts` publishes
  `effectiveReservePct` and `pushLevelReservePct`; Settings › Capacity › Accounts labels the field
  **Weekly reserve override** (placeholder "push level's N%"), and its over-reserve chip and usage
  marker sit at the effective ceiling. The CLI selector demotes only explicit overrides (a human's
  own pick is never steered by the push level, D12).
```
`multi-account-management.md:61`: `weekly reserve` → `weekly reserve override (percent kept back; blank = the capacity governor's push level)`; line 72 likewise if it names the field.

- [ ] **Step 2: Check and commit**

Run: `python3 scripts/brand_ratchet.py`
```bash
git add docs/product/features/capacity-governor.md docs/product/features/multi-account-management.md
git commit -F /tmp/pr5-t5-msg.txt
```
`/tmp/pr5-t5-msg.txt`:
```
docs(capacity): PR 5 — one reserve

Co-Authored-By: Claude <model> <noreply@anthropic.com>
```

---

### Task 6: Gate, rebase, PR

Same gate as #2080 / #2097 / #2102 / #2109; run by the coordinating session after the whole-branch review's fix wave.

- [ ] `ruff check src/ server/` · `mypy server/ --ignore-missing-imports` · `pytest tests/` (core, `PYTHONPATH=$PWD/src`) · `python3 scripts/brand_ratchet.py`.
- [ ] Full server suite (test-slot loop in the same shell), tee'd to `/tmp/pr5-server-tests.log`.
- [ ] `(cd web && npm run typecheck && npm run lint && npm run test && npm run build)`; `rm -rf web/dist`.
- [ ] `git fetch && git rebase origin/master`; re-run ruff + ratchet; in-flight PR check by file path.
- [ ] Push; `gh pr create --title "feat(capacity): one reserve, the plan honours a per-account override (spec PR 5)" --body-file /tmp/pr5-body.md --base master`; `gh pr merge <#> --squash --auto`. Body: D4 summary, the semantics flip and its zero live cost, `Refs #2060`, the Claude Code trailer.

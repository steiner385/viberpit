# Load-aware Account Selection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make account selection prefer accounts with fewer live sessions, so new/rotated sessions spread across accounts instead of piling onto one and triggering 429s.

**Architecture:** Live-session load is computed server-side (`SessionOrchestrator.live_session_load()`), passed as a plain `dict[str, float]` down the existing `select_account → pick_account → select_best` chain, and folded into the score by multiplicatively discounting each account's perishability. Every layer defaults `load={}`, so the CLI path and current behavior are unchanged.

**Tech Stack:** Python 3 (dataclasses), `unittest`, `pytest`. Core selector in `src/ccswitch/`; server orchestration in `server/sessions/`.

## Global Constraints

- Run tests against the prebuilt env: `<main-checkout>/.venv/bin/python -m pytest …` (a fresh worktree can't `uv sync`). Use `python -m pytest` from the worktree root.
- New `Config` fields MUST have defaults (config is loaded from optional JSON via `load_config`).
- New model/record columns are out of scope — load is derived live, never persisted.
- `select_best` MUST remain a pure function (no server imports); load arrives as data only.
- Backward compatibility is a hard requirement: with `load={}` the selector's ordering MUST be byte-for-byte identical to today's.
- Concurrency: any iteration over `self._drivers` MUST snapshot first (`list(self._drivers.keys())`) — the event loop mutates it.

---

### Task 1: Config knobs + load-aware scoring in `select_best`

**Files:**
- Modify: `src/ccswitch/config.py` (add two fields to the `Config` dataclass, after `prefer_soon_reset` on line 18)
- Modify: `src/ccswitch/selector.py` (`select_best`, lines 46-74)
- Test: `tests/test_selector.py`

**Interfaces:**
- Consumes: `ccswitch.oauth.Usage`, `ccswitch.config.Config`.
- Produces:
  - `Config.load_sensitivity: float = 0.5`, `Config.idle_session_weight: float = 0.3`
  - `select_best(usages: dict[str, Usage | None], reserved: set[str], cfg: Config, now: datetime, exclude: set[str] = frozenset(), load: dict[str, float] | None = None) -> str`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_selector.py` (inside `class TestSelector`):

```python
    # --- load-aware selection (live-session count discounts perishability) ---

    def test_load_demotes_busy_perishable_account(self):
        # Two equally-perishable accounts; the one already carrying live load is demoted.
        from ccswitch.oauth import Usage
        from ccswitch.selector import select_best
        soon = self.now + timedelta(minutes=30)
        usages = {
            "busy": Usage(40.0, soon, 20.0, soon, False),
            "free": Usage(40.0, soon, 20.0, soon, False),
        }
        self.assertEqual(
            select_best(usages, set(), self.cfg, self.now, load={"busy": 2.0}), "free")

    def test_empty_load_reproduces_legacy_order(self):
        # load={} must rank identically to the no-load call (regression guard).
        from ccswitch.oauth import Usage
        from ccswitch.selector import select_best
        soon = self.now + timedelta(minutes=20)
        later = self.now + timedelta(hours=5)
        usages = {
            "later": Usage(50.0, later, 20.0, later, False),
            "soon": Usage(50.0, soon, 20.0, soon, False),
        }
        self.assertEqual(
            select_best(usages, set(), self.cfg, self.now, load={}),
            select_best(usages, set(), self.cfg, self.now))

    def test_load_breaks_ties_when_perishability_off(self):
        # prefer_soon_reset off -> all composite scores 0; load (fewest sessions) decides.
        from ccswitch.selector import select_best
        self.cfg.prefer_soon_reset = False
        usages = {"alpha": usage(session=30.0), "beta": usage(session=30.0)}
        self.assertEqual(
            select_best(usages, set(), self.cfg, self.now,
                        load={"alpha": 1.0}), "beta")

    def test_heavier_load_loses_even_with_better_usage(self):
        # 'low' has better usage but heavy load; 'high' is idle and resets equally soon.
        # The load discount must outweigh the small usage edge.
        from ccswitch.oauth import Usage
        from ccswitch.selector import select_best
        soon = self.now + timedelta(minutes=30)
        usages = {
            "low": Usage(20.0, soon, 10.0, soon, False),
            "high": Usage(30.0, soon, 10.0, soon, False),
        }
        self.assertEqual(
            select_best(usages, set(), self.cfg, self.now, load={"low": 3.0}), "high")

    def test_large_load_is_bounded_no_crash(self):
        # A huge load value must not raise / produce NaN; it just sorts last.
        from ccswitch.oauth import Usage
        from ccswitch.selector import select_best
        soon = self.now + timedelta(minutes=30)
        usages = {
            "swamped": Usage(40.0, soon, 20.0, soon, False),
            "calm": Usage(40.0, soon, 20.0, soon, False),
        }
        self.assertEqual(
            select_best(usages, set(), self.cfg, self.now,
                        load={"swamped": 1e9}), "calm")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_selector.py -k "load" -v`
Expected: FAIL — `select_best()` got an unexpected keyword argument `load`.

- [ ] **Step 3: Add the Config fields**

In `src/ccswitch/config.py`, insert after `prefer_soon_reset: bool = True` (line 18):

```python
    # Live-session load awareness: a heavily-used account's perishability score is
    # divided by (1 + load_sensitivity * weighted_live_sessions), spreading new
    # sessions across accounts to avoid 429s. idle_session_weight is the per-session
    # weight of an idle live session; an actively-working session always weighs 1.0.
    load_sensitivity: float = 0.5
    idle_session_weight: float = 0.3
```

- [ ] **Step 4: Implement load-aware scoring in `select_best`**

Replace the body of `select_best` in `src/ccswitch/selector.py` (lines 46-74) with:

```python
def select_best(usages: dict[str, Usage | None], reserved: set[str],
                cfg: Config, now: datetime, exclude: set[str] = frozenset(),
                load: dict[str, float] | None = None) -> str:
    load = load or {}
    ceiling = cfg.hard_ceiling * 100.0
    candidates: list[tuple[float, float, float, float, str]] = []
    busy_resets: list[datetime] = []
    for name, u in usages.items():
        if name in exclude or name in reserved or u is None:
            continue
        at_ceiling = u.session_pct >= ceiling or u.weekly_pct >= ceiling
        if at_ceiling:
            # only the dimension(s) actually at ceiling contribute a reset time;
            # a reset can be None (0%/no active window) — exclude those
            resets = []
            if u.session_pct >= ceiling and u.session_resets_at is not None:
                resets.append(u.session_resets_at)
            if u.weekly_pct >= ceiling and u.weekly_resets_at is not None:
                resets.append(u.weekly_resets_at)
            if resets:
                busy_resets.append(min(resets))
            continue
        l = load.get(name, 0.0)
        perish = _perishability(u, ceiling, now) if cfg.prefer_soon_reset else 0.0
        # Multiplicative discount: live load shrinks an account's score, so a busy
        # perishable account can drop below a less-busy one. Bounded (1 + λ·l >= 1)
        # so no divide-by-zero / NaN; with load={} this reduces to the legacy key.
        composite = perish / (1.0 + cfg.load_sensitivity * l)
        # Sort ascending: most-perishable-after-discount first (-composite); then raw
        # load so spreading still applies when perishability can't differentiate
        # (prefer_soon_reset off or all-fresh -> every composite is 0); then the
        # legacy lowest-usage ordering; then name for determinism.
        candidates.append((-composite, l, u.session_pct, u.weekly_pct, name))
    if not candidates:
        raise AllAccountsBusy(min(busy_resets) if busy_resets else None)
    candidates.sort()
    return candidates[0][4]
```

- [ ] **Step 5: Run the full selector suite to verify pass + no regressions**

Run: `python -m pytest tests/test_selector.py -v`
Expected: PASS (all existing tests + the 5 new ones).

- [ ] **Step 6: Commit**

```bash
git add src/ccswitch/config.py src/ccswitch/selector.py tests/test_selector.py
git commit -m "feat(selector): load-aware scoring discounts perishability by live session count

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: Thread `load` through `pick_account` and `select_account`

**Files:**
- Modify: `src/ccswitch/accounts.py` (`pick_account`, lines 149-158)
- Modify: `server/sessions/selection.py` (`select_account`, lines 18-30)
- Test: `tests/test_accounts.py`

**Interfaces:**
- Consumes: `select_best(..., load=...)` from Task 1.
- Produces:
  - `pick_account(cfg: Config, exclude: set[str] = frozenset(), load: dict[str, float] | None = None) -> str`
  - `select_account(exclude: frozenset[str] = frozenset(), load: dict[str, float] | None = None) -> str`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_accounts.py` (inside the same test class as `test_pick_account_reserves_winner`, near line 122):

```python
    def test_pick_account_forwards_load_to_selector(self):
        with temp_env():
            from ccswitch import accounts
            _seed_account("alpha"); _seed_account("beta")
            # Equal usage so load is the deciding factor; alpha carries live load.
            def fake_usage(name):
                return usage(session=30.0)
            with mock.patch.object(accounts, "usage_for", side_effect=fake_usage):
                picked = accounts.pick_account(
                    accounts.load_config(), load={"alpha": 2.0})
                self.assertEqual(picked, "beta")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_accounts.py -k forwards_load -v`
Expected: FAIL — `pick_account()` got an unexpected keyword argument `load`.

- [ ] **Step 3: Implement `pick_account` forwarding**

Replace `pick_account` in `src/ccswitch/accounts.py` (lines 149-158) with:

```python
def pick_account(cfg: Config, exclude: set[str] = frozenset(),
                 load: dict[str, float] | None = None) -> str:
    names = _account_names()
    usages = {n: usage_for(n) for n in names}
    now = datetime.now(timezone.utc)
    now_ts = time.time()
    with ledger.locked_ledger() as led:
        reserved = {n for n in names if ledger.reservation_active(led, n, now_ts)}
        name = select_best(usages, reserved, cfg, now, exclude, load or {})
        ledger.reserve(led, name, now_ts + cfg.reservation_ttl_sec)
    return name
```

- [ ] **Step 4: Implement `select_account` forwarding**

Replace `select_account` in `server/sessions/selection.py` (lines 18-30) with:

```python
def select_account(exclude: frozenset[str] = frozenset(),
                   load: dict[str, float] | None = None) -> str:
    """Select the best available account, skipping any in *exclude*.

    Writes a soft reservation into the ledger so concurrent calls don't
    double-assign the same account (R8 / FR-009a). *load* is an optional
    per-account weighted live-session count; accounts already carrying load are
    discounted so new sessions spread out (avoids 429s).

    Raises:
        AllAccountsBusy: when every account is at or above the usage ceiling.
            The exception carries ``.earliest_reset`` (datetime | None).
    """
    cfg = load_config()
    # pick_account already calls select_best + ledger.reserve under a lock.
    return pick_account(cfg, exclude=set(exclude), load=load)
```

- [ ] **Step 5: Run tests to verify pass**

Run: `python -m pytest tests/test_accounts.py -k "pick_account" -v`
Expected: PASS (both `test_pick_account_reserves_winner` and `test_pick_account_forwards_load_to_selector`).

- [ ] **Step 6: Commit**

```bash
git add src/ccswitch/accounts.py server/sessions/selection.py tests/test_accounts.py
git commit -m "feat(selection): thread live-session load through pick_account/select_account

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: `live_session_load()` on the orchestrator + wire both call sites

**Files:**
- Modify: `server/sessions/orchestrator.py` (add `live_session_load` near `has_live_session` ~line 675; pass `load=` at the two `select_account` call sites, lines 279 and 1137; add a `load_config` import if not present)
- Test: `server/tests/test_orchestrator_has_live_session.py`

**Interfaces:**
- Consumes: `self._drivers: dict[str, SessionDriver]`, `self._records: dict[str, SessionRecord]` (record has `.account: str`, `.status: str`), module-level `_ACTIVE_STATUSES`, `ccswitch.config.load_config`, `select_account(exclude, load)` from Task 2.
- Produces: `SessionOrchestrator.live_session_load(self) -> dict[str, float]`

- [ ] **Step 1: Write the failing tests**

Append to `server/tests/test_orchestrator_has_live_session.py`:

```python
# ---------------------------------------------------------------------------
# live_session_load — weighted count of live sessions per account
# ---------------------------------------------------------------------------

def _plant(orch, sid, account, status):
    from server.sessions.types import SessionRecord
    driver = MagicMock()
    driver.account = account
    orch._drivers[sid] = driver
    orch._records[sid] = SessionRecord(
        id=sid, name=sid, account=account, claude_session_id="c",
        cols=80, rows=24, cwd="/tmp", status=status)


def test_live_session_load_weights_working_and_idle():
    orch = SessionOrchestrator(sdk_factory=MagicMock())
    _plant(orch, "s1", "acct1", "in_progress")  # working -> 1.0
    _plant(orch, "s2", "acct1", "idle")         # idle    -> 0.3
    _plant(orch, "s3", "acct2", "working")      # working -> 1.0

    load = orch.live_session_load()
    assert load["acct1"] == 1.3
    assert load["acct2"] == 1.0
    assert "acct3" not in load  # accounts with no live sessions are absent


def test_live_session_load_ignores_driverless_records():
    orch = SessionOrchestrator(sdk_factory=MagicMock())
    from server.sessions.types import SessionRecord
    # A record with no live driver (detached/rehydrated) must not count.
    orch._records["dead"] = SessionRecord(
        id="dead", name="dead", account="acct1", claude_session_id="c",
        cols=80, rows=24, cwd="/tmp", status="working")
    assert orch.live_session_load() == {}


def test_live_session_load_skips_driver_without_record():
    orch = SessionOrchestrator(sdk_factory=MagicMock())
    d = MagicMock(); d.account = "acct1"
    orch._drivers["orphan"] = d  # no matching record
    assert orch.live_session_load() == {}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest server/tests/test_orchestrator_has_live_session.py -k live_session_load -v`
Expected: FAIL — `SessionOrchestrator` has no attribute `live_session_load`.

- [ ] **Step 3: Ensure `load_config` is imported**

In `server/sessions/orchestrator.py`, check the imports near the top (the `from ccswitch...` block around line 27). If `load_config` is not already imported, add:

```python
from ccswitch.config import load_config
```

- [ ] **Step 4: Implement `live_session_load`**

In `server/sessions/orchestrator.py`, add this method immediately after `has_live_session` (after line 675):

```python
    def live_session_load(self) -> dict[str, float]:
        """Weighted count of live sessions per account, for load-aware selection.

        A live session is one with an entry in ``_drivers`` (same liveness notion as
        ``has_live_session``). Actively-working sessions (_ACTIVE_STATUSES) weigh 1.0;
        idle live sessions weigh ``cfg.idle_session_weight``. Accounts with no live
        session are absent from the map (treated as load 0 by the selector).

        Snapshots ``_drivers`` before iterating — the event loop mutates it while this
        may run from a threadpool thread (see ``has_live_session``).
        """
        cfg = load_config()
        load: dict[str, float] = {}
        for sid in list(self._drivers.keys()):
            rec = self._records.get(sid)
            if rec is None:
                continue
            weight = 1.0 if rec.status in _ACTIVE_STATUSES else cfg.idle_session_weight
            load[rec.account] = load.get(rec.account, 0.0) + weight
        return load
```

- [ ] **Step 5: Wire the two `select_account` call sites**

In `server/sessions/orchestrator.py`, line ~279 (in `create_session`), change:

```python
            account = select_account(exclude=self._recovery.quarantined(now=self._clock()))
```
to:
```python
            account = select_account(
                exclude=self._recovery.quarantined(now=self._clock()),
                load=self.live_session_load())
```

And line ~1137 (in `_rotate_away`), change:

```python
            new_account = select_account(exclude=exclude)
```
to:
```python
            new_account = select_account(exclude=exclude, load=self.live_session_load())
```

- [ ] **Step 6: Run the new tests + the existing has_live_session suite**

Run: `python -m pytest server/tests/test_orchestrator_has_live_session.py -v`
Expected: PASS (existing tests + 3 new `live_session_load` tests).

- [ ] **Step 7: Commit**

```bash
git add server/sessions/orchestrator.py server/tests/test_orchestrator_has_live_session.py
git commit -m "feat(orchestrator): live_session_load() feeds load-aware selection at create/rotate

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 4: Full-suite verification

**Files:** none (verification only).

- [ ] **Step 1: Run the server + core suites**

Run: `python -m pytest tests/test_selector.py tests/test_accounts.py server/tests/test_orchestrator_has_live_session.py server/tests/test_orchestrator_recovery.py -v`
Expected: PASS (no regressions in recovery/rotation, which also calls `select_account`).

- [ ] **Step 2: Run the broader selection/orchestrator regression**

Run: `python -m pytest tests/ server/tests/ -q`
Expected: PASS. If any pre-existing unrelated failures appear, note them but do not fix out of scope.

---

## Self-Review

- **Spec coverage:** Config knobs (Task 1) · `select_best` formula + secondary load key (Task 1) · threading `load` through `pick_account`/`select_account` (Task 2) · `live_session_load()` + both call sites (Task 3) · backward-compat regression guard (Task 1 Step 1 `test_empty_load_reproduces_legacy_order`) · weighting working vs idle (Task 3 tests) · bounded/no-NaN (Task 1 `test_large_load_is_bounded_no_crash`). All spec sections map to a task.
- **Placeholder scan:** none — all steps carry concrete code/commands.
- **Type consistency:** `load: dict[str, float] | None = None` is used identically in `select_best`, `pick_account`, `select_account`; `live_session_load() -> dict[str, float]` matches the `load=` parameter type at the call sites. Sort tuple widened from 4-tuple to 5-tuple consistently (`candidates[0][4]`).

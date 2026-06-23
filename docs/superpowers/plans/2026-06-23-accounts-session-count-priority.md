# Accounts session counts + next-pick priority — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** On the Accounts screen, show each account's live session count (active + idle, split) and its rotation-algorithm priority rank (`#1 next`, `#2`, …) with a reason tooltip.

**Architecture:** Extract the ranking from `select_best()` into a reusable `rank_accounts()` (single source of truth for the formula). Add `live_session_counts()` to the orchestrator beside the existing `live_session_load()`. Surface both through the orchestrator adapter, the remote daemon RPC, and the `GET /api/accounts` response. The React `AccountRow` renders a sessions chip and a rank badge.

**Tech Stack:** Python (FastAPI, SQLAlchemy, dataclasses) backend · React + Vite + TS + Tailwind frontend · pytest + vitest.

## Global Constraints

- **Single source of truth (Constitution VI):** the ranking formula lives ONLY in `selector.py`. The API/TS layers must not re-derive it.
- **No new frontend dependency:** the reason tooltip uses the native `title` attribute (no Radix/shadcn tooltip — only `button.tsx` exists under `web/src/components/ui/`).
- **Backend tests** run against the prebuilt env with the worktree on the path:
  `PYTHONPATH=<worktree>/src <main-checkout>/.venv/bin/python -m pytest …`
  Auth-touching tests need an allowlist; run API tests with
  `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 … pytest`.
- **Web tests/gates** run in `web/`: `vitest run`, real type gate `npx tsc -b --force` (NOT `tsc --noEmit`), `eslint` with max-warnings 0. Symlink `web/node_modules` from the deploy worktree.
- **Backend lint gates (CI fails fast):** `ruff` + `mypy` must pass; add type annotations on new functions.
- **New model columns** (none in this plan) would need to be nullable; this plan adds no DB columns.
- Commit message trailer: `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`.

---

### Task 1: `rank_accounts()` — reusable ordering in the selector

**Files:**
- Modify: `src/ccswitch/selector.py`
- Test: `tests/test_selector.py` (repo-root `tests/`; co-locate with existing selector tests — verify path with `git ls-files | grep test_selector`)

**Interfaces:**
- Produces:
  - `@dataclass RankedAccount: name: str; rank: int; reason: str`
  - `rank_accounts(usages: dict[str, Usage | None], reserved: set[str], cfg: Config, now: datetime, exclude: set[str] = frozenset(), load: dict[str, float] | None = None) -> tuple[list[RankedAccount], datetime | None]` — returns `(ranked_in_priority_order, earliest_busy_reset)`.
  - `select_best(...)` unchanged signature; now delegates to `rank_accounts`.

- [ ] **Step 1: Write failing tests**

Add to `tests/test_selector.py`:

```python
from datetime import datetime, timedelta, timezone
from ccswitch.config import Config
from ccswitch.oauth import Usage
from ccswitch.selector import rank_accounts, select_best, RankedAccount

NOW = datetime(2026, 6, 23, 12, 0, tzinfo=timezone.utc)
SOON = NOW + timedelta(minutes=30)


def _cfg(**kw):
    return Config(**kw)


def test_rank_accounts_orders_like_select_best():
    usages = {
        "a": Usage(40.0, SOON, 20.0, SOON, False),
        "b": Usage(10.0, SOON, 5.0, SOON, False),
    }
    ranked, earliest = rank_accounts(usages, set(), _cfg(), NOW)
    names = [r.name for r in ranked]
    assert names[0] == select_best(usages, set(), _cfg(), NOW)
    assert [r.rank for r in ranked] == [1, 2]
    assert earliest is None


def test_rank_accounts_excludes_at_ceiling_and_reports_reset():
    reset = NOW + timedelta(hours=2)
    usages = {
        "busy": Usage(100.0, reset, 50.0, reset, False),
        "ok": Usage(10.0, SOON, 5.0, SOON, False),
    }
    ranked, earliest = rank_accounts(usages, set(), _cfg(), NOW)
    assert [r.name for r in ranked] == ["ok"]
    assert earliest == reset


def test_rank_accounts_all_busy_returns_empty_with_earliest():
    reset = NOW + timedelta(hours=1)
    usages = {"x": Usage(100.0, reset, 0.0, None, False)}
    ranked, earliest = rank_accounts(usages, set(), _cfg(), NOW)
    assert ranked == []
    assert earliest == reset


def test_rank_reason_perishable_lowest_load():
    usages = {"a": Usage(40.0, SOON, 20.0, SOON, False)}
    ranked, _ = rank_accounts(usages, set(), _cfg(), NOW, load={})
    assert ranked[0].reason == "perishable quota, lowest load"


def test_rank_reason_perishable_sharing_load():
    usages = {"a": Usage(40.0, SOON, 20.0, SOON, False)}
    ranked, _ = rank_accounts(usages, set(), _cfg(), NOW, load={"a": 2.0})
    assert ranked[0].reason == "perishable quota, sharing live load"


def test_rank_reason_fresh_saved_for_last():
    # No reset window on the binding dimension -> composite 0 -> "fresh".
    usages = {"a": Usage(0.0, None, 0.0, None, False)}
    ranked, _ = rank_accounts(usages, set(), _cfg(), NOW)
    assert ranked[0].reason == "fresh quota — saved for last"


def test_select_best_still_raises_all_busy():
    import pytest
    from ccswitch.selector import AllAccountsBusy
    reset = NOW + timedelta(hours=1)
    usages = {"x": Usage(100.0, reset, 0.0, None, False)}
    with pytest.raises(AllAccountsBusy) as ei:
        select_best(usages, set(), _cfg(), NOW)
    assert ei.value.earliest_reset == reset
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=$PWD/src python -m pytest tests/test_selector.py -k "rank_accounts or rank_reason or still_raises" -v`
Expected: FAIL — `cannot import name 'rank_accounts'` / `RankedAccount`.

- [ ] **Step 3: Implement `rank_accounts` and refactor `select_best`**

In `src/ccswitch/selector.py`, after `_perishability` and before `select_best`, add:

```python
@dataclass
class RankedAccount:
    name: str
    rank: int
    reason: str


def _reason(composite: float, acct_load: float) -> str:
    """Short, deterministic explanation of an account's standing (for the UI tooltip).

    composite == 0 means the binding dimension has no active reset window — a fresh
    account whose quota isn't perishable yet, so the selector saves it for last.
    """
    if composite <= 0.0:
        return "fresh quota — saved for last"
    return "perishable quota, lowest load" if acct_load == 0.0 else "perishable quota, sharing live load"


def rank_accounts(usages: dict[str, "Usage | None"], reserved: set[str],
                  cfg: Config, now: datetime, exclude: set[str] = frozenset(),
                  load: dict[str, float] | None = None,
                  ) -> tuple[list[RankedAccount], datetime | None]:
    """Rank all eligible accounts in selection-priority order.

    Returns ``(ranked, earliest_reset)``: ``ranked`` is the priority-ordered list
    (rank 1 = next pick); ``earliest_reset`` is the soonest reset among at-ceiling
    accounts when nothing is eligible (else None). ``select_best`` is the rank-1 name.
    """
    load = load or {}
    ceiling = cfg.hard_ceiling * 100.0
    candidates: list[tuple[float, float, float, float, str]] = []
    busy_resets: list[datetime] = []
    for name, u in usages.items():
        if name in exclude or name in reserved or u is None:
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
    earliest = min(busy_resets) if busy_resets else None
    ranked = [
        RankedAccount(name=c[4], rank=i + 1, reason=_reason(-c[0], c[1]))
        for i, c in enumerate(candidates)
    ]
    return ranked, earliest
```

Then replace the body of `select_best` (keep its signature/docstring) with:

```python
def select_best(usages: dict[str, Usage | None], reserved: set[str],
                cfg: Config, now: datetime, exclude: set[str] = frozenset(),
                load: dict[str, float] | None = None) -> str:
    ranked, earliest = rank_accounts(usages, reserved, cfg, now, exclude, load)
    if not ranked:
        raise AllAccountsBusy(earliest)
    return ranked[0].name
```

- [ ] **Step 4: Run the full selector suite**

Run: `PYTHONPATH=$PWD/src python -m pytest tests/test_selector.py -v`
Expected: PASS (new tests + all pre-existing `select_best` tests still green).

- [ ] **Step 5: Lint**

Run: `python -m ruff check src/ccswitch/selector.py && python -m mypy src/ccswitch/selector.py`
Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add src/ccswitch/selector.py tests/test_selector.py
git commit -m "feat(selector): expose rank_accounts() reused by select_best"
```

---

### Task 2: `live_session_counts()` on the orchestrator

**Files:**
- Modify: `server/sessions/orchestrator.py` (add method beside `live_session_load`, ~line 805)
- Test: `server/tests/test_orchestrator_has_live_session.py`

**Interfaces:**
- Produces: `Orchestrator.live_session_counts() -> dict[str, dict[str, int]]` — `{account: {"active": int, "idle": int}}`; accounts with no live session omitted.

- [ ] **Step 1: Write failing tests**

Add to `server/tests/test_orchestrator_has_live_session.py` (follow the existing helpers in that file for building an orch with fake drivers/records; mirror `test_live_session_load_weights_working_and_idle`):

```python
def test_live_session_counts_splits_active_and_idle(orch_with_two_accounts):
    orch = orch_with_two_accounts  # reuse the fixture/builder used by the load tests
    # account "a": one working (active) + one idle; account "b": one idle
    _add_live(orch, "s1", account="a", status="working")
    _add_live(orch, "s2", account="a", status="idle")
    _add_live(orch, "s3", account="b", status="idle")
    counts = orch.live_session_counts()
    assert counts["a"] == {"active": 1, "idle": 1}
    assert counts["b"] == {"active": 0, "idle": 1}


def test_live_session_counts_omits_accounts_without_live_sessions(orch_with_two_accounts):
    orch = orch_with_two_accounts
    assert orch.live_session_counts() == {}
```

> NOTE for implementer: match the exact fixture/helper names already used by the `live_session_load` tests in this file (e.g. how they populate `_drivers`/`_records`). Use the same status strings the file uses for active vs idle; `_ACTIVE_STATUSES` = `{"in_progress", "working", "waiting_input"}`.

- [ ] **Step 2: Run to verify failure**

Run: `PYTHONPATH=$PWD/src python -m pytest server/tests/test_orchestrator_has_live_session.py -k live_session_counts -v`
Expected: FAIL — `AttributeError: 'SessionOrchestrator' object has no attribute 'live_session_counts'`.

- [ ] **Step 3: Implement**

In `server/sessions/orchestrator.py`, directly below `live_session_load` (after line ~805):

```python
    def live_session_counts(self) -> dict[str, dict[str, int]]:
        """Raw active/idle live-session counts per account, for the Accounts UI.

        Same liveness notion as ``live_session_load`` (``_drivers`` ∩ ``_records``,
        ``_ACTIVE_STATUSES`` = active) but returns integer counts split into
        ``{"active", "idle"}`` instead of weighted floats. Accounts with no live
        session are omitted. Snapshots ``_drivers`` before iterating (event-loop
        mutation guard; see ``has_live_session``).
        """
        counts: dict[str, dict[str, int]] = {}
        for sid in list(self._drivers.keys()):
            rec = self._records.get(sid)
            if rec is None:
                continue
            bucket = counts.setdefault(rec.account, {"active": 0, "idle": 0})
            key = "active" if rec.status in _ACTIVE_STATUSES else "idle"
            bucket[key] += 1
        return counts
```

- [ ] **Step 4: Run to verify pass**

Run: `PYTHONPATH=$PWD/src python -m pytest server/tests/test_orchestrator_has_live_session.py -v`
Expected: PASS.

- [ ] **Step 5: Lint**

Run: `python -m ruff check server/sessions/orchestrator.py && python -m mypy server/sessions/orchestrator.py`
Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add server/sessions/orchestrator.py server/tests/test_orchestrator_has_live_session.py
git commit -m "feat(orchestrator): live_session_counts() split active/idle"
```

---

### Task 3: Expose load + counts through the adapter, remote orchestrator, and daemon RPC

**Files:**
- Modify: `server/sessions/facade.py` (`InProcessOrchestratorAdapter`, after line 56)
- Modify: `server/sessions/remote.py` (`RemoteOrchestrator`, after `active_session_count`, ~line 196)
- Modify: `server/ipc/orchestrator_rpc.py` (`_SIMPLE` list, line 71)
- Test: `server/tests/test_orchestrator_rpc_breadth.py`

**Interfaces:**
- Consumes: Task 2's `live_session_counts()`; existing `live_session_load()`.
- Produces (both adapters):
  - `async live_session_load(self) -> dict[str, float]`
  - `async live_session_counts(self) -> dict[str, dict[str, int]]`

- [ ] **Step 1: Write failing test (RPC breadth)**

In `server/tests/test_orchestrator_rpc_breadth.py` (follow its existing pattern for asserting a method name is registered and round-trips through the remote orchestrator), add:

```python
def test_live_session_load_and_counts_registered():
    # The breadth test already builds an app + registers orchestrator RPC and
    # asserts each method name is dispatchable. Add the two new names to whatever
    # expected-set / parametrize list this file uses.
    for name in ("live_session_load", "live_session_counts"):
        assert name in _registered_rpc_method_names()  # match the helper this file uses
```

> NOTE: this file already enumerates registered RPC methods (it is the "breadth" guard). Add `"live_session_load"` and `"live_session_counts"` to its expected list rather than inventing a new harness. If it round-trips values, assert `live_session_counts` returns a dict.

- [ ] **Step 2: Run to verify failure**

Run: `PYTHONPATH=$PWD/src python -m pytest server/tests/test_orchestrator_rpc_breadth.py -v`
Expected: FAIL — the two names are not registered.

- [ ] **Step 3a: Add adapter passthroughs** (`server/sessions/facade.py`, after line 56)

```python
    async def live_session_load(self): return await _maybe_await(self._orch.live_session_load())
    async def live_session_counts(self): return await _maybe_await(self._orch.live_session_counts())
```

- [ ] **Step 3b: Add remote orchestrator methods** (`server/sessions/remote.py`, after `active_session_count`, ~line 196)

```python
    async def live_session_load(self):
        return await self._client.call("live_session_load")

    async def live_session_counts(self):
        return await self._client.call("live_session_counts")
```

- [ ] **Step 3c: Register the daemon RPC handlers** (`server/ipc/orchestrator_rpc.py`)

Both return JSON-safe dicts, so add them to the existing `_SIMPLE` list (line 71):

```python
    _SIMPLE = [
        "archive_session", "rename_session", "interrupt",
        "set_permission_mode", "cancel_queued", "resolve_permission", "answer_question",
        "transcript_path", "pending_questions", "pop_pending_initial_prompt",
        "pop_pending_initial_attachment_ids", "command_catalog", "run_command", "list_archived",
        "latest_suggestions", "seed_idle_suggestions",
        "live_session_load", "live_session_counts",
    ]
```

- [ ] **Step 4: Run to verify pass**

Run: `PYTHONPATH=$PWD/src python -m pytest server/tests/test_orchestrator_rpc_breadth.py server/tests/test_remote_mode_wiring.py -v`
Expected: PASS (breadth includes the two new methods; wiring still green).

- [ ] **Step 5: Lint**

Run: `python -m ruff check server/sessions/facade.py server/sessions/remote.py server/ipc/orchestrator_rpc.py && python -m mypy server/sessions/facade.py server/sessions/remote.py server/ipc/orchestrator_rpc.py`
Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add server/sessions/facade.py server/sessions/remote.py server/ipc/orchestrator_rpc.py server/tests/test_orchestrator_rpc_breadth.py
git commit -m "feat(ipc): expose live_session_load/counts via adapter + daemon RPC"
```

---

### Task 4: Add `liveSessions` / `rank` / `rankReason` to `GET /api/accounts`

**Files:**
- Modify: `server/api/accounts.py` (`_account_response` line 64; `get_accounts` lines 102-128)
- Test: `server/tests/` — find the accounts-API test with `git ls-files | grep -i 'test.*account'` (e.g. `test_accounts_api.py`); add there.

**Interfaces:**
- Consumes: `rank_accounts` (Task 1); `orch.live_session_load()` / `orch.live_session_counts()` (Task 3).
- Produces: each account dict gains `"liveSessions": {"active": int, "idle": int}`, `"rank": int | None`, `"rankReason": str | None`.

- [ ] **Step 1: Write failing tests**

In the accounts-API test module (reuse its existing client/allowlist fixtures), add:

```python
def test_get_accounts_includes_live_sessions_and_rank(client, ...):
    # Arrange: two accounts with usage such that one outranks the other, and
    # one live active session on account "a" (use the same orchestrator stub the
    # other accounts tests use; set live_session_counts/load on it).
    resp = client.get("/api/accounts")
    assert resp.status_code == 200
    body = {a["name"]: a for a in resp.json()}
    assert body["a"]["liveSessions"] == {"active": 1, "idle": 0}
    assert body["b"]["liveSessions"] == {"active": 0, "idle": 0}
    # ranks are 1-based and contiguous over eligible accounts
    ranks = sorted(a["rank"] for a in resp.json() if a["rank"] is not None)
    assert ranks == [1, 2]
    ranked_one = next(a for a in resp.json() if a["rank"] == 1)
    assert isinstance(ranked_one["rankReason"], str)


def test_get_accounts_at_ceiling_has_null_rank(client, ...):
    # account pinned at 100% session usage -> rank None, rankReason None,
    # liveSessions still present.
    resp = client.get("/api/accounts")
    busy = next(a for a in resp.json() if a["name"] == "busy")
    assert busy["rank"] is None
    assert busy["rankReason"] is None
    assert "liveSessions" in busy
```

> NOTE: the existing accounts tests already stub `app.state.sessions` (the adapter) and its `usage()`. Extend that stub with `live_session_load()`/`live_session_counts()` returning the dicts your scenario needs. Match the file's fixture names.

- [ ] **Step 2: Run to verify failure**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 PYTHONPATH=$PWD/src python -m pytest server/tests/test_accounts_api.py -v`
Expected: FAIL — keys `liveSessions`/`rank` missing.

- [ ] **Step 3a: Extend `_account_response`** (`server/api/accounts.py` line 64)

```python
def _account_response(
    name: str,
    status_str: str,
    usage,
    nickname: str | None = None,
    live_sessions: dict[str, int] | None = None,
    rank: int | None = None,
    rank_reason: str | None = None,
) -> dict[str, Any]:
    """Build the account wire format — never includes tokens."""
    return {
        "name": name,
        "status": status_str,
        "usage": _usage_to_dict(usage),
        "nickname": nickname,
        "liveSessions": live_sessions or {"active": 0, "idle": 0},
        "rank": rank,
        "rankReason": rank_reason,
    }
```

- [ ] **Step 3b: Rewrite `get_accounts`** (`server/api/accounts.py` lines 102-128)

Add imports at the top of the file:

```python
from datetime import datetime, timezone
from ccswitch.config import load_config
from ccswitch.selector import rank_accounts
```

Replace the handler body:

```python
@router.get("")
async def get_accounts(
    request: Request,
    _identity: str = Depends(current_identity),
) -> list[dict]:
    """Return all accounts with live usage, live session counts, and next-pick rank.

    Never includes token or credential material.
    """
    orch = _get_orchestrator(request)
    accounts_info = list_accounts()

    # Fetch live usage for every account (poller dedupes concurrent fetches).
    usages = {acct["name"]: await orch.usage(acct["name"]) for acct in accounts_info}

    # Live session counts (display) + weighted load (ranking input).
    counts = await orch.live_session_counts()
    load = await orch.live_session_load()

    # Rank for display: ignore transient soft reservations so ranks stay stable
    # (a just-picked account would otherwise vanish for reservation_ttl_sec).
    ranked, _earliest = rank_accounts(
        usages, reserved=frozenset(), cfg=load_config(),
        now=datetime.now(timezone.utc), load=load,
    )
    rank_by_name = {r.name: (r.rank, r.reason) for r in ranked}

    result = []
    for acct in accounts_info:
        name = acct["name"]
        rank, reason = rank_by_name.get(name, (None, None))
        result.append(_account_response(
            name,
            acct.get("status", "unknown"),
            usages[name],
            acct.get("nickname"),
            live_sessions=counts.get(name) or {"active": 0, "idle": 0},
            rank=rank,
            rank_reason=reason,
        ))
    return result
```

Also update `refresh_account` (line ~131) to keep passing through `_account_response` unchanged — it returns `rank=None`/`rankReason=None` by default and `live_sessions` defaulted; explicitly pass that account's own counts so the count stays accurate after a manual refresh:

```python
    counts = await orch.live_session_counts()
    return _account_response(
        name, status_str, usage, nickname,
        live_sessions=counts.get(name) or {"active": 0, "idle": 0},
    )
```

- [ ] **Step 4: Run to verify pass**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 PYTHONPATH=$PWD/src python -m pytest server/tests/test_accounts_api.py -v`
Expected: PASS.

- [ ] **Step 5: Lint**

Run: `python -m ruff check server/api/accounts.py && python -m mypy server/api/accounts.py`
Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add server/api/accounts.py server/tests/test_accounts_api.py
git commit -m "feat(api): accounts list returns liveSessions + next-pick rank"
```

---

### Task 5: Frontend — sessions chip + rank badge on each account row

**Files:**
- Modify: `web/src/api/types.ts` (`Account` interface, lines 1-22)
- Modify: `web/src/components/AccountsManager.tsx` (`AccountRow`, ~lines 284-300; add badge components near `UsageBadge`)
- Test: `web/src/components/__tests__/AccountsManager.test.tsx` (find with `git ls-files web | grep -i account.*test`) and update any shared account factory found via `git ls-files web | grep -i factor`.

**Interfaces:**
- Consumes: API fields `liveSessions`, `rank`, `rankReason` (Task 4).

- [ ] **Step 1: Extend the `Account` type** (`web/src/api/types.ts`)

```ts
export interface LiveSessionCounts {
  active: number
  idle: number
}

export interface Account {
  name: string
  status: AccountStatus
  usage: {
    session: UsageWindow
    weekly: UsageWindow
  } | null
  nickname?: string | null
  liveSessions: LiveSessionCounts
  rank: number | null
  rankReason: string | null
}
```

- [ ] **Step 2: Write failing tests** (`web/src/components/__tests__/AccountsManager.test.tsx`)

Use the existing render helper for a single `AccountRow`/`AccountsStrip`. Add an account factory default (or update the shared factory) with `liveSessions: { active: 0, idle: 0 }, rank: null, rankReason: null`.

```tsx
it('shows split active/idle session counts', () => {
  renderRow(makeAccount({ liveSessions: { active: 2, idle: 1 } }))
  expect(screen.getByText('2 active · 1 idle')).toBeInTheDocument()
})

it('shows a dash when no live sessions', () => {
  renderRow(makeAccount({ liveSessions: { active: 0, idle: 0 } }))
  expect(screen.getByText('—')).toBeInTheDocument()
})

it('marks the next pick with #1 next and a reason tooltip', () => {
  renderRow(makeAccount({ rank: 1, rankReason: 'perishable quota, lowest load' }))
  const badge = screen.getByText('#1 next')
  expect(badge).toHaveAttribute('title', 'perishable quota, lowest load')
})

it('renders a plain rank number for non-first picks', () => {
  renderRow(makeAccount({ rank: 2, rankReason: 'fresh quota — saved for last' }))
  expect(screen.getByText('#2')).toBeInTheDocument()
})

it('shows unavailable when an account has no rank', () => {
  renderRow(makeAccount({ rank: null }))
  expect(screen.getByText('unavailable')).toBeInTheDocument()
})
```

- [ ] **Step 3: Run to verify failure**

Run (in `web/`): `npx vitest run src/components/__tests__/AccountsManager.test.tsx`
Expected: FAIL — text not found.

- [ ] **Step 4: Implement the badge components + render them**

In `web/src/components/AccountsManager.tsx`, add near `UsageBadge` (helper components):

```tsx
function SessionsBadge({ counts }: { counts: LiveSessionCounts }) {
  const { active, idle } = counts
  let text: string
  if (active === 0 && idle === 0) text = '—'
  else if (active > 0 && idle > 0) text = `${active} active · ${idle} idle`
  else if (active > 0) text = `${active} active`
  else text = `${idle} idle`
  return (
    <span
      className={cn(
        'text-xs whitespace-nowrap',
        active === 0 && idle === 0 ? 'text-gray-400 dark:text-gray-600' : 'text-gray-600 dark:text-gray-300',
      )}
      aria-label="Live sessions using this account"
    >
      {text}
    </span>
  )
}

function RankBadge({ rank, reason }: { rank: number | null; reason: string | null }) {
  if (rank === null) {
    return <span className="text-xs text-gray-400 dark:text-gray-600">unavailable</span>
  }
  const label = rank === 1 ? '#1 next' : `#${rank}`
  return (
    <span
      title={reason ?? undefined}
      className={cn(
        'text-xs font-medium rounded px-1.5 py-0.5 whitespace-nowrap',
        rank === 1
          ? 'bg-green-100 text-green-700 dark:bg-green-900/40 dark:text-green-300'
          : 'text-gray-500 dark:text-gray-400',
      )}
    >
      {label}
    </span>
  )
}
```

Add the `LiveSessionCounts` import to the existing types import line. Then in the row's metric cluster (after the `StatusPill`, before/after the usage badges, ~line 286-300), insert:

```tsx
            <StatusPill status={account.status} />
            <SessionsBadge counts={account.liveSessions} />
            <RankBadge rank={account.rank} reason={account.rankReason} />
            {account.usage && (
```

- [ ] **Step 5: Run to verify pass**

Run (in `web/`): `npx vitest run src/components/__tests__/AccountsManager.test.tsx`
Expected: PASS.

- [ ] **Step 6: Type + lint gates**

Run (in `web/`): `npx tsc -b --force && npx eslint src/components/AccountsManager.tsx src/api/types.ts`
Expected: clean. Fix any other call sites/fixtures the stricter `Account` type now flags (TS will point them out).

- [ ] **Step 7: Run the full web suite** (catches factory-mock breakage — see project memory)

Run (in `web/`): `npx vitest run`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add web/src/api/types.ts web/src/components/AccountsManager.tsx web/src/components/__tests__/AccountsManager.test.tsx
git commit -m "feat(web): show live session counts + next-pick rank on Accounts"
```

---

## Self-Review

**Spec coverage:**
- Split active/idle counts → Task 2 (`live_session_counts`), Task 4 (response), Task 5 (`SessionsBadge`). ✓
- Per-account rank badge → Task 1 (`rank_accounts`), Task 4 (response), Task 5 (`RankBadge`). ✓
- Reason tooltip → Task 1 (`_reason`), Task 5 (`title` attr). ✓
- Remote mode parity → Task 3 (adapter + remote + RPC) + breadth test. ✓
- `/refresh` returns `rank: null` → Task 4 (default args, explicit counts). ✓
- At-ceiling / zero-session / unavailable edge cases → Tasks 1, 4, 5 tests. ✓
- Single source of truth (no TS re-derivation) → ranking only in `selector.py`. ✓

**Deviation from spec (intentional, documented):** display ranking uses `reserved=frozenset()` rather than live reservations, for stable ranks. `select_best` still passes real `reserved`, so selection is unchanged. Documented inline in Task 4 Step 3b.

**Placeholder scan:** test files reference existing fixtures by description with explicit NOTEs to match real fixture names (their exact names can't be known without the test file open); all production code is fully written. No TBD/TODO in production steps.

**Type consistency:** `rank_accounts -> tuple[list[RankedAccount], datetime | None]`; `RankedAccount(name, rank, reason)`; API maps `r.reason → rankReason`, `r.rank → rank`; TS `Account.rank/rankReason/liveSessions` match the wire keys (`rank`, `rankReason`, `liveSessions`). `live_session_counts -> dict[str, dict[str,int]]` consistent across orchestrator, adapter, remote, API. ✓

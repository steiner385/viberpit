# Insights slice B — Duration Regressions + Runner-Wait Health — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add two panels — Duration Regressions and Runner-Wait Health — to the existing per-repo Insights tab, computed on-demand from the read-only `check_durations` / `runner_waits` tables.

**Architecture:** Two new `HistoryStore` read methods feed an extended `orchestrator.check_insights` payload (same authenticated route + daemon RPC — no new IPC surface). The frontend adds two more `CheckTable`s to `InsightsView`.

**Tech Stack:** Python (sync `sqlite3`, `mode=ro`), FastAPI, React + TS + Vitest.

## Global Constraints

- Store is **read-only** (`mode=ro`): SELECT only, never write to `pipeline_history.db`.
- `conclusion` in real data is **UPPERCASE** (`SUCCESS`/`FAILURE`/…). Regressions filter exact `conclusion='SUCCESS'` (matches the existing `recent_durations` / slice-A convention).
- Reuse the existing module-level `percentile(sorted_xs, q)` and `median(xs)` helpers in `server/pipeline/history.py` — do NOT add parallel copies.
- `check_insights` must **never raise**: any store error or missing store → the full empty payload shape.
- No new `RemoteOrchestrator` method or RPC registration — the payload is a plain JSON-safe dict passed through untouched; the daemon proxy + breadth guards already cover `check_insights`.
- Test env (worktree has no built venv): run pytest via `/mnt/ssk-ssd/tony/GitHub/.ccswitch/clones/steiner385/cc-account-switcher/.venv/bin/python -m pytest …` from the worktree root; web via `npx …` from `web/`.
- Regression constants (mirror Plotroom `estimator/regression.ts`): recent K=10, prior N=20, min samples=30, flag ratio ≥ 1.5, flag min delta ≥ 60s.

---

### Task 1: `duration_regressions` store method + `CheckRegressionStat`

**Files:**
- Modify: `server/pipeline/history.py` (add dataclass + constants + method beside `flakiest_checks`)
- Test: `server/tests/test_history_check_insights.py` (append; reuse the existing `_store` helper that builds a temp `check_durations` DB)

**Interfaces:**
- Consumes: module-level `median(xs)` helper (sorts internally).
- Produces: `CheckRegressionStat(check_name: str, event: str, prior_p50: float, recent_p50: float, ratio: float, since: str)` with `to_dict()`; `duration_regressions(self, repo: str) -> list[CheckRegressionStat]` (window-independent — no `since` arg).

- [ ] **Step 1: Write the failing tests**

Append to `server/tests/test_history_check_insights.py` (the `_store(rows)` helper already inserts `check_durations` rows shaped `(repo, check_name, event, duration_secs, completed_at, conclusion, head_sha, run_attempt, started_at, run_number)` — check the helper's exact column tuple and match it):

```python
def _dur_rows(check, event, durs, base="2026-06-01T00:00:00Z"):
    # durs is oldest→newest; completed_at increases so ORDER BY completed_at DESC
    # yields newest first. All SUCCESS.
    rows = []
    for i, d in enumerate(durs):
        ts = f"2026-06-{i+1:02d}T00:00:00Z"
        rows.append(("o/r", check, event, float(d), ts, "SUCCESS", "", 1, ts, i + 1))
    return rows


def test_duration_regressions_flags_a_real_step():
    # 20 prior samples ~30s, 10 recent ~120s → ratio 4, delta 90 → flagged.
    durs = [30.0] * 20 + [120.0] * 10
    st = _store(_dur_rows("slow-check", "pull_request", durs))
    regs = st.duration_regressions("o/r")
    assert len(regs) == 1
    r = regs[0]
    assert r.check_name == "slow-check" and r.event == "pull_request"
    assert r.prior_p50 == 30.0 and r.recent_p50 == 120.0
    assert r.ratio == 4.0
    # since = oldest completed_at of the recent-10 window = the 21st inserted row (day 21)
    assert r.since == "2026-06-21T00:00:00Z"


def test_duration_regressions_ignores_below_ratio():
    # recent 40 vs prior 30 → ratio 1.33 < 1.5 → not flagged.
    durs = [30.0] * 20 + [40.0] * 10
    st = _store(_dur_rows("mild", "push", durs))
    assert st.duration_regressions("o/r") == []


def test_duration_regressions_ignores_below_abs_delta():
    # recent 75 vs prior 50 → ratio 1.5 but delta 25 < 60 → not flagged.
    durs = [50.0] * 20 + [75.0] * 10
    st = _store(_dur_rows("smallabs", "push", durs))
    assert st.duration_regressions("o/r") == []


def test_duration_regressions_needs_min_samples():
    durs = [10.0] * 15 + [200.0] * 10  # only 25 samples < 30
    st = _store(_dur_rows("thin", "push", durs))
    assert st.duration_regressions("o/r") == []


def test_duration_regressions_sorted_by_ratio_desc():
    rows = _dur_rows("a", "push", [10.0] * 20 + [30.0] * 10)      # ratio 3
    rows += _dur_rows("b", "push", [10.0] * 20 + [100.0] * 10)    # ratio 10
    st = _store(rows)
    regs = st.duration_regressions("o/r")
    assert [r.check_name for r in regs] == ["b", "a"]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd /mnt/ssk-ssd/tony/GitHub/.ccswitch/worktrees/ccswitch/5a95d24e && /mnt/ssk-ssd/tony/GitHub/.ccswitch/clones/steiner385/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_history_check_insights.py -k duration_regressions -v`
Expected: FAIL — `AttributeError: 'HistoryStore' object has no attribute 'duration_regressions'`.

- [ ] **Step 3: Implement the dataclass + constants + method**

Add near the other `@dataclass(frozen=True)` stats (`CheckDurationStat`/`CheckFlakeStat`) and constants:

```python
_REG_RECENT_K = 10
_REG_PRIOR_N = 20
_REG_MIN = 30
_REG_RATIO = 1.5
_REG_MIN_DELTA = 60.0


@dataclass(frozen=True)
class CheckRegressionStat:
    check_name: str
    event: str
    prior_p50: float
    recent_p50: float
    ratio: float
    since: str

    def to_dict(self) -> dict:
        return {
            "check_name": self.check_name,
            "event": self.event,
            "prior_p50": self.prior_p50,
            "recent_p50": self.recent_p50,
            "ratio": self.ratio,
            "since": self.since,
        }
```

Add the method to `HistoryStore` (beside `flakiest_checks`):

```python
def duration_regressions(self, repo: str) -> list[CheckRegressionStat]:
    """Checks whose rolling-median SUCCESS duration stepped up (Plotroom
    measureDurationStep). Window-independent: last 10 vs prior 20 samples."""
    pairs = self._conn.execute(
        "SELECT DISTINCT check_name, event FROM check_durations WHERE repo=?",
        (repo,),
    ).fetchall()
    out: list[CheckRegressionStat] = []
    for p in pairs:
        rows = self._conn.execute(
            "SELECT duration_secs, completed_at FROM check_durations "
            "WHERE repo=? AND check_name=? AND event=? AND conclusion='SUCCESS' "
            "ORDER BY completed_at DESC LIMIT ?",
            (repo, p["check_name"], p["event"], _REG_MIN),
        ).fetchall()
        if len(rows) < _REG_MIN:
            continue
        durs = [float(r["duration_secs"]) for r in rows]
        recent = median(durs[:_REG_RECENT_K])
        prior = median(durs[_REG_RECENT_K:_REG_MIN])
        if prior <= 0:
            continue
        ratio = recent / prior
        if ratio < _REG_RATIO or (recent - prior) < _REG_MIN_DELTA:
            continue
        out.append(
            CheckRegressionStat(
                check_name=p["check_name"],
                event=p["event"],
                prior_p50=prior,
                recent_p50=recent,
                ratio=ratio,
                since=rows[_REG_RECENT_K - 1]["completed_at"],
            )
        )
    out.sort(key=lambda s: s.ratio, reverse=True)
    return out
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `/mnt/ssk-ssd/tony/GitHub/.ccswitch/clones/steiner385/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_history_check_insights.py -k duration_regressions -v`
Expected: 5 passed. Then `-m ruff check server/pipeline/history.py` → clean.

- [ ] **Step 5: Commit**

```bash
git add server/pipeline/history.py server/tests/test_history_check_insights.py
git commit -m "feat(insights): duration_regressions history read method"
```

---

### Task 2: `runner_wait_health` store method + `RunnerWaitStat`

**Files:**
- Modify: `server/pipeline/history.py` (add dataclass + method)
- Test: `server/tests/test_history_runner_waits.py` (new — its own `runner_waits` fixture)

**Interfaces:**
- Consumes: module-level `percentile(sorted_xs, q)` helper.
- Produces: `RunnerWaitStat(event: str, p50: float, p90: float, prev_p50: float | None, n: int)` with `to_dict()`; `runner_wait_health(self, repo: str, since_iso: str, prev_since_iso: str) -> list[RunnerWaitStat]`.

- [ ] **Step 1: Write the failing tests**

Create `server/tests/test_history_runner_waits.py`:

```python
import sqlite3
from server.pipeline.history import HistoryStore


def _store(rows):
    """rows: (repo, check_name, event, wait_secs, started_at, pool)."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE runner_waits (repo TEXT, check_name TEXT, event TEXT, "
        "wait_secs REAL, started_at TEXT, pool TEXT)"
    )
    conn.executemany("INSERT INTO runner_waits VALUES (?,?,?,?,?,?)", rows)
    conn.commit()
    st = HistoryStore.__new__(HistoryStore)
    st._conn = conn
    return st


def test_runner_wait_health_p50_p90_per_event():
    # current window (>= since): pull_request waits 10,20,30,40,50 → p50=30, p90=50
    rows = [
        ("o/r", "c", "pull_request", w, f"2026-06-10T00:0{i}:00Z", None)
        for i, w in enumerate([10, 20, 30, 40, 50])
    ]
    st = _store(rows)
    out = st.runner_wait_health("o/r", "2026-06-09T00:00:00Z", "2026-06-01T00:00:00Z")
    assert len(out) == 1
    s = out[0]
    assert s.event == "pull_request" and s.p50 == 30.0 and s.p90 == 50.0
    assert s.n == 5 and s.prev_p50 is None


def test_runner_wait_health_prev_window_and_boundary():
    rows = [
        # prior window [prev_since, since): 100,200 → prev_p50=100
        ("o/r", "c", "push", 100.0, "2026-06-05T00:00:00Z", None),
        ("o/r", "c", "push", 200.0, "2026-06-06T00:00:00Z", None),
        # exactly at since → counts as CURRENT
        ("o/r", "c", "push", 10.0, "2026-06-09T00:00:00Z", None),
        ("o/r", "c", "push", 20.0, "2026-06-10T00:00:00Z", None),
    ]
    st = _store(rows)
    out = st.runner_wait_health("o/r", "2026-06-09T00:00:00Z", "2026-06-01T00:00:00Z")
    s = out[0]
    assert s.n == 2 and s.p50 == 10.0 and s.prev_p50 == 100.0


def test_runner_wait_health_sorted_by_event_and_excludes_empty_current():
    rows = [
        ("o/r", "c", "push", 5.0, "2026-06-10T00:00:00Z", None),
        ("o/r", "c", "merge_group", 7.0, "2026-06-10T00:00:00Z", None),
        # this event only has a PRIOR-window row → excluded (no current samples)
        ("o/r", "c", "schedule", 9.0, "2026-06-05T00:00:00Z", None),
    ]
    st = _store(rows)
    out = st.runner_wait_health("o/r", "2026-06-09T00:00:00Z", "2026-06-01T00:00:00Z")
    assert [s.event for s in out] == ["merge_group", "push"]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `/mnt/ssk-ssd/tony/GitHub/.ccswitch/clones/steiner385/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_history_runner_waits.py -v`
Expected: FAIL — `AttributeError: … 'runner_wait_health'`.

- [ ] **Step 3: Implement the dataclass + method**

```python
@dataclass(frozen=True)
class RunnerWaitStat:
    event: str
    p50: float
    p90: float
    prev_p50: float | None
    n: int

    def to_dict(self) -> dict:
        return {
            "event": self.event,
            "p50": self.p50,
            "p90": self.p90,
            "prev_p50": self.prev_p50,
            "n": self.n,
        }
```

Method on `HistoryStore` (beside `expected_runner_wait`):

```python
def runner_wait_health(
    self, repo: str, since_iso: str, prev_since_iso: str
) -> list[RunnerWaitStat]:
    """Per-event runner pickup wait p50/p90 over the current window, with the
    prior equal-length window's p50 for a delta. Rows at exactly since_iso are
    current."""
    rows = self._conn.execute(
        "SELECT event, wait_secs, started_at FROM runner_waits "
        "WHERE repo=? AND started_at >= ?",
        (repo, prev_since_iso),
    ).fetchall()
    cur: dict[str, list[float]] = {}
    prev: dict[str, list[float]] = {}
    for r in rows:
        w = float(r["wait_secs"])
        bucket = cur if r["started_at"] >= since_iso else prev
        bucket.setdefault(r["event"], []).append(w)
    out: list[RunnerWaitStat] = []
    for ev in sorted(cur):
        c = sorted(cur[ev])
        pv = sorted(prev.get(ev, []))
        out.append(
            RunnerWaitStat(
                event=ev,
                p50=percentile(c, 0.5),
                p90=percentile(c, 0.9),
                prev_p50=percentile(pv, 0.5) if pv else None,
                n=len(c),
            )
        )
    return out
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `/mnt/ssk-ssd/tony/GitHub/.ccswitch/clones/steiner385/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_history_runner_waits.py -v`
Expected: 3 passed. Then `-m ruff check server/pipeline/history.py` → clean.

- [ ] **Step 5: Commit**

```bash
git add server/pipeline/history.py server/tests/test_history_runner_waits.py
git commit -m "feat(insights): runner_wait_health history read method"
```

---

### Task 3: Extend `orchestrator.check_insights`

**Files:**
- Modify: `server/sessions/orchestrator.py` (the existing `check_insights` method)
- Test: `server/tests/test_check_insights_orch.py` (append)

**Interfaces:**
- Consumes: `store.duration_regressions(repo)` (Task 1), `store.runner_wait_health(repo, since_iso, prev_since_iso)` (Task 2).
- Produces: `check_insights` dict now also carries `"regressions": [...]` and `"runner_wait_health": [...]`; `prev_since_iso = now − 2×window`.

- [ ] **Step 1: Write the failing tests**

Append to `server/tests/test_check_insights_orch.py` (mirror the existing fake-store construction there — a `SessionOrchestrator.__new__` with a fake `_pr_population.history_store()`):

```python
def test_check_insights_includes_regressions_and_runner_waits(monkeypatch):
    from datetime import datetime, timezone

    class _Reg:
        def to_dict(self):
            return {"check_name": "c", "event": "push", "prior_p50": 30.0,
                    "recent_p50": 120.0, "ratio": 4.0, "since": "2026-06-21T00:00:00Z"}

    class _RW:
        def to_dict(self):
            return {"event": "push", "p50": 10.0, "p90": 50.0, "prev_p50": 8.0, "n": 5}

    seen = {}

    class _Store:
        def slowest_checks(self, repo, since):
            seen["since"] = since
            return []

        def flakiest_checks(self, repo, since):
            return []

        def duration_regressions(self, repo):
            seen["reg_repo"] = repo
            return [_Reg()]

        def runner_wait_health(self, repo, since, prev_since):
            seen["prev_since"] = prev_since
            return [_RW()]

    orch = _orch_with_store(_Store())  # helper already in this test module
    now = datetime(2026, 7, 8, tzinfo=timezone.utc)
    out = orch.check_insights("o/r", "7d", now=now)

    assert out["regressions"][0]["ratio"] == 4.0
    assert out["runner_wait_health"][0]["event"] == "push"
    # window=7d → since = now-7d, prev_since = now-14d
    assert seen["since"] == "2026-07-01T00:00:00Z"
    assert seen["prev_since"] == "2026-06-24T00:00:00Z"
    assert seen["reg_repo"] == "o/r"


def test_check_insights_empty_payload_has_new_keys():
    orch = _orch_with_store(None)  # no store
    out = orch.check_insights("o/r", "7d")
    assert out["regressions"] == [] and out["runner_wait_health"] == []
```

If the existing test module builds the orchestrator inline rather than via an `_orch_with_store` helper, extract that construction into a small `_orch_with_store(store)` helper first (a store of `None` must make `_pr_population.history_store()` return `None`), then use it in both new tests and leave the existing tests working.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `/mnt/ssk-ssd/tony/GitHub/.ccswitch/clones/steiner385/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_check_insights_orch.py -v`
Expected: the two new tests FAIL (`KeyError: 'regressions'`), existing ones still pass.

- [ ] **Step 3: Extend the method**

In `check_insights`, build the empty payload once (DRY — replaces the two inline empty dicts), add `prev_since_iso`, and call the two new store methods inside the existing `try`:

```python
win = window if window in _WINDOWS else "7d"
store = self._pr_population.history_store()
empty = {"repo": repo, "window": win, "slowest": [], "flakiest": [],
         "regressions": [], "runner_wait_health": []}
if store is None or not repo:
    return empty
now = now or datetime.now(timezone.utc)
since_iso = (now - _WINDOWS[win]).strftime("%Y-%m-%dT%H:%M:%SZ")
prev_since_iso = (now - 2 * _WINDOWS[win]).strftime("%Y-%m-%dT%H:%M:%SZ")
try:
    slowest = store.slowest_checks(repo, since_iso)
    flakiest = store.flakiest_checks(repo, since_iso)
    regressions = store.duration_regressions(repo)
    runner_wait = store.runner_wait_health(repo, since_iso, prev_since_iso)
except Exception:
    return empty
return {
    "repo": repo, "window": win,
    "slowest": [s.to_dict() for s in slowest],
    "flakiest": [f.to_dict() for f in flakiest],
    "regressions": [r.to_dict() for r in regressions],
    "runner_wait_health": [w.to_dict() for w in runner_wait],
}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `/mnt/ssk-ssd/tony/GitHub/.ccswitch/clones/steiner385/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_check_insights_orch.py server/tests/test_insights_route.py server/tests/test_remote_orchestrator_breadth.py server/tests/test_orchestrator_rpc_breadth.py -v`
Expected: all pass (route + breadth guards still green — no new method to register). Then `-m ruff check server/sessions/orchestrator.py` + `-m mypy server/ --ignore-missing-imports` → clean.

- [ ] **Step 5: Commit**

```bash
git add server/sessions/orchestrator.py server/tests/test_check_insights_orch.py
git commit -m "feat(insights): check_insights returns regressions + runner-wait health"
```

---

### Task 4: Frontend — types + two InsightsView panels

**Files:**
- Modify: `web/src/api/types.ts` (add two interfaces, extend `CheckInsights`)
- Modify: `web/src/components/InsightsView.tsx` (two more `CheckTable`s)
- Test: `web/src/components/InsightsView.test.tsx` (extend the existing test's mock + assertions)

**Interfaces:**
- Consumes: `CheckInsights` from the API (now with `regressions` + `runner_wait_health`); existing `CheckTable<T>` / `Column<T>` and the `fmtSecs` formatter in `InsightsView.tsx`.
- Produces: rendered "Duration regressions" and "Runner-wait health" tables.

- [ ] **Step 1: Add the TS types**

In `web/src/api/types.ts`, after `CheckFlakeStat`:

```ts
export interface CheckRegressionStat {
  check_name: string
  event: string
  prior_p50: number
  recent_p50: number
  ratio: number
  since: string
}

export interface RunnerWaitStat {
  event: string
  p50: number
  p90: number
  prev_p50: number | null
  n: number
}
```

Extend `CheckInsights`:

```ts
export interface CheckInsights {
  repo: string
  window: string
  slowest: CheckDurationStat[]
  flakiest: CheckFlakeStat[]
  regressions: CheckRegressionStat[]
  runner_wait_health: RunnerWaitStat[]
}
```

- [ ] **Step 2: Write the failing test**

Extend `web/src/components/InsightsView.test.tsx` — add `regressions` + `runner_wait_health` to the mocked `useCheckInsights` return, and assert both new panels render. Match the file's existing mock style (read it first):

```tsx
// in the mocked useCheckInsights data:
regressions: [{ check_name: 'build', event: 'push', prior_p50: 30, recent_p50: 120, ratio: 4, since: '2026-06-21T00:00:00Z' }],
runner_wait_health: [{ event: 'push', p50: 12, p90: 40, prev_p50: 8, n: 5 }],

// new assertions:
expect(screen.getByText(/Duration regressions/i)).toBeInTheDocument()
expect(screen.getByText(/build/)).toBeInTheDocument()
expect(screen.getByText(/Runner-wait health/i)).toBeInTheDocument()
```

Run: `cd /mnt/ssk-ssd/tony/GitHub/.ccswitch/worktrees/ccswitch/5a95d24e/web && npx vitest run src/components/InsightsView.test.tsx`
Expected: FAIL (panels not rendered / text not found).

- [ ] **Step 3: Add the two panels**

In `web/src/components/InsightsView.tsx`, after the existing "Flakiest checks" `CheckTable`, add (reusing `CheckTable`, `Column`, `fmtSecs`, and the `data` object):

```tsx
<CheckTable<CheckRegressionStat>
  title="Duration regressions (live — not affected by window)"
  rows={data?.regressions ?? []}
  empty="No regressions detected"
  columns={[
    { header: 'Check', cell: (r) => r.check_name },
    { header: 'Event', cell: (r) => r.event },
    { header: 'Was', cell: (r) => fmtSecs(r.prior_p50) },
    { header: 'Now', cell: (r) => fmtSecs(r.recent_p50) },
    { header: 'Slowdown', cell: (r) => `${r.ratio.toFixed(1)}×` },
    { header: 'Since', cell: (r) => r.since.slice(0, 10) },
  ]}
/>
<CheckTable<RunnerWaitStat>
  title="Runner-wait health"
  rows={data?.runner_wait_health ?? []}
  empty="No runner-wait data"
  columns={[
    { header: 'Event', cell: (r) => r.event },
    { header: 'p50', cell: (r) => fmtSecs(r.p50) },
    { header: 'p90', cell: (r) => fmtSecs(r.p90) },
    {
      header: 'Δ vs prev',
      cell: (r) =>
        r.prev_p50 == null
          ? '—'
          : `${r.p50 >= r.prev_p50 ? '↑' : '↓'} ${fmtSecs(Math.abs(r.p50 - r.prev_p50))}`,
    },
    { header: 'n', cell: (r) => String(r.n) },
  ]}
/>
```

Add `CheckRegressionStat, RunnerWaitStat` to the `types` import at the top of the file. Match the exact `Column<T>` shape the existing tables use (if it's `{ header, cell }`, the above is correct; if it differs, adapt).

- [ ] **Step 4: Run the tests + gates**

```bash
cd /mnt/ssk-ssd/tony/GitHub/.ccswitch/worktrees/ccswitch/5a95d24e/web
npx vitest run src/components/InsightsView.test.tsx
npx vitest run src/components/        # full component suite stays green
npx tsc --noEmit
npx eslint src/api/types.ts src/components/InsightsView.tsx src/components/InsightsView.test.tsx
```
Expected: tests pass, full suite green, tsc + eslint clean.

- [ ] **Step 5: Commit**

```bash
git add web/src/api/types.ts web/src/components/InsightsView.tsx web/src/components/InsightsView.test.tsx
git commit -m "feat(insights): duration-regression + runner-wait panels in InsightsView"
```

---

## Self-Review

**Spec coverage:** Component 1 (duration_regressions) → Task 1. Component 2 (runner_wait_health) → Task 2. Component 3 (orchestrator extension, prev_since = now−2×window, never-raises) → Task 3. Component 4 (TS types + two panels + empty states) → Task 4. Non-goal (per-pool starvation) correctly absent. Testing section covered across tasks.

**Placeholder scan:** No TBD/TODO; every code/test step has concrete code and exact run commands with expected output.

**Type consistency:** `CheckRegressionStat` fields (`check_name, event, prior_p50, recent_p50, ratio, since`) identical in the Python dataclass (Task 1), orchestrator `to_dict` passthrough (Task 3), and TS interface (Task 4). `RunnerWaitStat` fields (`event, p50, p90, prev_p50, n`) identical in Task 2 dataclass and Task 4 interface; `prev_p50: float | None` ↔ `number | null`. `duration_regressions(repo)` / `runner_wait_health(repo, since_iso, prev_since_iso)` signatures match between store (Tasks 1-2) and orchestrator call sites (Task 3). Constants match the Global Constraints. `median` used for regression p50 (matches Plotroom), `percentile(_,0.5)` for runner-wait p50 (matches slice A) — intentional, per spec.

# Insights — Check Health Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a per-repo **Insights → Check Health** tab with two panels — Slowest/most-variable checks and Flakiest checks — computed on-demand from the existing read-only `check_durations` data.

**Architecture:** Two new read methods on the read-only `HistoryStore` do pure SQL + percentile/flake math; a sync `orchestrator.check_insights(repo, window)` maps the window to an ISO cutoff and assembles a JSON-safe payload (with a `RemoteOrchestrator` proxy + daemon RPC); `GET /api/pipeline/insights/checks` serves it; a new `InsightsView` tab (its own repo dropdown + window selector) renders two tables.

**Tech Stack:** Python FastAPI + sync `sqlite3` (read-only) backend · React + Vite + TS + Tailwind frontend · pytest + vitest.

## Global Constraints

- **Read-only history:** `HistoryStore` opens `pipeline_history.db` with `mode=ro`. Add only **read** methods; never write to it. No new tables, no migrations.
- **No GitHub call on the read path** — the endpoint reads the store only.
- **Daemon breadth guards:** `check_insights` is called by the API on `orch`, so it MUST get a `RemoteOrchestrator` proxy (`server/sessions/remote.py`) AND a registered RPC (`server/ipc/orchestrator_rpc.py`), or `test_remote_orchestrator_breadth` / `test_orchestrator_rpc_breadth` fail.
- **Auth:** the endpoint depends on `current_identity`.
- **Empty/absent history:** when `history_store()` is `None` or the repo has no rows, return empty lists — never 500.
- **Panels (MVP):** Slowest/most-variable + Flakiest only. Per-repo grain. Window selector: 24h / 7d / 14d / 30d, default 7d, applied via `completed_at >= cutoff`.
- **Flake definition:** a `head_sha` that produced **both** a pass and a fail for a check is a flaky commit; `flake_pct = 100 * flaky_commits / commits_with_a_definite_outcome`.
- **Conclusion normalization (lowercased):** pass = `{success}`; fail = `{failure, cancelled, timed_out, error, action_required}`; ignore `{skipped, neutral, '', None}` for the outcome test.
- **Gates (all must pass):** `ruff check`, `mypy server/ --ignore-missing-imports` (**easily missed**), `pytest`; web: `vitest run`, `tsc --noEmit`, `eslint`.
- **Test env (this worktree):** no built Python env — run server tests as
  `cd /mnt/ssk-ssd/tony/GitHub/.ccswitch/worktrees/ccswitch/5a95d24e && /mnt/ssk-ssd/tony/GitHub/.ccswitch/clones/steiner385/cc-account-switcher/.venv/bin/python -m pytest …` (from the worktree root). `web/node_modules` is a real `npm ci` install; run web tools in `web/` via `npx`.
- **Async test convention:** `@pytest.mark.asyncio` (pytest-asyncio). But `HistoryStore` is sync `sqlite3`, so the store tests (Tasks 2–3) are **plain sync tests** — no asyncio.

---

## File Structure

**Backend (modified):**
- `server/pipeline/history.py` — add `CheckDurationStat`, `CheckFlakeStat` dataclasses + `slowest_checks` / `flakiest_checks` read methods.
- `server/sessions/orchestrator.py` — add `check_insights(repo, window, now=None)`.
- `server/sessions/remote.py` — add `RemoteOrchestrator.check_insights` proxy.
- `server/ipc/orchestrator_rpc.py` — add `check_insights` RPC handler + registration.
- `server/api/pipeline.py` — add `GET /insights/checks`.

**Frontend (new):**
- `web/src/components/InsightsView.tsx` — tab body: repo dropdown + window selector + two panels.
- `web/src/components/CheckTable.tsx` — presentational table used by both panels.

**Frontend (modified):**
- `web/src/api/types.ts` — `CheckDurationStat`, `CheckFlakeStat`.
- `web/src/api/hooks.ts` — `QK.pipelineInsights` + `useCheckInsights`.
- `web/src/components/Dashboard.tsx` — `TabId` + tab button/ref + panel.
- `web/src/App.tsx` — `/insights` route.

---

## Task 1: Verify the real `check_durations` conclusion vocabulary (spike)

Confirm the `conclusion` values and that `head_sha`/`run_attempt` are populated, so the flake normalization set is correct (constitution III).

**Files:**
- Create: `docs/superpowers/spikes/2026-07-01-check-durations-conclusions.md`

- [ ] **Step 1: Inspect the real history DB (read-only)**

Run (the operational DB lives at `~/.claude-accounts/pipeline_history.db`):

```bash
PY=/mnt/ssk-ssd/tony/GitHub/.ccswitch/clones/steiner385/cc-account-switcher/.venv/bin/python
$PY - <<'EOF'
import sqlite3, os
p = os.path.expanduser("~/.claude-accounts/pipeline_history.db")
if not os.path.exists(p):
    print("NO DB — use the documented conclusion set"); raise SystemExit
c = sqlite3.connect(f"file:{p}?mode=ro", uri=True); c.row_factory = sqlite3.Row
print("distinct conclusions:", [dict(r) for r in c.execute(
  "SELECT conclusion, COUNT(*) n FROM check_durations GROUP BY conclusion ORDER BY n DESC")])
print("rows w/ head_sha:", c.execute("SELECT COUNT(*) FROM check_durations WHERE head_sha IS NOT NULL AND head_sha<>''").fetchone()[0])
print("max run_attempt:", c.execute("SELECT MAX(run_attempt) FROM check_durations").fetchone()[0])
EOF
```

- [ ] **Step 2: Record findings**

Write the observed conclusion values to the spike doc. If any fail-like value is present that the Global Constraints' `fail` set omits, add it there and note it (the plan's flake math keys off that set). If the DB is absent, note that the documented set stands and move on.

- [ ] **Step 3: Commit**

```bash
git add docs/superpowers/spikes/2026-07-01-check-durations-conclusions.md
git commit -m "spike(insights): confirm check_durations conclusion vocabulary"
```

---

## Task 2: `slowest_checks` store method

**Files:**
- Modify: `server/pipeline/history.py`
- Test: `server/tests/test_history_check_insights.py`

**Interfaces:**
- Produces: `CheckDurationStat(check_name:str, event:str, p50:float, p90:float, ratio:float|None, n:int)` with `to_dict()`, and
  `HistoryStore.slowest_checks(repo:str, since_iso:str, limit:int=10, event:str|None=None) -> list[CheckDurationStat]`.
- Consumes: the existing module-level `percentile(sorted_xs, q)`.

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_history_check_insights.py
import sqlite3
from server.pipeline.history import HistoryStore

_SCHEMA = (
    "CREATE TABLE check_durations (repo TEXT, check_name TEXT, event TEXT, "
    "duration_secs REAL, completed_at TEXT, conclusion TEXT, head_sha TEXT, "
    "run_attempt INTEGER, started_at TEXT, run_number INTEGER)"
)

def _store(tmp_path, rows):
    p = tmp_path / "ph.db"
    c = sqlite3.connect(str(p))
    c.execute(_SCHEMA)
    c.executemany(
        "INSERT INTO check_durations (repo,check_name,event,duration_secs,completed_at,"
        "conclusion,head_sha,run_attempt,run_number) VALUES (?,?,?,?,?,?,?,?,?)", rows)
    c.commit(); c.close()
    return HistoryStore(str(p))

def test_slowest_checks_ranks_by_p50_with_ratio(tmp_path):
    rows = [
        # slow check: durations 100,100,100,900 (p90 high → ratio > 1)
        ("o/n", "slow", "pr", 100, "2026-07-01T00:00:00Z", "success", "s1", 1, 1),
        ("o/n", "slow", "pr", 100, "2026-07-01T00:00:00Z", "success", "s2", 1, 2),
        ("o/n", "slow", "pr", 100, "2026-07-01T00:00:00Z", "success", "s3", 1, 3),
        ("o/n", "slow", "pr", 900, "2026-07-01T00:00:00Z", "success", "s4", 1, 4),
        # fast check
        ("o/n", "fast", "pr", 10, "2026-07-01T00:00:00Z", "success", "f1", 1, 5),
        ("o/n", "fast", "pr", 10, "2026-07-01T00:00:00Z", "success", "f2", 1, 6),
        # a FAILURE row must be ignored by the duration panel
        ("o/n", "fast", "pr", 9999, "2026-07-01T00:00:00Z", "failure", "f3", 1, 7),
    ]
    s = _store(tmp_path, rows)
    out = s.slowest_checks("o/n", since_iso="2026-01-01T00:00:00Z")
    assert [x.check_name for x in out] == ["slow", "fast"]      # ranked by p50 desc
    slow = out[0]
    assert slow.p50 == 100 and slow.p90 == 900 and slow.n == 4
    assert slow.ratio == 9.0
    assert out[1].n == 2                                        # failure row excluded

def test_slowest_checks_filters_by_window(tmp_path):
    rows = [
        ("o/n", "c", "pr", 50, "2026-06-01T00:00:00Z", "success", "a", 1, 1),  # old
        ("o/n", "c", "pr", 60, "2026-07-01T00:00:00Z", "success", "b", 1, 2),  # in window
    ]
    s = _store(tmp_path, rows)
    out = s.slowest_checks("o/n", since_iso="2026-06-15T00:00:00Z")
    assert len(out) == 1 and out[0].n == 1 and out[0].p50 == 60
```

- [ ] **Step 2: Run it to verify it fails**

Run: `pytest server/tests/test_history_check_insights.py -v`
Expected: FAIL — `HistoryStore` has no `slowest_checks`.

- [ ] **Step 3: Implement the dataclass + method**

In `server/pipeline/history.py`, add after the `Expected` dataclass:

```python
@dataclass(frozen=True)
class CheckDurationStat:
    check_name: str
    event: str
    p50: float
    p90: float
    ratio: float | None
    n: int

    def to_dict(self) -> dict:
        return {"check_name": self.check_name, "event": self.event, "p50": self.p50,
                "p90": self.p90, "ratio": self.ratio, "n": self.n}
```

And add this method to the `HistoryStore` class:

```python
    def slowest_checks(
        self, repo: str, since_iso: str, limit: int = 10, event: str | None = None,
    ) -> "list[CheckDurationStat]":
        q = ("SELECT check_name, event, duration_secs FROM check_durations "
             "WHERE repo=? AND conclusion='SUCCESS' AND completed_at >= ?")
        args: list = [repo, since_iso]
        if event is not None:
            q += " AND event=?"
            args.append(event)
        groups: dict[tuple[str, str], list[float]] = {}
        for r in self._conn.execute(q, args).fetchall():
            if r["duration_secs"] is None:
                continue
            groups.setdefault((r["check_name"], r["event"] or ""), []).append(
                float(r["duration_secs"]))
        stats: list[CheckDurationStat] = []
        for (name, ev), vals in groups.items():
            s = sorted(vals)
            p50 = percentile(s, 0.5)
            p90 = percentile(s, 0.9)
            stats.append(CheckDurationStat(
                check_name=name, event=ev, p50=p50, p90=p90,
                ratio=(p90 / p50) if p50 else None, n=len(s)))
        stats.sort(key=lambda x: x.p50, reverse=True)
        return stats[:limit]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest server/tests/test_history_check_insights.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add server/pipeline/history.py server/tests/test_history_check_insights.py
git commit -m "feat(insights): slowest_checks history read method"
```

---

## Task 3: `flakiest_checks` store method

**Files:**
- Modify: `server/pipeline/history.py`
- Test: `server/tests/test_history_check_insights.py`

**Interfaces:**
- Produces: `CheckFlakeStat(check_name:str, event:str, flake_pct:float, flaky_commits:int, total_commits:int, n:int)` with `to_dict()`, and
  `HistoryStore.flakiest_checks(repo:str, since_iso:str, limit:int=10, event:str|None=None) -> list[CheckFlakeStat]`.

- [ ] **Step 1: Write the failing test** (append to `test_history_check_insights.py`; reuses `_store`)

```python
def test_flakiest_checks_math(tmp_path):
    rows = [
        # 'flaky': commit A fails then passes (same sha) → flaky; commit B passes → clean
        ("o/n", "flaky", "pr", 1, "2026-07-01T00:00:00Z", "failure", "A", 1, 1),
        ("o/n", "flaky", "pr", 1, "2026-07-01T00:00:00Z", "success", "A", 2, 2),
        ("o/n", "flaky", "pr", 1, "2026-07-01T00:00:00Z", "success", "B", 1, 3),
        # 'solid': always green over two commits → 0%
        ("o/n", "solid", "pr", 1, "2026-07-01T00:00:00Z", "success", "C", 1, 4),
        ("o/n", "solid", "pr", 1, "2026-07-01T00:00:00Z", "success", "D", 1, 5),
        # 'broken': always fails → 0% (no pass, so no flake)
        ("o/n", "broken", "pr", 1, "2026-07-01T00:00:00Z", "failure", "E", 1, 6),
        # 'skiponly': skipped only → excluded entirely (no definite outcome)
        ("o/n", "skiponly", "pr", 1, "2026-07-01T00:00:00Z", "skipped", "F", 1, 7),
    ]
    s = _store(tmp_path, rows)
    by = {x.check_name: x for x in s.flakiest_checks("o/n", since_iso="2026-01-01T00:00:00Z")}
    assert by["flaky"].flaky_commits == 1 and by["flaky"].total_commits == 2
    assert by["flaky"].flake_pct == 50.0
    assert by["solid"].flake_pct == 0.0 and by["solid"].total_commits == 2
    assert by["broken"].flake_pct == 0.0 and by["broken"].total_commits == 1
    assert "skiponly" not in by                          # no definite outcome → dropped
    # ranked flakiest-first
    assert s.flakiest_checks("o/n", since_iso="2026-01-01T00:00:00Z")[0].check_name == "flaky"
```

- [ ] **Step 2: Run it to verify it fails**

Run: `pytest server/tests/test_history_check_insights.py::test_flakiest_checks_math -v`
Expected: FAIL — no `flakiest_checks`.

- [ ] **Step 3: Implement the dataclass + method**

In `server/pipeline/history.py`, add after `CheckDurationStat`:

```python
@dataclass(frozen=True)
class CheckFlakeStat:
    check_name: str
    event: str
    flake_pct: float
    flaky_commits: int
    total_commits: int
    n: int

    def to_dict(self) -> dict:
        return {"check_name": self.check_name, "event": self.event,
                "flake_pct": self.flake_pct, "flaky_commits": self.flaky_commits,
                "total_commits": self.total_commits, "n": self.n}


_FLAKE_PASS = {"success"}
_FLAKE_FAIL = {"failure", "cancelled", "timed_out", "error", "action_required"}
```

And add this method to `HistoryStore`:

```python
    def flakiest_checks(
        self, repo: str, since_iso: str, limit: int = 10, event: str | None = None,
    ) -> "list[CheckFlakeStat]":
        q = ("SELECT check_name, event, head_sha, conclusion FROM check_durations "
             "WHERE repo=? AND completed_at >= ?")
        args: list = [repo, since_iso]
        if event is not None:
            q += " AND event=?"
            args.append(event)
        # (check, event) -> head_sha -> {"pass": bool, "fail": bool}
        by_check: dict[tuple[str, str], dict[str, dict[str, bool]]] = {}
        totals: dict[tuple[str, str], int] = {}
        for r in self._conn.execute(q, args).fetchall():
            key = (r["check_name"], r["event"] or "")
            totals[key] = totals.get(key, 0) + 1
            c = (r["conclusion"] or "").lower()
            commits = by_check.setdefault(key, {})
            flags = commits.setdefault(r["head_sha"] or "", {"pass": False, "fail": False})
            if c in _FLAKE_PASS:
                flags["pass"] = True
            elif c in _FLAKE_FAIL:
                flags["fail"] = True
        stats: list[CheckFlakeStat] = []
        for key, commits in by_check.items():
            decided = [f for f in commits.values() if f["pass"] or f["fail"]]
            total = len(decided)
            if total == 0:
                continue
            flaky = sum(1 for f in decided if f["pass"] and f["fail"])
            stats.append(CheckFlakeStat(
                check_name=key[0], event=key[1], flake_pct=100.0 * flaky / total,
                flaky_commits=flaky, total_commits=total, n=totals[key]))
        stats.sort(key=lambda x: (x.flake_pct, x.flaky_commits), reverse=True)
        return stats[:limit]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest server/tests/test_history_check_insights.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add server/pipeline/history.py server/tests/test_history_check_insights.py
git commit -m "feat(insights): flakiest_checks history read method"
```

---

## Task 4: `orchestrator.check_insights` (window → cutoff → payload)

**Files:**
- Modify: `server/sessions/orchestrator.py`
- Test: `server/tests/test_check_insights_orch.py`

**Interfaces:**
- Produces: `SessionOrchestrator.check_insights(repo:str, window:str, now:datetime|None=None) -> dict`
  returning `{"repo","window","slowest":[...],"flakiest":[...]}` (JSON-safe; lists are `to_dict()`s).

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_check_insights_orch.py
from datetime import datetime, timezone
from server.sessions.orchestrator import SessionOrchestrator
from server.pipeline.history import CheckDurationStat, CheckFlakeStat

class _FakeStore:
    def __init__(self):
        self.calls = []
    def slowest_checks(self, repo, since_iso, limit=10, event=None):
        self.calls.append(("slow", repo, since_iso))
        return [CheckDurationStat("c", "pr", 100.0, 200.0, 2.0, 5)]
    def flakiest_checks(self, repo, since_iso, limit=10, event=None):
        self.calls.append(("flaky", repo, since_iso))
        return [CheckFlakeStat("c", "pr", 50.0, 1, 2, 3)]

def _orch_with_store(store):
    orch = SessionOrchestrator.__new__(SessionOrchestrator)   # skip heavy __init__
    class _Pop:
        def history_store(self_inner): return store
    orch._pr_population = _Pop()
    return orch

def test_check_insights_maps_window_to_cutoff_and_serializes():
    store = _FakeStore()
    orch = _orch_with_store(store)
    now = datetime(2026, 7, 8, 0, 0, 0, tzinfo=timezone.utc)
    out = orch.check_insights("o/n", "7d", now=now)
    assert out["repo"] == "o/n" and out["window"] == "7d"
    assert out["slowest"][0]["p50"] == 100.0
    assert out["flakiest"][0]["flake_pct"] == 50.0
    # 7d before 2026-07-08 → 2026-07-01
    assert store.calls[0][2] == "2026-07-01T00:00:00Z"

def test_check_insights_empty_when_no_store():
    orch = _orch_with_store(None)
    out = orch.check_insights("o/n", "7d")
    assert out == {"repo": "o/n", "window": "7d", "slowest": [], "flakiest": []}

def test_check_insights_unknown_window_clamps_to_7d():
    store = _FakeStore()
    out = _orch_with_store(store).check_insights("o/n", "bogus")
    assert out["window"] == "7d"
```

- [ ] **Step 2: Run it to verify it fails**

Run: `pytest server/tests/test_check_insights_orch.py -v`
Expected: FAIL — no `check_insights`.

- [ ] **Step 3: Implement the method** (add beside `all_merge_trains` in `SessionOrchestrator`)

```python
    def check_insights(self, repo: str, window: str, now=None) -> dict:
        """Per-repo Check Health: slowest + flakiest checks over a time window.
        Reads the read-only HistoryStore; returns a JSON-safe payload. Empty lists
        when history is unavailable or the repo has no rows (never raises)."""
        from datetime import datetime, timedelta, timezone
        _WINDOWS = {"24h": timedelta(hours=24), "7d": timedelta(days=7),
                    "14d": timedelta(days=14), "30d": timedelta(days=30)}
        win = window if window in _WINDOWS else "7d"
        store = self._pr_population.history_store()
        if store is None or not repo:
            return {"repo": repo, "window": win, "slowest": [], "flakiest": []}
        now = now or datetime.now(timezone.utc)
        since_iso = (now - _WINDOWS[win]).strftime("%Y-%m-%dT%H:%M:%SZ")
        try:
            slowest = store.slowest_checks(repo, since_iso)
            flakiest = store.flakiest_checks(repo, since_iso)
        except Exception:
            return {"repo": repo, "window": win, "slowest": [], "flakiest": []}
        return {"repo": repo, "window": win,
                "slowest": [s.to_dict() for s in slowest],
                "flakiest": [f.to_dict() for f in flakiest]}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest server/tests/test_check_insights_orch.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add server/sessions/orchestrator.py server/tests/test_check_insights_orch.py
git commit -m "feat(insights): orchestrator.check_insights window mapping"
```

---

## Task 5: API route + daemon proxy + RPC

**Files:**
- Modify: `server/api/pipeline.py`, `server/sessions/remote.py`, `server/ipc/orchestrator_rpc.py`
- Test: `server/tests/test_insights_route.py`

**Interfaces:**
- `GET /api/pipeline/insights/checks?repo=<r>&window=7d` → the `check_insights` payload.
- `RemoteOrchestrator.check_insights(repo, window) -> dict` (async proxy).
- RPC `check_insights` registered.

- [ ] **Step 1: Write the failing route test**

```python
# server/tests/test_insights_route.py
import os
import pytest
from fastapi.testclient import TestClient
from server.app import create_app
from server.sessions.orchestrator import SessionOrchestrator
from server.tests.fakes.fake_factory import fake_sdk_factory

_IDENT = {"Tailscale-User-Login": "insights@example.com"}

class _Fake:
    def check_insights(self, repo, window):
        return {"repo": repo, "window": window,
                "slowest": [{"check_name": "build", "event": "pr", "p50": 120.0,
                             "p90": 300.0, "ratio": 2.5, "n": 9}],
                "flakiest": [{"check_name": "e2e", "event": "pr", "flake_pct": 33.3,
                              "flaky_commits": 1, "total_commits": 3, "n": 4}]}

@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("CCSWITCH_DEV", "1")
    monkeypatch.setenv("CCSWITCH_TAILNET_ALLOWLIST", "")
    app = create_app(orchestrator=SessionOrchestrator(sdk_factory=fake_sdk_factory()))
    app.state.sessions = _Fake()
    return TestClient(app, raise_server_exceptions=False)

def test_insights_checks_returns_payload(client):
    r = client.get("/api/pipeline/insights/checks?repo=o/n&window=7d", headers=_IDENT)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["repo"] == "o/n" and body["window"] == "7d"
    assert body["slowest"][0]["check_name"] == "build"
    assert body["flakiest"][0]["flake_pct"] == 33.3

def test_insights_checks_requires_identity(client):
    assert client.get("/api/pipeline/insights/checks?repo=o/n").status_code == 401
```

> Match the exact auth/setup that `server/tests/test_merge_trains_route.py` uses (it was written for this same router); copy its fixture mechanism if it differs from the above.

- [ ] **Step 2: Run it to verify it fails**

Run: `pytest server/tests/test_insights_route.py -v`
Expected: FAIL — route 404 / missing.

- [ ] **Step 3: Add the endpoint** (in `server/api/pipeline.py`, on `repos_router`)

```python
import inspect  # if not already imported at top of the file

@repos_router.get("/insights/checks")
async def check_insights(
    request: Request,
    repo: str,
    window: str = "7d",
    _identity: str = Depends(current_identity),
) -> dict:
    orch = request.app.state.sessions
    result = orch.check_insights(repo, window)
    if inspect.isawaitable(result):
        result = await result
    return result
```

- [ ] **Step 4: Add the daemon proxy** (`server/sessions/remote.py`, beside `all_merge_trains`)

```python
    async def check_insights(self, repo: str, window: str) -> dict:
        """Resolve per-repo Check Health insights from the daemon (already JSON-safe)."""
        return await self._client.call("check_insights", repo=repo, window=window)
```

- [ ] **Step 5: Add + register the RPC** (`server/ipc/orchestrator_rpc.py`, beside `all_merge_trains`)

```python
    async def check_insights(params: dict) -> dict:
        result = orch.check_insights(params["repo"], params.get("window", "7d"))
        if inspect.isawaitable(result):
            result = await result
        return result
```
and add near the other `register_rpc(...)` calls:
```python
    register_rpc(app, "check_insights", check_insights)
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `pytest server/tests/test_insights_route.py server/tests/test_remote_orchestrator_breadth.py server/tests/test_orchestrator_rpc_breadth.py -v`
Expected: PASS (route tests + both breadth guards green).

- [ ] **Step 7: Commit**

```bash
git add server/api/pipeline.py server/sessions/remote.py server/ipc/orchestrator_rpc.py server/tests/test_insights_route.py
git commit -m "feat(insights): /api/pipeline/insights/checks endpoint + daemon proxy/RPC"
```

---

## Task 6: Frontend types + `useCheckInsights` hook

**Files:**
- Modify: `web/src/api/types.ts`, `web/src/api/hooks.ts`
- Test: `web/src/api/checkInsights.test.ts`

**Interfaces:**
- `CheckDurationStat`, `CheckFlakeStat`, `CheckInsights` TS interfaces.
- `QK.pipelineInsights(repo?, window?)`, `useCheckInsights(repo, window)`.

- [ ] **Step 1: Write the failing test**

```typescript
// web/src/api/checkInsights.test.ts
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { renderHook, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import * as client from './client'
import { useCheckInsights } from './hooks'

function wrapper({ children }: { children: React.ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>
}

describe('useCheckInsights', () => {
  beforeEach(() => vi.restoreAllMocks())
  it('fetches /pipeline/insights/checks with repo + window', async () => {
    const spy = vi.spyOn(client.api, 'get').mockResolvedValue({
      repo: 'o/n', window: '7d', slowest: [], flakiest: [],
    })
    const { result } = renderHook(() => useCheckInsights('o/n', '7d'), { wrapper })
    await waitFor(() => expect(result.current.data).toBeDefined())
    expect(spy).toHaveBeenCalledWith('/pipeline/insights/checks?repo=o%2Fn&window=7d')
  })
})
```

> Conform to the exact mocking style of `web/src/api/checkInsights`' siblings (`web/src/api/mergeTrains.test.ts` / `hooks.test.ts`) if they spy differently.

- [ ] **Step 2: Run it to verify it fails**

Run (in `web/`): `npx vitest run src/api/checkInsights.test.ts`
Expected: FAIL — `useCheckInsights` not exported.

- [ ] **Step 3: Add the types** (`web/src/api/types.ts`, after `MergeTrainState`)

```typescript
export interface CheckDurationStat {
  check_name: string
  event: string
  p50: number
  p90: number
  ratio: number | null
  n: number
}

export interface CheckFlakeStat {
  check_name: string
  event: string
  flake_pct: number
  flaky_commits: number
  total_commits: number
  n: number
}

export interface CheckInsights {
  repo: string
  window: string
  slowest: CheckDurationStat[]
  flakiest: CheckFlakeStat[]
}
```

- [ ] **Step 4: Add the query key + hook** (`web/src/api/hooks.ts`)

Add to `QK`:
```typescript
  pipelineInsights: (repo?: string, window?: string) =>
    ['pipeline-insights', repo, window] as const,
```
Add the hook (import `CheckInsights` from `./types`):
```typescript
export function useCheckInsights(repo: string | undefined, window: string) {
  return useQuery<CheckInsights>({
    queryKey: QK.pipelineInsights(repo, window),
    queryFn: () =>
      api.get<CheckInsights>(
        `/pipeline/insights/checks?repo=${encodeURIComponent(repo as string)}&window=${window}`),
    staleTime: 15_000,
    refetchInterval: 30_000,
    enabled: !!repo,
  })
}
```

- [ ] **Step 5: Verify + commit**

Run (in `web/`): `npx vitest run src/api/checkInsights.test.ts && npx tsc --noEmit`
Expected: PASS, no type errors.
```bash
git add web/src/api/types.ts web/src/api/hooks.ts web/src/api/checkInsights.test.ts
git commit -m "feat(insights): useCheckInsights hook + types"
```

---

## Task 7: `CheckTable` presentational component

**Files:**
- Create: `web/src/components/CheckTable.tsx`
- Test: `web/src/components/CheckTable.test.tsx`

**Interfaces:**
- `export interface Column<T> { header: string; cell: (row: T) => React.ReactNode; className?: string }`
- `export function CheckTable<T>({ title, columns, rows, empty }: { title: string; columns: Column<T>[]; rows: T[]; empty: string }): JSX.Element`

- [ ] **Step 1: Write the failing test**

```tsx
// web/src/components/CheckTable.test.tsx
import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { CheckTable } from './CheckTable'

describe('CheckTable', () => {
  it('renders title, headers and rows', () => {
    render(<CheckTable title="Slowest" empty="none"
      columns={[{ header: 'Check', cell: (r: { name: string }) => r.name }]}
      rows={[{ name: 'build' }, { name: 'e2e' }]} />)
    expect(screen.getByText('Slowest')).toBeInTheDocument()
    expect(screen.getByText('Check')).toBeInTheDocument()
    expect(screen.getByText('build')).toBeInTheDocument()
  })
  it('shows the empty message when there are no rows', () => {
    render(<CheckTable title="Flakiest" empty="No flaky checks"
      columns={[{ header: 'Check', cell: (r: { name: string }) => r.name }]} rows={[]} />)
    expect(screen.getByText('No flaky checks')).toBeInTheDocument()
  })
})
```

- [ ] **Step 2: Run it to verify it fails**

Run (in `web/`): `npx vitest run src/components/CheckTable.test.tsx`
Expected: FAIL — module missing.

- [ ] **Step 3: Implement** (match Tailwind tokens used by `StageTiles.tsx`: `border-gray-200 dark:border-gray-700`, `text-muted-foreground` equivalents already in the codebase)

```tsx
// web/src/components/CheckTable.tsx
import type { ReactNode } from 'react'

export interface Column<T> {
  header: string
  cell: (row: T) => ReactNode
  className?: string
}

export function CheckTable<T>({ title, columns, rows, empty }: {
  title: string
  columns: Column<T>[]
  rows: T[]
  empty: string
}) {
  return (
    <section className="rounded border border-gray-200 dark:border-gray-700">
      <h3 className="px-3 py-2 text-sm font-semibold border-b border-gray-200 dark:border-gray-700">
        {title}
      </h3>
      {rows.length === 0 ? (
        <p className="px-3 py-3 text-xs text-gray-500">{empty}</p>
      ) : (
        <table className="w-full text-xs">
          <thead>
            <tr className="text-left text-gray-500">
              {columns.map((c) => (
                <th key={c.header} className={`px-3 py-1 font-medium ${c.className ?? ''}`}>
                  {c.header}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((row, i) => (
              <tr key={i} className="border-t border-gray-100 dark:border-gray-800">
                {columns.map((c) => (
                  <td key={c.header} className={`px-3 py-1 ${c.className ?? ''}`}>
                    {c.cell(row)}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  )
}
```

- [ ] **Step 4: Verify + commit**

Run (in `web/`): `npx vitest run src/components/CheckTable.test.tsx && npx tsc --noEmit && npx eslint src/components/CheckTable.tsx`
Expected: PASS, clean.
```bash
git add web/src/components/CheckTable.tsx web/src/components/CheckTable.test.tsx
git commit -m "feat(insights): CheckTable presentational component"
```

---

## Task 8: `InsightsView` (repo dropdown + window selector + two panels)

**Files:**
- Create: `web/src/components/InsightsView.tsx`
- Test: `web/src/components/InsightsView.test.tsx`

**Interfaces:**
- Consumes: `useStageModels()` (existing — its keys are the watched repos), `useCheckInsights` (Task 6), `CheckTable`/`Column` (Task 7), types (Task 6).
- `export function InsightsView(): JSX.Element`

- [ ] **Step 1: Write the failing test**

```tsx
// web/src/components/InsightsView.test.tsx
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import type { UseQueryResult } from '@tanstack/react-query'
import * as hooks from '../api/hooks'
import { InsightsView } from './InsightsView'

function stub<T>(data: T): UseQueryResult<T> {
  return { data, dataUpdatedAt: 0 } as unknown as UseQueryResult<T>
}

describe('InsightsView', () => {
  beforeEach(() => vi.restoreAllMocks())
  it('renders both panels for the selected repo', () => {
    vi.spyOn(hooks, 'useStageModels').mockReturnValue(stub({ 'o/n': {} as never }))
    vi.spyOn(hooks, 'useCheckInsights').mockReturnValue(stub({
      repo: 'o/n', window: '7d',
      slowest: [{ check_name: 'build', event: 'pr', p50: 120, p90: 300, ratio: 2.5, n: 9 }],
      flakiest: [{ check_name: 'e2e', event: 'pr', flake_pct: 33.3, flaky_commits: 1, total_commits: 3, n: 4 }],
    }))
    render(<InsightsView />)
    expect(screen.getByText(/Slowest/i)).toBeInTheDocument()
    expect(screen.getByText(/Flakiest/i)).toBeInTheDocument()
    expect(screen.getByText('build')).toBeInTheDocument()
    expect(screen.getByText('e2e')).toBeInTheDocument()
  })
})
```

- [ ] **Step 2: Run it to verify it fails**

Run (in `web/`): `npx vitest run src/components/InsightsView.test.tsx`
Expected: FAIL — module missing.

- [ ] **Step 3: Implement**

```tsx
// web/src/components/InsightsView.tsx
import { useMemo, useState } from 'react'
import { useStageModels, useCheckInsights } from '../api/hooks'
import { CheckTable, type Column } from './CheckTable'
import type { CheckDurationStat, CheckFlakeStat } from '../api/types'

const WINDOWS = ['24h', '7d', '14d', '30d']

function fmtSecs(s: number): string {
  if (s < 60) return `${Math.round(s)}s`
  const m = Math.floor(s / 60)
  return m < 60 ? `${m}m ${Math.round(s % 60)}s` : `${Math.floor(m / 60)}h ${m % 60}m`
}

export function InsightsView() {
  const { data: models } = useStageModels()
  const repos = useMemo(() => Object.keys(models ?? {}).sort(), [models])
  const [repo, setRepo] = useState<string | undefined>(undefined)
  const [window, setWindow] = useState('7d')
  const selected = repo ?? repos[0]
  const { data } = useCheckInsights(selected, window)

  const slowCols: Column<CheckDurationStat>[] = [
    { header: 'Check', cell: (r) => r.check_name },
    { header: 'Event', cell: (r) => r.event },
    { header: 'p50', cell: (r) => fmtSecs(r.p50) },
    { header: 'p90', cell: (r) => fmtSecs(r.p90) },
    { header: 'Variability', cell: (r) => (r.ratio == null ? '—' : `${r.ratio.toFixed(1)}×`) },
    { header: 'n', cell: (r) => r.n },
  ]
  const flakeCols: Column<CheckFlakeStat>[] = [
    { header: 'Check', cell: (r) => r.check_name },
    { header: 'Event', cell: (r) => r.event },
    { header: 'Flake', cell: (r) => `${r.flake_pct.toFixed(1)}%` },
    { header: 'Flaky / total', cell: (r) => `${r.flaky_commits} / ${r.total_commits}` },
    { header: 'n', cell: (r) => r.n },
  ]

  return (
    <div className="space-y-3 p-2">
      <div className="flex items-center gap-3 text-sm">
        <label>
          Repo{' '}
          <select className="rounded border border-gray-300 dark:border-gray-700 bg-transparent px-1 py-0.5"
                  value={selected ?? ''} onChange={(e) => setRepo(e.target.value)}>
            {repos.map((r) => <option key={r} value={r}>{r}</option>)}
          </select>
        </label>
        <label>
          Window{' '}
          <select className="rounded border border-gray-300 dark:border-gray-700 bg-transparent px-1 py-0.5"
                  value={window} onChange={(e) => setWindow(e.target.value)}>
            {WINDOWS.map((w) => <option key={w} value={w}>{w}</option>)}
          </select>
        </label>
      </div>
      {!selected ? (
        <p className="text-xs text-gray-500">No repositories with pipeline history yet.</p>
      ) : (
        <>
          <CheckTable title="Slowest / most-variable checks" columns={slowCols}
            rows={data?.slowest ?? []} empty={`No check timing for ${selected} in this window.`} />
          <CheckTable title="Flakiest checks" columns={flakeCols}
            rows={data?.flakiest ?? []} empty={`No flaky checks for ${selected} in this window.`} />
        </>
      )}
    </div>
  )
}
```

- [ ] **Step 4: Verify + commit**

Run (in `web/`): `npx vitest run src/components/InsightsView.test.tsx && npx tsc --noEmit && npx eslint src/components/InsightsView.tsx`
Expected: PASS, clean.
```bash
git add web/src/components/InsightsView.tsx web/src/components/InsightsView.test.tsx
git commit -m "feat(insights): InsightsView with repo + window selectors"
```

---

## Task 9: Wire the Insights tab into nav

**Files:**
- Modify: `web/src/components/Dashboard.tsx`, `web/src/App.tsx`
- Test: `web/src/components/InsightsTab.test.tsx`

**Interfaces:**
- Consumes: `InsightsView` (Task 8).

- [ ] **Step 1: Write the failing test**

```tsx
// web/src/components/InsightsTab.test.tsx
import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { UseQueryResult } from '@tanstack/react-query'
import * as hooks from '../api/hooks'
import { Dashboard } from './Dashboard'

function stub<T>(d: T): UseQueryResult<T> {
  return { data: d, dataUpdatedAt: 0 } as unknown as UseQueryResult<T>
}

describe('Insights tab', () => {
  it('renders InsightsView when the active tab is insights', () => {
    vi.spyOn(hooks, 'useStageModels').mockReturnValue(stub({ 'o/n': {} as never }))
    vi.spyOn(hooks, 'useCheckInsights').mockReturnValue(stub({
      repo: 'o/n', window: '7d', slowest: [], flakiest: [],
    }))
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <MemoryRouter>
        <QueryClientProvider client={qc}><Dashboard activeTab="insights" /></QueryClientProvider>
      </MemoryRouter>
    )
    expect(screen.getByText(/Slowest \/ most-variable/i)).toBeInTheDocument()
  })
})
```

> `Dashboard`'s real props may differ (it may take more than `activeTab`). Read the top of `Dashboard.tsx` and pass whatever the component actually requires (mock the other hooks it calls, mirroring existing `Dashboard`/`App` tests). The assertion — that choosing the `insights` tab renders `InsightsView` — is the point.

- [ ] **Step 2: Run it to verify it fails**

Run (in `web/`): `npx vitest run src/components/InsightsTab.test.tsx`
Expected: FAIL — `'insights'` not an accepted tab / not rendered.

- [ ] **Step 3: Extend `TabId` + render the panel** (`web/src/components/Dashboard.tsx`)

- Add `'insights'` to the `TabId` union (line ~415):
```typescript
export type TabId = 'sessions' | 'accounts' | 'archived' | 'pipeline' | 'automation' | 'insights'
```
- Add a ref beside the others and to `tabRefs`:
```typescript
const insightsTabRef = useRef<HTMLButtonElement>(null)
// ...in tabRefs:
  insights: insightsTabRef,
```
- Add the nav button next to the Automation button (mirror its markup, label "Insights", `ref={insightsTabRef}`, sets active tab to `'insights'`).
- Add the panel next to the Automation panel:
```tsx
{activeTab === 'insights' && (
  <div className="flex-1 min-h-0 overflow-y-auto pb-4">
    <InsightsView />
  </div>
)}
```
- Import `InsightsView` at the top: `import { InsightsView } from './InsightsView'`.

- [ ] **Step 4: Add the route** (`web/src/App.tsx`, beside `/automation`)

```tsx
<Route path="/insights" element={<DashboardRoute tab="insights" />} />
```

- [ ] **Step 5: Verify + commit**

Run (in `web/`):
```
npx vitest run src/components/InsightsTab.test.tsx && npx vitest run src/components/ && npx tsc --noEmit && npx eslint src/components/Dashboard.tsx src/App.tsx
```
Expected: new test passes, full component suite stays green, tsc + eslint clean.
```bash
git add web/src/components/Dashboard.tsx web/src/App.tsx web/src/components/InsightsTab.test.tsx
git commit -m "feat(insights): wire Insights tab into nav + routing"
```

---

## Final verification (before PR)

- [ ] Server: `ruff check server/` · `mypy server/ --ignore-missing-imports` (**must be clean**) · `pytest server/tests/ -q` (feature tests green; note any pre-existing/env-only failures explicitly).
- [ ] Web (in `web/`): `npx vitest run` · `npx tsc --noEmit` · `npx eslint .` — all green.
- [ ] Optional smoke: validate the endpoint path in-process against the real `pipeline_history.db` (mirror the M-Track-10 live validation) to confirm real data renders.
- [ ] Open the PR per CLAUDE.md (branch off `origin/master`, non-draft, `gh pr merge --squash --auto`); ensure the required `lint-and-test` (ruff+mypy+pytest) and `frontend` checks are green.

---

## Self-Review Notes (author)

- **Spec coverage:** panels → Tasks 2–3 (store), 7–8 (UI); window model → Task 4 + Task 8 selector; API → Task 5; daemon breadth → Task 5; nav → Task 9; empty/absent history → Tasks 4/8; flake definition + conclusion normalization → Task 3 (verbatim from Global Constraints). Verification spike → Task 1.
- **Reality adjustment (noted):** the spec said "reuse the Pipeline repo selector"; there is none (PipelineView is cross-repo grouped, no `?pipeline=` param), so `InsightsView` owns a repo `<select>` populated from `useStageModels()` keys. Same per-repo behaviour.
- **Type consistency:** `CheckDurationStat` (6 fields) and `CheckFlakeStat` (6 fields) identical across store `to_dict` (Tasks 2–3), TS types (Task 6), and the tables (Task 8). `check_insights` payload keys (`repo/window/slowest/flakiest`) identical across orchestrator (Task 4), RPC (Task 5), endpoint (Task 5), hook (Task 6).
- **Verify-before-build flags:** real `conclusion` vocabulary (Task 1); the `test_merge_trains_route.py` auth mechanism (Task 5); `Dashboard`'s actual props (Task 9); the sibling hook-test mocking style (Task 6).

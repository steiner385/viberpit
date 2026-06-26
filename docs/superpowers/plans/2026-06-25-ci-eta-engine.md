# CI ETA Engine Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Compute and display a per-PR CI ETA on the Pipeline board by faithfully porting Plotroom's `compute_progress` estimator + history read-helpers over a migrated history snapshot, gated by live-parity against the running Plotroom oracle.

**Architecture:** A separate read-mostly `pipeline_history.db` is populated from Plotroom's real `history.db`. Pure Python ports of `math.ts`/`history.ts`/`progress.ts`/`classify.ts`'s required-checks helpers compute `(percent, eta_seconds, overdue)` from that history plus each open PR's live check timing (fetched via a new aliased GraphQL query). The result rides the existing `PullRequestInfo` → `all_prs()` → `/api/prs` → Pipeline board path (from #302), and is verified by diffing the live oracle's `etaSeconds`.

**Tech Stack:** Python 3 / asyncio / sqlite3 (stdlib, NOT SQLAlchemy — separate DB) · GitHub GraphQL via `gh` · React/TS (board) · pytest · vitest.

## Global Constraints

- **Faithful port — no improvisation.** Constants and logic come verbatim from Plotroom source at `/mnt/ssk-ssd/tony/GitHub/pr-dashboard/server/` (`math.ts`, `history.ts`, `estimator/progress.ts`, `estimator/classify.ts`). Copy values exactly: `RUNNING_CAP=0.97`, `OVERDUE_FACTOR=1.5`, `DEFAULT_DURATION=60`, `DURATION_FRESH_DAYS=14`, `DURATION_FRESH_MIN=5`, `DURATION_FALLBACK_LIMIT=10`, `EXPECTED_SET_STALE_MS=600000`.
- **Separate history DB (ADR-1).** `pipeline_history.db` lives beside the main DB at `~/.claude-accounts/pipeline_history.db`, accessed via stdlib `sqlite3`. The main app DB (`ccswitch.db`) and `create_all`/`init_db` never touch it.
- **Read source `?mode=ro`.** Plotroom's `history.db` (`/mnt/ssk-ssd/tony/GitHub/pr-dashboard/data/history.db`) is WAL and live-written — open `file:…?mode=ro&immutable=0` via `uri=True`; never write to it.
- **`PullRequestInfo` is RPC-shape-stable.** New fields additive with defaults; update the single `to_dict`/`from_dict` pair AND the frontend type together; tolerant of old payloads (no migration). **Adding a field breaks `test_sessions_pr_field.py`'s exact-dict assertion — update it in the same task** (see memory: pullrequestinfo-todict-exact-shape-test).
- **Event is `'pull_request'`** for the CI-progress path (matches Plotroom's `ciProgress` call). Queue-stage / `merge_group` ETAs are out of scope.
- **Estimator surfaces only `percent`, `eta_seconds`, `overdue`.** `failed` and `etaRangeSeconds` are not consumed.
- **Backend tests:** `PYTHONPATH=/mnt/ssk-ssd/tony/GitHub/.ccswitch/worktrees/cc-account-switcher/41b21f76/src /mnt/ssk-ssd/tony/GitHub/.ccswitch/clones/steiner385/cc-account-switcher/.venv/bin/python -m pytest <file> -v`. Lint whole tree before pushing: `ruff check src/ server/` + `mypy`. Run the FULL backend suite the CI way: `env -u CCSWITCH_TAILNET_ALLOWLIST -u CCSWITCH_ENABLE_XVFB CCSWITCH_DEV=1 … pytest server/tests/` (8 failures are documented env artifacts — auth/health live-socket + x11).
- **Frontend tests (in `web/`):** `npx vitest run`, `npx tsc -b --force` (NOT `tsc --noEmit`), `npx eslint . --max-warnings 0`. `web/node_modules` is installed.
- **Branch base:** this branch (`feat/ci-eta-engine`) is stacked on `feat/unified-pipeline-view` (#302). The board-display task (Task 9) needs #302's `PipelineView`. If #302 merges first, rebase onto `origin/master`.

---

### Task 1: History migration into `pipeline_history.db`

**Files:**
- Create: `server/pipeline/history_migrate.py`
- Test: `server/tests/test_pipeline_history_migrate.py`

**Interfaces:**
- Produces:
  - `SOURCE_DEFAULT = "/mnt/ssk-ssd/tony/GitHub/pr-dashboard/data/history.db"`
  - `migrate_history(source_path: str, dest_path: str) -> dict[str, int]` — copies `check_durations` and `runner_waits` schema-faithfully from source (read-only) into `dest` (created if absent), replacing existing rows; returns `{"check_durations": n, "runner_waits": m}` row counts written.

- [ ] **Step 1: Write the failing test**

```python
import sqlite3
from server.pipeline.history_migrate import migrate_history


def _make_source(path):
    c = sqlite3.connect(path)
    c.executescript(
        """
        CREATE TABLE check_durations (repo TEXT, check_name TEXT, event TEXT,
          duration_secs REAL, completed_at TEXT, conclusion TEXT, head_sha TEXT,
          run_attempt INTEGER, started_at TEXT, run_number INTEGER);
        CREATE TABLE runner_waits (repo TEXT, check_name TEXT, event TEXT,
          wait_secs REAL, started_at TEXT, pool TEXT);
        INSERT INTO check_durations VALUES
          ('o/n','ci','pull_request',120.0,'2026-06-20T00:00:00Z','SUCCESS','sha',1,'2026-06-20T00:00:00Z',1);
        INSERT INTO runner_waits VALUES ('o/n','ci','pull_request',5.0,'2026-06-20T00:00:00Z','ubuntu');
        """
    )
    c.commit(); c.close()


def test_migrate_copies_rows_and_counts(tmp_path):
    src = tmp_path / "src.db"; dest = tmp_path / "pipeline_history.db"
    _make_source(str(src))
    counts = migrate_history(str(src), str(dest))
    assert counts == {"check_durations": 1, "runner_waits": 1}
    d = sqlite3.connect(str(dest))
    assert d.execute("SELECT count(*) FROM check_durations").fetchone()[0] == 1
    assert d.execute("SELECT check_name FROM runner_waits").fetchone()[0] == "ci"


def test_migrate_is_idempotent(tmp_path):
    src = tmp_path / "src.db"; dest = tmp_path / "d.db"
    _make_source(str(src))
    migrate_history(str(src), str(dest))
    counts = migrate_history(str(src), str(dest))  # second run replaces, not appends
    assert counts == {"check_durations": 1, "runner_waits": 1}
    d = sqlite3.connect(str(dest))
    assert d.execute("SELECT count(*) FROM check_durations").fetchone()[0] == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=$PWD/src /mnt/ssk-ssd/tony/GitHub/.ccswitch/clones/steiner385/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_pipeline_history_migrate.py -v`
Expected: FAIL — `ModuleNotFoundError`.

- [ ] **Step 3: Implement the migration**

Create `server/pipeline/history_migrate.py`:

```python
"""Migrate Plotroom's history.db (read-only) into a ccswitch-owned pipeline_history.db.

ADR-1: a SEPARATE sqlite file from the main app DB; stdlib sqlite3, not SQLAlchemy.
Only the two tables the CI-ETA estimator consumes are copied. The source is WAL and
live-written — opened immutable/read-only, never written.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

SOURCE_DEFAULT = "/mnt/ssk-ssd/tony/GitHub/pr-dashboard/data/history.db"

# Column order preserved verbatim from the source schema (history.ts).
_SCHEMA = {
    "check_durations": (
        "repo TEXT, check_name TEXT, event TEXT, duration_secs REAL, completed_at TEXT, "
        "conclusion TEXT, head_sha TEXT, run_attempt INTEGER, started_at TEXT, run_number INTEGER"
    ),
    "runner_waits": (
        "repo TEXT, check_name TEXT, event TEXT, wait_secs REAL, started_at TEXT, pool TEXT"
    ),
}


def default_dest() -> str:
    return str(Path.home() / ".claude-accounts" / "pipeline_history.db")


def migrate_history(source_path: str = SOURCE_DEFAULT, dest_path: str | None = None) -> dict[str, int]:
    dest_path = dest_path or default_dest()
    src = sqlite3.connect(f"file:{source_path}?mode=ro", uri=True)
    dest = sqlite3.connect(dest_path)
    counts: dict[str, int] = {}
    try:
        for table, schema in _SCHEMA.items():
            cols = [c.split()[0] for c in schema.split(",")]
            dest.execute(f"DROP TABLE IF EXISTS {table}")
            dest.execute(f"CREATE TABLE {table} ({schema})")
            rows = src.execute(f"SELECT {', '.join(cols)} FROM {table}").fetchall()
            placeholders = ",".join("?" for _ in cols)
            dest.executemany(f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({placeholders})", rows)
            counts[table] = len(rows)
        # Indexes mirroring history.ts (speed the read helpers).
        dest.execute("CREATE INDEX IF NOT EXISTS idx_check_durations "
                     "ON check_durations(repo, check_name, event, completed_at)")
        dest.execute("CREATE INDEX IF NOT EXISTS idx_runner_waits "
                     "ON runner_waits(repo, check_name, event, started_at)")
        dest.commit()
    finally:
        src.close(); dest.close()
    return counts
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH=$PWD/src .../.venv/bin/python -m pytest server/tests/test_pipeline_history_migrate.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Verify against the REAL source (one-off sanity, not committed as a test)**

Run:
```bash
PYTHONPATH=$PWD/src .../.venv/bin/python -c "
from server.pipeline.history_migrate import migrate_history
print(migrate_history(dest_path='/tmp/ph.db'))
"
```
Expected: prints counts close to `{'check_durations': ~52000, 'runner_waits': ~54000}` (proves the real schema matches).

- [ ] **Step 6: Commit**

```bash
git add server/pipeline/history_migrate.py server/tests/test_pipeline_history_migrate.py
git commit -m "feat(eta): migrate Plotroom history.db into pipeline_history.db"
```

---

### Task 2: math + history read-helpers

**Files:**
- Create: `server/pipeline/history.py`
- Test: `server/tests/test_pipeline_history_read.py`

**Interfaces:**
- Consumes: `pipeline_history.db` schema (Task 1).
- Produces:
  - `percentile(sorted_xs: list[float], q: float) -> float` and `median(xs: list[float]) -> float` (match `math.ts`).
  - `class Expected: p10: float; p50: float; p90: float; n: int`
  - `class HistoryStore(db_path: str)` with: `recent_durations(repo,name,event)->list[float]`, `expected(repo,name,event)->Expected|None`, `samples(repo,name,event)->list[float]`, `expected_set(repo,event,now,window_days=14)->list[str]`, `expected_runner_wait(repo,name,event)->float|None`.

- [ ] **Step 1: Write the failing test**

```python
from datetime import datetime, timezone
import sqlite3
from server.pipeline.history import HistoryStore, median, percentile, Expected


def test_math_matches_plotroom():
    # percentile: sorted[min(len-1, ceil(p*len)-1)]
    assert percentile([1, 2, 3, 4], 0.5) == 2          # ceil(0.5*4)-1 = 1 → 2
    assert percentile([1, 2, 3, 4], 0.9) == 4
    # median: lower median, s[floor((len-1)/2)]
    assert median([3, 1, 2]) == 2
    assert median([4, 1, 3, 2]) == 2                   # floor(3/2)=1 → sorted[1]=2


def _store(tmp_path):
    p = tmp_path / "ph.db"
    c = sqlite3.connect(str(p))
    c.executescript(
        """
        CREATE TABLE check_durations (repo TEXT, check_name TEXT, event TEXT,
          duration_secs REAL, completed_at TEXT, conclusion TEXT, head_sha TEXT,
          run_attempt INTEGER, started_at TEXT, run_number INTEGER);
        CREATE TABLE runner_waits (repo TEXT, check_name TEXT, event TEXT,
          wait_secs REAL, started_at TEXT, pool TEXT);
        """
    )
    # 6 fresh SUCCESS samples for (o/n, ci, pull_request)
    for i, dur in enumerate([100, 110, 120, 130, 140, 150]):
        c.execute("INSERT INTO check_durations VALUES (?,?,?,?,?,?,?,?,?,?)",
                  ("o/n", "ci", "pull_request", dur, f"2026-06-2{i}T00:00:00Z",
                   "SUCCESS", "sha", 1, f"2026-06-2{i}T00:00:00Z", i))
    c.execute("INSERT INTO check_durations VALUES (?,?,?,?,?,?,?,?,?,?)",
              ("o/n", "ci", "pull_request", 999, "2026-06-20T00:00:00Z", "FAILURE",
               "sha", 1, "2026-06-20T00:00:00Z", 9))  # non-SUCCESS ignored
    for w in [4, 6, 8]:
        c.execute("INSERT INTO runner_waits VALUES (?,?,?,?,?,?)",
                  ("o/n", "ci", "pull_request", w, "2026-06-20T00:00:00Z", "ubuntu"))
    c.commit(); c.close()
    return HistoryStore(str(p))


def test_expected_and_samples_success_only(tmp_path):
    s = _store(tmp_path)
    exp = s.expected("o/n", "ci", "pull_request")
    assert exp is not None and exp.n == 6 and exp.p50 == 130   # see percentile rule
    assert 999 not in s.samples("o/n", "ci", "pull_request")   # FAILURE excluded
    assert s.expected("o/n", "missing", "pull_request") is None


def test_expected_runner_wait_is_median(tmp_path):
    s = _store(tmp_path)
    assert s.expected_runner_wait("o/n", "ci", "pull_request") == 6
    assert s.expected_runner_wait("o/n", "none", "pull_request") is None


def test_expected_set_window(tmp_path):
    s = _store(tmp_path)
    now = datetime(2026, 6, 26, tzinfo=timezone.utc)
    names = s.expected_set("o/n", "pull_request", now, window_days=14)
    assert "ci" in names
```

(Note: `percentile([100,110,120,130,140,150],0.5)` = sorted[min(5, ceil(3)-1)] = sorted[2] = 120 — adjust the asserted `p50` to **120** if 130 fails; compute it from the rule, do not guess.)

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=$PWD/src .../.venv/bin/python -m pytest server/tests/test_pipeline_history_read.py -v`
Expected: FAIL — `ModuleNotFoundError`.

- [ ] **Step 3: Implement**

Create `server/pipeline/history.py`:

```python
"""Read-only helpers over pipeline_history.db, mirroring Plotroom's history.ts.

Pure SQL + the same recency/percentile semantics. No writes (migration owns that).
"""
from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta

# Verbatim from history.ts.
DURATION_FRESH_DAYS = 14
DURATION_FRESH_MIN = 5
DURATION_FALLBACK_LIMIT = 10


def percentile(sorted_xs: list[float], q: float) -> float:
    """p-th percentile of an already-sorted list (math.ts: sorted[min(len-1, ceil(p*len)-1)])."""
    n = len(sorted_xs)
    return sorted_xs[min(n - 1, math.ceil(q * n) - 1)]


def median(xs: list[float]) -> float:
    """Lower median (math.ts: s[floor((len-1)/2)] over a sorted copy)."""
    s = sorted(xs)
    return s[(len(s) - 1) // 2]


@dataclass(frozen=True)
class Expected:
    p10: float
    p50: float
    p90: float
    n: int


def _parse_iso_ms(iso: str) -> float:
    """ms since epoch from an ISO-8601 string (tolerant of trailing Z)."""
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp() * 1000.0


class HistoryStore:
    def __init__(self, db_path: str) -> None:
        self._conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        self._conn.row_factory = sqlite3.Row

    def recent_durations(self, repo: str, name: str, event: str) -> list[float]:
        rows = self._conn.execute(
            "SELECT duration_secs, completed_at FROM check_durations "
            "WHERE repo=? AND check_name=? AND event=? AND conclusion='SUCCESS' "
            "ORDER BY completed_at DESC LIMIT 20",
            (repo, name, event),
        ).fetchall()
        if not rows:
            return []
        cutoff = _parse_iso_ms(rows[0]["completed_at"]) - DURATION_FRESH_DAYS * 86400_000
        fresh = [r for r in rows if _parse_iso_ms(r["completed_at"]) >= cutoff]
        chosen = fresh if len(fresh) >= DURATION_FRESH_MIN else rows[:DURATION_FALLBACK_LIMIT]
        return [float(r["duration_secs"]) for r in chosen]

    def expected(self, repo: str, name: str, event: str) -> Expected | None:
        vals = self.recent_durations(repo, name, event)
        if not vals:
            return None
        s = sorted(vals)
        return Expected(percentile(s, 0.1), percentile(s, 0.5), percentile(s, 0.9), len(s))

    def samples(self, repo: str, name: str, event: str) -> list[float]:
        return self.recent_durations(repo, name, event)

    def expected_set(self, repo: str, event: str, now: datetime, window_days: int = 14) -> list[str]:
        cutoff = (now - timedelta(days=window_days)).isoformat()
        rows = self._conn.execute(
            "SELECT DISTINCT check_name FROM check_durations "
            "WHERE repo=? AND event=? AND conclusion='SUCCESS' AND completed_at >= ?",
            (repo, event, cutoff),
        ).fetchall()
        return [r["check_name"] for r in rows]

    def expected_runner_wait(self, repo: str, name: str, event: str) -> float | None:
        rows = self._conn.execute(
            "SELECT wait_secs FROM runner_waits WHERE repo=? AND check_name=? AND event=? "
            "ORDER BY started_at DESC LIMIT 20",
            (repo, name, event),
        ).fetchall()
        vals = [float(r["wait_secs"]) for r in rows]
        return median(vals) if vals else None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH=$PWD/src .../.venv/bin/python -m pytest server/tests/test_pipeline_history_read.py -v`
Expected: PASS (compute the exact `p50` from the percentile rule and fix the assertion if needed).

- [ ] **Step 5: Commit**

```bash
git add server/pipeline/history.py server/tests/test_pipeline_history_read.py
git commit -m "feat(eta): history read-helpers (expected/samples/expected_set/runner_wait)"
```

---

### Task 3: `compute_progress` estimator port

**Files:**
- Create: `server/pipeline/estimator.py`
- Test: `server/tests/test_pipeline_estimator.py`

**Interfaces:**
- Consumes: `median`, `percentile` from `server.pipeline.history` (Task 2).
- Produces:
  - `class Check(TypedDict)`-like dict shape: `{name, status, conclusion, started_at}` (`status` ∈ `COMPLETED|IN_PROGRESS|QUEUED|…`; `started_at` ISO or None).
  - `class ProgressResult: percent: int; eta_seconds: int | None; overdue: bool`
  - `compute_progress(checks: list[dict], expected_set: list[str], lookup: Callable[[str], Expected|None], now: datetime, samples: Callable[[str], list[float]] | None = None, queue_delay: Callable[[str], float|None] | None = None) -> ProgressResult`

- [ ] **Step 1: Write the failing test**

Port the core cases from Plotroom's `server/estimator/__tests__/progress.test.ts`. Read that file first and translate these scenarios verbatim:

```python
from datetime import datetime, timezone
from server.pipeline.estimator import compute_progress, ProgressResult
from server.pipeline.history import Expected

NOW = datetime(2026, 6, 25, 12, 0, 0, tzinfo=timezone.utc)


def _lookup(table):
    return lambda n: table.get(n)


def test_all_complete_is_100_eta_0():
    checks = [{"name": "ci", "status": "COMPLETED", "conclusion": "SUCCESS", "started_at": None}]
    r = compute_progress(checks, ["ci"], _lookup({"ci": Expected(50, 100, 150, 8)}), NOW)
    assert r == ProgressResult(percent=100, eta_seconds=0, overdue=False)


def test_queued_expected_uses_p50_plus_queue_delay():
    # No live checks; one expected name → queued → rem = p50 + queueDelay
    r = compute_progress([], ["ci"], _lookup({"ci": Expected(50, 100, 150, 8)}), NOW,
                         queue_delay=lambda n: 20.0)
    assert r.percent == 0 and r.eta_seconds == 120 and r.overdue is False


def test_running_under_p50_eta_is_remaining():
    started = datetime(2026, 6, 25, 11, 59, 0, tzinfo=timezone.utc).isoformat()  # 60s elapsed
    checks = [{"name": "ci", "status": "IN_PROGRESS", "conclusion": None, "started_at": started}]
    r = compute_progress(checks, ["ci"], _lookup({"ci": Expected(50, 100, 150, 8)}), NOW,
                         samples=lambda n: [])  # <5 samples → p50/p90 path
    assert r.eta_seconds == 40 and r.overdue is False        # 100 - 60

def test_overdue_with_10plus_samples_all_below_elapsed():
    started = datetime(2026, 6, 25, 11, 0, 0, tzinfo=timezone.utc).isoformat()  # 3600s elapsed
    checks = [{"name": "ci", "status": "IN_PROGRESS", "conclusion": None, "started_at": started}]
    r = compute_progress(checks, ["ci"], _lookup({"ci": Expected(50, 100, 150, 12)}), NOW,
                         samples=lambda n: [90, 95, 100, 105, 110, 92, 97, 102, 107, 112])
    assert r.overdue is True and r.eta_seconds is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=$PWD/src .../.venv/bin/python -m pytest server/tests/test_pipeline_estimator.py -v`
Expected: FAIL — `ModuleNotFoundError`.

- [ ] **Step 3: Implement the port**

Create `server/pipeline/estimator.py` — a line-faithful translation of `progress.ts`:

```python
"""Port of Plotroom's estimator/progress.ts — the CI-progress ETA.

Pure: (checks, expected_set, lookup, now, samples, queue_delay) → (percent, eta, overdue).
Only percent/eta_seconds/overdue are surfaced (failed/etaRange dropped — see spec §2).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from server.pipeline.history import Expected, median, percentile

RUNNING_CAP = 0.97
OVERDUE_FACTOR = 1.5
DEFAULT_DURATION = 60.0
_FAILED = {"FAILURE", "TIMED_OUT", "STARTUP_FAILURE", "ACTION_REQUIRED"}


@dataclass(frozen=True)
class ProgressResult:
    percent: int
    eta_seconds: int | None
    overdue: bool


def _parse_epoch_s(iso: str | None) -> float:
    if not iso:
        return float("nan")
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return float("nan")


def compute_progress(
    checks: list[dict],
    expected_set: list[str],
    lookup: Callable[[str], Expected | None],
    now: datetime,
    samples: Callable[[str], list[float]] | None = None,
    queue_delay: Callable[[str], float | None] | None = None,
) -> ProgressResult:
    live = {c["name"]: c for c in checks}
    names = set(expected_set) | {c["name"] for c in checks}
    known_p50s = [e.p50 for e in (lookup(n) for n in names) if e is not None]
    fallback = median(known_p50s) if known_p50s else DEFAULT_DURATION

    w_sum = p_sum = eta_p50 = eta_p90 = 0.0
    overdue = any_unfinished = False
    now_s = now.timestamp()

    for name in names:
        c = live.get(name)
        if c is not None and c.get("conclusion") == "SKIPPED":
            continue
        exp = lookup(name)
        p50 = exp.p50 if exp else fallback
        p90 = exp.p90 if exp else p50
        status = c.get("status") if c else None
        if status == "COMPLETED":
            progress, rem50, rem90 = 1.0, 0.0, 0.0
        elif status == "IN_PROGRESS" and c.get("started_at"):
            raw_elapsed = now_s - _parse_epoch_s(c["started_at"])
            if raw_elapsed != raw_elapsed:  # NaN
                progress, rem50, rem90 = 0.0, p50, p90
                any_unfinished = True
            else:
                elapsed = max(raw_elapsed, 0.0)
                progress = min(elapsed / p50, RUNNING_CAP) if p50 else RUNNING_CAP
                any_unfinished = True
                vals = (samples(name) if samples else []) or []
                qualifying = [v for v in vals if v > elapsed] if len(vals) >= 5 else None
                if qualifying is not None and len(qualifying) == 0:
                    if len(vals) >= 10:
                        overdue = True
                    elif elapsed > OVERDUE_FACTOR * p50:
                        overdue = True
                    rem50 = rem90 = 0.0
                elif qualifying is not None and len(qualifying) >= 2:
                    sq = sorted(qualifying)
                    rem50 = median(sq) - elapsed
                    rem90 = percentile(sq, 0.9) - elapsed
                    progress = min(elapsed / (elapsed + rem50), RUNNING_CAP) if (elapsed + rem50) else RUNNING_CAP
                else:
                    if elapsed > OVERDUE_FACTOR * p50:
                        overdue = True
                    rem50 = max(p50 - elapsed, 0.0)
                    rem90 = max(p90 - elapsed, 0.0)
        else:
            delay = (queue_delay(name) if queue_delay else None) or 0.0
            progress, rem50, rem90 = 0.0, p50 + delay, p90 + delay
            any_unfinished = True
        w_sum += p50
        p_sum += p50 * progress
        eta_p50 = max(eta_p50, rem50)
        eta_p90 = max(eta_p90, rem90)

    percent = round((p_sum / w_sum) * 100) if w_sum else 100
    if not any_unfinished:
        return ProgressResult(100, 0, False)
    if overdue:
        return ProgressResult(percent, None, True)
    return ProgressResult(percent, round(eta_p50), False)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH=$PWD/src .../.venv/bin/python -m pytest server/tests/test_pipeline_estimator.py -v`
Expected: PASS. Cross-check each asserted value against `progress.ts` by hand; fix asserted numbers (not the port) where your manual trace differs.

- [ ] **Step 5: Commit**

```bash
git add server/pipeline/estimator.py server/tests/test_pipeline_estimator.py
git commit -m "feat(eta): port compute_progress estimator (progress.ts)"
```

---

### Task 4: required-checks + expected-set glue

**Files:**
- Create: `server/pipeline/required.py`
- Test: `server/tests/test_pipeline_required.py`

**Interfaces:**
- Produces (ported from `classify.ts` + the `poller.ts` expectedSet branch):
  - `matching_prefix(name, prefixes) -> str | None`
  - `matches_required_prefix(name, prefixes) -> bool`
  - `workflow_scope_allows(check_wf, rollup_wf) -> bool`
  - `required_checks(checks: list[dict], prefixes: list[str] | None = None, rollup_wf: str | None = None) -> list[dict]`
  - `effective_prefixes(repo) -> list[str]` → `[]`; `rollup_workflow_for(repo) -> None`
  - `build_expected_set(history_set: list[str], checks: list[dict], prefixes, rollup_wf, now) -> list[str]`
  - `EXPECTED_SET_STALE_MS = 600000`

- [ ] **Step 1: Write the failing test**

```python
from datetime import datetime, timezone
from server.pipeline.required import (
    required_checks, build_expected_set, effective_prefixes, rollup_workflow_for,
    workflow_scope_allows,
)

NOW = datetime(2026, 6, 25, 12, 0, 0, tzinfo=timezone.utc)


def test_no_config_collapses():
    assert effective_prefixes("o/n") == []
    assert rollup_workflow_for("o/n") is None
    assert workflow_scope_allows("any", None) is True


def test_required_checks_falls_back_to_all_when_no_signal():
    checks = [{"name": "a", "event": "pull_request", "isRequired": False, "workflowName": "ci"},
              {"name": "b", "event": "pull_request", "isRequired": False, "workflowName": "ci"}]
    assert [c["name"] for c in required_checks(checks, [], None)] == ["a", "b"]


def test_required_checks_uses_isrequired_marks_when_present():
    checks = [{"name": "a", "event": "pull_request", "isRequired": True, "workflowName": "ci"},
              {"name": "b", "event": "pull_request", "isRequired": False, "workflowName": "ci"}]
    assert [c["name"] for c in required_checks(checks, [], None)] == ["a"]


def test_required_checks_drops_merge_group():
    checks = [{"name": "a", "event": "merge_group", "isRequired": True, "workflowName": "ci"},
              {"name": "b", "event": "pull_request", "isRequired": True, "workflowName": "ci"}]
    assert [c["name"] for c in required_checks(checks, [], None)] == ["b"]


def test_expected_set_filters_to_required_when_marked():
    history = ["a", "b", "c"]
    checks = [{"name": "a", "event": "pull_request", "isRequired": True, "workflowName": "ci"},
              {"name": "b", "event": "pull_request", "isRequired": False, "workflowName": "ci"}]
    assert sorted(build_expected_set(history, checks, [], None, NOW)) == ["a"]
```

- [ ] **Step 2: Run to verify it fails**

Run: `PYTHONPATH=$PWD/src .../.venv/bin/python -m pytest server/tests/test_pipeline_required.py -v`
Expected: FAIL — `ModuleNotFoundError`.

- [ ] **Step 3: Implement** (port `classify.ts` lines 36–80 + the `poller.ts` expectedSet branch)

Create `server/pipeline/required.py`:

```python
"""Port of Plotroom's required-checks + expectedSet glue (classify.ts + poller.ts).

ccswitch has no per-repo config (Plotroom config.json repos:{}), so prefixes are
always [] and rollup_wf None — but the logic is ported faithfully so a future
config can switch it on.
"""
from __future__ import annotations

from datetime import datetime

EXPECTED_SET_STALE_MS = 10 * 60_000


def matching_prefix(name: str, prefixes) -> str | None:
    best = None
    for p in prefixes:
        if name.startswith(p) and (best is None or len(p) > len(best)):
            best = p
    return best


def matches_required_prefix(name: str, prefixes) -> bool:
    return prefixes is not None and matching_prefix(name, prefixes) is not None


def workflow_scope_allows(check_wf: str | None, rollup_wf: str | None = None) -> bool:
    return rollup_wf is None or check_wf is None or check_wf == rollup_wf


def required_checks(checks: list[dict], prefixes=None, rollup_wf: str | None = None) -> list[dict]:
    pr_event = [c for c in checks if c.get("event") != "merge_group"]
    req = [c for c in pr_event if c.get("isRequired")
           or (matches_required_prefix(c["name"], prefixes)
               and workflow_scope_allows(c.get("workflowName"), rollup_wf))]
    if req:
        return req
    return [] if (prefixes and len(prefixes)) else pr_event


def effective_prefixes(repo: str) -> list[str]:
    return []


def rollup_workflow_for(repo: str) -> str | None:
    return None


def build_expected_set(history_set: list[str], checks: list[dict], prefixes,
                       rollup_wf: str | None, now: datetime) -> list[str]:
    if prefixes:
        req_names = {c["name"] for c in required_checks(checks, prefixes, rollup_wf)}
        foreign = {c["name"] for c in checks if not workflow_scope_allows(c.get("workflowName"), rollup_wf)}
        return [n for n in history_set
                if (matches_required_prefix(n, prefixes) and n not in foreign) or n in req_names]
    if any(c.get("isRequired") for c in checks):
        req_names = {c["name"] for c in required_checks(checks, prefixes, rollup_wf)}
        return [n for n in history_set if n in req_names]
    # all-done-stale path: drop expected names no longer live once everything finished long ago
    if checks and all(c.get("status") == "COMPLETED" for c in checks):
        newest = max((_epoch_ms(c.get("completed_at")) for c in checks), default=0.0)
        if now.timestamp() * 1000.0 - newest > EXPECTED_SET_STALE_MS:
            live = {c["name"] for c in checks}
            return [n for n in history_set if n in live]
    return history_set


def _epoch_ms(iso: str | None) -> float:
    if not iso:
        return 0.0
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp() * 1000.0
    except ValueError:
        return 0.0
```

- [ ] **Step 4: Run to verify pass**

Run: `PYTHONPATH=$PWD/src .../.venv/bin/python -m pytest server/tests/test_pipeline_required.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add server/pipeline/required.py server/tests/test_pipeline_required.py
git commit -m "feat(eta): required-checks + expected-set glue (no-config path)"
```

---

### Task 5: per-PR check-timing GraphQL query + parser

**Files:**
- Modify: `server/github/pr_status.py` (add `build_eta_checks_query` + `parse_eta_checks_response`)
- Test: `server/tests/test_eta_checks_query.py`

**Interfaces:**
- Produces:
  - `build_eta_checks_query(prs: list[tuple[str, int]]) -> str` — one GraphQL doc with one aliased `repository(owner,name){ pullRequest(number: N){ commits(last:1){ … statusCheckRollup{ contexts(first:100){ nodes{ __typename ... on CheckRun { name status conclusion startedAt isRequired(pullRequestNumber: N) checkSuite { workflowRun { workflow { name } } } } ... on StatusContext { context state } } } } } } } }` block per PR (number embedded as literal in both `pullRequest(number:)` and `isRequired(pullRequestNumber:)`).
  - `parse_eta_checks_response(response: dict, prs: list[tuple[str, int]]) -> dict[tuple[str, int], list[dict]]` — per (repo, number) a list of `{name, status, conclusion, started_at, isRequired, workflowName, event}` (event fixed `"pull_request"`). Maps GraphQL `status` (`COMPLETED`/`IN_PROGRESS`/`QUEUED`/`PENDING`/etc.) through unchanged; StatusContext → `{name: context, status: 'COMPLETED', conclusion: state, started_at: None, isRequired: False, workflowName: None}`.

- [ ] **Step 1: Write the failing test**

```python
from server.github.pr_status import build_eta_checks_query, parse_eta_checks_response


def test_build_eta_query_embeds_number_in_both_places():
    q = build_eta_checks_query([("steiner385/cc-account-switcher", 293)])
    assert "pullRequest(number: 293)" in q
    assert "isRequired(pullRequestNumber: 293)" in q
    assert "startedAt" in q and "status" in q


def test_parse_eta_checks_maps_checkruns():
    resp = {"data": {"r0": {"pullRequest": {"commits": {"nodes": [{"commit": {"statusCheckRollup":
        {"contexts": {"nodes": [
            {"__typename": "CheckRun", "name": "lint-and-test", "status": "IN_PROGRESS",
             "conclusion": None, "startedAt": "2026-06-25T11:59:00Z", "isRequired": True,
             "checkSuite": {"workflowRun": {"workflow": {"name": "CI"}}}},
        ]}}}}]}}}}}
    out = parse_eta_checks_response(resp, [("o/n", 293)])
    chk = out[("o/n", 293)][0]
    assert chk == {"name": "lint-and-test", "status": "IN_PROGRESS", "conclusion": None,
                   "started_at": "2026-06-25T11:59:00Z", "isRequired": True,
                   "workflowName": "CI", "event": "pull_request"}
```

- [ ] **Step 2: Run to verify it fails**

Run: `PYTHONPATH=$PWD/src .../.venv/bin/python -m pytest server/tests/test_eta_checks_query.py -v`
Expected: FAIL — `ImportError`.

- [ ] **Step 3: Implement** in `server/github/pr_status.py` (reuse the `_alias`/`json.dumps` pattern; add near `build_population_query`):

```python
def build_eta_checks_query(prs: list[tuple[str, int]]) -> str:
    if not prs:
        return ""
    blocks = []
    for i, (name_with_owner, number) in enumerate(prs):
        owner, _, name = name_with_owner.partition("/")
        blocks.append(
            f"{_alias(i)}: repository(owner: {json.dumps(owner)}, name: {json.dumps(name)}) {{\n"
            f"  pullRequest(number: {int(number)}) {{\n"
            f"    commits(last: 1) {{ nodes {{ commit {{ statusCheckRollup {{\n"
            f"      contexts(first: 100) {{ nodes {{\n"
            f"        __typename\n"
            f"        ... on CheckRun {{ name status conclusion startedAt "
            f"isRequired(pullRequestNumber: {int(number)}) "
            f"checkSuite {{ workflowRun {{ workflow {{ name }} }} }} }}\n"
            f"        ... on StatusContext {{ context state }}\n"
            f"      }} }}\n"
            f"    }} }} }} }}\n"
            f"  }}\n"
            f"}}"
        )
    return "query {\n" + "\n".join(blocks) + "\n}"


def parse_eta_checks_response(
    response: dict, prs: list[tuple[str, int]]
) -> dict[tuple[str, int], list[dict]]:
    data = (response or {}).get("data") or {}
    out: dict[tuple[str, int], list[dict]] = {}
    for i, key in enumerate(prs):
        node = data.get(_alias(i)) or {}
        pr = node.get("pullRequest") or {}
        commits = ((pr.get("commits") or {}).get("nodes")) or []
        contexts = []
        if commits:
            rollup = (commits[0].get("commit") or {}).get("statusCheckRollup") or {}
            contexts = ((rollup.get("contexts") or {}).get("nodes")) or []
        checks: list[dict] = []
        for c in contexts:
            if c.get("__typename") == "CheckRun":
                wf = (((c.get("checkSuite") or {}).get("workflowRun") or {}).get("workflow") or {})
                checks.append({
                    "name": c.get("name", ""), "status": c.get("status", ""),
                    "conclusion": c.get("conclusion"), "started_at": c.get("startedAt"),
                    "isRequired": bool(c.get("isRequired")), "workflowName": wf.get("name"),
                    "event": "pull_request",
                })
            else:  # StatusContext — legacy, treated as completed
                checks.append({
                    "name": c.get("context", ""), "status": "COMPLETED",
                    "conclusion": c.get("state"), "started_at": None,
                    "isRequired": False, "workflowName": None, "event": "pull_request",
                })
        out[key] = checks
    return out
```

- [ ] **Step 4: Run to verify pass**

Run: `PYTHONPATH=$PWD/src .../.venv/bin/python -m pytest server/tests/test_eta_checks_query.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add server/github/pr_status.py server/tests/test_eta_checks_query.py
git commit -m "feat(eta): per-PR check-timing GraphQL query + parser (status/startedAt/isRequired)"
```

---

### Task 6: ETA fields on `PullRequestInfo`

**Files:**
- Modify: `server/github/pr_status.py` (`PullRequestInfo` dataclass + `to_dict`/`from_dict`)
- Modify: `server/tests/test_sessions_pr_field.py` (the exact-dict assertion)
- Test: `server/tests/test_pr_status.py` (round-trip)

**Interfaces:**
- Produces: `PullRequestInfo` gains `eta_seconds: int | None = None`, `eta_percent: int | None = None`, `eta_overdue: bool = False`, round-tripping through `to_dict`/`from_dict`.

- [ ] **Step 1: Write the failing test** (append to `server/tests/test_pr_status.py`)

```python
def test_pull_request_info_eta_round_trips():
    from server.github.pr_status import PullRequestInfo
    info = PullRequestInfo(number=1, url="u", state="open", checks="pending", title="t",
                           eta_seconds=120, eta_percent=46, eta_overdue=False)
    d = info.to_dict()
    assert d["eta_seconds"] == 120 and d["eta_percent"] == 46 and d["eta_overdue"] is False
    back = PullRequestInfo.from_dict(d)
    assert back.eta_seconds == 120 and back.eta_percent == 46 and back.eta_overdue is False


def test_pull_request_info_eta_defaults_for_old_payload():
    from server.github.pr_status import PullRequestInfo
    d = {"number": 1, "url": "u", "state": "open", "checks": "none"}
    info = PullRequestInfo.from_dict(d)
    assert info.eta_seconds is None and info.eta_percent is None and info.eta_overdue is False
```

- [ ] **Step 2: Run to verify it fails**

Run: `PYTHONPATH=$PWD/src .../.venv/bin/python -m pytest server/tests/test_pr_status.py -k eta -v`
Expected: FAIL — unexpected keyword argument.

- [ ] **Step 3: Implement** — in `server/github/pr_status.py`, add to `PullRequestInfo` (after `repo`):

```python
    eta_seconds: int | None = None
    eta_percent: int | None = None
    eta_overdue: bool = False
```

In `to_dict` add: `"eta_seconds": self.eta_seconds, "eta_percent": self.eta_percent, "eta_overdue": self.eta_overdue,`
In `from_dict` add: `eta_seconds=d.get("eta_seconds"), eta_percent=d.get("eta_percent"), eta_overdue=d.get("eta_overdue", False),`

- [ ] **Step 4: Fix the exact-dict sibling test** (memory: pullrequestinfo-todict-exact-shape-test)

In `server/tests/test_sessions_pr_field.py`, the `test_record_to_dict_includes_prs_when_given` expected dict gains:
```python
            "eta_seconds": None, "eta_percent": None, "eta_overdue": False,
```

- [ ] **Step 5: Run both test files to verify pass**

Run: `PYTHONPATH=$PWD/src .../.venv/bin/python -m pytest server/tests/test_pr_status.py server/tests/test_sessions_pr_field.py -v`
Expected: PASS (all).

- [ ] **Step 6: Commit**

```bash
git add server/github/pr_status.py server/tests/test_pr_status.py server/tests/test_sessions_pr_field.py
git commit -m "feat(eta): add eta_seconds/eta_percent/eta_overdue to PullRequestInfo"
```

---

### Task 7: ETA enrichment — compute per open PR and attach

**Files:**
- Create: `server/pipeline/eta.py`
- Test: `server/tests/test_pipeline_eta.py`

**Interfaces:**
- Consumes: `HistoryStore` (Task 2), `compute_progress` (Task 3), `required_checks`/`build_expected_set` (Task 4), `parse_eta_checks_response` (Task 5).
- Produces:
  - `compute_pr_eta(store: HistoryStore, repo: str, checks: list[dict], now: datetime) -> tuple[int|None, int|None, bool]` — returns `(eta_seconds, eta_percent, eta_overdue)` by assembling history + expected-set + required checks and calling `compute_progress`. Mirrors the `poller.ts` ciProgress call site exactly (event `'pull_request'`).

- [ ] **Step 1: Write the failing test**

```python
from datetime import datetime, timezone
import sqlite3
from server.pipeline.history import HistoryStore
from server.pipeline.eta import compute_pr_eta

NOW = datetime(2026, 6, 25, 12, 0, 0, tzinfo=timezone.utc)


def _store(tmp_path):
    p = tmp_path / "ph.db"
    c = sqlite3.connect(str(p))
    c.executescript("""
        CREATE TABLE check_durations (repo TEXT, check_name TEXT, event TEXT,
          duration_secs REAL, completed_at TEXT, conclusion TEXT, head_sha TEXT,
          run_attempt INTEGER, started_at TEXT, run_number INTEGER);
        CREATE TABLE runner_waits (repo TEXT, check_name TEXT, event TEXT,
          wait_secs REAL, started_at TEXT, pool TEXT);
    """)
    for i in range(8):
        c.execute("INSERT INTO check_durations VALUES (?,?,?,?,?,?,?,?,?,?)",
                  ("o/n", "ci", "pull_request", 100, f"2026-06-2{i%10}T00:00:00Z",
                   "SUCCESS", "s", 1, "x", i))
    c.commit(); c.close()
    return HistoryStore(str(p))


def test_compute_pr_eta_queued_check(tmp_path):
    s = _store(tmp_path)
    # no live checks → expected 'ci' is queued → eta ≈ p50 (100)
    eta, percent, overdue = compute_pr_eta(s, "o/n", [], NOW)
    assert eta == 100 and percent == 0 and overdue is False
```

- [ ] **Step 2: Run to verify it fails**

Run: `PYTHONPATH=$PWD/src .../.venv/bin/python -m pytest server/tests/test_pipeline_eta.py -v`
Expected: FAIL — `ModuleNotFoundError`.

- [ ] **Step 3: Implement** — `server/pipeline/eta.py`, mirroring the `poller.ts` ciProgress assembly:

```python
"""Assemble the CI-progress ETA for one PR — mirrors poller.ts's ciProgress call site."""
from __future__ import annotations

from datetime import datetime

from server.pipeline.estimator import compute_progress
from server.pipeline.history import HistoryStore
from server.pipeline.required import (
    build_expected_set, effective_prefixes, required_checks, rollup_workflow_for,
)

_EVENT = "pull_request"


def compute_pr_eta(
    store: HistoryStore, repo: str, checks: list[dict], now: datetime
) -> tuple[int | None, int | None, bool]:
    prefixes = effective_prefixes(repo)
    rollup_wf = rollup_workflow_for(repo)
    req = required_checks(checks, prefixes, rollup_wf)
    history_set = store.expected_set(repo, _EVENT, now)
    expected_set = build_expected_set(history_set, checks, prefixes, rollup_wf, now)
    result = compute_progress(
        req, expected_set,
        lookup=lambda n: store.expected(repo, n, _EVENT),
        now=now,
        samples=lambda n: store.samples(repo, n, _EVENT),
        queue_delay=lambda n: store.expected_runner_wait(repo, n, _EVENT),
    )
    return result.eta_seconds, result.percent, result.overdue
```

- [ ] **Step 4: Run to verify pass**

Run: `PYTHONPATH=$PWD/src .../.venv/bin/python -m pytest server/tests/test_pipeline_eta.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add server/pipeline/eta.py server/tests/test_pipeline_eta.py
git commit -m "feat(eta): per-PR ETA assembly (ciProgress call-site port)"
```

---

### Task 8: Live-parity gate (the acceptance test)

**Files:**
- Modify: `server/github/parity/coverage.py` (add phase 2 with `eta_seconds`)
- Create: `server/github/parity/eta_parity.py` (compute ccswitch ETA for oracle PRs + diff)
- Test/CLI: `server/tests/test_eta_parity_live.py` (marked live; skipped when oracle/ source absent)

**Interfaces:**
- Consumes: everything above + the live oracle `http://127.0.0.1:4400/api/state` + the real `history.db`.
- Produces: `eta_parity_report(state: dict, store: HistoryStore, fetch_checks: Callable[[list[tuple[str,int]]], dict], now: datetime) -> list[dict]` — for each oracle PR with a non-null `stage.etaSeconds` that derives from CI progress, computes ccswitch's ETA and returns `{repo, number, oracle_eta, ours_eta, within_tolerance}` where tolerance = `abs(ours-oracle) <= max(2, 0.05*oracle)`.

- [ ] **Step 1: Widen the phase map**

In `server/github/parity/coverage.py`:
```python
PHASE_FIELDS: dict[int, frozenset[str]] = {
    0: frozenset({"number", "url", "checks"}),
    1: frozenset({"number", "url", "checks"}) | frozenset({"stage"}),
    2: frozenset({"number", "url", "checks", "stage", "eta_seconds"}),
}
```

- [ ] **Step 2: Write the live parity test** (`server/tests/test_eta_parity_live.py`)

```python
import json, os, subprocess, urllib.request
from datetime import datetime, timezone
import pytest

ORACLE = "http://127.0.0.1:4400/api/state"
SOURCE = "/mnt/ssk-ssd/tony/GitHub/pr-dashboard/data/history.db"


def _oracle_state():
    try:
        with urllib.request.urlopen(ORACLE, timeout=5) as r:
            return json.load(r)
    except Exception:
        return None


@pytest.mark.skipif(not os.path.exists(SOURCE), reason="history.db source absent")
def test_eta_live_parity():
    state = _oracle_state()
    if state is None:
        pytest.skip("live Plotroom oracle not reachable")
    from server.pipeline.history_migrate import migrate_history
    from server.pipeline.history import HistoryStore
    from server.pipeline.eta import compute_pr_eta
    from server.github.pr_status import build_eta_checks_query, parse_eta_checks_response

    migrate_history(SOURCE, "/tmp/parity_history.db")
    store = HistoryStore("/tmp/parity_history.db")
    now = datetime.now(timezone.utc)

    targets = [(r["repo"], p["number"], (p.get("stage") or {}).get("etaSeconds"))
               for r in state["repos"] for p in r["prs"]
               if (p.get("stage") or {}).get("etaSeconds") is not None]
    assert targets, "gate: oracle exposes no ETA PRs right now"

    # fetch live checks for all targets in one query
    pairs = [(repo, num) for repo, num, _ in targets]
    proc = subprocess.run(["gh", "api", "graphql", "-f",
                           f"query={build_eta_checks_query(pairs)}"],
                          capture_output=True, text=True)
    checks_by_pr = parse_eta_checks_response(json.loads(proc.stdout or "{}"), pairs)

    mismatches = []
    for repo, num, oracle_eta in targets:
        ours, _pct, _ov = compute_pr_eta(store, repo, checks_by_pr.get((repo, num), []), now)
        tol = max(2, 0.05 * oracle_eta)
        if ours is None or abs(ours - oracle_eta) > tol:
            mismatches.append((repo, num, oracle_eta, ours))
    assert not mismatches, f"ETA parity mismatches: {mismatches}"
```

- [ ] **Step 3: Run the live parity test and ITERATE**

Run: `PYTHONPATH=$PWD/src .../.venv/bin/python -m pytest server/tests/test_eta_parity_live.py -v -s`
Expected first run: likely FAIL with specific `(repo, number, oracle_eta, ours)` mismatches.

**Iterate to zero mismatches.** The divergence will be in the input-assembly glue, not the estimator math. Investigate in this order (these are the known suspect points):
1. **`event` mismatch** — confirm the oracle uses `pull_request` for these PRs; if a PR's checks are `merge_group`, it's a queue-stage ETA → out of scope, exclude it from `targets`.
2. **`isRequired` source** — verify the GraphQL returned real `isRequired` values (not all False); if all False, the `isRequired(pullRequestNumber:)` arg is wrong — fix Task 5's query.
3. **expected-set filter** — compare `build_expected_set` output names against what the oracle's percent implies.
4. **recency window / now** — for running checks, align `now`; for all-completed PRs `now` is irrelevant.

Fix the glue (Tasks 4/5/7 code) — NOT the estimator (Task 3) unless a hand-trace against `progress.ts` proves a port bug. Re-run until `mismatches == []`.

> If a target's ETA provably comes from queue-stage (`queue.ts`), document it and exclude it — only CI-progress ETAs are in scope (spec §2).

- [ ] **Step 4: Commit**

```bash
git add server/github/parity/coverage.py server/github/parity/eta_parity.py server/tests/test_eta_parity_live.py
git commit -m "test(eta): live-parity gate — ccswitch ETA matches oracle within tolerance"
```

---

### Task 9: Wire ETA into the PR population + render on the board

**Files:**
- Modify: `server/github/pr_population.py` (compute ETA for open PRs each cycle) OR `server/sessions/orchestrator.py` `all_prs()` enrichment — pick the single place PRs are assembled for `/api/prs`.
- Modify: `web/src/api/types.ts` (`PullRequestInfo` gains `etaSeconds`/`etaPercent`/`etaOverdue`)
- Modify: `web/src/components/PipelineView.tsx` (render ETA on `ci` rows)
- Test: `web/src/components/__tests__/PipelineBoard.test.tsx` (ETA rendering)

**Interfaces:**
- Consumes: `compute_pr_eta` (Task 7), `build_eta_checks_query`/`parse_eta_checks_response` (Task 5), the migrated `HistoryStore`.
- Produces: open PRs in the population carry `eta_seconds`/`eta_percent`/`eta_overdue`; the board shows `~Xm`/`overdue` on `ci`-stage rows.

- [ ] **Step 1 (backend): write the failing test** — `server/tests/test_population_eta_wired.py`

Assert that the population poller, given a `HistoryStore` and a stubbed eta-checks fetch, attaches `eta_seconds` to an open PR. (Mirror `test_pr_population.py`'s injection style; inject a `query_fn` for the eta-checks query and a `HistoryStore` pointing at a temp db built like Task 7's `_store`.)

- [ ] **Step 2:** Run → FAIL.

- [ ] **Step 3 (backend): implement** — in the population assembly, after building each open PR's `PullRequestInfo`, for open PRs batch-fetch checks via `build_eta_checks_query`, call `compute_pr_eta`, and `dataclasses.replace(info, eta_seconds=…, eta_percent=…, eta_overdue=…)`. Guard: if `HistoryStore`/source is unavailable, leave ETA fields None (never crash the board). Construct the `HistoryStore` once (lazy singleton) pointing at `default_dest()`; if the db is absent, skip ETA enrichment.

- [ ] **Step 4:** Run backend test → PASS.

- [ ] **Step 5 (frontend): write the failing test** — extend `PipelineBoard.test.tsx`:

```tsx
it('shows an ETA on a ci-stage PR that has one', () => {
  render(<PipelineBoard prs={[pr({ number: 7, stage: 'ci', etaSeconds: 720 })]} />)
  expect(screen.getByText(/~12m/)).toBeInTheDocument()
})
it('shows overdue when eta_overdue', () => {
  render(<PipelineBoard prs={[pr({ number: 8, stage: 'ci', etaOverdue: true })]} />)
  expect(screen.getByText(/overdue/i)).toBeInTheDocument()
})
```
Add `etaSeconds?`, `etaPercent?`, `etaOverdue?` to the `pr()` helper and to `PullRequestInfo` in `web/src/api/types.ts` (camelCase wire — confirm the backend `to_dict` keys; if backend emits snake_case `eta_seconds`, the frontend type uses `eta_seconds` to match — match the actual JSON).

> NOTE: backend `to_dict` emits **snake_case** (`eta_seconds`). Use the snake_case keys in the TS type and tests to match the wire exactly (the existing `PullRequestInfo` TS fields are camelCase but map 1:1 to snake only where the backend emits snake — verify against a real `/api/prs` payload or the `to_dict` in Task 6 and use whatever it actually emits).

- [ ] **Step 6:** Run → FAIL.

- [ ] **Step 7 (frontend): implement** — a small `formatEta(seconds)` (`~12m`, `~45s`, `~1h5m`) and render it on rows where `stage === 'ci'`: show `overdue` when `eta_overdue`, else `~{formatEta(eta_seconds)}` when `eta_seconds != null`, else nothing.

- [ ] **Step 8:** Run frontend test → PASS; then full gates: `npx tsc -b --force`, `npx eslint . --max-warnings 0`, `npx vitest run`.

- [ ] **Step 9: Commit**

```bash
git add server/github/pr_population.py server/tests/test_population_eta_wired.py web/src/api/types.ts web/src/components/PipelineView.tsx web/src/components/__tests__/PipelineBoard.test.tsx
git commit -m "feat(eta): compute ETA in the population + render on ci-stage board rows"
```

---

## Final verification (before opening the PR)

- [ ] Backend full suite (CI env): `env -u CCSWITCH_TAILNET_ALLOWLIST -u CCSWITCH_ENABLE_XVFB CCSWITCH_DEV=1 PYTHONPATH=$PWD/src .../.venv/bin/python -m pytest server/tests/ -q` — only the 8 documented env failures may remain.
- [ ] `ruff check src/ server/` clean; `mypy` no new errors.
- [ ] Frontend (in `web/`): `npx tsc -b --force` · `npx eslint . --max-warnings 0` · `npx vitest run`.
- [ ] **Live-parity gate green** (Task 8): `pytest server/tests/test_eta_parity_live.py -v` reports 0 mismatches (the headline acceptance).
- [ ] PR per CLAUDE.md: non-draft + `gh pr merge <#> --squash --auto`. Base on #302's branch; if #302 merged, rebase onto `origin/master` first.

---

## Self-Review notes (author)

- **Spec coverage:** §3.1 migration→T1; §3.2 history helpers→T2; §3.3 estimator→T3; §3.4 required glue→T4; §3.5 GraphQL timing→T5; §3.6 PullRequestInfo fields→T6 + wire→T9; ETA assembly→T7; §3.7 board→T9; §3.8 live-parity→T8. §2 non-goals (queue/recording/range) honored — no task ports `waits.ts`/`queue.ts`. §6 success criteria map to T8 (parity), T3 (unit), T1 (row counts), T9 (no fabricated 0).
- **Type consistency:** `compute_progress`→`ProgressResult{percent,eta_seconds,overdue}` (T3) consumed by `compute_pr_eta`→`(eta_seconds,eta_percent,overdue)` (T7); `PullRequestInfo.eta_seconds/eta_percent/eta_overdue` (T6) carried unchanged to wire (T9). `build_eta_checks_query`/`parse_eta_checks_response` shapes (T5) feed T7/T8. Check dict shape `{name,status,conclusion,started_at,isRequired,workflowName,event}` consistent T5↔T3↔T4↔T7.
- **Known iteration point:** T8 is wire-and-iterate-to-zero-mismatch by design — the exact glue divergence on the live PRs only surfaces against the oracle (spec §5). The estimator/history/required ports (T1–T4) are independently unit-verified so T8's iteration is confined to input assembly.
- **Audit trap honored:** T6 updates `test_sessions_pr_field.py` (memory: pullrequestinfo-todict-exact-shape-test).
- **Soft spots flagged inline:** exact `p50` in T2 (compute from the percentile rule), the wire-key case (snake vs camel) in T9 (verify against the real `to_dict`), and the fixture style copies in T8/T9 (mirror the named existing test files).

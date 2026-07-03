# Merge-Train Widget (M-Track-10) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a Plotroom-style per-repo merge-train widget to ccswitch's Pipeline view — live queue segments (failing / building / blocked) plus historical metrics (trains/hr, batch success %, ejects/24h) derived from persisted queue snapshots.

**Architecture:** The existing single `PrPopulationPoller` sweep gains a step that queries each repo's `mergeQueue.entries` and writes one observation row per queued PR to a new SQLAlchemy table (Approach A — raw snapshots are the single source of truth). A pure derivation layer turns the 24h window of observations into a `MergeTrainState` at read time; a new `GET /api/pipeline/trains` serves `{repo: state}`; a React `MergeTrainWidget` renders it atop each repo group.

**Tech Stack:** Python FastAPI + SQLAlchemy(async SQLite) backend · React + Vite + TS + Tailwind frontend · GitHub GraphQL via the `gh` CLI · pytest + vitest.

## Global Constraints

- **No second poller** — extend the existing `PrPopulationPoller` sweep (constitution VI).
- **New DB columns MUST be nullable** — dev `create_all` never ALTERs; `init_db` backfills only nullable columns. A NOT NULL column needs a real migration (do not add one here).
- **Single source of truth** — deploy-backlog counts reuse existing `PullRequestInfo` data; do not re-derive deploy state from GitHub.
- **Verify against the real API, not assumptions** (constitution III) — Task 1 captures a real `mergeQueue` response before any parser depends on its shape.
- **Read path makes no GitHub call** — the endpoint serves from the DB/cache, same as `GET /api/prs`.
- **Pure I/O separation** — query-building, parsing, and metric derivation are pure functions (no network, no DB); only the store module and poller touch I/O.
- **Auth** — every new endpoint depends on `current_identity`; tests must pin `CCSWITCH_TAILNET_ALLOWLIST` or run under `CCSWITCH_DEV=1` with an empty allowlist.
- **Async tests use `@pytest.mark.asyncio`** (the repo uses `pytest-asyncio`, strict mode — there is no `anyio`). DB-touching tests follow the `asyncio.run(body())` + `init_db(db_url=f"sqlite+aiosqlite:///{tmp_path}/…")` idiom from `server/tests/test_app_rehydrate_on_startup.py`, **not** async fixtures.
- **Verified test env (this session):** the worktree has no built env. Run server tests as:
  `cd /mnt/ssk-ssd/tony/GitHub/.ccswitch/worktrees/ccswitch/5a95d24e && /mnt/ssk-ssd/tony/GitHub/.ccswitch/clones/steiner385/cc-account-switcher/.venv/bin/python -m pytest server/tests/…`
  (run from the worktree root so `import server` resolves to the worktree copy — confirmed). `web/node_modules` is already symlinked from that clone; run web tests in `web/` with `npx vitest run` / `npx tsc --noEmit` / `npx eslint`.

---

## File Structure

**Backend (new):**
- `server/github/merge_queue.py` — pure `build_merge_queue_query`, `parse_merge_queue_response`, and the `QueueEntry` dataclass.
- `server/pipeline/merge_train.py` — `Observation`, `MergeTrainState` (+`to_dict`/`from_dict`), and the pure `derive_merge_train_state`.
- `server/pipeline/merge_queue_store.py` — async `record` / `window` / `prune` over the app DB.

**Backend (modified):**
- `server/db/models.py` — add `MergeQueueObservation` model (nullable columns).
- `server/github/pr_population.py` — snapshot step in `refresh_once` (injected query-fn + sink).
- `server/sessions/orchestrator.py` — construct the poller with the merge-queue query-fn + sink; add `all_merge_trains()`.
- `server/api/pipeline.py` — `GET /api/pipeline/trains`.

**Frontend (new):**
- `web/src/components/MergeTrainWidget.tsx` — the widget (collapses to null when idle).

**Frontend (modified):**
- `web/src/api/types.ts` — `MergeTrainState` + sub-types.
- `web/src/api/hooks.ts` — `QK.pipelineTrains` + `useMergeTrains()`.
- `web/src/components/PipelineView.tsx` — mount the widget under each repo header.

---

## Task 1: Capture the real `mergeQueue` response (verification spike)

Verify the repo-level merge-queue GraphQL shape against the live API before any parser depends on it. `cairnea/KinDash` has an active merge queue (per the reference screenshot).

**Files:**
- Create: `server/tests/fixtures/merge_queue_kindash.json` (captured response)
- Create: `docs/superpowers/spikes/2026-06-30-merge-queue-query.md` (the confirmed query + notes)

- [ ] **Step 1: Run the query against the live API**

Run (in the main checkout with the live shell env / `gh` auth; the worktree has no token):

```bash
gh api graphql -f query='
query {
  r0: repository(owner: "cairnea", name: "KinDash") {
    mergeQueue {
      entries(first: 50) {
        nodes {
          position
          state
          enqueuedAt
          estimatedTimeToMerge
          headCommit {
            oid
            statusCheckRollup {
              state
              contexts(first: 100) { totalCount }
            }
          }
          pullRequest { number }
        }
      }
    }
  }
}' > server/tests/fixtures/merge_queue_kindash.json
```

- [ ] **Step 2: Inspect & record the confirmed shape**

Confirm which fields are present and the `state` enum values seen (`QUEUED` / `AWAITING_CHECKS` / `MERGEABLE` / `UNMERGEABLE` / `LOCKED`). Note in the spike doc: whether `statusCheckRollup.contexts.totalCount` and a passed-count are obtainable (decides the `building.percent` source), and whether `estimatedTimeToMerge` is populated. If a field is absent, record the substitute. **The captured JSON is the fixture Task 2's parser tests load** — so it must be a real response (even an empty-queue one is acceptable; if the queue is empty when you run it, also hand-add a second fixture `merge_queue_synthetic.json` with 3-4 entries matching the confirmed shape, including one `UNMERGEABLE` and one failing rollup).

- [ ] **Step 3: Commit**

```bash
git add server/tests/fixtures/merge_queue_kindash.json docs/superpowers/spikes/2026-06-30-merge-queue-query.md
git commit -m "spike(pipeline): capture real mergeQueue.entries shape (M-Track-10)"
```

---

## Task 2: Query builder + parser (`server/github/merge_queue.py`)

**Files:**
- Create: `server/github/merge_queue.py`
- Test: `server/tests/test_merge_queue_parse.py`

**Interfaces:**
- Produces:
  - `QueueEntry(pr_number:int, position:int|None, state:str, enqueued_at:int|None, batch_head_oid:str|None, group_checks:str|None, checks_total:int|None, checks_done:int|None, eta_seconds:int|None)` — frozen dataclass.
  - `build_merge_queue_query(repos: list[str]) -> str`
  - `parse_merge_queue_response(response: dict, repos: list[str]) -> dict[str, tuple[QueueEntry, ...]]`
- Consumes: the Task 1 fixture for tests.

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_merge_queue_parse.py
import json
from pathlib import Path
from server.github.merge_queue import build_merge_queue_query, parse_merge_queue_response, QueueEntry

def test_build_query_aliases_one_block_per_repo():
    q = build_merge_queue_query(["cairnea/KinDash", "acme/web"])
    assert q.count("mergeQueue") == 2
    assert "r0: repository(owner: \"cairnea\", name: \"KinDash\")" in q
    assert "r1: repository(owner: \"acme\", name: \"web\")" in q

def test_build_query_empty_repos_returns_empty_string():
    assert build_merge_queue_query([]) == ""

def test_parse_maps_entries_per_repo():
    resp = {"data": {"r0": {"mergeQueue": {"entries": {"nodes": [
        {"position": 1, "state": "AWAITING_CHECKS", "enqueuedAt": "2026-06-30T10:00:00Z",
         "estimatedTimeToMerge": 720,
         "headCommit": {"oid": "abc", "statusCheckRollup": {"state": "PENDING",
            "contexts": {"totalCount": 4, "nodes": [
                {"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "SUCCESS"},
                {"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "SKIPPED"},
                {"__typename": "CheckRun", "status": "IN_PROGRESS", "conclusion": None},
                {"__typename": "StatusContext", "state": "PENDING"}]}}},
         "pullRequest": {"number": 10824}},
        {"position": 2, "state": "UNMERGEABLE", "enqueuedAt": "2026-06-30T09:30:00Z",
         "estimatedTimeToMerge": None,
         "headCommit": {"oid": "def", "statusCheckRollup": {"state": "FAILURE",
            "contexts": {"totalCount": 4, "nodes": [
                {"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "FAILURE"}]}}},
         "pullRequest": {"number": 10805}},
    ]}}}}}
    out = parse_merge_queue_response(resp, ["cairnea/KinDash"])
    entries = out["cairnea/KinDash"]
    assert entries[0] == QueueEntry(
        pr_number=10824, position=1, state="AWAITING_CHECKS",
        enqueued_at=1751277600, batch_head_oid="abc",
        group_checks="pending", checks_total=4, checks_done=2, eta_seconds=720)
    assert entries[1].state == "UNMERGEABLE"
    assert entries[1].group_checks == "failing"
    assert entries[1].checks_done == 1

def test_parse_real_fixture_does_not_raise():
    fix = json.loads(Path("server/tests/fixtures/merge_queue_kindash.json").read_text())
    out = parse_merge_queue_response(fix, ["cairnea/KinDash"])
    assert isinstance(out["cairnea/KinDash"], tuple)

def test_parse_null_rollup_is_unknown():
    # synthetic fixture (Task 1) covers UNMERGEABLE + null rollup + a failing batch.
    fix = json.loads(Path("server/tests/fixtures/merge_queue_synthetic.json").read_text())
    by_num = {e.pr_number: e for e in parse_merge_queue_response(fix, ["cairnea/KinDash"])["cairnea/KinDash"]}
    assert by_num[10805].group_checks is None      # statusCheckRollup: null → unknown
    assert by_num[10805].checks_total is None
    assert by_num[10823].group_checks == "failing"
    assert by_num[10824].checks_done == 2          # 2 COMPLETED of 4
```

- [ ] **Step 2: Run it to verify it fails**

Run: `pytest server/tests/test_merge_queue_parse.py -v`
Expected: FAIL with `ModuleNotFoundError: server.github.merge_queue`.

- [ ] **Step 3: Implement the module**

```python
# server/github/merge_queue.py
"""Pure helpers for the repo-level merge queue: GraphQL builder + response parser.

Mirrors the style of server/github/pr_status.py (build_*_query / parse_*_response).
No I/O — the poller runs the query via gh and feeds the response here.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class QueueEntry:
    pr_number: int
    position: int | None
    state: str                 # QUEUED | AWAITING_CHECKS | MERGEABLE | UNMERGEABLE | LOCKED
    enqueued_at: int | None     # epoch seconds
    batch_head_oid: str | None  # speculative merge commit = batch identity
    group_checks: str | None    # 'passing' | 'failing' | 'pending'
    checks_total: int | None
    checks_done: int | None
    eta_seconds: int | None


def _alias(i: int) -> str:
    return f"r{i}"


def build_merge_queue_query(repos: list[str]) -> str:
    if not repos:
        return ""
    blocks = []
    for i, repo in enumerate(repos):
        owner, _, name = repo.partition("/")
        blocks.append(
            f"{_alias(i)}: repository(owner: {json.dumps(owner)}, name: {json.dumps(name)}) {{\n"
            f"  mergeQueue {{ entries(first: 50) {{ nodes {{\n"
            f"    position state enqueuedAt estimatedTimeToMerge\n"
            f"    headCommit {{ oid statusCheckRollup {{ state contexts(first: 100) {{\n"
            f"      totalCount nodes {{ __typename ... on CheckRun {{ status }} ... on StatusContext {{ state }} }}\n"
            f"    }} }} }} }}\n"
            f"    pullRequest {{ number }}\n"
            f"  }} }} }}\n"
            f"}}"
        )
    return "query {\n" + "\n".join(blocks) + "\n}"


def _epoch(iso: str | None) -> int | None:
    if not iso:
        return None
    return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())


def _group_checks(rollup_state: str | None) -> str | None:
    if rollup_state in ("SUCCESS", "EXPECTED"):
        return "passing"
    if rollup_state in ("FAILURE", "ERROR"):
        return "failing"
    if rollup_state in ("PENDING", "IN_PROGRESS", "QUEUED"):
        return "pending"
    return None


_TERMINAL_STATUS_STATES = {"SUCCESS", "FAILURE", "ERROR", "EXPECTED"}


def _checks_done(rollup: dict) -> int | None:
    """Completed-context count = the progress numerator (Task 1 spike: GitHub gives
    per-context status, not a passed-count). CheckRun done when status==COMPLETED;
    StatusContext done when its state is terminal."""
    nodes = ((rollup.get("contexts") or {}).get("nodes"))
    if not nodes:
        return None
    done = 0
    for c in nodes:
        if c.get("__typename") == "CheckRun":
            if c.get("status") == "COMPLETED":
                done += 1
        elif c.get("state") in _TERMINAL_STATUS_STATES:   # StatusContext
            done += 1
    return done


def _entry(node: dict) -> QueueEntry | None:
    pr = node.get("pullRequest") or {}
    number = pr.get("number")
    if number is None:
        return None
    head = node.get("headCommit") or {}
    rollup = head.get("statusCheckRollup") or {}    # may be null on UNMERGEABLE entries
    total = ((rollup.get("contexts") or {}).get("totalCount"))
    return QueueEntry(
        pr_number=int(number),
        position=node.get("position"),
        state=node.get("state", ""),
        enqueued_at=_epoch(node.get("enqueuedAt")),
        batch_head_oid=head.get("oid"),
        group_checks=_group_checks(rollup.get("state")),
        checks_total=total,
        checks_done=_checks_done(rollup),
        eta_seconds=node.get("estimatedTimeToMerge"),
    )


def parse_merge_queue_response(response: dict, repos: list[str]) -> dict[str, tuple[QueueEntry, ...]]:
    data = (response or {}).get("data") or {}
    out: dict[str, tuple[QueueEntry, ...]] = {}
    for i, repo in enumerate(repos):
        node = data.get(_alias(i)) or {}
        mq = node.get("mergeQueue") or {}
        nodes = ((mq.get("entries") or {}).get("nodes")) or []
        entries = tuple(e for e in (_entry(n) for n in nodes if n) if e is not None)
        out[repo] = entries
    return out
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest server/tests/test_merge_queue_parse.py -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add server/github/merge_queue.py server/tests/test_merge_queue_parse.py
git commit -m "feat(pipeline): merge-queue GraphQL builder + parser (M-Track-10)"
```

---

## Task 3: `MergeTrainState` dataclasses + wire shape (`server/pipeline/merge_train.py`)

**Files:**
- Create: `server/pipeline/merge_train.py`
- Test: `server/tests/test_merge_train_shape.py`

**Interfaces:**
- Produces:
  - `Observation(captured_at:int, entries:tuple[QueueEntry, ...])`
  - `TrainBuild(pr_number:int, percent:int|None, eta_seconds:int|None)` with `to_dict`/`from_dict`
  - `MergeTrainState(...)` (fields below) with `to_dict`/`from_dict`

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_merge_train_shape.py
from server.pipeline.merge_train import MergeTrainState, TrainBuild

def test_to_dict_exact_shape():
    st = MergeTrainState(
        repo="cairnea/KinDash", depth=5, trains_per_hour=0.8, batch_success_pct=38.0,
        ejects_24h=46, oldest_wait_seconds=5760, failing=(10823,),
        building=TrainBuild(pr_number=10824, percent=47, eta_seconds=720),
        blocked=(10805, 10799, 10814), awaiting_qa=1, awaiting_prod=42,
        merges_next=TrainBuild(pr_number=10824, percent=47, eta_seconds=720),
    )
    assert st.to_dict() == {
        "repo": "cairnea/KinDash", "depth": 5, "trains_per_hour": 0.8,
        "batch_success_pct": 38.0, "ejects_24h": 46, "oldest_wait_seconds": 5760,
        "failing": [10823],
        "building": {"pr_number": 10824, "percent": 47, "eta_seconds": 720},
        "blocked": [10805, 10799, 10814], "awaiting_qa": 1, "awaiting_prod": 42,
        "merges_next": {"pr_number": 10824, "percent": 47, "eta_seconds": 720},
    }

def test_from_dict_round_trips():
    st = MergeTrainState(
        repo="r", depth=0, trains_per_hour=None, batch_success_pct=None, ejects_24h=0,
        oldest_wait_seconds=None, failing=(), building=None, blocked=(),
        awaiting_qa=0, awaiting_prod=0, merges_next=None)
    assert MergeTrainState.from_dict(st.to_dict()) == st
```

- [ ] **Step 2: Run it to verify it fails**

Run: `pytest server/tests/test_merge_train_shape.py -v`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Implement the dataclasses**

```python
# server/pipeline/merge_train.py  (dataclasses only; derivation added in Tasks 4-5)
from __future__ import annotations

from dataclasses import dataclass

from server.github.merge_queue import QueueEntry


@dataclass(frozen=True)
class Observation:
    captured_at: int
    entries: tuple[QueueEntry, ...]


@dataclass(frozen=True)
class TrainBuild:
    pr_number: int
    percent: int | None
    eta_seconds: int | None

    def to_dict(self) -> dict:
        return {"pr_number": self.pr_number, "percent": self.percent, "eta_seconds": self.eta_seconds}

    @classmethod
    def from_dict(cls, d: dict) -> "TrainBuild":
        return cls(pr_number=d["pr_number"], percent=d.get("percent"), eta_seconds=d.get("eta_seconds"))


@dataclass(frozen=True)
class MergeTrainState:
    repo: str
    depth: int
    trains_per_hour: float | None
    batch_success_pct: float | None
    ejects_24h: int
    oldest_wait_seconds: int | None
    failing: tuple[int, ...]
    building: TrainBuild | None
    blocked: tuple[int, ...]
    awaiting_qa: int
    awaiting_prod: int
    merges_next: TrainBuild | None

    def is_idle(self) -> bool:
        """No queue activity and no recent history → the widget renders nothing."""
        return (
            self.depth == 0 and self.ejects_24h == 0 and not self.failing
            and not self.blocked and not self.building
            and not self.trains_per_hour
        )

    def to_dict(self) -> dict:
        return {
            "repo": self.repo, "depth": self.depth,
            "trains_per_hour": self.trains_per_hour,
            "batch_success_pct": self.batch_success_pct, "ejects_24h": self.ejects_24h,
            "oldest_wait_seconds": self.oldest_wait_seconds,
            "failing": list(self.failing),
            "building": self.building.to_dict() if self.building else None,
            "blocked": list(self.blocked),
            "awaiting_qa": self.awaiting_qa, "awaiting_prod": self.awaiting_prod,
            "merges_next": self.merges_next.to_dict() if self.merges_next else None,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "MergeTrainState":
        b = d.get("building")
        n = d.get("merges_next")
        return cls(
            repo=d["repo"], depth=d["depth"], trains_per_hour=d.get("trains_per_hour"),
            batch_success_pct=d.get("batch_success_pct"), ejects_24h=d.get("ejects_24h", 0),
            oldest_wait_seconds=d.get("oldest_wait_seconds"),
            failing=tuple(d.get("failing", [])),
            building=TrainBuild.from_dict(b) if b else None,
            blocked=tuple(d.get("blocked", [])),
            awaiting_qa=d.get("awaiting_qa", 0), awaiting_prod=d.get("awaiting_prod", 0),
            merges_next=TrainBuild.from_dict(n) if n else None,
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest server/tests/test_merge_train_shape.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add server/pipeline/merge_train.py server/tests/test_merge_train_shape.py
git commit -m "feat(pipeline): MergeTrainState wire shape (M-Track-10)"
```

---

## Task 4: Live-segment derivation

Derive depth / oldest-wait / failing / building / blocked / merges-next from the **latest** observation.

**Files:**
- Modify: `server/pipeline/merge_train.py`
- Test: `server/tests/test_merge_train_live.py`

**Interfaces:**
- Produces: `derive_live(latest: Observation, now: int) -> dict` returning keys
  `depth, oldest_wait_seconds, failing, building, blocked, merges_next`.

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_merge_train_live.py
from server.github.merge_queue import QueueEntry
from server.pipeline.merge_train import Observation, TrainBuild, derive_live

def _e(num, pos, state, checks, oid="h", enq=0, total=4, eta=None):
    return QueueEntry(pr_number=num, position=pos, state=state, enqueued_at=enq,
                      batch_head_oid=oid, group_checks=checks, checks_total=total,
                      checks_done=None, eta_seconds=eta)

def test_live_segments():
    now = 10_000
    latest = Observation(captured_at=now, entries=(
        _e(10824, 1, "AWAITING_CHECKS", "pending", oid="a", enq=now - 5760, eta=720),
        _e(10823, 2, "AWAITING_CHECKS", "failing", oid="b", enq=now - 100),
        _e(10805, 3, "UNMERGEABLE", None, oid="c", enq=now - 200),
    ))
    out = derive_live(latest, now)
    assert out["depth"] == 3
    assert out["oldest_wait_seconds"] == 5760            # min(enqueued_at)
    assert out["failing"] == (10823,)
    assert out["blocked"] == (10805,)
    assert out["merges_next"] == TrainBuild(10824, None, 720)   # head of queue
    assert out["building"] == TrainBuild(10824, None, 720)      # head batch is running

def test_live_building_none_when_head_not_running():
    now = 5
    latest = Observation(captured_at=now, entries=(
        _e(1, 1, "MERGEABLE", "passing", eta=0),
    ))
    out = derive_live(latest, now)
    assert out["building"] is None

def test_live_empty_observation():
    out = derive_live(Observation(captured_at=1, entries=()), now=1)
    assert out == {"depth": 0, "oldest_wait_seconds": None, "failing": (),
                   "building": None, "blocked": (), "merges_next": None}
```

- [ ] **Step 2: Run it to verify it fails**

Run: `pytest server/tests/test_merge_train_live.py -v`
Expected: FAIL with `ImportError: cannot import name 'derive_live'`.

- [ ] **Step 3: Implement `derive_live`**

Append to `server/pipeline/merge_train.py`:

```python
_BIG = 1 << 30


def _percent(e: "QueueEntry") -> int | None:
    if e.checks_total and e.checks_done is not None and e.checks_total > 0:
        return round(100 * e.checks_done / e.checks_total)
    return None


def _build(e: "QueueEntry") -> "TrainBuild":
    return TrainBuild(pr_number=e.pr_number, percent=_percent(e), eta_seconds=e.eta_seconds)


def derive_live(latest: "Observation", now: int) -> dict:
    entries = latest.entries
    enq = [e.enqueued_at for e in entries if e.enqueued_at is not None]
    head = min(
        entries,
        key=lambda e: e.position if e.position is not None else _BIG,
        default=None,
    ) if entries else None
    building = _build(head) if (head and head.group_checks == "pending") else None
    return {
        "depth": len(entries),
        "oldest_wait_seconds": (now - min(enq)) if enq else None,
        "failing": tuple(e.pr_number for e in entries if e.group_checks == "failing"),
        "blocked": tuple(e.pr_number for e in entries if e.state == "UNMERGEABLE"),
        "building": building,
        "merges_next": _build(head) if head else None,
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest server/tests/test_merge_train_live.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add server/pipeline/merge_train.py server/tests/test_merge_train_live.py
git commit -m "feat(pipeline): live merge-train segment derivation (M-Track-10)"
```

---

## Task 5: Historical-metric derivation + `derive_merge_train_state`

Diff consecutive observations across the 24h window to compute ejects/24h, batch-success %, and trains/hr; then assemble the full `MergeTrainState`.

**Files:**
- Modify: `server/pipeline/merge_train.py`
- Test: `server/tests/test_merge_train_history.py`

**Interfaces:**
- Produces:
  - `derive_history(observations: list[Observation], now: int, window_seconds: int = 86400) -> dict` → keys `ejects_24h, batch_success_pct, trains_per_hour`.
  - `derive_merge_train_state(repo: str, observations: list[Observation], now: int, awaiting_qa: int, awaiting_prod: int, window_seconds: int = 86400) -> MergeTrainState` — the public entry point used by the orchestrator.

**Definitions (documented, pure over observations):** a **batch** = PRs sharing a `batch_head_oid`. A PR present in observation N−1 and absent in N has **departed**; classify by its last-seen entry — `state == UNMERGEABLE` or `group_checks == "failing"` → **eject** (and its batch **failed**); `group_checks == "passing"` → its batch **merged**; otherwise ambiguous, ignored. A batch that ever failed is counted failed (failed wins). `ejects_24h` = eject departures in window; `batch_success_pct` = merged ÷ (merged+failed) × 100; `trains_per_hour` = completed batches ÷ window-span-hours.

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_merge_train_history.py
from server.github.merge_queue import QueueEntry
from server.pipeline.merge_train import Observation, derive_history, derive_merge_train_state

def _e(num, checks, state="AWAITING_CHECKS", oid="h"):
    return QueueEntry(pr_number=num, position=1, state=state, enqueued_at=0,
                      batch_head_oid=oid, group_checks=checks, checks_total=4,
                      checks_done=None, eta_seconds=None)

def test_eject_when_pr_departs_with_failing_checks():
    obs = [
        Observation(0, (_e(1, "failing", oid="a"), _e(2, "pending", oid="b"))),
        Observation(60, (_e(2, "pending", oid="b"),)),  # PR 1 departed while failing → eject
    ]
    h = derive_history(obs, now=60)
    assert h["ejects_24h"] == 1

def test_merged_batch_vs_failed_batch():
    obs = [
        Observation(0, (_e(1, "passing", oid="ok"), _e(2, "failing", oid="bad"))),
        Observation(60, ()),  # both departed: 'ok' merged, 'bad' ejected/failed
    ]
    h = derive_history(obs, now=60)
    assert h["ejects_24h"] == 1
    assert h["batch_success_pct"] == 50.0      # 1 merged / (1 merged + 1 failed)

def test_pr_still_present_is_not_a_departure():
    obs = [Observation(0, (_e(1, "pending"),)), Observation(60, (_e(1, "pending"),))]
    h = derive_history(obs, now=60)
    assert h["ejects_24h"] == 0
    assert h["batch_success_pct"] is None      # no completed batches

def test_window_excludes_old_observations():
    obs = [
        Observation(0, (_e(1, "passing", oid="old"),)),
        Observation(100, ()),                          # departure at t=100
    ]
    # now far ahead; cutoff drops both -> no history
    h = derive_history(obs, now=1_000_000, window_seconds=86400)
    assert h["ejects_24h"] == 0

def test_derive_full_state_idle_when_empty():
    st = derive_merge_train_state("r", [], now=10, awaiting_qa=0, awaiting_prod=0)
    assert st.depth == 0
    assert st.is_idle()

def test_derive_full_state_carries_backlog_and_live():
    obs = [Observation(10, (_e(99, "pending", state="AWAITING_CHECKS", oid="z"),))]
    st = derive_merge_train_state("r", obs, now=10, awaiting_qa=1, awaiting_prod=42)
    assert st.depth == 1
    assert st.awaiting_qa == 1 and st.awaiting_prod == 42
    assert st.merges_next is not None and st.merges_next.pr_number == 99
```

- [ ] **Step 2: Run it to verify it fails**

Run: `pytest server/tests/test_merge_train_history.py -v`
Expected: FAIL with `ImportError`.

- [ ] **Step 3: Implement history derivation + the assembler**

Append to `server/pipeline/merge_train.py`:

```python
def derive_history(observations: list["Observation"], now: int, window_seconds: int = 86400) -> dict:
    cutoff = now - window_seconds
    obs = sorted((o for o in observations if o.captured_at >= cutoff), key=lambda o: o.captured_at)
    ejects = 0
    merged: set[str] = set()
    failed: set[str] = set()
    for prev, cur in zip(obs, obs[1:]):
        present = {e.pr_number for e in cur.entries}
        for e in prev.entries:
            if e.pr_number in present:
                continue
            if e.state == "UNMERGEABLE" or e.group_checks == "failing":
                ejects += 1
                if e.batch_head_oid:
                    failed.add(e.batch_head_oid)
            elif e.group_checks == "passing" and e.batch_head_oid:
                merged.add(e.batch_head_oid)
    merged -= failed                       # a batch that ever failed is counted failed
    completed = len(merged) + len(failed)
    success_pct = (len(merged) / completed * 100) if completed else None
    span_h = ((obs[-1].captured_at - obs[0].captured_at) / 3600) if len(obs) >= 2 else 0.0
    trains_per_hour = (completed / span_h) if (completed and span_h > 0) else None
    return {"ejects_24h": ejects, "batch_success_pct": success_pct,
            "trains_per_hour": trains_per_hour}


def derive_merge_train_state(
    repo: str, observations: list["Observation"], now: int,
    awaiting_qa: int, awaiting_prod: int, window_seconds: int = 86400,
) -> MergeTrainState:
    latest = max(observations, key=lambda o: o.captured_at, default=None)
    live = derive_live(latest, now) if latest else {
        "depth": 0, "oldest_wait_seconds": None, "failing": (), "building": None,
        "blocked": (), "merges_next": None}
    hist = derive_history(observations, now, window_seconds)
    return MergeTrainState(
        repo=repo, depth=live["depth"], trains_per_hour=hist["trains_per_hour"],
        batch_success_pct=hist["batch_success_pct"], ejects_24h=hist["ejects_24h"],
        oldest_wait_seconds=live["oldest_wait_seconds"], failing=live["failing"],
        building=live["building"], blocked=live["blocked"],
        awaiting_qa=awaiting_qa, awaiting_prod=awaiting_prod,
        merges_next=live["merges_next"],
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest server/tests/test_merge_train_history.py server/tests/test_merge_train_live.py server/tests/test_merge_train_shape.py -v`
Expected: PASS (all).

- [ ] **Step 5: Commit**

```bash
git add server/pipeline/merge_train.py server/tests/test_merge_train_history.py
git commit -m "feat(pipeline): historical merge-train metrics + state assembler (M-Track-10)"
```

---

## Task 6: DB model + async store (`server/pipeline/merge_queue_store.py`)

**Files:**
- Modify: `server/db/models.py` (add `MergeQueueObservation`)
- Create: `server/pipeline/merge_queue_store.py`
- Test: `server/tests/test_merge_queue_store.py`

**Interfaces:**
- Produces:
  - `MergeQueueObservation` SQLAlchemy model (table `merge_queue_observations`), **all non-key columns nullable**.
  - `async record(repo, captured_at, entries) -> None`
  - `async window(repo, since_epoch) -> list[Observation]` (grouped by `captured_at`, ascending)
  - `async prune(older_than_epoch) -> int`
- Consumes: `server.db.db_session` (async context manager), `QueueEntry`, `Observation`.

- [ ] **Step 1: Add the model**

In `server/db/models.py`, after the existing models, add (mirror the `Mapped`/`mapped_column` style of `Session`):

```python
class MergeQueueObservation(Base):
    __tablename__ = "merge_queue_observations"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    repo: Mapped[str] = mapped_column(String, nullable=False, index=True)
    captured_at: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    pr_number: Mapped[int] = mapped_column(Integer, nullable=False)
    position: Mapped[int | None] = mapped_column(Integer, nullable=True)
    state: Mapped[str | None] = mapped_column(String, nullable=True)
    enqueued_at: Mapped[int | None] = mapped_column(Integer, nullable=True)
    batch_head_oid: Mapped[str | None] = mapped_column(String, nullable=True)
    group_checks: Mapped[str | None] = mapped_column(String, nullable=True)
    checks_total: Mapped[int | None] = mapped_column(Integer, nullable=True)
    checks_done: Mapped[int | None] = mapped_column(Integer, nullable=True)
    eta_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
```

Ensure `Integer` is imported in that file's SQLAlchemy import line (it imports `String` already — add `Integer` if missing).

- [ ] **Step 2: Write the failing test**

> **Convention:** DB tests in this repo do **not** use async fixtures. They wrap an async
> body in `asyncio.run(...)` inside a sync `tmp_path` test and call `init_db(db_url=...)`
> first (see `server/tests/test_app_rehydrate_on_startup.py`). The store reaches the global
> `_session_factory` that `init_db` sets, via the `db_session()` export.

```python
# server/tests/test_merge_queue_store.py
import asyncio
from server.db import init_db
from server.github.merge_queue import QueueEntry
from server.pipeline import merge_queue_store as store

def _e(num, oid="h", checks="pending"):
    return QueueEntry(pr_number=num, position=1, state="AWAITING_CHECKS", enqueued_at=0,
                      batch_head_oid=oid, group_checks=checks, checks_total=4,
                      checks_done=None, eta_seconds=None)

def test_record_then_window_round_trips(tmp_path):
    async def body():
        await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path}/t.db")
        await store.record("r", 100, (_e(1), _e(2, oid="b")))
        await store.record("r", 200, (_e(1),))
        win = await store.window("r", since_epoch=0)
        assert [o.captured_at for o in win] == [100, 200]      # ascending
        assert {e.pr_number for e in win[0].entries} == {1, 2}
        assert win[0].entries[0].batch_head_oid in {"h", "b"}
    asyncio.run(body())

def test_window_filters_by_since(tmp_path):
    async def body():
        await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path}/t.db")
        await store.record("r", 100, (_e(1),))
        await store.record("r", 500, (_e(1),))
        win = await store.window("r", since_epoch=300)
        assert [o.captured_at for o in win] == [500]
    asyncio.run(body())

def test_prune_drops_old_rows(tmp_path):
    async def body():
        await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path}/t.db")
        await store.record("r", 100, (_e(1),))
        await store.record("r", 9000, (_e(1),))
        deleted = await store.prune(older_than_epoch=1000)
        assert deleted == 1
        assert [o.captured_at for o in await store.window("r", 0)] == [9000]
    asyncio.run(body())
```

- [ ] **Step 3: Run it to verify it fails**

Run: `pytest server/tests/test_merge_queue_store.py -v`
Expected: FAIL with `ModuleNotFoundError: server.pipeline.merge_queue_store`.

- [ ] **Step 4: Implement the store**

```python
# server/pipeline/merge_queue_store.py
"""Async read/write/prune over merge_queue_observations (Approach A: raw snapshots).

Each poll writes one row per queued PR. The 24h window is read back and grouped into
Observation objects for the pure derivation in merge_train.py.
"""
from __future__ import annotations

from sqlalchemy import delete, select

from server.db import db_session
from server.db.models import MergeQueueObservation
from server.github.merge_queue import QueueEntry
from server.pipeline.merge_train import Observation


async def record(repo: str, captured_at: int, entries: tuple[QueueEntry, ...]) -> None:
    async with db_session() as s:
        for e in entries:
            s.add(MergeQueueObservation(
                repo=repo, captured_at=captured_at, pr_number=e.pr_number,
                position=e.position, state=e.state, enqueued_at=e.enqueued_at,
                batch_head_oid=e.batch_head_oid, group_checks=e.group_checks,
                checks_total=e.checks_total, checks_done=e.checks_done,
                eta_seconds=e.eta_seconds))
        await s.commit()


def _entry(row: MergeQueueObservation) -> QueueEntry:
    return QueueEntry(
        pr_number=row.pr_number, position=row.position, state=row.state or "",
        enqueued_at=row.enqueued_at, batch_head_oid=row.batch_head_oid,
        group_checks=row.group_checks, checks_total=row.checks_total,
        checks_done=row.checks_done, eta_seconds=row.eta_seconds)


async def window(repo: str, since_epoch: int) -> list[Observation]:
    async with db_session() as s:
        rows = (await s.execute(
            select(MergeQueueObservation)
            .where(MergeQueueObservation.repo == repo,
                   MergeQueueObservation.captured_at >= since_epoch)
            .order_by(MergeQueueObservation.captured_at)
        )).scalars().all()
    grouped: dict[int, list[QueueEntry]] = {}
    for row in rows:
        grouped.setdefault(row.captured_at, []).append(_entry(row))
    return [Observation(captured_at=t, entries=tuple(es)) for t, es in sorted(grouped.items())]


async def prune(older_than_epoch: int) -> int:
    async with db_session() as s:
        result = await s.execute(
            delete(MergeQueueObservation)
            .where(MergeQueueObservation.captured_at < older_than_epoch))
        await s.commit()
        return result.rowcount or 0
```

> If `db_session` is exported differently (e.g. `get_db`), match the existing import in `server/db/__init__.py`. Confirm the async context-manager name before implementing.

- [ ] **Step 5: Run tests to verify they pass, then commit**

Run: `pytest server/tests/test_merge_queue_store.py -v`
Expected: PASS (3 tests).

```bash
git add server/db/models.py server/pipeline/merge_queue_store.py server/tests/test_merge_queue_store.py
git commit -m "feat(pipeline): persist merge-queue observations (M-Track-10)"
```

---

## Task 7: Snapshot step in the poller (`server/github/pr_population.py`)

**Files:**
- Modify: `server/github/pr_population.py`
- Test: `server/tests/test_pr_population_merge_queue.py`

**Interfaces:**
- `PrPopulationPoller.__init__` gains two keyword params:
  - `merge_queue_query_fn: Callable[[list[str]], dict] | None = None`
  - `merge_queue_sink: Callable[[dict[str, tuple[QueueEntry, ...]], int], Awaitable[None]] | None = None`
- `refresh_once` calls them best-effort after the population refresh.

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_pr_population_merge_queue.py
from datetime import datetime, timezone
import pytest
from server.github.pr_population import PrPopulationPoller
from server.github.merge_queue import QueueEntry

_FIXED_NOW = datetime(2026, 6, 30, 12, 0, 0, tzinfo=timezone.utc)  # epoch 1751284800

@pytest.mark.asyncio
async def test_refresh_snapshots_merge_queue():
    captured = {}
    def qfn(repo):  # population query — return empty population
        return {"data": {"repo": {"pullRequests": {"nodes": []}, "deployments": {"nodes": []}}}}
    def mqfn(repos):
        assert repos == ["acme/web"]
        return {"data": {"r0": {"mergeQueue": {"entries": {"nodes": [
            {"position": 1, "state": "AWAITING_CHECKS", "enqueuedAt": "2026-06-30T10:00:00Z",
             "estimatedTimeToMerge": 60,
             "headCommit": {"oid": "x", "statusCheckRollup": {"state": "PENDING",
                "contexts": {"totalCount": 2, "nodes": [
                    {"__typename": "CheckRun", "status": "COMPLETED"},
                    {"__typename": "CheckRun", "status": "IN_PROGRESS"}]}}},
             "pullRequest": {"number": 7}}]}}}}}
    async def sink(by_repo, captured_at):
        captured["by_repo"] = by_repo
        captured["at"] = captured_at
    p = PrPopulationPoller(query_fn=qfn, merge_queue_query_fn=mqfn, merge_queue_sink=sink,
                           now_fn=lambda: _FIXED_NOW)
    await p.refresh_once(["acme/web"])
    assert captured["by_repo"]["acme/web"][0] == QueueEntry(
        pr_number=7, position=1, state="AWAITING_CHECKS", enqueued_at=1751277600,
        batch_head_oid="x", group_checks="pending", checks_total=2, checks_done=1,
        eta_seconds=60)
    assert captured["at"] == 1751284800

@pytest.mark.asyncio
async def test_refresh_ok_when_merge_queue_fns_absent():
    def qfn(repo):
        return {"data": {"repo": {"pullRequests": {"nodes": []}, "deployments": {"nodes": []}}}}
    p = PrPopulationPoller(query_fn=qfn)         # no merge-queue wiring
    assert await p.refresh_once(["acme/web"]) is True
```

- [ ] **Step 2: Run it to verify it fails**

Run: `pytest server/tests/test_pr_population_merge_queue.py -v`
Expected: FAIL — `TypeError: __init__() got an unexpected keyword 'merge_queue_query_fn'`.

- [ ] **Step 3: Implement the snapshot step**

In `server/github/pr_population.py`:

1. Import at top: `from .merge_queue import parse_merge_queue_response, QueueEntry`
2. Add the two params to `__init__` (after `now_fn`) and store them:

```python
        merge_queue_query_fn: Callable[[list[str]], dict] | None = None,
        merge_queue_sink: Callable[..., Awaitable[None]] | None = None,
```
```python
        self._merge_queue_query_fn = merge_queue_query_fn
        self._merge_queue_sink = merge_queue_sink
```

3. In `refresh_once`, after the `_enrich_with_eta` block and before `self._cache.update(new_cache)`, add:

```python
        if self._merge_queue_query_fn and self._merge_queue_sink:
            repos_for_mq = list(new_cache.keys()) or repos
            try:
                resp = await asyncio.to_thread(self._merge_queue_query_fn, repos_for_mq)
                by_repo = parse_merge_queue_response(resp, repos_for_mq)
                captured_at = int(self._now_fn().timestamp())
                await self._merge_queue_sink(by_repo, captured_at)
            except Exception:
                log.warning("pr_population.merge_queue_failed", exc_info=True)
```

- [ ] **Step 4: Run tests to verify they pass, then commit**

Run: `pytest server/tests/test_pr_population_merge_queue.py server/tests/test_pr_population.py -v`
Expected: PASS (new tests pass; existing population tests still green).

```bash
git add server/github/pr_population.py server/tests/test_pr_population_merge_queue.py
git commit -m "feat(pipeline): poller snapshots the merge queue each sweep (M-Track-10)"
```

---

## Task 8: Orchestrator accessor + API endpoint

Wire the poller's merge-queue query-fn + sink at construction, add `all_merge_trains()`, and expose `GET /api/pipeline/trains`.

**Files:**
- Modify: `server/sessions/orchestrator.py`
- Modify: `server/api/pipeline.py`
- Test: `server/tests/test_merge_trains_route.py`

**Interfaces:**
- `SessionOrchestrator.all_merge_trains() -> Awaitable[dict[str, MergeTrainState]]`
- `GET /api/pipeline/trains` → `{repo: MergeTrainState.to_dict()}`

- [ ] **Step 1: Write the failing route test**

```python
# server/tests/test_merge_trains_route.py
import os
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from server.api.pipeline import repos_router
from server.pipeline.merge_train import MergeTrainState, TrainBuild

class _FakeSessions:
    async def all_merge_trains(self):
        return {"cairnea/KinDash": MergeTrainState(
            repo="cairnea/KinDash", depth=5, trains_per_hour=0.8, batch_success_pct=38.0,
            ejects_24h=46, oldest_wait_seconds=5760, failing=(10823,),
            building=TrainBuild(10824, None, 720), blocked=(10805,),
            awaiting_qa=1, awaiting_prod=42, merges_next=TrainBuild(10824, None, 720))}

@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("CCSWITCH_DEV", "1")
    monkeypatch.setenv("CCSWITCH_TAILNET_ALLOWLIST", "")
    app = FastAPI()
    app.include_router(repos_router)
    app.state.sessions = _FakeSessions()
    return TestClient(app)

def test_trains_endpoint_returns_serialized_state(client):
    r = client.get("/api/pipeline/trains")
    assert r.status_code == 200
    body = r.json()
    assert body["cairnea/KinDash"]["depth"] == 5
    assert body["cairnea/KinDash"]["building"]["pr_number"] == 10824
    assert body["cairnea/KinDash"]["blocked"] == [10805]
```

> Match the auth-bypass pattern used by `server/tests/test_pipeline_route.py`; if that test injects identity differently (e.g. dependency override), copy that exact mechanism instead of the env vars above.

- [ ] **Step 2: Run it to verify it fails**

Run: `pytest server/tests/test_merge_trains_route.py -v`
Expected: FAIL with 404 (route absent).

- [ ] **Step 3: Add the endpoint**

In `server/api/pipeline.py`, add to `repos_router`:

```python
@repos_router.get("/trains")
async def list_merge_trains(
    request: Request,
    _identity: str = Depends(current_identity),
) -> dict:
    orch = request.app.state.sessions
    trains = await orch.all_merge_trains()
    return {repo: st.to_dict() for repo, st in trains.items()}
```

- [ ] **Step 4: Wire the orchestrator**

In `server/sessions/orchestrator.py`:

1. At the poller construction (currently `self._pr_population = PrPopulationPoller()`), pass the query-fn + sink:

```python
        from server.github.merge_queue import build_merge_queue_query
        from server.pipeline import merge_queue_store
        from ccswitch import gitrepos
        from server.github import app_auth

        def _merge_queue_query(repos: list[str]) -> dict:
            query = build_merge_queue_query(repos)
            if not query:
                return {}
            # one owner token suffices when all repos share an owner; group like _default_eta_query
            token = app_auth.token_for_owner(repos[0].split("/", 1)[0]) if repos else None
            proc = gitrepos._run_gh(["api", "graphql", "-f", f"query={query}"], token=token)
            if proc.returncode != 0:
                raise gitrepos.GhError(proc.stderr.strip() or "gh api graphql (merge queue) failed")
            return json.loads(proc.stdout or "{}")

        async def _merge_queue_sink(by_repo, captured_at: int) -> None:
            for repo, entries in by_repo.items():
                await merge_queue_store.record(repo, captured_at, entries)
            await merge_queue_store.prune(older_than_epoch=captured_at - 36 * 3600)

        self._pr_population = PrPopulationPoller(
            merge_queue_query_fn=_merge_queue_query,
            merge_queue_sink=_merge_queue_sink,
        )
```

> If watched repos can span multiple owners, replace `_merge_queue_query` with the per-owner grouping used by `_default_eta_query` in `pr_population.py` (merge `data` blocks with remapped aliases). Single-owner is the common case; note the limitation if you keep it simple.

2. Add the accessor (near `all_repos_stage_models`):

```python
    async def all_merge_trains(self) -> "dict[str, MergeTrainState]":
        from server.pipeline.merge_train import derive_merge_train_state
        from server.pipeline import merge_queue_store
        import time as _time
        from server.github.pr_status import PipelinePr  # noqa: F401 (population already cached)
        now = int(_time.time())
        since = now - 86400
        out: dict = {}
        pop = self._pr_population.all()
        for repo in pop.keys():
            obs = await merge_queue_store.window(repo, since)
            qa, prod = self._deploy_backlog(pop[repo])
            out[repo] = derive_merge_train_state(repo, obs, now, qa, prod)
        return out

    @staticmethod
    def _deploy_backlog(prs) -> "tuple[int, int]":
        """Reuse existing PR data (single source of truth) — do not re-query GitHub.
        awaiting_prod = merged & not in production; awaiting_qa = merged & not deployed anywhere."""
        awaiting_prod = sum(1 for p in prs if p.merged and "production" not in p.deployed_envs)
        awaiting_qa = sum(1 for p in prs if p.merged and not p.deployed_envs)
        return awaiting_qa, awaiting_prod
```

Add `from server.pipeline.merge_train import MergeTrainState` to the `TYPE_CHECKING` block, and ensure `json` is imported in `orchestrator.py` (the query-fn uses it).

- [ ] **Step 5: Run tests, then commit**

Run: `pytest server/tests/test_merge_trains_route.py server/tests/test_pipeline_route.py -v`
Expected: PASS.

```bash
git add server/sessions/orchestrator.py server/api/pipeline.py server/tests/test_merge_trains_route.py
git commit -m "feat(pipeline): /api/pipeline/trains endpoint + orchestrator accessor (M-Track-10)"
```

---

## Task 9: Frontend types + hook

**Files:**
- Modify: `web/src/api/types.ts`
- Modify: `web/src/api/hooks.ts`
- Test: `web/src/api/mergeTrains.test.ts`

**Interfaces:**
- `MergeTrainState`, `TrainBuild` TS interfaces mirroring the backend `to_dict`.
- `QK.pipelineTrains` query key.
- `useMergeTrains(): UseQueryResult<Record<string, MergeTrainState>>` — fetches the full map (one request, like `useStageModels`).

- [ ] **Step 1: Write the failing test**

```typescript
// web/src/api/mergeTrains.test.ts
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { renderHook, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import * as client from './client'
import { useMergeTrains } from './hooks'

function wrapper({ children }: { children: React.ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>
}

describe('useMergeTrains', () => {
  beforeEach(() => vi.restoreAllMocks())
  it('fetches the per-repo train map from /pipeline/trains', async () => {
    const spy = vi.spyOn(client.api, 'get').mockResolvedValue({
      'cairnea/KinDash': { repo: 'cairnea/KinDash', depth: 5, ejects_24h: 46 },
    })
    const { result } = renderHook(() => useMergeTrains(), { wrapper })
    await waitFor(() => expect(result.current.data).toBeDefined())
    expect(spy).toHaveBeenCalledWith('/pipeline/trains')
    expect(result.current.data!['cairnea/KinDash'].depth).toBe(5)
  })
})
```

> Match the import style of the existing `web/src/api/hooks.test.ts` (how it mocks `./client` and builds the wrapper); adjust if that file mocks the module rather than spying on `api.get`.

- [ ] **Step 2: Run it to verify it fails**

Run (in `web/`): `vitest run src/api/mergeTrains.test.ts`
Expected: FAIL — `useMergeTrains` is not exported.

- [ ] **Step 3: Add the types**

In `web/src/api/types.ts`, after `PipelinePr` (~line 198):

```typescript
export interface TrainBuild {
  pr_number: number
  percent: number | null
  eta_seconds: number | null
}

export interface MergeTrainState {
  repo: string
  depth: number
  trains_per_hour: number | null
  batch_success_pct: number | null
  ejects_24h: number
  oldest_wait_seconds: number | null
  failing: number[]
  building: TrainBuild | null
  blocked: number[]
  awaiting_qa: number
  awaiting_prod: number
  merges_next: TrainBuild | null
}
```

- [ ] **Step 4: Add the query key + hook**

In `web/src/api/hooks.ts`: add to `QK` (after `pipelineRepos`):

```typescript
  pipelineTrains: ['pipeline-trains'] as const,
```

Add the hook (after `useStageModels`), importing `MergeTrainState` from `./types`:

```typescript
export function useMergeTrains() {
  return useQuery<Record<string, MergeTrainState>>({
    queryKey: QK.pipelineTrains,
    queryFn: () => api.get<Record<string, MergeTrainState>>('/pipeline/trains'),
    staleTime: 10_000,
    refetchInterval: 15_000,
  })
}
```

- [ ] **Step 5: Run test + typecheck, then commit**

Run (in `web/`): `vitest run src/api/mergeTrains.test.ts && tsc --noEmit`
Expected: PASS, no type errors.

```bash
git add web/src/api/types.ts web/src/api/hooks.ts web/src/api/mergeTrains.test.ts
git commit -m "feat(pipeline): useMergeTrains hook + MergeTrainState types (M-Track-10)"
```

---

## Task 10: `MergeTrainWidget` component

**Files:**
- Create: `web/src/components/MergeTrainWidget.tsx`
- Test: `web/src/components/MergeTrainWidget.test.tsx`

**Interfaces:**
- `export function MergeTrainWidget({ state }: { state: MergeTrainState | undefined }): JSX.Element | null`
- Renders nothing when `state` is undefined or idle (`depth === 0 && ejects_24h === 0 && trains_per_hour falsy && !failing.length && !blocked.length && !building`).

- [ ] **Step 1: Write the failing test**

```tsx
// web/src/components/MergeTrainWidget.test.tsx
import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { MergeTrainWidget } from './MergeTrainWidget'
import type { MergeTrainState } from '../api/types'

const active: MergeTrainState = {
  repo: 'cairnea/KinDash', depth: 5, trains_per_hour: 0.8, batch_success_pct: 38,
  ejects_24h: 46, oldest_wait_seconds: 5760, failing: [10823],
  building: { pr_number: 10824, percent: 47, eta_seconds: 720 }, blocked: [10805, 10799],
  awaiting_qa: 1, awaiting_prod: 42, merges_next: { pr_number: 10824, percent: 47, eta_seconds: 720 },
}

describe('MergeTrainWidget', () => {
  it('renders health line, segments and merges-next for an active queue', () => {
    render(<MergeTrainWidget state={active} />)
    expect(screen.getByText(/depth 5/)).toBeInTheDocument()
    expect(screen.getByText(/46 ejects/)).toBeInTheDocument()
    expect(screen.getByText(/#10823/)).toBeInTheDocument()        // failing
    expect(screen.getByText(/#10805/)).toBeInTheDocument()        // blocked
    expect(screen.getByText(/Merges next/)).toBeInTheDocument()
  })

  it('renders nothing for an idle repo', () => {
    const idle: MergeTrainState = { ...active, depth: 0, ejects_24h: 0, trains_per_hour: 0,
      failing: [], blocked: [], building: null }
    const { container } = render(<MergeTrainWidget state={idle} />)
    expect(container).toBeEmptyDOMElement()
  })

  it('renders nothing when state is undefined', () => {
    const { container } = render(<MergeTrainWidget state={undefined} />)
    expect(container).toBeEmptyDOMElement()
  })
})
```

- [ ] **Step 2: Run it to verify it fails**

Run (in `web/`): `vitest run src/components/MergeTrainWidget.test.tsx`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement the component**

```tsx
// web/src/components/MergeTrainWidget.tsx
import type { MergeTrainState, TrainBuild } from '../api/types'

function fmtDuration(secs: number | null): string {
  if (secs == null) return ''
  const m = Math.round(secs / 60)
  if (m < 60) return `~${m}m`
  const h = Math.floor(m / 60)
  return `~${h}h ${m % 60}m`
}

function buildLabel(b: TrainBuild): string {
  const pct = b.percent != null ? `${b.percent}% ` : ''
  const eta = b.eta_seconds != null ? `${fmtDuration(b.eta_seconds)} left` : ''
  return `#${b.pr_number} ${pct}${eta}`.trim()
}

function isIdle(s: MergeTrainState): boolean {
  return s.depth === 0 && s.ejects_24h === 0 && !s.trains_per_hour
    && s.failing.length === 0 && s.blocked.length === 0 && !s.building
}

export function MergeTrainWidget({ state }: { state: MergeTrainState | undefined }) {
  if (!state || isIdle(state)) return null
  const { depth, trains_per_hour, batch_success_pct, ejects_24h, oldest_wait_seconds } = state
  return (
    <div className="mb-2 rounded border border-border/60 bg-muted/30 p-2 text-xs">
      <div className="flex flex-wrap gap-x-3 gap-y-1 text-muted-foreground">
        <span>depth {depth}</span>
        {trains_per_hour != null && <span>{trains_per_hour.toFixed(1)} trains/hr</span>}
        {batch_success_pct != null && <span>{Math.round(batch_success_pct)}% batch success</span>}
        <span>{ejects_24h} ejects 24h</span>
        {oldest_wait_seconds != null && <span>oldest wait {fmtDuration(oldest_wait_seconds)}</span>}
      </div>
      <div className="mt-1 flex flex-wrap gap-2">
        {state.failing.length > 0 && (
          <span className="rounded border border-red-300 bg-red-50 px-2 py-1 text-red-700">
            ✗ failing {state.failing.map((n) => `#${n}`).join(' ')}
          </span>
        )}
        {state.building && (
          <span className="rounded border border-blue-300 bg-blue-50 px-2 py-1 text-blue-700">
            ▶ group {buildLabel(state.building)}
          </span>
        )}
        {state.blocked.length > 0 && (
          <span className="rounded border border-dashed border-amber-400 bg-amber-50 px-2 py-1 text-amber-700">
            ⊘ blocked behind conflict {state.blocked.map((n) => `#${n}`).join(' ')}
          </span>
        )}
      </div>
      <div className="mt-1 text-muted-foreground">
        Deploy backlog: {state.awaiting_qa} awaiting QA · {state.awaiting_prod} awaiting prod
      </div>
      {state.merges_next && (
        <div className="mt-1 text-foreground">
          ⤳ Merges next: <strong>#{state.merges_next.pr_number}</strong>
          {state.merges_next.percent != null && ` building ${state.merges_next.percent}%`}
          {state.merges_next.eta_seconds != null && ` · ${fmtDuration(state.merges_next.eta_seconds)}`}
        </div>
      )}
    </div>
  )
}
```

> Use the project's existing Tailwind tokens — if `border-border`, `bg-muted` etc. aren't the local convention, match the classes used by `StageTiles.tsx`/`PipelineView.tsx`. Keep the literal copy ("depth", "trains/hr", "blocked behind conflict", "Merges next") matching the Plotroom reference.

- [ ] **Step 4: Run tests + lint, then commit**

Run (in `web/`): `vitest run src/components/MergeTrainWidget.test.tsx && eslint src/components/MergeTrainWidget.tsx`
Expected: PASS (3 tests), no lint errors.

```bash
git add web/src/components/MergeTrainWidget.tsx web/src/components/MergeTrainWidget.test.tsx
git commit -m "feat(pipeline): MergeTrainWidget component (M-Track-10)"
```

---

## Task 11: Mount the widget in the Pipeline board

**Files:**
- Modify: `web/src/components/PipelineView.tsx`
- Test: `web/src/components/PipelineView.mergeTrain.test.tsx`

**Interfaces:**
- Consumes: `useMergeTrains()` (Task 9), `MergeTrainWidget` (Task 10).
- The widget mounts directly under each repo's `GroupHeader`, before the PR `<ul>`.

- [ ] **Step 1: Write the failing test**

```tsx
// web/src/components/PipelineView.mergeTrain.test.tsx
import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import * as hooks from '../api/hooks'
import { PipelineView } from './PipelineView'

function renderView() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={qc}><PipelineView /></QueryClientProvider>)
}

describe('PipelineView merge-train widget', () => {
  it('shows the train widget for a repo with active queue data', () => {
    vi.spyOn(hooks, 'useAllPrs').mockReturnValue({ data: [
      { number: 1, repo: 'cairnea/KinDash', title: 't', state: 'open', checks: 'pending',
        url: '', node_states: [], session: null } as any,
    ] } as any)
    vi.spyOn(hooks, 'useStageModels').mockReturnValue({ data: {} } as any)
    vi.spyOn(hooks, 'useMergeTrains').mockReturnValue({ data: {
      'cairnea/KinDash': { repo: 'cairnea/KinDash', depth: 5, trains_per_hour: 0.8,
        batch_success_pct: 38, ejects_24h: 46, oldest_wait_seconds: 5760, failing: [10823],
        building: null, blocked: [], awaiting_qa: 1, awaiting_prod: 42, merges_next: null },
    } } as any)
    renderView()
    expect(screen.getByText(/depth 5/)).toBeInTheDocument()
    expect(screen.getByText(/#10823/)).toBeInTheDocument()
  })
})
```

> Adjust the mocked `useAllPrs`/`useStageModels` return shapes to whatever `PipelineView` actually destructures (check the top of `PipelineView`/`PipelineBoard`). The point of the test is only that `MergeTrainWidget` is wired to `useMergeTrains()` per repo.

- [ ] **Step 2: Run it to verify it fails**

Run (in `web/`): `vitest run src/components/PipelineView.mergeTrain.test.tsx`
Expected: FAIL — "depth 5" not found (widget not mounted).

- [ ] **Step 3: Wire the widget**

In `web/src/components/PipelineView.tsx`:

1. Import: `import { MergeTrainWidget } from './MergeTrainWidget'` and add `useMergeTrains` to the existing `../api/hooks` import.
2. In the component that owns the repo loop (`PipelineBoard` is fed data by `PipelineView`; pass trains down or call the hook where the repo map is built). Call `const { data: trains } = useMergeTrains()` alongside the existing `useAllPrs()`/`useStageModels()` calls, and pass `trains` into `PipelineBoard` as a prop (mirror how `stageModels` is threaded).
3. In the repo `.map((repo) => ...)` render, immediately after `<GroupHeader .../>` and before `{!collapsed[repo] && (<ul>...`:

```tsx
            {!collapsed[repo] && <MergeTrainWidget state={trains?.[repo]} />}
```

- [ ] **Step 4: Run test + full web checks, then commit**

Run (in `web/`): `vitest run src/components/PipelineView.mergeTrain.test.tsx && tsc --noEmit && eslint src/components/PipelineView.tsx`
Expected: PASS, no type/lint errors.

```bash
git add web/src/components/PipelineView.tsx web/src/components/PipelineView.mergeTrain.test.tsx
git commit -m "feat(pipeline): mount merge-train widget per repo (M-Track-10)"
```

---

## Final verification (before PR)

- [ ] Server suite: `pytest server/tests/ -q` (via the main checkout's `.venv` per Global Constraints) — all green.
- [ ] Web: in `web/` → `vitest run`, `tsc --noEmit`, `eslint .` — all green.
- [ ] Manual smoke (optional, against a repo with a live merge queue): start the app, open the Pipeline tab, confirm the train widget appears atop a queued repo and collapses to nothing on idle repos.
- [ ] Open the PR per the CLAUDE.md completion workflow (branch off `origin/master`, non-draft, `gh pr merge --squash --auto`).

---

## Self-Review Notes (author)

- **Spec coverage:** §2 query→Tasks 1-2; §3 SQLite model→Task 6; §4 live metrics→Task 4, historical→Task 5; §5 API+types→Tasks 8-9; §6 widget→Tasks 10-11; §7 error-handling (best-effort sink, idle-collapse)→Tasks 7/10, testing throughout.
- **Known approximation (documented, not a gap):** merged-vs-ejected classification is heuristic (last-seen batch checks), and ejects occurring entirely between two polls are not observed — acceptable at snapshot resolution; called out in Task 5's definitions and the spike doc.
- **Type consistency:** `QueueEntry` (9 fields) is identical across Tasks 2/6/7; `MergeTrainState`/`TrainBuild` field names match across backend `to_dict` (Task 3) and TS interfaces (Task 9) and the widget (Task 10).
- **Verify-before-build:** `db_session` export name, the `test_pipeline_route.py` auth-bypass mechanism, the exact `PipelineView` hook-destructure shapes, and the live `mergeQueue` field availability are each flagged inline to confirm against real code/API before implementing the dependent step.

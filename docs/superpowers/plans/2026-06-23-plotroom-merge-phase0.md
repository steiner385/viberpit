# Plotroom Merge — Phase 0 Implementation Plan

> **⚠️ Built & merged (#258), with the parity model reworked vs this plan.** Tasks 1–2
> (extend `PullRequestInfo`) shipped as written. The harness (Tasks 3–6) shipped but the
> live oracle forced a redesign: number-keyed candidate, intersection-based `ok`, coverage
> narrowed to `{number,url,checks}`. See the superseded note in
> `…/specs/2026-06-23-plotroom-merge-phase0-parity-foundation-design.md`. The engine port
> beyond Phase 0 is decomposed in `2026-06-24-m5-phase1-engine-port.md` (+ `…-m5-1c-eta-engine.md`).

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a parity harness that diffs ccswitch's GitHub PR/CI snapshot against a live Plotroom `GET /api/state`, and extend ccswitch's existing PR snapshot to carry the fields that harness compares — with zero user-visible change.

**Architecture:** Extend the existing pure `server/github/pr_status.py` (query + parse + `PullRequestInfo`) additively so each PR snapshot also carries `head_sha`, `is_draft`, `merged`, `updated_at`, and a `checks_detail` list. Add a self-contained, dev-only `server/github/parity/` package (coverage map → Plotroom adapter → diff harness → CLI) that treats Plotroom's `/api/state` as the oracle and ccswitch's snapshot as the candidate.

**Tech Stack:** Python 3, pytest (+ `pytest-asyncio`), `urllib`/`gh` for I/O (no new deps), existing `server.github` module.

## Global Constraints

- **No new runtime dependency.** Use the stdlib (`urllib.request`, `json`) and the existing `gh` path. (Constitution VII: modern vetted deps — but the floor here is "nothing new for Phase 0".)
- **Additive only / nullable columns.** No DB schema change in Phase 0. Any new dataclass field has a default so existing constructors and the daemon→edge RPC keep working.
- **Single source of truth (Constitution VI).** Do NOT create a second poller or a parallel snapshot type. Extend `PullRequestInfo` + `pr_status.py` in place.
- **Verify against the real thing (Constitution III).** The harness compares against Plotroom's live `/api/state`, never a hand-mocked expectation, in real runs.
- **Parity tolerances are explicit and documented in `normalize.py`** — timestamps at second granularity; floats (later phases) `±max(2s, 5%)`; never bare float equality.
- **GraphQL string-injection safety:** every owner/name/branch is `json.dumps`-encoded into the query (the existing `build_pr_query` pattern — preserve it).
- **Run backend tests with the prebuilt env + worktree src on the path:** `PYTHONPATH=<worktree>/src <main-checkout>/.venv/bin/python -m pytest server/tests/...` (a fresh worktree can't `uv sync`).

### Deviations from the Phase-0 spec (deliberate, with rationale)

1. **No rename to `PrSnapshot` / `PrSnapshotPoller`; no new `pr_snapshot.py`.** `PullRequestInfo` is an **RPC-serialized type** (`server/sessions/remote.py:95` calls `from_dict`; consumed by `server/api/sessions.py` and 6 test files). Renaming it ripples across the daemon→edge boundary for zero behavior gain and contradicts the phase's "no user-visible change" goal. We extend it in place instead.
2. **`server/github/pr_poller.py` is untouched and NOT deleted.** It caches whatever `parse_pr_response` returns, so the extended fields flow through automatically. There is no duplicate poller to remove — the spec's single-source intent is already met.

These keep the spec's *intent* (one poller, one snapshot type, harness as the oracle) while minimizing churn.

---

### Task 1: Extend `PullRequestInfo` with parity fields (additive, RPC-safe)

**Files:**
- Modify: `server/github/pr_status.py` (the `PullRequestInfo` dataclass + `to_dict`/`from_dict`)
- Test: `server/tests/test_pr_status.py` (extend the existing round-trip test)

**Interfaces:**
- Produces: `PullRequestInfo` now has, in addition to `number, url, state, checks, title, deployed`:
  - `head_sha: str = ""`
  - `is_draft: bool = False`
  - `merged: bool = False`
  - `updated_at: str = ""`  (ISO-8601 UTC)
  - `checks_detail: tuple[CheckDetail, ...] = ()`  where `CheckDetail` is a frozen dataclass `{name: str, conclusion: str}` (Phase-0 covered subset; `status`/timestamps deferred).
- `to_dict`/`from_dict` round-trip every field including `checks_detail` (list of dicts on the wire).

- [ ] **Step 1: Write the failing test** — append to `server/tests/test_pr_status.py`:

```python
from server.github.pr_status import CheckDetail  # add to the existing import block


def test_pull_request_info_parity_fields_roundtrip():
    pr = PullRequestInfo(
        number=9, url="u", state="open", checks="passing", title="t",
        head_sha="abc123", is_draft=False, merged=False,
        updated_at="2026-06-22T10:00:00Z",
        checks_detail=(CheckDetail(name="ci", conclusion="success"),),
    )
    back = PullRequestInfo.from_dict(pr.to_dict())
    assert back == pr
    assert back.head_sha == "abc123"
    assert back.checks_detail[0].name == "ci"
    # Defaults still round-trip (older payloads without the new keys):
    legacy = PullRequestInfo.from_dict(
        {"number": 1, "url": "u", "state": "open", "checks": "none", "title": "t"}
    )
    assert legacy.head_sha == "" and legacy.checks_detail == ()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=$PWD/src .venv/bin/python -m pytest server/tests/test_pr_status.py::test_pull_request_info_parity_fields_roundtrip -v`
Expected: FAIL — `ImportError: cannot import name 'CheckDetail'` (or `TypeError` on unexpected kwargs).

- [ ] **Step 3: Write minimal implementation** — in `server/github/pr_status.py`, add `CheckDetail` and extend `PullRequestInfo`:

```python
@dataclass(frozen=True)
class CheckDetail:
    name: str
    conclusion: str  # 'success' | 'failure' | 'pending' | 'neutral' | 'skipped' | ...

    def to_dict(self) -> dict:
        return {"name": self.name, "conclusion": self.conclusion}

    @classmethod
    def from_dict(cls, d: dict) -> "CheckDetail":
        return cls(name=d.get("name", ""), conclusion=d.get("conclusion", ""))


@dataclass(frozen=True)
class PullRequestInfo:
    number: int
    url: str
    state: str
    checks: str
    title: str
    deployed: bool = False
    head_sha: str = ""
    is_draft: bool = False
    merged: bool = False
    updated_at: str = ""
    checks_detail: tuple[CheckDetail, ...] = ()

    def to_dict(self) -> dict:
        return {
            "number": self.number, "url": self.url, "state": self.state,
            "checks": self.checks, "title": self.title, "deployed": self.deployed,
            "head_sha": self.head_sha, "is_draft": self.is_draft,
            "merged": self.merged, "updated_at": self.updated_at,
            "checks_detail": [c.to_dict() for c in self.checks_detail],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "PullRequestInfo":
        return cls(
            number=d["number"], url=d["url"], state=d["state"],
            checks=d["checks"], title=d.get("title", ""),
            deployed=d.get("deployed", False),
            head_sha=d.get("head_sha", ""), is_draft=d.get("is_draft", False),
            merged=d.get("merged", False), updated_at=d.get("updated_at", ""),
            checks_detail=tuple(CheckDetail.from_dict(c) for c in d.get("checks_detail", [])),
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=$PWD/src .venv/bin/python -m pytest server/tests/test_pr_status.py -v`
Expected: the new test PASSES and the existing `test_from_dict_roundtrips_to_dict` still PASSES (new fields default, so the old 5-arg constructor is unchanged).

- [ ] **Step 5: Commit**

```bash
git add server/github/pr_status.py server/tests/test_pr_status.py
git commit -m "feat(pr): add parity fields (head_sha, is_draft, merged, updated_at, checks_detail) to PullRequestInfo"
```

---

### Task 2: Populate the new fields in the GraphQL query + parse

**Files:**
- Modify: `server/github/pr_status.py` (`build_pr_query`, `_to_info`, add `_checks_detail` helper)
- Test: `server/tests/test_pr_status.py` (new parse test + update one existing equality assertion)

**Interfaces:**
- Consumes: Task 1's extended `PullRequestInfo` + `CheckDetail`.
- Produces: `parse_pr_response` fills `head_sha` (from `headRefOid`), `is_draft` (from `isDraft`), `merged` (state == "merged"), `updated_at` (from `updatedAt`), and `checks_detail` from the rollup `contexts` (CheckRun `name`+`conclusion`; StatusContext `context`+`state`).

- [ ] **Step 1: Write the failing test** — append to `server/tests/test_pr_status.py`:

```python
def test_parse_populates_parity_fields():
    pairs = [("o/r", "br")]
    node = {
        "number": 7, "url": "https://github.com/o/r/pull/7", "title": "PR 7",
        "state": "OPEN", "isDraft": False, "updatedAt": "2026-06-22T10:00:00Z",
        "headRefOid": "deadbeef",
        "commits": {"nodes": [{"commit": {"statusCheckRollup": {
            "state": "PENDING",
            "contexts": {"nodes": [
                {"__typename": "CheckRun", "name": "ci", "conclusion": "SUCCESS"},
                {"__typename": "StatusContext", "context": "deploy", "state": "PENDING"},
            ]},
        }}}]},
    }
    out = parse_pr_response(_resp({"r0": [node]}), pairs)[("o/r", "br")]
    assert out.head_sha == "deadbeef"
    assert out.is_draft is False and out.merged is False
    assert out.updated_at == "2026-06-22T10:00:00Z"
    assert CheckDetail(name="ci", conclusion="success") in out.checks_detail
    assert CheckDetail(name="deploy", conclusion="pending") in out.checks_detail
```

Then **update the existing** `test_parse_open_passing` so its expected literal includes the now-populated fields (the `_pr` helper already supplies `updatedAt`):

```python
def test_parse_open_passing():
    pairs = [("o/r", "br")]
    out = parse_pr_response(_resp({"r0": [_pr(7, "OPEN", rollup="SUCCESS")]}), pairs)
    info = out[("o/r", "br")]
    assert info == PullRequestInfo(
        7, "https://github.com/o/r/pull/7", "open", "passing", "PR 7",
        updated_at="2026-06-22T10:00:00Z",
    )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=$PWD/src .venv/bin/python -m pytest server/tests/test_pr_status.py::test_parse_populates_parity_fields -v`
Expected: FAIL — `head_sha == ""`, `checks_detail == ()` (parse doesn't read the new fields yet).

- [ ] **Step 3: Write minimal implementation** — in `server/github/pr_status.py`:

Extend the `nodes { … }` selection in `build_pr_query` to add `headRefOid` and the rollup `contexts`:

```python
f"    nodes {{ number url title state isDraft updatedAt mergedAt headRefOid\n"
f"      commits(last: 1) {{ nodes {{ commit {{ statusCheckRollup {{ state\n"
f"        contexts(first: 100) {{ nodes {{\n"
f"          __typename\n"
f"          ... on CheckRun {{ name conclusion }}\n"
f"          ... on StatusContext {{ context state }}\n"
f"        }} }}\n"
f"      }} }} }} }}\n"
f"    }}\n"
```

Add a `_checks_detail` helper and wire it into `_to_info`:

```python
_CONCLUSION_MAP = {
    "SUCCESS": "success", "FAILURE": "failure", "ERROR": "failure",
    "PENDING": "pending", "EXPECTED": "pending", "NEUTRAL": "neutral",
    "SKIPPED": "skipped", "CANCELLED": "cancelled", "TIMED_OUT": "failure",
    "ACTION_REQUIRED": "failure", "STARTUP_FAILURE": "failure", "STALE": "neutral",
}


def _checks_detail(pr: dict) -> tuple["CheckDetail", ...]:
    nodes = ((pr.get("commits") or {}).get("nodes")) or []
    if not nodes:
        return ()
    rollup = (nodes[0].get("commit") or {}).get("statusCheckRollup")
    if not rollup:
        return ()
    ctx = ((rollup.get("contexts") or {}).get("nodes")) or []
    out: list[CheckDetail] = []
    for c in ctx:
        if c.get("__typename") == "CheckRun":
            name = c.get("name", "")
            raw = c.get("conclusion") or "PENDING"  # null conclusion = still running
        else:  # StatusContext
            name = c.get("context", "")
            raw = c.get("state", "")
        out.append(CheckDetail(name=name, conclusion=_CONCLUSION_MAP.get(raw, raw.lower())))
    return tuple(out)
```

Update `_to_info`:

```python
def _to_info(pr: dict, active_date: str | None) -> PullRequestInfo:
    state = _classify_state(pr.get("state", ""), bool(pr.get("isDraft")))
    return PullRequestInfo(
        number=pr["number"], url=pr["url"], state=state,
        checks=_classify_checks(_rollup_state(pr)), title=pr.get("title", ""),
        deployed=_is_deployed(pr, state, active_date),
        head_sha=pr.get("headRefOid", ""),
        is_draft=bool(pr.get("isDraft")),
        merged=(state == "merged"),
        updated_at=pr.get("updatedAt", ""),
        checks_detail=_checks_detail(pr),
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH=$PWD/src .venv/bin/python -m pytest server/tests/test_pr_status.py -v`
Expected: all PASS, including the updated `test_parse_open_passing` and the existing query/checks tests (`test_build_query_aliases_each_pair` still finds `statusCheckRollup`).

- [ ] **Step 5: Run the poller test (no change expected, regression guard)**

Run: `PYTHONPATH=$PWD/src .venv/bin/python -m pytest server/tests/test_pr_poller.py -v`
Expected: PASS — the poller transparently caches the richer `PullRequestInfo`.

- [ ] **Step 6: Commit**

```bash
git add server/github/pr_status.py server/tests/test_pr_status.py
git commit -m "feat(pr): parse headRefOid + per-check rollup contexts into snapshot"
```

---

### Task 3: Parity coverage map

**Files:**
- Create: `server/github/parity/__init__.py` (empty)
- Create: `server/github/parity/coverage.py`
- Test: `server/tests/test_parity_coverage.py`

**Interfaces:**
- Produces: `PHASE_FIELDS: dict[int, frozenset[str]]` and `covered_fields(phase: int) -> frozenset[str]` (raises `KeyError` for an unknown phase). Phase 0 set = `{"number","url","state","is_draft","merged","head_sha","checks","checks_detail"}`.

- [ ] **Step 1: Write the failing test** — `server/tests/test_parity_coverage.py`:

```python
from server.github.parity.coverage import PHASE_FIELDS, covered_fields


def test_phase0_covers_basic_pr_fields():
    f = covered_fields(0)
    assert {"number", "url", "state", "checks", "checks_detail"} <= f
    # Stage/ETA fields are explicitly NOT covered in phase 0:
    assert "eta_secs" not in f and "stage" not in f


def test_unknown_phase_raises():
    import pytest
    with pytest.raises(KeyError):
        covered_fields(99)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=$PWD/src .venv/bin/python -m pytest server/tests/test_parity_coverage.py -v`
Expected: FAIL — `ModuleNotFoundError: server.github.parity.coverage`.

- [ ] **Step 3: Write minimal implementation** — `server/github/parity/coverage.py`:

```python
"""Per-phase parity field map: which fields the harness is allowed to compare.

Comparing a field the Python side does not yet produce (stage, ETA, deploy) would
be noise, so each migration phase widens this set explicitly.
"""
from __future__ import annotations

PHASE_FIELDS: dict[int, frozenset[str]] = {
    0: frozenset(
        {"number", "url", "state", "is_draft", "merged", "head_sha",
         "checks", "checks_detail"}
    ),
}


def covered_fields(phase: int) -> frozenset[str]:
    return PHASE_FIELDS[phase]
```

Also create the empty package marker:

```bash
: > server/github/parity/__init__.py
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=$PWD/src .venv/bin/python -m pytest server/tests/test_parity_coverage.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add server/github/parity/__init__.py server/github/parity/coverage.py server/tests/test_parity_coverage.py
git commit -m "feat(parity): per-phase coverage field map"
```

---

### Task 4: Plotroom `/api/state` → comparable-snapshot adapter

**Files:**
- Create: `server/github/parity/normalize.py`
- Create: `server/tests/fixtures/plotroom_state_sample.json` (a trimmed, real `/api/state` blob — see step 1)
- Test: `server/tests/test_parity_normalize.py`

**Interfaces:**
- Produces:
  - `OraclePr` = frozen dataclass `{repo: str, number: int, branch: str, fields: dict[str, object]}` where `fields` keys are the canonical names matching `PullRequestInfo` (`state`, `is_draft`, `merged`, `head_sha`, `checks`, `checks_detail`, `url`, `number`).
  - `normalize_state(state_json: dict) -> dict[tuple[str, int], OraclePr]` keyed by `(repo, number)`.
  - Module constant `TIMESTAMP_GRANULARITY_SECS = 1` (documented tolerance anchor; timestamps are not in Phase-0 coverage but the constant is referenced by the harness for later phases).

- [ ] **Step 1: Capture a real fixture (one-time, manual).** With a live Plotroom running, save a trimmed snapshot:

```bash
curl -s http://127.0.0.1:4400/api/state | python -m json.tool > server/tests/fixtures/plotroom_state_sample.json
```

Trim it by hand to 2–3 PRs across 1–2 repos so the fixture stays small. (If no live instance is available, hand-author the fixture from the shape documented in Plotroom's `server/api.ts` `/api/state` handler — but a captured one is preferred per Constitution III.)

- [ ] **Step 2: Write the failing test** — `server/tests/test_parity_normalize.py`:

```python
import json
from pathlib import Path

from server.github.parity.normalize import normalize_state, OraclePr

FIXTURE = Path(__file__).parent / "fixtures" / "plotroom_state_sample.json"


def test_normalize_keys_by_repo_and_number():
    state = json.loads(FIXTURE.read_text())
    out = normalize_state(state)
    assert out, "fixture should contain at least one PR"
    (repo, number), pr = next(iter(out.items()))
    assert isinstance(pr, OraclePr)
    assert pr.fields["number"] == number
    assert "state" in pr.fields and "checks" in pr.fields


def test_normalize_maps_state_vocabulary():
    # Plotroom's per-PR shape -> ccswitch vocabulary ('open'/'draft'/'merged'/'closed').
    sample = {"repos": [{"nameWithOwner": "o/r", "prs": [
        {"number": 1, "url": "u", "headRefName": "br", "headSha": "abc",
         "state": "OPEN", "isDraft": True, "checksRollup": "PENDING", "checks": []},
    ]}]}
    out = normalize_state(sample)
    pr = out[("o/r", 1)]
    assert pr.fields["state"] == "draft"     # isDraft wins over OPEN
    assert pr.fields["checks"] == "pending"
    assert pr.branch == "br" and pr.fields["head_sha"] == "abc"
```

- [ ] **Step 3: Run test to verify it fails**

Run: `PYTHONPATH=$PWD/src .venv/bin/python -m pytest server/tests/test_parity_normalize.py -v`
Expected: FAIL — `ModuleNotFoundError: server.github.parity.normalize`.

- [ ] **Step 4: Write minimal implementation** — `server/github/parity/normalize.py`:

```python
"""Adapter: Plotroom GET /api/state -> comparable snapshot in ccswitch vocabulary.

This is the ONE place that encodes Plotroom's JSON shape. A Plotroom schema change
touches only this file. Tolerances are documented here, not scattered in the harness.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# Timestamps (when later phases compare them) are matched at second granularity:
TIMESTAMP_GRANULARITY_SECS = 1

_STATE_MAP = {"OPEN": "open", "MERGED": "merged", "CLOSED": "closed"}
_CHECKS_MAP = {
    "SUCCESS": "passing", "FAILURE": "failing", "ERROR": "failing",
    "PENDING": "pending", "EXPECTED": "pending", None: "none", "": "none",
}


@dataclass(frozen=True)
class OraclePr:
    repo: str
    number: int
    branch: str
    fields: dict = field(default_factory=dict)


def _map_state(raw_state: str, is_draft: bool) -> str:
    if raw_state == "OPEN" and is_draft:
        return "draft"
    return _STATE_MAP.get(raw_state, raw_state.lower())


def normalize_state(state_json: dict) -> dict[tuple[str, int], OraclePr]:
    out: dict[tuple[str, int], OraclePr] = {}
    for repo in (state_json or {}).get("repos", []):
        name = repo.get("nameWithOwner") or repo.get("repo") or ""
        for pr in repo.get("prs", []):
            number = pr.get("number")
            if number is None:
                continue
            is_draft = bool(pr.get("isDraft"))
            checks_detail = [
                {"name": c.get("name", ""), "conclusion": (c.get("conclusion") or "pending").lower()}
                for c in pr.get("checks", [])
            ]
            fields = {
                "number": number,
                "url": pr.get("url", ""),
                "state": _map_state(pr.get("state", ""), is_draft),
                "is_draft": is_draft,
                "merged": pr.get("state") == "MERGED",
                "head_sha": pr.get("headSha", ""),
                "checks": _CHECKS_MAP.get(pr.get("checksRollup"), "none"),
                "checks_detail": checks_detail,
            }
            out[(name, number)] = OraclePr(
                repo=name, number=number, branch=pr.get("headRefName", ""), fields=fields
            )
    return out
```

> **Implementer note:** the exact Plotroom key names (`repos[].prs[].state`, `headRefName`, `headSha`, `checksRollup`, `checks[]`) must be confirmed against the captured fixture in step 1 and Plotroom's `server/api.ts` / `server/types.ts`. Adjust the `.get(...)` keys to match the real shape; the golden-fixture test (next) is what proves you got it right.

- [ ] **Step 5: Run tests to verify they pass**

Run: `PYTHONPATH=$PWD/src .venv/bin/python -m pytest server/tests/test_parity_normalize.py -v`
Expected: PASS. If `test_normalize_keys_by_repo_and_number` fails on the real fixture, reconcile the `.get(...)` keys with the captured JSON (do not change the test's assertions — fix the adapter).

- [ ] **Step 6: Commit**

```bash
git add server/github/parity/normalize.py server/tests/test_parity_normalize.py server/tests/fixtures/plotroom_state_sample.json
git commit -m "feat(parity): Plotroom /api/state adapter to comparable snapshot"
```

---

### Task 5: The diff harness

**Files:**
- Create: `server/github/parity/harness.py`
- Test: `server/tests/test_parity_harness.py`

**Interfaces:**
- Consumes: `covered_fields` (Task 3), `normalize_state`/`OraclePr` (Task 4), `PullRequestInfo` (Tasks 1–2).
- Produces:
  - `diff_snapshots(candidate, oracle, phase) -> ParityReport` — pure, no I/O. `candidate: dict[tuple[str,int], PullRequestInfo]`, `oracle: dict[tuple[str,int], OraclePr]`.
  - `ParityReport` frozen dataclass: `matched: int`, `mismatches: list[FieldMismatch]`, `oracle_only: list[tuple[str,int]]`, `candidate_only: list[tuple[str,int]]`; property `ok: bool` (True iff no mismatches and no oracle/candidate-only in the covered set).
  - `FieldMismatch` frozen dataclass: `key: tuple[str,int]`, `field: str`, `candidate`, `oracle`.

- [ ] **Step 1: Write the failing test** — `server/tests/test_parity_harness.py`:

```python
from server.github.pr_status import PullRequestInfo, CheckDetail
from server.github.parity.normalize import OraclePr
from server.github.parity.harness import diff_snapshots


def _cand(**kw):
    base = dict(number=1, url="u", state="open", checks="passing", title="t",
                is_draft=False, merged=False, head_sha="abc",
                checks_detail=(CheckDetail("ci", "success"),))
    base.update(kw)
    return PullRequestInfo(**base)


def _oracle(**kw):
    fields = dict(number=1, url="u", state="open", checks="passing",
                  is_draft=False, merged=False, head_sha="abc",
                  checks_detail=[{"name": "ci", "conclusion": "success"}])
    fields.update(kw)
    return OraclePr(repo="o/r", number=fields["number"], branch="br", fields=fields)


def test_identical_snapshots_match():
    rep = diff_snapshots({("o/r", 1): _cand()}, {("o/r", 1): _oracle()}, phase=0)
    assert rep.ok and rep.matched == 1 and not rep.mismatches


def test_field_mismatch_detected():
    rep = diff_snapshots({("o/r", 1): _cand(state="open")},
                         {("o/r", 1): _oracle(state="merged")}, phase=0)
    assert not rep.ok
    assert any(m.field == "state" for m in rep.mismatches)


def test_uncovered_field_ignored():
    # 'title' is NOT in PHASE_FIELDS[0]; a difference there must NOT fail parity.
    rep = diff_snapshots({("o/r", 1): _cand(title="A")},
                         {("o/r", 1): _oracle()}, phase=0)  # oracle has no title
    assert rep.ok


def test_oracle_only_and_candidate_only():
    rep = diff_snapshots({("o/r", 1): _cand()},
                         {("o/r", 2): _oracle(number=2)}, phase=0)
    assert ("o/r", 1) in rep.candidate_only
    assert ("o/r", 2) in rep.oracle_only
    assert not rep.ok
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=$PWD/src .venv/bin/python -m pytest server/tests/test_parity_harness.py -v`
Expected: FAIL — `ModuleNotFoundError: server.github.parity.harness`.

- [ ] **Step 3: Write minimal implementation** — `server/github/parity/harness.py`:

```python
"""Diff ccswitch's PR snapshot (candidate) against Plotroom's /api/state (oracle).

`diff_snapshots` is pure. The I/O wrapper (fetch + compute candidate) lives in the
CLI (__main__) so this module stays unit-testable.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from server.github.pr_status import PullRequestInfo
from server.github.parity.coverage import covered_fields
from server.github.parity.normalize import OraclePr


@dataclass(frozen=True)
class FieldMismatch:
    key: tuple[str, int]
    field: str
    candidate: object
    oracle: object


@dataclass(frozen=True)
class ParityReport:
    matched: int
    mismatches: list = field(default_factory=list)
    oracle_only: list = field(default_factory=list)
    candidate_only: list = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.mismatches and not self.oracle_only and not self.candidate_only


def _candidate_value(pr: PullRequestInfo, name: str):
    if name == "checks_detail":
        return sorted((c.name, c.conclusion) for c in pr.checks_detail)
    return getattr(pr, name)


def _oracle_value(pr: OraclePr, name: str):
    if name == "checks_detail":
        return sorted((c["name"], c["conclusion"]) for c in pr.fields.get("checks_detail", []))
    return pr.fields.get(name)


def diff_snapshots(
    candidate: dict[tuple[str, int], PullRequestInfo],
    oracle: dict[tuple[str, int], OraclePr],
    phase: int,
) -> ParityReport:
    fields = covered_fields(phase)
    mismatches: list[FieldMismatch] = []
    matched = 0
    shared = candidate.keys() & oracle.keys()
    for key in shared:
        cand, orc = candidate[key], oracle[key]
        row_ok = True
        for name in fields:
            # Compare only when the ORACLE actually carries the field (oracle-missing
            # is treated as "not under test for this row", not a mismatch).
            if name not in orc.fields:
                continue
            cv, ov = _candidate_value(cand, name), _oracle_value(orc, name)
            if cv != ov:
                mismatches.append(FieldMismatch(key=key, field=name, candidate=cv, oracle=ov))
                row_ok = False
        if row_ok:
            matched += 1
    return ParityReport(
        matched=matched,
        mismatches=mismatches,
        oracle_only=sorted(oracle.keys() - candidate.keys()),
        candidate_only=sorted(candidate.keys() - oracle.keys()),
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH=$PWD/src .venv/bin/python -m pytest server/tests/test_parity_harness.py -v`
Expected: PASS (all four).

- [ ] **Step 5: Commit**

```bash
git add server/github/parity/harness.py server/tests/test_parity_harness.py
git commit -m "feat(parity): pure candidate-vs-oracle diff with per-phase coverage"
```

---

### Task 6: CLI entry — fetch Plotroom, compute candidate, report

**Files:**
- Create: `server/github/parity/__main__.py`
- Test: `server/tests/test_parity_cli.py`

**Interfaces:**
- Consumes: everything above.
- Produces:
  - `fetch_oracle(base_url: str, *, opener=urllib.request.urlopen) -> dict` — GET `<base>/api/state`, return parsed JSON; raises `HarnessError` on non-200/transport error (NOT a parity failure).
  - `compute_candidate(pairs, query_fn) -> dict[tuple[str,int], PullRequestInfo]` — one synchronous poller sweep, re-keyed `(repo, number)`.
  - `render_report(report) -> str` — human table.
  - `main(argv) -> int` — exit 0 iff `report.ok`; 2 on `HarnessError`; 1 on parity failure.

- [ ] **Step 1: Write the failing test** — `server/tests/test_parity_cli.py`:

```python
import json
import pytest

from server.github.parity.__main__ import fetch_oracle, render_report, HarnessError
from server.github.parity.harness import ParityReport, FieldMismatch


class _Resp:
    def __init__(self, body, status=200):
        self._body, self.status = body, status
    def read(self): return self._body
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def getcode(self): return self.status


def test_fetch_oracle_parses_json():
    def opener(url, timeout=0):
        assert url.endswith("/api/state")
        return _Resp(json.dumps({"repos": []}).encode())
    assert fetch_oracle("http://127.0.0.1:4400", opener=opener) == {"repos": []}


def test_fetch_oracle_raises_on_error():
    def opener(url, timeout=0):
        raise OSError("connection refused")
    with pytest.raises(HarnessError):
        fetch_oracle("http://127.0.0.1:4400", opener=opener)


def test_render_report_lists_mismatches():
    rep = ParityReport(matched=1, mismatches=[
        FieldMismatch(("o/r", 1), "state", "open", "merged")])
    text = render_report(rep)
    assert "state" in text and "open" in text and "merged" in text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=$PWD/src .venv/bin/python -m pytest server/tests/test_parity_cli.py -v`
Expected: FAIL — `ModuleNotFoundError`.

- [ ] **Step 3: Write minimal implementation** — `server/github/parity/__main__.py`:

```python
"""CLI: python -m server.github.parity --plotroom-url URL --repos a/b,c/d [--phase 0] [--json out.json]

Exit 0 = parity holds; 1 = parity mismatch; 2 = harness error (Plotroom unreachable etc.).
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request

from server.github.pr_poller import _default_query
from server.github.pr_status import build_pr_query, parse_pr_response
from server.github.parity.harness import ParityReport, diff_snapshots
from server.github.parity.normalize import normalize_state


class HarnessError(RuntimeError):
    pass


def fetch_oracle(base_url: str, *, opener=urllib.request.urlopen) -> dict:
    url = base_url.rstrip("/") + "/api/state"
    try:
        with opener(url, timeout=15) as resp:
            code = getattr(resp, "getcode", lambda: 200)()
            if code != 200:
                raise HarnessError(f"Plotroom {url} returned HTTP {code}")
            return json.loads(resp.read() or b"{}")
    except HarnessError:
        raise
    except Exception as exc:  # transport / JSON / timeout
        raise HarnessError(f"could not fetch oracle {url}: {exc}") from exc


def compute_candidate(pairs, query_fn=_default_query):
    """One synchronous sweep; re-key (repo, branch) -> (repo, number)."""
    by_branch = parse_pr_response(query_fn(pairs), pairs)
    return {(repo, info.number): info for (repo, _branch), info in by_branch.items()}


def render_report(report: ParityReport) -> str:
    lines = [f"matched: {report.matched}",
             f"mismatches: {len(report.mismatches)}",
             f"oracle-only: {len(report.oracle_only)}  candidate-only: {len(report.candidate_only)}"]
    for m in report.mismatches:
        lines.append(f"  ✗ {m.key} .{m.field}: candidate={m.candidate!r} oracle={m.oracle!r}")
    for k in report.oracle_only:
        lines.append(f"  ● oracle-only {k}")
    for k in report.candidate_only:
        lines.append(f"  ○ candidate-only {k}")
    lines.append("PARITY OK" if report.ok else "PARITY FAILED")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m server.github.parity")
    ap.add_argument("--plotroom-url", default="http://127.0.0.1:4400")
    ap.add_argument("--repos", required=True, help="comma-separated owner/name list")
    ap.add_argument("--phase", type=int, default=0)
    ap.add_argument("--json", dest="json_out")
    args = ap.parse_args(argv)

    repos = [r.strip() for r in args.repos.split(",") if r.strip()]
    try:
        oracle = normalize_state(fetch_oracle(args.plotroom_url))
    except HarnessError as exc:
        print(f"harness error: {exc}", file=sys.stderr)
        return 2

    # Candidate: query each repo for the branches the oracle knows about.
    pairs = [(orc.repo, orc.branch) for orc in oracle.values() if orc.branch]
    pairs = [p for p in pairs if p[0] in repos] if repos else pairs
    candidate = compute_candidate(pairs)

    report = diff_snapshots(candidate, oracle, phase=args.phase)
    print(render_report(report))
    if args.json_out:
        with open(args.json_out, "w") as fh:
            json.dump({
                "matched": report.matched,
                "mismatches": [vars(m) for m in report.mismatches],
                "oracle_only": report.oracle_only,
                "candidate_only": report.candidate_only,
                "ok": report.ok,
            }, fh, indent=2, default=list)
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH=$PWD/src .venv/bin/python -m pytest server/tests/test_parity_cli.py -v`
Expected: PASS.

- [ ] **Step 5: Lint + full PR-area test sweep**

Run:
```bash
PYTHONPATH=$PWD/src .venv/bin/python -m pytest server/tests/test_pr_status.py server/tests/test_pr_poller.py server/tests/test_parity_coverage.py server/tests/test_parity_normalize.py server/tests/test_parity_harness.py server/tests/test_parity_cli.py server/tests/test_sessions_pr_field.py server/tests/test_remote_orchestrator.py -v
.venv/bin/python -m ruff check server/github/
.venv/bin/python -m mypy server/github/parity/ server/github/pr_status.py
```
Expected: all tests PASS; ruff clean; mypy clean (it skips untyped bodies but flags signature issues).

- [ ] **Step 6: Commit**

```bash
git add server/github/parity/__main__.py server/tests/test_parity_cli.py
git commit -m "feat(parity): CLI — fetch Plotroom oracle, compute candidate, diff + report"
```

---

### Task 7: Manual end-to-end parity run (the phase's acceptance gate)

**Files:** none (operational verification — Constitution III).

**Interfaces:** Consumes the full CLI.

- [ ] **Step 1: Start a live Plotroom instance** (its standalone mode): `pnpm start` in the Plotroom checkout (`http://127.0.0.1:4400`). Ensure `gh auth status` is logged in for ccswitch's side.

- [ ] **Step 2: Run the harness against real repos you actively develop**, e.g.:

```bash
PYTHONPATH=$PWD/src .venv/bin/python -m server.github.parity \
  --plotroom-url http://127.0.0.1:4400 \
  --repos steiner385/cc-account-switcher \
  --json /tmp/parity-phase0.json
```

- [ ] **Step 3: Confirm `PARITY OK`** (exit 0) on the Phase-0 covered fields. If a field mismatches, decide which side is right: if it's an adapter bug, fix `normalize.py` and add the case to `test_parity_normalize.py`; if ccswitch genuinely computes it differently, that's a real finding — record it in the Phase-0 spec's notes and reconcile before declaring Phase 0 done. **Do not loosen the coverage set to make it green.**

- [ ] **Step 4: Commit the captured report as evidence** (optional but recommended):

```bash
mkdir -p docs/superpowers/evidence
cp /tmp/parity-phase0.json docs/superpowers/evidence/2026-06-23-parity-phase0.json
git add docs/superpowers/evidence/2026-06-23-parity-phase0.json
git commit -m "test(parity): record Phase-0 parity-OK evidence against live Plotroom"
```

---

## Self-Review

**1. Spec coverage** (against `2026-06-23-plotroom-merge-phase0-parity-foundation-design.md`):
- Part A data foundation (richer snapshot, additive, absorb poller) → Tasks 1–2 (+ documented deviation: extend in place, no rename; poller untouched because it transparently carries the richer type). ✓
- Part B harness: coverage map → Task 3; normalize adapter + tolerances + golden fixture → Task 4; diff harness → Task 5; CLI with exit codes → Task 6. ✓
- "Harness error ≠ parity mismatch" → Task 6 `HarnessError` + exit 2, tested. ✓
- "Phase-0 covers exactly {…}" → Task 3 `PHASE_FIELDS[0]`, tested incl. uncovered-field-ignored (Task 5). ✓
- "Verify against the real thing" acceptance → Task 7. ✓
- No DB schema change / nullable rule → no columns added; noted in Global Constraints. ✓

**2. Placeholder scan:** No TBD/TODO. The one explicit implementer judgement (exact Plotroom JSON keys) is bounded by the golden-fixture test, which is the proof — not a placeholder.

**3. Type consistency:** `PullRequestInfo`/`CheckDetail` (Tasks 1–2) ↔ `_candidate_value` (Task 5) ↔ `compute_candidate` (Task 6) use the same field names. `OraclePr.fields` keys (Task 4) match `covered_fields` (Task 3) and the oracle side of `diff_snapshots`. `ParityReport.ok` semantics consistent between Task 5 definition and Task 6 exit codes.

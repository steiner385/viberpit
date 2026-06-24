# M3a — Work Dispatcher: top-open-issue proposal (bite-sized)

> REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Branch `feat/m3-work-dispatcher` off master. Backend only.

**Goal:** For an idle/done session, propose the repo's top open GitHub issue as a chip (via the existing `latest_suggestions` surface), so an idle agent can be fed the next backlog task with one approval. Proposal-only — accepting the chip resumes the session with the task text; nothing auto-launches (AUT-04/08).

**Architecture:** A new `server/sessions/dispatch.py` (pure issue-picking + a `gh issue list` fetch) plus a lazy TTL cache + background refresh in the orchestrator (mirroring `_generate_detached_suggestions`: never block the hot `latest_suggestions` path, never call `gh` per request). `latest_suggestions` prepends the cached proposal string for idle/done sessions.

**Tech stack:** Python, `gitrepos._run_gh` (no new dep), pytest.

## Global Constraints
- **Reuse the existing chip surface** (`latest_suggestions` → `latestSuggestions` → Resume/chevron). Do NOT add a new UI or a parallel suggestion store (AUT-08).
- **Proposal, not action:** the proposal is a suggestion string; the session launches only when the user accepts the chip (AUT-04). No auto-resume.
- **No per-request `gh`, no new poller:** cache per-repo with a TTL; refresh in a fire-and-forget task like `_generate_detached_suggestions`. Never block `latest_suggestions`; never raise into it.
- **Single source:** one `gh issue` path in `dispatch.py`; do not scatter issue queries.
- **Whole-tree lint as CI does:** `ruff check src/ server/` (not just changed files — prior CI failures came from test-file lint slipping past per-file checks). Tests via the clone venv + `PYTHONPATH=$PWD/src`.

Test cases reference AUT-04/05/08 in the combined-app user-stories doc.

## Task 1 — `server/sessions/dispatch.py` (pure pick + gh fetch)

**Interfaces:**
- `PRIORITY_LABELS: frozenset[str]` = `{"priority", "p0", "p1", "high-priority", "priority:high"}` (lowercased match).
- `pick_top_issue(issues: list[dict]) -> dict | None` — pure. Sort key: issues whose labels intersect `PRIORITY_LABELS` first, then most-recent `updatedAt` desc, then lowest `number`. Returns None for `[]`.
- `issue_proposal_text(issue: dict) -> str` — `f"Work on issue #{issue['number']}: {issue['title']}"`.
- `fetch_open_issues(repo: str, *, run_gh=gitrepos._run_gh) -> list[dict]` — runs `gh issue list --repo <repo> --state open --json number,title,labels,updatedAt --limit 30`; parse JSON; return `[]` on any error (non-zero rc, bad JSON) — never raises. Normalize each issue's `labels` to a list of lowercased label-name strings for `pick_top_issue`.

- [ ] **Step 1 — failing tests** `server/tests/test_dispatch.py`:
```python
from server.sessions import dispatch

def test_proposal_text():
    assert dispatch.issue_proposal_text({"number": 12, "title": "Fix the thing"}) == "Work on issue #12: Fix the thing"

def test_pick_top_prefers_priority_label():
    issues = [
        {"number": 1, "title": "a", "labels": ["bug"], "updatedAt": "2026-06-24T10:00:00Z"},
        {"number": 2, "title": "b", "labels": ["priority"], "updatedAt": "2026-06-20T10:00:00Z"},
    ]
    assert dispatch.pick_top_issue(issues)["number"] == 2

def test_pick_top_falls_back_to_recency():
    issues = [
        {"number": 1, "title": "a", "labels": [], "updatedAt": "2026-06-20T10:00:00Z"},
        {"number": 2, "title": "b", "labels": [], "updatedAt": "2026-06-24T10:00:00Z"},
    ]
    assert dispatch.pick_top_issue(issues)["number"] == 2

def test_pick_top_empty_is_none():
    assert dispatch.pick_top_issue([]) is None

def test_fetch_open_issues_parses(monkeypatch):
    import subprocess
    def fake_gh(args):
        assert "issue" in args and "list" in args
        return subprocess.CompletedProcess(args, 0, stdout='[{"number":3,"title":"t","labels":[{"name":"Bug"}],"updatedAt":"x"}]', stderr="")
    out = dispatch.fetch_open_issues("o/r", run_gh=fake_gh)
    assert out[0]["number"] == 3 and out[0]["labels"] == ["bug"]

def test_fetch_open_issues_error_is_empty(monkeypatch):
    import subprocess
    def boom(args):
        return subprocess.CompletedProcess(args, 1, stdout="", stderr="gh fail")
    assert dispatch.fetch_open_issues("o/r", run_gh=boom) == []
```
- [ ] **Step 2 — RED.**
- [ ] **Step 3 — implement `dispatch.py`** per the interfaces (label-name normalization: `gh` returns labels as `[{"name": ...}]`; normalize to lowercased names; tolerate already-string labels in tests).
- [ ] **Step 4 — GREEN; ruff whole tree; mypy `server/sessions/dispatch.py`.**
- [ ] **Step 5 — commit** (`feat(dispatch): top-open-issue picker + gh fetch (pure + injectable)`).

## Task 2 — orchestrator: cached proposal + prepend into `latest_suggestions`

**Files:** `server/sessions/orchestrator.py`; test `server/tests/test_orchestrator_dispatch.py`.

**Interfaces:**
- `__init__`: `self._issue_proposals: dict[str, tuple[float, str | None]] = {}` (repo → (monotonic_ts, proposal_or_None)); `self._issue_inflight: set[str] = set()`; TTL const `_ISSUE_TTL_S = 300.0`.
- `_dispatch_proposal(self, rec) -> str | None`: if `not rec.repo` → None. If cached and fresh (`now - ts < TTL`) → return cached proposal. Else if repo not in `_issue_inflight`, schedule `asyncio.create_task(self._refresh_issue_proposal(rec.repo))`; return the stale cached proposal if any, else None. (Mirrors the detached-suggestion lazy pattern.) Use `time.monotonic()` via an injectable `self._clock` if one exists, else `time.monotonic`.
- `async _refresh_issue_proposal(self, repo)`: `try: issues = await asyncio.to_thread(dispatch.fetch_open_issues, repo); top = dispatch.pick_top_issue(issues); text = dispatch.issue_proposal_text(top) if top else None; self._issue_proposals[repo] = (time.monotonic(), text)` `except: store (now, None)` `finally: self._issue_inflight.discard(repo)`. Never raises.
- In `latest_suggestions`, for the **done/idle detached** branch only, prepend the proposal: compute `proposal = self._dispatch_proposal(rec)` and return `_prepend(proposal, base)` at each return point in that branch (the cache-hit list, and the cache-miss `[]`), where `_prepend` puts proposal first iff truthy and not already present. Do NOT touch the live-driver path or the non-done/idle early returns.

- [ ] **Step 1 — failing tests** `server/tests/test_orchestrator_dispatch.py` (use `_make_orch` pattern; monkeypatch `orch_mod.dispatch.fetch_open_issues`):
  - `_dispatch_proposal` returns None for a repo-less record.
  - With a fake `fetch_open_issues` returning one issue: after awaiting `_refresh_issue_proposal("o/r")`, `_dispatch_proposal(rec)` returns `"Work on issue #N: ..."`.
  - TTL: a second `_dispatch_proposal` within TTL does not re-schedule a fetch (monkeypatch/stub the fetch counter).
  - `latest_suggestions` for a detached done session with a repo includes the proposal as the FIRST item (stub the transcript path/signature + `_detached_suggestions` so the SDK branch returns a known base; assert proposal prepended, not duplicated).
- [ ] **Step 2 — RED.**
- [ ] **Step 3 — implement** the cache, refresh, and `latest_suggestions` prepend.
- [ ] **Step 4 — GREEN; ruff whole tree; mypy; rerun `test_suggestion_cache.py`/any latest_suggestions regression + `test_dispatch.py`.**
- [ ] **Step 5 — commit** (`feat(dispatch): propose top open issue to idle/done sessions via the suggestion chip (M3a)`).

## Out of scope (M3b+)
- Review-comment and broken-PR sources (separate slices, same chip surface).
- Ranking beyond the simple priority-label/recency heuristic; collision-avoidance (skip issues already owned by a live session — US-8).
- Embedding an explicit skill invocation (`implement-loop`) in the proposal text — v1 proposes a plain task string; the session/operator drives the skill. Revisit if the bare prompt under-triggers.

## Self-Review
- AUT-04 (propose, not act): proposal is a chip string; accept→resume launches. ✓ AUT-08 (reuse surface): prepends into `latest_suggestions`, no new store. ✓
- No hot-path `gh` / no new poller: lazy TTL cache + bg refresh mirrors `_generate_detached_suggestions`. ✓
- Never raises into `latest_suggestions`; `fetch_open_issues` returns [] on error. ✓
- Types: `pick_top_issue(list)->dict|None`, `issue_proposal_text(dict)->str`, `fetch_open_issues(repo,*,run_gh)->list[dict]`, `_dispatch_proposal(rec)->str|None`. ✓

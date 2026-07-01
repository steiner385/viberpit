# Backlog Runner — Plan B: Selection, Claim & Spawn

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the Backlog Runner's I/O surface — label-gated candidate query, one-shot LLM ranking into a coherent batch, atomic claim/unclaim/give-up with a reconciliation sweep, the batch-aware spawn prompt, and the propose-tap endpoint — behind injectable seams so the whole loop tests with no network and no real sessions.

**Architecture:** These are the seams Plan A's `BacklogRunnerLoop` was built to call: `select_batch`, `claim`, and the propose spawn. The ranking decision is a **pure** `rank_and_batch(candidates, max_issues_per_pr, ranking)`; the ranking itself is a **one-shot model query** (reusing the `generate_title` pattern — not a spawned session) behind an injected seam. All GitHub writes (labels/comments) go through `gitrepos._run_gh` behind a seam. **Task 1 builds the fake-`gh`/fake-LLM/fake-spawn harness.**

**Tech Stack:** Python 3 · `gh api graphql`/`gh issue` via `gitrepos._run_gh` · one-shot LLM (Haiku) · pytest.

## Global Constraints

- Spec: `docs/superpowers/specs/2026-06-30-backlog-runner-design.md` (§2 selection, §3 spawn, §4 claim/give-up, §Error handling). Depends on Plan A (`BacklogRunnerLoop`, `BacklogRunner`, `should_free_slot`).
- `rank_and_batch` is **pure** (candidates + K + an already-computed ranking → a `Batch`); malformed ranking → deterministic fallback (lowest issue number first).
- The ranking LLM call, the `gh` exec, and the session spawn are **injectable seams** — the end-to-end loop test runs with zero network and zero real sessions (acceptance criterion).
- Claim is atomic/self-healing: record the claimed issue set BEFORE labeling; on partial `gh` failure, roll back labels added in this batch and abort the pick (never spawn a partially/unclaimed batch).
- Give-up at `max_attempts` unclaims ALL batch issues + labels each `needs-human`; the reconciliation sweep clears dangling `in-progress` labels with no live `origin_ref` session.
- The spawn prompt tells the session to implement ALL batch issues and close each with `Fixes #n` (CLAUDE.md auto-close), producing one PR.
- Test env: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 <main-checkout>/.venv/bin/python -m pytest …`.

---

## File Structure

- Create: `server/automations/backlog_selection.py` — add `rank_and_batch` + the `Batch` type (file exists from Plan A).
- Create: `server/automations/backlog_github.py` — `query_candidates`, `claim_batch`, `unclaim_batch`, `label_needs_human`, `reconcile_dangling` (all take an injected `gh_run`).
- Create: `server/automations/backlog_prompt.py` — `build_implement_prompt(batch)`.
- Modify: `server/automations/backlog_runner.py` — assemble `select_batch` (query→rank→batch) from the pieces; the spawn wrapper.
- Modify: `server/api/automations.py` — `POST /api/automations/backlog-runner/implement` (propose tap).
- Create: `server/tests/fakes/fake_backlog.py` — the fake-`gh`/fake-LLM/fake-spawn harness.
- Tests: `server/tests/test_backlog_rank_and_batch.py`, `server/tests/test_backlog_github.py`, `server/tests/test_backlog_runner_e2e.py`.

---

### Task 1: Fake harness (gh / LLM / spawn) — zero-network fixtures

**Files:** Create `server/tests/fakes/fake_backlog.py`; Test: a smoke test that the fakes wire up.

**Interfaces:** `FakeGh` (records argv, returns scripted `CompletedProcess`), `fake_rank(order)` (returns a canned ranking), `FakeSpawn` (writes a `Session` row with `origin="backlog-runner"`). Used by every other task's tests.

- [ ] **Step 1:** Write `fake_backlog.py` with the three fakes (mirror `server/tests/fakes/fake_factory.py` conventions). `FakeGh(scripts: dict[tuple, CompletedProcess])` matches on argv prefix; default success. `FakeSpawn` inserts a `Session` row via `db_session`.
- [ ] **Step 2:** A smoke test constructing each fake and asserting basic behavior (FakeGh records a call; FakeSpawn persists a row queryable by origin).
- [ ] **Step 3:** Run → green.
- [ ] **Step 4: Commit** (`test(backlog-runner): fake gh/LLM/spawn harness`).

---

### Task 2: `rank_and_batch` (pure)

**Files:** Modify `server/automations/backlog_selection.py`; Test `server/tests/test_backlog_rank_and_batch.py`.

**Interfaces:** `Batch` dataclass (`primary: str`, `issues: list[str]`, `repo: str`); `rank_and_batch(candidates, max_issues_per_pr, ranking) -> Batch | None`.

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_backlog_rank_and_batch.py
from server.automations.backlog_selection import rank_and_batch, Batch

C = [  # candidate issues (repo-qualified refs + numbers for fallback ordering)
    {"ref": "o/r#5", "number": 5, "repo": "o/r"},
    {"ref": "o/r#2", "number": 2, "repo": "o/r"},
    {"ref": "o/r#9", "number": 9, "repo": "o/r"},
]


def test_empty_is_none():
    assert rank_and_batch([], 1, lambda cs: []) is None


def test_single_candidate_skips_ranking():
    b = rank_and_batch([C[0]], 3, lambda cs: (_ for _ in ()).throw(AssertionError("no rank")))
    assert b.primary == "o/r#5" and b.issues == ["o/r#5"]


def test_k1_takes_ranked_top():
    b = rank_and_batch(C, 1, lambda cs: ["o/r#9", "o/r#5", "o/r#2"])
    assert b.primary == "o/r#9" and b.issues == ["o/r#9"]


def test_kN_takes_coherent_group_up_to_cap():
    b = rank_and_batch(C, 2, lambda cs: ["o/r#9", "o/r#5", "o/r#2"])
    assert b.primary == "o/r#9" and b.issues == ["o/r#9", "o/r#5"]  # capped at 2


def test_malformed_ranking_falls_back_to_lowest_number():
    b = rank_and_batch(C, 1, lambda cs: ["nonsense", "also-bad"])
    assert b.primary == "o/r#2"  # deterministic fallback: lowest issue number
```

- [ ] **Step 2:** Run → fail.
- [ ] **Step 3: Implement** (append to `backlog_selection.py`):

```python
from dataclasses import dataclass


@dataclass(frozen=True)
class Batch:
    primary: str
    issues: list
    repo: str


def rank_and_batch(candidates: list, max_issues_per_pr: int, ranking) -> "Batch | None":
    """Pure: pick a coherent work item from `candidates`. `ranking(candidates)`
    returns an ordered list of refs (or a coherent group when K>1). ≤1 candidate
    skips ranking. Malformed/empty ranking → deterministic fallback (lowest issue
    number first)."""
    if not candidates:
        return None
    by_ref = {c["ref"]: c for c in candidates}
    if len(candidates) == 1:
        ordered = [candidates[0]["ref"]]
    else:
        ranked = [r for r in (ranking(candidates) or []) if r in by_ref]
        ordered = ranked or [c["ref"] for c in sorted(candidates, key=lambda c: c["number"])]
    chosen = ordered[: max(1, max_issues_per_pr)]
    primary = chosen[0]
    return Batch(primary=primary, issues=list(chosen), repo=by_ref[primary]["repo"])
```

- [ ] **Step 4:** Run → pass.
- [ ] **Step 5: Commit** (`feat(backlog-runner): pure rank_and_batch (coherent batching + fallback)`).

---

### Task 3: Candidate query + claim/unclaim/reconcile (gh, behind a seam)

**Files:** Create `server/automations/backlog_github.py`; Test `server/tests/test_backlog_github.py` (using `FakeGh`).

**Interfaces:** all take `gh_run=gitrepos._run_gh` (injectable):
- `query_candidates(config, *, gh_run) -> list[dict]` — `gh api graphql` for open issues in `enabled_repos`, `eligible_label`, unassigned, NOT `in-progress`/`needs-human` labeled.
- `claim_batch(batch, session_hint, *, gh_run) -> bool` — add `in-progress` label + a comment to every issue; roll back on partial failure.
- `unclaim_batch(batch, *, gh_run)` / `label_needs_human(batch, *, gh_run)`.
- `reconcile_dangling(open_claims, live_origin_refs, *, gh_run)` — remove `in-progress` from claims with no live session past a grace.

- [ ] **Step 1: Failing tests** (with `FakeGh`): query builds the right graphql (asserts label filter + not-in-progress); `claim_batch` labels every issue and returns True; a scripted failure on issue 2 rolls back issue 1's label and returns False; `unclaim_batch`/`label_needs_human` issue the right `gh` calls for every issue; `reconcile_dangling` removes a label for a claim with no live origin_ref.
- [ ] **Step 2:** Run → fail.
- [ ] **Step 3: Implement** each function via `gh_run([...])` (graphql for the read; `gh issue edit --add-label`/`--remove-label` + `gh issue comment` for writes). Record claimed refs before labeling; roll back in a `try/except`.
- [ ] **Step 4:** Run → green.
- [ ] **Step 5: Commit** (`feat(backlog-runner): candidate query + atomic claim/unclaim/reconcile`).

---

### Task 4: One-shot LLM rank + `select_batch` assembly + spawn prompt

**Files:** Modify `server/automations/backlog_runner.py`; Create `server/automations/backlog_prompt.py`; Test extend.

**Interfaces:** `rank_issues(candidates, config)` — a one-shot model query (reuse the `generate_title`/`_default_title_fn` Haiku pattern) returning a ranking; `select_batch(config)` = `query_candidates` → `rank_and_batch(…, ranking=lambda cs: rank_issues(cs, config))`; `build_implement_prompt(batch)`.

- [ ] **Step 1: Failing tests** — `select_batch` with a `FakeGh` (candidates) + a canned `rank_issues` returns the expected `Batch`; malformed model output → the fallback ordering (pure `rank_and_batch` already covers the fallback; here assert the assembly passes the seam through and never calls the real model in tests). `build_implement_prompt(batch)` includes each issue's `Fixes #n` and instructs following the repo completion workflow.
- [ ] **Step 2:** Run → fail.
- [ ] **Step 3: Implement.** `rank_issues` builds a compact prompt (issue titles/numbers) and runs the one-shot model like `generate_title` (config_dir under a healthy account); it's injected into the loop so tests pass a canned function. `build_implement_prompt` (in `backlog_prompt.py`) emits: "Implement these issues: … For each, close it with `Fixes #n` in the PR body. Follow this repo's completion workflow (open a PR, mark ready, arm auto-merge)." `select_batch` wires query→rank_and_batch. The spawn wrapper calls `ctx.spawn_session(origin_ref=batch.primary, dedupe_key=batch.primary, repo=batch.repo, account=…, name=…)` and seeds the first prompt with `build_implement_prompt(batch)`; the batch is recorded in the spawn event `detail`.
- [ ] **Step 4:** Run → green.
- [ ] **Step 5: Commit** (`feat(backlog-runner): one-shot rank + select_batch + batch implement prompt`).

---

### Task 5: End-to-end loop test + propose-tap endpoint

**Files:** Modify `server/api/automations.py` (implement endpoint); Test `server/tests/test_backlog_runner_e2e.py`.

**Interfaces:** `POST /api/automations/backlog-runner/implement` body `{primary}` → claims (if not already) + spawns the batch (propose-mode tap); auth required.

- [ ] **Step 1: Failing tests** — wire Plan A's `BacklogRunnerLoop` with the REAL Plan B seams but the FAKE `gh`/LLM/spawn (no network, no real sessions). Assert the full path: `auto` mode → select→claim→spawn one batch, in-flight cap respected, `spawned` event with `detail.issues`; give-up after `max_attempts` unclaims-all + `needs-human`; a PR-Doctor `gave_up` event for a handed-off target → `needs-human`; de-dup: two ticks/one primary → one session. Endpoint: `POST /implement` requires auth (401 without), 404 unknown primary, spawns on a valid actionable primary.
- [ ] **Step 2:** Run → fail.
- [ ] **Step 3: Implement** the endpoint (reuse `claim`+`spawn`), and confirm the loop e2e passes on the harness.
- [ ] **Step 4: Run the full backlog suite** (Plan A + B) with **no network and no real sessions** → green (this is the spec's acceptance criterion).
- [ ] **Step 5: Commit** (`feat(backlog-runner): propose-tap implement endpoint + zero-network e2e`).

---

## Self-Review

- **Spec coverage:** §2 selection (label-gated query + one-shot rank + rank_and_batch) → Tasks 2-4; §3 spawn/batch prompt → Task 4; §4 claim/unclaim/give-up/reconcile → Task 3 + Task 5; the zero-network harness acceptance criterion → Task 1 + Task 5. ✓
- **Placeholders:** none — `rank_and_batch` is complete; the `gh`/LLM/spawn seams are concrete function contracts with fakes.
- **Type consistency:** `Batch` (primary/issues/repo) flows from `rank_and_batch` → `claim`/`spawn`/`build_implement_prompt` consistently; the loop's injected seams (Plan A) match these signatures.

## Completion

With Plans A+B done, the Backlog Runner is functional and the full automation pipeline is closed: **backlog → runner session → PR → PR Doctor → merge**, all riding the shell with no new storage beyond the shared `PullRequestInfo` field. Full build order: shell (done) → PR-status field extension → PR Doctor A → PR Doctor B → Backlog Runner A → Backlog Runner B.

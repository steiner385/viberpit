# Archaeology Phase 2 — Completeness-Critic Pass Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** After a map-synthesis session opens its feature-map PR, automatically run an independent "coverage critic" session that finds capabilities with no feature doc and appends the missing docs to the **same PR branch** for human review.

**Architecture:** Reuse the existing `_pm_onboard_ctx.spawn_session` seam. The map-synthesis prompt is changed to push its work to a **deterministic branch** (`pm/feature-map-<slug>`). After `pm_onboard_spawn_map` (scope="initial") spawns synthesis, it launches a tracked background task that polls for the map PR to appear (via `pr_for`/`prs_for`), then spawns a fresh critic session whose prompt re-runs the structural census, diffs it against the drafted docs, and commits any missing docs to the same branch. Everything is daemon-side and propose-only — the critic never opens a new PR and never arms auto-merge.

**Tech Stack:** Python 3.12, FastAPI/SQLAlchemy orchestrator, async; existing PM modules `server/pm/classification.py` (pure prompt builders) and `server/sessions/orchestrator.py` (seams); pytest + `pytest-asyncio`.

**Spec:** `docs/superpowers/specs/2026-09-02-archaeology-robust-coverage-design.md` (§3.2 completeness-critic pass; §2 principles; §4 phase 2).

## Global Constraints

- **Propose-only, human-gated (constitution IV):** the critic session opens NO new PR and NEVER arms auto-merge; it only pushes commits to the already-open map PR branch. Copy this into every critic-prompt task verbatim.
- **Internal seams stay private:** `_pm_onboard_spawn_critic` and `_pm_onboard_chain_critic` are `_`-prefixed and called ONLY from within the orchestrator (the chain). They are NOT called by the request/API layer, so they must NOT be added to facade.py / remote.py / orchestrator_rpc.py `_SIMPLE` / the `test_fleet_orchestrator` GLOBAL set. (The four standing guard tests apply only to request-layer-reachable public seams.)
- **Deterministic map branch:** `pm/feature-map-<slug>` for scope="initial". The critic and the chain trigger both rely on this exact name.
- **No new DB models/columns** — reuses `SessionRecord`, `Project`. (Avoids the Postgres-suite/reconcile trap.)
- **Reuse, don't duplicate (constitution VI):** the census/authoritative-docs/anti-bias text already exists in `build_map_synthesis_prompt`; the critic prompt must reference the SAME census surfaces, not fork a second divergent list. Extract shared census text into a module-level constant if it would otherwise be copied.
- Run the gate before every commit: `ruff check server/`, `mypy server/ --ignore-missing-imports`, `pytest server/tests/test_pm_classification.py server/tests/test_pm_orchestrator_seams.py`.

---

### Task 1: Pin the map-synthesis branch to a deterministic name

**Files:**
- Modify: `server/pm/classification.py` (`build_map_synthesis_prompt`, the `scope="initial"` branch-instruction line, ~line 108/222)
- Test: `server/tests/test_pm_classification.py`

**Interfaces:**
- Consumes: existing `build_map_synthesis_prompt(*, slug, repo, default_branch, open_count, closed_count, scope="initial", suggestion=None, item_numbers=())`.
- Produces: for `scope="initial"`, the prompt instructs the agent to create/push branch **`pm/feature-map-<slug>`** (exact). Module-level constant `MAP_BRANCH = "pm/feature-map-{slug}"` helper `map_branch_name(slug: str) -> str`.

- [ ] **Step 1: Write the failing test**

```python
def test_initial_map_prompt_pins_deterministic_branch():
    from server.pm.classification import build_map_synthesis_prompt, map_branch_name
    p = build_map_synthesis_prompt(
        slug="acme", repo="o/acme", default_branch="main",
        open_count=5, closed_count=9, scope="initial")
    assert map_branch_name("acme") == "pm/feature-map-acme"
    assert "pm/feature-map-acme" in p          # the branch is named in the prompt
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest server/tests/test_pm_classification.py::test_initial_map_prompt_pins_deterministic_branch -v`
Expected: FAIL — `ImportError: cannot import name 'map_branch_name'` (and the branch string absent).

- [ ] **Step 3: Write minimal implementation**

Add near the top of `server/pm/classification.py`:

```python
def map_branch_name(slug: str) -> str:
    """Deterministic branch a map-synthesis PR pushes to, so the completeness
    critic (phase 2) can find and augment the SAME PR. One map per project."""
    return f"pm/feature-map-{slug}"
```

In `build_map_synthesis_prompt`, in the `scope="initial"` step-1 branch instruction, name the branch explicitly (keep the "branch off origin/<default_branch>, never local" rule):

```python
f"1. Branch off `origin/{default_branch}` (never local `{default_branch}`) "
f"into a branch named exactly `{map_branch_name(slug)}`, and open ONE "
f"non-draft PR from it.\n"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest server/tests/test_pm_classification.py::test_initial_map_prompt_pins_deterministic_branch -v`
Expected: PASS. Also run the full file to confirm no existing prompt assertion broke: `pytest server/tests/test_pm_classification.py -v`.

- [ ] **Step 5: Commit**

```bash
git add server/pm/classification.py server/tests/test_pm_classification.py
git commit -m "feat(pm): pin map-synthesis branch to pm/feature-map-<slug> for the critic pass"
```

---

### Task 2: Add `build_coverage_critic_prompt`

**Files:**
- Modify: `server/pm/classification.py` (new function; if the census/authoritative-docs/anti-bias text is not already a reusable constant, extract it to one and have BOTH prompts use it)
- Test: `server/tests/test_pm_classification.py`

**Interfaces:**
- Consumes: `map_branch_name` (Task 1); the shared census text.
- Produces: `build_coverage_critic_prompt(*, slug: str, repo: str, default_branch: str, branch: str, open_count: int, closed_count: int) -> str`.

- [ ] **Step 1: Write the failing test**

```python
def test_coverage_critic_prompt_is_same_branch_and_census_driven():
    from server.pm.classification import build_coverage_critic_prompt
    p = build_coverage_critic_prompt(
        slug="acme", repo="o/acme", default_branch="main",
        branch="pm/feature-map-acme", open_count=5, closed_count=9)
    pl = p.lower()
    # Independent second-agent census (the anti-omission check)
    assert "census" in pl
    assert any(k in pl for k in ["cli", "console", "entry point"])
    assert any(k in pl for k in ["router", "endpoint"])
    assert "model" in pl
    # Works on the SAME open PR branch — must NOT open a new PR or arm auto-merge
    assert "pm/feature-map-acme" in p
    assert "do not open a new pull request" in pl or "do not open a new pr" in pl
    assert "auto-merge" in pl  # instructed never to arm it
    # Anti-bias: low-churn foundations are the target, not skip-worthy
    assert any(k in pl for k in ["churn", "activity, not importance", "fundamental"])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest server/tests/test_pm_classification.py::test_coverage_critic_prompt_is_same_branch_and_census_driven -v`
Expected: FAIL — `ImportError: cannot import name 'build_coverage_critic_prompt'`.

- [ ] **Step 3: Write minimal implementation**

Add `build_coverage_critic_prompt`. Reuse the shared census text (extract a module-level `_CENSUS_SURFACES` string from `build_map_synthesis_prompt` if not already shared, and reference it in both). Body instructs, in order:

```python
def build_coverage_critic_prompt(*, slug: str, repo: str, default_branch: str,
                                 branch: str, open_count: int, closed_count: int) -> str:
    """Independent completeness-critic (design §3.2). A FRESH agent, no synthesis
    context, whose only job is to catch capabilities the map omitted — especially
    low-churn foundations (CLI, auth, storage, isolation, config)."""
    return (
        f"You are an independent COVERAGE CRITIC for the product `{slug}` "
        f"(repo `{repo}`). A prior session drafted a feature map on the open PR "
        f"branch `{branch}`. Your ONLY job: find capabilities that have NO feature "
        f"doc, and add them — do not rewrite existing docs.\n\n"
        f"1. `git fetch` and check out `{branch}` (the open map PR). Read the "
        f"drafted docs under `docs/product/features/`.\n"
        f"2. Structural capability CENSUS (churn-independent):\n{_CENSUS_SURFACES}\n"
        f"3. For every census surface with no covering feature doc, ADD a feature "
        f"doc (same outcome template + frontmatter; `links` = feature-to-feature "
        f"only) OR, if it is genuinely not a feature, record it in the PR's "
        f"coverage-audit table as an explicit not-a-feature line.\n"
        f"4. ANTI-BIAS: PR churn signals activity, not importance. Stable, "
        f"rarely-churned code (CLI, auth, storage, isolation primitives, config) "
        f"is frequently the MOST fundamental — a near-zero-churn surface with no "
        f"doc is a reason to ADD one, not to skip it.\n"
        f"5. Commit your additions and PUSH to `{branch}` to update the SAME PR. "
        f"DO NOT open a new pull request. DO NOT arm auto-merge. Leave the PR for "
        f"human review.\n"
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest server/tests/test_pm_classification.py::test_coverage_critic_prompt_is_same_branch_and_census_driven -v`
Expected: PASS. Run the full file too.

- [ ] **Step 5: Commit**

```bash
git add server/pm/classification.py server/tests/test_pm_classification.py
git commit -m "feat(pm): add build_coverage_critic_prompt (census-driven, same-PR, propose-only)"
```

---

### Task 3: Add the internal `_pm_onboard_spawn_critic` seam

**Files:**
- Modify: `server/sessions/orchestrator.py` (new private method near `pm_onboard_spawn_map`, ~line 3529)
- Test: `server/tests/test_pm_orchestrator_seams.py`

**Interfaces:**
- Consumes: `build_coverage_critic_prompt` (Task 2); existing `self._backlog_healthy_account()`, `self._effective_repo_root()`, `self._sdlc_default_branch(repo)`, `gitrepos.ensure_clone`, `self._pm_onboard_ctx.spawn_session(...)`.
- Produces: `async def _pm_onboard_spawn_critic(self, slug: str, repo: str, branch: str) -> "SessionRecord | None"`. Deduped on `origin_ref=f"{slug}:feature-map-critic"`; `detail={"slug": slug, "kind": "feature-map-critic", "branch": branch}`. Returns the record, or `None` when no healthy account.

- [ ] **Step 1: Write the failing test** (append to `test_pm_orchestrator_seams.py`; mirror the `_bare_orch`/`_mk_async`/`_SpawnRec` pattern)

```python
@pytest.mark.asyncio
async def test_spawn_critic_dedupes_and_builds_prompt(db_env):
    orch = _bare_orch()
    orch._backlog_healthy_account = _mk_async("acct1")
    orch._effective_repo_root = _mk_async("/tmp/root")
    orch._sdlc_default_branch = _mk_async("master")
    rec = _SpawnRec()
    orch._pm_onboard_ctx = rec
    with patch("ccswitch.gitrepos.ensure_clone", return_value="/tmp/clone"):
        out = await orch._pm_onboard_spawn_critic(
            "ccswitch", "steiner385/cc-account-switcher", "pm/feature-map-ccswitch")
    assert out is not None
    assert len(rec.calls) == 1
    call = rec.calls[0]
    assert call["origin_ref"] == "ccswitch:feature-map-critic"
    assert call["dedupe_key"] == "ccswitch:feature-map-critic"
    assert call["detail"]["kind"] == "feature-map-critic"
    assert "pm/feature-map-ccswitch" in call["initial_prompt"]


@pytest.mark.asyncio
async def test_spawn_critic_returns_none_without_account(db_env):
    orch = _bare_orch()
    orch._backlog_healthy_account = _mk_async(None)
    orch._effective_repo_root = _mk_async("/tmp/root")
    orch._sdlc_default_branch = _mk_async("master")
    orch._pm_onboard_ctx = _SpawnRec()
    with patch("ccswitch.gitrepos.ensure_clone", return_value="/tmp/clone"):
        out = await orch._pm_onboard_spawn_critic("ccswitch", "o/r", "pm/feature-map-ccswitch")
    assert out is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest server/tests/test_pm_orchestrator_seams.py::test_spawn_critic_dedupes_and_builds_prompt -v`
Expected: FAIL — `AttributeError: 'SessionOrchestrator' object has no attribute '_pm_onboard_spawn_critic'`.

- [ ] **Step 3: Write minimal implementation**

```python
async def _pm_onboard_spawn_critic(self, slug: str, repo: str,
                                   branch: str):
    """Independent completeness-critic session on the open map PR branch
    (design §3.2). Internal-only — spawned by the chain after synthesis; never
    reached by the request layer, so it stays private (no facade/RPC forward).
    Propose-only: augments the SAME PR, never opens a new one."""
    from server.pm.classification import build_coverage_critic_prompt
    account = await self._backlog_healthy_account()
    if account is None:
        return None
    url = f"https://github.com/{repo}.git"
    cwd = await asyncio.to_thread(
        gitrepos.ensure_clone, await self._effective_repo_root(), repo, url)
    default_branch = await self._sdlc_default_branch(repo)
    prompt = build_coverage_critic_prompt(
        slug=slug, repo=repo, default_branch=default_branch, branch=branch,
        open_count=0, closed_count=0)
    origin_ref = f"{slug}:feature-map-critic"
    return await self._pm_onboard_ctx.spawn_session(
        origin_ref=origin_ref, dedupe_key=origin_ref, repo=repo, cwd=cwd,
        account=account, name=f"pm map-critic {slug}", initial_prompt=prompt,
        detail={"slug": slug, "kind": "feature-map-critic", "branch": branch})
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest server/tests/test_pm_orchestrator_seams.py -k spawn_critic -v`
Expected: PASS (both).

- [ ] **Step 5: Commit**

```bash
git add server/sessions/orchestrator.py server/tests/test_pm_orchestrator_seams.py
git commit -m "feat(pm): internal _pm_onboard_spawn_critic seam (deduped, propose-only)"
```

---

### Task 4: Add the chain trigger `_pm_onboard_chain_critic`

**Files:**
- Modify: `server/sessions/orchestrator.py` (new private method)
- Test: `server/tests/test_pm_orchestrator_seams.py`

**Interfaces:**
- Consumes: `self.pr_for(repo, branch)` (returns cached `PullRequestInfo | None`); `_pm_onboard_spawn_critic` (Task 3).
- Produces: `async def _pm_onboard_chain_critic(self, slug: str, repo: str, branch: str, *, attempts: int = 30, delay: float = 30.0) -> "SessionRecord | None"`. Polls `pr_for` up to `attempts` times (sleeping `delay` between); on the first non-None PR, calls `_pm_onboard_spawn_critic(slug, repo, branch)` exactly once and returns its result. Returns `None` on timeout (logs a warning). Sleep via `asyncio.sleep`.

- [ ] **Step 1: Write the failing test** (inject a fake clock + record spawn)

```python
@pytest.mark.asyncio
async def test_chain_critic_waits_for_pr_then_spawns_once(db_env, monkeypatch):
    orch = _bare_orch()
    seen = {"pr": [None, None, object()]}   # PR appears on the 3rd poll
    def fake_pr_for(repo, branch):
        return seen["pr"].pop(0) if seen["pr"] else object()
    orch.pr_for = fake_pr_for
    spawned = []
    async def fake_spawn(slug, repo, branch):
        spawned.append((slug, repo, branch)); return object()
    orch._pm_onboard_spawn_critic = fake_spawn
    import asyncio as _a
    monkeypatch.setattr(_a, "sleep", _mk_async(None))   # no real waiting
    out = await orch._pm_onboard_chain_critic(
        "ccswitch", "o/r", "pm/feature-map-ccswitch", attempts=5, delay=0.0)
    assert out is not None
    assert spawned == [("ccswitch", "o/r", "pm/feature-map-ccswitch")]  # exactly once


@pytest.mark.asyncio
async def test_chain_critic_times_out_without_spawning(db_env, monkeypatch):
    orch = _bare_orch()
    orch.pr_for = lambda repo, branch: None   # PR never appears
    spawned = []
    async def fake_spawn(*a): spawned.append(a); return object()
    orch._pm_onboard_spawn_critic = fake_spawn
    import asyncio as _a
    monkeypatch.setattr(_a, "sleep", _mk_async(None))
    out = await orch._pm_onboard_chain_critic("s", "o/r", "b", attempts=3, delay=0.0)
    assert out is None
    assert spawned == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest server/tests/test_pm_orchestrator_seams.py -k chain_critic -v`
Expected: FAIL — `AttributeError: ... has no attribute '_pm_onboard_chain_critic'`.

- [ ] **Step 3: Write minimal implementation**

```python
async def _pm_onboard_chain_critic(self, slug: str, repo: str, branch: str, *,
                                   attempts: int = 30, delay: float = 30.0):
    """Wait (bounded) for the map-synthesis PR on *branch* to appear, then spawn
    ONE completeness-critic session against it (design §3.2). No webhook: polls
    the PR poller's cache. Best-effort — a timeout just skips the critic."""
    for _ in range(attempts):
        if self.pr_for(repo, branch) is not None:
            return await self._pm_onboard_spawn_critic(slug, repo, branch)
        await asyncio.sleep(delay)
    log.warning("pm_onboard.chain_critic: map PR for %s (%s) never appeared — "
                "skipping critic", slug, branch)
    return None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest server/tests/test_pm_orchestrator_seams.py -k chain_critic -v`
Expected: PASS (both).

- [ ] **Step 5: Commit**

```bash
git add server/sessions/orchestrator.py server/tests/test_pm_orchestrator_seams.py
git commit -m "feat(pm): _pm_onboard_chain_critic — bounded wait for map PR, then spawn critic once"
```

---

### Task 5: Wire the chain into `pm_onboard_spawn_map` (scope="initial")

**Files:**
- Modify: `server/sessions/orchestrator.py` (`pm_onboard_spawn_map`, the `scope == "initial"` success branch, ~line 3522)
- Test: `server/tests/test_pm_orchestrator_seams.py`

**Interfaces:**
- Consumes: `map_branch_name` (Task 1); `_pm_onboard_chain_critic` (Task 4); the tracked bg-task helper (`self._spawn_bg` / the pattern around orchestrator.py:899-912 — use the SAME helper the file already uses to launch fire-and-forget tracked tasks; if it is named differently, match the existing call site).
- Produces: after a successful `scope="initial"` synthesis spawn, a tracked background task runs `_pm_onboard_chain_critic(slug, repo, map_branch_name(slug))`. No behaviour change for `scope="diff"`.

- [ ] **Step 1: Write the failing test**

```python
@pytest.mark.asyncio
async def test_initial_map_spawn_schedules_critic_chain(db_env):
    proj_id = await _seed_project_for_map(db_env)   # helper: project+primary repo, syncing state
    orch = _bare_orch()
    orch._backlog_healthy_account = _mk_async("acct1")
    orch._effective_repo_root = _mk_async("/tmp/root")
    orch._sdlc_default_branch = _mk_async("master")
    orch._pm_onboard_ctx = _SpawnRec()
    chained = []
    async def fake_chain(slug, repo, branch, **kw):
        chained.append((slug, repo, branch))
    orch._pm_onboard_chain_critic = fake_chain
    # capture the bg-task instead of really backgrounding it
    scheduled = []
    orch._spawn_bg = lambda coro, name=None: scheduled.append(coro)
    with patch("ccswitch.gitrepos.ensure_clone", return_value="/tmp/clone"):
        rec = await orch.pm_onboard_spawn_map("ccswitch", scope="initial")
    assert rec is not None
    assert len(scheduled) == 1          # exactly one chain task scheduled
    await scheduled[0]                  # run it
    assert chained == [("ccswitch", "steiner385/cc-account-switcher",
                        "pm/feature-map-ccswitch")]


@pytest.mark.asyncio
async def test_diff_map_spawn_does_not_schedule_critic(db_env):
    await _seed_project_for_map(db_env)
    orch = _bare_orch()
    orch._backlog_healthy_account = _mk_async("acct1")
    orch._effective_repo_root = _mk_async("/tmp/root")
    orch._sdlc_default_branch = _mk_async("master")
    orch._pm_onboard_ctx = _SpawnRec()
    scheduled = []
    orch._spawn_bg = lambda coro, name=None: scheduled.append(coro)
    with patch("ccswitch.gitrepos.ensure_clone", return_value="/tmp/clone"):
        await orch.pm_onboard_spawn_map("ccswitch", scope="diff", suggestion="x")
    assert scheduled == []   # drift path never chains a critic
```

Add a small module-level helper in the test file if `_seed_project_for_map` does not already exist (project with slug "ccswitch", one primary `ProjectRepo` repo="steiner385/cc-account-switcher", `onboarding_state="syncing"`), mirroring the existing `_seed` in `test_pm_orchestrator_seams.py`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest server/tests/test_pm_orchestrator_seams.py -k "schedules_critic or does_not_schedule" -v`
Expected: FAIL — no bg task scheduled (`_spawn_bg` never called from `pm_onboard_spawn_map`).

- [ ] **Step 3: Write minimal implementation**

In `pm_onboard_spawn_map`, inside `if rec is not None and scope == "initial":` after setting `onboarding_state="map_proposed"`:

```python
from server.pm.classification import map_branch_name
self._spawn_bg(
    self._pm_onboard_chain_critic(slug, repo, map_branch_name(slug)),
    name=f"pm-critic-chain-{slug}")
```

(Use the file's actual tracked-bg helper. If it is not named `_spawn_bg`, match the existing fire-and-forget call site near orchestrator.py:899-912 — do NOT introduce a second background-task mechanism.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest server/tests/test_pm_orchestrator_seams.py -k "schedules_critic or does_not_schedule" -v`
Expected: PASS (both). Then the full PM seam + classification files.

- [ ] **Step 5: Commit**

```bash
git add server/sessions/orchestrator.py server/tests/test_pm_orchestrator_seams.py
git commit -m "feat(pm): chain the completeness-critic after an initial map spawn (design #908 phase 2)"
```

---

### Task 6: Full-gate green + branch/PR

**Files:** none (verification + delivery)

- [ ] **Step 1:** Run the four-part gate:
  `ruff check src/ server/` · `mypy server/ --ignore-missing-imports` · `pytest tests/` · `pytest server/tests/` (via the prebuilt venv, SESSIOND env unset, per CLAUDE.md). All green.
- [ ] **Step 2:** Branch off `origin/master` (`git fetch` first), squash-friendly commits already made; push; open a non-draft PR whose body includes the coverage-audit rationale and `Refs #903` + links the design (#908). Arm `--squash --auto`.
- [ ] **Step 3:** Update `docs/superpowers/specs/2026-09-02-archaeology-robust-coverage-design.md` status note or the PM initiative memory that phase 2 shipped (phase 3 = §3.3 correction feedback + §3.4 eval remains).

---

## Self-Review

**Spec coverage (§3.2):**
- "independent second agent" → Task 3 (`_pm_onboard_spawn_critic`, fresh session) ✓
- "after the map-synthesis session opens its PR" → Task 4 (poll `pr_for` for the PR) + Task 5 (chained on scope="initial") ✓
- "walk entry points / routers / models / CLI, list capabilities with no doc" → Task 2 (census-driven critic prompt) ✓
- "output … follow-up diff via the drift path" → user-chosen variant: same-PR follow-up commit (Task 2 prompt: push to `branch`, no new PR) ✓
- "reuses the existing session-spawn seam; modes stay propose-only" → Task 3 uses `_pm_onboard_ctx.spawn_session`; Global Constraints enforce propose-only ✓
- §3.3 correction feedback + §3.4 eval → explicitly deferred to Phase 3 (spec §4.3) — not in this plan ✓

**Placeholder scan:** none — every step has concrete code, exact file paths, and runnable commands.

**Type consistency:** `map_branch_name(slug)` (T1) → used T2/T5; `build_coverage_critic_prompt(*, slug, repo, default_branch, branch, open_count, closed_count)` (T2) → called T3; `_pm_onboard_spawn_critic(slug, repo, branch)` (T3) → called T4; `_pm_onboard_chain_critic(slug, repo, branch, *, attempts, delay)` (T4) → scheduled T5. Signatures consistent across tasks.

**Open verification for the implementer (flagged, not a placeholder):** Task 5 assumes a tracked fire-and-forget helper exists (orchestrator.py:899-912). Confirm its exact name at implementation time and match it; if it takes different arguments, adapt the call and the `test_initial_map_spawn_schedules_critic_chain` monkeypatch accordingly.
